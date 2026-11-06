import json
import re
import signal
import subprocess
import sys
import textwrap
import unittest
from unittest.mock import patch

from control_plane import service_bootstrap
from control_plane.contracts.merge_train_controller_state import build_merge_train_controller_key
from control_plane.github_request_timing import github_request_tally
from control_plane.github_request_timing import timed_github_request


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _tally_counts(stderr: str, label: str) -> dict[str, int]:
    (line,) = [line for line in stderr.splitlines() if label in line]
    return {key: int(value) for key, value in re.findall(r"(\w+)=(\d+)\b", line)}


class GitHubRequestTimingTests(unittest.TestCase):
    def test_worker_binds_partial_pass_receipts_and_actual_actions_in_one_observation(self) -> None:
        script = textwrap.dedent("""
            import logging
            from dataclasses import replace
            from unittest.mock import patch
            from control_plane.cli import main
            from control_plane.contracts.merge_train_controller_state import build_merge_train_controller_key
            from control_plane.merge_train_scheduler import MergeTrainScheduledTargetResult
            from control_plane.merge_train_wake import (
                MergeTrainEventReceipt, MergeTrainTargetReceipt, MergeTrainWake,
            )

            key = build_merge_train_controller_key(repository="example/repo", base_branch="main")
            skipped = build_merge_train_controller_key(repository="example/skipped", base_branch="main")
            receipt = MergeTrainEventReceipt(
                delivery_id="completion", request_trace_id="receiver-trace",
                received_at="2026-10-19T09:00:00Z", event="check_run", action="completed",
            )
            wake = MergeTrainWake(
                frozenset({key, skipped}), "event_hints",
                receipts=tuple(MergeTrainTargetReceipt(key, replace(receipt, delivery_id=f"delivery-{index}"))
                               for index in range(63)) +
                         (MergeTrainTargetReceipt(skipped, replace(receipt, request_trace_id="skipped-request")),),
                pass_started_at="2026-10-19T09:00:02Z",
                hint_observed_at="2026-10-19T09:00:01Z",
            )
            def run_loop(**kwargs):
                row = MergeTrainScheduledTargetResult(
                    repository="example/repo", base_branch="main", runner_mode="controller",
                    mutate=False, status="ran", trace_id="action-trace",
                    records={"candidate_id": "candidate-record"},
                )
                continuation = replace(row, trace_id="continuation-trace",
                                       records={"landing_id": "landing-record"})
                refused = replace(row, repository="example/refused", status="deferred",
                                  reason_code="backoff_pending", trace_id="", records={})
                fleet = tuple(replace(row, repository=f"example/product-{index}",
                                      trace_id=f"fleet-trace-{index}",
                                      last_controller_result={"runtime_body": "withheld-runtime-body"})
                              for index in range(32))
                kwargs["pass_callback"]((row, continuation, refused, *fleet), wake)
                kwargs["pass_callback"]((), replace(wake, pass_error_code="OSError",
                                                  pass_started_at="2026-10-19T09:05:02Z"))
                logging.getLogger("unrelated_library").info("unrelated INFO")
                return 2

            with (
                patch("control_plane.cli_service._store"),
                patch("control_plane.cli_service.Thread"),
                patch("control_plane.cli_service.MergeTrainEventListener"),
                patch("control_plane.cli_service.run_merge_train_scheduler_loop", run_loop),
            ):
                main(["service", "merge-train-workers", "run", "--database-url",
                      "postgresql://unused/test"], standalone_mode=False)
        """)
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, check=False, timeout=30
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"status": "stopped"})
        diagnostics = [
            json.loads(line) for line in result.stderr.splitlines() if line.startswith("{")
        ]
        passes = [item for item in diagnostics if "counts" in item]
        observation, failed = passes
        self.assertEqual(observation["counts"], {"results": 35, "targets": 2, "receipts": 64})
        self.assertEqual(failed["counts"], {"results": 0, "targets": 2, "receipts": 64})
        started_at = observation["started_at"]
        self.assertTrue(all(item["event"] == "merge_train_pass" for item in diagnostics))
        self.assertEqual(
            {item["started_at"] for item in diagnostics},
            {started_at, failed["started_at"]},
        )
        failed_index = diagnostics.index(failed)
        for item in diagnostics[:failed_index]:
            self.assertEqual(item["started_at"], started_at)
        for item in diagnostics[failed_index:]:
            self.assertEqual(item["started_at"], failed["started_at"])
        for item in diagnostics:
            self.assertLessEqual(len(item.get("results", [])), 1)
            self.assertLessEqual(len(item.get("wake", {}).get("receipts", [])), 1)
            self.assertLessEqual(len(item.get("wake", {}).get("targets", [])), 1)
        bindings = [binding for item in diagnostics for binding in item.get("results", [])]
        self.assertEqual(len(bindings), observation["counts"]["results"])
        self.assertEqual(
            [item["trace_id"] for item in bindings[:3]], ["action-trace", "continuation-trace", ""]
        )
        self.assertEqual(bindings[0]["records"], {"candidate_id": "candidate-record"})
        self.assertEqual(bindings[1]["records"], {"landing_id": "landing-record"})
        self.assertEqual(bindings[2]["status"], "deferred")
        self.assertEqual(bindings[2]["reason_code"], "backoff_pending")
        self.assertEqual(
            {item["repository"] for item in bindings[3:]},
            {f"example/product-{index}" for index in range(32)},
        )
        for binding in bindings:
            self.assertEqual(
                binding["routing_key"],
                build_merge_train_controller_key(
                    repository=binding["repository"], base_branch=binding["base_branch"]
                ),
            )
        receipts = [
            receipt for item in diagnostics for receipt in item.get("wake", {}).get("receipts", [])
        ]
        self.assertEqual(
            len(receipts), observation["counts"]["receipts"] + failed["counts"]["receipts"]
        )
        result_keys = {item["routing_key"] for item in bindings}
        self.assertEqual(
            [
                item["receipt"]["request_trace_id"]
                for item in receipts
                if item["routing_key"] not in result_keys
            ],
            ["skipped-request"] * 2,
        )
        target_keys = [
            key for item in diagnostics for key in item.get("wake", {}).get("targets", [])
        ]
        self.assertEqual(
            len(target_keys), observation["counts"]["targets"] + failed["counts"]["targets"]
        )
        self.assertEqual(
            set(target_keys),
            {
                build_merge_train_controller_key(repository=repository, base_branch="main")
                for repository in ("example/repo", "example/skipped")
            },
        )
        self.assertEqual(failed["wake"]["error_code"], "OSError")
        self.assertNotIn("withheld-runtime-body", result.stdout + result.stderr)
        self.assertNotIn("unrelated INFO", result.stderr)

    def test_worker_commands_emit_short_pass_tallies_to_stderr(self) -> None:
        script = textwrap.dedent("""
            import logging
            import sys
            from unittest.mock import patch
            from control_plane.cli import main
            from control_plane.github_request_timing import (
                github_request_tally, record_github_response, timed_github_request,
            )

            def run_pass(**kwargs):
                with github_request_tally("scheduled controller example/repo@main trace test"):
                    for status in (200, 304):
                        with timed_github_request(method="GET", path="/test"):
                            record_github_response(status)
                logging.getLogger("unrelated_library").info("unrelated INFO")
                return ()

            command = sys.argv[1]
            with (
                patch("control_plane.cli_service._store"),
                patch("control_plane.cli_service.Thread"),
                patch("control_plane.cli_service.MergeTrainEventListener"),
                patch("control_plane.cli_service.run_merge_train_scheduler_pass", run_pass),
                patch("control_plane.cli_service.run_merge_train_scheduler_loop", run_pass),
            ):
                main(["service", "merge-train-workers", command,
                      "--database-url", "postgresql://unused/test"], standalone_mode=False)
        """)
        for command in ("run", "run-once"):
            with self.subTest(command=command):
                result = subprocess.run(
                    [sys.executable, "-c", script, command],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIsInstance(json.loads(result.stdout), (dict, list))
                counts = _tally_counts(result.stderr, "example/repo@main trace test")
                self.assertEqual(counts["requests"], 2)
                self.assertEqual(counts["not_modified"], 1)
                self.assertEqual(counts["non_304_requests"], 1)
                self.assertNotIn("unrelated INFO", result.stderr)

    def test_web_startup_emits_tallies_through_existing_handler(self) -> None:
        script = textwrap.dedent("""
            import logging
            from pathlib import Path
            from unittest.mock import patch
            from control_plane.service_bootstrap import serve_launchplane_service
            from control_plane.github_request_timing import github_request_tally

            # A host may configure logging before starting the service.
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("host: %(message)s"))
            logging.getLogger().addHandler(handler)

            def serve(*args, **kwargs):
                with github_request_tally("controller example/repo@main trace web"):
                    pass

            with (
                patch("control_plane.service_bootstrap.load_authz_policy"),
                patch("control_plane.service_bootstrap.GitHubOidcVerifier"),
                patch("control_plane.service_bootstrap.build_shared_record_store"),
                patch("control_plane.service_bootstrap.create_launchplane_service_application"),
                patch("control_plane.service_bootstrap.native_routes._validate_native_descriptor_driver_routes"),
                patch("control_plane.service_bootstrap.native_routes._validate_native_fastapi_driver_routes"),
                patch("control_plane.service_bootstrap.register_thread_dump_signal"),
                patch("control_plane.service_bootstrap.uvicorn.run", serve),
            ):
                serve_launchplane_service(state_dir=Path("state"), policy_file=Path("unused"),
                                         host="127.0.0.1", port=0, audience="test")
        """)
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stderr.startswith("host: "), result.stderr)
        counts = _tally_counts(result.stderr, "trace web")
        self.assertEqual(counts["requests"], 0)
        self.assertEqual(counts["not_modified"], 0)

    def test_logs_a_slow_request_without_its_query(self) -> None:
        clock = _Clock()
        with self.assertLogs("control_plane.github_request_timing", level="WARNING") as logs:
            with timed_github_request(
                method="get", path="/repos/example/repo/pulls?page=2", clock=clock
            ):
                clock.now = 7.5
        self.assertEqual(
            logs.output,
            [
                "WARNING:control_plane.github_request_timing:"
                "Slow GitHub API request: GET /repos/example/repo/pulls took 7.5s"
            ],
        )

    def test_fast_request_is_silent(self) -> None:
        clock = _Clock()
        with self.assertNoLogs("control_plane.github_request_timing", level="WARNING"):
            with timed_github_request(method="GET", path="/rate_limit", clock=clock):
                clock.now = 0.2

    def test_long_operation_reports_how_its_github_time_split(self) -> None:
        clock = _Clock()
        with self.assertLogs("control_plane.github_request_timing", level="WARNING") as logs:
            with github_request_tally("controller example/repo@main", clock=clock):
                for path, finish in (("/a", 1.0), ("/b", 4.0), ("/c", 4.5)):
                    with timed_github_request(method="GET", path=path, clock=clock):
                        clock.now = finish
                clock.now = 90.0
        self.assertEqual(
            logs.output,
            [
                "WARNING:control_plane.github_request_timing:"
                "Slow operation: controller example/repo@main took 90.0s; "
                "3 GitHub requests took 4.5s, slowest GET /b at 3.0s"
            ],
        )

    def test_request_outside_a_tally_does_not_leak_into_the_next_one(self) -> None:
        clock = _Clock()
        with github_request_tally("first", clock=clock):
            pass
        with self.assertNoLogs("control_plane.github_request_timing", level="WARNING"):
            with timed_github_request(method="GET", path="/outside", clock=clock):
                clock.now = 1.0


class ThreadDumpSignalTests(unittest.TestCase):
    @unittest.skipUnless(hasattr(signal, "SIGUSR1"), "SIGUSR1 is POSIX-only")
    def test_sigusr1_dumps_every_thread_without_stopping_the_service(self) -> None:
        with patch("faulthandler.register") as register:
            service_bootstrap.register_thread_dump_signal()
        register.assert_called_once_with(signal.SIGUSR1, all_threads=True, chain=False)


if __name__ == "__main__":
    unittest.main()
