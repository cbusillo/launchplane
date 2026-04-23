import base64
import json
import unittest

from control_plane.odoo_instance_overrides import (
    ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY,
    build_post_deploy_environment,
    render_post_deploy_payload,
)
from control_plane.contracts.odoo_instance_override_record import (
    OdooAddonSettingOverride,
    OdooConfigParameterOverride,
    OdooInstanceOverrideRecord,
    OdooOverrideValue,
)


class OdooInstanceOverrideRenderingTests(unittest.TestCase):
    def test_render_post_deploy_payload_preserves_typed_override_shapes(self) -> None:
        record = OdooInstanceOverrideRecord(
            context="opw",
            instance="prod",
            config_parameters=(
                OdooConfigParameterOverride(
                    key="web.base.url",
                    value=OdooOverrideValue(source="literal", value="https://opw-prod.example.com"),
                ),
            ),
            addon_settings=(
                OdooAddonSettingOverride(
                    addon="authentik_sso",
                    setting="base_url",
                    value=OdooOverrideValue(source="literal", value="https://auth.example.com"),
                ),
            ),
            updated_at="2026-04-23T12:00:00Z",
        )

        payload = render_post_deploy_payload(record)

        self.assertEqual(
            payload,
            {
                "schema_version": 1,
                "context": "opw",
                "instance": "prod",
                "config_parameters": [
                    {
                        "key": "web.base.url",
                        "value": {
                            "source": "literal",
                            "value": "https://opw-prod.example.com",
                        },
                    }
                ],
                "addon_settings": [
                    {
                        "addon": "authentik_sso",
                        "setting": "base_url",
                        "value": {
                            "source": "literal",
                            "value": "https://auth.example.com",
                        },
                    }
                ],
            },
        )

    def test_build_post_deploy_environment_sets_base64_payload_env(self) -> None:
        record = OdooInstanceOverrideRecord(
            context="opw",
            instance="prod",
            config_parameters=(
                OdooConfigParameterOverride(
                    key="web.base.url",
                    value=OdooOverrideValue(source="literal", value="https://opw-prod.example.com"),
                ),
            ),
            updated_at="2026-04-23T12:00:00Z",
        )

        environment = build_post_deploy_environment(record)

        encoded_payload = environment.inline_environment[ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY]
        decoded_payload = json.loads(base64.b64decode(encoded_payload).decode("utf-8"))

        self.assertEqual(decoded_payload, render_post_deploy_payload(record))

    def test_build_post_deploy_environment_requires_container_env_for_secret_backed_values(self) -> None:
        record = OdooInstanceOverrideRecord(
            context="opw",
            instance="prod",
            addon_settings=(
                OdooAddonSettingOverride(
                    addon="shopify",
                    setting="api_token",
                    value=OdooOverrideValue(
                        source="secret_binding",
                        secret_binding_id="secret-binding-shopify-token",
                    ),
                ),
            ),
            updated_at="2026-04-23T12:00:00Z",
        )

        environment = build_post_deploy_environment(record)

        self.assertIn(ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY, environment.inline_environment)
        self.assertEqual(environment.required_container_environment_keys, ("ENV_OVERRIDE_SHOPIFY__API_TOKEN",))


if __name__ == "__main__":
    unittest.main()
