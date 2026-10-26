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
from tests.test_public_ingress_monitor import _notification_policy, _profile


class MonitorIncidentLifecycleTests(unittest.TestCase):
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
