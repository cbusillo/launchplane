from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.service_auth import AgentAuthzAudit


AgentWriteIntentKind = Literal[
    "every_code_rerun",
    "preview_refresh",
    "preview_cleanup",
    "preview_request",
    "product_config_apply",
    "promotion_dry_run",
    "promotion_dispatch",
]
AgentWriteIntentMode = Literal["dry_run", "apply"]
AgentWriteIntentStatus = Literal["allowed", "denied"]

_INTENT_AUTHZ_ACTIONS: dict[AgentWriteIntentKind, str] = {
    "every_code_rerun": "every_code_work_request.write",
    "preview_refresh": "preview_refresh.execute",
    "preview_cleanup": "preview_lifecycle.cleanup",
    "preview_request": "preview_generation.write",
    "product_config_apply": "product_config.apply",
    "promotion_dry_run": "generic_web_prod_promotion.dry_run",
    "promotion_dispatch": "generic_web_prod_promotion_workflow.execute",
}
_DRY_RUN_ONLY_INTENTS: frozenset[AgentWriteIntentKind] = frozenset(
    {"product_config_apply", "promotion_dry_run"}
)


class AgentWriteIntentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    intent: AgentWriteIntentKind
    mode: AgentWriteIntentMode = "dry_run"
    product: str = "launchplane"
    context: str = "launchplane"
    source_url: str
    idempotency_key: str = ""
    reason: str

    @model_validator(mode="after")
    def _validate_request(self) -> "AgentWriteIntentRequest":
        if not self.product.strip():
            raise ValueError("agent write intent requires product")
        if not self.context.strip():
            raise ValueError("agent write intent requires context")
        if not self.source_url.strip():
            raise ValueError("agent write intent requires source_url")
        if not self.reason.strip():
            raise ValueError("agent write intent requires reason")
        return self


class AgentWriteIntentEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    intent: AgentWriteIntentKind
    mode: AgentWriteIntentMode
    status: AgentWriteIntentStatus
    authz_action: str
    product: str
    context: str
    source_url: str
    safe_to_execute: bool
    next_action: str
    reason_code: str
    audit: AgentAuthzAudit


def authz_action_for_agent_write_intent(intent: AgentWriteIntentKind) -> str:
    return _INTENT_AUTHZ_ACTIONS[intent]


def evaluate_agent_write_intent(
    *, request: AgentWriteIntentRequest, authorized: bool, audit: AgentAuthzAudit
) -> AgentWriteIntentEvaluation:
    authz_action = authz_action_for_agent_write_intent(request.intent)
    if request.mode == "apply" and request.intent in _DRY_RUN_ONLY_INTENTS:
        return AgentWriteIntentEvaluation(
            intent=request.intent,
            mode=request.mode,
            status="denied",
            authz_action=authz_action,
            product=request.product,
            context=request.context,
            source_url=request.source_url,
            safe_to_execute=False,
            next_action="Run this intent in dry_run mode before requesting apply authority.",
            reason_code="dry_run_required",
            audit=audit.model_copy(update={"decision": "denied", "reason_code": "dry_run_required"}),
        )
    if not authorized:
        return AgentWriteIntentEvaluation(
            intent=request.intent,
            mode=request.mode,
            status="denied",
            authz_action=authz_action,
            product=request.product,
            context=request.context,
            source_url=request.source_url,
            safe_to_execute=False,
            next_action="Request a narrower policy grant for this intent, product, and context.",
            reason_code="authorization_denied",
            audit=audit,
        )
    return AgentWriteIntentEvaluation(
        intent=request.intent,
        mode=request.mode,
        status="allowed",
        authz_action=authz_action,
        product=request.product,
        context=request.context,
        source_url=request.source_url,
        safe_to_execute=request.mode == "apply",
        next_action=(
            "Submit the matching Launchplane action route with the same source and idempotency key."
            if request.mode == "apply"
            else "Review the dry-run result before requesting apply authority."
        ),
        reason_code="authorized",
        audit=audit,
    )
