"""Exercise the real verifier with finite synchronous request/response fixtures."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import patch
from urllib.error import URLError

import click

from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.promotion_record import DeploymentEvidence, PostDeployUpdateEvidence
from control_plane.provider_operations import ProviderMutationUnknownError
from control_plane.workflows.generic_web_deploy import (
    GenericWebPostDeployGuard,
    _run_post_deploy_extension,
)
from control_plane.workflows.generic_web_execution import ExecutionRecorder
from control_plane.workflows.verireel_app_maintenance import (
    VeriReelAppMaintenanceRequest,
    _request_internal_smoke_maintenance_api,
)
from control_plane.workflows import verireel_testing_post_deploy as verification
from tests.test_generic_web_deploy import _GenericWebDeployStore, _profile
from tests.test_verireel_testing_post_deploy import context


class _Response:
    def __init__(self, page: "_Page", method: str, path: str) -> None:
        self.page = page
        self.url = "https://video.test" + path
        self.status = 200
        self.request = _Request(self, method)


class _Request:
    def __init__(self, response: _Response, method: str) -> None:
        self.url = response.url
        self.method = method
        self.headers = {"next-action": "fixture"}
        self._response = response

    def response(self) -> _Response:
        return self._response


class _Locator:
    def __init__(self, page: "_Page", role: str, label: Any = "") -> None:
        self.page, self.role, self.label = page, role, label
        self.first = self.last = self

    def fill(self, _value: str) -> None:
        if self.page.browser.failure == "disconnected":
            raise OSError("Browser transport disconnected.")
        if self.page.browser.failure == "fill":
            raise RuntimeError("local input unavailable")
        if (
            self.page.browser.failure == "signup-refresh"
            and self.label == "Create a strong password"
        ) or (
            self.page.browser.failure in {"qr-refresh", "refresh-unknown"}
            and self.label == "Destination URL"
        ):
            self.page.emit("GET", "/dashboard?_rsc=fixture")

    def click(self) -> None:
        self.page.require_drained_requests()
        name = str(self.label)
        if "create account" in name:
            if self.page.browser.failure == "signup-click":
                raise TimeoutError("signup button cannot be clicked before dispatch")
            self.page.emit("POST", "/api/auth/sign-up/email")
            self.page.url = "https://video.test/billing"
            self.page.emit("GET", "/billing")
            self.page.emit("GET", "/billing")
        else:
            if self.page.browser.failure == "qr-click":
                raise TimeoutError("QR button cannot be clicked before dispatch")
            self.page.emit("POST", "/dashboard")

    def get_attribute(self, _name: str) -> str:
        return "" if self.page.browser.failure == "qr" else "/v/fixture"


class _Page:
    def __init__(self, browser: "_Browser") -> None:
        self.browser = browser
        self.url = "https://video.test"
        self.listeners: dict[str, list[Callable[[Any], None]]] = {}
        self.expected: tuple[Callable[[Any], bool], Any] | None = None

    def on(self, event: str, callback: Callable[[Any], None]) -> None:
        self.listeners.setdefault(event, []).append(callback)

    def is_closed(self) -> bool:
        return self.browser.failure == "disconnected"

    def event(self, event: str, value: Any) -> None:
        for callback in self.listeners.get(event, []):
            callback(value)

    def wait_for_event(self, event: str, *, predicate: Callable[[Any], bool], timeout: int) -> None:
        if timeout <= 0:
            raise AssertionError("Request completion needs a bounded built-in wait.")
        self.browser.events.append("bounded-body-wait")
        for request in self.browser.requests:
            if (
                self.browser.failure == "billing-pending"
                and request.url.endswith("/billing")
                or ("?_rsc=" in request.url and self.browser.failure != "refresh-unknown")
            ) and predicate(request):
                self.browser.finished.add(request)
                self.event("requestfinished", request)
                return
        raise TimeoutError("response headers arrived without body completion")

    def emit(self, method: str, path: str) -> _Response:
        response = _Response(self, method, path)
        request = response.request
        self.browser.requests.append(request)
        self.event("request", request)
        self.event("response", response)
        if path.endswith("/api/auth/sign-up/email") and self.browser.failure in {
            "signup",
            "body_pending",
        }:
            if self.browser.failure == "signup":
                self.event("requestfailed", request)
        elif path == "/billing" and self.browser.failure == "billing-pending":
            pass
        elif "?_rsc=" in path:
            pass
        else:
            self.browser.finished.add(request)
            self.browser.events.append("finished:" + response.url)
            self.event("requestfinished", request)
        if self.expected is not None and self.expected[0](response):
            self.expected[1].value = response
        return response

    def goto(self, url: str, **_kwargs: Any) -> _Response:
        self.require_drained_requests()
        path = url.removeprefix("https://video.test")
        if self.browser.failure == "initial-refusal":
            raise OSError("Connection refused before receiving a response.")
        self.url = (
            url
            if self.browser.admin or not path.startswith("/site-config")
            else "https://video.test/dashboard"
        )
        response = self.emit("GET", path)
        if self.browser.failure == "redirect-refusal":
            response.status = 302
            raise OSError("Connection refused after following the original response redirect.")
        if self.browser.failure == "gateway-error":
            response.status = 503
        if self.browser.failure == "navigation-refresh" and path == "/site-config":
            self.emit("GET", "/dashboard?_rsc=fixture")
        return response

    def require_drained_requests(self) -> None:
        for request in self.browser.requests:
            if "?_rsc=" in request.url and request not in self.browser.finished:
                self.event("requestfailed", request)
                raise OSError("Navigation or submission abandoned an outstanding product request.")

    def set_default_timeout(self, _value: int) -> None:
        pass

    def wait_for_load_state(self, _state: str) -> None:
        pass

    def wait_for_url(self, url: str, **_kwargs: Any) -> None:
        if self.url != url:
            raise RuntimeError("navigation response missing")

    def get_by_label(self, label: str) -> _Locator:
        return _Locator(self, "input", label)

    get_by_placeholder = get_by_label

    def get_by_role(self, role: str, *, name: Any, **_kwargs: Any) -> _Locator:
        return _Locator(self, role, name)

    def get_by_text(self, text: str, **_kwargs: Any) -> _Locator:
        return _Locator(self, "text", text)

    def locator(self, selector: str) -> _Locator:
        return _Locator(self, "link", selector)

    @contextmanager
    def expect_response(self, predicate: Callable[[Any], bool]) -> Iterator[Any]:
        result = SimpleNamespace(value=None)
        self.expected = predicate, result
        try:
            yield result
            if result.value is None:
                raise TimeoutError("initiating response missing")
        finally:
            self.expected = None


class _Browser:
    def is_connected(self) -> bool:
        return self.failure != "disconnected"

    def __init__(self, failure: str) -> None:
        self.failure = failure
        self.admin = False
        self.events: list[str] = []
        self.requests: list[_Request] = []
        self.finished: set[_Request] = set()
        self.closed = False

    def new_context(self) -> Any:
        return nullcontext(SimpleNamespace(new_page=lambda: _Page(self)))

    def close(self) -> None:
        self.closed = True


class ProductExecutionTests(unittest.TestCase):
    def verify(self, failure: str) -> tuple[ExecutionRecorder, _Browser, list[str]]:
        captured = context()
        records = _GenericWebDeployStore(_profile())
        records.write_deployment_record(
            DeploymentRecord(
                record_id=captured.deployment_record_id,
                context=captured.context,
                instance="testing",
                source_git_ref=captured.source_git_ref,
                deploy=DeploymentEvidence(
                    target_name="testing",
                    target_type="application",
                    deploy_mode="application",
                    status="pass",
                ),
            )
        )
        recorder = ExecutionRecorder(records, captured.deployment_record_id)
        guard = GenericWebPostDeployGuard(execution_recorder=recorder)
        browser = _Browser(failure)
        actions: list[str] = []
        self.recorder, self.browser, self.actions = recorder, browser, actions

        def maintenance(
            _root: Path,
            _context: Any,
            actual_guard: GenericWebPostDeployGuard,
            *,
            action: str,
            **_kwargs: Any,
        ) -> None:
            actions.append(action)
            browser.events.append("maintenance:" + action)
            if action == "grant-sponsored":
                self.billing_at_grant = [
                    request in browser.finished
                    for request in browser.requests
                    if request.url.endswith("/billing")
                ]
            if failure == "maintenance" and action == "grant-sponsored":
                with patch(
                    "control_plane.workflows.verireel_app_maintenance.urlopen",
                    side_effect=URLError("reply lost"),
                ):
                    _request_internal_smoke_maintenance_api(
                        base_url="https://video.test",
                        secret="fixture",
                        request=VeriReelAppMaintenanceRequest(
                            action="grant-sponsored",
                            intent="stable-testing-remote-e2e-grant-sponsored",
                            email="fixture@example.com",
                        ),
                        execution_guard=actual_guard,
                    )
            if action == "promote-owner":
                browser.admin = True

        def expect(locator: _Locator) -> Any:
            def visible(**_kwargs: Any) -> None:
                if failure == "assertion" and locator.role == "heading":
                    raise AssertionError("answered page has unexpected presentation")
                if failure == "public-refresh" and locator.role == "link":
                    locator.page.emit("GET", "/v/fixture?_rsc=fixture")

            return SimpleNamespace(to_be_visible=visible)

        def executor(
            root: Path, _records: Any, current: Any, current_guard: GenericWebPostDeployGuard
        ) -> PostDeployUpdateEvidence:
            evidence: dict[str, str] = {}
            verification.verify_verireel_testing_product(
                root, current, current_guard, "https://video.test", evidence
            )
            return PostDeployUpdateEvidence(attempted=True, status="pass", evidence=evidence)

        with (
            patch.object(
                verification,
                "sync_playwright",
                return_value=nullcontext(SimpleNamespace(chromium=None)),
            ),
            patch.object(verification, "launch_testing_browser", return_value=browser),
            patch.object(verification, "assert_verireel_testing_identity"),
            patch.object(verification, "_maintenance", side_effect=maintenance),
            patch.object(verification, "expect", side_effect=expect),
        ):
            _run_post_deploy_extension(
                control_plane_root=Path("."),
                record_store=records,
                context=captured,
                post_deploy_executor=executor,
                guard=guard,
            )
        return recorder, browser, actions

    def test_real_verifier_finishes_dynamic_refresh_and_action_responses(self) -> None:
        for failure in (
            "",
            "billing-pending",
            "signup-refresh",
            "qr-refresh",
            "navigation-refresh",
            "public-refresh",
        ):
            with self.subTest(failure=failure):
                recorder, browser, actions = self.verify(failure)
                self.assertEqual(recorder.receipt.entries[0].terminal, "executor_completed")
                self.assertTrue(browser.closed)
                self.assertIn("delete-user", actions)
                self.assertTrue(self.billing_at_grant)
                self.assertTrue(all(self.billing_at_grant))

    def test_lost_signup_or_maintenance_reply_skips_data_cleanup(self) -> None:
        for failure in (
            "signup",
            "body_pending",
            "maintenance",
            "qr",
            "disconnected",
            "refresh-unknown",
            "initial-refusal",
            "redirect-refusal",
            "gateway-error",
        ):
            with self.subTest(failure=failure):
                with self.assertRaises(ProviderMutationUnknownError):
                    self.verify(failure)
                self.assertNotIn("delete-user", self.actions)
                self.assertEqual(self.recorder.receipt.entries[0].terminal, "")
                self.assertTrue(self.browser.closed)

    def test_local_or_answered_assertion_failure_keeps_terminal_proof(self) -> None:
        for failure in ("fill", "assertion", "signup-click", "qr-click"):
            with self.subTest(failure=failure):
                with self.assertRaises(click.ClickException):
                    self.verify(failure)
                self.assertIn("delete-user", self.actions)
                self.assertEqual(self.recorder.receipt.entries[0].terminal, "executor_completed")
