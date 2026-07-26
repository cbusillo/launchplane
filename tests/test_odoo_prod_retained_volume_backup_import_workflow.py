import re
import unittest
from pathlib import Path
from typing import cast

from tests.support.workflows import YamlMapping, launchplane_request_steps, load_workflow


PLAN_WRAPPER_PATH = Path(".github/workflows/odoo-prod-retained-volume-backup-import-plan.yml")
PLAN_WORKER_PATH = Path(
    ".github/workflows/reusable-odoo-prod-retained-volume-backup-import-plan.yml"
)
APPLY_WRAPPER_PATH = Path(".github/workflows/odoo-prod-retained-volume-backup-import-apply.yml")
APPLY_WORKER_PATH = Path(
    ".github/workflows/reusable-odoo-prod-retained-volume-backup-import-apply.yml"
)
PINNED_WORKFLOW_SHA = "75c632d645f29775c5f2b1ab91f5318e1468000b"

IMPORT_IDENTITY_INPUTS = {
    "product",
    "context",
    "backup_record_id",
    "expected_current_artifact_id",
    "expected_active_db_volume",
    "expected_active_data_volume",
    "expected_active_log_volume",
    "source_db_volume",
    "source_data_volume",
    "source_database_name",
    "expected_destination_database_name",
    "expected_database_user",
    "staging_clone_volume",
    "expected_source_compose_project",
}


class OdooProdRetainedVolumeBackupImportWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan_wrapper = load_workflow(PLAN_WRAPPER_PATH)
        self.plan_worker = load_workflow(PLAN_WORKER_PATH)
        self.apply_wrapper = load_workflow(APPLY_WRAPPER_PATH)
        self.apply_worker = load_workflow(APPLY_WORKER_PATH)

    def test_plan_dispatch_requires_exact_runtime_and_retained_volume_identity(self) -> None:
        workflow_on = cast(YamlMapping, self.plan_wrapper.data["on"])
        dispatch = cast(YamlMapping, workflow_on["workflow_dispatch"])
        inputs = cast(YamlMapping, dispatch["inputs"])

        self.assertEqual(set(inputs), IMPORT_IDENTITY_INPUTS | {"idempotency_key"})
        self.assertEqual(self.plan_wrapper.permissions, {"contents": "read"})
        self.assertEqual(
            self.plan_wrapper.job_permissions("plan"),
            {"contents": "read", "id-token": "write"},
        )
        self.assertEqual(
            self.plan_wrapper.job_uses("plan"),
            "cbusillo/launchplane/.github/workflows/"
            "reusable-odoo-prod-retained-volume-backup-import-plan.yml@"
            f"{PINNED_WORKFLOW_SHA}",
        )
        self.assertEqual(self.plan_wrapper.steps("plan"), ())
        concurrency = cast(YamlMapping, self.plan_wrapper.data["concurrency"])
        self.assertFalse(concurrency["cancel-in-progress"])

    def test_apply_dispatch_requires_exact_plan_and_confirmation(self) -> None:
        workflow_on = cast(YamlMapping, self.apply_wrapper.data["on"])
        dispatch = cast(YamlMapping, workflow_on["workflow_dispatch"])
        inputs = cast(YamlMapping, dispatch["inputs"])

        self.assertEqual(
            set(inputs),
            IMPORT_IDENTITY_INPUTS
            | {
                "plan_operation_id",
                "plan_fingerprint",
                "confirmation",
                "idempotency_key",
                "timeout_seconds",
            },
        )
        self.assertEqual(
            self.apply_wrapper.job_permissions("apply"),
            {"contents": "read", "id-token": "write"},
        )
        self.assertEqual(
            self.apply_wrapper.job_uses("apply"),
            "cbusillo/launchplane/.github/workflows/"
            "reusable-odoo-prod-retained-volume-backup-import-apply.yml@"
            f"{PINNED_WORKFLOW_SHA}",
        )
        self.assertEqual(self.apply_wrapper.steps("apply"), ())

    def test_plan_worker_creates_and_polls_durable_operation(self) -> None:
        request_steps = launchplane_request_steps(self.plan_worker)

        self.assertEqual(len(request_steps), 2)
        self.assertEqual(
            [step.with_values["route-path"] for step in request_steps],
            [
                "/v1/drivers/odoo/prod-retained-volume-backup-import-plan",
                "${{ steps.create_plan.outputs.poll_url }}",
            ],
        )
        for request_step in request_steps:
            self.assertRegex(request_step.uses, r"@[0-9a-f]{40}$")
            self.assertEqual(request_step.with_values["log-response-body"], "false")
        self.assertEqual(
            request_steps[0].with_values["idempotency-key"],
            "${{ inputs.idempotency_key }}",
        )
        self.assertEqual(request_steps[1].with_values["method"], "GET")
        self.assertEqual(
            request_steps[1].with_values["poll-result-statuses"],
            "pending,running",
        )

        verify_step = self.plan_worker.step_named("plan", "Verify reviewed retained-volume plan")
        assert verify_step is not None
        for required_field in (
            "plan_fingerprint",
            "source_pg_version",
            "source_pg_control_sha256",
            "postgres_image_id",
            "script_runner_image_id",
            "source_db_project_label",
            "source_data_project_label",
            "source_filestore_file_count",
            "source_filestore_size_bytes",
            "staging_clone_volume_absent",
            "backup_destination_absent",
            "active_data_free_bytes",
            "active_data_required_bytes",
        ):
            self.assertIn(required_field, verify_step.run)

    def test_apply_worker_rereads_plan_then_creates_and_polls_apply(self) -> None:
        request_steps = launchplane_request_steps(self.apply_worker)

        self.assertEqual(len(request_steps), 3)
        self.assertEqual(
            [step.with_values["route-path"] for step in request_steps],
            [
                (
                    "/v1/drivers/odoo/prod-retained-volume-backup-import/operations/"
                    "${{ inputs.plan_operation_id }}"
                ),
                "/v1/drivers/odoo/prod-retained-volume-backup-import-apply",
                "${{ steps.create_import.outputs.poll_url }}",
            ],
        )
        for request_step in request_steps:
            self.assertRegex(request_step.uses, r"@[0-9a-f]{40}$")
            self.assertEqual(request_step.with_values["log-response-body"], "false")
        self.assertEqual(
            request_steps[1].with_values["idempotency-key"],
            "${{ inputs.idempotency_key }}",
        )
        self.assertEqual(request_steps[2].with_values["method"], "GET")

        validate_step = self.apply_worker.step_named(
            "apply", "Validate retained-volume import inputs"
        )
        assert validate_step is not None
        self.assertIn(
            "import-retained-volumes-as-production-backup",
            validate_step.run,
        )
        result_step = self.apply_worker.step_named(
            "apply", "Verify retained-volume backup import result"
        )
        assert result_step is not None
        for required_field in (
            "import_status",
            "backup_status",
            "schedule_deployment_id",
            "database_dump_sha256",
            "filestore_archive_sha256",
        ):
            self.assertIn(required_field, result_step.run)

    def test_workflows_have_no_runtime_defaults_or_unsafe_provider_commands(self) -> None:
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
        self.assertNotIn("docker volume rm", combined_text)
        self.assertNotIn("docker compose", combined_text)
        self.assertNotRegex(combined_text, re.compile(r"https?://[^${]"))
        self.assertNotIn("ODOO_DB_VOLUME=", combined_text)


if __name__ == "__main__":
    unittest.main()
