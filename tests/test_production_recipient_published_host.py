"""Execute the fixed published capture program against isolated local fixtures."""

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
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
        patch("subprocess.run", return_value=SimpleNamespace(returncode=0)).start()
        patch("shutil.disk_usage", return_value=SimpleNamespace(free=100 * 1024**3)).start()

    def dump(self, identifier: str, path: Path, *, source: bool) -> None:
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
        def interrupted(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("transport interrupted while bounded native dump may run")

        self.namespace["logical_dump"] = interrupted
        with self.assertRaises(RuntimeError):
            self.recover()
        with self.assertRaises(ValueError):
            self.recover(inspect=True)
        with self.assertRaises(ValueError):
            self.recover()

    def test_empty_business_data_refuses_even_when_integrity_succeeds(self) -> None:
        def empty(identifier: str) -> None:
            raise ValueError("empty business data")

        self.namespace["populated_rows"] = empty
        with self.assertRaises(ValueError):
            self.recover()

    def test_config_changes_or_source_container_replacement_refuse(self) -> None:
        original = self.dump

        def changed(identifier: str, path: Path, *, source: bool) -> None:
            original(identifier, path, source=source)
            (self.paths["config"] / "settings").write_text("changed during capture")

        self.namespace["logical_dump"] = changed
        with self.assertRaises(ValueError):
            self.recover()
        self.setUp()
        reads = []

        def replacement(spec: Any) -> Any:
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
