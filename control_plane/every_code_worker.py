from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import shlex
import subprocess
from typing import Callable, Literal, Protocol, Sequence

from control_plane.contracts.every_code_work_request import (
    EveryCodeWorkRequestRecord,
    EveryCodeWorkRequestStatusUpdate,
    apply_every_code_work_request_status,
)
from control_plane.workflows.ship import utc_now_timestamp


EveryCodeWorkerStatus = Literal["empty", "running", "blocked"]


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


def _run_subprocess(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, check=False, text=True)


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
