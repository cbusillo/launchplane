"""Exact stored-key contraction; snapshots stay in service-owned review custody."""

from typing import Literal
import re
import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord


def request_fingerprint(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


ROUTE = "/v1/admin/runtime-settings/removal"
REVIEW_ROUTE = ROUTE + "/reviews"


def printable_key(key: str) -> bool:
    # Historical keys need not be shell identifiers. Never echo arbitrary content.
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}", key)) and not bool(
        re.search(r"(?:gh[pousr]_|github_pat_|sk-|AKIA)[A-Za-z0-9_-]{12,}", key)
    )


class RuntimeSettingRemovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["dry-run", "apply", "readback"] = "dry-run"
    product: str = Field(min_length=1, max_length=256)
    scope: Literal["global", "context", "instance"]
    context: str = Field(default="", max_length=256)
    instance: str = Field(default="", max_length=256)
    keys: tuple[str, ...] = Field(min_length=1, max_length=256)
    reason: str = Field(min_length=1, max_length=1000)
    review_id: str = Field(default="", max_length=128)
    reviewed_plan_sha256: str = Field(default="", pattern=r"^(?:[0-9a-f]{64})?$")

    @model_validator(mode="after")
    def validate_selection(self) -> "RuntimeSettingRemovalRequest":
        if len(set(self.keys)) != len(self.keys) or any(not printable_key(k) for k in self.keys):
            raise ValueError("Select unique printable setting names.")
        if self.scope == "global":
            if self.product != "launchplane" or self.context or self.instance:
                raise ValueError("Global removal belongs to the Launchplane admin boundary.")
        elif not self.context.strip() or (self.scope == "context" and self.instance):
            raise ValueError("Context removal requires an exact context only.")
        elif self.scope == "instance" and not self.instance.strip():
            raise ValueError("Instance removal requires an exact lane.")
        if self.mode != "dry-run" and (not self.review_id or not self.reviewed_plan_sha256):
            raise ValueError("Apply/readback requires a saved reviewed plan.")
        return self

    def intent(self) -> dict[str, object]:
        return {
            "product": self.product,
            "scope": self.scope,
            "context": self.context,
            "instance": self.instance,
            "keys": sorted(self.keys),
            "reason": self.reason,
        }


def removal_plan(
    record: RuntimeEnvironmentRecord,
    request: RuntimeSettingRemovalRequest,
    *,
    authority_sha256: str,
    private_nonce: str,
) -> dict[str, object]:
    if (record.scope, record.context, record.instance) != (
        request.scope,
        request.context,
        request.instance,
    ) or not set(request.keys).issubset(record.env):
        raise ValueError("The selected record or keys are unavailable; refresh the inventory.")
    before_sha256 = request_fingerprint(
        {"nonce": private_nonce, "record": record.model_dump(mode="json")}
    )
    survivor_keys = sorted(set(record.env) - set(request.keys))
    plan: dict[str, object] = {
        "product": request.product,
        "scope": record.scope,
        "context": record.context,
        "instance": record.instance,
        "removed_keys": sorted(request.keys),
        "record_sha256": before_sha256,
        "updated_at": record.updated_at,
        "surviving_keys": [k for k in survivor_keys if printable_key(k)],
        "unprintable_survivor_count": sum(not printable_key(k) for k in survivor_keys),
        "surviving_value_count": len(survivor_keys),
        "retirement_intent_count": len(record.retired_provider_keys),
        "record_deleted": not survivor_keys and not record.retired_provider_keys,
        "requires_compatible_readers_and_rollback": not survivor_keys
        and bool(record.retired_provider_keys),
        "authority_sha256": authority_sha256,
        "applied": False,
    }
    digest = request_fingerprint({"intent": request.intent(), "plan": plan})
    return {**plan, "plan_sha256": digest, "review_id": "runtime-removal-" + digest}
