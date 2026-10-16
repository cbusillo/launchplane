"""Execute fixed destination programs against isolated files and bounded Docker doubles."""

import base64
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Any, cast
import unittest
from unittest.mock import patch

from control_plane.dokploy.post_deploy import extract_odoo_post_deploy_readback_markers
from control_plane.dokploy.api import JsonObject
from control_plane.production_recipient_host import RECOVERY_LIBRARY, upstream_program
from control_plane.production_recipient_resources import RESOURCE_LIBRARY


class UpstreamProgramTests(unittest.TestCase):
    def test_actual_grants_refuse_writers_roles_unknowns_and_keep_values_private(self) -> None:
        for grants, passed in (
            ("GRANT SELECT ON `source`.* TO 'reader'@'%'", True),
            ("GRANT ALL PRIVILEGES ON *.* TO 'reader'@'%'", False),
            ("GRANT 'writer_role' TO 'reader'@'%'", False),
            ("unavailable", False),
        ):
            with self.subTest(grants=grants):
                namespace = "lp-prod-recipient-" + "a" * 32
                spec: dict[str, Any] = {
                    "image": "mariadb@sha256:" + "b" * 64,
                    "project": namespace,
                    "binding": "c" * 64,
                    "connections": [
                        {
                            "settings": {
                                "host": "source.invalid",
                                "port": "3306",
                                "user": "reader",
                                "database": "source",
                                "password": "private-fixture-password",
                            }
                        }
                    ],
                }
                network = {
                    "Driver": "bridge",
                    "EnableIPv6": False,
                    "Options": {},
                    "Containers": {},
                    "Labels": {
                        "com.docker.compose.project": namespace,
                        "com.docker.compose.network": "default",
                        "io.launchplane.production-recipient": spec["binding"],
                    },
                }
                containers: dict[str, dict[str, Any]] = {}

                def docker(args: list[str], **kwargs: Any) -> Any:
                    call = args[1:]
                    if call[:2] == ["network", "ls"]:
                        result = ""
                    elif call[:2] == ["network", "create"]:
                        result = "network"
                    elif call[:2] == ["network", "inspect"]:
                        result = json.dumps([network])
                    elif call[0] == "create":
                        name = call[call.index("--name") + 1]
                        containers[name] = {
                            "State": {"ExitCode": 0},
                            "Config": {"Labels": {"io.launchplane.upstream-probe": name}},
                        }
                        self.assertEqual(
                            kwargs["env"]["MYSQL_PWD"],
                            spec["connections"][0]["settings"]["password"],
                        )
                        self.assertNotIn(spec["connections"][0]["settings"]["password"], args)
                        result = name
                    elif call[0] == "start":
                        result = grants
                    elif call[0] == "inspect":
                        result = json.dumps([containers[call[1]]])
                    elif call[0] == "ps":
                        result = "\n".join(containers)
                    elif call[0] == "rm":
                        containers.pop(call[-1])
                        result = ""
                    else:
                        self.fail(f"Unexpected operation {call}")
                    return subprocess.CompletedProcess(args, 0, stdout=result, stderr="")

                output = io.StringIO()
                code = 0
                with (
                    patch.object(
                        sys, "argv", ["probe", base64.b64encode(json.dumps(spec).encode()).decode()]
                    ),
                    patch("subprocess.run", side_effect=docker),
                    redirect_stdout(output),
                ):
                    try:
                        exec(compile(upstream_program(), "<upstream-probe>", "exec"), {})
                    except SystemExit as error:
                        code = error.code if isinstance(error.code, int) else 1
                self.assertEqual(code == 0, passed)
                self.assertFalse(containers)
                self.assertNotIn("private-fixture-password", output.getvalue())
                self.assertNotIn("source.invalid", output.getvalue())
                evidence = extract_odoo_post_deploy_readback_markers(
                    cast(JsonObject, {"logs": output.getvalue().splitlines()})
                )
                self.assertEqual(evidence.get("recipient_upstreams_ok"), str(passed).lower())
                if passed:
                    self.assertEqual(evidence["recipient_upstreams_binding"], spec["binding"])


class RecoveryProgramTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.paths = [self.root / role for role in ("database", "config")]
        for path in self.paths:
            path.mkdir()
        (self.paths[0] / "mysql").mkdir()
        (self.paths[0] / "table.data").write_bytes(b"needed database rows")
        (self.paths[1] / "configuration").write_bytes(b"needed config")
        self.namespace: dict[str, Any] = {}
        exec(
            compile(RESOURCE_LIBRARY + RECOVERY_LIBRARY, "<isolated-recovery>", "exec"),
            self.namespace,
        )
        self.namespace["main"] = lambda: self.paths
        self.spec = {"recovery_binding": "a" * 64, "database_image": "mariadb@sha256:" + "b" * 64}
        self.containers: dict[str, dict[str, Any]] = {}
        self.network = "none"
        self.fail_check = False
        self.calls: list[tuple[str, ...]] = []
        self.namespace["docker"] = self.docker

    def docker(self, *args: str) -> str:
        self.calls.append(args)
        if args[0] == "create":
            name = args[args.index("--name") + 1]
            self.containers[name] = {
                "Config": {
                    "Env": [],
                    "Labels": {"io.launchplane.recipient-recovery": self.spec["recovery_binding"]},
                },
                "HostConfig": {"NetworkMode": self.network},
            }
            return name
        if args[0] == "start":
            return ""
        if args[0] == "exec":
            if self.fail_check:
                raise ValueError("corrupt recovery")
            return ""
        if args[:2] == ("image", "inspect"):
            return json.dumps([{"Config": {"Env": []}}])
        if args[0] == "inspect":
            return json.dumps([self.containers[args[1]]])
        if args[0] == "rm":
            self.containers.pop(args[-1])
            return ""
        raise AssertionError(args)

    def recover(self) -> str:
        output = io.StringIO()
        with (
            patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0)),
            redirect_stdout(output),
        ):
            self.namespace["recover"](self.paths, self.spec, base=self.root / "captures")
        return output.getvalue()

    def test_capture_restores_exact_data_verifies_database_and_retains_archive(self) -> None:
        before = [self.namespace["manifest"](path) for path in self.paths]
        evidence = extract_odoo_post_deploy_readback_markers(
            cast(JsonObject, {"logs": self.recover().splitlines()})
        )
        self.assertEqual(evidence["recipient_recovery_ok"], "true")
        self.assertEqual(evidence["recipient_recovery_binding"], self.spec["recovery_binding"])
        self.assertEqual(before, [self.namespace["manifest"](path) for path in self.paths])
        destination = self.root / "captures" / self.spec["recovery_binding"]
        self.assertTrue((destination / "capture.tar").exists())
        self.assertTrue((destination / "receipt.json").exists())
        self.assertFalse(self.containers)
        with self.assertRaises(FileExistsError):
            self.recover()

    def test_live_network_corrupt_database_or_symlink_never_produces_a_receipt(self) -> None:
        self.network = "bridge"
        with self.assertRaises(ValueError):
            self.recover()
        self.assertFalse(
            (self.root / "captures" / self.spec["recovery_binding"] / "receipt.json").exists()
        )
        self.assertFalse(self.containers)
        (self.paths[1] / "alias").symlink_to(self.paths[0] / "table.data")
        with self.assertRaises(ValueError):
            self.namespace["manifest"](self.paths[1])

    def test_uncertain_integrity_preserves_capture_and_never_writes_source(self) -> None:
        self.fail_check = True
        before = (self.paths[0] / "table.data").read_bytes()
        with self.assertRaises(ValueError):
            self.recover()
        self.assertEqual((self.paths[0] / "table.data").read_bytes(), before)
        self.assertTrue(
            (self.root / "captures" / self.spec["recovery_binding"] / "capture.tar").exists()
        )
        self.assertFalse(self.containers)
