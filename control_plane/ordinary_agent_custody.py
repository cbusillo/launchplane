from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import time
from typing import Literal, Protocol

from control_plane import secrets as control_plane_secrets
from control_plane.contracts.ordinary_agent_custody import (
    GITHUB_TOKEN_MAXIMUM_LIFETIME_SECONDS,
    KNOWN_TOKEN_CLOCK_SKEW_SECONDS,
    OrdinaryAgentCustodyCandidate,
    OrdinaryAgentCustodyIssueAttempt,
)
from control_plane.contracts.ordinary_agent_provider import (
    ORDINARY_AGENT_GITHUB_APP_INTEGRATION as ORDINARY_AGENT_GITHUB_APP_INTEGRATION,
    ORDINARY_AGENT_GITHUB_APP_PRIVATE_KEY_BINDING as ORDINARY_AGENT_GITHUB_APP_PRIVATE_KEY_BINDING,
)
from control_plane.contracts.secret_record import SecretBinding, SecretRecord, SecretVersion
from control_plane.github_app_identity import (
    GitHubApiRequest,
    GitHubAppIdentity,
    GitHubAppInstallationToken,
    mint_ordinary_agent_installation_token,
    ordinary_agent_effect_permissions,
    revoke_installation_token,
)
from control_plane.workflows.launchplane import github_api_request


DISPATCH_WINDOW_SECONDS = 30


class OrdinaryAgentCustodyError(ValueError):
    pass


class OrdinaryAgentCustodyUnavailable(OrdinaryAgentCustodyError):
    def __init__(self, message: str, *, attempt: OrdinaryAgentCustodyIssueAttempt) -> None:
        super().__init__(message)
        self.attempt = attempt


@dataclass(frozen=True, slots=True)
class ResolvedOrdinaryAgentGitHubAppIdentity:
    identity: GitHubAppIdentity = field(repr=False)
    secret_id: str
    secret_binding_id: str
    secret_version_id: str


@dataclass(frozen=True, slots=True)
class OrdinaryAgentProviderTokenLease:
    installation_token: GitHubAppInstallationToken = field(repr=False)
    attempt_id: str


