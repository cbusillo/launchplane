from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from typing import NamedTuple, Protocol, cast

from control_plane.change_impact_service import (
    ChangeImpactRepositoryEvidenceProvider,
    evaluate_change_impact,
    load_change_impact_stored_evidence,
    require_change_impact_policy_read_store,
)
from control_plane.contracts.change_impact import (
    ChangeImpactAffectedProduct,
    ChangeImpactAuthorshipEvidence,
    ChangeImpactEvaluation,
    ChangeImpactRepositoryEvidence,
    ChangeImpactTargetReference,
)
from control_plane.contracts.owner_acceptance import (
    OWNER_ACCEPTANCE_STATUS_PRECEDENCE,
    OwnerAcceptanceAction,
    OwnerAcceptanceAuthorization,
    OwnerAcceptanceBinding,
    OwnerAcceptanceContributionBinding,
    OwnerAcceptanceDecision,
    OwnerAcceptanceDecisionStatus,
    OwnerAcceptanceEventRecord,
    OwnerAcceptanceEventWriteStatus,
    OwnerAcceptancePolicyFingerprintBinding,
    OwnerAcceptancePreviewBinding,
    OwnerAcceptancePreviewIsolationBinding,
    OwnerAcceptanceProductDecision,
    OwnerAcceptanceReasonCode,
    OwnerAcceptanceReviewContext,
    OwnerAcceptanceResolutionEvidence,
    OwnerAcceptanceSourceEventKind,
    OwnerAcceptanceViewerBindingEligibility,
    OwnerAcceptanceViewerEligibilityReason,
    owner_acceptance_event_replay_matches,
    owner_acceptance_bindings_match,
    owner_acceptance_human_action_semantics,
    owner_acceptance_runtime_identity_binding,
    validate_owner_acceptance_event_transition,
)
from control_plane.contracts.preview_generation_record import PreviewGenerationRecord
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.product_owner import (
    ProductOwnerActionContext,
    ProductOwnerActorIdentity,
    ProductOwnerPolicyRecord,
    ProductOwnerPreviewIsolationClass,
    ProductOwnerRequirementRecord,
    ProductOwnerReviewChangeClass,
    product_owner_scoped_policy_fingerprint,
)
from control_plane.product_owner_service import (
    evaluate_product_owner_authority,
    require_product_owner_policy_read_store,
    require_product_owner_requirement_read_store,
)
from control_plane.service_auth import GitHubHumanIdentity, LaunchplaneIdentity
from control_plane.preview_serving_evidence import (
    verify_serving_preview,
)


class OwnerAcceptanceEventConflictError(RuntimeError):
    """Raised when an immutable Owner acceptance event replay changes payload."""


class OwnerAcceptanceAuthorizationError(PermissionError):
    """Raised when a caller cannot author a human Owner acceptance event."""


class OwnerAcceptanceBindingConflictError(RuntimeError):
    """Raised when the current exact binding differs from the Owner-reviewed binding."""


class OwnerAcceptanceEvaluationUnavailableError(RuntimeError):
    """Raised when server-owned exact-change evidence cannot be resolved."""


class OwnerAcceptanceSelfReviewDeniedError(PermissionError):
    """Raised when the acting Owner contributed to the reviewed change."""


class OwnerAcceptanceReviewContextUnavailableError(OwnerAcceptanceEvaluationUnavailableError):
    """Raised when server-owned reviewed base/authorship context cannot be bound."""


class OwnerAcceptanceAuthorityDeniedError(PermissionError):
    """Raised when configured Owner authority explicitly excludes the target scope."""


_PREVIEW_ISOLATION_BY_TRANSPORT_MODE: dict[str, ProductOwnerPreviewIsolationClass] = {
    "none": "no_product_data",
    "bootstrap": "synthetic_seeded",
    "migrate_seed": "synthetic_seeded",
    "clone": "cloned_product_data",
    "driver": "unknown",
}


class OwnerAcceptanceEventStore(Protocol):
    def write_owner_acceptance_event_record(
        self,
        record: OwnerAcceptanceEventRecord,
    ) -> OwnerAcceptanceEventWriteStatus: ...

    def list_owner_acceptance_event_records(
        self,
        *,
        repository_id: str = "",
        repository: str = "",
        pull_request_number: int | None = None,
        product: str = "",
        system: str = "",
        action: str = "",
        acceptance_action: str = "",
        limit: int | None = None,
    ) -> tuple[OwnerAcceptanceEventRecord, ...]: ...

    def read_owner_acceptance_event_record(
        self,
        event_id: str,
    ) -> OwnerAcceptanceEventRecord: ...


class OwnerAcceptanceProjectionLockStore(Protocol):
    def owner_acceptance_projection_lock(
        self,
        *,
        repository_id: str,
        pull_request_number: int,
    ) -> AbstractContextManager[None]: ...


class OwnerAcceptancePreviewReadStore(Protocol):
    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord: ...

    def list_preview_records(
        self,
        *,
        context_name: str = "",
        anchor_repo: str = "",
        anchor_pr_number: int | None = None,
        limit: int | None = None,
    ) -> tuple[PreviewRecord, ...]: ...

    def read_preview_generation_record(self, generation_id: str) -> PreviewGenerationRecord: ...


class OwnerAcceptanceWriteResult(NamedTuple):
    status: OwnerAcceptanceEventWriteStatus
    record: OwnerAcceptanceEventRecord
    decision: OwnerAcceptanceDecision


def require_owner_acceptance_projection_lock_store(
    store: object,
) -> OwnerAcceptanceProjectionLockStore:
    if callable(getattr(store, "owner_acceptance_projection_lock", None)):
        return cast(OwnerAcceptanceProjectionLockStore, store)
    raise TypeError("record store does not support Owner acceptance projection locking")


class OwnerAcceptanceImpactEvidence(NamedTuple):
    repository_evidence: ChangeImpactRepositoryEvidence
    impact: ChangeImpactEvaluation


