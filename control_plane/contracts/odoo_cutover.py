"""Exact release and observation contracts for Odoo's two-slot executor."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import Field, model_validator

from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.artifact_release_compatibility import ReleaseDatabaseCompatibility
from control_plane.contracts.prepared_public_site import (
    WRITER_KINDS,
    FrozenRecord,
    PreparedPublicSite,
    PublicPauseRecord,
    PublicSiteBinding,
    WriterTarget,
)
from control_plane.release_compatibility import classify_release_database_compatibility
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.contracts.promotion_record import HealthcheckEvidence
from control_plane.contracts.odoo_website_settings import OdooWebsiteSettingsIntent


class OdooSlot(FrozenRecord):
    slot_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    compose_id: str = Field(min_length=1)
    private_url: str = Field(min_length=1)
    binding: PublicSiteBinding
    image: str = Field(pattern=r"^.+@sha256:[0-9a-f]{64}$")
    runtime_identity: RuntimeIdentity


class OdooCutoverPlan(FrozenRecord):
    release_id: str = Field(min_length=1)
    route_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    host: str = Field(pattern=r"^[a-zA-Z0-9.-]+$")
    production: ArtifactIdentityManifest
    candidate: ArtifactIdentityManifest
    compatibility: ReleaseDatabaseCompatibility
    old: OdooSlot
    new: OdooSlot
    writers: tuple[WriterTarget, ...]
    website_settings: OdooWebsiteSettingsIntent | None = None
    production_website_settings: OdooWebsiteSettingsIntent | None = None

    @property
    def rollback_website_settings(self) -> OdooWebsiteSettingsIntent | None:
        if self.production_website_settings is not None:
            return self.production_website_settings
        if self.website_settings is None:
            return None
        return OdooWebsiteSettingsIntent(
            artifact_id=self.production.artifact_id,
            settings=self.website_settings.settings.model_copy(
                update={
                    "operation": "disable_collection",
                    "google_analytics_key": "",
                    "public_indexing_enabled": False,
                    "cookies_bar": None,
                    "block_third_party_domains": None,
                }
            ),
        )

    @model_validator(mode="after")
    def validate_release(self) -> Self:
        for intent, artifact in (
            (self.website_settings, self.candidate),
            (self.production_website_settings, self.production),
        ):
            if intent is not None and (
                intent.artifact_id != artifact.artifact_id
                or intent.settings.public_origin.rstrip("/") != "https://" + self.host
            ):
                raise ValueError("website snapshot differs from its exact slot artifact/origin")
        if self.compatibility != classify_release_database_compatibility(
            production=self.production, candidate=self.candidate
        ):
            raise ValueError("cutover classification does not match verified artifact pair")
        if not self.compatibility.module_plan_complete:
            raise ValueError("cutover requires a complete targeted maintenance plan")
        if self.old.slot_id == self.new.slot_id or self.old.compose_id == self.new.compose_id:
            raise ValueError("candidate must be a separate app slot/project")
        for slot, artifact in ((self.old, self.production), (self.new, self.candidate)):
            if (
                slot.binding.artifact_id != artifact.artifact_id
                or slot.binding.image_digest != artifact.image.digest
                or slot.image != f"{artifact.image.repository}@{artifact.image.digest}"
                or slot.binding.release_id != self.release_id
                or slot.runtime_identity.artifact_id != artifact.artifact_id
                or slot.runtime_identity.source_git_ref != artifact.source_commit
                or slot.runtime_identity.image_reference != slot.image
                or slot.runtime_identity.image_digest != artifact.image.digest
                or slot.runtime_identity.slot != slot.slot_id
                or not slot.runtime_identity.database_uuid
                or slot.runtime_identity.instance != slot.binding.environment_id
            ):
                raise ValueError("slot identity must match its exact release artifact")
        for field in ("product_id", "environment_id", "release_id", "database"):
            if getattr(self.old.binding, field) != getattr(self.new.binding, field):
                raise ValueError("slots must share the authoritative lane/database binding")
        if (
            self.old.runtime_identity.database_uuid != self.new.runtime_identity.database_uuid
            or self.new.runtime_identity.release_id != self.release_id
            or self.old.private_url == self.new.private_url
        ):
            raise ValueError("distinct slots require the exact candidate release and same DB UUID")
        if self.old.runtime_identity.context != self.new.runtime_identity.context:
            raise ValueError("slot contexts differ")
        if len({writer.writer_id for writer in self.writers}) != len(self.writers):
            raise ValueError("duplicate authoritative writer target")
        if {writer.kind for writer in self.writers} != set(WRITER_KINDS):
            raise ValueError("all authoritative writer kinds must be inventoried")
        return self


class OdooSlotReadiness(FrozenRecord):
    binding: PublicSiteBinding
    slot_id: str
    image: str
    host: str
    observed_at: datetime
    evidence_id: str = Field(min_length=1)
    runtime_identity: RuntimeIdentity
    registry: Literal[True]
    pending_modules: Literal[False]
    render: Literal[True]
    assets: Literal[True]
    browser: Literal[True]
    side_effects_fenced: Literal[True]
    retained_previous_assets: Literal[True]


class OdooRouteObservation(FrozenRecord):
    route_id: str
    host: str
    target_url: str
    slot_id: str
    binding: PublicSiteBinding
    evidence_id: str = Field(min_length=1)
    # Read applied proxy authority and public runtime identity independently.
    applied: Literal[True]
    authoritative_router_count: Literal[1]
    public_health: HealthcheckEvidence | None = None

    @property
    def runtime_identity(self) -> RuntimeIdentity | None:
        return self.public_health.observed_runtime_identity if self.public_health else None


class OdooWebsiteSettingsReceipt(FrozenRecord):
    binding: PublicSiteBinding
    image: str
    settings_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    startup_settings_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    committed: Literal[True]


class OdooMaintenanceReceipt(FrozenRecord):
    release_id: str
    binding: PublicSiteBinding
    image: str
    candidate_manifest_sha256: str
    credential_commit_id: str = Field(min_length=1)
    credential_commit_verified_before_registry: Literal[True]
    install_modules: tuple[str, ...]
    update_modules: tuple[str, ...]
    module_readback: Literal[True]
    evidence_id: str = Field(min_length=1)
    website_settings: OdooWebsiteSettingsReceipt | None = None


class OdooRecoveryTuple(FrozenRecord):
    binding: PublicSiteBinding
    image: str
    logical_record_id: str = Field(min_length=1)
    infrastructure_record_id: str = Field(min_length=1)
    database_dump_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    filestore_archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    recovery_point_at: datetime
    coherence_verified: Literal[True]
    restore_verified: Literal[True]
    independent_backup_verified: Literal[True]


CutoverStep = Literal[
    "preparing",
    "prepared",
    "pausing",
    "backing_up",
    "maintaining",
    "ready",
    "switching",
    "resuming",
    "draining",
    "complete",
    "rolling_back",
    "rolled_back",
]


class OdooCutoverRecord(FrozenRecord):
    record_id: str
    plan: OdooCutoverPlan
    step: CutoverStep = "preparing"
    began_at: datetime
    updated_at: datetime
    ended_at: datetime | None = None
    rollback_started_at: datetime | None = None
    public_site: PreparedPublicSite | None = None
    pause: PublicPauseRecord | None = None
    backup: OdooRecoveryTuple | None = None
    maintenance: OdooMaintenanceReceipt | None = None
    rollback_website_settings: OdooWebsiteSettingsReceipt | None = None
    readiness: OdooSlotReadiness | None = None
    route: OdooRouteObservation | None = None
    error: str = ""
    old_connections_drained: bool = False
    previous_slot_retained: bool = True
