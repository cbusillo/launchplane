from datetime import datetime, timedelta, timezone
import json
import os
import signal
import time
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
                "api": "api",
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
        state: dict[str, Any] = {
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
            elapsed = [0.0]
            timer.monotonic.side_effect = lambda: elapsed[0]
            timer.sleep.side_effect = lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds)
            with self.assertRaises(TimeoutError):
                host.resume_original("worker", state)
        self.assertEqual(events, [])

    def test_signal_handle_closes_and_refuses_a_replaced_process_membership(self) -> None:
        state: dict[str, Any] = {
            "image_id": "image",
            "originals": {"owned": {"pid": 19, "started_at": "start"}},
        }
        observed: dict[str, Any] = {
            "Image": "image",
            "State": {"Running": True, "Pid": 19, "StartedAt": "start"},
        }
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
                patch.object(host, "rearm_original_policies"),
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

    def test_api_signal_exit_is_qualified_without_accepting_worker_or_oom_exit(self) -> None:
        state: dict[str, Any] = {"api": "api"}
        observed: dict[str, Any] = {
            "Id": "api",
            "State": {"ExitCode": 128 + signal.SIGTERM, "OOMKilled": False},
        }
        self.assertTrue(host.clean_original_exit(observed, state))
        observed["Id"] = "worker"
        self.assertFalse(host.clean_original_exit(observed, state))
        observed["State"]["ExitCode"] = 0
        self.assertTrue(host.clean_original_exit(observed, state))
        observed["State"]["OOMKilled"] = True
        self.assertFalse(host.clean_original_exit(observed, state))

    def test_every_policy_is_rearmed_even_when_the_api_is_still_exiting(self) -> None:
        from tests.test_database_maintenance_host import state_file

        with TemporaryDirectory() as directory:
            path = state_file(Path(directory))
            state = json.loads(path.read_text())
            containers = {
                name: {
                    "Id": name,
                    "Image": state["image_id"],
                    "Config": {"Labels": {"com.docker.compose.project": state["project"]}},
                    "HostConfig": {"RestartPolicy": {"Name": "no"}},
                    "State": {"Running": True},
                }
                for name in state["containers"]
            }

            def run(*args: str, **_kwargs: Any) -> str:
                self.assertEqual(args[:2], ("docker", "update"))
                name = args[-1]
                containers[name]["HostConfig"]["RestartPolicy"] = state["originals"][name][
                    "restart_policy"
                ]
                return ""

            def pending(_name: str, _current: dict[str, Any]) -> str:
                for name in state["containers"]:
                    self.assertEqual(
                        containers[name]["HostConfig"]["RestartPolicy"],
                        state["originals"][name]["restart_policy"],
                    )
                raise TimeoutError("The original API is finishing its request.")

            with (
                patch.object(host, "terminate_keeper"),
                patch.object(host, "inspect_container", side_effect=lambda name: containers[name]),
                patch.object(host, "run", side_effect=run),
                patch.object(host, "resume_original", side_effect=pending),
            ):
                with self.assertRaises(TimeoutError):
                    host.release(path)
            self.assertFalse(json.loads(path.read_text()).get("released", False))

    def test_restart_backoff_never_counts_as_a_recovered_process(self) -> None:
        from tests.test_database_maintenance_host import state_file

        with TemporaryDirectory() as directory:
            state = json.loads(state_file(Path(directory)).read_text())
            state["resume_started"] = True
            observed = {
                "Image": state["image_id"],
                "Config": {"Labels": {"com.docker.compose.project": state["project"]}},
                "HostConfig": {"RestartPolicy": state["originals"][state["api"]]["restart_policy"]},
                "State": {"Running": True, "Restarting": True, "Pid": 0, "StartedAt": "new"},
            }
            elapsed = [0.0]
            with (
                patch.object(host, "inspect_container", return_value=observed),
                patch.object(host, "run") as run,
                patch.object(time, "monotonic", side_effect=lambda: elapsed[0]),
                patch.object(
                    time,
                    "sleep",
                    side_effect=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
                ),
            ):
                with self.assertRaises(RuntimeError):
                    host.resume_original(state["api"], state)
                run.assert_not_called()

    def test_recovery_waits_for_an_exiting_signal_target_but_preserves_real_identity_failures(
        self,
    ) -> None:
        from tests.test_database_maintenance_host import state_file

        for failure in ("pid_gone", "membership_gone", "restarting", "foreign_membership"):
            with self.subTest(failure=failure), TemporaryDirectory() as directory:
                state = json.loads(state_file(Path(directory)).read_text())
                name = state["api"]
                original = state["originals"][name]
                state.update(resume_started=True, stop_requested=[name])
                running = {
                    "Image": state["image_id"],
                    "Config": {"Labels": {"com.docker.compose.project": state["project"]}},
                    "HostConfig": {"RestartPolicy": original["restart_policy"]},
                    "State": {
                        "Running": True,
                        "Pid": original["pid"],
                        "StartedAt": original["started_at"],
                    },
                }
                backoff = {**running, "State": {**running["State"], "Restarting": True, "Pid": 0}}
                healthy = {
                    **running,
                    "State": {"Running": True, "Pid": original["pid"] + 1, "StartedAt": "new"},
                }
                sequence = {
                    "pid_gone": [running, backoff],
                    "membership_gone": [running, running, backoff],
                    "restarting": [running, backoff, backoff],
                    "foreign_membership": [running, running, running],
                }
                observations = iter(sequence[failure])
                membership = "foreign" if failure == "foreign_membership" else name
                with (
                    patch.object(
                        host, "inspect_container", side_effect=lambda _: next(observations, healthy)
                    ),
                    patch.object(
                        os,
                        "pidfd_open",
                        create=True,
                        return_value=77,
                        side_effect=ProcessLookupError() if failure == "pid_gone" else None,
                    ),
                    patch.object(os, "close"),
                    patch.object(
                        Path,
                        "read_text",
                        return_value="0::/docker/" + membership,
                        side_effect=FileNotFoundError() if failure == "membership_gone" else None,
                    ),
                    patch.object(signal, "pidfd_send_signal", create=True) as send,
                    patch.object(time, "sleep"),
                ):
                    if failure == "foreign_membership":
                        with self.assertRaises(host.RecoveryRequired):
                            host.resume_original(name, state)
                    else:
                        self.assertEqual(host.resume_original(name, state), name)
                    send.assert_not_called()

    def test_readback_requires_two_healthy_samples_of_the_same_live_process(self) -> None:
        from tests.test_database_maintenance_host import state_file

        with TemporaryDirectory() as directory:
            state = json.loads(state_file(Path(directory)).read_text())
            state["resume_started"] = True
            name = state["api"]
            first = {
                "Running": True,
                "Pid": state["originals"][name]["pid"] + 1,
                "StartedAt": "first",
            }
            stable = {**first, "Pid": first["Pid"] + 1, "StartedAt": "stable"}
            backoff = {**first, "Restarting": True, "Pid": 0}
            for pair in (
                (backoff, backoff),
                ({**first, "Pid": 0}, stable),
                (first, {**stable, "Health": {"Status": "unhealthy"}}),
                (first, stable),
            ):
                with self.subTest(pair=pair):
                    sequence = iter([first, *pair])
                    elapsed = [0.0]
                    reads = []

                    def inspect(_name: str) -> dict[str, Any]:
                        current = next(sequence, stable)
                        reads.append((elapsed[0], current))
                        return {
                            "Image": state["image_id"],
                            "Config": {"Labels": {"com.docker.compose.project": state["project"]}},
                            "State": current,
                        }

                    with (
                        patch.object(host, "inspect_container", side_effect=inspect),
                        patch.object(time, "monotonic", side_effect=lambda: elapsed[0]),
                        patch.object(
                            time,
                            "sleep",
                            side_effect=lambda seconds: elapsed.__setitem__(
                                0, elapsed[0] + seconds
                            ),
                        ),
                    ):
                        self.assertEqual(host.resume_original(name, state), name)
                    earlier, later = reads[-2:]
                    self.assertGreater(later[0], earlier[0])
                    self.assertEqual(
                        (earlier[1]["Pid"], earlier[1]["StartedAt"]),
                        (later[1]["Pid"], later[1]["StartedAt"]),
                    )
                    for _, observed in reads[-2:]:
                        self.assertGreater(observed["Pid"], 0)
                        self.assertFalse(observed.get("Restarting"))
                        self.assertEqual(
                            observed.get("Health", {}).get("Status", "healthy"), "healthy"
                        )

    def test_socket_refresh_survives_the_same_database_process_restart(self) -> None:
        import socket

        with TemporaryDirectory() as directory:
            root = Path(directory)
            merged = root / "merged"
            socket_root = merged / "run/postgresql"
            socket_root.mkdir(parents=True)
            endpoint = socket_root / ".s.PGSQL.5432"
            first = socket.socket(socket.AF_UNIX)
            self.addCleanup(first.close)
            first.bind(str(endpoint))
            overlay = root / "operator.json"
            overlay.write_text(
                json.dumps({"services": {"launchplane-maintenance-verification": {}}})
            )
            state: dict[str, Any] = {
                "postgres": "database",
                "database": "own",
                "system_identifier": "same-cluster",
                "overlay": str(overlay),
                "job_directory": str(root),
            }
            postgres: dict[str, Any] = {
                "State": {"Running": True, "Pid": 10, "StartedAt": "first"},
                "Mounts": [],
                "GraphDriver": {"Data": {"MergedDir": str(merged)}},
            }
            actual_identity = host.socket_identity

            def identity(path: Path) -> tuple[int, int]:
                return actual_identity(endpoint if str(path).startswith("/proc/") else path)

            with (
                patch.object(host, "inspect_container", return_value=postgres),
                patch.object(host, "run", return_value="/run/postgresql"),
                patch.object(host, "socket_identity", side_effect=identity),
                patch.object(
                    host, "read_database", return_value=state["system_identifier"]
                ) as cluster,
            ):
                host.refresh_verification_socket(state)
                self.assertEqual(state["verification_socket"]["pid"], postgres["State"]["Pid"])
                first.close()
                endpoint.unlink()
                second = socket.socket(socket.AF_UNIX)
                self.addCleanup(second.close)
                second.bind(str(endpoint))
                postgres["State"].update(Pid=20, StartedAt="restarted")
                host.refresh_verification_socket(state)
                self.assertEqual(state["verification_socket"]["pid"], postgres["State"]["Pid"])
                self.assertEqual(
                    (state["verification_socket"]["device"], state["verification_socket"]["inode"]),
                    actual_identity(endpoint),
                )
                cluster.return_value = "different-cluster"
                with self.assertRaises(host.RecoveryRequired):
                    host.refresh_verification_socket(state)
