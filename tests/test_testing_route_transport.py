"""Execute fixed host and atomic writer programs against isolated route files."""

from copy import deepcopy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import Mock, patch

from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.dokploy import testing_route_transport as transport
from control_plane.dokploy.native_copy_stream import (
    NativeSourceAuthorizationDenied,
    NativeSourceUnavailable,
)
from control_plane.dokploy.testing_route import TestingRoutePending, route_document
from tests import test_testing_route as route_tests


class TestingRouteTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.fixture = route_tests.TestingRouteTests()
        self.fixture.setUp()
        self.plan = self.fixture.plan()
        self.raw = deepcopy(self.fixture.raw)
        self.route_file = self.root / (self.plan.route_id + ".yml")
        self.identity = RuntimeIdentity(
            product="example",
            context="example",
            instance="testing",
            deployment_record_id="old-deployment",
            artifact_id="old-build",
            source_git_ref="old-source",
        )
        self.static = (
            "providers:\n  file:\n    directory: "
            + transport.TRAEFIK_DYNAMIC_DIRECTORY
            + "\n    watch: true\n"
        )
        self.containers: dict[str, Any] = {
            "proxy": {
                "Id": "proxy",
                "Name": "/dokploy-traefik",
                "State": {"Status": "running"},
                "Config": {"Labels": None},
                "Mounts": [
                    {"Destination": transport.TRAEFIK_DYNAMIC_DIRECTORY, "Type": "bind", "RW": True}
                ],
                "NetworkSettings": {"Networks": {"dokploy-network": {"IPAddress": "172.20.0.2"}}},
            },
            "old": {
                "Id": "old",
                "Name": "/testing-old-web",
                "State": {"Status": "running"},
                "Config": {
                    "Labels": {
                        "com.docker.compose.project": "testing-old",
                        "com.docker.compose.service": "web",
                    },
                    "Env": [
                        "PLATFORM_INSTANCE=testing",
                        "LAUNCHPLANE_RUNTIME_IDENTITY_JSON=" + self.identity.model_dump_json(),
                    ],
                },
                "NetworkSettings": {
                    "Networks": {"dokploy-network": {"IPAddress": "172.20.0.3", "Aliases": None}}
                },
            },
        }
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.script(
            "sha256sum",
            "import hashlib,sys;from pathlib import Path;print(hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest()+'  '+sys.argv[1])",
        )
        self.script(
            "sync", "import os,sys;fd=os.open(sys.argv[-1],os.O_RDONLY);os.fsync(fd);os.close(fd)"
        )
        self.effect_count = 0
        self.apply_immediately = True
        self.deny = False
        self.before_publish: Any = None
        self.original_run = subprocess.run
        self.enterContext(patch.object(transport, "_read_remote", side_effect=self.remote))
        self.client = transport.DokployTestingRouteTransport(
            host="https://provider.invalid",
            token="fixture",
            server_id="testing-server",
            old_project="testing-old",
            product=self.plan.product,
            context=self.plan.context,
            public_host=self.plan.host,
            entrypoints=self.plan.public_entrypoints,
            old_runtime_identity=self.identity,
        )
        self.guards = 0

    def script(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text("#!" + sys.executable + "\n" + body + "\n")
        path.chmod(0o700)

    def guard(self, dispatch: Any) -> None:
        self.guards += 1
        dispatch()

    def apply_document(self) -> None:
        document = json.loads(self.route_file.read_text())["http"]
        for section in ("routers", "services", "middlewares"):
            for name, value in document.get(section, {}).items():
                self.raw[section][name + "@file"] = {**value, "status": "enabled"}

    def command(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        result = b""
        if command[1:3] == ["ps", "-aq"]:
            result = "\n".join(self.containers).encode()
        elif command[1] == "inspect":
            result = json.dumps([self.containers[command[2]]]).encode()
        elif command[1:4] == ["exec", "proxy", "cat"]:
            result = self.static.encode()
        elif command[1:3] == ["exec", "proxy"] and command[3:5] == ["sh", "-c"]:
            actual = command[3:]
            actual[-2] = str(self.root)
            return self.original_run(
                actual, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
            )
        elif command[1:4] == ["exec", "-i", "proxy"]:
            self.effect_count += 1
            if self.deny:
                return subprocess.CompletedProcess(
                    command, 1, stdout=b"", stderr=b"permission denied"
                )
            actual = command[4:]
            actual[-4] = str(self.root)
            environment = {**os.environ, "PATH": str(self.bin) + os.pathsep + os.environ["PATH"]}
            completed = self.original_run(
                actual,
                input=kwargs.get("input"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                env=environment,
            )
            if completed.returncode == 0 and self.apply_immediately:
                self.apply_document()
            return completed
        else:
            self.fail("Unexpected managed-server command")
        return subprocess.CompletedProcess(command, 0, stdout=result, stderr=b"")

    def remote(self, **kwargs: Any) -> None:
        namespace: dict[str, Any] = {"__name__": "fixed_program_test"}
        exec(compile(kwargs["program"], "<testing-route-host>", "exec"), namespace)
        opener = Mock()
        opener.open.side_effect = lambda *_args, **_kwargs: io.BytesIO(
            json.dumps(self.raw).encode()
        )

        def dispatch() -> None:
            if kwargs["payload"]["mode"] == "publish" and self.before_publish is not None:
                self.before_publish()
            with (
                patch("subprocess.run", side_effect=self.command),
                patch("urllib.request.build_opener", return_value=opener),
            ):
                # build_opener was imported by the fixed program at compile time.
                namespace["build_opener"] = lambda *_args, **_kwargs: opener
                try:
                    frame = namespace["main"](kwargs["payload"])
                except PermissionError:
                    raise NativeSourceAuthorizationDenied("fixture denied") from None
                except RuntimeError:
                    raise NativeSourceUnavailable("fixture unavailable") from None
            kwargs["receive"](frame)

        guard = kwargs.get("dispatch_guard")
        if guard is None:
            dispatch()
        else:
            guard(dispatch)

    def publish(self, backend: str, expected: str) -> None:
        self.client.publish(
            self.plan, backend, expected_backend=expected, dispatch_guard=self.guard
        )

    def test_native_atomic_migration_switch_and_switchback_keep_other_files_and_old_copy(
        self,
    ) -> None:
        other = self.root / "unrelated.yml"
        other.write_text("fixture unrelated route")
        before = other.read_bytes()
        self.publish(self.plan.old_backend, self.plan.old_backend)
        self.publish(self.plan.new_backend, self.plan.old_backend)
        self.publish(self.plan.old_backend, self.plan.new_backend)
        self.assertEqual(self.effect_count, 3)
        self.assertEqual(self.guards, 3)
        self.assertEqual(
            json.loads(self.route_file.read_text()),
            route_document(self.plan, self.plan.old_backend),
        )
        self.assertEqual(other.read_bytes(), before)
        self.assertEqual(self.containers["old"]["State"]["Status"], "running")
        self.assertFalse(list(self.root.glob(".launchplane-testing.pending.*")))

    def test_unapplied_reply_reconciles_without_writing_again(self) -> None:
        self.apply_immediately = False
        with self.assertRaises(TestingRoutePending):
            self.publish(self.plan.old_backend, self.plan.old_backend)
        with self.assertRaises(TestingRoutePending):
            self.publish(self.plan.old_backend, self.plan.old_backend)
        self.assertEqual(self.effect_count, 1)
        self.apply_document()
        self.publish(self.plan.old_backend, self.plan.old_backend)
        self.assertEqual(self.effect_count, 1)

    def test_authority_change_before_native_dispatch_refuses_without_writing(self) -> None:
        self.before_publish = lambda: self.raw["routers"]["old-http@docker"].update(priority=999)
        with self.assertRaises(NativeSourceUnavailable):
            self.publish(self.plan.old_backend, self.plan.old_backend)
        self.assertFalse(self.route_file.exists())
        self.assertEqual(self.effect_count, 0)

    def test_permission_denial_is_propagated_without_an_alternate_writer(self) -> None:
        self.deny = True
        with self.assertRaises(NativeSourceAuthorizationDenied):
            self.publish(self.plan.old_backend, self.plan.old_backend)
        self.assertEqual(self.effect_count, 1)
        self.assertFalse(self.route_file.exists())

    def test_retired_old_copy_still_allows_observation_of_current_healthy_route(self) -> None:
        self.publish(self.plan.new_backend, self.plan.old_backend)
        self.containers["old"]["State"]["Status"] = "exited"
        self.raw["routers"] = {
            name: value
            for name, value in self.raw["routers"].items()
            if not name.endswith("@docker")
        }
        frame = self.client.read()
        self.assertFalse(frame["old_endpoints"])
        self.publish(self.plan.new_backend, self.plan.new_backend)
        with self.assertRaises(ValueError):
            self.publish(self.plan.old_backend, self.plan.new_backend)
        self.assertEqual(self.effect_count, 1)

    def test_symlink_or_unrecorded_file_is_never_replaced(self) -> None:
        retained = self.root / "retained.yml"
        retained.write_text("retained")
        self.route_file.symlink_to(retained)
        with self.assertRaises(NativeSourceUnavailable):
            self.publish(self.plan.old_backend, self.plan.old_backend)
        self.assertEqual(retained.read_text(), "retained")
        self.assertEqual(self.effect_count, 0)

    def test_missing_or_changed_retained_identity_refuses_before_any_writer(self) -> None:
        environment = self.containers["old"]["Config"]["Env"]
        self.containers["old"]["Config"]["Env"] = ["PLATFORM_INSTANCE=testing"]
        with self.assertRaises(ValueError):
            self.publish(self.plan.old_backend, self.plan.old_backend)
        changed = self.identity.model_copy(update={"deployment_record_id": "different-deployment"})
        self.containers["old"]["Config"]["Env"] = [
            "PLATFORM_INSTANCE=testing",
            "LAUNCHPLANE_RUNTIME_IDENTITY_JSON=" + changed.model_dump_json(),
        ]
        with self.assertRaises(ValueError):
            self.publish(self.plan.old_backend, self.plan.old_backend)
        self.assertEqual(self.effect_count, 0)
        self.assertFalse(self.route_file.exists())
        self.containers["old"]["Config"]["Env"] = environment

    def test_cross_lane_plan_refuses_before_provider_reads(self) -> None:
        different = self.plan.model_copy(update={"host": "other.invalid"})
        with patch.object(self.client, "read") as read, self.assertRaises(ValueError):
            self.client.publish(
                different,
                different.old_backend,
                expected_backend=different.old_backend,
                dispatch_guard=self.guard,
            )
        read.assert_not_called()
        self.assertEqual(self.effect_count, 0)

    def test_fixed_host_writer_refuses_a_document_for_another_public_host(self) -> None:
        document = route_document(self.plan, self.plan.old_backend)
        next(iter(document["http"]["routers"].values()))["rule"] = "Host(`production.invalid`)"
        with (
            patch.object(transport, "route_document", return_value=document),
            self.assertRaises(NativeSourceUnavailable),
        ):
            self.publish(self.plan.old_backend, self.plan.old_backend)
        self.assertEqual(self.effect_count, 0)
        self.assertFalse(self.route_file.exists())
