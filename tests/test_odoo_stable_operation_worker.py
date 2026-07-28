import json
import logging
import unittest
from collections.abc import Callable
from pathlib import Path
from threading import Event
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import patch

from click import ClickException
from click.testing import CliRunner

from control_plane.cli import main
from control_plane.contracts.odoo_stable_bootstrap_operation import (
    OdooStableBootstrapOperationRecord,
)
from control_plane.contracts.odoo_prod_backup_restore import (
    ODOO_PROD_BACKUP_RESTORE_CONFIRMATION,
    OdooProdBackupRestorePlan,
    OdooProdBackupRestoreResult,
    build_odoo_prod_backup_restore_plan_fingerprint,
)
from control_plane.contracts.odoo_prod_backup_restore_operation import (
    OdooProdBackupRestoreCheckpoint,
    OdooProdBackupRestoreOperationRecord,
)
from control_plane.contracts.odoo_prod_retained_volume_backup_import import (
    OdooProdRetainedVolumeBackupImportPlan,
)
from control_plane.contracts.odoo_stable_bootstrap import OdooStableBootstrapResult
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.contracts.odoo_stable_target_replacement import (
    OdooStableTargetReplacementApplyResult,
)
from control_plane.odoo_stable_bootstrap_http import (
    OdooStableBootstrapEnvelope,
    enqueue_odoo_stable_bootstrap_operation,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.workflows.odoo_stable_operation_worker import (
    OdooStableOperationWorkerLoopResult,
    build_odoo_stable_operation_worker_status,
    reconcile_stale_odoo_stable_operation_records,
    run_odoo_stable_operation_worker_loop,
    run_odoo_stable_operation_worker_once,
)
from tests.support.durable_operations import (
    durable_operation_authorization_payload,
    durable_operation_policy_record,
)
from tests.test_odoo_prod_retained_volume_backup_import import (
    _Store as _RetainedImportStore,
    _build_plan as _build_retained_import_plan,
    _operation as _retained_import_operation,
)


_BOOTSTRAP_AUTHORIZATION = durable_operation_authorization_payload(
    action="odoo_stable_bootstrap.execute",
    managed_rule_id="cm-testing-bootstrap",
)
_REPLACEMENT_AUTHORIZATION = durable_operation_authorization_payload(
    action="odoo_target_replacement_apply.execute",
    managed_rule_id="cm-testing-target-replacement",
)
_RESTORE_AUTHORIZATION = durable_operation_authorization_payload(
    action="odoo_prod_backup_restore_apply.execute",
    managed_rule_id="cm-prod-backup-restore",
    context="cm",
    instances=("prod",),
)
_RETAINED_PLAN_AUTHORIZATION = durable_operation_authorization_payload(
    action="odoo_prod_retained_volume_backup_import_plan.execute",
    managed_rule_id="retained-import-plan",
    product="example-odoo-product",
    context="example-context",
    instances=("prod",),
)
_RETAINED_APPLY_AUTHORIZATION = durable_operation_authorization_payload(
    action="odoo_prod_retained_volume_backup_import_apply.execute",
    managed_rule_id="retained-import-apply",
    product="example-odoo-product",
    context="example-context",
    instances=("prod",),
)


def _bootstrap_payload(operation_id: str = "operation-cm-testing") -> dict[str, object]:
    return {
        "schema_version": 2,
        "operation_id": operation_id,
        "product": "odoo-tenant-cm",
        "context": "cm",
        "instance": "testing",
        "idempotency_key": "bootstrap-cm-testing",
        "request_fingerprint": "fingerprint-123",
        "request": {
            "schema_version": 1,
            "product": "odoo-tenant-cm",
            "context": "cm",
            "instance": "testing",
            "confirmation": "bootstrap cm testing",
        },
        "authorization": _BOOTSTRAP_AUTHORIZATION,
        "status": "pending",
        "phase": "created",
        "created_at": "2026-05-17T00:00:00Z",
        "updated_at": "2026-05-17T00:00:00Z",
    }


def _replacement_payload(operation_id: str = "operation-cm-testing") -> dict[str, object]:
    return {
        "schema_version": 2,
        "operation_id": operation_id,
        "product": "odoo-tenant-cm",
        "context": "cm",
        "instance": "testing",
        "idempotency_key": "replacement-cm-testing",
        "idempotency_scope": "github-actions|cbusillo/launchplane|apply.yml|subject-a",
        "request_fingerprint": "fingerprint-123",
        "request": {
            "schema_version": 1,
            "product": "odoo-tenant-cm",
            "instance": "testing",
            "strategy": "recreate-in-place",
            "confirmation": "recreate cm testing",
            "data_source_mode": "empty",
            "allow_empty_data": True,
        },
        "authorization": _REPLACEMENT_AUTHORIZATION,
        "status": "pending",
        "phase": "created",
        "created_at": "2026-05-17T00:00:00Z",
        "updated_at": "2026-05-17T00:00:00Z",
    }


def _restore_operation(
    operation_id: str = "operation-cm-prod-restore",
) -> OdooProdBackupRestoreOperationRecord:
    provisional_plan = OdooProdBackupRestorePlan(
        plan_status="blocked",
        product="odoo-tenant-cm",
        context="cm",
        instance="prod",
        backup_record_id="backup-20260614",
        backup_record_created_at="2026-06-14T00:00:00Z",
        verification_record_id="verification-backup-20260614",
        verification_record_created_at="2026-07-25T00:00:00Z",
        verification_nonce="c" * 64,
        verification_status="pass",
        manifest_status="pass",
        sha256_status="pass",
        pg_restore_status="pass",
        tar_status="pass",
        staging_space_status="pass",
        target_id="compose-cm-prod",
        target_name="cm-prod",
        expected_current_artifact_id="artifact-cm-prod",
        expected_source_git_ref="abc1234",
        image_reference="ghcr.io/example/odoo@sha256:artifact",
        database_name="cm_prod",
        database_dump_path="/volumes/data/backups/cm_prod.dump",
        filestore_archive_path="/volumes/data/backups/cm_prod-filestore.tar.gz",
        manifest_path="/volumes/data/backups/manifest.json",
        database_dump_sha256="a" * 64,
        filestore_archive_sha256="b" * 64,
        database_dump_size=1,
        filestore_archive_size=1,
        pg_restore_entry_count=1,
        filestore_member_count=1,
        filestore_unpacked_size=1,
        data_volume_free_bytes=2,
        staging_required_bytes=1,
        old_db_volume="cm_prod_db_corrupt",
        new_db_volume="cm_prod_db_restore",
        data_volume="cm_prod_data",
        log_volume="cm_prod_logs",
        filestore_path="/volumes/data/filestore",
        filestore_staging_path="/volumes/data/filestore/.restore",
        filestore_quarantine_path="/volumes/data/filestore/cm_prod.quarantine",
        base_url="https://prod.example.test",
        health_url="https://prod.example.test/launchplane/health",
        expected_domain_hosts=("prod.example.test",),
        blockers=("fingerprint-pending",),
    )
    fingerprint = build_odoo_prod_backup_restore_plan_fingerprint(provisional_plan)
    plan = OdooProdBackupRestorePlan.model_validate(
        {
            **provisional_plan.model_dump(mode="json"),
            "plan_status": "ready",
            "plan_fingerprint": fingerprint,
            "blockers": [],
        }
    )
    return OdooProdBackupRestoreOperationRecord.model_validate(
        {
            "operation_id": operation_id,
            "product": "odoo-tenant-cm",
            "context": "cm",
            "instance": "prod",
            "idempotency_key": "restore-cm-prod",
            "idempotency_scope": "github-actions|example|restore.yml|subject-a",
            "request_fingerprint": "fingerprint-restore",
            "request": {
                "product": "odoo-tenant-cm",
                "context": "cm",
                "instance": "prod",
                "backup_record_id": "backup-20260614",
                "verification_record_id": "verification-backup-20260614",
                "expected_current_artifact_id": "artifact-cm-prod",
                "expected_old_db_volume": "cm_prod_db_corrupt",
                "expected_new_db_volume": "cm_prod_db_restore",
                "expected_data_volume": "cm_prod_data",
                "expected_log_volume": "cm_prod_logs",
                "plan_fingerprint": fingerprint,
                "confirmation": ODOO_PROD_BACKUP_RESTORE_CONFIRMATION,
            },
            "plan": plan.model_dump(mode="json"),
            "authorization": _RESTORE_AUTHORIZATION,
            "status": "pending",
            "phase": "created",
            "created_at": "2026-07-25T00:00:00Z",
            "updated_at": "2026-07-25T00:00:00Z",
        }
    )


class OdooStableOperationWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.authorization_policy_record = durable_operation_policy_record(
            _BOOTSTRAP_AUTHORIZATION,
            _REPLACEMENT_AUTHORIZATION,
            _RESTORE_AUTHORIZATION,
            _RETAINED_PLAN_AUTHORIZATION,
            _RETAINED_APPLY_AUTHORIZATION,
        )
        self.authorization_policy_patcher = patch(
            "control_plane.workflows.odoo_stable_operation_worker.read_active_authz_policy_record",
            return_value=self.authorization_policy_record,
        )
        self.authorization_policy_read_mock = self.authorization_policy_patcher.start()
        self.addCleanup(self.authorization_policy_patcher.stop)

    def test_worker_authorizes_retained_plan_once_before_provider_effects(
        self,
    ) -> None:
        self.authorization_policy_read_mock.side_effect = (
            self.authorization_policy_record,
            self.authorization_policy_record,
            durable_operation_policy_record(revision=43),
        )
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            operation = _retained_import_operation(
                operation_kind="plan",
                operation_id="retained-import-plan-operation-1",
            )
            store.write_odoo_prod_retained_volume_backup_import_operation_record(operation)
            plan = _build_retained_import_plan(_RetainedImportStore())

            def build_plan(
                **kwargs: object,
            ) -> OdooProdRetainedVolumeBackupImportPlan:
                phase_checkpoint = cast(
                    Callable[[str, dict[str, str]], None],
                    kwargs["phase_checkpoint"],
                )
                provider_checkpoint = cast(
                    Callable[[str, str], None],
                    kwargs["provider_effect_checkpoint"],
                )
                provider_checkpoint("inspection_started", "schedule_upsert")
                provider_checkpoint("inspection_started", "schedule_trigger")
                phase_checkpoint(
                    "planned",
                    {"plan_fingerprint": plan.plan_fingerprint},
                )
                return plan

            with patch(
                "control_plane.workflows.odoo_stable_operation_worker.build_odoo_prod_retained_volume_backup_import_plan",
                side_effect=build_plan,
            ):
                worker_result = run_odoo_stable_operation_worker_once(
                    record_store=store,
                    control_plane_root_path=Path(temporary_directory_name),
                    lease_owner="worker-a",
                    lease_seconds=30,
                    heartbeat_seconds=10,
                )

            stored = store.read_odoo_prod_retained_volume_backup_import_operation_record(
                operation.operation_id
            )

        self.assertEqual(
            worker_result.operation_kind,
            "odoo_prod_retained_volume_backup_import_plan",
        )
        self.assertEqual(stored.status, "pass")
        self.assertEqual(stored.phase, "completed")
        self.assertEqual(stored.result, plan)
        provider_effects = [
            checkpoint.evidence.get("provider_effect")
            for checkpoint in stored.checkpoints
            if checkpoint.evidence.get("provider_effect")
        ]
        self.assertEqual(provider_effects, ["schedule_upsert", "schedule_trigger"])
        self.assertEqual(self.authorization_policy_read_mock.call_count, 2)

    def test_worker_marks_blocked_retained_import_plan_failed(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            operation = _retained_import_operation(
                operation_kind="plan",
                operation_id="retained-import-plan-operation-blocked",
            )
            store.write_odoo_prod_retained_volume_backup_import_operation_record(operation)
            ready_plan = _build_retained_import_plan(_RetainedImportStore())
            blocked_plan = OdooProdRetainedVolumeBackupImportPlan.model_validate(
                {
                    **ready_plan.model_dump(mode="json"),
                    "plan_status": "blocked",
                    "plan_fingerprint": "",
                    "blockers": ["Retained source evidence drifted."],
                }
            )

            with patch(
                "control_plane.workflows.odoo_stable_operation_worker.build_odoo_prod_retained_volume_backup_import_plan",
                return_value=blocked_plan,
            ):
                worker_result = run_odoo_stable_operation_worker_once(
                    record_store=store,
                    control_plane_root_path=Path(temporary_directory_name),
                    lease_owner="worker-1",
                    lease_seconds=30,
                    heartbeat_seconds=1,
                )

            terminal = store.read_odoo_prod_retained_volume_backup_import_operation_record(
                operation.operation_id
            )
            self.assertEqual(worker_result.status, "worked")
            self.assertEqual(
                worker_result.operation_kind,
                "odoo_prod_retained_volume_backup_import_plan",
            )
            self.assertEqual(terminal.status, "fail")
            self.assertEqual(terminal.phase, "failed")
            self.assertEqual(terminal.result, blocked_plan)
            self.assertIn("source evidence drifted", terminal.error_message.lower())

    def test_worker_checkpoints_restore_provider_effects_before_completion(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            operation = _restore_operation()
            store.write_odoo_prod_backup_restore_operation_record(operation)

            def execute_restore(
                *,
                phase_checkpoint: Callable[[str, dict[str, str]], None],
                provider_effect_checkpoint: Callable[[str, str], None],
                **_: object,
            ) -> OdooProdBackupRestoreResult:
                phase_checkpoint("validated", {"plan_fingerprint": "fingerprint"})
                provider_effect_checkpoint(
                    "database_restore_started",
                    "database_restore_schedule_trigger",
                )
                phase_checkpoint(
                    "database_restored",
                    {"new_db_volume": "cm_prod_db_restore"},
                )
                return OdooProdBackupRestoreResult(
                    product="odoo-tenant-cm",
                    context="cm",
                    instance="prod",
                    backup_record_id="backup-20260614",
                    verification_record_id="verification-backup-20260614",
                    plan_fingerprint=operation.plan.plan_fingerprint,
                    restore_status="pass",
                    old_db_volume="cm_prod_db_corrupt",
                    new_db_volume="cm_prod_db_restore",
                    data_volume="cm_prod_data",
                    log_volume="cm_prod_logs",
                    database_dump_sha256="a" * 64,
                    filestore_archive_sha256="b" * 64,
                )

            with patch(
                "control_plane.workflows.odoo_stable_operation_worker.execute_odoo_prod_backup_restore_apply",
                side_effect=execute_restore,
            ):
                worker_result = run_odoo_stable_operation_worker_once(
                    record_store=store,
                    control_plane_root_path=Path(temporary_directory_name),
                    lease_owner="worker-a",
                    lease_seconds=60,
                    heartbeat_seconds=30,
                )

            stored = store.read_odoo_prod_backup_restore_operation_record(operation.operation_id)
            self.assertEqual(worker_result.operation_kind, "odoo_prod_backup_restore")
            self.assertEqual(stored.status, "pass")
            self.assertEqual(stored.phase, "completed")
            self.assertEqual(
                [checkpoint.phase for checkpoint in stored.checkpoints],
                ["validated", "database_restore_started", "database_restored"],
            )
            self.assertEqual(
                stored.checkpoints[1].evidence["provider_effect"],
                "database_restore_schedule_trigger",
            )

    def test_worker_replays_restore_verification_without_full_apply(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            base_operation = _restore_operation()
            failed_result = OdooProdBackupRestoreResult(
                product=base_operation.product,
                context=base_operation.context,
                instance=base_operation.instance,
                backup_record_id=base_operation.plan.backup_record_id,
                verification_record_id=base_operation.plan.verification_record_id,
                plan_fingerprint=base_operation.plan.plan_fingerprint,
                deployment_record_id="deployment-cm-prod-restore",
                restore_status="fail",
                database_restore_status="pass",
                filestore_stage_status="pass",
                web_quiesce_status="pass",
                filestore_activation_status="pass",
                deployment_status="pass",
                post_deploy_status="pass",
                health_status="pass",
                canonical_status="fail",
                logo_status="pass",
                runtime_identity_status="skipped",
                old_db_volume=base_operation.plan.old_db_volume,
                new_db_volume=base_operation.plan.new_db_volume,
                data_volume=base_operation.plan.data_volume,
                log_volume=base_operation.plan.log_volume,
                filestore_quarantine_path=base_operation.plan.filestore_quarantine_path,
                database_dump_sha256=base_operation.plan.database_dump_sha256,
                filestore_archive_sha256=base_operation.plan.filestore_archive_sha256,
                phase_evidence={
                    "database_restore": {"status": "pass"},
                    "filestore_stage": {"status": "pass"},
                    "web_quiesce": {"status": "pass"},
                    "filestore_activate": {"status": "pass"},
                    "target_env_update": {
                        "old_db_volume": base_operation.plan.old_db_volume,
                        "new_db_volume": base_operation.plan.new_db_volume,
                        "data_volume": base_operation.plan.data_volume,
                        "log_volume": base_operation.plan.log_volume,
                    },
                    "deployment": {"provider_deployment": "provider-deployment-1"},
                    "post_deploy": {"status": "pass"},
                },
                error_message="Odoo canonical verification failed.",
            )
            terminal_operation = base_operation.model_copy(
                update={
                    "status": "fail",
                    "phase": "failed",
                    "checkpoints": tuple(
                        OdooProdBackupRestoreCheckpoint.model_validate(
                            {
                                "phase": phase,
                                "recorded_at": f"2026-07-25T00:{index:02d}:00Z",
                                "evidence": {"status": "pass"},
                            }
                        )
                        for index, phase in enumerate(
                            (
                                "validated",
                                "database_restored",
                                "filestore_staged",
                                "web_quiesced",
                                "filestore_activated",
                                "target_env_updated",
                                "deployed",
                                "post_deploy_completed",
                                "verification_started",
                            ),
                            start=1,
                        )
                    ),
                    "deployment_record_id": failed_result.deployment_record_id,
                    "started_at": "2026-07-25T00:01:00Z",
                    "finished_at": "2026-07-25T00:30:00Z",
                    "updated_at": "2026-07-25T00:30:00Z",
                    "attempt": 1,
                    "result": failed_result,
                    "error_message": failed_result.error_message,
                }
            )
            store.write_odoo_prod_backup_restore_operation_record(terminal_operation)
            requeued = store.requeue_terminal_failed_odoo_prod_backup_restore_operation_record(
                operation_id=terminal_operation.operation_id,
                queued_at="2026-07-25T00:31:00Z",
                authorization=base_operation.authorization,
            )
            assert requeued is not None

            def replay_restore(
                *,
                operation: OdooProdBackupRestoreOperationRecord,
                phase_checkpoint: Callable[[str, dict[str, str]], None],
                provider_effect_checkpoint: Callable[[str, str], None],
                **_: object,
            ) -> OdooProdBackupRestoreResult:
                self.assertEqual(operation.attempt, 2)
                phase_checkpoint("post_deploy_started", {"replay": "verification_only"})
                provider_effect_checkpoint("post_deploy_started", "post_deploy_schedule_trigger")
                phase_checkpoint("post_deploy_completed", {"status": "pass"})
                phase_checkpoint("verification_started", {"replay": "verification_only"})
                return failed_result.model_copy(
                    update={
                        "restore_status": "pass",
                        "canonical_status": "pass",
                        "runtime_identity_status": "match",
                        "error_message": "",
                    }
                )

            with (
                patch(
                    "control_plane.workflows.odoo_stable_operation_worker.execute_odoo_prod_backup_restore_apply",
                    side_effect=AssertionError("verification replay must not run full apply"),
                ),
                patch(
                    "control_plane.workflows.odoo_stable_operation_worker.execute_odoo_prod_backup_restore_verification_replay",
                    side_effect=replay_restore,
                ),
            ):
                worker_result = run_odoo_stable_operation_worker_once(
                    record_store=store,
                    control_plane_root_path=Path(temporary_directory_name),
                    lease_owner="worker-a",
                    lease_seconds=60,
                    heartbeat_seconds=30,
                )

            stored = store.read_odoo_prod_backup_restore_operation_record(
                terminal_operation.operation_id
            )
            self.assertEqual(worker_result.operation_kind, "odoo_prod_backup_restore")
            self.assertEqual(stored.status, "pass")
            self.assertEqual(stored.phase, "completed")
            self.assertEqual(stored.attempt, 2)
            self.assertEqual(stored.deployment_record_id, failed_result.deployment_record_id)
            self.assertEqual(
                [checkpoint.phase for checkpoint in stored.checkpoints[-3:]],
                ["post_deploy_started", "post_deploy_completed", "verification_started"],
            )

    def test_worker_never_runs_full_restore_when_prior_result_exists(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            base_operation = _restore_operation()
            failed_result = OdooProdBackupRestoreResult(
                product=base_operation.product,
                context=base_operation.context,
                instance=base_operation.instance,
                backup_record_id=base_operation.plan.backup_record_id,
                verification_record_id=base_operation.plan.verification_record_id,
                plan_fingerprint=base_operation.plan.plan_fingerprint,
                deployment_record_id="deployment-cm-prod-restore",
                restore_status="fail",
                old_db_volume=base_operation.plan.old_db_volume,
                new_db_volume=base_operation.plan.new_db_volume,
                data_volume=base_operation.plan.data_volume,
                log_volume=base_operation.plan.log_volume,
                database_dump_sha256=base_operation.plan.database_dump_sha256,
                filestore_archive_sha256=base_operation.plan.filestore_archive_sha256,
                error_message="Prior restore result exists.",
            )
            pending_operation = base_operation.model_copy(
                update={
                    "status": "pending",
                    "phase": "verification_started",
                    "checkpoints": (
                        OdooProdBackupRestoreCheckpoint(
                            phase="verification_started",
                            recorded_at="2026-07-25T00:26:00Z",
                            evidence={"replay": "verification_only"},
                        ),
                    ),
                    "deployment_record_id": failed_result.deployment_record_id,
                    "started_at": "2026-07-25T00:01:00Z",
                    "updated_at": "2026-07-25T00:31:00Z",
                    "attempt": 2,
                    "result": failed_result,
                }
            )
            store.write_odoo_prod_backup_restore_operation_record(pending_operation)

            with (
                patch(
                    "control_plane.workflows.odoo_stable_operation_worker.execute_odoo_prod_backup_restore_apply",
                    side_effect=AssertionError("prior result must never run full restore"),
                ),
                patch(
                    "control_plane.workflows.odoo_stable_operation_worker.execute_odoo_prod_backup_restore_verification_replay",
                    side_effect=AssertionError("ineligible prior result must not replay"),
                ),
            ):
                worker_result = run_odoo_stable_operation_worker_once(
                    record_store=store,
                    control_plane_root_path=Path(temporary_directory_name),
                    lease_owner="worker-a",
                    lease_seconds=60,
                    heartbeat_seconds=30,
                )

            stored = store.read_odoo_prod_backup_restore_operation_record(
                pending_operation.operation_id
            )
            self.assertEqual(worker_result.operation_kind, "odoo_prod_backup_restore")
            self.assertEqual(stored.status, "fail")
            self.assertEqual(stored.attempt, 3)
            self.assertEqual(
                stored.error_message,
                "Odoo production backup restore with prior result evidence requires operator "
                "reconciliation.",
            )

    def test_restore_lease_expiry_after_provider_effect_is_never_retried(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            operation = _restore_operation().model_copy(
                update={
                    "status": "running",
                    "phase": "database_restore_started",
                    "started_at": "2026-07-25T00:01:00Z",
                    "updated_at": "2026-07-25T00:01:00Z",
                    "lease_owner": "worker-a",
                    "lease_expires_at": "2026-07-25T00:02:00Z",
                    "heartbeat_at": "2026-07-25T00:01:00Z",
                    "attempt": 1,
                    "checkpoints": (
                        OdooProdBackupRestoreCheckpoint(
                            phase="database_restore_started",
                            recorded_at="2026-07-25T00:01:00Z",
                            evidence={"provider_effect": "database_restore_schedule_trigger"},
                        ),
                    ),
                }
            )
            store.write_odoo_prod_backup_restore_operation_record(operation)

            reconcile_result = reconcile_stale_odoo_stable_operation_records(
                record_store=store,
                now="2026-07-25T00:03:00Z",
            )

            stored = store.read_odoo_prod_backup_restore_operation_record(operation.operation_id)
            self.assertEqual(
                reconcile_result.reconciled_restore_ids,
                (operation.operation_id,),
            )
            self.assertEqual(stored.status, "reconciliation_required")
            self.assertEqual(stored.phase, "database_restore_started")
            self.assertEqual(stored.error_code, "operation_reconciliation_required")
            self.assertIn("operator reconciliation", stored.error_message)

    def test_worker_fails_closed_when_grant_is_removed_after_enqueue(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            envelope = OdooStableBootstrapEnvelope.model_validate(
                {
                    "schema_version": 1,
                    "product": "odoo-tenant-cm",
                    "bootstrap": _bootstrap_payload()["request"],
                }
            )
            authorization = OdooStableBootstrapOperationRecord.model_validate(
                _bootstrap_payload()
            ).authorization
            assert authorization is not None
            _, payload = enqueue_odoo_stable_bootstrap_operation(
                record_store=store,
                request=envelope,
                idempotency_key="bootstrap-cm-testing",
                request_fingerprint="fingerprint-123",
                created_at="2026-07-23T03:34:00Z",
                authorization=authorization,
            )
            operation_id = str(payload["operation_id"])
            revoked_policy = durable_operation_policy_record(revision=43)

            with (
                patch(
                    "control_plane.workflows.odoo_stable_operation_worker.read_active_authz_policy_record",
                    return_value=revoked_policy,
                ),
                patch(
                    "control_plane.workflows.odoo_stable_operation_worker.execute_odoo_stable_bootstrap"
                ) as execute_mock,
            ):
                worker_result = run_odoo_stable_operation_worker_once(
                    record_store=store,
                    control_plane_root_path=root,
                    lease_owner="worker-a",
                    lease_seconds=300,
                    heartbeat_seconds=60,
                )

            operation = store.read_odoo_stable_bootstrap_operation_record(operation_id)
            self.assertEqual(worker_result.status, "worked")
            self.assertEqual(operation.status, "fail")
            self.assertEqual(operation.error_code, "operation_authorization_revoked")
            execute_mock.assert_not_called()

    def test_worker_reauthorizes_at_first_provider_effect(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_odoo_stable_bootstrap_operation_record(
                OdooStableBootstrapOperationRecord.model_validate(_bootstrap_payload())
            )
            revoked_policy = durable_operation_policy_record(revision=43)
            provider_call_count = 0

            def execute_with_provider_checkpoint(
                **kwargs: object,
            ) -> OdooStableBootstrapResult:
                nonlocal provider_call_count
                checkpoint = kwargs["provider_effect_checkpoint"]
                assert callable(checkpoint)
                try:
                    checkpoint("stable_bootstrap_schedule")
                except ClickException as error:
                    return OdooStableBootstrapResult(
                        product="odoo-tenant-cm",
                        context="cm",
                        instance="testing",
                        deployment_record_id="deployment-cm-testing",
                        bootstrap_status="fail",
                        bootstrap_run_status="fail",
                        readiness_status="fail",
                        error_message=str(error),
                    )
                provider_call_count += 1
                raise AssertionError("Revoked authorization reached the provider effect.")

            with (
                patch(
                    "control_plane.workflows.odoo_stable_operation_worker.read_active_authz_policy_record",
                    side_effect=(self.authorization_policy_record, revoked_policy),
                ),
                patch(
                    "control_plane.workflows.odoo_stable_operation_worker.execute_odoo_stable_bootstrap",
                    side_effect=execute_with_provider_checkpoint,
                ) as execute_mock,
            ):
                worker_result = run_odoo_stable_operation_worker_once(
                    record_store=store,
                    control_plane_root_path=root,
                    lease_owner="worker-a",
                    lease_seconds=300,
                    heartbeat_seconds=60,
                )

            operation = store.read_odoo_stable_bootstrap_operation_record("operation-cm-testing")
            self.assertEqual(worker_result.status, "worked")
            self.assertEqual(operation.status, "fail")
            self.assertEqual(operation.error_code, "operation_authorization_revoked")
            self.assertEqual(provider_call_count, 0)
            execute_mock.assert_called_once()

    def test_worker_fails_closed_for_legacy_operation_without_provenance(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            legacy_payload = _bootstrap_payload()
            legacy_payload["schema_version"] = 1
            legacy_payload.pop("authorization")
            store.write_odoo_stable_bootstrap_operation_record(
                OdooStableBootstrapOperationRecord.model_validate(legacy_payload)
            )

            with patch(
                "control_plane.workflows.odoo_stable_operation_worker.execute_odoo_stable_bootstrap"
            ) as execute_mock:
                worker_result = run_odoo_stable_operation_worker_once(
                    record_store=store,
                    control_plane_root_path=root,
                    lease_owner="worker-a",
                    lease_seconds=300,
                    heartbeat_seconds=60,
                )

            operation = store.read_odoo_stable_bootstrap_operation_record("operation-cm-testing")
            self.assertEqual(worker_result.status, "worked")
            self.assertEqual(operation.status, "fail")
            self.assertEqual(
                operation.error_code,
                "operation_authorization_provenance_missing",
            )
            execute_mock.assert_not_called()

    def test_worker_executes_bootstrap_and_writes_terminal_result(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_odoo_stable_bootstrap_operation_record(
                OdooStableBootstrapOperationRecord.model_validate(_bootstrap_payload())
            )
            result = OdooStableBootstrapResult(
                product="odoo-tenant-cm",
                context="cm",
                instance="testing",
                deployment_record_id="deployment-cm-testing",
                bootstrap_status="pass",
                bootstrap_run_status="pass",
                readiness_status="pass",
                post_deploy_status="pass",
                health_status="pass",
                canonical_status="pass",
                logo_status="pass",
            )

            with patch(
                "control_plane.workflows.odoo_stable_operation_worker.execute_odoo_stable_bootstrap",
                return_value=result,
            ) as execute_mock:
                worker_result = run_odoo_stable_operation_worker_once(
                    record_store=store,
                    control_plane_root_path=root,
                    lease_owner="worker-a",
                    lease_seconds=300,
                    heartbeat_seconds=60,
                )

            operation = store.read_odoo_stable_bootstrap_operation_record("operation-cm-testing")
            self.assertEqual(worker_result.status, "worked")
            self.assertEqual(worker_result.operation_kind, "odoo_stable_bootstrap")
            self.assertTrue(worker_result.terminal_write_committed)
            self.assertEqual(operation.status, "pass")
            self.assertEqual(operation.phase, "completed")
            self.assertEqual(operation.deployment_record_id, "deployment-cm-testing")
            self.assertEqual(operation.lease_owner, "worker-a")
            execute_mock.assert_called_once()

    def test_worker_executes_target_replacement_and_writes_terminal_result(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_odoo_stable_target_replacement_operation_record(
                OdooStableTargetReplacementOperationRecord.model_validate(_replacement_payload())
            )
            result = OdooStableTargetReplacementApplyResult(
                product="odoo-tenant-cm",
                context="cm",
                instance="testing",
                strategy="recreate-in-place",
                deployment_record_id="deployment-cm-testing",
                deploy_status="pass",
                post_deploy_status="pass",
                health_status="pass",
                canonical_status="pass",
                logo_status="pass",
                runtime_identity_injected=True,
            )

            with patch(
                "control_plane.workflows.odoo_stable_operation_worker.execute_odoo_stable_target_replacement_apply",
                return_value=result,
            ) as execute_mock:
                worker_result = run_odoo_stable_operation_worker_once(
                    record_store=store,
                    control_plane_root_path=root,
                    lease_owner="worker-a",
                    lease_seconds=300,
                    heartbeat_seconds=60,
                )

            operation = store.read_odoo_stable_target_replacement_operation_record(
                "operation-cm-testing"
            )
            self.assertEqual(worker_result.status, "worked")
            self.assertEqual(worker_result.operation_kind, "odoo_stable_target_replacement")
            self.assertTrue(worker_result.terminal_write_committed)
            self.assertEqual(operation.status, "pass")
            self.assertEqual(operation.phase, "completed")
            self.assertEqual(operation.deployment_record_id, "deployment-cm-testing")
            self.assertEqual(operation.lease_owner, "worker-a")
            execute_mock.assert_called_once()

    def test_worker_writes_failure_when_execution_raises(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_odoo_stable_bootstrap_operation_record(
                OdooStableBootstrapOperationRecord.model_validate(_bootstrap_payload())
            )

            with (
                patch(
                    "control_plane.workflows.odoo_stable_operation_worker.execute_odoo_stable_bootstrap",
                    side_effect=RuntimeError("provider unavailable"),
                ),
                self.assertLogs(level=logging.ERROR),
            ):
                worker_result = run_odoo_stable_operation_worker_once(
                    record_store=store,
                    control_plane_root_path=root,
                    lease_owner="worker-a",
                    lease_seconds=300,
                    heartbeat_seconds=60,
                )

            operation = store.read_odoo_stable_bootstrap_operation_record("operation-cm-testing")
            self.assertEqual(worker_result.status, "worked")
            self.assertTrue(worker_result.terminal_write_committed)
            self.assertEqual(operation.status, "fail")
            self.assertEqual(operation.phase, "failed")
            self.assertEqual(operation.error_message, "provider unavailable")

    def test_worker_writes_target_replacement_failure_when_execution_raises(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_odoo_stable_target_replacement_operation_record(
                OdooStableTargetReplacementOperationRecord.model_validate(_replacement_payload())
            )

            with (
                patch(
                    "control_plane.workflows.odoo_stable_operation_worker.execute_odoo_stable_target_replacement_apply",
                    side_effect=RuntimeError("provider unavailable"),
                ),
                self.assertLogs(level=logging.ERROR),
            ):
                worker_result = run_odoo_stable_operation_worker_once(
                    record_store=store,
                    control_plane_root_path=root,
                    lease_owner="worker-a",
                    lease_seconds=300,
                    heartbeat_seconds=60,
                )

            operation = store.read_odoo_stable_target_replacement_operation_record(
                "operation-cm-testing"
            )
            self.assertEqual(worker_result.status, "worked")
            self.assertTrue(worker_result.terminal_write_committed)
            self.assertEqual(operation.status, "fail")
            self.assertEqual(operation.phase, "failed")
            self.assertEqual(operation.error_message, "provider unavailable")

    def test_worker_recovers_expired_safe_operation_before_claiming(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_odoo_stable_bootstrap_operation_record(
                OdooStableBootstrapOperationRecord.model_validate(
                    {
                        **_bootstrap_payload(),
                        "status": "running",
                        "phase": "created",
                        "started_at": "2026-05-17T00:01:00Z",
                        "lease_owner": "old-worker",
                        "lease_expires_at": "2000-01-01T00:00:00Z",
                        "heartbeat_at": "2000-01-01T00:00:00Z",
                        "attempt": 1,
                    }
                )
            )
            result = OdooStableBootstrapResult(
                product="odoo-tenant-cm",
                context="cm",
                instance="testing",
                deployment_record_id="deployment-cm-testing",
                bootstrap_status="pass",
                bootstrap_run_status="pass",
                readiness_status="pass",
                post_deploy_status="pass",
                health_status="pass",
                canonical_status="pass",
                logo_status="pass",
            )

            with patch(
                "control_plane.workflows.odoo_stable_operation_worker.execute_odoo_stable_bootstrap",
                return_value=result,
            ):
                worker_result = run_odoo_stable_operation_worker_once(
                    record_store=store,
                    control_plane_root_path=root,
                    lease_owner="worker-a",
                    lease_seconds=300,
                    heartbeat_seconds=60,
                )

            self.assertEqual(worker_result.status, "worked")
            self.assertEqual(worker_result.recovered_operation_ids, ("operation-cm-testing",))
            operation = store.read_odoo_stable_bootstrap_operation_record("operation-cm-testing")
            self.assertEqual(operation.status, "pass")
            self.assertEqual(operation.attempt, 2)

    def test_worker_returns_idle_when_no_operation_exists(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")

            worker_result = run_odoo_stable_operation_worker_once(
                record_store=store,
                control_plane_root_path=root,
                lease_owner="worker-a",
            )

            self.assertEqual(worker_result.status, "idle")
            self.assertEqual(worker_result.operation_kind, "")

    def test_reconcile_recovers_expired_leases_without_claiming_work(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_odoo_stable_bootstrap_operation_record(
                OdooStableBootstrapOperationRecord.model_validate(
                    {
                        **_bootstrap_payload("bootstrap-expired-safe"),
                        "status": "running",
                        "phase": "created",
                        "started_at": "2026-05-17T00:01:00Z",
                        "lease_owner": "old-worker",
                        "lease_expires_at": "2026-05-17T00:02:00Z",
                        "heartbeat_at": "2026-05-17T00:01:00Z",
                        "attempt": 1,
                    }
                )
            )
            store.write_odoo_stable_bootstrap_operation_record(
                OdooStableBootstrapOperationRecord.model_validate(
                    _bootstrap_payload("bootstrap-pending")
                )
            )
            store.write_odoo_stable_target_replacement_operation_record(
                OdooStableTargetReplacementOperationRecord.model_validate(
                    {
                        **_replacement_payload("replacement-expired-unsafe"),
                        "status": "running",
                        "phase": "apply",
                        "started_at": "2026-05-17T00:01:00Z",
                        "lease_owner": "old-worker",
                        "lease_expires_at": "2026-05-17T00:02:00Z",
                        "heartbeat_at": "2026-05-17T00:01:00Z",
                        "attempt": 1,
                    }
                )
            )

            result = reconcile_stale_odoo_stable_operation_records(
                record_store=store,
                now="2026-05-17T00:03:00Z",
            )

            self.assertEqual(result.reconciled_bootstrap_ids, ("bootstrap-expired-safe",))
            self.assertEqual(
                result.reconciled_replacement_ids,
                ("replacement-expired-unsafe",),
            )
            recovered = store.read_odoo_stable_bootstrap_operation_record("bootstrap-expired-safe")
            pending = store.read_odoo_stable_bootstrap_operation_record("bootstrap-pending")
            reconciliation_required = store.read_odoo_stable_target_replacement_operation_record(
                "replacement-expired-unsafe"
            )
            self.assertEqual(recovered.status, "pending")
            self.assertEqual(recovered.phase, "created")
            self.assertEqual(recovered.lease_owner, "")
            self.assertEqual(recovered.started_at, "")
            self.assertEqual(pending.status, "pending")
            self.assertEqual(pending.lease_owner, "")
            self.assertEqual(reconciliation_required.status, "reconciliation_required")
            self.assertEqual(reconciliation_required.phase, "apply")
            self.assertEqual(
                reconciliation_required.error_code,
                "operation_reconciliation_required",
            )
            self.assertIn("operator reconciliation", reconciliation_required.error_message)
            self.assertEqual(reconciliation_required.lease_owner, "")

    def test_reconcile_rejects_invalid_max_attempts(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")

            with self.assertRaisesRegex(ValueError, "max_attempts must be positive"):
                reconcile_stale_odoo_stable_operation_records(
                    record_store=store,
                    max_attempts=0,
                )

    def test_worker_status_reports_pending_running_stalled_and_terminal_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_odoo_stable_bootstrap_operation_record(
                OdooStableBootstrapOperationRecord.model_validate(
                    _bootstrap_payload("bootstrap-pending")
                )
            )
            store.write_odoo_stable_bootstrap_operation_record(
                OdooStableBootstrapOperationRecord.model_validate(
                    {
                        **_bootstrap_payload("bootstrap-running"),
                        "status": "running",
                        "phase": "running",
                        "lease_owner": "worker-a",
                        "lease_expires_at": "2026-05-17T00:10:00Z",
                        "heartbeat_at": "2026-05-17T00:04:00Z",
                        "attempt": 1,
                    }
                )
            )
            store.write_odoo_stable_target_replacement_operation_record(
                OdooStableTargetReplacementOperationRecord.model_validate(
                    {
                        **_replacement_payload("replacement-reconciliation"),
                        "status": "reconciliation_required",
                        "phase": "apply",
                        "error_code": "operation_reconciliation_required",
                        "error_message": "Provider state requires operator reconciliation.",
                    }
                )
            )
            store.write_odoo_stable_target_replacement_operation_record(
                OdooStableTargetReplacementOperationRecord.model_validate(
                    {
                        **_replacement_payload("replacement-stalled"),
                        "status": "running",
                        "phase": "running",
                        "lease_owner": "worker-b",
                        "lease_expires_at": "2026-05-17T00:03:00Z",
                        "heartbeat_at": "2026-05-17T00:01:00Z",
                        "attempt": 2,
                    }
                )
            )
            store.write_odoo_stable_target_replacement_operation_record(
                OdooStableTargetReplacementOperationRecord.model_validate(
                    {
                        **_replacement_payload("replacement-blank-lease"),
                        "status": "running",
                        "phase": "running",
                        "lease_owner": "worker-c",
                        "heartbeat_at": "2026-05-17T00:02:00Z",
                        "attempt": 1,
                    }
                )
            )
            store.write_odoo_stable_target_replacement_operation_record(
                OdooStableTargetReplacementOperationRecord.model_validate(
                    {
                        **_replacement_payload("replacement-pass"),
                        "status": "pass",
                        "phase": "completed",
                        "finished_at": "2026-05-17T00:02:00Z",
                    }
                )
            )

            status = build_odoo_stable_operation_worker_status(
                record_store=store,
                now="2026-05-17T00:05:00Z",
            )

            self.assertEqual(status.status, "stalled")
            self.assertEqual(status.pending_count, 1)
            self.assertEqual(status.running_count, 3)
            self.assertEqual(status.stalled_count, 3)
            self.assertEqual(status.terminal_count, 1)
            self.assertEqual(status.counts_by_kind_status["odoo_stable_bootstrap:pending"], 1)
            self.assertEqual(status.counts_by_kind_status["odoo_stable_bootstrap:running"], 1)
            self.assertEqual(
                status.counts_by_kind_status["odoo_stable_target_replacement:running"],
                2,
            )
            self.assertEqual(
                status.counts_by_kind_status["odoo_stable_target_replacement:pass"],
                1,
            )
            self.assertEqual(
                status.counts_by_kind_status[
                    "odoo_stable_target_replacement:reconciliation_required"
                ],
                1,
            )
            stalled_operation = next(
                operation
                for operation in status.operations
                if operation.operation_id == "replacement-stalled"
            )
            self.assertTrue(stalled_operation.lease_expired)
            self.assertEqual(stalled_operation.heartbeat_age_seconds, 240)
            blank_lease_operation = next(
                operation
                for operation in status.operations
                if operation.operation_id == "replacement-blank-lease"
            )
            self.assertTrue(blank_lease_operation.lease_expired)

    def test_worker_loop_processes_until_idle_then_stops(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_odoo_stable_bootstrap_operation_record(
                OdooStableBootstrapOperationRecord.model_validate(_bootstrap_payload())
            )
            result = OdooStableBootstrapResult(
                product="odoo-tenant-cm",
                context="cm",
                instance="testing",
                deployment_record_id="deployment-cm-testing",
                bootstrap_status="pass",
                bootstrap_run_status="pass",
                readiness_status="pass",
                post_deploy_status="pass",
                health_status="pass",
                canonical_status="pass",
                logo_status="pass",
            )
            stop_event = Event()
            iteration_statuses: list[str] = []

            def _record_iteration(worker_result: object) -> None:
                status = getattr(worker_result, "status")
                iteration_statuses.append(status)
                if status == "idle":
                    stop_event.set()

            with patch(
                "control_plane.workflows.odoo_stable_operation_worker.execute_odoo_stable_bootstrap",
                return_value=result,
            ):
                loop_result = run_odoo_stable_operation_worker_loop(
                    record_store=store,
                    control_plane_root_path=root,
                    lease_owner="worker-a",
                    lease_seconds=300,
                    heartbeat_seconds=60,
                    poll_seconds=1,
                    stop_event=stop_event,
                    iteration_callback=_record_iteration,
                )

            self.assertEqual(loop_result.status, "stopped")
            self.assertEqual(loop_result.iterations, 2)
            self.assertEqual(loop_result.worked_count, 1)
            self.assertEqual(loop_result.idle_count, 1)
            self.assertEqual(iteration_statuses, ["worked", "idle"])

    def test_worker_loop_raises_after_consecutive_errors(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")

            with (
                patch(
                    "control_plane.workflows.odoo_stable_operation_worker.run_odoo_stable_operation_worker_once",
                    side_effect=RuntimeError("boom"),
                ),
                self.assertLogs(level=logging.ERROR),
                self.assertRaises(RuntimeError),
            ):
                run_odoo_stable_operation_worker_loop(
                    record_store=store,
                    control_plane_root_path=root,
                    lease_owner="worker-a",
                    error_backoff_seconds=1,
                    max_consecutive_errors=2,
                    max_iterations=3,
                )

    def test_cli_status_outputs_worker_status_json(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_odoo_stable_bootstrap_operation_record(
                OdooStableBootstrapOperationRecord.model_validate(_bootstrap_payload())
            )

            with patch("control_plane.cli_service._store", return_value=store):
                result = CliRunner().invoke(
                    main,
                    [
                        "service",
                        "odoo-workers",
                        "status",
                        "--database-url",
                        "sqlite+pysqlite:///:memory:",
                    ],
                )

            self.assertEqual(result.exit_code, 0, result.output)
            payload = json.loads(result.output)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["pending_count"], 1)
            self.assertEqual(payload["running_count"], 0)
            self.assertEqual(payload["operations"][0]["operation_id"], "operation-cm-testing")
            self.assertNotIn("request", payload["operations"][0])

    def test_cli_reconcile_outputs_reconciled_ids_json(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_odoo_stable_bootstrap_operation_record(
                OdooStableBootstrapOperationRecord.model_validate(
                    {
                        **_bootstrap_payload(),
                        "status": "running",
                        "phase": "created",
                        "started_at": "2026-05-17T00:01:00Z",
                        "lease_owner": "old-worker",
                        "lease_expires_at": "2000-01-01T00:00:00Z",
                        "heartbeat_at": "2000-01-01T00:00:00Z",
                        "attempt": 1,
                    }
                )
            )

            with patch("control_plane.cli_service._store", return_value=store):
                result = CliRunner().invoke(
                    main,
                    [
                        "service",
                        "odoo-workers",
                        "reconcile",
                        "--database-url",
                        "sqlite+pysqlite:///:memory:",
                    ],
                )

            self.assertEqual(result.exit_code, 0, result.output)
            payload = json.loads(result.output)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["reconciled_count"], 1)
            self.assertEqual(
                payload["reconciled_bootstrap_ids"],
                ["operation-cm-testing"],
            )
            self.assertEqual(payload["reconciled_replacement_ids"], [])

    def test_cli_run_once_outputs_worker_result_json(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")

            with (
                patch("control_plane.cli_service._store", return_value=store),
                patch(
                    "control_plane.cli_service._control_plane_root",
                    return_value=root,
                ),
            ):
                result = CliRunner().invoke(
                    main,
                    [
                        "service",
                        "odoo-workers",
                        "run-once",
                        "--database-url",
                        "sqlite+pysqlite:///:memory:",
                        "--lease-owner",
                        "worker-a",
                    ],
                )

            self.assertEqual(result.exit_code, 0, result.output)
            payload = json.loads(result.output)
            self.assertEqual(payload["status"], "idle")
            self.assertEqual(payload["operation_kind"], "")

    def test_cli_run_delegates_to_worker_loop(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            loop_result = OdooStableOperationWorkerLoopResult(
                status="completed",
                iterations=1,
                worked_count=0,
                idle_count=1,
                error_count=0,
            )

            with (
                patch("control_plane.cli_service._store", return_value=store),
                patch(
                    "control_plane.cli_service._control_plane_root",
                    return_value=root,
                ),
                patch(
                    "control_plane.cli_service.run_odoo_stable_operation_worker_loop",
                    return_value=loop_result,
                ) as loop_mock,
            ):
                result = CliRunner().invoke(
                    main,
                    [
                        "service",
                        "odoo-workers",
                        "run",
                        "--database-url",
                        "sqlite+pysqlite:///:memory:",
                        "--lease-owner",
                        "worker-a",
                        "--poll-seconds",
                        "1",
                    ],
                )

            self.assertEqual(result.exit_code, 0, result.output)
            payload = json.loads(result.output)
            self.assertEqual(payload["status"], "completed")
            loop_mock.assert_called_once()
            self.assertEqual(loop_mock.call_args.kwargs["record_store"], store)
            self.assertEqual(loop_mock.call_args.kwargs["lease_owner"], "worker-a")
            self.assertEqual(loop_mock.call_args.kwargs["poll_seconds"], 1)


if __name__ == "__main__":
    unittest.main()
