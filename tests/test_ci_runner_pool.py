import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from tests.support.workflows import load_workflow


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

    def assert_hosted_pool(self, **overrides: str) -> None:
        result = self.select(**overrides)
        fork_pool = self.select(HEAD_REPOSITORY="outside/fork")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(fork_pool.returncode, 0, fork_pool.stderr)
        labels = json.loads(result.stdout)
        self.assertNotIn("self-hosted", labels)
        self.assertNotIn("configured-ci-lane", labels)
        self.assertEqual(labels, json.loads(fork_pool.stdout))

    def test_public_candidates_bypass_the_self_hosted_queue(self) -> None:
        self.assert_hosted_pool(EVENT_NAME="push", REF="refs/heads/launchplane/train/batch")
        self.assert_hosted_pool(HEAD_REF="launchplane/train/batch")

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
        self.assert_hosted_pool(
            HEAD_REPOSITORY="outside/fork",
            HEAD_REF="launchplane/train/batch",
            RUNNER_LABEL="",
        )

    def test_missing_label_fails_instead_of_using_a_broad_self_hosted_pool(self) -> None:
        result = self.select(RUNNER_LABEL="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not configured", result.stderr)
        self.assert_hosted_pool(HEAD_REF="launchplane/train/batch", RUNNER_LABEL="")

    def test_selector_failure_is_reported_by_both_summary_gates(self) -> None:
        for workflow, job, name in [
            ("ci.yml", "ci_gate", "Require successful CI path"),
            ("security.yml", "security_gate", "Require successful security path"),
        ]:
            step = load_workflow(Path(".github/workflows") / workflow).step_named(job, name)
            assert step is not None
            for status in ["failure", "skipped", "cancelled"]:
                with self.subTest(workflow=workflow, status=status):
                    result = subprocess.run(
                        ["bash", "-c", step.run],
                        env=os.environ | {"RUNNER_POOL_RESULT": status},
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(f"runner_pool concluded with: {status}", result.stderr)

    def test_failed_aggregate_still_fails_the_ci_gate(self) -> None:
        step = load_workflow(".github/workflows/ci.yml").step_named(
            "ci_gate", "Require successful CI path"
        )
        assert step is not None
        for status in ["failure", "skipped"]:
            with self.subTest(status=status):
                result = subprocess.run(
                    ["bash", "-c", step.run],
                    env=os.environ
                    | {
                        "RUNNER_POOL_RESULT": "success",
                        "AUDIT_POLICY_RESULT": "success",
                        "REPOSITORY_PINS_RESULT": "success",
                        "VERIFIED_TREE": "false",
                        "BASE_REPOSITORY": "example/control",
                        "HEAD_REPOSITORY": "example/control",
                        "STATIC_CHECKS_RESULT": "success",
                        "CONTAINER_SCAN_RESULT": "success",
                        "FRONTEND_VALIDATE_RESULT": "success",
                        "FRONTEND_BROWSER_SMOKE_RESULT": "success",
                        "TEST_RESULT": status,
                        "POSTGRES_INTEGRATION_RESULT": "success",
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"test concluded with: {status}", result.stderr)

    def test_runner_label_is_json_encoded(self) -> None:
        label = 'lane"with\\characters'
        self.assert_pool(["self-hosted", label], RUNNER_LABEL=label)


if __name__ == "__main__":
    unittest.main()
