"""Forward-only build changes; rollback callers use their existing pinned authority."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import TYPE_CHECKING, Protocol, cast
from urllib.parse import quote

import click

from control_plane.build_provenance import BuildProvenanceError, BuildProvenanceTransport
from control_plane.contracts.artifact_identity import ArtifactIdentityManifest, ArtifactSourceBuild
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord

if TYPE_CHECKING:
    from control_plane.product_reconcile import ProductReconcileStore
    from control_plane.contracts.promotion_record import RecordFailure

LANE_MOVEMENT_REFUSALS: dict[str, str] = {
    "ancestor_build": "The requested build is an ancestor of the lane's current build.",
    "older_artifact": "The requested artifact predates the lane's current artifact.",
    "source_order_unverified": "The source history does not prove a forward build change.",
    "source_order_unavailable": "The source history could not be read before the build change.",
    "build_order_unverified": "The build records do not prove a newer artifact for this commit.",
    "preview_source_unverified": "The preview's current source could not be resolved.",
}


class LaneMovementRefused(click.ClickException):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"Lane build change refused: {code}. Use the explicit rollback path.")

    def record_failure(self) -> RecordFailure:
        from control_plane.contracts.promotion_record import RecordFailure

        return RecordFailure(
            code=f"lane_movement.{self.code}", description=LANE_MOVEMENT_REFUSALS[self.code]
        )


@dataclass(frozen=True)
class LaneBuild:
    artifact_id: str = ""
    commit: str = ""
    image: str = ""
    source_build: ArtifactSourceBuild | None = None


class LaneMovementStore(Protocol):
    def read_environment_inventory(
        self, *, context_name: str, instance_name: str
    ) -> EnvironmentInventory: ...

    def read_artifact_manifest(self, artifact_id: str) -> ArtifactIdentityManifest: ...


def current_lane_build(record_store: object, *, context: str, instance: str) -> LaneBuild:
    store = cast(LaneMovementStore, record_store)
    try:
        inventory = store.read_environment_inventory(context_name=context, instance_name=instance)
    except FileNotFoundError:
        return LaneBuild()
    identity = inventory.runtime_identity
    return LaneBuild(
        artifact_id=(
            identity.artifact_id
            if identity
            else inventory.artifact_identity.artifact_id
            if inventory.artifact_identity
            else ""
        ),
        commit=identity.source_git_ref if identity else inventory.source_git_ref,
        image=identity.image_reference if identity else "",
    )


def require_forward_build(
    *,
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    current: LaneBuild,
    desired: LaneBuild,
    transport: BuildProvenanceTransport | None = None,
) -> None:
    if not current.artifact_id and not current.commit:
        return
    if (current.artifact_id and current.artifact_id == desired.artifact_id) or (
        current.image and current.image == desired.image
    ):
        return
    if not all(re.fullmatch(r"[0-9a-fA-F]{40}", sha) for sha in (current.commit, desired.commit)):
        raise LaneMovementRefused("source_order_unverified")
    if current.commit.lower() == desired.commit.lower():
        # A same-commit rebuild can change shared inputs. Never guess its age from
        # an artifact name, digest, or the time it was first recorded locally.
        store = cast(LaneMovementStore, record_store)
        try:
            before = (
                current.source_build
                or store.read_artifact_manifest(current.artifact_id).source_build
            )
            after = (
                desired.source_build
                or store.read_artifact_manifest(desired.artifact_id).source_build
            )
        except (FileNotFoundError, AttributeError):
            raise LaneMovementRefused("build_order_unverified") from None
        if before is None or after is None or before.repository != after.repository:
            raise LaneMovementRefused("build_order_unverified")
        if (after.run_id, after.run_attempt) <= (before.run_id, before.run_attempt):
            raise LaneMovementRefused("older_artifact")
        return
    try:
        if transport is None:
            # Keep the existing App/read-token resolver as the one authority.
            from control_plane.product_reconcile import (
                ProductReconcileError,
                resolve_build_provenance_transport,
            )

            try:
                transport = resolve_build_provenance_transport(record_store, profile)
            except ProductReconcileError as error:
                raise LaneMovementRefused("source_order_unavailable") from error
        repository = "/".join(quote(part, safe="") for part in profile.repository.split("/"))
        comparison = transport.get_json(
            f"/repos/{repository}/compare/{current.commit}...{desired.commit}"
        )
        if not isinstance(comparison, dict):
            raise LaneMovementRefused("source_order_unverified")
        base = comparison.get("base_commit")
        merge_base = comparison.get("merge_base_commit")
        if not isinstance(base, dict) or base.get("sha") != current.commit:
            raise LaneMovementRefused("source_order_unverified")
        if comparison.get("status") == "behind":
            raise LaneMovementRefused("ancestor_build")
        if (
            comparison.get("status") != "ahead"
            or not isinstance(merge_base, dict)
            or merge_base.get("sha") != current.commit
        ):
            raise LaneMovementRefused("source_order_unverified")
    except LaneMovementRefused:
        raise
    except (BuildProvenanceError, click.ClickException, OSError, ValueError) as error:
        raise LaneMovementRefused("source_order_unavailable") from error


def require_forward_lane_build(
    *,
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    context: str,
    instance: str,
    desired: LaneBuild,
) -> None:
    require_forward_build(
        record_store=record_store,
        profile=profile,
        current=current_lane_build(record_store, context=context, instance=instance),
        desired=desired,
    )


def require_forward_preview_build(
    *,
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    preview_slug: str,
    desired: LaneBuild,
) -> None:
    from control_plane.product_reconcile import _current_preview

    # Read the serving generation, using the same driver-specific projection as
    # planning. Direct apply and retries cannot bypass a newer generation.
    pattern = re.escape(profile.preview.slug_template).replace(r"\{number\}", r"(\d+)")
    match = re.fullmatch(pattern, preview_slug)
    number = int(match.group(1)) if match else None
    if number is None:
        raise LaneMovementRefused("preview_source_unverified")
    current, _ = _current_preview(
        record_store=cast("ProductReconcileStore", record_store),
        profile=profile,
        preview_context=profile.preview.context,
        pull_request_number=number,
    )
    require_forward_build(
        record_store=record_store,
        profile=profile,
        current=LaneBuild(
            commit=str(current["current_head_sha"]),
            image=f"{profile.image.repository}@{current['current_image_digest']}"
            if current["current_image_digest"]
            else "",
        ),
        desired=desired,
    )
