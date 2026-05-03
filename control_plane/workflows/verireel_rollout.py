from __future__ import annotations

import json
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane import dokploy as control_plane_dokploy
from control_plane import runtime_environments as control_plane_runtime_environments
from control_plane.contracts.promotion_record import HealthcheckEvidence
from control_plane.workflows.ship import utc_now_timestamp


DEFAULT_ROLLOUT_TIMEOUT_SECONDS = 300
DEFAULT_ROLLOUT_INTERVAL_SECONDS = 5
DEFAULT_ROLLOUT_PAGE_PATHS = ("/", "/sign-in")


class VeriReelRolloutVerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str = "verireel"
    instance: str = "testing"
    expected_build_revision: str = ""
    expected_build_tag: str = ""
    timeout_seconds: int = Field(default=DEFAULT_ROLLOUT_TIMEOUT_SECONDS, ge=1)
    interval_seconds: int = Field(default=DEFAULT_ROLLOUT_INTERVAL_SECONDS, ge=1)

    @model_validator(mode="after")
    def _validate_request(self) -> "VeriReelRolloutVerificationRequest":
        if not self.context.strip():
            raise ValueError("VeriReel rollout verification requires context.")
        if self.instance not in {"testing", "prod"}:
            raise ValueError("VeriReel rollout verification requires instance 'testing' or 'prod'.")
        return self


class VeriReelRolloutVerificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    base_url: str = ""
    health_urls: tuple[str, ...] = ()
    started_at: str = ""
    finished_at: str = ""
    error_message: str = ""


def resolve_verireel_rollout_base_urls(
    *,
    control_plane_root: Path,
    context: str,
    instance: str,
) -> tuple[str, ...]:
    source_of_truth = control_plane_dokploy.read_control_plane_dokploy_source_of_truth(
        control_plane_root=control_plane_root,
    )
    target_definition = control_plane_dokploy.find_dokploy_target_definition(
        source_of_truth,
        context_name=context,
        instance_name=instance,
    )
    if target_definition is None:
        raise click.ClickException(f"No Dokploy target definition found for {context}/{instance}.")

    environment_values = control_plane_runtime_environments.resolve_runtime_environment_values(
        control_plane_root=control_plane_root,
        context_name=context,
        instance_name=instance,
    )
    base_urls = control_plane_dokploy.resolve_healthcheck_base_urls(
        target_definition=target_definition,
        environment_values=environment_values,
    )
    if not base_urls:
        raise click.ClickException(f"No rollout base URL configured for {context}/{instance}.")
    return base_urls


def fetch_url_text(url: str, *, accept: str) -> tuple[int, str]:
    request = Request(
        url,
        headers={
            "Accept": accept,
            "Cache-Control": "no-store",
        },
    )
    with urlopen(request, timeout=15) as response:
        return response.status, response.read().decode("utf-8")


def validate_verireel_health_payload(
    payload: object,
    *,
    health_url: str,
    expected_build_revision: str,
    expected_build_tag: str,
) -> str | None:
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return f"health payload from {health_url} did not report ok=true"
    if expected_build_revision and payload.get("buildRevision") != expected_build_revision:
        return (
            f"health payload from {health_url} reported buildRevision "
            f"'{payload.get('buildRevision', 'unknown')}' instead of expected "
            f"'{expected_build_revision}'"
        )
    if expected_build_tag and payload.get("buildTag") != expected_build_tag:
        return (
            f"health payload from {health_url} reported buildTag "
            f"'{payload.get('buildTag', 'unknown')}' instead of expected "
            f"'{expected_build_tag}'"
        )
    return None


def assert_verireel_rollout_pages(
    base_url: str,
    *,
    error_prefix: str = "VeriReel prod rollout",
) -> None:
    for page_path in DEFAULT_ROLLOUT_PAGE_PATHS:
        page_url = f"{base_url.rstrip('/')}{page_path}"
        try:
            status_code, response_text = fetch_url_text(
                page_url,
                accept="text/html,application/json",
            )
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            raise click.ClickException(
                f"{error_prefix} page verification failed for {page_url}: {exc}"
            ) from exc
        if status_code < 200 or status_code >= 300:
            raise click.ClickException(
                f"{error_prefix} page verification expected {page_url} to return 2xx, received {status_code}."
            )
        if "VeriReel" not in response_text:
            raise click.ClickException(
                f'{error_prefix} page verification expected {page_url} to include "VeriReel".'
            )


