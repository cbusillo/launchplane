"""Forward-only build changes; rollback callers use their existing pinned authority."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
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
    observed_at: str = ""
    pull_request_number: int | None = None


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
        observed_at=inventory.updated_at,
    )


def require_forward_build(
    *,
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    current: LaneBuild,
    desired: LaneBuild,
    transport: BuildProvenanceTransport | None = None,
) -> None:
    store = cast(LaneMovementStore, record_store)
    # Older inventory records may abbreviate the ref; only its own immutable
    # manifest can supply the full commit, never a guessed GitHub abbreviation.
    if current.commit and not re.fullmatch(r"[0-9a-fA-F]{40}", current.commit):
        try:
            manifest = store.read_artifact_manifest(current.artifact_id)
        except (FileNotFoundError, AttributeError):
            pass
        else:
            if manifest.source_commit.lower().startswith(current.commit.lower()):
                current = replace(current, commit=manifest.source_commit)
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
        _require_newer_artifact(record_store, profile, current, desired, transport)
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
        if comparison.get("status") == "diverged" and current.pull_request_number is not None:
            # Following an open PR also admits rebases. Its freshly verified
            # artifact must be newer than the serving generation's build.
            _require_newer_artifact(record_store, profile, current, desired, transport)
            return
        if (
            comparison.get("status") != "ahead"
            or not isinstance(merge_base, dict)
            or merge_base.get("sha") != current.commit
        ):
            raise LaneMovementRefused("source_order_unverified")
        # Source order and artifact order are distinct: a late rebuild of an
        # ancestor must not authorize an older artifact of a descendant.
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
            before = after = None
        if before is not None and after is not None:
            _require_newer_artifact(
                record_store,
                profile,
                replace(current, source_build=before),
                replace(desired, source_build=after),
                transport,
            )
    except LaneMovementRefused:
        raise
    except (BuildProvenanceError, click.ClickException, OSError, ValueError) as error:
        raise LaneMovementRefused("source_order_unavailable") from error


def _require_newer_artifact(
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    current: LaneBuild,
    desired: LaneBuild,
    transport: BuildProvenanceTransport | None,
) -> None:
    store = cast(LaneMovementStore, record_store)

    def source(build: LaneBuild) -> ArtifactSourceBuild | None:
        if build.source_build is not None:
            return build.source_build
        try:
            return store.read_artifact_manifest(build.artifact_id).source_build
        except (FileNotFoundError, AttributeError):
            return None

    before, after = source(current), source(desired)
    if before is not None and after is not None:
        if before.repository != after.repository or after.repository != profile.repository:
            raise LaneMovementRefused("build_order_unverified")
        if (after.run_id, after.run_attempt) <= (before.run_id, before.run_attempt):
            raise LaneMovementRefused("older_artifact")
        return
    if not current.observed_at:
        raise LaneMovementRefused("build_order_unverified")
    try:
        if transport is None:
            from control_plane.product_reconcile import resolve_build_provenance_transport

            transport = resolve_build_provenance_transport(record_store, profile)
        repository = "/".join(quote(part, safe="") for part in profile.repository.split("/"))
        if after is None:
            from control_plane.build_provenance import verify_generic_web_build

            identity = transport.get_json(f"/repos/{repository}")
            if not isinstance(identity, dict) or not identity.get("id"):
                raise LaneMovementRefused("build_order_unverified")
            verified = verify_generic_web_build(
                transport=transport,
                repository=profile.repository,
                repository_id=str(identity["id"]),
                commit=desired.commit,
                purpose="preview" if current.pull_request_number is not None else "release",
                image_repository=profile.image.repository,
                pull_request_number=current.pull_request_number,
            )
            if verified.image_reference != desired.image:
                raise LaneMovementRefused("build_order_unverified")
            after = verified.source_build
        if after.repository != profile.repository:
            raise LaneMovementRefused("build_order_unverified")
        run = transport.get_json(
            f"/repos/{repository}/actions/runs/{after.run_id}/attempts/{after.run_attempt}"
        )
        if (
            not isinstance(run, dict)
            or run.get("head_sha") != desired.commit
            or run.get("id") != after.run_id
            or run.get("run_attempt") != after.run_attempt
            or run.get("conclusion") != "success"
        ):
            raise LaneMovementRefused("build_order_unverified")
        # The serving generation is an upper bound on its build's start time.
        # A verified build that started later is provably newer, even when old
        # generic-web records retained only an image digest.
        started = datetime.fromisoformat(str(run.get("run_started_at", "")).replace("Z", "+00:00"))
        observed = datetime.fromisoformat(current.observed_at.replace("Z", "+00:00"))
        if started.tzinfo is None or observed.tzinfo is None or started <= observed:
            raise LaneMovementRefused("build_order_unverified")
    except LaneMovementRefused:
        raise
    except (BuildProvenanceError, click.ClickException, OSError) as error:
        raise LaneMovementRefused("source_order_unavailable") from error
    except (ValueError, TypeError) as error:
        raise LaneMovementRefused("build_order_unverified") from error


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
        for_movement=True,
    )
    require_forward_build(
        record_store=record_store,
        profile=profile,
        current=LaneBuild(
            artifact_id=str(current.get("current_artifact_id", "")),
            commit=str(current["current_head_sha"]),
            image=f"{profile.image.repository}@{current['current_image_digest']}"
            if current["current_image_digest"]
            else "",
            observed_at=str(current.get("current_observed_at", "")),
            pull_request_number=number,
        ),
        desired=desired,
    )
