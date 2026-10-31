"""One private browser, no exported browser handle, credentials, traces or HTML."""

import base64
from collections.abc import Callable
from urllib.parse import urlsplit

from playwright.sync_api import Locator, Page, Route, sync_playwright

from control_plane.contracts.odoo_walkthrough import WalkthroughStep
from control_plane.contracts.runtime_identity import (
    RuntimeIdentity,
    runtime_identity_from_health_payload,
)
from control_plane.odoo_runtime_reads import require_runtime_identity
from control_plane.testing_browser import launch_testing_browser


def readonly_browser_request(method: str, path: str, payload: object) -> bool:
    if method == "GET":
        return path in {"/web", "/odoo"} or path.startswith(
            ("/odoo/", "/web/assets/", "/web/static/", "/web/image/", "/web/webclient/load_menus/")
        )
    if method != "POST" or not isinstance(payload, dict):
        return False
    params = payload.get("params")
    if not isinstance(params, dict):
        return False
    if path == "/web/action/load":
        return set(params) <= {"action_id", "additional_context"}
    if not (path == "/web/dataset/call_kw" or path.startswith("/web/dataset/call_kw/")):
        return False
    return params.get("method") in {
        "read",
        "search",
        "search_count",
        "search_read",
        "read_group",
        "web_read_group",
        "web_read",
        "web_search_read",
        "name_search",
        "fields_get",
        "get_views",
        "default_get",
        "has_group",
        "has_groups",
        "check_access_rights",
    }


def require_safe_browser_control(locator: Locator) -> None:
    safe = locator.evaluate("""element =>
        element.ownerDocument.defaultView === window.top &&
        !['password', 'hidden', 'file'].includes(element.type) &&
        !element.closest('iframe') &&
        !/password|token|secret|api.?key/i.test(
            [element.name, element.id, element.autocomplete].join(' '))
    """)
    if safe is not True:
        raise ValueError("Sensitive browser controls cannot be selected.")


