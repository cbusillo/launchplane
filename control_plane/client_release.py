"""A Client's accepted release, promoted by Launchplane itself.

When a product's recorded Client accepts a release and an admin has not held the
product's releases, the decision is stamped with how the release runs. The release
worker then runs the operations the admin's Release panel queues: a
verified backup, then the promotion. With the one rollback drill, it then rolls
back to the production version the Client's checklist was compiled against, takes
another backup, and promotes the same artifact again. Rolling back restores the
checklist's production version, so the checklist recompiles to the digest the
Client accepted, and that acceptance covers the second promotion and nothing else.

Every step runs under a Client release grant naming the decision. Fresh work
checks the product's newest decision, its recorded Client, and any release hold.
Generic rollback checks that authority until its own first marked effect. A
recipient inverse checks until its own replacement-writer pause dispatch, including
any earlier peer parking. After the relevant boundary, protective restoration
finishes under the original lease and target/publication fences.
The promotion checks release approval for the exact candidate and verified backup.
Each step's operation id derives from the decision, so the run needs no record of
its own and a second worker replica finds the same operations.

The Director's temporary hotfix runs the same backup and promotion under a grant that
names the hotfix decision and the Director, whom the active policy must still name as
administrator. Once it is live, the Client reviews that exact checklist: acceptance
starts nothing more, and a request for changes rolls production back to the newest
Client-accepted release with a passing production deployment, under the Client's grant.
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
from pydantic import BaseModel, ConfigDict, Field

from control_plane.child_process_errors import redact_untrusted_text
from control_plane.operation_status_read import safe_operation_error_code
from control_plane.service_deploy_drain import ServiceDeployDraining
from control_plane.odoo_business_hours import (
    BusinessHoursHeld,
    release_window,
    require_release_window,
)

from control_plane.contracts.deployment_record import DeploymentRecord, deployment_record_passed
from control_plane.contracts.generic_web_rollback import (
    GenericWebRollbackPlanRequest,
    build_generic_web_rollback_plan,
)
from control_plane.contracts.idempotency_record import build_launchplane_mutation_reservation_id
from control_plane.contracts.durable_operation_authorization import (
    RELEASE_DECISION_GRANTS,
    DurableOperationAuthorization,
    DurableOperationCallerIdentity,
)
from control_plane.durable_operation_authorization import (
    launchplane_identity_from_durable_caller,
    read_active_authz_policy_record,
)
from control_plane.service_auth import GitHubHumanIdentity
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
from control_plane.contracts.production_recipient import ProductionRecipientRecord
from control_plane.contracts.production_backup_gate import (
    PRODUCTION_BACKUP_GATE_EXECUTE_ACTION,
    ProductionBackupGateRequest,
)
from control_plane.contracts.release_review import (
    AcceptanceReleaseStart,
    ReleaseReviewDecisionRecord,
    ReleaseStart,
)
from control_plane.contracts.verireel_prod_backup_gate_operation import (
    VeriReelProdBackupGateOperationRecord,
)
from control_plane.odoo_release_recovery import (
    pin_odoo_release_recovery_target,
    read_odoo_release_recovery,
)
from control_plane.odoo_stable_lane import OdooStableLaneOperationConflictError
from control_plane.release_review import (
    ReleaseEvidenceUnavailable,
    current_release_review,
    release_version,
    recorded_generic_web_testing_version,
    checklist_blockers,
)
from control_plane.release_review_record import publish_release_decision
from control_plane.generic_web_promotion_http import (
    GENERIC_WEB_PROD_PROMOTION_ROUTE,
    GenericWebProdPromotionEnvelope,
)
from control_plane.generic_web_rollback_http import GENERIC_WEB_ROLLBACK_ROUTE
from control_plane.generic_web_rollback_drill import run_generic_web_rollback_drill
from control_plane.generic_web_source_qualification import GenericWebPromotionSourceRefused
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
    next_release_at: str = Field(
        default="",
        exclude_if=lambda value: not value,
        json_schema_extra={"x-launchplane-optional-response": True},
    )
    business_hours_timezone: str = Field(
        default="",
        exclude_if=lambda value: not value,
        json_schema_extra={"x-launchplane-optional-response": True},
    )
    steps: tuple[ClientReleaseStepView, ...]


def client_release_steps(release_start: ReleaseStart) -> tuple[ClientReleaseStep, ...]:
    if release_start == "promote_with_rollback_drill":
        return _PROMOTE_STEPS + _DRILL_STEPS
    if release_start == "promote":
        return _PROMOTE_STEPS
    if release_start == "rollback":
        return (ClientReleaseStep("rollback", 1),)
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
    if profile.driver_id == "generic-web" and step.kind in {"promote", "rollback"}:
        return build_launchplane_mutation_reservation_id(
            scope=CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
            route_path=GENERIC_WEB_PROD_PROMOTION_ROUTE
            if step.kind == "promote"
            else GENERIC_WEB_ROLLBACK_ROUTE,
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
) -> AcceptanceReleaseStart:
    """How a Client's acceptance recorded now runs; "" when it starts nothing.

    An Odoo or generic-web product with a prod lane, not prelaunch and not held, starts a
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
    if mode == "director_standing":
        if profile.driver_id != "generic-web":
            return ""
        mode = "promote_with_rollback_drill"
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
        caller=_decision_caller(decision),
        grant="temporary_hotfix"
        if decision.decision == "temporary_hotfix"
        else "client_release_acceptance",
        release_decision_record_id=decision.record_id,
    )


