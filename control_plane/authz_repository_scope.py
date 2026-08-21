from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
import os
from typing import Protocol, TypeVar

import click

from control_plane import secrets as control_plane_secrets
from control_plane.contracts.authz_access_read import (
    AUTHZ_REPOSITORY_SCOPE_MAX_SOURCE_RECORDS,
    AuthzRepositoryScopeCandidateResult,
    AuthzRepositoryScopeGap,
    AuthzRepositoryScopeGapReason,
    AuthzRepositoryScopeGapSource,
    AuthzRepositoryScopeMembership,
    AuthzRepositoryScopePolicyProvenance,
    AuthzRepositoryScopeProvenance,
    AuthzRepositoryScopeReadRequest,
    AuthzRepositoryScopeRecordProvenance,
    AuthzRepositoryScopeResponse,
    AuthzRepositoryScopeSourceCounts,
    AuthzRepositoryScopeCoverage,
)
from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.every_code_work_request import EveryCodeWorkRequestRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.repository_human_admission import RepositoryHumanRolePolicyRecord
from control_plane.contracts.tenant_merge_eligibility import (
    TenantRepositoryClassificationRecord,
)


_HANDLE_PURPOSE = "authz-repository-scope-handle-v1"
_HANDLE_GENERATION_PURPOSE = "authz-repository-scope-handle-generation-v1"
_MEMBERSHIP_ORDER: tuple[AuthzRepositoryScopeMembership, ...] = (
    "product",
    "repository_record",
    "work_graph",
    "authorization_chain",
)
_GAP_SOURCE_ORDER: tuple[AuthzRepositoryScopeGapSource, ...] = (
    "product",
    "repository_record",
    "work_graph",
    "authorization_chain",
    "candidate",
)
_CURRENT_WORK_REQUEST_STATES = frozenset({"queued", "claimed", "running"})
_RecordT = TypeVar("_RecordT")


