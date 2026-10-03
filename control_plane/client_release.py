"""A Client's accepted release, promoted by Launchplane itself.

When a product's recorded Client accepts a release and an admin has not held the
product's releases, the decision is stamped with how the release runs. The Odoo
stable worker then queues the operations the admin's Release panel queues: a
verified backup, then the promotion. With the one rollback drill, it then rolls
back to the production version the Client's checklist was compiled against, takes
another backup, and promotes the same artifact again. Rolling back restores the
checklist's production version, so the checklist recompiles to the digest the
Client accepted, and that acceptance covers the second promotion and nothing else.

Every step runs under a Client release grant naming the decision. Before a step is
queued, and before each provider effect, the decision must still be the product's
newest one, accepted by its recorded Client, with releases not held. The promotion
still checks release approval for the exact candidate and the verified backup.
Each step's operation id derives from the decision, so the run needs no record of
its own and a second worker replica finds the same operations.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

import click
from pydantic import BaseModel, ConfigDict

from control_plane.contracts.deployment_record import deployment_record_passed
from control_plane.contracts.durable_operation_authorization import (
    DurableOperationAuthorization,
    DurableOperationCallerIdentity,
)
from control_plane.contracts.odoo_prod_promotion_operation import (
    ODOO_PROD_PROMOTION_RUN_ACTION,
    OdooProdPromotionOperationRecord,
    OdooProdPromotionRunRequest,
    build_odoo_prod_promotion_operation_id,
    odoo_prod_promotion_request_fingerprint,
)
from control_plane.contracts.odoo_prod_rollback_operation import (
    ODOO_PROD_ROLLBACK_ACTION,
    OdooProdRollbackOperationRecord,
    OdooProdRollbackRequest,
    OdooProdRollbackTarget,
    build_odoo_prod_rollback_operation_id,
    odoo_prod_rollback_request_fingerprint,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.production_backup_gate import (
    PRODUCTION_BACKUP_GATE_EXECUTE_ACTION,
    ProductionBackupGateRequest,
)
from control_plane.contracts.release_review import ReleaseReviewDecisionRecord, ReleaseStart
from control_plane.odoo_stable_lane import OdooStableLaneOperationConflictError
from control_plane.release_review import current_release_review
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.odoo_prod_promotion_run import (
    OdooProdPromotionRunStore,
    admit_odoo_prod_promotion_run,
)
from control_plane.workflows.odoo_prod_rollback import resolve_odoo_prod_rollback_target
from control_plane.workflows.production_backup_gate import (
    enqueue_production_backup_gate,
    production_backup_gate_operation_id,
)
from control_plane.workflows.production_promotion_backup import ODOO_PROMOTION_BACKUP_ACTION
from control_plane.workflows.ship import utc_now_timestamp

_LOGGER = logging.getLogger(__name__)

CLIENT_RELEASE_IDEMPOTENCY_SCOPE = "client-release"
ClientReleaseStepKind = Literal["backup", "promote", "rollback"]
ClientReleaseStepStatus = Literal[
    "not_started", "pending", "running", "reconciliation_required", "pass", "fail", "cancelled"
]
ClientReleaseRunState = Literal["waiting", "running", "passed", "stopped"]
_STOPPED_STATUSES = frozenset({"fail", "cancelled", "reconciliation_required"})


@dataclass(frozen=True, slots=True)
class ClientReleaseStep:
    kind: ClientReleaseStepKind
    attempt: int

    @property
    def name(self) -> str:
        return f"{self.kind}-{self.attempt}"


_PROMOTE_STEPS = (ClientReleaseStep("backup", 1), ClientReleaseStep("promote", 1))
_DRILL_STEPS = (
    ClientReleaseStep("rollback", 1),
    ClientReleaseStep("backup", 2),
    ClientReleaseStep("promote", 2),
)


class ClientReleaseStepView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step: str
    kind: ClientReleaseStepKind
    status: ClientReleaseStepStatus
    operation_id: str


class ClientReleaseRunView(BaseModel):
    """What a Client's accepted release has done so far, derived from its operations."""

    model_config = ConfigDict(extra="forbid")

    decision_record_id: str
    rollback_drill: bool
    state: ClientReleaseRunState
    steps: tuple[ClientReleaseStepView, ...]


