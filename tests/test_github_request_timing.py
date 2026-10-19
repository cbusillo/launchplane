import json
import signal
import subprocess
import sys
import textwrap
import unittest
from unittest.mock import patch

from control_plane import service_bootstrap
from control_plane.github_request_timing import github_request_tally
from control_plane.github_request_timing import timed_github_request


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class GitHubRequestTimingTests(unittest.TestCase):
    def test_worker_serializes_receipts_with_traces_and_reports_a_failed_empty_pass(self) -> None:
        script = textwrap.dedent("""
            import logging
            from dataclasses import replace
            from unittest.mock import patch
            from control_plane.cli import main
            from control_plane.merge_train_scheduler import MergeTrainScheduledTargetResult
            from control_plane.merge_train_wake import (
                MergeTrainEventReceipt, MergeTrainTargetReceipt, MergeTrainWake,
            )

            key = "merge-train-controller:example:repo:main"
            receipt = MergeTrainEventReceipt(
                delivery_id="completion", request_trace_id="receiver-trace",
                received_at="2026-10-19T09:00:00Z", event="check_run", action="completed",
            )
            wake = MergeTrainWake(
                frozenset({key}), "event_hints",
                receipts=(MergeTrainTargetReceipt(key, receipt),),
                pass_started_at="2026-10-19T09:00:02Z",
                hint_observed_at="2026-10-19T09:00:01Z",
            )
            def run_loop(**kwargs):
                row = MergeTrainScheduledTargetResult(
                    repository="example/repo", base_branch="main", runner_mode="controller",
                    mutate=False, status="ran", trace_id="action-trace",
                    records={"candidate_id": "candidate-record"}, wake=wake.for_target(key),
                )
                kwargs["pass_callback"]((row,), wake)
                kwargs["pass_callback"]((), replace(wake, pass_error_code="OSError"))
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
        lines = result.stdout.splitlines()
        rows, empty = [json.loads(line) for line in lines[:2]]
        stopped = json.loads("\n".join(lines[2:]))
        self.assertEqual(rows[0]["trace_id"], "action-trace")
        self.assertEqual(rows[0]["records"], {"candidate_id": "candidate-record"})
        self.assertEqual(rows[0]["wake"]["event_receipts"][0]["delivery_id"], "completion")
        self.assertEqual(rows[0]["wake"]["event_receipts"][0]["request_trace_id"], "receiver-trace")
        self.assertEqual(empty, [])
        self.assertEqual(stopped, {"status": "stopped"})
        diagnostic = json.loads(result.stderr)
        self.assertEqual(diagnostic["wake"]["pass_error_code"], "OSError")
        self.assertEqual(diagnostic["wake"]["receipts"][0]["receipt"]["delivery_id"], "completion")
        self.assertIsInstance(diagnostic["wake"]["target_keys"], list)

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
                self.assertIn("example/repo@main trace test", result.stderr)
                self.assertIn("requests=2 not_modified=1 non_304_requests=1", result.stderr)
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
        self.assertEqual(result.stderr.count("trace web"), 1)
        self.assertIn("requests=0 not_modified=0 non_304_requests=0", result.stderr)

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
