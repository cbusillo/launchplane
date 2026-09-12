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
    MergePolicyProjectionState,
    OrdinaryAgentDeliveryAuthorizationCandidateInputsResponse,
    OrdinaryAgentDeliveryAuthorizationCandidateRepository,
    RepositoryInventoryProjectionState,
)
from control_plane.contracts.repository_inventory import (
    RepositoryInventoryRecord,
    normalize_repository,
)


MAX_AUTHORIZATION_INPUT_SOURCE_RECORDS = 1000
MAX_AUTHORIZATION_INPUT_MERGE_TARGETS = 1000


class OrdinaryAgentDeliveryAuthorizationInputStore(Protocol):
    def list_repository_inventory_records(
        self, *, repository_id: str = "", limit: int | None = None
    ) -> tuple[RepositoryInventoryRecord, ...]: ...

    def list_merge_train_policy_records(
        self, *, status: str = "", limit: int | None = None
    ) -> tuple[MergeTrainPolicyRecord, ...]: ...


def read_ordinary_agent_delivery_authorization_candidate_inputs(
    *,
    record_store: object,
    policy_record: LaunchplaneAuthzPolicyRecord,
    trace_id: str,
    observed_at: str,
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
