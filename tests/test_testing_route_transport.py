"""Execute fixed host and atomic writer programs against isolated route files."""

from copy import deepcopy
import base64
from contextlib import redirect_stdout
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
from control_plane.dokploy import native_copy_stream
from control_plane.dokploy.native_copy_stream import (
    NativeSourceAuthorizationDenied,
    NativeSourceUnavailable,
)
from control_plane.dokploy.testing_route import TestingRoutePending, route_document
from tests import test_testing_route as route_tests
from tests.test_odoo_testing_native_source import Connection


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
        self.new_identity = self.identity.model_copy(
            update={
                "deployment_record_id": "new-deployment",
                "artifact_id": "new-build",
                "source_git_ref": "new-source",
            }
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
                    {
                        "Destination": transport.TRAEFIK_DYNAMIC_DIRECTORY,
                        "Source": "/fixture/dynamic",
                        "Type": "bind",
                        "RW": True,
                    }
                ],
                "NetworkSettings": {"Networks": {"dokploy-network": {"IPAddress": "172.20.0.2"}}},
            },
            "old": {
                "Id": "old",
                "Name": "/testing-old-web-1",
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
                    "Networks": {
                        "dokploy-network": {
                            "IPAddress": "172.20.0.3",
                            "Aliases": ["testing-old-web-1"],
                        }
                    }
                },
            },
        }
        namespace = self.plan.new_backend.removeprefix("http://").split(":")[0]
        self.containers["new"] = {
            "Id": "new",
            "Name": "/" + namespace + "-web-1",
            "State": {"Status": "running"},
            "Config": {
                "Labels": {
                    "com.docker.compose.project": namespace,
                    "com.docker.compose.service": "web",
                },
                "Env": [
                    "PLATFORM_INSTANCE=testing",
                    "LAUNCHPLANE_RUNTIME_IDENTITY_JSON=" + self.new_identity.model_dump_json(),
                ],
            },
            "NetworkSettings": {
                "Networks": {
                    "dokploy-network": {
                        "IPAddress": "172.20.0.4",
                        "Aliases": [namespace],
                    }
                }
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
        self.script(
            "stat", "import os,sys;s=os.stat(sys.argv[-1]);print(str(s.st_dev)+':'+str(s.st_ino))"
        )
        self.script(
            "flock",
            "import fcntl,sys\ntry: fcntl.flock(int(sys.argv[-1]),fcntl.LOCK_EX|fcntl.LOCK_NB)\nexcept BlockingIOError: sys.exit(1)",
        )
        self.effect_count = 0
        self.apply_immediately = True
        self.deny = False
        self.denial_message = (
            b"permission denied while trying to connect to the Docker daemon socket"
        )
        self.before_publish: Any = None
        self.writer_timeout = False
        self.lose_writer_reply = False
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
            result = json.dumps([self.containers[name] for name in command[2:]]).encode()
        elif command[1:4] == ["exec", "proxy", "cat"]:
            result = self.static.encode()
        elif command[1:3] == ["exec", "proxy"] and command[3:5] == ["sh", "-c"]:
            actual = [
                str(self.root) if argument == transport.TRAEFIK_DYNAMIC_DIRECTORY else argument
                for argument in command[3:]
            ]
            environment = {**os.environ, "PATH": str(self.bin) + os.pathsep + os.environ["PATH"]}
            return self.original_run(
                actual, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, env=environment
            )
        elif command[1:4] == ["exec", "-i", "proxy"]:
            self.effect_count += 1
            if self.writer_timeout:
                raise subprocess.TimeoutExpired(command, 1)
            if self.deny:
                return subprocess.CompletedProcess(
                    command, 1, stdout=b"", stderr=self.denial_message
                )
            actual = [
                str(self.root) if argument == transport.TRAEFIK_DYNAMIC_DIRECTORY else argument
                for argument in command[4:]
            ]
            environment = {**os.environ, "PATH": str(self.bin) + os.pathsep + os.environ["PATH"]}
            completed = self.original_run(
                actual,
                input=kwargs.get("input"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                env=environment,
            )
            if completed.returncode == 0 and self.apply_immediately and self.route_file.exists():
                self.apply_document()
            if self.lose_writer_reply:
                raise subprocess.TimeoutExpired(command, 1)
            return completed
        else:
            self.fail("Unexpected managed-server command")
        return subprocess.CompletedProcess(command, 0, stdout=result, stderr=b"")

    def remote(self, **kwargs: Any) -> None:
        opener = Mock()
        opener.open.side_effect = lambda *_args, **_kwargs: io.BytesIO(
            json.dumps(self.raw).encode()
        )

        def dispatch() -> None:
            if kwargs["payload"]["mode"] == "publish" and self.before_publish is not None:
                self.before_publish()
            output = io.StringIO()
            with (
                patch("subprocess.run", side_effect=self.command),
                patch("urllib.request.build_opener", return_value=opener),
                patch.object(
                    sys,
                    "argv",
                    [
                        "route-program",
                        base64.b64encode(json.dumps(kwargs["payload"]).encode()).decode(),
                        "route-frame:",
                    ],
                ),
                redirect_stdout(output),
            ):
                try:
                    exec(
                        compile(kwargs["program"], "<testing-route-host>", "exec"),
                        {"__name__": "__main__"},
                    )
                except SystemExit:
                    pass
            for line in output.getvalue().splitlines():
                if not line.startswith("route-frame:"):
                    continue
                frame = json.loads(line.removeprefix("route-frame:"))
                if frame["type"] == "denied":
                    raise NativeSourceAuthorizationDenied("fixture denied")
                if frame["type"] == "failed":
                    raise NativeSourceUnavailable("fixture unavailable")
                kwargs["receive"](frame)

        guard = kwargs.get("dispatch_guard")
        if guard is None:
            dispatch()
        else:
            guard(dispatch)

    def publish(self, backend: str, expected: str) -> None:
        execution = self.client.execution_intent(
            self.plan, backend, operation_id="fixture:" + expected
        )
        self.client.publish(
            self.plan,
            backend,
            expected_backend=expected,
            dispatch_guard=self.guard,
            desired_runtime_identity=self.identity
            if backend == self.plan.old_backend
            else self.new_identity,
            execution=execution,
            execution_key=execution.key(attempt=1),
        )

    def test_settlement_excludes_a_delayed_original_writer(self) -> None:
        execution = self.client.execution_intent(
            self.plan, self.plan.new_backend, operation_id="fixture-rebuild"
        )
        key = execution.key(attempt=1)
        outcome = self.client.settle_execution(execution, key, dispatch_guard=self.guard)
        self.assertEqual(outcome, "excluded")
        with self.assertRaises(transport.TestingRouteWriteNotStarted):
            self.client.publish(
                self.plan,
                self.plan.new_backend,
                expected_backend=self.plan.old_backend,
                desired_runtime_identity=self.new_identity,
                dispatch_guard=self.guard,
                execution=execution,
                execution_key=key,
            )
        self.assertFalse(self.route_file.exists())
        self.assertEqual(self.client.read()["raw"], self.raw)

    def test_lost_finished_reply_has_a_durable_original_outcome(self) -> None:
        execution = self.client.execution_intent(
            self.plan, self.plan.new_backend, operation_id="fixture-rebuild"
        )
        key = execution.key(attempt=1)
        self.lose_writer_reply = True
        with self.assertRaises(NativeSourceUnavailable):
            self.client.publish(
                self.plan,
                self.plan.new_backend,
                expected_backend=self.plan.old_backend,
                desired_runtime_identity=self.new_identity,
                dispatch_guard=self.guard,
                execution=execution,
                execution_key=key,
            )
        self.lose_writer_reply = False
        before = self.route_file.read_bytes()
        self.assertEqual(
            self.client.settle_execution(execution, key, dispatch_guard=self.guard), "applied"
        )
        self.assertEqual(self.route_file.read_bytes(), before)

    def test_changed_mount_cannot_seal_the_original_writer(self) -> None:
        execution = self.client.execution_intent(
            self.plan, self.plan.new_backend, operation_id="fixture"
        )
        self.containers["proxy"]["Mounts"][0]["Source"] = "/fixture/another-mount"
        with self.assertRaises(NativeSourceUnavailable) as caught:
            self.client.settle_execution(
                execution, execution.key(attempt=1), dispatch_guard=self.guard
            )
        self.assertNotIsInstance(caught.exception, transport.TestingRouteWriteNotStarted)
        self.assertEqual(self.effect_count, 0)
        self.assertFalse(self.route_file.exists())

    def test_settlement_denial_is_not_exclusion_or_an_alternate_writer(self) -> None:
        execution = self.client.execution_intent(
            self.plan, self.plan.new_backend, operation_id="fixture"
        )
        self.deny = True
        with self.assertRaises(NativeSourceAuthorizationDenied):
            self.client.settle_execution(
                execution, execution.key(attempt=1), dispatch_guard=self.guard
            )
        self.assertEqual(self.effect_count, 1)
        self.assertFalse(self.route_file.exists())

    def test_initial_read_discovers_actual_host_entrypoints_without_assuming_names(self) -> None:
        self.client.payload["entrypoints"] = ()
        observed = self.client.read()
        self.assertEqual(set(observed["raw"]["routers"]), set(self.raw["routers"]))
        self.assertEqual(self.effect_count, 0)
        with self.assertRaises(ValueError):
            self.publish(self.plan.old_backend, self.plan.old_backend)
        self.assertEqual(self.effect_count, 0)

    def test_container_name_remains_a_qualified_dns_endpoint_without_extra_aliases(self) -> None:
        self.containers["old"]["NetworkSettings"]["Networks"]["dokploy-network"]["Aliases"] = None
        self.assertIn(self.plan.old_backend, self.client.read()["old_endpoints"])
        self.assertEqual(self.effect_count, 0)

    def test_proxy_filesystem_permission_error_is_unavailable_without_a_fallback(self) -> None:
        self.deny = True
        self.denial_message = b"mktemp: permission denied"
        with self.assertRaises(NativeSourceUnavailable) as caught:
            self.publish(self.plan.old_backend, self.plan.old_backend)
        self.assertNotIsInstance(caught.exception, transport.TestingRouteWriteNotStarted)
        self.assertEqual(self.effect_count, 1)

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

    def test_prewrite_refusal_is_terminal_but_a_started_writer_timeout_stays_unknown(self) -> None:
        original_raw = deepcopy(self.raw)
        self.before_publish = lambda: self.raw["routers"]["old-http@docker"].update(priority=999)
        with self.assertRaises(transport.TestingRouteWriteNotStarted):
            self.publish(self.plan.old_backend, self.plan.old_backend)
        self.assertEqual(self.effect_count, 0)
        self.raw = original_raw
        self.before_publish = None
        self.writer_timeout = True
        with self.assertRaises(NativeSourceUnavailable) as caught:
            self.publish(self.plan.old_backend, self.plan.old_backend)
        self.assertNotIsInstance(caught.exception, transport.TestingRouteWriteNotStarted)
        self.assertEqual(self.effect_count, 1)

    def test_candidate_alias_must_be_unique_running_web_and_the_admitted_runtime(self) -> None:
        original = deepcopy(self.containers["new"])
        for problem in ("missing", "shared", "stopped", "identity", "project"):
            with self.subTest(problem=problem):
                self.containers["new"] = deepcopy(original)
                self.containers.pop("foreign", None)
                if problem == "missing":
                    self.containers.pop("new")
                elif problem == "shared":
                    self.containers["foreign"] = deepcopy(original)
                    self.containers["foreign"]["Id"] = "foreign"
                elif problem == "stopped":
                    self.containers["new"]["State"]["Status"] = "exited"
                elif problem == "identity":
                    self.containers["new"]["Config"]["Env"] = [
                        "PLATFORM_INSTANCE=testing",
                        "LAUNCHPLANE_RUNTIME_IDENTITY_JSON=" + self.identity.model_dump_json(),
                    ]
                else:
                    self.containers["new"]["Config"]["Labels"]["com.docker.compose.project"] = (
                        "foreign"
                    )
                with self.assertRaises(NativeSourceUnavailable):
                    self.publish(self.plan.new_backend, self.plan.old_backend)
                self.assertEqual(self.effect_count, 0)
                self.assertFalse(self.route_file.exists())

    def test_alias_ownership_covers_every_proxy_network_and_dns_spelling(self) -> None:
        original = deepcopy(self.containers)
        original_raw = deepcopy(self.raw)
        alias = self.plan.new_backend.removeprefix("http://").split(":")[0]
        for problem in ("other-network", "case-alias", "dns-name", "detached-name"):
            with self.subTest(problem=problem):
                self.containers = deepcopy(original)
                self.raw = deepcopy(original_raw)
                self.route_file.unlink(missing_ok=True)
                self.effect_count = 0
                if problem == "detached-name":
                    self.containers["new"]["Name"] = "/" + alias
                    self.containers["new"]["NetworkSettings"]["Networks"] = {}
                else:
                    network = "other-app" if problem == "other-network" else "dokploy-network"
                    self.containers["proxy"]["NetworkSettings"]["Networks"][network] = {
                        "IPAddress": "172.21.0.2"
                    }
                    endpoint: dict[str, Any] = {"IPAddress": "172.21.0.3"}
                    endpoint["DNSNames" if problem == "dns-name" else "Aliases"] = [
                        alias.upper() if problem == "case-alias" else alias
                    ]
                    self.containers["foreign"] = {
                        "Id": "foreign",
                        "Name": "/foreign",
                        "NetworkSettings": {"Networks": {network: endpoint}},
                    }
                with self.assertRaises(transport.TestingRouteWriteNotStarted):
                    self.publish(self.plan.new_backend, self.plan.old_backend)
                self.assertEqual(self.effect_count, 0)
                self.assertFalse(self.route_file.exists())

    def test_candidate_port_must_match_before_publication(self) -> None:
        self.containers["new"]["Config"]["Env"].append("ODOO_HTTP_PORT=8070")
        with self.assertRaises(transport.TestingRouteWriteNotStarted):
            self.publish(self.plan.new_backend, self.plan.old_backend)
        self.assertEqual(self.effect_count, 0)

    def test_runtime_lane_must_match_before_provider_reads(self) -> None:
        execution = self.client.execution_intent(
            self.plan, self.plan.new_backend, operation_id="fixture"
        )
        with (
            patch.object(self.client, "read", side_effect=AssertionError("Provider read started")),
            self.assertRaises(ValueError),
        ):
            self.client.publish(
                self.plan,
                self.plan.new_backend,
                expected_backend=self.plan.old_backend,
                dispatch_guard=self.guard,
                desired_runtime_identity=self.new_identity.model_copy(update={"context": "other"}),
                execution=execution,
                execution_key=execution.key(attempt=1),
            )
        self.assertEqual(self.effect_count, 0)

    def test_not_started_frame_survives_the_real_stream_reader(self) -> None:
        connection = Connection({"type": "route_write_not_started"})
        context = Mock()
        context.__enter__ = Mock(return_value=connection)
        context.__exit__ = Mock(return_value=False)
        with (
            patch.object(native_copy_stream, "connect", return_value=context),
            patch.object(transport, "_read_remote", native_copy_stream._read_remote),
            self.assertRaises(transport.TestingRouteWriteNotStarted),
        ):
            self.client._run({"mode": "publish"})
        self.assertEqual(len(connection.sent), 2)

    def test_permission_denial_is_propagated_without_an_alternate_writer(self) -> None:
        self.deny = True
        for message in (
            b"permission denied while trying to connect to the Docker daemon socket",
            b"permission denied while trying to connect to the docker API at unix:///socket",
        ):
            with self.subTest(message=message):
                self.denial_message = message
                self.effect_count = 0
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
        execution = self.client.execution_intent(
            self.plan, self.plan.old_backend, operation_id="fixture"
        )
        with patch.object(self.client, "read") as read, self.assertRaises(ValueError):
            self.client.publish(
                different,
                different.old_backend,
                expected_backend=different.old_backend,
                dispatch_guard=self.guard,
                desired_runtime_identity=self.identity,
                execution=execution,
                execution_key=execution.key(attempt=1),
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
