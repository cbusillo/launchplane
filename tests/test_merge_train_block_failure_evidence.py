from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

from control_plane.contracts.merge_train_batch import MergeTrainBatchCandidate
from control_plane.merge_train_candidate_failures import CandidateCheckFailures
from control_plane.merge_train_github import GitHubMergeTrainClient
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
        self.status_changes: dict[str, object] = {}
        self.inventory_missing = False

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
            if "/actions/jobs/" in path:
                check = next(check for check in checks if path.endswith(f"/{check['id']}"))
                run = next(
                    run for run in runs if run["check_suite_id"] == check["check_suite"]["id"]
                )
                return {
                    **check,
                    "run_id": run["id"],
                    "run_attempt": run["run_attempt"],
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

            def request(self, **kwargs: Any) -> object:
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
    def test_decision_keeps_repeat_failures_after_a_later_pass(self) -> None:
        candidate = _batch_candidate().model_copy(
            update={"candidate_sha": "c" * 40, "check_rerun_attempts": {41: 1}}
        )
        history = FailureHistory(candidate)
        initial = history.observe()
        history.phase = 2
        repeat = history.observe()
        history.phase = 3
        current = history.observe()
        self.assertEqual([item.name for item in initial.attribution_requirements], ["ci-gate"])
        self.assertFalse(current.conclusive)
        evidence = repeat.decision_evidence
        self.assertIsNotNone(evidence)
        assert evidence is not None
        self.assertEqual(
            {check.name for check in evidence.checks},
            {"security-gate", "status-gate", "external-gate"},
        )
        security = next(check for check in evidence.checks if check.run_id is not None)
        self.assertEqual(
            (security.run_id, security.run_attempt, security.check_suite_id), (42, 1, 20)
        )
        status = next(check for check in evidence.checks if check.source == "commit_status")
        self.assertEqual(status.provider_id, 802)
        self.assertEqual(evidence.candidate_ref, candidate.candidate_ref)
        self.assertEqual(evidence.candidate_sha, candidate.candidate_sha)
        self.assertEqual(evidence.repository, candidate.repository)

    def test_partial_or_wrong_execution_evidence_cannot_qualify_a_decision(self) -> None:
        candidate = _batch_candidate().model_copy(update={"candidate_sha": "c" * 40})
        history = FailureHistory(candidate)
        history.phase = 2
        for changes in (
            {"run_id": 41},
            {"run_attempt": 2},
            {"id": 999},
            {"head_sha": "d" * 40},
            {"conclusion": "success"},
        ):
            with self.subTest(job=changes):
                history.job_changes = dict(changes)
                result = history.observe()
                self.assertTrue(result.conclusive)
                self.assertIsNone(result.decision_evidence)
        history.job_changes = {}
        history.inventory_missing = True
        self.assertIsNone(history.observe().decision_evidence)
        history.inventory_missing = False
        history.status_changes = {"total_count": 2}
        self.assertIsNone(history.observe().decision_evidence)
        history.status_changes = {}
        history.runs[1]["head_branch"] = "main"
        self.assertIsNone(history.observe().decision_evidence)
