"""Audited engineering updates cannot replace merge authority."""

import hashlib
import json
from typing import Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, PositiveInt

from control_plane.contracts.merge_train_policy import (
    MergeTrainFailurePolicy,
    MergeTrainMergeMethod,
    MergeTrainPolicy,
    MergeTrainPolicyCompareWriteResult,
    MergeTrainPolicyRecord,
    MergeTrainSchedulerRunnerMode,
    build_merge_train_policy_record_id,
    merge_train_repository_authority_sha256,
    merge_train_repository_execution_sha256,
    merge_train_scheduler_mutation_authorized,
)
from control_plane.storage.postgres import DbOnlyMutationRequest
from control_plane.workflows.ship import utc_now_timestamp

MERGE_TRAIN_OPERATIONAL_POLICY_ROUTE = "/v1/work-graph/merge-train/policy/operational"
MERGE_TRAIN_OPERATIONAL_POLICY_SCOPE = "merge-train-operational-policy"


class MergeTrainOperationalSchedulerPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: bool | None = None
    runner_mode: MergeTrainSchedulerRunnerMode | None = None
    mutate: bool | None = None


class MergeTrainOperationalPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    stack_child_disposition_label: str | None = Field(default=None, max_length=255)
    max_batch_size: PositiveInt | None = None
    failure_policy: MergeTrainFailurePolicy | None = None
    merge_method: MergeTrainMergeMethod | None = None
    config_authority_events_enabled: bool | None = None
    scheduler: MergeTrainOperationalSchedulerPatch | None = None


class MergeTrainOperationalUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    repository: str = Field(min_length=1)
    base_branch: str = Field(min_length=1)
    expected_record_id: str = Field(min_length=1)
    expected_policy_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_event_id: str = Field(min_length=1, max_length=240)
    reason: str = Field(min_length=1, max_length=240)
    patch: MergeTrainOperationalPatch


class MergeTrainOperationalReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal["ok"] = "ok"
    trace_id: str
    actor: str
    reason: str
    source_event_id: str
    changed: bool
    previous_record_id: str
    record_id: str
    policy_sha256: str
    authority_sha256: str
    execution_sha256: str
    changed_fields: tuple[str, ...]


class _OperationalPolicyStore(Protocol):
    def list_merge_train_policy_records(
        self, *, status: str = "", limit: int | None = None
    ) -> tuple[MergeTrainPolicyRecord, ...]: ...

    def read_merge_train_policy_record(self, record_id: str) -> MergeTrainPolicyRecord: ...

    def compare_and_write_merge_train_policy_record(
        self,
        *,
        expected_record: MergeTrainPolicyRecord,
        replacement_record: MergeTrainPolicyRecord,
        mutation: DbOnlyMutationRequest,
    ) -> MergeTrainPolicyCompareWriteResult: ...


def require_unchanged_merge_train_authority(
    before: MergeTrainPolicyRecord, after: MergeTrainPolicyRecord
) -> None:
    def projection(record: MergeTrainPolicyRecord) -> dict[str, str]:
        return {
            f"{target.repository.casefold()}:{target.base_branch}": merge_train_repository_authority_sha256(
                target
            )
            for target in record.policy.policies
        }

    if before.policy.schema_version != after.policy.schema_version or projection(
        before
    ) != projection(after):
        raise ValueError("merge_train_operational_update_changes_authority")
    after.policy.require_advisory_review()
    after.policy.require_supported_token_sources()


def apply_merge_train_operational_update(
    *, record_store: object, request: MergeTrainOperationalUpdate, actor: str, trace_id: str
) -> MergeTrainOperationalReceipt:
    store = cast(_OperationalPolicyStore, record_store)
    before = store.read_merge_train_policy_record(request.expected_record_id)
    if before.policy_sha256 != request.expected_policy_sha256:
        raise ValueError("merge_train_operational_baseline_mismatch")
    target = before.policy.find_repository_policy(
        repository=request.repository, base_branch=request.base_branch
    )
    payload = target.model_dump(mode="json")
    changes = request.patch.model_dump(exclude_unset=True)
    scheduler = changes.pop("scheduler", None)
    payload.update(changes)
    if scheduler is not None:
        scheduler_payload = target.scheduler.model_dump(mode="json")
        scheduler_payload.update(scheduler)
        # Carry standing lane authority across an operational pause.
        scheduler_payload["mutation_authorized"] = merge_train_scheduler_mutation_authorized(target)
        payload["scheduler"] = scheduler_payload
    replacement_target = type(target).model_validate(payload)
    policies = tuple(
        replacement_target if item.policy_key == target.policy_key else item
        for item in before.policy.policies
    )
    policy = MergeTrainPolicy(schema_version=before.policy.schema_version, policies=policies)
    timestamp = utc_now_timestamp()
    candidate = MergeTrainPolicyRecord(
        record_id=build_merge_train_policy_record_id(
            updated_at=timestamp, policy_sha256=policy.policy_sha256
        ),
        source=f"service:operational-policy:{actor}:{request.source_event_id}",
        updated_at=timestamp,
        policy=policy,
    )
    require_unchanged_merge_train_authority(before, candidate)
    changed = before.policy_sha256 != candidate.policy_sha256
    receipt = MergeTrainOperationalReceipt(
        trace_id=trace_id,
        actor=actor,
        reason=request.reason,
        source_event_id=request.source_event_id,
        changed=changed,
        previous_record_id=before.record_id,
        record_id=candidate.record_id if changed else before.record_id,
        policy_sha256=policy.policy_sha256,
        authority_sha256=merge_train_repository_authority_sha256(replacement_target),
        execution_sha256=merge_train_repository_execution_sha256(replacement_target),
        changed_fields=tuple(
            sorted(
                field
                for field in request.patch.model_fields_set
                if target.model_dump(mode="json").get(field)
                != replacement_target.model_dump(mode="json").get(field)
            )
        ),
    )
    fingerprint = hashlib.sha256(
        json.dumps(
            {"request": request.model_dump(mode="json", exclude_unset=True), "actor": actor},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    result = store.compare_and_write_merge_train_policy_record(
        expected_record=before.model_copy(update={"status": "active"}),
        replacement_record=candidate,
        mutation=DbOnlyMutationRequest(
            scope=MERGE_TRAIN_OPERATIONAL_POLICY_SCOPE,
            route_path=MERGE_TRAIN_OPERATIONAL_POLICY_ROUTE,
            idempotency_key=request.source_event_id,
            request_fingerprint=fingerprint,
            lease_owner=trace_id,
            response_status_code=200,
            response_trace_id=trace_id,
            response_payload=receipt.model_dump(mode="json"),
        ),
    )
    if result.status not in {"written", "unchanged", "replayed"}:
        raise ValueError(f"merge_train_operational_update_{result.status}")
    if result.idempotency_record is None:
        raise ValueError("merge_train_operational_audit_missing")
    return MergeTrainOperationalReceipt.model_validate(
        getattr(result.idempotency_record, "response_payload")
    )
