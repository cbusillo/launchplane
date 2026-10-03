from datetime import datetime, timezone
import json
from hashlib import sha256
import logging
from time import sleep
from typing import TYPE_CHECKING, Callable, Literal, Protocol, TypeVar
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, model_validator

from control_plane.contracts.advisory_check_projection import is_launchplane_projected_check
from control_plane.contracts.merge_train_batch import MergeTrainBatchCandidate
from control_plane.contracts.merge_train_branch_refresh_record import MergeTrainBranchRefreshRecord
from control_plane.contracts.merge_train_batch import MergeTrainBatchEntry
from control_plane.contracts.merge_train_batch import MergeTrainBatchHeldOutEntry
from control_plane.contracts.merge_train_batch import MergeTrainBatchLandingEntry
from control_plane.contracts.merge_train_batch import MergeTrainBatchLandingPlan
from control_plane.contracts.merge_train_batch import MergeTrainBatchLandingPlanRecord
from control_plane.contracts.merge_train_historical_completion import (
    MergeTrainHistoricalCompletionEntryEvidence,
    MergeTrainHistoricalCompletionProviderEvidence,
)
from control_plane.contracts.merge_train_effect import (
    CandidateHeadMergeEffect,
    CandidateHeadMergeOutcome,
    CandidateRefDeleteEffect,
    CandidateRefPrepareEffect,
    MergeTrainEffectLineage,
    MergeTrainSemanticEffectExecutor,
    PullRequestHeadRefreshEffect,
    PullRequestLandingEffect,
    StackChildCloseEffect,
    StackChildCommentEffect,
    StackChildLabelEffect,
    StackChildMergeEffect,
)
from control_plane.contracts.merge_train_stack_collapse import MergeTrainStackCollapseBranchClient
from control_plane.contracts.merge_train_policy import MergeTrainMergeMethod
from control_plane.contracts.merge_train_structural_provenance import (
    MergeTrainRollingStep,
    MergeTrainStructuralEntryBinding,
    MergeTrainStructuralProvenance,
)
from control_plane.github_payload import json_object
from control_plane.github_payload import required_positive_int
from control_plane.github_payload import required_string_text
from control_plane.github_response_headers import GitHubResponseHeadersObserver
from control_plane.github_response_headers import notify_github_quota_response_headers
from control_plane.github_request_timing import timed_github_request
from control_plane.merge_train_dependency_updates import DependencyUpdateClass
from control_plane.merge_train_dependency_updates import classify_dependency_update
from control_plane.source_control_change import change_fingerprint
from control_plane.merge_train import MergeTrainCheckStatus
from control_plane.merge_train import MergeTrainDryRunSnapshot
from control_plane.merge_train import MergeTrainLabelActor
from control_plane.merge_train import MergeTrainMergeableState
from control_plane.merge_train import MergeTrainPullRequestSnapshot
from control_plane.merge_train import MergeTrainPullRequestState
from control_plane.merge_train import MergeTrainQueueEntry
from control_plane.merge_train import MergeTrainReviewConversations
from control_plane.merge_train import CODE_SCANNING_REVIEW_AUTHOR
from control_plane.merge_admission import GuardedMergeAdmission, MergeAdmissionDeniedError

logger = logging.getLogger(__name__)


class MergeTrainBranchRefreshReadStore(Protocol):
    def list_merge_train_branch_refresh_records(
        self, *, repository: str, pull_request_number: int
    ) -> tuple[MergeTrainBranchRefreshRecord, ...]: ...


class MergeTrainBranchRefreshRecorder(Protocol):
    """Keeps the train's own branch refreshes, bound to the commit the provider made."""

    def __call__(
        self,
        *,
        repository: str,
        pull_request_number: int,
        expected_head_sha: str,
        result_head_sha: str,
        merged_base_sha: str,
        requested_at: datetime,
    ) -> None: ...


# GitHub makes the refresh commit after it answers; read the head back this often.
BRANCH_REFRESH_READBACK_ATTEMPTS = 10
BRANCH_REFRESH_READBACK_INTERVAL_SECONDS = 1.0


if TYPE_CHECKING:
    from control_plane.tenant_admission_controller import TenantAdmissionTechnicalChecks


# GitHub's own identity for commits it signs (web edits and Dependabot).
_GITHUB_WEB_FLOW_USER_ID = 19864447


class MergeTrainGitHubError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class MergeTrainGitHubStaleHeadError(MergeTrainGitHubError):
    """Raised when GitHub state no longer matches guarded merge evidence."""


class MergeTrainGitHubCandidateEntryConflictError(MergeTrainGitHubStaleHeadError):
    """A queued pull request's head does not merge cleanly into the batch candidate."""

    def __init__(self, *, pull_request_number: int, head_sha: str) -> None:
        self.pull_request_number = pull_request_number
        self.head_sha = head_sha
        super().__init__(
            f"Pull request #{pull_request_number} conflicts with the batch candidate built so far.",
            status_code=409,
        )


class MergeTrainGitHubMergeRejectedError(MergeTrainGitHubError):
    """A conclusive merge refusal with bounded, separately observed diagnosis."""

    def __init__(self, *, pull_request_number: int, observed_merge_state: str = "") -> None:
        self.pull_request_number = pull_request_number
        # GitHub's mergeable_state for the same open head, when it could be read.
        self.observed_merge_state = observed_merge_state
        self.refusal_diagnosis = {
            "behind": "head_behind_base",
            "blocked": "merge_blocked",
        }.get(observed_merge_state, "unconfirmed")
        diagnosis = {
            "head_behind_base": (
                "The same PR head is behind its base; refresh the source PR branches and let "
                "the train build a fresh candidate before another attempt."
            ),
            "merge_blocked": (
                "GitHub reports the PR blocked by a base-branch requirement, such as an "
                "unresolved review conversation or a missing review; clear it before another "
                "attempt."
            ),
        }.get(self.refusal_diagnosis, "Reread the PR's merge requirements before another attempt.")
        super().__init__(
            f"GitHub refused to merge PR #{pull_request_number} (HTTP 405). {diagnosis}",
            status_code=405,
        )


HistoricalCompletionProofStatus = Literal["unsupported", "indeterminate"]
HistoricalCompletionProofReason = Literal[
    "plan_invalid",
    "plan_noop",
    "plan_unmerged",
    "plan_bound",
    "provider_binding_mismatch",
    "provider_unavailable",
    "provider_response_malformed",
    "base_not_contains_merge",
    "target_moved",
]


class MergeTrainHistoricalCompletionProofError(MergeTrainGitHubError):
    """Closed, public-safe disposition for a read-only historical proof attempt."""

    def __init__(
        self,
        *,
        status: HistoricalCompletionProofStatus,
        reason_code: HistoricalCompletionProofReason,
        pull_request_number: int | None = None,
    ) -> None:
        self.proof_status = status
        self.reason_code = reason_code
        self.pull_request_number = pull_request_number
        super().__init__(
            f"Historical completion proof {reason_code}.",
            status_code=409 if status == "unsupported" else 503,
        )


ModelT = TypeVar("ModelT", bound=BaseModel)
MERGE_REF_READ_DELAYS_SECONDS = (0.25, 0.5, 1.0, 2.0, 4.0)


class MergeTrainGitHubTransport(Protocol):
    def request(
        self, *, method: str, path: str, body: dict[str, object] | None = None
    ) -> object: ...


class UrllibMergeTrainGitHubTransport:
    def __init__(
        self,
        *,
        token: str,
        api_base_url: str = "https://api.github.com",
        response_headers_observer: GitHubResponseHeadersObserver | None = None,
    ) -> None:
        self.token = _required_value(token, "GitHub token is required.")
        self.api_base_url = _required_value(
            api_base_url, "GitHub API base URL is required."
        ).rstrip("/")
        self.response_headers_observer = response_headers_observer

    def request(self, *, method: str, path: str, body: dict[str, object] | None = None) -> object:
        request_body = None
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if body is not None:
            request_body = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            url=f"{self.api_base_url}{path}",
            method=method,
            headers=headers,
            data=request_body,
        )
        try:
            with (
                timed_github_request(method=method, path=path),
                urlopen(request, timeout=15) as response,
            ):
                response_text = response.read().decode("utf-8")
                notify_github_quota_response_headers(
                    self.response_headers_observer,
                    getattr(response, "headers", None),
                )
                return json.loads(response_text) if response_text.strip() else None
        except HTTPError as error:
            raise _github_http_error(path=path, status_code=error.code, error=error) from error
        except (URLError, OSError) as error:
            raise MergeTrainGitHubError(f"GitHub API request failed for {path}: {error}") from error
        except json.JSONDecodeError as error:
            raise MergeTrainGitHubError(
                f"GitHub API response for {path} was not valid JSON."
            ) from error


