from __future__ import annotations

import unittest
from pathlib import Path


class ServiceImageContractTests(unittest.TestCase):
    def test_service_image_installs_ssh_client_for_rollback_worker(self) -> None:
        dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile"

        dockerfile_text = dockerfile.read_text(encoding="utf-8")

        self.assertIn("openssh-client", dockerfile_text)
        self.assertIn("rm -rf /var/lib/apt/lists/*", dockerfile_text)


if __name__ == "__main__":
    unittest.main()
