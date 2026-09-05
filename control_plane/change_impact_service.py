from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Protocol, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field

from control_plane.contracts.change_impact import (
    ChangeImpactAffectedProduct,
    ChangeImpactChangedFileEvidence,
    ChangeImpactComponentRule,
    ChangeImpactCoverage,
    ChangeImpactEvaluation,
    ChangeImpactMatchedEvidence,
    ChangeImpactPolicyRecord,
    ChangeImpactProductScope,
    ChangeImpactRepositoryEvidence,
    ChangeImpactStoredEvidence,
    ChangeImpactTarget,
    ChangeImpactTargetReference,
)


class ChangeImpactPolicyConflictError(ValueError):
    """Raised on a change-impact policy write race or conflicting replay."""


class ChangeImpactPolicySequenceError(ValueError):
    """Raised when change-impact policy revision history is stale or non-linear."""


ChangeImpactApplyMode = Literal["dry_run", "apply"]
ChangeImpactApplyStatus = Literal["would_apply", "would_replay", "applied", "replayed"]


class ChangeImpactPolicyReadStore(Protocol):
    def list_change_impact_policy_records(
        self,
        *,
        repository_id: str = "",
        repository: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[ChangeImpactPolicyRecord, ...]: ...


class ChangeImpactRepositoryEvidenceProvider(Protocol):
    def resolve(
        self,
        target: ChangeImpactTargetReference,
    ) -> ChangeImpactRepositoryEvidence: ...


class ChangeImpactStoredEvidenceReadStore(Protocol):
    def list_change_impact_stored_evidence(
        self,
        *,
        repository_id: str,
        pull_request_number: int,
        head_sha: str,
        tree_sha: str,
    ) -> tuple[ChangeImpactStoredEvidence, ...]: ...


class ChangeImpactPolicyStore(ChangeImpactPolicyReadStore, Protocol):
    def compare_and_write_change_impact_policy_record(
        self,
        record: ChangeImpactPolicyRecord,
        *,
        expected_current_record_id: str,
        expected_current_policy_digest: str,
    ) -> Literal["written", "replayed"]: ...


class ChangeImpactPolicyApplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    status: ChangeImpactApplyStatus
    record: ChangeImpactPolicyRecord


class ChangeImpactPolicyReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    repository_id: str
    current_policy: ChangeImpactPolicyRecord | None = None
    policy_history_count: int = Field(default=0, ge=0)


RecordT = TypeVar("RecordT", bound=ChangeImpactPolicyRecord)


@dataclass(frozen=True, slots=True)
class ChangeImpactTrustedCallerBinding:
    repository: str
    repository_id: str
    repository_owner_id: str
    head_sha: str
    ref: str = ""


def apply_change_impact_policy(
    *,
    store: ChangeImpactPolicyStore,
    record: ChangeImpactPolicyRecord,
    expected_current_record_id: str = "",
    expected_current_policy_digest: str = "",
    mode: ChangeImpactApplyMode = "apply",
) -> ChangeImpactPolicyApplyResult:
    replay = _validate_policy_append(
        records=store.list_change_impact_policy_records(repository_id=record.repository_id),
        record=record,
        expected_current_record_id=expected_current_record_id,
        expected_current_policy_digest=expected_current_policy_digest,
    )
    if mode == "dry_run":
        return ChangeImpactPolicyApplyResult(
            status="would_replay" if replay else "would_apply",
            record=record,
        )
    write_status = store.compare_and_write_change_impact_policy_record(
        record,
        expected_current_record_id=expected_current_record_id,
        expected_current_policy_digest=expected_current_policy_digest,
    )
    return ChangeImpactPolicyApplyResult(
        status="replayed" if write_status == "replayed" else "applied",
        record=record,
    )


def get_change_impact_policy_read_model(
    *,
    store: ChangeImpactPolicyReadStore,
    repository_id: str,
) -> ChangeImpactPolicyReadModel:
    records = store.list_change_impact_policy_records(repository_id=repository_id)
    return ChangeImpactPolicyReadModel(
        repository_id=repository_id.strip(),
        current_policy=_safe_current_policy(records),
        policy_history_count=len(records),
    )


def evaluate_change_impact(
    *,
    repository_evidence: ChangeImpactRepositoryEvidence,
    policies: tuple[ChangeImpactPolicyRecord, ...],
    stored_evidence: tuple[ChangeImpactStoredEvidence, ...] = (),
    trusted_caller_binding: ChangeImpactTrustedCallerBinding | None = None,
    evaluated_at: str = "",
) -> ChangeImpactEvaluation:
    target = repository_evidence.target
    if trusted_caller_binding is not None and not _binding_matches_repository(
        binding=trusted_caller_binding,
        target=target,
    ):
        return _unknown_evaluation(
            target=target,
            status="unknown",
            reason_code="caller_repository_mismatch",
            unknown_evidence=(
                "trusted caller repository identity does not match provider evidence",
            ),
        )
    if trusted_caller_binding is not None and not _binding_matches_target(
        binding=trusted_caller_binding,
        repository_evidence=repository_evidence,
    ):
        return _unknown_evaluation(
            target=target,
            status="stale_head",
            reason_code="stale_head",
            unknown_evidence=("trusted caller head does not match current pull request head",),
        )

    try:
        policy = _current_policy(
            policies,
            repository_id=target.repository_id,
            evaluated_at=_evaluation_timestamp(evaluated_at),
        )
    except ValueError:
        return _unknown_evaluation(
            target=target,
            status="unknown",
            reason_code="policy_history_invalid",
            unknown_evidence=("change-impact policy history is ambiguous",),
        )
    if policy is None:
        return _unknown_evaluation(
            target=target,
            status="unknown",
            reason_code="policy_unavailable",
            unknown_evidence=("no active change-impact policy for repository",),
        )
    if not _policy_matches_target(policy=policy, target=target):
        return _unknown_evaluation(
            target=target,
            status="unknown",
            reason_code="policy_target_mismatch",
            policy=policy,
            unknown_evidence=("active change-impact policy does not match repository identity",),
        )

    matched: list[ChangeImpactMatchedEvidence] = []
    affected_products: set[ChangeImpactProductScope] = set()
    production_affecting_products: set[ChangeImpactProductScope] = set()
    sensitive = False
    unknown: list[str] = []
    unmatched_paths: set[str] = set()

    rules_by_component = {rule.component: rule for rule in policy.component_rules}
    for changed_file in repository_evidence.changed_files:
        path_matches = _matching_rules(changed_file=changed_file, rules=policy.component_rules)
        if not path_matches:
            unmatched_paths.add(changed_file.path)
            continue
        for rule in path_matches:
            matched.append(_file_match(changed_file=changed_file, rule=rule))
            affected_products.update(rule.affected_products)
            if rule.production_affecting:
                production_affecting_products.update(rule.affected_products)
            if rule.review_tier == "sensitive":
                sensitive = True

    dependency_components = {
        evidence.component
        for evidence in stored_evidence
        if evidence.kind == "dependency" and evidence.confidence == "known"
    }
    matched_components = {evidence.component for evidence in matched if evidence.component}

    for evidence in stored_evidence:
        if evidence.confidence != "known":
            unknown.append(f"{evidence.confidence} stored evidence for {evidence.component}")
            continue
        if evidence.component not in rules_by_component:
            unknown.append(f"stored evidence references unknown component {evidence.component}")
            continue
        if evidence.component not in matched_components:
            unknown.append(
                f"stored evidence references component not matched by changed files {evidence.component}"
            )
            continue
        rule = rules_by_component[evidence.component]
        if (
            evidence.kind == "reviewer"
            and evidence.affected_products
            and evidence.component not in dependency_components
        ):
            unknown.append(
                f"reviewer evidence lacks trusted dependency evidence for {evidence.component}"
            )
            continue
        affected_products.update(evidence.affected_products)
        affected_products.update(rule.affected_products)
        if rule.production_affecting:
            production_affecting_products.update(evidence.affected_products)
            production_affecting_products.update(rule.affected_products)
        matched.append(_stored_evidence_match(evidence=evidence, rule=rule))
        if rule.review_tier == "sensitive":
            sensitive = True

    sorted_unmatched_paths = sorted(unmatched_paths)
    coverage = ChangeImpactCoverage(
        state="incomplete" if unmatched_paths else "complete",
        unmatched_path_count=len(unmatched_paths),
        unmatched_path_samples=tuple(path[:256] for path in sorted_unmatched_paths[:20]),
        truncated=len(unmatched_paths) > 20
        or any(len(path) > 256 for path in sorted_unmatched_paths[:20]),
    )
    if unknown or unmatched_paths:
        reason_code = "ambiguous_or_missing_evidence" if unknown else "policy_coverage_incomplete"
        unknown.extend(
            f"no policy rule matched changed path {path}"
            for path in coverage.unmatched_path_samples
        )
        if coverage.truncated:
            unknown.append(
                f"unmatched path diagnostics truncated; {coverage.unmatched_path_count} total paths"
            )
        return _unknown_evaluation(
            target=target,
            status="unknown",
            reason_code=reason_code,
            policy=policy,
            matched_evidence=tuple(matched),
            affected_products=_affected_products(affected_products),
            unknown_evidence=tuple(dict.fromkeys(unknown)),
            coverage=coverage,
        )

    affected = _affected_products(affected_products)
    review_tier: Literal["routine", "sensitive"] = "sensitive" if sensitive else "routine"
    return ChangeImpactEvaluation(
        status="success",
        reason_code="change_impact_classified",
        target=target,
        policy_record_id=policy.record_id,
        policy_revision=policy.policy_revision,
        policy_digest=policy.policy_digest,
        engineering_review_tier=review_tier,
        required_engineering_review_count=2 if review_tier == "sensitive" else 1,
        owner_impact="required" if affected else "not_required",
        affected_products=affected,
        production_affecting_products=tuple(sorted(production_affecting_products, key=_scope_key)),
        matched_evidence=tuple(matched),
        unknown_evidence=(),
        coverage=coverage,
    )


def require_change_impact_policy_store(store: object) -> ChangeImpactPolicyStore:
    required_methods = (
        "list_change_impact_policy_records",
        "compare_and_write_change_impact_policy_record",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(store, method_name, None))
    ]
    if missing_methods:
        raise TypeError(
            "Launchplane record store does not support change-impact policy writes: "
            + ", ".join(missing_methods)
        )
    return cast(ChangeImpactPolicyStore, store)


