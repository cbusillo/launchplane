"""Supported, audited reads and writes of a lane's integration allowances.

A non-production lane must not hold production integration settings unless an
allowance recorded here says why. The allowance list lives on the lane's tracked
Dokploy target record. Writes replace the whole list: dry-run returns a redacted diff
and a digest, and apply requires that digest, then reads the record back.

The read also lists the integration keys stored for the lane itself, with each
one's declared class and recorded sharing reason (names and metadata, never
values), so a later reader can see why a production key is on the lane.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, model_validator

from control_plane.contracts.dokploy_target_record import (
    DokployTargetIntegrationAllowance,
    DokployTargetRecord,
    DokployTargetRecordChanged,
    IntegrationAllowanceKind,
)
from control_plane.contracts.runtime_key_safety_policy import (
    RuntimeKeySafetyPolicyRecord,
    RuntimeSecretClass,
)
from control_plane.contracts.secret_record import SecretBinding, SecretSharingReason
from control_plane.runtime_key_safety import (
    is_integration_runtime_key,
    runtime_key_safety_environment_class,
)

INTEGRATION_ALLOWANCES_ROUTE = "/v1/product-config/integration-allowances"
INTEGRATION_ALLOWANCES_APPLY_ROUTE = "/v1/product-config/integration-allowances/apply"
INTEGRATION_ALLOWANCES_SOURCE_LABEL = "service:integration-allowances"

IntegrationAllowancesMode = Literal["dry-run", "apply"]
IntegrationAllowancesRefusalCode = Literal[
    "target_record_missing",
    "production_lane",
    "pre_live_not_allowed",
]
_SHA256_LENGTH = 64


class IntegrationAllowancesRefusal(ValueError):
    def __init__(self, code: IntegrationAllowancesRefusalCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class IntegrationAllowancesStale(ValueError):
    pass


class ReviewedLaneRequest(Protocol):
    product: str
    context: str
    instance: str
    mode: Literal["dry-run", "apply"]
    reason: str
    reviewed_plan_sha256: str


def normalize_reviewed_lane_request(request: ReviewedLaneRequest, *, label: str) -> None:
    """Normalize a lane product-config request; apply must name the reviewed plan digest."""
    request.product = request.product.strip()
    request.context = request.context.strip().lower()
    request.instance = request.instance.strip().lower()
    request.reason = request.reason.strip()
    request.reviewed_plan_sha256 = request.reviewed_plan_sha256.strip().lower()
    for field_name in ("product", "context", "instance", "reason"):
        if not getattr(request, field_name):
            raise ValueError(f"{label} request requires {field_name}.")
    if request.mode == "dry-run" and request.reviewed_plan_sha256:
        raise ValueError(f"{label} dry-run rejects reviewed_plan_sha256.")
    if request.mode == "apply" and (
        len(request.reviewed_plan_sha256) != _SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in request.reviewed_plan_sha256)
    ):
        raise ValueError(f"{label} apply requires the reviewed 64-character plan SHA-256.")


class IntegrationAllowancesStore(Protocol):
    def read_dokploy_target_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetRecord: ...

    def compare_and_write_dokploy_target_record(
        self,
        *,
        expected_record: DokployTargetRecord,
        replacement_record: DokployTargetRecord,
        required_context_owner: tuple[str, str] | None = None,
    ) -> DokployTargetRecord: ...


class IntegrationAllowancesReadStore(IntegrationAllowancesStore, Protocol):
    def list_secret_bindings(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[SecretBinding, ...]: ...

    def list_runtime_key_safety_policy_records(
        self,
        *,
        status: str = "",
        limit: int | None = None,
    ) -> tuple[RuntimeKeySafetyPolicyRecord, ...]: ...


class LaneIntegrationKey(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binding_key: str
    declared_secret_class: RuntimeSecretClass | None = None
    sharing_reason: SecretSharingReason | None = None


class IntegrationAllowanceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    integration: str
    kind: IntegrationAllowanceKind
    reason: str
    evidence: str = ""


class IntegrationAllowancesApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    product: str
    context: str
    instance: str
    mode: IntegrationAllowancesMode = "dry-run"
    reason: str
    reviewed_plan_sha256: str = ""
    allowances: tuple[IntegrationAllowanceInput, ...] = ()

    @model_validator(mode="after")
    def _validate_request(self) -> IntegrationAllowancesApplyRequest:
        normalize_reviewed_lane_request(self, label="Integration allowances")
        return self


class IntegrationAllowanceChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    integration: str
    action: Literal["add", "update", "remove", "unchanged"]
    before: DokployTargetIntegrationAllowance | None = None
    after: DokployTargetIntegrationAllowance | None = None


class IntegrationAllowancesPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    mode: IntegrationAllowancesMode
    product: str
    context: str
    instance: str
    environment_class: str
    changed: bool
    applied: bool = False
    changes: tuple[IntegrationAllowanceChange, ...]
    read_back: tuple[DokployTargetIntegrationAllowance, ...] = ()
    read_back_matches: bool | None = None
    reason: str
    source_label: str = INTEGRATION_ALLOWANCES_SOURCE_LABEL
    record_sha256_before: str
    record_sha256_after: str = ""
    plan_sha256: str


class IntegrationAllowancesReadResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    product: str
    context: str
    instance: str
    environment_class: str
    allowances: tuple[DokployTargetIntegrationAllowance, ...]
    integration_keys: tuple[LaneIntegrationKey, ...] = ()
    record_sha256: str


def canonical_sha256(payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def target_record_sha256(record: DokployTargetRecord) -> str:
    return canonical_sha256(record.model_dump(mode="json"))


def utc_now_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_target_record(
    *, record_store: IntegrationAllowancesStore, context: str, instance: str
) -> DokployTargetRecord:
    try:
        return record_store.read_dokploy_target_record(context_name=context, instance_name=instance)
    except FileNotFoundError as error:
        raise IntegrationAllowancesRefusal(
            "target_record_missing",
            "Integration allowances require the lane's tracked target record.",
        ) from error


def _allowance_terms(allowance: DokployTargetIntegrationAllowance) -> tuple[str, str, str, str]:
    return allowance.integration, allowance.kind, allowance.reason, allowance.evidence


def _desired_allowances(
    *,
    request: IntegrationAllowancesApplyRequest,
    existing: tuple[DokployTargetIntegrationAllowance, ...],
    actor: str,
    recorded_at: str,
) -> tuple[DokployTargetIntegrationAllowance, ...]:
    existing_by_integration = {allowance.integration: allowance for allowance in existing}
    desired: list[DokployTargetIntegrationAllowance] = []
    for item in request.allowances:
        candidate = DokployTargetIntegrationAllowance(
            integration=item.integration,
            kind=item.kind,
            reason=item.reason,
            evidence=item.evidence,
            recorded_by=actor,
            recorded_at=recorded_at,
        )
        previous = existing_by_integration.get(candidate.integration)
        # An unchanged allowance keeps who recorded it and when.
        if previous is not None and _allowance_terms(previous) == _allowance_terms(candidate):
            candidate = previous
        desired.append(candidate)
    return tuple(desired)


def _validate_lane(
    *, environment_class: str, allowances: tuple[DokployTargetIntegrationAllowance, ...]
) -> None:
    if environment_class == "prod" and allowances:
        raise IntegrationAllowancesRefusal(
            "production_lane",
            "A production lane holds its own integration settings and takes no allowances.",
        )
    if environment_class not in {"testing", "dev"} and any(
        allowance.kind == "pre_live" for allowance in allowances
    ):
        raise IntegrationAllowancesRefusal(
            "pre_live_not_allowed",
            "A pre_live allowance is only for a testing or dev lane.",
        )


def _changes(
    *,
    before: tuple[DokployTargetIntegrationAllowance, ...],
    after: tuple[DokployTargetIntegrationAllowance, ...],
) -> tuple[IntegrationAllowanceChange, ...]:
    before_by_integration = {allowance.integration: allowance for allowance in before}
    after_by_integration = {allowance.integration: allowance for allowance in after}
    changes: list[IntegrationAllowanceChange] = []
    for integration in sorted(set(before_by_integration) | set(after_by_integration)):
        previous = before_by_integration.get(integration)
        desired = after_by_integration.get(integration)
        if previous is None:
            action: Literal["add", "update", "remove", "unchanged"] = "add"
        elif desired is None:
            action = "remove"
        elif _allowance_terms(previous) == _allowance_terms(desired):
            action = "unchanged"
        else:
            action = "update"
        changes.append(
            IntegrationAllowanceChange(
                integration=integration, action=action, before=previous, after=desired
            )
        )
    return tuple(changes)


def _lane_integration_keys(
    *, record_store: IntegrationAllowancesReadStore, context: str, instance: str
) -> tuple[LaneIntegrationKey, ...]:
    active_policies = record_store.list_runtime_key_safety_policy_records(status="active", limit=1)
    extra_markers = active_policies[0].integration_key_markers if active_policies else ()
    return tuple(
        LaneIntegrationKey(
            binding_key=binding.binding_key,
            declared_secret_class=binding.declared_secret_class,
            sharing_reason=binding.sharing_reason,
        )
        for binding in sorted(
            record_store.list_secret_bindings(
                integration="runtime_environment",
                context_name=context,
                instance_name=instance,
                limit=None,
            ),
            key=lambda binding: binding.binding_key,
        )
        if binding.status == "configured"
        and binding.context == context
        and binding.instance == instance
        and is_integration_runtime_key(binding.binding_key, extra_markers=extra_markers)
    )


def read_integration_allowances(
    *, record_store: IntegrationAllowancesReadStore, product: str, context: str, instance: str
) -> IntegrationAllowancesReadResult:
    context = context.strip().lower()
    instance = instance.strip().lower()
    target = _read_target_record(record_store=record_store, context=context, instance=instance)
    return IntegrationAllowancesReadResult(
        product=product.strip(),
        context=context,
        instance=instance,
        environment_class=runtime_key_safety_environment_class(instance),
        allowances=target.policies.integration_allowances,
        integration_keys=_lane_integration_keys(
            record_store=record_store, context=context, instance=instance
        ),
        record_sha256=target_record_sha256(target),
    )


def build_integration_allowances_plan(
    *,
    record_store: IntegrationAllowancesStore,
    request: IntegrationAllowancesApplyRequest,
    actor: str,
) -> tuple[IntegrationAllowancesPlan, DokployTargetRecord]:
    """Validate the request against the current record and return a digest-bound plan."""

    target = _read_target_record(
        record_store=record_store, context=request.context, instance=request.instance
    )
    environment_class = runtime_key_safety_environment_class(request.instance)
    existing = target.policies.integration_allowances
    desired = _desired_allowances(
        request=request, existing=existing, actor=actor, recorded_at=utc_now_timestamp()
    )
    # Validate the full replacement through the contract: uniqueness and evidence rules.
    replacement_policies = target.policies.model_copy(update={"integration_allowances": desired})
    replacement_policies = type(target.policies).model_validate(
        replacement_policies.model_dump(mode="json")
    )
    desired = replacement_policies.integration_allowances
    _validate_lane(environment_class=environment_class, allowances=desired)
    changes = _changes(before=existing, after=desired)
    record_sha256_before = target_record_sha256(target)
    plan_sha256 = canonical_sha256(
        {
            "product": request.product,
            "context": request.context,
            "instance": request.instance,
            "record_sha256_before": record_sha256_before,
            "desired": [_allowance_terms(allowance) for allowance in desired],
        }
    )
    plan = IntegrationAllowancesPlan(
        mode=request.mode,
        product=request.product,
        context=request.context,
        instance=request.instance,
        environment_class=environment_class,
        changed=any(change.action != "unchanged" for change in changes),
        changes=changes,
        reason=request.reason,
        record_sha256_before=record_sha256_before,
        plan_sha256=plan_sha256,
    )
    replacement = target.model_copy(update={"policies": replacement_policies})
    return plan, replacement


def apply_integration_allowances_plan(
    *,
    record_store: IntegrationAllowancesStore,
    request: IntegrationAllowancesApplyRequest,
    actor: str,
    required_context_owner: tuple[str, str] | None = None,
) -> IntegrationAllowancesPlan:
    """Re-plan against the current record, require the reviewed digest, write, read back."""

    plan, replacement = build_integration_allowances_plan(
        record_store=record_store, request=request, actor=actor
    )
    requested_terms = [
        _allowance_terms(item) for item in replacement.policies.integration_allowances
    ]
    applied = False
    if request.reviewed_plan_sha256 != plan.plan_sha256:
        current = _read_target_record(
            record_store=record_store, context=request.context, instance=request.instance
        )
        current_terms = [_allowance_terms(item) for item in current.policies.integration_allowances]
        # A retried apply whose first attempt wrote but lost its receipt finds the lane
        # already holding exactly the requested allowances: report it, don't refuse it.
        if current_terms != requested_terms:
            raise IntegrationAllowancesStale(
                "Reviewed integration allowances plan no longer matches the lane's record."
            )
        plan = plan.model_copy(update={"changed": False})
    elif plan.changed:
        expected = _read_target_record(
            record_store=record_store, context=request.context, instance=request.instance
        )
        if target_record_sha256(expected) != plan.record_sha256_before:
            raise IntegrationAllowancesStale(
                "Reviewed integration allowances plan no longer matches the lane's record."
            )
        try:
            record_store.compare_and_write_dokploy_target_record(
                expected_record=expected,
                replacement_record=replacement.model_copy(
                    update={
                        "updated_at": utc_now_timestamp(),
                        "source_label": INTEGRATION_ALLOWANCES_SOURCE_LABEL,
                    }
                ),
                **(
                    {"required_context_owner": required_context_owner}
                    if required_context_owner is not None
                    else {}
                ),
            )
        except DokployTargetRecordChanged as error:
            raise IntegrationAllowancesStale(
                "The lane's target record changed while the allowances were applied."
            ) from error
        applied = True
    stored = _read_target_record(
        record_store=record_store, context=request.context, instance=request.instance
    )
    stored_allowances = stored.policies.integration_allowances
    return plan.model_copy(
        update={
            "applied": applied,
            "read_back": stored_allowances,
            "read_back_matches": [_allowance_terms(item) for item in stored_allowances]
            == requested_terms,
            "record_sha256_after": target_record_sha256(stored),
        }
    )
