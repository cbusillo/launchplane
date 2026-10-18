"""Actual container inventory must qualify only the retained testing web process."""

from copy import deepcopy
import json
import unittest
from typing import Any

from control_plane.dokploy.testing_retained_copy import qualify_retained_copy
from control_plane.odoo_testing_restore import FRESH_TESTING_LOCAL_CREDENTIAL_KEYS


class RetainedTestingCopyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec: dict[str, Any] = {"context": "example", "project": "old-testing"}
        self.identity = {"context": "example", "instance": "testing", "artifact_id": "old-build"}
        self.containers: list[dict[str, Any]] = []
        for service, destination in (
            ("web", "/volumes/data"),
            ("database", "/var/lib/postgresql/data"),
        ):
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
                            "Name": service + "-volume",
                            "RW": True,
                        }
                    ],
                    "State": {"Status": "running"},
                }
            )
        self.containers[0]["Config"]["Env"] = [
            "PLATFORM_INSTANCE=testing",
            "ODOO_DB_HOST=database",
            "LAUNCHPLANE_RUNTIME_IDENTITY_JSON=" + json.dumps(self.identity),
            *(key + "=fixture-" + key for key in FRESH_TESTING_LOCAL_CREDENTIAL_KEYS),
        ]

    def test_only_web_retirement_is_bound_to_actual_identity_and_both_data_volumes(self) -> None:
        read = qualify_retained_copy(self.containers, self.spec)
        self.assertFalse(read["stopped"])
        original = {
            **self.spec,
            **{key: read[key] for key in ("identity", "web_id", "resource_binding")},
        }
        self.containers[0]["State"]["Status"] = "exited"
        self.assertTrue(qualify_retained_copy(self.containers, original)["stopped"])
        self.assertEqual(self.containers[1]["State"]["Status"], "running")
        self.containers[1]["Mounts"][0]["Name"] = "foreign-volume"
        with self.assertRaises(ValueError):
            qualify_retained_copy(self.containers, original)

    def test_any_foreign_storage_consumer_or_credential_overlap_refuses(self) -> None:
        for foreign in (
            {"Mounts": deepcopy(self.containers[1]["Mounts"])},
            {
                "Config": {
                    "Env": ["DATABASE_URL=postgresql://user:fixture-ODOO_DB_PASSWORD@production/db"]
                }
            },
        ):
            with self.subTest(foreign=foreign), self.assertRaises(ValueError):
                qualify_retained_copy([*self.containers, foreign], self.spec)

    def test_candidate_credentials_do_not_excuse_shared_storage(self) -> None:
        candidate = deepcopy(self.containers[0])
        candidate["Id"] = "candidate-web"
        candidate["Config"]["Labels"]["com.docker.compose.project"] = "candidate-project"
        candidate["Mounts"][0]["Name"] = "candidate-data"
        spec = {**self.spec, "independent_candidate_projects": ["candidate-project"]}
        qualify_retained_copy([*self.containers, candidate], spec)
        candidate["Mounts"][0]["Name"] = self.containers[0]["Mounts"][0]["Name"]
        with self.assertRaises(ValueError):
            qualify_retained_copy([*self.containers, candidate], spec)

    def test_foreign_runtime_database_or_replaced_process_refuses(self) -> None:
        for update in (
            {"context": "other"},
            {"web_id": "other"},
            {"identity": {**self.identity, "instance": "prod"}},
        ):
            with self.subTest(update=update), self.assertRaises(ValueError):
                qualify_retained_copy(self.containers, {**self.spec, **update})
        self.containers[0]["Config"]["Env"].append("ODOO_DB_HOST=production-database")
        with self.assertRaises(ValueError):
            qualify_retained_copy(self.containers, self.spec)

    def run_host(
        self, *, deny: bool = False, mode: str = "stop"
    ) -> tuple[dict[str, Any], list[list[str]]]:
        import base64
        from contextlib import redirect_stdout
        import io
        import subprocess
        import sys
        from unittest.mock import patch
        from control_plane.dokploy.testing_retained_copy import retained_copy_host_program

        original = qualify_retained_copy(self.containers, self.spec)
        payload = {
            **self.spec,
            **{key: original[key] for key in ("identity", "web_id", "resource_binding")},
            "mode": mode,
            "health_path": "/launchplane/health",
        }
        commands: list[list[str]] = []

        def run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
            commands.append(command)
            if command[1:3] == ["ps", "-aq"]:
                output = "\n".join(item["Id"] for item in self.containers).encode()
            elif command[1] == "inspect":
                output = json.dumps(
                    [item for item in self.containers if item["Id"] in command[2:]]
                ).encode()
            elif command[1] == "exec":
                self.assertEqual(command[2], original["web_id"])
                spec = json.loads(base64.b64decode(command[-1]))
                self.assertEqual(spec["identity"], original["identity"])
                self.assertEqual(spec["path"], payload["health_path"])
                configured = dict(
                    item.split("=", 1)
                    for item in self.containers[0]["Config"]["Env"]
                    if "=" in item
                )
                self.assertEqual(spec["port"], int(configured["ODOO_HTTP_PORT"]))
                output = b""
            elif command[1] == "stop":
                self.assertEqual(command[-1], original["web_id"])
                if deny:
                    return subprocess.CompletedProcess(
                        command, 1, stdout=b"", stderr=b"permission denied"
                    )
                self.containers[0]["State"]["Status"] = "exited"
                output = b""
            else:
                self.fail("Unexpected retained-copy provider operation")
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
        frame = json.loads(stdout.getvalue().removeprefix("FRAME:"))
        for key in FRESH_TESTING_LOCAL_CREDENTIAL_KEYS:
            self.assertNotIn("fixture-" + key, stdout.getvalue())
        return frame, commands

    def test_emitted_host_program_stops_only_the_original_web_and_keeps_its_data(self) -> None:
        mounts = deepcopy([item["Mounts"] for item in self.containers])
        frame, commands = self.run_host()
        self.assertTrue(frame["stopped"])
        self.assertEqual(
            [command[-1] for command in commands if command[1] == "stop"],
            [self.containers[0]["Id"]],
        )
        self.assertEqual([item["Mounts"] for item in self.containers], mounts)
        self.assertEqual(self.containers[1]["State"]["Status"], "running")

    def test_emitted_host_program_reports_denial_without_an_alternate_stop(self) -> None:
        frame, commands = self.run_host(deny=True)
        self.assertEqual(frame, {"type": "denied"})
        self.assertEqual(len([command for command in commands if command[1] == "stop"]), 1)
        self.assertEqual(self.containers[0]["State"]["Status"], "running")

    def test_emitted_host_private_probe_uses_the_original_container_and_managed_port(self) -> None:
        self.containers[0]["Config"]["Env"].append("ODOO_HTTP_PORT=8171")
        frame, commands = self.run_host(mode="readiness")
        self.assertTrue(frame["healthy"])
        self.assertFalse(frame["stopped"])
        self.assertFalse(any(command[1] == "stop" for command in commands))
