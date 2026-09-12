from __future__ import annotations

import hashlib
import json

from control_plane.contracts.merge_train_policy import (
    MergeTrainPolicyRecord,
    normalize_merge_train_policy_timestamp,
)
from control_plane.contracts.privileged_operation import (
    ManagedMergeTrainPolicyImportHumanEvidence,
    ManagedMergeTrainPolicyImportProposalInput,
    PrivilegedOperationRecord,
)


PRIVILEGED_OPERATION_EXECUTION_SCOPE = "privileged-operation-execution"
PRIVILEGED_OPERATION_EXECUTION_ROUTE = "service-internal:privileged-operation-worker"
PRIVILEGED_POLICY_OPERATION_WRITE_ROUTE = (
    "service-internal:privileged-operation-worker:managed-authz-policy-set"
)
PRIVILEGED_MERGE_TRAIN_POLICY_WRITE_ROUTE = (
    "service-internal:privileged-operation-worker:managed-merge-train-policy-import"
)
MANAGED_MERGE_TRAIN_POLICY_IMPORT_PRE_EFFECT_DENIED = (
    "managed_merge_train_policy_import_pre_effect_denied"
)


class ManagedMergeTrainPolicyImportPreEffectDeniedError(ValueError):
    """The governed policy fence rejected a write before changing policy state."""

    def __init__(self, reason: str = "") -> None:
        self.reason = reason.strip()
        super().__init__(MANAGED_MERGE_TRAIN_POLICY_IMPORT_PRE_EFFECT_DENIED)


def privileged_operation_execution_fingerprint(record: PrivilegedOperationRecord) -> str:
    if record.approval is None:
        raise ValueError("Privileged operation has no approval evidence.")
    return _digest(
        {
            "operation_id": record.operation_id,
            "descriptor_id": record.descriptor_id,
            "descriptor_version": record.descriptor_version,
            "request_digest": record.request_digest,
            "evidence_digest": record.evidence_digest,
            "approval": record.approval.model_dump(mode="json"),
        }
    )


def privileged_operation_provider_target_key(record: PrivilegedOperationRecord) -> str:
    return f"privileged-operation-target:{record.descriptor_id}:global"


def merge_train_policy_import_request_fingerprint(record: PrivilegedOperationRecord) -> str:
    if record.approval is None:
        raise ValueError("approval_provenance_missing")
    if not isinstance(record.request, ManagedMergeTrainPolicyImportProposalInput):
        raise ValueError("executor_result_error")
    if not isinstance(record.evidence, ManagedMergeTrainPolicyImportHumanEvidence):
        raise ValueError("executor_result_error")
    return _digest(
        {
            "operation_id": record.operation_id,
            "active_record_id": record.evidence.active_record_id,
            "active_policy_sha256": record.evidence.active_policy_sha256,
            "candidate_record_id": record.request.record.record_id,
            "candidate_policy_sha256": record.request.record.policy_sha256,
            "plan_digest": record.approval.plan_digest,
        }
    )


def require_managed_merge_train_policy_import_execution_identity(
    record: PrivilegedOperationRecord,
    *,
    expected_record: MergeTrainPolicyRecord,
    replacement_record: MergeTrainPolicyRecord,
) -> None:
    error = "managed_merge_train_policy_import_identity_mismatch"
    if (
        record.descriptor_id != "managed-merge-train-policy-import"
        or record.status != "executing"
        or record.approval is None
        or record.execution is not None
        or not isinstance(record.request, ManagedMergeTrainPolicyImportProposalInput)
        or not isinstance(record.evidence, ManagedMergeTrainPolicyImportHumanEvidence)
        or expected_record.status != "active"
        or replacement_record.status != "active"
    ):
        raise ValueError(error)
    evidence = record.evidence
    if (
        record.request.record != replacement_record
        or evidence.active_record_id != expected_record.record_id
        or normalize_merge_train_policy_timestamp(evidence.active_updated_at)
        != normalize_merge_train_policy_timestamp(expected_record.updated_at)
        or evidence.active_policy_sha256 != expected_record.policy_sha256
        or evidence.candidate_record_id != replacement_record.record_id
        or evidence.candidate_policy_sha256 != replacement_record.policy_sha256
    ):
        raise ValueError(error)


def _digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
