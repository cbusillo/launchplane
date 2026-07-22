from __future__ import annotations

import unittest
from pathlib import Path


class ServiceImageContractTests(unittest.TestCase):
    def test_service_image_pins_fixed_github_cli_grpc_dependency(self) -> None:
        dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile"

        dockerfile_text = dockerfile.read_text(encoding="utf-8")

        self.assertIn("GITHUB_CLI_VERSION=v2.96.0", dockerfile_text)
        self.assertIn("GITHUB_CLI_GRPC_VERSION=v1.82.1", dockerfile_text)
        self.assertIn(
            'go get "github.com/cli/cli/v2/cmd/gh@${GITHUB_CLI_VERSION}"',
            dockerfile_text,
        )
        self.assertIn(
            'go get "google.golang.org/grpc@${GITHUB_CLI_GRPC_VERSION}"',
            dockerfile_text,
        )
        self.assertIn("go version -m /go/bin/gh", dockerfile_text)
        self.assertNotIn("go install github.com/cli/cli/v2/cmd/gh@v2.95.0", dockerfile_text)

    def test_service_image_installs_ssh_client_for_rollback_worker(self) -> None:
        dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile"

        dockerfile_text = dockerfile.read_text(encoding="utf-8")

        self.assertIn("openssh-client", dockerfile_text)
        self.assertIn("rm -rf /var/lib/apt/lists/*", dockerfile_text)


if __name__ == "__main__":
    unittest.main()
