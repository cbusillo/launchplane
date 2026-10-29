import base64
import json
import os
import subprocess
import sys
import time
import unittest
from typing import Any, cast

from control_plane.production_recipient_retirement_host import RETIREMENT_RESOURCE_PROGRAM
from tests import test_retained_volume_sweep_program as sweep_tests


class RetirementHostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = sweep_tests.RetainedVolumeSweepProgramTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.project = "old-project"
        self.fixture.state["volume_labels"] = {"com.docker.compose.project": self.project}
        self.fixture.write_state()

    def resources(self) -> dict[str, Any]:
        spec = {
            "project": self.project,
            "target": "old-target",
            "application_service": "web",
            "volumes": ["candidate"],
        }
        response = subprocess.run(
            [
                sys.executable,
                "-c",
                RETIREMENT_RESOURCE_PROGRAM,
                base64.b64encode(json.dumps(spec).encode()).decode(),
                "FRAME:",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
            env=os.environ
            | {
                "PATH": str(self.fixture.bin) + os.pathsep + os.environ["PATH"],
                "FAKE_DOCKER_STATE": str(self.fixture.state_path),
            },
        )
        return cast(dict[str, Any], json.loads(response.stdout.removeprefix("FRAME:"))["result"])

    def test_actual_storage_identity_and_foreign_owner_are_distinct(self) -> None:
        observed = self.resources()
        self.assertEqual(observed["volumes"][0]["identity"], self.fixture.identity())
        self.assertFalse(observed["volumes"][0]["held"])
        self.fixture.state["volume_labels"] = {"com.docker.compose.project": "foreign"}
        self.fixture.write_state()
        self.assertTrue(self.resources()["volumes"][0]["held"])

    def test_stopped_testing_container_preserves_shared_storage(self) -> None:
        self.fixture.state.update(containers=["stopped-testing"], container_ref=True)
        self.fixture.write_state()
        observed = self.resources()
        self.assertTrue(observed["project_absent"])
        self.assertTrue(observed["volumes"][0]["held"])

    def test_old_writer_and_shared_project_network_refuse(self) -> None:
        self.fixture.state.update(
            containers=["old-web"],
            container_labels={
                "com.docker.compose.project": self.project,
                "com.docker.compose.service": "web",
            },
            running=True,
        )
        self.fixture.write_state()
        self.assertTrue(self.resources()["unknown"])
        self.fixture.state.update(container_labels={}, running=False, networks={self.project: {}})
        self.fixture.write_state()
        self.assertTrue(self.resources()["unknown"])

    def test_incomplete_docker_inventory_and_aliases_never_qualify(self) -> None:
        self.fixture.state.update(containers=["incomplete"], malformed_containers=True)
        self.fixture.write_state()
        self.assertTrue(self.resources()["unknown"])
        self.fixture.state.update(containers=[], malformed_containers=False, alias=True)
        self.fixture.write_state()
        self.assertTrue(self.resources()["unknown"])

    def test_populated_removal_requires_fresh_retirement_and_fences_late_dispatch(self) -> None:
        (self.fixture.data / "business-data").write_text("synthetic retained state")
        self.assertEqual(self.fixture.run_program("apply")["status"], "held")
        permit = {"binding": "a" * 64, "fresh_until": time.time() + 60}
        self.assertEqual(
            self.fixture.run_program(
                "apply", retirement={**permit, "fresh_until": time.time() - 1}
            )["status"],
            "held",
        )
        self.assertEqual(
            self.fixture.run_program("readback", retirement=permit)["reason_code"], "never_started"
        )
        self.assertEqual(
            self.fixture.run_program("apply", retirement=permit)["reason_code"], "never_started"
        )
        self.assertEqual(
            self.fixture.run_program("apply", operation="qualified-retirement", retirement=permit)[
                "status"
            ],
            "removed",
        )
