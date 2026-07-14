import base64
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import click
from control_plane.odoo_instance_overrides import LAUNCHPLANE_INSTANCE_OVERRIDES_REQUIRED_ENV_KEY
from control_plane.odoo_instance_overrides import LAUNCHPLANE_WEBSITE_BOOTSTRAP_REQUIRED_ENV_KEY
from control_plane.odoo_instance_overrides import ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY
from control_plane.odoo_instance_overrides import build_post_deploy_environment
from control_plane.odoo_instance_overrides import render_post_deploy_payload
from control_plane.odoo_post_deploy_http import OdooWebsiteBootstrapOverrideRequest
from click.testing import CliRunner, Result
from pydantic import ValidationError

from control_plane.cli import main
from control_plane.cli import _run_compose_post_deploy_update
from control_plane.dokploy import DokploySourceOfTruth, DokployTargetDefinition
from control_plane.contracts.dokploy_target_record import (
    DokployTargetPolicies,
    DokployTargetShopifyPolicy,
)
from control_plane.contracts.odoo_instance_override_record import (
    OdooAddonSettingOverride,
    OdooConfigParameterOverride,
    OdooInstanceOverrideRecord,
    OdooOverrideApplyResult,
    OdooOverrideValue,
    OdooWebsiteBootstrapPayload,
    OdooWebsiteBootstrapRoute,
    validate_odoo_website_bootstrap_contract,
)
from control_plane.contracts.secret_record import SecretBinding, SecretRecord, SecretVersion
from control_plane.contracts.ship_request import ShipRequest
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.cli import _allow_direct_db_mutation_argument


def _sqlite_database_url(database_path: Path) -> str:
    return f"sqlite+pysqlite:///{database_path}"


def _ship_request() -> ShipRequest:
    return ShipRequest(
        artifact_id="artifact-123",
        context="opw",
        instance="prod",
        source_git_ref="abc123",
        target_name="opw-prod",
        target_type="compose",
        provider_id="dokploy",
        target_category="compose",
        provider_target_type="compose",
        deploy_mode="dokploy-compose-api",
    )


def _assert_direct_db_mutation_rejected(test_case: unittest.TestCase, result: Result) -> None:
    test_case.assertNotEqual(result.exit_code, 0)
    test_case.assertIn("Direct local DB mutation is restricted", result.output)
    test_case.assertIn("--allow-direct-db-mutation", result.output)


