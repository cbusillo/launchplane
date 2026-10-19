"""Execute the fixed published capture program against isolated local fixtures."""

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
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
        self.spec = {"recovery_binding": "a" * 64, "database_image": "mariadb@sha256:" + "b" * 64}
        self.network = "none"
        self.mount = ""
        self.roundtrip = (
            b"CREATE DATABASE fixture; INSERT INTO fixture.items VALUES ('business-row');\n"
        )
        self.identity = "c" * 64
        self.dumps: list[bool] = []
        self.namespace["published_source"] = lambda spec: (self.paths, "live-source", self.identity)
        self.namespace["populated_rows"] = lambda identifier: None
        self.namespace["logical_dump"] = self.dump
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
            return "scratch"
        if args[:2] == ("image", "inspect"):
            return json.dumps([{"Config": {"Env": []}}])
        if args[0] == "inspect":
            return json.dumps(
                [
                    {
                        "Config": {
                            "Labels": {
                                "io.launchplane.recipient-recovery": self.spec["recovery_binding"]
                            },
                            "Env": [],
                        },
                        "HostConfig": {"NetworkMode": self.network, "PortBindings": {}},
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
        self.assertTrue((attempt / "capture.tar").exists())
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

    def test_uncertain_native_dump_never_emits_a_retry_witness(self) -> None:
        def interrupted(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("transport interrupted while bounded native dump may run")

        self.namespace["logical_dump"] = interrupted
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
        with self.assertRaises(RuntimeError):
            self.recover()
        pointer = self.root / "captures" / self.spec["recovery_binding"] / "outcome.json"
        running = json.loads(pointer.read_text())
        native_reads: list[Any] = []

        def cancelled(native: Any) -> list[str]:
            native_reads.append(native)
            return ["cancelled", "1"]

        self.namespace["native_capture_status"] = cancelled
        pointer.write_text(json.dumps(running | {"phase": "restore"}))
        with self.assertRaisesRegex(ValueError, "scratch effects"):
            self.recover(inspect=True)
        self.assertEqual(native_reads, [])
        pointer.write_text(json.dumps(running))
        self.assertIn("recipient_recovery_retry_safe=true", self.recover(inspect=True))
        self.assertEqual(native_reads, [running["native"]])
        self.assertEqual(json.loads(pointer.read_text())["state"], "failed_safe")

    def test_empty_business_data_refuses_even_when_integrity_succeeds(self) -> None:
        def empty(_identifier: str) -> None:
            raise ValueError("empty business data")

        self.namespace["populated_rows"] = empty
        with self.assertRaises(ValueError):
            self.recover()

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
    def test_native_timeout_unlocks_writer_and_cancelled_nonce_never_launches(self) -> None:
        fixture = mariadb_tests.RealMariaDBRecoveryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        source = fixture.seed_database("Aria", keep_running=True)
        namespace: dict[str, Any] = {}
        exec(RESOURCE_LIBRARY + RECOVERY_LIBRARY + PUBLISHED_RECOVERY_LIBRARY, namespace)
        nonce = uuid.uuid4().hex
        command = [
            "docker",
            "exec",
            source,
            "sh",
            "-ec",
            namespace["NATIVE_CAPTURE_SCRIPT"],
            "capture",
            nonce,
            "8",
            "2",
            "mariadb",
            "--no-defaults",
            "--protocol=socket",
            "--user=root",
            "--execute=SELECT SLEEP(5)",
        ]
        capture = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: capture.communicate(timeout=15))
        deadline = time.monotonic() + 5
        while True:
            holder = subprocess.run(
                ["docker", "exec", source, "cat", "/tmp/launchplane-capture-" + nonce + "/holder"],
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
        native = {"source": source, "nonce": nonce}
        with self.assertRaises(ValueError):
            namespace["native_capture_status"](native)
        # The native deadline interrupts the dump, while the separate idle holder
        # releases FTWRL even when that dump's server query is still running.
        capture.communicate(timeout=10)
        self.assertNotEqual(capture.returncode, 0)
        _, writer_error = writer.communicate(timeout=5)
        self.assertEqual(writer.returncode, 0, writer_error)
        native = {"source": source, "nonce": nonce}
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
        native = {"source": source, "nonce": uuid.uuid4().hex}
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
                            fixture.spec, base=fixture.root / "published-captures"
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
