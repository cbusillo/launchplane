"""Old Owner events remain readable without restoring their retired writer."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sqlalchemy import insert

from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import LaunchplaneOwnerAcceptanceEventRow, PostgresRecordStore
from tests.test_postgres_integration import _owner_acceptance_event


class OwnerAcceptanceHistoryTests(unittest.TestCase):
    def test_stored_history_survives_and_import_refuses_before_writing(self) -> None:
        event = _owner_acceptance_event().model_copy(update={"subject_sequence": 7})
        serialized = event.model_dump_json()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            history = root / "state" / "launchplane_owner_acceptance_events"
            history.mkdir(parents=True)
            (history / f"{event.event_id}.json").write_text(serialized)
            filesystem = FilesystemRecordStore(state_dir=root / "state")
            self.assertEqual(filesystem.read_owner_acceptance_event_record(event.event_id), event)
            self.assertEqual(filesystem.list_owner_acceptance_event_records(), (event,))
            store = PostgresRecordStore(database_url=f"sqlite:///{root / 'history.sqlite3'}")
            store.ensure_schema()
            try:
                with self.assertRaisesRegex(ValueError, "Retired Owner acceptance history"):
                    store.import_core_records_from_filesystem(filesystem)
                self.assertEqual(store.list_owner_acceptance_event_records(), ())
                # Seed an existing historical row, as a deployment would find it.
                binding = event.binding
                with store._engine.begin() as connection:
                    connection.execute(
                        insert(LaunchplaneOwnerAcceptanceEventRow).values(
                            event_id=event.event_id,
                            acceptance_id=event.acceptance_id,
                            subject_sequence=event.subject_sequence,
                            binding_sha256=binding.binding_sha256,
                            repository_id=binding.repository_id,
                            repository_owner_id=binding.repository_owner_id,
                            repository=binding.repository,
                            pr_number=binding.pull_request_number,
                            head_sha=binding.head_sha,
                            tree_sha=binding.tree_sha,
                            product=binding.product,
                            system=binding.system,
                            owner_action=binding.action,
                            environment=binding.environment,
                            action=event.action,
                            occurred_at=event.occurred_at,
                            payload=event.model_dump(mode="json"),
                        )
                    )
                self.assertEqual(store.read_owner_acceptance_event_record(event.event_id), event)
                self.assertEqual(store.list_owner_acceptance_event_records(), (event,))
                self.assertEqual((history / f"{event.event_id}.json").read_text(), serialized)
            finally:
                store.close()
