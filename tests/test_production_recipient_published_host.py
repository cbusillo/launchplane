"""Execute the fixed published capture program against isolated local fixtures."""

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, cast
import unittest
from unittest.mock import patch

from control_plane.production_recipient_published_host import PUBLISHED_RECOVERY_LIBRARY
from control_plane.production_recipient_host import RECOVERY_LIBRARY
from control_plane.production_recipient_resources import RESOURCE_LIBRARY
from control_plane.dokploy.post_deploy import extract_odoo_post_deploy_readback_markers
from control_plane.dokploy.api import JsonObject
from tests import test_production_recipient_mariadb as mariadb_tests


class PublishedHostTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.paths = {role: self.root / role for role in ("database", "config")}
        for path in self.paths.values():
            path.mkdir()
        (self.paths["config"] / "settings").write_text("verified configuration")
        self.namespace: dict[str, Any] = {}
        exec(
            compile(
                RESOURCE_LIBRARY + RECOVERY_LIBRARY + PUBLISHED_RECOVERY_LIBRARY,
                "<published-recovery>",
                "exec",
            ),
            self.namespace,
        )
        self.spec = {
            "recovery_binding": "a" * 64,
            "operation": "d" * 64,
            "database_image": "mariadb@sha256:" + "b" * 64,
        }
        self.network = "none"
        self.mount = ""
        self.containers: set[str] = set()
        self.roundtrip = (
            b"CREATE DATABASE fixture; INSERT INTO fixture.items VALUES ('business-row');\n"
        )
        self.identity = "c" * 64
        self.dumps: list[bool] = []
        self.namespace["published_source"] = lambda spec: (self.paths, "live-source", self.identity)
        self.namespace["populated_rows"] = lambda identifier: None
        self.namespace["logical_dump"] = self.dump
        self.namespace["native_capture_status"] = lambda _native: ["done", "0"]
        self.namespace["docker"] = self.docker
        self.calls: list[tuple[str, ...]] = []
        self.addCleanup(patch.stopall)
        patch(
            "subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")
        ).start()
        patch("shutil.disk_usage", return_value=SimpleNamespace(free=100 * 1024**3)).start()

    def dump(self, _identifier: str, path: Path, *, source: bool, **_kwargs: Any) -> None:
        self.dumps.append(source)
        path.write_bytes(
            self.roundtrip
            if not source
            else b"CREATE DATABASE fixture; INSERT INTO fixture.items VALUES ('business-row');\n"
        )

    def docker(self, *args: str) -> str:
        self.calls.append(args)
        if args[0] == "create":
            self.mount = next(value for value in args if value.startswith("type=bind,src="))
            self.containers.add("e" * 64)
            return "e" * 64
        if args[0] == "rm":
            self.containers.remove(args[2])
            return ""
        if args[0] == "ps":
            return "\n".join(sorted(self.containers))
        if args[:2] == ("image", "inspect"):
            return json.dumps([{"Config": {"Env": []}}])
        if args[0] == "inspect":
            return json.dumps(
                [
                    {
                        "Id": "e" * 64,
                        "Config": {
                            "Image": self.spec["database_image"],
                            "Labels": {
                                "io.launchplane.recipient-recovery": self.spec["recovery_binding"],
                                "io.launchplane.recipient-recovery-operation": self.spec[
                                    "operation"
                                ],
                            },
                            "Env": [],
                        },
                        "State": {"Running": False},
                        "HostConfig": {
                            "NetworkMode": self.network,
                            "PortBindings": {},
                            "RestartPolicy": {"Name": "no"},
                        },
                        "Mounts": [
                            {
                                "Type": "bind",
                                "Source": self.mount.split("src=", 1)[1].split(",", 1)[0],
                                "Destination": "/var/lib/mysql",
                            }
                        ],
                    }
                ]
            )
        return ""

    def recover(self, *, inspect: bool = False) -> str:
        output = io.StringIO()
        with redirect_stdout(output):
            self.namespace["published_recovery"](
                self.spec | {"inspect": inspect}, base=self.root / "captures"
            )
        return output.getvalue()

    def test_roundtrip_and_terminal_inspection_never_recapture_or_touch_source(self) -> None:
        result = self.recover()
        self.assertIn("recipient_recovery_ok=true", result)
        self.assertIn("recipient_recovery_empty=false", result)
        parsed = extract_odoo_post_deploy_readback_markers(
            cast(JsonObject, {"logs": result.splitlines()})
        )
        self.assertEqual(parsed["recipient_recovery_source"], self.identity)
        self.assertIn("recipient_recovery_data", parsed)
        self.assertIn("recipient_recovery_duration_ms", parsed)
        self.assertEqual(self.dumps, [True, False])
        attempt = next((self.root / "captures" / self.spec["recovery_binding"]).glob("attempt-*"))
        self.assertFalse((attempt / "capture.tar").exists())
        self.assertFalse((attempt / "database.sql").exists())
        self.assertFalse((attempt / "roundtrip.sql").exists())
        self.assertEqual(self.recover(inspect=True), result)
        self.assertEqual(self.dumps, [True, False])
        self.assertFalse(any(call[0] in {"stop", "restart", "pause"} for call in self.calls))

    def test_changed_restored_data_or_outbound_network_never_qualifies(self) -> None:
        for attr, value in (("roundtrip", b"empty substituted database"), ("network", "bridge")):
            with self.subTest(attr=attr):
                self.setUp()
                setattr(self, attr, value)
                with self.assertRaises(ValueError):
                    self.recover()
                outcome = json.loads(
                    (
                        self.root / "captures" / self.spec["recovery_binding"] / "outcome.json"
                    ).read_text()
                )
                self.assertEqual(outcome["state"], "failed_safe")
                self.assertIn(("rm", "-f", "e" * 64), self.calls)
                self.assertFalse(self.containers)
                attempt = (
                    self.root / "captures" / self.spec["recovery_binding"] / outcome["attempt"]
                )
                for name in ("database.sql", "roundtrip.sql", "capture.tar", "restore"):
                    self.assertFalse((attempt / name).exists())

    def test_cleanup_uncertainty_retains_known_scratch_without_repeating_removal(self) -> None:
        for fault in ("reply_lost", "still_listed", "inventory_unavailable"):
            for verifier_failed in (False, True):
                with self.subTest(fault=fault, verifier_failed=verifier_failed):
                    self.setUp()
                    if verifier_failed:
                        self.roundtrip = b"substituted business data"
                    docker = self.namespace["docker"]
                    removals: list[str] = []

                    def uncertain(*args: str) -> str:
                        if args[0] == "rm":
                            removals.append(args[2])
                            if fault == "still_listed":
                                return ""
                            result = str(docker(*args))
                            if fault == "reply_lost":
                                raise TimeoutError("fixture lost cleanup response")
                            return result
                        if args[0] == "ps" and fault == "inventory_unavailable":
                            raise OSError("fixture inventory unavailable")
                        return str(docker(*args))

                    self.namespace["docker"] = uncertain
                    with self.assertRaises(Exception):
                        self.recover()
                    pointer = (
                        self.root / "captures" / self.spec["recovery_binding"] / "outcome.json"
                    )
                    receipt = json.loads(pointer.read_text())
                    self.assertEqual(receipt["state"], "failed_retained")
                    self.assertEqual(removals, [receipt["retained"]["id"]])
                    self.assertTrue(Path(receipt["retained"]["path"]).exists())
                    self.assertTrue(
                        (Path(receipt["retained"]["path"]).parent / "capture.tar").exists()
                    )
                    self.namespace["docker"] = lambda *_args: self.fail("repeated Docker effect")
                    self.assertIn("recipient_recovery_retained=", self.recover(inspect=True))
                    self.assertIn("recipient_recovery_retained=", self.recover())

    def test_restore_failure_removes_its_known_scratch_and_private_data(self) -> None:
        def failing_restore(arguments: list[str], **_kwargs: Any) -> Any:
            return SimpleNamespace(
                returncode=1 if arguments[:3] == ["docker", "exec", "-i"] else 0,
                stdout="",
                stderr="",
            )

        with patch("subprocess.run", side_effect=failing_restore):
            with self.assertRaises(ValueError):
                self.recover()
        pointer = self.root / "captures" / self.spec["recovery_binding"] / "outcome.json"
        receipt = json.loads(pointer.read_text())
        self.assertEqual(receipt["state"], "failed_safe")
        self.assertFalse(self.containers)
        self.assertTrue(any(call[0] == "rm" for call in self.calls))
        attempt = pointer.parent / receipt["attempt"]
        for name in ("database.sql", "capture.tar", "restore"):
            self.assertFalse((attempt / name).exists())

    def test_uncertain_native_dump_never_emits_a_retry_witness(self) -> None:
        def interrupted(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("transport interrupted while bounded native dump may run")

        self.namespace["logical_dump"] = interrupted
        self.namespace["native_capture_status"] = lambda _native: (_ for _ in ()).throw(
            ValueError("native unknown")
        )
        with self.assertRaises(RuntimeError):
            self.recover()
        with self.assertRaises(ValueError):
            self.recover(inspect=True)
        with self.assertRaises(ValueError):
            self.recover()

    def test_preflight_failure_is_terminal_and_never_recaptures_same_binding(self) -> None:
        self.namespace["published_source"] = lambda spec: (_ for _ in ()).throw(
            ValueError("source prerequisites unavailable")
        )
        with self.assertRaises(ValueError):
            self.recover()
        self.namespace["published_source"] = lambda spec: self.fail("terminal binding recaptured")
        self.assertIn("recipient_recovery_retry_safe=true", self.recover())
        self.assertEqual(self.dumps, [])

    def test_only_capture_phase_can_settle_after_native_cancellation(self) -> None:
        def interrupted(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("capture transport lost")

        self.namespace["logical_dump"] = interrupted
        self.namespace["native_capture_status"] = lambda _native: (_ for _ in ()).throw(
            ValueError("native unknown")
        )
        with self.assertRaises(RuntimeError):
            self.recover()
        pointer = self.root / "captures" / self.spec["recovery_binding"] / "outcome.json"
        running = json.loads(pointer.read_text())
        native_reads: list[Any] = []

        def cancelled(native: Any) -> list[str]:
            native_reads.append(native)
            return ["cancelled", "1"]

        self.namespace["native_capture_status"] = cancelled
        for phase in ("source_closed", "create_pending", "scratch"):
            for missing in (False, True):
                with self.subTest(phase=phase, missing=missing):
                    invalid = running | {"phase": phase}
                    if not missing:
                        invalid["source_closed"] = False
                    pointer.write_text(json.dumps(invalid))
                    with self.assertRaises(ValueError):
                        self.recover(inspect=True)
                    self.assertEqual(native_reads, [])
                    self.assertEqual(json.loads(pointer.read_text())["state"], "running")
        pointer.write_text(json.dumps(running))
        self.assertIn("recipient_recovery_retry_safe=true", self.recover(inspect=True))
        self.assertEqual(native_reads, [running["native"]])
        self.assertEqual(json.loads(pointer.read_text())["state"], "failed_safe")

    def test_missing_pointer_tombstone_fences_a_delayed_original_worker(self) -> None:
        self.assertIn("recipient_recovery_retry_safe=true", self.recover(inspect=True))
        self.namespace["published_source"] = lambda _spec: self.fail("late worker reached source")
        self.assertIn("recipient_recovery_retry_safe=true", self.recover())
        self.assertEqual(self.dumps, [])
        self.assertFalse(any(call[0] == "create" for call in self.calls))

    def test_source_closed_crash_before_create_settles_without_source_retry(self) -> None:
        write = self.namespace["write_outcome_pointer"]

        def crash(destination: Path, journal: dict[str, Any]) -> None:
            write(destination, journal)
            if journal.get("phase") == "source_closed":
                raise SystemExit("simulated parent loss")

        self.namespace["write_outcome_pointer"] = crash
        with self.assertRaises(SystemExit):
            self.recover()
        self.namespace["write_outcome_pointer"] = write
        self.namespace["published_source"] = lambda _spec: self.fail("source relaunched")
        self.assertIn("recipient_recovery_retry_safe=true", self.recover(inspect=True))
        self.assertFalse(any(call[0] == "create" for call in self.calls))

    def test_lost_create_is_terminal_stopped_custody_without_claiming_an_object(self) -> None:
        docker = self.namespace["docker"]
        observable = False

        def lost(*args: str) -> str:
            if args[0] == "create":
                docker(*args)
                raise TimeoutError("create reply lost")
            if args[0] == "inspect" and not observable:
                raise ValueError("object not observable")
            return str(docker(*args))

        self.namespace["docker"] = lost
        with self.assertRaises(TimeoutError):
            self.recover()
        pointer = self.root / "captures" / self.spec["recovery_binding"] / "outcome.json"
        evidence = self.recover(inspect=True)
        self.assertIn("recipient_recovery_retained=", evidence)
        receipt = json.loads(pointer.read_text())
        self.assertEqual(receipt["state"], "failed_retained")
        markers = extract_odoo_post_deploy_readback_markers(
            cast(JsonObject, {"logs": evidence.splitlines()})
        )
        retained = json.loads(bytes.fromhex(markers["recipient_recovery_retained"]))
        self.assertEqual(retained, receipt["retained"])
        self.assertEqual(receipt["retained"]["id"], "")
        self.assertEqual(receipt["retained"]["disposition"], "stopped_create_unconfirmed")
        self.assertTrue(Path(receipt["retained"]["path"]).exists())
        self.assertFalse(any(call[0] == "start" for call in self.calls))
        before = list(self.dumps)
        self.assertEqual(self.recover(), evidence)
        self.assertEqual(self.dumps, before)
        self.assertFalse(any(call[0] == "rm" for call in self.calls))

    def test_restore_and_cleanup_interruptions_keep_observed_scratch_custody(self) -> None:
        for crash_phase in ("start", "rm"):
            with self.subTest(crash_phase=crash_phase):
                self.setUp()
                docker = self.namespace["docker"]

                def crash(*args: str) -> str:
                    if args[0] == crash_phase:
                        raise SystemExit("parent disappeared")
                    return str(docker(*args))

                self.namespace["docker"] = crash
                with self.assertRaises(SystemExit):
                    self.recover()
                # The durable prior observation remains valid custody even when
                # cleanup's reply is unknown; this makes no claim of absence.
                self.namespace["docker"] = lambda *_args: self.fail("repeated Docker effect")
                evidence = self.recover(inspect=True)
                self.assertIn("recipient_recovery_retained=", evidence)
                pointer = self.root / "captures" / self.spec["recovery_binding"] / "outcome.json"
                receipt = json.loads(pointer.read_text())
                self.assertEqual(receipt["state"], "failed_retained")
                self.assertEqual(
                    receipt["retained"]["disposition"], "retained_or_cleanup_unconfirmed"
                )
                self.assertEqual(self.dumps, [True] if crash_phase == "start" else [True, False])

    def test_start_phase_without_its_durable_isolation_stays_unknown(self) -> None:
        docker = self.namespace["docker"]

        def crash(*args: str) -> str:
            if args[0] == "start":
                raise SystemExit("parent loss")
            return str(docker(*args))

        self.namespace["docker"] = crash
        with self.assertRaises(SystemExit):
            self.recover()
        pointer = self.root / "captures" / self.spec["recovery_binding"] / "outcome.json"
        journal = json.loads(pointer.read_text())
        journal.pop("isolated")
        pointer.write_text(json.dumps(journal))
        with self.assertRaises(ValueError):
            self.recover(inspect=True)
        self.assertEqual(json.loads(pointer.read_text())["state"], "running")
        self.assertFalse((pointer.parent / "receipt.json").exists())

    def test_empty_business_data_refuses_even_when_integrity_succeeds(self) -> None:
        def empty(_identifier: str) -> None:
            raise ValueError("empty business data")

        self.namespace["populated_rows"] = empty
        with self.assertRaises(ValueError):
            self.recover()
        pointer = self.root / "captures" / self.spec["recovery_binding"] / "outcome.json"
        receipt = json.loads(pointer.read_text())
        self.assertEqual(receipt["state"], "failed_safe")
        self.assertFalse(self.containers)
        self.assertTrue(any(call[0] == "rm" for call in self.calls))
        self.assertFalse((pointer.parent / receipt["attempt"] / "restore").exists())

    def test_config_changes_or_source_container_replacement_refuse(self) -> None:
        original = self.dump

        def changed(identifier: str, path: Path, *, source: bool, **_kwargs: Any) -> None:
            original(identifier, path, source=source)
            (self.paths["config"] / "settings").write_text("changed during capture")

        self.namespace["logical_dump"] = changed
        with self.assertRaises(ValueError):
            self.recover()
        self.setUp()
        reads = []

        def replacement(_spec: Any) -> Any:
            reads.append(True)
            return self.paths, "live-source", self.identity if len(reads) == 1 else "d" * 64

        self.namespace["published_source"] = replacement
        with self.assertRaises(ValueError):
            self.recover()


class ContainerAbsenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.namespace: dict[str, Any] = {}
        exec(RESOURCE_LIBRARY + RECOVERY_LIBRARY + PUBLISHED_RECOVERY_LIBRARY, self.namespace)
        self.identifier = "f" * 64
        self.native = {
            "source": self.identifier,
            "operation": "d" * 64,
            "nonce": "a" * 32,
        }

    def test_complete_inventory_distinguishes_absence_from_present_stopped_objects(self) -> None:
        for identifiers, absent in (([], True), ([self.identifier], False), (["b" * 64], True)):
            with self.subTest(identifiers=identifiers):
                self.namespace["docker"] = lambda *_args: "\n".join(identifiers)
                self.assertEqual(self.namespace["container_is_absent"](self.identifier), absent)

    def test_invalid_identity_or_inventory_never_proves_absence(self) -> None:
        def forbidden(*_args: str) -> str:
            self.fail("invalid identity reached Docker")

        self.namespace["docker"] = forbidden
        for identifier in ("short-name", self.identifier[:12]):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                self.namespace["container_is_absent"](identifier)
        for inventory in (self.identifier[:12], "unavailable", self.identifier + "\n\n"):
            with self.subTest(inventory=inventory):
                self.namespace["docker"] = lambda *_args: inventory
                with self.assertRaises(ValueError):
                    self.namespace["container_is_absent"](self.identifier)

    def test_removed_source_cancels_but_exec_errors_and_listed_sources_stay_unknown(self) -> None:
        failed = SimpleNamespace(returncode=1, stdout="", stderr="fixture transport refusal")
        with patch("subprocess.run", return_value=failed):
            self.namespace["docker"] = lambda *_args: ""
            self.assertEqual(
                self.namespace["native_capture_status"](self.native), ["cancelled", "1"]
            )
            for inventory in (self.identifier, self.identifier[:12]):
                with self.subTest(inventory=inventory):
                    self.namespace["docker"] = lambda *_args: inventory
                    with self.assertRaises(ValueError):
                        self.namespace["native_capture_status"](self.native)

            def unavailable(*_args: str) -> str:
                raise OSError("fixture daemon unavailable")

            self.namespace["docker"] = unavailable
            with self.assertRaises(OSError):
                self.namespace["native_capture_status"](self.native)

    def test_successful_native_witness_does_not_need_an_absence_inference(self) -> None:
        passed = SimpleNamespace(returncode=0, stdout=self.native["nonce"] + ":done:0\n", stderr="")
        self.namespace["docker"] = lambda *_args: self.fail("unnecessary absence inference")
        with patch("subprocess.run", return_value=passed):
            self.assertEqual(self.namespace["native_capture_status"](self.native), ["done", "0"])


class NativeHandshakeTests(unittest.TestCase):
    def test_go_is_written_only_after_exact_ready(self) -> None:
        namespace: dict[str, Any] = {}
        exec(RESOURCE_LIBRARY + RECOVERY_LIBRARY + PUBLISHED_RECOVERY_LIBRARY, namespace)
        nonce = uuid.uuid4().hex
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import select,sys; nonce=sys.argv[1]; "
                "assert not select.select([sys.stdin],[],[],0.05)[0]; "
                "print(nonce+':READY',file=sys.stderr,flush=True); "
                "assert sys.stdin.readline()==nonce+':GO\\n'",
                nonce,
            ],
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            namespace["await_native_ready"](child, nonce)
            child.wait(timeout=5)
            self.assertEqual(child.returncode, 0)
        finally:
            if child.stdin is not None:
                child.stdin.close()
            if child.stderr is not None:
                child.stderr.close()
            child.wait(timeout=5)

    def test_foreign_ready_never_receives_go(self) -> None:
        namespace: dict[str, Any] = {}
        exec(RESOURCE_LIBRARY + RECOVERY_LIBRARY + PUBLISHED_RECOVERY_LIBRARY, namespace)
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys; print('foreign:READY',file=sys.stderr,flush=True); "
                "assert sys.stdin.readline()==''",
            ],
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            with self.assertRaises(ValueError):
                namespace["await_native_ready"](child, uuid.uuid4().hex)
            assert child.stdin is not None
            child.stdin.close()
            child.wait(timeout=5)
            self.assertEqual(child.returncode, 0)
        finally:
            if child.stdin is not None and not child.stdin.closed:
                child.stdin.close()
            if child.stderr is not None:
                child.stderr.close()
            child.wait(timeout=5)


