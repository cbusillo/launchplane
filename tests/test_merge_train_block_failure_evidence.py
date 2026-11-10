from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

from control_plane.contracts.merge_train_batch import MergeTrainBatchCandidate
from control_plane.merge_train_candidate_failures import CandidateCheckFailures
from control_plane.merge_train_github import GitHubMergeTrainClient, MergeTrainGitHubError
from tests.test_merge_train_github import _batch_candidate


class FailureHistory:
    def __init__(self, candidate: MergeTrainBatchCandidate) -> None:
        self.candidate = candidate
        repository = candidate.repository
        runs: list[dict[str, Any]] = [
            dict(
                id=run_id,
                workflow_id=7,
                run_number=index,
                run_attempt=1,
                head_sha=candidate.candidate_sha,
                head_branch=candidate.candidate_ref.removeprefix("refs/heads/"),
                repository={"full_name": repository},
                head_repository={"full_name": repository, "id": 9},
                event="push",
                check_suite_id=suite,
                status="completed",
                conclusion="success" if run_id == 43 else "failure",
                run_started_at=f"2026-01-01T10:0{index}:00Z",
            )
            for index, (run_id, suite) in enumerate(((41, 10), (42, 20), (43, 30)), 1)
        ]
        checks: list[dict[str, Any]] = [
            dict(
                id=run["id"] * 10 + index,
                name=name,
                head_sha=candidate.candidate_sha,
                app={"id": 19, "slug": "github-actions"},
                check_suite={"id": run["check_suite_id"]},
                details_url=f"https://github.com/{repository}/actions/runs/{run['id']}/job/{run['id'] * 10 + index}",
                status="completed",
                conclusion=(
                    "failure"
                    if (run["id"], name) in {(41, "ci-gate"), (42, "security-gate")}
                    else "success"
                ),
            )
            for run in runs
            for index, name in enumerate(("ci-gate", "security-gate"), 1)
        ]
        self.phase = 1
        self.runs = runs
        self.checks = checks
        self.job_changes: dict[str, object] = {}
        self.job_attempts: dict[int, int] = {}
        self.status_changes: dict[str, object] = {}
        self.inventory_missing = False
        self.external_changes: dict[str, object] = {}
        self.job_reads = 0

        def request(*, method: str, path: str, body: object = None) -> object:
            assert method == "GET"
            assert body is None
            if "/check-runs?" in path:
                selected = checks[: self.phase * 2]
                selected = [
                    *selected,
                    {
                        "id": 901,
                        "name": "external-gate",
                        "head_sha": candidate.candidate_sha,
                        "app": {"id": 77, "slug": "external"},
                        "check_suite": {"id": 701},
                        "status": "completed",
                        "conclusion": "failure" if self.phase == 2 else "success",
                        **self.external_changes,
                    },
                ]
                return {
                    "total_count": len(selected) + int(self.inventory_missing),
                    "check_runs": selected,
                }
            if "/actions/runs?" in path:
                return {"total_count": self.phase, "workflow_runs": runs[: self.phase]}
            if "/actions/runs/" in path:
                return next(run for run in runs if path.endswith(f"/{run['id']}"))
            if "/annotations?" in path:
                return []
            if path.endswith("/logs"):
                return ""
            if "/actions/jobs/" in path:
                self.job_reads += 1
                check = next(check for check in checks if path.endswith(f"/{check['id']}"))
                run = next(
                    run for run in runs if run["check_suite_id"] == check["check_suite"]["id"]
                )
                return {
                    **check,
                    "run_id": run["id"],
                    "run_attempt": self.job_attempts.get(check["id"], 1),
                    **self.job_changes,
                }
            if "/status?" in path:
                return {
                    "sha": candidate.candidate_sha,
                    "total_count": 1,
                    **self.status_changes,
                    "statuses": [
                        {
                            "id": 800 + self.phase,
                            "context": "status-gate",
                            "state": "failure" if self.phase == 2 else "success",
                        }
                    ],
                }
            raise AssertionError(path)

        class Provider:
            token_permissions = ("actions:read",)

            @staticmethod
            def request(**kwargs: Any) -> object:
                return request(**kwargs)

        self.client = GitHubMergeTrainClient(transport=Provider())

    def observe(self) -> CandidateCheckFailures:
        with patch(
            "control_plane.merge_train_github._required_branch_checks",
            return_value=(
                ("ci-gate", 19),
                ("security-gate", 19),
                ("status-gate", None),
                ("external-gate", 77),
            ),
        ):
            return self.client.read_candidate_check_failures(candidate=self.candidate)


