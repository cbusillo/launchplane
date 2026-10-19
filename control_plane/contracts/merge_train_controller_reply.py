"""The accepted reply of `POST /v1/work-graph/merge-train/controller/run-once`.

This module is the one source of the controller reply contract. Launchplane
publishes it as `contracts/merge-train-controller-reply.schema.json`, and the
test suite checks every controller reply it produces against it, so a new reply
field fails Launchplane's own tests until it is declared here. Clients such as
the codex-skills merge-train helper pin that published schema instead of
keeping their own copy of the field list.

Objects whose fields clients read one by one are closed: each field is
declared. Larger nested records (candidate, landing plan, dry run, details and
so on) are open objects; their own models own their shape.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict

from pydantic import ConfigDict, TypeAdapter, with_config

from control_plane.contracts.merge_train_controller_state import MergeTrainControllerLastResult

_CLOSED = ConfigDict(extra="forbid")

JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"

MergeTrainControllerReplyStatus = Literal["accepted"]


@with_config(_CLOSED)
class MergeTrainControllerReplyRecords(TypedDict, total=False):
    """Record ids the pass wrote or settled."""

    merge_train_batch_candidate_record_id: str
    merge_train_batch_landing_plan_record_id: str
    merge_train_stack_collapse_plan_record_id: str
    superseded_merge_train_batch_candidate_record_id: str
    retired_merge_train_batch_candidate_record_id: str


@with_config(_CLOSED)
class MergeTrainControllerBlockingReason(TypedDict):
    code: str
    message: str


@with_config(_CLOSED)
class MergeTrainControllerBlockResult(TypedDict):
    status: str
    repository: NotRequired[str]
    base_branch: NotRequired[str]
    pull_request_number: int
    blocked_label: NotRequired[str]
    train_should_continue: bool
    detail: NotRequired[str]


@with_config(_CLOSED)
class MergeTrainControllerHeldOut(TypedDict):
    pull_request_number: int
    head_sha: str
    reason: str
    conflicts_with: list[int]


@with_config(_CLOSED)
class MergeTrainControllerConflictProbe(TypedDict):
    status: str
    pull_request_numbers: list[int]
    held_out: NotRequired[list[MergeTrainControllerHeldOut]]


@with_config(_CLOSED)
class MergeTrainControllerMergeReadiness(TypedDict):
    state: str
    reason_codes: list[str]
    owner_states: list[str]
    technical_checks_state: str
    engineering_review_state: str
    policy_state: str
    candidate_state: str
    fence_state: str


@with_config(_CLOSED)
class MergeTrainControllerStructuralProvenance(TypedDict):
    status: str
    reason_codes: list[str]
    effective_base_sha: str
    effective_base_tree_sha: str
    candidate_sha256: str
    landing_plan_sha256: str
    provenance_sha256: str


@with_config(_CLOSED)
class MergeTrainControllerReplyResult(TypedDict, total=False):
    """What one controller pass did and what it saw."""

    repository: str
    base_branch: str
    mode: str
    controller_action: str
    reason_code: str
    active_action: str
    active_phase: str
    active_record_id: str
    controller_reconciliation_status: str
    reconciliation_detail: str
    landing_progress: str
    merge_train_batch_candidate_record_id: str
    merge_train_batch_landing_plan_record_id: str
    merge_train_stack_collapse_plan_record_id: str
    superseded_merge_train_batch_candidate_record_id: str
    retired_merge_train_batch_candidate_record_id: str
    candidate_ref_cleanup_status: str
    candidate_ref_cleanup_github_status_code: int
    candidate_ref_cleanup_message: str
    blocking_reason: MergeTrainControllerBlockingReason
    block_result: MergeTrainControllerBlockResult
    conflict_probe: MergeTrainControllerConflictProbe | None
    merge_readiness: MergeTrainControllerMergeReadiness | None
    structural_provenance: MergeTrainControllerStructuralProvenance | None
    last_controller_result: MergeTrainControllerLastResult
    candidate: dict[str, Any]
    landing_plan: dict[str, Any]
    stack_collapse_plan: dict[str, Any]
    stack_discovery: dict[str, Any]
    dry_run_result: dict[str, Any]
    branch_update_result: dict[str, Any]
    details: dict[str, Any]
    error: dict[str, Any]


@with_config(_CLOSED)
class MergeTrainControllerReply(TypedDict):
    """The accepted reply of one merge-train controller pass."""

    status: MergeTrainControllerReplyStatus
    trace_id: str
    records: MergeTrainControllerReplyRecords
    result: MergeTrainControllerReplyResult
    replayed: NotRequired[bool | None]
    original_trace_id: NotRequired[str | None]


_REPLY_ADAPTER = TypeAdapter(MergeTrainControllerReply)


def validate_merge_train_controller_reply(reply: object) -> None:
    """Raise `pydantic.ValidationError` unless the reply matches the published contract."""

    _REPLY_ADAPTER.validate_json(json.dumps(reply), strict=True)


def build_merge_train_controller_reply_schema() -> dict[str, Any]:
    return {
        "$schema": JSON_SCHEMA_DIALECT,
        **_REPLY_ADAPTER.json_schema(mode="serialization"),
    }


def write_merge_train_controller_reply_schema(output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(build_merge_train_controller_reply_schema(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output_path


if __name__ == "__main__":
    import sys

    write_merge_train_controller_reply_schema(Path(sys.argv[1]))
