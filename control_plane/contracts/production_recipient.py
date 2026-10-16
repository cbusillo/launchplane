"""Custody of a fresh production recipient, independent of the serving binding."""

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord


class RecipientServiceLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    cpus: float = Field(gt=0)
    memory_bytes: int = Field(gt=0)
    command_memory_limits: dict[Literal["--innodb-buffer-pool-size"], int] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def validate_command_limits(self) -> Self:
        if any(value <= 0 for value in self.command_memory_limits.values()):
            raise ValueError("Command memory limits must be positive.")
        return self


class RecipientUpstream(BaseModel):
    """Connection roles are application environment keys, never supplied values."""

    model_config = ConfigDict(extra="forbid")

    purpose: Literal["read_only_source"] = "read_only_source"
    service: str = Field(min_length=1)
    keys: dict[Literal["host", "port", "user", "database", "password"], str]
    binding_id: str = Field(min_length=1)
    version_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_keys(self) -> Self:
        import re

        if (
            set(self.keys) != {"host", "port", "user", "database", "password"}
            or any(not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) for key in self.keys.values())
            or len(set(self.keys.values())) != len(self.keys)
        ):
            raise ValueError("An upstream requires distinct explicit connection roles.")
        return self


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
    service_limits: dict[str, RecipientServiceLimits]
    upstreams: tuple[RecipientUpstream, ...] = ()
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
        if set(self.service_limits) != {self.application_service, self.database_service}:
            raise ValueError("Explicit resource limits are required for every recipient service.")
        upstream_keys = [key for upstream in self.upstreams for key in upstream.keys.values()]
        if len(set(upstream_keys)) != len(upstream_keys) or any(
            upstream.service != self.application_service for upstream in self.upstreams
        ):
            raise ValueError("Upstream connection roles belong only to the application.")
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
    before_configuration_sha256: str = ""
    phase: str = "prepared"
    qualified_at: str = ""
    supersedes_plan_sha256: str = ""
    prior_attempts: list[dict[str, Any]] = Field(default_factory=list)
    upstream_proof: dict[str, Any] = Field(default_factory=dict)
    recovery_proof: dict[str, Any] = Field(default_factory=dict)

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
    upstream_qualified: bool = False
    recovery_qualified: bool = False


class RecipientRecoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    context: str = Field(min_length=1)
    preparation_plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    mode: Literal["dry-run", "apply", "readback", "receipt"] = "dry-run"
    reason: str = Field(min_length=1)
    reviewed_plan_sha256: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")


class RecipientRecoveryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    context: str
    preparation_plan_sha256: str
    plan_sha256: str
    recovered: bool = False
    capture_sha256: str = ""
    recovered_at: str = ""
    captured_database_empty: bool | None = None
    bootstrap_eligible: Literal[False] = False