class AuthzRepositoryScopeStore(Protocol):
    def list_product_profile_records(
        self,
        *,
        driver_id: str = "",
    ) -> tuple[LaunchplaneProductProfileRecord, ...]: ...

    def list_repository_human_role_policy_records(
        self,
        *,
        repository_id: str = "",
        repository_owner_id: str = "",
        repository: str = "",
        product: str = "",
        context: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[RepositoryHumanRolePolicyRecord, ...]: ...

    def list_tenant_repository_classification_records(
        self,
        *,
        repository_id: str = "",
        limit: int | None = None,
    ) -> tuple[TenantRepositoryClassificationRecord, ...]: ...

    def list_every_code_work_request_records(
        self,
        *,
        state: str = "",
        repository: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[EveryCodeWorkRequestRecord, ...]: ...


@dataclass(frozen=True)
class _Evidence:
    repository: str
    repository_id: str
    repository_owner_id: str
    membership: AuthzRepositoryScopeMembership
    current: bool
    preserves_repository_case: bool


@dataclass
class _RepositoryNode:
    normalized_repository: str
    exact_repositories: set[str] = field(default_factory=set)
    immutable_identities: set[tuple[str, str]] = field(default_factory=set)
    memberships: set[AuthzRepositoryScopeMembership] = field(default_factory=set)
    stale_memberships: set[AuthzRepositoryScopeMembership] = field(default_factory=set)
    ambiguous: bool = False

    @property
    def immutable_identity(self) -> tuple[str, str]:
        if len(self.immutable_identities) != 1:
            return "", ""
        return next(iter(self.immutable_identities))

    @property
    def ordered_memberships(self) -> tuple[AuthzRepositoryScopeMembership, ...]:
        return tuple(
            membership for membership in _MEMBERSHIP_ORDER if membership in self.memberships
        )


def build_authz_repository_scope_response(
    *,
    trace_id: str,
    generated_at: str,
    request: AuthzRepositoryScopeReadRequest,
    active_policy_record: LaunchplaneAuthzPolicyRecord,
    store: AuthzRepositoryScopeStore,
) -> AuthzRepositoryScopeResponse:
    generated_time = _parse_timestamp(generated_at)
    if generated_time is None:
        raise ValueError("Repository scope generation time must be timezone-aware.")
    truncated_sources: Counter[AuthzRepositoryScopeGapSource] = Counter()
    product_profiles = _bounded_records(
        store.list_product_profile_records(),
        source="product",
        truncated_sources=truncated_sources,
    )
    role_policy_records = _bounded_records(
        store.list_repository_human_role_policy_records(
            limit=AUTHZ_REPOSITORY_SCOPE_MAX_SOURCE_RECORDS + 1
        ),
        source="repository_record",
        truncated_sources=truncated_sources,
    )
    classification_records = _bounded_records(
        store.list_tenant_repository_classification_records(
            limit=AUTHZ_REPOSITORY_SCOPE_MAX_SOURCE_RECORDS + 1
        ),
        source="repository_record",
        truncated_sources=truncated_sources,
    )
    work_requests = _bounded_interleaved_records(
        tuple(
            store.list_every_code_work_request_records(
                state=state,
                limit=AUTHZ_REPOSITORY_SCOPE_MAX_SOURCE_RECORDS + 1,
            )
            for state in ("queued", "claimed", "running")
        ),
        source="work_graph",
        truncated_sources=truncated_sources,
    )
    authorization_rules = _bounded_records(
        active_policy_record.policy.github_actions,
        source="authorization_chain",
        truncated_sources=truncated_sources,
    )

    current_role_policy_records, role_policy_ambiguity_count = _current_role_policy_records(
        role_policy_records
    )
    current_classification_records, classification_ambiguity_count = (
        _current_classification_records(classification_records)
    )
    current_work_requests = tuple(
        record for record in work_requests if record.state in _CURRENT_WORK_REQUEST_STATES
    )

    evidence = (
        tuple(_product_evidence(record) for record in product_profiles)
        + tuple(
            _role_policy_evidence(record, current_role_policy_records)
            for record in role_policy_records
        )
        + tuple(
            _classification_evidence(record, current_classification_records)
            for record in classification_records
        )
        + tuple(_work_request_evidence(record) for record in work_requests)
        + tuple(_authorization_evidence(rule) for rule in authorization_rules)
    )

    nodes, malformed_identity_counts = _build_nodes(evidence)
    gap_counts: Counter[tuple[AuthzRepositoryScopeGapSource, AuthzRepositoryScopeGapReason]] = (
        Counter()
    )
    if role_policy_ambiguity_count:
        gap_counts[("repository_record", "active_record_ambiguous")] += role_policy_ambiguity_count
    if classification_ambiguity_count:
        gap_counts[("repository_record", "active_record_ambiguous")] += (
            classification_ambiguity_count
        )
    for source in _GAP_SOURCE_ORDER:
        if count := truncated_sources[source]:
            gap_counts[(source, "source_truncated")] += count
    stale_work_request_count = sum(
        record.state in {"claimed", "running"}
        and (
            (lease_expires_at := _parse_timestamp(record.lease_expires_at)) is None
            or lease_expires_at <= generated_time
        )
        for record in current_work_requests
    )
    if stale_work_request_count:
        gap_counts[("work_graph", "stale_work_graph_record")] += stale_work_request_count
    for source in _GAP_SOURCE_ORDER:
        if count := malformed_identity_counts[source]:
            gap_counts[(source, "repository_identity_conflict")] += count

    id_to_names: dict[tuple[str, str], set[str]] = defaultdict(set)
    for node in nodes.values():
        for immutable_identity in node.immutable_identities:
            id_to_names[immutable_identity].add(node.normalized_repository)
    conflicting_names = {
        repository
        for repositories in id_to_names.values()
        if len(repositories) > 1
        for repository in repositories
    }

    for node in nodes.values():
        if len(node.immutable_identities) > 1:
            node.ambiguous = True
            gap_counts[("repository_record", "immutable_identity_conflict")] += 1
        if node.normalized_repository in conflicting_names:
            node.ambiguous = True
            gap_counts[("repository_record", "repository_identity_conflict")] += 1
        if len(node.exact_repositories) > 1:
            gap_counts[("authorization_chain", "repository_case_variant")] += 1
        if not node.immutable_identities and node.memberships != {"work_graph"}:
            gap_counts[("repository_record", "immutable_identity_missing")] += 1
        if node.memberships == {"authorization_chain"} and node.stale_memberships:
            gap_counts[("authorization_chain", "stale_authorization_membership")] += 1

    timestamp_gaps = _timestamp_gap_counts(
        product_profiles=tuple(record for record in product_profiles if record.is_active),
        repository_records=(*current_role_policy_records, *current_classification_records),
        work_requests=current_work_requests,
    )
    gap_counts.update(timestamp_gaps)

    candidate_results: list[AuthzRepositoryScopeCandidateResult] = []
    matched_repositories: set[str] = set()
    handle_key_id, handle_generation = _keyed_digest(
        "",
        purpose=_HANDLE_GENERATION_PURPOSE,
    )
    for index, candidate in enumerate(request.candidates):
        normalized_repository = candidate.repository.casefold()
        candidate_node = nodes.get(normalized_repository)
        if candidate_node is None:
            gap_counts[("candidate", "candidate_repository_missing")] += 1
            candidate_results.append(
                AuthzRepositoryScopeCandidateResult(index=index, state="not_found")
            )
            continue
        candidate_identity = (candidate.repository_id, candidate.repository_owner_id)
        resolved_identity = candidate_node.immutable_identity
        immutable_identity_mismatch = bool(
            candidate.repository_id
            and (not resolved_identity[0] or candidate_identity != resolved_identity)
        )
        if (
            candidate_node.ambiguous
            or immutable_identity_mismatch
            or not candidate_node.ordered_memberships
        ):
            gap_counts[("candidate", "candidate_repository_ambiguous")] += 1
            candidate_results.append(
                AuthzRepositoryScopeCandidateResult(index=index, state="ambiguous")
            )
            continue
        key_id, digest = _keyed_digest(
            _handle_payload(candidate_node),
            purpose=_HANDLE_PURPOSE,
        )
        if key_id != handle_key_id:
            raise RuntimeError("Repository scope handle generation changed during one response.")
        matched_repositories.add(normalized_repository)
        candidate_results.append(
            AuthzRepositoryScopeCandidateResult(
                index=index,
                state="matched",
                handle=f"repo_{digest}",
                memberships=candidate_node.ordered_memberships,
            )
        )

    unmatched_nodes = tuple(
        node
        for normalized_repository, node in nodes.items()
        if normalized_repository not in matched_repositories
    )
    if unmatched_nodes:
        gap_counts[("candidate", "candidate_coverage_incomplete")] += len(unmatched_nodes)

    gaps = tuple(
        AuthzRepositoryScopeGap(source=source, reason_code=reason_code, count=count)
        for (source, reason_code), count in sorted(gap_counts.items())
        if count
    )
    repository_count = len(nodes)
    matched_repository_count = len(matched_repositories)
    return AuthzRepositoryScopeResponse(
        trace_id=trace_id,
        generated_at=generated_at,
        handle_generation=f"generation_{handle_generation[:16]}",
        coverage=AuthzRepositoryScopeCoverage(
            state="complete" if not gaps else "partial",
            source_counts=AuthzRepositoryScopeSourceCounts(
                product=sum(record.is_active for record in product_profiles),
                repository_record=len(current_role_policy_records)
                + len(current_classification_records),
                work_graph=len(current_work_requests),
                authorization_chain=sum(
                    "authorization_chain" in node.memberships for node in nodes.values()
                ),
            ),
            repository_count=repository_count,
            matched_repository_count=matched_repository_count,
            unmatched_repository_count=repository_count - matched_repository_count,
            gaps=gaps,
        ),
        provenance=AuthzRepositoryScopeProvenance(
            authorization_chain=AuthzRepositoryScopePolicyProvenance(
                record_id=active_policy_record.record_id,
                revision=active_policy_record.revision,
                updated_at=active_policy_record.updated_at,
            ),
            product=_record_provenance(
                tuple(record.updated_at for record in product_profiles if record.is_active)
            ),
            repository_record=_record_provenance(
                tuple(record.effective_at for record in current_role_policy_records)
                + tuple(record.classified_at for record in current_classification_records)
            ),
            work_graph=_record_provenance(
                tuple(record.updated_at for record in current_work_requests)
            ),
        ),
        candidates=tuple(candidate_results),
    )


def _build_nodes(
    evidence: tuple[_Evidence, ...],
) -> tuple[
    dict[str, _RepositoryNode],
    Counter[AuthzRepositoryScopeGapSource],
]:
    nodes: dict[str, _RepositoryNode] = {}
    malformed_counts: Counter[AuthzRepositoryScopeGapSource] = Counter()
    for item in evidence:
        repository = item.repository.strip()
        owner, separator, name = repository.partition("/")
        if not separator or not owner or not name or "/" in name:
            malformed_counts[_gap_source(item.membership)] += 1
            continue
        normalized_repository = repository.casefold()
        node = nodes.setdefault(
            normalized_repository,
            _RepositoryNode(normalized_repository=normalized_repository),
        )
        if item.preserves_repository_case:
            node.exact_repositories.add(repository)
        if item.repository_id and item.repository_owner_id:
            node.immutable_identities.add((item.repository_id, item.repository_owner_id))
        if item.current:
            node.memberships.add(item.membership)
        else:
            node.stale_memberships.add(item.membership)
    return (
        {
            normalized_repository: node
            for normalized_repository, node in nodes.items()
            if node.memberships
        },
        malformed_counts,
    )


def _bounded_records(
    records: tuple[_RecordT, ...],
    *,
    source: AuthzRepositoryScopeGapSource,
    truncated_sources: Counter[AuthzRepositoryScopeGapSource],
) -> tuple[_RecordT, ...]:
    if len(records) <= AUTHZ_REPOSITORY_SCOPE_MAX_SOURCE_RECORDS:
        return records
    truncated_sources[source] += 1
    return records[:AUTHZ_REPOSITORY_SCOPE_MAX_SOURCE_RECORDS]


def _bounded_interleaved_records(
    record_groups: tuple[tuple[_RecordT, ...], ...],
    *,
    source: AuthzRepositoryScopeGapSource,
    truncated_sources: Counter[AuthzRepositoryScopeGapSource],
) -> tuple[_RecordT, ...]:
    retained: list[_RecordT] = []
    for index in range(max((len(records) for records in record_groups), default=0)):
        for records in record_groups:
            if index >= len(records):
                continue
            if len(retained) == AUTHZ_REPOSITORY_SCOPE_MAX_SOURCE_RECORDS:
                truncated_sources[source] += 1
                return tuple(retained)
            retained.append(records[index])
    return tuple(retained)


def _current_role_policy_records(
    records: tuple[RepositoryHumanRolePolicyRecord, ...],
) -> tuple[tuple[RepositoryHumanRolePolicyRecord, ...], int]:
    return _latest_unique_records(
        tuple(record for record in records if record.status == "active"),
        key=lambda record: (record.repository_id, record.product, record.context),
        revision=lambda record: record.role_policy_revision,
    )


def _current_classification_records(
    records: tuple[TenantRepositoryClassificationRecord, ...],
) -> tuple[tuple[TenantRepositoryClassificationRecord, ...], int]:
    return _latest_unique_records(
        records,
        key=lambda record: record.repository_id,
        revision=lambda record: record.classification_revision,
    )


def _latest_unique_records(
    records: tuple[_RecordT, ...],
    *,
    key: Callable[[_RecordT], object],
    revision: Callable[[_RecordT], int],
) -> tuple[tuple[_RecordT, ...], int]:
    grouped: dict[object, list[_RecordT]] = defaultdict(list)
    for record in records:
        grouped[key(record)].append(record)
    current: list[_RecordT] = []
    ambiguity_count = 0
    for matching_records in grouped.values():
        highest_revision = max(revision(record) for record in matching_records)
        latest = [record for record in matching_records if revision(record) == highest_revision]
        if len(latest) != 1:
            ambiguity_count += 1
            continue
        current.append(latest[0])
    return tuple(current), ambiguity_count


def _product_evidence(record: LaunchplaneProductProfileRecord) -> _Evidence:
    return _Evidence(
        repository=record.repository,
        repository_id=record.repository_id,
        repository_owner_id=record.repository_owner_id,
        membership="product",
        current=record.is_active,
        preserves_repository_case=True,
    )


def _role_policy_evidence(
    record: RepositoryHumanRolePolicyRecord,
    current_records: tuple[RepositoryHumanRolePolicyRecord, ...],
) -> _Evidence:
    return _Evidence(
        repository=record.repository,
        repository_id=record.repository_id,
        repository_owner_id=record.repository_owner_id,
        membership="repository_record",
        current=record in current_records,
        preserves_repository_case=False,
    )


def _classification_evidence(
    record: TenantRepositoryClassificationRecord,
    current_records: tuple[TenantRepositoryClassificationRecord, ...],
) -> _Evidence:
    return _Evidence(
        repository=record.repository,
        repository_id=record.repository_id,
        repository_owner_id=record.repository_owner_id,
        membership="repository_record",
        current=record in current_records,
        preserves_repository_case=False,
    )


def _work_request_evidence(record: EveryCodeWorkRequestRecord) -> _Evidence:
    return _Evidence(
        repository=record.repository,
        repository_id="",
        repository_owner_id="",
        membership="work_graph",
        current=record.state in _CURRENT_WORK_REQUEST_STATES,
        preserves_repository_case=True,
    )


def _authorization_evidence(rule: object) -> _Evidence:
    repository = str(getattr(rule, "repository", ""))
    repository_id = str(getattr(rule, "repository_id", ""))
    repository_owner_id = str(getattr(rule, "repository_owner_id", ""))
    return _Evidence(
        repository=repository,
        repository_id=repository_id,
        repository_owner_id=repository_owner_id,
        membership="authorization_chain",
        current=True,
        preserves_repository_case=True,
    )


def _handle_payload(node: _RepositoryNode) -> str:
    repository_id, repository_owner_id = node.immutable_identity
    if repository_id:
        return f"id:{repository_owner_id}:{repository_id}"
    return f"name:{node.normalized_repository}"


def _keyed_digest(payload: str, *, purpose: str) -> tuple[str, str]:
    if not os.environ.get(control_plane_secrets.LAUNCHPLANE_SECRET_KEYS_JSON_ENV_VAR, "").strip():
        raise click.ClickException(
            "Public stable identifiers require the canonical managed-secret key ring."
        )
    fingerprint = control_plane_secrets.keyed_secret_payload_fingerprint(
        payload,
        purpose=purpose,
    )
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
        raise RuntimeError("Managed-secret fingerprint returned an unsupported format.")
    return key_id, digest


def _timestamp_gap_counts(
    *,
    product_profiles: tuple[LaunchplaneProductProfileRecord, ...],
    repository_records: tuple[
        RepositoryHumanRolePolicyRecord | TenantRepositoryClassificationRecord,
        ...,
    ],
    work_requests: tuple[EveryCodeWorkRequestRecord, ...],
) -> Counter[tuple[AuthzRepositoryScopeGapSource, AuthzRepositoryScopeGapReason]]:
    gaps: Counter[tuple[AuthzRepositoryScopeGapSource, AuthzRepositoryScopeGapReason]] = Counter()
    timestamp_groups: tuple[tuple[AuthzRepositoryScopeGapSource, tuple[str, ...]], ...] = (
        ("product", tuple(record.updated_at for record in product_profiles)),
        (
            "repository_record",
            tuple(
                record.effective_at
                if isinstance(record, RepositoryHumanRolePolicyRecord)
                else record.classified_at
                for record in repository_records
            ),
        ),
        ("work_graph", tuple(record.updated_at for record in work_requests)),
    )
    for source, values in timestamp_groups:
        malformed_count = sum(not _is_timestamp(value) for value in values)
        if malformed_count:
            gaps[(source, "record_timestamp_malformed")] += malformed_count
    return gaps


def _record_provenance(values: tuple[str, ...]) -> AuthzRepositoryScopeRecordProvenance:
    parsed = tuple(
        (parsed_value, value)
        for value in values
        if (parsed_value := _parse_timestamp(value)) is not None
    )
    latest = max(parsed, default=None)
    return AuthzRepositoryScopeRecordProvenance(
        active_record_count=len(values),
        latest_recorded_at=latest[1] if latest is not None else "",
    )


def _is_timestamp(value: str) -> bool:
    return _parse_timestamp(value) is not None


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.utcoffset() is None:
        return None
    return parsed


def _gap_source(membership: AuthzRepositoryScopeMembership) -> AuthzRepositoryScopeGapSource:
    if membership == "product":
        return "product"
    if membership == "work_graph":
        return "work_graph"
    if membership == "authorization_chain":
        return "authorization_chain"
    return "repository_record"