def _decision_caller(decision: ReleaseReviewDecisionRecord) -> DurableOperationCallerIdentity:
    return DurableOperationCallerIdentity(
        identity_type="github_human",
        login=decision.actor_github_login,
        github_id=int(decision.actor_github_id),
        role="admin" if decision.decision == "temporary_hotfix" else "client",
    )


def names_director(store: object, decision: ReleaseReviewDecisionRecord) -> bool:
    """Whether the active policy still names the hotfix's Director as administrator."""

    identity = launchplane_identity_from_durable_caller(_decision_caller(decision))
    try:
        policy = read_active_authz_policy_record(store).policy
    except (LookupError, TypeError):
        return False
    return isinstance(identity, GitHubHumanIdentity) and policy.names_administrator(identity)


def rejection_rollback_target(
    store: PostgresRecordStore, profile: LaunchplaneProductProfileRecord
) -> DeploymentRecord | None:
    """The newest Client-accepted release with a passing production deployment."""

    owner = profile.owner.github_id
    deployments = store.list_deployment_records(
        context_name=_prod_context(profile), instance_name="prod"
    )
    for decision in store.list_release_review_decision_records(product=profile.product):
        if (
            decision.decision != "accepted"
            or not owner
            or decision.actor_github_id != owner
            or decision.checklist.owner_github_id != owner
        ):
            continue
        version = decision.checklist.candidate
        for deployment in deployments:
            if (
                deployment.artifact_identity is not None
                and deployment.artifact_identity.artifact_id == version.artifact_id
                and (
                    profile.driver_id != "generic-web"
                    or deployment.source_git_ref == version.source_commit
                )
                and deployment_record_passed(deployment)
            ):
                return deployment
    return None


def is_hotfix_rejection(decision: ReleaseReviewDecisionRecord) -> bool:
    return decision.decision == "changes_requested" and decision.release_start == "rollback"


def rejection_rollback_pending(store: object, profile: LaunchplaneProductProfileRecord) -> bool:
    """The Client's published rejection still has production on the rejected hotfix.

    It stays authoritative until production no longer runs that candidate: after its
    rollback, or after an admin rollback when the automatic one stopped.
    """

    if not isinstance(store, PostgresRecordStore):
        return False
    latest = store.list_release_review_decision_records(product=profile.product, limit=1)
    if not latest or not is_hotfix_rejection(latest[0]) or not latest[0].release_issue_url:
        return False
    try:
        production = release_version(store=store, profile=profile, instance="prod")
    except (FileNotFoundError, ValueError):
        return True
    return production == latest[0].checklist.candidate


def live_temporary_hotfix(
    store: object, profile: LaunchplaneProductProfileRecord
) -> ReleaseReviewDecisionRecord | None:
    """The live temporary hotfix still awaiting the Client's published decision.

    Unpublished Client decisions on it keep the review open, so a retry, even with
    an edited reason, records against the same hotfix and keeps a rejection's rollback.
    """

    if not isinstance(store, PostgresRecordStore):
        return None
    unpublished: list[ReleaseReviewDecisionRecord] = []
    hotfix: ReleaseReviewDecisionRecord | None = None
    for decision in store.list_release_review_decision_records(product=profile.product):
        if decision.decision != "temporary_hotfix" and not decision.release_issue_url:
            unpublished.append(decision)
            continue
        hotfix = decision
        break
    if (
        hotfix is None
        or hotfix.decision != "temporary_hotfix"
        or any(decision.checklist_digest != hotfix.checklist_digest for decision in unpublished)
    ):
        return None
    run = read_client_release_run(store=store, profile=profile, decision=hotfix)
    try:
        production = release_version(store=store, profile=profile, instance="prod")
    except (FileNotFoundError, ValueError):
        return None
    if run is None or run.state != "passed" or production != hotfix.checklist.candidate:
        return None
    return hotfix


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
        or not profile.owner.github_id
    ):
        return None
    latest = record_store.list_release_review_decision_records(product=product, limit=1)
    if not latest:
        return None
    decision = latest[0]
    if (
        (decision_record_id and decision.record_id != decision_record_id)
        or not decision.release_start
        or not decision.release_issue_url
        or decision.checklist.owner_github_id != profile.owner.github_id
    ):
        return None
    if decision.decision == "temporary_hotfix":
        # The Director started it; the Client's release setting governs only Accept.
        return (profile, decision) if names_director(record_store, decision) else None
    if decision.decision == "changes_requested":
        # The Client rejected a live temporary hotfix: roll back to their last
        # accepted release, even while forward releases are held.
        rejected_hotfix = any(
            previous.decision == "temporary_hotfix"
            and previous.checklist_digest == decision.checklist_digest
            and previous.release_issue_url
            for previous in record_store.list_release_review_decision_records(product=product)
        )
        if decision.actor_github_id != profile.owner.github_id or not rejected_hotfix:
            return None
        return profile, decision
    if (
        decision.decision != "accepted"
        or profile.release_on_acceptance == "held"
        or decision.actor_github_id != profile.owner.github_id
        or (
            decision.acceptance_source == "director_standing"
            and profile.release_on_acceptance != "director_standing"
        )
    ):
        return None
    return profile, decision


