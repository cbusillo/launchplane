"""Original native copy qualification and crash-safe physical reclamation."""

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
            self.assertEqual(arguments[1:3], ("--timeout", "-1"))
            for descriptor in kwargs["pass_fds"]:
                os.fstat(descriptor)
            self.containers[arguments[3]]["State"].update(
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
        with self.assertRaisesRegex(ValueError, "physical backing"):
            self.run_mode("qualify-retirement")
        self.assertFalse(any(call[0] == "stop" for call in self.calls))

    def test_foreign_private_network_member_refuses_before_quiescing(self) -> None:
        self.network_members.add("foreign-consumer")
        with self.assertRaisesRegex(ValueError, "foreign consumer"):
            self.run_mode("qualify-retirement")
        self.assertFalse(any(call[0] == "stop" for call in self.calls))

    def test_restart_or_replacement_after_retirement_never_qualifies_for_removal(self) -> None:
        self.run_mode("qualify-retirement")
        self.containers["web"]["RestartCount"] += 1
        with self.assertRaisesRegex(ValueError, "restarted or changed"):
            self.run_mode("dispose-original")
        self.assertTrue(self.volumes)

    def test_same_name_volume_replacement_and_foreign_bind_consumer_refuse(self) -> None:
        self.run_mode("qualify-retirement")
        name = next(iter(self.volumes))
        original = deepcopy(self.volumes[name])
        self.volumes[name]["CreatedAt"] = "replacement"
        with self.assertRaisesRegex(ValueError, "replaced"):
            self.run_mode("dispose-original")
        self.volumes[name] = original
        self.containers["foreign"] = {
            "Id": "foreign",
            "Config": {},
            "Mounts": [{"Type": "bind", "Source": original["Mountpoint"]}],
        }
        with self.assertRaisesRegex(ValueError, "another consumer"):
            self.run_mode("dispose-original")

    def test_new_private_network_consumer_after_retirement_protects_the_copy(self) -> None:
        self.run_mode("qualify-retirement")
        self.network_members.add("new-foreign-consumer")
        with self.assertRaisesRegex(ValueError, "network changed or has another consumer"):
            self.run_mode("dispose-original")
        self.assertTrue(self.volumes)

    def test_private_network_requires_exact_original_compose_ownership(self) -> None:
        network = self.networks["original-private-network"]
        labels = dict(network["Labels"])
        network["Labels"] = {**labels, "com.docker.compose.project": "foreign-project"}
        with self.assertRaisesRegex(ValueError, "not owned"):
            self.run_mode("qualify-retirement")
        self.assertFalse(any(call[0] == "stop" for call in self.calls))

    def test_same_name_replacement_network_is_preserved(self) -> None:
        self.run_mode("qualify-retirement")
        original = self.networks.pop("original-private-network")
        self.networks["replacement-network"] = {**original, "Id": "replacement-network"}
        with self.assertRaisesRegex(ValueError, "network was replaced"):
            self.run_mode("dispose-original")
        self.assertTrue(self.containers)
        self.assertIn("replacement-network", self.networks)

    def test_nonlocal_network_is_not_given_deletion_authority(self) -> None:
        self.networks["original-private-network"].update(Driver="overlay", Scope="swarm")
        with self.assertRaisesRegex(ValueError, "not owned"):
            self.run_mode("qualify-retirement")
        self.assertFalse(any(call[0] == "stop" for call in self.calls))

    def test_lost_network_removal_reply_is_read_back_without_a_second_removal(self) -> None:
        self.run_mode("qualify-retirement")
        self.lose_after = ("network", "original-private-network")
        with self.assertRaises(TimeoutError):
            self.run_mode("dispose-original")
        self.assertEqual(self.run_mode("inspect-original")["status"], "disposed")
        self.assertEqual(self.run_mode("dispose-original")["status"], "disposed")
        self.assertEqual(self.calls.count(("network", "rm", "original-private-network")), 1)

    def test_uncertain_network_removal_preserves_original_dispatch_without_replay(self) -> None:
        self.run_mode("qualify-retirement")
        self.lose_after = ("network", "original-private-network")
        self.remove_before_loss = False
        with self.assertRaises(TimeoutError):
            self.run_mode("dispose-original")
        self.now += 121
        self.monotonic += 121
        self.spec.update(expires_at=self.now + 120, host_expires_at=self.monotonic + 120)
        self.assertEqual(self.run_mode("dispose-original")["status"], "unknown")
        self.assertEqual(self.calls.count(("network", "rm", "original-private-network")), 1)

    def test_partial_removal_lost_reply_resumes_only_original_resources_after_exclusion(
        self,
    ) -> None:
        self.run_mode("qualify-retirement")
        self.lose_after = ("container", "web")
        with self.assertRaises(TimeoutError):
            self.run_mode("dispose-original")
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
        with self.assertRaises(TimeoutError):
            self.run_mode("dispose-original")
        self.now += 121
        self.monotonic += 121
        self.spec["expires_at"] = self.now + 120
        self.spec["host_expires_at"] = self.monotonic + 120
        self.assertEqual(self.run_mode("dispose-original")["status"], "unknown")
        self.assertEqual(self.calls.count(("container", "rm", "web")), 1)

    def test_partial_volume_removal_uses_the_original_set_after_a_lost_reply(self) -> None:
        self.run_mode("qualify-retirement")
        name = next(iter(self.volumes))
        self.lose_after = ("volume", name)
        with self.assertRaises(TimeoutError):
            self.run_mode("dispose-original")
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
        with self.assertRaises(OSError):
            self.run_mode("qualify-retirement")
        self.assertFalse(any(call[0] == "stop" for call in self.calls))

    def test_host_wall_clock_rollback_cannot_extend_an_original_boot_deadline(self) -> None:
        self.monotonic += 121
        self.now -= 1000
        with self.assertRaises(TimeoutError):
            self.run_mode("qualify-retirement")
        self.assertFalse(any(call[0] == "stop" for call in self.calls))

    def test_expired_original_payload_and_busy_host_lock_cannot_dispatch(self) -> None:
        self.spec["expires_at"] = self.now - 1
        with self.assertRaises(TimeoutError):
            self.run_mode("qualify-retirement")
        self.spec["expires_at"] = self.now + 120
        executor = self.host["NativeMaintenanceHost"](
            self.spec, self.docker, receipt_root=self.state, lock_root=self.locks
        )
        executor.open()
        try:
            with self.assertRaises(BlockingIOError):
                self.run_mode("qualify-retirement")
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
