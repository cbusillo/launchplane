from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane import secrets as control_plane_secrets
from control_plane.authz_grant_service import (
    AuthzPolicyConflictError,
    plan_managed_authz_policy_reconcile,
)
from control_plane.contracts.privileged_operation import (
    AUTHZ_POLICY_OPERATION_APPROVE_ACTION,
    AUTHZ_POLICY_OPERATION_CANCEL_ACTION,
    AUTHZ_POLICY_OPERATION_PROPOSE_ACTION,
    AUTHZ_POLICY_OPERATION_READ_ACTION,
    AUTHZ_POLICY_OPERATION_REVOKE_ACTION,
    ManagedAuthzPolicySetHumanEvidence,
    ManagedAuthzPolicySetProposalInput,
    ManagedSecretReencryptionHumanEvidence,
    ManagedSecretReencryptionPlanInput,
    PRIVILEGED_OPERATION_SUMMARY_READ_ACTION,
    PRIVILEGED_POLICY_OPERATION_SUMMARY_READ_ACTION,
    PRIVILEGED_SECRET_OPERATION_APPROVE_ACTION,
    PRIVILEGED_SECRET_OPERATION_CANCEL_ACTION,
    PRIVILEGED_SECRET_OPERATION_PLAN_ACTION,
    PRIVILEGED_SECRET_OPERATION_READ_ACTION,
    PRIVILEGED_SECRET_OPERATION_REVOKE_ACTION,
    PrivilegedOperationDescriptorId,
    PrivilegedOperationHumanEvidence,
    PrivilegedOperationRequest,
    PrivilegedOperationSafetyClass,
)
from control_plane.service_auth import AgentConsumerActionSafety, action_safety


class PrivilegedOperationDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    descriptor_id: PrivilegedOperationDescriptorId
    descriptor_version: int = Field(default=1, ge=1)
    safety_class: PrivilegedOperationSafetyClass
    plan_action: str
    human_read_action: str
    cancel_action: str
    approve_action: str
    revoke_action: str
    agent_summary_read_action: str

    @model_validator(mode="after")
    def _validate_descriptor(self) -> "PrivilegedOperationDescriptor":
        if self.descriptor_version != 1:
            raise ValueError("Unsupported privileged-operation descriptor version.")
        actions = (
            self.plan_action,
            self.human_read_action,
            self.cancel_action,
            self.approve_action,
            self.revoke_action,
            self.agent_summary_read_action,
        )
        if any(not action.strip() for action in actions):
            raise ValueError("Privileged-operation descriptor actions must be non-empty")
        if len(set(actions)) != len(actions):
            raise ValueError("Privileged-operation descriptor actions must be unique")
        expected_safety: tuple[AgentConsumerActionSafety, ...] = (
            self.safety_class,
            self.safety_class,
            self.safety_class,
            self.safety_class,
            self.safety_class,
            "read",
        )
        actual_safety = tuple(action_safety(action) for action in actions)
        if actual_safety != expected_safety:
            raise ValueError(
                "Privileged-operation descriptor action safety classifications are invalid: "
                f"{actual_safety!r}"
            )
        return self


class PrivilegedOperationPlannerError(RuntimeError):
    pass


class PrivilegedOperationPlanningStoreError(TypeError):
    pass


PrivilegedOperationPlanner = Callable[
    [object, PrivilegedOperationRequest],
    PrivilegedOperationHumanEvidence,
]


@dataclass(frozen=True, slots=True)
class RegisteredPrivilegedOperationDescriptor:
    descriptor: PrivilegedOperationDescriptor
    planner: PrivilegedOperationPlanner


