"""One private browser, no exported browser handle, credentials, traces or HTML."""

import base64
from collections.abc import Callable
from urllib.parse import urlsplit

from playwright.sync_api import Route, sync_playwright

from control_plane.contracts.odoo_walkthrough import WalkthroughStep
from control_plane.contracts.runtime_identity import (
    RuntimeIdentity,
    runtime_identity_from_health_payload,
)
from control_plane.odoo_runtime_reads import require_runtime_identity
from control_plane.workflows.verireel_testing_post_deploy import launch_testing_browser


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
) -> tuple[int, tuple[str, ...]]:
    """Credentials are consumed by a non-redirecting request, never a login form.

    Request/response text and Playwright exceptions never leave this function.
    The caller translates failures to a fixed code. Browser storage lives only
    in this short-lived context, which is closed on every exit.
    """
    expected_origin = urlsplit(origin)
    if (
        identity.instance != "testing"
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
                    parsed = urlsplit(route.request.url)
                    if (parsed.scheme, parsed.netloc) != (
                        expected_origin.scheme,
                        expected_origin.netloc,
                    ) or parsed.path.startswith(
                        ("/web/login", "/web/reset_password", "/web/signup")
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

                check_origin_identity()
                checkpoint()
                response = context.request.post(
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
                    result = response.json().get("result", {}) if response.status == 200 else {}
                    if result.get("uid") != account_id:
                        raise ValueError("Testing account sign-in failed.")
                finally:
                    response.dispose()
                page = context.new_page()
                page.goto(origin + "/web", wait_until="domcontentloaded")
                page.locator(".o_web_client").wait_for(state="visible")
                screenshots: list[str] = []
                passed = 0
                for step in steps:
                    check_origin_identity()
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
                        safe = locator.evaluate("""element =>
                            element.ownerDocument === document &&
                            !['password', 'hidden', 'file'].includes(element.type) &&
                            !element.closest('iframe') &&
                            !/password|token|secret|api.?key/i.test(
                                [element.name, element.id, element.autocomplete].join(' '))
                        """)
                        if safe is not True:
                            raise ValueError("Sensitive browser controls cannot be selected.")
                        if step.action == "fill":
                            locator.fill(step.value)
                        elif step.action == "click":
                            locator.click(no_wait_after=True)
                        else:
                            locator.wait_for(state="visible")
                    parsed = urlsplit(page.url)
                    if (parsed.scheme, parsed.netloc) != (
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
