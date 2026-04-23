import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import click
from click.testing import CliRunner
from pydantic import ValidationError

from control_plane.cli import main
from control_plane.cli import _run_compose_post_deploy_update
from control_plane.dokploy import DokploySourceOfTruth, DokployTargetDefinition
from control_plane.contracts.odoo_instance_override_record import (
    OdooConfigParameterOverride,
    OdooInstanceOverrideRecord,
    OdooOverrideApplyResult,
    OdooOverrideValue,
)
from control_plane.contracts.ship_request import ShipRequest
from control_plane.storage.postgres import PostgresRecordStore


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
        deploy_mode="dokploy-compose-api",
    )


class OdooInstanceOverrideTests(unittest.TestCase):
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

    def test_cli_mark_apply_updates_result_metadata(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(Path(temporary_directory_name) / "launchplane.sqlite3")
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
                ],
            )

            store = PostgresRecordStore(database_url=database_url)
            stored_record = store.read_odoo_instance_override_record(context_name="opw", instance_name="prod")
            store.close()

        self.assertEqual(put_result.exit_code, 0, msg=put_result.output)
        self.assertEqual(mark_result.exit_code, 0, msg=mark_result.output)
        self.assertIn('"last_apply_status": "pass"', mark_result.output)
        self.assertEqual(stored_record.last_apply.status, "pass")
        self.assertEqual(stored_record.last_apply.applied_at, "2026-04-23T12:00:00Z")

    def test_post_deploy_update_renders_literal_odoo_overrides_and_marks_pass(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(Path(temporary_directory_name) / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            store.write_odoo_instance_override_record(
                OdooInstanceOverrideRecord(
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
            captured_workflow_environment: dict[str, str] = {}

            with patch.dict("os.environ", {"LAUNCHPLANE_DATABASE_URL": database_url}, clear=False), patch(
                "control_plane.dokploy.read_dokploy_config",
                return_value=("https://dokploy.example.com", "token-123"),
            ), patch(
                "control_plane.dokploy.read_control_plane_dokploy_source_of_truth",
                return_value=source_of_truth,
            ), patch(
                "control_plane.dokploy.run_compose_post_deploy_update",
                side_effect=lambda **kwargs: captured_workflow_environment.update(
                    kwargs["workflow_environment_overrides"]
                ),
            ):
                _run_compose_post_deploy_update(env_file=None, request=_ship_request())

            stored_record = store.read_odoo_instance_override_record(context_name="opw", instance_name="prod")
            store.close()

        self.assertEqual(
            captured_workflow_environment,
            {"ENV_OVERRIDE_CONFIG_PARAM__WEB__BASE__URL": "https://opw-prod.example.com"},
        )
        self.assertEqual(stored_record.last_apply.status, "pass")

    def test_post_deploy_update_rejects_secret_backed_odoo_overrides_and_marks_fail(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(Path(temporary_directory_name) / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            store.write_odoo_instance_override_record(
                OdooInstanceOverrideRecord(
                    context="opw",
                    instance="prod",
                    config_parameters=(
                        OdooConfigParameterOverride(
                            key="shopify.api_token",
                            value=OdooOverrideValue(
                                source="secret_binding",
                                secret_binding_id="secret-binding-shopify-token",
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
                    ),
                ),
            )

            with patch.dict("os.environ", {"LAUNCHPLANE_DATABASE_URL": database_url}, clear=False), patch(
                "control_plane.dokploy.read_dokploy_config",
                return_value=("https://dokploy.example.com", "token-123"),
            ), patch(
                "control_plane.dokploy.read_control_plane_dokploy_source_of_truth",
                return_value=source_of_truth,
            ):
                with self.assertRaisesRegex(click.ClickException, "cannot be rendered into a Dokploy schedule payload"):
                    _run_compose_post_deploy_update(env_file=None, request=_ship_request())

            stored_record = store.read_odoo_instance_override_record(context_name="opw", instance_name="prod")
            store.close()

        self.assertEqual(stored_record.last_apply.status, "fail")


if __name__ == "__main__":
    unittest.main()
