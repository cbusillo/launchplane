"""Adapt the cutover executor to the existing promotion/deployment record path."""

from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict

from control_plane.contracts.deployment_record import DeploymentRecord, ResolvedTargetEvidence
from control_plane.contracts.odoo_cutover import OdooCutoverPlan
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    DeploymentEvidence,
    PostDeployUpdateEvidence,
)
from control_plane.odoo_cutover_plan import cutover_record_id
from control_plane.odoo_cutover_storage import DeploymentCutoverStore, DeploymentStore
from control_plane.workflows.odoo_cutover import OdooCutoverProvider, execute_odoo_cutover
from control_plane.workflows.odoo_cutover import rollback_compatible_cutover


class OdooCutoverApplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deployment_record_id: str
    deploy_status: Literal["pass", "fail"]
    post_deploy_status: Literal["pass", "fail", "skipped"]
    health_status: Literal["pass", "fail", "skipped"]
    error_message: str = ""
    release_tuple_id: str = ""


def execute_odoo_cutover_deployment(
    *,
    plan: OdooCutoverPlan,
    record_store: DeploymentStore,
    provider: OdooCutoverProvider,
    context: str,
    instance: str,
    checkpoint: Callable[[str], None],
) -> OdooCutoverApplyResult:
    if context != plan.new.runtime_identity.context or instance != plan.new.binding.environment_id:
        raise ValueError("deployment envelope differs from cutover context/lane")
    record_id = cutover_record_id(plan)
    deployment_id = plan.new.runtime_identity.deployment_record_id
    deployment = DeploymentRecord(
        record_id=deployment_id,
        context=context,
        instance=instance,
        source_git_ref=plan.candidate.source_commit,
        artifact_identity=ArtifactIdentityReference(artifact_id=plan.candidate.artifact_id),
        runtime_identity=plan.new.runtime_identity,
        resolved_target=ResolvedTargetEvidence(
            target_type="compose", target_id=plan.new.compose_id, target_name=plan.new.slot_id
        ),
        deploy=DeploymentEvidence(
            target_type="compose",
            target_name=plan.new.slot_id,
            deploy_mode="atomic-two-slot",
            deployment_id=plan.release_id,
        ),
    )
    store = DeploymentCutoverStore(record_store, deployment)
    record = execute_odoo_cutover(
        plan,
        store,
        provider,
        record_id=record_id,
        checkpoint=checkpoint,
    )
    if record.step != "complete" or record.readiness is None or record.route is None:
        raise ValueError("cutover does not have verified completed traffic/writer evidence")
    persisted = record_store.read_deployment_record(deployment_id)
    persisted.deploy.status = "pass"
    persisted.deploy.started_at = record.began_at.isoformat()
    assert record.ended_at is not None
    persisted.deploy.finished_at = record.ended_at.isoformat()
    persisted.post_deploy_update = PostDeployUpdateEvidence(
        attempted=True,
        status="pass",
        detail="Exact planned maintenance receipt verified before traffic.",
        evidence={
            "cutover_record_id": record_id,
            "classification": plan.compatibility.classification,
        },
    )
    assert record.route.public_health is not None
    persisted.destination_health = record.route.public_health
    record_store.write_deployment_record(persisted)
    return OdooCutoverApplyResult(
        deployment_record_id=deployment_id,
        deploy_status="pass",
        post_deploy_status="pass",
        health_status="pass",
    )


def execute_odoo_cutover_rollback_deployment(
    *,
    deployment: DeploymentRecord,
    record_store: DeploymentStore,
    provider: OdooCutoverProvider,
    artifact_id: str,
) -> OdooCutoverApplyResult:
    record = deployment.odoo_cutover
    if record is None or artifact_id != record.plan.production.artifact_id:
        raise ValueError("cutover rollback requires its exact retained production artifact")
    store = DeploymentCutoverStore(record_store, deployment)
    rolled = rollback_compatible_cutover(store, provider, record.record_id)
    old = rolled.plan.old
    rollback_id = deployment.record_id + "-switch-back"
    previous = deployment.model_copy(
        update={
            "record_id": rollback_id,
            "odoo_cutover": None,
            "artifact_identity": ArtifactIdentityReference(artifact_id=artifact_id),
            "runtime_identity": old.runtime_identity,
            "runtime_source": {
                "retained_deployment_record_id": old.runtime_identity.deployment_record_id
            },
            "source_git_ref": rolled.plan.production.source_commit,
            "resolved_target": ResolvedTargetEvidence(
                target_type="compose", target_id=old.compose_id, target_name=old.slot_id
            ),
            "deploy": DeploymentEvidence(
                status="pass",
                target_type="compose",
                target_name=old.slot_id,
                deploy_mode="atomic-switch-back",
                deployment_id=record.plan.release_id,
            ),
            "post_deploy_update": PostDeployUpdateEvidence(attempted=False, status="skipped"),
            "destination_health": rolled.route.public_health if rolled.route else None,
        }
    )
    record_store.write_deployment_record(previous)
    return OdooCutoverApplyResult(
        deployment_record_id=rollback_id,
        deploy_status="pass",
        post_deploy_status="skipped",
        health_status="pass",
    )
