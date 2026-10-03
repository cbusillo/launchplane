import unittest
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from control_plane.contracts.merge_admission_record import MergeBatchNoEffectEvidence
from control_plane.contracts.merge_train_batch import build_merge_train_batch_landing_plan
from control_plane.contracts.merge_train_policy import MergeTrainPolicyRecord
from control_plane.merge_admission import (
    GuardedMergeAdmission,
    MergeAdmissionDeniedError,
    MergeAdmissionReconciliationRequiredError,
)
from control_plane.merge_train_github import GitHubMergeTrainClient, MergeTrainGitHubError
from control_plane.merge_train import MergeTrainDryRunSnapshot
from control_plane.merge_train_controller_run_once import (
    MergeTrainControllerRunOnceEnvelope,
    execute_merge_train_controller_with_client,
)
from tests.test_merge_train_batch_pull_request import _BatchProvider
from control_plane.storage.filesystem import FilesystemRecordStore
from tests import test_merge_admission_rolling_checks as rolling_fixtures
from tests.test_merge_admission_live import _queued_pull_request


class ProtectedBatchGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        # Reuse the complete Git/provenance fixture exercised by the live evaluator.
        self.fixture = rolling_fixtures.RollingCandidateCheckTests()
        self.fixture.setUp()
        self.fixture.plan = build_merge_train_batch_landing_plan(
            candidate=self.fixture.candidate_record.candidate,
            merge_method="merge",
            created_at=self.fixture.candidate_record.updated_at,
            candidate_pull_request_number=9000,
        )
        self.fixture.landing_record = self.fixture.landing_record.model_copy(
            update={"landing_plan": self.fixture.plan}
        )
        self.fixture.controller = self.fixture.controller.model_copy(
            update={
                "active_record_id": self.fixture.landing_record.record_id,
                "step_payload": {
                    "landing_plan_id": self.fixture.plan.plan_id,
                    "expected_effect_sha": self.fixture.plan.candidate_sha,
                },
            }
        )
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = FilesystemRecordStore(Path(temporary.name))
        self.store.write_merge_train_controller_state_record(self.fixture.controller)
        self.store.write_merge_train_batch_candidate_record(self.fixture.candidate_record)
        self.store.write_merge_train_batch_landing_plan_record(self.fixture.landing_record)

        fixture = self.fixture

        class LiveEvaluator:
            @staticmethod
            def evaluate(**kwargs: Any) -> Any:
                return fixture._evaluate(
                    after_first_landing=False,
                    target_position=kwargs["entry"].position,
                    base_tree=fixture.plan.entries[0].recorded_candidate_parent_tree_sha,
                )

        self.guard = GuardedMergeAdmission(
            record_store=self.store,
            evaluator=LiveEvaluator(),
            candidate_record=fixture.candidate_record,
            landing_plan_record=fixture.landing_record,
            controller_state=fixture.controller,
            controller_state_provider=lambda: self.fixture.controller,
            admission_time_provider=lambda: "2026-08-11T03:01:00Z",
            trace_id="batch-test",
        )

    def admit(self, position: int) -> Any:
        entry = self.fixture.plan.entries[position - 1]
        self.fixture.controller = self.fixture.controller.model_copy(
            update={"active_pull_request_number": entry.pull_request_number}
        )
        self.store.write_merge_train_controller_state_record(self.fixture.controller)
        return self.guard.admit(
            entry=entry,
            observed_base_sha=self.fixture.plan.entries[0].expected_base_sha,
            observed_base_tree_sha=self.fixture.plan.entries[0].recorded_candidate_parent_tree_sha,
            observed_head_sha=entry.expected_head_sha,
            observed_head_tree_sha=entry.expected_head_tree_sha,
        )

    def test_shared_merge_interruption_reconciles_both_real_stored_outcomes(self) -> None:
        provider = _BatchProvider(self.fixture.candidate_record.candidate)
        provider.number = 9000
        provider.interrupt_after_merge = True
        client = GitHubMergeTrainClient(transport=provider)
        client.ensure_batch_pull_request(candidate=provider.candidate)

        def checkpoint(_plan: Any, entry: Any, phase: str) -> None:
            if phase == "merge_entry":
                self.fixture.controller = self.fixture.controller.model_copy(
                    update={"active_pull_request_number": entry.pull_request_number}
                )
                self.store.write_merge_train_controller_state_record(self.fixture.controller)

        def land() -> Any:
            return client.land_batch_candidate(
                landing_plan=self.guard.landing_plan_record.landing_plan,
                admission_guard=self.guard,
                recorded_at="2026-08-11T03:02:00Z",
                checkpoint=checkpoint,
            )

        with self.assertRaises(KeyboardInterrupt):
            land()
        admissions = self.store.list_merge_admission_records()
        self.assertEqual(len(admissions), 2)
        self.assertEqual(self.store.list_merge_landing_outcome_records(), ())
        landed = land()
        self.assertTrue(all(entry.status == "merged" for entry in landed.entries))
        self.assertEqual(len(provider.merge_calls), 1)
        outcomes = self.store.list_merge_landing_outcome_records()
        self.assertEqual(
            {outcome.admission_id for outcome in outcomes}, {a.admission_id for a in admissions}
        )
        self.assertTrue(all(outcome.status == "landed" for outcome in outcomes))
        self.assertEqual({outcome.merge_commit_sha for outcome in outcomes}, {provider.merge_sha})

    def test_unready_second_member_does_not_append_prefix_admissions_each_pass(self) -> None:
        fixture = self.fixture

        class BlockedSecond:
            @staticmethod
            def evaluate(**kwargs: Any) -> Any:
                position = kwargs["entry"].position
                return fixture._evaluate(
                    after_first_landing=False,
                    target_position=position,
                    base_tree=fixture.plan.entries[0].recorded_candidate_parent_tree_sha,
                    conclusion="failure" if position == 2 else "success",
                )

        self.guard.evaluator = BlockedSecond()
        provider = _BatchProvider(fixture.candidate_record.candidate)
        provider.number = 9000
        client = GitHubMergeTrainClient(transport=provider)
        client.ensure_batch_pull_request(candidate=provider.candidate)

        def checkpoint(_plan: Any, entry: Any, _phase: str) -> None:
            fixture.controller = fixture.controller.model_copy(
                update={"active_pull_request_number": entry.pull_request_number}
            )
            self.store.write_merge_train_controller_state_record(fixture.controller)

        for _ in range(2):
            with self.assertRaises(MergeAdmissionDeniedError):
                client.land_batch_candidate(
                    landing_plan=fixture.plan,
                    admission_guard=self.guard,
                    recorded_at="2026-08-11T03:02:00Z",
                    checkpoint=checkpoint,
                )
        self.assertEqual(self.store.list_merge_admission_records(), ())
        self.assertEqual(self.store.list_merge_landing_outcome_records(), ())
        self.assertEqual(provider.merge_calls, [])

    def test_policy_change_to_squash_retires_pending_and_planned_batch_prs(self) -> None:
        candidate = self.fixture.candidate_record.candidate
        payload = self.fixture.policy.model_dump(mode="json")
        payload["policy"]["policies"][0]["merge_method"] = "squash"
        payload["policy_sha256"] = ""
        policy = MergeTrainPolicyRecord.model_validate(payload)
        snapshot = MergeTrainDryRunSnapshot(
            repository=candidate.repository,
            base_branch="main",
            base_sha=candidate.base_sha,
            pull_requests=tuple(
                _queued_pull_request(
                    number=entry.pull_request_number,
                    head_sha=entry.head_sha,
                    created_at="2026-08-11T01:00:00Z",
                )
                for entry in candidate.entries
            ),
        )
        for planned in (False, True):
            with self.subTest(planned=planned), TemporaryDirectory() as directory:
                store = FilesystemRecordStore(Path(directory))
                store.write_merge_train_batch_candidate_record(self.fixture.candidate_record)
                if planned:
                    store.write_merge_train_batch_landing_plan_record(self.fixture.landing_record)
                provider = _BatchProvider(candidate)
                provider.number = 9000
                client = GitHubMergeTrainClient(transport=provider)
                client.ensure_batch_pull_request(candidate=candidate)
                # The replacement plan's conflict probe is covered by the controller tests.
                with (
                    patch.object(client, "read_merge_train_snapshot", return_value=snapshot),
                    patch.object(client, "probe_batch_entry_conflicts", return_value=()),
                ):
                    result = execute_merge_train_controller_with_client(
                        request=MergeTrainControllerRunOnceEnvelope(
                            repository=candidate.repository, mutate=True
                        ),
                        policy=policy.policy,
                        policy_sha256=policy.policy_sha256,
                        repository_policy=policy.policy.policies[0],
                        github_client=client,
                        trace_id="policy-change",
                        recorded_at="2026-08-11T03:03:00Z",
                        candidate_store=store,
                        landing_store=store,
                        stack_collapse_store=store,
                        controller_state_store=store,
                        admission_store=store,
                        admission_evaluator=self.guard.evaluator,
                    )
                self.assertEqual(
                    result.accepted_result["controller_action"],
                    "retire_stale_landing" if planned else "plan_candidate",
                )
                self.assertTrue(provider.closed)
                self.assertEqual(provider.merge_calls, [])
                self.assertEqual(store.list_merge_admission_records(), ())
                self.assertEqual(
                    store.list_merge_train_controller_state_records()[0].status, "idle"
                )

    def _check_interrupted_batch_retirements(self, check: Callable[..., None]) -> None:
        """Run check for each retirement source on a batch interrupted after its close."""
        candidate = self.fixture.candidate_record.candidate
        payload = self.fixture.policy.model_dump(mode="json")
        payload["policy"]["policies"][0]["merge_method"] = "squash"
        payload["policy_sha256"] = ""
        changed_policy = MergeTrainPolicyRecord.model_validate(payload)

        class LineageChanged:
            @staticmethod
            def evaluate(**_: Any) -> Any:
                raise MergeAdmissionDeniedError(
                    "Live merge queue changed from the landing-plan lineage.",
                    reason_code="landing_lineage_changed",
                )

        cases = {
            "policy-changed-landing": (changed_policy, self.guard.evaluator),
            "lineage-changed-landing": (self.fixture.policy, LineageChanged()),
        }
        for retirement_source, (policy, evaluator) in cases.items():
            with self.subTest(retirement_source), TemporaryDirectory() as directory:
                store = FilesystemRecordStore(Path(directory))
                store.write_merge_train_batch_candidate_record(self.fixture.candidate_record)
                store.write_merge_train_batch_landing_plan_record(self.fixture.landing_record)
                provider = _BatchProvider(candidate)
                provider.number = 9000
                client = GitHubMergeTrainClient(transport=provider)
                client.ensure_batch_pull_request(candidate=candidate)

                def run(
                    trace_id: str,
                    policy: MergeTrainPolicyRecord = policy,
                    evaluator: Any = evaluator,
                    client: GitHubMergeTrainClient = client,
                    store: FilesystemRecordStore = store,
                ) -> Any:
                    return execute_merge_train_controller_with_client(
                        request=MergeTrainControllerRunOnceEnvelope(
                            repository=candidate.repository, mutate=True
                        ),
                        policy=policy.policy,
                        policy_sha256=policy.policy_sha256,
                        repository_policy=policy.policy.policies[0],
                        github_client=client,
                        trace_id=trace_id,
                        recorded_at="2026-08-11T03:03:00Z",
                        candidate_store=store,
                        landing_store=store,
                        stack_collapse_store=store,
                        controller_state_store=store,
                        admission_store=store,
                        admission_evaluator=evaluator,
                    )

                with patch.object(
                    store,
                    "write_merge_train_batch_landing_plan_record",
                    side_effect=OSError("interrupted"),
                ):
                    with self.assertRaises(OSError):
                        run("retire")
                self.assertTrue(provider.closed)
                check(retirement_source, store, provider, run)

    def test_retirement_interrupted_after_closing_the_batch_pr_resumes_as_retirement(
        self,
    ) -> None:
        """A closed batch PR must not turn the retirement into a generic stale landing (#2846)."""

        def check(retirement_source: str, store: Any, provider: Any, run: Any) -> None:
            result = run("resume")

            self.assertEqual(result.accepted_result["controller_action"], "retire_stale_landing")
            self.assertEqual(
                [request for request in provider.requests if request[0] == "PATCH"],
                [
                    (
                        "PATCH",
                        f"/repos/{provider.candidate.repository}/pulls/9000",
                        {"state": "closed"},
                    )
                ],
            )
            self.assertEqual(provider.merge_calls, [])
            sources = [
                record.source.rsplit(":", maxsplit=1)[0]
                for record in store.list_merge_train_batch_landing_plan_records()
                if record.record_id != self.fixture.landing_record.record_id
            ]
            self.assertEqual(sources, [f"service:controller:{retirement_source}"])
            self.assertEqual(
                store.list_merge_train_batch_candidate_records()[0].status, "superseded"
            )
            self.assertEqual(store.list_merge_train_controller_state_records()[0].status, "idle")

        self._check_interrupted_batch_retirements(check)

    def test_resumed_retirement_rechecks_constituents_changed_during_the_interruption(
        self,
    ) -> None:
        def check(_retirement_source: str, store: Any, provider: Any, run: Any) -> None:
            provider.heads[self.fixture.plan.entries[0].pull_request_number] = "f" * 40

            with self.assertRaises(MergeTrainGitHubError):
                run("resume")

            self.assertEqual(
                store.list_merge_train_batch_landing_plan_records(),
                (self.fixture.landing_record,),
            )
            self.assertEqual(store.list_merge_train_batch_candidate_records()[0].status, "active")
            self.assertEqual(
                store.list_merge_train_controller_state_records()[0].status,
                "reconcile_required",
            )

        self._check_interrupted_batch_retirements(check)

    def test_both_real_admissions_persist_before_one_shared_no_effect_reconciliation(self) -> None:
        admissions = [self.admit(1), self.admit(2)]
        self.assertEqual(len(self.store.list_unresolved_merge_admission_records()), 2)
        self.assertEqual(
            {admission.landing_plan_sha256 for admission in admissions},
            {self.fixture.plan.landing_plan_sha256},
        )
        self.guard.reconcile_batch_no_effect(
            evidence=MergeBatchNoEffectEvidence(
                pull_request_number=9000,
                head_sha=self.fixture.plan.candidate_sha,
                state="open",
                merged=False,
                base_contains_head=False,
            ),
            observed_base_sha="d" * 40,
            observed_base_tree_sha="e" * 40,
            observed_at="2026-08-11T03:02:00Z",
        )
        self.assertEqual(self.store.list_unresolved_merge_admission_records(), ())
        for admission in admissions:
            outcomes = self.store.list_merge_landing_outcome_records(
                admission_id=admission.admission_id
            )
            self.assertEqual(
                [outcome.reason for outcome in outcomes],
                ["batch_reconciliation_confirmed_no_effect", "process_interrupted"],
            )
            self.assertEqual(outcomes[0].observed_pull_request_head_sha, "")
            proof = outcomes[0].batch_no_effect
            assert proof is not None
            self.assertEqual(proof.pull_request_number, 9000)

    def test_admitted_prefix_can_record_that_dispatch_never_happened(self) -> None:
        admission = self.admit(1)
        outcome = self.guard.record_not_dispatched(
            admission=admission, observed_at="2026-08-11T03:02:00Z"
        )
        self.assertEqual(outcome.reason, "batch_not_dispatched")
        self.assertFalse(outcome.provider_effect_attempted)
        self.assertEqual(self.store.list_unresolved_merge_admission_records(), ())

    def test_different_batch_pr_cannot_resolve_an_interrupted_attempt(self) -> None:
        self.admit(1)
        with self.assertRaises(MergeAdmissionReconciliationRequiredError):
            self.guard.reconcile_batch_no_effect(
                evidence=MergeBatchNoEffectEvidence(
                    pull_request_number=9001,
                    head_sha=self.fixture.plan.candidate_sha,
                    state="open",
                    merged=False,
                    base_contains_head=False,
                ),
                observed_base_sha="d" * 40,
                observed_base_tree_sha="e" * 40,
                observed_at="2026-08-11T03:02:00Z",
            )
        self.assertEqual(len(self.store.list_unresolved_merge_admission_records()), 1)
