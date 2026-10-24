from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from control_plane.contracts.odoo_testing_drill import OdooTestingDrillStorageBudget
from control_plane.clean_testing_snapshot import (
    CleanTestingSnapshot,
    preview_snapshot,
)
from tests.support.profiles import _odoo_profile_payload_with_prod_lane
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.odoo_preview_apply_http import (
    ODOO_PREVIEW_APPLY_INPUTS_ROUTE,
    ODOO_PREVIEW_APPLY_ROUTE,
)
from tests.test_odoo_preview_apply_execution import _issued_plan, _refresh_payload
from control_plane.odoo_preview_apply_http import OdooPreviewApplyEnvelope
from tests.support.stores import _sqlite_database_url


class CleanTestingSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = LaunchplaneProductProfileRecord.model_validate(
            _odoo_profile_payload_with_prod_lane("example-odoo")
        )
        self.profile.preview.clean_testing_snapshot = True
        self.context = self.profile.preview.context
        self.now = datetime.now(timezone.utc).isoformat()
        self.snapshot = CleanTestingSnapshot(
            storage_budget=OdooTestingDrillStorageBudget(
                database_bytes=1024, filestore_bytes=1024, staging_bytes=1024, reserve_bytes=1024
            ),
            product=self.profile.product,
            context=self.context,
            snapshot_id="a" * 64,
            server_id="fixture-server",
            database_sha256="b" * 64,
            filestore_sha256="c" * 64,
            captured_at=self.now,
        )

    def test_preview_reads_only_the_completed_snapshot_without_a_live_source_fallback(self) -> None:
        store = Mock(spec=["read_clean_testing_snapshot"])
        store.read_clean_testing_snapshot.return_value = self.snapshot
        self.assertEqual(preview_snapshot(store, self.profile), self.snapshot)
        store.read_clean_testing_snapshot.assert_called_once_with(
            self.profile.product, self.context
        )
        store.read_clean_testing_snapshot.return_value = None
        with self.assertRaises(ValueError):
            preview_snapshot(store, self.profile)

    def test_foreign_snapshot_is_refused_before_any_preview_copy(self) -> None:
        store = Mock(spec=["read_clean_testing_snapshot"])
        store.read_clean_testing_snapshot.return_value = self.snapshot.model_copy(
            update={"product": "another-product"}
        )
        with self.assertRaises(ValueError):
            preview_snapshot(store, self.profile)

    def test_retirement_keeps_latest_two_and_every_unexpired_preview_plan(self) -> None:
        with TemporaryDirectory() as temporary:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary) / "store.sqlite3")
            )
            store.ensure_schema()
            now = datetime.now(timezone.utc)
            snapshots = []
            for index, letter in enumerate("abcd"):
                snapshot = self.snapshot.model_copy(
                    update={
                        "snapshot_id": letter * 64,
                        "captured_at": (now + timedelta(seconds=index)).isoformat(),
                    }
                )
                store.write_clean_testing_snapshot(snapshot)
                snapshots.append(snapshot)

            def candidates(moment: datetime) -> tuple[CleanTestingSnapshot, ...]:
                return store.clean_snapshot_retirement_candidates(
                    self.profile.product, self.context, self.snapshot.server_id, now=moment
                )

            self.assertCountEqual(candidates(now), snapshots[:2])
            envelope = OdooPreviewApplyEnvelope.model_validate(_refresh_payload())
            envelope.product = self.profile.product
            envelope.apply.dry_run_plan.product = self.profile.product
            envelope.apply.dry_run_plan.repository = self.profile.repository
            envelope.apply.dry_run_plan.clean_snapshot = snapshots[0]
            issued = _issued_plan(envelope)
            store.write_idempotency_record(
                LaunchplaneIdempotencyRecord(
                    record_id="issued-snapshot-plan",
                    scope="fixture",
                    route_path=ODOO_PREVIEW_APPLY_INPUTS_ROUTE,
                    idempotency_key="issued-snapshot-plan",
                    request_fingerprint="f" * 64,
                    response_status_code=202,
                    response_trace_id="fixture-plan",
                    recorded_at=now.isoformat(),
                    response_payload={"result": issued.model_dump(mode="json")},
                )
            )
            self.assertEqual(candidates(now), (snapshots[1],))
            assert issued.plan_provenance is not None
            self.assertCountEqual(
                candidates(issued.plan_provenance.expires_at + timedelta(seconds=1)),
                tuple(snapshots[:2]),
            )
            self.assertEqual(
                store.read_clean_testing_snapshot(self.profile.product, self.context), snapshots[-1]
            )
            active = LaunchplaneIdempotencyRecord(
                record_id="pending-snapshot-copy",
                scope="fixture",
                route_path=ODOO_PREVIEW_APPLY_ROUTE,
                idempotency_key="pending-snapshot-copy",
                request_fingerprint="e" * 64,
                state="reconcile_required",
                lease_owner="fixture",
                reconciliation_key="fixture",
                created_at=now.isoformat(),
                updated_at=now.isoformat(),
            )
            with store._session_factory() as session:
                session.add(store._idempotency_row(active))
                store._retain_original_mutation_intent(
                    session,
                    active,
                    {
                        "apply": {
                            "dry_run_plan": {"clean_snapshot": snapshots[0].model_dump(mode="json")}
                        }
                    },
                )
                session.commit()
            self.assertEqual(
                candidates(issued.plan_provenance.expires_at + timedelta(seconds=1)),
                (snapshots[1],),
            )


if __name__ == "__main__":
    unittest.main()
