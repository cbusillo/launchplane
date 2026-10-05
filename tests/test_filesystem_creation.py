"""Generic insert-only filesystem creation survives interrupted publication."""

import json
import multiprocessing
import os
import unittest
from multiprocessing.synchronize import Event
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.secret_record import SecretAuditEvent
from control_plane.storage.filesystem import FilesystemRecordStore


def _audit_event() -> SecretAuditEvent:
    return SecretAuditEvent(
        event_id="fixture-retirement-audit",
        secret_id="fixture-secret",
        event_type="disabled",
        recorded_at="2026-10-05T10:00:00Z",
        actor="fixture-actor",
        detail="Retirement evidence.",
    )


def _paused_creation(root: str, boundary: str, reached: Event, release: Event) -> None:
    def pause() -> None:
        reached.set()
        if not release.wait(timeout=60):
            raise TimeoutError("Parent did not terminate or release the writer.")

    original_dumps = json.dumps
    original_replace = os.replace

    def serialize(value: object, *, indent: int, sort_keys: bool) -> str:
        pause()
        return original_dumps(value, indent=indent, sort_keys=sort_keys)

    def publish(source: str, destination: Path) -> None:
        if boundary == "before_publication":
            pause()
        original_replace(source, destination)
        if boundary == "after_publication":
            pause()

    target = "control_plane.storage.filesystem."
    with patch(
        target + ("json.dumps" if boundary == "serialization" else "os.replace"),
        side_effect=serialize if boundary == "serialization" else publish,
    ):
        FilesystemRecordStore(Path(root)).create_product_retirement_secret_audit_event(
            _audit_event()
        )


class FilesystemCreationTests(unittest.TestCase):
    def test_long_valid_record_name_can_be_created_and_remains_insert_only(self) -> None:
        event = _audit_event().model_copy(update={"event_id": "audit-" + "x" * 239})
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(Path(directory))
            path = store.create_product_retirement_secret_audit_event(event)
            self.assertEqual(
                SecretAuditEvent.model_validate_json(path.read_text(encoding="utf-8")), event
            )
            with self.assertRaisesRegex(ValueError, "append-only"):
                store.create_product_retirement_secret_audit_event(
                    event.model_copy(update={"actor": "contender"})
                )
            self.assertEqual(store.list_secret_audit_events(secret_id=event.secret_id), (event,))

    def test_failed_serialization_or_sync_leaves_absence_and_allows_retry(self) -> None:
        event = _audit_event()
        for failing_operation in ("json.dumps", "os.fsync", "os.replace"):
            with self.subTest(operation=failing_operation), TemporaryDirectory() as directory:
                store = FilesystemRecordStore(Path(directory))
                with (
                    patch(
                        "control_plane.storage.filesystem." + failing_operation,
                        side_effect=OSError("injected write failure"),
                    ),
                    self.assertRaisesRegex(OSError, "injected write failure"),
                ):
                    store.create_product_retirement_secret_audit_event(event)

                self.assertEqual(store.list_secret_audit_events(secret_id=event.secret_id), ())
                path = store.create_product_retirement_secret_audit_event(event)
                self.assertEqual(
                    SecretAuditEvent.model_validate_json(path.read_text(encoding="utf-8")), event
                )
                self.assertEqual(
                    store.list_secret_audit_events(secret_id=event.secret_id), (event,)
                )

    def test_provider_target_creation_retries_after_write_failure(self) -> None:
        target = ProviderTargetRecord(
            context="fixture-context",
            instance="testing",
            provider_id="fixture-provider",
            target_id="fixture-target",
            display_name="Fixture target",
            updated_at="2026-10-05T10:00:00Z",
        )
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(Path(directory))
            with (
                patch(
                    "control_plane.storage.filesystem.json.dumps",
                    side_effect=OSError("injected write failure"),
                ),
                self.assertRaises(OSError),
            ):
                store.create_provider_target_record_if_absent(target)
            self.assertEqual(store.list_provider_target_records(), ())
            self.assertEqual(store.create_provider_target_record_if_absent(target), "created")
            self.assertEqual(
                store.create_provider_target_record_if_absent(
                    target.model_copy(update={"target_id": "contender"})
                ),
                "exists",
            )
            self.assertEqual(store.list_provider_target_records(), (target,))

    def test_killed_writer_leaves_absent_or_complete_record_and_retryable_lock(self) -> None:
        context = multiprocessing.get_context("spawn")
        event = _audit_event()
        for boundary in ("serialization", "before_publication", "after_publication"):
            with self.subTest(boundary=boundary), TemporaryDirectory() as directory:
                reached = context.Event()
                release = context.Event()
                writer = context.Process(
                    target=_paused_creation, args=(directory, boundary, reached, release)
                )
                writer.start()
                try:
                    self.assertTrue(reached.wait(timeout=20), "Writer did not reach the boundary.")
                    writer.kill()
                    writer.join(timeout=10)
                    self.assertFalse(writer.is_alive(), "Killed writer did not exit.")
                    self.assertNotEqual(writer.exitcode, 0)
                finally:
                    if writer.is_alive():
                        writer.kill()
                        writer.join(timeout=10)
                    writer.close()

                store = FilesystemRecordStore(Path(directory))
                expected = (event,) if boundary == "after_publication" else ()
                # The subprocess leaves its temporary file behind when killed before publication.
                self.assertEqual(
                    store.list_secret_audit_events(secret_id=event.secret_id), expected
                )
                if expected:
                    with self.assertRaisesRegex(ValueError, "append-only"):
                        store.create_product_retirement_secret_audit_event(event)
                else:
                    store.create_product_retirement_secret_audit_event(event)
                self.assertEqual(
                    store.list_secret_audit_events(secret_id=event.secret_id), (event,)
                )
                with self.assertRaisesRegex(ValueError, "append-only"):
                    store.create_product_retirement_secret_audit_event(
                        event.model_copy(update={"actor": "contender"})
                    )
                self.assertEqual(
                    store.list_secret_audit_events(secret_id=event.secret_id), (event,)
                )

    def test_existing_malformed_record_or_dangling_symlink_is_not_replaced(self) -> None:
        event = _audit_event()
        for existing_kind in ("malformed", "dangling_symlink"):
            with self.subTest(kind=existing_kind), TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / "launchplane_secret_audit_events" / f"{event.event_id}.json"
                path.parent.mkdir()
                if existing_kind == "malformed":
                    path.write_text("{partial", encoding="utf-8")
                else:
                    path.symlink_to(root / "missing.json")
                with self.assertRaisesRegex(ValueError, "append-only"):
                    FilesystemRecordStore(root).create_product_retirement_secret_audit_event(event)
                if existing_kind == "malformed":
                    self.assertEqual(path.read_text(encoding="utf-8"), "{partial")
                else:
                    self.assertTrue(path.is_symlink())
                    self.assertEqual(path.readlink(), root / "missing.json")
