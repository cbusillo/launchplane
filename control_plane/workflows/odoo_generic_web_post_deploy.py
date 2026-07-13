from __future__ import annotations

from pathlib import Path

from control_plane.contracts.promotion_record import PostDeployUpdateEvidence
from control_plane.workflows.generic_web_deploy import (
    GenericWebDeployStore,
    GenericWebPostDeployContext,
    GenericWebPostDeployExecutor,
    GenericWebPostDeployGuard,
)
from control_plane.workflows.odoo_post_deploy import (
    OdooPostDeployRequest,
    OdooPostDeployResult,
    execute_odoo_post_deploy,
)


_ODOO_GENERIC_WEB_POST_DEPLOY_DRIVERS = frozenset({"odoo"})


def generic_web_post_deploy_executor_for_driver_id(
    driver_id: str,
) -> GenericWebPostDeployExecutor | None:
    if driver_id.strip() in _ODOO_GENERIC_WEB_POST_DEPLOY_DRIVERS:
        return execute_odoo_generic_web_post_deploy
    return None


def post_deploy_evidence_from_odoo_result(
    result: OdooPostDeployResult,
) -> PostDeployUpdateEvidence:
    return PostDeployUpdateEvidence(
        attempted=True,
        status=result.post_deploy_status,
        detail=(
            result.error_message
            or "Odoo post-deploy completed through the generic-web extension hook."
        ),
        evidence={
            "driver_id": "odoo",
            "context": result.context,
            "instance": result.instance,
            "phase": result.phase,
            "override_status": result.override_status,
            "override_record_found": str(result.override_record_found).lower(),
            "override_payload_rendered": str(result.override_payload_rendered).lower(),
            **result.override_evidence,
        },
    )


def execute_odoo_generic_web_post_deploy(
    control_plane_root: Path,
    record_store: GenericWebDeployStore,
    context: GenericWebPostDeployContext,
    guard: GenericWebPostDeployGuard,
) -> PostDeployUpdateEvidence:
    result = execute_odoo_post_deploy(
        control_plane_root=control_plane_root,
        record_store=record_store,
        request=OdooPostDeployRequest(context=context.context, instance=context.instance),
        provider_effect_checkpoint=guard.checkpoint_effect,
        provider_operation_title=guard.deployment_title,
    )
    return post_deploy_evidence_from_odoo_result(result)
