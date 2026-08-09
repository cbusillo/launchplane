from __future__ import annotations

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
    ChangeImpactEvaluation,
    ChangeImpactTargetReference,
)
from control_plane.contracts.owner_acceptance import (
    OWNER_ACCEPTANCE_STATUS_PRECEDENCE,
    OwnerAcceptanceAction,
    OwnerAcceptanceAuthorization,
    OwnerAcceptanceBinding,
    OwnerAcceptanceDecision,
    OwnerAcceptanceDecisionStatus,
    OwnerAcceptanceEventRecord,
    OwnerAcceptanceEventWriteStatus,
    OwnerAcceptancePreviewBinding,
    OwnerAcceptanceProductDecision,
    OwnerAcceptanceReasonCode,
    OwnerAcceptanceSourceEventKind,
    OwnerAcceptanceViewerBindingEligibility,
    OwnerAcceptanceViewerEligibilityReason,
    owner_acceptance_runtime_identity_binding,
)
from control_plane.contracts.preview_generation_record import PreviewGenerationRecord
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.product_owner import (
    ProductOwnerActionContext,
    ProductOwnerActorIdentity,
    ProductOwnerPolicyRecord,
    ProductOwnerRequirementRecord,
)
from control_plane.product_owner_service import (
    evaluate_product_owner_shadow_authority,
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
                    authority = evaluate_product_owner_shadow_authority(
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
                except (TypeError, ValueError):
                    reason_code = "owner_authority_unavailable"
                else:
                    if authority.decision == "authorized":
                        can_submit_event = True
                        reason_code = "current_product_owner"
                    elif authority.reason_code == "actor_not_current_owner":
                        reason_code = "not_current_product_owner"
                    else:
                        reason_code = "owner_authority_unavailable"
            elif actor is not None:
                reason_code = "owner_authority_unavailable"
            eligibility.append(
                OwnerAcceptanceViewerBindingEligibility(
                    binding_sha256=binding.binding_sha256,
                    product=binding.product,
                    system=binding.system,
                    action=binding.action,
                    environment=binding.environment,
                    can_submit_event=can_submit_event,
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
    impact = _evaluate_change_impact_for_target(
        store=store,
        repository_evidence_provider=repository_evidence_provider,
        target=target,
        evaluated_at=normalized_evaluated_at,
    )
    if impact.status == "stale_head":
        return _decision(
            status="stale", reason_code="change_impact_stale", evaluated_at=normalized_evaluated_at
        )
    if impact.status != "success":
        return _decision(
            status="unavailable",
            reason_code="change_impact_unavailable",
            evaluated_at=normalized_evaluated_at,
        )
    if impact.owner_impact != "required":
        return _decision(
            status="not_required",
            reason_code="engineering_only",
            evaluated_at=normalized_evaluated_at,
        )
    if not impact.affected_products:
        return _decision(
            status="unavailable",
            reason_code="change_impact_unavailable",
            evaluated_at=normalized_evaluated_at,
        )
    return _evaluate_owner_acceptance_for_impact(
        impact=impact,
        store=store,
        evaluated_at=normalized_evaluated_at,
    )


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
    occurred_at: str = "",
) -> OwnerAcceptanceWriteResult:
    if action not in {"accepted", "changes_requested", "revoked"}:
        raise OwnerAcceptanceAuthorizationError("Human route cannot write system-only events.")
    if not isinstance(identity, GitHubHumanIdentity):
        raise OwnerAcceptanceAuthorizationError("Owner acceptance requires a GitHub human session.")
    normalized_occurred_at = _evaluation_timestamp(occurred_at)
    impact = _evaluate_change_impact_for_target(
        store=store,
        repository_evidence_provider=repository_evidence_provider,
        target=target,
        evaluated_at=normalized_occurred_at,
    )
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
        impact=impact,
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
    binding = _binding_from_impact(
        impact=impact,
        product_index=selected_product_index,
        store=store,
        actor=actor,
        evaluated_at=normalized_occurred_at,
        expected_binding_sha256=expected_binding_sha256,
    )
    if binding is None:
        raise OwnerAcceptanceEvaluationUnavailableError(
            "Owner acceptance cannot bind unavailable Owner authority."
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
    )
    record = OwnerAcceptanceEventRecord(
        binding=binding,
        action=action,
        occurred_at=normalized_occurred_at,
        source_event_kind=source_event_kind,
        source_event_id=source_event_id,
        reason=reason,
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
    write_status = event_store.write_owner_acceptance_event_record(record)
    if write_status == "replayed":
        record = event_store.read_owner_acceptance_event_record(record.event_id)
    selected_decision = evaluate_owner_acceptance_for_binding(
        binding=binding,
        events=(*prior_events, record),
        evaluated_at=normalized_occurred_at,
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
) -> OwnerAcceptanceDecision:
    normalized_evaluated_at = _evaluation_timestamp(evaluated_at)
    matching = tuple(
        event
        for event in events
        if event.binding.binding_sha256 == binding.binding_sha256
        and event.occurred_at <= normalized_evaluated_at
    )
    if not matching:
        stale_events = tuple(
            event for event in events if event.occurred_at <= normalized_evaluated_at
        )
        if stale_events:
            latest_stale = sorted(
                stale_events,
                key=lambda event: (event.occurred_at, event.event_id),
            )[-1]
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
    latest = sorted(matching, key=lambda event: (event.occurred_at, event.event_id))[-1]
    if latest.action == "accepted":
        return _decision(
            status="accepted",
            reason_code="acceptance_valid",
            binding=binding,
            event=latest,
            evaluated_at=normalized_evaluated_at,
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
    )


def _evaluate_owner_acceptance_for_impact(
    *,
    impact: ChangeImpactEvaluation,
    store: object,
    evaluated_at: str,
    events: tuple[OwnerAcceptanceEventRecord, ...] | None = None,
) -> OwnerAcceptanceDecision:
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
            impact=impact,
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
    impact: ChangeImpactEvaluation,
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
        binding = _binding_from_impact(
            impact=impact,
            product_index=product_index,
            store=store,
            actor=None,
            evaluated_at=evaluated_at,
        )
    except OwnerAcceptanceEvaluationUnavailableError:
        preview_events = tuple(
            event
            for event in product_events
            if event.binding.preview is not None and event.occurred_at <= evaluated_at
        )
        if preview_events:
            current_event = max(
                preview_events,
                key=lambda event: (event.occurred_at, event.event_id),
            )
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
    if binding is None:
        return _product_decision(
            affected_product=affected_product,
            decision=_decision(
                status="unavailable",
                reason_code="owner_authority_unavailable",
                evaluated_at=evaluated_at,
            ),
        )
    effective_preview_events = tuple(
        event
        for event in product_events
        if event.binding.preview is not None and event.occurred_at <= evaluated_at
    )
    if binding.preview is None and effective_preview_events:
        decision = _decision(
            status="stale",
            reason_code="preview_evidence_stale",
            binding=binding,
            event=max(
                effective_preview_events,
                key=lambda event: (event.occurred_at, event.event_id),
            ),
            evaluated_at=evaluated_at,
        )
    else:
        decision = evaluate_owner_acceptance_for_binding(
            binding=binding,
            events=product_events,
            evaluated_at=evaluated_at,
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
) -> ChangeImpactEvaluation:
    try:
        repository_evidence = repository_evidence_provider.resolve(target)
    except Exception as error:
        raise OwnerAcceptanceEvaluationUnavailableError(str(error)) from error
    policy_store = require_change_impact_policy_read_store(store)
    stored_evidence = load_change_impact_stored_evidence(
        store=store,
        target=repository_evidence.target,
    )
    return evaluate_change_impact(
        repository_evidence=repository_evidence,
        policies=policy_store.list_change_impact_policy_records(
            repository_id=repository_evidence.target.repository_id,
            status="active",
        ),
        stored_evidence=stored_evidence,
        evaluated_at=evaluated_at,
    )


def _binding_from_impact(
    *,
    impact: ChangeImpactEvaluation,
    product_index: int,
    store: object,
    actor: ProductOwnerActorIdentity | None,
    evaluated_at: str,
    expected_binding_sha256: str = "",
) -> OwnerAcceptanceBinding | None:
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
    for owner_actor in owner_actors:
        candidate = evaluate_product_owner_shadow_authority(
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
    if authority is None:
        if expected_binding_sha256:
            raise OwnerAcceptanceBindingConflictError(
                "Owner acceptance binding changed; evaluate the exact change again before recording."
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
    preview = _resolve_owner_acceptance_preview_binding(
        store=store,
        product=context.product,
        repository=impact.target.repository,
        pull_request_number=impact.target.pull_request_number,
        head_sha=impact.target.head_sha,
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
    )
    if (
        expected_binding_sha256
        and expected_binding_sha256.strip().lower() != binding.binding_sha256
    ):
        raise OwnerAcceptanceBindingConflictError(
            "Owner acceptance binding changed; evaluate the exact change again before recording."
        )
    if actor is not None:
        actor_authority = evaluate_product_owner_shadow_authority(
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
    return binding


def _resolve_owner_acceptance_preview_binding(
    *,
    store: object,
    product: str,
    repository: str,
    pull_request_number: int,
    head_sha: str,
) -> OwnerAcceptancePreviewBinding | None:
    preview_store = require_owner_acceptance_preview_read_store(store)
    try:
        profile = preview_store.read_product_profile_record(product)
    except (FileNotFoundError, LookupError):
        return None
    except ValueError as error:
        raise OwnerAcceptanceEvaluationUnavailableError(
            "Owner acceptance product profile is unavailable or invalid."
        ) from error
    if not profile.preview.enabled:
        return None
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
        return OwnerAcceptancePreviewBinding(
            context=evidence.context,
            preview_id=evidence.preview_id,
            serving_generation_id=evidence.serving_generation_id,
            artifact_id=evidence.artifact_id,
            artifact_image_digest=evidence.artifact_image_digest,
            manifest_fingerprint=evidence.manifest_fingerprint,
            preview_url=evidence.preview_url,
            runtime_identity=owner_acceptance_runtime_identity_binding(evidence.runtime_identity),
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
) -> OwnerAcceptanceDecision:
    return OwnerAcceptanceDecision(
        status=status,
        reason_code=reason_code,
        binding=binding,
        current_event=event,
        products=products,
        evaluated_at=evaluated_at,
    )


def _evaluation_timestamp(value: str) -> str:
    if value.strip():
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    else:
        parsed = datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        raise ValueError("Owner acceptance timestamp requires timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