def verify_verireel_rollout(
    *,
    control_plane_root: Path,
    context: str,
    instance: str,
    expected_build_revision: str = "",
    expected_build_tag: str = "",
    timeout_seconds: int = DEFAULT_ROLLOUT_TIMEOUT_SECONDS,
    interval_seconds: int = DEFAULT_ROLLOUT_INTERVAL_SECONDS,
    error_prefix: str = "VeriReel prod rollout",
) -> VeriReelRolloutVerificationResult:
    started_at = utc_now_timestamp()
    base_urls = resolve_verireel_rollout_base_urls(
        control_plane_root=control_plane_root,
        context=context,
        instance=instance,
    )
    health_urls = tuple(f"{base_url.rstrip('/')}/api/health" for base_url in base_urls)
    last_error = "health endpoint not checked yet"
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() <= deadline:
        for base_url, health_url in zip(base_urls, health_urls, strict=False):
            try:
                status_code, response_text = fetch_url_text(
                    health_url,
                    accept="application/json,text/html",
                )
                if status_code < 200 or status_code >= 300:
                    last_error = f"received {status_code} from {health_url}"
                    continue
                payload = json.loads(response_text)
                validation_error = validate_verireel_health_payload(
                    payload,
                    health_url=health_url,
                    expected_build_revision=expected_build_revision,
                    expected_build_tag=expected_build_tag,
                )
                if validation_error is not None:
                    last_error = validation_error
                    continue
                assert_verireel_rollout_pages(base_url, error_prefix=error_prefix)
                return VeriReelRolloutVerificationResult(
                    status="pass",
                    base_url=base_url,
                    health_urls=(health_url,),
                    started_at=started_at,
                    finished_at=utc_now_timestamp(),
                )
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
                last_error = str(exc)
            except click.ClickException as exc:
                raise click.ClickException(str(exc)) from exc
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            break
        time.sleep(min(interval_seconds, remaining_seconds))
    raise click.ClickException(f"{error_prefix} verification timed out: {last_error}")


def health_evidence_from_rollout(
    *,
    result: VeriReelRolloutVerificationResult | None,
    timeout_seconds: int,
) -> HealthcheckEvidence:
    if result is None:
        return HealthcheckEvidence(status="skipped")
    if not result.health_urls:
        return HealthcheckEvidence(status=result.status)
    return HealthcheckEvidence(
        verified=result.status in {"pass", "fail"},
        urls=result.health_urls,
        timeout_seconds=timeout_seconds,
        status=result.status,
    )


def failed_verireel_rollout_result(
    *,
    control_plane_root: Path,
    context: str,
    instance: str,
    error_message: str,
) -> VeriReelRolloutVerificationResult:
    try:
        base_urls = resolve_verireel_rollout_base_urls(
            control_plane_root=control_plane_root,
            context=context,
            instance=instance,
        )
    except click.ClickException:
        return VeriReelRolloutVerificationResult(status="fail", error_message=error_message)
    return VeriReelRolloutVerificationResult(
        status="fail",
        base_url=base_urls[0],
        health_urls=(f"{base_urls[0].rstrip('/')}/api/health",),
        error_message=error_message,
    )


def execute_verireel_rollout_verification(
    *,
    control_plane_root: Path,
    request: VeriReelRolloutVerificationRequest,
) -> VeriReelRolloutVerificationResult:
    try:
        return verify_verireel_rollout(
            control_plane_root=control_plane_root,
            context=request.context,
            instance=request.instance,
            expected_build_revision=request.expected_build_revision,
            expected_build_tag=request.expected_build_tag,
            timeout_seconds=request.timeout_seconds,
            interval_seconds=request.interval_seconds,
            error_prefix=f"VeriReel {request.instance} rollout",
        )
    except click.ClickException as exc:
        return failed_verireel_rollout_result(
            control_plane_root=control_plane_root,
            context=request.context,
            instance=request.instance,
            error_message=str(exc),
        )
