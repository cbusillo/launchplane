"""Read bounded configuration inputs for ordinary-agent authorization planning."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from typing import Protocol, cast

from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.merge_train_policy import MergeTrainPolicyRecord
from control_plane.contracts.ordinary_agent_delivery_authorization_inputs import (
    AuthorizationCandidateInputDiagnostic,
    AuthorizationCandidateMergePolicyProvenance,
    AuthorizationCandidatePolicyProvenance,
    InspectionSetupManagedSecretMetadata,
    InspectionSetupMetadata,
    InspectionSetupRuntimeMetadata,
    InspectionSetupState,
    MergePolicyProjectionState,
    OrdinaryAgentDeliveryAuthorizationCandidateInputsResponse,
    OrdinaryAgentDeliveryAuthorizationCandidateRepository,
    RepositoryInventoryProjectionState,
    TerminalEnrollmentCapabilityReadiness,
)
from control_plane.contracts.repository_inventory import (
    RepositoryInventoryRecord,
    normalize_repository,
    normalize_utc_timestamp,
)
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.secret_record import SecretBinding, SecretRecord
from control_plane.provider_delivery_inspection_profile import (
    PROVIDER_DELIVERY_INSPECTION_APP_ID_ENV_KEY,
    PROVIDER_DELIVERY_INSPECTION_CONTEXT,
    is_exact_provider_delivery_inspection_secret_binding,
    is_exact_provider_delivery_inspection_secret_record,
    provider_delivery_inspection_positive_app_id,
)
from control_plane.authz_candidate_preparation import terminal_enrollment_capability_state
from control_plane.service_auth import TerminalAgentIdentity


# This bounds historical rows, not repository count. Reaching it must report
# incomplete evidence; a latest-per-repository storage read can replace the scan.
MAX_AUTHORIZATION_INPUT_SOURCE_RECORDS = 1000
MAX_AUTHORIZATION_INPUT_MERGE_TARGETS = 1000


class OrdinaryAgentDeliveryAuthorizationInputStore(Protocol):
    def list_repository_inventory_records(
        self, *, repository_id: str = "", limit: int | None = None
    ) -> tuple[RepositoryInventoryRecord, ...]: ...

    def list_merge_train_policy_records(
        self, *, status: str = "", limit: int | None = None
    ) -> tuple[MergeTrainPolicyRecord, ...]: ...

    def list_provider_delivery_inspection_setup_runtime_records(
        self,
    ) -> tuple[RuntimeEnvironmentRecord, ...]: ...

    def list_provider_delivery_inspection_setup_secret_records(
        self,
    ) -> tuple[SecretRecord, ...]: ...

    def list_provider_delivery_inspection_setup_secret_bindings(
        self,
    ) -> tuple[SecretBinding, ...]: ...


def read_ordinary_agent_delivery_authorization_candidate_inputs(
    *,
    record_store: object,
    policy_record: LaunchplaneAuthzPolicyRecord,
    trace_id: str,
    observed_at: str,
    configured_terminal_identity: TerminalAgentIdentity | None = None,
) -> OrdinaryAgentDeliveryAuthorizationCandidateInputsResponse:
    inventory_state, current_inventory, inventory_diagnostics = _read_current_inventory(
        record_store
    )
    (
        merge_policy_state,
        merge_policy,
        branches_by_repository,
        merge_diagnostics,
    ) = _read_merge_policy(record_store)
    inspection_setup = _read_inspection_setup(record_store)
    repositories = tuple(
        OrdinaryAgentDeliveryAuthorizationCandidateRepository(
            record_id=record.record_id,
            repository_id=record.repository_id,
            repository=record.repository,
            inventory_revision=record.inventory_revision,
            inventory_sha256=record.inventory_digest,
            recorded_at=record.recorded_at,
            configured_branches=branches_by_repository.get(record.repository, ()),
        )
        for record in current_inventory
    )
    branch_diagnostics: tuple[AuthorizationCandidateInputDiagnostic, ...] = ()
    if merge_policy_state == "available" and any(
        not repository.configured_branches for repository in repositories
    ):
        branch_diagnostics = (
            _diagnostic(
                "configured_branches_missing",
                "At least one tracked repository has no configured merge policy branch.",
            ),
        )
    return OrdinaryAgentDeliveryAuthorizationCandidateInputsResponse(
        trace_id=trace_id,
        observed_at=observed_at,
        authorization_policy=AuthorizationCandidatePolicyProvenance(
            record_id=policy_record.record_id,
            revision=policy_record.revision,
            schema_version=policy_record.policy.schema_version,
            policy_sha256=policy_record.policy_sha256,
        ),
        inventory_state=inventory_state,
        merge_policy_state=merge_policy_state,
        merge_policy=merge_policy,
        repositories=repositories,
        diagnostics=(
            inventory_diagnostics
            + merge_diagnostics
            + branch_diagnostics
            + (
                _diagnostic(
                    "principal_state_not_inspected",
                    "Principal state is not inspected by this configuration read.",
                ),
            )
        ),
        inspection_setup=inspection_setup,
        terminal_enrollment=TerminalEnrollmentCapabilityReadiness(
            state=terminal_enrollment_capability_state(
                policy=policy_record.policy,
                identity=configured_terminal_identity,
            )
        ),
    )


def _read_inspection_setup(record_store: object) -> InspectionSetupMetadata:
    runtime_reader = getattr(
        record_store, "list_provider_delivery_inspection_setup_runtime_records", None
    )
    secret_reader = getattr(
        record_store, "list_provider_delivery_inspection_setup_secret_records", None
    )
    binding_reader = getattr(
        record_store, "list_provider_delivery_inspection_setup_secret_bindings", None
    )
    if not all(callable(reader) for reader in (runtime_reader, secret_reader, binding_reader)):
        return InspectionSetupMetadata()
    runtime = _read_inspection_runtime(cast(Callable[[], tuple[object, ...]], runtime_reader))
    managed_secret = _read_inspection_managed_secret(
        secret_reader=cast(Callable[[], tuple[object, ...]], secret_reader),
        binding_reader=cast(Callable[[], tuple[object, ...]], binding_reader),
    )
    unavailable_states = {
        "unavailable",
        "record_unreadable",
        "secret_unreadable",
        "binding_unreadable",
    }
    if runtime.state in unavailable_states or managed_secret.state in unavailable_states:
        state: InspectionSetupState = "unavailable"
    elif runtime.state == "metadata_recorded" and managed_secret.state == "metadata_recorded":
        state = "metadata_recorded"
    else:
        state = "incomplete"
    return InspectionSetupMetadata(
        state=state,
        runtime=runtime,
        managed_secret=managed_secret,
    )


def _read_inspection_runtime(
    reader: Callable[[], tuple[object, ...]],
) -> InspectionSetupRuntimeMetadata:
    try:
        raw_records = tuple(reader())
    except ValueError:
        return InspectionSetupRuntimeMetadata(state="record_unreadable")
    except Exception:
        return InspectionSetupRuntimeMetadata(state="unavailable")
    if not raw_records:
        return InspectionSetupRuntimeMetadata(state="record_missing")
    if len(raw_records) != 1:
        return InspectionSetupRuntimeMetadata(state="record_ambiguous")
    try:
        record = RuntimeEnvironmentRecord.model_validate(raw_records[0])
    except (TypeError, ValueError):
        return InspectionSetupRuntimeMetadata(state="record_unreadable")
    if (
        record.scope != "context"
        or record.context != PROVIDER_DELIVERY_INSPECTION_CONTEXT
        or record.instance
    ):
        return InspectionSetupRuntimeMetadata(state="record_unreadable")
    if PROVIDER_DELIVERY_INSPECTION_APP_ID_ENV_KEY not in record.env:
        return InspectionSetupRuntimeMetadata(state="app_id_missing")
    try:
        app_id = provider_delivery_inspection_positive_app_id(
            record.env[PROVIDER_DELIVERY_INSPECTION_APP_ID_ENV_KEY]
        )
    except ValueError:
        return InspectionSetupRuntimeMetadata(state="app_id_invalid")
    try:
        recorded_at = normalize_utc_timestamp(record.updated_at, "inspection runtime recorded_at")
    except ValueError:
        return InspectionSetupRuntimeMetadata(state="record_unreadable")
    return InspectionSetupRuntimeMetadata(
        state="metadata_recorded",
        app_id=str(app_id),
        recorded_at=recorded_at,
    )


def _read_inspection_managed_secret(
    *,
    secret_reader: Callable[[], tuple[object, ...]],
    binding_reader: Callable[[], tuple[object, ...]],
) -> InspectionSetupManagedSecretMetadata:
    try:
        raw_secrets = tuple(secret_reader())
    except ValueError:
        return InspectionSetupManagedSecretMetadata(state="secret_unreadable")
    except Exception:
        return InspectionSetupManagedSecretMetadata(state="unavailable")
    try:
        raw_bindings = tuple(binding_reader())
    except ValueError:
        return InspectionSetupManagedSecretMetadata(state="binding_unreadable")
    except Exception:
        return InspectionSetupManagedSecretMetadata(state="unavailable")
    if not raw_secrets:
        return InspectionSetupManagedSecretMetadata(state="secret_missing")
    if len(raw_secrets) != 1:
        return InspectionSetupManagedSecretMetadata(state="secret_ambiguous")
    if not raw_bindings:
        return InspectionSetupManagedSecretMetadata(state="binding_missing")
    if len(raw_bindings) != 1:
        return InspectionSetupManagedSecretMetadata(state="binding_ambiguous")
    raw_current_version_id = getattr(raw_secrets[0], "current_version_id", None)
    if isinstance(raw_current_version_id, str) and not raw_current_version_id.strip():
        return InspectionSetupManagedSecretMetadata(state="version_pointer_missing")
    try:
        secret = SecretRecord.model_validate(raw_secrets[0])
    except (TypeError, ValueError):
        return InspectionSetupManagedSecretMetadata(state="secret_unreadable")
    try:
        binding = SecretBinding.model_validate(raw_bindings[0])
    except (TypeError, ValueError):
        return InspectionSetupManagedSecretMetadata(state="binding_unreadable")
    if not is_exact_provider_delivery_inspection_secret_record(secret):
        return InspectionSetupManagedSecretMetadata(state="secret_unreadable")
    if not is_exact_provider_delivery_inspection_secret_binding(binding):
        return InspectionSetupManagedSecretMetadata(state="binding_unreadable")
    if binding.secret_id != secret.secret_id:
        return InspectionSetupManagedSecretMetadata(state="binding_mismatch")
    return InspectionSetupManagedSecretMetadata(
        state="metadata_recorded",
        secret_id=secret.secret_id,
        binding_id=binding.binding_id,
        current_version_id=secret.current_version_id,
    )


def _read_current_inventory(
    record_store: object,
) -> tuple[
    RepositoryInventoryProjectionState,
    tuple[RepositoryInventoryRecord, ...],
    tuple[AuthorizationCandidateInputDiagnostic, ...],
]:
    reader = getattr(record_store, "list_repository_inventory_records", None)
    if not callable(reader):
        return (
            "unavailable",
            (),
            (_diagnostic("inventory_unavailable", "Repository inventory is unavailable."),),
        )
    try:
        raw_records = tuple(
            cast(Callable[..., tuple[object, ...]], reader)(
                limit=MAX_AUTHORIZATION_INPUT_SOURCE_RECORDS + 1
            )
        )
    except Exception:
        return (
            "unavailable",
            (),
            (_diagnostic("inventory_unavailable", "Repository inventory is unavailable."),),
        )
    try:
        records = tuple(RepositoryInventoryRecord.model_validate(record) for record in raw_records)
    except (TypeError, ValueError):
        return (
            "unavailable",
            (),
            (_diagnostic("inventory_unavailable", "Repository inventory is unavailable."),),
        )
    if len(records) > MAX_AUTHORIZATION_INPUT_SOURCE_RECORDS:
        return (
            "truncated",
            (),
            (
                _diagnostic(
                    "inventory_truncated",
                    "Repository inventory exceeds the bounded configuration read.",
                ),
            ),
        )

    records_by_id: dict[str, list[RepositoryInventoryRecord]] = defaultdict(list)
    for record in records:
        records_by_id[record.repository_id].append(record)
    ambiguous_ids: set[str] = set()
    current_records: list[RepositoryInventoryRecord] = []
    for repository_id, repository_records in records_by_id.items():
        highest_revision = max(record.inventory_revision for record in repository_records)
        highest_records = tuple(
            record for record in repository_records if record.inventory_revision == highest_revision
        )
        if len(highest_records) != 1:
            ambiguous_ids.add(repository_id)
            continue
        current_records.append(highest_records[0])

    tracked_records = [
        record
        for record in current_records
        if record.repository_id not in ambiguous_ids and record.inventory_state == "tracked"
    ]
    tracked_ids_by_repository: dict[str, set[str]] = defaultdict(set)
    for record in tracked_records:
        tracked_ids_by_repository[record.repository].add(record.repository_id)
    ambiguous_repositories = {
        repository
        for repository, repository_ids in tracked_ids_by_repository.items()
        if len(repository_ids) > 1
    }
    selected_records = tuple(
        sorted(
            (
                record
                for record in tracked_records
                if record.repository not in ambiguous_repositories
            ),
            key=lambda record: (record.repository, record.repository_id),
        )
    )
    if ambiguous_ids or ambiguous_repositories:
        return (
            "ambiguous",
            selected_records,
            (
                _diagnostic(
                    "inventory_ambiguous",
                    "Repository inventory has an ambiguous current configuration.",
                ),
            ),
        )
    if not selected_records:
        return (
            "complete",
            (),
            (
                _diagnostic(
                    "tracked_inventory_missing",
                    "No current tracked repository inventory configuration was found.",
                ),
            ),
        )
    return "complete", selected_records, ()


def _read_merge_policy(
    record_store: object,
) -> tuple[
    MergePolicyProjectionState,
    AuthorizationCandidateMergePolicyProvenance | None,
    dict[str, tuple[str, ...]],
    tuple[AuthorizationCandidateInputDiagnostic, ...],
]:
    reader = getattr(record_store, "list_merge_train_policy_records", None)
    if not callable(reader):
        return _merge_policy_failure(
            "unavailable", "merge_policy_unavailable", "Merge policy configuration is unavailable."
        )
    try:
        active_records = tuple(
            cast(Callable[..., tuple[object, ...]], reader)(status="active", limit=2)
        )
    except Exception:
        return _merge_policy_failure(
            "unavailable", "merge_policy_unavailable", "Merge policy configuration is unavailable."
        )
    if not active_records:
        return _merge_policy_failure(
            "missing", "merge_policy_missing", "No active merge policy configuration was found."
        )
    if len(active_records) != 1:
        return _merge_policy_failure(
            "ambiguous",
            "merge_policy_ambiguous",
            "Active merge policy configuration is ambiguous.",
        )
    raw_record = active_records[0]
    try:
        raw_policies = tuple(getattr(getattr(raw_record, "policy"), "policies"))
    except (AttributeError, TypeError):
        return _merge_policy_failure(
            "unavailable", "merge_policy_unavailable", "Merge policy configuration is unavailable."
        )
    if len(raw_policies) > MAX_AUTHORIZATION_INPUT_MERGE_TARGETS:
        return _merge_policy_failure(
            "truncated",
            "merge_policy_truncated",
            "Merge policy targets exceed the bounded configuration read.",
        )
    try:
        policy_keys = tuple(
            (
                normalize_repository(str(getattr(policy, "repository")), "repository"),
                str(getattr(policy, "base_branch")).strip(),
            )
            for policy in raw_policies
        )
    except (AttributeError, TypeError, ValueError):
        return _merge_policy_failure(
            "unavailable", "merge_policy_unavailable", "Merge policy configuration is unavailable."
        )
    if len(policy_keys) != len(set(policy_keys)):
        return _merge_policy_failure(
            "ambiguous",
            "merge_policy_ambiguous",
            "Active merge policy configuration is ambiguous.",
        )
    try:
        record = MergeTrainPolicyRecord.model_validate(raw_record)
    except (TypeError, ValueError):
        return _merge_policy_failure(
            "unavailable", "merge_policy_unavailable", "Merge policy configuration is unavailable."
        )
    if record.status != "active":
        return _merge_policy_failure(
            "unavailable", "merge_policy_unavailable", "Merge policy configuration is unavailable."
        )
    branches: dict[str, set[str]] = defaultdict(set)
    for repository_policy in record.policy.policies:
        repository = normalize_repository(repository_policy.repository, "repository")
        branches[repository].add(repository_policy.base_branch)
    return (
        "available",
        AuthorizationCandidateMergePolicyProvenance(
            record_id=record.record_id,
            policy_sha256=record.policy_sha256,
            updated_at=record.updated_at,
        ),
        {repository: tuple(sorted(values)) for repository, values in branches.items()},
        (),
    )


def _merge_policy_failure(
    state: MergePolicyProjectionState, code: str, message: str
) -> tuple[
    MergePolicyProjectionState,
    None,
    dict[str, tuple[str, ...]],
    tuple[AuthorizationCandidateInputDiagnostic, ...],
]:
    return state, None, {}, (_diagnostic(code, message),)


def _diagnostic(code: str, message: str) -> AuthorizationCandidateInputDiagnostic:
    return AuthorizationCandidateInputDiagnostic(code=code, message=message)
