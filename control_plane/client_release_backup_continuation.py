import hashlib
import json
from pathlib import Path
from typing import Literal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from control_plane.client_release import (
    CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
    ClientReleaseStep,
    ClientReleaseNotReady,
    _covering_decision,
    _generic_web_drill_target,
    _require_current_release,
    _step_key,
    client_release_promotion_request,
    client_release_step_operation_id,
    client_release_steps,
    client_release_grant_allows,
    native_release_recipient,
    read_client_release_step_views,
)
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.integration_allowances import canonical_sha256
from control_plane.generic_web_promotion_http import GENERIC_WEB_PROD_PROMOTION_ROUTE
from control_plane.provider_operations import build_provider_operation_key
from control_plane.contracts.verireel_prod_backup_gate_operation import (
    VeriReelProdBackupGateOperationRecord,
)
from control_plane.guest_capture_credentials import guest_capture_metadata
from control_plane.odoo_business_hours import BusinessHoursHeld, require_release_window
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.production_backup_gate import resolve_production_backup_binding
from control_plane.workflows.production_promotion_backup import (
    ProductionBackupNotAdmissible,
    promotion_is_temporary_admission_refusal,
    require_production_promotion_backup,
)
from control_plane.workflows.ship import utc_now_timestamp

BACKUP_CONTINUATION_ROUTE = (
    "/v1/products/{product}/releases/{decision_record_id}/backup-continuation"
)


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
        or (operation.error_code, operation.progress_evidence)
        not in (
            ("backup_preflight_unavailable", {"provider_stage": "backup_preflight"}),
            ("backup_source_busy", {}),
        )
        or operation.result is not None
        or operation.cancellation is not None
        or not operation.finished_at
    ):
        raise BackupContinuationRefused("backup_capture_outcome_not_proven_absent")


