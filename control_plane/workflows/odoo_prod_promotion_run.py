from __future__ import annotations

from pathlib import Path
from typing import Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

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


class OdooProdPromotionRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str
    from_instance: str = "testing"
    to_instance: str = "prod"
    request_id: str
    backup_timeout_seconds: int | None = Field(default=None, ge=1)
    promotion_timeout_seconds: int | None = Field(default=None, ge=1)
    health_timeout_seconds: int | None = Field(default=None, ge=1)
    wait: bool = True
    verify_health: bool = True
    no_cache: bool = False

    @model_validator(mode="after")
    def _validate_request(self) -> "OdooProdPromotionRunRequest":
        self.context = self.context.strip().lower()
        self.from_instance = self.from_instance.strip().lower()
        self.to_instance = self.to_instance.strip().lower()
        self.request_id = self.request_id.strip()
        if not self.context:
            raise ValueError("Odoo prod promotion run requires context.")
        if self.from_instance != "testing" or self.to_instance != "prod":
            raise ValueError("Odoo prod promotion run requires testing -> prod.")
        if not self.request_id:
            raise ValueError("Odoo prod promotion run requires request_id.")
        if self.verify_health and not self.wait:
            raise ValueError("Odoo prod promotion run health verification requires wait=true.")
        return self


class OdooProdPromotionRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: str
    from_instance: str
    to_instance: str
    request_id: str
    run_status: Literal["pass", "fail", "blocked"]
    input_status: Literal["ready", "blocked"]
    backup_status: Literal["pass", "fail", "skipped"] = "skipped"
    promotion_status: Literal["pass", "fail", "skipped"] = "skipped"
    deployment_status: Literal["pending", "pass", "fail", "skipped"] = "skipped"
    post_deploy_status: Literal["pending", "pass", "fail", "skipped"] = "skipped"
    destination_health_status: Literal["pending", "pass", "fail", "skipped"] = "skipped"
    artifact_id: str = ""
    source_git_ref: str = ""
    backup_record_id: str = ""
    promotion_record_id: str = ""
    deployment_record_id: str = ""
    release_tuple_id: str = ""
    image_repository: str = ""
    image_digest: str = ""
    error_message: str = ""


def execute_odoo_prod_promotion_run(
    *,
    control_plane_root: Path,
    state_dir: Path,
    database_url: str | None,
    record_store: OdooProdPromotionRunStore,
    request: OdooProdPromotionRunRequest,
) -> OdooProdPromotionRunResult:
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
        return _result_from_inputs(
            request=request,
            inputs_result=inputs_result,
            run_status="blocked",
            error_message=inputs_result.error_message,
        )

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

    promotion_result = execute_odoo_prod_promotion(
        control_plane_root=control_plane_root,
        state_dir=state_dir,
        database_url=database_url,
        record_store=record_store,
        request=OdooProdPromotionRequest(
            context=request.context,
            from_instance=request.from_instance,
            to_instance=request.to_instance,
            artifact_id=inputs_result.artifact_id,
            backup_record_id=inputs_result.backup_record_id,
            source_git_ref=inputs_result.source_git_ref,
            wait=request.wait,
            timeout_seconds=request.promotion_timeout_seconds,
            verify_health=request.verify_health,
            health_timeout_seconds=request.health_timeout_seconds,
            no_cache=request.no_cache,
        ),
    )
    run_status: Literal["pass", "fail"] = (
        "pass"
        if promotion_result.promotion_status == "pass"
        and promotion_result.destination_health_status == "pass"
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