class OwnerAcceptanceBoundSubject(NamedTuple):
    binding: OwnerAcceptanceBinding
    policy: ProductOwnerPolicyRecord


class OwnerAcceptancePreviewEvidence(NamedTuple):
    preview: OwnerAcceptancePreviewBinding | None
    isolation: OwnerAcceptancePreviewIsolationBinding


def evaluate_owner_acceptance_viewer_eligibility(
    *,
    store: object,
    decisions: tuple[OwnerAcceptanceDecision, ...],
    identity: LaunchplaneIdentity,
) -> tuple[OwnerAcceptanceViewerBindingEligibility, ...]:
    actor = (
        ProductOwnerActorIdentity(
            provider="github",
            provider_subject_id=str(identity.github_id),
        )
        if isinstance(identity, GitHubHumanIdentity)
        else None
    )
    policy_store = None
    requirement_store = None
    if actor is not None:
        try:
            policy_store = require_product_owner_policy_read_store(store)
            requirement_store = require_product_owner_requirement_read_store(store)
        except TypeError:
            pass

    policy_cache: dict[tuple[str, str], tuple[ProductOwnerPolicyRecord, ...]] = {}
    requirement_cache: dict[tuple[str, str], tuple[ProductOwnerRequirementRecord, ...]] = {}
    seen_bindings: set[str] = set()
    eligibility: list[OwnerAcceptanceViewerBindingEligibility] = []
    for decision in decisions:
        bindings = tuple(
            product.binding for product in decision.products if product.binding is not None
        )
        if decision.binding is not None:
            bindings = (*bindings, decision.binding)
        for binding in bindings:
            if binding.binding_sha256 in seen_bindings:
                continue
            seen_bindings.add(binding.binding_sha256)
            can_submit_event = False
            can_accept = False
            can_request_changes = False
            can_revoke = False
            reason_code: OwnerAcceptanceViewerEligibilityReason = "viewer_identity_unsupported"
            if actor is not None and policy_store is not None and requirement_store is not None:
                cache_key = (binding.product, binding.system)
                try:
                    if cache_key not in policy_cache:
                        policy_cache[cache_key] = policy_store.list_product_owner_policy_records(
                            product=binding.product,
                            system=binding.system,
                        )
                    if cache_key not in requirement_cache:
                        requirement_cache[cache_key] = (
                            requirement_store.list_product_owner_requirement_records(
                                product=binding.product,
                                system=binding.system,
                            )
                        )
                    policies = policy_cache[cache_key]
                    requirements = requirement_cache[cache_key]
                    authority = evaluate_product_owner_authority(
                        context=ProductOwnerActionContext(
                            product=binding.product,
                            system=binding.system,
                            repository_id=binding.repository_id,
                            environment=binding.environment,
                            action=binding.action,
                        ),
                        actor=actor,
                        policies=policies,
                        requirements=requirements,
                        routings=(),
                        claimed_policy_revision=binding.owner_policy_revision,
                        claimed_policy_digest=binding.owner_policy_digest,
                        claimed_requirement_revision=binding.owner_requirement_revision,
                        claimed_requirement_digest=binding.owner_requirement_digest,
                        evaluated_at=decision.evaluated_at,
                    )
                except (FileNotFoundError, LookupError, TypeError, ValueError):
                    reason_code = "owner_authority_unavailable"
                else:
                    if authority.decision == "authorized":
                        can_submit_event = True
                        reason_code = "current_product_owner"
                    elif authority.reason_code == "actor_not_current_owner":
                        reason_code = "not_current_product_owner"
                    else:
                        reason_code = "owner_authority_unavailable"
                    can_accept = can_submit_event
                    can_request_changes = can_submit_event
                    can_revoke = can_submit_event
                    if can_accept and not _viewer_may_self_review(
                        binding=binding,
                        policies=policies,
                        viewer_github_id=int(actor.provider_subject_id),
                    ):
                        can_accept = False
                        reason_code = "self_review_denied"
            elif actor is not None:
                reason_code = "owner_authority_unavailable"
            if not can_submit_event:
                can_accept = False
                can_request_changes = False
                can_revoke = False
            eligibility.append(
                OwnerAcceptanceViewerBindingEligibility(
                    binding_sha256=binding.binding_sha256,
                    product=binding.product,
                    system=binding.system,
                    action=binding.action,
                    environment=binding.environment,
                    can_submit_event=can_submit_event,
                    can_accept=can_accept,
                    can_request_changes=can_request_changes,
                    can_revoke=can_revoke,
                    reason_code=reason_code,
                )
            )
    return tuple(eligibility)


def evaluate_owner_acceptance(
    *,
    store: object,
    repository_evidence_provider: ChangeImpactRepositoryEvidenceProvider,
    target: ChangeImpactTargetReference,
    evaluated_at: str = "",
) -> OwnerAcceptanceDecision:
    normalized_evaluated_at = _evaluation_timestamp(evaluated_at)
    evidence = _evaluate_change_impact_for_target(
        store=store,
        repository_evidence_provider=repository_evidence_provider,
        target=target,
        evaluated_at=normalized_evaluated_at,
    )
    impact = evidence.impact
    if impact.status == "stale_head":
        decision = _decision(
            status="stale", reason_code="change_impact_stale", evaluated_at=normalized_evaluated_at
        )
    elif impact.status != "success":
        decision = _decision(
            status="unavailable",
            reason_code="change_impact_unavailable",
            evaluated_at=normalized_evaluated_at,
        )
    elif impact.owner_impact != "required":
        decision = _decision(
            status="not_required",
            reason_code="engineering_only",
            evaluated_at=normalized_evaluated_at,
        )
    elif not impact.affected_products:
        decision = _decision(
            status="unavailable",
            reason_code="change_impact_unavailable",
            evaluated_at=normalized_evaluated_at,
        )
    else:
        decision = _evaluate_owner_acceptance_for_impact(
            evidence=evidence,
            store=store,
            evaluated_at=normalized_evaluated_at,
        )
    # Copy the already validated diagnostic without resolving authority again.
    return decision.model_copy(update={"change_impact_coverage": impact.coverage})


