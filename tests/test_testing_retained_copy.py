"""Retirement reads prove owned data and every old service without reading passwords."""

import base64
from contextlib import redirect_stdout
from copy import deepcopy
import io
from itertools import product
import json
import os
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
from control_plane.copy_readback_diagnostics import CopyReadbackRefused, ReadbackPredicate


def compose_created_web_command() -> list[str]:
    import re
    import yaml
    from control_plane.dokploy.compose import render_odoo_raw_compose_file

    rendered = yaml.safe_load(
        render_odoo_raw_compose_file(
            image_reference="ghcr.io/example/odoo@sha256:" + "a" * 64,
            hold_web_until_integration_readback=True,
        )
    )
    # https://docs.docker.com/reference/compose-file/interpolation/
    interpolation = re.compile(r"\$\$|\$\{[A-Z_]+:-([^}]*)}")
    return [
        interpolation.sub(lambda match: "$" if match[0] == "$$" else match[1], value)
        for value in rendered["services"]["web"]["command"]
    ]


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
        self.web_processes: list[str] = []
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

    def test_failed_candidate_detachment_requires_original_members_and_no_active_exec_or_web(
        self,
    ) -> None:
        from control_plane.dokploy.testing_retained_copy import qualify_failed_testing_containers
        from control_plane.odoo_testing_resources import OdooTestingResources
        from control_plane.contracts.testing_candidate import testing_candidate_namespace

        namespace = testing_candidate_namespace("fixture", "example", "failed-original")
        resources = OdooTestingResources(
            product="fixture",
            context="example",
            operation_id="failed-original",
            target_id="failed-compose",
            namespace=namespace,
        )
        spec = {
            **self.spec,
            "project": namespace,
            "identity": self.identity,
            "failed_candidate": {
                "resources": resources.model_dump(mode="json"),
                "image": "fixture@sha256:" + "a" * 64,
            },
        }
        for position, row in enumerate(self.containers):
            role = row["Config"]["Labels"]["com.docker.compose.service"]
            row["Id"] = str(position + 1) * 64
            row["Image"] = "sha256:" + "b" * 64
            row["Config"]["Image"] = spec["failed_candidate"]["image"]
            row["Config"]["Labels"]["com.docker.compose.project"] = namespace
            row["Mounts"][0]["Name"] = resources.volumes[
                "ODOO_DATA_VOLUME" if role == "web" else "ODOO_DB_VOLUME"
            ]
            row["HostConfig"] = {"PortBindings": {}}
            row["State"] = {
                "Status": "exited" if role == "web" else "running",
                "StartedAt": "fixture-start",
            }
            row["ExecIDs"] = []
        original = qualify_failed_testing_containers(self.containers, spec)
        self.assertEqual(original["container_ids"], sorted(row["Id"] for row in self.containers))
        for field, value in (("ExecIDs", ["active-job"]), ("Image", "sha256:" + "c" * 64)):
            changed = deepcopy(self.containers)
            changed[1][field] = value
            with self.assertRaises(ValueError):
                qualify_failed_testing_containers(
                    changed, {**spec, "container_snapshot": original["container_snapshot"]}
                )
        self.containers[0]["State"]["Status"] = "running"
        with self.assertRaises(ValueError):
            qualify_failed_testing_containers(self.containers, spec)
        for row in self.containers:
            row["State"]["Status"] = "exited"
            mount = row["Mounts"][0]
            destination = self.root / mount["Name"]
            Path(mount["Source"]).rename(destination)
            mount["Source"] = str(destination)
        self.spec = spec
        read, _ = self.run_host(extra={"allow_unavailable": True})
        self.assertEqual(read["type"], "complete")
        original = {
            "allow_unavailable": True,
            **{
                key: read[key]
                for key in (
                    "container_ids",
                    "container_snapshot",
                    "resource_binding",
                    "physical_binding",
                )
            },
        }
        refused, _ = self.run_host(mode="remove", extra=original, remove_refused=True)
        self.assertEqual(refused["availability"], "incomplete")
        self.assertEqual(sorted(row["Id"] for row in self.containers), original["container_ids"])
        removed, commands = self.run_host(mode="remove", extra=original)
        self.assertEqual(removed["availability"], "absent")
        self.assertTrue(
            all(command[1:3] == ["--host", "unix:///var/run/docker.sock"] for command in commands)
        )
        readback, commands = self.run_host(mode="removal-readback", extra=original)
        self.assertEqual(readback["availability"], "absent")
        self.assertFalse(any("rm" in command for command in commands))

    def test_held_failed_candidate_web_created_by_compose_qualifies_only_while_waiting(
        self,
    ) -> None:
        from control_plane.dokploy.compose import held_odoo_web_container_command
        from control_plane.integration_readback import INTEGRATION_READBACK_PASSED_PATH
        from control_plane.odoo_testing_resources import OdooTestingResources
        from control_plane.contracts.testing_candidate import testing_candidate_namespace

        namespace = testing_candidate_namespace("fixture", "example", "failed-original")
        resources = OdooTestingResources(
            product="fixture",
            context="example",
            operation_id="failed-original",
            target_id="failed-compose",
            namespace=namespace,
        )
        self.spec = {
            **self.spec,
            "project": namespace,
            "failed_candidate": {
                "resources": resources.model_dump(mode="json"),
                "image": "fixture@sha256:" + "a" * 64,
                "web_command": held_odoo_web_container_command(),
                "web_gate_path": INTEGRATION_READBACK_PASSED_PATH,
            },
        }
        for position, row in enumerate(self.containers):
            role = row["Config"]["Labels"]["com.docker.compose.service"]
            mount = row["Mounts"][0]
            mount["Name"] = resources.volumes[
                "ODOO_DATA_VOLUME" if role == "web" else "ODOO_DB_VOLUME"
            ]
            destination = self.root / mount["Name"]
            Path(mount["Source"]).rename(destination)
            mount["Source"] = str(destination.resolve())
            row.update(
                {
                    "Id": str(position + 1) * 64,
                    "Image": "sha256:" + "b" * 64,
                    "HostConfig": {"PortBindings": {}},
                    "State": {"Status": "running", "StartedAt": "fixture-start"},
                    "ExecIDs": None,
                }
            )
            row["Config"]["Image"] = self.spec["failed_candidate"]["image"]
            row["Config"]["Labels"]["com.docker.compose.project"] = namespace
        self.containers[0]["Config"]["Cmd"] = compose_created_web_command()

        self.web_processes = ["sh", "sleep"]
        waiting, calls = self.run_host(extra={"allow_unavailable": True})
        self.assertEqual(waiting["type"], "complete", waiting)
        observations: dict[str, Any] = {}
        observed, observed_calls = self.run_host(
            extra={"allow_unavailable": True}, evidence=observations
        )
        self.assertEqual(observed, waiting)
        self.assertEqual(observed_calls, calls)
        self.assertEqual(len(observations["containers"]), len(self.containers))
        from control_plane.contracts.canonical_json import canonical_json_sha256

        self.assertEqual(
            {row["container_sha256"] for row in observations["containers"]},
            {canonical_json_sha256(row["Id"]) for row in self.containers},
        )
        self.assertEqual(observations["containers"][0]["checks"]["web_process"], "pass")
        from control_plane.contracts.candidate_custody_evidence import LifecycleEvidence

        LifecycleEvidence.model_validate(observations)
        self.web_processes = ["sh", "python3"]
        observations = {}
        serving, _ = self.run_host(extra={"allow_unavailable": True}, evidence=observations)
        self.assertEqual(serving["type"], "failed")
        self.assertEqual(len(observations["containers"]), len(self.containers))
        from control_plane.contracts.canonical_json import canonical_json_sha256

        self.assertEqual(
            {row["container_sha256"] for row in observations["containers"]},
            {canonical_json_sha256(row["Id"]) for row in self.containers},
        )
        self.assertEqual(observations["containers"][0]["checks"]["web_process"], "fail")
        self.assertEqual(observations["checks"]["lifecycle_acquisition"], "unavailable")
        LifecycleEvidence.model_validate(observations)
        for value in (self.spec["project"], self.containers[0]["Id"], str(self.root)):
            self.assertNotIn(value, json.dumps(observations))

    def test_failed_candidate_without_web_keeps_exact_original_runtime_custody(self) -> None:
        from control_plane.odoo_testing_resources import OdooTestingResources
        from control_plane.contracts.testing_candidate import testing_candidate_namespace

        namespace = testing_candidate_namespace("fixture", "example", "failed-original")
        resources = OdooTestingResources(
            product="fixture",
            context="example",
            operation_id="failed-original",
            target_id="failed-compose",
            namespace=namespace,
        )
        runner = self.containers[0]
        runner["Config"]["Labels"]["com.docker.compose.service"] = "script-runner"
        for index, row in enumerate(self.containers):
            row.update(
                Id=str(index + 1) * 64,
                Image="sha256:" + "b" * 64,
                HostConfig={"PortBindings": {}},
                ExecIDs=[],
            )
            row["State"].update(Status="exited", StartedAt="fixture-start")
            row["Config"].update(Image="fixture@sha256:" + "a" * 64)
            row["Config"]["Labels"]["com.docker.compose.project"] = namespace
            mount = row["Mounts"][0]
            name = resources.volumes["ODOO_DATA_VOLUME" if index == 0 else "ODOO_DB_VOLUME"]
            Path(mount["Source"]).rename(self.root / name)
            mount.update(Name=name, Source=str(self.root / name))
        self.spec.update(
            project=namespace,
            failed_candidate={
                "resources": resources.model_dump(mode="json"),
                "image": runner["Config"]["Image"],
            },
        )
        result, _ = self.run_host(extra={"allow_unavailable": True})
        self.assertEqual(result["type"], "complete")
        runner["Config"]["Env"] = []
        refused, _ = self.run_host(extra={"allow_unavailable": True})
        self.assertEqual(refused["type"], "failed")

    def own_web_spec(
        self, **failed_candidate: object
    ) -> tuple[dict[str, Any], dict[str, Any], Path]:
        from control_plane.dokploy.compose import held_odoo_web_container_command
        from control_plane.integration_readback import INTEGRATION_READBACK_PASSED_PATH

        web = self.containers[0]
        web["Config"]["Cmd"] = compose_created_web_command()
        web["Mounts"][0]["Name"] = "fixture-data"
        web["Mounts"][0]["Source"] = str(Path(web["Mounts"][0]["Source"]).resolve())
        spec = {
            "identity": self.identity,
            "failed_candidate": {
                "resources": {"namespace": "fixture"},
                "web_command": held_odoo_web_container_command(),
                "web_gate_path": INTEGRATION_READBACK_PASSED_PATH,
                **failed_candidate,
            },
        }
        gate = Path(web["Mounts"][0]["Source"]) / Path(INTEGRATION_READBACK_PASSED_PATH).name
        return web, spec, gate

    def test_web_evidence_preserves_gate_and_process_refusals_without_exporting_inputs(
        self,
    ) -> None:
        from control_plane.dokploy.testing_retained_copy import failed_candidate_own_web

        web, spec, gate = self.own_web_spec()
        evidence: dict[str, str] = {}
        self.assertFalse(failed_candidate_own_web(web, spec, lambda _: ["sh", "odoo"], evidence))
        self.assertEqual(evidence["web_gate"], "pass")
        self.assertEqual(evidence["web_process"], "fail")
        gate.write_text("private-gate-content")
        evidence = {}
        self.assertFalse(failed_candidate_own_web(web, spec, lambda _: ["sh", "sleep"], evidence))
        self.assertEqual(evidence["web_gate"], "fail")
        self.assertNotIn("web_process", evidence)
        self.assertNotIn("private-gate-content", json.dumps(evidence))

    def test_waiting_web_requires_original_command_processes_and_absent_gate(self) -> None:
        from control_plane.dokploy.testing_retained_copy import failed_candidate_own_web

        web, spec, gate = self.own_web_spec()
        self.assertTrue(failed_candidate_own_web(web, spec, lambda _: ["sh", "sleep"]))
        self.assertFalse(failed_candidate_own_web(web, spec, lambda _: ["sh", "odoo"]))
        gate.write_text("completed-copy-evidence")
        self.assertFalse(failed_candidate_own_web(web, spec, lambda _: ["sh", "sleep"]))

    def test_completed_copy_web_qualifies_only_with_its_own_identity_mount_and_gate(self) -> None:
        from control_plane.dokploy.testing_retained_copy import failed_candidate_own_web

        web, spec, gate = self.own_web_spec(completed=True)

        def serving(_: str) -> list[str]:
            return ["sh", "python3"]

        self.assertFalse(failed_candidate_own_web(web, spec, serving))
        gate.write_text("completed-copy-evidence")
        self.assertTrue(failed_candidate_own_web(web, spec, serving))
        foreign = {**spec, "identity": {**self.identity, "artifact_id": "foreign"}}
        self.assertFalse(failed_candidate_own_web(web, foreign, serving))
        web["Mounts"][0]["Name"] = "foreign-data"
        self.assertFalse(failed_candidate_own_web(web, spec, serving))
        web["Mounts"][0]["Name"] = "fixture-data"
        gate.unlink()
        gate.symlink_to(web["Mounts"][0]["Source"])
        self.assertFalse(failed_candidate_own_web(web, spec, serving))

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
        with self.assertRaises(CopyReadbackRefused) as raised:
            qualify_retained_copy(self.containers, original)
        self.assertEqual(raised.exception.predicate, ReadbackPredicate.CUSTODY)

    def test_unavailable_web_does_not_need_its_database_to_stop_before_rebuild(self) -> None:
        self.containers[0]["State"]["Status"] = "exited"
        frame, _ = self.run_host(extra={"allow_unavailable": True})
        self.assertEqual(frame["availability"], "stopped")
        self.assertFalse(frame["stopped"])
        self.assertTrue(frame["physical_binding"])

    def test_unhealthy_running_web_uses_the_same_unavailable_copy_path(self) -> None:
        self.containers[0]["State"]["Health"] = {"Status": "unhealthy"}
        self.containers[0]["State"]["StartedAt"] = "fixture-original-start"
        frame, _ = self.run_host(extra={"allow_unavailable": True})
        self.assertEqual(frame["availability"], "stopped")
        self.assertFalse(frame["stopped"])
        self.assertTrue(frame["physical_binding"])
        self.assertEqual(
            frame["web_process"],
            {
                "id": self.containers[0]["Id"],
                "status": "running",
                "started_at": self.containers[0]["State"]["StartedAt"],
                "health": "unhealthy",
            },
        )

    def test_removed_web_preserves_database_custody_without_an_original_runtime(self) -> None:
        self.containers.pop(0)
        frame, _ = self.run_host(extra={"allow_unavailable": True, "identity": None})
        self.assertEqual(frame["type"], "complete")
        self.assertEqual(frame["availability"], "stopped")
        self.assertIsNone(frame.get("identity"))
        self.assertFalse(frame["stopped"])
        self.assertTrue(frame["physical_binding"])

    def test_admitted_unhealthy_process_can_recover_without_inventing_old_identity(self) -> None:
        self.containers[0]["State"].update(
            StartedAt="fixture-original-start", Health={"Status": "unhealthy"}
        )
        self.containers[0]["Config"]["Env"] = [
            item
            for item in self.containers[0]["Config"]["Env"]
            if not item.startswith("LAUNCHPLANE_RUNTIME_IDENTITY_JSON=")
        ]
        admitted, _ = self.run_host(extra={"allow_unavailable": True, "identity": None})
        self.containers[0]["State"]["Health"]["Status"] = "healthy"
        current, _ = self.run_host(
            extra={
                "allow_unavailable": True,
                "identity": None,
                "unavailable_web_process": admitted["web_process"],
            }
        )
        self.assertEqual(current["type"], "complete")
        self.assertIsNone(current["identity"])
        self.assertEqual(current["physical_binding"], admitted["physical_binding"])
        self.containers[0]["State"]["StartedAt"] = "replacement-start"
        refused, _ = self.run_host(
            extra={
                "allow_unavailable": True,
                "identity": None,
                "unavailable_web_process": admitted["web_process"],
            }
        )
        self.assertEqual(refused["type"], "failed")

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
        with self.assertRaises(CopyReadbackRefused):
            qualify_retained_copy(
                [*self.containers, {"Mounts": deepcopy(self.containers[1]["Mounts"])}], self.spec
            )
        self.containers[1]["Config"]["Env"] = ["PLATFORM_INSTANCE=prod"]
        with self.assertRaises(CopyReadbackRefused):
            qualify_retained_copy(self.containers, self.spec)

    def test_foreign_identity_or_database_refuses(self) -> None:
        for update in ({"context": "foreign"}, {"identity": {**self.identity, "instance": "prod"}}):
            with self.subTest(update=update), self.assertRaises(CopyReadbackRefused):
                qualify_retained_copy(self.containers, {**self.spec, **update})

    def test_unowned_service_in_project_cannot_be_included_in_stack_retirement(self) -> None:
        extra = deepcopy(self.containers[1])
        extra["Id"] = "another-application"
        extra["Config"]["Labels"]["com.docker.compose.service"] = "another-application"
        with self.assertRaises(CopyReadbackRefused):
            qualify_retained_copy([*self.containers, extra], self.spec)
        self.containers[0]["Config"]["Env"].append("ODOO_DB_HOST=production-database")
        with self.assertRaises(CopyReadbackRefused):
            qualify_retained_copy(self.containers, self.spec)

    def run_host(
        self,
        *,
        mode: str = "read",
        deny: bool = False,
        remove_refused: bool = False,
        extra: dict[str, Any] | None = None,
        inspect_result: list[dict[str, Any]] | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], list[list[str]]]:
        payload: dict[str, Any] = {
            **self.spec,
            "mode": mode,
            "identity": self.identity,
            **(extra or {}),
        }
        if evidence is not None:
            payload["custody_evidence"] = True
        commands: list[list[str]] = []

        def run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
            commands.append(command)
            if payload.get("failed_candidate"):
                self.assertEqual(command[1:3], ["--host", "unix:///var/run/docker.sock"])
                command = [command[0], *command[3:]]
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
                from control_plane.contracts.canonical_json import canonical_json_sha256

                output = json.dumps(
                    [
                        {
                            "Driver": "local",
                            "Options": {},
                            "Mountpoint": str(self.root / name),
                            "Labels": {
                                "io.launchplane.testing-resources": canonical_json_sha256(
                                    payload["failed_candidate"]["resources"]
                                )
                            }
                            if payload.get("failed_candidate")
                            else {},
                        }
                    ]
                ).encode()
            elif command[1] == "top":
                if "pid" not in command[-1].split(","):
                    return subprocess.CompletedProcess(
                        command, 1, stdout=b"", stderr=b"Couldn't find PID field in ps output"
                    )
                output = "\n".join(
                    ["PID                 COMMAND"]
                    + [
                        f"{100 + index}                 {name}"
                        for index, name in enumerate(self.web_processes)
                    ]
                ).encode()
            elif command[1] == "rm":
                if remove_refused:
                    return subprocess.CompletedProcess(
                        command, 1, stdout=b"", stderr=b"container removal refused"
                    )
                self.assertNotIn("-f", command)
                self.assertNotIn("-v", command)
                self.assertTrue(set(command[2:]) <= {item["Id"] for item in self.containers})
                self.containers[:] = [
                    item for item in self.containers if item["Id"] not in command[2:]
                ]
                output = b""
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
            patch.dict(os.environ),
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
        frames = [
            json.loads(line.removeprefix("FRAME:")) for line in stdout.getvalue().splitlines()
        ]
        if evidence is not None:
            for frame in frames[:-1]:
                self.assertEqual(frame["type"], "custody_evidence")
                evidence.update(frame["evidence"])
        return frames[-1], commands

    def test_command_failure_after_inventory_retains_private_output_bound_to_the_input(
        self,
    ) -> None:
        from control_plane.copy_readback_diagnostics import (
            COPY_FAILURE_STATE_DIRECTORY,
            COPY_FAILURE_EVIDENCE_MARKER,
            COPY_FAILURE_EXIT_MARKER,
        )
        import hashlib

        spec = {**self.spec, "identity": self.identity}
        private = b"password=fixture-secret /credential/path dump-content"

        def command(arguments: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
            if arguments[1:3] == ["ps", "-aq"]:
                data = "\n".join(item["Id"] for item in self.containers).encode()
            elif arguments[1] == "inspect":
                data = json.dumps(self.containers).encode()
            else:
                self.assertEqual(arguments[1:3], ["volume", "inspect"])
                return subprocess.CompletedProcess(arguments, 7, stdout=b"", stderr=private)
            return subprocess.CompletedProcess(arguments, 0, stdout=data, stderr=b"")

        home = self.root / "private-home"
        home.mkdir()
        output = io.StringIO()
        with (
            patch.object(Path, "home", return_value=home),
            patch.object(subprocess, "run", side_effect=command),
            patch.object(
                sys,
                "argv",
                ["fixture", base64.b64encode(json.dumps(spec).encode()).decode(), "FRAME:"],
            ),
            redirect_stdout(output),
        ):
            exec(compile(retained_copy_host_program(), "<retained-copy-host>", "exec"), {})
        frame = json.loads(output.getvalue().removeprefix("FRAME:"))
        self.assertNotIn(private.decode(), output.getvalue())
        identity = frame[COPY_FAILURE_EVIDENCE_MARKER]
        evidence = json.loads(
            (home / COPY_FAILURE_STATE_DIRECTORY / (identity + ".json")).read_text()
        )
        self.assertEqual(base64.b64decode(evidence["stderr"]), private)
        self.assertEqual(
            evidence["input_sha256"],
            hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest(),
        )
        self.assertEqual(frame[COPY_FAILURE_EXIT_MARKER], str(evidence["exit_status"]))

    def test_host_read_binds_actual_storage_and_private_probe_uses_the_managed_port(self) -> None:
        read, _ = self.run_host()
        self.assertEqual(read["type"], "complete")
        frame, commands = self.run_host(
            mode="readiness", extra={"physical_binding": read["physical_binding"]}
        )
        self.assertTrue(frame["healthy"])
        self.assertFalse(any(command[1] in {"stop", "rm"} for command in commands))
        self.assertEqual(frame["physical_binding"], read["physical_binding"])
        frame, _ = self.run_host(extra={"physical_binding": "changed"})
        self.assertEqual(frame["predicate"], ReadbackPredicate.BACKING_STORAGE)
        self.assertEqual(frame["kind"], "refused")
        self.containers[0]["State"]["Status"] = "exited"
        frame, _ = self.run_host(
            mode="readiness", extra={"physical_binding": read["physical_binding"]}
        )
        self.assertEqual(frame["predicate"], ReadbackPredicate.RUNTIME)
        self.assertEqual(frame["kind"], "refused")

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
        self.assertEqual(frame["kind"], "unavailable")
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
        with self.assertRaises(CopyReadbackRefused) as raised:
            qualify_retained_copy([], self.spec)
        self.assertEqual(raised.exception.predicate, ReadbackPredicate.SERVICES)

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

    def test_rebuild_removes_only_admitted_stopped_containers_and_keeps_volumes(self) -> None:
        for item in self.containers:
            item["State"]["Status"] = "exited"
        expected = [item["Id"] for item in self.containers]
        volumes = [Path(item["Mounts"][0]["Source"]) for item in self.containers]
        frame, commands = self.run_host(
            mode="remove", extra={"allow_unavailable": True, "container_ids": expected}
        )
        self.assertEqual(frame["type"], "complete")
        self.assertEqual(self.containers, [])
        self.assertTrue(all(volume.is_dir() for volume in volumes))
        self.assertTrue(any(command[1] == "rm" for command in commands))
        replay, commands = self.run_host(
            mode="remove", extra={"allow_unavailable": True, "container_ids": expected}
        )
        self.assertEqual(replay["availability"], "absent")
        self.assertFalse(any(command[1] == "rm" for command in commands))

    def test_removal_distinguishes_changed_custody_from_unknown_inventory(self) -> None:
        expected = [item["Id"] for item in self.containers]
        for mode, change in product(
            ("remove", "removal-readback"), ("running", "foreign", "incomplete", "unknown")
        ):
            with self.subTest(mode=mode, change=change):
                for item in self.containers:
                    item["State"]["Status"] = "exited"
                extra = {"allow_unavailable": True, "container_ids": expected}
                if change == "running":
                    self.containers[0]["State"]["Status"] = "running"
                if change == "foreign":
                    extra["container_ids"] = ["another-copy"]
                if change == "unknown":
                    self.containers[0]["State"]["Status"] = "unrecognized"
                frame, commands = self.run_host(
                    mode=mode,
                    extra=extra,
                    inspect_result=[] if change == "incomplete" else None,
                )
                if change in {"running", "foreign"}:
                    self.assertEqual(frame["availability"], "changed")
                else:
                    self.assertEqual(frame["type"], "failed")
                    self.assertEqual(frame["predicate"], ReadbackPredicate.NATIVE_READ)
                self.assertFalse(any(command[1] == "rm" for command in commands))

    def test_interrupted_removal_finishes_only_the_qualified_leftovers(self) -> None:
        original = deepcopy(self.containers)
        for retired in (False, True):
            with self.subTest(retired=retired):
                self.containers = deepcopy(original)
                for item in self.containers:
                    item["State"]["Status"] = "exited"
                qualified, _ = self.run_host(extra={"allow_unavailable": True})
                expected = [item["Id"] for item in self.containers]
                removed = self.containers.pop()
                self.containers[0]["State"]["Status"] = "dead"
                extra = {
                    "allow_unavailable": True,
                    "container_ids": expected,
                    **(
                        {
                            "retiring": True,
                            "resource_binding": qualified["resource_binding"],
                            "physical_binding": qualified["physical_binding"],
                        }
                        if retired
                        else {}
                    ),
                }
                frame, commands = self.run_host(mode="removal-readback", extra=extra)
                self.assertEqual(frame["availability"], "stopped")
                self.assertFalse(any(command[1] == "rm" for command in commands))
                frame, commands = self.run_host(mode="remove", extra=extra)
                self.assertEqual(frame["availability"], "absent")
                self.assertNotIn(["docker", "rm", removed["Id"]], commands)
                self.assertEqual(self.containers, [])

    def test_retiring_target_qualifies_later_process_without_inventing_old_identity(self) -> None:
        self.containers[0]["Config"]["Env"] = ["PLATFORM_INSTANCE=testing", "ODOO_DB_HOST=database"]
        self.containers[0]["Id"] = "later-web"
        self.assertEqual(self.run_host(extra={"allow_unavailable": True})[0]["type"], "failed")
        qualified, _ = self.run_host(
            extra={"allow_unavailable": True, "retiring": True, "identity": None}
        )
        self.assertEqual(qualified["type"], "complete")
        self.assertFalse(qualified["stopped"])
        self.assertIsNone(qualified["identity"])
        expected = [item["Id"] for item in self.containers]
        for item in self.containers:
            item["State"]["Status"] = "exited"
        frame, commands = self.run_host(
            mode="remove",
            extra={
                "allow_unavailable": True,
                "retiring": True,
                "identity": None,
                "container_ids": expected,
                "resource_binding": qualified["resource_binding"],
                "physical_binding": qualified["physical_binding"],
            },
        )
        self.assertEqual(frame["availability"], "absent")
        self.assertTrue(any(command[1] == "rm" for command in commands))

    def test_retirement_removal_rechecks_storage_and_production_isolation(self) -> None:
        for item in self.containers:
            item["State"]["Status"] = "exited"
        qualified, _ = self.run_host(extra={"allow_unavailable": True, "retiring": True})
        extra = {
            "allow_unavailable": True,
            "retiring": True,
            "container_ids": [item["Id"] for item in self.containers],
            "resource_binding": qualified["resource_binding"],
            "physical_binding": qualified["physical_binding"],
        }
        for change in ("production", "consumer", "backing"):
            with self.subTest(change=change):
                original = deepcopy(self.containers)
                payload = dict(extra)
                if change == "production":
                    self.containers[1]["Config"]["Env"] = ["PLATFORM_INSTANCE=prod"]
                elif change == "consumer":
                    foreign = deepcopy(self.containers[1])
                    foreign["Id"] = "production-consumer"
                    foreign["Config"]["Labels"] = {}
                    self.containers.append(foreign)
                else:
                    payload["physical_binding"] = "changed"
                frame, commands = self.run_host(mode="remove", extra=payload)
                self.assertEqual(frame["type"], "failed")
                self.assertFalse(any(command[1] == "rm" for command in commands))
                self.containers = original

    def test_retirement_settles_missing_original_ids_without_removing_replacements(self) -> None:
        expected = [item["Id"] for item in self.containers]
        for item in self.containers:
            item["Id"] = "replacement-" + item["Id"]
        for mode in ("remove", "removal-readback"):
            frame, commands = self.run_host(
                mode=mode,
                extra={
                    "allow_unavailable": True,
                    "retiring": True,
                    "container_ids": expected,
                },
            )
            self.assertEqual(frame["availability"], "absent")
            self.assertFalse(any(command[1] == "rm" for command in commands))

    def test_retired_batch_qualifies_full_project_storage_but_removes_only_saved_ids(self) -> None:
        for item in self.containers:
            item["State"]["Status"] = "exited"
        qualified, _ = self.run_host(extra={"allow_unavailable": True, "retiring": True})
        expected = [item["Id"] for item in self.containers]
        extra = deepcopy(self.containers[0])
        extra["Id"] = "later-script-runner"
        extra["Config"]["Labels"]["com.docker.compose.service"] = "script-runner"
        self.containers.append(extra)
        frame, commands = self.run_host(
            mode="remove",
            extra={
                "allow_unavailable": True,
                "retiring": True,
                "container_ids": expected,
                "resource_binding": qualified["resource_binding"],
                "physical_binding": qualified["physical_binding"],
            },
        )
        self.assertEqual(frame["availability"], "absent")
        self.assertEqual([item["Id"] for item in self.containers], [extra["Id"]])
        self.assertNotIn(["docker", "rm", extra["Id"]], commands)
