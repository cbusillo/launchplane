from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from control_plane.contracts.merge_train_batch import build_merge_train_batch_landing_plan_record
from control_plane.contracts.merge_train_stack_collapse import (
    MergeTrainStackCollapsePlan,
)
from control_plane.contracts.merge_train_policy import MergeTrainMergeMethod
from control_plane.contracts.merge_readiness import MergeReadinessCandidateEvidence
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.merge_admission import (
    GuardedMergeAdmission,
    MergeAdmissionDeniedError,
    MergeAdmissionEvaluation,
)
from control_plane.merge_train import MergeTrainDryRunSnapshot
from control_plane.merge_train_admission import build_merge_train_controller_status_read_model
from control_plane.merge_train_controller_run_once import (
    MergeTrainControllerRunOnceEnvelope,
    MergeTrainControllerRunOnceResult,
    _ConflictProbeOutcome,
    _lineage_change_retires_landing,
    execute_merge_train_controller_with_client,
)
from control_plane.merge_train_github import GitHubMergeTrainClient, MergeTrainGitHubError
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.merge_train_policy_fixtures import build_test_merge_train_policy_record
from tests.http_app_test_support import _post_merge_train_controller_run_once
from tests.support.auth import _StubVerifier
from tests.support.merge_train import _merge_train_service_identity, _merge_train_service_policy
from tests.test_merge_admission_live import _queued_pull_request
from tests.test_merge_admission_records import _StaticEvaluator, _guard_records
from tests.test_merge_train_github import _landing_plan
from tests.test_merge_train_controller import _stack_collapse_record
from tests.test_merge_readiness import (
    BASE_SHA,
    HEAD_SHA,
    OTHER_SHA,
    REPOSITORY,
    TREE_SHA,
    _candidate,
    _evaluate,
    _target,
)


class _RecoveryTransport:
    def __init__(self) -> None:
        self.base_sha = BASE_SHA
        self.head_sha = HEAD_SHA
        self.pr_state = "open"
        self.unavailable = False
        self.merge_sha = ""
        self.contained = True
        self.calls: list[str] = []

    def request(self, *, method: str, path: str, body: dict[str, object] | None = None) -> object:
        if method != "GET" or body is not None:
            raise AssertionError("Policy recovery must not mutate GitHub")
        self.calls.append(path)
        if self.unavailable:
            raise MergeTrainGitHubError("Provider read unavailable", status_code=503)
        if path.endswith("/branches/main"):
            return {"commit": {"sha": self.base_sha, "commit": {"tree": {"sha": OTHER_SHA}}}}
        if path.endswith(f"/git/commits/{HEAD_SHA}"):
            return {"sha": HEAD_SHA, "tree": {"sha": TREE_SHA}, "parents": []}
        if path.endswith("/pulls/2083"):
            return {
                "state": self.pr_state,
                "merged": bool(self.merge_sha),
                "merge_commit_sha": self.merge_sha,
                "head": {"sha": self.head_sha},
                "base": {"ref": "main", "sha": self.base_sha},
            }
        if "/compare/" in path:
            return {"status": "ahead" if self.contained else "diverged"}
        raise AssertionError(f"Unexpected recovery read: {path}")


class _NoAdmissionEvaluator:
    def evaluate(self, **_: object) -> MergeAdmissionEvaluation:
        raise AssertionError("Retirement must not admit a merge under the old policy")


class _StackRecoveryTransport(_RecoveryTransport):
    def __init__(self) -> None:
        super().__init__()
        self.children: dict[int, dict[str, object]] = {
            2: {"head": {"sha": "2" * 40}, "state": "closed", "labels": []},
            3: {"head": {"sha": "3" * 40}, "state": "open", "labels": []},
        }
        self.comments: dict[int, list[dict[str, str]]] = {2: [], 3: []}
        self.effects: list[tuple[str, str]] = []
        self.child_read_failure = False
        self.child_error: MergeTrainGitHubError | None = None
        self.child_contained = True
        self.child_merge_contained = True
        self.interrupt_after_close = False

    def request(self, *, method: str, path: str, body: dict[str, object] | None = None) -> object:
        for number, child in self.children.items():
            if path.endswith(f"/pulls/{number}"):
                if method == "GET":
                    if self.child_error is not None:
                        raise self.child_error
                    if self.child_read_failure:
                        raise MergeTrainGitHubError("Child not found", status_code=404)
                    return child
                if method == "PATCH" and body == {"state": "closed"}:
                    self.effects.append((method, path))
                    child["state"] = "closed"
                    if self.interrupt_after_close:
                        self.interrupt_after_close = False
                        raise OSError("Interrupted after provider close")
                    return child
            if path.endswith(f"/issues/{number}/comments"):
                if method == "GET":
                    return self.comments[number]
                if method == "POST" and body is not None:
                    self.effects.append((method, path))
                    comment_body = body["body"]
                    assert isinstance(comment_body, str)
                    comment = {
                        "body": comment_body,
                        "html_url": f"https://example.test/{number}",
                    }
                    self.comments[number].append(comment)
                    return comment
            if path.endswith(f"/issues/{number}/labels") and method == "POST" and body is not None:
                self.effects.append((method, path))
                labels = body["labels"]
                assert isinstance(labels, list)
                child["labels"] = [{"name": label} for label in labels]
                return child["labels"]
        if "/compare/" in path and any(f"/{n * 40}..." in path for n in ("2", "3")):
            contained = self.child_contained and (
                self.child_merge_contained or not path.endswith(f"...{OTHER_SHA}")
            )
            return {"status": "ahead" if contained else "diverged"}
        return super().request(method=method, path=path, body=body)


class MergeTrainPolicyRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = FilesystemRecordStore(state_dir=Path(temporary.name))
        self.candidate, self.landing, controller, structural = _guard_records()
        self.store.write_merge_train_batch_candidate_record(self.candidate)
        self.store.write_merge_train_batch_landing_plan_record(self.landing)
        self.store.write_merge_train_controller_state_record(controller)
        readiness = _evaluate(
            target=_target(queue_position=1),
            candidate_evidence=MergeReadinessCandidateEvidence.model_validate(
                {
                    **_candidate().model_dump(),
                    "queue_position": 1,
                    "record_id": self.candidate.record_id,
                }
            ),
        )
        self.guard = GuardedMergeAdmission(
            record_store=self.store,
            evaluator=_StaticEvaluator(
                MergeAdmissionEvaluation(readiness=readiness, structural_result=structural)
            ),
            candidate_record=self.candidate,
            landing_plan_record=self.landing,
            controller_state=controller,
            trace_id="original-attempt",
            admission_time_provider=lambda: "2026-08-11T03:01:00Z",
        )
        self.admission = self.guard.admit(
            entry=self.landing.landing_plan.entries[0],
            observed_base_sha=BASE_SHA,
            observed_base_tree_sha=OTHER_SHA,
            observed_head_sha=HEAD_SHA,
            observed_head_tree_sha=TREE_SHA,
        )
        self.store.write_merge_train_controller_state_record(
            controller.model_copy(
                update={
                    "status": "reconcile_required",
                    "lease_owner": "",
                    "lease_acquired_at": "",
                    "lease_expires_at": "",
                    "heartbeat_at": "",
                    "active_phase": "merge_pull_request",
                    "active_record_id": self.landing.record_id,
                    "reconciliation_status": "required",
                    "reconciliation_detail": "operator_required:github_request_rejected",
                }
            )
        )
        self.policy = build_test_merge_train_policy_record(repository=REPOSITORY)
        self.transport = _RecoveryTransport()
        self.client = GitHubMergeTrainClient(transport=self.transport)
        self.attempt = 0

    def _run(self, *, mutate: bool = True) -> MergeTrainControllerRunOnceResult:
        self.attempt += 1
        return execute_merge_train_controller_with_client(
            request=MergeTrainControllerRunOnceEnvelope(repository=REPOSITORY, mutate=mutate),
            policy=self.policy.policy,
            policy_sha256=self.policy.policy_sha256,
            repository_policy=self.policy.policy.policies[0],
            github_client=self.client,
            trace_id=f"policy-recovery-{self.attempt}",
            recorded_at="2026-08-11T03:03:00Z",
            candidate_store=self.store,
            landing_store=self.store,
            stack_collapse_store=self.store,
            controller_state_store=self.store,
            admission_store=self.store,
            admission_evaluator=_NoAdmissionEvaluator(),
        )

    def _record_failure(self, status: int) -> None:
        self.guard.record_provider_failure(
            admission=self.admission,
            error=MergeTrainGitHubError("Provider refused merge", status_code=status),
            observed_at="2026-08-11T03:02:00Z",
        )

    def _record_completed_landing(self) -> None:
        plan = self.landing.landing_plan
        completed = build_merge_train_batch_landing_plan_record(
            landing_plan=plan.model_copy(
                update={
                    "entries": tuple(
                        entry.model_copy(update={"status": "merged", "merge_commit_sha": OTHER_SHA})
                        for entry in plan.entries
                    )
                }
            ),
            source="test:completed-landing",
            updated_at=self.landing.updated_at,
        )
        self.store.write_merge_train_batch_landing_plan_record(completed)
        controller = self.store.list_merge_train_controller_state_records()[0]
        self.store.write_merge_train_controller_state_record(
            controller.model_copy(update={"active_phase": "landing_entry_merged"})
        )
        self.transport.pr_state = "closed"
        self.transport.merge_sha = OTHER_SHA

    async def test_landing_entry_merged_resumes_after_policy_change_without_provider_writes(
        self,
    ) -> None:
        self._record_completed_landing()
        self.store.write_merge_train_policy_record(self.policy)
        original_landings = self.store.list_merge_train_batch_landing_plan_records()
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_merge_train_service_identity()),
            authz_policy=_merge_train_service_policy(),
            record_store_factory=lambda: self.store,
        )
        payload = {"repository": REPOSITORY, "base_branch": "main", "mutate": False}
        with (
            patch("control_plane.http_app.resolve_merge_train_github_token", return_value="token"),
            patch(
                "control_plane.merge_train_controller_run_once.GitHubMergeTrainClient",
                return_value=self.client,
            ),
        ):
            dry_run = await _post_merge_train_controller_run_once(app, payload)
            self.assertEqual(dry_run.status_code, 202, dry_run.text)
            self.assertEqual(dry_run.json()["result"]["controller_action"], "resume_reconciliation")
            self.assertEqual(self.transport.calls, [])
            resumed = await _post_merge_train_controller_run_once(app, {**payload, "mutate": True})

        self.assertEqual(resumed.status_code, 202, resumed.text)
        result = resumed.json()["result"]
        self.assertEqual(result["reason_code"], "completed_landing_policy_changed")
        self.assertEqual(result["candidate_ref_cleanup_status"], "retained")
        self.assertEqual(result["landing_plan"]["entries"][0]["merge_commit_sha"], OTHER_SHA)
        self.assertEqual(
            self.store.list_merge_train_batch_landing_plan_records(), original_landings
        )
        self.assertEqual(self.store.list_merge_admission_records(), (self.admission,))
        state = self.store.list_merge_train_controller_state_records()[0]
        self.assertEqual((state.status, state.reconciliation_status), ("idle", "clean"))
        snapshot = MergeTrainDryRunSnapshot(
            repository=REPOSITORY, base_branch="main", base_sha=OTHER_SHA, pull_requests=()
        )
        with patch.object(self.client, "read_merge_train_snapshot", return_value=snapshot):
            next_read = self._run(mutate=False)
        self.assertEqual(next_read.accepted_result["controller_action"], "idle")

    def test_completed_old_policy_landing_keeps_fence_when_live_merge_disagrees(self) -> None:
        self._record_completed_landing()
        for field, value in (("merge_sha", BASE_SHA), ("contained", False), ("unavailable", True)):
            with self.subTest(field=field):
                previous = getattr(self.transport, field)
                setattr(self.transport, field, value)
                with self.assertRaises(MergeTrainGitHubError):
                    self._run()
                setattr(self.transport, field, previous)
                state = self.store.list_merge_train_controller_state_records()[0]
                self.assertEqual(state.status, "reconcile_required")
                self.assertEqual(state.active_phase, "landing_entry_merged")

    def _record_unfinished_stack(self) -> _StackRecoveryTransport:
        self._record_completed_landing()
        collapse = _stack_collapse_record(status="waiting_for_root_checks")
        plan = self.landing.landing_plan
        stack_plan = MergeTrainStackCollapsePlan.model_validate(
            {
                **collapse.plan.model_dump(),
                "repository": REPOSITORY,
                "policy_key": plan.policy_key,
                "policy_sha256": plan.policy_sha256,
                "root_pull_request_number": plan.entries[0].pull_request_number,
                "root_initial_head_sha": BASE_SHA,
                "entries": [
                    {
                        "pull_request_number": 2083,
                        "position": 1,
                        "head_sha": BASE_SHA,
                        "head_ref": "feature/root",
                        "base_ref": "main",
                    },
                    {
                        "pull_request_number": 2,
                        "position": 2,
                        "head_sha": "a" * 40,
                        "head_ref": "feature/child",
                        "base_ref": "feature/root",
                    },
                    {
                        "pull_request_number": 3,
                        "position": 3,
                        "head_sha": "3" * 40,
                        "head_ref": "feature/leaf",
                        "base_ref": "feature/child",
                    },
                ],
                "mutations": [
                    {
                        "child_pull_request_number": 3,
                        "parent_pull_request_number": 2,
                        "child_head_sha": "3" * 40,
                        "expected_parent_head_sha": "a" * 40,
                        "parent_head_ref": "feature/child",
                        "status": "mutated",
                        "merge_commit_sha": "2" * 40,
                    },
                    {
                        "child_pull_request_number": 2,
                        "parent_pull_request_number": 2083,
                        "child_head_sha": "2" * 40,
                        "expected_parent_head_sha": BASE_SHA,
                        "parent_head_ref": "feature/root",
                        "status": "mutated",
                        "merge_commit_sha": HEAD_SHA,
                    },
                ],
                "child_dispositions": [],
            }
        )
        collapse = collapse.model_copy(update={"plan": stack_plan, "status": "superseded"})
        self.store.write_merge_train_stack_collapse_plan_record(collapse)
        # CM shape: a retained planned fence and two completed successors; the
        # unfinished collapse has been retired after its root landed.
        completed = self.store.list_merge_train_batch_landing_plan_records(status="active")[0]
        self.store.write_merge_train_batch_landing_plan_record(
            completed.model_copy(update={"record_id": "second-completed-successor"})
        )
        transport = _StackRecoveryTransport()
        transport.pr_state = "closed"
        transport.merge_sha = OTHER_SHA
        self.transport = transport
        self.client = GitHubMergeTrainClient(transport=transport)
        return transport

    async def test_completed_old_policy_stack_recovers_with_current_label_and_no_second_merge(
        self,
    ) -> None:
        transport = self._record_unfinished_stack()
        original_landings = self.store.list_merge_train_batch_landing_plan_records()
        self.store.write_merge_train_policy_record(self.policy)
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_merge_train_service_identity()),
            authz_policy=_merge_train_service_policy(),
            record_store_factory=lambda: self.store,
        )
        payload = {"repository": REPOSITORY, "base_branch": "main", "mutate": False}
        with (
            patch("control_plane.http_app.resolve_merge_train_github_token", return_value="token"),
            patch(
                "control_plane.merge_train_controller_run_once.GitHubMergeTrainClient",
                return_value=self.client,
            ),
        ):
            dry_run = await _post_merge_train_controller_run_once(app, payload)
            self.assertEqual(dry_run.status_code, 202, dry_run.text)
            self.assertEqual(dry_run.json()["result"]["controller_action"], "resume_reconciliation")
            self.assertEqual(transport.effects, [])
            resumed = await _post_merge_train_controller_run_once(app, {**payload, "mutate": True})
        self.assertEqual(resumed.status_code, 202, resumed.text)
        result = resumed.json()["result"]
        self.assertEqual(result["reason_code"], "completed_landing_policy_changed")
        self.assertEqual(result["stack_collapse_plan"]["status"], "ready_for_train")
        self.assertEqual(
            self.store.list_merge_train_batch_landing_plan_records(), original_landings
        )
        self.assertEqual(self.store.list_merge_admission_records(), (self.admission,))
        label = self.policy.policy.policies[0].stack_child_disposition_label
        for child in transport.children.values():
            self.assertEqual(child["state"], "closed")
            self.assertEqual(child["labels"], [{"name": label}])
        self.assertEqual(len(transport.effects), 5)  # Two annotations each; only one needs closing.
        self.assertEqual(self.store.list_merge_train_controller_state_records()[0].status, "idle")

    def test_old_policy_stack_retry_observes_provider_effects_after_interrupted_close(self) -> None:
        transport = self._record_unfinished_stack()
        transport.interrupt_after_close = True
        with self.assertRaisesRegex(OSError, "Interrupted"):
            self._run()
        self.assertEqual(
            self.store.list_merge_train_controller_state_records()[0].status, "reconcile_required"
        )
        effects = list(transport.effects)
        # The first child completed before the crash. Its subsequent new work
        # must neither block the remaining disposition nor be closed by history.
        transport.children[2].update(state="open", head={"sha": "9" * 40})
        resumed = self._run().accepted_result
        stack_plan = resumed["stack_collapse_plan"]
        assert isinstance(stack_plan, dict)
        self.assertEqual(stack_plan["status"], "ready_for_train")
        self.assertEqual(transport.effects, effects)
        self.assertEqual(transport.children[2]["state"], "open")
        snapshot = MergeTrainDryRunSnapshot(
            repository=REPOSITORY, base_branch="main", base_sha=OTHER_SHA, pull_requests=()
        )
        with patch.object(self.client, "read_merge_train_snapshot", return_value=snapshot):
            self.assertEqual(self._run().accepted_result["controller_action"], "idle")
        self.assertEqual(transport.effects, effects)

    def test_old_policy_stack_incomplete_child_evidence_keeps_fence(self) -> None:
        transport = self._record_unfinished_stack()
        for problem, reason in (
            ("missing", "completed_landing_stack_child_evidence_unavailable"),
            ("containment", "completed_landing_stack_child_not_contained"),
            ("unchanged_containment", "completed_landing_stack_child_not_contained"),
        ):
            with self.subTest(problem=problem):
                transport.children[3]["head"] = {
                    "sha": "3" * 40 if problem == "unchanged_containment" else "9" * 40
                }
                transport.child_read_failure = problem == "missing"
                transport.child_contained = problem == "missing"
                result = self._run().accepted_result
                self.assertEqual(result["reason_code"], reason)
                details = result["details"]
                assert isinstance(details, dict)
                self.assertEqual(details["stack_collapse_plan_record_ids"], ["stack-record"])
                self.assertEqual(transport.effects, [])
                self.assertEqual(
                    self.store.list_merge_train_controller_state_records()[0].status,
                    "reconcile_required",
                )

    async def test_completed_stack_preserves_moved_unfinished_child_and_clears_fence(self) -> None:
        transport = self._record_unfinished_stack()
        transport.children[3]["head"] = {"sha": "9" * 40}
        original_landings = self.store.list_merge_train_batch_landing_plan_records()
        original_stack = self.store.list_merge_train_stack_collapse_plan_records()[0]
        self.store.write_merge_train_policy_record(self.policy)
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_merge_train_service_identity()),
            authz_policy=_merge_train_service_policy(),
            record_store_factory=lambda: self.store,
        )
        payload = {"repository": REPOSITORY, "base_branch": "main", "mutate": False}
        with (
            patch("control_plane.http_app.resolve_merge_train_github_token", return_value="token"),
            patch(
                "control_plane.merge_train_controller_run_once.GitHubMergeTrainClient",
                return_value=self.client,
            ),
        ):
            dry_run = await _post_merge_train_controller_run_once(app, payload)
            self.assertEqual(dry_run.status_code, 202, dry_run.text)
            self.assertEqual(dry_run.json()["result"]["controller_action"], "resume_reconciliation")
            self.assertEqual(transport.effects, [])
            self.assertEqual(
                self.store.list_merge_train_stack_collapse_plan_records(), (original_stack,)
            )
            response = await _post_merge_train_controller_run_once(app, {**payload, "mutate": True})
        self.assertEqual(response.status_code, 202, response.text)
        result = response.json()["result"]
        self.assertEqual(result["mode"], "land")
        stack_plan = result["stack_collapse_plan"]
        assert isinstance(stack_plan, dict)
        self.assertEqual(stack_plan["status"], "ready_for_train")
        dispositions = stack_plan["child_dispositions"]
        self.assertEqual(dispositions[1]["status"], "preserved")
        self.assertEqual(dispositions[1]["expected_head_sha"], "3" * 40)
        self.assertEqual(dispositions[1]["preserved_head_sha"], "9" * 40)
        self.assertEqual(transport.children[3]["state"], "open")
        self.assertEqual(transport.children[3]["labels"], [])
        self.assertEqual(transport.comments[3], [])
        self.assertTrue(all("/2/" in path for _, path in transport.effects))
        self.assertEqual(
            self.store.list_merge_train_batch_landing_plan_records(), original_landings
        )
        self.assertIn(original_stack, self.store.list_merge_train_stack_collapse_plan_records())
        self.assertEqual(self.store.list_merge_admission_records(), (self.admission,))
        state = self.store.list_merge_train_controller_state_records()[0]
        self.assertEqual((state.status, state.reconciliation_status), ("idle", "clean"))
        unrelated = _queued_pull_request(
            number=50, head_sha="5" * 40, created_at="2026-08-11T03:04:00Z"
        )
        snapshot = MergeTrainDryRunSnapshot(
            repository=REPOSITORY,
            base_branch="main",
            base_sha=OTHER_SHA,
            pull_requests=(unrelated,),
        )
        effects = list(transport.effects)
        with patch.object(self.client, "read_merge_train_snapshot", return_value=snapshot):
            self.assertEqual(
                self._run(mutate=False).accepted_result["controller_action"], "plan_candidate"
            )
        self.assertEqual(transport.effects, effects)

    def test_moved_child_preservation_is_checkpointed_before_other_provider_effects(self) -> None:
        transport = self._record_unfinished_stack()
        transport.children[2].update(state="open", head={"sha": "9" * 40})
        transport.interrupt_after_close = True
        with self.assertRaisesRegex(OSError, "Interrupted"):
            self._run()
        effects = list(transport.effects)
        records = self.store.list_merge_train_stack_collapse_plan_records()
        self.assertTrue(
            any(record.plan.child_dispositions[0].status == "preserved" for record in records)
        )
        # Reopened work after a successful close but before its checkpoint must
        # also survive. Both changed PRs now finish as historical preservation.
        transport.children[3].update(state="open", head={"sha": "8" * 40})
        result = self._run().accepted_result
        stack_plan = result["stack_collapse_plan"]
        assert isinstance(stack_plan, dict)
        self.assertEqual(stack_plan["status"], "ready_for_train")
        self.assertTrue(
            all(child["status"] == "preserved" for child in stack_plan["child_dispositions"])
        )
        self.assertEqual(transport.effects, effects)
        self.assertTrue(all(child["state"] == "open" for child in transport.children.values()))
        snapshot = MergeTrainDryRunSnapshot(
            repository=REPOSITORY, base_branch="main", base_sha=OTHER_SHA, pull_requests=()
        )
        with patch.object(self.client, "read_merge_train_snapshot", return_value=snapshot):
            self.assertEqual(self._run().accepted_result["controller_action"], "idle")
        self.assertEqual(transport.effects, effects)

    def test_missing_moved_child_head_preserves_fence_and_history(self) -> None:
        transport = self._record_unfinished_stack()
        transport.children[3]["head"] = {}
        original_stack = self.store.list_merge_train_stack_collapse_plan_records()
        with self.assertRaises(MergeTrainGitHubError):
            self._run()
        self.assertEqual(transport.effects, [])
        self.assertEqual(self.store.list_merge_train_stack_collapse_plan_records(), original_stack)
        self.assertEqual(
            self.store.list_merge_train_controller_state_records()[0].status, "reconcile_required"
        )

    def test_old_policy_stack_requires_current_disposition_policy(self) -> None:
        transport = self._record_unfinished_stack()
        current = self.policy.policy
        self.policy = self.policy.model_copy(
            update={
                "policy": current.model_copy(
                    update={
                        "policies": (
                            current.policies[0].model_copy(
                                update={"stack_child_disposition_label": ""}
                            ),
                        )
                    }
                )
            }
        )
        result = self._run().accepted_result
        self.assertEqual(
            result["reason_code"], "completed_landing_stack_disposition_not_configured"
        )
        self.assertEqual(transport.effects, [])

    def _assert_rewritten_root_recovery(self, method: MergeTrainMergeMethod) -> None:
        transport = self._record_unfinished_stack()
        transport.child_merge_contained = False
        for record in self.store.list_merge_train_batch_landing_plan_records():
            plan = record.landing_plan
            updated_plan = type(plan).model_validate(
                {
                    **plan.model_dump(),
                    "entries": [
                        entry.model_copy(update={"merge_method": method}) for entry in plan.entries
                    ],
                    "landing_plan_sha256": "",
                }
            )
            self.store.write_merge_train_batch_landing_plan_record(
                record.model_copy(update={"landing_plan": updated_plan})
            )
        result = self._run().accepted_result
        if method == "merge":
            self.assertEqual(result["reason_code"], "completed_landing_stack_child_not_contained")
            self.assertEqual(transport.effects, [])
        else:
            self.assertEqual(result["reason_code"], "completed_landing_policy_changed")
            self.assertEqual(
                self.store.list_merge_train_controller_state_records()[0].status, "idle"
            )
            self.assertEqual(transport.children[3]["state"], "closed")

    def test_old_policy_squashed_root_recovers_from_exact_merged_head(self) -> None:
        self._assert_rewritten_root_recovery("squash")

    def test_old_policy_rebased_root_recovers_from_exact_merged_head(self) -> None:
        self._assert_rewritten_root_recovery("rebase")

    def test_old_policy_plain_merge_still_requires_child_in_merge_commit(self) -> None:
        self._assert_rewritten_root_recovery("merge")

    def test_old_policy_child_read_keeps_transient_error_retry_evidence(self) -> None:
        transport = self._record_unfinished_stack()
        for error, expected_detail in (
            (
                MergeTrainGitHubError("Unavailable", status_code=503),
                "retryable:github_request_failed",
            ),
            (
                MergeTrainGitHubError(
                    "Quota limited",
                    status_code=429,
                    rate_limited=True,
                    rate_limit_reset=123,
                    retry_after_seconds=60,
                ),
                "retryable:github_rate_limited; reset_at:123; retry_after_seconds:60",
            ),
        ):
            with self.subTest(status=error.status_code):
                transport.child_error = error
                with self.assertRaises(MergeTrainGitHubError):
                    self._run()
                state = self.store.list_merge_train_controller_state_records()[0]
                self.assertEqual(state.reconciliation_detail, expected_detail)
                self.assertEqual(state.status, "reconcile_required")
                self.assertEqual(transport.effects, [])

    def test_old_policy_stack_missing_record_keeps_record_linked_fence(self) -> None:
        self._record_completed_landing()
        controller = self.store.list_merge_train_controller_state_records()[0]
        self.store.write_merge_train_controller_state_record(
            controller.model_copy(
                update={
                    "step_payload": {
                        **controller.step_payload,
                        "stack_collapse_plan_record_id": "missing-stack",
                    }
                }
            )
        )
        result = self._run().accepted_result
        self.assertEqual(result["reason_code"], "completed_landing_stack_reconciliation_required")
        self.assertEqual(
            result["details"],
            {
                "stack_collapse_plan_record_ids": [],
                "expected_stack_collapse_plan_record_id": "missing-stack",
            },
        )
        self.assertEqual(
            self.store.list_merge_train_controller_state_records()[0].status, "reconcile_required"
        )

    def test_rejected_old_policy_plan_retires_and_requires_a_fresh_candidate(self) -> None:
        self._record_failure(405)
        old_outcome = self.store.list_merge_landing_outcome_records()[0]
        result = self._run()

        self.assertEqual(result.accepted_result["controller_action"], "retire_stale_landing")
        self.assertEqual(self.store.list_merge_landing_outcome_records(), (old_outcome,))
        self.assertEqual(self.store.list_merge_admission_records(), (self.admission,))
        self.assertEqual(self.store.list_merge_train_controller_state_records()[0].status, "idle")
        self.assertEqual(
            self.store.list_merge_train_batch_candidate_records()[0].status, "superseded"
        )
        self.assertIn(self.landing, self.store.list_merge_train_batch_landing_plan_records())

        snapshot = MergeTrainDryRunSnapshot(
            repository=REPOSITORY,
            base_branch="main",
            base_sha=BASE_SHA,
            pull_requests=(
                _queued_pull_request(
                    number=2083, head_sha=HEAD_SHA, created_at="2026-08-11T01:00:00Z"
                ),
            ),
        )
        with patch.object(self.client, "read_merge_train_snapshot", return_value=snapshot):
            fresh = self._run()
            next_action = self._run(mutate=False)
        self.assertEqual(fresh.accepted_result["controller_action"], "plan_candidate")
        self.assertEqual(next_action.accepted_result["controller_action"], "build_candidate")
        active = self.store.list_merge_train_batch_candidate_records(status="active")
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].candidate.policy_sha256, self.policy.policy_sha256)
        self.assertEqual(active[0].candidate.candidate_sha, "")

    def test_ambiguous_attempt_gets_append_only_no_effect_evidence(self) -> None:
        self._record_failure(500)
        self._run()
        outcomes = self.store.list_merge_landing_outcome_records()
        self.assertEqual(
            [outcome.status for outcome in outcomes], ["rejected", "reconcile_required"]
        )
        self.assertEqual(outcomes[0].reason, "reconciliation_confirmed_no_effect")
        self.assertEqual(outcomes[0].prior_outcome_id, outcomes[1].outcome_id)

    def test_confirmed_rejection_allows_unrelated_base_movement(self) -> None:
        self._record_failure(405)
        original_outcome = self.store.list_merge_landing_outcome_records()[0]
        self.transport.base_sha = "9" * 40
        result = self._run()
        self.assertEqual(result.accepted_result["controller_action"], "retire_stale_landing")
        self.assertEqual(self.store.list_merge_train_controller_state_records()[0].status, "idle")
        self.assertEqual(self.store.list_merge_landing_outcome_records(), (original_outcome,))

    def test_old_stale_record_cannot_suppress_same_sha_candidate_under_new_policy(self) -> None:
        self._record_failure(405)
        self._run()
        fresh_candidate, _, _, _ = _guard_records(policy_sha256=self.policy.policy_sha256)
        fresh_candidate = fresh_candidate.model_copy(update={"record_id": "new-policy-candidate"})
        self.store.write_merge_train_batch_candidate_record(fresh_candidate)
        snapshot = MergeTrainDryRunSnapshot(
            repository=REPOSITORY,
            base_branch="main",
            base_sha=BASE_SHA,
            pull_requests=(
                _queued_pull_request(
                    number=2083, head_sha=HEAD_SHA, created_at="2026-08-11T01:00:00Z"
                ),
            ),
        )
        with patch.object(self.client, "read_merge_train_snapshot", return_value=snapshot):
            result = self._run(mutate=False)
        self.assertEqual(result.accepted_result["controller_action"], "plan_landing")

    def test_dry_run_inspects_an_idle_stale_plan_without_writing_records(self) -> None:
        self._record_failure(500)
        controller = self.store.list_merge_train_controller_state_records()[0]
        controller = controller.model_copy(
            update={
                "status": "idle",
                "active_action": "",
                "active_phase": "",
                "active_record_id": "",
                "active_pull_request_number": None,
                "step_payload": {},
                "reconciliation_status": "clean",
                "reconciliation_detail": "",
            }
        )
        self.store.write_merge_train_controller_state_record(controller)
        before = self.store.list_merge_landing_outcome_records()
        result = self._run(mutate=False)
        self.assertEqual(result.accepted_result["controller_action"], "retire_stale_landing")
        self.assertEqual(self.store.list_merge_landing_outcome_records(), before)
        self.assertEqual(self.store.list_merge_train_controller_state_records(), (controller,))
        self.assertEqual(self.store.list_merge_train_batch_landing_plan_records(), (self.landing,))

    def test_partial_landing_cannot_be_declared_unlanded(self) -> None:
        plan = _landing_plan()
        partial = plan.model_copy(
            update={
                "entries": (
                    plan.entries[0].model_copy(update={"status": "merged"}),
                    plan.entries[1],
                )
            }
        )
        with self.assertRaisesRegex(MergeTrainGitHubError, "no completed entries"):
            self.client.verify_unlanded_batch(landing_plan=partial)
        self.assertEqual(self.transport.calls, [])

    def test_changed_or_unreadable_provider_state_preserves_the_recovery_fence(self) -> None:
        self._record_failure(500)
        checkpoint = self.store.list_merge_train_controller_state_records()[0]
        for field, value in (
            ("base_sha", "9" * 40),
            ("head_sha", "9" * 40),
            ("pr_state", "closed"),
            ("unavailable", True),
        ):
            with self.subTest(field=field):
                previous = getattr(self.transport, field)
                setattr(self.transport, field, value)
                with self.assertRaises(MergeTrainGitHubError):
                    self._run()
                setattr(self.transport, field, previous)
                self.assertEqual(
                    self.store.list_merge_train_batch_landing_plan_records(), (self.landing,)
                )
                self.assertEqual(
                    self.store.list_merge_train_batch_candidate_records()[0].status, "active"
                )
                self.assertEqual(
                    self.store.list_merge_train_controller_state_records()[0].status,
                    "reconcile_required",
                )
                current = self.store.list_merge_train_controller_state_records()[0]
                self.assertEqual(current.active_phase, checkpoint.active_phase)
                self.assertEqual(
                    current.active_pull_request_number, checkpoint.active_pull_request_number
                )
                self.assertEqual(current.step_payload, checkpoint.step_payload)
                self.assertEqual(
                    self.store.list_merge_landing_outcome_records()[0].status, "reconcile_required"
                )

    def test_interruption_after_retirement_resumes_without_another_provider_effect(self) -> None:
        self._record_failure(405)
        with patch.object(
            self.store,
            "write_merge_train_batch_candidate_record",
            side_effect=OSError("interrupted"),
        ):
            with self.assertRaises(OSError):
                self._run()
        status = build_merge_train_controller_status_read_model(
            store=self.store,
            repository=REPOSITORY,
            base_branch="main",
            generated_at="2026-08-11T03:05:00Z",
            current_policy_key=self.policy.policy.policies[0].policy_key,
            current_policy_sha256=self.policy.policy_sha256,
        )
        (diagnostic,) = status.reconciliation_diagnostics
        self.assertEqual(diagnostic.binding_detail, "plan_binding_changed")
        provider_reads = len(self.transport.calls)
        self._run()
        self.assertEqual(len(self.transport.calls), provider_reads)
        self.assertEqual(self.store.list_merge_train_controller_state_records()[0].status, "idle")
        self.assertEqual(
            self.store.list_merge_train_batch_candidate_records()[0].status, "superseded"
        )


