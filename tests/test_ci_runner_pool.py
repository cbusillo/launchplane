import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest


class CIRunnerPoolTests(unittest.TestCase):
    def select(self, **overrides: str) -> subprocess.CompletedProcess[str]:
        with TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            result = subprocess.run(
                ["bash", "scripts/select-ci-runner.sh"],
                env=os.environ
                | {
                    "EVENT_NAME": "pull_request",
                    "REF": "refs/pull/1/merge",
                    "HEAD_REF": "work/example",
                    "REPOSITORY": "example/control",
                    "HEAD_REPOSITORY": "example/control",
                    "REPOSITORY_VISIBILITY": "public",
                    "RUNNER_LABEL": "configured-ci-lane",
                    "GITHUB_OUTPUT": str(output),
                }
                | overrides,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0:
                result.stdout = output.read_text().removeprefix("runner=").strip()
            return result

    def assert_pool(self, expected: list[str], **overrides: str) -> None:
        result = self.select(**overrides)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), expected)

    def test_public_candidates_bypass_the_self_hosted_queue(self) -> None:
        self.assert_pool(
            ["ubuntu-latest"], EVENT_NAME="push", REF="refs/heads/launchplane/train/batch"
        )
        self.assert_pool(["ubuntu-latest"], HEAD_REF="launchplane/train/batch")

    def test_ordinary_work_keeps_its_configured_lane(self) -> None:
        for event in ["pull_request", "push", "schedule", "workflow_dispatch"]:
            with self.subTest(event=event):
                self.assert_pool(
                    ["self-hosted", "configured-ci-lane"], EVENT_NAME=event, REF="refs/heads/main"
                )

    def test_private_candidates_do_not_introduce_paid_hosted_work(self) -> None:
        for event, ref in [
            ("push", "refs/heads/launchplane/train/batch"),
            ("pull_request", "refs/pull/1/merge"),
        ]:
            with self.subTest(event=event):
                self.assert_pool(
                    ["self-hosted", "configured-ci-lane"],
                    EVENT_NAME=event,
                    REF=ref,
                    HEAD_REF="launchplane/train/batch",
                    REPOSITORY_VISIBILITY="private",
                )

    def test_fork_branches_never_reach_the_private_lane(self) -> None:
        self.assert_pool(
            ["ubuntu-latest"],
            HEAD_REPOSITORY="outside/fork",
            HEAD_REF="launchplane/train/batch",
            RUNNER_LABEL="",
        )

    def test_missing_label_fails_instead_of_using_a_broad_self_hosted_pool(self) -> None:
        result = self.select(RUNNER_LABEL="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not configured", result.stderr)
        self.assert_pool(["ubuntu-latest"], HEAD_REF="launchplane/train/batch", RUNNER_LABEL="")

    def test_runner_label_is_json_encoded(self) -> None:
        label = 'lane"with\\characters'
        self.assert_pool(["self-hosted", label], RUNNER_LABEL=label)


if __name__ == "__main__":
    unittest.main()
