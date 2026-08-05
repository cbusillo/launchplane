from __future__ import annotations

from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, model_validator

from control_plane.contracts.generic_web_rollback import (
    GenericWebRollbackBlocker,
    GenericWebRollbackPlanRequest,
    GenericWebRollbackPlanStore,
    build_generic_web_rollback_plan,
)
from control_plane.workflows.generic_web_deploy import (
    GenericWebDeployRequest,
    GenericWebDeployStore,
    GenericWebPostDeployExecutor,
    execute_generic_web_deploy,
)


class GenericWebRollbackApplyStore(GenericWebRollbackPlanStore, GenericWebDeployStore, Protocol):
    pass


class GenericWebRollbackApplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str
    deployment_record_id: str = ""
    rollback_status: Literal["pass", "fail", "blocked"]
    deploy_status: Literal["pass", "fail", "skipped"] = "skipped"
    post_deploy_status: Literal["pass", "fail", "skipped"] = "skipped"
    product: str
    context: str
    instance: str
    rollback_deployment_record_id: str
    blockers: tuple[GenericWebRollbackBlocker, ...] = ()
    error_message: str = ""

    @model_validator(mode="after")
    def _validate_result(self) -> "GenericWebRollbackApplyResult":
        self.plan_id = _required_text(
            self.plan_id, "generic web rollback apply result requires plan_id"
        )
        self.product = _required_text(
            self.product, "generic web rollback apply result requires product"
        )
        self.context = _required_text(
            self.context, "generic web rollback apply result requires context"
        )
        self.instance = _required_text(
            self.instance, "generic web rollback apply result requires instance"
        )
        self.rollback_deployment_record_id = _required_text(
            self.rollback_deployment_record_id,
            "generic web rollback apply result requires rollback_deployment_record_id",
        )
        self.deployment_record_id = self.deployment_record_id.strip()
        self.error_message = self.error_message.strip()
        if self.rollback_status == "pass" and not self.deployment_record_id:
            raise ValueError("passing generic web rollback apply requires deployment_record_id")
        if self.rollback_status == "blocked" and not self.blockers:
            raise ValueError("blocked generic web rollback apply requires blockers")
        if self.rollback_status != "blocked" and self.blockers:
            raise ValueError("non-blocked generic web rollback apply cannot include blockers")
        return self


def execute_generic_web_rollback(
    *,
    control_plane_root: Path,
    record_store: GenericWebRollbackApplyStore,
    request: GenericWebRollbackPlanRequest,
    post_deploy_executor: GenericWebPostDeployExecutor | None = None,
) -> GenericWebRollbackApplyResult:
    plan = build_generic_web_rollback_plan(record_store=record_store, request=request)
    record_store.write_generic_web_rollback_plan_record(plan)
    if plan.status == "blocked" or plan.planned_deploy is None:
        return GenericWebRollbackApplyResult(
            plan_id=plan.plan_id,
            rollback_status="blocked",
            product=plan.product,
            context=plan.context,
            instance=plan.instance,
            rollback_deployment_record_id=plan.rollback_deployment_record_id,
            blockers=plan.blockers,
        )
    planned_deploy = plan.planned_deploy
    deploy_result = execute_generic_web_deploy(
        control_plane_root=control_plane_root,
        record_store=record_store,
        request=GenericWebDeployRequest(
            product=planned_deploy.product,
            instance=planned_deploy.instance,
            artifact_id=planned_deploy.artifact_id,
            deploy_reference=planned_deploy.deploy_reference,
            source_git_ref=planned_deploy.source_git_ref,
            timeout_seconds=planned_deploy.timeout_seconds,
            no_cache=planned_deploy.no_cache,
        ),
        post_deploy_executor=post_deploy_executor,
    )
    rollback_status: Literal["pass", "fail"] = (
        "pass"
        if deploy_result.deploy_status == "pass"
        and deploy_result.post_deploy_status in {"pass", "skipped"}
        else "fail"
    )
    return GenericWebRollbackApplyResult(
        plan_id=plan.plan_id,
        deployment_record_id=deploy_result.deployment_record_id,
        rollback_status=rollback_status,
        deploy_status=deploy_result.deploy_status,
        post_deploy_status=deploy_result.post_deploy_status,
        product=plan.product,
        context=plan.context,
        instance=plan.instance,
        rollback_deployment_record_id=plan.rollback_deployment_record_id,
        error_message=deploy_result.error_message,
    )


def _required_text(value: str, message: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(message)
    return normalized