def accepted_release_can_start(store: object, decision: ReleaseReviewDecisionRecord) -> bool:
    """Whether this decision is still the product's newest one that starts a release."""

    return (
        _covering_decision(store, decision.product, decision_record_id=decision.record_id)
        is not None
    )


def _client_release_authority(
    store: object,
    authorization: DurableOperationAuthorization,
) -> tuple[LaunchplaneProductProfileRecord, ReleaseReviewDecisionRecord] | None:
    """Read the covering authority and its production boundary independently of testing."""

    if authorization.grant not in RELEASE_DECISION_GRANTS or authorization.instances != ("prod",):
        return None
    covered = _covering_decision(
        store,
        authorization.product,
        decision_record_id=authorization.release_decision_record_id,
    )
    if covered is None:
        return None
    profile, decision = covered
    context = _prod_context(profile)
    checklist = decision.checklist
    expected = _decision_caller(decision)
    try:
        record_store = cast(PostgresRecordStore, store)
        production = release_version(store=record_store, profile=profile, instance="prod")
    except (FileNotFoundError, ValueError):
        return None
    if (
        authorization.context != context
        or authorization.grant
        != ("temporary_hotfix" if expected.role == "admin" else "client_release_acceptance")
        or authorization.caller.github_id != expected.github_id
        or authorization.caller.role != expected.role
    ):
        return None
    if decision.release_start == "rollback":
        # The rejected hotfix's candidate is live until the rollback restores the
        # Client's last accepted release; testing may already carry newer work.
        target = rejection_rollback_target(record_store, profile)
        return (
            covered
            if target is not None
            and production.artifact_id
            in (
                checklist.candidate.artifact_id,
                target.artifact_identity.artifact_id if target.artifact_identity else "",
            )
            else None
        )
    return (
        covered
        if production
        in (
            checklist.production,
            checklist.candidate,
        )
        else None
    )


def client_release_grant_allows(
    store: object, authorization: DurableOperationAuthorization
) -> bool:
    """Validate external/queued release work against its authority and current source."""
    covered = _client_release_authority(store, authorization)
    if covered is None:
        return False
    profile, decision = covered
    if decision.release_start == "rollback" and profile.driver_id == "generic-web":
        return True
    try:
        testing = (
            recorded_generic_web_testing_version(
                store=cast(PostgresRecordStore, store), profile=profile
            )
            if profile.driver_id == "generic-web"
            else release_version(
                store=cast(PostgresRecordStore, store), profile=profile, instance="testing"
            )
        )
    except (FileNotFoundError, ValueError):
        return False
    return decision.release_start == "rollback" or testing == decision.checklist.candidate


def _generic_web_release_authority_allows(
    store: object, authorization: DurableOperationAuthorization
) -> bool:
    """Ongoing authority for an admitted web attempt or its accepted baseline inverse."""
    covered = _client_release_authority(store, authorization)
    return covered is not None and covered[0].driver_id == "generic-web"


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
        elif profile.driver_id == "generic-web":
            reservation = store.read_idempotency_record(
                scope=CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
                route_path=GENERIC_WEB_PROD_PROMOTION_ROUTE
                if step.kind == "promote"
                else GENERIC_WEB_ROLLBACK_ROUTE,
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
                "pass"
                if outcome.get("promotion_status" if step.kind == "promote" else "rollback_status")
                == "pass"
                else "fail"
            ), reservation
        elif step.kind == "promote":
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
            code = code or str(outcome.get("error_code", ""))
            if not message and code == "promotion_not_ready":
                message = "The release stopped before deployment because its readiness or acceptance checks no longer passed."
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


_TESTING_RELEASE_CHANGED_MESSAGE = "Testing has moved to a different version. Qualify this accepted artifact and revision again, or accept the new candidate for its own release."


def _release_needs_testing(
    profile: LaunchplaneProductProfileRecord, views: tuple[ClientReleaseStepView, ...]
) -> bool:
    next_step = next(view for view in views if view.status != "pass")
    return not (
        profile.driver_id == "generic-web"
        and (
            next_step.kind == "rollback"
            or next_step.kind == "promote"
            and next_step.status in {"pending", "running"}
        )
    )


