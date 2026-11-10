import unittest

from control_plane.contracts.odoo_testing_drill import OdooTestingDrillStorageBudget
from control_plane.contracts.odoo_testing_source import MigratedOdooTestingSource
from control_plane.testing_storage_admission import (
    TESTING_STORAGE_CAPACITY_INSUFFICIENT,
    BackingMeasurement,
    TestingStorageCapacityRefused,
    require_migrated_restore_capacity,
)
from tests import test_migrated_testing_source as migrated_tests

LANE_BUDGET = OdooTestingDrillStorageBudget(
    database_bytes=10, filestore_bytes=100, staging_bytes=1, reserve_bytes=5
)


def migrated_pair() -> MigratedOdooTestingSource:
    source = migrated_tests.MigratedTestingRequestTests().request().migrated_source
    assert source is not None
    return source.model_copy(
        update={
            "database": source.database.model_copy(update={"size": 20}),
            "filestore": source.filestore.model_copy(update={"size": 50}),
            "filestore_expanded_bytes": 120,
        }
    )


def backings(
    volumes: int, writable: int, snapshot: int, *, shared: bool = True
) -> dict[str, BackingMeasurement]:
    return {
        "volumes": BackingMeasurement("zfs:testing", volumes),
        "writable": BackingMeasurement("zfs:testing" if shared else "device:2", writable),
        "snapshot": BackingMeasurement("device:3", snapshot),
    }


class MigratedRestoreAdmissionTests(unittest.TestCase):
    def shortfall(
        self, roles: dict[str, BackingMeasurement], *, clean_snapshot: bool = False
    ) -> str | None:
        try:
            require_migrated_restore_capacity(
                roles, LANE_BUDGET, migrated_pair(), clean_snapshot=clean_snapshot
            )
        except TestingStorageCapacityRefused as error:
            self.assertEqual(error.code, TESTING_STORAGE_CAPACITY_INSUFFICIENT)
            return str(error)
        return None

    def test_shared_pool_is_charged_database_filestore_staged_pair_runner_and_reserve(
        self,
    ) -> None:
        self.assertIsNone(self.shortfall(backings(235, 235, 0)))
        self.assertIn("1 bytes short", self.shortfall(backings(234, 234, 0)) or "")

    def test_each_separate_backing_keeps_its_own_reserve(self) -> None:
        self.assertIsNone(self.shortfall(backings(215, 25, 0, shared=False)))
        self.assertIn("1 bytes short", self.shortfall(backings(215, 24, 0, shared=False)) or "")

    def test_clean_snapshot_charges_its_own_backing(self) -> None:
        self.assertIsNone(self.shortfall(backings(235, 235, 145), clean_snapshot=True))
        self.assertIn(
            "1 bytes short", self.shortfall(backings(235, 235, 144), clean_snapshot=True) or ""
        )


if __name__ == "__main__":
    unittest.main()
