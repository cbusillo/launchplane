from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import os
from pathlib import Path
import secrets
import subprocess
from typing import Callable, Mapping, Protocol, Sequence

from control_plane.contracts.engineering_review_run import (
    EngineeringReviewRunAssignment,
    EngineeringReviewRunClaimRequest,
    EngineeringReviewRunView,
    EngineeringReviewRunWorkerFailure,
    EngineeringReviewRunWorkerUpdate,
)
from control_plane.contracts.every_code_work_request import EveryCodeWorkRequestRecord
from control_plane.every_code_worker import (
    EVERY_CODE_GITHUB_TOKEN_ENV_KEY,
    EveryCodeWorkerApiStore,
    every_code_worktree_root,
)


class EngineeringReviewWorkerStore(Protocol):
    def list_pending(self, *, worker_runtime_id: str, worker_host: str) -> tuple[EngineeringReviewRunView, ...]: ...
    def claim(self, request: EngineeringReviewRunClaimRequest) -> EngineeringReviewRunAssignment: ...
    def start(self, request: EngineeringReviewRunWorkerUpdate) -> EngineeringReviewRunView: ...
    def read(self, run_id: str) -> EngineeringReviewRunView: ...
    def fail(self, request: EngineeringReviewRunWorkerFailure) -> EngineeringReviewRunView: ...
    def expire_stale(self) -> int: ...
    def read_work_request(self, request_id: str) -> EveryCodeWorkRequestRecord: ...


Runner = Callable[
    [Sequence[str], Path, Mapping[str, str]],
    subprocess.CompletedProcess[str],
]


@dataclass(frozen=True)
class EngineeringReviewWorkerApiStore:
    service_url: str
    worker_token: str

    def _api(self) -> EveryCodeWorkerApiStore:
        return EveryCodeWorkerApiStore(
            service_url=self.service_url,
            worker_token=self.worker_token,
        )

    def list_pending(
        self, *, worker_runtime_id: str, worker_host: str
    ) -> tuple[EngineeringReviewRunView, ...]:
        payload = self._api()._request("GET", "/v1/engineering-review-runs/pending")
        raw_runs = payload.get("runs")
        if not isinstance(raw_runs, list):
            raise RuntimeError("Engineering review pending response is invalid.")
        runs = tuple(EngineeringReviewRunView.model_validate(item) for item in raw_runs)
        if any(run.worker_runtime_id != worker_runtime_id for run in runs):
            raise RuntimeError("Engineering review service returned another worker runtime.")
        return runs

    def claim(self, request: EngineeringReviewRunClaimRequest) -> EngineeringReviewRunAssignment:
        payload = self._api()._request(
            "POST", "/v1/engineering-review-runs/claim", body=request.model_dump(mode="json")
        )
        return EngineeringReviewRunAssignment.model_validate(payload["assignment"])

    def start(self, request: EngineeringReviewRunWorkerUpdate) -> EngineeringReviewRunView:
        payload = self._api()._request(
            "POST", "/v1/engineering-review-runs/start", body=request.model_dump(mode="json")
        )
        return EngineeringReviewRunView.model_validate(payload["run"])

    def read(self, run_id: str) -> EngineeringReviewRunView:
        payload = self._api()._request("GET", f"/v1/engineering-review-runs/{run_id}")
        return EngineeringReviewRunView.model_validate(payload["run"])

    def fail(self, request: EngineeringReviewRunWorkerFailure) -> EngineeringReviewRunView:
        payload = self._api()._request(
            "POST", "/v1/engineering-review-runs/fail", body=request.model_dump(mode="json")
        )
        return EngineeringReviewRunView.model_validate(payload["run"])

    def expire_stale(self) -> int:
        payload = self._api()._request("POST", "/v1/engineering-review-runs/expire-stale", body={})
        expired_count = payload.get("expired_count")
        if not isinstance(expired_count, int):
            raise RuntimeError("Engineering review maintenance response is invalid.")
        return expired_count

    def read_work_request(self, request_id: str) -> EveryCodeWorkRequestRecord:
        return self._api().read_every_code_work_request_record(request_id)