def client_release_steps(release_start: ReleaseStart) -> tuple[ClientReleaseStep, ...]:
    if release_start == "promote_with_rollback_drill":
        return _PROMOTE_STEPS + _DRILL_STEPS
    if release_start == "promote":
        return _PROMOTE_STEPS
    return ()


def _prod_context(profile: LaunchplaneProductProfileRecord) -> str:
    lane = next((lane for lane in profile.lanes if lane.instance == "prod"), None)
    return lane.context if lane is not None else ""


def _step_key(decision: ReleaseReviewDecisionRecord, step: ClientReleaseStep) -> str:
    return f"{decision.record_id}:{step.name}"


def _backup_record_id(context: str, decision: ReleaseReviewDecisionRecord, attempt: int) -> str:
    # Backup record ids are global; the decision digest keeps them short and unique.
    digest = hashlib.sha256(decision.record_id.encode()).hexdigest()[:24]
    return f"infrastructure-{context}-client-release-{digest}-{attempt}"


def client_release_step_operation_id(
    *,
    profile: LaunchplaneProductProfileRecord,
    decision: ReleaseReviewDecisionRecord,
    step: ClientReleaseStep,
) -> str:
    context = _prod_context(profile)
    key = _step_key(decision, step)
    if step.kind == "backup":
        return production_backup_gate_operation_id(f"{CLIENT_RELEASE_IDEMPOTENCY_SCOPE}|{key}")
    builder = (
        build_odoo_prod_promotion_operation_id
        if step.kind == "promote"
        else build_odoo_prod_rollback_operation_id
    )
    return builder(
        product=profile.product,
        context=context,
        idempotency_key=key,
        idempotency_scope=CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
    )


def release_start_for_acceptance(
    *, store: object, profile: LaunchplaneProductProfileRecord
) -> ReleaseStart:
    """How a Client's acceptance recorded now runs; "" when it starts nothing.

    Only an Odoo product with a prod lane, not prelaunch and not held, starts a
    release. A product drills once: after a release's drill passed, later ones
    only promote.
    """

    mode = profile.release_on_acceptance
    if (
        mode == "held"
        or profile.driver_id != "odoo"
        or profile.production_use == "prelaunch"
        or not _prod_context(profile)
    ):
        return ""
    if mode == "promote_with_rollback_drill" and _product_drill_passed(store, profile):
        return "promote"
    return mode


def _product_drill_passed(store: object, profile: LaunchplaneProductProfileRecord) -> bool:
    if not isinstance(store, PostgresRecordStore):
        return False
    record_store = store
    final_step = _DRILL_STEPS[-1]
    for decision in record_store.list_release_review_decision_records(product=profile.product):
        if decision.release_start != "promote_with_rollback_drill":
            continue
        if _step_status(record_store, profile, decision, final_step)[0] == "pass":
            return True
    return False


def client_release_grant(
    *,
    decision: ReleaseReviewDecisionRecord,
    action: str,
    context: str,
    authorized_at: str,
) -> DurableOperationAuthorization:
    return DurableOperationAuthorization(
        action=action,
        product=decision.product,
        context=context,
        instances=("prod",),
        authorized_at=authorized_at,
        caller=DurableOperationCallerIdentity(
            identity_type="github_human",
            login=decision.actor_github_login,
            github_id=int(decision.actor_github_id),
            role="client",
        ),
        grant="client_release_acceptance",
        release_decision_record_id=decision.record_id,
    )


def _covering_decision(
    store: object, product: str, *, decision_record_id: str = ""
) -> tuple[LaunchplaneProductProfileRecord, ReleaseReviewDecisionRecord] | None:
    """The product and its newest decision, when that decision still starts a release."""

    record_store = cast(PostgresRecordStore, store)
    try:
        profile = record_store.read_product_profile_record(product)
    except FileNotFoundError:
        return None
    if (
        not profile.is_active
        or profile.driver_id != "odoo"
        or profile.production_use == "prelaunch"
        or profile.release_on_acceptance == "held"
        or not profile.owner.github_id
    ):
        return None
    latest = record_store.list_release_review_decision_records(product=product, limit=1)
    if not latest:
        return None
    decision = latest[0]
    if (
        (decision_record_id and decision.record_id != decision_record_id)
        or decision.decision != "accepted"
        or not decision.release_start
        or not decision.release_issue_url
        or decision.actor_github_id != profile.owner.github_id
        or decision.checklist.owner_github_id != profile.owner.github_id
    ):
        return None
    return profile, decision


