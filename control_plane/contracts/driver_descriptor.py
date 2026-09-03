from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from control_plane.contracts.data_provenance import DataProvenance
from control_plane.contracts.lane_summary import LaunchplaneLaneSummary
from control_plane.contracts.preview_summary import LaunchplanePreviewSummary
from control_plane.contracts.production_backup_authority import (
    ProductionBackupAuthorityStatus,
)


DriverActionSafety = Literal["read", "safe_write", "mutation", "destructive"]
DriverActionScope = Literal["global", "context", "instance", "preview"]
DriverActionReadinessRequirement = Literal[
    "provider_target",
    "route_binding",
    "runtime_environment",
    "managed_secrets",
    "artifact",
    "deployment",
    "topology",
]
DriverPanelKind = Literal[
    "summary",
    "lane_health",
    "artifact_evidence",
    "deployment_evidence",
    "promotion_evidence",
    "backup_evidence",
    "preview_inventory",
    "settings",
    "secret_bindings",
    "audit",
]


class DriverActionDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: str
    label: str
    description: str
    safety: DriverActionSafety
    scope: DriverActionScope
    method: Literal["GET", "POST"]
    route_path: str
    authz_action: str = ""
    alternate_authz_actions: tuple[str, ...] = ()
    operator_visible: bool = True
    input_schema: dict[str, object] = Field(default_factory=dict)
    output_schema: dict[str, object] = Field(default_factory=dict)
    writes_records: tuple[str, ...] = ()
    readiness_requirements: tuple[DriverActionReadinessRequirement, ...] = ()
    requires_production_backup_policy: bool = False


class DriverCapabilityDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability_id: str
    label: str
    description: str
    actions: tuple[str, ...] = ()
    panels: tuple[DriverPanelKind, ...] = ()


class DriverSettingGroupDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group_id: str
    label: str
    description: str
    scope: DriverActionScope
    fields: tuple[str, ...] = ()
    secret_bindings: tuple[str, ...] = ()


class DriverDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    driver_id: str
    base_driver_id: str = ""
    label: str
    product: str
    description: str
    context_patterns: tuple[str, ...] = ()
    provider_boundary: str
    capabilities: tuple[DriverCapabilityDescriptor, ...] = ()
    actions: tuple[DriverActionDescriptor, ...] = ()
    setting_groups: tuple[DriverSettingGroupDescriptor, ...] = ()


class DriverView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    driver_id: str
    descriptor: DriverDescriptor
    available_actions: tuple[DriverActionDescriptor, ...] = ()
    lane_summary: LaunchplaneLaneSummary | None = None
    production_backup_authorities: tuple[ProductionBackupAuthorityStatus, ...] = ()
    preview_summaries: tuple[LaunchplanePreviewSummary, ...] = ()
    preview_inventory_provenance: DataProvenance | None = None


class DriverContextView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str
    instance: str = ""
    drivers: tuple[DriverView, ...] = ()
