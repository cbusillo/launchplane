"""Exercise the real verifier with finite synchronous request/response fixtures."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError
from urllib.parse import urlparse

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
        self.headers: dict[str, str] = {}
        self.request = _Request(self, method)

    def body(self) -> bytes:
        if (
            self.page.browser.failure == "qr-truncated"
            and self.request.method == "POST"
            and self.url.endswith("/dashboard")
        ):
            raise OSError("partial body")
        return b"fixture completed"


class _Request:
    def __init__(self, response: _Response, method: str) -> None:
        self.url = response.url
        self.method = method
        self.resource_type = "fetch" if method == "POST" or "?_rsc=" in self.url else "document"
        self.headers = (
            {"next-action": "fixture"}
            if method == "POST" and self.url.endswith("/dashboard")
            else {}
        )
        self._response = response
        self.redirected_from: _Request | None = None
        self.failure: str | None = None

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
        self.closed = False
        self.context = browser.current_context
        browser.pages.append(self)

    def on(self, event: str, callback: Callable[[Any], None]) -> None:
        self.listeners.setdefault(event, []).append(callback)

    def is_closed(self) -> bool:
        return self.closed or self.browser.failure == "disconnected"

    def close(self) -> None:
        if self.browser.failure == "close":
            raise OSError("Browser connection lost while closing the page")
        if self.browser.failure in {"read-at-close", "mutation-at-close"}:
            self.emit(
                "POST" if self.browser.failure == "mutation-at-close" else "GET",
                "/billing?_rsc=fixture",
            )
        self.closed = True

    def event(self, event: str, value: Any) -> None:
        if event == "requestfailed":
            self.browser.failed.add(value)
        for callback in self.listeners.get(event, []):
            callback(value)

    def emit(self, method: str, path: str) -> _Response:
        response = _Response(self, method, path)
        if (
            path == "/site-config"
            and self.browser.failure == "signin-redirect"
            and not self.browser.admin
        ):
            response.status = 307
            response.headers = {"location": "/sign-in"}
        request = response.request
        if path == "/billing" and self.browser.failure == "billing-action-aborted":
            request.headers["next-action"] = "fixture"
        self.browser.requests.append(request)
        self.event("request", request)
        self.event("response", response)
        route = _Route(self, response)
        if self.context.handler and self.context.predicate(request.url):
            self.context.handler(route)
        else:
            route.fetch()
            route.fulfill(response=response)
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
        if (
            self.browser.failure == "signin-redirect"
            and path == "/site-config"
            and not self.browser.admin
        ):
            self.url = "https://video.test/sign-in"
            final = _Response(self, "GET", "/sign-in")
            final.request.redirected_from = response.request
            self.browser.requests.append(final.request)
            self.browser.effects.append(final.request)
            self.browser.upstream_completed.add(final.request)
            self.event("request", final.request)
            self.event("response", final)
            self.event("requestfinished", final.request)
            return final
        if self.browser.failure == "redirect-refusal":
            raise OSError("Connection refused after following the original response redirect.")
        if self.browser.failure == "gateway-error":
            response.status = 503
        if self.browser.failure == "navigation-refresh" and path == "/site-config":
            self.emit("GET", "/dashboard?_rsc=fixture")
        return response

    def require_drained_requests(self) -> None:
        for request in self.browser.requests:
            if (
                request._response.page is self
                and request not in self.browser.upstream_completed
                and request.failure is None
            ):
                raise OSError("Navigation abandoned an outstanding product request.")

    def wait_for_timeout(self, _milliseconds: float) -> None:
        pass

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


class _Route:
    def __init__(self, page: _Page, response: _Response) -> None:
        self.page = page
        self.response = response
        self.request = response.request

    def fetch(
        self, *, max_redirects: int = 20, max_retries: int = 0, timeout: float = 30_000
    ) -> _Response:
        browser = self.page.browser
        browser.effects.append(self.request)
        path = urlparse(self.request.url).path
        affected = (
            path == "/api/auth/sign-up/email"
            and browser.failure in {"signup", "body_pending"}
            or "?_rsc=" in self.request.url
            and browser.failure == "refresh-unknown"
            or path == "/dashboard"
            and self.request.method == "POST"
            and browser.failure in {"qr-upstream-lost", "qr-http-error", "qr-redirect"}
            or path.startswith("/v/")
            and browser.failure == "public-upstream-lost"
        )
        if affected:
            if max_retries:
                browser.effects.extend([self.request] * max_retries)
            if browser.failure == "qr-redirect":
                if max_redirects:
                    browser.effects.append(self.request)
                self.response.status = 307
            elif browser.failure == "qr-http-error":
                self.response.status = 500
            else:
                raise TimeoutError("private-transport-detail")
        if browser.failure == "gateway-error":
            self.response.status = 503
        browser.upstream_completed.add(self.request)
        return self.response

    def fulfill(self, *, response: _Response) -> None:
        browser = self.page.browser
        browser.delivered.append(self.request)
        self.page.event("response", response)
        path = urlparse(self.request.url).path
        aborted = (
            path == "/billing"
            and browser.failure
            in {"billing-aborted", "billing-aborted-in-wait", "billing-action-aborted"}
            or path == "/dashboard"
            and self.request.method == "POST"
            and browser.failure == "qr-aborted"
            or path.startswith("/v/")
            and browser.failure == "public-aborted"
            or path == "/api/auth/sign-up/email"
            and browser.failure == "signup-buffered"
        )
        if aborted:
            self.request.failure = "net::ERR_ABORTED"
            self.page.event("requestfailed", self.request)
        else:
            browser.finished.add(self.request)
            self.page.event("requestfinished", self.request)
        if self.page.expected is not None and self.page.expected[0](response):
            self.page.expected[1].value = response

    def abort(self) -> None:
        self.request.failure = "net::ERR_ABORTED"
        self.page.event("requestfailed", self.request)


class _Context:
    def __init__(self, browser: "_Browser") -> None:
        self.browser = browser
        self.handler: Callable[[Any], None] | None = None
        self.predicate: Callable[[str], bool] = lambda _url: False

    def new_page(self) -> _Page:
        return _Page(self.browser)

    def route(self, predicate: Callable[[str], bool], handler: Callable[[Any], None]) -> None:
        self.predicate, self.handler = predicate, handler

    def unroute_all(self, *, behavior: str) -> None:
        self.handler = None


class _Browser:
    def is_connected(self) -> bool:
        return self.failure != "disconnected"

    def __init__(self, failure: str) -> None:
        self.failure = failure
        self.admin = False
        self.events: list[str] = []
        self.requests: list[_Request] = []
        self.finished: set[_Request] = set()
        self.failed: set[_Request] = set()
        self.pages: list[_Page] = []
        self.closed = False
        self.effects: list[_Request] = []
        self.delivered: list[_Request] = []
        self.upstream_completed: set[_Request] = set()
        self.current_context = _Context(self)

    @contextmanager
    def new_context(self, **_kwargs: Any) -> Iterator[_Context]:
        self.current_context = _Context(self)
        yield self.current_context

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
                    request in browser.upstream_completed
                    for request in browser.requests
                    if urlparse(request.url).path == "/billing"
                ]
            if action == "delete-user" and failure == "late-read":
                for page in browser.pages:
                    if not page.is_closed():
                        page.emit("GET", "/billing?_rsc=fixture")
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
                if locator.role == "heading" and (
                    failure == "assertion"
                    and "/site-config" in locator.page.url
                    or failure == "billing-assertion"
                    and urlparse(locator.page.url).path == "/billing"
                ):
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
            "signup-buffered",
            "billing-pending",
            "billing-aborted",
            "billing-aborted-in-wait",
            "late-read",
            "read-at-close",
            "signup-refresh",
            "qr-refresh",
            "navigation-refresh",
            "public-refresh",
            "billing-action-aborted",
            "qr-aborted",
            "public-aborted",
            "mutation-at-close",
        ):
            with self.subTest(failure=failure):
                recorder, browser, actions = self.verify(failure)
                self.assertEqual(recorder.receipt.entries[0].terminal, "executor_completed")
                self.assertTrue(browser.closed)
                self.assertIn("delete-user", actions)
                self.assertTrue(self.billing_at_grant)
                self.assertTrue(all(self.billing_at_grant))
                for request in browser.requests:
                    self.assertLessEqual(browser.effects.count(request), 1)
                if failure == "mutation-at-close":
                    late = [
                        request
                        for request in browser.requests
                        if request.method == "POST" and "?_rsc=" in request.url
                    ]
                    self.assertTrue(late)
                    self.assertTrue(all(request not in browser.effects for request in late))

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
            "qr-upstream-lost",
            "public-upstream-lost",
            "qr-http-error",
            "qr-truncated",
            "qr-redirect",
            "close",
        ):
            with self.subTest(failure=failure):
                with self.assertRaises(ProviderMutationUnknownError):
                    self.verify(failure)
                self.assertNotIn("delete-user", self.actions)
                self.assertEqual(self.recorder.receipt.entries[0].terminal, "")
                self.assertTrue(self.browser.closed)
                self.assertTrue(
                    all(
                        self.browser.effects.count(request) <= 1
                        for request in self.browser.requests
                    )
                )
                if failure == "qr-redirect":
                    self.assertTrue(
                        all(
                            request.method != "POST"
                            or request.url.endswith("/api/auth/sign-up/email")
                            for request in self.browser.delivered
                        )
                    )

    def test_unknown_signup_retains_safe_exchange_cause_without_request_values(self) -> None:
        with (
            self.assertLogs(verification.__name__, level="WARNING") as logs,
            self.assertRaises(ProviderMutationUnknownError),
        ):
            self.verify("signup")
        output = "\n".join(logs.output)
        request = next(request for request in self.browser.requests if request.method == "POST")
        for value in (
            request.method,
            request.url.removeprefix("https://video.test"),
            request.resource_type,
        ):
            self.assertIn(value, output)
        self.assertIn("status=None", output)
        self.assertNotIn("https://video.test", output)
        self.assertNotIn("next-action", output)
        self.assertNotIn(verification.fixture_email(context().deployment_record_id), output)

    def test_failed_exchange_diagnostic_omits_dynamic_paths_queries_and_transport_errors(
        self,
    ) -> None:
        browser = _Browser("public-upstream-lost")
        page: Any = _Page(browser)
        guard = GenericWebPostDeployGuard()
        exchanges: Any = verification._TestingPageExchanges(page, "https://video.test", guard)
        with self.assertLogs(verification.__name__, level="WARNING") as logs:
            page.emit("GET", "/v/private-record?_rsc=private-query")
            with self.assertRaises(ProviderMutationUnknownError):
                exchanges.finish()
        output = "\n".join(logs.output)
        self.assertIn("/v/", output)
        self.assertNotIn("private-", output)
        self.assertNotIn("?_rsc", output)
        with self.assertRaises(ProviderMutationUnknownError):
            guard.require_completed_exchanges()

    def test_one_request_receipt_cannot_complete_a_same_path_sibling(self) -> None:
        browser = _Browser("")
        page: Any = _Page(browser)
        guard = GenericWebPostDeployGuard()
        exchanges: Any = verification._TestingPageExchanges(page, "https://video.test", guard)
        first = page.emit("POST", "/dashboard")
        browser.failure = "qr-upstream-lost"
        second = page.emit("POST", "/dashboard")
        exchanges.finish_response(first)
        with self.assertRaises(ProviderMutationUnknownError):
            exchanges.finish_response(second)
        for request in browser.requests:
            self.assertEqual(browser.effects.count(request), 1)
        with self.assertRaises(ProviderMutationUnknownError):
            exchanges.finish_response(_Response(page, "POST", "/dashboard"))

    def test_native_redirect_descendant_requires_its_own_completed_body(self) -> None:
        for outcome in ("finished", "aborted", "missing", "mutation", "public"):
            with self.subTest(outcome=outcome):
                browser = _Browser("")
                page: Any = _Page(browser)
                guard = GenericWebPostDeployGuard()
                exchanges: Any = verification._TestingPageExchanges(
                    page, "https://video.test", guard
                )
                initial = _Response(page, "GET", "/site-config")
                initial.status = 307
                initial.headers = {"location": "/dashboard"}
                exchanges.capture(_Route(page, initial))
                final = _Response(
                    page,
                    "POST" if outcome == "mutation" else "GET",
                    "/v/fixture" if outcome == "public" else "/dashboard",
                )
                final.request.redirected_from = initial.request
                page.event("request", final.request)
                if outcome == "aborted":
                    final.request.failure = "net::ERR_ABORTED"
                elif outcome == "missing":
                    page.expect_request_finished = Mock(side_effect=TimeoutError("body missing"))
                else:
                    page.event("requestfinished", final.request)
                if outcome == "finished":
                    exchanges.finish_response(final)
                    guard.require_completed_exchanges()
                else:
                    with self.assertRaises(ProviderMutationUnknownError):
                        exchanges.finish_response(final)
                    with self.assertRaises(ProviderMutationUnknownError):
                        guard.require_completed_exchanges()

    def test_signin_read_redirect_has_its_own_receipt(self) -> None:
        browser = _Browser("")
        page: Any = _Page(browser)
        guard = GenericWebPostDeployGuard()
        exchanges: Any = verification._TestingPageExchanges(page, "https://video.test", guard)
        initial = _Response(page, "GET", "/site-config")
        initial.status = 307
        initial.headers = {"location": "/sign-in"}
        exchanges.capture(_Route(page, initial))
        final = _Response(page, "GET", "/sign-in")
        final.request.redirected_from = initial.request
        page.event("request", final.request)
        page.event("requestfinished", final.request)
        exchanges.finish_response(final)
        guard.require_completed_exchanges()

    def test_redirect_from_untracked_head_cannot_hide_a_tracked_mutation(self) -> None:
        for method, path, finished in (
            ("GET", "/dashboard", True),
            ("POST", "/dashboard", True),
            ("GET", "/v/fixture", True),
            ("GET", "/dashboard", False),
        ):
            with self.subTest(method=method, path=path, finished=finished):
                browser = _Browser("")
                page: Any = _Page(browser)
                guard = GenericWebPostDeployGuard()
                exchanges: Any = verification._TestingPageExchanges(
                    page, "https://video.test", guard
                )
                initial = _Response(page, "GET", "/dashboard/")
                final = _Response(page, method, path)
                final.request.redirected_from = initial.request
                page.event("request", final.request)
                if finished:
                    page.event("requestfinished", final.request)
                else:
                    final.request.failure = "net::ERR_ABORTED"
                if method == "GET" and path == "/dashboard" and finished:
                    exchanges.finish()
                    guard.require_completed_exchanges()
                else:
                    with self.assertRaises(ProviderMutationUnknownError):
                        exchanges.finish()
                    with self.assertRaises(ProviderMutationUnknownError):
                        guard.require_completed_exchanges()

    def test_routing_a_previously_observed_redirect_keeps_one_receipt(self) -> None:
        browser = _Browser("")
        page: Any = _Page(browser)
        guard = GenericWebPostDeployGuard()
        exchanges: Any = verification._TestingPageExchanges(page, "https://video.test", guard)
        initial = page.emit("GET", "/site-config")
        final = _Response(page, "GET", "/dashboard")
        final.request.redirected_from = initial.request
        page.event("request", final.request)
        exchanges.capture(_Route(page, final))
        exchanges.finish()
        guard.require_completed_exchanges()

    def test_unexpected_redirect_is_aborted_before_browser_replay(self) -> None:
        for method, path, location in (
            ("POST", "/dashboard", "/dashboard"),
            ("GET", "/v/fixture", "/dashboard"),
            ("GET", "/site-config", "https://other.test/dashboard"),
            ("GET", "/site-config", "/api/auth/sign-up/email"),
        ):
            with self.subTest(method=method, path=path, location=location):
                browser = _Browser("")
                page: Any = _Page(browser)
                guard = GenericWebPostDeployGuard()
                exchanges: Any = verification._TestingPageExchanges(
                    page, "https://video.test", guard
                )
                response = _Response(page, method, path)
                response.status = 307
                response.headers = {"location": location}
                exchanges.capture(_Route(page, response))
                self.assertFalse(browser.delivered)
                self.assertEqual(browser.effects, [response.request])
                with self.assertRaises(ProviderMutationUnknownError):
                    exchanges.finish_response(response)

    def test_failed_browser_abort_does_not_repeat_the_upstream_failure(self) -> None:
        for failure in ("status", "fetch", "closing"):
            with self.subTest(failure=failure):
                page = Mock()
                guard = GenericWebPostDeployGuard()
                exchanges = verification._TestingPageExchanges(page, "https://video.test", guard)
                request = Mock(
                    url="https://video.test/billing",
                    method="GET",
                    headers={},
                    resource_type="fetch",
                )
                route = Mock(request=request)
                response = route.fetch.return_value
                response.status = 500 if failure == "status" else 200
                response.headers = {}
                if failure == "fetch":
                    route.fetch.side_effect = OSError("lost upstream channel")
                elif failure == "closing":

                    def close_during_fetch(**_kwargs: Any) -> Any:
                        exchanges.closing = True
                        return response

                    route.fetch.side_effect = close_during_fetch
                route.abort.side_effect = OSError("browser already gone")
                with self.assertLogs(verification.__name__, level="WARNING") as logs:
                    exchanges.capture(route)
                route.fetch.assert_called_once()
                route.abort.assert_called_once()
                route.fulfill.assert_not_called()
                self.assertEqual(len(logs.records), 1 if failure == "closing" else 2)
                if failure == "closing":
                    exchanges.finish()
                    guard.require_completed_exchanges()
                else:
                    with self.assertRaises(ProviderMutationUnknownError):
                        exchanges.finish()

    def test_local_or_answered_assertion_failure_keeps_terminal_proof(self) -> None:
        for failure in (
            "fill",
            "assertion",
            "billing-assertion",
            "signup-click",
            "qr-click",
            "signin-redirect",
        ):
            with self.subTest(failure=failure):
                with self.assertRaises(click.ClickException):
                    self.verify(failure)
                self.assertIn("delete-user", self.actions)
                self.assertEqual(self.recorder.receipt.entries[0].terminal, "executor_completed")
