from __future__ import annotations

from pathlib import Path
from typing import cast

from pydantic import Field, model_validator

from control_plane.contracts.verireel_prod_backup_gate import (
    VeriReelProdBackupGateRequest,
)
from control_plane.drivers.dispatch import (
    _DriverRouteExecutionMetadata,
    _ProductRouteEnvelope,
    _validate_driver_envelope_product,
)
from control_plane.workflows.verireel_prod_backup_gate import (
    VeriReelProdBackupGateOperationStore,
    enqueue_verireel_prod_backup_gate,
)
from control_plane.workflows.verireel_prod_promotion import (
    VeriReelProdPromotionRequest,
    VeriReelProdPromotionStore,
    execute_verireel_prod_promotion,
)
from control_plane.workflows.verireel_prod_rollback import (
    VeriReelProdRollbackRequest,
    VeriReelProdRollbackStore,
    execute_verireel_prod_rollback,
)
from control_plane.workflows.verireel_stable_deploy import (
    VeriReelStableDeployRequest,
    VeriReelStableDeployStore,
    execute_verireel_stable_deploy,
)


VERIREEL_PROD_DEPLOY_ROUTE = "/v1/drivers/verireel/prod-deploy"
VERIREEL_PROD_BACKUP_GATE_ROUTE = "/v1/drivers/verireel/prod-backup-gate"
VERIREEL_PROD_PROMOTION_ROUTE = "/v1/drivers/verireel/prod-promotion"
VERIREEL_PROD_ROLLBACK_ROUTE = "/v1/drivers/verireel/prod-rollback"


class VeriReelProdDeployEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    deploy: VeriReelStableDeployRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "VeriReelProdDeployEnvelope":
        _validate_driver_envelope_product(self.product, label="VeriReel prod deploy")
        if self.deploy.instance != "prod":
            raise ValueError("VeriReel prod deploy requires instance 'prod'.")
        return self


class VeriReelProdBackupGateEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    backup_gate: VeriReelProdBackupGateRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "VeriReelProdBackupGateEnvelope":
        _validate_driver_envelope_product(self.product, label="VeriReel prod backup gate")
        return self


class VeriReelProdPromotionEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    promotion: VeriReelProdPromotionRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "VeriReelProdPromotionEnvelope":
        _validate_driver_envelope_product(self.product, label="VeriReel prod promotion")
        return self


class VeriReelProdRollbackEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    rollback: VeriReelProdRollbackRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "VeriReelProdRollbackEnvelope":
        _validate_driver_envelope_product(self.product, label="VeriReel prod rollback")
        return self


_VERIREEL_PROD_DEPLOY_ROUTE = _DriverRouteExecutionMetadata(
    route_path=VERIREEL_PROD_DEPLOY_ROUTE,
    envelope_model=VeriReelProdDeployEnvelope,
    denial_message=(
        "Workflow cannot execute the VeriReel prod deploy driver for the requested product/context."
    ),
)


_VERIREEL_PROD_BACKUP_GATE_ROUTE = _DriverRouteExecutionMetadata(
    route_path=VERIREEL_PROD_BACKUP_GATE_ROUTE,
    envelope_model=VeriReelProdBackupGateEnvelope,
    denial_message=(
        "Workflow cannot execute the VeriReel prod backup gate driver"
        " for the requested product/context."
    ),
)


_VERIREEL_PROD_PROMOTION_ROUTE = _DriverRouteExecutionMetadata(
    route_path=VERIREEL_PROD_PROMOTION_ROUTE,
    envelope_model=VeriReelProdPromotionEnvelope,
    denial_message=(
        "Workflow cannot execute the VeriReel prod promotion driver"
        " for the requested product/context."
    ),
)


_VERIREEL_PROD_ROLLBACK_ROUTE = _DriverRouteExecutionMetadata(
    route_path=VERIREEL_PROD_ROLLBACK_ROUTE,
    envelope_model=VeriReelProdRollbackEnvelope,
    denial_message=(
        "Workflow cannot execute the VeriReel prod rollback driver"
        " for the requested product/context."
    ),
)


def _result_contains_status(result: dict[str, object], status: str) -> bool:
    return status in {
        str(value).strip()
        for key, value in result.items()
        if key.endswith("_status") or key == "status"
    }


def should_store_verireel_prod_result_idempotency(
    result: dict[str, object], *, skip_pending: bool = False
) -> bool:
    if _result_contains_status(result, "blocked"):
        return False
    if _result_contains_status(result, "fail"):
        return False
    if skip_pending and _result_contains_status(result, "pending"):
        return False
    return True


def apply_verireel_prod_deploy_result(
    *,
    control_plane_root: Path,
    record_store: object,
    request: VeriReelProdDeployEnvelope,
) -> tuple[dict[str, object], dict[str, object]]:
    driver_result = execute_verireel_stable_deploy(
        control_plane_root=control_plane_root,
        record_store=cast(VeriReelStableDeployStore, record_store),
        request=request.deploy,
    )
    result = driver_result.model_dump(mode="json")
    result.pop("target_type", None)
    return {"deployment_record_id": driver_result.deployment_record_id}, result


def apply_verireel_prod_backup_gate_result(
    *,
    record_store: object,
    request: VeriReelProdBackupGateEnvelope,
) -> tuple[dict[str, object], dict[str, object]]:
    driver_result = enqueue_verireel_prod_backup_gate(
        record_store=cast(VeriReelProdBackupGateOperationStore, record_store),
        request=request.backup_gate,
    )
    return (
        {"backup_gate_record_id": driver_result.backup_record_id},
        driver_result.model_dump(mode="json"),
    )


def apply_verireel_prod_promotion_result(
    *,
    control_plane_root: Path,
    record_store: object,
    request: VeriReelProdPromotionEnvelope,
) -> tuple[dict[str, object], dict[str, object]]:
    driver_result = execute_verireel_prod_promotion(
        control_plane_root=control_plane_root,
        record_store=cast(VeriReelProdPromotionStore, record_store),
        request=request.promotion,
    )
    result = driver_result.model_dump(mode="json")
    result.pop("target_type", None)
    return (
        {
            "promotion_record_id": driver_result.promotion_record_id,
            "deployment_record_id": driver_result.deployment_record_id,
        },
        result,
    )


def apply_verireel_prod_rollback_result(
    *,
    control_plane_root: Path,
    record_store: object,
    request: VeriReelProdRollbackEnvelope,
) -> tuple[dict[str, object], dict[str, object]]:
    driver_result = execute_verireel_prod_rollback(
        control_plane_root=control_plane_root,
        record_store=cast(VeriReelProdRollbackStore, record_store),
        request=request.rollback,
    )
    return (
        {
            "promotion_record_id": driver_result.promotion_record_id,
            "backup_record_id": driver_result.backup_record_id,
        },
        driver_result.model_dump(mode="json"),
    )