def require_change_impact_policy_read_store(store: object) -> ChangeImpactPolicyReadStore:
    if not callable(getattr(store, "list_change_impact_policy_records", None)):
        raise TypeError("Launchplane record store does not support change-impact policy reads.")
    return cast(ChangeImpactPolicyReadStore, store)


def load_change_impact_stored_evidence(
    *,
    store: object,
    target: ChangeImpactTarget,
) -> tuple[ChangeImpactStoredEvidence, ...]:
    if not callable(getattr(store, "list_change_impact_stored_evidence", None)):
        return ()
    reader = cast(ChangeImpactStoredEvidenceReadStore, store)
    evidence = reader.list_change_impact_stored_evidence(
        repository_id=target.repository_id,
        pull_request_number=target.pull_request_number,
        head_sha=target.head_sha,
        tree_sha=target.tree_sha,
    )
    if not isinstance(evidence, tuple) or not all(
        isinstance(item, ChangeImpactStoredEvidence) for item in evidence
    ):
        raise TypeError("Launchplane stored change-impact evidence has an invalid contract.")
    return evidence


def _validate_policy_append(
    *,
    records: tuple[ChangeImpactPolicyRecord, ...],
    record: ChangeImpactPolicyRecord,
    expected_current_record_id: str,
    expected_current_policy_digest: str,
) -> bool:
    matching = tuple(
        existing for existing in records if existing.repository_id == record.repository_id
    )
    same_id = tuple(existing for existing in matching if existing.record_id == record.record_id)
    if same_id:
        if len(same_id) != 1 or same_id[0].policy_digest != record.policy_digest:
            raise ChangeImpactPolicyConflictError(
                "change-impact policy record id conflicts with history"
            )
        if same_id[0].status != record.status:
            raise ChangeImpactPolicyConflictError(
                "change-impact policy replay status conflicts with history"
            )
        return True
    current_records = tuple(existing for existing in matching if existing.status == "active")
    if len(current_records) > 1:
        raise ChangeImpactPolicySequenceError(
            "change-impact policy history has multiple active revisions"
        )
    current = current_records[0] if current_records else None
    if record.status != "active":
        raise ChangeImpactPolicySequenceError(
            "incoming change-impact policy revision must be active"
        )
    if record.effective_at > _evaluation_timestamp(""):
        raise ChangeImpactPolicySequenceError(
            "incoming change-impact policy effective_at cannot be in the future"
        )
    if current is None:
        if record.policy_revision != 1:
            raise ChangeImpactPolicySequenceError("initial change-impact policy revision must be 1")
        if expected_current_record_id.strip() or expected_current_policy_digest.strip():
            raise ChangeImpactPolicyConflictError(
                "initial change-impact policy expected tip must be absent"
            )
        return False
    if (
        expected_current_record_id.strip() != current.record_id
        or expected_current_policy_digest.strip().lower() != current.policy_digest
    ):
        raise ChangeImpactPolicyConflictError("expected current change-impact policy tip is stale")
    if record.policy_revision != current.policy_revision + 1:
        raise ChangeImpactPolicySequenceError("next change-impact policy revision is not linear")
    if record.supersedes_record_id != current.record_id:
        raise ChangeImpactPolicySequenceError(
            "next change-impact policy must supersede current record"
        )
    if record.effective_at < current.effective_at:
        raise ChangeImpactPolicySequenceError(
            "next change-impact policy effective_at cannot precede current revision"
        )
    return False


