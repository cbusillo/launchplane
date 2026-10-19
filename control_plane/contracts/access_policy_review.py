from typing import Literal

from pydantic import BaseModel, ConfigDict


class AccessPolicyRoleEffect(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    principal: str
    before: Literal["admin", "read_only"] | None = None
    after: Literal["admin", "read_only"] | None = None
    state: Literal["lost", "reduced", "retained", "gained", "unknown", "unaffected"]


class AccessPolicyRuleReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    managed_rule_id: str
    change: Literal["added", "removed"]
    principal: str
    roles: tuple[str, ...] = ()
    permissions: tuple[str, ...]
    scope_summary: str
    role_effects: tuple[AccessPolicyRoleEffect, ...] = ()


class AccessPolicyReview(BaseModel):
    """Director-only display, separate from persisted and agent evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: Literal["available", "unavailable"]
    rules: tuple[AccessPolicyRuleReview, ...] = ()
