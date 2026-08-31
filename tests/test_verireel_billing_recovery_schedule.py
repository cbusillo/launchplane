import unittest
from datetime import datetime
from unittest.mock import patch

import click

from control_plane.dokploy import api as dokploy_api
from control_plane.workflows.verireel_billing_recovery_schedule import (
    VERIREEL_BILLING_RECOVERY_SCHEDULE_NAME,
    VeriReelRecoveryScheduleSnapshot,
    cron_matches_utc,
    delete_verireel_billing_recovery_schedule,
    finalize_verireel_billing_recovery_schedule,
    quiesce_verireel_billing_recovery_schedule,
    reconcile_verireel_billing_recovery_schedule,
    recovery_schedule_command,
    recovery_schedule_cron,
    restore_verireel_billing_recovery_schedule,
)


def _application_schedule(
    *, cron_expression: str = "2,17,32,47 * * * *", enabled: bool = True
) -> dokploy_api.JsonObject:
    return {
        "scheduleId": "schedule-123",
        "name": VERIREEL_BILLING_RECOVERY_SCHEDULE_NAME,
        "cronExpression": cron_expression,
        "scheduleType": "application",
        "shellType": "sh",
        "command": recovery_schedule_command("testing"),
        "applicationId": "application-123",
        "enabled": enabled,
        "timezone": "UTC",
    }


class DokployApplicationScheduleTests(unittest.TestCase):
    def test_upsert_creates_then_requires_exact_readback(self) -> None:
        payload = _application_schedule()
        schedule_payload: dokploy_api.JsonObject = {
            key: value for key, value in payload.items() if key != "scheduleId"
        }
        with (
            patch(
                "control_plane.dokploy.api.list_dokploy_schedules",
                side_effect=[(), (payload,)],
            ),
            patch("control_plane.dokploy.api.dokploy_request") as request,
        ):
            result = dokploy_api.upsert_dokploy_application_schedule(
                host="https://dokploy.example.com",
                token="token-123",
                application_id="application-123",
                schedule_payload=schedule_payload,
            )

        self.assertEqual(result["scheduleId"], "schedule-123")
        self.assertEqual(request.call_args.kwargs["path"], "/api/schedule.create")
        self.assertEqual(request.call_args.kwargs["payload"], schedule_payload)

    def test_upsert_repairs_stale_existing_schedule_and_reads_back_exact_state(self) -> None:
        payload = _application_schedule(enabled=False)
        schedule_payload: dokploy_api.JsonObject = {
            key: value for key, value in payload.items() if key != "scheduleId"
        }
        stale_payload = _application_schedule(cron_expression="0 * * * *")
        stale_payload["timezone"] = "Etc/UTC"
        with (
            patch(
                "control_plane.dokploy.api.list_dokploy_schedules",
                side_effect=[(stale_payload,), (payload,)],
            ),
            patch("control_plane.dokploy.api.dokploy_request") as request,
        ):
            result = dokploy_api.upsert_dokploy_application_schedule(
                host="https://dokploy.example.com",
                token="token-123",
                application_id="application-123",
                schedule_payload=schedule_payload,
            )

        self.assertEqual(result, payload)
        self.assertEqual(request.call_args.kwargs["path"], "/api/schedule.update")
        self.assertEqual(
            request.call_args.kwargs["payload"],
            {"scheduleId": "schedule-123", **schedule_payload},
        )

    def test_duplicate_managed_application_schedule_blocks_mutation(self) -> None:
        duplicate = _application_schedule()
        duplicate["scheduleId"] = "schedule-456"
        schedule_payload: dokploy_api.JsonObject = {
            key: value for key, value in _application_schedule().items() if key != "scheduleId"
        }
        with (
            patch(
                "control_plane.dokploy.api.list_dokploy_schedules",
                return_value=(_application_schedule(), duplicate),
            ),
            patch("control_plane.dokploy.api.dokploy_request") as request,
        ):
            with self.assertRaisesRegex(click.ClickException, "duplicate detected"):
                dokploy_api.upsert_dokploy_application_schedule(
                    host="https://dokploy.example.com",
                    token="token-123",
                    application_id="application-123",
                    schedule_payload=schedule_payload,
                )

        request.assert_not_called()

    def test_exact_readback_mismatch_blocks_schedule_activation(self) -> None:
        payload = _application_schedule()
        schedule_payload: dokploy_api.JsonObject = {
            key: value for key, value in payload.items() if key != "scheduleId"
        }
        mismatched = dict(payload)
        mismatched["cronExpression"] = "0 * * * *"
        with (
            patch(
                "control_plane.dokploy.api.list_dokploy_schedules",
                side_effect=[(), (mismatched,)],
            ),
            patch("control_plane.dokploy.api.dokploy_request"),
        ):
            with self.assertRaisesRegex(click.ClickException, "readback did not exactly match"):
                dokploy_api.upsert_dokploy_application_schedule(
                    host="https://dokploy.example.com",
                    token="token-123",
                    application_id="application-123",
                    schedule_payload=schedule_payload,
                )


