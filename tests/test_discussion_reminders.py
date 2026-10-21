from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
import json

from pydantic import ValidationError

from control_plane.contracts.discussion_topic import DiscussionTopicInput
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressNotificationDestination,
    PublicIngressNotificationPolicyRecord,
)
from control_plane.discussion_reminders import (
    DiscussionReminderScheduler,
    DiscussionReminderSettings,
    SETTINGS_KEY,
    due_reminders,
)
from control_plane.outbox_worker import run_outbox_worker_once
from control_plane.notifications import DiscordNotificationError
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.public_ingress_monitor import PublicIngressNotificationDriverSet
from tests.support.stores import sqlite_database_url
from tests.test_public_ingress_monitor import _profile


class DiscussionReminderTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = PostgresRecordStore(database_url=sqlite_database_url(self.root / "store.db"))
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.profile = _profile()
        self.store.write_product_profile_record(self.profile)
        self.now = datetime(2026, 10, 23, 19, 0, tzinfo=timezone.utc)
        self.store.write_public_ingress_notification_policy_record(
            PublicIngressNotificationPolicyRecord(
                policy_id="director-policy",
                product="launchplane",
                context="service",
                instance="prod",
                created_at=self.now.isoformat(),
                updated_at=self.now.isoformat(),
                destinations=(
                    PublicIngressNotificationDestination(
                        destination_id="director",
                        kind="discord",
                        discord_webhook_secret="director-alert",
                    ),
                ),
            )
        )
        self.sender = Mock()
        self.alert_sender = Mock()
        self.drivers = PublicIngressNotificationDriverSet(
            discord_sender=self.alert_sender,
            secret_resolver=lambda _: "https://discord.com/api/webhooks/fixture/opaque",
        )
        self.settings = DiscussionReminderSettings.model_validate(
            {
                "director": {
                    "policy_id": "director-policy",
                    "destination_id": "director",
                },
                "schedules": [
                    {
                        "product": self.profile.product,
                        "enabled": True,
                        "bridge_released": True,
                        "invitation_confirmed": True,
                        "timezone": "America/New_York",
                        "times": [{"weekday": 4, "time": "15:00"}, {"weekday": 5, "time": "10:00"}],
                        "meeting": "Saturday, usually around 2 PM ET",
                    }
                ],
            }
        )
        self.scheduler = DiscussionReminderScheduler(
            self.store,
            control_plane_root=self.root,
            public_url="https://plane.example.test",
            clock=lambda: self.now.timestamp(),
        )

    def configure(self) -> None:
        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="context",
                context="launchplane",
                updated_at=self.now.isoformat(),
                env={SETTINGS_KEY: self.settings.model_dump_json()},
            )
        )

    def work(self, *, sender: Mock | None = None) -> str:
        with patch.object(
            self.store,
            "_database_mutation_timestamp",
            return_value=self.now.isoformat().replace("+00:00", "Z"),
        ):
            return run_outbox_worker_once(
                record_store=self.store,
                control_plane_root=self.root,
                lease_owner="test-worker",
                notification_drivers=self.drivers,
                discussion_reminder_sender=sender,
                discussion_reminder_clock=lambda: self.now.timestamp(),
            ).status

    def test_no_configuration_or_uncleared_gate_sends_nothing(self) -> None:
        self.scheduler._run_sweep()
        self.assertEqual(self.work(sender=self.sender), "idle")
        for gate in ("enabled", "bridge_released", "invitation_confirmed"):
            with self.subTest(gate=gate):
                original = self.settings
                schedule = original.schedules[0].model_copy(update={gate: False})
                self.settings = original.model_copy(update={"schedules": (schedule,)})
                self.configure()
                self.scheduler._run_sweep()
                self.assertEqual(self.work(sender=self.sender), "idle")
                self.settings = original
        self.sender.assert_not_called()
        self.alert_sender.assert_not_called()

    def test_count_only_message_empty_message_and_durable_deduplication(self) -> None:
        self.configure()
        topic = self.store.add_discussion_topic(
            self.profile.product,
            "one",
            DiscussionTopicInput(
                title="Private-looking fixture question?",
                issue_url="https://git.example.test/issue/1",
            ),
        )
        self.store.add_discussion_topic(
            "another-product",
            "two",
            DiscussionTopicInput(title=topic.title, issue_url=topic.issue_url),
        )
        self.scheduler._run_sweep()
        self.scheduler._run_sweep()
        self.assertEqual(self.work(sender=self.sender), "delivered")
        content = self.sender.call_args.kwargs["content"]
        self.assertIn(str(len(self.store.list_discussion_topics(self.profile.product))), content)
        self.assertIn(f"/ui/products/{self.profile.product}/topics", content)
        self.assertNotIn(topic.title, content)
        self.assertNotIn(topic.issue_url, content)
        self.assertEqual(self.work(sender=self.sender), "idle")
        self.scheduler._run_sweep()
        self.assertEqual(self.work(sender=self.sender), "idle")
        self.store.close_discussion_topic(self.profile.product, "one")
        self.now = datetime(2026, 10, 24, 14, 0, tzinfo=timezone.utc)
        self.scheduler._run_sweep()
        self.assertEqual(self.work(sender=self.sender), "delivered")
        self.assertNotEqual(content, self.sender.call_args.kwargs["content"])

    def test_read_and_send_failures_alert_director_without_replaying_reminder(self) -> None:
        self.configure()
        for index, failure in enumerate(("read", "send")):
            with self.subTest(failure=failure):
                self.now = datetime(
                    2026, 10, 23 + index, 19 if index == 0 else 14, tzinfo=timezone.utc
                )
                self.scheduler._run_sweep()
                self.sender.side_effect = RuntimeError("sensitive provider response")
                if failure == "read":
                    with patch.object(
                        self.store, "list_discussion_topics", side_effect=RuntimeError("private")
                    ):
                        self.assertEqual(self.work(sender=self.sender), "pending")
                    self.sender.assert_not_called()
                else:
                    self.assertEqual(self.work(sender=self.sender), "failed")
                self.assertEqual(self.work(), "delivered")
                content = self.alert_sender.call_args.args[1]["content"]
                self.assertIn(self.profile.product, content)
                self.assertNotIn("private", content)
                self.assertNotIn("sensitive", content)
                if failure == "read":
                    self.now += timedelta(seconds=30)
                    self.sender.side_effect = None
                    self.assertEqual(self.work(sender=self.sender), "delivered")
                self.assertEqual(self.work(sender=self.sender), "idle")

    def test_default_sender_unavailable_alerts_and_gate_revocation_cancels_queued_send(
        self,
    ) -> None:
        self.configure()
        self.scheduler._run_sweep()
        self.assertEqual(self.work(), "failed")
        policy = self.store.list_public_ingress_notification_policy_records(product="launchplane")[
            0
        ]
        self.store.write_public_ingress_notification_policy_record(
            policy.model_copy(update={"product": ""})
        )
        self.assertEqual(self.work(), "pending")
        self.alert_sender.assert_not_called()
        self.store.write_public_ingress_notification_policy_record(policy)
        alert = next(
            item
            for item in self.store.list_outbox_delivery_records(kind="discussion_reminder")
            if item.payload["alert"]
        )
        self.now = datetime.fromisoformat(alert.next_attempt_at.replace("Z", "+00:00"))
        self.assertEqual(self.work(), "delivered")
        self.now = datetime(2026, 10, 24, 14, 0, tzinfo=timezone.utc)
        self.scheduler._run_sweep()
        schedule = self.settings.schedules[0].model_copy(update={"invitation_confirmed": False})
        self.settings = self.settings.model_copy(update={"schedules": (schedule,)})
        self.configure()
        self.assertEqual(self.work(sender=self.sender), "delivered")
        self.sender.assert_not_called()

    def test_weekly_local_times_dst_override_and_expired_slot(self) -> None:
        schedule = self.settings.schedules[0]
        self.assertEqual(len(due_reminders(schedule, self.now)), 1)
        self.assertEqual(due_reminders(schedule, self.now.replace(hour=20)), ())
        winter = datetime(2026, 11, 6, 20, 0, tzinfo=timezone.utc)
        self.assertEqual(len(due_reminders(schedule, winter)), 1)
        overridden = schedule.model_copy(update={"overrides": {"2026-10-23": "16:00"}})
        self.assertEqual(due_reminders(overridden, self.now), ())
        self.assertEqual(len(due_reminders(overridden, self.now.replace(hour=20))), 1)

    def test_malformed_schedule_alerts_using_independent_director_destination(self) -> None:
        self.configure()
        data = self.settings.model_dump(mode="json")
        data["schedules"][0]["times"][0]["time"] = "not-a-time"
        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="context",
                context="launchplane",
                updated_at=self.now.isoformat(),
                env={SETTINGS_KEY: json.dumps(data)},
            )
        )
        with self.assertLogs("control_plane.discussion_reminders", level="ERROR") as logged:
            self.scheduler.run_once()
            self.scheduler.run_once()
        self.assertEqual(len(logged.output), 1)
        self.assertEqual(self.work(), "delivered")
        self.assertEqual(self.work(), "idle")
        self.alert_sender.assert_called_once()
        self.sender.assert_not_called()

    def test_expired_queue_and_lost_confirmation_alert_instead_of_sending_again(self) -> None:
        self.configure()
        self.scheduler.run_once()
        self.now = self.now.replace(hour=20)
        self.assertEqual(self.work(sender=self.sender), "failed")
        self.sender.assert_not_called()
        self.assertEqual(self.work(), "delivered")
        self.now = datetime(2026, 10, 24, 14, 0, tzinfo=timezone.utc)
        self.scheduler.run_once()
        record = self.store.list_outbox_delivery_records(kind="discussion_reminder")[-1]
        self.store.write_outbox_delivery_record(
            record.model_copy(
                update={
                    "state": "reconcile_required",
                    "provider_operation_key": record.delivery_id,
                }
            )
        )
        self.assertEqual(self.work(sender=self.sender), "failed")
        self.sender.assert_not_called()
        self.assertEqual(self.work(), "delivered")

    def test_invalid_input_and_other_contexts_cannot_enable_reminders(self) -> None:
        data = self.settings.model_dump(mode="json")
        for field, bad in (
            ("timezone", "Not/AZone"),
            ("meeting", "@everyone\nprivate"),
            ("meeting", "https://example.test"),
            ("meeting", "[link](https://example.test)"),
        ):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                candidate = json.loads(json.dumps(data))
                candidate["schedules"][0][field] = bad
                DiscussionReminderSettings.model_validate(candidate)
        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="context",
                context="other-product",
                updated_at=self.now.isoformat(),
                env={SETTINGS_KEY: self.settings.model_dump_json()},
            )
        )
        self.scheduler.run_once()
        self.assertEqual(self.work(sender=self.sender), "idle")

    def test_retired_product_skips_and_director_alert_failure_stays_visible(self) -> None:
        self.configure()
        self.store.write_product_profile_record(
            self.profile.model_copy(update={"lifecycle_state": "retired"})
        )
        self.scheduler.run_once()
        self.assertEqual(self.work(sender=self.sender), "delivered")
        self.sender.assert_not_called()
        self.store.write_product_profile_record(self.profile)
        self.now = datetime(2026, 10, 24, 14, 0, tzinfo=timezone.utc)
        self.scheduler.run_once()
        self.assertEqual(self.work(), "failed")
        self.alert_sender.side_effect = DiscordNotificationError(429)
        self.assertEqual(self.work(), "pending")
        alert = next(
            item
            for item in self.store.list_outbox_delivery_records(kind="discussion_reminder")
            if item.payload["alert"]
        )
        self.assertFalse(alert.provider_operation_key)
        self.now = datetime.fromisoformat(alert.next_attempt_at.replace("Z", "+00:00"))
        self.alert_sender.side_effect = OSError("secret-bearing-provider-response")
        with self.assertLogs("control_plane.discussion_reminders", level="CRITICAL") as captured:
            self.assertEqual(self.work(), "failed")
        self.assertNotIn("secret-bearing", str(captured.output))
        self.assertEqual(self.work(), "idle")

    def test_lost_marker_ownership_never_sends_or_publishes_a_failure_alert(self) -> None:
        self.configure()
        self.scheduler.run_once()
        with patch.object(
            self.store,
            "mark_outbox_delivery_provider_started",
            return_value=SimpleNamespace(status="lease_expired"),
        ):
            with self.assertRaisesRegex(RuntimeError, "outbox_provider_marker_not_recorded"):
                self.work(sender=self.sender)
        self.sender.assert_not_called()
        self.assertFalse(
            any(
                item.payload["alert"]
                for item in self.store.list_outbox_delivery_records(kind="discussion_reminder")
            )
        )
        self.now += timedelta(seconds=301)
        self.assertEqual(self.work(sender=self.sender), "delivered")
        self.now = datetime(2026, 10, 24, 14, 0, tzinfo=timezone.utc)
        self.scheduler.run_once()
        self.assertEqual(self.work(), "failed")
        with patch.object(
            self.store,
            "mark_outbox_delivery_provider_started",
            return_value=SimpleNamespace(status="lease_expired"),
        ):
            with self.assertRaisesRegex(RuntimeError, "outbox_provider_marker_not_recorded"):
                self.work()
        self.alert_sender.assert_not_called()
        self.now += timedelta(seconds=301)
        self.assertEqual(self.work(), "delivered")

    def test_harmless_schedule_edit_preserves_due_reminder_and_disabled_config_is_quiet(
        self,
    ) -> None:
        self.configure()
        self.scheduler.run_once()
        schedule = self.settings.schedules[0].model_copy(
            update={
                "meeting": "Saturday, around 3 PM ET",
                "overrides": {"2026-10-30": None},
            }
        )
        self.settings = self.settings.model_copy(update={"schedules": (schedule,)})
        self.configure()
        self.assertEqual(self.work(sender=self.sender), "delivered")
        self.assertIn(schedule.meeting, self.sender.call_args.kwargs["content"])
        self.settings = self.settings.model_copy(update={"schedules": ()})
        self.configure()
        self.scheduler.public_url = ""
        self.scheduler.run_once()
        self.assertEqual(self.work(), "idle")
        self.alert_sender.assert_not_called()

    def test_repeated_read_failure_alerts_and_stops_at_budget_without_sending(self) -> None:
        self.configure()
        self.scheduler.run_once()
        maximum = self.store.list_outbox_delivery_records(kind="discussion_reminder")[
            0
        ].max_attempts
        with patch.object(self.store, "list_discussion_topics", side_effect=OSError("private")):
            for attempt in range(1, maximum + 1):
                self.assertEqual(
                    self.work(sender=self.sender), "pending" if attempt < maximum else "failed"
                )
                if attempt in (1, maximum):
                    self.assertEqual(self.work(), "delivered")
                if attempt < maximum:
                    record = next(
                        item
                        for item in self.store.list_outbox_delivery_records(
                            kind="discussion_reminder"
                        )
                        if not item.payload["alert"]
                    )
                    self.now = datetime.fromisoformat(record.next_attempt_at.replace("Z", "+00:00"))
        self.assertEqual(self.work(), "idle")
        self.sender.assert_not_called()

    def test_spring_gap_first_fall_fold_and_null_override(self) -> None:
        data = self.settings.schedules[0].model_dump(mode="json")
        data["times"] = [{"weekday": 6, "time": "02:30"}]
        schedule = type(self.settings.schedules[0]).model_validate(data)
        self.assertEqual(
            due_reminders(schedule, datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc)), ()
        )
        data["times"] = [{"weekday": 6, "time": "01:30"}]
        schedule = type(schedule).model_validate(data)
        first = datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)
        self.assertEqual(due_reminders(schedule, first), (first,))
        self.assertEqual(due_reminders(schedule, first + timedelta(hours=1)), ())
        skipped = schedule.model_copy(update={"overrides": {first.date().isoformat(): None}})
        self.assertEqual(due_reminders(skipped, first), ())


if __name__ == "__main__":
    unittest.main()