def client_release_grant_allows(
    store: object, authorization: DurableOperationAuthorization
) -> bool:
    """The worker's re-check of a Client release grant before each provider effect."""

    if authorization.grant != "client_release_acceptance" or authorization.instances != ("prod",):
        return False
    covered = _covering_decision(
        store,
        authorization.product,
        decision_record_id=authorization.release_decision_record_id,
    )
    if covered is None:
        return False
    profile, decision = covered
    context = _prod_context(profile)
    checklist = decision.checklist
    try:
        record_store = cast(PostgresRecordStore, store)
        testing = record_store.read_release_tuple_record(
            context_name=context, channel_name="testing"
        )
        production = record_store.read_release_tuple_record(
            context_name=context, channel_name="prod"
        )
    except FileNotFoundError:
        return False
    # Every step happens while testing carries the accepted candidate, and production
    # runs either the checklist's production version or that candidate.
    return (
        authorization.context == context
        and str(authorization.caller.github_id) == decision.actor_github_id
        and authorization.caller.role == "client"
        and testing.artifact_id == checklist.candidate.artifact_id
        and production.artifact_id
        in {checklist.production.artifact_id, checklist.candidate.artifact_id}
    )


def _step_status(
    store: PostgresRecordStore,
    profile: LaunchplaneProductProfileRecord,
    decision: ReleaseReviewDecisionRecord,
    step: ClientReleaseStep,
) -> tuple[ClientReleaseStepStatus, object | None]:
    operation_id = client_release_step_operation_id(profile=profile, decision=decision, step=step)
    try:
        if step.kind == "backup":
            operation: object = store.read_verireel_prod_backup_gate_operation_record(operation_id)
        elif step.kind == "promote":
            operation = store.read_odoo_prod_promotion_operation_record(operation_id)
        else:
            operation = store.read_odoo_prod_rollback_operation_record(operation_id)
    except FileNotFoundError:
        return "not_started", None
    return cast(ClientReleaseStepStatus, getattr(operation, "status")), operation


def read_client_release_run(
    *,
    store: object,
    profile: LaunchplaneProductProfileRecord,
    decision: ReleaseReviewDecisionRecord,
) -> ClientReleaseRunView | None:
    steps = client_release_steps(decision.release_start)
    if not steps or not isinstance(store, PostgresRecordStore):
        return None
    views = []
    for step in steps:
        status, _operation = _step_status(store, profile, decision, step)
        views.append(
            ClientReleaseStepView(
                step=step.name,
                kind=step.kind,
                status=status,
                operation_id=client_release_step_operation_id(
                    profile=profile, decision=decision, step=step
                ),
            )
        )
    statuses = [view.status for view in views]
    state: ClientReleaseRunState
    if any(status in _STOPPED_STATUSES for status in statuses):
        state = "stopped"
    elif all(status == "pass" for status in statuses):
        state = "passed"
    elif any(status in {"pending", "running"} for status in statuses):
        state = "running"
    else:
        state = "waiting"
    return ClientReleaseRunView(
        decision_record_id=decision.record_id,
        rollback_drill=decision.release_start == "promote_with_rollback_drill",
        state=state,
        steps=tuple(views),
    )


class ClientReleaseNotReady(Exception):
    """The next step cannot be queued now; nothing was queued."""