def record_owner_acceptance_event(
    *,
    store: object,
    repository_evidence_provider: ChangeImpactRepositoryEvidenceProvider,
    target: ChangeImpactTargetReference,
    identity: LaunchplaneIdentity,
    action: OwnerAcceptanceAction,
    expected_binding_sha256: str,
    source_event_kind: OwnerAcceptanceSourceEventKind,
    source_event_id: str,
    reason: str = "",
    resolution: OwnerAcceptanceResolutionEvidence | None = None,
    occurred_at: str = "",
    before_write: Callable[[OwnerAcceptanceEventRecord], None] | None = None,
) -> OwnerAcceptanceWriteResult:
    if action not in {"accepted", "changes_requested", "revoked"}:
        raise OwnerAcceptanceAuthorizationError("Human route cannot write system-only events.")
    if not isinstance(identity, GitHubHumanIdentity):
        raise OwnerAcceptanceAuthorizationError("Owner acceptance requires a GitHub human session.")
    normalized_occurred_at = _evaluation_timestamp(occurred_at)
    evidence = _evaluate_change_impact_for_target(
        store=store,
        repository_evidence_provider=repository_evidence_provider,
        target=target,
        evaluated_at=normalized_occurred_at,
    )
    impact = evidence.impact
    if impact.status != "success":
        raise OwnerAcceptanceEvaluationUnavailableError(
            "Owner acceptance requires current server-derived change-impact evidence."
        )
    if impact.owner_impact != "required":
        raise OwnerAcceptanceBindingConflictError(
            "Owner acceptance binding changed; evaluate the exact change again before recording."
        )
    if not impact.affected_products:
        raise OwnerAcceptanceBindingConflictError(
            "Owner acceptance binding changed; evaluate the exact change again before recording."
        )
    event_store = require_owner_acceptance_event_store(store)
    target_events = event_store.list_owner_acceptance_event_records(
        repository_id=impact.target.repository_id,
        pull_request_number=impact.target.pull_request_number,
    )
    prewrite_decision = _evaluate_owner_acceptance_for_impact(
        evidence=evidence,
        store=store,
        evaluated_at=normalized_occurred_at,
        events=target_events,
    )
    normalized_expected_binding_sha256 = expected_binding_sha256.strip().lower()
    current_matches = tuple(
        product_index
        for product_index, product_decision in enumerate(prewrite_decision.products)
        if product_decision.binding is not None
        and product_decision.binding.binding_sha256 == normalized_expected_binding_sha256
    )
    selected_product_index = current_matches[0] if len(current_matches) == 1 else None
    if selected_product_index is None:
        historical_subjects = {
            (
                event.binding.product,
                event.binding.system,
                event.binding.action,
                event.binding.environment,
            )
            for event in target_events
            if event.binding.binding_sha256 == normalized_expected_binding_sha256
        }
        historical_matches = tuple(
            product_index
            for product_index, affected_product in enumerate(impact.affected_products)
            if (
                affected_product.product,
                affected_product.system,
                affected_product.owner_action,
                affected_product.owner_environment,
            )
            in historical_subjects
        )
        if len(historical_matches) == 1:
            selected_product_index = historical_matches[0]
    if selected_product_index is None:
        raise OwnerAcceptanceBindingConflictError(
            "Owner acceptance binding changed; evaluate the exact change again before recording."
        )
    actor = ProductOwnerActorIdentity(
        provider="github",
        provider_subject_id=str(identity.github_id),
    )
    subject = _binding_from_impact(
        evidence=evidence,
        product_index=selected_product_index,
        store=store,
        actor=actor,
        evaluated_at=normalized_occurred_at,
        expected_binding_sha256=expected_binding_sha256,
    )
    if subject is None:
        raise OwnerAcceptanceEvaluationUnavailableError(
            "Owner acceptance cannot bind unavailable Owner authority."
        )
    binding = subject.binding
    self_review = _resolve_self_review(
        binding=binding,
        policy=subject.policy,
        acting_github_id=identity.github_id,
        action=action,
    )
    authorization = OwnerAcceptanceAuthorization(
        owner_identity_id=actor.identity_id,
        owner_github_id=identity.github_id,
        owner_login=identity.login,
        owner_policy_record_id=binding.owner_policy_record_id,
        owner_policy_revision=binding.owner_policy_revision,
        owner_policy_digest=binding.owner_policy_digest,
        owner_requirement_record_id=binding.owner_requirement_record_id,
        owner_requirement_revision=binding.owner_requirement_revision,
        owner_requirement_digest=binding.owner_requirement_digest,
        authorized_at=normalized_occurred_at,
        self_review=self_review,
        self_review_exception_revision=(
            subject.policy.self_review.exception_revision
            if self_review and action == "accepted"
            else 0
        ),
    )
    record = OwnerAcceptanceEventRecord(
        binding=binding,
        action=action,
        occurred_at=normalized_occurred_at,
        source_event_kind=source_event_kind,
        source_event_id=source_event_id,
        reason=reason,
        resolution=resolution,
        authorization=authorization,
    )
    prior_events = tuple(
        event
        for event in event_store.list_owner_acceptance_event_records(
            repository_id=binding.repository_id,
            pull_request_number=binding.pull_request_number,
            product=binding.product,
            system=binding.system,
            action=binding.action,
        )
        if event.binding.environment == binding.environment
    )
    if binding.preview is None and any(event.binding.preview is not None for event in prior_events):
        raise OwnerAcceptanceEvaluationUnavailableError(
            "Preview-bound Owner acceptance cannot downgrade to an exact-change-only binding."
        )
    existing = next(
        (event for event in target_events if event.event_id == record.event_id),
        None,
    )
    if existing is not None:
        if not owner_acceptance_event_replay_matches(existing, record):
            raise OwnerAcceptanceEventConflictError(
                "Owner acceptance event replay changed the persisted payload."
            )
    else:
        previous = (
            max(prior_events, key=lambda event: event.subject_sequence) if prior_events else None
        )
        validate_owner_acceptance_event_transition(
            previous=previous,
            proposed=record.model_copy(
                update={
                    "subject_sequence": (
                        previous.subject_sequence + 1 if previous is not None else 1
                    )
                }
            ),
        )
    if before_write is not None:
        before_write(record)
    write_status = event_store.write_owner_acceptance_event_record(record)
    record = event_store.read_owner_acceptance_event_record(record.event_id)
    folded_events = (
        prior_events
        if any(event.event_id == record.event_id for event in prior_events)
        else (*prior_events, record)
    )
    selected_decision = evaluate_owner_acceptance_for_binding(
        binding=binding,
        events=folded_events,
        evaluated_at=normalized_occurred_at,
        policy=subject.policy,
    )
    product_decisions = list(prewrite_decision.products)
    product_decisions[selected_product_index] = _product_decision(
        affected_product=impact.affected_products[selected_product_index],
        decision=selected_decision,
    )
    decision = aggregate_owner_acceptance_decision(
        products=tuple(product_decisions),
        evaluated_at=normalized_occurred_at,
    )
    return OwnerAcceptanceWriteResult(
        status=write_status,
        record=record,
        decision=decision,
    )


