"""Compile DB-backed generic-web lane authority and persist in deployment payloads."""

from collections.abc import Callable
from typing import Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from control_plane.workflows.generic_web_deploy_provider import GenericWebResolvedDeployTarget

from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.deployment_record import DeploymentRecord, ResolvedTargetEvidence
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.generic_web_cutover import (
    GenericWebCutoverPlan,
    GenericWebCutoverRecord,
    WebSlot,
)
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    DeploymentEvidence,
    PostDeployUpdateEvidence,
)
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.release_compatibility import classify_release_database_compatibility
from control_plane.workflows.generic_web_cutover import (
    WebCutoverProvider,
    execute_web_cutover,
    rollback_web_cutover,
)
from control_plane.workflows.inventory import build_environment_inventory


class WebCutoverAuthorityStore(Protocol):
    def read_artifact_manifest(self, artifact_id: str) -> ArtifactIdentityManifest: ...
    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord: ...
    def read_environment_inventory(
        self, *, context_name: str, instance_name: str
    ) -> EnvironmentInventory: ...
    def read_deployment_record(self, record_id: str) -> DeploymentRecord: ...
    def list_deployment_records(
        self, *, context_name: str = "", instance_name: str = ""
    ) -> tuple[DeploymentRecord, ...]: ...
    def write_deployment_record(self, record: DeploymentRecord) -> object: ...
    def write_environment_inventory(self, record: EnvironmentInventory) -> object: ...


class WebDeploymentCutoverStore:
    def __init__(self, store: WebCutoverAuthorityStore, deployment: DeploymentRecord):
        self.store, self.deployment = store, deployment

    def load(self, record_id: str) -> GenericWebCutoverRecord:
        record = self.store.read_deployment_record(self.deployment.record_id).generic_web_cutover
        if record is None:
            raise FileNotFoundError(record_id)
        if record.record_id != record_id:
            raise ValueError("deployment belongs to another cutover")
        return record

    def save(self, record: GenericWebCutoverRecord) -> None:
        try:
            deployment = self.store.read_deployment_record(self.deployment.record_id)
        except FileNotFoundError:
            deployment = self.deployment
        current = deployment.generic_web_cutover
        if current is not None and (
            current.record_id != record.record_id or current.plan != record.plan
        ):
            raise ValueError("cannot replace a cutover's immutable intent")
        self.store.write_deployment_record(
            deployment.model_copy(update={"generic_web_cutover": record})
        )


def compile_web_cutover(
    store: WebCutoverAuthorityStore,
    *,
    product: str,
    lane: ProductLaneProfile,
    artifact_id: str,
    source_git_ref: str,
    deploy_reference: str,
    record_id: str,
) -> GenericWebCutoverPlan:
    config = lane.generic_web_cutover
    if config is None:
        raise ValueError("persisted two-slot lane authority is unavailable")
    try:
        previous = store.read_deployment_record(record_id).generic_web_cutover
    except FileNotFoundError:
        previous = None
    if previous is not None:
        plan = previous.plan
        if (
            plan.lane != config
            or plan.new.runtime_identity.product != product
            or plan.new.runtime_identity.context != lane.context
            or plan.new.runtime_identity.instance != lane.instance
            or plan.candidate.artifact_id != artifact_id
            or plan.candidate.source_commit != source_git_ref
            or plan.new.deploy_reference != deploy_reference
        ):
            raise ValueError("retry differs from its persisted lane/artifact authority")
        return plan
    inventory = store.read_environment_inventory(
        context_name=lane.context, instance_name=lane.instance
    )
    identity = inventory.runtime_identity
    if identity is None or not identity.slot or identity.product != product:
        raise ValueError("lane lacks independently seeded serving slot identity")
    production = store.read_artifact_manifest(identity.artifact_id)
    candidate = store.read_artifact_manifest(artifact_id)
    require_web_artifact_scope(store.read_product_profile_record(product), candidate)
    if candidate.source_commit != source_git_ref:
        raise ValueError("candidate source differs from the verified manifest")
    old_coordinate = next((slot for slot in config.slots if slot.slot_id == identity.slot), None)
    if old_coordinate is None:
        raise ValueError("serving inventory does not match the admitted slots")
    new_coordinate = next(slot for slot in config.slots if slot != old_coordinate)
    return GenericWebCutoverPlan(
        release_id=record_id,
        lane=config,
        production=production,
        candidate=candidate,
        compatibility=classify_release_database_compatibility(
            production=production, candidate=candidate
        ),
        old=WebSlot(
            **old_coordinate.model_dump(),
            image=f"{production.image.repository}@{production.image.digest}",
            runtime_identity=identity,
        ),
        new=WebSlot(
            **new_coordinate.model_dump(),
            image=f"{candidate.image.repository}@{candidate.image.digest}",
            deploy_reference=deploy_reference,
            runtime_identity=RuntimeIdentity(
                product=product,
                context=lane.context,
                instance=lane.instance,
                artifact_id=candidate.artifact_id,
                source_git_ref=candidate.source_commit,
                deployment_record_id=record_id,
                release_id=record_id,
                slot=new_coordinate.slot_id,
                image_reference=f"{candidate.image.repository}@{candidate.image.digest}",
                image_digest=candidate.image.digest,
                database_uuid=identity.database_uuid,
            ),
        ),
    )


