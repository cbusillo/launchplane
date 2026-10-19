"""Original native copy qualification and crash-safe physical reclamation."""

import ast
import base64
from contextlib import redirect_stdout
import io
import sys
from copy import deepcopy
from datetime import UTC, datetime, timezone
import fcntl
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time
from typing import Any
import unittest
import uuid
from unittest.mock import patch

from control_plane._odoo_copy_runtime_guard import COPY_RUNTIME_INSPECTION_LIBRARY
from control_plane._odoo_testing_native_maintenance_program import NATIVE_MAINTENANCE_LIBRARY
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.odoo_testing_drill import OdooTestingDrillStorageBudget
from control_plane.dokploy.testing_retained_copy import qualify_retained_copy
from control_plane.odoo_testing_restore import TESTING_COPY_COMPLETION_PARAMETER, generation_binding
from tests import test_odoo_testing_restore as restore_tests


class NativeMaintenanceHostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = restore_tests.FreshTestingHostProgramTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.state = self.root / "state"
        self.locks = self.root / "locks"
        self.state.mkdir(mode=0o700)
        self.locks.mkdir(mode=0o700)
        self.containers = deepcopy(self.fixture.containers)
        self.volumes = deepcopy(self.fixture.volumes)
        self.now = datetime.now(UTC).timestamp()
        self.monotonic = 10.0
        self.fixture.gen = self.fixture.gen.model_copy(
            update={
                "prepared_at": datetime.fromtimestamp(self.now - 3600, UTC).isoformat(),
                "completed_at": datetime.fromtimestamp(self.now - 3000, UTC).isoformat(),
            }
        )
        self.fixture.spec["generation"] = self.fixture.gen.model_dump(mode="json")
        self.login: str | None = None
        self.witness = generation_binding(self.fixture.gen)
        self.calls: list[tuple[str, ...]] = []
        self.lose_after: tuple[str, str] | None = None
        self.remove_before_loss = True
        self.runtime = {
            "context": self.fixture.gen.context,
            "instance": "testing",
            "artifact_id": "retirement-build",
        }
        for name, volume in self.volumes.items():
            volume.update(
                Name=name, CreatedAt=datetime.fromtimestamp(self.now - 100, UTC).isoformat()
            )
        for container in self.containers.values():
            container.update(
                RestartCount=0,
                State={"Status": "running", "StartedAt": "original-start", "FinishedAt": ""},
            )
            container["Config"]["Env"].append(
                "LAUNCHPLANE_RUNTIME_IDENTITY_JSON=" + json.dumps(self.runtime)
            )
            for mount in container["Mounts"]:
                mount.update(RW=True, Source=self.volumes[mount["Name"]]["Mountpoint"])
        self.network_members = set(self.containers)
        self.network_name = self.fixture.gen.namespace + "_default"
        self.networks: dict[str, dict[str, Any]] = {
            "original-private-network": {
                "Id": "original-private-network",
                "Name": self.network_name,
                "Internal": True,
                "Driver": "bridge",
                "Scope": "local",
                "Ingress": False,
                "Labels": {
                    "com.docker.compose.project": self.fixture.gen.namespace,
                    "com.docker.compose.network": "default",
                },
            }
        }
        for container in self.containers.values():
            networks = container["NetworkSettings"]["Networks"]
            networks[self.network_name] = networks.pop("testing-data")
        self.host: dict[str, Any] = {
            "fcntl": fcntl,
            "hashlib": hashlib,
            "json": json,
            "os": os,
            "Path": Path,
            "re": re,
            "stat": stat,
            "subprocess": subprocess,
            "tempfile": tempfile,
            "time": time,
            "uuid": uuid,
            "datetime": datetime,
            "timezone": timezone,
            "Any": Any,
            "docker": self.docker,
            "one_inspection": self.one_inspection,
        }
        exec(
            COPY_RUNTIME_INSPECTION_LIBRARY
            + inspect.getsource(qualify_retained_copy)
            + NATIVE_MAINTENANCE_LIBRARY,
            self.host,
        )
        logical = qualify_retained_copy(
            list(self.containers.values()),
            {
                "project": self.fixture.gen.namespace,
                "context": self.fixture.gen.context,
                "identity": self.runtime,
            },
        )
        backings = self.host["inspect_volume_backings"](
            logical["volumes"], list(self.containers.values()), set(self.containers)
        )
        identity = {
            "namespace": self.fixture.gen.namespace,
            "resources_binding": self.fixture.gen.resources_binding_sha256,
            "copy_binding": self.witness,
            "context": self.fixture.gen.context,
            "physical_binding": canonical_json_sha256(
                {"logical": logical["resource_binding"], "backings": backings}
            ),
        }
        self.spec = {
            **self.fixture.spec,
            "identity": identity,
            "identity_sha256": canonical_json_sha256(identity),
            "runtime_identity": self.runtime,
            "copy_witness_parameter": TESTING_COPY_COMPLETION_PARAMETER,
            "recent_use_seconds": 24 * 60 * 60,
            "storage_maintenance": True,
            "expires_at": self.now + 120,
        }
        self.spec["generation"]["prepared_at"] = datetime.fromtimestamp(
            self.now - 3600, UTC
        ).isoformat()
        self.spec.update(host_boot_id="fixture-boot", host_expires_at=self.monotonic + 120)
        self.host["native_host_clock"] = lambda: {
            "host_boot_id": "fixture-boot",
            "host_monotonic": self.monotonic,
            "host_observed_at": self.now,
        }

    def framed_mode(
        self, payload: dict[str, Any], *, stderr: bytes | None = None, default_layout: bool = False
    ) -> dict[str, Any]:
        """Run the real generated framing and command classifier with fixture host dependencies."""
        from control_plane.dokploy.testing_native_maintenance import native_maintenance_host_program

        program = ast.parse(native_maintenance_host_program())
        entry = next(item for item in program.body if isinstance(item, ast.Try))
        namespace: dict[str, Any] = {}
        exec(
            compile(
                ast.Module(
                    body=[item for item in program.body if item is not entry], type_ignores=[]
                ),
                "<native-host-definitions>",
                "exec",
            ),
            namespace,
        )
        host = namespace["NativeMaintenanceHost"]
        original_init = host.__init__

        def initialize(executor: Any, spec: dict[str, Any], command: Any, **_options: Any) -> None:
            original_init(
                executor,
                spec,
                command,
                receipt_root=None if default_layout else self.state,
                lock_root=self.locks,
                clock=lambda: self.now,
                monotonic=lambda: self.monotonic,
            )

        namespace["native_host_clock"] = self.host["native_host_clock"]

        def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
            if stderr is not None:
                self.calls.append(tuple(command[1:]))
                return subprocess.CompletedProcess(command, 1, stdout=b"", stderr=stderr)
            output = self.docker(
                *command[1:], stdin=kwargs.get("input"), pass_fds=kwargs.get("pass_fds", ())
            )
            return subprocess.CompletedProcess(command, 0, stdout=output, stderr=b"")

        stdout = io.StringIO()
        home = self.root / "fixture-home"
        home.mkdir(mode=0o700, exist_ok=True)
        with (
            redirect_stdout(stdout),
            patch.object(Path, "home", return_value=home),
            patch.object(host, "__init__", initialize),
            patch.object(subprocess, "run", side_effect=run),
            patch.object(
                sys,
                "argv",
                ["fixture", base64.b64encode(json.dumps(payload).encode()).decode(), "FRAME:"],
            ),
        ):
            try:
                exec(
                    compile(
                        ast.Module(body=[entry], type_ignores=[]), "<native-host-entry>", "exec"
                    ),
                    namespace,
                )
            except SystemExit as error:
                self.assertEqual(error.code, 1)
        return dict(json.loads(stdout.getvalue().removeprefix("FRAME:")))

    def one_inspection(self, identifier: str) -> dict[str, Any]:
        return dict(json.loads(self.docker("inspect", identifier))[0])

    def docker(self, *arguments: str, stdin: bytes | None = None, **kwargs: Any) -> bytes:
        self.calls.append(arguments)
        if arguments[:2] == ("ps", "-aq"):
            return "\n".join(self.containers).encode()
        if arguments[0] == "inspect":
            return json.dumps(
                [
                    {
                        **next(
                            network
                            for network in self.networks.values()
                            if identifier in {network["Id"], network["Name"]}
                        ),
                        "Containers": dict.fromkeys(self.network_members, {}),
                    }
                    if identifier == self.network_name or identifier in self.networks
                    else self.containers[identifier]
                    for identifier in arguments[1:]
                ]
            ).encode()
        if arguments[:2] == ("volume", "inspect"):
            return json.dumps([self.volumes[arguments[2]]]).encode()
        if arguments[:2] == ("volume", "ls"):
            return "\n".join(self.volumes).encode()
        if arguments[:2] == ("network", "ls"):
            return "\n".join(
                json.dumps({"ID": network["Id"], "Name": network["Name"]})
                for network in self.networks.values()
            ).encode()
        if arguments[0] == "exec":
            self.assertEqual(arguments[-3], self.fixture.database)
            self.assertTrue(
                all(
                    self.containers[name]["State"]["Status"] == "exited"
                    for name in ("web", "script-runner")
                )
            )
            return json.dumps({"witness": self.witness, "max_login_at": self.login}).encode()
        if arguments[0] == "stop":
            values = iter(arguments[1:-1])
            options = {}
            for argument in values:
                name, separator, value = argument.partition("=")
                options[name] = value if separator else next(values)
            self.assertEqual(int(options.get("--timeout", options.get("-t", "0"))), -1)
            for descriptor in kwargs["pass_fds"]:
                os.fstat(descriptor)
            self.containers[arguments[-1]]["State"].update(
                Status="exited", FinishedAt="retirement-finished"
            )
            return b""
        if arguments[1] == "rm":
            kind, name = arguments[0], arguments[2]
            self.assertEqual(len(arguments), 3)
            target = {
                "container": self.containers,
                "volume": self.volumes,
                "network": self.networks,
            }[kind]
            if self.lose_after == (kind, name) and not self.remove_before_loss:
                raise TimeoutError("fixture CLI lost before daemon outcome")
            del target[name]
            self.network_members.discard(name)
            if self.lose_after == (kind, name):
                raise TimeoutError("fixture result lost after original removal")
            return b""
        self.fail(f"Unexpected native host command: {arguments}")

    def run_mode(self, mode: str) -> dict[str, Any]:
        return dict(
            self.host["native_maintenance_main"](
                {**self.spec, "mode": mode},
                self.docker,
                receipt_root=self.state,
                lock_root=self.locks,
                clock=lambda: self.now,
                monotonic=lambda: self.monotonic,
            )
        )

    def test_qualified_original_copy_removes_exact_resources_and_replays_tombstone(self) -> None:
        self.assertEqual(self.run_mode("qualify-retirement")["status"], "eligible")
        self.assertEqual(self.run_mode("dispose-original")["status"], "disposed")
        self.assertFalse(self.containers)
        self.assertFalse(self.volumes)
        self.assertFalse(self.networks)
        mutations = [call for call in self.calls if len(call) > 1 and call[1] == "rm"]
        self.assertEqual(self.run_mode("dispose-original")["status"], "disposed")
        self.assertEqual(
            mutations, [call for call in self.calls if len(call) > 1 and call[1] == "rm"]
        )

    def test_recent_login_ages_out_without_restarting_the_original_database(self) -> None:
        self.login = datetime.fromtimestamp(self.now - 60, UTC).isoformat()
        self.assertEqual(self.run_mode("qualify-retirement")["status"], "recent_use")
        self.assertEqual(self.run_mode("dispose-original")["status"], "recent_use")
        self.now += 24 * 60 * 60
        self.spec["expires_at"] = self.now + 120
        self.assertEqual(self.run_mode("dispose-original")["status"], "disposed")
        self.assertFalse(any(call[0] == "start" for call in self.calls))

    def test_wrong_original_witness_preserves_storage_after_ordinary_retirement(self) -> None:
        self.witness = "wrong-original-copy"
        self.assertEqual(self.run_mode("qualify-retirement")["status"], "unqualified")
        self.assertEqual(self.run_mode("dispose-original")["status"], "unqualified")
        self.assertTrue(self.volumes)

    def test_backing_drift_after_control_plane_admission_refuses_before_stopping(self) -> None:
        self.spec["identity"]["physical_binding"] = "a" * 64
        self.spec["identity_sha256"] = canonical_json_sha256(self.spec["identity"])
        self.assertEqual(self.run_mode("qualify-retirement")["status"], "protected")
        self.assertFalse(any(call[0] == "stop" for call in self.calls))

    def test_foreign_private_network_member_refuses_before_quiescing(self) -> None:
        self.network_members.add("foreign-consumer")
        self.assertEqual(self.run_mode("qualify-retirement")["status"], "protected")
        self.assertFalse(any(call[0] == "stop" for call in self.calls))
        for mismatch in ("empty_networks", "multiple_networks", "wrong_networks", "empty_mounts"):
            with self.subTest(mismatch=mismatch):
                fixture = NativeMaintenanceHostTests()
                fixture.setUp()
                try:
                    database = fixture.containers["database"]
                    if mismatch == "empty_mounts":
                        database["Mounts"] = []
                    else:
                        networks = database["NetworkSettings"]["Networks"]
                        if mismatch == "empty_networks":
                            networks.clear()
                        elif mismatch == "multiple_networks":
                            networks["foreign-network"] = {}
                        else:
                            networks["dokploy-network"] = networks.pop(fixture.network_name)
                            fixture.networks["dokploy-network"] = {
                                **fixture.networks["original-private-network"],
                                "Id": "dokploy-network",
                                "Name": "dokploy-network",
                            }
                    self.assertEqual(
                        fixture.framed_mode({**fixture.spec, "mode": "qualify-retirement"})[
                            "status"
                        ],
                        "protected",
                    )
                    self.assertFalse(any(call[0] == "stop" for call in fixture.calls))
                finally:
                    fixture.doCleanups()

    def test_restart_or_replacement_after_retirement_never_qualifies_for_removal(self) -> None:
        self.run_mode("qualify-retirement")
        self.containers["web"]["RestartCount"] += 1
        self.assertEqual(self.run_mode("dispose-original")["status"], "protected")
        self.assertTrue(self.volumes)

    def test_same_name_volume_replacement_and_foreign_bind_consumer_refuse(self) -> None:
        self.run_mode("qualify-retirement")
        name = next(iter(self.volumes))
        original = deepcopy(self.volumes[name])
        self.volumes[name]["CreatedAt"] = "replacement"
        self.assertEqual(self.run_mode("dispose-original")["status"], "protected")
        self.volumes[name] = original
        self.containers["foreign"] = {
            "Id": "foreign",
            "Config": {},
            "Mounts": [{"Type": "bind", "Source": original["Mountpoint"]}],
        }
        self.assertEqual(self.run_mode("dispose-original")["status"], "protected")

    def test_new_private_network_consumer_after_retirement_protects_the_copy(self) -> None:
        self.run_mode("qualify-retirement")
        self.network_members.add("new-foreign-consumer")
        self.assertEqual(self.run_mode("dispose-original")["status"], "protected")
        self.assertTrue(self.volumes)

    def test_private_network_requires_exact_original_compose_ownership(self) -> None:
        network = self.networks["original-private-network"]
        labels = dict(network["Labels"])
        network["Labels"] = {**labels, "com.docker.compose.project": "foreign-project"}
        self.assertEqual(self.run_mode("qualify-retirement")["status"], "protected")
        self.assertFalse(any(call[0] == "stop" for call in self.calls))

    def test_same_name_replacement_network_is_preserved(self) -> None:
        self.run_mode("qualify-retirement")
        original = self.networks.pop("original-private-network")
        self.networks["replacement-network"] = {**original, "Id": "replacement-network"}
        self.assertEqual(self.run_mode("dispose-original")["status"], "protected")
        self.assertTrue(self.containers)
        self.assertIn("replacement-network", self.networks)

    def test_nonlocal_network_is_not_given_deletion_authority(self) -> None:
        self.networks["original-private-network"].update(Driver="overlay", Scope="swarm")
        self.assertEqual(self.run_mode("qualify-retirement")["status"], "protected")
        self.assertFalse(any(call[0] == "stop" for call in self.calls))

    def test_lost_network_removal_reply_is_read_back_without_a_second_removal(self) -> None:
        self.run_mode("qualify-retirement")
        self.lose_after = ("network", "original-private-network")
        self.assertEqual(self.run_mode("dispose-original")["status"], "observation_unavailable")
        self.assertEqual(self.run_mode("inspect-original")["status"], "disposed")
        self.assertEqual(self.run_mode("dispose-original")["status"], "disposed")
        self.assertEqual(self.calls.count(("network", "rm", "original-private-network")), 1)

    def test_uncertain_network_removal_preserves_original_dispatch_without_replay(self) -> None:
        self.run_mode("qualify-retirement")
        self.lose_after = ("network", "original-private-network")
        self.remove_before_loss = False
        self.assertEqual(self.run_mode("dispose-original")["status"], "observation_unavailable")
        self.now += 121
        self.monotonic += 121
        self.spec.update(expires_at=self.now + 120, host_expires_at=self.monotonic + 120)
        self.assertEqual(self.run_mode("dispose-original")["status"], "recovery_required")
        self.assertEqual(self.calls.count(("network", "rm", "original-private-network")), 1)

    def test_partial_removal_lost_reply_resumes_only_original_resources_after_exclusion(
        self,
    ) -> None:
        self.run_mode("qualify-retirement")
        self.lose_after = ("container", "web")
        self.assertEqual(self.run_mode("dispose-original")["status"], "observation_unavailable")
        self.assertEqual(self.run_mode("inspect-original")["status"], "incomplete")
        self.now += 121
        self.monotonic += 121
        self.spec["expires_at"] = self.now + 120
        self.spec["host_expires_at"] = self.monotonic + 120
        self.assertEqual(self.run_mode("inspect-original")["status"], "resumable")
        self.lose_after = None
        self.assertEqual(self.run_mode("dispose-original")["status"], "disposed")
        self.assertEqual(self.calls.count(("container", "rm", "web")), 1)

    def test_uncertain_still_present_dispatch_is_never_resent_after_expiry(self) -> None:
        self.run_mode("qualify-retirement")
        self.lose_after = ("container", "web")
        self.remove_before_loss = False
        self.assertEqual(self.run_mode("dispose-original")["status"], "observation_unavailable")
        self.now += 121
        self.monotonic += 121
        self.spec["expires_at"] = self.now + 120
        self.spec["host_expires_at"] = self.monotonic + 120
        self.assertEqual(self.run_mode("dispose-original")["status"], "recovery_required")
        self.assertEqual(self.calls.count(("container", "rm", "web")), 1)

    def test_partial_volume_removal_uses_the_original_set_after_a_lost_reply(self) -> None:
        self.run_mode("qualify-retirement")
        name = next(iter(self.volumes))
        self.lose_after = ("volume", name)
        self.assertEqual(self.run_mode("dispose-original")["status"], "observation_unavailable")
        self.now += 121
        self.monotonic += 121
        self.spec["expires_at"] = self.now + 120
        self.spec["host_expires_at"] = self.monotonic + 120
        self.lose_after = None
        self.assertEqual(self.run_mode("dispose-original")["status"], "disposed")
        self.assertEqual(self.calls.count(("volume", "rm", name)), 1)

    def test_receipt_symlink_cannot_redirect_native_authority(self) -> None:
        target = self.root / "foreign.json"
        target.write_text("{}")
        (self.state / ("native-" + self.spec["identity_sha256"] + ".json")).symlink_to(target)
        self.assertEqual(
            self.run_mode("qualify-retirement")["reason"], "native_copy_receipt_untrusted"
        )
        self.assertFalse(any(call[0] == "stop" for call in self.calls))

    def test_host_wall_clock_rollback_cannot_extend_an_original_boot_deadline(self) -> None:
        self.monotonic += 121
        self.now -= 1000
        self.assertEqual(self.run_mode("qualify-retirement")["status"], "observation_unavailable")
        self.assertFalse(any(call[0] == "stop" for call in self.calls))

    def test_expired_original_payload_and_busy_host_lock_cannot_dispatch(self) -> None:
        self.spec["expires_at"] = self.now - 1
        self.assertEqual(self.run_mode("qualify-retirement")["status"], "observation_unavailable")
        self.spec["expires_at"] = self.now + 120
        executor = self.host["NativeMaintenanceHost"](
            self.spec, self.docker, receipt_root=self.state, lock_root=self.locks
        )
        executor.open()
        try:
            self.assertEqual(self.run_mode("qualify-retirement")["status"], "active")
        finally:
            executor.close()
        self.assertFalse(any(call[0] == "stop" for call in self.calls))

    def test_retention_policy_extension_preserves_existing_generation_serialization(self) -> None:
        original = OdooTestingDrillStorageBudget(
            database_bytes=1, filestore_bytes=1, staging_bytes=1, reserve_bytes=1
        )
        empty = OdooTestingDrillStorageBudget.model_validate(
            {**original.model_dump(), "retained_candidate_namespaces": []}
        )
        self.assertEqual(original.model_dump(), empty.model_dump())
        retained = original.model_copy(
            update={"retained_candidate_namespaces": (self.fixture.gen.namespace,)}
        )
        self.assertNotEqual(original.model_dump(), retained.model_dump())

    def next_admission(self) -> None:
        self.now += 121
        self.monotonic += 121
        self.spec.update(expires_at=self.now + 120, host_expires_at=self.monotonic + 120)

    def test_original_no_dispatch_retirement_continues_only_after_exclusion(self) -> None:
        self.network_members.add("foreign")
        self.assertEqual(self.run_mode("qualify-retirement")["status"], "protected")
        self.assertFalse(any(call[0] == "stop" for call in self.calls))
        self.network_members.remove("foreign")
        self.assertEqual(self.run_mode("inspect-original")["status"], "active")
        old = dict(self.spec)
        self.next_admission()
        self.assertEqual(self.run_mode("inspect-original")["status"], "resumable_retirement")
        self.assertEqual(self.run_mode("qualify-retirement")["status"], "eligible")
        mutations = [call for call in self.calls if call[0] == "stop"]
        result = self.host["native_maintenance_main"](
            {**old, "mode": "qualify-retirement"},
            self.docker,
            receipt_root=self.state,
            lock_root=self.locks,
            clock=lambda: self.now,
            monotonic=lambda: self.monotonic,
        )
        self.assertEqual(result["status"], "observation_unavailable")
        self.assertEqual(mutations, [call for call in self.calls if call[0] == "stop"])

    def test_lost_stop_reply_continues_only_remaining_original_steps(self) -> None:
        for service in ("web", "script-runner", "database"):
            with self.subTest(service=service):
                fixture = NativeMaintenanceHostTests()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                command = fixture.docker

                def lost(*arguments: str, **kwargs: Any) -> bytes:
                    result = command(*arguments, **kwargs)
                    if arguments[0] == "stop" and arguments[-1] == service:
                        raise TimeoutError("fixture lost original stop reply")
                    return result

                with patch.object(fixture, "docker", side_effect=lost):
                    self.assertEqual(
                        fixture.run_mode("qualify-retirement")["status"], "observation_unavailable"
                    )
                if service != "database":
                    self.assertEqual(fixture.run_mode("inspect-original")["status"], "active")
                    fixture.next_admission()
                    self.assertEqual(
                        fixture.run_mode("inspect-original")["status"], "resumable_retirement"
                    )
                self.assertEqual(fixture.run_mode("qualify-retirement")["status"], "eligible")
                self.assertEqual(
                    sum(call[0] == "stop" and call[-1] == service for call in fixture.calls), 1
                )

    def test_uncertain_running_stop_is_never_repeated_after_expiry(self) -> None:
        command = self.docker

        def unknown(*arguments: str, **kwargs: Any) -> bytes:
            if arguments[0] == "stop":
                self.calls.append(arguments)
                raise TimeoutError("fixture daemon work remains unknown")
            return command(*arguments, **kwargs)

        with patch.object(self, "docker", side_effect=unknown):
            self.assertEqual(
                self.run_mode("qualify-retirement")["status"], "observation_unavailable"
            )
        self.next_admission()
        self.assertEqual(self.run_mode("inspect-original")["status"], "recovery_required")
        self.assertEqual(self.run_mode("qualify-retirement")["status"], "recovery_required")
        self.assertEqual(sum(call[0] == "stop" for call in self.calls), 1)
        self.assertTrue(self.volumes)
        self.containers["web"]["State"].update(Status="exited", FinishedAt="late-original-stop")
        self.assertEqual(self.run_mode("inspect-original")["status"], "resumable_retirement")
        self.assertEqual(self.run_mode("qualify-retirement")["status"], "eligible")
        self.assertEqual(sum(call[0] == "stop" and call[-1] == "web" for call in self.calls), 1)

    def test_history_is_durable_before_database_stop_and_finalization_recovers(self) -> None:
        self.login = datetime.fromtimestamp(self.now - 60, UTC).isoformat()
        command = self.docker
        failed = False

        def lost(*arguments: str, **kwargs: Any) -> bytes:
            nonlocal failed
            if (
                arguments[:2] == ("ps", "-aq")
                and all(item["State"]["Status"] == "exited" for item in self.containers.values())
                and not failed
            ):
                failed = True
                raise RuntimeError("fixture inventory failed after all stops")
            return command(*arguments, **kwargs)

        with patch.object(self, "docker", side_effect=lost):
            self.assertEqual(
                self.run_mode("qualify-retirement")["status"], "observation_unavailable"
            )
        self.assertEqual(self.run_mode("inspect-original")["status"], "recent_use")
        self.assertEqual(sum(call[0] == "exec" for call in self.calls), 1)
        self.assertEqual(sum(call[0] == "stop" for call in self.calls), 3)

    def test_stopped_database_never_creates_unused_history(self) -> None:
        self.containers["database"]["State"]["Status"] = "exited"
        self.assertEqual(self.run_mode("qualify-retirement")["status"], "unqualified")
        self.assertFalse(any(call[0] in {"exec", "start"} for call in self.calls))
        self.assertTrue(self.volumes)

    def test_deterministic_runtime_drift_can_only_seal_without_stopping(self) -> None:
        self.containers["one-off"] = deepcopy(self.containers["web"])
        self.containers["one-off"]["Id"] = "one-off"
        self.assertEqual(self.run_mode("qualify-retirement")["status"], "protected")
        seal = self.run_mode("seal-retirement")
        self.assertEqual(seal["status"], "preserved")
        self.assertTrue(seal["no_stop_dispatched"])
        self.next_admission()
        self.assertEqual(self.run_mode("qualify-retirement")["status"], "preserved")
        self.assertFalse(any(call[0] == "stop" for call in self.calls))

    def test_observed_completed_stop_never_becomes_no_stop_proof(self) -> None:
        command = self.docker

        def lost(*arguments: str, **kwargs: Any) -> bytes:
            result = command(*arguments, **kwargs)
            if arguments[0] == "stop":
                raise TimeoutError("fixture original stop reply lost")
            return result

        with patch.object(self, "docker", side_effect=lost):
            self.assertEqual(
                self.run_mode("qualify-retirement")["status"], "observation_unavailable"
            )
        self.next_admission()
        self.assertEqual(self.run_mode("inspect-original")["status"], "resumable_retirement")
        self.assertNotEqual(self.run_mode("seal-retirement")["status"], "preserved")
        self.assertEqual(sum(call[0] == "stop" for call in self.calls), 1)

    def test_partial_stop_with_changed_or_missing_original_keeps_uncertain_custody(self) -> None:
        for change in ("restart", "prune", "labels"):
            with self.subTest(change=change):
                fixture = NativeMaintenanceHostTests()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                command = fixture.docker

                def lost(*arguments: str, **kwargs: Any) -> bytes:
                    result = command(*arguments, **kwargs)
                    if arguments[0] == "stop":
                        raise TimeoutError("fixture daemon stop reply lost")
                    return result

                with patch.object(fixture, "docker", side_effect=lost):
                    fixture.run_mode("qualify-retirement")
                fixture.next_admission()
                if change == "restart":
                    fixture.containers["database"]["State"]["StartedAt"] = "new-lifetime"
                    fixture.containers["database"]["RestartCount"] += 1
                elif change == "prune":
                    del fixture.containers["web"]
                else:
                    fixture.containers["database"]["Config"]["Labels"]["replacement"] = "true"
                self.assertEqual(
                    fixture.run_mode("inspect-original")["status"], "recovery_required"
                )
                self.assertNotEqual(fixture.run_mode("seal-retirement")["status"], "preserved")
                self.assertEqual(sum(call[0] == "stop" for call in fixture.calls), 1)
                self.assertTrue(fixture.volumes)

    def test_expiry_after_stop_wal_before_command_has_positive_no_send_proof(self) -> None:
        host = self.host["NativeMaintenanceHost"]
        save = host.save

        def expire(executor: Any, receipt: dict[str, Any]) -> None:
            save(executor, receipt)
            if any(
                step.get("state") == "dispatching"
                for step in receipt.get("retirement", {}).get("stops", {}).values()
            ):
                self.monotonic = float(self.spec["host_expires_at"]) + 1

        with patch.object(host, "save", expire):
            self.assertEqual(
                self.run_mode("qualify-retirement")["status"], "observation_unavailable"
            )
        self.assertFalse(any(call[0] == "stop" for call in self.calls))
        self.assertEqual(self.run_mode("seal-retirement")["status"], "observation_unavailable")
        self.next_admission()
        self.assertEqual(self.run_mode("seal-retirement")["status"], "preserved")

    def test_expiry_after_removal_wal_before_command_can_continue_without_resend(self) -> None:
        self.assertEqual(self.run_mode("qualify-retirement")["status"], "eligible")
        host = self.host["NativeMaintenanceHost"]
        save = host.save

        def expire(executor: Any, receipt: dict[str, Any]) -> None:
            save(executor, receipt)
            if any(
                step.get("state") == "dispatching" for step in receipt.get("resources", {}).values()
            ):
                self.monotonic = float(self.spec["host_expires_at"]) + 1

        with patch.object(host, "save", expire):
            self.assertEqual(self.run_mode("dispose-original")["status"], "observation_unavailable")
        self.assertFalse(any(len(call) > 1 and call[1] == "rm" for call in self.calls))
        self.next_admission()
        self.assertEqual(self.run_mode("dispose-original")["status"], "disposed")
        removals = [call for call in self.calls if len(call) > 1 and call[1] == "rm"]
        self.assertEqual(len(removals), len(set(removals)))

    def test_state_mount_exposure_refuses_both_stop_and_seal(self) -> None:
        originals = deepcopy(self.containers)
        for missing in ("status", "networks", "mounts"):
            self.containers = deepcopy(originals)
            if missing == "status":
                self.containers["database"]["State"]["Status"] = None
            elif missing == "networks":
                self.containers["database"]["NetworkSettings"]["Networks"] = None
            else:
                self.containers["database"]["Mounts"] = None
            self.containers["foreign"] = {
                "Id": "foreign",
                "Mounts": [{"Source": str(self.state), "RW": True}],
            }
            with self.subTest(unavailable=missing):
                self.assertEqual(
                    self.framed_mode({**self.spec, "mode": "inspect-original"})["reason"],
                    "native_copy_receipt_untrusted",
                )
        self.containers = originals
        self.containers["foreign"] = {
            "Id": "foreign",
            "Mounts": [{"Source": str(self.root), "RW": True}],
        }
        self.assertEqual(self.run_mode("qualify-retirement")["status"], "protected")
        self.assertEqual(self.run_mode("seal-retirement")["status"], "protected")
        self.assertFalse(any(call[0] == "stop" for call in self.calls))

    def test_shared_receipt_exposure_keeps_the_exact_namespace_scope(self) -> None:
        for own in (True, False):
            with self.subTest(own=own):
                fixture = NativeMaintenanceHostTests()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                home = fixture.root / "fixture-home"
                root = home / ".local/state/launchplane-testing-disposal"
                source = root if own else root / ("lp-testing-" + "b" * 32)
                fixture.containers["foreign"] = {
                    "Id": "foreign",
                    "Config": {"Labels": {}},
                    "Mounts": [{"Source": str(source), "RW": True}],
                }
                result = fixture.framed_mode(
                    {**fixture.spec, "mode": "qualify-retirement"}, default_layout=True
                )
                self.assertEqual(result["status"], "protected" if own else "eligible")
                if own:
                    self.assertEqual(result["reason"], "native_copy_receipt_untrusted")
                self.assertNotIn(str(source), json.dumps(result))
                self.assertEqual(sum(call[0] == "stop" for call in fixture.calls), 0 if own else 3)

    def test_namespace_replacement_cannot_supply_missing_receipt_exclusion(self) -> None:
        original = self.run_mode("qualify-retirement")
        self.assertEqual(original["status"], "eligible")
        self.spec["receipt_namespaces"] = [original["namespace_sha256"]]
        previous = self.state.with_name("previous-state")
        self.state.rename(previous)
        self.state.mkdir(mode=0o700)
        receipt = next(previous.glob("native-*.json"))
        (self.state / receipt.name).write_bytes(receipt.read_bytes())
        (self.state / receipt.name).chmod(0o600)
        calls = list(self.calls)
        self.assertEqual(
            self.run_mode("dispose-original")["reason"], "native_copy_receipt_untrusted"
        )
        self.assertEqual(self.run_mode("seal-retirement")["status"], "protected")
        self.assertFalse(
            any(len(call) > 1 and call[1] == "rm" for call in self.calls[len(calls) :])
        )

    def test_namespace_nonce_survives_directory_metadata_change_and_detects_reused_metadata(
        self,
    ) -> None:
        original = self.run_mode("inspect-original")
        self.spec["receipt_namespaces"] = [original["namespace_sha256"]]
        fstat = os.fstat

        def changed(descriptor: int) -> os.stat_result:
            metadata = fstat(descriptor)
            if stat.S_ISDIR(metadata.st_mode):
                values = list(metadata)
                values[2] += 1
                return os.stat_result(values)
            return metadata

        with patch.object(os, "fstat", side_effect=changed):
            self.assertEqual(
                self.run_mode("inspect-original")["namespace_sha256"], original["namespace_sha256"]
            )
        # Same directory/inode and copied success-looking WAL do not authenticate
        # a different marker. Physical volume identity is deliberately untouched.
        (self.state / "namespace-id").write_text(os.urandom(32).hex())
        self.assertEqual(
            self.run_mode("inspect-original")["reason"], "native_copy_receipt_untrusted"
        )
        (self.state / "namespace-id").write_bytes(b"\xff")
        self.assertEqual(
            self.framed_mode({**self.spec, "mode": "inspect-original"})["reason"],
            "native_copy_receipt_untrusted",
        )
        self.assertFalse(any(call[0] == "stop" for call in self.calls))

    def test_transient_history_read_retries_only_before_the_running_database_stops(self) -> None:
        command = self.docker
        reads = 0

        def transient(*arguments: str, **kwargs: Any) -> bytes:
            nonlocal reads
            if arguments[0] == "exec":
                reads += 1
                if reads == 1:
                    raise RuntimeError("fixture transient SQL transport")
            return command(*arguments, **kwargs)

        with patch.object(self, "docker", side_effect=transient):
            self.assertEqual(self.run_mode("qualify-retirement")["status"], "eligible")
        self.assertEqual(reads, 2)
        self.assertEqual(
            sum(call[0] == "stop" and call[-1] == "database" for call in self.calls), 1
        )

    def test_local_io_and_shape_errors_stay_typed_and_cannot_invent_empty_wal(self) -> None:
        for error in (
            OSError("fixture IO"),
            PermissionError("fixture filesystem"),
            KeyError("fixture shape"),
        ):
            with (
                self.subTest(error=type(error).__name__),
                patch.object(self, "docker", side_effect=error),
            ):
                self.assertIn(
                    self.run_mode("inspect-original")["status"],
                    {"protected", "observation_unavailable"},
                )
        self.assertFalse(any(call[0] == "stop" for call in self.calls))
        host = self.host["NativeMaintenanceHost"]
        with patch.object(host, "read", side_effect=OSError("fixture unreadable receipt")):
            self.assertEqual(self.run_mode("seal-retirement")["status"], "recovery_required")

    def test_malformed_receipt_shape_cannot_become_an_empty_no_stop_wal(self) -> None:
        receipt = self.state / ("native-" + self.spec["identity_sha256"] + ".json")
        with os.fdopen(os.open(receipt, os.O_CREAT | os.O_WRONLY, 0o600), "w") as handle:
            json.dump([], handle)
        self.assertEqual(
            self.run_mode("seal-retirement")["reason"], "native_copy_receipt_untrusted"
        )
        self.assertFalse(any(call[0] == "stop" for call in self.calls))

    def test_unavailable_inventory_retries_and_read_time_exposure_taints_the_original(self) -> None:
        command = self.docker

        def incomplete(*arguments: str, **kwargs: Any) -> bytes:
            if (
                len(arguments) == 2
                and arguments[0] == "inspect"
                and arguments[1] in self.containers
            ):
                return b"[]"
            return command(*arguments, **kwargs)

        with patch.object(self, "docker", side_effect=incomplete):
            self.assertEqual(
                self.framed_mode({**self.spec, "mode": "qualify-retirement"})["status"],
                "observation_unavailable",
            )
        self.assertFalse(any(call[0] == "stop" for call in self.calls))
        self.next_admission()

        def malformed(*arguments: str, **kwargs: Any) -> bytes:
            return b"[" if arguments[0] == "inspect" else command(*arguments, **kwargs)

        with patch.object(self, "docker", side_effect=malformed):
            self.assertEqual(
                self.run_mode("qualify-retirement")["status"], "observation_unavailable"
            )
        for missing in (
            "volume_created_at",
            "process_lifetime",
            "process_status_missing",
            "process_status_null",
            "process_status_unknown",
            "network_members",
            "container_networks",
            "container_mounts",
            "volume_inventory",
        ):
            self.next_admission()

            def unavailable(*arguments: str, **kwargs: Any) -> bytes:
                output = command(*arguments, **kwargs)
                if arguments[:2] == ("volume", "inspect"):
                    if missing == "volume_inventory":
                        return b"[]"
                    if missing == "volume_created_at":
                        values = json.loads(output)
                        values[0].pop("CreatedAt")
                        return json.dumps(values).encode()
                if arguments[0] == "inspect":
                    if (
                        missing
                        in {
                            "process_lifetime",
                            "process_status_missing",
                            "process_status_null",
                            "process_status_unknown",
                            "container_networks",
                            "container_mounts",
                        }
                        and len(arguments) != 2
                    ):
                        return output
                    values = json.loads(output)
                    for value in values:
                        if missing == "process_lifetime" and value.get("Id") in self.containers:
                            value.pop("RestartCount", None)
                        if value.get("Id") in self.containers:
                            if missing == "container_networks":
                                value["NetworkSettings"]["Networks"] = None
                            elif missing == "container_mounts":
                                value["Mounts"] = None
                            if missing == "process_status_missing":
                                value["State"].pop("Status", None)
                            elif missing == "process_status_null":
                                value["State"]["Status"] = None
                            elif missing == "process_status_unknown":
                                value["State"]["Status"] = "unavailable"
                        if missing == "network_members" and value.get("Id") in self.networks:
                            value.pop("Containers", None)
                    return json.dumps(values).encode()
                return output

            with (
                self.subTest(missing=missing),
                patch.object(self, "docker", side_effect=unavailable),
            ):
                self.assertEqual(
                    self.framed_mode({**self.spec, "mode": "qualify-retirement"})["status"],
                    "observation_unavailable",
                )
            self.assertFalse(any(call[0] == "stop" for call in self.calls))
            for receipt in self.state.glob("native-*.json"):
                self.assertNotEqual(json.loads(receipt.read_text())["state"], "preserved")
        for missing in ("absent", "null", "unsupported"):
            self.next_admission()
            observed_owned = set()

            def late_unavailable(*arguments: str, **kwargs: Any) -> bytes:
                output = command(*arguments, **kwargs)
                if arguments[0] != "inspect":
                    return output
                if len(arguments) == 2 and arguments[1] in self.containers:
                    observed_owned.add(arguments[1])
                    return output
                if len(arguments) > 2 and observed_owned == set(self.containers):
                    values = json.loads(output)
                    for value in values:
                        if missing == "absent":
                            value["State"].pop("Status", None)
                        else:
                            value["State"]["Status"] = None if missing == "null" else "unavailable"
                    return json.dumps(values).encode()
                return output

            with (
                self.subTest(late_status=missing),
                patch.object(self, "docker", side_effect=late_unavailable),
            ):
                self.assertEqual(
                    self.framed_mode({**self.spec, "mode": "qualify-retirement"})["status"],
                    "observation_unavailable",
                )
            self.assertEqual(observed_owned, set(self.containers))
        self.assertFalse(any(call[0] == "stop" for call in self.calls))
        self.next_admission()
        self.assertEqual(self.run_mode("qualify-retirement")["status"], "eligible")
        host = self.host["NativeMaintenanceHost"]
        read = host.read

        def exposed(executor: Any) -> Any:
            receipt = read(executor)
            self.containers["foreign"] = {
                "Id": "foreign",
                "Mounts": [{"Source": str(self.state), "RW": True}],
            }
            return receipt

        with patch.object(host, "read", exposed):
            self.assertEqual(
                self.run_mode("dispose-original")["reason"], "native_copy_receipt_untrusted"
            )
        self.assertFalse(any(len(call) > 1 and call[1] == "rm" for call in self.calls))

    def test_new_seal_needs_live_admission_but_existing_seal_is_immutable_observation(self) -> None:
        self.network_members.add("foreign")
        self.assertEqual(self.run_mode("qualify-retirement")["status"], "protected")
        self.monotonic = float(self.spec["host_expires_at"]) + 1
        self.assertEqual(self.run_mode("seal-retirement")["status"], "observation_unavailable")
        receipt = next(self.state.glob("native-*.json"))
        self.assertNotEqual(json.loads(receipt.read_text())["state"], "preserved")
        self.next_admission()
        sealed = self.run_mode("seal-retirement")
        self.assertEqual(sealed["status"], "preserved")
        original = receipt.read_bytes()
        self.monotonic = float(self.spec["host_expires_at"]) + 1
        self.assertEqual(self.run_mode("seal-retirement")["status"], "preserved")
        self.assertEqual(receipt.read_bytes(), original)
        self.assertFalse(any(call[0] == "stop" for call in self.calls))

    def test_legacy_incomplete_receipt_stays_protected(self) -> None:
        executor = self.host["NativeMaintenanceHost"](
            self.spec, self.docker, receipt_root=self.state, lock_root=self.locks
        )
        executor.open()
        try:
            executor.save(
                {
                    "identity": self.spec["identity"],
                    "state": "qualifying",
                    "invocation_boot_id": "earlier-boot",
                    "containers": {},
                }
            )
        finally:
            executor.close()
        self.assertEqual(self.run_mode("inspect-original")["status"], "protected")
        self.assertEqual(self.run_mode("qualify-retirement")["status"], "protected")
        self.assertFalse(any(call[0] == "stop" for call in self.calls))

    def test_provider_denial_cannot_fall_through_to_another_stop(self) -> None:
        for boundary in ("stop", "exec"):
            with self.subTest(boundary=boundary):
                fixture = NativeMaintenanceHostTests()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                command = fixture.docker
                denied_type = fixture.host["NativeMaintenanceProviderDenied"]

                def denied(*arguments: str, **kwargs: Any) -> bytes:
                    if arguments[0] == boundary:
                        fixture.calls.append(arguments)
                        raise denied_type("fixture daemon denied original action")
                    return command(*arguments, **kwargs)

                with (
                    patch.object(fixture, "docker", side_effect=denied),
                    self.assertRaises(denied_type),
                ):
                    fixture.run_mode("qualify-retirement")
                self.assertEqual(sum(call[0] == boundary for call in fixture.calls), 1)
                if boundary == "stop":
                    self.assertEqual(sum(call[0] == "stop" for call in fixture.calls), 1)
                else:
                    self.assertTrue(
                        all(
                            fixture.containers[name]["State"]["Status"] == "exited"
                            for name in ("web", "script-runner")
                        )
                    )
                    self.assertEqual(fixture.containers["database"]["State"]["Status"], "running")
                    self.assertFalse(
                        any(call[0] == "stop" and call[-1] == "database" for call in fixture.calls)
                    )
                self.assertTrue(fixture.volumes)

    def test_generated_host_frames_distinguish_daemon_denial_from_local_errors(self) -> None:
        for stderr, expected in (
            (b"authorization denied by plugin: fixture", "denied"),
            (b"Error response from daemon: unauthorized", "denied"),
            (b"psql: permission denied for table res_users_log", "complete"),
            (b"volume data: access denied", "complete"),
        ):
            with self.subTest(stderr=stderr):
                before = len(self.calls)
                frame = self.framed_mode({**self.spec, "mode": "inspect-original"}, stderr=stderr)
                self.assertEqual(frame["type"], expected)
                self.assertEqual(len(self.calls) - before, 1)
                if expected == "complete":
                    self.assertEqual(frame["status"], "observation_unavailable")

    def test_transport_preserves_protection_and_rejects_untyped_outcomes(self) -> None:
        from control_plane.dokploy import testing_native_maintenance as transport
        from control_plane.dokploy.native_copy_stream import NativeSourceUnavailable

        frame = {
            "type": "complete",
            "status": "protected",
            "reason": "native_copy_custody_changed",
            "host_boot_id": "fixture-boot",
            "host_monotonic": self.monotonic,
            "host_observed_at": self.now,
        }

        def receive(**kwargs: Any) -> None:
            kwargs["receive"](dict(frame))

        with patch.object(transport, "_read_remote", side_effect=receive):
            result = transport.native_maintenance_operation(
                host="fixture", token="fixture", server_id="fixture", payload={}
            )
            self.assertEqual(result, frame)
            frame["status"] = "unsafe_or_unknown_protocol"
            with self.assertRaises(NativeSourceUnavailable):
                transport.native_maintenance_operation(
                    host="fixture", token="fixture", server_id="fixture", payload={}
                )
