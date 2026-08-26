from __future__ import annotations

from tempfile import TemporaryDirectory
import unittest
from pathlib import Path

from control_plane.contracts.repository_inventory import RepositoryInventoryRecord
from control_plane.repository_inventory import (
    RepositoryInventoryConflictError,
    RepositoryInventorySequenceError,
    apply_repository_inventory,
    get_repository_inventory_read_model,
)
from control_plane.storage.filesystem import FilesystemRecordStore


def _record(
    *,
    revision: int = 1,
    state: str = "tracked",
    supersedes_record_id: str | None = None,
) -> RepositoryInventoryRecord:
    return RepositoryInventoryRecord.model_validate(
        {
            "repository_id": "1001",
            "repository_owner_id": "100",
            "repository": "Example-Owner/Example-Repository",
            "inventory_state": state,
            "inventory_revision": revision,
            "recorded_at": "2026-08-26T00:00:00Z",
            "source": "test",
            "reason": "synthetic inventory evidence",
            "supersedes_record_id": supersedes_record_id,
        }
    )


class RepositoryInventoryTests(unittest.TestCase):
    def test_record_normalizes_and_hashes_deterministically(self) -> None:
        record = _record()
        self.assertEqual(record.repository, "example-owner/example-repository")
        self.assertEqual(record.record_id, "repository-inventory-1001-r1")
        self.assertEqual(len(record.inventory_digest), 64)

    def test_record_requires_utc_timestamp(self) -> None:
        with self.assertRaises(ValueError):
            _record().model_validate(
                {**_record().model_dump(), "recorded_at": "2026-08-26T01:00:00+01:00"}
            )

    def test_append_replay_conflict_and_retirement_read_model(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(Path(temporary_directory_name))
            first = _record()
            applied = apply_repository_inventory(store=store, record=first)
            replay = apply_repository_inventory(store=store, record=first)
            self.assertEqual(applied.status, "applied")
            self.assertEqual(replay.status, "replayed")
            retired = _record(revision=2, state="retired", supersedes_record_id=first.record_id)
            self.assertEqual(
                apply_repository_inventory(
                    store=store,
                    record=retired,
                    expected_current_record_id=first.record_id,
                ).status,
                "applied",
            )
            read_model = get_repository_inventory_read_model(repository_id="1001", store=store)
            self.assertEqual(read_model.current_record, retired)
            with self.assertRaises(RepositoryInventoryConflictError):
                apply_repository_inventory(store=store, record=_record(revision=2))

    def test_first_revision_requires_no_predecessor(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            with self.assertRaises(RepositoryInventorySequenceError):
                apply_repository_inventory(
                    store=FilesystemRecordStore(Path(temporary_directory_name)),
                    record=_record(revision=2),
                )
