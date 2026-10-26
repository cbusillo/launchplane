from contextlib import redirect_stdout
from collections.abc import Callable
import io
import os
import json
import subprocess
import hashlib
from tempfile import TemporaryDirectory
from pathlib import Path
from typing import Any
import unittest
from unittest.mock import Mock, patch

from control_plane.dokploy.cutover_restore_probe import (
    RESTORE_PROBE_PROGRAM,
    RESTORE_PREFLIGHT_PROGRAM,
    verify_disposable_database_restore,
)
from control_plane.dokploy.cutover_restore_state import (
    RESTORE_CANCEL_PROGRAM,
    RESTORE_STATE_PROGRAM,
)
from control_plane.odoo_cutover_recovery import BackupVerificationRejected


class CutoverRestoreProbeTests(unittest.TestCase):
    def test_missing_restore_image_is_acquired_and_qualified_before_use(self) -> None:
        for failure in ("", "pull", "version"):
            with self.subTest(failure=failure):
                local = False
                actions = []

                def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
                    nonlocal local
                    actions.append(argv[1])
                    if argv[1:3] == ["image", "inspect"]:
                        return subprocess.CompletedProcess(argv, 0 if local else 1, stdout=b"[{}]")
                    if argv[1] == "pull":
                        local = True
                        return subprocess.CompletedProcess(argv, int(failure == "pull"))
                    self.assertTrue(local)
                    version = 18 if failure == "version" else 17
                    return subprocess.CompletedProcess(
                        argv, 0, stdout=f"pg_restore (PostgreSQL) {version}.1\n".encode()
                    )

                output = io.StringIO()
                with (
                    patch("subprocess.run", side_effect=run),
                    patch(
                        "sys.stdin",
                        io.StringIO(json.dumps({"image": self.request["image"], "dump_major": 17})),
                    ),
                    redirect_stdout(output),
                ):
                    if failure:
                        with self.assertRaises(ValueError):
                            exec(RESTORE_PREFLIGHT_PROGRAM, {})
                    else:
                        exec(RESTORE_PREFLIGHT_PROGRAM, {})
                        self.assertEqual(json.loads(output.getvalue()), {"restore_tools": True})
                self.assertIn("pull", actions)
                if failure == "pull":
                    self.assertNotIn("run", actions)

    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.request = dict(
            authority_directory=str(Path(temporary.name).resolve()),
            capture_nonce="f" * 64,
            record="original-cutover",
            image="postgres@sha256:" + "a" * 64,
            runner="runner",
            dump="/backup/example.dump",
            database="example",
            database_uuid="original-database",
            database_dump_sha256="b" * 64,
            filestore="/backup/example.tar.gz",
            filestore_sha256="c" * 64,
            timeout_seconds=1800,
        )
        self.commands: list[tuple[list[str], dict[str, Any]]] = []
        self.attachment_count = 10000
        self.checksum = "d" * 40
        self.container = "e" * 64
        self.clock = 0.0

    def run_probe(
        self,
        *,
        failure: str = "",
        cancel: bool = False,
        intercept: Callable[[list[str]], None] | None = None,
    ) -> dict[str, Any]:
        request = {**self.request, "record": str(self.request["record"]) + failure}
        binding = {
            key: request[key]
            for key in (
                "record",
                "image",
                "database",
                "database_uuid",
                "database_dump_sha256",
                "filestore_sha256",
                "capture_nonce",
            )
        }
        digest = hashlib.sha256(
            json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        live = failure == "live"

        def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
            nonlocal live
            self.commands.append((argv, kwargs))
            if intercept is not None:
                intercept(argv)
            output = b""
            status = 0
            if argv[1] == "ps" and live:
                output = self.container.encode()
            elif argv[1] == "inspect":
                output = json.dumps(
                    [
                        {
                            "Config": {
                                "Image": self.request["image"],
                                "Labels": {"launchplane.cutover-restore-request": digest},
                                "Entrypoint": ["timeout"],
                                "User": "postgres",
                                "Cmd": [
                                    "--signal=KILL",
                                    "1700",
                                    "docker-entrypoint.sh",
                                    "postgres",
                                ],
                            },
                            "HostConfig": {
                                "NetworkMode": "none",
                                "AutoRemove": True,
                                "ReadonlyRootfs": True,
                                "Privileged": False,
                                "CapDrop": ["ALL"],
                                "SecurityOpt": ["no-new-privileges:true"],
                            },
                            "Mounts": [
                                {
                                    "Type": "volume",
                                    "Name": "a" * 64,
                                    "Destination": "/var/lib/postgresql/data",
                                }
                            ],
                        }
                    ]
                ).encode()
            elif argv[1] == "run":
                output = self.container.encode()
                if failure == "allocation":
                    status = 1
                elif failure == "allocation-timeout":
                    raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            elif "pg_database_size" in argv[-1]:
                output = b"100000"
            elif "df" in argv:
                output = b"Filesystem 1-blocks Used Available Capacity Mounted\nx 100000000 100 99999900 1% /data"
                if failure == "space":
                    output = (
                        b"Filesystem 1-blocks Used Available Capacity Mounted\nx 100 99 1 99% /data"
                    )
            elif "sha256sum" in argv:
                field = (
                    "database_dump_sha256"
                    if argv[-1] == self.request["dump"]
                    else "filestore_sha256"
                )
                output = (str(self.request[field]) + "  artifact").encode()
            elif argv[-1].startswith("SELECT store_fname"):
                output = (
                    ("dd/" + self.checksum + "|" + self.checksum + "\n") * self.attachment_count
                ).encode()
            elif argv[-1].startswith("SELECT value"):
                output = str(self.request["database_uuid"]).encode()
            elif "sha1sum" in argv and failure == "attachment":
                status = 1
            elif "pg_restore" in argv:
                self.clock += 30
                if failure == "timeout":
                    raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            elif argv[1] == "rm" and failure == "cleanup":
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            if argv[1] == "rm":
                live = False
            return subprocess.CompletedProcess(argv, status, stdout=output)

        source = Mock(stdout=io.BytesIO(b"archive"))
        source.wait.return_value = 0
        source.poll.return_value = 0
        output = io.StringIO()
        with (
            patch("sys.stdin", io.StringIO(json.dumps(request))),
            patch("subprocess.run", side_effect=run),
            patch("subprocess.Popen", return_value=source),
            patch("time.monotonic", side_effect=lambda: self.clock),
            redirect_stdout(output),
        ):
            try:
                exec(RESTORE_CANCEL_PROGRAM if cancel else RESTORE_PROBE_PROGRAM, {})
            except SystemExit as ended:
                self.assertEqual(ended.code, 0)
        return json.loads(output.getvalue())  # type: ignore[no-any-return]

    def test_restore_uses_one_deadline_and_one_attachment_batch_with_owned_cleanup(self) -> None:
        result = self.run_probe()
        self.assertEqual(result["restored_database_uuid"], self.request["database_uuid"])
        checks = [(argv, kwargs) for argv, kwargs in self.commands if "sha1sum" in argv]
        self.assertEqual(len(checks), 1)
        self.assertEqual(len(checks[0][1]["input"].splitlines()), self.attachment_count)
        restore = next(kwargs for argv, kwargs in self.commands if "pg_restore" in argv)
        archive = next(kwargs for argv, kwargs in self.commands if "tar" in argv)
        self.assertLess(archive["timeout"], restore["timeout"])
        self.assertLess(restore["timeout"], self.request["timeout_seconds"])
        self.assertTrue(
            all(
                0 < kwargs["timeout"] <= self.request["timeout_seconds"]
                for _, kwargs in self.commands
            )
        )
        self.assertIn(
            ["docker", "rm", "-f", "-v", self.container], [argv for argv, _ in self.commands]
        )

    def test_space_hash_and_timeout_failures_are_definite_only_after_verified_cleanup(self) -> None:
        for failure in ("space", "attachment", "timeout"):
            with self.subTest(failure=failure):
                self.commands.clear()
                result = self.run_probe(failure=failure)
                self.assertEqual(result, {"status": "failed", "terminated": True})
                self.assertIn(
                    ["docker", "rm", "-f", "-v", self.container],
                    [argv for argv, _ in self.commands],
                )
                if failure == "space":
                    self.assertFalse(any("pg_restore" in argv for argv, _ in self.commands))
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_probe(failure="cleanup")

    def test_live_original_restore_is_cancelled_without_a_second_copy(self) -> None:
        result = self.run_probe(failure="live")
        self.assertEqual(result, {"status": "failed", "terminated": True})
        self.assertFalse(any(argv[1] == "run" for argv, _ in self.commands))
        self.assertIn(
            ["docker", "rm", "-f", "-v", self.container], [argv for argv, _ in self.commands]
        )

    def test_completed_original_restore_is_read_back_without_another_copy(self) -> None:
        first = self.run_probe()
        self.commands.clear()
        self.assertEqual(self.run_probe(), first)
        self.assertEqual(self.commands, [])
        self.request["capture_nonce"] = "e" * 64
        with self.assertRaises(ValueError):
            self.run_probe()

    def test_lost_intent_claim_cannot_allocate_after_original_completion(self) -> None:
        original = os.link
        raced = False

        def finish_original(source: str, destination: Path) -> None:
            nonlocal raced
            if str(destination).endswith("-restore-intent.json") and not raced:
                raced = True
                intent = json.loads(Path(source).read_text())
                original(source, destination)
                destination.with_name(
                    str(self.request["record"]) + "-restore-completed.json"
                ).write_text(
                    json.dumps(
                        {
                            "request": intent["request"],
                            "result": {"status": "failed", "terminated": True},
                        }
                    )
                )
                raise FileExistsError()
            original(source, destination)

        with patch("os.link", side_effect=finish_original), self.assertRaises(RuntimeError):
            self.run_probe()
        self.assertFalse(any(argv[1] == "run" for argv, _ in self.commands))
        self.commands.clear()
        self.assertEqual(self.run_probe(), {"status": "failed", "terminated": True})
        self.assertEqual(self.commands, [])

    def test_cancellation_seals_unused_restore_and_prevents_later_allocation(self) -> None:
        self.assertEqual(self.run_probe(cancel=True), {"cancelled": True})
        self.commands.clear()
        self.assertEqual(
            self.run_probe(), {"status": "abandoned", "allocation_state": "not_dispatched"}
        )
        self.assertFalse(any(argv[1] == "run" for argv, _ in self.commands))

    def test_rejected_allocation_is_definite_after_native_absence_readback(self) -> None:
        self.assertEqual(
            self.run_probe(failure="allocation"), {"status": "failed", "terminated": True}
        )
        self.commands.clear()
        self.assertEqual(self.run_probe(failure="allocation", cancel=True), {"cancelled": True})
        self.assertEqual(self.commands, [])

    def receipt(self, kind: str, *, failure: str = "") -> Path:
        return Path(str(self.request["authority_directory"])) / (
            str(self.request["record"]) + failure + "-restore-" + kind + ".json"
        )

    def seal(self, kind: str, *, failure: str = "", **values: Any) -> None:
        namespace: dict[str, Any] = {}
        with patch(
            "sys.stdin",
            io.StringIO(
                json.dumps({**self.request, "record": str(self.request["record"]) + failure})
            ),
        ):
            exec(RESTORE_STATE_PROGRAM, namespace)
        namespace["seal_receipt"](kind, **values)

    def test_coordinator_death_before_dispatch_cancellation_fences_late_allocation(self) -> None:
        def stop_before_dispatch(argv: list[str]) -> None:
            if "pg_database_size" in argv[-1]:
                raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            self.run_probe(intercept=stop_before_dispatch)
        self.assertFalse(self.receipt("dispatch").exists())
        self.assertEqual(self.run_probe(cancel=True), {"cancelled": True})
        self.commands.clear()
        self.assertEqual(
            self.run_probe(), {"status": "abandoned", "allocation_state": "not_dispatched"}
        )
        self.assertFalse(any(argv[1] == "run" for argv, _ in self.commands))
        self.assertFalse(self.receipt("completed").exists())

    def test_unknown_allocation_can_be_abandoned_without_claiming_termination(self) -> None:
        failure = "allocation-timeout"
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_probe(failure=failure)
        self.assertEqual(self.run_probe(failure=failure, cancel=True), {"cancelled": True})
        self.commands.clear()
        self.assertEqual(
            self.run_probe(failure=failure),
            {"status": "abandoned", "allocation_state": "unknown_isolated"},
        )
        self.assertFalse(self.receipt("completed", failure=failure).exists())
        self.assertFalse(any(argv[1] == "run" for argv, _ in self.commands))

    def test_delayed_allocation_after_cancellation_is_removed_and_never_verified(self) -> None:
        def cancel_during_dispatch(argv: list[str]) -> None:
            if argv[1] == "run":
                self.assertEqual(self.run_probe(cancel=True), {"cancelled": True})

        result = self.run_probe(intercept=cancel_during_dispatch)
        self.assertEqual(result, {"status": "abandoned", "allocation_state": "unknown_isolated"})
        self.assertEqual(sum(argv[1] == "run" for argv, _ in self.commands), 1)
        self.assertIn(
            ["docker", "rm", "-f", "-v", self.container], [argv for argv, _ in self.commands]
        )
        self.assertFalse(any("pg_restore" in argv for argv, _ in self.commands))
        self.assertFalse(self.receipt("completed").exists())

    def test_late_success_receipt_cannot_override_abandonment_or_pass_backup_verification(
        self,
    ) -> None:
        failure = "allocation-timeout"
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_probe(failure=failure)
        self.run_probe(failure=failure, cancel=True)
        success = {
            "restored_database_uuid": self.request["database_uuid"],
            "database_dump_sha256": self.request["database_dump_sha256"],
            "filestore_archive_sha256": self.request["filestore_sha256"],
        }
        self.seal("completed", failure=failure, result=success)
        outcome = self.run_probe(failure=failure)
        self.assertEqual(outcome, {"status": "abandoned", "allocation_state": "unknown_isolated"})
        with self.assertRaises(BackupVerificationRejected):
            verify_disposable_database_restore(
                lambda argv, stdin: json.dumps(outcome),
                image=str(self.request["image"]),
                runner=str(self.request["runner"]),
                record_id=str(self.request["record"]) + failure,
                dump=str(self.request["dump"]),
                database_uuid=str(self.request["database_uuid"]),
                dump_sha256=str(self.request["database_dump_sha256"]),
                database=str(self.request["database"]),
                filestore=str(self.request["filestore"]),
                filestore_sha256=str(self.request["filestore_sha256"]),
                timeout_seconds=int(str(self.request["timeout_seconds"])),
                authority_directory=str(self.request["authority_directory"]),
                capture_nonce=str(self.request["capture_nonce"]),
            )

    def test_legacy_intent_without_dispatch_protocol_retains_unknown_allocation(self) -> None:
        self.seal("intent")
        for cancel in (False, True):
            with self.subTest(cancel=cancel), self.assertRaises(RuntimeError):
                self.run_probe(cancel=cancel)
        self.assertFalse(self.receipt("cancelled").exists())
        self.assertFalse(self.receipt("completed").exists())
        self.assertFalse(any(argv[1] == "run" for argv, _ in self.commands))


class NativeRestoreLifetimeTests(unittest.TestCase):
    def test_coordinator_death_cannot_retain_restore_volume(self) -> None:
        self.check_lifetime(cancel=False)

    def test_original_reconciliation_cancels_owned_orphan_and_removes_its_volume(self) -> None:
        self.check_lifetime(cancel=True)

    def check_lifetime(self, *, cancel: bool) -> None:
        import os
        from pathlib import Path
        import sys
        from tempfile import TemporaryDirectory
        import time

        image = os.environ.get("LAUNCHPLANE_TEST_RESTORE_IMAGE")
        if not image:
            self.skipTest("select an already-local immutable PostgreSQL restore image")
        from control_plane.dokploy.cutover_restore_probe import RESTORE_PREFLIGHT_PROGRAM

        preflight = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--pull",
                "never",
                "--network",
                "none",
                "--entrypoint",
                "pg_dump",
                image,
                "--version",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        major = int(preflight.stdout.split()[2].split(".")[0])
        subprocess.run(
            [sys.executable, "-c", RESTORE_PREFLIGHT_PROGRAM],
            input=json.dumps({"image": image, "dump_major": major}),
            text=True,
            capture_output=True,
            check=True,
            timeout=40,
        )
        with TemporaryDirectory() as temporary:
            marker = Path(temporary) / "allocated.json"
            spec = {
                "image": image,
                "record": "native-lifetime",
                "runner": "inert-source",
                "database": "example",
                "timeout_seconds": 20,
                "authority_directory": str(marker.parent.resolve()),
                "database_uuid": "native-uuid",
                "filestore_sha256": "b" * 64,
                "capture_nonce": "c" * 64,
                "dump": "inert.dump",
                "database_dump_sha256": "a" * 64,
            }
            harness = """
import json,subprocess,sys,time
from pathlib import Path
from control_plane.dokploy.cutover_restore_probe import RESTORE_PROBE_PROGRAM
original=subprocess.run
marker=Path(sys.argv[1])
def run(argv,**kwargs):
    if argv[1:4]==['exec','-e','DATABASE_NAME=example']:
        return subprocess.CompletedProcess(argv,0,stdout=b'1024')
    if 'sha256sum' in argv:
        value=json.loads(original(['docker','inspect',container],capture_output=True,check=True).stdout)[0]
        marker.write_text(json.dumps({'container':container,'volumes':[m['Name'] for m in value['Mounts'] if m['Type']=='volume']}))
        time.sleep(120)
    return original(argv,**kwargs)
subprocess.run=run
exec(RESTORE_PROBE_PROGRAM,globals())
"""
            child = subprocess.Popen(
                [sys.executable, "-c", harness, str(marker)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.addCleanup(lambda: child.kill() if child.poll() is None else None)
            assert child.stdin is not None
            child.stdin.write(json.dumps(spec))
            child.stdin.close()
            deadline = time.monotonic() + 35
            while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.1)
            self.assertTrue(
                marker.exists(),
                child.stderr.read()
                if child.poll() is not None and child.stderr
                else "restore did not reach native allocation",
            )
            allocation = json.loads(marker.read_text())
            child.kill()
            child.wait(timeout=5)
            if cancel:
                from control_plane.dokploy.cutover_restore_state import RESTORE_CANCEL_PROGRAM

                cancelled = subprocess.run(
                    [sys.executable, "-c", RESTORE_CANCEL_PROGRAM],
                    input=json.dumps(spec),
                    text=True,
                    capture_output=True,
                    check=True,
                    timeout=10,
                )
                self.assertEqual(json.loads(cancelled.stdout), {"cancelled": True})
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                current = subprocess.run(
                    [
                        "docker",
                        "ps",
                        "-a",
                        "--no-trunc",
                        "--filter",
                        "id=" + allocation["container"],
                        "--format",
                        "{{.ID}}",
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=5,
                )
                if not current.stdout.strip():
                    break
                time.sleep(0.25)
            self.assertFalse(current.stdout.strip())
            for volume in allocation["volumes"]:
                observed = subprocess.run(
                    [
                        "docker",
                        "volume",
                        "ls",
                        "--filter",
                        "name=^" + volume + "$",
                        "--format",
                        "{{.Name}}",
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=5,
                )
                self.assertFalse(observed.stdout.strip())
            if child.stdout:
                child.stdout.close()
            if child.stderr:
                child.stderr.close()
