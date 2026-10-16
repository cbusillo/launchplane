"""Service-owned image retention planning. No delete operation is supported yet."""

from datetime import UTC, datetime, timedelta
import re
from typing import Literal
from urllib.parse import quote

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from control_plane.build_provenance import BuildProvenanceTransport
from control_plane.contracts.protected_artifacts import ProtectedArtifactSet, ProtectedArtifactStore

_GHCR = re.compile(r"ghcr\.io/([a-z0-9][a-z0-9._-]*)/([a-z0-9][a-z0-9._/-]*)")
_BUILD_TAG = re.compile(r"(?:sha-[0-9a-f]{40}|pr-[1-9][0-9]*(?:-sha-[0-9a-f]{7,40})?)")
_MAX_PAGES = 10


class RetentionVersion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version_id: int = Field(gt=0, strict=True)
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    tags: tuple[str, ...]
    created_at: AwareDatetime


class ArtifactRetentionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["dry-run"] = "dry-run"
    status: Literal["available", "unavailable"]
    image_repository: str
    observed_at: str
    permission: Literal["unconfirmed"] = "unconfirmed"
    candidates: tuple[int, ...] = ()
    retained: tuple[int, ...] = ()
    reason: str = ""


def plan_retention(
    *,
    image_repository: str,
    protected: ProtectedArtifactSet,
    versions: tuple[RetentionVersion, ...],
    now: datetime,
    retain_count: int = 20,
    minimum_age_days: int = 30,
) -> ArtifactRetentionPlan:
    """Only old build-tagged roots absent from complete protection can be candidates.

    Untagged manifests include architecture children and attestations; keep them.
    A plan is evidence, never authority to replay deletions against a later inventory.
    """
    if now.tzinfo is None or retain_count < 1 or minimum_age_days < 1:
        raise ValueError("Retention requires a timezone and positive retention budgets.")
    if protected.warnings:
        return ArtifactRetentionPlan(
            status="unavailable",
            image_repository=image_repository,
            observed_at=now.isoformat(),
            reason="protected_inventory_incomplete",
        )
    protected_refs = set(protected.image_references)
    # A digest may be referenced through another package or a legacy identity.
    protected_digests = set(protected.image_digests)
    for reference in protected_refs:
        if "@" in reference:
            protected_digests.add(reference.rsplit("@", 1)[1])
    ordered = sorted(
        versions, key=lambda version: (version.created_at, version.version_id), reverse=True
    )
    if len({version.version_id for version in ordered}) != len(ordered):
        raise ValueError("Package inventory has duplicate versions.")
    recent = {
        version.version_id
        for version in ordered
        if any(tag.startswith("sha-") for tag in version.tags)
    }
    newest = [version.version_id for version in ordered if version.version_id in recent][
        :retain_count
    ]
    retained: list[int] = []
    candidates: list[int] = []
    for version in ordered:
        keep = (
            version.created_at.tzinfo is None
            or version.created_at > now - timedelta(days=minimum_age_days)
            or version.version_id in newest
            or version.digest in protected_digests
            or not version.tags
            or any(not _BUILD_TAG.fullmatch(tag) for tag in version.tags)
            or any(f"{image_repository}:{tag}" in protected_refs for tag in version.tags)
        )
        (retained if keep else candidates).append(version.version_id)
    return ArtifactRetentionPlan(
        status="available",
        image_repository=image_repository,
        observed_at=now.isoformat(),
        candidates=tuple(candidates),
        retained=tuple(retained),
    )


def read_package_versions(
    *, transport: BuildProvenanceTransport, image_repository: str, repository_id: str
) -> tuple[RetentionVersion, ...]:
    """Read the profile's exact package; truncated or ambiguous reads are unusable."""
    match = _GHCR.fullmatch(image_repository)
    if match is None:
        raise ValueError("Unsupported image registry.")
    owner, package = match.groups()
    owner_record = transport.get_json(f"/users/{quote(owner, safe='')}")
    if not isinstance(owner_record, dict) or owner_record.get("type") not in {
        "User",
        "Organization",
    }:
        raise ValueError("Package owner identity unavailable.")
    kind = "orgs" if owner_record["type"] == "Organization" else "users"
    path = f"/{kind}/{quote(owner, safe='')}/packages/container/{quote(package, safe='')}"
    metadata = transport.get_json(path)
    if (
        not isinstance(metadata, dict)
        or metadata.get("name") != package
        or metadata.get("package_type") != "container"
        or not isinstance(metadata.get("repository"), dict)
        or str(metadata["repository"].get("id")) != repository_id
    ):
        raise ValueError("Package is not bound to the tracked product repository.")
    versions: list[RetentionVersion] = []
    for page in range(1, _MAX_PAGES + 1):
        payload = transport.get_json(f"{path}/versions?per_page=100&page={page}")
        if not isinstance(payload, list):
            raise ValueError("Package version inventory unavailable.")
        for entry in payload:
            if not isinstance(entry, dict) or not isinstance(entry.get("metadata"), dict):
                raise ValueError("Invalid package version.")
            container = entry["metadata"].get("container")
            if not isinstance(container, dict) or not isinstance(container.get("tags"), list):
                raise ValueError("Package version has no tag inventory.")
            versions.append(
                RetentionVersion.model_validate(
                    {
                        "version_id": entry.get("id"),
                        "digest": entry.get("name"),
                        "tags": container["tags"],
                        "created_at": entry.get("created_at"),
                    }
                )
            )
        if len(payload) < 100:
            if len({version.version_id for version in versions}) != len(versions):
                raise ValueError("Package inventory changed while reading.")
            return tuple(versions)
    raise ValueError("Package version inventory is truncated.")


def product_retention_dry_run(
    record_store: ProtectedArtifactStore, *, product: str
) -> ArtifactRetentionPlan:
    from control_plane.build_provenance import BuildProvenanceError
    from control_plane.contracts.protected_artifacts import build_protected_artifact_set
    from control_plane.product_reconcile import (
        ProductReconcileError,
        product_repository_identity,
        resolve_build_provenance_transport,
    )

    profiles = [
        profile
        for profile in record_store.list_product_profile_records()
        if profile.product == product
    ]
    if len(profiles) != 1 or not profiles[0].is_active:
        raise ValueError("An active product profile is required.")
    profile = profiles[0]
    now = datetime.now(UTC)
    try:
        # Whole-store protection also covers another product sharing this image.
        protected = build_protected_artifact_set(record_store)
        identity = product_repository_identity(record_store, profile)
        transport = resolve_build_provenance_transport(record_store, profile)
        versions = read_package_versions(
            transport=transport,
            image_repository=profile.image.repository,
            repository_id=identity.repository_id,
        )
        return plan_retention(
            image_repository=profile.image.repository,
            protected=protected,
            versions=versions,
            now=now,
        )
    except (BuildProvenanceError, ValueError, OSError, RuntimeError, ProductReconcileError):
        return ArtifactRetentionPlan(
            status="unavailable",
            image_repository=profile.image.repository,
            observed_at=now.isoformat(),
            reason="package_or_protection_read_unavailable",
        )
