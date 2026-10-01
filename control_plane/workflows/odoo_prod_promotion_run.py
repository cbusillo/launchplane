from __future__ import annotations

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


@dataclass(frozen=True, slots=True)
class OdooProdPromotionRunAdmission:
    """What a promotion run checks before any provider effect."""

    inputs_result: OdooProdPromotionInputsResult
    blocked_reason: str = ""


def admit_odoo_prod_promotion_run(
    *,
    control_plane_root: Path,
    record_store: OdooProdPromotionRunStore,
    request: OdooProdPromotionRunRequest,
) -> OdooProdPromotionRunAdmission:
    """Check ready inputs, release approval, and the verified infrastructure backup."""

    inputs_result = resolve_odoo_prod_promotion_inputs(
        record_store=record_store,
        request=OdooProdPromotionInputsRequest(
            context=request.context,
            from_instance=request.from_instance,
            to_instance=request.to_instance,
            request_id=request.request_id,
        ),
    )
    if inputs_result.input_status != "ready":
        return OdooProdPromotionRunAdmission(
            inputs_result=inputs_result,
            blocked_reason=inputs_result.error_message
            or "Odoo prod promotion inputs are not ready.",
        )
    try:
        require_release_approval(
            control_plane_root=control_plane_root,
            record_store=record_store,
            product=request.product,
            artifact_id=inputs_result.artifact_id,
            source_commit=inputs_result.source_git_ref,
        )
        require_production_promotion_backup(
            record_store=record_store,
            product=request.product,
            context=request.context,
            instance=request.to_instance,
            promotion_action=ODOO_PROMOTION_BACKUP_ACTION,
            backup_record_id=request.infrastructure_backup_record_id,
        )
    except (AttributeError, FileNotFoundError, ValueError, click.ClickException) as error:
        return OdooProdPromotionRunAdmission(
            inputs_result=inputs_result,
            blocked_reason=str(error) or "Odoo prod promotion is not admitted.",
        )
    return OdooProdPromotionRunAdmission(inputs_result=inputs_result)


def execute_odoo_prod_promotion_run(
    *,
    control_plane_root: Path,
    state_dir: Path,
    database_url: str | None,
    record_store: OdooProdPromotionRunStore,
    request: OdooProdPromotionRunRequest,
    phase_checkpoint: Callable[[OdooProdPromotionOperationPhase], None] | None = None,
    provider_effect_checkpoint: Callable[[str], None] | None = None,
) -> OdooProdPromotionRunResult:
    """Run one promotion; the durable worker passes checkpoints, the sync route does not.

    ``provider_effect_checkpoint`` runs before the first provider effect (the logical
    backup) and again before the deploy's effects; ``phase_checkpoint`` records progress
    after each check passes and before the effect it names starts.
    """

    def record_phase(phase: OdooProdPromotionOperationPhase) -> None:
        if phase_checkpoint is not None:
            phase_checkpoint(phase)

    admission = admit_odoo_prod_promotion_run(
        control_plane_root=control_plane_root,
        record_store=record_store,
        request=request,
    )
    inputs_result = admission.inputs_result
    if admission.blocked_reason:
        return _result_from_inputs(
            request=request,
            inputs_result=inputs_result,
            run_status="blocked",
            error_message=admission.blocked_reason,
        )
    record_phase("validated")
    if provider_effect_checkpoint is not None:
        provider_effect_checkpoint("odoo_logical_backup")
    record_phase("logical_backup_started")
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
