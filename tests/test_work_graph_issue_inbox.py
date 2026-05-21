from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import click

from control_plane.work_graph_github_projects import GitHubProjectPlanningFactsConfig
from control_plane.work_graph_issue_inbox import (
    GitHubIssueInboxConfig,
    GitHubIssueInboxReconcileRequest,
    build_github_issue_inbox_read_model,
    load_github_issue_inbox_config_from_env,
    reconcile_github_issue_inbox,
)
from tests.test_work_graph_github_projects import _write_fake_gh_sequence


class GitHubIssueInboxTests(unittest.TestCase):
    def test_build_issue_inbox_groups_open_issues_and_project_membership(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            directory = Path(temporary_directory_name)
            args_file = directory / "args.txt"
            fake_gh = _write_fake_gh_sequence(
                directory,
                responses=[
                    {
                        "stdout": {
                            "items": [
                                {
                                    "content": {
                                        "number": 7,
                                        "repository": "cbusillo/launchplane",
                                        "title": "Inbox item",
                                        "type": "Issue",
                                        "url": "https://github.com/cbusillo/launchplane/issues/7",
                                    }
                                }
                            ]
                        }
                    },
                    {
                        "stdout": [
                            {
                                "number": 8,
                                "title": "Missing from project",
                                "url": "https://github.com/cbusillo/launchplane/issues/8",
                                "state": "OPEN",
                                "labels": [{"name": "plan"}],
                                "author": {"login": "alice"},
                                "createdAt": "2026-05-21T10:00:00Z",
                                "updatedAt": "2026-05-21T11:00:00Z",
                            },
                            {
                                "number": 7,
                                "title": "Inbox item",
                                "url": "https://github.com/cbusillo/launchplane/issues/7",
                                "state": "OPEN",
                                "labels": ["ready"],
                                "author": {"login": "code"},
                                "createdAt": "2026-05-21T09:00:00Z",
                                "updatedAt": "2026-05-21T12:00:00Z",
                            },
                        ]
                    },
                    {"stdout": []},
                ],
            )
            previous_args = os.environ.get("FAKE_GH_ARGS")
            os.environ["FAKE_GH_ARGS"] = str(args_file)
            try:
                inbox = build_github_issue_inbox_read_model(
                    generated_at="2026-05-21T12:00:00Z",
                    config=GitHubIssueInboxConfig(
                        repositories=("cbusillo/launchplane", "cbusillo/private-fork"),
                        limit_per_repo=25,
                        gh_binary=str(fake_gh),
                        project_config=GitHubProjectPlanningFactsConfig(
                            owner="cbusillo",
                            project_number=4,
                            limit=50,
                            gh_binary=str(fake_gh),
                        ),
                    ),
                )
            finally:
                if previous_args is None:
                    os.environ.pop("FAKE_GH_ARGS", None)
                else:
                    os.environ["FAKE_GH_ARGS"] = previous_args
            recorded_args = args_file.read_text().splitlines()

        self.assertTrue(inbox.project_configured)
        self.assertEqual(inbox.repository_count, 2)
        self.assertEqual(inbox.issue_count, 2)
        launchplane_group = inbox.repositories[0]
        self.assertEqual(launchplane_group.repository, "cbusillo/launchplane")
        self.assertEqual(launchplane_group.present_in_project_count, 1)
        self.assertEqual(launchplane_group.missing_from_project_count, 1)
        self.assertEqual([issue.number for issue in launchplane_group.issues], [7, 8])
        self.assertEqual(launchplane_group.issues[0].key, "cbusillo/launchplane#7")
        self.assertEqual(launchplane_group.issues[0].project_status, "present")
        self.assertEqual(launchplane_group.issues[1].project_status, "missing")
        self.assertEqual(launchplane_group.issues[1].labels, ("plan",))
        self.assertEqual(inbox.repositories[1].repository, "cbusillo/private-fork")
        self.assertEqual(inbox.repositories[1].issue_count, 0)
        self.assertIn("issue", recorded_args)
        self.assertIn("cbusillo/private-fork", recorded_args)

    def test_build_issue_inbox_returns_empty_unconfigured_project_groups(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            directory = Path(temporary_directory_name)
            args_file = directory / "args.txt"
            fake_gh = _write_fake_gh_sequence(directory, responses=[{"stdout": []}])
            previous_args = os.environ.get("FAKE_GH_ARGS")
            os.environ["FAKE_GH_ARGS"] = str(args_file)
            try:
                inbox = build_github_issue_inbox_read_model(
                    generated_at="2026-05-21T12:00:00Z",
                    config=GitHubIssueInboxConfig(
                        repositories=("cbusillo/launchplane",),
                        gh_binary=str(fake_gh),
                    ),
                )
            finally:
                if previous_args is None:
                    os.environ.pop("FAKE_GH_ARGS", None)
                else:
                    os.environ["FAKE_GH_ARGS"] = previous_args

        self.assertFalse(inbox.project_configured)
        self.assertEqual(inbox.issue_count, 0)
        self.assertEqual(inbox.repositories[0].repository, "cbusillo/launchplane")
        self.assertEqual(inbox.repositories[0].issues, ())

    def test_reconcile_issue_inbox_dry_run_lists_missing_open_issues(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            directory = Path(temporary_directory_name)
            fake_gh = _write_fake_gh_sequence(
                directory,
                responses=[
                    {
                        "stdout": {
                            "items": [
                                {
                                    "content": {
                                        "number": 7,
                                        "repository": "cbusillo/launchplane",
                                        "title": "Already planned",
                                        "type": "Issue",
                                    }
                                }
                            ]
                        }
                    },
                    {
                        "stdout": [
                            {
                                "number": 7,
                                "title": "Already planned",
                                "url": "https://github.com/cbusillo/launchplane/issues/7",
                            },
                            {
                                "number": 8,
                                "title": "Missing issue",
                                "url": "https://github.com/cbusillo/launchplane/issues/8",
                            },
                        ]
                    },
                    {"stdout": []},
                ],
            )
            result = reconcile_github_issue_inbox(
                generated_at="2026-05-21T12:00:00Z",
                config=GitHubIssueInboxConfig(
                    repositories=("cbusillo/launchplane",),
                    gh_binary=str(fake_gh),
                    project_config=GitHubProjectPlanningFactsConfig(
                        owner="cbusillo",
                        project_number=4,
                        gh_binary=str(fake_gh),
                    ),
                ),
                request=GitHubIssueInboxReconcileRequest(mode="dry_run"),
            )

        self.assertEqual(result.mode, "dry_run")
        self.assertEqual(result.would_add_count, 1)
        self.assertEqual(result.items[0].key, "cbusillo/launchplane#8")
        self.assertEqual(result.items[0].title, "Missing issue")
        self.assertEqual(result.items[0].action, "would_add")

    def test_reconcile_issue_inbox_apply_adds_missing_and_skips_duplicates(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            directory = Path(temporary_directory_name)
            args_file = directory / "args.txt"
            fake_gh = _write_fake_gh_sequence(
                directory,
                responses=[
                    {"stdout": {"items": []}},
                    {
                        "stdout": [
                            {
                                "number": 8,
                                "title": "Missing issue",
                                "url": "https://github.com/cbusillo/launchplane/issues/8",
                            },
                            {
                                "number": 9,
                                "title": "Race already added",
                                "url": "https://github.com/cbusillo/launchplane/issues/9",
                            },
                        ]
                    },
                    {
                        "stdout": {
                            "items": [
                                {
                                    "content": {
                                        "number": 9,
                                        "repository": "cbusillo/launchplane",
                                        "type": "Issue",
                                    }
                                }
                            ]
                        }
                    },
                    {"stdout": {"id": "PVTI_added"}},
                ],
            )
            previous_args = os.environ.get("FAKE_GH_ARGS")
            os.environ["FAKE_GH_ARGS"] = str(args_file)
            try:
                result = reconcile_github_issue_inbox(
                    generated_at="2026-05-21T12:00:00Z",
                    config=GitHubIssueInboxConfig(
                        repositories=("cbusillo/launchplane",),
                        gh_binary=str(fake_gh),
                        project_config=GitHubProjectPlanningFactsConfig(
                            owner="cbusillo",
                            project_number=4,
                            gh_binary=str(fake_gh),
                        ),
                    ),
                    request=GitHubIssueInboxReconcileRequest(mode="apply"),
                )
            finally:
                if previous_args is None:
                    os.environ.pop("FAKE_GH_ARGS", None)
                else:
                    os.environ["FAKE_GH_ARGS"] = previous_args
            recorded_args = args_file.read_text().splitlines()

        self.assertEqual(result.added_count, 1)
        self.assertEqual(result.already_present_count, 1)
        actions_by_key = {item.key: item.action for item in result.items}
        self.assertEqual(actions_by_key["cbusillo/launchplane#8"], "added")
        self.assertEqual(actions_by_key["cbusillo/launchplane#9"], "already_present")
        self.assertIn("item-add", recorded_args)
        self.assertIn("https://github.com/cbusillo/launchplane/issues/8", recorded_args)

    def test_reconcile_issue_inbox_apply_reports_partial_failure_with_redaction(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            directory = Path(temporary_directory_name)
            fake_gh = _write_fake_gh_sequence(
                directory,
                responses=[
                    {"stdout": {"items": []}},
                    {
                        "stdout": [
                            {
                                "number": 8,
                                "title": "Missing issue",
                                "url": "https://github.com/cbusillo/launchplane/issues/8",
                            }
                        ]
                    },
                    {"stdout": {"items": []}},
                    {
                        "stdout": {},
                        "exit_code": 1,
                        "stderr": "GraphQL failed for ghp_supersecret",
                    },
                ],
            )
            result = reconcile_github_issue_inbox(
                generated_at="2026-05-21T12:00:00Z",
                config=GitHubIssueInboxConfig(
                    repositories=("cbusillo/launchplane",),
                    gh_binary=str(fake_gh),
                    project_config=GitHubProjectPlanningFactsConfig(
                        owner="cbusillo",
                        project_number=4,
                        gh_binary=str(fake_gh),
                    ),
                ),
                request=GitHubIssueInboxReconcileRequest(mode="apply"),
            )

        self.assertEqual(result.failed_count, 1)
        self.assertEqual(result.items[0].action, "failed")
        self.assertIn("[redacted]", result.items[0].detail)
        self.assertNotIn("ghp_supersecret", result.items[0].detail)

    def test_reconcile_issue_inbox_apply_reports_empty_and_already_present(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            directory = Path(temporary_directory_name)
            fake_gh = _write_fake_gh_sequence(
                directory,
                responses=[
                    {
                        "stdout": {
                            "items": [
                                {
                                    "content": {
                                        "number": 8,
                                        "repository": "cbusillo/launchplane",
                                        "type": "Issue",
                                    }
                                }
                            ]
                        }
                    },
                    {
                        "stdout": [
                            {
                                "number": 8,
                                "title": "Already planned",
                                "url": "https://github.com/cbusillo/launchplane/issues/8",
                            }
                        ]
                    },
                    {"stdout": []},
                    {
                        "stdout": {
                            "items": [
                                {
                                    "content": {
                                        "number": 8,
                                        "repository": "cbusillo/launchplane",
                                        "type": "Issue",
                                    }
                                }
                            ]
                        }
                    },
                ],
            )
            result = reconcile_github_issue_inbox(
                generated_at="2026-05-21T12:00:00Z",
                config=GitHubIssueInboxConfig(
                    repositories=("cbusillo/launchplane", "cbusillo/empty"),
                    gh_binary=str(fake_gh),
                    project_config=GitHubProjectPlanningFactsConfig(
                        owner="cbusillo",
                        project_number=4,
                        gh_binary=str(fake_gh),
                    ),
                ),
                request=GitHubIssueInboxReconcileRequest(mode="apply"),
            )

        self.assertEqual(result.repository_count, 2)
        self.assertEqual(result.issue_count, 1)
        self.assertEqual(result.added_count, 0)
        self.assertEqual(result.already_present_count, 1)
        self.assertEqual(result.failed_count, 0)
        self.assertEqual(result.items[0].action, "already_present")

    def test_load_issue_inbox_config_from_env_parses_inventory_and_limit(self) -> None:
        config = load_github_issue_inbox_config_from_env(
            {
                "LAUNCHPLANE_WORK_GRAPH_ISSUE_INBOX_REPOSITORIES": " cbusillo/launchplane,\ncbusillo/code,cbusillo/launchplane ",
                "LAUNCHPLANE_WORK_GRAPH_ISSUE_INBOX_LIMIT": "25",
                "LAUNCHPLANE_WORK_GRAPH_GH_BINARY": "/opt/bin/gh",
            }
        )

        self.assertIsNotNone(config)
        assert config is not None
        self.assertEqual(config.repositories, ("cbusillo/launchplane", "cbusillo/code"))
        self.assertEqual(config.limit_per_repo, 25)
        self.assertEqual(config.gh_binary, "/opt/bin/gh")

    def test_load_issue_inbox_config_from_env_rejects_invalid_repository(self) -> None:
        with self.assertRaises(click.ClickException):
            load_github_issue_inbox_config_from_env(
                {"LAUNCHPLANE_WORK_GRAPH_ISSUE_INBOX_REPOSITORIES": "cbusillo"}
            )


if __name__ == "__main__":
    unittest.main()
