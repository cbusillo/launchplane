"""Testing-only migration and product verification under the deploy reservation."""

from __future__ import annotations

import json
from http.client import HTTPException
import logging
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from uuid import uuid4
from typing import Literal
from typing import cast

import click
from playwright.sync_api import Page, expect, sync_playwright

from control_plane.contracts.promotion_record import PostDeployUpdateEvidence, ReleaseStatus
from control_plane.workflows.generic_web_deploy import (
    GenericWebDeployStore,
    GenericWebPostDeployContext,
    GenericWebPostDeployGuard,
)
from control_plane.workflows.ship import utc_now_timestamp
from control_plane.workflows.verireel_app_maintenance import (
    VeriReelAppMaintenanceAction,
    VeriReelAppMaintenanceIntent,
    VeriReelAppMaintenanceRequest,
    _resolve_stable_testing_application,
    _resolve_stable_testing_base_url,
    execute_verireel_app_maintenance,
)
from control_plane.workflows.verireel_rollout import fetch_url_text

_LOGGER = logging.getLogger(__name__)


def recorded_verireel_testing_qualification(
    store: GenericWebDeployStore,
    deployment_id: str,
    artifact_id: str,
    source_git_ref: str,
    authority_digest: str,
) -> Literal["pass", "fail", "missing"]:
    try:
        record = store.read_deployment_record(deployment_id)
    except FileNotFoundError:
        return "missing"
    evidence = record.post_deploy_update.evidence
    if (
        record.instance != "testing"
        or record.artifact_identity is None
        or record.artifact_identity.artifact_id != artifact_id
        or record.source_git_ref != source_git_ref
        or evidence.get("deployment_record_id") != deployment_id
        or evidence.get("artifact_id") != artifact_id
        or evidence.get("source_git_ref") != source_git_ref
        or evidence.get("driver_id") != "verireel"
    ):
        return "missing"
    if (
        record.post_deploy_update.status == "fail"
        and evidence.get("authority_digest") == authority_digest
    ):
        return "fail"
    if record.post_deploy_update.status == "pass" and all(
        evidence.get(key) == "pass"
        for key in (
            "migration_status",
            "creator_verification_status",
            "admin_verification_status",
            "cleanup_status",
        )
    ):
        return "pass"
    return "missing"


def testing_qualification_authority(
    store: GenericWebDeployStore, context: GenericWebPostDeployContext
) -> str:
    from control_plane.product_reconcile import (
        ProductReconcileStore,
        _generic_web_testing_authority,
    )

    profile = store.read_product_profile_record(context.product)
    lane = next(
        lane
        for lane in profile.lanes
        if lane.context == context.context and lane.instance == context.instance
    )
    return _generic_web_testing_authority(
        record_store=cast(ProductReconcileStore, store), lane=lane
    )


def assert_verireel_testing_identity(base_url: str, context: GenericWebPostDeployContext) -> None:
    status, text = fetch_url_text(f"{base_url}/api/health", accept="application/json")
    payload = json.loads(text)
    identity = payload.get("runtime_identity") if isinstance(payload, dict) else None
    if (
        status != 200
        or not isinstance(payload, dict)
        or payload.get("ok") is not True
        or payload.get("buildRevision") != context.source_git_ref
        or not isinstance(identity, dict)
        or any(
            identity.get(key) != value
            for key, value in {
                "context": context.context,
                "instance": context.instance,
                "deployment_record_id": context.deployment_record_id,
                "artifact_id": context.artifact_id,
                "source_git_ref": context.source_git_ref,
            }.items()
        )
    ):
        raise click.ClickException("Testing runtime does not match this exact deployment.")


