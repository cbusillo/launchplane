import os
import subprocess
import unittest

from control_plane.contracts.merge_train_batch import MergeTrainBatchCandidate
from control_plane.merge_train_github import (
    GitHubMergeTrainClient,
    _candidate_required_checks_status,
    _required_checks_observation,
)
from tests.test_merge_train_github import _batch_candidate
from tests.support.workflows import load_workflow
from tests.test_merge_train_head_check_reuse import _ReuseTransport


class _FocusedTransport(_ReuseTransport):
    actions_slug = "github-actions"

    def request(self, *, method: str, path: str, body: dict[str, object] | None = None) -> object:
        response = super().request(method=method, path=path, body=body)
        if "/check-runs" in path and isinstance(response, dict):
            checks = response["check_runs"]
            assert isinstance(checks, list)
            for check in checks:
                assert isinstance(check, dict)
                app = check["app"]
                assert isinstance(app, dict)
                app["slug"] = self.actions_slug
        return response


class FocusedSourceGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.transport = _FocusedTransport()
        self.client = GitHubMergeTrainClient(transport=self.transport)

    def build(self) -> MergeTrainBatchCandidate:
        candidate = _batch_candidate()
        return self.client.build_batch_candidate(
            candidate=candidate.model_copy(update={"entries": candidate.entries[:1]})
        )

    def test_focused_source_qualifies_screening_but_cannot_qualify_candidate_or_reuse(self) -> None:
        self.transport.observed_names = ("pr-ci-gate", "security-gate")
        status, _ = _required_checks_observation(
            transport=self.transport,
            source_screening=True,
            repository_path="example/merge-train-repo",
            encoded_head_sha=self.transport.head_sha,
            base_branch="main",
        )
        self.assertEqual(status, "pass")
        self.assertEqual(
            _candidate_required_checks_status(
                transport=self.transport,
                repository_path="example/merge-train-repo",
                encoded_head_sha=self.transport.head_sha,
                base_branch="main",
            ),
            "pending",
        )
        built = self.build()
        self.assertEqual(built.status, "ready_for_checks")
        self.assertIsNone(built.head_check_reuse)
        self.transport.check_head = built.candidate_sha
        self.transport.status_head = built.candidate_sha
        for names, conclusion, expected in (
            (("pr-ci-gate", "security-gate"), "success", "pending"),
            (("ci-gate", "security-gate", "pr-ci-gate"), "success", "pass"),
            (("ci-gate", "security-gate", "pr-ci-gate"), "failure", "fail"),
        ):
            with self.subTest(names=names, conclusion=conclusion):
                self.transport.observed_names = names
                self.transport.check_conclusion = conclusion
                self.assertEqual(
                    _candidate_required_checks_status(
                        transport=self.transport,
                        repository_path="example/merge-train-repo",
                        encoded_head_sha=built.candidate_sha,
                        base_branch="main",
                    ),
                    expected,
                )

    def test_a_non_actions_focused_gate_cannot_qualify_screening(self) -> None:
        self.transport.observed_names = ("pr-ci-gate", "security-gate")
        self.transport.actions_slug = "other-integration"
        status, _ = _required_checks_observation(
            transport=self.transport,
            source_screening=True,
            repository_path="example/merge-train-repo",
            encoded_head_sha=self.transport.head_sha,
            base_branch="main",
        )
        self.assertEqual(status, "pending")

    def test_focused_source_cannot_hide_failed_full_gate_or_wrong_app(self) -> None:
        for names, app, conclusion in (
            (("pr-ci-gate", "security-gate", "ci-gate"), 15368, "failure"),
            (("pr-ci-gate", "security-gate"), 99, "success"),
            (("pr-ci-gate", "security-gate"), 15368, "skipped"),
            (("pr-ci-gate", "security-gate"), 15368, "failure"),
        ):
            with self.subTest(names=names, app=app):
                self.transport.observed_names = names
                self.transport.check_app = app
                self.transport.check_conclusion = conclusion
                status, _ = _required_checks_observation(
                    transport=self.transport,
                    source_screening=True,
                    repository_path="example/merge-train-repo",
                    encoded_head_sha=self.transport.head_sha,
                    base_branch="main",
                )
                self.assertNotEqual(status, "pass")


class CandidateFullSuiteGateTests(unittest.TestCase):
    def test_candidate_cannot_use_focused_success_to_skip_full_jobs(self) -> None:
        step = load_workflow(".github/workflows/ci.yml").step_named(
            "ci_gate", "Require successful CI path"
        )
        assert step is not None
        step_environment = step.data["env"]
        assert isinstance(step_environment, dict)
        environment = {name: "success" for name in step_environment}
        environment.update(
            BASE_REPOSITORY="example/control-plane",
            HEAD_REPOSITORY="example/control-plane",
            EVENT_NAME="push",
            REF="refs/heads/launchplane/train/example/batch",
            FOCUSED="false",
            FOCUSED_TESTS_RESULT="success",
            VERIFIED_TREE="false",
        )
        for name in ("TEST_RESULT", "POSTGRES_INTEGRATION_RESULT", "FRONTEND_BROWSER_SMOKE_RESULT"):
            for result in ("failure", "skipped", "cancelled", ""):
                with self.subTest(job=name, result=result):
                    completed = subprocess.run(
                        ["bash", "-c", step.run],
                        env=os.environ | environment | {name: result},
                        capture_output=True,
                        text=True,
                    )
                    self.assertNotEqual(completed.returncode, 0, completed.stdout)

    def test_focused_push_is_refused_even_when_all_jobs_report_success(self) -> None:
        step = load_workflow(".github/workflows/ci.yml").step_named(
            "ci_gate", "Require successful CI path"
        )
        assert step is not None
        step_environment = step.data["env"]
        assert isinstance(step_environment, dict)
        environment = {name: "success" for name in step_environment}
        environment.update(
            BASE_REPOSITORY="example/control",
            HEAD_REPOSITORY="example/control",
            EVENT_NAME="push",
            FOCUSED="true",
            VERIFIED_TREE="false",
        )
        completed = subprocess.run(
            ["bash", "-c", step.run], env=os.environ | environment, capture_output=True, text=True
        )
        self.assertNotEqual(completed.returncode, 0)