def _testing_release_evidence_blocked_reason(error: ValueError | FileNotFoundError) -> str:
    if (
        isinstance(error, ReleaseEvidenceUnavailable)
        and error.code == "candidate_operation_unresolved"
    ):
        return str(error)
    if isinstance(error, ReleaseEvidenceUnavailable) and error.code == "candidate_identity_missing":
        return "Testing must qualify this accepted artifact and revision again before a fresh release step can start. Run its normal testing deployment."
    return "The testing release evidence is unavailable. Restore the testing lane and qualify this accepted artifact and revision before starting a fresh release step."


def _release_run_blocked_reason(
    store: PostgresRecordStore,
    profile: LaunchplaneProductProfileRecord,
    decision: ReleaseReviewDecisionRecord,
    views: tuple[ClientReleaseStepView, ...],
) -> str:
    """Explain known local blockers without recompiling a checklist or admitting work."""

    if not decision.release_issue_url:
        return "The release approval has not been published yet. This release cannot advance until its release record is published."
    if not accepted_release_can_start(store, decision):
        if decision.decision == "accepted" and profile.release_on_acceptance == "held":
            return (
                "Releases are on hold. This release cannot advance until an admin lifts the hold."
            )
        return "This decision no longer covers the product's release. The current Client and release decision must be reviewed before it can advance."
    try:
        production = release_version(store=store, profile=profile, instance="prod")
        # A rejected hotfix rolls back even if testing has moved on.
        if decision.release_start == "rollback":
            return ""
    except (FileNotFoundError, ValueError):
        return "The current release's lane records are unavailable. An admin must reconcile them before this release can advance."
    try:
        candidate = (
            (
                recorded_generic_web_testing_version(store=store, profile=profile)
                if profile.driver_id == "generic-web"
                else release_version(store=store, profile=profile, instance="testing")
            )
            if _release_needs_testing(profile, views)
            else None
        )
    except (FileNotFoundError, ValueError) as error:
        return _testing_release_evidence_blocked_reason(error)
    if candidate is not None and candidate != decision.checklist.candidate:
        return _TESTING_RELEASE_CHANGED_MESSAGE
    next_step = next(view for view in views if view.status != "pass")
    expected_production = (
        decision.checklist.candidate
        if next_step.kind == "rollback"
        else decision.checklist.production
    )
    # An admitted promotion/rollback may have updated production before settling.
    if production != expected_production and not (
        next_step.kind in {"promote", "rollback"}
        and next_step.status in {"pending", "running"}
        and production in (decision.checklist.production, decision.checklist.candidate)
    ):
        return "The production version has changed since this release was accepted. Its release evidence must be reviewed before it can advance."
    return ""


def read_client_release_run(
    *,
    store: object,
    profile: LaunchplaneProductProfileRecord,
    decision: ReleaseReviewDecisionRecord,
    control_plane_root: Path | None = None,
) -> ClientReleaseRunView | None:
    run = _read_client_release_continuation(
        store=store, profile=profile, decision=decision, control_plane_root=control_plane_root
    )
    if (
        run is None
        or run.blocked_reason
        or run.state not in {"waiting", "running"}
        or profile.driver_id != "generic-web"
        or not _release_needs_testing(profile, run.steps)
    ):
        return run
    try:
        candidate = release_version(
            store=cast(PostgresRecordStore, store), profile=profile, instance="testing"
        )
    except (FileNotFoundError, ValueError) as error:
        return run.model_copy(
            update={"blocked_reason": _testing_release_evidence_blocked_reason(error)}
        )
    if candidate != decision.checklist.candidate:
        return run.model_copy(update={"blocked_reason": _TESTING_RELEASE_CHANGED_MESSAGE})
    return run


