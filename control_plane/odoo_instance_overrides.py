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
SHOPIFY_ADDON_NAME = "shopify"
SHOPIFY_ACTION_SETTING = "action"
SHOPIFY_ACTION_APPLY = "apply"
SHOPIFY_ACTION_CLEAR = "clear"
SHOPIFY_ALLOW_PRODUCTION_SETTING = "allow_production"
SHOPIFY_PRODUCTION_INDICATORS_SETTING = "production_indicators"
SHOPIFY_REQUIRED_SETTINGS = ("shop_url_key", "api_token", "webhook_key", "api_version")
DEFAULT_SHOPIFY_PRODUCTION_INDICATORS = ("production", "live", "prod-")


@dataclass(frozen=True)
class PostDeployOverrideEnvironment:
    inline_environment: dict[str, str]
    required_container_environment_keys: tuple[str, ...]


def _normalize_boolean_literal(*, value: object, setting_name: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"", "0", "false", "no", "off"}:
        return False
    raise click.ClickException(
        f"Shopify override {setting_name!r} must be a boolean-compatible literal value."
    )


def _normalize_text_literal(*, value: object) -> str:
    if isinstance(value, bool):
        return "True" if value else "False"
    return str(value).strip()


def _shopify_override_value_text(*, override_value: OdooOverrideValue, setting_name: str) -> str:
    if override_value.source != "literal":
        raise click.ClickException(
            f"Shopify override {setting_name!r} must use a literal value so Launchplane can validate it."
        )
    if override_value.value is None:
        raise click.ClickException(f"Shopify override {setting_name!r} is missing a literal value.")
    return _normalize_text_literal(value=override_value.value)


def _render_literal_payload_override(*, value: object) -> dict[str, object]:
    return {
        "source": "literal",
        "value": value,
    }


def _resolve_shopify_payload_settings(
    *,
    record: OdooInstanceOverrideRecord,
    protected_shopify_store_keys: tuple[str, ...] = (),
) -> list[dict[str, object]]:
    shopify_overrides = [
        override for override in record.addon_settings if override.addon == SHOPIFY_ADDON_NAME
    ]
    if not shopify_overrides:
        return []

    overrides_by_setting = {override.setting: override for override in shopify_overrides}
    configured_required_settings = [
        setting_name
        for setting_name in SHOPIFY_REQUIRED_SETTINGS
        if setting_name in overrides_by_setting
    ]
    if len(configured_required_settings) != len(SHOPIFY_REQUIRED_SETTINGS):
        return [
            {
                "addon": SHOPIFY_ADDON_NAME,
                "setting": SHOPIFY_ACTION_SETTING,
                "value": _render_literal_payload_override(value=SHOPIFY_ACTION_CLEAR),
            }
        ]

    shop_url_key = _shopify_override_value_text(
        override_value=overrides_by_setting["shop_url_key"].value,
        setting_name="shopify.shop_url_key",
    )
    normalized_shop_url_key = shop_url_key.lower()
    normalized_protected_keys = {
        raw_key.strip().lower() for raw_key in protected_shopify_store_keys if raw_key.strip()
    }
    if normalized_shop_url_key in normalized_protected_keys:
        protected_list = ", ".join(sorted(normalized_protected_keys))
        raise click.ClickException(
            "Shopify shop_url_key is protected for this Launchplane target. "
            f"current={shop_url_key!r} protected={protected_list}"
        )

    allow_production = False
    if SHOPIFY_ALLOW_PRODUCTION_SETTING in overrides_by_setting:
        allow_production = _normalize_boolean_literal(
            value=overrides_by_setting[SHOPIFY_ALLOW_PRODUCTION_SETTING].value.value,
            setting_name=f"shopify.{SHOPIFY_ALLOW_PRODUCTION_SETTING}",
        )

    production_indicators = list(DEFAULT_SHOPIFY_PRODUCTION_INDICATORS)
    if SHOPIFY_PRODUCTION_INDICATORS_SETTING in overrides_by_setting:
        raw_indicators = _shopify_override_value_text(
            override_value=overrides_by_setting[SHOPIFY_PRODUCTION_INDICATORS_SETTING].value,
            setting_name=f"shopify.{SHOPIFY_PRODUCTION_INDICATORS_SETTING}",
        )
        cleaned_indicators = [
            item.strip().lower() for item in raw_indicators.split(",") if item.strip()
        ]
        production_indicators = cleaned_indicators or list(DEFAULT_SHOPIFY_PRODUCTION_INDICATORS)

    matched_indicator = ""
    for indicator in production_indicators:
        if indicator and indicator in normalized_shop_url_key:
            matched_indicator = indicator
            break
    if matched_indicator and not allow_production:
        raise click.ClickException(
            "Shopify shop_url_key appears to be production-like for this Launchplane payload. "
            f"key={shop_url_key!r} indicator={matched_indicator!r}"
        )

    payload_settings: list[dict[str, object]] = [
        {
            "addon": SHOPIFY_ADDON_NAME,
            "setting": SHOPIFY_ACTION_SETTING,
            "value": _render_literal_payload_override(value=SHOPIFY_ACTION_APPLY),
        }
    ]
    for override in shopify_overrides:
        if override.setting in {
            SHOPIFY_ALLOW_PRODUCTION_SETTING,
            SHOPIFY_PRODUCTION_INDICATORS_SETTING,
        }:
            continue
        environment_key = addon_setting_env_key(
            addon_name=override.addon, setting_name=override.setting
        )
        payload_settings.append(
            {
                "addon": override.addon,
                "setting": override.setting,
                "value": _payload_override_value(
                    value=override.value, environment_key=environment_key
                ),
            }
        )
    return payload_settings


