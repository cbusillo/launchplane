from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, cast
import unittest
from unittest.mock import Mock, patch

from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidate,
    build_merge_train_batch_landing_plan,
)
from control_plane.merge_admission import GuardedMergeAdmission, MergeAdmissionDeniedError
from control_plane.merge_admission_live import LiveMergeAdmissionEvaluator
from control_plane.merge_train import MergeTrainDryRunSnapshot
from control_plane.merge_train_controller_run_once import (
    MergeTrainControllerRunOnceEnvelope,
    MergeTrainControllerLeaseContext,
    _advance_passed_candidate_record,
    _lineage_change_retires_landing,
)
from control_plane.merge_train_github import GitHubMergeTrainClient, merge_train_construction_ref
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.merge_train_policy_fixtures import build_test_merge_train_policy_record
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
            lease = cast(
                MergeTrainControllerLeaseContext,
                cast(object, SimpleNamespace(record=controller, checkpoint=Mock())),
            )
            request = MergeTrainControllerRunOnceEnvelope(repository=REPOSITORY, mutate=True)
            with patch(
                "control_plane.merge_train_controller_run_once._reflow_stale_candidate_record",
                return_value=None,
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
                transport.check_conclusion = "failure"
                with self.assertRaises(MergeAdmissionDeniedError) as denied:
                    guard.build_proposal(**facts)
                self.assertEqual(denied.exception.reason_code, "head_check_reuse_unavailable")
                self.assertTrue(
                    _lineage_change_retires_landing(
                        reason_code=denied.exception.reason_code,
                        landing_record=landing_record,
                        has_stack_collapse=False,
                    )
                )
                transport.check_conclusion = "success"
                admission = guard.admit(**facts)
                self.assertEqual(admission.expected_effect_sha, candidate.candidate_sha)
                self.assertEqual(admission.readiness.technical_checks.head_sha, HEAD_SHA)
                self.assertEqual(
                    store.read_merge_admission_record(admission.admission_id), admission
                )
