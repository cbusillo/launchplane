"""Retirement reads prove owned data and every old service without reading passwords."""

import base64
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

from control_plane.dokploy.testing_retained_copy import (
    qualify_retained_copy,
    retained_copy_host_program,
)
from control_plane.odoo_testing_restore import FRESH_TESTING_LOCAL_CREDENTIAL_KEYS


class RetainedTestingCopyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.spec: dict[str, Any] = {
            "mode": "read",
            "context": "example",
            "project": "old-testing",
            "health_path": "/launchplane/health",
        }
        self.identity = {"context": "example", "instance": "testing", "artifact_id": "old-build"}
        self.containers: list[dict[str, Any]] = []
        for service, destination in (
            ("web", "/volumes/data"),
            ("database", "/var/lib/postgresql/data"),
        ):
            source = self.root / (service + "-volume")
            source.mkdir()
            self.containers.append(
                {
                    "Id": service + "-id",
                    "Config": {
                        "Labels": {
                            "com.docker.compose.project": self.spec["project"],
                            "com.docker.compose.service": service,
                        },
                        "Env": [],
                    },
                    "Mounts": [
                        {
                            "Destination": destination,
                            "Type": "volume",
                            "Name": source.name,
                            "Source": str(source),
                            "RW": True,
                        }
                    ],
                    "State": {"Status": "running"},
                }
            )
        self.containers[0]["Config"]["Env"] = [
            "PLATFORM_INSTANCE=testing",
            "ODOO_DB_HOST=database",
            "ODOO_HTTP_PORT=8171",
            "LAUNCHPLANE_RUNTIME_IDENTITY_JSON=" + json.dumps(self.identity),
            *(key + "=fixture-" + key for key in FRESH_TESTING_LOCAL_CREDENTIAL_KEYS),
        ]

    def test_all_services_must_be_stopped_and_restarts_keep_the_same_data_custody(self) -> None:
        read = qualify_retained_copy(self.containers, self.spec)
        original = {
            **self.spec,
            "identity": read["identity"],
            "resource_binding": read["resource_binding"],
        }
        self.containers[0]["Id"] = "restarted-web"
        self.assertEqual(
            qualify_retained_copy(self.containers, original)["resource_binding"],
            read["resource_binding"],
        )
        self.containers[0]["State"]["Status"] = "exited"
        self.assertTrue(qualify_retained_copy(self.containers, original)["web_stopped"])
        self.assertFalse(qualify_retained_copy(self.containers, original)["stopped"])
        self.containers[1]["State"]["Status"] = "exited"
        self.assertTrue(qualify_retained_copy(self.containers, original)["stopped"])
        self.containers[1]["Mounts"][0]["Name"] = "foreign-data"
        with self.assertRaises(ValueError):
            qualify_retained_copy(self.containers, original)

    def test_old_password_knowledge_is_not_a_prerequisite_for_rebuilding_with_local_credentials(
        self,
    ) -> None:
        self.containers[0]["Config"]["Env"] = [
            item
            for item in self.containers[0]["Config"]["Env"]
            if item.split("=", 1)[0] not in FRESH_TESTING_LOCAL_CREDENTIAL_KEYS
        ]
        self.assertEqual(
            qualify_retained_copy(self.containers, self.spec)["identity"], self.identity
        )

    def test_foreign_storage_consumers_or_another_lane_in_the_project_refuse(self) -> None:
        with self.assertRaises(ValueError):
            qualify_retained_copy(
                [*self.containers, {"Mounts": deepcopy(self.containers[1]["Mounts"])}], self.spec
            )
        self.containers[1]["Config"]["Env"] = ["PLATFORM_INSTANCE=prod"]
        with self.assertRaises(ValueError):
            qualify_retained_copy(self.containers, self.spec)

    def test_foreign_identity_or_database_refuses(self) -> None:
        for update in ({"context": "foreign"}, {"identity": {**self.identity, "instance": "prod"}}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                qualify_retained_copy(self.containers, {**self.spec, **update})

    def test_unowned_service_in_project_cannot_be_included_in_stack_retirement(self) -> None:
        extra = deepcopy(self.containers[1])
        extra["Id"] = "another-application"
        extra["Config"]["Labels"]["com.docker.compose.service"] = "another-application"
        with self.assertRaises(ValueError):
            qualify_retained_copy([*self.containers, extra], self.spec)
        self.containers[0]["Config"]["Env"].append("ODOO_DB_HOST=production-database")
        with self.assertRaises(ValueError):
            qualify_retained_copy(self.containers, self.spec)

    def run_host(
        self,
        *,
        mode: str = "read",
        deny: bool = False,
        extra: dict[str, Any] | None = None,
        inspect_result: list[dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], list[list[str]]]:
        payload = {**self.spec, "mode": mode, "identity": self.identity, **(extra or {})}
        commands: list[list[str]] = []

        def run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
            commands.append(command)
            if deny:
                return subprocess.CompletedProcess(
                    command, 1, stdout=b"", stderr=b"permission denied"
                )
            if command[1:3] == ["ps", "-aq"]:
                output = "\n".join(item["Id"] for item in self.containers).encode()
            elif command[1] == "inspect":
                output = json.dumps(
                    inspect_result
                    if inspect_result is not None
                    else [item for item in self.containers if item["Id"] in command[2:]]
                ).encode()
            elif command[1:3] == ["volume", "inspect"]:
                name = command[-1]
                output = json.dumps(
                    [{"Driver": "local", "Options": {}, "Mountpoint": str(self.root / name)}]
                ).encode()
            elif command[1] == "exec":
                self.assertEqual(command[2], self.containers[0]["Id"])
                spec = json.loads(base64.b64decode(command[-1]))
                self.assertEqual(spec["identity"], self.identity)
                self.assertEqual(spec["path"], payload["health_path"])
                environment = dict(
                    item.split("=", 1)
                    for item in self.containers[0]["Config"]["Env"]
                    if "=" in item
                )
                self.assertEqual(spec["port"], int(environment["ODOO_HTTP_PORT"]))
                output = b""
            else:
                self.fail("Unexpected retained-copy provider mutation")
            return subprocess.CompletedProcess(command, 0, stdout=output, stderr=b"")

        stdout = io.StringIO()
        with (
            redirect_stdout(stdout),
            patch.object(
                sys,
                "argv",
                ["fixture", base64.b64encode(json.dumps(payload).encode()).decode(), "FRAME:"],
            ),
            patch.object(subprocess, "run", side_effect=run),
        ):
            try:
                exec(compile(retained_copy_host_program(), "<retained-copy-host>", "exec"), {})
            except SystemExit as error:
                self.assertTrue(deny)
                self.assertEqual(error.code, 1)
        for key in FRESH_TESTING_LOCAL_CREDENTIAL_KEYS:
            self.assertNotIn("fixture-" + key, stdout.getvalue())
        return json.loads(stdout.getvalue().removeprefix("FRAME:")), commands

    def test_host_read_binds_actual_storage_and_private_probe_uses_the_managed_port(self) -> None:
        read, _ = self.run_host()
        self.assertEqual(read["type"], "complete")
        frame, commands = self.run_host(
            mode="readiness", extra={"physical_binding": read["physical_binding"]}
        )
        self.assertTrue(frame["healthy"])
        self.assertFalse(any(command[1] in {"stop", "rm"} for command in commands))
        self.assertEqual(frame["physical_binding"], read["physical_binding"])

    def test_host_read_refuses_a_bind_alias_of_the_testing_data(self) -> None:
        self.containers.append(
            {
                "Id": "foreign",
                "Config": {"Labels": {}},
                "Mounts": [{"Type": "bind", "Source": str(self.root / "web-volume"), "RW": True}],
            }
        )
        self.assertEqual(self.run_host()[0]["type"], "failed")

    def test_host_read_reports_denial_and_exposes_no_writer_mode(self) -> None:
        frame, commands = self.run_host(deny=True)
        self.assertEqual(frame, {"type": "denied"})
        self.assertEqual(len(commands), 1)
        frame, commands = self.run_host(mode="stop")
        self.assertEqual(frame, {"type": "failed"})
        self.assertFalse(any(command[1] in {"stop", "rm"} for command in commands))

    def test_stopped_inventory_is_bound_without_starting_a_copy_or_reading_health(self) -> None:
        for item in self.containers:
            item["State"]["Status"] = "exited"
        read, commands = self.run_host(extra={"allow_unavailable": True})
        self.assertEqual(read["availability"], "stopped")
        self.assertEqual(read["identity"], self.identity)
        self.assertTrue(read["physical_binding"])
        self.assertFalse(any(command[1] in {"exec", "start", "stop", "rm"} for command in commands))
        self.assertEqual(
            self.run_host(
                extra={"allow_unavailable": True, "unavailable_sha256": read["observation_sha256"]}
            )[0],
            read,
        )
        for item in self.containers:
            item["State"].update(
                Pid=0,
                ExitCode=1,
                StartedAt="2026-10-20T11:00:00Z",
                FinishedAt="2026-10-20T11:01:00Z",
            )
        self.assertEqual(
            self.run_host(
                extra={"allow_unavailable": True, "unavailable_sha256": read["observation_sha256"]}
            )[0],
            read,
        )
        self.containers[0]["Id"] = "different-stopped-web"
        self.assertEqual(
            self.run_host(
                extra={"allow_unavailable": True, "unavailable_sha256": read["observation_sha256"]}
            )[0]["type"],
            "failed",
        )

    def test_absence_requires_complete_inventory_and_never_invents_old_resources(self) -> None:
        self.assertEqual(
            self.run_host(extra={"allow_unavailable": True}, inspect_result=[])[0]["type"], "failed"
        )
        self.containers = []
        read, commands = self.run_host(extra={"allow_unavailable": True})
        self.assertEqual(read["availability"], "absent")
        for field in ("identity", "web_id", "physical_binding", "resource_binding"):
            self.assertNotIn(field, read)
        self.assertTrue(read["observation_sha256"])
        self.assertFalse(any(command[1] in {"exec", "start", "stop", "rm"} for command in commands))
        with self.assertRaises(ValueError):
            qualify_retained_copy([], self.spec)

    def test_matching_ids_with_missing_inspect_fields_cannot_establish_absence_or_isolation(
        self,
    ) -> None:
        for field in ("Config", "Mounts", "State"):
            with self.subTest(field=field):
                incomplete = deepcopy(self.containers)
                for item in incomplete:
                    item.pop(field)
                self.assertEqual(
                    self.run_host(extra={"allow_unavailable": True}, inspect_result=incomplete)[0][
                        "type"
                    ],
                    "failed",
                )

    def test_stopped_legacy_copy_needs_no_fabricated_runtime_identity(self) -> None:
        for item in self.containers:
            item["State"]["Status"] = "exited"
        self.containers[0]["Config"]["Env"] = [
            item
            for item in self.containers[0]["Config"]["Env"]
            if not item.startswith("LAUNCHPLANE_RUNTIME_IDENTITY_JSON=")
        ]
        read, commands = self.run_host(extra={"allow_unavailable": True, "identity": None})
        self.assertEqual(read["type"], "complete")
        self.assertEqual(read["availability"], "stopped")
        self.assertIsNone(read["identity"])
        self.assertFalse(any(command[1] == "exec" for command in commands))
