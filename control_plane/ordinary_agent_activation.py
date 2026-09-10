"""Planning helpers for inert ordinary-agent delivery activation administration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, cast

from pydantic import BaseModel

from control_plane.authz_grant_service import plan_managed_authz_policy_reconcile
from control_plane.contracts.authz_policy_record import (
    AuthzPolicySchemaWriteNotActivatedError,
    LaunchplaneAuthzPolicyRecord,
    require_authz_policy_schema_write_activated,
)
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.ordinary_agent_activation import (
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
from control_plane.contracts.ordinary_agent_custody import OrdinaryAgentCustodyIssueAttempt
from control_plane.contracts.ordinary_agent_effect import OrdinaryAgentSnapshotAttemptRecord
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


_ADMISSIBLE_POLICY_OPERATION_STATUSES = frozenset({"planned", "approved", "executing", "executed"})


class OrdinaryAgentDeliveryActivationPlanningError(ValueError):
    """Raised when referenced server-owned setup data does not resolve exactly."""


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
    if record.status not in _ADMISSIBLE_POLICY_OPERATION_STATUSES:
        raise OrdinaryAgentDeliveryActivationPlanningError(
            "Referenced policy operation status is not admissible for activation setup."
        )
    if record.status in {"planned", "approved"} and observed_at >= datetime.fromisoformat(
        record.expires_at
    ):
        raise OrdinaryAgentDeliveryActivationPlanningError(
            "Referenced policy operation has passed its finite approval lifetime."
        )
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
    try:
        require_authz_policy_schema_write_activated(LaunchplaneAuthzPolicy(schema_version=3))
    except AuthzPolicySchemaWriteNotActivatedError:
        policy_v3_write_supported = False
    else:
        policy_v3_write_supported = True
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
        read_attempt_versions=(_model_schema_version(OrdinaryAgentSnapshotAttemptRecord),),
        custody_reservation_versions=(_model_schema_version(OrdinaryAgentCustodyIssueAttempt),),
        qualification_attestation_versions=(),
        activation_record_versions=(_model_schema_version(OrdinaryAgentDeliveryActivationRecord),),
        activation_event_versions=(_model_schema_version(OrdinaryAgentDeliveryActivationEvent),),
        recovery_versions=(1,) if activation_recovery_registered else (),
        authz_policy_read_versions=tuple(sorted(SUPPORTED_MANAGED_RULE_POLICY_SCHEMA_VERSIONS)),
        variant_parsers_registered=callable(parse_ordinary_agent_finite_request),
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
        record for record in _activation_records(record_store) if record.scope == scope
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
        record for record in _activation_records(record_store) if record.scope == scope
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
                label=f"{scope.target.repository} · {scope.target.base_branch}",
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
            label=(f"{record.scope.target.repository} · {record.scope.target.base_branch}"),
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
