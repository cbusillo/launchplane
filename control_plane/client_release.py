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
from datetime import UTC, datetime
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from time import monotonic
from typing import Literal, cast

import click
from pydantic import BaseModel, ConfigDict

from control_plane.child_process_errors import redact_untrusted_text
from control_plane.operation_status_read import safe_operation_error_code

from control_plane.contracts.deployment_record import deployment_record_passed
from control_plane.contracts.idempotency_record import build_launchplane_mutation_reservation_id
from control_plane.contracts.durable_operation_authorization import (
    DurableOperationAuthorization,
    DurableOperationCallerIdentity,
)
from control_plane.contracts.odoo_prod_promotion_operation import (
    ODOO_PROD_PROMOTION_RUN_ACTION,
    OdooProdPromotionCheckpoint,
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
from control_plane.contracts.verireel_prod_backup_gate_operation import (
    VeriReelProdBackupGateOperationRecord,
)
from control_plane.odoo_release_recovery import (
    pin_odoo_release_recovery_target,
    read_odoo_release_recovery,
)
from control_plane.odoo_stable_lane import OdooStableLaneOperationConflictError
from control_plane.release_review import (
    ReleaseReviewStore,
    current_release_review,
    release_version,
    checklist_blockers,
)
from control_plane.release_review_record import publish_release_decision
from control_plane.release_invitation import ReleaseInvitationBackoff, publish_release_invitation
from control_plane.generic_web_promotion_http import (
    GENERIC_WEB_PROD_PROMOTION_ROUTE,
    GenericWebProdPromotionEnvelope,
)
from control_plane.generic_web_promotion_provider_adapter import (
    GenericWebProdPromotionProviderMutationAdapter,
    require_generic_web_promotion_target,
)
from control_plane.contracts.idempotency_record import parse_launchplane_mutation_timestamp
from control_plane.provider_operations import run_durable_provider_operation
from control_plane.workflows.generic_web_promotion import GenericWebProdPromotionRequest
from control_plane.workflows.generic_web_deploy_provider import GenericWebResolvedDeployTarget
from control_plane.workflows.production_promotion_backup import GENERIC_WEB_PROMOTION_BACKUP_ACTION
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
ClientReleaseStepKind = Literal["backup", "promote", "rollback", "recovery"]
ClientReleaseStepStatus = Literal[
    "not_started", "pending", "running", "reconciliation_required", "pass", "fail", "cancelled"
]
ClientReleaseRunState = Literal["waiting", "running", "passed", "stopped"]
_STOPPED_STATUSES = frozenset({"fail", "cancelled", "reconciliation_required"})


@dataclass(slots=True)
class StandingReleaseReviewBackoff:
    """Delay incomplete standing reviews; never cache acceptance authority."""

    blocked: dict[str, tuple[str, float]] = field(default_factory=dict)


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


class ClientReleaseFailureView(BaseModel):
    """Bounded failure evidence from the operation that stopped the release."""

    model_config = ConfigDict(extra="forbid")

    code: str
    reason: str
    record_id: str
    trace_id: str = ""
    recorded_at: str


class ClientReleaseStepView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step: str
    kind: ClientReleaseStepKind
    status: ClientReleaseStepStatus
    operation_id: str
    failure: ClientReleaseFailureView | None = None


class ClientReleaseRunView(BaseModel):
    """What a Client's accepted release has done so far, derived from its operations."""

    model_config = ConfigDict(extra="forbid")

    decision_record_id: str
    rollback_drill: bool
    state: ClientReleaseRunState
    blocked_reason: str = ""
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
    if profile.driver_id == "generic-web" and step.kind == "promote":
        return build_launchplane_mutation_reservation_id(
            scope=CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
            route_path=GENERIC_WEB_PROD_PROMOTION_ROUTE,
            idempotency_key=key,
        )
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
        or profile.driver_id not in {"odoo", "generic-web"}
        or profile.production_use == "prelaunch"
        or not _prod_context(profile)
    ):
        return ""
    if profile.driver_id == "generic-web":
        return "promote" if mode in {"promote", "director_standing"} else ""
    if mode == "director_standing":
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
        or profile.driver_id not in {"odoo", "generic-web"}
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
        or (
            decision.acceptance_source == "director_standing"
            and profile.release_on_acceptance != "director_standing"
        )
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
        testing = release_version(store=record_store, profile=profile, instance="testing")
        production = release_version(store=record_store, profile=profile, instance="prod")
    except (FileNotFoundError, ValueError):
        return False
    # Every step happens while testing carries the accepted candidate, and production
    # runs either the checklist's production version or that candidate.
    return (
        authorization.context == context
        and str(authorization.caller.github_id) == decision.actor_github_id
        and authorization.caller.role == "client"
        and testing == checklist.candidate
        and production in (checklist.production, checklist.candidate)
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
            if profile.driver_id == "generic-web":
                reservation = store.read_idempotency_record(
                    scope=CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
                    route_path=GENERIC_WEB_PROD_PROMOTION_ROUTE,
                    idempotency_key=_step_key(decision, step),
                )
                if reservation is None:
                    return "not_started", None
                if reservation.state == "reconcile_required":
                    return "reconciliation_required", reservation
                if reservation.state == "running":
                    if parse_launchplane_mutation_timestamp(
                        reservation.lease_expires_at, field_name="lease_expires_at"
                    ) <= datetime.now(UTC):
                        return "reconciliation_required", reservation
                    return "running", reservation
                outcome = reservation.response_payload.get("result", {})
                return (
                    "pass" if outcome.get("promotion_status") == "pass" else "fail"
                ), reservation
            operation = store.read_odoo_prod_promotion_operation_record(operation_id)
        else:
            operation = store.read_odoo_prod_rollback_operation_record(operation_id)
    except FileNotFoundError:
        return "not_started", None
    return cast(ClientReleaseStepStatus, getattr(operation, "status")), operation


def _release_step_failure(
    store: PostgresRecordStore, operation: object | None, status: ClientReleaseStepStatus
) -> ClientReleaseFailureView | None:
    if operation is None or status not in _STOPPED_STATUSES:
        return None
    result = getattr(operation, "result", None)
    # A release's source promotion and verified backup may have passed. They
    # are inputs, not fallback failure records when its redeploy never started.
    record_id = str(getattr(operation, "operation_id", getattr(operation, "record_id", "")))
    result_record_id = getattr(
        result,
        "backup_record_id"
        if isinstance(operation, VeriReelProdBackupGateOperationRecord)
        else "deployment_record_id",
        "",
    )
    if result_record_id:
        record_id = str(result_record_id)
    message = str(getattr(operation, "error_message", "") or getattr(result, "error_message", ""))
    code = str(getattr(operation, "error_code", ""))
    trace_id = str(
        getattr(operation, "runner_trace_id", "") or getattr(operation, "response_trace_id", "")
    )
    payload = getattr(operation, "response_payload", {})
    if isinstance(payload, dict) and payload:
        error = payload.get("error", {})
        if isinstance(error, dict):
            message = message or str(error.get("message", ""))
            code = code or str(error.get("code", ""))
        outcome = payload.get("result", {})
        if isinstance(outcome, dict):
            record_id = str(outcome.get("deployment_record_id") or record_id)
            message = message or str(outcome.get("error_message", ""))
            failure = outcome.get("failure", {})
            if isinstance(failure, dict):
                code = code or str(failure.get("code", ""))
                message = message or str(failure.get("description", ""))
            promotion_id = outcome.get("promotion_record_id")
            if isinstance(promotion_id, str) and promotion_id:
                try:
                    promotion = store.read_promotion_record(promotion_id)
                except FileNotFoundError:
                    pass
                else:
                    if promotion.failure is not None:
                        code = code or promotion.failure.code
                        message = message or promotion.failure.description
                        if not outcome.get("deployment_record_id"):
                            record_id = promotion.record_id
        trace_id = str(payload.get("original_trace_id") or payload.get("trace_id") or trace_id)
    if status == "cancelled":
        message = message or "The release step was cancelled."
    elif status == "reconciliation_required":
        message = (
            message
            or "The provider outcome needs admin reconciliation before this release can continue."
        )
    return ClientReleaseFailureView(
        code=safe_operation_error_code(code) or status,
        reason=redact_untrusted_text(
            message,
            fallback="No failure reason was recorded for this release step.",
            maximum_length=220,
        ),
        record_id=record_id,
        trace_id=trace_id,
        recorded_at=str(
            getattr(operation, "finished_at", "") or getattr(operation, "updated_at", "")
        ),
    )


def read_client_release_step_views(
    *,
    store: PostgresRecordStore,
    profile: LaunchplaneProductProfileRecord,
    decision: ReleaseReviewDecisionRecord,
) -> tuple[ClientReleaseStepView, ...]:
    """Read recorded steps without checking whether an unstarted run can start."""

    steps = client_release_steps(decision.release_start)
    views = []
    for step in steps:
        status, operation = _step_status(store, profile, decision, step)
        views.append(
            ClientReleaseStepView(
                step=step.name,
                kind=step.kind,
                status=status,
                operation_id=client_release_step_operation_id(
                    profile=profile, decision=decision, step=step
                ),
                failure=_release_step_failure(store, operation, status),
            )
        )
        if isinstance(operation, OdooProdPromotionOperationRecord):
            recovery = read_odoo_release_recovery(store, operation)
            if recovery is not None:
                views.append(
                    ClientReleaseStepView(
                        step=f"failure-recovery-{step.attempt}",
                        kind="recovery",
                        status=recovery.status,
                        operation_id=recovery.operation_id,
                        failure=_release_step_failure(store, recovery, recovery.status),
                    )
                )
    return tuple(views)


def read_client_release_run(
    *,
    store: object,
    profile: LaunchplaneProductProfileRecord,
    decision: ReleaseReviewDecisionRecord,
) -> ClientReleaseRunView | None:
    if not client_release_steps(decision.release_start) or not isinstance(
        store, PostgresRecordStore
    ):
        return None
    views = read_client_release_step_views(store=store, profile=profile, decision=decision)
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
    blocked_reason = ""
    if profile.driver_id == "odoo" and state == "waiting":
        try:
            pin_odoo_release_recovery_target(store, decision, _prod_context(profile))
        except ValueError:
            blocked_reason = "No passing production deployment is available for recovery. An admin must reconcile the production record before this release can start."
    return ClientReleaseRunView(
        blocked_reason=blocked_reason,
        decision_record_id=decision.record_id,
        rollback_drill=decision.release_start == "promote_with_rollback_drill",
        state=state,
        steps=tuple(views),
    )


class ClientReleaseNotReady(Exception):
    """The next step cannot be queued now; nothing was queued."""


def _record_standing_acceptance(
    store: PostgresRecordStore,
    control_plane_root: Path,
    profile: LaunchplaneProductProfileRecord,
    backoff: StandingReleaseReviewBackoff | None = None,
) -> None:
    """Materialize the Director's recorded standing acceptance for one exact checklist.

    The profile switch is the explicit admin-recorded assertion that this Client
    is the Director. Admin permission alone never supplies that assertion.
    """

    if (
        profile.driver_id != "generic-web"
        or profile.release_on_acceptance != "director_standing"
        or not profile.is_active
        or not profile.owner.is_set
        or profile.production_use == "prelaunch"
    ):
        return
    production = release_version(store=store, profile=profile, instance="prod")
    candidate = release_version(store=store, profile=profile, instance="testing")
    if production == candidate:
        return
    latest = store.list_release_review_decision_records(product=profile.product, limit=1)
    if latest:
        previous = latest[0]
        if (
            previous.checklist.production == production
            and previous.checklist.candidate == candidate
            and previous.checklist.owner_github_id == profile.owner.github_id
            and previous.checklist.repository == profile.repository
            and (
                previous.decision != "accepted"
                or (
                    previous.release_issue_url
                    and previous.release_start
                    and (
                        run := read_client_release_run(
                            store=store, profile=profile, decision=previous
                        )
                    )
                    is not None
                    and run.state != "waiting"
                )
            )
        ):
            return
    fingerprint = hashlib.sha256(
        (
            profile.model_dump_json() + production.model_dump_json() + candidate.model_dump_json()
        ).encode()
    ).hexdigest()
    if backoff is not None:
        blocked = backoff.blocked.get(profile.product)
        if blocked is not None and blocked[0] == fingerprint and monotonic() < blocked[1]:
            return
        backoff.blocked[profile.product] = (fingerprint, monotonic() + 300)
    # Stamp before reads: a human decision made while GitHub is being read remains
    # newer than this standing decision even if its DB write finishes first.
    decided_at = datetime.now(UTC).isoformat()
    review = current_release_review(
        control_plane_root=control_plane_root, record_store=store, profile=profile
    )
    checklist = review.checklist
    if (
        checklist is None
        or checklist.production == checklist.candidate
        or checklist_blockers(checklist)
    ):
        return
    existing = review.latest_decision
    if existing is not None and existing.decision != "accepted":
        return
    record_key = hashlib.sha256(
        (review.checklist_digest + (existing.record_id if existing is not None else "")).encode()
    ).hexdigest()
    expected_record_id = f"release-review-standing-{record_key}"
    if existing is not None and existing.release_issue_url and existing.release_start:
        return
    if existing is not None and not existing.release_issue_url:
        decision = existing
    else:
        decision = ReleaseReviewDecisionRecord(
            record_id=expected_record_id,
            product=profile.product,
            checklist=checklist,
            checklist_digest=review.checklist_digest,
            decision="accepted",
            actor_github_id=profile.owner.github_id,
            actor_github_login=profile.owner.github_login,
            decided_at=decided_at,
            release_start="promote",
            acceptance_source="director_standing",
        )
    decision = store.create_release_review_decision_record_if_absent(decision)
    # Use the existing publication/recovery path. An unpublished decision cannot
    # queue a backup or promotion, and a retry keeps the same decision id.
    issue_url = decision.release_issue_url or publish_release_decision(
        store=store, control_plane_root=control_plane_root, profile=profile, decision=decision
    )
    if issue_url:
        store.record_release_review_decision_publication(
            record_id=decision.record_id, release_issue_url=issue_url
        )
        if backoff is not None:
            backoff.blocked.pop(profile.product, None)


def client_release_promotion_request(
    profile: LaunchplaneProductProfileRecord,
    decision: ReleaseReviewDecisionRecord,
    backup_record_id: str,
) -> GenericWebProdPromotionRequest:
    return GenericWebProdPromotionRequest(
        product=profile.product,
        artifact_id=decision.checklist.candidate.artifact_id,
        source_git_ref=decision.checklist.candidate.source_commit,
        backup_record_id=backup_record_id,
    )


def _run_generic_web_promotion(
    store: PostgresRecordStore,
    control_plane_root: Path,
    profile: LaunchplaneProductProfileRecord,
    decision: ReleaseReviewDecisionRecord,
    step: ClientReleaseStep,
    backup_record_id: str,
) -> str:
    request = client_release_promotion_request(profile, decision, backup_record_id)
    lane = next(lane for lane in profile.lanes if lane.instance == "prod")
    grant = client_release_grant(
        decision=decision,
        action="generic_web_prod_promotion.execute",
        context=lane.context,
        authorized_at=utc_now_timestamp(),
    )

    def validate_checkpoint() -> None:
        if not client_release_grant_allows(store, grant):
            raise click.ClickException("The release is no longer accepted.")

    def validate_before_effect(target: GenericWebResolvedDeployTarget) -> None:
        validate_checkpoint()
        covered = _covering_decision(store, profile.product, decision_record_id=decision.record_id)
        if covered is None:
            raise click.ClickException("The release is no longer accepted.")
        try:
            _require_current_release(store, control_plane_root, *covered)
        except ClientReleaseNotReady as error:
            raise click.ClickException("The accepted release changed.") from error
        require_generic_web_promotion_target(
            record_store=store,
            lane=lane,
            resolved_deploy_target=target,
        )

    adapter = GenericWebProdPromotionProviderMutationAdapter(
        control_plane_root=control_plane_root,
        record_store=store,
        promotion_request=GenericWebProdPromotionEnvelope(
            product=profile.product, promotion=request
        ),
        profile=profile,
        lane=lane,
        trace_id=f"client-release-{decision.record_id}",
        validate_before_effect=validate_before_effect,
        validate_before_checkpoint=validate_checkpoint,
        settle_pre_effect_failure=True,
    )
    # This is Launchplane's own worker, never a route or a caller promotion grant.
    # The shared runner fences the provider target, heartbeats, preserves uncertain
    # outcomes for reconciliation, and stores the #2743 rollback outcome.
    result = run_durable_provider_operation(
        store=store,
        scope=CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
        route_path=GENERIC_WEB_PROD_PROMOTION_ROUTE,
        idempotency_key=_step_key(decision, step),
        request_fingerprint=hashlib.sha256(request.model_dump_json().encode()).hexdigest(),
        lease_owner=f"client-release-{decision.record_id}-{datetime.now(UTC).isoformat()}",
        response_trace_id=f"client-release-{decision.record_id}",
        adapter=adapter,
    )
    operation_id = client_release_step_operation_id(profile=profile, decision=decision, step=step)
    return (
        operation_id
        if result.record is not None and result.record.record_id == operation_id
        else ""
    )


def advance_client_releases(
    *,
    store: object,
    control_plane_root: Path,
    stop_event: Event | None = None,
    standing_review_backoff: StandingReleaseReviewBackoff | None = None,
    invitation_backoff: ReleaseInvitationBackoff | None = None,
) -> tuple[str, ...]:
    """Queue the next step of every Client release that is ready for one.

    Returns the operation ids it queued. A step whose evidence no longer matches the
    Client's decision is never queued, so a stale acceptance starts nothing.
    """

    if not isinstance(store, PostgresRecordStore):
        return ()
    queued: list[str] = []
    profiles = store.list_product_profile_records()
    for profile in profiles:
        if stop_event is not None and stop_event.is_set():
            break
        if profile.driver_id not in {"odoo", "generic-web"}:
            continue
        try:
            _record_standing_acceptance(store, control_plane_root, profile, standing_review_backoff)
        except (FileNotFoundError, ValueError, click.ClickException):
            _LOGGER.info("standing acceptance not ready product=%s", profile.product)
            continue
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
        except Exception as error:
            _LOGGER.error(
                "client release failed product=%s error_type=%s",
                profile.product,
                type(error).__name__,
            )
            continue
        if operation_id:
            queued.append(operation_id)
    # Queue accepted releases before potentially slow invitation lookups.
    for profile in profiles:
        if stop_event is not None and stop_event.is_set():
            break
        try:
            publish_release_invitation(
                store=cast(ReleaseReviewStore, store),
                control_plane_root=control_plane_root,
                profile=profile,
                backoff=invitation_backoff,
            )
        except Exception as error:
            _LOGGER.warning(
                "release invitation unavailable product=%s error_type=%s",
                profile.product,
                type(error).__name__,
            )
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
            if profile.driver_id == "odoo":
                try:
                    pin_odoo_release_recovery_target(store, decision, context)
                except ValueError as error:
                    raise ClientReleaseNotReady("recovery_target_missing") from error
            return _queue_backup(store, profile, decision, step, context, authorized_at)
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
    # Cheap reads first, so a stale acceptance does not read GitHub on every poll.
    try:
        production = release_version(store=store, profile=profile, instance="prod")
        candidate = release_version(store=store, profile=profile, instance="testing")
    except (FileNotFoundError, ValueError) as error:
        raise ClientReleaseNotReady("release_record_missing") from error
    if (production, candidate) != (checklist.production, checklist.candidate):
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
    profile: LaunchplaneProductProfileRecord,
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
                promotion_action=(
                    GENERIC_WEB_PROMOTION_BACKUP_ACTION
                    if profile.driver_id == "generic-web"
                    else ODOO_PROMOTION_BACKUP_ACTION
                ),
                backup_record_id=_backup_record_id(context, decision, step.attempt),
            ),
            authorization=client_release_grant(
                decision=decision,
                action=PRODUCTION_BACKUP_GATE_EXECUTE_ACTION,
                context=context,
                authorized_at=authorized_at,
            ),
            operation_key=f"{CLIENT_RELEASE_IDEMPOTENCY_SCOPE}|{_step_key(decision, step)}",
            runner_trace_id=f"client-release-{decision.record_id}",
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
    if profile.driver_id == "generic-web":
        return _run_generic_web_promotion(
            store, control_plane_root, profile, decision, step, backup_record_id
        )
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
    try:
        recovery_pin = pin_odoo_release_recovery_target(store, decision, context)
    except ValueError as error:
        raise ClientReleaseNotReady("recovery_target_missing") from error
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
        checkpoints=(
            OdooProdPromotionCheckpoint(
                phase="created",
                recorded_at=authorized_at,
                evidence=recovery_pin,
            ),
        ),
        request=request,
        authorization=client_release_grant(
            decision=decision,
            action=ODOO_PROD_PROMOTION_RUN_ACTION,
            context=context,
            authorized_at=authorized_at,
        ),
        created_at=authorized_at,
        updated_at=authorized_at,
        runner_trace_id=f"client-release-{decision.record_id}",
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
        runner_trace_id=f"client-release-{decision.record_id}",
    )
    persisted, _created = store.create_odoo_prod_rollback_operation_record_if_no_active_lane(
        operation
    )
    return persisted.operation_id if persisted.operation_id == operation.operation_id else ""
