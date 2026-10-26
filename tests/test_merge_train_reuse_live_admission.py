from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidate,
    build_merge_train_batch_landing_plan,
)
from control_plane.contracts.merge_train_stack_collapse import MergeTrainStackCollapsePlanRecord
from control_plane.contracts.merge_train_structural_provenance import (
    MergeTrainStackCollapseRootProof,
    MergeTrainStructuralProvenance,
)
from control_plane.merge_admission import GuardedMergeAdmission, MergeAdmissionDeniedError
from control_plane.merge_admission_live import LiveMergeAdmissionEvaluator
from control_plane.merge_train import MergeTrainDryRunSnapshot, build_merge_train_dry_run_result
from control_plane.merge_train_controller_run_once import (
    MergeTrainControllerRunOnceEnvelope,
    MergeTrainControllerLeaseContext,
    _advance_passed_candidate_record,
    _lineage_change_retires_landing,
)
from control_plane.merge_train_github import GitHubMergeTrainClient, merge_train_construction_ref
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.merge_train_policy_fixtures import (
    build_test_merge_train_policy_record,
    dependency_repository_policy,
)
from tests.support.repository_evidence import (
    BASE_SHA,
    HEAD_SHA,
    REPOSITORY,
    _EvidenceProvider,
    _repository_evidence,
)
from tests.test_merge_admission_live import (
    _authz_policy_record,
    _EmptyEngineeringReviewStore,
    _queued_pull_request,
    _StaticSnapshotReader,
)
from tests.test_merge_admission_records import _guard_records
from tests.test_merge_train_head_check_reuse import _ReuseTransport


class _AdmissionTransport(_ReuseTransport):
    """The same provider behavior with valid SHA bindings for live admission."""

    def __init__(self) -> None:
        super().__init__()
        self.native_paths: list[str] = []
        self.bindings = {
            "base-main": BASE_SHA,
            "head-1": HEAD_SHA,
            "candidate-after-1": "4" * 40,
            "tree-base": "5" * 40,
            "tree-head-1": "6" * 40,
        }

    def request(self, *, method: str, path: str, body: dict[str, object] | None = None) -> object:
        self.native_paths.append(path)
        path = path.replace(REPOSITORY, "example/merge-train-repo")
        for alias, sha in self.bindings.items():
            path = path.replace(sha, alias)
        path = path.replace("/pulls/2022", "/pulls/1").replace("/issues/2022", "/issues/1")
        return self._bind(super().request(method=method, path=path, body=body))

    def _bind(self, payload: object) -> object:
        if isinstance(payload, dict):
            return {
                key: 2022 if key == "number" and value == 1 else self._bind(value)
                for key, value in payload.items()
            }
        if isinstance(payload, list):
            return [self._bind(value) for value in payload]
        if isinstance(payload, str):
            if payload == "example/merge-train-repo":
                return REPOSITORY
            return self.bindings.get(payload, payload)
        return payload


