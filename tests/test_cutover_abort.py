from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from typing import Any
from unittest.mock import patch

from control_plane.odoo_cutover_recovery import DefiniteBackupFailure
from control_plane.workflows.odoo_cutover import execute_odoo_cutover
from tests.test_odoo_cutover import IsolatedCutoverProvider, cutover_store


class CutoverAbortTests(unittest.TestCase):
    def test_definite_failure_restores_old_service_for_both_release_classes(self) -> None:
        for changing in (False, True):
            with self.subTest(changing=changing), TemporaryDirectory() as directory:
                root = Path(directory)
                provider = IsolatedCutoverProvider(root, database_changing=changing)
                self.addCleanup(provider.engine.dispose)
                store = cutover_store(root)
                with patch.object(provider, "backup", side_effect=DefiniteBackupFailure()):
                    result = execute_odoo_cutover(
                        provider.cutover, store, provider, record_id="original"
                    )
                self.assertEqual(result.step, "aborted")
                self.assertEqual(provider.target, provider.cutover.old)
                self.assertEqual(provider.maintenance_count, 0)
                self.assertIsNotNone(result.ended_at)
                if result.pause is not None:
                    self.assertEqual(result.pause.state, "complete")
                    self.assertEqual(result.pause.recovery_binding, provider.cutover.old.binding)
                with patch.object(provider, "backup") as backup:
                    replay = execute_odoo_cutover(
                        provider.cutover, store, provider, record_id="original"
                    )
                backup.assert_not_called()
                self.assertEqual(replay.step, "aborted")

    def test_unknown_failure_retains_original_operation_and_never_resumes_writers(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            provider = IsolatedCutoverProvider(root, database_changing=True)
            self.addCleanup(provider.engine.dispose)
            store = cutover_store(root)
            with (
                patch.object(provider, "backup", side_effect=TimeoutError()),
                self.assertRaises(TimeoutError),
            ):
                execute_odoo_cutover(provider.cutover, store, provider, record_id="original")
            held = store.load("original")
            self.assertEqual(held.step, "backing_up")
            self.assertIsNone(held.ended_at)
            assert held.pause is not None
            self.assertEqual(held.pause.state, "drained")
            self.assertEqual(provider.maintenance_count, 0)

    def test_ended_foreign_or_maintained_pause_never_aborts(self) -> None:
        from datetime import UTC, datetime
        from control_plane.odoo_cutover_recovery import backup_recovery_eligible

        with TemporaryDirectory() as directory:
            root = Path(directory)
            provider = IsolatedCutoverProvider(root, database_changing=True)
            self.addCleanup(provider.engine.dispose)
            store = cutover_store(root)
            with (
                patch.object(provider, "backup", side_effect=TimeoutError()),
                self.assertRaises(TimeoutError),
            ):
                execute_odoo_cutover(provider.cutover, store, provider, record_id="original")
            held = store.load("original")
            assert held.pause is not None
            self.assertTrue(backup_recovery_eligible(held))
            for updates in (
                {"website_settings_attempted": True},
                {"step": "maintaining"},
                {"pause": held.pause.model_copy(update={"ended_at": datetime.now(UTC)})},
                {"pause": held.pause.model_copy(update={"pause_id": "foreign"})},
                {"pause": held.pause.model_copy(update={"state": "fencing"})},
            ):
                with self.subTest(updates=updates):
                    self.assertFalse(backup_recovery_eligible(held.model_copy(update=updates)))

    def test_interrupted_abort_resumes_the_same_pause_without_recapturing(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            provider = IsolatedCutoverProvider(root, database_changing=True)
            self.addCleanup(provider.engine.dispose)
            store = cutover_store(root)
            with (
                patch.object(provider, "backup", side_effect=DefiniteBackupFailure()),
                patch.object(provider, "readiness", side_effect=TimeoutError()),
                self.assertRaises(TimeoutError),
            ):
                execute_odoo_cutover(provider.cutover, store, provider, record_id="original")
            held = store.load("original")
            self.assertEqual(held.step, "aborting")
            with patch.object(provider, "backup") as backup:
                resumed = execute_odoo_cutover(
                    provider.cutover, store, provider, record_id="original"
                )
            backup.assert_not_called()
            self.assertEqual(resumed.step, "aborted")
            assert resumed.pause is not None and held.pause is not None
            self.assertEqual(resumed.pause.pause_id, held.pause.pause_id)
            self.assertEqual(resumed.pause.began_at, held.pause.began_at)

    def test_abort_reconciles_a_lost_switch_reply_without_republishing_pause(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            provider = IsolatedCutoverProvider(root, database_changing=True)
            self.addCleanup(provider.engine.dispose)
            store = cutover_store(root)
            switch = provider.switch

            def lost_reply(plan: Any, slot: Any) -> Any:
                switch(plan, slot)
                raise TimeoutError("old route applied; reply lost")

            with (
                patch.object(provider, "backup", side_effect=DefiniteBackupFailure()),
                patch.object(provider, "switch", side_effect=lost_reply),
                self.assertRaises(TimeoutError),
            ):
                execute_odoo_cutover(provider.cutover, store, provider, record_id="original")
            held = store.load("original")
            assert held.pause is not None
            self.assertEqual(held.pause.state, "drained")
            with patch.object(provider, "backup") as backup:
                recovered = execute_odoo_cutover(
                    provider.cutover, store, provider, record_id="original"
                )
            backup.assert_not_called()
            self.assertEqual(recovered.step, "aborted")
            assert recovered.pause is not None
            self.assertEqual(recovered.pause.began_at, held.pause.began_at)

    def test_unpaused_abort_keeps_existing_serving_writers_running(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            provider = IsolatedCutoverProvider(root)
            self.addCleanup(provider.engine.dispose)
            store = cutover_store(root)
            with (
                patch.object(provider, "backup", side_effect=DefiniteBackupFailure()),
                patch.object(provider, "readiness") as readiness,
                patch.object(provider, "switch") as switch,
            ):
                result = execute_odoo_cutover(
                    provider.cutover, store, provider, record_id="original"
                )
            readiness.assert_not_called()
            switch.assert_not_called()
            self.assertEqual(result.step, "aborted")

    def test_compatible_settings_release_requires_its_original_pause_for_abort(self) -> None:
        from datetime import UTC, datetime
        from control_plane.contracts.odoo_cutover import OdooCutoverRecord
        from control_plane.odoo_cutover_recovery import backup_recovery_eligible
        from tests.test_odoo_cutover_website_settings import snapshot

        with TemporaryDirectory() as directory:
            provider = IsolatedCutoverProvider(Path(directory))
            self.addCleanup(provider.engine.dispose)
            plan = provider.cutover.model_copy(
                update={"website_settings": snapshot(provider.cutover)}
            )
            record = OdooCutoverRecord(
                record_id="original",
                plan=plan,
                step="backing_up",
                began_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            self.assertFalse(backup_recovery_eligible(record))

    def test_resumed_abort_never_stops_old_writers_again(self) -> None:
        for state in ("resuming", "complete"):
            with self.subTest(state=state), TemporaryDirectory() as directory:
                root = Path(directory)
                provider = IsolatedCutoverProvider(root, database_changing=True)
                self.addCleanup(provider.engine.dispose)
                store = cutover_store(root)

                def interrupt(step: str) -> None:
                    if step == "aborting":
                        raise TimeoutError("interrupted before old handoff")

                with (
                    patch.object(provider, "backup", side_effect=DefiniteBackupFailure()),
                    self.assertRaises(TimeoutError),
                ):
                    execute_odoo_cutover(
                        provider.cutover,
                        store,
                        provider,
                        record_id="original",
                        checkpoint=interrupt,
                    )
                held = store.load("original")
                assert held.pause is not None
                provider.target = provider.cutover.old
                pause = held.pause.model_copy(
                    update={"state": state, "recovery_binding": provider.cutover.old.binding}
                )
                if state == "complete":
                    from datetime import UTC, datetime

                    pause = pause.model_copy(
                        update={"ended_at": datetime.now(UTC), "duration_seconds": 1.0}
                    )
                store.save(held.model_copy(update={"pause": pause}))
                with (
                    patch.object(provider, "readiness") as readiness,
                    patch.object(provider, "switch") as switch,
                ):
                    result = execute_odoo_cutover(
                        provider.cutover, store, provider, record_id="original"
                    )
                self.assertEqual(result.step, "aborted")
                readiness.assert_not_called()
                switch.assert_not_called()

    def test_unhealthy_prepared_server_can_only_be_escaped_with_original_ownership(self) -> None:
        for owned in (True, False):
            with self.subTest(owned=owned), TemporaryDirectory() as directory:
                root = Path(directory)
                provider = IsolatedCutoverProvider(root, database_changing=True)
                self.addCleanup(provider.engine.dispose)
                store = cutover_store(root)
                with (
                    patch.object(provider, "backup", side_effect=TimeoutError()),
                    self.assertRaises(TimeoutError),
                ):
                    execute_odoo_cutover(provider.cutover, store, provider, record_id="original")
                with (
                    patch.object(provider, "backup", side_effect=DefiniteBackupFailure()),
                    patch.object(provider, "observed_public_copy", return_value=""),
                    patch.object(provider, "owns_prepared_pause", return_value=owned),
                ):
                    if owned:
                        result = execute_odoo_cutover(
                            provider.cutover, store, provider, record_id="original"
                        )
                        self.assertEqual(result.step, "aborted")
                    else:
                        with self.assertRaisesRegex(ValueError, "prepared-route authority"):
                            execute_odoo_cutover(
                                provider.cutover, store, provider, record_id="original"
                            )
                        self.assertEqual(store.load("original").step, "aborting")
