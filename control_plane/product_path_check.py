"""One read that says whether a caller can take a product along a path.

The path check composes records other reads already show: the product
profile, the testing lane's hold and last reconcile attempt, the caller's own
authorization, Client release acceptance, production backup authority and the
last promotion. Each step is ``clear``, ``blocked`` or ``unknown``, with a code,
Launchplane's fixed description and the kind of fix. Every blocker is reported
at once. A step whose evidence could not be read is ``unknown``, never
``clear``. Nothing here writes, and no provider, script or exception text is
returned.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import re
from typing import Literal, Protocol, cast

import click

from pydantic import BaseModel, ConfigDict

from control_plane.contracts.odoo_target_replacement_failures import (
    DEPLOY_BLOCKED_PREFIX,
)
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.contracts.production_backup_authority import (
    ProductionBackupAuthorityReadModel,
)
from control_plane.contracts.promotion_record import (
    PROMOTION_FAILURE_DESCRIPTIONS,
    PromotionRecord,
)
from control_plane.contracts.release_review import ReleaseReviewStatus
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.deployment_record import DeploymentRecord, previous_passing_deployment
from control_plane.contracts.generic_web_rollback import (
    GenericWebRollbackPlanReader,
    GenericWebRollbackPlanRequest,
    build_generic_web_rollback_plan,
)
from control_plane.contracts.odoo_prod_rollback_operation import OdooProdRollbackRequest
from control_plane.workflows.odoo_prod_rollback import (
    OdooProdRollbackTargetMissingError,
    resolve_odoo_prod_rollback_target,
)
from control_plane.contracts.odoo_prod_promotion_operation import ODOO_PROD_PROMOTION_RUN_ACTION
from control_plane.operation_status_read import safe_operation_error_code
from control_plane.product_reconcile_read import (
    ProductReconcileRequestReader,
    product_reconcile_request_view,
)
from control_plane.production_backup_authority import (
    require_production_backup_authority_store,
    resolve_production_backup_authority,
)
from control_plane.drivers.registry import effective_driver_actions, read_driver_descriptor
from control_plane.contracts.driver_descriptor import DriverDescriptor
from control_plane.testing_lane_hold import TestingHoldReader, read_staff_testing_hold
from control_plane.workflows.production_promotion_backup import (
    GENERIC_WEB_PROMOTION_BACKUP_ACTION,
    ODOO_PROMOTION_BACKUP_ACTION,
)

# The action the generic-web release panel's live promotion checks.
GENERIC_WEB_PROMOTION_ACTION = "generic_web_prod_promotion.dispatch"

PathName = Literal["testing", "promote", "rollback"]
PATH_NAMES: tuple[PathName, ...] = ("testing", "promote", "rollback")
StepState = Literal["clear", "blocked", "unknown"]
FixKind = Literal["none", "code", "grant", "owner_approval", "client_acceptance", "by_hand", "wait"]


class PathCheckStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str
    state: StepState
    code: str
    description: str
    fix: FixKind = "none"
    record_ids: tuple[str, ...] = ()


class ProductPathCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    path: PathName
    state: StepState
    blocked_count: int
    unknown_count: int
    steps: tuple[PathCheckStep, ...]


@dataclass(frozen=True)
class Unread:
    """Evidence a step needed but could not read; the step reports ``unknown``."""

    code: str


@dataclass(frozen=True)
class PathCheckInputs:
    profile: LaunchplaneProductProfileRecord | Unread
    testing_hold_active: bool | Unread = False
    testing_reconcile_plan: dict[str, object] | None | Unread = None
    promotion_allowed: bool | Unread = False
    promotion_action: str = ""
    # An Odoo prod release is queued only by the signed-in admin.
    promotion_needs_administrator: bool = False
    release_review: ReleaseReviewStatus | Unread | None = None
    backup_authority: ProductionBackupAuthorityReadModel | Unread | None = None
    latest_promotion: PromotionRecord | None | Unread = None
    rollback_steps: tuple[PathCheckStep, ...] = ()


_TRANSIENT_DEPLOY_CHECKS = frozenset(
    {
        "provider_target_unreadable",
        "runtime_key_safety_unavailable",
        "runtime_settings_unavailable",
        "site_environment_unresolved",
    }
)


def _step(
    step_id: str,
    state: StepState,
    code: str,
    description: str,
    fix: FixKind = "none",
    record_ids: Sequence[str] = (),
) -> PathCheckStep:
    return PathCheckStep(
        step_id=step_id,
        state=state,
        code=code,
        description=description,
        fix=fix if state != "clear" else "none",
        record_ids=tuple(record_id for record_id in record_ids if record_id),
    )


def _unread(step_id: str, unread: Unread) -> PathCheckStep:
    return _step(
        step_id,
        "unknown",
        unread.code,
        "Launchplane could not read the evidence for this step.",
        "wait",
    )


def _lane(profile: LaunchplaneProductProfileRecord, instance: str) -> ProductLaneProfile | None:
    return next((lane for lane in profile.lanes if lane.instance == instance), None)


def _profile_step(
    profile: LaunchplaneProductProfileRecord | Unread, instance: str
) -> PathCheckStep:
    if isinstance(profile, Unread):
        return _unread("profile_lane", profile)
    if profile.lifecycle_state != "active":
        return _step(
            "profile_lane",
            "blocked",
            "product_not_active",
            "The product is retiring or retired.",
            "owner_approval",
        )
    if _lane(profile, instance) is None:
        return _step(
            "profile_lane",
            "blocked",
            f"{instance}_lane_missing",
            f"The product profile has no {instance} lane.",
            "by_hand",
        )
    return _step("profile_lane", "clear", "lane_recorded", f"The {instance} lane is recorded.")


def _testing_failure_fix(code: str) -> FixKind:
    if code.startswith(DEPLOY_BLOCKED_PREFIX):
        check = code.removeprefix(DEPLOY_BLOCKED_PREFIX)
        return "wait" if check in _TRANSIENT_DEPLOY_CHECKS else "by_hand"
    if code.startswith("plan_not_ready."):
        return "by_hand"
    if code.startswith("operation_authorization_"):
        return "grant"
    return "code"


def _testing_attempt_step(plan: dict[str, object] | None | Unread) -> PathCheckStep:
    step_id = "testing_deploy"
    if isinstance(plan, Unread):
        return _unread(step_id, plan)
    if plan is None:
        return _step(
            step_id,
            "unknown",
            "no_reconcile_record",
            "Launchplane has not planned a testing deploy for this product yet.",
            "wait",
        )
    deployed = str(plan.get("deployed_operation_id") or "")
    queued = str(plan.get("queued_operation_id") or "")
    failed = str(plan.get("last_failed_operation_id") or "")
    if deployed:
        return _step(
            step_id,
            "clear",
            "already_deployed",
            "The testing lane runs the latest release build.",
            record_ids=(deployed,),
        )
    if queued:
        return _step(
            step_id,
            "blocked",
            "deploy_in_progress",
            "A testing deploy is queued or running.",
            "wait",
            (queued,),
        )
    if failed:
        code = safe_operation_error_code(str(plan.get("last_failed_error_code") or ""))
        summary = str(plan.get("last_failed_error_summary") or "")
        return _step(
            step_id,
            "blocked",
            code or "deploy_failed",
            summary or "The last testing deploy failed.",
            _testing_failure_fix(code),
            (failed,),
        )
    if _completed_testing_noop(plan):
        return _step(
            step_id,
            "clear",
            "already_deployed",
            "The completed reconcile records matching current and desired testing build provenance.",
            record_ids=(str(plan["current_artifact_id"]),),
        )
    return _step(
        step_id,
        "unknown",
        safe_operation_error_code(str(plan.get("reason") or "")) or "no_deploy_outcome",
        "The last reconcile plan recorded no deploy outcome.",
        "wait",
    )


def _completed_testing_noop(plan: dict[str, object]) -> bool:
    if (
        plan.get("reconcile_state") != "done"
        or plan.get("action") != "none"
        or plan.get("reason") != "already_deployed"
        or plan.get("held") is not False
    ):
        return False
    artifact = plan.get("current_artifact_id")
    commit = plan.get("current_commit")
    digest = plan.get("current_image_digest")
    return (
        isinstance(artifact, str)
        and bool(artifact.strip())
        and artifact != "[redacted]"
        and artifact == plan.get("desired_artifact_id")
        and isinstance(commit, str)
        and re.fullmatch(r"[0-9a-f]{40}", commit) is not None
        and commit == plan.get("desired_commit")
        and isinstance(digest, str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is not None
        and digest == plan.get("desired_image_digest")
    )


def _testing_steps(inputs: PathCheckInputs) -> list[PathCheckStep]:
    steps = [_profile_step(inputs.profile, "testing")]
    hold = inputs.testing_hold_active
    if isinstance(hold, Unread):
        steps.append(_unread("testing_hold", hold))
    elif hold:
        steps.append(
            _step(
                "testing_hold",
                "blocked",
                "testing_hold_active",
                "Site staff hold the testing lane; deploys wait until the hold is lifted.",
                "wait",
            )
        )
    else:
        steps.append(_step("testing_hold", "clear", "no_hold", "The testing lane is not held."))
    steps.append(_testing_attempt_step(inputs.testing_reconcile_plan))
    return steps


def _promotion_grant_step(inputs: PathCheckInputs) -> PathCheckStep:
    allowed = inputs.promotion_allowed
    if isinstance(allowed, Unread):
        return _unread("promotion_grant", allowed)
    if allowed:
        return _step(
            "promotion_grant",
            "clear",
            "caller_may_promote",
            "The caller may start this product's promotion.",
        )
    if inputs.promotion_needs_administrator:
        return _step(
            "promotion_grant",
            "blocked",
            "promotion_needs_signed_in_administrator",
            "Only the signed-in admin can queue this product's prod release.",
            "owner_approval",
        )
    return _step(
        "promotion_grant",
        "blocked",
        "caller_lacks_promotion_grant",
        f"The caller does not hold {inputs.promotion_action} for this product's prod lane.",
        "grant",
    )


def _release_review_step(review: ReleaseReviewStatus | Unread | None) -> PathCheckStep:
    step_id = "client_acceptance"
    if review is None:
        return _unread(step_id, Unread("release_review_unread"))
    if isinstance(review, Unread):
        return _unread(step_id, review)
    if not review.required:
        return _step(
            step_id, "clear", "not_required", "The product is prelaunch; no Client review."
        )
    if review.unavailable_reason:
        return _step(
            step_id,
            "unknown",
            f"release_review_{review.unavailable_reason}",
            "Launchplane could not build the current release checklist.",
            "wait",
        )
    if review.approved:
        return _step(step_id, "clear", "accepted", "The Client accepted the current release.")
    return _step(
        step_id,
        "blocked",
        "client_acceptance_pending",
        "The Client has not accepted the current release checklist.",
        "client_acceptance",
    )


def _backup_step(authority: ProductionBackupAuthorityReadModel | Unread | None) -> PathCheckStep:
    step_id = "backup_authority"
    if authority is None:
        return _unread(step_id, Unread("backup_authority_unread"))
    if isinstance(authority, Unread):
        return _unread(step_id, authority)
    record_ids = (authority.policy.record_id,) if authority.policy is not None else ()
    if authority.ready:
        return _step(
            step_id,
            "clear",
            "backup_ready",
            "The prod lane's backup policy and targets are ready.",
            record_ids=record_ids,
        )
    return _step(
        step_id,
        "blocked",
        f"backup_{authority.state}",
        "The prod lane's backup policy or targets are missing, stale or invalid.",
        "owner_approval",
        record_ids,
    )


def _previous_promotion_step(latest: PromotionRecord | None | Unread) -> PathCheckStep:
    step_id = "previous_promotion"
    if isinstance(latest, Unread):
        return _unread(step_id, latest)
    if latest is None:
        return _step(step_id, "clear", "no_previous_promotion", "No earlier promotion is recorded.")
    if latest.failure is not None:
        # The description comes from the code, never from the stored text.
        code = latest.failure.code
        description = PROMOTION_FAILURE_DESCRIPTIONS.get(code)
        return _step(
            step_id,
            "clear",
            code if description else "previous_promotion_failed",
            f"The last promotion failed: {description or 'Launchplane does not describe its code.'}",
            record_ids=(latest.record_id,),
        )
    return _step(
        step_id,
        "clear",
        f"previous_promotion_{latest.deploy.status}",
        "The last promotion's outcome is recorded.",
        record_ids=(latest.record_id,),
    )


def _promote_steps(inputs: PathCheckInputs) -> list[PathCheckStep]:
    return [
        _profile_step(inputs.profile, "prod"),
        _promotion_grant_step(inputs),
        _release_review_step(inputs.release_review),
        _backup_step(inputs.backup_authority),
        _previous_promotion_step(inputs.latest_promotion),
    ]


def _rollback_steps(inputs: PathCheckInputs) -> list[PathCheckStep]:
    return [
        _profile_step(inputs.profile, "prod"),
        *(
            inputs.rollback_steps
            or (_unread("rollback_target", Unread("rollback_evidence_unread")),)
        ),
    ]


_PATH_STEPS: dict[PathName, Callable[[PathCheckInputs], list[PathCheckStep]]] = {
    "testing": _testing_steps,
    "promote": _promote_steps,
    "rollback": _rollback_steps,
}


def build_product_path_check(
    *, product: str, path: PathName, inputs: PathCheckInputs
) -> ProductPathCheck:
    steps = tuple(_PATH_STEPS[path](inputs))
    blocked = sum(step.state == "blocked" for step in steps)
    unknown = sum(step.state == "unknown" for step in steps)
    state: StepState = "blocked" if blocked else "unknown" if unknown else "clear"
    return ProductPathCheck(
        product=product,
        path=path,
        state=state,
        blocked_count=blocked,
        unknown_count=unknown,
        steps=steps,
    )


ActionAllowed = Callable[[str, str, tuple[str, ...]], bool]
"""Whether the caller holds ``action`` for this product's ``context`` and instances."""


