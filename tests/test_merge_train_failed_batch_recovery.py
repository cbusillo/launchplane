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
from control_plane.github_required_checks import CheckRequirement
from control_plane.merge_train import MergeTrainCheckStatus
from control_plane.merge_train import build_merge_train_dry_run_result
from control_plane.merge_train_batch_pull_request import (
    batch_pull_request_body,
    changed_closed_batch_body,
)
from control_plane.merge_train_candidate_failures import (
    CandidateCheckFailure,
    CandidateCheckFailures,
)
from control_plane.merge_train_controller_feedback import build_feedback_payloads
from control_plane.merge_train_controller_run_once import (
    MergeTrainControllerRunOnceEnvelope,
    _advance_from_live_snapshot,
    _records_for_result,
    _reflow_stale_candidate_record,
    try_reflow_failed_merge_train_candidate,
)
from control_plane.merge_train_github import (
    GitHubMergeTrainClient,
    MergeTrainGitHubError,
    MergeTrainBaseCheckObservation,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.contracts.merge_train_controller_state import MergeTrainControllerLeaseLostError
from tests.merge_train_policy_fixtures import (
    build_test_merge_train_policy,
)
from tests.support.merge_train import _FakeExpandedMergeTrainSnapshotReader
from tests.test_merge_train_batch_pull_request import _BatchProvider
from tests.test_merge_train_github import (
    _protected_branch_with_checks,
    _required_check_run,
)


class _RerunProvider(_BatchProvider):
    checks_failed = True
    checks_missing = False
    batch_head_sha: str | None = None

    def pull_request(self, number: int) -> dict[str, object]:
        payload = super().pull_request(number)
        if number == self.number and self.batch_head_sha is not None:
            cast(dict[str, object], payload["head"])["sha"] = self.batch_head_sha
        return payload

    def request(self, *, method: str, path: str, body: dict[str, object] | None = None) -> object:
        if method == "PUT" and path.endswith("/update-branch"):
            self.requests.append((method, path, body))
            return {"message": "Branch update accepted"}
        if method == "GET" and path.endswith("/branches/main"):
            self.requests.append((method, path, body))
            return _protected_branch_with_checks("ci-gate")
        if method == "GET" and "/check-runs?" in path:
            self.requests.append((method, path, body))
            failed = {
                **_required_check_run("ci-gate", "completed", "failure"),
                "id": 1,
                "check_suite": {"id": 10},
            }
            rerun = {
                **_required_check_run(
                    "ci-gate",
                    "completed" if self.checks_pass else "in_progress",
                    "success" if self.checks_pass else None,
                ),
                "id": 2,
                "check_suite": {"id": 10},
            }
            return {
                "check_runs": []
                if self.checks_missing
                else [failed]
                if self.checks_failed
                else [failed, rerun]
            }
        return super().request(method=method, path=path, body=body)


class FailedBatchRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prepare()

    def test_concurrent_workflow_success_does_not_hide_failed_candidate_recovery(self) -> None:
        original_request = self.provider.request

        def request(**kwargs: Any) -> object:
            if kwargs["method"] == "GET" and "/annotations?" in kwargs["path"]:
                return []
            result = original_request(**kwargs)
            if kwargs["method"] == "GET" and "/check-runs?" in kwargs["path"]:
                payload = cast(dict[str, Any], result)
                for check in payload["check_runs"]:
                    check["check_suite"] = {"id": 10}
                    if check["id"] == 2:
                        check["id"] = 4
                payload["check_runs"].append(
                    {
                        **_required_check_run("ci-gate", "completed", "success"),
                        "id": 3,
                        "check_suite": {"id": 20},
                    }
                )
            return result

        with patch.object(self.provider, "request", side_effect=request):
            failed = self.client.observe_batch_candidate_checks(candidate=self.candidate)
            self.assertEqual(failed.status, "failed")
            self.assertEqual(failed.required_checks_status, "fail")
            failures = self.client.read_candidate_check_failures(candidate=failed)
            self.assertTrue(failures.conclusive)
            self.assertEqual(failures.check_names, ("ci-gate",))
            self.provider.checks_failed = False
            self.provider.checks_pass = True
            with TemporaryDirectory() as directory:
                result = self._reflow(
                    FilesystemRecordStore(state_dir=Path(directory)), failed, observe_checks=True
                )
                self.assertEqual(result["reason_code"], "candidate_checks_recovered")
                replacement = MergeTrainBatchCandidate.model_validate(result["candidate"])
                restarted = FilesystemRecordStore(state_dir=Path(directory))
                persisted = restarted.list_merge_train_batch_candidate_records()
                self.assertTrue(
                    any(
                        record.candidate.check_retry_of == replacement.check_retry_of
                        for record in persisted
                    )
                )
                self.assertTrue(replacement.check_retry_of)

    def _prepare(self, *, singleton: bool = False) -> None:
        self.snapshot = _FakeExpandedMergeTrainSnapshotReader(
            transport=object()
        ).read_merge_train_snapshot(repository="cbusillo/sellyouroutboard", base_branch="main")
        if singleton:
            self.snapshot = self.snapshot.model_copy(
                update={"pull_requests": self.snapshot.pull_requests[:1]}
            )
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
        self.provider = _RerunProvider(self.candidate)
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
        observe_checks: bool = False,
        failure_evidence: CandidateCheckFailures | None = None,
        base_status: str | MergeTrainGitHubError = "pass",
        label_prepared: bool = True,
        lease: Mock | None = None,
    ) -> dict[str, Any]:
        record = build_merge_train_batch_candidate_record(
            candidate=candidate, source="test", updated_at="2026-10-03T12:00:00Z"
        )
        store.write_merge_train_batch_candidate_record(record)
        client = Mock(wraps=self.client)
        client.candidate_block_label_events.return_value = []
        client.candidate_block_application.return_value = {"status": "applied", "event_id": 1}
        client.read_merge_train_snapshot.return_value = self.snapshot
        if isinstance(base_status, MergeTrainGitHubError):
            client.read_base_check_observation.side_effect = base_status
        else:
            client.read_base_check_observation.return_value = MergeTrainBaseCheckObservation(
                cast(MergeTrainCheckStatus, base_status)
            )
        client.probe_batch_entry_conflicts.return_value = conflicts
        client.add_pull_request_label.return_value = None
        client.ensure_repository_label.return_value = label_prepared
        if not observe_checks:
            client.observe_batch_candidate_checks.return_value = candidate
        if not observe_checks or failure_evidence is not None:
            client.read_candidate_check_failures.return_value = (
                failure_evidence
                or CandidateCheckFailures(
                    attribution_requirements=(CheckRequirement("ci-gate"),),
                    checks=(CandidateCheckFailure(name="ci-gate", conclusion="failure", url=""),),
                )
            )
        self.reflow_client = client
        result = try_reflow_failed_merge_train_candidate(
            candidate_store=store,
            stack_collapse_store=store,
            active_candidate_record=record,
            policy=self.policy,
            policy_sha256="policy-digest",
            github_client=cast(GitHubMergeTrainClient, client),
            repository=self.snapshot.repository,
            base_branch=self.snapshot.base_branch,
            merge_method="merge",
            recorded_at="2026-10-03T12:01:00Z",
            trace_id="recovery",
            mutate=mutate,
            lease=lease
            or Mock(
                record=Mock(
                    updated_at=self.candidate.updated_at, step_payload={}, ordinary_job_binding=None
                )
            ),
        )
        assert result is not None
        return result

    def test_failed_candidate_reflow_does_not_admit_new_arrivals_over_cap(self) -> None:
        self.policy = self.policy.model_copy(
            update={"policies": (self.policy.policies[0].model_copy(update={"max_batch_size": 2}),)}
        )
        third = self.snapshot.pull_requests[-1].model_copy(
            update={"number": 3, "head_sha": "head-3", "created_at": "2026-05-08T11:00:00Z"}
        )
        self.snapshot = self.snapshot.model_copy(
            update={"pull_requests": (*self.snapshot.pull_requests, third)}
        )
        # An unbuilt failure follows the ordinary reflow/probe replacement path.
        unbuilt = self.candidate.model_copy(update={"candidate_sha": ""})
        with TemporaryDirectory() as directory:
            result = self._reflow(FilesystemRecordStore(state_dir=Path(directory)), unbuilt)
        replacement = MergeTrainBatchCandidate.model_validate(result["candidate"])
        self.assertEqual([entry.pull_request_number for entry in replacement.entries], [1, 2])
        self.assertEqual(result["dry_run_result"]["queue_order"], [1, 2])

    def test_base_exploration_history_is_scoped_to_exact_current_inputs(self) -> None:
        for changed in ("none", "base", "head", "policy", "added"):
            with self.subTest(changed=changed), TemporaryDirectory() as directory:
                self._prepare()
                store = FilesystemRecordStore(state_dir=Path(directory))
                for pull in self.snapshot.pull_requests:
                    singleton_snapshot = self.snapshot.model_copy(update={"pull_requests": (pull,)})
                    singleton = build_merge_train_batch_candidate(
                        dry_run_result=build_merge_train_dry_run_result(
                            policy=self.policy, snapshot=singleton_snapshot, batch_landing=True
                        ),
                        base_sha="old-base" if changed == "base" else self.snapshot.base_sha,
                        policy_sha256="old-policy" if changed == "policy" else "policy-digest",
                        created_at=self.candidate.created_at,
                    ).model_copy(
                        update={
                            "candidate_sha": f"failed-{pull.number}",
                            "status": "failed",
                            "required_checks_status": "fail",
                        }
                    )
                    store.write_merge_train_batch_candidate_record(
                        build_merge_train_batch_candidate_record(
                            candidate=singleton,
                            source="test:earlier-singleton",
                            updated_at=self.candidate.created_at,
                        ).model_copy(update={"status": "superseded"})
                    )
                if changed == "head":
                    self.snapshot = self.snapshot.model_copy(
                        update={
                            "pull_requests": (
                                self.snapshot.pull_requests[0].model_copy(
                                    update={"head_sha": "new-head-1"}
                                ),
                                *self.snapshot.pull_requests[1:],
                            )
                        }
                    )
                elif changed == "added":
                    self.snapshot = self.snapshot.model_copy(
                        update={
                            "pull_requests": (
                                *self.snapshot.pull_requests,
                                self.snapshot.pull_requests[-1].model_copy(
                                    update={
                                        "number": 3,
                                        "head_sha": "head-3",
                                        "head_ref": "feature-3",
                                        "created_at": "2026-10-03T12:30:00Z",
                                    }
                                ),
                            )
                        }
                    )
                failed = build_merge_train_batch_candidate(
                    dry_run_result=build_merge_train_dry_run_result(
                        policy=self.policy, snapshot=self.snapshot, batch_landing=True
                    ),
                    base_sha=self.snapshot.base_sha,
                    policy_sha256="policy-digest",
                    created_at=self.candidate.created_at,
                ).model_copy(
                    update={
                        "candidate_sha": "failed-current-group",
                        "status": "failed",
                        "required_checks_status": "fail",
                    }
                )
                result = self._reflow(store, failed, base_status="fail")
                self.reflow_client.add_pull_request_label.assert_not_called()
                if changed == "none":
                    self.assertEqual(result["reason_code"], "base_failure_exploration_exhausted")
                    self.assertEqual(result["controller_action"], "candidate_failed")
                else:
                    self.assertEqual(result["reason_code"], "base_failure_exploration")
                    selected = MergeTrainBatchCandidate.model_validate(result["candidate"])
                    self.assertEqual(
                        tuple(entry.pull_request_number for entry in selected.entries),
                        (3,) if changed == "added" else (1,),
                    )

    def test_pending_client_head_stops_before_base_exploration(self) -> None:
        waiting = self.snapshot.pull_requests[1]
        self.snapshot = self.snapshot.model_copy(
            update={
                "pull_requests": (
                    *self.snapshot.pull_requests,
                    waiting.model_copy(
                        update={
                            "number": 3,
                            "head_sha": "manual-head",
                            "labels": (*waiting.labels, "merge-blocked"),
                        }
                    ),
                    waiting.model_copy(
                        update={
                            "number": 4,
                            "head_sha": "client-head",
                            "owner_review_required": True,
                            "required_checks_status": "pending",
                        }
                    ),
                )
            }
        )
        with TemporaryDirectory() as directory:
            result = self._reflow(
                FilesystemRecordStore(state_dir=Path(directory)), self.candidate, base_status="fail"
            )
            self.assertEqual(result["controller_action"], "wait_for_checks")
            self.assertEqual(result["reason_code"], "original_head_checks_pending")
            self.assertNotIn("candidate", result)
            self.reflow_client.add_pull_request_label.assert_not_called()
            self.assertFalse(
                any(
                    method == "DELETE" and "/labels/" in path
                    for method, path, _ in self.provider.requests
                )
            )

    def test_base_exploration_routes_or_defers_stacks_without_starving_later_repair(self) -> None:
        for case in (
            "ready",
            "unsupported",
            "preparation",
            "unsupported_without_repair",
            "preparation_without_repair",
            "lease_lost_before_stack_persist",
            "crash_after_stack_persist",
        ):
            with self.subTest(case=case), TemporaryDirectory() as directory:
                self._prepare()
                first, root = self.snapshot.pull_requests
                child = root.model_copy(
                    update={
                        "number": 3,
                        "head_sha": "stack-child-head",
                        "head_ref": "feature/stack-child",
                        "base_ref": root.head_ref,
                        "base_sha": root.head_sha,
                    }
                )
                extras = []
                if case.startswith("unsupported"):
                    extras.append(
                        child.model_copy(
                            update={
                                "number": 5,
                                "head_sha": "sibling-head",
                                "head_ref": "feature/sibling",
                            }
                        )
                    )
                if not case.endswith("without_repair"):
                    extras.append(
                        root.model_copy(
                            update={
                                "number": 4,
                                "head_sha": "repair-head",
                                "head_ref": "feature/repair",
                                "created_at": "2026-10-03T12:30:00Z",
                            }
                        )
                    )
                self.snapshot = self.snapshot.model_copy(
                    update={"pull_requests": (first, root, child, *extras)}
                )
                store = FilesystemRecordStore(state_dir=Path(directory))
                first_failed = build_merge_train_batch_candidate(
                    dry_run_result=build_merge_train_dry_run_result(
                        policy=self.policy,
                        snapshot=self.snapshot.model_copy(update={"pull_requests": (first,)}),
                        batch_landing=True,
                    ),
                    base_sha=self.snapshot.base_sha,
                    policy_sha256="policy-digest",
                    created_at=self.candidate.created_at,
                ).model_copy(
                    update={
                        "candidate_sha": "failed-first-singleton",
                        "status": "failed",
                        "required_checks_status": "fail",
                    }
                )
                store.write_merge_train_batch_candidate_record(
                    build_merge_train_batch_candidate_record(
                        candidate=first_failed,
                        source="test:earlier-singleton",
                        updated_at=self.candidate.created_at,
                    ).model_copy(update={"status": "superseded"})
                )
                failed = build_merge_train_batch_candidate(
                    dry_run_result=build_merge_train_dry_run_result(
                        policy=self.policy, snapshot=self.snapshot, batch_landing=True
                    ),
                    base_sha=self.snapshot.base_sha,
                    policy_sha256="policy-digest",
                    created_at=self.candidate.created_at,
                ).model_copy(
                    update={
                        "candidate_sha": "failed-current-group",
                        "status": "failed",
                        "required_checks_status": "fail",
                    }
                )
                if case in {"lease_lost_before_stack_persist", "crash_after_stack_persist"}:
                    lease = Mock(
                        record=Mock(
                            updated_at=self.candidate.updated_at,
                            step_payload={},
                            ordinary_job_binding=None,
                        )
                    )
                    phase = (
                        "persist_stack_collapse_plan"
                        if case == "lease_lost_before_stack_persist"
                        else "stack_collapse_plan_recorded"
                    )
                    failure = (
                        MergeTrainControllerLeaseLostError("Lease adopted by another pass")
                        if case == "lease_lost_before_stack_persist"
                        else RuntimeError("Process interrupted after plan write")
                    )

                    def checkpoint(**updates: object) -> None:
                        if updates.get("active_phase") == phase:
                            raise failure

                    lease.checkpoint.side_effect = checkpoint
                    with self.assertRaises(type(failure)):
                        self._reflow(store, failed, base_status="fail", lease=lease)
                    store = FilesystemRecordStore(state_dir=Path(directory))
                    self.assertTrue(
                        any(
                            record.candidate.candidate_sha == failed.candidate_sha
                            for record in store.list_merge_train_batch_candidate_records(
                                status="active"
                            )
                        )
                    )
                    retained = store.list_merge_train_stack_collapse_plan_records(status="active")
                    if case == "lease_lost_before_stack_persist":
                        self.assertFalse(retained)
                    else:
                        self.assertTrue(retained)
                        retained_identity = {record.plan.collapse_id for record in retained}
                    self.reflow_client.add_pull_request_label.assert_not_called()
                result = self._reflow(
                    store,
                    failed,
                    base_status="fail",
                    label_prepared=not case.startswith("preparation"),
                )
                if case in {
                    "ready",
                    "lease_lost_before_stack_persist",
                    "crash_after_stack_persist",
                }:
                    self.assertEqual(result["controller_action"], "plan_stack_collapse")
                    plan = result["stack_collapse_plan"]
                    self.assertEqual(
                        [entry["pull_request_number"] for entry in plan["entries"]], [2, 3]
                    )
                    self.assertTrue(
                        store.list_merge_train_stack_collapse_plan_records(status="active")
                    )
                    if case == "crash_after_stack_persist":
                        self.assertEqual({plan["collapse_id"]}, retained_identity)
                    self.assertEqual(
                        store.list_merge_train_batch_candidate_records(status="active"), ()
                    )
                elif case.endswith("without_repair"):
                    self.assertEqual(result["controller_action"], "candidate_failed")
                    self.assertEqual(result["reason_code"], "base_failure_exploration_exhausted")
                    active = store.list_merge_train_batch_candidate_records(status="active")
                    self.assertEqual(len(active), 1)
                    self.assertEqual(active[0].candidate.candidate_sha, failed.candidate_sha)
                else:
                    self.assertEqual(result["controller_action"], "plan_candidate")
                    selected = MergeTrainBatchCandidate.model_validate(result["candidate"])
                    self.assertEqual([entry.pull_request_number for entry in selected.entries], [4])
                    active = store.list_merge_train_batch_candidate_records(status="active")
                    self.assertEqual(
                        {record.candidate.batch_id for record in active}, {selected.batch_id}
                    )
                self.reflow_client.add_pull_request_label.assert_not_called()

    def _four_member_failed_candidate(self) -> MergeTrainBatchCandidate:
        extra = tuple(
            self.snapshot.pull_requests[-1].model_copy(
                update={
                    "number": number,
                    "head_sha": f"head-{number}",
                    "created_at": f"2026-05-08T11:0{number}:00Z",
                }
            )
            for number in (3, 4)
        )
        self.snapshot = self.snapshot.model_copy(
            update={"pull_requests": (*self.snapshot.pull_requests, *extra)}
        )
        failed = build_merge_train_batch_candidate(
            dry_run_result=build_merge_train_dry_run_result(
                policy=self.policy, snapshot=self.snapshot, batch_landing=True
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
        self.assertEqual(len(failed.entries), 4)
        self.provider.candidate = failed
        self.provider.heads.update(
            {entry.pull_request_number: entry.head_sha for entry in failed.entries}
        )
        self.provider.body = batch_pull_request_body(client=self.client, candidate=failed)
        return failed

    def test_lowered_cap_still_isolates_failed_existing_batch(self) -> None:
        failed = self._four_member_failed_candidate()
        self.policy = self.policy.model_copy(
            update={"policies": (self.policy.policies[0].model_copy(update={"max_batch_size": 1}),)}
        )
        with TemporaryDirectory() as directory:
            result = self._reflow(FilesystemRecordStore(state_dir=Path(directory)), failed)
        replacement = MergeTrainBatchCandidate.model_validate(result["candidate"])
        self.assertEqual(result["reason_code"], "failure_isolation")
        self.assertEqual([entry.pull_request_number for entry in replacement.entries], [1])
        self.assertEqual([entry.pull_request_number for entry in replacement.held_out], [2, 3, 4])
        self.reflow_client.add_pull_request_label.assert_not_called()

    def test_lowered_cap_isolates_behind_singleton_without_refreshing(self) -> None:
        failed = self._four_member_failed_candidate()
        self.snapshot = self.snapshot.model_copy(
            update={
                "pull_requests": (
                    self.snapshot.pull_requests[0].model_copy(
                        update={"branch_update_required": True}
                    ),
                    *self.snapshot.pull_requests[1:],
                )
            }
        )
        self.policy = self.policy.model_copy(
            update={"policies": (self.policy.policies[0].model_copy(update={"max_batch_size": 1}),)}
        )
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            result = self._reflow(store, failed)
            active = store.list_merge_train_batch_candidate_records(status="active")
            self.assertEqual(
                [entry.pull_request_number for entry in active[0].candidate.entries], [1]
            )
        self.assertEqual(result["controller_action"], "plan_candidate")
        self.assertEqual(result["reason_code"], "failure_isolation")
        self.reflow_client.update_pull_request_branch.assert_not_called()
        self.reflow_client.add_pull_request_label.assert_not_called()

    def test_changed_queue_does_not_wait_on_client_outside_lowered_cap(self) -> None:
        failed = self._four_member_failed_candidate()
        newcomer = self.snapshot.pull_requests[-1].model_copy(
            update={
                "number": 5,
                "head_sha": "head-5",
                "created_at": "2026-05-08T12:00:00Z",
                "owner_review_required": True,
                "required_checks_status": "pending",
            }
        )
        self.snapshot = self.snapshot.model_copy(
            update={"pull_requests": (*self.snapshot.pull_requests[1:], newcomer)}
        )
        self.policy = self.policy.model_copy(
            update={"policies": (self.policy.policies[0].model_copy(update={"max_batch_size": 1}),)}
        )
        with TemporaryDirectory() as directory:
            result = self._reflow(FilesystemRecordStore(state_dir=Path(directory)), failed)
        replacement = MergeTrainBatchCandidate.model_validate(result["candidate"])
        self.assertEqual(result["controller_action"], "plan_candidate")
        self.assertEqual([entry.pull_request_number for entry in replacement.entries], [2])
        self.reflow_client.add_pull_request_label.assert_not_called()

    def test_passing_exact_sha_rebuilds_closed_unchanged_batch_without_blame(self) -> None:
        self._check_passing_exact_sha_rebuilds_closed_candidate()

    def test_passing_exact_sha_rebuilds_closed_singleton_without_reopening_it(self) -> None:
        self._prepare(singleton=True)
        self._check_passing_exact_sha_rebuilds_closed_candidate()

    def test_legacy_singleton_without_generated_pr_reobserves_its_exact_sha(self) -> None:
        self._prepare(singleton=True)
        self.provider.created = False
        self.provider.checks_failed = False
        self.provider.requests.clear()
        with TemporaryDirectory() as directory:
            result = self._reflow(
                FilesystemRecordStore(state_dir=Path(directory)),
                self.candidate,
                observe_checks=True,
            )
        self.assertEqual(result["controller_action"], "observe_candidate")
        self.assertEqual(result["candidate"]["candidate_sha"], self.candidate.candidate_sha)
        self.assertEqual(result["candidate"]["status"], "passed")
        self.assertTrue(all(method == "GET" for method, _, _ in self.provider.requests))

    def test_unbound_closed_singleton_is_not_legacy_reobservation(self) -> None:
        self._prepare(singleton=True)
        self.provider.checks_failed = False
        self.provider.body = "manually edited generated PR"
        self.provider.batch_head_sha = "different-generated-head"
        with TemporaryDirectory() as directory:
            result = self._reflow(
                FilesystemRecordStore(state_dir=Path(directory)),
                self.candidate,
                observe_checks=True,
            )
        self.assertEqual(result["controller_action"], "candidate_failed")
        self.assertTrue(all(method == "GET" for method, _, _ in self.provider.requests))

    def test_legacy_singleton_discovery_error_keeps_recovery_stopped(self) -> None:
        self._prepare(singleton=True)
        self.provider.created = False
        self.provider.checks_failed = False
        with (
            TemporaryDirectory() as directory,
            patch(
                "control_plane.merge_train_controller_run_once.has_batch_pull_request",
                side_effect=MergeTrainGitHubError("unavailable", status_code=503),
            ),
        ):
            result = self._reflow(
                FilesystemRecordStore(state_dir=Path(directory)),
                self.candidate,
                observe_checks=True,
            )
        self.assertEqual(result["controller_action"], "candidate_failed")

    def _check_passing_exact_sha_rebuilds_closed_candidate(self) -> None:
        failed_observation = self.client.observe_batch_candidate_checks(candidate=self.candidate)
        self.assertEqual(failed_observation.required_checks_status, "fail")
        self.provider.checks_failed = False
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            original_body = self.provider.body
            dry = self._reflow(store, self.candidate, mutate=False, observe_checks=True)
            self.assertEqual(dry["reason_code"], "candidate_checks_recovered")
            self.assertTrue(
                all(
                    record.status == "active"
                    for record in store.list_merge_train_batch_candidate_records()
                )
            )
            self.assertFalse(
                any(
                    record.candidate.check_retry_of
                    for record in store.list_merge_train_batch_candidate_records()
                )
            )
            result = self._reflow(store, self.candidate, observe_checks=True)
            replacement = MergeTrainBatchCandidate.model_validate(result["candidate"])
            self.assertEqual(result["controller_action"], "plan_candidate")
            self.assertEqual(replacement.entries, self.candidate.entries)
            self.assertEqual(replacement.base_sha, self.candidate.base_sha)
            self.assertEqual(replacement.status, "planned")
            self.assertEqual(replacement.required_checks_status, "unknown")
            self.assertNotEqual(replacement.candidate_ref, self.candidate.candidate_ref)
            self.assertEqual(
                replacement.check_retry_of,
                result["superseded_merge_train_batch_candidate_record_id"],
            )
            self.assertEqual(
                result["details"]["observed_candidate_sha"], self.candidate.candidate_sha
            )
            self.assertEqual(self.provider.body, original_body)
            self.assertTrue(self.provider.closed)
            self.assertTrue(all(method == "GET" for method, _, _ in self.provider.requests))
            check_paths = [path for _, path, _ in self.provider.requests if "/check-runs?" in path]
            self.assertTrue(check_paths)
            self.assertTrue(
                all(f"/commits/{self.candidate.candidate_sha}/" in path for path in check_paths)
            )
            self.reflow_client.add_pull_request_label.assert_not_called()
            restarted = FilesystemRecordStore(state_dir=Path(directory))
            active = restarted.list_merge_train_batch_candidate_records(status="active")
            self.assertEqual(
                [record.record_id for record in active],
                [result["merge_train_batch_candidate_record_id"]],
            )
            self.assertEqual(active[0].candidate.check_retry_of, replacement.check_retry_of)
            evidence = active[0].candidate.check_retry_evidence
            assert evidence is not None
            self.assertEqual(evidence.candidate_sha, self.candidate.candidate_sha)
            self.assertEqual(evidence.required_checks_status, "pass")
            self.assertEqual(evidence.closed_batch_pull_request_number, self.provider.number)
            self.assertEqual(evidence.controller_pass_recorded_at, active[0].updated_at)
            # The rebuild uses ordinary batch creation and must receive its own CI.
            built = replacement.model_copy(
                update={"candidate_sha": "rebuilt-sha", "status": "ready_for_checks"}
            )
            provider = _RerunProvider(built)
            provider.checks_failed = False
            provider.checks_pass = False
            new_client = GitHubMergeTrainClient(transport=provider)
            new_client.ensure_batch_pull_request(candidate=built)
            observed = new_client.observe_batch_candidate_checks(candidate=built)
            self.assertEqual(observed.required_checks_status, "pending")
            self.assertEqual(observed.status, "ready_for_checks")

    def test_candidate_history_filters_base_before_limit(self) -> None:
        for backend in ("filesystem", "sqlite"):
            with self.subTest(backend=backend), TemporaryDirectory() as directory:
                store = (
                    FilesystemRecordStore(state_dir=Path(directory))
                    if backend == "filesystem"
                    else PostgresRecordStore(
                        database_url=f"sqlite+pysqlite:///{Path(directory) / 'records.sqlite3'}"
                    )
                )
                if isinstance(store, PostgresRecordStore):
                    store.ensure_schema()
                try:
                    current = build_merge_train_batch_candidate_record(
                        candidate=self.candidate, source="test", updated_at="2026-10-03T12:00:00Z"
                    )
                    other = build_merge_train_batch_candidate_record(
                        candidate=self.candidate.model_copy(update={"base_sha": "other-base"}),
                        source="test",
                        updated_at="2026-10-03T12:10:00Z",
                    )
                    store.write_merge_train_batch_candidate_record(current)
                    store.write_merge_train_batch_candidate_record(other)
                    self.assertEqual(
                        store.list_merge_train_batch_candidate_records(
                            repository=self.snapshot.repository,
                            base_branch=self.snapshot.base_branch,
                            base_sha=self.candidate.base_sha,
                            limit=1,
                        ),
                        (current,),
                    )
                    self.assertEqual(
                        store.list_merge_train_batch_candidate_records(base_sha="missing-base"), ()
                    )
                finally:
                    if isinstance(store, PostgresRecordStore):
                        store.close()

    def test_passing_rerun_retry_budget_survives_restart(self) -> None:
        self.provider.checks_failed = False
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            self._reflow(store, self.candidate, observe_checks=True)
            restarted = FilesystemRecordStore(state_dir=Path(directory))
            # Even replaying the original failure cannot mint another retry ref.
            result = self._reflow(restarted, self.candidate, observe_checks=True)
            self.assertEqual(result["controller_action"], "candidate_failed")
            self.assertEqual(result["reason_code"], "candidate_check_retry_used")
            self.assertEqual(
                sum(
                    bool(record.candidate.check_retry_of)
                    for record in restarted.list_merge_train_batch_candidate_records()
                ),
                1,
            )
            self.reflow_client.add_pull_request_label.assert_not_called()

    def test_base_exploration_observes_passing_rerun_of_a_deferred_singleton(self) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            prior = build_merge_train_batch_candidate(
                dry_run_result=build_merge_train_dry_run_result(
                    policy=self.policy,
                    snapshot=self.snapshot.model_copy(
                        update={"pull_requests": self.snapshot.pull_requests[:1]}
                    ),
                    batch_landing=True,
                ),
                base_sha=self.snapshot.base_sha,
                policy_sha256="policy-digest",
                created_at=self.candidate.created_at,
            ).model_copy(
                update={
                    "candidate_sha": "earlier-singleton-sha",
                    "status": "failed",
                    "required_checks_status": "fail",
                }
            )
            parent = build_merge_train_batch_candidate_record(
                candidate=prior, source="test:deferred-singleton", updated_at=prior.created_at
            ).model_copy(update={"status": "superseded"})
            store.write_merge_train_batch_candidate_record(parent)
            current = build_merge_train_batch_candidate(
                dry_run_result=build_merge_train_dry_run_result(
                    policy=self.policy,
                    snapshot=self.snapshot.model_copy(
                        update={"pull_requests": self.snapshot.pull_requests[1:]}
                    ),
                    batch_landing=True,
                ),
                base_sha=self.snapshot.base_sha,
                policy_sha256="policy-digest",
                created_at=self.candidate.created_at,
                held_out=(
                    MergeTrainBatchHeldOutEntry(
                        pull_request_number=prior.entries[0].pull_request_number,
                        head_sha=prior.entries[0].head_sha,
                        reason="failure_isolation",
                        probe_base_sha=prior.base_sha,
                    ),
                ),
            ).model_copy(
                update={
                    "candidate_sha": "current-singleton-sha",
                    "status": "failed",
                    "required_checks_status": "fail",
                }
            )
            self.provider = _RerunProvider(prior)
            self.provider.created = True
            self.provider.closed = True
            self.provider.checks_failed = False
            self.provider.checks_pass = True
            self.client = GitHubMergeTrainClient(transport=self.provider)
            self.provider.body = batch_pull_request_body(client=self.client, candidate=prior)
            real_observe = self.client.observe_batch_candidate_checks

            def observe(*, candidate: MergeTrainBatchCandidate) -> MergeTrainBatchCandidate:
                return (
                    real_observe(candidate=candidate)
                    if candidate.candidate_sha == prior.candidate_sha
                    else candidate
                )

            with patch.object(self.client, "observe_batch_candidate_checks", side_effect=observe):
                failure = CandidateCheckFailures(
                    attribution_requirements=(CheckRequirement("ci-gate"),),
                    checks=(CandidateCheckFailure(name="ci-gate", conclusion="failure", url=""),),
                )
                result = self._reflow(
                    store,
                    current,
                    observe_checks=True,
                    failure_evidence=failure,
                    base_status="fail",
                )
                self.assertEqual(result["reason_code"], "candidate_checks_recovered")
                retry = MergeTrainBatchCandidate.model_validate(result["candidate"])
                self.assertEqual(retry.entries, prior.entries)
                self.assertEqual(retry.check_retry_of, parent.record_id)
                recovered_feedback = build_feedback_payloads(response={"result": result})
                exploration_feedback = build_feedback_payloads(
                    response={
                        "result": {
                            **result,
                            "reason_code": "base_failure_exploration",
                        }
                    }
                )
                held_numbers = {entry.pull_request_number for entry in retry.held_out}
                self.assertEqual(
                    held_numbers, {entry.pull_request_number for entry in current.entries}
                )
                recovered_messages = {
                    payload["pull_request_number"]: payload["message"]
                    for payload in recovered_feedback
                    if payload["pull_request_number"] in held_numbers
                }
                exploration_messages = {
                    payload["pull_request_number"]: payload["message"]
                    for payload in exploration_feedback
                    if payload["pull_request_number"] in held_numbers
                }
                self.assertEqual(set(recovered_messages), held_numbers)
                self.assertEqual(set(exploration_messages), held_numbers)
                self.assertEqual(recovered_messages, exploration_messages)
                restarted = FilesystemRecordStore(state_dir=Path(directory))
                again = self._reflow(
                    restarted,
                    current,
                    observe_checks=True,
                    failure_evidence=failure,
                    base_status="fail",
                )
                self.assertEqual(again["controller_action"], "candidate_failed")
                self.assertEqual(
                    {
                        record.candidate.candidate_ref
                        for record in restarted.list_merge_train_batch_candidate_records()
                        if record.candidate.check_retry_of == parent.record_id
                    },
                    {retry.candidate_ref},
                )
                self.reflow_client.add_pull_request_label.assert_not_called()

    def test_spent_check_retry_does_not_suppress_unused_body_retry(self) -> None:
        self.provider.checks_failed = False
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            self._reflow(store, self.candidate, observe_checks=True)
            self.provider.body = self.provider.body.split("## Owner test notes")[0]
            result = self._reflow(store, self.candidate, observe_checks=True)
            replacement = MergeTrainBatchCandidate.model_validate(result["candidate"])
            self.assertEqual(result["controller_action"], "plan_candidate")
            self.assertTrue(replacement.batch_body_retry_of)
            self.assertFalse(replacement.check_retry_of)
            self.assertEqual(replacement.entries, self.candidate.entries)

    def test_new_failure_recovers_after_rerun_despite_earlier_queue_retry(self) -> None:
        self.provider.checks_failed = False
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            first = self._reflow(store, self.candidate, observe_checks=True)
            replacement = MergeTrainBatchCandidate.model_validate(first["candidate"])
            later_failure = replacement.model_copy(
                update={
                    "candidate_sha": "later-failed-sha",
                    "status": "failed",
                    "required_checks_status": "fail",
                }
            )
            self.provider.candidate = later_failure
            self.provider.body = batch_pull_request_body(
                client=self.client, candidate=later_failure
            )
            self.provider.batch_head_sha = later_failure.candidate_sha
            recovered = self._reflow(store, later_failure, observe_checks=True)
            next_candidate = MergeTrainBatchCandidate.model_validate(recovered["candidate"])
            self.assertEqual(recovered["controller_action"], "plan_candidate")
            self.assertEqual(next_candidate.entries, later_failure.entries)
            self.assertNotEqual(next_candidate.candidate_ref, replacement.candidate_ref)
            assert next_candidate.check_retry_evidence is not None
            self.assertEqual(
                next_candidate.check_retry_evidence.candidate_sha, later_failure.candidate_sha
            )

    def test_rebuilt_candidate_real_failure_still_isolates_members(self) -> None:
        self.provider.checks_failed = False
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            result = self._reflow(store, self.candidate, observe_checks=True)
            replacement = MergeTrainBatchCandidate.model_validate(result["candidate"])
            failed_retry = replacement.model_copy(
                update={
                    "candidate_sha": "retry-failed-sha",
                    "status": "failed",
                    "required_checks_status": "fail",
                }
            )
            isolated = self._reflow(store, failed_retry)
            self.assertEqual(isolated["reason_code"], "failure_isolation")
            retest = MergeTrainBatchCandidate.model_validate(isolated["candidate"])
            self.assertLess(len(retest.entries), len(failed_retry.entries))
            self.assertTrue(any(entry.reason == "failure_isolation" for entry in retest.held_out))

    def test_pending_or_missing_checks_do_not_authorize_check_retry(self) -> None:
        self.provider.checks_failed = False
        self.provider.checks_pass = False
        with TemporaryDirectory() as directory:
            result = self._reflow(
                FilesystemRecordStore(state_dir=Path(directory)),
                self.candidate,
                observe_checks=True,
            )
            self.assertEqual(result["controller_action"], "candidate_failed")
            self.reflow_client.add_pull_request_label.assert_not_called()
        self.provider.checks_missing = True
        with TemporaryDirectory() as directory:
            result = self._reflow(
                FilesystemRecordStore(state_dir=Path(directory)),
                self.candidate,
                observe_checks=True,
            )
            self.assertEqual(result["controller_action"], "candidate_failed")
            self.reflow_client.add_pull_request_label.assert_not_called()

    def test_passing_checks_cannot_retry_missing_open_merged_or_unbound_batch(self) -> None:
        self.provider.checks_failed = False
        for state in ("open", "merged", "missing", "wrong_binding", "wrong_sha"):
            with self.subTest(state=state), TemporaryDirectory() as directory:
                self.provider.closed = state != "open"
                self.provider.merged = state == "merged"
                self.provider.created = state != "missing"
                self.provider.batch_head_sha = "wrong-sha" if state == "wrong_sha" else None
                self.provider.body = (
                    "unbound"
                    if state == "wrong_binding"
                    else batch_pull_request_body(client=self.client, candidate=self.candidate)
                )
                store = FilesystemRecordStore(state_dir=Path(directory))
                result = self._reflow(store, self.candidate, observe_checks=True)
                self.assertEqual(result["controller_action"], "candidate_failed")
                self.assertFalse(
                    any(
                        record.candidate.check_retry_of
                        for record in store.list_merge_train_batch_candidate_records()
                    )
                )

    def test_changed_body_does_not_rebuild_without_conclusive_failure_evidence(self) -> None:
        self.provider.body = self.provider.body.split("## Owner test notes")[0]
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            result = self._reflow(
                store, self.candidate, failure_evidence=CandidateCheckFailures(checks=())
            )
            self.assertEqual(result["controller_action"], "candidate_failed")
            self.reflow_client.add_pull_request_label.assert_not_called()
            self.assertFalse(
                any(
                    record.candidate.batch_body_retry_of
                    for record in store.list_merge_train_batch_candidate_records()
                )
            )

    def test_real_failure_is_split_even_when_generated_body_changed(self) -> None:
        self.provider.body = self.provider.body.split("## Owner test notes")[0]
        with TemporaryDirectory() as directory:
            result = self._reflow(FilesystemRecordStore(state_dir=Path(directory)), self.candidate)
        replacement = MergeTrainBatchCandidate.model_validate(result["candidate"])
        self.assertEqual(result["reason_code"], "failure_isolation")
        self.assertLess(len(replacement.entries), len(self.candidate.entries))
        self.assertFalse(replacement.batch_body_retry_of)

    def _fresh_plan(
        self,
        store: FilesystemRecordStore,
        *,
        mutate: bool,
        base_status: str = "pass",
        failure_evidence: CandidateCheckFailures | None = None,
        observe_checks: bool = False,
        conflicts: tuple[MergeTrainBatchHeldOutEntry, ...] = (),
        lease: Any = None,
    ) -> dict[str, object]:
        client = Mock(wraps=self.client)
        client.candidate_block_label_events.return_value = []
        client.candidate_block_application.return_value = {"status": "applied", "event_id": 1}
        if not observe_checks:
            client.observe_batch_candidate_checks.return_value = self.candidate
        client.read_candidate_check_failures.return_value = (
            failure_evidence
            or CandidateCheckFailures(
                attribution_requirements=(CheckRequirement("ci-gate"),),
                checks=(CandidateCheckFailure(name="ci-gate", conclusion="failure", url=""),),
            )
        )
        client.read_base_check_observation.return_value = MergeTrainBaseCheckObservation(
            cast(MergeTrainCheckStatus, base_status)
        )
        client.probe_batch_entry_conflicts.return_value = conflicts
        self.reflow_client = client
        client.add_pull_request_label.return_value = None
        return _advance_from_live_snapshot(
            request=MergeTrainControllerRunOnceEnvelope(
                repository=self.snapshot.repository, mutate=mutate
            ),
            policy=self.policy,
            policy_sha256="policy-digest",
            transport=self.provider,
            github_client=cast(GitHubMergeTrainClient, client),
            candidate_store=store,
            stack_collapse_store=store,
            trace_id="fresh-plan",
            recorded_at="2026-10-03T12:02:00Z",
            lease=lease
            or Mock(
                owner="test",
                acquisition_token="test",
                record=Mock(
                    ordinary_job_binding=None,
                    updated_at=self.candidate.updated_at,
                    step_payload={},
                ),
            ),
            snapshot=self.snapshot,
        )

    def test_retired_failure_is_not_rebuilt_after_restart_and_replanning(self) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            failed_record = build_merge_train_batch_candidate_record(
                candidate=self.candidate, source="test", updated_at="2026-10-03T12:00:00Z"
            ).model_copy(update={"status": "superseded"})
            store.write_merge_train_batch_candidate_record(failed_record)
            restarted = FilesystemRecordStore(state_dir=Path(directory))
            before = restarted.list_merge_train_batch_candidate_records()
            dry = self._fresh_plan(restarted, mutate=False)
            self.assertEqual(dry["reason_code"], "failure_isolation")
            self.assertEqual(restarted.list_merge_train_batch_candidate_records(), before)
            result = self._fresh_plan(restarted, mutate=True)
            replacement = MergeTrainBatchCandidate.model_validate(result["candidate"])
            self.assertEqual(result["reason_code"], "failure_isolation")
            self.assertNotEqual(replacement.entries, self.candidate.entries)
            self.assertEqual(
                result["superseded_merge_train_batch_candidate_record_id"], failed_record.record_id
            )

    def test_capped_infrastructure_retry_resumes_from_larger_parent(self) -> None:
        failed = self._four_member_failed_candidate()
        self.policy = self.policy.model_copy(
            update={"policies": (self.policy.policies[0].model_copy(update={"max_batch_size": 2}),)}
        )
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            result = self._reflow(
                store,
                failed,
                failure_evidence=CandidateCheckFailures(
                    checks=(CandidateCheckFailure(name="ci-gate", conclusion="cancelled", url=""),)
                ),
            )
            retry = MergeTrainBatchCandidate.model_validate(result["candidate"])
            self.assertEqual([entry.pull_request_number for entry in retry.entries], [1, 2])
            self.assertTrue(retry.infrastructure_retry_of)
            # Queue churn retired the unfinished retry; the same heads/base return.
            record = store.list_merge_train_batch_candidate_records(status="active")[0]
            store.write_merge_train_batch_candidate_record(
                record.model_copy(update={"status": "superseded"})
            )
            before = store.list_merge_train_batch_candidate_records()
            dry = self._fresh_plan(store, mutate=False)
            self.assertEqual(dry["reason_code"], "candidate_retry_resumed")
            self.assertEqual(store.list_merge_train_batch_candidate_records(), before)
            resumed_result = self._fresh_plan(store, mutate=True)
            resumed = MergeTrainBatchCandidate.model_validate(resumed_result["candidate"])
            self.assertEqual(resumed_result["reason_code"], "candidate_retry_resumed")
            self.assertEqual(resumed.candidate_ref, retry.candidate_ref)
            self.assertEqual(resumed.infrastructure_retry_of, retry.infrastructure_retry_of)
            self.reflow_client.add_pull_request_label.assert_not_called()

    def test_later_passing_retry_supersedes_historical_failure(self) -> None:
        for retry_field in ("check_retry_of", "infrastructure_retry_of"):
            with self.subTest(retry_field=retry_field), TemporaryDirectory() as directory:
                store = FilesystemRecordStore(state_dir=Path(directory))
                failure = build_merge_train_batch_candidate_record(
                    candidate=self.candidate, source="test", updated_at="2026-10-03T12:00:00Z"
                ).model_copy(update={"status": "superseded"})
                store.write_merge_train_batch_candidate_record(failure)
                passed = self.candidate.model_copy(
                    update={
                        "status": "passed",
                        "required_checks_status": "pass",
                        "candidate_sha": "passing-retry-sha",
                        retry_field: failure.record_id,
                    }
                )
                store.write_merge_train_batch_candidate_record(
                    build_merge_train_batch_candidate_record(
                        candidate=passed, source="test", updated_at="2026-10-03T12:01:00Z"
                    ).model_copy(update={"status": "superseded"})
                )
                result = self._fresh_plan(store, mutate=True)
                self.assertEqual(result["controller_action"], "plan_candidate")
                self.assertNotIn("reason_code", result)
                self.reflow_client.read_candidate_check_failures.assert_not_called()

    def test_interrupted_retry_resumes_its_recorded_ref_and_budget(self) -> None:
        original_snapshot = self.snapshot
        third = self.snapshot.pull_requests[1].model_copy(
            update={
                "number": 3,
                "head_sha": "third-head",
                "head_ref": "third-branch",
                "created_at": "2026-10-03T12:30:00Z",
            }
        )
        hold = MergeTrainBatchHeldOutEntry(
            pull_request_number=3,
            head_sha=third.head_sha,
            conflicts_with=(1, 2),
            probe_base_sha=self.snapshot.base_sha,
            conflicts_with_head_shas=tuple(pr.head_sha for pr in self.snapshot.pull_requests),
        )
        for retry_field in ("check_retry_of", "infrastructure_retry_of", "batch_body_retry_of"):
            for status in ("planned", "ready_for_checks"):
                with (
                    self.subTest(retry=retry_field, status=status),
                    TemporaryDirectory() as directory,
                ):
                    store = FilesystemRecordStore(state_dir=Path(directory))
                    failure = build_merge_train_batch_candidate_record(
                        candidate=self.candidate, source="test", updated_at="2026-10-03T12:00:00Z"
                    ).model_copy(update={"status": "superseded"})
                    store.write_merge_train_batch_candidate_record(failure)
                    retry = self.candidate.model_copy(
                        update={
                            "batch_id": self.candidate.batch_id + "-retry",
                            "candidate_ref": self.candidate.candidate_ref + "-retry",
                            "status": status,
                            "required_checks_status": "unknown",
                            "candidate_sha": "" if status == "planned" else "interrupted-sha",
                            retry_field: failure.record_id,
                        }
                    )
                    store.write_merge_train_batch_candidate_record(
                        build_merge_train_batch_candidate_record(
                            candidate=retry, source="test", updated_at="2026-10-03T12:01:00Z"
                        ).model_copy(update={"status": "superseded"})
                    )
                    self.provider = _RerunProvider(retry)
                    self.provider.checks_failed = False
                    self.client = GitHubMergeTrainClient(transport=self.provider)
                    before = store.list_merge_train_batch_candidate_records()
                    dry = self._fresh_plan(store, mutate=False, observe_checks=True)
                    self.assertEqual(dry["reason_code"], "candidate_retry_resumed")
                    self.assertEqual(store.list_merge_train_batch_candidate_records(), before)
                    self.snapshot = original_snapshot.model_copy(
                        update={"pull_requests": (*original_snapshot.pull_requests, third)}
                    )
                    result = self._fresh_plan(
                        store, mutate=True, observe_checks=True, conflicts=(hold,)
                    )
                    resumed = MergeTrainBatchCandidate.model_validate(result["candidate"])
                    self.assertEqual(resumed.candidate_ref, retry.candidate_ref)
                    self.assertEqual(getattr(resumed, retry_field), failure.record_id)
                    self.assertEqual(resumed.status, "planned" if status == "planned" else "passed")
                    self.assertEqual(resumed.candidate_sha, retry.candidate_sha)
                    self.assertEqual(resumed.held_out, (hold,))
                    self.reflow_client.add_pull_request_label.assert_not_called()
                    self.snapshot = original_snapshot
                    if retry_field == "batch_body_retry_of" and status == "planned":
                        # A legacy body retry is not evidence that a real failure was flaky.
                        self.provider.checks_failed = True
                        active = store.list_merge_train_batch_candidate_records(status="active")[0]
                        store.write_merge_train_batch_candidate_record(
                            active.model_copy(update={"status": "superseded"})
                        )
                        refused = self._fresh_plan(store, mutate=False, observe_checks=True)
                        self.assertEqual(refused["reason_code"], "failure_isolation")

    def test_isolation_skips_a_subset_that_already_failed(self) -> None:
        self.snapshot = self.snapshot.model_copy(
            update={
                "pull_requests": (
                    *self.snapshot.pull_requests,
                    *(
                        self.snapshot.pull_requests[1].model_copy(
                            update={
                                "number": number,
                                "head_sha": f"head-{number}",
                                "head_ref": f"pr-{number}",
                            }
                        )
                        for number in (3, 4)
                    ),
                )
            }
        )
        self.candidate = build_merge_train_batch_candidate(
            dry_run_result=build_merge_train_dry_run_result(
                policy=self.policy, snapshot=self.snapshot
            ),
            base_sha=self.snapshot.base_sha,
            policy_sha256="policy-digest",
            created_at="2026-10-03T12:00:00Z",
        ).model_copy(
            update={
                "candidate_sha": "full-failed",
                "status": "failed",
                "required_checks_status": "fail",
            }
        )
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            for index, candidate in enumerate(
                (
                    self.candidate,
                    self.candidate.model_copy(
                        update={
                            "entries": self.candidate.entries[:2],
                            "batch_id": "failed-half",
                            "candidate_sha": "half-failed",
                        }
                    ),
                )
            ):
                store.write_merge_train_batch_candidate_record(
                    build_merge_train_batch_candidate_record(
                        candidate=candidate, source="test", updated_at=f"2026-10-03T12:0{index}:00Z"
                    ).model_copy(update={"status": "superseded"})
                )
            before = store.list_merge_train_batch_candidate_records()
            dry = self._fresh_plan(store, mutate=False)
            self.assertEqual(
                len(MergeTrainBatchCandidate.model_validate(dry["candidate"]).entries), 1
            )
            self.assertEqual(store.list_merge_train_batch_candidate_records(), before)
            result = self._fresh_plan(store, mutate=True)
            candidate = MergeTrainBatchCandidate.model_validate(result["candidate"])
            self.assertEqual([entry.pull_request_number for entry in candidate.entries], [1])
            self.assertEqual({entry.pull_request_number for entry in candidate.held_out}, {2, 3, 4})

    def test_retired_retry_observes_concluded_checks_before_resuming(self) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            parent = build_merge_train_batch_candidate_record(
                candidate=self.candidate, source="test", updated_at="2026-10-03T12:00:00Z"
            ).model_copy(update={"status": "superseded"})
            store.write_merge_train_batch_candidate_record(parent)
            retry = self.candidate.model_copy(
                update={
                    "batch_id": self.candidate.batch_id + "-retry",
                    "candidate_ref": self.candidate.candidate_ref + "-retry",
                    "candidate_sha": "retry-sha",
                    "status": "ready_for_checks",
                    "required_checks_status": "unknown",
                    "check_retry_of": parent.record_id,
                }
            )
            store.write_merge_train_batch_candidate_record(
                build_merge_train_batch_candidate_record(
                    candidate=retry, source="test", updated_at="2026-10-03T12:01:00Z"
                ).model_copy(update={"status": "superseded"})
            )
            self.provider = _RerunProvider(retry)
            self.client = GitHubMergeTrainClient(transport=self.provider)
            result = self._fresh_plan(store, mutate=True, observe_checks=True)
            self.assertEqual(result["reason_code"], "failure_isolation")
            self.assertLess(
                len(MergeTrainBatchCandidate.model_validate(result["candidate"]).entries),
                len(retry.entries),
            )
            paths = [path for _, path, _ in self.provider.requests if "/check-runs?" in path]
            self.assertTrue(any("/commits/retry-sha/" in path for path in paths))

    def test_closed_retired_retry_is_not_rebuilt(self) -> None:
        for status in ("ready_for_checks", "failed"):
            with self.subTest(status=status), TemporaryDirectory() as directory:
                store = FilesystemRecordStore(state_dir=Path(directory))
                parent = build_merge_train_batch_candidate_record(
                    candidate=self.candidate, source="test", updated_at="2026-10-03T12:00:00Z"
                ).model_copy(update={"status": "superseded"})
                store.write_merge_train_batch_candidate_record(parent)
                retry = self.candidate.model_copy(
                    update={
                        "batch_id": self.candidate.batch_id + "-retry",
                        "candidate_ref": self.candidate.candidate_ref + "-retry",
                        "candidate_sha": "retry-sha",
                        "status": status,
                        "required_checks_status": "unknown",
                        "check_retry_of": parent.record_id,
                    }
                )
                store.write_merge_train_batch_candidate_record(
                    build_merge_train_batch_candidate_record(
                        candidate=retry, source="test", updated_at="2026-10-03T12:01:00Z"
                    ).model_copy(update={"status": "superseded"})
                )
                self.provider = _RerunProvider(retry)
                self.provider.checks_failed = False
                self.provider.checks_pass = False
                self.provider.created = True
                self.provider.closed = True
                self.client = GitHubMergeTrainClient(transport=self.provider)
                self.provider.body = batch_pull_request_body(client=self.client, candidate=retry)
                result = self._fresh_plan(store, mutate=True, observe_checks=True)
                self.assertEqual(result["controller_action"], "candidate_failed")
                self.reflow_client.add_pull_request_label.assert_not_called()
                self.assertTrue(all(method == "GET" for method, _, _ in self.provider.requests))

    def test_controller_closed_retry_resumes_after_queue_change_and_close_crash(self) -> None:
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted), TemporaryDirectory() as directory:
                store = FilesystemRecordStore(state_dir=Path(directory))
                parent = build_merge_train_batch_candidate_record(
                    candidate=self.candidate, source="test", updated_at="2026-10-03T12:00:00Z"
                ).model_copy(update={"status": "superseded"})
                store.write_merge_train_batch_candidate_record(parent)
                retry = self.candidate.model_copy(
                    update={
                        "batch_id": self.candidate.batch_id + "-retry",
                        "candidate_ref": self.candidate.candidate_ref + "-retry",
                        "candidate_sha": "retry-sha",
                        "status": "ready_for_checks",
                        "required_checks_status": "pending",
                        "check_retry_of": parent.record_id,
                    }
                )
                record = build_merge_train_batch_candidate_record(
                    candidate=retry, source="test", updated_at="2026-10-03T12:01:00Z"
                )
                store.write_merge_train_batch_candidate_record(record)
                self.provider = _RerunProvider(retry)
                self.provider.checks_failed = False
                self.provider.checks_pass = False
                self.provider.created = True
                self.client = GitHubMergeTrainClient(transport=self.provider)
                self.provider.body = batch_pull_request_body(client=self.client, candidate=retry)
                third = self.snapshot.pull_requests[1].model_copy(
                    update={
                        "number": 3,
                        "head_sha": "third-head",
                        "head_ref": "third-branch",
                        "created_at": "2026-10-03T12:30:00Z",
                    }
                )
                snapshot = self.snapshot.model_copy(
                    update={"pull_requests": (*self.snapshot.pull_requests, third)}
                )
                hold = MergeTrainBatchHeldOutEntry(
                    pull_request_number=3,
                    head_sha=third.head_sha,
                    conflicts_with=(1, 2),
                    conflicts_with_head_shas=tuple(
                        pr.head_sha for pr in self.snapshot.pull_requests
                    ),
                    probe_base_sha=self.snapshot.base_sha,
                )
                client = Mock(wraps=self.client)
                # Existing members can leave and rejoin while a retirement runs.
                # New arrivals alone wait for the next candidate on current main.
                removed = self.snapshot.model_copy(
                    update={"pull_requests": self.snapshot.pull_requests[:1]}
                )
                client.read_merge_train_snapshot.side_effect = (removed, snapshot)
                client.probe_batch_entry_conflicts.return_value = (hold,)
                lease = Mock(
                    record=Mock(ordinary_job_binding=None, updated_at=self.candidate.updated_at)
                )

                def close_then_interrupt(*, candidate: MergeTrainBatchCandidate) -> None:
                    self.client.close_batch_pull_request(candidate=candidate)
                    raise RuntimeError("interrupted after close")

                if interrupted:
                    client.close_batch_pull_request.side_effect = close_then_interrupt
                kwargs: dict[str, Any] = dict(
                    request=MergeTrainControllerRunOnceEnvelope(
                        repository=snapshot.repository, mutate=True
                    ),
                    policy=self.policy,
                    policy_sha256="policy-digest",
                    repository_policy=self.policy.policies[0],
                    transport=self.provider,
                    github_client=client,
                    candidate_store=store,
                    stack_collapse_store=store,
                    candidate_record=record,
                    trace_id="retire",
                    recorded_at="2026-10-03T12:02:00Z",
                    lease=lease,
                )
                if interrupted:
                    with self.assertRaisesRegex(RuntimeError, "interrupted after close"):
                        _reflow_stale_candidate_record(**kwargs)
                    client.close_batch_pull_request.side_effect = None
                    # The new PR can leave before the restart; the original
                    # retry is still active and the queue now matches it again.
                    client.read_merge_train_snapshot.side_effect = None
                    client.read_merge_train_snapshot.return_value = self.snapshot
                result, _ = _reflow_stale_candidate_record(**kwargs)
                assert result is not None
                self.assertEqual(result["reason_code"], "candidate_retry_resumed")
                candidate = MergeTrainBatchCandidate.model_validate(result["candidate"])
                self.assertEqual(candidate.candidate_ref, retry.candidate_ref)
                self.assertEqual(candidate.check_retry_of, parent.record_id)
                self.assertEqual(candidate.status, "planned")
                self.assertFalse(candidate.candidate_sha)
                self.assertEqual(candidate.held_out, () if interrupted else (hold,))
                self.assertTrue(self.provider.closed)

    def test_nonmerge_isolation_recovers_a_subset_whose_sha_now_passes(self) -> None:
        self.policy = self.policy.model_copy(
            update={
                "policies": tuple(
                    entry.model_copy(update={"merge_method": "squash"})
                    for entry in self.policy.policies
                )
            }
        )
        self.snapshot = self.snapshot.model_copy(
            update={
                "pull_requests": (
                    *self.snapshot.pull_requests,
                    *(
                        self.snapshot.pull_requests[1].model_copy(
                            update={
                                "number": number,
                                "head_sha": f"head-{number}",
                                "head_ref": f"pr-{number}",
                            }
                        )
                        for number in (3, 4)
                    ),
                )
            }
        )
        self.candidate = build_merge_train_batch_candidate(
            dry_run_result=build_merge_train_dry_run_result(
                policy=self.policy, snapshot=self.snapshot
            ),
            base_sha=self.snapshot.base_sha,
            policy_sha256="policy-digest",
            created_at="2026-10-03T12:00:00Z",
        ).model_copy(
            update={
                "candidate_sha": "full-failed",
                "status": "failed",
                "required_checks_status": "fail",
            }
        )
        half = self.candidate.model_copy(
            update={
                "entries": self.candidate.entries[:2],
                "batch_id": "failed-half",
                "candidate_sha": "half-failed",
            }
        )
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            for index, candidate in enumerate((self.candidate, half)):
                store.write_merge_train_batch_candidate_record(
                    build_merge_train_batch_candidate_record(
                        candidate=candidate, source="test", updated_at=f"2026-10-03T12:0{index}:00Z"
                    ).model_copy(update={"status": "superseded"})
                )

            def observed(*, candidate: MergeTrainBatchCandidate) -> MergeTrainBatchCandidate:
                return (
                    candidate.model_copy(
                        update={"status": "passed", "required_checks_status": "pass"}
                    )
                    if candidate.candidate_sha == half.candidate_sha
                    else candidate
                )

            with patch.object(self.client, "observe_batch_candidate_checks", side_effect=observed):
                result = self._fresh_plan(store, mutate=True, observe_checks=True)
            candidate = MergeTrainBatchCandidate.model_validate(result["candidate"])
            self.assertEqual(result["controller_action"], "observe_candidate")
            self.assertEqual(candidate.candidate_sha, half.candidate_sha)
            self.assertEqual(candidate.status, "passed")
            self.assertEqual(len(candidate.entries), 2)

    def test_reobserved_singleton_is_fenced_before_persistence(self) -> None:
        self.snapshot = self.snapshot.model_copy(
            update={"pull_requests": self.snapshot.pull_requests[:1]}
        )
        self.candidate = self.candidate.model_copy(update={"entries": self.candidate.entries[:1]})
        self.provider.checks_failed = False
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            store.write_merge_train_batch_candidate_record(
                build_merge_train_batch_candidate_record(
                    candidate=self.candidate, source="test", updated_at="2026-10-03T12:00:00Z"
                ).model_copy(update={"status": "superseded"})
            )
            before = store.list_merge_train_batch_candidate_records()
            lease = Mock(
                record=Mock(ordinary_job_binding=None, updated_at=self.candidate.updated_at)
            )
            lease.checkpoint.side_effect = MergeTrainControllerLeaseLostError("expired")
            with self.assertRaises(MergeTrainControllerLeaseLostError):
                self._fresh_plan(store, mutate=True, observe_checks=True, lease=lease)
            self.assertEqual(store.list_merge_train_batch_candidate_records(), before)

    def test_rebuilt_singleton_retains_new_conflict_holds(self) -> None:
        self.snapshot = self.snapshot.model_copy(
            update={"pull_requests": self.snapshot.pull_requests[:1]}
        )
        self.candidate = self.candidate.model_copy(update={"entries": self.candidate.entries[:1]})
        third = self.snapshot.pull_requests[0].model_copy(
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
        hold = MergeTrainBatchHeldOutEntry(
            pull_request_number=3,
            head_sha=third.head_sha,
            conflicts_with=(1,),
            probe_base_sha=self.snapshot.base_sha,
            conflicts_with_head_shas=(self.candidate.entries[0].head_sha,),
        )
        self.provider.checks_failed = False
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            store.write_merge_train_batch_candidate_record(
                build_merge_train_batch_candidate_record(
                    candidate=self.candidate, source="test", updated_at="2026-10-03T12:00:00Z"
                ).model_copy(update={"status": "superseded"})
            )
            result = self._fresh_plan(store, mutate=True, observe_checks=True, conflicts=(hold,))
            self.assertEqual(result["controller_action"], "plan_candidate")
            self.assertEqual(
                store.list_merge_train_batch_candidate_records(status="active")[
                    0
                ].candidate.held_out,
                (hold,),
            )

            # A still-failing singleton also retains the conflict while it is blocked.
            self.provider.checks_failed = True
            record = store.list_merge_train_batch_candidate_records(status="active")[0]
            store.write_merge_train_batch_candidate_record(
                record.model_copy(update={"status": "superseded"})
            )
            store.write_merge_train_batch_candidate_record(
                build_merge_train_batch_candidate_record(
                    candidate=self.candidate, source="test", updated_at="2026-10-03T12:03:00Z"
                ).model_copy(update={"status": "superseded"})
            )
            result = self._fresh_plan(store, mutate=True, observe_checks=True, conflicts=(hold,))
            self.assertEqual(result["controller_action"], "block")
            self.assertEqual(
                store.list_merge_train_batch_candidate_records(status="active")[
                    0
                ].candidate.held_out,
                (hold,),
            )
            self.assertTrue(
                any(
                    payload["pull_request_number"] == 3
                    for payload in build_feedback_payloads(response={"result": result})
                )
            )
            reflow = self._reflow(store, self.candidate, conflicts=(hold,))
            self.assertEqual(reflow["controller_action"], "block")
            active = store.list_merge_train_batch_candidate_records(status="active")
            self.assertEqual(
                [entry.record_id for entry in active],
                [reflow["merge_train_batch_candidate_record_id"]],
            )
            self.assertEqual(active[0].candidate.held_out, (hold,))

    def test_historical_failure_does_not_apply_to_changed_inputs(self) -> None:
        for changed in ("base", "head", "policy", "order"):
            with self.subTest(changed=changed), TemporaryDirectory() as directory:
                candidate = self.candidate.model_copy(
                    update={
                        "base_sha": "old-base" if changed == "base" else self.candidate.base_sha,
                        "policy_sha256": "old-policy" if changed == "policy" else "policy-digest",
                        "entries": tuple(
                            entry.model_copy(update={"head_sha": "old-head"})
                            if changed == "head"
                            else entry
                            for entry in self.candidate.entries
                        )
                        if changed != "order"
                        else tuple(
                            entry.model_copy(update={"position": position})
                            for position, entry in enumerate(reversed(self.candidate.entries), 1)
                        ),
                    }
                )
                store = FilesystemRecordStore(state_dir=Path(directory))
                store.write_merge_train_batch_candidate_record(
                    build_merge_train_batch_candidate_record(
                        candidate=candidate, source="test", updated_at="2026-10-03T12:00:00Z"
                    ).model_copy(update={"status": "superseded"})
                )
                result = self._fresh_plan(store, mutate=True)
                self.assertEqual(result["controller_action"], "plan_candidate")
                self.assertNotIn("reason_code", result)
                self.assertEqual(
                    MergeTrainBatchCandidate.model_validate(result["candidate"]).entries,
                    self.candidate.entries,
                )

    def test_historical_failure_keeps_flaky_recovery_and_red_base_protection(self) -> None:
        for case in ("flaky", "red_base", "missing_failure"):
            with self.subTest(case=case), TemporaryDirectory() as directory:
                self.provider.checks_failed = case != "flaky"
                store = FilesystemRecordStore(state_dir=Path(directory))
                store.write_merge_train_batch_candidate_record(
                    build_merge_train_batch_candidate_record(
                        candidate=self.candidate, source="test", updated_at="2026-10-03T12:00:00Z"
                    ).model_copy(update={"status": "superseded"})
                )
                result = self._fresh_plan(
                    store,
                    mutate=True,
                    base_status="fail" if case == "red_base" else "pass",
                    failure_evidence=CandidateCheckFailures(checks=())
                    if case == "missing_failure"
                    else None,
                    observe_checks=case == "flaky",
                )
                self.reflow_client.add_pull_request_label.assert_not_called()
                self.assertEqual(
                    result["reason_code"],
                    {
                        "flaky": "candidate_checks_recovered",
                        "red_base": "base_failure_exploration",
                        "missing_failure": "failed_candidate_recovery_unavailable",
                    }[case],
                )
                active = store.list_merge_train_batch_candidate_records(status="active")
                if case == "flaky":
                    self.assertTrue(active[0].candidate.check_retry_of)
                    self.assertEqual(active[0].candidate.entries, self.candidate.entries)
                elif case == "red_base":
                    self.assertEqual(active[0].candidate.status, "planned")
                    self.assertEqual(len(active[0].candidate.entries), 1)
                    self.assertFalse(active[0].candidate.check_retry_of)
                else:
                    self.assertEqual([record.candidate.status for record in active], ["failed"])
                    self.assertEqual(
                        active[0].candidate.candidate_sha, self.candidate.candidate_sha
                    )

    def test_reflow_to_an_older_failed_group_retires_the_current_lineage(self) -> None:
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            store.write_merge_train_batch_candidate_record(
                build_merge_train_batch_candidate_record(
                    candidate=self.candidate, source="test", updated_at="2026-10-03T12:00:00Z"
                ).model_copy(update={"status": "superseded"})
            )
            extra = self.candidate.entries[-1].model_copy(
                update={"pull_request_number": 3, "head_sha": "third-head", "position": 3}
            )
            current = self.candidate.model_copy(
                update={
                    "batch_id": "later-batch",
                    "candidate_ref": self.candidate.candidate_ref + "-later",
                    "candidate_sha": "later-failed-sha",
                    "entries": (*self.candidate.entries, extra),
                }
            )
            result = self._reflow(store, current)
            self.assertEqual(result["reason_code"], "failure_isolation")
            self.assertIn(
                result["details"]["retired_candidate_record_id"],
                _records_for_result(result).values(),
            )
            self.assertEqual(
                [
                    record.record_id
                    for record in store.list_merge_train_batch_candidate_records(status="active")
                ],
                [result["merge_train_batch_candidate_record_id"]],
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
            self.assertEqual(result["controller_action"], "plan_candidate")
            self.assertEqual(result["reason_code"], "failure_isolation")
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
            self.assertIn("conflicts with #1, #2", cast(str, held_out_feedback[0]["message"]))

            # The held-out evidence survives a restart without spending the budget.
            restarted = FilesystemRecordStore(state_dir=Path(directory))
            persisted = restarted.list_merge_train_batch_candidate_records(status="active")[
                0
            ].candidate
            self.assertEqual(persisted.status, "planned")
            self.assertFalse(persisted.batch_body_retry_of)
            self.assertEqual(
                [(entry.pull_request_number, entry.conflicts_with) for entry in persisted.held_out],
                [(3, (1, 2)), (2, ())],
            )

    def test_red_base_resume_preserves_conflict_holds_and_feedback(self) -> None:
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
        hold = MergeTrainBatchHeldOutEntry(
            pull_request_number=3,
            head_sha=third.head_sha,
            conflicts_with=(1, 2),
            probe_base_sha=self.snapshot.base_sha,
            conflicts_with_head_shas=tuple(pr.head_sha for pr in self.snapshot.pull_requests[:2]),
        )
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            result = self._reflow(store, self.candidate, conflicts=(hold,), base_status="fail")
            self.assertEqual(result["reason_code"], "base_failure_exploration")
            persisted = store.list_merge_train_batch_candidate_records(status="active")[0].candidate
            self.assertIn(hold, persisted.held_out)
            self.assertEqual([entry.pull_request_number for entry in persisted.entries], [1])
            messages = [
                payload["message"]
                for payload in build_feedback_payloads(response={"result": result})
                if payload["pull_request_number"] == 3
            ]
            self.assertTrue(messages)
            self.assertTrue(all("conflicts with #1, #2" in str(message) for message in messages))
            events = {
                row["pull_request_number"]: row["event"]
                for row in build_feedback_payloads(response={"result": result})
            }
            with self.subTest(base_read="fail"):
                self.assertEqual(events[3], "blocked")
                self.assertEqual(events[2], "waiting")
            with self.subTest(base_read="exception"), TemporaryDirectory() as failed_read_directory:
                failed_read = self._reflow(
                    FilesystemRecordStore(state_dir=Path(failed_read_directory)),
                    self.candidate,
                    conflicts=(hold,),
                    base_status=MergeTrainGitHubError("Required base evidence unavailable"),
                )
                self.assertEqual(failed_read["reason_code"], "base_checks_not_green")
                held_payloads = [
                    row
                    for row in build_feedback_payloads(response={"result": failed_read})
                    if row["pull_request_number"] == hold.pull_request_number
                ]
                self.assertTrue(held_payloads)
                self.assertEqual({row["event"] for row in held_payloads}, {"blocked"})
            restarted = FilesystemRecordStore(state_dir=Path(directory))
            later = self._reflow(
                restarted,
                persisted.model_copy(
                    update={
                        "candidate_sha": "failed-singleton-1",
                        "status": "failed",
                        "required_checks_status": "fail",
                    }
                ),
                base_status="fail",
            )
            self.assertEqual(later["controller_action"], "plan_candidate")
            next_candidate = MergeTrainBatchCandidate.model_validate(later["candidate"])
            self.assertEqual(
                [entry.pull_request_number for entry in next_candidate.entries], [1, 3]
            )
            self.assertNotIn(hold, next_candidate.held_out)
            self.reflow_client.probe_batch_entry_conflicts.assert_called_once()
            self.reflow_client.add_pull_request_label.assert_not_called()

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
            result, _ = _reflow_stale_candidate_record(
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

    def test_unavailable_body_evidence_cannot_spend_a_body_retry(self) -> None:
        from control_plane.merge_train_github import MergeTrainGitHubError

        self.provider.checks_failed = False
        with (
            TemporaryDirectory() as directory,
            patch(
                "control_plane.merge_train_controller_run_once.changed_closed_batch_body",
                side_effect=MergeTrainGitHubError("provider unavailable", status_code=503),
            ),
        ):
            store = FilesystemRecordStore(state_dir=Path(directory))
            self._reflow(store, self.candidate, observe_checks=True)
            result = self._reflow(store, self.candidate, observe_checks=True)
        self.assertEqual(result["reason_code"], "candidate_check_retry_used")
        self.assertFalse(
            MergeTrainBatchCandidate.model_validate(result["candidate"]).batch_body_retry_of
        )
        self.reflow_client.add_pull_request_label.assert_not_called()

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
