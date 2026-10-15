from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import delete, select

from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    DeploymentEvidence,
    HealthcheckEvidence,
    RecordFailure,
)
from control_plane.production_delivery_monitor import (
    DELIVERY_EVIDENCE_PAGE_SIZE,
    _candidate_key,
    _write_receipt,
    monitor_production_delivery_once,
    record_production_delivery_failure,
)
from control_plane.storage.delivery_evidence import sources
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.test_public_ingress_monitor import _profile


class DeliveryEvidenceIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profile = _profile().model_copy(update={"production_use": "prelaunch"})
        self.lane = self.profile.lanes[0]
        self.now = datetime.now(timezone.utc)
        self.store: PostgresRecordStore | FilesystemRecordStore

    def setup_store(self, backend: str) -> None:
        if backend == "filesystem":
            self.store = FilesystemRecordStore(self.root / backend)
        else:
            self.store = PostgresRecordStore(
                database_url="sqlite+pysqlite:///" + str(self.root / (backend + ".db"))
            )
            self.addCleanup(self.store.close)
            self.store.ensure_schema()
        self.store.write_product_profile_record(self.profile)

    def deploy(
        self,
        identity: str,
        *,
        failed: bool = False,
        minutes_ago: int = 0,
        code: str = "deployment_failed",
    ) -> DeploymentRecord:
        record = DeploymentRecord(
            record_id=identity,
            context=self.lane.context,
            instance="prod",
            source_git_ref=identity,
            artifact_identity=ArtifactIdentityReference(artifact_id="image@" + identity),
            deploy=DeploymentEvidence(
                deploy_mode="test",
                target_name="test",
                target_type="application",
                status="fail" if failed else "pass",
                finished_at=(self.now - timedelta(minutes=minutes_ago)).isoformat(),
            ),
            destination_health=HealthcheckEvidence(status="pass"),
            failure=RecordFailure(code=code, description="A delivery failed") if failed else None,
        )
        self.store.write_deployment_record(record)
        return record

    def bounded_list(self, original: object) -> object:
        def read(*args: object, **kwargs: object) -> object:
            limit = kwargs.get("limit")
            if not isinstance(limit, int) or limit > DELIVERY_EVIDENCE_PAGE_SIZE:
                raise AssertionError("Unbounded historical read")
            return original(*args, **kwargs)  # type: ignore[operator]

        return read

    def monitor(self) -> None:
        monitor_production_delivery_once(
            self.store,
            observed_at=self.now.isoformat(),
            transport_factory=Mock(side_effect=AssertionError("Prelaunch has no source deadline")),
        )

    def incidents(self) -> tuple[object, ...]:
        return self.store.list_public_ingress_incident_records(status="open")

    def test_large_history_is_paged_then_idle_and_late_updates_read_only_changed_sources(
        self,
    ) -> None:
        for backend in ("filesystem", "sqlite"):
            with self.subTest(backend=backend):
                self.setup_store(backend)
                count = DELIVERY_EVIDENCE_PAGE_SIZE * 2 + 3
                for number in range(count):
                    self.deploy(f"history-{number:05}", minutes_ago=count - number)
                with (
                    patch.object(
                        self.store,
                        "list_deployment_records",
                        side_effect=self.bounded_list(self.store.list_deployment_records),
                    ),
                    patch.object(
                        self.store,
                        "list_promotion_records",
                        side_effect=self.bounded_list(self.store.list_promotion_records),
                    ),
                    patch.object(
                        self.store,
                        "list_release_review_decision_records",
                        side_effect=self.bounded_list(
                            self.store.list_release_review_decision_records
                        ),
                    ),
                    patch.object(
                        self.store,
                        "read_deployment_record",
                        wraps=self.store.read_deployment_record,
                    ) as read,
                ):
                    for _ in range(3):
                        read.reset_mock()
                        self.monitor()
                        self.assertLessEqual(read.call_count, DELIVERY_EVIDENCE_PAGE_SIZE + 1)
                    read.reset_mock()
                    self.monitor()
                    self.assertLessEqual(read.call_count, 1, "Idle source reads must stay bounded")
                    self.assertFalse(self.incidents())
                    self.deploy("late", failed=True, minutes_ago=count + 10)
                    read.reset_mock()
                    self.monitor()
                    self.assertLessEqual(read.call_count, 2)
                    self.assertEqual(len(self.incidents()), 1)
                    self.deploy("late", failed=True, minutes_ago=count + 10, code="provider_failed")
                    self.monitor()
                    self.assertIn(
                        "provider_failed",
                        self.store.list_public_ingress_incident_records(status="open")[0].summary,
                    )
                with (
                    self.store.delivery_evidence_index() as index,
                    index.engine.connect() as connection,
                ):
                    rows = connection.execute(select(sources.c.processed)).scalars().all()
                    self.assertEqual(len(rows), count + 1)
                    self.assertEqual(sum(len(row) for row in rows), 1)
                    self.assertFalse(
                        index.pending(self.lane.context, self.profile.product, limit=count)
                    )

    def test_new_failure_is_prioritized_over_remaining_bootstrap_history(self) -> None:
        for backend in ("filesystem", "sqlite"):
            with self.subTest(backend=backend):
                self.setup_store(backend)
                for number in range(DELIVERY_EVIDENCE_PAGE_SIZE * 3):
                    self.deploy(f"history-{number:05}", minutes_ago=number + 1)
                self.monitor()
                self.deploy("new-failure", failed=True, minutes_ago=1000)
                self.monitor()
                self.assertTrue(
                    any(
                        "new-failure" in incident.summary
                        for incident in self.store.list_public_ingress_incident_records(
                            status="open"
                        )
                    )
                )
                with self.store.delivery_evidence_index() as index:
                    self.assertTrue(
                        index.pending(self.lane.context, self.profile.product, limit=1),
                        "Old history remains queued while new evidence alerts",
                    )

    def test_corrupt_history_does_not_block_new_filesystem_source_writes(self) -> None:
        self.setup_store("filesystem")
        path = self.root / "filesystem" / "deployments" / "corrupt.json"
        path.parent.mkdir(parents=True)
        path.write_text("invalid json")
        self.deploy("new-source", failed=True)
        self.assertEqual(self.store.read_deployment_record("new-source").deploy.status, "fail")
        # The monitor remains explicitly unknown until the damaged evidence is repaired.
        with self.assertLogs("control_plane.production_delivery_monitor", level="ERROR"):
            self.monitor()
        self.assertTrue(self.incidents())
        path.unlink()
        self.monitor()
        self.assertTrue(
            any(
                "new-source" in incident.summary
                for incident in self.store.list_public_ingress_incident_records(status="open")
            )
        )

    def test_acknowledging_an_old_version_cannot_hide_a_concurrent_source_update(self) -> None:
        for backend in ("filesystem", "sqlite"):
            with self.subTest(backend=backend):
                self.setup_store(backend)
                self.deploy("candidate")
                self.monitor()
                self.deploy("candidate", failed=True)
                with self.store.delivery_evidence_index() as index:
                    (old,) = index.pending(self.lane.context, self.profile.product, limit=1)
                    self.deploy("candidate", failed=True, code="later_stage_failed")
                    index.acknowledge(old, {"candidate": "old-signature"})
                    (new,) = index.pending(self.lane.context, self.profile.product, limit=1)
                    self.assertNotEqual(old.version, new.version)
                self.monitor()
                self.assertIn(
                    "later_stage_failed",
                    self.store.list_public_ingress_incident_records(status="open")[0].summary,
                )

    def test_failed_persistence_retries_after_restart_and_does_not_replay_after_recovery(
        self,
    ) -> None:
        for backend in ("filesystem", "sqlite"):
            with self.subTest(backend=backend):
                self.setup_store(backend)
                self.deploy("current", minutes_ago=20)
                self.monitor()
                self.deploy("candidate", failed=True)
                with (
                    patch(
                        "control_plane.production_delivery_monitor.record_production_delivery_failure",
                        side_effect=[RuntimeError("persistence unavailable"), None],
                    ),
                    self.assertLogs("control_plane.production_delivery_monitor", level="ERROR"),
                ):
                    self.monitor()
                with self.store.delivery_evidence_index() as index:
                    self.assertTrue(index.pending(self.lane.context, self.profile.product, limit=1))
                if backend == "filesystem":
                    self.store = FilesystemRecordStore(self.root / backend)
                else:
                    self.store = PostgresRecordStore(database_url=self.store.database_url)  # type: ignore[union-attr]
                    self.addCleanup(self.store.close)
                self.monitor()
                failures = self.store.list_public_ingress_incident_records(status="open")
                self.assertEqual(len(failures), 1)
                self.now += timedelta(seconds=1)
                self.deploy("candidate")
                self.now += timedelta(seconds=1)
                self.deploy("newer")
                self.monitor()
                self.assertFalse(self.incidents())
                self.monitor()
                self.assertFalse(self.incidents())

    def test_replica_overlapping_a_pass_does_not_duplicate_an_alert(self) -> None:
        for backend in ("filesystem", "sqlite"):
            with self.subTest(backend=backend):
                self.setup_store(backend)
                self.monitor()
                self.deploy("candidate", failed=True)
                entered, release = Event(), Event()

                def hold_failure(*args: object, **kwargs: object) -> None:
                    entered.set()
                    self.assertTrue(release.wait(10))
                    record_production_delivery_failure(*args, **kwargs)  # type: ignore[arg-type]

                with patch(
                    "control_plane.production_delivery_monitor.record_production_delivery_failure",
                    side_effect=hold_failure,
                ):
                    with ThreadPoolExecutor(max_workers=2) as executor:
                        first = executor.submit(self.monitor)
                        self.assertTrue(entered.wait(10))
                        try:
                            second = executor.submit(self.monitor)
                            second.result(timeout=10)
                        finally:
                            release.set()
                        first.result(timeout=10)
                self.assertEqual(len(self.incidents()), 1)

    def test_interrupted_file_publication_cannot_create_a_failure_or_fake_recovery(self) -> None:
        self.setup_store("filesystem")
        self.deploy("current")
        self.monitor()
        with patch(
            "control_plane.storage.filesystem.os.replace", side_effect=OSError("interrupted")
        ):
            with self.assertRaises(OSError):
                self.deploy("missing", failed=True)
        self.monitor()
        self.assertFalse(self.incidents())
        self.deploy("candidate", failed=True)
        self.monitor()
        self.assertEqual(len(self.incidents()), 1)
        self.now += timedelta(seconds=1)
        self.deploy("unrelated")
        with patch(
            "control_plane.storage.filesystem.os.replace", side_effect=OSError("interrupted")
        ):
            with self.assertRaises(OSError):
                self.deploy("candidate")
        # The interrupted success is behind another pending record in this page.
        with patch("control_plane.production_delivery_monitor.DELIVERY_EVIDENCE_PAGE_SIZE", 1):
            self.monitor()
        self.assertEqual(len(self.incidents()), 1)
        self.monitor()
        self.assertEqual(len(self.incidents()), 1)

    def test_filesystem_read_waits_for_a_paused_publication(self) -> None:
        import os

        self.setup_store("filesystem")
        record = self.deploy("candidate")
        paused, release, attempted, published = Event(), Event(), Event(), Event()
        original_replace = os.replace
        original_read = self.store._read_model

        def replace(source: object, destination: object) -> None:
            if str(destination).endswith("deployments/candidate.json"):
                paused.set()
                self.assertTrue(release.wait(10))
                original_replace(source, destination)  # type: ignore[arg-type]
                published.set()
            else:
                original_replace(source, destination)  # type: ignore[arg-type]

        def read(*args: object, **kwargs: object) -> object:
            attempted.set()
            result: object = original_read(*args, **kwargs)  # type: ignore[arg-type]
            self.assertTrue(
                published.is_set(), "A source read must wait for publication under the bundle lock"
            )
            return result

        failed = record.model_copy(
            update={"deploy": record.deploy.model_copy(update={"status": "fail"})}
        )
        with (
            patch("control_plane.storage.filesystem.os.replace", side_effect=replace),
            patch.object(self.store, "_read_model", side_effect=read),
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                writer = executor.submit(self.store.write_deployment_record, failed)
                self.assertTrue(paused.wait(10))
                reader = executor.submit(self.store.read_deployment_record, record.record_id)
                try:
                    self.assertTrue(attempted.wait(10))
                finally:
                    release.set()
                writer.result(timeout=10)
                self.assertEqual(reader.result(timeout=10).deploy.status, "fail")
        self.monitor()
        self.assertEqual(len(self.incidents()), 1)

    def test_bulk_source_retention_removes_processed_index_state(self) -> None:
        from control_plane.storage.postgres import LaunchplaneDeploymentRow

        self.setup_store("sqlite")
        self.deploy("retained", failed=True)
        self.monitor()
        with self.store.delivery_evidence_index() as index:
            with index.engine.begin() as connection:
                connection.execute(
                    delete(LaunchplaneDeploymentRow).where(
                        LaunchplaneDeploymentRow.record_id == "retained"
                    )
                )
            with index.engine.connect() as connection:
                self.assertFalse(connection.execute(select(sources.c.key)).all())

    def test_legacy_receipt_bootstrap_across_pages_preserves_deduplication(self) -> None:
        for backend in ("filesystem", "sqlite"):
            with self.subTest(backend=backend):
                self.setup_store(backend)
                seen = {}
                for number in range(DELIVERY_EVIDENCE_PAGE_SIZE + 3):
                    record = self.deploy(f"old-{number:05}", failed=True, minutes_ago=10)
                    assert record.failure is not None
                    seen[record.record_id] = _candidate_key(
                        record.failure.code + ":" + record.deploy.finished_at
                    )
                _write_receipt(
                    self.store,
                    key="failures:" + self.profile.product,
                    now=self.now.isoformat(),
                    payload={"seen": seen},
                )
                self.monitor()
                self.deploy("late", failed=True, minutes_ago=60)
                self.monitor()
                self.assertEqual(len(self.incidents()), 1)
                self.assertIn(
                    "late",
                    self.store.list_public_ingress_incident_records(status="open")[0].summary,
                )
                self.monitor()
                self.assertEqual(len(self.incidents()), 1)
