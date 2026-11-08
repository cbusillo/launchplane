from pathlib import Path
import subprocess
import os
import sys
import json
import tempfile
import unittest
from typing import Any
from unittest.mock import patch

from control_plane.contracts.runner_lane_inventory import (
    RunnerLaneRecord,
    build_runner_lane_inventory,
)
from control_plane.workflows.runner_lane_ephemeral import (
    AddonRunnerRequest,
    Receipt,
    _start_runner_process,
    cleanup_ephemeral_addon_job,
    run_ephemeral_addon_job,
)


class FakeClient:
    def __init__(self, request: AddonRunnerRequest) -> None:
        self.request = request
        self.started = False
        self.job_runner_id = 71
        self.lanes: tuple[RunnerLaneRecord, ...] = ()
        self.retired: list[int] = []

    def read_addon_job(self, _request: AddonRunnerRequest) -> dict[str, object]:
        return {
            "status": "completed" if self.started else "queued",
            "conclusion": "success" if self.started else "",
            "runner_id": self.job_runner_id if self.started else None,
            "source_commit": "a" * 40,
            "recipe": {
                "Dockerfile": "FROM fixture",
                "entrypoint.sh": "#!/bin/sh\n",
                "source-commit": "a" * 40,
            },
        }

    def read_runner_lane_inventory(self, *, repository: str) -> Any:
        return build_runner_lane_inventory(
            repository=repository, lanes=self.lanes, observed_at="2026-11-08T17:00:00Z"
        )

    def fetch_registration_token(self, *, repository: str) -> Any:
        self.lanes = (
            RunnerLaneRecord(
                github_id=71,
                name=self.request.name,
                repository=repository,
                status="offline",
                busy=False,
                labels=(self.request.label,),
                observed_at="2026-11-08T17:00:00Z",
            ),
        )
        return "synthetic-registration-value", None

    def delete_runner(self, *, repository: str, runner_id: int) -> None:
        self.retired.append(runner_id)
        self.lanes = tuple(lane for lane in self.lanes if lane.github_id != runner_id)


