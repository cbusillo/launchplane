import unittest
from unittest.mock import Mock

from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidate,
    MergeTrainBatchCandidateRecord,
    MergeTrainBatchEntry,
    MergeTrainBatchLandingEntry,
    MergeTrainBatchLandingPlan,
    MergeTrainBatchLandingPlanRecord,
    build_merge_train_batch_candidate_record,
    build_merge_train_batch_landing_plan,
    build_merge_train_batch_landing_plan_record,
    build_ordinary_merge_train_candidate_ref,
)
from control_plane.contracts.merge_train_controller_state import (
    build_merge_train_controller_state_record,
)
from control_plane.contracts.merge_train_effect import (
    CandidateHeadMergeOutcome,
    MergeTrainSemanticEffectExecutor,
)
from control_plane.contracts.ordinary_agent import OrdinaryAgentPullRequest
from control_plane.contracts.ordinary_agent_session_lifecycle import (
    OrdinaryAgentFiniteRequestRecord,
)
from control_plane.contracts.ordinary_agent_snapshot import (
    OrdinaryAgentCommitIdentity,
    OrdinaryAgentMergeTrainSnapshotResult,
    OrdinaryAgentProtectionEvidence,
    OrdinaryAgentProviderRequestCounts,
    OrdinaryAgentPullRequestHeadIdentity,
)
from control_plane.contracts.merge_train_structural_provenance import (
    MergeTrainRollingStep,
    MergeTrainStackCollapseRootProof,
    MergeTrainStructuralEntryBinding,
    MergeTrainStructuralProvenance,
)
from control_plane.merge_train import MergeTrainDryRunSnapshot, MergeTrainPullRequestSnapshot
from control_plane.merge_admission import GuardedMergeAdmission
from control_plane.merge_train_github import MergeTrainGitHubStaleHeadError
from control_plane.ordinary_agent_merge_train_client import (
    OrdinaryAgentLandingStep,
    OrdinaryAgentMergeTrainClient,
)
from tests.support.ordinary_agent_lifecycle import TARGET


