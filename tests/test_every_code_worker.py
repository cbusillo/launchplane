import os
import subprocess
import unittest
from collections.abc import Mapping, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import signal
import shutil
import threading
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from click.testing import CliRunner
from pydantic import BaseModel

from control_plane.cli import main
from control_plane.contracts.every_code_pr_feedback_record import EveryCodePrFeedbackRecord
from control_plane.contracts.every_code_preview_gate_record import EveryCodePreviewGateRecord
from control_plane.contracts.every_code_preview_gate_record import build_every_code_preview_gate_id
from control_plane.contracts.every_code_work_request import EveryCodeWorkRequestRecord
from control_plane.contracts.every_code_work_request import requeue_every_code_work_request
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.product_profile_record import ProductImageProfile
from control_plane.contracts.product_profile_record import ProductPreviewProfile
from control_plane.every_code_worker import (
    EveryCodeWorkerApiError,
    EveryCodeWorkerApiStore,
    apply_every_code_pr_feedback_for_host,
    build_every_code_worker_daemon_spec,
    build_every_code_session_command,
    close_terminal_every_code_sessions,
    default_every_code_command,
    every_code_claim_comment_body,
    every_code_session_state_path,
    every_code_tmux_session_name,
    every_code_worktree_branch,
    every_code_worktree_root,
    every_code_worker_daemon_status,
    finish_every_code_work_request,
    prepare_every_code_checkout,
    request_ready_every_code_pr_preview_labels,
    request_every_code_pr_preview_label,
    recover_stale_every_code_work_requests,
    reconcile_every_code_worker_cleanup_state,
    route_every_code_pr_check_failures,
    run_every_code_worker_loop,
    run_every_code_worker_once,
    start_every_code_heartbeat_thread,
    start_every_code_worker_daemon,
    stop_every_code_worker_daemon,
)
from control_plane.storage.filesystem import FilesystemRecordStore


def _queued_record() -> EveryCodeWorkRequestRecord:
    return EveryCodeWorkRequestRecord(
        request_id="every-code-cbusillo-code-123-test",
        source="github_issue_label",
        state="queued",
        repository="cbusillo/code",
        issue_number=123,
        issue_url="https://github.com/cbusillo/code/issues/123",
        issue_title="Wire local automation",
        trigger_label="every-code",
        trigger_actor="cbusillo",
        github_delivery_id="delivery-1",
        queued_at="2026-05-05T22:00:00Z",
        updated_at="2026-05-05T22:00:00Z",
    )


def _queued_preview_record() -> EveryCodeWorkRequestRecord:
    return _queued_record().model_copy(
        update={
            "request_id": "every-code-every-tenant-opw-123-test",
            "repository": "every/tenant-opw",
            "issue_url": "https://github.com/every/tenant-opw/issues/123",
        }
    )


def _claimed_record() -> EveryCodeWorkRequestRecord:
    return _queued_record().model_copy(
        update={
            "state": "claimed",
            "claimed_at": "2026-05-05T22:01:00Z",
            "claimed_by_host": "Chris-Studio",
            "lease_expires_at": "2026-05-05T22:31:00Z",
            "fencing_token": 1,
            "attempt": 1,
        }
    )


def _current_fencing_token(
    store: FilesystemRecordStore,
    request_id: str = "every-code-cbusillo-code-123-test",
) -> int:
    return store.read_every_code_work_request_record(request_id).fencing_token


def _feedback_record(*, status: str = "pending") -> EveryCodePrFeedbackRecord:
    return EveryCodePrFeedbackRecord(
        feedback_id="every-code-pr-feedback-cbusillo-code-26-ic-1001",
        request_id="every-code-cbusillo-code-123-test",
        repository="cbusillo/code",
        pr_number=26,
        pr_url="https://github.com/cbusillo/code/pull/26",
        feedback_kind="issue_comment",
        github_delivery_id="delivery-comment",
        github_node_id="IC_kwDO_test_1001",
        github_id="1001",
        actor="cbusillo",
        body="Please tighten the README wording before merge.",
        html_url="https://github.com/cbusillo/code/pull/26#issuecomment-1001",
        received_at="2026-05-06T19:00:00Z",
        status=status,  # type: ignore[arg-type]
    )


def _done_record(*, repository: str, result_pr_url: str) -> EveryCodeWorkRequestRecord:
    return _queued_record().model_copy(
        update={
            "state": "done",
            "repository": repository,
            "result_pr_url": result_pr_url,
            "claimed_at": "2026-05-05T22:01:00Z",
            "claimed_by_host": "Chris-Studio",
            "fencing_token": 1,
            "attempt": 1,
            "started_at": "2026-05-05T22:02:00Z",
            "finished_at": "2026-05-05T22:03:00Z",
        }
    )


def _preview_gate_record(
    *, status: str = "pending", head_sha: str = "abcdef1234567890"
) -> EveryCodePreviewGateRecord:
    return EveryCodePreviewGateRecord(
        gate_id=build_every_code_preview_gate_id(
            repository="cbusillo/sellyouroutboard",
            pr_number=86,
            head_sha=head_sha,
        ),
        request_id="every-code-cbusillo-code-123-test",
        repository="cbusillo/sellyouroutboard",
        issue_number=123,
        issue_url="https://github.com/cbusillo/sellyouroutboard/issues/123",
        issue_author="Mbanks89",
        pr_number=86,
        pr_url="https://github.com/cbusillo/sellyouroutboard/pull/86",
        head_sha=head_sha,
        status=status,  # type: ignore[arg-type]
        created_at="2026-05-06T20:00:00Z",
        updated_at="2026-05-06T20:00:00Z",
        ready_at="2026-05-06T20:01:00Z" if status == "ready" else "",
        labeled_at="2026-05-06T20:02:00Z" if status == "labeled" else "",
        blocked_at="2026-05-06T20:03:00Z" if status == "blocked" else "",
        cancelled_at="2026-05-06T20:04:00Z" if status == "cancelled" else "",
        last_checked_at="2026-05-06T20:00:00Z",
    )


def _preview_product_profile(
    *,
    product: str,
    repository: str,
    preview_context: str,
    enable_label: str = "launchplane-preview",
) -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord(
        product=product,
        display_name=product.title(),
        repository=repository,
        driver_id="generic-web",
        image=ProductImageProfile(repository=f"ghcr.io/{repository}"),
        runtime_port=3000,
        health_path="/health",
        preview=ProductPreviewProfile(
            enabled=True,
            context=preview_context,
            enable_label=enable_label,
        ),
        updated_at="2026-05-09T00:00:00Z",
        source="test",
    )


def _write_sellyouroutboard_preview_profile(store: FilesystemRecordStore) -> None:
    store.write_product_profile_record(
        _preview_product_profile(
            product="sellyouroutboard",
            repository="cbusillo/sellyouroutboard",
            preview_context="sellyouroutboard-testing",
            enable_label="preview",
        )
    )


def _terminal_record(*, state: str = "done") -> EveryCodeWorkRequestRecord:
    return _queued_record().model_copy(
        update={
            "state": state,
            "claimed_at": "2026-05-06T00:00:00Z",
            "claimed_by_host": "Chris-Studio",
            "fencing_token": 1,
            "attempt": 1,
            "started_at": "2026-05-06T00:01:00Z",
            "finished_at": "2026-05-06T00:02:00Z" if state in {"done", "blocked"} else "",
            "updated_at": "2026-05-06T00:02:00Z",
            "result_summary": "Linked pull request merged.",
        }
    )


