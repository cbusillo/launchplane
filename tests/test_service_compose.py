import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest

from control_plane.cli import ARTIFACT_IMAGE_REFERENCE_ENV_KEY


ROOT = Path(__file__).resolve().parents[1]


class LaunchplaneServiceComposeTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("docker"), "Docker CLI is required to render Compose")
    def test_services_use_the_supplied_image_and_credentialed_services_receive_it(self) -> None:
        image_reference = "example.invalid/fixture/runtime@sha256:" + "a" * 64
        with TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copyfile(ROOT / "docker-compose.yml", root / "docker-compose.yml")
            (root / ".env").write_text("", encoding="utf-8")
            result = subprocess.run(
                [
                    "docker",
                    "compose",
                    "--env-file",
                    str(root / ".env"),
                    "config",
                    "--no-env-resolution",
                    "--format",
                    "json",
                ],
                cwd=root,
                env={
                    "PATH": os.environ["PATH"],
                    ARTIFACT_IMAGE_REFERENCE_ENV_KEY: image_reference,
                    "LAUNCHPLANE_COMPOSE_EXTERNAL_NETWORK": "fixture-network",
                },
                capture_output=True,
                text=True,
                timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        services = json.loads(result.stdout)["services"]
        self.assertTrue(services)
        for name, service in services.items():
            with self.subTest(service=name):
                self.assertEqual(service["image"], image_reference)
                if service.get("env_file"):
                    self.assertEqual(
                        service["environment"].get(ARTIFACT_IMAGE_REFERENCE_ENV_KEY),
                        image_reference,
                    )
