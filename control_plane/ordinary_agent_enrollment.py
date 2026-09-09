from __future__ import annotations

from collections.abc import Mapping
from pydantic import TypeAdapter

from control_plane.contracts.ordinary_agent import (
    OrdinaryAgentAction,
    OrdinaryAgentPolicyRule,
    PrincipalProfile,
)
from control_plane.contracts.ordinary_agent_enrollment import (
    DormantOrdinaryAgentEnrollmentResult,
    EnrollmentDiagnosticCode,
    OrdinaryAgentEnrollRequest,
    OrdinaryAgentEnrollmentRequest,
    OrdinaryAgentEnrollmentReview,
    OrdinaryAgentRevokePrincipalRequest,
    OrdinaryAgentRotateCredentialRequest,
)


_REQUEST_ADAPTER: TypeAdapter[OrdinaryAgentEnrollmentRequest] = TypeAdapter(
    OrdinaryAgentEnrollmentRequest
)
_ACTION_MINIMUM_PROFILE: dict[OrdinaryAgentAction, PrincipalProfile] = {
    "self_read": "read_only",
    "preflight": "read_only",
    "guarded_merge": "guarded_executor",
}


def parse_ordinary_agent_enrollment_request(
    payload: bytes | str | Mapping[str, object],
) -> OrdinaryAgentEnrollmentRequest:
    if isinstance(payload, bytes | str):
        return _REQUEST_ADAPTER.validate_json(payload)
    return _REQUEST_ADAPTER.validate_python(payload, strict=True)


def derive_ordinary_agent_execution_profile(
    actions: tuple[OrdinaryAgentAction, ...],
) -> PrincipalProfile:
    if not actions:
        raise ValueError("ordinary-agent policy rule requires at least one action")
    minimum_profiles: set[PrincipalProfile] = set()
    for action in actions:
        try:
            minimum_profiles.add(_ACTION_MINIMUM_PROFILE[action])
        except KeyError as error:
            raise ValueError(
                "ordinary-agent action has no execution-profile classification"
            ) from error
    return "guarded_executor" if "guarded_executor" in minimum_profiles else "read_only"


def build_ordinary_agent_enrollment_review(
    *,
    request: OrdinaryAgentEnrollmentRequest,
    bound_rule: OrdinaryAgentPolicyRule | None = None,
) -> OrdinaryAgentEnrollmentReview:
    if isinstance(request, OrdinaryAgentRevokePrincipalRequest):
        if bound_rule is not None:
            raise ValueError("principal revocation review does not accept an active policy rule")
        return OrdinaryAgentEnrollmentReview(
            action=request.action,
            principal_id=request.principal_id,
            principal=request.principal,
            credential_custody="not_applicable",
        )
    if bound_rule is None:
        raise ValueError("enroll and rotate review require one exact ordinary-agent policy rule")
    if (
        bound_rule.principal_id != request.principal_id
        or bound_rule.managed_set_id != request.policy.managed_set_id
        or bound_rule.managed_rule_id != request.policy.managed_rule_id
        or bound_rule.target != request.policy.target
    ):
        raise ValueError("ordinary-agent enrollment request does not match its bound policy rule")
    derived_profile = derive_ordinary_agent_execution_profile(bound_rule.actions)
    if isinstance(request, OrdinaryAgentEnrollRequest):
        return OrdinaryAgentEnrollmentReview(
            action=request.action,
            principal_id=request.principal_id,
            policy=request.policy,
            expected_principal_absent=True,
            derived_execution_profile=derived_profile,
            credential_custody="unavailable",
        )
    if isinstance(request, OrdinaryAgentRotateCredentialRequest):
        return OrdinaryAgentEnrollmentReview(
            action=request.action,
            principal_id=request.principal_id,
            policy=request.policy,
            principal=request.principal,
            credential_id=request.credential_id,
            credential_version=request.credential_version,
            derived_execution_profile=derived_profile,
            credential_custody="unavailable",
        )
    raise TypeError("ordinary-agent enrollment request variant is unsupported")


def dormant_ordinary_agent_enrollment(
    request: OrdinaryAgentEnrollmentRequest,
) -> DormantOrdinaryAgentEnrollmentResult:
    diagnostic_codes: tuple[EnrollmentDiagnosticCode, ...] = (
        ()
        if isinstance(request, OrdinaryAgentRevokePrincipalRequest)
        else ("credential_custody_unavailable",)
    )
    return DormantOrdinaryAgentEnrollmentResult(
        action=request.action,
        principal_id=request.principal_id,
        diagnostic_codes=diagnostic_codes,
    )
