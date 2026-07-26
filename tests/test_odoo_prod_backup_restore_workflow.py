import re
import unittest
from pathlib import Path
from typing import cast

from tests.support.workflows import (
    YamlMapping,
    launchplane_request_steps,
    load_workflow,
)


PLAN_WRAPPER_PATH = Path(".github/workflows/odoo-prod-backup-restore-plan.yml")
PLAN_WORKER_PATH = Path(".github/workflows/reusable-odoo-prod-backup-restore-plan.yml")
APPLY_WRAPPER_PATH = Path(".github/workflows/odoo-prod-backup-restore-apply.yml")
APPLY_WORKER_PATH = Path(".github/workflows/reusable-odoo-prod-backup-restore-apply.yml")
PINNED_WORKFLOW_SHA = "75c632d645f29775c5f2b1ab91f5318e1468000b"

RESTORE_IDENTITY_INPUTS = {
    "product",
    "context",
    "backup_record_id",
    "verification_record_id",
    "expected_current_artifact_id",
    "expected_old_db_volume",
    "expected_new_db_volume",
    "expected_data_volume",
    "expected_log_volume",
}


class OdooProdBackupRestoreWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan_wrapper = load_workflow(PLAN_WRAPPER_PATH)
        self.plan_worker = load_workflow(PLAN_WORKER_PATH)
        self.apply_wrapper = load_workflow(APPLY_WRAPPER_PATH)
        self.apply_worker = load_workflow(APPLY_WORKER_PATH)

    def test_plan_dispatch_requires_exact_restore_identity(self) -> None:
        workflow_on = cast(YamlMapping, self.plan_wrapper.data["on"])
        workflow_dispatch = cast(YamlMapping, workflow_on["workflow_dispatch"])
        inputs = cast(YamlMapping, workflow_dispatch["inputs"])

        self.assertEqual(set(inputs), RESTORE_IDENTITY_INPUTS)
        self.assertEqual(
            self.plan_wrapper.permissions,
            {"contents": "read"},
        )
        self.assertEqual(
            self.plan_wrapper.job_permissions("plan"),
            {"contents": "read", "id-token": "write"},
        )
        self.assertEqual(
            self.plan_wrapper.job_uses("plan"),
            "cbusillo/launchplane/.github/workflows/"
            f"reusable-odoo-prod-backup-restore-plan.yml@{PINNED_WORKFLOW_SHA}",
        )
        self.assertEqual(self.plan_wrapper.steps("plan"), ())
        concurrency = cast(YamlMapping, self.plan_wrapper.data["concurrency"])
        self.assertFalse(concurrency["cancel-in-progress"])
        self.assertIn("inputs.product", str(concurrency["group"]))
        self.assertIn("inputs.context", str(concurrency["group"]))

    def test_apply_dispatch_requires_reviewed_destructive_authority(self) -> None:
        workflow_on = cast(YamlMapping, self.apply_wrapper.data["on"])
        workflow_dispatch = cast(YamlMapping, workflow_on["workflow_dispatch"])
        inputs = cast(YamlMapping, workflow_dispatch["inputs"])

        self.assertEqual(
            set(inputs),
            RESTORE_IDENTITY_INPUTS
            | {
                "plan_fingerprint",
                "confirmation",
                "idempotency_key",
                "no_cache",
                "timeout_seconds",
                "health_timeout_seconds",
            },
        )
        self.assertEqual(
            self.apply_wrapper.job_permissions("apply"),
            {"contents": "read", "id-token": "write"},
        )
        self.assertEqual(
            self.apply_wrapper.job_uses("apply"),
            "cbusillo/launchplane/.github/workflows/"
            f"reusable-odoo-prod-backup-restore-apply.yml@{PINNED_WORKFLOW_SHA}",
        )
        self.assertEqual(self.apply_wrapper.steps("apply"), ())
        concurrency = cast(YamlMapping, self.apply_wrapper.data["concurrency"])
        self.assertFalse(concurrency["cancel-in-progress"])

    def test_plan_worker_calls_only_the_restore_plan_route(self) -> None:
        request_steps = launchplane_request_steps(self.plan_worker)

        self.assertEqual(len(request_steps), 1)
        request_step = request_steps[0]
        self.assertRegex(request_step.uses, r"@[0-9a-f]{40}$")
        self.assertEqual(
            request_step.with_values["route-path"],
            "/v1/drivers/odoo/prod-backup-restore-plan",
        )
        self.assertEqual(
            request_step.with_values["payload-file"],
            ".launchplane/odoo-prod-backup-restore-plan-payload.json",
        )
        self.assertEqual(request_step.with_values["log-response-body"], "false")

        build_step = self.plan_worker.step_named("plan", "Build exact restore plan request")
        assert build_step is not None
        build_script = build_step.run
        self.assertIn('instance: "prod"', build_script)
        self.assertIn("Old and new DB volumes must differ", build_script)
        self.assertIn("expected_current_artifact_id", build_script)

        verify_step = self.plan_worker.step_named("plan", "Verify reviewed restore plan")
        assert verify_step is not None
        verify_script = verify_step.run
        for required_field in (
            "plan_fingerprint",
            "backup_record_id",
            "verification_record_id",
            "verification_nonce",
            "verification_status",
            "pg_restore_status",
            "tar_status",
            "staging_space_status",
            "database_dump_sha256",
            "filestore_archive_sha256",
            "old_db_volume",
            "new_db_volume",
            "data_volume",
            "log_volume",
        ):
            self.assertIn(required_field, verify_script)

    def test_apply_worker_replans_then_creates_and_polls_operation(self) -> None:
        request_steps = launchplane_request_steps(self.apply_worker)

        self.assertEqual(len(request_steps), 3)
        self.assertEqual(
            [step.with_values["route-path"] for step in request_steps],
            [
                "/v1/drivers/odoo/prod-backup-restore-plan",
                "/v1/drivers/odoo/prod-backup-restore-apply",
                "${{ steps.create_restore.outputs.poll_url }}",
            ],
        )
        for request_step in request_steps:
            self.assertRegex(request_step.uses, r"@[0-9a-f]{40}$")
            self.assertEqual(
                request_step.with_values["log-response-body"],
                "false",
            )
        self.assertEqual(
            request_steps[1].with_values["idempotency-key"],
            "${{ inputs.idempotency_key }}",
        )
        self.assertEqual(request_steps[2].with_values["method"], "GET")
        self.assertEqual(
            request_steps[2].with_values["poll-result-statuses"],
            "pending,running",
        )

        validate_step = self.apply_worker.step_named("apply", "Validate destructive restore inputs")
        assert validate_step is not None
        self.assertIn(
            "restore-verified-production-backup",
            validate_step.run,
        )

        fingerprint_step = self.apply_worker.step_named(
            "apply", "Enforce reviewed plan fingerprint"
        )
        assert fingerprint_step is not None
        self.assertIn("PLAN_FINGERPRINT", fingerprint_step.run)

        result_step = self.apply_worker.step_named("apply", "Verify destructive restore result")
        assert result_step is not None
        result_script = result_step.run
        for required_status in (
            "database_restore_status",
            "filestore_stage_status",
            "web_quiesce_status",
            "filestore_activation_status",
            "deployment_status",
            "post_deploy_status",
            "health_status",
            "canonical_status",
            "logo_status",
            "runtime_identity_status",
        ):
            self.assertIn(required_status, result_script)

    def test_workflows_do_not_embed_runtime_identity_or_unsafe_recovery(self) -> None:
        combined_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (
                PLAN_WRAPPER_PATH,
                PLAN_WORKER_PATH,
                APPLY_WRAPPER_PATH,
                APPLY_WORKER_PATH,
            )
        )

        self.assertNotIn("pg_resetwal", combined_text)
        self.assertNotRegex(combined_text, re.compile(r"https?://[^${]"))
        self.assertNotIn("ODOO_DB_VOLUME=", combined_text)


if __name__ == "__main__":
    unittest.main()
