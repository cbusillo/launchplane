from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.drivers.registry import read_driver_descriptor
from control_plane.workflows.odoo_artifact_publish import (
    OdooArtifactPublishInputsDependencyNotFoundError,
    OdooArtifactPublishInputsRequest,
    build_odoo_artifact_publish_inputs,
)


ODOO_ARTIFACT_PUBLISH_INPUTS_ROUTE = "/v1/drivers/odoo/artifact-publish-inputs"
ODOO_ARTIFACT_PUBLISH_INPUTS_ACTION = "odoo_artifact_publish_inputs.read"
ODOO_DRIVER_ID = "odoo"


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


def _product_profile_uses_odoo_driver(profile: LaunchplaneProductProfileRecord) -> bool:
    profile_driver_id = profile.driver_id.strip()
    if profile_driver_id == ODOO_DRIVER_ID:
        return True
    try:
        descriptor = read_driver_descriptor(profile_driver_id)
    except FileNotFoundError:
        return False
    return descriptor.base_driver_id == ODOO_DRIVER_ID


def resolve_odoo_artifact_publish_inputs_profile(
    *, record_store: object, request: OdooArtifactPublishInputsEnvelope
) -> LaunchplaneProductProfileRecord | None:
    normalized_product = request.product.strip()
    if normalized_product == ODOO_DRIVER_ID:
        return None
    read_profile = getattr(record_store, "read_product_profile_record", None)
    if not callable(read_profile):
        raise ValueError("Product driver validation requires product profile storage.")
    try:
        profile = read_profile(normalized_product)
    except FileNotFoundError as error:
        raise OdooArtifactPublishInputsRouteDependencyError from error
    if not isinstance(profile, LaunchplaneProductProfileRecord):
        profile = LaunchplaneProductProfileRecord.model_validate(profile)
    if not _product_profile_uses_odoo_driver(profile):
        raise OdooArtifactPublishInputsProductMismatchError
    if request.inputs.context.strip() or request.inputs.instance.strip():
        for lane in profile.lanes:
            if (
                lane.context.strip() == request.inputs.context.strip()
                and lane.instance.strip() == request.inputs.instance.strip()
            ):
                return profile
        raise OdooArtifactPublishInputsProductMismatchError
    return profile


def build_odoo_artifact_publish_inputs_result(
    *,
    control_plane_root: Path,
    request: OdooArtifactPublishInputsEnvelope,
    product_profile: LaunchplaneProductProfileRecord | None,
) -> dict[str, object]:
    try:
        return build_odoo_artifact_publish_inputs(
            control_plane_root=control_plane_root,
            request=request.inputs,
            product_profile=product_profile,
        )
    except OdooArtifactPublishInputsDependencyNotFoundError as error:
        raise OdooArtifactPublishInputsRouteDependencyError from error