class EphemeralAddonRunnerTests(unittest.TestCase):
    def test_already_completed_job_records_no_execution_and_creates_no_resources(self) -> None:
        self.client.started = True
        result = self.execute()
        self.assertEqual(result["status"], "not_queued")
        self.assertEqual(json.loads((self.output / "result.json").read_text()), result)
        self.assertEqual(self.calls, [])

    def test_repeated_cleanup_preserves_original_job_and_removes_nothing_else(self) -> None:
        first = self.execute()
        mutations = len(self.calls)
        second = cleanup_ephemeral_addon_job(
            client=self.client, request=self.request, output=self.output, command=self.command
        )
        self.assertEqual(second["status"], first["status"])
        self.assertTrue(second["container_removed"])
        self.assertEqual(len(self.calls), mutations)

    def test_cleanup_refuses_another_job_receipt_before_any_mutation(self) -> None:
        self.execute()
        before = len(self.calls)
        changed = AddonRunnerRequest(
            repository=self.request.repository, run_id=12, run_attempt=2, job_id=99
        )
        with self.assertRaises(ValueError):
            cleanup_ephemeral_addon_job(
                client=self.client, request=changed, output=self.output, command=self.command
            )
        self.assertEqual(len(self.calls), before)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name) / "evidence"
        self.request = AddonRunnerRequest(
            repository="example/tenant", run_id=12, run_attempt=2, job_id=34
        )
        self.client = FakeClient(self.request)
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.container_id = "c" * 64
        self.fail_cleanup = False
        self.listener_exit = 0
        self.cancel = False
        self.container_present = False
        self.malformed_create = False

    def command(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, kwargs))
        if args[1] == "create":
            self.container_present = True
            identity = "unexpected stdout" if self.malformed_create else self.container_id
            return subprocess.CompletedProcess(args, 0, identity + "\n", "")
        if args[1] == "start":
            self.client.started = True
            if self.cancel:
                raise InterruptedError("fixture cancellation")
            return subprocess.CompletedProcess(
                args, self.listener_exit, '{"runner_id":71,"ephemeral":true}\n', ""
            )
        if args[1] == "rm":
            if self.fail_cleanup:
                return subprocess.CompletedProcess(args, 1, "", "fixture failure")
            self.container_present = False
        if args[1:3] == ["container", "inspect"]:
            return subprocess.CompletedProcess(args, int(not self.container_present), "", "")
        if args[1:3] == ["container", "ls"]:
            return subprocess.CompletedProcess(
                args, 0, self.container_id if self.container_present else "", ""
            )
        if args[1] == "inspect":
            if "launchplane.ephemeral-addon" in args[3]:
                return subprocess.CompletedProcess(args, 0, self.request.name, "")
            return subprocess.CompletedProcess(args, 0, "[]", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    def execute(self) -> dict[str, object]:
        return run_ephemeral_addon_job(
            client=self.client,
            request=self.request,
            output=self.output,
            command=self.command,
            start_runner=self.start_runner,
        )

    def start_runner(
        self, container_id: str, token: str, registered: Receipt
    ) -> subprocess.CompletedProcess[str]:
        registered({"runner_id": 71, "ephemeral": True})
        return self.command(
            ["docker", "start", "--attach", "--interactive", container_id], input=token + "\n"
        )

    def test_native_one_job_execution_removes_exact_container_and_remaining_registration(
        self,
    ) -> None:
        result = self.execute()
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["container_removed"])
        self.assertTrue(result["registration_removed"])
        self.assertEqual(self.client.retired, [71])
        created = next(args for args, _ in self.calls if args[1] == "create")
        self.assertIn("--cgroupns=private", created)
        stopped = next(index for index, (args, _) in enumerate(self.calls) if args[1] == "stop")
        removed = next(index for index, (args, _) in enumerate(self.calls) if args[1] == "rm")
        self.assertLess(stopped, removed)
        removal = next(args for args, _ in self.calls if args[1] == "rm")
        self.assertEqual(removal[-1], self.container_id)
        arguments = " ".join(argument for args, _ in self.calls for argument in args)
        self.assertNotIn("synthetic-registration-value", arguments)
        start_input = next(options["input"] for args, options in self.calls if args[1] == "start")
        self.assertEqual(start_input, "synthetic-registration-value\n")

    def test_listener_failure_does_not_become_a_successful_addon_job(self) -> None:
        self.listener_exit = 17
        result = self.execute()
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["container_removed"])
        self.assertTrue(result["registration_removed"])

    def test_failed_startup_preserves_redacted_diagnostics_and_cleans_without_settlement_polling(
        self,
    ) -> None:
        def failed_start(
            container_id: str, token: str, _registered: Receipt
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                ["docker", "start", container_id], 3, "", f"fixture refused {token}"
            )

        with patch(
            "control_plane.workflows.runner_lane_ephemeral.time.sleep",
            side_effect=AssertionError("A failed startup does not need job-success settlement."),
        ):
            result = run_ephemeral_addon_job(
                client=self.client,
                request=self.request,
                output=self.output,
                command=self.command,
                start_runner=failed_start,
            )
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["container_removed"])
        self.assertTrue(result["registration_removed"])
        log = (self.output / "runner.log").read_text()
        self.assertIn("fixture refused", log)
        self.assertNotIn("synthetic-registration-value", log)

    def test_success_on_another_runner_does_not_prove_this_container_executed_the_job(self) -> None:
        self.client.job_runner_id = 99
        result = self.execute()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["job_runner_id"], self.client.job_runner_id)
        self.assertTrue(result["container_removed"])
        self.assertTrue(result["registration_removed"])

    def test_cancellation_removes_original_resources_before_propagating(self) -> None:
        self.cancel = True
        with self.assertRaises(InterruptedError):
            self.execute()
        self.assertFalse(self.container_present)
        self.assertEqual(self.client.retired, [71])
        self.assertTrue((self.output / "result.json").is_file())
        result = json.loads((self.output / "result.json").read_text())
        self.assertEqual(result["runner_id"], 71)

    def test_container_cleanup_failure_still_attempts_registration_cleanup_and_records_failure(
        self,
    ) -> None:
        self.fail_cleanup = True
        result = self.execute()
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["container_removed"])
        self.assertEqual(self.client.retired, [71])
        self.assertTrue((self.output / "result.json").is_file())

    def test_daemon_read_failure_never_proves_container_removal(self) -> None:
        original = self.command

        def unavailable(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            if args[1:3] in (["container", "inspect"], ["container", "ls"]):
                return subprocess.CompletedProcess(args, 1, "", "daemon unavailable")
            return original(args, **kwargs)

        result = run_ephemeral_addon_job(
            client=self.client,
            request=self.request,
            output=self.output,
            command=unavailable,
            start_runner=self.start_runner,
        )
        self.assertFalse(result["container_removed"])
        self.assertEqual(result["status"], "failed")

    def test_unknown_create_response_reconciles_its_original_owned_container(self) -> None:
        self.malformed_create = True
        with self.assertRaises(ValueError):
            self.execute()
        result = json.loads((self.output / "result.json").read_text())
        self.assertTrue(result["container_removed"])
        self.assertEqual(result["container_id"], self.container_id)
        self.assertFalse(self.container_present)


class NativeRunnerStreamTests(unittest.TestCase):
    def test_startup_failure_retains_diagnostics_without_claiming_a_native_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            program = root / "docker"
            program.write_text(
                f"#!{sys.executable}\nimport sys\n"
                "sys.stdin.readline()\n"
                "print('fixture registration refused', file=sys.stderr)\n"
                "sys.exit(3)\n"
            )
            program.chmod(0o755)
            with patch.dict(os.environ, {"PATH": f"{root}{os.pathsep}{os.environ['PATH']}"}):
                result = _start_runner_process(
                    "c" * 64,
                    "synthetic-registration-value",
                    lambda _: self.fail("No native identity was reported."),
                    timeout_seconds=5,
                )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("fixture registration refused", result.stderr)

    def test_native_identity_reaches_the_caller_before_the_listener_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            program = root / "docker"
            finished = root / "finished"
            release = root / "release"
            program.write_text(
                f"#!{sys.executable}\nimport json, sys, time\nfrom pathlib import Path\n"
                "token = sys.stdin.readline()\n"
                'print(json.dumps({"runner_id":71,"ephemeral":True}), flush=True)\n'
                f"while not Path({str(release)!r}).exists(): time.sleep(0.01)\n"
                f"Path({str(finished)!r}).write_text('done')\n"
            )
            program.chmod(0o755)

            def registered(receipt: dict[str, object]) -> None:
                self.assertEqual(receipt["runner_id"], 71)
                self.assertFalse(finished.exists())
                release.touch()

            with patch.dict(os.environ, {"PATH": f"{root}{os.pathsep}{os.environ['PATH']}"}):
                result = _start_runner_process(
                    "c" * 64, "synthetic-registration-value", registered, timeout_seconds=5
                )
            self.assertEqual(result.returncode, 0)
            self.assertTrue(finished.exists())
            self.assertNotIn("synthetic-registration-value", " ".join(result.args))


if __name__ == "__main__":
    unittest.main()
