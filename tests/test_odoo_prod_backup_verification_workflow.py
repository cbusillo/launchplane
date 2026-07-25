import re
import unittest
from pathlib import Path
from typing import cast

from tests.support.workflows import (
    YamlMapping,
    launchplane_request_steps,
    load_workflow,
)


WORKFLOW_PATH = Path(".github/workflows/odoo-prod-backup-verification.yml")


class OdooProdBackupVerificationWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = load_workflow(WORKFLOW_PATH)

    def test_workflow_dispatch_requires_exact_backup_selector(self) -> None:
        workflow_on = cast(YamlMapping, self.workflow.data["on"])
        workflow_dispatch = cast(YamlMapping, workflow_on["workflow_dispatch"])
        inputs = cast(YamlMapping, workflow_dispatch["inputs"])

        self.assertEqual(
            set(inputs),
            {"product", "context", "instance", "backup_record_id", "timeout_seconds"},
        )
        self.assertEqual(cast(YamlMapping, inputs["instance"])["default"], "prod")
        self.assertTrue(cast(YamlMapping, inputs["backup_record_id"])["required"])
        self.assertEqual(self.workflow.permissions, {"contents": "read", "id-token": "write"})
        self.assertEqual(self.workflow.job("verify")["runs-on"], "ubuntu-latest")

    def test_workflow_calls_dedicated_route_with_bounded_outputs(self) -> None:
        request_steps = launchplane_request_steps(self.workflow)

        self.assertEqual(len(request_steps), 1)
        request_step = request_steps[0]
        self.assertRegex(request_step.uses, r"@[0-9a-f]{40}$")
        self.assertEqual(
            request_step.with_values["route-path"],
            "/v1/drivers/odoo/prod-backup-verification",
        )
        self.assertEqual(
            request_step.with_values["payload-file"],
            ".launchplane/backup-verification-payload.json",
        )
        output_paths = str(request_step.with_values["output-paths"])
        for required_output in (
            "verification_status",
            "database_dump_sha256",
            "filestore_archive_sha256",
            "pg_restore_entry_count",
            "filestore_member_count",
            "data_volume_free_bytes",
            "staging_required_bytes",
        ):
            self.assertIn(required_output, output_paths)
        for forbidden_output in (
            "backup_root",
            "backup_dir",
            "database_dump_path",
            "filestore_archive_path",
            "manifest_path",
            "error_message",
        ):
            self.assertNotIn(forbidden_output, output_paths)

    def test_workflow_rejects_unbounded_response_fields(self) -> None:
        verify_step = self.workflow.step_named("verify", "Verify bounded backup evidence")

        self.assertIsNotNone(verify_step)
        assert verify_step is not None
        self.assertIn('has("database_dump_path")', verify_step.run)
        self.assertIn('has("manifest_path")', verify_step.run)
        self.assertIn('has("error_message")', verify_step.run)
        self.assertIn('"$VERIFICATION_STATUS" != "pass"', verify_step.run)

    def test_workflow_contains_no_runtime_path_or_identity_authority(self) -> None:
        workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertNotIn("/volumes/", workflow_text)
        self.assertNotRegex(workflow_text, re.compile(r"ODOO_(DATA|LOG|DB)_VOLUME="))


if __name__ == "__main__":
    unittest.main()
