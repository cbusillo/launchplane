"""Authoritative, narrow evidence for an unrelated pnpm-store setup failure.

This does not reclassify arbitrary install failures or change isolation policy.
Only already-blocked singleton recovery consumes this proof.
"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import PurePosixPath
import re
from typing import TYPE_CHECKING, cast

from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidate,
    MergeTrainSetupFailureRecovery,
)

if TYPE_CHECKING:
    from control_plane.merge_train_github import GitHubMergeTrainClient

_STORE_MISSING = re.compile(
    r"ERR_PNPM_ENOENT[^\n]*ENOENT: no such file or directory, copyfile "
    r"'[^'\n]+/pnpm/store/v\d+/files/[0-9a-f]{2}/[0-9a-f]+' -> "
    r"'[^'\n]+/frontend/node_modules/[^'\n]+'"
)
_BROWSER_JOB = re.compile(r"frontend_browser_smoke \([1-9][0-9]*\)")


def _safe_source_path(value: object) -> bool:
    if not isinstance(value, str):
        return False
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        return False
    # Positive scope: changes outside Python application/tests and Markdown docs
    # could affect install inputs, hooks, tools, runner or workflow behavior.
    return (value.startswith(("control_plane/", "tests/")) and path.suffix == ".py") or (
        value.startswith("docs/") and path.suffix == ".md"
    )


def read_setup_failure_recovery(
    client: GitHubMergeTrainClient, *, candidate: MergeTrainBatchCandidate
) -> MergeTrainSetupFailureRecovery | None:
    if len(candidate.entries) != 1 or not re.fullmatch(r"[0-9a-f]{40}", candidate.candidate_sha):
        return None
    prefix = f"/repos/{candidate.repository}"

    def rows(path: str, key: str) -> list[dict[str, object]] | None:
        payload = client.transport.request(method="GET", path=path)
        if not isinstance(payload, dict):
            return None
        values = payload.get(key)
        if (
            not isinstance(values, list)
            or len(values) >= 100
            or type(payload.get("total_count")) is not int
            or payload["total_count"] != len(values)
            or any(not isinstance(value, dict) for value in values)
        ):
            return None
        return cast(list[dict[str, object]], values)

    runs = rows(
        f"{prefix}/commits/{candidate.candidate_sha}/check-runs?per_page=100&page=1", "check_runs"
    )
    statuses = rows(
        f"{prefix}/commits/{candidate.candidate_sha}/status?per_page=100&page=1", "statuses"
    )
    if runs is None or statuses is None:
        return None
    # Commit-status failures, pending work and unfamiliar failures have no causal
    # attribution here. A single setup failure plus its CI aggregate is required.
    if any(row.get("state") != "success" for row in statuses):
        return None
    if any(row.get("status") != "completed" for row in runs):
        return None
    failed = [row for row in runs if row.get("conclusion") not in {"success", "skipped", "neutral"}]
    browsers = [row for row in failed if _BROWSER_JOB.fullmatch(str(row.get("name") or ""))]
    if len(browsers) != 1 or {row.get("name") for row in failed} != {
        browsers[0]["name"],
        "ci-gate",
    }:
        return None
    job_check = browsers[0]
    match = re.fullmatch(
        rf"https://github\.com/{re.escape(candidate.repository)}/actions/runs/([1-9][0-9]*)/job/([1-9][0-9]*)",
        str(job_check.get("details_url") or ""),
    )
    if match is None:
        return None
    run_id, job_id = map(int, match.groups())
    for row in failed:
        app = row.get("app")
        if (
            row.get("head_sha") != candidate.candidate_sha
            or row.get("conclusion") != "failure"
            or not isinstance(app, dict)
            or app.get("slug") != "github-actions"
            or not str(row.get("details_url") or "").startswith(
                f"https://github.com/{candidate.repository}/actions/runs/{run_id}/job/"
            )
        ):
            return None
    if job_check.get("id") != job_id:
        return None
    job = client.transport.request(method="GET", path=f"{prefix}/actions/jobs/{job_id}")
    if not isinstance(job, dict) or (
        job.get("id"),
        job.get("run_id"),
        job.get("head_sha"),
        job.get("name"),
        job.get("status"),
        job.get("conclusion"),
    ) != (job_id, run_id, candidate.candidate_sha, job_check["name"], "completed", "failure"):
        return None
    attempt = job.get("run_attempt")
    steps = job.get("steps")
    if type(attempt) is not int or attempt <= 0 or not isinstance(steps, list) or not steps:
        return None
    if any(not isinstance(step, dict) for step in steps):
        return None
    steps = cast(list[dict[str, object]], steps)
    if (
        [step.get("name") for step in steps if step.get("conclusion") == "failure"]
        != ["Install frontend dependencies", "Upload browser evidence"]
        or [step.get("conclusion") for step in steps if step.get("name") == "Run browser smoke"]
        != ["skipped"]
        or any(step.get("conclusion") not in {"success", "skipped", "failure"} for step in steps)
    ):
        return None
    # The provider's complete same-attempt job set must include independent
    # successful frontend work, not just green aggregate/tree-reuse evidence.
    jobs_payload = client.transport.request(
        method="GET", path=f"{prefix}/actions/runs/{run_id}/jobs?per_page=100&page=1"
    )
    if not isinstance(jobs_payload, dict):
        return None
    jobs = jobs_payload.get("jobs")
    if (
        not isinstance(jobs, list)
        or len(jobs) >= 100
        or jobs_payload.get("total_count") != len(jobs)
    ):
        return None
    if any(not isinstance(row, dict) for row in jobs):
        return None
    jobs = cast(list[dict[str, object]], jobs)
    siblings = [
        row
        for row in jobs
        if row.get("name") == "frontend_validate"
        or (_BROWSER_JOB.fullmatch(str(row.get("name") or "")) and row.get("id") != job_id)
    ]
    if not any(row.get("name") == "frontend_validate" for row in siblings) or not any(
        _BROWSER_JOB.fullmatch(str(row.get("name") or "")) for row in siblings
    ):
        return None
    if job not in jobs or any(
        (
            row.get("run_id"),
            row.get("run_attempt"),
            row.get("head_sha"),
            row.get("status"),
            row.get("conclusion"),
        )
        != (run_id, attempt, candidate.candidate_sha, "completed", "success")
        for row in siblings
    ):
        return None
    log = client.transport.request(method="GET", path=f"{prefix}/actions/jobs/{job_id}/logs")
    if not isinstance(log, str) or not _STORE_MISSING.search(log) or log.count("ERR_PNPM_") != 1:
        return None
    head = candidate.entries[0].head_sha
    comparison = client.transport.request(
        method="GET", path=f"{prefix}/compare/{candidate.base_sha}...{head}"
    )
    if not isinstance(comparison, dict) or comparison.get("base_commit") is None:
        return None
    base = comparison.get("base_commit")
    commits, files = comparison.get("commits"), comparison.get("files")
    if (
        not isinstance(base, dict)
        or base.get("sha") != candidate.base_sha
        or not isinstance(commits, list)
        or not commits
        or len(commits) >= 250
        or comparison.get("total_commits") != len(commits)
        or not isinstance(commits[-1], dict)
        or commits[-1].get("sha") != head
        or not isinstance(files, list)
        or not files
        or len(files) >= 300
    ):
        return None
    paths: list[str] = []
    for file in files:
        if not isinstance(file, dict) or not _safe_source_path(file.get("filename")):
            return None
        paths.append(str(file["filename"]))
        if "previous_filename" in file:
            if not _safe_source_path(file["previous_filename"]):
                return None
            paths.append(str(file["previous_filename"]))
    return MergeTrainSetupFailureRecovery(
        candidate_sha=candidate.candidate_sha,
        head_sha=head,
        job_id=job_id,
        run_id=run_id,
        run_attempt=attempt,
        log_sha256=sha256(log.encode()).hexdigest(),
        changed_files_sha256=sha256(json.dumps(sorted(paths)).encode()).hexdigest(),
    )
