from __future__ import annotations

import json
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import unittest

import click

from control_plane.work_graph_github_projects import (
    GitHubProjectPlanningFactsConfig,
    build_github_project_planning_facts,
    load_github_project_planning_facts_config_from_env,
)


def _write_fake_gh(
    directory: Path, *, stdout: object, exit_code: int = 0, stderr: str = ""
) -> Path:
    script = directory / "gh"
    script.write_text(
        "#!/bin/sh\n"
        'printf \'%s\n\' "$@" > "$FAKE_GH_ARGS"\n'
        f"printf '%s' {json.dumps(json.dumps(stdout))}\n"
        + (f"printf '%s' {json.dumps(stderr)} >&2\n" if stderr else "")
        + f"exit {exit_code}\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


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