def _read_client_release_continuation(
    *,
    store: object,
    profile: LaunchplaneProductProfileRecord,
    decision: ReleaseReviewDecisionRecord,
    control_plane_root: Path | None = None,
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
    blocked_reason = (
        _release_run_blocked_reason(store, profile, decision, views)
        if state in {"waiting", "running"}
        else ""
    )
    window = None
    if not blocked_reason and state in {"waiting", "running"}:
        window = release_window(
            store=store, profile=profile, decision=decision, control_plane_root=control_plane_root
        )
        blocked_reason = window.blocked_reason
    from control_plane.service_deploy_drain import read_status

    if (
        not blocked_reason
        and state in {"waiting", "running"}
        and read_status(store).get("admission_paused")
    ):
        blocked_reason = "Launchplane is draining admitted release steps before replacement. Pending steps will resume on the replacement worker."
    next_step = next((view for view in views if view.status != "pass"), None)
    native_inverse = None
    if (
        not blocked_reason
        and profile.driver_id == "generic-web"
        and state == "waiting"
        and next_step is not None
        and next_step.kind == "rollback"
    ):
        try:
            native_inverse = _native_recipient_inverse(store, profile, decision)
        except ClientReleaseNotReady:
            blocked_reason = "The retained recipient inverse's saved baseline or owned publication rows changed. Reconcile its existing release before restoring production."
    if not blocked_reason and decision.release_start == "rollback" and state == "waiting":
        if native_inverse is None and rejection_rollback_target(store, profile) is None:
            blocked_reason = "No Client-accepted release has a passing production deployment to roll back to. An admin must roll back by hand."
    elif not blocked_reason and profile.driver_id == "odoo" and state == "waiting":
        try:
            pin_odoo_release_recovery_target(store, decision, _prod_context(profile))
        except ValueError:
            blocked_reason = "No passing production deployment is available for recovery. An admin must reconcile the production record before this release can start."
    if (
        not blocked_reason
        and profile.driver_id == "generic-web"
        and decision.release_start == "promote_with_rollback_drill"
        and state == "waiting"
        and native_inverse is None
    ):
        try:
            _generic_web_drill_target(store, profile, decision)
        except ClientReleaseNotReady:
            blocked_reason = "The reviewed production version has no deployable passing rollback target. An admin must reconcile its deployment evidence before this drill can continue."
    return ClientReleaseRunView(
        blocked_reason=blocked_reason,
        next_release_at=window.next_release_at if window else "",
        business_hours_timezone=window.timezone if window else "",
        decision_record_id=decision.record_id,
        rollback_drill=decision.release_start == "promote_with_rollback_drill",
        state=state,
        steps=tuple(views),
    )


class ClientReleaseNotReady(Exception):
    """The next step cannot be queued now; nothing was queued."""


def _require_standing_release_available(store: PostgresRecordStore, product: str) -> None:
    """Automatic acceptance cannot supersede an outstanding admitted production attempt."""
    covered = _covering_decision(store, product)
    if covered is None:
        return
    profile, decision = covered
    if profile.driver_id != "generic-web":
        return
    steps = client_release_steps(decision.release_start)
    statuses: dict[str, ClientReleaseStepStatus] = {}
    for step in steps:
        status, operation = _step_status(store, profile, decision, step)
        statuses[step.name] = status
        if step.kind in {"promote", "rollback"} and (
            status == "reconciliation_required"
            or status == "running"
            and bool(getattr(operation, "provider_effect_started_at", ""))
        ):
            raise ClientReleaseNotReady("standing_release_effects_outstanding")
    if (
        decision.release_start == "promote_with_rollback_drill"
        and statuses.get(ClientReleaseStep("promote", 1).name) == "pass"
        and statuses.get(ClientReleaseStep("rollback", 1).name)
        in {"not_started", "pending", "running", "reconciliation_required"}
    ):
        production = release_version(store=store, profile=profile, instance="prod")
        if production != decision.checklist.candidate:
            return
        try:
            if _native_recipient_inverse(store, profile, decision) is None:
                _generic_web_drill_target(store, profile, decision)
        except ClientReleaseNotReady:
            # No original inverse was dispatched. A new candidate must qualify
            # its own reviewed production baseline and drill before any effect.
            return
        raise ClientReleaseNotReady("standing_release_inverse_outstanding")


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
    _require_standing_release_available(store, profile.product)
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
            release_start=release_start_for_acceptance(store=store, profile=profile),
            acceptance_source="director_standing",
        )
    decision = store.create_release_review_decision_record_if_absent(
        decision,
        validate_before_insert=lambda: _require_standing_release_available(store, profile.product),
    )
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
        if not _generic_web_release_authority_allows(store, grant):
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

    from control_plane.production_recipient import custody
    from control_plane.production_recipient_cutover import RecipientCutoverAdapter

    recipient = custody(store, profile.product, lane.context)
    if (
        recipient is not None
        and recipient.activation
        and recipient.phase != "retired"
        and (
            recipient.phase != "published"
            or decision.checklist.candidate.source_commit == recipient.request.source_commit
        )
    ):
        if (
            decision.checklist.candidate.artifact_id,
            decision.checklist.candidate.source_commit,
        ) != (recipient.image_reference, recipient.request.source_commit):
            raise ClientReleaseNotReady("recipient_release_candidate_changed")

        def validate_recipient_release() -> None:
            if (
                decision.decision != "accepted"
                or decision.actor_github_id != profile.owner.github_id
            ):
                raise click.ClickException(
                    "Recipient cutover requires the Client's accepted release."
                )
            validate_checkpoint()
            covered = _covering_decision(
                store, profile.product, decision_record_id=decision.record_id
            )
            if covered is None:
                raise click.ClickException("The release is no longer accepted.")

        def validate_recipient_source() -> None:
            # The adapter admits the source before this attempt starts production;
            # its later acceptance and recovery checks do not chase testing.
            _require_qualified_generic_web_release_source(store, profile, decision)

        if recipient.phase != "published" and not recipient.execution:
            _require_current_release(store, control_plane_root, profile, decision)

        recipient_adapter = RecipientCutoverAdapter(
            control_plane_root,
            store,
            recipient,
            f"client-release-{decision.record_id}",
            backup_record_id,
            validate_recipient_release,
            validate_source=validate_recipient_source,
        )
        recipient_result = run_durable_provider_operation(
            store=store,
            scope=CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
            route_path=GENERIC_WEB_PROD_PROMOTION_ROUTE,
            idempotency_key=_step_key(decision, step),
            request_fingerprint=hashlib.sha256(request.model_dump_json().encode()).hexdigest(),
            lease_owner=f"client-release-{decision.record_id}-{datetime.now(UTC).isoformat()}",
            response_trace_id=f"client-release-{decision.record_id}",
            adapter=recipient_adapter,
        )
        expected_id = client_release_step_operation_id(
            profile=profile, decision=decision, step=step
        )
        return (
            expected_id
            if recipient_result.record is not None
            and recipient_result.record.record_id == expected_id
            else ""
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
        except (ClientReleaseNotReady, FileNotFoundError, ValueError, click.ClickException):
            _LOGGER.info("standing acceptance not ready product=%s", profile.product)
        covered = _covering_decision(store, profile.product)
        if covered is None:
            continue
        try:
            operation_id = _advance(store, control_plane_root, *covered)
        except (
            ClientReleaseNotReady,
            BusinessHoursHeld,
            GenericWebPromotionSourceRefused,
        ) as reason:
            _LOGGER.info(
                "client release not advanced product=%s reason=%s", profile.product, reason
            )
            continue
        except ServiceDeployDraining:
            # Admission raced the self-deploy fence; the pending step is untouched.
            continue
        except OdooStableLaneOperationConflictError:
            continue
        except Exception as error:
            _LOGGER.error(
                "client release failed product=%s error_type=%s",
                profile.product,
                type(error).__name__,
            )
            # A failure before a step record exists must still reach delivery attention.
            from control_plane.production_delivery_monitor import record_production_delivery_failure

            accepted_profile, decision = covered
            lane = next((lane for lane in accepted_profile.lanes if lane.instance == "prod"), None)
            if lane is not None:
                try:
                    record_production_delivery_failure(
                        store,
                        accepted_profile,
                        lane,
                        candidate=decision.checklist.candidate.artifact_id,
                        reason="client_release_failed",
                        trace_id=f"client-release-{decision.record_id}",
                    )
                except Exception:
                    _LOGGER.exception(
                        "Could not persist client release failure product=%s", profile.product
                    )
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
    from control_plane.service_deploy_drain import read_status

    if read_status(store).get("admission_paused"):
        return ""
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
        if decision.release_start == "rollback":
            return _queue_rejection_rollback(
                store, control_plane_root, profile, decision, step, context, authorized_at
            )
        if step.kind == "rollback":
            return _queue_rollback(
                store, control_plane_root, profile, decision, step, context, authorized_at
            )
        require_release_window(
            store=store, profile=profile, decision=decision, control_plane_root=control_plane_root
        )
        _require_current_release(store, control_plane_root, profile, decision)
        if step.kind == "backup":
            if profile.driver_id == "odoo":
                try:
                    pin_odoo_release_recovery_target(store, decision, context)
                except ValueError as error:
                    raise ClientReleaseNotReady("recovery_target_missing") from error
            elif decision.release_start == "promote_with_rollback_drill":
                _generic_web_drill_target(store, profile, decision)
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


def _require_qualified_generic_web_release_source(
    store: PostgresRecordStore,
    profile: LaunchplaneProductProfileRecord,
    decision: ReleaseReviewDecisionRecord,
) -> None:
    try:
        candidate = release_version(store=store, profile=profile, instance="testing")
    except ReleaseEvidenceUnavailable as error:
        raise GenericWebPromotionSourceRefused(str(error)) from error
    except (FileNotFoundError, ValueError) as error:
        raise GenericWebPromotionSourceRefused(
            "Testing is no longer qualified for release."
        ) from error
    if candidate != decision.checklist.candidate:
        raise GenericWebPromotionSourceRefused("The accepted testing release changed.")


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
    except (FileNotFoundError, ValueError) as error:
        raise ClientReleaseNotReady("release_record_missing") from error
    if production != checklist.production:
        raise ClientReleaseNotReady("release_changed")
    if profile.driver_id == "generic-web":
        _require_qualified_generic_web_release_source(store, profile, decision)
    else:
        try:
            candidate = release_version(store=store, profile=profile, instance="testing")
        except (FileNotFoundError, ValueError) as error:
            raise ClientReleaseNotReady("release_record_missing") from error
        if candidate != checklist.candidate:
            raise ClientReleaseNotReady("release_changed")
    review = current_release_review(
        control_plane_root=control_plane_root, record_store=store, profile=profile
    )
    if profile.driver_id == "generic-web":
        if (
            review.latest_decision is not None
            and review.latest_decision.record_id != decision.record_id
        ):
            raise ClientReleaseNotReady("release_not_accepted")
        if review.unavailable_reason in {
            "candidate_identity_missing",
            "candidate_operation_unresolved",
        }:
            raise GenericWebPromotionSourceRefused(
                str(ReleaseEvidenceUnavailable(review.unavailable_reason))
            )
        if review.checklist is not None:
            if review.checklist.production != checklist.production:
                raise ClientReleaseNotReady("release_changed")
            if review.checklist.candidate != checklist.candidate:
                raise GenericWebPromotionSourceRefused("The accepted testing release changed.")
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
    control_plane_root: Path,
    profile: LaunchplaneProductProfileRecord,
    decision: ReleaseReviewDecisionRecord,
    step: ClientReleaseStep,
    context: str,
    authorized_at: str,
) -> str:
    """Roll back to the production version the Client's checklist was compiled against."""

    checklist = decision.checklist
    try:
        production = release_version(store=store, profile=profile, instance="prod")
        testing = (
            release_version(store=store, profile=profile, instance="testing")
            if profile.driver_id != "generic-web"
            else None
        )
    except (FileNotFoundError, ValueError) as error:
        raise ClientReleaseNotReady("release_record_missing") from error
    if production.artifact_id != checklist.candidate.artifact_id or (
        testing is not None and testing.artifact_id != checklist.candidate.artifact_id
    ):
        raise ClientReleaseNotReady("release_changed")
    target_artifact_id = checklist.production.artifact_id
    if profile.driver_id == "generic-web":
        recipient_operation = _queue_recipient_inverse(
            store, control_plane_root, profile, decision, step, context, authorized_at
        )
        if recipient_operation is not None:
            return recipient_operation
    deployment = (
        _generic_web_drill_target(store, profile, decision)
        if profile.driver_id == "generic-web"
        else next(
            (
                record
                for record in store.list_deployment_records(
                    context_name=context, instance_name="prod"
                )
                if record.artifact_identity is not None
                and record.artifact_identity.artifact_id == target_artifact_id
                and deployment_record_passed(record)
            ),
            None,
        )
    )
    if deployment is None:
        raise ClientReleaseNotReady("rollback_target_missing")
    if profile.driver_id == "generic-web":
        grant = client_release_grant(
            decision=decision,
            action="generic_web_rollback.execute",
            context=context,
            authorized_at=authorized_at,
        )

        def validate_checkpoint() -> None:
            if not _generic_web_release_authority_allows(store, grant):
                raise click.ClickException("The release is no longer accepted.")

        operation_id = run_generic_web_rollback_drill(
            store=store,
            control_plane_root=control_plane_root,
            profile=profile,
            decision=decision,
            deployment=deployment,
            scope=CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
            idempotency_key=_step_key(decision, step),
            validate_checkpoint=validate_checkpoint,
        )
        expected_id = client_release_step_operation_id(
            profile=profile, decision=decision, step=step
        )
        return expected_id if operation_id == expected_id else ""
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


