from copy import deepcopy
from collections.abc import Callable
import json
from pathlib import Path
import subprocess
from typing import Any
from unittest import TestCase
from unittest.mock import patch

from control_plane.artifact_scan import artifact_scan_from_report, scan_artifact

IMAGE = "ghcr.io/example/site@sha256:" + "a" * 64
NOW = "2026-10-16T12:00:00Z"


def report() -> dict[str, Any]:
    return {
        "SchemaVersion": 2,
        "ArtifactName": IMAGE,
        "ArtifactType": "container_image",
        "Results": [{"Class": "os-pkgs", "Packages": [{"Name": "openssl"}]}],
    }


class ArtifactScanTests(TestCase):
    def test_clean_and_fixable_high_critical_gate(self) -> None:
        clean = artifact_scan_from_report(IMAGE, report(), scanned_at=NOW)
        self.assertEqual(clean.status, "pass")
        vulnerable = report()
        vulnerable["Results"][0]["Vulnerabilities"] = [
            {"Severity": "HIGH", "FixedVersion": "2"},
            {"Severity": "CRITICAL", "FixedVersion": "2"},
            {"Severity": "CRITICAL"},
        ]
        result = artifact_scan_from_report(IMAGE, vulnerable, scanned_at=NOW)
        self.assertEqual((result.status, result.high, result.critical), ("fail", 1, 1))

    def test_missing_inventory_wrong_image_and_bad_findings_are_not_clean(self) -> None:
        mutations: tuple[Callable[[dict[str, Any]], object], ...] = (
            lambda item: item.update(Results=[]),
            lambda item: item.update(ArtifactName="ghcr.io/example/other@sha256:" + "b" * 64),
            lambda item: item["Results"][0].update(Packages=[]),
            lambda item: item["Results"][0].update(Vulnerabilities="missing"),
            lambda item: item["Results"][0].update(Vulnerabilities=[{"Severity": "typo"}]),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                payload = deepcopy(report())
                mutate(payload)
                with self.assertRaises(ValueError):
                    artifact_scan_from_report(IMAGE, payload, scanned_at=NOW)

    def test_subprocess_scans_digest_without_host_or_product_policy(self) -> None:
        def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            self.assertEqual(argv[-1], IMAGE)
            self.assertEqual(argv[argv.index("--image-src") + 1], "remote")
            self.assertNotIn("TRIVY_TOKEN", kwargs["env"])
            self.assertNotIn("GITHUB_TOKEN", kwargs["env"])
            self.assertEqual(
                Path(kwargs["cwd"]) / "trivy.yaml", Path(argv[argv.index("--config") + 1])
            )
            Path(argv[argv.index("--output") + 1]).write_text(json.dumps(report()))
            return subprocess.CompletedProcess(argv, 0)

        with patch.dict(
            "os.environ", {"TRIVY_TOKEN": "fixture-only", "GITHUB_TOKEN": "fixture-only"}
        ):
            with patch("control_plane.artifact_scan.subprocess.run", side_effect=run):
                self.assertEqual(scan_artifact(IMAGE).status, "pass")

    def test_missing_binary_timeout_and_error_are_unavailable(self) -> None:
        for error in (FileNotFoundError(), subprocess.TimeoutExpired("trivy", 1)):
            with patch("control_plane.artifact_scan.subprocess.run", side_effect=error):
                self.assertEqual(scan_artifact(IMAGE).status, "unavailable")
        with patch(
            "control_plane.artifact_scan.subprocess.run",
            return_value=subprocess.CompletedProcess([], 1),
        ):
            self.assertEqual(scan_artifact(IMAGE).status, "unavailable")
        with patch("control_plane.artifact_scan.subprocess.run") as run:
            self.assertEqual(scan_artifact("ghcr.io/example/site:latest").status, "unavailable")
            run.assert_not_called()
