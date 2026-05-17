from __future__ import annotations

from collections.abc import Callable
import html
import json
import re
import time
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

import click
from pydantic import BaseModel, ConfigDict, Field

from control_plane.contracts.promotion_record import ReleaseStatus


ODOO_VERIFY_RETRY_INTERVAL_SECONDS = 5
OdooProbeKind = Literal["health", "canonical", "logo"]
OdooReadinessStatus = Literal["pass", "fail", "skipped"]


class OdooVerificationProbeEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    probe: OdooProbeKind
    url: str
    status: ReleaseStatus = "pending"
    status_code: int | None = None
    content_type: str = ""
    failure_class: str = ""
    detail: str = ""


class OdooVerificationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = ""
    health_url: str = ""
    canonical_url: str = ""
    logo_urls: tuple[str, ...] = ()
    probes: tuple[OdooVerificationProbeEvidence, ...] = ()


class OdooVerificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    health_status: OdooReadinessStatus = "skipped"
    canonical_status: OdooReadinessStatus = "skipped"
    logo_status: OdooReadinessStatus = "skipped"
    evidence: OdooVerificationEvidence = Field(default_factory=OdooVerificationEvidence)
    error_message: str = ""


class _ProbeFailure(click.ClickException):
    def __init__(self, message: str, *, evidence: OdooVerificationProbeEvidence) -> None:
        super().__init__(message)
        self.evidence = evidence


def default_odoo_health_url(*, base_url: str, health_path: str = "/web/health") -> str:
    normalized_base_url = base_url.strip().rstrip("/")
    if not normalized_base_url:
        return ""
    normalized_health_path = health_path.strip() or "/web/health"
    if not normalized_health_path.startswith("/"):
        normalized_health_path = f"/{normalized_health_path}"
    return f"{normalized_base_url}{normalized_health_path}"


def verify_odoo_stable_readiness(
    *,
    base_url: str,
    health_url: str = "",
    verify_health: bool = True,
    verify_canonical: bool = True,
    verify_logo: bool = True,
    timeout_seconds: int,
    retry_interval_seconds: int = ODOO_VERIFY_RETRY_INTERVAL_SECONDS,
) -> OdooVerificationResult:
    normalized_base_url = base_url.strip().rstrip("/")
    normalized_health_url = health_url.strip() or default_odoo_health_url(
        base_url=normalized_base_url
    )
    probes: list[OdooVerificationProbeEvidence] = []
    health_status: OdooReadinessStatus = "skipped"
    canonical_status: OdooReadinessStatus = "skipped"
    logo_status: OdooReadinessStatus = "skipped"
    logo_urls: tuple[str, ...] = ()

    try:
        if verify_health:
            if not normalized_health_url:
                raise click.ClickException("Odoo health verification has no health URL.")
            evidence = _run_health_probe_with_retry(
                lambda: _verify_health_url(
                    health_url=normalized_health_url,
                    timeout_seconds=timeout_seconds,
                ),
                timeout_seconds=timeout_seconds,
                retry_interval_seconds=retry_interval_seconds,
            )
            probes.append(evidence)
            health_status = "pass"
        if verify_canonical:
            if not normalized_base_url:
                raise click.ClickException("Odoo canonical verification has no base URL.")
            evidence = _run_probe_with_retry(
                lambda: _verify_canonical_url(
                    base_url=normalized_base_url,
                    expected_base_url=normalized_base_url,
                    timeout_seconds=timeout_seconds,
                ),
                timeout_seconds=timeout_seconds,
                retry_interval_seconds=retry_interval_seconds,
            )
            probes.append(evidence)
            canonical_status = "pass"
        if verify_logo:
            if not normalized_base_url:
                raise click.ClickException("Odoo logo verification has no base URL.")
            evidence, logo_urls = _run_logo_probe_with_retry(
                lambda: _verify_logo_route(
                    base_url=normalized_base_url,
                    timeout_seconds=timeout_seconds,
                ),
                timeout_seconds=timeout_seconds,
                retry_interval_seconds=retry_interval_seconds,
            )
            probes.append(evidence)
            logo_status = "pass"
    except _ProbeFailure as error:
        probes.append(error.evidence)
        if error.evidence.probe == "health":
            health_status = "fail"
        elif error.evidence.probe == "canonical":
            canonical_status = "fail"
        elif error.evidence.probe == "logo":
            logo_status = "fail"
            logo_urls = (error.evidence.url,) if error.evidence.url else ()
        return OdooVerificationResult(
            health_status=health_status,
            canonical_status=canonical_status,
            logo_status=logo_status,
            evidence=OdooVerificationEvidence(
                base_url=normalized_base_url,
                health_url=normalized_health_url,
                canonical_url=normalized_base_url,
                logo_urls=logo_urls,
                probes=tuple(probes),
            ),
            error_message=str(error),
        )
    except click.ClickException as error:
        return OdooVerificationResult(
            health_status="fail" if verify_health and health_status != "pass" else health_status,
            canonical_status="fail" if verify_canonical and canonical_status != "pass" else canonical_status,
            logo_status="fail" if verify_logo and logo_status != "pass" else logo_status,
            evidence=OdooVerificationEvidence(
                base_url=normalized_base_url,
                health_url=normalized_health_url,
                canonical_url=normalized_base_url,
                logo_urls=logo_urls,
                probes=tuple(probes),
            ),
            error_message=str(error),
        )

    return OdooVerificationResult(
        health_status=health_status,
        canonical_status=canonical_status,
        logo_status=logo_status,
        evidence=OdooVerificationEvidence(
            base_url=normalized_base_url,
            health_url=normalized_health_url,
            canonical_url=normalized_base_url,
            logo_urls=logo_urls,
            probes=tuple(probes),
        ),
    )


