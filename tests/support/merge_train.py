from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol, cast

from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidate,
    MergeTrainBatchCandidateRecord,
    MergeTrainBatchEntry,
    MergeTrainBatchLandingEntry,
    MergeTrainBatchLandingPlan,
    MergeTrainBatchLandingPlanRecord,
    build_merge_train_batch_candidate,
    build_merge_train_batch_candidate_record,
)
from control_plane.contracts.merge_train_policy import (
    MergeTrainPolicy,
    MergeTrainPolicyRecord,
)
from control_plane.contracts.merge_train_run_record import (
    MergeTrainRunRecord,
    build_merge_train_run_record,
)
from control_plane.contracts.merge_train_stack_collapse import (
    build_merge_train_stack_collapse_plan,
    build_merge_train_stack_collapse_plan_record,
    execute_merge_train_stack_collapse_plan,
)
from control_plane.merge_train import (
    MergeTrainCheckStatus,
    MergeTrainDryRunSnapshot,
    MergeTrainPullRequestSnapshot,
    build_merge_train_dry_run_result,
    discover_merge_train_stack,
)
from control_plane.merge_train_github import MergeTrainGitHubError, MergeTrainGitHubStaleHeadError
from control_plane.merge_admission import MergeAdmissionDeniedError
from control_plane.service_auth import GitHubActionsIdentity, LaunchplaneAuthzPolicy
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.workflows.merge_train_worker import (
    MergeTrainWorkerClients,
    run_merge_train_worker_step,
)
from tests.merge_train_policy_fixtures import build_test_merge_train_policy
from tests.merge_train_policy_fixtures import build_test_merge_train_policy_record
from tests.support.auth import _identity


class _LandingPlanRecordUpdater(Protocol):
    def update_landing_plan_record(
        self, landing_plan_record: MergeTrainBatchLandingPlanRecord
    ) -> None: ...