def execute_web_cutover_deployment(
    store: WebCutoverAuthorityStore,
    *,
    product: str,
    lane: ProductLaneProfile,
    artifact_id: str,
    source_git_ref: str,
    deploy_reference: str,
    record_id: str,
    factory: Callable[[GenericWebCutoverPlan], WebCutoverProvider] | None,
    rollback: bool = False,
    checkpoint: Callable[[str], None] = lambda _phase: None,
    before_records_commit: Callable[[], None] = lambda: None,
) -> DeploymentRecord:
    # The outer product-operation serializer and existing accepted-release/backup
    # guard remain authority. This store adds checkpoints, not a competing lock.
    records = store.list_deployment_records(context_name=lane.context, instance_name=lane.instance)
    unfinished = [
        r
        for r in records
        if r.generic_web_cutover is not None
        and r.generic_web_cutover.step not in {"complete", "rolled_back", "blocked"}
    ]
    if len(unfinished) > 1:
        raise ValueError("multiple unfinished cutovers require reconciliation")
    current = store.read_environment_inventory(
        context_name=lane.context, instance_name=lane.instance
    )
    if rollback:
        source = (
            unfinished[0]
            if len(unfinished) == 1
            else store.read_deployment_record(current.deployment_record_id)
        )
        cutover = source.generic_web_cutover
        if (
            cutover is None
            or artifact_id != cutover.plan.production.artifact_id
            or source_git_ref != cutover.plan.production.source_commit
        ):
            raise ValueError("rollback must select the cutover's retained artifact/source")
        plan = cutover.plan
        if plan.lane != lane.generic_web_cutover:
            raise ValueError("rollback differs from current lane authority")
    else:
        if any(r.record_id != record_id for r in unfinished):
            raise ValueError("another unfinished cutover holds this lane; recover it first")
        plan = compile_web_cutover(
            store,
            product=product,
            lane=lane,
            artifact_id=artifact_id,
            source_git_ref=source_git_ref,
            deploy_reference=deploy_reference,
            record_id=record_id,
        )
        source = DeploymentRecord(
            record_id=record_id,
            context=lane.context,
            instance=lane.instance,
            source_git_ref=plan.candidate.source_commit,
            artifact_identity=ArtifactIdentityReference(artifact_id=plan.candidate.artifact_id),
            runtime_identity=plan.new.runtime_identity,
            resolved_target=ResolvedTargetEvidence(
                target_type=plan.new.target_type,
                target_id=plan.new.target_id,
                target_name=plan.new.slot_id,
            ),
            deploy=DeploymentEvidence(
                target_type=plan.new.target_type,
                target_name=plan.new.slot_id,
                deploy_mode="atomic-two-slot",
                status="pending",
                deployment_id=plan.release_id,
            ),
        )
    if plan.compatibility.classification != "compatible":
        from datetime import UTC, datetime

        persistence = WebDeploymentCutoverStore(store, source)
        now = datetime.now(UTC)
        source.deploy.status = "fail"
        source.deploy.started_at = now.isoformat()
        source.deploy.finished_at = now.isoformat()
        persistence.save(
            GenericWebCutoverRecord(
                record_id=source.record_id,
                plan=plan,
                step="blocked",
                began_at=now,
                updated_at=now,
                error="database_changing",
            )
        )
        raise ValueError("generic-web release lacks proved read/write compatibility; no fallback")
    if factory is None:
        raise ValueError("two-slot provider capability is unavailable; in-place fallback refused")
    if lane.generic_web_extensions.configured:
        raise ValueError("application lifecycle extensions require explicit two-slot qualification")
    provider = factory(plan)
    persistence = WebDeploymentCutoverStore(store, source)
    cutover = (
        rollback_web_cutover(persistence, provider, source.record_id, checkpoint=checkpoint)
        if rollback
        else execute_web_cutover(
            plan, persistence, provider, record_id=source.record_id, checkpoint=checkpoint
        )
    )
    if rollback and source.deploy.status == "pending":
        source = store.read_deployment_record(source.record_id)
        source.deploy.status = "fail"
        source.deploy.started_at = cutover.began_at.isoformat()
        source.deploy.finished_at = cutover.updated_at.isoformat()
        store.write_deployment_record(source)
    slot = plan.old if rollback else plan.new
    if cutover.route is None:
        raise ValueError("cutover lacks applied/public runtime evidence")
    finished = cutover.updated_at.isoformat()
    result = source.model_copy(
        update={
            "record_id": record_id,
            "generic_web_cutover": None if rollback else cutover,
            "artifact_identity": ArtifactIdentityReference(
                artifact_id=slot.runtime_identity.artifact_id
            ),
            "source_git_ref": slot.runtime_identity.source_git_ref,
            "runtime_identity": slot.runtime_identity,
            "resolved_target": ResolvedTargetEvidence(
                target_type=slot.target_type, target_id=slot.target_id, target_name=slot.slot_id
            ),
            "deployed_target": ResolvedTargetEvidence(
                target_type=slot.target_type, target_id=slot.target_id, target_name=slot.slot_id
            ).to_deployed_target_reference(),
            "deploy": DeploymentEvidence(
                target_type=slot.target_type,
                target_name=slot.slot_id,
                deploy_mode="atomic-switch-back" if rollback else "atomic-two-slot",
                status="pass",
                deployment_id=plan.release_id,
                started_at=(cutover.rollback_started_at or cutover.began_at).isoformat()
                if rollback
                else cutover.began_at.isoformat(),
                finished_at=finished,
            ),
            "post_deploy_update": PostDeployUpdateEvidence(
                status="pass",
                attempted=True,
                evidence={
                    "cutover_record_id": source.record_id,
                    "classification": plan.compatibility.classification,
                },
            ),
            "destination_health": cutover.route.public_health,
        }
    )
    before_records_commit()
    store.write_deployment_record(result)
    store.write_environment_inventory(
        build_environment_inventory(deployment_record=result, updated_at=finished)
    )
    return result


