import base64
import binascii
import re

from control_plane.contracts.odoo_post_deploy_payload import OdooPostDeployPayload
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.secret_record import SecretBinding
from control_plane.odoo_instance_overrides import ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY
from control_plane.runtime_key_safety import (
    is_integration_runtime_key,
    runtime_value_carries_credentials,
)
from control_plane.runtime_platform_credentials import (
    PlatformCredentialRefusedError,
    plain_setting_looks_like_credential,
    refuse_platform_credentials,
)


def director_preview_snapshot_allowed(
    existing: LaunchplaneProductProfileRecord | None,
    replacement: LaunchplaneProductProfileRecord,
) -> bool:
    if (
        existing is None
        or not existing.is_active
        or existing.driver_id != "odoo"
        or not existing.preview.enabled
        or existing.preview.clean_testing_snapshot
        or not replacement.preview.clean_testing_snapshot
    ):
        return False
    payload = existing.model_dump(mode="json")
    payload["preview"]["clean_testing_snapshot"] = True
    try:
        expected = LaunchplaneProductProfileRecord.model_validate(payload)
    except ValueError:
        return False
    return replacement == expected


def director_preview_override_allowed(
    *,
    existing: LaunchplaneProductProfileRecord | None,
    replacement: LaunchplaneProductProfileRecord,
    secret_bindings: tuple[SecretBinding, ...],
    integration_key_markers: tuple[str, ...],
) -> bool:
    if (
        existing is None
        or not existing.is_active
        or existing.driver_id != "odoo"
        or not existing.preview.enabled
        or existing.preview.historical_transport
    ):
        return False
    before = existing.preview.override_env
    after = replacement.preview.override_env
    if before.keys() - after.keys():
        return False
    changed = {key: value for key, value in after.items() if before.get(key) != value}
    if len(changed) != 1:
        return False
    expected = existing.model_copy(deep=True)
    expected.preview.override_env.update(changed)
    if replacement != expected:
        return False
    key, value = next(iter(changed.items()))
    secret_keys = {
        item.binding_key for item in existing.expected_config.managed_secret_bindings
    } | {
        binding.binding_key
        for binding in secret_bindings
        if not binding.context or binding.context == existing.preview.context
    }
    if (
        not re.fullmatch(r"[A-Z][A-Z0-9_]*", key)
        or key in secret_keys
        or key.startswith("ENV_OVERRIDE_")
        or is_integration_runtime_key(key, extra_markers=integration_key_markers)
    ):
        return False
    try:
        refuse_platform_credentials(changed, target="preview", source="preview override")
        if plain_setting_looks_like_credential(key, value) or runtime_value_carries_credentials(
            value
        ):
            return False
        if key != ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY:
            return True
        payloads = (before[key], value) if key in before else (value,)
        return all(
            _nonsecret_preview_payload_allowed(
                encoded,
                context=existing.preview.context,
                integration_key_markers=integration_key_markers,
            )
            for encoded in payloads
        )
    except (ValueError, binascii.Error, PlatformCredentialRefusedError):
        return False


def _nonsecret_preview_payload_allowed(
    value: str, *, context: str, integration_key_markers: tuple[str, ...]
) -> bool:
    payload = OdooPostDeployPayload.model_validate_json(base64.b64decode(value, validate=True))
    pairs = [(item.addon, item.setting) for item in payload.addon_settings]
    return (
        payload.context == context
        and payload.instance == "preview"
        and payload.workflow_intent == "deploy"
        and not payload.config_parameters
        and payload.website_bootstrap is None
        and payload.website_settings is None
        and bool(pairs)
        and len(pairs) == len(set(pairs))
        and all(
            not is_integration_runtime_key(
                f"{item.addon}_{item.setting}", extra_markers=integration_key_markers
            )
            and item.value.source == "literal"
            and not plain_setting_looks_like_credential(item.setting, str(item.value.value))
            and not runtime_value_carries_credentials(str(item.value.value))
            for item in payload.addon_settings
        )
    )
