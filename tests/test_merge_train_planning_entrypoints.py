from __future__ import annotations

import copy
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import patch
from urllib.error import URLError

import click
from github_plan_status import DEFAULT_STATUS_LABELS

from control_plane import merge_train_planning_completion as completion
from control_plane import merge_train_planning_scope as scope_module
from control_plane import merge_train_scheduler as scheduler
from control_plane.contracts.merge_train_policy import (
    MergeTrainPolicyRecord,
    MergeTrainSchedulerPolicy,
)
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.merge_train_controller_run_once import MergeTrainControllerRunOnceResult
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.http_app_test_support import _post_merge_train_pr_feedback
from tests.support.auth import _StubVerifier
from tests.support.merge_train import _merge_train_service_identity, _merge_train_service_policy
from tests.test_merge_train_planning_completion import _BOT, _REPO, _receipt, _wait
from tests.test_merge_train_planning_projection import NativeGraphProvider
from tests.test_merge_train_scheduler import _policy_record


_TARGET = "example/other"
_TIME = "2026-10-01T00:00:00Z"


def _policy(*, authorized: bool = True, target_author: int = 73) -> MergeTrainPolicyRecord:
    record = _policy_record(
        (_REPO, MergeTrainSchedulerPolicy(enabled=True, mutate=True)),
        (_TARGET, MergeTrainSchedulerPolicy()),
    )
    source, target = record.policy.policies
    source = source.model_copy(
        update={
            "enqueue": source.enqueue.model_copy(
                update={"trusted_automation_github_user_ids": (_BOT,)}
            )
        }
    )
    assert target.github_token.github_app is not None
    target = target.model_copy(
        update={
            "base_branch": "release",
            "enqueue": target.enqueue.model_copy(
                update={"trusted_automation_github_user_ids": (target_author,)}
            ),
            "service_authz": target.service_authz.model_copy(
                update={"context": "launchplane" if authorized else "other-context"}
            ),
            "github_token": target.github_token.model_copy(
                update={
                    "github_app": target.github_token.github_app.model_copy(
                        update={"repository_id": 456}
                    )
                }
            ),
        }
    )
    return MergeTrainPolicyRecord(
        record_id=f"{record.record_id}-{target_author}-{authorized}",
        source=record.source,
        updated_at=record.updated_at,
        policy=record.policy.model_copy(update={"policies": (source, target)}),
    )


def _graph() -> tuple[NativeGraphProvider, dict[str, str]]:
    graph = NativeGraphProvider()
    target = graph.add(_TARGET, 8, wait="waiting")
    target.default_branch = "release"
    names = {key: f"target:{key}" for key in DEFAULT_STATUS_LABELS}
    target.config = {"planning": {"labels": names}}
    target.issue["labels"] = [{"name": names[key]} for key in ("plan", "blocked")]
    target.comments = [
        {"user": {"id": 73}, "body": _wait()},
        {"user": {"id": _BOT}, "body": _wait("waiting")},
    ]
    graph.blocking = [graph.ref(_TARGET, 8)]
    return graph, names


def _token_reader(graph: NativeGraphProvider, record: MergeTrainPolicyRecord) -> Any:
    by_repository = {item.repository: item for item in record.policy.policies}

    def resolve(*, source: Any, repository: str, **_kwargs: Any) -> str:
        if source != by_repository[repository].github_token:
            raise AssertionError("Token request used another repository's configured credential")
        return graph.token(repository)

    return resolve


class NativePlanningHttpTests(IsolatedAsyncioTestCase):
    async def test_completed_feedback_projects_only_current_identity_authorized_targets(
        self,
    ) -> None:
        for authorized in (False, True):
            with self.subTest(authorized=authorized), TemporaryDirectory() as directory:
                graph, names = _graph()
                target = graph.nodes[_TARGET, 8]
                before = copy.deepcopy(target.issue)
                policy = _policy(authorized=authorized)
                store = FilesystemRecordStore(Path(directory))
                store.write_merge_train_policy_record(policy)
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_merge_train_service_identity()),
                    authz_policy=_merge_train_service_policy(),
                    record_store_factory=lambda: store,
                    control_plane_root_path=Path(directory),
                )
                with (
                    patch.object(completion, "github_api_request", side_effect=graph.request),
                    patch.object(scope_module, "github_api_request", side_effect=graph.request),
                    patch(
                        "control_plane.http_app.resolve_merge_train_github_token",
                        side_effect=_token_reader(graph, policy),
                    ) as mint,
                    patch("control_plane.http_app.utc_now_timestamp", return_value=_TIME),
                    patch(
                        "control_plane.merge_train_pr_feedback.upsert_github_issue_comment",
                        return_value={
                            "action": "updated_comment",
                            "comment_id": 3,
                            "comment_url": "",
                        },
                    ),
                ):
                    response = await _post_merge_train_pr_feedback(
                        app,
                        {
                            "repository": _REPO,
                            "base_branch": "main",
                            "pull_request_number": 9,
                            "event": "completed",
                            "controller_action": "land_batch",
                        },
                    )
                self.assertEqual(response.status_code, 202, response.json())
                saved = store.list_merge_train_pr_feedback_records(repository=_REPO)
                self.assertEqual(len(saved), 1)
                rows = {(row["repo"], row["number"]): row for row in _receipt(saved[0])["issues"]}
                self.assertTrue(rows[_REPO, 7]["verified"])
                minted_repositories = [call.kwargs["repository"] for call in mint.call_args_list]
                if authorized:
                    self.assertEqual(minted_repositories, [_REPO, _TARGET])
                    self.assertTrue(rows[_TARGET, 8]["verified"])
                    self.assertEqual(rows[_TARGET, 8]["wait"]["state"], "clear")
                    self.assertEqual(
                        {item["name"] for item in target.issue["labels"]},
                        {names["plan"], names["active"]},
                    )
                    configs = [
                        call
                        for call in graph.calls
                        if call["repo"] == _TARGET and call["path"] == "/graphql"
                    ]
                    self.assertTrue(configs)
                    self.assertTrue(
                        all(
                            call["body"]["variables"]["config"] == "release:.github/github.json"
                            for call in configs
                        )
                    )
                else:
                    self.assertEqual(minted_repositories, [_REPO])
                    self.assertEqual(rows[_TARGET, 8]["status"], "review_posting_authority")
                    self.assertFalse(any(call["repo"] == _TARGET for call in graph.calls))
                    self.assertFalse(target.writes)
                    self.assertEqual(target.issue, before)
                self.assertEqual(target.issue["body"], before["body"])
                self.assertEqual(target.issue["state"], before["state"])


