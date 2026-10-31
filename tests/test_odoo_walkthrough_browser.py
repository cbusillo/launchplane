from __future__ import annotations

import unittest
from unittest.mock import MagicMock, PropertyMock, patch
from typing import Any

from playwright.sync_api import Error as PlaywrightError

from control_plane.contracts.odoo_walkthrough import WalkthroughStep
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.odoo_walkthrough_browser import (
    ReadonlyBrowserCheckFailed,
    readonly_browser_request,
    run_walkthrough_browser,
)


class OpaqueBrowserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.playwright = MagicMock()
        self.browser = MagicMock()
        self.context = self.browser.new_context.return_value
        self.page = self.context.new_page.return_value
        self.page.url = "https://testing.example/web"
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

    def run_browser(
        self, *steps: WalkthroughStep, read_only: bool = False
    ) -> tuple[int, tuple[str, ...]]:
        return run_walkthrough_browser(
            origin="https://testing.example",
            database="testing",
            login="task-user",
            password="private-fixture-password",
            account_id=17,
            identity=self.identity,
            steps=steps,
            checkpoint=self.checkpoint,
            read_only=read_only,
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

    def test_late_rendered_controls_can_be_read_and_clicked(self) -> None:
        shell = self.page.locator.return_value
        for action in ("visible", "click"):
            with self.subTest(action=action):
                control = MagicMock()
                rendered = False

                def resolve_control(_script: str) -> bool:
                    nonlocal rendered
                    rendered = True
                    return True

                control.count.side_effect = lambda: int(rendered)
                control.evaluate.side_effect = resolve_control
                self.page.locator.side_effect = lambda selector: (
                    control if selector == ".delayed" else shell
                )
                passed, _ = self.run_browser(
                    WalkthroughStep(action=action, selector=".delayed"), read_only=True
                )
                self.assertEqual(passed, 1)
                self.assertTrue(rendered)
                if action == "click":
                    control.click.assert_called_once_with(no_wait_after=True)

    def test_signout_and_signin_use_only_the_private_task_identity(self) -> None:
        authenticated = self.context.request.post.return_value
        signed_out = MagicMock(status=200)
        signed_out.json.return_value = {"result": {"uid": False}}
        self.context.request.post.side_effect = lambda url, **_kwargs: (
            signed_out if url.endswith("/web/session/get_session_info") else authenticated
        )
        passed, screenshots = self.run_browser(
            WalkthroughStep(action="sign_out"),
            WalkthroughStep(action="sign_in"),
            WalkthroughStep(action="visible", selector=".o_web_client"),
        )
        self.assertEqual(passed, 3)
        self.assertFalse(screenshots)
        self.assertTrue(
            self.context.request.post.call_args.args[0].endswith("/web/session/authenticate")
        )
        for call in self.context.request.post.call_args_list:
            if "password" in call.kwargs["data"].get("params", {}):
                self.assertTrue(call.args[0].endswith("/web/session/authenticate"))
                self.assertEqual(call.kwargs["data"]["params"]["login"], "task-user")

    def test_signout_must_really_remove_the_authenticated_session(self) -> None:
        with self.assertRaises(ValueError):
            self.run_browser(WalkthroughStep(action="sign_out"), WalkthroughStep(action="sign_in"))
        self.context.close.assert_called_once()
        self.browser.close.assert_called_once()

    def test_readonly_transport_allows_ui_reads_and_refuses_business_calls(self) -> None:
        for path in ("/web/webclient/translations/hash", "/web/webclient/locale/en_US"):
            self.assertTrue(readonly_browser_request("GET", path, None))
        self.assertTrue(
            readonly_browser_request("POST", "/web/action/load", {"params": {"context": {}}})
        )
        for method in ("web_read", "web_search_read", "get_views", "default_get"):
            self.assertTrue(
                readonly_browser_request(
                    "POST",
                    "/web/dataset/call_kw/product.product/" + method,
                    {"params": {"model": "product.product", "method": method}},
                )
            )
        for method in ("write", "create", "unlink", "button_validate", "action_print"):
            self.assertFalse(
                readonly_browser_request(
                    "POST",
                    "/web/dataset/call_kw/product.product/read",
                    {"params": {"model": "product.product", "method": method}},
                )
            )
        for verb, path in (
            ("GET", "/report/pdf/product.label/1"),
            ("POST", "/web/dataset/call_button"),
            ("POST", "/web/dataset/call_button/stock.picking/button_validate"),
            ("POST", "/web/session/change_password"),
            ("GET", "/web/content/1?download=true"),
            ("POST", "/mail/message/post"),
        ):
            self.assertFalse(readonly_browser_request(verb, path, {}))
        self.assertFalse(readonly_browser_request("POST", "/web/dataset/call_kw", {}))
        self.assertTrue(readonly_browser_request("GET", "/web/assets/client.js", None))

    def test_preview_browser_requires_readonly_and_rejects_fill_before_signin(self) -> None:
        preview = self.identity.model_copy(
            update={
                "instance": "preview-example-12",
                "environment_kind": "preview",
                "preview_id": "preview-example",
                "preview_generation_id": "generation-example",
            }
        )
        arguments: dict[str, Any] = {
            "origin": "https://testing.example",
            "database": "preview",
            "login": "test-identity",
            "password": "private-fixture-password",
            "account_id": 17,
            "identity": preview,
            "checkpoint": self.checkpoint,
        }
        with self.assertRaises(ValueError):
            run_walkthrough_browser(**arguments, steps=())
        with self.assertRaises(ValueError):
            run_walkthrough_browser(
                **arguments,
                read_only=True,
                steps=(WalkthroughStep(action="fill", selector="input", value="TEST x"),),
            )
        self.context.request.post.assert_not_called()
        self.context.request.get.return_value.json.return_value = {
            "runtime_identity": preview.model_dump(mode="json")
        }
        run_walkthrough_browser(**arguments, read_only=True, steps=())
        self.context.close.assert_called_once()

    def test_readonly_step_failure_settles_only_after_the_browser_closes(self) -> None:
        self.page.locator.return_value.evaluate.side_effect = PlaywrightError("Control timed out")
        with self.assertRaises(ReadonlyBrowserCheckFailed) as failed:
            self.run_browser(WalkthroughStep(action="click", selector=".missing"), read_only=True)
        self.assertEqual(failed.exception.steps_passed, 0)
        self.page.locator.return_value.click.assert_not_called()
        self.context.close.assert_called_once()
        self.browser.close.assert_called_once()
        self.context.close.side_effect = RuntimeError("Unverified close")
        with self.assertRaises(RuntimeError):
            self.run_browser(WalkthroughStep(action="visible", selector=".missing"), read_only=True)

    def test_readonly_checkpoint_and_identity_failures_remain_unverified(self) -> None:
        self.checkpoint.side_effect = RuntimeError("Changed lane")
        with self.assertRaises(RuntimeError):
            self.run_browser(read_only=True)
        self.checkpoint.side_effect = None
        self.context.request.get.return_value.status = 503
        with self.assertRaises(ValueError) as failed:
            self.run_browser(read_only=True)
        self.assertNotIsInstance(failed.exception, ReadonlyBrowserCheckFailed)
        self.context.request.post.assert_not_called()

    def test_readonly_failed_step_rechecks_the_live_runtime_before_settling(self) -> None:
        healthy = self.context.request.get.return_value
        self.context.request.get.side_effect = [healthy, healthy, MagicMock(status=503)]
        self.page.locator.return_value.evaluate.side_effect = PlaywrightError("Control timed out")
        with self.assertRaises(ValueError) as failed:
            self.run_browser(WalkthroughStep(action="visible", selector=".missing"), read_only=True)
        self.assertNotIsInstance(failed.exception, ReadonlyBrowserCheckFailed)

    def test_readonly_route_aborts_malformed_rpc_and_allows_my_printers(self) -> None:
        self.run_browser(read_only=True)
        handle = self.context.route.call_args.args[1]
        route = MagicMock()
        route.request.url = "https://testing.example/web/dataset/call_kw"
        route.request.method = "POST"
        type(route.request).post_data_json = PropertyMock(side_effect=ValueError("Not JSON"))
        handle(route)
        route.abort.assert_called_once()
        route.continue_.assert_not_called()
        self.assertTrue(
            readonly_browser_request(
                "POST", "/web/dataset/call_kw", {"params": {"method": "action_my_printers"}}
            )
        )


if __name__ == "__main__":
    unittest.main()
