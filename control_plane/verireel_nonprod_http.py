from __future__ import annotations

from pathlib import Path
from typing import cast

from pydantic import Field, model_validator

from control_plane.drivers.dispatch import (
    _DriverRouteExecutionMetadata,
    _ProductRouteEnvelope,
    _validate_driver_envelope_product,
)
from control_plane.workflows.verireel_app_maintenance import (
    VeriReelAppMaintenanceRequest,
    execute_verireel_app_maintenance,
)
from control_plane.workflows.verireel_stable_deploy import (
    VeriReelStableDeployRequest,
    VeriReelStableDeployStore,
    execute_verireel_stable_deploy,
)


VERIREEL_TESTING_DEPLOY_ROUTE = "/v1/drivers/verireel/testing-deploy"
VERIREEL_APP_MAINTENANCE_ROUTE = "/v1/drivers/verireel/app-maintenance"


class VeriReelTestingDeployEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    deploy: VeriReelStableDeployRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "VeriReelTestingDeployEnvelope":
        _validate_driver_envelope_product(self.product, label="VeriReel testing deploy")
        if self.deploy.instance != "testing":
            raise ValueError("VeriReel testing deploy requires instance 'testing'.")
        return self


class VeriReelAppMaintenanceEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    maintenance: VeriReelAppMaintenanceRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "VeriReelAppMaintenanceEnvelope":
        _validate_driver_envelope_product(self.product, label="VeriReel app maintenance")
        return self


_VERIREEL_TESTING_DEPLOY_ROUTE = _DriverRouteExecutionMetadata(
    route_path=VERIREEL_TESTING_DEPLOY_ROUTE,
    envelope_model=VeriReelTestingDeployEnvelope,
    denial_message=(
        "Workflow cannot execute the VeriReel testing deploy driver"
        " for the requested product/context."
    ),
)


_VERIREEL_APP_MAINTENANCE_ROUTE = _DriverRouteExecutionMetadata(
    route_path=VERIREEL_APP_MAINTENANCE_ROUTE,
    envelope_model=VeriReelAppMaintenanceEnvelope,
    denial_message=(
        "Workflow cannot execute the VeriReel app maintenance driver"
        " for the requested product/context."
    ),
)


def apply_verireel_testing_deploy_result(
    *,
    control_plane_root: Path,
    record_store: object,
    request: VeriReelTestingDeployEnvelope,
) -> tuple[dict[str, object], dict[str, object]]:
    driver_result = execute_verireel_stable_deploy(
        control_plane_root=control_plane_root,
        record_store=cast(VeriReelStableDeployStore, record_store),
        request=request.deploy,
    )
    result = driver_result.model_dump(mode="json")
    result.pop("target_type", None)
    return {"deployment_record_id": driver_result.deployment_record_id}, result


def apply_verireel_app_maintenance_result(
    *,
    control_plane_root: Path,
    request: VeriReelAppMaintenanceEnvelope,
) -> tuple[dict[str, object], dict[str, object]]:
    driver_result = execute_verireel_app_maintenance(
        control_plane_root=control_plane_root,
        request=request.maintenance,
    )
    return {}, driver_result.model_dump(mode="json")