def build_owner_acceptance_system_event(
    *,
    binding: OwnerAcceptanceBinding,
    action: OwnerAcceptanceAction,
    occurred_at: str,
    source_event_id: str,
    reason: str,
) -> OwnerAcceptanceEventRecord:
    if action not in {"superseded", "invalidated"}:
        raise ValueError("Only system Owner acceptance actions can be built here.")
    return OwnerAcceptanceEventRecord(
        binding=binding,
        action=action,
        occurred_at=occurred_at,
        source_event_kind="system",
        source_event_id=source_event_id,
        reason=reason,
    )


def evaluate_owner_acceptance_for_binding(
    *,
    binding: OwnerAcceptanceBinding,
    events: tuple[OwnerAcceptanceEventRecord, ...],
    evaluated_at: str = "",
    policy: ProductOwnerPolicyRecord | None = None,
) -> OwnerAcceptanceDecision:
    normalized_evaluated_at = _evaluation_timestamp(evaluated_at)
    matching = tuple(
        event for event in events if owner_acceptance_bindings_match(event.binding, binding)
    )
    if not matching:
        stale_events = events
        if stale_events:
            latest_stale = _latest_owner_acceptance_event(stale_events)
            return _decision(
                status="stale",
                reason_code="acceptance_stale",
                binding=binding,
                event=latest_stale,
                evaluated_at=normalized_evaluated_at,
            )
        return _decision(
            status="pending",
            reason_code="acceptance_missing",
            binding=binding,
            evaluated_at=normalized_evaluated_at,
        )
    latest = _latest_owner_acceptance_event(matching)
    if latest.action == "accepted":
        inadmissible_reason = _inadmissible_reason(
            binding=binding,
            event=latest,
            policy=policy,
            evaluated_at=normalized_evaluated_at,
        )
        if inadmissible_reason is not None:
            status, reason_code = inadmissible_reason
            return _decision(
                status=status,
                reason_code=reason_code,
                binding=binding,
                event=latest,
                evaluated_at=normalized_evaluated_at,
            )
        return _decision(
            status="accepted",
            reason_code="acceptance_valid",
            binding=binding,
            event=latest,
            evaluated_at=normalized_evaluated_at,
            admissible=True,
        )
    if latest.action == "changes_requested":
        return _decision(
            status="changes_requested",
            reason_code="changes_requested",
            binding=binding,
            event=latest,
            evaluated_at=normalized_evaluated_at,
        )
    if latest.action == "revoked":
        return _decision(
            status="revoked",
            reason_code="acceptance_revoked",
            binding=binding,
            event=latest,
            evaluated_at=normalized_evaluated_at,
        )
    return _decision(
        status="stale",
        reason_code="acceptance_stale",
        binding=binding,
        event=latest,
        evaluated_at=normalized_evaluated_at,
    )


def aggregate_owner_acceptance_decision(
    *,
    products: tuple[OwnerAcceptanceProductDecision, ...],
    evaluated_at: str,
) -> OwnerAcceptanceDecision:
    if not products:
        raise ValueError("Owner acceptance aggregate requires at least one product decision.")
    precedence = {status: index for index, status in enumerate(OWNER_ACCEPTANCE_STATUS_PRECEDENCE)}
    _, governing = min(
        enumerate(products),
        key=lambda item: (precedence[item[1].status], item[0]),
    )
    return _decision(
        status=governing.status,
        reason_code=governing.reason_code,
        binding=governing.binding,
        event=governing.current_event,
        products=products,
        evaluated_at=evaluated_at,
        admissible=all(product.admissible for product in products),
    )


def _evaluate_owner_acceptance_for_impact(
    *,
    evidence: OwnerAcceptanceImpactEvidence,
    store: object,
    evaluated_at: str,
    events: tuple[OwnerAcceptanceEventRecord, ...] | None = None,
) -> OwnerAcceptanceDecision:
    impact = evidence.impact
    resolved_events = events
    if resolved_events is None:
        resolved_events = require_owner_acceptance_event_store(
            store
        ).list_owner_acceptance_event_records(
            repository_id=impact.target.repository_id,
            pull_request_number=impact.target.pull_request_number,
        )
    products = tuple(
        _evaluate_owner_acceptance_product(
            evidence=evidence,
            affected_product=affected_product,
            product_index=product_index,
            store=store,
            events=resolved_events,
            evaluated_at=evaluated_at,
        )
        for product_index, affected_product in enumerate(impact.affected_products)
    )
    return aggregate_owner_acceptance_decision(products=products, evaluated_at=evaluated_at)


