from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from control_plane.contracts.engineering_review_run import (
    EngineeringReviewRunAssignment,
    EngineeringReviewRunClaimRequest,
    EngineeringReviewRunView,
    EngineeringReviewRunWorkerFailure,
    EngineeringReviewRunWorkerUpdate,
    claim_engineering_review_run,
    engineering_review_run_view,
)
from control_plane.engineering_review_worker import (
    _failure_summary,
    _remaining_lease_seconds,
    _verify_worktree,
    run_engineering_review_worker_once,
)
from control_plane.contracts.every_code_work_request import EveryCodeWorkRequestRecord
from tests.test_engineering_review_run import HEAD_SHA, TREE_SHA, _pending_record


class _WorkerStore:
    def __init__(self, assignment: EngineeringReviewRunAssignment) -> None:
        self.assignment = assignment
        self.current = assignment.run

    def expire_stale(self) -> int:
        return 0

    def list_pending(self, **_filters: str) -> tuple[EngineeringReviewRunView, ...]:
        return (self.assignment.run,)

    def claim(self, request: EngineeringReviewRunClaimRequest) -> EngineeringReviewRunAssignment:
        return self.assignment

    def start(self, request: EngineeringReviewRunWorkerUpdate) -> EngineeringReviewRunView:
        return self.assignment.run.model_copy(update={"state": "running"})

    def read(self, run_id: str) -> EngineeringReviewRunView:
        return self.current

    def fail(self, request: EngineeringReviewRunWorkerFailure) -> EngineeringReviewRunView:
        return self.assignment.run.model_copy(update={"state": "failed"})

    def read_work_request(self, request_id: str) -> EveryCodeWorkRequestRecord:
        return EveryCodeWorkRequestRecord(
            request_id="request", lifecycle_id="lifecycle", source="manual", state="done",
            repository="example/repository", issue_number=42,
            issue_url="https://github.com/example/repository/issues/42",
            trigger_label="every-code", queued_at="2026-08-06T00:00:00Z",
            updated_at="2026-08-06T00:00:00Z", claimed_at="2026-08-06T00:00:00Z",
            claimed_by_host="worker", started_at="2026-08-06T00:00:00Z",
            finished_at="2026-08-06T00:00:00Z",
            result_pr_url="https://github.com/example/repository/pull/42",
        )


class EngineeringReviewWorkerTests(unittest.TestCase):
    def test_worktree_verification_requires_exact_clean_tree(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            worktree = Path(temporary_directory)
            with patch(
                "control_plane.engineering_review_worker.subprocess.run",
                side_effect=(
                    subprocess.CompletedProcess((), 0, f"{HEAD_SHA}\n", ""),
                    subprocess.CompletedProcess((), 0, f"{TREE_SHA}\n", ""),
                    subprocess.CompletedProcess((), 0, "", ""),
                ),
            ):
                _verify_worktree(worktree, HEAD_SHA, TREE_SHA)

            with patch(
                "control_plane.engineering_review_worker.subprocess.run",
                side_effect=(
                    subprocess.CompletedProcess((), 0, f"{HEAD_SHA}\n", ""),
                    subprocess.CompletedProcess((), 0, f"{TREE_SHA}\n", ""),
                    subprocess.CompletedProcess((), 0, "?? injected.txt\n", ""),
                ),
            ), self.assertRaisesRegex(RuntimeError, "not clean"):
                _verify_worktree(worktree, HEAD_SHA, TREE_SHA)

    def test_lease_timeout_and_failure_summary_are_bounded(self) -> None:
        future = (datetime.now(UTC) + timedelta(seconds=30)).isoformat()
        self.assertGreater(_remaining_lease_seconds(future), 0)
        with self.assertRaisesRegex(RuntimeError, "expired"):
            _remaining_lease_seconds("2026-01-01T00:00:00Z")
        self.assertNotIn("secret-value", _failure_summary(ValueError("secret-value")))
        self.assertEqual(
            _failure_summary(subprocess.TimeoutExpired(("code",), 1)),
            "Engineering review process exceeded its scoped lease.",
        )

    def test_worker_pins_binary_model_and_scopes_environment(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            executable = Path(temporary_directory) / "code"
            executable.write_bytes(b"controlled-review-binary")
            pending, credential = _pending_record()
            pending = pending.model_copy(
                update={
                    "executable_path": str(executable),
                    "binary_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
                }
            )
            claimed = claim_engineering_review_run(
                pending, claimed_at="2026-08-06T00:00:00Z"
            )
            assignment = EngineeringReviewRunAssignment(
                run=engineering_review_run_view(claimed),
                credential=credential,
                worker_host=claimed.worker_host,
                executable_path=str(executable),
            )
            store = _WorkerStore(assignment)
            captured_command: list[str] = []
            captured_environment: dict[str, str] = {}

            def runner(command, cwd, environment):  # type: ignore[no-untyped-def]
                nonlocal captured_command, captured_environment
                captured_command = list(command)
                captured_environment = dict(environment)
                store.current = assignment.run.model_copy(update={"state": "completed"})
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "broad-worker-token",
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_TOKEN": "broad-github-token",
                },
            ), patch(
                "control_plane.engineering_review_worker.every_code_worktree_root",
                return_value=Path(temporary_directory),
            ), patch("control_plane.engineering_review_worker._verify_worktree"):
                result = run_engineering_review_worker_once(
                    store=store,
                    worker_runtime_id=claimed.worker_runtime_id,
                    worker_host=claimed.worker_host,
                    state_dir=Path(temporary_directory),
                    service_url="https://launchplane.example.test",
                    runner=runner,
                )

        self.assertEqual(captured_command[0], str(executable))
        self.assertEqual(
            captured_command[captured_command.index("--model") + 1], claimed.model_id
        )
        self.assertNotIn("LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN", captured_environment)
        self.assertNotIn("LAUNCHPLANE_EVERY_CODE_GITHUB_TOKEN", captured_environment)
        self.assertEqual(
            captured_environment["LAUNCHPLANE_ENGINEERING_REVIEW_CREDENTIAL"], credential
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.state, "completed")


if __name__ == "__main__":
    unittest.main()
