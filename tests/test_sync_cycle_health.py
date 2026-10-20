from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from control_plane.contracts.product_environment_read_model import build_product_environment_detail
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneHealthCheck,
    ProductLaneHealthMonitoringPolicy,
    ProductLaneProfile,
)
from control_plane.contracts.public_ingress_monitoring import PublicIngressObservationRecord
from control_plane.contracts.sync_cycle_health import sync_cycle_health_evidence_from_payload
from control_plane.workflows.public_ingress_monitor import HttpObservation
from control_plane.workflows.public_ingress_monitor import run_public_ingress_monitor_once
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.stores import _sqlite_database_url
from tests.support.sync_cycle_health import sync_cycle_payload
from tests.test_product_environment_read_model import (
    _PublicIngressReadModelStore,
    _site_profile_payload,
)
from tests.test_public_ingress_monitor import _Store, _add_private_endpoint, _profile


class SyncCycleHealthTests(unittest.TestCase):
    def test_records_completed_incremental_without_business_rows_or_errors(self) -> None:
        private = "private-source-error-and-business-sentinel"
        evidence = sync_cycle_health_evidence_from_payload(
            {"status": "ok", "sync": sync_cycle_payload(last_error=private, rows=[private])}
        )
        assert evidence is not None and evidence.cycle is not None
        self.assertEqual(evidence.status, "recorded")
        self.assertEqual(evidence.cycle.status, "success")
        self.assertEqual(evidence.cycle.mode, "incremental")
        self.assertEqual(evidence.cycle.cycle_id, sync_cycle_payload()["cycle_id"])
        self.assertEqual(evidence.cycle.last_heartbeat, sync_cycle_payload()["last_heartbeat"])
        self.assertNotIn(private, evidence.model_dump_json())

    def test_absent_sync_preserves_generic_health_wire_shape(self) -> None:
        payloads: tuple[object, ...] = (None, [], {"status": "ok"})
        for payload in payloads:
            with self.subTest(payload=payload):
                evidence = sync_cycle_health_evidence_from_payload(payload)
                self.assertIsNone(evidence)

    def test_preserves_full_running_failed_stale_and_overdue_facts(self) -> None:
        cases = (
            sync_cycle_payload(mode="full"),
            sync_cycle_payload(status="running", cycle_finished_at=None),
            sync_cycle_payload(
                status="running", cycle_finished_at=None, is_stale=True, freshness_status="stale"
            ),
            sync_cycle_payload(status="failed", freshness_status="failed"),
            sync_cycle_payload(next_cycle_overdue=True, freshness_status="overdue"),
            sync_cycle_payload(stale_threshold_seconds=0),
        )
        for cycle in cases:
            with self.subTest(cycle=cycle):
                evidence = sync_cycle_health_evidence_from_payload({"sync": cycle})
                assert evidence is not None and evidence.cycle is not None
                self.assertEqual(evidence.status, "recorded")
                for field in (
                    "status",
                    "mode",
                    "is_stale",
                    "next_cycle_overdue",
                    "freshness_status",
                    "stale_threshold_seconds",
                ):
                    self.assertEqual(getattr(evidence.cycle, field), cycle[field])

    def test_unknown_idle_and_unavailable_are_not_completed_cycles(self) -> None:
        for status, freshness, stale in (
            ("unknown", "unknown", False),
            ("idle", "fresh", False),
            ("unavailable", "unavailable", True),
        ):
            with self.subTest(status=status):
                cycle = sync_cycle_payload(
                    status=status,
                    mode=None,
                    cycle_id=None,
                    cycle_started_at=None,
                    cycle_finished_at=None,
                    last_heartbeat=None,
                    is_stale=stale,
                    freshness_status=freshness,
                )
                if status == "unavailable":
                    del cycle["cycle_started_at"]
                evidence = sync_cycle_health_evidence_from_payload({"sync": cycle})
                assert evidence is not None and evidence.cycle is not None
                self.assertEqual(evidence.cycle.status, status)
                self.assertIsNone(evidence.cycle.cycle_finished_at)

    def test_malformed_evidence_is_closed_and_never_echoes_input(self) -> None:
        private = "private-source-error-and-business-sentinel"
        invalid: tuple[object, ...] = (
            None,
            [],
            {},
            sync_cycle_payload(cycle_id=private),
            sync_cycle_payload(status=private),
            sync_cycle_payload(mode=private),
            sync_cycle_payload(cycle_started_at=private),
            sync_cycle_payload(cycle_started_at="2026-05-29T12:00:00"),
            sync_cycle_payload(cycle_finished_at="2026-05-29T11:59:59Z"),
            sync_cycle_payload(last_heartbeat="2026-05-29T11:59:59Z"),
            sync_cycle_payload(cycle_finished_at=None),
            sync_cycle_payload(mode=None),
            sync_cycle_payload(is_stale="false"),
            sync_cycle_payload(next_cycle_overdue=0),
            sync_cycle_payload(stale_threshold_seconds=True),
            sync_cycle_payload(stale_threshold_seconds=-1),
            sync_cycle_payload(freshness_status="stale"),
            sync_cycle_payload(status="running"),
        )
        for cycle in invalid:
            with self.subTest(cycle=cycle):
                evidence = sync_cycle_health_evidence_from_payload({"sync": cycle})
                assert evidence is not None
                self.assertEqual(evidence.status, "malformed")
                self.assertIsNone(evidence.cycle)
                self.assertNotIn(private, evidence.model_dump_json())

    def test_uuid_and_aware_timestamp_representations(self) -> None:
        evidence = sync_cycle_health_evidence_from_payload(
            {
                "sync": sync_cycle_payload(
                    cycle_id="b12bc47c-1321-44cd-94b9-0e72385d10dc",
                    cycle_started_at="2026-05-29T08:00:00-04:00",
                )
            }
        )
        assert evidence is not None and evidence.cycle is not None
        self.assertEqual(evidence.status, "recorded")

    def test_monitor_records_health_only_and_retains_failed_observation(self) -> None:
        checked_at = "2026-05-29T12:05:00Z"
        for status_code in (200, 503):
            with self.subTest(status_code=status_code):
                store = _Store((_profile(),))
                run_public_ingress_monitor_once(
                    record_store=store,
                    checked_at=checked_at,
                    http_get=lambda url, _timeout: HttpObservation(
                        status_code=status_code,
                        final_url=url,
                        redirect_count=0,
                        payload={"sync": sync_cycle_payload()},
                    ),
                )
                record = PublicIngressObservationRecord.model_validate_json(
                    store.records[0].model_dump_json()
                )
                self.assertEqual(record.observed_at, checked_at)
                self.assertEqual(record.status, "pass" if status_code == 200 else "fail")
                self.assertIsNone(record.targets[0].sync)
                assert record.targets[-1].sync is not None
                self.assertEqual(record.targets[-1].sync.status, "recorded")

    def test_private_monitor_keeps_sync_with_its_private_check(self) -> None:
        lane = ProductLaneProfile(
            context="example-site",
            instance="prod",
            health_monitoring=ProductLaneHealthMonitoringPolicy(
                monitoring_intent="private",
                checks=(
                    ProductLaneHealthCheck(
                        name="private-runtime",
                        kind="private_http",
                        private_endpoint_key="example-site-prod-runtime",
                    ),
                ),
            ),
        )
        store = _Store((_profile(lane=lane),))
        _add_private_endpoint(store)
        result = run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:05:00Z",
            private_http_get=lambda url, _timeout: HttpObservation(
                status_code=200,
                final_url=url,
                redirect_count=0,
                payload={"sync": sync_cycle_payload()},
            ),
        )
        self.assertEqual(result.pass_count, 1)
        record = store.records[0]
        self.assertEqual(record.check_name, "private-runtime")
        self.assertEqual(record.check_kind, "private_http")
        self.assertEqual(record.targets[0].target, "private_health_url")
        self.assertIsNotNone(record.targets[0].sync)

    def test_runtime_identity_failure_is_not_cleared_by_recorded_sync(self) -> None:
        lane = ProductLaneProfile(
            context="example-site",
            instance="prod",
            base_url="https://example.test",
            health_monitoring=ProductLaneHealthMonitoringPolicy(
                monitoring_intent="public",
                checks=(
                    ProductLaneHealthCheck(name="public-ingress", require_runtime_identity=True),
                ),
            ),
        )
        store = _Store((_profile(lane=lane),))
        run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:05:00Z",
            http_get=lambda url, _timeout: HttpObservation(
                status_code=200,
                final_url=url,
                redirect_count=0,
                payload={"sync": sync_cycle_payload()},
            ),
        )
        record = store.records[0]
        self.assertEqual(record.status, "fail")
        self.assertEqual(record.failure_code, "wrong_runtime_identity")
        self.assertIsNotNone(record.targets[-1].sync)

    def test_monitor_cycle_survives_real_storage_restart(self) -> None:
        checked_at = "2026-05-29T12:05:00Z"
        with TemporaryDirectory() as directory:
            url = _sqlite_database_url(Path(directory) / "records.sqlite3")
            store = PostgresRecordStore(database_url=url)
            try:
                store.ensure_schema()
                store.write_product_profile_record(_profile())
                run_public_ingress_monitor_once(
                    record_store=store,
                    checked_at=checked_at,
                    http_get=lambda endpoint, _timeout: HttpObservation(
                        status_code=503,
                        final_url=endpoint,
                        redirect_count=0,
                        payload={
                            "sync": sync_cycle_payload(
                                status="failed",
                                freshness_status="failed",
                                last_error="private-error-sentinel",
                            )
                        },
                    ),
                )
            finally:
                store.close()
            store = PostgresRecordStore(database_url=url)
            try:
                record = store.list_public_ingress_observation_records()[0]
                self.assertEqual(record.status, "fail")
                assert (
                    record.targets[-1].sync is not None
                    and record.targets[-1].sync.cycle is not None
                )
                self.assertEqual(record.targets[-1].sync.cycle.status, "failed")
                self.assertNotIn("private-error-sentinel", record.model_dump_json())
                self.assertEqual(record.observed_at, checked_at)
            finally:
                store.close()

    def test_monitor_observes_sync_when_base_url_is_the_health_endpoint(self) -> None:
        profile = _profile()
        profile = profile.model_copy(
            update={
                "health_path": "/",
                "lanes": (
                    profile.lanes[0].model_copy(update={"health_url": "https://example.test"}),
                ),
            }
        )
        store = _Store((profile,))
        requested: list[str] = []

        def fetch(url: str, _timeout: int) -> HttpObservation:
            requested.append(url)
            return HttpObservation(
                status_code=200,
                final_url=url,
                redirect_count=0,
                payload={"sync": sync_cycle_payload()},
            )

        run_public_ingress_monitor_once(
            record_store=store, checked_at="2026-05-29T12:05:00Z", http_get=fetch
        )
        record = store.records[0]
        self.assertEqual(requested, [record.base_url])
        self.assertEqual(record.targets[0].target, "base_url")
        self.assertIsNotNone(record.targets[0].sync)

    def test_readback_uses_same_latest_probe_and_does_not_reuse_old_sync(self) -> None:
        profile_payload = _site_profile_payload(preview_enabled=False)
        lanes = cast(tuple[dict[str, object], ...], profile_payload["lanes"])
        lanes[1]["health_monitoring"] = {
            "monitoring_intent": "public",
            "checks": [{"name": "public-ingress", "kind": "public_http"}],
        }
        profile = LaunchplaneProductProfileRecord.model_validate(profile_payload)
        store = _Store((_profile(),))
        run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:05:00Z",
            http_get=lambda url, _timeout: HttpObservation(
                status_code=200,
                final_url=url,
                redirect_count=0,
                payload={"sync": sync_cycle_payload()},
            ),
        )
        observed = store.records[0].model_copy(update={"context": "example-site-prod"})
        for include_sync in (True, False):
            with self.subTest(include_sync=include_sync):
                latest = observed.model_copy(
                    update={
                        "record_id": "latest-probe",
                        "observed_at": "2026-05-29T12:06:00Z",
                        "targets": observed.targets
                        if include_sync
                        else tuple(
                            target.model_copy(update={"sync": None}) for target in observed.targets
                        ),
                    }
                )
                reader = _PublicIngressReadModelStore(profile, (latest, observed))
                detail = build_product_environment_detail(
                    record_store=reader,
                    product="example-site",
                    environment="prod",
                    action_allowed=lambda *_: False,
                )
                check = detail.health_monitoring.checks[0]
                self.assertEqual(check.record_id, "latest-probe")
                self.assertEqual(check.observed_at, latest.observed_at)
                self.assertEqual(check.sync, observed.targets[-1].sync if include_sync else None)


if __name__ == "__main__":
    unittest.main()