class GitHubMergeTrainClient(MergeTrainStackCollapseBranchClient):
    def __init__(
        self,
        *,
        transport: MergeTrainGitHubTransport,
        effect_executor: MergeTrainSemanticEffectExecutor | None = None,
        branch_refresh_recorder: MergeTrainBranchRefreshRecorder | None = None,
        branch_refresh_store: MergeTrainBranchRefreshReadStore | None = None,
        wait: Callable[[float], None] = sleep,
    ) -> None:
        self.transport = transport
        self._effect_executor = effect_executor
        self._branch_refresh_recorder = branch_refresh_recorder
        self._branch_refresh_store = branch_refresh_store
        self._wait = wait

    def read_merge_train_snapshot(
        self, *, repository: str, base_branch: str
    ) -> MergeTrainDryRunSnapshot:
        """Read planning evidence through the client-owned provider boundary."""
        return GitHubMergeTrainSnapshotReader(
            transport=self.transport, branch_refresh_store=self._branch_refresh_store
        ).read_merge_train_snapshot(repository=repository, base_branch=base_branch)

    def read_pull_request_snapshot(
        self, *, repository: str, pull_request_number: int
    ) -> MergeTrainPullRequestSnapshot:
        return GitHubMergeTrainSnapshotReader(
            transport=self.transport, branch_refresh_store=self._branch_refresh_store
        ).read_pull_request_snapshot(repository=repository, pull_request_number=pull_request_number)

    def read_review_conversations(
        self, *, repository: str, base_branch: str, pull_request_number: int
    ) -> MergeTrainReviewConversations | None:
        """Return unresolved threads that can block this pull request's merge, if any."""
        repository_path = _repository_path(repository)
        rule = _conversation_resolution_rule(
            transport=self.transport, repository_path=repository_path, base_branch=base_branch
        )
        if rule == "not_required":
            return None
        return _review_conversations(
            transport=self.transport,
            repository_path=repository_path,
            pull_request_number=pull_request_number,
            rule=rule,
        )

    def observe_historical_batch_completion(
        self,
        *,
        landing_plan: MergeTrainBatchLandingPlan,
        observed_at: str,
    ) -> MergeTrainHistoricalCompletionProviderEvidence:
        """Read exact merged evidence without admitting or dispatching an effect."""
        entries = landing_plan.entries
        try:
            _required_value(observed_at, "Historical completion observation time is required.")
            repository_path = _repository_path(landing_plan.repository)
            if not 1 <= len(entries) <= 25:
                reason_code: HistoricalCompletionProofReason = "plan_bound"
                raise MergeTrainHistoricalCompletionProofError(
                    status="unsupported", reason_code=reason_code
                )
            if tuple(entry.position for entry in entries) != tuple(
                range(1, len(entries) + 1)
            ) or len({entry.pull_request_number for entry in entries}) != len(entries):
                raise MergeTrainHistoricalCompletionProofError(
                    status="unsupported", reason_code="plan_invalid"
                )
            for entry in entries:
                if entry.status not in {"planned", "merging"}:
                    raise MergeTrainHistoricalCompletionProofError(
                        status="unsupported",
                        reason_code="plan_invalid",
                        pull_request_number=entry.pull_request_number,
                    )
                if _recorded_candidate_step_is_no_op(entry):
                    raise MergeTrainHistoricalCompletionProofError(
                        status="unsupported",
                        reason_code="plan_noop",
                        pull_request_number=entry.pull_request_number,
                    )
                if not all(
                    (
                        entry.expected_head_sha,
                        entry.expected_head_tree_sha,
                        entry.expected_base_sha,
                        entry.recorded_candidate_parent_sha,
                        entry.recorded_candidate_parent_tree_sha,
                        entry.recorded_candidate_result_sha,
                        entry.recorded_candidate_result_tree_sha,
                    )
                ):
                    raise MergeTrainHistoricalCompletionProofError(
                        status="unsupported",
                        reason_code="plan_invalid",
                        pull_request_number=entry.pull_request_number,
                    )
            first_entry = entries[0]
            if first_entry.recorded_candidate_parent_sha != first_entry.expected_base_sha:
                raise MergeTrainHistoricalCompletionProofError(
                    status="unsupported",
                    reason_code="plan_invalid",
                    pull_request_number=first_entry.pull_request_number,
                )
            expected_base_sha = first_entry.expected_base_sha
            for entry in entries:
                if entry.expected_base_sha != expected_base_sha:
                    raise MergeTrainHistoricalCompletionProofError(
                        status="unsupported",
                        reason_code="plan_invalid",
                        pull_request_number=entry.pull_request_number,
                    )
        except MergeTrainHistoricalCompletionProofError:
            raise
        except (MergeTrainGitHubError, TypeError, ValueError) as error:
            raise MergeTrainHistoricalCompletionProofError(
                status="unsupported", reason_code="plan_invalid"
            ) from error

        try:
            observed_base_sha, observed_base_tree_sha = _base_branch_identity(
                transport=self.transport,
                repository_path=repository_path,
                base_branch=landing_plan.base_branch,
            )
        except MergeTrainGitHubError as error:
            raise _historical_provider_error(error) from error

        rolling_parent_sha = first_entry.expected_base_sha
        rolling_parent_tree_sha = first_entry.recorded_candidate_parent_tree_sha
        evidence: list[MergeTrainHistoricalCompletionEntryEvidence] = []
        for entry in entries:
            try:
                recovered = self._already_merged_landing_entry(
                    repository_path=repository_path,
                    entry=entry,
                    expected_base_ref=landing_plan.base_branch,
                    expected_rolling_base_sha=rolling_parent_sha,
                    expected_rolling_base_tree_sha=rolling_parent_tree_sha,
                )
            except MergeTrainGitHubError as error:
                raise _historical_provider_error(
                    error, pull_request_number=entry.pull_request_number
                ) from error
            if recovered is None:
                raise MergeTrainHistoricalCompletionProofError(
                    status="unsupported",
                    reason_code="plan_unmerged",
                    pull_request_number=entry.pull_request_number,
                )
            if recovered.merge_commit_tree_sha != entry.recorded_candidate_result_tree_sha:
                raise MergeTrainHistoricalCompletionProofError(
                    status="unsupported",
                    reason_code="provider_binding_mismatch",
                    pull_request_number=entry.pull_request_number,
                )
            try:
                contained = _branch_contains_commit_at_pinned_base(
                    transport=self.transport,
                    repository_path=repository_path,
                    merge_commit_sha=recovered.merge_commit_sha,
                    pinned_base_sha=observed_base_sha,
                )
            except MergeTrainGitHubError as error:
                raise _historical_provider_error(
                    error, pull_request_number=entry.pull_request_number
                ) from error
            if not contained:
                raise MergeTrainHistoricalCompletionProofError(
                    status="unsupported",
                    reason_code="base_not_contains_merge",
                    pull_request_number=entry.pull_request_number,
                )
            evidence.append(
                MergeTrainHistoricalCompletionEntryEvidence(
                    pull_request_number=entry.pull_request_number,
                    position=entry.position,
                    expected_head_sha=entry.expected_head_sha,
                    expected_head_tree_sha=entry.expected_head_tree_sha,
                    expected_base_sha=entry.expected_base_sha,
                    expected_parent_tree_sha=entry.recorded_candidate_parent_tree_sha,
                    expected_result_tree_sha=entry.recorded_candidate_result_tree_sha,
                    observed_head_sha=recovered.landed_head_sha,
                    observed_head_tree_sha=recovered.landed_head_tree_sha,
                    observed_merge_commit_sha=recovered.merge_commit_sha,
                    observed_merge_commit_tree_sha=recovered.merge_commit_tree_sha,
                    observed_parent_sha=rolling_parent_sha,
                    observed_parent_tree_sha=rolling_parent_tree_sha,
                    base_contains_merge_commit=True,
                )
            )
            rolling_parent_sha = recovered.merge_commit_sha
            rolling_parent_tree_sha = recovered.merge_commit_tree_sha

        try:
            final_observed_base_sha, final_observed_base_tree_sha = _base_branch_identity(
                transport=self.transport,
                repository_path=repository_path,
                base_branch=landing_plan.base_branch,
            )
        except MergeTrainGitHubError as error:
            raise _historical_provider_error(error) from error
        if (
            final_observed_base_sha != observed_base_sha
            or final_observed_base_tree_sha != observed_base_tree_sha
        ):
            raise MergeTrainHistoricalCompletionProofError(
                status="indeterminate", reason_code="target_moved"
            )
        return MergeTrainHistoricalCompletionProviderEvidence(
            observed_at=observed_at,
            observed_base_sha=observed_base_sha,
            observed_base_tree_sha=observed_base_tree_sha,
            final_observed_base_sha=final_observed_base_sha,
            entries=tuple(evidence),
        )

    @property
    def semantic_effect_executor(self) -> MergeTrainSemanticEffectExecutor:
        if self._effect_executor is None:
            self._effect_executor = LegacyMergeTrainEffectExecutor(client=self)
        return self._effect_executor

    def build_batch_candidate(
        self,
        *,
        candidate: MergeTrainBatchCandidate,
        effect_executor: MergeTrainSemanticEffectExecutor | None = None,
        checkpoint: (
            Callable[[MergeTrainBatchCandidate, MergeTrainBatchEntry | None, str], None] | None
        ) = None,
    ) -> MergeTrainBatchCandidate:
        resolved_effect_executor = effect_executor or self.semantic_effect_executor
        repository_path = _repository_path(candidate.repository)
        construction_ref = merge_train_construction_ref(candidate.candidate_ref)
        candidate_branch = _branch_name_from_ref(construction_ref)
        if checkpoint is not None:
            checkpoint(candidate, None, "reset_construction_ref")
        resolved_effect_executor.prepare_candidate_ref(
            CandidateRefPrepareEffect(
                lineage=MergeTrainEffectLineage(
                    repository=candidate.repository,
                    base_branch=candidate.base_branch,
                    batch_id=candidate.batch_id,
                ),
                candidate_ref=construction_ref,
                base_sha=candidate.base_sha,
            )
        )
        if checkpoint is not None:
            checkpoint(candidate, None, "construction_ref_ready")
        base_identity = _git_commit_identity(
            transport=self.transport,
            repository_path=repository_path,
            commit_sha=candidate.base_sha,
        )
        resolved_entries = tuple(
            _validated_model_update(
                entry,
                head_tree_sha=_git_commit_identity(
                    transport=self.transport,
                    repository_path=repository_path,
                    commit_sha=entry.head_sha,
                )[1],
            )
            for entry in candidate.entries
        )
        candidate = _validated_model_update(candidate, entries=resolved_entries, status="building")
        candidate_sha, candidate_tree_sha = base_identity
        rolling_steps: list[MergeTrainRollingStep] = []
        for entry_index, entry in enumerate(candidate.entries, start=1):
            if checkpoint is not None:
                checkpoint(candidate, entry, "merge_candidate_entry")
            parent_sha = candidate_sha
            parent_tree_sha = candidate_tree_sha
            merge_outcome = resolved_effect_executor.merge_candidate_head(
                CandidateHeadMergeEffect(
                    lineage=MergeTrainEffectLineage(
                        repository=candidate.repository,
                        base_branch=candidate.base_branch,
                        batch_id=candidate.batch_id,
                    ),
                    candidate_ref=construction_ref,
                    rolling_parent_sha=parent_sha,
                    pull_request_number=entry.pull_request_number,
                    head_sha=entry.head_sha,
                )
            )
            if merge_outcome.result_sha is None:
                observed_result_sha = _base_branch_sha(
                    transport=self.transport,
                    repository_path=repository_path,
                    base_branch=candidate_branch,
                )
                observed_result_sha, observed_result_tree_sha = _git_commit_identity(
                    transport=self.transport,
                    repository_path=repository_path,
                    commit_sha=observed_result_sha,
                )
                if observed_result_sha != parent_sha or observed_result_tree_sha != parent_tree_sha:
                    raise MergeTrainGitHubStaleHeadError(
                        "GitHub no-op merge moved the candidate ref outside the recorded parent.",
                        status_code=409,
                    )
                if not self.branch_contains_commit(
                    repository=candidate.repository,
                    branch_ref=candidate_branch,
                    commit_sha=entry.head_sha,
                ):
                    raise MergeTrainGitHubStaleHeadError(
                        "GitHub no-op merge did not prove the recorded head is contained.",
                        status_code=409,
                    )
                rolling_steps.append(
                    MergeTrainRollingStep(
                        position=entry.position,
                        pull_request_number=entry.pull_request_number,
                        parent_sha=parent_sha,
                        parent_tree_sha=parent_tree_sha,
                        head_sha=entry.head_sha,
                        head_tree_sha=entry.head_tree_sha,
                        result_sha=parent_sha,
                        result_tree_sha=parent_tree_sha,
                        kind="no_op_already_contained",
                    )
                )
                progress_candidate = _candidate_with_structural_provenance(
                    candidate=candidate,
                    candidate_sha=candidate_sha,
                    candidate_tree_sha=candidate_tree_sha,
                    rolling_steps=tuple(rolling_steps),
                )
                if checkpoint is not None:
                    checkpoint(
                        progress_candidate,
                        entry,
                        f"candidate_entry_merged:{entry_index}",
                    )
                candidate = progress_candidate
                continue
            response_sha = merge_outcome.result_sha
            observed_candidate_sha = _wait_for_branch_sha(
                transport=self.transport,
                repository_path=repository_path,
                base_branch=candidate_branch,
                expected_sha=response_sha,
                previous_sha=parent_sha,
            )
            if observed_candidate_sha != response_sha:
                raise MergeTrainGitHubStaleHeadError(
                    "GitHub candidate ref did not resolve to the reported merge commit.",
                    status_code=409,
                )
            candidate_sha, candidate_tree_sha = _validated_merge_commit_identity(
                transport=self.transport,
                repository_path=repository_path,
                commit_sha=response_sha,
                expected_parent_sha=parent_sha,
                expected_head_sha=entry.head_sha,
            )
            rolling_steps.append(
                MergeTrainRollingStep(
                    position=entry.position,
                    pull_request_number=entry.pull_request_number,
                    parent_sha=parent_sha,
                    parent_tree_sha=parent_tree_sha,
                    head_sha=entry.head_sha,
                    head_tree_sha=entry.head_tree_sha,
                    result_sha=candidate_sha,
                    result_tree_sha=candidate_tree_sha,
                    kind="merge_commit",
                )
            )
            progress_candidate = _candidate_with_structural_provenance(
                candidate=candidate,
                candidate_sha=candidate_sha,
                candidate_tree_sha=candidate_tree_sha,
                rolling_steps=tuple(rolling_steps),
            )
            if checkpoint is not None:
                checkpoint(
                    progress_candidate,
                    entry,
                    f"candidate_entry_merged:{entry_index}",
                )
            candidate = progress_candidate
        if checkpoint is not None:
            checkpoint(candidate, None, "publish_candidate_ref")
        resolved_effect_executor.prepare_candidate_ref(
            CandidateRefPrepareEffect(
                lineage=MergeTrainEffectLineage(
                    repository=candidate.repository,
                    base_branch=candidate.base_branch,
                    batch_id=candidate.batch_id,
                ),
                candidate_ref=candidate.candidate_ref,
                # Publication points at the completed candidate, never an intermediate base.
                base_sha=candidate_sha,
            )
        )
        _verify_candidate_publication(
            transport=self.transport,
            repository_path=repository_path,
            candidate_ref=candidate.candidate_ref,
            expected_sha=candidate_sha,
        )
        if checkpoint is not None:
            checkpoint(candidate, None, "candidate_ref_published")
        try:
            resolved_effect_executor.delete_candidate_ref(
                CandidateRefDeleteEffect(
                    lineage=MergeTrainEffectLineage(
                        repository=candidate.repository,
                        base_branch=candidate.base_branch,
                        batch_id=candidate.batch_id,
                    ),
                    candidate_ref=construction_ref,
                )
            )
        except MergeTrainGitHubError as error:
            # Rebuilding here would replace a verified publication and start duplicate CI.
            logger.warning(
                "Published candidate retained; construction ref cleanup failed for %s "
                "(GitHub status %s).",
                construction_ref,
                error.status_code,
            )
        return _validated_model_update(candidate, status="ready_for_checks")

    def probe_batch_entry_conflicts(
        self,
        *,
        repository: str,
        base_branch: str,
        base_sha: str,
        queue: tuple[MergeTrainQueueEntry, ...],
        probe_ref: str,
        checkpoint: Callable[[int | None], None] | None = None,
    ) -> tuple[MergeTrainBatchHeldOutEntry, ...]:
        """Find queued heads that do not merge cleanly onto the heads ahead of them.

        GitHub cannot test-merge two pull requests without writing a ref, and a
        pull request's mergeability is computed only against its base. The probe
        resets a dedicated construction ref to the base and merges each head in
        queue order. A conflicting merge writes no commit, so the probe records
        that head and continues. The probe ref is deleted afterwards; the
        canonical train ref and pull request branches are never written.

        `probe_ref` belongs to one controller lease acquisition, so a pass that
        lost its lease cannot reset or delete another pass's probe. `checkpoint`
        runs before the ref is reset and before each merge, with the pull
        request about to merge; it renews the lease and raises once the lease
        is lost, which stops the probe and still deletes its own ref.
        """
        lineage = MergeTrainEffectLineage(
            repository=repository, base_branch=base_branch, batch_id="conflict-probe"
        )
        effect_executor = self.semantic_effect_executor
        if checkpoint is not None:
            checkpoint(None)
        merged_pull_request_numbers: list[int] = []
        held_out: list[MergeTrainBatchHeldOutEntry] = []
        probe_sha = base_sha
        try:
            # Inside the cleanup: a create that got no answer may still have written the ref.
            effect_executor.prepare_candidate_ref(
                CandidateRefPrepareEffect(
                    lineage=lineage, candidate_ref=probe_ref, base_sha=base_sha
                )
            )
            for queue_entry in queue:
                if checkpoint is not None:
                    checkpoint(queue_entry.number)
                try:
                    merge_outcome = effect_executor.merge_candidate_head(
                        CandidateHeadMergeEffect(
                            lineage=lineage,
                            candidate_ref=probe_ref,
                            rolling_parent_sha=probe_sha,
                            pull_request_number=queue_entry.number,
                            head_sha=queue_entry.head_sha,
                        )
                    )
                except MergeTrainGitHubCandidateEntryConflictError:
                    held_out.append(
                        MergeTrainBatchHeldOutEntry(
                            pull_request_number=queue_entry.number,
                            head_sha=queue_entry.head_sha,
                            conflicts_with=tuple(merged_pull_request_numbers),
                        )
                    )
                    continue
                merged_pull_request_numbers.append(queue_entry.number)
                probe_sha = merge_outcome.result_sha or probe_sha
        finally:
            try:
                effect_executor.delete_candidate_ref(
                    CandidateRefDeleteEffect(lineage=lineage, candidate_ref=probe_ref)
                )
            except MergeTrainGitHubError as error:
                # A leftover probe ref has no authority; nothing reads it again.
                logger.warning(
                    "Conflict probe ref cleanup failed for %s (GitHub status %s).",
                    probe_ref,
                    error.status_code,
                )
        return tuple(held_out)

    def observe_batch_candidate_checks(
        self, *, candidate: MergeTrainBatchCandidate
    ) -> MergeTrainBatchCandidate:
        repository_path = _repository_path(candidate.repository)
        candidate_sha = _required_value(
            candidate.candidate_sha,
            "Merge train batch candidate SHA is required before observing checks.",
        )
        check_status = _candidate_required_checks_status(
            transport=self.transport,
            repository_path=repository_path,
            base_branch=candidate.base_branch,
            encoded_head_sha=quote(candidate_sha, safe=""),
        )
        candidate_status = "ready_for_checks"
        if check_status == "pass":
            candidate_status = "passed"
        elif check_status == "fail":
            candidate_status = "failed"
        return _validated_model_update(
            candidate,
            required_checks_status=check_status,
            status=candidate_status,
        )

    def read_technical_checks(
        self,
        *,
        repository: str,
        base_branch: str,
        base_sha: str,
        head_sha: str,
        evaluated_at: str,
    ) -> "TenantAdmissionTechnicalChecks":
        from control_plane.tenant_admission_controller import (
            _required_technical_checks,
            read_technical_checks_for_requirements,
        )

        required_checks = _required_branch_checks(
            transport=self.transport,
            repository_path=_repository_path(repository),
            base_branch=base_branch,
        )
        _, normalized_checks = _required_technical_checks(
            {
                "strict": True,
                "checks": [{"context": name, "app_id": app_id} for name, app_id in required_checks],
                "contexts": [],
            }
        )
        # Strict freshness is a native train requirement, independent of
        # GitHub's optional strict flag. Admission separately proves exact or
        # recorded-rolling structural provenance against the current base.
        return read_technical_checks_for_requirements(
            transport=self.transport,
            merge_client=self,
            repository=repository,
            base_sha=base_sha,
            head_sha=head_sha,
            evaluated_at=evaluated_at,
            strict=True,
            required_checks=normalized_checks,
        )

    def verify_unlanded_batch(
        self, *, landing_plan: MergeTrainBatchLandingPlan, allow_changed_base: bool = False
    ) -> tuple[str, str]:
        """Prove that a changed-policy plan can be retired without losing a landing."""
        if any(entry.status not in {"planned", "merging"} for entry in landing_plan.entries):
            raise MergeTrainGitHubStaleHeadError(
                "Policy-change recovery requires a batch with no completed entries.",
                status_code=409,
            )
        repository_path = _repository_path(landing_plan.repository)
        first_entry = landing_plan.entries[0]
        expected_base = (
            first_entry.expected_base_sha,
            _required_value(
                first_entry.recorded_candidate_parent_tree_sha,
                "Policy-change recovery requires the recorded base tree.",
            ),
        )
        observed_base = _base_branch_identity(
            transport=self.transport,
            repository_path=repository_path,
            base_branch=landing_plan.base_branch,
        )
        if not allow_changed_base and observed_base != expected_base:
            raise MergeTrainGitHubStaleHeadError(
                "Policy-change recovery cannot prove the base is unchanged.", status_code=409
            )
        for entry in landing_plan.entries:
            head_sha, head_tree_sha = _git_commit_identity(
                transport=self.transport,
                repository_path=repository_path,
                commit_sha=entry.expected_head_sha,
            )
            if (head_sha, head_tree_sha) != (entry.expected_head_sha, entry.expected_head_tree_sha):
                raise MergeTrainGitHubStaleHeadError(
                    "Policy-change recovery cannot prove the recorded head tree.", status_code=409
                )
            self._validate_open_landing_pull_request(
                repository_path=repository_path,
                entry=entry,
                expected_base_ref=landing_plan.base_branch,
                expected_base_sha=observed_base[0],
                expected_base_tree_sha=observed_base[1],
            )
        if (
            _base_branch_identity(
                transport=self.transport,
                repository_path=repository_path,
                base_branch=landing_plan.base_branch,
            )
            != observed_base
        ):
            raise MergeTrainGitHubStaleHeadError(
                "Base branch moved during policy-change recovery.", status_code=409
            )
        return observed_base

    def land_batch_candidate(
        self,
        *,
        landing_plan: MergeTrainBatchLandingPlan,
        effect_executor: MergeTrainSemanticEffectExecutor | None = None,
        admission_guard: GuardedMergeAdmission | None = None,
        recorded_at: str = "",
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
        resolved_effect_executor = effect_executor or self.semantic_effect_executor
        if admission_guard is None:
            raise MergeAdmissionDeniedError(
                "Batch landing requires the guarded merge admission boundary."
            )
        if not recorded_at.strip():
            raise ValueError("Batch landing admission requires recorded_at.")
        if landing_plan.candidate_pull_request_number is not None:
            from control_plane.merge_train_batch_pull_request import land_protected_batch

            if not isinstance(resolved_effect_executor, LegacyMergeTrainEffectExecutor):
                raise MergeAdmissionDeniedError(
                    "Protected batch landing requires the service adapter."
                )
            return land_protected_batch(
                client=self,
                landing_plan=landing_plan,
                admission_guard=admission_guard,
                recorded_at=recorded_at,
                provider_checkpoint=provider_checkpoint,
                checkpoint=checkpoint,
            )
        repository_path = _repository_path(landing_plan.repository)
        expected_base_sha = landing_plan.entries[0].expected_base_sha
        expected_base_tree_sha = landing_plan.entries[0].recorded_candidate_parent_tree_sha
        landed_entries: list[MergeTrainBatchLandingEntry] = []

        def update_progress(
            progress_plan: MergeTrainBatchLandingPlan,
            progress_entry: MergeTrainBatchLandingEntry,
            phase: str,
        ) -> None:
            persisted_record = (
                checkpoint(progress_plan, progress_entry, phase) if checkpoint is not None else None
            )
            if persisted_record is not None:
                admission_guard.update_landing_plan_record(persisted_record)
            else:
                admission_guard.update_landing_plan(progress_plan)

        for entry_index, entry in enumerate(landing_plan.entries):
            current_base_sha, current_base_tree_sha = _base_branch_identity(
                transport=self.transport,
                repository_path=repository_path,
                base_branch=landing_plan.base_branch,
            )
            if not expected_base_tree_sha and current_base_sha == expected_base_sha:
                expected_base_tree_sha = current_base_tree_sha
            if (
                current_base_sha == expected_base_sha
                and current_base_tree_sha != expected_base_tree_sha
            ):
                raise MergeTrainGitHubStaleHeadError(
                    "Base branch tree does not match the rolling batch landing plan.",
                    status_code=409,
                )
            if entry.status == "merged":
                recovered_entry = self._already_merged_landing_entry(
                    repository_path=repository_path,
                    entry=entry,
                    expected_base_ref=landing_plan.base_branch,
                    expected_rolling_base_sha=expected_base_sha,
                    expected_rolling_base_tree_sha=expected_base_tree_sha,
                )
                if recovered_entry is None:
                    raise MergeTrainGitHubStaleHeadError(
                        "Persisted merged pull request is no longer merged.", status_code=409
                    )
                if entry.merge_commit_sha and (
                    recovered_entry.merge_commit_sha != entry.merge_commit_sha
                ):
                    raise MergeTrainGitHubStaleHeadError(
                        "Pull request merge commit does not match the batch landing plan.",
                        status_code=409,
                    )
                if not self.branch_contains_commit(
                    repository=landing_plan.repository,
                    branch_ref=landing_plan.base_branch,
                    commit_sha=recovered_entry.merge_commit_sha,
                ):
                    raise MergeTrainGitHubStaleHeadError(
                        "Merged pull request commit is not contained by the target branch.",
                        status_code=409,
                    )
                landed_entries.append(recovered_entry)
                admission_guard.reconcile_existing_landed(
                    entry=recovered_entry,
                    observed_base_sha=current_base_sha,
                    observed_base_tree_sha=current_base_tree_sha,
                    provider_effect_attempted=True,
                    observed_at=recorded_at,
                )
                expected_base_sha = recovered_entry.merge_commit_sha
                expected_base_tree_sha = recovered_entry.merge_commit_tree_sha
                progress_plan = _validated_model_update(
                    landing_plan,
                    entries=tuple(landed_entries) + landing_plan.entries[entry_index + 1 :],
                )
                update_progress(progress_plan, recovered_entry, "entry_merged")
                continue
            if _recorded_candidate_step_is_no_op(entry):
                if checkpoint is not None:
                    checkpoint(
                        _validated_model_update(
                            landing_plan,
                            entries=tuple(landed_entries) + landing_plan.entries[entry_index:],
                        ),
                        entry,
                        "merge_entry",
                    )
                skipped_entry = self._recover_no_op_landing_entry(
                    repository=landing_plan.repository,
                    repository_path=repository_path,
                    entry=entry,
                    expected_base_ref=landing_plan.base_branch,
                    expected_rolling_base_sha=expected_base_sha,
                    expected_rolling_base_tree_sha=expected_base_tree_sha,
                )
                no_op_admission = admission_guard.admit(
                    entry=entry,
                    observed_base_sha=current_base_sha,
                    observed_base_tree_sha=current_base_tree_sha,
                    observed_head_sha=entry.expected_head_sha,
                    observed_head_tree_sha=entry.expected_head_tree_sha,
                )
                admission_guard.record_landed(
                    admission=no_op_admission,
                    entry=skipped_entry,
                    observed_base_sha=current_base_sha,
                    observed_base_tree_sha=current_base_tree_sha,
                    base_contains_merge_commit=True,
                    provider_effect_attempted=False,
                    observed_at=recorded_at,
                )
                landed_entries.append(skipped_entry)
                progress_plan = _validated_model_update(
                    landing_plan,
                    entries=tuple(landed_entries) + landing_plan.entries[entry_index + 1 :],
                )
                update_progress(progress_plan, skipped_entry, "entry_skipped")
                continue
            if current_base_sha != expected_base_sha:
                already_merged_entry = self._already_merged_landing_entry(
                    repository_path=repository_path,
                    entry=entry,
                    expected_base_ref=landing_plan.base_branch,
                    expected_rolling_base_sha=expected_base_sha,
                    expected_rolling_base_tree_sha=expected_base_tree_sha,
                )
                if already_merged_entry is None:
                    raise MergeTrainGitHubStaleHeadError(
                        "Base branch moved outside the batch landing plan.", status_code=409
                    )
                if not self.branch_contains_commit(
                    repository=landing_plan.repository,
                    branch_ref=landing_plan.base_branch,
                    commit_sha=already_merged_entry.merge_commit_sha,
                ):
                    raise MergeTrainGitHubStaleHeadError(
                        "Merged pull request commit is not contained by the target branch.",
                        status_code=409,
                    )
                landed_entries.append(already_merged_entry)
                admission_guard.reconcile_existing_landed(
                    entry=already_merged_entry,
                    observed_base_sha=current_base_sha,
                    observed_base_tree_sha=current_base_tree_sha,
                    provider_effect_attempted=True,
                    observed_at=recorded_at,
                )
                expected_base_sha = already_merged_entry.merge_commit_sha
                expected_base_tree_sha = already_merged_entry.merge_commit_tree_sha
                progress_plan = _validated_model_update(
                    landing_plan,
                    entries=tuple(landed_entries) + landing_plan.entries[entry_index + 1 :],
                )
                update_progress(progress_plan, already_merged_entry, "entry_merged")
                continue
            landed_head_sha, landed_head_tree_sha = _git_commit_identity(
                transport=self.transport,
                repository_path=repository_path,
                commit_sha=entry.expected_head_sha,
            )
            if (
                entry.expected_head_tree_sha
                and landed_head_tree_sha != entry.expected_head_tree_sha
            ):
                raise MergeTrainGitHubStaleHeadError(
                    "Pull request head tree moved outside the batch landing plan.",
                    status_code=409,
                )
            head_behind_base = self._validate_open_landing_pull_request(
                repository_path=repository_path,
                entry=entry,
                expected_base_ref=landing_plan.base_branch,
                expected_base_sha=current_base_sha,
                expected_base_tree_sha=current_base_tree_sha,
                require_client_review=True,
            )
            admission_guard.reconcile_existing_no_effect(
                entry=entry,
                observed_base_sha=current_base_sha,
                observed_base_tree_sha=current_base_tree_sha,
                observed_head_sha=landed_head_sha,
                observed_head_tree_sha=landed_head_tree_sha,
                observed_pull_request_state="open",
                observed_at=recorded_at,
            )
            if head_behind_base:
                raise MergeAdmissionDeniedError(
                    f"PR #{entry.pull_request_number} is behind its base; refresh the branch and "
                    "wait for fresh checks before submitting it to the train again.",
                    reason_code="pull_request_head_behind_base",
                )
            if checkpoint is not None:
                checkpoint(
                    _validated_model_update(
                        landing_plan,
                        entries=tuple(landed_entries) + landing_plan.entries[entry_index:],
                    ),
                    entry,
                    "merge_entry",
                )
            admission = admission_guard.admit(
                entry=entry,
                observed_base_sha=current_base_sha,
                observed_base_tree_sha=current_base_tree_sha,
                observed_head_sha=landed_head_sha,
                observed_head_tree_sha=landed_head_tree_sha,
            )
            if provider_checkpoint is not None:
                provider_checkpoint(
                    _validated_model_update(
                        landing_plan,
                        entries=tuple(landed_entries) + landing_plan.entries[entry_index:],
                    ),
                    entry,
                )
            effect = PullRequestLandingEffect(
                lineage=MergeTrainEffectLineage(
                    repository=landing_plan.repository,
                    base_branch=landing_plan.base_branch,
                    batch_id=landing_plan.batch_id,
                    landing_plan_id=landing_plan.plan_id,
                ),
                pull_request_number=entry.pull_request_number,
                head_sha=entry.expected_head_sha,
                rolling_base_sha=current_base_sha,
                admission_id=admission.admission_id,
                merge_method=entry.merge_method,
            )
            try:
                merge_commit_sha = resolved_effect_executor.land_pull_request(effect)
            except Exception as error:
                admission_guard.record_provider_failure(
                    admission=admission,
                    error=error,
                    observed_at=recorded_at,
                )
                raise
            try:
                merge_commit_sha, merge_commit_tree_sha = _validated_landing_commit_identity(
                    transport=self.transport,
                    repository_path=repository_path,
                    commit_sha=merge_commit_sha,
                    expected_parent_sha=current_base_sha,
                    expected_head_sha=landed_head_sha,
                    merge_method=entry.merge_method,
                )
                observed_base_sha, observed_base_tree_sha = _base_branch_identity(
                    transport=self.transport,
                    repository_path=repository_path,
                    base_branch=landing_plan.base_branch,
                )
            except Exception as error:
                admission_guard.record_reconcile_required(
                    admission=admission,
                    reason="landing_evidence_incomplete",
                    message=str(error).strip(),
                    observed_at=recorded_at,
                )
                raise
            base_contains_merge_commit = observed_base_sha == merge_commit_sha or (
                self.branch_contains_commit(
                    repository=landing_plan.repository,
                    branch_ref=landing_plan.base_branch,
                    commit_sha=merge_commit_sha,
                )
            )
            if not base_contains_merge_commit:
                message = "Target base branch does not contain the provider merge commit."
                admission_guard.record_reconcile_required(
                    admission=admission,
                    reason="landing_evidence_contradicted",
                    message=message,
                    observed_at=recorded_at,
                )
                raise MergeTrainGitHubStaleHeadError(message, status_code=409)
            merged_entry = _validated_model_update(
                entry,
                status="merged",
                recorded_rolling_base_sha=current_base_sha,
                recorded_rolling_base_tree_sha=current_base_tree_sha,
                landed_head_sha=landed_head_sha,
                landed_head_tree_sha=landed_head_tree_sha,
                merge_commit_sha=merge_commit_sha,
                merge_commit_tree_sha=merge_commit_tree_sha,
            )
            landed_entries.append(merged_entry)
            admission_guard.record_landed(
                admission=admission,
                entry=merged_entry,
                observed_base_sha=observed_base_sha,
                observed_base_tree_sha=observed_base_tree_sha,
                base_contains_merge_commit=base_contains_merge_commit,
                provider_effect_attempted=True,
                observed_at=recorded_at,
            )
            expected_base_sha = merge_commit_sha
            expected_base_tree_sha = merge_commit_tree_sha
            progress_plan = _validated_model_update(
                landing_plan,
                entries=tuple(landed_entries) + landing_plan.entries[entry_index + 1 :],
            )
            update_progress(progress_plan, merged_entry, "entry_merged")
        current_base_sha = _base_branch_sha(
            transport=self.transport,
            repository_path=repository_path,
            base_branch=landing_plan.base_branch,
        )
        if current_base_sha != expected_base_sha:
            if not self.branch_contains_commit(
                repository=landing_plan.repository,
                branch_ref=landing_plan.base_branch,
                commit_sha=expected_base_sha,
            ):
                raise MergeTrainGitHubStaleHeadError(
                    "Base branch moved outside the batch landing plan.", status_code=409
                )
        return _validated_model_update(landing_plan, entries=tuple(landed_entries))

    def cleanup_batch_candidate_ref(
        self,
        *,
        landing_plan: MergeTrainBatchLandingPlan,
        effect_executor: MergeTrainSemanticEffectExecutor | None = None,
    ) -> bool:
        resolved_effect_executor = effect_executor or self.semantic_effect_executor
        return resolved_effect_executor.delete_candidate_ref(
            CandidateRefDeleteEffect(
                lineage=MergeTrainEffectLineage(
                    repository=landing_plan.repository,
                    base_branch=landing_plan.base_branch,
                    batch_id=landing_plan.batch_id,
                    landing_plan_id=landing_plan.plan_id,
                ),
                candidate_ref=landing_plan.candidate_ref,
            )
        )

    def candidate_ref_exists(self, *, repository: str, reference: str) -> bool:
        repository_path = _repository_path(repository)
        try:
            self.transport.request(
                method="GET",
                path=f"/repos/{repository_path}/git/ref/{_reference_path(reference)}",
            )
        except MergeTrainGitHubError as error:
            if error.status_code == 404:
                return False
            raise
        return True

    def pull_request_has_label(
        self, *, repository: str, pull_request_number: int, label: str
    ) -> bool:
        pull_request = self._pull_request_detail(
            repository=repository,
            pull_request_number=pull_request_number,
        )
        labels = pull_request.get("labels")
        if not isinstance(labels, list):
            return False
        normalized_label = _required_value(label, "GitHub label is required.")
        for label_payload in labels:
            if not isinstance(label_payload, dict):
                continue
            if str(label_payload.get("name") or "").strip() == normalized_label:
                return True
        return False

    def pull_request_is_closed(
        self, *, repository: str, pull_request_number: int, expected_head_sha: str
    ) -> bool:
        pull_request = self._pull_request_detail(
            repository=repository,
            pull_request_number=pull_request_number,
        )
        head = _json_object(pull_request.get("head"), "GitHub pull request head")
        head_sha = _required_text(head.get("sha"), "GitHub pull request head requires sha.")
        if head_sha != _required_value(
            expected_head_sha, "Expected pull request head SHA is required."
        ):
            raise MergeTrainGitHubStaleHeadError(
                "Stack child PR moved outside the stored collapse plan.",
                status_code=409,
            )
        return _pull_request_state(str(pull_request.get("state") or "")) == "closed"

    def find_pull_request_comment_url(
        self, *, repository: str, pull_request_number: int, body_contains: str
    ) -> str:
        repository_path = _repository_path(repository)
        payload = self.transport.request(
            method="GET",
            path=f"/repos/{repository_path}/issues/{pull_request_number}/comments",
        )
        if not isinstance(payload, list):
            raise MergeTrainGitHubError("GitHub issue comments response must be a JSON list.")
        needle = _required_value(body_contains, "GitHub comment match text is required.")
        for item in payload:
            if not isinstance(item, dict):
                continue
            body = str(item.get("body") or "")
            if needle in body:
                return str(item.get("html_url") or "").strip()
        return ""

    def pull_request_is_merged(
        self, *, repository: str, pull_request_number: int, expected_head_sha: str
    ) -> str:
        pull_request = self._pull_request_detail(
            repository=repository,
            pull_request_number=pull_request_number,
        )
        if pull_request.get("merged") is not True:
            return ""
        head = _json_object(pull_request.get("head"), "GitHub pull request head")
        head_sha = _required_text(head.get("sha"), "GitHub pull request head requires sha.")
        if head_sha != _required_value(
            expected_head_sha, "Expected pull request head SHA is required."
        ):
            raise MergeTrainGitHubStaleHeadError(
                "Pull request was merged with a different head SHA than the batch landing plan.",
                status_code=409,
            )
        return _required_text(
            pull_request.get("merge_commit_sha"),
            "Merged GitHub pull request requires merge_commit_sha.",
        )

    def branch_contains_commit(self, *, repository: str, branch_ref: str, commit_sha: str) -> bool:
        repository_path = _repository_path(repository)
        branch_name = quote(_required_value(branch_ref, "GitHub branch ref is required."), safe="")
        normalized_commit_sha = quote(
            _required_value(commit_sha, "GitHub commit SHA is required."),
            safe="",
        )
        payload = _json_object(
            self.transport.request(
                method="GET",
                path=(f"/repos/{repository_path}/compare/{normalized_commit_sha}...{branch_name}"),
            ),
            "GitHub compare response",
        )
        return str(payload.get("status") or "").strip() in {"ahead", "identical"}

    def branch_head_sha(self, *, repository: str, branch_ref: str) -> str:
        repository_path = _repository_path(repository)
        return _base_branch_sha(
            transport=self.transport,
            repository_path=repository_path,
            base_branch=_required_value(branch_ref, "GitHub branch ref is required."),
        )

    def _pull_request_detail(
        self, *, repository: str, pull_request_number: int
    ) -> dict[str, object]:
        repository_path = _repository_path(repository)
        return _json_object(
            self.transport.request(
                method="GET",
                path=f"/repos/{repository_path}/pulls/{pull_request_number}",
            ),
            "GitHub pull request detail response",
        )

    def _already_merged_landing_entry(
        self,
        *,
        repository_path: str,
        entry: MergeTrainBatchLandingEntry,
        expected_base_ref: str,
        expected_rolling_base_sha: str,
        expected_rolling_base_tree_sha: str,
    ) -> MergeTrainBatchLandingEntry | None:
        pull_request = _json_object(
            self.transport.request(
                method="GET",
                path=f"/repos/{repository_path}/pulls/{entry.pull_request_number}",
            ),
            "GitHub pull request detail response",
        )
        if _pull_request_state(str(pull_request.get("state") or "")) != "closed":
            return None
        if pull_request.get("merged") is not True:
            return None
        base = _json_object(pull_request.get("base"), "GitHub pull request base")
        base_ref = _required_text(base.get("ref"), "GitHub pull request base requires ref.")
        if base_ref != _required_value(
            expected_base_ref, "Expected pull request base ref is required."
        ):
            raise MergeTrainGitHubStaleHeadError(
                "Pull request was merged into a different base branch than the batch landing plan.",
                status_code=409,
            )
        head = _json_object(pull_request.get("head"), "GitHub pull request head")
        head_sha = _required_text(head.get("sha"), "GitHub pull request head requires sha.")
        if head_sha != entry.expected_head_sha:
            raise MergeTrainGitHubStaleHeadError(
                "Pull request was merged with a different head SHA than the batch landing plan.",
                status_code=409,
            )
        merge_commit_sha = _required_text(
            pull_request.get("merge_commit_sha"),
            "Merged GitHub pull request requires merge_commit_sha.",
        )
        landed_head_sha, landed_head_tree_sha = _git_commit_identity(
            transport=self.transport,
            repository_path=repository_path,
            commit_sha=head_sha,
        )
        if entry.expected_head_tree_sha and landed_head_tree_sha != entry.expected_head_tree_sha:
            raise MergeTrainGitHubStaleHeadError(
                "Merged pull request head tree does not match the batch landing plan.",
                status_code=409,
            )
        merge_commit_sha, merge_commit_tree_sha = _validated_landing_commit_identity(
            transport=self.transport,
            repository_path=repository_path,
            commit_sha=merge_commit_sha,
            expected_parent_sha=expected_rolling_base_sha,
            expected_head_sha=landed_head_sha,
            merge_method=entry.merge_method,
        )
        rolling_base_sha, rolling_base_tree_sha = _git_commit_identity(
            transport=self.transport,
            repository_path=repository_path,
            commit_sha=expected_rolling_base_sha,
        )
        if (
            expected_rolling_base_tree_sha
            and rolling_base_tree_sha != expected_rolling_base_tree_sha
        ):
            raise MergeTrainGitHubStaleHeadError(
                "Merged pull request rolling base tree does not match the batch landing plan.",
                status_code=409,
            )
        return _validated_model_update(
            entry,
            status="merged",
            recorded_rolling_base_sha=rolling_base_sha,
            recorded_rolling_base_tree_sha=rolling_base_tree_sha,
            landed_head_sha=landed_head_sha,
            landed_head_tree_sha=landed_head_tree_sha,
            merge_commit_sha=merge_commit_sha,
            merge_commit_tree_sha=merge_commit_tree_sha,
        )

    def _validate_no_op_landing_pull_request(
        self,
        *,
        repository_path: str,
        entry: MergeTrainBatchLandingEntry,
        expected_base_ref: str,
    ) -> None:
        pull_request = _json_object(
            self.transport.request(
                method="GET",
                path=f"/repos/{repository_path}/pulls/{entry.pull_request_number}",
            ),
            "GitHub pull request detail response",
        )
        head = _json_object(pull_request.get("head"), "GitHub pull request head")
        if _required_text(head.get("sha"), "GitHub pull request head requires sha.") != (
            entry.expected_head_sha
        ):
            raise MergeTrainGitHubStaleHeadError(
                "No-op pull request head moved outside the batch landing plan.",
                status_code=409,
            )
        base = _json_object(pull_request.get("base"), "GitHub pull request base")
        if _required_text(base.get("ref"), "GitHub pull request base requires ref.") != (
            _required_value(expected_base_ref, "Expected pull request base ref is required.")
        ):
            raise MergeTrainGitHubStaleHeadError(
                "No-op pull request targets a different base branch than the batch landing plan.",
                status_code=409,
            )

    def _recover_no_op_landing_entry(
        self,
        *,
        repository: str,
        repository_path: str,
        entry: MergeTrainBatchLandingEntry,
        expected_base_ref: str,
        expected_rolling_base_sha: str,
        expected_rolling_base_tree_sha: str,
    ) -> MergeTrainBatchLandingEntry:
        self._validate_no_op_landing_pull_request(
            repository_path=repository_path,
            entry=entry,
            expected_base_ref=expected_base_ref,
        )
        landed_head_sha, landed_head_tree_sha = _git_commit_identity(
            transport=self.transport,
            repository_path=repository_path,
            commit_sha=entry.expected_head_sha,
        )
        if entry.expected_head_tree_sha and landed_head_tree_sha != entry.expected_head_tree_sha:
            raise MergeTrainGitHubStaleHeadError(
                "No-op pull request head tree does not match the batch landing plan.",
                status_code=409,
            )
        rolling_base_sha, rolling_base_tree_sha = _git_commit_identity(
            transport=self.transport,
            repository_path=repository_path,
            commit_sha=expected_rolling_base_sha,
        )
        if (
            expected_rolling_base_tree_sha
            and rolling_base_tree_sha != expected_rolling_base_tree_sha
        ):
            raise MergeTrainGitHubStaleHeadError(
                "No-op rolling base tree does not match the batch landing plan.",
                status_code=409,
            )
        for commit_sha, label in (
            (rolling_base_sha, "rolling base"),
            (landed_head_sha, "head"),
        ):
            if not self.branch_contains_commit(
                repository=repository,
                branch_ref=expected_base_ref,
                commit_sha=commit_sha,
            ):
                raise MergeTrainGitHubStaleHeadError(
                    f"Recorded candidate no-op {label} is not contained by the target branch.",
                    status_code=409,
                )
        recovered_entry = _validated_model_update(
            entry,
            status="skipped",
            recorded_rolling_base_sha=rolling_base_sha,
            recorded_rolling_base_tree_sha=rolling_base_tree_sha,
            landed_head_sha=landed_head_sha,
            landed_head_tree_sha=landed_head_tree_sha,
            merge_commit_sha=rolling_base_sha,
            merge_commit_tree_sha=rolling_base_tree_sha,
        )
        if entry.status == "skipped" and entry != recovered_entry:
            raise MergeTrainGitHubStaleHeadError(
                "Persisted no-op landing evidence does not match the batch landing plan.",
                status_code=409,
            )
        return recovered_entry

    def _validate_open_landing_pull_request(
        self,
        *,
        repository_path: str,
        entry: MergeTrainBatchLandingEntry,
        expected_base_ref: str,
        expected_base_sha: str,
        expected_base_tree_sha: str,
        require_client_review: bool = False,
    ) -> bool:
        pull_request = _json_object(
            self.transport.request(
                method="GET",
                path=f"/repos/{repository_path}/pulls/{entry.pull_request_number}",
            ),
            "GitHub pull request detail response",
        )
        if _pull_request_state(str(pull_request.get("state") or "")) != "open":
            raise MergeTrainGitHubStaleHeadError(
                "Pull request is no longer open for batch landing.", status_code=409
            )
        head = _json_object(pull_request.get("head"), "GitHub pull request head")
        head_sha = _required_text(head.get("sha"), "GitHub pull request head requires sha.")
        if head_sha != entry.expected_head_sha:
            raise MergeTrainGitHubStaleHeadError(
                "Pull request head moved outside the batch landing plan.", status_code=409
            )
        base = _json_object(pull_request.get("base"), "GitHub pull request base")
        base_ref = _required_text(base.get("ref"), "GitHub pull request base requires ref.")
        base_sha = _required_text(base.get("sha"), "GitHub pull request base requires sha.")
        if base_ref != _required_value(
            expected_base_ref, "Expected pull request base ref is required."
        ):
            raise MergeTrainGitHubStaleHeadError(
                "Pull request targets a different base branch than the batch landing plan.",
                status_code=409,
            )
        if base_sha != _required_value(
            expected_base_sha, "Expected pull request base SHA is required."
        ):
            # PR detail may describe an older base projection. Confirm the
            # actual branch identity once; never treat that projection as a
            # replacement for the independently verified rolling branch.
            confirmed_sha, confirmed_tree_sha = _base_branch_identity(
                transport=self.transport,
                repository_path=repository_path,
                base_branch=expected_base_ref,
            )
            if (confirmed_sha, confirmed_tree_sha) != (
                expected_base_sha,
                expected_base_tree_sha,
            ):
                raise MergeTrainGitHubStaleHeadError(
                    "Target base branch moved outside the batch landing plan.", status_code=409
                )
        review_reader = GitHubMergeTrainSnapshotReader(
            transport=self.transport, branch_refresh_store=self._branch_refresh_store
        )
        if require_client_review and review_reader.client_review_required(
            labels=_labels(pull_request.get("labels")), repository=repository_path
        ):
            review_status = _owner_review_status(
                _list_commit_statuses(
                    transport=self.transport,
                    repository_path=repository_path,
                    encoded_head_sha=quote(head_sha, safe=""),
                )
            )
            if review_status != "pass":
                raise MergeAdmissionDeniedError(
                    f"Pull request #{entry.pull_request_number} requires current-head Client review.",
                    reason_code="client_review_not_ready",
                )
        return pull_request.get("mergeable_state") == "behind"

    def ensure_batch_pull_request(self, *, candidate: MergeTrainBatchCandidate) -> int:
        from control_plane.merge_train_batch_pull_request import ensure_batch_pull_request

        if self._effect_executor is not None and not isinstance(
            self._effect_executor, LegacyMergeTrainEffectExecutor
        ):
            raise MergeAdmissionDeniedError(
                "Protected batch PR creation requires the service adapter."
            )
        return ensure_batch_pull_request(client=self, candidate=candidate)

    def close_batch_pull_request(self, *, candidate: MergeTrainBatchCandidate) -> None:
        from control_plane.merge_train_batch_pull_request import close_batch_pull_request

        if self._effect_executor is not None and not isinstance(
            self._effect_executor, LegacyMergeTrainEffectExecutor
        ):
            raise MergeAdmissionDeniedError("Batch PR retirement requires the service adapter.")
        close_batch_pull_request(client=self, candidate=candidate)

    def add_pull_request_label(
        self, *, repository: str, pull_request_number: int, label: str
    ) -> None:
        repository_path = _repository_path(repository)
        normalized_label = _required_value(label, "GitHub label is required.")
        self.transport.request(
            method="POST",
            path=f"/repos/{repository_path}/issues/{pull_request_number}/labels",
            body={"labels": [normalized_label]},
        )

    def update_pull_request_branch(
        self, *, repository: str, pull_request_number: int, expected_head_sha: str
    ) -> None:
        repository_path = _repository_path(repository)
        requested_at = datetime.now(timezone.utc)
        self.transport.request(
            method="PUT",
            path=f"/repos/{repository_path}/pulls/{pull_request_number}/update-branch",
            body={
                "expected_head_sha": _required_value(
                    expected_head_sha, "Expected pull request head SHA is required."
                )
            },
        )
        if self._branch_refresh_recorder is None:
            return
        try:
            result = self._read_branch_refresh_result(
                repository_path=repository_path,
                pull_request_number=pull_request_number,
                expected_head_sha=expected_head_sha.strip().lower(),
            )
            if result is None:
                logger.info(
                    "The merge train refreshed a pull request but did not see its merge commit.",
                    extra={"repository": repository, "pull_request_number": pull_request_number},
                )
                return
            result_head_sha, merged_base_sha = result
            self._branch_refresh_recorder(
                repository=repository,
                pull_request_number=pull_request_number,
                expected_head_sha=expected_head_sha,
                result_head_sha=result_head_sha,
                merged_base_sha=merged_base_sha,
                requested_at=requested_at,
            )
        except Exception:
            # The refresh happened; only carrying a Client's acceptance across it is lost.
            logger.warning(
                "The merge train refreshed a pull request but could not record it.",
                exc_info=True,
                extra={"repository": repository, "pull_request_number": pull_request_number},
            )

    def _read_branch_refresh_result(
        self, *, repository_path: str, pull_request_number: int, expected_head_sha: str
    ) -> tuple[str, str] | None:
        """The merge commit this refresh made and the base commit it merged, or None.

        The new head counts only when it is a merge whose first parent is the head
        the train refreshed from; anything else moved the pull request, not the train.
        """
        for attempt in range(BRANCH_REFRESH_READBACK_ATTEMPTS):
            if attempt:
                self._wait(BRANCH_REFRESH_READBACK_INTERVAL_SECONDS)
            pull_request = _json_object(
                self.transport.request(
                    method="GET", path=f"/repos/{repository_path}/pulls/{pull_request_number}"
                ),
                "GitHub pull request response",
            )
            head = pull_request.get("head")
            head_sha = str(head.get("sha") or "").strip().lower() if isinstance(head, dict) else ""
            if not head_sha or head_sha == expected_head_sha:
                continue
            commit = _json_object(
                self.transport.request(
                    method="GET",
                    path=f"/repos/{repository_path}/git/commits/{quote(head_sha, safe='')}",
                ),
                "GitHub commit response",
            )
            parents = commit.get("parents")
            parent_shas = [
                str(parent.get("sha") or "").strip().lower()
                for parent in (parents if isinstance(parents, list) else ())
                if isinstance(parent, dict)
            ]
            if len(parent_shas) != 2 or parent_shas[0] != expected_head_sha or not parent_shas[1]:
                return None
            return head_sha, parent_shas[1]
        return None

    def require_current_client_review(
        self, *, repository: str, pull_request_number: int, head_sha: str
    ) -> None:
        """Re-read Client review just before a direct merge; a changed decision is stale."""
        repository_path = _repository_path(repository)
        pull_request = _json_object(
            self.transport.request(
                method="GET", path=f"/repos/{repository_path}/pulls/{pull_request_number}"
            ),
            "GitHub pull request response",
        )
        review_reader = GitHubMergeTrainSnapshotReader(
            transport=self.transport, branch_refresh_store=self._branch_refresh_store
        )
        if not review_reader.client_review_required(
            labels=_labels(pull_request.get("labels")), repository=repository_path
        ):
            return
        review_status = _owner_review_status(
            _list_commit_statuses(
                transport=self.transport,
                repository_path=repository_path,
                encoded_head_sha=quote(head_sha, safe=""),
            )
        )
        if review_status != "pass":
            raise MergeTrainGitHubStaleHeadError(
                f"Pull request #{pull_request_number} no longer has current-head Client review.",
                status_code=409,
            )

    def merge_pull_request(
        self,
        *,
        repository: str,
        pull_request_number: int,
        head_sha: str,
        merge_method: MergeTrainMergeMethod,
    ) -> str:
        repository_path = _repository_path(repository)
        expected_head_sha = _required_value(head_sha, "Pull request head SHA is required.")
        try:
            payload = self.transport.request(
                method="PUT",
                path=f"/repos/{repository_path}/pulls/{pull_request_number}/merge",
                body={"sha": expected_head_sha, "merge_method": merge_method},
            )
        except MergeTrainGitHubError as error:
            if error.status_code != 405:
                raise
            observed_merge_state = ""
            try:
                observed = self.transport.request(
                    method="GET",
                    path=f"/repos/{repository_path}/pulls/{pull_request_number}",
                )
            except Exception:  # noqa: BLE001 - diagnosis cannot erase the confirmed merge refusal
                observed = None
            if isinstance(observed, dict):
                head = observed.get("head")
                merge_state = observed.get("mergeable_state")
                if (
                    observed.get("number") == pull_request_number
                    and observed.get("state") == "open"
                    and isinstance(head, dict)
                    and head.get("sha") == expected_head_sha
                    and isinstance(merge_state, str)
                ):
                    observed_merge_state = merge_state.strip().lower()
            raise MergeTrainGitHubMergeRejectedError(
                pull_request_number=pull_request_number,
                observed_merge_state=observed_merge_state,
            ) from error
        if not isinstance(payload, dict):
            raise MergeTrainGitHubError(
                "GitHub merge response must be a JSON object.", status_code=None
            )
        merge_commit_sha = str(payload.get("sha") or "").strip()
        if not merge_commit_sha:
            raise MergeTrainGitHubError(
                "GitHub merge response did not include a merge commit SHA.", status_code=None
            )
        return merge_commit_sha

    def comment_pull_request(self, *, repository: str, pull_request_number: int, body: str) -> str:
        repository_path = _repository_path(repository)
        payload = self.transport.request(
            method="POST",
            path=f"/repos/{repository_path}/issues/{pull_request_number}/comments",
            body={"body": _required_value(body, "GitHub pull request comment body is required.")},
        )
        if not isinstance(payload, dict):
            raise MergeTrainGitHubError("GitHub comment response must be a JSON object.")
        return str(payload.get("html_url") or "").strip()

    def close_pull_request(
        self, *, repository: str, pull_request_number: int, expected_head_sha: str
    ) -> None:
        repository_path = _repository_path(repository)
        pull_request_path = f"/repos/{repository_path}/pulls/{pull_request_number}"
        expected_sha = _required_value(
            expected_head_sha, "Expected pull request head SHA is required."
        )
        current_head_sha = self._pull_request_head_sha(pull_request_path=pull_request_path)
        if current_head_sha != _required_value(
            expected_head_sha, "Expected pull request head SHA is required."
        ):
            raise MergeTrainGitHubStaleHeadError(
                "Stack child PR moved outside the stored collapse plan.",
                status_code=409,
            )
        closed_pull_request = _json_object(
            self.transport.request(
                method="PATCH",
                path=pull_request_path,
                body={"state": "closed"},
            ),
            "GitHub close pull request response",
        )
        closed_head = _json_object(closed_pull_request.get("head"), "GitHub pull request head")
        closed_head_sha = _required_text(
            closed_head.get("sha"), "GitHub pull request head requires sha."
        )
        if closed_head_sha != expected_sha:
            raise MergeTrainGitHubStaleHeadError(
                "Stack child PR moved while Launchplane was closing it.",
                status_code=409,
            )
        if _pull_request_state(str(closed_pull_request.get("state") or "")) != "closed":
            raise MergeTrainGitHubStaleHeadError(
                "Stack child PR did not remain closed.",
                status_code=409,
            )

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
        repository_path = _repository_path(repository)
        normalized_parent_ref = _required_value(
            parent_head_ref, "Stack collapse parent head ref is required."
        )
        if normalized_parent_ref == _required_value(
            protected_base_ref, "Stack collapse protected base ref is required."
        ):
            raise MergeTrainGitHubError("Stack collapse cannot mutate a protected base branch.")
        current_parent_sha = _base_branch_sha(
            transport=self.transport,
            repository_path=repository_path,
            base_branch=normalized_parent_ref,
        )
        expected_sha = _required_value(
            expected_parent_head_sha, "Stack collapse expected parent SHA is required."
        )
        if current_parent_sha != expected_sha:
            raise MergeTrainGitHubStaleHeadError(
                "Stack collapse parent branch moved outside the stored plan.",
                status_code=409,
            )
        payload = self.transport.request(
            method="POST",
            path=f"/repos/{repository_path}/merges",
            body={
                "base": normalized_parent_ref,
                "head": _required_value(
                    child_head_sha, "Stack collapse child head SHA is required."
                ),
                "commit_message": _stack_collapse_commit_message(
                    collapse_id=collapse_id,
                    child_pull_request_number=child_pull_request_number,
                    parent_pull_request_number=parent_pull_request_number,
                ),
            },
        )
        if not isinstance(payload, dict):
            raise MergeTrainGitHubError("GitHub stack merge response must be a JSON object.")
        return _required_text(payload.get("sha"), "GitHub stack merge response requires sha.")

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
        repository_path = _repository_path(repository)
        current_parent_sha = _base_branch_sha(
            transport=self.transport,
            repository_path=repository_path,
            base_branch=_required_value(
                parent_head_ref,
                "Stack collapse parent head ref is required.",
            ),
        )
        expected_parent_sha = _required_value(
            expected_parent_head_sha,
            "Stack collapse expected parent SHA is required.",
        )
        if current_parent_sha == expected_parent_sha:
            return ""
        payload = _json_object(
            self.transport.request(
                method="GET",
                path=f"/repos/{repository_path}/commits/{quote(current_parent_sha, safe='')}",
            ),
            "GitHub commit response",
        )
        commit = _json_object(payload.get("commit"), "GitHub commit detail")
        message = str(commit.get("message") or "").strip()
        parents = payload.get("parents")
        parent_shas = (
            {str(parent.get("sha") or "").strip() for parent in parents if isinstance(parent, dict)}
            if isinstance(parents, list)
            else set()
        )
        expected_message = _stack_collapse_commit_message(
            collapse_id=collapse_id,
            child_pull_request_number=child_pull_request_number,
            parent_pull_request_number=parent_pull_request_number,
        )
        if message == expected_message and parent_shas == {
            expected_parent_sha,
            _required_value(child_head_sha, "Stack collapse child head SHA is required."),
        }:
            return current_parent_sha
        raise MergeTrainGitHubStaleHeadError(
            "Stack collapse parent branch moved outside the stored plan.",
            status_code=409,
        )

    def _create_or_reset_reference(self, *, repository_path: str, reference: str, sha: str) -> None:
        normalized_sha = _required_value(sha, "GitHub reference SHA is required.")
        try:
            self.transport.request(
                method="POST",
                path=f"/repos/{repository_path}/git/refs",
                body={"ref": reference, "sha": normalized_sha},
            )
        except MergeTrainGitHubError as error:
            if error.status_code not in {409, 422}:
                raise
            reference_path = _reference_path(reference)
            self.transport.request(
                method="PATCH",
                path=f"/repos/{repository_path}/git/refs/{reference_path}",
                body={"sha": normalized_sha, "force": True},
            )

    def _delete_reference_if_present(self, *, repository_path: str, reference: str) -> bool:
        try:
            self.transport.request(
                method="DELETE",
                path=f"/repos/{repository_path}/git/refs/{_reference_path(reference)}",
            )
        except MergeTrainGitHubError as error:
            if error.status_code == 404:
                return False
            raise
        return True

    def _pull_request_head_sha(self, *, pull_request_path: str) -> str:
        pull_request = _json_object(
            self.transport.request(method="GET", path=pull_request_path),
            "GitHub pull request detail response",
        )
        head = _json_object(pull_request.get("head"), "GitHub pull request head")
        return _required_text(head.get("sha"), "GitHub pull request head requires sha.")


class LegacyMergeTrainEffectExecutor:
    """Map provider-neutral merge-train effects to the existing GitHub client."""

    def __init__(self, *, client: GitHubMergeTrainClient) -> None:
        self.client = client

    def prepare_candidate_ref(self, effect: CandidateRefPrepareEffect) -> None:
        self.client._create_or_reset_reference(
            repository_path=_repository_path(effect.lineage.repository),
            reference=effect.candidate_ref,
            sha=effect.base_sha,
        )

    def merge_candidate_head(self, effect: CandidateHeadMergeEffect) -> CandidateHeadMergeOutcome:
        repository_path = _repository_path(effect.lineage.repository)
        try:
            payload = self.client.transport.request(
                method="POST",
                path=f"/repos/{repository_path}/merges",
                body={
                    "base": _branch_name_from_ref(effect.candidate_ref),
                    "head": effect.head_sha,
                    "commit_message": (
                        f"Launchplane merge train {effect.lineage.batch_id}: "
                        f"merge PR #{effect.pull_request_number}"
                    ),
                },
            )
        except MergeTrainGitHubStaleHeadError as error:
            # The merges API answers 409 only for a merge conflict.
            raise MergeTrainGitHubCandidateEntryConflictError(
                pull_request_number=effect.pull_request_number,
                head_sha=effect.head_sha,
            ) from error
        if payload is None:
            return CandidateHeadMergeOutcome(result_sha=None)
        if not isinstance(payload, dict):
            raise MergeTrainGitHubError("GitHub merge response must be a JSON object.")
        tree = payload.get("tree")
        parents = payload.get("parents")
        result_tree_sha = str(tree.get("sha") or "").strip() if isinstance(tree, dict) else ""
        parent_shas = (
            tuple(
                str(parent.get("sha") or "").strip()
                for parent in parents
                if isinstance(parent, dict) and str(parent.get("sha") or "").strip()
            )
            if isinstance(parents, list)
            else ()
        )
        return CandidateHeadMergeOutcome(
            result_sha=_required_text(payload.get("sha"), "GitHub merge response requires sha."),
            result_tree_sha=result_tree_sha or None,
            parent_shas=parent_shas,
        )

    def refresh_pull_request_head(self, effect: PullRequestHeadRefreshEffect) -> None:
        self.client.update_pull_request_branch(
            repository=effect.lineage.repository,
            pull_request_number=effect.pull_request_number,
            expected_head_sha=effect.expected_head_sha,
        )

    def merge_stack_child(self, effect: StackChildMergeEffect) -> str:
        return self.client.merge_stack_child_into_parent(
            repository=effect.lineage.repository,
            child_head_sha=effect.child_head_sha,
            expected_parent_head_sha=effect.expected_parent_head_sha,
            parent_head_ref=effect.parent_head_ref,
            protected_base_ref=effect.protected_base_ref,
            collapse_id=effect.lineage.collapse_id,
            child_pull_request_number=effect.child_pull_request_number,
            parent_pull_request_number=effect.parent_pull_request_number,
        )

    def land_pull_request(self, effect: PullRequestLandingEffect) -> str:
        return self.client.merge_pull_request(
            repository=effect.lineage.repository,
            pull_request_number=effect.pull_request_number,
            head_sha=effect.head_sha,
            merge_method=effect.merge_method,
        )

    def comment_stack_child(self, effect: StackChildCommentEffect) -> str:
        return self.client.comment_pull_request(
            repository=effect.lineage.repository,
            pull_request_number=effect.pull_request_number,
            body=effect.body,
        )

    def label_stack_child(self, effect: StackChildLabelEffect) -> None:
        self.client.add_pull_request_label(
            repository=effect.lineage.repository,
            pull_request_number=effect.pull_request_number,
            label=effect.label,
        )

    def close_stack_child(self, effect: StackChildCloseEffect) -> None:
        self.client.close_pull_request(
            repository=effect.lineage.repository,
            pull_request_number=effect.pull_request_number,
            expected_head_sha=effect.expected_head_sha,
        )

    def delete_candidate_ref(self, effect: CandidateRefDeleteEffect) -> bool:
        return self.client._delete_reference_if_present(
            repository_path=_repository_path(effect.lineage.repository),
            reference=effect.candidate_ref,
        )


class GitHubMergeTrainSnapshotReader:
    def __init__(
        self,
        *,
        transport: MergeTrainGitHubTransport,
        branch_refresh_store: MergeTrainBranchRefreshReadStore | None = None,
    ) -> None:
        self.transport = transport
        self._branch_refresh_store = branch_refresh_store
        self._actor_roles: dict[tuple[str, str], str] = {}

    def read_merge_train_snapshot(
        self, *, repository: str, base_branch: str
    ) -> MergeTrainDryRunSnapshot:
        repository_path = _repository_path(repository)
        normalized_base_branch = _required_value(
            base_branch, "GitHub pull request base branch is required."
        )
        base_sha = self._base_branch_sha(
            repository_path=repository_path, base_branch=normalized_base_branch
        )
        open_pull_requests = self._list_open_pull_requests(repository_path=repository_path)
        relevant_pull_requests = _base_rooted_pull_requests(
            pull_requests=open_pull_requests,
            repository=repository,
            base_branch=normalized_base_branch,
        )
        pull_requests = self._with_review_conversations(
            repository_path=repository_path,
            base_branch=normalized_base_branch,
            pull_requests=tuple(
                self._pull_request_snapshot(
                    repository=repository,
                    repository_path=repository_path,
                    pull_request=pull_request,
                )
                for pull_request in relevant_pull_requests
            ),
        )
        return MergeTrainDryRunSnapshot(
            repository=repository,
            base_branch=normalized_base_branch,
            base_sha=base_sha,
            pull_requests=pull_requests,
        )

    def read_pull_request_snapshot(
        self, *, repository: str, pull_request_number: int
    ) -> MergeTrainPullRequestSnapshot:
        repository_path = _repository_path(repository)
        snapshot = self._pull_request_snapshot(
            repository=repository,
            repository_path=repository_path,
            pull_request={"number": pull_request_number},
        )
        if not snapshot.base_ref:
            return snapshot
        (snapshot,) = self._with_review_conversations(
            repository_path=repository_path,
            base_branch=snapshot.base_ref,
            pull_requests=(snapshot,),
        )
        return snapshot

    def _with_review_conversations(
        self,
        *,
        repository_path: str,
        base_branch: str,
        pull_requests: tuple[MergeTrainPullRequestSnapshot, ...],
    ) -> tuple[MergeTrainPullRequestSnapshot, ...]:
        """Record unresolved threads that GitHub would refuse a merge over.

        GitHub refuses a merge into a base that requires conversation
        resolution while any review thread is unresolved, so such a pull
        request is not ready for the train. Closed and draft pull requests are
        already ineligible and are not read.
        """
        if not any(_conversations_can_block(pull_request) for pull_request in pull_requests):
            return pull_requests
        rule = _conversation_resolution_rule(
            transport=self.transport, repository_path=repository_path, base_branch=base_branch
        )
        if rule == "not_required":
            return pull_requests
        return tuple(
            pull_request.model_copy(
                update={
                    "review_conversations": _review_conversations(
                        transport=self.transport,
                        repository_path=repository_path,
                        pull_request_number=pull_request.number,
                        rule=rule,
                    )
                }
            )
            if _conversations_can_block(pull_request)
            else pull_request
            for pull_request in pull_requests
        )

    def _base_branch_sha(self, *, repository_path: str, base_branch: str) -> str:
        return _base_branch_sha(
            transport=self.transport,
            repository_path=repository_path,
            base_branch=base_branch,
        )

    def _list_open_pull_requests(self, *, repository_path: str) -> tuple[dict[str, object], ...]:
        pull_requests: list[dict[str, object]] = []
        page = 1
        while True:
            query = urlencode(
                {
                    "state": "open",
                    "sort": "created",
                    "direction": "asc",
                    "per_page": "100",
                    "page": str(page),
                }
            )
            payload = self.transport.request(
                method="GET", path=f"/repos/{repository_path}/pulls?{query}"
            )
            if not isinstance(payload, list):
                raise MergeTrainGitHubError(
                    "GitHub pull request list response must be a JSON array."
                )
            page_pull_requests = [
                _json_object(item, "GitHub pull request entry") for item in payload
            ]
            pull_requests.extend(page_pull_requests)
            if len(page_pull_requests) < 100:
                return tuple(pull_requests)
            page += 1

    def _pull_request_snapshot(
        self, *, repository: str, repository_path: str, pull_request: dict[str, object]
    ) -> MergeTrainPullRequestSnapshot:
        pull_request_number = _required_int(
            pull_request.get("number"), "GitHub pull request entry requires number."
        )
        detail = _json_object(
            self.transport.request(
                method="GET", path=f"/repos/{repository_path}/pulls/{pull_request_number}"
            ),
            "GitHub pull request detail response",
        )
        source = pull_request | detail
        head = _json_object(source.get("head"), "GitHub pull request head")
        base = _json_object(source.get("base"), "GitHub pull request base")
        head_sha = _required_text(head.get("sha"), "GitHub pull request head requires sha.")
        head_repository = _repository_full_name(head.get("repo"), "GitHub pull request head repo")
        base_repository = _repository_full_name(base.get("repo"), "GitHub pull request base repo")
        user = _json_object(source.get("user"), "GitHub pull request user")
        actor_id = _required_int(user.get("id"), "GitHub pull request user requires id.")
        actor_role = self._actor_role_for_pull_request(
            repository_path=repository_path,
            username=_required_text(user.get("login"), "GitHub pull request user requires login."),
            author_association=str(source.get("author_association") or ""),
        )
        labels = _labels(source.get("labels"))
        label_actors = (
            self._label_actors(
                repository_path=repository_path,
                pull_request_number=pull_request_number,
                labels=labels,
            )
            if labels
            else ()
        )
        dependency_update_class = (
            self._safe_dependency_update_class(
                repository_path=repository_path,
                pull_request_number=pull_request_number,
                author_id=actor_id,
                head_sha=head_sha,
                base_branch=str(base.get("ref") or ""),
                base_sha=str(base.get("sha") or ""),
            )
            if str(user.get("type") or "") == "Bot"
            else None
        )
        owner_review_required = self.client_review_required(
            labels=labels, repository=base_repository
        )
        return MergeTrainPullRequestSnapshot(
            owner_review_required=owner_review_required,
            number=pull_request_number,
            url=str(source.get("html_url") or "").strip(),
            title=str(source.get("title") or "").strip(),
            state=_pull_request_state(str(source.get("state") or "")),
            is_draft=bool(source.get("draft")),
            created_at=_required_text(
                source.get("created_at"), "GitHub pull request entry requires created_at."
            ),
            labels=labels,
            label_actors=label_actors,
            actor_id=actor_id,
            actor_role=actor_role,
            head_sha=head_sha,
            head_ref=_required_text(head.get("ref"), "GitHub pull request head requires ref."),
            head_repository=head_repository,
            base_sha=str(base.get("sha") or "").strip(),
            base_ref=_required_text(base.get("ref"), "GitHub pull request base requires ref."),
            base_repository=base_repository,
            mergeable=_mergeable_state(source),
            required_checks_status=self._required_checks_status(
                repository_path=repository_path,
                head_sha=head_sha,
                owner_review_required=owner_review_required,
            ),
            branch_update_required=_branch_update_required(source),
            dependency_update_class=dependency_update_class,
        )

    def _safe_dependency_update_class(
        self,
        *,
        repository_path: str,
        pull_request_number: int,
        author_id: int,
        head_sha: str,
        base_branch: str,
        base_sha: str,
    ) -> DependencyUpdateClass:
        # A read failure only withholds label-free admission; it must not stop the train.
        try:
            return self._dependency_update_class(
                repository_path=repository_path,
                pull_request_number=pull_request_number,
                author_id=author_id,
                head_sha=head_sha,
                base_branch=base_branch,
                base_sha=base_sha,
            )
        except MergeTrainGitHubError:
            return "needs_review"

    def _dependency_update_class(
        self,
        *,
        repository_path: str,
        pull_request_number: int,
        author_id: int,
        head_sha: str,
        base_branch: str,
        base_sha: str,
    ) -> DependencyUpdateClass:
        payload = self.transport.request(
            method="GET",
            path=f"/repos/{repository_path}/pulls/{pull_request_number}/commits?per_page=100",
        )
        if not isinstance(payload, list) or not payload or len(payload) >= 100:
            return "needs_review"
        commits = [_json_object(item, "GitHub pull request commit") for item in payload]
        # The commits must be the ones this snapshot saw, not a newer push.
        if commits[-1].get("sha") != head_sha:
            return "needs_review"
        messages: list[str] = []
        commit_shas = {commit.get("sha") for commit in commits}
        for commit in commits:
            # A commit author is only an email, so a collaborator can amend and
            # keep it. Require the bot as author and a GitHub-signed commit.
            author = commit.get("author")
            committer = commit.get("committer")
            detail = _json_object(commit.get("commit"), "GitHub pull request commit detail")
            verification = detail.get("verification")
            if (
                not isinstance(committer, dict)
                or committer.get("id") != _GITHUB_WEB_FLOW_USER_ID
                or not isinstance(verification, dict)
                or verification.get("verified") is not True
            ):
                return "needs_review"
            if self._recorded_base_refresh(
                repository_path=repository_path,
                pull_request_number=pull_request_number,
                commit=commit,
                commit_shas=commit_shas,
                base_branch=base_branch,
                base_sha=base_sha,
            ):
                continue
            if not isinstance(author, dict) or author.get("id") != author_id:
                return "needs_review"
            messages.append(str(detail.get("message") or ""))
        if self._force_pushed_by_other(
            repository_path=repository_path,
            pull_request_number=pull_request_number,
            author_id=author_id,
        ):
            return "needs_review"
        return classify_dependency_update(messages)

    def _recorded_base_refresh(
        self,
        *,
        repository_path: str,
        pull_request_number: int,
        commit: dict[str, object],
        commit_shas: set[object],
        base_branch: str,
        base_sha: str,
    ) -> bool:
        parents = commit.get("parents")
        if not isinstance(parents, list) or len(parents) != 2 or self._branch_refresh_store is None:
            return False
        parent_shas = [
            parent.get("sha") if isinstance(parent, dict) else None for parent in parents
        ]
        if parent_shas[0] not in commit_shas:
            return False
        try:
            records = self._branch_refresh_store.list_merge_train_branch_refresh_records(
                repository=repository_path, pull_request_number=pull_request_number
            )
        except Exception:
            logger.warning("Cannot read train branch refresh evidence.", exc_info=True)
            return False
        for record in records:
            if (
                record.repository.casefold() != repository_path.casefold()
                or record.pull_request_number != pull_request_number
                or record.base_branch != base_branch
                or record.result_head_sha != commit.get("sha")
                or record.expected_head_sha != parent_shas[0]
                or record.merged_base_sha != parent_shas[1]
            ):
                continue
            comparison = _json_object(
                self.transport.request(
                    method="GET",
                    path=(
                        f"/repos/{repository_path}/compare/{record.merged_base_sha}..."
                        f"{quote(base_sha, safe='')}"
                    ),
                ),
                "GitHub base ancestry comparison",
            )
            if comparison.get("status") not in {"ahead", "identical"}:
                return False

            def read(path: str) -> object:
                return self.transport.request(method="GET", path=path)

            before = change_fingerprint(
                repository=repository_path,
                base=record.merged_base_sha,
                head=record.expected_head_sha,
                read=read,
                include_blobs=False,
            )
            return before is not None and before == change_fingerprint(
                repository=repository_path,
                base=record.merged_base_sha,
                head=record.result_head_sha,
                read=read,
                include_blobs=False,
            )
        return False

    def _force_pushed_by_other(
        self, *, repository_path: str, pull_request_number: int, author_id: int
    ) -> bool:
        payload = self.transport.request(
            method="GET",
            path=f"/repos/{repository_path}/issues/{pull_request_number}/timeline?per_page=100",
        )
        if not isinstance(payload, list) or len(payload) >= 100:
            return True
        for item in payload:
            event = _json_object(item, "GitHub pull request timeline event")
            if event.get("event") != "head_ref_force_pushed":
                continue
            actor = event.get("actor")
            if not isinstance(actor, dict) or actor.get("id") != author_id:
                return True
        return False

    def _label_actors(
        self, *, repository_path: str, pull_request_number: int, labels: tuple[str, ...]
    ) -> tuple[MergeTrainLabelActor, ...]:
        # The latest "labeled" event names who applied each current label. A
        # label with no readable event gets no actor, which admission refuses.
        last_labeled: dict[str, dict[str, object]] = {}
        page = 1
        while True:
            payload = self.transport.request(
                method="GET",
                path=(
                    f"/repos/{repository_path}/issues/{pull_request_number}/events"
                    f"?per_page=100&page={page}"
                ),
            )
            if not isinstance(payload, list):
                raise MergeTrainGitHubError("GitHub issue events response must be a JSON array.")
            for item in payload:
                event = _json_object(item, "GitHub issue event")
                if event.get("event") not in {"labeled", "unlabeled"}:
                    continue
                label = event.get("label")
                name = label.get("name") if isinstance(label, dict) else None
                if not isinstance(name, str) or not name.strip():
                    continue
                key = name.strip().casefold()
                if event.get("event") == "unlabeled":
                    last_labeled.pop(key, None)
                else:
                    last_labeled[key] = event
            if len(payload) < 100:
                break
            page += 1
        label_actors: list[MergeTrainLabelActor] = []
        for label in labels:
            labeled_event = last_labeled.get(label.strip().casefold())
            if labeled_event is None:
                continue
            actor = labeled_event.get("actor")
            if not isinstance(actor, dict):
                continue
            actor_id = actor.get("id")
            login = actor.get("login")
            if type(actor_id) is not int or actor_id <= 0 or not isinstance(login, str):
                continue
            label_actors.append(
                MergeTrainLabelActor(
                    label=label,
                    actor_id=actor_id,
                    actor_login=login,
                    actor_role=self._actor_role_for_user(
                        repository_path=repository_path, username=login
                    ),
                    on_behalf_via_app=_on_behalf_via_app(event=labeled_event, actor=actor),
                )
            )
        return tuple(label_actors)

    def _actor_role_for_user(self, *, repository_path: str, username: str) -> str:
        owner = repository_path.split("/", 1)[0]
        if username.casefold() == owner.casefold():
            return "repo_owner"
        # One snapshot usually sees the same few labelers on every pull request.
        cache_key = (repository_path, username.casefold())
        if cache_key not in self._actor_roles:
            self._actor_roles[cache_key] = self._collaborator_role(
                repository_path=repository_path, username=username
            )
        return self._actor_roles[cache_key]

    def _actor_role_for_pull_request(
        self, *, repository_path: str, username: str, author_association: str
    ) -> str:
        if author_association.upper() == "OWNER":
            return "repo_owner"
        return self._collaborator_role(repository_path=repository_path, username=username)

    def _collaborator_role(self, *, repository_path: str, username: str) -> str:
        try:
            payload = _json_object(
                self.transport.request(
                    method="GET",
                    path=f"/repos/{repository_path}/collaborators/{quote(username, safe='')}/permission",
                ),
                "GitHub collaborator permission response",
            )
        except MergeTrainGitHubError as error:
            if error.status_code == 404:
                return "unknown"
            raise
        return "repo_admin" if str(payload.get("permission") or "") == "admin" else "unknown"

    def client_review_required(self, *, labels: tuple[str, ...], repository: str) -> bool:
        review_labels: set[str] = set()
        list_profiles = getattr(self._branch_refresh_store, "list_product_profile_records", None)
        if callable(list_profiles):
            review_labels.update(
                profile.owner.review_label.strip().casefold()
                for profile in list_profiles()
                if profile.is_active and profile.repository.casefold() == repository.casefold()
            )
        return bool(review_labels.intersection(label.casefold() for label in labels))

    def _required_checks_status(
        self, *, repository_path: str, head_sha: str, owner_review_required: bool = False
    ) -> MergeTrainCheckStatus:
        encoded_head_sha = quote(head_sha, safe="")
        return _required_checks_status(
            transport=self.transport,
            repository_path=repository_path,
            encoded_head_sha=encoded_head_sha,
            owner_review_required=owner_review_required,
        )

    def _list_check_runs(self, *, repository_path: str, encoded_head_sha: str) -> dict[str, object]:
        return _list_check_runs(
            transport=self.transport,
            repository_path=repository_path,
            encoded_head_sha=encoded_head_sha,
        )


class MergeTrainGitHubRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: str
    path: str
    body: dict[str, object] | None = None

    @model_validator(mode="after")
    def _validate_request(self) -> "MergeTrainGitHubRequest":
        self.method = _required_value(self.method, "GitHub request method is required.").upper()
        self.path = _required_value(self.path, "GitHub request path is required.")
        if not self.path.startswith("/"):
            raise ValueError("GitHub request path must start with '/'.")
        return self


class RecordingMergeTrainGitHubTransport:
    def __init__(self, *, responses: tuple[object, ...] = ()) -> None:
        self.responses = list(responses)
        self.requests: list[MergeTrainGitHubRequest] = []

    def request(self, *, method: str, path: str, body: dict[str, object] | None = None) -> object:
        self.requests.append(
            MergeTrainGitHubRequest.model_validate({"method": method, "path": path, "body": body})
        )
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            return response
        return {}


def _candidate_with_structural_provenance(
    *,
    candidate: MergeTrainBatchCandidate,
    candidate_sha: str,
    candidate_tree_sha: str,
    rolling_steps: tuple[MergeTrainRollingStep, ...],
) -> MergeTrainBatchCandidate:
    stack_root = candidate.stack_collapse_root
    if stack_root is not None and not stack_root.collapsed_root_tree_sha:
        root_entry = next(
            (
                entry
                for entry in candidate.entries
                if entry.pull_request_number == stack_root.root_pull_request_number
            ),
            None,
        )
        if root_entry is not None:
            stack_root = _validated_model_update(
                stack_root,
                collapsed_root_tree_sha=root_entry.head_tree_sha,
            )
    provenance = MergeTrainStructuralProvenance(
        repository=candidate.repository,
        base_branch=candidate.base_branch,
        base_sha=candidate.base_sha,
        base_tree_sha=rolling_steps[0].parent_tree_sha,
        policy_key=candidate.policy_key,
        policy_sha256=candidate.policy_sha256,
        entries=tuple(
            MergeTrainStructuralEntryBinding(
                position=entry.position,
                pull_request_number=entry.pull_request_number,
                head_sha=entry.head_sha,
                head_tree_sha=entry.head_tree_sha,
                impact_status=entry.impact_status,
                affected_subjects=entry.affected_subjects,
            )
            for entry in candidate.entries
        ),
        steps=rolling_steps,
        candidate_sha=candidate_sha,
        candidate_tree_sha=candidate_tree_sha,
        stack_collapse_root=stack_root,
    )
    return _validated_model_update(
        candidate,
        candidate_sha=candidate_sha,
        candidate_tree_sha=candidate_tree_sha,
        candidate_sha256=provenance.candidate_sha256,
        stack_collapse_root=stack_root,
        structural_provenance=provenance,
        status="building",
    )


def _git_commit_identity(
    *,
    transport: MergeTrainGitHubTransport,
    repository_path: str,
    commit_sha: str,
) -> tuple[str, str]:
    observed_sha, tree_sha, _ = _git_commit_proof(
        transport=transport,
        repository_path=repository_path,
        commit_sha=commit_sha,
    )
    return observed_sha, tree_sha


def _git_commit_proof(
    *,
    transport: MergeTrainGitHubTransport,
    repository_path: str,
    commit_sha: str,
) -> tuple[str, str, tuple[str, ...]]:
    encoded_sha = quote(_required_value(commit_sha, "GitHub commit SHA is required."), safe="")
    payload = _json_object(
        transport.request(
            method="GET",
            path=f"/repos/{repository_path}/git/commits/{encoded_sha}",
        ),
        "GitHub commit response",
    )
    observed_sha = _required_text(payload.get("sha"), "GitHub commit response requires sha.")
    if observed_sha != commit_sha:
        raise MergeTrainGitHubStaleHeadError(
            "GitHub commit response did not match the requested SHA.", status_code=409
        )
    tree = _json_object(payload.get("tree"), "GitHub commit tree response")
    tree_sha = _required_text(tree.get("sha"), "GitHub commit tree response requires sha.")
    raw_parents = payload.get("parents", [])
    if not isinstance(raw_parents, list):
        raise MergeTrainGitHubError("GitHub commit response parents must be a JSON list.")
    parents = tuple(
        _required_text(
            _json_object(parent, "GitHub commit parent").get("sha"),
            "GitHub commit parent requires sha.",
        )
        for parent in raw_parents
    )
    return observed_sha, tree_sha, parents


def _validated_merge_commit_identity(
    *,
    transport: MergeTrainGitHubTransport,
    repository_path: str,
    commit_sha: str,
    expected_parent_sha: str,
    expected_head_sha: str,
) -> tuple[str, str]:
    observed_sha, tree_sha, parents = _git_commit_proof(
        transport=transport,
        repository_path=repository_path,
        commit_sha=commit_sha,
    )
    if parents != (expected_parent_sha, expected_head_sha):
        raise MergeTrainGitHubStaleHeadError(
            "GitHub merge commit parents do not exactly match the recorded parent and head.",
            status_code=409,
        )
    return observed_sha, tree_sha


def _validated_landing_commit_identity(
    *,
    transport: MergeTrainGitHubTransport,
    repository_path: str,
    commit_sha: str,
    expected_parent_sha: str,
    expected_head_sha: str,
    merge_method: MergeTrainMergeMethod,
) -> tuple[str, str]:
    observed_sha, tree_sha, parents = _git_commit_proof(
        transport=transport,
        repository_path=repository_path,
        commit_sha=commit_sha,
    )
    expected_parents = (
        (expected_parent_sha, expected_head_sha)
        if merge_method == "merge"
        else (expected_parent_sha,)
    )
    if parents != expected_parents:
        raise MergeTrainGitHubStaleHeadError(
            "GitHub landing commit parents do not match the recorded rolling merge inputs.",
            status_code=409,
        )
    return observed_sha, tree_sha


def _recorded_candidate_step_is_no_op(entry: MergeTrainBatchLandingEntry) -> bool:
    return bool(entry.recorded_candidate_parent_sha) and (
        entry.recorded_candidate_parent_sha == entry.recorded_candidate_result_sha
        and entry.recorded_candidate_parent_tree_sha == entry.recorded_candidate_result_tree_sha
    )


def _historical_provider_error(
    error: MergeTrainGitHubError, *, pull_request_number: int | None = None
) -> MergeTrainHistoricalCompletionProofError:
    if isinstance(error, MergeTrainGitHubStaleHeadError):
        reason_code: HistoricalCompletionProofReason = "provider_binding_mismatch"
        status: HistoricalCompletionProofStatus = "unsupported"
    elif error.status_code is not None or isinstance(
        error.__cause__, (OSError, TimeoutError, URLError)
    ):
        reason_code = "provider_unavailable"
        status = "indeterminate"
    else:
        reason_code = "provider_response_malformed"
        status = "indeterminate"
    return MergeTrainHistoricalCompletionProofError(
        status=status,
        reason_code=reason_code,
        pull_request_number=pull_request_number,
    )


def _branch_contains_commit_at_pinned_base(
    *,
    transport: MergeTrainGitHubTransport,
    repository_path: str,
    merge_commit_sha: str,
    pinned_base_sha: str,
) -> bool:
    normalized_merge_sha = quote(
        _required_value(merge_commit_sha, "Historical completion merge SHA is required."),
        safe="",
    )
    normalized_base_sha = quote(
        _required_value(pinned_base_sha, "Historical completion pinned base SHA is required."),
        safe="",
    )
    payload = _json_object(
        transport.request(
            method="GET",
            path=f"/repos/{repository_path}/compare/{normalized_merge_sha}...{normalized_base_sha}",
        ),
        "GitHub historical completion compare response",
    )
    status = _required_text(
        payload.get("status"), "GitHub historical completion compare status is required."
    )
    if status not in {"ahead", "identical", "behind", "diverged"}:
        raise MergeTrainGitHubError("GitHub historical completion compare status is unsupported.")
    merge_base = _json_object(
        payload.get("merge_base_commit"),
        "GitHub historical completion merge base response",
    )
    merge_base_sha = _required_text(
        merge_base.get("sha"),
        "GitHub historical completion merge base SHA is required.",
    )
    if status in {"ahead", "identical"}:
        if merge_base_sha != merge_commit_sha:
            raise MergeTrainGitHubError(
                "GitHub historical completion compare merge base was inconsistent."
            )
        return True
    return False


def _validated_model_update(model: ModelT, **updates: object) -> ModelT:
    return type(model).model_validate({**model.model_dump(mode="python"), **updates})


def _on_behalf_via_app(*, event: dict[str, object], actor: dict[str, object]) -> str:
    """Name the GitHub App that acted with a user's token, or return ""."""
    # An App's own installation token acts as its bot account. With a user
    # access token GitHub attributes the event to the user, so the user's role
    # must not stand in for the App's.
    app = event.get("performed_via_github_app")
    if not isinstance(app, dict) or actor.get("type") == "Bot":
        return ""
    slug = app.get("slug")
    return slug.strip() if isinstance(slug, str) and slug.strip() else f"app {app.get('id')}"


def _repository_path(repository: str) -> str:
    normalized = _required_value(repository, "GitHub repository is required.").lower()
    parts = normalized.split("/")
    if len(parts) != 2 or not all(part.strip() for part in parts):
        raise ValueError("GitHub repository must be formatted as owner/name.")
    return "/".join(quote(part.strip(), safe="") for part in parts)


def _branch_name_from_ref(reference: str) -> str:
    normalized = _required_value(reference, "GitHub branch ref is required.")
    prefix = "refs/heads/"
    if not normalized.startswith(prefix):
        raise ValueError("GitHub branch ref must start with refs/heads/.")
    return normalized.removeprefix(prefix)


def _reference_path(reference: str) -> str:
    normalized = _required_value(reference, "GitHub reference is required.")
    prefix = "refs/"
    if not normalized.startswith(prefix):
        raise ValueError("GitHub reference must start with refs/.")
    return "/".join(quote(part, safe="") for part in normalized.removeprefix(prefix).split("/"))


def _json_object(value: object, label: str) -> dict[str, object]:
    return json_object(value, label, error_type=MergeTrainGitHubError)


def _repository_full_name(value: object, label: str) -> str:
    repository = _json_object(value, label)
    return _required_text(repository.get("full_name"), f"{label} requires full_name.").lower()


def _base_rooted_pull_requests(
    *,
    pull_requests: tuple[dict[str, object], ...],
    repository: str,
    base_branch: str,
) -> tuple[dict[str, object], ...]:
    repository = _required_value(repository, "GitHub repository is required.").lower()
    by_base_ref: dict[str, list[dict[str, object]]] = {}
    relevant_numbers: set[int] = set()
    for pull_request in pull_requests:
        base = _json_object(pull_request.get("base"), "GitHub pull request base")
        base_repository = _repository_full_name(base.get("repo"), "GitHub pull request base repo")
        if base_repository != repository:
            continue
        base_ref = _required_text(base.get("ref"), "GitHub pull request base requires ref.")
        by_base_ref.setdefault(base_ref, []).append(pull_request)

    branch_refs = [base_branch]
    seen_branch_refs: set[str] = set()
    while branch_refs:
        branch_ref = branch_refs.pop(0)
        if branch_ref in seen_branch_refs:
            continue
        seen_branch_refs.add(branch_ref)
        for pull_request in by_base_ref.get(branch_ref, ()):
            pull_request_number = _required_int(
                pull_request.get("number"), "GitHub pull request entry requires number."
            )
            if pull_request_number in relevant_numbers:
                continue
            relevant_numbers.add(pull_request_number)
            head = _json_object(pull_request.get("head"), "GitHub pull request head")
            head_repository = _repository_full_name(
                head.get("repo"), "GitHub pull request head repo"
            )
            if head_repository != repository:
                continue
            branch_refs.append(
                _required_text(head.get("ref"), "GitHub pull request head requires ref.")
            )
    return tuple(
        pull_request
        for pull_request in pull_requests
        if _required_int(pull_request.get("number"), "GitHub pull request entry requires number.")
        in relevant_numbers
    )


def _required_int(value: object, message: str) -> int:
    return required_positive_int(value, message, error_type=MergeTrainGitHubError)


def _required_text(value: object, message: str) -> str:
    return required_string_text(value, message, error_type=MergeTrainGitHubError)


def _pull_request_state(value: str) -> MergeTrainPullRequestState:
    normalized = value.strip().lower()
    if normalized == "open":
        return "open"
    if normalized == "closed":
        return "closed"
    raise MergeTrainGitHubError("GitHub pull request state must be open or closed.")


def _labels(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise MergeTrainGitHubError("GitHub pull request labels must be a JSON array.")
    labels: list[str] = []
    for item in value:
        label = _json_object(item, "GitHub pull request label")
        name = _required_text(label.get("name"), "GitHub pull request label requires name.")
        if name not in labels:
            labels.append(name)
    return tuple(labels)


def _mergeable_state(source: dict[str, object]) -> MergeTrainMergeableState:
    if bool(source.get("draft")):
        return "unknown"
    mergeable = source.get("mergeable")
    if mergeable is True:
        return "mergeable"
    if mergeable is False:
        return "conflicting"
    return "unknown"


def _branch_update_required(source: dict[str, object]) -> bool:
    mergeable_state = str(source.get("mergeable_state") or "").strip().lower()
    return mergeable_state == "behind"


def _base_branch_sha(
    *, transport: MergeTrainGitHubTransport, repository_path: str, base_branch: str
) -> str:
    branch = _json_object(
        transport.request(
            method="GET",
            path=f"/repos/{repository_path}/branches/{quote(base_branch, safe='')}",
        ),
        "GitHub branch response",
    )
    commit = _json_object(branch.get("commit"), "GitHub branch commit")
    return _required_text(commit.get("sha"), "GitHub branch commit requires sha.")


def merge_train_construction_ref(candidate_ref: str) -> str:
    """Locate native construction evidence from the canonical candidate identity."""
    return "refs/heads/launchplane/construct/" + sha256(candidate_ref.encode("utf-8")).hexdigest()


def merge_train_conflict_probe_ref(
    *, repository: str, base_branch: str, lease_owner: str, lease_acquired_at: str
) -> str:
    """Locate a conflict probe in the construction namespace.

    The ref is unique to one controller lease acquisition. A pass that outlived
    its lease cannot reset or delete the probe of the pass that adopted it.
    """
    probe_identity = (
        f"conflict-probe:{repository.lower()}:{base_branch}:{lease_owner}:{lease_acquired_at}"
    )
    return "refs/heads/launchplane/construct/" + sha256(probe_identity.encode()).hexdigest()


def _verify_candidate_publication(
    *,
    transport: MergeTrainGitHubTransport,
    repository_path: str,
    candidate_ref: str,
    expected_sha: str,
) -> None:
    for delay_seconds in (0.0, *MERGE_REF_READ_DELAYS_SECONDS):
        if delay_seconds:
            sleep(delay_seconds)
        try:
            observed_sha = _base_branch_sha(
                transport=transport,
                repository_path=repository_path,
                base_branch=_branch_name_from_ref(candidate_ref),
            )
        except MergeTrainGitHubError as error:
            if error.status_code != 404:
                raise
            continue
        if observed_sha == expected_sha:
            return
    raise MergeTrainGitHubStaleHeadError(
        "GitHub published candidate ref did not resolve to the completed candidate.",
        status_code=409,
    )


def _wait_for_branch_sha(
    *,
    transport: MergeTrainGitHubTransport,
    repository_path: str,
    base_branch: str,
    expected_sha: str,
    previous_sha: str,
) -> str:
    observed_sha = _base_branch_sha(
        transport=transport,
        repository_path=repository_path,
        base_branch=base_branch,
    )
    for delay_seconds in MERGE_REF_READ_DELAYS_SECONDS:
        if observed_sha != previous_sha or observed_sha == expected_sha:
            return observed_sha
        sleep(delay_seconds)
        observed_sha = _base_branch_sha(
            transport=transport,
            repository_path=repository_path,
            base_branch=base_branch,
        )
    return observed_sha


def _base_branch_identity(
    *, transport: MergeTrainGitHubTransport, repository_path: str, base_branch: str
) -> tuple[str, str]:
    branch = _json_object(
        transport.request(
            method="GET",
            path=f"/repos/{repository_path}/branches/{quote(base_branch, safe='')}",
        ),
        "GitHub branch response",
    )
    commit = _json_object(branch.get("commit"), "GitHub branch commit")
    branch_sha = _required_text(commit.get("sha"), "GitHub branch commit requires sha.")
    commit_detail = commit.get("commit")
    if isinstance(commit_detail, dict):
        tree = commit_detail.get("tree")
        if isinstance(tree, dict):
            tree_sha = tree.get("sha")
            if isinstance(tree_sha, str) and tree_sha.strip():
                return branch_sha, tree_sha.strip()
    return _git_commit_identity(
        transport=transport,
        repository_path=repository_path,
        commit_sha=branch_sha,
    )


def _combined_status_state(payload: dict[str, object]) -> MergeTrainCheckStatus:
    raw_statuses = payload.get("statuses")
    if not isinstance(raw_statuses, list):
        raise MergeTrainGitHubError("GitHub combined status response must include statuses.")
    statuses: list[MergeTrainCheckStatus] = []
    seen_contexts: set[str] = set()
    for item in raw_statuses:
        status = _json_object(item, "GitHub commit status")
        context = _required_text(status.get("context"), "GitHub commit status requires context.")
        normalized_context = context.casefold()
        if is_launchplane_projected_check(context) or normalized_context in seen_contexts:
            continue
        seen_contexts.add(normalized_context)
        statuses.append(_commit_status_state(status))
    if not statuses:
        return "unknown"
    if any(status == "fail" for status in statuses):
        return "fail"
    if any(status == "pending" for status in statuses):
        return "pending"
    if all(status == "pass" for status in statuses):
        return "pass"
    return "unknown"


def _commit_status_state(payload: dict[str, object]) -> MergeTrainCheckStatus:
    state = _required_text(payload.get("state"), "GitHub commit status requires state.").lower()
    if state == "success":
        return "pass"
    if state in {"failure", "error"}:
        return "fail"
    if state == "pending":
        return "pending"
    return "unknown"


def _check_runs_status(payload: dict[str, object]) -> MergeTrainCheckStatus:
    check_runs = payload.get("check_runs")
    if not isinstance(check_runs, list):
        raise MergeTrainGitHubError("GitHub check runs response must include check_runs.")
    if not check_runs:
        return "unknown"
    statuses: list[MergeTrainCheckStatus] = []
    for item in check_runs:
        check_run = _json_object(item, "GitHub check run")
        if is_launchplane_projected_check(str(check_run.get("name") or "")):
            continue
        statuses.append(_check_run_status(check_run))
    if not statuses:
        return "unknown"
    return _combine_check_statuses(*statuses)


def _required_checks_status(
    *,
    transport: MergeTrainGitHubTransport,
    repository_path: str,
    encoded_head_sha: str,
    owner_review_required: bool = False,
) -> MergeTrainCheckStatus:
    status_payload = _list_commit_statuses(
        transport=transport,
        repository_path=repository_path,
        encoded_head_sha=encoded_head_sha,
    )
    check_runs_payload = _list_check_runs(
        transport=transport,
        repository_path=repository_path,
        encoded_head_sha=encoded_head_sha,
    )
    statuses = [_combined_status_state(status_payload), _check_runs_status(check_runs_payload)]
    if owner_review_required:
        statuses.append(_owner_review_status(status_payload))
    return _combine_check_statuses(*statuses)


def _owner_review_status(status_payload: dict[str, object]) -> MergeTrainCheckStatus:
    from control_plane.product_review_status import OWNER_REVIEW_STATUS_CONTEXT

    raw_statuses = status_payload["statuses"]
    assert isinstance(raw_statuses, list)
    owner_status = next(
        (
            item
            for item in raw_statuses
            if isinstance(item, dict)
            and str(item.get("context") or "").casefold() == OWNER_REVIEW_STATUS_CONTEXT.casefold()
        ),
        None,
    )
    # Current-head status responses are newest-first; unknown review evidence waits.
    state = _commit_status_state(owner_status) if owner_status is not None else "pending"
    return "pending" if state == "unknown" else state


def _candidate_required_checks_status(
    *,
    transport: MergeTrainGitHubTransport,
    repository_path: str,
    base_branch: str,
    encoded_head_sha: str,
) -> MergeTrainCheckStatus:
    required_checks = _required_branch_checks(
        transport=transport,
        repository_path=repository_path,
        base_branch=base_branch,
    )
    status_payload = _list_commit_statuses(
        transport=transport,
        repository_path=repository_path,
        encoded_head_sha=encoded_head_sha,
    )
    check_runs_payload = _list_check_runs(
        transport=transport,
        repository_path=repository_path,
        encoded_head_sha=encoded_head_sha,
    )
    observed_status = _combine_check_statuses(
        _combined_status_state(status_payload),
        _check_runs_status(check_runs_payload),
    )
    required_status, missing_required_checks = _required_check_evidence_status(
        required_checks=required_checks,
        status_payload=status_payload,
        check_runs_payload=check_runs_payload,
    )
    if observed_status == "pass" and required_status == "pending" and missing_required_checks:
        raise MergeTrainGitHubError(
            "Merge train candidate is missing required check evidence: "
            + ", ".join(missing_required_checks)
        )
    return _combine_check_statuses(observed_status, required_status)


_CONVERSATION_RESOLUTION_RULE_QUERY = """
query($owner: String!, $name: String!, $ref: String!) {
  repository(owner: $owner, name: $name) {
    ref(qualifiedName: $ref) { refUpdateRule { requiresConversationResolution } }
  }
}
"""

_REVIEW_THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $after) {
        nodes { isResolved comments(first: 1) { nodes { author { login } } } }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""


def _conversations_can_block(pull_request: MergeTrainPullRequestSnapshot) -> bool:
    return pull_request.state == "open" and not pull_request.is_draft


def _graphql_repository(
    *,
    transport: MergeTrainGitHubTransport,
    repository_path: str,
    query: str,
    variables: dict[str, object],
) -> dict[str, object]:
    owner, name = repository_path.split("/", 1)
    payload = _json_object(
        transport.request(
            method="POST",
            path="/graphql",
            body={"query": query, "variables": {"owner": owner, "name": name, **variables}},
        ),
        "GitHub GraphQL response",
    )
    if payload.get("errors"):
        raise MergeTrainGitHubError("GitHub GraphQL request returned errors.")
    data = _json_object(payload.get("data"), "GitHub GraphQL data")
    return _json_object(data.get("repository"), "GitHub GraphQL repository")


def _conversation_resolution_rule(
    *, transport: MergeTrainGitHubTransport, repository_path: str, base_branch: str
) -> Literal["required", "not_required", "unreadable"]:
    """Read whether the base branch requires resolved conversations.

    Classic branch protection and active rulesets can each require it. The
    train token has no Administration permission, so this reads the classic
    rule GitHub shows to non-admins and the branch's active rules, which need
    only Metadata read. An unreadable rule is not taken as absent: callers then
    treat unresolved threads as blocking.
    """
    classic = _classic_conversation_rule(
        transport=transport, repository_path=repository_path, base_branch=base_branch
    )
    if classic == "required":
        return classic
    ruleset = _ruleset_conversation_rule(
        transport=transport, repository_path=repository_path, base_branch=base_branch
    )
    if ruleset == "required":
        return ruleset
    if "unreadable" in (classic, ruleset):
        return "unreadable"
    return "not_required"


def _classic_conversation_rule(
    *, transport: MergeTrainGitHubTransport, repository_path: str, base_branch: str
) -> Literal["required", "not_required", "unreadable"]:
    try:
        repository = _graphql_repository(
            transport=transport,
            repository_path=repository_path,
            query=_CONVERSATION_RESOLUTION_RULE_QUERY,
            variables={"ref": f"refs/heads/{base_branch}"},
        )
        ref = _json_object(repository.get("ref"), "GitHub GraphQL base ref")
    except MergeTrainGitHubError:
        return "unreadable"
    rule = ref.get("refUpdateRule")
    if rule is None:
        return "not_required"
    required = rule.get("requiresConversationResolution") if isinstance(rule, dict) else None
    if not isinstance(required, bool):
        return "unreadable"
    return "required" if required else "not_required"


def _ruleset_conversation_rule(
    *, transport: MergeTrainGitHubTransport, repository_path: str, base_branch: str
) -> Literal["required", "not_required", "unreadable"]:
    encoded_branch = quote(base_branch, safe="")
    page = 1
    while True:
        try:
            rules = transport.request(
                method="GET",
                path=f"/repos/{repository_path}/rules/branches/{encoded_branch}"
                f"?per_page=100&page={page}",
            )
        except MergeTrainGitHubError:
            return "unreadable"
        if not isinstance(rules, list):
            return "unreadable"
        for rule in rules:
            if not isinstance(rule, dict) or rule.get("type") != "pull_request":
                continue
            parameters = rule.get("parameters")
            if not isinstance(parameters, dict):
                return "unreadable"
            if parameters.get("required_review_thread_resolution") is True:
                return "required"
        if len(rules) < 100:
            return "not_required"
        page += 1


def _review_conversations(
    *,
    transport: MergeTrainGitHubTransport,
    repository_path: str,
    pull_request_number: int,
    rule: Literal["required", "unreadable"],
) -> MergeTrainReviewConversations | None:
    unresolved_count = 0
    code_scanning_count = 0
    after: str | None = None
    while True:
        repository = _graphql_repository(
            transport=transport,
            repository_path=repository_path,
            query=_REVIEW_THREADS_QUERY,
            variables={"number": pull_request_number, "after": after},
        )
        pull_request = _json_object(repository.get("pullRequest"), "GitHub GraphQL pull request")
        threads = _json_object(pull_request.get("reviewThreads"), "GitHub GraphQL review threads")
        nodes = threads.get("nodes")
        if not isinstance(nodes, list):
            raise MergeTrainGitHubError("GitHub GraphQL review threads must be a list.")
        for node in nodes:
            thread = _json_object(node, "GitHub GraphQL review thread")
            if thread.get("isResolved") is True:
                continue
            unresolved_count += 1
            if _first_comment_author(thread) == CODE_SCANNING_REVIEW_AUTHOR:
                code_scanning_count += 1
        page = _json_object(threads.get("pageInfo"), "GitHub GraphQL review thread page")
        if page.get("hasNextPage") is not True:
            break
        after = _required_text(
            page.get("endCursor"), "GitHub GraphQL review thread page requires endCursor."
        )
    if not unresolved_count:
        return None
    return MergeTrainReviewConversations(
        rule=rule,
        unresolved_count=unresolved_count,
        code_scanning_count=code_scanning_count,
    )


def _first_comment_author(thread: dict[str, object]) -> str:
    comments = thread.get("comments")
    nodes = comments.get("nodes") if isinstance(comments, dict) else None
    first = nodes[0] if isinstance(nodes, list) and nodes else None
    author = first.get("author") if isinstance(first, dict) else None
    login = author.get("login") if isinstance(author, dict) else None
    return login.strip() if isinstance(login, str) else ""


def _required_branch_checks(
    *,
    transport: MergeTrainGitHubTransport,
    repository_path: str,
    base_branch: str,
) -> tuple[tuple[str, int | None], ...]:
    encoded_base_branch = quote(base_branch, safe="")
    try:
        raw_payload = transport.request(
            method="GET",
            path=f"/repos/{repository_path}/branches/{encoded_base_branch}",
        )
    except MergeTrainGitHubError as error:
        if error.status_code in {403, 404}:
            raise MergeTrainGitHubError(
                "Merge train candidate validation requires a readable protected-branch "
                "required-check policy and GitHub contents: read permission.",
                status_code=error.status_code,
            ) from error
        raise
    branch = _json_object(raw_payload, "GitHub protected branch response")
    if branch.get("protected") is not True:
        raise MergeTrainGitHubError("Merge train candidate validation requires a protected branch.")
    protection = _json_object(branch.get("protection"), "GitHub branch protection")
    payload = _json_object(
        protection.get("required_status_checks"), "GitHub required status checks response"
    )
    if payload.get("enforcement_level") not in {"everyone", "non_admins"}:
        raise MergeTrainGitHubError(
            "Merge train candidate validation requires enforced protected-branch status checks."
        )
    required_checks: dict[tuple[str, int | None], tuple[str, int | None]] = {}
    raw_checks = payload.get("checks")
    if raw_checks is not None:
        if not isinstance(raw_checks, list):
            raise MergeTrainGitHubError(
                "GitHub required status checks response checks must be a list."
            )
        for item in raw_checks:
            check = _json_object(item, "GitHub required status check")
            context = _required_text(
                check.get("context"), "GitHub required status check requires context."
            )
            raw_app_id = check.get("app_id")
            if raw_app_id is not None and (
                isinstance(raw_app_id, bool)
                or not isinstance(raw_app_id, int)
                or raw_app_id == 0
                or raw_app_id < -1
            ):
                raise MergeTrainGitHubError(
                    "GitHub required status check app_id must be positive, -1, or null."
                )
            app_id = raw_app_id if isinstance(raw_app_id, int) and raw_app_id > 0 else None
            required_checks[(context.casefold(), app_id)] = (context, app_id)
    if not required_checks:
        raw_contexts = payload.get("contexts")
        if not isinstance(raw_contexts, list):
            raise MergeTrainGitHubError(
                "GitHub required status checks response contexts must be a list."
            )
        for item in raw_contexts:
            context = _required_text(item, "GitHub required status check context is required.")
            required_checks[(context.casefold(), None)] = (context, None)
    if not required_checks:
        raise MergeTrainGitHubError(
            "Merge train candidate validation requires at least one protected-branch status check."
        )
    return tuple(
        required_checks[key]
        for key in sorted(
            required_checks,
            key=lambda item: (item[0], item[1] if item[1] is not None else -1),
        )
    )


def _required_check_evidence_status(
    *,
    required_checks: tuple[tuple[str, int | None], ...],
    status_payload: dict[str, object],
    check_runs_payload: dict[str, object],
) -> tuple[MergeTrainCheckStatus, tuple[str, ...]]:
    raw_statuses = status_payload.get("statuses")
    if not isinstance(raw_statuses, list):
        raise MergeTrainGitHubError("GitHub combined status response must include statuses.")
    raw_check_runs = check_runs_payload.get("check_runs")
    if not isinstance(raw_check_runs, list):
        raise MergeTrainGitHubError("GitHub check runs response must include check_runs.")
    commit_statuses: dict[str, MergeTrainCheckStatus] = {}
    for item in raw_statuses:
        status = _json_object(item, "GitHub commit status")
        context = _required_text(status.get("context"), "GitHub commit status requires context.")
        normalized_context = context.casefold()
        if is_launchplane_projected_check(context) or normalized_context in commit_statuses:
            continue
        commit_statuses[normalized_context] = _commit_status_state(status)
    check_runs: list[tuple[str, int | None, MergeTrainCheckStatus]] = []
    for item in raw_check_runs:
        check_run = _json_object(item, "GitHub check run")
        name = _required_text(check_run.get("name"), "GitHub check run requires name.")
        if is_launchplane_projected_check(name):
            continue
        app = check_run.get("app")
        app_id: int | None = None
        if isinstance(app, dict):
            raw_app_id = app.get("id")
            if isinstance(raw_app_id, int) and not isinstance(raw_app_id, bool):
                app_id = raw_app_id
        check_runs.append((name.casefold(), app_id, _check_run_status(check_run)))
    required_statuses: list[MergeTrainCheckStatus] = []
    missing_required_checks: list[str] = []
    for context, required_app_id in required_checks:
        normalized_context = context.casefold()
        matching_statuses = [
            status
            for name, app_id, status in check_runs
            if name == normalized_context and (required_app_id is None or app_id == required_app_id)
        ]
        if required_app_id is None:
            commit_status = commit_statuses.get(normalized_context)
            if commit_status is not None:
                matching_statuses.append(commit_status)
        if not matching_statuses:
            required_statuses.append("pending")
            missing_required_checks.append(
                context if required_app_id is None else f"{context} (app_id={required_app_id})"
            )
        elif any(status == "fail" for status in matching_statuses):
            required_statuses.append("fail")
        elif all(status == "pass" for status in matching_statuses):
            required_statuses.append("pass")
        else:
            required_statuses.append("pending")
    if any(status == "fail" for status in required_statuses):
        return "fail", tuple(missing_required_checks)
    if all(status == "pass" for status in required_statuses):
        return "pass", ()
    return "pending", tuple(missing_required_checks)


def _list_commit_statuses(
    *,
    transport: MergeTrainGitHubTransport,
    repository_path: str,
    encoded_head_sha: str,
) -> dict[str, object]:
    statuses: list[object] = []
    total_count: int | None = None
    page = 1
    while True:
        payload = _json_object(
            transport.request(
                method="GET",
                path=(
                    f"/repos/{repository_path}/commits/{encoded_head_sha}/status"
                    f"?per_page=100&page={page}"
                ),
            ),
            "GitHub combined status response",
        )
        raw_statuses = payload.get("statuses")
        if not isinstance(raw_statuses, list):
            raise MergeTrainGitHubError("GitHub combined status response must include statuses.")
        raw_total_count = payload.get("total_count")
        if isinstance(raw_total_count, int):
            total_count = raw_total_count
        statuses.extend(raw_statuses)
        if len(raw_statuses) < 100 or (total_count is not None and len(statuses) >= total_count):
            break
        page += 1
    return {
        "total_count": total_count if total_count is not None else len(statuses),
        "statuses": statuses,
    }


def _list_check_runs(
    *,
    transport: MergeTrainGitHubTransport,
    repository_path: str,
    encoded_head_sha: str,
) -> dict[str, object]:
    check_runs: list[object] = []
    page = 1
    total_count: int | None = None
    while True:
        query = urlencode({"per_page": "100", "page": str(page)})
        payload = _json_object(
            transport.request(
                method="GET",
                path=(f"/repos/{repository_path}/commits/{encoded_head_sha}/check-runs?{query}"),
            ),
            "GitHub check runs response",
        )
        raw_check_runs = payload.get("check_runs")
        if not isinstance(raw_check_runs, list):
            raise MergeTrainGitHubError("GitHub check runs response must include check_runs.")
        raw_total_count = payload.get("total_count")
        if isinstance(raw_total_count, int):
            total_count = raw_total_count
        check_runs.extend(raw_check_runs)
        if len(raw_check_runs) < 100:
            break
        page += 1
    return {
        "total_count": total_count if total_count is not None else len(check_runs),
        "check_runs": _latest_check_runs(check_runs),
    }


def _latest_check_runs(check_runs: list[object]) -> list[object]:
    """Keep only the most recent run of each check, as GitHub's own merge box does.

    A rerun is a new check run on the same commit; the superseded run must not
    decide the result. GitHub issues increasing ids, so the highest id wins.
    """
    latest: dict[tuple[str, object], dict[str, object]] = {}
    for item in check_runs:
        check_run = _json_object(item, "GitHub check run")
        app = check_run.get("app")
        app_id = app.get("id") if isinstance(app, dict) else None
        key = (str(check_run.get("name") or "").casefold(), app_id)
        current = latest.get(key)
        if current is None or _check_run_order(check_run) > _check_run_order(current):
            latest[key] = check_run
    return list(latest.values())


def _check_run_order(check_run: dict[str, object]) -> int:
    check_run_id = check_run.get("id")
    if isinstance(check_run_id, int) and not isinstance(check_run_id, bool):
        return check_run_id
    return -1


def _check_run_status(check_run: dict[str, object]) -> MergeTrainCheckStatus:
    status = str(check_run.get("status") or "").strip().lower()
    if status != "completed":
        return "pending" if status else "unknown"
    conclusion = str(check_run.get("conclusion") or "").strip().lower()
    if conclusion in {"success", "neutral", "skipped"}:
        return "pass"
    if conclusion in {"failure", "timed_out", "cancelled", "action_required"}:
        return "fail"
    return "unknown"


def _combine_check_statuses(*statuses: MergeTrainCheckStatus) -> MergeTrainCheckStatus:
    if any(status == "fail" for status in statuses):
        return "fail"
    if any(status == "pending" for status in statuses):
        return "pending"
    if statuses and all(status == "pass" for status in statuses):
        return "pass"
    if any(status == "pass" for status in statuses):
        return "pass"
    return "unknown"


def _stack_collapse_commit_message(
    *,
    collapse_id: str,
    child_pull_request_number: int,
    parent_pull_request_number: int,
) -> str:
    return (
        f"Launchplane stack collapse {collapse_id}: merge PR "
        f"#{child_pull_request_number} into PR #{parent_pull_request_number}"
    )


def _required_value(value: str, message: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(message)
    return normalized


def _github_http_error(*, path: str, status_code: int, error: HTTPError) -> MergeTrainGitHubError:
    message = f"GitHub API request failed for {path}: HTTP {status_code}"
    if status_code == 409:
        return MergeTrainGitHubStaleHeadError(message, status_code=status_code)
    return MergeTrainGitHubError(message, status_code=status_code)
