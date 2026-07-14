from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


MergeTrainControllerStateStatus = Literal["idle", "running", "reconcile_required"]
MergeTrainControllerReconciliationStatus = Literal["clean", "adopted", "required"]


class MergeTrainControllerLeaseHeldError(RuntimeError):
    pass


class MergeTrainControllerLeaseLostError(RuntimeError):
    pass


class MergeTrainControllerReconciliationRequiredError(RuntimeError):
    pass


class MergeTrainControllerStateRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    controller_key: str
    repository: str
    base_branch: str
    policy_key: str
    policy_sha256: str
    status: MergeTrainControllerStateStatus = "idle"
    updated_at: str
    lease_owner: str = ""
    lease_acquired_at: str = ""
    lease_expires_at: str = ""
    heartbeat_at: str = ""
    active_action: str = ""
    active_phase: str = ""
    active_record_id: str = ""
    active_pull_request_number: int | None = None
    step_payload: dict[str, object] = Field(default_factory=dict)
    last_owner: str = ""
    last_action: str = ""
    last_phase: str = ""
    last_record_id: str = ""
    last_pull_request_number: int | None = None
    last_transition_at: str = ""
    reconciliation_status: MergeTrainControllerReconciliationStatus = "clean"
    reconciliation_detail: str = ""

    @model_validator(mode="after")
    def _validate_record(self) -> "MergeTrainControllerStateRecord":
        self.controller_key = _normalize_required(
            self.controller_key, "merge train controller state requires controller_key"
        )
        self.repository = _normalize_repository(self.repository)
        self.base_branch = _normalize_required(
            self.base_branch, "merge train controller state requires base_branch"
        )
        self.policy_key = _normalize_required(
            self.policy_key, "merge train controller state requires policy_key"
        )
        self.policy_sha256 = _normalize_required(
            self.policy_sha256, "merge train controller state requires policy_sha256"
        )
        self.updated_at = _normalize_required(
            self.updated_at, "merge train controller state requires updated_at"
        )
        self.lease_owner = self.lease_owner.strip()
        self.lease_acquired_at = self.lease_acquired_at.strip()
        self.lease_expires_at = self.lease_expires_at.strip()
        self.heartbeat_at = self.heartbeat_at.strip()
        self.active_action = self.active_action.strip()
        self.active_phase = self.active_phase.strip()
        self.active_record_id = self.active_record_id.strip()
        self.step_payload = dict(self.step_payload)
        self.last_owner = self.last_owner.strip()
        self.last_action = self.last_action.strip()
        self.last_phase = self.last_phase.strip()
        self.last_record_id = self.last_record_id.strip()
        self.last_transition_at = self.last_transition_at.strip()
        self.reconciliation_detail = self.reconciliation_detail.strip()
        expected_key = build_merge_train_controller_key(
            repository=self.repository,
            base_branch=self.base_branch,
        )
        if self.controller_key != expected_key:
            raise ValueError("merge train controller state controller_key does not match scope")
        if self.status == "running":
            if not self.lease_owner:
                raise ValueError("running merge train controller state requires lease_owner")
            if not self.lease_acquired_at:
                raise ValueError("running merge train controller state requires lease_acquired_at")
            if not self.lease_expires_at:
                raise ValueError("running merge train controller state requires lease_expires_at")
            if not self.heartbeat_at:
                raise ValueError("running merge train controller state requires heartbeat_at")
            if bool(self.active_action) != bool(self.active_phase):
                raise ValueError(
                    "running merge train controller state requires action and phase together"
                )
        elif (
            self.lease_owner or self.lease_acquired_at or self.lease_expires_at or self.heartbeat_at
        ):
            raise ValueError("inactive merge train controller state cannot retain a lease")
        if self.status == "reconcile_required":
            if not self.active_action or not self.active_phase:
                raise ValueError(
                    "reconcile-required merge train controller state requires active phase"
                )
            if self.reconciliation_status != "required":
                raise ValueError(
                    "reconcile-required merge train controller state requires reconciliation status"
                )
        return self


def build_merge_train_controller_key(*, repository: str, base_branch: str) -> str:
    normalized_repository = _normalize_repository(repository).replace("/", ":")
    normalized_base_branch = _normalize_required(
        base_branch, "merge train controller key requires base_branch"
    )
    return f"merge-train-controller:{normalized_repository}:{normalized_base_branch}"


def build_merge_train_controller_state_record(
    *,
    repository: str,
    base_branch: str,
    policy_key: str,
    policy_sha256: str,
    updated_at: str,
) -> MergeTrainControllerStateRecord:
    return MergeTrainControllerStateRecord(
        controller_key=build_merge_train_controller_key(
            repository=repository,
            base_branch=base_branch,
        ),
        repository=repository,
        base_branch=base_branch,
        policy_key=policy_key,
        policy_sha256=policy_sha256,
        updated_at=updated_at,
    )


def build_merge_train_controller_resume_detail(
    record: MergeTrainControllerStateRecord,
) -> str:
    if record.reconciliation_detail.startswith("resuming:"):
        return record.reconciliation_detail
    detail = f"resuming:{record.active_action}:{record.active_phase}"
    if record.reconciliation_detail:
        return f"{detail}; previous:{record.reconciliation_detail}"
    return detail


def _normalize_required(value: str, message: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(message)
    return normalized


def _normalize_repository(repository: str) -> str:
    normalized = _normalize_required(repository, "merge train controller state requires repository")
    if "/" not in normalized:
        raise ValueError("merge train controller state repository must be owner/name")
    return normalized
