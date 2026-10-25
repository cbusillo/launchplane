import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from control_plane.dokploy.slot_compose import render_odoo_app_slot_compose
from control_plane.dokploy.slot_maintenance import maintenance_payload, payload_digest
from tests.test_odoo_cutover_website_settings import WebsiteProvider


class SlotComposeTests(unittest.TestCase):
    def test_web_and_scheduler_wait_for_the_same_committed_slot_before_startup(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = WebsiteProvider(root / "fixture")
            self.addCleanup(fixture.engine.dispose)
            plan = fixture.cutover
            compose = json.loads(
                render_odoo_app_slot_compose(plan, data_volume="fixture", private_network="private")
            )
            marker = (
                root
                / ".launchplane-cutovers"
                / payload_digest(maintenance_payload(plan))
                / "web-ready"
            )
            marker.parent.mkdir(parents=True)
            marker.write_text("uncommitted")
            startup = root / "startup.py"
            startup.write_text("import json,sys; print(json.dumps(sys.argv[1:]))")
            processes = []
            try:
                for service in ("web", "scheduler"):
                    definition = compose["services"][service]
                    command = (
                        definition["command"]
                        .replace("$$", "$")
                        .replace("/volumes/data", str(root))
                        .replace(
                            "/venv/bin/python /volumes/scripts/run_odoo_startup.py",
                            f"{sys.executable} {startup}",
                        )
                    )
                    process = subprocess.Popen(
                        [*definition["entrypoint"], command],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        start_new_session=True,
                    )
                    processes.append((service, process))
                    with self.assertRaises(subprocess.TimeoutExpired):
                        process.communicate(timeout=0.1)
                token = hashlib.sha256(plan.new.model_dump_json().encode()).hexdigest()
                marker.write_text(token)
                for service, process in processes:
                    output, error = process.communicate(timeout=5)
                    self.assertEqual(process.returncode, 0, error)
                    arguments = json.loads(output)
                    if service == "scheduler":
                        self.assertIn("--scheduler", arguments)
                        self.assertEqual(
                            arguments[arguments.index("--ready-file") + 1], str(marker)
                        )
                        self.assertEqual(arguments[arguments.index("--ready-token") + 1], token)
                    else:
                        self.assertNotIn("--scheduler", arguments)
            finally:
                for _, process in processes:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                    process.communicate()