def _generic_web_drill_target(
    store: PostgresRecordStore,
    profile: LaunchplaneProductProfileRecord,
    decision: ReleaseReviewDecisionRecord,
) -> DeploymentRecord:
    from control_plane.production_recipient import custody

    recipient = custody(store, profile.product, _prod_context(profile))
    if (
        recipient is not None
        and recipient.publication
        and recipient.execution.get("release_trace") == f"client-release-{decision.record_id}"
    ):
        before = recipient.publication["before"]["inventory"]
        if before is not None and (
            before["source_git_ref"] == decision.checklist.production.source_commit
        ):
            retained = store.read_deployment_record(before["deployment_record_id"])
            if (
                retained.artifact_identity is not None
                and retained.artifact_identity.artifact_id
                == decision.checklist.production.artifact_id
                and deployment_record_passed(retained)
            ):
                return retained
        raise ClientReleaseNotReady("rollback_target_missing")
    for deployment in store.list_deployment_records(
        context_name=_prod_context(profile), instance_name="prod"
    ):
        if (
            deployment.artifact_identity is None
            or deployment.artifact_identity.artifact_id != decision.checklist.production.artifact_id
            or deployment.source_git_ref != decision.checklist.production.source_commit
            or not deployment_record_passed(deployment)
        ):
            continue
        plan = build_generic_web_rollback_plan(
            record_store=store,
            request=GenericWebRollbackPlanRequest(
                product=profile.product, rollback_deployment_record_id=deployment.record_id
            ),
        )
        if plan.status != "ready":
            raise ClientReleaseNotReady("rollback_not_ready")
        return deployment
    raise ClientReleaseNotReady("rollback_target_missing")


