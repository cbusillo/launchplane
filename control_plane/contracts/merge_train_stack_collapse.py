from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.merge_train import MergeTrainStackDiscoveryResult


MergeTrainStackCollapseStatus = Literal[
    "planned", "collapsing", "waiting_for_root_checks", "ready_for_train", "blocked", "stale"
]
MergeTrainStackCollapseRunStatus = Literal["planned", "mutated", "blocked", "stale"]
MergeTrainStackChildDispositionStatus = Literal["planned", "closed", "blocked", "stale"]
MergeTrainStackCollapseRecordStatus = Literal["active", "superseded"]
MergeTrainStackCollapseIntentSource = Literal["root_ready_to_merge"]


class MergeTrainStackCollapseEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pull_request_number: int = Field(gt=0)
    position: int = Field(gt=0)
    head_sha: str
    head_ref: str
    base_sha: str = ""
    base_ref: str

    @model_validator(mode="after")
    def _validate_entry(self) -> "MergeTrainStackCollapseEntry":
        self.head_sha = _normalize_required_value(
            self.head_sha, "merge train stack collapse entry requires head_sha"
        )
        self.head_ref = _normalize_required_value(
            self.head_ref, "merge train stack collapse entry requires head_ref"
        )
        self.base_sha = self.base_sha.strip()
        self.base_ref = _normalize_required_value(
            self.base_ref, "merge train stack collapse entry requires base_ref"
        )
        return self


class MergeTrainStackCollapseMutation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    child_pull_request_number: int = Field(gt=0)
    parent_pull_request_number: int = Field(gt=0)
    child_head_sha: str
    expected_parent_head_sha: str
    parent_head_ref: str
    status: MergeTrainStackCollapseRunStatus = "planned"
    merge_commit_sha: str = ""
    detail: str = ""

    @model_validator(mode="after")
    def _validate_mutation(self) -> "MergeTrainStackCollapseMutation":
        self.child_head_sha = _normalize_required_value(
            self.child_head_sha, "merge train stack collapse mutation requires child_head_sha"
        )
        self.expected_parent_head_sha = _normalize_required_value(
            self.expected_parent_head_sha,
            "merge train stack collapse mutation requires expected_parent_head_sha",
        )
        self.parent_head_ref = _normalize_required_value(
            self.parent_head_ref, "merge train stack collapse mutation requires parent_head_ref"
        )
        self.merge_commit_sha = self.merge_commit_sha.strip()
        self.detail = self.detail.strip()
        return self


class MergeTrainStackChildDisposition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pull_request_number: int = Field(gt=0)
    expected_head_sha: str
    status: MergeTrainStackChildDispositionStatus = "planned"
    comment_url: str = ""
    detail: str = ""

    @model_validator(mode="after")
    def _validate_disposition(self) -> "MergeTrainStackChildDisposition":
        self.expected_head_sha = _normalize_required_value(
            self.expected_head_sha,
            "merge train stack child disposition requires expected_head_sha",
        )
        self.comment_url = self.comment_url.strip()
        self.detail = self.detail.strip()
        return self


class MergeTrainStackCollapsePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    collapse_id: str
    repository: str
    base_branch: str
    root_pull_request_number: int = Field(gt=0)
    root_initial_head_sha: str
    root_head_ref: str
    policy_key: str
    policy_sha256: str
    intent_source: MergeTrainStackCollapseIntentSource = "root_ready_to_merge"
    status: MergeTrainStackCollapseStatus = "planned"
    entries: tuple[MergeTrainStackCollapseEntry, ...]
    mutations: tuple[MergeTrainStackCollapseMutation, ...]
    child_dispositions: tuple[MergeTrainStackChildDisposition, ...] = ()
    created_at: str
    updated_at: str

    @model_validator(mode="after")
    def _validate_plan(self) -> "MergeTrainStackCollapsePlan":
        self.collapse_id = _normalize_required_value(
            self.collapse_id, "merge train stack collapse plan requires collapse_id"
        )
        self.repository = _normalize_repository(self.repository)
        self.base_branch = _normalize_required_value(
            self.base_branch, "merge train stack collapse plan requires base_branch"
        )
        self.root_initial_head_sha = _normalize_required_value(
            self.root_initial_head_sha,
            "merge train stack collapse plan requires root_initial_head_sha",
        )
        self.root_head_ref = _normalize_required_value(
            self.root_head_ref, "merge train stack collapse plan requires root_head_ref"
        )
        self.policy_key = _normalize_required_value(
            self.policy_key, "merge train stack collapse plan requires policy_key"
        )
        self.policy_sha256 = _normalize_required_value(
            self.policy_sha256, "merge train stack collapse plan requires policy_sha256"
        )
        self.created_at = _normalize_required_value(
            self.created_at, "merge train stack collapse plan requires created_at"
        )
        self.updated_at = _normalize_required_value(
            self.updated_at, "merge train stack collapse plan requires updated_at"
        )
        self.entries = _normalize_entries(self.entries)
        self.mutations = _normalize_mutations(self.entries, self.mutations)
        self.child_dispositions = _normalize_child_dispositions(
            self.entries, self.child_dispositions
        )
        return self


class MergeTrainStackCollapsePlanRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    record_id: str
    status: MergeTrainStackCollapseRecordStatus = "active"
    source: str
    updated_at: str
    plan: MergeTrainStackCollapsePlan

    @model_validator(mode="after")
    def _validate_record(self) -> "MergeTrainStackCollapsePlanRecord":
        self.record_id = _normalize_required_value(
            self.record_id, "merge train stack collapse plan record requires record_id"
        )
        self.source = _normalize_required_value(
            self.source, "merge train stack collapse plan record requires source"
        )
        self.updated_at = _normalize_required_value(
            self.updated_at, "merge train stack collapse plan record requires updated_at"
        )
        return self


class MergeTrainStackCollapseBranchClient(Protocol):
    def merge_stack_child_into_parent(
        self,
        *,
        repository: str,
        child_head_sha: str,
        expected_parent_head_sha: str,
        parent_head_ref: str,
        collapse_id: str,
        child_pull_request_number: int,
        parent_pull_request_number: int,
    ) -> str: ...


class MergeTrainStackChildDispositionClient(Protocol):
    def comment_pull_request(
        self, *, repository: str, pull_request_number: int, body: str
    ) -> str: ...

    def add_pull_request_label(
        self, *, repository: str, pull_request_number: int, label: str
    ) -> None: ...

    def close_pull_request(
        self, *, repository: str, pull_request_number: int, expected_head_sha: str
    ) -> None: ...


def execute_merge_train_stack_collapse_plan(
    *,
    plan: MergeTrainStackCollapsePlan,
    branch_client: MergeTrainStackCollapseBranchClient,
    updated_at: str,
) -> MergeTrainStackCollapsePlan:
    if plan.status not in {"planned", "collapsing"}:
        raise ValueError("merge train stack collapse plan is not executable")
    updated_mutations: list[MergeTrainStackCollapseMutation] = []
    current_status: MergeTrainStackCollapseStatus = "waiting_for_root_checks"
    for mutation in plan.mutations:
        try:
            merge_commit_sha = branch_client.merge_stack_child_into_parent(
                repository=plan.repository,
                child_head_sha=mutation.child_head_sha,
                expected_parent_head_sha=mutation.expected_parent_head_sha,
                parent_head_ref=mutation.parent_head_ref,
                collapse_id=plan.collapse_id,
                child_pull_request_number=mutation.child_pull_request_number,
                parent_pull_request_number=mutation.parent_pull_request_number,
            )
        except Exception as error:  # noqa: BLE001
            updated_mutations.append(
                mutation.model_copy(
                    update={
                        "status": "blocked",
                        "detail": str(error),
                    }
                )
            )
            current_status = "blocked"
            break
        updated_mutations.append(
            mutation.model_copy(
                update={
                    "status": "mutated",
                    "merge_commit_sha": merge_commit_sha,
                    "detail": "child head merged into parent feature branch",
                }
            )
        )
    if len(updated_mutations) < len(plan.mutations):
        updated_mutations.extend(plan.mutations[len(updated_mutations) :])
    return plan.model_copy(
        update={
            "status": current_status,
            "mutations": tuple(updated_mutations),
            "updated_at": updated_at,
        }
    )


