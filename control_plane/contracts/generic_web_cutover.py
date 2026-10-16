"""Provider-neutral two-slot intent and durable compatible-release observations."""

from datetime import datetime
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.artifact_release_compatibility import ReleaseDatabaseCompatibility
from control_plane.contracts.prepared_public_site import FrozenRecord
from control_plane.contracts.promotion_record import HealthcheckEvidence
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.contracts.deploy_reference import validate_provider_deploy_reference
from control_plane.release_compatibility import classify_release_database_compatibility


class WebSlotCoordinates(FrozenRecord):
    slot_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    target_id: str = Field(min_length=1)
    target_type: Literal["application", "compose"]
    private_url: str

    @model_validator(mode="after")
    def origin(self) -> Self:
        url = urlsplit(self.private_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.path not in {"", "/"}
            or url.query
            or url.fragment
        ):
            raise ValueError("slot requires an explicit private HTTP origin")
        return self


class GenericWebCutoverLane(FrozenRecord):
    route_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    host: str = Field(pattern=r"^[a-zA-Z0-9.-]+$")
    slots: tuple[WebSlotCoordinates, WebSlotCoordinates]
    runtime_settings_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    # Explicit inventory of singleton/background writers; empty means none.
    writers: tuple[str, ...]

    @model_validator(mode="after")
    def distinct(self) -> Self:
        for field in ("slot_id", "target_id", "private_url"):
            if getattr(self.slots[0], field) == getattr(self.slots[1], field):
                raise ValueError("two distinct private app targets are required")
        if self.slots[0].target_type != self.slots[1].target_type:
            raise ValueError("both slots must use the same target category")
        if len(set(self.writers)) != len(self.writers) or any(not w.strip() for w in self.writers):
            raise ValueError("writer inventory must contain distinct nonempty identities")
        return self


class WebSlot(WebSlotCoordinates):
    image: str = Field(pattern=r"^.+@sha256:[0-9a-f]{64}$")
    deploy_reference: str = ""
    runtime_identity: RuntimeIdentity

    @model_validator(mode="after")
    def immutable_reference(self) -> Self:
        if self.deploy_reference:
            validate_provider_deploy_reference(
                artifact_id=self.image,
                deploy_reference=self.deploy_reference,
                label="two-slot candidate",
            )
        return self


class GenericWebCutoverPlan(FrozenRecord):
    release_id: str = Field(min_length=1)
    lane: GenericWebCutoverLane
    production: ArtifactIdentityManifest
    candidate: ArtifactIdentityManifest
    compatibility: ReleaseDatabaseCompatibility
    old: WebSlot
    new: WebSlot

    @model_validator(mode="after")
    def exact_pair(self) -> Self:
        if self.compatibility != classify_release_database_compatibility(
            production=self.production, candidate=self.candidate
        ):
            raise ValueError("classification differs from the exact verified artifact pair")
        if {
            WebSlotCoordinates.model_validate(
                s.model_dump(include=set(WebSlotCoordinates.model_fields))
            )
            for s in (self.old, self.new)
        } != set(self.lane.slots):
            raise ValueError("slot targets differ from persisted lane authority")
        if self.old.slot_id == self.new.slot_id:
            raise ValueError("candidate cannot replace the serving slot")
        for slot, artifact in ((self.old, self.production), (self.new, self.candidate)):
            identity = slot.runtime_identity
            if (
                slot.image != f"{artifact.image.repository}@{artifact.image.digest}"
                or identity.image_reference != slot.image
                or identity.image_digest != artifact.image.digest
                or identity.source_git_ref != artifact.source_commit
                or identity.artifact_id != artifact.artifact_id
                or identity.slot != slot.slot_id
            ):
                raise ValueError("slot runtime does not match its immutable artifact")
        if (
            self.new.runtime_identity.release_id != self.release_id
            or not self.old.runtime_identity.product
            or any(
                getattr(self.old.runtime_identity, f) != getattr(self.new.runtime_identity, f)
                for f in ("product", "context", "instance", "database_uuid")
            )
        ):
            raise ValueError("slot release or authoritative lane differs")
        return self


class WebSlotReadiness(FrozenRecord):
    slot_id: str
    runtime_identity: RuntimeIdentity
    image: str
    host: str
    runtime_settings_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime
    evidence_id: str = Field(min_length=1)
    render: Literal[True]
    assets: Literal[True]
    warmed: Literal[True]
    first_request_ms: float = Field(ge=0)
    warmed_request_ms: float = Field(ge=0)
    background_workers_fenced: bool
    retained_previous_assets: Literal[True]


class WebRouteObservation(FrozenRecord):
    route_id: str
    host: str
    target_url: str
    slot_id: str
    evidence_id: str = Field(min_length=1)
    applied: Literal[True]
    authoritative_router_count: Literal[1]
    public_health: HealthcheckEvidence


WebCutoverStep = Literal[
    "preparing",
    "ready",
    "switching",
    "resuming",
    "draining",
    "complete",
    "rolling_back",
    "rolled_back",
    "blocked",
]


class GenericWebCutoverRecord(FrozenRecord):
    record_id: str
    plan: GenericWebCutoverPlan
    step: WebCutoverStep = "preparing"
    began_at: datetime
    updated_at: datetime
    ended_at: datetime | None = None
    rollback_started_at: datetime | None = None
    readiness: WebSlotReadiness | None = None
    route: WebRouteObservation | None = None
    old_connections_drained: bool = False
    previous_slot_retained: Literal[True] = True
    error: str = ""
