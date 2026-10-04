import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from unittest.mock import Mock, patch

from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidate,
    MergeTrainBatchHeldOutEntry,
    build_merge_train_batch_candidate,
    build_merge_train_batch_candidate_record,
)
from control_plane.merge_train import build_merge_train_dry_run_result
from control_plane.merge_train_batch_pull_request import (
    batch_pull_request_body,
    changed_closed_batch_body,
)
from control_plane.merge_train_controller_feedback import build_feedback_payloads
from control_plane.merge_train_controller_run_once import try_reflow_failed_merge_train_candidate
from control_plane.merge_train_github import GitHubMergeTrainClient
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.merge_train_policy_fixtures import build_test_merge_train_policy
from tests.support.merge_train import _FakeExpandedMergeTrainSnapshotReader
from tests.test_merge_train_batch_pull_request import _BatchProvider


class FailedBatchRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = _FakeExpandedMergeTrainSnapshotReader(
            transport=object()
        ).read_merge_train_snapshot(repository="cbusillo/sellyouroutboard", base_branch="main")
        self.policy = build_test_merge_train_policy()
        self.candidate = build_merge_train_batch_candidate(
            dry_run_result=build_merge_train_dry_run_result(
                policy=self.policy, snapshot=self.snapshot
            ),
            base_sha=self.snapshot.base_sha,
            policy_sha256="policy-digest",
            created_at="2026-10-03T12:00:00Z",
        ).model_copy(
            update={
                "candidate_sha": "failed-sha",
                "status": "failed",
                "required_checks_status": "fail",
            }
        )
        self.provider = _BatchProvider(self.candidate)
        self.client = GitHubMergeTrainClient(transport=self.provider)
        self.provider.created = True
        self.provider.closed = True
        self.provider.body = batch_pull_request_body(client=self.client, candidate=self.candidate)

    def _reflow(
        self,
        store: FilesystemRecordStore,
        candidate: MergeTrainBatchCandidate,
        *,
        mutate: bool = True,
        conflicts: tuple[MergeTrainBatchHeldOutEntry, ...] = (),
    ) -> dict[str, Any]:
        record = build_merge_train_batch_candidate_record(
            candidate=candidate, source="test", updated_at="2026-10-03T12:00:00Z"
        )
        store.write_merge_train_batch_candidate_record(record)
        client = Mock(wraps=self.client)
        client.read_merge_train_snapshot.return_value = self.snapshot
        client.probe_batch_entry_conflicts.return_value = conflicts
        self.reflow_client = client
        result = try_reflow_failed_merge_train_candidate(
            candidate_store=store,
            active_candidate_record=record,
            policy=self.policy,
            policy_sha256="policy-digest",
            transport=self.provider,
            github_client=cast(GitHubMergeTrainClient, client),
            repository=self.snapshot.repository,
            base_branch=self.snapshot.base_branch,
            merge_method="merge",
            recorded_at="2026-10-03T12:01:00Z",
            trace_id="recovery",
            mutate=mutate,
            lease=Mock(),
        )
        assert result is not None
        return result

    def test_changed_closed_body_rebuilds_once_and_creates_a_new_batch_pr(self) -> None:
        self.provider.body = self.provider.body.split("## Owner test notes")[0]
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            dry = self._reflow(store, self.candidate, mutate=False)
            self.assertEqual(dry["controller_action"], "plan_candidate")
            self.assertFalse(
                any(
                    record.candidate.batch_body_retry_of
                    for record in store.list_merge_train_batch_candidate_records()
                )
            )
            result = self._reflow(store, self.candidate)
            replacement = MergeTrainBatchCandidate.model_validate(result["candidate"])
            self.assertEqual(result["controller_action"], "plan_candidate")
            self.assertNotEqual(replacement.candidate_ref, self.candidate.candidate_ref)
            self.assertTrue(replacement.batch_body_retry_of)
            self.assertEqual(replacement.entries, self.candidate.entries)
            self.assertEqual(replacement.base_sha, self.candidate.base_sha)
            # The retry ref has no PR yet; it goes through ordinary batch creation.
            built = replacement.model_copy(
                update={"candidate_sha": "rebuilt-sha", "status": "ready_for_checks"}
            )
            provider = _BatchProvider(built)
            provider.number = self.provider.number + 1
            new_client = GitHubMergeTrainClient(transport=provider)
            new_number = new_client.ensure_batch_pull_request(candidate=built)
            self.assertNotEqual(new_number, self.provider.number)
            self.assertIn("## Owner test notes", provider.body)
            posted_body = next(body for method, _, body in provider.requests if method == "POST")
            assert posted_body is not None
            self.assertEqual(posted_body["head"], built.candidate_ref.removeprefix("refs/heads/"))
            # A restarted process reads the persisted budget, even if generation changes again.
            restarted = FilesystemRecordStore(state_dir=Path(directory))
            failed_retry = built.model_copy(
                update={"status": "failed", "required_checks_status": "fail"}
            )
            stopped = self._reflow(restarted, failed_retry)
            self.assertEqual(stopped["controller_action"], "candidate_failed")
            self.assertEqual(stopped["reason_code"], "batch_body_retry_already_used")
            # Older failed evidence cannot reset the budget either.
            self.assertEqual(
                self._reflow(restarted, self.candidate)["reason_code"],
                "batch_body_retry_already_used",
            )

    def test_conflict_probe_cannot_reset_used_unchanged_queue_budget(self) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            failed_retry = self.candidate.model_copy(
                update={"batch_body_retry_of": "earlier-failure"}
            )
            third = self.snapshot.pull_requests[1].model_copy(
                update={
                    "number": 3,
                    "head_sha": "third-head",
                    "head_ref": "third-branch",
                    "created_at": "2026-10-03T12:30:00Z",
                }
            )
            self.snapshot = self.snapshot.model_copy(
                update={"pull_requests": (*self.snapshot.pull_requests, third)}
            )
            result = self._reflow(
                store,
                failed_retry,
                conflicts=(
                    MergeTrainBatchHeldOutEntry(
                        pull_request_number=3,
                        head_sha=third.head_sha,
                        conflicts_with=(1, 2),
                        probe_base_sha=self.snapshot.base_sha,
                        conflicts_with_head_shas=tuple(
                            pr.head_sha for pr in self.snapshot.pull_requests[:2]
                        ),
                    ),
                ),
            )
            self.assertEqual(result["controller_action"], "candidate_failed")
            self.assertEqual(result["reason_code"], "unchanged_batch_after_conflict_probe")
            payloads = build_feedback_payloads(
                response={
                    "result": result,
                    "records": {
                        "merge_train_batch_candidate_record_id": result[
                            "merge_train_batch_candidate_record_id"
                        ]
                    },
                }
            )
            held_out_feedback = [
                payload for payload in payloads if payload["pull_request_number"] == 3
            ]
            self.assertEqual([payload["event"] for payload in held_out_feedback], ["blocked"])
            self.assertIn("#1", cast(str, held_out_feedback[0]["message"]))
            self.assertIn("stays stopped", cast(str, held_out_feedback[0]["message"]))

            # The held-out evidence survives a restart without spending the budget.
            restarted = FilesystemRecordStore(state_dir=Path(directory))
            persisted = max(
                restarted.list_merge_train_batch_candidate_records(),
                key=lambda record: record.updated_at,
            ).candidate
            self.assertEqual(persisted.status, "failed")
            self.assertEqual(persisted.batch_body_retry_of, "earlier-failure")
            self.assertEqual(
                [(entry.pull_request_number, entry.conflicts_with) for entry in persisted.held_out],
                [(3, (1, 2))],
            )
            later = self._reflow(restarted, persisted)
            self.reflow_client.probe_batch_entry_conflicts.assert_not_called()
            self.assertEqual(later["reason_code"], "batch_body_retry_already_used")
            self.assertFalse(
                [
                    payload
                    for payload in build_feedback_payloads(
                        response={"result": later, "records": {}}
                    )
                    if payload["pull_request_number"] == 3
                ]
            )

    def test_active_batch_reports_later_review_wait_even_when_that_pr_is_behind(self) -> None:
        from control_plane.merge_train_controller_run_once import (
            _reflow_stale_candidate_record,
            MergeTrainControllerRunOnceEnvelope,
        )

        snapshot = self.snapshot.model_copy(
            update={
                "pull_requests": (
                    self.snapshot.pull_requests[0],
                    self.snapshot.pull_requests[1].model_copy(
                        update={
                            "owner_review_required": True,
                            "required_checks_status": "pending",
                            "branch_update_required": True,
                        }
                    ),
                )
            }
        )
        record = build_merge_train_batch_candidate_record(
            candidate=self.candidate.model_copy(
                update={"status": "passed", "required_checks_status": "pass"}
            ),
            source="test",
            updated_at="2026-10-03T12:00:00Z",
        )
        client = Mock()
        client.read_merge_train_snapshot.return_value = snapshot
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            store.write_merge_train_batch_candidate_record(record)
            result = _reflow_stale_candidate_record(
                request=MergeTrainControllerRunOnceEnvelope(
                    repository=snapshot.repository, base_branch=snapshot.base_branch, mutate=True
                ),
                policy=self.policy,
                policy_sha256="policy-digest",
                repository_policy=self.policy.policies[0],
                transport=self.provider,
                github_client=client,
                candidate_store=store,
                stack_collapse_store=store,
                candidate_record=record,
                trace_id="test",
                recorded_at="2026-10-03T12:01:00Z",
                lease=Mock(),
            )
            assert result is not None
            self.assertEqual(result["controller_action"], "wait_for_checks")
            self.assertEqual(
                cast(dict[str, Any], result["dry_run_result"])["selected_pr"]["number"],
                snapshot.pull_requests[1].number,
            )
            self.assertEqual(store.list_merge_train_batch_candidate_records()[0].status, "active")
            client.build_batch_candidate.assert_not_called()

    def test_later_review_wait_prevents_partial_squash_or_rebase_landing(self) -> None:
        for method in ("squash", "rebase"):
            with self.subTest(method=method):
                policy = self.policy.model_copy(
                    update={
                        "policies": tuple(
                            entry.model_copy(update={"merge_method": method})
                            for entry in self.policy.policies
                        )
                    }
                )
                snapshot = self.snapshot.model_copy(
                    update={
                        "pull_requests": (
                            self.snapshot.pull_requests[0],
                            self.snapshot.pull_requests[1].model_copy(
                                update={
                                    "owner_review_required": True,
                                    "required_checks_status": "pending",
                                }
                            ),
                        )
                    }
                )
                result = build_merge_train_dry_run_result(
                    policy=policy, snapshot=snapshot, batch_landing=True
                )
                self.assertEqual(result.intended_next_action, "wait_for_checks")
                self.assertEqual(
                    result.selected_pr.number if result.selected_pr else None,
                    snapshot.pull_requests[1].number,
                )

    def test_standalone_landing_route_requires_and_passes_profile_reader(self) -> None:
        from types import SimpleNamespace
        from control_plane.merge_admission import MergeAdmissionDeniedError
        from control_plane.merge_train_batch_landing import (
            _execute_land_mode,
            MergeTrainBatchLandingRunOnceEnvelope,
        )
        from tests.test_merge_train_structural_provenance import _entry, _records

        candidate_record, landing_record = _records((_entry(1, 1), _entry(2, 2)))
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            store.write_merge_train_batch_candidate_record(candidate_record)
            store.write_merge_train_batch_landing_plan_record(landing_record)
            request = MergeTrainBatchLandingRunOnceEnvelope(
                repository=candidate_record.candidate.repository,
                base_branch="main",
                mode="land",
                landing_plan_record_id=landing_record.record_id,
            )
            kwargs: dict[str, Any] = dict(
                request=request,
                repository_policy=self.policy.policies[0],
                policy_sha256=candidate_record.candidate.policy_sha256,
                token="test",
                trace_id="test",
                recorded_at="2026-10-03T12:00:00Z",
                landing_store=store,
                stack_collapse_store=store,
                admission_store=store,
                admission_evaluator=Mock(),
                controller_state_provider=Mock(),
                mutation_checkpoint=None,
            )
            with patch("control_plane.merge_train_batch_landing.GitHubMergeTrainClient") as client:
                client.return_value.land_batch_candidate.side_effect = MergeAdmissionDeniedError(
                    "test stop"
                )
                with self.assertRaises(MergeAdmissionDeniedError):
                    _execute_land_mode(candidate_store=store, **kwargs)
                self.assertIs(client.call_args.kwargs["branch_refresh_store"], store)
            missing_reader = SimpleNamespace(
                list_merge_train_batch_candidate_records=store.list_merge_train_batch_candidate_records
            )
            with patch("control_plane.merge_train_batch_landing.GitHubMergeTrainClient") as client:
                with self.assertRaises(MergeAdmissionDeniedError) as refused:
                    _execute_land_mode(candidate_store=missing_reader, **kwargs)
                self.assertEqual(
                    refused.exception.reason_code, "client_review_profiles_unavailable"
                )
                client.assert_not_called()

    def test_later_labelled_member_waits_before_batch_planning(self) -> None:
        for status in ("pending", "fail"):
            with self.subTest(status=status):
                snapshot = self.snapshot.model_copy(
                    update={
                        "pull_requests": (
                            self.snapshot.pull_requests[0],
                            self.snapshot.pull_requests[1].model_copy(
                                update={
                                    "owner_review_required": True,
                                    "required_checks_status": status,
                                }
                            ),
                        )
                    }
                )
                result = build_merge_train_dry_run_result(
                    policy=self.policy, snapshot=snapshot, batch_landing=True
                )
                self.assertEqual(
                    result.intended_next_action,
                    "wait_for_checks" if status == "pending" else "block",
                )
                assert result.selected_pr is not None
                self.assertEqual(result.selected_pr.number, snapshot.pull_requests[1].number)
                with self.assertRaises(ValueError):
                    build_merge_train_batch_candidate(
                        dry_run_result=result,
                        base_sha=snapshot.base_sha,
                        policy_sha256="digest",
                        created_at="2026-10-03T12:00:00Z",
                    )

    def test_unavailable_body_evidence_reports_failure_without_retry(self) -> None:
        from control_plane.merge_train_github import MergeTrainGitHubError

        with (
            TemporaryDirectory() as directory,
            patch(
                "control_plane.merge_train_controller_run_once.changed_closed_batch_body",
                side_effect=MergeTrainGitHubError("provider unavailable", status_code=503),
            ),
        ):
            result = self._reflow(FilesystemRecordStore(state_dir=Path(directory)), self.candidate)
        self.assertEqual(result["controller_action"], "candidate_failed")
        self.assertEqual(result["reason_code"], "closed_batch_body_unchanged_or_unavailable")

    def test_unchanged_body_keeps_the_failure_stopped(self) -> None:
        with TemporaryDirectory() as directory:
            result = self._reflow(FilesystemRecordStore(state_dir=Path(directory)), self.candidate)
        self.assertEqual(result["controller_action"], "candidate_failed")
        self.assertEqual(result["reason_code"], "closed_batch_body_unchanged_or_unavailable")

    def test_only_bound_closed_unmerged_batch_is_recovery_evidence(self) -> None:
        for state in ("open", "merged", "missing", "wrong_binding"):
            with self.subTest(state=state):
                self.provider.closed = state != "open"
                self.provider.merged = state == "merged"
                self.provider.created = state != "missing"
                self.provider.body = (
                    "unbound"
                    if state == "wrong_binding"
                    else batch_pull_request_body(
                        client=self.client, candidate=self.candidate
                    ).split("## Owner test notes")[0]
                )
                self.assertFalse(
                    changed_closed_batch_body(client=self.client, candidate=self.candidate)
                )

    def test_failed_batch_reports_current_queue_head_wait_on_both_passes(self) -> None:
        self.snapshot = self.snapshot.model_copy(
            update={
                "pull_requests": tuple(
                    pr.model_copy(update={"required_checks_status": "pending"})
                    if index == 0
                    else pr
                    for index, pr in enumerate(self.snapshot.pull_requests)
                )
            }
        )
        for mutate in (False, True):
            with self.subTest(mutate=mutate), TemporaryDirectory() as directory:
                store = FilesystemRecordStore(state_dir=Path(directory))
                result = self._reflow(store, self.candidate, mutate=mutate)
                self.assertEqual(result["controller_action"], "wait_for_checks")
                self.assertEqual(
                    result["dry_run_result"]["selected_pr"]["number"],
                    self.snapshot.pull_requests[0].number,
                )
                self.assertEqual(
                    result["dry_run_result"]["intended_next_action"], "wait_for_checks"
                )
                from control_plane.merge_train_controller_feedback import build_feedback_payloads

                feedback = build_feedback_payloads(response={"result": result})
                self.assertEqual(
                    [item["pull_request_number"] for item in feedback],
                    [self.snapshot.pull_requests[0].number],
                )
                self.assertFalse(
                    any(
                        record.candidate.batch_body_retry_of
                        for record in store.list_merge_train_batch_candidate_records()
                    )
                )
