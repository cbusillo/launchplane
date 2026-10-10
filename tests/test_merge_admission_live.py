from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from control_plane.contracts.merge_train_branch_refresh_record import (
    build_merge_train_branch_refresh_record,
)
from control_plane.contracts.merge_train_policy import MergeTrainPolicyRecord
from tests.test_merge_train_dependency_updates import INDIRECT_PATCH
from tests.test_merge_train_dependency_refresh import _change
from tests.test_merge_train_github import (
    _github_pull_request,
    _github_branch,
    _github_commit,
    _combined_status,
    _conversation_rule,
    _check_run,
)

from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.repository_evidence import (
    RepositoryEvidence,
    RepositoryTargetReference,
)
from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidateRecord,
    MergeTrainBatchHeldOutEntry,
    MergeTrainBatchLandingPlanRecord,
    build_merge_train_batch_landing_plan,
)
from control_plane.contracts.merge_train_controller_state import MergeTrainControllerStateRecord
from control_plane.contracts.merge_train_structural_provenance import (
    MergeTrainStructuralCandidateResult,
    MergeTrainStructuralEntryObservation,
)
from control_plane.contracts.ordinary_agent_session_lifecycle import OrdinaryAgentJobBinding
from control_plane.merge_admission import MergeAdmissionDeniedError, MergeAdmissionEvaluation
from control_plane.merge_admission_live import LiveMergeAdmissionEvaluator
from control_plane.merge_train import (
    MergeTrainDryRunSnapshot,
    MergeTrainPullRequestSnapshot,
    MergeTrainReviewConversations,
)
from control_plane.merge_train_github import RecordingMergeTrainGitHubTransport
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.tenant_admission_controller import (
    TenantAdmissionControllerGitHubClient,
    TenantAdmissionRequiredTechnicalCheck,
    TenantAdmissionTechnicalCheckSignal,
    TenantAdmissionTechnicalChecks,
)
from tests.merge_train_policy_fixtures import build_test_merge_train_policy_record
from tests.test_merge_admission_records import _guard_records
from tests.support.repository_evidence import (
    BASE_SHA as OWNER_BASE_SHA,
    HEAD_SHA as OWNER_HEAD_SHA,
    REPOSITORY as OWNER_REPOSITORY,
    TREE_SHA as OWNER_TREE_SHA,
    _EvidenceProvider,
    _repository_evidence,
)
from tests.test_merge_readiness import (
    BASE_SHA,
    HEAD_SHA,
    REPOSITORY,
    TREE_SHA,
    _policy_fingerprints,
)
from tests.support.merge_train import labeled_by


class _StaticSnapshotReader:
    def __init__(self, snapshot: MergeTrainDryRunSnapshot) -> None:
        self.snapshot = snapshot
        self.read_count = 0

    def read_merge_train_snapshot(
        self,
        *,
        repository: str,
        base_branch: str,
    ) -> MergeTrainDryRunSnapshot:
        self.read_count += 1
        if repository != self.snapshot.repository or base_branch != self.snapshot.base_branch:
            raise AssertionError("unexpected merge queue scope")
        return self.snapshot


class _UnusedRepositoryEvidenceProvider:
    def resolve(
        self,
        target: RepositoryTargetReference,
    ) -> RepositoryEvidence:
        raise AssertionError(f"queue drift should fail before repository evidence: {target}")


class _QueueAccepted(RuntimeError):
    pass


class _TechnicalCheckClient(TenantAdmissionControllerGitHubClient):
    def __init__(self) -> None:
        super().__init__(transport=RecordingMergeTrainGitHubTransport())

    def read_technical_checks(
        self,
        *,
        repository: str,
        base_branch: str,
        base_sha: str,
        head_sha: str,
        evaluated_at: str,
    ) -> TenantAdmissionTechnicalChecks:
        return TenantAdmissionTechnicalChecks(
            head_sha=head_sha,
            base_sha=base_sha,
            strict=False,
            status="unavailable",
            evaluated_at=evaluated_at,
        )


class _PassingTechnicalCheckClient(TenantAdmissionControllerGitHubClient):
    def __init__(self) -> None:
        super().__init__(transport=RecordingMergeTrainGitHubTransport())

    def read_technical_checks(
        self,
        *,
        repository: str,
        base_branch: str,
        base_sha: str,
        head_sha: str,
        evaluated_at: str,
    ) -> TenantAdmissionTechnicalChecks:
        return TenantAdmissionTechnicalChecks(
            head_sha=head_sha,
            base_sha=base_sha,
            strict=False,
            status="pass",
            required_checks=(TenantAdmissionRequiredTechnicalCheck(name="ci-gate"),),
            signals=(
                TenantAdmissionTechnicalCheckSignal(
                    source="check_run",
                    name="ci-gate",
                    state="pass",
                ),
            ),
            evaluated_at=evaluated_at,
        )


class _EmptyEngineeringReviewStore:
    @staticmethod
    def list_engineering_review_run_records(**_filters: object) -> tuple[()]:
        return ()

    @staticmethod
    def list_engineering_review_decision_records(**_filters: object) -> tuple[()]:
        return ()

    @staticmethod
    def list_engineering_review_authority_records(**_filters: object) -> tuple[()]:
        return ()