class _FakeMergeTrainGitHubClient:
    land_batch_candidate_calls = 0
    cleanup_batch_candidate_ref_calls = 0

    def __init__(self, *, transport: object) -> None:
        self.transport = transport

    def add_pull_request_label(
        self, *, repository: str, pull_request_number: int, label: str
    ) -> None:
        return None

    def update_pull_request_branch(
        self, *, repository: str, pull_request_number: int, expected_head_sha: str
    ) -> None:
        return None

    def merge_pull_request(
        self,
        *,
        repository: str,
        pull_request_number: int,
        head_sha: str,
        merge_method: str,
    ) -> str:
        return f"merge-{pull_request_number}"

    def build_batch_candidate(
        self,
        *,
        candidate: MergeTrainBatchCandidate,
        checkpoint: (
            Callable[[MergeTrainBatchCandidate, MergeTrainBatchEntry | None, str], None] | None
        ) = None,
    ) -> MergeTrainBatchCandidate:
        if checkpoint is not None:
            checkpoint(candidate, None, "reset_candidate_ref")
            checkpoint(candidate, None, "candidate_ref_ready")
            for entry_index, entry in enumerate(candidate.entries, start=1):
                checkpoint(candidate, entry, "merge_candidate_entry")
                checkpoint(
                    candidate.model_copy(update={"candidate_sha": "candidate-built"}),
                    entry,
                    f"candidate_entry_merged:{entry_index}",
                )
        return candidate.model_copy(
            update={"candidate_sha": "candidate-built", "status": "ready_for_checks"}
        )

    def observe_batch_candidate_checks(
        self, *, candidate: MergeTrainBatchCandidate
    ) -> MergeTrainBatchCandidate:
        return candidate.model_copy(update={"required_checks_status": "pass", "status": "passed"})

    def land_batch_candidate(
        self,
        *,
        landing_plan: MergeTrainBatchLandingPlan,
        admission_guard: _LandingPlanRecordUpdater,
        recorded_at: str,
        provider_checkpoint: (
            Callable[[MergeTrainBatchLandingPlan, MergeTrainBatchLandingEntry], None] | None
        ) = None,
        checkpoint: (
            Callable[
                [MergeTrainBatchLandingPlan, MergeTrainBatchLandingEntry, str],
                MergeTrainBatchLandingPlanRecord | None,
            ]
            | None
        ) = None,
    ) -> MergeTrainBatchLandingPlan:
        type(self).land_batch_candidate_calls += 1
        merged_entries: list[MergeTrainBatchLandingEntry] = []
        for entry_index, entry in enumerate(landing_plan.entries):
            if checkpoint is not None:
                checkpoint(
                    landing_plan.model_copy(
                        update={
                            "entries": tuple(merged_entries) + landing_plan.entries[entry_index:]
                        }
                    ),
                    entry,
                    "merge_entry",
                )
            if provider_checkpoint is not None:
                provider_checkpoint(
                    landing_plan.model_copy(
                        update={
                            "entries": tuple(merged_entries) + landing_plan.entries[entry_index:]
                        }
                    ),
                    entry,
                )
            merged_entry = entry.model_copy(
                update={
                    "status": "merged",
                    "merge_commit_sha": f"merge-{entry.pull_request_number}",
                }
            )
            merged_entries.append(merged_entry)
            if checkpoint is not None:
                checkpoint(
                    landing_plan.model_copy(
                        update={
                            "entries": tuple(merged_entries)
                            + landing_plan.entries[entry_index + 1 :]
                        }
                    ),
                    merged_entry,
                    "entry_merged",
                )
        return landing_plan.model_copy(update={"entries": tuple(merged_entries)})

    def cleanup_batch_candidate_ref(self, *, landing_plan: MergeTrainBatchLandingPlan) -> bool:
        type(self).cleanup_batch_candidate_ref_calls += 1
        return True

    def candidate_ref_exists(self, *, repository: str, reference: str) -> bool:
        return True

    def pull_request_has_label(
        self, *, repository: str, pull_request_number: int, label: str
    ) -> bool:
        return True

    def pull_request_is_closed(
        self, *, repository: str, pull_request_number: int, expected_head_sha: str
    ) -> bool:
        return True

    def find_pull_request_comment_url(
        self, *, repository: str, pull_request_number: int, body_contains: str
    ) -> str:
        return f"https://github.com/{repository}/pull/{pull_request_number}#issuecomment-1"

    def pull_request_is_merged(
        self, *, repository: str, pull_request_number: int, expected_head_sha: str
    ) -> str:
        return f"merge-{pull_request_number}"

    def branch_contains_commit(self, *, repository: str, branch_ref: str, commit_sha: str) -> bool:
        return True

    def branch_head_sha(self, *, repository: str, branch_ref: str) -> str:
        return f"branch-head:{branch_ref}"

    def find_stack_child_merge_commit(
        self,
        *,
        repository: str,
        child_head_sha: str,
        expected_parent_head_sha: str,
        parent_head_ref: str,
        collapse_id: str,
        child_pull_request_number: int,
        parent_pull_request_number: int,
    ) -> str:
        return ""

    def merge_stack_child_into_parent(
        self,
        *,
        repository: str,
        child_head_sha: str,
        expected_parent_head_sha: str,
        parent_head_ref: str,
        protected_base_ref: str,
        collapse_id: str,
        child_pull_request_number: int,
        parent_pull_request_number: int,
    ) -> str:
        return f"stack-merge-{child_pull_request_number}-into-{parent_pull_request_number}"

    def comment_pull_request(self, *, repository: str, pull_request_number: int, body: str) -> str:
        return f"https://github.com/{repository}/pull/{pull_request_number}#issuecomment-1"

    def close_pull_request(
        self, *, repository: str, pull_request_number: int, expected_head_sha: str
    ) -> None:
        return None


class _FakeFailingMergeTrainGitHubClient(_FakeMergeTrainGitHubClient):
    def observe_batch_candidate_checks(
        self, *, candidate: MergeTrainBatchCandidate
    ) -> MergeTrainBatchCandidate:
        return candidate.model_copy(update={"required_checks_status": "fail", "status": "failed"})


