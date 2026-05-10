from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.runtime_key_safety_policy import RuntimeKeySafetyEvaluation
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
AgentWriteIntentSecretStatus = Literal["not_required", "pass", "fail", "unavailable"]

_INTENT_AUTHZ_ACTIONS: dict[AgentWriteIntentKind, str] = {
    "every_code_rerun": "every_code_work_request.rerun",
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


class AgentWriteIntentDestination(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["runtime_environment"] = "runtime_environment"
    context: str
    instance: str

    @model_validator(mode="after")
    def _validate_destination(self) -> "AgentWriteIntentDestination":
        if not self.context.strip():
            raise ValueError("agent write intent destination requires context")
        if not self.instance.strip():
            raise ValueError("agent write intent destination requires instance")
        self.context = self.context.strip()
        self.instance = self.instance.strip()
        return self


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
    secret_bindings: tuple[str, ...] = ()
    destination: AgentWriteIntentDestination | None = None

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
        self.secret_bindings = _normalize_secret_binding_keys(self.secret_bindings)
        if self.secret_bindings and self.destination is None:
            raise ValueError("secret-backed agent write intent requires destination")
        return self


class AgentWriteIntentSecretEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: AgentWriteIntentSecretStatus = "not_required"
    destination: AgentWriteIntentDestination | None = None
    checked_binding_keys: tuple[str, ...] = ()
    policy_record_id: str = ""
    policy_sha256: str = ""
    findings: tuple[dict[str, object], ...] = ()


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
    secret_evidence: AgentWriteIntentSecretEvidence = Field(
        default_factory=AgentWriteIntentSecretEvidence
    )


class AgentWriteIntentRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    record_id: str
    recorded_at: str
    trace_id: str
    idempotency_key: str = ""
    request: AgentWriteIntentRequest
    evaluation: AgentWriteIntentEvaluation

    @model_validator(mode="after")
    def _validate_record(self) -> "AgentWriteIntentRecord":
        if not self.record_id.strip():
            raise ValueError("agent write intent record requires record_id")
        if not self.recorded_at.strip():
            raise ValueError("agent write intent record requires recorded_at")
        if not self.trace_id.strip():
            raise ValueError("agent write intent record requires trace_id")
        return self


def authz_action_for_agent_write_intent(intent: AgentWriteIntentKind) -> str:
    return _INTENT_AUTHZ_ACTIONS[intent]


def build_agent_write_intent_record_id(
    *,
    recorded_at: str,
    trace_id: str,
    request: AgentWriteIntentRequest,
    evaluation: AgentWriteIntentEvaluation,
) -> str:
    normalized_timestamp = recorded_at.replace("-", "").replace(":", "").replace(
        "+00:00", "Z"
    )
    payload = {
        "trace_id": trace_id,
        "intent": request.model_dump(mode="json"),
        "evaluation": evaluation.model_dump(mode="json"),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return f"agent-write-intent-{normalized_timestamp}-{digest}"


def agent_write_intent_secret_action(request: AgentWriteIntentRequest) -> str:
    if not request.secret_bindings:
        return ""
    return f"{authz_action_for_agent_write_intent(request.intent)}.secret"


def secret_evidence_for_agent_write_intent(
    *,
    request: AgentWriteIntentRequest,
    evaluation: RuntimeKeySafetyEvaluation | None,
    policy_record_id: str = "",
    policy_sha256: str = "",
    unavailable: bool = False,
) -> AgentWriteIntentSecretEvidence:
    if not request.secret_bindings:
        return AgentWriteIntentSecretEvidence()
    if unavailable or evaluation is None:
        return AgentWriteIntentSecretEvidence(
            status="unavailable",
            destination=request.destination,
            checked_binding_keys=request.secret_bindings,
        )
    return AgentWriteIntentSecretEvidence(
        status=evaluation.status,
        destination=request.destination,
        checked_binding_keys=evaluation.checked_binding_keys,
        policy_record_id=policy_record_id,
        policy_sha256=policy_sha256,
        findings=tuple(finding.model_dump(mode="json") for finding in evaluation.findings),
    )


def evaluate_agent_write_intent(
    *,
    request: AgentWriteIntentRequest,
    authorized: bool,
    audit: AgentAuthzAudit,
    secret_evidence: AgentWriteIntentSecretEvidence | None = None,
) -> AgentWriteIntentEvaluation:
    authz_action = authz_action_for_agent_write_intent(request.intent)
    resolved_secret_evidence = secret_evidence or AgentWriteIntentSecretEvidence()
    if request.secret_bindings and resolved_secret_evidence.status != "pass":
        return AgentWriteIntentEvaluation(
            intent=request.intent,
            mode=request.mode,
            status="denied",
            authz_action=authz_action,
            product=request.product,
            context=request.context,
            source_url=request.source_url,
            safe_to_execute=False,
            next_action="Fix the managed secret binding policy or destination before requesting this intent.",
            reason_code="secret_evidence_denied",
            audit=audit.model_copy(update={"decision": "denied", "reason_code": "secret_evidence_denied"}),
            secret_evidence=resolved_secret_evidence,
        )
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
            secret_evidence=resolved_secret_evidence,
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
            secret_evidence=resolved_secret_evidence,
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
        secret_evidence=resolved_secret_evidence,
    )


def _normalize_secret_binding_keys(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    for raw_value in values:
        value = raw_value.strip()
        if not value:
            raise ValueError("agent write intent secret binding keys must be non-empty")
        if value not in normalized:
            normalized.append(value)
    return tuple(normalized)
