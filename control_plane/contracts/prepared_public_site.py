"""Provider-neutral inputs and immutable evidence for prepared public serving."""

from datetime import datetime
from typing import Literal, Self
from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

WriterKind = Literal["web", "cron", "queue", "mail", "integration", "asset_gc"]
WRITER_KINDS: tuple[WriterKind, ...] = ("web", "cron", "queue", "mail", "integration", "asset_gc")


class FrozenRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PublicSiteBinding(FrozenRecord):
    product_id: str = Field(min_length=1)
    environment_id: str = Field(min_length=1)
    release_id: str = Field(min_length=1)
    artifact_id: str = Field(min_length=1)
    image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    database: str = Field(min_length=1)


class WriterTarget(FrozenRecord):
    writer_id: str = Field(min_length=1)
    kind: WriterKind


def validate_public_path(value: str) -> str:
    parts = urlsplit(value)
    if any(
        key.casefold() in {"csrf", "csrf_token", "session_id", "access_token", "token", "password"}
        for key, _ in parse_qsl(parts.query)
    ):
        raise ValueError("session and credential parameters cannot enter public coverage")
    if (
        not value.startswith("/")
        or value.startswith("//")
        or parts.scheme
        or parts.netloc
        or parts.fragment
        or "\\" in value
        or "%" in value
        or any(ord(char) <= 32 for char in value)
        or any(part in {".", ".."} for part in parts.path.split("/"))
    ):
        raise ValueError("public paths must be canonical local paths without encoded aliases")
    return value


class PublicSitePlan(FrozenRecord):
    binding: PublicSiteBinding
    origin: str
    public_routes: tuple[str, ...] = Field(min_length=1)
    retained_assets: tuple[str, ...] = ()
    excluded_prefixes: tuple[str, ...] = Field(min_length=1)
    writers: tuple[WriterTarget, ...] = Field(min_length=1)
    notice: str = Field(min_length=1)
    max_resources: int = Field(default=2000, ge=1, le=10000)
    max_resource_bytes: int = Field(default=20_000_000, ge=1)
    max_total_bytes: int = Field(default=200_000_000, ge=1)

    @field_validator("origin")
    @classmethod
    def valid_origin(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in {"https", "http"} or not parts.netloc or parts.username:
            raise ValueError("an explicit HTTP origin is required")
        if parts.path or parts.query or parts.fragment:
            raise ValueError("origin must not include a path, query or fragment")
        return value

    @model_validator(mode="after")
    def validate_inventory(self) -> Self:
        for values in (self.public_routes, self.retained_assets, self.excluded_prefixes):
            for value in values:
                validate_public_path(value)
            if len(set(values)) != len(values):
                raise ValueError("duplicate coverage paths")
        if set(self.public_routes) & set(self.retained_assets):
            raise ValueError("page and asset inventories overlap")
        if any(self.excluded(path) for path in (*self.public_routes, *self.retained_assets)):
            raise ValueError("personal/editor paths cannot be public coverage")
        if {writer.kind for writer in self.writers} != set(WRITER_KINDS):
            raise ValueError("writer inventory must cover every authoritative writer kind")
        if len({writer.writer_id for writer in self.writers}) != len(self.writers):
            raise ValueError("writer identities must be unique")
        return self

    def excluded(self, path: str) -> bool:
        path = urlsplit(path).path
        return any(
            path == prefix.rstrip("/") or path.startswith(prefix.rstrip("/") + "/")
            for prefix in self.excluded_prefixes
        )


class CapturedPublicResponse(FrozenRecord):
    """The capture adapter attests an anonymous response from this exact runtime."""

    binding: PublicSiteBinding
    anonymous: Literal[True]
    public: Literal[True]
    status: int
    content_type: str
    body: bytes = b""
    headers: dict[str, str] = Field(default_factory=dict)


class PreparedResource(FrozenRecord):
    path: str
    status: int
    content_type: str
    body_base64: str
    headers: tuple[tuple[str, str], ...] = ()


class PreparedPublicSite(FrozenRecord):
    plan: PublicSitePlan
    prepared_at: datetime
    resources: tuple[PreparedResource, ...]
    content_digest: str


class WriterObservation(FrozenRecord):
    binding: PublicSiteBinding
    pause_id: str
    writer: WriterTarget
    fenced: bool
    active_jobs: int = Field(ge=0)
    evidence_id: str = Field(min_length=1)


class PublicPauseRecord(FrozenRecord):
    pause_id: str
    binding: PublicSiteBinding
    content_digest: str
    began_at: datetime
    ended_at: datetime | None = None
    duration_seconds: float | None = None
    state: Literal["fencing", "drained", "resuming", "complete"] = "fencing"
    served_mode: Literal["unverified", "prepared-public"] = "unverified"
    reduced_service: Literal[True] = True
    writers: tuple[WriterObservation, ...] = ()
    recovery_binding: PublicSiteBinding | None = None


class PublicRecoveryObservation(FrozenRecord):
    binding: PublicSiteBinding
    pause_id: str
    serving_evidence_id: str = Field(min_length=1)
    writer_owners: tuple[tuple[str, int], ...]
