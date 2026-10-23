"""Scan a provenance-verified image without executing it or reading host credentials."""

from datetime import UTC, datetime
from collections import OrderedDict
from dataclasses import dataclass, field
import hashlib
import base64
import json
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
from threading import Lock
from time import monotonic
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

_IMAGE = re.compile(r"[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}")
_MAX_REPORT_BYTES = 32 * 1024 * 1024
SCAN_RESULT_TTL_SECONDS = 30 * 60
SCAN_UNAVAILABLE_TTL_SECONDS = 60
GHCR_ARTIFACT_READ_INTEGRATION = "ghcr_artifact_read"
_SCAN_LOCK = Lock()
_RESULTS_LOCK = Lock()
_SCANNER_CACHE: TemporaryDirectory[str] | None = None


@dataclass(frozen=True)
class RegistryReadCredentials:
    username: str = field(repr=False)
    password: str = field(repr=False)


class ArtifactScanResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    image_reference: str
    status: Literal["pass", "fail", "unavailable"]
    reason: str
    scanned_at: str
    high: int = Field(default=0, ge=0)
    critical: int = Field(default=0, ge=0)


_RESULTS: OrderedDict[tuple[str, str], tuple[float, ArtifactScanResult]] = OrderedDict()


def scan_product_artifact(image_reference: str) -> ArtifactScanResult:
    from control_plane import secrets
    import click

    credentials = None
    try:
        registry = urlsplit("https://" + image_reference.split("@", 1)[0])
        values = (
            {
                key: secrets.resolve_context_secret_value(
                    integration=GHCR_ARTIFACT_READ_INTEGRATION,
                    context_name="launchplane",
                    binding_key=key,
                )
                for key in ("username", "password")
            }
            if _IMAGE.fullmatch(image_reference) and registry.netloc == "ghcr.io"
            else {}
        )
        if values.get("username") and values.get("password"):
            credentials = RegistryReadCredentials(**values)
    except (click.ClickException, OSError, ValueError):
        pass  # Public registries remain usable without private registry authority.
    return scan_artifact(image_reference, credentials=credentials)


def qualify_product_artifact(record_store: object, *, product: str) -> ArtifactScanResult:
    """Read-only qualification of the verified testing candidate, even before activation."""
    from typing import cast
    from control_plane.product_reconcile import (
        ProductReconcileError,
        ProductReconcileStore,
        _plan_testing_target,
        product_repository_identity,
        resolve_build_provenance_transport,
    )
    from control_plane.build_provenance import BuildProvenanceError

    store = cast(ProductReconcileStore, record_store)
    try:
        profile = store.read_product_profile_record(product)
    except FileNotFoundError:
        raise ValueError("An active product profile is required.") from None
    if not profile.is_active or not profile.is_managed_runtime:
        raise ValueError("An active product profile is required.")
    try:
        identity = product_repository_identity(store, profile)
        transport = resolve_build_provenance_transport(store, profile)
        _plan, verified = _plan_testing_target(
            record_store=store,
            profile=profile,
            repository_id=identity.repository_id,
            transport=transport,
        )
        if verified is not None:
            return scan_product_artifact(
                f"{verified.manifest.image.repository}@{verified.manifest.image.digest}"
            )
    except (BuildProvenanceError, ProductReconcileError, OSError, ValueError):
        pass
    return ArtifactScanResult(
        image_reference="",
        status="unavailable",
        reason="verified_build_unavailable",
        scanned_at=datetime.now(UTC).isoformat(),
    )


def scan_artifact(
    image_reference: str, *, credentials: RegistryReadCredentials | None = None
) -> ArtifactScanResult:
    """Bound repeated scans while keeping scanner databases in an isolated cache."""
    credential_key = hashlib.sha256(
        repr((credentials.username, credentials.password)).encode() if credentials else b"anonymous"
    ).hexdigest()
    key = image_reference, credential_key

    def cached_result() -> ArtifactScanResult | None:
        with _RESULTS_LOCK:
            cached = _RESULTS.get(key)
            if cached is not None and monotonic() - cached[0] < (
                SCAN_UNAVAILABLE_TTL_SECONDS
                if cached[1].status == "unavailable"
                else SCAN_RESULT_TTL_SECONDS
            ):
                _RESULTS.move_to_end(key)
                return cached[1]
        return None

    cached = cached_result()
    if cached is not None:
        return cached
    if not _SCAN_LOCK.acquire(timeout=5):
        return ArtifactScanResult(
            image_reference=image_reference,
            status="unavailable",
            reason="scanner_busy",
            scanned_at=datetime.now(UTC).isoformat(),
        )
    try:
        cached = cached_result()
        if cached is not None:
            return cached
        result = _scan_artifact(image_reference, credentials=credentials)
        with _RESULTS_LOCK:
            _RESULTS[key] = monotonic(), result
            _RESULTS.move_to_end(key)
            while len(_RESULTS) > 128:
                _RESULTS.popitem(last=False)
        return result
    finally:
        _SCAN_LOCK.release()


def _scan_artifact(
    image_reference: str, *, credentials: RegistryReadCredentials | None
) -> ArtifactScanResult:
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
        global _SCANNER_CACHE
        if _SCANNER_CACHE is None:
            _SCANNER_CACHE = TemporaryDirectory(prefix="launchplane-artifact-cache-")
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
            if credentials is not None:
                # Registry credentials must not become Trivy's global DB-download
                # credentials. Docker's auth map scopes this entry to GHCR only.
                auth = base64.b64encode(
                    f"{credentials.username}:{credentials.password}".encode()
                ).decode()
                with os.fdopen(
                    os.open(root / "config.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
                ) as auth_file:
                    json.dump({"auths": {"ghcr.io": {"auth": auth}}}, auth_file)
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
                    _SCANNER_CACHE.name,
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
