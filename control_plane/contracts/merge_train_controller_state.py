from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.ordinary_agent_session_lifecycle import OrdinaryAgentJobBinding
from control_plane.contracts.text_fields import require_text


MergeTrainControllerStateStatus = Literal["idle", "running", "reconcile_required"]
MergeTrainControllerReconciliationStatus = Literal["clean", "adopted", "required"]


class MergeTrainControllerLeaseHeldError(RuntimeError):
    pass


class MergeTrainControllerAdoptionRejectedError(RuntimeError):
    pass


class MergeTrainControllerLeaseLostError(RuntimeError):
    pass


class MergeTrainControllerReconciliationRequiredError(RuntimeError):
    pass


MergeTrainObservedCheckStatus = Literal["not_observed", "unknown", "pending", "pass", "fail"]


class MergeTrainControllerLastResult(BaseModel):
    """One pass's bounded evidence. It is never admission or landing authority."""

    model_config = ConfigDict(extra="forbid")

    repository: str = Field(max_length=256)
    base_branch: str = Field(max_length=256)
    policy_key: str = Field(max_length=256)
    policy_sha256: str = Field(max_length=256)
    pass_started_at: str = Field(max_length=64)
    observed_at: str = Field(max_length=64)
    trace_id: str = Field(max_length=256)
    action: str = Field(max_length=128)
    reason_code: str = Field(max_length=128)
    record_id: str = Field(default="", max_length=256)
    source_failed_record_id: str = Field(default="", max_length=256)
    candidate_batch_id: str = Field(default="", max_length=256)
    recovery_kind: str = Field(default="", max_length=128)
    candidate_sha: str = Field(default="", max_length=128)
    observed_candidate_sha: str = Field(default="", max_length=128)
    base_sha: str = Field(default="", max_length=128)
    candidate_checks_status: MergeTrainObservedCheckStatus = "not_observed"
    base_checks_status: MergeTrainObservedCheckStatus = "not_observed"
    check_retry_of: str = Field(default="", max_length=256)
    infrastructure_retry_of: str = Field(default="", max_length=256)
    batch_body_retry_of: str = Field(default="", max_length=256)
    reconciliation_required: bool = False
    semantic_progress_at: str = Field(default="", max_length=64)
    next_actor: Literal["controller", "provider", "repository_agent"] = "controller"
    next_observation: str = Field(max_length=256)


class MergeTrainControllerStateRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ordinary_job_binding: OrdinaryAgentJobBinding | None = None

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
    last_result: MergeTrainControllerLastResult | None = None
    semantic_progress_key: str = ""

    @model_validator(mode="after")
    def _validate_record(self) -> "MergeTrainControllerStateRecord":
        self.controller_key = require_text(
            self.controller_key, "merge train controller state requires controller_key"
        )
        stored_repository = require_text(
            self.repository, "merge train controller state requires repository"
        )
        self.repository = _normalize_repository(stored_repository)
        self.base_branch = require_text(
            self.base_branch, "merge train controller state requires base_branch"
        )
        self.policy_key = require_text(
            self.policy_key, "merge train controller state requires policy_key"
        )
        self.policy_sha256 = require_text(
            self.policy_sha256, "merge train controller state requires policy_sha256"
        )
        self.updated_at = require_text(
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
            historical_key = (
                f"merge-train-controller:{stored_repository.replace('/', ':')}:{self.base_branch}"
            )
            if self.controller_key != historical_key:
                raise ValueError("merge train controller state controller_key does not match scope")
            self.repository = stored_repository
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
    normalized_base_branch = require_text(
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


def require_canonical_merge_train_controller_state(
    record: MergeTrainControllerStateRecord,
) -> MergeTrainControllerStateRecord:
    validated = MergeTrainControllerStateRecord.model_validate(record.model_dump(mode="json"))
    if validated.controller_key != build_merge_train_controller_key(
        repository=validated.repository, base_branch=validated.base_branch
    ):
        raise ValueError("Historical merge controller identities are read-only.")
    return validated


def find_current_merge_train_controller_state(
    records: Iterable[MergeTrainControllerStateRecord],
) -> MergeTrainControllerStateRecord | None:
    return next(
        (
            record
            for record in records
            if record.controller_key
            == build_merge_train_controller_key(
                repository=record.repository,
                base_branch=record.base_branch,
            )
        ),
        None,
    )


def select_canonical_merge_train_controller_state(
    records: Iterable[MergeTrainControllerStateRecord], *, repository: str, base_branch: str
) -> MergeTrainControllerStateRecord | None:
    expected_key = build_merge_train_controller_key(repository=repository, base_branch=base_branch)
    canonical: MergeTrainControllerStateRecord | None = None
    for stored in records:
        record = MergeTrainControllerStateRecord.model_validate(stored.model_dump(mode="json"))
        if (
            build_merge_train_controller_key(
                repository=record.repository, base_branch=record.base_branch
            )
            != expected_key
        ):
            raise MergeTrainControllerReconciliationRequiredError(
                "A retained merge controller has inconsistent scope."
            )
        if record.controller_key == expected_key:
            if canonical is not None:
                raise MergeTrainControllerReconciliationRequiredError(
                    "More than one canonical merge controller is present."
                )
            canonical = record
            continue
        if (
            record.status != "idle"
            or record.ordinary_job_binding is not None
            or any(
                (
                    record.lease_owner,
                    record.lease_acquired_at,
                    record.lease_expires_at,
                    record.heartbeat_at,
                    record.active_action,
                    record.active_phase,
                    record.active_record_id,
                    record.step_payload,
                    record.reconciliation_detail,
                )
            )
            or record.active_pull_request_number is not None
            or record.reconciliation_status != "clean"
        ):
            raise MergeTrainControllerReconciliationRequiredError(
                "A historical merge controller still requires reconciliation."
            )
    return canonical


def build_merge_train_controller_resume_detail(
    record: MergeTrainControllerStateRecord,
) -> str:
    if record.reconciliation_detail.startswith("resuming:"):
        return record.reconciliation_detail
    detail = f"resuming:{record.active_action}:{record.active_phase}"
    if record.reconciliation_detail:
        return f"{detail}; previous:{record.reconciliation_detail}"
    return detail


def _normalize_repository(repository: str) -> str:
    # Lowercased like batch records, so every spelling of one repository shares a
    # controller key and lease (GitHub repository names are case-insensitive).
    normalized = require_text(
        repository, "merge train controller state requires repository"
    ).lower()
    if "/" not in normalized:
        raise ValueError("merge train controller state repository must be owner/name")
    return normalized
