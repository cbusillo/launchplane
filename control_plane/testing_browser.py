import os
import sys

import click
from playwright.sync_api import Browser, BrowserType, sync_playwright


def launch_testing_browser(chromium: BrowserType) -> Browser:
    endpoint = os.environ.get("LAUNCHPLANE_BROWSER_WS_ENDPOINT", "").strip()
    if endpoint:
        return chromium.connect(endpoint, timeout=30_000)
    if sys.platform == "linux":
        raise click.ClickException("Testing verification needs the isolated browser service.")
    environment: dict[str, str | float | bool] = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
    }
    return chromium.launch(chromium_sandbox=True, env=environment)


def check_testing_browser_availability() -> None:
    with sync_playwright() as playwright:
        browser = launch_testing_browser(playwright.chromium)
        browser.close()
