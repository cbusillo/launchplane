import base64
from dataclasses import dataclass

import click

from control_plane.contracts.odoo_instance_override_record import OdooInstanceOverrideRecord
from control_plane.contracts.odoo_instance_override_record import OdooOverrideValue
from control_plane.contracts.odoo_post_deploy_payload import OdooPostDeployAddonSetting
from control_plane.contracts.odoo_post_deploy_payload import OdooPostDeployConfigParameter
from control_plane.contracts.odoo_post_deploy_payload import OdooPostDeployPayload
from control_plane.contracts.odoo_post_deploy_payload import OdooPostDeployRenderedValue
from control_plane.contracts.odoo_post_deploy_payload import OdooPostDeployWorkflowIntent
from control_plane.contracts.runtime_environment_record import ScalarValue

ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY = "ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64"
ODOO_OVERRIDE_SECRET_ENV_PREFIX = "ODOO_OVERRIDE_SECRET__"
SHOPIFY_ADDON_NAME = "shopify"
SHOPIFY_ACTION_SETTING = "action"
SHOPIFY_ACTION_APPLY = "apply"
SHOPIFY_ACTION_CLEAR = "clear"
SHOPIFY_ALLOW_PRODUCTION_SETTING = "allow_production"
SHOPIFY_PRODUCTION_INDICATORS_SETTING = "production_indicators"
SHOPIFY_REQUIRED_SETTINGS = ("shop_url_key", "api_token", "webhook_key", "api_version")
DEFAULT_SHOPIFY_PRODUCTION_INDICATORS = ("production", "live", "prod-")
PostDeployWorkflowIntent = OdooPostDeployWorkflowIntent


@dataclass(frozen=True)
class PostDeployOverrideEnvironment:
    inline_environment: dict[str, str]
    required_container_environment_keys: tuple[str, ...]
    payload: OdooPostDeployPayload


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


def _render_literal_payload_override(*, value: ScalarValue) -> OdooPostDeployRenderedValue:
    return OdooPostDeployRenderedValue(source="literal", value=value)


def _resolve_shopify_payload_settings(
    *,
    record: OdooInstanceOverrideRecord,
    protected_shopify_store_keys: tuple[str, ...] = (),
) -> list[OdooPostDeployAddonSetting]:
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
            OdooPostDeployAddonSetting(
                addon=SHOPIFY_ADDON_NAME,
                setting=SHOPIFY_ACTION_SETTING,
                value=_render_literal_payload_override(value=SHOPIFY_ACTION_CLEAR),
            )
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

    payload_settings: list[OdooPostDeployAddonSetting] = [
        OdooPostDeployAddonSetting(
            addon=SHOPIFY_ADDON_NAME,
            setting=SHOPIFY_ACTION_SETTING,
            value=_render_literal_payload_override(value=SHOPIFY_ACTION_APPLY),
        )
    ]
    for override in shopify_overrides:
        if override.setting in {
            SHOPIFY_ALLOW_PRODUCTION_SETTING,
            SHOPIFY_PRODUCTION_INDICATORS_SETTING,
        }:
            continue
        environment_key = addon_setting_secret_env_key(
            addon_name=override.addon, setting_name=override.setting
        )
        payload_settings.append(
            OdooPostDeployAddonSetting(
                addon=override.addon,
                setting=override.setting,
                value=_payload_override_value(
                    value=override.value, environment_key=environment_key
                ),
            )
        )
    return payload_settings


def _payload_override_value(
    *, value: OdooOverrideValue, environment_key: str | None = None
) -> OdooPostDeployRenderedValue:
    if value.source == "literal":
        return OdooPostDeployRenderedValue(source=value.source, value=value.value)
    if not environment_key:
        raise click.ClickException(
            "Secret-backed Odoo overrides require a runtime environment key."
        )
    return OdooPostDeployRenderedValue(
        source=value.source,
        secret_binding_id=value.secret_binding_id,
        environment_variable=environment_key,
    )