def reconcile_merge_train_stack_children_after_root_landing(
    *,
    plan: MergeTrainStackCollapsePlan,
    disposition_client: MergeTrainStackChildDispositionClient,
    root_merge_commit_sha: str,
    label: str,
    updated_at: str,
) -> MergeTrainStackCollapsePlan:
    if plan.status != "waiting_for_root_checks":
        raise ValueError("merge train stack collapse plan is not ready for child disposition")
    normalized_root_merge_commit_sha = _normalize_required_value(
        root_merge_commit_sha,
        "merge train stack child disposition requires root_merge_commit_sha",
    )
    normalized_label = _normalize_required_value(
        label, "merge train stack child disposition requires label"
    )
    updated_dispositions: list[MergeTrainStackChildDisposition] = []
    current_status: MergeTrainStackCollapseStatus = "ready_for_train"
    for disposition in plan.child_dispositions:
        try:
            comment_url = disposition_client.comment_pull_request(
                repository=plan.repository,
                pull_request_number=disposition.pull_request_number,
                body=_child_disposition_comment_body(
                    root_pull_request_number=plan.root_pull_request_number,
                    root_merge_commit_sha=normalized_root_merge_commit_sha,
                ),
            )
            disposition_client.add_pull_request_label(
                repository=plan.repository,
                pull_request_number=disposition.pull_request_number,
                label=normalized_label,
            )
            disposition_client.close_pull_request(
                repository=plan.repository,
                pull_request_number=disposition.pull_request_number,
                expected_head_sha=disposition.expected_head_sha,
            )
        except Exception as error:  # noqa: BLE001
            updated_dispositions.append(
                disposition.model_copy(update={"status": "blocked", "detail": str(error)})
            )
            current_status = "blocked"
            break
        updated_dispositions.append(
            disposition.model_copy(
                update={
                    "status": "closed",
                    "comment_url": comment_url,
                    "detail": "child PR closed after root landed",
                }
            )
        )
    if len(updated_dispositions) < len(plan.child_dispositions):
        updated_dispositions.extend(plan.child_dispositions[len(updated_dispositions) :])
    return plan.model_copy(
        update={
            "status": current_status,
            "child_dispositions": tuple(updated_dispositions),
            "updated_at": updated_at,
        }
    )


def build_merge_train_stack_collapse_plan(
    *,
    discovery_result: MergeTrainStackDiscoveryResult,
    policy_key: str,
    policy_sha256: str,
    created_at: str,
) -> MergeTrainStackCollapsePlan:
    if discovery_result.status != "ready_for_collapse":
        raise ValueError("merge train stack collapse plan requires a discovered stack")
    entries = tuple(
        MergeTrainStackCollapseEntry(
            pull_request_number=entry.number,
            position=position,
            head_sha=entry.head_sha,
            head_ref=entry.head_ref,
            base_sha=entry.base_sha,
            base_ref=entry.base_ref,
        )
        for position, entry in enumerate(discovery_result.entries, start=1)
    )
    if not entries:
        raise ValueError("merge train stack collapse plan requires entries")
    mutations = tuple(
        MergeTrainStackCollapseMutation(
            child_pull_request_number=child.pull_request_number,
            parent_pull_request_number=parent.pull_request_number,
            child_head_sha=child.head_sha,
            expected_parent_head_sha=parent.head_sha,
            parent_head_ref=parent.head_ref,
        )
        for parent, child in zip(entries, entries[1:])
    )
    child_dispositions = tuple(
        MergeTrainStackChildDisposition(
            pull_request_number=entry.pull_request_number,
            expected_head_sha=entry.head_sha,
        )
        for entry in entries[1:]
    )
    collapse_id = build_merge_train_stack_collapse_id(
        repository=discovery_result.repository,
        base_branch=discovery_result.base_branch,
        root_pull_request_number=discovery_result.root_pull_request_number,
        entry_head_shas=tuple(entry.head_sha for entry in entries),
    )
    return MergeTrainStackCollapsePlan(
        collapse_id=collapse_id,
        repository=discovery_result.repository,
        base_branch=discovery_result.base_branch,
        root_pull_request_number=discovery_result.root_pull_request_number,
        root_initial_head_sha=entries[0].head_sha,
        root_head_ref=entries[0].head_ref,
        policy_key=policy_key,
        policy_sha256=policy_sha256,
        entries=entries,
        mutations=mutations,
        child_dispositions=child_dispositions,
        created_at=created_at,
        updated_at=created_at,
    )


def build_merge_train_stack_collapse_plan_record(
    *,
    plan: MergeTrainStackCollapsePlan,
    source: str,
    updated_at: str,
) -> MergeTrainStackCollapsePlanRecord:
    record_without_id = MergeTrainStackCollapsePlanRecord(
        record_id="pending",
        source=source,
        updated_at=updated_at,
        plan=plan,
    )
    return record_without_id.model_copy(
        update={"record_id": build_merge_train_stack_collapse_plan_record_id(record_without_id)}
    )