class OrdinaryAgentCustodySecretStore(Protocol):
    def read_secret_record(self, secret_id: str) -> SecretRecord: ...

    def read_secret_version(self, version_id: str) -> SecretVersion: ...

    def list_secret_bindings(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[SecretBinding, ...]: ...


class OrdinaryAgentCustodyAttemptStore(Protocol):
    def acquire_ordinary_agent_custody_issue_attempt(
        self,
        *,
        attempt_id: str,
        idempotency_key_sha256: str,
        request_sha256: str,
        candidate: OrdinaryAgentCustodyCandidate,
        requested_permissions: tuple[str, ...],
        dispatch_window_seconds: int,
    ) -> tuple[Literal["acquired", "replay", "fenced"], OrdinaryAgentCustodyIssueAttempt]: ...

    def mark_ordinary_agent_custody_issue_unknown(
        self, *, attempt_id: str
    ) -> OrdinaryAgentCustodyIssueAttempt: ...

    def mark_ordinary_agent_custody_issued(
        self,
        *,
        attempt_id: str,
        app_id: int,
        installation_id: int,
        token_expires_at: str,
        residual_expires_at: str,
    ) -> OrdinaryAgentCustodyIssueAttempt: ...

    def mark_ordinary_agent_custody_cleanup_unknown(
        self, *, attempt_id: str
    ) -> OrdinaryAgentCustodyIssueAttempt: ...

    def close_ordinary_agent_custody_issue_attempt(
        self,
        *,
        attempt_id: str,
        reason: Literal["not_dispatched", "confirmed_revoked", "known_expired"],
    ) -> OrdinaryAgentCustodyIssueAttempt: ...


def resolve_ordinary_agent_github_app_identity(
    *,
    record_store: OrdinaryAgentCustodySecretStore,
    candidate: OrdinaryAgentCustodyCandidate,
) -> ResolvedOrdinaryAgentGitHubAppIdentity:
    try:
        record = record_store.read_secret_record(candidate.secret_id)
        version = record_store.read_secret_version(candidate.secret_version_id)
    except FileNotFoundError as error:
        raise OrdinaryAgentCustodyError(
            "Ordinary-agent GitHub App secret is unavailable."
        ) from error
    bindings = tuple(
        binding
        for binding in record_store.list_secret_bindings(
            integration=ORDINARY_AGENT_GITHUB_APP_INTEGRATION,
            limit=None,
        )
        if binding.binding_id == candidate.secret_binding_id
    )
    if len(bindings) != 1:
        raise OrdinaryAgentCustodyError(
            "Ordinary-agent GitHub App requires one exact managed-secret binding."
        )
    binding = bindings[0]
    if (
        record.status != "configured"
        or record.policy != "write_only"
        or record.integration != ORDINARY_AGENT_GITHUB_APP_INTEGRATION
        or record.current_version_id != candidate.secret_version_id
        or version.secret_id != record.secret_id
        or binding.status != "configured"
        or binding.secret_id != record.secret_id
        or binding.integration != ORDINARY_AGENT_GITHUB_APP_INTEGRATION
        or binding.binding_key != ORDINARY_AGENT_GITHUB_APP_PRIVATE_KEY_BINDING
    ):
        raise OrdinaryAgentCustodyError(
            "Ordinary-agent GitHub App managed-secret binding is not current and exact."
        )
    private_key = control_plane_secrets._decrypt_secret_value(
        version.ciphertext, version.key_id
    ).strip()
    if not private_key:
        raise OrdinaryAgentCustodyError("Ordinary-agent GitHub App secret is unavailable.")
    return ResolvedOrdinaryAgentGitHubAppIdentity(
        identity=GitHubAppIdentity(app_id=candidate.expected_app_id, private_key=private_key),
        secret_id=record.secret_id,
        secret_binding_id=binding.binding_id,
        secret_version_id=version.version_id,
    )


@contextmanager
def ordinary_agent_provider_token_lease(
    *,
    record_store: OrdinaryAgentCustodyAttemptStore,
    secret_store: OrdinaryAgentCustodySecretStore,
    candidate: OrdinaryAgentCustodyCandidate,
    idempotency_key: str,
    request_payload: Mapping[str, object],
    api_request: GitHubApiRequest = github_api_request,
    monotonic: Callable[[], float] = time.monotonic,
    utc_now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> Iterator[OrdinaryAgentProviderTokenLease]:
    idempotency_digest = _sha256_text(idempotency_key)
    request_digest = _sha256_text(
        json.dumps(
            {
                "candidate": candidate.model_dump(mode="json"),
                "request": request_payload,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    attempt_id = f"custody_{idempotency_digest}"
    monotonic_anchor = monotonic()
    status, attempt = record_store.acquire_ordinary_agent_custody_issue_attempt(
        attempt_id=attempt_id,
        idempotency_key_sha256=idempotency_digest,
        request_sha256=request_digest,
        candidate=candidate,
        requested_permissions=ordinary_agent_effect_permissions(candidate.effect_profile),
        dispatch_window_seconds=DISPATCH_WINDOW_SECONDS,
    )
    if status != "acquired":
        raise OrdinaryAgentCustodyUnavailable(
            "Ordinary-agent provider credential issuance is already fenced.",
            attempt=attempt,
        )

    try:
        resolved = resolve_ordinary_agent_github_app_identity(
            record_store=secret_store,
            candidate=candidate,
        )
    except Exception:
        record_store.close_ordinary_agent_custody_issue_attempt(
            attempt_id=attempt_id, reason="not_dispatched"
        )
        raise

    dispatch_attempted = False

    def bounded_request(**kwargs: object) -> object:
        nonlocal dispatch_attempted
        if monotonic() - monotonic_anchor >= DISPATCH_WINDOW_SECONDS:
            raise OrdinaryAgentCustodyError(
                "Ordinary-agent provider credential dispatch window expired."
            )
        path = kwargs.get("path")
        method = kwargs.get("method", "GET")
        if method == "POST" and isinstance(path, str) and path.endswith("/access_tokens"):
            dispatch_attempted = True
        return api_request(**kwargs)

    token: GitHubAppInstallationToken | None = None
    issued = False
    try:
        try:
            token = mint_ordinary_agent_installation_token(
                identity=resolved.identity,
                repository=candidate.repository,
                repository_id=str(candidate.repository_id),
                effect_profile=candidate.effect_profile,
                api_request=bounded_request,
                now=utc_now(),
            )
        except Exception:
            if dispatch_attempted:
                record_store.mark_ordinary_agent_custody_issue_unknown(attempt_id=attempt_id)
            else:
                record_store.close_ordinary_agent_custody_issue_attempt(
                    attempt_id=attempt_id, reason="not_dispatched"
                )
            raise

        received_at = utc_now().astimezone(timezone.utc)
        token_expires_at = _parse_provider_expiry(token.expires_at)
        if token_expires_at <= received_at or token_expires_at > received_at + timedelta(
            seconds=GITHUB_TOKEN_MAXIMUM_LIFETIME_SECONDS
        ):
            raise OrdinaryAgentCustodyError(
                "Ordinary-agent provider token expiry is outside the supported provider bound."
            )
        residual_expires_at = token_expires_at + timedelta(seconds=KNOWN_TOKEN_CLOCK_SKEW_SECONDS)
        record_store.mark_ordinary_agent_custody_issued(
            attempt_id=attempt_id,
            app_id=token.app_id,
            installation_id=token.installation_id,
            token_expires_at=_format_timestamp(token_expires_at),
            residual_expires_at=_format_timestamp(residual_expires_at),
        )
        issued = True
        if monotonic() - monotonic_anchor >= DISPATCH_WINDOW_SECONDS:
            raise OrdinaryAgentCustodyError(
                "Ordinary-agent provider token arrived after its dispatch window."
            )
        yield OrdinaryAgentProviderTokenLease(
            installation_token=token,
            attempt_id=attempt_id,
        )
    finally:
        if token is not None:
            try:
                revoke_installation_token(installation_token=token, api_request=api_request)
            except Exception:
                if issued:
                    record_store.mark_ordinary_agent_custody_cleanup_unknown(attempt_id=attempt_id)
                else:
                    record_store.mark_ordinary_agent_custody_issue_unknown(attempt_id=attempt_id)
                raise
            else:
                record_store.close_ordinary_agent_custody_issue_attempt(
                    attempt_id=attempt_id, reason="confirmed_revoked"
                )


def _parse_provider_expiry(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise OrdinaryAgentCustodyError(
            "Ordinary-agent provider token expiry is malformed."
        ) from error
    if parsed.tzinfo is None:
        raise OrdinaryAgentCustodyError(
            "Ordinary-agent provider token expiry must include a timezone."
        )
    return parsed.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256_text(value: str) -> str:
    if not value:
        raise OrdinaryAgentCustodyError("Ordinary-agent custody identity must not be empty.")
    return hashlib.sha256(value.encode()).hexdigest()