def advance_client_releases(*, store: object, control_plane_root: Path) -> tuple[str, ...]:
    """Queue the next step of every Client release that is ready for one.

    Returns the operation ids it queued. A step whose evidence no longer matches the
    Client's decision is never queued, so a stale acceptance starts nothing.
    """

    if not isinstance(store, PostgresRecordStore):
        return ()
    queued: list[str] = []
    for profile in store.list_product_profile_records(driver_id="odoo"):
        covered = _covering_decision(store, profile.product)
        if covered is None:
            continue
        try:
            operation_id = _advance(store, control_plane_root, *covered)
        except ClientReleaseNotReady as reason:
            _LOGGER.info(
                "client release not advanced product=%s reason=%s", profile.product, reason
            )
            continue
        except OdooStableLaneOperationConflictError:
            continue
        if operation_id:
            queued.append(operation_id)
    return tuple(queued)


def _advance(
    store: PostgresRecordStore,
    control_plane_root: Path,
    profile: LaunchplaneProductProfileRecord,
    decision: ReleaseReviewDecisionRecord,
) -> str:
    context = _prod_context(profile)
    previous: object | None = None
    for step in client_release_steps(decision.release_start):
        status, operation = _step_status(store, profile, decision, step)
        if status == "pass":
            previous = operation
            continue
        if status != "not_started":
            # Running, or stopped: a stopped release never continues by itself.
            return ""
        authorized_at = utc_now_timestamp()
        if step.kind == "rollback":
            return _queue_rollback(store, profile, decision, step, context, authorized_at)
        _require_current_release(store, control_plane_root, profile, decision)
        if step.kind == "backup":
            return _queue_backup(store, decision, step, context, authorized_at)
        backup_record_id = str(getattr(previous, "backup_record_id", ""))
        return _queue_promotion(
            store,
            control_plane_root,
            profile,
            decision,
            step,
            context,
            backup_record_id,
            authorized_at,
        )
    return ""


def _require_current_release(
    store: PostgresRecordStore,
    control_plane_root: Path,
    profile: LaunchplaneProductProfileRecord,
    decision: ReleaseReviewDecisionRecord,
) -> None:
    """The release on the lanes now is exactly the one the Client accepted."""

    checklist = decision.checklist
    context = _prod_context(profile)
    # Cheap reads first, so a stale acceptance does not read GitHub on every poll.
    try:
        production = store.read_release_tuple_record(context_name=context, channel_name="prod")
        candidate = store.read_release_tuple_record(context_name=context, channel_name="testing")
    except FileNotFoundError as error:
        raise ClientReleaseNotReady("release_record_missing") from error
    if (production.artifact_id, candidate.artifact_id) != (
        checklist.production.artifact_id,
        checklist.candidate.artifact_id,
    ):
        raise ClientReleaseNotReady("release_changed")
    review = current_release_review(
        control_plane_root=control_plane_root, record_store=store, profile=profile
    )
    if (
        not review.approved
        or review.checklist_digest != decision.checklist_digest
        or review.latest_decision is None
        or review.latest_decision.record_id != decision.record_id
    ):
        raise ClientReleaseNotReady("release_not_accepted")


def _queue_backup(
    store: PostgresRecordStore,
    decision: ReleaseReviewDecisionRecord,
    step: ClientReleaseStep,
    context: str,
    authorized_at: str,
) -> str:
    try:
        operation = enqueue_production_backup_gate(
            record_store=store,
            request=ProductionBackupGateRequest(
                product=decision.product,
                context=context,
                instance="prod",
                promotion_action=ODOO_PROMOTION_BACKUP_ACTION,
                backup_record_id=_backup_record_id(context, decision, step.attempt),
            ),
            authorization=client_release_grant(
                decision=decision,
                action=PRODUCTION_BACKUP_GATE_EXECUTE_ACTION,
                context=context,
                authorized_at=authorized_at,
            ),
            operation_key=f"{CLIENT_RELEASE_IDEMPOTENCY_SCOPE}|{_step_key(decision, step)}",
        )
    except (FileNotFoundError, ValueError) as error:
        raise ClientReleaseNotReady("backup_not_ready") from error
    return operation.operation_id


