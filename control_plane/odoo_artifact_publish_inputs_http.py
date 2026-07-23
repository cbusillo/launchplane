from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.odoo_product_driver_http import (
    OdooProductMismatchError,
    OdooRouteDependencyError,
    resolve_odoo_product_route,
)
from control_plane.workflows.odoo_artifact_publish import (
    OdooArtifactPublishInputsDependencyNotFoundError,
    OdooArtifactPublishInputsRequest,
    build_odoo_artifact_publish_inputs,
)


ODOO_ARTIFACT_PUBLISH_INPUTS_ROUTE = "/v1/drivers/odoo/artifact-publish-inputs"


class OdooArtifactPublishInputsRouteDependencyError(ValueError):
    pass


class OdooArtifactPublishInputsProductMismatchError(ValueError):
    pass


class OdooArtifactPublishInputsEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    inputs: OdooArtifactPublishInputsRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "OdooArtifactPublishInputsEnvelope":
        if not self.product.strip():
            raise ValueError("Odoo artifact publish inputs requires product.")
        return self


def resolve_odoo_artifact_publish_inputs_profile(
    *, record_store: object, request: OdooArtifactPublishInputsEnvelope
) -> LaunchplaneProductProfileRecord:
    try:
        return resolve_odoo_product_route(
            record_store=record_store,
            product=request.product,
            context=request.inputs.context,
            instance=request.inputs.instance,
        )
    except OdooRouteDependencyError as error:
        raise OdooArtifactPublishInputsRouteDependencyError from error
    except OdooProductMismatchError as error:
        raise OdooArtifactPublishInputsProductMismatchError from error


def build_odoo_artifact_publish_inputs_result(
    *,
    control_plane_root: Path,
    request: OdooArtifactPublishInputsEnvelope,
    product_profile: LaunchplaneProductProfileRecord,
) -> dict[str, object]:
    try:
        return build_odoo_artifact_publish_inputs(
            control_plane_root=control_plane_root,
            request=request.inputs,
            product_profile=product_profile,
        )
    except OdooArtifactPublishInputsDependencyNotFoundError as error:
        raise OdooArtifactPublishInputsRouteDependencyError from error
