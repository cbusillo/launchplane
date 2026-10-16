from copy import deepcopy
from collections.abc import Callable
import json
from pathlib import Path
import subprocess
from typing import Any
from unittest import TestCase
from unittest.mock import patch

from control_plane.artifact_scan import (
    ArtifactScanResult,
    RegistryReadCredentials,
    SCAN_RESULT_TTL_SECONDS,
    GHCR_ARTIFACT_READ_INTEGRATION,
    _RESULTS,
    artifact_scan_from_report,
    scan_artifact,
    scan_product_artifact,
)

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
    def setUp(self) -> None:
        _RESULTS.clear()

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
            self.assertIn(IMAGE, argv)
            self.assertEqual(argv[argv.index("--image-src") + 1], "remote")
            self.assertNotIn("TRIVY_TOKEN", kwargs["env"])
            self.assertNotIn("GITHUB_TOKEN", kwargs["env"])
            self.assertEqual(Path(kwargs["cwd"]), Path(argv[argv.index("--config") + 1]).parent)
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

    def test_scan_cache_expires_and_credential_rotation_changes_its_identity(self) -> None:
        clean = ArtifactScanResult(
            image_reference=IMAGE, status="pass", reason="clean", scanned_at=NOW
        )
        with patch("control_plane.artifact_scan._scan_artifact", return_value=clean) as scanner:
            with patch("control_plane.artifact_scan.monotonic", return_value=0):
                scan_artifact(IMAGE)
                scan_artifact(IMAGE)
                self.assertEqual(scanner.call_count, 1)
                scan_artifact(
                    IMAGE, credentials=RegistryReadCredentials("fixture", "fixture-password")
                )
                self.assertEqual(scanner.call_count, 2)
            with patch(
                "control_plane.artifact_scan.monotonic", return_value=SCAN_RESULT_TTL_SECONDS + 1
            ):
                scan_artifact(IMAGE)
                self.assertEqual(scanner.call_count, 3)

    def test_registry_credential_is_purpose_scoped_and_never_used_for_another_host(self) -> None:
        with patch(
            "control_plane.secrets.resolve_context_secret_value",
            side_effect=lambda **kwargs: "fixture-" + kwargs["binding_key"],
        ) as resolve:
            with patch("control_plane.artifact_scan.scan_artifact") as scanner:
                scan_product_artifact(IMAGE)
                credentials = scanner.call_args.kwargs["credentials"]
                self.assertEqual(credentials.username, "fixture-username")
                self.assertNotIn("fixture-password", repr(credentials))
                self.assertTrue(
                    all(
                        call.kwargs["integration"] == GHCR_ARTIFACT_READ_INTEGRATION
                        for call in resolve.call_args_list
                    )
                )
                resolve.reset_mock()
                scan_product_artifact("registry.example/site@sha256:" + "a" * 64)
                resolve.assert_not_called()
                self.assertIsNone(scanner.call_args.kwargs["credentials"])
