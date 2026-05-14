from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.merge_train_policy import MergeTrainMergeMethod
from control_plane.merge_train import MergeTrainDryRunResult


MergeTrainBatchCandidateStatus = Literal[
    "planned", "building", "ready_for_checks", "passed", "failed", "stale", "blocked"
]
MergeTrainBatchRecordStatus = Literal["active", "superseded"]
MergeTrainBatchLandingStatus = Literal[
    "planned", "merging", "merged", "blocked", "stale", "skipped"
]


class MergeTrainBatchEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pull_request_number: int = Field(gt=0)
    position: int = Field(gt=0)
    head_sha: str
    title: str = ""
    url: str = ""

    @model_validator(mode="after")
    def _validate_entry(self) -> "MergeTrainBatchEntry":
        self.head_sha = _normalize_required_value(
            self.head_sha, "merge train batch entry requires head_sha"
        )
        self.title = self.title.strip()
        self.url = self.url.strip()
        return self


class MergeTrainBatchCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: str
    repository: str
    base_branch: str
    base_sha: str
    policy_key: str
    policy_sha256: str
    candidate_ref: str
    candidate_sha: str = ""
    status: MergeTrainBatchCandidateStatus = "planned"
    entries: tuple[MergeTrainBatchEntry, ...]
    required_checks_status: Literal["unknown", "pending", "pass", "fail"] = "unknown"
    created_at: str
    updated_at: str

    @model_validator(mode="after")
    def _validate_candidate(self) -> "MergeTrainBatchCandidate":
        self.batch_id = _normalize_required_value(
            self.batch_id, "merge train batch candidate requires batch_id"
        )
        self.repository = _normalize_repository(self.repository)
        self.base_branch = _normalize_required_value(
            self.base_branch, "merge train batch candidate requires base_branch"
        )
        self.base_sha = _normalize_required_value(
            self.base_sha, "merge train batch candidate requires base_sha"
        )
        self.policy_key = _normalize_required_value(
            self.policy_key, "merge train batch candidate requires policy_key"
        )
        self.policy_sha256 = _normalize_required_value(
            self.policy_sha256,
            "merge train batch candidate requires policy_sha256",
        )
        self.candidate_ref = _normalize_required_value(
            self.candidate_ref, "merge train batch candidate requires candidate_ref"
        )
        self.candidate_sha = self.candidate_sha.strip()
        self.created_at = _normalize_required_value(
            self.created_at, "merge train batch candidate requires created_at"
        )
        self.updated_at = _normalize_required_value(
            self.updated_at, "merge train batch candidate requires updated_at"
        )
        self.entries = _normalize_entries(self.entries)
        return self


class MergeTrainBatchLandingEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pull_request_number: int = Field(gt=0)
    position: int = Field(gt=0)
    expected_head_sha: str
    expected_base_sha: str
    merge_method: MergeTrainMergeMethod
    status: MergeTrainBatchLandingStatus = "planned"
    merge_commit_sha: str = ""

    @model_validator(mode="after")
    def _validate_landing_entry(self) -> "MergeTrainBatchLandingEntry":
        self.expected_head_sha = _normalize_required_value(
            self.expected_head_sha,
            "merge train batch landing entry requires expected_head_sha",
        )
        self.expected_base_sha = _normalize_required_value(
            self.expected_base_sha,
            "merge train batch landing entry requires expected_base_sha",
        )
        self.merge_commit_sha = self.merge_commit_sha.strip()
        return self


class MergeTrainBatchLandingPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str
    batch_id: str
    repository: str
    base_branch: str
    candidate_ref: str
    candidate_sha: str
    policy_key: str
    policy_sha256: str
    entries: tuple[MergeTrainBatchLandingEntry, ...]
    created_at: str

    @model_validator(mode="after")
    def _validate_plan(self) -> "MergeTrainBatchLandingPlan":
        self.plan_id = _normalize_required_value(
            self.plan_id, "merge train batch landing plan requires plan_id"
        )
        self.batch_id = _normalize_required_value(
            self.batch_id, "merge train batch landing plan requires batch_id"
        )
        self.repository = _normalize_repository(self.repository)
        self.base_branch = _normalize_required_value(
            self.base_branch, "merge train batch landing plan requires base_branch"
        )
        self.candidate_ref = _normalize_required_value(
            self.candidate_ref, "merge train batch landing plan requires candidate_ref"
        )
        self.candidate_sha = _normalize_required_value(
            self.candidate_sha, "merge train batch landing plan requires candidate_sha"
        )
        self.policy_key = _normalize_required_value(
            self.policy_key, "merge train batch landing plan requires policy_key"
        )
        self.policy_sha256 = _normalize_required_value(
            self.policy_sha256,
            "merge train batch landing plan requires policy_sha256",
        )
        self.created_at = _normalize_required_value(
            self.created_at, "merge train batch landing plan requires created_at"
        )
        if not self.entries:
            raise ValueError("merge train batch landing plan requires entries")
        positions = [entry.position for entry in self.entries]
        if positions != list(range(1, len(positions) + 1)):
            raise ValueError("merge train batch landing plan positions must be contiguous")
        return self


class MergeTrainBatchCandidateRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    record_id: str
    status: MergeTrainBatchRecordStatus = "active"
    source: str
    updated_at: str
    candidate: MergeTrainBatchCandidate

    @model_validator(mode="after")
    def _validate_record(self) -> "MergeTrainBatchCandidateRecord":
        self.record_id = _normalize_required_value(
            self.record_id, "merge train batch candidate record requires record_id"
        )
        self.source = _normalize_required_value(
            self.source, "merge train batch candidate record requires source"
        )
        self.updated_at = _normalize_required_value(
            self.updated_at,
            "merge train batch candidate record requires updated_at",
        )
        return self


class MergeTrainBatchLandingPlanRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    record_id: str
    status: MergeTrainBatchRecordStatus = "active"
    source: str
    updated_at: str
    landing_plan: MergeTrainBatchLandingPlan

    @model_validator(mode="after")
    def _validate_record(self) -> "MergeTrainBatchLandingPlanRecord":
        self.record_id = _normalize_required_value(
            self.record_id, "merge train batch landing plan record requires record_id"
        )
        self.source = _normalize_required_value(
            self.source, "merge train batch landing plan record requires source"
        )
        self.updated_at = _normalize_required_value(
            self.updated_at,
            "merge train batch landing plan record requires updated_at",
        )
        return self


def build_merge_train_batch_candidate(
    *,
    dry_run_result: MergeTrainDryRunResult,
    base_sha: str,
    policy_sha256: str,
    created_at: str,
) -> MergeTrainBatchCandidate:
    if dry_run_result.intended_next_action not in ("merge", "idle"):
        raise ValueError("merge train batch candidate requires a queue without blocking actions")
    queue_by_number = {entry.number: entry for entry in dry_run_result.queue}
    entries: list[MergeTrainBatchEntry] = []
    for pull_request_number in dry_run_result.queue_order:
        queue_entry = queue_by_number[pull_request_number]
        if not queue_entry.eligible:
            raise ValueError("merge train batch candidate requires eligible queue entries")
        entries.append(
            MergeTrainBatchEntry(
                pull_request_number=queue_entry.number,
                position=len(entries) + 1,
                head_sha=queue_entry.head_sha,
                title=queue_entry.title,
                url=queue_entry.url,
            )
        )
    if not entries:
        raise ValueError("merge train batch candidate requires at least one eligible PR")
    normalized_base_sha = _normalize_required_value(
        base_sha, "merge train batch candidate builder requires base_sha"
    )
    batch_id = build_merge_train_batch_id(
        repository=dry_run_result.repository,
        base_branch=dry_run_result.base_branch,
        base_sha=normalized_base_sha,
        entry_head_shas=tuple(entry.head_sha for entry in entries),
    )
    return MergeTrainBatchCandidate(
        batch_id=batch_id,
        repository=dry_run_result.repository,
        base_branch=dry_run_result.base_branch,
        base_sha=normalized_base_sha,
        policy_key=dry_run_result.policy_key,
        policy_sha256=policy_sha256,
        candidate_ref=build_merge_train_batch_candidate_ref(
            repository=dry_run_result.repository,
            base_branch=dry_run_result.base_branch,
            batch_id=batch_id,
        ),
        status="planned",
        entries=tuple(entries),
        created_at=created_at,
        updated_at=created_at,
    )


def build_merge_train_batch_candidate_record(
    *,
    candidate: MergeTrainBatchCandidate,
    source: str,
    updated_at: str,
) -> MergeTrainBatchCandidateRecord:
    record_without_id = MergeTrainBatchCandidateRecord(
        record_id="pending",
        source=source,
        updated_at=updated_at,
        candidate=candidate,
    )
    return record_without_id.model_copy(
        update={"record_id": build_merge_train_batch_candidate_record_id(record_without_id)}
    )


