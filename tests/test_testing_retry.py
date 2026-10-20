"""Exact-artifact recovery through the real HTTP, queue and operation stores."""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, cast
from unittest.mock import patch
from urllib.parse import urlencode
from sqlalchemy.exc import SQLAlchemyError

from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from control_plane.contracts.promotion_record import DeploymentEvidence
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.product_reconcile import (
    TESTING_DEPLOY_MAX_FAILED_ATTEMPTS,
    TESTING_DEPLOY_MAX_ATTEMPT_CHAIN,
)
from control_plane.odoo_testing_restore import FreshOdooTestingGeneration, generation_binding
from control_plane.contracts.deployment_record import DeploymentRecord, ResolvedTargetEvidence
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.promotion_record import PostDeployUpdateEvidence
from control_plane.service_human_auth import HumanSessionManager, InMemoryHumanSessionStore
from control_plane.storage.postgres import (
    LaunchplaneFreshOdooTestingGenerationRow,
    LaunchplaneReleaseTupleRow,
)
from control_plane.contracts.testing_retry import (
    TESTING_RETRY_ROUTE,
    TESTING_RETRY_REARM_SCOPE,
    TESTING_RETRY_REARM_ROUTE,
    testing_retry_history_key,
)
from control_plane.testing_lane_hold import STAFF_TESTING_HOLD_REASON
from control_plane.launchplane_reconcile_authorization import (
    build_launchplane_reconcile_authorization,
)
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

ROUTE = TESTING_RETRY_ROUTE.format(product="site")


