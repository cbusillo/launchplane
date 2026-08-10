from __future__ import annotations

import re
import unittest
from pathlib import Path


GITHUB_CLI_MODULE_CONTRACTS = {
    "github.com/cli/cli/v2/cmd/gh": (
        "github.com/cli/cli/v2",
        "GITHUB_CLI_VERSION",
    ),
    "google.golang.org/grpc": (
        "google.golang.org/grpc",
        "GITHUB_CLI_GRPC_VERSION",
    ),
    "golang.org/x/text": (
        "golang.org/x/text",
        "GITHUB_CLI_X_TEXT_VERSION",
    ),
}
GO_VERSION_PATTERN = re.compile(
    r"^v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
ARG_PATTERN = re.compile(r"^ARG\s+(?P<name>[A-Z0-9_]+)=(?P<value>\S+)\s*$", re.MULTILINE)
GO_GET_PATTERN = re.compile(r'\bgo\s+get\s+"(?P<module>[^"@\s]+)@(?P<reference>[^"\s]+)"')
GO_PROVENANCE_PATTERN = re.compile(
    r'\bgo\s+version\s+-m\s+/go/bin/gh\s+\|\s+grep\s+-F\s+"'
    r'(?P<module>[^"\s]+)"\s+\|\s+grep\s+-F\s+"\$\{(?P<argument>[A-Z0-9_]+)\}"'
)


class ServiceImageContractTests(unittest.TestCase):
    def test_service_image_relates_github_cli_dependencies(self) -> None:
        dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile"

        dockerfile_text = dockerfile.read_text(encoding="utf-8")
        build_stage = re.search(
            r"(?ms)^(?P<header>FROM\s+\S*golang:\S+\s+AS\s+github-cli-build\s*)$"
            r"(?P<body>.*?)(?=^FROM\s|\Z)",
            dockerfile_text,
        )

        self.assertIsNotNone(build_stage)
        assert build_stage is not None
        build_stage_text = build_stage.group("body")

        arguments = {
            match.group("name"): match.group("value")
            for match in ARG_PATTERN.finditer(build_stage_text)
        }
        expected_argument_names = {
            argument_name for _, argument_name in GITHUB_CLI_MODULE_CONTRACTS.values()
        }

        self.assertTrue(expected_argument_names.issubset(arguments))
        for argument_name in expected_argument_names:
            self.assertRegex(arguments[argument_name], GO_VERSION_PATTERN)

        go_gets = {
            match.group("module"): match.group("reference")
            for match in GO_GET_PATTERN.finditer(build_stage_text)
        }
        expected_go_gets = {
            package_path: f"${{{argument_name}}}"
            for package_path, (_, argument_name) in GITHUB_CLI_MODULE_CONTRACTS.items()
        }

        self.assertEqual(go_gets, expected_go_gets)
        self.assertNotRegex(build_stage_text, r"\bgo\s+install\b")
        self.assertNotRegex(build_stage_text, r"\bgo\s+(?:get|install)\s+[^\n]*@v\d")

        self.assertRegex(
            build_stage_text,
            r"\bgo\s+build\s+-trimpath\s+-o\s+/go/bin/gh\s+"
            r"github\.com/cli/cli/v2/cmd/gh\b",
        )
        provenance = {
            (match.group("module"), match.group("argument"))
            for match in GO_PROVENANCE_PATTERN.finditer(build_stage_text)
        }
        expected_provenance = {
            (module_path, argument_name)
            for _, (module_path, argument_name) in GITHUB_CLI_MODULE_CONTRACTS.items()
        }

        self.assertEqual(provenance, expected_provenance)

        self.assertRegex(
            dockerfile_text,
            r"(?m)^COPY\s+--from=github-cli-build\s+/go/bin/gh\s+/usr/local/bin/gh\s*$",
        )

    def test_service_image_installs_ssh_client_for_rollback_worker(self) -> None:
        dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile"

        dockerfile_text = dockerfile.read_text(encoding="utf-8")

        self.assertIn("openssh-client", dockerfile_text)
        self.assertIn("rm -rf /var/lib/apt/lists/*", dockerfile_text)


if __name__ == "__main__":
    unittest.main()
