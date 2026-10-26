from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
import unittest
from unittest.mock import patch

from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.odoo_cutover import OdooCutoverPlan
from control_plane.contracts.promotion_record import DeploymentEvidence
from control_plane.dokploy.cutover_execution import require_cutover_execution
from control_plane.odoo_cutover_plan import cutover_identity
from control_plane.odoo_cutover_recovery import DefiniteBackupFailure
from control_plane.odoo_cutover_storage import DeploymentCutoverStore
from control_plane.workflows.odoo_cutover import execute_odoo_cutover
from control_plane.workflows.odoo_cutover_deployment import execute_odoo_cutover_deployment
from control_plane.workflows.odoo_stable_operation_worker import (
    run_odoo_stable_operation_worker_once,
)
from tests.test_odoo_cutover import IsolatedCutoverProvider
from tests.test_odoo_prod_promotion_operation import _operation, _passing_result, _store


class CutoverWorkerAbortTests(unittest.TestCase):
    def test_worker_reclaims_original_backup_and_checks_lease_before_abort_effects(self) -> None:
        for changing, expired in ((False, False), (True, False), (True, True)):
            with (
                self.subTest(changing=changing, expired=expired),
                TemporaryDirectory() as temporary,
            ):
                store = _store(temporary)
                self.addCleanup(store.close)
                operation = _operation()
                provider = IsolatedCutoverProvider(
                    Path(temporary) / "site", database_changing=changing
                )
                self.addCleanup(provider.engine.dispose)
                plan = provider.cutover
                identity = cutover_identity(
                    operation.product,
                    operation.context,
                    operation.instance,
                    operation.request.request_id,
                )
                slots = [
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
                                    "deployment_record_id": "deployment-cutover-" + identity
                                    if slot == plan.new
                                    else slot.runtime_identity.deployment_record_id,
                                }
                            ),
                        }
                    )
                    for slot in (plan.old, plan.new)
                ]
                plan = OdooCutoverPlan.model_validate(
                    {
                        **plan.model_dump(),
                        "release_id": operation.request.request_id,
                        "old": slots[0],
                        "new": slots[1],
                    }
                )
                provider.cutover = plan
                provider.target = plan.old
                provider.plan = provider.plan.model_copy(update={"binding": plan.old.binding})
                provider.binding = plan.old.binding
                deployment = DeploymentRecord(
                    record_id=plan.new.runtime_identity.deployment_record_id,
                    context=operation.context,
                    instance=operation.instance,
                    source_git_ref=plan.candidate.source_commit,
                    deploy=DeploymentEvidence(
                        deploy_mode="atomic-two-slot", target_type="compose", target_name="inert"
                    ),
                )
                cutover_store = DeploymentCutoverStore(store, deployment)
                with (
                    patch.object(provider, "backup", side_effect=TimeoutError()),
                    self.assertRaises(TimeoutError),
                ):
                    execute_odoo_cutover(plan, cutover_store, provider, record_id=identity)
                held = operation.model_copy(
                    update={
                        "status": "reconciliation_required",
                        "phase": "promotion_started",
                        "attempt": 1,
                        "error_code": "operation_reconciliation_required",
                        "error_message": "Native completion lost.",
                    }
                )
                store.create_odoo_prod_promotion_operation_record_if_no_active_lane(held)
                before = cutover_store.load(identity)

                def run(**kwargs: Any) -> Any:
                    checkpoint = cast(Callable[[str], None], kwargs["provider_effect_checkpoint"])

                    def guarded(phase: str) -> None:
                        from control_plane.workflows.odoo_stable_target_replacement import (
                            TARGET_REPLACEMENT_FIRST_PROVIDER_WRITE,
                        )

                        checkpoint(TARGET_REPLACEMENT_FIRST_PROVIDER_WRITE)

                    def failed_backup(current: OdooCutoverPlan) -> Any:
                        if expired:
                            leased = store.read_odoo_prod_promotion_operation_record(
                                operation.operation_id
                            )
                            store.write_odoo_prod_promotion_operation_record(
                                leased.model_copy(
                                    update={
                                        "lease_expires_at": datetime(
                                            2000, 1, 1, tzinfo=UTC
                                        ).isoformat()
                                    }
                                )
                            )
                        raise DefiniteBackupFailure()

                    original_readiness = provider.readiness

                    def readiness(current: OdooCutoverPlan, slot: Any) -> Any:
                        require_cutover_execution(plan)
                        return original_readiness(current, slot)

                    with (
                        patch.object(provider, "backup", side_effect=failed_backup),
                        patch.object(provider, "readiness", side_effect=readiness),
                    ):
                        applied = execute_odoo_cutover_deployment(
                            plan=plan,
                            record_store=store,
                            provider=provider,
                            context=operation.context,
                            instance=operation.instance,
                            checkpoint=guarded,
                        )
                        self.assertEqual(applied.deploy_status, "fail")
                    return _passing_result().model_copy(
                        update={
                            "run_status": "fail",
                            "promotion_status": "fail",
                            "error_message": "Aborted before maintenance.",
                        }
                    )

                with patch(
                    "control_plane.workflows.odoo_stable_operation_worker.execute_odoo_prod_promotion_run",
                    side_effect=run,
                ):
                    result = run_odoo_stable_operation_worker_once(
                        record_store=store,
                        control_plane_root_path=Path(temporary),
                        lease_owner="fresh-worker",
                        cutover_provider_factory=lambda current: provider,
                    )
                self.assertEqual(result.operation_id, operation.operation_id)
                after = cutover_store.load(identity)
                self.assertEqual(after.step, "aborting" if expired else "aborted")
                self.assertEqual(provider.maintenance_count, 0)
                self.assertEqual(after.plan, before.plan)
                latest = store.read_odoo_prod_promotion_operation_record(operation.operation_id)
                self.assertEqual(latest.idempotency_key, operation.idempotency_key)
                if expired:
                    self.assertIsNone(after.ended_at)
                else:
                    self.assertEqual(latest.status, "fail")
                    self.assertEqual(provider.target, plan.old)