class OdooInstanceOverrideTests(unittest.TestCase):
    def test_website_bootstrap_payload_accepts_devkit_route_shape(self) -> None:
        payload = validate_odoo_website_bootstrap_contract(
            OdooWebsiteBootstrapPayload(
                tenant=" opw ",
                name=" OPW ",
                canonical_url=" https://opw-testing.example.com/ ",
                homepage_url=" /shop ",
                primary_page_xmlid="website_sale.shop",
                routes=(
                    OdooWebsiteBootstrapRoute(
                        name=" Shop ",
                        url=" /shop ",
                        module=" website_sale ",
                        published=True,
                        homepage=True,
                    ),
                ),
            )
        )

        self.assertEqual(payload.tenant, "opw")
        self.assertEqual(payload.name, "OPW")
        self.assertEqual(payload.homepage_url, "/shop")
        self.assertEqual(payload.primary_page_xmlid, "website_sale.shop")
        self.assertEqual(payload.routes[0].url, "/shop")
        self.assertEqual(payload.routes[0].module, "website_sale")

    def test_website_bootstrap_payload_load_remains_tolerant_for_stored_records(self) -> None:
        payload = OdooWebsiteBootstrapPayload(
            name="OPW",
            homepage_url="https://opw-testing.example.com/shop",
            primary_page_xmlid="legacy_page",
            routes=(
                OdooWebsiteBootstrapRoute(name="Shop", url="shop", homepage=True),
                OdooWebsiteBootstrapRoute(name="Home", url="/", homepage=True),
                OdooWebsiteBootstrapRoute(name="Shop", url="shop"),
            ),
        )

        self.assertEqual(payload.homepage_url, "https://opw-testing.example.com/shop")
        self.assertEqual(payload.primary_page_xmlid, "legacy_page")
        self.assertEqual(payload.routes[0].url, "shop")
        self.assertEqual(payload.routes[2].name, "Shop")

    def test_website_bootstrap_contract_rejects_non_local_homepage_url(self) -> None:
        with self.assertRaisesRegex(ValueError, "homepage_url.*local route path"):
            validate_odoo_website_bootstrap_contract(
                OdooWebsiteBootstrapPayload(
                    name="OPW",
                    homepage_url="https://opw-testing.example.com/shop",
                )
            )

    def test_website_bootstrap_contract_rejects_protocol_relative_homepage_url(self) -> None:
        with self.assertRaisesRegex(ValueError, "homepage_url.*local route path"):
            validate_odoo_website_bootstrap_contract(
                OdooWebsiteBootstrapPayload(
                    name="OPW",
                    homepage_url="//example.com/shop",
                )
            )

    def test_website_bootstrap_contract_rejects_non_local_route_url(self) -> None:
        with self.assertRaisesRegex(ValueError, "route url.*local route path"):
            validate_odoo_website_bootstrap_contract(
                OdooWebsiteBootstrapPayload(
                    name="OPW",
                    routes=(OdooWebsiteBootstrapRoute(name="Shop", url="shop"),),
                )
            )

    def test_website_bootstrap_contract_rejects_protocol_relative_route_url(self) -> None:
        with self.assertRaisesRegex(ValueError, "route url.*local route path"):
            validate_odoo_website_bootstrap_contract(
                OdooWebsiteBootstrapPayload(
                    name="OPW",
                    routes=(OdooWebsiteBootstrapRoute(name="Shop", url="//example.com/shop"),),
                )
            )

    def test_website_bootstrap_contract_accepts_root_route(self) -> None:
        payload = validate_odoo_website_bootstrap_contract(
            OdooWebsiteBootstrapPayload(
                name="OPW",
                homepage_url="/",
                routes=(OdooWebsiteBootstrapRoute(name="Home", url="/", homepage=True),),
            )
        )

        self.assertEqual(payload.homepage_url, "/")
        self.assertEqual(payload.routes[0].url, "/")

    def test_website_bootstrap_contract_rejects_bad_primary_page_xmlid(self) -> None:
        with self.assertRaisesRegex(ValueError, "primary_page_xmlid.*dotted XML ID"):
            validate_odoo_website_bootstrap_contract(
                OdooWebsiteBootstrapPayload(
                    name="Cell Mechanic",
                    primary_page_xmlid="bad xml id",
                )
            )

    def test_website_bootstrap_contract_accepts_hyphenated_record_xmlid(self) -> None:
        payload = validate_odoo_website_bootstrap_contract(
            OdooWebsiteBootstrapPayload(
                name="Cell Mechanic",
                primary_page_xmlid="cm_website.website-page-cell-mechanic",
            )
        )

        self.assertEqual(payload.primary_page_xmlid, "cm_website.website-page-cell-mechanic")

    def test_website_bootstrap_contract_rejects_multiple_homepage_routes(self) -> None:
        with self.assertRaisesRegex(ValueError, "multiple homepage routes"):
            validate_odoo_website_bootstrap_contract(
                OdooWebsiteBootstrapPayload(
                    name="OPW",
                    routes=(
                        OdooWebsiteBootstrapRoute(name="Shop", url="/shop", homepage=True),
                        OdooWebsiteBootstrapRoute(name="Home", url="/", homepage=True),
                    ),
                )
            )

    def test_website_bootstrap_contract_rejects_duplicate_route_names(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate route names"):
            validate_odoo_website_bootstrap_contract(
                OdooWebsiteBootstrapPayload(
                    name="OPW",
                    routes=(
                        OdooWebsiteBootstrapRoute(name="Shop", url="/shop"),
                        OdooWebsiteBootstrapRoute(name="Shop", url="/store"),
                    ),
                )
            )

    def test_website_bootstrap_contract_rejects_duplicate_route_urls(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate route urls"):
            validate_odoo_website_bootstrap_contract(
                OdooWebsiteBootstrapPayload(
                    name="OPW",
                    routes=(
                        OdooWebsiteBootstrapRoute(name="Shop", url="/shop"),
                        OdooWebsiteBootstrapRoute(name="Store", url="/shop"),
                    ),
                )
            )

    def test_website_bootstrap_override_request_enforces_write_contract(self) -> None:
        with self.assertRaisesRegex(ValidationError, "homepage_url.*local route path"):
            OdooWebsiteBootstrapOverrideRequest(
                product="odoo",
                context="opw",
                instance="testing",
                website_bootstrap=OdooWebsiteBootstrapPayload(
                    name="OPW",
                    homepage_url="//example.com/shop",
                ),
            )

    def test_record_rejects_duplicate_config_parameter_keys(self) -> None:
        with self.assertRaisesRegex(ValidationError, "duplicate config parameter keys"):
            OdooInstanceOverrideRecord(
                context="opw",
                instance="prod",
                config_parameters=(
                    OdooConfigParameterOverride(
                        key="web.base.url",
                        value=OdooOverrideValue(source="literal", value="https://prod.example.com"),
                    ),
                    OdooConfigParameterOverride(
                        key="WEB.BASE.URL",
                        value=OdooOverrideValue(
                            source="literal", value="https://other.example.com"
                        ),
                    ),
                ),
                updated_at="2026-04-21T18:30:00Z",
            )

    def test_apply_result_requires_completed_timestamp(self) -> None:
        with self.assertRaisesRegex(ValidationError, "requires applied_at"):
            OdooOverrideApplyResult(attempted=True, status="pass")

    def test_render_post_deploy_payload_returns_typed_v1_wire_payload(self) -> None:
        record = OdooInstanceOverrideRecord(
            context="OPW",
            instance="Testing",
            apply_on=("deploy",),
            config_parameters=(
                OdooConfigParameterOverride(
                    key="WEB.BASE.URL",
                    value=OdooOverrideValue(
                        source="literal", value="https://opw-testing.example.com"
                    ),
                ),
            ),
            addon_settings=(
                OdooAddonSettingOverride(
                    addon="openai",
                    setting="api-key",
                    value=OdooOverrideValue(
                        source="secret_binding",
                        secret_binding_id="secret-opw-openai",
                    ),
                ),
            ),
            updated_at="2026-04-21T18:30:00Z",
        )

        payload = render_post_deploy_payload(record)

        self.assertEqual(payload.context, "opw")
        self.assertEqual(payload.instance, "testing")
        self.assertEqual(
            payload.required_container_environment_keys,
            ("ODOO_OVERRIDE_SECRET__ADDON__OPENAI__API_KEY",),
        )
        self.assertEqual(payload.override_count, 2)
        self.assertEqual(
            payload.to_wire_dict(),
            {
                "schema_version": 1,
                "context": "opw",
                "instance": "testing",
                "config_parameters": [
                    {
                        "key": "web.base.url",
                        "value": {
                            "source": "literal",
                            "value": "https://opw-testing.example.com",
                        },
                    }
                ],
                "addon_settings": [
                    {
                        "addon": "openai",
                        "setting": "api-key",
                        "value": {
                            "source": "secret_binding",
                            "secret_binding_id": "secret-opw-openai",
                            "environment_variable": "ODOO_OVERRIDE_SECRET__ADDON__OPENAI__API_KEY",
                        },
                    }
                ],
            },
        )

    def test_build_post_deploy_environment_reuses_typed_payload_required_keys(self) -> None:
        record = OdooInstanceOverrideRecord(
            context="opw",
            instance="testing",
            addon_settings=(
                OdooAddonSettingOverride(
                    addon="openai",
                    setting="api_key",
                    value=OdooOverrideValue(
                        source="secret_binding",
                        secret_binding_id="secret-opw-openai",
                    ),
                ),
            ),
            updated_at="2026-04-21T18:30:00Z",
        )

        environment = build_post_deploy_environment(record)

        self.assertEqual(
            environment.required_container_environment_keys,
            environment.payload.required_container_environment_keys,
        )
        decoded_payload = json.loads(
            base64.b64decode(
                environment.inline_environment[ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY]
            ).decode("utf-8")
        )
        self.assertEqual(decoded_payload, environment.payload.to_wire_dict())
        self.assertEqual(
            environment.inline_environment[LAUNCHPLANE_INSTANCE_OVERRIDES_REQUIRED_ENV_KEY],
            "true",
        )
        self.assertNotIn(
            LAUNCHPLANE_WEBSITE_BOOTSTRAP_REQUIRED_ENV_KEY,
            environment.inline_environment,
        )

    def test_build_post_deploy_environment_sets_website_bootstrap_required_flag(self) -> None:
        record = OdooInstanceOverrideRecord(
            context="cm",
            instance="testing",
            website_bootstrap=OdooWebsiteBootstrapPayload(
                tenant="cm",
                name="Cell Mechanic",
                canonical_url="https://cm-testing.example.com",
            ),
            updated_at="2026-06-13T18:00:00Z",
        )

        environment = build_post_deploy_environment(record)

        decoded_payload = json.loads(
            base64.b64decode(
                environment.inline_environment[ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY]
            ).decode("utf-8")
        )
        self.assertIn("website_bootstrap", decoded_payload)
        self.assertEqual(
            environment.inline_environment[LAUNCHPLANE_WEBSITE_BOOTSTRAP_REQUIRED_ENV_KEY],
            "true",
        )
        self.assertNotIn(
            LAUNCHPLANE_INSTANCE_OVERRIDES_REQUIRED_ENV_KEY,
            environment.inline_environment,
        )

    def test_cli_put_config_param_does_not_echo_plaintext_value(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            runner = CliRunner()

            result = runner.invoke(
                main,
                [
                    "odoo-overrides",
                    "put-config-param",
                    "--database-url",
                    database_url,
                    "--context",
                    "OPW",
                    "--instance",
                    "Prod",
                    "--key",
                    "web.base.url",
                    "--value",
                    "https://opw-prod.example.com",
                    "--source-label",
                    "test",
                    *_allow_direct_db_mutation_argument(),
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn('"web.base.url"', result.output)
            self.assertIn('"deploy"', result.output)
            self.assertIn('"skipped"', result.output)
            self.assertNotIn("https://opw-prod.example.com", result.output)

            store = PostgresRecordStore(database_url=database_url)
            stored_record = store.read_odoo_instance_override_record(
                context_name="opw", instance_name="prod"
            )
            store.close()

        self.assertEqual(
            stored_record.config_parameters[0].value.value, "https://opw-prod.example.com"
        )

    def test_cli_put_config_param_requires_direct_db_acknowledgement(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            result = CliRunner().invoke(
                main,
                [
                    "odoo-overrides",
                    "put-config-param",
                    "--database-url",
                    database_url,
                    "--context",
                    "opw",
                    "--instance",
                    "prod",
                    "--key",
                    "web.base.url",
                    "--value",
                    "https://opw-prod.example.com",
                ],
            )

        _assert_direct_db_mutation_rejected(self, result)

    def test_cli_put_config_param_adds_deploy_phases_to_existing_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            store.write_odoo_instance_override_record(
                OdooInstanceOverrideRecord(
                    context="opw",
                    instance="prod",
                    apply_on=("manual",),
                    config_parameters=(
                        OdooConfigParameterOverride(
                            key="web.base.url",
                            value=OdooOverrideValue(
                                source="literal", value="https://old.example.com"
                            ),
                        ),
                    ),
                    updated_at="2026-05-17T00:00:00Z",
                    source_label="test",
                )
            )
            store.close()
            runner = CliRunner()

            result = runner.invoke(
                main,
                [
                    "odoo-overrides",
                    "put-config-param",
                    "--database-url",
                    database_url,
                    "--context",
                    "opw",
                    "--instance",
                    "prod",
                    "--key",
                    "web.base.url",
                    "--value",
                    "https://opw-prod.example.com",
                    *_allow_direct_db_mutation_argument(),
                ],
            )
            store = PostgresRecordStore(database_url=database_url)
            stored_record = store.read_odoo_instance_override_record(
                context_name="opw", instance_name="prod"
            )
            store.close()

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(stored_record.apply_on, ("manual", "deploy", "promotion"))

    def test_cli_put_addon_setting_requires_secret_binding_for_secret_shaped_setting(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            runner = CliRunner()

            result = runner.invoke(
                main,
                [
                    "odoo-overrides",
                    "put-addon-setting",
                    "--database-url",
                    database_url,
                    "--context",
                    "opw",
                    "--instance",
                    "prod",
                    "--addon",
                    "shopify",
                    "--setting",
                    "api_token",
                    "--value",
                    "plain-token",
                    *_allow_direct_db_mutation_argument(),
                ],
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("must use --secret-binding-id", result.output)

    def test_cli_put_addon_setting_accepts_secret_binding_and_lists_metadata(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            runner = CliRunner()
            put_result = runner.invoke(
                main,
                [
                    "odoo-overrides",
                    "put-addon-setting",
                    "--database-url",
                    database_url,
                    "--context",
                    "opw",
                    "--instance",
                    "prod",
                    "--addon",
                    "shopify",
                    "--setting",
                    "api_token",
                    "--secret-binding-id",
                    "secret-binding-shopify-token",
                    *_allow_direct_db_mutation_argument(),
                ],
            )
            list_result = runner.invoke(
                main, ["odoo-overrides", "list", "--database-url", database_url]
            )

        self.assertEqual(put_result.exit_code, 0, msg=put_result.output)
        self.assertEqual(list_result.exit_code, 0, msg=list_result.output)
        payload = json.loads(list_result.output)
        self.assertEqual(payload["records"][0]["addon_settings"], ["shopify.api_token"])
        self.assertNotIn("secret-binding-shopify-token", list_result.output)

    def test_cli_put_addon_setting_requires_direct_db_acknowledgement(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            result = CliRunner().invoke(
                main,
                [
                    "odoo-overrides",
                    "put-addon-setting",
                    "--database-url",
                    database_url,
                    "--context",
                    "opw",
                    "--instance",
                    "prod",
                    "--addon",
                    "shopify",
                    "--setting",
                    "shop_url_key",
                    "--value",
                    "candidate-store",
                ],
            )

        _assert_direct_db_mutation_rejected(self, result)

    def test_cli_put_addon_setting_adds_deploy_phases_to_existing_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            store.write_odoo_instance_override_record(
                OdooInstanceOverrideRecord(
                    context="opw",
                    instance="prod",
                    apply_on=("manual",),
                    addon_settings=(
                        OdooAddonSettingOverride(
                            addon="shopify",
                            setting="api_token",
                            value=OdooOverrideValue(
                                source="secret_binding",
                                secret_binding_id="secret-binding-old-shopify-token",
                            ),
                        ),
                    ),
                    updated_at="2026-05-17T00:00:00Z",
                    source_label="test",
                )
            )
            store.close()
            runner = CliRunner()

            result = runner.invoke(
                main,
                [
                    "odoo-overrides",
                    "put-addon-setting",
                    "--database-url",
                    database_url,
                    "--context",
                    "opw",
                    "--instance",
                    "prod",
                    "--addon",
                    "shopify",
                    "--setting",
                    "api_token",
                    "--secret-binding-id",
                    "secret-binding-shopify-token",
                    *_allow_direct_db_mutation_argument(),
                ],
            )
            store = PostgresRecordStore(database_url=database_url)
            stored_record = store.read_odoo_instance_override_record(
                context_name="opw", instance_name="prod"
            )
            store.close()

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(stored_record.apply_on, ("manual", "deploy", "promotion"))

    def test_cli_put_website_bootstrap_preserves_existing_overrides(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temp_dir = Path(temporary_directory_name)
            database_url = _sqlite_database_url(temp_dir / "launchplane.sqlite3")
            payload_file = temp_dir / "website-bootstrap.json"
            payload_file.write_text(
                json.dumps(
                    {
                        "tenant": "opw",
                        "name": "OPW",
                        "canonical_url": "https://opw-testing.example.com",
                        "homepage_url": "/shop",
                        "routes": [
                            {
                                "name": "Shop",
                                "url": "/shop",
                                "module": "website_sale",
                                "homepage": True,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            runner = CliRunner()
            put_config_result = runner.invoke(
                main,
                [
                    "odoo-overrides",
                    "put-config-param",
                    "--database-url",
                    database_url,
                    "--context",
                    "opw",
                    "--instance",
                    "testing",
                    "--key",
                    "web.base.url",
                    "--value",
                    "https://opw-testing.example.com",
                    *_allow_direct_db_mutation_argument(),
                ],
            )
            put_bootstrap_result = runner.invoke(
                main,
                [
                    "odoo-overrides",
                    "put-website-bootstrap",
                    "--database-url",
                    database_url,
                    "--context",
                    "opw",
                    "--instance",
                    "testing",
                    "--payload-file",
                    str(payload_file),
                    *_allow_direct_db_mutation_argument(),
                ],
            )
            store = PostgresRecordStore(database_url=database_url)
            stored_record = store.read_odoo_instance_override_record(
                context_name="opw", instance_name="testing"
            )
            store.close()

        self.assertEqual(put_config_result.exit_code, 0, msg=put_config_result.output)
        self.assertEqual(put_bootstrap_result.exit_code, 0, msg=put_bootstrap_result.output)
        self.assertEqual(stored_record.config_parameters[0].key, "web.base.url")
        self.assertIsNotNone(stored_record.website_bootstrap)
        assert stored_record.website_bootstrap is not None
        self.assertEqual(stored_record.website_bootstrap.name, "OPW")
        self.assertEqual(stored_record.website_bootstrap.routes[0].url, "/shop")

    def test_cli_put_website_bootstrap_adds_deploy_and_promotion_apply_phases(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temp_dir = Path(temporary_directory_name)
            database_url = _sqlite_database_url(temp_dir / "launchplane.sqlite3")
            payload_file = temp_dir / "website-bootstrap.json"
            payload_file.write_text(
                json.dumps(
                    {
                        "tenant": "opw",
                        "name": "OPW",
                        "canonical_url": "https://opw-testing.example.com",
                    }
                ),
                encoding="utf-8",
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            store.write_odoo_instance_override_record(
                OdooInstanceOverrideRecord(
                    context="opw",
                    instance="testing",
                    apply_on=("manual",),
                    config_parameters=(
                        OdooConfigParameterOverride(
                            key="web.base.url",
                            value=OdooOverrideValue(
                                source="literal", value="https://opw-testing.example.com"
                            ),
                        ),
                    ),
                    updated_at="2026-05-17T00:00:00Z",
                    source_label="test",
                )
            )
            store.close()
            runner = CliRunner()
            put_bootstrap_result = runner.invoke(
                main,
                [
                    "odoo-overrides",
                    "put-website-bootstrap",
                    "--database-url",
                    database_url,
                    "--context",
                    "opw",
                    "--instance",
                    "testing",
                    "--payload-file",
                    str(payload_file),
                    *_allow_direct_db_mutation_argument(),
                ],
            )
            store = PostgresRecordStore(database_url=database_url)
            stored_record = store.read_odoo_instance_override_record(
                context_name="opw", instance_name="testing"
            )
            store.close()

        self.assertEqual(put_bootstrap_result.exit_code, 0, msg=put_bootstrap_result.output)
        self.assertEqual(stored_record.apply_on, ("manual", "deploy", "promotion"))

    def test_cli_put_website_bootstrap_requires_direct_db_acknowledgement(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temp_dir = Path(temporary_directory_name)
            database_url = _sqlite_database_url(temp_dir / "launchplane.sqlite3")
            payload_file = temp_dir / "website-bootstrap.json"
            payload_file.write_text(json.dumps({"tenant": "opw", "name": "OPW"}), encoding="utf-8")
            result = CliRunner().invoke(
                main,
                [
                    "odoo-overrides",
                    "put-website-bootstrap",
                    "--database-url",
                    database_url,
                    "--context",
                    "opw",
                    "--instance",
                    "testing",
                    "--payload-file",
                    str(payload_file),
                ],
            )

        _assert_direct_db_mutation_rejected(self, result)

    def test_cli_put_website_bootstrap_reports_schema_errors(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temp_dir = Path(temporary_directory_name)
            database_url = _sqlite_database_url(temp_dir / "launchplane.sqlite3")
            payload_file = temp_dir / "website-bootstrap.json"
            payload_file.write_text(json.dumps({"tenant": "opw"}), encoding="utf-8")
            runner = CliRunner()
            put_bootstrap_result = runner.invoke(
                main,
                [
                    "odoo-overrides",
                    "put-website-bootstrap",
                    "--database-url",
                    database_url,
                    "--context",
                    "opw",
                    "--instance",
                    "testing",
                    "--payload-file",
                    str(payload_file),
                    *_allow_direct_db_mutation_argument(),
                ],
            )

        self.assertNotEqual(put_bootstrap_result.exit_code, 0)
        self.assertIn("Invalid Odoo website bootstrap payload", put_bootstrap_result.output)
        self.assertIn("name", put_bootstrap_result.output)

    def test_cli_put_website_bootstrap_reports_contract_errors(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temp_dir = Path(temporary_directory_name)
            database_url = _sqlite_database_url(temp_dir / "launchplane.sqlite3")
            payload_file = temp_dir / "website-bootstrap.json"
            payload_file.write_text(
                json.dumps({"tenant": "opw", "name": "OPW", "homepage_url": "//example.com"}),
                encoding="utf-8",
            )
            runner = CliRunner()
            put_bootstrap_result = runner.invoke(
                main,
                [
                    "odoo-overrides",
                    "put-website-bootstrap",
                    "--database-url",
                    database_url,
                    "--context",
                    "opw",
                    "--instance",
                    "testing",
                    "--payload-file",
                    str(payload_file),
                    *_allow_direct_db_mutation_argument(),
                ],
            )

        self.assertNotEqual(put_bootstrap_result.exit_code, 0)
        self.assertIn("Invalid Odoo website bootstrap payload", put_bootstrap_result.output)
        self.assertIn("homepage_url", put_bootstrap_result.output)

    def test_cli_mark_apply_updates_result_metadata(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            runner = CliRunner()
            put_result = runner.invoke(
                main,
                [
                    "odoo-overrides",
                    "put-config-param",
                    "--database-url",
                    database_url,
                    "--context",
                    "opw",
                    "--instance",
                    "prod",
                    "--key",
                    "web.base.url",
                    "--value",
                    "https://opw-prod.example.com",
                    *_allow_direct_db_mutation_argument(),
                ],
            )
            mark_result = runner.invoke(
                main,
                [
                    "odoo-overrides",
                    "mark-apply",
                    "--database-url",
                    database_url,
                    "--context",
                    "opw",
                    "--instance",
                    "prod",
                    "--status",
                    "pass",
                    "--applied-at",
                    "2026-04-23T12:00:00Z",
                    "--detail",
                    "Applied through test driver.",
                    *_allow_direct_db_mutation_argument(),
                ],
            )

            store = PostgresRecordStore(database_url=database_url)
            stored_record = store.read_odoo_instance_override_record(
                context_name="opw", instance_name="prod"
            )
            store.close()

        self.assertEqual(put_result.exit_code, 0, msg=put_result.output)
        self.assertEqual(mark_result.exit_code, 0, msg=mark_result.output)
        self.assertIn('"last_apply_status": "pass"', mark_result.output)
        self.assertEqual(stored_record.last_apply.status, "pass")
        self.assertEqual(stored_record.last_apply.applied_at, "2026-04-23T12:00:00Z")

    def test_cli_mark_apply_requires_direct_db_acknowledgement(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            result = CliRunner().invoke(
                main,
                [
                    "odoo-overrides",
                    "mark-apply",
                    "--database-url",
                    database_url,
                    "--context",
                    "opw",
                    "--instance",
                    "prod",
                    "--status",
                    "pass",
                ],
            )

        _assert_direct_db_mutation_rejected(self, result)

    def test_post_deploy_update_renders_literal_odoo_overrides_and_marks_pass(self) -> None:
        def capture_post_deploy_update(**kwargs: object) -> None:
            workflow_environment_overrides = kwargs["workflow_environment_overrides"]
            protected_shopify_store_keys = kwargs["protected_shopify_store_keys"]
            assert isinstance(workflow_environment_overrides, dict)
            assert isinstance(protected_shopify_store_keys, tuple)
            captured_workflow_environment.update(workflow_environment_overrides)
            captured_protected_shopify_store_keys.extend(protected_shopify_store_keys)

        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            store.write_odoo_instance_override_record(
                OdooInstanceOverrideRecord(
                    context="opw",
                    instance="prod",
                    config_parameters=(
                        OdooConfigParameterOverride(
                            key="web.base.url",
                            value=OdooOverrideValue(
                                source="literal", value="https://opw-prod.example.com"
                            ),
                        ),
                    ),
                    updated_at="2026-04-23T12:00:00Z",
                )
            )
            source_of_truth = DokploySourceOfTruth(
                schema_version=1,
                targets=(
                    DokployTargetDefinition(
                        context="opw",
                        instance="prod",
                        target_type="compose",
                        target_name="opw-prod",
                        target_id="compose-123",
                        policies=DokployTargetPolicies(
                            shopify=DokployTargetShopifyPolicy(
                                protected_store_keys=("yps-your-part-supplier",)
                            )
                        ),
                    ),
                ),
            )
            captured_workflow_environment: dict[str, str] = {}
            captured_protected_shopify_store_keys: list[str] = []

            with (
                patch.dict("os.environ", {"LAUNCHPLANE_DATABASE_URL": database_url}, clear=False),
                patch(
                    "control_plane.dokploy.source.read_dokploy_config",
                    return_value=("https://dokploy.example.com", "token-123"),
                ),
                patch(
                    "control_plane.dokploy.source.read_control_plane_dokploy_source_of_truth",
                    return_value=source_of_truth,
                ),
                patch(
                    "control_plane.dokploy.post_deploy.run_compose_post_deploy_update",
                    side_effect=capture_post_deploy_update,
                ),
            ):
                _run_compose_post_deploy_update(env_file=None, request=_ship_request())

            stored_record = store.read_odoo_instance_override_record(
                context_name="opw", instance_name="prod"
            )
            store.close()

        payload = json.loads(
            base64.b64decode(
                captured_workflow_environment[ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY]
            ).decode("utf-8")
        )
        self.assertEqual(
            payload["config_parameters"],
            [
                {
                    "key": "web.base.url",
                    "value": {
                        "source": "literal",
                        "value": "https://opw-prod.example.com",
                    },
                }
            ],
        )
        self.assertNotIn(
            "ENV_OVERRIDE_CONFIG_PARAM__WEB__BASE__URL",
            captured_workflow_environment,
        )
        self.assertEqual(captured_protected_shopify_store_keys, ["yps-your-part-supplier"])
        self.assertEqual(stored_record.last_apply.status, "pass")

    def test_post_deploy_update_requires_container_env_for_secret_backed_odoo_overrides(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            store.write_odoo_instance_override_record(
                OdooInstanceOverrideRecord(
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
            )
            source_of_truth = DokploySourceOfTruth(
                schema_version=1,
                targets=(
                    DokployTargetDefinition(
                        context="opw",
                        instance="prod",
                        target_type="compose",
                        target_name="opw-prod",
                        target_id="compose-123",
                    ),
                ),
            )
            captured_required_environment_keys: list[str] = []

            with (
                patch.dict("os.environ", {"LAUNCHPLANE_DATABASE_URL": database_url}, clear=False),
                patch(
                    "control_plane.dokploy.source.read_dokploy_config",
                    return_value=("https://dokploy.example.com", "token-123"),
                ),
                patch(
                    "control_plane.dokploy.source.read_control_plane_dokploy_source_of_truth",
                    return_value=source_of_truth,
                ),
                patch(
                    "control_plane.dokploy.post_deploy.run_compose_post_deploy_update",
                    side_effect=lambda **kwargs: captured_required_environment_keys.extend(
                        kwargs["required_workflow_environment_keys"]
                    ),
                ),
            ):
                _run_compose_post_deploy_update(env_file=None, request=_ship_request())

            stored_record = store.read_odoo_instance_override_record(
                context_name="opw", instance_name="prod"
            )
            store.close()

        self.assertEqual(
            captured_required_environment_keys,
            ["ODOO_OVERRIDE_SECRET__ADDON__SHOPIFY__API_TOKEN"],
        )
        self.assertEqual(stored_record.last_apply.status, "pass")

    def test_post_deploy_update_requires_database_for_override_authority(self) -> None:
        source_of_truth = DokploySourceOfTruth(
            schema_version=1,
            targets=(
                DokployTargetDefinition(
                    context="opw",
                    instance="prod",
                    target_type="compose",
                    target_name="opw-prod",
                    target_id="compose-123",
                ),
            ),
        )

        with (
            patch.dict("os.environ", {}, clear=True),
            patch(
                "control_plane.dokploy.source.read_dokploy_config",
                return_value=("https://dokploy.example.com", "token-123"),
            ),
            patch(
                "control_plane.dokploy.source.read_control_plane_dokploy_source_of_truth",
                return_value=source_of_truth,
            ),
            patch("control_plane.dokploy.post_deploy.run_compose_post_deploy_update") as runner,
        ):
            with self.assertRaisesRegex(click.ClickException, "LAUNCHPLANE_DATABASE_URL"):
                _run_compose_post_deploy_update(env_file=None, request=_ship_request())

        runner.assert_not_called()

    def test_cli_migrate_secret_transport_relabels_secret_bindings_without_plaintext(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            store.write_secret_record(
                SecretRecord(
                    secret_id="secret-runtime-shopify-token",
                    scope="context",
                    integration="runtime_environment",
                    name="shopify-api-token",
                    context="opw",
                    current_version_id="secret-runtime-shopify-token-version-1",
                    created_at="2026-04-23T12:00:00Z",
                    updated_at="2026-04-23T12:00:00Z",
                )
            )
            store.write_secret_version(
                SecretVersion(
                    version_id="secret-runtime-shopify-token-version-1",
                    secret_id="secret-runtime-shopify-token",
                    created_at="2026-04-23T12:00:00Z",
                    ciphertext="encrypted-value-placeholder",
                )
            )
            store.write_secret_binding(
                SecretBinding(
                    binding_id="secret-runtime-shopify-token-binding-env-override-shopify-api-token",
                    secret_id="secret-runtime-shopify-token",
                    integration="runtime_environment",
                    binding_key="ENV_OVERRIDE_SHOPIFY__API_TOKEN",
                    context="opw",
                    created_at="2026-04-23T12:00:00Z",
                    updated_at="2026-04-23T12:00:00Z",
                )
            )
            store.write_odoo_instance_override_record(
                OdooInstanceOverrideRecord(
                    context="opw",
                    instance="prod",
                    addon_settings=(
                        OdooAddonSettingOverride(
                            addon="shopify",
                            setting="api_token",
                            value=OdooOverrideValue(
                                source="secret_binding",
                                secret_binding_id="secret-runtime-shopify-token-binding-env-override-shopify-api-token",
                            ),
                        ),
                    ),
                    updated_at="2026-04-23T12:00:00Z",
                )
            )
            store.write_odoo_instance_override_record(
                OdooInstanceOverrideRecord(
                    context="opw",
                    instance="testing",
                    addon_settings=(
                        OdooAddonSettingOverride(
                            addon="shopify",
                            setting="api_token",
                            value=OdooOverrideValue(
                                source="secret_binding",
                                secret_binding_id="secret-runtime-shopify-token-binding-env-override-shopify-api-token",
                            ),
                        ),
                    ),
                    updated_at="2026-04-23T12:00:00Z",
                )
            )
            store.close()

            runner = CliRunner()
            result = runner.invoke(
                main,
                [
                    "odoo-overrides",
                    "migrate-secret-transport",
                    "--database-url",
                    database_url,
                    "--context",
                    "opw",
                    "--apply",
                    *_allow_direct_db_mutation_argument(),
                ],
            )

            store = PostgresRecordStore(database_url=database_url)
            migrated_prod_record = store.read_odoo_instance_override_record(
                context_name="opw", instance_name="prod"
            )
            migrated_testing_record = store.read_odoo_instance_override_record(
                context_name="opw", instance_name="testing"
            )
            bindings = store.list_secret_bindings(integration="runtime_environment", limit=None)
            store.close()

        self.assertEqual(result.exit_code, 0, msg=result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["changed_record_count"], 2)
        self.assertEqual(
            migrated_prod_record.addon_settings[0].value.secret_binding_id,
            "secret-runtime-shopify-token-binding-odoo-override-secret-addon-shopify-api-token",
        )
        self.assertEqual(
            migrated_testing_record.addon_settings[0].value.secret_binding_id,
            "secret-runtime-shopify-token-binding-odoo-override-secret-addon-shopify-api-token",
        )
        binding_status_by_key = {binding.binding_key: binding.status for binding in bindings}
        self.assertEqual(binding_status_by_key["ENV_OVERRIDE_SHOPIFY__API_TOKEN"], "disabled")
        self.assertEqual(
            binding_status_by_key["ODOO_OVERRIDE_SECRET__ADDON__SHOPIFY__API_TOKEN"],
            "configured",
        )
        self.assertNotIn("encrypted-value-placeholder", result.output)

    def test_cli_migrate_secret_transport_apply_requires_direct_db_acknowledgement(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            result = CliRunner().invoke(
                main,
                [
                    "odoo-overrides",
                    "migrate-secret-transport",
                    "--database-url",
                    database_url,
                    "--apply",
                ],
            )

        _assert_direct_db_mutation_rejected(self, result)

    def test_cli_migrate_secret_transport_dry_run_does_not_require_direct_db_acknowledgement(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            store.close()
            with patch.object(
                PostgresRecordStore,
                "ensure_schema",
                side_effect=AssertionError("dry-run must not ensure schema"),
            ):
                result = CliRunner().invoke(
                    main,
                    [
                        "odoo-overrides",
                        "migrate-secret-transport",
                        "--database-url",
                        database_url,
                    ],
                )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["mode"], "dry-run")


if __name__ == "__main__":
    unittest.main()