def wait_for_verireel_testing_identity(
    base_url: str, context: GenericWebPostDeployContext, *, timeout_seconds: int = 60
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            assert_verireel_testing_identity(base_url, context)
            return
        except (click.ClickException, HTTPError, URLError, HTTPException, TimeoutError, ValueError):
            if time.monotonic() >= deadline:
                raise click.ClickException("Testing did not start this exact deployment in time.")
            time.sleep(min(2, max(0, deadline - time.monotonic())))


def _maintenance(
    root: Path,
    context: GenericWebPostDeployContext,
    guard: GenericWebPostDeployGuard,
    action: VeriReelAppMaintenanceAction,
    intent: VeriReelAppMaintenanceIntent,
    email: str = "",
) -> None:
    # Maintenance resolves its target from records too. Never act after a repoint.
    _, application_id = _resolve_stable_testing_application(control_plane_root=root)
    if application_id != context.target_id:
        raise click.ClickException("Testing maintenance target changed during deployment.")
    guard.checkpoint_effect(f"post_deploy_{action}")
    result = execute_verireel_app_maintenance(
        control_plane_root=root,
        request=VeriReelAppMaintenanceRequest.model_validate(
            {
                "context": context.context,
                "instance": context.instance,
                "action": action,
                "intent": intent,
                "email": email,
            }
        ),
    )
    if result.maintenance_status != "pass":
        raise click.ClickException(f"Testing {action} did not complete successfully.")


def _goto(page: Page, base_url: str, path: str) -> None:
    response = page.goto(f"{base_url}{path}", wait_until="domcontentloaded")
    if response is None or response.status >= 400:
        raise click.ClickException("Testing product route failed.")
    if urlparse(page.url).netloc != urlparse(base_url).netloc:
        raise click.ClickException("Testing product route left the recorded origin.")


def verification_path(base_url: str, href: str) -> str:
    base = urlparse(base_url)
    link = urlparse(urljoin(f"{base_url}/", href))
    if (link.scheme, link.netloc) != (base.scheme, base.netloc) or not link.path.startswith("/v/"):
        raise click.ClickException("Testing QR did not expose a verification path on this lane.")
    return link.path + (f"?{link.query}" if link.query else "")


def verify_verireel_testing_product(
    root: Path,
    context: GenericWebPostDeployContext,
    guard: GenericWebPostDeployGuard,
    base_url: str,
    evidence: dict[str, str],
) -> None:
    stamp = uuid4().hex
    email = f"e2e-{stamp}@example.com"
    public_label = f"Launchplane testing {stamp}"
    destination = f"https://example.com/watch/{stamp}"
    # A disposable application fixture only; no operator credential is created.
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            with browser.new_context() as browser_context:
                page = browser_context.new_page()
                page.set_default_timeout(30_000)
                try:
                    assert_verireel_testing_identity(base_url, context)
                    _goto(page, base_url, "/sign-up")
                    page.wait_for_load_state("networkidle")
                    page.get_by_label("Creator name").fill("Launchplane testing")
                    page.get_by_label("Email").fill(email)
                    page.get_by_placeholder("Create a strong password").fill("Quartz!Canopy$79M")
                    guard.checkpoint_effect("post_deploy_fixture_signup")
                    page.get_by_role(
                        "button", name=re.compile("create account and choose a plan", re.I)
                    ).click()
                    page.wait_for_url(f"{base_url}/billing", wait_until="domcontentloaded")
                    assert_verireel_testing_identity(base_url, context)
                    # Exercise creator denial before turning this fixture into an admin.
                    for path in ("/site-config", "/site-config/billing"):
                        _goto(page, base_url, path)
                        if urlparse(page.url).path == path:
                            raise click.ClickException("Creator reached a protected admin route.")
                    _maintenance(
                        root,
                        context,
                        guard,
                        "grant-sponsored",
                        "stable-testing-remote-e2e-grant-sponsored",
                        email,
                    )
                    _goto(page, base_url, "/dashboard")
                    page.get_by_label("Video label").fill(f"Launchplane creator {stamp}")
                    page.get_by_label("Public verification label").fill(public_label)
                    page.get_by_label("Destination URL").fill(destination)
                    guard.checkpoint_effect("post_deploy_fixture_qr")
                    page.get_by_role(
                        "button", name=re.compile("create verification qr", re.I)
                    ).click()
                    link = page.locator("a[href*='/v/']").last
                    expect(link).to_be_visible()
                    href = link.get_attribute("href") or ""
                    # Only this fixture's QR can qualify; never inspect another user's record.
                    path = verification_path(base_url, href)
                    with browser.new_context() as public_context:
                        public_page = public_context.new_page()
                        public_page.set_default_timeout(30_000)
                        _goto(public_page, base_url, path)
                        expect(public_page.get_by_text(public_label, exact=False)).to_be_visible()
                        expect(
                            public_page.get_by_role("link", name=destination, exact=True)
                        ).to_be_visible()
                    assert_verireel_testing_identity(base_url, context)
                    evidence["creator_verification_status"] = "pass"
                    _maintenance(
                        root, context, guard, "promote-owner", "owner-route-promote-owner", email
                    )
                    for path in ("/site-config", "/site-config/billing"):
                        _goto(page, base_url, path)
                        if urlparse(page.url).path != path:
                            raise click.ClickException("Admin could not reach the protected route.")
                        assert_verireel_testing_identity(base_url, context)
                    evidence["admin_verification_status"] = "pass"
                finally:
                    # The existing cleanup intent also removes sponsored creator fixtures.
                    evidence["cleanup_status"] = "fail"
                    _maintenance(
                        root, context, guard, "delete-user", "owner-route-delete-user", email
                    )
                    evidence["cleanup_status"] = "pass"
        finally:
            browser.close()


def execute_verireel_testing_post_deploy(
    control_plane_root: Path,
    record_store: GenericWebDeployStore,
    context: GenericWebPostDeployContext,
    guard: GenericWebPostDeployGuard,
) -> PostDeployUpdateEvidence:
    evidence = {
        "driver_id": "verireel",
        "deployment_record_id": context.deployment_record_id,
        "source_git_ref": context.source_git_ref,
        "artifact_id": context.artifact_id,
        "migration_status": "skipped",
        "creator_verification_status": "skipped",
        "admin_verification_status": "skipped",
        "cleanup_status": "skipped",
        "started_at": utc_now_timestamp(),
    }
    phase = "scope"
    status: ReleaseStatus
    try:
        profile = record_store.read_product_profile_record(context.product)
        if (
            profile.driver_id != "verireel"
            or context.instance != "testing"
            or not any(
                lane.context == context.context and lane.instance == context.instance
                for lane in profile.lanes
            )
        ):
            raise click.ClickException("VeriReel qualification requires its recorded testing lane.")
        evidence["authority_digest"] = testing_qualification_authority(record_store, context)
        base_url = _resolve_stable_testing_base_url(control_plane_root=control_plane_root)
        phase = "runtime_identity"
        wait_for_verireel_testing_identity(base_url, context)
        phase = "migration"
        evidence["migration_status"] = "fail"
        _maintenance(control_plane_root, context, guard, "migrate", "stable-testing-migration")
        evidence["migration_status"] = "pass"
        phase = "product_verification"
        evidence["creator_verification_status"] = "fail"
        evidence["admin_verification_status"] = "fail"
        verify_verireel_testing_product(control_plane_root, context, guard, base_url, evidence)
        assert_verireel_testing_identity(base_url, context)
        if any(
            evidence[key] != "pass"
            for key in (
                "migration_status",
                "creator_verification_status",
                "admin_verification_status",
                "cleanup_status",
            )
        ):
            raise click.ClickException(
                "Testing qualification did not return complete passing evidence."
            )
        status = "pass"
        detail = "Testing migrations and creator/admin verification passed; fixture removed."
    except Exception as error:
        _LOGGER.warning(
            "VeriReel testing qualification failed in %s (%s)", phase, type(error).__name__
        )
        status = "fail"
        detail = f"Testing qualification failed in {phase}; no passing qualification is recorded."
    evidence["finished_at"] = utc_now_timestamp()
    return PostDeployUpdateEvidence(attempted=True, status=status, detail=detail, evidence=evidence)
