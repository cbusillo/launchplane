from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.odoo_instance_override_record import (
    OdooConfigParameterOverride,
    OdooInstanceOverrideRecord,
    OdooOverrideValue,
    OdooWebsiteBootstrapPayload,
    validate_odoo_website_bootstrap_contract,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.drivers.dispatch import (
    _ProductRouteEnvelope,
    _validate_driver_envelope_product,
)
from control_plane.drivers.registry import read_driver_descriptor
from control_plane.workflows.odoo_post_deploy import (
    OdooPostDeployRequest,
    execute_odoo_post_deploy,
)


ODOO_POST_DEPLOY_ROUTE = "/v1/drivers/odoo/post-deploy"
ODOO_CONFIG_PARAMETER_OVERRIDE_ROUTE = "/v1/drivers/odoo/config-parameter-override"
ODOO_WEBSITE_BOOTSTRAP_OVERRIDE_ROUTE = "/v1/drivers/odoo/website-bootstrap-override"
ODOO_POST_DEPLOY_ACTION = "odoo_post_deploy.execute"
ODOO_CONFIG_PARAMETER_OVERRIDE_ACTION = "odoo_config_parameter_override.write"
ODOO_WEBSITE_BOOTSTRAP_OVERRIDE_ACTION = "odoo_website_bootstrap_override.write"
ODOO_DRIVER_ID = "odoo"


class OdooPostDeployProductMismatchError(ValueError):
    pass


class OdooPostDeployRouteDependencyError(ValueError):
    pass


class OdooPostDeployEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    post_deploy: OdooPostDeployRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "OdooPostDeployEnvelope":
        _validate_driver_envelope_product(self.product, label="Odoo post-deploy")
        return self


class OdooConfigParameterOverrideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    context: str
    instance: str
    key: str
    value: str
    source_label: str = "launchplane-service"

    @model_validator(mode="after")
    def _validate_request(self) -> "OdooConfigParameterOverrideRequest":
        self.product = self.product.strip()
        self.context = self.context.strip().lower()
        self.instance = self.instance.strip().lower()
        self.key = self.key.strip().lower()
        self.source_label = self.source_label.strip() or "launchplane-service"
        if not self.product:
            raise ValueError("Odoo config-parameter override requires product.")
        if not self.context:
            raise ValueError("Odoo config-parameter override requires context.")
        if not self.instance:
            raise ValueError("Odoo config-parameter override requires instance.")
        if not self.key:
            raise ValueError("Odoo config-parameter override requires key.")
        if self.key != "web.base.url":
            raise ValueError("Only web.base.url overrides are currently service-writable.")
        if not self.value.strip():
            raise ValueError("Odoo config-parameter override requires value.")
        return self


class OdooWebsiteBootstrapOverrideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    context: str
    instance: str
    website_bootstrap: OdooWebsiteBootstrapPayload
    source_label: str = "launchplane-service"

    @model_validator(mode="after")
    def _validate_request(self) -> "OdooWebsiteBootstrapOverrideRequest":
        self.product = self.product.strip()
        self.context = self.context.strip().lower()
        self.instance = self.instance.strip().lower()
        self.source_label = self.source_label.strip() or "launchplane-service"
        if not self.product:
            raise ValueError("Odoo website-bootstrap override requires product.")
        if not self.context:
            raise ValueError("Odoo website-bootstrap override requires context.")
        if not self.instance:
            raise ValueError("Odoo website-bootstrap override requires instance.")
        self.website_bootstrap = validate_odoo_website_bootstrap_contract(self.website_bootstrap)
        return self


class OdooConfigParameterOverrideEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    override: OdooConfigParameterOverrideRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "OdooConfigParameterOverrideEnvelope":
        _validate_driver_envelope_product(self.product, label="Odoo config-parameter override")
        if self.product.strip() != self.override.product.strip():
            raise ValueError("Odoo config-parameter override requires matching product values.")
        return self


class OdooWebsiteBootstrapOverrideEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    override: OdooWebsiteBootstrapOverrideRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "OdooWebsiteBootstrapOverrideEnvelope":
        _validate_driver_envelope_product(self.product, label="Odoo website-bootstrap override")
        if self.product.strip() != self.override.product.strip():
            raise ValueError("Odoo website-bootstrap override requires matching product values.")
        return self


class OdooInstanceOverrideStore(Protocol):
    def read_odoo_instance_override_record(
        self, *, context_name: str, instance_name: str
    ) -> OdooInstanceOverrideRecord: ...

    def write_odoo_instance_override_record(self, record: OdooInstanceOverrideRecord) -> object: ...


def resolve_odoo_post_deploy_product_route(
    *,
    record_store: object,
    product: str,
    context: str = "",
    instance: str = "",
) -> LaunchplaneProductProfileRecord | None:
    normalized_product = product.strip()
    if normalized_product == ODOO_DRIVER_ID:
        return None
    read_profile = getattr(record_store, "read_product_profile_record", None)
    if not callable(read_profile):
        raise ValueError("Product driver validation requires product profile storage.")
    try:
        profile = read_profile(normalized_product)
    except FileNotFoundError as error:
        raise OdooPostDeployRouteDependencyError from error
    if not isinstance(profile, LaunchplaneProductProfileRecord):
        profile = LaunchplaneProductProfileRecord.model_validate(profile)
    if not _product_profile_uses_odoo_driver(profile):
        raise OdooPostDeployProductMismatchError(
            "Product is not configured for the requested Odoo driver route."
        )
    if context.strip() or instance.strip():
        for lane in profile.lanes:
            if (not context.strip() or lane.context.strip() == context.strip()) and (
                not instance.strip() or lane.instance.strip() == instance.strip()
            ):
                return profile
        raise OdooPostDeployProductMismatchError(
            "Product profile does not own the requested Odoo driver lane."
        )
    return profile


def execute_odoo_post_deploy_result(
    *,
    control_plane_root: Path,
    record_store: object,
    request: OdooPostDeployEnvelope,
) -> tuple[dict[str, object], dict[str, object]]:
    driver_result = execute_odoo_post_deploy(
        control_plane_root=control_plane_root,
        record_store=record_store,
        request=request.post_deploy,
    )
    records: dict[str, object] = {
        "transition": (
            f"odoo-post-deploy:{driver_result.context}:{driver_result.instance}:{driver_result.phase}"
        )
    }
    return records, cast(dict[str, object], driver_result.model_dump(mode="json"))


def _product_profile_uses_odoo_driver(profile: LaunchplaneProductProfileRecord) -> bool:
    profile_driver_id = profile.driver_id.strip()
    if profile_driver_id == ODOO_DRIVER_ID:
        return True
    try:
        descriptor = read_driver_descriptor(profile_driver_id)
    except FileNotFoundError:
        return False
    return descriptor.base_driver_id == ODOO_DRIVER_ID


def write_odoo_config_parameter_override_result(
    *,
    record_store: OdooInstanceOverrideStore,
    request: OdooConfigParameterOverrideEnvelope,
) -> dict[str, object]:
    override_record = write_odoo_config_parameter_override_record(
        record_store=record_store,
        request=request.override,
    )
    return {
        "context": override_record.context,
        "instance": override_record.instance,
        "config_parameter_keys": sorted(
            override.key for override in override_record.config_parameters
        ),
    }


def write_odoo_website_bootstrap_override_result(
    *,
    record_store: OdooInstanceOverrideStore,
    request: OdooWebsiteBootstrapOverrideEnvelope,
) -> dict[str, object]:
    override_record = write_odoo_website_bootstrap_override_record(
        record_store=record_store,
        request=request.override,
    )
    return {
        "context": override_record.context,
        "instance": override_record.instance,
        "website_bootstrap": override_record.website_bootstrap is not None,
    }


def write_odoo_config_parameter_override_record(
    *,
    record_store: OdooInstanceOverrideStore,
    request: OdooConfigParameterOverrideRequest,
) -> OdooInstanceOverrideRecord:
    try:
        existing_record = record_store.read_odoo_instance_override_record(
            context_name=request.context, instance_name=request.instance
        )
    except FileNotFoundError:
        existing_record = None
    config_parameters = {
        override.key: override
        for override in (existing_record.config_parameters if existing_record is not None else ())
    }
    addon_settings = existing_record.addon_settings if existing_record is not None else ()
    config_parameters[request.key] = OdooConfigParameterOverride(
        key=request.key,
        value=OdooOverrideValue(source="literal", value=request.value),
    )
    apply_on = tuple(
        dict.fromkeys(
            (
                *(existing_record.apply_on if existing_record is not None else ()),
                "deploy",
                "promotion",
            )
        )
    )
    record = OdooInstanceOverrideRecord(
        context=request.context,
        instance=request.instance,
        apply_on=apply_on,
        config_parameters=tuple(config_parameters[key] for key in sorted(config_parameters)),
        addon_settings=addon_settings,
        website_bootstrap=existing_record.website_bootstrap
        if existing_record is not None
        else None,
        updated_at=_utc_now_timestamp(),
        source_label=request.source_label,
    )
    record_store.write_odoo_instance_override_record(record)
    return record


def write_odoo_website_bootstrap_override_record(
    *,
    record_store: OdooInstanceOverrideStore,
    request: OdooWebsiteBootstrapOverrideRequest,
) -> OdooInstanceOverrideRecord:
    try:
        existing_record = record_store.read_odoo_instance_override_record(
            context_name=request.context, instance_name=request.instance
        )
    except FileNotFoundError:
        existing_record = None
    apply_on = tuple(
        dict.fromkeys(
            (
                *(existing_record.apply_on if existing_record is not None else ()),
                "deploy",
                "promotion",
            )
        )
    )
    record = OdooInstanceOverrideRecord(
        context=request.context,
        instance=request.instance,
        apply_on=apply_on,
        config_parameters=existing_record.config_parameters if existing_record is not None else (),
        addon_settings=existing_record.addon_settings if existing_record is not None else (),
        website_bootstrap=request.website_bootstrap,
        updated_at=_utc_now_timestamp(),
        source_label=request.source_label,
    )
    record_store.write_odoo_instance_override_record(record)
    return record


def _utc_now_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
