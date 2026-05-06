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

from click.testing import CliRunner

from control_plane.cli import main
from control_plane.contracts.every_code_work_request import EveryCodeWorkRequestRecord
from control_plane.every_code_worker import (
    EveryCodeWorkerApiStore,
    build_every_code_worker_daemon_spec,
    build_every_code_session_command,
    default_every_code_command,
    every_code_tmux_session_name,
    every_code_worker_daemon_status,
    finish_every_code_work_request,
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


class _Runner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(tuple(args))
        if args[1] == "has-session":
            return subprocess.CompletedProcess(args, 1, "", "no session")
        return subprocess.CompletedProcess(args, 0, "", "")


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
        if self.path.startswith("/v1/every-code/work-requests?"):
            records = self.store.list_every_code_work_request_records(state="queued", limit=1)
            self._write_json(
                200,
                {"status": "ok", "requests": [record.model_dump(mode="json") for record in records]},
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
        if self.path == "/v1/every-code/work-requests/claim":
            record = self.store.claim_every_code_work_request_record(
                request_id=str(payload["request_id"]),
                host=str(payload["host"]),
                claimed_at="2026-05-06T00:00:00Z",
            )
            if record is None:
                self._write_json(409, {"status": "rejected"})
                return
            self._write_json(
                202,
                {
                    "status": "accepted",
                    "records": {"request_id": record.request_id, "state": record.state},
                    "result": {"request": record.model_dump(mode="json")},
                },
            )
            return
        record = self.store.read_every_code_work_request_record(str(payload["request_id"]))
        updated_record = record.model_copy(
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
        self.store.write_every_code_work_request_record(updated_record)
        self._write_json(
            202,
            {
                "status": "accepted",
                "records": {
                    "request_id": updated_record.request_id,
                    "state": updated_record.state,
                },
                "result": {"request": updated_record.model_dump(mode="json")},
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
            _EveryCodeApiHandler.store = FilesystemRecordStore(
                state_dir=temporary_root / "state"
            )
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

    def test_session_name_is_stable_and_tmux_safe(self) -> None:
        session_name = every_code_tmux_session_name("every code/cbusillo/code#123 !")

        self.assertEqual(session_name, "every-code-every-code-cbusillo-code-123")

    def test_default_command_includes_issue_and_request(self) -> None:
        command = default_every_code_command(_queued_record())

        self.assertIn("https://github.com/cbusillo/code/issues/123", command)
        self.assertIn("every-code-cbusillo-code-123-test", command)

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

    def test_run_once_claims_request_and_launches_tmux_session(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            checkout_root = temporary_root / "Developer" / "code"
            checkout_root.mkdir(parents=True)
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

        self.assertEqual(result.status, "running")
        self.assertEqual(record.state, "running")
        self.assertEqual(record.claimed_by_host, "Chris-Studio")
        self.assertEqual(result.checkout_root, str(checkout_root.resolve()))
        self.assertEqual(runner.calls[0][1], "has-session")
        self.assertEqual(runner.calls[1][1], "new-session")
        self.assertIn("every-code finish", runner.calls[1][-1])

    def test_finish_marks_running_request_done(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            checkout_root = temporary_root / "Developer" / "code"
            checkout_root.mkdir(parents=True)
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

    def test_finish_marks_failed_session_blocked(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_root = Path(temporary_directory_name)
            checkout_root = temporary_root / "Developer" / "code"
            checkout_root.mkdir(parents=True)
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
                    def launcher(
                        _args: Sequence[str], _log_file: Path, _cwd: Path
                    ) -> _Process:
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
