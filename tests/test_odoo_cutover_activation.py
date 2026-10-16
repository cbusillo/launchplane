"""Persisted lane authority and narrowly resumable native worker ownership."""

from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import os
from unittest.mock import Mock, patch
from concurrent.futures import ThreadPoolExecutor

from control_plane.contracts.odoo_prod_promotion_operation import (
    OdooProdPromotionOperationRecord,
    ODOO_CUTOVER_RECONCILIATION_DELAY,
)
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.odoo_cutover import OdooCutoverRecord
from control_plane.contracts.odoo_cutover_lane import OdooCutoverLane, OdooSlotCoordinates
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.promotion_record import DeploymentEvidence
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.odoo_cutover_plan import cutover_identity, resolve_profile_cutover_plan
from tests.support.profiles import _odoo_preview_profile_payload
from tests.test_odoo_cutover import IsolatedCutoverProvider
from tests.test_odoo_prod_promotion_operation import _operation, _rollback_operation, _store
from tests.test_postgres_integration import _store_for_fresh_head_database
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.odoo_stable_operation_worker import (
    run_odoo_stable_operation_worker_once,
)


class OdooCutoverActivationTests(unittest.TestCase):
    def store(self, temporary: str) -> PostgresRecordStore:
        fixture = _store(temporary)
        self.addCleanup(fixture.close)
        if not os.environ.get("LAUNCHPLANE_TEST_POSTGRES_URL"):
            return fixture
        store = self.enterContext(_store_for_fresh_head_database())
        store.write_product_profile_record(fixture.read_product_profile_record("odoo-tenant-cm"))
        return store

    def test_plan_uses_persisted_lane_and_retains_original_pair_after_inventory_advances(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            store = self.store(temporary)
            provider = IsolatedCutoverProvider(Path(temporary) / "fixture")
            self.addCleanup(provider.engine.dispose)
            pair = provider.cutover
            config = OdooCutoverLane(
                route_id=pair.route_id,
                host=pair.host,
                database=pair.old.binding.database,
                database_uuid=pair.old.runtime_identity.database_uuid,
                prepared_origin="http://prepared:8080",
                slots=(
                    OdooSlotCoordinates(
                        **pair.old.model_dump(include={"slot_id", "compose_id", "private_url"})
                    ),
                    OdooSlotCoordinates(
                        **pair.new.model_dump(include={"slot_id", "compose_id", "private_url"})
                    ),
                ),
                writers=pair.writers,
            )
            profile = _odoo_preview_profile_payload()
            profile["lanes"] = [
                {
                    "context": "fixture",
                    "instance": "isolated",
                    "base_url": "http://fixture.invalid",
                    "health_url": "http://fixture.invalid/launchplane/health",
                    "odoo_cutover": config.model_dump(mode="json"),
                }
            ]
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(profile)
            )
            for artifact in (pair.production, pair.candidate):
                store.write_artifact_manifest(artifact)
            store.write_release_tuple_record(
                ReleaseTupleRecord(
                    tuple_id="previous",
                    context="fixture",
                    channel="isolated",
                    artifact_id=pair.production.artifact_id,
                    repo_shas={"example/fixture": pair.production.source_commit},
                    provenance="ship",
                    minted_at=datetime.now(UTC).isoformat(),
                )
            )
            inventory = EnvironmentInventory(
                context="fixture",
                instance="isolated",
                source_git_ref=pair.production.source_commit,
                deploy=DeploymentEvidence(
                    status="pass",
                    deploy_mode="atomic-two-slot",
                    target_type="compose",
                    target_name="fixture",
                ),
                runtime_identity=pair.old.runtime_identity,
                updated_at=datetime.now(UTC).isoformat(),
                deployment_record_id="previous",
            )
            store.write_environment_inventory(inventory)
            arguments = dict(
                product="odoo-tenant-cm",
                context="fixture",
                instance="isolated",
                artifact_id=pair.candidate.artifact_id,
                release_id="accepted-operation",
            )
            plan = resolve_profile_cutover_plan(store, **arguments)
            assert plan is not None
            self.assertEqual(plan.old.compose_id, config.slots[0].compose_id)
            self.assertEqual(plan.new.private_url, config.slots[1].private_url)
            record = OdooCutoverRecord(
                record_id=cutover_identity(
                    "odoo-tenant-cm", "fixture", "isolated", plan.release_id
                ),
                plan=plan,
                began_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            store.write_deployment_record(
                DeploymentRecord(
                    record_id=plan.new.runtime_identity.deployment_record_id,
                    context="fixture",
                    instance="isolated",
                    source_git_ref=plan.candidate.source_commit,
                    deploy=DeploymentEvidence(
                        deploy_mode="atomic-two-slot", target_type="compose", target_name="fixture"
                    ),
                    odoo_cutover=record,
                )
            )
            inventory.runtime_identity = plan.new.runtime_identity
            store.write_environment_inventory(inventory)
            self.assertEqual(resolve_profile_cutover_plan(store, **arguments), plan)
            lane = store.read_product_profile_record("odoo-tenant-cm")
            lane.lanes[0].odoo_cutover = config.model_copy(
                update={"database_uuid": "different-physical-db"}
            )
            store.write_product_profile_record(lane)
            with self.assertRaisesRegex(ValueError, "persisted cutover"):
                resolve_profile_cutover_plan(store, **arguments)

    def test_native_reconciliation_claim_is_exclusive_bounded_and_never_replays_maintenance(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            store = self.store(temporary)
            operation = _operation()
            store.create_odoo_prod_promotion_operation_record_if_no_active_lane(operation)
            held = operation.model_copy(
                update={
                    "status": "reconciliation_required",
                    "phase": "promotion_started",
                    "attempt": 1,
                    "error_code": "odoo_prod_promotion_reconciliation_required",
                    "error_message": "Bound route reply was lost.",
                }
            )
            store.write_odoo_prod_promotion_operation_record(held)
            provider = IsolatedCutoverProvider(Path(temporary) / "fixture")
            self.addCleanup(provider.engine.dispose)
            plan = provider.cutover
            slots = []
            for slot in (plan.old, plan.new):
                slots.append(
                    slot.model_copy(
                        update={
                            "binding": slot.binding.model_copy(
                                update={
                                    "product_id": operation.product,
                                    "environment_id": operation.instance,
                                    "release_id": operation.request.request_id,
                                }
                            ),
                            "runtime_identity": slot.runtime_identity.model_copy(
                                update={
                                    "product": operation.product,
                                    "context": operation.context,
                                    "instance": operation.instance,
                                    "release_id": operation.request.request_id
                                    if slot == plan.new
                                    else "previous",
                                }
                            ),
                        }
                    )
                )
            plan = type(plan).model_validate(
                plan.model_copy(
                    update={
                        "release_id": operation.request.request_id,
                        "old": slots[0],
                        "new": slots[1],
                    }
                ).model_dump()
            )
            identity = cutover_identity(
                operation.product,
                operation.context,
                operation.instance,
                operation.request.request_id,
            )
            progress = OdooCutoverRecord(
                record_id=identity,
                plan=plan,
                began_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
                step="switching",
                error="TimeoutError",
            )
            deployment = DeploymentRecord(
                record_id="deployment-cutover-" + identity,
                context=operation.context,
                instance=operation.instance,
                source_git_ref=plan.candidate.source_commit,
                deploy=DeploymentEvidence(
                    deploy_mode="atomic-two-slot", target_type="compose", target_name="fixture"
                ),
                odoo_cutover=progress,
            )

            def claim(owner: str) -> OdooProdPromotionOperationRecord | None:
                return store.claim_odoo_cutover_reconciliation_record(
                    lease_owner=owner,
                    lease_expires_at="2026-10-10T21:05:00Z",
                    claimed_at="2026-10-10T21:00:00Z",
                    max_attempts=3,
                )

            self.assertIsNone(claim("worker-a"))
            for step, error in (("maintaining", "TimeoutError"), ("switching", "ValueError")):
                deployment.odoo_cutover = progress.model_copy(update={"step": step, "error": error})
                store.write_deployment_record(deployment)
                self.assertIsNone(claim("worker-a"))
            deployment.odoo_cutover = progress
            store.write_deployment_record(deployment)
            with ThreadPoolExecutor(max_workers=2) as workers:
                claims = list(workers.map(claim, ("worker-a", "worker-b")))
            self.assertEqual(sum(item is not None for item in claims), 1)
            claimed = next(item for item in claims if item is not None)
            self.assertIsNotNone(claimed)
            self.assertIsNone(claim("worker-c"))
            assert claimed is not None
            self.assertIn(claimed.lease_owner, {"worker-a", "worker-b"})
            self.assertEqual(claimed.attempt, 2)

            # A denied resume retains its lane and provider budget. Its update
            # time is durable, so a fresh worker cannot immediately reclaim it.
            denied_at = datetime.fromisoformat(claimed.updated_at.replace("Z", "+00:00"))
            denied = held.model_copy(
                update={"updated_at": denied_at.isoformat(), "error_code": "authorization_denied"}
            )
            store.write_odoo_prod_promotion_operation_record(denied)
            with closing(PostgresRecordStore(database_url=store.database_url)) as restarted:

                def retry(at: datetime) -> OdooProdPromotionOperationRecord | None:
                    return restarted.claim_odoo_cutover_reconciliation_record(
                        lease_owner="restarted",
                        lease_expires_at=(at + timedelta(minutes=5)).isoformat(),
                        claimed_at=at.isoformat(),
                        max_attempts=3,
                    )

                self.assertIsNone(retry(denied_at))
                self.assertIsNone(retry(denied_at + ODOO_CUTOVER_RECONCILIATION_DELAY / 2))
                unchanged = restarted.read_odoo_prod_promotion_operation_record(denied.operation_id)
                self.assertEqual(unchanged, denied)
                resumed = retry(denied_at + ODOO_CUTOVER_RECONCILIATION_DELAY)
                assert resumed is not None
                self.assertEqual(resumed.attempt, denied.attempt + 1)
            store.write_odoo_prod_promotion_operation_record(held.model_copy(update={"attempt": 3}))
            self.assertIsNone(claim("worker-b"))

    def test_pending_rollback_precedes_held_reconciliation(self) -> None:
        with TemporaryDirectory() as temporary:
            store = self.store(temporary)
            rollback = _rollback_operation()
            recovery = Mock(side_effect=AssertionError("held recovery must yield"))
            with (
                patch.object(store, "claim_odoo_cutover_reconciliation_record", recovery),
                patch.object(
                    store, "claim_next_odoo_prod_rollback_operation_record", return_value=rollback
                ),
                patch(
                    "control_plane.workflows.odoo_stable_operation_worker._execute_prod_rollback_operation",
                    return_value=True,
                ) as execute,
            ):
                result = run_odoo_stable_operation_worker_once(
                    record_store=store,
                    control_plane_root_path=Path(temporary),
                    lease_owner="worker",
                    cutover_provider_factory=Mock(),
                )
            self.assertEqual(result.operation_id, rollback.operation_id)
            self.assertTrue(result.terminal_write_committed)
            execute.assert_called_once()
            recovery.assert_not_called()
