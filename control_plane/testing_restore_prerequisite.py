"""Keep ordinary maintenance out of a testing target with an incomplete restore."""

from typing import Protocol

from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.odoo_target_replacement_failures import TESTING_DRILL_DATABASE_KEY
from control_plane.odoo_testing_restore import FreshOdooTestingGeneration


class TestingRestoreHistoryStore(Protocol):
    def list_deployment_records(
        self, *, context_name: str = "", instance_name: str = "", limit: int | None = None
    ) -> tuple[DeploymentRecord, ...]: ...

    def read_dokploy_target_id_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetIdRecord: ...


def incomplete_testing_restore(
    record_store: TestingRestoreHistoryStore,
    *,
    context: str,
    instance: str,
    generation: FreshOdooTestingGeneration | None = None,
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
        if record.runtime_source.get("data_source_mode") == "upstream_restore"
        or (
            record.post_deploy_update.attempted
            and record.post_deploy_update.evidence.get("workflow_intent") == "restore"
        )
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
    if (
        generation is not None
        and generation.completed_at
        and (generation.context, generation.instance, generation.target_id)
        == (context, instance, target.target_id)
    ):
        # A no-effect drill can put the prior completed database back on the
        # same provider target. Its untouched candidate is not that database.
        # Untagged historical restores still require completion proof.
        restores = [
            record
            for record in restores
            if not record.runtime_source.get(TESTING_DRILL_DATABASE_KEY)
            or record.runtime_source[TESTING_DRILL_DATABASE_KEY] == generation.drill_database
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