def _safe_current_policy(
    records: tuple[ChangeImpactPolicyRecord, ...],
) -> ChangeImpactPolicyRecord | None:
    try:
        return _current_policy(records, repository_id="", evaluated_at=_evaluation_timestamp(""))
    except ValueError:
        return None


def _current_policy(
    records: tuple[ChangeImpactPolicyRecord, ...],
    *,
    repository_id: str,
    evaluated_at: str,
) -> ChangeImpactPolicyRecord | None:
    matching = tuple(
        record
        for record in records
        if (not repository_id or record.repository_id == repository_id)
        and record.effective_at <= evaluated_at
    )
    active = tuple(record for record in matching if record.status == "active")
    if len(active) > 1:
        raise ValueError("multiple active change-impact policies")
    if active:
        return active[0]
    return None


def _policy_matches_target(
    *,
    policy: ChangeImpactPolicyRecord,
    target: object,
) -> bool:
    return bool(
        getattr(target, "repository_id") == policy.repository_id
        and getattr(target, "repository_owner_id") == policy.repository_owner_id
        and getattr(target, "repository") == policy.repository
    )


def _matching_rules(
    *,
    changed_file: ChangeImpactChangedFileEvidence,
    rules: tuple[ChangeImpactComponentRule, ...],
) -> tuple[ChangeImpactComponentRule, ...]:
    return tuple(
        rule
        for rule in rules
        if any(
            changed_file.path == prefix or changed_file.path.startswith(f"{prefix}/")
            for prefix in rule.path_prefixes
        )
    )


