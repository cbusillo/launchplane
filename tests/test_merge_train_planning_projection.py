from __future__ import annotations

import copy
import json
from email.message import Message
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import TestCase
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

import click

from control_plane import merge_train_planning_completion as completion
from control_plane import merge_train_planning_scope as scope_module
from control_plane.contracts.merge_train_policy import MergeTrainSchedulerPolicy
from control_plane.merge_train_planning_scope import PlanningRepositoryScope, PlanningScopeReview
from control_plane.merge_train_pr_feedback import (
    MergeTrainPrFeedbackEnvelope,
    build_merge_train_pr_feedback_record,
    deliver_merge_train_pr_feedback_record,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.test_merge_train_planning_completion import (
    PlanningProvider,
    _BOT,
    _LABELS,
    _REPO,
    _receipt,
    _wait,
)
from tests.test_merge_train_scheduler import _policy_record


class NativeGraphProvider:
    """Repository-specific state; a source token cannot pass as target authority."""

    def __init__(self) -> None:
        self.nodes: dict[tuple[str, int], PlanningProvider] = {}
        self.blocking: list[dict[str, Any]] = []
        self.parent: dict[str, Any] | None = None
        self.calls: list[dict[str, Any]] = []
        self.token_overrides: dict[str, str] = {}
        self.fail: Any = None
        self.add(_REPO, 7, state="closed")

    def add(
        self,
        repo: str,
        number: int,
        *,
        state: str = "open",
        wait: str | None = "clear",
        labels: tuple[str, ...] = ("plan", "blocked"),
    ) -> PlanningProvider:
        node = PlanningProvider()
        node.issue.update(
            number=number,
            node_id=f"{repo}#{number}",
            state=state,
            state_reason="completed" if state == "closed" else None,
            body="<!-- github-plan:managed-provenance -->" + ("\n" + _wait(wait) if wait else ""),
            labels=[{"name": _LABELS[key]} for key in labels],
        )
        node.issue["user"]["id"] = _BOT if repo == _REPO else 73
        self.nodes[repo, number] = node
        return node

    def token(self, repo: str) -> str:
        return self.token_overrides.get(repo, "fixture" if repo == _REPO else f"fixture:{repo}")

    def scope(self, repo: str) -> PlanningRepositoryScope:
        node = next(node for (target, _), node in self.nodes.items() if target == repo)
        return PlanningRepositoryScope(
            repo, node.default_branch, self.token(repo), (_BOT,) if repo == _REPO else (73,)
        )

    def ref(self, repo: str, number: int) -> dict[str, Any]:
        return {
            **copy.deepcopy(self.nodes[repo, number].issue),
            "repository_url": f"https://api.github.com/repos/{repo}",
        }

    def request(self, *, path: str, method: str = "GET", token: str, body: Any = None) -> Any:
        if path == "/graphql":
            variables = body["variables"]
            repo = f"{variables.get('owner')}/{variables.get('name')}"
            node = next(node for (target, _), node in self.nodes.items() if target == repo)
        else:
            parts = path.split("/")
            if len(parts) < 4 or parts[1] != "repos":
                raise AssertionError(f"Unexpected graph request: {path}")
            repo = "/".join(parts[2:4])
            number = int(parts[5]) if len(parts) > 5 and parts[4] == "issues" else None
            node = (
                self.nodes[repo, number]
                if number
                else next(node for (target, _), node in self.nodes.items() if target == repo)
            )
        if token != self.token(repo):
            raise AssertionError(f"Wrong repository token used for {repo}")
        call = {"repo": repo, "path": path, "method": method, "token": token, "body": body}
        self.calls.append(call)
        if self.fail:
            self.fail(call)
        if repo == _REPO and "/issues/7/dependencies/blocking?" in path:
            return copy.deepcopy(self.blocking)
        if repo == _REPO and path.endswith("/issues/7/parent") and self.parent:
            return copy.deepcopy(self.parent)
        return node.request(
            path=path.replace(f"/repos/{repo}", f"/repos/{_REPO}"), method=method, body=body
        )


class NativePlanningProjectionTests(TestCase):
    def setUp(self) -> None:
        self.graph = NativeGraphProvider()
        self.transport = patch.object(
            completion,
            "github_api_request",
            side_effect=lambda **kwargs: self.graph.request(**kwargs),
        )
        self.transport.start()
        self.addCleanup(self.transport.stop)
        self.comment = patch(
            "control_plane.merge_train_pr_feedback.upsert_github_issue_comment",
            return_value={"action": "updated_comment", "comment_id": 3, "comment_url": ""},
        )
        self.comment_mock = self.comment.start()
        self.addCleanup(self.comment.stop)

    def record(self) -> Any:
        return build_merge_train_pr_feedback_record(
            request=MergeTrainPrFeedbackEnvelope(
                repository=_REPO,
                pull_request_number=9,
                event="completed",
                controller_action="land_batch",
            ),
            policy_key="test",
            policy_sha256="digest",
            token="fixture",
            recorded_at="2026-10-01T00:00:00Z",
            response_trace_id="projection",
            planning_author_ids=(_BOT,),
            planning_scope_resolver=self.graph.scope,
        )

    def rows(self, record: Any) -> dict[tuple[str, int], dict[str, Any]]:
        return {(row["repo"], row["number"]): row for row in _receipt(record)["issues"]}

    def test_parent_and_dependents_keep_only_current_blockers_and_person_waits(self) -> None:
        parent = self.graph.add(_REPO, 8)
        parent.blockers = [{"number": 7, "state": "closed"}]
        remaining = self.graph.add(_REPO, 9)
        held = self.graph.add(_REPO, 10, wait="waiting")
        remaining.blockers = [{"number": 7, "state": "closed"}, {"number": 12, "state": "open"}]
        held.blockers = [{"number": 12, "state": "open"}]
        self.graph.parent = self.graph.ref(_REPO, 8)
        self.graph.blocking = [self.graph.ref(_REPO, n) for n in (9, 10, 9)]
        before = {key: copy.deepcopy(node.issue) for key, node in self.graph.nodes.items()}
        record = self.record()
        self.assertEqual(record.delivery_status, "delivered")
        rows = self.rows(record)
        self.assertEqual(len(rows), len(_receipt(record)["issues"]))
        self.assertEqual(rows[_REPO, 8]["derived_status"], "active")
        self.assertEqual(rows[_REPO, 8]["blocked_by"], [])
        self.assertEqual(
            rows[_REPO, 9]["blocked_by"], [{"repo": _REPO, "number": 12, "state": "open"}]
        )
        self.assertEqual(rows[_REPO, 10]["wait"]["state"], "waiting")
        self.assertEqual(
            {x["name"] for x in parent.issue["labels"]}, {_LABELS["plan"], _LABELS["active"]}
        )
        self.assertEqual(
            {x["name"] for x in remaining.issue["labels"]}, {_LABELS["plan"], _LABELS["blocked"]}
        )
        self.assertEqual(
            {x["name"] for x in held.issue["labels"]},
            {_LABELS["plan"], _LABELS["blocked"], _LABELS["waiting"]},
        )
        writes = {key: list(node.writes) for key, node in self.graph.nodes.items()}
        self.record()
        self.assertEqual(writes, {key: node.writes for key, node in self.graph.nodes.items()})
        for key, node in self.graph.nodes.items():
            self.assertEqual(node.issue["body"], before[key]["body"])
            self.assertEqual(node.issue["state"], before[key]["state"])

    def test_closed_unread_lifecycle_does_not_release_open_unknown_or_scope_hold(self) -> None:
        closed = self.graph.add(
            _REPO, 8, state="closed", wait=None, labels=("plan", "active", "waiting")
        )
        unknown = self.graph.add(_REPO, 9, wait=None)
        stale = self.graph.add(_REPO, 10, labels=("plan", "stale"))
        unstatused = self.graph.add(_REPO, 11, labels=("plan",))
        before = {n: copy.deepcopy(self.graph.nodes[_REPO, n].issue) for n in (8, 9, 10, 11)}
        self.graph.blocking = [self.graph.ref(_REPO, n) for n in (8, 9, 10, 11)]
        record = self.record()
        rows = self.rows(record)
        self.assertEqual(
            {x["name"] for x in closed.issue["labels"]},
            {_LABELS["plan"], _LABELS["done"], _LABELS["waiting"]},
        )
        self.assertEqual(rows[_REPO, 8]["projection_status"], "reconciled")
        self.assertFalse(rows[_REPO, 8]["verified"])
        for number, node in ((9, unknown), (10, stale), (11, unstatused)):
            self.assertEqual(node.issue, before[number])
            self.assertFalse(node.writes)
        self.assertFalse(rows[_REPO, 9]["verified"])
        self.assertEqual(rows[_REPO, 10]["derived_status"], "hold")
        self.assertEqual(rows[_REPO, 11]["derived_status"], "hold")
        self.assertEqual(closed.issue["body"], before[8]["body"])

    def test_source_wait_preserves_full_completion_review_but_refreshes_neighbor(self) -> None:
        source = self.graph.nodes[_REPO, 7]
        source.comments = [{"user": {"id": _BOT}, "body": _wait("waiting")}]
        neighbor = self.graph.add(_REPO, 8)
        self.graph.blocking = [self.graph.ref(_REPO, 8)]
        before = copy.deepcopy(source.issue)
        record = self.record()
        rows = self.rows(record)
        self.assertEqual(record.delivery_status, "failed")
        self.assertFalse(rows[_REPO, 7]["verified"])
        self.assertTrue(rows[_REPO, 8]["verified"])
        self.assertEqual(
            {x["name"] for x in neighbor.issue["labels"]}, {_LABELS["plan"], _LABELS["active"]}
        )
        self.assertEqual(source.issue["body"], before["body"])
        self.assertEqual(source.issue["state"], before["state"])
        self.assertEqual(source.comments[0]["body"], _wait("waiting"))

    def test_open_source_projection_retries_before_shape_or_acceptance_guards(self) -> None:
        for shape in ("nonplan", "reporter", "unmanaged", "nonplan_unread"):
            with self.subTest(shape=shape), TemporaryDirectory() as directory:
                self.graph = NativeGraphProvider()
                source = self.graph.nodes[_REPO, 7]
                source.issue.update(state="open", state_reason=None)
                if shape.startswith("nonplan"):
                    source.issue["labels"] = [{"name": "bug"}]
                elif shape == "reporter":
                    source.issue["user"]["id"] = 99
                else:
                    source.issue["body"] = _wait()
                if shape == "nonplan_unread":

                    def unread(call: dict[str, Any]) -> None:
                        if "/issues/7/comments?" in call["path"]:
                            raise click.ClickException("unread unrelated history") from URLError(
                                "fixture unavailable"
                            )

                    self.graph.fail = unread
                neighbor = self.graph.add(_REPO, 8)
                neighbor.blockers = [{"number": 7, "state": "open"}]
                self.graph.blocking = [self.graph.ref(_REPO, 8)]
                source_before = copy.deepcopy(source.issue)
                store = FilesystemRecordStore(Path(directory))
                store.write_merge_train_pr_feedback_record(self.record())
                saved = store.list_merge_train_pr_feedback_records(repository=_REPO)[0]
                self.assertTrue(saved.retryable)
                self.assertEqual(self.rows(saved)[_REPO, 7]["status"], "review_projection_pending")
                self.assertIn(
                    _LABELS["blocked"], {item["name"] for item in neighbor.issue["labels"]}
                )

                source.issue.update(state="closed", state_reason="completed")
                neighbor.blockers[0]["state"] = "closed"
                self.graph.fail = None
                retried = deliver_merge_train_pr_feedback_record(
                    record=saved,
                    token="fixture",
                    attempted_at="2026-10-01T00:02:00Z",
                    planning_author_ids=(_BOT,),
                    planning_scope_resolver=self.graph.scope,
                )
                self.assertFalse(retried.retryable)
                self.assertTrue(self.rows(retried)[_REPO, 8]["verified"])
                self.assertEqual(self.rows(retried)[_REPO, 8]["blocked_by"], [])
                self.assertEqual(
                    {item["name"] for item in neighbor.issue["labels"]},
                    {_LABELS["plan"], _LABELS["active"]},
                )
                if shape in {"reporter", "unmanaged"}:
                    self.assertEqual(retried.delivery_status, "failed")
                    self.assertFalse(self.rows(retried)[_REPO, 7]["verified"])
                    self.assertEqual(source.issue["body"], source_before["body"])
                    self.assertFalse(source.writes)
                else:
                    self.assertEqual(retried.delivery_status, "delivered")

    def test_known_denial_skips_only_its_repository_and_keeps_independent_target(self) -> None:
        denied = "example/other"
        independent = "example/independent"
        self.graph.add(denied, 8)
        sibling = self.graph.add(denied, 9)
        other = self.graph.add(independent, 10)
        self.graph.blocking = [
            self.graph.ref(denied, 8),
            self.graph.ref(denied, 9),
            self.graph.ref(independent, 10),
        ]
        attempts: list[str] = []
        headers = Message()
        headers["X-GitHub-Request-Id"] = "fixture-denial"
        error = HTTPError("https://example.test/labels", 403, "Forbidden", headers, BytesIO())
        self.addCleanup(error.close)

        def fail(call: dict[str, Any]) -> None:
            if call["repo"] == denied and call["method"] != "GET":
                attempts.append(call["path"])
                raise click.ClickException("metadata denied") from error

        self.graph.fail = fail
        record = self.record()
        rows = self.rows(record)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(rows[denied, 8]["failure"]["cause"], "permission_denied")
        self.assertEqual(rows[denied, 8]["failure"]["request_id"], headers["X-GitHub-Request-Id"])
        self.assertEqual(rows[denied, 9]["status"], "not_attempted_posting_authority")
        self.assertFalse(sibling.writes)
        self.assertTrue(rows[_REPO, 7]["verified"])
        self.assertTrue(rows[independent, 10]["verified"])
        self.assertEqual(
            {x["name"] for x in other.issue["labels"]}, {_LABELS["plan"], _LABELS["active"]}
        )

    def test_target_uses_its_nonmain_config_token_and_trust(self) -> None:
        repo = "example/other"
        target = self.graph.add(repo, 8)
        target.default_branch = "release"
        custom = {key: f"other:{key}" for key in _LABELS}
        target.config = {"planning": {"labels": custom}}
        target.issue["labels"] = [{"name": custom[key]} for key in ("plan", "blocked")]
        target.issue["body"] = "<!-- github-plan:managed-provenance -->\n" + _wait("waiting")
        target.comments = [{"user": {"id": _BOT, "login": "source-bot[bot]"}, "body": _wait()}]
        self.graph.blocking = [self.graph.ref(repo, 8)]
        first = self.record()
        self.assertEqual(self.rows(first)[repo, 8]["wait"]["state"], "waiting")
        self.assertIn(custom["waiting"], {x["name"] for x in target.issue["labels"]})
        target.comments.append({"user": {"id": 73, "login": "target-bot[bot]"}, "body": _wait()})
        second = self.record()
        self.assertEqual(self.rows(second)[repo, 8]["derived_status"], "active")
        self.assertEqual(
            {x["name"] for x in target.issue["labels"]}, {custom["plan"], custom["active"]}
        )
        configs = [
            call for call in self.graph.calls if call["repo"] == repo and call["path"] == "/graphql"
        ]
        self.assertTrue(configs)
        self.assertTrue(
            all(
                call["body"]["variables"]["config"] == "release:.github/github.json"
                for call in configs
            )
        )

    def test_unknown_write_stops_later_targets_and_retains_prior_verified_readback(self) -> None:
        for n in (8, 9, 10):
            self.graph.add(_REPO, n)
        self.graph.blocking = [self.graph.ref(_REPO, n) for n in (8, 9, 10)]

        def fail(call: dict[str, Any]) -> None:
            if "/issues/9/labels" in call["path"] and call["method"] == "POST":
                raise click.ClickException("reply lost") from URLError("offline")

        self.graph.fail = fail
        record = self.record()
        rows = self.rows(record)
        self.assertTrue(rows[_REPO, 7]["verified"])
        self.assertTrue(rows[_REPO, 8]["verified"])
        self.assertEqual(rows[_REPO, 9]["failure"]["write_outcome"], "unknown")
        self.assertEqual(rows[_REPO, 10]["status"], "not_attempted_after_stop")
        self.assertFalse(self.graph.nodes[_REPO, 10].writes)
        self.assertFalse(_receipt(record)["ok"])
        self.comment_mock.assert_not_called()

    def test_secondary403_body_is_consumed_once_across_projection_and_retry(self) -> None:
        class Body(BytesIO):
            reads = 0

            def read(self, size: int | None = -1) -> bytes:
                self.reads += 1
                return super().read(size)

        body = Body(json.dumps({"message": "You have exceeded a secondary rate limit."}).encode())
        headers = Message()
        headers["X-RateLimit-Remaining"] = "42"
        error = HTTPError("https://example.test/comments", 403, "Forbidden", headers, body)
        self.addCleanup(error.close)

        def fail(call: dict[str, Any]) -> None:
            if "/comments?" in call["path"]:
                raise click.ClickException("provider refused") from error

        self.graph.fail = fail
        record = self.record()
        self.assertTrue(record.retryable)
        self.assertTrue(record.provider_retry_at)
        self.assertEqual(body.reads, 1)
        self.assertEqual(self.rows(record)[_REPO, 7]["failure"]["cause"], "rate_limited")
        self.graph.fail = None
        retried = deliver_merge_train_pr_feedback_record(
            record=record,
            token="fixture",
            attempted_at="2026-10-01T00:02:00Z",
            planning_author_ids=(_BOT,),
            planning_scope_resolver=self.graph.scope,
        )
        self.assertEqual(retried.delivery_status, "delivered")
        self.assertTrue(_receipt(retried)["ok"])
        self.assertEqual(body.reads, 1)

    def test_later_source_discovery_authority_failure_prevents_prior_source_writes(self) -> None:
        for status in (401, 403):
            with self.subTest(status=status):
                self.graph = NativeGraphProvider()
                self.graph.nodes[_REPO, 7].references = [7, 8]
                later = self.graph.add(_REPO, 8, state="closed")
                independent = self.graph.add("example/independent", 10)
                self.graph.blocking = [self.graph.ref("example/independent", 10)]
                error = HTTPError(
                    "https://example.test/discovery", status, "Forbidden", Message(), BytesIO()
                )
                self.addCleanup(error.close)

                def fail(call: dict[str, Any]) -> None:
                    if "/issues/8/dependencies/blocking?" in call["path"]:
                        raise click.ClickException("discovery authority refused") from error

                self.graph.fail = fail
                record = self.record()
                self.assertFalse(self.graph.nodes[_REPO, 7].writes)
                self.assertFalse(later.writes)
                if status == 401:
                    self.assertFalse(independent.writes)
                else:
                    self.assertTrue(self.rows(record)["example/independent", 10]["verified"])

    def test_explicit_malformed_closing_repository_never_defaults_to_source(self) -> None:
        original = self.graph.request

        def request(**kwargs: Any) -> Any:
            result = original(**kwargs)
            if (
                kwargs["path"] == "/graphql"
                and "closingIssuesReferences" in kwargs["body"]["query"]
            ):
                result["data"]["repository"]["pullRequest"]["closingIssuesReferences"]["nodes"][0][
                    "repository"
                ] = {}
            return result

        with patch.object(completion, "github_api_request", side_effect=request):
            record = self.record()
        self.assertFalse(self.graph.nodes[_REPO, 7].writes)
        self.assertEqual(record.delivery_status, "failed")

    def test_known_blocking_target_survives_later_parent_discovery_denial(self) -> None:
        related = self.graph.add(_REPO, 8)
        self.graph.blocking = [self.graph.ref(_REPO, 8)]
        error = HTTPError("https://example.test/parent", 403, "Forbidden", Message(), BytesIO())
        self.addCleanup(error.close)

        def fail(call: dict[str, Any]) -> None:
            if call["path"].endswith("/issues/7/parent"):
                raise click.ClickException("parent read refused") from error

        self.graph.fail = fail
        rows = self.rows(self.record())
        self.assertIn((_REPO, 8), rows)
        self.assertEqual(rows[_REPO, 8]["status"], "not_attempted_posting_authority")
        self.assertFalse(related.writes)


class PlanningScopeTests(TestCase):
    def policy(self) -> Any:
        record = _policy_record(("example/other", MergeTrainSchedulerPolicy()))
        original = record.policy.policies[0]
        return record.policy.model_copy(
            update={
                "policies": tuple(
                    original.model_copy(
                        update={
                            "base_branch": branch,
                            "enqueue": original.enqueue.model_copy(
                                update={"trusted_automation_github_user_ids": (author,)}
                            ),
                        }
                    )
                    for branch, author in (("main", 41), ("release", 73))
                )
            }
        )

    def test_authorization_precedes_token_and_external_targets_never_resolve(self) -> None:
        authorize = Mock(return_value=False)
        token = Mock(return_value="unused")
        policy = self.policy()
        policy = policy.model_copy(
            update={
                "policies": (
                    *policy.policies,
                    policy.policies[0].model_copy(update={"repository": "outside/other"}),
                )
            }
        )
        resolver = scope_module.planning_scope_resolver(
            policy=policy, source_repository=_REPO, authorize=authorize, resolve_token=token
        )
        with patch.object(scope_module, "github_api_request") as request:
            for repo, allowed in (("example/other", False), ("outside/other", True)):
                authorize.reset_mock()
                authorize.return_value = allowed
                with self.assertRaises(PlanningScopeReview):
                    resolver(repo)
                if repo == "outside/other":
                    authorize.assert_not_called()
            token.assert_not_called()
            request.assert_not_called()

    def test_actual_default_is_reauthorized_before_its_token(self) -> None:
        tokens: list[str] = []
        for result in ("unauthorized", "ok", "401", "404", "503"):
            with self.subTest(result=result):
                tokens.clear()

                def token_for(policy: Any) -> str:
                    tokens.append(policy.base_branch)
                    if policy.base_branch == "release" and result.isdigit():
                        cause = HTTPError(
                            "https://example.test/app/installations/1/access_tokens",
                            int(result),
                            "fixture rejection",
                            Message(),
                            BytesIO(b"{}"),
                        )
                        self.addCleanup(cause.close)
                        raise click.ClickException("fixture mint failed") from cause
                    return f"token:{policy.base_branch}"

                resolver = scope_module.planning_scope_resolver(
                    policy=self.policy(),
                    source_repository=_REPO,
                    authorize=lambda policy: (
                        policy.base_branch == "main" or result != "unauthorized"
                    ),
                    resolve_token=token_for,
                )
                with patch.object(
                    scope_module, "github_api_request", return_value={"default_branch": "release"}
                ) as request:
                    if result == "ok":
                        resolved = resolver("example/other")
                        self.assertEqual(
                            (resolved.base_branch, resolved.token, resolved.trusted_author_ids),
                            ("release", "token:release", (73,)),
                        )
                        self.assertEqual(tokens, ["main", "release"])
                    elif result == "unauthorized":
                        with self.assertRaises(PlanningScopeReview):
                            resolver("example/other")
                        self.assertEqual(tokens, ["main"])
                    elif result == "503":
                        with self.assertRaises(click.ClickException) as failed:
                            resolver("example/other")
                        self.assertNotIsInstance(failed.exception, PlanningScopeReview)
                        self.assertEqual(tokens, ["main", "release"])
                    else:
                        with self.assertRaises(PlanningScopeReview) as held:
                            resolver("example/other")
                        self.assertIsNotNone(held.exception.credential_failure)
                        assert held.exception.credential_failure is not None
                        self.assertEqual(held.exception.credential_failure.status_code, int(result))
                        self.assertEqual(tokens, ["main", "release"])
                    request.assert_called_once_with(path="/repos/example/other", token="token:main")

    def test_missing_selected_token_or_unenrolled_default_never_tries_an_alternative(self) -> None:
        for missing_token in (False, True):
            with self.subTest(missing_token=missing_token):
                token = Mock(return_value="" if missing_token else "discovery-token")
                resolver = scope_module.planning_scope_resolver(
                    policy=self.policy(),
                    source_repository=_REPO,
                    authorize=lambda _: True,
                    resolve_token=token,
                )
                with patch.object(
                    scope_module,
                    "github_api_request",
                    return_value={"default_branch": "unconfigured"},
                ) as request:
                    with self.assertRaises(PlanningScopeReview):
                        resolver("example/other")
                    self.assertEqual(token.call_count, 1)
                    self.assertEqual(request.call_count, 0 if missing_token else 1)
