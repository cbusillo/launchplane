import re
import unittest
from pathlib import Path

from tests.support.workflows import load_workflow


class DetachedApplicationRetirementWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = Path(".github/workflows/reusable-detached-application-retirement.yml")
        self.workflow = load_workflow(self.path)

    def test_worker_is_reusable_only_and_protected(self) -> None:
        trigger = self.workflow.data["on"]
        assert isinstance(trigger, dict)
        self.assertEqual(set(trigger), {"workflow_call"})
        self.assertNotIn("workflow_dispatch", trigger)
        self.assertEqual(
            self.workflow.permissions,
            {"contents": "read", "id-token": "write"},
        )
        self.assertEqual(
            self.workflow.data["concurrency"],
            {
                "group": (
                    "launchplane-detached-application-retirement-"
                    "${{ inputs.candidate_target_sha256 }}"
                ),
                "cancel-in-progress": False,
            },
        )
        job = self.workflow.job("retire")
        self.assertEqual(job["runs-on"], "ubuntu-latest")
        self.assertEqual(job["environment"], "launchplane-authz-admin")

    def test_worker_validates_exact_digest_set_and_uses_oidc_request(self) -> None:
        validate = self.workflow.step_named("retire", "Validate exact detached application intent")
        self.assertIsNotNone(validate)
        assert validate is not None
        self.assertIn("protected targets must be sorted unique", validate.run)
        self.assertIn("candidate target cannot be protected", validate.run)
        self.assertIn("retire detached Dokploy application", validate.run)
        self.assertIn("Caller repository must be cbusillo/launchplane", validate.run)

        request = self.workflow.step_named(
            "retire", "Request audited detached application retirement"
        )
        self.assertIsNotNone(request)
        assert request is not None
        self.assertRegex(
            request.uses,
            re.compile(r"^cbusillo/launchplane/\.github/actions/launchplane-request@[0-9a-f]{40}$"),
        )
        self.assertEqual(
            request.with_values["route-path"],
            "/v1/detached-application-retirement",
        )

    def test_artifact_and_summary_are_redacted(self) -> None:
        evidence = self.workflow.step_named(
            "retire", "Verify redacted detached application evidence"
        )
        self.assertIsNotNone(evidence)
        assert evidence is not None
        for identifier in (
            "application_id",
            "project_id",
            "environment_id",
            "provider_operation_key",
        ):
            self.assertIn(identifier, evidence.run)
        self.assertIn("authority_write_count == 0", evidence.run)
        upload = self.workflow.step_named("retire", "Upload redacted detached application evidence")
        self.assertIsNotNone(upload)
        assert upload is not None
        self.assertEqual(
            upload.uses,
            "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
        )
        self.assertEqual(
            upload.with_values["path"],
            "detached-application-retirement-response.json",
        )

    def test_no_mutable_dispatch_wrapper_exists(self) -> None:
        for workflow_path in Path(".github/workflows").glob("*.yml"):
            if workflow_path == self.path:
                continue
            workflow = load_workflow(workflow_path)
            trigger = workflow.data.get("on")
            if not isinstance(trigger, dict) or "workflow_dispatch" not in trigger:
                continue
            self.assertNotIn(
                "reusable-detached-application-retirement.yml",
                workflow_path.read_text(encoding="utf-8"),
                workflow_path.as_posix(),
            )


if __name__ == "__main__":
    unittest.main()
