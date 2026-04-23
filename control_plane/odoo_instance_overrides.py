import base64
import json
from dataclasses import dataclass

import click

from control_plane.contracts.odoo_instance_override_record import OdooInstanceOverrideRecord
from control_plane.contracts.odoo_instance_override_record import OdooOverrideValue

ODOO_CONFIG_PARAMETER_ENV_PREFIX = "ENV_OVERRIDE_CONFIG_PARAM__"
ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY = "ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64"
ODOO_ADDON_ENV_PREFIXES = {
    "authentik": "ENV_OVERRIDE_AUTHENTIK__",
    "authentik_sso": "ENV_OVERRIDE_AUTHENTIK__",
    "shopify": "ENV_OVERRIDE_SHOPIFY__",
}


@dataclass(frozen=True)
class PostDeployOverrideEnvironment:
    inline_environment: dict[str, str]
    required_container_environment_keys: tuple[str, ...]


def _payload_override_value(*, value: OdooOverrideValue, environment_key: str | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "source": value.source,
    }
    if value.source == "literal":
        payload["value"] = value.value
        return payload
    payload["secret_binding_id"] = value.secret_binding_id
    if not environment_key:
        raise click.ClickException("Secret-backed Odoo overrides require a runtime environment key.")
    payload["environment_variable"] = environment_key
    return payload


def render_post_deploy_payload(record: OdooInstanceOverrideRecord) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "context": record.context,
        "instance": record.instance,
        "config_parameters": [],
        "addon_settings": [],
    }
    config_parameters: list[dict[str, object]] = []
    for override in record.config_parameters:
        environment_key = config_parameter_env_key(override.key)
        config_parameters.append(
            {
                "key": override.key,
                "value": _payload_override_value(value=override.value, environment_key=environment_key),
            }
        )
    addon_settings: list[dict[str, object]] = []
    for override in record.addon_settings:
        environment_key = addon_setting_env_key(addon_name=override.addon, setting_name=override.setting)
        addon_settings.append(
            {
                "addon": override.addon,
                "setting": override.setting,
                "value": _payload_override_value(value=override.value, environment_key=environment_key),
            }
        )
    payload["config_parameters"] = config_parameters
    payload["addon_settings"] = addon_settings
    return payload


def _encode_post_deploy_payload(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.b64encode(encoded).decode("ascii")


def config_parameter_env_key(config_parameter_key: str) -> str:
    suffix = config_parameter_key.strip().upper().replace(".", "__")
    if not suffix:
        raise click.ClickException("Odoo config parameter override requires a non-empty key.")
    return f"{ODOO_CONFIG_PARAMETER_ENV_PREFIX}{suffix}"


def addon_setting_env_key(*, addon_name: str, setting_name: str) -> str:
    normalized_addon = addon_name.strip().lower()
    prefix = ODOO_ADDON_ENV_PREFIXES.get(normalized_addon)
    if prefix is None:
        raise click.ClickException(
            f"Odoo addon override {normalized_addon!r} does not have a current post-deploy transport mapping."
        )
    suffix = setting_name.strip().upper().replace(".", "__").replace("-", "_")
    if not suffix:
        raise click.ClickException("Odoo addon setting override requires a non-empty setting.")
    return f"{prefix}{suffix}"

def build_post_deploy_environment(record: OdooInstanceOverrideRecord) -> PostDeployOverrideEnvironment:
    payload = render_post_deploy_payload(record)
    inline_environment: dict[str, str] = {
        ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY: _encode_post_deploy_payload(payload),
    }
    required_container_environment_keys: list[str] = []
    for override in record.config_parameters:
        if override.value.source != "secret_binding":
            continue
        required_container_environment_keys.append(config_parameter_env_key(override.key))
    for override in record.addon_settings:
        if override.value.source != "secret_binding":
            continue
        required_container_environment_keys.append(
            addon_setting_env_key(addon_name=override.addon, setting_name=override.setting)
        )
    return PostDeployOverrideEnvironment(
        inline_environment=inline_environment,
        required_container_environment_keys=tuple(sorted(set(required_container_environment_keys))),
    )


def render_post_deploy_environment(record: OdooInstanceOverrideRecord) -> dict[str, str]:
    return build_post_deploy_environment(record).inline_environment
