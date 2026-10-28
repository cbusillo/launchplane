from datetime import datetime, timedelta, timezone
from contextlib import contextmanager, redirect_stdout
from collections.abc import Iterator
import json
from io import StringIO
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
from typing import Any
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock
from unittest.mock import patch

from control_plane import database_maintenance_host as host


def state_file(root: Path, **changes: Any) -> Path:
    originals = {}
    for index, identifier in enumerate(("own-api", "own-train"), start=10):
        group = root / identifier
        group.mkdir()
        (group / "cgroup.freeze").write_text("0")
        (group / "cgroup.events").write_text("populated 1\nfrozen 0\n")
        metadata = group.stat()
        originals[identifier] = {
            "cgroup": str(group),
            "device": metadata.st_dev,
            "inode": metadata.st_ino,
            "pid": index,
            "started_at": "original-start",
            "restart_policy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
        }
    state = {
        "hold_id": "synthetic",
        "project": "own",
        "containers": list(originals),
        "api": "own-api",
        "browser": "own-browser",
        "operator": "own-keeper",
        "postgres": "own-database",
        "database": "own",
        "target_id": "own-target",
        "image_id": "synthetic-image",
        "system_identifier": "own-cluster",
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "job_directory": str(root),
        "commands": str(root / "commands"),
        "directory": str(root),
        "compose": str(root / "compose.yaml"),
        "overlay": str(root / "operator.json"),
        "originals": originals,
        **changes,
    }
    path = root / "state.json"
    host.save(path, state)
    return path


