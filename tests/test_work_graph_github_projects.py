from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import click

from control_plane.work_graph_github_projects import (
    GitHubProjectPlanningFactsConfig,
    build_github_project_planning_facts,
    load_github_project_planning_facts_config_from_env,
)
from tests.support.work_graph import write_fake_gh_sequence as _write_fake_gh_sequence


def _write_fake_gh(
    directory: Path, *, stdout: object, exit_code: int = 0, stderr: str = ""
) -> Path:
    return _write_fake_gh_sequence(
        directory, responses=[{"stdout": stdout, "exit_code": exit_code, "stderr": stderr}]
    )


class GitHubProjectPlanningFactsTests(unittest.TestCase):
    def test_build_project_facts_from_gh_item_list(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            directory = Path(temporary_directory_name)
            args_file = directory / "args.txt"
            fake_gh = _write_fake_gh(
                directory,
                stdout={
                    "items": [
                        {
                            "content": {
                                "number": 190,
                                "repository": "cbusillo/launchplane",
                                "title": "Build What To Work On Next cockpit",
                                "type": "Issue",
                                "updatedAt": "2026-05-06T04:00:00Z",
                                "url": "https://github.com/cbusillo/launchplane/issues/190",
                                "body": "This body must not be copied.",
                            },
                            "finish Line": "Ranked queue includes Project fields.",
                            "focus": "Now",
                            "id": "PVTI_123",
                            "labels": ["plan", "plan:active"],
                            "manager": "@cellmechanic",
                            "repository": "https://github.com/cbusillo/launchplane",
                            "status": "In Progress",
                            "title": "Build What To Work On Next cockpit",
                        },
                        {
                            "content": {
                                "number": 14,
                                "repository": "cbusillo/code",
                                "title": "Done item",
                                "type": "Issue",
                                "url": "https://github.com/cbusillo/code/issues/14",
                            },
                            "finish Line": "Done plan disappears from active queue.",
                            "labels": ["plan", "plan:done"],
                            "manager": "@cellmechanic",
                            "repository": "https://github.com/cbusillo/code",
                            "status": "Done",
                            "title": "Done item",
                        },
                        {"content": {"type": "DraftIssue", "title": "Skip draft"}},
                    ]
                },
            )
            previous_args = os.environ.get("FAKE_GH_ARGS")
            os.environ["FAKE_GH_ARGS"] = str(args_file)
            try:
                facts = build_github_project_planning_facts(
                    GitHubProjectPlanningFactsConfig(
                        owner="cbusillo",
                        project_number=4,
                        limit=50,
                        gh_binary=str(fake_gh),
                    )
                )
            finally:
                if previous_args is None:
                    os.environ.pop("FAKE_GH_ARGS", None)
                else:
                    os.environ["FAKE_GH_ARGS"] = previous_args
            recorded_args = args_file.read_text().splitlines()[:5]

        self.assertEqual(len(facts), 2)
        self.assertEqual(facts[0].repository, "cbusillo/launchplane")
        self.assertEqual(facts[0].number, 190)
        self.assertEqual(facts[0].title, "Build What To Work On Next cockpit")
        self.assertEqual(facts[0].url, "https://github.com/cbusillo/launchplane/issues/190")
        self.assertEqual(facts[0].focus, "Now")
        self.assertEqual(facts[0].manager, "@cellmechanic")
        self.assertEqual(facts[0].finish_line, "Ranked queue includes Project fields.")
        self.assertEqual(facts[0].labels, ("plan", "plan:active"))
        self.assertEqual(facts[0].updated_at, "2026-05-06T04:00:00Z")
        self.assertIs(facts[0].is_pull_request, False)
        self.assertEqual(facts[1].repository, "cbusillo/code")
        self.assertEqual(facts[1].focus, "Done")
        self.assertEqual(facts[1].state, "closed")
        self.assertEqual(recorded_args, ["project", "item-list", "4", "--owner", "cbusillo"])

    def test_project_facts_include_dependency_subissue_and_pr_check_signals(self) -> None:
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
                                        "number": 302,
                                        "repository": "cbusillo/launchplane",
                                        "title": "Read work graph Project fields",
                                        "type": "PullRequest",
                                        "url": "https://github.com/cbusillo/launchplane/pull/302",
                                    },
                                    "focus": "Next",
                                    "status": "Ready",
                                }
                            ]
                        }
                    },
                    {"stdout": [{"number": 190}]},
                    {"stdout": [{"number": 153}, {"number": 164}]},
                    {"stdout": [{"state": "closed"}, {"state": "open"}]},
                    {
                        "stdout": [
                            {"bucket": "pass", "name": "test"},
                            {"bucket": "pending", "name": "deploy"},
                        ]
                    },
                ],
            )
            previous_args = os.environ.get("FAKE_GH_ARGS")
            os.environ["FAKE_GH_ARGS"] = str(args_file)
            try:
                facts = build_github_project_planning_facts(
                    GitHubProjectPlanningFactsConfig(
                        owner="cbusillo",
                        project_number=4,
                        signal_limit=5,
                        gh_binary=str(fake_gh),
                    )
                )
            finally:
                if previous_args is None:
                    os.environ.pop("FAKE_GH_ARGS", None)
                else:
                    os.environ["FAKE_GH_ARGS"] = previous_args
            recorded_args = args_file.read_text().splitlines()

        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].blocked_by, 1)
        self.assertEqual(facts[0].blocking, 2)
        self.assertEqual(facts[0].subissues_total, 2)
        self.assertEqual(facts[0].subissues_completed, 1)
        self.assertEqual(facts[0].check_state, "pending")
        self.assertIn("repos/cbusillo/launchplane/issues/302/sub_issues", recorded_args)
        self.assertIn("pr", recorded_args)
        self.assertIn("checks", recorded_args)

    def test_project_signal_limit_bounds_extra_github_reads(self) -> None:
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
                                        "number": 190,
                                        "repository": "cbusillo/launchplane",
                                        "title": "First",
                                        "type": "Issue",
                                        "url": "https://github.com/cbusillo/launchplane/issues/190",
                                    }
                                },
                                {
                                    "content": {
                                        "number": 191,
                                        "repository": "cbusillo/launchplane",
                                        "title": "Second",
                                        "type": "Issue",
                                        "url": "https://github.com/cbusillo/launchplane/issues/191",
                                    }
                                },
                            ]
                        }
                    },
                    {"stdout": []},
                    {"stdout": []},
                    {"stdout": []},
                ],
            )
            previous_args = os.environ.get("FAKE_GH_ARGS")
            os.environ["FAKE_GH_ARGS"] = str(args_file)
            try:
                facts = build_github_project_planning_facts(
                    GitHubProjectPlanningFactsConfig(
                        owner="cbusillo",
                        project_number=4,
                        signal_limit=1,
                        gh_binary=str(fake_gh),
                    )
                )
            finally:
                if previous_args is None:
                    os.environ.pop("FAKE_GH_ARGS", None)
                else:
                    os.environ["FAKE_GH_ARGS"] = previous_args
            recorded_args = args_file.read_text().splitlines()

        self.assertEqual(len(facts), 2)
        self.assertEqual(facts[0].blocked_by, 0)
        self.assertIsNone(facts[1].blocked_by)
        self.assertEqual(recorded_args.count("api"), 3)

    def test_project_item_repository_can_be_derived_from_project_field_url(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            directory = Path(temporary_directory_name)
            args_file = directory / "args.txt"
            fake_gh = _write_fake_gh(
                directory,
                stdout={
                    "items": [
                        {
                            "content": {
                                "number": 168,
                                "title": "Adopt intents",
                                "type": "PullRequest",
                                "url": "https://github.com/cbusillo/verireel/pull/168",
                            },
                            "repository": "https://github.com/cbusillo/verireel",
                            "status": "Ready",
                        }
                    ]
                },
            )
            previous_args = os.environ.get("FAKE_GH_ARGS")
            os.environ["FAKE_GH_ARGS"] = str(args_file)
            try:
                facts = build_github_project_planning_facts(
                    GitHubProjectPlanningFactsConfig(
                        owner="cbusillo",
                        project_number=4,
                        gh_binary=str(fake_gh),
                    )
                )
            finally:
                if previous_args is None:
                    os.environ.pop("FAKE_GH_ARGS", None)
                else:
                    os.environ["FAKE_GH_ARGS"] = previous_args

        self.assertEqual(facts[0].repository, "cbusillo/verireel")
        self.assertEqual(facts[0].number, 168)
        self.assertIs(facts[0].is_pull_request, True)

    def test_env_config_is_none_when_project_is_not_configured(self) -> None:
        self.assertIsNone(load_github_project_planning_facts_config_from_env({}))

    def test_env_config_requires_owner_and_project_number_together(self) -> None:
        with self.assertRaises(click.ClickException):
            load_github_project_planning_facts_config_from_env(
                {"LAUNCHPLANE_WORK_GRAPH_PROJECT_OWNER": "cbusillo"}
            )

    def test_env_config_accepts_signal_limit(self) -> None:
        config = load_github_project_planning_facts_config_from_env(
            {
                "LAUNCHPLANE_WORK_GRAPH_PROJECT_OWNER": "cbusillo",
                "LAUNCHPLANE_WORK_GRAPH_PROJECT_NUMBER": "4",
                "LAUNCHPLANE_WORK_GRAPH_PROJECT_SIGNAL_LIMIT": "12",
            }
        )

        self.assertIsNotNone(config)
        assert config is not None
        self.assertEqual(config.signal_limit, 12)

    def test_env_config_rejects_invalid_limit(self) -> None:
        with self.assertRaisesRegex(
            click.ClickException,
            "LAUNCHPLANE_WORK_GRAPH_PROJECT_LIMIT must be a positive integer.",
        ):
            load_github_project_planning_facts_config_from_env(
                {
                    "LAUNCHPLANE_WORK_GRAPH_PROJECT_OWNER": "cbusillo",
                    "LAUNCHPLANE_WORK_GRAPH_PROJECT_NUMBER": "4",
                    "LAUNCHPLANE_WORK_GRAPH_PROJECT_LIMIT": "many",
                }
            )

    def test_failed_gh_call_is_operator_visible(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            directory = Path(temporary_directory_name)
            args_file = directory / "args.txt"
            fake_gh = _write_fake_gh(
                directory,
                stdout={},
                exit_code=1,
                stderr="missing project scope",
            )
            previous_args = os.environ.get("FAKE_GH_ARGS")
            os.environ["FAKE_GH_ARGS"] = str(args_file)
            try:
                with self.assertRaisesRegex(click.ClickException, "missing project scope"):
                    build_github_project_planning_facts(
                        GitHubProjectPlanningFactsConfig(
                            owner="cbusillo",
                            project_number=4,
                            gh_binary=str(fake_gh),
                        )
                    )
            finally:
                if previous_args is None:
                    os.environ.pop("FAKE_GH_ARGS", None)
                else:
                    os.environ["FAKE_GH_ARGS"] = previous_args


if __name__ == "__main__":
    unittest.main()
