import base64
import binascii
from collections.abc import Mapping

from control_plane.contracts.odoo_post_deploy_payload import OdooPostDeployPayload
from control_plane.runtime_platform_credentials import plain_setting_looks_like_credential
from control_plane.runtime_key_safety import runtime_value_carries_credentials

from control_plane.contracts.odoo_post_deploy_payload import ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY


def validate_preview_plain_settings(values: Mapping[str, object], *, context: str) -> None:
    if any(plain_setting_looks_like_credential(key, str(value)) for key, value in values.items()):
        raise ValueError("Preview credentials require managed secret bindings.")
    if any(key.startswith("ENV_OVERRIDE_") for key in values):
        raise ValueError("Preview Odoo settings require structured runtime payloads.")
    encoded = values.get(ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY)
    if encoded is None:
        return
    try:
        payload = OdooPostDeployPayload.model_validate_json(
            base64.b64decode(str(encoded), validate=True)
        )
        if (
            payload.context != context
            or payload.instance != "preview"
            or payload.workflow_intent != "deploy"
        ):
            raise ValueError("Preview payload must match its configuration scope.")
        items = [(item.key, item.value) for item in payload.config_parameters] + [
            (f"{item.addon}_{item.setting}", item.value) for item in payload.addon_settings
        ]
        for key, value in items:
            if value.source == "literal" and (
                plain_setting_looks_like_credential(key, str(value.value))
                or runtime_value_carries_credentials(str(value.value))
            ):
                raise ValueError("Preview credentials require managed secret bindings.")
    except (ValueError, binascii.Error):
        raise ValueError(
            "Preview payload requires scoped plain values or managed secret references."
        ) from None
