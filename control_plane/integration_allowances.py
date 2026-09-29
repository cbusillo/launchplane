"""Supported, audited reads and writes of a lane's integration allowances.

A non-production lane must not hold production integration settings unless an
allowance recorded here says why. The allowance list lives on the lane's tracked
Dokploy target record. Writes replace the whole list: dry-run returns a redacted diff
and a digest, and apply requires that digest, then reads the record back.
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
    IntegrationAllowanceKind,
)
from control_plane.runtime_key_safety import runtime_key_safety_environment_class

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


class IntegrationAllowancesStore(Protocol):
    def read_dokploy_target_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetRecord: ...

    def write_dokploy_target_record(self, record: DokployTargetRecord) -> object: ...


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
        self.product = self.product.strip()
        self.context = self.context.strip().lower()
        self.instance = self.instance.strip().lower()
        self.reason = self.reason.strip()
        self.reviewed_plan_sha256 = self.reviewed_plan_sha256.strip().lower()
        for field_name in ("product", "context", "instance", "reason"):
            if not getattr(self, field_name):
                raise ValueError(f"Integration allowances request requires {field_name}.")
        if self.mode == "dry-run" and self.reviewed_plan_sha256:
            raise ValueError("Integration allowances dry-run rejects reviewed_plan_sha256.")
        if self.mode == "apply" and (
            len(self.reviewed_plan_sha256) != _SHA256_LENGTH
            or any(character not in "0123456789abcdef" for character in self.reviewed_plan_sha256)
        ):
            raise ValueError(
                "Integration allowances apply requires the reviewed 64-character plan SHA-256."
            )
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
    record_sha256: str


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _record_sha256(record: DokployTargetRecord) -> str:
    return _canonical_sha256(record.model_dump(mode="json"))


def _utc_now_timestamp() -> str:
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


def read_integration_allowances(
    *, record_store: IntegrationAllowancesStore, product: str, context: str, instance: str
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
        record_sha256=_record_sha256(target),
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
        request=request, existing=existing, actor=actor, recorded_at=_utc_now_timestamp()
    )
    # Validate the full replacement through the contract: uniqueness and evidence rules.
    replacement_policies = target.policies.model_copy(update={"integration_allowances": desired})
    replacement_policies = type(target.policies).model_validate(
        replacement_policies.model_dump(mode="json")
    )
    desired = replacement_policies.integration_allowances
    _validate_lane(environment_class=environment_class, allowances=desired)
    changes = _changes(before=existing, after=desired)
    record_sha256_before = _record_sha256(target)
    plan_sha256 = _canonical_sha256(
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
) -> IntegrationAllowancesPlan:
    """Re-plan against the current record, require the reviewed digest, write, read back."""

    plan, replacement = build_integration_allowances_plan(
        record_store=record_store, request=request, actor=actor
    )
    if request.reviewed_plan_sha256 != plan.plan_sha256:
        raise IntegrationAllowancesStale(
            "Reviewed integration allowances plan no longer matches the lane's record."
        )
    applied = False
    if plan.changed:
        record_store.write_dokploy_target_record(
            replacement.model_copy(
                update={
                    "updated_at": _utc_now_timestamp(),
                    "source_label": INTEGRATION_ALLOWANCES_SOURCE_LABEL,
                }
            )
        )
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
            == [_allowance_terms(item) for item in replacement.policies.integration_allowances],
            "record_sha256_after": _record_sha256(stored),
        }
    )