class BlockFailureEvidenceTests(unittest.TestCase):
    def test_partial_or_wrong_execution_evidence_cannot_qualify_a_decision(self) -> None:
        candidate = _batch_candidate().model_copy(update={"candidate_sha": "c" * 40})
        history = FailureHistory(candidate)
        history.phase = 2
        invalid_jobs: tuple[dict[str, object], ...] = (
            {"run_id": 41},
            {"run_attempt": 2},
            {"run_attempt": 0},
            {"run_attempt": True},
            {"id": 999},
            {"head_sha": "d" * 40},
            {"conclusion": "success"},
        )
        for changes in invalid_jobs:
            with self.subTest(job=changes):
                history.job_changes = changes
                result = history.observe()
                self.assertTrue(result.conclusive)
                evidence = result.decision_evidence
                assert evidence is not None
                self.assertFalse(evidence.checks)
                self.assertTrue(evidence.unavailable_reason)
        history.job_changes = {}
        history.inventory_missing = True
        evidence = history.observe().decision_evidence
        assert evidence is not None
        self.assertFalse(evidence.checks)
        self.assertTrue(evidence.unavailable_reason)
        history.inventory_missing = False
        history.status_changes = {"total_count": 2}
        evidence = history.observe().decision_evidence
        assert evidence is not None
        self.assertFalse(evidence.checks)
        self.assertTrue(evidence.unavailable_reason)
        history.status_changes = {}
        history.runs[1]["head_branch"] = "main"
        evidence = history.observe().decision_evidence
        assert evidence is not None
        self.assertFalse(evidence.checks)
        self.assertTrue(evidence.unavailable_reason)

    def test_malformed_origins_leave_no_partial_decision_evidence(self) -> None:
        history = FailureHistory(_batch_candidate().model_copy(update={"candidate_sha": "c" * 40}))
        history.phase = 2
        history.external_changes = {"check_suite": None}
        evidence = history.observe().decision_evidence
        assert evidence is not None
        self.assertFalse(evidence.checks)
        self.assertTrue(evidence.unavailable_reason)
        history.external_changes = {}
        malformed_jobs: tuple[object, ...] = (None, [], {"id": 422})
        for job in malformed_jobs:
            failures = history.observe()
            with (
                self.subTest(job=job),
                patch.object(history.client.transport, "request", return_value=job),
            ):
                evidence = failures.decision_evidence
            assert evidence is not None
            self.assertFalse(evidence.checks)
            self.assertTrue(evidence.unavailable_reason)

    def test_job_origin_keeps_its_own_attempt_within_the_observed_execution(self) -> None:
        history = FailureHistory(_batch_candidate().model_copy(update={"candidate_sha": "c" * 40}))
        history.runs[0]["run_attempt"] = 2
        history.checks[1]["id"] = 512
        history.checks[1]["details_url"] = (
            f"https://github.com/{history.candidate.repository}/actions/runs/41/job/512"
        )
        history.checks[1]["conclusion"] = "failure"
        history.job_attempts[512] = 2
        evidence = history.observe().decision_evidence
        assert evidence is not None
        self.assertEqual(
            {(check.provider_id, check.run_id, check.run_attempt) for check in evidence.checks},
            {(411, 41, 1), (512, 41, 2)},
        )
        history.job_changes = {"run_attempt": 3}
        evidence = history.observe().decision_evidence
        assert evidence is not None
        self.assertFalse(evidence.checks)
        self.assertTrue(evidence.unavailable_reason)

    def test_manual_candidate_failure_retains_authenticated_origin(self) -> None:
        history = FailureHistory(_batch_candidate().model_copy(update={"candidate_sha": "c" * 40}))
        history.runs[0]["event"] = "workflow_dispatch"
        failures = history.observe()
        evidence = failures.decision_evidence
        assert evidence is not None
        self.assertEqual(
            {check.name for check in evidence.checks},
            {"ci-gate"},
        )

    def test_running_execution_defers_capture_until_its_origin_is_complete(self) -> None:
        history = FailureHistory(_batch_candidate().model_copy(update={"candidate_sha": "c" * 40}))
        history.runs[0]["status"] = "in_progress"
        failures = history.observe()
        self.assertTrue(failures.conclusive)
        with self.assertRaises(MergeTrainGitHubError):
            _ = failures.decision_evidence
        self.assertEqual(history.job_reads, 0)
        history.runs[0]["status"] = "completed"
        evidence = history.observe().decision_evidence
        assert evidence is not None
        self.assertTrue(evidence.checks)

    def test_external_suite_and_current_selection_must_be_proved(self) -> None:
        history = FailureHistory(_batch_candidate().model_copy(update={"candidate_sha": "c" * 40}))
        history.phase = 2
        history.external_changes = {"check_suite": {}}
        evidence = history.observe().decision_evidence
        assert evidence is not None
        self.assertFalse(evidence.checks)
        self.assertTrue(evidence.unavailable_reason)
        history.external_changes = {}
        with patch(
            "control_plane.merge_train_github._without_superseded_actions_checks",
            side_effect=lambda **kwargs: (kwargs["check_runs"], "unavailable"),
        ):
            evidence = history.observe().decision_evidence
        assert evidence is not None
        self.assertFalse(evidence.checks)
        self.assertTrue(evidence.unavailable_reason)