class _StaleCandidateMergeTrainGitHubClient(_FakeMergeTrainGitHubClient):
    def build_batch_candidate(
        self,
        *,
        candidate: MergeTrainBatchCandidate,
        checkpoint: (
            Callable[[MergeTrainBatchCandidate, MergeTrainBatchEntry | None, str], None] | None
        ) = None,
    ) -> MergeTrainBatchCandidate:
        if checkpoint is not None:
            checkpoint(candidate, None, "reset_candidate_ref")
            checkpoint(candidate, None, "candidate_ref_ready")
            checkpoint(candidate, candidate.entries[0], "merge_candidate_entry")
        raise MergeTrainGitHubStaleHeadError(
            "Candidate entry conflicts with the rolling merge base.", status_code=409
        )


class _StaleLandingMergeTrainGitHubClient(_FakeMergeTrainGitHubClient):
    def land_batch_candidate(
        self,
        *,
        landing_plan: MergeTrainBatchLandingPlan,
        admission_guard: object,
        recorded_at: str,
        provider_checkpoint: (
            Callable[[MergeTrainBatchLandingPlan, MergeTrainBatchLandingEntry], None] | None
        ) = None,
        checkpoint: (
            Callable[
                [MergeTrainBatchLandingPlan, MergeTrainBatchLandingEntry, str],
                MergeTrainBatchLandingPlanRecord | None,
            ]
            | None
        ) = None,
    ) -> MergeTrainBatchLandingPlan:
        raise MergeTrainGitHubStaleHeadError(
            "Base branch moved outside the batch landing plan.", status_code=409
        )


class _UnavailableLandingMergeTrainGitHubClient(_FakeMergeTrainGitHubClient):
    def land_batch_candidate(
        self,
        *,
        landing_plan: MergeTrainBatchLandingPlan,
        admission_guard: object,
        recorded_at: str,
        provider_checkpoint: (
            Callable[[MergeTrainBatchLandingPlan, MergeTrainBatchLandingEntry], None] | None
        ) = None,
        checkpoint: (
            Callable[
                [MergeTrainBatchLandingPlan, MergeTrainBatchLandingEntry, str],
                MergeTrainBatchLandingPlanRecord | None,
            ]
            | None
        ) = None,
    ) -> MergeTrainBatchLandingPlan:
        raise MergeTrainGitHubError(
            "GitHub API request failed for /repos/example/repo", status_code=503
        )


class _BlockedAdmissionMergeTrainGitHubClient(_FakeMergeTrainGitHubClient):
    def land_batch_candidate(
        self,
        *,
        landing_plan: MergeTrainBatchLandingPlan,
        admission_guard: object,
        recorded_at: str,
        provider_checkpoint: (
            Callable[[MergeTrainBatchLandingPlan, MergeTrainBatchLandingEntry], None] | None
        ) = None,
        checkpoint: (
            Callable[
                [MergeTrainBatchLandingPlan, MergeTrainBatchLandingEntry, str],
                MergeTrainBatchLandingPlanRecord | None,
            ]
            | None
        ) = None,
    ) -> MergeTrainBatchLandingPlan:
        entry = landing_plan.entries[0]
        if checkpoint is not None:
            checkpoint(landing_plan, entry, "merge_entry")
        raise MergeAdmissionDeniedError(
            "Fresh merge readiness evidence did not admit the provider effect.",
            reason_code="merge_readiness_not_ready",
        )


