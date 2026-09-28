"""Keep platform credentials out of application runtime environments.

Launchplane holds source-control, deploy-provider, and its own service
credentials to operate products. An application runtime never needs them, and
a preview runs unmerged code that can read anything in its environment. Every
path that renders or writes an application runtime environment checks it here.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

import click

PLATFORM_CREDENTIAL_KEYS = frozenset(
    (
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "DOKPLOY_TOKEN",
        "DOKPLOY_HOST",
        "LAUNCHPLANE_SERVICE_TOKEN",
        "LAUNCHPLANE_LOCAL_OPERATOR_TOKEN",
        "LAUNCHPLANE_LOCAL_ADMIN_TOKEN",
        "LAUNCHPLANE_TERMINAL_AGENT_READ_TOKEN",
        "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN",
        "LAUNCHPLANE_EVERY_CODE_GITHUB_TOKEN",
        "LAUNCHPLANE_PUBLIC_INGRESS_GITHUB_TOKEN",
        "LAUNCHPLANE_WORK_GRAPH_GH_TOKEN",
        "LAUNCHPLANE_EMERGENCY_DOKPLOY_HOST",
        "LAUNCHPLANE_EMERGENCY_DOKPLOY_TOKEN",
        "LAUNCHPLANE_MASTER_ENCRYPTION_KEY",
        "LAUNCHPLANE_SECRET_KEYS_JSON",
    )
)

# Refusal favors precision over the looser redaction pattern in
# child_process_errors: real GitHub tokens carry long bodies, so short
# look-alike configuration values do not block a deployment.
GITHUB_TOKEN_VALUE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"
)

PlatformCredentialReason = Literal["platform_credential_key", "github_token_value"]


@dataclass(frozen=True, slots=True)
class PlatformCredentialFinding:
    key: str
    reason: PlatformCredentialReason
    source: str = ""


class PlatformCredentialRefusedError(click.ClickException):
    """Raised instead of rendering or writing an app runtime with platform credentials."""

    code = "runtime_platform_credential_refused"

    def __init__(self, *, target: str, findings: tuple[PlatformCredentialFinding, ...]) -> None:
        self.target = target
        self.findings = findings
        details = "; ".join(_describe_finding(finding) for finding in findings)
        super().__init__(
            f"Refusing to render the app runtime environment for {target}: {details}. "
            "Platform credentials never belong in an app runtime. Remove the key from that "
            "source, or retire a legacy provider key through product-config "
            "retired_provider_keys."
        )


def platform_credential_reason(key: str, value: str) -> PlatformCredentialReason | None:
    if not value.strip():
        return None
    if key.strip() in PLATFORM_CREDENTIAL_KEYS:
        return "platform_credential_key"
    if GITHUB_TOKEN_VALUE_PATTERN.search(value):
        return "github_token_value"
    return None


def find_platform_credentials(
    values: Mapping[str, str], *, source: str = ""
) -> tuple[PlatformCredentialFinding, ...]:
    findings: list[PlatformCredentialFinding] = []
    for key in sorted(values):
        reason = platform_credential_reason(key, str(values[key]))
        if reason is not None:
            findings.append(PlatformCredentialFinding(key=key, reason=reason, source=source))
    return tuple(findings)


def refuse_platform_credentials(values: Mapping[str, str], *, target: str, source: str) -> None:
    findings = find_platform_credentials(values, source=source)
    if findings:
        raise PlatformCredentialRefusedError(target=target, findings=findings)


def refuse_introduced_platform_credentials(
    *, env_map: Mapping[str, str], current_env_map: Mapping[str, str], target: str
) -> None:
    """Refuse a provider env write that adds or changes a platform credential.

    A key already present with the same value is legacy provider state: the
    write preserves it, and the provider-env report lists it for retirement.
    """

    introduced = {key: value for key, value in env_map.items() if current_env_map.get(key) != value}
    refuse_platform_credentials(introduced, target=target, source="the provider env write")


def _describe_finding(finding: PlatformCredentialFinding) -> str:
    source = f" from {finding.source}" if finding.source else ""
    if finding.reason == "github_token_value":
        return f"{finding.key}{source} holds a GitHub token value"
    return f"{finding.key}{source} is a platform credential"
