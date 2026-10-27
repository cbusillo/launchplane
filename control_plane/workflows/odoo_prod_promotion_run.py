from __future__ import annotations

from control_plane.contracts.release_review import ReleaseReviewDecisionRecord

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

import click

from control_plane.contracts.odoo_prod_promotion_operation import (
    OdooProdPromotionOperationPhase,
    OdooProdPromotionRunRequest as OdooProdPromotionRunRequest,
    OdooProdPromotionRunResult as OdooProdPromotionRunResult,
)

from control_plane.release_review import require_release_approval
from control_plane.workflows.production_promotion_backup import (
    ODOO_PROMOTION_BACKUP_ACTION,
    require_production_promotion_backup,
)

from control_plane.workflows.odoo_prod_backup_gate import (
    OdooProdBackupGateResult,
    OdooProdBackupGateStore,
    OdooProdBackupGateRequest,
    execute_odoo_prod_backup_gate,
)
from control_plane.workflows.odoo_cutover import OdooCutoverProvider
from control_plane.contracts.odoo_cutover import OdooCutoverPlan, ODOO_CUTOVER_TERMINAL_STEPS
from control_plane.odoo_cutover_plan import (
    decision_bound_cutover_plan,
    resolve_profile_cutover_plan,
    persisted_cutover,
)

from control_plane.workflows.odoo_prod_promotion import (
    OdooProdPromotionResult,
    OdooProdPromotionStore,
    OdooProdPromotionRequest,
    execute_odoo_prod_promotion,
)
from control_plane.workflows.odoo_prod_promotion_inputs import (
    OdooProdPromotionInputsRequest,
    OdooProdPromotionInputsResult,
    OdooProdPromotionInputsStore,
    resolve_odoo_prod_promotion_inputs,
)


class OdooProdPromotionRunStore(
    OdooProdPromotionInputsStore,
    OdooProdBackupGateStore,
    OdooProdPromotionStore,
    Protocol,
):
    pass


# The two effects a queued promotion's authority is re-read before, even after
# an earlier effect: the logical backup, then the deploy.
ODOO_LOGICAL_BACKUP_EFFECT = "odoo_logical_backup"
ODOO_PROD_DEPLOY_EFFECT = "odoo_prod_deploy"


@dataclass(frozen=True, slots=True)
class OdooProdPromotionRunAdmission:
    """What a promotion run checks before any provider effect."""

    inputs_result: OdooProdPromotionInputsResult
    blocked_reason: str = ""
    release_decision: ReleaseReviewDecisionRecord | None = None


