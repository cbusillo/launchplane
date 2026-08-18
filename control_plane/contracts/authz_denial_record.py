from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from control_plane.service_auth import (
    AgentConsumerSubjectType,
    AuthorizationScope,
    AuthzDecisionReason,
    AuthzEvaluation,
)


class AuthzDenialRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    trace_id: str
    recorded_at: str
    expires_at: str
    route_path: str
    principal_type: AgentConsumerSubjectType
    action: str
    product: str
    context: str
    target_scope: AuthorizationScope
    instance_specified: bool
    reason_code: AuthzDecisionReason
    policy_record_id: str
    policy_revision: int = Field(ge=1)
    policy_sha256: str

    @field_validator("recorded_at", "expires_at")
    @classmethod
    def _normalize_timestamp(cls, value: str) -> str:
        timestamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError("Authz denial timestamps require an explicit timezone.")
        return timestamp.astimezone(timezone.utc).isoformat()

    @model_validator(mode="after")
    def _validate_record(self) -> "AuthzDenialRecord":
        required_values = (
            self.trace_id,
            self.recorded_at,
            self.expires_at,
            self.route_path,
            self.principal_type,
            self.policy_record_id,
            self.policy_sha256,
        )
        if not all(value.strip() for value in required_values):
            raise ValueError("Authz denial record requires complete redacted evidence.")
        bounded_values = {
            "trace_id": (self.trace_id, 128),
            "recorded_at": (self.recorded_at, 64),
            "expires_at": (self.expires_at, 64),
            "route_path": (self.route_path, 512),
            "action": (self.action, 512),
            "product": (self.product, 512),
            "context": (self.context, 512),
            "policy_record_id": (self.policy_record_id, 512),
            "policy_sha256": (self.policy_sha256, 128),
        }
        for field_name, (value, maximum_length) in bounded_values.items():
            if len(value) > maximum_length:
                raise ValueError(f"Authz denial record {field_name} is too long.")
        if self.reason_code == "allowed":
            raise ValueError("Authz denial record cannot persist an allowed decision.")
        return self


def build_authz_denial_record(
    *,
    trace_id: str,
    recorded_at: str,
    expires_at: str,
    route_path: str,
    evaluation: AuthzEvaluation,
    policy_record_id: str,
    policy_revision: int,
    policy_sha256: str,
) -> AuthzDenialRecord:
    if evaluation.decision != "denied":
        raise ValueError("Authz denial record requires a denied evaluation.")
    return AuthzDenialRecord(
        trace_id=trace_id,
        recorded_at=recorded_at,
        expires_at=expires_at,
        route_path=route_path,
        principal_type=evaluation.principal_type,
        action=evaluation.action,
        product=evaluation.product,
        context=evaluation.context,
        target_scope=evaluation.target_scope,
        instance_specified=evaluation.instance_specified,
        reason_code=evaluation.reason_code,
        policy_record_id=policy_record_id,
        policy_revision=policy_revision,
        policy_sha256=policy_sha256,
    )