class PublishedSourceTests(unittest.TestCase):
    def test_exact_active_mounts_pass_and_stopped_foreign_consumers_refuse(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            namespace: dict[str, Any] = {}
            exec(
                compile(
                    RESOURCE_LIBRARY + RECOVERY_LIBRARY + PUBLISHED_RECOVERY_LIBRARY,
                    "<published-source>",
                    "exec",
                ),
                namespace,
            )
            project = "lp-prod-recipient-" + "a" * 32
            spec: dict[str, Any] = {
                "namespace": project,
                "project": project,
                "binding": "b" * 64,
                "volumes": {"database": project + "-db", "config": project + "-config"},
                "database_service": "database",
                "application_service": "application",
                "database_image": "database@sha256:" + "c" * 64,
                "application_image": "application@sha256:" + "d" * 64,
                "mounts": {
                    "database": {"destination": "/var/lib/mysql", "read_only": False},
                    "config": {"destination": "/config", "read_only": False},
                },
            }
            volumes: dict[str, Any] = {}
            containers: dict[str, Any] = {}
            for role, name in spec["volumes"].items():
                path = root / name / "_data"
                path.mkdir(parents=True)
                volumes[name] = {
                    "Name": name,
                    "Driver": "local",
                    "Options": {},
                    "Mountpoint": str(path),
                    "Labels": {
                        "io.launchplane.production-recipient": spec["binding"],
                        "io.launchplane.resource-role": role,
                    },
                }
                service = "database" if role == "database" else "application"
                containers[service] = {
                    "Id": service,
                    "Image": service + "-image-id",
                    "State": {"Running": True},
                    "Config": {
                        "Image": spec[service + "_image"],
                        "Labels": {
                            "com.docker.compose.project": project,
                            "com.docker.compose.service": service,
                        },
                    },
                    "Mounts": [
                        {
                            "Type": "volume",
                            "Name": name,
                            "Source": str(path),
                            "RW": True,
                            "Destination": "/var/lib/mysql" if role == "database" else "/config",
                        }
                    ],
                }

            def docker(*args: str) -> str:
                if args[:2] == ("volume", "inspect"):
                    return json.dumps([volumes[args[2]]])
                if args[:2] == ("ps", "-aq"):
                    return "\n".join(containers)
                if args[0] == "inspect":
                    return json.dumps([containers[args[1]]])
                raise AssertionError(args)

            namespace["docker"] = docker
            paths, source, identity = namespace["published_source"](spec)
            self.assertEqual(source, "database")
            self.assertEqual(set(paths), {"database", "config"})
            self.assertTrue(identity)
            containers["application"]["Mounts"][0]["Destination"] = "/wrong-config"
            with self.assertRaises(ValueError):
                namespace["published_source"](spec)
            containers["application"]["Mounts"][0]["Destination"] = "/config"
            containers["stopped-foreign"] = {
                "Id": "foreign",
                "Config": {"Labels": {}},
                "State": {"Running": False},
                "Mounts": containers["database"]["Mounts"],
            }
            with self.assertRaises(ValueError):
                namespace["published_source"](spec)


@unittest.skipUnless(
    os.environ.get("LAUNCHPLANE_TEST_MARIADB_IMAGE"), "explicit local MariaDB image required"
)
class RealPublishedMariaDBTests(unittest.TestCase):
    def test_removed_source_settles_original_capture_but_stopped_source_stays_unknown(self) -> None:
        fixture = mariadb_tests.RealMariaDBRecoveryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        namespace: dict[str, Any] = {}
        exec(RESOURCE_LIBRARY + RECOVERY_LIBRARY + PUBLISHED_RECOVERY_LIBRARY, namespace)
        source = fixture.docker(
            "run",
            "-d",
            "--name",
            "lp-recovery-removed-" + uuid.uuid4().hex,
            "--network",
            "none",
            "--entrypoint",
            "sh",
            fixture.image,
            "-ec",
            "exec sleep 60",
        )

        def cleanup_source() -> None:
            if not namespace["container_is_absent"](source):
                fixture.docker("rm", "-f", source)

        self.addCleanup(cleanup_source)
        operation = fixture.spec["recovery_binding"]
        native = {"source": source, "operation": operation, "nonce": uuid.uuid4().hex}
        spec = {"operation": operation, "recovery_binding": fixture.spec["recovery_binding"]}
        base = fixture.root / "captures"
        with namespace["recovery_directory"](spec, base) as destination:
            attempt = destination / ("attempt-" + operation[:32])
            attempt.mkdir()
            namespace["write_outcome_pointer"](
                destination,
                {
                    "state": "running",
                    "phase": "capture",
                    "operation": operation,
                    "binding": spec["recovery_binding"],
                    "attempt": attempt.name,
                    "native": native,
                },
            )
        waiting = subprocess.Popen(
            [
                "docker",
                "exec",
                "-i",
                source,
                "sh",
                "-ec",
                namespace["NATIVE_CAPTURE_SCRIPT"],
                "capture",
                operation,
                native["nonce"],
                "6",
                "2",
                "mariadb",
                "--user=root",
                "--execute=SELECT 1",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(lambda: waiting.communicate(timeout=15))
        namespace["read_native_ready"](waiting, native["nonce"])
        fixture.docker("stop", "--time", "1", source)
        waiting.communicate(timeout=10)
        with self.assertRaises(ValueError):
            namespace["native_capture_status"](native)
        fixture.docker("rm", source)
        output = io.StringIO()
        with redirect_stdout(output):
            namespace["published_recovery"](spec | {"inspect": True}, base=base)
        receipt = json.loads((base / spec["recovery_binding"] / "receipt.json").read_text())
        self.assertEqual(receipt["state"], "failed_safe")
        self.assertFalse((attempt / "restore").exists())
        with patch("subprocess.run", side_effect=AssertionError("terminal replay touched Docker")):
            replay = io.StringIO()
            with redirect_stdout(replay):
                namespace["published_recovery"](spec | {"inspect": True}, base=base)
        self.assertEqual(replay.getvalue(), output.getvalue())

    def test_native_timeout_unlocks_writer_and_cancelled_nonce_never_launches(self) -> None:
        fixture = mariadb_tests.RealMariaDBRecoveryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        source = fixture.seed_database("Aria", keep_running=True)
        namespace: dict[str, Any] = {}
        exec(RESOURCE_LIBRARY + RECOVERY_LIBRARY + PUBLISHED_RECOVERY_LIBRARY, namespace)
        nonce = uuid.uuid4().hex
        operation = "d" * 64
        command = [
            "docker",
            "exec",
            "-i",
            source,
            "sh",
            "-ec",
            namespace["NATIVE_CAPTURE_SCRIPT"],
            "capture",
            operation,
            nonce,
            "8",
            "2",
            "mariadb",
            "--no-defaults",
            "--protocol=socket",
            "--user=root",
            "--execute=SELECT SLEEP(5)",
        ]
        capture = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        namespace["await_native_ready"](capture, nonce)
        self.addCleanup(lambda: capture.communicate(timeout=15))
        deadline = time.monotonic() + 5
        while True:
            holder = subprocess.run(
                [
                    "docker",
                    "exec",
                    source,
                    "cat",
                    "/tmp/launchplane-capture-" + operation + "-" + nonce + "/holder",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if nonce + ":locked" in holder.stdout.splitlines():
                break
            if capture.poll() is not None:
                output, error = capture.communicate()
                self.fail(f"native capture refused before lock: {output!r} {error!r}")
            if time.monotonic() > deadline:
                self.fail("native holder never acquired its lock")
            time.sleep(0.05)
        writer = subprocess.Popen(
            [
                "docker",
                "exec",
                source,
                "mariadb",
                "--user=root",
                "--execute=INSERT INTO fixture.items VALUES ('writer-after-lock')",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(lambda: writer.communicate(timeout=15))
        deadline = time.monotonic() + 1.5
        while (
            fixture.docker(
                "exec",
                source,
                "mariadb",
                "--user=root",
                "--batch",
                "--skip-column-names",
                "--execute=SELECT COUNT(*) FROM information_schema.PROCESSLIST WHERE STATE='Waiting for backup lock'",
            )
            == "0"
        ):
            if time.monotonic() > deadline:
                self.fail("writer was not blocked by the capture holder")
            time.sleep(0.05)
        native = {"source": source, "nonce": nonce, "operation": operation}
        with self.assertRaises(ValueError):
            namespace["native_capture_status"](native)
        # The native deadline interrupts the dump, while the separate idle holder
        # releases FTWRL even when that dump's server query is still running.
        # Kill the host transport after GO. Source settlement still requires
        # the in-container deadline and a positive quiescence observation.
        capture.kill()
        capture.communicate(timeout=10)
        self.assertNotEqual(capture.returncode, 0)
        _, writer_error = writer.communicate(timeout=5)
        self.assertEqual(writer.returncode, 0, writer_error)
        native = {"source": source, "nonce": nonce, "operation": operation}
        deadline = time.monotonic() + 8
        while True:
            try:
                terminal = namespace["native_capture_status"](native)
                break
            except ValueError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)
        self.assertNotEqual(terminal, ["done", "0"])
        late = subprocess.run(command, capture_output=True, timeout=10)
        self.assertNotEqual(late.returncode, 0)
        self.assertEqual(namespace["native_capture_status"](native), terminal)
        self.assertEqual(
            fixture.docker(
                "exec",
                source,
                "mariadb",
                "--user=root",
                "--batch",
                "--skip-column-names",
                "--execute=SELECT note FROM fixture.items WHERE note='writer-after-lock'",
            ),
            "writer-after-lock",
        )

    def test_restart_without_a_live_post_ready_go_cannot_launch_sql(self) -> None:
        fixture = mariadb_tests.RealMariaDBRecoveryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        source = fixture.seed_database(keep_running=True, tmpfs=True)
        namespace: dict[str, Any] = {}
        exec(RESOURCE_LIBRARY + RECOVERY_LIBRARY + PUBLISHED_RECOVERY_LIBRARY, namespace)
        operation = "d" * 64
        nonce = uuid.uuid4().hex
        native = {"source": source, "operation": operation, "nonce": nonce}
        command = [
            "docker",
            "exec",
            "-i",
            source,
            "sh",
            "-ec",
            namespace["NATIVE_CAPTURE_SCRIPT"],
            "capture",
            operation,
            nonce,
            "6",
            "2",
            "mariadb",
            "--user=root",
            "--execute=INSERT INTO fixture.items VALUES ('must-not-launch')",
        ]
        # READY is observed, but host permission has not been sent. Restart
        # kills this actual exec; Docker never restarts it with the container.
        waiting = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        self.addCleanup(lambda: waiting.communicate(timeout=15))
        namespace["read_native_ready"](waiting, nonce)
        fixture.docker("restart", source)
        waiting.communicate(timeout=10)
        self.assertNotEqual(waiting.returncode, 0)
        # Fence the old nonce only after positive server readiness/quiescence;
        # restart completion alone is not a database-readiness assertion.
        deadline = time.monotonic() + 10
        while True:
            try:
                namespace["native_capture_status"](native)
                break
            except ValueError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
        # Remove its tmpfs state by a real restart.
        marker = "/tmp/launchplane-capture-" + operation + "-" + nonce + "/cancelled"
        fixture.docker("exec", source, "test", "-f", marker)
        gate = fixture.root / "delayed-exec-go"
        # A queued transport survives outside the container. Closing the only
        # private input writer models its host parent's death before READY.
        queued = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import os,sys,time; from pathlib import Path; "
                "gate=Path(sys.argv[1]); "
                "exec('while not gate.exists(): time.sleep(0.02)'); "
                "os.execvp(sys.argv[2],sys.argv[2:])",
                str(gate),
                *command,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(lambda: queued.communicate(timeout=15))
        assert queued.stdin is not None
        queued.stdin.close()
        queued.stdin = None
        fixture.docker("restart", source)
        missing = subprocess.run(
            ["docker", "exec", source, "test", "-f", marker], capture_output=True, timeout=10
        )
        self.assertNotEqual(missing.returncode, 0)
        gate.touch()
        queued.communicate(timeout=10)
        self.assertNotEqual(queued.returncode, 0)
        deadline = time.monotonic() + 10
        while True:
            try:
                rows = fixture.docker(
                    "exec",
                    source,
                    "mariadb",
                    "--user=root",
                    "--batch",
                    "--skip-column-names",
                    "--execute=SELECT COUNT(*) FROM fixture.items WHERE note='must-not-launch'",
                )
                break
            except subprocess.CalledProcessError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
        self.assertEqual(rows, "0")

    def test_existing_root_session_refuses_before_native_capture(self) -> None:
        fixture = mariadb_tests.RealMariaDBRecoveryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        source = fixture.seed_database(keep_running=True)
        namespace: dict[str, Any] = {}
        exec(RESOURCE_LIBRARY + RECOVERY_LIBRARY + PUBLISHED_RECOVERY_LIBRARY, namespace)
        # A live administrative root session is an explicit unsupported precondition.
        other = subprocess.Popen(
            ["docker", "exec", source, "mariadb", "--user=root", "--execute=SELECT SLEEP(3)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(lambda: other.communicate(timeout=10))
        deadline = time.monotonic() + 5
        while (
            fixture.docker(
                "exec",
                source,
                "mariadb",
                "--user=root",
                "--batch",
                "--skip-column-names",
                "--execute=SELECT COUNT(*) FROM information_schema.PROCESSLIST WHERE USER='root' AND ID<>CONNECTION_ID()",
            )
            == "0"
        ):
            if time.monotonic() > deadline:
                self.fail("fixture root session did not start")
            time.sleep(0.05)
        native = {"source": source, "nonce": uuid.uuid4().hex, "operation": "d" * 64}
        with self.assertRaises(namespace["NativeCaptureFinishedError"]):
            namespace["logical_dump"](
                source, fixture.root / "refused.sql", source=True, native=native
            )
        self.assertEqual(namespace["native_capture_status"](native)[0], "refused")
        other.communicate(timeout=10)

    def test_running_transactional_and_nontransactional_sources_restore_exact_business_rows(
        self,
    ) -> None:
        for engine in ("InnoDB", "Aria"):
            with self.subTest(engine=engine):
                fixture = mariadb_tests.RealMariaDBRecoveryTests()
                fixture.setUp()
                try:
                    source = fixture.seed_database(engine, keep_running=True)
                    operation = fixture.spec["recovery_binding"]

                    def remove_fixture_scratch() -> None:
                        name = "lp-recovery-" + operation[:32]
                        observed = subprocess.run(
                            ["docker", "inspect", name], capture_output=True, text=True
                        )
                        if observed.returncode:
                            return
                        row = json.loads(observed.stdout)[0]
                        self.assertEqual(
                            row["Config"]["Labels"].get(
                                "io.launchplane.recipient-recovery-operation"
                            ),
                            operation,
                        )
                        fixture.docker("rm", "-f", row["Id"])

                    fixture.addCleanup(remove_fixture_scratch)
                    fixture.docker(
                        "exec",
                        source,
                        "mariadb",
                        "--user=root",
                        "--execute=CREATE TABLE fixture.serial (id INT AUTO_INCREMENT PRIMARY KEY, note VARCHAR(80)) ENGINE=InnoDB; "
                        "CREATE TABLE fixture.audit (id INT PRIMARY KEY, note VARCHAR(80)) ENGINE=Aria; "
                        "CREATE TRIGGER fixture.serial_audit AFTER INSERT ON fixture.serial FOR EACH ROW INSERT INTO fixture.audit VALUES (NEW.id,NEW.note); "
                        "INSERT INTO fixture.serial (note) VALUES ('first'),('second'); "
                        "CREATE VIEW fixture.serial_view AS SELECT id,note FROM fixture.serial; "
                        "CREATE PROCEDURE fixture.business_rows() SELECT * FROM fixture.serial_view;",
                    )
                    namespace: dict[str, Any] = {}
                    exec(
                        compile(
                            RESOURCE_LIBRARY + RECOVERY_LIBRARY + PUBLISHED_RECOVERY_LIBRARY,
                            "<real-published-recovery>",
                            "exec",
                        ),
                        namespace,
                    )
                    paths = dict(zip(("database", "config"), fixture.paths, strict=True))
                    namespace["published_source"] = lambda spec: (paths, source, "c" * 64)
                    actual = namespace["populated_rows"]
                    checked: list[str] = []

                    def check(identifier: str) -> None:
                        actual(identifier)
                        rows = fixture.docker(
                            "exec",
                            identifier,
                            "mariadb",
                            "--no-defaults",
                            "--protocol=socket",
                            "--user=root",
                            "--batch",
                            "--skip-column-names",
                            "--execute=SELECT note FROM fixture.items",
                        )
                        self.assertEqual(rows, "restored-business-row")
                        checked.append(identifier)

                    namespace["populated_rows"] = check
                    output = io.StringIO()
                    with redirect_stdout(output):
                        namespace["published_recovery"](
                            fixture.spec | {"operation": operation},
                            base=fixture.root / "published-captures",
                        )
                    self.assertIn("recipient_recovery_ok=true", output.getvalue())
                    self.assertTrue(checked)
                    self.assertTrue(
                        json.loads(fixture.docker("inspect", source))[0]["State"]["Running"]
                    )
                    self.assertEqual(
                        fixture.docker(
                            "exec",
                            source,
                            "mariadb",
                            "--protocol=socket",
                            "--user=root",
                            "--batch",
                            "--skip-column-names",
                            "--execute=SELECT note FROM fixture.items",
                        ),
                        "restored-business-row",
                    )
                finally:
                    fixture.doCleanups()
