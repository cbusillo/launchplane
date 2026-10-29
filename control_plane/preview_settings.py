import base64
import binascii
from collections.abc import Mapping

import click

from control_plane.contracts.odoo_post_deploy_payload import (
    ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY,
    OdooPostDeployPayload,
)
from control_plane.runtime_platform_credentials import plain_setting_looks_like_credential
from control_plane.runtime_key_safety import runtime_value_carries_credentials


def validate_preview_scope(
    plain: Mapping[str, object], *, context: str, secret_keys: frozenset[str] = frozenset()
) -> OdooPostDeployPayload | None:
    if ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY in secret_keys:
        raise ValueError("Preview payloads must be plain settings with managed-secret references.")
    if plain.keys() & secret_keys:
        raise ValueError("Preview secret bindings cannot be stored as plain settings.")
    if any(
        plain_setting_looks_like_credential(key, str(value))
        or runtime_value_carries_credentials(str(value))
        for key, value in plain.items()
    ):
        raise ValueError("Preview credentials require managed secret bindings.")
    if any(key.startswith("ENV_OVERRIDE_") for key in plain):
        raise ValueError("Preview Odoo settings require structured runtime payloads.")
    encoded = plain.get(ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY)
    if encoded is None:
        return None
    try:
        payload = OdooPostDeployPayload.model_validate_json(
            base64.b64decode(str(encoded), validate=True)
        )
        if (
            payload.context != context
            or payload.instance != "preview"
            or payload.workflow_intent != "deploy"
            or payload.config_parameters
            or payload.website_settings is not None
            or payload.website_bootstrap is not None
        ):
            raise ValueError("Preview payload must contain only scoped addon settings.")
        pairs = [(item.addon, item.setting) for item in payload.addon_settings]
        if len(pairs) != len(set(pairs)):
            raise ValueError("Conflicting preview addon settings.")
        from control_plane.odoo_instance_overrides import SHOPIFY_ADDON_NAME, SHOPIFY_STORE_SETTINGS

        for item in payload.addon_settings:
            credential_name = (
                ""
                if item.addon == SHOPIFY_ADDON_NAME and item.setting in SHOPIFY_STORE_SETTINGS
                else f"{item.addon}_{item.setting}"
            )
            if item.value.source == "literal" and (
                plain_setting_looks_like_credential(credential_name, str(item.value.value))
                or runtime_value_carries_credentials(str(item.value.value))
            ):
                raise ValueError("Preview credentials require managed secret bindings.")
            if item.value.source == "secret_binding":
                from control_plane.odoo_instance_overrides import addon_setting_secret_env_key

                if item.value.environment_variable != addon_setting_secret_env_key(
                    addon_name=item.addon, setting_name=item.setting
                ):
                    raise ValueError("Preview addon secrets require canonical transport.")
        return payload
    except (ValueError, binascii.Error, click.ClickException):
        raise ValueError(
            "Preview payload requires scoped addon settings and managed secret references."
        ) from None
