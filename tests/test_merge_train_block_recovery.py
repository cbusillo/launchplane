"""Regression for author-fixed candidate blocks, including unchanged-head refusal."""

from __future__ import annotations

import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote
from typing import Any
from unittest.mock import Mock, patch
from contextlib import ExitStack
from types import SimpleNamespace

from control_plane.contracts.merge_train_batch import (
    build_merge_train_batch_candidate_record,
    MergeTrainCandidateBlockIntent,
)
from control_plane.contracts.merge_train_policy import MergeTrainPolicyRecord
from control_plane.contracts.merge_train_pr_feedback_record import MergeTrainPrFeedbackRecord
from control_plane.merge_train import MergeTrainBlockResult, build_merge_train_dry_run_result
from control_plane.merge_train_controller_run_once import (
    MergeTrainControllerRunOnceEnvelope,
    _advance_from_live_snapshot,
)
from control_plane.merge_train_github import (
    GitHubMergeTrainClient,
    GitHubMergeTrainSnapshotReader,
    MergeTrainGitHubError,
)
from control_plane.merge_train_setup_recovery_receipt import read_setup_recovery_receipt
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.merge_train_policy_fixtures import build_test_merge_train_policy
from tests.support.merge_train import _FakeExpandedMergeTrainSnapshotReader
from control_plane.contracts.merge_train_batch import build_merge_train_batch_candidate


class CandidateBlockRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store: FilesystemRecordStore | PostgresRecordStore = FilesystemRecordStore(
            Path(self.directory.name)
        )
        self.policy = build_test_merge_train_policy(trusted_automation_github_user_ids=(42,))
        self.target = self.policy.policies[0]
        source_app = self.target.github_token.github_app
        assert source_app is not None
        self.writer_app_id = source_app.app_id
        before = MergeTrainPolicyRecord(
            record_id="before", source="test", updated_at="2026-10-18T00:00:00Z", policy=self.policy
        )
        self.store.write_merge_train_policy_record(before)
        self.policy_digest = before.policy_sha256
        template = _FakeExpandedMergeTrainSnapshotReader(
            transport=object()
        ).read_merge_train_snapshot(
            repository=self.target.repository, base_branch=self.target.base_branch
        )
        self.old = template.pull_requests[1].model_copy(update={"head_sha": "a" * 40})
        self.other = template.pull_requests[0]
        initial = template.model_copy(update={"pull_requests": (self.old,)})
        candidate = build_merge_train_batch_candidate(
            dry_run_result=build_merge_train_dry_run_result(policy=self.policy, snapshot=initial),
            base_sha=initial.base_sha,
            policy_sha256=self.policy_digest,
            created_at="2026-10-18T00:00:00Z",
        ).model_copy(update={"status": "failed", "required_checks_status": "fail"})
        self.record = build_merge_train_batch_candidate_record(
            candidate=candidate,
            source="service:controller:batch-candidate-failed:test",
            updated_at="2026-10-18T00:01:00Z",
            block_intent=MergeTrainCandidateBlockIntent(
                label=self.target.blocked_label,
                prior_label_event_id=0,
                applied_label_event_id=50,
                writer_app_id=self.writer_app_id,
                source_failed_record_id="source-failed",
            ),
        ).model_copy(update={"status": "superseded"})
        self.store.write_merge_train_batch_candidate_record(self.record)
        self.feedback = MergeTrainPrFeedbackRecord(
            feedback_id="blocked-feedback",
            repository=self.target.repository,
            base_branch=self.target.base_branch,
            pull_request_number=self.old.number,
            pull_request_url="https://github.com/example/repo/pull/2",
            event="blocked",
            marker="marker",
            comment_markdown="Recorded block",
            source="launchplane:merge-train-scheduler",
            recorded_at="2026-10-18T00:02:00Z",
            policy_key=self.target.policy_key,
            policy_sha256=self.policy_digest,
            controller_action="block",
            controller_record_id=self.record.record_id,
            delivery_status="delivered",
            comment_id=100,
        )
        self.store.write_merge_train_pr_feedback_record(self.feedback)
        self.current = self.old.model_copy(
            update={"head_sha": "b" * 40, "labels": (self.target.blocked_label,)}
        )
        self.snapshot = template.model_copy(update={"pull_requests": (self.current,)})
        self.comments: list[dict[str, Any]] = [
            {
                "id": 101,
                "body": f"Review result: weighed\nHead: {self.current.head_sha}\n\nFindings addressed.",
                "user": {"id": 42, "login": "train[bot]"},
                "created_at": "2026-10-18T00:03:00Z",
                "updated_at": "2026-10-18T00:03:00Z",
            }
        ]
        self.label_event: dict[str, Any] = {
            "id": 50,
            "event": "labeled",
            "label": {"name": self.target.blocked_label},
            "actor": {"id": 42, "login": "train[bot]", "type": "Bot"},
            "created_at": "2026-10-18T00:01:30Z",
        }
        self.block_comment = {
            "id": 100,
            "body": self.feedback.comment_markdown,
            "user": {"id": 42, "login": "train[bot]"},
            "created_at": "2026-10-18T00:02:00Z",
            "updated_at": "2026-10-18T00:02:00Z",
        }
        self.events: list[dict[str, Any]] | None = None
        self.mutations: list[str] = []
        self.transport = Mock()
        self.transport.token_permissions = None
        self.transport.repository_private = None
        self.transport.request.side_effect = self.request
        self.client = GitHubMergeTrainClient(transport=self.transport)
        self.client.read_pull_request_snapshot = Mock(side_effect=lambda **_: self.current)  # type: ignore[method-assign]
        self.lease = Mock()
        self.lease.record.ordinary_job_binding = None
        self.lease.record.updated_at = "2026-10-18T00:02:00Z"

    def request(self, *, method: str, path: str, body: object = None) -> object:
        del body
        if path in getattr(self, "setup_responses", {}):
            value = self.setup_responses[path]
            if isinstance(value, BaseException):
                raise value
            return value
        if method == "DELETE":
            self.mutations.append(path)
            self.current = self.current.model_copy(update={"labels": ()})
            return None
        if "/events?" in path:
            return self.events if self.events is not None else [self.label_event]
        if path.endswith("/issues/comments/100"):
            return self.block_comment
        if "/comments?" in path:
            return self.comments
        if "/collaborators/" in path:
            return {"permission": "read"}
        if path == "/apps/train":
            return {"id": self.writer_app_id}
        if "/pulls/" in path:
            return {"labels": [{"name": label} for label in self.current.labels]}
        raise AssertionError((method, path))

    def setup_missing_store_failure(self) -> None:
        """The observed singleton failure: install fails, browser never starts."""
        self.current = self.current.model_copy(update={"head_sha": self.old.head_sha})
        self.snapshot = self.snapshot.model_copy(update={"pull_requests": (self.current,)})
        self.comments[0]["body"] = (
            f"Review result: weighed\nHead: {self.current.head_sha}\n\nFindings addressed."
        )
        failed = self.record.candidate.model_copy(update={"candidate_sha": "c" * 40})
        self.record = self.record.model_copy(update={"candidate": failed})
        self.store.write_merge_train_batch_candidate_record(self.record)
        prefix = f"/repos/{self.target.repository}"
        self.job: dict[str, Any] = {
            "id": 91,
            "run_id": 81,
            "run_attempt": 1,
            "head_sha": failed.candidate_sha,
            "name": "frontend_browser_smoke (2)",
            "status": "completed",
            "conclusion": "failure",
            "steps": [
                {"name": "Checkout", "conclusion": "success"},
                {"name": "Install frontend dependencies", "conclusion": "failure"},
                {"name": "Run browser smoke", "conclusion": "skipped"},
                {"name": "Upload browser evidence", "conclusion": "failure"},
            ],
        }

        def check(identifier: int, name: str, conclusion: str) -> dict[str, object]:
            return {
                "id": identifier,
                "name": name,
                "status": "completed",
                "conclusion": conclusion,
                "head_sha": failed.candidate_sha,
                "app": {"slug": "github-actions"},
                "details_url": f"https://github.com/{self.target.repository}/actions/runs/81/job/{identifier}",
            }

        self.setup_responses: dict[str, Any] = {
            f"{prefix}/commits/{failed.candidate_sha}/check-runs?per_page=100&page=1&filter=all": {
                "total_count": 2,
                "check_runs": [
                    check(91, self.job["name"], "failure"),
                    check(92, "ci-gate", "failure"),
                ],
            },
            f"{prefix}/commits/{failed.candidate_sha}/status?per_page=100&page=1": {
                "total_count": 0,
                "statuses": [],
            },
            f"{prefix}/actions/jobs/91": self.job,
            f"{prefix}/actions/jobs/91/logs": (
                "ERR_PNPM_ENOENT ENOENT: no such file or directory, copyfile "
                "'/home/runner/.local/share/pnpm/store/v10/files/71/" + "a" * 126 + "' -> "
                "'/work/frontend/node_modules/.pnpm/lightningcss/node_modules/file.node'\n"
            ),
            f"{prefix}/actions/runs/81/attempts/1/jobs?per_page=100&page=1": {
                "total_count": 3,
                "jobs": [
                    self.job,
                    {
                        "id": 93,
                        "run_id": 81,
                        "run_attempt": 1,
                        "head_sha": failed.candidate_sha,
                        "name": "frontend_browser_smoke (1)",
                        "status": "completed",
                        "conclusion": "success",
                    },
                    {
                        "id": 94,
                        "run_id": 81,
                        "run_attempt": 1,
                        "head_sha": failed.candidate_sha,
                        "name": "frontend_validate",
                        "status": "completed",
                        "conclusion": "success",
                    },
                ],
            },
            f"{prefix}/compare/{failed.base_sha}...{self.old.head_sha}": {
                "base_commit": {"sha": failed.base_sha},
                "total_commits": 1,
                "commits": [{"sha": self.old.head_sha}],
                "files": [{"filename": "control_plane/testing_retry.py", "status": "modified"}],
            },
        }

    def test_same_head_missing_store_block_recovers_once_with_durable_proof(self) -> None:
        self.setup_missing_store_failure()
        result = self.advance(mutate=True)
        self.assertEqual(result["controller_action"], "recover_candidate_block")
        saved = read_setup_recovery_receipt(
            self.store, self.target.repository, self.current.number, self.current.head_sha
        )
        self.assertIsNotNone(saved)
        self.assertEqual(len(self.mutations), 1)
        self.current = self.current.model_copy(update={"labels": (self.target.blocked_label,)})
        self.events = [
            self.label_event,
            {**self.label_event, "id": 51, "event": "unlabeled"},
            {**self.label_event, "id": 52},
        ]
        assert self.record.block_intent is not None
        self.store.write_merge_train_batch_candidate_record(
            self.record.model_copy(
                update={
                    "record_id": "another-failed-candidate",
                    "block_intent": self.record.block_intent.model_copy(
                        update={
                            "prior_label_event_id": 51,
                            "applied_label_event_id": 52,
                        }
                    ),
                }
            )
        )
        self.assertNotEqual(
            self.advance(mutate=True)["controller_action"], "recover_candidate_block"
        )
        self.assertEqual(len(self.mutations), 1)

    def test_setup_recovery_dry_run_and_exhausted_retry_budget(self) -> None:
        self.setup_missing_store_failure()
        self.assertEqual(self.advance()["controller_action"], "recover_candidate_block")
        self.assertIsNone(
            read_setup_recovery_receipt(
                self.store, self.target.repository, self.current.number, self.current.head_sha
            )
        )
        self.assertEqual(self.mutations, [])
        for field in ("infrastructure_retry_of", "check_retry_of", "batch_body_retry_of"):
            with self.subTest(field=field):
                self.store.write_merge_train_batch_candidate_record(
                    self.record.model_copy(
                        update={
                            "candidate": self.record.candidate.model_copy(
                                update={field: "already-spent"}
                            )
                        }
                    )
                )
                self.assertNotEqual(
                    self.advance(mutate=True)["controller_action"], "recover_candidate_block"
                )
                self.assertEqual(self.mutations, [])

    def test_snapshot_reader_enforces_recovery_gate_and_refreshes_exact_receipt(self) -> None:
        from tests.test_merge_train_github import _github_pull_request
        from control_plane.merge_train import MergeTrainLabelActor

        self.setup_missing_store_failure()
        reader = GitHubMergeTrainSnapshotReader(
            transport=self.transport, branch_refresh_store=self.store, repository_policy=self.target
        )
        detail = _github_pull_request(
            self.current.number,
            repository=self.target.repository,
            head_sha=self.current.head_sha,
            base_sha=self.snapshot.base_sha,
        )
        detail["labels"] = [{"name": self.target.enqueue_label}]
        prefix = f"/repos/{self.target.repository}"
        self.setup_responses[f"{prefix}/pulls/{self.current.number}"] = detail
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(reader, "_base_branch_sha", return_value=self.snapshot.base_sha)
            )
            stack.enter_context(
                patch.object(reader, "_list_open_pull_requests", return_value=(detail,))
            )
            stack.enter_context(
                patch.object(reader, "_actor_role_for_pull_request", return_value="repo_admin")
            )
            stack.enter_context(
                patch.object(
                    reader,
                    "_label_actors",
                    return_value=(
                        MergeTrainLabelActor(label=self.target.enqueue_label, actor_id=42),
                    ),
                )
            )
            stack.enter_context(
                patch.object(
                    reader,
                    "client_review_intent",
                    return_value=SimpleNamespace(
                        explicit_override=True, error="", review_required=False
                    ),
                )
            )
            stack.enter_context(patch.object(reader, "client_veto", return_value=False))
            stack.enter_context(
                patch.object(reader, "_required_checks_observation", return_value=("pass", ""))
            )
            stack.enter_context(
                patch.object(
                    reader,
                    "_with_review_conversations",
                    side_effect=lambda **kw: kw["pull_requests"],
                )
            )
            stack.enter_context(
                patch(
                    "control_plane.merge_train_github.individual_landing_snapshots",
                    side_effect=lambda **kw: kw["pull_requests"],
                )
            )
            # The same reader first sees no reservation, then a committed one.
            self.assertEqual(
                reader.read_pull_request_snapshot(
                    repository=self.target.repository, pull_request_number=self.current.number
                ).recovery_ready_comment_floor,
                0,
            )
            self.advance(mutate=True)
            for fresh in (False, True):
                if fresh:
                    self.comments.append(
                        {
                            "id": 102,
                            "body": f"Ready for the merge train\n\nHead: {self.current.head_sha}",
                            "user": {"id": 42, "login": "train[bot]"},
                            "created_at": "2026-10-18T00:05:00Z",
                        }
                    )
                snapshot = reader.read_merge_train_snapshot(
                    repository=self.target.repository, base_branch=self.target.base_branch
                )
                result = build_merge_train_dry_run_result(
                    policy=self.policy, snapshot=snapshot, batch_landing=True, new_candidate=True
                )
                self.assertEqual(bool(result.queue_order), fresh)

    def test_green_rerun_preserves_original_attempt_attribution_across_pages(self) -> None:
        self.setup_missing_store_failure()
        prefix = f"/repos/{self.target.repository}"
        checks = f"{prefix}/commits/{self.record.candidate.candidate_sha}/check-runs"
        key = f"{checks}?per_page=100&page=1&filter=all"
        original = self.setup_responses[key]["check_runs"]
        rows = [
            {**original[0], "id": 1000 + index, "conclusion": "success"} for index in range(100)
        ] + original
        self.setup_responses[key] = {"total_count": len(rows), "check_runs": rows[:100]}
        self.setup_responses[f"{checks}?per_page=100&page=2&filter=all"] = {
            "total_count": len(rows),
            "check_runs": rows[100:],
        }
        jobs = f"{prefix}/actions/runs/81/attempts/1/jobs"
        key = f"{jobs}?per_page=100&page=1"
        original_jobs = self.setup_responses[key]["jobs"]
        rows = [
            {**original_jobs[1], "id": 2000 + index, "name": "other_success"}
            for index in range(100)
        ] + original_jobs
        self.setup_responses[key] = {"total_count": len(rows), "jobs": rows[:100]}
        self.setup_responses[f"{jobs}?per_page=100&page=2"] = {
            "total_count": len(rows),
            "jobs": rows[100:],
        }
        self.assertEqual(self.advance(mutate=True)["controller_action"], "recover_candidate_block")

    def test_legacy_reservation_resumes_after_newer_feedback(self) -> None:
        self.setup_missing_store_failure()
        self.record = self.record.model_copy(update={"block_intent": None})
        self.store.write_merge_train_batch_candidate_record(self.record)
        request = self.request

        def failed_delete(**kwargs: Any) -> object:
            if kwargs["method"] == "DELETE":
                raise MergeTrainGitHubError("reply unavailable before effect")
            return request(**kwargs)

        with patch(
            "control_plane.merge_train_batch_pull_request.bound_closed_batch_pull_request",
            return_value={"closed_at": "2026-10-18T00:01:00Z"},
        ):
            self.transport.request.side_effect = failed_delete
            with self.assertRaises(MergeTrainGitHubError):
                self.advance(mutate=True)
            self.assertIsNotNone(
                read_setup_recovery_receipt(
                    self.store, self.target.repository, self.current.number, self.current.head_sha
                )
            )
            self.store.write_merge_train_pr_feedback_record(
                self.feedback.model_copy(
                    update={
                        "feedback_id": "newer-waiting",
                        "event": "waiting",
                        "controller_action": "wait",
                        "recorded_at": "2026-10-18T00:05:00Z",
                    }
                )
            )
            self.transport.request.side_effect = request
            self.assertEqual(
                self.advance(mutate=True)["controller_action"], "recover_candidate_block"
            )
            self.assertEqual(len(self.mutations), 1)

    def test_optional_setup_evidence_denial_preserves_hold_and_other_train_work(self) -> None:
        self.setup_missing_store_failure()
        for code in (401, 403, 404, 410):
            with self.subTest(code=code):
                self.setup_responses[f"/repos/{self.target.repository}/actions/jobs/91"] = (
                    MergeTrainGitHubError("optional evidence unavailable", status_code=code)
                )
                self.assertNotEqual(
                    self.advance(mutate=True)["controller_action"], "recover_candidate_block"
                )
        self.snapshot = self.snapshot.model_copy(
            update={"pull_requests": (self.current, self.other)}
        )
        self.assertEqual(self.advance()["controller_action"], "plan_candidate")
        self.transport.token_permissions = frozenset({"checks:read"})
        self.transport.repository_private = True
        self.transport.request.reset_mock()
        self.assertIsNone(self.client.read_setup_failure_recovery(candidate=self.record.candidate))
        self.transport.request.assert_not_called()
        self.assertEqual(self.mutations, [])

    def test_causal_reader_accepts_captured_incident_evidence(self) -> None:
        fixture = json.loads(
            (Path(__file__).parent / "fixtures/merge_train_missing_pnpm_store.json").read_text()
        )
        prefix = f"/repos/{fixture['repository']}"
        job = fixture["job"]
        candidate = self.record.candidate.model_copy(
            update={
                "repository": fixture["repository"],
                "candidate_sha": fixture["candidate_sha"],
                "base_sha": fixture["base_sha"],
                "entries": (
                    self.record.candidate.entries[0].model_copy(
                        update={"head_sha": fixture["head_sha"]}
                    ),
                ),
            }
        )
        self.setup_responses = {
            f"{prefix}/commits/{fixture['candidate_sha']}/check-runs?per_page=100&page=1&filter=all": fixture[
                "checks"
            ],
            f"{prefix}/commits/{fixture['candidate_sha']}/status?per_page=100&page=1": fixture[
                "statuses"
            ],
            f"{prefix}/actions/jobs/{job['id']}": job,
            f"{prefix}/actions/jobs/{job['id']}/logs": fixture["log_excerpt"],
            f"{prefix}/actions/runs/{job['run_id']}/attempts/{job['run_attempt']}/jobs?per_page=100&page=1": fixture[
                "jobs"
            ],
            f"{prefix}/compare/{fixture['base_sha']}...{fixture['head_sha']}": fixture[
                "comparison"
            ],
        }
        proof = self.client.read_setup_failure_recovery(candidate=candidate)
        self.assertIsNotNone(proof)
        assert proof is not None
        self.assertEqual(proof.job_id, job["id"])
        self.assertEqual(proof.head_sha, fixture["head_sha"])

    def test_setup_recovery_rechecks_current_head_and_persists_before_delete(self) -> None:
        self.setup_missing_store_failure()
        original = self.current
        for changes in (
            {"required_checks_status": "pending"},
            {"required_checks_status": "fail"},
            {"required_checks_status": "unknown"},
            {"is_draft": True},
        ):
            with self.subTest(changes=changes):
                self.current = original.model_copy(update=changes)
                self.assertNotEqual(
                    self.advance(mutate=True)["controller_action"], "recover_candidate_block"
                )
        self.current = original
        with patch.object(
            self.store,
            "write_merge_train_pr_feedback_record",
            side_effect=OSError("disk unavailable"),
        ):
            self.assertNotEqual(
                self.advance(mutate=True)["controller_action"], "recover_candidate_block"
            )
        self.assertEqual(self.mutations, [])

    def test_same_head_setup_recovery_keeps_code_unknown_and_admin_holds(self) -> None:
        self.setup_missing_store_failure()
        prefix = f"/repos/{self.target.repository}"
        compare = f"{prefix}/compare/{self.record.candidate.base_sha}...{self.old.head_sha}"
        originals = self.setup_responses.copy()
        for path, replacement in (
            (f"{prefix}/actions/jobs/91/logs", "ERR_PNPM_OUTDATED_LOCKFILE"),
            (
                f"{prefix}/actions/jobs/91/logs",
                "ERR_PNPM_ENOENT copyfile '/work/source.js' -> '/work/node_modules/file'",
            ),
            (compare, {**originals[compare], "files": [{"filename": "frontend/pnpm-lock.yaml"}]}),
            (
                compare,
                {
                    **originals[compare],
                    "files": [
                        {"filename": "tests/a.py", "previous_filename": "frontend/package.json"}
                    ],
                },
            ),
            (compare, {**originals[compare], "files": []}),
            (f"{prefix}/actions/jobs/91", {**self.job, "head_sha": "d" * 40}),
            (
                f"{prefix}/actions/jobs/91",
                {**self.job, "steps": [{"name": "Run browser smoke", "conclusion": "failure"}]},
            ),
        ):
            with self.subTest(path=path, replacement=replacement):
                self.setup_responses = {**originals, path: replacement}
                self.assertNotEqual(
                    self.advance(mutate=True)["controller_action"], "recover_candidate_block"
                )
                self.assertEqual(self.mutations, [])
        self.setup_responses = originals
        self.events = [self.label_event, {**self.label_event, "id": 51, "actor": {"id": 7}}]
        self.assertNotEqual(
            self.advance(mutate=True)["controller_action"], "recover_candidate_block"
        )
        self.assertEqual(self.mutations, [])

    def advance(self, *, mutate: bool = False) -> dict[str, Any]:
        return _advance_from_live_snapshot(
            request=MergeTrainControllerRunOnceEnvelope(
                repository=self.target.repository, mutate=mutate
            ),
            policy=self.policy,
            policy_sha256=self.policy_digest,
            transport=self.transport,
            github_client=self.client,
            candidate_store=self.store,
            stack_collapse_store=self.store,
            trace_id="recovery",
            recorded_at="2026-10-18T00:04:00Z",
            lease=self.lease,
            snapshot=self.snapshot,
        )

    def test_unconfirmed_abandoned_intent_cannot_claim_a_later_label(self) -> None:
        assert self.record.block_intent is not None
        self.store.write_merge_train_batch_candidate_record(
            self.record.model_copy(
                update={
                    "block_intent": self.record.block_intent.model_copy(
                        update={"applied_label_event_id": None}
                    )
                }
            )
        )
        self.assertNotEqual(self.advance()["controller_action"], "recover_candidate_block")
        self.assertEqual(self.mutations, [])

    def test_dry_run_finds_reviewed_green_new_head_without_requiring_ready(self) -> None:
        result = self.advance()
        self.assertEqual(result["controller_action"], "recover_candidate_block")
        self.assertEqual(self.mutations, [])
        self.assertEqual(result["details"]["blocked_head_sha"], self.old.head_sha)
        self.assertEqual(result["details"]["head_sha"], self.current.head_sha)

    def test_mutation_removes_only_the_proven_old_block(self) -> None:
        result = self.advance(mutate=True)
        self.assertEqual(result["controller_action"], "recover_candidate_block")
        self.assertEqual(len(self.mutations), 1)
        self.assertTrue(
            self.mutations[0].endswith(f"/labels/{quote(self.target.blocked_label, safe='')}")
        )
        self.lease.checkpoint.assert_called()

    def test_same_head_and_unready_heads_keep_block(self) -> None:
        for changes in (
            {"head_sha": self.old.head_sha},
            {"required_checks_status": "pending"},
            {"required_checks_status": "fail"},
            {"required_checks_status": "unknown"},
            {"mergeable": "conflicting"},
            {"is_draft": True},
        ):
            with self.subTest(changes=changes):
                current = self.current
                self.current = current.model_copy(update=changes)
                self.snapshot = self.snapshot.model_copy(update={"pull_requests": (self.current,)})
                self.assertNotEqual(
                    self.advance(mutate=True)["controller_action"], "recover_candidate_block"
                )
                self.assertEqual(self.mutations, [])
                self.current = current

    def test_missing_stale_unweighed_or_renewed_review_keeps_block(self) -> None:
        receipt = self.comments[0]
        for comments in (
            [],
            [{**receipt, "body": f"Review result: weighed\nHead: {self.old.head_sha}\n\nDone."}],
            [{**receipt, "body": "Review of record: Anthropic, no findings."}],
            [
                receipt,
                {
                    **receipt,
                    "id": 102,
                    "body": "Supervisor runs the review",
                    "created_at": "2026-10-18T00:03:30Z",
                },
            ],
            [{**receipt, "user": {"id": 999, "login": "outsider"}}],
        ):
            with self.subTest(comments=comments):
                self.comments = comments
                self.assertNotEqual(
                    self.advance(mutate=True)["controller_action"], "recover_candidate_block"
                )
                self.assertEqual(self.mutations, [])

    def test_manual_or_reapplied_label_is_preserved(self) -> None:
        original = dict(self.label_event)
        for event in (
            {**original, "actor": {"id": 999, "login": "owner"}},
            {
                **original,
                "performed_via_github_app": {"id": 1},
                "actor": {"id": 42, "type": "User"},
            },
        ):
            with self.subTest(event=event):
                self.label_event = event
                self.assertNotEqual(
                    self.advance(mutate=True)["controller_action"], "recover_candidate_block"
                )
                self.assertEqual(self.mutations, [])

    def test_absent_durable_block_receipt_does_not_clear_generic_label(self) -> None:
        self.record = self.record.model_copy(update={"block_intent": None})
        self.store.write_merge_train_batch_candidate_record(self.record)
        self.store.write_merge_train_pr_feedback_record(
            self.feedback.model_copy(update={"event": "waiting"})
        )
        self.assertNotEqual(
            self.advance(mutate=True)["controller_action"], "recover_candidate_block"
        )
        self.assertEqual(self.mutations, [])

    def test_head_or_label_change_during_final_recheck_refuses_before_delete(self) -> None:
        for change in ("head", "label"):
            with self.subTest(change=change):
                current = self.current
                event = dict(self.label_event)

                def checkpoint(**_: object) -> None:
                    if change == "head":
                        self.current = current.model_copy(update={"head_sha": "c" * 40})
                    else:
                        self.label_event = {**event, "id": 51}

                self.lease.checkpoint.side_effect = checkpoint
                self.assertNotEqual(
                    self.advance(mutate=True)["controller_action"], "recover_candidate_block"
                )
                self.assertEqual(self.mutations, [])
                self.current, self.label_event = current, event

    def test_same_timestamp_remove_and_reapply_is_a_new_hold(self) -> None:
        self.events = [
            self.label_event,
            {**self.label_event, "id": 51, "event": "unlabeled"},
            {**self.label_event, "id": 52},
        ]
        self.advance(mutate=True)
        self.assertEqual(self.mutations, [])

    def test_provider_denial_is_not_retried(self) -> None:
        from control_plane.merge_train_github import MergeTrainGitHubError

        requests = self.request
        attempts = 0

        def denied(**kwargs: Any) -> object:
            nonlocal attempts
            if kwargs["method"] == "DELETE":
                attempts += 1
                raise MergeTrainGitHubError("denied", status_code=403)
            return requests(**kwargs)

        self.transport.request.side_effect = denied
        with self.assertRaises(MergeTrainGitHubError):
            self.advance(mutate=True)
        self.assertEqual(attempts, 1)

    def test_lost_delete_reply_is_observed_without_replaying_removal(self) -> None:
        requests = self.request

        def lost_reply(**kwargs: Any) -> object:
            result = requests(**kwargs)
            if kwargs["method"] == "DELETE":
                raise TimeoutError("reply lost")
            return result

        self.transport.request.side_effect = lost_reply
        with self.assertRaises(TimeoutError):
            self.advance(mutate=True)
        self.snapshot = self.snapshot.model_copy(update={"pull_requests": (self.current,)})
        self.transport.request.side_effect = requests
        self.assertNotEqual(
            self.advance(mutate=True)["controller_action"], "recover_candidate_block"
        )
        self.assertEqual(len(self.mutations), 1)

    def test_unmatched_legacy_feedback_cannot_clear_a_hold(self) -> None:
        self.record = self.record.model_copy(update={"block_intent": None})
        self.store.write_merge_train_batch_candidate_record(self.record)
        for update in ({"controller_record_id": "unrelated"},):
            with self.subTest(update=update):
                self.store.write_merge_train_pr_feedback_record(
                    self.feedback.model_copy(update=update)
                )
                self.assertNotEqual(
                    self.advance(mutate=True)["controller_action"], "recover_candidate_block"
                )
                self.assertEqual(self.mutations, [])

    def test_binding_equivalent_policy_publish_preserves_recovery(self) -> None:
        other = build_test_merge_train_policy(repository="example/other").policies[0]
        self.policy = self.policy.model_copy(update={"policies": (*self.policy.policies, other)})
        after = MergeTrainPolicyRecord(
            record_id="after", source="test", updated_at="2026-10-18T00:04:00Z", policy=self.policy
        )
        self.store.write_merge_train_policy_record(after)
        self.policy_digest = after.policy_sha256
        self.assertEqual(self.advance()["controller_action"], "recover_candidate_block")

    def test_execution_policy_change_does_not_strand_the_recorded_block(self) -> None:
        self.target = self.target.model_copy(update={"enqueue_label": "different-enqueue"})
        self.policy = self.policy.model_copy(update={"policies": (self.target,)})
        after = MergeTrainPolicyRecord(
            record_id="after", source="test", updated_at="2026-10-18T00:04:00Z", policy=self.policy
        )
        self.store.write_merge_train_policy_record(after)
        self.policy_digest = after.policy_sha256
        self.assertEqual(self.advance()["controller_action"], "recover_candidate_block")

    def test_changed_block_label_is_a_different_hold(self) -> None:
        self.target = self.target.model_copy(update={"blocked_label": "different-hold"})
        self.policy = self.policy.model_copy(update={"policies": (self.target,)})
        self.current = self.current.model_copy(update={"labels": (self.target.blocked_label,)})
        self.snapshot = self.snapshot.model_copy(update={"pull_requests": (self.current,)})
        self.assertNotEqual(self.advance()["controller_action"], "recover_candidate_block")
        self.assertEqual(self.mutations, [])

    def test_saved_writer_app_survives_rotation_but_another_app_does_not_qualify(self) -> None:
        app = self.target.github_token.github_app
        intent = self.record.block_intent
        assert app is not None and intent is not None
        self.record = self.record.model_copy(
            update={"block_intent": intent.model_copy(update={"writer_app_id": app.app_id})}
        )
        self.store.write_merge_train_batch_candidate_record(self.record)
        self.target = self.target.model_copy(
            update={
                "github_token": self.target.github_token.model_copy(
                    update={"github_app": app.model_copy(update={"app_id": app.app_id + 1})}
                )
            }
        )
        self.policy = self.policy.model_copy(update={"policies": (self.target,)})
        self.assertEqual(self.advance()["controller_action"], "recover_candidate_block")
        self.record = self.record.model_copy(
            update={"block_intent": intent.model_copy(update={"writer_app_id": app.app_id + 1})}
        )
        self.store.write_merge_train_batch_candidate_record(self.record)
        self.assertNotEqual(self.advance()["controller_action"], "recover_candidate_block")

    def test_current_authority_still_applies_after_policy_change(self) -> None:
        self.target = self.target.model_copy(
            update={
                "enqueue": self.target.enqueue.model_copy(
                    update={
                        "trusted_automation_github_user_ids": (),
                        "allowed_actor_roles": ("repo_owner",),
                    }
                )
            }
        )
        self.policy = self.policy.model_copy(update={"policies": (self.target,)})
        self.current = self.current.model_copy(update={"actor_id": 999, "actor_role": "unknown"})
        self.snapshot = self.snapshot.model_copy(update={"pull_requests": (self.current,)})
        self.assertNotEqual(self.advance()["controller_action"], "recover_candidate_block")
        self.assertEqual(self.mutations, [])

    def test_confirmed_writer_needs_no_old_app_read_after_rotation(self) -> None:
        intent = self.record.block_intent
        assert intent is not None
        self.record = self.record.model_copy(
            update={
                "block_intent": intent.model_copy(
                    update={
                        "writer_app_id": 7,
                        "applied_label_actor_id": self.label_event["actor"]["id"],
                    }
                )
            }
        )
        self.store.write_merge_train_batch_candidate_record(self.record)
        requests = self.request

        def no_old_app(**kwargs: Any) -> object:
            if kwargs["path"].startswith("/apps/"):
                raise AssertionError("confirmed writer must not need old App metadata")
            return requests(**kwargs)

        self.transport.request.side_effect = no_old_app
        self.assertEqual(self.advance()["controller_action"], "recover_candidate_block")
        self.label_event["actor"] = {"id": 777, "login": "other[bot]", "type": "Bot"}
        self.assertNotEqual(self.advance()["controller_action"], "recover_candidate_block")

    def test_legacy_block_without_closed_batch_chronology_stays_held(self) -> None:
        self.record = self.record.model_copy(update={"block_intent": None})
        self.store.write_merge_train_batch_candidate_record(self.record)
        with patch(
            "control_plane.merge_train_batch_pull_request.bound_closed_batch_pull_request",
            return_value=None,
        ):
            self.assertNotEqual(self.advance()["controller_action"], "recover_candidate_block")
        self.assertEqual(self.mutations, [])

    def test_confirmation_rechecks_a_transient_absent_provider_snapshot(self) -> None:
        from control_plane.merge_train_controller_run_once import _confirm_candidate_block

        self.client.candidate_block_application = Mock(  # type: ignore[method-assign]
            side_effect=[
                {"status": "absent"},
                {"status": "applied", "event_id": self.label_event["id"]},
            ]
        )
        self.assertEqual(
            _confirm_candidate_block(
                record=self.record,
                candidate_store=self.store,
                github_client=self.client,
                repository_policy=self.target,
                lease=self.lease,
            ),
            self.record,
        )

    def test_new_intent_recovery_does_not_scan_policy_history(self) -> None:
        published = self.policy.model_copy(
            update={"policies": (self.target.model_copy(update={"enqueue_label": "new-ready"}),)}
        )
        self.policy_digest = MergeTrainPolicyRecord(
            record_id="after", source="test", updated_at="2026-10-18T00:04:00Z", policy=published
        ).policy_sha256
        self.store.list_merge_train_policy_records = Mock(side_effect=AssertionError)  # type: ignore[method-assign]
        self.assertEqual(self.advance()["controller_action"], "recover_candidate_block")

    def test_renewed_allowed_review_request_on_behalf_of_user_preserves_hold(self) -> None:
        self.comments.append(
            {
                **self.comments[0],
                "id": 102,
                "body": "Supervisor runs the review",
                "user": {"id": 42, "login": "helper", "type": "User"},
                "performed_via_github_app": {"id": 7},
                "created_at": "2026-10-18T00:04:00Z",
            }
        )
        self.assertNotEqual(self.advance()["controller_action"], "recover_candidate_block")
        self.assertEqual(self.mutations, [])

    def test_legacy_workflow_or_undelivered_feedback_recovers_without_a_comment(self) -> None:
        self.record = self.record.model_copy(
            update={"block_intent": None, "updated_at": "2099-01-01T00:00:00Z"}
        )
        self.store.write_merge_train_batch_candidate_record(self.record)
        self.feedback = self.feedback.model_copy(
            update={
                "source": "workflow:merge-train-runner",
                "delivery_status": "failed",
                "comment_id": 0,
                "recorded_at": "2000-01-01T00:00:00Z",
            }
        )
        self.store.write_merge_train_pr_feedback_record(self.feedback)
        with patch(
            "control_plane.merge_train_batch_pull_request.bound_closed_batch_pull_request",
            return_value={"closed_at": "2026-10-18T00:01:00Z"},
        ):
            self.assertEqual(self.advance()["controller_action"], "recover_candidate_block")

    def test_review_ordering_matches_the_catalog_and_client_wait_is_independent(self) -> None:
        receipt = self.comments[0]
        for comments in (
            [{**receipt, "updated_at": "2026-10-18T00:04:00Z"}],
            [
                {
                    **receipt,
                    "id": 100,
                    "created_at": "2026-10-18T00:02:00Z",
                    "updated_at": "2026-10-18T00:04:00Z",
                    "body": "Supervisor runs the review",
                },
                receipt,
            ],
            [
                receipt,
                {
                    **receipt,
                    "id": 102,
                    "body": "Review request for some documentation",
                    "created_at": "2026-10-18T00:04:00Z",
                },
            ],
            [
                receipt,
                {
                    **receipt,
                    "id": 102,
                    "body": "Engineering ready; waiting for the Client's acceptance",
                    "created_at": "2026-10-18T00:04:00Z",
                },
            ],
        ):
            with self.subTest(comments=comments):
                self.comments = comments
                self.assertEqual(self.advance()["controller_action"], "recover_candidate_block")

    def test_review_helpers_use_existing_ready_handoff_authority(self) -> None:
        receipt = {**self.comments[0], "user": {"id": 44, "login": "helper"}}
        self.comments = [receipt]
        request = self.request

        def as_admin(**kwargs: Any) -> object:
            if "/collaborators/helper/permission" in kwargs["path"]:
                return {"permission": "admin"}
            return request(**kwargs)

        self.transport.request.side_effect = as_admin
        self.assertEqual(self.advance()["controller_action"], "recover_candidate_block")
        self.comments = [
            self.comments[0],
            {
                **receipt,
                "id": 102,
                "body": "Supervisor runs the review",
                "created_at": "2026-10-18T00:04:00Z",
            },
        ]
        self.assertNotEqual(self.advance()["controller_action"], "recover_candidate_block")
        self.comments = [receipt]
        self.transport.request.side_effect = request
        self.assertNotEqual(self.advance()["controller_action"], "recover_candidate_block")
        self.assertEqual(self.mutations, [])

    def test_client_acceptance_can_settle_before_recovery_without_another_review_receipt(
        self,
    ) -> None:
        self.comments.append(
            {
                **self.comments[0],
                "id": 102,
                "body": "Engineering ready; waiting for the Client's acceptance",
                "created_at": "2026-10-18T00:04:00Z",
            }
        )
        self.current = self.current.model_copy(
            update={"owner_review_required": True, "required_checks_status": "pending"}
        )
        self.assertNotEqual(self.advance()["controller_action"], "recover_candidate_block")
        self.current = self.current.model_copy(update={"required_checks_status": "pass"})
        self.assertEqual(self.advance()["controller_action"], "recover_candidate_block")

    def test_unavailable_provider_evidence_preserves_hold_without_stalling_queue(self) -> None:
        from control_plane.merge_train_github import MergeTrainGitHubError

        requests = self.request
        next_pr = self.old.model_copy(
            update={"number": 3, "head_sha": "c" * 40, "required_checks_status": "pending"}
        )
        self.snapshot = self.snapshot.model_copy(update={"pull_requests": (self.current, next_pr)})
        for status in (404, 500, "timeout"):
            with self.subTest(status=status):

                def unavailable(**kwargs: Any) -> object:
                    if "/events?" in kwargs["path"]:
                        if isinstance(status, int):
                            raise MergeTrainGitHubError("unavailable", status_code=status)
                        raise TimeoutError("read timed out")
                    return requests(**kwargs)

                self.transport.request.side_effect = unavailable
                result = self.advance()
                self.assertEqual(result["controller_action"], "wait_for_checks")
                self.assertEqual(result["dry_run_result"]["selected_pr"]["number"], next_pr.number)
                self.assertEqual(self.mutations, [])

    def test_real_block_producer_preserves_intent_across_a_lost_label_reply(self) -> None:
        self._exercise_real_block_producer(lose_label_reply=True)

    def test_successful_block_write_survives_lagging_absence_without_holding_other_work(
        self,
    ) -> None:
        self._exercise_real_block_producer(lose_label_reply=False)

    def test_existing_label_does_not_create_a_successful_write_receipt(self) -> None:
        from control_plane.merge_train_controller_run_once import _block_failed_candidate_culprit

        held = self.old.model_copy(update={"labels": (self.target.blocked_label,)})
        dry = build_merge_train_dry_run_result(
            policy=self.policy,
            snapshot=self.snapshot.model_copy(update={"pull_requests": (held,)}),
            batch_landing=True,
        )
        before = self.store.list_merge_train_batch_candidate_records()
        result = _block_failed_candidate_culprit(
            candidate_store=self.store,
            active_candidate_record=self.record,
            github_client=self.client,
            dry_run_result=dry,
            details={},
            repository=self.target.repository,
            base_branch=self.target.base_branch,
            mutate=True,
            lease=self.lease,
            repository_policy=self.target,
        )
        assert result is not None
        self.assertNotIn("merge_train_batch_candidate_record_id", result)
        self.assertEqual(self.store.list_merge_train_batch_candidate_records(), before)
        self.transport.request.assert_not_called()

    def _exercise_real_block_producer(self, *, lose_label_reply: bool) -> None:
        from control_plane.merge_train_controller_run_once import (
            _block_failed_candidate_culprit,
            execute_merge_train_controller_with_client,
            MergeTrainControllerLeaseContext,
            MERGE_TRAIN_CONTROLLER_ACTIVE_ACTION,
        )

        self.record = self.record.model_copy(update={"block_intent": None})
        self.store.write_merge_train_batch_candidate_record(self.record)
        fixed = self.current
        self.current = self.old
        self.snapshot = self.snapshot.model_copy(update={"pull_requests": (self.old,)})
        self.events = []
        # The App's label writer need not be on the unrelated enqueue allowlist.
        self.label_event["actor"] = {"id": 777, "login": "train[bot]", "type": "Bot"}
        requests = self.request
        posts = 0

        def lost_reply(**kwargs: Any) -> object:
            nonlocal posts
            if kwargs["method"] == "POST" and kwargs["path"].endswith("/labels"):
                posts += 1
                intents = [
                    r
                    for r in self.store.list_merge_train_batch_candidate_records(
                        status="superseded"
                    )
                    if r.block_intent is not None
                ]
                self.assertEqual(len(intents), 1)
                self.assertEqual(intents[0].candidate.entries[0].head_sha, self.old.head_sha)
                if lose_label_reply:
                    self.events = [self.label_event]
                    self.current = self.current.model_copy(
                        update={"labels": (self.target.blocked_label,)}
                    )
                    raise TimeoutError("label reply lost before feedback")
                # The write succeeds, while both event and PR-detail reads
                # remain absent through the producer's two observations.
                return []
            return requests(**kwargs)

        self.transport.request.side_effect = lost_reply
        dry = build_merge_train_dry_run_result(
            policy=self.policy, snapshot=self.snapshot, batch_landing=True
        )
        state = self.store.acquire_merge_train_controller_state_record(
            repository=self.target.repository,
            base_branch=self.target.base_branch,
            policy_key=self.target.policy_key,
            policy_sha256=self.policy_digest,
            lease_owner="block-owner",
            lease_seconds=300,
            initial_active_action=MERGE_TRAIN_CONTROLLER_ACTIVE_ACTION,
            initial_active_phase="select_next_action",
            adoptable_active_actions=(MERGE_TRAIN_CONTROLLER_ACTIVE_ACTION,),
        )
        lease = MergeTrainControllerLeaseContext(record=state, record_store=self.store)
        failed_check_name = "integration"
        failure_details: dict[str, object] = {
            "failed_checks": [{"name": failed_check_name, "conclusion": "failure"}]
        }

        def produce() -> Any:
            return _block_failed_candidate_culprit(
                candidate_store=self.store,
                active_candidate_record=self.record,
                github_client=self.client,
                dry_run_result=dry,
                details=failure_details,
                repository=self.target.repository,
                base_branch=self.target.base_branch,
                mutate=True,
                lease=lease,
                repository_policy=self.target,
            )

        if lose_label_reply:
            with self.assertRaises(TimeoutError):
                produce()
            lease.release(
                reconciliation_status="required",
                reconciliation_detail="lost label reply",
                clear_active_state=False,
            )
        else:
            produced = produce()
            self.assertNotIn("controller_reconciliation_status", produced)
            from control_plane.merge_train_controller_feedback import build_feedback_payloads

            feedback = build_feedback_payloads(response={"result": produced})[0]
            self.assertEqual(feedback["event"], "waiting")
            self.assertIn(failed_check_name, str(feedback["message"]))
            block = MergeTrainBlockResult.model_validate(produced["block_result"])
            self.assertEqual(block.pull_request_number, self.old.number)
            lease.release()
        self.current = fixed
        self.snapshot = self.snapshot.model_copy(update={"pull_requests": (fixed,)})
        self.transport.request.side_effect = requests
        self.store = FilesystemRecordStore(Path(self.directory.name))
        self.client.read_merge_train_snapshot = Mock(side_effect=lambda **_: self.snapshot)  # type: ignore[method-assign]

        def run(trace: str) -> Any:
            return execute_merge_train_controller_with_client(
                request=MergeTrainControllerRunOnceEnvelope(
                    repository=self.target.repository, mutate=True
                ),
                policy=self.policy,
                policy_sha256=self.policy_digest,
                repository_policy=self.target,
                github_client=self.client,
                trace_id=trace,
                recorded_at="2026-10-18T00:04:00Z",
                candidate_store=self.store,
                landing_store=self.store,
                stack_collapse_store=self.store,
                controller_state_store=self.store,
                admission_store=self.store,
                admission_evaluator=Mock(),
            )

        from control_plane.merge_train_controller_feedback import build_feedback_payloads

        # Delayed event visibility is a bounded, visible native hold. It never
        # repeats POST, and later observation resumes the exact saved intent.
        self.events = []
        pending = run("pending-label")
        if lose_label_reply:
            self.assertEqual(
                pending.accepted_result["controller_reconciliation_status"], "required"
            )
            self.assertEqual(
                build_feedback_payloads(
                    response={"result": pending.accepted_result, "records": pending.records}
                )[0]["event"],
                "waiting",
            )
        else:
            next_pr = self.old.model_copy(
                update={"number": 3, "head_sha": "c" * 40, "required_checks_status": "pending"}
            )
            self.snapshot = self.snapshot.model_copy(update={"pull_requests": (fixed, next_pr)})
            pending = run("unrelated-queue")
            self.assertEqual(pending.accepted_result["controller_action"], "wait_for_checks")
            self.assertEqual(
                pending.accepted_result["dry_run_result"]["selected_pr"]["number"], next_pr.number
            )
        self.events = [self.label_event]
        if lose_label_reply:
            confirmed = run("confirm-label").accepted_result
            self.assertEqual(confirmed["controller_action"], "block")
            self.assertEqual(confirmed["details"], failure_details)
        self.assertEqual(
            run("recover-new-head").accepted_result["controller_action"], "recover_candidate_block"
        )
        self.assertEqual(posts, 1)
        self.assertEqual(len(self.mutations), 1)

    def test_legacy_feedback_producer_agrees_with_recovery_after_deferred_delivery(self) -> None:
        from types import SimpleNamespace
        from control_plane.merge_train_controller_run_once import _block_failed_candidate_culprit
        from control_plane.merge_train_controller_feedback import build_feedback_payloads
        from control_plane.merge_train_pr_feedback import (
            MergeTrainPrFeedbackEnvelope,
            build_merge_train_pr_feedback_record,
        )

        self.record = self.record.model_copy(update={"block_intent": None})
        self.store.write_merge_train_batch_candidate_record(self.record)
        old_snapshot = self.snapshot.model_copy(update={"pull_requests": (self.old,)})
        dry = build_merge_train_dry_run_result(
            policy=self.policy, snapshot=old_snapshot, batch_landing=True
        )
        legacy_client: Any = SimpleNamespace(add_pull_request_label=lambda **_: None)
        result = _block_failed_candidate_culprit(
            candidate_store=self.store,
            active_candidate_record=self.record,
            github_client=legacy_client,
            dry_run_result=dry,
            details={},
            repository=self.target.repository,
            base_branch=self.target.base_branch,
            mutate=True,
            lease=self.lease,
            repository_policy=self.target,
        )
        assert result is not None
        payloads = build_feedback_payloads(
            response={
                "result": result,
                "records": {"merge_train_batch_candidate_record_id": self.record.record_id},
            },
            source="workflow:merge-train-runner",
        )
        record = build_merge_train_pr_feedback_record(
            request=MergeTrainPrFeedbackEnvelope.model_validate(payloads[0]),
            policy_key=self.target.policy_key,
            policy_sha256=self.policy_digest,
            token="fixture-token",
            recorded_at="2026-10-18T00:04:00Z",
            response_trace_id="fixture",
            defer_until="2026-10-18T00:05:00Z",
        )
        self.store.write_merge_train_pr_feedback_record(record)
        with patch(
            "control_plane.merge_train_batch_pull_request.bound_closed_batch_pull_request",
            return_value={"closed_at": "2026-10-18T00:01:00Z"},
        ):
            self.assertEqual(self.advance()["controller_action"], "recover_candidate_block")

    def test_native_lease_resumes_lost_removal_with_feedback_without_another_delete(self) -> None:
        from control_plane.merge_train_controller_run_once import (
            execute_merge_train_controller_with_client,
        )
        from control_plane.merge_train_controller_feedback import build_feedback_payloads

        self.client.read_merge_train_snapshot = Mock(side_effect=lambda **_: self.snapshot)  # type: ignore[method-assign]
        requests = self.request

        def lost_reply(**kwargs: Any) -> object:
            result = requests(**kwargs)
            if kwargs["method"] == "DELETE":
                raise TimeoutError("reply lost")
            return result

        self.transport.request.side_effect = lost_reply
        feedback: list[dict[str, object]] = []

        def run(trace: str) -> Any:
            return execute_merge_train_controller_with_client(
                request=MergeTrainControllerRunOnceEnvelope(
                    repository=self.target.repository, mutate=True
                ),
                policy=self.policy,
                policy_sha256=self.policy_digest,
                repository_policy=self.target,
                github_client=self.client,
                trace_id=trace,
                recorded_at="2026-10-18T00:04:00Z",
                candidate_store=self.store,
                landing_store=self.store,
                stack_collapse_store=self.store,
                controller_state_store=self.store,
                admission_store=self.store,
                admission_evaluator=Mock(),
                before_release=lambda result: feedback.extend(
                    build_feedback_payloads(
                        response={"result": result.accepted_result, "records": result.records}
                    )
                ),
            )

        with self.assertRaises(TimeoutError):
            run("lost-removal")
        self.store = FilesystemRecordStore(Path(self.directory.name))
        self.transport.request.side_effect = requests
        self.snapshot = self.snapshot.model_copy(update={"pull_requests": (self.current,)})
        result = run("resume-removal")
        self.assertEqual(result.accepted_result["controller_action"], "recover_candidate_block")
        self.assertIn("removal_observation", result.accepted_result["details"])
        self.assertEqual(len(self.mutations), 1)
        self.assertEqual(feedback[0]["event"], "waiting")

    def test_native_lease_requalifies_a_still_present_label_after_lost_removal(self) -> None:
        from control_plane.merge_train_controller_run_once import (
            execute_merge_train_controller_with_client,
        )

        self.client.read_merge_train_snapshot = Mock(side_effect=lambda **_: self.snapshot)  # type: ignore[method-assign]
        requests = self.request

        def lost_before_effect(**kwargs: Any) -> object:
            if kwargs["method"] == "DELETE":
                raise TimeoutError("request interrupted")
            return requests(**kwargs)

        self.transport.request.side_effect = lost_before_effect

        def run(trace: str) -> Any:
            return execute_merge_train_controller_with_client(
                request=MergeTrainControllerRunOnceEnvelope(
                    repository=self.target.repository, mutate=True
                ),
                policy=self.policy,
                policy_sha256=self.policy_digest,
                repository_policy=self.target,
                github_client=self.client,
                trace_id=trace,
                recorded_at="2026-10-18T00:04:00Z",
                candidate_store=self.store,
                landing_store=self.store,
                stack_collapse_store=self.store,
                controller_state_store=self.store,
                admission_store=self.store,
                admission_evaluator=Mock(),
            )

        with self.assertRaises(TimeoutError):
            run("uncertain-removal")
        self.assertEqual(self.mutations, [])
        self.transport.request.side_effect = requests
        self.current = self.current.model_copy(update={"required_checks_status": "pending"})
        self.snapshot = self.snapshot.model_copy(update={"pull_requests": (self.current,)})
        self.assertNotEqual(
            run("requalify-pending").accepted_result["controller_action"], "recover_candidate_block"
        )
        self.assertEqual(self.mutations, [])
        self.current = self.current.model_copy(update={"required_checks_status": "pass"})
        self.snapshot = self.snapshot.model_copy(update={"pull_requests": (self.current,)})
        self.assertEqual(
            run("requalify-green").accepted_result["controller_action"], "recover_candidate_block"
        )
        self.assertEqual(len(self.mutations), 1)

    def test_success_feedback_replaces_the_stale_block_instructions(self) -> None:
        from control_plane.merge_train_controller_feedback import build_feedback_payloads

        result = self.advance(mutate=True)
        payloads = build_feedback_payloads(response={"result": result, "records": {}})
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["pull_request_number"], self.current.number)
        self.assertEqual(payloads[0]["event"], "waiting")
        self.assertEqual(
            build_feedback_payloads(response={"result": self.advance(), "records": {}}), []
        )


if __name__ == "__main__":
    unittest.main()
