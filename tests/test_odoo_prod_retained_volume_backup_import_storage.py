import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from control_plane.contracts.odoo_prod_retained_volume_backup_import_operation import (
    OdooProdRetainedVolumeBackupImportOperationRecord,
)
from control_plane.contracts.odoo_stable_bootstrap_operation import (
    OdooStableBootstrapOperationRecord,
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.odoo_stable_lane import (
    OdooStableLaneOperationConflictError,
    OdooStableLaneOperationRecord,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.durable_operations import durable_operation_authorization_payload
from tests.test_odoo_prod_retained_volume_backup_import import _operation
from tests.test_odoo_stable_operation_worker import (
    _bootstrap_payload,
    _replacement_payload,
    _restore_operation,
)


def _retained_operation_for_restore_lane() -> OdooProdRetainedVolumeBackupImportOperationRecord:
    payload = _operation(
        operation_kind="plan",
        operation_id="retained-plan-operation-cm-prod",
    ).model_dump(mode="json")
    payload["product"] = "odoo-tenant-cm"
    payload["context"] = "cm"
    request_payload = payload["request"]
    assert isinstance(request_payload, dict)
    request = dict(request_payload)
    request["product"] = "odoo-tenant-cm"
    request["context"] = "cm"
    payload["request"] = request
    payload["authorization"] = durable_operation_authorization_payload(
        action="odoo_prod_retained_volume_backup_import_plan.execute",
        managed_rule_id="retained-import-plan-cm-prod",
        product="odoo-tenant-cm",
        context="cm",
        instances=("prod",),
    )
    return OdooProdRetainedVolumeBackupImportOperationRecord.model_validate(payload)


def _bootstrap_operation_for_restore_lane() -> OdooStableBootstrapOperationRecord:
    payload = _bootstrap_payload("bootstrap-operation-cm-prod")
    payload["instance"] = "prod"
    request_payload = payload["request"]
    assert isinstance(request_payload, dict)
    request = dict(request_payload)
    request["instance"] = "prod"
    request["confirmation"] = "bootstrap cm prod"
    payload["request"] = request
    payload["authorization"] = durable_operation_authorization_payload(
        action="odoo_stable_bootstrap.execute",
        managed_rule_id="bootstrap-cm-prod",
        product="odoo-tenant-cm",
        context="cm",
        instances=("prod",),
    )
    return OdooStableBootstrapOperationRecord.model_validate(payload)


def _target_replacement_operation_for_restore_lane() -> OdooStableTargetReplacementOperationRecord:
    payload = _replacement_payload("target-replacement-operation-cm-prod")
    payload["instance"] = "prod"
    request_payload = payload["request"]
    assert isinstance(request_payload, dict)
    request = dict(request_payload)
    request["instance"] = "prod"
    request["confirmation"] = ""
    request["data_source_mode"] = "existing"
    request["allow_empty_data"] = False
    payload["request"] = request
    payload["authorization"] = durable_operation_authorization_payload(
        action="odoo_target_replacement_apply.execute",
        managed_rule_id="target-replacement-cm-prod",
        product="odoo-tenant-cm",
        context="cm",
        instances=("prod",),
    )
    return OdooStableTargetReplacementOperationRecord.model_validate(payload)


def _stable_lane_operations() -> dict[str, OdooStableLaneOperationRecord]:
    return {
        "stable_bootstrap": _bootstrap_operation_for_restore_lane(),
        "target_replacement": _target_replacement_operation_for_restore_lane(),
        "prod_backup_restore": _restore_operation(),
        "retained_volume_backup_import": _retained_operation_for_restore_lane(),
    }


def _create_stable_lane_operation(
    store: FilesystemRecordStore | PostgresRecordStore,
    operation: OdooStableLaneOperationRecord,
) -> bool:
    if isinstance(operation, OdooStableBootstrapOperationRecord):
        return store.create_odoo_stable_bootstrap_operation_record_if_no_active_lane(operation)[1]
    if isinstance(operation, OdooStableTargetReplacementOperationRecord):
        return store.create_odoo_stable_target_replacement_operation_record_if_no_active_lane(
            operation
        )[1]
    if isinstance(operation, OdooProdRetainedVolumeBackupImportOperationRecord):
        return (
            store.create_odoo_prod_retained_volume_backup_import_operation_record_if_no_active_lane(
                operation
            )[1]
        )
    return store.create_odoo_prod_backup_restore_operation_record_if_no_active_lane(operation)[1]


def _write_stable_lane_operation(
    store: FilesystemRecordStore | PostgresRecordStore,
    operation: OdooStableLaneOperationRecord,
) -> None:
    if isinstance(operation, OdooStableBootstrapOperationRecord):
        store.write_odoo_stable_bootstrap_operation_record(operation)
    elif isinstance(operation, OdooStableTargetReplacementOperationRecord):
        store.write_odoo_stable_target_replacement_operation_record(operation)
    elif isinstance(operation, OdooProdRetainedVolumeBackupImportOperationRecord):
        store.write_odoo_prod_retained_volume_backup_import_operation_record(operation)
    else:
        store.write_odoo_prod_backup_restore_operation_record(operation)


def _claim_stable_lane_operation(
    store: FilesystemRecordStore | PostgresRecordStore,
    operation_kind: str,
) -> OdooStableLaneOperationRecord | None:
    claim_kwargs = {
        "lease_owner": f"worker-{operation_kind}",
        "lease_expires_at": "2026-07-26T05:10:00Z",
        "claimed_at": "2026-07-26T05:00:00Z",
    }
    if operation_kind == "stable_bootstrap":
        return store.claim_next_odoo_stable_bootstrap_operation_record(**claim_kwargs)
    if operation_kind == "target_replacement":
        return store.claim_next_odoo_stable_target_replacement_operation_record(**claim_kwargs)
    if operation_kind == "prod_backup_restore":
        return store.claim_next_odoo_prod_backup_restore_operation_record(**claim_kwargs)
    return store.claim_next_odoo_prod_retained_volume_backup_import_operation_record(**claim_kwargs)


class OdooProdRetainedVolumeBackupImportStorageTests(unittest.TestCase):
    def test_claims_fence_preexisting_cross_kind_stable_lane_queue(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            stores = (
                FilesystemRecordStore(state_dir=root / "filesystem"),
                PostgresRecordStore(
                    database_url=f"sqlite+pysqlite:///{(root / 'launchplane.sqlite3').as_posix()}"
                ),
            )
            stores[1].ensure_schema()
            try:
                for store in stores:
                    with self.subTest(store=type(store).__name__):
                        for operation in _stable_lane_operations().values():
                            _write_stable_lane_operation(store, operation)

                        for blocked_kind in (
                            "retained_volume_backup_import",
                            "prod_backup_restore",
                            "target_replacement",
                        ):
                            self.assertIsNone(_claim_stable_lane_operation(store, blocked_kind))

                        claimed = _claim_stable_lane_operation(store, "stable_bootstrap")
                        self.assertIsNotNone(claimed)
                        assert claimed is not None
                        self.assertEqual(claimed.operation_id, "bootstrap-operation-cm-prod")
                        self.assertEqual(claimed.status, "running")

                        for blocked_kind in (
                            "retained_volume_backup_import",
                            "prod_backup_restore",
                            "target_replacement",
                        ):
                            self.assertIsNone(_claim_stable_lane_operation(store, blocked_kind))
            finally:
                stores[1].close()

    def test_filesystem_store_blocks_every_cross_kind_stable_lane_pair(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            for owner_kind in _stable_lane_operations():
                with self.subTest(owner_kind=owner_kind):
                    store = FilesystemRecordStore(state_dir=root / owner_kind)
                    operations = _stable_lane_operations()
                    self.assertTrue(_create_stable_lane_operation(store, operations[owner_kind]))
                    for contender_kind, contender in operations.items():
                        if contender_kind == owner_kind:
                            continue
                        with self.assertRaises(OdooStableLaneOperationConflictError) as raised:
                            _create_stable_lane_operation(store, contender)
                        self.assertEqual(raised.exception.owner.operation_kind, owner_kind)

    def test_postgres_store_blocks_every_cross_kind_stable_lane_pair(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            for owner_kind in _stable_lane_operations():
                with self.subTest(owner_kind=owner_kind):
                    database_path = root / f"{owner_kind}.sqlite3"
                    store = PostgresRecordStore(
                        database_url=f"sqlite+pysqlite:///{database_path.as_posix()}"
                    )
                    store.ensure_schema()
                    operations = _stable_lane_operations()
                    self.assertTrue(_create_stable_lane_operation(store, operations[owner_kind]))
                    for contender_kind, contender in operations.items():
                        if contender_kind == owner_kind:
                            continue
                        with self.assertRaises(OdooStableLaneOperationConflictError) as raised:
                            _create_stable_lane_operation(store, contender)
                        self.assertEqual(raised.exception.owner.operation_kind, owner_kind)

    def test_filesystem_store_blocks_cross_kind_stable_lane_operations(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            retained_operation = _retained_operation_for_restore_lane()
            restore_operation = _restore_operation()

            _, created = (
                store.create_odoo_prod_retained_volume_backup_import_operation_record_if_no_active_lane(
                    retained_operation
                )
            )
            with self.assertRaises(OdooStableLaneOperationConflictError) as raised:
                store.create_odoo_prod_backup_restore_operation_record_if_no_active_lane(
                    restore_operation
                )

        self.assertTrue(created)
        self.assertEqual(
            raised.exception.owner.operation_kind,
            "retained_volume_backup_import",
        )

    def test_postgres_store_blocks_cross_kind_stable_lane_operations(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{database_path.as_posix()}"
            )
            store.ensure_schema()
            restore_operation = _restore_operation()
            retained_operation = _retained_operation_for_restore_lane()

            _, created = store.create_odoo_prod_backup_restore_operation_record_if_no_active_lane(
                restore_operation
            )
            with self.assertRaises(OdooStableLaneOperationConflictError) as raised:
                store.create_odoo_prod_retained_volume_backup_import_operation_record_if_no_active_lane(
                    retained_operation
                )

        self.assertTrue(created)
        self.assertEqual(raised.exception.owner.operation_kind, "prod_backup_restore")

    def test_filesystem_store_round_trips_plan_and_apply_kinds(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            plan_operation = _operation(
                operation_kind="plan",
                operation_id="retained-plan-operation-1",
            )
            store.write_odoo_prod_retained_volume_backup_import_operation_record(plan_operation)

            stored = store.read_odoo_prod_retained_volume_backup_import_operation_record(
                plan_operation.operation_id
            )
            plan_records = store.list_odoo_prod_retained_volume_backup_import_operation_records(
                operation_kind="plan"
            )
            apply_records = store.list_odoo_prod_retained_volume_backup_import_operation_records(
                operation_kind="apply"
            )

        self.assertEqual(stored, plan_operation)
        self.assertEqual(plan_records, (plan_operation,))
        self.assertEqual(apply_records, ())

    def test_postgres_store_single_flight_checkpoint_and_kind_filter(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{database_path.as_posix()}"
            )
            store.ensure_schema()
            operation = _operation(
                operation_kind="plan",
                operation_id="retained-plan-operation-1",
            )

            persisted, created = (
                store.create_odoo_prod_retained_volume_backup_import_operation_record_if_no_active_lane(
                    operation
                )
            )
            duplicate, duplicate_created = (
                store.create_odoo_prod_retained_volume_backup_import_operation_record_if_no_active_lane(
                    _operation(
                        operation_kind="apply",
                        operation_id="retained-apply-operation-2",
                    )
                )
            )
            claimed = store.claim_next_odoo_prod_retained_volume_backup_import_operation_record(
                lease_owner="worker-a",
                lease_expires_at="2026-07-26T00:10:00Z",
                claimed_at="2026-07-26T00:01:00Z",
            )
            assert claimed is not None
            checkpointed = (
                store.checkpoint_odoo_prod_retained_volume_backup_import_operation_record(
                    operation_id=claimed.operation_id,
                    lease_owner="worker-a",
                    phase="inspection_started",
                    checkpointed_at="2026-07-26T00:02:00Z",
                    evidence={"provider_effect": "schedule_trigger"},
                )
            )
            plan_records = store.list_odoo_prod_retained_volume_backup_import_operation_records(
                operation_kind="plan"
            )

        self.assertTrue(created)
        self.assertEqual(persisted.operation_id, operation.operation_id)
        self.assertFalse(duplicate_created)
        self.assertEqual(duplicate.operation_id, operation.operation_id)
        self.assertIsNotNone(checkpointed)
        assert checkpointed is not None
        self.assertEqual(checkpointed.phase, "inspection_started")
        self.assertEqual(
            checkpointed.checkpoints[-1].evidence["provider_effect"],
            "schedule_trigger",
        )
        self.assertEqual(len(plan_records), 1)

    def test_postgres_store_fails_expired_import_after_provider_effect(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{database_path.as_posix()}"
            )
            store.ensure_schema()
            operation = _operation(
                operation_kind="apply",
                operation_id="retained-apply-operation-1",
            )
            store.write_odoo_prod_retained_volume_backup_import_operation_record(operation)
            claimed = store.claim_next_odoo_prod_retained_volume_backup_import_operation_record(
                lease_owner="worker-a",
                lease_expires_at="2026-07-26T00:02:00Z",
                claimed_at="2026-07-26T00:01:00Z",
            )
            assert claimed is not None
            checkpointed = (
                store.checkpoint_odoo_prod_retained_volume_backup_import_operation_record(
                    operation_id=claimed.operation_id,
                    lease_owner="worker-a",
                    phase="provider_import_started",
                    checkpointed_at="2026-07-26T00:01:30Z",
                    evidence={"provider_effect": "schedule_trigger"},
                )
            )
            assert checkpointed is not None

            recovered_ids = (
                store.recover_expired_odoo_prod_retained_volume_backup_import_operation_records(
                    now="2026-07-26T00:03:00Z",
                    safe_phases=("created", "running", "validated"),
                    max_attempts=3,
                )
            )
            stored = store.read_odoo_prod_retained_volume_backup_import_operation_record(
                operation.operation_id
            )

        self.assertEqual(recovered_ids, (operation.operation_id,))
        self.assertEqual(stored.status, "reconciliation_required")
        self.assertEqual(stored.phase, "provider_import_started")
        self.assertEqual(stored.error_code, "operation_reconciliation_required")
        self.assertIn("operator reconciliation", stored.error_message)


if __name__ == "__main__":
    unittest.main()
