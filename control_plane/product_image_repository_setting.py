"""Move a product's image repository to the GHCR package named after its repository.

A product publishes its own images (DIRECTION.md), to the package named after
its repository with that repository's workflow token. Changing the profile's
image repository changes which artifacts its lanes accept from callers; the
artifacts Launchplane already recorded, such as a rollback target, stay valid
(``normalize_generic_web_artifact_id`` with ``recorded``).
"""

from __future__ import annotations

import re
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, model_validator

from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
)

ProductImageRepositoryMode = Literal["dry-run", "apply"]
PRODUCT_IMAGE_REPOSITORY_SOURCE: Literal["service:product-image-repository"] = (
    "service:product-image-repository"
)
_GHCR_REPOSITORY_PATTERN = re.compile(r"ghcr\.io/[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*")


class ProductImageRepositoryRefusal(ValueError):
    """The requested image repository is not one this route may set."""


class ProductImageRepositoryChangedError(ValueError):
    """The profile's image repository is no longer the one the dry run showed."""


class ProductImageRepositoryInventoryStore(Protocol):
    def read_environment_inventory(
        self, *, context_name: str, instance_name: str
    ) -> EnvironmentInventory: ...


class ProductImageRepositoryApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    mode: ProductImageRepositoryMode = "dry-run"
    image_repository: str
    expected_image_repository: str = ""
    reason: str

    @model_validator(mode="after")
    def _validate_request(self) -> "ProductImageRepositoryApplyRequest":
        self.image_repository = self.image_repository.strip().rstrip("/")
        self.expected_image_repository = self.expected_image_repository.strip().rstrip("/")
        self.reason = self.reason.strip()
        if not self.reason:
            raise ValueError("Product image repository request requires reason.")
        if not _GHCR_REPOSITORY_PATTERN.fullmatch(self.image_repository):
            raise ValueError(
                "Product image repository must be an untagged lowercase "
                "ghcr.io/<owner>/<name> repository."
            )
        if self.mode == "apply" and not self.expected_image_repository:
            raise ValueError(
                "Product image repository apply requires expected_image_repository from "
                "the dry run."
            )
        return self


class ProductImageRepositoryLaneArtifact(BaseModel):
    """What a lane runs now, and whether it is already in the new repository."""

    model_config = ConfigDict(extra="forbid")

    instance: str
    context: str
    current_artifact_id: str = ""
    in_new_repository: bool = False


class ProductImageRepositoryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    mode: ProductImageRepositoryMode
    product: str
    repository: str
    image_repository_before: str
    image_repository_after: str
    changed: bool
    applied: bool = False
    lanes: tuple[ProductImageRepositoryLaneArtifact, ...] = ()
    reason: str
    source_label: Literal["service:product-image-repository"] = PRODUCT_IMAGE_REPOSITORY_SOURCE
    profile_updated_at_before: str
    profile_updated_at_after: str = ""


def repository_named_image_repository(profile: LaunchplaneProductProfileRecord) -> str:
    """The GHCR package named after the product's repository."""
    return f"ghcr.io/{profile.repository.strip().lower()}"


def build_product_image_repository_plan(
    *,
    record_store: ProductImageRepositoryInventoryStore,
    profile: LaunchplaneProductProfileRecord,
    request: ProductImageRepositoryApplyRequest,
) -> ProductImageRepositoryPlan:
    """Refuse anything but the repository-named package; for apply, the dry run's starting point."""
    expected = repository_named_image_repository(profile)
    if request.image_repository != expected:
        raise ProductImageRepositoryRefusal(
            f"Product {profile.product} may only publish to the package named after its "
            f"repository, {expected}."
        )
    before = profile.image.repository.strip().rstrip("/")
    if request.mode == "apply" and request.expected_image_repository != before:
        raise ProductImageRepositoryChangedError(
            "The product's image repository changed since the dry run; run a new dry run."
        )
    return ProductImageRepositoryPlan(
        mode=request.mode,
        product=profile.product,
        repository=profile.repository,
        image_repository_before=before,
        image_repository_after=request.image_repository,
        changed=before != request.image_repository,
        lanes=tuple(
            _lane_artifact(
                record_store=record_store,
                instance=lane.instance,
                context=lane.context,
                image_repository=request.image_repository,
            )
            for lane in profile.lanes
        ),
        reason=request.reason,
        profile_updated_at_before=profile.updated_at,
    )


def updated_product_image_repository_profile(
    *,
    profile: LaunchplaneProductProfileRecord,
    image_repository: str,
    updated_at: str,
) -> LaunchplaneProductProfileRecord:
    updated_profile = profile.model_copy(
        update={
            "image": ProductImageProfile(repository=image_repository),
            "updated_at": updated_at,
            "source": PRODUCT_IMAGE_REPOSITORY_SOURCE,
        }
    )
    return LaunchplaneProductProfileRecord.model_validate(updated_profile.model_dump(mode="json"))


def _lane_artifact(
    *,
    record_store: ProductImageRepositoryInventoryStore,
    instance: str,
    context: str,
    image_repository: str,
) -> ProductImageRepositoryLaneArtifact:
    try:
        inventory = record_store.read_environment_inventory(
            context_name=context, instance_name=instance
        )
    except FileNotFoundError:
        return ProductImageRepositoryLaneArtifact(instance=instance, context=context)
    artifact_id = (
        inventory.runtime_identity.artifact_id.strip()
        if inventory.runtime_identity is not None
        else ""
    )
    return ProductImageRepositoryLaneArtifact(
        instance=instance,
        context=context,
        current_artifact_id=artifact_id,
        in_new_repository=artifact_id.startswith(f"{image_repository}@"),
    )