class _ProgressedBlockedAdmissionMergeTrainGitHubClient(_FakeMergeTrainGitHubClient):
    def land_batch_candidate(
        self,
        *,
        landing_plan: MergeTrainBatchLandingPlan,
        admission_guard: _LandingPlanRecordUpdater,
        recorded_at: str,
        provider_checkpoint: (
            Callable[[MergeTrainBatchLandingPlan, MergeTrainBatchLandingEntry], None] | None
        ) = None,
        checkpoint: (
            Callable[
                [MergeTrainBatchLandingPlan, MergeTrainBatchLandingEntry, str],
                MergeTrainBatchLandingPlanRecord | None,
            ]
            | None
        ) = None,
    ) -> MergeTrainBatchLandingPlan:
        entry = landing_plan.entries[0]
        merged_entry = entry.model_copy(
            update={
                "status": "merged",
                "merge_commit_sha": f"merge-{entry.pull_request_number}",
            }
        )
        progress_plan = landing_plan.model_copy(update={"entries": (merged_entry,)})
        if checkpoint is not None:
            progress_record = checkpoint(progress_plan, merged_entry, "entry_merged")
            if progress_record is not None:
                admission_guard.update_landing_plan_record(progress_record)
        raise MergeAdmissionDeniedError(
            "Fresh merge readiness evidence did not admit the provider effect.",
            reason_code="merge_readiness_not_ready",
        )


class _CleanupFailingMergeTrainGitHubClient(_FakeMergeTrainGitHubClient):
    cleanup_batch_candidate_ref_calls = 0

    def cleanup_batch_candidate_ref(self, *, landing_plan: MergeTrainBatchLandingPlan) -> bool:
        type(self).cleanup_batch_candidate_ref_calls += 1
        raise MergeTrainGitHubError("candidate ref cleanup unavailable", status_code=503)


class _CleanupAlreadyMissingMergeTrainGitHubClient(_FakeMergeTrainGitHubClient):
    cleanup_batch_candidate_ref_calls = 0

    def candidate_ref_exists(self, *, repository: str, reference: str) -> bool:
        return False

    def cleanup_batch_candidate_ref(self, *, landing_plan: MergeTrainBatchLandingPlan) -> bool:
        type(self).cleanup_batch_candidate_ref_calls += 1
        return False


class _CleanupFailingWithoutStatusMergeTrainGitHubClient(_FakeMergeTrainGitHubClient):
    cleanup_batch_candidate_ref_calls = 0

    def cleanup_batch_candidate_ref(self, *, landing_plan: MergeTrainBatchLandingPlan) -> bool:
        type(self).cleanup_batch_candidate_ref_calls += 1
        raise MergeTrainGitHubError("candidate ref cleanup network unavailable")


class _StackCollapseWriteFailingFilesystemRecordStore(FilesystemRecordStore):
    def write_merge_train_stack_collapse_plan_record(self, record: object) -> Path:
        raise RuntimeError("stack collapse persistence unavailable")


class _CandidateReflowWriteFailingFilesystemRecordStore(FilesystemRecordStore):
    def write_merge_train_batch_candidate_record(self, record: object) -> Path:
        candidate_record = cast(MergeTrainBatchCandidateRecord, record)
        if "candidate-reflow" in candidate_record.source:
            raise RuntimeError("candidate reflow persistence unavailable")
        return super().write_merge_train_batch_candidate_record(candidate_record)


class _CandidateReflowSupersedeFailingFilesystemRecordStore(FilesystemRecordStore):
    def write_merge_train_batch_candidate_record(self, record: object) -> Path:
        candidate_record = cast(MergeTrainBatchCandidateRecord, record)
        if (
            candidate_record.status == "superseded"
            and "candidate-reflow" not in candidate_record.source
        ):
            raise RuntimeError("candidate supersession persistence unavailable")
        return super().write_merge_train_batch_candidate_record(candidate_record)


class _SameBatchIdReflowFilesystemRecordStore(FilesystemRecordStore):
    def write_merge_train_batch_candidate_record(self, record: object) -> Path:
        candidate_record = cast(MergeTrainBatchCandidateRecord, record)
        if "candidate-reflow" in candidate_record.source:
            records = self.list_merge_train_batch_candidate_records(
                repository=candidate_record.candidate.repository,
                base_branch=candidate_record.candidate.base_branch,
                status="active",
            )
            failed_record = next(
                record for record in records if record.candidate.status == "failed"
            )
            candidate_record = candidate_record.model_copy(
                update={
                    "candidate": candidate_record.candidate.model_copy(
                        update={"batch_id": failed_record.candidate.batch_id}
                    )
                }
            )
        return super().write_merge_train_batch_candidate_record(candidate_record)


