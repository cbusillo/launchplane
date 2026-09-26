from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterator
import unittest
from unittest.mock import Mock, patch

import click
from pydantic import ValidationError

from control_plane.contracts.durable_operation_authorization import DurableOperationAuthorization
from control_plane.contracts.production_backup_authority import ProductionBackupPolicyRecord
from control_plane.contracts.production_backup_gate import ProductionBackupGateWorkerRequest
from control_plane.contracts.promotion_record import BackupGateEvidence
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.production_backup_gate import enqueue_production_backup_gate
from control_plane.workflows.production_promotion_backup import (
    GENERIC_WEB_PROMOTION_BACKUP_ACTION,
    ODOO_PROMOTION_BACKUP_ACTION,
    production_promotion_backup_guard,
    require_production_promotion_backup,
)
from control_plane.workflows.generic_web_promotion import (
    GenericWebProdPromotionRequest,
    execute_generic_web_prod_promotion,
)
from control_plane.workflows.odoo_prod_promotion import (
    OdooProdPromotionRequest,
    execute_odoo_prod_promotion,
)
from control_plane.workflows.odoo_prod_promotion_run import (
    OdooProdPromotionRunRequest,
    execute_odoo_prod_promotion_run,
)
from control_plane.workflows.verireel_prod_backup_gate_operation_worker import (
    run_verireel_prod_backup_gate_operation_worker_once,
)
from tests.support.durable_operations import (
    durable_operation_authorization_payload,
    durable_operation_policy_record,
)
from tests.test_generic_web_promotion import _profile
from tests.test_odoo_prod_promotion_run import _inputs_result
from tests.test_production_backup_provider import BackupHost, _binding, setup_memory_files


class ProductionPromotionBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        setup_memory_files(self)
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store_count = 0

    def capture(self, action: str = ODOO_PROMOTION_BACKUP_ACTION) -> PostgresRecordStore:
        self.store_count += 1
        store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{self.root / str(self.store_count)}.sqlite3"
        )
        self.addCleanup(store.close)
        store.ensure_schema()
        original = _binding()
        policy = ProductionBackupPolicyRecord.model_validate(
            {
                **original.policy.model_dump(),
                "policy_id": "",
                "record_id": "",
                "policy_digest": "",
                "promotion_action": action,
            }
        )
        binding = ProductionBackupGateWorkerRequest(
            request=original.request.model_copy(update={"promotion_action": action}),
            policy=policy,
            source_target=original.source_target,
            destination_target=original.destination_target,
        )
        store.write_production_backup_target_record(binding.source_target)
        store.write_production_backup_target_record(binding.destination_target)
        store.write_production_backup_policy_record(policy)
        payload = durable_operation_authorization_payload(
            action="production_backup_gate.execute",
            managed_rule_id="example-backup",
            product="example-product",
            context="example-product",
            instances=("prod",),
        )
        store.seed_authz_policy_if_absent(durable_operation_policy_record(payload))
        enqueue_production_backup_gate(
            record_store=store,
            request=binding.request,
            authorization=DurableOperationAuthorization.model_validate(payload),
            operation_key="example-caller|capture",
        )
        with (
            patch(
                "control_plane.workflows.production_backup_gate.enforce_worker_runtime_key_safety"
            ),
            patch(
                "control_plane.workflows.production_backup_gate.runtime_environments.resolve_runtime_environment_values",
                return_value={
                    "PRODUCTION_BACKUP_SSH_PRIVATE_KEY": "synthetic",
                    "PRODUCTION_BACKUP_SSH_KNOWN_HOSTS": "synthetic",
                },
            ),
            patch(
                "control_plane.workflows.production_backup_provider.subprocess.run",
                side_effect=BackupHost().run,
            ),
        ):
            result = run_verireel_prod_backup_gate_operation_worker_once(
                record_store=store, control_plane_root_path=self.root, lease_owner="test-worker"
            )
        self.assertTrue(result.terminal_write_committed)
        self.assertEqual(store.read_backup_gate_record("backup-example").status, "pass")
        return store

    def require(
        self, store: PostgresRecordStore, action: str = ODOO_PROMOTION_BACKUP_ACTION
    ) -> BackupGateEvidence:
        return require_production_promotion_backup(
            record_store=store,
            product="example-product",
            context="example-product",
            instance="prod",
            promotion_action=action,
            backup_record_id="backup-example",
        )

    def test_worker_capture_binds_exact_policy_and_operation(self) -> None:
        store = self.capture()
        evidence = self.require(store)
        operation = store.list_verireel_prod_backup_gate_operation_records()[0]
        self.assertEqual(evidence.evidence["backup_operation_id"], operation.operation_id)
        self.assertEqual(evidence.evidence["backup_record_id"], operation.backup_record_id)
        self.assertIsNotNone(operation.binding)
        assert operation.binding is not None
        self.assertEqual(evidence.evidence["policy_digest"], operation.binding.policy.policy_digest)
        self.assertTrue(evidence.required)

    def test_missing_stale_partial_mismatched_and_failed_evidence_blocks_all_entrypoints(
        self,
    ) -> None:
        for action in (ODOO_PROMOTION_BACKUP_ACTION, GENERIC_WEB_PROMOTION_BACKUP_ACTION):
            store = self.capture(action)
            original = store.read_backup_gate_record("backup-example")
            operation = store.list_verireel_prod_backup_gate_operation_records()[0]
            assert operation.result is not None
            for case, change in (
                ("partial", {"capture_status": "partial"}),
                ("missing snapshot", {"snapshot_name": ""}),
                ("missing PBS", {"independent_backup_id": ""}),
                ("wrong policy", {"policy_digest": "0" * 64}),
                ("wrong target", {"source_target_digest": "0" * 64}),
                ("invalid time", {"snapshot_finished_at": "invalid"}),
                ("stale", {"snapshot_finished_at": "2020-01-01T00:00:00Z"}),
                ("future", {"independent_backup_finished_at": "2099-01-01T00:00:00Z"}),
                ("failed", {}),
            ):
                with self.subTest(action=action, case=case):
                    evidence = {**original.evidence, **change}
                    store.write_backup_gate_record(
                        original.model_copy(
                            update={
                                "evidence": evidence,
                                "status": "fail" if case == "failed" else "pass",
                            }
                        )
                    )
                    store.write_verireel_prod_backup_gate_operation_record(
                        operation.model_copy(
                            update={
                                "result": operation.result.model_copy(update={"evidence": evidence})
                            }
                        )
                    )
                    with self.assertRaises(click.ClickException):
                        self.require(store, action)
                    self.assert_entrypoints_block(store, action)
            store.write_backup_gate_record(original)
            store.write_verireel_prod_backup_gate_operation_record(operation)
            with self.subTest(action=action, case="missing"):
                self.assert_entrypoints_block(store, action, backup_record_id="missing-backup")

    def assert_entrypoints_block(
        self, store: PostgresRecordStore, action: str, *, backup_record_id: str = "backup-example"
    ) -> None:
        if action == GENERIC_WEB_PROMOTION_BACKUP_ACTION:
            profile = _profile().model_copy(update={"product": "example-product"})
            lanes = tuple(
                lane.model_copy(update={"context": "example-product"}) for lane in profile.lanes
            )
            request = GenericWebProdPromotionRequest(
                product="example-product",
                artifact_id="example-artifact",
                backup_record_id=backup_record_id,
            )
            with (
                patch(
                    "control_plane.workflows.generic_web_promotion.resolve_generic_web_promotion_lanes",
                    return_value=(profile, *lanes),
                ),
                patch(
                    "control_plane.workflows.generic_web_promotion._resolve_source_inventory_inputs",
                    return_value=request,
                ),
                patch("control_plane.workflows.generic_web_promotion.require_release_approval"),
                patch(
                    "control_plane.workflows.generic_web_promotion.execute_generic_web_deploy"
                ) as deploy,
                self.assertRaises(click.ClickException),
            ):
                execute_generic_web_prod_promotion(
                    control_plane_root=self.root, record_store=store, request=request
                )
            deploy.assert_not_called()
            return
        with (
            patch("control_plane.workflows.odoo_prod_promotion.require_release_approval"),
            patch(
                "control_plane.workflows.odoo_prod_promotion.execute_odoo_stable_target_replacement_apply"
            ) as replacement,
        ):
            direct = execute_odoo_prod_promotion(
                control_plane_root=self.root,
                state_dir=self.root,
                database_url=store.database_url,
                record_store=store,
                request=OdooProdPromotionRequest(
                    product="example-product",
                    context="example-product",
                    artifact_id="example-artifact",
                    backup_record_id="logical-backup",
                    infrastructure_backup_record_id=backup_record_id,
                ),
            )
        self.assertEqual(direct.promotion_status, "fail")
        replacement.assert_not_called()
        with (
            patch("control_plane.workflows.odoo_prod_promotion_run.require_release_approval"),
            patch(
                "control_plane.workflows.odoo_prod_promotion_run.resolve_odoo_prod_promotion_inputs",
                return_value=_inputs_result(),
            ),
            patch(
                "control_plane.workflows.odoo_prod_promotion_run.execute_odoo_prod_backup_gate"
            ) as logical_backup,
            patch(
                "control_plane.workflows.odoo_prod_promotion_run.execute_odoo_prod_promotion"
            ) as promote,
        ):
            run = execute_odoo_prod_promotion_run(
                control_plane_root=self.root,
                state_dir=self.root,
                database_url=store.database_url,
                record_store=store,
                request=OdooProdPromotionRunRequest(
                    product="example-product",
                    context="example-product",
                    request_id="example-run",
                    infrastructure_backup_record_id=backup_record_id,
                ),
            )
        self.assertEqual(run.run_status, "blocked")
        logical_backup.assert_not_called()
        promote.assert_not_called()

    def test_changed_policy_blocks_before_effect(self) -> None:
        store = self.capture()
        policy = store.list_production_backup_policy_records()[0]
        replacement = ProductionBackupPolicyRecord.model_validate(
            {
                **policy.model_dump(),
                "record_id": "",
                "policy_revision": 2,
                "policy_digest": "",
                "supersedes_record_id": policy.record_id,
            }
        )
        store.write_production_backup_policy_record(replacement)
        with self.assertRaisesRegex(click.ClickException, "changed"):
            self.require(store)
        self.assert_entrypoints_block(store, ODOO_PROMOTION_BACKUP_ACTION)

    def test_later_capture_invalidates_possibly_pruned_snapshot(self) -> None:
        store = self.capture()
        first = store.list_verireel_prod_backup_gate_operation_records()[0]
        assert first.binding is not None
        later_request = first.binding.request.model_copy(
            update={"backup_record_id": "later-backup"}
        )
        later = first.model_copy(
            update={
                "operation_id": "later-operation",
                "backup_record_id": "later-backup",
                "request": later_request,
                "binding": first.binding.model_copy(update={"request": later_request}),
                "started_at": (datetime.now(UTC) + timedelta(seconds=1)).isoformat(),
            }
        )
        store.write_verireel_prod_backup_gate_operation_record(later)
        with self.assertRaisesRegex(click.ClickException, "superseded"):
            self.require(store)

    def test_guard_refuses_lock_loss_before_first_effect(self) -> None:
        store = self.capture()
        check = Mock()

        @contextmanager
        def lock(_key: str) -> Iterator[Mock]:
            yield check

        with patch.object(store, "production_backup_source_lock", side_effect=lock):
            with production_promotion_backup_guard(
                record_store=store,
                product="example-product",
                context="example-product",
                instance="prod",
                promotion_action=ODOO_PROMOTION_BACKUP_ACTION,
                backup_record_id="backup-example",
            ) as checkpoint:
                check.side_effect = RuntimeError("lost connection")
                with self.assertRaisesRegex(click.ClickException, "lock was lost"):
                    checkpoint("first_effect")
                check.side_effect = None

    def test_admitted_promotion_finishes_after_policy_change_and_refused_capture(self) -> None:
        store = self.capture()
        original = store.list_verireel_prod_backup_gate_operation_records()[0]
        assert original.binding is not None and original.result is not None
        with production_promotion_backup_guard(
            record_store=store,
            product="example-product",
            context="example-product",
            instance="prod",
            promotion_action=ODOO_PROMOTION_BACKUP_ACTION,
            backup_record_id="backup-example",
        ) as checkpoint:
            checkpoint("target_update")
            request = original.binding.request.model_copy(
                update={"backup_record_id": "refused-backup"}
            )
            refused = original.model_copy(
                update={
                    "operation_id": "refused-operation",
                    "backup_record_id": "refused-backup",
                    "request": request,
                    "binding": original.binding.model_copy(update={"request": request}),
                    "status": "fail",
                    "error_code": "backup_source_busy",
                    "error_message": "busy",
                    "progress_evidence": {},
                    "result": original.result.model_copy(
                        update={"backup_status": "fail", "evidence": {}}
                    ),
                }
            )
            store.write_verireel_prod_backup_gate_operation_record(refused)
            self.require(store)
            policy = original.binding.policy
            store.write_production_backup_policy_record(
                ProductionBackupPolicyRecord.model_validate(
                    {
                        **policy.model_dump(),
                        "record_id": "",
                        "policy_revision": 2,
                        "policy_digest": "",
                        "supersedes_record_id": policy.record_id,
                    }
                )
            )
            checkpoint("odoo_module_update")
        with self.assertRaisesRegex(click.ClickException, "changed"):
            self.require(store)

    def test_guard_rechecks_evidence_immediately_before_first_effect(self) -> None:
        store = self.capture()
        with production_promotion_backup_guard(
            record_store=store,
            product="example-product",
            context="example-product",
            instance="prod",
            promotion_action=ODOO_PROMOTION_BACKUP_ACTION,
            backup_record_id="backup-example",
        ) as checkpoint:
            record = store.read_backup_gate_record("backup-example")
            store.write_backup_gate_record(record.model_copy(update={"status": "fail"}))
            with self.assertRaisesRegex(click.ClickException, "incomplete or mismatched"):
                checkpoint("target_update")

    def test_guard_records_lock_loss_without_interrupting_admitted_deployment(self) -> None:
        store = self.capture()
        check = Mock()

        @contextmanager
        def lock(_key: str) -> Iterator[Mock]:
            yield check

        with (
            patch.object(store, "production_backup_source_lock", side_effect=lock),
            self.assertLogs(level="WARNING"),
        ):
            with production_promotion_backup_guard(
                record_store=store,
                product="example-product",
                context="example-product",
                instance="prod",
                promotion_action=ODOO_PROMOTION_BACKUP_ACTION,
                backup_record_id="backup-example",
            ) as checkpoint:
                checkpoint("target_update")
                check.side_effect = RuntimeError("lost connection")
                checkpoint("post_deploy")
        self.assertEqual(checkpoint.evidence["source_lock_status"], "lost_after_effect")
        self.assertIn("source_lock_lost_at", checkpoint.evidence)

        check.side_effect = None
        with (
            patch.object(store, "production_backup_source_lock", side_effect=lock),
            self.assertLogs(level="WARNING"),
        ):
            with production_promotion_backup_guard(
                record_store=store,
                product="example-product",
                context="example-product",
                instance="prod",
                promotion_action=ODOO_PROMOTION_BACKUP_ACTION,
                backup_record_id="backup-example",
            ) as completed:
                completed("target_update")
                check.side_effect = RuntimeError("lost connection on completion")
        self.assertEqual(completed.evidence["source_lock_status"], "lost_after_effect")

    def test_generic_web_cannot_opt_out(self) -> None:
        with self.assertRaises(ValidationError):
            GenericWebProdPromotionRequest.model_validate(
                {"product": "example", "backup_required": False}
            )
