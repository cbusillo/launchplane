import unittest

from control_plane.odoo_instance_overrides import build_post_deploy_environment, render_post_deploy_environment
from control_plane.contracts.odoo_instance_override_record import (
    OdooAddonSettingOverride,
    OdooConfigParameterOverride,
    OdooInstanceOverrideRecord,
    OdooOverrideValue,
)


class OdooInstanceOverrideRenderingTests(unittest.TestCase):
    def test_render_post_deploy_environment_maps_literal_overrides_to_current_transport_keys(self) -> None:
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

        environment = render_post_deploy_environment(record)

        self.assertEqual(
            environment,
            {
                "ENV_OVERRIDE_CONFIG_PARAM__WEB__BASE__URL": "https://opw-prod.example.com",
                "ENV_OVERRIDE_AUTHENTIK__BASE_URL": "https://auth.example.com",
            },
        )

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

        self.assertEqual(environment.inline_environment, {})
        self.assertEqual(environment.required_container_environment_keys, ("ENV_OVERRIDE_SHOPIFY__API_TOKEN",))


if __name__ == "__main__":
    unittest.main()