def _evaluate_owner_acceptance_product(
    *,
    evidence: OwnerAcceptanceImpactEvidence,
    affected_product: ChangeImpactAffectedProduct,
    product_index: int,
    store: object,
    events: tuple[OwnerAcceptanceEventRecord, ...],
    evaluated_at: str,
) -> OwnerAcceptanceProductDecision:
    product_events = tuple(
        event
        for event in events
        if event.binding.product == affected_product.product
        and event.binding.system == affected_product.system
        and event.binding.action == affected_product.owner_action
        and event.binding.environment == affected_product.owner_environment
    )
    try:
        subject = _binding_from_impact(
            evidence=evidence,
            product_index=product_index,
            store=store,
            actor=None,
            evaluated_at=evaluated_at,
        )
    except OwnerAcceptanceReviewContextUnavailableError:
        return _product_decision(
            affected_product=affected_product,
            decision=_decision(
                status="unavailable",
                reason_code="review_context_missing",
                evaluated_at=evaluated_at,
            ),
        )
    except OwnerAcceptanceEvaluationUnavailableError:
        preview_events = tuple(
            event for event in product_events if event.binding.preview is not None
        )
        if preview_events:
            current_event = _latest_owner_acceptance_event(preview_events)
            decision = _decision(
                status="stale",
                reason_code="preview_evidence_stale",
                binding=current_event.binding,
                event=current_event,
                evaluated_at=evaluated_at,
            )
        else:
            decision = _decision(
                status="unavailable",
                reason_code="preview_evidence_unavailable",
                evaluated_at=evaluated_at,
            )
        return _product_decision(affected_product=affected_product, decision=decision)
    except OwnerAcceptanceAuthorityDeniedError:
        return _product_decision(
            affected_product=affected_product,
            decision=_decision(
                status="unavailable",
                reason_code="owner_authority_denied",
                evaluated_at=evaluated_at,
            ),
        )
    if subject is None:
        return _product_decision(
            affected_product=affected_product,
            decision=_decision(
                status="unavailable",
                reason_code="owner_authority_unavailable",
                evaluated_at=evaluated_at,
            ),
        )
    binding = subject.binding
    effective_preview_events = tuple(
        event for event in product_events if event.binding.preview is not None
    )
    if binding.preview is None and effective_preview_events:
        decision = _decision(
            status="stale",
            reason_code="preview_evidence_stale",
            binding=binding,
            event=_latest_owner_acceptance_event(effective_preview_events),
            evaluated_at=evaluated_at,
        )
    else:
        decision = evaluate_owner_acceptance_for_binding(
            binding=binding,
            events=product_events,
            evaluated_at=evaluated_at,
            policy=subject.policy,
        )
    return _product_decision(affected_product=affected_product, decision=decision)


def _product_decision(
    *,
    affected_product: ChangeImpactAffectedProduct,
    decision: OwnerAcceptanceDecision,
) -> OwnerAcceptanceProductDecision:
    return OwnerAcceptanceProductDecision(
        product=affected_product.product,
        system=affected_product.system,
        action=affected_product.owner_action,
        environment=affected_product.owner_environment,
        status=decision.status,
        reason_code=decision.reason_code,
        binding=decision.binding,
        current_event=decision.current_event,
        admissible=decision.admissible,
        human_action_semantics=decision.human_action_semantics,
    )


def require_owner_acceptance_event_store(store: object) -> OwnerAcceptanceEventStore:
    if not callable(getattr(store, "write_owner_acceptance_event_record", None)):
        raise TypeError("Owner acceptance event storage is unavailable.")
    if not callable(getattr(store, "list_owner_acceptance_event_records", None)):
        raise TypeError("Owner acceptance event storage is unavailable.")
    if not callable(getattr(store, "read_owner_acceptance_event_record", None)):
        raise TypeError("Owner acceptance event storage is unavailable.")
    return cast(OwnerAcceptanceEventStore, store)


def require_owner_acceptance_preview_read_store(store: object) -> OwnerAcceptancePreviewReadStore:
    for method_name in (
        "read_product_profile_record",
        "list_preview_records",
        "read_preview_generation_record",
    ):
        if not callable(getattr(store, method_name, None)):
            raise TypeError("Owner acceptance preview evidence storage is unavailable.")
    return cast(OwnerAcceptancePreviewReadStore, store)


def _evaluate_change_impact_for_target(
    *,
    store: object,
    repository_evidence_provider: ChangeImpactRepositoryEvidenceProvider,
    target: ChangeImpactTargetReference,
    evaluated_at: str,
) -> OwnerAcceptanceImpactEvidence:
    try:
        repository_evidence = repository_evidence_provider.resolve(target)
    except Exception as error:
        raise OwnerAcceptanceEvaluationUnavailableError(str(error)) from error
    policy_store = require_change_impact_policy_read_store(store)
    stored_evidence = load_change_impact_stored_evidence(
        store=store,
        target=repository_evidence.target,
    )
    return OwnerAcceptanceImpactEvidence(
        repository_evidence=repository_evidence,
        impact=evaluate_change_impact(
            repository_evidence=repository_evidence,
            policies=policy_store.list_change_impact_policy_records(
                repository_id=repository_evidence.target.repository_id,
                status="active",
            ),
            stored_evidence=stored_evidence,
            evaluated_at=evaluated_at,
        ),
    )