MANAGED_SECRET_REENCRYPTION_DESCRIPTOR = PrivilegedOperationDescriptor(
    descriptor_id="managed-secret-reencryption",
    descriptor_version=1,
    safety_class="secret_backed",
    plan_action=PRIVILEGED_SECRET_OPERATION_PLAN_ACTION,
    human_read_action=PRIVILEGED_SECRET_OPERATION_READ_ACTION,
    cancel_action=PRIVILEGED_SECRET_OPERATION_CANCEL_ACTION,
    approve_action=PRIVILEGED_SECRET_OPERATION_APPROVE_ACTION,
    revoke_action=PRIVILEGED_SECRET_OPERATION_REVOKE_ACTION,
    agent_summary_read_action=PRIVILEGED_OPERATION_SUMMARY_READ_ACTION,
)

MANAGED_AUTHZ_POLICY_SET_DESCRIPTOR = PrivilegedOperationDescriptor(
    descriptor_id="managed-authz-policy-set",
    descriptor_version=1,
    safety_class="policy_admin",
    plan_action=AUTHZ_POLICY_OPERATION_PROPOSE_ACTION,
    human_read_action=AUTHZ_POLICY_OPERATION_READ_ACTION,
    cancel_action=AUTHZ_POLICY_OPERATION_CANCEL_ACTION,
    approve_action=AUTHZ_POLICY_OPERATION_APPROVE_ACTION,
    revoke_action=AUTHZ_POLICY_OPERATION_REVOKE_ACTION,
    agent_summary_read_action=PRIVILEGED_POLICY_OPERATION_SUMMARY_READ_ACTION,
)


def _required_int(result: Mapping[str, object], field_name: str) -> int:
    value = result.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PrivilegedOperationPlannerError(
            f"Managed-secret re-encryption planner returned invalid {field_name}."
        )
    return value


def _required_bool(result: Mapping[str, object], field_name: str) -> bool:
    value = result.get(field_name)
    if not isinstance(value, bool):
        raise PrivilegedOperationPlannerError(
            f"Managed-secret re-encryption planner returned invalid {field_name}."
        )
    return value


def _required_string(result: Mapping[str, object], field_name: str) -> str:
    value = result.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise PrivilegedOperationPlannerError(
            f"Managed-secret re-encryption planner returned invalid {field_name}."
        )
    return value.strip()


def _required_string_tuple(result: Mapping[str, object], field_name: str) -> tuple[str, ...]:
    value = result.get(field_name)
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise PrivilegedOperationPlannerError(
            f"Managed-secret re-encryption planner returned invalid {field_name}."
        )
    return tuple(cast(str, item).strip() for item in value)


def plan_managed_secret_reencryption(
    record_store: object,
    request: PrivilegedOperationRequest,
) -> ManagedSecretReencryptionHumanEvidence:
    if not isinstance(request, ManagedSecretReencryptionPlanInput):
        raise PrivilegedOperationPlannerError(
            "Managed-secret re-encryption planner received an invalid request type."
        )
    list_records = getattr(record_store, "list_secret_records", None)
    read_version = getattr(record_store, "read_secret_version", None)
    if not callable(list_records) or not callable(read_version):
        raise PrivilegedOperationPlanningStoreError(
            "Managed-secret privileged-operation planning requires secret record storage."
        )
    try:
        raw_result = control_plane_secrets.reencrypt_secrets(
            record_store=cast(control_plane_secrets.SecretRotationStore, record_store),
            apply=False,
            actor="github-human",
            source_label=request.source_label,
            reason=request.reason,
        )
    except (click.ClickException, FileNotFoundError, ValueError) as error:
        raise PrivilegedOperationPlannerError(
            "Managed-secret re-encryption planning failed before evidence was produced."
        ) from error
    if raw_result.get("dry_run") is not True:
        raise PrivilegedOperationPlannerError(
            "Managed-secret privileged-operation planning must remain dry-run only."
        )
    result_status = raw_result.get("status")
    if result_status not in {"ok", "error"}:
        raise PrivilegedOperationPlannerError(
            "Managed-secret re-encryption planner returned invalid status."
        )
    rotation_candidate_count = _required_int(raw_result, "rotated_count")
    unchanged_count = _required_int(raw_result, "unchanged_count")
    unreadable_secret_count = _required_int(raw_result, "error_count")
    normalized_result_status: Literal["ok", "error"] = "ok" if result_status == "ok" else "error"
    return ManagedSecretReencryptionHumanEvidence(
        result_status=normalized_result_status,
        plan_digest=_required_string(raw_result, "plan_digest"),
        configured_secret_count=rotation_candidate_count + unchanged_count,
        rotation_candidate_count=rotation_candidate_count,
        unchanged_count=unchanged_count,
        unreadable_secret_count=unreadable_secret_count,
        active_key_id=_required_string(raw_result, "active_key_id"),
        retirement_blocked_key_ids=_required_string_tuple(
            raw_result,
            "retirement_blocked_key_ids",
        ),
        retirement_ready_key_ids=_required_string_tuple(
            raw_result,
            "retirement_ready_key_ids",
        ),
        legacy_compatibility_key_loaded=_required_bool(
            raw_result,
            "legacy_compatibility_key_loaded",
        ),
    )


