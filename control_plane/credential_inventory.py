from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord

INVENTORY_ROUTE = "/v1/admin/credential-inventory"
INVENTORY_SCOPE = "credential-inventory"
COLLECTION_MAX_AGE = timedelta(hours=26)
MetadataName = Annotated[str, Field(pattern=r"^[A-Za-z0-9_./:@+~=-]{1,512}$")]
SourceName = Annotated[str, Field(pattern=r"^[A-Za-z0-9_./:@+~=-]{1,256}$")]
FindingKind = Literal[
    "unknown_key_found", "unclassified_file_found", "missing", "presence_unknown", "rotation_due"
]
CollectionFailure = Literal[
    "collector_unavailable",
    "collector_timeout",
    "collector_failed",
    "report_too_large",
    "invalid_snapshot",
]


class MetadataModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CredentialMetadata(MetadataModel):
    name: MetadataName
    restrictions: list[Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")]] = Field(
        max_length=64
    )
    purpose: Annotated[str, Field(pattern=r"^[A-Za-z0-9 .,;:/_()@+-]{1,200}$")]
    rotation_due: str

    @field_validator("rotation_due", mode="before")
    @classmethod
    def normalize_rotation(cls, value: object) -> object:
        return "Unknown" if value is None or value == "" else value

    @model_validator(mode="after")
    def validate_rotation(self) -> Self:
        if self.rotation_due != "Unknown":
            date.fromisoformat(self.rotation_due)
        return self


class CredentialObservation(MetadataModel):
    source: SourceName
    status: Literal["complete", "partial", "unknown"]
    reason: Annotated[str, Field(pattern=r"^[A-Za-z0-9_./:@+~=-]{0,256}$")]
    trace: Annotated[str, Field(pattern=r"^[A-Za-z0-9_./:@+~=-]{0,256}$")] = ""
    records: list[CredentialMetadata] = Field(max_length=1000)


class CredentialFinding(MetadataModel):
    source: SourceName
    name: MetadataName
    kind: FindingKind


class CredentialSnapshot(MetadataModel):
    generated_at: AwareDatetime
    observations: list[CredentialObservation] = Field(min_length=1, max_length=200)
    findings: list[CredentialFinding] = Field(max_length=10000)

    @model_validator(mode="after")
    def validate_coverage(self) -> Self:
        sources = {item.source: item for item in self.observations}
        if len(sources) != len(self.observations):
            raise ValueError("Duplicate inventory sources.")
        if sum(len(item.records) for item in self.observations) > 10000:
            raise ValueError("Inventory is too large.")
        for finding in self.findings:
            if finding.source not in sources:
                raise ValueError("Finding has no source coverage.")
            if finding.kind == "missing" and sources[finding.source].status != "complete":
                raise ValueError("Incomplete coverage cannot establish absence.")
        return self


class CredentialCollection(MetadataModel):
    attempted_at: AwareDatetime
    status: Literal["collected", "failed"]
    snapshot: CredentialSnapshot | None
    failure_reason: CollectionFailure | None = None

    @field_serializer("attempted_at")
    def serialize_attempted_at(self, value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds")

    @model_validator(mode="after")
    def validate_collection(self) -> Self:
        if (self.status == "collected") != (self.snapshot is not None):
            raise ValueError("Collection status and snapshot disagree.")
        if self.status == "collected" and self.failure_reason is not None:
            raise ValueError("A collected snapshot cannot carry a failure reason.")
        if self.snapshot and self.snapshot.generated_at > self.attempted_at:
            raise ValueError("Snapshot is newer than its collection attempt.")
        return self


class CredentialInventoryView(BaseModel):
    collection_status: Literal["never", "collected", "failed"]
    last_attempt_at: datetime | None
    received_at: datetime | None
    stale: bool
    fresh_until: datetime | None
    failure_reason: CollectionFailure | None
    coverage: Literal["complete", "partial", "unknown"]
    snapshot: CredentialSnapshot | None
    findings: list[CredentialFinding]


def collection_digest(collection: CredentialCollection) -> str:
    return hashlib.sha256(collection.model_dump_json().encode()).hexdigest()


def collection_receipt_id(collection: CredentialCollection) -> str:
    return f"credential-inventory-{collection_digest(collection)}"


def inventory_receipt(
    collection: CredentialCollection, *, now: datetime, trace_id: str
) -> LaunchplaneIdempotencyRecord:
    if collection.attempted_at > now + timedelta(minutes=5):
        raise ValueError("Collection timestamp is in the future.")
    fingerprint = collection_digest(collection)
    return LaunchplaneIdempotencyRecord(
        record_id=collection_receipt_id(collection),
        scope=INVENTORY_SCOPE,
        route_path=INVENTORY_ROUTE,
        idempotency_key=fingerprint,
        request_fingerprint=fingerprint,
        created_at=collection.attempted_at.astimezone(timezone.utc).isoformat(),
        recorded_at=now.isoformat(),
        response_status_code=200,
        response_trace_id=trace_id,
        response_payload=collection.model_dump(mode="json"),
    )


def inventory_view(
    latest: LaunchplaneIdempotencyRecord | None,
    successful: LaunchplaneIdempotencyRecord | None,
    *,
    now: datetime,
) -> CredentialInventoryView:
    attempt = (
        CredentialCollection.model_validate_json(json.dumps(latest.response_payload))
        if latest
        else None
    )
    snapshot = (
        CredentialCollection.model_validate_json(json.dumps(successful.response_payload)).snapshot
        if successful
        else None
    )
    findings = list(snapshot.findings) if snapshot else []
    statuses = {item.status for item in snapshot.observations} if snapshot else set()
    coverage: Literal["complete", "partial", "unknown"] = "unknown"
    if statuses == {"complete"}:
        coverage = "complete"
    elif statuses and statuses != {"unknown"}:
        coverage = "partial"
    return CredentialInventoryView(
        collection_status=attempt.status if attempt else "never",
        last_attempt_at=attempt.attempted_at if attempt else None,
        received_at=datetime.fromisoformat(latest.recorded_at) if latest else None,
        stale=snapshot is None or now - snapshot.generated_at > COLLECTION_MAX_AGE,
        fresh_until=snapshot.generated_at + COLLECTION_MAX_AGE if snapshot else None,
        failure_reason=attempt.failure_reason if attempt else None,
        coverage=coverage,
        snapshot=snapshot,
        findings=findings,
    )