class OrdinaryAgentMergeTrainClientTests(unittest.TestCase):
    def test_restart_resumes_next_entry_without_reset_or_duplicate_merge(self) -> None:
        now = "2026-01-01T00:00:00Z"
        request = OrdinaryAgentFiniteRequestRecord(
            request_id="job-one",
            idempotency_key="job-one",
            principal_id="agent_one",
            session_id="session_one",
            lease_id="lease_one",
            target=TARGET,
            base_sha="a" * 40,
            pull_requests=(
                OrdinaryAgentPullRequest(number=12, head_sha="b" * 40),
                OrdinaryAgentPullRequest(number=13, head_sha="c" * 40),
            ),
            permitted_stack_edit_pull_requests=(),
            refresh_allowance_total=0,
            admitted_at=10,
            expires_at=100,
        )
        snapshot = MergeTrainDryRunSnapshot(
            repository=TARGET.repository,
            base_branch=TARGET.base_branch,
            base_sha=request.base_sha,
            pull_requests=tuple(
                MergeTrainPullRequestSnapshot(number=number, head_sha=head, created_at=now)
                for number, head in ((12, "b" * 40), (13, "c" * 40), (99, "d" * 40))
            ),
        )
        evidence = OrdinaryAgentMergeTrainSnapshotResult(
            snapshot=snapshot,
            base_identity=OrdinaryAgentCommitIdentity(sha=request.base_sha, tree_sha="base-tree"),
            head_identities=tuple(
                OrdinaryAgentPullRequestHeadIdentity(
                    pull_request_number=item.number,
                    identity=OrdinaryAgentCommitIdentity(
                        sha=item.head_sha, tree_sha=f"tree-{item.number}"
                    ),
                )
                for item in snapshot.pull_requests
            ),
            protection=OrdinaryAgentProtectionEvidence(
                source="evaluated_rules", evaluated_rules_sha256="a" * 64, required_checks=()
            ),
            counts=OrdinaryAgentProviderRequestCounts(
                rest_core_requests=1, graphql_requests=1, graphql_points=1
            ),
            snapshot_sha256="b" * 64,
        )
        executor = Mock(spec=MergeTrainSemanticEffectExecutor)
        executor.merge_candidate_head.side_effect = (
            CandidateHeadMergeOutcome("e" * 40, "first-tree", (request.base_sha, "b" * 40)),
            CandidateHeadMergeOutcome(None, "first-tree", ()),
        )

        def client() -> OrdinaryAgentMergeTrainClient:
            return OrdinaryAgentMergeTrainClient(
                request=request,
                effect_executor=executor,
                snapshot=lambda: evidence,
                candidate_check=Mock(),
            )

        first = client()
        candidate = MergeTrainBatchCandidate(
            batch_id="batch",
            repository=TARGET.repository,
            base_branch=TARGET.base_branch,
            base_sha=request.base_sha,
            policy_key="test-policy",
            policy_sha256="a" * 64,
            candidate_ref=build_ordinary_merge_train_candidate_ref(
                binding=first.binding, batch_id="batch"
            ),
            entries=tuple(
                MergeTrainBatchEntry(
                    pull_request_number=item.number, position=index, head_sha=item.head_sha
                )
                for index, item in enumerate(request.pull_requests, 1)
            ),
            created_at=now,
            updated_at=now,
        )
        self.assertEqual(
            tuple(
                item.number
                for item in first.read_merge_train_snapshot(
                    repository=TARGET.repository, base_branch=TARGET.base_branch
                ).pull_requests
            ),
            (12, 13),
        )
        planned_ref = candidate.candidate_ref
        for expected_merges in (0, 1, 2):
            candidate = client().build_batch_candidate(candidate=candidate)
            candidate = MergeTrainBatchCandidate.model_validate_json(candidate.model_dump_json())
            self.assertEqual(executor.merge_candidate_head.call_count, expected_merges)
            self.assertEqual(candidate.candidate_ref, planned_ref)
        executor.prepare_candidate_ref.assert_called_once()
        self.assertEqual(candidate.status, "ready_for_checks")
        assert candidate.structural_provenance is not None
        self.assertTrue(candidate.structural_provenance.complete)
        self.assertEqual(
            tuple(step.kind for step in candidate.structural_provenance.steps),
            ("merge_commit", "no_op_already_contained"),
        )
        client().build_batch_candidate(candidate=candidate)
        self.assertEqual(executor.merge_candidate_head.call_count, 2)
        other = first.binding.model_copy(update={"request_id": "job-two"})
        with self.assertRaises(MergeTrainGitHubStaleHeadError):
            client().build_batch_candidate(
                candidate=candidate.model_copy(
                    update={
                        "candidate_ref": build_ordinary_merge_train_candidate_ref(
                            binding=other, batch_id="batch"
                        )
                    }
                )
            )
        self.assertEqual(executor.merge_candidate_head.call_count, 2)
        with self.assertRaises(PermissionError):
            first.transport.request(method="GET", path="/unscoped")