def _binding_from_impact(
    *,
    evidence: OwnerAcceptanceImpactEvidence,
    product_index: int,
    store: object,
    actor: ProductOwnerActorIdentity | None,
    evaluated_at: str,
    expected_binding_sha256: str = "",
) -> OwnerAcceptanceBoundSubject | None:
    impact = evidence.impact
    if impact.policy_revision is None or not impact.policy_digest or not impact.policy_record_id:
        return None
    if product_index >= len(impact.affected_products):
        return None
    affected_product = impact.affected_products[product_index]
    context = ProductOwnerActionContext(
        product=affected_product.product,
        system=affected_product.system,
        repository_id=impact.target.repository_id,
        environment=affected_product.owner_environment,
        action=affected_product.owner_action,
    )
    policy_store = require_product_owner_policy_read_store(store)
    requirement_store = require_product_owner_requirement_read_store(store)
    policies = policy_store.list_product_owner_policy_records(
        product=context.product,
        system=context.system,
    )
    requirements = requirement_store.list_product_owner_requirement_records(
        product=context.product,
        system=context.system,
    )
    active_policies = tuple(record for record in policies if record.status == "active")
    active_requirements = tuple(record for record in requirements if record.status == "active")
    if len(active_policies) != 1 or len(active_requirements) != 1:
        if expected_binding_sha256:
            raise OwnerAcceptanceBindingConflictError(
                "Owner acceptance binding changed; evaluate the exact change again before recording."
            )
        return None
    if not active_policies[0].owners:
        if expected_binding_sha256:
            raise OwnerAcceptanceBindingConflictError(
                "Owner acceptance binding changed; evaluate the exact change again before recording."
            )
        return None
    owner_actors = tuple(
        ProductOwnerActorIdentity(
            provider=owner.identity.provider,
            provider_subject_id=owner.identity.provider_subject_id,
        )
        for owner in active_policies[0].owners
    )
    authority = None
    authority_denied = False
    for owner_actor in owner_actors:
        candidate = evaluate_product_owner_authority(
            context=context,
            actor=owner_actor,
            policies=policies,
            requirements=requirements,
            routings=(),
            claimed_policy_revision=active_policies[0].policy_revision,
            claimed_policy_digest=active_policies[0].policy_digest,
            claimed_requirement_revision=active_requirements[0].requirement_revision,
            claimed_requirement_digest=active_requirements[0].requirement_digest,
            evaluated_at=evaluated_at,
        )
        if candidate.decision == "authorized":
            authority = candidate
            break
        if candidate.decision == "denied" or candidate.reason_code == "policy_scope_not_covered":
            authority_denied = True
    if authority is None:
        if expected_binding_sha256:
            raise OwnerAcceptanceBindingConflictError(
                "Owner acceptance binding changed; evaluate the exact change again before recording."
            )
        if authority_denied:
            raise OwnerAcceptanceAuthorityDeniedError(
                "Configured product Owner authority does not cover this exact scope."
            )
        return None
    if (
        not authority.policy_record_id
        or authority.policy_revision is None
        or not authority.policy_digest
        or not authority.requirement_record_id
        or authority.requirement_revision is None
        or not authority.requirement_digest
    ):
        return None
    policy = active_policies[0]
    requirement = active_requirements[0]
    preview_evidence = _resolve_owner_acceptance_preview_binding(
        store=store,
        product=context.product,
        repository=impact.target.repository,
        pull_request_number=impact.target.pull_request_number,
        head_sha=impact.target.head_sha,
    )
    preview = preview_evidence.preview
    review_context = _review_context(
        evidence=evidence,
        context=context,
        affected_product=affected_product,
        policy=policy,
        requirement=requirement,
        preview_isolation=preview_evidence.isolation,
    )
    binding = OwnerAcceptanceBinding(
        repository_id=impact.target.repository_id,
        repository_owner_id=impact.target.repository_owner_id,
        repository=impact.target.repository,
        pull_request_number=impact.target.pull_request_number,
        head_sha=impact.target.head_sha,
        tree_sha=impact.target.tree_sha,
        change_impact_policy_record_id=impact.policy_record_id,
        change_impact_policy_revision=impact.policy_revision,
        change_impact_policy_digest=impact.policy_digest,
        binding_hash_version=impact.binding_hash_version,
        change_impact_decision_digest=impact.change_impact_decision_digest,
        product=context.product,
        system=context.system,
        action=context.action,
        environment=context.environment,
        owner_policy_record_id=authority.policy_record_id,
        owner_policy_revision=authority.policy_revision,
        owner_policy_digest=authority.policy_digest,
        owner_requirement_record_id=authority.requirement_record_id,
        owner_requirement_revision=authority.requirement_revision,
        owner_requirement_digest=authority.requirement_digest,
        preview=preview,
        review_context=review_context,
    )
    if (
        expected_binding_sha256
        and expected_binding_sha256.strip().lower() != binding.binding_sha256
    ):
        raise OwnerAcceptanceBindingConflictError(
            "Owner acceptance binding changed; evaluate the exact change again before recording."
        )
    if actor is not None:
        actor_authority = evaluate_product_owner_authority(
            context=context,
            actor=actor,
            policies=policies,
            requirements=requirements,
            routings=(),
            claimed_policy_revision=active_policies[0].policy_revision,
            claimed_policy_digest=active_policies[0].policy_digest,
            claimed_requirement_revision=active_requirements[0].requirement_revision,
            claimed_requirement_digest=active_requirements[0].requirement_digest,
            evaluated_at=evaluated_at,
        )
        if actor_authority.decision != "authorized":
            raise OwnerAcceptanceAuthorizationError("Caller is not a current product Owner.")
    return OwnerAcceptanceBoundSubject(binding=binding, policy=policy)


def _viewer_may_self_review(
    *,
    binding: OwnerAcceptanceBinding,
    policies: tuple[ProductOwnerPolicyRecord, ...],
    viewer_github_id: int,
) -> bool:
    """Advisory viewer check mirroring the fail-closed write-time self-review rule."""
    review_context = binding.review_context
    if review_context is None:
        return False
    contributions = review_context.contributions
    if contributions.resolution != "resolved":
        return False
    if not contributions.includes(viewer_github_id):
        return True
    active = tuple(record for record in policies if record.status == "active")
    if len(active) != 1:
        return False
    return active[0].self_review.allows(review_context.change_class)


