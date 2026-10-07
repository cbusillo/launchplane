"""Forward-only build changes; rollback callers use their existing pinned authority."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import re
from typing import TYPE_CHECKING, Literal, Protocol, cast
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
    "source_authority_unavailable": "The configured source read authority is missing or inconsistent.",
    "build_identity_unverified": "The requested image is not proved by the stated source build.",
}


class LaneMovementRefused(click.ClickException):
    def __init__(self, code: str) -> None:
        self.code = code
        guidance = (
            "Use the explicit rollback path."
            if code in {"ancestor_build", "older_artifact"}
            else "Retry with verified source/build evidence."
        )
        super().__init__(f"Lane build change refused: {code}. {guidance}")

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
    deploy_reference: str = ""
    context: str = ""


class LaneMovementStore(Protocol):
    def read_environment_inventory(
        self, *, context_name: str, instance_name: str
    ) -> EnvironmentInventory: ...

    def read_artifact_manifest(self, artifact_id: str) -> ArtifactIdentityManifest: ...

    def list_product_profile_records(self) -> tuple[LaunchplaneProductProfileRecord, ...]: ...


class SourceReadRetryRecord(Protocol):
    error_code: str
    attempt: int
    updated_at: str


def source_read_retry_ready(record: SourceReadRetryRecord, claimed_at: str) -> bool:
    if record.error_code != "lane_movement.source_order_unavailable":
        return True
    delay = min(30 * 2 ** min(max(record.attempt - 1, 0), 6), 1800)
    updated = datetime.fromisoformat(record.updated_at.replace("Z", "+00:00"))
    claimed = datetime.fromisoformat(claimed_at.replace("Z", "+00:00"))
    return claimed >= updated + timedelta(seconds=delay)


def _source_transport(
    record_store: object, profile: LaunchplaneProductProfileRecord
) -> BuildProvenanceTransport:
    from control_plane.product_reconcile import (
        ProductReconcileError,
        resolve_build_provenance_transport,
    )

    try:
        return resolve_build_provenance_transport(record_store, profile)
    except ProductReconcileError as error:
        # Missing policy/App/key is configuration, not a transient provider read.
        from control_plane.merge_train_policy_source import MergeTrainPolicyStoreMissingError
        from control_plane.product_repository_identity import ProductRepositoryIdentityRefusal

        code = (
            "source_authority_unavailable"
            if error.__cause__ is None
            or isinstance(
                error.__cause__,
                (
                    MergeTrainPolicyStoreMissingError,
                    ProductRepositoryIdentityRefusal,
                    ValueError,
                    TypeError,
                    FileNotFoundError,
                ),
            )
            else "source_order_unavailable"
        )
        raise LaneMovementRefused(code) from error


def current_lane_build(record_store: object, *, context: str, instance: str) -> LaneBuild:
    store = cast(LaneMovementStore, record_store)
    try:
        inventory = store.read_environment_inventory(context_name=context, instance_name=instance)
    except FileNotFoundError:
        return LaneBuild()
    identity = inventory.runtime_identity
    artifact_id = (
        identity.artifact_id
        if identity
        else inventory.artifact_identity.artifact_id
        if inventory.artifact_identity
        else ""
    )
    image = identity.image_reference if identity else ""
    if re.fullmatch(r".+@sha256:[0-9a-f]{64}", artifact_id):
        image = artifact_id
    return LaneBuild(
        artifact_id=artifact_id,
        commit=identity.source_git_ref if identity else inventory.source_git_ref,
        image=image,
        observed_at=inventory.updated_at,
        context=inventory.context,
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
    try:
        desired_manifest = store.read_artifact_manifest(desired.artifact_id)
    except (FileNotFoundError, AttributeError):
        desired_manifest = None
    if desired_manifest is not None and desired_manifest.source_commit != desired.commit:
        raise LaneMovementRefused("build_identity_unverified")
    if desired.deploy_reference:
        prefix = f"{profile.image.repository}:sha-"
        tag_sha = desired.deploy_reference.removeprefix(prefix)
        canonical = (
            desired.deploy_reference.startswith(prefix)
            and re.fullmatch(r"[0-9a-f]{40}", tag_sha)
            and desired.commit == tag_sha
        )
        if not canonical:
            desired = _proved_generic_build(
                record_store,
                profile,
                replace(desired, source_build=None),
                transport,
                current.pull_request_number,
            )
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
    if current.commit.lower() == desired.commit.lower() and (
        (current.artifact_id and current.artifact_id == desired.artifact_id)
        or (current.image and current.image == desired.image)
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
            transport = _source_transport(record_store, profile)
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
        if comparison.get("status") == "diverged":
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
        if profile.driver_id != "odoo" and desired.image:
            desired = _proved_generic_build(
                record_store, profile, desired, transport, current.pull_request_number
            )
        elif desired_manifest is None and desired.source_build is None and desired.image:
            desired = _proved_odoo_build(record_store, profile, desired, current.context, transport)
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
        elif profile.driver_id != "odoo" and desired.image:
            _require_newer_artifact(record_store, profile, current, desired, transport)
    except LaneMovementRefused:
        raise
    except (BuildProvenanceError, click.ClickException, OSError, ValueError) as error:
        raise LaneMovementRefused("source_order_unavailable") from error


def _proved_generic_build(
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    desired: LaneBuild,
    transport: BuildProvenanceTransport | None,
    pull_request_number: int | None = None,
    *,
    recorded: bool = False,
) -> LaneBuild:
    if desired.source_build is not None:
        return desired
    from control_plane.build_provenance import verify_generic_web_build

    transport = transport or _source_transport(record_store, profile)
    repository = "/".join(quote(part, safe="") for part in profile.repository.split("/"))
    try:
        identity = transport.get_json(f"/repos/{repository}")
    except OSError as error:
        raise LaneMovementRefused("source_order_unavailable") from error
    if not isinstance(identity, dict) or not identity.get("id"):
        raise LaneMovementRefused("build_identity_unverified")
    try:
        verified = verify_generic_web_build(
            transport=transport,
            repository=profile.repository,
            repository_id=str(identity["id"]),
            commit=desired.commit,
            purpose="preview" if pull_request_number else "release",
            image_repository=profile.image.repository,
            pull_request_number=pull_request_number,
            recorded_image_reference=desired.image if recorded else "",
        )
    except BuildProvenanceError as error:
        from urllib.error import HTTPError

        cause = error.__cause__
        if isinstance(cause, HTTPError) and (cause.code >= 500 or cause.code in {403, 429}):
            raise LaneMovementRefused("source_order_unavailable") from error
        raise LaneMovementRefused("build_identity_unverified") from error
    canonical_tag = f"{profile.image.repository}:sha-{desired.commit}"
    if verified.image_reference != desired.image and desired.image != canonical_tag:
        raise LaneMovementRefused("build_identity_unverified")
    if desired.deploy_reference and desired.deploy_reference != canonical_tag:
        allowed = {
            tag
            if tag.startswith(f"{profile.image.repository}:")
            else f"{profile.image.repository}:{tag}"
            for tag in verified.manifest.image.tags
        }
        if desired.deploy_reference not in allowed:
            raise LaneMovementRefused("build_identity_unverified")
    return replace(desired, source_build=verified.source_build, image=verified.image_reference)


def _proved_odoo_build(
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    desired: LaneBuild,
    context: str,
    transport: BuildProvenanceTransport | None,
    pull_request_number: int | None = None,
) -> LaneBuild:
    from control_plane.build_provenance import verify_build_artifact

    transport = transport or _source_transport(record_store, profile)
    repository = "/".join(quote(part, safe="") for part in profile.repository.split("/"))
    try:
        identity = transport.get_json(f"/repos/{repository}")
    except OSError as error:
        raise LaneMovementRefused("source_order_unavailable") from error
    if not isinstance(identity, dict) or not identity.get("id"):
        raise LaneMovementRefused("build_identity_unverified")
    try:
        verified = verify_build_artifact(
            transport=transport,
            repository=profile.repository,
            repository_id=str(identity["id"]),
            commit=desired.commit,
            purpose="preview" if pull_request_number else "release",
            context=context,
            image_repository=profile.image.repository,
            pull_request_number=pull_request_number,
        )
    except BuildProvenanceError as error:
        from urllib.error import HTTPError

        cause = error.__cause__
        if isinstance(cause, HTTPError) and (cause.code >= 500 or cause.code in {403, 429}):
            raise LaneMovementRefused("source_order_unavailable") from error
        raise LaneMovementRefused("build_identity_unverified") from error
    image = f"{verified.manifest.image.repository}@{verified.manifest.image.digest}"
    if image != desired.image:
        raise LaneMovementRefused("build_identity_unverified")
    return replace(desired, source_build=verified.manifest.source_build)


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
    if before is None and current.image and profile.driver_id != "odoo":
        try:
            before = _proved_generic_build(
                record_store,
                profile,
                current,
                transport,
                current.pull_request_number,
                recorded=True,
            ).source_build
        except LaneMovementRefused as error:
            if error.code != "build_identity_unverified":
                raise
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
            transport = _source_transport(record_store, profile)
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


def require_forward_ship_build(
    *, record_store: object, context: str, instance: str, desired: LaneBuild
) -> None:
    current = current_lane_build(record_store, context=context, instance=instance)
    if not current.artifact_id and not current.commit:
        return
    if current.artifact_id and current.artifact_id == desired.artifact_id:
        return
    profiles = [
        profile
        for profile in cast(LaneMovementStore, record_store).list_product_profile_records()
        if any(lane.context == context and lane.instance == instance for lane in profile.lanes)
    ]
    if len(profiles) != 1:
        raise LaneMovementRefused("source_authority_unavailable")
    require_forward_lane_build(
        record_store=record_store,
        profile=profiles[0],
        context=context,
        instance=instance,
        desired=desired,
    )


class PreviewRefusalStore(Protocol):
    def write_deployment_record(self, record: object) -> object: ...


def record_preview_refusal(
    *,
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    preview_slug: str,
    desired: LaneBuild,
    error: LaneMovementRefused,
    target_type: Literal["compose", "application"],
) -> None:
    from control_plane.contracts.deployment_record import DeploymentRecord
    from control_plane.contracts.promotion_record import (
        ArtifactIdentityReference,
        DeploymentEvidence,
    )
    from control_plane.workflows.ship import generate_deployment_record_id, utc_now_timestamp
    from uuid import uuid4

    timestamp = utc_now_timestamp()
    record = DeploymentRecord(
        record_id=(
            generate_deployment_record_id(
                context_name=profile.preview.context, instance_name=preview_slug
            )
            + f"-refused-{uuid4().hex}"
        ),
        context=profile.preview.context,
        instance=preview_slug,
        source_git_ref=desired.commit,
        artifact_identity=ArtifactIdentityReference(artifact_id=desired.artifact_id),
        verify_destination_health=False,
        delegated_executor="control-plane.lane-movement",
        runtime_source={"provider_effects_status": "not_started"},
        failure=error.record_failure(),
        deploy=DeploymentEvidence(
            target_name=preview_slug,
            target_type=target_type,
            deploy_mode="source-order-check",
            status="fail",
            started_at=timestamp,
            finished_at=timestamp,
        ),
    )
    cast(PreviewRefusalStore, record_store).write_deployment_record(record)


def require_forward_preview_build(
    *,
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    preview_slug: str,
    desired: LaneBuild,
    pull_request_number: int | None = None,
    transport: BuildProvenanceTransport | None = None,
) -> None:
    from control_plane.workflows.generic_web_preview import preview_pr_number_from_slug

    number = pull_request_number or preview_pr_number_from_slug(
        slug_template=profile.preview.slug_template.strip(), preview_slug=preview_slug
    )
    if number is None:
        raise LaneMovementRefused("preview_source_unverified")
    builds = current_preview_builds(record_store, profile, number)
    if profile.driver_id == "odoo" and any(
        build.commit and build.image != desired.image for build in builds
    ):
        desired = _proved_odoo_build(
            record_store,
            profile,
            replace(desired, source_build=None),
            profile.preview.context,
            transport,
            number,
        )
    for current in builds:
        require_forward_build(
            record_store=record_store,
            profile=profile,
            current=current,
            desired=desired,
            transport=transport,
        )


def current_preview_builds(
    record_store: object, profile: LaunchplaneProductProfileRecord, number: int
) -> tuple[LaneBuild, ...]:
    from control_plane.product_reconcile import _current_preview

    builds: list[LaneBuild] = []
    for serving_only in (False, True):
        current, _ = _current_preview(
            record_store=cast("ProductReconcileStore", record_store),
            profile=profile,
            preview_context=profile.preview.context,
            pull_request_number=number,
            for_movement=True,
            serving_only=serving_only,
        )
        build = LaneBuild(
            artifact_id=str(current.get("current_artifact_id", "")),
            commit=str(current["current_head_sha"]),
            image=f"{profile.image.repository}@{current['current_image_digest']}"
            if current["current_image_digest"]
            else "",
            observed_at=str(current.get("current_observed_at", "")),
            pull_request_number=number,
        )
        if build not in builds:
            builds.append(build)
    return tuple(builds)