def render_post_deploy_payload(
    record: OdooInstanceOverrideRecord,
    *,
    workflow_intent: PostDeployWorkflowIntent = "deploy",
    protected_shopify_store_keys: tuple[str, ...] = (),
) -> OdooPostDeployPayload:
    config_parameters: list[OdooPostDeployConfigParameter] = []
    for config_parameter_override in record.config_parameters:
        environment_key = config_parameter_secret_env_key(config_parameter_override.key)
        config_parameters.append(
            OdooPostDeployConfigParameter(
                key=config_parameter_override.key,
                value=_payload_override_value(
                    value=config_parameter_override.value, environment_key=environment_key
                ),
            )
        )
    addon_settings: list[OdooPostDeployAddonSetting] = []
    addon_settings.extend(
        _resolve_shopify_payload_settings(
            record=record,
            protected_shopify_store_keys=protected_shopify_store_keys,
        )
    )
    for addon_setting_override in record.addon_settings:
        if addon_setting_override.addon == SHOPIFY_ADDON_NAME:
            continue
        environment_key = addon_setting_secret_env_key(
            addon_name=addon_setting_override.addon,
            setting_name=addon_setting_override.setting,
        )
        addon_settings.append(
            OdooPostDeployAddonSetting(
                addon=addon_setting_override.addon,
                setting=addon_setting_override.setting,
                value=_payload_override_value(
                    value=addon_setting_override.value, environment_key=environment_key
                ),
            )
        )
    website_bootstrap = record.website_bootstrap if workflow_intent != "restore" else None
    return OdooPostDeployPayload(
        schema_version=1,
        context=record.context,
        instance=record.instance,
        workflow_intent=workflow_intent,
        config_parameters=tuple(config_parameters),
        addon_settings=tuple(addon_settings),
        website_bootstrap=website_bootstrap,
    )


def _encode_post_deploy_payload(payload: OdooPostDeployPayload) -> str:
    return base64.b64encode(payload.to_wire_json_bytes()).decode("ascii")


def _secret_env_suffix(raw_value: str) -> str:
    suffix = raw_value.strip().upper().replace(".", "__").replace("-", "_")
    if not suffix:
        raise click.ClickException("Odoo secret override transport requires a non-empty key.")
    return suffix


def config_parameter_secret_env_key(config_parameter_key: str) -> str:
    suffix = config_parameter_key.strip().upper().replace(".", "__")
    if not suffix:
        raise click.ClickException("Odoo config parameter override requires a non-empty key.")
    return f"{ODOO_OVERRIDE_SECRET_ENV_PREFIX}CONFIG_PARAM__{suffix}"


def addon_setting_secret_env_key(*, addon_name: str, setting_name: str) -> str:
    normalized_addon = addon_name.strip().lower()
    if not normalized_addon:
        raise click.ClickException("Odoo addon setting override requires a non-empty addon.")
    suffix = _secret_env_suffix(setting_name)
    addon_suffix = _secret_env_suffix(normalized_addon)
    if not suffix:
        raise click.ClickException("Odoo addon setting override requires a non-empty setting.")
    return f"{ODOO_OVERRIDE_SECRET_ENV_PREFIX}ADDON__{addon_suffix}__{suffix}"


def build_post_deploy_environment(
    record: OdooInstanceOverrideRecord,
    *,
    workflow_intent: PostDeployWorkflowIntent = "deploy",
    protected_shopify_store_keys: tuple[str, ...] = (),
) -> PostDeployOverrideEnvironment:
    payload = render_post_deploy_payload(
        record,
        workflow_intent=workflow_intent,
        protected_shopify_store_keys=protected_shopify_store_keys,
    )
    inline_environment: dict[str, str] = {
        ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY: _encode_post_deploy_payload(payload),
    }
    return PostDeployOverrideEnvironment(
        inline_environment=inline_environment,
        required_container_environment_keys=payload.required_container_environment_keys,
        payload=payload,
    )


def render_post_deploy_environment(
    record: OdooInstanceOverrideRecord,
    *,
    workflow_intent: PostDeployWorkflowIntent = "deploy",
    protected_shopify_store_keys: tuple[str, ...] = (),
) -> dict[str, str]:
    return build_post_deploy_environment(
        record,
        workflow_intent=workflow_intent,
        protected_shopify_store_keys=protected_shopify_store_keys,
    ).inline_environment