class DatabaseMaintenanceHostTests(unittest.TestCase):
    def test_only_api_thaws_for_a_fresh_http_probe_then_refreezes_before_socket_and_fence_cleanup(
        self,
    ) -> None:
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                requests.append(self.path)
                self.send_response(
                    200 if self.path == "/v1/health?maintenance_liveness=true" else 404
                )
                self.end_headers()
                self.wfile.write(json.dumps({"status": "ok"}).encode())

            def log_message(self, _format: str, *args: Any) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = state_file(root, revision="verified")
            state = json.loads(path.read_text())
            phase = {
                "phase_id": "a" * 32,
                "table": "lpmaint_" + "a" * 32,
                "namespace": "synthetic",
                "active": True,
                "health_passed": False,
            }
            frozen = {identifier: True for identifier in state["containers"]}
            changes = []
            actions = []
            fenced = False

            def freeze(original: dict[str, Any], wanted: bool) -> None:
                identifier = next(
                    key
                    for key, value in state["originals"].items()
                    if value["pid"] == original["pid"]
                )
                changes.append((identifier, wanted))
                frozen[identifier] = wanted
                actions.append("freeze" if wanted else "thaw")

            def read(original: dict[str, Any]) -> tuple[str, str]:
                identifier = next(
                    key
                    for key, value in state["originals"].items()
                    if value["pid"] == original["pid"]
                )
                return ("1", "1") if frozen[identifier] else ("0", "0")

            def network(_state: dict[str, Any], *arguments: str, **_values: Any) -> str:
                nonlocal fenced
                if arguments[:3] == ("nft", "-f", "-"):
                    fenced = True
                    actions.append("fence")
                    return ""
                if arguments == ("ss", "-Hlx"):
                    return ""
                if arguments == ("ss", "-Knt", "state", "connected"):
                    self.assertTrue(frozen[state["api"]])
                    actions.append("destroy")
                    return ""
                if arguments == ("ss", "-Hnt", "state", "connected"):
                    return ""
                if arguments[:3] == ("nft", "delete", "table"):
                    self.assertTrue(frozen[state["api"]])
                    actions.append("unfence")
                    fenced = False
                    return ""
                if arguments == ("nft", "-j", "list", "tables"):
                    return json.dumps(
                        {"nftables": [{"table": {"name": phase["table"]}}] if fenced else []}
                    )
                if arguments[:4] == ("nft", "-j", "list", "table"):
                    if not fenced:
                        raise AssertionError("The actual API probe requires its network fence")
                    entries = []
                    for name in ("input", "output", "postrouting"):
                        entries.extend(
                            [
                                {
                                    "chain": {
                                        "name": name,
                                        "hook": name,
                                        "type": "filter",
                                        "prio": host.API_EGRESS_PRIORITY,
                                    }
                                },
                                {
                                    "rule": {
                                        "chain": name,
                                        "expr": [
                                            {
                                                "match": {
                                                    "op": "!=",
                                                    "left": {
                                                        "meta": {
                                                            "key": "iifname"
                                                            if name == "input"
                                                            else "oifname"
                                                        }
                                                    },
                                                    "right": "lo",
                                                }
                                            },
                                            {"counter": {"packets": 1}},
                                            {"drop": None},
                                        ],
                                    }
                                },
                            ]
                        )
                    return json.dumps({"nftables": entries})
                raise AssertionError("Unexpected namespace operation")

            process = Mock(returncode=0)
            process.poll.return_value = 0

            def start(command: list[str]) -> Mock:
                self.assertTrue(fenced)
                self.assertFalse(frozen[state["api"]])
                self.assertTrue(frozen["own-train"])
                exec(compile(command[-1], "<actual-health-program>", "exec"), {})
                return process

            def require_cpu(_state: dict[str, Any], *, api_thawed: bool) -> None:
                self.assertEqual(frozen[state["api"]], not api_thawed)
                self.assertTrue(frozen["own-train"])

            with (
                patch.object(host, "qualify_api_namespace", return_value=phase),
                patch.object(host, "api_namespace_command", side_effect=network),
                patch.object(
                    host,
                    "inspect_container",
                    return_value={
                        "State": {
                            "Running": True,
                            "Pid": state["originals"][state["api"]]["pid"],
                            "StartedAt": state["originals"][state["api"]]["started_at"],
                        }
                    },
                ),
                patch.object(host, "set_freezer", side_effect=freeze),
                patch.object(host, "read_freezer", side_effect=read),
                patch.object(host, "require_no_api_database_clients"),
                patch.object(host, "require_verification_cpu_hold", side_effect=require_cpu),
                patch.object(host, "install_watchdog"),
                patch.object(host, "remove_watchdog"),
                patch.object(host, "start_process", side_effect=start),
                patch.object(host, "run", return_value=""),
                patch.dict(os.environ, {"LAUNCHPLANE_SERVICE_PORT": str(server.server_address[1])}),
            ):
                host.verify_api_health(path)
            self.assertEqual(len(requests), 1)
            self.assertEqual(changes, [(state["api"], False), (state["api"], True)])
            self.assertLess(actions.index("destroy"), actions.index("thaw"))
            final_destroy = len(actions) - 1 - actions[::-1].index("destroy")
            self.assertLess(actions.index("freeze"), final_destroy)
            self.assertLess(final_destroy, actions.index("unfence"))
            self.assertTrue(
                json.loads((root / "verification-complete.json").read_text())["health_passed"]
            )

    def test_release_retries_transient_health_and_verifies_before_any_original_resumes(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = state_file(root, verification_phase={"phase_id": "own", "active": True})
            (root / "commit-pending.json").write_text("{}")
            events = []
            fail_health = True

            def health(path: Path) -> None:
                nonlocal fail_health
                events.append("health")
                if fail_health:
                    fail_health = False
                    raise RuntimeError("Transient health transport failure")
                state = json.loads(path.read_text())
                state["verification_phase"]["active"] = False
                host.save(path, state)
                host.save(
                    root / "verification-complete.json",
                    {
                        **{
                            key: state[key]
                            for key in ("hold_id", "system_identifier", "image_id", "revision")
                        },
                        "health_passed": True,
                    },
                )

            def resume_original(identifier: str, _state: dict[str, Any]) -> str:
                events.append("resume")
                return identifier

            with (
                patch.object(
                    host,
                    "cleanup_api_probe",
                    side_effect=lambda _state: events.append("cleanup"),
                ),
                patch.object(
                    host, "terminate_keeper", side_effect=lambda _state: events.append("terminate")
                ),
                patch.object(host, "original_containers", return_value=[]),
                patch.object(host, "operator", return_value={"revision": "verified"}),
                patch.object(host, "verify_api_health", side_effect=health),
                patch.object(
                    host,
                    "resume_original",
                    side_effect=resume_original,
                ) as resume,
                patch.object(host, "operator_container", return_value=None),
                patch.object(host, "remove_watchdog"),
            ):
                with self.assertRaises(RuntimeError):
                    host.release(path)
                resume.assert_not_called()
                host.release(path)
            self.assertEqual(events[:2], ["cleanup", "terminate"])
            self.assertLess(events.index("health"), events.index("resume"))
            self.assertTrue(json.loads(path.read_text())["released"])

    def test_release_cannot_finish_between_health_intent_and_native_fence_install(self) -> None:
        with TemporaryDirectory() as temporary:
            path = state_file(Path(temporary), revision="verified")
            original_lock = host.locked_state
            entries = 0
            mutations_after_release = []

            @contextmanager
            def interleaved(path: Path, **values: Any) -> Iterator[dict[str, Any]]:
                nonlocal entries
                entries += 1
                first = entries == 1
                with original_lock(path, **values) as state:
                    yield state
                if first:
                    with original_lock(path) as state:
                        state["released"] = True
                        state["verification_phase"]["active"] = False

            def network(state: dict[str, Any], *arguments: str, **_values: Any) -> str:
                if arguments[:3] == ("nft", "-f", "-"):
                    mutations_after_release.append(state.get("released", False))
                    raise RuntimeError("Fixture aborts before native mutation")
                return ""

            def cleanup(state: dict[str, Any], **_values: Any) -> None:
                if state.get("verification_phase"):
                    state["verification_phase"]["active"] = False

            phase = {"phase_id": "a" * 32, "table": "lpmaint_" + "a" * 32, "active": True}
            with (
                patch.object(host, "locked_state", side_effect=interleaved),
                patch.object(host, "qualify_api_namespace", return_value=phase),
                patch.object(host, "require_no_api_database_clients"),
                patch.object(host, "api_namespace_command", side_effect=network),
                patch.object(host, "cleanup_api_probe", side_effect=cleanup),
                patch.object(host, "remove_watchdog"),
            ):
                with self.assertRaises(RuntimeError):
                    host.verify_api_health(path)
            self.assertFalse(any(mutations_after_release))

    def test_old_health_controller_cannot_complete_or_clean_a_replacement_phase(self) -> None:
        with TemporaryDirectory() as temporary:
            path = state_file(Path(temporary), revision="verified")
            phase = {"phase_id": "a" * 32, "table": "lpmaint_" + "a" * 32, "active": True}

            def replace(_command: list[str]) -> Mock:
                with host.locked_state(path) as state:
                    state["verification_phase"]["phase_id"] = "b" * 32
                return Mock(poll=Mock(return_value=0), stdout=None, stdin=None, returncode=0)

            with (
                patch.object(host, "qualify_api_namespace", return_value=phase),
                patch.object(host, "require_no_api_database_clients"),
                patch.object(host, "api_namespace_command", return_value=""),
                patch.object(host, "require_api_egress_fence"),
                patch.object(host, "install_watchdog"),
                patch.object(host, "remove_watchdog") as remove,
                patch.object(host, "set_freezer"),
                patch.object(host, "start_process", side_effect=replace),
            ):
                with self.assertRaises(RuntimeError):
                    host.verify_api_health(path)
                remove.assert_not_called()
            self.assertTrue(json.loads(path.read_text())["verification_phase"]["active"])
            self.assertFalse((path.parent / "verification-complete.json").exists())

    def test_transient_verification_remains_manually_recoverable_without_a_state_edit(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = state_file(root)
            (root / "commit-pending.json").write_text("{}")
            with (
                patch.object(host, "terminate_keeper"),
                patch.object(host, "original_containers", return_value=[]),
                patch.object(host, "operator", side_effect=RuntimeError("Transient read")),
                patch.object(host, "resume_original") as resume,
            ):
                for _ in range(host.WATCHDOG_ATTEMPTS + 1):
                    with self.assertRaisesRegex(RuntimeError, "Transient read"):
                        host.release(path)
                resume.assert_not_called()
            self.assertNotIn("recovery_required", json.loads(path.read_text()))
            with (
                patch.object(host, "terminate_keeper"),
                patch.object(host, "original_containers", return_value=[]),
                patch.object(
                    host, "operator", return_value={"revision": "verified", "outcome": "before"}
                ),
                patch.object(
                    host, "resume_original", side_effect=lambda identifier, _state: identifier
                ),
                patch.object(host, "operator_container", return_value=None),
                patch.object(host, "remove_watchdog"),
            ):
                host.release(path)
            self.assertTrue(json.loads(path.read_text())["released"])

    def test_automatic_budget_survives_failures_before_and_after_owner_observation(self) -> None:
        for failure in ("owner", "verification"):
            with self.subTest(failure=failure), TemporaryDirectory() as temporary:
                path = state_file(Path(temporary))
                with (
                    patch.object(sys, "argv", ["driver", "recover", "--state", str(path)]),
                    patch.object(
                        host,
                        "run",
                        side_effect=RuntimeError("Owner read") if failure == "owner" else None,
                        return_value=str(os.getpid()),
                    ),
                    patch.object(
                        host, "release", side_effect=RuntimeError("Verification unavailable")
                    ) as release,
                ):
                    for _ in range(host.WATCHDOG_ATTEMPTS):
                        with self.assertRaises(RuntimeError):
                            host.main()
                    self.assertEqual(host.main(), host.AUTOMATIC_RECOVERY_EXHAUSTED_EXIT)
                self.assertEqual(
                    json.loads(path.read_text())["automatic_attempts"], host.WATCHDOG_ATTEMPTS
                )
                if failure == "owner":
                    release.assert_not_called()
                with host.locked_state(path) as state:
                    state["released"] = True
                with (
                    patch.object(sys, "argv", ["driver", "recover", "--state", str(path)]),
                    patch.object(host, "run", return_value=str(os.getpid())),
                    patch.object(host, "release") as cleanup,
                ):
                    self.assertEqual(host.main(), 0)
                    cleanup.assert_called_once_with(path)

    def test_manual_release_can_follow_an_already_exhausted_joined_job(self) -> None:
        with TemporaryDirectory() as temporary:
            path = state_file(Path(temporary), automatic_attempts=host.WATCHDOG_ATTEMPTS)
            old_job_finishing = True

            def run(*arguments: str, **_values: Any) -> str:
                nonlocal old_job_finishing
                if arguments[:2] == ("systemctl", "start"):
                    if old_job_finishing:
                        old_job_finishing = False
                        raise RuntimeError("The joined old job already selected exhaustion")
                    with host.locked_state(path) as state:
                        self.assertLess(state["automatic_attempts"], host.WATCHDOG_ATTEMPTS)
                        state.update(released=True, watchdog_removed=True)
                    return ""
                if arguments[:2] == ("systemctl", "show"):
                    return str(host.AUTOMATIC_RECOVERY_EXHAUSTED_EXIT)
                if arguments[:2] == ("systemctl", "reset-failed"):
                    return ""
                raise AssertionError("Unexpected fixture operation")

            with patch.object(host, "run", side_effect=run):
                host.request_release(path, manual=True)
            self.assertTrue(json.loads(path.read_text())["released"])

    def test_metadata_cleanup_waits_for_the_current_release_owner_before_stopping_units(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            path = state_file(Path(temporary))
            owner_entered = threading.Event()
            owner_can_finish = threading.Event()
            owner_finished = threading.Event()
            stopped_active_owner = []
            errors = []
            original_lock = host.locked_state

            def owner() -> None:
                with original_lock(path) as state:
                    state["released"] = True
                    host.save(path, state)
                    owner_entered.set()
                    owner_can_finish.wait(5)
                    state["watchdog_removed"] = True
                    host.save(path, state)
                owner_finished.set()

            @contextmanager
            def observe_lock(path: Path, **values: Any) -> Iterator[dict[str, Any]]:
                owner_can_finish.set()
                with original_lock(path, **values) as state:
                    yield state

            def run(*arguments: str, **_values: Any) -> str:
                if arguments[:2] == ("systemctl", "stop"):
                    stopped_active_owner.append(not owner_finished.is_set())
                    owner_can_finish.set()
                return "LoadState=loaded"

            thread = threading.Thread(target=owner)
            thread.start()
            try:
                self.assertTrue(owner_entered.wait(5))
                with (
                    patch.object(host, "locked_state", side_effect=observe_lock),
                    patch.object(host, "run", side_effect=run),
                    patch.object(host, "remove_watchdog"),
                    patch.object(host, "emit_release"),
                ):
                    try:
                        host.request_release(path)
                    except Exception as error:
                        errors.append(error)
            finally:
                owner_can_finish.set()
                thread.join(5)
            self.assertFalse(errors)
            self.assertFalse(any(stopped_active_owner))
            self.assertTrue(owner_finished.is_set())

    def test_completed_release_finishes_missing_unit_metadata_before_success(self) -> None:
        with TemporaryDirectory() as temporary:
            path = state_file(Path(temporary), released=True)
            observed = []

            def cleanup(state: dict[str, Any], **_values: Any) -> None:
                state["watchdog_removed"] = True
                host.save(path, state)

            def emit(state: dict[str, Any]) -> None:
                observed.append(state["watchdog_removed"])

            with (
                patch.object(host, "run", return_value="LoadState=not-found"),
                patch.object(host, "remove_watchdog", side_effect=cleanup),
                patch.object(host, "emit_release", side_effect=emit),
                patch.object(
                    host, "release", side_effect=AssertionError("Runtime recovery must not repeat")
                ),
            ):
                host.request_release(path)
            self.assertEqual(observed, [True])
            self.assertTrue(json.loads(path.read_text())["watchdog_removed"])

    def test_namespace_command_uses_and_closes_the_opened_handle(self) -> None:
        with TemporaryDirectory() as temporary:
            namespace = Path(temporary) / "namespace"
            namespace.touch()
            descriptor = os.open(namespace, os.O_RDONLY)
            metadata = os.fstat(descriptor)
            state = {
                "api": "own-api",
                "originals": {"own-api": {"pid": 100}},
                "verification_phase": {"namespace": "owned-namespace"},
            }

            def run(*arguments: str, **values: Any) -> str:
                inherited = values["pass_fds"]
                self.assertEqual(os.fstat(inherited[0]).st_ino, metadata.st_ino)
                self.assertEqual(arguments[1], "--net=/proc/self/fd/" + str(inherited[0]))
                return "observed"

            with (
                patch.object(os, "open", return_value=descriptor),
                patch.object(os, "readlink", return_value="owned-namespace"),
                patch.object(host, "run", side_effect=run),
            ):
                self.assertEqual(
                    host.api_namespace_command(state, "nft", "list", "tables"), "observed"
                )
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_a_late_health_timer_cannot_touch_an_inactive_or_different_phase(self) -> None:
        with TemporaryDirectory() as temporary:
            state = json.loads(state_file(Path(temporary)).read_text())
            for phase in (
                {"phase_id": "old", "active": False},
                {"phase_id": "new", "active": True},
            ):
                state["verification_phase"] = phase
                with patch.object(host, "run") as run:
                    host.cleanup_api_probe(state, phase_id="old")
                    run.assert_not_called()

    def test_source_terminal_result_reaches_the_watchdog_without_an_exception_heuristic(
        self,
    ) -> None:
        for event in (
            host.DATABASE_UNSAFE_EVENT,
            host.DEPLOYMENT_RECOVERY_REQUIRED_EVENT,
            host.VERIFICATION_BLOCKED_EVENT,
        ):
            with self.subTest(event=event), TemporaryDirectory() as temporary:
                state = json.loads(state_file(Path(temporary)).read_text())
                with (
                    patch.object(host, "settle_operator"),
                    patch.object(host, "run", return_value=json.dumps({"event": event})),
                ):
                    with self.assertRaises(host.RecoveryRequired):
                        host.operator(state, "verify")

    def test_prewrite_failure_is_retryable_but_dispatched_marker_failure_is_terminal(self) -> None:
        for dispatched in (False, True):
            with self.subTest(dispatched=dispatched), TemporaryDirectory() as temporary:
                root = Path(temporary)
                state = json.loads(state_file(root).read_text())
                if dispatched:
                    host.save(
                        root / "marker-transition.json",
                        {
                            "hold_id": state["hold_id"],
                            "target_id": state["target_id"],
                            "action": "remove",
                            "before": "held",
                            "desired": None,
                            "phase": "dispatching",
                        },
                    )
                with (
                    patch.object(host, "settle_operator"),
                    patch.object(host, "run", side_effect=RuntimeError("one-off failed")),
                ):
                    try:
                        host.operator(state, "unfence")
                    except host.RecoveryRequired:
                        self.assertTrue(dispatched)
                    except RuntimeError:
                        self.assertFalse(dispatched)
                    else:
                        self.fail("The failed operation must not report successful release")

    def test_watchdog_cleanup_handles_failed_inactive_and_unloaded_service_states(self) -> None:
        for active in ("failed", "inactive", "not-found"):
            with self.subTest(active=active), TemporaryDirectory() as temporary:
                root = Path(temporary)
                state = json.loads(state_file(root).read_text())
                units = root / "systemd"
                units.mkdir()
                resets = []

                def path(value: Any) -> Path:
                    original = Path(value)
                    return (
                        units / original.relative_to("/etc/systemd/system")
                        if original.is_relative_to("/etc/systemd/system")
                        else original
                    )

                def run(*arguments: str, **_values: Any) -> str:
                    if arguments[:2] == ("systemctl", "show"):
                        if arguments[2].endswith(".timer"):
                            return (
                                "LoadState="
                                + ("not-found" if active == "not-found" else "loaded")
                                + "\nActiveState=inactive\n"
                            )
                        if "--value" in arguments:
                            return "0"
                        return (
                            "MainPID=0\nLoadState="
                            + ("not-found" if active == "not-found" else "loaded")
                            + "\nActiveState="
                            + ("inactive" if active == "not-found" else active)
                        )
                    if arguments[:2] == ("systemctl", "reset-failed"):
                        if active != "failed":
                            raise RuntimeError("Unit not loaded")
                        resets.append(arguments)
                    if arguments[:2] == ("systemctl", "disable") and active == "not-found":
                        raise RuntimeError("The timer was never installed")
                    return ""

                with (
                    patch.object(host, "Path", side_effect=path),
                    patch.object(host, "run", side_effect=run),
                    patch.object(host, "sync_path"),
                ):
                    host.remove_watchdog(state)
                    host.remove_watchdog(state)
                self.assertEqual(len(resets), int(active == "failed"))
                self.assertTrue(state["watchdog_removed"])

    def test_timed_out_reader_settles_before_another_read(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = json.loads(state_file(root).read_text())
            active = False
            first = True
            observed = {
                "Id": "owned-oneoff",
                "Image": state["image_id"],
                "Config": {
                    "Labels": {
                        "com.docker.compose.project": state["project"],
                        "com.docker.compose.oneoff": "true",
                    }
                },
                "NetworkSettings": {"Networks": {"own": {"IPAddress": "10.0.1.2"}}},
            }

            def run(*arguments: str, **_values: Any) -> str:
                nonlocal active, first
                if arguments[:2] == ("docker", "compose"):
                    self.assertFalse(active)
                    active = True
                    if first:
                        first = False
                        raise subprocess.TimeoutExpired(arguments, 1)
                    active = False
                    return json.dumps({"event": "database_verified", "revision": "observed"})
                if arguments[:2] == ("docker", "ps"):
                    return observed["Id"] if active else ""
                if arguments[:2] == ("docker", "inspect"):
                    return json.dumps([observed])
                if arguments[:3] == ("docker", "rm", "-f"):
                    self.assertEqual(arguments[-1], observed["Id"])
                    active = False
                    return ""
                if arguments[:3] == ("docker", "exec", "-i"):
                    self.assertFalse(active)
                    return "0\n"
                raise AssertionError("Unexpected fixture operation")

            with patch.object(host, "run", side_effect=run):
                with self.assertRaises(subprocess.TimeoutExpired):
                    host.operator(state, "verify")
                self.assertFalse(active)
                host.operator(state, "verify")
            self.assertFalse((root / "operator-intent.json").exists())

    def test_genuine_recovery_required_returns_terminal_watchdog_status(self) -> None:
        with TemporaryDirectory() as temporary:
            path = state_file(Path(temporary))
            with (
                patch.object(sys, "argv", ["driver", "recover", "--state", str(path)]),
                patch.object(host, "run", return_value=str(os.getpid())),
                patch.object(
                    host, "release", side_effect=host.RecoveryRequired("verified state mismatch")
                ),
            ):
                self.assertEqual(host.main(), host.RECOVERY_REQUIRED_EXIT)

    def test_canceling_an_incomplete_kernel_freeze_cannot_freeze_later(self) -> None:
        with TemporaryDirectory() as temporary:
            path = state_file(Path(temporary))
            original = json.loads(path.read_text())["originals"]["own-api"]
            with patch.object(time, "monotonic", side_effect=[0, 6]):
                with self.assertRaises(TimeoutError):
                    host.set_freezer(original, True)
            host.set_freezer(original, False)
            self.assertEqual(host.read_freezer(original), ("0", "0"))

    def test_replaced_cgroup_is_never_modified(self) -> None:
        with TemporaryDirectory() as temporary:
            path = state_file(Path(temporary))
            original = json.loads(path.read_text())["originals"]["own-api"]
            original["inode"] += 1
            with self.assertRaises(RuntimeError):
                host.set_freezer(original, True)
            self.assertEqual((Path(original["cgroup"]) / "cgroup.freeze").read_text(), "0")

    def test_unsafe_committed_state_stays_held_until_exact_verification_passes(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = state_file(root, fenced=True)
            (root / "commit-pending.json").write_text("{}")
            observations = [
                {
                    "Id": identifier,
                    "State": {"Running": False},
                    "HostConfig": {"RestartPolicy": {"Name": "no"}},
                }
                for identifier in json.loads(path.read_text())["containers"]
            ]
            with (
                patch.object(host, "terminate_keeper"),
                patch.object(host, "original_containers", return_value=observations),
                patch.object(host, "operator_container", return_value=None),
                patch.object(host, "remove_watchdog"),
                patch.object(
                    host, "resume_original", side_effect=lambda identifier, _state: identifier
                ) as resume,
                patch.object(host, "operator", side_effect=host.RecoveryRequired("unsafe")),
            ):
                with self.assertRaises(RuntimeError):
                    host.release(path)
                resume.assert_not_called()
            state = json.loads(path.read_text())
            self.assertTrue(state["recovery_required"])
            self.assertFalse(state.get("released", False))
            self.assertFalse(state.get("resume_started", False))
            with (
                patch.object(host, "terminate_keeper"),
                patch.object(host, "original_containers", return_value=observations),
                patch.object(host, "operator_container", return_value=None),
                patch.object(host, "remove_watchdog"),
                patch.object(
                    host, "resume_original", side_effect=lambda identifier, _state: identifier
                ),
                patch.object(
                    host,
                    "operator",
                    return_value={"revision": "verified-revision", "outcome": "before"},
                ),
            ):
                host.release(path)
            state = json.loads(path.read_text())
            self.assertTrue(state["released"])
            self.assertEqual(state["safe_to_resume"]["revision"], state["revision"])
            self.assertEqual(
                state["safe_to_resume"]["system_identifier"], state["system_identifier"]
            )
            with self.assertRaises(RuntimeError):
                host.update_state(path, held={"stale": True})

    def test_partial_resume_keeps_safe_proof_and_never_rechecks_changed_rows(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            safe = {
                "hold_id": "synthetic",
                "system_identifier": "own-cluster",
                "image_id": "synthetic-image",
                "revision": "verified-revision",
            }
            path = state_file(root, safe_to_resume=safe, revision=safe["revision"])
            (root / "commit-pending.json").write_text("{}")
            resumed = []
            fail = True

            def resume(identifier: str, _state: dict[str, Any]) -> str:
                if identifier == "own-train" and fail:
                    raise OSError("temporarily unavailable")
                resumed.append(identifier)
                return identifier

            with (
                patch.object(host, "terminate_keeper"),
                patch.object(host, "operator_container", return_value=None),
                patch.object(host, "remove_watchdog"),
                patch.object(host, "require_database_cluster"),
                patch.object(
                    host,
                    "operator",
                    side_effect=AssertionError(
                        "A safe partial resume must not compare mutable rows again"
                    ),
                ),
                patch.object(host, "resume_original", side_effect=resume),
            ):
                with self.assertRaises(RuntimeError):
                    host.release(path)
                self.assertEqual(json.loads(path.read_text())["resumed_containers"], ["own-api"])
                fail = False
                host.release(path)
                host.release(path)
            self.assertCountEqual(resumed, ["own-api", "own-train"])
            self.assertTrue(json.loads(path.read_text())["released"])

    def test_terminal_deployment_reconciliation_after_resume_reports_actual_writer_state(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = state_file(root, fenced=True, revision="verified")
            output = StringIO()
            with (
                patch.object(host, "terminate_keeper"),
                patch.object(host, "operator_container", return_value=None),
                patch.object(host, "remove_watchdog"),
                patch.object(
                    host, "resume_original", side_effect=lambda identifier, _state: identifier
                ),
                patch.object(
                    host,
                    "operator",
                    side_effect=host.RecoveryRequired(
                        "Original marker unknown", event=host.DEPLOYMENT_RECOVERY_REQUIRED_EVENT
                    ),
                ),
                redirect_stdout(output),
            ):
                with self.assertRaises(host.RecoveryRequired):
                    host.release(path)
            event = json.loads(output.getvalue().splitlines()[-1])
            self.assertEqual(event["event"], host.DEPLOYMENT_RECOVERY_REQUIRED_EVENT)
            self.assertIs(event["writers_held"], False)
            self.assertTrue(json.loads(path.read_text())["recovery_required"])
            self.assertFalse(json.loads(path.read_text()).get("released", False))

    def test_changed_live_cluster_cannot_reuse_a_sticky_resume_proof(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            safe = {
                "hold_id": "synthetic",
                "system_identifier": "own-cluster",
                "image_id": "synthetic-image",
                "revision": "verified",
            }
            path = state_file(root, safe_to_resume=safe, revision=safe["revision"])
            (root / "commit-pending.json").write_text("{}")
            with (
                patch.object(host, "terminate_keeper"),
                patch.object(host, "read_database", return_value="another-cluster"),
                patch.object(host, "resume_original") as resume,
            ):
                with self.assertRaises(host.RecoveryRequired):
                    host.release(path)
                resume.assert_not_called()

    def test_release_settles_a_started_original_after_crash_before_progress_save(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            safe = {
                "hold_id": "synthetic",
                "system_identifier": "own-cluster",
                "image_id": "synthetic-image",
                "revision": "verified-revision",
            }
            path = state_file(
                root,
                safe_to_resume=safe,
                revision=safe["revision"],
                resume_started=True,
                freeze_requested=["own-api", "own-train"],
            )
            (root / "commit-pending.json").write_text("{}")
            state = json.loads(path.read_text())
            containers = {
                identifier: {
                    "Id": identifier,
                    "Image": state["image_id"],
                    "Config": {"Labels": {"com.docker.compose.project": state["project"]}},
                    "State": {
                        "Running": True,
                        "Pid": original["pid"] + 100,
                        "StartedAt": "recovered-start",
                    },
                    "HostConfig": {"RestartPolicy": {"Name": "no", "MaximumRetryCount": 0}},
                }
                for identifier, original in state["originals"].items()
            }

            def run(*arguments: str, **_values: Any) -> str:
                if arguments[:2] != ("docker", "update"):
                    raise AssertionError("A healthy recovered original must not be started again")
                containers[arguments[-1]]["HostConfig"]["RestartPolicy"] = dict(
                    state["originals"][arguments[-1]]["restart_policy"]
                )
                return ""

            with (
                patch.object(host, "terminate_keeper"),
                patch.object(host, "operator_container", return_value=None),
                patch.object(host, "remove_watchdog"),
                patch.object(host, "require_database_cluster"),
                patch.object(host, "run", side_effect=run),
                patch.object(
                    host, "inspect_container", side_effect=lambda identifier: containers[identifier]
                ),
                patch.object(
                    host,
                    "freezer_identity",
                    side_effect=lambda observed: state["originals"][observed["Id"]],
                ),
                patch.object(time, "sleep"),
            ):
                host.release(path)
            self.assertTrue(json.loads(path.read_text())["released"])
            for identifier, observed in containers.items():
                self.assertEqual(
                    observed["HostConfig"]["RestartPolicy"],
                    state["originals"][identifier]["restart_policy"],
                )
                self.assertTrue(observed["State"]["Running"])
                self.assertEqual(host.read_freezer(state["originals"][identifier]), ("0", "0"))

    def test_no_commit_release_settles_the_already_started_exact_original(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = json.loads(
                state_file(root, resume_started=True, freeze_requested=["own-api"]).read_text()
            )
            observed = {
                "Id": "own-api",
                "Image": state["image_id"],
                "Config": {"Labels": {"com.docker.compose.project": state["project"]}},
                "State": {"Running": True, "Pid": 100, "StartedAt": "recovered"},
                "HostConfig": {"RestartPolicy": state["originals"]["own-api"]["restart_policy"]},
            }
            with (
                patch.object(host, "inspect_container", return_value=observed),
                patch.object(host, "freezer_identity", return_value={}) as qualify,
                patch.object(host, "run") as run,
                patch.object(time, "sleep"),
            ):
                self.assertEqual(host.resume_original("own-api", state), "own-api")
                qualify.assert_called_once_with(observed)
                self.assertEqual(run.call_args.args[:2], ("docker", "update"))
                (root / "commit-pending.json").write_text("{}")
                with self.assertRaises(RuntimeError):
                    host.resume_original("own-api", state)

    def test_uncertain_keeper_termination_never_reopens_a_pending_commit(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = state_file(root)
            (root / "commit-pending.json").write_text("{}")
            with (
                patch.object(host, "terminate_keeper", side_effect=OSError("database unavailable")),
                patch.object(host, "resume_original") as resume,
                patch.object(host, "operator") as operator,
            ):
                with self.assertRaises(OSError):
                    host.release(path)
                resume.assert_not_called()
                operator.assert_not_called()
            self.assertFalse(json.loads(path.read_text()).get("resume_started", False))

    def test_failed_controller_start_still_invokes_supported_release(self) -> None:
        with TemporaryDirectory() as temporary:
            path = state_file(Path(temporary))
            with (
                patch.object(host, "install_watchdog"),
                patch.object(host, "start_process", side_effect=RuntimeError("controller refused")),
                patch.object(host, "request_release") as release,
            ):
                with self.assertRaises(RuntimeError):
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
                with self.assertRaises(ValueError):
                    host.submit(path, receipt_path)
            open_fifo.assert_not_called()
            receipt["backup_sha256"] = "a" * 64
            receipt_path.write_text(json.dumps(receipt))
            received = []
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

    def test_bound_database_server_is_custody_but_other_password_copies_refuse(self) -> None:
        with TemporaryDirectory() as temporary:
            state = json.loads(state_file(Path(temporary)).read_text())
            items = {}
            for identifier in (
                *state["containers"],
                state["browser"],
                state["postgres"],
                "outside",
            ):
                items[identifier] = {
                    "Id": identifier,
                    "Name": "/" + identifier,
                    "Image": state["image_id"]
                    if identifier in state["containers"]
                    else "other-image",
                    "Config": {
                        "Labels": {"com.docker.compose.project": state["project"]},
                        "Env": [
                            "LAUNCHPLANE_DATABASE_URL=postgresql://fixture:private-custody@own/db"
                        ]
                        if identifier in state["containers"]
                        else [],
                    },
                }
            items[state["postgres"]]["Config"]["Env"] = ["POSTGRES_PASSWORD=private-custody"]
            items["outside"]["Config"]["Env"] = [
                "DATABASE_URL=postgresql://fixture:private-custody@own/db"
            ]
            outside_present = False

            def run(*arguments: str, **_values: Any) -> str:
                if arguments[:2] == ("docker", "inspect"):
                    return json.dumps([items[identifier] for identifier in arguments[2:]])
                if arguments[:2] == ("docker", "ps"):
                    if "--filter" in arguments:
                        return " ".join([*state["containers"], state["browser"]])
                    return " ".join(
                        identifier
                        for identifier in items
                        if identifier != "outside" or outside_present
                    )
                raise AssertionError("Unexpected fixture operation")

            with (
                patch.object(
                    host, "inspect_container", side_effect=lambda identifier: items[identifier]
                ),
                patch.object(host, "run", side_effect=run),
            ):
                host.check_originals(state)
                outside_present = True
                with self.assertRaises(RuntimeError):
                    host.check_originals(state)

    def test_wrong_project_is_refused_before_runtime_changes(self) -> None:
        with TemporaryDirectory() as temporary:
            path = state_file(Path(temporary))
            with (
                patch.object(
                    host,
                    "inspect_container",
                    return_value={"Config": {"Labels": {"com.docker.compose.project": "another"}}},
                ),
                patch.object(host, "run") as run,
            ):
                with self.assertRaises(RuntimeError):
                    host.check_originals(json.loads(path.read_text()))
            run.assert_not_called()

    def test_dormant_writer_outside_the_project_refuses_before_freeze(self) -> None:
        database = "postgresql://synthetic:credential@own/database"
        api = {
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
        outside["Id"] = "outside"
        outside["Image"] = "foreign-image"
        outside["Config"]["Labels"]["org.opencontainers.image.source"] = "foreign-source"

        def inventory(*arguments: str, **_values: Any) -> str:
            if arguments[:2] == ("docker", "ps"):
                return "" if "--filter" in arguments else "outside\n"
            if arguments[:2] == ("docker", "inspect"):
                return json.dumps([outside])
            raise AssertionError("Unexpected fixture operation")

        for url in (
            database,
            "postgresql+psycopg://synthetic:cred%65ntial@alias/another?application_name=fixture",
        ):
            outside["Config"]["Env"] = ["LAUNCHPLANE_DATABASE_URL=" + url]
            for key in ("LAUNCHPLANE_DATABASE_URL", "DATABASE_URL", "PGPASSWORD"):
                outside["Config"]["Env"] = [
                    key + "=" + ("credential" if key == "PGPASSWORD" else url)
                ]
                with patch.object(host, "run", side_effect=inventory):
                    with self.assertRaises(RuntimeError):
                        host.require_no_outside_writers(
                            api, {"original"}, project="own", operators=set()
                        )
        outside["Config"]["Env"] = [
            "LAUNCHPLANE_DATABASE_URL=postgresql://isolated:other@restore/db"
        ]
        outside["Image"] = api["Image"]
        with patch.object(host, "run", side_effect=inventory):
            host.require_no_outside_writers(api, {"original"}, project="own", operators=set())
        outside["Config"]["Env"] = []
        with patch.object(host, "run", side_effect=inventory):
            with self.assertRaises(RuntimeError):
                host.require_no_outside_writers(api, {"original"}, project="own", operators=set())