def run_engineering_review_worker_once(
    *,
    store: EngineeringReviewWorkerStore,
    worker_runtime_id: str,
    worker_host: str,
    state_dir: Path,
    service_url: str,
    runner: Runner | None = None,
) -> EngineeringReviewRunView | None:
    store.expire_stale()
    pending = store.list_pending(
        worker_runtime_id=worker_runtime_id,
        worker_host=worker_host,
    )
    if not pending:
        return None
    selected = pending[0]
    assignment = store.claim(
        EngineeringReviewRunClaimRequest(
            run_id=selected.run_id,
        )
    )
    run = assignment.run
    if run.worker_runtime_id != worker_runtime_id or assignment.worker_host != worker_host:
        raise RuntimeError("Engineering review assignment does not match this worker identity.")
    executable = Path(assignment.executable_path)
    try:
        _verify_executable(executable, run.binary_sha256)
        work_request = store.read_work_request(run.work_request_id)
        worktree = every_code_worktree_root(work_request, state_dir=state_dir)
        _verify_worktree(worktree, run.head_sha, run.tree_sha)
        update = EngineeringReviewRunWorkerUpdate(
            run_id=run.run_id,
            fencing_token=run.fencing_token,
        )
        store.start(update)
        command = (
            str(executable),
            "--model", run.model_id,
            "--sandbox", "read-only",
            "--ask-for-approval", "never",
            "--cd", str(worktree),
            "exec",
            _review_prompt(run.run_id),
        )
        environment = _review_environment(
            service_url=service_url,
            credential=assignment.credential,
        )
        if runner is None:
            completed = _run(
                command,
                worktree,
                environment,
                timeout_seconds=_remaining_lease_seconds(run.lease_expires_at),
            )
        else:
            completed = runner(command, worktree, environment)
        current = store.read(run.run_id)
        if current.state == "completed":
            return current
        if completed.returncode != 0:
            summary = f"Reviewer process exited with status {completed.returncode}."
        else:
            summary = "Reviewer process exited without submitting bounded review output."
        return store.fail(
            EngineeringReviewRunWorkerFailure(
                **update.model_dump(),
                error_code="reviewer_process_failed",
                summary=summary,
            )
        )
    except Exception as error:
        return store.fail(
            EngineeringReviewRunWorkerFailure(
                run_id=run.run_id,
                fencing_token=run.fencing_token,
                error_code="review_worker_failed",
                summary=_failure_summary(error),
            )
        )


def _verify_executable(executable: Path, expected_sha256: str) -> None:
    if not executable.is_absolute() or not executable.is_file():
        raise RuntimeError("Configured engineering review executable is unavailable.")
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    if not secrets.compare_digest(digest, expected_sha256):
        raise RuntimeError("Engineering review executable SHA-256 does not match authority.")


def _verify_worktree(worktree: Path, expected_head_sha: str, expected_tree_sha: str) -> None:
    if not worktree.is_dir():
        raise RuntimeError("Existing Every Code PR worktree is unavailable.")
    result = subprocess.run(
        ("/usr/bin/git", "-C", str(worktree), "rev-parse", "HEAD"),
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    if result.stdout.strip().lower() != expected_head_sha:
        raise RuntimeError("Existing Every Code PR worktree is not at the exact GitHub head.")
    tree_result = subprocess.run(
        ("/usr/bin/git", "-C", str(worktree), "rev-parse", "HEAD^{tree}"),
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    if tree_result.stdout.strip().lower() != expected_tree_sha:
        raise RuntimeError("Existing Every Code PR worktree tree does not match GitHub authority.")
    status_result = subprocess.run(
        (
            "/usr/bin/git",
            "-C",
            str(worktree),
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ),
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    if status_result.stdout:
        raise RuntimeError("Existing Every Code PR worktree is not clean.")


def _review_environment(*, service_url: str, credential: str) -> dict[str, str]:
    allowed = {key: value for key, value in os.environ.items() if key in {
        "HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "SSL_CERT_FILE",
        "SSL_CERT_DIR", "CODE_HOME", "CODEX_HOME",
    }}
    allowed["PATH"] = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"
    allowed["LAUNCHPLANE_ENGINEERING_REVIEW_URL"] = service_url.rstrip(
        "/"
    ) + "/v1/engineering-review-runs/complete"
    allowed["LAUNCHPLANE_ENGINEERING_REVIEW_CREDENTIAL"] = credential
    allowed.pop("LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN", None)
    allowed.pop(EVERY_CODE_GITHUB_TOKEN_ENV_KEY, None)
    return allowed


def _review_prompt(run_id: str) -> str:
    return (
        "Review the exact current pull-request worktree without modifying it. "
        "Submit exactly one bounded result to $LAUNCHPLANE_ENGINEERING_REVIEW_URL with JSON fields "
        "credential=$LAUNCHPLANE_ENGINEERING_REVIEW_CREDENTIAL, decision, findings, and summary. "
        "Do not include repository, target, reviewer, model, tier, or evidence digest. "
        f"Launchplane run: {run_id}."
    )


def _run(
    command: Sequence[str],
    cwd: Path,
    environment: Mapping[str, str],
    *,
    timeout_seconds: float,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=dict(environment),
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout_seconds,
    )


def _remaining_lease_seconds(lease_expires_at: str) -> float:
    expires_at = datetime.fromisoformat(lease_expires_at.replace("Z", "+00:00"))
    remaining = (expires_at.astimezone(UTC) - datetime.now(UTC)).total_seconds()
    if remaining <= 0:
        raise RuntimeError("Engineering review run lease expired before process launch.")
    return remaining


def _failure_summary(error: Exception) -> str:
    if isinstance(error, subprocess.TimeoutExpired):
        return "Engineering review process exceeded its scoped lease."
    if isinstance(error, RuntimeError):
        return str(error)[:1000]
    return f"Engineering review worker failed with {type(error).__name__}."
