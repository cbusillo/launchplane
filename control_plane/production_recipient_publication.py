"""Exact lane publication and scoped inverse under the service authority transaction.

Provider work finishes before entering this transaction. A lost commit reply is
resolved from custody, never by redeploying or restoring a shared guest.
"""

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from control_plane.contracts.production_recipient import ProductionRecipientRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.private_health_endpoint_record import PrivateHealthEndpointRecord
from control_plane.contracts.production_backup_authority import ProductionBackupPolicyRecord
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    parse_launchplane_mutation_timestamp,
)
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.promotion_record import PromotionRecord
from control_plane.integration_allowances import canonical_sha256
from control_plane.production_recipient import authority_snapshot
from control_plane.storage.product_authority_bundle import (
    ProductAuthorityBundle,
    ProviderTargetWrite,
)
from control_plane.storage.postgres import (
    PostgresRecordStore,
    LaunchplaneProductionRecipientRow,
    LaunchplanePrivateHealthEndpointRow,
    LaunchplaneProductionBackupPolicyRow,
    LaunchplaneProductionBackupTargetRow,
    LaunchplaneDeploymentRow,
    LaunchplanePromotionRow,
)
from control_plane.production_backup_authority import (
    plan_production_backup_policy_append,
    validate_production_backup_policy_binding,
)
from control_plane.contracts.production_backup_authority import ProductionBackupTargetRecord
from control_plane.workflows.ship import utc_now_timestamp