class _NoopMergeTrainGitHubClient:
    def add_pull_request_label(
        self, *, repository: str, pull_request_number: int, label: str
    ) -> None:
        return None

    def update_pull_request_branch(
        self, *, repository: str, pull_request_number: int, expected_head_sha: str
    ) -> None:
        return None

    def merge_pull_request(
        self,
        *,
        repository: str,
        pull_request_number: int,
        head_sha: str,
        merge_method: str,
    ) -> str:
        return f"merge-{pull_request_number}"


class _FakeMergeTrainSnapshotReader:
    def __init__(self, *, transport: object) -> None:
        self.transport = transport

    def read_merge_train_snapshot(
        self, *, repository: str, base_branch: str
    ) -> MergeTrainDryRunSnapshot:
        return MergeTrainDryRunSnapshot(
            repository=repository,
            base_branch=base_branch,
            base_sha="current-base-main",
            pull_requests=(
                MergeTrainPullRequestSnapshot(
                    number=1,
                    url=f"https://github.com/{repository}/pull/1",
                    title="Ready PR",
                    created_at="2026-05-08T10:00:00Z",
                    labels=("ready-to-merge",),
                    actor_role="repo_admin",
                    head_sha="head-1",
                    head_ref="feature/root",
                    head_repository=repository,
                    base_ref=base_branch,
                    base_repository=repository,
                    base_sha="base-main",
                    mergeable="mergeable",
                    required_checks_status="pass",
                ),
            ),
        )


class _FakeExpandedMergeTrainSnapshotReader(_FakeMergeTrainSnapshotReader):
    def read_merge_train_snapshot(
        self, *, repository: str, base_branch: str
    ) -> MergeTrainDryRunSnapshot:
        base_snapshot = super().read_merge_train_snapshot(
            repository=repository, base_branch=base_branch
        )
        return base_snapshot.model_copy(
            update={
                "pull_requests": (
                    *base_snapshot.pull_requests,
                    MergeTrainPullRequestSnapshot(
                        number=2,
                        url=f"https://github.com/{repository}/pull/2",
                        title="Validation fix",
                        created_at="2026-05-08T10:05:00Z",
                        labels=("ready-to-merge",),
                        actor_role="repo_admin",
                        head_sha="head-2",
                        head_ref="feature/validation-fix",
                        head_repository=repository,
                        base_ref=base_branch,
                        base_repository=repository,
                        base_sha="base-main",
                        mergeable="mergeable",
                        required_checks_status="pass",
                    ),
                )
            }
        )


class _FakeEmptyMergeTrainSnapshotReader:
    def __init__(self, *, transport: object) -> None:
        self.transport = transport

    def read_merge_train_snapshot(
        self, *, repository: str, base_branch: str
    ) -> MergeTrainDryRunSnapshot:
        return MergeTrainDryRunSnapshot(
            repository=repository,
            base_branch=base_branch,
            base_sha="current-base-main",
            pull_requests=(),
        )


class _FakeStackedMergeTrainSnapshotReader:
    def __init__(self, *, transport: object) -> None:
        self.transport = transport

    def read_merge_train_snapshot(
        self, *, repository: str, base_branch: str
    ) -> MergeTrainDryRunSnapshot:
        return MergeTrainDryRunSnapshot(
            repository=repository,
            base_branch=base_branch,
            base_sha="current-base-main",
            pull_requests=(
                MergeTrainPullRequestSnapshot(
                    number=1,
                    url=f"https://github.com/{repository}/pull/1",
                    title="Root PR",
                    created_at="2026-05-08T10:00:00Z",
                    labels=("ready-to-merge",),
                    actor_role="repo_admin",
                    head_sha=self._root_head_sha(),
                    head_ref="feature/root",
                    head_repository=repository,
                    base_ref=base_branch,
                    base_repository=repository,
                    base_sha="base-main",
                    mergeable="mergeable",
                    required_checks_status="pass",
                ),
                MergeTrainPullRequestSnapshot(
                    number=2,
                    url=f"https://github.com/{repository}/pull/2",
                    title="Stacked child PR",
                    created_at="2026-05-08T11:00:00Z",
                    labels=(),
                    actor_role="repo_admin",
                    head_sha="head-child",
                    head_ref="feature/child",
                    head_repository=repository,
                    base_ref="feature/root",
                    base_repository=repository,
                    base_sha="head-root",
                    mergeable="mergeable",
                    required_checks_status="pending",
                ),
            ),
        )

    def _root_head_sha(self) -> str:
        return "head-root"


