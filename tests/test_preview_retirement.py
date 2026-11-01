from datetime import datetime, timedelta, timezone
from pathlib import Path
from contextlib import closing, nullcontext
from tempfile import TemporaryDirectory
import unittest
from typing import Literal
from fastapi import FastAPI
from unittest.mock import Mock, patch

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.product_reconcile import ProductReconcileTarget
from control_plane.odoo_preview_apply_execution import run_odoo_preview_apply_operation
from control_plane.odoo_preview_apply_http import (
    OdooPreviewApplyEnvelope,
    issue_odoo_preview_apply_plan,
)
from control_plane.product_reconcile_read import preview_refresh_failure
from control_plane.product_reconcile_feedback import _preview_feedback, post_reconcile_feedback
from control_plane.product_reconcile import ReconcileOutcome, run_product_reconcile_once
from control_plane.contracts.product_environment_read_model import _preview_refresh_warnings
from control_plane.preview_retirement import verified_preview_retirement_digest
from control_plane.preview_pr_feedback_remediation import PreviewPrFeedbackCredentials
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import (
    LaunchplaneAuthzPolicy,
    BearerIdentityConfig,
    LocalOperatorPolicyRule,
)
from tests.http_app_test_support import _AsgiResponse, _asgi_request, _local_operator_bearer_config
from tests.support.auth import StubVerifier, identity
from tests.test_http_app_preview_pr_feedback_remediation import _installation_token
from control_plane.contracts.preview_pr_feedback_remediation import PreviewPrFeedbackObservation
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.profiles import _odoo_preview_profile_payload
from tests.support.stores import _sqlite_database_url
from tests.test_odoo_preview_apply_execution import _issued_plan, _refresh_payload
from tests.test_product_reconcile import FakeGitHubComments, REPOSITORY


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
        old_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        self.failed = self.store.complete_product_reconcile_request(
            claimed.target_key,
            "worker",
            "failed",
            {
                "action": "apply",
                "last_failed_error_summary": "Preview source history unknown",
                "timing": {"planning_finished_at": old_at},
            },
            "Preview source history unknown",
            now=old_at,
        )

    def retirement_digest(self) -> str:
        return verified_preview_retirement_digest(self.store, profile=self.profile, number=42)

    async def remediate(
        self,
        app: FastAPI,
        mode: str,
        status: str = "destroyed",
        key: str = "retirement-feedback",
        token: str = "local-operator-token",
    ) -> _AsgiResponse:
        pr_url = f"https://github.com/{self.profile.repository}/pull/42"
        return await _asgi_request(
            app,
            "POST",
            "/v1/previews/pr-feedback/remediation",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key},
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

    def retire(self) -> None:
        self.apply_native(operation="destroy", plan_id="retirement-plan")

    def apply_native(
        self,
        *,
        operation: Literal["refresh", "destroy"],
        plan_id: str,
        issued_at: datetime | None = None,
    ) -> None:
        request = OdooPreviewApplyEnvelope.model_validate(_refresh_payload())
        request.apply.dry_run_plan.operation = operation
        request.apply.dry_run_plan.compose_ref = "owned-preview"
        issued = issue_odoo_preview_apply_plan(
            result=_issued_plan(request), plan_id=plan_id, issued_at=issued_at
        )
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
                    "operation": operation,
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
        self.assertFalse(preview_refresh_failure(self.failed, store=self.store))
        self.assertEqual(_preview_refresh_warnings(self.store, self.profile), ())
        self.assertEqual(
            self.store.read_product_reconcile_request(self.failed.target_key), self.failed
        )

    def test_unverified_lifecycle_does_not_clear_failure(self) -> None:
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
                self.assertTrue(preview_refresh_failure(self.failed, store=self.store))

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
                self.assertTrue(preview_refresh_failure(self.failed, store=self.store))
        with patch.object(self.store, "read_original_mutation_intent", return_value={}):
            self.assertTrue(preview_refresh_failure(self.failed, store=self.store))

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
        self.assertTrue(preview_refresh_failure(later, store=self.store))

    def test_harmless_sweep_does_not_revive_a_superseded_failure(self) -> None:
        self.retire()
        self.store.request_product_reconcile(self.target, datetime.now(timezone.utc).isoformat())
        claimed = self.store.claim_next_product_reconcile_request("worker", 60)
        assert claimed is not None
        current = self.store.complete_product_reconcile_request(
            claimed.target_key,
            "worker",
            "done",
            {
                **self.failed.last_plan,
                "action": "wait",
                "reason": "no_verified_build",
                "held": False,
            },
        )
        self.assertFalse(preview_refresh_failure(current, store=self.store))
        self.assertFalse(_preview_refresh_warnings(self.store, self.profile))

    def test_claim_preserves_the_date_of_an_inherited_failure(self) -> None:
        self.retire()
        self.store.request_product_reconcile(self.target, datetime.now(timezone.utc).isoformat())
        running = self.store.claim_next_product_reconcile_request("worker", 60)
        assert running is not None
        self.assertTrue(self.retirement_digest())
        with patch.object(
            self.store,
            "read_product_reconcile_request",
            return_value=running.model_copy(update={"last_plan": {"action": "apply"}}),
        ):
            self.assertFalse(self.retirement_digest())
        later_at = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
        self.store.complete_product_reconcile_request(
            running.target_key,
            "worker",
            "failed",
            {"action": "apply", "timing": {"planning_finished_at": later_at}},
            "Later failure",
            now=later_at,
        )
        self.store.request_product_reconcile(self.target, later_at)
        self.assertIsNotNone(self.store.claim_next_product_reconcile_request("worker", 60))
        self.assertFalse(self.retirement_digest())

    def feedback_comments(self) -> FakeGitHubComments:
        comments = FakeGitHubComments()

        def github_api(
            *,
            path: str,
            token: str,
            method: str = "GET",
            body: dict[str, object] | None = None,
            **_kwargs: object,
        ) -> object:
            return comments.request(
                path=path.replace(self.profile.repository, REPOSITORY),
                token=token,
                method=method,
                body=body,
            )

        for module in (
            "control_plane.workflows.launchplane",
            "control_plane.product_reconcile_feedback",
        ):
            self.enterContext(patch(f"{module}.github_api_request", side_effect=github_api))
        return comments

    def assert_pending_writer_survives_retirement(self, *, delivered: bool) -> None:
        comments = self.feedback_comments()
        comments.fail_writes = not delivered
        previous = post_reconcile_feedback(
            record_store=self.store,
            request=self.failed,
            plan=dict(self.failed.last_plan),
            error=self.failed.last_error,
            feedback_token=lambda *_: "feedback-token",
            public_origin=lambda: "",
            source="test",
            recorded_at=self.failed.updated_at,
        )
        assert previous is not None
        self.store.request_product_reconcile(self.target, self.failed.requested_at)
        claimed = self.store.claim_next_product_reconcile_request("worker", 60)
        assert claimed is not None
        self.store.complete_product_reconcile_request(
            claimed.target_key,
            "worker",
            "failed",
            {**self.failed.last_plan, "pr_feedback": previous},
            self.failed.last_error,
            now=self.failed.updated_at,
        )
        self.retire()
        comments.fail_writes = False
        for reason in ("no_verified_build", "client_test_notes_required"):
            self.store.request_product_reconcile(
                self.target, datetime.now(timezone.utc).isoformat()
            )
            with patch(
                "control_plane.product_reconcile.reconcile_product_request",
                return_value=ReconcileOutcome(
                    {
                        "action": "wait",
                        "held": False,
                        "reason": reason,
                        "pull_request_state": "open",
                    }
                ),
            ):
                completed = run_product_reconcile_once(
                    record_store=self.store,
                    lease_owner="worker",
                    feedback_token=lambda *_: "feedback-token",
                    public_origin=lambda: "",
                    control_plane_root=self.root,
                )
            assert completed is not None
            self.assertFalse(preview_refresh_failure(completed, store=self.store))
            self.assertFalse(_preview_refresh_warnings(self.store, self.profile))
            self.assertTrue(self.retirement_digest())
            feedback = self.store.list_preview_pr_feedback_records()[0]
            self.assertEqual(feedback.status, "pending")
            self.assertEqual(feedback.delivery_status, "delivered")

    def test_pending_writer_does_not_revive_delivered_failure(self) -> None:
        self.assert_pending_writer_survives_retirement(delivered=True)

    def test_pending_writer_does_not_redeliver_retired_failure(self) -> None:
        self.assert_pending_writer_survives_retirement(delivered=False)

    def test_later_original_blocks_a_stale_destroyed_projection(self) -> None:
        self.retire()
        destroyed = self.store.list_preview_records()[0]
        self.apply_native(
            operation="destroy",
            plan_id="later-retirement-plan",
            issued_at=datetime.fromisoformat(destroyed.destroyed_at) + timedelta(seconds=1),
        )
        originals = self.store.list_odoo_preview_originals(product=self.profile.product, number=42)
        self.assertEqual(len(originals), 2)
        self.assertTrue(all(original.state == "completed" for original in originals))
        self.assertFalse(
            self.store.list_held_provider_target_reservations(
                provider_target_key=originals[0].provider_target_key
            )
        )
        self.store.write_preview_record(destroyed)
        self.assertFalse(self.retirement_digest())
        self.assertEqual(
            self.store.list_odoo_preview_originals(product=self.profile.product, number=42),
            originals,
        )

    def test_held_target_blocks_retirement_without_changing_the_reservation(self) -> None:
        self.retire()
        original = self.store.list_odoo_preview_originals(product=self.profile.product, number=42)[
            0
        ]
        held = self.store.reserve_mutation(
            scope="other-operation",
            route_path="/test/other-operation",
            idempotency_key="held-target",
            request_fingerprint="held-target",
            lease_owner="other-worker",
            reconciliation_key=original.reconciliation_key,
            provider_target_key=original.provider_target_key,
        ).record
        assert held is not None
        self.assertFalse(self.retirement_digest())
        self.assertEqual(self.store.read_idempotency_record_by_id(held.record_id), held)

    def test_later_failed_feedback_stays_effective_after_pending_feedback(self) -> None:
        self.retire()
        self.feedback_comments()
        failed = post_reconcile_feedback(
            record_store=self.store,
            request=self.failed,
            plan={"action": "apply"},
            error="A later preview failure.",
            feedback_token=lambda *_: "feedback-token",
            public_origin=lambda: "",
            source="test",
            recorded_at=datetime.now(timezone.utc).isoformat(),
        )
        assert failed is not None
        self.assertEqual(failed["status"], "failed")
        self.store.request_product_reconcile(self.target, datetime.now(timezone.utc).isoformat())
        with patch(
            "control_plane.product_reconcile.reconcile_product_request",
            return_value=ReconcileOutcome(
                {"action": "wait", "reason": "no_verified_build", "held": False}
            ),
        ):
            self.assertIsNotNone(
                run_product_reconcile_once(
                    record_store=self.store,
                    lease_owner="worker",
                    feedback_token=lambda *_: "feedback-token",
                    public_origin=lambda: "",
                    control_plane_root=self.root,
                )
            )
        self.assertFalse(self.retirement_digest())

    def test_specific_current_error_and_closed_cleanup_keep_their_meaning(self) -> None:
        for reason in (None, "held_preview_recovery_required"):
            plan: dict[str, object] = {"action": "wait", "held": True}
            if reason is not None:
                plan["reason"] = reason
            error = "The current build or original recovery needs correction."
            current = self.failed.model_copy(update={"last_error": error, "last_plan": plan})
            self.assertEqual(preview_refresh_failure(current), error)
            feedback = _preview_feedback(
                request=current,
                profile=self.profile,
                plan=plan,
                error=error,
                public_origin=lambda: "",
            )
            assert feedback is not None
            self.assertEqual(feedback.failure_summary, error)
        closed = {
            "action": "wait",
            "held": True,
            "reason": "preview_cleanup_safety_unknown",
            "pull_request_state": "closed",
        }
        current = self.failed.model_copy(update={"last_error": "", "last_plan": closed})
        self.assertFalse(preview_refresh_failure(current))
        self.assertIsNone(
            _preview_feedback(
                request=current,
                profile=self.profile,
                plan=closed,
                error="",
                public_origin=lambda: "",
            )
        )
        cleanup = _preview_feedback(
            request=current,
            profile=self.profile,
            plan={"action": "destroy"},
            error=error,
            public_origin=lambda: "",
        )
        assert cleanup is not None
        self.assertEqual(cleanup.status, "cleanup_failed")

    def test_current_wait_supersedes_retained_failure_in_warning_and_feedback(self) -> None:
        self.retire()
        for reason in (
            "artifact_scan_refused",
            "preview_cleanup_pr_state_unknown",
            "lane_movement_refused",
        ):
            with self.subTest(reason=reason):
                self.store.request_product_reconcile(
                    self.target, datetime.now(timezone.utc).isoformat()
                )
                claimed = self.store.claim_next_product_reconcile_request("worker", 60)
                assert claimed is not None
                current = self.store.complete_product_reconcile_request(
                    claimed.target_key,
                    "worker",
                    "done",
                    {
                        **self.failed.last_plan,
                        "action": "wait",
                        "reason": reason,
                        "held": True,
                        "timing": {"planning_finished_at": datetime.now(timezone.utc).isoformat()},
                    },
                )
                failure = preview_refresh_failure(current, store=self.store)
                self.assertTrue(failure)
                self.assertNotEqual(failure, self.failed.last_error)
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

    async def test_other_machine_identities_do_not_inherit_terminal_reconciliation(self) -> None:
        self.retire()
        for transport in ("local_admin", "terminal_agent"):
            with self.subTest(transport=transport):
                app = create_launchplane_fastapi_app(
                    verifier=StubVerifier(identity()),
                    bearer_identity_config=BearerIdentityConfig.model_validate(
                        {
                            f"{transport}_token": "machine-token",
                            f"{transport}_subject": "other-machine",
                            f"{transport}_token_label": "other-machine",
                        }
                    ),
                    authz_policy=LaunchplaneAuthzPolicy(schema_version=3),
                    control_plane_root_path=self.root,
                    record_store_factory=lambda: self.store,
                )
                response = await self.remediate(app, "apply", key="foreign", token="machine-token")
                self.assertEqual(response.status_code, 403, response.text)
        self.assertFalse(self.store.list_preview_pr_feedback_records())

    async def test_policy_granted_caller_keeps_the_existing_review_contract(self) -> None:
        self.retire()
        app = create_launchplane_fastapi_app(
            verifier=StubVerifier(identity()),
            authz_policy=LaunchplaneAuthzPolicy(
                schema_version=3,
                local_operators=(
                    LocalOperatorPolicyRule(
                        subjects=("local-owner-agent",),
                        token_labels=("local-owner-read",),
                        products=(self.profile.product,),
                        contexts=(self.profile.preview.context,),
                        actions=(
                            "preview_pr_feedback_remediation.plan",
                            "preview_pr_feedback_remediation.apply",
                        ),
                    ),
                ),
            ),
            bearer_identity_config=_local_operator_bearer_config(),
            control_plane_root_path=self.root,
            record_store_factory=lambda: self.store,
        )
        observation = PreviewPrFeedbackObservation(
            state="absent",
            digest_sha256="a" * 64,
            token_actor_id=101,
            token_actor_login="delivery[bot]",
        )
        with (
            patch(
                "control_plane.http_app.resolve_remediation_token",
                return_value=nullcontext(
                    PreviewPrFeedbackCredentials(
                        token="fixture", actor_id=101, actor_login="delivery[bot]"
                    )
                ),
            ),
            patch(
                "control_plane.http_app.observe_managed_preview_pr_feedback",
                return_value=observation,
            ),
        ):
            dry = await self.remediate(app, "dry-run", key="policy-review")
            self.assertEqual(dry.status_code, 202, dry.text)
            self.assertFalse(dry.json()["result"]["observation"]["retirement_sha256"])
            applied = await self.remediate(app, "apply", key="policy-review")
            self.assertEqual(applied.status_code, 202, applied.text)

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
            missing = await self.remediate(app, "apply")
            self.assertEqual(missing.status_code, 403, missing.text)
            self.retire()
            unmatched = await self.remediate(app, "apply")
            self.assertEqual(unmatched.status_code, 409, unmatched.text)
            dry = await self.remediate(app, "dry-run")
            self.assertEqual(dry.status_code, 202, dry.text)
            self.assertEqual(
                dry.json()["result"]["observation"]["retirement_sha256"],
                self.retirement_digest(),
            )
            preview = self.store.list_preview_records()[0]
            self.store.write_preview_record(
                preview.model_copy(update={"updated_at": self.failed.requested_at})
            )
            drifted = await self.remediate(app, "apply")
            self.assertEqual(drifted.status_code, 409, drifted.text)
            self.assertEqual(self.store.list_preview_pr_feedback_records(), ())
            self.store.write_preview_record(preview)
            applied = await self.remediate(app, "apply")
            self.assertEqual(applied.status_code, 202, applied.text)
            self.store.request_product_reconcile(
                self.target, datetime.now(timezone.utc).isoformat()
            )
            claimed = self.store.claim_next_product_reconcile_request("worker", 60)
            assert claimed is not None
            self.store.complete_product_reconcile_request(
                claimed.target_key,
                "worker",
                "failed",
                {"action": "apply"},
                "Later deploy failure",
                now=(datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(),
            )
            self.assertFalse(self.retirement_digest())
            replay = await self.remediate(app, "apply")
            self.assertTrue(replay.json()["replayed"], replay.text)
            later = await self.remediate(app, "apply", key="new-after-failure")
            self.assertEqual(later.status_code, 403, later.text)
            denied = await self.remediate(app, "apply", "cleared", "invented-clear")
            self.assertEqual(denied.status_code, 403, denied.text)
            update.assert_not_called()
            delete.assert_not_called()
        self.assertEqual(self.store.list_preview_pr_feedback_records()[0].status, "destroyed")


if __name__ == "__main__":
    unittest.main()
