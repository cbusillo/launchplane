import unittest
from typing import cast
from unittest.mock import Mock, patch

import click

from control_plane.dokploy import api
from control_plane.dokploy.runtime_evidence import DokployEvidenceProviderError
from control_plane.dokploy_automation_inspect import (
    MAX_AUTOMATION_SCHEDULES,
    AUTOMATION_READ_SECONDS,
)
from control_plane.dokploy_target_inspect import (
    DokployTargetInspectRequest,
    DokployTargetInspectStore,
    inspect_dokploy_target,
)


class DokployAutomationInspectionTests(unittest.TestCase):
    @staticmethod
    def inspect(payload: api.JsonObject) -> dict[str, object]:
        with patch(
            "control_plane.dokploy_target_inspect.dokploy_api.fetch_dokploy_target_payload",
            return_value=payload,
        ):
            result = inspect_dokploy_target(
                record_store=cast(DokployTargetInspectStore, Mock()),
                host="https://provider.example.invalid",
                token="synthetic-token",
                request=DokployTargetInspectRequest(
                    target_type="compose", target_id="retained-target", include_automation=True
                ),
            )["automation"]
            assert isinstance(result, dict)
            return result

    def test_enabled_auto_deploy_does_not_skip_schedule_history_or_expose_scripts(self) -> None:
        schedule: api.JsonObject = {
            "scheduleId": "retained-job",
            "composeId": "retained-target",
            "scheduleType": "compose",
            "enabled": False,
            "command": "synthetic-secret",
            "script": "synthetic-secret",
            "cronExpression": "synthetic-secret",
        }
        history: api.JsonValue = [
            {"deploymentId": "retained-run", "status": "done", "log": "synthetic-secret"}
        ]
        with patch(
            "control_plane.dokploy.api.dokploy_request", side_effect=[[schedule], history]
        ) as read:
            result = self.inspect({"composeId": "retained-target", "autoDeploy": True})
        self.assertEqual(result["auto_deploy"], "enabled")
        self.assertTrue(result["schedule_inventory_complete"])
        self.assertEqual(
            result["schedules"],
            [
                {
                    "schedule_id": schedule["scheduleId"],
                    "enabled": "disabled",
                    "history": {
                        "state": "present",
                        "entries": [{"deployment_id": "retained-run", "status": "done"}],
                    },
                }
            ],
        )
        self.assertTrue(
            all(call.kwargs.get("method", "GET") == "GET" for call in read.call_args_list)
        )
        self.assertNotIn("synthetic-secret", str(result))

    def test_missing_flag_and_malformed_inventory_remain_unknown(self) -> None:
        inventories: tuple[api.JsonValue, ...] = (None, {}, {"data": [], "items": []}, [None])
        for schedules in inventories:
            with (
                self.subTest(schedules=schedules),
                patch("control_plane.dokploy.api.dokploy_request", return_value=schedules),
            ):
                result = self.inspect({"composeId": "retained-target"})
                self.assertEqual(result["auto_deploy"], "unknown")
                self.assertFalse(result["schedule_inventory_complete"])

    def test_explicit_empty_schedules_and_false_flag_are_observed(self) -> None:
        with patch("control_plane.dokploy.api.dokploy_request", return_value=[]):
            result = self.inspect({"composeId": "retained-target", "autoDeploy": False})
        self.assertEqual(result["auto_deploy"], "disabled")
        self.assertTrue(result["schedule_inventory_complete"])
        self.assertEqual(result["schedules"], [])

    def test_mismatched_target_refuses_before_schedule_reads(self) -> None:
        with patch("control_plane.dokploy.api.dokploy_request") as read:
            with self.assertRaises(DokployEvidenceProviderError):
                self.inspect({"composeId": "another-target", "autoDeploy": False})
        read.assert_not_called()

    def test_unreadable_history_refuses_without_raw_provider_text(self) -> None:
        with patch(
            "control_plane.dokploy.api.dokploy_request",
            side_effect=click.ClickException("synthetic-secret"),
        ):
            with self.assertRaises(DokployEvidenceProviderError) as caught:
                self.inspect({"composeId": "retained-target"})
        self.assertNotIn("synthetic-secret", str(caught.exception))

    def test_history_unknown_values_and_missing_schedule_identity_never_prove_empty(self) -> None:
        with patch("control_plane.dokploy.api.dokploy_request", return_value=[{"enabled": False}]):
            result = self.inspect({"composeId": "retained-target"})
        self.assertFalse(result["schedule_inventory_complete"])

    def test_malformed_oversized_and_foreign_schedules_are_incomplete(self) -> None:
        schedule: api.JsonObject = {
            "scheduleId": "job",
            "scheduleType": "compose",
            "composeId": "retained-target",
        }
        inventories: tuple[api.JsonValue, ...] = (
            [schedule] * (MAX_AUTOMATION_SCHEDULES + 1),
            [{**schedule, "composeId": "foreign-target"}],
            [{**schedule, "scheduleType": "server"}],
            [{**schedule, "scheduleId": {"secret": "synthetic-secret"}}],
        )
        for inventory in inventories:
            with (
                self.subTest(inventory=inventory),
                patch("control_plane.dokploy.api.dokploy_request", return_value=inventory) as read,
            ):
                result = self.inspect({"composeId": "retained-target"})
            self.assertFalse(result["schedule_inventory_complete"])
            self.assertEqual(result["schedules"], [])
            self.assertNotIn("synthetic-secret", str(result))
            read.assert_called_once()

    def test_malformed_history_and_unknown_status_stay_distinct_from_empty(self) -> None:
        schedule: api.JsonObject = {
            "scheduleId": "job",
            "scheduleType": "compose",
            "composeId": "retained-target",
        }
        histories: tuple[api.JsonValue, ...] = (
            None,
            [None],
            [{"status": "done"}],
            [{"deploymentId": "run", "status": "synthetic-secret"}],
            [{"deploymentId": "run"}, {"deploymentId": "run"}],
        )
        for history in histories:
            with (
                self.subTest(history=history),
                patch(
                    "control_plane.dokploy.api.dokploy_request", side_effect=[[schedule], history]
                ),
            ):
                result = self.inspect({"composeId": "retained-target"})
            self.assertNotIn("synthetic-secret", str(result))
            rows = result["schedules"]
            assert isinstance(rows, list)
            self.assertNotEqual(rows[0]["history"]["state"], "no_history")

    def test_expired_read_budget_prevents_provider_call(self) -> None:
        with (
            patch(
                "control_plane.dokploy_automation_inspect.monotonic",
                side_effect=[0, AUTOMATION_READ_SECONDS + 1],
            ),
            patch("control_plane.dokploy.api.dokploy_request") as read,
        ):
            with self.assertRaises(DokployEvidenceProviderError):
                self.inspect({"composeId": "retained-target"})
        read.assert_not_called()
