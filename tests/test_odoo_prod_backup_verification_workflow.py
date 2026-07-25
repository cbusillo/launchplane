import re
import unittest
from pathlib import Path
from typing import cast

from tests.support.workflows import (
    YamlMapping,
    launchplane_request_steps,
    load_workflow,
)


WRAPPER_PATH = Path(".github/workflows/odoo-prod-backup-verification.yml")
WORKER_PATH = Path(".github/workflows/reusable-odoo-prod-backup-verification.yml")
WORKER_SHA = "b0f389326cf372cd433b96a0c98365c4cae6c5f1"


class OdooProdBackupVerificationWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.wrapper = load_workflow(WRAPPER_PATH)
        self.worker = load_workflow(WORKER_PATH)

    def test_dispatch_wrapper_pins_reviewed_reusable_worker(self) -> None:
        workflow_on = cast(YamlMapping, self.wrapper.data["on"])
        workflow_dispatch = cast(YamlMapping, workflow_on["workflow_dispatch"])
        inputs = cast(YamlMapping, workflow_dispatch["inputs"])

        self.assertEqual(
            set(inputs),
            {"product", "context", "instance", "backup_record_id", "timeout_seconds"},
        )
        self.assertEqual(cast(YamlMapping, inputs["instance"])["default"], "prod")
        self.assertTrue(cast(YamlMapping, inputs["backup_record_id"])["required"])
        self.assertEqual(
            self.wrapper.permissions,
            {"contents": "read", "id-token": "write"},
        )
        self.assertEqual(set(self.wrapper.jobs), {"verify"})
        self.assertEqual(
            self.wrapper.job_uses("verify"),
            (
                "cbusillo/launchplane/.github/workflows/"
                f"reusable-odoo-prod-backup-verification.yml@{WORKER_SHA}"
            ),
        )
        self.assertEqual(
            self.wrapper.job_permissions("verify"),
            {"contents": "read", "id-token": "write"},
        )
        self.assertEqual(self.wrapper.steps("verify"), ())
        self.assertEqual(
            self.wrapper.job("verify")["with"],
            {
                "product": "${{ inputs.product }}",
                "context": "${{ inputs.context }}",
                "instance": "${{ inputs.instance }}",
                "backup_record_id": "${{ inputs.backup_record_id }}",
                "timeout_seconds": "${{ inputs.timeout_seconds }}",
                "launchplane_url": "${{ vars.LAUNCHPLANE_PUBLIC_URL }}",
                "launchplane_audience": "${{ vars.LAUNCHPLANE_SERVICE_AUDIENCE }}",
            },
        )
        wrapper_text = WRAPPER_PATH.read_text(encoding="utf-8")
        self.assertNotIn("launchplane-request@", wrapper_text)
        self.assertNotIn("runs-on:", wrapper_text)

    def test_worker_is_reusable_oidc_job(self) -> None:
        workflow_on = cast(YamlMapping, self.worker.data["on"])
        self.assertEqual(set(workflow_on), {"workflow_call"})
        workflow_call = cast(YamlMapping, workflow_on["workflow_call"])
        inputs = cast(YamlMapping, workflow_call["inputs"])
        self.assertEqual(
            set(inputs),
            {
                "product",
                "context",
                "instance",
                "backup_record_id",
                "timeout_seconds",
                "launchplane_url",
                "launchplane_audience",
            },
        )
        self.assertEqual(self.worker.permissions, {"contents": "read"})
        self.assertEqual(self.worker.job("verify")["runs-on"], "ubuntu-latest")
        self.assertEqual(
            self.worker.job_permissions("verify"),
            {"contents": "read", "id-token": "write"},
        )

    def test_worker_calls_dedicated_route_with_bounded_outputs(self) -> None:
        request_steps = launchplane_request_steps(self.worker)

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

    def test_worker_rejects_unbounded_response_fields(self) -> None:
        verify_step = self.worker.step_named("verify", "Verify bounded backup evidence")

        self.assertIsNotNone(verify_step)
        assert verify_step is not None
        self.assertIn('has("database_dump_path")', verify_step.run)
        self.assertIn('has("manifest_path")', verify_step.run)
        self.assertIn('has("error_message")', verify_step.run)
        self.assertIn('"$VERIFICATION_STATUS" != "pass"', verify_step.run)

    def test_workflows_contain_no_runtime_path_or_identity_authority(self) -> None:
        workflow_text = "\n".join(
            path.read_text(encoding="utf-8") for path in (WRAPPER_PATH, WORKER_PATH)
        )

        self.assertNotIn("/volumes/", workflow_text)
        self.assertNotRegex(workflow_text, re.compile(r"ODOO_(DATA|LOG|DB)_VOLUME="))


if __name__ == "__main__":
    unittest.main()
