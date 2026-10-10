"""Build exact cutover intent from current DB-backed lane/artifact authority."""

import hashlib
from typing import Protocol
from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.odoo_cutover import OdooCutoverPlan, OdooSlot
from control_plane.contracts.odoo_cutover import OdooCutoverRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.prepared_public_site import PublicSiteBinding
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.release_compatibility import classify_release_database_compatibility


class CutoverAuthorityStore(Protocol):
    def read_deployment_record(self, record_id: str) -> DeploymentRecord: ...
    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord: ...
    def read_environment_inventory(
        self, *, context_name: str, instance_name: str
    ) -> EnvironmentInventory: ...
    def read_release_tuple_record(
        self, *, context_name: str, channel_name: str
    ) -> ReleaseTupleRecord: ...
    def read_artifact_manifest(self, artifact_id: str) -> ArtifactIdentityManifest: ...


def cutover_identity(product: str, context: str, instance: str, release_id: str) -> str:
    return hashlib.sha256(
        "\x00".join((product, context, instance, release_id)).encode()
    ).hexdigest()[:32]


def cutover_record_id(plan: OdooCutoverPlan) -> str:
    return cutover_identity(
        plan.new.binding.product_id,
        plan.new.runtime_identity.context,
        plan.new.binding.environment_id,
        plan.release_id,
    )


def persisted_cutover(
    store: CutoverAuthorityStore, plan: OdooCutoverPlan | None
) -> OdooCutoverRecord | None:
    if plan is None:
        return None
    try:
        deployment = store.read_deployment_record(plan.new.runtime_identity.deployment_record_id)
    except FileNotFoundError:
        return None
    if not isinstance(deployment, DeploymentRecord) or deployment.odoo_cutover is None:
        return None
    if deployment.odoo_cutover.plan != plan:
        raise ValueError("persisted release differs from its recovery plan")
    return deployment.odoo_cutover


def resolve_profile_cutover_plan(
    store: CutoverAuthorityStore,
    *,
    product: str,
    context: str,
    instance: str,
    artifact_id: str,
    release_id: str,
) -> OdooCutoverPlan | None:
    profile = store.read_product_profile_record(product)
    lane = next(
        (lane for lane in profile.lanes if lane.context == context and lane.instance == instance),
        None,
    )
    if lane is None or lane.odoo_cutover is None:
        return None
    config = lane.odoo_cutover
    record_id = cutover_identity(product, context, instance, release_id)
    try:
        existing = store.read_deployment_record("deployment-cutover-" + record_id)
    except FileNotFoundError:
        existing = None
    if isinstance(existing, DeploymentRecord) and existing.odoo_cutover is not None:
        plan = existing.odoo_cutover.plan
        coordinates = {(slot.slot_id, slot.compose_id, slot.private_url) for slot in config.slots}
        if (
            plan.candidate.artifact_id != artifact_id
            or plan.route_id != config.route_id
            or plan.host != config.host
            or plan.writers != config.writers
            or plan.old.binding.database != config.database
            or plan.old.runtime_identity.database_uuid != config.database_uuid
            or coordinates
            != {(slot.slot_id, slot.compose_id, slot.private_url) for slot in (plan.old, plan.new)}
        ):
            raise ValueError("persisted cutover no longer matches current admitted lane records")
        return plan
    inventory = store.read_environment_inventory(context_name=context, instance_name=instance)
    previous = inventory.runtime_identity
    if previous is None or previous.database_uuid != config.database_uuid:
        raise ValueError("two-slot lane lacks its exact recorded current DB/runtime identity")
    current = store.read_release_tuple_record(context_name=context, channel_name=instance)
    production = store.read_artifact_manifest(current.artifact_id)
    candidate = store.read_artifact_manifest(artifact_id)
    old_coordinate = next((slot for slot in config.slots if slot.slot_id == previous.slot), None)
    if old_coordinate is None or previous.artifact_id != production.artifact_id:
        raise ValueError("lane coordinates differ from current serving inventory")
    new_coordinate = next(slot for slot in config.slots if slot != old_coordinate)
    binding = PublicSiteBinding(
        product_id=product,
        environment_id=instance,
        release_id=release_id,
        artifact_id=production.artifact_id,
        image_digest=production.image.digest,
        database=config.database,
    )
    old = OdooSlot(
        **old_coordinate.model_dump(),
        binding=binding,
        image=f"{production.image.repository}@{production.image.digest}",
        runtime_identity=previous,
    )
    # ID derives from the same lane/operation binding as the persisted envelope.
    new = OdooSlot(
        **new_coordinate.model_dump(),
        binding=binding.model_copy(
            update={"artifact_id": candidate.artifact_id, "image_digest": candidate.image.digest}
        ),
        image=f"{candidate.image.repository}@{candidate.image.digest}",
        runtime_identity=RuntimeIdentity(
            product=product,
            context=context,
            instance=instance,
            deployment_record_id="deployment-cutover-" + record_id,
            artifact_id=candidate.artifact_id,
            source_git_ref=candidate.source_commit,
            slot=new_coordinate.slot_id,
            release_id=release_id,
            image_digest=candidate.image.digest,
            image_reference=f"{candidate.image.repository}@{candidate.image.digest}",
            database_uuid=config.database_uuid,
        ),
    )
    return OdooCutoverPlan(
        release_id=release_id,
        route_id=config.route_id,
        host=config.host,
        production=production,
        candidate=candidate,
        compatibility=classify_release_database_compatibility(
            production=production, candidate=candidate
        ),
        old=old,
        new=new,
        writers=config.writers,
    )