def _queue_rejection_rollback(
    store: PostgresRecordStore,
    control_plane_root: Path,
    profile: LaunchplaneProductProfileRecord,
    decision: ReleaseReviewDecisionRecord,
    step: ClientReleaseStep,
    context: str,
    authorized_at: str,
) -> str:
    """Return production from a rejected temporary hotfix to the last accepted release."""

    try:
        production = release_version(store=store, profile=profile, instance="prod")
    except (FileNotFoundError, ValueError) as error:
        raise ClientReleaseNotReady("release_record_missing") from error
    if production.artifact_id != decision.checklist.candidate.artifact_id:
        raise ClientReleaseNotReady("release_changed")
    if profile.driver_id == "generic-web":
        recipient_operation = _queue_recipient_inverse(
            store, control_plane_root, profile, decision, step, context, authorized_at
        )
        if recipient_operation is not None:
            return recipient_operation
    deployment = rejection_rollback_target(store, profile)
    if deployment is None or deployment.artifact_identity is None:
        raise ClientReleaseNotReady("rollback_target_missing")
    grant = client_release_grant(
        decision=decision,
        action="generic_web_rollback.execute"
        if profile.driver_id == "generic-web"
        else ODOO_PROD_ROLLBACK_ACTION,
        context=context,
        authorized_at=authorized_at,
    )
    expected_id = client_release_step_operation_id(profile=profile, decision=decision, step=step)
    if profile.driver_id == "generic-web":
        plan = build_generic_web_rollback_plan(
            record_store=store,
            request=GenericWebRollbackPlanRequest(
                product=profile.product, rollback_deployment_record_id=deployment.record_id
            ),
        )
        if plan.status != "ready":
            raise ClientReleaseNotReady("rollback_not_ready")

        def validate_checkpoint() -> None:
            if not _generic_web_release_authority_allows(store, grant):
                raise click.ClickException("The Client's request for changes no longer applies.")

        operation_id = run_generic_web_rollback_drill(
            store=store,
            control_plane_root=control_plane_root,
            profile=profile,
            decision=decision,
            deployment=deployment,
            scope=CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
            idempotency_key=_step_key(decision, step),
            validate_checkpoint=validate_checkpoint,
        )
        return expected_id if operation_id == expected_id else ""
    request = OdooProdRollbackRequest(
        context=context,
        artifact_id=deployment.artifact_identity.artifact_id,
        reason=f"The Client requested changes to temporary hotfix {decision.record_id}.",
    )
    try:
        resolve_odoo_prod_rollback_target(record_store=store, request=request)
    except click.ClickException as error:
        raise ClientReleaseNotReady("rollback_not_ready") from error
    operation = OdooProdRollbackOperationRecord(
        operation_id=expected_id,
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
            artifact_id=deployment.artifact_identity.artifact_id,
            deployment_record_id=deployment.record_id,
        ),
        authorization=grant,
        created_at=authorized_at,
        updated_at=authorized_at,
        runner_trace_id=f"client-release-{decision.record_id}",
    )
    persisted, _created = store.create_odoo_prod_rollback_operation_record_if_no_active_lane(
        operation
    )
    return persisted.operation_id if persisted.operation_id == operation.operation_id else ""


