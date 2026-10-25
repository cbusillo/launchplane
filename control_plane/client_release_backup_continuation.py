import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from control_plane.client_release import (
    ClientReleaseNotReady,
    _covering_decision,
    _generic_web_drill_target,
    _require_current_release,
    client_release_step_operation_id,
    client_release_steps,
    client_release_grant_allows,
    read_client_release_step_views,
)
from control_plane.contracts.verireel_prod_backup_gate_operation import (
    VeriReelProdBackupGateOperationRecord,
)
from control_plane.guest_capture_credentials import guest_capture_metadata
from control_plane.odoo_business_hours import require_release_window
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.production_backup_gate import resolve_production_backup_binding
from control_plane.workflows.ship import utc_now_timestamp

BACKUP_CONTINUATION_ROUTE = (
    "/v1/products/{product}/releases/{decision_record_id}/backup-continuation"
)
BACKUP_CONTINUATION_ACTION = "client_release_backup.continue"


class BackupContinuationRefused(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class BackupContinuationReview(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str = Field(min_length=1, max_length=1000)
    expected_digest: str = Field(default="", pattern=r"^(?:[0-9a-f]{64})?$")
    apply: bool = False


class BackupContinuationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trace_id: str
    decision_record_id: str
    operation_id: str
    review_digest: str
    status: Literal["ready", "queued", "replayed"]
    provider_mutation: bool = False
    apply_authorized: bool = False


def require_precapture_failure(operation: VeriReelProdBackupGateOperationRecord) -> None:
    if (
        operation.schema_version != 3
        or operation.binding is None
        or operation.status != "fail"
        or operation.phase != "failed"
        or operation.error_code != "backup_preflight_unavailable"
        or operation.progress_evidence != {"provider_stage": "backup_preflight"}
        or operation.result is not None
        or operation.cancellation is not None
        or not operation.finished_at
    ):
        raise BackupContinuationRefused("backup_capture_outcome_not_proven_absent")


def _has_continuation_receipt(
    operation: VeriReelProdBackupGateOperationRecord,
    review: BackupContinuationReview,
    actor: str,
) -> bool:
    if not review.apply or not review.expected_digest:
        return False
    prior = operation
    while prior.previous_attempt is not None:
        if prior.continuation_digest == review.expected_digest:
            if prior.continuation_reason != review.reason or prior.continuation_actor != actor:
                raise BackupContinuationRefused("backup_continuation_replay_conflict")
            return True
        prior = prior.previous_attempt
    return False


def continue_release_backup(
    *,
    store: PostgresRecordStore,
    root: Path,
    product: str,
    decision_record_id: str,
    review: BackupContinuationReview,
    actor: str,
    trace_id: str,
) -> BackupContinuationResult:
    decision = store.read_release_review_decision_record(
        product=product, record_id=decision_record_id
    )
    profile = store.read_product_profile_record(product)
    if decision.product != product or profile.driver_id != "generic-web":
        raise BackupContinuationRefused("backup_continuation_scope_invalid")
    steps = client_release_steps(decision.release_start)
    if not steps or steps[0].kind != "backup":
        raise BackupContinuationRefused("backup_continuation_scope_invalid")
    operation_id = client_release_step_operation_id(
        profile=profile, decision=decision, step=steps[0]
    )
    operation = store.read_verireel_prod_backup_gate_operation_record(operation_id)
    if _has_continuation_receipt(operation, review, actor):
        return BackupContinuationResult(
            trace_id=trace_id,
            decision_record_id=decision_record_id,
            operation_id=operation_id,
            review_digest=review.expected_digest,
            status="replayed",
        )
    require_precapture_failure(operation)
    assert operation.binding is not None
    if _covering_decision(store, product, decision_record_id=decision_record_id) is None:
        raise BackupContinuationRefused("release_acceptance_no_longer_current")
    views = read_client_release_step_views(store=store, profile=profile, decision=decision)
    if any(view.status != "not_started" for view in views[1:]):
        raise BackupContinuationRefused("later_release_step_already_started")
    try:
        _require_current_release(store, root, profile, decision)
        require_release_window(
            store=store, profile=profile, decision=decision, control_plane_root=root
        )
        if decision.release_start == "promote_with_rollback_drill":
            _generic_web_drill_target(store, profile, decision)
    except ClientReleaseNotReady as error:
        raise BackupContinuationRefused("release_evidence_not_current") from error
    authorization = operation.authorization
    if (
        authorization is None
        or authorization.grant != "client_release_acceptance"
        or not client_release_grant_allows(store, authorization)
    ):
        raise BackupContinuationRefused("backup_acceptance_provenance_invalid")
    binding = resolve_production_backup_binding(store, operation.binding.request)
    if binding != operation.binding:
        raise BackupContinuationRefused("backup_authority_changed")
    guest_capture_metadata(store, binding)
    backup = store.read_backup_gate_record(operation.backup_record_id)
    if backup.status != "fail" or backup.evidence != {
        **operation.progress_evidence,
        "error_message": operation.error_message,
    }:
        raise BackupContinuationRefused("backup_evidence_changed")
    digest = hashlib.sha256(
        json.dumps(
            {
                "operation": operation.model_dump(mode="json"),
                "backup": backup.model_dump(mode="json"),
                "decision": decision.model_dump(mode="json"),
                "profile": profile.model_dump(mode="json"),
                "reason": review.reason,
                "actor": actor,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    if review.apply:
        if review.expected_digest != digest:
            raise BackupContinuationRefused("backup_continuation_review_changed")
        now = utc_now_timestamp()
        resumed = operation.model_copy(
            update={
                "previous_attempt": operation,
                "previous_backup": backup,
                "continuation_digest": digest,
                "continuation_reason": review.reason,
                "continuation_actor": actor,
                "continued_at": now,
                "status": "pending",
                "phase": "created",
                "updated_at": now,
                "started_at": "",
                "finished_at": "",
                "lease_owner": "",
                "lease_expires_at": "",
                "heartbeat_at": "",
                "error_code": "",
                "error_message": "",
                "progress_evidence": {},
                "runner_trace_id": trace_id,
            }
        )
        if not store.continue_precapture_backup_operation(
            expected=operation, resumed=resumed, expected_backup=backup
        ):
            current = store.read_verireel_prod_backup_gate_operation_record(operation_id)
            if not _has_continuation_receipt(current, review, actor):
                raise BackupContinuationRefused("backup_continuation_review_changed")
            return BackupContinuationResult(
                trace_id=trace_id,
                decision_record_id=decision_record_id,
                operation_id=operation_id,
                review_digest=digest,
                status="replayed",
            )
    return BackupContinuationResult(
        trace_id=trace_id,
        decision_record_id=decision_record_id,
        operation_id=operation_id,
        review_digest=digest,
        status="queued" if review.apply else "ready",
    )
