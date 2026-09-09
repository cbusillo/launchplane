from __future__ import annotations

from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from control_plane.contracts.ordinary_agent import OrdinaryAgentPolicyRule, OrdinaryAgentTarget
from control_plane.contracts.ordinary_agent_enrollment import (
    OrdinaryAgentPolicyBinding,
    OrdinaryAgentPrincipalPreState,
)
from control_plane.contracts.ordinary_agent_lifecycle import (
    ORDINARY_AGENT_ENROLLMENT_MUTATION_ROUTE,
    ORDINARY_AGENT_ENROLLMENT_MUTATION_SCOPE,
    OrdinaryAgentAdministratorAuthorizationBinding,
    OrdinaryAgentAuthenticationCredentialCandidate,
    OrdinaryAgentCredentialCustodyCandidate,
    OrdinaryAgentEnrollApplyEnvelope,
    OrdinaryAgentManagedSecretBinding,
    OrdinaryAgentProviderPermission,
    OrdinaryAgentRepositoryInventoryBinding,
    OrdinaryAgentRevokePrincipalApplyEnvelope,
    OrdinaryAgentRotateCredentialApplyEnvelope,
    ordinary_agent_enrollment_envelope_sha256,
)
from control_plane.contracts.repository_inventory import RepositoryInventoryRecord
from control_plane.contracts.secret_record import SecretBinding, SecretRecord, SecretVersion
from control_plane.service_auth import GitHubHumanPolicyRule, LaunchplaneAuthzPolicy
from control_plane.storage.postgres import (
    DbOnlyMutationRequest,
    LaunchplaneAuthzPolicyRow,
    PostgresRecordStore,
)


TARGET = OrdinaryAgentTarget(
    repository_id=911001,
    repository="example/postgres-inventory",
    base_branch="main",
)
ADMIN_GITHUB_ID = 903001


def ordinary_agent_policy_record() -> LaunchplaneAuthzPolicyRecord:
    policy = LaunchplaneAuthzPolicy(
        schema_version=3,
        github_humans=(
            GitHubHumanPolicyRule(
                managed_set_id="ordinary-agent.administrators",
                managed_rule_id="owner",
                github_ids=(ADMIN_GITHUB_ID,),
                roles=("admin",),
                products=("launchplane",),
                contexts=("launchplane",),
                actions=("authz_policy_grant.write",),
            ),
        ),
        ordinary_agents=(
            OrdinaryAgentPolicyRule(
                managed_set_id="ordinary-agent.pilot",
                managed_rule_id="agent_one.launchplane.main",
                principal_id="agent_one",
                target=TARGET,
                actions=("self_read", "preflight", "guarded_merge"),
            ),
        ),
    )
    digest = authz_policy_sha256(policy)
    return LaunchplaneAuthzPolicyRecord(
        record_id=build_authz_policy_record_id(revision=1, policy_sha256=digest),
        revision=1,
        source="test:ordinary-agent-lifecycle",
        updated_at="2026-09-08T00:00:00Z",
        policy_sha256=digest,
        policy=policy,
    )


def setup_ordinary_agent_authority(
    store: PostgresRecordStore,
) -> tuple[LaunchplaneAuthzPolicyRecord, RepositoryInventoryRecord]:
    policy_record = ordinary_agent_policy_record()
    store._write_row(store._authz_policy_row(policy_record))
    inventory = RepositoryInventoryRecord.model_validate(
        {
            "repository_id": str(TARGET.repository_id),
            "repository_owner_id": "912001",
            "repository": TARGET.repository,
            "inventory_state": "tracked",
            "inventory_revision": 1,
            "recorded_at": "2026-09-08T00:00:00Z",
            "source": "test:ordinary-agent-lifecycle",
            "reason": "exercise exact enrollment authority",
        }
    )
    store.write_repository_inventory_record(inventory)
    store.write_secret_version(
        SecretVersion(
            version_id="ordinary-agent-app-key-v1",
            secret_id="ordinary-agent-app-key",
            created_at="2026-09-08T00:00:00Z",
            created_by="test",
            ciphertext="encrypted-test-placeholder",
        )
    )
    store.write_secret_record(
        SecretRecord(
            secret_id="ordinary-agent-app-key",
            scope="global",
            integration="github-ordinary-agent-app",
            name="ordinary-agent-app-key",
            current_version_id="ordinary-agent-app-key-v1",
            created_at="2026-09-08T00:00:00Z",
            updated_at="2026-09-08T00:00:00Z",
            updated_by="test",
        )
    )
    store.write_secret_binding(
        SecretBinding(
            binding_id="ordinary-agent-app-key-binding",
            secret_id="ordinary-agent-app-key",
            integration="github-ordinary-agent-app",
            binding_key="private-key",
            status="configured",
            created_at="2026-09-08T00:00:00Z",
            updated_at="2026-09-08T00:00:00Z",
        )
    )
    return policy_record, inventory


