"""A known remote job can outlive its waiting API executor without another trigger."""

import unittest
from unittest.mock import Mock, patch

from control_plane.dokploy import api


class ScheduleCooperativeCheckpointTests(unittest.TestCase):
    def test_handoff_runs_only_after_exact_job_identification_in_both_trigger_outcomes(
        self,
    ) -> None:
        for trigger_error in (
            None,
            api.DokployRequestFailed(
                method="POST", path="/api/schedule.runManually", status_code=504, detail="timeout"
            ),
        ):
            with self.subTest(trigger_error=trigger_error):
                checkpoint = Mock(side_effect=InterruptedError("cooperative handoff"))
                with (
                    patch.object(api, "read_dokploy_schedule_baseline", return_value=[]),
                    patch.object(api, "dokploy_request", side_effect=trigger_error) as trigger,
                    patch.object(
                        api,
                        "_read_back_triggered_schedule_deployment",
                        return_value={"deploymentId": "original-job"},
                    ),
                    patch.object(api, "_schedule_deployment_by_key") as poll,
                    self.assertRaises(InterruptedError),
                ):
                    api.run_dokploy_schedule(
                        host="https://provider.invalid",
                        token="fixture",
                        schedule_id="original-schedule",
                        timeout_seconds=120,
                        on_observation=checkpoint,
                    )
                checkpoint.assert_called_once_with("original-schedule", "original-job")
                self.assertEqual(trigger.call_count, 1)
                poll.assert_not_called()

    def test_unknown_trigger_never_hands_off_an_unidentified_job(self) -> None:
        checkpoint = Mock()
        with (
            patch.object(api, "read_dokploy_schedule_baseline", return_value=[]),
            patch.object(api, "dokploy_request") as trigger,
            patch.object(api, "_read_back_triggered_schedule_deployment", return_value=None),
            self.assertRaises(api.DokployScheduleExecutionFailed),
        ):
            api.run_dokploy_schedule(
                host="https://provider.invalid",
                token="fixture",
                schedule_id="original-schedule",
                timeout_seconds=120,
                on_observation=checkpoint,
            )
        checkpoint.assert_not_called()
        self.assertEqual(trigger.call_count, 1)
