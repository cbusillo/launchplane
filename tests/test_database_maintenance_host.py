import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType
from typing import Any
import unittest
from unittest.mock import patch


def host_module() -> ModuleType:
    specification = importlib.util.spec_from_file_location(
        "maintenance_host", Path("scripts/database-maintenance-host.py")
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


class DatabaseMaintenanceHostTests(unittest.TestCase):
    def test_release_restarts_only_originals_after_ending_the_exact_keeper(self) -> None:
        host = host_module()
        with TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "state.json"
            state = {
                "hold_id": "synthetic",
                "project": "own",
                "containers": ["own-api", "own-worker"],
                "browser": "own-browser",
                "operator": "own-keeper",
                "postgres": "own-database",
                "database": "own",
                "keeper": {"pid": 17, "backend_start": "2026-01-01T00:00:00+00:00"},
                "stopping_started": True,
                "fenced": True,
                "expires_at": "2026-01-01T00:00:00+00:00",
            }
            state_path.write_text(json.dumps(state))
            commands: list[tuple[str, ...]] = []

            def run(*args: str, **kwargs: Any) -> str:
                commands.append(args)
                return ""

            healthy = {"State": {"Running": True, "Health": {"Status": "healthy"}}}
            with (
                patch.object(host, "run", side_effect=run),
                patch.object(host, "check_originals"),
                patch.object(host, "inspect_container", return_value=healthy),
                patch.object(host, "operator") as unfence,
                patch.object(host.time, "sleep"),
            ):
                host.release(state_path)
            self.assertEqual([command[1] for command in commands], ["exec", "ps", "stop", "start"])
            self.assertEqual(commands[-1], ("docker", "start", "own-api", "own-worker"))
            unfence.assert_called_once()
            self.assertEqual(unfence.call_args.args[0]["hold_id"], state["hold_id"])
            self.assertEqual(unfence.call_args.args[1], "unfence")
            self.assertTrue(json.loads(state_path.read_text())["released"])
            with patch.object(host, "run") as second:
                host.release(state_path)
            second.assert_not_called()

    def test_wrong_project_is_refused_before_restart(self) -> None:
        host = host_module()
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

    def test_failed_fence_still_invokes_supported_release(self) -> None:
        host = host_module()
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            from datetime import datetime, timedelta, timezone

            state = {
                "hold_id": "fixture",
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                "released": True,
            }
            path.write_text(json.dumps(state))

            def run(*args: str, **kwargs: Any) -> str:
                if "ExecStart" in args:
                    return str(path)
                if args[0] == "systemctl" and args[1] == "show":
                    return (
                        "ActiveState=active\n" + str(path)
                        if args[2].endswith(".timer") and args[-1] != "--property=ActiveState"
                        else "ActiveState=inactive\n" + str(path)
                    )
                return ""

            with (
                patch.object(host, "run", side_effect=run),
                patch.object(host, "operator", side_effect=RuntimeError("provider refused")),
                patch.object(host, "release") as release,
            ):
                with self.assertRaisesRegex(RuntimeError, "provider refused"):
                    host.hold(path, state, 300)
            release.assert_called_once_with(path)

    def test_large_bound_receipt_is_delivered_without_a_terminal_limit(self) -> None:
        import os
        import threading

        host = host_module()
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            fifo = root / "commands"
            os.mkfifo(fifo, 0o600)
            held = {"hold_id": "own", "snapshot": "snapshot", "transaction": "transaction"}
            state = {"held": held, "commands": str(fifo)}
            (root / "state.json").write_text(json.dumps(state))
            receipt = {**held, "restored": {"synthetic": "a" * 200000}}
            (root / "receipt.json").write_text(json.dumps(receipt))
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
            host.submit(root / "state.json", root / "receipt.json")
            reader.join(5)
            self.assertFalse(reader.is_alive())
            self.assertEqual(received, [receipt])
