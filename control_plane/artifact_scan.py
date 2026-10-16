"""Scan a provenance-verified image without executing it or reading host credentials."""

from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_IMAGE = re.compile(r"[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}")
_MAX_REPORT_BYTES = 32 * 1024 * 1024


class ArtifactScanResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    image_reference: str
    status: Literal["pass", "fail", "unavailable"]
    reason: str
    scanned_at: str
    high: int = Field(default=0, ge=0)
    critical: int = Field(default=0, ge=0)


def scan_artifact(image_reference: str) -> ArtifactScanResult:
    """Scan remotely by digest; an unavailable scanner never means a clean image.

    Trivy gets a clean working directory and explicit options, rather than product
    config, ignore files, plugins, a Docker socket or the host's registry login.
    Only vulnerability counts leave this boundary; provider errors stay private.
    """
    now = datetime.now(UTC).isoformat()

    def unavailable(reason: str) -> ArtifactScanResult:
        return ArtifactScanResult(
            image_reference=image_reference,
            status="unavailable",
            reason=reason,
            scanned_at=now,
        )

    if not _IMAGE.fullmatch(image_reference):
        return unavailable("immutable_image_required")
    try:
        with TemporaryDirectory(prefix="launchplane-artifact-scan-") as directory:
            root = Path(directory)
            config = root / "trivy.yaml"
            config.write_text("{}\n", encoding="utf-8")
            ignore = root / "ignore"
            ignore.write_text("", encoding="utf-8")
            report_path = root / "report.json"
            # A service scan has no ambient product/host credentials or TRIVY_* policy.
            environment = {
                "PATH": os.defpath,
                "HOME": directory,
                "DOCKER_CONFIG": directory,
                "XDG_CONFIG_HOME": directory,
                "XDG_CACHE_HOME": directory,
            }
            completed = subprocess.run(
                [
                    "trivy",
                    "image",
                    "--image-src",
                    "remote",
                    "--scanners",
                    "vuln",
                    "--format",
                    "json",
                    "--list-all-pkgs",
                    "--ignore-unfixed",
                    "--severity",
                    "HIGH,CRITICAL",
                    "--exit-code",
                    "0",
                    "--config",
                    str(config),
                    "--ignorefile",
                    str(ignore),
                    "--cache-dir",
                    str(root / "cache"),
                    "--timeout",
                    "4m",
                    "--output",
                    str(report_path),
                    image_reference,
                ],
                cwd=root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=270,
                check=False,
            )
            if completed.returncode:
                return unavailable("scanner_failed")
            if report_path.stat().st_size > _MAX_REPORT_BYTES:
                return unavailable("scanner_report_too_large")
            report = json.loads(report_path.read_bytes())
        return artifact_scan_from_report(image_reference, report, scanned_at=now)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return unavailable("scanner_unavailable")


def artifact_scan_from_report(
    image_reference: str, report: object, *, scanned_at: str
) -> ArtifactScanResult:
    """Require package inventory and the requested image, even for a clean report."""
    high = critical = 0
    if not isinstance(report, dict) or report.get("SchemaVersion") != 2:
        raise ValueError("Invalid artifact scan report.")
    if (
        report.get("ArtifactName") != image_reference
        or report.get("ArtifactType") != "container_image"
    ):
        raise ValueError("Scan report is for a different artifact.")
    results = report.get("Results")
    if not isinstance(results, list) or not results:
        raise ValueError("Scan report has no package inventory.")
    for result in results:
        if not isinstance(result, dict) or result.get("Class") not in {"os-pkgs", "lang-pkgs"}:
            raise ValueError("Scan report has unsupported results.")
        if not isinstance(result.get("Packages"), list) or not result["Packages"]:
            raise ValueError("Scan report has no package inventory.")
        vulnerabilities = result.get("Vulnerabilities", [])
        if not isinstance(vulnerabilities, list):
            raise ValueError("Invalid vulnerability inventory.")
        for finding in vulnerabilities:
            if not isinstance(finding, dict):
                raise ValueError("Invalid vulnerability.")
            severity = finding.get("Severity")
            if severity not in {"UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL"}:
                raise ValueError("Invalid vulnerability severity.")
            # Independently apply the gate, even if a producer did not filter.
            if finding.get("FixedVersion"):
                high += severity == "HIGH"
                critical += severity == "CRITICAL"
    return ArtifactScanResult(
        image_reference=image_reference,
        status="fail" if high or critical else "pass",
        reason="fixable_vulnerabilities" if high or critical else "clean",
        scanned_at=scanned_at,
        high=high,
        critical=critical,
    )