def _http_text(url: str, *, timeout_seconds: int) -> tuple[int, str, str]:
    request = Request(url, headers={"User-Agent": "Launchplane-Odoo-Verify/1"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310 - operator-configured URLs only.
            body = response.read(1024 * 512).decode("utf-8", errors="replace")
            content_type = response.headers.get("Content-Type", "")
            return response.status, body, content_type
    except HTTPError as error:
        body = error.read(4096).decode("utf-8", errors="replace")
        return error.code, body, error.headers.get("Content-Type", "")
    except (TimeoutError, URLError) as error:
        raise click.ClickException(f"Could not reach {url}: {error}") from error


def _verify_health_url(*, health_url: str, timeout_seconds: int) -> OdooVerificationProbeEvidence:
    status_code, body, content_type = _http_text(health_url, timeout_seconds=timeout_seconds)
    evidence = OdooVerificationProbeEvidence(
        probe="health", url=health_url, status_code=status_code, content_type=content_type
    )
    if status_code >= 400:
        raise _ProbeFailure(
            f"Health check {health_url} returned HTTP {status_code}.",
            evidence=evidence.model_copy(update={"status": "fail", "failure_class": "http"}),
        )
    try:
        payload = json.loads(body)
    except ValueError:
        return evidence.model_copy(update={"status": "pass"})
    if isinstance(payload, dict):
        raw_status = str(payload.get("status") or "").strip().lower()
        if raw_status and raw_status not in {"ok", "pass", "healthy"}:
            raise _ProbeFailure(
                f"Health check {health_url} returned status={raw_status!r}.",
                evidence=evidence.model_copy(
                    update={
                        "status": "fail",
                        "failure_class": "payload_status",
                        "detail": f"status={raw_status}",
                    }
                ),
            )
    return evidence.model_copy(update={"status": "pass"})


def _verify_canonical_url(
    *, base_url: str, expected_base_url: str, timeout_seconds: int
) -> OdooVerificationProbeEvidence:
    status_code, body, content_type = _http_text(base_url, timeout_seconds=timeout_seconds)
    evidence = OdooVerificationProbeEvidence(
        probe="canonical", url=base_url, status_code=status_code, content_type=content_type
    )
    if status_code >= 400:
        raise _ProbeFailure(
            f"Canonical check {base_url} returned HTTP {status_code}.",
            evidence=evidence.model_copy(update={"status": "fail", "failure_class": "http"}),
        )
    expected = expected_base_url.rstrip("/")
    stale_preview_marker = ".cm-preview."
    if stale_preview_marker in body:
        raise _ProbeFailure(
            "Canonical page still contains a cm-preview hostname.",
            evidence=evidence.model_copy(
                update={"status": "fail", "failure_class": "stale_preview_marker"}
            ),
        )
    canonical_href = f'rel="canonical" href="{expected}'
    alternate_canonical_href = f"rel='canonical' href='{expected}"
    if canonical_href not in body and alternate_canonical_href not in body:
        raise _ProbeFailure(
            f"Canonical page does not advertise {expected}.",
            evidence=evidence.model_copy(
                update={"status": "fail", "failure_class": "canonical_missing"}
            ),
        )
    return evidence.model_copy(update={"status": "pass"})


def _verify_logo_route(
    *, base_url: str, timeout_seconds: int
) -> tuple[OdooVerificationProbeEvidence, tuple[str, ...]]:
    checked_urls: list[str] = []
    last_failure: OdooVerificationProbeEvidence | None = None
    for logo_url in _candidate_logo_urls(base_url=base_url, timeout_seconds=timeout_seconds):
        if logo_url in checked_urls:
            continue
        checked_urls.append(logo_url)
        status_code, body, content_type = _http_text(logo_url, timeout_seconds=timeout_seconds)
        evidence = OdooVerificationProbeEvidence(
            probe="logo", url=logo_url, status_code=status_code, content_type=content_type
        )
        if status_code < 400 and not ("text/html" in content_type.lower() and "404" in body[:500].lower()):
            return evidence.model_copy(update={"status": "pass"}), tuple(checked_urls)
        failure_class = "http" if status_code >= 400 else "html_404"
        last_failure = evidence.model_copy(
            update={
                "status": "fail",
                "failure_class": failure_class,
                "detail": f"checked={', '.join(checked_urls)}",
            }
        )
    checked = ", ".join(checked_urls) if checked_urls else base_url
    if last_failure is not None and last_failure.status_code is not None:
        raise _ProbeFailure(
            f"Logo check {last_failure.url} returned HTTP {last_failure.status_code}.",
            evidence=last_failure,
        )
    raise _ProbeFailure(
        f"Logo check failed for {checked}.",
        evidence=last_failure
        or OdooVerificationProbeEvidence(
            probe="logo", url=base_url, status="fail", failure_class="logo_unavailable"
        ),
    )


def _candidate_logo_urls(*, base_url: str, timeout_seconds: int) -> tuple[str, ...]:
    normalized_base_url = base_url.rstrip("/")
    fallback_url = f"{normalized_base_url}/web/image/website/1/logo"
    candidates: list[str] = []
    status_code, body, _content_type = _http_text(normalized_base_url, timeout_seconds=timeout_seconds)
    if status_code < 400:
        candidates.extend(_extract_same_origin_logo_urls(base_url=normalized_base_url, body=body))
    if fallback_url not in candidates:
        candidates.append(fallback_url)
    return tuple(candidates)


def _extract_same_origin_logo_urls(*, base_url: str, body: str) -> tuple[str, ...]:
    base_origin = _url_origin(base_url)
    logo_urls: list[str] = []
    for match in re.finditer(
        (
            r"(?:src|href|content)\s*=\s*"
            r"(?P<quote>[\"'])?"
            r"(?P<url>[^\s\"'<>]*/web/(?:image|content)[^\s\"'<>]*)"
            r"(?P=quote)?"
        ),
        body,
        re.IGNORECASE,
    ):
        raw_url = html.unescape(match.group("url")).strip()
        absolute_url = urljoin(f"{base_url.rstrip('/')}/", raw_url)
        if (
            _url_origin(absolute_url) == base_origin
            and _is_odoo_website_logo_url(absolute_url)
            and absolute_url not in logo_urls
        ):
            logo_urls.append(absolute_url)
    return tuple(logo_urls)


def _is_odoo_website_logo_url(raw_url: str) -> bool:
    parsed = urlparse(raw_url)
    path = parsed.path.lower()
    query = parsed.query.lower()
    if path.startswith("/web/image/website/") and "/logo" in path:
        return True
    if path.startswith("/web/content/website/") and "/logo" in path:
        return True
    if path == "/web/content" and "model=website" in query and "field=logo" in query:
        return True
    return False


def _url_origin(raw_url: str) -> str:
    parsed = urlparse(raw_url)
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def _run_probe_with_retry(
    verification: Callable[[], OdooVerificationProbeEvidence],
    *,
    timeout_seconds: int,
    retry_interval_seconds: int,
) -> OdooVerificationProbeEvidence:
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            return verification()
        except _ProbeFailure:
            raise
        except click.ClickException as error:
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                raise error
            time.sleep(min(retry_interval_seconds, remaining_seconds))


def _run_health_probe_with_retry(
    verification: Callable[[], OdooVerificationProbeEvidence],
    *,
    timeout_seconds: int,
    retry_interval_seconds: int,
) -> OdooVerificationProbeEvidence:
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            return verification()
        except click.ClickException as error:
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                raise error
            time.sleep(min(retry_interval_seconds, remaining_seconds))


def _run_logo_probe_with_retry(
    verification: Callable[[], tuple[OdooVerificationProbeEvidence, tuple[str, ...]]],
    *,
    timeout_seconds: int,
    retry_interval_seconds: int,
) -> tuple[OdooVerificationProbeEvidence, tuple[str, ...]]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            return verification()
        except _ProbeFailure:
            raise
        except click.ClickException as error:
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                raise error
            time.sleep(min(retry_interval_seconds, remaining_seconds))
