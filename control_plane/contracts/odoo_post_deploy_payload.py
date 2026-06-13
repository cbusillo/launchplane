from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.odoo_instance_override_record import (
    OdooOverrideValueSource,
    OdooWebsiteBootstrapPayload,
)
from control_plane.contracts.runtime_environment_record import ScalarValue

OdooPostDeployWorkflowIntent = Literal["deploy", "restore", "bootstrap"]


class OdooPostDeployRenderedValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: OdooOverrideValueSource
    value: ScalarValue | None = None
    secret_binding_id: str = ""
    environment_variable: str = ""

    @model_validator(mode="after")
    def _validate_source(self) -> "OdooPostDeployRenderedValue":
        self.secret_binding_id = self.secret_binding_id.strip()
        self.environment_variable = self.environment_variable.strip()
        if self.source == "literal":
            if self.value is None:
                raise ValueError("literal Odoo post-deploy payload values require value")
            if self.secret_binding_id:
                raise ValueError(
                    "literal Odoo post-deploy payload values must not include secret_binding_id"
                )
            if self.environment_variable:
                raise ValueError(
                    "literal Odoo post-deploy payload values must not include environment_variable"
                )
            return self
        if self.source == "secret_binding":
            if self.value is not None:
                raise ValueError(
                    "secret-backed Odoo post-deploy payload values must not include plaintext value"
                )
            if not self.secret_binding_id:
                raise ValueError(
                    "secret-backed Odoo post-deploy payload values require secret_binding_id"
                )
            if not self.environment_variable:
                raise ValueError(
                    "secret-backed Odoo post-deploy payload values require environment_variable"
                )
            return self
        raise ValueError(f"unsupported Odoo post-deploy payload value source: {self.source}")

    def to_wire_dict(self) -> dict[str, object]:
        if self.source == "literal":
            return {"source": self.source, "value": self.value}
        return {
            "source": self.source,
            "secret_binding_id": self.secret_binding_id,
            "environment_variable": self.environment_variable,
        }


class OdooPostDeployConfigParameter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    value: OdooPostDeployRenderedValue

    @model_validator(mode="after")
    def _validate_key(self) -> "OdooPostDeployConfigParameter":
        self.key = self.key.strip().lower()
        if not self.key:
            raise ValueError("Odoo post-deploy config parameter requires key")
        return self

    def to_wire_dict(self) -> dict[str, object]:
        return {"key": self.key, "value": self.value.to_wire_dict()}


class OdooPostDeployAddonSetting(BaseModel):
    model_config = ConfigDict(extra="forbid")

    addon: str
    setting: str
    value: OdooPostDeployRenderedValue

    @model_validator(mode="after")
    def _validate_key(self) -> "OdooPostDeployAddonSetting":
        self.addon = self.addon.strip().lower()
        self.setting = self.setting.strip().lower()
        if not self.addon:
            raise ValueError("Odoo post-deploy addon setting requires addon")
        if not self.setting:
            raise ValueError("Odoo post-deploy addon setting requires setting")
        return self

    def to_wire_dict(self) -> dict[str, object]:
        return {
            "addon": self.addon,
            "setting": self.setting,
            "value": self.value.to_wire_dict(),
        }


class OdooPostDeployPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str
    instance: str
    workflow_intent: OdooPostDeployWorkflowIntent = "deploy"
    config_parameters: tuple[OdooPostDeployConfigParameter, ...] = ()
    addon_settings: tuple[OdooPostDeployAddonSetting, ...] = ()
    website_bootstrap: OdooWebsiteBootstrapPayload | None = None

    @model_validator(mode="after")
    def _validate_payload(self) -> "OdooPostDeployPayload":
        self.context = self.context.strip().lower()
        self.instance = self.instance.strip().lower()
        if not self.context:
            raise ValueError("Odoo post-deploy payload requires context")
        if not self.instance:
            raise ValueError("Odoo post-deploy payload requires instance")
        if self.workflow_intent == "restore" and self.website_bootstrap is not None:
            raise ValueError("restore Odoo post-deploy payloads must not include website_bootstrap")
        return self

    @property
    def required_container_environment_keys(self) -> tuple[str, ...]:
        keys: list[str] = []
        for config_parameter in self.config_parameters:
            if config_parameter.value.source == "secret_binding":
                keys.append(config_parameter.value.environment_variable)
        for addon_setting in self.addon_settings:
            if addon_setting.value.source == "secret_binding":
                keys.append(addon_setting.value.environment_variable)
        return tuple(sorted(set(keys)))

    @property
    def override_count(self) -> int:
        website_count = 1 if self.website_bootstrap is not None else 0
        return len(self.config_parameters) + len(self.addon_settings) + website_count

    @property
    def website_bootstrap_included(self) -> bool:
        return self.website_bootstrap is not None

    def to_wire_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "context": self.context,
            "instance": self.instance,
            "config_parameters": [override.to_wire_dict() for override in self.config_parameters],
            "addon_settings": [override.to_wire_dict() for override in self.addon_settings],
        }
        if self.website_bootstrap is not None:
            payload["website_bootstrap"] = self.website_bootstrap.model_dump(mode="json")
        return payload

    def to_wire_json_bytes(self) -> bytes:
        return json.dumps(self.to_wire_dict(), separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )

    @property
    def wire_sha256(self) -> str:
        return hashlib.sha256(self.to_wire_json_bytes()).hexdigest()

    def redacted_evidence(self) -> dict[str, str]:
        evidence = {
            "workflow_intent": self.workflow_intent,
            "payload_schema_version": str(self.schema_version),
            "payload_sha256": self.wire_sha256,
            "config_parameter_count": str(len(self.config_parameters)),
            "addon_setting_count": str(len(self.addon_settings)),
            "website_bootstrap_included": str(self.website_bootstrap_included).lower(),
            "required_container_environment_keys": ",".join(
                self.required_container_environment_keys
            ),
        }
        if self.website_bootstrap is not None:
            homepage_route_count = sum(
                1 for route in self.website_bootstrap.routes if route.homepage
            )
            evidence.update(
                {
                    "website_bootstrap_name_present": str(
                        bool(self.website_bootstrap.name.strip())
                    ).lower(),
                    "website_bootstrap_canonical_url_present": str(
                        bool(self.website_bootstrap.canonical_url.strip())
                    ).lower(),
                    "website_bootstrap_homepage_url_present": str(
                        bool(self.website_bootstrap.homepage_url.strip())
                    ).lower(),
                    "website_bootstrap_logo_path_present": str(
                        bool(self.website_bootstrap.logo_path.strip())
                    ).lower(),
                    "website_bootstrap_logo_alt_present": str(
                        bool(self.website_bootstrap.logo_alt.strip())
                    ).lower(),
                    "website_bootstrap_route_count": str(len(self.website_bootstrap.routes)),
                    "website_bootstrap_homepage_route_count": str(homepage_route_count),
                }
            )
        return evidence
