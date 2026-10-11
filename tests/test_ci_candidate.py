import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from tests.support.workflows import load_workflow
from tests.test_merge_train_github import (
    _batch_candidate,
    _combined_status,
    _protected_branch_with_checks,
    _required_check_run,
)
from control_plane.merge_train_github import (
    GitHubMergeTrainClient,
    MergeTrainGitHubError,
    RecordingMergeTrainGitHubTransport,
)


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

    def test_failed_routing_cannot_fall_through_to_tree_reuse(self) -> None:
        step = self.workflow.step_named("ci_gate", "Require successful CI path")
        assert step is not None
        result = subprocess.run(
            ["bash", "-c", step.run],
            env=os.environ
            | {
                "CANDIDATE_CI_RESULT": "failure",
                "VERIFIED_TREE": "true",
                "AUDIT_POLICY_RESULT": "success",
                "REPOSITORY_PINS_RESULT": "success",
            },
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)

    def test_delegation_cannot_replace_missing_required_candidate_ci(self) -> None:
        candidate = _batch_candidate().model_copy(
            update={"candidate_sha": self.sha, "status": "ready_for_checks"}
        )
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _protected_branch_with_checks(),
                _combined_status(),
                {
                    "check_runs": [
                        _required_check_run("candidate-ci-delegated", "completed", "success"),
                        _required_check_run("security-gate", "completed", "success"),
                    ]
                },
            )
        )
        with self.assertRaisesRegex(
            MergeTrainGitHubError, "missing required check evidence: ci-gate"
        ):
            GitHubMergeTrainClient(transport=transport).observe_batch_candidate_checks(
                candidate=candidate
            )
        self.assertTrue(all(self.sha in request.path for request in transport.requests[1:]))

    def test_delegation_never_overrides_the_actual_candidate_gate(self) -> None:
        candidate = _batch_candidate().model_copy(
            update={"candidate_sha": self.sha, "status": "ready_for_checks"}
        )
        for status, conclusion, expected in (
            ("queued", None, "pending"),
            ("completed", "failure", "fail"),
            ("completed", "success", "pass"),
        ):
            with self.subTest(status=status, conclusion=conclusion):
                source = _required_check_run("ci-gate", status, conclusion) | {"id": 1}
                routing = _required_check_run("candidate-ci-delegated", "completed", "success") | {
                    "id": 2
                }
                transport = RecordingMergeTrainGitHubTransport(
                    responses=(
                        _protected_branch_with_checks(),
                        _combined_status(),
                        {
                            "check_runs": [
                                source,
                                routing,
                                _required_check_run("security-gate", "completed", "success"),
                            ]
                        },
                    )
                )
                observed = GitHubMergeTrainClient(
                    transport=transport
                ).observe_batch_candidate_checks(candidate=candidate)
                self.assertEqual(observed.required_checks_status, expected)
