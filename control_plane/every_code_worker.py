from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import time
from typing import Callable, Literal, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from control_plane.contracts.every_code_pr_feedback_record import (
    EveryCodePrFeedbackRecord,
    EveryCodePrFeedbackStatus,
)
from control_plane.contracts.every_code_preview_gate_record import (
    EveryCodePreviewGateRecord,
    build_every_code_preview_gate_id,
)
from control_plane.contracts.every_code_work_request import (
    EveryCodeWorkRequestRecord,
    EveryCodeWorkRequestStatusUpdate,
    apply_every_code_work_request_status,
    resume_every_code_work_request,
)
from control_plane.workflows.launchplane import (
    github_pull_request_reference,
    launchplane_anchor_repo_preview_label,
)
from control_plane.workflows.ship import utc_now_timestamp


EveryCodeWorkerStatus = Literal["empty", "running", "blocked"]
EveryCodeWorkerFinishStatus = Literal["done", "blocked"]
TERMINAL_EVERY_CODE_STATES = {"done", "blocked"}
EVERY_CODE_PREVIEW_GATE_TIMEOUT_SECONDS = 24 * 60 * 60


class EveryCodeWorkerStore(Protocol):
    def read_every_code_work_request_record(
        self, request_id: str
    ) -> EveryCodeWorkRequestRecord: ...

    def list_every_code_work_request_records(
        self,
        *,
        state: str = "",
        repository: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[EveryCodeWorkRequestRecord, ...]: ...

    def claim_every_code_work_request_record(
        self,
        *,
        request_id: str,
        host: str,
        claimed_at: str,
    ) -> EveryCodeWorkRequestRecord | None: ...

    def write_every_code_work_request_record(
        self, record: EveryCodeWorkRequestRecord
    ) -> object: ...

    def list_every_code_pr_feedback_records(
        self,
        *,
        request_id: str = "",
        repository: str = "",
        pr_number: int | None = None,
        status: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[EveryCodePrFeedbackRecord, ...]: ...

    def write_every_code_pr_feedback_record(self, record: EveryCodePrFeedbackRecord) -> object: ...

    def list_every_code_preview_gate_records(
        self,
        *,
        request_id: str = "",
        repository: str = "",
        pr_number: int | None = None,
        status: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[EveryCodePreviewGateRecord, ...]: ...

    def write_every_code_preview_gate_record(
        self, record: EveryCodePreviewGateRecord
    ) -> object: ...


Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class DaemonProcess(Protocol):
    pid: int


ProcessLauncher = Callable[[Sequence[str], Path, Path], DaemonProcess]


class EveryCodeWorkerApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class EveryCodeWorkerApiStore:
    service_url: str
    worker_token: str
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not self.service_url.strip():
            raise ValueError("Every Code API worker requires service_url")
        if not self.worker_token.strip():
            raise ValueError("Every Code API worker requires worker_token")

    def read_every_code_work_request_record(self, request_id: str) -> EveryCodeWorkRequestRecord:
        payload = self._request("GET", f"/v1/every-code/work-requests/{request_id.strip()}")
        return EveryCodeWorkRequestRecord.model_validate(payload["request"])

    def list_every_code_work_request_records(
        self,
        *,
        state: str = "",
        repository: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[EveryCodeWorkRequestRecord, ...]:
        query: dict[str, object] = {}
        if state.strip():
            query["state"] = state.strip()
        if repository.strip():
            query["repository"] = repository.strip()
        if limit is not None:
            query["limit"] = limit
        if offset > 0:
            query["offset"] = offset
        suffix = f"?{urlencode(query)}" if query else ""
        payload = self._request("GET", f"/v1/every-code/work-requests{suffix}")
        requests = payload.get("requests", [])
        if not isinstance(requests, list):
            raise EveryCodeWorkerApiError("Launchplane work-request list response is invalid")
        return tuple(EveryCodeWorkRequestRecord.model_validate(item) for item in requests)

    def claim_every_code_work_request_record(
        self,
        *,
        request_id: str,
        host: str,
        claimed_at: str,
    ) -> EveryCodeWorkRequestRecord | None:
        del claimed_at
        try:
            payload = self._request(
                "POST",
                "/v1/every-code/work-requests/claim",
                body={"request_id": request_id.strip(), "host": host.strip()},
                idempotency_key=f"every-code-claim-{request_id.strip()}-{host.strip()}",
            )
        except EveryCodeWorkerApiError as error:
            if "HTTP 409" in str(error):
                return None
            raise
        result_payload = payload.get("result")
        request_payload: object = None
        if isinstance(result_payload, dict):
            request_payload = result_payload.get("request")
        if request_payload is None:
            request_payload = payload.get("records")
        if request_payload is None:
            raise EveryCodeWorkerApiError("Launchplane claim response is invalid")
        return EveryCodeWorkRequestRecord.model_validate(request_payload)

    def write_every_code_work_request_record(self, record: EveryCodeWorkRequestRecord) -> object:
        payload = self._request(
            "POST",
            "/v1/every-code/work-requests/status",
            body={
                "request_id": record.request_id,
                "host": record.claimed_by_host,
                "state": record.state,
                "result_pr_url": record.result_pr_url,
                "result_summary": record.result_summary,
                "error_message": record.error_message,
                "updated_at": record.updated_at,
            },
            idempotency_key=f"every-code-status-{record.request_id}-{record.state}-{record.updated_at}",
        )
        return payload

    def rerun_every_code_work_request_record(
        self, *, request_id: str, trigger_actor: str = ""
    ) -> EveryCodeWorkRequestRecord:
        payload = self._request(
            "POST",
            "/v1/every-code/work-requests/rerun",
            body={"request_id": request_id.strip(), "trigger_actor": trigger_actor.strip()},
            idempotency_key=f"every-code-rerun-{request_id.strip()}-{trigger_actor.strip()}",
        )
        request_payload = payload.get("request")
        result_payload = payload.get("result")
        if request_payload is None and isinstance(result_payload, dict):
            request_payload = result_payload.get("request")
        if not isinstance(request_payload, dict):
            raise EveryCodeWorkerApiError("Launchplane rerun response is invalid")
        return EveryCodeWorkRequestRecord.model_validate(request_payload)

    def list_every_code_pr_feedback_records(
        self,
        *,
        request_id: str = "",
        repository: str = "",
        pr_number: int | None = None,
        status: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[EveryCodePrFeedbackRecord, ...]:
        query: dict[str, object] = {}
        if request_id.strip():
            query["request_id"] = request_id.strip()
        if repository.strip():
            query["repository"] = repository.strip()
        if pr_number is not None:
            query["pr_number"] = pr_number
        if status.strip():
            query["status"] = status.strip()
        if limit is not None:
            query["limit"] = limit
        if offset > 0:
            query["offset"] = offset
        suffix = f"?{urlencode(query)}" if query else ""
        payload = self._request("GET", f"/v1/every-code/pr-feedback{suffix}")
        feedback = payload.get("feedback", [])
        if not isinstance(feedback, list):
            raise EveryCodeWorkerApiError("Launchplane PR feedback list response is invalid")
        return tuple(EveryCodePrFeedbackRecord.model_validate(item) for item in feedback)

    def write_every_code_pr_feedback_record(self, record: EveryCodePrFeedbackRecord) -> object:
        payload = self._request(
            "POST",
            "/v1/every-code/pr-feedback/status",
            body={
                "feedback_id": record.feedback_id,
                "request_id": record.request_id,
                "status": record.status,
            },
            idempotency_key=f"every-code-pr-feedback-{record.feedback_id}-{record.status}",
        )
        return payload

    def list_every_code_preview_gate_records(
        self,
        *,
        request_id: str = "",
        repository: str = "",
        pr_number: int | None = None,
        status: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[EveryCodePreviewGateRecord, ...]:
        query: dict[str, object] = {}
        if request_id.strip():
            query["request_id"] = request_id.strip()
        if repository.strip():
            query["repository"] = repository.strip()
        if pr_number is not None:
            query["pr_number"] = pr_number
        if status.strip():
            query["status"] = status.strip()
        if limit is not None:
            query["limit"] = limit
        if offset > 0:
            query["offset"] = offset
        suffix = f"?{urlencode(query)}" if query else ""
        payload = self._request("GET", f"/v1/every-code/preview-gates{suffix}")
        gates = payload.get("gates", [])
        if not isinstance(gates, list):
            raise EveryCodeWorkerApiError("Launchplane preview gate list response is invalid")
        return tuple(EveryCodePreviewGateRecord.model_validate(item) for item in gates)

    def write_every_code_preview_gate_record(
        self, record: EveryCodePreviewGateRecord
    ) -> object:
        payload = self._request(
            "POST",
            "/v1/every-code/preview-gates",
            body=record.model_dump(mode="json"),
            idempotency_key=f"every-code-preview-gate-{record.gate_id}-{record.updated_at}",
        )
        return payload

    def _request(
        self,
        method: Literal["GET", "POST"],
        path: str,
        *,
        body: dict[str, object] | None = None,
        idempotency_key: str = "",
    ) -> dict[str, object]:
        base_url = self.service_url.strip().rstrip("/")
        url = f"{base_url}{path}"
        data = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.worker_token.strip()}",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise EveryCodeWorkerApiError(
                f"Launchplane API request failed with HTTP {error.code}: {detail}"
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            raise EveryCodeWorkerApiError(f"Launchplane API request failed: {error}") from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise EveryCodeWorkerApiError("Launchplane API response was not valid JSON") from error
        if not isinstance(payload, dict):
            raise EveryCodeWorkerApiError("Launchplane API response was not a JSON object")
        return payload


@dataclass(frozen=True)
class EveryCodeWorkerHandoffResult:
    status: EveryCodeWorkerStatus
    detail: str
    request_id: str = ""
    repository: str = ""
    issue_number: int = 0
    session_name: str = ""
    checkout_root: str = ""
    worktree_root: str = ""
    worktree_branch: str = ""

    def as_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "detail": self.detail,
            "request_id": self.request_id,
            "repository": self.repository,
            "issue_number": self.issue_number,
            "session_name": self.session_name,
            "checkout_root": self.checkout_root,
            "worktree_root": self.worktree_root,
            "worktree_branch": self.worktree_branch,
        }


@dataclass(frozen=True)
class EveryCodeWorkerLoopResult:
    iterations: int
    handed_off: int
    blocked: int
    empty: int
    stopped_reason: str
    last_result: EveryCodeWorkerHandoffResult

    def as_payload(self) -> dict[str, object]:
        return {
            "iterations": self.iterations,
            "handed_off": self.handed_off,
            "blocked": self.blocked,
            "empty": self.empty,
            "stopped_reason": self.stopped_reason,
            "last_result": self.last_result.as_payload(),
        }


@dataclass(frozen=True)
class EveryCodeWorkerDaemonSpec:
    pid_file: Path
    log_file: Path
    command: tuple[str, ...]

    def as_payload(self) -> dict[str, object]:
        return {
            "pid_file": str(self.pid_file),
            "log_file": str(self.log_file),
            "command": list(self.command),
        }


@dataclass(frozen=True)
class EveryCodeWorkerDaemonStatus:
    running: bool
    pid: int | None
    pid_file: Path
    log_file: Path
    detail: str

    def as_payload(self) -> dict[str, object]:
        return {
            "running": self.running,
            "pid": self.pid,
            "pid_file": str(self.pid_file),
            "log_file": str(self.log_file),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class EveryCodeWorkerStartResult:
    status: Literal["started", "already_running"]
    pid: int
    spec: EveryCodeWorkerDaemonSpec

    def as_payload(self) -> dict[str, object]:
        payload = self.spec.as_payload()
        payload.update({"status": self.status, "pid": self.pid})
        return payload


@dataclass(frozen=True)
class EveryCodeWorkerStopResult:
    status: Literal["stopped", "not_running"]
    pid: int | None
    detail: str

    def as_payload(self) -> dict[str, object]:
        return {"status": self.status, "pid": self.pid, "detail": self.detail}


@dataclass(frozen=True)
class EveryCodeWorkerFinishResult:
    status: EveryCodeWorkerFinishStatus
    detail: str
    request_id: str
    repository: str
    issue_number: int
    result_pr_url: str = ""

    def as_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "detail": self.detail,
            "request_id": self.request_id,
            "repository": self.repository,
            "issue_number": self.issue_number,
            "result_pr_url": self.result_pr_url,
        }


@dataclass(frozen=True)
class EveryCodePrFeedbackApplyResult:
    status: Literal["empty", "applied", "ignored", "blocked"]
    detail: str
    feedback_id: str = ""
    request_id: str = ""
    session_name: str = ""

    def as_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "detail": self.detail,
            "feedback_id": self.feedback_id,
            "request_id": self.request_id,
            "session_name": self.session_name,
        }


@dataclass(frozen=True)
class EveryCodePreviewGateResult:
    checked: int = 0
    labeled: int = 0
    pending: int = 0
    blocked: int = 0
    skipped: int = 0
    cancelled: int = 0
    reset: int = 0
    reviewer_backfilled: int = 0

    def as_payload(self) -> dict[str, object]:
        return {
            "checked": self.checked,
            "labeled": self.labeled,
            "pending": self.pending,
            "blocked": self.blocked,
            "skipped": self.skipped,
            "cancelled": self.cancelled,
            "reset": self.reset,
            "reviewer_backfilled": self.reviewer_backfilled,
        }


@dataclass(frozen=True)
class EveryCodePrFailureRouteResult:
    checked: int = 0
    routed: int = 0
    pending: int = 0
    duplicate: int = 0
    retried_infra: int = 0
    persistent_infra: int = 0
    recovered: int = 0
    skipped: int = 0

    def as_payload(self) -> dict[str, object]:
        return {
            "checked": self.checked,
            "routed": self.routed,
            "pending": self.pending,
            "duplicate": self.duplicate,
            "retried_infra": self.retried_infra,
            "persistent_infra": self.persistent_infra,
            "recovered": self.recovered,
            "skipped": self.skipped,
        }


def every_code_tmux_session_name(request_id: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.:-]+", "-", request_id.strip()).strip("-._:")
    return f"every-code-{normalized or 'request'}"[:80]


def every_code_session_state_path(*, state_dir: Path, request_id: str) -> Path:
    normalized = re.sub(r"[^A-Za-z0-9_.:-]+", "-", request_id.strip()).strip("-._:")
    return (
        state_dir.expanduser().resolve()
        / "every-code-worker"
        / "sessions"
        / f"{normalized or 'request'}.json"
    )


def write_every_code_session_state(
    *,
    state_dir: Path,
    record: EveryCodeWorkRequestRecord,
    session_name: str,
    source_checkout_root: Path,
    launch_root: Path,
    worktree_branch: str,
    host: str,
) -> None:
    path = every_code_session_state_path(state_dir=state_dir, request_id=record.request_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "request_id": record.request_id,
        "session_name": session_name,
        "source_checkout_root": str(source_checkout_root),
        "launch_root": str(launch_root),
        "worktree_branch": worktree_branch,
        "host": host.strip(),
        "created_at": utc_now_timestamp(),
    }
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def read_every_code_session_state(path: Path) -> dict[str, str] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    result: dict[str, str] = {}
    for key in (
        "request_id",
        "session_name",
        "host",
        "source_checkout_root",
        "launch_root",
        "worktree_branch",
    ):
        value = payload.get(key)
        if isinstance(value, str):
            result[key] = value
    if not result.get("request_id") or not result.get("session_name"):
        return None
    return result


def every_code_claim_comment_body(
    record: EveryCodeWorkRequestRecord, *, host: str, session_name: str
) -> str:
    return (
        "<!-- every-code-claim -->\n"
        "Every Code is working on this issue from "
        f"`{host.strip() or 'this host'}`.\n\n"
        f"Session: `{session_name}`\n"
        f"Launchplane request: `{record.request_id}`"
    )


def _post_every_code_claim_comment(
    *,
    record: EveryCodeWorkRequestRecord,
    host: str,
    session_name: str,
    runner: Runner,
) -> str:
    repository = record.repository.strip()
    if not repository or record.issue_number <= 0:
        return ""
    body = every_code_claim_comment_body(
        record,
        host=host,
        session_name=session_name,
    )
    try:
        result = runner(
            (
                "gh",
                "issue",
                "comment",
                str(record.issue_number),
                "--repo",
                repository,
                "--body",
                body,
            )
        )
    except OSError as exc:
        return f"Could not post GitHub working comment: {exc}"
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "gh issue comment failed"
        return f"Could not post GitHub working comment: {detail}"
    return ""


def default_every_code_command(record: EveryCodeWorkRequestRecord) -> str:
    prompt = (
        f"Work on {record.repository} issue #{record.issue_number}: {record.issue_title}. "
        f"Issue URL: {record.issue_url}. Launchplane request: {record.request_id}. "
        "Before changing files, read the issue body and every issue comment. "
        "Treat newer comments as potentially more current than the original body. "
        "Inspect linked references and any available images or attachments before deciding what to change. "
        "You are already running inside the isolated Every Code worktree and branch for this request. "
        "Open a pull request for the change. If the PR fully resolves the issue, "
        f"include `Closes #{record.issue_number}` or `Fixes #{record.issue_number}` "
        "in the PR body so GitHub closes the issue when the PR merges. Use "
        "`Refs` only when the PR is partial or still needs reporter validation. "
        "When the PR is open and your checks are complete, let the session exit "
        "so Launchplane can mark this request finished."
    )
    return "code " + shlex.quote(prompt)


def every_code_pr_feedback_prompt(feedback: EveryCodePrFeedbackRecord) -> str:
    source = feedback.html_url.strip() or feedback.pr_url.strip()
    submitted = feedback.submitted_at.strip() or feedback.received_at.strip()
    lines = [
        "Every Code received new PR feedback for this request.",
        f"Feedback kind: {feedback.feedback_kind}",
        f"Actor: {feedback.actor}",
    ]
    if submitted:
        lines.append(f"Submitted at: {submitted}")
    if source:
        lines.append(f"Source: {source}")
    lines.extend(
        (
            "",
            "Feedback:",
            feedback.body.strip(),
            "",
            "Read the PR context and existing discussion before changing files. "
            "Apply the requested correction on the same branch/worktree, update the PR, "
            "and then let the session exit when checks are complete.",
        )
    )
    return "\n".join(lines)


def render_every_code_command(
    record: EveryCodeWorkRequestRecord,
    *,
    command_template: str,
) -> str:
    template = command_template.strip()
    if not template:
        return default_every_code_command(record)
    return template.format(
        request_id=record.request_id,
        repository=record.repository,
        issue_number=record.issue_number,
        issue_url=record.issue_url,
        issue_title=record.issue_title,
        trigger_label=record.trigger_label,
    )


def build_every_code_session_command(
    *,
    record: EveryCodeWorkRequestRecord,
    command: str,
    state_dir: Path,
    host: str,
    database_url: str = "",
    service_url: str = "",
    worker_token_env: str = "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN",
) -> str:
    finish_command = [
        "uv",
        "run",
        "launchplane",
        "every-code",
        "finish",
        "--state-dir",
        str(state_dir.expanduser().resolve()),
        "--request-id",
        record.request_id,
        "--host",
        host.strip(),
        "--exit-code",
        "$status",
    ]
    if database_url.strip():
        finish_command.extend(("--database-url", database_url.strip()))
    if service_url.strip():
        finish_command.extend(("--service-url", service_url.strip()))
        finish_command.extend(("--worker-token-env", worker_token_env.strip()))
    finish_shell = " ".join(
        "$status" if part == "$status" else shlex.quote(part) for part in finish_command
    )
    session_env = {
        "EVERY_CODE_SESSION_ORIGIN": "every_code",
        "EVERY_CODE_REQUEST_ID": record.request_id,
        "EVERY_CODE_REPOSITORY": record.repository,
        "EVERY_CODE_ISSUE_NUMBER": str(record.issue_number),
        "EVERY_CODE_ISSUE_URL": record.issue_url,
    }
    env_shell = " ".join(
        f"{key}={shlex.quote(value)}" for key, value in session_env.items() if value
    )
    session_command = f"{env_shell} {command}" if env_shell else command
    return f"{session_command}\nstatus=$?\n{finish_shell}\nexit $status"


def build_every_code_feedback_session_command(
    *,
    feedback: EveryCodePrFeedbackRecord,
    state_dir: Path,
    host: str,
    database_url: str = "",
    service_url: str = "",
    worker_token_env: str = "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN",
) -> str:
    command = "code " + shlex.quote(every_code_pr_feedback_prompt(feedback))
    return build_every_code_session_command(
        record=EveryCodeWorkRequestRecord(
            request_id=feedback.request_id,
            source="manual",
            state="running",
            repository=feedback.repository,
            issue_number=1,
            issue_url=feedback.pr_url,
            trigger_label="every-code",
            queued_at=feedback.received_at,
            updated_at=feedback.received_at,
            claimed_at=feedback.received_at,
            claimed_by_host=host.strip() or "Every Code worker",
            started_at=feedback.received_at,
        ),
        command=command,
        state_dir=state_dir,
        host=host,
        database_url=database_url,
        service_url=service_url,
        worker_token_env=worker_token_env,
    )


def resolve_every_code_checkout_root(
    record: EveryCodeWorkRequestRecord,
    *,
    workspace_root: Path,
    checkout_root: Path | None = None,
) -> Path:
    if checkout_root is not None:
        return checkout_root.expanduser().resolve()
    repository_name = record.repository.strip().rsplit("/", 1)[-1]
    return (workspace_root.expanduser() / repository_name).resolve()


@dataclass(frozen=True)
class EveryCodePreparedCheckout:
    launch_root: Path
    source_checkout_root: Path
    worktree_branch: str = ""


def every_code_worktree_root(
    record: EveryCodeWorkRequestRecord,
    *,
    state_dir: Path,
) -> Path:
    repository_slug = _safe_path_component(record.repository.strip().replace("/", "-"))
    request_slug = _safe_path_component(record.request_id)
    return (
        state_dir.expanduser().resolve()
        / "every-code-worker"
        / "worktrees"
        / repository_slug
        / request_slug
    )


def every_code_worktree_branch(record: EveryCodeWorkRequestRecord) -> str:
    repository_slug = _safe_branch_component(record.repository.strip().replace("/", "-"))
    request_slug = _safe_branch_component(record.request_id)
    issue_part = str(record.issue_number) if record.issue_number > 0 else "request"
    return f"every-code/{repository_slug}-{issue_part}-{request_slug}"[:180].rstrip("/.-")


def prepare_every_code_checkout(
    record: EveryCodeWorkRequestRecord,
    *,
    workspace_root: Path,
    state_dir: Path | None,
    checkout_root: Path | None = None,
    runner: Runner | None = None,
) -> EveryCodePreparedCheckout:
    resolved_checkout_root = resolve_every_code_checkout_root(
        record,
        workspace_root=workspace_root,
        checkout_root=checkout_root,
    )
    if checkout_root is not None or state_dir is None:
        return EveryCodePreparedCheckout(
            launch_root=resolved_checkout_root,
            source_checkout_root=resolved_checkout_root,
        )
    if not resolved_checkout_root.is_dir():
        return EveryCodePreparedCheckout(
            launch_root=resolved_checkout_root,
            source_checkout_root=resolved_checkout_root,
        )
    run = runner or _run_subprocess
    worktree_root = every_code_worktree_root(record, state_dir=state_dir)
    branch = every_code_worktree_branch(record)
    _ensure_every_code_worktree(
        source_checkout_root=resolved_checkout_root,
        worktree_root=worktree_root,
        branch=branch,
        runner=run,
    )
    return EveryCodePreparedCheckout(
        launch_root=worktree_root,
        source_checkout_root=resolved_checkout_root,
        worktree_branch=branch,
    )


def run_every_code_worker_once(
    *,
    record_store: EveryCodeWorkerStore,
    host: str,
    workspace_root: Path,
    checkout_root: Path | None = None,
    worktree_state_dir: Path | None = None,
    repository: str = "",
    command_template: str = "",
    state_dir: Path | None = None,
    database_url: str = "",
    service_url: str = "",
    worker_token_env: str = "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN",
    tmux_binary: str = "tmux",
    runner: Runner | None = None,
) -> EveryCodeWorkerHandoffResult:
    normalized_host = host.strip()
    if not normalized_host:
        raise ValueError("Every Code worker requires a host name")
    queued_records = record_store.list_every_code_work_request_records(
        state="queued",
        repository=repository.strip(),
        limit=1,
    )
    if not queued_records:
        return EveryCodeWorkerHandoffResult(
            status="empty", detail="No queued Every Code work request."
        )

    queued_record = queued_records[0]
    claimed_record = record_store.claim_every_code_work_request_record(
        request_id=queued_record.request_id,
        host=normalized_host,
        claimed_at=utc_now_timestamp(),
    )
    if claimed_record is None:
        return EveryCodeWorkerHandoffResult(
            status="empty",
            detail="Queued Every Code work request was claimed by another worker.",
            request_id=queued_record.request_id,
            repository=queued_record.repository,
            issue_number=queued_record.issue_number,
        )

    session_name = every_code_tmux_session_name(claimed_record.request_id)
    run = runner or _run_subprocess
    resolved_worktree_state_dir = (
        worktree_state_dir if worktree_state_dir is not None else state_dir
    )
    try:
        prepared_checkout = prepare_every_code_checkout(
            claimed_record,
            workspace_root=workspace_root,
            checkout_root=checkout_root,
            state_dir=resolved_worktree_state_dir,
            runner=run,
        )
    except RuntimeError as exc:
        fallback_checkout_root = resolve_every_code_checkout_root(
            claimed_record,
            workspace_root=workspace_root,
            checkout_root=checkout_root,
        )
        return _block_every_code_request(
            record_store=record_store,
            record=claimed_record,
            host=normalized_host,
            detail=str(exc),
            session_name=session_name,
            checkout_root=fallback_checkout_root,
        )
    resolved_checkout_root = prepared_checkout.launch_root
    if not prepared_checkout.source_checkout_root.is_dir():
        return _block_every_code_request(
            record_store=record_store,
            record=claimed_record,
            host=normalized_host,
            detail=f"Checkout root does not exist: {prepared_checkout.source_checkout_root}",
            session_name=session_name,
            checkout_root=prepared_checkout.source_checkout_root,
        )
    if not resolved_checkout_root.is_dir():
        return _block_every_code_request(
            record_store=record_store,
            record=claimed_record,
            host=normalized_host,
            detail=f"Every Code worktree root does not exist: {resolved_checkout_root}",
            session_name=session_name,
            checkout_root=prepared_checkout.source_checkout_root,
        )

    existing_session = _tmux_session_exists(
        tmux_binary=tmux_binary,
        session_name=session_name,
        runner=run,
    )
    if existing_session is None:
        return _block_every_code_request(
            record_store=record_store,
            record=claimed_record,
            host=normalized_host,
            detail=f"Could not inspect tmux session {session_name!r}.",
            session_name=session_name,
            checkout_root=prepared_checkout.source_checkout_root,
        )
    if not existing_session:
        command = render_every_code_command(claimed_record, command_template=command_template)
        if state_dir is not None:
            command = build_every_code_session_command(
                record=claimed_record,
                command=command,
                state_dir=state_dir,
                database_url=database_url,
                service_url=service_url,
                worker_token_env=worker_token_env,
                host=normalized_host,
            )
        try:
            launch_result = run(
                (
                    tmux_binary,
                    "new-session",
                    "-d",
                    "-s",
                    session_name,
                    "-c",
                    str(resolved_checkout_root),
                    command,
                )
            )
        except OSError as exc:
            return _block_every_code_request(
                record_store=record_store,
                record=claimed_record,
                host=normalized_host,
                detail=f"Could not launch tmux session: {exc}",
                session_name=session_name,
                checkout_root=prepared_checkout.source_checkout_root,
            )
        if launch_result.returncode != 0:
            return _block_every_code_request(
                record_store=record_store,
                record=claimed_record,
                host=normalized_host,
                detail=f"tmux launch failed: {launch_result.stderr.strip()}",
                session_name=session_name,
                checkout_root=prepared_checkout.source_checkout_root,
            )
    if state_dir is not None:
        write_every_code_session_state(
            state_dir=state_dir,
            record=claimed_record,
            session_name=session_name,
            source_checkout_root=prepared_checkout.source_checkout_root,
            launch_root=resolved_checkout_root,
            worktree_branch=prepared_checkout.worktree_branch,
            host=normalized_host,
        )

    claim_comment_warning = _post_every_code_claim_comment(
        record=claimed_record,
        host=normalized_host,
        session_name=session_name,
        runner=run,
    )
    running_record = apply_every_code_work_request_status(
        record_store.read_every_code_work_request_record(claimed_record.request_id),
        EveryCodeWorkRequestStatusUpdate(
            state="running",
            host=normalized_host,
            updated_at=utc_now_timestamp(),
            result_summary="; ".join(
                part
                for part in (
                    f"Visible tmux session: {session_name}",
                    f"Worktree: {resolved_checkout_root}",
                    claim_comment_warning,
                )
                if part
            ),
        ),
    )
    record_store.write_every_code_work_request_record(running_record)
    return EveryCodeWorkerHandoffResult(
        status="running",
        detail="Every Code work request handed off to a visible tmux session.",
        request_id=running_record.request_id,
        repository=running_record.repository,
        issue_number=running_record.issue_number,
        session_name=session_name,
        checkout_root=str(prepared_checkout.source_checkout_root),
        worktree_root=str(resolved_checkout_root),
        worktree_branch=prepared_checkout.worktree_branch,
    )


def apply_every_code_pr_feedback_for_host(
    *,
    record_store: EveryCodeWorkerStore,
    host: str,
    state_dir: Path | None,
    database_url: str = "",
    service_url: str = "",
    worker_token_env: str = "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN",
    tmux_binary: str = "tmux",
    runner: Runner | None = None,
) -> EveryCodePrFeedbackApplyResult:
    if state_dir is None:
        return EveryCodePrFeedbackApplyResult(
            status="empty",
            detail="Every Code PR feedback requires a worker state directory.",
        )
    normalized_host = host.strip()
    if not normalized_host:
        raise ValueError("Every Code worker requires a host name")
    feedback_records = record_store.list_every_code_pr_feedback_records(
        status="pending",
        limit=25,
    )
    if not feedback_records:
        return EveryCodePrFeedbackApplyResult(
            status="empty", detail="No pending Every Code PR feedback."
        )
    run = runner or _run_subprocess
    for feedback in feedback_records:
        result = _apply_every_code_pr_feedback_record(
            record_store=record_store,
            feedback=feedback,
            host=normalized_host,
            state_dir=state_dir,
            database_url=database_url,
            service_url=service_url,
            worker_token_env=worker_token_env,
            tmux_binary=tmux_binary,
            runner=run,
        )
        if result.status != "empty":
            return result
    return EveryCodePrFeedbackApplyResult(
        status="empty",
        detail="No pending Every Code PR feedback belongs to this host.",
    )


def run_every_code_worker_loop(
    *,
    record_store: EveryCodeWorkerStore,
    host: str,
    workspace_root: Path,
    checkout_root: Path | None = None,
    worktree_state_dir: Path | None = None,
    repository: str = "",
    command_template: str = "",
    state_dir: Path | None = None,
    database_url: str = "",
    service_url: str = "",
    worker_token_env: str = "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN",
    tmux_binary: str = "tmux",
    interval_seconds: float = 60.0,
    max_iterations: int = 0,
    runner: Runner | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> EveryCodeWorkerLoopResult:
    if interval_seconds < 0:
        raise ValueError("Every Code worker interval must be non-negative")
    if max_iterations < 0:
        raise ValueError("Every Code worker max_iterations must be non-negative")

    iterations = 0
    handed_off = 0
    blocked = 0
    empty = 0
    last_result = EveryCodeWorkerHandoffResult(
        status="empty",
        detail="Every Code worker loop has not run yet.",
    )
    while max_iterations == 0 or iterations < max_iterations:
        iterations += 1
        close_terminal_every_code_sessions(
            record_store=record_store,
            host=host,
            state_dir=state_dir,
            tmux_binary=tmux_binary,
            runner=runner,
        )
        apply_every_code_pr_feedback_for_host(
            record_store=record_store,
            host=host,
            state_dir=state_dir,
            database_url=database_url,
            service_url=service_url,
            worker_token_env=worker_token_env,
            tmux_binary=tmux_binary,
            runner=runner,
        )
        request_ready_every_code_pr_preview_labels(
            record_store=record_store,
            repository=repository,
            runner=runner,
        )
        route_every_code_pr_check_failures(
            record_store=record_store,
            repository=repository,
            runner=runner,
        )
        apply_every_code_pr_feedback_for_host(
            record_store=record_store,
            host=host,
            state_dir=state_dir,
            database_url=database_url,
            service_url=service_url,
            worker_token_env=worker_token_env,
            tmux_binary=tmux_binary,
            runner=runner,
        )
        last_result = run_every_code_worker_once(
            record_store=record_store,
            host=host,
            workspace_root=workspace_root,
            checkout_root=checkout_root,
            worktree_state_dir=worktree_state_dir,
            repository=repository,
            command_template=command_template,
            state_dir=state_dir,
            database_url=database_url,
            service_url=service_url,
            worker_token_env=worker_token_env,
            tmux_binary=tmux_binary,
            runner=runner,
        )
        if last_result.status == "running":
            handed_off += 1
        elif last_result.status == "blocked":
            blocked += 1
        else:
            empty += 1
        if max_iterations != 0 and iterations >= max_iterations:
            return EveryCodeWorkerLoopResult(
                iterations=iterations,
                handed_off=handed_off,
                blocked=blocked,
                empty=empty,
                stopped_reason="max_iterations",
                last_result=last_result,
            )
        sleeper(interval_seconds)

    return EveryCodeWorkerLoopResult(
        iterations=iterations,
        handed_off=handed_off,
        blocked=blocked,
        empty=empty,
        stopped_reason="stopped",
        last_result=last_result,
    )


def close_terminal_every_code_sessions(
    *,
    record_store: EveryCodeWorkerStore,
    host: str,
    state_dir: Path | None,
    tmux_binary: str = "tmux",
    runner: Runner | None = None,
) -> int:
    if state_dir is None:
        return 0
    normalized_host = host.strip()
    if not normalized_host:
        raise ValueError("Every Code worker requires a host name")
    sessions_dir = state_dir.expanduser().resolve() / "every-code-worker" / "sessions"
    try:
        session_state_paths = tuple(sessions_dir.glob("*.json"))
    except OSError:
        return 0

    run = runner or _run_subprocess
    closed = 0
    for path in session_state_paths:
        session_state = read_every_code_session_state(path)
        if session_state is None:
            continue
        if session_state.get("host", normalized_host) != normalized_host:
            continue
        request_id = session_state["request_id"]
        try:
            record = record_store.read_every_code_work_request_record(request_id)
        except Exception:
            continue
        if record.claimed_by_host.strip() != normalized_host:
            continue
        if record.state not in TERMINAL_EVERY_CODE_STATES:
            continue

        session_name = session_state["session_name"]
        existing_session = _tmux_session_exists(
            tmux_binary=tmux_binary,
            session_name=session_name,
            runner=run,
        )
        if existing_session is False:
            path.unlink(missing_ok=True)
            continue
        if existing_session is None:
            continue
        pane_pid = _tmux_pane_pid(
            tmux_binary=tmux_binary,
            session_name=session_name,
            runner=run,
        )
        if pane_pid is None:
            continue
        try:
            os.killpg(pane_pid, signal.SIGTERM)
        except ProcessLookupError:
            path.unlink(missing_ok=True)
            continue
        except OSError:
            continue
        closed += 1
    return closed


def _apply_every_code_pr_feedback_record(
    *,
    record_store: EveryCodeWorkerStore,
    feedback: EveryCodePrFeedbackRecord,
    host: str,
    state_dir: Path,
    database_url: str,
    service_url: str,
    worker_token_env: str,
    tmux_binary: str,
    runner: Runner,
) -> EveryCodePrFeedbackApplyResult:
    _acknowledge_every_code_pr_feedback(feedback=feedback, reaction="eyes", runner=runner)
    try:
        record = record_store.read_every_code_work_request_record(feedback.request_id)
    except Exception:
        _mark_every_code_pr_feedback(record_store, feedback=feedback, status="ignored")
        _acknowledge_every_code_pr_feedback(
            feedback=feedback, reaction="confused", runner=runner
        )
        return EveryCodePrFeedbackApplyResult(
            status="ignored",
            detail="Every Code work request for PR feedback was not found.",
            feedback_id=feedback.feedback_id,
            request_id=feedback.request_id,
        )
    if record.claimed_by_host.strip() != host:
        return EveryCodePrFeedbackApplyResult(
            status="empty",
            detail="Every Code PR feedback belongs to another host.",
            feedback_id=feedback.feedback_id,
            request_id=feedback.request_id,
        )
    if _terminal_every_code_request_closed_by_linked_pr(record):
        _mark_every_code_pr_feedback(record_store, feedback=feedback, status="ignored")
        _acknowledge_every_code_pr_feedback(
            feedback=feedback, reaction="confused", runner=runner
        )
        return EveryCodePrFeedbackApplyResult(
            status="ignored",
            detail="Every Code PR feedback belongs to a closed linked pull request.",
            feedback_id=feedback.feedback_id,
            request_id=feedback.request_id,
        )
    state_path = every_code_session_state_path(state_dir=state_dir, request_id=record.request_id)
    session_state = read_every_code_session_state(state_path)
    if session_state is None:
        _mark_every_code_pr_feedback(record_store, feedback=feedback, status="ignored")
        _acknowledge_every_code_pr_feedback(
            feedback=feedback, reaction="confused", runner=runner
        )
        return EveryCodePrFeedbackApplyResult(
            status="ignored",
            detail="Every Code PR feedback has no local session state on this host.",
            feedback_id=feedback.feedback_id,
            request_id=feedback.request_id,
        )
    if session_state.get("host", host) != host:
        return EveryCodePrFeedbackApplyResult(
            status="empty",
            detail="Every Code PR feedback session state belongs to another host.",
            feedback_id=feedback.feedback_id,
            request_id=feedback.request_id,
        )
    session_name = session_state["session_name"]
    existing_session = _tmux_session_exists(
        tmux_binary=tmux_binary,
        session_name=session_name,
        runner=runner,
    )
    if existing_session is None:
        return EveryCodePrFeedbackApplyResult(
            status="blocked",
            detail=f"Could not inspect tmux session {session_name!r}.",
            feedback_id=feedback.feedback_id,
            request_id=feedback.request_id,
            session_name=session_name,
        )
    if existing_session:
        if not _send_every_code_pr_feedback_to_session(
            feedback=feedback,
            session_name=session_name,
            tmux_binary=tmux_binary,
            runner=runner,
        ):
            _acknowledge_every_code_pr_feedback(
                feedback=feedback, reaction="confused", runner=runner
            )
            return EveryCodePrFeedbackApplyResult(
                status="blocked",
                detail=f"Could not send PR feedback to tmux session {session_name!r}.",
                feedback_id=feedback.feedback_id,
                request_id=feedback.request_id,
                session_name=session_name,
            )
        _mark_every_code_pr_feedback(record_store, feedback=feedback, status="applied")
        _acknowledge_every_code_pr_feedback(feedback=feedback, reaction="rocket", runner=runner)
        return EveryCodePrFeedbackApplyResult(
            status="applied",
            detail="Every Code PR feedback was sent to the running session.",
            feedback_id=feedback.feedback_id,
            request_id=feedback.request_id,
            session_name=session_name,
        )

    launch_root = Path(session_state.get("launch_root", "")).expanduser()
    if not launch_root.is_dir():
        _mark_every_code_pr_feedback(record_store, feedback=feedback, status="ignored")
        _acknowledge_every_code_pr_feedback(
            feedback=feedback, reaction="confused", runner=runner
        )
        state_path.unlink(missing_ok=True)
        return EveryCodePrFeedbackApplyResult(
            status="ignored",
            detail="Every Code PR feedback worktree is no longer available on this host.",
            feedback_id=feedback.feedback_id,
            request_id=feedback.request_id,
            session_name=session_name,
        )
    if record.state in TERMINAL_EVERY_CODE_STATES:
        resumed = resume_every_code_work_request(
            record,
            host=host,
            resumed_at=utc_now_timestamp(),
            result_summary=f"Resumed for PR feedback: {feedback.html_url or feedback.pr_url}",
        )
        record_store.write_every_code_work_request_record(resumed)
    command = build_every_code_feedback_session_command(
        feedback=feedback,
        state_dir=state_dir,
        host=host,
        database_url=database_url,
        service_url=service_url,
        worker_token_env=worker_token_env,
    )
    try:
        launch_result = runner(
            (
                tmux_binary,
                "new-session",
                "-d",
                "-s",
                session_name,
                "-c",
                str(launch_root),
                command,
            )
        )
    except OSError:
        _acknowledge_every_code_pr_feedback(
            feedback=feedback, reaction="confused", runner=runner
        )
        return EveryCodePrFeedbackApplyResult(
            status="blocked",
            detail=f"Could not relaunch tmux session {session_name!r} for PR feedback.",
            feedback_id=feedback.feedback_id,
            request_id=feedback.request_id,
            session_name=session_name,
        )
    if launch_result.returncode != 0:
        _acknowledge_every_code_pr_feedback(
            feedback=feedback, reaction="confused", runner=runner
        )
        return EveryCodePrFeedbackApplyResult(
            status="blocked",
            detail=f"tmux relaunch failed: {launch_result.stderr.strip()}",
            feedback_id=feedback.feedback_id,
            request_id=feedback.request_id,
            session_name=session_name,
        )
    _mark_every_code_pr_feedback(record_store, feedback=feedback, status="applied")
    _acknowledge_every_code_pr_feedback(feedback=feedback, reaction="rocket", runner=runner)
    return EveryCodePrFeedbackApplyResult(
        status="applied",
        detail="Every Code PR feedback resumed the local session.",
        feedback_id=feedback.feedback_id,
        request_id=feedback.request_id,
        session_name=session_name,
    )


def _acknowledge_every_code_pr_feedback(
    *,
    feedback: EveryCodePrFeedbackRecord,
    reaction: str,
    runner: Runner,
) -> None:
    if feedback.feedback_kind != "issue_comment":
        return
    if not feedback.github_id.strip():
        return
    reference = github_pull_request_reference(pr_url=feedback.pr_url)
    if reference is None:
        return
    try:
        runner(
            (
                "gh",
                "api",
                "--method",
                "POST",
                f"repos/{reference['owner']}/{reference['repo']}/issues/comments/{feedback.github_id.strip()}/reactions",
                "-H",
                "Accept: application/vnd.github+json",
                "-f",
                f"content={reaction}",
            )
        )
    except OSError:
        return


def build_every_code_worker_daemon_spec(
    *,
    state_dir: Path,
    database_url: str = "",
    service_url: str = "",
    worker_token_env: str = "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN",
    host: str = "",
    workspace_root: Path = Path.home() / "Developer",
    checkout_root: Path | None = None,
    worktree_state_dir: Path | None = None,
    repository: str = "",
    command_template: str = "",
    tmux_binary: str = "tmux",
    interval_seconds: float = 60.0,
) -> EveryCodeWorkerDaemonSpec:
    resolved_state_dir = state_dir.expanduser().resolve()
    worker_dir = resolved_state_dir / "every-code-worker"
    command: list[str] = [
        "uv",
        "run",
        "launchplane",
        "every-code",
        "run",
        "--state-dir",
        str(resolved_state_dir),
        "--workspace-root",
        str(workspace_root.expanduser().resolve()),
        "--tmux-binary",
        tmux_binary,
        "--interval-seconds",
        str(interval_seconds),
    ]
    if database_url.strip():
        command.extend(("--database-url", database_url.strip()))
    if service_url.strip():
        command.extend(("--service-url", service_url.strip()))
        command.extend(("--worker-token-env", worker_token_env.strip()))
    if host.strip():
        command.extend(("--host", host.strip()))
    if checkout_root is not None:
        command.extend(("--checkout-root", str(checkout_root.expanduser().resolve())))
    if worktree_state_dir is not None:
        command.extend(("--worktree-state-dir", str(worktree_state_dir.expanduser().resolve())))
    if repository.strip():
        command.extend(("--repository", repository.strip()))
    if command_template.strip():
        command.extend(("--command-template", command_template))
    return EveryCodeWorkerDaemonSpec(
        pid_file=worker_dir / "worker.pid",
        log_file=worker_dir / "worker.log",
        command=tuple(command),
    )


def every_code_worker_daemon_status(
    *, spec: EveryCodeWorkerDaemonSpec
) -> EveryCodeWorkerDaemonStatus:
    pid_payload = _read_pid_file(spec.pid_file)
    if pid_payload is None:
        return EveryCodeWorkerDaemonStatus(
            running=False,
            pid=None,
            pid_file=spec.pid_file,
            log_file=spec.log_file,
            detail="Every Code worker is not running.",
        )
    pid, expected_command = pid_payload
    if _process_is_running(pid):
        command = expected_command or spec.command
        if not _process_matches_expected_command(pid, command):
            return EveryCodeWorkerDaemonStatus(
                running=False,
                pid=pid,
                pid_file=spec.pid_file,
                log_file=spec.log_file,
                detail="Every Code worker pid file belongs to a different process.",
            )
        return EveryCodeWorkerDaemonStatus(
            running=True,
            pid=pid,
            pid_file=spec.pid_file,
            log_file=spec.log_file,
            detail="Every Code worker is running.",
        )
    return EveryCodeWorkerDaemonStatus(
        running=False,
        pid=pid,
        pid_file=spec.pid_file,
        log_file=spec.log_file,
        detail="Every Code worker pid file is stale.",
    )


def start_every_code_worker_daemon(
    *,
    spec: EveryCodeWorkerDaemonSpec,
    cwd: Path,
    launcher: ProcessLauncher | None = None,
) -> EveryCodeWorkerStartResult:
    status = every_code_worker_daemon_status(spec=spec)
    if status.running and status.pid is not None:
        return EveryCodeWorkerStartResult(status="already_running", pid=status.pid, spec=spec)
    spec.pid_file.parent.mkdir(parents=True, exist_ok=True)
    spec.log_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file = spec.pid_file.with_suffix(".lock")
    lock_fd = _acquire_start_lock(lock_file)
    if lock_fd is None:
        racing_status = every_code_worker_daemon_status(spec=spec)
        if racing_status.running and racing_status.pid is not None:
            return EveryCodeWorkerStartResult(
                status="already_running", pid=racing_status.pid, spec=spec
            )
        raise RuntimeError("Every Code worker start is already in progress.")
    try:
        locked_status = every_code_worker_daemon_status(spec=spec)
        if locked_status.running and locked_status.pid is not None:
            return EveryCodeWorkerStartResult(
                status="already_running", pid=locked_status.pid, spec=spec
            )
        launch = launcher or _launch_daemon_process
        process = launch(spec.command, spec.log_file, cwd.expanduser().resolve())
        _write_pid_file(spec.pid_file, process.pid, spec.command)
        return EveryCodeWorkerStartResult(status="started", pid=process.pid, spec=spec)
    finally:
        os.close(lock_fd)
        lock_file.unlink(missing_ok=True)


def stop_every_code_worker_daemon(
    *,
    spec: EveryCodeWorkerDaemonSpec,
    timeout_seconds: float = 5.0,
    sleeper: Callable[[float], None] = time.sleep,
) -> EveryCodeWorkerStopResult:
    status = every_code_worker_daemon_status(spec=spec)
    if not status.running or status.pid is None:
        if spec.pid_file.exists():
            spec.pid_file.unlink()
        return EveryCodeWorkerStopResult(status="not_running", pid=status.pid, detail=status.detail)
    os.kill(status.pid, signal.SIGTERM)
    deadline = time.monotonic() + max(timeout_seconds, 0)
    while time.monotonic() < deadline:
        if not _process_is_running(status.pid):
            spec.pid_file.unlink(missing_ok=True)
            return EveryCodeWorkerStopResult(
                status="stopped",
                pid=status.pid,
                detail="Every Code worker stopped.",
            )
        sleeper(0.1)
    if _process_is_running(status.pid):
        pid_payload = _read_pid_file(spec.pid_file)
        expected_command = pid_payload[1] if pid_payload is not None else spec.command
        if _process_matches_expected_command(status.pid, expected_command):
            os.kill(status.pid, signal.SIGKILL)
    spec.pid_file.unlink(missing_ok=True)
    return EveryCodeWorkerStopResult(
        status="stopped",
        pid=status.pid,
        detail="Every Code worker stop signal sent.",
    )


def finish_every_code_work_request(
    *,
    record_store: EveryCodeWorkerStore,
    request_id: str,
    host: str,
    exit_code: int,
    result_pr_url: str = "",
    result_summary: str = "",
    runner: Runner | None = None,
) -> EveryCodeWorkerFinishResult:
    record = record_store.read_every_code_work_request_record(request_id.strip())
    if record.state in TERMINAL_EVERY_CODE_STATES:
        terminal_pr_url = result_pr_url.strip() or record.result_pr_url
        del runner
        detail_parts = [
            part
            for part in (
                record.result_summary
                or record.error_message
                or "Every Code request is already terminal.",
            )
            if part
        ]
        return EveryCodeWorkerFinishResult(
            status="done" if record.state == "done" else "blocked",
            detail="; ".join(detail_parts),
            request_id=record.request_id,
            repository=record.repository,
            issue_number=record.issue_number,
            result_pr_url=terminal_pr_url,
        )
    succeeded = exit_code == 0
    del runner
    summary = result_summary.strip() or (
        "Every Code session completed successfully."
        if succeeded
        else f"Every Code session exited with status {exit_code}."
    )
    updated_record = apply_every_code_work_request_status(
        record,
        EveryCodeWorkRequestStatusUpdate(
            state="done" if succeeded else "blocked",
            host=host.strip(),
            updated_at=utc_now_timestamp(),
            result_pr_url=result_pr_url.strip(),
            result_summary=summary,
            error_message="" if succeeded else summary,
        ),
    )
    record_store.write_every_code_work_request_record(updated_record)
    return EveryCodeWorkerFinishResult(
        status="done" if succeeded else "blocked",
        detail=summary,
        request_id=updated_record.request_id,
        repository=updated_record.repository,
        issue_number=updated_record.issue_number,
        result_pr_url=updated_record.result_pr_url,
    )


def request_ready_every_code_pr_preview_labels(
    *,
    record_store: EveryCodeWorkerStore,
    repository: str = "",
    limit: int = 50,
    gate_timeout_seconds: int = EVERY_CODE_PREVIEW_GATE_TIMEOUT_SECONDS,
    runner: Runner | None = None,
) -> EveryCodePreviewGateResult:
    normalized_repository = repository.strip()
    records = (
        *record_store.list_every_code_work_request_records(
            state="running",
            repository=normalized_repository,
            limit=limit,
        ),
        *record_store.list_every_code_work_request_records(
            state="done",
            repository=normalized_repository,
            limit=limit,
        ),
    )
    if not records:
        return EveryCodePreviewGateResult()

    run = runner or _run_subprocess
    checked = 0
    labeled = 0
    pending = 0
    blocked = 0
    skipped = 0
    cancelled = 0
    reset = 0
    reviewer_backfilled = 0
    for record in records:
        if _every_code_work_request_closed_before_preview(record):
            cancelled += _cancel_every_code_preview_gates_for_record(
                record_store=record_store,
                record=record,
                reason="Source issue closed before preview was ready.",
            )
            continue
        result_pr_url = _every_code_preview_pr_url_for_record(record, runner=run)
        if not result_pr_url:
            skipped += 1
            continue
        reference = github_pull_request_reference(pr_url=result_pr_url)
        if reference is None:
            skipped += 1
            continue
        payload = _github_pr_view_payload(
            owner=reference["owner"],
            repo=reference["repo"],
            pr_number=reference["pr_number"],
            runner=run,
        )
        if payload is None:
            checked += 1
            blocked += 1
            continue
        gate, gate_reset = _reconcile_every_code_preview_gate_record(
            record_store=record_store,
            record=record,
            pr_number=reference["pr_number"],
            pr_url=result_pr_url,
            payload=payload,
        )
        if gate_reset:
            reset += 1
        readiness = every_code_pr_preview_readiness_from_payload(
            repository=record.repository,
            payload=payload,
        )
        now = utc_now_timestamp()
        if readiness == "ready":
            checked += 1
            ready_gate = gate.model_copy(
                update={
                    "status": "ready",
                    "updated_at": now,
                    "ready_at": gate.ready_at or now,
                    "last_checked_at": now,
                    "pending_reason": "",
                    "blocked_reason": "",
                    "check_summary": _every_code_check_summary(payload),
                }
            )
            record_store.write_every_code_preview_gate_record(ready_gate)
            summary = request_every_code_pr_preview_label(
                result_pr_url=result_pr_url,
                runner=run,
            )
            if summary.startswith("Requested Launchplane preview"):
                record_store.write_every_code_preview_gate_record(
                    ready_gate.model_copy(
                        update={
                            "status": "labeled",
                            "updated_at": now,
                            "labeled_at": now,
                        }
                    )
                )
                labeled += 1
            elif summary:
                record_store.write_every_code_preview_gate_record(
                    ready_gate.model_copy(
                        update={
                            "status": "blocked",
                            "updated_at": now,
                            "blocked_at": now,
                            "blocked_reason": summary,
                        }
                    )
                )
                blocked += 1
            else:
                skipped += 1
        elif readiness == "pending":
            checked += 1
            if _every_code_preview_gate_timed_out(
                gate,
                now=now,
                timeout_seconds=gate_timeout_seconds,
            ):
                record_store.write_every_code_preview_gate_record(
                    gate.model_copy(
                        update={
                            "status": "blocked",
                            "updated_at": now,
                            "blocked_at": gate.blocked_at or now,
                            "last_checked_at": now,
                            "pending_reason": "",
                            "blocked_reason": "Timed out waiting for GitHub checks to become preview-ready.",
                            "check_summary": _every_code_check_summary(payload),
                        }
                    )
                )
                blocked += 1
                continue
            record_store.write_every_code_preview_gate_record(
                gate.model_copy(
                    update={
                        "status": "pending",
                        "updated_at": now,
                        "last_checked_at": now,
                        "pending_reason": _every_code_pending_gate_reason(payload),
                        "blocked_reason": "",
                        "check_summary": _every_code_check_summary(payload),
                    }
                )
            )
            pending += 1
        elif readiness == "blocked":
            checked += 1
            record_store.write_every_code_preview_gate_record(
                gate.model_copy(
                    update={
                        "status": "blocked",
                        "updated_at": now,
                        "blocked_at": gate.blocked_at or now,
                        "last_checked_at": now,
                        "pending_reason": "",
                        "blocked_reason": _every_code_blocked_gate_reason(payload),
                        "check_summary": _every_code_check_summary(payload),
                    }
                )
            )
            blocked += 1
        elif readiness == "cancelled":
            checked += 1
            record_store.write_every_code_preview_gate_record(
                gate.model_copy(
                    update={
                        "status": "cancelled",
                        "updated_at": now,
                        "cancelled_at": gate.cancelled_at or now,
                        "last_checked_at": now,
                        "pending_reason": "",
                        "blocked_reason": "Pull request is no longer open.",
                        "check_summary": _every_code_check_summary(payload),
                    }
                )
            )
            cancelled += 1
        else:
            if readiness == "skipped" and _github_pr_payload_has_label(
                payload,
                launchplane_anchor_repo_preview_label(repo=reference["repo"]),
            ):
                reviewer_backfilled += _backfill_every_code_preview_reviewer(
                    record=record,
                    owner=reference["owner"],
                    repo=reference["repo"],
                    pr_number=reference["pr_number"],
                    runner=run,
                )
            skipped += 1
    return EveryCodePreviewGateResult(
        checked=checked,
        labeled=labeled,
        pending=pending,
        blocked=blocked,
        skipped=skipped,
        cancelled=cancelled,
        reset=reset,
        reviewer_backfilled=reviewer_backfilled,
    )


def _every_code_work_request_closed_before_preview(
    record: EveryCodeWorkRequestRecord,
) -> bool:
    if record.state != "done":
        return False
    summary = (record.result_summary or record.error_message).strip().lower()
    return summary.startswith("source issue closed") or summary.startswith(
        "source pull request closed"
    )


def _cancel_every_code_preview_gates_for_record(
    *,
    record_store: EveryCodeWorkerStore,
    record: EveryCodeWorkRequestRecord,
    reason: str,
) -> int:
    now = utc_now_timestamp()
    cancelled = 0
    for gate in record_store.list_every_code_preview_gate_records(
        request_id=record.request_id,
        limit=100,
    ):
        if gate.status in {"labeled", "cancelled"}:
            continue
        record_store.write_every_code_preview_gate_record(
            gate.model_copy(
                update={
                    "status": "cancelled",
                    "updated_at": now,
                    "cancelled_at": gate.cancelled_at or now,
                    "pending_reason": "",
                    "blocked_reason": reason,
                }
            )
        )
        cancelled += 1
    return cancelled


def _every_code_preview_gate_timed_out(
    gate: EveryCodePreviewGateRecord,
    *,
    now: str,
    timeout_seconds: int,
) -> bool:
    if timeout_seconds <= 0:
        return False
    created_at = _parse_utc_timestamp(gate.created_at)
    checked_at = _parse_utc_timestamp(now)
    if created_at is None or checked_at is None:
        return False
    return (checked_at - created_at).total_seconds() >= timeout_seconds


def _parse_utc_timestamp(timestamp: str) -> datetime | None:
    normalized = timestamp.strip()
    if not normalized:
        return None
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _backfill_every_code_preview_reviewer(
    *,
    record: EveryCodeWorkRequestRecord,
    owner: str,
    repo: str,
    pr_number: int,
    runner: Runner,
) -> int:
    issue_author = _github_issue_author_login(
        owner=owner,
        repo=repo,
        issue_number=record.issue_number,
        runner=runner,
    )
    if not issue_author:
        return 0
    pr_author = _github_pr_author_login(
        owner=owner,
        repo=repo,
        pr_number=pr_number,
        runner=runner,
    )
    if pr_author.casefold() == issue_author.casefold():
        return 0
    requested_reviewers = _github_requested_reviewer_logins(
        owner=owner,
        repo=repo,
        pr_number=pr_number,
        runner=runner,
    )
    if issue_author.casefold() in requested_reviewers:
        return 0
    return int(
        _request_github_pull_request_reviewer(
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            reviewer=issue_author,
            runner=runner,
        )
    )


def _github_issue_author_login(
    *,
    owner: str,
    repo: str,
    issue_number: int,
    runner: Runner,
) -> str:
    payload = _gh_api_payload(
        runner=runner,
        path=f"repos/{owner}/{repo}/issues/{issue_number}",
    )
    user = payload.get("user")
    if not isinstance(user, dict):
        return ""
    login = user.get("login")
    return login.strip() if isinstance(login, str) else ""


def _github_pr_author_login(
    *,
    owner: str,
    repo: str,
    pr_number: int,
    runner: Runner,
) -> str:
    payload = _gh_api_payload(
        runner=runner,
        path=f"repos/{owner}/{repo}/pulls/{pr_number}",
    )
    user = payload.get("user")
    if not isinstance(user, dict):
        return ""
    login = user.get("login")
    return login.strip() if isinstance(login, str) else ""


def _github_requested_reviewer_logins(
    *,
    owner: str,
    repo: str,
    pr_number: int,
    runner: Runner,
) -> frozenset[str]:
    payload = _gh_api_payload(
        runner=runner,
        path=f"repos/{owner}/{repo}/pulls/{pr_number}/requested_reviewers",
    )
    users = payload.get("users")
    if not isinstance(users, list):
        return frozenset()
    logins: set[str] = set()
    for item in users:
        if not isinstance(item, dict):
            continue
        login = item.get("login")
        if isinstance(login, str) and login.strip():
            logins.add(login.strip().casefold())
    return frozenset(logins)


def _request_github_pull_request_reviewer(
    *,
    owner: str,
    repo: str,
    pr_number: int,
    reviewer: str,
    runner: Runner,
) -> bool:
    result = _run_gh_api(
        runner=runner,
        args=(
            "gh",
            "api",
            f"repos/{owner}/{repo}/pulls/{pr_number}/requested_reviewers",
            "--method",
            "POST",
            "--field",
            f"reviewers[]={reviewer}",
        ),
    )
    return result is not None


def _gh_api_payload(*, runner: Runner, path: str) -> dict[str, object]:
    result = _run_gh_api(runner=runner, args=("gh", "api", path))
    if result is None:
        return {}
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _run_gh_api(
    *,
    runner: Runner,
    args: Sequence[str],
) -> subprocess.CompletedProcess[str] | None:
    try:
        result = runner(args)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result


def _reconcile_every_code_preview_gate_record(
    *,
    record_store: EveryCodeWorkerStore,
    record: EveryCodeWorkRequestRecord,
    pr_number: int,
    pr_url: str,
    payload: dict[str, object],
) -> tuple[EveryCodePreviewGateRecord, bool]:
    head_sha = str(payload.get("headRefOid") or "").strip() or "unknown"
    gate_id = build_every_code_preview_gate_id(
        repository=record.repository,
        pr_number=pr_number,
        head_sha=head_sha,
    )
    existing_gates = record_store.list_every_code_preview_gate_records(
        request_id=record.request_id,
        pr_number=pr_number,
        limit=50,
    )
    for gate in existing_gates:
        if gate.gate_id == gate_id:
            return gate, False
    now = utc_now_timestamp()
    gate = EveryCodePreviewGateRecord(
        gate_id=gate_id,
        request_id=record.request_id,
        repository=record.repository,
        issue_number=record.issue_number,
        issue_url=record.issue_url,
        issue_author="",
        pr_number=pr_number,
        pr_url=pr_url,
        head_sha=head_sha,
        status="pending",
        created_at=now,
        updated_at=now,
        last_checked_at=now,
        pending_reason="Waiting for GitHub checks to complete.",
        check_summary=_every_code_check_summary(payload),
    )
    record_store.write_every_code_preview_gate_record(gate)
    cancelled = 0
    for previous_gate in existing_gates:
        if previous_gate.status in {"labeled", "cancelled"}:
            continue
        record_store.write_every_code_preview_gate_record(
            previous_gate.model_copy(
                update={
                    "status": "cancelled",
                    "updated_at": now,
                    "cancelled_at": previous_gate.cancelled_at or now,
                    "blocked_reason": "Superseded by a newer pull request head SHA.",
                }
            )
        )
        cancelled += 1
    return gate, cancelled > 0


def route_every_code_pr_check_failures(
    *,
    record_store: EveryCodeWorkerStore,
    repository: str = "",
    limit: int = 50,
    runner: Runner | None = None,
) -> EveryCodePrFailureRouteResult:
    normalized_repository = repository.strip()
    records = (
        *record_store.list_every_code_work_request_records(
            state="running",
            repository=normalized_repository,
            limit=limit,
        ),
        *record_store.list_every_code_work_request_records(
            state="done",
            repository=normalized_repository,
            limit=limit,
        ),
    )
    if not records:
        return EveryCodePrFailureRouteResult()

    run = runner or _run_subprocess
    checked = 0
    routed = 0
    pending = 0
    duplicate = 0
    retried_infra = 0
    persistent_infra = 0
    recovered = 0
    skipped = 0
    for record in records:
        result_pr_url = _every_code_preview_pr_url_for_record(record, runner=run)
        if not result_pr_url:
            skipped += 1
            continue
        reference = github_pull_request_reference(pr_url=result_pr_url)
        if reference is None:
            skipped += 1
            continue
        payload = _github_pr_view_payload(
            owner=reference["owner"],
            repo=reference["repo"],
            pr_number=reference["pr_number"],
            runner=run,
        )
        if payload is None or str(payload.get("state") or "").upper() != "OPEN":
            skipped += 1
            continue
        check_rollup = payload.get("statusCheckRollup")
        if not isinstance(check_rollup, list):
            pending += 1
            continue
        relevant_checks = _every_code_relevant_pr_checks(check_rollup)
        if not relevant_checks:
            recovered += _ignore_pending_every_code_check_failure_feedback(
                record_store=record_store,
                request_id=record.request_id,
                pr_number=reference["pr_number"],
            )
            continue
        if any(_github_check_status(item) != "COMPLETED" for item in relevant_checks):
            pending += 1
            continue
        checked += 1
        failed_checks = [
            item for item in relevant_checks if _github_check_conclusion(item) != "SUCCESS"
        ]
        if not failed_checks:
            recovered += _ignore_pending_every_code_check_failure_feedback(
                record_store=record_store,
                request_id=record.request_id,
                pr_number=reference["pr_number"],
            )
            continue
        app_failures = [
            item
            for item in failed_checks
            if _every_code_pr_failure_kind(item) == "app"
        ]
        if app_failures:
            feedback = _build_every_code_check_failure_feedback(
                record=record,
                pr_number=reference["pr_number"],
                pr_url=result_pr_url,
                checks=app_failures,
                payload=payload,
            )
            existing = _find_every_code_pr_feedback_record(
                record_store=record_store,
                feedback_id=feedback.feedback_id,
                request_id=record.request_id,
                pr_number=reference["pr_number"],
            )
            if existing is None:
                record_store.write_every_code_pr_feedback_record(feedback)
                routed += 1
            elif existing.status == "pending":
                duplicate += 1
            else:
                duplicate += 1
            continue
        infra_marker = _build_every_code_infra_retry_feedback(
            record=record,
            pr_number=reference["pr_number"],
            pr_url=result_pr_url,
            checks=failed_checks,
            payload=payload,
        )
        existing_marker = _find_every_code_pr_feedback_record(
            record_store=record_store,
            feedback_id=infra_marker.feedback_id,
            request_id=record.request_id,
            pr_number=reference["pr_number"],
        )
        if existing_marker is None:
            _rerun_failed_every_code_infra_checks(
                repository=record.repository,
                checks=failed_checks,
                runner=run,
            )
            record_store.write_every_code_pr_feedback_record(infra_marker)
            retried_infra += 1
        else:
            persistent_infra += 1
    return EveryCodePrFailureRouteResult(
        checked=checked,
        routed=routed,
        pending=pending,
        duplicate=duplicate,
        retried_infra=retried_infra,
        persistent_infra=persistent_infra,
        recovered=recovered,
        skipped=skipped,
    )


def every_code_pr_preview_readiness(
    *,
    result_pr_url: str,
    runner: Runner | None = None,
) -> Literal["ready", "pending", "blocked", "skipped", "cancelled"]:
    reference = github_pull_request_reference(pr_url=result_pr_url.strip())
    if reference is None:
        return "skipped"
    preview_label = launchplane_anchor_repo_preview_label(repo=reference["repo"])
    if not preview_label:
        return "skipped"
    payload = _github_pr_view_payload(
        owner=reference["owner"],
        repo=reference["repo"],
        pr_number=reference["pr_number"],
        runner=runner or _run_subprocess,
    )
    if payload is None:
        return "blocked"
    return every_code_pr_preview_readiness_from_payload(
        repository=f"{reference['owner']}/{reference['repo']}",
        payload=payload,
    )


def every_code_pr_preview_readiness_from_payload(
    *,
    repository: str,
    payload: dict[str, object],
) -> Literal["ready", "pending", "blocked", "skipped", "cancelled"]:
    reference_repo = repository.strip().split("/", 1)[-1]
    preview_label = launchplane_anchor_repo_preview_label(repo=reference_repo)
    if not preview_label:
        return "skipped"
    if str(payload.get("state") or "").upper() != "OPEN":
        return "cancelled"
    if _github_pr_payload_has_label(payload, preview_label):
        return "skipped"
    check_rollup = payload.get("statusCheckRollup")
    if not isinstance(check_rollup, list):
        return "pending"
    relevant_checks = _every_code_relevant_pr_checks(check_rollup)
    if not relevant_checks:
        return "ready"
    if any(_github_check_status(item) != "COMPLETED" for item in relevant_checks):
        return "pending"
    if all(_github_check_conclusion(item) == "SUCCESS" for item in relevant_checks):
        return "ready"
    return "blocked"


def _every_code_relevant_pr_checks(
    check_rollup: list[object],
) -> list[dict[str, object]]:
    return [
        item
        for item in check_rollup
        if isinstance(item, dict) and _github_check_conclusion(item) != "SKIPPED"
    ]


def _every_code_check_summary(payload: dict[str, object]) -> str:
    check_rollup = payload.get("statusCheckRollup")
    if not isinstance(check_rollup, list):
        return "GitHub check status is not available yet."
    checks = _every_code_relevant_pr_checks(check_rollup)
    if not checks:
        return "No required GitHub checks reported."
    return "; ".join(
        f"{_github_check_name(check) or 'unnamed check'}="
        f"{_github_check_status(check) or 'UNKNOWN'}/"
        f"{_github_check_conclusion(check) or 'PENDING'}"
        for check in checks
    )


def _every_code_pending_gate_reason(payload: dict[str, object]) -> str:
    check_rollup = payload.get("statusCheckRollup")
    if not isinstance(check_rollup, list):
        return "Waiting for GitHub check data."
    pending_names = [
        _github_check_name(check) or "unnamed check"
        for check in _every_code_relevant_pr_checks(check_rollup)
        if _github_check_status(check) != "COMPLETED"
    ]
    if pending_names:
        return "Waiting for checks: " + ", ".join(sorted(pending_names))
    return "Waiting for GitHub checks to settle."


def _every_code_blocked_gate_reason(payload: dict[str, object]) -> str:
    check_rollup = payload.get("statusCheckRollup")
    if not isinstance(check_rollup, list):
        return "Could not read GitHub check state."
    failed_names = [
        _github_check_name(check) or "unnamed check"
        for check in _every_code_relevant_pr_checks(check_rollup)
        if _github_check_status(check) == "COMPLETED"
        and _github_check_conclusion(check) != "SUCCESS"
    ]
    if failed_names:
        return "Checks did not pass: " + ", ".join(sorted(failed_names))
    return "GitHub checks are blocked."


def _github_check_status(check: dict[str, object]) -> str:
    return str(check.get("status") or "").upper()


def _github_check_conclusion(check: dict[str, object]) -> str:
    return str(check.get("conclusion") or "").upper()


def _github_check_name(check: dict[str, object]) -> str:
    return str(check.get("name") or "").strip()


def _github_check_details_url(check: dict[str, object]) -> str:
    value = check.get("detailsUrl") or check.get("details_url") or check.get("url")
    return str(value or "").strip()


def _every_code_pr_failure_kind(check: dict[str, object]) -> Literal["app", "infra"]:
    name = _github_check_name(check).lower().replace("-", "_").replace(" ", "_")
    infra_names = {
        "prepare_preview",
        "preview_prepare",
        "publish_preview_image",
        "provision_preview",
        "verify_preview",
        "report_preview_feedback",
        "request_pending_feedback",
        "launchplane_preview",
        "preview_control_plane",
    }
    if name in infra_names or name.startswith("preview_"):
        return "infra"
    return "app"


def _every_code_check_failure_key(
    *,
    record: EveryCodeWorkRequestRecord,
    pr_number: int,
    checks: Sequence[dict[str, object]],
    payload: dict[str, object],
) -> str:
    head_sha = str(payload.get("headRefOid") or "").strip()[:16]
    check_identity = "|".join(
        sorted(
            f"{_github_check_name(check)}:{_github_check_conclusion(check)}:{_github_check_details_url(check)}"
            for check in checks
        )
    )
    digest = hashlib.sha256(
        f"{record.request_id}|{pr_number}|{head_sha}|{check_identity}".encode("utf-8")
    ).hexdigest()[:16]
    return f"{head_sha or 'unknown'}-{digest}"


def _build_every_code_check_failure_feedback(
    *,
    record: EveryCodeWorkRequestRecord,
    pr_number: int,
    pr_url: str,
    checks: Sequence[dict[str, object]],
    payload: dict[str, object],
) -> EveryCodePrFeedbackRecord:
    key = _every_code_check_failure_key(
        record=record,
        pr_number=pr_number,
        checks=checks,
        payload=payload,
    )
    github_id = f"check-failure-{key}"
    now = utc_now_timestamp()
    return EveryCodePrFeedbackRecord(
        feedback_id=f"every-code-pr-feedback-{record.repository.replace('/', '-')}-{pr_number}-{github_id}",
        request_id=record.request_id,
        repository=record.repository,
        pr_number=pr_number,
        pr_url=pr_url,
        feedback_kind="issue_comment",
        github_delivery_id=f"launchplane-check-failure-{key}",
        github_node_id=github_id,
        actor="launchplane",
        body=_every_code_check_failure_feedback_body(checks=checks, payload=payload),
        html_url=pr_url,
        received_at=now,
        submitted_at=now,
        status="pending",
    )


def _build_every_code_infra_retry_feedback(
    *,
    record: EveryCodeWorkRequestRecord,
    pr_number: int,
    pr_url: str,
    checks: Sequence[dict[str, object]],
    payload: dict[str, object],
) -> EveryCodePrFeedbackRecord:
    key = _every_code_check_failure_key(
        record=record,
        pr_number=pr_number,
        checks=checks,
        payload=payload,
    )
    github_id = f"infra-retry-{key}"
    now = utc_now_timestamp()
    return EveryCodePrFeedbackRecord(
        feedback_id=f"every-code-pr-feedback-{record.repository.replace('/', '-')}-{pr_number}-{github_id}",
        request_id=record.request_id,
        repository=record.repository,
        pr_number=pr_number,
        pr_url=pr_url,
        feedback_kind="issue_comment",
        github_delivery_id=f"launchplane-infra-retry-{key}",
        github_node_id=github_id,
        actor="launchplane",
        body=_every_code_infra_retry_feedback_body(checks=checks, payload=payload),
        html_url=pr_url,
        received_at=now,
        submitted_at=now,
        status="ignored",
    )


def _every_code_check_failure_feedback_body(
    *,
    checks: Sequence[dict[str, object]],
    payload: dict[str, object],
) -> str:
    head_sha = str(payload.get("headRefOid") or "").strip()
    lines = [
        "GitHub checks failed on this Every Code PR. Treat this as actionable PR feedback and fix the same branch.",
    ]
    if head_sha:
        lines.append(f"Head SHA: {head_sha}")
    lines.append("")
    lines.append("Failed checks:")
    for check in checks:
        detail = _github_check_details_url(check)
        suffix = f" - {detail}" if detail else ""
        lines.append(
            f"- {_github_check_name(check) or 'unnamed check'}: {_github_check_conclusion(check) or 'FAILED'}{suffix}"
        )
    return "\n".join(lines)


def _every_code_infra_retry_feedback_body(
    *,
    checks: Sequence[dict[str, object]],
    payload: dict[str, object],
) -> str:
    head_sha = str(payload.get("headRefOid") or "").strip()
    lines = ["Launchplane retried a preview infrastructure failure for this PR."]
    if head_sha:
        lines.append(f"Head SHA: {head_sha}")
    lines.append("")
    lines.append("Infra checks:")
    for check in checks:
        detail = _github_check_details_url(check)
        suffix = f" - {detail}" if detail else ""
        lines.append(
            f"- {_github_check_name(check) or 'unnamed check'}: {_github_check_conclusion(check) or 'FAILED'}{suffix}"
        )
    return "\n".join(lines)


def _find_every_code_pr_feedback_record(
    *,
    record_store: EveryCodeWorkerStore,
    feedback_id: str,
    request_id: str,
    pr_number: int,
) -> EveryCodePrFeedbackRecord | None:
    for feedback in record_store.list_every_code_pr_feedback_records(
        request_id=request_id,
        pr_number=pr_number,
        limit=200,
    ):
        if feedback.feedback_id == feedback_id:
            return feedback
    return None


def _ignore_pending_every_code_check_failure_feedback(
    *,
    record_store: EveryCodeWorkerStore,
    request_id: str,
    pr_number: int,
) -> int:
    ignored = 0
    for feedback in record_store.list_every_code_pr_feedback_records(
        request_id=request_id,
        pr_number=pr_number,
        status="pending",
        limit=200,
    ):
        if "check-failure-" not in feedback.feedback_id:
            continue
        record_store.write_every_code_pr_feedback_record(
            feedback.model_copy(update={"status": "ignored"})
        )
        ignored += 1
    return ignored


def _rerun_failed_every_code_infra_checks(
    *,
    repository: str,
    checks: Sequence[dict[str, object]],
    runner: Runner,
) -> None:
    rerun_run_ids = {
        run_id
        for check in checks
        if (run_id := _github_actions_run_id_from_url(_github_check_details_url(check)))
    }
    for run_id in sorted(rerun_run_ids):
        try:
            runner(("gh", "run", "rerun", run_id, "--repo", repository, "--failed"))
        except OSError:
            continue


def _github_actions_run_id_from_url(url: str) -> str:
    match = re.search(r"/actions/runs/(\d+)(?:/|$)", url)
    if match is None:
        return ""
    return match.group(1)


def _every_code_preview_pr_url_for_record(
    record: EveryCodeWorkRequestRecord,
    *,
    runner: Runner,
) -> str:
    result_pr_url = record.result_pr_url.strip()
    if result_pr_url:
        return result_pr_url
    if record.state != "running":
        return ""
    branch = every_code_worktree_branch(record)
    try:
        result = runner(
            (
                "gh",
                "pr",
                "list",
                "--repo",
                record.repository.strip(),
                "--state",
                "open",
                "--head",
                branch,
                "--json",
                "url",
                "--limit",
                "1",
            )
        )
    except OSError:
        return ""
    if result.returncode != 0:
        return ""
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, list) or not payload:
        return ""
    first = payload[0]
    if not isinstance(first, dict):
        return ""
    url = first.get("url")
    if not isinstance(url, str):
        return ""
    return url.strip()


def _github_pr_view_payload(
    *,
    owner: str,
    repo: str,
    pr_number: int,
    runner: Runner,
) -> dict[str, object] | None:
    try:
        result = runner(
            (
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--repo",
                f"{owner}/{repo}",
                "--json",
                "state,labels,headRefOid,statusCheckRollup",
            )
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _github_pr_payload_has_label(payload: dict[str, object], label_name: str) -> bool:
    labels = payload.get("labels")
    if not isinstance(labels, list):
        return False
    for label in labels:
        if isinstance(label, dict) and label.get("name") == label_name:
            return True
    return False


def request_every_code_pr_preview_label(
    *,
    result_pr_url: str,
    runner: Runner | None = None,
) -> str:
    reference = github_pull_request_reference(pr_url=result_pr_url.strip())
    if reference is None:
        return ""
    preview_label = launchplane_anchor_repo_preview_label(repo=reference["repo"])
    if not preview_label:
        return ""
    run = runner or _run_subprocess
    repo = f"{reference['owner']}/{reference['repo']}"
    try:
        result = run(
            (
                "gh",
                "pr",
                "edit",
                str(reference["pr_number"]),
                "--repo",
                repo,
                "--add-label",
                preview_label,
            )
        )
    except OSError as exc:
        return f"Could not request Launchplane preview: {exc}"
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "gh pr edit failed"
        return f"Could not request Launchplane preview: {detail}"
    return f"Requested Launchplane preview with `{preview_label}`."


def _mark_every_code_pr_feedback(
    record_store: EveryCodeWorkerStore,
    *,
    feedback: EveryCodePrFeedbackRecord,
    status: EveryCodePrFeedbackStatus,
) -> EveryCodePrFeedbackRecord | None:
    if feedback.status != "pending":
        return None
    updated_feedback = feedback.model_copy(update={"status": status})
    record_store.write_every_code_pr_feedback_record(updated_feedback)
    return updated_feedback


def _terminal_every_code_request_closed_by_linked_pr(record: EveryCodeWorkRequestRecord) -> bool:
    if record.state not in TERMINAL_EVERY_CODE_STATES:
        return False
    summary = record.result_summary.strip()
    return summary.startswith("Linked pull request merged:") or summary.startswith(
        "Linked pull request closed without merge:"
    )


def _send_every_code_pr_feedback_to_session(
    *,
    feedback: EveryCodePrFeedbackRecord,
    session_name: str,
    tmux_binary: str,
    runner: Runner,
) -> bool:
    prompt = every_code_pr_feedback_prompt(feedback)
    try:
        result = runner((tmux_binary, "send-keys", "-t", session_name, prompt))
        if result.returncode != 0:
            return False
        result = runner((tmux_binary, "send-keys", "-t", session_name, "C-m"))
    except OSError:
        return False
    return result.returncode == 0


def _run_subprocess(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, check=False, text=True)


def _ensure_every_code_worktree(
    *,
    source_checkout_root: Path,
    worktree_root: Path,
    branch: str,
    runner: Runner,
) -> None:
    if not _is_git_checkout(source_checkout_root):
        raise RuntimeError(f"Checkout root is not a git checkout: {source_checkout_root}")
    if worktree_root.exists():
        if not worktree_root.is_dir():
            raise RuntimeError(f"Every Code worktree path is not a directory: {worktree_root}")
        if not _is_git_checkout(worktree_root):
            raise RuntimeError(
                f"Existing Every Code worktree is not a git checkout: {worktree_root}"
            )
        actual_branch = _git_output(
            ("git", "-C", str(worktree_root), "branch", "--show-current"),
            runner=runner,
            detail="inspect Every Code worktree branch",
        )
        if actual_branch != branch:
            raise RuntimeError(
                "Existing Every Code worktree uses unexpected branch "
                f"{actual_branch!r}; expected {branch!r}: {worktree_root}"
            )
        return

    worktree_root.parent.mkdir(parents=True, exist_ok=True)
    if _git_branch_exists(source_checkout_root, branch=branch, runner=runner):
        _git_checked(
            (
                "git",
                "-C",
                str(source_checkout_root),
                "worktree",
                "add",
                str(worktree_root),
                branch,
            ),
            runner=runner,
            detail="create Every Code worktree from existing branch",
        )
    else:
        default_branch = _git_default_branch(source_checkout_root, runner=runner)
        _git_checked(
            ("git", "-C", str(source_checkout_root), "fetch", "--quiet", "origin", default_branch),
            runner=runner,
            detail="fetch default branch before creating Every Code worktree",
        )
        _git_checked(
            (
                "git",
                "-C",
                str(source_checkout_root),
                "worktree",
                "add",
                "-b",
                branch,
                str(worktree_root),
                f"origin/{default_branch}",
            ),
            runner=runner,
            detail="create Every Code worktree",
        )


def _git_default_branch(source_checkout_root: Path, *, runner: Runner) -> str:
    origin_head = _git_output(
        (
            "git",
            "-C",
            str(source_checkout_root),
            "symbolic-ref",
            "--short",
            "refs/remotes/origin/HEAD",
        ),
        runner=runner,
        detail="inspect origin default branch",
    )
    if origin_head.startswith("origin/"):
        return origin_head.removeprefix("origin/")
    if origin_head:
        return origin_head
    raise RuntimeError(f"Could not determine default branch for {source_checkout_root}")


def _git_branch_exists(source_checkout_root: Path, *, branch: str, runner: Runner) -> bool:
    try:
        result = runner(
            (
                "git",
                "-C",
                str(source_checkout_root),
                "show-ref",
                "--verify",
                "--quiet",
                f"refs/heads/{branch}",
            )
        )
    except OSError as exc:
        raise RuntimeError(f"Could not inspect Every Code worktree branch: {exc}") from exc
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    message = result.stderr.strip() or result.stdout.strip() or "git show-ref failed"
    raise RuntimeError(f"Could not inspect Every Code worktree branch: {message}")


def _git_output(args: Sequence[str], *, runner: Runner, detail: str) -> str:
    result = _git_checked(args, runner=runner, detail=detail)
    return result.stdout.strip()


def _git_checked(
    args: Sequence[str], *, runner: Runner, detail: str
) -> subprocess.CompletedProcess[str]:
    try:
        result = runner(args)
    except OSError as exc:
        raise RuntimeError(f"Could not {detail}: {exc}") from exc
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or f"{args[0]} failed"
        raise RuntimeError(f"Could not {detail}: {message}")
    return result


def _is_git_checkout(path: Path) -> bool:
    return (path / ".git").exists()


def _safe_path_component(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-._")
    return normalized or "request"


def _safe_branch_component(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-._")
    return normalized or "request"


def _launch_daemon_process(args: Sequence[str], log_file: Path, cwd: Path) -> subprocess.Popen[str]:
    log_handle = log_file.open("a", encoding="utf-8")
    try:
        return subprocess.Popen(
            args,
            cwd=cwd,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
    finally:
        log_handle.close()


def _write_pid_file(pid_file: Path, pid: int, command: Sequence[str]) -> None:
    temporary_file = pid_file.with_suffix(".tmp")
    temporary_file.write_text(
        json.dumps({"pid": pid, "command": list(command)}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary_file.replace(pid_file)


def _acquire_start_lock(lock_file: Path) -> int | None:
    stale_lock_pid = _read_lock_pid(lock_file)
    if stale_lock_pid is not None and not _process_is_running(stale_lock_pid):
        lock_file.unlink(missing_ok=True)
    try:
        lock_fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return None
    os.write(lock_fd, str(os.getpid()).encode("utf-8"))
    return lock_fd


def _read_lock_pid(lock_file: Path) -> int | None:
    try:
        lock_text = lock_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        pid = int(lock_text)
    except ValueError:
        return None
    if pid > 0:
        return pid
    return None


def _read_pid_file(pid_file: Path) -> tuple[int, tuple[str, ...]] | None:
    try:
        payload = json.loads(pid_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    pid = payload.get("pid") if isinstance(payload, dict) else None
    if isinstance(pid, int) and pid > 0:
        command = payload.get("command")
        if isinstance(command, list) and all(isinstance(part, str) for part in command):
            return pid, tuple(command)
        return pid, ()
    return None


def _process_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_matches_expected_command(pid: int, expected_command: Sequence[str]) -> bool:
    if not expected_command:
        return True
    try:
        result = subprocess.run(
            ("ps", "-ww", "-p", str(pid), "-o", "command="),
            capture_output=True,
            check=False,
            text=True,
        )
    except OSError:
        return False
    if result.returncode != 0:
        return False
    command_text = result.stdout.strip()
    if not command_text:
        return False
    expected_tail = " ".join(expected_command[1:])
    if expected_tail:
        return expected_tail in command_text
    return expected_command[0] in command_text


def _tmux_session_exists(*, tmux_binary: str, session_name: str, runner: Runner) -> bool | None:
    try:
        result = runner((tmux_binary, "has-session", "-t", session_name))
    except OSError:
        return None
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None


def _tmux_pane_pid(*, tmux_binary: str, session_name: str, runner: Runner) -> int | None:
    try:
        result = runner(
            (
                tmux_binary,
                "display-message",
                "-p",
                "-t",
                session_name,
                "#{pane_pid}",
            )
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    try:
        pane_pid = int(result.stdout.strip())
    except ValueError:
        return None
    return pane_pid if pane_pid > 0 else None


def _block_every_code_request(
    *,
    record_store: EveryCodeWorkerStore,
    record: EveryCodeWorkRequestRecord,
    host: str,
    detail: str,
    session_name: str,
    checkout_root: Path,
) -> EveryCodeWorkerHandoffResult:
    blocked_record = apply_every_code_work_request_status(
        record_store.read_every_code_work_request_record(record.request_id),
        EveryCodeWorkRequestStatusUpdate(
            state="blocked",
            host=host,
            updated_at=utc_now_timestamp(),
            error_message=detail,
        ),
    )
    record_store.write_every_code_work_request_record(blocked_record)
    return EveryCodeWorkerHandoffResult(
        status="blocked",
        detail=detail,
        request_id=blocked_record.request_id,
        repository=blocked_record.repository,
        issue_number=blocked_record.issue_number,
        session_name=session_name,
        checkout_root=str(checkout_root),
    )
