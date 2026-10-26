import base64
from copy import deepcopy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from control_plane.application_state_volume import state_volume_allocation_names
from control_plane.application_testing_storage import APPLICATION_STORAGE_INSPECTION_PROGRAM
from control_plane.contracts.canonical_json import canonical_json_sha256


class ApplicationTestingStorageProgramTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "volumes").mkdir()
        plan = uuid4().hex
        names = state_volume_allocation_names(plan)
        self.spec = {
            "plan_id": plan,
            "volume": names["volume_name"],
            "application": names["app_name"],
            "binding": "a" * 64,
            "application_id": "fixture-application",
            "server": "fixture-server",
        }
        self.volumes: list[str] = []
        self.containers: dict[str, object] = {}
        self.change_inventory = False
        self.list_calls = 0
        self.volume_rows: dict[str, object] = {}
        self.services: dict[str, object] = {}
        self.images: dict[str, object] = {}

    def docker(self, command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        arguments = command[1:]
        if arguments[:2] == ["volume", "ls"]:
            output = "\n".join(self.volumes)
        elif arguments[:2] == ["volume", "inspect"]:
            if arguments[-1] not in self.volume_rows:
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="not found")
            output = json.dumps([self.volume_rows[arguments[-1]]])
        elif arguments[:2] == ["service", "ls"]:
            output = "\n".join(self.services)
        elif arguments[:2] == ["service", "inspect"]:
            output = json.dumps([self.services[arguments[-1]]])
        elif arguments[:2] == ["image", "inspect"]:
            output = json.dumps([self.images[arguments[-1]]])
        elif arguments[:2] == ["node", "ls"]:
            output = json.dumps({"ID": "local-node", "Self": True})
        elif arguments[0] == "info":
            output = json.dumps(str(self.root))
        elif arguments[0] == "ps":
            options = set(arguments[1:])
            self.assertTrue(options & {"-aq", "-a", "--all"})
            self.assertTrue(options & {"-aq", "-q", "--quiet"})
            self.list_calls += 1
            output = "\n".join(self.containers)
            if self.change_inventory and self.list_calls > 1:
                output += "\nchanged-container"
        elif arguments[0] == "inspect":
            output = json.dumps(self.containers[arguments[-1]])
        else:
            self.fail(f"Unexpected Docker operation: {arguments}")
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    def run_program(self) -> dict[str, str]:
        encoded = base64.b64encode(json.dumps(self.spec).encode()).decode()
        output = io.StringIO()
        with (
            patch.object(sys, "argv", ["inspection", encoded]),
            patch("subprocess.run", side_effect=self.docker),
            redirect_stdout(output),
        ):
            try:
                exec(
                    compile(APPLICATION_STORAGE_INSPECTION_PROGRAM, "<host-inspection>", "exec"), {}
                )
            except SystemExit as error:
                self.assertNotEqual(error.code, 0)
        return dict(line.split("=", 1) for line in output.getvalue().splitlines())

    def test_absent_allocation_qualifies_without_creating_any_resource(self) -> None:
        evidence = self.run_program()
        self.assertEqual(evidence["testing_resources_ok"], "true")
        self.assertEqual(
            evidence["testing_resources_binding_sha256"], canonical_json_sha256(self.spec)
        )
        self.assertFalse((self.root / "volumes" / self.spec["volume"]).exists())

    def prepare_serving(self) -> None:
        self.spec.update(
            image="registry.example.invalid/site@sha256:" + "a" * 64,
            deployment="serving-deployment",
            mount_path="/var/lib/example",
        )
        backing = self.root / "volumes" / self.spec["volume"] / "_data"
        backing.mkdir(parents=True)
        (backing / "preserved-data").write_text("testing data")
        self.volumes = [self.spec["volume"]]
        self.volume_rows[self.spec["volume"]] = {
            "Name": self.spec["volume"],
            "Driver": "local",
            "Options": None,
            "Mountpoint": str(backing),
        }
        self.services[self.spec["application"]] = {
            "ID": "serving-service",
            "Spec": {
                "Name": self.spec["application"],
                "Mode": {"Replicated": {"Replicas": 1}},
                "TaskTemplate": {
                    "ContainerSpec": {
                        "Image": self.spec["image"],
                        "Env": ["LAUNCHPLANE_DEPLOYMENT_RECORD_ID=" + self.spec["deployment"]],
                        "Mounts": [
                            {
                                "Type": "volume",
                                "Source": self.spec["volume"],
                                "Target": self.spec["mount_path"],
                            }
                        ],
                    }
                },
            },
            "UpdateStatus": {"State": "completed"},
        }
        identifier = "a" * 64
        self.containers[identifier] = {
            "id": identifier,
            "name": "/" + self.spec["application"] + ".1",
            "labels": {
                "com.docker.swarm.service.id": "serving-service",
                "com.docker.swarm.service.name": self.spec["application"],
            },
            "mounts": [
                {
                    "Type": "volume",
                    "Name": self.spec["volume"],
                    "Destination": self.spec["mount_path"],
                    "Source": str(backing),
                    "RW": True,
                }
            ],
            "state": "running",
            "env": ["LAUNCHPLANE_DEPLOYMENT_RECORD_ID=" + self.spec["deployment"]],
            "image": "fixture-image",
            "ports": None,
            "privileged": False,
        }
        self.images["fixture-image"] = {"RepoDigests": [self.spec["image"]]}

    def test_serving_local_volume_qualifies_and_preserves_its_contents(self) -> None:
        self.prepare_serving()
        result = self.run_program()
        self.assertEqual(result["testing_resources_ok"], "true")
        self.assertEqual(
            result["testing_resources_binding_sha256"], canonical_json_sha256(self.spec)
        )
        self.assertEqual(
            (self.root / "volumes" / self.spec["volume"] / "_data" / "preserved-data").read_text(),
            "testing data",
        )

    def test_serving_foreign_stopped_consumer_and_future_service_mount_remain_fenced(self) -> None:
        self.prepare_serving()
        self.containers["b" * 64] = {
            "id": "b" * 64,
            "name": "/foreign-stopped",
            "labels": {},
            "mounts": [{"Type": "volume", "Name": self.spec["volume"]}],
        }
        self.assertEqual(self.run_program()["testing_resources_ok"], "false")
        del self.containers["b" * 64]
        other = deepcopy(self.services[self.spec["application"]])
        assert isinstance(other, dict)
        other["ID"] = "foreign-service"
        self.services["foreign-service"] = other
        self.assertEqual(self.run_program()["testing_resources_ok"], "false")

    def test_serving_remote_storage_wrong_runtime_or_missing_inventory_remain_fenced(self) -> None:
        self.prepare_serving()
        original = deepcopy(self.volume_rows[self.spec["volume"]])
        assert isinstance(original, dict)
        for changes in (
            {"Driver": "nfs"},
            {"Options": {"device": "/production"}},
            {"Mountpoint": "/production"},
        ):
            with self.subTest(changes=changes):
                self.volume_rows[self.spec["volume"]] = {**original, **changes}
                self.assertEqual(self.run_program()["testing_resources_ok"], "false")
        self.volume_rows[self.spec["volume"]] = original
        self.images["fixture-image"] = {"RepoDigests": ["different-image"]}
        self.assertEqual(self.run_program()["testing_resources_ok"], "false")
        self.images["fixture-image"] = {"RepoDigests": [self.spec["image"]]}
        self.change_inventory = True
        self.assertEqual(self.run_program()["testing_resources_ok"], "false")

    def test_existing_volume_and_unregistered_backing_remain_held(self) -> None:
        self.volumes = [self.spec["volume"]]
        self.assertEqual(self.run_program()["testing_resources_ok"], "false")
        self.volumes = []
        (self.root / "volumes" / self.spec["volume"]).mkdir()
        self.assertEqual(self.run_program()["testing_resources_ok"], "false")

    def test_stopped_consumers_parent_binds_and_owned_containers_refuse(self) -> None:
        identifier = "b" * 64
        for changed in (
            {"mounts": [{"Type": "volume", "Name": self.spec["volume"]}]},
            {"mounts": [{"Type": "bind", "Source": str(self.root), "RW": True}]},
            {"labels": {"com.docker.swarm.service.name": self.spec["application"]}},
            {"mounts": None},
        ):
            with self.subTest(changed=changed):
                self.containers = {
                    identifier: {
                        "id": identifier,
                        "name": "/unrelated-stopped-container",
                        "labels": {},
                        "mounts": [],
                        **changed,
                    }
                }
                self.assertEqual(self.run_program()["testing_resources_ok"], "false")

    def test_read_only_administration_and_unrelated_deleted_bind_do_not_hold_fresh_storage(
        self,
    ) -> None:
        identifier = "c" * 64
        for mount in (
            {"Type": "bind", "Source": str(self.root), "RW": False},
            {"Type": "bind", "Source": str(self.root / "deleted-unrelated-bind"), "RW": True},
        ):
            with self.subTest(mount=mount):
                self.containers = {
                    identifier: {
                        "id": identifier,
                        "name": "/unrelated-stopped-container",
                        "labels": {},
                        "mounts": [mount],
                    }
                }
                self.assertEqual(self.run_program()["testing_resources_ok"], "true")

    def test_read_only_exact_or_child_backing_remains_a_protected_consumer(self) -> None:
        identifier = "d" * 64
        allocation = self.root / "volumes" / self.spec["volume"]
        for source in (allocation, allocation / "_data"):
            with self.subTest(source=source):
                self.containers = {
                    identifier: {
                        "id": identifier,
                        "name": "/stopped-consumer",
                        "labels": {},
                        "mounts": [{"Type": "bind", "Source": str(source), "RW": False}],
                    }
                }
                self.assertEqual(self.run_program()["testing_resources_ok"], "false")

    def test_changed_or_ambiguous_inventory_never_proves_absence(self) -> None:
        self.volumes = ["duplicate", "duplicate"]
        self.assertEqual(self.run_program()["testing_resources_ok"], "false")
        self.volumes = []
        self.change_inventory = True
        self.assertEqual(self.run_program()["testing_resources_ok"], "false")
