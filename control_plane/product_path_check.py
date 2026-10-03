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
from typing import Literal, Protocol, cast

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
from control_plane.testing_lane_hold import TestingHoldReader, read_staff_testing_hold
from control_plane.workflows.production_promotion_backup import (
    GENERIC_WEB_PROMOTION_BACKUP_ACTION,
    ODOO_PROMOTION_BACKUP_ACTION,
)

# The action the generic-web release panel's live promotion checks.
GENERIC_WEB_PROMOTION_ACTION = "generic_web_prod_promotion.dispatch"

PathName = Literal["testing", "promote"]
PATH_NAMES: tuple[PathName, ...] = ("testing", "promote")
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
    return _step(
        step_id,
        "unknown",
        safe_operation_error_code(str(plan.get("reason") or "")) or "no_deploy_outcome",
        "The last reconcile plan recorded no deploy outcome.",
        "wait",
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


_PATH_STEPS: dict[PathName, Callable[[PathCheckInputs], list[PathCheckStep]]] = {
    "testing": _testing_steps,
    "promote": _promote_steps,
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
    return dict(product_reconcile_request_view(testing).last_plan)


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
