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

from control_plane.odoo_testing_resources import (
    ODOO_TESTING_RESOURCE_PROGRAM,
    TESTING_RESOURCES_INCOMPLETE_MARKER,
    OdooTestingResources,
    resource_readback_passes,
)


class TestingResourceProgramTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.resources = OdooTestingResources(
            product="example-odoo",
            context="example",
            operation_id="retained-testing-operation",
            target_id="fresh-compose",
            namespace=f"lp-testing-{uuid4().hex}",
        )
        self.volumes: dict[str, dict[str, object]] = {}
        self.containers: dict[str, dict[str, object]] = {}
        self.calls: list[list[str]] = []

    def docker(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.assertEqual(command[0], "docker")
        arguments = command[1:]
        self.calls.append(arguments)
        if arguments[:2] == ["volume", "ls"]:
            result = "\n".join(self.volumes)
        elif arguments[:2] == ["volume", "inspect"]:
            result = json.dumps([self.volumes[arguments[-1]]])
        elif arguments[:2] == ["volume", "create"]:
            name = arguments[-1]
            path = self.root / name / "_data"
            path.mkdir(parents=True)
            labels = {}
            for index, argument in enumerate(arguments):
                if argument == "--label":
                    key, value = arguments[index + 1].split("=", 1)
                    labels[key] = value
            self.volumes[name] = {
                "Name": name,
                "Driver": "local",
                "Options": {},
                "Labels": labels,
                "Mountpoint": str(path),
            }
            result = name
        elif arguments == ["ps", "-aq"]:
            result = "\n".join(self.containers)
        elif arguments[0] == "inspect":
            result = json.dumps([self.containers[arguments[1]]])
        else:
            self.fail(f"Unexpected Docker operation: {arguments}")
        return subprocess.CompletedProcess(command, 0, stdout=result, stderr="")

    def run_program(
        self, mode: str = "prepare", *, project: str = ""
    ) -> tuple[int, dict[str, str]]:
        spec = {
            **self.resources.model_dump(mode="json"),
            "volumes": self.resources.volumes,
            "mode": mode,
            "compose_project": project or self.resources.namespace,
        }
        encoded = base64.b64encode(json.dumps(spec).encode()).decode()
        output = io.StringIO()
        status = 0
        with (
            patch.object(sys, "argv", ["resource-program", encoded]),
            patch("subprocess.run", side_effect=self.docker),
            redirect_stdout(output),
        ):
            try:
                exec(compile(ODOO_TESTING_RESOURCE_PROGRAM, "<testing-resources>", "exec"), {})
            except SystemExit as error:
                status = error.code if isinstance(error.code, int) else 1
        return status, dict(line.split("=", 1) for line in output.getvalue().splitlines())

    def prepare(self) -> None:
        status, evidence = self.run_program()
        self.assertEqual(status, 0)
        self.assertTrue(resource_readback_passes(evidence, self.resources))

    def test_prepares_only_new_resources_and_readback_replays_without_mutation(self) -> None:
        self.prepare()
        original = json.loads(json.dumps(self.volumes))
        self.calls.clear()
        status, evidence = self.run_program("readback")
        self.assertEqual(status, 0)
        self.assertTrue(resource_readback_passes(evidence, self.resources))
        self.assertEqual(self.volumes, original)
        self.assertFalse(any(call[:2] == ["volume", "create"] for call in self.calls))
        changed_target = self.resources.model_copy(update={"target_id": "another-compose"})
        self.assertFalse(resource_readback_passes(evidence, changed_target))

    def test_foreign_existing_volume_is_preserved_and_refused(self) -> None:
        self.prepare()
        name = next(iter(self.volumes))
        self.volumes[name]["Labels"] = {"owner": "somebody-else"}
        original = json.loads(json.dumps(self.volumes))
        status, evidence = self.run_program()
        self.assertNotEqual(status, 0)
        self.assertFalse(resource_readback_passes(evidence, self.resources))
        self.assertEqual(self.volumes, original)

    def test_external_backing_options_and_needed_data_refuse_without_changes(self) -> None:
        self.prepare()
        for name in self.volumes:
            with self.subTest(name=name):
                self.volumes[name]["Options"] = {"type": "nfs", "device": ":/business-data"}
                self.assertNotEqual(self.run_program("readback")[0], 0)
                self.volumes[name]["Options"] = {}
                copy = Path(str(self.volumes[name]["Mountpoint"])) / "needed-copy"
                copy.write_text("keep me")
                self.assertNotEqual(self.run_program()[0], 0)
                self.assertEqual(copy.read_text(), "keep me")
                copy.unlink()

    def test_stopped_container_and_parent_bind_mount_protect_the_resources(self) -> None:
        self.prepare()
        name = next(iter(self.volumes))
        self.containers["stopped-container"] = {
            "State": {"Running": False},
            "Mounts": [{"Name": name, "Source": self.volumes[name]["Mountpoint"]}],
        }
        self.assertNotEqual(self.run_program()[0], 0)
        self.containers["stopped-container"]["Mounts"] = [{"Source": str(self.root)}]
        self.assertNotEqual(self.run_program()[0], 0)
        self.containers["stopped-container"].pop("Mounts")
        self.assertNotEqual(self.run_program()[0], 0)

    def test_missing_readback_does_not_create_storage(self) -> None:
        status, evidence = self.run_program("readback")
        self.assertNotEqual(status, 0)
        self.assertFalse(resource_readback_passes(evidence, self.resources))
        self.assertFalse(self.volumes)

    def test_actual_provider_project_container_blocks_even_without_volume_mounts(self) -> None:
        self.prepare()
        project = self.resources.namespace + "-abc123"
        self.containers["new-project-container"] = {
            "Config": {"Labels": {"com.docker.compose.project": project}},
            "Mounts": [],
        }
        self.assertNotEqual(self.run_program("readback", project=project)[0], 0)

    def test_resolved_data_root_and_read_only_host_monitor_do_not_make_storage_shared(self) -> None:
        self.prepare()
        alias = self.root / "docker-data-root"
        alias.symlink_to(self.root, target_is_directory=True)
        for name, volume in self.volumes.items():
            volume["Mountpoint"] = str(alias / name / "_data")
        monitor_mount: dict[str, object] = {"Type": "bind", "RW": False, "Source": str(self.root)}
        self.containers["host-monitor"] = {"Mounts": [monitor_mount]}
        status, evidence = self.run_program("readback")
        self.assertEqual(status, 0)
        self.assertTrue(resource_readback_passes(evidence, self.resources))
        monitor_mount["RW"] = True
        self.assertNotEqual(self.run_program("readback")[0], 0)
        monitor_mount.update(RW=False, Source=next(iter(self.volumes.values()))["Mountpoint"])
        self.assertNotEqual(self.run_program("readback")[0], 0)

    def test_partial_empty_preparation_can_be_classified_but_foreign_data_cannot(self) -> None:
        self.prepare()
        missing_name = next(iter(self.volumes))
        del self.volumes[missing_name]
        # Keep the fake backing directory so a later retry can recreate metadata.
        (self.root / missing_name / "_data").rmdir()
        (self.root / missing_name).rmdir()
        status, evidence = self.run_program("readback")
        self.assertNotEqual(status, 0)
        self.assertEqual(evidence.get(TESTING_RESOURCES_INCOMPLETE_MARKER), "true")
        remaining = next(iter(self.volumes.values()))
        kept = Path(str(remaining["Mountpoint"])) / "needed-copy"
        kept.write_text("keep me")
        status, evidence = self.run_program("readback")
        self.assertNotEqual(status, 0)
        self.assertNotIn(TESTING_RESOURCES_INCOMPLETE_MARKER, evidence)
        self.assertEqual(kept.read_text(), "keep me")


if __name__ == "__main__":
    unittest.main()
