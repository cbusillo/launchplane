from tests.merge_train_policy_fixtures import dependency_bot_policy
import json
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request

from control_plane.github_read_cache import GitHubReadCache
from control_plane.contracts.github_app_installation_token import GitHubAppInstallationToken
from control_plane.github_request_timing import (
    GitHubRequestPhase,
    MAX_QUOTA_OBSERVATIONS,
    github_request_tally,
    github_request_phase,
)
from control_plane.workflows.launchplane import github_api_request
from control_plane.merge_admission import MergeAdmissionEvaluator
from control_plane.merge_train import build_merge_train_dry_run_result
from control_plane.merge_train_controller_run_once import (
    MergeTrainControllerRunOnceEnvelope,
    execute_merge_train_controller_run_once,
    _advance_planned_stack_collapse_record,
)
from control_plane.merge_train_github import (
    GitHubMergeTrainClient,
    GitHubMergeTrainSnapshotReader,
    MergeTrainGitHubError,
    UrllibMergeTrainGitHubTransport,
)
from tests.support.merge_train_github import RecordingMergeTrainGitHubTransport
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.merge_train_policy_fixtures import build_test_merge_train_policy
from tests.test_merge_train_github import _github_branch, _github_commit, _github_pull_request
from tests.support.merge_train import (
    _FakeStackedMergeTrainSnapshotReader,
    _seed_merge_train_stack_collapse_plan_record,
)


class _Response:
    def __init__(self, payload: object, headers: Message) -> None:
        self.headers = headers
        self.payload = payload
        self.status = 200

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


class _Provider:
    """HTTP-level fixture with GitHub's charged-200/free-304 counter semantics."""

    def __init__(self) -> None:
        self.used = 0
        self.requests: list[Request] = []
        self.pulls = [dict(_github_pull_request(n), labels=[]) for n in range(1, 16)]
        self.payloads: dict[str, object] = {"/value": {"state": "success"}}

    def __call__(self, request: Request, *, timeout: int) -> _Response:
        self.requests.append(request)
        path = urlsplit(request.full_url).path
        payload: object
        if path.endswith("/branches/main"):
            payload = _github_branch()
        elif path.endswith("/pulls"):
            payload = self.pulls
        elif "/issues/" in path and path.endswith("/comments"):
            payload = self.payloads.get(path, [])
        else:
            payload = self.payloads[path]
        etag = json.dumps(payload, sort_keys=True)
        headers = Message()
        headers["ETag"] = etag
        if request.get_header("If-none-match") == etag:
            headers["x-ratelimit-used"] = str(self.used)
            raise HTTPError(request.full_url, 304, "Not Modified", headers, None)
        self.used += 1
        headers["x-ratelimit-used"] = str(self.used)
        return _Response(payload, headers)


