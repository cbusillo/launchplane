"""Opaque, testing-only Odoo task accounts and service-owned browser work."""

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

WALKTHROUGH_ROUTE = "/v1/drivers/odoo/testing-walkthrough"
WALKTHROUGH_ACTION = "odoo_testing_walkthrough.execute"
WALKTHROUGH_PLAN_ACTION = "odoo_testing_walkthrough.plan"
WALKTHROUGH_RECONCILIATION_REQUIRED = "testing_task_reconciliation_required"
WALKTHROUGH_LOGIN_PREFIX = "lp_walk_"
WALKTHROUGH_MARKER_PREFIX = "launchplane.walkthrough."
WALKTHROUGH_LOCK_PREFIX = "launchplane.walkthrough:"
WALKTHROUGH_ACTIVE_STATE = "active"
WALKTHROUGH_CLOSED_STATE = "closed"
WALKTHROUGH_FORBIDDEN_GROUPS = (
    "base.group_system",
    "base.group_erp_manager",
    "base.group_sanitize_override",
    "base.group_allow_export",
)
WALKTHROUGH_CREDENTIAL_LIFETIME_SECONDS = 4 * 60 * 60
# Persisted before effects so a missing journal can be reconciled safely.
# Includes the bounded eligibility re-reads; native execution has its own
# shorter timeouts, and browser work does not extend this admission window.
WALKTHROUGH_DISPATCH_SECONDS = 10 * 60


def walkthrough_activity_scope(product: str) -> str:
    return "odoo-walkthrough:" + product


class WalkthroughStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["open", "click", "fill", "visible", "screenshot"]
    selector: str = Field(default="", max_length=500)
    value: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def validate_step(self) -> "WalkthroughStep":
        if self.action == "open":
            parsed = urlsplit(self.value)
            if (
                not self.value.startswith("/")
                or self.value.startswith("//")
                or parsed.scheme
                or parsed.netloc
                or parsed.query
                or "%" in parsed.path
                or any(segment in {".", ".."} for segment in parsed.path.split("/"))
                or "\\" in self.value
                or any(ord(c) < 32 for c in self.value)
                or not (parsed.path in {"/web", "/odoo"} or parsed.path.startswith("/odoo/"))
            ):
                raise ValueError("Open accepts only the testing web client and its fragment.")
        elif self.action != "screenshot" and not self.selector.strip():
            raise ValueError("A browser interaction requires a selector.")
        if self.action == "fill" and not self.value.startswith("TEST"):
            raise ValueError("Walkthrough input must be marked TEST.")
        if self.action not in {"fill", "open"} and self.value:
            raise ValueError("This step does not accept input.")
        return self


class OdooWalkthroughRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str = Field(min_length=1, max_length=128)
    context: str = Field(min_length=1, max_length=128)
    instance: Literal["testing"] = "testing"
    task_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    operation: Literal["provision", "run", "cleanup"]
    mode: Literal["dry-run", "apply"] = "dry-run"
    reviewed_plan_sha256: str = Field(default="", pattern=r"^(|[a-f0-9]{64})$")
    groups: tuple[str, ...] = Field(default=("base.group_user",), max_length=20)
    steps: tuple[WalkthroughStep, ...] = Field(default=(), max_length=50)

    @model_validator(mode="after")
    def validate_request(self) -> "OdooWalkthroughRequest":
        import re

        if self.mode == "apply" and not self.reviewed_plan_sha256:
            raise ValueError("Apply requires the reviewed plan digest.")
        if self.mode == "dry-run" and self.reviewed_plan_sha256:
            raise ValueError("Dry-run cannot supply a reviewed plan digest.")
        if self.operation != "run" and self.steps:
            raise ValueError("Only run accepts browser steps.")
        if not self.groups or any(
            not re.fullmatch(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*", group)
            or group in (*WALKTHROUGH_FORBIDDEN_GROUPS, "base.group_no_one")
            for group in self.groups
        ):
            raise ValueError("Use explicit business groups, excluding Odoo administration.")
        return self


class OdooWalkthroughResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ready", "pass", "fail", "unknown"]
    plan_sha256: str
    task_id: str
    operation: Literal["provision", "run", "cleanup"]
    account_id: int | None = None
    signed_in: bool = False
    steps_passed: int = 0
    # PNG only, after login and redaction; never an HTML/trace/HAR/storage artifact.
    screenshots: tuple[str, ...] = ()
    screenshot_sha256: tuple[str, ...] = ()
    product: str = ""
    context: str = ""
    instance: Literal["testing"] = "testing"
    deployment_record_id: str = ""
    artifact_id: str = ""
    code: str = ""


class OdooWalkthroughResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["accepted"] = "accepted"
    trace_id: str
    result: OdooWalkthroughResult