def replace_policy_without_ordinary_agent_rule(
    store: PostgresRecordStore,
    *,
    current: LaunchplaneAuthzPolicyRecord,
) -> LaunchplaneAuthzPolicyRecord:
    policy = current.policy.model_copy(update={"ordinary_agents": ()})
    digest = authz_policy_sha256(policy)
    replacement = LaunchplaneAuthzPolicyRecord(
        record_id=build_authz_policy_record_id(
            revision=current.revision + 1,
            policy_sha256=digest,
        ),
        revision=current.revision + 1,
        source="test:ordinary-agent-lifecycle-rule-removed",
        updated_at="2026-09-08T00:01:00Z",
        policy_sha256=digest,
        policy=policy,
    )
    with store._session_factory() as session:
        current_row = session.get(LaunchplaneAuthzPolicyRow, current.record_id)
        assert current_row is not None
        superseded = current.model_copy(update={"status": "superseded"})
        current_row.status = superseded.status
        current_row.payload = store._authz_policy_payload(superseded)
        session.add(store._authz_policy_row(replacement))
        session.commit()
    return replacement


def enrollment_envelope(
    *,
    policy_record: LaunchplaneAuthzPolicyRecord,
    inventory: RepositoryInventoryRecord,
    operation_id: str = "ordinary-agent-enroll-1",
    credential_digest: str = "a" * 64,
) -> OrdinaryAgentEnrollApplyEnvelope:
    policy = OrdinaryAgentPolicyBinding(
        record_id=policy_record.record_id,
        revision=policy_record.revision,
        policy_sha256=policy_record.policy_sha256,
        managed_set_id="ordinary-agent.pilot",
        managed_rule_id="agent_one.launchplane.main",
        target=TARGET,
    )
    return OrdinaryAgentEnrollApplyEnvelope(
        action="enroll",
        operation_id=operation_id,
        principal_id="agent_one",
        request_sha256="1" * 64,
        approval_sha256="2" * 64,
        evidence_sha256="3" * 64,
        plan_sha256="4" * 64,
        administrator=OrdinaryAgentAdministratorAuthorizationBinding(
            policy_record_id=policy_record.record_id,
            policy_revision=policy_record.revision,
            policy_schema_version=policy_record.policy.schema_version,
            policy_sha256=policy_record.policy_sha256,
            policy_source=policy_record.source,
            managed_set_id="ordinary-agent.administrators",
            managed_rule_id="owner",
            administrator_github_id=ADMIN_GITHUB_ID,
        ),
        policy=policy,
        expected_principal_absent=True,
        authentication_credential=OrdinaryAgentAuthenticationCredentialCandidate(
            principal_id="agent_one",
            credential_id="agent_one_auth",
            credential_digest=credential_digest,
            valid_from=1_700_000_000,
            expires_at=2_000_000_000,
            issuance_evidence_sha256="5" * 64,
        ),
        custody=OrdinaryAgentCredentialCustodyCandidate(
            principal_id="agent_one",
            policy=policy,
            repository_inventory=OrdinaryAgentRepositoryInventoryBinding(
                record_id=inventory.record_id,
                inventory_revision=inventory.inventory_revision,
                inventory_digest=inventory.inventory_digest,
            ),
            target=TARGET,
            github_app_id=123456,
            managed_secret=OrdinaryAgentManagedSecretBinding(
                binding_id="ordinary-agent-app-key-binding",
                secret_id="ordinary-agent-app-key",
                secret_version_id="ordinary-agent-app-key-v1",
                integration="github-ordinary-agent-app",
                binding_key="private-key",
            ),
            effect_profiles=("guarded_merge",),
            permissions=(
                OrdinaryAgentProviderPermission(name="metadata", access="read"),
                OrdinaryAgentProviderPermission(name="contents", access="write"),
            ),
            valid_from=1_700_000_000,
            expires_at=2_000_000_000,
            provider_inspection_sha256="6" * 64,
        ),
    )


