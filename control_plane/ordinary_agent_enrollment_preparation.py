"""Resolve enrollment scope from current records before any authority transaction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.ordinary_agent_client import OrdinaryAgentEnrollmentClientRequest
from control_plane.contracts.ordinary_agent import OrdinaryAgentTarget
from control_plane.contracts.ordinary_agent_enrollment import (
    OrdinaryAgentPolicyBinding,
    OrdinaryAgentPrincipalPreState,
)
from control_plane.contracts.ordinary_agent_lifecycle import (
    OrdinaryAgentCredentialCustodyCandidate,
    OrdinaryAgentEnrollmentIntent,
    OrdinaryAgentPlannedAuthenticationCredential,
    OrdinaryAgentManagedSecretBinding,
)
from control_plane.github_app_identity import GitHubApiRequest
from control_plane.ordinary_agent_custody import ORDINARY_AGENT_GITHUB_APP_INTEGRATION
from control_plane.ordinary_agent_custody_enrollment import (
    build_provider_inspected_custody_candidate,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.launchplane import github_api_request


@dataclass(frozen=True, slots=True)
class PreparedOrdinaryAgentEnrollmentScope:
    policy: OrdinaryAgentPolicyBinding
    custody: OrdinaryAgentCredentialCustodyCandidate
    principal: OrdinaryAgentPrincipalPreState | None
    credential_id: str | None
    credential_version: int | None


def ordinary_agent_client_enrollment_operation_id(
    *, principal_id: str, request_operation_id: str
) -> str:
    """Scope a client retry key to one principal in the global issuer namespace."""
    return "enrollment-" + canonical_json_sha256(
        {
            "domain": "ordinary-agent-client-enrollment-v1",
            "principal_id": principal_id,
            "request_operation_id": request_operation_id,
        }
    )


def prepare_ordinary_agent_enrollment_scope(
    *,
    store: PostgresRecordStore,
    policy_record: LaunchplaneAuthzPolicyRecord,
    action: Literal["enroll", "rotate_credential"],
    principal_id: str,
    target: OrdinaryAgentTarget,
    github_app_id: int,
    secret_binding_id: str,
    valid_from: int,
    expires_at: int,
    api_request: GitHubApiRequest = github_api_request,
) -> PreparedOrdinaryAgentEnrollmentScope:
    """Resolve explicit selectors; the eventual apply still CAS-checks every binding.

    The caller must authenticate and authorize proposal creation before invoking
    this provider-reading helper. App ID and binding ID are reviewed setup input,
    never a grant. This function neither approves nor persists an operation.
    """
    rules = tuple(
        rule
        for rule in policy_record.policy.ordinary_agents
        if rule.principal_id == principal_id and rule.target == target
    )
    if policy_record.status != "active" or len(rules) != 1:
        raise ValueError("Enrollment requires one exact current ordinary-agent rule.")
    rule = rules[0]
    policy = OrdinaryAgentPolicyBinding(
        record_id=policy_record.record_id,
        revision=policy_record.revision,
        policy_sha256=policy_record.policy_sha256,
        managed_set_id=rule.managed_set_id,
        managed_rule_id=rule.managed_rule_id,
        target=rule.target,
    )
    inventories = store.list_repository_inventory_records(
        repository_id=str(target.repository_id), limit=None
    )
    if not inventories:
        raise ValueError("Enrollment requires current tracked repository inventory.")
    revision = max(record.inventory_revision for record in inventories)
    current_inventories = tuple(
        record for record in inventories if record.inventory_revision == revision
    )
    if len(current_inventories) != 1:
        raise ValueError("Enrollment repository inventory is ambiguous.")
    bindings = tuple(
        binding
        for binding in store.list_secret_bindings(
            integration=ORDINARY_AGENT_GITHUB_APP_INTEGRATION, limit=None
        )
        if binding.binding_id == secret_binding_id
    )
    if len(bindings) != 1:
        raise ValueError("Enrollment requires one exact managed-secret binding.")
    binding = bindings[0]
    secret = store.read_secret_record(binding.secret_id)
    if not secret.current_version_id:
        raise ValueError("Enrollment managed secret has no current version.")
    managed_secret = OrdinaryAgentManagedSecretBinding(
        integration=binding.integration,
        binding_key=binding.binding_key,
        binding_id=binding.binding_id,
        secret_id=secret.secret_id,
        secret_version_id=secret.current_version_id,
    )
    current = store.read_current_ordinary_agent_principal(principal_id=principal_id)
    principal = None
    predecessor_id = None
    predecessor_sha256 = None
    credential_id = None
    credential_version = None
    if action == "enroll":
        if current is not None:
            raise ValueError("Enrollment requires an absent principal.")
    else:
        if current is None or current.status != "active":
            raise ValueError("Rotation requires a current active principal.")
        principal = OrdinaryAgentPrincipalPreState(
            record_id=current.record_id,
            revision=current.principal_revision,
            pre_state_sha256=current.record_sha256,
        )
        credential_id = current.credential_id
        credential_version = current.credential_version
        predecessor_id = current.custody_record_id
        predecessor_sha256 = current.custody_sha256
    inspected = build_provider_inspected_custody_candidate(
        record_store=store,
        principal_id=principal_id,
        policy=policy,
        repository_inventory=current_inventories[0],
        managed_secret=managed_secret,
        github_app_id=github_app_id,
        valid_from=valid_from,
        expires_at=expires_at,
        predecessor_record_id=predecessor_id,
        predecessor_sha256=predecessor_sha256,
        api_request=api_request,
    )
    return PreparedOrdinaryAgentEnrollmentScope(
        policy=policy,
        custody=inspected.candidate,
        principal=principal,
        credential_id=credential_id,
        credential_version=credential_version,
    )


def prepare_ordinary_agent_enrollment_intent(
    *,
    store: PostgresRecordStore,
    policy_record: LaunchplaneAuthzPolicyRecord,
    request: OrdinaryAgentEnrollmentClientRequest,
    now: int,
    api_request: GitHubApiRequest = github_api_request,
) -> OrdinaryAgentEnrollmentIntent:
    """Construct reviewed provenance from real scope, without generating a secret."""
    if not request.credential_valid_from <= now < request.delivery.expires_at <= now + 900:
        raise ValueError("requested delivery window is unavailable")
    scope = prepare_ordinary_agent_enrollment_scope(
        store=store,
        policy_record=policy_record,
        action=request.action,
        principal_id=request.principal_id,
        target=request.target,
        github_app_id=request.github_app_id,
        secret_binding_id=request.secret_binding_id,
        valid_from=request.credential_valid_from,
        expires_at=request.credential_expires_at,
        api_request=api_request,
    )
    request_sha256 = canonical_json_sha256(request.model_dump(mode="json"))
    evidence_sha256 = canonical_json_sha256(scope.custody.model_dump(mode="json"))
    credential_id = (
        scope.credential_id
        or "credential_"
        + canonical_json_sha256(
            {
                "domain": "ordinary-agent-client-credential-v1",
                "principal_id": request.principal_id,
                "operation_id": request.operation_id,
            }
        )[:48]
    )
    return OrdinaryAgentEnrollmentIntent(
        action=request.action,
        operation_id=ordinary_agent_client_enrollment_operation_id(
            principal_id=request.principal_id, request_operation_id=request.operation_id
        ),
        principal_id=request.principal_id,
        request_sha256=request_sha256,
        evidence_sha256=evidence_sha256,
        plan_sha256=canonical_json_sha256({"request": request_sha256, "evidence": evidence_sha256}),
        policy=scope.policy,
        principal=scope.principal,
        credential_id=scope.credential_id,
        credential_version=scope.credential_version,
        authentication_credential=OrdinaryAgentPlannedAuthenticationCredential(
            credential_id=credential_id,
            valid_from=request.credential_valid_from,
            expires_at=request.credential_expires_at,
        ),
        delivery=request.delivery,
        custody=scope.custody,
        session_attenuation=request.session_attenuation,
    )
