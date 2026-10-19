"""The rehearsal preflight measures through existing readers without preparing a copy."""

import base64
from contextlib import redirect_stdout
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlencode

from control_plane._odoo_rehearsal_preflight_program import (
    REHEARSAL_PREFLIGHT_PROGRAM,
    SOURCE_MEASUREMENTS_PROGRAM,
)
from control_plane._odoo_rehearsal_source_reader import REHEARSAL_SOURCE_READER_LIBRARY
from control_plane.contracts.odoo_upgrade_rehearsal import (
    ODOO_UPGRADE_REHEARSAL_PREFLIGHT_ROUTE,
    RehearsalPreflightObservation,
)
from control_plane.dokploy.native_copy_stream import NativeSourceAuthorizationDenied
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import LaunchplaneAuthzPolicy, LocalOperatorPolicyRule
from tests.http_app_test_support import _local_operator_bearer_config, _RejectingVerifier
from tests.support.auth import scoped_agent_transport
from tests.support.http import request
from tests import test_odoo_upgrade_rehearsal as rehearsal_tests


def observation() -> dict[str, Any]:
    return {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "reader_host_sha256": hashlib.sha256(b"fixture-reader").hexdigest(),
        "odoo_version": "19.0",
        "database_bytes": 4096,
        "filestore_apparent_bytes": 8192,
        "filestore_allocated_bytes": 4096,
        "backing_basis": "allocation-parent",
        "backing_groups": [
            {
                "group_id": hashlib.sha256(b"fixture-backing").hexdigest(),
                "available_bytes": 65536,
                "roles": [
                    "database",
                    "filestore",
                    "runner_scratch",
                    "database_scratch",
                    "host_custody",
                ],
            }
        ],
        "read_only": True,
    }


class SourceMeasurementTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.filestore = self.root / "filestore"
        self.filestore.mkdir()
        self.database = {"database_bytes": 4096, "odoo_version": "19.0"}

    def measure(
        self, *, result: subprocess.CompletedProcess[bytes] | None = None
    ) -> tuple[dict[str, Any], Mock]:
        output = io.StringIO()

        def psql(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
            if result is not None:
                return result
            # Older psql prints only COMMIT's suppressed tag for one multi-command -c.
            stdout = b"" if "-c" in arguments else json.dumps(self.database).encode()
            return subprocess.CompletedProcess(arguments, 0, stdout=stdout, stderr=b"")

        with (
            patch.object(
                sys, "argv", ["measure", "fixture_db_reader", "fixture_db", str(self.filestore)]
            ),
            patch(
                "subprocess.run",
                side_effect=psql,
            ) as database,
            redirect_stdout(output),
        ):
            exec(compile(SOURCE_MEASUREMENTS_PROGRAM, "<source-measurements>", "exec"), {})
        return json.loads(output.getvalue()), database

    def test_measurements_read_database_and_apparent_and_allocated_filestore_without_writes(
        self,
    ) -> None:
        nested = self.filestore / "aa"
        nested.mkdir()
        attachment = nested / "attachment"
        with attachment.open("wb") as handle:
            handle.write(b"fixture attachment")
            handle.seek(1024 * 1024)
            handle.write(b"sparse tail")
        paths = [self.filestore, nested, attachment]
        before = {path: path.stat() for path in paths}
        result, database = self.measure()
        self.assertEqual(result["type"], "measurements")
        self.assertEqual(result["database_bytes"], self.database["database_bytes"])
        self.assertEqual(result["odoo_version"], self.database["odoo_version"])
        self.assertEqual(
            result["filestore_apparent_bytes"], sum(item.st_size for item in before.values())
        )
        self.assertEqual(
            result["filestore_allocated_bytes"],
            sum(item.st_blocks * 512 for item in before.values()),
        )
        self.assertEqual({self.filestore, *self.filestore.rglob("*")}, set(paths))
        self.assertEqual({path: path.stat() for path in paths}, before)
        database.assert_called_once()
        arguments = database.call_args.args[0]
        self.assertEqual(arguments[:6], ["sudo", "-n", "-u", "fixture_db_reader", "--", "psql"])
        self.assertIn(b"BEGIN READ ONLY;", database.call_args.kwargs["input"])
        self.assertEqual(arguments[arguments.index("-d") + 1], "fixture_db")
        self.assertTrue(database.call_args.kwargs["capture_output"])
        self.assertNotIn("pg_dump", arguments)

    def test_links_and_special_members_refuse_without_following_or_creating_files(self) -> None:
        outside = self.root / "outside"
        outside.write_bytes(b"fixture retained source")
        member = self.filestore / "member"
        for kind in ("link", "fifo"):
            with self.subTest(kind=kind):
                if kind == "link":
                    member.symlink_to(outside)
                else:
                    os.mkfifo(member)
                result, _ = self.measure()
                self.assertEqual(result, {"type": "failed"})
                self.assertEqual(outside.read_bytes(), b"fixture retained source")
                self.assertEqual(set(self.filestore.iterdir()), {member})
                member.unlink()

    def test_selected_filestore_follows_root_aliases_like_capture_and_refuses_a_file(self) -> None:
        original = self.filestore
        selected = self.root / "selected-filestore"
        selected.symlink_to(original, target_is_directory=True)
        alias = self.root / "alias-parent"
        alias.symlink_to(self.root, target_is_directory=True)
        regular = self.root / "regular-file"
        regular.write_bytes(b"fixture file is not a filestore directory")
        for path in (selected, alias / original.name):
            with self.subTest(path=path):
                self.filestore = path
                result, _ = self.measure()
                self.assertEqual(result["type"], "measurements")
                self.assertEqual(result["filestore_apparent_bytes"], original.stat().st_size)
        self.filestore = regular
        result, _ = self.measure()
        self.assertEqual(result, {"type": "failed"})

    def test_database_denial_stops_before_filestore_traversal(self) -> None:
        for message in (
            b"permission denied: private database detail",
            b"fixture-reader is not in the sudoers file. This incident will be reported.",
            b"Sorry, user fixture-reader may not run sudo on fixture-host.",
        ):
            with (
                self.subTest(message=message),
                patch("os.scandir", side_effect=AssertionError("No traversal after denial")),
            ):
                result, database = self.measure(
                    result=subprocess.CompletedProcess([], 1, stdout=b"", stderr=message)
                )
            self.assertEqual(result, {"type": "denied"})
            database.assert_called_once()


class PreflightHostTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.docker_root = self.root / "docker"
        (self.docker_root / "volumes").mkdir(parents=True)
        (self.docker_root / "overlay2").mkdir()
        self.ssh = self.root / "ssh"
        self.ssh.mkdir()
        (self.ssh / "id_ed25519").write_text("synthetic reader identity")
        (self.ssh / "known_hosts").write_text("synthetic pinned trust")
        self.source = {
            "ODOO_UPSTREAM_HOST": "source.invalid",
            "ODOO_UPSTREAM_USER": "reader",
            "ODOO_UPSTREAM_DB_USER": "fixture_db_reader",
            "ODOO_UPSTREAM_DB_NAME": "fixture_db",
            "ODOO_UPSTREAM_FILESTORE_PATH": "/source/filestore",
            "DATA_WORKFLOW_SSH_DIR": str(self.ssh),
        }
        self.driver = "overlay2"
        self.source_response: dict[str, Any] = {
            "type": "measurements",
            "database_bytes": 4096,
            "odoo_version": "19.0",
            "filestore_apparent_bytes": 8192,
            "filestore_allocated_bytes": 4096,
        }
        self.commands: list[list[str]] = []

    def command(self, arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.commands.append(arguments)
        if arguments[0] == "docker":
            self.assertEqual(arguments, ["docker", "info", "--format", "{{json .}}"])
            value = {"Driver": self.driver, "DockerRootDir": str(self.docker_root)}
        elif arguments[0] == "ssh":
            self.assertEqual(arguments[-2], "reader@source.invalid")
            remote = shlex.split(arguments[-1])
            self.assertEqual(remote[:2], ["python3", "-c"])
            self.assertEqual(remote[2], SOURCE_MEASUREMENTS_PROGRAM)
            self.assertEqual(remote[3:], ["fixture_db_reader", "fixture_db", "/source/filestore"])
            value = self.source_response
        else:
            raise AssertionError("Unexpected command: " + arguments[0])
        return subprocess.CompletedProcess(
            arguments, 0, stdout=json.dumps(value).encode(), stderr=b""
        )

    def run_program(self) -> dict[str, Any]:
        spec = {"server_id": "fixture-server", "source": self.source}
        output = io.StringIO()
        with (
            patch.object(Path, "home", return_value=self.home),
            patch.object(
                sys,
                "argv",
                ["preflight", base64.b64encode(json.dumps(spec).encode()).decode(), "fixture:"],
            ),
            patch("subprocess.run", side_effect=self.command),
            redirect_stdout(output),
        ):
            exec(compile(REHEARSAL_PREFLIGHT_PROGRAM, "<preflight-host>", "exec"), {})
        frame: dict[str, Any] = json.loads(output.getvalue().removeprefix("fixture:"))
        return frame

    def test_groups_actual_backing_with_nonroot_minimum_and_uses_nearest_existing_parent(
        self,
    ) -> None:
        paths = {self.docker_root / "volumes": 1, self.docker_root / "overlay2": 1, self.home: 2}
        free = {
            self.docker_root / "volumes": 400,
            self.docker_root / "overlay2": 350,
            self.home: 200,
        }
        original_stat = Path.stat

        def stat(path: Path, *args: Any, **kwargs: Any) -> os.stat_result:
            values = list(original_stat(path, *args, **kwargs))
            if path in paths:
                values[2] = paths[path]
            return os.stat_result(values)

        def statvfs(path: Path) -> SimpleNamespace:
            return SimpleNamespace(f_bavail=free[path], f_bfree=10_000, f_frsize=2)

        before = set(self.root.rglob("*"))
        for scratch_device in (1, 3):
            with self.subTest(scratch_device=scratch_device):
                self.commands.clear()
                paths[self.docker_root / "overlay2"] = scratch_device
                with patch.object(Path, "stat", stat), patch.object(os, "statvfs", statvfs):
                    frame = self.run_program()
                self.assertEqual(frame["type"], "complete")
                result = RehearsalPreflightObservation.model_validate(frame["result"])
                groups = {
                    frozenset(group.roles): group.available_bytes for group in result.backing_groups
                }
                expected = {frozenset(("host_custody",)): 400}
                if scratch_device == 1:
                    expected[
                        frozenset(("database", "filestore", "runner_scratch", "database_scratch"))
                    ] = 700
                else:
                    expected[frozenset(("database", "filestore"))] = 800
                    expected[frozenset(("runner_scratch", "database_scratch"))] = 700
                self.assertEqual(groups, expected)
                self.assertEqual(set(self.root.rglob("*")), before)
                self.assertFalse((self.home / ".local").exists())
                self.assertEqual([command[0] for command in self.commands], ["docker", "ssh"])

    def test_unknown_backing_refuses_before_any_source_read(self) -> None:
        self.driver = "containerd"
        self.assertEqual(self.run_program(), {"type": "failed"})
        self.assertEqual([command[0] for command in self.commands], ["docker"])
        self.driver = "overlay2"
        self.commands.clear()
        (self.docker_root / "overlay2").rmdir()
        self.assertEqual(self.run_program(), {"type": "failed"})
        self.assertEqual([command[0] for command in self.commands], ["docker"])

    def test_source_denial_preserves_identity_and_never_tries_an_alternate_command(self) -> None:
        self.source_response = {"type": "denied", "private_detail": "private source refusal"}
        self.assertEqual(self.run_program(), {"type": "denied"})
        self.assertEqual([command[0] for command in self.commands], ["docker", "ssh"])
        command = self.commands[-1]
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertIn("IdentitiesOnly=yes", command)
        self.assertEqual(command[command.index("-i") + 1], str(self.ssh / "id_ed25519"))
        self.assertIn("UserKnownHostsFile=" + str(self.ssh / "known_hosts"), command)

    def test_shared_reader_uses_only_existing_selected_key_and_known_hosts(self) -> None:
        namespace: dict[str, Any] = {"Path": Path, "re": re}
        exec(REHEARSAL_SOURCE_READER_LIBRARY, namespace)
        self.source["DATA_WORKFLOW_SSH_KEY"] = "/root/.ssh/id_ed25519"
        command, _, _ = namespace["rehearsal_source_reader"](self.source)
        self.assertEqual(command[command.index("-i") + 1], str(self.ssh / "id_ed25519"))
        self.source["DATA_WORKFLOW_SSH_KEY"] = str(self.ssh / "missing-selected-key")
        with self.assertRaises(RuntimeError):
            namespace["rehearsal_source_reader"](self.source)
        self.source.pop("DATA_WORKFLOW_SSH_KEY")
        (self.ssh / "known_hosts").unlink()
        with self.assertRaises(RuntimeError):
            namespace["rehearsal_source_reader"](self.source)
        self.assertFalse((self.ssh / "known_hosts").exists())

    def test_docker_authorization_plugin_denial_stops_without_a_source_command(self) -> None:
        with patch.object(
            self,
            "command",
            return_value=subprocess.CompletedProcess(
                [], 1, stdout=b"", stderr=b"authorization denied by plugin: private provider detail"
            ),
        ) as command:
            self.assertEqual(self.run_program(), {"type": "denied"})
        command.assert_called_once()
        self.assertEqual(command.call_args.args[0][0], "docker")


class PreflightHttpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.fixture = rehearsal_tests.RehearsalScenario()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def app(self, *, actions: tuple[str, ...] = ("operations.read",)) -> Any:
        policy = LaunchplaneAuthzPolicy(
            schema_version=2,
            local_operators=(
                LocalOperatorPolicyRule(
                    subjects=("local-owner-agent",),
                    token_labels=("local-owner-read",),
                    products=(self.fixture.product,),
                    contexts=(self.fixture.context,),
                    actions=actions,
                ),
            ),
        )
        return create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=policy,
            bearer_identity_config=_local_operator_bearer_config(),
            record_store_factory=lambda: self.fixture.store,
            control_plane_root_path=self.fixture.root,
        )

    async def get(self, app: Any, *, product: str = "", context: str = "") -> Any:
        return await request(
            app,
            "GET",
            ODOO_UPGRADE_REHEARSAL_PREFLIGHT_ROUTE.format(product=product or self.fixture.product)
            + "?"
            + urlencode({"source_context": context or self.fixture.context}),
            headers={"Authorization": "Bearer local-operator-token"},
        )

    async def test_read_grant_requires_action_product_and_context_before_any_provider_call(
        self,
    ) -> None:
        with (
            scoped_agent_transport(),
            patch(
                "control_plane.odoo_rehearsal_preflight.api.fetch_dokploy_target_payload"
            ) as provider,
            patch("control_plane.odoo_rehearsal_preflight.read_odoo_rehearsal_preflight") as read,
        ):
            for changes in ({"product": "other-product"}, {"context": "other-context"}):
                response = await self.get(self.app(), **changes)
                self.assertEqual(response.status_code, 403, response.text)
            response = await self.get(self.app(actions=("product_profile.write",)))
            self.assertEqual(response.status_code, 403, response.text)
            provider.assert_not_called()
            read.assert_not_called()

    async def test_allowed_read_returns_trace_and_leaves_all_authority_and_custody_unchanged(
        self,
    ) -> None:
        expected = observation()
        before = (
            self.fixture.authority(),
            self.fixture.store.read_odoo_upgrade_rehearsal(self.fixture.identifier),
        )
        files = set(self.fixture.root.rglob("*"))

        def receive(**kwargs: Any) -> None:
            self.assertEqual(kwargs["server_id"], self.fixture.record.server_id)
            kwargs["receive"]({"type": "complete", "result": expected})

        with (
            scoped_agent_transport(),
            patch(
                "control_plane.odoo_rehearsal_preflight.read_odoo_rehearsal_preflight",
                side_effect=receive,
            ) as read,
            patch("control_plane.dokploy.api.dokploy_request") as write,
            patch(
                "control_plane.testing_local_credentials.prepare_rehearsal_local_credentials"
            ) as credentials,
            patch.object(self.fixture.store, "compare_and_write_odoo_upgrade_rehearsal") as records,
        ):
            response = await self.get(self.app())
            write.assert_not_called()
            credentials.assert_not_called()
            records.assert_not_called()
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertTrue(result["trace_id"])
        self.assertEqual(result["result"]["product"], self.fixture.product)
        self.assertEqual(result["result"]["source_context"], self.fixture.context)
        self.assertNotIn("rehearsal_id", result["result"])
        self.assertEqual(result["result"]["database_bytes"], expected["database_bytes"])
        self.assertEqual(
            (
                self.fixture.authority(),
                self.fixture.store.read_odoo_upgrade_rehearsal(self.fixture.identifier),
            ),
            before,
        )
        self.assertEqual(set(self.fixture.root.rglob("*")), files)
        read.assert_called_once()

    async def test_transport_denial_and_incomplete_frames_fail_closed_without_leaking_details(
        self,
    ) -> None:
        app = self.app()
        with scoped_agent_transport():
            with patch(
                "control_plane.odoo_rehearsal_preflight.read_odoo_rehearsal_preflight",
                side_effect=NativeSourceAuthorizationDenied("private provider detail"),
            ) as read:
                denied = await self.get(app)
            self.assertEqual(denied.status_code, 403, denied.text)
            self.assertEqual(denied.json()["error"]["code"], "authorization_denied")
            self.assertTrue(denied.json()["trace_id"])
            self.assertNotIn("private provider detail", denied.text)
            read.assert_called_once()
            for frames in (
                [],
                [{"type": "failed"}],
                [{"type": "complete", "result": {}}],
                [{"type": "complete", "result": observation()}] * 2,
            ):
                with self.subTest(frames=len(frames)):

                    def receive(**kwargs: Any) -> None:
                        for frame in frames:
                            kwargs["receive"](frame)

                    with patch(
                        "control_plane.odoo_rehearsal_preflight.read_odoo_rehearsal_preflight",
                        side_effect=receive,
                    ):
                        response = await self.get(app)
                    self.assertEqual(response.status_code, 409, response.text)
                    self.assertEqual(
                        response.json()["error"]["code"], "rehearsal_preflight_unavailable"
                    )
