from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict

from control_plane.contracts.merge_train_policy import MergeTrainPolicy
from control_plane.merge_train import MergeTrainBranchClient
from control_plane.merge_train import MergeTrainBranchUpdateResult
from control_plane.merge_train import MergeTrainBlockResult
from control_plane.merge_train import MergeTrainDryRunAction
from control_plane.merge_train import MergeTrainDryRunResult
from control_plane.merge_train import MergeTrainDryRunSnapshot
from control_plane.merge_train import MergeTrainLabelClient
from control_plane.merge_train import MergeTrainMergeClient
from control_plane.merge_train import MergeTrainMergeResult
from control_plane.merge_train import MergeTrainWaitResult
from control_plane.merge_train import apply_merge_train_branch_update_intent
from control_plane.merge_train import apply_merge_train_block_intent
from control_plane.merge_train import apply_merge_train_merge_intent
from control_plane.merge_train import build_merge_train_dry_run_result
from control_plane.merge_train import build_merge_train_wait_result
from control_plane.merge_train_github import MergeTrainGitHubStaleHeadError


MergeTrainWorkerStepStatus = Literal[
    "blocked", "branch_updated", "waiting", "merged", "idle", "stale_head"
]


@dataclass(frozen=True)
class MergeTrainWorkerClients:
    label_client: MergeTrainLabelClient
    branch_client: MergeTrainBranchClient
    merge_client: MergeTrainMergeClient


class MergeTrainWorkerStepResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str
    base_branch: str
    intended_next_action: MergeTrainDryRunAction
    selected_pr_number: int | None = None
    status: MergeTrainWorkerStepStatus
    reread_required: bool
    poll_required: bool
    detail: str
    dry_run_result: MergeTrainDryRunResult
    block_result: MergeTrainBlockResult | None = None
    branch_update_result: MergeTrainBranchUpdateResult | None = None
    wait_result: MergeTrainWaitResult | None = None
    merge_result: MergeTrainMergeResult | None = None


def run_merge_train_worker_step(
    *,
    policy: MergeTrainPolicy,
    snapshot: MergeTrainDryRunSnapshot,
    clients: MergeTrainWorkerClients,
) -> MergeTrainWorkerStepResult:
    dry_run_result = build_merge_train_dry_run_result(policy=policy, snapshot=snapshot)
    selected_pr_number = (
        dry_run_result.selected_pr.number if dry_run_result.selected_pr is not None else None
    )

    if dry_run_result.intended_next_action == "block":
        block_result = apply_merge_train_block_intent(
            dry_run_result=dry_run_result, label_client=clients.label_client
        )
        return _build_worker_step_result(
            dry_run_result=dry_run_result,
            selected_pr_number=selected_pr_number,
            status="blocked",
            reread_required=False,
            poll_required=False,
            detail=block_result.detail,
            block_result=block_result,
        )

    if dry_run_result.intended_next_action == "update_branch":
        branch_update_result = apply_merge_train_branch_update_intent(
            dry_run_result=dry_run_result, branch_client=clients.branch_client
        )
        return _build_worker_step_result(
            dry_run_result=dry_run_result,
            selected_pr_number=selected_pr_number,
            status="branch_updated",
            reread_required=branch_update_result.reread_required,
            poll_required=False,
            detail=branch_update_result.detail,
            branch_update_result=branch_update_result,
        )

    if dry_run_result.intended_next_action == "wait_for_checks":
        wait_result = build_merge_train_wait_result(dry_run_result=dry_run_result)
        return _build_worker_step_result(
            dry_run_result=dry_run_result,
            selected_pr_number=selected_pr_number,
            status="waiting",
            reread_required=False,
            poll_required=wait_result.poll_required,
            detail=wait_result.detail,
            wait_result=wait_result,
        )

    if dry_run_result.intended_next_action == "merge":
        try:
            merge_result = apply_merge_train_merge_intent(
                dry_run_result=dry_run_result, merge_client=clients.merge_client
            )
        except MergeTrainGitHubStaleHeadError as exc:
            return _build_worker_step_result(
                dry_run_result=dry_run_result,
                selected_pr_number=selected_pr_number,
                status="stale_head",
                reread_required=True,
                poll_required=False,
                detail=str(exc),
            )
        return _build_worker_step_result(
            dry_run_result=dry_run_result,
            selected_pr_number=selected_pr_number,
            status="merged",
            reread_required=merge_result.reread_required,
            poll_required=False,
            detail=merge_result.detail,
            merge_result=merge_result,
        )

    return _build_worker_step_result(
        dry_run_result=dry_run_result,
        selected_pr_number=selected_pr_number,
        status="idle",
        reread_required=False,
        poll_required=False,
        detail=dry_run_result.next_action_detail,
    )


def _build_worker_step_result(
    *,
    dry_run_result: MergeTrainDryRunResult,
    selected_pr_number: int | None,
    status: MergeTrainWorkerStepStatus,
    reread_required: bool,
    poll_required: bool,
    detail: str,
    block_result: MergeTrainBlockResult | None = None,
    branch_update_result: MergeTrainBranchUpdateResult | None = None,
    wait_result: MergeTrainWaitResult | None = None,
    merge_result: MergeTrainMergeResult | None = None,
) -> MergeTrainWorkerStepResult:
    return MergeTrainWorkerStepResult(
        repository=dry_run_result.repository,
        base_branch=dry_run_result.base_branch,
        intended_next_action=dry_run_result.intended_next_action,
        selected_pr_number=selected_pr_number,
        status=status,
        reread_required=reread_required,
        poll_required=poll_required,
        detail=detail,
        dry_run_result=dry_run_result,
        block_result=block_result,
        branch_update_result=branch_update_result,
        wait_result=wait_result,
        merge_result=merge_result,
    )