def build_merge_train_batch_candidate_record_id(
    record: MergeTrainBatchCandidateRecord,
) -> str:
    digest_payload = record.model_dump(mode="json", exclude={"record_id"})
    digest = hashlib.sha256(
        json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    normalized_timestamp = record.updated_at.replace("-", "").replace(":", "")
    return f"merge-train-batch-candidate-{normalized_timestamp}-{digest}"


def build_merge_train_batch_id(
    *, repository: str, base_branch: str, base_sha: str, entry_head_shas: tuple[str, ...]
) -> str:
    normalized_repository = _normalize_repository(repository).replace("/", "-")
    normalized_base_branch = _normalize_required_value(
        base_branch, "merge train batch id requires base_branch"
    ).replace("/", "-")
    digest = hashlib.sha256(
        json.dumps(
            {
                "base_sha": _normalize_required_value(
                    base_sha, "merge train batch id requires base_sha"
                ),
                "entry_head_shas": [
                    _normalize_required_value(sha, "merge train batch id requires head sha")
                    for sha in entry_head_shas
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:16]
    return f"merge-train-batch-{normalized_repository}-{normalized_base_branch}-{digest}"


def build_merge_train_batch_candidate_ref(
    *, repository: str, base_branch: str, batch_id: str
) -> str:
    owner, name = _normalize_repository(repository).split("/", maxsplit=1)
    normalized_base_branch = _normalize_required_value(
        base_branch, "merge train batch candidate ref requires base_branch"
    ).replace("/", "-")
    normalized_batch_id = _normalize_required_value(
        batch_id, "merge train batch candidate ref requires batch_id"
    )
    return f"refs/heads/launchplane/train/{owner}/{name}/{normalized_base_branch}/{normalized_batch_id}"


def build_merge_train_batch_landing_plan(
    *,
    candidate: MergeTrainBatchCandidate,
    merge_method: MergeTrainMergeMethod,
    created_at: str,
) -> MergeTrainBatchLandingPlan:
    if candidate.status != "passed":
        raise ValueError("merge train batch landing plan requires passed candidate")
    if not candidate.candidate_sha:
        raise ValueError("merge train batch landing plan requires candidate_sha")
    entries = tuple(
        MergeTrainBatchLandingEntry(
            pull_request_number=entry.pull_request_number,
            position=entry.position,
            expected_head_sha=entry.head_sha,
            expected_base_sha=candidate.base_sha,
            merge_method=merge_method,
        )
        for entry in candidate.entries
    )
    plan_without_id = MergeTrainBatchLandingPlan(
        plan_id="pending",
        batch_id=candidate.batch_id,
        repository=candidate.repository,
        base_branch=candidate.base_branch,
        candidate_ref=candidate.candidate_ref,
        candidate_sha=candidate.candidate_sha,
        policy_key=candidate.policy_key,
        policy_sha256=candidate.policy_sha256,
        entries=entries,
        created_at=created_at,
    )
    return plan_without_id.model_copy(
        update={"plan_id": build_merge_train_batch_landing_plan_id(plan_without_id)}
    )


def build_merge_train_batch_landing_plan_id(plan: MergeTrainBatchLandingPlan) -> str:
    digest_payload = plan.model_dump(mode="json", exclude={"plan_id"})
    digest = hashlib.sha256(
        json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return f"merge-train-landing-plan-{plan.batch_id}-{digest}"


def _normalize_entries(
    entries: tuple[MergeTrainBatchEntry, ...],
) -> tuple[MergeTrainBatchEntry, ...]:
    if not entries:
        raise ValueError("merge train batch candidate requires entries")
    positions = [entry.position for entry in entries]
    if positions != list(range(1, len(positions) + 1)):
        raise ValueError("merge train batch candidate positions must be contiguous")
    seen_pr_numbers: set[int] = set()
    for entry in entries:
        if entry.pull_request_number in seen_pr_numbers:
            raise ValueError("merge train batch candidate PR numbers must be unique")
        seen_pr_numbers.add(entry.pull_request_number)
    return entries


def _normalize_repository(repository: str) -> str:
    normalized = _normalize_required_value(repository, "merge train batch requires repository")
    if "/" not in normalized:
        raise ValueError("merge train batch repository must be owner/name")
    return normalized


def _normalize_required_value(value: str, error_message: str) -> str:
    normalized_value = value.strip()
    if not normalized_value:
        raise ValueError(error_message)
    return normalized_value