class _FakeCollapsedRootStackedMergeTrainSnapshotReader(_FakeStackedMergeTrainSnapshotReader):
    def _root_head_sha(self) -> str:
        return "stack-merge-2-into-1"


class _FakeMovedRootStackedMergeTrainSnapshotReader(_FakeStackedMergeTrainSnapshotReader):
    def read_merge_train_snapshot(
        self, *, repository: str, base_branch: str
    ) -> MergeTrainDryRunSnapshot:
        snapshot = super().read_merge_train_snapshot(repository=repository, base_branch=base_branch)
        return snapshot.model_copy(
            update={
                "pull_requests": tuple(
                    pull_request.model_copy(update={"head_sha": "moved-root-head"})
                    if pull_request.number == 1
                    else pull_request
                    for pull_request in snapshot.pull_requests
                )
            }
        )


def _merge_train_service_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "cbusillo/launchplane",
                    "workflow_refs": [
                        "cbusillo/launchplane/.github/workflows/merge-train.yml@refs/heads/main"
                    ],
                    "event_names": ["workflow_dispatch"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["merge_train.run_once"],
                }
            ]
        }
    )


def _merge_train_service_identity() -> GitHubActionsIdentity:
    return _identity(
        repository="cbusillo/launchplane",
        workflow_ref="cbusillo/launchplane/.github/workflows/merge-train.yml@refs/heads/main",
        event_name="workflow_dispatch",
    )


def _seed_merge_train_policy(
    state_dir: Path, *, policy: MergeTrainPolicyRecord | None = None
) -> MergeTrainPolicyRecord:
    record = policy or build_test_merge_train_policy_record()
    FilesystemRecordStore(state_dir).write_merge_train_policy_record(record)
    return record


def _merge_train_policy_table(
    repository: str,
    base_branch: str = "main",
    *,
    scheduler_enabled: bool = False,
    scheduler_runner_mode: str = "controller",
    scheduler_mutate: bool = False,
) -> str:
    scheduler_table = ""
    if scheduler_enabled:
        scheduler_table = f"""
[policies.scheduler]
enabled = true
runner_mode = "{scheduler_runner_mode}"
mutate = {str(scheduler_mutate).lower()}
"""
    return f"""[[policies]]
repository = "{repository}"
base_branch = "{base_branch}"
enqueue_label = "ready-to-merge"
blocked_label = "merge-blocked"
stack_child_disposition_label = "stack-landed"
merge_method = "merge"
failure_policy = "pause_train"

[policies.enqueue]
label_required = true
allowed_actor_roles = ["repo_owner", "repo_admin"]

[policies.merge_identity]
kind = "github_actions_oidc"
name = "launchplane-merge-train"

[policies.service_authz]
action = "merge_train.run_once"
product = "launchplane"
context = "launchplane"

[policies.github_token]
env_var = "GH_TOKEN"
{scheduler_table}
"""