def admit_odoo_prod_promotion_run(
    *,
    control_plane_root: Path,
    record_store: OdooProdPromotionRunStore,
    request: OdooProdPromotionRunRequest,
    release_decision: ReleaseReviewDecisionRecord | None = None,
    continuation_plan: OdooCutoverPlan | None = None,
) -> OdooProdPromotionRunAdmission:
    """Check ready inputs, release approval, and the verified infrastructure backup."""

    original_promotion = None
    if continuation_plan is not None:
        from control_plane.odoo_cutover_plan import cutover_record_id

        original_promotion = record_store.read_promotion_record(
            "promotion-cutover-" + cutover_record_id(continuation_plan)
        )
        if (
            original_promotion.context,
            original_promotion.from_instance,
            original_promotion.to_instance,
            original_promotion.artifact_identity.artifact_id,
        ) != (
            request.context,
            request.from_instance,
            request.to_instance,
            continuation_plan.candidate.artifact_id,
        ):
            raise ValueError("continuation lacks its original promotion and logical backup binding")
    inputs_result = (
        OdooProdPromotionInputsResult(
            context=request.context,
            from_instance=request.from_instance,
            to_instance=request.to_instance,
            request_id=request.request_id,
            input_status="ready",
            artifact_id=continuation_plan.candidate.artifact_id,
            source_git_ref=continuation_plan.candidate.source_commit,
            backup_record_id=original_promotion.backup_record_id
            if original_promotion is not None
            else "",
            image_repository=continuation_plan.candidate.image.repository,
            image_digest=continuation_plan.candidate.image.digest,
        )
        if continuation_plan is not None
        else resolve_odoo_prod_promotion_inputs(
            record_store=record_store,
            request=OdooProdPromotionInputsRequest(
                context=request.context,
                from_instance=request.from_instance,
                to_instance=request.to_instance,
                request_id=request.request_id,
            ),
        )
    )
    if inputs_result.input_status != "ready":
        return OdooProdPromotionRunAdmission(
            inputs_result=inputs_result,
            blocked_reason=inputs_result.error_message
            or "Odoo prod promotion inputs are not ready.",
        )
    if request.expected_artifact_id and request.expected_artifact_id != inputs_result.artifact_id:
        return OdooProdPromotionRunAdmission(
            inputs_result=inputs_result,
            blocked_reason="Testing no longer carries the accepted release; a new decision is required.",
        )
    try:
        if continuation_plan is not None:
            recovery = persisted_cutover(record_store, continuation_plan)
            if (
                recovery is None
                or continuation_plan.release_id != request.request_id
                or continuation_plan.new.binding.product_id != request.product
                or continuation_plan.new.runtime_identity.context != request.context
                or continuation_plan.new.binding.environment_id != request.to_instance
            ):
                raise ValueError("continuation requires its exact persisted admitted cutover")
        current_decision = (
            release_decision
            if continuation_plan is not None
            else require_release_approval(
                control_plane_root=control_plane_root,
                record_store=record_store,
                product=request.product,
                artifact_id=inputs_result.artifact_id,
                source_commit=inputs_result.source_git_ref,
            )
        )
        release_decision = release_decision or current_decision
        admitted_plan = resolve_profile_cutover_plan(
            record_store,
            product=request.product,
            context=request.context,
            instance=request.to_instance,
            artifact_id=inputs_result.artifact_id,
            release_id=request.request_id,
        )
        if continuation_plan is not None and admitted_plan is None:
            raise ValueError("continuation lost its admitted two-slot lane")
        if admitted_plan is not None:
            admitted_plan = decision_bound_cutover_plan(
                admitted_plan,
                release_decision,
                stored=persisted_cutover(record_store, admitted_plan) is not None,
            )
            if continuation_plan is not None and admitted_plan != continuation_plan:
                raise ValueError("continuation crossed its original admitted plan")
        recovery = persisted_cutover(record_store, admitted_plan)
        if admitted_plan is not None and any(
            item.odoo_cutover is not None
            and item.odoo_cutover.step not in ODOO_CUTOVER_TERMINAL_STEPS
            and item.odoo_cutover.plan != admitted_plan
            for item in record_store.list_deployment_records(
                context_name=request.context, instance_name=request.to_instance
            )
        ):
            raise click.ClickException(
                "Another unfinished cutover must be recovered before this release."
            )
        require_production_promotion_backup(
            record_store=record_store,
            product=request.product,
            context=request.context,
            instance=request.to_instance,
            promotion_action=ODOO_PROMOTION_BACKUP_ACTION,
            backup_record_id=request.infrastructure_backup_record_id,
            cutover_recovery=recovery,
        )
    except (AttributeError, FileNotFoundError, ValueError, click.ClickException) as error:
        return OdooProdPromotionRunAdmission(
            inputs_result=inputs_result,
            blocked_reason=str(error) or "Odoo prod promotion is not admitted.",
        )
    return OdooProdPromotionRunAdmission(
        inputs_result=inputs_result, release_decision=release_decision
    )