def _queued_pull_request(
    *,
    number: int,
    head_sha: str,
    created_at: str,
) -> MergeTrainPullRequestSnapshot:
    return MergeTrainPullRequestSnapshot(
        number=number,
        created_at=created_at,
        labels=("ready-to-merge",),
        label_actors=labeled_by(("ready-to-merge",), "repo_owner"),
        actor_role="repo_owner",
        head_sha=head_sha,
        base_sha=BASE_SHA,
        base_ref="main",
        mergeable="mergeable",
        required_checks_status="pass",
    )


def _ordinary_no_op_records() -> tuple[
    MergeTrainBatchCandidateRecord,
    MergeTrainBatchLandingPlanRecord,
    MergeTrainControllerStateRecord,
    MergeTrainStructuralCandidateResult,
    str,
]:
    candidate_record, landing_record, controller_state, structural_result = _guard_records()
    candidate = candidate_record.candidate
    provenance = candidate.structural_provenance
    assert provenance is not None
    step = provenance.steps[0]
    no_op_step = step.model_copy(
        update={
            "result_sha": step.parent_sha,
            "result_tree_sha": step.parent_tree_sha,
            "kind": "no_op_already_contained",
        }
    )
    provenance_payload = provenance.model_dump()
    provenance_payload.update(
        {
            "steps": (no_op_step,),
            "candidate_sha": step.parent_sha,
            "candidate_tree_sha": step.parent_tree_sha,
            "provenance_sha256": "",
            "candidate_sha256": "",
        }
    )
    no_op_provenance = type(provenance).model_validate(provenance_payload)
    candidate_payload = candidate.model_dump()
    candidate_payload.update(
        {
            "candidate_sha": step.parent_sha,
            "candidate_tree_sha": step.parent_tree_sha,
            "candidate_sha256": "",
            "structural_provenance": no_op_provenance,
        }
    )
    no_op_candidate = type(candidate).model_validate(candidate_payload)
    binding = OrdinaryAgentJobBinding(
        request_id="ordinary-no-op-request",
        scope_sha256="d" * 64,
        binding_revision=1,
    )
    no_op_plan = build_merge_train_batch_landing_plan(
        candidate=no_op_candidate,
        merge_method="merge",
        created_at=landing_record.landing_plan.created_at,
    )
    return (
        candidate_record.model_copy(
            update={"ordinary_job_binding": binding, "candidate": no_op_candidate}
        ),
        landing_record.model_copy(
            update={"ordinary_job_binding": binding, "landing_plan": no_op_plan}
        ),
        controller_state.model_copy(update={"ordinary_job_binding": binding}),
        structural_result,
        step.parent_tree_sha,
    )


def _authz_policy_record() -> LaunchplaneAuthzPolicyRecord:
    return LaunchplaneAuthzPolicyRecord(
        record_id="authz-policy-live-owner-tests",
        source="test",
        updated_at="2026-08-11T03:00:00Z",
        policy=LaunchplaneAuthzPolicy(),
    )


def _owner_repository_evidence(
    *,
    head_sha: str = OWNER_HEAD_SHA,
    tree_sha: str = OWNER_TREE_SHA,
) -> RepositoryEvidence:
    evidence = _repository_evidence(head=head_sha)
    return evidence.model_copy(
        update={
            "target": evidence.target.model_copy(update={"tree_sha": tree_sha}),
        }
    )


def _evaluate_live(
    *,
    store: FilesystemRecordStore,
    provider: _EvidenceProvider,
    evidence: RepositoryEvidence,
    extra_pull_requests: tuple[MergeTrainPullRequestSnapshot, ...] = (),
    held_out: tuple[MergeTrainBatchHeldOutEntry, ...] = (),
    target_conversations: MergeTrainReviewConversations | None = None,
) -> MergeAdmissionEvaluation:
    policy_record = build_test_merge_train_policy_record(repository=OWNER_REPOSITORY)
    candidate_record, landing_record, controller_state, _ = _guard_records(
        policy_sha256=policy_record.policy_sha256,
        repository=OWNER_REPOSITORY,
        pull_request_number=2022,
        base_sha=OWNER_BASE_SHA,
        head_sha=evidence.target.head_sha,
        tree_sha=evidence.target.tree_sha,
    )
    candidate_record = candidate_record.model_copy(
        update={"candidate": candidate_record.candidate.model_copy(update={"held_out": held_out})}
    )
    evaluator = LiveMergeAdmissionEvaluator(
        store=store,
        repository_evidence_provider=provider,
        technical_check_client=_PassingTechnicalCheckClient(),
        policy_record_provider=lambda: policy_record,
        snapshot_reader=_StaticSnapshotReader(
            MergeTrainDryRunSnapshot(
                repository=OWNER_REPOSITORY,
                base_branch="main",
                base_sha=OWNER_BASE_SHA,
                pull_requests=(
                    _queued_pull_request(
                        number=2022,
                        head_sha=evidence.target.head_sha,
                        created_at="2026-08-11T03:00:00Z",
                    ).model_copy(update={"review_conversations": target_conversations}),
                    *extra_pull_requests,
                ),
            )
        ),
    )
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
        return evaluator.evaluate(
            candidate_record=candidate_record,
            landing_plan_record=landing_record,
            entry=landing_record.landing_plan.entries[0],
            observed_base_sha=OWNER_BASE_SHA,
            observed_base_tree_sha="5" * 40,
            observed_head_sha=evidence.target.head_sha,
            observed_head_tree_sha=evidence.target.tree_sha,
            controller_state=controller_state,
            expected_lease_owner=controller_state.lease_owner,
            stack_collapse_record=None,
            evaluated_at="2026-08-11T03:01:00Z",
        )


class LiveMergeAdmissionRealStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        # The dependency refresh provider fixture has no CODEOWNERS.
        fixture = patch("control_plane.merge_train_codeowners._read_patterns", return_value=())
        fixture.start()
        self.addCleanup(fixture.stop)

    def test_merge_admission_needs_no_retired_owner_or_impact_records(self) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            evidence = _repository_evidence()
            result = _evaluate_live(
                store=store, provider=_EvidenceProvider(evidence), evidence=evidence
            )
        self.assertEqual(result.readiness.state, "ready")
        self.assertEqual(result.structural_result.status, "exact")
        self.assertEqual(result.readiness.owner_facets, ())

    def test_service_landing_excludes_a_blocked_older_failing_pull_request(self) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            evidence = _repository_evidence()
            held = _queued_pull_request(
                number=2021, head_sha="d" * 40, created_at="2026-08-11T02:59:00Z"
            ).model_copy(
                update={
                    "labels": ("ready-to-merge", "MERGE-BLOCKED"),
                    "required_checks_status": "fail",
                }
            )
            result = _evaluate_live(
                store=store,
                provider=_EvidenceProvider(evidence),
                evidence=evidence,
                extra_pull_requests=(held,),
            )
        self.assertEqual(result.readiness.state, "ready")
        self.assertEqual(result.structural_result.status, "exact")

    def test_landing_rediscovers_a_dependabot_update_after_a_recorded_refresh(self) -> None:
        bot_id = 49699333
        old_head = "1" * 40
        record = build_test_merge_train_policy_record(repository=OWNER_REPOSITORY)
        policy_payload = record.policy.model_dump()
        policy_payload["policies"][0]["enqueue"].update(
            {
                "trusted_automation_github_user_ids": [bot_id],
                "dependency_update_github_user_ids": [bot_id],
            }
        )
        policy_record = MergeTrainPolicyRecord.model_validate(
            record.model_dump() | {"policy": policy_payload, "policy_sha256": ""}
        )
        evidence = _repository_evidence()
        candidate_record, landing_record, controller_state, _ = _guard_records(
            policy_sha256=policy_record.policy_sha256,
            repository=OWNER_REPOSITORY,
            pull_request_number=2022,
            base_sha=OWNER_BASE_SHA,
            head_sha=evidence.target.head_sha,
            tree_sha=evidence.target.tree_sha,
        )
        pull_request = _github_pull_request(
            2022,
            head_sha=evidence.target.head_sha,
            base_sha=OWNER_BASE_SHA,
            repository=OWNER_REPOSITORY,
            author_association="CONTRIBUTOR",
        )
        pull_request["labels"] = []
        pull_request["user"] = {"id": bot_id, "login": "dependabot[bot]", "type": "Bot"}
        refresh = _github_commit(1234, "Merge main", sha=evidence.target.head_sha) | {
            "parents": [{"sha": old_head}, {"sha": OWNER_BASE_SHA}],
        }
        for recorded in (False, True):
            with self.subTest(recorded=recorded), TemporaryDirectory() as directory:
                store = FilesystemRecordStore(state_dir=Path(directory))
                if recorded:
                    store.write_merge_train_branch_refresh_record(
                        build_merge_train_branch_refresh_record(
                            repository=OWNER_REPOSITORY,
                            base_branch="main",
                            pull_request_number=2022,
                            expected_head_sha=old_head,
                            result_head_sha=evidence.target.head_sha,
                            merged_base_sha=OWNER_BASE_SHA,
                            requested_at=datetime.now(timezone.utc),
                        )
                    )
                responses: tuple[object, ...] = (
                    _github_branch(sha=OWNER_BASE_SHA),
                    [pull_request],
                    pull_request,
                    {"permission": "read"},
                    [_github_commit(bot_id, INDIRECT_PATCH, sha=old_head), refresh],
                    *(({"status": "identical"}, _change(), _change(), []) if recorded else ()),
                    _combined_status(),
                    {"check_runs": [_check_run("completed", "success")]},
                    _conversation_rule(),
                    [],  # no active branch rules
                )
                client = _PassingTechnicalCheckClient()
                client.transport = RecordingMergeTrainGitHubTransport(responses=responses)
                evaluator = LiveMergeAdmissionEvaluator(
                    store=store,
                    repository_evidence_provider=_EvidenceProvider(evidence),
                    technical_check_client=client,
                    policy_record_provider=lambda: policy_record,
                )
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

                    def evaluate() -> MergeAdmissionEvaluation:
                        return evaluator.evaluate(
                            candidate_record=candidate_record,
                            landing_plan_record=landing_record,
                            entry=landing_record.landing_plan.entries[0],
                            observed_base_sha=OWNER_BASE_SHA,
                            observed_base_tree_sha="5" * 40,
                            observed_head_sha=evidence.target.head_sha,
                            observed_head_tree_sha=evidence.target.tree_sha,
                            controller_state=controller_state,
                            expected_lease_owner=controller_state.lease_owner,
                            stack_collapse_record=None,
                            evaluated_at="2026-08-11T03:01:00Z",
                        )

                    if recorded:
                        result = evaluate()
                        self.assertEqual(result.readiness.state, "ready")
                    else:
                        with self.assertRaisesRegex(MergeAdmissionDeniedError, "Live merge queue"):
                            evaluate()

    def test_a_conversation_opened_after_planning_blocks_with_its_own_reason(self) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            evidence = _repository_evidence()
            with self.assertRaises(MergeAdmissionDeniedError) as blocked:
                _evaluate_live(
                    store=store,
                    provider=_EvidenceProvider(evidence),
                    evidence=evidence,
                    target_conversations=MergeTrainReviewConversations(
                        rule="required", unresolved_count=1
                    ),
                )
        self.assertEqual(blocked.exception.reason_code, "pull_request_conversations_unresolved")
        self.assertIn("PR #2022", str(blocked.exception))

    def test_a_pull_request_queued_behind_the_plan_does_not_block_landing(self) -> None:
        # A newer PR labeled while the batch lands waits for the next candidate (#2637).
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            evidence = _repository_evidence()
            result = _evaluate_live(
                store=store,
                provider=_EvidenceProvider(evidence),
                evidence=evidence,
                extra_pull_requests=(
                    _queued_pull_request(
                        number=2030, head_sha="e" * 40, created_at="2026-08-11T03:00:30Z"
                    ),
                ),
            )
        self.assertEqual(result.readiness.state, "ready")

    def test_held_out_pull_request_ahead_of_the_plan_is_excluded_only_at_its_recorded_head(
        self,
    ) -> None:
        held_out = MergeTrainBatchHeldOutEntry(pull_request_number=2021, head_sha="d" * 40)
        cases = (
            ("unchanged", held_out.pull_request_number, held_out.head_sha, True),
            ("head_changed", held_out.pull_request_number, "e" * 40, False),
            ("different_pr_same_head", 2020, held_out.head_sha, False),
        )
        for case, number, head_sha, admitted in cases:
            with self.subTest(case=case), TemporaryDirectory() as directory:
                store = FilesystemRecordStore(state_dir=Path(directory))
                evidence = _repository_evidence()

                def evaluate() -> MergeAdmissionEvaluation:
                    return _evaluate_live(
                        store=store,
                        provider=_EvidenceProvider(evidence),
                        evidence=evidence,
                        extra_pull_requests=(
                            _queued_pull_request(
                                number=number,
                                head_sha=head_sha,
                                created_at="2026-08-11T02:59:00Z",
                            ),
                        ),
                        held_out=(held_out,),
                    )

                if admitted:
                    result = evaluate()
                    self.assertEqual(result.readiness.state, "ready")
                else:
                    with self.assertRaises(MergeAdmissionDeniedError) as denied:
                        evaluate()
                    self.assertEqual(denied.exception.reason_code, "landing_lineage_changed")