def resolve_web_cutover_target(
    store: WebCutoverAuthorityStore,
    *,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    artifact_id: str,
    source_git_ref: str,
    deploy_reference: str,
    provider_id: str,
    timeout_seconds: int | None,
) -> "GenericWebResolvedDeployTarget":
    """Reserve the stable traffic authority, never an unrelated legacy recipient."""
    from control_plane.contracts.ship_request import ShipRequest
    from control_plane.workflows.generic_web_deploy_provider import GenericWebResolvedDeployTarget

    current = store.read_product_profile_record(profile.product)
    active = next(
        (
            item
            for item in current.lanes
            if item.context == lane.context and item.instance == lane.instance
        ),
        None,
    )
    if (
        active is None
        or active.generic_web_cutover is None
        or active.generic_web_cutover != lane.generic_web_cutover
    ):
        raise ValueError("two-slot traffic authority changed before reservation")
    config = active.generic_web_cutover
    manifest = store.read_artifact_manifest(artifact_id)
    require_web_artifact_scope(current, manifest)
    if manifest.source_commit != source_git_ref:
        raise ValueError("candidate source differs from its verified artifact")
    target_type = config.slots[0].target_type
    target = ResolvedTargetEvidence(
        target_type=target_type, target_id="route:" + config.route_id, target_name=config.route_id
    )
    return GenericWebResolvedDeployTarget(
        ship_request=ShipRequest(
            artifact_id=f"{manifest.image.repository}@{manifest.image.digest}",
            deploy_reference=deploy_reference,
            context=lane.context,
            instance=lane.instance,
            source_git_ref=source_git_ref,
            target_type=target_type,
            target_name=config.route_id,
            deploy_mode="atomic-two-slot",
            provider_id=provider_id,
            target_category=target_type,
            provider_target_type=target_type,
            wait=True,
            verify_health=False,
        ),
        resolved_target=target,
        deployed_target=target.to_deployed_target_reference(provider_id=provider_id),
        deploy_timeout_seconds=timeout_seconds or 300,
    )


def require_web_artifact_scope(
    profile: LaunchplaneProductProfileRecord, manifest: ArtifactIdentityManifest
) -> None:
    build = manifest.source_build
    if (
        manifest.image.repository.rstrip("/") != profile.image.repository.rstrip("/")
        or build is None
        or build.repository.casefold() != profile.repository.casefold()
        or (profile.repository_id and build.repository_id != profile.repository_id)
    ):
        raise ValueError("candidate artifact does not belong to the product repository/image")
