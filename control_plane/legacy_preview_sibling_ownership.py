"""Current generic preview ownership from actual HTTP or application lifecycle receipts."""

from dataclasses import dataclass
from datetime import datetime

import click

from control_plane.contracts.idempotency_record import (
    build_launchplane_idempotency_record_id,
    parse_launchplane_mutation_timestamp,
)
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.generic_web_preview_http import GENERIC_WEB_PREVIEW_REFRESH_ROUTE
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.generic_web_application_snapshot import recorded_application_lifecycle
from control_plane.workflows.generic_web_preview import (
    GenericWebPreviewRefreshResult,
    effective_preview_app_name_prefix,
    preview_application_name,
)
from control_plane.workflows.launchplane import generate_preview_id


@dataclass(frozen=True)
class PreviewSiblingOwnership:
    target_id: str
    target_name: str
    evidence: dict[str, object]
    receipt_ids: tuple[str, ...]


def _time(value: str) -> datetime:
    return parse_launchplane_mutation_timestamp(value, field_name="preview sibling evidence time")


def current_generic_sibling_ownership(
    store: PostgresRecordStore,
    profile: LaunchplaneProductProfileRecord,
    preview: PreviewRecord,
    slug: str,
) -> PreviewSiblingOwnership | None:
    if not preview.serving_generation_id:
        return None
    generation = store.read_preview_generation_record(preview.serving_generation_id)
    runtime = generation.runtime_identity
    if runtime is None:
        return None
    if (
        preview.preview_id
        != generate_preview_id(
            context_name=profile.preview.context,
            anchor_repo=profile.repository.partition("/")[2],
            anchor_pr_number=preview.anchor_pr_number,
        )
        or generation.preview_id != preview.preview_id
        or generation.state != "ready"
        or generation.deploy_status != "pass"
        or generation.verify_status != "pass"
        or (runtime.product, runtime.context, runtime.instance, runtime.environment_kind)
        != (profile.product, preview.context, slug, "preview")
        or runtime.preview_id not in {slug, preview.preview_id}
        or runtime.preview_generation_id not in {"", generation.generation_id}
        or (generation.artifact_id, generation.anchor_summary.head_sha)
        != (runtime.artifact_id, runtime.source_git_ref)
        or generation.anchor_summary.repo != preview.anchor_repo
        or generation.anchor_summary.pr_number != preview.anchor_pr_number
    ):
        return None
    name = preview_application_name(
        app_name_prefix=effective_preview_app_name_prefix(profile=profile), preview_slug=slug
    )
    targets: set[str] = set()
    receipt_ids: tuple[str, ...] = ()
    evidence: dict[str, object] = {
        "preview": preview.model_dump(mode="json"),
        "generation": generation.model_dump(mode="json"),
    }
    # Storage/index failures remain errors. Only the evidence below is optional.
    receipts = store.find_generic_preview_refresh_receipts(runtime.deployment_record_id)
    deployments = store.list_deployment_records(context_name=preview.context, instance_name=slug)
    try:
        requested_at = _time(generation.requested_at)
        ready_at = _time(generation.ready_at)
    except ValueError:
        return None
    unproven = False
    for receipt in receipts:
        try:
            result = GenericWebPreviewRefreshResult.model_validate(
                receipt.response_payload.get("result")
            )
            started_at = _time(result.refresh_started_at)
            finished_at = _time(result.refresh_finished_at)
        except ValueError:
            unproven = True
            continue
        records = receipt.response_payload.get("records")
        if (
            receipt.state != "completed"
            or receipt.route_path != GENERIC_WEB_PREVIEW_REFRESH_ROUTE
            or receipt.response_status_code != 202
            or receipt.record_id
            != build_launchplane_idempotency_record_id(response_trace_id=receipt.response_trace_id)
            or receipt.response_payload.get("status") != "accepted"
            or receipt.response_payload.get("trace_id") != receipt.response_trace_id
            or result.refresh_status != "pass"
            or (
                result.product,
                result.context,
                result.preview_slug,
                result.preview_url,
                result.application_name,
            )
            != (profile.product, preview.context, slug, preview.canonical_url, name)
            or result.runtime_identity != runtime
            or not result.application_id.strip()
            or not isinstance(records, dict)
            or records.get("preview_id") != preview.preview_id
            or records.get("generation_id") != generation.generation_id
            or records.get("transition") not in {"ready", "verifying"}
            or started_at != requested_at
            or finished_at > ready_at
        ):
            unproven = True
            continue
        targets.add(result.application_id)
        receipt_ids = (receipt.record_id,)
        evidence["refresh_receipt"] = receipt.model_dump(mode="json")
    if len(targets) > 1:
        raise ValueError("Generic sibling receipts disagree on the physical application.")
    # These are distinct real DeploymentRecords; runtime's nonce is not their ID.
    lifecycle_records = []
    lifecycle = []
    positive_targets = set(targets)
    for record in deployments:
        if record.delegated_executor != "control-plane.preview-application-lifecycle":
            continue
        try:
            snapshot = recorded_application_lifecycle(record)
            captured_at = _time(snapshot.captured_at) if snapshot is not None else None
        except (ValueError, click.ClickException):
            unproven = True
            continue
        if (
            snapshot is None
            or captured_at is None
            or (
                snapshot.product != profile.product
                or snapshot.provider_id != "dokploy"
                or record.deploy.target_type != "application"
                or record.deploy.deploy_mode != "preview-lifecycle"
                or record.deploy.target_name != snapshot.target_id
            )
        ):
            unproven = True
            continue
        qualified = (
            requested_at <= captured_at <= ready_at
            and snapshot.state == "qualified"
            and snapshot.mode == "deploy"
            and (snapshot.artifact_id, snapshot.source_git_ref)
            == (runtime.artifact_id, runtime.source_git_ref)
            and record.deploy.status == "pass"
            and record.post_deploy_update.status == "pass"
            and bool(snapshot.target_id.strip())
        )
        if qualified:
            positive_targets.add(snapshot.target_id)
        lifecycle.append((captured_at, snapshot, qualified))
        lifecycle_records.append(record.model_dump(mode="json"))
    if len(positive_targets) > 1:
        raise ValueError("Generic sibling receipts disagree on the physical application.")
    if unproven:
        return None
    if lifecycle:
        latest_time = max(captured_at for captured_at, _, _ in lifecycle)
        latest = [item for item in lifecycle if item[0] == latest_time]
        if len(latest) != 1:
            return None
        _, snapshot, qualified = latest[0]
        if latest_time >= requested_at:
            if not qualified:
                # Do not retain an older HTTP exemption after uncertain newer work.
                return None
            targets.add(snapshot.target_id)
    evidence["application_lifecycle_records"] = lifecycle_records
    if len(targets) > 1:
        raise ValueError("Generic sibling receipts disagree on the physical application.")
    if not targets or len(receipts) > 1:
        return None
    return PreviewSiblingOwnership(targets.pop(), name, evidence, receipt_ids)