def _file_match(
    *,
    changed_file: ChangeImpactChangedFileEvidence,
    rule: ChangeImpactComponentRule,
) -> ChangeImpactMatchedEvidence:
    return ChangeImpactMatchedEvidence(
        source="server_diff",
        path=changed_file.path,
        component=rule.component,
        rule_id=rule.rule_id,
        review_tier=rule.review_tier,
        production_affecting=rule.production_affecting,
        affected_products=rule.affected_products,
        reason=rule.reason,
    )


def _stored_evidence_match(
    *,
    evidence: ChangeImpactStoredEvidence,
    rule: ChangeImpactComponentRule,
) -> ChangeImpactMatchedEvidence:
    return ChangeImpactMatchedEvidence(
        source=(
            "launchplane_dependency" if evidence.kind == "dependency" else "launchplane_reviewer"
        ),
        component=evidence.component,
        rule_id=rule.rule_id,
        review_tier=rule.review_tier,
        production_affecting=rule.production_affecting,
        affected_products=tuple(
            sorted(set(rule.affected_products) | set(evidence.affected_products), key=_scope_key)
        ),
        reason=evidence.reason,
    )


def _affected_products(
    scopes: set[ChangeImpactProductScope] | tuple[ChangeImpactAffectedProduct, ...],
) -> tuple[ChangeImpactAffectedProduct, ...]:
    if not scopes:
        return ()
    affected: list[ChangeImpactAffectedProduct] = []
    for scope in sorted(scopes, key=_scope_key):
        if isinstance(scope, ChangeImpactAffectedProduct):
            affected.append(scope)
        else:
            affected.append(
                ChangeImpactAffectedProduct(
                    product=scope.product,
                    system=scope.system,
                    owner_action=scope.owner_action,
                    owner_environment=scope.owner_environment,
                )
            )
    return tuple(affected)


