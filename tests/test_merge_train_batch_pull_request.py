import unittest
import hashlib
from unittest.mock import patch
from collections.abc import Callable
from copy import deepcopy
from types import SimpleNamespace
from typing import Any, cast

from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidate,
    build_merge_train_batch_candidate_ref,
    build_merge_train_batch_id,
    build_merge_train_batch_landing_plan,
)
from control_plane.merge_admission import (
    GuardedMergeAdmission,
    MergeAdmissionDeniedError,
    MergeAdmissionReconciliationRequiredError,
)
from control_plane.merge_train_github import (
    GitHubMergeTrainClient,
    MergeTrainGitHubError,
    MergeTrainGitHubMergeRejectedError,
    MergeTrainGitHubStaleHeadError,
)
from tests.test_merge_train_github import (
    _combined_status,
    _git_commit,
    _protected_branch_with_checks,
    _required_check_run,
)
from tests.test_merge_train_structural_provenance import _entry, _records


class _BatchProvider:
    def __init__(self, candidate: Any) -> None:
        self.candidate = candidate
        self.number = 99
        self.created = False
        self.closed = False
        self.merged = False
        self.repository_name = candidate.repository
        self.settled = True
        self.checks_pass = True
        self.refuse = False
        self.refusal_observed = False
        self.interrupt_after_merge = False
        self.merge_sha = hashlib.sha256(b"batch-merge").hexdigest()[:40]
        self.base_sha = candidate.base_sha
        self.heads = {entry.pull_request_number: entry.head_sha for entry in candidate.entries}
        self.body = ""
        self.requests: list[tuple[str, str, dict[str, object] | None]] = []
        self.before_merge: Callable[[], None] = lambda: None

    @property
    def merge_calls(self) -> list[tuple[str, str, dict[str, object] | None]]:
        return [request for request in self.requests if request[0] == "PUT"]

    def pull_request(self, number: int) -> dict[str, object]:
        batch = number == self.number
        merged = self.merged and (batch or self.settled)
        return {
            "number": number,
            "state": "closed" if merged or (batch and self.closed) else "open",
            "merged": merged,
            "merge_commit_sha": self.merge_sha if merged else None,
            "draft": False,
            "body": self.body,
            "head": {
                "sha": self.candidate.candidate_sha if batch else self.heads[number],
                "ref": self.candidate.candidate_ref.removeprefix("refs/heads/")
                if batch
                else f"feature-{number}",
                "repo": {"full_name": self.repository_name},
            },
            "base": {
                "ref": "main",
                "sha": self.base_sha,
                "repo": {"full_name": self.repository_name},
            },
            "mergeable_state": "behind" if batch and self.refusal_observed else "clean",
        }

    def request(self, *, method: str, path: str, body: dict[str, object] | None = None) -> object:
        self.requests.append((method, path, deepcopy(body)))
        if method == "GET" and "/pulls?" in path:
            return [self.pull_request(self.number)] if self.created else []
        if method == "POST" and path.endswith("/pulls"):
            assert body is not None
            assert isinstance(body["body"], str)
            if self.created:
                self.number += 1
            self.created = True
            self.closed = False
            self.body = body["body"]
            return self.pull_request(self.number)
        if method == "GET" and "/pulls/" in path:
            return self.pull_request(int(path.rsplit("/", maxsplit=1)[1]))
        if method == "PATCH" and path.endswith(f"/pulls/{self.number}"):
            assert body == {"state": "closed"}
            self.closed = True
            return self.pull_request(self.number)
        if method == "GET" and path.endswith("/branches/main"):
            return {
                **_protected_branch_with_checks("ci-gate"),
                "commit": {
                    "sha": self.base_sha,
                    "commit": {
                        "tree": {
                            "sha": self.candidate.candidate_tree_sha
                            if self.merged
                            else self.candidate.structural_provenance.base_tree_sha
                        }
                    },
                },
            }
        if method == "GET" and "/status?" in path:
            return _combined_status(statuses=())
        if method == "GET" and "/check-runs?" in path:
            return {
                "check_runs": [
                    _required_check_run(
                        "ci-gate",
                        "completed" if self.checks_pass else "in_progress",
                        "success" if self.checks_pass else None,
                    )
                ]
            }
        if method == "GET" and "/git/commits/" in path:
            sha = path.rsplit("/", maxsplit=1)[1]
            if sha == self.merge_sha:
                return _git_commit(
                    sha,
                    self.candidate.candidate_tree_sha,
                    parents=(self.candidate.base_sha, self.candidate.candidate_sha),
                )
            entry = next(entry for entry in self.candidate.entries if entry.head_sha == sha)
            return _git_commit(sha, entry.head_tree_sha)
        if method == "GET" and "/compare/" in path:
            return {"status": "ahead" if self.merged else "diverged"}
        if method == "PUT" and path.endswith(f"/pulls/{self.number}/merge"):
            self.before_merge()
            if self.refuse:
                self.refusal_observed = True
                raise MergeTrainGitHubError("refused", status_code=405)
            if body != {"sha": self.candidate.candidate_sha, "merge_method": "merge"}:
                raise AssertionError("Batch merge must guard the exact candidate SHA and method")
            if self.base_sha != self.candidate.base_sha:
                raise AssertionError("Strict base protection refused the batch")
            self.merged = True
            self.base_sha = self.merge_sha
            if self.interrupt_after_merge:
                raise KeyboardInterrupt("process exited after the provider effect")
            return {"sha": self.merge_sha, "merged": True}
        raise AssertionError(f"Unexpected provider request: {method} {path}")


