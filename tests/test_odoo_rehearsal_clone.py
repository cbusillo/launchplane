import ast
import os
from pathlib import Path
import subprocess
import unittest

from control_plane._odoo_upgrade_rehearsal_program import ODOO_UPGRADE_REHEARSAL_PROGRAM
from tests.test_odoo_slot_startup_rehearsal import require_local_docker_endpoint


@unittest.skipUnless(
    os.environ.get("LAUNCHPLANE_TEST_STARTUP_IMAGE"), "Select a local immutable image"
)
class RehearsalCloneTests(unittest.TestCase):
    def test_clone_preserves_runner_owned_files_under_root_owned_parent(self) -> None:
        require_local_docker_endpoint()
        image = os.environ["LAUNCHPLANE_TEST_STARTUP_IMAGE"]
        self.assertRegex(image, r"@sha256:[a-f0-9]{64}$")
        subprocess.run(["docker", "image", "inspect", image], capture_output=True, check=True)
        module = ast.parse(ODOO_UPGRADE_REHEARSAL_PROGRAM)
        program = "\n\n".join(
            ast.get_source_segment(ODOO_UPGRADE_REHEARSAL_PROGRAM, node) or ""
            for node in module.body
            if isinstance(node, ast.FunctionDef)
            and node.name in {"clone", "restore_preserved_snapshot", "prepare_runner_storage"}
        )
        fixture = Path(__file__).parent / "fixtures/rehearsal_clone.py"
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--pull",
                "never",
                "--network",
                "none",
                "--read-only",
                "--tmpfs",
                "/volumes:rw,size=1m",
                "--user",
                "0:0",
                "--mount",
                f"type=bind,src={fixture},dst=/fixture.py,readonly",
                "--entrypoint",
                "python3",
                "-i",
                image,
                "/fixture.py",
            ],
            input=program,
            text=True,
            capture_output=True,
            check=False,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
