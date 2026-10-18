"""Reauthorize one held native operation without changing any effect fence."""

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.durable_operation_authorization import (
    DurableOperationAuthorization,
    DurableOperationCallerIdentity,
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.durable_operation_authorization import DurableOperationAuthorizationDeniedError

RESUME_SCOPE = "launchplane:testing-rebuild-resume:v1"
RESUME_ROUTE = "service:testing-rebuild-resume"


class TestingRebuildResumeRefused(ValueError):
    """The original operation is not at the held revision being resumed."""


def caller_binding(caller: DurableOperationCallerIdentity) -> tuple[object, ...]:
    if caller.identity_type == "github_human":
        return caller.identity_type, caller.github_id
    if caller.identity_type in {"terminal_agent", "local_operator", "local_admin"}:
        return caller.identity_type, caller.subject, caller.token_label
    raise DurableOperationAuthorizationDeniedError(
        code="authorization_denied",
        message="A workflow or service identity cannot substitute for this request's caller.",
    )


def require_resume_authorization(
    operation: OdooStableTargetReplacementOperationRecord,
    authorization: DurableOperationAuthorization,
) -> None:
    previous = operation.authorization
    if (
        previous is None
        or (
            authorization.action,
            authorization.product,
            authorization.context,
            authorization.instances,
        )
        != (
            previous.action,
            operation.product,
            operation.context,
            ("testing",),
        )
        or caller_binding(previous.caller) != caller_binding(authorization.caller)
    ):
        raise DurableOperationAuthorizationDeniedError(
            code="authorization_denied",
            message="Resume requires fresh authority from the original caller for this testing request.",
        )


def resume_key(
    operation: OdooStableTargetReplacementOperationRecord,
    *,
    expected_updated_at: str,
    expected_attempt: int,
) -> str:
    return canonical_json_sha256(
        {
            "operation": operation.operation_id,
            "product": operation.product,
            "context": operation.context,
            "scope": operation.idempotency_scope,
            "key": operation.idempotency_key,
            "request": operation.request_fingerprint,
            "held_updated_at": expected_updated_at,
            "held_attempt": expected_attempt,
        }
    )
