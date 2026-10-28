import os
import unittest
from unittest.mock import Mock, patch

import click

from control_plane.testing_browser import check_testing_browser_availability, launch_testing_browser


class TestingBrowserTests(unittest.TestCase):
    def test_remote_browser_receives_no_service_environment(self) -> None:
        chromium = Mock()
        endpoint = "ws://browser.example.test:3000/"
        with patch.dict(
            os.environ,
            {
                "LAUNCHPLANE_BROWSER_WS_ENDPOINT": endpoint,
                "LAUNCHPLANE_LOCAL_OPERATOR_TOKEN": "private-fixture",
            },
        ):
            self.assertIs(launch_testing_browser(chromium), chromium.connect.return_value)
        chromium.connect.assert_called_once_with(endpoint, timeout=30_000)
        chromium.launch.assert_not_called()

    def test_linux_requires_the_isolated_browser(self) -> None:
        chromium = Mock()
        with (
            patch.dict(os.environ, {"LAUNCHPLANE_BROWSER_WS_ENDPOINT": ""}),
            patch("control_plane.testing_browser.sys.platform", "linux"),
        ):
            with self.assertRaises(click.ClickException):
                launch_testing_browser(chromium)
        chromium.launch.assert_not_called()
        chromium.connect.assert_not_called()

    def test_local_browser_is_sandboxed_with_a_scrubbed_environment(self) -> None:
        chromium = Mock()
        with (
            patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_BROWSER_WS_ENDPOINT": "",
                    "LAUNCHPLANE_LOCAL_OPERATOR_TOKEN": "private-fixture",
                },
            ),
            patch("control_plane.testing_browser.sys.platform", "darwin"),
        ):
            launch_testing_browser(chromium)
        arguments = chromium.launch.call_args.kwargs
        self.assertTrue(arguments["chromium_sandbox"])
        self.assertNotIn("LAUNCHPLANE_LOCAL_OPERATOR_TOKEN", arguments["env"])
        self.assertTrue(arguments["env"]["PATH"])

    def test_availability_probe_closes_the_browser(self) -> None:
        with patch("control_plane.testing_browser.sync_playwright") as playwright:
            chromium = playwright.return_value.__enter__.return_value.chromium
            with patch.dict(os.environ, {"LAUNCHPLANE_BROWSER_WS_ENDPOINT": "ws://browser/"}):
                check_testing_browser_availability()
        chromium.connect.return_value.close.assert_called_once_with()
