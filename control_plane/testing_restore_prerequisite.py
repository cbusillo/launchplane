"""Keep ordinary maintenance out of a testing target with an incomplete restore."""

from typing import Protocol

from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord


class TestingRestoreHistoryStore(Protocol):
    def list_deployment_records(
        self, *, context_name: str = "", instance_name: str = "", limit: int | None = None
    ) -> tuple[DeploymentRecord, ...]: ...

    def read_dokploy_target_id_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetIdRecord: ...


def incomplete_testing_restore(
    record_store: TestingRestoreHistoryStore, *, context: str, instance: str
) -> DeploymentRecord | None:
    """A deploy cannot repair data lost by a failed restore. A governed restore can.

    Read the latest attempted restore of this target, ignoring subsequent ordinary
    maintenance. A new isolated generation has its own restore lifecycle instead.
    """
    restores = [
        record
        for record in record_store.list_deployment_records(
            context_name=context, instance_name=instance
        )
        if record.post_deploy_update.attempted
        and record.post_deploy_update.evidence.get("workflow_intent") == "restore"
    ]
    if not restores:
        return None
    target = record_store.read_dokploy_target_id_record(
        context_name=context, instance_name=instance
    )
    restores = [
        record
        for record in restores
        if record.resolved_target is not None
        and record.resolved_target.target_id == target.target_id
    ]
    if not restores:
        return None
    latest = max(restores, key=lambda record: (record.deploy.started_at, record.record_id))
    evidence = latest.post_deploy_update
    if evidence.status == "pass" or (
        evidence.evidence.get("post_deploy_readback_odoo_restore_completed") == "true"
    ):
        return None
    return latest
