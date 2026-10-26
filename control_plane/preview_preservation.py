from __future__ import annotations

from datetime import timedelta
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import click
from pydantic import BaseModel, ConfigDict

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.dokploy import api, source
from control_plane.odoo_preview_resources import preview_resource_environment
from control_plane.storage.postgres import PostgresRecordStore

if TYPE_CHECKING:
    from control_plane.workflows.odoo_preview_runtime import OdooPreviewApplyInputsResult

PREVIEW_PRESERVATION_INSPECT_PHASE = "preview_preservation_inspect"
PREVIEW_PRESERVATION_CAPTURE_PHASE = "preview_preservation_capture"
PREVIEW_PRESERVATION_PHASES = frozenset(
    {PREVIEW_PRESERVATION_INSPECT_PHASE, PREVIEW_PRESERVATION_CAPTURE_PHASE}
)


def is_retained_preservation_retry(
    *,
    store: object,
    profile: LaunchplaneProductProfileRecord,
    issued_plan: OdooPreviewApplyInputsResult,
    request: object,
    operation_record_id: str,
    reservation: object,
) -> bool:
    from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
    from control_plane.odoo_preview_apply_http import (
        ODOO_PREVIEW_APPLY_ROUTE,
        OdooPreviewApplyEnvelope,
    )
    from control_plane.preview_reservation_recovery import original_preview_intent

    if (
        not isinstance(store, PostgresRecordStore)
        or not isinstance(request, OdooPreviewApplyEnvelope)
        or not request.preserve_original_reservation_id
        or not isinstance(reservation, LaunchplaneIdempotencyRecord)
        or reservation.record_id != operation_record_id
        or reservation.route_path != ODOO_PREVIEW_APPLY_ROUTE
        or reservation.state != "running"
        or reservation.attempt <= 1
        or reservation.provider_effect_phase not in PREVIEW_PRESERVATION_PHASES
    ):
        return False
    current = store.read_idempotency_record_by_id(operation_record_id)
    heartbeat_fields = {"updated_at", "lease_expires_at"}
    if current is None or current.model_dump(exclude=heartbeat_fields) != reservation.model_dump(
        exclude=heartbeat_fields
    ):
        return False
    retained = original_preview_intent(reservation, profile=profile, store=store)
    normalized = request.model_copy(
        update={"apply": request.apply.model_copy(update={"environment_values": {}})}
    )
    return retained.issued_plan == issued_plan and retained.apply_request == normalized


