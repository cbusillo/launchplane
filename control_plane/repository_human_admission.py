from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, NamedTuple, cast

from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.repository_human_admission import (
    TENANT_TECHNICAL_HUMAN_WAIVER_WRITE_ACTION,
    RepositoryHumanManagerAuthorityKind,
    RepositoryHumanRolePolicyProvenance,
    RepositoryHumanRolePolicyRecord,
    TenantTechnicalHumanWaiverAction,
    TenantTechnicalHumanWaiverAuthorization,
    TenantTechnicalHumanWaiverBinding,
    TenantTechnicalHumanWaiverEventRecord,
)
from control_plane.contracts.tenant_merge_eligibility import (
    TenantAdmissionPathState,
    TenantAdmissionPathResult,
    TenantMergeCandidate,
    TenantRepositoryClassificationRecord,
)
from control_plane.service_auth import (
    AuthorizationTarget,
    GitHubHumanIdentity,
    LaunchplaneIdentity,
    matching_github_human_policy_rules,
)


class RepositoryHumanRolePolicyError(ValueError):
    pass


class TenantTechnicalHumanWaiverAuthorizationError(PermissionError):
    pass


class TenantTechnicalHumanWaiverWriteResult(NamedTuple):
    record: TenantTechnicalHumanWaiverEventRecord
    path_result: TenantAdmissionPathResult


def repository_owner_role_policy_provenance(
    *,
    role_policy_record: RepositoryHumanRolePolicyRecord,
    repository_id: str,
    repository_owner_id: str,
    repository: str,
    product: str,
    context: str,
    github_id: int,
    evaluated_at: str,
) -> RepositoryHumanRolePolicyProvenance:
    if role_policy_record.status != "active":
        raise RepositoryHumanRolePolicyError("Repository human role policy must be active.")
    if github_id < 1:
        raise RepositoryHumanRolePolicyError("Repository human role lookup requires a GitHub ID.")
    normalized_evaluated_at = _normalize_utc_timestamp(evaluated_at, "evaluated_at")
    if role_policy_record.effective_at > normalized_evaluated_at:
        raise RepositoryHumanRolePolicyError("Repository human role policy is not effective yet.")
    _require_role_policy_scope_match(
        role_policy_record=role_policy_record,
        repository_id=repository_id,
        repository_owner_id=repository_owner_id,
        repository=repository,
        product=product,
        context=context,
    )
    if github_id not in role_policy_record.repository_owner_github_ids:
        raise RepositoryHumanRolePolicyError("GitHub human is not a repository owner.")
    return _role_policy_provenance(
        role_policy_record=role_policy_record,
        authority_kind="repository_owner",
        evaluated_at=normalized_evaluated_at,
    )


def manager_role_policy_provenance(
    *,
    role_policy_record: RepositoryHumanRolePolicyRecord,
    repository_id: str,
    repository_owner_id: str,
    repository: str,
    product: str,
    context: str,
    github_id: int,
    evaluated_at: str,
) -> RepositoryHumanRolePolicyProvenance:
    if role_policy_record.status != "active":
        raise RepositoryHumanRolePolicyError("Repository human role policy must be active.")
    if github_id < 1:
        raise RepositoryHumanRolePolicyError("Manager role lookup requires a GitHub ID.")
    normalized_evaluated_at = _normalize_utc_timestamp(evaluated_at, "evaluated_at")
    if role_policy_record.effective_at > normalized_evaluated_at:
        raise RepositoryHumanRolePolicyError("Repository human role policy is not effective yet.")
    _require_role_policy_scope_match(
        role_policy_record=role_policy_record,
        repository_id=repository_id,
        repository_owner_id=repository_owner_id,
        repository=repository,
        product=product,
        context=context,
    )
    if github_id in role_policy_record.manager_primary_github_ids:
        return _role_policy_provenance(
            role_policy_record=role_policy_record,
            authority_kind="manager_primary",
            evaluated_at=normalized_evaluated_at,
        )
    if github_id in role_policy_record.manager_backup_github_ids:
        return _role_policy_provenance(
            role_policy_record=role_policy_record,
            authority_kind="manager_backup",
            evaluated_at=normalized_evaluated_at,
        )
    active_delegations = tuple(
        delegation
        for delegation in role_policy_record.manager_delegations
        if delegation.delegated_manager_github_id == github_id
        and delegation.delegated_by_github_id
        in (
            set(role_policy_record.manager_primary_github_ids)
            | set(role_policy_record.manager_backup_github_ids)
        )
        and delegation.starts_at <= normalized_evaluated_at < delegation.expires_at
        and (not delegation.revoked_at or delegation.revoked_at > normalized_evaluated_at)
    )
    if len(active_delegations) != 1:
        raise RepositoryHumanRolePolicyError(
            "GitHub human does not have exactly one active manager authority."
        )
    return _role_policy_provenance(
        role_policy_record=role_policy_record,
        authority_kind="manager_delegated",
        evaluated_at=normalized_evaluated_at,
        delegation_id=active_delegations[0].delegation_id,
    )


