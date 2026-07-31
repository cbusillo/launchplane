from __future__ import annotations

import unittest

from control_plane.workflows.runner_host_github_app import (
    expected_runner_host_github_repositories,
)
from control_plane.workflows.runner_host_github_app import (
    read_github_app_installation_repositories,
)
from control_plane.workflows.runner_host_github_app import (
    validate_github_app_installation_scope,
)


class RunnerHostGitHubAppScopeTests(unittest.TestCase):
    def test_expected_repositories_merge_and_deduplicate_runtime_sources(self) -> None:
        repositories = expected_runner_host_github_repositories(
            expected_owner="example-owner",
            github_idle_bindings=(
                "example-owner/repo-one|lane|runner-one|runner-one.service,"
                "example-owner/repo-two|lane|runner-two|runner-two.service"
            ),
            generated_run_cache_roots=(
                "cache=/srv/cache|example-owner/repo-two|20|10|1|24,"
                "other=/srv/other|example-owner/repo-three|20|10|1|24"
            ),
        )

        self.assertEqual(
            repositories,
            frozenset(
                (
                    "example-owner/repo-one",
                    "example-owner/repo-two",
                    "example-owner/repo-three",
                )
            ),
        )

    def test_expected_repositories_reject_cross_owner_scope(self) -> None:
        with self.assertRaisesRegex(ValueError, "share the workflow owner"):
            expected_runner_host_github_repositories(
                expected_owner="example-owner",
                github_idle_bindings=("other-owner/repo-one|lane|runner-one|runner-one.service"),
                generated_run_cache_roots="",
            )

    def test_expected_repositories_reject_malformed_or_empty_sources(self) -> None:
        for bindings, roots, message in (
            ("example-owner/repo-one|lane", "", "four fields"),
            ("", "cache=/srv/cache|example-owner/repo-one|20", "six fields"),
            ("", "", "At least one"),
        ):
            with self.subTest(bindings=bindings, roots=roots):
                with self.assertRaisesRegex(ValueError, message):
                    expected_runner_host_github_repositories(
                        expected_owner="example-owner",
                        github_idle_bindings=bindings,
                        generated_run_cache_roots=roots,
                    )

    def test_installation_repository_read_supports_pagination(self) -> None:
        pages = [
            {
                "total_count": 101,
                "repositories": [
                    {"full_name": f"example-owner/repo-{index}"} for index in range(100)
                ],
            },
            {
                "total_count": 101,
                "repositories": [{"full_name": "example-owner/repo-100"}],
            },
        ]
        calls: list[str] = []

        def fetch_json(url: str, bearer_token: str) -> dict[str, object]:
            self.assertEqual(bearer_token, "installation-token")
            calls.append(url)
            return pages[len(calls) - 1]

        repositories = read_github_app_installation_repositories(
            api_url="https://api.example.test",
            bearer_token="installation-token",
            fetch_json=fetch_json,
        )

        self.assertEqual(len(repositories), 101)
        self.assertEqual(len(calls), 2)
        self.assertIn("page=2", calls[1])

    def test_scope_validation_returns_public_safe_evidence(self) -> None:
        def fetch_json(url: str, bearer_token: str) -> dict[str, object]:
            return {
                "total_count": 2,
                "repositories": [
                    {"full_name": "example-owner/repo-one"},
                    {"full_name": "example-owner/repo-two"},
                ],
            }

        evidence = validate_github_app_installation_scope(
            expected_owner="example-owner",
            github_idle_bindings=("example-owner/repo-one|lane|runner-one|runner-one.service"),
            generated_run_cache_roots=("cache=/srv/cache|example-owner/repo-two|20|10|1|24"),
            api_url="https://api.example.test",
            bearer_token="installation-token",
            fetch_json=fetch_json,
        )

        self.assertEqual(evidence.repository_count, 2)
        self.assertRegex(evidence.repository_sha256, r"^[0-9a-f]{16}$")

    def test_scope_mismatch_reports_only_counts_and_digests(self) -> None:
        def fetch_json(url: str, bearer_token: str) -> dict[str, object]:
            return {
                "total_count": 1,
                "repositories": [{"full_name": "example-owner/unexpected-repo"}],
            }

        with self.assertRaisesRegex(ValueError, "expected_count=1 observed_count=1") as raised:
            validate_github_app_installation_scope(
                expected_owner="example-owner",
                github_idle_bindings=(
                    "example-owner/expected-repo|lane|runner-one|runner-one.service"
                ),
                generated_run_cache_roots="",
                api_url="https://api.example.test",
                bearer_token="installation-token",
                fetch_json=fetch_json,
            )

        message = str(raised.exception)
        self.assertNotIn("expected-repo", message)
        self.assertNotIn("unexpected-repo", message)
        self.assertRegex(message, r"expected_sha256=[0-9a-f]{16}")
        self.assertRegex(message, r"observed_sha256=[0-9a-f]{16}")

    def test_installation_repository_read_rejects_malformed_or_incomplete_response(self) -> None:
        cases: tuple[tuple[dict[str, object], str], ...] = (
            ({"total_count": "1", "repositories": []}, "malformed"),
            ({"total_count": 2, "repositories": [{"full_name": "example/repo"}]}, "incomplete"),
            ({"total_count": 1, "repositories": [{}]}, "entry is malformed"),
        )
        for payload, message in cases:
            with self.subTest(payload=payload):

                def fetch_json(
                    url: str,
                    bearer_token: str,
                    payload: dict[str, object] = payload,
                ) -> dict[str, object]:
                    return payload

                with self.assertRaisesRegex(ValueError, message):
                    read_github_app_installation_repositories(
                        api_url="https://api.example.test",
                        bearer_token="installation-token",
                        fetch_json=fetch_json,
                    )


if __name__ == "__main__":
    unittest.main()