class PreviewPreservationAuthority(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    original_reservation_id: str
    original_identity_sha256: str
    original_intent_sha256: str
    product: str
    context: str
    preview_slug: str
    pull_request_number: int
    compose_id: str
    compose_name: str
    server_id: str

    @property
    def preservation_id(self) -> str:
        return canonical_json_sha256(self.model_dump(mode="json"))


def read_preview_preservation_authority(
    *,
    store: PostgresRecordStore,
    profile: LaunchplaneProductProfileRecord,
    issued_plan: OdooPreviewApplyInputsResult,
    original_reservation_id: str,
    operation_record_id: str = "",
    captured_schedule_binding: dict[str, object] | None = None,
) -> PreviewPreservationAuthority:
    from control_plane.preview_reservation_recovery import original_preview_intent

    original = store.read_idempotency_record_by_id(original_reservation_id)
    if original is None:
        raise ValueError("Original preview preservation authority is unavailable.")
    retained = original_preview_intent(original, profile=profile, store=store)
    old = retained.issued_plan
    old_target = old.dry_run_plan
    target = issued_plan.dry_run_plan
    snapshot = old_target.clean_snapshot
    if (
        issued_plan.operation != "destroy"
        or old.operation != "refresh"
        or issued_plan.product != profile.product
        or issued_plan.repository != profile.repository
        or issued_plan.context != profile.preview.context
        or (old.context, old.preview_slug, old.plan_request.pr_number)
        != (issued_plan.context, issued_plan.preview_slug, issued_plan.plan_request.pr_number)
        or old_target.compose_name != target.compose_name
        or old_target.environment_id != target.environment_id
        or not target.compose_ref
        or target.compose_ref.startswith("${created.")
        or (
            not old_target.compose_ref.startswith("${created.")
            and old_target.compose_ref != target.compose_ref
        )
    ):
        raise ValueError("Original preview preservation does not match this destroy target.")
    stable_identity = original.model_dump(
        include={
            "record_id",
            "scope",
            "route_path",
            "idempotency_key",
            "request_fingerprint",
            "reconciliation_key",
            "provider_target_key",
            "created_at",
        },
        mode="json",
    )
    return PreviewPreservationAuthority(
        original_reservation_id=original.record_id,
        original_identity_sha256=canonical_json_sha256(stable_identity),
        original_intent_sha256=canonical_json_sha256(store.read_original_mutation_intent(original)),
        product=profile.product,
        context=issued_plan.context,
        preview_slug=issued_plan.preview_slug,
        pull_request_number=issued_plan.plan_request.pr_number,
        compose_id=target.compose_ref,
        compose_name=target.compose_name,
        server_id=snapshot.server_id
        if snapshot is not None
        else _preservation_execution_server(
            store=store,
            profile=profile,
            issued_plan=issued_plan,
            original_reservation_id=original_reservation_id,
            operation_record_id=operation_record_id,
            captured_schedule_binding=captured_schedule_binding,
        ),
    )


def _preservation_execution_server(
    *,
    store: PostgresRecordStore,
    profile: LaunchplaneProductProfileRecord,
    issued_plan: OdooPreviewApplyInputsResult,
    original_reservation_id: str,
    operation_record_id: str,
    captured_schedule_binding: dict[str, object] | None,
) -> str:
    from control_plane.dokploy.post_deploy import (
        ComposeDataWorkflowBinding,
        _build_dokploy_data_workflow_schedule_app_name,
    )
    from control_plane.preview_reservation_recovery import original_preview_intent

    original = store.read_idempotency_record_by_id(original_reservation_id)
    if original is None:
        raise ValueError("Original preview preservation server is unavailable.")
    bindings: list[object] = [store.read_original_mutation_intent(original).get("schedule_binding")]
    operation = (
        store.read_idempotency_record_by_id(operation_record_id) if operation_record_id else None
    )
    if operation is not None:
        retained = original_preview_intent(operation, profile=profile, store=store)
        if (
            retained.issued_plan != issued_plan
            or retained.apply_request.preserve_original_reservation_id != original_reservation_id
        ):
            raise ValueError("The preserving operation server binding changed.")
        bindings.append(store.read_original_mutation_intent(operation).get("schedule_binding"))
    else:
        bindings.append(captured_schedule_binding)
    expected_app = _build_dokploy_data_workflow_schedule_app_name(
        context_name=profile.product,
        instance_name=issued_plan.preview_slug,
    )
    servers: set[str] = set()
    for value in bindings:
        if value is None:
            continue
        binding = ComposeDataWorkflowBinding.model_validate(value)
        if binding.schedule_type != "server" or binding.app_name != expected_app:
            raise ValueError("Original preview preservation server binding is unavailable.")
        servers.add(binding.lookup_id)
    if len(servers) != 1:
        raise ValueError("Original preview preservation server is missing or conflicting.")
    return servers.pop()


def require_preview_preservation_admission(
    *,
    store: PostgresRecordStore,
    profile: LaunchplaneProductProfileRecord,
    issued_plan: OdooPreviewApplyInputsResult,
    original_reservation_id: str,
    operation_record_id: str,
    captured_schedule_binding: dict[str, object] | None = None,
) -> PreviewPreservationAuthority:
    from control_plane.contracts.idempotency_record import parse_launchplane_mutation_timestamp
    from control_plane.workflows.odoo_preview_runtime import ODOO_PREVIEW_SUPERSESSION_GRACE_SECONDS

    authority = read_preview_preservation_authority(
        store=store,
        profile=profile,
        issued_plan=issued_plan,
        original_reservation_id=original_reservation_id,
        operation_record_id=operation_record_id,
        captured_schedule_binding=captured_schedule_binding,
    )
    original = store.read_idempotency_record_by_id(original_reservation_id)
    if original is None:
        raise ValueError("Original preview preservation authority disappeared.")
    target = issued_plan.dry_run_plan
    if any(
        held.record_id not in {original.record_id, operation_record_id}
        for held in store.list_held_provider_target_reservations(
            provider_target_key=original.provider_target_key
        )
    ):
        raise ValueError("Another original operation holds this preview target.")
    if original.state != "completed":
        lookup = store.lookup_existing_mutation_reservation(
            route_path=original.route_path,
            idempotency_key=original.idempotency_key,
            request_fingerprint=original.request_fingerprint,
        )
        if (
            lookup.record != original
            or not original.lease_expires_at
            or parse_launchplane_mutation_timestamp(
                original.lease_expires_at, field_name="lease_expires_at"
            )
            + timedelta(seconds=ODOO_PREVIEW_SUPERSESSION_GRACE_SECONDS)
            > parse_launchplane_mutation_timestamp(lookup.observed_at, field_name="observed_at")
        ):
            raise ValueError("Original preview preservation is waiting for its recovery grace.")
    preview_matches = [
        preview
        for preview in store.list_preview_records(context_name=issued_plan.context)
        if profile.preview.slug_template.format(number=preview.anchor_pr_number)
        == issued_plan.preview_slug
    ]
    if preview_matches:
        raise ValueError("Original-only preservation requires an unrecorded preview.")
    if any(
        other.product != profile.product
        and issued_plan.context
        in {
            other.preview.context,
            *other.historical_contexts,
            *(lane.context for lane in other.lanes),
        }
        for other in store.list_product_profile_records()
    ):
        raise ValueError("Original preview preservation has shared context ownership.")
    for target_id in store.list_dokploy_target_id_records():
        if (target_id.context, target_id.instance) == (
            issued_plan.context,
            issued_plan.preview_slug,
        ) and target_id.target_id != target.compose_ref:
            raise ValueError("Original preview preservation target record changed.")
        if target_id.target_id == target.compose_ref and (
            target_id.context,
            target_id.instance,
        ) != (issued_plan.context, issued_plan.preview_slug):
            raise ValueError("Original preview preservation aliases another lane.")
    report = store.read_disposable_lane_safety(
        profile.product, issued_plan.context, issued_plan.preview_slug
    )
    if report is not None and (
        report.usage == "in_use"
        or report.data == "only_copy"
        or report.production_resources in {"shared", "separation_ready"}
    ):
        raise ValueError("Original preview preservation has a protected use or resource.")
    return authority


def read_preview_preservation_source(
    *, authority: PreviewPreservationAuthority, control_plane_root: Path, database_url: str
) -> dict[str, object] | None:
    from control_plane.odoo_preview_resources import (
        _preview_resource_binding,
        _require_preview_destroy_source,
    )

    host, token = source.read_dokploy_config(
        control_plane_root=control_plane_root, database_url=database_url
    )
    try:
        payload = api.fetch_dokploy_target_payload(
            host=host, token=token, target_type="compose", target_id=authority.compose_id
        )
    except api.DokployRequestFailed as error:
        if error.status_code == 404:
            return None
        raise
    _require_preview_destroy_source(payload)
    project = payload.get("appName")
    if (
        payload.get("name") != authority.compose_name
        or payload.get("serverId") != authority.server_id
        or not isinstance(project, str)
        or not project
    ):
        raise click.ClickException("Original preview provider ownership changed.")
    environment = preview_resource_environment(authority.compose_name)
    binding = _preview_resource_binding(
        payload, compose_id=authority.compose_id, environment=environment
    )
    return {
        "compose_project": project,
        "database_name": environment["ODOO_DB_NAME"],
        "preview_volumes": {
            "data": environment["ODOO_DATA_VOLUME"],
            "database": environment["ODOO_DB_VOLUME"],
            "logs": environment["ODOO_LOG_VOLUME"],
        },
        "provider_binding_sha256": binding,
    }


def run_preview_preservation(
    *,
    store: PostgresRecordStore,
    profile: LaunchplaneProductProfileRecord,
    issued_plan: OdooPreviewApplyInputsResult,
    original_reservation_id: str,
    operation_record_id: str,
    control_plane_root: Path,
    mode: Literal["preserve", "verify", "inspect"],
    timeout_seconds: int,
    check: Callable[[], None],
    effect_checkpoint: Callable[[str], None] | None = None,
    mutation_guard: Callable[[], AbstractContextManager[None]] | None = None,
) -> dict[str, object]:
    from control_plane._preview_preservation_program import (
        PREVIEW_PRESERVATION_CLEANUP_SECONDS,
        PREVIEW_PRESERVATION_PROGRAM,
    )
    from control_plane.clean_testing_snapshot import snapshot_storage_budget
    from control_plane.dokploy.memory_capacity import preview_host_memory_floor
    from control_plane.dokploy.native_copy_stream import _read_remote
    from control_plane.odoo_preview_resources import require_existing_preview_resources

    authority = read_preview_preservation_authority(
        store=store,
        profile=profile,
        issued_plan=issued_plan,
        original_reservation_id=original_reservation_id,
        operation_record_id=operation_record_id,
    )
    current_source = read_preview_preservation_source(
        authority=authority, control_plane_root=control_plane_root, database_url=store.database_url
    )
    host, token = source.read_dokploy_config(
        control_plane_root=control_plane_root, database_url=store.database_url
    )
    budget = (
        snapshot_storage_budget(profile, profile.preview.context) if mode == "preserve" else None
    )
    if mode == "preserve":
        if effect_checkpoint is None or budget is None:
            raise ValueError("Preview preservation requires durable custody and reviewed capacity.")
        require_preview_preservation_admission(
            store=store,
            profile=profile,
            issued_plan=issued_plan,
            original_reservation_id=original_reservation_id,
            operation_record_id=operation_record_id,
        )
        if current_source is not None:
            with mutation_guard() if mutation_guard else nullcontext():
                check()
                effect_checkpoint(PREVIEW_PRESERVATION_INSPECT_PHASE)
            binding = require_existing_preview_resources(
                host=host,
                token=token,
                compose_id=authority.compose_id,
                compose_name=authority.compose_name,
                context=profile.product,
                instance=authority.preview_slug,
                environment=preview_resource_environment(authority.compose_name),
                checkpoint=check,
                operation="destroy",
            )
            with mutation_guard() if mutation_guard else nullcontext():
                check()
                if binding != current_source["provider_binding_sha256"]:
                    raise ValueError("Original preview changed during preservation qualification.")

    def dispatch(send: Callable[[], None]) -> None:
        check()
        if mode != "preserve":
            send()
            return
        assert effect_checkpoint is not None
        with mutation_guard() if mutation_guard else nullcontext():
            check()
            effect_checkpoint(PREVIEW_PRESERVATION_CAPTURE_PHASE)
            send()

    frames: list[dict[str, object]] = []
    _read_remote(
        host=host,
        token=token,
        server_id=authority.server_id,
        program=PREVIEW_PRESERVATION_PROGRAM,
        payload={
            "mode": mode,
            "authority": authority.model_dump(mode="json"),
            "preservation_id": authority.preservation_id,
            "source": current_source,
            "storage_budget": budget.model_dump(mode="json") if budget else {},
            "memory_floor": preview_host_memory_floor(
                server_id=authority.server_id, record_store=store
            )
            if mode == "preserve"
            else None,
            "timeout_seconds": timeout_seconds,
        },
        receive=frames.append,
        timeout_seconds=timeout_seconds + 2 * PREVIEW_PRESERVATION_CLEANUP_SECONDS,
        dispatch_guard=dispatch,
    )
    check()
    if (
        len(frames) != 1
        or frames[0].get("type") != "complete"
        or frames[0].get("preservation_id") != authority.preservation_id
        or not isinstance(frames[0].get("source"), dict)
        or not isinstance(frames[0].get("volume_binding"), dict)
        or (current_source is not None and frames[0]["source"] != current_source)
        or (mode == "inspect" and frames[0].get("retry_safe") is not True)
    ):
        raise click.ClickException("Original preview preservation is unavailable or incomplete.")
    return frames[0]
