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
    def setUp(self) -> None:
        self.socket_check = patch.object(host, "require_verification_socket")
        self.socket_check.start()
        self.addCleanup(self.socket_check.stop)
        self.client_check = patch.object(host, "require_no_api_database_clients")
        self.client_check.start()
        self.addCleanup(self.client_check.stop)

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
                    host,
                    "operator",
                    return_value={
                        "revision": "verified",
                        "outcome": "before",
                        "health_passed": True,
                    },
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
                    return_value={
                        "revision": "verified-revision",
                        "outcome": "before",
                        "health_passed": True,
                    },
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
                stop_requested=["own-api", "own-train"],
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

    def test_no_commit_release_settles_the_already_started_exact_original(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = json.loads(
                state_file(root, resume_started=True, stop_requested=["own-api"]).read_text()
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
                patch.object(host, "run") as run,
                patch.object(time, "sleep"),
            ):
                self.assertEqual(host.resume_original("own-api", state), "own-api")
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