def _scope_key(
    scope: ChangeImpactProductScope | ChangeImpactAffectedProduct,
) -> tuple[str, str, str, str]:
    return scope.product, scope.system, scope.owner_action, scope.owner_environment


def _unknown_evaluation(
    *,
    target: ChangeImpactTarget,
    status: Literal["unknown", "stale_head", "stale_policy"],
    reason_code: str,
    policy: ChangeImpactPolicyRecord | None = None,
    matched_evidence: tuple[ChangeImpactMatchedEvidence, ...] = (),
    affected_products: tuple[ChangeImpactAffectedProduct, ...] = (),
    unknown_evidence: tuple[str, ...],
    coverage: ChangeImpactCoverage | None = None,
) -> ChangeImpactEvaluation:
    return ChangeImpactEvaluation(
        status=status,
        reason_code=reason_code,
        target=target,
        policy_record_id=policy.record_id if policy is not None else "",
        policy_revision=policy.policy_revision if policy is not None else None,
        policy_digest=policy.policy_digest if policy is not None else "",
        engineering_review_tier="sensitive",
        required_engineering_review_count=2,
        owner_impact="unknown" if not affected_products else "required",
        affected_products=affected_products,
        matched_evidence=matched_evidence,
        unknown_evidence=unknown_evidence,
        coverage=coverage,
    )


def _binding_matches_repository(
    *,
    binding: ChangeImpactTrustedCallerBinding,
    target: ChangeImpactTarget,
) -> bool:
    return bool(
        binding.repository.strip().lower() == target.repository
        and binding.repository_id.strip() == target.repository_id
        and binding.repository_owner_id.strip() == target.repository_owner_id
    )


def _binding_matches_target(
    *,
    binding: ChangeImpactTrustedCallerBinding,
    repository_evidence: ChangeImpactRepositoryEvidence,
) -> bool:
    target = repository_evidence.target
    expected_sha = target.head_sha
    if binding.ref.strip() == f"refs/pull/{target.pull_request_number}/merge":
        expected_sha = repository_evidence.merge_commit_sha
    return bool(expected_sha and binding.head_sha.strip().lower() == expected_sha)


def _evaluation_timestamp(value: str) -> str:
    if not value.strip():
        return (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace(
                "+00:00",
                "Z",
            )
        )
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("evaluated_at must include a timezone")
    return (
        parsed.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace(
            "+00:00",
            "Z",
        )
    )
