import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from control_plane.contracts.merge_train_batch import MergeTrainBatchCandidate
from control_plane.merge_admission import MergeAdmissionEvaluator
from control_plane.merge_train_controller_run_once import (
    MergeTrainControllerRunOnceEnvelope,
    execute_merge_train_controller_run_once,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.merge_train_github import (
    GitHubMergeTrainClient,
    GitHubMergeTrainSnapshotReader,
    MergeTrainGitHubPolicyUnavailableError,
    _candidate_required_checks_status,
    _required_checks_observation,
)
from tests.test_merge_train_github import (
    _batch_candidate,
    _protected_branch_with_checks,
    _combined_status,
    _github_branch,
    _github_pull_request,
    _conversation_rule,
    _required_check_run,
)
from tests.support.merge_train_github import RecordingMergeTrainGitHubTransport
from tests.merge_train_policy_fixtures import build_test_merge_train_policy
from tests.test_github_required_checks import HEAD, _superseded_with_unrelated_running
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

    def test_controller_plans_focused_ready_head_for_full_candidate_checks(self) -> None:
        head = "a" * 40
        repository = "example/merge-train-repo"
        policy = build_test_merge_train_policy(repository=repository)
        pull = _github_pull_request(
            1,
            repository=repository,
            author_association="OWNER",
            base_sha="base-main",
            head_sha=head,
        )
        pull["labels"] = []
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _github_branch(sha="base-main"),
                [pull],
                [
                    {
                        "id": 1,
                        "body": f"Ready for the merge train\n\nHead: {head}",
                        "user": {"id": 20, "login": "example"},
                        "created_at": "2026-10-16T10:00:00Z",
                    }
                ],
                pull,
                _protected_branch_with_checks("ci-gate"),
                [],
                _combined_status(),
                {
                    "check_runs": [
                        _required_check_run("pr-ci-gate", "completed", "success")
                        | {"app": {"id": 15368, "slug": "github-actions"}}
                    ]
                },
                _conversation_rule(),
                [],
                [{"filename": "application.py"}],
                pull,
            )
        )
        with (
            TemporaryDirectory() as directory,
            patch(
                "control_plane.merge_train_controller_run_once.UrllibMergeTrainGitHubTransport",
                return_value=transport,
            ),
            patch("control_plane.merge_train_codeowners._read_patterns", return_value=()),
        ):
            store = FilesystemRecordStore(Path(directory))
            result = execute_merge_train_controller_run_once(
                request=MergeTrainControllerRunOnceEnvelope(repository=repository),
                policy=policy,
                policy_sha256=policy.policy_sha256,
                repository_policy=policy.policies[0],
                token="fixture",
                trace_id="focused-controller",
                recorded_at="2026-10-16T10:00:00Z",
                candidate_store=store,
                landing_store=store,
                stack_collapse_store=store,
                controller_state_store=store,
                admission_store=store,
                admission_evaluator=Mock(spec=MergeAdmissionEvaluator),
            )
        self.assertEqual(
            result.accepted_result["controller_action"], "plan_candidate", result.accepted_result
        )
        candidate = MergeTrainBatchCandidate.model_validate(result.accepted_result["candidate"])
        self.assertEqual(candidate.entries[0].head_sha, head)
        self.assertEqual(candidate.status, "planned")

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

    def test_default_and_nonmerge_readers_require_full_evidence(self) -> None:
        self.transport.observed_names = ("pr-ci-gate", "security-gate")
        policy = build_test_merge_train_policy(repository="example/merge-train-repo").policies[0]
        for opted_in, method, expected in (
            (False, "merge", "pending"),
            (True, "merge", "pass"),
            (True, "squash", "pending"),
        ):
            with self.subTest(opted_in=opted_in, method=method):
                reader = GitHubMergeTrainSnapshotReader(
                    transport=self.transport,
                    source_screening=opted_in,
                    repository_policy=policy.model_copy(update={"merge_method": method}),
                )
                status, _ = reader._required_checks_observation(
                    repository_path="example/merge-train-repo",
                    head_sha=self.transport.head_sha,
                    base_branch="main",
                )
                self.assertEqual(status, expected)

    def test_current_individual_routing_restores_full_source_requirement(self) -> None:
        self.transport.observed_names = ("pr-ci-gate", "security-gate")
        policy = build_test_merge_train_policy(repository="example/merge-train-repo").policies[0]
        reader = GitHubMergeTrainSnapshotReader(
            transport=self.transport, source_screening=True, repository_policy=policy
        )
        from control_plane.merge_train import MergeTrainPullRequestSnapshot

        pull = MergeTrainPullRequestSnapshot(
            number=1,
            created_at="2026-01-01T00:00:00Z",
            head_sha=self.transport.head_sha,
            base_repository="example/merge-train-repo",
            required_checks_status="pass",
            requires_individual_landing=True,
        )
        strict = reader._require_full_individual_checks(
            repository_path="example/merge-train-repo",
            base_branch="main",
            base_sha=self.transport.base_sha,
            pull_requests=(pull,),
        )[0]
        self.assertEqual(strict.required_checks_status, "pending")
        self.transport.observed_names = ("ci-gate", "security-gate")
        full = reader._require_full_individual_checks(
            repository_path="example/merge-train-repo",
            base_branch="main",
            base_sha=self.transport.base_sha,
            pull_requests=(pull,),
        )[0]
        self.assertEqual(full.required_checks_status, "pass")
        with patch(
            "control_plane.merge_train_github._required_checks_observation",
            side_effect=MergeTrainGitHubPolicyUnavailableError("Policy unavailable"),
        ):
            unknown = reader._require_full_individual_checks(
                repository_path="example/merge-train-repo",
                base_branch="main",
                base_sha=self.transport.base_sha,
                pull_requests=(pull,),
            )[0]
        self.assertEqual(unknown.required_checks_status, "unknown")
        self.assertTrue(unknown.required_checks_observation_error)

    def test_new_focused_suite_supersedes_an_old_failed_suite_with_provider_proof(self) -> None:
        checks, runs = _superseded_with_unrelated_running()
        for check in checks[:2]:
            check["name"] = "pr-ci-gate"
        transport = RecordingMergeTrainGitHubTransport(
            responses=(
                _protected_branch_with_checks("ci-gate"),
                [],
                {**_combined_status(statuses=()), "sha": HEAD},
                {"check_runs": checks},
                runs,
            )
        )
        status, _ = _required_checks_observation(
            transport=transport,
            repository_path="example/tool",
            encoded_head_sha=HEAD,
            base_branch="main",
            source_screening=True,
        )
        self.assertEqual(status, "pass")

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
    def setUp(self) -> None:
        step = load_workflow(".github/workflows/ci.yml").step_named(
            "ci_gate", "Require successful CI path"
        )
        assert step is not None
        self.program = step.run
        step_environment = step.data["env"]
        assert isinstance(step_environment, dict)
        self.environment = {name: "success" for name in step_environment}
        self.environment.update(
            BASE_REPOSITORY="example/control",
            HEAD_REPOSITORY="example/control",
            EVENT_NAME="push",
            REF="refs/heads/launchplane/train/example/batch",
            FOCUSED="false",
            VERIFIED_TREE="false",
        )

    def gate(self, **overrides: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "-c", self.program],
            env=os.environ | self.environment | overrides,
            capture_output=True,
            text=True,
        )

    def test_candidate_cannot_use_focused_success_to_skip_full_jobs(self) -> None:
        for name in ("TEST_RESULT", "POSTGRES_INTEGRATION_RESULT", "FRONTEND_BROWSER_SMOKE_RESULT"):
            for result in ("failure", "skipped", "cancelled", ""):
                with self.subTest(job=name, result=result):
                    completed = self.gate(**{name: result})
                    self.assertNotEqual(completed.returncode, 0, completed.stdout)

    def test_focused_pr_and_manual_full_run_reject_missing_required_work(self) -> None:
        for event, focused, required in (
            ("pull_request", "true", "FOCUSED_TESTS_RESULT"),
            ("workflow_dispatch", "false", "TEST_RESULT"),
        ):
            for status in ("success", "failure", "skipped", "cancelled"):
                with self.subTest(event=event, status=status):
                    result = self.gate(
                        **{"EVENT_NAME": event, "FOCUSED": focused, required: status}
                    )
                    self.assertEqual(result.returncode == 0, status == "success", result.stderr)

    def test_focused_push_is_refused_even_when_all_jobs_report_success(self) -> None:
        self.assertNotEqual(self.gate(FOCUSED="true").returncode, 0)