class MergeTrainReadCostTests(unittest.TestCase):
    def test_native_graphql_points_follow_response_phase_and_tally(self) -> None:
        headers = Message()
        headers["x-ratelimit-resource"] = "graphql"
        headers["x-ratelimit-reset"] = "123456"
        transport = UrllibMergeTrainGitHubTransport(token="fixture", read_cache=GitHubReadCache())
        with (
            self.assertLogs("control_plane.github_request_timing", level="INFO") as logs,
            github_request_tally("controller trace native-cost"),
            patch("control_plane.merge_train_github.urlopen") as open_request,
        ):
            for phase, points in (("controller", 7), ("controller", 11), ("admission", 3)):
                open_request.return_value = _Response(
                    {"data": {"rateLimit": {"cost": points}, "private-body": "hidden"}}, headers
                )
                with github_request_phase(cast(GitHubRequestPhase, phase)):
                    transport.request(method="POST", path="/graphql")
            with github_request_tally("feedback trace native-cost", phase="feedback"):
                with patch(
                    "control_plane.workflows.launchplane.urlopen",
                    return_value=_Response({"data": {"rateLimit": {"cost": 5}}}, headers),
                ):
                    github_api_request(path="/graphql", token="fixture", method="POST")
        observations = [
            json.loads(line.split("; ", 1)[1])
            for line in logs.output
            if "GitHub quota observation:" in line
        ]
        by_phase = {row["phase"]: row for row in observations}
        self.assertEqual(by_phase["controller"]["graphql_points"], 18)
        self.assertEqual(by_phase["controller"]["requests"], 2)
        self.assertEqual(by_phase["admission"]["graphql_points"], 3)
        self.assertEqual(by_phase["feedback"]["graphql_points"], 5)
        self.assertTrue(all(row["charged_requests"] == "UNKNOWN" for row in observations))
        self.assertNotIn("hidden", "\n".join(logs.output))

    def test_missing_or_failed_graphql_cost_never_becomes_zero_or_a_partial_total(self) -> None:
        valid = {"data": {"rateLimit": {"cost": 7}}}
        payloads: list[object] = [
            None,
            {},
            {"data": None},
            {"data": {"rateLimit": None}},
            {"data": {"rateLimit": {}}},
            {**valid, "errors": [{"message": "private-error"}]},
            *(
                {"data": {"rateLimit": {"cost": value}}}
                for value in (None, "REDACTED", "7", True, 0, -1, 1.5, 10**30)
            ),
        ]
        transport = UrllibMergeTrainGitHubTransport(token="fixture", read_cache=GitHubReadCache())
        for payload in payloads:
            for first, second in ((valid, payload), (payload, valid)):
                with (
                    self.subTest(payload=payload, first_valid=first is valid),
                    self.assertLogs("control_plane.github_request_timing", level="INFO") as logs,
                    github_request_tally("controller trace unknown-cost"),
                    patch(
                        "control_plane.merge_train_github.urlopen",
                        side_effect=[_Response(first, Message()), _Response(second, Message())],
                    ),
                ):
                    transport.request(method="POST", path="/graphql")
                    transport.request(method="POST", path="/graphql")
                (observation,) = [
                    json.loads(line.split("; ", 1)[1])
                    for line in logs.output
                    if "GitHub quota observation:" in line
                ]
                self.assertEqual(observation["graphql_points"], "UNKNOWN")
                self.assertEqual(observation["requests"], 2)
                self.assertNotIn("private-error", "\n".join(logs.output))

    def test_native_cost_preserves_http_errors_and_bounded_omissions(self) -> None:
        transport = UrllibMergeTrainGitHubTransport(token="fixture", read_cache=GitHubReadCache())
        with (
            self.assertLogs("control_plane.github_request_timing", level="INFO") as logs,
            github_request_tally("controller trace capped-cost"),
            patch("control_plane.merge_train_github.urlopen") as open_request,
        ):
            for reset in range(MAX_QUOTA_OBSERVATIONS + 2):
                headers = Message()
                headers["x-ratelimit-reset"] = str(reset)
                open_request.return_value = _Response({"data": {"rateLimit": {"cost": 9}}}, headers)
                transport.request(method="POST", path="/graphql")
            open_request.side_effect = HTTPError("url", 503, "unavailable", Message(), None)
            with self.assertRaises(MergeTrainGitHubError):
                transport.request(method="POST", path="/graphql")
        observations = [
            json.loads(line.split("; ", 1)[1])
            for line in logs.output
            if "GitHub quota observation:" in line
        ]
        self.assertEqual(len(observations), MAX_QUOTA_OBSERVATIONS)
        self.assertTrue(all(row["graphql_points"] == 9 for row in observations))
        (coverage,) = [line for line in logs.output if "GitHub quota coverage:" in line]
        self.assertIn("omitted_requests=3", coverage)
        self.assertIn("graphql_points=UNKNOWN", coverage)

    def test_unavailable_status_cannot_make_quota_logging_break_a_response(self) -> None:
        response = _Response({"result": "available"}, Message())
        transport = UrllibMergeTrainGitHubTransport(token="fixture", read_cache=GitHubReadCache())
        with (
            self.assertLogs("control_plane.github_request_timing", level="INFO") as logs,
            github_request_tally("controller trace unavailable-status"),
            patch.object(response, "status", Mock()),
            patch("control_plane.merge_train_github.urlopen", return_value=response),
        ):
            self.assertEqual(transport.request(method="GET", path="/value"), response.payload)
        (observation,) = [
            json.loads(line.split("; ", 1)[1])
            for line in logs.output
            if "GitHub quota observation:" in line
        ]
        self.assertIsNone(observation["status"])
        self.assertEqual(observation["charged_requests"], "UNKNOWN")

    def test_quota_receipts_keep_shared_counters_separate_from_charged_cost(self) -> None:
        credential = GitHubAppInstallationToken(
            token="private-token",
            app_id=17,
            installation_id=23,
            repository_id=41,
            repository="example/repo",
            expires_at="2030-01-01T00:00:00Z",
        )
        transport = UrllibMergeTrainGitHubTransport(token=credential, read_cache=GitHubReadCache())
        headers = Message()
        for name, value in {
            "x-ratelimit-resource": "core",
            "x-ratelimit-reset": "123456",
            "x-ratelimit-used": "10",
            "x-ratelimit-remaining": "90",
            "x-ratelimit-limit": "100",
            "Authorization": "private-authorization",
            "Set-Cookie": "private-cookie",
        }.items():
            headers[name] = value
        with (
            self.assertLogs("control_plane.github_request_timing", level="INFO") as logs,
            github_request_tally("controller trace quota", credential=credential),
            patch("control_plane.merge_train_github.urlopen") as open_request,
        ):
            open_request.return_value = _Response({"private-body": "hidden"}, headers)
            transport.request(method="GET", path="/value?private=query")
            headers.replace_header("x-ratelimit-used", "70")
            headers.replace_header("x-ratelimit-remaining", "30")
            transport.request(method="GET", path="/value")
            open_request.side_effect = HTTPError("url", 304, "unchanged", headers, None)
            with self.assertRaises(MergeTrainGitHubError):
                transport.request(method="GET", path="/uncached")
            open_request.side_effect = HTTPError("url", 403, "refused", headers, None)
            with self.assertRaises(MergeTrainGitHubError):
                transport.request(method="GET", path="/value")
        observations = [
            json.loads(line.split("; ", 1)[1])
            for line in logs.output
            if "GitHub quota observation:" in line
        ]
        self.assertEqual(sum(row["requests"] for row in observations), 4)
        by_status = {row["status"]: row for row in observations}
        self.assertEqual(by_status[200]["first"]["used"], 10)
        self.assertEqual(by_status[200]["last"]["used"], 70)
        self.assertEqual(by_status[200]["charged_requests"], "UNKNOWN")
        self.assertEqual(by_status[403]["charged_requests"], "UNKNOWN")
        self.assertEqual(by_status[304]["charged_requests"], 0)
        for row in observations:
            self.assertEqual(row["app_id"], credential.app_id)
            self.assertEqual(row["installation_id"], credential.installation_id)
            self.assertEqual(row["resource"], "core")
            self.assertEqual(row["reset"], 123456)
            self.assertEqual(row["graphql_points"], "UNKNOWN")
        for hidden in (
            credential.token,
            "private-authorization",
            "private-cookie",
            "private-body",
            "private=query",
        ):
            self.assertNotIn(hidden, "\n".join(logs.output))

    def test_admission_workflow_receipts_match_only_the_verified_credential(self) -> None:
        credential = GitHubAppInstallationToken(
            token="native-token",
            app_id=71,
            installation_id=82,
            repository_id=93,
            repository="example/repo",
            expires_at="2030-01-01T00:00:00Z",
        )
        headers = Message()
        headers["x-ratelimit-resource"] = "graphql"
        headers["x-ratelimit-used"] = "999999999999999999999999999999999999999"
        headers["x-ratelimit-reset"] = "123456"
        with (
            self.assertLogs("control_plane.github_request_timing", level="INFO") as logs,
            github_request_tally("controller trace admission", credential=credential),
            github_request_phase("admission"),
            patch(
                "control_plane.workflows.launchplane.urlopen", return_value=_Response({}, headers)
            ),
        ):
            github_api_request(
                path="/graphql", token=credential.token, method="POST", body={"secret": "hidden"}
            )
            github_api_request(path="/graphql", token="different-token", method="POST")
        observations = [
            json.loads(line.split("; ", 1)[1])
            for line in logs.output
            if "GitHub quota observation:" in line
        ]
        self.assertEqual(len(observations), 2)
        self.assertEqual([row["installation_id"] for row in observations], [82, None])
        for row in observations:
            self.assertEqual(row["phase"], "admission")
            self.assertEqual(row["first"]["used"], None)
            self.assertEqual(row["charged_requests"], "UNKNOWN")
            self.assertEqual(row["graphql_points"], "UNKNOWN")
        self.assertNotIn("hidden", "\n".join(logs.output))

    def test_rejected_dependencies_cache_commit_and_handoff_evidence_on_repeat_pass(self) -> None:
        provider = _Provider()
        policy = build_test_merge_train_policy()
        target = policy.policies[0]
        target.enqueue.trusted_automation_github_user_ids = (1001,)
        target.enqueue.dependency_update_bots = (dependency_bot_policy(1001),)
        provider.pulls = [
            dict(
                _github_pull_request(n),
                labels=[],
                user={"id": 1001, "type": "Bot", "login": "updates"},
            )
            for n in range(1, 16)
        ]
        for number in range(1, 16):
            provider.payloads[f"/repos/{target.repository}/pulls/{number}/commits"] = [
                _github_commit(1001, "Bump example-lib from 1.0.0 to 2.0.0", sha=f"head-{number}")
            ]
        reader = GitHubMergeTrainSnapshotReader(
            transport=UrllibMergeTrainGitHubTransport(
                token="fixture", read_cache=GitHubReadCache()
            ),
            repository_policy=target,
        )
        deltas = []
        with patch("control_plane.merge_train_github.urlopen", provider):
            for _ in range(2):
                before = provider.used
                snapshot = reader.read_merge_train_snapshot(
                    repository=target.repository, base_branch="main"
                )
                self.assertEqual(
                    build_merge_train_dry_run_result(
                        policy=policy, snapshot=snapshot
                    ).intended_next_action,
                    "idle",
                )
                deltas.append(provider.used - before)
        self.assertGreater(deltas[0], 0)
        self.assertEqual(deltas[1], 0)

    def test_dependency_classification_preserves_quota_deadline_failures(self) -> None:
        provider = _Provider()
        policy = build_test_merge_train_policy().policies[0]
        policy.enqueue.dependency_update_bots = (dependency_bot_policy(1001),)
        provider.pulls = [
            dict(
                _github_pull_request(1),
                labels=[],
                user={"id": 1001, "type": "Bot", "login": "updates"},
            )
        ]
        reset = int((datetime.now(timezone.utc) + timedelta(minutes=1)).timestamp())
        headers = Message()
        headers["x-ratelimit-remaining"] = "0"
        headers["x-ratelimit-reset"] = str(reset)

        def request(value: Request, *, timeout: int) -> _Response:
            if "/commits?" in value.full_url:
                raise HTTPError(value.full_url, 403, "quota exhausted", headers, None)
            return provider(value, timeout=timeout)

        reader = GitHubMergeTrainSnapshotReader(
            transport=UrllibMergeTrainGitHubTransport(
                token="fixture", read_cache=GitHubReadCache()
            ),
            repository_policy=policy,
        )
        with patch("control_plane.merge_train_github.urlopen", request):
            with self.assertRaises(MergeTrainGitHubError) as failure:
                reader.read_merge_train_snapshot(repository=policy.repository, base_branch="main")
        self.assertTrue(failure.exception.rate_limited)
        self.assertEqual(failure.exception.rate_limit_reset, reset)

    def test_unenriched_stack_child_pauses_instead_of_retiring_the_saved_plan(self) -> None:
        policy = build_test_merge_train_policy()
        target = policy.policies[0]
        for draft in (False, True):
            with self.subTest(draft=draft), TemporaryDirectory() as directory:
                state_dir = Path(directory)
                _seed_merge_train_stack_collapse_plan_record(state_dir, policy=policy)
                store = FilesystemRecordStore(state_dir)
                (record,) = store.list_merge_train_stack_collapse_plan_records(
                    repository=target.repository, base_branch="main", status="active"
                )
                snapshot = _FakeStackedMergeTrainSnapshotReader(
                    transport=None
                ).read_merge_train_snapshot(repository=target.repository, base_branch="main")
                root, _ = snapshot.pull_requests
                raw_child = dict(
                    _github_pull_request(
                        2, head_sha="head-child", head_ref="feature/child", base_ref="feature/root"
                    ),
                    draft=draft,
                    labels=[{"name": target.enqueue_label}] if draft else [],
                )
                reader = GitHubMergeTrainSnapshotReader(
                    transport=RecordingMergeTrainGitHubTransport(responses=([],)),
                    repository_policy=target,
                )
                child = reader._pull_request_snapshot(
                    repository_path=target.repository,
                    pull_request=raw_child,
                    enrich=reader._needs_enrichment(raw_child),
                )
                client = Mock(spec=GitHubMergeTrainClient)
                client.read_merge_train_snapshot.return_value = snapshot.model_copy(
                    update={"pull_requests": (root, child)}
                )
                result = _advance_planned_stack_collapse_record(
                    request=MergeTrainControllerRunOnceEnvelope(
                        repository=target.repository, mutate=True
                    ),
                    policy_sha256=policy.policy_sha256,
                    repository_policy=target,
                    transport=cast(Any, None),
                    github_client=client,
                    stack_collapse_store=store,
                    planned_collapse_record=record,
                    trace_id="pause-test",
                    recorded_at=record.updated_at,
                    lease=cast(Any, Mock()),
                )
                self.assertIsNone(result)
                self.assertEqual(
                    store.list_merge_train_stack_collapse_plan_records(
                        repository=target.repository, base_branch="main", status="active"
                    ),
                    (record,),
                )

    def test_real_idle_controller_caches_required_discussion_reads_without_admission(self) -> None:
        provider = _Provider()
        policy = build_test_merge_train_policy()
        admission = Mock(spec=MergeAdmissionEvaluator)
        admission.evaluate.side_effect = AssertionError("Idle controller attempted admission")
        with (
            TemporaryDirectory() as directory,
            patch("control_plane.merge_train_github.urlopen", provider),
            patch("control_plane.merge_train_github.MERGE_TRAIN_READ_CACHE", GitHubReadCache()),
        ):
            store = FilesystemRecordStore(state_dir=Path(directory))
            results = []
            deltas = []
            for index in range(2):
                before = provider.used
                with github_request_tally("isolated idle controller"):
                    results.append(
                        execute_merge_train_controller_run_once(
                            request=MergeTrainControllerRunOnceEnvelope(
                                repository=policy.policies[0].repository
                            ),
                            policy=policy,
                            policy_sha256="test-policy",
                            repository_policy=policy.policies[0],
                            token="fixture-token",
                            trace_id=f"fixture-{index}",
                            recorded_at="2026-10-10T17:00:00Z",
                            candidate_store=store,
                            landing_store=store,
                            stack_collapse_store=store,
                            controller_state_store=store,
                            admission_store=store,
                            admission_evaluator=cast(
                                MergeAdmissionEvaluator, cast(object, admission)
                            ),
                        )
                    )
                deltas.append(provider.used - before)
            self.assertGreater(deltas[0], 0)
            self.assertEqual(deltas[1], 0)
            admission.evaluate.assert_not_called()
            self.assertTrue(all(r.accepted_result["controller_action"] == "idle" for r in results))
            self.assertTrue(all(request.method == "GET" for request in provider.requests))

    def test_idle_snapshot_preserves_stack_graph_and_enriches_after_label_added(self) -> None:
        provider = _Provider()
        provider.pulls[1] = dict(_github_pull_request(2, base_ref="feature-1"), labels=[])
        transport = UrllibMergeTrainGitHubTransport(token="fixture", read_cache=GitHubReadCache())
        reader = GitHubMergeTrainSnapshotReader(
            transport=transport, repository_policy=build_test_merge_train_policy().policies[0]
        )
        with patch("control_plane.merge_train_github.urlopen", provider):
            snapshot = reader.read_merge_train_snapshot(
                repository="cbusillo/sellyouroutboard", base_branch="main"
            )
            self.assertEqual([pr.number for pr in snapshot.pull_requests], list(range(1, 16)))
            self.assertEqual(snapshot.pull_requests[1].base_ref, "feature-1")
            self.assertTrue(
                all(pr.required_checks_status == "unknown" for pr in snapshot.pull_requests)
            )
            provider.pulls[0]["labels"] = [{"name": "ready-to-merge"}]
            # The new label must cause a detail read; an ineligible snapshot
            # cannot be reused across passes. Stop there to expose the request.
            with self.assertRaises(KeyError):
                reader.read_merge_train_snapshot(
                    repository="cbusillo/sellyouroutboard", base_branch="main"
                )
            self.assertTrue(provider.requests[-1].full_url.endswith("/pulls/1"))

    def test_label_free_dependency_and_permissive_policy_are_still_enriched(self) -> None:
        policy = build_test_merge_train_policy().policies[0]
        policy.enqueue.dependency_update_bots = (dependency_bot_policy(1001),)
        provider = _Provider()
        provider.pulls = [
            dict(
                _github_pull_request(1),
                labels=[],
                user={"id": 1001, "type": "Bot", "login": "updates"},
            )
        ]
        reader = GitHubMergeTrainSnapshotReader(
            transport=UrllibMergeTrainGitHubTransport(
                token="fixture", read_cache=GitHubReadCache()
            ),
            repository_policy=policy,
        )
        with patch("control_plane.merge_train_github.urlopen", provider):
            with self.assertRaises(KeyError):
                reader.read_merge_train_snapshot(repository=policy.repository, base_branch="main")
            self.assertTrue(
                provider.requests[-1].full_url.endswith("/pulls/1/commits?per_page=100")
            )
            policy.enqueue.label_required = False
            provider.pulls = [dict(_github_pull_request(2), labels=[])]
            with self.assertRaises(KeyError):
                reader.read_merge_train_snapshot(repository=policy.repository, base_branch="main")
            self.assertTrue(provider.requests[-1].full_url.endswith("/pulls/2"))

    def test_dry_run_snapshot_reuse_ends_at_pass_boundary_and_mutating_reads_stay_fresh(
        self,
    ) -> None:
        provider = _Provider()
        client = GitHubMergeTrainClient(
            transport=UrllibMergeTrainGitHubTransport(token="fixture", read_cache=GitHubReadCache())
        )
        policy = build_test_merge_train_policy().policies[0]
        with patch("control_plane.merge_train_github.urlopen", provider):
            client.begin_snapshot_pass(policy=policy, read_only=True)
            first = client.read_merge_train_snapshot(
                repository=policy.repository, base_branch="main"
            )
            requests_after_first = len(provider.requests)
            second = client.read_merge_train_snapshot(
                repository=policy.repository, base_branch="main"
            )
            self.assertEqual(len(provider.requests), requests_after_first)
            self.assertEqual(first, second)
            client.end_snapshot_pass()
            client.begin_snapshot_pass(policy=policy, read_only=False)
            for _ in range(2):
                before = len(provider.requests)
                client.read_merge_train_snapshot(repository=policy.repository, base_branch="main")
                self.assertGreater(len(provider.requests), before)
            client.end_snapshot_pass()

    def test_validators_revalidate_terminal_results_and_changed_heads(self) -> None:
        provider = _Provider()
        observed: list[dict[str, str]] = []
        transport = UrllibMergeTrainGitHubTransport(
            token="fixture",
            read_cache=GitHubReadCache(),
            response_headers_observer=lambda headers: observed.append(dict(headers)),
        )
        with patch("control_plane.merge_train_github.urlopen", provider):
            first = transport.request(method="GET", path="/value")
            cast(dict[str, object], first)["state"] = "corrupted by caller"
            self.assertEqual(transport.request(method="GET", path="/value"), {"state": "success"})
            provider.payloads["/value"] = {"state": "failure"}
            self.assertEqual(transport.request(method="GET", path="/value"), {"state": "failure"})
            provider.payloads["/new-head"] = {"state": "pending"}
            self.assertEqual(
                transport.request(method="GET", path="/new-head"), {"state": "pending"}
            )
        self.assertEqual(provider.used, 3)
        self.assertEqual(observed[1]["x-ratelimit-used"], "1")

    def test_identity_host_and_eviction_do_not_share_bodies_or_validators(self) -> None:
        provider = _Provider()
        cache = GitHubReadCache(capacity=1)
        first = UrllibMergeTrainGitHubTransport(token="a", read_cache=cache)
        other_actor = UrllibMergeTrainGitHubTransport(token="b", read_cache=cache)
        other_host = UrllibMergeTrainGitHubTransport(
            token="a", api_base_url="https://example.invalid", read_cache=cache
        )
        with patch("control_plane.merge_train_github.urlopen", provider):
            for transport in (first, other_actor, other_host, first):
                transport.request(method="GET", path="/value")
        self.assertEqual(provider.used, 4)
        self.assertTrue(
            all(request.get_header("If-none-match") is None for request in provider.requests)
        )

    def test_cached_body_cannot_mask_access_denial_or_unsolicited_304(self) -> None:
        provider = _Provider()
        transport = UrllibMergeTrainGitHubTransport(token="fixture", read_cache=GitHubReadCache())
        with patch("control_plane.merge_train_github.urlopen", provider):
            transport.request(method="GET", path="/value")
        for status, path in ((403, "/value"), (304, "/uncached")):
            with patch(
                "control_plane.merge_train_github.urlopen",
                side_effect=HTTPError("url", status, "refused", Message(), None),
            ):
                with self.assertRaises(MergeTrainGitHubError):
                    transport.request(method="GET", path=path)

    def test_commit_pinned_contents_are_reused_but_new_base_and_absence_are_read(self) -> None:
        provider = _Provider()
        contents = "/repos/example/repo/contents/.github/CODEOWNERS"
        provider.payloads[contents] = {"content": "encoded owners"}
        transport = UrllibMergeTrainGitHubTransport(token="fixture", read_cache=GitHubReadCache())
        with patch("control_plane.merge_train_github.urlopen", provider):
            for revision in ("a" * 40, "a" * 40, "b" * 40):
                transport.request(method="GET", path=f"{contents}?ref={revision}")
        self.assertEqual(provider.used, 2)
        absent = contents.replace(".github/CODEOWNERS", "CODEOWNERS") + "?ref=" + "a" * 40
        with patch(
            "control_plane.merge_train_github.urlopen",
            side_effect=HTTPError("url", 404, "absent", Message(), None),
        ) as request:
            for _ in range(2):
                with self.assertRaises(MergeTrainGitHubError) as refusal:
                    transport.request(method="GET", path=absent)
                self.assertEqual(refusal.exception.status_code, 404)
            self.assertEqual(request.call_count, 2)

    def test_large_response_eviction_preserves_revalidation(self) -> None:
        provider = _Provider()
        provider.payloads["/large"] = "x" * 1000
        transport = UrllibMergeTrainGitHubTransport(
            token="fixture", read_cache=GitHubReadCache(max_bytes=500)
        )
        with patch("control_plane.merge_train_github.urlopen", provider):
            for _ in range(2):
                transport.request(method="GET", path="/large")
        self.assertEqual(provider.used, 2)


if __name__ == "__main__":
    unittest.main()