def _payload_override_value(
    *, value: OdooOverrideValue, environment_key: str | None = None
) -> dict[str, object]:
    payload: dict[str, object] = {
        "source": value.source,
    }
    if value.source == "literal":
        payload["value"] = value.value
        return payload
    payload["secret_binding_id"] = value.secret_binding_id
    if not environment_key:
        raise click.ClickException(
            "Secret-backed Odoo overrides require a runtime environment key."
        )
    payload["environment_variable"] = environment_key
    return payload


def render_post_deploy_payload(
    record: OdooInstanceOverrideRecord,
    *,
    protected_shopify_store_keys: tuple[str, ...] = (),
) -> dict[str, object]:
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
                "value": _payload_override_value(
                    value=override.value, environment_key=environment_key
                ),
            }
        )
    addon_settings: list[dict[str, object]] = []
    addon_settings.extend(
        _resolve_shopify_payload_settings(
            record=record,
            protected_shopify_store_keys=protected_shopify_store_keys,
        )
    )
    for override in record.addon_settings:
        if override.addon == SHOPIFY_ADDON_NAME:
            continue
        environment_key = addon_setting_env_key(
            addon_name=override.addon, setting_name=override.setting
        )
        addon_settings.append(
            {
                "addon": override.addon,
                "setting": override.setting,
                "value": _payload_override_value(
                    value=override.value, environment_key=environment_key
                ),
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


def build_post_deploy_environment(
    record: OdooInstanceOverrideRecord,
    *,
    protected_shopify_store_keys: tuple[str, ...] = (),
) -> PostDeployOverrideEnvironment:
    payload = render_post_deploy_payload(
        record,
        protected_shopify_store_keys=protected_shopify_store_keys,
    )
    inline_environment: dict[str, str] = {
        ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY: _encode_post_deploy_payload(payload),
    }
    required_container_environment_keys: list[str] = []
    config_parameter_payloads = payload.get("config_parameters")
    if not isinstance(config_parameter_payloads, list):
        raise click.ClickException("Odoo override payload is missing config_parameters.")
    for override in config_parameter_payloads:
        value_payload = override.get("value") if isinstance(override, dict) else None
        if not isinstance(value_payload, dict) or value_payload.get("source") != "secret_binding":
            continue
        environment_key = str(value_payload.get("environment_variable") or "").strip()
        if environment_key:
            required_container_environment_keys.append(environment_key)
    addon_setting_payloads = payload.get("addon_settings")
    if not isinstance(addon_setting_payloads, list):
        raise click.ClickException("Odoo override payload is missing addon_settings.")
    for override in addon_setting_payloads:
        value_payload = override.get("value") if isinstance(override, dict) else None
        if not isinstance(value_payload, dict) or value_payload.get("source") != "secret_binding":
            continue
        environment_key = str(value_payload.get("environment_variable") or "").strip()
        if environment_key:
            required_container_environment_keys.append(environment_key)
    return PostDeployOverrideEnvironment(
        inline_environment=inline_environment,
        required_container_environment_keys=tuple(sorted(set(required_container_environment_keys))),
    )


def render_post_deploy_environment(
    record: OdooInstanceOverrideRecord,
    *,
    protected_shopify_store_keys: tuple[str, ...] = (),
) -> dict[str, str]:
    return build_post_deploy_environment(
        record,
        protected_shopify_store_keys=protected_shopify_store_keys,
    ).inline_environment
