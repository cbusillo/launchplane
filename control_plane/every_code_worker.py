from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import time
from typing import Callable, Literal, Protocol, Sequence

from control_plane.contracts.every_code_work_request import (
    EveryCodeWorkRequestRecord,
    EveryCodeWorkRequestStatusUpdate,
    apply_every_code_work_request_status,
)
from control_plane.workflows.ship import utc_now_timestamp


EveryCodeWorkerStatus = Literal["empty", "running", "blocked"]
EveryCodeWorkerFinishStatus = Literal["done", "blocked"]


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


Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class DaemonProcess(Protocol):
    pid: int


ProcessLauncher = Callable[[Sequence[str], Path, Path], DaemonProcess]


@dataclass(frozen=True)
class EveryCodeWorkerHandoffResult:
    status: EveryCodeWorkerStatus
    detail: str
    request_id: str = ""
    repository: str = ""
    issue_number: int = 0
    session_name: str = ""
    checkout_root: str = ""

    def as_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "detail": self.detail,
            "request_id": self.request_id,
            "repository": self.repository,
            "issue_number": self.issue_number,
            "session_name": self.session_name,
            "checkout_root": self.checkout_root,
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


def every_code_tmux_session_name(request_id: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.:-]+", "-", request_id.strip()).strip("-._:")
    return f"every-code-{normalized or 'request'}"[:80]


def default_every_code_command(record: EveryCodeWorkRequestRecord) -> str:
    prompt = (
        f"Work on {record.repository} issue #{record.issue_number}: {record.issue_title}. "
        f"Issue URL: {record.issue_url}. Launchplane request: {record.request_id}."
    )
    return "code " + shlex.quote(prompt)


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
    finish_shell = " ".join(
        "$status" if part == "$status" else shlex.quote(part) for part in finish_command
    )
    return (
        f"{command}\n"
        "status=$?\n"
        f"{finish_shell}\n"
        "exit $status"
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


def run_every_code_worker_once(
    *,
    record_store: EveryCodeWorkerStore,
    host: str,
    workspace_root: Path,
    checkout_root: Path | None = None,
    repository: str = "",
    command_template: str = "",
    state_dir: Path | None = None,
    database_url: str = "",
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

    resolved_checkout_root = resolve_every_code_checkout_root(
        claimed_record,
        workspace_root=workspace_root,
        checkout_root=checkout_root,
    )
    session_name = every_code_tmux_session_name(claimed_record.request_id)
    if not resolved_checkout_root.is_dir():
        return _block_every_code_request(
            record_store=record_store,
            record=claimed_record,
            host=normalized_host,
            detail=f"Checkout root does not exist: {resolved_checkout_root}",
            session_name=session_name,
            checkout_root=resolved_checkout_root,
        )

    run = runner or _run_subprocess
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
            checkout_root=resolved_checkout_root,
        )
    if not existing_session:
        command = render_every_code_command(claimed_record, command_template=command_template)
        if state_dir is not None:
            command = build_every_code_session_command(
                record=claimed_record,
                command=command,
                state_dir=state_dir,
                database_url=database_url,
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
                checkout_root=resolved_checkout_root,
            )
        if launch_result.returncode != 0:
            return _block_every_code_request(
                record_store=record_store,
                record=claimed_record,
                host=normalized_host,
                detail=f"tmux launch failed: {launch_result.stderr.strip()}",
                session_name=session_name,
                checkout_root=resolved_checkout_root,
            )

    running_record = apply_every_code_work_request_status(
        record_store.read_every_code_work_request_record(claimed_record.request_id),
        EveryCodeWorkRequestStatusUpdate(
            state="running",
            host=normalized_host,
            updated_at=utc_now_timestamp(),
            result_summary=f"Visible tmux session: {session_name}",
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
        checkout_root=str(resolved_checkout_root),
    )


def run_every_code_worker_loop(
    *,
    record_store: EveryCodeWorkerStore,
    host: str,
    workspace_root: Path,
    checkout_root: Path | None = None,
    repository: str = "",
    command_template: str = "",
    state_dir: Path | None = None,
    database_url: str = "",
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
        last_result = run_every_code_worker_once(
            record_store=record_store,
            host=host,
            workspace_root=workspace_root,
            checkout_root=checkout_root,
            repository=repository,
            command_template=command_template,
            state_dir=state_dir,
            database_url=database_url,
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


def build_every_code_worker_daemon_spec(
    *,
    state_dir: Path,
    database_url: str = "",
    host: str = "",
    workspace_root: Path = Path.home() / "Developer",
    checkout_root: Path | None = None,
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
    if host.strip():
        command.extend(("--host", host.strip()))
    if checkout_root is not None:
        command.extend(("--checkout-root", str(checkout_root.expanduser().resolve())))
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
        return EveryCodeWorkerStartResult(
            status="already_running", pid=status.pid, spec=spec
        )
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
        return EveryCodeWorkerStopResult(
            status="not_running", pid=status.pid, detail=status.detail
        )
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
) -> EveryCodeWorkerFinishResult:
    record = record_store.read_every_code_work_request_record(request_id.strip())
    succeeded = exit_code == 0
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


def _run_subprocess(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, check=False, text=True)


def _launch_daemon_process(
    args: Sequence[str], log_file: Path, cwd: Path
) -> subprocess.Popen[str]:
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
        if isinstance(command, list) and all(
            isinstance(part, str) for part in command
        ):
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
