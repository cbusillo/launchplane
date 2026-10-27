from datetime import datetime, timedelta, timezone
import json
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
    def test_timed_out_oneoffs_settle_before_reads_and_never_replay_uncertain_cleanup(self) -> None:
        for mode in ("verify", "unfence"):
            with self.subTest(mode=mode), TemporaryDirectory() as temporary:
                root = Path(temporary)
                path = state_file(root)
                state = json.loads(path.read_text())
                active = False
                first = True
                marker_present = True
                writes = []
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
                        active = True
                        if "unfence" in arguments and "--readback-only" not in arguments:
                            writes.append(arguments)
                        if first:
                            first = False
                            raise subprocess.TimeoutExpired(arguments, 1)
                        if mode == "unfence" and marker_present:
                            raise RuntimeError("cleanup outcome remains unknown")
                        active = False
                        return json.dumps(
                            {
                                "event": "deployment_released"
                                if mode == "unfence"
                                else "database_verified",
                                "revision": "observed",
                            }
                        )
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
                        host.operator(state, mode)
                    self.assertFalse(active)
                    if mode == "unfence":
                        self.assertTrue((root / "marker-cleanup-uncertain.json").exists())
                        with self.assertRaises(RuntimeError):
                            host.operator(state, mode)
                        self.assertFalse(active)
                        marker_present = False
                    host.operator(state, mode)
                self.assertEqual(len(writes), int(mode == "unfence"))
                self.assertFalse((root / "operator-intent.json").exists())
                self.assertFalse((root / "marker-cleanup-uncertain.json").exists())

    def test_genuine_recovery_required_returns_terminal_watchdog_status(self) -> None:
        with TemporaryDirectory() as temporary:
            path = state_file(Path(temporary))
            with (
                patch.object(sys, "argv", ["driver", "release", "--state", str(path)]),
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
                patch.object(host, "operator", side_effect=RuntimeError("unsafe")),
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
                patch.object(host, "operator", return_value={"revision": "verified-revision"}),
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
                patch.object(host, "release") as release,
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
        with (
            patch.object(host, "run", return_value="original\noutside\n"),
            patch.object(host, "inspect_container", return_value=outside),
        ):
            with self.assertRaises(RuntimeError):
                host.require_no_outside_writers(api, {"original"}, project="own", operators=set())
        outside["Config"]["Env"] = ["LAUNCHPLANE_DATABASE_URL=postgresql://isolated/restore"]
        with (
            patch.object(host, "run", return_value="outside\n"),
            patch.object(host, "inspect_container", return_value=outside),
        ):
            host.require_no_outside_writers(api, {"original"}, project="own", operators=set())
