"""Scoped controller client: durable read evidence and one candidate effect per step."""

from collections.abc import Callable
from typing import Literal, Protocol

from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidate,
    MergeTrainBatchCandidateRecord,
    MergeTrainBatchEntry,
    MergeTrainBatchLandingEntry,
    MergeTrainBatchLandingPlan,
    MergeTrainBatchLandingPlanRecord,
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
from control_plane.merge_admission import GuardedMergeAdmission
from control_plane.merge_train_structural_provenance import (
    ordinary_candidate_is_exact_landing_dependency,
)
from control_plane.ordinary_agent_noop_route import OrdinaryNoOpFinalizationUnavailable
from control_plane.merge_train_github import (
    GitHubMergeTrainClient,
    MergeTrainGitHubStaleHeadError,
    _candidate_with_structural_provenance,
    _validated_model_update,
)


class OrdinaryAgentLandingStep(Protocol):
    def __call__(
        self,
        *,
        candidate_record: MergeTrainBatchCandidateRecord,
        landing_plan_record: MergeTrainBatchLandingPlanRecord,
        entry: MergeTrainBatchLandingEntry,
        semantic_ordinal: int,
        checkpoint: Callable[[MergeTrainBatchLandingEntry], None],
    ) -> MergeTrainBatchLandingEntry: ...


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
        advance_landing_entry: OrdinaryAgentLandingStep | None = None,
        advance_no_op_landing_entry: OrdinaryAgentLandingStep | None = None,
    ) -> None:
        super().__init__(transport=_NoAmbientTransport(), effect_executor=effect_executor)
        self._request = request
        self._snapshot = snapshot
        self._candidate_check = candidate_check
        self._advance_landing_entry = advance_landing_entry
        self._advance_no_op_landing_entry = advance_no_op_landing_entry

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

    def land_batch_candidate(
        self,
        *,
        landing_plan: MergeTrainBatchLandingPlan,
        effect_executor: MergeTrainSemanticEffectExecutor | None = None,
        admission_guard: GuardedMergeAdmission | None = None,
        recorded_at: str = "",
        provider_checkpoint: Callable[
            [MergeTrainBatchLandingPlan, MergeTrainBatchLandingEntry], None
        ]
        | None = None,
        checkpoint: Callable[
            [MergeTrainBatchLandingPlan, MergeTrainBatchLandingEntry, str],
            MergeTrainBatchLandingPlanRecord | None,
        ]
        | None = None,
    ) -> MergeTrainBatchLandingPlan:
        # The inherited hook records legacy provider intent. Ordinary dispatch
        # persists its joined preparation and child attempt inside the injected
        # step before I/O; invoking the legacy hook would create a competing path.
        del provider_checkpoint
        if effect_executor is not None and effect_executor is not self.semantic_effect_executor:
            raise PermissionError("ordinary landing executor cannot be replaced")
        if self._advance_landing_entry is None:
            raise PermissionError("ordinary landing step is not assembled")
        if admission_guard is None:
            raise PermissionError("ordinary landing requires a guarded admission wrapper")
        if checkpoint is None:
            raise PermissionError("ordinary landing requires a durable progress checkpoint")
        if not recorded_at.strip():
            raise ValueError("ordinary landing requires recorded_at")

        landing_record = admission_guard.landing_plan_record
        candidate_record = admission_guard.candidate_record
        self._validate_landing_scope(
            landing_plan=landing_plan,
            landing_record=landing_record,
            candidate_record=candidate_record,
        )
        selected_index, rolling_base_sha, rolling_base_tree_sha = self._next_landing_entry(
            landing_plan=landing_plan, candidate_record=candidate_record
        )
        if selected_index is None:
            return landing_plan
        selected = landing_plan.entries[selected_index]
        provenance = candidate_record.candidate.structural_provenance
        assert provenance is not None
        candidate_step = provenance.steps[selected_index]
        is_no_op = candidate_step.kind == "no_op_already_contained"
        if is_no_op and self._advance_no_op_landing_entry is None:
            raise OrdinaryNoOpFinalizationUnavailable()
        advance_entry = (
            self._advance_no_op_landing_entry if is_no_op else self._advance_landing_entry
        )
        assert advance_entry is not None

        checkpointed_entry: MergeTrainBatchLandingEntry | None = None
        checkpointed_plan: MergeTrainBatchLandingPlan | None = None

        def checkpoint_entry(entry: MergeTrainBatchLandingEntry) -> None:
            nonlocal checkpointed_entry, checkpointed_plan
            if checkpointed_entry is not None:
                raise RuntimeError("ordinary landing entry was checkpointed more than once")
            self._validate_merged_landing_entry(
                planned=selected,
                landed=entry,
                rolling_base_sha=rolling_base_sha,
                rolling_base_tree_sha=rolling_base_tree_sha,
                no_op=is_no_op,
            )
            successor = _validated_model_update(
                landing_plan,
                entries=(
                    *landing_plan.entries[:selected_index],
                    entry,
                    *landing_plan.entries[selected_index + 1 :],
                ),
            )
            persisted = checkpoint(
                successor, entry, "entry_skipped" if is_no_op else "entry_merged"
            )
            if (
                not isinstance(persisted, MergeTrainBatchLandingPlanRecord)
                or persisted.status != "active"
                or persisted.ordinary_job_binding != self.binding
                or persisted.record_id == landing_record.record_id
                or persisted.landing_plan != successor
            ):
                raise RuntimeError(
                    "ordinary landing checkpoint did not persist the exact successor"
                )
            admission_guard.update_landing_plan_record(persisted)
            checkpointed_entry = entry
            checkpointed_plan = persisted.landing_plan

        if not is_no_op:
            # Bind the selected PR in the existing controller admission phase.
            # This records no provider intent; dispatch still requires the
            # joined preparation/admission/child transaction inside the step.
            checkpoint(landing_plan, selected, "merge_entry")
        result = advance_entry(
            candidate_record=candidate_record,
            landing_plan_record=landing_record,
            entry=selected,
            semantic_ordinal=selected.position,
            checkpoint=checkpoint_entry,
        )
        if checkpointed_entry is None or checkpointed_plan is None:
            raise RuntimeError("ordinary landing callback returned before durable checkpoint")
        if result != checkpointed_entry:
            raise RuntimeError("ordinary landing callback result differs from its checkpoint")
        return checkpointed_plan

    def _validate_landing_scope(
        self,
        *,
        landing_plan: MergeTrainBatchLandingPlan,
        landing_record: MergeTrainBatchLandingPlanRecord,
        candidate_record: MergeTrainBatchCandidateRecord,
    ) -> None:
        candidate = candidate_record.candidate
        provenance = candidate.structural_provenance
        request_entries = tuple(
            (item.number, item.head_sha) for item in self._request.pull_requests
        )
        candidate_entries = tuple(
            (entry.pull_request_number, entry.head_sha) for entry in candidate.entries
        )
        landing_entries = tuple(
            (entry.pull_request_number, entry.expected_head_sha) for entry in landing_plan.entries
        )
        expected_ref = build_ordinary_merge_train_candidate_ref(
            binding=self.binding, batch_id=candidate.batch_id
        )
        if (
            landing_record.status != "active"
            or candidate_record.status not in {"active", "superseded"}
            or (
                candidate_record.status == "superseded"
                and not ordinary_candidate_is_exact_landing_dependency(
                    candidate_record=candidate_record,
                    landing_plan_record=landing_record,
                )
            )
            or landing_record.ordinary_job_binding != self.binding
            or candidate_record.ordinary_job_binding != self.binding
            or landing_record.landing_plan != landing_plan
            or candidate.status != "passed"
            or provenance is None
            or not provenance.complete
            or candidate.stack_collapse_root is not None
            or candidate.repository.lower() != self._request.target.repository.lower()
            or candidate.base_branch != self._request.target.base_branch
            or candidate.base_sha != self._request.base_sha
            or candidate.candidate_ref != expected_ref
            or landing_plan.repository != candidate.repository
            or landing_plan.base_branch != candidate.base_branch
            or landing_plan.batch_id != candidate.batch_id
            or landing_plan.candidate_ref != candidate.candidate_ref
            or landing_plan.candidate_sha != candidate.candidate_sha
            or landing_plan.candidate_tree_sha != candidate.candidate_tree_sha
            or landing_plan.candidate_sha256 != candidate.candidate_sha256
            or landing_plan.structural_provenance_sha256 != provenance.provenance_sha256
            or landing_plan.policy_key != candidate.policy_key
            or landing_plan.policy_sha256 != candidate.policy_sha256
            or request_entries != candidate_entries
            or request_entries != landing_entries
            or len(landing_plan.entries) != len(candidate.entries)
            or any(
                entry.expected_base_sha != self._request.base_sha for entry in landing_plan.entries
            )
            or any(entry.merge_method != "merge" for entry in landing_plan.entries)
        ):
            raise MergeTrainGitHubStaleHeadError(
                "ordinary landing plan does not match its finite request and guarded records",
                status_code=409,
            )
        for candidate_entry, landing_entry, step in zip(
            candidate.entries, landing_plan.entries, provenance.steps, strict=True
        ):
            if (
                candidate_entry.position != landing_entry.position
                or landing_entry.expected_head_tree_sha != candidate_entry.head_tree_sha
                or landing_entry.recorded_candidate_parent_sha != step.parent_sha
                or landing_entry.recorded_candidate_parent_tree_sha != step.parent_tree_sha
                or landing_entry.recorded_candidate_result_sha != step.result_sha
                or landing_entry.recorded_candidate_result_tree_sha != step.result_tree_sha
            ):
                raise MergeTrainGitHubStaleHeadError(
                    "ordinary landing entry does not match structural provenance",
                    status_code=409,
                )

    def _next_landing_entry(
        self,
        *,
        landing_plan: MergeTrainBatchLandingPlan,
        candidate_record: MergeTrainBatchCandidateRecord,
    ) -> tuple[int | None, str, str]:
        provenance = candidate_record.candidate.structural_provenance
        assert provenance is not None
        rolling_base_sha = provenance.base_sha
        rolling_base_tree_sha = provenance.base_tree_sha
        selected_index: int | None = None
        planned_seen = False
        for index, entry in enumerate(landing_plan.entries):
            if entry.status in {"merging", "stale", "blocked"}:
                raise MergeTrainGitHubStaleHeadError(
                    "ordinary landing requires recovery for a non-resumable entry state",
                    status_code=409,
                )
            if entry.status == "planned":
                if any(
                    (
                        entry.recorded_rolling_base_sha,
                        entry.recorded_rolling_base_tree_sha,
                        entry.landed_head_sha,
                        entry.landed_head_tree_sha,
                        entry.merge_commit_sha,
                        entry.merge_commit_tree_sha,
                    )
                ):
                    raise MergeTrainGitHubStaleHeadError(
                        "ordinary planned landing entry carries terminal evidence",
                        status_code=409,
                    )
                planned_seen = True
                if selected_index is None:
                    selected_index = index
                continue
            if planned_seen:
                raise MergeTrainGitHubStaleHeadError(
                    "ordinary landing progress is not a contiguous terminal prefix",
                    status_code=409,
                )
            if (
                entry.recorded_rolling_base_sha != rolling_base_sha
                or entry.recorded_rolling_base_tree_sha != rolling_base_tree_sha
                or entry.landed_head_sha != entry.expected_head_sha
                or entry.landed_head_tree_sha != entry.expected_head_tree_sha
                or not entry.merge_commit_sha
                or not entry.merge_commit_tree_sha
            ):
                raise MergeTrainGitHubStaleHeadError(
                    "ordinary landing progress lacks exact rolling-base proof",
                    status_code=409,
                )
            if entry.status == "merged":
                if entry.merge_commit_sha == rolling_base_sha:
                    raise MergeTrainGitHubStaleHeadError(
                        "ordinary merged landing entry did not advance its rolling base",
                        status_code=409,
                    )
                rolling_base_sha = entry.merge_commit_sha
                rolling_base_tree_sha = entry.merge_commit_tree_sha
            elif entry.status == "skipped":
                if (
                    entry.merge_commit_sha != rolling_base_sha
                    or entry.merge_commit_tree_sha != rolling_base_tree_sha
                ):
                    raise MergeTrainGitHubStaleHeadError(
                        "ordinary skipped landing entry changed its rolling base",
                        status_code=409,
                    )
            else:
                raise MergeTrainGitHubStaleHeadError(
                    "ordinary landing entry status is unsupported", status_code=409
                )
        return selected_index, rolling_base_sha, rolling_base_tree_sha

    @staticmethod
    def _validate_merged_landing_entry(
        *,
        planned: MergeTrainBatchLandingEntry,
        landed: MergeTrainBatchLandingEntry,
        rolling_base_sha: str,
        rolling_base_tree_sha: str,
        no_op: bool = False,
    ) -> None:
        immutable_fields = (
            "pull_request_number",
            "position",
            "expected_head_sha",
            "expected_head_tree_sha",
            "expected_base_sha",
            "merge_method",
            "recorded_candidate_parent_sha",
            "recorded_candidate_parent_tree_sha",
            "recorded_candidate_result_sha",
            "recorded_candidate_result_tree_sha",
        )
        if (
            landed.status != ("skipped" if no_op else "merged")
            or any(getattr(landed, name) != getattr(planned, name) for name in immutable_fields)
            or landed.recorded_rolling_base_sha != rolling_base_sha
            or landed.recorded_rolling_base_tree_sha != rolling_base_tree_sha
            or landed.landed_head_sha != planned.expected_head_sha
            or landed.landed_head_tree_sha != planned.expected_head_tree_sha
            or not landed.merge_commit_sha
            or not landed.merge_commit_tree_sha
            or (
                (
                    landed.merge_commit_sha != rolling_base_sha
                    or landed.merge_commit_tree_sha != rolling_base_tree_sha
                )
                if no_op
                else landed.merge_commit_sha == rolling_base_sha
            )
        ):
            raise MergeTrainGitHubStaleHeadError(
                "ordinary landing callback returned incomplete or mismatched merge proof",
                status_code=409,
            )
