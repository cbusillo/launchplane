from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal
import unittest
from unittest.mock import patch

from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressFailureCode,
    PublicIngressNotificationDestination,
    PublicIngressObservationRecord,
    PublicIngressTargetObservation,
)
from control_plane.contracts.secret_record import SecretRecord
from control_plane.discord_incident_summary import (
    build_discord_incident_summary_delivery,
)
from control_plane.notifications import DiscordNotificationError, DiscordNotificationNotSentError
from control_plane.outbox_inspection import inspect_outbox
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.public_ingress_monitor import (
    PublicIngressNotificationDriverSet,
    _store_monitor_observation,
)
from tests.support.stores import _sqlite_database_url
from tests.test_public_ingress_monitor import _notification_policy
from tests.test_public_ingress_direct_outbox import _is_red
from tests import test_public_ingress_direct_outbox as direct_outbox


class DiscordIncidentSummaryTests(unittest.TestCase):
    def database_url(self) -> str:
        directory = self.enterContext(TemporaryDirectory())
        return _sqlite_database_url(Path(directory) / "records.sqlite3")

    def setUp(self) -> None:
        self.store = PostgresRecordStore(database_url=self.database_url())
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.destination = PublicIngressNotificationDestination(
            destination_id="discord", kind="discord", discord_webhook_secret="monitor-webhook"
        )
        self.policy = _notification_policy(self.destination).model_copy(
            update={"product": "demo", "context": "demo", "instance": ""}
        )
        self.store.write_public_ingress_notification_policy_record(self.policy)
        self.secret = SecretRecord(
            secret_id="monitor-webhook",
            scope="global",
            integration="discord",
            name="monitor",
            current_version_id="test-version",
            created_at="2026-10-25T12:00:00Z",
            updated_at="2026-10-25T12:00:00Z",
        )
        self.store.write_secret_record(self.secret)
        self.sent: list[dict[str, object]] = []
        self.drivers = PublicIngressNotificationDriverSet(
            secret_resolver=lambda _name: "https://discord.com/api/webhooks/test/test",
            discord_sender=lambda _url, payload: self.sent.append(payload),
        )

    def observe(
        self,
        instance: str,
        *,
        time: str = "12:00:00",
        status: Literal["pass", "fail"] = "fail",
        critical: bool = False,
        code: PublicIngressFailureCode | None = None,
        enqueued_time: str | None = None,
    ) -> None:
        failure = (
            (code or ("http_error" if critical else "deploy_fence_held"))
            if status == "fail"
            else None
        )
        record = PublicIngressObservationRecord(
            record_id=f"{instance}-{time}",
            product="demo",
            context="demo",
            instance=instance,
            check_kind="public_http" if critical else "provider",
            check_name="health" if critical else "launchplane-deploy-fence",
            observed_at=f"2026-10-25T{time}Z",
            status=status,
            failure_code=failure,
            targets=(
                PublicIngressTargetObservation(
                    target="health_url",
                    url="https://example.test/health",
                    status=status,
                    failure_code=failure,
                    summary="Measured health",
                ),
            ),
            summary="Measured observation",
        )
        with patch.object(
            self.store,
            "_database_mutation_timestamp",
            return_value=f"2026-10-25T{enqueued_time}Z" if enqueued_time else record.observed_at,
        ):
            _store_monitor_observation(
                record_store=self.store,
                record=record,
                incident_eligible=True,
                expected_profile_sha256="",
                recovery_observation_threshold=1,
                notify=True,
                notification_drivers=self.drivers,
            )

    def drain(self, time: str) -> None:
        for _ in range(100):
            result = direct_outbox.DirectNotificationOutboxTests._work(
                self.store, self.drivers, f"2026-10-25T{time}Z"
            )
            if result.status == "idle":
                return
        self.fail("Outbox did not settle within its fixture inventory")

    def test_worker_interleaved_with_fleet_burst_sends_one_grouped_summary(self) -> None:
        self.observe("preview-one")
        self.drain("12:00:01")
        self.observe("preview-two", time="12:04:00")
        self.drain("12:04:01")
        self.assertEqual(self.sent, [])
        self.drain("12:05:00")
        self.assertEqual(len(self.sent), 1)
        self.assertFalse(_is_red(self.sent[0]))
        mentions = self.sent[0].get("allowed_mentions")
        assert isinstance(mentions, dict)
        self.assertEqual(mentions.get("parse"), [])
        self.assertTrue(
            all(
                value.status == "open"
                for value in self.store.list_public_ingress_incident_records()
            )
        )

    def test_late_probe_queues_after_the_already_sent_batch(self) -> None:
        self.observe("one")
        self.drain("12:05:00")
        self.observe("two", time="12:01:00", enqueued_time="12:06:00")
        self.drain("12:06:01")
        self.assertEqual(len(self.sent), 1)
        self.drain("12:10:00")
        self.assertEqual(len(self.sent), 2)

    def test_clearing_subset_and_unchanged_reminders_do_not_repost(self) -> None:
        self.observe("one")
        self.observe("two", time="12:01:00")
        self.drain("12:05:00")
        self.observe("two", time="12:06:00", status="pass")
        self.observe("one", time="19:00:00")
        self.drain("19:05:00")
        self.assertEqual(len(self.sent), 1)
        self.observe("one", time="19:06:00", status="pass")
        self.drain("19:10:00")
        self.assertEqual(len(self.sent), 1)

    def test_overdue_windows_fold_into_one_current_summary(self) -> None:
        self.observe("one")
        self.observe("two", time="12:06:00")
        self.observe("three", time="12:12:00")
        self.drain("12:30:00")
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(
            all(value.state == "delivered" for value in self.store.list_outbox_delivery_records())
        )

    def test_scope_silence_and_acknowledgement_preserve_only_eligible_conditions(self) -> None:
        self.store.write_secret_record(
            self.secret.model_copy(
                update={"scope": "context_instance", "context": "demo", "instance": "allowed"}
            )
        )
        for index, instance in enumerate(("allowed", "foreign", "silenced", "acknowledged")):
            self.observe(instance, time=f"12:0{index}:00")
            if instance in {"silenced", "acknowledged"}:
                incident = next(
                    value
                    for value in self.store.list_public_ingress_incident_records()
                    if value.instance == instance
                )
                self.store.write_public_ingress_incident_record(
                    incident.model_copy(
                        update={
                            "notification_state": instance,
                            "silenced_until": "2026-10-25T12:06:00Z"
                            if instance == "silenced"
                            else "",
                        }
                    )
                )
        self.drain("12:05:00")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("allowed", str(self.sent[0]))
        for instance in ("foreign", "silenced", "acknowledged"):
            self.assertNotIn(instance, str(self.sent[0]))
        self.observe("allowed", time="19:00:00")
        self.drain("19:05:00")
        self.assertEqual(len(self.sent), 1)

    def test_wholly_out_of_scope_summary_skips_without_retry_or_failure(self) -> None:
        self.store.write_secret_record(
            self.secret.model_copy(
                update={"scope": "context_instance", "context": "demo", "instance": "prod"}
            )
        )
        self.observe("preview-one")
        self.drain("12:05:00")
        rows = self.store.list_outbox_delivery_records()
        self.assertEqual(self.sent, [])
        self.assertTrue(
            all(
                value.state == "delivered"
                and not value.error_code
                and not value.provider_operation_key
                for value in rows
            )
        )

    def test_silence_expiry_and_returning_material_state_can_be_summarized(self) -> None:
        self.observe("one")
        self.observe("two", time="12:01:00")
        incident = next(
            value
            for value in self.store.list_public_ingress_incident_records()
            if value.instance == "two"
        )
        self.store.write_public_ingress_incident_record(
            incident.model_copy(
                update={"notification_state": "silenced", "silenced_until": "2026-10-25T12:06:00Z"}
            )
        )
        self.drain("12:05:00")
        self.observe("two", time="19:00:00")
        self.drain("19:05:00")
        self.assertEqual(len(self.sent), 2)
        self.observe("one", time="19:06:00", code="testing_delivery_failed")
        self.drain("19:10:00")
        self.observe("one", time="19:11:00")
        self.drain("19:15:00")
        self.assertEqual(len(self.sent), 4)

    def test_critical_failure_and_material_change_page_but_reminder_is_quiet(self) -> None:
        self.observe("prod", critical=True)
        self.drain("12:00:01")
        self.observe("prod", critical=True, time="12:01:00", code="dns_failure")
        self.drain("12:01:01")
        self.assertEqual(sum(_is_red(value) for value in self.sent), 2)
        self.observe("prod", critical=True, time="19:00:00", code="dns_failure")
        self.drain("19:05:00")
        self.assertEqual(sum(_is_red(value) for value in self.sent), 2)
        self.assertEqual(len(self.sent), 3)

    def test_unknown_summary_retains_rendered_state_and_survives_routing_edits(self) -> None:
        self.observe("one")

        def lose_response(_url: str, payload: dict[str, object]) -> None:
            self.sent.append(payload)
            raise TimeoutError("response lost")

        self.drivers.discord_sender = lose_response
        self.drain("12:05:00")
        failed = self.store.list_outbox_delivery_records()[0]
        self.assertTrue(failed.provider_operation_key)
        self.assertTrue(failed.payload.get("summary_states"))
        inspection = inspect_outbox(self.store, limit=100)
        self.assertTrue(
            any(
                value["error_code"] == "delivery_outcome_unknown" for value in inspection.deliveries
            )
        )
        self.store.write_public_ingress_notification_policy_record(
            self.policy.model_copy(update={"updated_at": "2026-10-25T18:00:00Z"})
        )
        self.store.write_secret_record(
            self.secret.model_copy(
                update={"current_version_id": "rotated", "updated_at": "2026-10-25T18:00:00Z"}
            )
        )
        self.observe("one", time="19:00:00")
        self.drain("19:05:00")
        self.assertEqual(len(self.sent), 1)

    def test_preconnect_and_rate_limit_retry_critical_alert(self) -> None:
        self.observe("prod", critical=True)
        for error, time in (
            (DiscordNotificationNotSentError("not sent"), "12:00:01"),
            (DiscordNotificationError(429), "12:01:00"),
        ):
            self.drivers.discord_sender = lambda _url, _payload: (_ for _ in ()).throw(error)
            self.drain(time)
            self.assertEqual(self.store.list_outbox_delivery_records()[0].state, "pending")
        self.drivers.discord_sender = lambda _url, payload: self.sent.append(payload)
        self.drain("12:02:00")
        self.assertEqual(len(self.sent), 1)

    def test_summary_retries_keep_one_budget_across_window_boundaries(self) -> None:
        self.observe("one")
        calls = []

        def refuse(_url: str, _payload: dict[str, object]) -> None:
            calls.append(1)
            raise DiscordNotificationError(429)

        self.drivers.discord_sender = refuse
        budget = self.store.list_outbox_delivery_records()[0].max_attempts
        for index in range(budget + 1):
            self.drain(f"{12 + index:02d}:05:00")
        self.assertEqual(len(calls), budget)
        rows = self.store.list_outbox_delivery_records()
        failed = [value for value in rows if value.state == "failed"]
        self.assertTrue(failed)
        self.assertTrue(all(not value.provider_operation_key for value in failed))

    def test_new_channel_reference_receives_existing_conditions(self) -> None:
        self.observe("one")
        self.drain("12:05:00")
        replacement = self.secret.model_copy(update={"secret_id": "other-webhook", "name": "other"})
        self.store.write_secret_record(replacement)
        self.store.write_public_ingress_notification_policy_record(
            self.policy.model_copy(
                update={
                    "destinations": (
                        self.destination.model_copy(
                            update={"discord_webhook_secret": replacement.secret_id}
                        ),
                    )
                }
            )
        )
        self.observe("one", time="19:00:00")
        self.drain("19:05:00")
        self.assertEqual(len(self.sent), 2)

    def test_quiet_and_critical_http_failures_do_not_replay_uncertain_sends(self) -> None:
        for index, (critical, status) in enumerate(
            ((False, 404), (False, 503), (True, 503), (True, None))
        ):
            with self.subTest(critical=critical, status=status):
                instance = f"lane-{critical}-{status}"
                self.observe(instance, critical=critical, time=f"{12 + index}:00:00")

                def fail(_url: str, payload: dict[str, object]) -> None:
                    self.sent.append(payload)
                    if status is None:
                        raise TimeoutError("response lost")
                    raise DiscordNotificationError(status)

                self.drivers.discord_sender = fail
                before = len(self.sent)
                self.drain(f"{12 + index}:05:00")
                self.drain(f"{12 + index}:10:00")
                self.assertEqual(len(self.sent), before + 1)
                row = max(self.store.list_outbox_delivery_records(), key=lambda v: v.updated_at)
                self.assertEqual(row.state, "failed")
                self.assertEqual(bool(row.provider_operation_key), status != 404)
                self.observe(instance, critical=critical, time=f"{12 + index}:11:00", status="pass")
                self.drain(f"{12 + index}:15:00")

    def test_delayed_critical_page_respects_silence_and_acknowledgement(self) -> None:
        for state in ("silenced", "acknowledged"):
            self.observe(state, critical=True)
            incident = next(
                v for v in self.store.list_public_ingress_incident_records() if v.instance == state
            )
            self.store.write_public_ingress_incident_record(
                incident.model_copy(
                    update={
                        "notification_state": state,
                        "silenced_until": "2026-10-25T20:00:00Z" if state == "silenced" else "",
                    }
                )
            )
        self.drain("12:00:01")
        self.assertEqual(self.sent, [])
        self.assertTrue(
            all(v.status == "open" for v in self.store.list_public_ingress_incident_records())
        )

    def test_critical_pre_send_resolver_failure_does_not_claim_unknown_effect(self) -> None:
        self.observe("prod", critical=True)

        def unavailable(_name: str) -> str:
            raise RuntimeError("unavailable")

        self.drivers.secret_resolver = unavailable
        self.drain("12:00:01")
        row = self.store.list_outbox_delivery_records()[0]
        self.assertEqual(row.state, "pending")
        self.assertFalse(row.provider_operation_key)
        self.assertEqual(self.sent, [])

    def test_older_render_cannot_post_after_a_newer_started_window(self) -> None:
        for time in ("12:00:00", "12:06:00"):
            row = build_discord_incident_summary_delivery(
                policy_id=self.policy.policy_id,
                destination_id=self.destination.destination_id,
                created_at=f"2026-10-25T{time}Z",
            )
            self.store.write_outbox_delivery_record(row)
        with patch.object(
            self.store, "_database_mutation_timestamp", return_value="2026-10-25T12:11:00Z"
        ):
            claims = [
                self.store.claim_next_outbox_delivery_record(lease_owner=owner).record
                for owner in ("old", "new")
            ]
            old, new = claims
            assert old is not None and new is not None
            for record, owner in ((new, "new"), (old, "old")):
                result = self.store.mark_outbox_delivery_provider_started(
                    record=record.model_copy(
                        update={"payload": {**record.payload, "summary_states": [[owner, "state"]]}}
                    ),
                    lease_owner=owner,
                    provider_operation_key=owner,
                    provider_id="discord",
                )
                self.assertEqual(result.status == "updated", owner == "new")
        self.assertFalse(
            self.store.read_outbox_delivery_record(old.delivery_id).provider_operation_key
        )

    def test_atomic_marker_rechecks_adjacent_windows_and_persists_snapshot(self) -> None:
        rows = []
        for time in ("12:00:00", "12:06:00"):
            row = build_discord_incident_summary_delivery(
                policy_id=self.policy.policy_id,
                destination_id=self.destination.destination_id,
                created_at=f"2026-10-25T{time}Z",
            )
            self.store.write_outbox_delivery_record(row)
        with patch.object(
            self.store, "_database_mutation_timestamp", return_value="2026-10-25T12:11:00Z"
        ):
            for owner in ("first", "second"):
                claim = self.store.claim_next_outbox_delivery_record(lease_owner=owner)
                self.assertIsNotNone(claim.record)
                assert claim.record is not None
                rows.append(
                    claim.record.model_copy(
                        update={
                            "payload": {
                                **claim.record.payload,
                                "summary_states": [["incident", "fingerprint"]],
                            }
                        }
                    )
                )
            first = self.store.mark_outbox_delivery_provider_started(
                record=rows[0],
                lease_owner="first",
                provider_operation_key="first-send",
                provider_id="discord",
            )
            second = self.store.mark_outbox_delivery_provider_started(
                record=rows[1],
                lease_owner="second",
                provider_operation_key="second-send",
                provider_id="discord",
            )
        self.assertEqual(first.status, "updated")
        self.assertNotEqual(second.status, "updated")
        self.assertFalse(
            self.store.read_outbox_delivery_record(rows[1].delivery_id).provider_operation_key
        )
        self.assertEqual(
            self.store.read_outbox_delivery_record(rows[0].delivery_id).payload, rows[0].payload
        )
