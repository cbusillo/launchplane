import subprocess
import unittest
from collections.abc import Sequence
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from click.testing import CliRunner

from control_plane.cli import main
from control_plane.contracts.every_code_work_request import EveryCodeWorkRequestRecord
from control_plane.every_code_worker import (
    default_every_code_command,
    every_code_tmux_session_name,
    run_every_code_worker_loop,
    run_every_code_worker_once,
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


class EveryCodeWorkerTests(unittest.TestCase):
    def test_session_name_is_stable_and_tmux_safe(self) -> None:
        session_name = every_code_tmux_session_name("every code/cbusillo/code#123 !")

        self.assertEqual(session_name, "every-code-every-code-cbusillo-code-123")

    def test_default_command_includes_issue_and_request(self) -> None:
        command = default_every_code_command(_queued_record())

        self.assertIn("https://github.com/cbusillo/code/issues/123", command)
        self.assertIn("every-code-cbusillo-code-123-test", command)

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
                runner=runner,
            )
            record = store.read_every_code_work_request_record("every-code-cbusillo-code-123-test")

        self.assertEqual(result.status, "running")
        self.assertEqual(record.state, "running")
        self.assertEqual(record.claimed_by_host, "Chris-Studio")
        self.assertEqual(result.checkout_root, str(checkout_root.resolve()))
        self.assertEqual(runner.calls[0][1], "has-session")
        self.assertEqual(runner.calls[1][1], "new-session")

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


if __name__ == "__main__":
    unittest.main()
