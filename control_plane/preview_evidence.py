from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.dokploy import api, post_deploy, source
from control_plane.dokploy.native_copy_stream import NativeSourceAuthorizationDenied
from control_plane.odoo_preview_apply_http import ODOO_PREVIEW_APPLY_ROUTE
from control_plane.odoo_preview_qualification import (
    PreviewQualificationMissing,
    read_odoo_preview_qualification,
    read_preview_inspection_authority,
)
from control_plane.odoo_runtime_reads import OdooRuntimeReadError, assert_selection_current
from control_plane.preview_reservation_recovery import (
    original_preview_intent,
    preview_reservation_reference,
)
from control_plane.product_reconcile import reconcile_reservation_scope
from control_plane.preview_source import PreviewSourceDecision
from control_plane.provider_operations import build_provider_operation_key, provider_operation_title
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.odoo_preview_runtime import OdooPreviewDokployApplyResult


class PreviewEvidenceQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pr_number: int = Field(ge=1)
    source_git_ref: str = Field(pattern=r"^[a-f0-9]{40}$")
    image_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    original_reservation_id: str = Field(
        default="", pattern=r"^(|mutation-reservation-[a-f0-9]{64})$"
    )
    plan_id: str = Field(
        default="", max_length=128, pattern=r"^(|odoo-preview-plan-[A-Za-z0-9_-]+)$"
    )
    preview_id: str = Field(default="", max_length=256)
    generation_id: str = Field(default="", max_length=256)

    @model_validator(mode="after")
    def require_exact_selection(self) -> "PreviewEvidenceQuery":
        if bool(self.original_reservation_id) == bool(self.plan_id):
            raise ValueError("Select one original reservation or controller plan.")
        if bool(self.preview_id) != bool(self.generation_id):
            raise ValueError("Current probes require both preview and generation ids.")
        return self


class PreviewEvidenceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    result: dict[str, object]


def unavailable_history() -> dict[str, object]:
    return {"coverage": "unavailable", "entries": [], "truncated": False, "quiescent": None}


def provider_timestamp(value: api.JsonValue) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.isoformat() if parsed.tzinfo is not None else None
    except ValueError:
        return None


def project_provider_history(
    deployments: list[api.JsonObject], *, original_title: str = ""
) -> dict[str, object]:
    api.require_unique_deployment_ids(deployments)
    terminal = post_deploy.DOKPLOY_TERMINAL_DEPLOYMENT_STATUSES
    known = terminal | post_deploy.DOKPLOY_RUNNING_DEPLOYMENT_STATUSES
    entries = [
        {
            "reference": canonical_json_sha256(api.deployment_key(item)),
            "status": status if (status := api.deployment_status(item)) in known else "unknown",
            "created_at": provider_timestamp(item.get("createdAt")),
            "started_at": provider_timestamp(item.get("startedAt")),
            "finished_at": provider_timestamp(item.get("finishedAt")),
            "original_operation_marker": bool(original_title)
            and item.get("title") == original_title,
        }
        for item in deployments[:100]
    ]
    return {
        "coverage": "complete_retained_history",
        "entries": entries,
        "truncated": len(deployments) > 100,
        "quiescent": all(api.deployment_status(item) in terminal for item in deployments),
        "historical_completeness": "unknown",
        "original_job_binding": "operation_marker_present"
        if any(original_title and item.get("title") == original_title for item in deployments)
        else "unavailable",
    }


