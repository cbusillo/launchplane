import subprocess
import unittest
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import signal
import threading
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from click.testing import CliRunner

from control_plane.cli import main
from control_plane.contracts.every_code_pr_feedback_record import EveryCodePrFeedbackRecord
from control_plane.contracts.every_code_work_request import EveryCodeWorkRequestRecord
from control_plane.contracts.every_code_work_request import requeue_every_code_work_request
from control_plane.every_code_worker import (
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
    request_every_code_pr_preview_label,
    run_every_code_worker_loop,
    run_every_code_worker_once,
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
        actor="cbusillo",
        body="Please tighten the README wording before merge.",
        html_url="https://github.com/cbusillo/code/pull/26#issuecomment-1001",
        received_at="2026-05-06T19:00:00Z",
        status=status,  # type: ignore[arg-type]
    )


class _Runner:
    def __init__(self, *, fail_issue_comment: bool = False, existing_branch: bool = False) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.fail_issue_comment = fail_issue_comment
        self.existing_branch = existing_branch

    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
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
            if self.fail_issue_comment:
                return subprocess.CompletedProcess(args, 1, "", "rate limited")
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[1] == "display-message":
            return subprocess.CompletedProcess(args, 0, "4242\n", "")
        if args[1] == "has-session":
            return subprocess.CompletedProcess(args, 1, "", "no session")
        return subprocess.CompletedProcess(args, 0, "", "")


class _ExistingSessionRunner(_Runner):
    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(tuple(args))
        if args[1] == "has-session":
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[1] == "display-message":
            return subprocess.CompletedProcess(args, 0, "4242\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")


class _GoneSessionRunner(_Runner):
    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(tuple(args))
        if args[1] == "has-session":
            return subprocess.CompletedProcess(args, 1, "", "no session")
        if args[0] == "tmux" and args[1] == "new-session":
            return subprocess.CompletedProcess(args, 0, "", "")
        return super().__call__(args)


class _Process:
    def __init__(self, pid: int) -> None:
        self.pid = pid


class _EveryCodeApiHandler(BaseHTTPRequestHandler):
    store: FilesystemRecordStore
    token = "worker-token"

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        if self.headers.get("Authorization") != f"Bearer {self.token}":
            self._write_json(401, {"status": "rejected"})
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

        self.assertEqual(session_name, "every-code-every-code-cbusillo-code-123")

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
            record=_queued_record(),
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
        self.assertIn("--exit-code $status", command)

    def test_preview_label_request_labels_eligible_pull_request(self) -> None:
        runner = _Runner()

        summary = request_every_code_pr_preview_label(
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
        runner = _Runner()

        summary = request_every_code_pr_preview_label(
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
        runner = _Runner()

        summary = request_every_code_pr_preview_label(
            result_pr_url="https://github.com/cbusillo/code/pull/123",
            runner=runner,
        )

        self.assertEqual(summary, "")
        self.assertEqual(runner.calls, [])

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
        self.assertTrue(any(call[:3] == ("gh", "issue", "comment") for call in runner.calls))

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
        send_call = next(call for call in runner.calls if call[1] == "send-keys")
        self.assertIn("Please tighten the README wording", send_call[4])

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
            runner = _ExistingSessionRunner()

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
        self.assertEqual(runner.calls[1][1], "display-message")

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

    def test_run_once_still_launches_tmux_when_claim_comment_fails(self) -> None:
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

        self.assertEqual(result.status, "running")
        self.assertEqual(record.state, "running")
        self.assertIn("Visible tmux session", record.result_summary)
        self.assertIn("Could not post GitHub working comment", record.result_summary)
        self.assertTrue(any(call[1] == "new-session" for call in runner.calls))
        self.assertTrue(any(call[:3] == ("gh", "issue", "comment") for call in runner.calls))

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
                exit_code=0,
                result_pr_url="https://github.com/cbusillo/code/pull/99",
            )
            record = store.read_every_code_work_request_record("every-code-cbusillo-code-123-test")

        self.assertEqual(result.status, "done")
        self.assertEqual(record.state, "done")
        self.assertEqual(record.result_pr_url, "https://github.com/cbusillo/code/pull/99")
        self.assertEqual(record.error_message, "")

    def test_finish_requests_preview_label_for_eligible_pr(self) -> None:
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
                exit_code=0,
                result_pr_url="https://github.com/every/tenant-opw/pull/99",
                runner=runner,
            )
            record = store.read_every_code_work_request_record(
                "every-code-every-tenant-opw-123-test"
            )

        self.assertEqual(result.status, "done")
        self.assertIn("Requested Launchplane preview", record.result_summary)
        self.assertTrue(any(call[:3] == ("gh", "pr", "edit") for call in runner.calls))

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
                exit_code=0,
            )

        self.assertEqual(result.status, "done")
        self.assertEqual(result.detail, "Linked pull request merged.")

    def test_finish_retries_preview_label_for_terminal_done_request(self) -> None:
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
                exit_code=0,
                runner=runner,
            )

        self.assertEqual(result.status, "done")
        self.assertEqual(
            result.result_pr_url,
            "https://github.com/cbusillo/sellyouroutboard/pull/71",
        )
        self.assertIn("Requested Launchplane preview", result.detail)
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
                interval_seconds=15,
            )

        self.assertEqual(spec.pid_file.name, "worker.pid")
        self.assertIn("every-code", spec.command)
        self.assertIn("run", spec.command)
        self.assertIn("--database-url", spec.command)
        self.assertIn("postgres://example", spec.command)
        self.assertIn("--service-url", spec.command)
        self.assertIn("https://launchplane.example", spec.command)
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
                    "--exit-code",
                    "0",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["status"], "done")


if __name__ == "__main__":
    unittest.main()
