import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from control_plane.dokploy.cutover_host_state import CutoverHostState
from control_plane.dokploy.service_cutover import installed_cutover_host
from control_plane.dokploy.cutover_host import DokployCutoverHost
from tests.support.prepared_public_fixture import WebsiteFixture
from tests.test_odoo_cutover import release_plan


class CutoverHostStateTests(unittest.TestCase):
    def test_installed_host_discovery_constructs_without_credentials_or_provider_effects(
        self,
    ) -> None:
        with patch("control_plane.dokploy.cutover_host.read_dokploy_config") as config:
            self.assertIsInstance(installed_cutover_host(), DokployCutoverHost)
        config.assert_not_called()

    def test_original_execution_survives_restart_and_refuses_new_payload_or_placement(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            fixture = WebsiteFixture(root)
            self.addCleanup(fixture.engine.dispose)
            plan = release_plan(fixture)
            directory = root / "authority"
            directory.mkdir()

            def execute(argv: tuple[str, ...], stdin: str | None) -> str:
                return subprocess.run(
                    [sys.executable, *argv[1:]],
                    input=stdin,
                    text=True,
                    capture_output=True,
                    check=True,
                ).stdout

            effects = Mock(side_effect=execute)
            state = CutoverHostState(plan, str(directory), execute, effects, {"server": "isolated"})
            state.admit_execution(recovery=False)
            original = state.expected_execution()
            restarted = CutoverHostState(
                plan, str(directory), execute, effects, {"server": "isolated"}
            )
            restarted.verify_execution(require_existing=True)
            before = effects.call_count
            with (
                patch(
                    "control_plane.dokploy.cutover_host_state.maintenance_payload",
                    return_value={"package": "changed"},
                ),
                self.assertRaises(ValueError),
            ):
                restarted.admit_execution(recovery=True)
            self.assertEqual(effects.call_count, before)
            moved = CutoverHostState(plan, str(directory), execute, effects, {"server": "other"})
            with self.assertRaises(ValueError):
                moved.admit_execution(recovery=True)
            self.assertEqual(
                json.loads((directory / (state.record_id + "-execution.json")).read_text()),
                original,
            )