def capture_tenant_technical_human_waiver_event(
    *,
    identity: LaunchplaneIdentity,
    candidate: TenantMergeCandidate,
    classification: TenantRepositoryClassificationRecord,
    role_policy_record: RepositoryHumanRolePolicyRecord,
    authz_policy_record: LaunchplaneAuthzPolicyRecord,
    action: TenantTechnicalHumanWaiverAction,
    occurred_at: str,
    source_event_kind: str,
    source_event_id: str,
    reason: str,
    recorded_at: str,
    expires_at: str = "",
) -> TenantTechnicalHumanWaiverWriteResult:
    if not isinstance(identity, GitHubHumanIdentity):
        raise TenantTechnicalHumanWaiverAuthorizationError(
            "Tenant technical human waiver requires a GitHub human identity."
        )
    if identity.github_id < 1:
        raise TenantTechnicalHumanWaiverAuthorizationError(
            "Tenant technical human waiver requires a stable GitHub numeric identity."
        )
    if authz_policy_record.status != "active" or authz_policy_record.policy.schema_version != 2:
        raise TenantTechnicalHumanWaiverAuthorizationError(
            "Tenant technical human waiver requires one active schema-v2 authorization policy."
        )
    if not _classification_matches_candidate(classification=classification, candidate=candidate):
        raise ValueError("Tenant technical human waiver classification does not match candidate.")
    if classification.classification_kind != "tenant_ui":
        raise ValueError("Tenant technical human waiver requires tenant_ui classification.")

    normalized_occurred_at = _normalize_utc_timestamp(occurred_at, "occurred_at")
    normalized_recorded_at = _normalize_utc_timestamp(recorded_at, "recorded_at")
    if normalized_occurred_at > normalized_recorded_at:
        raise ValueError("Tenant technical human waiver cannot be recorded before it occurred.")
    try:
        role_provenance = repository_owner_role_policy_provenance(
            role_policy_record=role_policy_record,
            repository_id=candidate.repository_id,
            repository_owner_id=candidate.repository_owner_id,
            repository=candidate.repository,
            product=candidate.product,
            context=candidate.context,
            github_id=identity.github_id,
            evaluated_at=normalized_occurred_at,
        )
    except RepositoryHumanRolePolicyError as error:
        raise TenantTechnicalHumanWaiverAuthorizationError(str(error)) from error
    matching_rules = matching_github_human_policy_rules(
        policy=authz_policy_record.policy,
        identity=identity,
        action=TENANT_TECHNICAL_HUMAN_WAIVER_WRITE_ACTION,
        product=candidate.product,
        context=candidate.context,
        target=AuthorizationTarget(scope="context"),
        managed_only=True,
    )
    if len(matching_rules) != 1:
        raise TenantTechnicalHumanWaiverAuthorizationError(
            "Tenant technical human waiver requires exactly one managed authz policy rule."
        )
    matching_rule = matching_rules[0]
    binding = TenantTechnicalHumanWaiverBinding(
        repository_id=candidate.repository_id,
        repository_owner_id=candidate.repository_owner_id,
        repository=candidate.repository,
        product=candidate.product,
        context=candidate.context,
        pull_request_number=candidate.pull_request_number,
        head_sha=candidate.head_sha,
        classification_revision=classification.classification_revision,
        classification_digest=classification.classification_digest,
        role_policy_record_id=role_policy_record.record_id,
        role_policy_revision=role_policy_record.role_policy_revision,
        role_policy_digest=role_policy_record.role_policy_digest,
        authz_policy_record_id=authz_policy_record.record_id,
        authz_policy_revision=authz_policy_record.revision,
        authz_policy_digest=authz_policy_record.policy_sha256,
    )
    authorization = TenantTechnicalHumanWaiverAuthorization(
        author_github_id=identity.github_id,
        author_login=identity.login,
        managed_set_id=cast(str, matching_rule.managed_set_id),
        managed_rule_id=cast(str, matching_rule.managed_rule_id),
        authz_policy_record_id=authz_policy_record.record_id,
        authz_policy_revision=authz_policy_record.revision,
        authz_policy_digest=authz_policy_record.policy_sha256,
        authz_policy_source=authz_policy_record.source,
        role_policy_provenance=role_provenance,
        authorized_at=normalized_occurred_at,
    )
    record = TenantTechnicalHumanWaiverEventRecord(
        binding=binding,
        action=action,
        occurred_at=normalized_occurred_at,
        source_event_kind=source_event_kind,
        source_event_id=source_event_id,
        reason=reason,
        authorization=authorization,
        expires_at=expires_at,
    )
    return TenantTechnicalHumanWaiverWriteResult(
        record=record,
        path_result=technical_human_waiver_path_result(
            candidate=candidate,
            classification=classification,
            role_policy_record=role_policy_record,
            authz_policy_record=authz_policy_record,
            events=(record,),
            evaluated_at=normalized_occurred_at,
        ),
    )