def read_preview_provider_history(
    store: PostgresRecordStore,
    *,
    root: Path,
    reservation: LaunchplaneIdempotencyRecord,
    compose_id: str,
) -> dict[str, object]:
    result: dict[str, object] = {
        "deployment": unavailable_history(),
        "schedule": unavailable_history(),
    }
    original_title = provider_operation_title(
        build_provider_operation_key(
            scope=reservation.scope,
            route_path=reservation.route_path,
            idempotency_key=reservation.idempotency_key,
            request_fingerprint=reservation.request_fingerprint,
            reconciliation_key=reservation.reconciliation_key,
        )
    )
    try:
        host, token = source.read_dokploy_config(
            control_plane_root=root, database_url=store.database_url
        )
        if compose_id and not compose_id.startswith("${created."):
            result["deployment"] = project_provider_history(
                api.list_deployments_for_target(
                    host=host,
                    token=token,
                    target_type="compose",
                    target_id=compose_id,
                    require_complete=True,
                ),
                original_title=original_title,
            )
    except api.DokployRequestFailed as error:
        if error.status_code in {401, 403}:
            raise NativeSourceAuthorizationDenied("Provider refused preview history.") from None
    except (ValueError, OSError, click.ClickException):
        result["deployment"] = unavailable_history()
    try:
        retained = store.read_original_mutation_intent(reservation)
        binding = post_deploy.ComposeDataWorkflowBinding.model_validate(
            retained.get("schedule_binding")
        )
        host, token = source.read_dokploy_config(
            control_plane_root=root, database_url=store.database_url
        )
        rows = post_deploy.read_bound_data_workflow_deployments(
            host=host,
            token=token,
            binding=binding,
        )
        if rows is None or len(rows) != 1:
            return result
        result["schedule"] = project_provider_history(rows[0], original_title=original_title)
    except api.DokployRequestFailed as error:
        if error.status_code in {401, 403}:
            raise NativeSourceAuthorizationDenied("Provider refused preview history.") from None
    except (ValueError, OSError, click.ClickException):
        return result
    return result


