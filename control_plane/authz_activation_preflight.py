from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
import json
import math
from typing import Protocol

from control_plane import secrets as control_plane_secrets
from control_plane.contracts.authz_access_read import (
    AUTHZ_ACTIVATION_PREFLIGHT_MAX_SESSIONS,
    AuthzActivationPreflightDecision,
    AuthzActivationPreflightPolicySummary,
    AuthzActivationPreflightResponse,
    AuthzActivationPreflightScope,
    AuthzActivationPreflightSessionSummary,
    AuthzUnmanagedActionEmptyRuleCounts,
)
from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.service_auth import (
    AuthorizationTarget,
    GitHubHumanIdentity,
    LaunchplaneAuthzPolicy,
)
from control_plane.service_human_auth import (
    LaunchplaneHumanSession,
    SESSION_AUTHORIZATION_CLAIMS_TTL_SECONDS,
)


ACTIVATION_PREFLIGHT_ACTION = "authz_policy_grant.write"
ACTIVATION_PREFLIGHT_PRODUCT = "launchplane"
ACTIVATION_PREFLIGHT_CONTEXT = "launchplane"
ACTIVATION_PREFLIGHT_STORAGE_SCAN_LIMIT = 256
ACTIVATION_PREFLIGHT_STORAGE_SESSION_LIMIT = ACTIVATION_PREFLIGHT_STORAGE_SCAN_LIMIT + 1
AUTHZ_ACTIVATION_PREFLIGHT_GITHUB_ID_MAX = 2**63 - 1
_IDENTITY_FINGERPRINT_PURPOSE = "authz-activation-preflight-identity-v1"


class ActivationPreflightStore(Protocol):
    def read_human_sessions_for_github_id_without_cleanup(
        self,
        github_id: int,
        *,
        limit: int,
    ) -> tuple[LaunchplaneHumanSession, ...]: ...


