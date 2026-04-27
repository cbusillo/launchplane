import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from control_plane.contracts.odoo_instance_override_record import (
    OdooConfigParameterOverride,
    OdooInstanceOverrideRecord,
    OdooOverrideValue,
)
from control_plane.dokploy import DokploySourceOfTruth, DokployTargetDefinition
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.workflows.odoo_post_deploy import (
    OdooPostDeployRequest,
    execute_odoo_post_deploy,
)


class OdooPostDeployWorkflowTests(unittest.TestCase):
    def _source_of_truth(self) -> DokploySourceOfTruth:
        return DokploySourceOfTruth(
            schema_version=1,
            targets=(
                DokployTargetDefinition(
                    context="opw",
                    instance="testing",
                    target_type="compose",
                    target_id="compose-123",
                    target_name="opw-testing",
                ),
            ),
        )

    def test_execute_applies_deploy_phase_overrides_through_post_deploy_runner(self) -> None:
        captured_runs: list[dict[str, object]] = []
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_odoo_instance_override_record(
                OdooInstanceOverrideRecord(
                    context="opw",
                    instance="testing",
                    apply_on=("deploy",),
                    config_parameters=(
                        OdooConfigParameterOverride(
                            key="web.base.url",
                            value=OdooOverrideValue(
                                source="literal",
                                value="https://opw-testing.example.com",
                            ),
                        ),
                    ),
                    updated_at="2026-04-26T12:00:00Z",
                    source_label="test",
                )
            )

            with (
                patch(
                    "control_plane.workflows.odoo_post_deploy.control_plane_dokploy.read_control_plane_dokploy_source_of_truth",
                    return_value=self._source_of_truth(),
                ),
                patch(
                    "control_plane.workflows.odoo_post_deploy.control_plane_dokploy.read_dokploy_config",
                    return_value=("https://dokploy.example.com", "token-123"),
                ),
                patch(
                    "control_plane.workflows.odoo_post_deploy.control_plane_dokploy.run_compose_post_deploy_update",
                    side_effect=lambda **kwargs: captured_runs.append(kwargs),
                ),
                patch(
                    "control_plane.workflows.odoo_post_deploy.utc_now_timestamp",
                    return_value="2026-04-26T12:05:00Z",
                ),
            ):
                result = execute_odoo_post_deploy(
                    control_plane_root=root,
                    record_store=store,
                    request=OdooPostDeployRequest(context="opw", instance="testing"),
                )

            self.assertEqual(result.post_deploy_status, "pass")
            self.assertEqual(result.override_status, "pass")
            self.assertTrue(result.override_payload_rendered)
            self.assertEqual(len(captured_runs), 1)
            workflow_environment = captured_runs[0]["workflow_environment_overrides"]
            self.assertIn("ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64", workflow_environment)
            self.assertNotIn("ENV_OVERRIDE_CONFIG_PARAM__WEB__BASE__URL", workflow_environment)
            updated_record = store.read_odoo_instance_override_record(
                context_name="opw",
                instance_name="testing",
            )
            self.assertEqual(updated_record.last_apply.status, "pass")
            self.assertEqual(updated_record.last_apply.applied_at, "2026-04-26T12:05:00Z")
            self.assertEqual(updated_record.source_label, "odoo-post-deploy-driver")

    def test_execute_runs_post_deploy_without_override_record(self) -> None:
        captured_runs: list[dict[str, object]] = []
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")

            with (
                patch(
                    "control_plane.workflows.odoo_post_deploy.control_plane_dokploy.read_control_plane_dokploy_source_of_truth",
                    return_value=self._source_of_truth(),
                ),
                patch(
                    "control_plane.workflows.odoo_post_deploy.control_plane_dokploy.read_dokploy_config",
                    return_value=("https://dokploy.example.com", "token-123"),
                ),
                patch(
                    "control_plane.workflows.odoo_post_deploy.control_plane_dokploy.run_compose_post_deploy_update",
                    side_effect=lambda **kwargs: captured_runs.append(kwargs),
                ),
            ):
                result = execute_odoo_post_deploy(
                    control_plane_root=root,
                    record_store=store,
                    request=OdooPostDeployRequest(context="opw", instance="testing"),
                )

            self.assertEqual(result.post_deploy_status, "pass")
            self.assertEqual(result.override_status, "skipped")
            self.assertFalse(result.override_record_found)
            self.assertEqual(len(captured_runs), 1)
            self.assertEqual(captured_runs[0]["workflow_environment_overrides"], {})


if __name__ == "__main__":
    unittest.main()
