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
    ChangeImpactEvaluation,
    ChangeImpactTargetReference,
)
from control_plane.contracts.owner_acceptance import (
    OwnerAcceptanceAction,
    OwnerAcceptanceAuthorization,
    OwnerAcceptanceBinding,
    OwnerAcceptanceDecision,
    OwnerAcceptanceDecisionStatus,
    OwnerAcceptanceEventRecord,
    OwnerAcceptanceEventWriteStatus,
    OwnerAcceptanceReasonCode,
    OwnerAcceptanceSourceEventKind,
)
from control_plane.contracts.product_owner import (
    ProductOwnerActionContext,
    ProductOwnerActorIdentity,
)
from control_plane.product_owner_service import (
    evaluate_product_owner_shadow_authority,
    require_product_owner_policy_read_store,
    require_product_owner_requirement_read_store,
    require_product_owner_routing_read_store,
)
from control_plane.service_auth import GitHubHumanIdentity, LaunchplaneIdentity


class OwnerAcceptanceEventConflictError(RuntimeError):
    """Raised when an immutable Owner acceptance event replay changes payload."""


class OwnerAcceptanceAuthorizationError(PermissionError):
    """Raised when a caller cannot author a human Owner acceptance event."""


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


class OwnerAcceptanceWriteResult(NamedTuple):
    status: OwnerAcceptanceEventWriteStatus
    record: OwnerAcceptanceEventRecord
    decision: OwnerAcceptanceDecision


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
    if len(impact.affected_products) != 1:
        return _decision(
            status="unavailable",
            reason_code="multi_product_unsupported",
            evaluated_at=normalized_evaluated_at,
        )
    binding = _binding_from_impact(
        impact=impact,
        product_index=0,
        store=store,
        actor=None,
        evaluated_at=normalized_evaluated_at,
    )
    if binding is None:
        return _decision(
            status="unavailable",
            reason_code="owner_authority_unavailable",
            evaluated_at=normalized_evaluated_at,
        )
    return evaluate_owner_acceptance_for_binding(
        binding=binding,
        events=require_owner_acceptance_event_store(store).list_owner_acceptance_event_records(
            repository_id=binding.repository_id,
            pull_request_number=binding.pull_request_number,
            product=binding.product,
            system=binding.system,
            action=binding.action,
        ),
        evaluated_at=normalized_evaluated_at,
    )


def record_owner_acceptance_event(
    *,
    store: object,
    repository_evidence_provider: ChangeImpactRepositoryEvidenceProvider,
    target: ChangeImpactTargetReference,
    identity: LaunchplaneIdentity,
    action: OwnerAcceptanceAction,
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
        raise OwnerAcceptanceAuthorizationError(
            "Owner acceptance is not required for this exact change."
        )
    if not impact.affected_products:
        raise OwnerAcceptanceEvaluationUnavailableError(
            "Owner acceptance requires an affected product for Owner-impacting changes."
        )
    if len(impact.affected_products) != 1:
        raise OwnerAcceptanceEvaluationUnavailableError(
            "Owner acceptance does not yet support multi-product changes."
        )
    actor = ProductOwnerActorIdentity(
        provider="github",
        provider_subject_id=str(identity.github_id),
    )
    binding = _binding_from_impact(
        impact=impact,
        product_index=0,
        store=store,
        actor=actor,
        evaluated_at=normalized_occurred_at,
    )
    if binding is None:
        raise OwnerAcceptanceAuthorizationError(
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
    event_store = require_owner_acceptance_event_store(store)
    write_status = event_store.write_owner_acceptance_event_record(record)
    if write_status == "replayed":
        record = event_store.read_owner_acceptance_event_record(record.event_id)
    decision = evaluate_owner_acceptance_for_binding(
        binding=binding,
        events=event_store.list_owner_acceptance_event_records(
            repository_id=binding.repository_id,
            pull_request_number=binding.pull_request_number,
            product=binding.product,
            system=binding.system,
            action=binding.action,
        ),
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


def require_owner_acceptance_event_store(store: object) -> OwnerAcceptanceEventStore:
    if not callable(getattr(store, "write_owner_acceptance_event_record", None)):
        raise TypeError("Owner acceptance event storage is unavailable.")
    if not callable(getattr(store, "list_owner_acceptance_event_records", None)):
        raise TypeError("Owner acceptance event storage is unavailable.")
    if not callable(getattr(store, "read_owner_acceptance_event_record", None)):
        raise TypeError("Owner acceptance event storage is unavailable.")
    return cast(OwnerAcceptanceEventStore, store)


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
    routing_store = require_product_owner_routing_read_store(store)
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
        return None
    if not active_policies[0].owners:
        return None
    owner_actors = (
        (actor,)
        if actor is not None
        else tuple(
            ProductOwnerActorIdentity(
                provider=owner.identity.provider,
                provider_subject_id=owner.identity.provider_subject_id,
            )
            for owner in active_policies[0].owners
        )
    )
    authority = None
    for owner_actor in owner_actors:
        candidate = evaluate_product_owner_shadow_authority(
            context=context,
            actor=owner_actor,
            policies=policies,
            requirements=requirements,
            routings=routing_store.list_product_owner_routing_records(
                product=context.product,
                system=context.system,
            ),
            claimed_policy_revision=active_policies[0].policy_revision,
            claimed_policy_digest=active_policies[0].policy_digest,
            claimed_requirement_revision=active_requirements[0].requirement_revision,
            claimed_requirement_digest=active_requirements[0].requirement_digest,
            evaluated_at=evaluated_at,
        )
        if candidate.decision == "authorized":
            authority = candidate
            break
        if actor is not None:
            authority = candidate
    if authority is None:
        return None
    if authority.decision not in {"authorized", "denied"}:
        return None
    if actor is not None and authority.decision != "authorized":
        raise OwnerAcceptanceAuthorizationError("Caller is not a current product Owner.")
    if (
        not authority.policy_record_id
        or authority.policy_revision is None
        or not authority.policy_digest
        or not authority.requirement_record_id
        or authority.requirement_revision is None
        or not authority.requirement_digest
    ):
        return None
    return OwnerAcceptanceBinding(
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
    )


def _decision(
    *,
    status: OwnerAcceptanceDecisionStatus,
    reason_code: OwnerAcceptanceReasonCode,
    evaluated_at: str,
    binding: OwnerAcceptanceBinding | None = None,
    event: OwnerAcceptanceEventRecord | None = None,
) -> OwnerAcceptanceDecision:
    return OwnerAcceptanceDecision(
        status=status,
        reason_code=reason_code,
        binding=binding,
        current_event=event,
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