def _write_cleanup_session_state(
    *,
    state_dir: Path,
    request_id: str = "every-code-cbusillo-code-123-test",
    host: str = "Chris-Studio",
    source_checkout_root: Path,
    launch_root: Path,
    worktree_branch: str | None = None,
    session_name: str | None = None,
) -> Path:
    path = every_code_session_state_path(state_dir=state_dir, request_id=request_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "request_id": request_id,
                "session_name": session_name or every_code_tmux_session_name(request_id),
                "source_checkout_root": str(source_checkout_root),
                "launch_root": str(launch_root),
                "worktree_branch": worktree_branch or every_code_worktree_branch(_queued_record()),
                "host": host,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


class _Runner:
    def __init__(
        self,
        *,
        fail_issue_comment: bool = False,
        issue_comment_error_detail: str = "rate limited",
        existing_branch: bool = False,
        pr_view_payload: dict[str, object] | None = None,
        pr_list_payload: list[dict[str, object]] | None = None,
        gh_api_payloads: dict[str, dict[str, object]] | None = None,
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.fail_issue_comment = fail_issue_comment
        self.issue_comment_error_detail = issue_comment_error_detail
        self.existing_branch = existing_branch
        self.pr_view_payload = pr_view_payload
        self.pr_list_payload = pr_list_payload
        self.gh_api_payloads = gh_api_payloads or {}

    def __call__(
        self, args: Sequence[str], env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(tuple(args))
        if args[0] == "git" and args[3:6] == (
            "symbolic-ref",
            "--short",
            "refs/remotes/origin/HEAD",
        ):
            return subprocess.CompletedProcess(args, 0, "origin/main\n", "")
        if args[0] == "git" and args[3:6] == ("show-ref", "--verify", "--quiet"):
            return subprocess.CompletedProcess(args, 0 if self.existing_branch else 1, "", "")
        if args[0] == "git" and args[3:5] == ("branch", "--show-current"):
            return subprocess.CompletedProcess(
                args, 0, every_code_worktree_branch(_queued_record()) + "\n", ""
            )
        if args[0] == "git" and args[3:6] == ("worktree", "add", "-b"):
            worktree_root = Path(args[7])
            worktree_root.mkdir(parents=True, exist_ok=True)
            (worktree_root / ".git").write_text("gitdir: test\n", encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == "git" and args[3:5] == ("worktree", "add"):
            worktree_root = Path(args[5])
            worktree_root.mkdir(parents=True, exist_ok=True)
            (worktree_root / ".git").write_text("gitdir: test\n", encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == "git":
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:3] == ("gh", "issue", "comment"):
            if env is None or env.get("GH_TOKEN") != "bot-token":
                return subprocess.CompletedProcess(args, 1, "", "missing bot token")
            if self.fail_issue_comment:
                return subprocess.CompletedProcess(args, 1, "", self.issue_comment_error_detail)
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:3] == ("gh", "issue", "edit"):
            if env is None or env.get("GH_TOKEN") != "bot-token":
                return subprocess.CompletedProcess(args, 1, "", "missing bot token")
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:3] == ("gh", "api", "user"):
            if env is None or env.get("GH_TOKEN") != "bot-token":
                return subprocess.CompletedProcess(args, 1, "", "missing bot token")
            return subprocess.CompletedProcess(args, 0, "shiny-code-bot\n", "")
        if args[:3] == ("gh", "pr", "view") and self.pr_view_payload is not None:
            return subprocess.CompletedProcess(args, 0, json.dumps(self.pr_view_payload), "")
        if args[:3] == ("gh", "pr", "list") and self.pr_list_payload is not None:
            return subprocess.CompletedProcess(args, 0, json.dumps(self.pr_list_payload), "")
        if args[:2] == ("gh", "api"):
            path = args[2]
            if "--method" in args:
                return subprocess.CompletedProcess(args, 0, json.dumps({"ok": True}), "")
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps(self.gh_api_payloads.get(path, {})),
                "",
            )
        if args[1] == "display-message":
            return subprocess.CompletedProcess(args, 0, "4242\n", "")
        if args[1] == "has-session":
            return subprocess.CompletedProcess(args, 1, "", "no session")
        return subprocess.CompletedProcess(args, 0, "", "")


class _ExistingSessionRunner(_Runner):
    def __call__(
        self, args: Sequence[str], env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        del env
        self.calls.append(tuple(args))
        if args[1] == "has-session":
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[1] == "display-message":
            return subprocess.CompletedProcess(args, 0, "4242\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")


class _FailingFeedbackSendRunner(_ExistingSessionRunner):
    def __call__(
        self, args: Sequence[str], env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        del env
        self.calls.append(tuple(args))
        if args[1] == "has-session":
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[1] == "display-message":
            return subprocess.CompletedProcess(args, 0, "4242\n", "")
        if args[1] == "send-keys":
            return subprocess.CompletedProcess(args, 1, "", "send failed")
        return subprocess.CompletedProcess(args, 0, "", "")


class _GoneSessionRunner(_Runner):
    def __call__(
        self, args: Sequence[str], env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(tuple(args))
        if args[1] == "has-session":
            return subprocess.CompletedProcess(args, 1, "", "no session")
        if args[0] == "tmux" and args[1] == "new-session":
            return subprocess.CompletedProcess(args, 0, "", "")
        return super().__call__(args, env)


class _FailingFeedbackRelaunchRunner(_GoneSessionRunner):
    def __call__(
        self, args: Sequence[str], env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        if args[0] == "tmux" and args[1] == "new-session":
            self.calls.append(tuple(args))
            return subprocess.CompletedProcess(
                args,
                1,
                "",
                (
                    "Authorization: Basic dXNlcjpzaG91bGQtbm90LXN1cnZpdmU=\n"
                    "Cookie: launchplane_session=short-cookie-secret\n"
                    "connect worker.internal failed"
                ),
            )
        return super().__call__(args, env)


class _CleanupReconciliationRunner(_ExistingSessionRunner):
    def __init__(
        self,
        *,
        worktree_root: Path,
        branch_exists: bool = True,
        branch_delete_fails: bool = False,
        dirty: bool = False,
        tmux_active: bool = False,
        tmux_inspection_fails: bool = False,
        registered: bool = True,
    ) -> None:
        super().__init__(existing_branch=branch_exists)
        self.worktree_root = worktree_root
        self.branch_delete_fails = branch_delete_fails
        self.dirty = dirty
        self.tmux_active = tmux_active
        self.tmux_inspection_fails = tmux_inspection_fails
        self.registered = registered

    def __call__(
        self, args: Sequence[str], env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        del env
        self.calls.append(tuple(args))
        if args[1:3] == ("has-session", "-t"):
            if self.tmux_inspection_fails:
                return subprocess.CompletedProcess(args, 2, "", "tmux failed")
            return subprocess.CompletedProcess(args, 0 if self.tmux_active else 1, "", "")
        if args[0] == "git" and args[3:5] == ("worktree", "list"):
            output = f"worktree {self.worktree_root}\nHEAD abcdef\nbranch refs/heads/test\n"
            return subprocess.CompletedProcess(args, 0, output if self.registered else "", "")
        if args[0] == "git" and args[3:5] == ("status", "--porcelain"):
            return subprocess.CompletedProcess(args, 0, " M README.md\n" if self.dirty else "", "")
        if args[0] == "git" and args[3:5] == ("branch", "-d") and self.branch_delete_fails:
            return subprocess.CompletedProcess(args, 1, "", "not merged")
        if args[0] == "git" and args[3:5] == ("for-each-ref", "--format=%(refname:short)"):
            return subprocess.CompletedProcess(
                args,
                0,
                "every-code/cbusillo-code-123-every-code-cbusillo-code-123-test\n"
                "every-code/unlinked\n",
                "",
            )
        self.calls.pop()
        return _Runner.__call__(self, args, None)


class _CleanupStoreMissingRequest(FilesystemRecordStore):
    def read_every_code_work_request_record(self, request_id: str) -> EveryCodeWorkRequestRecord:
        raise FileNotFoundError(request_id)


class _GoneSessionWithWorktreeProcessRunner(_GoneSessionRunner):
    def __call__(
        self, args: Sequence[str], env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(tuple(args))
        if args[0] == "lsof":
            return subprocess.CompletedProcess(args, 0, "9001\n9002\n", "")
        if args[1] == "has-session":
            return subprocess.CompletedProcess(args, 1, "", "no session")
        return super().__call__(args, env)


class _Process:
    def __init__(self, pid: int) -> None:
        self.pid = pid


class _MaintenanceFailingStore(FilesystemRecordStore):
    def list_every_code_pr_feedback_records(
        self, **kwargs: object
    ) -> tuple[EveryCodePrFeedbackRecord, ...]:
        if kwargs.get("status") == "pending":
            raise EveryCodeWorkerApiError("Launchplane API request failed with HTTP 401")
        return super().list_every_code_pr_feedback_records(**kwargs)  # type: ignore[arg-type]


class _EveryCodeApiHandler(BaseHTTPRequestHandler):
    store: FilesystemRecordStore
    token = "worker-token"

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        if self.headers.get("Authorization") != f"Bearer {self.token}":
            self._write_json(401, {"status": "rejected"})
            return
        if self.path.startswith("/v1/every-code/preview-gates"):
            query = parse_qs(urlparse(self.path).query)
            gate_records = self.store.list_every_code_preview_gate_records(
                status=str((query.get("status") or [""])[0]),
                limit=10,
            )
            self._write_json(
                200,
                {
                    "status": "ok",
                    "gates": [record.model_dump(mode="json") for record in gate_records],
                },
            )
            return
        if self.path.startswith("/v1/every-code/pr-feedback"):
            query = parse_qs(urlparse(self.path).query)
            feedback_records = self.store.list_every_code_pr_feedback_records(
                status=str((query.get("status") or [""])[0]),
                limit=10,
            )
            self._write_json(
                200,
                {
                    "status": "ok",
                    "feedback": [record.model_dump(mode="json") for record in feedback_records],
                },
            )
            return
        if self.path.startswith("/v1/product-profiles"):
            query = parse_qs(urlparse(self.path).query)
            profile_records = self.store.list_product_profile_records(
                driver_id=str((query.get("driver_id") or [""])[0])
            )
            self._write_json(
                200,
                {
                    "status": "ok",
                    "profiles": [record.model_dump(mode="json") for record in profile_records],
                },
            )
            return
        if self.path.startswith("/v1/every-code/work-requests?"):
            work_request_records = self.store.list_every_code_work_request_records(
                state="queued", limit=1
            )
            self._write_json(
                200,
                {
                    "status": "ok",
                    "requests": [record.model_dump(mode="json") for record in work_request_records],
                },
            )
            return
        request_id = self.path.rsplit("/", 1)[-1]
        record = self.store.read_every_code_work_request_record(request_id)
        self._write_json(200, {"status": "ok", "request": record.model_dump(mode="json")})

    def do_POST(self) -> None:
        if self.headers.get("Authorization") != f"Bearer {self.token}":
            self._write_json(401, {"status": "rejected"})
            return
        content_length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        if self.path == "/v1/every-code/preview-gates":
            gate_record = EveryCodePreviewGateRecord.model_validate(payload)
            self.store.write_every_code_preview_gate_record(gate_record)
            self._write_json(
                202,
                {
                    "status": "accepted",
                    "records": {"gate_id": gate_record.gate_id, "status": gate_record.status},
                    "result": {"gate": gate_record.model_dump(mode="json")},
                },
            )
            return
        if self.path == "/v1/every-code/pr-feedback":
            feedback_record = EveryCodePrFeedbackRecord.model_validate(payload)
            self.store.write_every_code_pr_feedback_record(feedback_record)
            self._write_json(
                202,
                {
                    "status": "accepted",
                    "records": {
                        "request_id": feedback_record.request_id,
                        "feedback_id": feedback_record.feedback_id,
                        "status": feedback_record.status,
                    },
                    "result": {"feedback": feedback_record.model_dump(mode="json")},
                },
            )
            return
        if self.path == "/v1/every-code/pr-feedback/status":
            feedback_records = self.store.list_every_code_pr_feedback_records(
                request_id=str(payload["request_id"]),
                limit=10,
            )
            feedback_record = next(
                record
                for record in feedback_records
                if record.feedback_id == payload["feedback_id"]
            )
            updated_feedback_record = feedback_record.model_copy(
                update={"status": payload["status"]}
            )
            self.store.write_every_code_pr_feedback_record(updated_feedback_record)
            self._write_json(
                202,
                {
                    "status": "accepted",
                    "records": {
                        "request_id": updated_feedback_record.request_id,
                        "feedback_id": updated_feedback_record.feedback_id,
                        "status": updated_feedback_record.status,
                    },
                    "result": {"feedback": updated_feedback_record.model_dump(mode="json")},
                },
            )
            return
        if self.path == "/v1/every-code/work-requests/rerun":
            work_request_record = self.store.read_every_code_work_request_record(
                str(payload["request_id"])
            )
            requeued_record = work_request_record.model_copy(
                update={
                    "state": "queued",
                    "trigger_actor": str(payload.get("trigger_actor") or "rerun"),
                    "updated_at": "2026-05-06T00:02:00Z",
                    "queued_at": "2026-05-06T00:02:00Z",
                    "claimed_at": "",
                    "claimed_by_host": "",
                    "started_at": "",
                    "finished_at": "",
                    "result_pr_url": "",
                    "result_summary": "",
                    "error_message": "",
                }
            )
            self.store.write_every_code_work_request_record(requeued_record)
            self._write_json(
                202,
                {
                    "status": "accepted",
                    "result": {"request": requeued_record.model_dump(mode="json")},
                },
            )
            return
        if self.path == "/v1/every-code/work-requests/claim":
            claimed_record = self.store.claim_every_code_work_request_record(
                request_id=str(payload["request_id"]),
                host=str(payload["host"]),
                claimed_at="2026-05-06T00:00:00Z",
            )
            if claimed_record is None:
                self._write_json(409, {"status": "rejected"})
                return
            self._write_json(
                202,
                {
                    "status": "accepted",
                    "records": {
                        "request_id": claimed_record.request_id,
                        "state": claimed_record.state,
                    },
                    "result": {"request": claimed_record.model_dump(mode="json")},
                },
            )
            return
        if self.path == "/v1/every-code/work-requests/recover-stale":
            stale_records = self.store.list_stale_every_code_work_request_records(
                as_of="2026-05-06T00:30:00Z",
                limit=20,
            )
            requeued: list[str] = []
            flagged: list[str] = []
            for stale_record in stale_records:
                recovered = self.store.recover_stale_every_code_work_request_record(
                    expected_record=stale_record,
                    recovered_at="2026-05-06T00:30:00Z",
                )
                if recovered is None:
                    continue
                if recovered.state == "queued":
                    requeued.append(recovered.request_id)
                elif recovered.state == "blocked":
                    flagged.append(recovered.request_id)
            self._write_json(
                202,
                {
                    "status": "accepted",
                    "records": {
                        "checked": len(stale_records),
                        "requeued": len(requeued),
                        "flagged": len(flagged),
                    },
                    "result": {"requeued": requeued, "flagged": flagged},
                },
            )
            return
        if self.path == "/v1/every-code/work-requests/rerun":
            work_request_record = self.store.read_every_code_work_request_record(
                str(payload["request_id"])
            )
            requeued_work_request_record = requeue_every_code_work_request(
                work_request_record,
                queued_at="2026-05-06T00:02:00Z",
                trigger_actor=str(payload.get("trigger_actor") or ""),
            )
            self.store.write_every_code_work_request_record(requeued_work_request_record)
            self._write_json(
                202,
                {
                    "status": "accepted",
                    "records": {
                        "request_id": requeued_work_request_record.request_id,
                        "state": requeued_work_request_record.state,
                    },
                    "result": {"request": requeued_work_request_record.model_dump(mode="json")},
                },
            )
            return
        work_request_record = self.store.read_every_code_work_request_record(
            str(payload["request_id"])
        )
        updated_work_request_record = work_request_record.model_copy(
            update={
                "state": payload["state"],
                "updated_at": payload["updated_at"],
                "started_at": payload["updated_at"],
                "finished_at": payload["updated_at"] if payload["state"] != "running" else "",
                "result_pr_url": payload.get("result_pr_url", ""),
                "result_summary": payload.get("result_summary", ""),
                "error_message": payload.get("error_message", ""),
            }
        )
        self.store.write_every_code_work_request_record(updated_work_request_record)
        self._write_json(
            202,
            {
                "status": "accepted",
                "records": {
                    "request_id": updated_work_request_record.request_id,
                    "state": updated_work_request_record.state,
                },
                "result": {"request": updated_work_request_record.model_dump(mode="json")},
            },
        )

    def _write_json(self, status_code: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class EveryCodeWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._github_env_patch = patch.dict(
            os.environ,
            {
                "LAUNCHPLANE_EVERY_CODE_GITHUB_TOKEN": "bot-token",
                "LAUNCHPLANE_EVERY_CODE_GITHUB_ACTOR": "shiny-code-bot",
            },
        )
        self._github_env_patch.start()

    def tearDown(self) -> None:
        self._github_env_patch.stop()

    def test_filesystem_claim_serializes_concurrent_workers(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            first_store = FilesystemRecordStore(state_dir=state_dir)
            second_store = FilesystemRecordStore(state_dir=state_dir)
            first_store.write_every_code_work_request_record(_queued_record())
            first_write_started = threading.Event()
            release_first_write = threading.Event()
            second_finished = threading.Event()
            first_results: list[EveryCodeWorkRequestRecord | None] = []
            second_results: list[EveryCodeWorkRequestRecord | None] = []
            errors: list[BaseException] = []
            original_write = first_store._write_model

            def paused_write(record_type: str, record_id: str, model: BaseModel) -> Path:
                if (
                    record_type == "launchplane_every_code_work_requests"
                    and isinstance(model, EveryCodeWorkRequestRecord)
                    and model.state == "claimed"
                ):
                    first_write_started.set()
                    if not release_first_write.wait(timeout=5):
                        raise TimeoutError("timed out waiting to release first claim write")
                return original_write(record_type, record_id, model)

            def claim_first() -> None:
                try:
                    first_results.append(
                        first_store.claim_every_code_work_request_record(
                            request_id=_queued_record().request_id,
                            host="worker-a",
                            claimed_at="2026-05-05T22:01:00Z",
                        )
                    )
                except BaseException as error:
                    errors.append(error)

            def claim_second() -> None:
                try:
                    second_results.append(
                        second_store.claim_every_code_work_request_record(
                            request_id=_queued_record().request_id,
                            host="worker-b",
                            claimed_at="2026-05-05T22:01:01Z",
                        )
                    )
                except BaseException as error:
                    errors.append(error)
                finally:
                    second_finished.set()

            with patch.object(first_store, "_write_model", side_effect=paused_write):
                first_thread = threading.Thread(target=claim_first)
                second_thread = threading.Thread(target=claim_second)
                first_thread.start()
                self.assertTrue(first_write_started.wait(timeout=5))
                second_thread.start()
                self.assertFalse(second_finished.wait(timeout=0.1))
                release_first_write.set()
                first_thread.join(timeout=5)
                second_thread.join(timeout=5)

            self.assertFalse(first_thread.is_alive())
            self.assertFalse(second_thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(first_results), 1)
            self.assertIsNotNone(first_results[0])
            self.assertEqual(second_results, [None])

    def test_heartbeat_rejection_stops_and_notifies_lease_loss(self) -> None:
        class RejectingHeartbeatStore(FilesystemRecordStore):
            def heartbeat_every_code_work_request_record(
                self,
                *,
                request_id: str,
                host: str,
                fencing_token: int,
                heartbeat_at: str,
                lease_expires_at: str,
                lease_seconds: int = 1800,
            ) -> bool:
                del request_id, host, fencing_token, heartbeat_at, lease_expires_at, lease_seconds
                return False

        with TemporaryDirectory() as temporary_directory_name:
            stop_event = threading.Event()
            lease_lost = threading.Event()
            thread = start_every_code_heartbeat_thread(
                record_store=RejectingHeartbeatStore(
                    state_dir=Path(temporary_directory_name) / "state"
                ),
                request_id=_queued_record().request_id,
                host="worker-a",
                fencing_token=1,
                interval_seconds=0.01,
                stop_event=stop_event,
                on_lease_lost=lease_lost.set,
            )

            self.assertTrue(lease_lost.wait(timeout=2))
            thread.join(timeout=2)

        self.assertTrue(stop_event.is_set())
        self.assertFalse(thread.is_alive())

    def test_stale_recovery_does_not_overwrite_changed_snapshot(self) -> None:
        class RejectingRecoveryStore(FilesystemRecordStore):
            def recover_stale_every_code_work_request_record(
                self,
                *,
                expected_record: EveryCodeWorkRequestRecord,
                recovered_at: str,
            ) -> EveryCodeWorkRequestRecord | None:
                del expected_record, recovered_at
                return None

        with TemporaryDirectory() as temporary_directory_name:
            store = RejectingRecoveryStore(state_dir=Path(temporary_directory_name) / "state")
            stale_record = _queued_record().model_copy(
                update={
                    "state": "running",
                    "claimed_at": "2026-05-05T22:01:00Z",
                    "claimed_by_host": "worker-a",
                    "lease_expires_at": "2026-05-05T22:02:00Z",
                    "fencing_token": 1,
                    "attempt": 1,
                    "started_at": "2026-05-05T22:01:30Z",
                }
            )
            store.write_every_code_work_request_record(stale_record)

            recovered = recover_stale_every_code_work_requests(record_store=store)
            stored = store.read_every_code_work_request_record(stale_record.request_id)

        self.assertEqual(recovered, 0)
        self.assertEqual(stored, stale_record)

    def test_api_store_lists_claims_and_updates_via_service(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            _EveryCodeApiHandler.store = FilesystemRecordStore(state_dir=temporary_root / "state")
            _EveryCodeApiHandler.store.write_every_code_work_request_record(_queued_record())
            server = ThreadingHTTPServer(("127.0.0.1", 0), _EveryCodeApiHandler)
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            try:
                store = EveryCodeWorkerApiStore(
                    service_url=f"http://127.0.0.1:{server.server_port}",
                    worker_token="worker-token",
                )

                records = store.list_every_code_work_request_records(state="queued", limit=1)
                claimed_record = store.claim_every_code_work_request_record(
                    request_id=records[0].request_id,
                    host="Chris-Studio",
                    claimed_at="ignored-by-service",
                )
                assert claimed_record is not None
                running_record = claimed_record.model_copy(
                    update={
                        "state": "running",
                        "started_at": "2026-05-06T00:01:00Z",
                        "updated_at": "2026-05-06T00:01:00Z",
                        "result_summary": "Visible tmux session: every-code-test",
                    }
                )
                store.write_every_code_work_request_record(running_record)
                read_record = store.read_every_code_work_request_record(records[0].request_id)
            finally:
                server.shutdown()
                server.server_close()

        self.assertEqual(len(records), 1)
        self.assertEqual(claimed_record.claimed_by_host, "Chris-Studio")
        self.assertEqual(read_record.state, "running")
        self.assertEqual(read_record.result_summary, "Visible tmux session: every-code-test")

    def test_api_store_recovers_stale_requests_via_service(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            _EveryCodeApiHandler.store = FilesystemRecordStore(state_dir=temporary_root / "state")
            _EveryCodeApiHandler.store.write_every_code_work_request_record(_queued_record())
            claimed = _EveryCodeApiHandler.store.claim_every_code_work_request_record(
                request_id=_queued_record().request_id,
                host="Chris-Studio",
                claimed_at="2026-05-05T22:01:00Z",
            )
            assert claimed is not None
            server = ThreadingHTTPServer(("127.0.0.1", 0), _EveryCodeApiHandler)
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            try:
                store = EveryCodeWorkerApiStore(
                    service_url=f"http://127.0.0.1:{server.server_port}",
                    worker_token="worker-token",
                )

                recovered = recover_stale_every_code_work_requests(record_store=store)
                record = _EveryCodeApiHandler.store.read_every_code_work_request_record(
                    claimed.request_id
                )
            finally:
                server.shutdown()
                server.server_close()
                server_thread.join(timeout=5)

        self.assertEqual(recovered, 1)
        self.assertEqual(record.state, "queued")

    def test_api_store_lists_and_updates_pr_feedback_via_service(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            _EveryCodeApiHandler.store = FilesystemRecordStore(state_dir=temporary_root / "state")
            _EveryCodeApiHandler.store.write_every_code_pr_feedback_record(_feedback_record())
            server = ThreadingHTTPServer(("127.0.0.1", 0), _EveryCodeApiHandler)
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            try:
                store = EveryCodeWorkerApiStore(
                    service_url=f"http://127.0.0.1:{server.server_port}",
                    worker_token="worker-token",
                )

                feedback_records = store.list_every_code_pr_feedback_records(status="pending")
                applied_record = feedback_records[0].model_copy(update={"status": "applied"})
                store.write_every_code_pr_feedback_record(applied_record)
                listed_after_update = store.list_every_code_pr_feedback_records(status="applied")
            finally:
                server.shutdown()
                server.server_close()
                server_thread.join(timeout=5)

        self.assertEqual(len(feedback_records), 1)
        self.assertEqual(feedback_records[0].feedback_id, _feedback_record().feedback_id)
        self.assertEqual(listed_after_update[0].status, "applied")

    def test_api_store_creates_pr_feedback_via_service(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            _EveryCodeApiHandler.store = FilesystemRecordStore(state_dir=temporary_root / "state")
            server = ThreadingHTTPServer(("127.0.0.1", 0), _EveryCodeApiHandler)
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            try:
                store = EveryCodeWorkerApiStore(
                    service_url=f"http://127.0.0.1:{server.server_port}",
                    worker_token="worker-token",
                )

                store.write_every_code_pr_feedback_record(_feedback_record())
                feedback_records = store.list_every_code_pr_feedback_records(status="pending")
            finally:
                server.shutdown()
                server.server_close()
                server_thread.join(timeout=5)

        self.assertEqual(len(feedback_records), 1)
        self.assertEqual(feedback_records[0].feedback_id, _feedback_record().feedback_id)

    def test_api_store_lists_and_writes_preview_gates_via_service(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            _EveryCodeApiHandler.store = FilesystemRecordStore(state_dir=temporary_root / "state")
            _EveryCodeApiHandler.store.write_every_code_preview_gate_record(_preview_gate_record())
            server = ThreadingHTTPServer(("127.0.0.1", 0), _EveryCodeApiHandler)
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            try:
                store = EveryCodeWorkerApiStore(
                    service_url=f"http://127.0.0.1:{server.server_port}",
                    worker_token="worker-token",
                )

                gate_records = store.list_every_code_preview_gate_records(status="pending")
                blocked_record = gate_records[0].model_copy(
                    update={
                        "status": "blocked",
                        "updated_at": "2026-05-06T20:05:00Z",
                        "blocked_at": "2026-05-06T20:05:00Z",
                    }
                )
                store.write_every_code_preview_gate_record(blocked_record)
                listed_after_update = store.list_every_code_preview_gate_records(status="blocked")
            finally:
                server.shutdown()
                server.server_close()
                server_thread.join(timeout=5)

        self.assertEqual(len(gate_records), 1)
        self.assertEqual(gate_records[0].gate_id, _preview_gate_record().gate_id)
        self.assertEqual(listed_after_update[0].status, "blocked")

    def test_api_store_lists_product_profiles_via_service(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            _EveryCodeApiHandler.store = FilesystemRecordStore(state_dir=temporary_root / "state")
            _EveryCodeApiHandler.store.write_product_profile_record(
                _preview_product_profile(
                    product="sellyouroutboard",
                    repository="cbusillo/sellyouroutboard",
                    preview_context="sellyouroutboard-testing",
                    enable_label="preview",
                )
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), _EveryCodeApiHandler)
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            try:
                store = EveryCodeWorkerApiStore(
                    service_url=f"http://127.0.0.1:{server.server_port}",
                    worker_token="worker-token",
                )

                profiles = store.list_product_profile_records(driver_id="generic-web")
            finally:
                server.shutdown()
                server.server_close()

        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0].repository, "cbusillo/sellyouroutboard")
        self.assertEqual(profiles[0].preview.enable_label, "preview")

    def test_api_store_reruns_terminal_request_via_service(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            _EveryCodeApiHandler.store = FilesystemRecordStore(state_dir=temporary_root / "state")
            _EveryCodeApiHandler.store.write_every_code_work_request_record(
                _queued_record().model_copy(
                    update={
                        "state": "blocked",
                        "claimed_at": "2026-05-06T00:00:00Z",
                        "claimed_by_host": "Chris-Studio",
                        "started_at": "2026-05-06T00:01:00Z",
                        "finished_at": "2026-05-06T00:02:00Z",
                        "updated_at": "2026-05-06T00:02:00Z",
                        "result_pr_url": "https://github.com/cbusillo/code/pull/26",
                        "result_summary": "Detached session went stale.",
                        "error_message": "Detached session went stale.",
                    }
                )
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), _EveryCodeApiHandler)
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            try:
                store = EveryCodeWorkerApiStore(
                    service_url=f"http://127.0.0.1:{server.server_port}",
                    worker_token="worker-token",
                )

                requeued_record = store.rerun_every_code_work_request_record(
                    request_id=_queued_record().request_id,
                    trigger_actor="cbusillo",
                )
            finally:
                server.shutdown()
                server.server_close()
                server_thread.join(timeout=5)

        self.assertEqual(requeued_record.state, "queued")
        self.assertEqual(requeued_record.trigger_actor, "cbusillo")
        self.assertEqual(requeued_record.claimed_by_host, "")
        self.assertEqual(requeued_record.result_pr_url, "")
        self.assertEqual(requeued_record.error_message, "")

    def test_session_name_is_stable_and_tmux_safe(self) -> None:
        session_name = every_code_tmux_session_name("every code/cbusillo/code#123 !")
        fenced_session_name = every_code_tmux_session_name(
            "every code/cbusillo/code#123 !",
            fencing_token=2,
        )

        self.assertEqual(session_name, "every-code-every-code-cbusillo-code-123")
        self.assertEqual(fenced_session_name, "every-code-every-code-cbusillo-code-123-f2")

    def test_default_command_includes_issue_and_request(self) -> None:
        command = default_every_code_command(_queued_record())

        self.assertIn("https://github.com/cbusillo/code/issues/123", command)
        self.assertIn("every-code-cbusillo-code-123-test", command)
        self.assertIn("read the issue body and every issue comment", command)
        self.assertIn("newer comments", command)
        self.assertIn("images or attachments", command)
        self.assertIn("isolated Every Code worktree", command)
        self.assertIn("Closes #123", command)
        self.assertIn("Use `Refs` only", command)
        self.assertIn("run closeout hygiene", command)
        self.assertIn("Love Gate", command)
        self.assertIn("let the session exit", command)

    def test_claim_comment_body_marks_issue_as_in_progress(self) -> None:
        body = every_code_claim_comment_body(
            _queued_record(),
            host="Chris-Studio",
            session_name="every-code-test",
        )

        self.assertIn("<!-- every-code-claim -->", body)
        self.assertIn("Every Code is working on this issue", body)
        self.assertIn("`Chris-Studio`", body)
        self.assertIn("`every-code-test`", body)
        self.assertIn("`every-code-cbusillo-code-123-test`", body)

    def test_worktree_path_and_branch_are_deterministic(self) -> None:
        record = _queued_record()
        root = every_code_worktree_root(record, state_dir=Path("state"))
        branch = every_code_worktree_branch(record)

        self.assertEqual(
            root,
            Path("state").resolve()
            / "every-code-worker"
            / "worktrees"
            / "cbusillo-code"
            / "every-code-cbusillo-code-123-test",
        )
        self.assertEqual(
            branch,
            "every-code/cbusillo-code-123-every-code-cbusillo-code-123-test",
        )

    def test_session_command_reports_terminal_status(self) -> None:
        command = build_every_code_session_command(
            record=_claimed_record(),
            command="code issue",
            state_dir=Path("state"),
            host="Chris-Studio",
            service_url="https://launchplane.example",
        )

        self.assertIn("code issue", command)
        self.assertIn("every-code finish", command)
        self.assertIn("--service-url https://launchplane.example", command)
        self.assertIn("--worker-token-env LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN", command)
        self.assertIn("--request-id every-code-cbusillo-code-123-test", command)
        self.assertIn("--fencing-token 1", command)
        self.assertIn("--exit-code $status", command)
        self.assertIn("EVERY_CODE_SESSION_ORIGIN=every_code", command)
        self.assertIn("EVERY_CODE_REQUEST_ID=every-code-cbusillo-code-123-test", command)
        self.assertIn("EVERY_CODE_REPOSITORY=cbusillo/code", command)
        self.assertIn("EVERY_CODE_ISSUE_NUMBER=123", command)
        self.assertIn("EVERY_CODE_ISSUE_URL=https://github.com/cbusillo/code/issues/123", command)
        self.assertIn("AGENT_SESSION_ORIGIN=every_code", command)
        self.assertIn("AGENT_SESSION_REQUEST_ID=every-code-cbusillo-code-123-test", command)
        self.assertIn("AGENT_SESSION_REPOSITORY=cbusillo/code", command)
        self.assertIn("AGENT_SESSION_ISSUE_NUMBER=123", command)
        self.assertIn(
            "AGENT_SESSION_ISSUE_URL=https://github.com/cbusillo/code/issues/123", command
        )

    def test_preview_label_request_labels_eligible_pull_request(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            store.write_product_profile_record(
                _preview_product_profile(
                    product="tenant-opw",
                    repository="every/tenant-opw",
                    preview_context="opw",
                )
            )
            runner = _Runner()

            summary = request_every_code_pr_preview_label(
                record_store=store,
                result_pr_url="https://github.com/every/tenant-opw/pull/123",
                runner=runner,
            )

        self.assertIn("Requested Launchplane preview", summary)
        self.assertIn(
            (
                "gh",
                "pr",
                "edit",
                "123",
                "--repo",
                "every/tenant-opw",
                "--add-label",
                "launchplane-preview",
            ),
            runner.calls,
        )

    def test_preview_label_request_labels_sellyouroutboard_pull_request(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            store.write_product_profile_record(
                _preview_product_profile(
                    product="sellyouroutboard",
                    repository="cbusillo/sellyouroutboard",
                    preview_context="sellyouroutboard-testing",
                    enable_label="preview",
                )
            )
            runner = _Runner()

            summary = request_every_code_pr_preview_label(
                record_store=store,
                result_pr_url="https://github.com/cbusillo/sellyouroutboard/pull/71",
                runner=runner,
            )

        self.assertIn("Requested Launchplane preview", summary)
        self.assertIn(
            (
                "gh",
                "pr",
                "edit",
                "71",
                "--repo",
                "cbusillo/sellyouroutboard",
                "--add-label",
                "preview",
            ),
            runner.calls,
        )

    def test_preview_label_request_skips_ineligible_pull_request(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            runner = _Runner()

            summary = request_every_code_pr_preview_label(
                record_store=store,
                result_pr_url="https://github.com/cbusillo/code/pull/123",
                runner=runner,
            )

        self.assertEqual(summary, "")
        self.assertEqual(runner.calls, [])

    def test_preview_gate_labels_done_pull_request_after_checks_pass(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            _write_sellyouroutboard_preview_profile(store)
            store.write_every_code_work_request_record(
                _done_record(
                    repository="cbusillo/sellyouroutboard",
                    result_pr_url="https://github.com/cbusillo/sellyouroutboard/pull/86",
                ).model_copy(
                    update={"issue_url": "https://github.com/cbusillo/sellyouroutboard/issues/123"}
                )
            )
            runner = _Runner(
                pr_view_payload={
                    "state": "OPEN",
                    "headRefOid": "abcdef1234567890",
                    "labels": [],
                    "statusCheckRollup": [
                        {"name": "static_checks", "status": "COMPLETED", "conclusion": "SUCCESS"},
                        {"name": "preview", "status": "COMPLETED", "conclusion": "SKIPPED"},
                    ],
                }
            )

            result = request_ready_every_code_pr_preview_labels(
                record_store=store,
                runner=runner,
            )
            gate_records = store.list_every_code_preview_gate_records(
                request_id="every-code-cbusillo-code-123-test",
                pr_number=86,
            )

        self.assertEqual(result.labeled, 1)
        self.assertEqual(len(gate_records), 1)
        self.assertEqual(gate_records[0].status, "labeled")
        self.assertEqual(gate_records[0].head_sha, "abcdef1234567890")
        self.assertIn(
            (
                "gh",
                "pr",
                "edit",
                "86",
                "--repo",
                "cbusillo/sellyouroutboard",
                "--add-label",
                "preview",
            ),
            runner.calls,
        )

    def test_preview_gate_waits_for_incomplete_checks(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            _write_sellyouroutboard_preview_profile(store)
            store.write_every_code_work_request_record(
                _done_record(
                    repository="cbusillo/sellyouroutboard",
                    result_pr_url="https://github.com/cbusillo/sellyouroutboard/pull/86",
                )
            )
            runner = _Runner(
                pr_view_payload={
                    "state": "OPEN",
                    "headRefOid": "abcdef1234567890",
                    "labels": [],
                    "statusCheckRollup": [
                        {"name": "static_checks", "status": "IN_PROGRESS", "conclusion": ""},
                    ],
                }
            )

            result = request_ready_every_code_pr_preview_labels(
                record_store=store,
                runner=runner,
            )
            gate_records = store.list_every_code_preview_gate_records(status="pending")

        self.assertEqual(result.pending, 1)
        self.assertEqual(len(gate_records), 1)
        self.assertIn("static_checks", gate_records[0].pending_reason)
        self.assertFalse(any(call[:3] == ("gh", "pr", "edit") for call in runner.calls))

    def test_preview_gate_blocks_stale_pending_checks_after_timeout(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            _write_sellyouroutboard_preview_profile(store)
            store.write_every_code_work_request_record(
                _done_record(
                    repository="cbusillo/sellyouroutboard",
                    result_pr_url="https://github.com/cbusillo/sellyouroutboard/pull/86",
                )
            )
            store.write_every_code_preview_gate_record(_preview_gate_record())
            runner = _Runner(
                pr_view_payload={
                    "state": "OPEN",
                    "headRefOid": "abcdef1234567890",
                    "labels": [],
                    "statusCheckRollup": [
                        {"name": "static_checks", "status": "IN_PROGRESS", "conclusion": ""},
                    ],
                }
            )

            result = request_ready_every_code_pr_preview_labels(
                record_store=store,
                gate_timeout_seconds=1,
                runner=runner,
            )
            gate_records = store.list_every_code_preview_gate_records(status="blocked")

        self.assertEqual(result.blocked, 1)
        self.assertEqual(result.pending, 0)
        self.assertEqual(len(gate_records), 1)
        self.assertIn("Timed out", gate_records[0].blocked_reason)
        self.assertFalse(any(call[:3] == ("gh", "pr", "edit") for call in runner.calls))

    def test_preview_gate_blocks_failed_checks(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            _write_sellyouroutboard_preview_profile(store)
            store.write_every_code_work_request_record(
                _done_record(
                    repository="cbusillo/sellyouroutboard",
                    result_pr_url="https://github.com/cbusillo/sellyouroutboard/pull/86",
                )
            )
            runner = _Runner(
                pr_view_payload={
                    "state": "OPEN",
                    "headRefOid": "abcdef1234567890",
                    "labels": [],
                    "statusCheckRollup": [
                        {"name": "static_checks", "status": "COMPLETED", "conclusion": "FAILURE"},
                    ],
                }
            )

            result = request_ready_every_code_pr_preview_labels(
                record_store=store,
                runner=runner,
            )
            gate_records = store.list_every_code_preview_gate_records(status="blocked")

        self.assertEqual(result.blocked, 1)
        self.assertEqual(len(gate_records), 1)
        self.assertIn("static_checks", gate_records[0].blocked_reason)
        self.assertFalse(any(call[:3] == ("gh", "pr", "edit") for call in runner.calls))

    def test_preview_gate_resets_when_pull_request_head_changes(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            _write_sellyouroutboard_preview_profile(store)
            store.write_every_code_work_request_record(
                _done_record(
                    repository="cbusillo/sellyouroutboard",
                    result_pr_url="https://github.com/cbusillo/sellyouroutboard/pull/86",
                )
            )
            store.write_every_code_preview_gate_record(
                _preview_gate_record(head_sha="oldsha1234567890")
            )
            runner = _Runner(
                pr_view_payload={
                    "state": "OPEN",
                    "headRefOid": "newsha1234567890",
                    "labels": [],
                    "statusCheckRollup": [
                        {"name": "static_checks", "status": "IN_PROGRESS", "conclusion": ""},
                    ],
                }
            )

            result = request_ready_every_code_pr_preview_labels(
                record_store=store,
                runner=runner,
            )
            all_gates = store.list_every_code_preview_gate_records(pr_number=86)
            pending_gates = store.list_every_code_preview_gate_records(status="pending")
            cancelled_gates = store.list_every_code_preview_gate_records(status="cancelled")

        self.assertEqual(result.pending, 1)
        self.assertEqual(result.reset, 1)
        self.assertEqual(len(all_gates), 2)
        self.assertEqual(pending_gates[0].head_sha, "newsha1234567890")
        self.assertEqual(cancelled_gates[0].head_sha, "oldsha1234567890")

    def test_preview_gate_cancels_closed_pull_request(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            _write_sellyouroutboard_preview_profile(store)
            store.write_every_code_work_request_record(
                _done_record(
                    repository="cbusillo/sellyouroutboard",
                    result_pr_url="https://github.com/cbusillo/sellyouroutboard/pull/86",
                )
            )
            runner = _Runner(
                pr_view_payload={
                    "state": "CLOSED",
                    "headRefOid": "abcdef1234567890",
                    "labels": [],
                    "statusCheckRollup": [],
                }
            )

            result = request_ready_every_code_pr_preview_labels(
                record_store=store,
                runner=runner,
            )
            gate_records = store.list_every_code_preview_gate_records(status="cancelled")

        self.assertEqual(result.cancelled, 1)
        self.assertEqual(len(gate_records), 1)
        self.assertIn("no longer open", gate_records[0].blocked_reason)

    def test_preview_gate_cancels_when_source_issue_closed(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            _write_sellyouroutboard_preview_profile(store)
            store.write_every_code_work_request_record(
                _done_record(
                    repository="cbusillo/sellyouroutboard",
                    result_pr_url="https://github.com/cbusillo/sellyouroutboard/pull/86",
                ).model_copy(update={"result_summary": "Source issue closed by Mbanks89."})
            )
            store.write_every_code_preview_gate_record(_preview_gate_record())
            runner = _Runner()

            with patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_TOKEN": "bot-token",
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_ACTOR": "shiny-code-bot",
                },
            ):
                result = request_ready_every_code_pr_preview_labels(
                    record_store=store,
                    runner=runner,
                )
                gate_records = store.list_every_code_preview_gate_records(status="cancelled")

        self.assertEqual(result.cancelled, 1)
        self.assertEqual(len(gate_records), 1)
        self.assertIn("Source issue closed", gate_records[0].blocked_reason)
        self.assertEqual(
            runner.calls,
            [
                ("gh", "api", "user", "--jq", ".login"),
                (
                    "gh",
                    "issue",
                    "edit",
                    "123",
                    "--repo",
                    "cbusillo/sellyouroutboard",
                    "--remove-label",
                    "every-code",
                ),
                (
                    "gh",
                    "issue",
                    "edit",
                    "123",
                    "--repo",
                    "cbusillo/sellyouroutboard",
                    "--remove-label",
                    "preview-ready",
                ),
            ],
        )

    def test_preview_gate_cancels_when_source_issue_closed_even_without_label_token(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            _write_sellyouroutboard_preview_profile(store)
            store.write_every_code_work_request_record(
                _done_record(
                    repository="cbusillo/sellyouroutboard",
                    result_pr_url="https://github.com/cbusillo/sellyouroutboard/pull/86",
                ).model_copy(update={"result_summary": "Source issue closed by Mbanks89."})
            )
            store.write_every_code_preview_gate_record(_preview_gate_record())
            runner = _Runner()

            with patch.dict(os.environ, {"LAUNCHPLANE_EVERY_CODE_GITHUB_TOKEN": ""}):
                result = request_ready_every_code_pr_preview_labels(
                    record_store=store,
                    runner=runner,
                )
                gate_records = store.list_every_code_preview_gate_records(status="cancelled")

        self.assertEqual(result.cancelled, 1)
        self.assertEqual(len(gate_records), 1)
        self.assertIn("Source issue closed", gate_records[0].blocked_reason)
        self.assertIn("LAUNCHPLANE_EVERY_CODE_GITHUB_TOKEN", gate_records[0].blocked_reason)
        self.assertFalse(runner.calls)

    def test_preview_gate_skips_pull_request_with_preview_label(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            _write_sellyouroutboard_preview_profile(store)
            store.write_every_code_work_request_record(
                _done_record(
                    repository="cbusillo/sellyouroutboard",
                    result_pr_url="https://github.com/cbusillo/sellyouroutboard/pull/86",
                )
            )
            runner = _Runner(
                pr_view_payload={
                    "state": "OPEN",
                    "labels": [{"name": "preview"}],
                    "statusCheckRollup": [],
                }
            )

            result = request_ready_every_code_pr_preview_labels(
                record_store=store,
                runner=runner,
            )

        self.assertEqual(result.skipped, 1)
        self.assertFalse(any(call[:3] == ("gh", "pr", "edit") for call in runner.calls))

    def test_preview_gate_backfills_reviewer_for_existing_preview_label(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            _write_sellyouroutboard_preview_profile(store)
            store.write_every_code_work_request_record(
                _done_record(
                    repository="cbusillo/sellyouroutboard",
                    result_pr_url="https://github.com/cbusillo/sellyouroutboard/pull/86",
                )
            )
            runner = _Runner(
                pr_view_payload={
                    "state": "OPEN",
                    "headRefOid": "abcdef1234567890",
                    "labels": [{"name": "preview"}],
                    "statusCheckRollup": [],
                },
                gh_api_payloads={
                    "repos/cbusillo/sellyouroutboard/issues/123": {"user": {"login": "Mbanks89"}},
                    "repos/cbusillo/sellyouroutboard/pulls/86": {"user": {"login": "cbusillo"}},
                    "repos/cbusillo/sellyouroutboard/pulls/86/requested_reviewers": {"users": []},
                },
            )

            result = request_ready_every_code_pr_preview_labels(
                record_store=store,
                runner=runner,
            )

        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.reviewer_backfilled, 1)
        self.assertIn(
            (
                "gh",
                "api",
                "repos/cbusillo/sellyouroutboard/pulls/86/requested_reviewers",
                "--method",
                "POST",
                "--field",
                "reviewers[]=Mbanks89",
            ),
            runner.calls,
        )

    def test_preview_gate_skips_reviewer_backfill_when_already_requested(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            _write_sellyouroutboard_preview_profile(store)
            store.write_every_code_work_request_record(
                _done_record(
                    repository="cbusillo/sellyouroutboard",
                    result_pr_url="https://github.com/cbusillo/sellyouroutboard/pull/86",
                )
            )
            runner = _Runner(
                pr_view_payload={
                    "state": "OPEN",
                    "labels": [{"name": "preview"}],
                    "statusCheckRollup": [],
                },
                gh_api_payloads={
                    "repos/cbusillo/sellyouroutboard/issues/123": {"user": {"login": "Mbanks89"}},
                    "repos/cbusillo/sellyouroutboard/pulls/86": {"user": {"login": "cbusillo"}},
                    "repos/cbusillo/sellyouroutboard/pulls/86/requested_reviewers": {
                        "users": [{"login": "Mbanks89"}]
                    },
                },
            )

            result = request_ready_every_code_pr_preview_labels(
                record_store=store,
                runner=runner,
            )

        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.reviewer_backfilled, 0)
        self.assertFalse(
            any(
                call[:4]
                == (
                    "gh",
                    "api",
                    "repos/cbusillo/sellyouroutboard/pulls/86/requested_reviewers",
                    "--method",
                )
                for call in runner.calls
            )
        )

    def test_preview_gate_skips_reviewer_backfill_for_pr_author(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            _write_sellyouroutboard_preview_profile(store)
            store.write_every_code_work_request_record(
                _done_record(
                    repository="cbusillo/sellyouroutboard",
                    result_pr_url="https://github.com/cbusillo/sellyouroutboard/pull/86",
                )
            )
            runner = _Runner(
                pr_view_payload={
                    "state": "OPEN",
                    "labels": [{"name": "preview"}],
                    "statusCheckRollup": [],
                },
                gh_api_payloads={
                    "repos/cbusillo/sellyouroutboard/issues/123": {"user": {"login": "Mbanks89"}},
                    "repos/cbusillo/sellyouroutboard/pulls/86": {"user": {"login": "Mbanks89"}},
                },
            )

            result = request_ready_every_code_pr_preview_labels(
                record_store=store,
                runner=runner,
            )

        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.reviewer_backfilled, 0)
        self.assertFalse(
            any(
                call[:4]
                == (
                    "gh",
                    "api",
                    "repos/cbusillo/sellyouroutboard/pulls/86/requested_reviewers",
                    "--method",
                )
                for call in runner.calls
            )
        )

    def test_preview_gate_discovers_running_request_pull_request_by_branch(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            _write_sellyouroutboard_preview_profile(store)
            running_record = _queued_record().model_copy(
                update={
                    "state": "running",
                    "repository": "cbusillo/sellyouroutboard",
                    "claimed_at": "2026-05-05T22:01:00Z",
                    "claimed_by_host": "Chris-Studio",
                    "started_at": "2026-05-05T22:02:00Z",
                }
            )
            store.write_every_code_work_request_record(running_record)
            runner = _Runner(
                pr_list_payload=[{"url": "https://github.com/cbusillo/sellyouroutboard/pull/86"}],
                pr_view_payload={
                    "state": "OPEN",
                    "labels": [],
                    "statusCheckRollup": [
                        {"name": "static_checks", "status": "COMPLETED", "conclusion": "SUCCESS"},
                    ],
                },
            )

            result = request_ready_every_code_pr_preview_labels(
                record_store=store,
                runner=runner,
            )

        self.assertEqual(result.labeled, 1)
        self.assertIn(
            (
                "gh",
                "pr",
                "list",
                "--repo",
                "cbusillo/sellyouroutboard",
                "--state",
                "open",
                "--head",
                every_code_worktree_branch(running_record),
                "--json",
                "url",
                "--limit",
                "1",
            ),
            runner.calls,
        )

    def test_check_failure_route_records_app_failure_feedback(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            store.write_every_code_work_request_record(
                _done_record(
                    repository="cbusillo/sellyouroutboard",
                    result_pr_url="https://github.com/cbusillo/sellyouroutboard/pull/86",
                )
            )
            runner = _Runner(
                pr_view_payload={
                    "state": "OPEN",
                    "headRefOid": "abcdef1234567890",
                    "labels": [],
                    "statusCheckRollup": [
                        {
                            "name": "automated_tests",
                            "status": "COMPLETED",
                            "conclusion": "FAILURE",
                            "detailsUrl": "https://github.com/cbusillo/sellyouroutboard/actions/runs/1001/job/2002",
                        },
                    ],
                }
            )

            result = route_every_code_pr_check_failures(
                record_store=store,
                runner=runner,
            )
            feedback = store.list_every_code_pr_feedback_records(status="pending")

        self.assertEqual(result.routed, 1)
        self.assertEqual(len(feedback), 1)
        self.assertIn("automated_tests", feedback[0].body)
        self.assertIn("same branch", feedback[0].body)
        self.assertIn("check-failure", feedback[0].feedback_id)

    def test_check_failure_route_suppresses_duplicate_app_failure(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            store.write_every_code_work_request_record(
                _done_record(
                    repository="cbusillo/sellyouroutboard",
                    result_pr_url="https://github.com/cbusillo/sellyouroutboard/pull/86",
                )
            )
            runner = _Runner(
                pr_view_payload={
                    "state": "OPEN",
                    "headRefOid": "abcdef1234567890",
                    "labels": [],
                    "statusCheckRollup": [
                        {"name": "build", "status": "COMPLETED", "conclusion": "FAILURE"},
                    ],
                }
            )

            first = route_every_code_pr_check_failures(record_store=store, runner=runner)
            second = route_every_code_pr_check_failures(record_store=store, runner=runner)
            feedback = store.list_every_code_pr_feedback_records(status="pending")

        self.assertEqual(first.routed, 1)
        self.assertEqual(second.duplicate, 1)
        self.assertEqual(len(feedback), 1)

    def test_check_failure_route_retries_preview_infra_without_feedback(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            store.write_every_code_work_request_record(
                _done_record(
                    repository="cbusillo/sellyouroutboard",
                    result_pr_url="https://github.com/cbusillo/sellyouroutboard/pull/86",
                )
            )
            runner = _Runner(
                pr_view_payload={
                    "state": "OPEN",
                    "headRefOid": "abcdef1234567890",
                    "labels": [],
                    "statusCheckRollup": [
                        {
                            "name": "publish_preview_image",
                            "status": "COMPLETED",
                            "conclusion": "FAILURE",
                            "detailsUrl": "https://github.com/cbusillo/sellyouroutboard/actions/runs/1001/job/2002",
                        },
                    ],
                }
            )

            result = route_every_code_pr_check_failures(
                record_store=store,
                runner=runner,
            )
            pending_feedback = store.list_every_code_pr_feedback_records(status="pending")
            ignored_feedback = store.list_every_code_pr_feedback_records(status="ignored")

        self.assertEqual(result.retried_infra, 1)
        self.assertEqual(pending_feedback, ())
        self.assertEqual(len(ignored_feedback), 1)
        self.assertIn(
            (
                "gh",
                "run",
                "rerun",
                "1001",
                "--repo",
                "cbusillo/sellyouroutboard",
                "--failed",
            ),
            runner.calls,
        )

    def test_check_failure_route_marks_previous_pending_failure_ignored_after_recovery(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            store.write_every_code_work_request_record(
                _done_record(
                    repository="cbusillo/sellyouroutboard",
                    result_pr_url="https://github.com/cbusillo/sellyouroutboard/pull/86",
                )
            )
            failing_runner = _Runner(
                pr_view_payload={
                    "state": "OPEN",
                    "headRefOid": "abcdef1234567890",
                    "labels": [],
                    "statusCheckRollup": [
                        {"name": "build", "status": "COMPLETED", "conclusion": "FAILURE"},
                    ],
                }
            )
            route_every_code_pr_check_failures(record_store=store, runner=failing_runner)
            passing_runner = _Runner(
                pr_view_payload={
                    "state": "OPEN",
                    "headRefOid": "abcdef1234567890",
                    "labels": [],
                    "statusCheckRollup": [
                        {"name": "build", "status": "COMPLETED", "conclusion": "SUCCESS"},
                    ],
                }
            )

            result = route_every_code_pr_check_failures(
                record_store=store,
                runner=passing_runner,
            )
            pending_feedback = store.list_every_code_pr_feedback_records(status="pending")
            ignored_feedback = store.list_every_code_pr_feedback_records(status="ignored")

        self.assertEqual(result.recovered, 1)
        self.assertEqual(pending_feedback, ())
        self.assertEqual(len(ignored_feedback), 1)

    def test_prepare_checkout_creates_worker_owned_worktree(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            checkout_root = temporary_root / "Developer" / "code"
            checkout_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            state_dir = temporary_root / "state"
            runner = _Runner()

            prepared = prepare_every_code_checkout(
                _queued_record(),
                workspace_root=temporary_root / "Developer",
                state_dir=state_dir,
                runner=runner,
            )

        self.assertEqual(prepared.source_checkout_root, checkout_root.resolve())
        self.assertEqual(
            prepared.launch_root,
            every_code_worktree_root(_queued_record(), state_dir=state_dir),
        )
        self.assertEqual(prepared.worktree_branch, every_code_worktree_branch(_queued_record()))
        self.assertIn(
            ("git", "-C", str(checkout_root.resolve()), "fetch", "--quiet", "origin", "main"),
            runner.calls,
        )
        self.assertTrue(any(call[3:6] == ("worktree", "add", "-b") for call in runner.calls))

    def test_prepare_checkout_reuses_matching_worker_worktree(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            checkout_root = temporary_root / "Developer" / "code"
            checkout_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            state_dir = temporary_root / "state"
            worktree_root = every_code_worktree_root(_queued_record(), state_dir=state_dir)
            worktree_root.mkdir(parents=True)
            (worktree_root / ".git").write_text("gitdir: test\n", encoding="utf-8")
            runner = _Runner()

            prepared = prepare_every_code_checkout(
                _queued_record(),
                workspace_root=temporary_root / "Developer",
                state_dir=state_dir,
                runner=runner,
            )

        self.assertEqual(prepared.launch_root, worktree_root)
        self.assertFalse(any(call[3:6] == ("worktree", "add", "-b") for call in runner.calls))

    def test_prepare_checkout_reuses_existing_request_branch(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            checkout_root = temporary_root / "Developer" / "code"
            checkout_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            state_dir = temporary_root / "state"
            runner = _Runner(existing_branch=True)

            prepared = prepare_every_code_checkout(
                _queued_record(),
                workspace_root=temporary_root / "Developer",
                state_dir=state_dir,
                runner=runner,
            )

        self.assertEqual(
            prepared.launch_root,
            every_code_worktree_root(_queued_record(), state_dir=state_dir),
        )
        self.assertFalse(any(call[3] == "fetch" for call in runner.calls))
        self.assertTrue(any(call[3:5] == ("worktree", "add") for call in runner.calls))
        self.assertFalse(any(call[3:6] == ("worktree", "add", "-b") for call in runner.calls))

    def test_prepare_checkout_rejects_existing_non_git_worktree(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            checkout_root = temporary_root / "Developer" / "code"
            checkout_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            state_dir = temporary_root / "state"
            every_code_worktree_root(_queued_record(), state_dir=state_dir).mkdir(parents=True)

            with self.assertRaisesRegex(RuntimeError, "not a git checkout"):
                prepare_every_code_checkout(
                    _queued_record(),
                    workspace_root=temporary_root / "Developer",
                    state_dir=state_dir,
                    runner=_Runner(),
                )

    def test_run_once_claims_request_and_launches_tmux_session(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            checkout_root = temporary_root / "Developer" / "code"
            checkout_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            store = FilesystemRecordStore(state_dir=temporary_root / "state")
            store.write_every_code_work_request_record(_queued_record())
            runner = _Runner()

            result = run_every_code_worker_once(
                record_store=store,
                host="Chris-Studio",
                workspace_root=temporary_root / "Developer",
                state_dir=temporary_root / "state",
                runner=runner,
            )
            record = store.read_every_code_work_request_record("every-code-cbusillo-code-123-test")
            state_path = every_code_session_state_path(
                state_dir=temporary_root / "state",
                request_id="every-code-cbusillo-code-123-test",
            )
            self.assertTrue(state_path.exists())
            session_payload = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(session_payload["request_id"], "every-code-cbusillo-code-123-test")
        self.assertEqual(session_payload["lifecycle_id"], record.lifecycle_id)
        self.assertEqual(session_payload["fencing_token"], "1")
        self.assertEqual(session_payload["attempt"], "1")
        self.assertEqual(session_payload["host"], "Chris-Studio")
        self.assertEqual(session_payload["source_checkout_root"], str(checkout_root.resolve()))
        self.assertEqual(
            session_payload["launch_root"],
            str(every_code_worktree_root(_queued_record(), state_dir=temporary_root / "state")),
        )
        self.assertEqual(
            session_payload["worktree_branch"], every_code_worktree_branch(_queued_record())
        )
        self.assertEqual(result.status, "running")
        self.assertEqual(record.state, "running")
        self.assertEqual(record.claimed_by_host, "Chris-Studio")
        self.assertEqual(result.checkout_root, str(checkout_root.resolve()))
        self.assertEqual(
            result.worktree_root,
            str(every_code_worktree_root(_queued_record(), state_dir=temporary_root / "state")),
        )
        self.assertEqual(result.worktree_branch, every_code_worktree_branch(_queued_record()))
        launch_call = next(call for call in runner.calls if call[1] == "new-session")
        self.assertEqual(
            launch_call[6],
            str(every_code_worktree_root(_queued_record(), state_dir=temporary_root / "state")),
        )
        self.assertIn("every-code finish", launch_call[-1])
        self.assertIn("--fencing-token 1", launch_call[-1])
        self.assertTrue(any(call[:3] == ("gh", "issue", "comment") for call in runner.calls))

    def test_run_once_terminates_stale_session_before_relaunch(self) -> None:
        class StaleSessionRunner(_Runner):
            stale_session_seen = False

            def __call__(
                self, args: Sequence[str], env: Mapping[str, str] | None = None
            ) -> subprocess.CompletedProcess[str]:
                if args[0] == "tmux" and args[1] == "has-session":
                    self.calls.append(tuple(args))
                    if not self.stale_session_seen:
                        self.stale_session_seen = True
                        return subprocess.CompletedProcess(args, 0, "", "")
                    return subprocess.CompletedProcess(args, 1, "", "no session")
                if args[0] == "tmux" and args[1] == "display-message":
                    self.calls.append(tuple(args))
                    return subprocess.CompletedProcess(args, 0, "4242\n", "")
                return super().__call__(args, env)

        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            checkout_root = temporary_root / "Developer" / "code"
            checkout_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            store = FilesystemRecordStore(state_dir=temporary_root / "state")
            store.write_every_code_work_request_record(_queued_record())
            runner = StaleSessionRunner()

            with patch("control_plane.every_code_worker.os.killpg") as killpg:
                result = run_every_code_worker_once(
                    record_store=store,
                    host="Chris-Studio",
                    workspace_root=temporary_root / "Developer",
                    state_dir=temporary_root / "state",
                    runner=runner,
                )

        self.assertEqual(result.status, "running")
        killpg.assert_called_once_with(4242, signal.SIGTERM)
        kill_index = next(
            index for index, call in enumerate(runner.calls) if call[1] == "kill-session"
        )
        launch_index = next(
            index for index, call in enumerate(runner.calls) if call[1] == "new-session"
        )
        self.assertLess(kill_index, launch_index)

    def test_apply_feedback_sends_prompt_to_active_session(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            checkout_root = temporary_root / "Developer" / "code"
            checkout_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            store = FilesystemRecordStore(state_dir=temporary_root / "state")
            store.write_every_code_work_request_record(_queued_record())
            run_every_code_worker_once(
                record_store=store,
                host="Chris-Studio",
                workspace_root=temporary_root / "Developer",
                state_dir=temporary_root / "state",
                runner=_Runner(),
            )
            store.write_every_code_pr_feedback_record(_feedback_record())
            runner = _ExistingSessionRunner()

            result = apply_every_code_pr_feedback_for_host(
                record_store=store,
                host="Chris-Studio",
                state_dir=temporary_root / "state",
                runner=runner,
            )
            feedback = store.list_every_code_pr_feedback_records(limit=1)[0]

        self.assertEqual(result.status, "applied")
        self.assertEqual(feedback.status, "applied")
        send_calls = [call for call in runner.calls if call[1] == "send-keys"]
        self.assertEqual(len(send_calls), 2)
        self.assertIn("Please tighten the README wording", send_calls[0][4])
        self.assertEqual(send_calls[0][-1], send_calls[0][4])
        self.assertEqual(send_calls[1][-1], "C-m")
        reaction_calls = [
            call for call in runner.calls if call[:4] == ("gh", "api", "--method", "POST")
        ]
        self.assertEqual(
            [call[-1] for call in reaction_calls],
            ["content=eyes", "content=rocket"],
        )

    def test_apply_feedback_reacts_when_active_session_send_fails(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            checkout_root = temporary_root / "Developer" / "code"
            checkout_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            store = FilesystemRecordStore(state_dir=temporary_root / "state")
            store.write_every_code_work_request_record(_queued_record())
            run_every_code_worker_once(
                record_store=store,
                host="Chris-Studio",
                workspace_root=temporary_root / "Developer",
                state_dir=temporary_root / "state",
                runner=_Runner(),
            )
            store.write_every_code_pr_feedback_record(_feedback_record())
            runner = _FailingFeedbackSendRunner()

            result = apply_every_code_pr_feedback_for_host(
                record_store=store,
                host="Chris-Studio",
                state_dir=temporary_root / "state",
                runner=runner,
            )
            feedback = store.list_every_code_pr_feedback_records(limit=1)[0]

        self.assertEqual(result.status, "blocked")
        self.assertEqual(feedback.status, "pending")
        reaction_calls = [
            call for call in runner.calls if call[:4] == ("gh", "api", "--method", "POST")
        ]
        self.assertEqual(
            [call[-1] for call in reaction_calls],
            ["content=eyes", "content=confused"],
        )

    def test_apply_feedback_relaunches_terminal_session_in_saved_worktree(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            checkout_root = temporary_root / "Developer" / "code"
            checkout_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            store = FilesystemRecordStore(state_dir=temporary_root / "state")
            store.write_every_code_work_request_record(_queued_record())
            run_every_code_worker_once(
                record_store=store,
                host="Chris-Studio",
                workspace_root=temporary_root / "Developer",
                state_dir=temporary_root / "state",
                runner=_Runner(),
            )
            finished_record = store.read_every_code_work_request_record(
                "every-code-cbusillo-code-123-test"
            ).model_copy(
                update={
                    "state": "done",
                    "finished_at": "2026-05-06T19:05:00Z",
                    "updated_at": "2026-05-06T19:05:00Z",
                    "result_pr_url": "https://github.com/cbusillo/code/pull/26",
                    "result_summary": "Opened PR.",
                }
            )
            store.write_every_code_work_request_record(finished_record)
            store.write_every_code_pr_feedback_record(_feedback_record())
            runner = _GoneSessionRunner()

            result = apply_every_code_pr_feedback_for_host(
                record_store=store,
                host="Chris-Studio",
                state_dir=temporary_root / "state",
                runner=runner,
            )
            feedback = store.list_every_code_pr_feedback_records(limit=1)[0]
            resumed_record = store.read_every_code_work_request_record(
                "every-code-cbusillo-code-123-test"
            )

        self.assertEqual(result.status, "applied")
        self.assertEqual(feedback.status, "applied")
        self.assertEqual(resumed_record.state, "running")
        self.assertEqual(resumed_record.finished_at, "")
        launch_call = next(call for call in runner.calls if call[1] == "new-session")
        self.assertEqual(
            launch_call[6],
            str(every_code_worktree_root(_queued_record(), state_dir=temporary_root / "state")),
        )
        self.assertIn("Every Code received new PR feedback", launch_call[-1])

    def test_apply_feedback_persists_redacted_relaunch_failure(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            checkout_root = temporary_root / "Developer" / "code"
            checkout_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            store = FilesystemRecordStore(state_dir=temporary_root / "state")
            store.write_every_code_work_request_record(_queued_record())
            run_every_code_worker_once(
                record_store=store,
                host="Chris-Studio",
                workspace_root=temporary_root / "Developer",
                state_dir=temporary_root / "state",
                runner=_Runner(),
            )
            finished_record = store.read_every_code_work_request_record(
                "every-code-cbusillo-code-123-test"
            ).model_copy(
                update={
                    "state": "done",
                    "finished_at": "2026-05-06T19:05:00Z",
                    "updated_at": "2026-05-06T19:05:00Z",
                    "result_pr_url": "https://github.com/cbusillo/code/pull/26",
                    "result_summary": "Opened PR.",
                }
            )
            store.write_every_code_work_request_record(finished_record)
            store.write_every_code_pr_feedback_record(_feedback_record())

            result = apply_every_code_pr_feedback_for_host(
                record_store=store,
                host="Chris-Studio",
                state_dir=temporary_root / "state",
                runner=_FailingFeedbackRelaunchRunner(),
            )
            feedback = store.list_every_code_pr_feedback_records(limit=1)[0]
            blocked_record = store.read_every_code_work_request_record(
                "every-code-cbusillo-code-123-test"
            )

        payload = json.dumps({"result": result.as_payload(), "record": blocked_record.model_dump()})
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.error_code, "child_process_failed")
        self.assertTrue(result.error_correlation_id.startswith("cpf-"))
        self.assertEqual(feedback.status, "pending")
        self.assertEqual(blocked_record.state, "blocked")
        self.assertIn(f"error_code={result.error_code}", blocked_record.error_message)
        self.assertIn(f"correlation_id={result.error_correlation_id}", blocked_record.error_message)
        for value in (
            "dXNlcjpzaG91bGQtbm90LXN1cnZpdmU=",
            "short-cookie-secret",
            "worker.internal",
        ):
            self.assertNotIn(value, payload)

    def test_apply_feedback_ignores_request_closed_by_linked_pr(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            checkout_root = temporary_root / "Developer" / "code"
            checkout_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            store = FilesystemRecordStore(state_dir=temporary_root / "state")
            store.write_every_code_work_request_record(_queued_record())
            run_every_code_worker_once(
                record_store=store,
                host="Chris-Studio",
                workspace_root=temporary_root / "Developer",
                state_dir=temporary_root / "state",
                runner=_Runner(),
            )
            closed_record = store.read_every_code_work_request_record(
                "every-code-cbusillo-code-123-test"
            ).model_copy(
                update={
                    "state": "done",
                    "finished_at": "2026-05-06T19:05:00Z",
                    "updated_at": "2026-05-06T19:05:00Z",
                    "result_pr_url": "https://github.com/cbusillo/code/pull/26",
                    "result_summary": "Linked pull request merged: https://github.com/cbusillo/code/pull/26",
                }
            )
            store.write_every_code_work_request_record(closed_record)
            store.write_every_code_pr_feedback_record(_feedback_record())
            runner = _GoneSessionRunner()

            result = apply_every_code_pr_feedback_for_host(
                record_store=store,
                host="Chris-Studio",
                state_dir=temporary_root / "state",
                runner=runner,
            )
            feedback = store.list_every_code_pr_feedback_records(limit=1)[0]

        self.assertEqual(result.status, "ignored")
        self.assertEqual(feedback.status, "ignored")
        self.assertFalse(any(call[1] == "new-session" for call in runner.calls))

    def test_terminal_host_request_sends_sigterm_to_session_process_group(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            checkout_root = temporary_root / "Developer" / "code"
            checkout_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            store = FilesystemRecordStore(state_dir=temporary_root / "state")
            store.write_every_code_work_request_record(_queued_record())
            run_every_code_worker_once(
                record_store=store,
                host="Chris-Studio",
                workspace_root=temporary_root / "Developer",
                state_dir=temporary_root / "state",
                runner=_Runner(),
            )
            record = store.read_every_code_work_request_record(
                "every-code-cbusillo-code-123-test"
            ).model_copy(
                update={
                    "state": "done",
                    "finished_at": "2026-05-06T00:02:00Z",
                    "updated_at": "2026-05-06T00:02:00Z",
                    "result_summary": "Linked pull request merged.",
                }
            )
            store.write_every_code_work_request_record(record)
            runner = _ExistingSessionRunner(existing_branch=True)

            with patch("control_plane.every_code_worker.os.killpg") as killpg:
                closed = close_terminal_every_code_sessions(
                    record_store=store,
                    host="Chris-Studio",
                    state_dir=temporary_root / "state",
                    runner=runner,
                )

        self.assertEqual(closed, 1)
        killpg.assert_called_once_with(4242, signal.SIGTERM)
        self.assertEqual(runner.calls[0][1], "has-session")
        self.assertEqual(runner.calls[1][0], "lsof")
        self.assertEqual(runner.calls[2][1], "display-message")
        self.assertEqual(runner.calls[3][1], "kill-session")

    def test_terminal_request_removes_clean_worktree_after_session_close(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            checkout_root = temporary_root / "Developer" / "code"
            checkout_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            store = FilesystemRecordStore(state_dir=temporary_root / "state")
            store.write_every_code_work_request_record(_queued_record())
            run_every_code_worker_once(
                record_store=store,
                host="Chris-Studio",
                workspace_root=temporary_root / "Developer",
                state_dir=temporary_root / "state",
                runner=_Runner(),
            )
            record = store.read_every_code_work_request_record(
                "every-code-cbusillo-code-123-test"
            ).model_copy(
                update={
                    "state": "done",
                    "finished_at": "2026-05-06T00:02:00Z",
                    "updated_at": "2026-05-06T00:02:00Z",
                    "result_summary": "Linked pull request merged.",
                }
            )
            store.write_every_code_work_request_record(record)
            state_path = every_code_session_state_path(
                state_dir=temporary_root / "state",
                request_id="every-code-cbusillo-code-123-test",
            )
            runner = _ExistingSessionRunner()

            with patch("control_plane.every_code_worker.os.killpg"):
                close_terminal_every_code_sessions(
                    record_store=store,
                    host="Chris-Studio",
                    state_dir=temporary_root / "state",
                    runner=runner,
                )

        self.assertFalse(state_path.exists())
        self.assertTrue(any(call[3:5] == ("worktree", "remove") for call in runner.calls))
        self.assertTrue(any(call[3:5] == ("branch", "-d") for call in runner.calls))

    def test_terminal_request_preserves_dirty_worktree(self) -> None:
        class _DirtyWorktreeRunner(_ExistingSessionRunner):
            def __call__(
                self, args: Sequence[str], env: Mapping[str, str] | None = None
            ) -> subprocess.CompletedProcess[str]:
                if args[0] == "git" and args[3:5] == ("status", "--porcelain"):
                    self.calls.append(tuple(args))
                    return subprocess.CompletedProcess(args, 0, " M README.md\n", "")
                return super().__call__(args, env)

        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            checkout_root = temporary_root / "Developer" / "code"
            checkout_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            store = FilesystemRecordStore(state_dir=temporary_root / "state")
            store.write_every_code_work_request_record(_queued_record())
            run_every_code_worker_once(
                record_store=store,
                host="Chris-Studio",
                workspace_root=temporary_root / "Developer",
                state_dir=temporary_root / "state",
                runner=_Runner(),
            )
            record = store.read_every_code_work_request_record(
                "every-code-cbusillo-code-123-test"
            ).model_copy(
                update={
                    "state": "done",
                    "finished_at": "2026-05-06T00:02:00Z",
                    "updated_at": "2026-05-06T00:02:00Z",
                    "result_summary": "Linked pull request merged.",
                }
            )
            store.write_every_code_work_request_record(record)
            runner = _DirtyWorktreeRunner()

            with patch("control_plane.every_code_worker.os.killpg"):
                close_terminal_every_code_sessions(
                    record_store=store,
                    host="Chris-Studio",
                    state_dir=temporary_root / "state",
                    runner=runner,
                )

        self.assertFalse(any(call[3:5] == ("worktree", "remove") for call in runner.calls))
        self.assertFalse(any(call[3:5] == ("branch", "-d") for call in runner.calls))

    def test_cleanup_reconciliation_dry_run_reports_safe_terminal_state(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            state_dir = temporary_root / "state"
            checkout_root = temporary_root / "Developer" / "code"
            worktree_root = every_code_worktree_root(_queued_record(), state_dir=state_dir)
            checkout_root.mkdir(parents=True)
            worktree_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            (worktree_root / ".git").mkdir()
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_every_code_work_request_record(_terminal_record())
            state_path = _write_cleanup_session_state(
                state_dir=state_dir,
                source_checkout_root=checkout_root,
                launch_root=worktree_root,
            )
            runner = _CleanupReconciliationRunner(worktree_root=worktree_root)

            result = reconcile_every_code_worker_cleanup_state(
                record_store=store,
                host="Chris-Studio",
                state_dir=state_dir,
                runner=runner,
            )
            state_exists = state_path.exists()

        self.assertEqual(result.mode, "dry_run")
        self.assertEqual(result.checked_sessions, 1)
        self.assertEqual(result.would_remove, 1)
        self.assertEqual(result.items[0].status, "would_remove")
        self.assertEqual(result.items[0].reason, "safe_terminal_worker_state")
        self.assertTrue(state_exists)
        self.assertFalse(any(call[3:5] == ("worktree", "remove") for call in runner.calls))
        self.assertTrue(
            any(
                item.kind == "worker_worktree_directory" and item.status == "linked"
                for item in result.inventory
            )
        )
        self.assertTrue(
            any(
                item.kind == "local_branch"
                and item.value == every_code_worktree_branch(_queued_record())
                and item.status == "linked"
                for item in result.inventory
            )
        )

    def test_cleanup_reconciliation_apply_removes_safe_terminal_state(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            state_dir = temporary_root / "state"
            checkout_root = temporary_root / "Developer" / "code"
            worktree_root = every_code_worktree_root(_queued_record(), state_dir=state_dir)
            checkout_root.mkdir(parents=True)
            worktree_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            (worktree_root / ".git").mkdir()
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_every_code_work_request_record(_terminal_record())
            state_path = _write_cleanup_session_state(
                state_dir=state_dir,
                source_checkout_root=checkout_root,
                launch_root=worktree_root,
            )
            runner = _CleanupReconciliationRunner(worktree_root=worktree_root)

            result = reconcile_every_code_worker_cleanup_state(
                record_store=store,
                host="Chris-Studio",
                state_dir=state_dir,
                apply=True,
                runner=runner,
            )
            state_exists = state_path.exists()

        self.assertEqual(result.mode, "apply")
        self.assertEqual(result.removed, 1)
        self.assertEqual(result.items[0].status, "removed")
        self.assertFalse(state_exists)
        self.assertTrue(any(call[3:5] == ("worktree", "remove") for call in runner.calls))
        self.assertTrue(any(call[3:5] == ("branch", "-d") for call in runner.calls))

    def test_cleanup_reconciliation_skips_missing_worktree_path_but_deletes_branch(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            state_dir = temporary_root / "state"
            checkout_root = temporary_root / "Developer" / "code"
            worktree_root = every_code_worktree_root(_queued_record(), state_dir=state_dir)
            checkout_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_every_code_work_request_record(_terminal_record())
            state_path = _write_cleanup_session_state(
                state_dir=state_dir,
                source_checkout_root=checkout_root,
                launch_root=worktree_root,
            )
            runner = _CleanupReconciliationRunner(worktree_root=worktree_root, registered=False)

            result = reconcile_every_code_worker_cleanup_state(
                record_store=store,
                host="Chris-Studio",
                state_dir=state_dir,
                apply=True,
                runner=runner,
            )
            state_exists = state_path.exists()

        self.assertEqual(result.removed, 1)
        self.assertFalse(state_exists)
        self.assertFalse(any(call[3:5] == ("worktree", "remove") for call in runner.calls))
        self.assertTrue(any(call[3:5] == ("branch", "-d") for call in runner.calls))

    def test_cleanup_reconciliation_skips_missing_request_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            state_dir = temporary_root / "state"
            checkout_root = temporary_root / "Developer" / "code"
            worktree_root = every_code_worktree_root(_queued_record(), state_dir=state_dir)
            checkout_root.mkdir(parents=True)
            worktree_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            (worktree_root / ".git").mkdir()
            state_path = _write_cleanup_session_state(
                state_dir=state_dir,
                source_checkout_root=checkout_root,
                launch_root=worktree_root,
            )
            store = _CleanupStoreMissingRequest(state_dir=state_dir)

            result = reconcile_every_code_worker_cleanup_state(
                record_store=store,
                host="Chris-Studio",
                state_dir=state_dir,
                apply=True,
                runner=_CleanupReconciliationRunner(worktree_root=worktree_root),
            )
            state_exists = state_path.exists()

        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.items[0].reason, "request_record_missing")
        self.assertTrue(state_exists)

    def test_cleanup_reconciliation_skips_non_terminal_request_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            state_dir = temporary_root / "state"
            checkout_root = temporary_root / "Developer" / "code"
            worktree_root = every_code_worktree_root(_queued_record(), state_dir=state_dir)
            checkout_root.mkdir(parents=True)
            worktree_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            (worktree_root / ".git").mkdir()
            state_path = _write_cleanup_session_state(
                state_dir=state_dir,
                source_checkout_root=checkout_root,
                launch_root=worktree_root,
            )
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_every_code_work_request_record(_queued_record())

            result = reconcile_every_code_worker_cleanup_state(
                record_store=store,
                host="Chris-Studio",
                state_dir=state_dir,
                apply=True,
                runner=_CleanupReconciliationRunner(worktree_root=worktree_root),
            )
            state_exists = state_path.exists()

        self.assertEqual(result.items[0].reason, "request_record_not_terminal")
        self.assertTrue(state_exists)

    def test_cleanup_reconciliation_skips_dirty_worktree(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            state_dir = temporary_root / "state"
            checkout_root = temporary_root / "Developer" / "code"
            worktree_root = every_code_worktree_root(_queued_record(), state_dir=state_dir)
            checkout_root.mkdir(parents=True)
            worktree_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            (worktree_root / ".git").mkdir()
            state_path = _write_cleanup_session_state(
                state_dir=state_dir,
                source_checkout_root=checkout_root,
                launch_root=worktree_root,
            )
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_every_code_work_request_record(_terminal_record())
            runner = _CleanupReconciliationRunner(worktree_root=worktree_root, dirty=True)

            result = reconcile_every_code_worker_cleanup_state(
                record_store=store,
                host="Chris-Studio",
                state_dir=state_dir,
                apply=True,
                runner=runner,
            )
            state_exists = state_path.exists()

        self.assertEqual(result.items[0].reason, "worktree_dirty_or_status_failed")
        self.assertTrue(state_exists)
        self.assertFalse(any(call[3:5] == ("worktree", "remove") for call in runner.calls))

    def test_cleanup_reconciliation_skips_active_session(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            state_dir = temporary_root / "state"
            checkout_root = temporary_root / "Developer" / "code"
            worktree_root = every_code_worktree_root(_queued_record(), state_dir=state_dir)
            checkout_root.mkdir(parents=True)
            worktree_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            (worktree_root / ".git").mkdir()
            state_path = _write_cleanup_session_state(
                state_dir=state_dir,
                source_checkout_root=checkout_root,
                launch_root=worktree_root,
            )
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_every_code_work_request_record(_terminal_record())

            result = reconcile_every_code_worker_cleanup_state(
                record_store=store,
                host="Chris-Studio",
                state_dir=state_dir,
                apply=True,
                runner=_CleanupReconciliationRunner(worktree_root=worktree_root, tmux_active=True),
            )
            state_exists = state_path.exists()

        self.assertEqual(result.items[0].reason, "tmux_session_active")
        self.assertTrue(state_exists)

    def test_cleanup_reconciliation_skips_unknown_source_checkout(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            state_dir = temporary_root / "state"
            checkout_root = temporary_root / "Developer" / "missing-code"
            worktree_root = every_code_worktree_root(_queued_record(), state_dir=state_dir)
            worktree_root.mkdir(parents=True)
            (worktree_root / ".git").mkdir()
            state_path = _write_cleanup_session_state(
                state_dir=state_dir,
                source_checkout_root=checkout_root,
                launch_root=worktree_root,
            )
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_every_code_work_request_record(_terminal_record())

            result = reconcile_every_code_worker_cleanup_state(
                record_store=store,
                host="Chris-Studio",
                state_dir=state_dir,
                apply=True,
                runner=_CleanupReconciliationRunner(worktree_root=worktree_root),
            )
            state_exists = state_path.exists()

        self.assertEqual(result.items[0].reason, "source_checkout_missing")
        self.assertTrue(state_exists)

    def test_cleanup_reconciliation_skips_worktree_outside_state_root(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            state_dir = temporary_root / "state"
            checkout_root = temporary_root / "Developer" / "code"
            worktree_root = temporary_root / "other" / "worktree"
            checkout_root.mkdir(parents=True)
            worktree_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            (worktree_root / ".git").mkdir()
            state_path = _write_cleanup_session_state(
                state_dir=state_dir,
                source_checkout_root=checkout_root,
                launch_root=worktree_root,
            )
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_every_code_work_request_record(_terminal_record())

            result = reconcile_every_code_worker_cleanup_state(
                record_store=store,
                host="Chris-Studio",
                state_dir=state_dir,
                apply=True,
                runner=_CleanupReconciliationRunner(worktree_root=worktree_root),
            )
            state_exists = state_path.exists()

        self.assertEqual(result.items[0].reason, "worktree_path_outside_worker_state_root")
        self.assertTrue(state_exists)

    def test_cleanup_reconciliation_skips_non_worker_branch(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            state_dir = temporary_root / "state"
            checkout_root = temporary_root / "Developer" / "code"
            worktree_root = every_code_worktree_root(_queued_record(), state_dir=state_dir)
            checkout_root.mkdir(parents=True)
            worktree_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            (worktree_root / ".git").mkdir()
            state_path = _write_cleanup_session_state(
                state_dir=state_dir,
                source_checkout_root=checkout_root,
                launch_root=worktree_root,
                worktree_branch="feature/not-worker-owned",
            )
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_every_code_work_request_record(_terminal_record())

            result = reconcile_every_code_worker_cleanup_state(
                record_store=store,
                host="Chris-Studio",
                state_dir=state_dir,
                apply=True,
                runner=_CleanupReconciliationRunner(worktree_root=worktree_root),
            )
            state_exists = state_path.exists()

        self.assertEqual(result.items[0].reason, "branch_not_worker_owned")
        self.assertTrue(state_exists)

    def test_cleanup_reconciliation_preserves_state_when_branch_delete_fails(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            state_dir = temporary_root / "state"
            checkout_root = temporary_root / "Developer" / "code"
            worktree_root = every_code_worktree_root(_queued_record(), state_dir=state_dir)
            checkout_root.mkdir(parents=True)
            worktree_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            (worktree_root / ".git").mkdir()
            state_path = _write_cleanup_session_state(
                state_dir=state_dir,
                source_checkout_root=checkout_root,
                launch_root=worktree_root,
            )
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_every_code_work_request_record(_terminal_record())
            runner = _CleanupReconciliationRunner(
                worktree_root=worktree_root,
                branch_delete_fails=True,
            )

            result = reconcile_every_code_worker_cleanup_state(
                record_store=store,
                host="Chris-Studio",
                state_dir=state_dir,
                apply=True,
                runner=runner,
            )
            state_exists = state_path.exists()

        self.assertEqual(result.items[0].reason, "cleanup_failed")
        self.assertTrue(state_exists)
        self.assertTrue(any(call[3:5] == ("branch", "-d") for call in runner.calls))

    def test_terminal_request_preserves_session_state_when_worktree_cleanup_fails(
        self,
    ) -> None:
        class _BranchDeleteFailsRunner(_ExistingSessionRunner):
            def __init__(self) -> None:
                super().__init__(existing_branch=True)

            def __call__(
                self, args: Sequence[str], env: Mapping[str, str] | None = None
            ) -> subprocess.CompletedProcess[str]:
                self.calls.append(tuple(args))
                if args[0] == "git" and args[3:5] == ("branch", "-d"):
                    return subprocess.CompletedProcess(args, 1, "", "not merged")
                if args[0] == "git":
                    self.calls.pop()
                    return _Runner.__call__(self, args, env)
                self.calls.pop()
                return super().__call__(args, env)

        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            checkout_root = temporary_root / "Developer" / "code"
            checkout_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            store = FilesystemRecordStore(state_dir=temporary_root / "state")
            store.write_every_code_work_request_record(_queued_record())
            run_every_code_worker_once(
                record_store=store,
                host="Chris-Studio",
                workspace_root=temporary_root / "Developer",
                state_dir=temporary_root / "state",
                runner=_Runner(),
            )
            record = store.read_every_code_work_request_record(
                "every-code-cbusillo-code-123-test"
            ).model_copy(
                update={
                    "state": "done",
                    "finished_at": "2026-05-06T00:02:00Z",
                    "updated_at": "2026-05-06T00:02:00Z",
                    "result_summary": "Linked pull request merged.",
                }
            )
            store.write_every_code_work_request_record(record)
            state_path = every_code_session_state_path(
                state_dir=temporary_root / "state",
                request_id="every-code-cbusillo-code-123-test",
            )
            runner = _BranchDeleteFailsRunner()

            with patch("control_plane.every_code_worker.os.killpg"):
                close_terminal_every_code_sessions(
                    record_store=store,
                    host="Chris-Studio",
                    state_dir=temporary_root / "state",
                    runner=runner,
                )

            self.assertTrue(state_path.exists())
        self.assertTrue(any(call[3:5] == ("worktree", "remove") for call in runner.calls))
        self.assertTrue(any(call[3:5] == ("branch", "-d") for call in runner.calls))

    def test_terminal_request_deletes_branch_when_worktree_path_is_gone(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            checkout_root = temporary_root / "Developer" / "code"
            checkout_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            store = FilesystemRecordStore(state_dir=temporary_root / "state")
            store.write_every_code_work_request_record(_queued_record())
            run_every_code_worker_once(
                record_store=store,
                host="Chris-Studio",
                workspace_root=temporary_root / "Developer",
                state_dir=temporary_root / "state",
                runner=_Runner(),
            )
            record = store.read_every_code_work_request_record(
                "every-code-cbusillo-code-123-test"
            ).model_copy(
                update={
                    "state": "done",
                    "finished_at": "2026-05-06T00:02:00Z",
                    "updated_at": "2026-05-06T00:02:00Z",
                    "result_summary": "Linked pull request merged.",
                }
            )
            store.write_every_code_work_request_record(record)
            worktree_root = every_code_worktree_root(
                _queued_record(), state_dir=temporary_root / "state"
            )
            shutil.rmtree(worktree_root)
            state_path = every_code_session_state_path(
                state_dir=temporary_root / "state",
                request_id="every-code-cbusillo-code-123-test",
            )
            runner = _Runner(existing_branch=True)

            with patch("control_plane.every_code_worker.os.killpg"):
                close_terminal_every_code_sessions(
                    record_store=store,
                    host="Chris-Studio",
                    state_dir=temporary_root / "state",
                    runner=runner,
                )

        self.assertFalse(state_path.exists())
        self.assertFalse(any(call[3:5] == ("worktree", "remove") for call in runner.calls))
        self.assertTrue(any(call[3:5] == ("branch", "-d") for call in runner.calls))

    def test_terminal_request_kills_worktree_processes_when_tmux_is_gone(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            checkout_root = temporary_root / "Developer" / "code"
            checkout_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            store = FilesystemRecordStore(state_dir=temporary_root / "state")
            store.write_every_code_work_request_record(_queued_record())
            run_every_code_worker_once(
                record_store=store,
                host="Chris-Studio",
                workspace_root=temporary_root / "Developer",
                state_dir=temporary_root / "state",
                runner=_Runner(),
            )
            record = store.read_every_code_work_request_record(
                "every-code-cbusillo-code-123-test"
            ).model_copy(
                update={
                    "state": "done",
                    "finished_at": "2026-05-06T00:02:00Z",
                    "updated_at": "2026-05-06T00:02:00Z",
                    "result_summary": "Linked pull request merged.",
                }
            )
            store.write_every_code_work_request_record(record)
            state_path = every_code_session_state_path(
                state_dir=temporary_root / "state",
                request_id="every-code-cbusillo-code-123-test",
            )
            runner = _GoneSessionWithWorktreeProcessRunner()

            with patch(
                "control_plane.every_code_worker.os.getpgid",
                side_effect=lambda pid: 7000 if pid in {9001, 9002} else pid,
            ):
                with patch("control_plane.every_code_worker.os.killpg") as killpg:
                    closed = close_terminal_every_code_sessions(
                        record_store=store,
                        host="Chris-Studio",
                        state_dir=temporary_root / "state",
                        runner=runner,
                    )

        self.assertEqual(closed, 1)
        killpg.assert_called_once_with(7000, signal.SIGTERM)
        self.assertFalse(state_path.exists())
        lsof_call = next(call for call in runner.calls if call[0] == "lsof")
        self.assertEqual(lsof_call[1:3], ("-t", "+D"))

    def test_terminal_request_unlinks_state_when_pane_pid_is_missing(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            checkout_root = temporary_root / "Developer" / "code"
            checkout_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            store = FilesystemRecordStore(state_dir=temporary_root / "state")
            store.write_every_code_work_request_record(_queued_record())
            run_every_code_worker_once(
                record_store=store,
                host="Chris-Studio",
                workspace_root=temporary_root / "Developer",
                state_dir=temporary_root / "state",
                runner=_Runner(),
            )
            record = store.read_every_code_work_request_record(
                "every-code-cbusillo-code-123-test"
            ).model_copy(
                update={
                    "state": "blocked",
                    "finished_at": "2026-05-06T00:02:00Z",
                    "updated_at": "2026-05-06T00:02:00Z",
                    "error_message": "Session ended without a PR.",
                }
            )
            store.write_every_code_work_request_record(record)
            state_path = every_code_session_state_path(
                state_dir=temporary_root / "state",
                request_id="every-code-cbusillo-code-123-test",
            )

            class _MissingPanePidRunner(_ExistingSessionRunner):
                def __call__(
                    self, args: Sequence[str], env: Mapping[str, str] | None = None
                ) -> subprocess.CompletedProcess[str]:
                    del env
                    self.calls.append(tuple(args))
                    if args[0] == "lsof":
                        return subprocess.CompletedProcess(args, 1, "", "")
                    if args[1] == "has-session":
                        return subprocess.CompletedProcess(args, 0, "", "")
                    if args[1] == "display-message":
                        return subprocess.CompletedProcess(args, 1, "", "no pane")
                    if args[1] == "kill-session":
                        return subprocess.CompletedProcess(args, 0, "", "")
                    return subprocess.CompletedProcess(args, 0, "", "")

            runner = _MissingPanePidRunner()

            closed = close_terminal_every_code_sessions(
                record_store=store,
                host="Chris-Studio",
                state_dir=temporary_root / "state",
                runner=runner,
            )

        self.assertEqual(closed, 1)
        self.assertFalse(state_path.exists())
        self.assertTrue(any(call[1] == "kill-session" for call in runner.calls))

    def test_terminal_request_claimed_by_other_host_is_ignored(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            checkout_root = temporary_root / "Developer" / "code"
            checkout_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            store = FilesystemRecordStore(state_dir=temporary_root / "state")
            store.write_every_code_work_request_record(_queued_record())
            run_every_code_worker_once(
                record_store=store,
                host="Chris-Studio",
                workspace_root=temporary_root / "Developer",
                state_dir=temporary_root / "state",
                runner=_Runner(),
            )
            record = store.read_every_code_work_request_record(
                "every-code-cbusillo-code-123-test"
            ).model_copy(
                update={
                    "state": "done",
                    "claimed_by_host": "Other-Mac",
                    "finished_at": "2026-05-06T00:02:00Z",
                    "updated_at": "2026-05-06T00:02:00Z",
                    "result_summary": "Linked pull request merged.",
                }
            )
            store.write_every_code_work_request_record(record)
            runner = _ExistingSessionRunner()

            with patch("control_plane.every_code_worker.os.killpg") as killpg:
                closed = close_terminal_every_code_sessions(
                    record_store=store,
                    host="Chris-Studio",
                    state_dir=temporary_root / "state",
                    runner=runner,
                )

        self.assertEqual(closed, 0)
        killpg.assert_not_called()

    def test_run_once_blocks_before_launch_when_claim_comment_fails(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            checkout_root = temporary_root / "Developer" / "code"
            checkout_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            store = FilesystemRecordStore(state_dir=temporary_root / "state")
            store.write_every_code_work_request_record(_queued_record())
            runner = _Runner(fail_issue_comment=True)

            result = run_every_code_worker_once(
                record_store=store,
                host="Chris-Studio",
                workspace_root=temporary_root / "Developer",
                state_dir=temporary_root / "state",
                runner=runner,
            )
            record = store.read_every_code_work_request_record("every-code-cbusillo-code-123-test")

        self.assertEqual(result.status, "blocked")
        self.assertEqual(record.state, "blocked")
        self.assertIn("Could not post GitHub working comment", record.error_message)
        self.assertFalse(any(call[1] == "new-session" for call in runner.calls))
        self.assertTrue(any(call[:3] == ("gh", "issue", "comment") for call in runner.calls))

    def test_run_once_redacts_claim_comment_failure_before_persistence(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            checkout_root = temporary_root / "Developer" / "code"
            checkout_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            store = FilesystemRecordStore(state_dir=temporary_root / "state")
            store.write_every_code_work_request_record(_queued_record())
            runner = _Runner(
                fail_issue_comment=True,
                issue_comment_error_detail=(
                    "Bearer example-bearer-value\n"
                    "Authorization: Basic dXNlcjpzaG91bGQtbm90LXN1cnZpdmU=\n"
                    "Cookie: launchplane_session=short-cookie-secret\n"
                    "GH_TOKEN=example-token-value\n"
                    "https://operator:password@worker.internal/private/log\n"
                    "/Users/operator/.config/launchplane/token"
                ),
            )

            result = run_every_code_worker_once(
                record_store=store,
                host="Chris-Studio",
                workspace_root=temporary_root / "Developer",
                state_dir=temporary_root / "state",
                runner=runner,
            )
            record = store.read_every_code_work_request_record("every-code-cbusillo-code-123-test")

        payload = json.dumps({"result": result.as_payload(), "record": record.model_dump()})
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.error_code, "github_cli_failed")
        self.assertTrue(result.error_correlation_id.startswith("cpf-"))
        self.assertIn(f"error_code={result.error_code}", record.error_message)
        self.assertIn(f"correlation_id={result.error_correlation_id}", record.error_message)
        self.assertNotIn("error_code", record.model_dump())
        self.assertNotIn("error_correlation_id", record.model_dump())
        self.assertNotIn("\n", record.error_message)
        for value in (
            "example-bearer-value",
            "dXNlcjpzaG91bGQtbm90LXN1cnZpdmU=",
            "short-cookie-secret",
            "example-token-value",
            "operator:password",
            "worker.internal",
            "/Users/operator/.config/launchplane/token",
        ):
            self.assertNotIn(value, payload)

    def test_run_once_blocks_before_launch_without_claim_comment_token(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            checkout_root = temporary_root / "Developer" / "code"
            checkout_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            state_dir = temporary_root / "state"
            store = FilesystemRecordStore(state_dir=temporary_root / "state")
            store.write_every_code_work_request_record(_queued_record())
            runner = _Runner()

            with patch.dict(os.environ, {"LAUNCHPLANE_EVERY_CODE_GITHUB_TOKEN": ""}):
                result = run_every_code_worker_once(
                    record_store=store,
                    host="Chris-Studio",
                    workspace_root=temporary_root / "Developer",
                    state_dir=temporary_root / "state",
                    runner=runner,
                )
            record = store.read_every_code_work_request_record("every-code-cbusillo-code-123-test")
            worktree_exists = every_code_worktree_root(
                _queued_record(), state_dir=state_dir
            ).exists()

        self.assertEqual(result.status, "blocked")
        self.assertEqual(record.state, "blocked")
        self.assertIn("LAUNCHPLANE_EVERY_CODE_GITHUB_TOKEN", record.error_message)
        self.assertFalse(any(call[1] == "new-session" for call in runner.calls))
        self.assertFalse(worktree_exists)

    def test_run_once_uses_configured_claim_comment_token_env(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            checkout_root = temporary_root / "Developer" / "code"
            checkout_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            store = FilesystemRecordStore(state_dir=temporary_root / "state")
            store.write_every_code_work_request_record(_queued_record())
            runner = _Runner()

            with patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_TOKEN": "",
                    "CUSTOM_EVERY_CODE_GITHUB_TOKEN": "bot-token",
                },
            ):
                result = run_every_code_worker_once(
                    record_store=store,
                    host="Chris-Studio",
                    workspace_root=temporary_root / "Developer",
                    state_dir=temporary_root / "state",
                    github_token_env="CUSTOM_EVERY_CODE_GITHUB_TOKEN",
                    runner=runner,
                )

        self.assertEqual(result.status, "running")
        self.assertTrue(any(call[:3] == ("gh", "api", "user") for call in runner.calls))
        self.assertTrue(any(call[:3] == ("gh", "issue", "comment") for call in runner.calls))

    def test_run_once_blocks_before_launch_on_claim_comment_actor_mismatch(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            checkout_root = temporary_root / "Developer" / "code"
            checkout_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            store = FilesystemRecordStore(state_dir=temporary_root / "state")
            store.write_every_code_work_request_record(_queued_record())
            runner = _Runner()

            with patch.dict(os.environ, {"LAUNCHPLANE_EVERY_CODE_GITHUB_ACTOR": "cbusillo"}):
                result = run_every_code_worker_once(
                    record_store=store,
                    host="Chris-Studio",
                    workspace_root=temporary_root / "Developer",
                    state_dir=temporary_root / "state",
                    runner=runner,
                )
            record = store.read_every_code_work_request_record("every-code-cbusillo-code-123-test")

        self.assertEqual(result.status, "blocked")
        self.assertEqual(record.state, "blocked")
        self.assertIn("GitHub actor mismatch", record.error_message)
        self.assertFalse(any(call[1] == "new-session" for call in runner.calls))

    def test_finish_marks_running_request_done(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            checkout_root = temporary_root / "Developer" / "code"
            checkout_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            store = FilesystemRecordStore(state_dir=temporary_root / "state")
            store.write_every_code_work_request_record(_queued_record())
            run_every_code_worker_once(
                record_store=store,
                host="Chris-Studio",
                workspace_root=temporary_root / "Developer",
                state_dir=temporary_root / "state",
                runner=_Runner(),
            )

            result = finish_every_code_work_request(
                record_store=store,
                request_id="every-code-cbusillo-code-123-test",
                host="Chris-Studio",
                fencing_token=_current_fencing_token(store),
                exit_code=0,
                result_pr_url="https://github.com/cbusillo/code/pull/99",
            )
            record = store.read_every_code_work_request_record("every-code-cbusillo-code-123-test")

        self.assertEqual(result.status, "done")
        self.assertEqual(record.state, "done")
        self.assertEqual(record.result_pr_url, "https://github.com/cbusillo/code/pull/99")
        self.assertEqual(record.error_message, "")

    def test_finish_rejects_token_from_stale_session(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            checkout_root = temporary_root / "Developer" / "code"
            checkout_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            store = FilesystemRecordStore(state_dir=temporary_root / "state")
            store.write_every_code_work_request_record(_queued_record())
            run_every_code_worker_once(
                record_store=store,
                host="Chris-Studio",
                workspace_root=temporary_root / "Developer",
                state_dir=temporary_root / "state",
                runner=_Runner(),
            )
            first_attempt = store.read_every_code_work_request_record(_queued_record().request_id)
            store.write_every_code_work_request_record(
                requeue_every_code_work_request(
                    first_attempt,
                    queued_at="2026-05-06T00:10:00Z",
                )
            )
            second_attempt = store.claim_every_code_work_request_record(
                request_id=first_attempt.request_id,
                host="Chris-Studio",
                claimed_at="2026-05-06T00:11:00Z",
            )
            assert second_attempt is not None

            with self.assertRaisesRegex(ValueError, "fencing token"):
                finish_every_code_work_request(
                    record_store=store,
                    request_id=first_attempt.request_id,
                    host="Chris-Studio",
                    fencing_token=first_attempt.fencing_token,
                    exit_code=0,
                    result_pr_url="https://github.com/cbusillo/code/pull/99",
                )
            stored = store.read_every_code_work_request_record(first_attempt.request_id)

        self.assertEqual(stored.state, "claimed")
        self.assertEqual(stored.fencing_token, second_attempt.fencing_token)

    def test_finish_discovers_open_pr_for_successful_exit_without_result_url(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            checkout_root = temporary_root / "Developer" / "code"
            checkout_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            store = FilesystemRecordStore(state_dir=temporary_root / "state")
            store.write_every_code_work_request_record(_queued_record())
            run_every_code_worker_once(
                record_store=store,
                host="Chris-Studio",
                workspace_root=temporary_root / "Developer",
                state_dir=temporary_root / "state",
                runner=_Runner(),
            )
            runner = _Runner(pr_list_payload=[{"url": "https://github.com/cbusillo/code/pull/99"}])

            result = finish_every_code_work_request(
                record_store=store,
                request_id="every-code-cbusillo-code-123-test",
                host="Chris-Studio",
                fencing_token=_current_fencing_token(store),
                exit_code=0,
                runner=runner,
            )
            record = store.read_every_code_work_request_record("every-code-cbusillo-code-123-test")

        self.assertEqual(result.status, "done")
        self.assertEqual(record.state, "done")
        self.assertEqual(record.result_pr_url, "https://github.com/cbusillo/code/pull/99")

    def test_finish_blocks_successful_exit_without_pull_request(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            checkout_root = temporary_root / "Developer" / "code"
            checkout_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            store = FilesystemRecordStore(state_dir=temporary_root / "state")
            store.write_every_code_work_request_record(_queued_record())
            run_every_code_worker_once(
                record_store=store,
                host="Chris-Studio",
                workspace_root=temporary_root / "Developer",
                state_dir=temporary_root / "state",
                runner=_Runner(),
            )

            result = finish_every_code_work_request(
                record_store=store,
                request_id="every-code-cbusillo-code-123-test",
                host="Chris-Studio",
                fencing_token=_current_fencing_token(store),
                exit_code=0,
                runner=_Runner(pr_list_payload=[]),
            )
            record = store.read_every_code_work_request_record("every-code-cbusillo-code-123-test")

        self.assertEqual(result.status, "blocked")
        self.assertEqual(record.state, "blocked")
        self.assertEqual(record.result_pr_url, "")
        self.assertIn("did not open a pull request", record.error_message)

    def test_finish_defers_preview_label_until_gate_passes(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            checkout_root = temporary_root / "Developer" / "tenant-opw"
            checkout_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            store = FilesystemRecordStore(state_dir=temporary_root / "state")
            store.write_every_code_work_request_record(_queued_preview_record())
            runner = _Runner()
            run_every_code_worker_once(
                record_store=store,
                host="Chris-Studio",
                workspace_root=temporary_root / "Developer",
                state_dir=temporary_root / "state",
                runner=runner,
            )

            result = finish_every_code_work_request(
                record_store=store,
                request_id="every-code-every-tenant-opw-123-test",
                host="Chris-Studio",
                fencing_token=_current_fencing_token(store, "every-code-every-tenant-opw-123-test"),
                exit_code=0,
                result_pr_url="https://github.com/every/tenant-opw/pull/99",
                runner=runner,
            )
            record = store.read_every_code_work_request_record(
                "every-code-every-tenant-opw-123-test"
            )

        self.assertEqual(result.status, "done")
        self.assertNotIn("Requested Launchplane preview", record.result_summary)
        self.assertFalse(any(call[:3] == ("gh", "pr", "edit") for call in runner.calls))

    def test_finish_marks_failed_session_blocked(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            checkout_root = temporary_root / "Developer" / "code"
            checkout_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            store = FilesystemRecordStore(state_dir=temporary_root / "state")
            store.write_every_code_work_request_record(_queued_record())
            run_every_code_worker_once(
                record_store=store,
                host="Chris-Studio",
                workspace_root=temporary_root / "Developer",
                state_dir=temporary_root / "state",
                runner=_Runner(),
            )

            result = finish_every_code_work_request(
                record_store=store,
                request_id="every-code-cbusillo-code-123-test",
                host="Chris-Studio",
                fencing_token=_current_fencing_token(store),
                exit_code=7,
            )
            record = store.read_every_code_work_request_record("every-code-cbusillo-code-123-test")

        self.assertEqual(result.status, "blocked")
        self.assertEqual(record.state, "blocked")
        self.assertIn("exited with status 7", record.error_message)

    def test_finish_is_idempotent_when_request_already_terminal(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            checkout_root = temporary_root / "Developer" / "code"
            checkout_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            store = FilesystemRecordStore(state_dir=temporary_root / "state")
            store.write_every_code_work_request_record(_queued_record())
            run_every_code_worker_once(
                record_store=store,
                host="Chris-Studio",
                workspace_root=temporary_root / "Developer",
                state_dir=temporary_root / "state",
                runner=_Runner(),
            )
            record = store.read_every_code_work_request_record(
                "every-code-cbusillo-code-123-test"
            ).model_copy(
                update={
                    "state": "done",
                    "finished_at": "2026-05-06T00:02:00Z",
                    "updated_at": "2026-05-06T00:02:00Z",
                    "result_summary": "Linked pull request merged.",
                }
            )
            store.write_every_code_work_request_record(record)

            result = finish_every_code_work_request(
                record_store=store,
                request_id="every-code-cbusillo-code-123-test",
                host="Chris-Studio",
                fencing_token=record.fencing_token,
                exit_code=0,
            )

        self.assertEqual(result.status, "done")
        self.assertEqual(result.detail, "Linked pull request merged.")

    def test_finish_does_not_retry_preview_label_for_terminal_done_request(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            checkout_root = temporary_root / "Developer" / "sellyouroutboard"
            checkout_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            store = FilesystemRecordStore(state_dir=temporary_root / "state")
            store.write_every_code_work_request_record(_queued_record())
            run_every_code_worker_once(
                record_store=store,
                host="Chris-Studio",
                workspace_root=temporary_root / "Developer",
                state_dir=temporary_root / "state",
                runner=_Runner(),
            )
            record = store.read_every_code_work_request_record(
                "every-code-cbusillo-code-123-test"
            ).model_copy(
                update={
                    "repository": "cbusillo/sellyouroutboard",
                    "state": "done",
                    "finished_at": "2026-05-06T00:02:00Z",
                    "updated_at": "2026-05-06T00:02:00Z",
                    "result_pr_url": "https://github.com/cbusillo/sellyouroutboard/pull/71",
                    "result_summary": "Every Code opened PR #71.",
                    "error_message": "",
                }
            )
            store.write_every_code_work_request_record(record)
            runner = _Runner()

            result = finish_every_code_work_request(
                record_store=store,
                request_id="every-code-cbusillo-code-123-test",
                host="Chris-Studio",
                fencing_token=record.fencing_token,
                exit_code=0,
                runner=runner,
            )

        self.assertEqual(result.status, "done")
        self.assertEqual(
            result.result_pr_url,
            "https://github.com/cbusillo/sellyouroutboard/pull/71",
        )
        self.assertEqual(result.detail, "Every Code opened PR #71.")
        self.assertFalse(any(call[:3] == ("gh", "pr", "edit") for call in runner.calls))

    def test_run_once_marks_missing_checkout_blocked(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=temporary_root / "state")
            store.write_every_code_work_request_record(_queued_record())

            result = run_every_code_worker_once(
                record_store=store,
                host="Chris-Studio",
                workspace_root=temporary_root / "Developer",
                runner=_Runner(),
            )
            record = store.read_every_code_work_request_record("every-code-cbusillo-code-123-test")

        self.assertEqual(result.status, "blocked")
        self.assertEqual(record.state, "blocked")
        self.assertIn("Checkout root does not exist", record.error_message)

    def test_run_once_returns_empty_when_no_queued_request_exists(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            result = run_every_code_worker_once(
                record_store=FilesystemRecordStore(
                    state_dir=Path(temporary_directory_name) / "state"
                ),
                host="Chris-Studio",
                workspace_root=Path(temporary_directory_name) / "Developer",
                runner=_Runner(),
            )

        self.assertEqual(result.status, "empty")

    def test_run_loop_processes_until_max_iterations(self) -> None:
        sleeps: list[float] = []
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            checkout_root = temporary_root / "Developer" / "code"
            checkout_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            store = FilesystemRecordStore(state_dir=temporary_root / "state")
            store.write_every_code_work_request_record(_queued_record())
            runner = _Runner()

            result = run_every_code_worker_loop(
                record_store=store,
                host="Chris-Studio",
                workspace_root=temporary_root / "Developer",
                interval_seconds=2.5,
                max_iterations=2,
                runner=runner,
                sleeper=sleeps.append,
            )

        self.assertEqual(result.iterations, 2)
        self.assertEqual(result.handed_off, 1)
        self.assertEqual(result.empty, 1)
        self.assertEqual(result.blocked, 0)
        self.assertEqual(result.stopped_reason, "max_iterations")
        self.assertEqual(sleeps, [2.5])

    def test_run_loop_claims_request_when_pr_feedback_maintenance_fails(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            checkout_root = temporary_root / "Developer" / "code"
            checkout_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            store = _MaintenanceFailingStore(state_dir=temporary_root / "state")
            store.write_every_code_work_request_record(_queued_record())
            result = run_every_code_worker_loop(
                record_store=store,
                host="Chris-Studio",
                workspace_root=temporary_root / "Developer",
                state_dir=temporary_root / "worker-state",
                interval_seconds=0,
                max_iterations=1,
                runner=_Runner(),
                sleeper=lambda _seconds: None,
            )
            record = store.read_every_code_work_request_record("every-code-cbusillo-code-123-test")

        self.assertEqual(result.handed_off, 1)
        self.assertEqual(result.blocked, 0)
        self.assertEqual(record.state, "running")
        self.assertIn("Maintenance warning", result.last_result.detail)
        self.assertIn("HTTP 401", result.last_result.detail)

    def test_run_loop_rejects_negative_interval(self) -> None:
        with self.assertRaises(ValueError):
            run_every_code_worker_loop(
                record_store=FilesystemRecordStore(state_dir=Path("state")),
                host="Chris-Studio",
                workspace_root=Path("."),
                interval_seconds=-1,
                max_iterations=1,
                sleeper=lambda _seconds: None,
            )

    def test_cli_run_reports_loop_summary(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            result = CliRunner().invoke(
                main,
                [
                    "every-code",
                    "run",
                    "--state-dir",
                    str(temporary_root / "state"),
                    "--workspace-root",
                    str(temporary_root / "Developer"),
                    "--host",
                    "Chris-Studio",
                    "--interval-seconds",
                    "0",
                    "--max-iterations",
                    "1",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["iterations"], 1)
        self.assertEqual(payload["empty"], 1)
        self.assertEqual(payload["stopped_reason"], "max_iterations")

    def test_cli_run_once_reconciles_ready_preview_gate(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            state_dir = temporary_root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            _write_sellyouroutboard_preview_profile(store)
            store.write_every_code_work_request_record(
                _done_record(
                    repository="cbusillo/sellyouroutboard",
                    result_pr_url="https://github.com/cbusillo/sellyouroutboard/pull/86",
                ).model_copy(
                    update={"issue_url": "https://github.com/cbusillo/sellyouroutboard/issues/123"}
                )
            )
            runner = _Runner(
                pr_view_payload={
                    "state": "OPEN",
                    "headRefOid": "abcdef1234567890",
                    "labels": [],
                    "statusCheckRollup": [
                        {
                            "name": "static_checks",
                            "status": "COMPLETED",
                            "conclusion": "SUCCESS",
                        },
                    ],
                }
            )
            with patch("control_plane.every_code_worker._run_subprocess", runner):
                result = CliRunner().invoke(
                    main,
                    [
                        "every-code",
                        "run-once",
                        "--state-dir",
                        str(state_dir),
                        "--workspace-root",
                        str(temporary_root / "Developer"),
                        "--host",
                        "Chris-Studio",
                        "--repository",
                        "cbusillo/sellyouroutboard",
                    ],
                )
            gate_records = store.list_every_code_preview_gate_records(
                request_id="every-code-cbusillo-code-123-test",
                pr_number=86,
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["status"], "empty")
        self.assertEqual(len(gate_records), 1)
        self.assertEqual(gate_records[0].status, "labeled")
        self.assertIn(
            (
                "gh",
                "pr",
                "edit",
                "86",
                "--repo",
                "cbusillo/sellyouroutboard",
                "--add-label",
                "preview",
            ),
            runner.calls,
        )

    def test_cli_api_mode_requires_worker_token_env(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            result = CliRunner().invoke(
                main,
                [
                    "every-code",
                    "run-once",
                    "--service-url",
                    "https://launchplane.example",
                    "--worker-token-env",
                    "MISSING_EVERY_CODE_TOKEN",
                    "--state-dir",
                    str(temporary_root / "state"),
                    "--workspace-root",
                    str(temporary_root / "Developer"),
                ],
            )

        self.assertEqual(result.exit_code, 1)
        self.assertIn(
            "MISSING_EVERY_CODE_TOKEN is required when --service-url is set",
            result.output,
        )

    def test_daemon_spec_builds_worker_run_command(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            spec = build_every_code_worker_daemon_spec(
                state_dir=temporary_root / "state",
                database_url="postgres://example",
                service_url="https://launchplane.example",
                host="Chris-Studio",
                workspace_root=temporary_root / "Developer",
                worktree_state_dir=temporary_root / "worktrees",
                repository="cbusillo/code",
                github_token_env="CUSTOM_EVERY_CODE_GITHUB_TOKEN",
                github_actor_env="CUSTOM_EVERY_CODE_GITHUB_ACTOR",
                interval_seconds=15,
            )

        self.assertEqual(spec.pid_file.name, "worker.pid")
        self.assertIn("every-code", spec.command)
        self.assertIn("run", spec.command)
        self.assertIn("--database-url", spec.command)
        self.assertIn("postgres://example", spec.command)
        self.assertIn("--service-url", spec.command)
        self.assertIn("https://launchplane.example", spec.command)
        self.assertIn("--github-token-env", spec.command)
        self.assertIn("CUSTOM_EVERY_CODE_GITHUB_TOKEN", spec.command)
        self.assertIn("--github-actor-env", spec.command)
        self.assertIn("CUSTOM_EVERY_CODE_GITHUB_ACTOR", spec.command)
        self.assertIn("--worktree-state-dir", spec.command)
        self.assertIn(str((temporary_root / "worktrees").resolve()), spec.command)
        self.assertIn("cbusillo/code", spec.command)

    def test_start_daemon_writes_pid_file_and_log_path(self) -> None:
        launched: list[tuple[tuple[str, ...], Path, Path]] = []
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            spec = build_every_code_worker_daemon_spec(state_dir=temporary_root / "state")

            def launcher(args: Sequence[str], log_file: Path, cwd: Path) -> _Process:
                launched.append((tuple(args), log_file, cwd))
                return _Process(4242)

            result = start_every_code_worker_daemon(
                spec=spec,
                cwd=temporary_root,
                launcher=launcher,
            )
            payload = json.loads(spec.pid_file.read_text(encoding="utf-8"))

        self.assertEqual(result.status, "started")
        self.assertEqual(result.pid, 4242)
        self.assertEqual(payload["pid"], 4242)
        self.assertEqual(launched[0][1], spec.log_file)

    def test_start_daemon_reuses_running_pid(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            spec = build_every_code_worker_daemon_spec(state_dir=temporary_root / "state")
            spec.pid_file.parent.mkdir(parents=True)
            spec.pid_file.write_text('{"pid": 4242}\n', encoding="utf-8")
            with patch("control_plane.every_code_worker.os.kill") as kill:
                with patch(
                    "control_plane.every_code_worker._process_matches_expected_command",
                    return_value=True,
                ):
                    result = start_every_code_worker_daemon(
                        spec=spec,
                        cwd=temporary_root,
                        launcher=lambda _args, _log_file, _cwd: self.fail(
                            "launcher should not run"
                        ),
                    )

        kill.assert_called_once_with(4242, 0)
        self.assertEqual(result.status, "already_running")
        self.assertEqual(result.pid, 4242)

    def test_status_rejects_reused_pid_for_unrelated_process(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            spec = build_every_code_worker_daemon_spec(state_dir=temporary_root / "state")
            spec.pid_file.parent.mkdir(parents=True)
            spec.pid_file.write_text(
                '{"pid": 4242, "command": ["uv", "run", "launchplane"]}\n',
                encoding="utf-8",
            )
            with patch("control_plane.every_code_worker.os.kill"):
                with patch(
                    "control_plane.every_code_worker._process_matches_expected_command",
                    return_value=False,
                ):
                    status = every_code_worker_daemon_status(spec=spec)

        self.assertFalse(status.running)
        self.assertEqual(status.pid, 4242)
        self.assertIn("different process", status.detail)

    def test_start_daemon_replaces_reused_pid_file(self) -> None:
        launched: list[int] = []
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            spec = build_every_code_worker_daemon_spec(state_dir=temporary_root / "state")
            spec.pid_file.parent.mkdir(parents=True)
            spec.pid_file.write_text(
                '{"pid": 4242, "command": ["uv", "run", "launchplane"]}\n',
                encoding="utf-8",
            )
            with patch("control_plane.every_code_worker.os.kill"):
                with patch(
                    "control_plane.every_code_worker._process_matches_expected_command",
                    return_value=False,
                ):

                    def launcher(_args: Sequence[str], _log_file: Path, _cwd: Path) -> _Process:
                        launched.append(1)
                        return _Process(5252)

                    result = start_every_code_worker_daemon(
                        spec=spec,
                        cwd=temporary_root,
                        launcher=launcher,
                    )

        self.assertEqual(result.status, "started")
        self.assertEqual(result.pid, 5252)
        self.assertEqual(launched, [1])

    def test_start_daemon_rejects_concurrent_start(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            spec = build_every_code_worker_daemon_spec(state_dir=temporary_root / "state")
            spec.pid_file.parent.mkdir(parents=True)
            spec.pid_file.with_suffix(".lock").write_text("4242", encoding="utf-8")
            with patch("control_plane.every_code_worker.os.kill"):
                with self.assertRaisesRegex(RuntimeError, "already in progress"):
                    start_every_code_worker_daemon(
                        spec=spec,
                        cwd=temporary_root,
                        launcher=lambda _args, _log_file, _cwd: self.fail(
                            "launcher should not run"
                        ),
                    )

    def test_start_daemon_removes_stale_lock(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            spec = build_every_code_worker_daemon_spec(state_dir=temporary_root / "state")
            spec.pid_file.parent.mkdir(parents=True)
            spec.pid_file.with_suffix(".lock").write_text("4242", encoding="utf-8")

            with patch(
                "control_plane.every_code_worker.os.kill",
                side_effect=ProcessLookupError,
            ):
                start_every_code_worker_daemon(
                    spec=spec,
                    cwd=temporary_root,
                    launcher=lambda _args, _log_file, _cwd: _Process(5252),
                )

            lock_exists = spec.pid_file.with_suffix(".lock").exists()

        self.assertFalse(lock_exists)

    def test_status_reports_stale_pid_file(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            spec = build_every_code_worker_daemon_spec(state_dir=temporary_root / "state")
            spec.pid_file.parent.mkdir(parents=True)
            spec.pid_file.write_text('{"pid": 4242}\n', encoding="utf-8")
            with patch(
                "control_plane.every_code_worker.os.kill",
                side_effect=ProcessLookupError,
            ):
                status = every_code_worker_daemon_status(spec=spec)

        self.assertFalse(status.running)
        self.assertEqual(status.pid, 4242)
        self.assertIn("stale", status.detail)

    def test_stop_daemon_signals_running_pid_and_removes_pid_file(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            spec = build_every_code_worker_daemon_spec(state_dir=temporary_root / "state")
            spec.pid_file.parent.mkdir(parents=True)
            spec.pid_file.write_text('{"pid": 4242}\n', encoding="utf-8")
            calls: list[tuple[int, int]] = []

            def fake_kill(pid: int, signal_number: int) -> None:
                calls.append((pid, signal_number))

            with patch("control_plane.every_code_worker.os.kill", side_effect=fake_kill):
                with patch(
                    "control_plane.every_code_worker._process_matches_expected_command",
                    return_value=True,
                ):
                    result = stop_every_code_worker_daemon(
                        spec=spec,
                        timeout_seconds=0,
                    )

            pid_file_exists = spec.pid_file.exists()

        self.assertEqual(result.status, "stopped")
        self.assertFalse(pid_file_exists)
        self.assertEqual(
            calls,
            [(4242, 0), (4242, signal.SIGTERM), (4242, 0), (4242, signal.SIGKILL)],
        )

    def test_cli_status_reports_not_running(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            result = CliRunner().invoke(
                main,
                ["every-code", "status", "--state-dir", str(temporary_root / "state")],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertFalse(payload["running"])
        self.assertIsNone(payload["pid"])

    def test_cli_finish_reports_done(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            checkout_root = temporary_root / "Developer" / "code"
            checkout_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            store = FilesystemRecordStore(state_dir=temporary_root / "state")
            store.write_every_code_work_request_record(_queued_record())
            run_every_code_worker_once(
                record_store=store,
                host="Chris-Studio",
                workspace_root=temporary_root / "Developer",
                state_dir=temporary_root / "state",
                runner=_Runner(),
            )
            fencing_token = _current_fencing_token(store)

            result = CliRunner().invoke(
                main,
                [
                    "every-code",
                    "finish",
                    "--state-dir",
                    str(temporary_root / "state"),
                    "--request-id",
                    "every-code-cbusillo-code-123-test",
                    "--host",
                    "Chris-Studio",
                    "--fencing-token",
                    str(fencing_token),
                    "--exit-code",
                    "0",
                    "--result-pr-url",
                    "https://github.com/cbusillo/code/pull/99",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["status"], "done")

    def test_cli_reconcile_previews_reports_gate_summary(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=temporary_root / "state")
            store.write_every_code_work_request_record(
                _done_record(
                    repository="cbusillo/code",
                    result_pr_url="https://github.com/cbusillo/code/pull/99",
                )
            )

            result = CliRunner().invoke(
                main,
                [
                    "every-code",
                    "reconcile-previews",
                    "--state-dir",
                    str(temporary_root / "state"),
                    "--repository",
                    "cbusillo/code",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["checked"], 1)
        self.assertEqual(payload["blocked"], 1)

    def test_cli_reconcile_cleanup_defaults_to_dry_run(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            state_dir = temporary_root / "state"
            checkout_root = temporary_root / "Developer" / "code"
            worktree_root = every_code_worktree_root(_queued_record(), state_dir=state_dir)
            checkout_root.mkdir(parents=True)
            worktree_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            (worktree_root / ".git").mkdir()
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_every_code_work_request_record(_terminal_record())
            state_path = _write_cleanup_session_state(
                state_dir=state_dir,
                source_checkout_root=checkout_root,
                launch_root=worktree_root,
            )
            runner = _CleanupReconciliationRunner(worktree_root=worktree_root)
            with patch("control_plane.every_code_worker._run_subprocess", runner):
                result = CliRunner().invoke(
                    main,
                    [
                        "every-code",
                        "reconcile-cleanup",
                        "--state-dir",
                        str(state_dir),
                        "--host",
                        "Chris-Studio",
                    ],
                )
            state_exists = state_path.exists()

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["mode"], "dry_run")
        self.assertEqual(payload["would_remove"], 1)
        self.assertTrue(state_exists)

    def test_cli_reconcile_cleanup_apply_removes_safe_state(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            state_dir = temporary_root / "state"
            checkout_root = temporary_root / "Developer" / "code"
            worktree_root = every_code_worktree_root(_queued_record(), state_dir=state_dir)
            checkout_root.mkdir(parents=True)
            worktree_root.mkdir(parents=True)
            (checkout_root / ".git").mkdir()
            (worktree_root / ".git").mkdir()
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_every_code_work_request_record(_terminal_record())
            state_path = _write_cleanup_session_state(
                state_dir=state_dir,
                source_checkout_root=checkout_root,
                launch_root=worktree_root,
            )
            runner = _CleanupReconciliationRunner(worktree_root=worktree_root)
            with patch("control_plane.every_code_worker._run_subprocess", runner):
                result = CliRunner().invoke(
                    main,
                    [
                        "every-code",
                        "reconcile-cleanup",
                        "--state-dir",
                        str(state_dir),
                        "--host",
                        "Chris-Studio",
                        "--apply",
                    ],
                )
            state_exists = state_path.exists()

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["mode"], "apply")
        self.assertEqual(payload["removed"], 1)
        self.assertFalse(state_exists)


if __name__ == "__main__":
    unittest.main()
