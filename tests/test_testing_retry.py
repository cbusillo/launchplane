"""Exact-artifact recovery through the real HTTP, queue and operation stores."""

import asyncio
from dataclasses import replace
from typing import Any, cast
from unittest.mock import patch

from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from control_plane.contracts.promotion_record import DeploymentEvidence
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.product_reconcile import TESTING_DEPLOY_MAX_FAILED_ATTEMPTS
from control_plane.service_human_auth import HumanSessionManager, InMemoryHumanSessionStore
from control_plane.storage.postgres import LaunchplaneReleaseTupleRow
from tests import test_product_reconcile as reconcile_fixtures
from tests.http_app_test_support import (
    _AsgiResponse,
    _RejectingVerifier,
    _asgi_request,
    _browser_mutation_headers,
    _github_human_identity,
    _github_human_product_config_policy,
    _github_oauth_config,
)

ROUTE = "/v1/products/site/testing-retry"


class TestingRetryTests(reconcile_fixtures.ProductReconcileTestCase):
    running_release = reconcile_fixtures.ProductReconcileTestCase._running_release

    def setUp(self) -> None:
        super().setUp()
        self.sessions = HumanSessionManager(
            config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
        )
        self.session = self.sessions.issue(_github_human_identity())
        policy = _github_human_product_config_policy(
            actions=("odoo_target_replacement_apply.execute",), product="site", context="cm"
        )
        self.store.seed_authz_policy_if_absent(
            LaunchplaneAuthzPolicyRecord(
                record_id=build_authz_policy_record_id(
                    revision=1, policy_sha256=authz_policy_sha256(policy)
                ),
                revision=1,
                status="active",
                source="test:testing-retry",
                updated_at="2026-09-30T12:00:00Z",
                policy=policy,
            )
        )
        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=policy,
            human_session_manager=self.sessions,
            record_store_factory=lambda: self.store,
        )
        self.running_release(10, reconcile_fixtures.OLDER)
        self.store.write_environment_inventory(
            EnvironmentInventory(
                context="cm",
                instance="testing",
                source_git_ref=reconcile_fixtures.OLDER,
                deploy=DeploymentEvidence(
                    status="pass",
                    deploy_mode="recreate-in-place",
                    target_name="fixture",
                    target_type="compose",
                ),
                updated_at="2026-09-30T12:00:00Z",
                deployment_record_id="retained-data-deploy",
            )
        )
        self.github.add_run(20, reconcile_fixtures.DEPLOYABLE)
        for _ in range(TESTING_DEPLOY_MAX_FAILED_ATTEMPTS):
            self.request()
            self.finish(cast(str, self.reconcile()["queued_operation_id"]), "fail")
        self.request()
        with self.assertLogs("control_plane.product_reconcile", "WARNING"):
            exhausted = self.run_once()
        self.assertEqual(exhausted.state, "failed")
        self.draft: dict[str, object] = {
            "artifact_id": exhausted.last_plan["desired_artifact_id"],
            "source_git_ref": exhausted.last_plan["desired_commit"],
            "instance": "testing",
            "reason": "Retry after a control-plane fix, keeping testing data",
        }

    def post(self, body: dict[str, object], key: str = "") -> _AsgiResponse:
        return asyncio.run(
            _asgi_request(
                self.app,
                "POST",
                ROUTE,
                headers={
                    **_browser_mutation_headers(self.sessions, self.session),
                    "Idempotency-Key": key,
                },
                payload=body,
            )
        )

    def reviewed(self) -> dict[str, object]:
        dry = self.post(self.draft)
        self.assertEqual(dry.status_code, 202, dry.text)
        return {
            **self.draft,
            "mode": "apply",
            "reviewed_plan_sha256": dry.json()["result"]["plan_sha256"],
        }

    def test_exhausted_artifact_rearms_through_reconciler_preserving_data_and_history(self) -> None:
        before = self.snapshot()
        old_operations = self.store.list_odoo_stable_target_replacement_operation_records()
        inventory = self.store.read_environment_inventory(
            context_name="cm", instance_name="testing"
        )
        release = self.store.read_release_tuple_record(context_name="cm", channel_name="testing")
        body = self.reviewed()
        self.assertEqual(self.snapshot(), before)
        applied = self.post(body, "retry-original")
        self.assertEqual(applied.status_code, 202, applied.text)
        self.assertTrue(applied.json()["result"]["reconcile_requested"])
        self.assertEqual(self.snapshot(), before)
        plan = self.reconcile()
        operation = self.store.read_odoo_stable_target_replacement_operation_record(
            cast(str, plan["queued_operation_id"])
        )
        self.assertEqual(operation.request.artifact_id, self.draft["artifact_id"])
        self.assertEqual(operation.request.data_source_mode, "existing")
        self.assertFalse(operation.request.allow_empty_data)
        assert operation.authorization is not None
        self.assertEqual(operation.authorization.caller.identity_type, "launchplane_reconcile")
        self.assertEqual(
            self.store.read_environment_inventory(context_name="cm", instance_name="testing"),
            inventory,
        )
        self.assertEqual(
            self.store.read_release_tuple_record(context_name="cm", channel_name="testing"), release
        )
        for old in old_operations:
            self.assertEqual(
                self.store.read_odoo_stable_target_replacement_operation_record(old.operation_id),
                old,
            )
        self.assertTrue(self.artifact_scanner.called)

    def test_same_key_replays_after_progress_and_desired_build_drift(self) -> None:
        body = self.reviewed()
        applied = self.post(body, "retry-original")
        self.assertEqual(applied.status_code, 202, applied.text)
        queued = cast(str, self.reconcile()["queued_operation_id"])
        self.finish(queued, "pass")
        self.running_release(20, reconcile_fixtures.DEPLOYABLE)
        inventory = self.store.read_environment_inventory(
            context_name="cm", instance_name="testing"
        )
        self.store.write_environment_inventory(
            inventory.model_copy(update={"deployment_record_id": "completed-retry"})
        )
        self.github.add_run(30, reconcile_fixtures.NEWEST)
        before = self.store.read_product_reconcile_request("site:testing")
        replay = self.post(body, "retry-original")
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(replay.json()["original_trace_id"], applied.json()["trace_id"])
        self.assertEqual(self.store.read_product_reconcile_request("site:testing"), before)
        conflict = self.post({**body, "reason": "Another request"}, "retry-original")
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertEqual(conflict.json()["error"]["code"], "idempotency_key_reused")

    def test_moved_inventory_refuses_reviewed_apply(self) -> None:
        body = self.reviewed()
        inventory = self.store.read_environment_inventory(
            context_name="cm", instance_name="testing"
        )
        self.store.write_environment_inventory(
            inventory.model_copy(update={"deployment_record_id": "moved"})
        )
        refused = self.post(body, "stale-inventory")
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(refused.json()["error"]["code"], "testing_retry_stale")

    def test_missing_tuple_with_retained_inventory_can_be_reviewed_and_rearmed(self) -> None:
        # Inventory and tuple are separate authorities. Bind absence rather than
        # pretending their artifact ids must agree before recovery is possible.
        with self.store._session_factory() as session:
            row = session.get(LaunchplaneReleaseTupleRow, ("cm", "testing"))
            assert row is not None
            session.delete(row)
            session.commit()
        body = self.reviewed()
        applied = self.post(body, "missing-tuple")
        self.assertEqual(applied.status_code, 202, applied.text)
        self.assertIn("queued_operation_id", self.reconcile())

    def test_moved_release_tuple_refuses_atomic_commit(self) -> None:
        body = self.reviewed()
        original = self.store.commit_testing_retry_rearm

        def race(**kwargs: Any) -> Any:
            release = self.store.read_release_tuple_record(
                context_name="cm", channel_name="testing"
            )
            self.store.write_release_tuple_record(release.model_copy(update={"tuple_id": "moved"}))
            return original(**kwargs)

        with patch.object(self.store, "commit_testing_retry_rearm", side_effect=race):
            refused = self.post(body, "raced-tuple")
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(refused.json()["error"]["code"], "testing_retry_stale")
        self.assertEqual(self.store.read_product_reconcile_request("site:testing").state, "failed")

    def test_inventory_move_after_apply_does_not_reset_the_worker_budget(self) -> None:
        body = self.reviewed()
        self.assertEqual(self.post(body, "retry-original").status_code, 202)
        inventory = self.store.read_environment_inventory(
            context_name="cm", instance_name="testing"
        )
        self.store.write_environment_inventory(
            inventory.model_copy(update={"deployment_record_id": "concurrent-deploy"})
        )
        with self.assertLogs("control_plane.product_reconcile", "WARNING"):
            refused = self.run_once()
        self.assertEqual(refused.state, "failed")
        self.assertNotIn("queued_operation_id", refused.last_plan)

    def test_existing_active_operation_and_unprivileged_caller_refused(self) -> None:
        body = self.reviewed()
        operation = self.store.list_odoo_stable_target_replacement_operation_records()[-1]
        self.store.write_odoo_stable_target_replacement_operation_record(
            operation.model_copy(update={"status": "reconciliation_required", "finished_at": ""})
        )
        refused = self.post(body, "active-attempt")
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(self.store.read_product_reconcile_request("site:testing").state, "failed")
        self.session = self.sessions.issue(
            replace(
                _github_human_identity(role="read_only"),
                login="unprivileged",
                github_id=456,
                organizations=frozenset(),
                teams=frozenset(),
                email="unprivileged@example.invalid",
            )
        )
        self.assertEqual(self.post(self.draft).status_code, 403)

    def test_missing_key_and_csrf_refuse_without_record_changes(self) -> None:
        body = self.reviewed()
        self.assertEqual(self.post(body).status_code, 400)
        refused = asyncio.run(
            _asgi_request(
                self.app,
                "POST",
                ROUTE,
                headers={"Cookie": self.sessions.session_cookie_header(self.session)},
                payload=body,
            )
        )
        self.assertEqual(refused.status_code, 403)
        self.assertEqual(self.store.read_product_reconcile_request("site:testing").state, "failed")

    def test_production_unrelated_artifact_and_held_lane_refused(self) -> None:
        self.assertEqual(self.post({**self.draft, "instance": "prod"}).status_code, 400)
        self.assertEqual(self.post({**self.draft, "artifact_id": "older-build"}).status_code, 409)
        self.hold_testing()
        held = self.post(self.draft)
        self.assertEqual(held.status_code, 409, held.text)
        self.assertEqual(held.json()["error"]["code"], "testing_retry_held")

    def test_rearm_does_not_bypass_worker_hold_or_replenish_budget_on_replay(self) -> None:
        body = self.reviewed()
        self.assertEqual(self.post(body, "retry-original").status_code, 202)
        self.hold_testing()
        self.assertEqual(self.reconcile()["reason"], "staff_testing")
        self.lift_testing_hold()
        for _ in range(TESTING_DEPLOY_MAX_FAILED_ATTEMPTS):
            self.request()
            self.finish(cast(str, self.reconcile()["queued_operation_id"]), "fail")
        self.assertTrue(self.post(body, "retry-original").json()["replayed"])
        self.request()
        with self.assertLogs("control_plane.product_reconcile", "WARNING"):
            exhausted = self.run_once()
        self.assertEqual(exhausted.state, "failed")
        self.assertNotIn("queued_operation_id", exhausted.last_plan)
