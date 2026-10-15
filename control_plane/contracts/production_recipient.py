"""Custody of a fresh production recipient, independent of the serving binding."""

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord


class ProductionRecipientRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    context: str = Field(min_length=1)
    instance: Literal["prod"] = "prod"
    source_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    destination_server_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_-]+$")
    volume_keys: dict[str, str]
    ports: dict[str, int]
    application_service: str = Field(min_length=1)
    database_service: str = Field(min_length=1)
    mode: Literal["dry-run", "apply", "readback", "receipt"] = "dry-run"
    reason: str = Field(min_length=1)
    reviewed_plan_sha256: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")

    @model_validator(mode="after")
    def validate_resources(self) -> Self:
        import re

        if set(self.volume_keys) != {"database", "config"}:
            raise ValueError("Both independent database and config storage are required.")
        keys = (*self.volume_keys.values(), *self.ports)
        if len(set(keys)) != len(keys) or any(
            not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) for key in keys
        ):
            raise ValueError("Resource overrides require distinct setting keys.")
        if (
            not self.ports
            or len(set(self.ports.values())) != len(self.ports)
            or any(not 1024 <= port <= 65535 for port in self.ports.values())
        ):
            raise ValueError("Independent explicit unprivileged ports are required.")
        if self.application_service == self.database_service:
            raise ValueError("Application and database must be separate services.")
        return self


class ProductionRecipientRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    context: str
    actor: str
    scope: str
    original_key: str
    request: ProductionRecipientRequest
    plan_sha256: str
    namespace: str
    retained_target: DokployTargetRecord
    retained_target_id: DokployTargetIdRecord
    retained_provider: ProviderTargetRecord
    authority_snapshot: dict[str, Any]
    retained_provider_sha256: str
    compose_source: str
    image_reference: str
    project_id: str = ""
    environment_id: str = ""
    target_id: str = ""
    app_name: str = ""
    phase: str = "prepared"
    qualified_at: str = ""

    @property
    def volumes(self) -> dict[str, str]:
        return {"database": self.namespace + "-db", "config": self.namespace + "-config"}


class ProductionRecipientPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    context: str
    instance: Literal["prod"] = "prod"
    plan_sha256: str
    source_commit: str
    image_reference: str
    retained_binding_sha256: str
    old_binding_preserved: Literal[True] = True
    old_recipient_preserved: Literal[True] = True
    deployment_requested: Literal[False] = False
    bootstrap_eligible: Literal[False] = False
    deletion_requested: Literal[False] = False
    qualified: bool = False
    qualified_at: str = ""