def _merge_train_run_record(
    *,
    recorded_at: str,
    required_checks_status: MergeTrainCheckStatus = "pass",
    mutate: bool = False,
) -> MergeTrainRunRecord:
    policy = build_test_merge_train_policy()
    snapshot = MergeTrainDryRunSnapshot(
        repository="cbusillo/sellyouroutboard",
        base_branch="main",
        pull_requests=(
            MergeTrainPullRequestSnapshot(
                number=1,
                url="https://github.com/cbusillo/sellyouroutboard/pull/1",
                title="Ready PR",
                created_at="2026-05-08T10:00:00Z",
                labels=("ready-to-merge",),
                actor_role="repo_admin",
                head_sha="head-1",
                base_ref="main",
                base_sha="base-main",
                mergeable="mergeable",
                required_checks_status=required_checks_status,
            ),
        ),
    )
    dry_run_result = build_merge_train_dry_run_result(policy=policy, snapshot=snapshot)
    worker_step_result = None
    if mutate:
        noop_client = _NoopMergeTrainGitHubClient()
        worker_step_result = run_merge_train_worker_step(
            policy=policy,
            snapshot=snapshot,
            clients=MergeTrainWorkerClients(
                label_client=noop_client,
                branch_client=noop_client,
                merge_client=noop_client,
            ),
        )
    return build_merge_train_run_record(
        recorded_at=recorded_at,
        trace_id="launchplane_req_merge_train_service_test",
        policy_sha256=policy.policy_sha256,
        snapshot=snapshot,
        dry_run_result=dry_run_result,
        worker_step_result=worker_step_result,
    )


def _seed_merge_train_batch_candidate_record(
    state_dir: Path,
    *,
    status: str = "planned",
    required_checks_status: str = "pending",
    candidate_sha: str = "",
    policy: MergeTrainPolicy | None = None,
    snapshot_reader: type[_FakeMergeTrainSnapshotReader] = _FakeMergeTrainSnapshotReader,
) -> MergeTrainBatchCandidateRecord:
    merge_train_policy = policy or build_test_merge_train_policy()
    snapshot = snapshot_reader(transport=object()).read_merge_train_snapshot(
        repository="cbusillo/sellyouroutboard",
        base_branch="main",
    )
    dry_run_result = build_merge_train_dry_run_result(
        policy=merge_train_policy,
        snapshot=snapshot,
    )
    candidate = build_merge_train_batch_candidate(
        dry_run_result=dry_run_result,
        base_sha=snapshot.base_sha,
        policy_sha256=merge_train_policy.policy_sha256,
        created_at="2026-05-13T21:00:00Z",
    ).model_copy(
        update={
            "status": status,
            "required_checks_status": required_checks_status,
            "candidate_sha": candidate_sha,
        }
    )
    record = build_merge_train_batch_candidate_record(
        candidate=candidate,
        source=f"test:{status}",
        updated_at="2026-05-13T21:00:00Z",
    )
    FilesystemRecordStore(state_dir).write_merge_train_batch_candidate_record(record)
    return record


def _mark_merge_train_batch_candidate_record_passed(
    state_dir: Path, *, record_id: str
) -> MergeTrainBatchCandidateRecord:
    store = FilesystemRecordStore(state_dir)
    existing_record = next(
        record
        for record in store.list_merge_train_batch_candidate_records(
            repository="cbusillo/sellyouroutboard",
            base_branch="main",
        )
        if record.record_id == record_id
    )
    candidate = existing_record.candidate.model_copy(
        update={
            "status": "passed",
            "required_checks_status": "pass",
            "candidate_sha": "candidate-built",
        }
    )
    passed_record = build_merge_train_batch_candidate_record(
        candidate=candidate,
        source="test:passed",
        updated_at="2026-05-13T21:05:00Z",
    )
    store.write_merge_train_batch_candidate_record(passed_record)
    return passed_record


