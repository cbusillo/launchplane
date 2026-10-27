from dataclasses import dataclass

import click
from pydantic import BaseModel

from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    parse_launchplane_mutation_timestamp,
)
from control_plane.generic_web_deploy_provider_adapter import (
    GenericWebDeployProviderMutationAdapter,
)
from control_plane.generic_web_testing_exclusion import bind_testing_recipient
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.generic_web_application_snapshot import recorded_application_lifecycle
from control_plane.workflows.generic_web_deploy import GENERIC_WEB_UNOBSERVED_DEPLOYMENT_ID
from control_plane.workflows.generic_web_deploy_provider import (
    decode_generic_web_provider_reconciliation_target,
)
from control_plane.workflows.generic_web_execution import execution_receipt
from control_plane.generic_web_testing_inputs import DELIVERY_RECEIPT_KEY


@dataclass(frozen=True)
class PreDispatchRefusal:
    deployment: DeploymentRecord
    bindings: tuple[BaseModel, ...]
    original_intent: dict[str, object]

    def digest_evidence(self) -> dict[str, object]:
        return {
            "deployment": self.deployment.model_dump(mode="json"),
            "bindings": [record.model_dump(mode="json") for record in self.bindings],
            "original_intent": self.original_intent,
        }


def inspect_pre_dispatch_refusal(
    store: PostgresRecordStore,
    adapter: GenericWebDeployProviderMutationAdapter,
    original: LaunchplaneIdempotencyRecord,
    operation_key: str,
    observed_at: str,
) -> PreDispatchRefusal | None:
    if (
        original.state != "reconcile_required"
        or original.attempt != 1
        or original.provider_effect_phase != "records_commit"
        or adapter.lane.instance != "testing"
        or adapter.lane.generic_web_cutover is not None
    ):
        return None
    try:
        if parse_launchplane_mutation_timestamp(
            original.lease_expires_at, field_name="lease_expires_at"
        ) > parse_launchplane_mutation_timestamp(observed_at, field_name="observed_at"):
            return None
        snapshot = decode_generic_web_provider_reconciliation_target(original.reconciliation_key)
        if (
            snapshot.product != adapter.profile.product
            or snapshot.target_type != "compose"
            or snapshot.testing_runtime_digest
            or snapshot.original_event_deploy is not None
            or original.provider_target_key != adapter.target_key()
        ):
            return None
        bindings = bind_testing_recipient(
            store, adapter.profile.product, adapter.lane.context, adapter.lane.instance
        )
        profile, target, target_id, provider = bindings
        if (
            profile != adapter.profile
            or (target.target_type, target.target_name, target_id.target_id)
            != (snapshot.target_type, snapshot.target_name, snapshot.target_id)
            or (provider.provider_id, provider.target_category, provider.provider_target_type)
            != (snapshot.provider_id, snapshot.target_category, snapshot.provider_target_type)
        ):
            return None
        record = store.read_deployment_record(adapter._deployment_record_id(operation_key))
        ship = adapter._resolve_deploy_target().ship_request
        if (
            record.failure is None
            or record.failure.code
            not in {"lane_movement.ancestor_build", "lane_movement.older_artifact"}
            or record.deploy.status != "fail"
            or record.deploy.deployment_id != GENERIC_WEB_UNOBSERVED_DEPLOYMENT_ID
            or record.runtime_identity is not None
            or record.bootstrap.attempted
            or record.post_deploy_update.attempted
            or record.post_deploy_update.status != "skipped"
            or record.post_deploy_update.evidence
            or record.destination_health.verified
            or record.destination_health.status != "skipped"
            or record.integration_key_readback is not None
            or record.generic_web_cutover is not None
            or record.odoo_cutover is not None
            or recorded_application_lifecycle(record) is not None
            or execution_receipt(record) is not None
            or DELIVERY_RECEIPT_KEY in record.runtime_source
            or (record.context, record.instance, record.source_git_ref)
            != (ship.context, ship.instance, ship.source_git_ref)
            or record.artifact_identity is None
            or record.artifact_identity.artifact_id != ship.artifact_id
            or record.resolved_target != adapter._resolve_deploy_target().resolved_target
            or record.deployed_target is None
            or (
                record.deployed_target.provider_id,
                record.deployed_target.target_id,
                record.deployed_target.target_category,
                record.deployed_target.provider_target_type,
            )
            != (
                snapshot.provider_id,
                snapshot.target_id,
                snapshot.target_category,
                snapshot.provider_target_type,
            )
        ):
            return None
        created = parse_launchplane_mutation_timestamp(original.created_at, field_name="created_at")
        started = parse_launchplane_mutation_timestamp(
            record.deploy.started_at, field_name="started_at"
        )
        finished = parse_launchplane_mutation_timestamp(
            record.deploy.finished_at, field_name="finished_at"
        )
        effect = parse_launchplane_mutation_timestamp(
            original.provider_effect_started_at, field_name="provider_effect_started_at"
        )
        if (
            started.replace(microsecond=0) != created.replace(microsecond=0)
            or not started <= finished <= effect
        ):
            return None
        return PreDispatchRefusal(record, bindings, store.read_original_mutation_intent(original))
    except (FileNotFoundError, ValueError, click.ClickException):
        return None
