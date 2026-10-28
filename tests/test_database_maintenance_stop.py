from datetime import datetime, timedelta, timezone
import json
import os
import signal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

from control_plane import database_maintenance_host as host


class StoppedWriterHoldTests(unittest.TestCase):
    def test_every_signal_is_durable_and_all_exits_precede_admission(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            state: dict[str, Any] = {
                "containers": ["api", "worker"],
                "project": "own",
                "image_id": "image",
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
                "originals": {
                    name: {"pid": index, "started_at": "start"}
                    for index, name in enumerate(("api", "worker"), 10)
                },
            }
            host.save(path, state)
            running = {"api": True, "worker": True}
            signals = []

            def inspect(identifier: str) -> dict[str, Any]:
                return {
                    "Id": identifier,
                    "Image": "image",
                    "Config": {"Labels": {"com.docker.compose.project": "own"}},
                    "State": {
                        "Running": running[identifier],
                        "Pid": state["originals"][identifier]["pid"],
                        "StartedAt": "start",
                        "ExitCode": 0,
                        "OOMKilled": False,
                    },
                    "HostConfig": {"RestartPolicy": {"Name": "no"}},
                }

            def run(*arguments: str, **_kwargs: Any) -> str:
                return ""

            def signal_original(identifier: str, _state: dict[str, Any]) -> None:
                self.assertIn(identifier, json.loads(path.read_text())["stop_requested"])
                signals.append(identifier)
                running[identifier] = False

            with (
                patch.object(host, "inspect_container", side_effect=inspect),
                patch.object(host, "run", side_effect=run),
                patch.object(host, "signal_original", side_effect=signal_original),
                patch.object(host, "check_originals"),
            ):
                result = host.stop_services(path)
            self.assertEqual(set(signals), set(state["containers"]))
            self.assertEqual(set(result["stopped_containers"]), set(state["containers"]))
            self.assertFalse(any(running.values()))

    def test_a_late_exit_cannot_be_reported_as_resumed(self) -> None:
        state = {
            "containers": ["worker"],
            "originals": {"worker": {"pid": 12, "started_at": "old"}},
            "stop_requested": ["worker"],
            "project": "own",
            "image_id": "image",
        }
        running = True
        events = []

        def inspect(_identifier: str) -> dict[str, Any]:
            return {
                "Id": "worker",
                "Image": "image",
                "Config": {"Labels": {"com.docker.compose.project": "own"}},
                "State": {"Running": running, "Pid": 12 if running else 0, "StartedAt": "old"},
            }

        def run(*arguments: str, **_kwargs: Any) -> str:
            nonlocal running
            if arguments[:2] == ("docker", "start"):
                self.assertFalse(running)
                events.append("start")
                running = True
            return ""

        with (
            patch.object(host, "inspect_container", side_effect=inspect),
            patch.object(host, "run", side_effect=run),
            patch.object(host, "signal_original"),
            patch.object(host, "time") as timer,
        ):
            timer.monotonic.side_effect = [0, 0, host.STOP_WAIT_SECONDS + 1]
            with self.assertRaises(host.RecoveryRequired):
                host.resume_original("worker", state)
        self.assertEqual(events, [])

    def test_signal_handle_closes_and_refuses_a_replaced_process_membership(self) -> None:
        state = {"image_id": "image", "originals": {"owned": {"pid": 19, "started_at": "start"}}}
        observed = {"Image": "image", "State": {"Running": True, "Pid": 19, "StartedAt": "start"}}
        for membership, allowed in (("0::/docker/owned", True), ("0::/docker/foreign", False)):
            with (
                self.subTest(membership=membership),
                patch.object(os, "pidfd_open", create=True, return_value=27) as open_pidfd,
                patch.object(os, "close") as close,
                patch.object(signal, "pidfd_send_signal", create=True) as send,
                patch.object(host, "inspect_container", return_value=observed),
                patch.object(Path, "read_text", return_value=membership),
            ):
                if allowed:
                    host.signal_original("owned", state)
                    send.assert_called_once_with(open_pidfd.return_value, signal.SIGTERM)
                else:
                    with self.assertRaises(host.RecoveryRequired):
                        host.signal_original("owned", state)
                    send.assert_not_called()
                close.assert_called_once_with(open_pidfd.return_value)

    def test_health_failure_keeps_every_original_stopped_before_a_retry(self) -> None:
        from tests.test_database_maintenance_host import state_file

        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = state_file(root)
            (root / "commit-pending.json").write_text("{}")
            proof = {"revision": "verified", "outcome": "after", "health_passed": False}
            with (
                patch.object(host, "terminate_keeper"),
                patch.object(host, "original_containers", return_value=[]),
                patch.object(host, "operator", side_effect=lambda *_: dict(proof)),
                patch.object(host, "require_no_api_database_clients"),
                patch.object(host, "require_database_cluster"),
                patch.object(host, "operator_container", return_value=None),
                patch.object(host, "remove_watchdog"),
                patch.object(host, "resume_original", side_effect=lambda name, _: name) as resume,
            ):
                with self.assertRaises(host.RecoveryRequired):
                    host.release(path)
                resume.assert_not_called()
                self.assertFalse(json.loads(path.read_text()).get("resume_started", False))
                proof["health_passed"] = True
                host.release(path)
                names = [call.args[0] for call in resume.call_args_list]
                self.assertEqual(names[0], json.loads(path.read_text())["api"])
                self.assertTrue(json.loads(path.read_text())["released"])