class RecipientPublication(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: DokployTargetRecord
    target_id: DokployTargetIdRecord
    provider: ProviderTargetRecord
    inventory: EnvironmentInventory
    health_endpoints: tuple[PrivateHealthEndpointRecord, ...]
    backup_policies: tuple[ProductionBackupPolicyRecord, ...]

    def require_lane(self, record: ProductionRecipientRecord) -> None:
        for row in (
            self.target,
            self.target_id,
            self.provider,
            self.inventory,
            *self.health_endpoints,
            *self.backup_policies,
        ):
            if (row.model_dump()["context"], row.model_dump()["instance"]) != (
                record.context,
                "prod",
            ):
                raise ValueError("Publication must preserve the canonical production lane.")
        if (
            self.target_id.target_id != self.provider.target_id
            or self.provider.target_id != record.target_id
        ):
            raise ValueError("Publication must select the exact prepared recipient.")
        if self.provider != ProviderTargetRecord.from_dokploy_records(
            target_record=self.target, target_id_record=self.target_id
        ):
            raise ValueError("Provider and compatibility authority must agree.")
        if (
            self.target.public_hosts
            or self.target.pending_public_host_effects
            or self.target.domains
        ):
            raise ValueError("A private recipient cannot publish public traffic.")
        if (
            self.inventory.source_git_ref != record.request.source_commit
            or self.inventory.destination_health.status != "pass"
            or self.inventory.deploy.status != "pass"
            or self.inventory.runtime_identity is None
            or self.inventory.runtime_identity.image_reference != record.image_reference
        ):
            raise ValueError("Publication requires healthy exact-artifact runtime evidence.")
        before = record.authority_snapshot
        if {r.endpoint_key for r in self.health_endpoints} != {
            r["endpoint_key"] for r in before["health_endpoints"]
        }:
            raise ValueError("Publication preserves every private monitor identity.")
        active = [r for r in before["backup_policies"] if r["status"] == "active"]
        if not active or {r.policy_id for r in self.backup_policies} != {
            r["policy_id"] for r in active
        }:
            raise ValueError("Publication preserves every active production backup gate.")
        for policy in self.backup_policies:
            previous = next(r for r in active if r["policy_id"] == policy.policy_id)
            if (
                policy.status != "active"
                or policy.supersedes_record_id != previous["record_id"]
                or policy.policy_revision != previous["policy_revision"] + 1
            ):
                raise ValueError("Backup publication must append the exact next source revision.")
            # Source placement changes; protection, freshness and retention do not.
            old = ProductionBackupPolicyRecord.model_validate(previous)
            if policy.fast_snapshot.model_dump(
                exclude={"source_target_id"}
            ) != old.fast_snapshot.model_dump(
                exclude={"source_target_id"}
            ) or policy.independent_backup.model_dump(
                exclude={"source_target_id"}
            ) != old.independent_backup.model_dump(exclude={"source_target_id"}):
                raise ValueError("A recipient move cannot weaken production backup protection.")


def _bundle(publication: RecipientPublication) -> ProductAuthorityBundle:
    return ProductAuthorityBundle(
        dokploy_targets=(publication.target,),
        dokploy_target_ids=(publication.target_id,),
        provider_target_writes=(
            ProviderTargetWrite(record=publication.provider, expected_record=publication.provider),
        ),
        environment_inventory=(publication.inventory,),
    )


def commit_publication(
    store: PostgresRecordStore,
    record: ProductionRecipientRecord,
    publication: RecipientPublication,
    *,
    validate_release: Callable[[], None],
    reservation: LaunchplaneIdempotencyRecord | None = None,
    deployment: DeploymentRecord | None = None,
    promotion: PromotionRecord | None = None,
    authority: dict[str, Any] | None = None,
    writer_binding: str = "",
) -> ProductionRecipientRecord:
    """CAS all authority plus custody; the callback rechecks accepted-release gates.

    This is an internal release-worker primitive. No HTTP payload can supply the
    callback or substitute caller assertions for completed runtime/recovery work.
    """
    expected_authority = authority if authority is not None else record.authority_snapshot
    publication.require_lane(record.model_copy(update={"authority_snapshot": expected_authority}))
    if (
        not record.recovery_proof
        or not record.qualified_at
        or (record.publication and authority is None)
    ):
        raise ValueError("An unpublished, qualified and recovered preparation is required.")
    with store._session_factory() as session:
        store._begin_serialized_write(session)
        store._lock_product_authority_bundle_write(session)
        require_publication_lease(store, session, reservation)
        row = session.get(
            LaunchplaneProductionRecipientRow,
            (record.product, record.context),
            with_for_update=True,
        )
        if row is None or ProductionRecipientRecord.model_validate(row.payload) != record:
            raise ValueError("Recipient custody changed before publication.")
        if authority_snapshot(store, record.product, record.context) != expected_authority:
            raise ValueError("Production authority changed before publication.")
        validate_release()
        before = expected_authority
        bundle = _bundle(publication).model_copy(
            update={
                "provider_target_writes": (
                    ProviderTargetWrite(
                        record=publication.provider, expected_record=record.retained_provider
                    ),
                ),
            }
        )
        store._write_product_authority_bundle(
            bundle, write_session=session, commit=False, write_transaction_started=True
        )
        targets = tuple(
            ProductionBackupTargetRecord.model_validate(r.payload)
            for r in session.scalars(select(LaunchplaneProductionBackupTargetRow)).all()
        )
        for policy in publication.backup_policies:
            validate_production_backup_policy_binding(policy=policy, target_records=targets)
            history = tuple(
                ProductionBackupPolicyRecord.model_validate(r.payload)
                for r in session.scalars(
                    select(LaunchplaneProductionBackupPolicyRow).where(
                        LaunchplaneProductionBackupPolicyRow.policy_id == policy.policy_id
                    )
                ).all()
            )
            append = plan_production_backup_policy_append(records=history, record=policy)
            if append.status != "written" or append.superseded_current_record is None:
                raise ValueError("Backup source revision changed before publication.")
            session.merge(store._production_backup_policy_row(append.superseded_current_record))
            session.flush()
            session.merge(store._production_backup_policy_row(policy))
        for endpoint in publication.health_endpoints:
            session.merge(
                LaunchplanePrivateHealthEndpointRow(
                    endpoint_key=endpoint.endpoint_key,
                    product=endpoint.product,
                    context=endpoint.context,
                    instance=endpoint.instance,
                    url=endpoint.url,
                    status=endpoint.status,
                    updated_at=endpoint.updated_at,
                    payload=endpoint.model_dump(mode="json"),
                )
            )
        result = record.model_copy(
            update={
                "phase": "published",
                "publication": {
                    "before": before,
                    "published": publication.model_dump(mode="json"),
                    "published_sha256": canonical_sha256(publication.model_dump(mode="json")),
                    "published_at": utc_now_timestamp(),
                    "writer_binding": writer_binding,
                    "history": [
                        *record.publication.get("history", []),
                        {
                            key: value
                            for key, value in record.publication.items()
                            if key != "history"
                        },
                    ]
                    if record.publication
                    else [],
                },
            }
        )
        row.payload = result.model_dump(mode="json")
        if deployment is not None:
            if deployment.record_id != publication.inventory.deployment_record_id:
                raise ValueError("Publication deployment evidence does not match inventory.")
            session.merge(
                LaunchplaneDeploymentRow(
                    record_id=deployment.record_id,
                    context=deployment.context,
                    instance=deployment.instance,
                    artifact_id=record.image_reference,
                    source_git_ref=deployment.source_git_ref,
                    deploy_started_at=deployment.deploy.started_at,
                    deploy_finished_at=deployment.deploy.finished_at,
                    payload=deployment.model_dump(mode="json"),
                )
            )
        if promotion is not None:
            if publication.inventory.promotion_record_id != promotion.record_id:
                raise ValueError("Publication promotion evidence does not match inventory.")
            session.merge(
                LaunchplanePromotionRow(
                    record_id=promotion.record_id,
                    context=promotion.context,
                    from_instance=promotion.from_instance,
                    to_instance=promotion.to_instance,
                    artifact_id=promotion.artifact_identity.artifact_id,
                    deploy_started_at=promotion.deploy.started_at,
                    deploy_finished_at=promotion.deploy.finished_at,
                    payload=promotion.model_dump(mode="json"),
                )
            )
        store._after_product_authority_bundle_step("publish_production_recipient")
        session.commit()
        return result


def rollback_publication(
    store: PostgresRecordStore,
    record: ProductionRecipientRecord,
    *,
    stop_replacement: Callable[[], None],
    reservation: LaunchplaneIdempotencyRecord | None = None,
) -> ProductionRecipientRecord:
    """Stop only the replacement writer, then invert unchanged task-owned rows.

    Unrelated lanes, runtime settings, extra endpoints and backup history survive.
    Changed owned rows refuse the whole inverse; they are never overwritten.
    """
    saved = record.publication
    if record.phase in {"rolled_back", "inverse_published"}:
        return record
    if record.phase not in {"published", "reverting"} or not saved:
        raise ValueError("Only the recorded publication can be reversed.")
    published = RecipientPublication.model_validate(saved["published"])
    before = saved["before"]
    if before["inventory"] is None:
        raise ValueError("The retained inventory is unavailable.")
    # No transaction/authority lock is held over host I/O. The current lease
    # fences the exact replacement stop; the CAS below repeats every row check.
    stop_replacement()
    with store._session_factory() as session:
        store._begin_serialized_write(session)
        store._lock_product_authority_bundle_write(session)
        require_publication_lease(store, session, reservation)
        row = session.get(
            LaunchplaneProductionRecipientRow,
            (record.product, record.context),
            with_for_update=True,
        )
        if row is None or ProductionRecipientRecord.model_validate(row.payload) != record:
            raise ValueError("Recipient custody changed before inverse publication.")
        current = authority_snapshot(store, record.product, record.context)
        expected = published.model_dump(mode="json")
        for key in ("target", "target_id", "provider", "inventory"):
            if current[key] != expected[key]:
                raise ValueError("An owned authority row changed; preserve it for review.")
        current_endpoints = {r["endpoint_key"]: r for r in current["health_endpoints"]}
        if any(current_endpoints.get(r["endpoint_key"]) != r for r in expected["health_endpoints"]):
            raise ValueError("An owned monitor row changed; preserve it for review.")
        current_policies = {
            r["policy_id"]: r for r in current["backup_policies"] if r["status"] == "active"
        }
        if any(current_policies.get(r["policy_id"]) != r for r in expected["backup_policies"]):
            raise ValueError("An owned backup row changed; preserve it for review.")
        original_provider = ProviderTargetRecord.model_validate(before["provider"])
        store._write_product_authority_bundle(
            ProductAuthorityBundle(
                dokploy_targets=(DokployTargetRecord.model_validate(before["target"]),),
                dokploy_target_ids=(DokployTargetIdRecord.model_validate(before["target_id"]),),
                provider_target_writes=(
                    ProviderTargetWrite(
                        record=original_provider, expected_record=published.provider
                    ),
                ),
                environment_inventory=(EnvironmentInventory.model_validate(before["inventory"]),),
            ),
            write_session=session,
            commit=False,
            write_transaction_started=True,
        )
        for endpoint in before["health_endpoints"]:
            old = PrivateHealthEndpointRecord.model_validate(endpoint)
            session.merge(
                LaunchplanePrivateHealthEndpointRow(
                    endpoint_key=old.endpoint_key,
                    product=old.product,
                    context=old.context,
                    instance=old.instance,
                    url=old.url,
                    status=old.status,
                    updated_at=old.updated_at,
                    payload=old.model_dump(mode="json"),
                )
            )
        inverse_policies = []
        for policy in published.backup_policies:
            old_policy = next(
                r
                for r in before["backup_policies"]
                if r["policy_id"] == policy.policy_id and r["status"] == "active"
            )
            inverse = ProductionBackupPolicyRecord.model_validate(
                {
                    **old_policy,
                    "record_id": "",
                    "policy_digest": "",
                    "policy_revision": policy.policy_revision + 1,
                    "supersedes_record_id": policy.record_id,
                    "reason": "Exact scoped production recipient inverse",
                }
            )
            session.merge(
                store._production_backup_policy_row(
                    policy.model_copy(update={"status": "superseded"})
                )
            )
            session.flush()
            session.merge(store._production_backup_policy_row(inverse))
            inverse_policies.append(inverse.model_dump(mode="json"))
        result = record.model_copy(
            update={
                "phase": "inverse_published",
                "publication": {
                    **saved,
                    "inverse_backup_policies": inverse_policies,
                    "rolled_back_at": utc_now_timestamp(),
                },
            }
        )
        row.payload = result.model_dump(mode="json")
        store._after_product_authority_bundle_step("reverse_production_recipient")
        session.commit()
        return result


def require_publication_lease(
    store: PostgresRecordStore, session: Any, expected: LaunchplaneIdempotencyRecord | None
) -> None:
    if expected is None:
        return
    row = session.scalar(
        store._idempotency_statement(
            scope=expected.scope,
            route_path=expected.route_path,
            idempotency_key=expected.idempotency_key,
            for_update=True,
        )
    )
    current = LaunchplaneIdempotencyRecord.model_validate(row.payload) if row is not None else None
    if (
        current is None
        or current.state != "running"
        or any(
            getattr(current, key) != getattr(expected, key)
            for key in (
                "record_id",
                "scope",
                "route_path",
                "idempotency_key",
                "request_fingerprint",
                "lease_owner",
                "attempt",
                "provider_target_key",
                "reconciliation_key",
            )
        )
        or parse_launchplane_mutation_timestamp(
            current.lease_expires_at, field_name="lease_expires_at"
        )
        <= parse_launchplane_mutation_timestamp(
            store._database_mutation_timestamp(session), field_name="observed_at"
        )
    ):
        raise ValueError("The publication worker no longer owns its execution lease.")


def republication_snapshot(
    store: PostgresRecordStore, record: ProductionRecipientRecord
) -> dict[str, Any]:
    """Admit only this release's recorded inverse, never arbitrary authority drift."""
    saved = record.publication
    if not saved.get("inverse_backup_policies"):
        raise ValueError("The exact recorded inverse is required before re-promotion.")
    current = authority_snapshot(store, record.product, record.context)
    before = saved["before"]
    if any(current[key] != before[key] for key in before if key != "backup_policies"):
        raise ValueError("Authority changed outside this release's scoped inverse.")
    expected = {row["record_id"]: dict(row) for row in before["backup_policies"]}
    for row in saved["published"]["backup_policies"]:
        expected[row["supersedes_record_id"]]["status"] = "superseded"
        expected[row["record_id"]] = {**row, "status": "superseded"}
    expected.update({row["record_id"]: row for row in saved["inverse_backup_policies"]})
    if {row["record_id"]: row for row in current["backup_policies"]} != expected:
        raise ValueError("Backup authority changed outside the recorded inverse.")
    return current
