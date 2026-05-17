from __future__ import annotations

import json
from collections.abc import Callable
from typing import NamedTuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import click

from control_plane.contracts.promotion_record import HealthcheckEvidence
from control_plane.contracts.runtime_identity import (
    RuntimeIdentity,
    health_payload_runtime_identity_status,
)


class HealthcheckPass(NamedTuple):
    payload: object = None
    json_parse_failed: bool = False


WaitForHealthcheck = Callable[[str, int], HealthcheckPass]


def wait_for_json_healthcheck(*, url: str, timeout_seconds: int) -> HealthcheckPass:
    request = Request(url, method="GET")
    with urlopen(request, timeout=min(5, timeout_seconds)) as response:  # noqa: S310 - operator-configured URL.
        body = response.read(1024 * 256)
        if not 200 <= response.status < 300:
            raise click.ClickException(f"http {response.status}")
    if not body.strip():
        return HealthcheckPass()
    try:
        return HealthcheckPass(payload=json.loads(body.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return HealthcheckPass(json_parse_failed=True)


def wait_for_healthcheck_with_retry(
    *,
    url: str,
    timeout_seconds: int,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    wait_once: Callable[..., HealthcheckPass] = wait_for_json_healthcheck,
) -> HealthcheckPass:
    deadline = monotonic() + timeout_seconds
    last_error: str = ""
    while monotonic() < deadline:
        try:
            return wait_once(url, timeout_seconds)
        except click.ClickException as error:
            last_error = str(error)
        except HTTPError as error:
            last_error = f"http {error.code}"
        except URLError as error:
            last_error = str(error.reason)
        sleep(1)
    raise click.ClickException(f"Healthcheck failed for {url}: {last_error or 'timeout'}")


def healthcheck_evidence_with_runtime_identity(
    evidence: HealthcheckEvidence,
    *,
    expected_runtime_identity: RuntimeIdentity | None,
    healthcheck_pass: HealthcheckPass,
) -> HealthcheckEvidence:
    status, detail, observed = health_payload_runtime_identity_status(
        expected=expected_runtime_identity,
        payload=healthcheck_pass.payload,
        json_parse_failed=healthcheck_pass.json_parse_failed,
    )
    return evidence.model_copy(
        update={
            "runtime_identity_status": status,
            "runtime_identity_detail": detail,
            "observed_runtime_identity": observed,
        }
    )