def build_merge_train_stack_collapse_plan_record_id(
    record: MergeTrainStackCollapsePlanRecord,
) -> str:
    canonical_updated_at = _canonical_utc_timestamp(record.updated_at)
    digest_payload = record.model_dump(mode="json", exclude={"record_id"})
    digest_payload["updated_at"] = canonical_updated_at
    digest = hashlib.sha256(
        json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    normalized_timestamp = canonical_updated_at.replace("-", "").replace(":", "")
    return f"merge-train-stack-collapse-plan-{normalized_timestamp}-{digest}"


def build_merge_train_stack_collapse_id(
    *,
    repository: str,
    base_branch: str,
    root_pull_request_number: int,
    entry_head_shas: tuple[str, ...],
) -> str:
    normalized_repository = _normalize_repository(repository).replace("/", "-")
    normalized_base_branch = _normalize_required_value(
        base_branch, "merge train stack collapse id requires base_branch"
    ).replace("/", "-")
    digest = hashlib.sha256(
        json.dumps(
            {
                "entry_head_shas": [
                    _normalize_required_value(sha, "merge train stack collapse id requires head sha")
                    for sha in entry_head_shas
                ],
                "root_pull_request_number": root_pull_request_number,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:16]
    return (
        f"merge-train-stack-collapse-{normalized_repository}-"
        f"{normalized_base_branch}-{digest}"
    )


def _normalize_entries(
    entries: tuple[MergeTrainStackCollapseEntry, ...],
) -> tuple[MergeTrainStackCollapseEntry, ...]:
    if len(entries) < 2:
        raise ValueError("merge train stack collapse plan requires at least two entries")
    positions = [entry.position for entry in entries]
    if positions != list(range(1, len(positions) + 1)):
        raise ValueError("merge train stack collapse entry positions must be contiguous")
    seen_numbers: set[int] = set()
    for entry in entries:
        if entry.pull_request_number in seen_numbers:
            raise ValueError("merge train stack collapse PR numbers must be unique")
        seen_numbers.add(entry.pull_request_number)
    return entries


def _normalize_mutations(
    entries: tuple[MergeTrainStackCollapseEntry, ...],
    mutations: tuple[MergeTrainStackCollapseMutation, ...],
) -> tuple[MergeTrainStackCollapseMutation, ...]:
    if len(mutations) != len(entries) - 1:
        raise ValueError("merge train stack collapse mutations must connect each stack layer")
    expected_pairs = tuple(
        (child.pull_request_number, parent.pull_request_number)
        for parent, child in zip(entries, entries[1:])
    )
    actual_pairs = tuple(
        (mutation.child_pull_request_number, mutation.parent_pull_request_number)
        for mutation in mutations
    )
    if actual_pairs != expected_pairs:
        raise ValueError("merge train stack collapse mutations must be leaf-to-root ordered")
    return mutations


def _normalize_child_dispositions(
    entries: tuple[MergeTrainStackCollapseEntry, ...],
    child_dispositions: tuple[MergeTrainStackChildDisposition, ...],
) -> tuple[MergeTrainStackChildDisposition, ...]:
    if not child_dispositions:
        child_dispositions = tuple(
            MergeTrainStackChildDisposition(
                pull_request_number=entry.pull_request_number,
                expected_head_sha=entry.head_sha,
            )
            for entry in entries[1:]
        )
    expected_numbers = tuple(entry.pull_request_number for entry in entries[1:])
    actual_numbers = tuple(
        disposition.pull_request_number for disposition in child_dispositions
    )
    if actual_numbers != expected_numbers:
        raise ValueError(
            "merge train stack child dispositions must match stack child order"
        )
    return child_dispositions


def _child_disposition_comment_body(
    *, root_pull_request_number: int, root_merge_commit_sha: str
) -> str:
    return (
        "Launchplane landed this stacked PR through root PR "
        f"#{root_pull_request_number}.\n\n"
        "The root PR was merged by the merge train at "
        f"`{root_merge_commit_sha}`. Closing this child PR keeps the stack state "
        "aligned with the landed root."
    )


def _normalize_repository(repository: str) -> str:
    normalized = _normalize_required_value(repository, "merge train stack collapse requires repository")
    if "/" not in normalized:
        raise ValueError("merge train stack collapse repository must be owner/name")
    return normalized


def _normalize_required_value(value: str, error_message: str) -> str:
    normalized_value = value.strip()
    if not normalized_value:
        raise ValueError(error_message)
    return normalized_value


def _canonical_utc_timestamp(timestamp: str) -> str:
    normalized_timestamp = _normalize_required_value(
        timestamp, "merge train stack collapse timestamp is required"
    )
    parseable_timestamp = normalized_timestamp
    if parseable_timestamp.endswith("Z"):
        parseable_timestamp = f"{parseable_timestamp[:-1]}+00:00"
    try:
        parsed_timestamp = datetime.fromisoformat(parseable_timestamp)
    except ValueError:
        return normalized_timestamp
    if parsed_timestamp.tzinfo is None:
        return normalized_timestamp
    return parsed_timestamp.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
