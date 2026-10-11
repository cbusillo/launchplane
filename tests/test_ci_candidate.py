import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from tests.support.workflows import load_workflow


class CandidateCiWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = load_workflow(".github/workflows/ci.yml")
        self.repository = "example/control-plane"
        self.sha = "a" * 40
        self.branch = "launchplane/train/example/batch"

    def route(self, *, event: str = "pull_request", **head: str) -> dict[str, str]:
        step = self.workflow.step_named("candidate_ci", "Route candidate CI")
        assert step is not None
        with TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / "event.json"
            payload.write_text(
                json.dumps(
                    {
                        "pull_request": {
                            "head": {
                                "repo": {"full_name": head.get("repository", self.repository)},
                                "ref": head.get("branch", self.branch),
                                "sha": head.get("sha", self.sha),
                            }
                        }
                    }
                )
            )
            output = root / "output"
            subprocess.run(
                ["bash", "-c", step.run],
                env=os.environ
                | {
                    "EVENT_NAME": event,
                    "REPOSITORY": self.repository,
                    "GITHUB_EVENT_PATH": str(payload),
                    "GITHUB_OUTPUT": str(output),
                },
                capture_output=True,
                text=True,
                check=True,
            )
            return dict(line.split("=", 1) for line in output.read_text().splitlines())

    def test_only_same_repository_train_pr_delegates(self) -> None:
        self.assertEqual(
            self.route(), {"sha": self.sha, "branch": self.branch, "delegated": "true"}
        )
        for parameters in (
            {"event": "push"},
            {"event": "schedule"},
            {"repository": "fork/control-plane"},
            {"branch": "feature"},
            {"branch": "launchplane/construct/batch"},
            {"sha": "invalid"},
        ):
            with self.subTest(parameters=parameters):
                self.assertEqual(self.route(**parameters), {"delegated": "false"})
