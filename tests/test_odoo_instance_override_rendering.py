import base64
import json
import unittest
from typing import cast

import click

from control_plane.odoo_instance_overrides import (
    ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY,
    build_post_deploy_environment,
    render_post_deploy_payload,
)
from control_plane.dokploy import (
    DokployTargetDefinition,
    protected_shopify_store_keys_for_target_definition,
)
from control_plane.contracts.dokploy_target_record import (
    DokployTargetPolicies,
    DokployTargetShopifyPolicy,
)
from control_plane.contracts.odoo_instance_override_record import (
    OdooAddonSettingOverride,
    OdooConfigParameterOverride,
    OdooInstanceOverrideRecord,
    OdooOverrideValue,
    OdooWebsiteBootstrapPayload,
    OdooWebsiteBootstrapRoute,
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
            payload.to_wire_dict(),
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

        self.assertEqual(decoded_payload, render_post_deploy_payload(record).to_wire_dict())
        self.assertEqual(
            set(environment.inline_environment), {ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY}
        )

    def test_render_post_deploy_payload_preserves_website_bootstrap(self) -> None:
        record = OdooInstanceOverrideRecord(
            context="example",
            instance="testing",
            website_bootstrap=OdooWebsiteBootstrapPayload(
                tenant="example",
                name="Example Site",
                default_lang="en_US",
                homepage_url="/home",
                primary_page_xmlid="example_website.page_home",
                logo_path="addons/example_website/static/src/img/logo.png",
                logo_alt="Example Site",
                canonical_url="https://example-testing.example.com",
                pages_source={
                    "module": "example_website",
                    "data_files": ["views/pages.xml"],
                    "model": "website.page",
                },
                routes_source={"kind": "controller", "module": "website"},
                routes=(
                    OdooWebsiteBootstrapRoute(
                        name="Home",
                        url="/home",
                        module="website",
                        homepage=True,
                    ),
                ),
            ),
            updated_at="2026-05-16T00:00:00Z",
        )

        payload = render_post_deploy_payload(record)
        environment = build_post_deploy_environment(record)
        decoded_payload = json.loads(
            base64.b64decode(
                environment.inline_environment[ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY]
            ).decode("utf-8")
        )

        wire_payload = payload.to_wire_dict()
        self.assertEqual(wire_payload["config_parameters"], [])
        self.assertEqual(wire_payload["addon_settings"], [])
        website_bootstrap = cast("dict[str, object]", wire_payload["website_bootstrap"])
        routes = cast("list[dict[str, object]]", website_bootstrap["routes"])
        self.assertEqual(website_bootstrap["name"], "Example Site")
        self.assertEqual(
            website_bootstrap["canonical_url"], "https://example-testing.example.com"
        )
        self.assertEqual(routes[0]["url"], "/home")
        self.assertEqual(decoded_payload, wire_payload)

    def test_restore_intent_suppresses_website_bootstrap(self) -> None:
        record = OdooInstanceOverrideRecord(
            context="example",
            instance="testing",
            config_parameters=(
                OdooConfigParameterOverride(
                    key="web.base.url",
                    value=OdooOverrideValue(
                        source="literal",
                        value="https://example-testing.example.com",
                    ),
                ),
            ),
            website_bootstrap=OdooWebsiteBootstrapPayload(
                tenant="example",
                name="Example Site",
                canonical_url="https://example-testing.example.com",
                routes=(
                    OdooWebsiteBootstrapRoute(
                        name="Home",
                        url="/home",
                        homepage=True,
                    ),
                ),
            ),
            updated_at="2026-05-16T00:00:00Z",
        )

        payload = render_post_deploy_payload(record, workflow_intent="restore")
        wire_payload = payload.to_wire_dict()
        environment = build_post_deploy_environment(record, workflow_intent="restore")
        decoded_payload = json.loads(
            base64.b64decode(
                environment.inline_environment[ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY]
            ).decode("utf-8")
        )

        self.assertNotIn("website_bootstrap", wire_payload)
        self.assertNotIn("website_bootstrap", decoded_payload)
        self.assertEqual(
            wire_payload["config_parameters"],
            [
                {
                    "key": "web.base.url",
                    "value": {
                        "source": "literal",
                        "value": "https://example-testing.example.com",
                    },
                }
            ],
        )
        self.assertEqual(decoded_payload, wire_payload)

    def test_build_post_deploy_environment_requires_container_env_for_secret_backed_values(
        self,
    ) -> None:
        record = OdooInstanceOverrideRecord(
            context="opw",
            instance="prod",
            addon_settings=(
                OdooAddonSettingOverride(
                    addon="shopify",
                    setting="shop_url_key",
                    value=OdooOverrideValue(source="literal", value="candidate-store"),
                ),
                OdooAddonSettingOverride(
                    addon="shopify",
                    setting="api_token",
                    value=OdooOverrideValue(
                        source="secret_binding",
                        secret_binding_id="secret-binding-shopify-token",
                    ),
                ),
                OdooAddonSettingOverride(
                    addon="shopify",
                    setting="webhook_key",
                    value=OdooOverrideValue(source="literal", value="hook"),
                ),
                OdooAddonSettingOverride(
                    addon="shopify",
                    setting="api_version",
                    value=OdooOverrideValue(source="literal", value="2025-01"),
                ),
            ),
            updated_at="2026-04-23T12:00:00Z",
        )

        environment = build_post_deploy_environment(record)

        self.assertIn(ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY, environment.inline_environment)
        self.assertEqual(
            environment.required_container_environment_keys,
            ("ODOO_OVERRIDE_SECRET__ADDON__SHOPIFY__API_TOKEN",),
        )

    def test_render_post_deploy_payload_injects_shopify_apply_action(self) -> None:
        record = OdooInstanceOverrideRecord(
            context="opw",
            instance="testing",
            addon_settings=(
                OdooAddonSettingOverride(
                    addon="shopify",
                    setting="shop_url_key",
                    value=OdooOverrideValue(source="literal", value="candidate-store"),
                ),
                OdooAddonSettingOverride(
                    addon="shopify",
                    setting="api_token",
                    value=OdooOverrideValue(
                        source="secret_binding",
                        secret_binding_id="secret-binding-shopify-token",
                    ),
                ),
                OdooAddonSettingOverride(
                    addon="shopify",
                    setting="webhook_key",
                    value=OdooOverrideValue(source="literal", value="hook"),
                ),
                OdooAddonSettingOverride(
                    addon="shopify",
                    setting="api_version",
                    value=OdooOverrideValue(source="literal", value="2025-01"),
                ),
                OdooAddonSettingOverride(
                    addon="shopify",
                    setting="test_store",
                    value=OdooOverrideValue(source="literal", value=True),
                ),
                OdooAddonSettingOverride(
                    addon="shopify",
                    setting="production_indicators",
                    value=OdooOverrideValue(source="literal", value="prod"),
                ),
            ),
            updated_at="2026-04-23T12:00:00Z",
        )

        payload = render_post_deploy_payload(record)
        self.assertEqual(
            payload.to_wire_dict()["addon_settings"],
            [
                {
                    "addon": "shopify",
                    "setting": "action",
                    "value": {"source": "literal", "value": "apply"},
                },
                {
                    "addon": "shopify",
                    "setting": "shop_url_key",
                    "value": {"source": "literal", "value": "candidate-store"},
                },
                {
                    "addon": "shopify",
                    "setting": "api_token",
                    "value": {
                        "source": "secret_binding",
                        "secret_binding_id": "secret-binding-shopify-token",
                        "environment_variable": "ODOO_OVERRIDE_SECRET__ADDON__SHOPIFY__API_TOKEN",
                    },
                },
                {
                    "addon": "shopify",
                    "setting": "webhook_key",
                    "value": {"source": "literal", "value": "hook"},
                },
                {
                    "addon": "shopify",
                    "setting": "api_version",
                    "value": {"source": "literal", "value": "2025-01"},
                },
                {
                    "addon": "shopify",
                    "setting": "test_store",
                    "value": {"source": "literal", "value": True},
                },
            ],
        )

    def test_render_post_deploy_payload_injects_shopify_clear_action_for_incomplete_settings(
        self,
    ) -> None:
        record = OdooInstanceOverrideRecord(
            context="opw",
            instance="testing",
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

        payload = render_post_deploy_payload(record)
        environment = build_post_deploy_environment(record)

        self.assertEqual(
            payload.to_wire_dict()["addon_settings"],
            [
                {
                    "addon": "shopify",
                    "setting": "action",
                    "value": {"source": "literal", "value": "clear"},
                }
            ],
        )
        self.assertEqual(environment.required_container_environment_keys, ())

    def test_build_post_deploy_environment_rejects_protected_shopify_store_key(self) -> None:
        record = OdooInstanceOverrideRecord(
            context="opw",
            instance="testing",
            addon_settings=(
                OdooAddonSettingOverride(
                    addon="shopify",
                    setting="shop_url_key",
                    value=OdooOverrideValue(source="literal", value="yps-your-part-supplier"),
                ),
                OdooAddonSettingOverride(
                    addon="shopify",
                    setting="api_token",
                    value=OdooOverrideValue(source="literal", value="token"),
                ),
                OdooAddonSettingOverride(
                    addon="shopify",
                    setting="webhook_key",
                    value=OdooOverrideValue(source="literal", value="hook"),
                ),
                OdooAddonSettingOverride(
                    addon="shopify",
                    setting="api_version",
                    value=OdooOverrideValue(source="literal", value="2025-01"),
                ),
            ),
            updated_at="2026-04-23T12:00:00Z",
        )

        with self.assertRaises(click.ClickException):
            build_post_deploy_environment(
                record,
                protected_shopify_store_keys=("yps-your-part-supplier",),
            )

    def test_build_post_deploy_environment_rejects_production_like_shopify_key(self) -> None:
        record = OdooInstanceOverrideRecord(
            context="opw",
            instance="testing",
            addon_settings=(
                OdooAddonSettingOverride(
                    addon="shopify",
                    setting="shop_url_key",
                    value=OdooOverrideValue(source="literal", value="prod-store"),
                ),
                OdooAddonSettingOverride(
                    addon="shopify",
                    setting="api_token",
                    value=OdooOverrideValue(source="literal", value="token"),
                ),
                OdooAddonSettingOverride(
                    addon="shopify",
                    setting="webhook_key",
                    value=OdooOverrideValue(source="literal", value="hook"),
                ),
                OdooAddonSettingOverride(
                    addon="shopify",
                    setting="api_version",
                    value=OdooOverrideValue(source="literal", value="2025-01"),
                ),
                OdooAddonSettingOverride(
                    addon="shopify",
                    setting="production_indicators",
                    value=OdooOverrideValue(source="literal", value="prod"),
                ),
            ),
            updated_at="2026-04-23T12:00:00Z",
        )

        with self.assertRaises(click.ClickException):
            build_post_deploy_environment(record)

    def test_protected_shopify_store_keys_for_target_definition_reads_target_policies(self) -> None:
        self.assertEqual(
            protected_shopify_store_keys_for_target_definition(
                DokployTargetDefinition(
                    context="opw",
                    instance="testing",
                    target_id="compose-123",
                    policies=DokployTargetPolicies(
                        shopify=DokployTargetShopifyPolicy(
                            protected_store_keys=("yps-your-part-supplier",)
                        )
                    ),
                )
            ),
            ("yps-your-part-supplier",),
        )

    def test_protected_shopify_store_keys_for_target_definition_skips_unconfigured_targets(
        self,
    ) -> None:
        self.assertEqual(
            protected_shopify_store_keys_for_target_definition(
                DokployTargetDefinition(
                    context="opw",
                    instance="prod",
                    target_id="compose-123",
                )
            ),
            (),
        )


if __name__ == "__main__":
    unittest.main()
