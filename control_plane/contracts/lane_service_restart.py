"""A restart is bound to one existing container, never a deployment request."""

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


SERVICE_RESTART_ROUTE = "/v1/drivers/odoo/service-restart"


def restart_activity_scope(product: str) -> str:
    return "lane-service-restart:" + json.dumps(product, ensure_ascii=True)


class LaneServiceRestartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str = Field(min_length=1, max_length=128)
    context: str = Field(min_length=1, max_length=128)
    instance: str = Field(min_length=1, max_length=128)
    service: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    reason: str = Field(min_length=1, max_length=1000)
    mode: Literal["dry-run", "apply"] = "dry-run"
    reviewed_plan_sha256: str = ""

    @model_validator(mode="after")
    def validate_request(self) -> "LaneServiceRestartRequest":
        for name in ("product", "context", "instance", "reason"):
            value = getattr(self, name).strip()
            if not value:
                raise ValueError(f"Restart requires {name}.")
            setattr(self, name, value)
        if self.mode == "apply" and not re.fullmatch(r"[a-f0-9]{64}", self.reviewed_plan_sha256):
            raise ValueError("Apply requires the reviewed restart plan digest.")
        if self.mode == "dry-run" and self.reviewed_plan_sha256:
            raise ValueError("Dry-run cannot carry a reviewed plan digest.")
        return self


class RestartContainerIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    container_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    image_id: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    image_reference: str
    configuration_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    started_at: str = Field(min_length=1)
    running: bool
    health: str
    runtime_identity_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class LaneServiceRestartPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    context: str
    instance: str
    service: str
    actor: str
    reason: str
    driver_id: str
    target_id: str
    app_name: str
    server_id: str = ""
    artifact_id: str
    deployment_record_id: str
    acceptance_record_id: str = ""
    before: RestartContainerIdentity

    def digest(self) -> str:
        return hashlib.sha256(
            json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


class LaneServiceRestartResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ready", "pass", "fail", "unknown"]
    plan: LaneServiceRestartPlan
    plan_sha256: str
    after: RestartContainerIdentity | None = None
    error_message: str = ""


class LaneServiceRestartResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["accepted"] = "accepted"
    trace_id: str
    records: dict[str, str] = Field(default_factory=dict)
    result: LaneServiceRestartResult
    replayed: bool | None = Field(
        default=None, json_schema_extra={"x-launchplane-optional-response": True}
    )
    original_trace_id: str | None = Field(
        default=None, json_schema_extra={"x-launchplane-optional-response": True}
    )