class OrdinaryAgentLandingStepTests(unittest.TestCase):
    now = "2026-01-01T00:00:00Z"

    def setUp(self) -> None:
        self.request = OrdinaryAgentFiniteRequestRecord(
            request_id="landing-job",
            idempotency_key="landing-job",
            principal_id="agent_one",
            session_id="session_one",
            lease_id="lease_one",
            target=TARGET,
            base_sha="a" * 40,
            pull_requests=(
                OrdinaryAgentPullRequest(number=12, head_sha="b" * 40),
                OrdinaryAgentPullRequest(number=13, head_sha="c" * 40),
            ),
            permitted_stack_edit_pull_requests=(),
            refresh_allowance_total=0,
            admitted_at=10,
            expires_at=100,
        )
        self.executor = Mock(spec=MergeTrainSemanticEffectExecutor)
        self.client_without_step = self.client()
        self.candidate_record, self.landing_record = self.records()

    def client(self, step: OrdinaryAgentLandingStep | None = None) -> OrdinaryAgentMergeTrainClient:
        return OrdinaryAgentMergeTrainClient(
            request=self.request,
            effect_executor=self.executor,
            snapshot=Mock(),
            candidate_check=Mock(),
            advance_landing_entry=step,
        )

    def records(
        self, *, no_op_first: bool = False
    ) -> tuple[MergeTrainBatchCandidateRecord, MergeTrainBatchLandingPlanRecord]:
        base_sha, base_tree = self.request.base_sha, "base-tree"
        first_sha = base_sha if no_op_first else "d" * 40
        first_tree = base_tree if no_op_first else "first-candidate-tree"
        structural_entries = (
            MergeTrainStructuralEntryBinding(
                position=1,
                pull_request_number=12,
                head_sha="b" * 40,
                head_tree_sha="head-tree-12",
            ),
            MergeTrainStructuralEntryBinding(
                position=2,
                pull_request_number=13,
                head_sha="c" * 40,
                head_tree_sha="head-tree-13",
            ),
        )
        steps = (
            MergeTrainRollingStep(
                position=1,
                pull_request_number=12,
                parent_sha=base_sha,
                parent_tree_sha=base_tree,
                head_sha="b" * 40,
                head_tree_sha="head-tree-12",
                result_sha=first_sha,
                result_tree_sha=first_tree,
                kind="no_op_already_contained" if no_op_first else "merge_commit",
            ),
            MergeTrainRollingStep(
                position=2,
                pull_request_number=13,
                parent_sha=first_sha,
                parent_tree_sha=first_tree,
                head_sha="c" * 40,
                head_tree_sha="head-tree-13",
                result_sha="e" * 40,
                result_tree_sha="second-candidate-tree",
                kind="merge_commit",
            ),
        )
        provenance = MergeTrainStructuralProvenance(
            repository=TARGET.repository,
            base_branch=TARGET.base_branch,
            base_sha=base_sha,
            base_tree_sha=base_tree,
            policy_key="test-policy",
            policy_sha256="1" * 64,
            entries=structural_entries,
            steps=steps,
            candidate_sha="e" * 40,
            candidate_tree_sha="second-candidate-tree",
        )
        candidate = MergeTrainBatchCandidate(
            batch_id="landing-batch",
            repository=TARGET.repository,
            base_branch=TARGET.base_branch,
            base_sha=base_sha,
            policy_key="test-policy",
            policy_sha256="1" * 64,
            candidate_ref=build_ordinary_merge_train_candidate_ref(
                binding=self.client_without_step.binding, batch_id="landing-batch"
            ),
            candidate_sha=provenance.candidate_sha,
            candidate_tree_sha=provenance.candidate_tree_sha,
            status="passed",
            entries=tuple(
                MergeTrainBatchEntry(
                    pull_request_number=entry.pull_request_number,
                    position=entry.position,
                    head_sha=entry.head_sha,
                    head_tree_sha=entry.head_tree_sha,
                )
                for entry in structural_entries
            ),
            structural_provenance=provenance,
            required_checks_status="pass",
            created_at=self.now,
            updated_at=self.now,
        )
        candidate_record = build_merge_train_batch_candidate_record(
            candidate=candidate,
            source="test:candidate",
            updated_at=self.now,
            ordinary_job_binding=self.client_without_step.binding,
        )
        landing_record = build_merge_train_batch_landing_plan_record(
            landing_plan=build_merge_train_batch_landing_plan(
                candidate=candidate, merge_method="merge", created_at=self.now
            ),
            source="test:landing",
            updated_at=self.now,
            ordinary_job_binding=self.client_without_step.binding,
        )
        return candidate_record, landing_record

    def guard(
        self,
        *,
        candidate_record: MergeTrainBatchCandidateRecord | None = None,
        landing_record: MergeTrainBatchLandingPlanRecord | None = None,
    ) -> GuardedMergeAdmission:
        return GuardedMergeAdmission(
            record_store=Mock(),
            evaluator=Mock(),
            candidate_record=candidate_record or self.candidate_record,
            landing_plan_record=landing_record or self.landing_record,
            controller_state=build_merge_train_controller_state_record(
                repository=TARGET.repository,
                base_branch=TARGET.base_branch,
                policy_key="test-policy",
                policy_sha256="1" * 64,
                updated_at=self.now,
            ),
            trace_id="landing-test",
        )

    def checkpoint(
        self,
        plan: MergeTrainBatchLandingPlan,
        entry: MergeTrainBatchLandingEntry,
        phase: str,
    ) -> MergeTrainBatchLandingPlanRecord:
        self.assertEqual(phase, "entry_merged")
        self.assertEqual(plan.entries[entry.position - 1], entry)
        return build_merge_train_batch_landing_plan_record(
            landing_plan=plan,
            source=f"test:progress:{entry.position}",
            updated_at=f"2026-01-01T00:00:0{entry.position}Z",
            ordinary_job_binding=self.client_without_step.binding,
        )

    @staticmethod
    def merged_entry(
        entry: MergeTrainBatchLandingEntry, *, rolling_sha: str, rolling_tree: str
    ) -> MergeTrainBatchLandingEntry:
        return entry.model_copy(
            update={
                "status": "merged",
                "recorded_rolling_base_sha": rolling_sha,
                "recorded_rolling_base_tree_sha": rolling_tree,
                "landed_head_sha": entry.expected_head_sha,
                "landed_head_tree_sha": entry.expected_head_tree_sha,
                "merge_commit_sha": str(entry.position + 5) * 40,
                "merge_commit_tree_sha": f"landed-tree-{entry.position}",
            }
        )

    def test_advances_exactly_one_entry_and_restart_uses_proven_rolling_tip(self) -> None:
        calls: list[tuple[int, int, str]] = []

        def step(**kwargs: object) -> MergeTrainBatchLandingEntry:
            entry = kwargs["entry"]
            assert isinstance(entry, MergeTrainBatchLandingEntry)
            ordinal = kwargs["semantic_ordinal"]
            assert isinstance(ordinal, int)
            rolling_sha = self.request.base_sha if entry.position == 1 else "6" * 40
            rolling_tree = "base-tree" if entry.position == 1 else "landed-tree-1"
            calls.append((entry.position, ordinal, rolling_sha))
            result = self.merged_entry(entry, rolling_sha=rolling_sha, rolling_tree=rolling_tree)
            checkpoint = kwargs["checkpoint"]
            assert callable(checkpoint)
            checkpoint(result)
            return result

        first_guard = self.guard()
        first = self.client(step).land_batch_candidate(
            landing_plan=self.landing_record.landing_plan,
            admission_guard=first_guard,
            recorded_at=self.now,
            checkpoint=self.checkpoint,
        )
        self.assertEqual([entry.status for entry in first.entries], ["merged", "planned"])
        self.assertEqual(calls, [(1, 1, self.request.base_sha)])
        self.assertNotEqual(
            first_guard.landing_plan_record.record_id, self.landing_record.record_id
        )

        second_guard = self.guard(landing_record=first_guard.landing_plan_record)
        second = self.client(step).land_batch_candidate(
            landing_plan=first,
            admission_guard=second_guard,
            recorded_at=self.now,
            checkpoint=self.checkpoint,
        )
        self.assertEqual([entry.status for entry in second.entries], ["merged", "merged"])
        self.assertEqual(calls, [(1, 1, self.request.base_sha), (2, 2, "6" * 40)])

        terminal_calls = Mock()
        terminal = self.client(terminal_calls).land_batch_candidate(
            landing_plan=second,
            admission_guard=self.guard(landing_record=second_guard.landing_plan_record),
            recorded_at=self.now,
            checkpoint=self.checkpoint,
        )
        self.assertEqual(terminal, second)
        terminal_calls.assert_not_called()

    def test_requires_callback_and_exact_checkpoint_before_success(self) -> None:
        with self.assertRaisesRegex(PermissionError, "not assembled"):
            self.client_without_step.land_batch_candidate(
                landing_plan=self.landing_record.landing_plan,
                admission_guard=self.guard(),
                recorded_at=self.now,
                checkpoint=self.checkpoint,
            )

        def no_checkpoint(**kwargs: object) -> MergeTrainBatchLandingEntry:
            entry = kwargs["entry"]
            assert isinstance(entry, MergeTrainBatchLandingEntry)
            return self.merged_entry(
                entry, rolling_sha=self.request.base_sha, rolling_tree="base-tree"
            )

        with self.assertRaisesRegex(RuntimeError, "before durable checkpoint"):
            self.client(no_checkpoint).land_batch_candidate(
                landing_plan=self.landing_record.landing_plan,
                admission_guard=self.guard(),
                recorded_at=self.now,
                checkpoint=self.checkpoint,
            )

        def wrong_successor(
            plan: MergeTrainBatchLandingPlan,
            entry: MergeTrainBatchLandingEntry,
            phase: str,
        ) -> MergeTrainBatchLandingPlanRecord:
            del plan, entry, phase
            return self.landing_record

        def checkpoint_once(**kwargs: object) -> MergeTrainBatchLandingEntry:
            entry = kwargs["entry"]
            assert isinstance(entry, MergeTrainBatchLandingEntry)
            result = self.merged_entry(
                entry, rolling_sha=self.request.base_sha, rolling_tree="base-tree"
            )
            callback = kwargs["checkpoint"]
            assert callable(callback)
            callback(result)
            return result

        with self.assertRaisesRegex(RuntimeError, "exact successor"):
            self.client(checkpoint_once).land_batch_candidate(
                landing_plan=self.landing_record.landing_plan,
                admission_guard=self.guard(),
                recorded_at=self.now,
                checkpoint=wrong_successor,
            )

    def test_rejects_no_op_stack_collapse_and_recovery_states_before_callback(self) -> None:
        callback = Mock()
        no_op_candidate, no_op_landing = self.records(no_op_first=True)
        with self.assertRaisesRegex(PermissionError, "joined no-op"):
            self.client(callback).land_batch_candidate(
                landing_plan=no_op_landing.landing_plan,
                admission_guard=self.guard(
                    candidate_record=no_op_candidate, landing_record=no_op_landing
                ),
                recorded_at=self.now,
                checkpoint=self.checkpoint,
            )

        root = MergeTrainStackCollapseRootProof(
            collapse_record_id="collapse-record",
            collapse_id="collapse",
            root_pull_request_number=12,
            original_root_head_sha="b" * 40,
            collapsed_root_head_sha="f" * 40,
        )
        candidate = self.candidate_record.candidate
        assert candidate.structural_provenance is not None
        stack_provenance = candidate.structural_provenance.model_copy(
            update={"stack_collapse_root": root}
        )
        stack_candidate = candidate.model_copy(
            update={"stack_collapse_root": root, "structural_provenance": stack_provenance}
        )
        stack_record = self.candidate_record.model_copy(update={"candidate": stack_candidate})
        with self.assertRaisesRegex(MergeTrainGitHubStaleHeadError, "guarded records"):
            self.client(callback).land_batch_candidate(
                landing_plan=self.landing_record.landing_plan,
                admission_guard=self.guard(candidate_record=stack_record),
                recorded_at=self.now,
                checkpoint=self.checkpoint,
            )

        for status in ("merging", "stale", "blocked"):
            with self.subTest(status=status):
                entry = self.landing_record.landing_plan.entries[0].model_copy(
                    update={"status": status}
                )
                plan = self.landing_record.landing_plan.model_copy(
                    update={"entries": (entry, self.landing_record.landing_plan.entries[1])}
                )
                record = self.landing_record.model_copy(update={"landing_plan": plan})
                with self.assertRaisesRegex(MergeTrainGitHubStaleHeadError, "requires recovery"):
                    self.client(callback).land_batch_candidate(
                        landing_plan=plan,
                        admission_guard=self.guard(landing_record=record),
                        recorded_at=self.now,
                        checkpoint=self.checkpoint,
                    )
        callback.assert_not_called()

    def test_rejects_broken_rolling_proof_and_callback_identity_drift(self) -> None:
        first = self.merged_entry(
            self.landing_record.landing_plan.entries[0],
            rolling_sha="wrong-base",
            rolling_tree="base-tree",
        )
        plan = self.landing_record.landing_plan.model_copy(
            update={"entries": (first, self.landing_record.landing_plan.entries[1])}
        )
        record = self.landing_record.model_copy(update={"landing_plan": plan})
        callback = Mock()
        with self.assertRaisesRegex(MergeTrainGitHubStaleHeadError, "rolling-base proof"):
            self.client(callback).land_batch_candidate(
                landing_plan=plan,
                admission_guard=self.guard(landing_record=record),
                recorded_at=self.now,
                checkpoint=self.checkpoint,
            )
        callback.assert_not_called()

        skipped = self.merged_entry(
            self.landing_record.landing_plan.entries[0],
            rolling_sha=self.request.base_sha,
            rolling_tree="base-tree",
        ).model_copy(update={"status": "skipped"})
        skipped_plan = self.landing_record.landing_plan.model_copy(
            update={"entries": (skipped, self.landing_record.landing_plan.entries[1])}
        )
        skipped_record = self.landing_record.model_copy(update={"landing_plan": skipped_plan})
        with self.assertRaisesRegex(MergeTrainGitHubStaleHeadError, "skipped landing entry"):
            self.client(callback).land_batch_candidate(
                landing_plan=skipped_plan,
                admission_guard=self.guard(landing_record=skipped_record),
                recorded_at=self.now,
                checkpoint=self.checkpoint,
            )
        callback.assert_not_called()

        def drift(**kwargs: object) -> MergeTrainBatchLandingEntry:
            entry = kwargs["entry"]
            assert isinstance(entry, MergeTrainBatchLandingEntry)
            result = self.merged_entry(
                entry.model_copy(update={"expected_head_sha": "9" * 40}),
                rolling_sha=self.request.base_sha,
                rolling_tree="base-tree",
            )
            checkpoint = kwargs["checkpoint"]
            assert callable(checkpoint)
            checkpoint(result)
            return result

        with self.assertRaisesRegex(MergeTrainGitHubStaleHeadError, "mismatched merge proof"):
            self.client(drift).land_batch_candidate(
                landing_plan=self.landing_record.landing_plan,
                admission_guard=self.guard(),
                recorded_at=self.now,
                checkpoint=self.checkpoint,
            )
