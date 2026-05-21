from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import click

from control_plane.work_graph_github_projects import GitHubProjectPlanningFactsConfig
from control_plane.work_graph_issue_inbox import (
    GitHubIssueInboxConfig,
    build_github_issue_inbox_read_model,
    load_github_issue_inbox_config_from_env,
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
