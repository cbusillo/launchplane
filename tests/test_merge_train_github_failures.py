from email.message import Message
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from control_plane.merge_train_controller_run_once import (
    _controller_exception_reconciliation_detail,
    merge_train_controller_mutation_fence,
)
from control_plane.merge_train_github import (
    MergeTrainGitHubError,
    MergeTrainGitHubMergeRejectedError,
    UrllibMergeTrainGitHubTransport,
    _required_branch_checks,
)
from control_plane.storage.filesystem import FilesystemRecordStore


def _failed_request(
    status: int,
    headers: dict[str, str],
    *,
    method: str = "GET",
    path: str = "/repos/private-owner/private-repo/pulls/42?token=secret-query",
) -> MergeTrainGitHubError:
    response_headers = Message()
    for name, value in headers.items():
        response_headers[name] = value
    error = HTTPError(
        "https://api.github.com" + path, status, "secret-provider-message", response_headers, None
    )
    with patch("control_plane.merge_train_github.urlopen", side_effect=error):
        try:
            UrllibMergeTrainGitHubTransport(token="secret-token").request(method=method, path=path)
        except MergeTrainGitHubError as caught:
            return caught
    raise AssertionError("HTTP failure was not raised")


class MergeTrainGitHubFailureTests(unittest.TestCase):
    def test_protected_branch_read_preserves_rate_limit_classification(self) -> None:
        for headers, classification in (
            (
                {"Retry-After": "60", "X-RateLimit-Reset": "1791090000"},
                "retryable:github_rate_limited",
            ),
            ({}, "operator_required:github_request_rejected"),
        ):
            with self.subTest(headers=headers):
                original = _failed_request(
                    403, headers, path="/repos/private-owner/private-repo/branches/main"
                )
                transport = UrllibMergeTrainGitHubTransport(token="secret-token")
                with patch.object(transport, "request", side_effect=original):
                    with self.assertRaises(MergeTrainGitHubError) as caught:
                        _required_branch_checks(
                            transport=transport,
                            repository_path="private-owner/private-repo",
                            base_branch="main",
                        )
                detail = _controller_exception_reconciliation_detail(caught.exception)
                self.assertTrue(detail.startswith(classification + ";"), detail)
                self.assertIn("GET /repos/{owner}/{repo}/branches/{branch} HTTP 403", detail)
                if "X-RateLimit-Reset" in headers:
                    self.assertIn("reset_at:1791090000", detail)

    def test_refusal_and_rate_limit_classification_preserves_safe_request(self) -> None:
        cases: tuple[tuple[int, dict[str, str], str], ...] = (
            (403, {}, "operator_required:github_request_rejected"),
            (422, {"x-ratelimit-remaining": "0"}, "operator_required:github_request_rejected"),
            (403, {"Retry-After": "60"}, "retryable:github_rate_limited"),
            (403, {"X-RateLimit-Remaining": "0"}, "retryable:github_rate_limited"),
            (403, {"x-ratelimit-remaining": "1"}, "operator_required:github_request_rejected"),
            (429, {}, "retryable:github_rate_limited"),
            (503, {}, "retryable:github_request_failed"),
        )
        for status, headers, classification in cases:
            with self.subTest(status=status, headers=headers):
                error = _failed_request(status, headers)
                detail = _controller_exception_reconciliation_detail(error)
                self.assertEqual(
                    detail,
                    f"{classification}; request:GET /repos/{{owner}}/{{repo}}/pulls/{{number}} HTTP {status}",
                )
                for private in (
                    "private-owner",
                    "private-repo",
                    "secret-query",
                    "secret-token",
                    "secret-provider-message",
                    "?",
                ):
                    self.assertNotIn(private, detail)
                    self.assertNotIn(private, str(error))

    def test_reset_is_bounded_numeric_metadata_only_on_rate_limits(self) -> None:
        for value in ("1791090000", "secret-token", "123; secret-query", "9" * 200):
            with self.subTest(value=value):
                error = _failed_request(429, {"x-ratelimit-reset": value})
                detail = _controller_exception_reconciliation_detail(error)
                if value == "1791090000":
                    self.assertIn("; reset_at:1791090000", detail)
                else:
                    self.assertNotIn("reset_at:", detail)
                    self.assertNotIn(value, detail)
        error = _failed_request(403, {"x-ratelimit-reset": "1791090000"})
        self.assertTrue(
            _controller_exception_reconciliation_detail(error).startswith("operator_required:")
        )
        self.assertNotIn("reset_at:", _controller_exception_reconciliation_detail(error))

    def test_routes_discard_dynamic_segments_and_unknown_routes(self) -> None:
        cases = (
            (
                "PUT",
                "/repos/secret-owner/secret-repo/pulls/7/merge",
                "/repos/{owner}/{repo}/pulls/{number}/merge",
            ),
            (
                "DELETE",
                "/repos/secret-owner/secret-repo/git/refs/heads/secret-branch",
                "/repos/{owner}/{repo}/git/refs/{reference}",
            ),
            (
                "GET",
                "/repos/secret-owner/secret-repo/branches/secret%2Fbranch",
                "/repos/{owner}/{repo}/branches/{branch}",
            ),
            ("POST", "/graphql?token=secret-query", "/graphql"),
            ("GET", "/secret-unknown-route?token=secret-query", "/{unknown_route}"),
            (
                "GET",
                "/repos/secret-owner/secret-repo/issues/7/events?page=secret-query",
                "/repos/{owner}/{repo}/issues/{number}/events",
            ),
        )
        for method, path, template in cases:
            with self.subTest(path=path):
                error = _failed_request(403, {}, method=method, path=path)
                self.assertEqual(error.request_description, f"{method} {template} HTTP 403")
                self.assertNotIn("secret", _controller_exception_reconciliation_detail(error))

    def test_failure_survives_controller_fence_and_store_reopen(self) -> None:
        error = _failed_request(429, {"x-ratelimit-reset": "1791090000"}, method="PUT")
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            with self.assertRaises(MergeTrainGitHubError):
                with merge_train_controller_mutation_fence(
                    record_store=FilesystemRecordStore(state_dir=state_dir),
                    repository="example/repo",
                    base_branch="main",
                    policy_key="example-policy",
                    policy_sha256="1" * 64,
                    trace_id="fixture-http-failure",
                    active_action="land_batch",
                    active_phase="merge_batch_entries",
                    active_record_id="fixture-landing",
                ):
                    raise error
            (record,) = FilesystemRecordStore(
                state_dir=state_dir
            ).list_merge_train_controller_state_records(
                repository="example/repo", base_branch="main"
            )
            self.assertEqual(
                record.reconciliation_detail, _controller_exception_reconciliation_detail(error)
            )
            self.assertEqual(record.reconciliation_status, "required")
            self.assertEqual(record.active_phase, "merge_batch_entries")
            self.assertEqual(record.active_record_id, "fixture-landing")

    def test_specialized_merge_refusal_retains_request_and_diagnosis(self) -> None:
        original = _failed_request(405, {}, method="PUT", path="/repos/private/repo/pulls/7/merge")
        error = MergeTrainGitHubMergeRejectedError(
            pull_request_number=7, observed_merge_state="behind"
        )
        error.__cause__ = original
        detail = _controller_exception_reconciliation_detail(error)
        self.assertTrue(detail.startswith("operator_required:pull_request_head_behind_base;"))
        self.assertIn("request:PUT /repos/{owner}/{repo}/pulls/{number}/merge HTTP 405", detail)