class NativePlanningSchedulerTests(TestCase):
    def test_initial_feedback_and_idle_retry_use_current_target_scope(self) -> None:
        graph, names = _graph()
        target = graph.nodes[_TARGET, 8]
        policy = _policy()
        landed = MergeTrainControllerRunOnceResult(
            accepted_result={
                "repository": _REPO,
                "base_branch": "main",
                "controller_action": "batch_landed",
                "landing_plan": {"entries": [{"pull_request_number": 9, "status": "merged"}]},
            },
            records={"merge_train_batch_landing_plan_record_id": "landed"},
        )
        idle = MergeTrainControllerRunOnceResult(
            accepted_result={
                "repository": _REPO,
                "base_branch": "main",
                "controller_action": "idle",
            },
            records={},
        )

        def temporary(call: dict[str, Any]) -> None:
            if call["repo"] == _TARGET and "/comments?" in call["path"]:
                raise click.ClickException("target temporarily unreadable") from URLError("offline")

        graph.fail = temporary
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(Path(directory))
            store.write_merge_train_policy_record(policy)
            with (
                patch.object(completion, "github_api_request", side_effect=graph.request),
                patch.object(scope_module, "github_api_request", side_effect=graph.request),
                patch.object(
                    scheduler,
                    "resolve_merge_train_github_token",
                    side_effect=_token_reader(graph, policy),
                ) as mint,
                patch.object(
                    scheduler,
                    "execute_merge_train_controller_run_once",
                    side_effect=[landed, idle, idle],
                ),
                patch(
                    "control_plane.merge_train_pr_feedback.upsert_github_issue_comment",
                    return_value={"action": "updated_comment", "comment_id": 3, "comment_url": ""},
                ) as comment,
            ):

                def run(record: MergeTrainPolicyRecord, now: str) -> Any:
                    return scheduler._run_controller(
                        record_store=store,
                        control_plane_root=Path(directory),
                        policy_record=record,
                        repository_policy=record.policy.policies[0],
                        token="fixture",
                        trace_id=now,
                        now=lambda: now,
                    )

                first = run(policy, _TIME)
                self.assertEqual(first.feedback_failed, 1)
                original = store.list_merge_train_pr_feedback_records(repository=_REPO)[0]
                self.assertTrue(original.retryable)
                first_rows = {
                    (row["repo"], row["number"]): row for row in _receipt(original)["issues"]
                }
                self.assertTrue(first_rows[_REPO, 7]["verified"])
                self.assertFalse(target.writes)
                self.assertTrue(any(call["repo"] == _TARGET for call in graph.calls))
                comment.assert_not_called()
                graph.fail = None
                policy = _policy(target_author=74)
                store.write_merge_train_policy_record(policy)
                target.issue["user"]["id"] = 74
                target.comments = [{"user": {"id": 74}, "body": _wait()}]
                graph.token_overrides[_TARGET] = graph.token(_TARGET) + ":rotated"
                mint.side_effect = _token_reader(graph, policy)
                boundary = len(graph.calls)
                second = run(policy, "2026-10-01T00:02:00Z")
                self.assertEqual(second.feedback_delivered, 1)
                recovered = store.list_merge_train_pr_feedback_records(repository=_REPO)[0]
                self.assertEqual(recovered.feedback_id, original.feedback_id)
                self.assertEqual(recovered.recorded_at, original.recorded_at)
                self.assertEqual(recovered.delivery_attempts, original.delivery_attempts + 1)
                self.assertTrue(_receipt(recovered)["ok"])
                self.assertEqual(
                    {item["name"] for item in target.issue["labels"]},
                    {names["plan"], names["active"]},
                )
                self.assertTrue(
                    any(
                        call["repo"] == _TARGET and call["token"].endswith(":rotated")
                        for call in graph.calls[boundary:]
                    )
                )
                third = run(policy, "2026-10-01T00:03:00Z")
                self.assertEqual(third.feedback_delivered, 0)
                comment.assert_called_once()