def require_continuable_backup(
    store: PostgresRecordStore,
    operation: VeriReelProdBackupGateOperationRecord,
    backup: BackupGateRecord,
) -> dict[str, Any]:
    if operation.status == "pass" and (
        operation.schema_version != 3
        or operation.binding is None
        or operation.result is None
        or operation.phase != "completed"
        or operation.cancellation is not None
        or not operation.finished_at
    ):
        raise BackupContinuationRefused("backup_capture_outcome_not_proven_absent")
    authorization = operation.authorization
    if authorization is None or not authorization.release_decision_record_id:
        raise BackupContinuationRefused("backup_acceptance_provenance_invalid")
    decision = store.read_release_review_decision_record(
        product=operation.product, record_id=authorization.release_decision_record_id
    )
    profile = store.read_product_profile_record(operation.product)
    if (
        profile.driver_id != "generic-web"
        or _covering_decision(store, operation.product, decision_record_id=decision.record_id)
        is None
        or not client_release_grant_allows(store, authorization)
    ):
        raise BackupContinuationRefused("backup_acceptance_provenance_invalid")
    if (
        operation.binding is None
        or resolve_production_backup_binding(store, operation.binding.request) != operation.binding
    ):
        raise BackupContinuationRefused("backup_authority_changed")
    steps = client_release_steps(decision.release_start)
    index = next(
        (
            index
            for index, step in enumerate(steps)
            if step.kind == "backup"
            and client_release_step_operation_id(profile=profile, decision=decision, step=step)
            == operation.operation_id
        ),
        None,
    )
    if index is None:
        raise BackupContinuationRefused("backup_continuation_scope_invalid")
    views = read_client_release_step_views(store=store, profile=profile, decision=decision)
    if any(view.status != "not_started" for view in views[index + 1 :]):
        raise BackupContinuationRefused("later_release_step_already_started")
    promotions = store.list_promotion_records(
        context_name=operation.context, to_instance_name=operation.instance
    )
    from control_plane.production_recipient_cutover import (
        _attempt_execution_started,
        cutover_reconciliation_key,
    )

    recipient = native_release_recipient(store, profile, decision_record_id=decision.record_id)
    native_context: dict[str, Any] = {}
    excluded_promotion_id = ""
    if recipient is not None:
        native_context["recipient"] = recipient.model_dump(mode="json")
    if recipient is not None and recipient.execution:
        step = ClientReleaseStep("promote", steps[index].attempt)
        step_key = _step_key(decision, step)
        operation_id = client_release_step_operation_id(
            profile=profile, decision=decision, step=step
        )
        request = client_release_promotion_request(profile, decision, operation.backup_record_id)
        key = build_provider_operation_key(
            scope=CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
            route_path=GENERIC_WEB_PROD_PROMOTION_ROUTE,
            idempotency_key=step_key,
            request_fingerprint=hashlib.sha256(request.model_dump_json().encode()).hexdigest(),
            reconciliation_key=cutover_reconciliation_key(recipient.plan_sha256),
        )
        saved = recipient.execution.get("reservation:" + operation_id)
        attempt_prefix = canonical_sha256(key) + ":"
        repromote = bool(recipient.publication.get("inverse_backup_policies"))
        if (
            not (
                recipient.phase == "preflight_refused"
                and saved is not None
                or step.attempt == 2
                and recipient.phase == "rolled_back"
                and saved is None
            )
            or recipient.execution.get("native_inverse")
            or repromote != (step.attempt == 2)
            or recipient.execution.get("release_trace") != f"client-release-{decision.record_id}"
            or _attempt_execution_started(recipient, key, repromote=repromote)
            or recipient.execution.get(attempt_prefix + "old_pause_fenced")
            or recipient.execution.get(attempt_prefix + "rollback_completed_at")
            or store.read_idempotency_record(
                scope=CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
                route_path=GENERIC_WEB_PROD_PROMOTION_ROUTE,
                idempotency_key=step_key,
            )
            is not None
        ):
            raise BackupContinuationRefused("recipient_release_execution_already_started")
        if saved is not None:
            reservation = LaunchplaneIdempotencyRecord.model_validate(saved)
            if (
                reservation.record_id != operation_id
                or reservation.state != "running"
                or reservation.scope != CLIENT_RELEASE_IDEMPOTENCY_SCOPE
                or reservation.route_path != GENERIC_WEB_PROD_PROMOTION_ROUTE
                or reservation.idempotency_key != step_key
                or reservation.request_fingerprint
                != hashlib.sha256(request.model_dump_json().encode()).hexdigest()
                or reservation.reconciliation_key
                != cutover_reconciliation_key(recipient.plan_sha256)
                or reservation.provider_effect_started_at
            ):
                raise BackupContinuationRefused("recipient_release_execution_already_started")
        excluded_promotion_id = "promotion-recipient-" + canonical_sha256(key)[:24]
        pending = next((row for row in promotions if row.record_id == excluded_promotion_id), None)
        native_context["promotion"] = pending.model_dump(mode="json") if pending else None
        if pending is not None and (
            recipient.phase != "preflight_refused"
            or pending.deployment_record_id
            or pending.deploy.status not in {"pending", "fail"}
            or pending.backup_record_id != operation.backup_record_id
            or pending.artifact_identity is None
            or pending.artifact_identity.artifact_id != recipient.image_reference
        ):
            raise BackupContinuationRefused("backup_capture_already_consumed")
    for promotion in promotions:
        if promotion.record_id == excluded_promotion_id:
            continue
        if not promotion_is_temporary_admission_refusal(
            promotion
        ) and operation.backup_record_id in (
            promotion.backup_gate.evidence.get("backup_record_id"),
            promotion.backup_gate.evidence.get("infrastructure_backup_record_id"),
        ):
            raise BackupContinuationRefused("backup_capture_already_consumed")
    if operation.status != "pass":
        require_precapture_failure(operation)
        if backup.status != "fail" or backup.evidence != {
            **operation.progress_evidence,
            "error_message": operation.error_message,
        }:
            raise BackupContinuationRefused("backup_evidence_changed")
        return native_context
    try:
        require_production_promotion_backup(
            record_store=store,
            product=operation.product,
            context=operation.context,
            instance=operation.instance,
            promotion_action=operation.binding.request.promotion_action,
            backup_record_id=operation.backup_record_id,
            active_promotion_record_id=excluded_promotion_id,
        )
    except ProductionBackupNotAdmissible as error:
        if error.reason in {"expired", "superseded"}:
            return native_context
        raise BackupContinuationRefused("backup_capture_not_renewable") from error
    raise BackupContinuationRefused("backup_capture_still_admissible")


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
    try:
        decision = store.read_release_review_decision_record(
            product=product, record_id=decision_record_id
        )
    except FileNotFoundError as error:
        raise BackupContinuationRefused("release_record_not_found") from error
    profile = store.read_product_profile_record(product)
    if decision.product != product or profile.driver_id != "generic-web":
        raise BackupContinuationRefused("backup_continuation_scope_invalid")
    steps = client_release_steps(decision.release_start)
    if not steps or steps[0].kind != "backup":
        raise BackupContinuationRefused("backup_continuation_scope_invalid")
    operations: dict[str, VeriReelProdBackupGateOperationRecord] = {}
    for step in steps:
        if step.kind != "backup":
            continue
        operation_id = client_release_step_operation_id(
            profile=profile, decision=decision, step=step
        )
        try:
            prior = store.read_verireel_prod_backup_gate_operation_record(operation_id)
        except FileNotFoundError:
            continue
        operations[operation_id] = prior
        if _has_continuation_receipt(prior, review, actor):
            return BackupContinuationResult(
                trace_id=trace_id,
                decision_record_id=decision_record_id,
                operation_id=operation_id,
                review_digest=review.expected_digest,
                status="replayed",
            )
    views = read_client_release_step_views(store=store, profile=profile, decision=decision)
    index = next((index for index, view in enumerate(views) if view.status != "pass"), len(views))
    if index == len(views) or steps[index].kind == "rollback":
        raise BackupContinuationRefused("later_release_step_already_started")
    if steps[index].kind == "promote":
        index -= 1
    operation_id = client_release_step_operation_id(
        profile=profile, decision=decision, step=steps[index]
    )
    try:
        operation = operations[operation_id]
    except KeyError as error:
        raise BackupContinuationRefused("backup_operation_unavailable") from error
    if operation.binding is None:
        raise BackupContinuationRefused("backup_capture_outcome_not_proven_absent")
    if _covering_decision(store, product, decision_record_id=decision_record_id) is None:
        raise BackupContinuationRefused("release_acceptance_no_longer_current")
    if any(view.status != "not_started" for view in views[index + 1 :]):
        raise BackupContinuationRefused("later_release_step_already_started")
    try:
        _require_current_release(store, root, profile, decision)
        require_release_window(
            store=store, profile=profile, decision=decision, control_plane_root=root
        )
        if decision.release_start == "promote_with_rollback_drill":
            _generic_web_drill_target(store, profile, decision)
    except BusinessHoursHeld as error:
        raise BackupContinuationRefused("release_window_held") from error
    except ClientReleaseNotReady as error:
        raise BackupContinuationRefused("release_evidence_not_current") from error
    authorization = operation.authorization
    if (
        authorization is None
        or authorization.grant != "client_release_acceptance"
        or not client_release_grant_allows(store, authorization)
    ):
        raise BackupContinuationRefused("backup_acceptance_provenance_invalid")
    try:
        binding = resolve_production_backup_binding(store, operation.binding.request)
    except (FileNotFoundError, ValueError) as error:
        raise BackupContinuationRefused("backup_authority_unavailable") from error
    if binding != operation.binding:
        raise BackupContinuationRefused("backup_authority_changed")
    try:
        guest_capture_metadata(store, binding)
    except (FileNotFoundError, ValueError) as error:
        raise BackupContinuationRefused("backup_capture_credential_unavailable") from error
    try:
        backup = store.read_backup_gate_record(operation.backup_record_id)
    except FileNotFoundError as error:
        raise BackupContinuationRefused("backup_evidence_unavailable") from error
    native_context = require_continuable_backup(store, operation, backup)
    digest = hashlib.sha256(
        json.dumps(
            {
                "operation": operation.model_dump(mode="json"),
                "backup": backup.model_dump(mode="json"),
                "decision": decision.model_dump(mode="json"),
                "profile": profile.model_dump(mode="json"),
                "reason": review.reason,
                "actor": actor,
                "native_context": native_context,
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
                "result": None,
                "attempt": 0,
                "runner_trace_id": trace_id,
            }
        )
        if not store.continue_precapture_backup_operation(
            expected=operation,
            resumed=resumed,
            expected_backup=backup,
            expected_native_context=native_context,
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
