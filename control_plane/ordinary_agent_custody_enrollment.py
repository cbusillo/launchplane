from __future__ import annotations

from dataclasses import dataclass

from pydantic import TypeAdapter

from control_plane import secrets as control_plane_secrets
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.ordinary_agent_enrollment import OrdinaryAgentPolicyBinding
from control_plane.contracts.ordinary_agent_lifecycle import (
    OrdinaryAgentCredentialCustodyCandidate,
    OrdinaryAgentEffectProfile,
    OrdinaryAgentManagedSecretBinding,
    OrdinaryAgentProviderPermission,
    OrdinaryAgentRepositoryInventoryBinding,
)
from control_plane.contracts.repository_inventory import RepositoryInventoryRecord
from control_plane.github_app_identity import (
    GitHubApiRequest,
    GitHubAppIdentity,
    GitHubAppInstallationInspection,
    inspect_ordinary_agent_github_app_installation,
    ordinary_agent_enrollment_effect_profiles,
    ordinary_agent_enrollment_permissions,
)
from control_plane.ordinary_agent_custody import (
    ORDINARY_AGENT_GITHUB_APP_INTEGRATION,
    ORDINARY_AGENT_GITHUB_APP_PRIVATE_KEY_BINDING,
    OrdinaryAgentCustodyError,
    OrdinaryAgentCustodySecretStore,
)
from control_plane.workflows.launchplane import github_api_request


@dataclass(frozen=True, slots=True)
class ProviderInspectedCustodyCandidate:
    candidate: OrdinaryAgentCredentialCustodyCandidate
    installation_id: int
    provider_inspection_sha256: str


def build_provider_inspected_custody_candidate(
    *,
    record_store: OrdinaryAgentCustodySecretStore,
    principal_id: str,
    policy: OrdinaryAgentPolicyBinding,
    repository_inventory: RepositoryInventoryRecord,
    managed_secret: OrdinaryAgentManagedSecretBinding,
    github_app_id: int,
    valid_from: int,
    expires_at: int,
    predecessor_record_id: str | None = None,
    predecessor_sha256: str | None = None,
    api_request: GitHubApiRequest = github_api_request,
) -> ProviderInspectedCustodyCandidate:
    _validate_repository_inventory(policy=policy, inventory=repository_inventory)
    identity = _resolve_enrollment_github_app_identity(
        record_store=record_store,
        managed_secret=managed_secret,
        github_app_id=github_app_id,
    )
    inspection = inspect_ordinary_agent_github_app_installation(
        identity=identity,
        repository=policy.target.repository,
        repository_id=str(policy.target.repository_id),
        repository_owner_id=repository_inventory.repository_owner_id,
        api_request=api_request,
    )
    inspection_sha256 = _provider_inspection_sha256(inspection)
    effect_profiles = TypeAdapter(tuple[OrdinaryAgentEffectProfile, ...]).validate_python(
        ordinary_agent_enrollment_effect_profiles()
    )
    permissions = tuple(
        OrdinaryAgentProviderPermission.model_validate(
            dict(zip(("name", "access"), item.split(":", 1)))
        )
        for item in ordinary_agent_enrollment_permissions()
    )
    if inspection.permissions != ordinary_agent_enrollment_permissions():
        raise RuntimeError("Ordinary-agent enrollment permission contract drifted.")
    candidate = OrdinaryAgentCredentialCustodyCandidate(
        principal_id=principal_id,
        policy=policy,
        repository_inventory=OrdinaryAgentRepositoryInventoryBinding(
            record_id=repository_inventory.record_id,
            inventory_revision=repository_inventory.inventory_revision,
            inventory_digest=repository_inventory.inventory_digest,
        ),
        target=policy.target,
        github_app_id=inspection.app_id,
        github_installation_id=inspection.installation_id,
        managed_secret=managed_secret,
        effect_profiles=effect_profiles,
        permissions=permissions,
        valid_from=valid_from,
        expires_at=expires_at,
        provider_inspection_sha256=inspection_sha256,
        predecessor_record_id=predecessor_record_id,
        predecessor_sha256=predecessor_sha256,
    )
    return ProviderInspectedCustodyCandidate(
        candidate=candidate,
        installation_id=inspection.installation_id,
        provider_inspection_sha256=inspection_sha256,
    )


def _validate_repository_inventory(
    *, policy: OrdinaryAgentPolicyBinding, inventory: RepositoryInventoryRecord
) -> None:
    target = policy.target
    if (
        inventory.inventory_state != "tracked"
        or inventory.repository_id != str(target.repository_id)
        or inventory.repository.casefold() != target.repository.casefold()
    ):
        raise OrdinaryAgentCustodyError(
            "Ordinary-agent provider inspection requires exact tracked repository inventory."
        )


def _resolve_enrollment_github_app_identity(
    *,
    record_store: OrdinaryAgentCustodySecretStore,
    managed_secret: OrdinaryAgentManagedSecretBinding,
    github_app_id: int,
) -> GitHubAppIdentity:
    if (
        managed_secret.integration != ORDINARY_AGENT_GITHUB_APP_INTEGRATION
        or managed_secret.binding_key != ORDINARY_AGENT_GITHUB_APP_PRIVATE_KEY_BINDING
    ):
        raise OrdinaryAgentCustodyError(
            "Ordinary-agent GitHub App managed-secret binding is not exact."
        )
    try:
        record = record_store.read_secret_record(managed_secret.secret_id)
        version = record_store.read_secret_version(managed_secret.secret_version_id)
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
        if binding.binding_id == managed_secret.binding_id
    )
    if len(bindings) != 1:
        raise OrdinaryAgentCustodyError(
            "Ordinary-agent GitHub App requires one exact managed-secret binding."
        )
    binding = bindings[0]
    if (
        record.status != "configured"
        or record.policy != "write_only"
        or record.secret_id != managed_secret.secret_id
        or record.integration != ORDINARY_AGENT_GITHUB_APP_INTEGRATION
        or record.current_version_id != managed_secret.secret_version_id
        or version.version_id != managed_secret.secret_version_id
        or version.secret_id != record.secret_id
        or binding.binding_id != managed_secret.binding_id
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
    return GitHubAppIdentity(app_id=github_app_id, private_key=private_key)


def _provider_inspection_sha256(inspection: GitHubAppInstallationInspection) -> str:
    payload: dict[str, object] = {
        "schema_version": 1,
        "app_id": inspection.app_id,
        "installation_id": inspection.installation_id,
        "repository_id": inspection.repository_id,
        "repository_owner_id": inspection.repository_owner_id,
        "repository": inspection.repository,
        "permissions": inspection.permissions,
    }
    return canonical_json_sha256(payload)