def rotation_envelope(
    *,
    enrolled: OrdinaryAgentEnrollApplyEnvelope,
    principal_record_id: str,
    principal_revision: int,
    principal_sha256: str,
    custody_record_id: str,
    custody_sha256: str,
) -> OrdinaryAgentRotateCredentialApplyEnvelope:
    return OrdinaryAgentRotateCredentialApplyEnvelope(
        **{
            **enrolled.model_dump(mode="json", exclude={"action", "expected_principal_absent"}),
            "action": "rotate_credential",
            "operation_id": "ordinary-agent-rotate-1",
            "principal": OrdinaryAgentPrincipalPreState(
                record_id=principal_record_id,
                revision=principal_revision,
                pre_state_sha256=principal_sha256,
            ),
            "credential_id": enrolled.authentication_credential.credential_id,
            "credential_version": 1,
            "authentication_credential": enrolled.authentication_credential.model_copy(
                update={
                    "credential_digest": "b" * 64,
                    "issuance_evidence_sha256": "7" * 64,
                }
            ),
            "custody": enrolled.custody.model_copy(
                update={
                    "provider_inspection_sha256": "8" * 64,
                    "predecessor_record_id": custody_record_id,
                    "predecessor_sha256": custody_sha256,
                }
            ),
        }
    )


def revocation_envelope(
    *,
    enrolled: OrdinaryAgentEnrollApplyEnvelope,
    principal_record_id: str,
    principal_revision: int,
    principal_sha256: str,
) -> OrdinaryAgentRevokePrincipalApplyEnvelope:
    return OrdinaryAgentRevokePrincipalApplyEnvelope(
        **{
            **enrolled.model_dump(
                mode="json",
                include={
                    "schema_version",
                    "principal_id",
                    "request_sha256",
                    "approval_sha256",
                    "evidence_sha256",
                    "plan_sha256",
                    "administrator",
                },
            ),
            "action": "revoke_principal",
            "operation_id": "ordinary-agent-revoke-1",
            "principal": OrdinaryAgentPrincipalPreState(
                record_id=principal_record_id,
                revision=principal_revision,
                pre_state_sha256=principal_sha256,
            ),
        }
    )


def enrollment_mutation(envelope: object) -> DbOnlyMutationRequest:
    typed_envelope = envelope
    assert isinstance(
        typed_envelope,
        (
            OrdinaryAgentEnrollApplyEnvelope,
            OrdinaryAgentRotateCredentialApplyEnvelope,
            OrdinaryAgentRevokePrincipalApplyEnvelope,
        ),
    )
    return DbOnlyMutationRequest(
        scope=ORDINARY_AGENT_ENROLLMENT_MUTATION_SCOPE,
        route_path=ORDINARY_AGENT_ENROLLMENT_MUTATION_ROUTE,
        idempotency_key=typed_envelope.operation_id,
        request_fingerprint=ordinary_agent_enrollment_envelope_sha256(typed_envelope),
        lease_owner=f"test:{typed_envelope.operation_id}",
        response_status_code=200,
        response_trace_id=f"trace:{typed_envelope.operation_id}",
        response_payload={},
    )
