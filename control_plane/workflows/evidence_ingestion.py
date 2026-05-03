from __future__ import annotations

from typing import Protocol

import click

from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.promotion_record import PromotionRecord
from control_plane.workflows.inventory import build_environment_inventory, inventory_record_id
from control_plane.workflows.ship import utc_now_timestamp


class EvidenceIngestionStore(Protocol):
    def write_deployment_record(self, record: DeploymentRecord) -> object: ...

    def read_deployment_record(self, deployment_record_id: str) -> DeploymentRecord: ...

    def write_promotion_record(self, record: PromotionRecord) -> object: ...

    def write_environment_inventory(self, inventory: EnvironmentInventory) -> object: ...


def _artifact_id_or_empty(artifact_identity: object) -> str:
    if artifact_identity is None:
        return ""
    artifact_id = getattr(artifact_identity, "artifact_id", "")
    if isinstance(artifact_id, str):
        return artifact_id
    return ""


def _write_environment_inventory(
    *,
    record_store: EvidenceIngestionStore,
    deployment_record: DeploymentRecord,
    promotion_record_id: str = "",
    promoted_from_instance: str = "",
) -> str:
    inventory_record = build_environment_inventory(
        deployment_record=deployment_record,
        updated_at=utc_now_timestamp(),
        promotion_record_id=promotion_record_id,
        promoted_from_instance=promoted_from_instance,
    )
    record_store.write_environment_inventory(inventory_record)
    return inventory_record_id(
        context_name=inventory_record.context,
        instance_name=inventory_record.instance,
    )


def apply_deployment_evidence(
    *,
    record_store: EvidenceIngestionStore,
    deployment_record: DeploymentRecord,
) -> dict[str, str]:
    record_store.write_deployment_record(deployment_record)
    result = {"deployment_record_id": deployment_record.record_id}
    result["inventory_record_id"] = _write_environment_inventory(
        record_store=record_store,
        deployment_record=deployment_record,
    )
    return result


def apply_promotion_evidence(
    *,
    record_store: EvidenceIngestionStore,
    promotion_record: PromotionRecord,
) -> dict[str, str]:
    record_store.write_promotion_record(promotion_record)
    result = {"promotion_record_id": promotion_record.record_id}

    deployment_record_id = promotion_record.deployment_record_id.strip()
    if not deployment_record_id:
        return result

    deployment_record = record_store.read_deployment_record(deployment_record_id)
    if deployment_record.context != promotion_record.context:
        raise click.ClickException(
            "Promotion record context does not match linked deployment record context."
        )
    if deployment_record.instance != promotion_record.to_instance:
        raise click.ClickException(
            "Promotion destination instance does not match linked deployment record instance."
        )

    promotion_artifact_id = _artifact_id_or_empty(promotion_record.artifact_identity)
    deployment_artifact_id = _artifact_id_or_empty(deployment_record.artifact_identity)
    if (
        promotion_artifact_id
        and deployment_artifact_id
        and promotion_artifact_id != deployment_artifact_id
    ):
        raise click.ClickException(
            "Promotion artifact_id does not match linked deployment record artifact_id."
        )

    result["inventory_record_id"] = _write_environment_inventory(
        record_store=record_store,
        deployment_record=deployment_record,
        promotion_record_id=promotion_record.record_id,
        promoted_from_instance=promotion_record.from_instance,
    )
    return result
