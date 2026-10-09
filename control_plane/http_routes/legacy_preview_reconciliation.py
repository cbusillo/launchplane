import asyncio
from typing import Annotated

import click
from fastapi import Depends, Header, Request

from control_plane.http_routes.generic_web import GenericWebWriteRouteDependencies
from control_plane.http_routes.mutation_support import (
    AcceptedEvidenceResponse,
    accepted_evidence_response,
    idempotency_scope,
)
from control_plane.http_routes.support import ApiRouteRegistrar
from control_plane.legacy_preview_reconciliation import (
    LEGACY_PREVIEW_RECONCILIATION_ROUTE,
    LegacyPreviewReconciliationRequest,
    plan_legacy_preview,
)
from control_plane.service_auth import AuthorizationTarget, LaunchplaneIdentity
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.generic_web_preview import (
    resolve_generic_web_preview_slug,
    serialize_generic_web_preview_operation,
)
from control_plane.workflows.ship import utc_now_timestamp


def register_legacy_preview_reconciliation_route(
    app: ApiRouteRegistrar, *, dependencies: GenericWebWriteRouteDependencies
) -> None:
    async def reconcile_legacy_generic_web_preview(
        request: Request,
        reconciliation: LegacyPreviewReconciliationRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_write_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = dependencies.next_trace_id()
        if not isinstance(record_store, PostgresRecordStore):
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message="Reconciliation requires database storage.",
            )
        try:
            profile = record_store.read_product_profile_record(reconciliation.product)
        except FileNotFoundError as error:
            raise dependencies.http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message="Product profile was not found.",
            ) from error
        actions = ["preview_inventory.read"]
        if reconciliation.mode == "apply":
            actions += ["preview_destroy.execute", "preview_destroyed.write"]
        for action in actions:
            if not dependencies.authorization_allows(
                identity=identity,
                action=action,
                product=profile.product,
                context=profile.preview.context,
                target=AuthorizationTarget(
                    scope="preview" if action == "preview_destroy.execute" else "context"
                ),
            ):
                raise dependencies.http_error(
                    status_code=403,
                    trace_id=trace_id,
                    code="authorization_denied",
                    message=f"Caller lacks {action} for this preview context.",
                )
        if reconciliation.mode != "inspect" and not idempotency_key.strip():
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="idempotency_key_required",
                message="Plan and apply require Idempotency-Key.",
            )
        replay_args = dict(
            request=request,
            record_store=record_store,
            identity=identity,
            route_path=LEGACY_PREVIEW_RECONCILIATION_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=reconciliation.mode != "inspect",
        )
        key, fingerprint, replay = await dependencies.replay_apply_idempotency(**replay_args)
        if reconciliation.mode != "inspect" and replay is not None:
            return replay

        def execute() -> AcceptedEvidenceResponse:
            preview = record_store.read_preview_record(reconciliation.preview_id)
            slug = resolve_generic_web_preview_slug(
                profile=profile,
                preview_slug="",
                anchor_pr_number=preview.anchor_pr_number,
                label="Reconciliation",
            )
            with serialize_generic_web_preview_operation(
                record_store=record_store, profile=profile, preview_slug=slug
            ):
                if reconciliation.mode == "apply":
                    saved = record_store.read_idempotency_record(
                        scope=idempotency_scope(identity),
                        route_path=LEGACY_PREVIEW_RECONCILIATION_ROUTE,
                        idempotency_key=reconciliation.plan_idempotency_key,
                    )
                    saved_result = saved.response_payload.get("result") if saved else None
                    if (
                        saved is None
                        or saved.state != "completed"
                        or not isinstance(saved_result, dict)
                        or saved_result.get("mode") != "plan"
                        or saved_result.get("apply_eligible") is not True
                        or saved_result.get("plan_digest") != reconciliation.expected_plan_digest
                    ):
                        raise ValueError("Reviewed plan is missing or bound to another caller.")
                bound, result = plan_legacy_preview(
                    store=record_store,
                    control_plane_root=dependencies.control_plane_root,
                    request=reconciliation,
                    caller_scope=idempotency_scope(identity),
                )
                # The scoped context used for authorization cannot drift while acquiring the lock.
                if bound.profile.preview.context != profile.preview.context or bound.slug != slug:
                    raise ValueError("Authorization context changed during inspection.")
                if reconciliation.mode == "apply":
                    if (
                        result["plan_digest"] != reconciliation.expected_plan_digest
                        or result["apply_eligible"] is not True
                    ):
                        raise ValueError(
                            "Reviewed plan is missing, blocked, changed or bound to another caller."
                        )
                    result.update(
                        mode="apply", preview_state="destroyed", destroyed_at=utc_now_timestamp()
                    )
                else:
                    result["mode"] = reconciliation.mode
                response = accepted_evidence_response(
                    trace_id=trace_id,
                    records={"preview_id": bound.preview.preview_id},
                    result=result,
                )
                if reconciliation.mode != "inspect":
                    completion = dependencies.build_apply_idempotency_record(
                        identity=identity,
                        route_path=LEGACY_PREVIEW_RECONCILIATION_ROUTE,
                        idempotency_key=key,
                        request_fingerprint_value=fingerprint,
                        trace_id=trace_id,
                        response=response,
                    )
                    if reconciliation.mode == "apply":
                        record_store.commit_legacy_preview_reconciliation(
                            reconciliation=reconciliation,
                            expected_authority_digest=bound.digest,
                            destroyed_at=str(result["destroyed_at"]),
                            completion=completion,
                        )
                    else:
                        record_store.write_legacy_preview_plan(completion)
                return response

        try:
            # Preserve completion/replay evidence even when the HTTP caller disconnects.
            return await asyncio.shield(asyncio.to_thread(execute))
        except (ValueError, FileNotFoundError, click.ClickException) as error:
            if reconciliation.mode != "inspect":
                _, _, completed = await dependencies.replay_apply_idempotency(**replay_args)
                if completed is not None:
                    return completed
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace_id,
                code="legacy_preview_reconciliation_blocked",
                message="Legacy preview evidence is missing, uncertain or changed; no reconciliation applied.",
            ) from error

    app.add_api_route(
        LEGACY_PREVIEW_RECONCILIATION_ROUTE,
        reconcile_legacy_generic_web_preview,
        methods=["POST"],
        response_model=AcceptedEvidenceResponse,
        status_code=202,
        operation_id="reconcile_legacy_generic_web_preview",
        summary="Inspect or reconcile one provider-absent legacy generic-web preview",
        responses={
            status: {"model": dependencies.error_response_model}
            for status in (400, 403, 404, 409, 503)
        },
    )