class _BatchGuard:
    def __init__(self, candidate_record: Any, landing_record: Any) -> None:
        self.candidate_record = candidate_record
        self.landing_plan_record = landing_record
        self.admissions: dict[int, SimpleNamespace] = {}
        self.outcomes: dict[int, str] = {}
        self.after_admit: Callable[[int], None] = lambda _number: None

    def reconcile_batch_no_effect(self, **_kwargs: Any) -> None:
        for number in self.admissions:
            if self.outcomes.get(number) != "landed":
                self.outcomes[number] = "rejected"

    @staticmethod
    def build_proposal(**_kwargs: Any) -> None:
        pass

    def admit(self, **kwargs: Any) -> Any:
        number = kwargs["entry"].pull_request_number
        if number in self.admissions and number not in self.outcomes:
            raise AssertionError("An unresolved admission must be reconciled first")
        admission = SimpleNamespace(pull_request_number=number, admission_id=f"admission-{number}")
        self.admissions[number] = admission
        self.outcomes.pop(number, None)
        self.after_admit(number)
        return admission

    def record_not_dispatched(self, **kwargs: Any) -> None:
        self.outcomes[kwargs["admission"].pull_request_number] = "batch_not_dispatched"

    def record_provider_failure(self, **kwargs: Any) -> None:
        self.outcomes[kwargs["admission"].pull_request_number] = (
            "rejected" if kwargs["error"].status_code == 405 else "reconcile_required"
        )

    def record_reconcile_required(self, **kwargs: Any) -> None:
        self.outcomes[kwargs["admission"].pull_request_number] = "reconcile_required"

    def record_landed(self, **kwargs: Any) -> None:
        self.outcomes[kwargs["admission"].pull_request_number] = "landed"

    def reconcile_existing_landed(self, **kwargs: Any) -> None:
        number = kwargs["entry"].pull_request_number
        if number not in self.admissions or self.outcomes.get(number) == "rejected":
            raise MergeAdmissionReconciliationRequiredError("Missing preceding batch admission")
        self.outcomes[number] = "landed"

    def update_landing_plan(self, plan: Any) -> None:
        self.landing_plan_record = self.landing_plan_record.model_copy(
            update={"landing_plan": plan}
        )

    def update_landing_plan_record(self, record: Any) -> None:
        self.landing_plan_record = record