class ActivationPreflightFailure(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _utc_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _whole_hours(value: float) -> int:
    return max(0, math.floor(value / 3600))


def _whole_hours_remaining(value: float) -> int:
    return max(0, math.ceil(value / 3600))


def _identity_fingerprint(identity: GitHubHumanIdentity) -> str:
    payload = json.dumps(
        {
            "github_id": identity.github_id,
            "login": identity.login,
            "organizations": sorted(identity.organizations),
            "teams": sorted(identity.teams),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    try:
        fingerprint = control_plane_secrets.keyed_secret_payload_fingerprint(
            payload,
            purpose=_IDENTITY_FINGERPRINT_PURPOSE,
        )
    except Exception as error:
        raise ActivationPreflightFailure(
            "activation_identity_fingerprint_unavailable",
            "Activation preflight identity fingerprinting is unavailable.",
            status_code=503,
        ) from error
    algorithm, separator, remainder = fingerprint.partition(":")
    key_id, digest_separator, digest = remainder.partition(":")
    if (
        algorithm != "hmac-sha256"
        or not separator
        or not key_id
        or not digest_separator
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ActivationPreflightFailure(
            "activation_identity_fingerprint_unavailable",
            "Activation preflight identity fingerprinting is unavailable.",
            status_code=503,
        )
    return f"identity_{digest}"


def _unmanaged_action_empty_rule_counts(
    policy: LaunchplaneAuthzPolicy,
) -> AuthzUnmanagedActionEmptyRuleCounts:
    rule_collections = (
        ("github_actions", policy.github_actions),
        ("github_humans", policy.github_humans),
        ("terminal_agents", policy.terminal_agents),
        ("local_operators", policy.local_operators),
        ("local_admins", policy.local_admins),
    )
    counts = {
        principal_type: sum(
            not rule.actions and rule.managed_set_id is None and rule.managed_rule_id is None
            for rule in rules
        )
        for principal_type, rules in rule_collections
    }
    return AuthzUnmanagedActionEmptyRuleCounts(total=sum(counts.values()), **counts)


def _resolve_session(
    *,
    sessions: Sequence[LaunchplaneHumanSession],
    github_id: int,
    now: datetime,
) -> tuple[LaunchplaneHumanSession, GitHubHumanIdentity, int, datetime]:
    if len(sessions) > ACTIVATION_PREFLIGHT_STORAGE_SCAN_LIMIT:
        raise ActivationPreflightFailure(
            "activation_session_history_truncated",
            "Activation preflight session history exceeded the bounded scan limit.",
        )
    unexpired_sessions: list[LaunchplaneHumanSession] = []
    for candidate_session in sessions:
        session_github_id = candidate_session.identity.github_id
        if session_github_id != github_id:
            raise ActivationPreflightFailure(
                "activation_session_payload_mismatch",
                "Activation preflight session identity did not match its lookup key.",
            )
        if _utc_timestamp(candidate_session.expires_at) <= now:
            continue
        unexpired_sessions.append(candidate_session)
    if not unexpired_sessions:
        raise ActivationPreflightFailure(
            "activation_session_not_found",
            "No unexpired Launchplane session matched the GitHub ID.",
        )
    current_sessions = sorted(
        (
            candidate_session
            for candidate_session in unexpired_sessions
            if _utc_timestamp(candidate_session.created_at)
            <= now
            < _utc_timestamp(candidate_session.created_at)
            + timedelta(seconds=SESSION_AUTHORIZATION_CLAIMS_TTL_SECONDS)
        ),
        key=lambda item: _utc_timestamp(item.created_at),
        reverse=True,
    )
    if not current_sessions:
        raise ActivationPreflightFailure(
            "activation_session_claims_stale",
            "All matching Launchplane session authorization claims are stale.",
        )
    if len(current_sessions) > AUTHZ_ACTIVATION_PREFLIGHT_MAX_SESSIONS:
        raise ActivationPreflightFailure(
            "activation_session_results_truncated",
            "Activation preflight found too many current sessions.",
        )
    canonical_claims = (
        current_sessions[0].identity.login,
        current_sessions[0].identity.organizations,
        current_sessions[0].identity.teams,
    )
    if any(
        (
            session.identity.login,
            session.identity.organizations,
            session.identity.teams,
        )
        != canonical_claims
        for session in current_sessions[1:]
    ):
        raise ActivationPreflightFailure(
            "activation_session_claims_ambiguous",
            "Current Launchplane session authorization claims are ambiguous.",
        )
    canonical_session = current_sessions[0]
    canonical_identity = GitHubHumanIdentity(
        login=canonical_session.identity.login,
        github_id=github_id,
        name="",
        email="",
        organizations=canonical_session.identity.organizations,
        teams=canonical_session.identity.teams,
        role="read_only",
    )
    latest_expiry = max(_utc_timestamp(session.expires_at) for session in current_sessions)
    return canonical_session, canonical_identity, len(current_sessions), latest_expiry


def build_activation_preflight_response(
    *,
    trace_id: str,
    github_id: int,
    active_record: LaunchplaneAuthzPolicyRecord,
    sessions: Sequence[LaunchplaneHumanSession],
    now: datetime,
) -> AuthzActivationPreflightResponse:
    current_time = _utc_timestamp(now)
    canonical_session, canonical_identity, session_count, latest_expiry = _resolve_session(
        sessions=sessions,
        github_id=github_id,
        now=current_time,
    )
    derived_role = active_record.policy.human_role_for(
        github_id=github_id,
        login=canonical_identity.login,
        organizations=canonical_identity.organizations,
        teams=canonical_identity.teams,
    )
    evaluated_identity = GitHubHumanIdentity(
        login=canonical_identity.login,
        github_id=github_id,
        name="",
        email="",
        organizations=canonical_identity.organizations,
        teams=canonical_identity.teams,
        role=derived_role or "read_only",
    )
    evaluation = active_record.policy.evaluate(
        identity=evaluated_identity,
        action=ACTIVATION_PREFLIGHT_ACTION,
        product=ACTIVATION_PREFLIGHT_PRODUCT,
        context=ACTIVATION_PREFLIGHT_CONTEXT,
        target=AuthorizationTarget(scope="context"),
        record_context=False,
    )
    claims_age = (current_time - _utc_timestamp(canonical_session.created_at)).total_seconds()
    expiry_remaining = (latest_expiry - current_time).total_seconds()
    return AuthzActivationPreflightResponse(
        trace_id=trace_id,
        policy=AuthzActivationPreflightPolicySummary(
            record_id=active_record.record_id,
            revision=active_record.revision,
            policy_sha256=active_record.policy_sha256,
            schema_version=active_record.policy.schema_version,
        ),
        session=AuthzActivationPreflightSessionSummary(
            session_count=session_count,
            claims_current=True,
            claims_age_bucket_hours=_whole_hours(claims_age),
            expiry_bucket_hours=_whole_hours_remaining(expiry_remaining),
            identity_fingerprint=_identity_fingerprint(evaluated_identity),
        ),
        scope=AuthzActivationPreflightScope(),
        evaluation=AuthzActivationPreflightDecision(
            decision=evaluation.decision,
            reason_code=evaluation.reason_code,
        ),
        unmanaged_action_empty_rules=_unmanaged_action_empty_rule_counts(active_record.policy),
    )


def resolve_activation_preflight(
    *,
    store: ActivationPreflightStore,
    active_record: LaunchplaneAuthzPolicyRecord,
    github_id: int,
    now: datetime,
    trace_id: str,
) -> AuthzActivationPreflightResponse:
    if github_id <= 0 or github_id > AUTHZ_ACTIVATION_PREFLIGHT_GITHUB_ID_MAX:
        raise ActivationPreflightFailure(
            "invalid_github_id",
            "Activation preflight requires a bounded positive GitHub ID.",
            status_code=400,
        )
    try:
        sessions = store.read_human_sessions_for_github_id_without_cleanup(
            github_id,
            limit=ACTIVATION_PREFLIGHT_STORAGE_SESSION_LIMIT,
        )
    except ActivationPreflightFailure:
        raise
    except Exception as error:
        raise ActivationPreflightFailure(
            "activation_preflight_storage_unavailable",
            "Activation preflight session storage is unavailable.",
            status_code=503,
        ) from error
    try:
        return build_activation_preflight_response(
            trace_id=trace_id,
            github_id=github_id,
            active_record=active_record,
            sessions=sessions,
            now=now,
        )
    except ActivationPreflightFailure:
        raise
    except Exception as error:
        raise ActivationPreflightFailure(
            "activation_preflight_unavailable",
            "Activation preflight evidence is unavailable.",
            status_code=503,
        ) from error
