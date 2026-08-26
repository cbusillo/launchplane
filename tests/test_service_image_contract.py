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
GITHUB_CLI_INTERNAL_MODULE_CONTRACTS = {
    "golang.org/x/mod": ("golang.org/x/mod", "github_cli_x_mod_version"),
}
RUNTIME_SECURITY_UPGRADE_PACKAGES = {
    "bsdutils",
    "libblkid1",
    "liblastlog2-2",
    "libmount1",
    "libsmartcols1",
    "libuuid1",
    "login",
    "mount",
    "util-linux",
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
    r'(?P<module>[^"\s]+)"\s+\|\s+grep\s+-F\s+"\$\{(?P<argument>[A-Za-z_][A-Za-z0-9_]*)\}"'
)
SHELL_VERSION_PATTERN = re.compile(r"\b(?P<name>[a-z][a-z0-9_]*)=(?P<value>v\S+)\s+\\")


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

        shell_versions = {
            match.group("name"): match.group("value")
            for match in SHELL_VERSION_PATTERN.finditer(build_stage_text)
        }
        expected_shell_version_names = {
            version_name for _, version_name in GITHUB_CLI_INTERNAL_MODULE_CONTRACTS.values()
        }

        self.assertTrue(expected_shell_version_names.issubset(shell_versions))
        for version_name in expected_shell_version_names:
            self.assertRegex(shell_versions[version_name], GO_VERSION_PATTERN)

        go_gets = {
            match.group("module"): match.group("reference")
            for match in GO_GET_PATTERN.finditer(build_stage_text)
        }
        expected_go_gets = {
            package_path: f"${{{argument_name}}}"
            for package_path, (_, argument_name) in GITHUB_CLI_MODULE_CONTRACTS.items()
        }
        expected_go_gets.update(
            {
                package_path: f"${{{version_name}}}"
                for package_path, (_, version_name) in GITHUB_CLI_INTERNAL_MODULE_CONTRACTS.items()
            }
        )

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
        expected_provenance.update(GITHUB_CLI_INTERNAL_MODULE_CONTRACTS.values())

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

    def test_service_image_refreshes_runtime_security_packages(self) -> None:
        dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile"

        dockerfile_text = dockerfile.read_text(encoding="utf-8")
        runtime_stage = re.search(
            r"(?ms)^FROM\s+\S*python:3\.13-slim\s+AS\s+runtime\s*$"
            r"(?P<body>.*?)(?=^FROM\s|\Z)",
            dockerfile_text,
        )

        self.assertIsNotNone(runtime_stage)
        assert runtime_stage is not None
        runtime_stage_text = runtime_stage.group("body")
        upgrade = re.search(
            r"(?ms)apt-get install -y --no-install-recommends --only-upgrade\s+\\\n"
            r"(?P<packages>.*?)\n\s*&& apt-get install -y --no-install-recommends",
            runtime_stage_text,
        )

        self.assertIsNotNone(upgrade)
        assert upgrade is not None
        observed_packages = set(
            re.findall(
                r"(?m)^\s+(?P<package>[a-z0-9][a-z0-9+.-]*)\s+\\?\s*$",
                upgrade.group("packages"),
            )
        )

        self.assertTrue(RUNTIME_SECURITY_UPGRADE_PACKAGES.issubset(observed_packages))


if __name__ == "__main__":
    unittest.main()