def run_walkthrough_browser(
    *,
    origin: str,
    database: str,
    login: str,
    password: str,
    account_id: int,
    identity: RuntimeIdentity,
    steps: tuple[WalkthroughStep, ...],
    checkpoint: Callable[[], None],
    read_only: bool = False,
) -> tuple[int, tuple[str, ...]]:
    """Credentials are consumed by a non-redirecting request, never a login form.

    Request/response text and Playwright exceptions never leave this function.
    The caller translates failures to a fixed code. Browser storage lives only
    in this short-lived context, which is closed on every exit.
    """
    expected_origin = urlsplit(origin)
    if (
        (
            identity.instance != "testing"
            and not (read_only and identity.environment_kind == "preview")
        )
        or identity.instance in {"prod", "production"}
        or identity.environment_kind == "preview"
        and not (
            identity.preview_id
            and identity.preview_generation_id
            and identity.instance != "testing"
        )
        or read_only
        and any(step.action == "fill" for step in steps)
        or expected_origin.scheme != "https"
        or expected_origin.path not in {"", "/"}
    ):
        raise ValueError("Testing browser requires its recorded HTTPS origin.")
    with sync_playwright() as playwright:
        browser = launch_testing_browser(playwright.chromium)
        try:
            context = browser.new_context(
                service_workers="block",
                accept_downloads=False,
                viewport={"width": 1280, "height": 800},
            )
            try:

                def route_request(route: Route) -> None:
                    request_url = urlsplit(route.request.url)
                    if (request_url.scheme, request_url.netloc) != (
                        expected_origin.scheme,
                        expected_origin.netloc,
                    ) or request_url.path.startswith(
                        ("/web/login", "/web/reset_password", "/web/signup")
                    ):
                        route.abort()
                    elif read_only and not readonly_browser_request(
                        route.request.method,
                        request_url.path,
                        route.request.post_data_json if route.request.post_data else None,
                    ):
                        route.abort()
                    else:
                        route.continue_()

                context.route("**/*", route_request)
                context.route_web_socket("**/*", lambda socket: socket.close())
                context.set_default_timeout(10000)

                def check_origin_identity() -> None:
                    checkpoint()
                    health = context.request.get(
                        origin + "/launchplane/health", max_redirects=0, timeout=10000
                    )
                    try:
                        if health.status != 200:
                            raise ValueError("Testing origin identity is unavailable.")
                        require_runtime_identity(
                            identity, runtime_identity_from_health_payload(health.json())
                        )
                    finally:
                        health.dispose()

                def sign_in() -> Page:
                    check_origin_identity()
                    authentication_response = context.request.post(
                        origin + "/web/session/authenticate",
                        data={
                            "jsonrpc": "2.0",
                            "method": "call",
                            "params": {
                                "db": database,
                                "login": login,
                                "password": password,
                            },
                        },
                        max_redirects=0,
                        timeout=15000,
                    )
                    try:
                        result = (
                            authentication_response.json().get("result", {})
                            if authentication_response.status == 200
                            else {}
                        )
                        if result.get("uid") != account_id:
                            raise ValueError("Testing account sign-in failed.")
                    finally:
                        authentication_response.dispose()
                    authenticated_page = context.new_page()
                    authenticated_page.goto(origin + "/web", wait_until="domcontentloaded")
                    authenticated_page.locator(".o_web_client").wait_for(state="visible")
                    return authenticated_page

                page = sign_in()
                signed_in = True
                screenshots: list[str] = []
                passed = 0
                for step in steps:
                    check_origin_identity()
                    if step.action == "sign_in":
                        if signed_in:
                            raise ValueError("The test identity is already signed in.")
                        page = sign_in()
                        signed_in = True
                        passed += 1
                        continue
                    if not signed_in:
                        raise ValueError("Sign in before inspecting the web client.")
                    if step.action == "sign_out":
                        response = context.request.post(
                            origin + "/web/session/destroy",
                            data={"jsonrpc": "2.0", "method": "call", "params": {}},
                            max_redirects=0,
                            timeout=15000,
                        )
                        try:
                            if response.status != 200 or response.json().get("error"):
                                raise ValueError("Test identity sign-out was not verified.")
                        finally:
                            response.dispose()
                        page.close()
                        context.clear_cookies()
                        signed_in = False
                        passed += 1
                        continue
                    if step.action == "open":
                        page.goto(origin + step.value, wait_until="domcontentloaded")
                    elif step.action == "screenshot":
                        # No login-page screenshots, raw HTML, console, tracing,
                        # downloads, HAR or storage-state export exists here.
                        page.locator(".o_web_client").wait_for(state="visible")
                        masks = [
                            page.locator("input, textarea, [contenteditable], pre, code"),
                            page.get_by_text(password, exact=False),
                        ]
                        if len(screenshots) >= 5:
                            raise ValueError("Screenshot limit reached.")
                        png = page.screenshot(mask=masks, animations="disabled")
                        if len(png) > 2 * 1024 * 1024:
                            raise ValueError("Screenshot exceeds receipt bound.")
                        screenshots.append(base64.b64encode(png).decode("ascii"))
                    else:
                        locator = page.locator(step.selector)
                        if locator.count() != 1:
                            raise ValueError("A step must select exactly one element.")
                        # Browser scripts may not fill secrets or select an iframe.
                        require_safe_browser_control(locator)
                        if step.action == "fill":
                            locator.fill(step.value)
                        elif step.action == "click":
                            locator.click(no_wait_after=True)
                        else:
                            locator.wait_for(state="visible")
                    page_origin = urlsplit(page.url)
                    if (page_origin.scheme, page_origin.netloc) != (
                        expected_origin.scheme,
                        expected_origin.netloc,
                    ):
                        raise ValueError("The browser left its testing origin.")
                    passed += 1
                check_origin_identity()
                return passed, tuple(screenshots)
            finally:
                context.close()
        finally:
            browser.close()
