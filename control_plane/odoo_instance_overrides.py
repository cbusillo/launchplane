import click

from control_plane.contracts.odoo_instance_override_record import OdooInstanceOverrideRecord
from control_plane.contracts.odoo_instance_override_record import OdooOverrideValue

ODOO_CONFIG_PARAMETER_ENV_PREFIX = "ENV_OVERRIDE_CONFIG_PARAM__"
ODOO_ADDON_ENV_PREFIXES = {
    "authentik": "ENV_OVERRIDE_AUTHENTIK__",
    "authentik_sso": "ENV_OVERRIDE_AUTHENTIK__",
    "shopify": "ENV_OVERRIDE_SHOPIFY__",
}


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


def literal_override_value(value: OdooOverrideValue, *, override_name: str) -> str:
    if value.source == "secret_binding":
        raise click.ClickException(
            f"Odoo override {override_name!r} is secret-backed and cannot be rendered into a Dokploy schedule payload."
        )
    if value.value is None:
        raise click.ClickException(f"Odoo override {override_name!r} is missing a literal value.")
    return str(value.value)


def render_post_deploy_environment(record: OdooInstanceOverrideRecord) -> dict[str, str]:
    environment: dict[str, str] = {}
    for override in record.config_parameters:
        environment_key = config_parameter_env_key(override.key)
        environment[environment_key] = literal_override_value(override.value, override_name=override.key)
    for override in record.addon_settings:
        override_name = f"{override.addon}.{override.setting}"
        environment_key = addon_setting_env_key(addon_name=override.addon, setting_name=override.setting)
        environment[environment_key] = literal_override_value(override.value, override_name=override_name)
    return environment