def plan_managed_authz_policy_set(
    record_store: object,
    request: PrivilegedOperationRequest,
) -> ManagedAuthzPolicySetHumanEvidence:
    if not isinstance(request, ManagedAuthzPolicySetProposalInput):
        raise PrivilegedOperationPlannerError(
            "Managed authz policy planner received an invalid request type."
        )
    if not callable(getattr(record_store, "list_authz_policy_records", None)):
        raise PrivilegedOperationPlanningStoreError(
            "Managed authz policy planning requires authorization policy storage."
        )
    try:
        _, _, _, diff = plan_managed_authz_policy_reconcile(
            record_store=cast(Any, record_store),
            request=request.reconcile_request(),
        )
    except (AuthzPolicyConflictError, LookupError, TypeError, ValueError) as error:
        raise PrivilegedOperationPlannerError(
            "Managed authz policy planning failed before evidence was produced."
        ) from error
    blocked = bool(
        diff.policy_safety_blocker_count or diff.operational_readiness_blocked_rule_count
    )
    return ManagedAuthzPolicySetHumanEvidence(
        result_status="blocked" if blocked else "ok",
        plan_digest=diff.plan_sha256,
        diff=diff,
    )


_REGISTRY: dict[PrivilegedOperationDescriptorId, RegisteredPrivilegedOperationDescriptor] = {
    "managed-secret-reencryption": RegisteredPrivilegedOperationDescriptor(
        descriptor=MANAGED_SECRET_REENCRYPTION_DESCRIPTOR,
        planner=plan_managed_secret_reencryption,
    ),
    "managed-authz-policy-set": RegisteredPrivilegedOperationDescriptor(
        descriptor=MANAGED_AUTHZ_POLICY_SET_DESCRIPTOR,
        planner=plan_managed_authz_policy_set,
    ),
}


def validate_privileged_operation_registry() -> None:
    if not _REGISTRY:
        raise RuntimeError("Privileged-operation descriptor registry cannot be empty.")
    for descriptor_id, registration in _REGISTRY.items():
        descriptor = registration.descriptor
        if descriptor.descriptor_id != descriptor_id:
            raise RuntimeError(
                "Privileged-operation registry key does not match descriptor identity."
            )
        if not callable(registration.planner):
            raise RuntimeError(f"Privileged-operation descriptor {descriptor_id!r} has no planner.")


def read_privileged_operation_descriptor(
    descriptor_id: PrivilegedOperationDescriptorId,
) -> RegisteredPrivilegedOperationDescriptor:
    try:
        return _REGISTRY[descriptor_id]
    except KeyError as error:
        raise LookupError(f"Unknown privileged-operation descriptor: {descriptor_id}") from error


def list_privileged_operation_descriptors() -> tuple[PrivilegedOperationDescriptor, ...]:
    return tuple(
        registration.descriptor
        for _, registration in sorted(_REGISTRY.items(), key=lambda item: item[0])
    )


validate_privileged_operation_registry()
