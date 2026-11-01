from datetime import datetime, timedelta, timezone
from pathlib import Path
from contextlib import closing
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.product_reconcile import ProductReconcileTarget
from control_plane.odoo_preview_apply_execution import run_odoo_preview_apply_operation
from control_plane.odoo_preview_apply_http import OdooPreviewApplyEnvelope
from control_plane.product_reconcile_read import product_preview_requests, preview_refresh_failure
from control_plane.product_reconcile_feedback import _preview_feedback
from control_plane.contracts.product_environment_read_model import _preview_refresh_warnings
from control_plane.preview_retirement import verified_preview_retirement_digest
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import LaunchplaneAuthzPolicy
from tests.http_app_test_support import _AsgiResponse, _asgi_request, _local_operator_bearer_config
from tests.support.auth import StubVerifier, identity
from tests.test_http_app_preview_pr_feedback_remediation import _installation_token
from control_plane.contracts.preview_pr_feedback_remediation import PreviewPrFeedbackObservation
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.profiles import _odoo_preview_profile_payload
from tests.support.stores import _sqlite_database_url
from tests.test_odoo_preview_apply_execution import _issued_plan, _refresh_payload


class PreviewRetirementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.store = self.enterContext(
            closing(PostgresRecordStore(database_url=_sqlite_database_url(self.root / "state.db")))
        )
        self.store.ensure_schema()
        self.profile = LaunchplaneProductProfileRecord.model_validate(
            _odoo_preview_profile_payload()
        )
        self.store.write_product_profile_record(self.profile)
        self.target = ProductReconcileTarget(
            product=self.profile.product, target_kind="preview", pull_request_number=42
        )
        self.store.request_product_reconcile(self.target, datetime.now(timezone.utc).isoformat())
        claimed = self.store.claim_next_product_reconcile_request("worker", 60)
        assert claimed is not None
        self.failed = self.store.complete_product_reconcile_request(
            claimed.target_key,
            "worker",
            "failed",
            {"action": "apply", "last_failed_error_summary": "Preview source history unknown"},
            "Preview source history unknown",
            now=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        )

    def retire(self) -> None:
        request = OdooPreviewApplyEnvelope.model_validate(_refresh_payload())
        request.apply.dry_run_plan.operation = "destroy"
        request.apply.dry_run_plan.compose_ref = "owned-preview"
        issued = _issued_plan(request)
        assert issued.plan_provenance is not None
        result = run_odoo_preview_apply_operation(
            store=self.store,
            control_plane_root=self.root,
            record_store=self.store,
            profile=self.profile,
            apply_request=request,
            issued_plan=issued,
            reservation_scope="local-operator:test",
            idempotency_key=issued.plan_provenance.plan_id,
            request_fingerprint="retirement",
            trace_id="retirement",
            execute_apply=Mock(
                return_value={
                    "status": "pass",
                    "operation": "destroy",
                    "product": self.profile.product,
                    "repository": self.profile.repository,
                    "preview_slug": issued.preview_slug,
                    "preview_url": issued.preview_url,
                    "domain_host": issued.dry_run_plan.domain_host,
                    "compose_name": issued.dry_run_plan.compose_name,
                }
            ),
        )
        self.assertEqual(result.status, "completed")

    def test_completed_native_retirement_supersedes_old_failure_without_rewriting_it(self) -> None:
        self.assertTrue(_preview_refresh_warnings(self.store, self.profile))
        self.retire()
        self.assertEqual(product_preview_requests(self.store, product=self.profile.product), ())
        self.assertEqual(_preview_refresh_warnings(self.store, self.profile), ())
        self.assertEqual(
            self.store.read_product_reconcile_request(self.failed.target_key), self.failed
        )

    def test_later_failure_and_active_lifecycle_remain_visible(self) -> None:
        self.retire()
        preview = self.store.list_preview_records()[0]
        for changes in (
            {"state": "active"},
            {"serving_generation_id": "still-serving"},
            {"destroy_reason": "caller-claimed-destroy"},
            {
                "destroyed_at": (
                    datetime.fromisoformat(self.failed.updated_at) - timedelta(seconds=1)
                ).isoformat()
            },
        ):
            with self.subTest(changes=changes):
                self.store.write_preview_record(preview.model_copy(update=changes))
                self.assertEqual(
                    product_preview_requests(self.store, product=self.profile.product),
                    (self.failed,),
                )

    def test_incomplete_or_conflicting_original_cannot_clear_failure(self) -> None:
        self.retire()
        original = self.store.list_odoo_preview_originals(product=self.profile.product, number=42)[
            0
        ]
        for changes in (
            {"state": "running"},
            {"state": "reconcile_required"},
            {"response_status_code": 409},
            {"response_payload": {"result": {"status": "pass", "operation": "destroy"}}},
            {"idempotency_key": "foreign-plan"},
        ):
            with (
                self.subTest(changes=changes),
                patch.object(
                    self.store,
                    "list_odoo_preview_originals",
                    return_value=(original.model_copy(update=changes),),
                ),
            ):
                self.assertEqual(
                    product_preview_requests(self.store, product=self.profile.product),
                    (self.failed,),
                )
        with patch.object(self.store, "read_original_mutation_intent", return_value={}):
            self.assertEqual(
                product_preview_requests(self.store, product=self.profile.product), (self.failed,)
            )

    def test_later_reconcile_failure_is_not_hidden(self) -> None:
        self.retire()
        self.store.request_product_reconcile(self.target, datetime.now(timezone.utc).isoformat())
        claimed = self.store.claim_next_product_reconcile_request("worker", 60)
        assert claimed is not None
        later = self.store.complete_product_reconcile_request(
            claimed.target_key,
            "worker",
            "failed",
            {"action": "apply", "last_failed_error_summary": "New artifact scan refused"},
            "New artifact scan refused",
            now=(datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(),
        )
        self.assertEqual(
            product_preview_requests(self.store, product=self.profile.product), (later,)
        )

    def test_current_scan_refusal_supersedes_retained_provenance_in_warning_and_feedback(
        self,
    ) -> None:
        self.retire()
        self.store.request_product_reconcile(self.target, datetime.now(timezone.utc).isoformat())
        claimed = self.store.claim_next_product_reconcile_request("worker", 60)
        assert claimed is not None
        current = self.store.complete_product_reconcile_request(
            claimed.target_key,
            "worker",
            "done",
            {**self.failed.last_plan, "action": "wait", "reason": "artifact_scan_refused"},
        )
        failure = preview_refresh_failure(current, store=self.store)
        self.assertIn(str(current.last_plan["reason"]), failure)
        self.assertNotIn(str(self.failed.last_plan["last_failed_error_summary"]), failure)
        self.assertTrue(_preview_refresh_warnings(self.store, self.profile))
        feedback = _preview_feedback(
            request=current,
            profile=self.profile,
            plan=dict(current.last_plan),
            error="",
            public_origin=lambda: "",
        )
        assert feedback is not None
        self.assertEqual(feedback.failure_summary, failure)
        self.assertEqual(feedback.status, "failed")

    async def test_director_reconciles_verified_terminal_feedback_without_grant_and_replays(
        self,
    ) -> None:
        app = create_launchplane_fastapi_app(
            verifier=StubVerifier(identity()),
            authz_policy=LaunchplaneAuthzPolicy(schema_version=3),
            bearer_identity_config=_local_operator_bearer_config(),
            control_plane_root_path=self.root,
            record_store_factory=lambda: self.store,
        )
        pr_url = f"https://github.com/{self.profile.repository}/pull/42"

        async def remediate(
            mode: str, status: str = "destroyed", key: str = "retirement-feedback"
        ) -> _AsgiResponse:
            return await _asgi_request(
                app,
                "POST",
                "/v1/previews/pr-feedback/remediation",
                headers={"Authorization": "Bearer local-operator-token", "Idempotency-Key": key},
                payload={
                    "product": self.profile.product,
                    "context": self.profile.preview.context,
                    "repository": self.profile.repository,
                    "pull_request_url": pr_url,
                    "terminal_status": status,
                    "reason": "Reconcile verified retirement",
                    "related_issue": "cbusillo/launchplane#5489",
                    "mode": mode,
                    "confirmation": f"remediate preview feedback {pr_url} to {status}"
                    if mode == "apply"
                    else "",
                },
            )

        observation = PreviewPrFeedbackObservation(
            state="absent",
            digest_sha256="a" * 64,
            token_actor_id=101,
            token_actor_login="example-delivery[bot]",
        )
        with (
            patch(
                "control_plane.preview_pr_feedback_remediation.resolve_delivery_github_installation_token",
                return_value=_installation_token(),
            ),
            patch(
                "control_plane.preview_pr_feedback_remediation.github_api_request",
                side_effect=lambda **kwargs: (
                    None
                    if kwargs.get("method") == "DELETE"
                    else {"id": 101, "login": "example-delivery[bot]", "type": "Bot"}
                ),
            ),
            patch(
                "control_plane.http_app.observe_managed_preview_pr_feedback",
                return_value=observation,
            ),
            patch(
                "control_plane.preview_pr_feedback_remediation.update_github_issue_comment"
            ) as update,
            patch(
                "control_plane.preview_pr_feedback_remediation.delete_github_issue_comment"
            ) as delete,
        ):
            missing = await remediate("apply")
            self.assertEqual(missing.status_code, 403, missing.text)
            self.retire()
            unmatched = await remediate("apply")
            self.assertEqual(unmatched.status_code, 409, unmatched.text)
            dry = await remediate("dry-run")
            self.assertEqual(dry.status_code, 202, dry.text)
            self.assertEqual(
                dry.json()["result"]["observation"]["retirement_sha256"],
                verified_preview_retirement_digest(self.store, profile=self.profile, number=42),
            )
            preview = self.store.list_preview_records()[0]
            self.store.write_preview_record(
                preview.model_copy(update={"updated_at": self.failed.requested_at})
            )
            drifted = await remediate("apply")
            self.assertEqual(drifted.status_code, 409, drifted.text)
            self.assertEqual(self.store.list_preview_pr_feedback_records(), ())
            self.store.write_preview_record(preview)
            applied = await remediate("apply")
            self.assertEqual(applied.status_code, 202, applied.text)
            replay = await remediate("apply")
            self.assertTrue(replay.json()["replayed"], replay.text)
            denied = await remediate("apply", "cleared", "invented-clear")
            self.assertEqual(denied.status_code, 403, denied.text)
            update.assert_not_called()
            delete.assert_not_called()
        self.assertEqual(self.store.list_preview_pr_feedback_records()[0].status, "destroyed")


if __name__ == "__main__":
    unittest.main()
