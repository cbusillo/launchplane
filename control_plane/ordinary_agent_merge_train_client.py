"""Scoped controller client: durable read evidence and one candidate effect per step."""

from collections.abc import Callable
from typing import Literal

from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidate,
    MergeTrainBatchEntry,
    build_ordinary_merge_train_candidate_ref,
)
from control_plane.contracts.merge_train_effect import (
    CandidateHeadMergeEffect,
    CandidateRefPrepareEffect,
    MergeTrainEffectLineage,
    MergeTrainSemanticEffectExecutor,
)
from control_plane.contracts.merge_train_structural_provenance import MergeTrainRollingStep
from control_plane.contracts.ordinary_agent_session_lifecycle import (
    OrdinaryAgentFiniteRequestRecord,
    OrdinaryAgentJobBinding,
)
from control_plane.contracts.ordinary_agent_snapshot import (
    OrdinaryAgentCandidateCheckResult,
    OrdinaryAgentMergeTrainSnapshotResult,
)
from control_plane.merge_train import MergeTrainDryRunSnapshot
from control_plane.merge_train_github import (
    GitHubMergeTrainClient,
    MergeTrainGitHubStaleHeadError,
    _candidate_with_structural_provenance,
    _validated_model_update,
)


class _NoAmbientTransport:
    def request(self, *, method: str, path: str, body: dict[str, object] | None = None) -> object:
        raise PermissionError("ordinary controller requires a scoped provider operation")


