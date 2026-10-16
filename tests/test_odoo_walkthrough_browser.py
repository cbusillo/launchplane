from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from control_plane.contracts.odoo_walkthrough import WalkthroughStep
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.odoo_walkthrough_browser import run_walkthrough_browser


class OpaqueBrowserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.playwright = MagicMock()
        self.browser = MagicMock()
        self.context = self.browser.new_context.return_value
        self.page = self.context.new_page.return_value
        self.page.url = "https://testing.example/web"
        self.page.locator.return_value.count.return_value = 1
        self.page.locator.return_value.evaluate.return_value = True
        self.page.screenshot.return_value = b"\x89PNG\r\n\x1a\nfixture"
        self.context.request.post.return_value.status = 200
        self.context.request.post.return_value.json.return_value = {"result": {"uid": 17}}
        self.identity = RuntimeIdentity(
            product="example-odoo",
            context="example",
            instance="testing",
            deployment_record_id="deployment-fixture",
            artifact_id="artifact-fixture",
            source_git_ref="b" * 40,
        )
        self.context.request.get.return_value.status = 200
        self.context.request.get.return_value.json.return_value = {
            "runtime_identity": self.identity.model_dump(mode="json")
        }
        self.stack = patch("control_plane.odoo_walkthrough_browser.sync_playwright")
        self.addCleanup(self.stack.stop)
        self.stack.start().return_value.__enter__.return_value = self.playwright
        self.launch = patch(
            "control_plane.odoo_walkthrough_browser.launch_testing_browser",
            return_value=self.browser,
        )
        self.addCleanup(self.launch.stop)
        self.launch.start()
        self.checkpoint = MagicMock()

    def run_browser(self, *steps: WalkthroughStep) -> tuple[int, tuple[str, ...]]:
        return run_walkthrough_browser(
            origin="https://testing.example",
            database="testing",
            login="task-user",
            password="private-fixture-password",
            account_id=17,
            identity=self.identity,
            steps=steps,
            checkpoint=self.checkpoint,
        )

    def test_opaque_login_masks_inputs_and_password_and_exports_only_png(self) -> None:
        passed, screenshots = self.run_browser(WalkthroughStep(action="screenshot"))
        self.assertEqual(passed, 1)
        self.assertTrue(screenshots)
        self.page.locator.return_value.fill.assert_not_called()
        arguments = self.context.request.post.call_args.kwargs
        self.assertEqual(arguments["max_redirects"], 0)
        self.assertEqual(arguments["data"]["params"]["password"], "private-fixture-password")
        self.page.screenshot.assert_called_once()
        self.assertTrue(self.page.screenshot.call_args.kwargs["mask"])
        self.context.storage_state.assert_not_called()
        self.context.tracing.start.assert_not_called()
        self.context.close.assert_called_once()
        self.browser.close.assert_called_once()

    def test_failed_or_wrong_account_login_has_no_screenshot_or_page(self) -> None:
        self.context.request.post.return_value.json.return_value = {"result": {"uid": 18}}
        with self.assertRaises(ValueError):
            self.run_browser(WalkthroughStep(action="screenshot"))
        self.context.new_page.assert_not_called()
        self.page.screenshot.assert_not_called()
        self.context.request.post.return_value.dispose.assert_called_once()
        self.context.close.assert_called_once()
        self.browser.close.assert_called_once()

    def test_sensitive_controls_and_changed_identity_are_refused_before_interaction(self) -> None:
        self.page.locator.return_value.evaluate.return_value = False
        with self.assertRaises(ValueError):
            self.run_browser(WalkthroughStep(action="fill", selector="input", value="TEST value"))
        self.page.locator.return_value.fill.assert_not_called()
        self.checkpoint.side_effect = RuntimeError("Testing lane changed")
        with self.assertRaises(RuntimeError):
            self.run_browser(WalkthroughStep(action="click", selector="button"))
        self.page.locator.return_value.click.assert_not_called()

    def test_production_origin_refuses_before_password_is_sent(self) -> None:
        production = self.identity.model_copy(update={"instance": "prod"})
        self.context.request.get.return_value.json.return_value = {
            "runtime_identity": production.model_dump(mode="json")
        }
        with self.assertRaises(ValueError):
            self.run_browser()
        self.context.request.post.assert_not_called()
        self.context.close.assert_called_once()

    def test_external_requests_websockets_and_login_pages_are_blocked(self) -> None:
        self.run_browser()
        handle = self.context.route.call_args.args[1]
        for url in (
            "https://production.example/web",
            "https://external.example/collect",
            "https://testing.example/web/login",
        ):
            route = MagicMock()
            route.request.url = url
            handle(route)
            route.abort.assert_called_once()
            route.continue_.assert_not_called()
        route = MagicMock()
        route.request.url = "https://testing.example/web/assets/client.js"
        handle(route)
        route.continue_.assert_called_once()
        socket = MagicMock()
        self.context.route_web_socket.call_args.args[1](socket)
        socket.close.assert_called_once()

    def test_browser_receives_only_testing_origin_and_test_data(self) -> None:
        self.run_browser(
            WalkthroughStep(action="fill", selector="input[name=name]", value="TEST return")
        )
        self.page.locator.return_value.fill.assert_called_once_with("TEST return")
        self.assertFalse(self.browser.new_context.call_args.kwargs["accept_downloads"])


if __name__ == "__main__":
    unittest.main()
