from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from control_plane.contracts.idempotency_record import build_launchplane_mutation_reservation_id
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.odoo_preview_apply_execution import run_odoo_preview_apply_operation
from control_plane.odoo_preview_apply_http import (
    ODOO_PREVIEW_APPLY_ROUTE,
    OdooPreviewApplyEnvelope,
    issue_odoo_preview_apply_plan,
    validate_odoo_preview_issued_plan,
)
from control_plane.provider_operations import DurableProviderOperationResult
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.odoo_preview_runtime import (
    OdooPreviewApplyInputsRequest,
    OdooPreviewApplyInputsResult,
)
from control_plane.contracts.odoo_preview_runtime_plan import OdooPreviewRuntimePlan
from tests.support.profiles import _odoo_preview_profile_payload
from tests.support.stores import _sqlite_database_url

_HEAD_SHA = "c" * 40
_IMAGE_DIGEST = "a" * 64
_PLAN_ID = "odoo-preview-plan-worker"
_WORKER_SCOPE = "worker|odoo-preview-apply|test"


def _refresh_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "product": "odoo-tenant-cm",
        "apply": {
            "dry_run_plan": {
                "status": "ready",
                "operation": "refresh",
                "product": "odoo-tenant-cm",
                "repository": "cbusillo/odoo-tenant-cm",
                "preview_slug": "pr-42",
                "preview_url": "https://pr-42.cm-preview.example.test",
                "domain_host": "pr-42.cm-preview.example.test",
                "compose_ref": "${created.composeId:cm-odoo-preview-pr-42}",
                "compose_name": "cm-odoo-preview-pr-42",
                "environment_id": "env-cm-preview",
                "template_compose_id": "compose-cm-testing",
                "summary": "ready isolated Odoo preview apply",
            },
            "image_reference": f"ghcr.io/cbusillo/odoo-tenant-cm@sha256:{_IMAGE_DIGEST}",
            "manifest": {
                "artifact_id": "artifact-cm-preview",
                "source_commit": _HEAD_SHA,
                "enterprise_base_digest": "sha256:enterprise",
                "image": {
                    "repository": "ghcr.io/cbusillo/odoo-tenant-cm",
                    "digest": f"sha256:{_IMAGE_DIGEST}",
                },
            },
            "wait_for_deploy": False,
            "smoke_check": False,
        },
    }


def _issued_plan(apply_request: OdooPreviewApplyEnvelope) -> OdooPreviewApplyInputsResult:
    dry_run_plan = apply_request.apply.dry_run_plan
    planned = OdooPreviewApplyInputsResult(
        status="ready",
        product=apply_request.product,
        context="cm",
        template_instance="testing",
        operation=dry_run_plan.operation,
        preview_slug=dry_run_plan.preview_slug,
        preview_url=dry_run_plan.preview_url,
        repository=dry_run_plan.repository,
        plan_request=OdooPreviewApplyInputsRequest(
            product=apply_request.product,
            operation=dry_run_plan.operation,
            pr_number=42,
            preview_slug=dry_run_plan.preview_slug,
            preview_url=dry_run_plan.preview_url,
            image_reference=apply_request.apply.image_reference,
            manifest=apply_request.apply.manifest,
            source_git_ref=_HEAD_SHA,
            source="test-issued-plan",
        ),
        runtime_plan=OdooPreviewRuntimePlan(
            status="ready",
            operation=dry_run_plan.operation,
            product=apply_request.product,
            repository=dry_run_plan.repository,
            pr_number=42,
            preview_slug=dry_run_plan.preview_slug,
            preview_url=dry_run_plan.preview_url,
            strategy="isolated_dokploy_compose",
            summary="ready test Odoo preview runtime plan",
        ),
        dry_run_plan=dry_run_plan,
        source="test-issued-plan",
    )
    return issue_odoo_preview_apply_plan(result=planned, plan_id=_PLAN_ID)


class RunOdooPreviewApplyOperationTests(unittest.TestCase):
    def test_runs_durable_preview_apply_without_http_identity_and_replays(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(root / "launchplane.sqlite3")
            )
            store.ensure_schema()
            profile = LaunchplaneProductProfileRecord.model_validate(
                _odoo_preview_profile_payload()
            )
            store.write_product_profile_record(profile)
            apply_request = OdooPreviewApplyEnvelope.model_validate(_refresh_payload())
            issued_plan = _issued_plan(apply_request)
            service_apply_request = validate_odoo_preview_issued_plan(
                plan_id=_PLAN_ID,
                issued_plan=issued_plan,
                request=apply_request,
            )
            execute_apply = Mock(
                return_value={
                    "status": "pass",
                    "operation": "refresh",
                    "product": "odoo-tenant-cm",
                    "repository": "cbusillo/odoo-tenant-cm",
                    "preview_slug": "pr-42",
                    "preview_url": "https://pr-42.cm-preview.example.test",
                    "domain_host": "pr-42.cm-preview.example.test",
                    "compose_name": "cm-odoo-preview-pr-42",
                }
            )
            observe_apply = Mock(side_effect=AssertionError("fresh apply must not observe"))

            def run(trace_id: str) -> DurableProviderOperationResult:
                return run_odoo_preview_apply_operation(
                    store=store,
                    control_plane_root=root,
                    record_store=store,
                    profile=profile,
                    apply_request=service_apply_request,
                    issued_plan=issued_plan,
                    reservation_scope=_WORKER_SCOPE,
                    idempotency_key=_PLAN_ID,
                    request_fingerprint="worker-request-fingerprint",
                    trace_id=trace_id,
                    execute_apply=execute_apply,
                    observe_apply=observe_apply,
                )

            first_result = run("trace-worker-first")
            replay_result = run("trace-worker-replay")

            provider_operation_record = store.read_idempotency_record(
                scope=_WORKER_SCOPE,
                route_path=ODOO_PREVIEW_APPLY_ROUTE,
                idempotency_key=_PLAN_ID,
            )
            previews = store.list_preview_records(
                context_name="cm",
                anchor_repo="odoo-tenant-cm",
                anchor_pr_number=42,
            )

        self.assertEqual(first_result.status, "completed")
        self.assertEqual(first_result.response_status_code, 202)
        self.assertEqual(first_result.response_payload["trace_id"], "trace-worker-first")
        first_payload_result = first_result.response_payload["result"]
        assert isinstance(first_payload_result, dict)
        self.assertEqual(first_payload_result["status"], "pass")
        first_records = first_result.response_payload["records"]
        assert isinstance(first_records, dict)
        self.assertEqual(first_records["lifecycle_evidence_status"], "applied")
        self.assertEqual(replay_result.status, "replayed")
        execute_apply.assert_called_once()
        observe_apply.assert_not_called()
        expected_deployment_record_id = build_launchplane_mutation_reservation_id(
            scope=_WORKER_SCOPE,
            route_path=ODOO_PREVIEW_APPLY_ROUTE,
            idempotency_key=_PLAN_ID,
        )
        self.assertEqual(
            execute_apply.call_args.kwargs["deployment_record_id"],
            expected_deployment_record_id,
        )
        assert provider_operation_record is not None
        self.assertEqual(provider_operation_record.record_id, expected_deployment_record_id)
        self.assertEqual(provider_operation_record.state, "completed")
        self.assertEqual(len(previews), 1)
        self.assertEqual(previews[0].state, "active")


if __name__ == "__main__":
    unittest.main()