def _resolve_self_review(
    *,
    binding: OwnerAcceptanceBinding,
    policy: ProductOwnerPolicyRecord,
    acting_github_id: int,
    action: OwnerAcceptanceAction,
) -> bool:
    """Decide whether this human may record review of a change they contributed to.

    Self-review is denied by default for ``accepted``. A revisioned product policy
    exception may permit it for routine changes only; sensitive, unknown, and
    production-affecting changes are always denied, and unresolved or conflicting
    contributing-identity evidence denies every actor.

    ``changes_requested`` and ``revoked`` are never blocked. They withdraw or
    withhold product judgment rather than asserting it, so denying them would trap
    stale evidence instead of failing closed.
    """
    review_context = binding.review_context
    if review_context is None:
        raise OwnerAcceptanceEvaluationUnavailableError(
            "Owner acceptance requires bound reviewed context before recording an event."
        )
    contributions = review_context.contributions
    if contributions.resolution != "resolved":
        if action == "accepted":
            raise OwnerAcceptanceSelfReviewDeniedError(
                "Owner acceptance cannot resolve contributing GitHub identities for this change."
            )
        return False
    if not contributions.includes(acting_github_id):
        return False
    if action == "accepted" and not policy.self_review.allows(review_context.change_class):
        raise OwnerAcceptanceSelfReviewDeniedError(
            "Owner acceptance denies self-review for this product change."
        )
    return True


def _no_preview_evidence() -> "OwnerAcceptancePreviewEvidence":
    return OwnerAcceptancePreviewEvidence(
        preview=None,
        isolation=OwnerAcceptancePreviewIsolationBinding(
            isolation_class="not_applicable",
            source="no_preview_binding",
        ),
    )


def _change_class(
    *,
    impact: ChangeImpactEvaluation,
    affected_product: ChangeImpactAffectedProduct,
) -> ProductOwnerReviewChangeClass:
    """Classify one affected product subject for review-age and self-review policy."""
    if impact.status != "success" or impact.unknown_evidence:
        return "unknown"
    subject = (
        affected_product.product,
        affected_product.system,
        affected_product.owner_action,
        affected_product.owner_environment,
    )
    for scope in impact.production_affecting_products:
        if (scope.product, scope.system, scope.owner_action, scope.owner_environment) == subject:
            return "production_affecting"
    if impact.engineering_review_tier == "sensitive":
        return "sensitive"
    return "routine"


def _contribution_binding(
    authorship: ChangeImpactAuthorshipEvidence | None,
) -> OwnerAcceptanceContributionBinding:
    if authorship is None:
        return OwnerAcceptanceContributionBinding(
            resolution="unknown",
            reason_code="identity_evidence_unavailable",
        )
    if authorship.resolution == "resolved":
        return OwnerAcceptanceContributionBinding(
            resolution="resolved",
            reason_code="server_resolved",
            contributor_github_ids=authorship.contributor_github_ids,
            commit_count=authorship.commit_count,
        )
    if authorship.resolution == "conflicting":
        return OwnerAcceptanceContributionBinding(
            resolution="unknown",
            reason_code="identity_evidence_conflicting",
            commit_count=authorship.commit_count,
        )
    return OwnerAcceptanceContributionBinding(
        resolution="unknown",
        reason_code="identity_evidence_incomplete",
        commit_count=authorship.commit_count,
    )


def _policy_fingerprints(
    *,
    context: ProductOwnerActionContext,
    policy: ProductOwnerPolicyRecord,
    requirement: ProductOwnerRequirementRecord,
) -> OwnerAcceptancePolicyFingerprintBinding:
    return OwnerAcceptancePolicyFingerprintBinding(
        owner_membership_fingerprint=product_owner_scoped_policy_fingerprint(
            dimension="owner_membership",
            context=context,
            record_id=policy.record_id,
            revision=policy.policy_revision,
            payload=policy.policy_digest,
        ),
        self_review_fingerprint=product_owner_scoped_policy_fingerprint(
            dimension="self_review",
            context=context,
            record_id=policy.record_id,
            revision=policy.policy_revision,
            payload=policy.self_review.model_dump(mode="json"),
        ),
        review_age_fingerprint=product_owner_scoped_policy_fingerprint(
            dimension="review_age",
            context=context,
            record_id=policy.record_id,
            revision=policy.policy_revision,
            payload=policy.review_age.model_dump(mode="json"),
        ),
        requirement_fingerprint=product_owner_scoped_policy_fingerprint(
            dimension="requirement",
            context=context,
            record_id=requirement.record_id,
            revision=requirement.requirement_revision,
            payload=requirement.requirement_digest,
        ),
        preview_trust_fingerprint=product_owner_scoped_policy_fingerprint(
            dimension="preview_trust",
            context=context,
            record_id=policy.record_id,
            revision=policy.policy_revision,
            payload=policy.preview_trust.model_dump(mode="json"),
        ),
    )


def _review_context(
    *,
    evidence: OwnerAcceptanceImpactEvidence,
    context: ProductOwnerActionContext,
    affected_product: ChangeImpactAffectedProduct,
    policy: ProductOwnerPolicyRecord,
    requirement: ProductOwnerRequirementRecord,
    preview_isolation: OwnerAcceptancePreviewIsolationBinding,
) -> OwnerAcceptanceReviewContext:
    base = evidence.repository_evidence.base
    if base is None:
        raise OwnerAcceptanceReviewContextUnavailableError(
            "Owner acceptance requires server-resolved reviewed base evidence."
        )
    change_class = _change_class(impact=evidence.impact, affected_product=affected_product)
    return OwnerAcceptanceReviewContext(
        base_ref=base.base_ref,
        base_sha=base.base_sha,
        change_class=change_class,
        engineering_review_tier=evidence.impact.engineering_review_tier,
        review_max_age_seconds=policy.review_age.max_age_seconds(change_class),
        contributions=_contribution_binding(evidence.repository_evidence.authorship),
        policy_fingerprints=_policy_fingerprints(
            context=context,
            policy=policy,
            requirement=requirement,
        ),
        preview_isolation=preview_isolation,
    )