class OrdinaryAgentMergeTrainClient(GitHubMergeTrainClient):
    def __init__(
        self,
        *,
        request: OrdinaryAgentFiniteRequestRecord,
        effect_executor: MergeTrainSemanticEffectExecutor,
        snapshot: Callable[[], OrdinaryAgentMergeTrainSnapshotResult],
        candidate_check: Callable[[str], OrdinaryAgentCandidateCheckResult],
    ) -> None:
        super().__init__(transport=_NoAmbientTransport(), effect_executor=effect_executor)
        self._request = request
        self._snapshot = snapshot
        self._candidate_check = candidate_check

    @property
    def binding(self) -> OrdinaryAgentJobBinding:
        return OrdinaryAgentJobBinding(
            request_id=self._request.request_id,
            binding_revision=self._request.binding_revision,
            scope_sha256=self._request.scope_sha256,
        )

    def _evidence(self) -> OrdinaryAgentMergeTrainSnapshotResult:
        evidence = self._snapshot()
        snapshot = evidence.snapshot
        target = self._request.target
        expected = {item.number: item.head_sha for item in self._request.pull_requests}
        observed = {item.number: item.head_sha for item in snapshot.pull_requests}
        if (
            snapshot.repository.lower() != target.repository.lower()
            or snapshot.base_branch != target.base_branch
            or snapshot.base_sha != self._request.base_sha
            or any(observed.get(number) != head for number, head in expected.items())
        ):
            raise MergeTrainGitHubStaleHeadError("ordinary snapshot does not match finite request")
        return evidence

    def read_merge_train_snapshot(
        self, *, repository: str, base_branch: str
    ) -> MergeTrainDryRunSnapshot:
        target = self._request.target
        if repository.lower() != target.repository.lower() or base_branch != target.base_branch:
            raise PermissionError("ordinary snapshot target mismatch")
        snapshot = self._evidence().snapshot
        by_number = {item.number: item for item in snapshot.pull_requests}
        # Preserve the finite request's exact order and exclude unrelated PRs.
        return snapshot.model_copy(
            update={
                "pull_requests": tuple(
                    by_number[item.number] for item in self._request.pull_requests
                )
            }
        )

    def build_batch_candidate(
        self,
        *,
        candidate: MergeTrainBatchCandidate,
        effect_executor: MergeTrainSemanticEffectExecutor | None = None,
        checkpoint: Callable[[MergeTrainBatchCandidate, MergeTrainBatchEntry | None, str], None]
        | None = None,
    ) -> MergeTrainBatchCandidate:
        if effect_executor is not None and effect_executor is not self.semantic_effect_executor:
            raise PermissionError("ordinary candidate executor cannot be replaced")
        evidence = self._evidence()
        expected_entries = tuple(
            (item.number, item.head_sha) for item in self._request.pull_requests
        )
        if (
            candidate.repository.lower() != self._request.target.repository.lower()
            or candidate.base_branch != self._request.target.base_branch
            or candidate.base_sha != self._request.base_sha
            or tuple((entry.pull_request_number, entry.head_sha) for entry in candidate.entries)
            != expected_entries
            or candidate.candidate_ref
            != build_ordinary_merge_train_candidate_ref(
                binding=self.binding, batch_id=candidate.batch_id
            )
        ):
            raise MergeTrainGitHubStaleHeadError("ordinary candidate scope mismatch")
        identities = {item.pull_request_number: item.identity for item in evidence.head_identities}
        entries = tuple(
            _validated_model_update(
                entry, head_tree_sha=identities[entry.pull_request_number].tree_sha
            )
            for entry in candidate.entries
        )
        candidate = _validated_model_update(candidate, entries=entries)
        lineage = MergeTrainEffectLineage(
            repository=candidate.repository,
            base_branch=candidate.base_branch,
            batch_id=candidate.batch_id,
        )
        if not candidate.candidate_sha:
            if checkpoint is not None:
                checkpoint(candidate, None, "prepare_candidate_ref")
            self.semantic_effect_executor.prepare_candidate_ref(
                CandidateRefPrepareEffect(
                    lineage=lineage,
                    candidate_ref=candidate.candidate_ref,
                    base_sha=candidate.base_sha,
                )
            )
            # The executor returned only after exact provider proof and history.
            # Core persists this full marker before yielding this normal step.
            return _validated_model_update(
                candidate,
                status="building",
                candidate_sha=candidate.base_sha,
                candidate_tree_sha=evidence.base_identity.tree_sha,
            )
        steps = candidate.structural_provenance.steps if candidate.structural_provenance else ()
        if len(steps) == len(entries):
            return _validated_model_update(candidate, status="ready_for_checks")
        entry = entries[len(steps)]
        parent_sha, parent_tree = candidate.candidate_sha, candidate.candidate_tree_sha
        if not parent_tree or (
            not steps
            and (parent_sha != candidate.base_sha or parent_tree != evidence.base_identity.tree_sha)
        ):
            raise MergeTrainGitHubStaleHeadError(
                "ordinary candidate progress lacks exact base proof"
            )
        if checkpoint is not None:
            checkpoint(candidate, entry, "merge_candidate_entry")
        outcome = self.semantic_effect_executor.merge_candidate_head(
            CandidateHeadMergeEffect(
                lineage=lineage,
                candidate_ref=candidate.candidate_ref,
                rolling_parent_sha=parent_sha,
                pull_request_number=entry.pull_request_number,
                head_sha=entry.head_sha,
            )
        )
        kind: Literal["merge_commit", "no_op_already_contained"]
        if outcome.result_sha is None:
            if outcome.result_tree_sha != parent_tree or outcome.parent_shas:
                raise MergeTrainGitHubStaleHeadError(
                    "ordinary no-op lacks completed containment proof"
                )
            result_sha, result_tree = parent_sha, parent_tree
            kind = "no_op_already_contained"
        else:
            if not outcome.result_tree_sha or outcome.parent_shas != (parent_sha, entry.head_sha):
                raise MergeTrainGitHubStaleHeadError("ordinary merge lacks exact commit proof")
            result_sha, result_tree = outcome.result_sha, outcome.result_tree_sha
            kind = "merge_commit"
        step = MergeTrainRollingStep(
            position=entry.position,
            pull_request_number=entry.pull_request_number,
            parent_sha=parent_sha,
            parent_tree_sha=parent_tree,
            head_sha=entry.head_sha,
            head_tree_sha=entry.head_tree_sha,
            result_sha=result_sha,
            result_tree_sha=result_tree,
            kind=kind,
        )
        progress = _candidate_with_structural_provenance(
            candidate=candidate,
            candidate_sha=result_sha,
            candidate_tree_sha=result_tree,
            rolling_steps=(*steps, step),
        )
        return _validated_model_update(
            progress, status="ready_for_checks" if len(steps) + 1 == len(entries) else "building"
        )

    def observe_batch_candidate_checks(
        self, *, candidate: MergeTrainBatchCandidate
    ) -> MergeTrainBatchCandidate:
        observation = self._candidate_check(candidate.candidate_sha)
        if observation.candidate_identity.sha != candidate.candidate_sha:
            raise MergeTrainGitHubStaleHeadError("ordinary checks observe a different candidate")
        status = {"pass": "passed", "fail": "failed"}.get(observation.status, "ready_for_checks")
        return _validated_model_update(
            candidate, required_checks_status=observation.status, status=status
        )