def technical_human_waiver_path_result(
    *,
    candidate: TenantMergeCandidate,
    classification: TenantRepositoryClassificationRecord,
    role_policy_record: RepositoryHumanRolePolicyRecord,
    authz_policy_record: LaunchplaneAuthzPolicyRecord,
    events: tuple[TenantTechnicalHumanWaiverEventRecord, ...],
    evaluated_at: str,
) -> TenantAdmissionPathResult:
    normalized_evaluated_at = _normalize_utc_timestamp(evaluated_at, "evaluated_at")

    def path_result(
        *,
        state: TenantAdmissionPathState,
        evidence_id: str = "",
        evidence_digest: str = "",
    ) -> TenantAdmissionPathResult:
        return TenantAdmissionPathResult(
            path_kind="technical_human_waiver",
            state=state,
            evidence_id=evidence_id,
            evidence_digest=evidence_digest,
            repository_id=candidate.repository_id,
            repository_owner_id=candidate.repository_owner_id,
            repository=candidate.repository,
            pull_request_number=candidate.pull_request_number,
            head_sha=candidate.head_sha,
            classification_digest=classification.classification_digest,
        )

    if role_policy_record.status != "active" or authz_policy_record.status != "active":
        return path_result(state="unavailable")
    if role_policy_record.effective_at > normalized_evaluated_at:
        return path_result(state="unavailable")
    if not _role_policy_matches_candidate(
        role_policy_record=role_policy_record,
        candidate=candidate,
    ) or not _classification_matches_candidate(classification=classification, candidate=candidate):
        return path_result(state="stale")
    if (
        classification.classification_kind != "tenant_ui"
        or not classification.classification_digest
    ):
        return path_result(state="stale")

    current_events = tuple(
        event
        for event in events
        if event.occurred_at <= normalized_evaluated_at
        and event.binding.repository_id == candidate.repository_id
        and event.binding.repository_owner_id == candidate.repository_owner_id
        and event.binding.repository == candidate.repository
        and event.binding.product == candidate.product
        and event.binding.context == candidate.context
        and event.binding.pull_request_number == candidate.pull_request_number
        and event.binding.head_sha == candidate.head_sha
        and event.binding.classification_revision == classification.classification_revision
        and event.binding.classification_digest == classification.classification_digest
        and event.binding.role_policy_record_id == role_policy_record.record_id
        and event.binding.role_policy_revision == role_policy_record.role_policy_revision
        and event.binding.role_policy_digest == role_policy_record.role_policy_digest
        and event.binding.authz_policy_record_id == authz_policy_record.record_id
        and event.binding.authz_policy_revision == authz_policy_record.revision
        and event.binding.authz_policy_digest == authz_policy_record.policy_sha256
    )
    if not current_events:
        stale_events = tuple(
            event
            for event in events
            if event.occurred_at <= normalized_evaluated_at
            and event.binding.repository_id == candidate.repository_id
            and event.binding.pull_request_number == candidate.pull_request_number
        )
        return path_result(state="stale" if stale_events else "pending")

    latest = max(
        current_events,
        key=lambda event: (event.occurred_at, event.action == "revoked", event.event_id),
    )
    if latest.action == "revoked":
        return path_result(
            state="denied",
            evidence_id=latest.waiver_id,
            evidence_digest=latest.event_digest,
        )
    if latest.expires_at and latest.expires_at <= normalized_evaluated_at:
        return path_result(
            state="stale",
            evidence_id=latest.waiver_id,
            evidence_digest=latest.event_digest,
        )
    if not _waiver_authz_rule_current(
        event=latest,
        authz_policy_record=authz_policy_record,
        product=candidate.product,
        context=candidate.context,
    ):
        return path_result(
            state="stale",
            evidence_id=latest.waiver_id,
            evidence_digest=latest.event_digest,
        )
    if latest.authorization.author_github_id not in role_policy_record.repository_owner_github_ids:
        return path_result(
            state="stale",
            evidence_id=latest.waiver_id,
            evidence_digest=latest.event_digest,
        )
    return path_result(
        state="satisfied",
        evidence_id=latest.waiver_id,
        evidence_digest=latest.event_digest,
    )


