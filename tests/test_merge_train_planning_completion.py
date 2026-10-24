from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta
from email.message import Message
from io import BytesIO
from typing import Any, cast
from unittest import TestCase
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

import click
from github_plan_status import DEFAULT_STATUS_LABELS

from control_plane import merge_train_planning_completion as completion
from control_plane.merge_train_pr_feedback import (
    MergeTrainPrFeedbackEnvelope,
    build_merge_train_pr_feedback_record,
    deliver_merge_train_pr_feedback_record,
    PLANNING_RECEIPT_PREFIX,
    _PLANNING_NOTE_START,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.contracts.merge_train_pr_feedback_record import MergeTrainPrFeedbackRecord
from control_plane.contracts.merge_train_policy import MergeTrainSchedulerPolicy
from control_plane.merge_train_scheduler import _deliver_controller_feedback
from tests.test_merge_train_scheduler import _policy_record
from tests.test_merge_train_structural_provenance import _entry, _records
from control_plane import merge_train_controller_run_once as controller
from control_plane import merge_train_batch_landing as standalone
from control_plane.merge_train_controller_feedback import build_feedback_payloads
from pathlib import Path
from tempfile import TemporaryDirectory


_REPO = "example/product"
_BOT = 41
_LABELS = DEFAULT_STATUS_LABELS


def _wait(state: str = "clear") -> str:
    return (
        "<!-- github-plan:wait "
        + json.dumps(
            {
                "version": 1,
                "state": state,
                "evidence": "Reviewed source delivery",
                "waits": []
                if state == "clear"
                else [{"waiting_for": "Client", "ends_when": "Client accepts"}],
            }
        )
        + " -->"
    )


def _receipt(record: MergeTrainPrFeedbackRecord) -> dict[str, Any]:
    line = next(
        line
        for line in record.comment_markdown.splitlines()
        if line.startswith(PLANNING_RECEIPT_PREFIX)
    )
    return cast(
        dict[str, Any], json.loads(line.removeprefix(PLANNING_RECEIPT_PREFIX).removesuffix(" -->"))
    )


class PlanningProvider:
    def __init__(self) -> None:
        self.issue: dict[str, Any] = {
            "number": 7,
            "node_id": "issue7",
            "state": "closed",
            "state_reason": "completed",
            "user": {"id": _BOT, "login": "example-bot[bot]"},
            "body": "<!-- github-plan:managed-provenance -->\n" + _wait(),
            "labels": [{"name": _LABELS[key]} for key in ("plan", "active")] + [{"name": "bug"}],
        }
        self.comments: list[dict[str, Any]] = []
        self.blockers: list[dict[str, Any]] = []
        self.children: list[dict[str, Any]] = []
        self.writes: list[tuple[str, object]] = []
        self.fail_labels = False
        self.config: dict[str, Any] = {}
        self.project_status = "active"
        self.drop_project_write = False
        self.missing_project_item = False
        self.references = [7]
        self.merged = True
        self.base_branch = "main"
        self.default_branch: Any = "main"
        self.merged_at: Any = "2026-09-30T23:59:00Z"
        self.drop_label_write = False
        self.issue_reads: list[str] = []

    def request(self, *, path: str, method: str = "GET", body: Any = None, **_kwargs: Any) -> Any:
        if method == "GET" and path.startswith(f"/repos/{_REPO}/issues/"):
            self.issue_reads.append(path)
        if path == "/graphql":
            query = body["query"]
            if "closingIssuesReferences" in query:
                return {
                    "data": {
                        "repository": {
                            "defaultBranchRef": {"name": self.default_branch},
                            "object": {"text": json.dumps(self.config)},
                            "pullRequest": {
                                "merged": self.merged,
                                "mergedAt": self.merged_at,
                                "baseRefName": self.base_branch,
                                "mergeCommit": {"oid": "landed"},
                                "closingIssuesReferences": {
                                    "nodes": [
                                        {"number": number, "repository": {"nameWithOwner": _REPO}}
                                        for number in self.references
                                    ],
                                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                                },
                            },
                        }
                    }
                }
            if "updateProjectV2ItemFieldValue" in query:
                self.writes.append(("project", body))
                if not self.drop_project_write:
                    self.project_status = "done"
                return {
                    "data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "item7"}}}
                }
            if "defaultBranchRef" in query:
                return {
                    "data": {
                        "repository": {
                            "defaultBranchRef": {"name": self.default_branch},
                            "object": {"text": json.dumps(self.config)},
                        }
                    }
                }
            return {
                "data": {
                    "user": {
                        "projectV2": {
                            "id": "project",
                            "fields": {
                                "nodes": [
                                    {
                                        "id": "status",
                                        "name": "Status",
                                        "options": [{"id": "done", "name": "Done"}],
                                    }
                                ],
                                "pageInfo": {"hasNextPage": False},
                            },
                        }
                    },
                    "node": {
                        "projectItems": {
                            "nodes": []
                            if self.missing_project_item
                            else [
                                {
                                    "id": "item7",
                                    "project": {"id": "project"},
                                    "fieldValueByName": {"optionId": self.project_status},
                                }
                            ],
                            "pageInfo": {"hasNextPage": False},
                        }
                    },
                }
            }
        if path == "/users/example":
            return {"type": "User"}
        if "/comments?" in path:
            return copy.deepcopy(self.comments)
        if "/dependencies/blocked_by?" in path:
            return copy.deepcopy(self.blockers)
        if "/sub_issues?" in path:
            return copy.deepcopy(self.children)
        if "/dependencies/blocking?" in path:
            return []
        if path.endswith("/parent"):
            raise click.ClickException("not found") from HTTPError(
                path, 404, "Not Found", Message(), None
            )
        if method == "GET" and path == f"/repos/{_REPO}":
            return {"default_branch": self.default_branch}
        if method == "GET" and path == f"/repos/{_REPO}/issues/{self.issue['number']}":
            return copy.deepcopy(self.issue)
        if method == "GET":
            raise AssertionError(f"Unexpected provider read: {path}")
        self.writes.append((method, path))
        if self.fail_labels:
            error = click.ClickException("temporary provider failure")
            error.__cause__ = URLError("offline")
            raise error
        if not self.drop_label_write:
            if method == "POST":
                self.issue["labels"].extend({"name": name} for name in body["labels"])
            elif method == "DELETE":
                from urllib.parse import unquote

                name = unquote(path.rsplit("/", 1)[-1])
                self.issue["labels"] = [
                    label for label in self.issue["labels"] if label["name"] != name
                ]
        return None


class PlanningCompletionTests(TestCase):
    def setUp(self) -> None:
        self.provider = PlanningProvider()
        self.request = patch.object(
            completion, "github_api_request", side_effect=self.provider.request
        )
        self.request.start()
        self.addCleanup(self.request.stop)
        self.comment = patch(
            "control_plane.merge_train_pr_feedback.upsert_github_issue_comment",
            return_value={
                "action": "updated_comment",
                "comment_id": 3,
                "comment_url": "https://example.test/comment",
            },
        )
        self.comment_mock = self.comment.start()
        self.addCleanup(self.comment.stop)

    def record(
        self, *, recorded_at: str = "2026-10-01T00:00:00Z", base_branch: str = "main"
    ) -> MergeTrainPrFeedbackRecord:
        return build_merge_train_pr_feedback_record(
            request=MergeTrainPrFeedbackEnvelope(
                repository=_REPO,
                base_branch=base_branch,
                pull_request_number=9,
                event="completed",
                controller_action="land_batch",
            ),
            policy_key="test",
            policy_sha256="digest",
            token="fixture",
            recorded_at=recorded_at,
            response_trace_id="test",
            planning_author_ids=(_BOT,),
        )

    def test_native_completion_cleans_labels_and_persists_readback_without_driver(self) -> None:
        before = copy.deepcopy(self.provider.issue)
        record = self.record()
        self.assertEqual(record.delivery_status, "delivered")
        self.assertEqual(
            {label["name"] for label in self.provider.issue["labels"]},
            {_LABELS["plan"], _LABELS["done"], "bug"},
        )
        self.assertEqual(self.provider.issue["state"], before["state"])
        self.assertEqual(self.provider.issue["body"], before["body"])
        self.assertTrue(_receipt(record)["ok"])
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(Path(directory))
            store.write_merge_train_pr_feedback_record(record)
            saved = store.list_merge_train_pr_feedback_records(repository=_REPO)[0]
            self.assertEqual(_receipt(saved), _receipt(record))
        writes = list(self.provider.writes)
        retry = deliver_merge_train_pr_feedback_record(
            record=record,
            token="fixture",
            attempted_at="2026-10-01T00:02:00Z",
            planning_author_ids=(_BOT,),
        )
        self.assertEqual(retry.delivery_status, "delivered")
        self.assertEqual(self.provider.writes, writes)

    def test_scheduler_landing_reconciles_before_persisting_completed_feedback(self) -> None:
        policy_record = _policy_record(
            (_REPO, MergeTrainSchedulerPolicy(enabled=True, mutate=True))
        )
        policy = policy_record.policy.policies[0]
        policy = policy.model_copy(
            update={
                "enqueue": policy.enqueue.model_copy(
                    update={"trusted_automation_github_user_ids": (_BOT,)}
                )
            }
        )
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(Path(directory))
            delivered, failed = _deliver_controller_feedback(
                record_store=store,
                policy_record=policy_record,
                repository_policy=policy,
                token="fixture",
                trace_id="scheduler-test",
                now=lambda: "2026-10-01T00:00:00Z",
                response={
                    "result": {
                        "repository": _REPO,
                        "base_branch": "main",
                        "controller_action": "land_batch",
                        "landing_plan": {
                            "entries": [{"pull_request_number": 9, "status": "merged"}]
                        },
                    },
                    "records": {},
                },
            )
            self.assertEqual((delivered, failed), (1, 0))
            record = store.list_merge_train_pr_feedback_records(repository=_REPO)[0]
            self.assertTrue(_receipt(record)["ok"])
            self.assertEqual(
                {label["name"] for label in self.provider.issue["labels"]},
                {_LABELS["plan"], _LABELS["done"], "bug"},
            )

    def test_direct_controller_landing_queues_cleanup_for_idle_worker_without_caller_return(
        self,
    ) -> None:
        policy_record = _policy_record(
            (_REPO, MergeTrainSchedulerPolicy(enabled=True, mutate=True))
        )
        policy = policy_record.policy.policies[0]
        policy = policy.model_copy(
            update={
                "enqueue": policy.enqueue.model_copy(
                    update={"trusted_automation_github_user_ids": (_BOT,)}
                )
            }
        )
        _, record = _records((_entry(9, 1),), repository=_REPO)
        record = record.model_copy(
            update={
                "landing_plan": record.landing_plan.model_copy(
                    update={
                        "entries": tuple(
                            entry.model_copy(
                                update={"status": "merged", "merge_commit_sha": "landed"}
                            )
                            for entry in record.landing_plan.entries
                        )
                    }
                )
            }
        )
        lease = Mock()
        lease.record.step_payload = {}
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(Path(directory))
            building = build_merge_train_pr_feedback_record(
                request=MergeTrainPrFeedbackEnvelope(
                    repository=_REPO,
                    base_branch="main",
                    pull_request_number=9,
                    event="building",
                    controller_record_id=record.record_id,
                ),
                policy_key=policy.policy_key,
                policy_sha256=record.landing_plan.policy_sha256,
                token="",
                recorded_at=(datetime.fromisoformat(record.updated_at) + timedelta(seconds=10))
                .isoformat()
                .replace("+00:00", "Z"),
                response_trace_id="earlier-building",
            )
            store.write_merge_train_pr_feedback_record(building)
            with patch.multiple(
                controller,
                validate_merge_train_landing_record_for_controller=Mock(),
                stack_collapse_records_for_completed_landing=Mock(return_value=()),
                cleanup_merge_train_batch_candidate_ref=Mock(
                    return_value={"candidate_ref_cleanup_status": "deleted"}
                ),
                _annotate_historical_closed_stack_children=Mock(),
            ):
                for _ in range(2):
                    controller._finish_landed_merge_train_batch(
                        request=controller.MergeTrainControllerRunOnceEnvelope(
                            repository=_REPO, mutate=True
                        ),
                        policy_sha256=record.landing_plan.policy_sha256,
                        repository_policy=policy,
                        trace_id="native",
                        recorded_at=record.updated_at,
                        github_client=Mock(),
                        stack_collapse_store=store,
                        landed_record=record,
                        lease=lease,
                    )
            saved = store.list_merge_train_pr_feedback_records(repository=_REPO)
            self.assertEqual([item.event for item in saved], ["completed", "building"])
            self.assertTrue(saved[0].retryable)
            self.assertFalse(_receipt(saved[0])["ok"])
            self.assertFalse(self.provider.writes)
            self.comment_mock.assert_not_called()
            delivered, failed = _deliver_controller_feedback(
                record_store=store,
                policy_record=policy_record,
                repository_policy=policy,
                token="fixture",
                trace_id="idle",
                now=lambda: "2026-10-17T00:00:00Z",
                response={
                    "result": {
                        "repository": _REPO,
                        "base_branch": "main",
                        "controller_action": "idle",
                    }
                },
            )
            self.assertEqual((delivered, failed), (1, 0))
            saved = store.list_merge_train_pr_feedback_records(repository=_REPO)
            self.assertTrue(_receipt(saved[0])["ok"])
            self.assertNotIn(
                _LABELS["active"], {label["name"] for label in self.provider.issue["labels"]}
            )

    def test_landing_recomputes_direct_dependent_from_remaining_blockers_and_wait(self) -> None:
        for remaining, waiting, expected in (
            (False, False, "active"),
            (True, False, "blocked"),
            (False, True, "waiting"),
        ):
            with self.subTest(remaining=remaining, waiting=waiting):
                self.provider = PlanningProvider()
                dependent = copy.deepcopy(self.provider.issue)
                dependent.update(
                    number=8,
                    node_id="issue8",
                    state="open",
                    state_reason=None,
                    body="<!-- github-plan:managed-provenance -->\n"
                    + _wait("waiting" if waiting else "clear"),
                    labels=[{"name": _LABELS[key]} for key in ("plan", "blocked")],
                )
                body_before = dependent["body"]
                blocker = {
                    "number": 9,
                    "state": "open",
                    "repository_url": f"https://api.github.com/repos/{_REPO}",
                }

                def request(
                    *, path: str, method: str = "GET", body: Any = None, **kwargs: Any
                ) -> Any:
                    from urllib.parse import unquote

                    if "/issues/7/dependencies/blocking?" in path:
                        return [
                            {**dependent, "repository_url": f"https://api.github.com/repos/{_REPO}"}
                        ]
                    if path.endswith("/parent"):
                        raise click.ClickException("not found") from HTTPError(
                            path, 404, "Not Found", Message(), None
                        )
                    if "/issues/8" not in path:
                        return self.provider.request(path=path, method=method, body=body, **kwargs)
                    if "/comments?" in path or "/sub_issues?" in path:
                        return []
                    if "/dependencies/blocked_by?" in path:
                        return [blocker] if remaining else []
                    if method == "GET":
                        return copy.deepcopy(dependent)
                    if method == "POST":
                        dependent["labels"].extend({"name": name} for name in body["labels"])
                    elif method == "DELETE":
                        name = unquote(path.rsplit("/", 1)[-1])
                        dependent["labels"] = [
                            item for item in dependent["labels"] if item["name"] != name
                        ]
                    return None

                with patch.object(completion, "github_api_request", side_effect=request):
                    result = self.record()
                self.assertEqual(result.delivery_status, "delivered")
                names = {item["name"] for item in dependent["labels"]}
                self.assertEqual(
                    names & set(_LABELS.values()), {_LABELS["plan"], _LABELS[expected]}
                )
                self.assertEqual(dependent["body"], body_before)
                self.assertEqual(dependent["state"], "open")

    def test_standalone_landing_persists_completion_before_returning(self) -> None:
        policy = _policy_record(
            (_REPO, MergeTrainSchedulerPolicy(enabled=True, mutate=True))
        ).policy.policies[0]
        policy = policy.model_copy(
            update={
                "enqueue": policy.enqueue.model_copy(
                    update={"trusted_automation_github_user_ids": (_BOT,)}
                )
            }
        )
        candidate, record = _records((_entry(9, 1),), repository=_REPO)
        landed = record.landing_plan.model_copy(
            update={
                "entries": tuple(
                    entry.model_copy(update={"status": "merged", "merge_commit_sha": "landed"})
                    for entry in record.landing_plan.entries
                )
            }
        )
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(Path(directory))
            store.write_merge_train_batch_candidate_record(candidate)
            store.write_merge_train_batch_landing_plan_record(record)
            with (
                patch.object(standalone, "GitHubMergeTrainClient") as client,
                patch.object(standalone, "GuardedMergeAdmission"),
                patch.object(
                    standalone, "_cleanup_merge_train_batch_candidate_ref", return_value={}
                ),
            ):
                client.return_value.land_batch_candidate.return_value = landed
                standalone._execute_land_mode(
                    request=standalone.MergeTrainBatchLandingRunOnceEnvelope(
                        repository=_REPO, mode="land", landing_plan_record_id=record.record_id
                    ),
                    repository_policy=policy,
                    policy_sha256=candidate.candidate.policy_sha256,
                    token="fixture",
                    trace_id="standalone",
                    recorded_at=record.updated_at,
                    candidate_store=store,
                    landing_store=store,
                    stack_collapse_store=store,
                    admission_store=store,
                    admission_evaluator=Mock(),
                    controller_state_provider=Mock(),
                    mutation_checkpoint=None,
                )
            saved = store.list_merge_train_pr_feedback_records(repository=_REPO)
            self.assertEqual(len(saved), 1)
            self.assertTrue(saved[0].retryable)
            self.assertFalse(self.provider.writes)
            self.comment_mock.assert_not_called()

    def test_changed_policy_recovery_queues_verified_landing_only_in_mutate_mode(self) -> None:
        policy = _policy_record(
            (_REPO, MergeTrainSchedulerPolicy(enabled=True, mutate=True))
        ).policy.policies[0]
        policy = policy.model_copy(
            update={
                "enqueue": policy.enqueue.model_copy(
                    update={"trusted_automation_github_user_ids": (_BOT,)}
                )
            }
        )
        _, record = _records((_entry(9, 1),), repository=_REPO)
        record = record.model_copy(
            update={
                "landing_plan": record.landing_plan.model_copy(
                    update={
                        "entries": tuple(
                            entry.model_copy(
                                update={"status": "merged", "merge_commit_sha": "landed"}
                            )
                            for entry in record.landing_plan.entries
                        )
                    }
                )
            }
        )
        for mutate in (False, True):
            with self.subTest(mutate=mutate), TemporaryDirectory() as directory:
                store = FilesystemRecordStore(Path(directory))
                client, lease = Mock(), Mock()
                client.pull_request_is_merged.return_value = "landed"
                client.branch_contains_commit.return_value = True
                lease.record.step_payload = {}
                with patch.object(
                    controller, "stack_collapse_records_for_completed_landing", return_value=()
                ):
                    controller._reconcile_completed_landing_after_policy_change(
                        request=controller.MergeTrainControllerRunOnceEnvelope(
                            repository=_REPO, mutate=mutate
                        ),
                        repository_policy=policy,
                        trace_id="changed-policy",
                        recorded_at=record.updated_at,
                        github_client=client,
                        stack_collapse_store=store,
                        landed_record=record,
                        lease=lease,
                    )
                self.assertEqual(
                    bool(store.list_merge_train_pr_feedback_records(repository=_REPO)), mutate
                )
                self.assertFalse(self.provider.writes)
                self.comment_mock.assert_not_called()

    def test_configured_status_labels_and_all_stale_statuses_are_reconciled(self) -> None:
        self.provider.config = {
            "planning": {
                "labels": {
                    "plan": "work",
                    "done": "work:done",
                    "active": "work:active",
                    "blocked": "work:blocked",
                    "waiting": "work:waiting",
                    "stale": "work:stale",
                }
            }
        }
        self.provider.issue["labels"] = [
            {"name": name}
            for name in ("work", "work:active", "work:blocked", "work:waiting", "work:stale", "bug")
        ]
        self.assertEqual(self.record().delivery_status, "delivered")
        self.assertEqual(
            {label["name"] for label in self.provider.issue["labels"]}, {"work", "work:done", "bug"}
        )

    def test_native_completion_refuses_false_success_then_retries_transient_failure(self) -> None:
        self.provider.fail_labels = True
        record = self.record()
        self.assertEqual(record.delivery_status, "failed")
        self.assertTrue(record.retryable)
        self.comment_mock.assert_not_called()
        self.provider.fail_labels = False
        retry = deliver_merge_train_pr_feedback_record(
            record=record,
            token="fixture",
            attempted_at="2026-10-01T00:02:00Z",
            planning_author_ids=(_BOT,),
        )
        self.assertEqual(retry.delivery_status, "delivered")
        self.assertTrue(_receipt(retry)["ok"])
        self.assertNotIn(
            _LABELS["active"], {label["name"] for label in self.provider.issue["labels"]}
        )

    def test_retry_cannot_reuse_an_author_retired_by_current_policy(self) -> None:
        self.provider.fail_labels = True
        record = self.record()
        self.provider.fail_labels = False
        self.provider.writes.clear()
        retry = deliver_merge_train_pr_feedback_record(
            record=record,
            token="fixture",
            attempted_at="2026-10-01T00:02:00Z",
            planning_author_ids=(),
        )
        self.assertEqual(retry.delivery_status, "failed")
        self.assertFalse(retry.retryable)
        self.assertFalse(self.provider.writes)
        self.assertIn(_LABELS["active"], {label["name"] for label in self.provider.issue["labels"]})

    def test_waits_reporters_and_unfinished_native_prerequisites_are_preserved(self) -> None:
        original = copy.deepcopy(self.provider.issue)
        for change in ("open", "reporter", "waiting", "unreviewed", "blocker", "child"):
            with self.subTest(change=change):
                self.provider.issue = copy.deepcopy(original)
                self.provider.comments, self.provider.blockers, self.provider.children = [], [], []
                self.provider.writes.clear()
                if change == "open":
                    self.provider.issue["state"] = "open"
                elif change == "reporter":
                    self.provider.issue["user"]["id"] = 99
                elif change == "waiting":
                    self.provider.comments = [{"user": {"id": _BOT}, "body": _wait("waiting")}]
                elif change == "unreviewed":
                    self.provider.issue["body"] = "<!-- github-plan:managed-provenance -->"
                elif change == "blocker":
                    self.provider.blockers = [{"number": 10, "state": "open"}]
                else:
                    self.provider.children = [{"number": 11, "state": "open"}]
                before = copy.deepcopy(self.provider.issue)
                record = self.record()
                self.assertEqual(record.delivery_status, "failed")
                self.assertEqual(record.retryable, change == "open")
                self.assertEqual(self.provider.issue["body"], before["body"])
                self.assertEqual(self.provider.issue["state"], before["state"])
                if change in {"waiting", "unreviewed"}:
                    names = {item["name"] for item in self.provider.issue["labels"]}
                    self.assertIn(_LABELS["done"], names)
                    self.assertNotIn(_LABELS["active"], names)
                    self.assertEqual(_LABELS["waiting"] in names, change == "waiting")
                    self.assertFalse(_receipt(record)["ok"])
                    self.assertFalse(_receipt(record)["issues"][0]["verified"])
                else:
                    self.assertEqual(self.provider.issue, before)
                    self.assertFalse(self.provider.writes)

    def test_foreign_or_quoted_clear_does_not_resolve_a_real_wait(self) -> None:
        self.provider.issue["body"] = "<!-- github-plan:managed-provenance -->\n" + _wait("waiting")
        for author, text in (
            (99, _wait()),
            (_BOT, "```\n" + _wait() + "\n```"),
            (_BOT, "<details>\n" + _wait() + "\n</details>"),
        ):
            self.provider.comments = [{"user": {"id": author}, "body": text}]
            self.assertEqual(self.record().delivery_status, "failed")
        self.provider.comments = [{"user": {"id": _BOT}, "body": _wait()}]
        self.assertEqual(self.record().delivery_status, "delivered")

    def test_held_first_reference_does_not_leave_an_eligible_issue_stale(self) -> None:
        for reason in ("waiting", "reporter", "blocker", "open", "reopened", "project"):
            with self.subTest(reason=reason):
                self.provider = PlanningProvider()
                self.provider.references = [8, 7]
                held = PlanningProvider()
                held.issue.update(number=8, node_id="issue8")
                if reason == "waiting":
                    held.comments = [{"user": {"id": _BOT}, "body": _wait("waiting")}]
                elif reason == "reporter":
                    held.issue["user"]["id"] = 99
                elif reason == "blocker":
                    held.blockers = [{"number": 10, "state": "open"}]
                elif reason == "project":
                    self.provider.config = {"planning": {"projects": {"default_project": 2}}}
                    held.missing_project_item = True
                else:
                    held.issue["state"] = "open"
                    held.issue["state_reason"] = "reopened" if reason == "reopened" else None
                before = copy.deepcopy(held.issue)

                def request(*, path: str, **kwargs: Any) -> Any:
                    issue_node = (kwargs.get("body") or {}).get("variables", {}).get("issue")
                    provider = (
                        held
                        if path.startswith(f"/repos/{_REPO}/issues/8")
                        or issue_node == held.issue["node_id"]
                        else self.provider
                    )
                    return provider.request(path=path, **kwargs)

                self.comment_mock.reset_mock()
                with patch.object(completion, "github_api_request", side_effect=request):
                    record = self.record()
                self.assertEqual(
                    {label["name"] for label in self.provider.issue["labels"]},
                    {_LABELS["plan"], _LABELS["done"], "bug"},
                )
                self.assertEqual(held.issue["body"], before["body"])
                self.assertEqual(held.issue["state"], before["state"])
                if reason in {"waiting", "project"}:
                    names = {item["name"] for item in held.issue["labels"]}
                    self.assertIn(_LABELS["done"], names)
                    self.assertNotIn(_LABELS["active"], names)
                    self.assertEqual(_LABELS["waiting"] in names, reason == "waiting")
                    self.assertEqual(held.project_status, "active")
                else:
                    self.assertEqual(held.issue, before)
                    self.assertFalse(held.writes)
                self.assertEqual(record.delivery_status, "failed")
                self.assertEqual(record.retryable, reason in {"open", "reopened"})
                self.assertEqual(self.comment_mock.called, reason not in {"open", "reopened"})
                with TemporaryDirectory() as directory:
                    store = FilesystemRecordStore(Path(directory))
                    store.write_merge_train_pr_feedback_record(record)
                    saved = store.list_merge_train_pr_feedback_records(repository=_REPO)[0]
                    self.assertEqual(
                        {item["number"]: item["status"] for item in _receipt(saved)["issues"]},
                        {
                            8: "review_projection_pending"
                            if reason in {"open", "reopened"}
                            else "review_wait_record"
                            if reason == "waiting"
                            else "review_related_finish_line"
                            if reason == "project"
                            else "review_reporter_acceptance",
                            7: "reconciled",
                        },
                    )

    def test_failed_metadata_or_project_readback_is_never_delivered(self) -> None:
        self.provider.drop_label_write = True
        self.assertEqual(self.record().delivery_status, "failed")
        self.provider.drop_label_write = False
        self.provider.config = {"planning": {"projects": {"default_project": 2}}}
        self.provider.drop_project_write = True
        self.assertEqual(self.record().delivery_status, "failed")
        self.assertEqual(self.provider.project_status, "active")
        self.provider.drop_project_write = False
        record = self.record()
        self.assertEqual(record.delivery_status, "delivered")
        self.assertEqual(self.provider.project_status, "done")

    def test_metadata_permission_denial_stops_other_sources_without_retry(self) -> None:
        self.provider.references = [7, 8]
        other = PlanningProvider()
        other.issue.update(number=8, node_id="issue8")
        before = copy.deepcopy(other.issue)
        attempts: list[str] = []

        def request(*, path: str, method: str = "GET", **kwargs: Any) -> Any:
            if method != "GET" and path != "/graphql":
                attempts.append(path)
                error = click.ClickException("provider denied metadata write")
                error.__cause__ = HTTPError(path, 403, "Forbidden", Message(), BytesIO())
                raise error
            provider = other if path.startswith(f"/repos/{_REPO}/issues/8") else self.provider
            return provider.request(path=path, method=method, **kwargs)

        with patch.object(completion, "github_api_request", side_effect=request):
            record = self.record()
        self.assertEqual(len(attempts), 1)
        self.assertTrue(attempts[0].startswith(f"/repos/{_REPO}/issues/7/"))
        self.assertEqual(other.issue, before)
        self.assertFalse(other.writes)
        self.assertEqual(record.delivery_status, "failed")
        self.assertFalse(record.retryable)
        self.comment_mock.assert_not_called()

    def test_nonplans_and_no_closing_references_have_no_metadata_writes(self) -> None:
        self.provider.issue["labels"] = [{"name": "bug"}]
        self.assertEqual(self.record().delivery_status, "delivered")
        self.assertFalse(self.provider.writes)
        self.provider.references = []
        self.assertEqual(self.record().delivery_status, "delivered")
        self.assertFalse(self.provider.writes)

    def test_reopened_projection_has_a_bounded_pending_window(self) -> None:
        self.provider.issue.update(state="open", state_reason="reopened")
        record = self.record()
        self.assertTrue(record.retryable)
        self.assertFalse(self.provider.writes)
        self.comment_mock.assert_not_called()
        deadline = (
            datetime.fromisoformat(self.provider.merged_at.replace("Z", "+00:00"))
            + completion.PROJECTION_GRACE
        )
        held = deliver_merge_train_pr_feedback_record(
            record=record,
            token="fixture",
            attempted_at=deadline.isoformat(),
            planning_author_ids=(_BOT,),
        )
        self.assertEqual(held.delivery_status, "failed")
        self.assertFalse(held.retryable)
        self.assertFalse(_receipt(held)["ok"])
        self.assertEqual(held.comment_id, 3)
        self.assertIn(_PLANNING_NOTE_START, self.comment_mock.call_args.kwargs["body"])
        self.assertFalse(self.provider.writes)

    def test_reopened_source_can_finish_during_projection_grace(self) -> None:
        self.provider.issue.update(state="open", state_reason="reopened")
        record = self.record()
        self.assertTrue(record.retryable)
        self.provider.issue.update(state="closed", state_reason="completed")
        result = deliver_merge_train_pr_feedback_record(
            record=record,
            token="fixture",
            attempted_at="2026-10-01T00:02:00Z",
            planning_author_ids=(_BOT,),
        )
        self.assertEqual(result.delivery_status, "delivered")
        self.assertTrue(_receipt(result)["ok"])

    def test_invalid_or_future_merge_time_never_starts_pending_retries(self) -> None:
        self.provider.issue["state"] = "open"
        for timestamp in (None, "invalid", "2026-11-01T00:00:00Z"):
            with self.subTest(timestamp=timestamp):
                self.provider.merged_at = timestamp
                result = self.record()
                self.assertFalse(result.retryable)
                self.assertFalse(self.provider.writes)
                self.comment_mock.assert_not_called()

    def test_pending_source_cannot_hide_a_terminal_hold_indefinitely(self) -> None:
        self.provider.references = [7, 8]
        self.provider.issue["state"] = "open"
        held = PlanningProvider()
        held.issue.update(number=8, node_id="issue8")
        held.comments = [{"user": {"id": _BOT}, "body": _wait("waiting")}]

        def request(*, path: str, **kwargs: Any) -> Any:
            provider = held if path.startswith(f"/repos/{_REPO}/issues/8") else self.provider
            return provider.request(path=path, **kwargs)

        with patch.object(completion, "github_api_request", side_effect=request):
            result = self.record()
            self.assertTrue(result.retryable)
            self.comment_mock.assert_not_called()
            deadline = (
                datetime.fromisoformat(self.provider.merged_at.replace("Z", "+00:00"))
                + completion.PROJECTION_GRACE
            )
            terminal = deliver_merge_train_pr_feedback_record(
                record=result,
                token="fixture",
                attempted_at=deadline.isoformat(),
                planning_author_ids=(_BOT,),
            )
        self.assertFalse(terminal.retryable)
        self.assertEqual(
            {item["number"]: item["status"] for item in _receipt(terminal)["issues"]},
            {7: "review_reporter_acceptance", 8: "review_wait_record"},
        )
        self.assertTrue(self.comment_mock.called)
        self.assertFalse(self.provider.writes)
        self.assertIn(_LABELS["done"], {item["name"] for item in held.issue["labels"]})
        self.assertEqual(held.comments[0]["body"], _wait("waiting"))

    def test_graphql_failure_classification_requires_transient_evidence(self) -> None:
        for errors, retryable, rate_limited in (
            ([{"type": "RATE_LIMITED"}], True, True),
            ([{"extensions": {"code": "SERVICE_UNAVAILABLE"}}], True, False),
            ([{"type": "TIMEOUT"}], True, False),
            ([{"type": "INTERNAL"}], True, False),
            ([{"type": "FORBIDDEN"}, {"type": "RATE_LIMITED"}], False, False),
            ([{"message": "unknown"}], False, False),
        ):
            with self.subTest(errors=errors):
                with patch.object(
                    completion, "github_api_request", return_value={"errors": errors}
                ):
                    result = self.record()
                self.assertEqual(result.delivery_status, "failed")
                self.assertEqual(result.retryable, retryable)
                self.assertEqual(bool(result.provider_retry_at), rate_limited)
                self.assertFalse(self.provider.writes)
                self.comment_mock.assert_not_called()

    def test_later_global_provider_failure_preserves_verified_source_receipt(self) -> None:
        for failure in ("read", "project", "write", "malformed"):
            with self.subTest(failure=failure), TemporaryDirectory() as directory:
                self.provider = PlanningProvider()
                self.provider.references = [7, 8]
                other = PlanningProvider()
                other.issue.update(number=8, node_id="issue8")
                if failure == "project":
                    self.provider.config = {"planning": {"projects": {"default_project": 2}}}

                def request(
                    *, path: str, method: str = "GET", body: Any = None, **kwargs: Any
                ) -> Any:
                    variables = (body or {}).get("variables", {})
                    source = path.startswith(f"/repos/{_REPO}/issues/8")
                    if failure == "malformed" and source and "/comments?" in path:
                        return {"incomplete": True}
                    if (failure == "read" and source and method == "GET") or (
                        failure == "write" and source and method != "GET"
                    ):
                        error = click.ClickException("temporary provider failure")
                        error.__cause__ = URLError("offline")
                        raise error
                    if failure == "project" and variables.get("issue") == "issue8":
                        return {"errors": [{"type": "RATE_LIMITED"}]}
                    provider = other if source else self.provider
                    return provider.request(path=path, method=method, body=body, **kwargs)

                with patch.object(completion, "github_api_request", side_effect=request):
                    result = self.record()
                self.assertEqual(result.retryable, failure != "malformed")
                if failure == "malformed":
                    self.assertTrue(self.comment_mock.called)
                    self.assertIn(_LABELS["done"], {item["name"] for item in other.issue["labels"]})
                else:
                    self.comment_mock.assert_not_called()
                store = FilesystemRecordStore(Path(directory))
                store.write_merge_train_pr_feedback_record(result)
                saved = store.list_merge_train_pr_feedback_records(repository=_REPO)[0]
                self.assertFalse(_receipt(saved)["ok"])
                self.assertEqual(
                    {item["number"]: item["status"] for item in _receipt(saved)["issues"]},
                    {
                        7: "reconciled",
                        8: "review_wait_record"
                        if failure == "malformed"
                        else "reconciliation_failed",
                    },
                )

    def test_upsert_programming_errors_surface_for_every_feedback_kind(self) -> None:
        for event in ("queued", "blocked", "completed"):
            for error in (KeyError, TypeError, ValueError):
                with self.subTest(event=event, error=error):
                    with patch(
                        "control_plane.merge_train_pr_feedback.upsert_github_issue_comment",
                        side_effect=error("bug"),
                    ):
                        with self.assertRaises(error):
                            build_merge_train_pr_feedback_record(
                                request=MergeTrainPrFeedbackEnvelope(
                                    repository=_REPO, pull_request_number=9, event=event
                                ),
                                policy_key="test",
                                policy_sha256="digest",
                                token="fixture",
                                recorded_at="2026-10-01T00:00:00Z",
                                response_trace_id="test",
                                planning_author_ids=(_BOT,),
                            )

    def test_terminal_hold_comment_provider_failure_can_retry_publication(self) -> None:
        self.provider.comments = [{"user": {"id": _BOT}, "body": _wait("waiting")}]
        error = click.ClickException("comment provider unavailable")
        error.__cause__ = URLError("offline")
        self.comment_mock.side_effect = error
        result = self.record()
        self.assertTrue(result.retryable)
        self.assertFalse(_receipt(result)["ok"])
        self.comment_mock.side_effect = None
        retry = deliver_merge_train_pr_feedback_record(
            record=result,
            token="fixture",
            attempted_at="2026-10-01T00:02:00Z",
            planning_author_ids=(_BOT,),
        )
        self.assertEqual(retry.delivery_status, "failed")
        self.assertFalse(retry.retryable)
        self.assertEqual(retry.comment_id, 3)
        self.assertEqual(retry.comment_markdown.count(_PLANNING_NOTE_START), 1)

    def test_malformed_reconciliation_is_recorded_without_publishing(self) -> None:
        with patch(
            "control_plane.merge_train_pr_feedback.reconcile_completed_change",
            side_effect=ValueError("bad evidence"),
        ):
            result = self.record()
        self.assertEqual(result.delivery_status, "failed")
        self.assertFalse(result.retryable)
        self.comment_mock.assert_not_called()

    def test_collapsed_stack_child_feedback_is_delivered_without_metadata_completion(self) -> None:
        payloads = build_feedback_payloads(
            response={
                "result": {
                    "repository": _REPO,
                    "base_branch": "main",
                    "controller_action": "land_batch",
                    "landing_plan": {
                        "candidate_pull_request_number": 99,
                        "entries": [{"pull_request_number": 9, "status": "merged"}],
                    },
                    "stack_collapse_plan": {
                        "entries": [{"pull_request_number": 8, "status": "closed"}]
                    },
                },
                "records": {"merge_train_batch_landing_plan_record_id": "landed-batch"},
            }
        )
        request = MergeTrainPrFeedbackEnvelope.model_validate(
            next(payload for payload in payloads if payload["pull_request_number"] == 8)
        )
        self.provider.merged = False
        self.provider.merged_at = None
        result = build_merge_train_pr_feedback_record(
            request=request,
            policy_key="test",
            policy_sha256="digest",
            token="fixture",
            recorded_at="2026-10-01T00:00:00Z",
            response_trace_id="stack-child",
            planning_author_ids=(_BOT,),
        )
        self.assertEqual(result.delivery_status, "delivered")
        self.assertEqual(_receipt(result)["status"], "not_applicable")
        self.assertFalse(_receipt(result)["ok"])
        self.assertIn(request.message, self.comment_mock.call_args.kwargs["body"])
        self.assertFalse(self.provider.writes)
        self.assertFalse(self.provider.issue_reads)

    def test_nondefault_landing_feedback_does_not_require_planning_reconciliation(self) -> None:
        self.provider.base_branch = "release"
        original = copy.deepcopy(self.provider.issue)
        result = self.record(base_branch="release")
        self.assertEqual(result.delivery_status, "delivered")
        self.assertEqual(_receipt(result)["status"], "not_applicable")
        self.assertFalse(_receipt(result)["ok"])
        self.assertEqual(self.provider.issue, original)
        self.assertFalse(self.provider.writes)
        self.assertFalse(self.provider.issue_reads)
        self.comment_mock.assert_called_once()

    def test_wrong_feedback_base_is_not_a_nondefault_skip(self) -> None:
        self.provider.base_branch = "other"
        for merged in (False, True):
            with self.subTest(merged=merged):
                self.provider.merged = merged
                result = self.record(base_branch="release")
                self.assertEqual(result.delivery_status, "failed")
                self.assertFalse(result.retryable)
                self.assertFalse(self.provider.writes)
                self.assertFalse(self.provider.issue_reads)
                self.comment_mock.assert_not_called()

    def test_small_merge_clock_difference_waits_then_reconciles(self) -> None:
        attempted = datetime.fromisoformat("2026-10-01T00:00:00+00:00")
        self.provider.merged_at = (attempted + timedelta(seconds=1)).isoformat()
        result = self.record(recorded_at=attempted.isoformat())
        self.assertEqual(result.delivery_status, "failed")
        self.assertTrue(result.retryable)
        self.assertFalse(self.provider.writes)
        self.assertFalse(self.provider.issue_reads)
        self.comment_mock.assert_not_called()
        completed = deliver_merge_train_pr_feedback_record(
            record=result,
            token="fixture",
            attempted_at=(attempted + timedelta(seconds=2)).isoformat(),
            planning_author_ids=(_BOT,),
        )
        self.assertEqual(completed.delivery_status, "delivered")
        self.assertTrue(_receipt(completed)["ok"])

    def test_incomplete_provider_identity_cannot_be_classified_not_applicable(self) -> None:
        for field, value in (
            ("merged", None),
            ("merged", "false"),
            ("default_branch", None),
            ("default_branch", ""),
        ):
            with self.subTest(field=field, value=value):
                self.provider = PlanningProvider()
                self.provider.merged = False
                setattr(self.provider, field, value)
                with patch.object(
                    completion, "github_api_request", side_effect=self.provider.request
                ):
                    result = self.record()
                self.assertEqual(result.delivery_status, "failed")
                self.assertFalse(result.retryable)
                self.assertFalse(self.provider.issue_reads)
                self.assertFalse(self.provider.writes)
                self.comment_mock.assert_not_called()

    def test_merge_clock_skew_boundary_uses_the_same_bounded_grace(self) -> None:
        attempted = datetime.fromisoformat("2026-10-01T00:00:00+00:00")
        for offset, retryable in (
            (completion.PROJECTION_GRACE, True),
            (completion.PROJECTION_GRACE + timedelta(microseconds=1), False),
        ):
            with self.subTest(offset=offset):
                self.provider.merged_at = (attempted + offset).isoformat()
                result = self.record(recorded_at=attempted.isoformat())
                self.assertEqual(result.delivery_status, "failed")
                self.assertEqual(result.retryable, retryable)
                self.assertFalse(self.provider.issue_reads)
                self.assertFalse(self.provider.writes)
                self.comment_mock.assert_not_called()
