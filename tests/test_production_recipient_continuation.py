"""Unknown effects retain their original identity while a reviewed generation proceeds."""

from datetime import datetime, timedelta
from typing import Any
import unittest
from unittest.mock import patch

from control_plane.integration_allowances import canonical_sha256
from control_plane.production_recipient import custody, ROUTE
from control_plane.production_recipient_continuation import (
    RecipientContinuationRequest,
    continuation_plan,
    supersede_recipient,
)
from control_plane.provider_operations import run_durable_provider_operation
from tests import test_production_recipient as preparation_tests


class RecipientContinuationTests(unittest.TestCase):
    database_url = ""

    def setUp(self) -> None:
        self.fixture = preparation_tests.ProductionRecipientTests()
        self.fixture.database_url = self.database_url
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def draft(self) -> RecipientContinuationRequest:
        f = self.fixture
        record = custody(f.store, f.product, f.context)
        assert record is not None
        return RecipientContinuationRequest(
            context=f.context,
            original_key=f.adapter.original_key,
            expected_custody_sha256=canonical_sha256(record.model_dump(mode="json")),
            replacement=f.body.model_copy(update={"mode": "dry-run", "reviewed_plan_sha256": ""}),
            reason="Retain every uncertain effect and continue with a reviewed independent allocation",
        )

    def review(self, request: RecipientContinuationRequest) -> RecipientContinuationRequest:
        f = self.fixture
        result = continuation_plan(
            f.root, f.store, f.product, request, f.adapter.actor, f.adapter.scope
        )[2]
        return request.model_copy(
            update={"mode": "apply", "reviewed_plan_sha256": result["plan_sha256"]}
        )

    def apply(self, request: RecipientContinuationRequest) -> dict[str, Any]:
        f = self.fixture
        return supersede_recipient(
            f.root,
            f.store,
            f.product,
            request,
            f.adapter.actor,
            f.adapter.scope,
            lambda _phase: None,
        )

    def test_unobserved_create_keeps_custody_and_late_worker_cannot_configure_new_generation(
        self,
    ) -> None:
        f = self.fixture
        with patch(
            "control_plane.production_recipient.api.dokploy_request",
            side_effect=TimeoutError("unknown create"),
        ):
            self.assertEqual(f.run_operation().response_status_code, 409)
        previous = custody(f.store, f.product, f.context)
        assert previous is not None
        reservation = f.store.read_idempotency_record(
            scope=f.adapter.scope,
            route_path=ROUTE.format(product=f.product),
            idempotency_key=f.adapter.original_key,
        )
        assert reservation is not None
        before = self.review(self.draft())
        with self.assertRaises(ValueError):
            self.apply(before)
        expired = (
            datetime.fromisoformat(reservation.lease_expires_at.replace("Z", "+00:00"))
            + timedelta(seconds=1)
        ).isoformat()
        with patch.object(f.store, "_database_mutation_timestamp", return_value=expired):
            reviewed = self.review(self.draft())
            result = self.apply(reviewed)
            self.assertEqual(self.apply(reviewed), result)
        current = custody(f.store, f.product, f.context)
        assert current is not None
        self.assertEqual(current.original_key, previous.original_key)
        self.assertNotEqual(current.namespace, previous.namespace)
        self.assertEqual(current.prior_attempts[-1]["namespace"], previous.namespace)
        self.assertEqual(
            current.continuation["retained_reservations"][0]["record_id"], reservation.record_id
        )
        stale = reservation.model_copy(update={"state": "running"})
        self.assertEqual(
            f.store.checkpoint_mutation_provider_effect(
                reservation=stale, effect_phase="late_configure"
            ).status,
            "not_running",
        )
        f.adapter.request = current.request.model_copy(
            update={"mode": "apply", "reviewed_plan_sha256": current.plan_sha256}
        )
        completed = run_durable_provider_operation(
            store=f.store,
            scope=f.adapter.scope,
            route_path=ROUTE.format(product=f.product),
            idempotency_key=f.adapter.execution_key(),
            request_fingerprint="d" * 64,
            lease_owner="continuing-worker",
            response_trace_id="launchplane_req_continuation",
            adapter=f.adapter,
        )
        self.assertEqual(completed.response_status_code, 202)
        # The timed-out request is allowed to finish in its own retained namespace.
        f.projects.append(
            {"name": previous.namespace, "projectId": "late-original-project", "environments": []}
        )
        self.assertEqual(custody(f.store, f.product, f.context).namespace, current.namespace)  # type: ignore[union-attr]
        self.assertEqual(f.projects[-1]["name"], previous.namespace)
        f.assert_old_preserved()

    def test_actual_authority_drift_supersedes_original_key_without_discarding_resources(
        self,
    ) -> None:
        f = self.fixture
        self.assertEqual(f.run_operation().response_status_code, 202)
        previous = custody(f.store, f.product, f.context)
        assert previous is not None
        f.store.write_product_profile_record(
            f.profile.model_copy(update={"updated_at": "2026-10-16T09:00:00Z"})
        )
        with self.assertRaises(ValueError):
            f.adapter.current()
        reviewed = self.review(self.draft())
        with patch(
            "control_plane.production_recipient_continuation.run_recipient_program",
            return_value={"recipient_execution_fenced": previous.plan_sha256},
        ) as fenced:
            self.apply(reviewed)
        current = custody(f.store, f.product, f.context)
        assert current is not None
        self.assertTrue(current.continuation["host_execution_fenced"])
        self.assertEqual(current.original_key, previous.original_key)
        self.assertEqual(current.prior_attempts[-1]["target_id"], previous.target_id)
        self.assertIn(previous.target_id, f.provider)
        self.assertEqual(fenced.call_count, 1)
        self.assertEqual(f.effects.count("/api/compose.create"), 1)
        f.assert_old_preserved()

    def test_failed_commit_retains_current_custody_and_all_provider_resources(self) -> None:
        f = self.fixture
        f.run_operation()
        previous = custody(f.store, f.product, f.context)
        assert previous is not None
        reviewed = self.review(self.draft())
        with (
            patch(
                "control_plane.production_recipient_continuation.run_recipient_program",
                return_value={"recipient_execution_fenced": previous.plan_sha256},
            ),
            patch.object(
                f.store,
                "_after_product_authority_bundle_step",
                side_effect=RuntimeError("injected commit failure"),
            ),
        ):
            with self.assertRaises(RuntimeError):
                self.apply(reviewed)
        self.assertEqual(custody(f.store, f.product, f.context), previous)
        f.assert_old_preserved()
