from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Outcome = Literal["pass", "fail", "unavailable", "not_reached", "not_applicable"]
Shape = Literal["missing", "null", "mapping", "list", "string", "boolean", "number", "other"]
Predicate = Literal[
    "acquisition",
    "target_acquisition",
    "conversion_acquisition",
    "cached_acquisition",
    "identity",
    "placement",
    "complete",
    "source_scope",
    "root",
    "includes",
    "input_precedence",
    "exact_environment",
    "default_absence",
    "stable_documents",
    "conversion_parse",
    "root_path",
    "include_paths",
    "document_shape",
    "default_inputs",
    "default_acquisition",
    "environment_identity",
    "compose_identity_present",
    "application_identity",
    "schedules_acquisition",
    "own_candidate_excluded",
    "source_fields_present",
    "source_fields_empty",
    "source_type",
    "env_shape",
    "mounts_empty",
    "command_present",
    "command_empty",
    "backups_empty",
    "domains_empty",
    "deployments_shape",
    "deployments_terminal",
    "schedules_empty",
    "cached_project_absent",
    "named_storage",
    "volume_definitions",
    "volume_definition",
    "volume_identity",
    "standard_environment",
    "allowed_inputs",
    "unique_inputs",
    "interpolation",
    "supported_name",
    "resolution",
    "volume_reference",
    "environment_reference",
    "mounts_shape",
    "mount_reference",
    "provider_inventory",
    "native_inventory",
    "lifecycle_acquisition",
    "lifecycle_usable",
    "physical",
    "source_preserved",
    "owned",
    "mounts",
    "web_command",
    "runtime_identity",
    "web_data_mount",
    "web_gate",
    "web_process",
    "container_inventory",
    "container_qualification",
    "foreign_reference",
]


class EvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class QualificationEvidence(EvidenceModel):
    checks: dict[Predicate, Outcome] = Field(default_factory=dict)


class VolumeResolutionEvidence(QualificationEvidence):
    alias_sha256: Digest
    name_source: Literal["explicit", "external_alias", "project_prefix", "unavailable"] = (
        "unavailable"
    )
    resolved_name_sha256: Digest | None = None


class VolumeDocumentEvidence(QualificationEvidence):
    document_sha256: Digest | None = None
    resolution_index: int | None = Field(default=None, ge=0)
    path_sha256: Digest | None = None
    input_sha256: Digest | None = None
    resolved_name_sha256: tuple[Digest, ...] = ()
    volumes: list[VolumeResolutionEvidence] = Field(default_factory=list)


class ConsumerEvidence(QualificationEvidence):
    kind: Literal[
        "project",
        "environment",
        "compose",
        "application",
        "postgres",
        "mysql",
        "mariadb",
        "mongo",
        "redis",
        "libsql",
    ]
    record_sha256: Digest | None = None
    payload_sha256: Digest | None = None
    server_identity: Literal["missing", "local", "remote", "malformed"] = "missing"
    server_sha256: Digest | None = None
    endpoint_sha256: Digest | None = None
    conversion_shape: Shape = "missing"
    converted_sha256: Digest | None = None
    source_path_shape: Shape = "missing"
    include_shape: Shape = "missing"
    include_count: int | None = Field(default=None, ge=0)
    source_type: Literal[
        "missing", "null", "empty", "raw", "git", "github", "gitlab", "bitbucket", "gitea", "other"
    ] = "missing"
    cached_endpoint_sha256: Digest | None = None
    documents: list[VolumeDocumentEvidence] = Field(default_factory=list)


class ContainerEvidence(QualificationEvidence):
    container_sha256: Digest
    role: Literal["web", "database", "script-runner", "other", "unknown"] = "unknown"
    state: Literal["running", "exited", "created", "dead", "other", "unknown"] = "unknown"
    volume_roles: tuple[Literal["ODOO_DB_VOLUME", "ODOO_DATA_VOLUME", "ODOO_LOG_VOLUME"], ...] = ()
    mounts_sha256: Digest | None = None


class LifecycleEvidence(QualificationEvidence):
    snapshot_sha256: Digest | None = None
    resource_binding_sha256: Digest | None = None
    physical_binding_sha256: Digest | None = None
    container_count: int | None = Field(default=None, ge=0)
    containers: list[ContainerEvidence] = Field(default_factory=list)


class VolumeCustodyEvidence(QualificationEvidence):
    role: Literal["ODOO_DB_VOLUME", "ODOO_DATA_VOLUME", "ODOO_LOG_VOLUME"]
    name_sha256: Digest
    identity_sha256: Digest | None = None
    engine_sha256: Digest | None = None
    backing_sha256: Digest | None = None
    planned_removal_sha256: Digest | None = None
    planned_snapshot_sha256: Digest | None = None
    planned_container_sha256: tuple[Digest, ...] = ()
    referenced: bool | None = None
    empty: bool | None = None
    source_inputs: bool | None = None
    gate_unexpected: bool | None = None
    consumers: list[ConsumerEvidence] = Field(default_factory=list)


class CandidateCustodyEvidence(QualificationEvidence):
    version: Literal[1] = 1
    observed_at: datetime
    completed_at: datetime | None = None
    candidate_sha256: Digest
    server_sha256: Digest
    endpoint_sha256: Digest | None = None
    plan_sha256: Digest | None = None
    packet_sha256: Digest | None = None
    lifecycle: LifecycleEvidence = Field(default_factory=LifecycleEvidence)
    volumes: list[VolumeCustodyEvidence] = Field(default_factory=list)