def execute_odoo_prod_promotion_run(
    *,
    control_plane_root: Path,
    state_dir: Path,
    database_url: str | None,
    record_store: OdooProdPromotionRunStore,
    request: OdooProdPromotionRunRequest,
    phase_checkpoint: Callable[[OdooProdPromotionOperationPhase], None] | None = None,
    provider_effect_checkpoint: Callable[[str], None] | None = None,
    hold_uncertain_effects: bool = False,
    cutover_dispatch_guard: Callable[[], None] | None = None,
    continuation_plan: OdooCutoverPlan | None = None,
    cutover_provider_factory: Callable[[OdooCutoverPlan], OdooCutoverProvider] | None = None,
    release_decision: ReleaseReviewDecisionRecord | None = None,
) -> OdooProdPromotionRunResult:
    """Run one promotion; the durable worker passes checkpoints, the sync route does not.

    ``provider_effect_checkpoint`` runs before the logical backup, before the deploy
    starts, and again before the deploy's own effects; ``phase_checkpoint`` records
    progress after each check passes and before the effect it names starts.
    """

    def record_phase(phase: OdooProdPromotionOperationPhase) -> None:
        if phase_checkpoint is not None:
            phase_checkpoint(phase)

    admission = admit_odoo_prod_promotion_run(
        control_plane_root=control_plane_root,
        record_store=record_store,
        request=request,
        release_decision=release_decision,
        continuation_plan=continuation_plan,
    )
    release_decision = admission.release_decision
    inputs_result = admission.inputs_result
    if admission.blocked_reason:
        from control_plane.odoo_cutover_plan import cutover_identity

        try:
            prior = record_store.read_deployment_record(
                "deployment-cutover-"
                + cutover_identity(
                    request.product, request.context, request.to_instance, request.request_id
                )
            )
        except FileNotFoundError:
            prior = None
        from control_plane.contracts.deployment_record import DeploymentRecord

        if (
            isinstance(prior, DeploymentRecord)
            and prior.odoo_cutover is not None
            and prior.odoo_cutover.step not in ODOO_CUTOVER_TERMINAL_STEPS
        ):
            raise RuntimeError("Cutover remains unfinished; retain its recovery hold.")
        return _result_from_inputs(
            request=request,
            inputs_result=inputs_result,
            run_status="blocked",
            error_message=admission.blocked_reason,
        )
    cutover_provider = None
    cutover_plan = resolve_profile_cutover_plan(
        record_store,
        product=request.product,
        context=request.context,
        instance=request.to_instance,
        artifact_id=inputs_result.artifact_id,
        release_id=request.request_id,
    )
    if cutover_plan is not None:
        if cutover_provider_factory is None:
            return _result_from_inputs(
                request=request,
                inputs_result=inputs_result,
                run_status="blocked",
                error_message="Two-slot release requires an activated lane-bound provider; in-place fallback is refused.",
            )
        try:
            cutover_plan = decision_bound_cutover_plan(
                cutover_plan,
                release_decision,
                stored=persisted_cutover(record_store, cutover_plan) is not None,
            )
            cutover_provider = cutover_provider_factory(cutover_plan)
            cutover_provider.preflight(cutover_plan)
        except ValueError as error:
            return _result_from_inputs(
                request=request,
                inputs_result=inputs_result,
                run_status="blocked",
                error_message=str(error),
            )
    record_phase("validated")
    if provider_effect_checkpoint is not None:
        provider_effect_checkpoint(ODOO_LOGICAL_BACKUP_EFFECT)
    record_phase("logical_backup_started")
    original_backup = None
    if cutover_plan is not None:
        from control_plane.odoo_cutover_plan import cutover_record_id

        try:
            previous = record_store.read_deployment_record(
                "deployment-cutover-" + cutover_record_id(cutover_plan)
            )
            if previous.odoo_cutover is not None:
                original_backup = record_store.read_backup_gate_record(
                    inputs_result.backup_record_id
                )
        except FileNotFoundError:
            pass
    if original_backup is not None:
        from control_plane.workflows.odoo_prod_backup_gate import BACKUP_GATE_SOURCE

        if (
            original_backup.status != "pass"
            or original_backup.source != BACKUP_GATE_SOURCE
            or original_backup.context != request.context
            or original_backup.instance != request.to_instance
        ):
            raise ValueError("cutover resume has no original passing logical backup gate")
        backup_result = OdooProdBackupGateResult(
            context=request.context,
            instance=request.to_instance,
            backup_record_id=original_backup.record_id,
            backup_status="pass",
        )
    else:
        if continuation_plan is not None:
            raise ValueError("continuation lacks its original ordinary logical backup gate")
        backup_result = execute_odoo_prod_backup_gate(
            control_plane_root=control_plane_root,
            record_store=record_store,
            request=OdooProdBackupGateRequest(
                context=request.context,
                instance=request.to_instance,
                backup_record_id=inputs_result.backup_record_id,
                timeout_seconds=request.backup_timeout_seconds,
            ),
        )
    if backup_result.backup_status != "pass":
        return _result_from_inputs(
            request=request,
            inputs_result=inputs_result,
            backup_result=backup_result,
            run_status="fail",
            error_message=backup_result.error_message or "Odoo prod backup gate failed.",
        )

    record_phase("logical_backup_completed")
    if provider_effect_checkpoint is not None:
        provider_effect_checkpoint(ODOO_PROD_DEPLOY_EFFECT)
    record_phase("promotion_started")
    promotion_result = execute_odoo_prod_promotion(
        control_plane_root=control_plane_root,
        state_dir=state_dir,
        database_url=database_url,
        record_store=record_store,
        request=OdooProdPromotionRequest(
            context=request.context,
            from_instance=request.from_instance,
            to_instance=request.to_instance,
            product=request.product,
            artifact_id=inputs_result.artifact_id,
            backup_record_id=inputs_result.backup_record_id,
            source_git_ref=inputs_result.source_git_ref,
            infrastructure_backup_record_id=request.infrastructure_backup_record_id,
            wait=request.wait,
            timeout_seconds=request.promotion_timeout_seconds,
            verify_health=request.verify_health,
            health_timeout_seconds=request.health_timeout_seconds,
            no_cache=request.no_cache,
        ),
        provider_effect_checkpoint=provider_effect_checkpoint,
        cutover_dispatch_guard=cutover_dispatch_guard,
        hold_uncertain_effects=hold_uncertain_effects,
        cutover_provider=cutover_provider,
        cutover_plan=cutover_plan,
        release_decision=release_decision,
    )
    run_status: Literal["pass", "fail"] = (
        "pass"
        if promotion_result.promotion_status == "pass"
        and promotion_result.destination_health_status in {"pass", "skipped"}
        else "fail"
    )
    error_message = promotion_result.error_message
    if run_status == "fail" and not error_message:
        error_message = (
            "Odoo prod promotion did not finish with passing promotion and health status."
        )
    return _result_from_inputs(
        request=request,
        inputs_result=inputs_result,
        backup_result=backup_result,
        promotion_result=promotion_result,
        run_status=run_status,
        error_message=error_message,
    )


