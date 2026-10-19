import base64
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

    def docker(self, command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        arguments = command[1:]
        if arguments[:2] == ["volume", "ls"]:
            output = "\n".join(self.volumes)
        elif arguments[0] == "info":
            output = json.dumps(str(self.root))
        elif arguments[:2] == ["ps", "-aq"]:
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
            {"mounts": [{"Type": "bind", "Source": str(self.root), "RW": False}]},
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

    def test_changed_or_ambiguous_inventory_never_proves_absence(self) -> None:
        self.volumes = ["duplicate", "duplicate"]
        self.assertEqual(self.run_program()["testing_resources_ok"], "false")
        self.volumes = []
        self.change_inventory = True
        self.assertEqual(self.run_program()["testing_resources_ok"], "false")