def _seed_merge_train_stack_collapse_plan_record(
    state_dir: Path,
    *,
    policy: MergeTrainPolicy | None = None,
    snapshot_reader: type[
        _FakeStackedMergeTrainSnapshotReader
    ] = _FakeStackedMergeTrainSnapshotReader,
) -> str:
    merge_train_policy = policy or build_test_merge_train_policy()
    snapshot = snapshot_reader(transport=object()).read_merge_train_snapshot(
        repository="cbusillo/sellyouroutboard",
        base_branch="main",
    )
    dry_run_result = build_merge_train_dry_run_result(
        policy=merge_train_policy,
        snapshot=snapshot,
    )
    selected_pr = dry_run_result.selected_pr
    assert selected_pr is not None
    stack_discovery = discover_merge_train_stack(
        snapshot=snapshot,
        root_pull_request_number=selected_pr.number,
    )
    stack_collapse_plan = build_merge_train_stack_collapse_plan(
        discovery_result=stack_discovery,
        policy_key=dry_run_result.policy_key,
        policy_sha256=merge_train_policy.policy_sha256,
        created_at="2026-05-13T21:00:00Z",
    )
    record = build_merge_train_stack_collapse_plan_record(
        plan=stack_collapse_plan,
        source="test:plan",
        updated_at="2026-05-13T21:00:00Z",
    )
    FilesystemRecordStore(state_dir).write_merge_train_stack_collapse_plan_record(record)
    return record.record_id


def _seed_executed_merge_train_stack_collapse_plan_record(
    state_dir: Path,
    *,
    policy: MergeTrainPolicy | None = None,
    snapshot_reader: type[
        _FakeStackedMergeTrainSnapshotReader
    ] = _FakeStackedMergeTrainSnapshotReader,
) -> str:
    planned_record_id = _seed_merge_train_stack_collapse_plan_record(
        state_dir,
        policy=policy,
        snapshot_reader=snapshot_reader,
    )
    store = FilesystemRecordStore(state_dir)
    planned_record = next(
        record
        for record in store.list_merge_train_stack_collapse_plan_records(
            repository="cbusillo/sellyouroutboard", base_branch="main"
        )
        if record.record_id == planned_record_id
    )
    executed_plan = execute_merge_train_stack_collapse_plan(
        plan=planned_record.plan,
        branch_client=_FakeMergeTrainGitHubClient(transport=object()),
        updated_at="2026-05-13T21:02:00Z",
    )
    executed_record = build_merge_train_stack_collapse_plan_record(
        plan=executed_plan,
        source="test:execute",
        updated_at="2026-05-13T21:02:00Z",
    )
    store.write_merge_train_stack_collapse_plan_record(executed_record)
    return executed_record.record_id


def _seed_admitted_merge_train_stack_collapse_candidate(
    state_dir: Path,
    *,
    executed_record_id: str,
    policy: MergeTrainPolicy | None = None,
    snapshot_reader: type[
        _FakeStackedMergeTrainSnapshotReader
    ] = _FakeCollapsedRootStackedMergeTrainSnapshotReader,
) -> MergeTrainBatchCandidateRecord:
    merge_train_policy = policy or build_test_merge_train_policy()
    store = FilesystemRecordStore(state_dir)
    executed_record = next(
        record
        for record in store.list_merge_train_stack_collapse_plan_records(
            repository="cbusillo/sellyouroutboard", base_branch="main"
        )
        if record.record_id == executed_record_id
    )
    snapshot = snapshot_reader(transport=object()).read_merge_train_snapshot(
        repository="cbusillo/sellyouroutboard",
        base_branch="main",
    )
    root_pull_request = next(
        pull_request
        for pull_request in snapshot.pull_requests
        if pull_request.number == executed_record.plan.root_pull_request_number
    )
    dry_run_result = build_merge_train_dry_run_result(
        policy=merge_train_policy,
        snapshot=snapshot.model_copy(update={"pull_requests": (root_pull_request,)}),
    )
    candidate = build_merge_train_batch_candidate(
        dry_run_result=dry_run_result,
        base_sha=snapshot.base_sha,
        policy_sha256=merge_train_policy.policy_sha256,
        created_at="2026-05-13T21:03:00Z",
    )
    candidate_record = build_merge_train_batch_candidate_record(
        candidate=candidate,
        source="test:stack-collapse-admit",
        updated_at="2026-05-13T21:03:00Z",
    )
    store.write_merge_train_batch_candidate_record(candidate_record)
    return candidate_record
