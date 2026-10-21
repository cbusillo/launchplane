from __future__ import annotations

import hashlib
import json
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.text_fields import require_text
from control_plane.merge_train import MergeTrainDryRunResult
from control_plane.merge_train import MergeTrainDryRunSnapshot
from control_plane.merge_train import (
    MergeTrainDryRunAction,
    MergeTrainMergeableState,
    MergeTrainCheckStatus,
)
from control_plane.workflows.merge_train_worker import (
    MergeTrainWorkerStepResult,
    MergeTrainWorkerStepStatus,
)


MergeTrainRunMode = Literal["dry_run", "mutate"]


class MergeTrainRunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    run_id: str
    recorded_at: str
    trace_id: str
    repository: str
    base_branch: str
    mode: MergeTrainRunMode
    status: str = Field(
        json_schema_extra={
            "x-launchplane-snapshot-protocol": list(
                dict.fromkeys(
                    get_args(MergeTrainWorkerStepStatus) + get_args(MergeTrainDryRunAction)
                )
            )
        }
    )
    intended_next_action: str = Field(
        json_schema_extra={
            "x-launchplane-snapshot-protocol": list(get_args(MergeTrainDryRunAction))
        }
    )
    selected_pr_number: int | None = None
    selected_head_sha: str = ""
    selected_mergeable: str = Field(
        default="",
        json_schema_extra={
            "x-launchplane-snapshot-protocol": list(get_args(MergeTrainMergeableState))
        },
    )
    selected_required_checks_status: str = Field(
        default="",
        json_schema_extra={
            "x-launchplane-snapshot-protocol": list(get_args(MergeTrainCheckStatus))
        },
    )
    reread_required: bool = False
    poll_required: bool = False
    policy_key: str
    policy_sha256: str
    dry_run_result: dict[str, object]
    worker_step_result: dict[str, object] | None = None
    snapshot: dict[str, object]

    @model_validator(mode="after")
    def _validate_record(self) -> "MergeTrainRunRecord":
        self.run_id = require_text(self.run_id, "merge train run record requires run_id")
        self.recorded_at = require_text(
            self.recorded_at, "merge train run record requires recorded_at"
        )
        self.trace_id = require_text(self.trace_id, "merge train run record requires trace_id")
        self.repository = require_text(
            self.repository, "merge train run record requires repository"
        )
        self.base_branch = require_text(
            self.base_branch, "merge train run record requires base_branch"
        )
        self.status = require_text(self.status, "merge train run record requires status")
        self.intended_next_action = require_text(
            self.intended_next_action,
            "merge train run record requires intended_next_action",
        )
        self.selected_head_sha = self.selected_head_sha.strip()
        self.selected_mergeable = self.selected_mergeable.strip()
        self.selected_required_checks_status = self.selected_required_checks_status.strip()
        self.policy_key = require_text(
            self.policy_key, "merge train run record requires policy_key"
        )
        self.policy_sha256 = require_text(
            self.policy_sha256, "merge train run record requires policy_sha256"
        )
        return self


def build_merge_train_run_record(
    *,
    recorded_at: str,
    trace_id: str,
    policy_sha256: str,
    snapshot: MergeTrainDryRunSnapshot,
    dry_run_result: MergeTrainDryRunResult,
    worker_step_result: MergeTrainWorkerStepResult | None = None,
) -> MergeTrainRunRecord:
    selected_pr = dry_run_result.selected_pr
    mode: MergeTrainRunMode = "mutate" if worker_step_result is not None else "dry_run"
    status = (
        worker_step_result.status
        if worker_step_result is not None
        else dry_run_result.intended_next_action
    )
    record_without_id = MergeTrainRunRecord(
        run_id="pending",
        recorded_at=recorded_at,
        trace_id=trace_id,
        repository=dry_run_result.repository,
        base_branch=dry_run_result.base_branch,
        mode=mode,
        status=status,
        intended_next_action=dry_run_result.intended_next_action,
        selected_pr_number=selected_pr.number if selected_pr is not None else None,
        selected_head_sha=selected_pr.head_sha if selected_pr is not None else "",
        selected_mergeable=selected_pr.mergeable if selected_pr is not None else "",
        selected_required_checks_status=(
            selected_pr.required_checks_status if selected_pr is not None else ""
        ),
        reread_required=worker_step_result.reread_required if worker_step_result else False,
        poll_required=worker_step_result.poll_required if worker_step_result else False,
        policy_key=dry_run_result.policy_key,
        policy_sha256=policy_sha256,
        dry_run_result=dry_run_result.model_dump(mode="json"),
        worker_step_result=(
            worker_step_result.model_dump(mode="json") if worker_step_result else None
        ),
        snapshot=snapshot.model_dump(mode="json"),
    )
    return record_without_id.model_copy(
        update={"run_id": build_merge_train_run_record_id(record_without_id)}
    )


def build_merge_train_run_record_id(record: MergeTrainRunRecord) -> str:
    normalized_timestamp = (
        record.recorded_at.replace("-", "").replace(":", "").replace("+00:00", "Z")
    )
    digest_payload = record.model_dump(mode="json", exclude={"run_id"})
    digest = hashlib.sha256(
        json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return f"merge-train-run-{normalized_timestamp}-{digest}"