class _LineageChangedEvaluator:
    def evaluate(self, **_: object) -> MergeAdmissionEvaluation:
        raise MergeAdmissionDeniedError(
            "Live merge queue or base identity changed from the landing-plan lineage.",
            reason_code="landing_lineage_changed",
        )


class MergeTrainLineageChangeRecoveryTests(unittest.TestCase):
    """An older PR labelled after planning must not wedge the train (#2843)."""

    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = FilesystemRecordStore(state_dir=Path(temporary.name))
        self.policy = build_test_merge_train_policy_record(repository=REPOSITORY)
        self.candidate, self.landing, controller, _ = _guard_records(
            policy_sha256=self.policy.policy_sha256
        )
        self.store.write_merge_train_batch_candidate_record(self.candidate)
        self.store.write_merge_train_batch_landing_plan_record(self.landing)
        self.store.write_merge_train_controller_state_record(
            controller.model_copy(
                update={
                    "status": "idle",
                    "lease_owner": "",
                    "lease_acquired_at": "",
                    "lease_expires_at": "",
                    "heartbeat_at": "",
                    "active_action": "",
                    "active_phase": "",
                    "active_record_id": "",
                    "active_pull_request_number": None,
                    "step_payload": {},
                    "reconciliation_status": "clean",
                    "reconciliation_detail": "",
                }
            )
        )
        self.transport = _RecoveryTransport()
        self.client = GitHubMergeTrainClient(transport=self.transport)
        self.attempt = 0

    def _run(self, *, mutate: bool = True) -> MergeTrainControllerRunOnceResult:
        self.attempt += 1
        return execute_merge_train_controller_with_client(
            request=MergeTrainControllerRunOnceEnvelope(repository=REPOSITORY, mutate=mutate),
            policy=self.policy.policy,
            policy_sha256=self.policy.policy_sha256,
            repository_policy=self.policy.policy.policies[0],
            github_client=self.client,
            trace_id=f"lineage-recovery-{self.attempt}",
            recorded_at="2026-08-11T03:03:00Z",
            candidate_store=self.store,
            landing_store=self.store,
            stack_collapse_store=self.store,
            controller_state_store=self.store,
            admission_store=self.store,
            admission_evaluator=_LineageChangedEvaluator(),
        )

    def test_queue_change_retires_the_unlanded_plan_and_replans(self) -> None:
        result = self._run()

        self.assertEqual(result.accepted_result["controller_action"], "retire_stale_landing")
        self.assertEqual(self.store.list_merge_admission_records(), ())
        self.assertEqual(self.store.list_merge_train_controller_state_records()[0].status, "idle")
        self.assertEqual(
            self.store.list_merge_train_batch_candidate_records()[0].status, "superseded"
        )
        retired = [
            record
            for record in self.store.list_merge_train_batch_landing_plan_records()
            if record.source.startswith("service:controller:lineage-changed-landing:")
        ]
        self.assertEqual(len(retired), 1)
        self.assertEqual({entry.status for entry in retired[0].landing_plan.entries}, {"stale"})

        older = _queued_pull_request(
            number=2080, head_sha="d" * 40, created_at="2026-08-11T00:30:00Z"
        )
        planned = _queued_pull_request(
            number=2083, head_sha=HEAD_SHA, created_at="2026-08-11T01:00:00Z"
        )
        snapshot = MergeTrainDryRunSnapshot(
            repository=REPOSITORY,
            base_branch="main",
            base_sha=BASE_SHA,
            pull_requests=(older, planned),
        )
        with (
            patch.object(self.client, "read_merge_train_snapshot", return_value=snapshot),
            patch(
                "control_plane.merge_train_controller_run_once._probe_queue_entry_conflicts",
                side_effect=lambda **kwargs: _ConflictProbeOutcome(
                    snapshot=kwargs["snapshot"],
                    dry_run_result=kwargs["dry_run_result"],
                    held_out=kwargs["held_out"],
                ),
            ),
        ):
            fresh = self._run()
        self.assertEqual(fresh.accepted_result["controller_action"], "plan_candidate")
        (active,) = self.store.list_merge_train_batch_candidate_records(status="active")
        self.assertEqual(
            [entry.pull_request_number for entry in active.candidate.entries], [2080, 2083]
        )

    def test_retired_plan_does_not_suppress_the_same_batch_when_the_queue_returns(
        self,
    ) -> None:
        self._run()
        rebuilt, _, _, _ = _guard_records(policy_sha256=self.policy.policy_sha256)
        rebuilt = rebuilt.model_copy(update={"record_id": "rebuilt-same-batch-candidate"})
        self.store.write_merge_train_batch_candidate_record(rebuilt)
        snapshot = MergeTrainDryRunSnapshot(
            repository=REPOSITORY,
            base_branch="main",
            base_sha=BASE_SHA,
            pull_requests=(
                _queued_pull_request(
                    number=2083, head_sha=HEAD_SHA, created_at="2026-08-11T01:00:00Z"
                ),
            ),
        )
        with patch.object(self.client, "read_merge_train_snapshot", return_value=snapshot):
            result = self._run(mutate=False)
        self.assertEqual(result.accepted_result["controller_action"], "plan_landing")

    def test_only_an_unlanded_plain_plan_is_retired_on_a_lineage_change(self) -> None:
        plan = _landing_plan()
        unlanded = self.landing.model_copy(update={"landing_plan": plan})
        partial = self.landing.model_copy(
            update={
                "landing_plan": plan.model_copy(
                    update={
                        "entries": (
                            plan.entries[0].model_copy(update={"status": "merged"}),
                            plan.entries[1],
                        )
                    }
                )
            }
        )
        cases = {
            "unlanded": (unlanded, "landing_lineage_changed", False, True),
            "partial_landing": (partial, "landing_lineage_changed", False, False),
            "collapsed_stack": (unlanded, "landing_lineage_changed", True, False),
            "other_denial": (unlanded, "merge_readiness_not_ready", False, False),
        }
        for case, (record, reason_code, has_stack_collapse, expected) in cases.items():
            with self.subTest(case=case):
                self.assertIs(
                    _lineage_change_retires_landing(
                        reason_code=reason_code,
                        landing_record=record,
                        has_stack_collapse=has_stack_collapse,
                    ),
                    expected,
                )


if __name__ == "__main__":
    unittest.main()