class ProtectedBatchPullRequestTests(unittest.TestCase):
    def test_legacy_landing_plan_binding_stays_readable(self) -> None:
        from tests.test_merge_train_github import _landing_plan

        plan = _landing_plan()
        self.assertEqual(
            plan.landing_plan_sha256,
            "a4bb62122f6803c5b2c05b4570c63855d06b6533b16af4c051e89d57bca669f2",
        )
        self.assertNotIn("candidate_pull_request_number", plan.model_dump(mode="json"))

    def setUp(self) -> None:
        sleeper = patch("control_plane.merge_train_batch_pull_request.sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)
        candidate_record, landing_record = _records((_entry(1, 1), _entry(2, 2)))

        def git_identities(value: Any) -> Any:
            if isinstance(value, dict):
                return {
                    key: hashlib.sha256(item.encode()).hexdigest()[:40]
                    if key.endswith("_sha") and isinstance(item, str)
                    else ""
                    if key in {"candidate_sha256", "provenance_sha256"}
                    else git_identities(item)
                    for key, item in value.items()
                }
            if isinstance(value, list):
                return [git_identities(item) for item in value]
            return value

        payload = git_identities(candidate_record.candidate.model_dump(mode="json"))
        payload["batch_id"] = build_merge_train_batch_id(
            repository=payload["repository"],
            base_branch="main",
            base_sha=payload["base_sha"],
            entry_head_shas=tuple(entry["head_sha"] for entry in payload["entries"]),
        )
        payload["candidate_ref"] = build_merge_train_batch_candidate_ref(
            repository=payload["repository"],
            base_branch="main",
            batch_id=payload["batch_id"],
        )
        candidate_record = candidate_record.model_copy(
            update={"candidate": MergeTrainBatchCandidate.model_validate(payload)}
        )
        self.provider = _BatchProvider(candidate_record.candidate)
        self.client = GitHubMergeTrainClient(transport=self.provider)
        number = self.client.ensure_batch_pull_request(candidate=candidate_record.candidate)
        plan = build_merge_train_batch_landing_plan(
            candidate=candidate_record.candidate,
            merge_method="merge",
            created_at=candidate_record.updated_at,
            candidate_pull_request_number=number,
        )
        self.guard = _BatchGuard(
            candidate_record, landing_record.model_copy(update={"landing_plan": plan})
        )
        self.progress: list[Any] = []
        self.provider.before_merge = lambda: self.assertEqual(set(self.guard.admissions), {1, 2})

    def land(self, checkpoint: Any = None) -> Any:
        return self.client.land_batch_candidate(
            landing_plan=self.guard.landing_plan_record.landing_plan,
            admission_guard=cast(GuardedMergeAdmission, self.guard),
            recorded_at="2026-08-11T04:02:00Z",
            checkpoint=checkpoint or (lambda plan, _entry, _phase: self.progress.append(plan)),
        )

    def test_creation_reuses_the_bound_candidate_pr_without_rewriting_sources(self) -> None:
        number = self.client.ensure_batch_pull_request(
            candidate=self.guard.candidate_record.candidate
        )
        self.assertEqual(number, 99)
        self.assertEqual(
            len([request for request in self.provider.requests if request[0] == "POST"]), 1
        )
        for entry in self.provider.candidate.entries:
            self.assertEqual(self.provider.heads[entry.pull_request_number], entry.head_sha)
            self.assertIn(f"#{entry.pull_request_number} at `{entry.head_sha}`", self.provider.body)

    def test_both_entries_land_through_one_sha_guarded_protected_merge(self) -> None:
        landed = self.land()
        self.assertEqual([entry.status for entry in landed.entries], ["merged", "merged"])
        self.assertEqual(
            {entry.merge_commit_sha for entry in landed.entries}, {self.provider.merge_sha}
        )
        self.assertEqual(self.guard.outcomes, {1: "landed", 2: "landed"})
        self.assertEqual(len(self.provider.merge_calls), 1)
        self.assertTrue(self.provider.merge_calls[0][1].endswith("/pulls/99/merge"))

    def test_repository_capitalization_does_not_change_candidate_identity(self) -> None:
        self.provider.repository_name = self.provider.candidate.repository.upper()
        self.assertEqual(
            self.client.ensure_batch_pull_request(candidate=self.provider.candidate), 99
        )
        self.assertTrue(all(entry.status == "merged" for entry in self.land().entries))

    def test_closed_batch_is_terminal_without_new_admissions(self) -> None:
        self.provider.closed = True
        with self.assertRaises(MergeTrainGitHubStaleHeadError):
            self.land()
        self.assertEqual(self.guard.admissions, {})
        self.assertEqual(self.provider.merge_calls, [])

    def test_retirement_closes_only_the_bound_batch_pr_and_is_idempotent(self) -> None:
        self.client.close_batch_pull_request(candidate=self.provider.candidate)
        self.client.close_batch_pull_request(candidate=self.provider.candidate)
        self.assertTrue(self.provider.closed)
        self.assertEqual(len([r for r in self.provider.requests if r[0] == "PATCH"]), 1)
        self.assertTrue(all(self.provider.pull_request(n)["state"] == "open" for n in (1, 2)))

    def test_retirement_does_not_dispose_of_an_unrecorded_merge(self) -> None:
        self.provider.merged = True
        with self.assertRaises(MergeAdmissionReconciliationRequiredError):
            self.client.close_batch_pull_request(candidate=self.provider.candidate)
        self.assertFalse(any(r[0] == "PATCH" for r in self.provider.requests))

    def test_retirement_handles_mutable_batch_pr_drift(self) -> None:
        original = self.provider.pull_request
        for changed in ("head", "body", "draft", "base"):
            with self.subTest(changed=changed):
                self.provider.closed = False

                def altered(number: int) -> dict[str, object]:
                    payload = original(number)
                    if number == self.provider.number:
                        if changed in {"head", "base"}:
                            nested = payload[changed]
                            assert isinstance(nested, dict)
                            nested["sha" if changed == "head" else "ref"] = "changed"
                        else:
                            payload[changed] = True if changed == "draft" else "Edited body"
                    return payload

                with patch.object(self.provider, "pull_request", side_effect=altered):
                    with self.assertRaises(MergeTrainGitHubStaleHeadError):
                        self.client.ensure_batch_pull_request(candidate=self.provider.candidate)
                    self.client.close_batch_pull_request(candidate=self.provider.candidate)
                self.assertTrue(self.provider.closed)
        self.assertEqual(self.provider.merge_calls, [])

    def test_rebuilt_candidate_does_not_reuse_an_old_closed_pr(self) -> None:
        self.provider.closed = True
        self.provider.body = self.provider.body.replace(
            self.provider.candidate.candidate_sha, "older-sha"
        )
        number = self.client.ensure_batch_pull_request(candidate=self.provider.candidate)
        self.assertEqual(number, 100)
        self.assertFalse(self.provider.closed)
        self.assertEqual(len([r for r in self.provider.requests if r[0] == "POST"]), 2)

    def test_pending_checks_do_not_hide_a_changed_member(self) -> None:
        self.provider.checks_pass = False
        self.provider.heads[2] = "new-head"
        with self.assertRaises(MergeTrainGitHubStaleHeadError):
            self.land()
        self.assertEqual(self.guard.admissions, {})
        self.assertEqual(self.provider.merge_calls, [])

    def test_pending_candidate_checks_do_not_issue_admissions_or_merge(self) -> None:
        self.provider.checks_pass = False
        with self.assertRaises(MergeAdmissionDeniedError):
            self.land()
        self.assertEqual(self.guard.admissions, {})
        self.assertEqual(self.provider.merge_calls, [])

    def test_later_head_drift_records_non_dispatch_for_the_admitted_prefix(self) -> None:
        self.guard.after_admit = lambda _number: self.provider.heads.update({2: "new-head"})
        with self.assertRaises(MergeTrainGitHubError):
            self.land()
        self.assertEqual(self.guard.outcomes, {1: "batch_not_dispatched"})
        self.assertEqual(self.provider.merge_calls, [])

    def test_provider_refusal_applies_to_both_admissions_without_member_merges(self) -> None:
        self.provider.refuse = True
        with self.assertRaises(MergeTrainGitHubMergeRejectedError):
            self.land()
        self.assertEqual(self.guard.outcomes, {1: "rejected", 2: "rejected"})
        self.assertEqual(len(self.provider.merge_calls), 1)
        self.assertFalse(self.provider.merged)

    def test_interrupted_provider_effect_reconciles_without_a_second_merge(self) -> None:
        self.provider.interrupt_after_merge = True
        with self.assertRaises(KeyboardInterrupt):
            self.land()
        self.assertEqual(self.guard.outcomes, {})
        landed = self.land()
        self.assertTrue(all(entry.status == "merged" for entry in landed.entries))
        self.assertEqual(len(self.provider.merge_calls), 1)
        self.assertEqual(self.guard.outcomes, {1: "landed", 2: "landed"})

    def test_pending_indirect_completion_reconciles_without_a_second_merge(self) -> None:
        self.provider.settled = False
        with self.assertRaises(MergeAdmissionReconciliationRequiredError):
            self.land()
        self.assertEqual(self.guard.outcomes, {1: "reconcile_required", 2: "reconcile_required"})
        self.provider.settled = True
        self.land()
        self.assertEqual(len(self.provider.merge_calls), 1)
        self.assertEqual(self.guard.outcomes, {1: "landed", 2: "landed"})

    def test_brief_indirect_completion_lag_finishes_in_the_same_landing_pass(self) -> None:
        self.provider.settled = False
        with patch(
            "control_plane.merge_train_batch_pull_request.sleep",
            side_effect=lambda _delay: setattr(self.provider, "settled", True),
        ):
            landed = self.land()
        self.assertTrue(all(entry.status == "merged" for entry in landed.entries))
        self.assertEqual(len(self.provider.merge_calls), 1)

    def test_checkpoint_interruption_does_not_repeat_the_shared_effect(self) -> None:
        def interrupt(plan: Any, _entry: Any, phase: str) -> None:
            if phase == "entry_merged":
                self.guard.update_landing_plan(plan)
                raise KeyboardInterrupt("lost after first constituent outcome")

        with self.assertRaises(KeyboardInterrupt):
            self.land(checkpoint=interrupt)
        self.assertEqual(self.guard.outcomes, {1: "landed"})
        self.land()
        self.assertEqual(len(self.provider.merge_calls), 1)
        self.assertEqual(self.guard.outcomes, {1: "landed", 2: "landed"})

    def test_member_push_after_dispatch_cannot_be_reported_as_its_new_head_landing(self) -> None:
        self.provider.before_merge = lambda: self.provider.heads.update(
            {2: "pushed-after-admission"}
        )
        with self.assertRaises(MergeAdmissionReconciliationRequiredError):
            self.land()
        self.assertEqual(self.guard.outcomes, {1: "reconcile_required", 2: "reconcile_required"})
        self.assertEqual(len(self.provider.merge_calls), 1)