def _inadmissible_reason(
    *,
    binding: OwnerAcceptanceBinding,
    event: OwnerAcceptanceEventRecord,
    policy: ProductOwnerPolicyRecord | None,
    evaluated_at: str,
) -> tuple[OwnerAcceptanceDecisionStatus, OwnerAcceptanceReasonCode] | None:
    """Return why a historical accepted event is not currently admissible, if it is not.

    History is never rewritten here. An expired, weakly isolated, or
    unknown-authorship review stays recorded and simply stops being current.
    """
    review_context = binding.review_context
    if review_context is None:
        return "unavailable", "review_context_missing"
    if review_context.contributions.resolution != "resolved":
        return "unavailable", "contributing_identity_unknown"
    if policy is not None and not policy.preview_trust.satisfied_by(
        review_context.preview_isolation.isolation_class
    ):
        return "unavailable", "preview_isolation_insufficient"
    if policy is not None and event.authorization is not None:
        authorization = event.authorization
        if authorization.self_review and not policy.self_review.allows(review_context.change_class):
            return "unavailable", "self_review_denied"
        if (
            authorization.self_review
            and authorization.self_review_exception_revision
            != policy.self_review.exception_revision
        ):
            return "unavailable", "self_review_denied"
    if _elapsed_seconds(event.occurred_at, evaluated_at) > review_context.review_max_age_seconds:
        return "stale", "owner_review_expired"
    return None


def _elapsed_seconds(start: str, end: str) -> float:
    started = datetime.fromisoformat(start.replace("Z", "+00:00"))
    ended = datetime.fromisoformat(end.replace("Z", "+00:00"))
    return (ended - started).total_seconds()


def _resolve_owner_acceptance_preview_binding(
    *,
    store: object,
    product: str,
    repository: str,
    pull_request_number: int,
    head_sha: str,
) -> OwnerAcceptancePreviewEvidence:
    preview_store = require_owner_acceptance_preview_read_store(store)
    try:
        profile = preview_store.read_product_profile_record(product)
    except (FileNotFoundError, LookupError) as error:
        raise OwnerAcceptanceEvaluationUnavailableError(
            "Owner acceptance product profile is unavailable."
        ) from error
    except ValueError as error:
        raise OwnerAcceptanceEvaluationUnavailableError(
            "Owner acceptance product profile is unavailable or invalid."
        ) from error
    if not profile.preview.enabled:
        return _no_preview_evidence()
    if profile.repository.strip().casefold() != repository.strip().casefold():
        raise OwnerAcceptanceEvaluationUnavailableError(
            "Preview product profile does not match the exact-change repository."
        )
    repository_name = repository.split("/", 1)[-1].casefold()
    previews = tuple(
        preview
        for preview in preview_store.list_preview_records(
            context_name=profile.preview.context,
            anchor_pr_number=pull_request_number,
        )
        if preview.anchor_repo.strip().casefold() in {repository.casefold(), repository_name}
        and preview.state == "active"
    )
    if not previews:
        raise OwnerAcceptanceEvaluationUnavailableError(
            "Owner acceptance requires an active serving preview for this product."
        )
    if len(previews) != 1:
        raise OwnerAcceptanceEvaluationUnavailableError(
            "Owner acceptance requires exactly one unambiguous serving preview."
        )
    preview = previews[0]
    if not preview.serving_generation_id.strip():
        raise OwnerAcceptanceEvaluationUnavailableError(
            "Owner acceptance preview does not have a serving generation."
        )
    try:
        generation = preview_store.read_preview_generation_record(preview.serving_generation_id)
        evidence = verify_serving_preview(
            product=product,
            preview=preview,
            generation=generation,
            require_runtime_generation_id=True,
        )
        if evidence.head_sha.strip().lower() != head_sha.strip().lower():
            raise OwnerAcceptanceEvaluationUnavailableError(
                "Owner acceptance preview does not serve the current pull request head."
            )
        transport_mode = profile.preview.data_transport_mode
        return OwnerAcceptancePreviewEvidence(
            preview=OwnerAcceptancePreviewBinding(
                context=evidence.context,
                preview_id=evidence.preview_id,
                serving_generation_id=evidence.serving_generation_id,
                artifact_id=evidence.artifact_id,
                artifact_image_digest=evidence.artifact_image_digest,
                manifest_fingerprint=evidence.manifest_fingerprint,
                preview_url=evidence.preview_url,
                runtime_identity=owner_acceptance_runtime_identity_binding(
                    evidence.runtime_identity
                ),
            ),
            isolation=OwnerAcceptancePreviewIsolationBinding(
                isolation_class=_PREVIEW_ISOLATION_BY_TRANSPORT_MODE.get(
                    transport_mode,
                    "unknown",
                ),
                data_transport_mode=transport_mode,
                source="product_preview_profile",
            ),
        )
    except (FileNotFoundError, LookupError, ValueError) as error:
        raise OwnerAcceptanceEvaluationUnavailableError(
            "Owner acceptance preview evidence is unavailable or invalid."
        ) from error


def _decision(
    *,
    status: OwnerAcceptanceDecisionStatus,
    reason_code: OwnerAcceptanceReasonCode,
    evaluated_at: str,
    binding: OwnerAcceptanceBinding | None = None,
    event: OwnerAcceptanceEventRecord | None = None,
    products: tuple[OwnerAcceptanceProductDecision, ...] = (),
    admissible: bool = False,
) -> OwnerAcceptanceDecision:
    return OwnerAcceptanceDecision(
        status=status,
        reason_code=reason_code,
        binding=binding,
        current_event=event,
        products=products,
        evaluated_at=evaluated_at,
        admissible=admissible,
        human_action_semantics=owner_acceptance_human_action_semantics(
            event.action if event is not None else None
        ),
    )


def _latest_owner_acceptance_event(
    events: tuple[OwnerAcceptanceEventRecord, ...],
) -> OwnerAcceptanceEventRecord:
    sequences = tuple(event.subject_sequence for event in events)
    if any(sequence < 1 for sequence in sequences):
        raise OwnerAcceptanceEvaluationUnavailableError(
            "Owner acceptance history is missing persisted subject sequence metadata."
        )
    if len(set(sequences)) != len(sequences):
        raise OwnerAcceptanceEvaluationUnavailableError(
            "Owner acceptance history contains duplicate subject sequences."
        )
    return max(events, key=lambda event: event.subject_sequence)


def _evaluation_timestamp(value: str) -> str:
    if value.strip():
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    else:
        parsed = datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        raise ValueError("Owner acceptance timestamp requires timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
