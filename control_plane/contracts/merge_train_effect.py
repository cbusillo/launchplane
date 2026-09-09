from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from control_plane.contracts.merge_train_policy import MergeTrainMergeMethod


@dataclass(frozen=True)
class MergeTrainEffectLineage:
    repository: str
    base_branch: str
    batch_id: str = ""
    landing_plan_id: str = ""
    collapse_id: str = ""


@dataclass(frozen=True)
class CandidateRefPrepareEffect:
    lineage: MergeTrainEffectLineage
    candidate_ref: str
    base_sha: str


@dataclass(frozen=True)
class CandidateHeadMergeEffect:
    lineage: MergeTrainEffectLineage
    candidate_ref: str
    rolling_parent_sha: str
    pull_request_number: int
    head_sha: str


@dataclass(frozen=True)
class CandidateHeadMergeOutcome:
    result_sha: str | None
    result_tree_sha: str | None = None
    parent_shas: tuple[str, ...] = ()


@dataclass(frozen=True)
class PullRequestHeadRefreshEffect:
    lineage: MergeTrainEffectLineage
    pull_request_number: int
    expected_head_sha: str
    expected_base_sha: str = ""


@dataclass(frozen=True)
class StackChildMergeEffect:
    lineage: MergeTrainEffectLineage
    child_head_sha: str
    expected_parent_head_sha: str
    parent_head_ref: str
    protected_base_ref: str
    child_pull_request_number: int
    parent_pull_request_number: int


@dataclass(frozen=True)
class PullRequestLandingEffect:
    lineage: MergeTrainEffectLineage
    pull_request_number: int
    head_sha: str
    rolling_base_sha: str
    admission_id: str
    merge_method: MergeTrainMergeMethod


@dataclass(frozen=True)
class StackChildCommentEffect:
    lineage: MergeTrainEffectLineage
    pull_request_number: int
    body: str


@dataclass(frozen=True)
class StackChildLabelEffect:
    lineage: MergeTrainEffectLineage
    pull_request_number: int
    label: str


@dataclass(frozen=True)
class StackChildCloseEffect:
    lineage: MergeTrainEffectLineage
    pull_request_number: int
    expected_head_sha: str


@dataclass(frozen=True)
class CandidateRefDeleteEffect:
    lineage: MergeTrainEffectLineage
    candidate_ref: str
    expected_ref_sha: str = ""


class MergeTrainSemanticEffectExecutor(Protocol):
    def prepare_candidate_ref(self, effect: CandidateRefPrepareEffect) -> None: ...

    def merge_candidate_head(
        self, effect: CandidateHeadMergeEffect
    ) -> CandidateHeadMergeOutcome: ...

    def refresh_pull_request_head(self, effect: PullRequestHeadRefreshEffect) -> None: ...

    def merge_stack_child(self, effect: StackChildMergeEffect) -> str: ...

    def land_pull_request(self, effect: PullRequestLandingEffect) -> str: ...

    def comment_stack_child(self, effect: StackChildCommentEffect) -> str: ...

    def label_stack_child(self, effect: StackChildLabelEffect) -> None: ...

    def close_stack_child(self, effect: StackChildCloseEffect) -> None: ...

    def delete_candidate_ref(self, effect: CandidateRefDeleteEffect) -> bool: ...