def read_preview_evidence(
    store: PostgresRecordStore, *, product: str, query: PreviewEvidenceQuery, root: Path
) -> dict[str, object]:
    profile = store.read_product_profile_record(product)
    reservation = (
        store.read_idempotency_record_by_id(query.original_reservation_id)
        if query.original_reservation_id
        else store.read_idempotency_record(
            scope=reconcile_reservation_scope(product),
            route_path=ODOO_PREVIEW_APPLY_ROUTE,
            idempotency_key=query.plan_id,
        )
    )
    if reservation is None:
        raise OdooRuntimeReadError(
            "preview_original_unavailable", "Original preview record unavailable.", 404
        )
    if query.original_reservation_id and reservation.record_id != query.original_reservation_id:
        raise OdooRuntimeReadError(
            "preview_original_binding_mismatch", "Original identity differs."
        )
    try:
        original = original_preview_intent(reservation, profile=profile, store=store)
    except ValueError:
        raise OdooRuntimeReadError(
            "preview_original_binding_unavailable", "Original preview intent cannot be bound."
        ) from None
    issued = original.issued_plan
    manifest = original.apply_request.apply.manifest
    if (
        issued.operation != "refresh"
        or issued.plan_request.pr_number != query.pr_number
        or issued.plan_request.source_git_ref != query.source_git_ref
        or manifest is None
        or manifest.source_commit != query.source_git_ref
        or manifest.image.digest != query.image_digest
    ):
        raise OdooRuntimeReadError(
            "preview_original_binding_mismatch", "The original differs from the requested tuple."
        )
    retained = store.read_original_mutation_intent(reservation)
    reference = preview_reservation_reference(reservation, retained)
    try:
        source_decision = PreviewSourceDecision.model_validate(retained.get("effective_source"))
    except ValueError:
        source_decision = None
    current: dict[str, object] = {
        "usage": "unknown",
        "production_resources": "unknown",
        "data": "unknown",
        "runtime_identity": "unknown",
        "serving_process": "unknown",
        "credential_isolation": "unknown",
        "credential_comparison_scope": "managed_and_provider_production_values",
        "database_only_production_credentials": "unavailable",
        "reason_code": "not_requested",
    }
    authority = None
    sequence = None
    if query.preview_id:
        records = reservation.response_payload.get("records")
        if not isinstance(records, dict) or records.get("preview_id") != query.preview_id:
            raise OdooRuntimeReadError(
                "preview_original_binding_mismatch", "The preview is not bound to this original."
            )
        preview = store.read_preview_record(query.preview_id)
        if preview.context != profile.preview.context:
            raise OdooRuntimeReadError(
                "preview_original_binding_mismatch", "Preview context differs."
            )
        if preview.serving_generation_id != query.generation_id:
            raise OdooRuntimeReadError(
                "preview_generation_mismatch", "The requested generation is not serving."
            )
        authority = read_preview_inspection_authority(store, preview)
        if authority.selection.identity.deployment_record_id != reservation.record_id:
            raise OdooRuntimeReadError(
                "preview_original_binding_mismatch", "The generation belongs to another original."
            )
        sequence = store.read_preview_generation_record(query.generation_id).sequence
        try:
            qualification = read_odoo_preview_qualification(store, preview=preview, root=root)
            current.update(
                usage=qualification.usage.usage,
                production_resources="isolated",
                runtime_identity="verified",
                serving_process="verified",
                credential_isolation="separated",
                **qualification.usage.payload(),
            )
        except PreviewQualificationMissing as error:
            checkpoint = str(error).partition(":")[0]
            if checkpoint == "preview_production_resources_shared":
                current.update(production_resources="shared", reason_code=checkpoint)
            elif checkpoint == "preview_production_credential_separation_unproven":
                current.update(credential_isolation="shared", reason_code=checkpoint)
            else:
                current["reason_code"] = error.reason or "preview_qualification_unavailable"
    compose_id = issued.dry_run_plan.compose_ref
    result_status = None
    if reservation.state == "completed":
        try:
            result = OdooPreviewDokployApplyResult.model_validate(
                reservation.response_payload.get("result")
            )
            if (
                result.product == profile.product
                and result.repository == profile.repository
                and result.compose_name == issued.dry_run_plan.compose_name
                and result.preview_slug == issued.preview_slug
                and result.operation == issued.operation
                and (
                    issued.dry_run_plan.compose_ref.startswith("${created.")
                    or issued.dry_run_plan.compose_ref == result.compose_id
                )
            ):
                compose_id = result.compose_id
                result_status = result.status
        except ValueError:
            result_status = None
    provider = read_preview_provider_history(
        store, root=root, reservation=reservation, compose_id=compose_id
    )
    if authority is not None:
        try:
            assert_selection_current(store, authority.selection)
        except (FileNotFoundError, ValueError):
            raise OdooRuntimeReadError(
                "preview_evidence_changed", "Preview selection changed during observation."
            ) from None
        if (
            read_preview_inspection_authority(store, store.read_preview_record(query.preview_id))
            != authority
        ):
            raise OdooRuntimeReadError(
                "preview_evidence_changed", "Preview authority changed during observation."
            )
    after = store.read_idempotency_record_by_id(reservation.record_id)
    if (
        after is None
        or preview_reservation_reference(after, store.read_original_mutation_intent(after))
        != reference
    ):
        raise OdooRuntimeReadError(
            "preview_evidence_changed", "The original changed during observation."
        )
    return {
        "read_only": True,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "binding": {
            "product": product,
            "pr_number": query.pr_number,
            "source_git_ref": query.source_git_ref,
            "image_digest": query.image_digest,
            "artifact_id": manifest.artifact_id,
            "preview_id": query.preview_id or None,
            "generation_id": query.generation_id or None,
            "generation_sequence": sequence,
        },
        "original": {
            "reservation_id": reservation.record_id,
            "reference": reference,
            "plan_id": reservation.idempotency_key,
            "state": reservation.state,
            "attempt": reservation.attempt,
            "created_at": reservation.created_at,
            "updated_at": reservation.updated_at,
            "lease_expires_at": reservation.lease_expires_at,
            "executor_excluded": None,
            "provider_effect_phase": reservation.provider_effect_phase or None,
            "provider_effect_started_at": reservation.provider_effect_started_at or None,
            "response_status_code": reservation.response_status_code,
            "result_status": result_status,
            "recorded_at": reservation.recorded_at or None,
            "intent_coverage": "validated_original",
            "effective_source": source_decision.model_dump(mode="json")
            if source_decision
            else None,
            "schedule_binding_available": bool(retained.get("schedule_binding")),
        },
        "current": current,
        "provider": provider,
        "coverage": {
            "data_custody": "unavailable",
            "historic_provider_proof": "unknown",
            "admission": "not_published",
            "settlement": "not_performed",
        },
    }
