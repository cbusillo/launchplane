"""Execute worker shell wrappers with isolated local executables and no network."""

import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from control_plane.contracts.every_code_pr_feedback_record import EveryCodePrFeedbackRecord
from control_plane.contracts.every_code_work_request import EveryCodeWorkRequestRecord
from control_plane.every_code_worker import (
    build_every_code_feedback_session_command,
    build_every_code_session_command,
    default_every_code_command,
    finish_every_code_work_request,
)
from control_plane.storage.filesystem import FilesystemRecordStore


class AgentSessionLauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.worktree = self.root / "worker's checkout"
        self.worktree.mkdir()
        self.state = self.root / "worker's state"
        self.host = "worker's host"
        self.literal = "Keep 'quotes', $HOME, $(touch injected), `touch injected`\nand newlines"
        self.record = EveryCodeWorkRequestRecord(
            request_id="agent-session-example-project-123",
            source="github_issue_label",
            state="claimed",
            repository="example/project",
            issue_number=123,
            issue_url="https://github.com/example/project/issues/123",
            issue_title=self.literal,
            trigger_label="every-code",
            trigger_actor="operator",
            github_delivery_id="delivery-1",
            queued_at="2026-05-05T22:00:00Z",
            updated_at="2026-05-05T22:01:00Z",
            claimed_at="2026-05-05T22:01:00Z",
            claimed_by_host=self.host,
            lease_expires_at="2026-05-05T22:31:00Z",
            fencing_token=7,
            attempt=1,
        )
        self.feedback = EveryCodePrFeedbackRecord(
            feedback_id="feedback-1",
            request_id=self.record.request_id,
            repository=self.record.repository,
            pr_number=26,
            pr_url="https://github.com/example/project/pull/26",
            feedback_kind="issue_comment",
            github_delivery_id="delivery-comment",
            github_node_id="IC_test_1001",
            github_id="1001",
            actor="operator",
            body=self.literal,
            html_url="https://github.com/example/project/pull/26#issuecomment-1001",
            received_at="2026-05-06T19:00:00Z",
            status="pending",
        )

    def executable(self, name: str, exit_code: int) -> None:
        script = self.root / f"{name}.py"
        script.write_text(
            "import json, os, sys\nfrom pathlib import Path\n"
            f"Path({str(self.root / (name + '.json'))!r}).write_text(json.dumps({{\n"
            "'argv': sys.argv[1:], 'cwd': os.getcwd(),\n"
            "'origin': {k: v for k, v in os.environ.items() if k.startswith(('AGENT_SESSION_', 'EVERY_CODE_'))}\n"
            "}))\n"
            f"sys.exit({exit_code})\n"
        )
        executable = self.bin / name
        executable.write_text(
            f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} "$@"\n'
        )
        executable.chmod(0o700)

    def run_wrapper(
        self, *, feedback: bool, exit_code: int | None, shell: str = "/bin/sh"
    ) -> list[str]:
        self.executable("uv", 0)
        self.executable("code", 99)
        if exit_code is not None:
            self.executable("codex-lab", exit_code)
        if feedback:
            command = build_every_code_feedback_session_command(
                record=self.record, feedback=self.feedback, state_dir=self.state, host=self.host
            )
        else:
            command = build_every_code_session_command(
                record=self.record,
                command=default_every_code_command(self.record),
                state_dir=self.state,
                host=self.host,
            )
        result = subprocess.run(
            [shell, "-f", "-c", command],
            cwd=self.worktree,
            env={"PATH": str(self.bin), "HOME": str(self.root), "ZDOTDIR": str(self.root)},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        expected_exit = 127 if exit_code is None else exit_code
        self.assertEqual(result.returncode, expected_exit, result.stderr)
        self.assertFalse((self.root / "code.json").exists(), "Must never fall back to Every Code")
        self.assertFalse((self.worktree / "injected").exists())
        callback = json.loads((self.root / "uv.json").read_text())
        expected_args = [
            "run",
            "launchplane",
            "every-code",
            "finish",
            "--state-dir",
            str(self.state),
            "--request-id",
            self.record.request_id,
            "--host",
            self.host,
            "--fencing-token",
            "7",
            "--exit-code",
            str(expected_exit),
        ]
        self.assertEqual(callback["argv"], expected_args)
        self.assertEqual(callback["cwd"], str(self.worktree))
        if exit_code is not None:
            invocation = json.loads((self.root / "codex-lab.json").read_text())
            self.assertEqual(len(invocation["argv"]), 1)
            self.assertIn(self.literal, invocation["argv"][0])
            self.assertEqual(invocation["cwd"], str(self.worktree))
            self.assertEqual(
                invocation["origin"],
                {
                    "AGENT_SESSION_ORIGIN": "launchplane",
                    "AGENT_SESSION_SOURCE": "agent-session",
                    "AGENT_SESSION_REQUEST_ID": self.record.request_id,
                    "AGENT_SESSION_REPOSITORY": self.record.repository,
                    "AGENT_SESSION_ISSUE_NUMBER": "123",
                    "AGENT_SESSION_ISSUE_URL": self.record.issue_url,
                },
            )
        return list(callback["argv"])

    def test_initial_session_success_and_failure_report_exit_and_lease(self) -> None:
        for exit_code in (0, 23):
            with self.subTest(exit_code=exit_code):
                self.run_wrapper(feedback=False, exit_code=exit_code)

    def test_feedback_session_success_and_failure_preserve_literal_prompt(self) -> None:
        for exit_code in (0, 23):
            with self.subTest(exit_code=exit_code):
                self.run_wrapper(feedback=True, exit_code=exit_code)

    def test_missing_local_binary_reports_failure_without_every_code_fallback(self) -> None:
        for feedback in (False, True):
            with self.subTest(feedback=feedback):
                self.run_wrapper(feedback=feedback, exit_code=None)

    def test_captured_completion_cannot_finish_a_successor_lease(self) -> None:
        callback = self.run_wrapper(feedback=False, exit_code=0)
        store = FilesystemRecordStore(state_dir=self.state)
        successor = self.record.model_copy(update={"fencing_token": 8, "attempt": 2})
        store.write_every_code_work_request_record(successor)
        with self.assertRaisesRegex(ValueError, "fencing token 7 .* fencing token 8"):
            finish_every_code_work_request(
                record_store=store,
                request_id=callback[callback.index("--request-id") + 1],
                host=callback[callback.index("--host") + 1],
                fencing_token=int(callback[callback.index("--fencing-token") + 1]),
                exit_code=int(callback[callback.index("--exit-code") + 1]),
                result_pr_url=self.feedback.pr_url,
            )
        self.assertEqual(store.read_every_code_work_request_record(successor.request_id), successor)

    def test_zsh_worker_shell_reports_completion_for_both_launch_paths(self) -> None:
        shell = shutil.which("zsh")
        if shell is None:
            self.skipTest("zsh is not installed on this test host")
        for feedback in (False, True):
            for exit_code in (0, 23):
                with self.subTest(feedback=feedback, exit_code=exit_code):
                    self.run_wrapper(feedback=feedback, exit_code=exit_code, shell=shell)