def _queue_promotion(
    store: PostgresRecordStore,
    control_plane_root: Path,
    profile: LaunchplaneProductProfileRecord,
    decision: ReleaseReviewDecisionRecord,
    step: ClientReleaseStep,
    context: str,
    backup_record_id: str,
    authorized_at: str,
) -> str:
    key = _step_key(decision, step)
    request = OdooProdPromotionRunRequest(
        context=context,
        product=profile.product,
        request_id=f"client-release-{step.name}-{decision.record_id}",
        infrastructure_backup_record_id=backup_record_id,
        expected_artifact_id=decision.checklist.candidate.artifact_id,
    )
    admission = admit_odoo_prod_promotion_run(
        control_plane_root=control_plane_root,
        record_store=cast(OdooProdPromotionRunStore, store),
        request=request,
    )
    if admission.blocked_reason:
        raise ClientReleaseNotReady("promotion_not_ready")
    if admission.inputs_result.artifact_id != decision.checklist.candidate.artifact_id:
        raise ClientReleaseNotReady("release_changed")
    operation = OdooProdPromotionOperationRecord(
        operation_id=client_release_step_operation_id(
            profile=profile, decision=decision, step=step
        ),
        product=profile.product,
        context=context,
        instance="prod",
        idempotency_key=key,
        idempotency_scope=CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
        request_fingerprint=odoo_prod_promotion_request_fingerprint(request),
        request=request,
        authorization=client_release_grant(
            decision=decision,
            action=ODOO_PROD_PROMOTION_RUN_ACTION,
            context=context,
            authorized_at=authorized_at,
        ),
        created_at=authorized_at,
        updated_at=authorized_at,
    )
    persisted, _created = store.create_odoo_prod_promotion_operation_record_if_no_active_lane(
        operation
    )
    return persisted.operation_id if persisted.operation_id == operation.operation_id else ""


def _queue_rollback(
    store: PostgresRecordStore,
    profile: LaunchplaneProductProfileRecord,
    decision: ReleaseReviewDecisionRecord,
    step: ClientReleaseStep,
    context: str,
    authorized_at: str,
) -> str:
    """Roll back to the production version the Client's checklist was compiled against."""

    checklist = decision.checklist
    try:
        production = store.read_release_tuple_record(context_name=context, channel_name="prod")
        testing = store.read_release_tuple_record(context_name=context, channel_name="testing")
    except FileNotFoundError as error:
        raise ClientReleaseNotReady("release_record_missing") from error
    if (production.artifact_id, testing.artifact_id) != (
        checklist.candidate.artifact_id,
        checklist.candidate.artifact_id,
    ):
        raise ClientReleaseNotReady("release_changed")
    target_artifact_id = checklist.production.artifact_id
    deployment = next(
        (
            record
            for record in store.list_deployment_records(context_name=context, instance_name="prod")
            if record.artifact_identity is not None
            and record.artifact_identity.artifact_id == target_artifact_id
            and deployment_record_passed(record)
        ),
        None,
    )
    if deployment is None:
        raise ClientReleaseNotReady("rollback_target_missing")
    request = OdooProdRollbackRequest(
        context=context,
        artifact_id=target_artifact_id,
        reason=f"Rollback drill for the Client's accepted release {decision.record_id}.",
    )
    try:
        resolve_odoo_prod_rollback_target(record_store=store, request=request)
    except click.ClickException as error:
        raise ClientReleaseNotReady("rollback_not_ready") from error
    operation = OdooProdRollbackOperationRecord(
        operation_id=client_release_step_operation_id(
            profile=profile, decision=decision, step=step
        ),
        product=profile.product,
        context=context,
        instance="prod",
        idempotency_key=_step_key(decision, step),
        idempotency_scope=CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
        request_fingerprint=odoo_prod_rollback_request_fingerprint(
            product=profile.product, request=request
        ),
        request=request,
        target=OdooProdRollbackTarget(
            artifact_id=target_artifact_id, deployment_record_id=deployment.record_id
        ),
        authorization=client_release_grant(
            decision=decision,
            action=ODOO_PROD_ROLLBACK_ACTION,
            context=context,
            authorized_at=authorized_at,
        ),
        created_at=authorized_at,
        updated_at=authorized_at,
    )
    persisted, _created = store.create_odoo_prod_rollback_operation_record_if_no_active_lane(
        operation
    )
    return persisted.operation_id if persisted.operation_id == operation.operation_id else ""
