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
    OwnerAcceptancePreviewBinding,
    OwnerAcceptanceReasonCode,
    OwnerAcceptanceSourceEventKind,
    owner_acceptance_runtime_identity_binding,
)
from control_plane.contracts.preview_generation_record import PreviewGenerationRecord
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.product_owner import (
    ProductOwnerActionContext,
    ProductOwnerActorIdentity,
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
    try:
        binding = _binding_from_impact(
            impact=impact,
            product_index=0,
            store=store,
            actor=None,
            evaluated_at=normalized_evaluated_at,
        )
    except OwnerAcceptanceEvaluationUnavailableError:
        affected_product = impact.affected_products[0]
        preview_events = tuple(
            event
            for event in require_owner_acceptance_event_store(
                store
            ).list_owner_acceptance_event_records(
                repository_id=impact.target.repository_id,
                pull_request_number=impact.target.pull_request_number,
                product=affected_product.product,
                system=affected_product.system,
                action=affected_product.owner_action,
            )
            if event.binding.preview is not None and event.occurred_at <= normalized_evaluated_at
        )
        if preview_events:
            current_event = max(
                preview_events,
                key=lambda event: (event.occurred_at, event.event_id),
            )
            return _decision(
                status="stale",
                reason_code="preview_evidence_stale",
                binding=current_event.binding,
                event=current_event,
                evaluated_at=normalized_evaluated_at,
            )
        return _decision(
            status="unavailable",
            reason_code="preview_evidence_unavailable",
            evaluated_at=normalized_evaluated_at,
        )
    if binding is None:
        return _decision(
            status="unavailable",
            reason_code="owner_authority_unavailable",
            evaluated_at=normalized_evaluated_at,
        )
    events = require_owner_acceptance_event_store(store).list_owner_acceptance_event_records(
        repository_id=binding.repository_id,
        pull_request_number=binding.pull_request_number,
        product=binding.product,
        system=binding.system,
        action=binding.action,
    )
    effective_preview_events = tuple(
        event
        for event in events
        if event.binding.preview is not None and event.occurred_at <= normalized_evaluated_at
    )
    if binding.preview is None and effective_preview_events:
        return _decision(
            status="stale",
            reason_code="preview_evidence_stale",
            binding=binding,
            event=max(
                effective_preview_events,
                key=lambda event: (event.occurred_at, event.event_id),
            ),
            evaluated_at=normalized_evaluated_at,
        )
    return evaluate_owner_acceptance_for_binding(
        binding=binding,
        events=events,
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
    if len(impact.affected_products) != 1:
        raise OwnerAcceptanceBindingConflictError(
            "Owner acceptance binding changed; evaluate the exact change again before recording."
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
    event_store = require_owner_acceptance_event_store(store)
    prior_events = event_store.list_owner_acceptance_event_records(
        repository_id=binding.repository_id,
        pull_request_number=binding.pull_request_number,
        product=binding.product,
        system=binding.system,
        action=binding.action,
    )
    if binding.preview is None and any(event.binding.preview is not None for event in prior_events):
        raise OwnerAcceptanceEvaluationUnavailableError(
            "Preview-bound Owner acceptance cannot downgrade to an exact-change-only binding."
        )
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
