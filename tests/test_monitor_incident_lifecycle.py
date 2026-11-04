from datetime import datetime, timedelta
from itertools import product
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from typing import Any, cast
from unittest.mock import patch

from control_plane.contracts.product_profile_record import ProductLaneProfile
from control_plane.contracts.public_ingress_monitoring import PublicIngressNotificationDestination
from control_plane.outbox_worker import run_outbox_worker_once
from control_plane.health_monitor_scheduler import HealthMonitorScheduler
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.public_ingress_monitor import (
    HttpObservation,
    PublicIngressNotificationDriverSet,
    run_public_ingress_monitor_once,
)
from tests.support.stores import _sqlite_database_url
from tests.test_public_ingress_monitor import _identity, _notification_policy, _profile


class MonitorIncidentLifecycleTests(unittest.TestCase):
    def test_changed_health_target_cannot_keep_verified_probe_credit(self) -> None:
        from control_plane.lane_health_history import summarize_observations

        for failed, incident_retry in product((False, True), repeat=2):
            with (
                self.subTest(failed=failed, incident_retry=incident_retry),
                TemporaryDirectory() as directory,
            ):
                store = PostgresRecordStore(
                    database_url=_sqlite_database_url(Path(directory) / "db")
                )
                self.addCleanup(store.close)
                store.ensure_schema()
                profile = _profile()
                store.write_product_profile_record(profile)
                write_transition = store.write_public_ingress_transition_with_outbox
                attempts = 0

                def change_health_target_before_publication(**kwargs: Any) -> object:
                    nonlocal attempts
                    attempts += 1
                    if incident_retry and attempts == 1:
                        return write_transition(
                            **{**kwargs, "expected_open_incident_id": "concurrently-replaced"}
                        )
                    lane = profile.lanes[0].model_copy(
                        update={"health_url": "https://replacement.test/health"}
                    )
                    store.write_product_profile_record(
                        profile.model_copy(update={"lanes": (lane,)})
                    )
                    return write_transition(**kwargs)

                with (
                    patch.object(
                        store,
                        "write_public_ingress_transition_with_outbox",
                        side_effect=change_health_target_before_publication,
                    ),
                    patch(
                        "control_plane.workflows.public_ingress_monitor._expected_runtime_identity",
                        return_value=_identity(),
                    ),
                ):
                    result = run_public_ingress_monitor_once(
                        record_store=store,
                        checked_at="2026-07-27T16:42:00Z",
                        http_get=lambda url, timeout: HttpObservation(
                            503 if failed and url == profile.lanes[0].base_url else 200,
                            url,
                            0,
                            {"runtime_identity": _identity().model_dump(mode="json")},
                        ),
                        notify=False,
                    )
                self.assertEqual(result.authority_changed_count, 1)
                self.assertEqual(result.pass_count, 0)
                stored = store.list_public_ingress_observation_records()
                self.assertEqual(len(stored), 1)
                self.assertEqual(stored[0].status, "fail" if failed else "skipped")
                self.assertEqual(result.records[0], stored[0])
                self.assertEqual(store.list_public_ingress_incident_records(), ())
                checks = ((stored[0].check_kind, stored[0].check_name),)
                start = datetime.fromisoformat(stored[0].observed_at)
                accounting = summarize_observations(
                    stored,
                    since=start,
                    until=start + timedelta(seconds=60),
                    instance=stored[0].instance,
                    required_checks=checks,
                    identity_checks=checks,
                )
                self.assertEqual(accounting.lane_health_pass_seconds, 0)
                self.assertEqual(accounting.lane_identity_verified_seconds, 0)
                if failed:
                    self.assertEqual(stored[0].failure_code, "http_error")
                    self.assertEqual(
                        accounting.lane_health_fail_seconds, accounting.denominator_seconds
                    )
                else:
                    self.assertEqual(
                        accounting.lane_health_unknown_seconds, accounting.denominator_seconds
                    )

    def test_removed_lane_reconciles_without_rewriting_failed_probe(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(database_url=_sqlite_database_url(Path(directory) / "db"))
            self.addCleanup(store.close)
            store.ensure_schema()
            profile = _profile()
            store.write_product_profile_record(profile)
            failed = run_public_ingress_monitor_once(
                record_store=store,
                checked_at="2026-07-27T16:41:00Z",
                http_get=lambda url, timeout: HttpObservation(503, url, 0),
                notify=False,
            )
            incident = failed.incidents[0]
            original = store.read_public_ingress_observation_record(incident.latest_observation_id)
            replacement = ProductLaneProfile(instance="testing", context="replacement-site")
            store.write_product_profile_record(profile.model_copy(update={"lanes": (replacement,)}))
            scheduler = HealthMonitorScheduler(store)
            selected = frozenset(scheduler._revisions("prod"))
            self.assertIn((incident.product, incident.context, incident.instance), selected)
            write_transition = store.write_public_ingress_transition_with_outbox

            def restore_lane_before_publication(**kwargs: Any) -> object:
                store.write_product_profile_record(profile)
                return write_transition(**kwargs)

            with patch.object(
                store,
                "write_public_ingress_transition_with_outbox",
                side_effect=restore_lane_before_publication,
            ):
                raced = run_public_ingress_monitor_once(
                    record_store=store,
                    checked_at="2026-07-27T16:42:00Z",
                    lane_keys=selected,
                    notify=False,
                )
            self.assertEqual(raced.authority_changed_count, 1)
            rejected = store.read_public_ingress_observation_record(raced.records[0].record_id)
            self.assertEqual(rejected.purpose, "reconciliation")
            self.assertEqual(rejected.status, "skipped")
            self.assertEqual(
                store.read_public_ingress_incident_record(incident.incident_id).status, "open"
            )
            store.write_product_profile_record(profile.model_copy(update={"lanes": (replacement,)}))
            reconciled = run_public_ingress_monitor_once(
                record_store=store,
                checked_at="2026-07-27T16:43:00Z",
                lane_keys=selected,
                notify=False,
            )
            settled = store.read_public_ingress_incident_record(incident.incident_id)
            self.assertEqual(settled.status, "resolved")
            self.assertEqual(settled.resolution_reason, "monitoring_intent_changed")
            self.assertEqual(reconciled.reconciliation_count, 1)
            self.assertEqual(reconciled.pass_count, 0)
            self.assertEqual(
                store.read_public_ingress_observation_record(original.record_id), original
            )
            self.assertEqual(reconciled.records[-1].purpose, "reconciliation")
            self.assertEqual(reconciled.records[-1].status, "skipped")
            self.assertEqual(reconciled.records[-1].monitoring_intent, original.monitoring_intent)

    def test_store_backed_worker_closes_recovered_issue_and_suppresses_old_failure(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(database_url=_sqlite_database_url(Path(directory) / "db"))
            self.addCleanup(store.close)
            store.ensure_schema()
            store.write_product_profile_record(_profile())
            store.write_public_ingress_notification_policy_record(
                _notification_policy(
                    PublicIngressNotificationDestination(
                        destination_id="github",
                        kind="github_issue",
                        github_repository="example/site",
                        github_label="incident",
                    )
                ).model_copy(update={"reminder_interval_seconds": 900})
            )
            actions: list[str] = []

            def github(action: str, payload: dict[str, object]) -> dict[str, object]:
                actions.append(action)
                return {"url": "https://github.com/example/site/issues/1", "id": "issue-1"}

            drivers = PublicIngressNotificationDriverSet(github_client=github)

            def monitor(at: str, code: int) -> None:
                run_public_ingress_monitor_once(
                    record_store=store,
                    checked_at=at,
                    http_get=lambda url, timeout: HttpObservation(code, url, 0),
                )

            def worker() -> None:
                run_outbox_worker_once(
                    record_store=store,
                    control_plane_root=Path(directory),
                    lease_owner="worker",
                    notification_drivers=drivers,
                )

            monitor("2026-07-27T16:00:00Z", 503)
            worker()
            self.assertEqual(actions, ["create"])
            monitor("2026-07-27T16:20:00Z", 503)
            queued = store.list_outbox_delivery_records(
                states=("pending",), kind="public_ingress_notification"
            )
            self.assertTrue(
                any(
                    cast(dict[str, object], row.payload["attempt"]).get("event") == "reminder"
                    for row in queued
                )
            )
            monitor("2026-07-27T16:21:00Z", 200)
            store.close()
            store = PostgresRecordStore(database_url=_sqlite_database_url(Path(directory) / "db"))
            self.addCleanup(store.close)
            worker()
            worker()
            worker()
            self.assertEqual(actions, ["create", "close"])
            deliveries = store.list_outbox_delivery_records(kind="public_ingress_notification")
            self.assertTrue(all(row.state == "delivered" for row in deliveries))