def _result_from_inputs(
    *,
    request: OdooProdPromotionRunRequest,
    inputs_result: OdooProdPromotionInputsResult,
    run_status: Literal["pass", "fail", "blocked"],
    backup_result: OdooProdBackupGateResult | None = None,
    promotion_result: OdooProdPromotionResult | None = None,
    error_message: str = "",
) -> OdooProdPromotionRunResult:
    promotion_status: Literal["pass", "fail", "skipped"] = "skipped"
    deployment_status: Literal["pending", "pass", "fail", "skipped"] = "skipped"
    post_deploy_status: Literal["pending", "pass", "fail", "skipped"] = "skipped"
    destination_health_status: Literal["pending", "pass", "fail", "skipped"] = "skipped"
    if promotion_result is not None:
        promotion_status = cast(
            Literal["pass", "fail", "skipped"], promotion_result.promotion_status
        )
        deployment_status = promotion_result.deployment_status
        post_deploy_status = promotion_result.post_deploy_status
        destination_health_status = promotion_result.destination_health_status
    return OdooProdPromotionRunResult(
        context=request.context,
        from_instance=request.from_instance,
        to_instance=request.to_instance,
        request_id=request.request_id,
        run_status=run_status,
        input_status=inputs_result.input_status,
        backup_status=backup_result.backup_status if backup_result is not None else "skipped",
        promotion_status=promotion_status,
        deployment_status=deployment_status,
        post_deploy_status=post_deploy_status,
        destination_health_status=destination_health_status,
        artifact_id=inputs_result.artifact_id,
        source_git_ref=inputs_result.source_git_ref,
        backup_record_id=inputs_result.backup_record_id,
        infrastructure_backup_record_id=request.infrastructure_backup_record_id,
        promotion_record_id=(
            promotion_result.promotion_record_id if promotion_result is not None else ""
        ),
        deployment_record_id=(
            promotion_result.deployment_record_id if promotion_result is not None else ""
        ),
        release_tuple_id=(
            promotion_result.release_tuple_id
            if promotion_result is not None
            else inputs_result.release_tuple_id
        ),
        image_repository=inputs_result.image_repository,
        image_digest=inputs_result.image_digest,
        error_message=error_message,
    )
