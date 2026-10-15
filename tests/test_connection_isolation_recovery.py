"""Reconcile only the exact isolation operation, never a guessed latest job."""

import hashlib
import unittest
from unittest.mock import Mock, patch

from control_plane.dokploy import api, post_deploy
from control_plane.dokploy.connection_isolation import (
    ConnectionIsolationObservation,
    connection_isolation_plan_sha256,
    isolation_script,
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
            "log_available": "true",
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
        self,
        phase: str = post_deploy.ODOO_POST_DEPLOY_SCHEDULE_TRIGGER,
        **review: str,
    ) -> ConnectionIsolationObservation:
        return observe_connection_isolation_operation(
            host="fixture",
            token="fixture",
            target_definition=self.target,
            provider_operation_key=self.operation,
            provider_effect_phase=phase,
            **review,
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

    def test_long_unrelated_history_does_not_hide_the_original_operation(self) -> None:
        self.dokploy_request.return_value = [
            {"deploymentId": str(index), "status": "done"} for index in range(120)
        ] + [{"deploymentId": "original", "status": "done"}]
        self.logs.side_effect = lambda **kwargs: (
            self.markers if kwargs["deployment_id"] == "original" else {}
        )
        self.assertTrue(self.observe().passed)
        original_name = self.find_matching_dokploy_schedule.call_args.kwargs["app_name"]
        self.operation = "different-operation"
        self.observe()
        self.assertNotEqual(
            self.find_matching_dokploy_schedule.call_args.kwargs["app_name"], original_name
        )

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

    def test_interrupted_jobs_and_sql_incomplete_errors_remain_fenced(self) -> None:
        for status in ("canceled", "cancelled", "killed", "timeout", "unhealthy"):
            with self.subTest(status=status):
                self.dokploy_request.return_value = [{"deploymentId": "original", "status": status}]
                self.assertEqual(self.observe().outcome, "unknown")
        self.dokploy_request.return_value = [{"deploymentId": "original", "status": "error"}]
        self.markers.pop("integration_readback_ok")
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

    def test_upsert_with_unreadable_deployment_is_not_assumed_never_triggered(self) -> None:
        self.logs.return_value = {}
        self.assertEqual(
            self.observe(post_deploy.ODOO_POST_DEPLOY_SCHEDULE_UPSERT).outcome, "unknown"
        )

    def test_exact_terminal_script_can_settle_failure_when_logs_are_missing(self) -> None:
        from tests.test_connection_isolation_qualification import resource_fixture
        from control_plane.integration_readback import integration_readback_policy

        _, identity = resource_fixture()
        environment = {
            "ODOO_DB_NAME": "example_testing",
            "LAUNCHPLANE_RUNTIME_IDENTITY_JSON": identity,
        }
        payload: api.JsonObject = {
            "env": "\n".join(f"{key}={value}" for key, value in environment.items()),
            "appName": "app",
        }
        self.fetch_dokploy_target_payload.return_value = payload
        policy = integration_readback_policy(
            instance_name="testing", policies=self.target.policies, workflow_mode="maintenance"
        )
        review = {
            "reviewed_plan_sha256": connection_isolation_plan_sha256(
                self.target, payload, environment, policy
            ),
            "reviewed_resource_sha256": "b" * 64,
        }
        script = isolation_script(
            compose_app_name="app",
            database_name="example_testing",
            instance="testing",
            mode="apply",
            readback_spec=policy.encoded_spec(),
            context="example",
            runtime_identity=identity,
            reviewed_resource_sha256=review["reviewed_resource_sha256"],
            operation_digest=self.markers["connection_isolation_operation_sha256"],
        )
        self.find_matching_dokploy_schedule.return_value = {
            "scheduleId": "schedule",
            "script": script,
            "enabled": False,
        }
        self.logs.return_value = {"log_available": "true"}
        observed = self.observe(**review)
        self.assertEqual(observed.outcome, "present")
        self.assertFalse(observed.passed)
        self.logs.return_value = {"log_available": "false"}
        self.assertEqual(self.observe(**review).outcome, "unknown")
        self.logs.return_value = {"log_available": "true"}
        # The dedicated artifact retains the reviewed inputs after a redeploy.
        self.fetch_dokploy_target_payload.return_value = {
            "env": "ODOO_DB_NAME=redeployed-copy",
            "appName": "app",
        }
        recovered_after_redeploy = self.observe(**review)
        self.assertEqual(recovered_after_redeploy.outcome, "present")
        self.assertFalse(recovered_after_redeploy.passed)
        for change in ("script", "plan", "resource", "enabled"):
            with self.subTest(change=change):
                self.find_matching_dokploy_schedule.return_value = {
                    "scheduleId": "schedule",
                    "script": script + "\nexit 0" if change == "script" else script,
                    "enabled": change == "enabled",
                }
                changed_review = {**review}
                if change in {"plan", "resource"}:
                    changed_review[
                        f"reviewed_{'plan' if change == 'plan' else 'resource'}_sha256"
                    ] = "c" * 64
                self.assertEqual(self.observe(**changed_review).outcome, "unknown")

    def test_production_cannot_be_observed_by_this_adapter(self) -> None:
        self.target = DokployTargetDefinition(
            context="example",
            instance="prod",
            target_type="compose",
            target_id="production",
        )
        self.assertEqual(self.observe().outcome, "unknown")
        self.fetch_dokploy_target_payload.assert_not_called()
