from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from control_plane.contracts.runtime_environment_record import ScalarValue

OdooOverrideValueSource = Literal["literal", "secret_binding"]


class OdooOverrideValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: OdooOverrideValueSource
    value: ScalarValue | None = None
    secret_binding_id: str = ""

    @model_validator(mode="after")
    def _validate_value_source(self) -> "OdooOverrideValue":
        if self.source == "literal":
            if self.value is None:
                raise ValueError("literal Odoo override values require value")
            if self.secret_binding_id.strip():
                raise ValueError("literal Odoo override values must not include secret_binding_id")
            return self
        if self.source == "secret_binding":
            if self.value is not None:
                raise ValueError(
                    "secret-backed Odoo override values must not include plaintext value"
                )
            if not self.secret_binding_id.strip():
                raise ValueError("secret-backed Odoo override values require secret_binding_id")
            self.secret_binding_id = self.secret_binding_id.strip()
            return self
        raise ValueError(f"unsupported Odoo override value source: {self.source}")


class OdooConfigParameterOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    value: OdooOverrideValue

    @field_validator("key", mode="after")
    @classmethod
    def _validate_key(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("Odoo config parameter override requires key")
        return normalized


class OdooAddonSettingOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")

    addon: str
    setting: str
    value: OdooOverrideValue

    @field_validator("addon", mode="after")
    @classmethod
    def _validate_addon(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("Odoo addon setting override requires addon")
        return normalized

    @field_validator("setting", mode="after")
    @classmethod
    def _validate_setting(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("Odoo addon setting override requires setting")
        return normalized


class OdooInstanceOverrideRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str
    instance: str
    config_parameters: tuple[OdooConfigParameterOverride, ...] = ()
    addon_settings: tuple[OdooAddonSettingOverride, ...] = ()
    updated_at: str
    source_label: str = ""

    @model_validator(mode="after")
    def _validate_record(self) -> "OdooInstanceOverrideRecord":
        self.context = self.context.strip().lower()
        self.instance = self.instance.strip().lower()
        self.updated_at = self.updated_at.strip()
        self.source_label = self.source_label.strip()
        if not self.context:
            raise ValueError("Odoo instance override record requires context")
        if not self.instance:
            raise ValueError("Odoo instance override record requires instance")
        if not self.updated_at:
            raise ValueError("Odoo instance override record requires updated_at")
        if not self.config_parameters and not self.addon_settings:
            raise ValueError("Odoo instance override record requires at least one override")

        config_parameter_keys = [override.key for override in self.config_parameters]
        if len(config_parameter_keys) != len(set(config_parameter_keys)):
            raise ValueError("Odoo instance override record has duplicate config parameter keys")

        addon_setting_keys = [
            (override.addon, override.setting) for override in self.addon_settings
        ]
        if len(addon_setting_keys) != len(set(addon_setting_keys)):
            raise ValueError("Odoo instance override record has duplicate addon settings")
        return self
