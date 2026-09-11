"""Planning helpers for inert ordinary-agent delivery activation administration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import inspect
from typing import Any, cast

from pydantic import BaseModel

from control_plane.authz_grant_service import plan_managed_authz_policy_reconcile
from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
)
from control_plane.contracts.ordinary_agent_activation import (
    OrdinaryAgentDeliveryActivationDurationOption,
    OrdinaryAgentDeliveryActivationRecord,
    OrdinaryAgentDeliveryActivationEvent,
    OrdinaryAgentDeliveryActivationReference,
    OrdinaryAgentDeliveryActivationRequest,
    OrdinaryAgentDeliveryActivationRevokeHumanEvidence,
    OrdinaryAgentDeliveryActivationRevokeOption,
    OrdinaryAgentDeliveryActivationRevokeRequest,
    OrdinaryAgentDeliveryActivationScope,
    OrdinaryAgentDeliveryActivationSetupHumanEvidence,
    OrdinaryAgentDeliveryActivationSetupOption,
    OrdinaryAgentDeliveryActivationSetupRequest,
    OrdinaryAgentDeliveryInventoryReference,
    OrdinaryAgentDeliveryPolicyPackageReference,
    OrdinaryAgentDeliveryRuntimeCapabilityEvidence,
)
from control_plane.contracts.authz_policy_write_transition import (
    AuthzPolicyImmutableHumanCallerBinding,
    AuthzPolicySchemaV3OrdinaryEnableEvidence,
    AuthzPolicySchemaV3TransitionDeniedError,
    classify_authz_policy_schema_v3_transition,
    require_authz_policy_source_status,
)
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.ordinary_agent_custody import OrdinaryAgentCustodyIssueAttempt
from control_plane.contracts.ordinary_agent_effect import (
    OrdinaryAgentQualificationAttemptRecord,
    OrdinaryAgentSnapshotAttemptRecord,
    parse_ordinary_agent_read_attempt,
)
from control_plane.contracts.ordinary_agent_qualification import (
    OrdinaryAgentQualificationAttestation,
)
from control_plane.contracts.ordinary_agent_session_lifecycle import (
    OrdinaryAgentFiniteRequestRecord,
    OrdinaryAgentGuardedDeliveryFiniteRequestV2,
    OrdinaryAgentQualificationFiniteRequestV2,
    parse_ordinary_agent_finite_request,
)
from control_plane.contracts.privileged_operation import (
    ManagedAuthzPolicySetHumanEvidence,
    ManagedAuthzPolicySetProposalInput,
    PrivilegedOperationRecord,
    privileged_operation_evidence_digest_candidates,
    privileged_operation_request_digest_candidates,
)
from control_plane.contracts.repository_inventory import RepositoryInventoryRecord
from control_plane.durable_operation_authorization import (
    SUPPORTED_MANAGED_RULE_POLICY_SCHEMA_VERSIONS,
)
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.schema_invariants import RUNTIME_COMPATIBLE_ALEMBIC_REVISIONS


_ACTIVATION_DURATION_OPTIONS = (
    (60 * 60, "1 hour"),
    (24 * 60 * 60, "1 day"),
    (7 * 24 * 60 * 60, "7 days"),
    (30 * 24 * 60 * 60, "30 days"),
)
_MAX_ACTIVATION_DURATION = timedelta(days=30)


class OrdinaryAgentDeliveryActivationPlanningError(ValueError):
    """Raised when referenced server-owned setup data does not resolve exactly."""


def _same_activation_scope_identity(
    left: OrdinaryAgentDeliveryActivationScope,
    right: OrdinaryAgentDeliveryActivationScope,
) -> bool:
    return (
        left.target.repository_id == right.target.repository_id
        and left.target.base_branch == right.target.base_branch
        and left.managed_set_id == right.managed_set_id
        and left.managed_rule_id == right.managed_rule_id
    )


def _activation_option_timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(value).astimezone(timezone.utc)
    fraction = f".{parsed.microsecond:06d}" if parsed.microsecond else ""
    return f"{parsed.strftime('%b %d, %Y at %H:%M:%S')}{fraction} UTC"


@dataclass(frozen=True, slots=True)
class ResolvedOrdinaryAgentDeliveryActivationSetupSource:
    scope: OrdinaryAgentDeliveryActivationScope
    policy_package: OrdinaryAgentDeliveryPolicyPackageReference
    inventory: OrdinaryAgentDeliveryInventoryReference
    observed_at: str


class _HistoricalPolicyStore:
    def __init__(self, record: LaunchplaneAuthzPolicyRecord) -> None:
        self.record = record.model_copy(update={"status": "active"})

    def list_authz_policy_records(
        self,
        *,
        status: str = "",
        limit: int | None = None,
    ) -> tuple[LaunchplaneAuthzPolicyRecord, ...]:
        records = (self.record,) if status in {"", "active"} else ()
        return records[:limit]


def _read_policy_operation(record_store: object, operation_id: str) -> PrivilegedOperationRecord:
    reader = getattr(record_store, "read_privileged_operation_record", None)
    if not callable(reader):
        raise OrdinaryAgentDeliveryActivationPlanningError(
            "Activation planning requires privileged-operation storage."
        )
    try:
        record = reader(operation_id)
    except FileNotFoundError as error:
        raise OrdinaryAgentDeliveryActivationPlanningError(
            "Referenced policy operation was not found."
        ) from error
    if not isinstance(record, PrivilegedOperationRecord):
        record = PrivilegedOperationRecord.model_validate(record)
    return record


def _require_admissible_policy_operation(
    record: PrivilegedOperationRecord,
    *,
    observed_at: datetime,
) -> tuple[ManagedAuthzPolicySetProposalInput, ManagedAuthzPolicySetHumanEvidence]:
    if record.descriptor_id != "managed-authz-policy-set":
        raise OrdinaryAgentDeliveryActivationPlanningError(
            "Activation setup requires a managed authz policy operation."
        )
    try:
        require_authz_policy_source_status(
            status=record.status,
            expires_at=record.expires_at,
            observed_at=observed_at,
        )
    except AuthzPolicySchemaV3TransitionDeniedError as error:
        raise OrdinaryAgentDeliveryActivationPlanningError(
            "Referenced policy operation status is not admissible for activation setup."
        ) from error
    if not isinstance(record.request, ManagedAuthzPolicySetProposalInput) or not isinstance(
        record.evidence,
        ManagedAuthzPolicySetHumanEvidence,
    ):
        raise OrdinaryAgentDeliveryActivationPlanningError(
            "Referenced policy operation payload variants do not match."
        )
    if record.request_digest not in privileged_operation_request_digest_candidates(record.request):
        raise OrdinaryAgentDeliveryActivationPlanningError(
            "Referenced policy operation request digest is invalid."
        )
    if record.evidence_digest not in privileged_operation_evidence_digest_candidates(
        record.evidence
    ):
        raise OrdinaryAgentDeliveryActivationPlanningError(
            "Referenced policy operation evidence digest is invalid."
        )
    return record.request, record.evidence


def _historical_policy_record(
    record_store: object,
    *,
    record_id: str,
) -> LaunchplaneAuthzPolicyRecord:
    reader = getattr(record_store, "list_authz_policy_records", None)
    if not callable(reader):
        raise OrdinaryAgentDeliveryActivationPlanningError(
            "Activation planning requires authz policy history."
        )
    records = tuple(reader(limit=None))
    matches = tuple(record for record in records if getattr(record, "record_id", "") == record_id)
    if len(matches) != 1:
        raise OrdinaryAgentDeliveryActivationPlanningError(
            "Referenced policy operation does not bind one historical policy record."
        )
    record = matches[0]
    if not isinstance(record, LaunchplaneAuthzPolicyRecord):
        record = LaunchplaneAuthzPolicyRecord.model_validate(record)
    return record


def _recompute_policy_package(
    record_store: object,
    *,
    operation: PrivilegedOperationRecord,
    request: ManagedAuthzPolicySetProposalInput,
    evidence: ManagedAuthzPolicySetHumanEvidence,
) -> OrdinaryAgentDeliveryPolicyPackageReference:
    if request.desired_policy.schema_version != 3:
        raise OrdinaryAgentDeliveryActivationPlanningError(
            "Activation setup requires a schema-v3 policy proposal."
        )
    policy_record = _historical_policy_record(
        record_store,
        record_id=evidence.diff.previous_record_id,
    )
    migration_pair = (policy_record.policy.schema_version, request.schema_migration)
    if migration_pair not in {
        (2, "migrate_v2_to_v3"),
        (3, "reject"),
    }:
        raise OrdinaryAgentDeliveryActivationPlanningError(
            "Activation policy migration mode does not match its historical policy schema."
        )
    try:
        _, _, _, recomputed_diff = plan_managed_authz_policy_reconcile(
            record_store=cast(Any, _HistoricalPolicyStore(policy_record)),
            request=request.reconcile_request(),
        )
    except (LookupError, TypeError, ValueError) as error:
        raise OrdinaryAgentDeliveryActivationPlanningError(
            "Referenced policy operation cannot reproduce its reviewed policy package."
        ) from error
    expected_digests = (
        evidence.diff.plan_sha256,
        evidence.diff.desired_set_sha256,
        evidence.diff.desired_policy_sha256,
    )
    actual_digests = (
        recomputed_diff.plan_sha256,
        recomputed_diff.desired_set_sha256,
        recomputed_diff.desired_policy_sha256,
    )
    if actual_digests != expected_digests or evidence.plan_digest != recomputed_diff.plan_sha256:
        raise OrdinaryAgentDeliveryActivationPlanningError(
            "Referenced policy operation reviewed digests no longer reproduce."
        )
    return OrdinaryAgentDeliveryPolicyPackageReference(
        policy_operation_id=operation.operation_id,
        request_sha256=operation.request_digest,
        evidence_sha256=operation.evidence_digest,
        plan_sha256=recomputed_diff.plan_sha256,
        desired_set_sha256=recomputed_diff.desired_set_sha256,
        candidate_policy_sha256=recomputed_diff.desired_policy_sha256,
    )


def _activation_scope(
    request: ManagedAuthzPolicySetProposalInput,
) -> OrdinaryAgentDeliveryActivationScope:
    ordinary_rules = tuple(
        rule
        for rule in request.desired_policy.ordinary_agents
        if rule.managed_set_id == request.managed_set_id
    )
    if len(ordinary_rules) != 1 or len(request.desired_policy.ordinary_agents) != 1:
        raise OrdinaryAgentDeliveryActivationPlanningError(
            "Activation policy package must contain exactly one ordinary-agent managed rule."
        )
    rule = ordinary_rules[0]
    return OrdinaryAgentDeliveryActivationScope(
        target=rule.target,
        managed_set_id=rule.managed_set_id,
        managed_rule_id=rule.managed_rule_id,
    )


def _resolve_inventory(
    record_store: object,
    *,
    record_id: str,
    scope: OrdinaryAgentDeliveryActivationScope,
) -> tuple[OrdinaryAgentDeliveryInventoryReference, str]:
    reader = getattr(record_store, "list_repository_inventory_records", None)
    if not callable(reader):
        raise OrdinaryAgentDeliveryActivationPlanningError(
            "Activation planning requires repository inventory storage."
        )
    records = tuple(reader(repository_id=str(scope.target.repository_id), limit=None))
    if not records:
        raise OrdinaryAgentDeliveryActivationPlanningError(
            "Activation scope has no repository inventory history."
        )
    typed_records = tuple(
        record
        if isinstance(record, RepositoryInventoryRecord)
        else RepositoryInventoryRecord.model_validate(record)
        for record in records
    )
    highest_revision = max(record.inventory_revision for record in typed_records)
    current = tuple(
        record for record in typed_records if record.inventory_revision == highest_revision
    )
    if len(current) != 1 or current[0].record_id != record_id:
        raise OrdinaryAgentDeliveryActivationPlanningError(
            "Activation setup must bind the unique current repository inventory record."
        )
    inventory = current[0]
    if (
        inventory.inventory_state != "tracked"
        or int(inventory.repository_id) != scope.target.repository_id
        or inventory.repository.casefold() != scope.target.repository.casefold()
    ):
        raise OrdinaryAgentDeliveryActivationPlanningError(
            "Activation setup repository inventory does not match the policy target."
        )
    return (
        OrdinaryAgentDeliveryInventoryReference(
            record_id=inventory.record_id,
            revision=inventory.inventory_revision,
            inventory_sha256=inventory.inventory_digest,
        ),
        inventory.recorded_at,
    )


def resolve_ordinary_agent_delivery_activation_setup_source(
    record_store: object,
    *,
    policy_operation_id: str,
    repository_inventory_record_id: str,
    observed_at: datetime | None = None,
) -> ResolvedOrdinaryAgentDeliveryActivationSetupSource:
    operation = _read_policy_operation(record_store, policy_operation_id)
    request, evidence = _require_admissible_policy_operation(
        operation,
        observed_at=(observed_at or datetime.now(timezone.utc)).astimezone(timezone.utc),
    )
    scope = _activation_scope(request)
    inventory, inventory_recorded_at = _resolve_inventory(
        record_store,
        record_id=repository_inventory_record_id,
        scope=scope,
    )
    return ResolvedOrdinaryAgentDeliveryActivationSetupSource(
        scope=scope,
        policy_package=_recompute_policy_package(
            record_store,
            operation=operation,
            request=request,
            evidence=evidence,
        ),
        inventory=inventory,
        observed_at=inventory_recorded_at,
    )


def resolve_authz_policy_schema_v3_enable_evidence(
    record_store: object,
    *,
    current_record: LaunchplaneAuthzPolicyRecord,
    candidate_policy: LaunchplaneAuthzPolicy,
    github_id: int,
    observed_at: datetime,
) -> AuthzPolicySchemaV3OrdinaryEnableEvidence:
    transition = classify_authz_policy_schema_v3_transition(current_record.policy, candidate_policy)
    if transition.kind not in {"v2_to_v3_enable", "v3_enable_or_expand"}:
        raise OrdinaryAgentDeliveryActivationPlanningError(
            "Authz policy transition does not require ordinary activation evidence."
        )
    managed_set_id, managed_rule_id = transition.added_or_changed_ordinary_rule_keys[0].split(
        "\x1f", 1
    )
    matching = tuple(
        record
        for record in _activation_records(record_store)
        if record.scope.managed_set_id == managed_set_id
        and record.scope.managed_rule_id == managed_rule_id
        and record.desired_state == "guarded"
        and record.effective_state in {"qualification_only", "guarded"}
        and not record.revoked_at
        and not record.superseded_at
        and observed_at < datetime.fromisoformat(record.activation_expires_at)
    )
    if len(matching) != 1:
        raise OrdinaryAgentDeliveryActivationPlanningError(
            "Authz policy transition requires one current matching activation."
        )
    activation = matching[0]
    current_capability = _runtime_capability(
        record_store,
        observed_at=observed_at.isoformat(),
    )
    if not _runtime_supports_authz_policy_schema_v3_enable(current_capability):
        raise OrdinaryAgentDeliveryActivationPlanningError(
            "Authz policy transition requires current compatible runtime support."
        )
    source = resolve_ordinary_agent_delivery_activation_setup_source(
        record_store,
        policy_operation_id=activation.policy_package.policy_operation_id,
        repository_inventory_record_id=activation.inventory.record_id,
        observed_at=observed_at,
    )
    if source.scope != activation.scope or source.policy_package != activation.policy_package:
        raise OrdinaryAgentDeliveryActivationPlanningError(
            "Activation policy package no longer resolves exactly."
        )
    if activation.policy_package.candidate_policy_sha256 != authz_policy_sha256(candidate_policy):
        raise OrdinaryAgentDeliveryActivationPlanningError(
            "Activation does not bind the complete candidate policy."
        )
    setup_operation = _read_policy_operation(record_store, activation.source_setup_operation_id)
    from control_plane.contracts.ordinary_agent_activation import (
        OrdinaryAgentDeliveryActivationExecutionEvidence,
        OrdinaryAgentDeliveryActivationSetupHumanEvidence,
        OrdinaryAgentDeliveryActivationSetupRequest,
    )

    if (
        setup_operation.descriptor_id != "ordinary-agent-delivery-activation"
        or setup_operation.status != "executed"
        or not isinstance(setup_operation.request, OrdinaryAgentDeliveryActivationSetupRequest)
        or not isinstance(
            setup_operation.evidence, OrdinaryAgentDeliveryActivationSetupHumanEvidence
        )
        or setup_operation.approval is None
        or not isinstance(
            setup_operation.execution, OrdinaryAgentDeliveryActivationExecutionEvidence
        )
        or setup_operation.execution.action != "setup"
        or setup_operation.execution.result_status != "ok"
    ):
        raise OrdinaryAgentDeliveryActivationPlanningError(
            "Activation setup operation is not one successful terminal setup."
        )
    if (
        canonical_json_sha256(setup_operation.approval.model_dump(mode="json"))
        != activation.source_setup_approval_sha256
    ):
        raise OrdinaryAgentDeliveryActivationPlanningError(
            "Activation setup approval digest changed."
        )
    recovery = getattr(
        record_store, "recover_ordinary_agent_delivery_activation_by_source_operation", None
    )
    if not callable(recovery):
        raise OrdinaryAgentDeliveryActivationPlanningError(
            "Activation setup recovery is unavailable."
        )
    recovered = recovery(activation.source_setup_operation_id)
    installed_record, installed_event = recovered
    execution = setup_operation.execution
    if (
        installed_event.action != "installed"
        or installed_event.sequence != 1
        or execution.activation_id != installed_record.activation_id
        or execution.activation_revision != installed_record.revision
        or execution.activation_sha256 != installed_record.activation_sha256
        or execution.source_operation_id != activation.source_setup_operation_id
    ):
        raise OrdinaryAgentDeliveryActivationPlanningError(
            "Activation setup execution does not match its installed outcome."
        )
    package = activation.policy_package
    return AuthzPolicySchemaV3OrdinaryEnableEvidence(
        caller=AuthzPolicyImmutableHumanCallerBinding(github_id=github_id),
        expected_record_id=current_record.record_id,
        expected_revision=current_record.revision,
        expected_policy_sha256=current_record.policy_sha256,
        candidate_policy_sha256=package.candidate_policy_sha256,
        activation_id=activation.activation_id,
        activation_revision=activation.revision,
        activation_sha256=activation.activation_sha256,
        source_setup_operation_id=activation.source_setup_operation_id,
        policy_operation_id=package.policy_operation_id,
        policy_request_sha256=package.request_sha256,
        policy_evidence_sha256=package.evidence_sha256,
        policy_plan_sha256=package.plan_sha256,
        desired_set_sha256=package.desired_set_sha256,
        repository_id=activation.scope.target.repository_id,
        repository=activation.scope.target.repository,
        base_branch=activation.scope.target.base_branch,
        managed_set_id=managed_set_id,
        managed_rule_id=managed_rule_id,
    )


def ordinary_agent_delivery_activation_plan_sha256(payload: dict[str, object]) -> str:
    return canonical_json_sha256(
        {
            "domain": "ordinary-agent-delivery-activation-plan-v1",
            **payload,
        }
    )


def _model_schema_version(model_type: type[BaseModel]) -> int:
    version = model_type.model_fields["schema_version"].default
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise OrdinaryAgentDeliveryActivationPlanningError(
            f"{model_type.__name__} has no concrete schema version."
        )
    return version


def _runtime_supports_authz_policy_schema_v3_enable(
    capability: OrdinaryAgentDeliveryRuntimeCapabilityEvidence,
) -> bool:
    """Require the process and schema seams consumed by one enabling write.

    ``policy_v3_write_supported`` reports this feature and therefore cannot be
    used to authorize itself. Qualification advancement, guarded execution,
    and provider/image evidence belong to later runtime phases.
    """
    return (
        capability.database_revision_compatible
        and capability.activation_schema_invariants_valid
        and capability.finite_request_versions
        == tuple(
            sorted(
                {
                    _model_schema_version(OrdinaryAgentFiniteRequestRecord),
                    _model_schema_version(OrdinaryAgentQualificationFiniteRequestV2),
                    _model_schema_version(OrdinaryAgentGuardedDeliveryFiniteRequestV2),
                }
            )
        )
        and capability.read_attempt_versions
        == tuple(
            sorted(
                {
                    _model_schema_version(OrdinaryAgentSnapshotAttemptRecord),
                    _model_schema_version(OrdinaryAgentQualificationAttemptRecord),
                }
            )
        )
        and capability.custody_issue_attempt_versions
        == (_model_schema_version(OrdinaryAgentCustodyIssueAttempt),)
        and capability.qualification_attestation_versions
        == (_model_schema_version(OrdinaryAgentQualificationAttestation),)
        and capability.activation_record_versions
        == (_model_schema_version(OrdinaryAgentDeliveryActivationRecord),)
        and capability.activation_event_versions
        == (_model_schema_version(OrdinaryAgentDeliveryActivationEvent),)
        and capability.recovery_versions == (1,)
        and capability.authz_policy_read_versions
        == tuple(sorted(SUPPORTED_MANAGED_RULE_POLICY_SCHEMA_VERSIONS))
        and capability.variant_parsers_registered
        and capability.activation_storage_registered
        and capability.activation_cas_registered
        and capability.activation_recovery_registered
        and capability.bounded_cleanup_registered
        and capability.rollback_reader_registered
    )


def _runtime_capability(
    record_store: object,
    *,
    observed_at: str,
) -> OrdinaryAgentDeliveryRuntimeCapabilityEvidence:
    schema_reader = getattr(
        record_store,
        "ordinary_agent_delivery_activation_schema_capability",
        None,
    )
    observed_revision = "unavailable"
    invariants_sha256 = canonical_json_sha256(
        {"domain": "ordinary-agent-delivery-activation-schema-unavailable-v1"}
    )
    invariants_valid = False
    if callable(schema_reader):
        try:
            raw_capability = schema_reader()
            if (
                isinstance(raw_capability, tuple)
                and len(raw_capability) == 3
                and isinstance(raw_capability[0], str)
                and isinstance(raw_capability[1], str)
                and isinstance(raw_capability[2], bool)
            ):
                observed_revision, invariants_sha256, invariants_valid = raw_capability
        except (RuntimeError, TypeError, ValueError):
            pass

    activation_storage_registered = all(
        callable(getattr(record_store, name, None))
        for name in (
            "read_ordinary_agent_delivery_activation_record",
            "list_ordinary_agent_delivery_activation_records",
            "list_ordinary_agent_delivery_activation_event_records",
        )
    )
    activation_cas_registered = all(
        callable(getattr(record_store, name, None))
        for name in (
            "install_ordinary_agent_delivery_activation",
            "revoke_ordinary_agent_delivery_activation",
        )
    )
    activation_recovery_registered = callable(
        getattr(
            record_store,
            "recover_ordinary_agent_delivery_activation_by_source_operation",
            None,
        )
    )
    rollback_reader_registered = callable(
        getattr(record_store, "read_ordinary_agent_delivery_activation_record", None)
    )
    compare_write = getattr(record_store, "compare_and_write_authz_policy_record", None)
    policy_v3_write_supported = (
        callable(classify_authz_policy_schema_v3_transition)
        and callable(compare_write)
        and "schema_v3_write_evidence" in inspect.signature(compare_write).parameters
    )
    return OrdinaryAgentDeliveryRuntimeCapabilityEvidence(
        observed_database_revision=observed_revision or "unavailable",
        database_revision_compatible=(
            bool(observed_revision) and observed_revision in RUNTIME_COMPATIBLE_ALEMBIC_REVISIONS
        ),
        activation_schema_invariants_sha256=invariants_sha256,
        activation_schema_invariants_valid=invariants_valid,
        finite_request_versions=tuple(
            sorted(
                {
                    _model_schema_version(OrdinaryAgentFiniteRequestRecord),
                    _model_schema_version(OrdinaryAgentQualificationFiniteRequestV2),
                    _model_schema_version(OrdinaryAgentGuardedDeliveryFiniteRequestV2),
                }
            )
        ),
        read_attempt_versions=tuple(
            sorted(
                {
                    _model_schema_version(OrdinaryAgentSnapshotAttemptRecord),
                    _model_schema_version(OrdinaryAgentQualificationAttemptRecord),
                }
            )
        ),
        custody_issue_attempt_versions=(_model_schema_version(OrdinaryAgentCustodyIssueAttempt),),
        qualification_attestation_versions=(
            _model_schema_version(OrdinaryAgentQualificationAttestation),
        ),
        activation_record_versions=(_model_schema_version(OrdinaryAgentDeliveryActivationRecord),),
        activation_event_versions=(_model_schema_version(OrdinaryAgentDeliveryActivationEvent),),
        recovery_versions=(1,) if activation_recovery_registered else (),
        authz_policy_read_versions=tuple(sorted(SUPPORTED_MANAGED_RULE_POLICY_SCHEMA_VERSIONS)),
        variant_parsers_registered=callable(parse_ordinary_agent_finite_request)
        and callable(parse_ordinary_agent_read_attempt),
        activation_storage_registered=activation_storage_registered,
        activation_cas_registered=activation_cas_registered,
        activation_recovery_registered=activation_recovery_registered,
        bounded_cleanup_registered=callable(
            getattr(record_store, "expire_ordinary_agent_deliveries", None)
        ),
        rollback_reader_registered=rollback_reader_registered,
        qualification_advancer_registered=callable(
            getattr(record_store, "advance_ordinary_agent_qualification", None)
        ),
        guarded_worker_registered=callable(
            getattr(record_store, "claim_guarded_ordinary_agent_delivery", None)
        ),
        policy_v3_write_supported=policy_v3_write_supported,
        observed_at=observed_at,
    )


def _activation_records(record_store: object) -> tuple[OrdinaryAgentDeliveryActivationRecord, ...]:
    reader = getattr(record_store, "list_ordinary_agent_delivery_activation_records", None)
    if not callable(reader):
        return ()
    try:
        records = tuple(reader(limit=None))
    except (RuntimeError, TypeError, ValueError) as error:
        raise OrdinaryAgentDeliveryActivationPlanningError(
            "Activation planning could not read activation history."
        ) from error
    return tuple(
        record
        if isinstance(record, OrdinaryAgentDeliveryActivationRecord)
        else OrdinaryAgentDeliveryActivationRecord.model_validate(record)
        for record in records
    )


def _scope_predecessor(
    record_store: object,
    *,
    scope: OrdinaryAgentDeliveryActivationScope,
    requested: OrdinaryAgentDeliveryActivationReference | None,
    observed_at: datetime,
) -> OrdinaryAgentDeliveryActivationReference | None:
    matching = tuple(
        record
        for record in _activation_records(record_store)
        if _same_activation_scope_identity(record.scope, scope)
    )
    if not matching:
        if requested is not None:
            raise OrdinaryAgentDeliveryActivationPlanningError(
                "Activation setup supplied a predecessor for an empty scope."
            )
        return None
    newest_time = max(datetime.fromisoformat(record.installed_at) for record in matching)
    newest = tuple(
        record for record in matching if datetime.fromisoformat(record.installed_at) == newest_time
    )
    if len(newest) != 1:
        raise OrdinaryAgentDeliveryActivationPlanningError(
            "Activation history does not have one newest intent."
        )
    record = newest[0]
    structurally_current = not record.revoked_at and not record.superseded_by_activation_id
    if structurally_current and observed_at < datetime.fromisoformat(record.activation_expires_at):
        raise OrdinaryAgentDeliveryActivationPlanningError(
            "Activation scope already has an unexpired guarded intent."
        )
    expected = OrdinaryAgentDeliveryActivationReference(
        activation_id=record.activation_id,
        revision=record.revision,
        activation_sha256=record.activation_sha256,
    )
    if requested != expected:
        raise OrdinaryAgentDeliveryActivationPlanningError(
            "Activation setup requires the exact latest predecessor reference."
        )
    return expected


def _available_scope_predecessor(
    record_store: object,
    *,
    scope: OrdinaryAgentDeliveryActivationScope,
    observed_at: datetime,
) -> OrdinaryAgentDeliveryActivationReference | None:
    matching = tuple(
        record
        for record in _activation_records(record_store)
        if _same_activation_scope_identity(record.scope, scope)
    )
    if not matching:
        return None
    newest_time = max(datetime.fromisoformat(record.installed_at) for record in matching)
    newest = tuple(
        record for record in matching if datetime.fromisoformat(record.installed_at) == newest_time
    )
    if len(newest) != 1:
        raise OrdinaryAgentDeliveryActivationPlanningError(
            "Activation history does not have one newest intent."
        )
    record = newest[0]
    if (
        not record.revoked_at
        and not record.superseded_by_activation_id
        and observed_at < datetime.fromisoformat(record.activation_expires_at)
    ):
        raise OrdinaryAgentDeliveryActivationPlanningError(
            "Activation scope already has an unexpired guarded intent."
        )
    return OrdinaryAgentDeliveryActivationReference(
        activation_id=record.activation_id,
        revision=record.revision,
        activation_sha256=record.activation_sha256,
    )


def _read_activation(
    record_store: object,
    activation_id: str,
) -> OrdinaryAgentDeliveryActivationRecord:
    reader = getattr(record_store, "read_ordinary_agent_delivery_activation_record", None)
    if not callable(reader):
        raise OrdinaryAgentDeliveryActivationPlanningError(
            "Activation revocation requires activation storage."
        )
    try:
        record = reader(activation_id)
    except FileNotFoundError as error:
        raise OrdinaryAgentDeliveryActivationPlanningError(
            "Activation revocation target was not found."
        ) from error
    return (
        record
        if isinstance(record, OrdinaryAgentDeliveryActivationRecord)
        else OrdinaryAgentDeliveryActivationRecord.model_validate(record)
    )


def plan_ordinary_agent_delivery_activation(
    record_store: object,
    request: OrdinaryAgentDeliveryActivationRequest,
    *,
    observed_at: datetime | None = None,
) -> (
    OrdinaryAgentDeliveryActivationSetupHumanEvidence
    | OrdinaryAgentDeliveryActivationRevokeHumanEvidence
):
    now = (observed_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if isinstance(request, OrdinaryAgentDeliveryActivationSetupRequest):
        expires_at = datetime.fromisoformat(request.activation_expires_at)
        if expires_at <= now:
            raise OrdinaryAgentDeliveryActivationPlanningError(
                "Activation setup expiry must be in the future."
            )
        if expires_at > now + _MAX_ACTIVATION_DURATION:
            raise OrdinaryAgentDeliveryActivationPlanningError(
                "Activation setup expiry cannot exceed 30 days."
            )
        source = resolve_ordinary_agent_delivery_activation_setup_source(
            record_store,
            policy_operation_id=request.policy_operation_id,
            repository_inventory_record_id=request.repository_inventory_record_id,
            observed_at=now,
        )
        predecessor = _scope_predecessor(
            record_store,
            scope=source.scope,
            requested=request.predecessor,
            observed_at=now,
        )
        runtime_capability = _runtime_capability(
            record_store,
            observed_at=source.observed_at,
        )
        blockers = tuple(sorted(runtime_capability.setup_blockers))
        plan_digest = ordinary_agent_delivery_activation_plan_sha256(
            {
                "action": request.action,
                "scope": source.scope.model_dump(mode="json"),
                "policy_package": source.policy_package.model_dump(mode="json"),
                "inventory": source.inventory.model_dump(mode="json"),
                "predecessor": (
                    predecessor.model_dump(mode="json") if predecessor is not None else None
                ),
                "activation_expires_at": request.activation_expires_at,
                "runtime_capability": runtime_capability.model_dump(mode="json"),
                "blocker_codes": blockers,
            }
        )
        return OrdinaryAgentDeliveryActivationSetupHumanEvidence(
            result_status="blocked" if blockers else "ok",
            scope=source.scope,
            policy_package=source.policy_package,
            inventory=source.inventory,
            predecessor=predecessor,
            activation_expires_at=request.activation_expires_at,
            runtime_capability=runtime_capability,
            blocker_codes=blockers,
            plan_digest=plan_digest,
        )
    if isinstance(request, OrdinaryAgentDeliveryActivationRevokeRequest):
        record = _read_activation(record_store, request.activation_id)
        if (
            record.revision != request.expected_revision
            or record.activation_sha256 != request.expected_activation_sha256
        ):
            raise OrdinaryAgentDeliveryActivationPlanningError(
                "Activation revocation target no longer matches the expected revision and digest."
            )
        if record.desired_state == "revoked" or record.superseded_by_activation_id:
            raise OrdinaryAgentDeliveryActivationPlanningError(
                "Activation revocation target is already terminal."
            )
        activation = OrdinaryAgentDeliveryActivationReference(
            activation_id=record.activation_id,
            revision=record.revision,
            activation_sha256=record.activation_sha256,
        )
        return OrdinaryAgentDeliveryActivationRevokeHumanEvidence(
            scope=record.scope,
            activation=activation,
            source_setup_operation_id=record.source_setup_operation_id,
            plan_digest=ordinary_agent_delivery_activation_plan_sha256(
                {
                    "action": request.action,
                    "scope": record.scope.model_dump(mode="json"),
                    "activation": activation.model_dump(mode="json"),
                    "source_setup_operation_id": record.source_setup_operation_id,
                }
            ),
        )
    raise OrdinaryAgentDeliveryActivationPlanningError(
        "Activation planner received an unsupported request variant."
    )


def ordinary_agent_delivery_activation_duration_options(
    *, observed_at: datetime | None = None
) -> tuple[OrdinaryAgentDeliveryActivationDurationOption, ...]:
    now = (observed_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return tuple(
        OrdinaryAgentDeliveryActivationDurationOption(
            duration_seconds=duration_seconds,
            activation_expires_at=(now + timedelta(seconds=duration_seconds)).isoformat(),
            label=label,
        )
        for duration_seconds, label in _ACTIVATION_DURATION_OPTIONS
    )


def list_ordinary_agent_delivery_activation_options(
    record_store: object,
    *,
    observed_at: datetime | None = None,
) -> tuple[
    tuple[OrdinaryAgentDeliveryActivationSetupOption, ...],
    tuple[OrdinaryAgentDeliveryActivationRevokeOption, ...],
]:
    operation_reader = getattr(record_store, "list_privileged_operation_records", None)
    inventory_reader = getattr(record_store, "list_repository_inventory_records", None)
    if not callable(operation_reader) or not callable(inventory_reader):
        raise OrdinaryAgentDeliveryActivationPlanningError(
            "Activation options require operation and repository inventory storage."
        )
    now = (observed_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    setup_options: list[OrdinaryAgentDeliveryActivationSetupOption] = []
    for operation in operation_reader(
        descriptor_id="managed-authz-policy-set",
        limit=None,
    ):
        try:
            typed_operation = (
                operation
                if isinstance(operation, PrivilegedOperationRecord)
                else PrivilegedOperationRecord.model_validate(operation)
            )
            request, _ = _require_admissible_policy_operation(typed_operation, observed_at=now)
            scope = _activation_scope(request)
            inventories = tuple(
                inventory_reader(
                    repository_id=str(scope.target.repository_id),
                    limit=None,
                )
            )
            if not inventories:
                continue
            typed_inventories = tuple(
                item
                if isinstance(item, RepositoryInventoryRecord)
                else RepositoryInventoryRecord.model_validate(item)
                for item in inventories
            )
            highest_revision = max(item.inventory_revision for item in typed_inventories)
            current = tuple(
                item
                for item in typed_inventories
                if item.inventory_revision == highest_revision and item.inventory_state == "tracked"
            )
            if len(current) != 1:
                continue
            resolve_ordinary_agent_delivery_activation_setup_source(
                record_store,
                policy_operation_id=typed_operation.operation_id,
                repository_inventory_record_id=current[0].record_id,
                observed_at=now,
            )
            predecessor = _available_scope_predecessor(
                record_store,
                scope=scope,
                observed_at=now,
            )
        except (OrdinaryAgentDeliveryActivationPlanningError, TypeError, ValueError):
            continue
        setup_options.append(
            OrdinaryAgentDeliveryActivationSetupOption(
                policy_operation_id=typed_operation.operation_id,
                repository_inventory_record_id=current[0].record_id,
                scope=scope,
                predecessor=predecessor,
                label=(
                    f"{scope.target.repository} · {scope.target.base_branch} · prepared "
                    f"{_activation_option_timestamp(typed_operation.created_at)}"
                ),
            )
        )
    revoke_options = tuple(
        OrdinaryAgentDeliveryActivationRevokeOption(
            activation=OrdinaryAgentDeliveryActivationReference(
                activation_id=record.activation_id,
                revision=record.revision,
                activation_sha256=record.activation_sha256,
            ),
            scope=record.scope,
            label=(
                f"{record.scope.target.repository} · {record.scope.target.base_branch} · set up "
                f"{_activation_option_timestamp(record.installed_at)} · allowed until "
                f"{_activation_option_timestamp(record.activation_expires_at)}"
            ),
        )
        for record in _activation_records(record_store)
        if record.desired_state == "guarded"
        and not record.superseded_by_activation_id
        and now < datetime.fromisoformat(record.activation_expires_at)
    )
    return (
        tuple(sorted(setup_options, key=lambda option: option.label.casefold())),
        tuple(sorted(revoke_options, key=lambda option: option.label.casefold())),
    )