def _read(code: str, read: Callable[[], object]) -> object:
    # A read that fails leaves its step unknown; its error text is never kept.
    try:
        return read()
    except Exception:  # noqa: BLE001 - every failed read becomes a fixed code.
        return Unread(code)


def read_path_check_inputs(
    *,
    path: PathName,
    profile: LaunchplaneProductProfileRecord,
    record_store: object,
    action_allowed: ActionAllowed,
    caller_is_admin: Callable[[], bool],
    read_release_review: Callable[[], ReleaseReviewStatus],
    generated_at: str,
    caller_can_use_generic_rollback: bool = False,
) -> PathCheckInputs:
    """Read the evidence ``path`` needs, each read independent of the others."""
    if path == "testing":
        testing_lane = _lane(profile, "testing")
        if testing_lane is None:
            return PathCheckInputs(profile=profile)
        hold = _read(
            "testing_hold_unread",
            lambda: (
                read_staff_testing_hold(
                    record_store=cast(TestingHoldReader, record_store), context=testing_lane.context
                )
                is not None
            ),
        )
        plan = _read(
            "reconcile_requests_unread", lambda: _testing_reconcile_plan(record_store, profile)
        )
        return PathCheckInputs(
            profile=profile,
            testing_hold_active=cast(bool | Unread, hold),
            testing_reconcile_plan=cast(dict[str, object] | None | Unread, plan),
        )
    prod_lane = _lane(profile, "prod")
    if prod_lane is None:
        return PathCheckInputs(profile=profile)
    if path == "rollback":
        return PathCheckInputs(
            profile=profile,
            rollback_steps=_read_rollback_steps(
                profile=profile,
                prod_lane=prod_lane,
                record_store=record_store,
                action_allowed=action_allowed,
                caller_is_admin=caller_is_admin,
                caller_can_use_generic_rollback=caller_can_use_generic_rollback,
            ),
        )
    odoo = profile.driver_id == "odoo"
    promotion_action = ODOO_PROD_PROMOTION_RUN_ACTION if odoo else GENERIC_WEB_PROMOTION_ACTION
    instances = ("prod",) if odoo else ("testing", "prod")
    backup_action = ODOO_PROMOTION_BACKUP_ACTION if odoo else GENERIC_WEB_PROMOTION_BACKUP_ACTION
    return PathCheckInputs(
        profile=profile,
        promotion_action=promotion_action,
        promotion_needs_administrator=odoo,
        promotion_allowed=cast(
            bool | Unread,
            _read(
                "authorization_unread",
                # The queued Odoo release checks the signed-in admin,
                # not an action grant; generic-web checks the dispatch action.
                caller_is_admin
                if odoo
                else lambda: action_allowed(promotion_action, prod_lane.context, instances),
            ),
        ),
        release_review=cast(
            ReleaseReviewStatus | Unread, _read("release_review_unread", read_release_review)
        ),
        backup_authority=cast(
            ProductionBackupAuthorityReadModel | Unread,
            _read(
                "backup_authority_unread",
                lambda: resolve_production_backup_authority(
                    record_store=require_production_backup_authority_store(record_store),
                    product=profile.product,
                    context=prod_lane.context,
                    instance="prod",
                    promotion_action=backup_action,
                    generated_at=generated_at,
                ),
            ),
        ),
        latest_promotion=cast(
            PromotionRecord | None | Unread,
            _read("promotion_records_unread", lambda: _latest_promotion(record_store, prod_lane)),
        ),
    )


