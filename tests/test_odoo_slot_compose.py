import json
import os
from pathlib import Path
import signal
import shlex
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from control_plane.dokploy.slot_compose import render_odoo_app_slot_compose, held_slot_command
from control_plane.dokploy.slot_maintenance import slot_run_stamp
from tests.test_odoo_cutover_website_settings import WebsiteProvider


class SlotComposeTests(unittest.TestCase):
    def test_web_and_scheduler_wait_for_the_same_committed_slot_before_startup(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = WebsiteProvider(root / "fixture")
            self.addCleanup(fixture.engine.dispose)
            plan = fixture.cutover
            marker = root / "run-ready"
            marker.write_text("uncommitted")
            startup = root / "startup.py"
            startup.write_text("import json,sys; print(json.dumps(sys.argv[1:]))")

            def launch_probe(_plan: object, command: str) -> str:
                return held_slot_command(plan, f"{sys.executable} {startup} {shlex.quote(command)}")

            with (
                patch(
                    "control_plane.dokploy.slot_compose.slot_run_marker", return_value=str(marker)
                ),
                patch(
                    "control_plane.dokploy.slot_compose.held_slot_command", side_effect=launch_probe
                ),
            ):
                compose = json.loads(
                    render_odoo_app_slot_compose(
                        plan, data_volume="fixture", private_network="private"
                    )
                )
            processes = []
            try:
                for service in ("web", "scheduler"):
                    definition = compose["services"][service]
                    command = definition["command"].replace("$$", "$")
                    process = subprocess.Popen(
                        [*definition["entrypoint"], command],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        start_new_session=True,
                    )
                    processes.append((service, process))
                    with self.assertRaises(subprocess.TimeoutExpired):
                        process.communicate(timeout=1.5)
                marker.write_text(slot_run_stamp(plan))
                launched = []
                for _, process in processes:
                    output, error = process.communicate(timeout=5)
                    self.assertEqual(process.returncode, 0, error)
                    launched.append(json.loads(output))
                self.assertNotEqual(*launched)
            finally:
                for _, process in processes:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                    process.communicate()