def _native_recipient_inverse(
    store: PostgresRecordStore,
    profile: LaunchplaneProductProfileRecord,
    decision: ReleaseReviewDecisionRecord,
) -> ProductionRecipientRecord | None:
    """Read native inverse applicability without provider I/O or fresh drill gates."""
    from control_plane.production_recipient import custody, RecipientAuthorityChanged
    from control_plane.production_recipient_publication import require_owned_publication_rows

    record = custody(store, profile.product, _prod_context(profile))
    if (
        record is None
        or not record.publication
        or record.execution.get("release_trace") != f"client-release-{decision.record_id}"
        or record.phase not in {"published", "reverting", "inverse_published", "rolled_back"}
    ):
        return None
    before = record.publication["before"]["inventory"]
    if (
        before is None
        or before["source_git_ref"] != decision.checklist.production.source_commit
        or (before.get("runtime_identity") or {}).get("artifact_id")
        != decision.checklist.production.artifact_id
    ):
        raise ClientReleaseNotReady("recipient_inverse_target_changed")
    if record.phase in {"published", "reverting"}:
        try:
            require_owned_publication_rows(store, record)
        except (RecipientAuthorityChanged, FileNotFoundError) as error:
            raise ClientReleaseNotReady("recipient_inverse_publication_changed") from error
    return record


def _queue_recipient_inverse(
    store: PostgresRecordStore,
    root: Path,
    profile: LaunchplaneProductProfileRecord,
    decision: ReleaseReviewDecisionRecord,
    step: ClientReleaseStep,
    context: str,
    authorized_at: str,
) -> str | None:
    from control_plane.production_recipient_inverse import run_recipient_inverse

    record = _native_recipient_inverse(store, profile, decision)
    if record is None:
        return None
    grant = client_release_grant(
        decision=decision,
        action="generic_web_rollback.execute",
        context=context,
        authorized_at=authorized_at,
    )

    def validate() -> None:
        if not _generic_web_release_authority_allows(store, grant):
            raise click.ClickException("The Client release no longer authorizes this inverse.")

    actual = run_recipient_inverse(
        root=root,
        store=store,
        record=record,
        scope=CLIENT_RELEASE_IDEMPOTENCY_SCOPE,
        route_path=GENERIC_WEB_ROLLBACK_ROUTE,
        key=_step_key(decision, step),
        trace=f"client-release-{decision.record_id}",
        validate_release=validate,
    )
    expected = client_release_step_operation_id(profile=profile, decision=decision, step=step)
    return expected if actual == expected else ""