def manager_authority_current(
    *,
    provenance: RepositoryHumanRolePolicyProvenance | None,
    role_policy_record: RepositoryHumanRolePolicyRecord | None,
    manager_github_id: int,
    evaluated_at: str,
) -> bool:
    if provenance is None:
        return role_policy_record is None
    if role_policy_record is None or role_policy_record.status != "active":
        return False
    if (
        provenance.role_policy_record_id != role_policy_record.record_id
        or provenance.role_policy_revision != role_policy_record.role_policy_revision
        or provenance.role_policy_digest != role_policy_record.role_policy_digest
        or provenance.repository_id != role_policy_record.repository_id
        or provenance.repository_owner_id != role_policy_record.repository_owner_id
        or provenance.repository != role_policy_record.repository
    ):
        return False
    try:
        current = manager_role_policy_provenance(
            role_policy_record=role_policy_record,
            repository_id=provenance.repository_id,
            repository_owner_id=provenance.repository_owner_id,
            repository=provenance.repository,
            product=provenance.product,
            context=provenance.context,
            github_id=manager_github_id,
            evaluated_at=evaluated_at,
        )
    except RepositoryHumanRolePolicyError:
        return False
    return (
        current.authority_kind == provenance.authority_kind
        and current.delegation_id == provenance.delegation_id
    )


def _waiver_authz_rule_current(
    *,
    event: TenantTechnicalHumanWaiverEventRecord,
    authz_policy_record: LaunchplaneAuthzPolicyRecord,
    product: str,
    context: str,
) -> bool:
    authorization = event.authorization
    if authz_policy_record.policy.schema_version != 2:
        return False
    if (
        authorization.authz_policy_record_id != authz_policy_record.record_id
        or authorization.authz_policy_revision != authz_policy_record.revision
        or authorization.authz_policy_digest != authz_policy_record.policy_sha256
    ):
        return False
    matching_rules = tuple(
        rule
        for rule in authz_policy_record.policy.github_humans
        if rule.managed_set_id == authorization.managed_set_id
        and rule.managed_rule_id == authorization.managed_rule_id
    )
    if len(matching_rules) != 1:
        return False
    rule = matching_rules[0]
    return (
        authorization.author_github_id in rule.github_ids
        and TENANT_TECHNICAL_HUMAN_WAIVER_WRITE_ACTION in rule.actions
        and (not rule.products or product in rule.products)
        and (not rule.contexts or context in rule.contexts)
    )


def _role_policy_provenance(
    *,
    role_policy_record: RepositoryHumanRolePolicyRecord,
    authority_kind: RepositoryHumanManagerAuthorityKind | Literal["repository_owner"],
    evaluated_at: str,
    delegation_id: str = "",
) -> RepositoryHumanRolePolicyProvenance:
    return RepositoryHumanRolePolicyProvenance(
        repository_id=role_policy_record.repository_id,
        repository_owner_id=role_policy_record.repository_owner_id,
        repository=role_policy_record.repository,
        product=role_policy_record.product,
        context=role_policy_record.context,
        role_policy_record_id=role_policy_record.record_id,
        role_policy_revision=role_policy_record.role_policy_revision,
        role_policy_digest=role_policy_record.role_policy_digest,
        role_policy_source=role_policy_record.source,
        authority_kind=authority_kind,
        delegation_id=delegation_id,
        evaluated_at=evaluated_at,
    )


def _require_role_policy_scope_match(
    *,
    role_policy_record: RepositoryHumanRolePolicyRecord,
    repository_id: str,
    repository_owner_id: str,
    repository: str,
    product: str,
    context: str,
) -> None:
    if (
        role_policy_record.repository_id != repository_id
        or role_policy_record.repository_owner_id != repository_owner_id
        or role_policy_record.repository != repository.lower()
        or role_policy_record.product != product
        or role_policy_record.context != context
    ):
        raise RepositoryHumanRolePolicyError(
            "Repository human role policy does not match repository scope."
        )


def _role_policy_matches_candidate(
    *,
    role_policy_record: RepositoryHumanRolePolicyRecord,
    candidate: TenantMergeCandidate,
) -> bool:
    return (
        role_policy_record.repository_id == candidate.repository_id
        and role_policy_record.repository_owner_id == candidate.repository_owner_id
        and role_policy_record.repository == candidate.repository
        and role_policy_record.product == candidate.product
        and role_policy_record.context == candidate.context
    )


def _classification_matches_candidate(
    *,
    classification: TenantRepositoryClassificationRecord,
    candidate: TenantMergeCandidate,
) -> bool:
    return (
        classification.repository_id == candidate.repository_id
        and classification.repository_owner_id == candidate.repository_owner_id
        and classification.repository == candidate.repository
        and classification.product == candidate.product
        and classification.context == candidate.context
    )


def _normalize_utc_timestamp(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"repository human admission requires {label}")
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(
            f"repository human admission {label} must be an ISO-8601 timestamp"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"repository human admission {label} requires a timezone")
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
