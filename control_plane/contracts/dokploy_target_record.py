import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from control_plane.contracts.public_hosts import normalize_public_hosts

DokployTargetType = Literal["compose", "application"]
DEFAULT_DOKPLOY_HEALTHCHECK_PATH = "/web/health"


class DokployTargetShopifyPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protected_store_keys: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _normalize_store_keys(self) -> "DokployTargetShopifyPolicy":
        normalized_keys: list[str] = []
        for raw_key in self.protected_store_keys:
            normalized_key = raw_key.strip()
            if not normalized_key:
                raise ValueError("Dokploy Shopify protected store keys must be non-empty")
            if normalized_key not in normalized_keys:
                normalized_keys.append(normalized_key)
        self.protected_store_keys = tuple(normalized_keys)
        return self


class DokployTargetRecordChanged(ValueError):
    """A compare-and-write found the target record changed since it was reviewed."""


IntegrationAllowanceKind = Literal["dev_store", "read_only_source", "pre_live"]
_INTEGRATION_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class DokployTargetIntegrationAllowance(BaseModel):
    """Why a non-production lane may hold one integration's settings.

    ``dev_store`` is a non-production service account. ``read_only_source`` is a
    production import source reached with a read-only account, with the grant as
    evidence. ``pre_live`` lets a testing lane keep a tenant's real settings until the
    tenant's production lane is live on Launchplane.
    """

    model_config = ConfigDict(extra="forbid")

    integration: str
    kind: IntegrationAllowanceKind
    reason: str
    evidence: str = ""
    recorded_by: str = ""
    recorded_at: str = ""

    @model_validator(mode="after")
    def _validate_allowance(self) -> "DokployTargetIntegrationAllowance":
        self.integration = self.integration.strip().lower()
        self.reason = self.reason.strip()
        self.evidence = self.evidence.strip()
        self.recorded_by = self.recorded_by.strip()
        self.recorded_at = self.recorded_at.strip()
        if not _INTEGRATION_NAME_PATTERN.fullmatch(self.integration):
            raise ValueError(
                "Integration allowance names use lowercase letters, digits and underscores."
            )
        if not self.reason:
            raise ValueError("Integration allowances require a reason.")
        if self.kind == "read_only_source" and not self.evidence:
            raise ValueError(
                "A read_only_source allowance requires evidence of the read-only grant."
            )
        return self


class DokployTargetStaffTestingHold(BaseModel):
    """Site staff are testing on this lane: event-driven deploys wait until it is lifted."""

    model_config = ConfigDict(extra="forbid")

    reason: str
    recorded_by: str = ""
    recorded_at: str = ""

    @model_validator(mode="after")
    def _validate_hold(self) -> "DokployTargetStaffTestingHold":
        self.reason = self.reason.strip()
        self.recorded_by = self.recorded_by.strip()
        self.recorded_at = self.recorded_at.strip()
        if not self.reason:
            raise ValueError("A staff-testing hold requires a reason.")
        return self


class DokployTargetPolicies(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shopify: DokployTargetShopifyPolicy = Field(default_factory=DokployTargetShopifyPolicy)
    integration_allowances: tuple[DokployTargetIntegrationAllowance, ...] = ()
    staff_testing_hold: DokployTargetStaffTestingHold | None = None

    @model_validator(mode="after")
    def _validate_unique_allowances(self) -> "DokployTargetPolicies":
        integrations = [allowance.integration for allowance in self.integration_allowances]
        if len(integrations) != len(set(integrations)):
            raise ValueError("A lane has at most one allowance per integration.")
        self.integration_allowances = tuple(
            sorted(self.integration_allowances, key=lambda allowance: allowance.integration)
        )
        return self


class DokployTargetRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str
    instance: str
    project_name: str = ""
    target_type: DokployTargetType = "compose"
    target_name: str = ""
    git_branch: str = ""
    source_git_ref: str = "origin/main"
    source_type: str = ""
    custom_git_url: str = ""
    custom_git_branch: str = ""
    compose_path: str = ""
    watch_paths: tuple[str, ...] = ()
    enable_submodules: bool | None = None
    require_test_gate: bool = False
    require_prod_gate: bool = False
    deploy_timeout_seconds: int | None = Field(default=None, ge=1)
    healthcheck_enabled: bool = True
    healthcheck_path: str = DEFAULT_DOKPLOY_HEALTHCHECK_PATH
    healthcheck_timeout_seconds: int | None = Field(default=None, ge=1)
    env: dict[str, str] = Field(default_factory=dict)
    domains: tuple[str, ...] = ()
    public_hosts: tuple[str, ...] = Field(default=(), exclude_if=lambda value: not value)
    policies: DokployTargetPolicies = Field(default_factory=DokployTargetPolicies)
    updated_at: str
    source_label: str = ""

    @field_validator("public_hosts", mode="before")
    @classmethod
    def _validate_public_hosts(cls, value: object) -> tuple[str, ...]:
        return normalize_public_hosts(value)

    @field_validator("context", "instance", "updated_at", mode="after")
    @classmethod
    def _validate_required_string(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Dokploy target record requires non-empty string fields")
        return value.strip()
