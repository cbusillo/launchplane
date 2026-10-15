"""Reconcile only the exact isolation operation, never a guessed latest job."""

import hashlib
import unittest
from unittest.mock import Mock, patch

from control_plane.dokploy import api, post_deploy
from control_plane.dokploy.connection_isolation import (
    ConnectionIsolationObservation,
    observe_connection_isolation_operation,
)
from control_plane.dokploy.source import DokployTargetDefinition


class ConnectionIsolationRecoveryTests(unittest.TestCase):
    fetch_dokploy_target_payload: Mock
    find_matching_dokploy_schedule: Mock
    dokploy_request: Mock

    def setUp(self) -> None:
        self.target = DokployTargetDefinition(
            context="example",
            instance="testing",
            target_type="compose",
            target_id="fixture",
        )
        self.operation = "stable-fixture-operation"
        self.markers = {
            "connection_isolation_operation_sha256": hashlib.sha256(
                self.operation.encode()
            ).hexdigest(),
            "connection_isolation_ok": "true",
            "connection_isolation_resources_ok": "true",
            "integration_readback_ok": "true",
        }
        for name, value in (
            ("fetch_dokploy_target_payload", {}),
            ("find_matching_dokploy_schedule", {"scheduleId": "schedule"}),
            ("dokploy_request", [{"deploymentId": "original", "status": "done"}]),
        ):
            mock = self.enterContext(patch.object(api, name, return_value=value))
            setattr(self, name, mock)
        self.enterContext(
            patch.object(
                post_deploy,
                "_resolve_dokploy_schedule_runtime",
                return_value=("server", "server", "app", "server"),
            )
        )
        self.logs = self.enterContext(
            patch.object(
                post_deploy, "_read_odoo_post_deploy_log_markers", return_value=self.markers
            )
        )

    def observe(
        self, phase: str = post_deploy.ODOO_POST_DEPLOY_SCHEDULE_TRIGGER
    ) -> ConnectionIsolationObservation:
        return observe_connection_isolation_operation(
            host="fixture",
            token="fixture",
            target_definition=self.target,
            provider_operation_key=self.operation,
            provider_effect_phase=phase,
        )

    def test_exact_terminal_success_and_failure_can_be_adopted(self) -> None:
        observed = self.observe()
        self.assertEqual(observed.outcome, "present")
        self.assertTrue(observed.passed)
        self.dokploy_request.return_value = [{"deploymentId": "original", "status": "error"}]
        observed = self.observe()
        self.assertEqual(observed.outcome, "present")
        self.assertFalse(observed.passed)
        self.dokploy_request.return_value = [{"deploymentId": "original", "status": "done"}]
        self.markers["integration_readback_ok"] = "false"
        self.assertFalse(self.observe().passed)

    def test_other_latest_job_never_substitutes_for_original(self) -> None:
        self.dokploy_request.return_value = [
            {"deploymentId": "newer", "status": "done"},
            {"deploymentId": "original", "status": "done"},
        ]
        self.logs.side_effect = [
            {"connection_isolation_operation_sha256": "different"},
            self.markers,
        ]
        self.assertTrue(self.observe().passed)
        self.logs.side_effect = None
        self.logs.return_value = {"integration_readback_ok": "true"}
        self.assertEqual(self.observe().outcome, "unknown")

    def test_queued_active_missing_and_conflicting_evidence_stays_fenced(self) -> None:
        for deployments in (
            [],
            [{"deploymentId": "original", "status": "running"}],
            [{"deploymentId": "original", "status": "queued"}],
            [
                {"deploymentId": "first", "status": "done"},
                {"deploymentId": "duplicate", "status": "done"},
            ],
        ):
            with self.subTest(deployments=deployments):
                self.dokploy_request.return_value = deployments
                self.assertEqual(self.observe().outcome, "unknown")
        self.find_matching_dokploy_schedule.return_value = None
        self.assertEqual(self.observe().outcome, "unknown")

    def test_upsert_only_checkpoint_is_a_known_non_triggered_failure(self) -> None:
        self.dokploy_request.return_value = []
        observed = self.observe(post_deploy.ODOO_POST_DEPLOY_SCHEDULE_UPSERT)
        self.assertEqual(observed.outcome, "present")
        self.assertFalse(observed.passed)
        self.assertEqual(observed.evidence["connection_isolation_triggered"], "false")
        self.find_matching_dokploy_schedule.return_value = None
        self.assertEqual(
            self.observe(post_deploy.ODOO_POST_DEPLOY_SCHEDULE_UPSERT).outcome, "present"
        )

    def test_production_cannot_be_observed_by_this_adapter(self) -> None:
        self.target = DokployTargetDefinition(
            context="example",
            instance="prod",
            target_type="compose",
            target_id="production",
        )
        self.assertEqual(self.observe().outcome, "unknown")
        self.fetch_dokploy_target_payload.assert_not_called()