class VeriReelBillingRecoveryScheduleTests(unittest.TestCase):
    def test_stable_and_preview_crons_have_staggered_semantics(self) -> None:
        self.assertTrue(
            cron_matches_utc(recovery_schedule_cron("testing"), datetime(2026, 1, 1, 0, 2))
        )
        self.assertTrue(
            cron_matches_utc(recovery_schedule_cron("testing"), datetime(2026, 1, 1, 4, 17))
        )
        self.assertFalse(
            cron_matches_utc(recovery_schedule_cron("testing"), datetime(2026, 1, 1, 2, 7))
        )
        self.assertTrue(
            cron_matches_utc(recovery_schedule_cron("prod"), datetime(2026, 1, 1, 4, 22))
        )
        self.assertFalse(
            cron_matches_utc(recovery_schedule_cron("prod"), datetime(2026, 1, 1, 4, 17))
        )
        self.assertTrue(
            cron_matches_utc(recovery_schedule_cron("preview"), datetime(2026, 1, 1, 12, 42))
        )
        self.assertFalse(
            cron_matches_utc(recovery_schedule_cron("preview"), datetime(2026, 1, 1, 4, 47))
        )

    def test_preview_command_is_commission_only_and_has_no_secret(self) -> None:
        command = recovery_schedule_command("preview")
        self.assertIn("invoke-billing-recovery-cron.sh", command)
        self.assertIn("/api/cron/billing-recovery", command)
        self.assertNotIn("reminder", command.lower())
        self.assertNotIn("authorize", command.lower())
        for forbidden_value in ("cron-secret", "token-123", "Bearer ", "API_KEY"):
            self.assertNotIn(forbidden_value, command)

    def test_quiesce_and_restore_leave_absent_schedule_absent(self) -> None:
        with (
            patch(
                "control_plane.workflows.verireel_billing_recovery_schedule.dokploy_api.read_dokploy_application_schedule",
                return_value=None,
            ),
            patch(
                "control_plane.workflows.verireel_billing_recovery_schedule.reconcile_verireel_billing_recovery_schedule"
            ) as reconcile,
            patch(
                "control_plane.workflows.verireel_billing_recovery_schedule.dokploy_api.upsert_dokploy_application_schedule"
            ) as upsert,
        ):
            snapshot = quiesce_verireel_billing_recovery_schedule(
                host="https://dokploy.example.com",
                token="token-123",
                application_id="application-123",
                instance="testing",
            )
            restore_verireel_billing_recovery_schedule(
                host="https://dokploy.example.com",
                token="token-123",
                application_id="application-123",
                instance="testing",
                snapshot=snapshot,
            )

        self.assertEqual(snapshot, VeriReelRecoveryScheduleSnapshot(existed=False))
        reconcile.assert_not_called()
        upsert.assert_not_called()

    def test_quiesce_and_failure_restore_preserve_prior_enabled_state(self) -> None:
        schedule = _application_schedule(enabled=True)
        with (
            patch(
                "control_plane.workflows.verireel_billing_recovery_schedule.dokploy_api.read_dokploy_application_schedule",
                return_value=schedule,
            ),
            patch(
                "control_plane.workflows.verireel_billing_recovery_schedule.dokploy_api.assert_dokploy_application_schedule_exact"
            ),
            patch(
                "control_plane.workflows.verireel_billing_recovery_schedule.dokploy_api.upsert_dokploy_application_schedule"
            ) as upsert,
            patch(
                "control_plane.workflows.verireel_billing_recovery_schedule.dokploy_api.run_dokploy_schedule"
            ) as run_schedule,
        ):
            snapshot = quiesce_verireel_billing_recovery_schedule(
                host="https://dokploy.example.com",
                token="token-123",
                application_id="application-123",
                instance="testing",
            )
            restore_verireel_billing_recovery_schedule(
                host="https://dokploy.example.com",
                token="token-123",
                application_id="application-123",
                instance="testing",
                snapshot=snapshot,
            )

        self.assertEqual(snapshot, VeriReelRecoveryScheduleSnapshot(existed=True, enabled=True))
        self.assertEqual(upsert.call_count, 2)
        self.assertFalse(upsert.call_args_list[0].kwargs["schedule_payload"]["enabled"])
        self.assertTrue(upsert.call_args_list[1].kwargs["schedule_payload"]["enabled"])
        run_schedule.assert_not_called()

    def test_quiesce_and_restore_preserve_prior_disabled_state(self) -> None:
        schedule = _application_schedule(enabled=False)
        with (
            patch(
                "control_plane.workflows.verireel_billing_recovery_schedule.dokploy_api.read_dokploy_application_schedule",
                return_value=schedule,
            ),
            patch(
                "control_plane.workflows.verireel_billing_recovery_schedule.dokploy_api.upsert_dokploy_application_schedule"
            ) as upsert,
            patch(
                "control_plane.workflows.verireel_billing_recovery_schedule.dokploy_api.run_dokploy_schedule"
            ) as run_schedule,
        ):
            snapshot = quiesce_verireel_billing_recovery_schedule(
                host="https://dokploy.example.com",
                token="token-123",
                application_id="application-123",
                instance="testing",
            )
            restore_verireel_billing_recovery_schedule(
                host="https://dokploy.example.com",
                token="token-123",
                application_id="application-123",
                instance="testing",
                snapshot=snapshot,
            )

        self.assertEqual(snapshot, VeriReelRecoveryScheduleSnapshot(existed=True, enabled=False))
        self.assertEqual(upsert.call_count, 2)
        self.assertFalse(upsert.call_args_list[0].kwargs["schedule_payload"]["enabled"])
        self.assertFalse(upsert.call_args_list[1].kwargs["schedule_payload"]["enabled"])
        run_schedule.assert_not_called()

    def test_reconcile_enables_preview_schedule_after_canary(self) -> None:
        schedule = _application_schedule(cron_expression="12,27,42,57 * * * *", enabled=False)
        schedule["command"] = recovery_schedule_command("preview")
        with (
            patch(
                "control_plane.workflows.verireel_billing_recovery_schedule.dokploy_api.read_dokploy_application_schedule",
                return_value=None,
            ),
            patch(
                "control_plane.workflows.verireel_billing_recovery_schedule.dokploy_api.upsert_dokploy_application_schedule",
                return_value=schedule,
            ) as upsert,
            patch(
                "control_plane.workflows.verireel_billing_recovery_schedule.dokploy_api.run_dokploy_schedule"
            ) as run_schedule,
        ):
            reconcile_verireel_billing_recovery_schedule(
                host="https://dokploy.example.com",
                token="token-123",
                application_id="preview-app-123",
                instance="preview",
            )

        self.assertFalse(upsert.call_args_list[0].kwargs["schedule_payload"]["enabled"])
        self.assertTrue(upsert.call_args_list[1].kwargs["schedule_payload"]["enabled"])
        self.assertEqual(
            upsert.call_args_list[1].kwargs["schedule_payload"]["cronExpression"],
            "12,27,42,57 * * * *",
        )
        self.assertEqual(run_schedule.call_args.kwargs["schedule_id"], "schedule-123")

    def test_failed_new_schedule_canary_removes_disabled_schedule(self) -> None:
        schedule = _application_schedule(enabled=False)
        with (
            patch(
                "control_plane.workflows.verireel_billing_recovery_schedule.dokploy_api.read_dokploy_application_schedule",
                return_value=None,
            ),
            patch(
                "control_plane.workflows.verireel_billing_recovery_schedule.dokploy_api.upsert_dokploy_application_schedule",
                return_value=schedule,
            ),
            patch(
                "control_plane.workflows.verireel_billing_recovery_schedule.dokploy_api.run_dokploy_schedule",
                side_effect=click.ClickException("canary failed"),
            ),
            patch(
                "control_plane.workflows.verireel_billing_recovery_schedule.dokploy_api.delete_dokploy_application_schedule"
            ) as delete_schedule,
        ):
            with self.assertRaisesRegex(click.ClickException, "canary failed"):
                reconcile_verireel_billing_recovery_schedule(
                    host="https://dokploy.example.com",
                    token="token-123",
                    application_id="application-123",
                    instance="testing",
                )

        self.assertFalse(delete_schedule.call_args.kwargs["schedule_payload"]["enabled"])

    def test_failed_existing_schedule_canary_leaves_it_disabled_without_deleting(self) -> None:
        existing_schedule = _application_schedule()
        disabled_schedule = _application_schedule(enabled=False)
        with (
            patch(
                "control_plane.workflows.verireel_billing_recovery_schedule.dokploy_api.read_dokploy_application_schedule",
                return_value=existing_schedule,
            ),
            patch(
                "control_plane.workflows.verireel_billing_recovery_schedule.dokploy_api.upsert_dokploy_application_schedule",
                return_value=disabled_schedule,
            ) as upsert,
            patch(
                "control_plane.workflows.verireel_billing_recovery_schedule.dokploy_api.run_dokploy_schedule",
                side_effect=click.ClickException("canary failed"),
            ),
            patch(
                "control_plane.workflows.verireel_billing_recovery_schedule.dokploy_api.delete_dokploy_application_schedule"
            ) as delete_schedule,
        ):
            with self.assertRaisesRegex(click.ClickException, "canary failed"):
                reconcile_verireel_billing_recovery_schedule(
                    host="https://dokploy.example.com",
                    token="token-123",
                    application_id="application-123",
                    instance="testing",
                )

        upsert.assert_called_once()
        self.assertFalse(upsert.call_args.kwargs["schedule_payload"]["enabled"])
        delete_schedule.assert_not_called()

    def test_finalize_preserves_an_existing_disabled_schedule(self) -> None:
        snapshot = VeriReelRecoveryScheduleSnapshot(existed=True, enabled=False)
        with (
            patch(
                "control_plane.workflows.verireel_billing_recovery_schedule.restore_verireel_billing_recovery_schedule"
            ) as restore,
            patch(
                "control_plane.workflows.verireel_billing_recovery_schedule.reconcile_verireel_billing_recovery_schedule"
            ) as reconcile,
        ):
            finalize_verireel_billing_recovery_schedule(
                host="https://dokploy.example.com",
                token="token-123",
                application_id="application-123",
                instance="testing",
                snapshot=snapshot,
            )

        restore.assert_called_once()
        reconcile.assert_not_called()

    def test_preview_destroy_removes_the_exact_managed_schedule_before_app_delete(self) -> None:
        schedule = _application_schedule(cron_expression="12,27,42,57 * * * *", enabled=True)
        schedule["command"] = recovery_schedule_command("preview")
        with (
            patch(
                "control_plane.workflows.verireel_billing_recovery_schedule.dokploy_api.read_dokploy_application_schedule",
                return_value=schedule,
            ),
            patch(
                "control_plane.workflows.verireel_billing_recovery_schedule.dokploy_api.assert_dokploy_application_schedule_exact"
            ),
            patch(
                "control_plane.workflows.verireel_billing_recovery_schedule.dokploy_api.delete_dokploy_application_schedule"
            ) as delete_schedule,
        ):
            delete_verireel_billing_recovery_schedule(
                host="https://dokploy.example.com",
                token="token-123",
                application_id="application-123",
            )

        self.assertEqual(delete_schedule.call_args.kwargs["application_id"], "application-123")
        self.assertTrue(delete_schedule.call_args.kwargs["schedule_payload"]["enabled"])

    def test_preview_destroy_removes_a_drifted_managed_schedule(self) -> None:
        schedule = _application_schedule(cron_expression="0 * * * *", enabled=True)
        with (
            patch(
                "control_plane.workflows.verireel_billing_recovery_schedule.dokploy_api.read_dokploy_application_schedule",
                return_value=schedule,
            ),
            patch(
                "control_plane.workflows.verireel_billing_recovery_schedule.dokploy_api.delete_dokploy_application_schedule"
            ) as delete_schedule,
        ):
            delete_verireel_billing_recovery_schedule(
                host="https://dokploy.example.com",
                token="token-123",
                application_id="application-123",
            )

        self.assertEqual(
            delete_schedule.call_args.kwargs["schedule_payload"]["cronExpression"],
            "0 * * * *",
        )
