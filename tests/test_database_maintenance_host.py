from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import subprocess
import time
from typing import Any
import unittest
from unittest.mock import patch

from control_plane import database_maintenance_host as host


def state_file(root: Path, **changes: Any) -> Path:
    state = {
        "hold_id": "synthetic",
        "project": "own",
        "containers": ["own-api", "own-train"],
        "api": "own-api",
        "browser": "own-browser",
        "operator": "own-keeper",
        "postgres": "own-database",
        "database": "own",
        "target_id": "own-target",
        "image_id": "synthetic-image",
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "job_directory": str(root),
        "commands": str(root / "commands"),
        "directory": str(root),
        "compose": str(root / "compose.yaml"),
        "overlay": str(root / "operator.json"),
        **changes,
    }
    path = root / "state.json"
    host.save(path, state)
    return path


class DatabaseMaintenanceHostTests(unittest.TestCase):
    def test_release_restarts_originals_and_rejects_a_late_driver_stop_or_save(self) -> None:
        with TemporaryDirectory() as temporary:
            path = state_file(Path(temporary))
            started: list[str] = []

            def restart(identifier: str, _state: dict[str, Any], _settled: Any = None) -> str:
                started.append(identifier)
                return identifier

            with (
                patch.object(host, "original_containers"),
                patch.object(host, "operator_container", return_value=None),
                patch.object(host, "restart_original", side_effect=restart),
            ):
                host.release(path)
            self.assertEqual(set(started), set(json.loads(path.read_text())["containers"]))
            self.assertTrue(json.loads(path.read_text())["released"])
            with patch.object(subprocess, "Popen") as stop:
                with self.assertRaises(RuntimeError):
                    host.launch_stop(path, "own-train")
            stop.assert_not_called()
            with self.assertRaises(RuntimeError):
                host.update_state(path, held={"stale": "driver copy"})
            self.assertTrue(json.loads(path.read_text())["released"])
            with patch.object(host, "restart_original") as second:
                host.release(path)
            second.assert_not_called()

    def test_unfence_failure_does_not_restart_services_twice(self) -> None:
        with TemporaryDirectory() as temporary:
            path = state_file(Path(temporary), fenced=True, fence_confirmed=True)
            with (
                patch.object(host, "original_containers"),
                patch.object(host, "operator_container", return_value=None),
                patch.object(
                    host,
                    "restart_original",
                    side_effect=lambda identifier, _state, _settled: identifier,
                ) as restart,
                patch.object(
                    host, "operator", side_effect=[RuntimeError("provider unavailable"), {}]
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "readback|unavailable"):
                    host.release(path)
                first_count = restart.call_count
                self.assertTrue(json.loads(path.read_text())["restarted"])
                host.release(path)
                self.assertEqual(restart.call_count, first_count)
            self.assertTrue(json.loads(path.read_text())["released"])

    def test_recovery_of_api_and_train_does_not_wait_for_a_long_capture_stop(self) -> None:
        with TemporaryDirectory() as temporary:
            path = state_file(Path(temporary), containers=["own-api", "own-train", "own-capture"])
            allow_capture = threading.Event()
            failed: list[BaseException] = []

            def restart(identifier: str, _state: dict[str, Any], _settled: Any = None) -> str:
                if identifier == "own-capture" and not allow_capture.wait(5):
                    raise RuntimeError("Test capture did not finish.")
                return identifier

            def release() -> None:
                try:
                    host.release(path)
                except BaseException as error:
                    failed.append(error)

            with (
                patch.object(host, "original_containers"),
                patch.object(host, "operator_container", return_value=None),
                patch.object(host, "restart_original", side_effect=restart),
            ):
                worker = threading.Thread(target=release)
                worker.start()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and set(
                    json.loads(path.read_text()).get("restarted_containers", [])
                ) != {"own-api", "own-train"}:
                    time.sleep(0.01)
                self.assertEqual(
                    set(json.loads(path.read_text())["restarted_containers"]),
                    {"own-api", "own-train"},
                )
                self.assertFalse(json.loads(path.read_text()).get("released", False))
                allow_capture.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertFalse(failed)
            self.assertTrue(json.loads(path.read_text())["released"])

    def test_restart_waits_for_its_recorded_stop_client_without_canceling_it(self) -> None:
        stop_finished = False

        def sleep(_seconds: float) -> None:
            nonlocal stop_finished
            stop_finished = True

        def run(*_args: str, **_kwargs: Any) -> str:
            self.assertTrue(stop_finished)
            return ""

        healthy = {"State": {"Running": True}}
        with (
            patch.object(host, "process_running", side_effect=lambda _process: not stop_finished),
            patch.object(time, "sleep", side_effect=sleep),
            patch.object(host, "run", side_effect=run),
            patch.object(host, "inspect_container", return_value=healthy),
        ):
            self.assertEqual(
                host.restart_original(
                    "own-api", {"stops": {"own-api": {"pid": 1, "start": "owned"}}}
                ),
                "own-api",
            )

    def test_wrong_project_is_refused_before_restart(self) -> None:
        state = {"containers": ["own-api"], "project": "own"}
        with (
            patch.object(
                host,
                "inspect_container",
                return_value={"Config": {"Labels": {"com.docker.compose.project": "another"}}},
            ),
            patch.object(host, "run") as run,
        ):
            with self.assertRaisesRegex(RuntimeError, "project identity"):
                host.check_originals(state)
        run.assert_not_called()

    def test_failed_controller_start_still_invokes_supported_release(self) -> None:
        with TemporaryDirectory() as temporary:
            path = state_file(Path(temporary))

            def run(*args: str, **_kwargs: Any) -> str:
                if args[:2] == ("systemctl", "show"):
                    return "ActiveState=active\n" + str(path)
                return ""

            with (
                patch.object(host, "run", side_effect=run),
                patch.object(subprocess, "Popen", side_effect=RuntimeError("controller refused")),
                patch.object(host, "boot_identity", return_value="synthetic"),
                patch.object(host, "release") as release,
            ):
                with self.assertRaisesRegex(RuntimeError, "controller refused"):
                    host.hold(path)
            release.assert_called_once_with(path)

    def test_large_receipt_is_bound_to_the_stored_dump_hash(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            fifo = root / "commands"
            os.mkfifo(fifo, 0o600)
            held = {"hold_id": "own", "snapshot": "snapshot", "transaction": "transaction"}
            path = state_file(root, held=held, backup_sha256="a" * 64)
            receipt = {**held, "backup_sha256": "b" * 64, "restored": {"synthetic": "a" * 200000}}
            receipt_path = root / "receipt.json"
            receipt_path.write_text(json.dumps(receipt))
            with patch.object(os, "open") as open_fifo:
                with self.assertRaisesRegex(ValueError, "another dump"):
                    host.submit(path, receipt_path)
            open_fifo.assert_not_called()
            receipt["backup_sha256"] = "a" * 64
            receipt_path.write_text(json.dumps(receipt))
            received: list[dict[str, Any]] = []
            ready = threading.Event()

            def read() -> None:
                descriptor = os.open(fifo, os.O_RDWR)
                ready.set()
                with os.fdopen(descriptor) as stream:
                    received.append(json.loads(stream.readline()))

            reader = threading.Thread(target=read)
            reader.start()
            self.assertTrue(ready.wait(5))
            host.submit(path, receipt_path)
            reader.join(5)
            self.assertFalse(reader.is_alive())
            self.assertEqual(received, [receipt])

    def test_dormant_writer_outside_the_project_refuses_before_pause(self) -> None:
        database = "postgresql://synthetic:credential@own/database"
        api: dict[str, Any] = {
            "Image": "own-image",
            "Config": {
                "Labels": {"org.opencontainers.image.source": "https://source.invalid/own/control"},
                "Env": ["LAUNCHPLANE_DATABASE_URL=" + database],
            },
        }
        outside: dict[str, Any] = {
            "Image": "own-image",
            "Name": "/other-job",
            "State": {"Running": False},
            "Config": {
                "Labels": {"com.docker.compose.project": "other"},
                "Env": ["LAUNCHPLANE_DATABASE_URL=" + database],
            },
        }
        with (
            patch.object(host, "run", return_value="original\noutside\n"),
            patch.object(host, "inspect_container", return_value=outside),
            patch.object(subprocess, "Popen") as stop,
        ):
            with self.assertRaises(RuntimeError):
                host.require_no_outside_writers(api, {"original"}, project="own", operators=set())
        stop.assert_not_called()
        outside["Config"]["Env"] = ["LAUNCHPLANE_DATABASE_URL=postgresql://isolated/restore"]
        with (
            patch.object(host, "run", return_value="outside\n"),
            patch.object(host, "inspect_container", return_value=outside),
        ):
            host.require_no_outside_writers(api, {"original"}, project="own", operators=set())

    def test_a_failed_writer_inventory_does_not_prevent_releasing_our_services(self) -> None:
        with TemporaryDirectory() as temporary:
            path = state_file(Path(temporary))
            with (
                patch.object(host, "original_containers"),
                patch.object(
                    host, "check_originals", side_effect=RuntimeError("another writer appeared")
                ) as inventory,
                patch.object(host, "operator_container", return_value=None),
                patch.object(
                    host,
                    "restart_original",
                    side_effect=lambda identifier, _state, _settled: identifier,
                ),
            ):
                host.release(path)
            inventory.assert_not_called()
            self.assertTrue(json.loads(path.read_text())["released"])