class LiveMergeAdmissionEvaluatorTests(unittest.TestCase):
    def test_historical_required_policy_cannot_restore_engineering_merge_gate(self) -> None:
        candidate_record, landing_record, controller_state, structural_result = _guard_records()
        snapshot_reader = _StaticSnapshotReader(
            MergeTrainDryRunSnapshot(
                repository=REPOSITORY,
                base_branch="main",
                base_sha=BASE_SHA,
                pull_requests=(
                    _queued_pull_request(
                        number=2083,
                        head_sha=HEAD_SHA,
                        created_at="2026-08-11T03:00:00Z",
                    ),
                ),
            )
        )
        entry_evidence = MergeTrainStructuralEntryObservation(
            position=1,
            pull_request_number=2083,
            head_sha=HEAD_SHA,
            head_tree_sha=TREE_SHA,
        )
        engineering_store = _EmptyEngineeringReviewStore()

        for mode in ("advisory", "required"):
            with self.subTest(mode=mode):
                legacy_record = MergeTrainPolicyRecord.model_validate_json(
                    (
                        Path(__file__).parent / "fixtures" / "merge-train-policy-required.json"
                    ).read_text(encoding="utf-8")
                )
                legacy_payload = legacy_record.model_dump(mode="json")
                legacy_payload["policy"]["policies"][0]["repository"] = REPOSITORY
                legacy_payload["policy_sha256"] = ""
                legacy_record = MergeTrainPolicyRecord.model_validate(legacy_payload)
                evaluator = LiveMergeAdmissionEvaluator(
                    store=object(),
                    repository_evidence_provider=_UnusedRepositoryEvidenceProvider(),
                    technical_check_client=_PassingTechnicalCheckClient(),
                    policy_record_provider=lambda: (
                        build_test_merge_train_policy_record(repository=REPOSITORY)
                        if mode == "advisory"
                        else legacy_record
                    ),
                    snapshot_reader=snapshot_reader,
                )
                with (
                    patch.object(
                        LiveMergeAdmissionEvaluator,
                        "_entry_evidence",
                        return_value=entry_evidence,
                    ),
                    patch.object(
                        LiveMergeAdmissionEvaluator,
                        "_policy_fingerprints",
                        return_value=_policy_fingerprints(),
                    ),
                    patch(
                        "control_plane.merge_admission_live.evaluate_merge_train_structural_candidate",
                        return_value=structural_result,
                    ),
                    patch(
                        "control_plane.merge_admission_live.require_engineering_review_decision_store",
                        return_value=engineering_store,
                    ),
                ):
                    result = evaluator.evaluate(
                        candidate_record=candidate_record,
                        landing_plan_record=landing_record,
                        entry=landing_record.landing_plan.entries[0],
                        observed_base_sha=BASE_SHA,
                        observed_base_tree_sha="5" * 40,
                        observed_head_sha=HEAD_SHA,
                        observed_head_tree_sha=TREE_SHA,
                        controller_state=controller_state,
                        expected_lease_owner=controller_state.lease_owner,
                        stack_collapse_record=None,
                        evaluated_at="2026-08-11T03:01:00Z",
                    )

                self.assertEqual(result.readiness.state, "ready")
                self.assertEqual(result.readiness.engineering_review_authority, "advisory")
                self.assertEqual(result.readiness.engineering_review.state, "unknown")

    def test_an_existing_multi_pr_plan_cannot_land_an_owned_change_in_a_batch(self) -> None:
        candidate_record, landing_record, controller_state, _ = _guard_records()
        first = landing_record.landing_plan.entries[0]
        second = first.model_copy(
            update={
                "pull_request_number": first.pull_request_number + 1,
                "position": 2,
                "expected_head_sha": "d" * 40,
            }
        )
        landing_record = landing_record.model_copy(
            update={
                "landing_plan": landing_record.landing_plan.model_copy(
                    update={"entries": (first, second)}
                ),
            }
        )
        snapshot = MergeTrainDryRunSnapshot(
            repository=REPOSITORY,
            base_branch="main",
            base_sha=BASE_SHA,
            pull_requests=(
                _queued_pull_request(
                    number=first.pull_request_number,
                    head_sha=HEAD_SHA,
                    created_at="2026-08-11T03:00:00Z",
                ).model_copy(update={"requires_individual_landing": True}),
                _queued_pull_request(
                    number=second.pull_request_number,
                    head_sha=second.expected_head_sha,
                    created_at="2026-08-11T03:00:01Z",
                ),
            ),
        )
        evaluator = LiveMergeAdmissionEvaluator(
            store=object(),
            repository_evidence_provider=_UnusedRepositoryEvidenceProvider(),
            technical_check_client=TenantAdmissionControllerGitHubClient(
                transport=RecordingMergeTrainGitHubTransport()
            ),
            policy_record_provider=lambda: build_test_merge_train_policy_record(
                repository=REPOSITORY
            ),
            snapshot_reader=_StaticSnapshotReader(snapshot),
        )
        with self.assertRaises(MergeAdmissionDeniedError) as denied:
            evaluator.evaluate(
                candidate_record=candidate_record,
                landing_plan_record=landing_record,
                entry=first,
                observed_base_sha=BASE_SHA,
                observed_base_tree_sha="4" * 40,
                observed_head_sha=HEAD_SHA,
                observed_head_tree_sha=TREE_SHA,
                controller_state=controller_state,
                expected_lease_owner=controller_state.lease_owner,
                stack_collapse_record=None,
                evaluated_at="2026-08-11T03:01:00Z",
            )
        self.assertEqual(denied.exception.reason_code, "landing_lineage_changed")

    def test_live_queue_is_rediscovered_and_inserted_pr_refuses_admission(self) -> None:
        candidate_record, landing_record, controller_state, _ = _guard_records()
        snapshot_reader = _StaticSnapshotReader(
            MergeTrainDryRunSnapshot(
                repository=REPOSITORY,
                base_branch="main",
                base_sha=BASE_SHA,
                pull_requests=(
                    _queued_pull_request(
                        number=2082,
                        head_sha="d" * 40,
                        created_at="2026-08-11T02:59:00Z",
                    ),
                    _queued_pull_request(
                        number=2083,
                        head_sha=HEAD_SHA,
                        created_at="2026-08-11T03:00:00Z",
                    ),
                ),
            )
        )
        policy_reads = 0

        def read_policy():  # type: ignore[no-untyped-def]
            nonlocal policy_reads
            policy_reads += 1
            return build_test_merge_train_policy_record(repository=REPOSITORY)

        evaluator = LiveMergeAdmissionEvaluator(
            store=object(),
            repository_evidence_provider=_UnusedRepositoryEvidenceProvider(),
            technical_check_client=TenantAdmissionControllerGitHubClient(
                transport=RecordingMergeTrainGitHubTransport()
            ),
            policy_record_provider=read_policy,
            snapshot_reader=snapshot_reader,
        )
        entry = landing_record.landing_plan.entries[0]

        for _ in range(2):
            with self.assertRaisesRegex(MergeAdmissionDeniedError, "Live merge queue"):
                evaluator.evaluate(
                    candidate_record=candidate_record,
                    landing_plan_record=landing_record,
                    entry=entry,
                    observed_base_sha=BASE_SHA,
                    observed_base_tree_sha="4" * 40,
                    observed_head_sha=HEAD_SHA,
                    observed_head_tree_sha="2" * 40,
                    controller_state=controller_state,
                    expected_lease_owner=controller_state.lease_owner,
                    stack_collapse_record=None,
                    evaluated_at="2026-08-11T03:01:00Z",
                )

        self.assertEqual(policy_reads, 2)
        self.assertEqual(snapshot_reader.read_count, 2)

    def test_dependency_check_flip_invalidates_planned_landing(self) -> None:
        policy_record = build_test_merge_train_policy_record(repository=REPOSITORY)
        policy_payload = policy_record.model_dump(mode="json")
        enqueue = policy_payload["policy"]["policies"][0]["enqueue"]
        enqueue["dependency_update_github_user_ids"] = [42]
        enqueue["trusted_automation_github_user_ids"] = [42]
        policy_payload["policy_sha256"] = ""
        policy_record = MergeTrainPolicyRecord.model_validate(policy_payload)
        candidate, landing, controller, _ = _guard_records(
            policy_sha256=policy_record.policy_sha256
        )
        entry = landing.landing_plan.entries[0]
        update = _queued_pull_request(
            number=entry.pull_request_number, head_sha=HEAD_SHA, created_at="2026-08-11T03:00:00Z"
        ).model_copy(
            update={
                "labels": (),
                "label_actors": (),
                "actor_id": 42,
                "dependency_update_class": "patch_or_minor",
            }
        )
        older_unknown_update = update.model_copy(
            update={
                "number": entry.pull_request_number - 1,
                "head_sha": "d" * 40,
                "created_at": "2026-08-11T02:59:00Z",
                "mergeable": "unknown",
            }
        )
        for checks, mergeable, head_sha, admitted in (
            ("pass", "mergeable", HEAD_SHA, True),
            ("pass", "unknown", HEAD_SHA, True),
            ("fail", "unknown", HEAD_SHA, False),
            ("pass", "conflicting", HEAD_SHA, False),
            ("pass", "unknown", "e" * 40, False),
        ):
            with self.subTest(checks=checks, mergeable=mergeable, head_sha=head_sha):
                evaluator = LiveMergeAdmissionEvaluator(
                    store=object(),
                    repository_evidence_provider=_UnusedRepositoryEvidenceProvider(),
                    technical_check_client=_TechnicalCheckClient(),
                    policy_record_provider=lambda: policy_record,
                    snapshot_reader=_StaticSnapshotReader(
                        MergeTrainDryRunSnapshot(
                            repository=REPOSITORY,
                            base_branch="main",
                            base_sha=BASE_SHA,
                            pull_requests=(
                                older_unknown_update,
                                update.model_copy(
                                    update={
                                        "required_checks_status": checks,
                                        "mergeable": mergeable,
                                        "head_sha": head_sha,
                                    }
                                ),
                            ),
                        )
                    ),
                )
                with patch.object(
                    LiveMergeAdmissionEvaluator,
                    "_entry_evidence",
                    side_effect=_QueueAccepted("queue accepted"),
                ):
                    expected_error = _QueueAccepted if admitted else MergeAdmissionDeniedError
                    with self.assertRaises(expected_error) as result:
                        evaluator.evaluate(
                            candidate_record=candidate,
                            landing_plan_record=landing,
                            entry=entry,
                            observed_base_sha=BASE_SHA,
                            observed_base_tree_sha="4" * 40,
                            observed_head_sha=HEAD_SHA,
                            observed_head_tree_sha="2" * 40,
                            controller_state=controller,
                            expected_lease_owner=controller.lease_owner,
                            stack_collapse_record=None,
                            evaluated_at="2026-08-11T03:01:00Z",
                        )
                    if isinstance(result.exception, MergeAdmissionDeniedError):
                        self.assertEqual(result.exception.reason_code, "landing_lineage_changed")

    def test_a_changed_planned_entry_refuses_even_with_a_newer_pull_request_behind_it(
        self,
    ) -> None:
        # Tolerating late arrivals must not hide a planned PR that moved or left (#2637).
        candidate_record, landing_record, controller_state, _ = _guard_records()
        entry = landing_record.landing_plan.entries[0]
        late_arrival = _queued_pull_request(
            number=2090, head_sha="e" * 40, created_at="2026-08-11T03:00:30Z"
        )
        cases = {
            "head_moved": (
                _queued_pull_request(
                    number=entry.pull_request_number,
                    head_sha="d" * 40,
                    created_at="2026-08-11T03:00:00Z",
                ),
                late_arrival,
            ),
            "left_the_queue": (late_arrival,),
        }
        for case, pull_requests in cases.items():
            with self.subTest(case=case):
                evaluator = LiveMergeAdmissionEvaluator(
                    store=object(),
                    repository_evidence_provider=_UnusedRepositoryEvidenceProvider(),
                    technical_check_client=_TechnicalCheckClient(),
                    policy_record_provider=lambda: build_test_merge_train_policy_record(
                        repository=REPOSITORY
                    ),
                    snapshot_reader=_StaticSnapshotReader(
                        MergeTrainDryRunSnapshot(
                            repository=REPOSITORY,
                            base_branch="main",
                            base_sha=BASE_SHA,
                            pull_requests=pull_requests,
                        )
                    ),
                )
                with self.assertRaises(MergeAdmissionDeniedError) as denied:
                    evaluator.evaluate(
                        candidate_record=candidate_record,
                        landing_plan_record=landing_record,
                        entry=entry,
                        observed_base_sha=BASE_SHA,
                        observed_base_tree_sha="4" * 40,
                        observed_head_sha=HEAD_SHA,
                        observed_head_tree_sha="2" * 40,
                        controller_state=controller_state,
                        expected_lease_owner=controller_state.lease_owner,
                        stack_collapse_record=None,
                        evaluated_at="2026-08-11T03:01:00Z",
                    )
                self.assertEqual(denied.exception.reason_code, "landing_lineage_changed")

    def test_ordinary_proven_no_op_accepts_closed_or_merged_lifecycle_only(self) -> None:
        candidate_record, landing_record, controller_state, _, base_tree_sha = (
            _ordinary_no_op_records()
        )
        entry = landing_record.landing_plan.entries[0]
        for state in ("closed", "merged"):
            with self.subTest(state=state):
                snapshot_reader = _StaticSnapshotReader(
                    MergeTrainDryRunSnapshot(
                        repository=REPOSITORY,
                        base_branch="main",
                        base_sha=BASE_SHA,
                        pull_requests=(
                            _queued_pull_request(
                                number=entry.pull_request_number,
                                head_sha=entry.expected_head_sha,
                                created_at="2026-08-11T03:00:00Z",
                            ).model_copy(update={"state": state}),
                        ),
                    )
                )
                evaluator = LiveMergeAdmissionEvaluator(
                    store=object(),
                    repository_evidence_provider=_UnusedRepositoryEvidenceProvider(),
                    technical_check_client=_TechnicalCheckClient(),
                    policy_record_provider=lambda: build_test_merge_train_policy_record(
                        repository=REPOSITORY
                    ),
                    snapshot_reader=snapshot_reader,
                )
                with (
                    patch.object(
                        LiveMergeAdmissionEvaluator,
                        "_entry_evidence",
                        side_effect=_QueueAccepted("queue accepted"),
                    ),
                    self.assertRaisesRegex(_QueueAccepted, "queue accepted"),
                ):
                    evaluator.evaluate(
                        candidate_record=candidate_record,
                        landing_plan_record=landing_record,
                        entry=entry,
                        observed_base_sha=BASE_SHA,
                        observed_base_tree_sha=base_tree_sha,
                        observed_head_sha=entry.expected_head_sha,
                        observed_head_tree_sha=entry.expected_head_tree_sha,
                        controller_state=controller_state,
                        expected_lease_owner=controller_state.lease_owner,
                        stack_collapse_record=None,
                        evaluated_at="2026-08-11T03:01:00Z",
                    )

    def test_nonordinary_or_ineligible_closed_no_op_cannot_bypass_queue(self) -> None:
        candidate_record, landing_record, controller_state, _, base_tree_sha = (
            _ordinary_no_op_records()
        )
        entry = landing_record.landing_plan.entries[0]
        base_pull_request = _queued_pull_request(
            number=entry.pull_request_number,
            head_sha=entry.expected_head_sha,
            created_at="2026-08-11T03:00:00Z",
        ).model_copy(update={"state": "closed"})
        merge_candidate, merge_landing, _, _ = _guard_records()
        cases = {
            "ordinary_real_merge": (
                merge_candidate.model_copy(
                    update={"ordinary_job_binding": candidate_record.ordinary_job_binding}
                ),
                merge_landing.model_copy(
                    update={"ordinary_job_binding": landing_record.ordinary_job_binding}
                ),
                base_pull_request,
            ),
            "generic": (
                candidate_record.model_copy(update={"ordinary_job_binding": None}),
                landing_record.model_copy(update={"ordinary_job_binding": None}),
                base_pull_request,
            ),
            "missing_label": (
                candidate_record,
                landing_record,
                base_pull_request.model_copy(update={"labels": ()}),
            ),
            "untrusted_actor": (
                candidate_record,
                landing_record,
                base_pull_request.model_copy(update={"actor_role": "unknown"}),
            ),
        }
        for case, (candidate, landing, pull_request) in cases.items():
            with self.subTest(case=case):
                evaluator = LiveMergeAdmissionEvaluator(
                    store=object(),
                    repository_evidence_provider=_UnusedRepositoryEvidenceProvider(),
                    technical_check_client=_TechnicalCheckClient(),
                    policy_record_provider=lambda: build_test_merge_train_policy_record(
                        repository=REPOSITORY
                    ),
                    snapshot_reader=_StaticSnapshotReader(
                        MergeTrainDryRunSnapshot(
                            repository=REPOSITORY,
                            base_branch="main",
                            base_sha=BASE_SHA,
                            pull_requests=(pull_request,),
                        )
                    ),
                )
                with self.assertRaises(MergeAdmissionDeniedError) as denied:
                    evaluator.evaluate(
                        candidate_record=candidate,
                        landing_plan_record=landing,
                        entry=landing.landing_plan.entries[0],
                        observed_base_sha=BASE_SHA,
                        observed_base_tree_sha=base_tree_sha,
                        observed_head_sha=entry.expected_head_sha,
                        observed_head_tree_sha=entry.expected_head_tree_sha,
                        controller_state=controller_state,
                        expected_lease_owner=controller_state.lease_owner,
                        stack_collapse_record=None,
                        evaluated_at="2026-08-11T03:01:00Z",
                    )
                self.assertEqual(denied.exception.reason_code, "landing_lineage_changed")

    def test_closed_ordinary_entry_requires_exact_recorded_no_op_identity(self) -> None:
        candidate_record, landing_record, controller_state, _, base_tree_sha = (
            _ordinary_no_op_records()
        )
        entry = landing_record.landing_plan.entries[0]
        changed_entry = entry.model_copy(update={"recorded_candidate_result_tree_sha": "7" * 40})
        changed_plan = landing_record.landing_plan.model_copy(update={"entries": (changed_entry,)})
        changed_record = landing_record.model_copy(update={"landing_plan": changed_plan})
        evaluator = LiveMergeAdmissionEvaluator(
            store=object(),
            repository_evidence_provider=_UnusedRepositoryEvidenceProvider(),
            technical_check_client=_TechnicalCheckClient(),
            policy_record_provider=lambda: build_test_merge_train_policy_record(
                repository=REPOSITORY
            ),
            snapshot_reader=_StaticSnapshotReader(
                MergeTrainDryRunSnapshot(
                    repository=REPOSITORY,
                    base_branch="main",
                    base_sha=BASE_SHA,
                    pull_requests=(
                        _queued_pull_request(
                            number=entry.pull_request_number,
                            head_sha=entry.expected_head_sha,
                            created_at="2026-08-11T03:00:00Z",
                        ).model_copy(update={"state": "closed"}),
                    ),
                )
            ),
        )

        with self.assertRaises(MergeAdmissionDeniedError) as denied:
            evaluator.evaluate(
                candidate_record=candidate_record,
                landing_plan_record=changed_record,
                entry=changed_entry,
                observed_base_sha=BASE_SHA,
                observed_base_tree_sha=base_tree_sha,
                observed_head_sha=entry.expected_head_sha,
                observed_head_tree_sha=entry.expected_head_tree_sha,
                controller_state=controller_state,
                expected_lease_owner=controller_state.lease_owner,
                stack_collapse_record=None,
                evaluated_at="2026-08-11T03:01:00Z",
            )
        self.assertEqual(denied.exception.reason_code, "landing_lineage_changed")

    def test_active_policy_removal_is_rediscovered_and_refuses_admission(self) -> None:
        candidate_record, landing_record, controller_state, _ = _guard_records()
        snapshot_reader = _StaticSnapshotReader(
            MergeTrainDryRunSnapshot(
                repository=REPOSITORY,
                base_branch="main",
                base_sha=BASE_SHA,
                pull_requests=(
                    _queued_pull_request(
                        number=2083,
                        head_sha=HEAD_SHA,
                        created_at="2026-08-11T03:00:00Z",
                    ),
                ),
            )
        )
        evaluator = LiveMergeAdmissionEvaluator(
            store=object(),
            repository_evidence_provider=_UnusedRepositoryEvidenceProvider(),
            technical_check_client=TenantAdmissionControllerGitHubClient(
                transport=RecordingMergeTrainGitHubTransport()
            ),
            policy_record_provider=lambda: build_test_merge_train_policy_record(
                repository="example/other-repository"
            ),
            snapshot_reader=snapshot_reader,
        )

        with self.assertRaisesRegex(MergeAdmissionDeniedError, "Active merge-train policy"):
            evaluator.evaluate(
                candidate_record=candidate_record,
                landing_plan_record=landing_record,
                entry=landing_record.landing_plan.entries[0],
                observed_base_sha=BASE_SHA,
                observed_base_tree_sha="4" * 40,
                observed_head_sha=HEAD_SHA,
                observed_head_tree_sha="2" * 40,
                controller_state=controller_state,
                expected_lease_owner=controller_state.lease_owner,
                stack_collapse_record=None,
                evaluated_at="2026-08-11T03:01:00Z",
            )

        self.assertEqual(snapshot_reader.read_count, 1)


if __name__ == "__main__":
    unittest.main()