def _testing_reconcile_plan(
    record_store: object, profile: LaunchplaneProductProfileRecord
) -> dict[str, object] | None:
    requests = cast(ProductReconcileRequestReader, record_store).list_product_reconcile_requests(
        product=profile.product
    )
    testing = next((record for record in requests if record.target_kind == "testing"), None)
    if testing is None:
        return None
    return {
        **product_reconcile_request_view(testing).last_plan,
        "reconcile_state": testing.state,
    }


def _latest_promotion(
    record_store: object, prod_lane: ProductLaneProfile
) -> PromotionRecord | None:
    # Storage orders by deploy times, which a source-health failure never sets;
    # the record id starts with its creation time, so the newest id is the latest.
    records = cast(_PromotionRecordLister, record_store).list_promotion_records(
        context_name=prod_lane.context,
        from_instance_name="testing",
        to_instance_name="prod",
        limit=_RECENT_PROMOTIONS,
    )
    return max(records, key=lambda record: record.record_id, default=None)


_RECENT_PROMOTIONS = 50


class _PromotionRecordLister(Protocol):
    def list_promotion_records(
        self,
        *,
        context_name: str = "",
        from_instance_name: str = "",
        to_instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[PromotionRecord, ...]: ...


class _RollbackRecordReader(Protocol):
    def read_environment_inventory(
        self, *, context_name: str, instance_name: str
    ) -> EnvironmentInventory: ...

    def list_deployment_records(
        self,
        *,
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[DeploymentRecord, ...]: ...


def _rollback_grant_step(step_id: str, action: str, allowed: bool | Unread) -> PathCheckStep:
    if isinstance(allowed, Unread):
        return _unread(step_id, allowed)
    return _step(
        step_id,
        "clear" if allowed else "blocked",
        "caller_may_rollback" if allowed else "caller_lacks_rollback_grant",
        f"The caller {'holds' if allowed else 'does not hold'} {action} for the prod lane.",
        "grant",
    )


def _read_rollback_steps(
    *,
    profile: LaunchplaneProductProfileRecord,
    prod_lane: ProductLaneProfile,
    record_store: object,
    action_allowed: ActionAllowed,
    caller_is_admin: Callable[[], bool],
    caller_can_use_generic_rollback: bool,
) -> tuple[PathCheckStep, ...]:
    steps: list[PathCheckStep] = []
    odoo = profile.driver_id == "odoo"
    if odoo:
        allowed = _read("authorization_unread", caller_is_admin)
        if isinstance(allowed, Unread):
            steps.append(_unread("rollback_grant", allowed))
        else:
            steps.append(
                _step(
                    "rollback_grant",
                    "clear" if allowed else "blocked",
                    "caller_may_rollback" if allowed else "rollback_needs_signed_in_administrator",
                    "Only the signed-in admin can queue an Odoo prod rollback.",
                    "owner_approval",
                )
            )
    else:
        descriptor = _read(
            "rollback_driver_unread", lambda: read_driver_descriptor(profile.driver_id)
        )
        if isinstance(descriptor, Unread):
            return (_unread("rollback_route", descriptor),)
        actions = effective_driver_actions(cast(DriverDescriptor, descriptor))
        rollback_action = next(
            (action for action in actions if action.action_id == "prod_rollback"), None
        )
        if rollback_action is None:
            return (
                _step(
                    "rollback_route",
                    "blocked",
                    "rollback_not_supported",
                    "The product's driver has no rollback route.",
                    "code",
                ),
            )
        if rollback_action.route_path != "/v1/drivers/generic-web/prod-rollback":
            return (
                _step(
                    "rollback_route",
                    "unknown",
                    "rollback_driver_unchecked",
                    "This path check does not cover the driver's custom rollback route yet.",
                    "code",
                ),
            )
        action = rollback_action.authz_action
        allowed = cast(
            bool | Unread,
            _read(
                "authorization_unread",
                lambda: action_allowed(action, prod_lane.context, ("prod",)),
            ),
        )
        # Execute revalidates and builds its own plan; a separate plan grant is not needed.
        if not caller_can_use_generic_rollback:
            steps.append(
                _step(
                    "rollback_grant",
                    "blocked",
                    "rollback_identity_not_supported",
                    "Use an authorized local operator/admin bearer credential or GitHub Actions OIDC for rollback.",
                    "by_hand",
                )
            )
        else:
            steps.append(_rollback_grant_step("rollback_grant", action, allowed))
    # An authority refusal must not hide independent target blockers.
    target_steps = _read(
        "rollback_records_unread",
        lambda: _rollback_target_steps(
            profile=profile, prod_lane=prod_lane, record_store=record_store
        ),
    )
    if isinstance(target_steps, Unread):
        steps.append(_unread("rollback_target", target_steps))
    else:
        steps.extend(cast(tuple[PathCheckStep, ...], target_steps))
    return tuple(steps)


def _rollback_target_steps(
    *,
    profile: LaunchplaneProductProfileRecord,
    prod_lane: ProductLaneProfile,
    record_store: object,
) -> tuple[PathCheckStep, ...]:
    reader = cast(_RollbackRecordReader, record_store)
    if profile.driver_id == "odoo":
        try:
            inventory = reader.read_environment_inventory(
                context_name=prod_lane.context, instance_name="prod"
            )
        except FileNotFoundError:
            return (
                _step(
                    "rollback_target",
                    "blocked",
                    "prod_inventory_missing",
                    "The prod lane has no current inventory record.",
                    "by_hand",
                ),
            )
        if inventory.context != prod_lane.context or inventory.instance != "prod":
            return (
                _step(
                    "rollback_target",
                    "blocked",
                    "inventory_scope_mismatch",
                    "The inventory does not match the prod lane.",
                    "code",
                ),
            )
        try:
            target = resolve_odoo_prod_rollback_target(
                record_store=record_store,
                request=OdooProdRollbackRequest(context=prod_lane.context),
            )
        except OdooProdRollbackTargetMissingError:
            return (
                _step(
                    "rollback_target",
                    "blocked",
                    "rollback_target_missing",
                    "No default rollback target is recorded; choose an explicit artifact through the rollback route.",
                    "by_hand",
                ),
            )
        except click.ClickException:
            return (
                _step(
                    "rollback_target",
                    "blocked",
                    "rollback_not_ready",
                    "The recorded rollback prerequisites do not pass target resolution.",
                    "by_hand",
                ),
            )
        return (
            _step(
                "rollback_target",
                "clear",
                "rollback_target_recorded",
                "The previous passing deployment, artifact manifest and current promotion are recorded.",
                record_ids=(
                    target.deployment_record_id,
                    target.artifact_id,
                    inventory.promotion_record_id,
                ),
            ),
        )
    previous = previous_passing_deployment(
        reader.list_deployment_records(
            context_name=prod_lane.context,
            instance_name="prod",
        )
    )
    if previous is None:
        return (
            _step(
                "rollback_target",
                "blocked",
                "rollback_target_missing",
                "No default rollback target is recorded; choose an explicit deployment through the rollback route.",
                "by_hand",
            ),
        )
    record_ids = (previous.record_id,)
    # Build only: execute_generic_web_rollback_plan persists a plan and must not run here.
    plan = build_generic_web_rollback_plan(
        record_store=cast(GenericWebRollbackPlanReader, record_store),
        request=GenericWebRollbackPlanRequest(
            product=profile.product,
            rollback_deployment_record_id=previous.record_id,
        ),
    )
    if plan.blockers:
        return tuple(
            _step(
                f"rollback_target_{blocker.code}",
                "blocked",
                blocker.code,
                _ROLLBACK_BLOCKER_DESCRIPTIONS[blocker.code],
                "by_hand",
                record_ids,
            )
            for blocker in plan.blockers
        )
    return (
        _step(
            "rollback_target",
            "clear",
            "rollback_target_ready",
            "The recorded previous deployment passes rollback planning with an immutable digest.",
            record_ids=record_ids,
        ),
    )


_ROLLBACK_BLOCKER_DESCRIPTIONS = {
    "backup_gate_missing": "The requested backup gate record is missing.",
    "backup_gate_not_passed": "The requested backup gate has not passed.",
    "backup_gate_scope_mismatch": "The backup gate does not match the prod lane.",
    "deployment_scope_mismatch": "The rollback deployment does not match the prod lane.",
    "health_evidence_failed": "The rollback deployment has failed health evidence.",
    "missing_deploy_reference": "The rollback deployment has no immutable provider deploy reference.",
    "missing_rollback_target": "The rollback deployment or its artifact identity is missing.",
    "mutable_artifact_reference": "The rollback deployment must use an immutable image digest.",
    "target_deploy_not_passed": "The rollback deployment did not pass.",
}
