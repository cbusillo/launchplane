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
WALKTHROUGH_READONLY_GROUP = "launchplane_readonly.group_user"
WALKTHROUGH_READONLY_FIXTURE = "read-only-test-identity"
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

    action: Literal["open", "click", "fill", "visible", "screenshot", "sign_out", "sign_in"]
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
        elif self.action not in {"screenshot", "sign_out", "sign_in"} and not self.selector.strip():
            raise ValueError("A browser interaction requires a selector.")
        if self.action in {"sign_out", "sign_in"} and self.selector:
            raise ValueError("Sign-in controls do not accept selectors.")
        if self.action == "fill" and not self.value.startswith("TEST"):
            raise ValueError("Walkthrough input must be marked TEST.")
        if self.action not in {"fill", "open"} and self.value:
            raise ValueError("This step does not accept input.")
        return self


class OdooWalkthroughRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str = Field(min_length=1, max_length=128)
    context: str = Field(min_length=1, max_length=128)
    instance: str = Field(default="testing", min_length=1, max_length=128)
    read_only: bool = False
    preview_id: str = Field(default="", max_length=200)
    generation_id: str = Field(default="", max_length=200)
    task_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    operation: Literal["provision", "run", "cleanup"]
    mode: Literal["dry-run", "apply"] = "dry-run"
    reviewed_plan_sha256: str = Field(default="", pattern=r"^(|[a-f0-9]{64})$")
    groups: tuple[str, ...] = Field(default=("base.group_user",), max_length=20)
    steps: tuple[WalkthroughStep, ...] = Field(default=(), max_length=50)

    def operation_payload(self, *, exclude: set[str] | None = None) -> dict[str, object]:
        omitted = set(exclude or ())
        if not self.read_only and not self.preview_id and not self.generation_id:
            omitted.update({"read_only", "preview_id", "generation_id"})
        return self.model_dump(mode="json", exclude=omitted)

    @model_validator(mode="after")
    def validate_request(self) -> "OdooWalkthroughRequest":
        import re
        from control_plane.contracts.canonical_json import canonical_json_sha256

        preview = self.instance != "testing"
        if self.instance.strip().lower() in {"prod", "production"} or (
            preview and not (self.read_only and self.preview_id and self.generation_id)
        ):
            raise ValueError("Use testing or an exact read-only preview target.")
        if not preview and (self.preview_id or self.generation_id):
            raise ValueError("Testing cannot supply a preview binding.")
        if self.read_only:
            if self.task_id != canonical_json_sha256(self.product)[:32]:
                raise ValueError("Read-only identities use the product's fixed task ID.")
            if self.groups not in {("base.group_user",), (WALKTHROUGH_READONLY_GROUP,)}:
                raise ValueError("Read-only identities cannot select business roles.")
            self.groups = (WALKTHROUGH_READONLY_GROUP,)
            if any(step.action == "fill" for step in self.steps):
                raise ValueError("Read-only checks cannot fill forms.")

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
    instance: str = "testing"
    deployment_record_id: str = ""
    artifact_id: str = ""
    code: str = ""


class OdooWalkthroughResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["accepted"] = "accepted"
    trace_id: str
    result: OdooWalkthroughResult
