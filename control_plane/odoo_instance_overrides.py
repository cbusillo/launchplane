from dataclasses import dataclass

import click

from control_plane.contracts.odoo_instance_override_record import OdooInstanceOverrideRecord
from control_plane.contracts.odoo_instance_override_record import OdooOverrideValue

ODOO_CONFIG_PARAMETER_ENV_PREFIX = "ENV_OVERRIDE_CONFIG_PARAM__"
ODOO_ADDON_ENV_PREFIXES = {
    "authentik": "ENV_OVERRIDE_AUTHENTIK__",
    "authentik_sso": "ENV_OVERRIDE_AUTHENTIK__",
    "shopify": "ENV_OVERRIDE_SHOPIFY__",
}


@dataclass(frozen=True)
class PostDeployOverrideEnvironment:
    inline_environment: dict[str, str]
    required_container_environment_keys: tuple[str, ...]


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


def _apply_override_value(
    *,
    environment_key: str,
    value: OdooOverrideValue,
    override_name: str,
    inline_environment: dict[str, str],
    required_container_environment_keys: list[str],
) -> None:
    if value.source == "secret_binding":
        _ = override_name
        required_container_environment_keys.append(environment_key)
        return
    if value.value is None:
        raise click.ClickException(f"Odoo override {override_name!r} is missing a literal value.")
    inline_environment[environment_key] = str(value.value)


def build_post_deploy_environment(record: OdooInstanceOverrideRecord) -> PostDeployOverrideEnvironment:
    inline_environment: dict[str, str] = {}
    required_container_environment_keys: list[str] = []
    for override in record.config_parameters:
        environment_key = config_parameter_env_key(override.key)
        _apply_override_value(
            environment_key=environment_key,
            value=override.value,
            override_name=override.key,
            inline_environment=inline_environment,
            required_container_environment_keys=required_container_environment_keys,
        )
    for override in record.addon_settings:
        override_name = f"{override.addon}.{override.setting}"
        environment_key = addon_setting_env_key(addon_name=override.addon, setting_name=override.setting)
        _apply_override_value(
            environment_key=environment_key,
            value=override.value,
            override_name=override_name,
            inline_environment=inline_environment,
            required_container_environment_keys=required_container_environment_keys,
        )
    return PostDeployOverrideEnvironment(
        inline_environment=inline_environment,
        required_container_environment_keys=tuple(sorted(set(required_container_environment_keys))),
    )


def render_post_deploy_environment(record: OdooInstanceOverrideRecord) -> dict[str, str]:
    return build_post_deploy_environment(record).inline_environment
