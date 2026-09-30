import signal
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
        with patch.object(service_bootstrap.faulthandler, "register") as register:
            service_bootstrap.register_thread_dump_signal()
        register.assert_called_once_with(signal.SIGUSR1, all_threads=True, chain=False)


if __name__ == "__main__":
    unittest.main()