class TestingRetryTests(reconcile_fixtures.ProductReconcileTestCase):
    running_release = reconcile_fixtures.ProductReconcileTestCase._running_release

    def setUp(self) -> None:
        super().setUp()
        self.sessions = HumanSessionManager(
            config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
        )
        self.session = self.sessions.issue(_github_human_identity())
        policy = _github_human_product_config_policy(
            actions=("odoo_target_replacement_apply.execute", "product_environment.read"),
            product="site",
            context="cm",
        )
        reader = policy.github_humans[0].model_copy(
            update={
                "logins": ("unprivileged",),
                "roles": ("read_only",),
                "actions": ("product_environment.read",),
            }
        )
        policy = policy.model_copy(update={"github_humans": (*policy.github_humans, reader)})
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
        queue_before = self.store.read_product_reconcile_request("site:testing").model_dump()
        inventory = self.store.read_environment_inventory(
            context_name="cm", instance_name="testing"
        )
        release = self.store.read_release_tuple_record(context_name="cm", channel_name="testing")
        body = self.reviewed()
        self.assertEqual(self.snapshot(), before)
        applied = self.post(body, "retry-original")
        self.assertEqual(applied.status_code, 202, applied.text)
        self.assertTrue(applied.json()["result"]["reconcile_requested"])
        queue_after = self.store.read_product_reconcile_request("site:testing").model_dump()
        self.assertEqual(queue_after.keys(), queue_before.keys())
        self.assertEqual(self.snapshot(), before)
        plan = self.reconcile()
        operation = self.store.read_odoo_stable_target_replacement_operation_record(
            cast(str, plan["queued_operation_id"])
        )
        self.assertEqual(operation.request.artifact_id, self.draft["artifact_id"])
        self.assertEqual(operation.request.data_source_mode, "existing")
        self.assertFalse(operation.request.allow_empty_data)
        assert operation.authorization is not None
        self.assertEqual(
            operation.authorization,
            build_launchplane_reconcile_authorization(
                product="site",
                context="cm",
                authorized_at=operation.created_at,
            ),
        )
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
        denied = self.post(body, "reader-apply")
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertEqual(denied.json()["error"]["code"], "authorization_denied")
        self.assertEqual(self.store.read_product_reconcile_request("site:testing").state, "failed")

    def test_read_only_review_needs_no_csrf_or_deploy_grant(self) -> None:
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
        before = self.store.read_product_reconcile_request("site:testing")
        response = asyncio.run(
            _asgi_request(
                self.app,
                "GET",
                ROUTE + "?" + urlencode({k: v for k, v in self.draft.items() if k != "instance"}),
                headers={"Cookie": self.sessions.session_cookie_header(self.session)},
            )
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["result"]["reconcile_requested"])
        self.assertEqual(self.store.read_product_reconcile_request("site:testing"), before)

    def test_apply_storage_preflight_failure_is_a_service_refusal(self) -> None:
        body = self.reviewed()
        with patch.object(
            self.store, "prepare_db_only_mutation", side_effect=SQLAlchemyError("unavailable")
        ):
            response = self.post(body, "storage-failed")
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"]["code"], "testing_retry_storage_unavailable")

    def test_repeated_rearms_do_not_exhaust_traversal_on_retained_history(self) -> None:
        self.repeated_existing_rearms()
        inventory = self.store.read_environment_inventory(
            context_name="cm", instance_name="testing"
        )
        self.store.write_environment_inventory(
            inventory.model_copy(update={"deployment_record_id": "review-new-state"})
        )
        self.assertEqual(self.post(self.reviewed(), "review-after-movement").status_code, 202)

    def repeated_existing_rearms(self) -> None:
        for cycle in range(
            TESTING_DEPLOY_MAX_ATTEMPT_CHAIN // TESTING_DEPLOY_MAX_FAILED_ATTEMPTS + 1
        ):
            body = self.reviewed()
            applied = self.post(body, f"rearm-{cycle}")
            self.assertEqual(applied.status_code, 202, applied.text)
            for _ in range(TESTING_DEPLOY_MAX_FAILED_ATTEMPTS):
                self.request()
                self.finish(cast(str, self.reconcile()["queued_operation_id"]), "fail")
        self.assertGreater(
            len(self.store.list_odoo_stable_target_replacement_operation_records()),
            TESTING_DEPLOY_MAX_ATTEMPT_CHAIN,
        )

    def test_old_writer_rearms_after_new_checkpoint_survive_roll_forward(self) -> None:
        self.assertEqual(self.post(self.reviewed(), "new-writer").status_code, 202)
        # Old APIs/workers know only the product-key record. Preserve the older
        # per-artifact checkpoint while several legacy re-arms move past it.
        with (
            patch.object(self.store, "read_testing_retry_history", return_value=None),
            patch("control_plane.storage.postgres.testing_retry_history_key", return_value="site"),
        ):
            for cycle in range(
                TESTING_DEPLOY_MAX_ATTEMPT_CHAIN // TESTING_DEPLOY_MAX_FAILED_ATTEMPTS + 1
            ):
                for _ in range(TESTING_DEPLOY_MAX_FAILED_ATTEMPTS):
                    self.request()
                    self.finish(str(self.reconcile()["queued_operation_id"]), "fail")
                self.assertEqual(self.post(self.reviewed(), f"old-writer-{cycle}").status_code, 202)
        legacy = self.store.read_idempotency_record(
            scope=TESTING_RETRY_REARM_SCOPE,
            route_path=TESTING_RETRY_REARM_ROUTE,
            idempotency_key="site",
        )
        history = self.store.read_idempotency_record(
            scope=TESTING_RETRY_REARM_SCOPE,
            route_path=TESTING_RETRY_REARM_ROUTE,
            idempotency_key=testing_retry_history_key("site", str(self.draft["artifact_id"])),
        )
        assert legacy is not None and history is not None
        # Avoid incidental same-second tie order hiding a reversed comparison.
        earlier = (datetime.fromisoformat(legacy.recorded_at) - timedelta(seconds=1)).isoformat()
        self.store.write_idempotency_record(history.model_copy(update={"recorded_at": earlier}))
        # A valid reviewed budget must take effect even with stale traversal
        # metadata from before the emergency rollback.
        self.request()
        queued = str(self.reconcile()["queued_operation_id"])
        self.finish(queued, "fail")
        inventory = self.store.read_environment_inventory(
            context_name="cm", instance_name="testing"
        )
        self.store.write_environment_inventory(
            inventory.model_copy(update={"deployment_record_id": "after-old-writer"})
        )
        # Invalidating the budget still permits review using the newer legacy
        # traversal checkpoint. No history edit is needed after roll-forward.
        reviewed = self.post(self.draft)
        self.assertEqual(reviewed.status_code, 202, reviewed.text)

    def test_fresh_generation_uses_old_traversal_without_inheriting_old_budget(self) -> None:
        self.repeated_existing_rearms()
        generation = self.fresh_generation(after_history=True)
        with (
            patch(
                "control_plane.odoo_testing_restore.read_current_testing_generation",
                return_value=generation,
            ),
            patch("control_plane.product_reconcile._utc_now", return_value=generation.prepared_at),
        ):
            self.request()
            for _ in range(TESTING_DEPLOY_MAX_FAILED_ATTEMPTS):
                queued = self.reconcile()["queued_operation_id"]
                self.finish(str(queued), "fail")
                self.request()
            with self.assertLogs("control_plane.product_reconcile", "WARNING"):
                stopped = self.run_once()
            self.assertEqual(stopped.state, "failed")
            # The older product-key boundary only skipped history. It did not
            # replenish the new generation's bounded budget.
            self.assertNotIn("queued_operation_id", stopped.last_plan)
            applied = self.post(self.reviewed(), "fresh-after-long-history")
            self.assertEqual(applied.status_code, 202, applied.text)

    def test_completed_generation_keeps_existing_data_retry_receipt(self) -> None:
        self.assertEqual(self.post(self.reviewed(), "old-existing-rearm").status_code, 202)
        generation = self.fresh_generation()
        completed = generation.model_copy(update={"completed_at": generation.prepared_at})
        with self.store._session_factory() as session:
            row = session.get(LaunchplaneFreshOdooTestingGenerationRow, ("site", "cm"))
            assert row is not None
            row.payload = completed.model_dump(mode="json")
            session.commit()
        with patch(
            "control_plane.odoo_testing_restore.read_current_testing_generation",
            return_value=completed,
        ):
            queued = self.reconcile()["queued_operation_id"]
        operation = self.store.read_odoo_stable_target_replacement_operation_record(str(queued))
        self.assertEqual(operation.request.data_source_mode, "existing")
        self.assertFalse(operation.request.allow_empty_data)

    def fresh_rearm_transition(self, transition: str, *, long_history: bool) -> None:
        generation = self.fresh_generation(after_history=True)
        with (
            patch(
                "control_plane.odoo_testing_restore.read_current_testing_generation",
                return_value=generation,
            ),
            patch("control_plane.product_reconcile._utc_now", return_value=generation.prepared_at),
        ):
            cycles = (
                TESTING_DEPLOY_MAX_ATTEMPT_CHAIN // TESTING_DEPLOY_MAX_FAILED_ATTEMPTS + 1
                if long_history
                else 0
            )
            for cycle in range(cycles + 1):
                for _ in range(TESTING_DEPLOY_MAX_FAILED_ATTEMPTS):
                    self.request()
                    queued = str(self.reconcile()["queued_operation_id"])
                    self.finish(queued, "fail")
                if cycle < cycles:
                    self.assertEqual(self.post(self.reviewed(), f"fresh-{cycle}").status_code, 202)
            if transition != "successor":
                self.assertEqual(self.post(self.reviewed(), "finish-copy").status_code, 202)
                self.request()
                queued = str(self.reconcile()["queued_operation_id"])
                # Completion is persisted before tuple publication. Cover both
                # success and failure of that final publication step.
                self.finish(queued, "pass" if transition == "completed-pass" else "fail")
            if long_history:
                # A different artifact can be desired and reviewed in between.
                # Returning to A must retain A's own traversal checkpoint.
                original_draft = self.draft
                self.github.add_run(30, reconcile_fixtures.NEWEST)
                for _ in range(TESTING_DEPLOY_MAX_FAILED_ATTEMPTS):
                    self.request()
                    self.finish(str(self.reconcile()["queued_operation_id"]), "fail")
                self.request()
                with self.assertLogs("control_plane.product_reconcile", "WARNING"):
                    exhausted = self.run_once()
                self.draft = {
                    **self.draft,
                    "artifact_id": exhausted.last_plan["desired_artifact_id"],
                    "source_git_ref": exhausted.last_plan["desired_commit"],
                }
                self.assertEqual(self.post(self.reviewed(), "artifact-b").status_code, 202)
                self.draft = original_draft
                self.github.runs.pop(30)
        operations = self.store.list_odoo_stable_target_replacement_operation_records()
        changed_at = (
            max(datetime.fromisoformat(op.created_at) for op in operations) + timedelta(seconds=1)
        ).isoformat()
        if transition == "successor":
            generation = generation.model_copy(
                update={
                    "operation_id": "successor-copy",
                    "prepared_at": changed_at,
                    "reconcile_requested_at": changed_at,
                }
            )
        else:
            generation = generation.model_copy(update={"completed_at": changed_at})
            if transition == "completed-pass":
                self.running_release(20, reconcile_fixtures.DEPLOYABLE)
                self.running_release(10, reconcile_fixtures.OLDER)
        with self.store._session_factory() as session:
            row = session.get(LaunchplaneFreshOdooTestingGenerationRow, ("site", "cm"))
            assert row is not None
            row.payload = generation.model_dump(mode="json")
            session.commit()
        with (
            patch(
                "control_plane.odoo_testing_restore.read_current_testing_generation",
                return_value=generation,
            ),
            patch("control_plane.product_reconcile._utc_now", return_value=changed_at),
        ):
            self.request()
            self.reconcile()
            review = self.post(self.draft)
            self.assertEqual(review.status_code, 409, review.text)
            self.assertEqual(review.json()["error"]["code"], "testing_retry_not_exhausted")
            # Each phase gets its own bounded attempts; a traversal checkpoint
            # must neither lose old history nor spend the new phase's budget.
            for _ in range(TESTING_DEPLOY_MAX_FAILED_ATTEMPTS):
                self.request()
                queued = str(self.reconcile()["queued_operation_id"])
                operation = self.store.read_odoo_stable_target_replacement_operation_record(queued)
                self.assertEqual(
                    operation.request.data_source_mode,
                    "upstream_restore" if transition == "successor" else "existing",
                )
                self.finish(queued, "fail")
            self.request()
            with self.assertLogs("control_plane.product_reconcile", "WARNING"):
                stopped = self.run_once()
            self.assertEqual(stopped.state, "failed")
            self.assertNotIn("queued_operation_id", stopped.last_plan)
            self.assertEqual(self.post(self.reviewed(), "new-phase-review").status_code, 202)

    def test_successor_preserves_long_fresh_history_with_independent_budget(self) -> None:
        self.fresh_rearm_transition("successor", long_history=True)

    def test_completed_copy_preserves_long_history_after_rollback(self) -> None:
        self.fresh_rearm_transition("completed-pass", long_history=True)

    def test_completed_copy_starts_existing_data_budget_after_publication_failure(self) -> None:
        self.fresh_rearm_transition("completed-fail", long_history=False)

    def test_fresh_generation_does_not_inherit_existing_data_rearm(self) -> None:
        self.assertEqual(self.post(self.reviewed(), "existing-data-rearm").status_code, 202)
        operations = self.store.list_odoo_stable_target_replacement_operation_records()
        prepared = (
            max(datetime.fromisoformat(op.created_at) for op in operations) + timedelta(seconds=1)
        ).isoformat()
        generation = FreshOdooTestingGeneration(
            product="site",
            context="cm",
            operation_id="fresh-copy",
            target_id="fresh-target",
            namespace="lp-testing-" + "a" * 32,
            domain_host="fresh.example.invalid",
            resources_binding_sha256="b" * 64,
            bootstrap_plan_sha256="c" * 64,
            request_sha256="d" * 64,
            prepared_at=prepared,
            reconcile_requested_at=prepared,
        )
        with patch(
            "control_plane.odoo_testing_restore.read_current_testing_generation",
            return_value=generation,
        ):
            plan = self.reconcile()
        operation = self.store.read_odoo_stable_target_replacement_operation_record(
            cast(str, plan["queued_operation_id"])
        )
        self.assertEqual(operation.request.data_source_mode, "upstream_restore")
        self.assertTrue(operation.request.allow_empty_data)

    def fresh_generation(self, *, after_history: bool = False) -> FreshOdooTestingGeneration:
        operations = self.store.list_odoo_stable_target_replacement_operation_records()
        timestamps = [datetime.fromisoformat(op.created_at) for op in operations]
        prepared = (
            max(timestamps) + timedelta(seconds=1)
            if after_history
            else min(timestamps) - timedelta(seconds=1)
        ).isoformat()
        generation = FreshOdooTestingGeneration(
            product="site",
            context="cm",
            operation_id="original-fresh-preparation",
            target_id="fresh-target",
            namespace="lp-testing-" + "a" * 32,
            domain_host="fresh.example.invalid",
            resources_binding_sha256="b" * 64,
            bootstrap_plan_sha256="c" * 64,
            request_sha256="d" * 64,
            prepared_at=prepared,
            reconcile_requested_at=prepared,
        )
        # The bootstrap producer owns these rows. This fixture isolates retry
        # admission from its independently tested provider qualification.
        with self.store._session_factory() as session:
            session.merge(
                LaunchplaneFreshOdooTestingGenerationRow(
                    product=generation.product,
                    context=generation.context,
                    payload=generation.model_dump(mode="json"),
                )
            )
            session.commit()
        return generation

    def test_unfinished_generation_rearms_its_restore_without_rewriting_history(self) -> None:
        generation = self.fresh_generation()
        before = self.store.list_odoo_stable_target_replacement_operation_records()
        with patch(
            "control_plane.odoo_testing_restore.read_current_testing_generation",
            return_value=generation,
        ):
            dry = self.post(self.draft)
            self.assertEqual(dry.status_code, 202, dry.text)
            self.assertEqual(dry.json()["result"]["data_source_mode"], "upstream_restore")
            body = self.reviewed()
            applied = self.post(body, "resume-original-generation")
            self.assertEqual(applied.status_code, 202, applied.text)
            queued = self.reconcile()["queued_operation_id"]
            queue = self.store.read_product_reconcile_request("site:testing")
            replay = self.post(body, "resume-original-generation")
            self.assertEqual(replay.status_code, 202, replay.text)
            self.assertTrue(replay.json()["replayed"])
            self.assertEqual(self.store.read_product_reconcile_request("site:testing"), queue)
        operation = self.store.read_odoo_stable_target_replacement_operation_record(str(queued))
        self.assertEqual(operation.request.data_source_mode, "upstream_restore")
        self.assertTrue(operation.request.allow_empty_data)
        for original in before:
            self.assertEqual(
                self.store.read_odoo_stable_target_replacement_operation_record(
                    original.operation_id
                ),
                original,
            )
        self.assertEqual(self.store.read_fresh_odoo_testing_generation("site", "cm"), generation)
        self.assertIsNone(self.store.read_testing_retry_rearm("site"))
        self.assertIsNotNone(
            self.store.read_testing_retry_rearm(
                "site", generation_binding_sha256=generation_binding(generation)
            )
        )
        self.assertIsNone(
            self.store.read_testing_retry_rearm(
                "site",
                generation_binding_sha256=generation_binding(
                    generation.model_copy(update={"operation_id": "another-preparation"})
                ),
            )
        )

    def test_generation_change_at_commit_refuses_without_rearm_or_queue_wakeup(self) -> None:
        generation = self.fresh_generation()
        original_commit = self.store.commit_testing_retry_rearm
        queue = self.store.read_product_reconcile_request("site:testing")

        def change_then_commit(**kwargs: Any) -> Any:
            with self.store._session_factory() as session:
                row = session.get(LaunchplaneFreshOdooTestingGenerationRow, ("site", "cm"))
                assert row is not None
                row.payload = generation.model_copy(
                    update={"operation_id": "concurrent-successor"}
                ).model_dump(mode="json")
                session.commit()
            return original_commit(**kwargs)

        with (
            patch(
                "control_plane.odoo_testing_restore.read_current_testing_generation",
                return_value=generation,
            ),
            patch.object(self.store, "commit_testing_retry_rearm", side_effect=change_then_commit),
        ):
            refused = self.post(self.reviewed(), "changed-at-commit")
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(refused.json()["error"]["code"], "testing_retry_stale")
        self.assertEqual(self.store.read_product_reconcile_request("site:testing"), queue)
        self.assertIsNone(
            self.store.read_testing_retry_rearm(
                "site", generation_binding_sha256=generation_binding(generation)
            )
        )

    def test_fresh_generation_change_refuses_reviewed_rearm(self) -> None:
        generation = self.fresh_generation()
        with patch(
            "control_plane.odoo_testing_restore.read_current_testing_generation",
            return_value=generation,
        ):
            body = self.reviewed()
        successor = generation.model_copy(update={"operation_id": "successor-preparation"})
        with self.store._session_factory() as session:
            row = session.get(LaunchplaneFreshOdooTestingGenerationRow, ("site", "cm"))
            assert row is not None
            row.payload = successor.model_dump(mode="json")
            session.commit()
        with patch(
            "control_plane.odoo_testing_restore.read_current_testing_generation",
            return_value=successor,
        ):
            refused = self.post(body, "stale-generation")
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(refused.json()["error"]["code"], "testing_retry_stale")

    def test_unfinished_restore_refuses_rearm_before_apply(self) -> None:
        self.store.write_dokploy_target_id_record(
            DokployTargetIdRecord(
                context="cm",
                instance="testing",
                target_id="retained-target",
                updated_at="2026-09-30T12:00:00Z",
            )
        )
        self.store.write_deployment_record(
            DeploymentRecord(
                record_id="failed-copy",
                context="cm",
                instance="testing",
                source_git_ref=reconcile_fixtures.OLDER,
                resolved_target=ResolvedTargetEvidence(
                    target_type="compose", target_id="retained-target", target_name="fixture"
                ),
                deploy=DeploymentEvidence(
                    status="fail",
                    deploy_mode="recreate-in-place",
                    target_name="fixture",
                    target_type="compose",
                ),
                runtime_source={"data_source_mode": "upstream_restore"},
                post_deploy_update=PostDeployUpdateEvidence(attempted=True, status="fail"),
            )
        )
        response = self.post(self.draft)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "testing_retry_restore_incomplete")

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
        self.assertEqual(self.reconcile()["reason"], STAFF_TESTING_HOLD_REASON)
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