class ReuseLiveAdmissionTests(unittest.TestCase):
    def test_live_guard_admits_the_reused_tree_with_fresh_source_checks_and_rejects_loss(
        self,
    ) -> None:
        self._assert_live_reuse(has_stack_collapse=False)

    def test_collapsed_root_reuses_fresh_head_checks_without_repeating_an_admission_block(
        self,
    ) -> None:
        self._assert_live_reuse(has_stack_collapse=True)

    def _assert_live_reuse(self, *, has_stack_collapse: bool) -> None:
        policy = build_test_merge_train_policy_record(repository=REPOSITORY)
        candidate_record, landing_record, controller, _ = _guard_records(
            policy_sha256=policy.policy_sha256,
            repository=REPOSITORY,
            pull_request_number=2022,
            base_sha=BASE_SHA,
            head_sha=HEAD_SHA,
            tree_sha="6" * 40,
        )
        transport = _AdmissionTransport()
        client = GitHubMergeTrainClient(transport=transport)
        client.begin_snapshot_pass(
            policy=dependency_repository_policy(20, repository=REPOSITORY), read_only=False
        )
        collapse_record = None
        if has_stack_collapse:
            collapse_record = MergeTrainStackCollapsePlanRecord.model_validate(
                {
                    "record_id": "collapse-record",
                    "source": "test",
                    "updated_at": candidate_record.updated_at,
                    "plan": {
                        "collapse_id": "collapse-plan",
                        "repository": REPOSITORY,
                        "base_branch": "main",
                        "root_pull_request_number": 2022,
                        "root_initial_head_sha": "original-root",
                        "root_head_ref": "feature/root",
                        "policy_key": candidate_record.candidate.policy_key,
                        "policy_sha256": policy.policy_sha256,
                        "status": "ready_for_train",
                        "entries": [
                            {
                                "pull_request_number": 2022,
                                "position": 1,
                                "head_sha": "original-root",
                                "head_ref": "feature/root",
                                "base_ref": "main",
                            },
                            {
                                "pull_request_number": 2023,
                                "position": 2,
                                "head_sha": "child-head",
                                "head_ref": "feature/child",
                                "base_ref": "feature/root",
                            },
                        ],
                        "mutations": [
                            {
                                "child_pull_request_number": 2023,
                                "parent_pull_request_number": 2022,
                                "child_head_sha": "child-head",
                                "expected_parent_head_sha": "original-root",
                                "parent_head_ref": "feature/root",
                                "status": "mutated",
                                "merge_commit_sha": HEAD_SHA,
                            }
                        ],
                        "created_at": candidate_record.updated_at,
                        "updated_at": candidate_record.updated_at,
                    },
                }
            )
            root_proof = MergeTrainStackCollapseRootProof(
                collapse_record_id=collapse_record.record_id,
                collapse_id=collapse_record.plan.collapse_id,
                root_pull_request_number=collapse_record.plan.root_pull_request_number,
                original_root_head_sha=collapse_record.plan.root_initial_head_sha,
                collapsed_root_head_sha=HEAD_SHA,
                collapsed_root_tree_sha=candidate_record.candidate.candidate_tree_sha,
            )
            provenance = candidate_record.candidate.structural_provenance
            assert provenance is not None
            provenance = MergeTrainStructuralProvenance.model_validate(
                provenance.model_dump(exclude={"provenance_sha256", "candidate_sha256"})
                | {"stack_collapse_root": root_proof}
            )
            candidate_record = candidate_record.model_copy(
                update={
                    "candidate": MergeTrainBatchCandidate.model_validate(
                        candidate_record.candidate.model_dump(exclude={"candidate_sha256"})
                        | {"stack_collapse_root": root_proof, "structural_provenance": provenance}
                    )
                }
            )
        reuse = client.read_head_check_reuse(candidate=candidate_record.candidate)
        assert reuse is not None
        candidate = MergeTrainBatchCandidate.model_validate(
            candidate_record.candidate.model_dump()
            | {
                "candidate_ref": merge_train_construction_ref(
                    candidate_record.candidate.candidate_ref
                ),
                "head_check_reuse": reuse,
            }
        )
        candidate_record = candidate_record.model_copy(update={"candidate": candidate})
        plan = build_merge_train_batch_landing_plan(
            candidate=candidate, merge_method="merge", created_at=candidate.created_at
        )
        landing_record = landing_record.model_copy(update={"landing_plan": plan})
        controller = controller.model_copy(
            update={
                "step_payload": {
                    "landing_plan_id": plan.plan_id,
                    "expected_effect_sha": plan.candidate_sha,
                }
            }
        )
        evidence = _repository_evidence()
        evidence = evidence.model_copy(
            update={"target": evidence.target.model_copy(update={"tree_sha": "6" * 40})}
        )
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(Path(directory))
            store.write_merge_train_controller_state_record(controller)
            # A passed candidate really reaches observation before the
            # controller creates its landing plan, including full-CI fallback.
            lease = MergeTrainControllerLeaseContext(record=controller)
            request = MergeTrainControllerRunOnceEnvelope(repository=REPOSITORY, mutate=True)
            with (
                patch.object(lease, "checkpoint"),
                patch(
                    "control_plane.merge_train_controller_run_once._reflow_stale_candidate_record",
                    return_value=(
                        None,
                        build_merge_train_dry_run_result(
                            policy=policy.policy,
                            snapshot=MergeTrainDryRunSnapshot(
                                repository=REPOSITORY,
                                base_branch="main",
                                base_sha=BASE_SHA,
                                pull_requests=(
                                    _queued_pull_request(
                                        number=plan.entries[0].pull_request_number,
                                        head_sha=HEAD_SHA,
                                        created_at=candidate.created_at,
                                    ),
                                ),
                            ),
                            batch_landing=True,
                        ),
                    ),
                ),
            ):
                advanced = _advance_passed_candidate_record(
                    request=request,
                    policy=policy.policy,
                    policy_sha256=policy.policy_sha256,
                    repository_policy=policy.policy.find_repository_policy(
                        repository=REPOSITORY, base_branch="main"
                    ),
                    transport=transport,
                    github_client=client,
                    trace_id="reuse-test",
                    recorded_at="2026-08-11T03:01:00Z",
                    candidate_store=store,
                    landing_store=store,
                    stack_collapse_store=store,
                    passed_candidate_record=candidate_record,
                    lease=lease,
                )
            self.assertEqual(advanced["controller_action"], "plan_landing")
            evaluator = LiveMergeAdmissionEvaluator(
                store=store,
                repository_evidence_provider=_EvidenceProvider(evidence),
                technical_check_client=client,
                policy_record_provider=lambda: policy,
                snapshot_reader=_StaticSnapshotReader(
                    MergeTrainDryRunSnapshot(
                        repository=REPOSITORY,
                        base_branch="main",
                        base_sha=BASE_SHA,
                        pull_requests=(
                            _queued_pull_request(
                                number=2022, head_sha=HEAD_SHA, created_at=candidate.created_at
                            ),
                        ),
                    )
                ),
            )
            guard = GuardedMergeAdmission(
                record_store=store,
                evaluator=evaluator,
                candidate_record=candidate_record,
                landing_plan_record=landing_record,
                controller_state=controller,
                stack_collapse_record=collapse_record,
                trace_id="reuse-test",
                admission_time_provider=lambda: "2026-08-11T03:01:00Z",
            )
            facts: dict[str, Any] = {
                "entry": plan.entries[0],
                "observed_base_sha": BASE_SHA,
                "observed_base_tree_sha": "5" * 40,
                "observed_head_sha": HEAD_SHA,
                "observed_head_tree_sha": "6" * 40,
            }
            with (
                patch(
                    "control_plane.merge_admission_live.read_active_authz_policy_record",
                    return_value=_authz_policy_record(),
                ),
                patch(
                    "control_plane.merge_admission_live.require_engineering_review_decision_store",
                    return_value=_EmptyEngineeringReviewStore(),
                ),
            ):
                # Proposal uses the real guard/evaluator/readiness adapters;
                # there are deliberately no check signals at the candidate SHA.
                transport.native_paths.clear()
                proposal = guard.build_proposal(**facts)
                self.assertEqual(proposal.record.readiness.state, "ready")
                self.assertEqual(proposal.record.readiness.technical_checks.head_sha, HEAD_SHA)
                self.assertTrue(
                    any(f"/commits/{HEAD_SHA}/" in path for path in transport.native_paths)
                )
                self.assertFalse(
                    any(
                        f"/commits/{candidate.candidate_sha}/" in path
                        for path in transport.native_paths
                    )
                )
                if has_stack_collapse:
                    self.assertEqual(proposal.record.structural_result.status, "recorded_rolling")
                    guard.stack_collapse_record = None
                    with self.assertRaises(MergeAdmissionDeniedError) as unproven:
                        guard.build_proposal(**facts)
                    assert unproven.exception.structural_result is not None
                    self.assertIn(
                        "structural_stack_root_unproven",
                        unproven.exception.structural_result.reason_codes,
                    )
                    guard.stack_collapse_record = collapse_record
                transport.check_conclusion = "failure"
                with self.assertRaises(MergeAdmissionDeniedError) as denied:
                    guard.build_proposal(**facts)
                self.assertEqual(denied.exception.reason_code, "head_check_reuse_unavailable")
                self.assertEqual(
                    _lineage_change_retires_landing(
                        reason_code=denied.exception.reason_code,
                        landing_record=landing_record,
                        has_stack_collapse=has_stack_collapse,
                    ),
                    not has_stack_collapse,
                )
                transport.check_conclusion = "success"
                transport.commit_author = {"id": 20, "type": "Bot"}
                with self.assertRaises(MergeAdmissionDeniedError) as source_denied:
                    guard.build_proposal(**facts)
                self.assertEqual(
                    source_denied.exception.reason_code, "head_check_reuse_unavailable"
                )
                transport.commit_author = transport.user
                admission = guard.admit(**facts)
                self.assertEqual(admission.expected_effect_sha, candidate.candidate_sha)
                self.assertEqual(admission.readiness.technical_checks.head_sha, HEAD_SHA)
                self.assertEqual(
                    store.read_merge_admission_record(admission.admission_id), admission
                )
