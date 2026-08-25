from __future__ import annotations

from datetime import datetime, timezone

from control_plane import secrets as control_plane_secrets
from control_plane.contracts.authz_access_read import (
    AuthzActivationPreflightSelfResponse,
)
from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.service_auth import AuthorizationTarget, GitHubHumanIdentity
from control_plane.service_human_auth import LaunchplaneHumanSession


ACTIVATION_PREFLIGHT_ACTION = "authz_policy_grant.write"
ACTIVATION_PREFLIGHT_PRODUCT = "launchplane"
ACTIVATION_PREFLIGHT_CONTEXT = "launchplane"
ACTIVATION_PREFLIGHT_TARGET = AuthorizationTarget(scope="global")
_POLICY_GENERATION_PURPOSE = "authz-activation-preflight-self-v1"


class ActivationPreflightFailure(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _utc_hour_timestamp(value: datetime) -> str:
    normalized = value.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return normalized.isoformat().replace("+00:00", "Z")


def _policy_generation(record: LaunchplaneAuthzPolicyRecord) -> str:
    payload = "\0".join(
        (
            _POLICY_GENERATION_PURPOSE,
            record.record_id,
            str(record.revision),
            record.policy_sha256,
        )
    )
    try:
        fingerprint = control_plane_secrets.keyed_secret_payload_fingerprint(
            payload,
            purpose=_POLICY_GENERATION_PURPOSE,
        )
    except Exception as error:
        raise ActivationPreflightFailure(
            "activation_policy_generation_unavailable",
            "Activation preflight policy generation is unavailable.",
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
            "activation_policy_generation_unavailable",
            "Activation preflight policy generation is unavailable.",
            status_code=503,
        )
    return digest


def build_activation_preflight_self_response(
    *,
    trace_id: str,
    session: LaunchplaneHumanSession,
    active_record: LaunchplaneAuthzPolicyRecord,
    now: datetime,
) -> AuthzActivationPreflightSelfResponse:
    normalized_now = now.astimezone(timezone.utc)
    if session.identity.github_id <= 0:
        raise ActivationPreflightFailure(
            "activation_preflight_session_invalid",
            "The Launchplane session is invalid.",
            status_code=401,
        )
    if session.expires_at.astimezone(timezone.utc) <= normalized_now:
        raise ActivationPreflightFailure(
            "activation_preflight_session_expired",
            "The Launchplane session is expired.",
            status_code=401,
        )
    if session.created_at.astimezone(timezone.utc) > normalized_now:
        raise ActivationPreflightFailure(
            "activation_preflight_session_invalid",
            "The Launchplane session is invalid.",
            status_code=401,
        )
    if not session.identity.login.strip():
        raise ActivationPreflightFailure(
            "activation_preflight_session_invalid",
            "The Launchplane session is invalid.",
            status_code=401,
        )
    derived_role = active_record.policy.human_role_for(
        github_id=session.identity.github_id,
        login=session.identity.login,
        organizations=session.identity.organizations,
        teams=session.identity.teams,
    )
    evaluated_identity = GitHubHumanIdentity(
        login=session.identity.login,
        github_id=session.identity.github_id,
        name="",
        email="",
        organizations=session.identity.organizations,
        teams=session.identity.teams,
        role=derived_role or "read_only",
    )
    evaluation = active_record.policy.evaluate(
        identity=evaluated_identity,
        action=ACTIVATION_PREFLIGHT_ACTION,
        product=ACTIVATION_PREFLIGHT_PRODUCT,
        context=ACTIVATION_PREFLIGHT_CONTEXT,
        target=ACTIVATION_PREFLIGHT_TARGET,
        record_context=False,
    )
    return AuthzActivationPreflightSelfResponse(
        status="ok",
        trace_id=trace_id,
        decision=evaluation.decision,
        evaluated_at=_utc_hour_timestamp(now),
        policy_generation=_policy_generation(active_record),
    )
