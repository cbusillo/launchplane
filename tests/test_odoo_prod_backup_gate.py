import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import click
from pydantic import ValidationError

from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.workflows.odoo_prod_backup_gate import (
    OdooProdBackupGateRequest,
    OdooProdBackupVerificationRequest,
    execute_odoo_prod_backup_gate,
    execute_odoo_prod_backup_verification,
)


def _target_record() -> DokployTargetRecord:
    return DokployTargetRecord(
        context="cm",
        instance="prod",
        target_type="compose",
        target_name="cm-prod",
        deploy_timeout_seconds=900,
        healthcheck_path="/web/health",
        healthcheck_timeout_seconds=180,
        domains=("cm-prod.shinycomputers.com",),
        env={},
        updated_at="2026-04-27T00:00:00Z",
    )


def _target_id_record() -> DokployTargetIdRecord:
    return DokployTargetIdRecord(
        context="cm",
        instance="prod",
        target_id="cm-prod-compose-id",
        updated_at="2026-04-27T00:00:00Z",
    )


def _runtime_values() -> dict[str, str]:
    return {
        "ODOO_DB_NAME": "cm",
        "ODOO_FILESTORE_PATH": "/volumes/data/filestore",
        "ODOO_BACKUP_ROOT": "/volumes/data/backups/launchplane",
    }


def _backup_evidence() -> dict[str, str]:
    backup_dir = "/volumes/data/backups/launchplane/cm/backup-gate-cm-prod-1"
    return {
        "database_name": "cm",
        "filestore_path": "/volumes/data/filestore",
        "backup_root": "/volumes/data/backups/launchplane",
        "backup_dir": backup_dir,
        "database_dump_path": f"{backup_dir}/cm.dump",
        "filestore_archive_path": f"{backup_dir}/cm-filestore.tar.gz",
        "manifest_path": f"{backup_dir}/manifest.json",
    }


def _passing_backup_record() -> BackupGateRecord:
    return BackupGateRecord(
        record_id="backup-gate-cm-prod-1",
        context="cm",
        instance="prod",
        created_at="2026-07-25T00:00:00Z",
        source="launchplane-odoo-prod-backup-gate",
        required=True,
        status="pass",
        evidence=_backup_evidence(),
    )


def _passing_verification_evidence() -> dict[str, object]:
    return {
        "schema_version": 1,
        "verification_nonce": "c" * 64,
        "backup_record_id": "backup-gate-cm-prod-1",
        "database_name": "cm",
        "verification_status": "pass",
        "manifest_status": "pass",
        "sha256_status": "pass",
        "pg_restore_status": "pass",
        "tar_status": "pass",
        "staging_space_status": "pass",
        "database_dump_sha256": "a" * 64,
        "filestore_archive_sha256": "b" * 64,
        "database_dump_size": 4096,
        "filestore_archive_size": 8192,
        "pg_restore_entry_count": 42,
        "filestore_member_count": 128,
        "filestore_unpacked_size": 16384,
        "data_volume_free_bytes": 32768,
        "staging_required_bytes": 16384,
        "failure_code": "",
    }


class OdooProdBackupGateWorkflowTests(unittest.TestCase):
    def test_backup_gate_request_accepts_profile_owned_context(self) -> None:
        request = OdooProdBackupGateRequest(
            context=" New-Site ",
            backup_record_id="backup-gate-new-site-prod-1",
        )

        self.assertEqual(request.context, "new-site")

    def test_backup_gate_request_rejects_blank_context(self) -> None:
        with self.assertRaisesRegex(ValidationError, "requires context"):
            OdooProdBackupGateRequest(
                context=" ",
                backup_record_id="backup-gate-new-site-prod-1",
            )

    def _record_store(self) -> Mock:
        record_store = Mock()
        record_store.read_dokploy_target_record.return_value = _target_record()
        record_store.read_dokploy_target_id_record.return_value = _target_id_record()
        return record_store

    def test_backup_gate_captures_backup_and_writes_pass_record(self) -> None:
        record_store = self._record_store()

        with (
            patch(
                "control_plane.workflows.odoo_prod_backup_gate.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_gate.dokploy_post_deploy.run_compose_odoo_backup_gate"
            ) as run_backup_mock,
            patch(
                "control_plane.workflows.odoo_prod_backup_gate.control_plane_runtime_environments.resolve_runtime_environment_values",
                return_value=_runtime_values(),
            ),
        ):
            result = execute_odoo_prod_backup_gate(
                control_plane_root=Path("/control-plane"),
                record_store=record_store,
                request=OdooProdBackupGateRequest(
                    context="cm",
                    backup_record_id="backup-gate-cm-prod-1",
                ),
            )

        self.assertEqual(result.backup_status, "pass")
        self.assertEqual(result.backup_record_id, "backup-gate-cm-prod-1")
        self.assertEqual(
            result.database_dump_path,
            "/volumes/data/backups/launchplane/cm/backup-gate-cm-prod-1/cm.dump",
        )
        run_backup_mock.assert_called_once()
        self.assertEqual(run_backup_mock.call_args.kwargs["database_name"], "cm")
        self.assertEqual(
            run_backup_mock.call_args.kwargs["filestore_path"],
            "/volumes/data/filestore",
        )
        self.assertEqual(
            run_backup_mock.call_args.kwargs["backup_root"],
            "/volumes/data/backups/launchplane",
        )
        self.assertEqual(record_store.write_backup_gate_record.call_count, 2)
        pending_record = record_store.write_backup_gate_record.call_args_list[0].args[0]
        final_record = record_store.write_backup_gate_record.call_args_list[-1].args[0]
        self.assertIsInstance(final_record, BackupGateRecord)
        self.assertEqual(pending_record.status, "pending")
        self.assertEqual(final_record.status, "pass")
        self.assertEqual(final_record.source, "launchplane-odoo-prod-backup-gate")
        self.assertEqual(
            final_record.evidence["filestore_archive_path"],
            "/volumes/data/backups/launchplane/cm/backup-gate-cm-prod-1/cm-filestore.tar.gz",
        )

    def test_failed_backup_gate_writes_fail_record_without_evidence(self) -> None:
        record_store = self._record_store()

        with (
            patch(
                "control_plane.workflows.odoo_prod_backup_gate.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_gate.dokploy_post_deploy.run_compose_odoo_backup_gate",
                side_effect=click.ClickException("backup failed"),
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_gate.control_plane_runtime_environments.resolve_runtime_environment_values",
                return_value=_runtime_values(),
            ),
        ):
            result = execute_odoo_prod_backup_gate(
                control_plane_root=Path("/control-plane"),
                record_store=record_store,
                request=OdooProdBackupGateRequest(
                    context="cm",
                    backup_record_id="backup-gate-cm-prod-1",
                ),
            )

        self.assertEqual(result.backup_status, "fail")
        self.assertIn("backup failed", result.error_message)
        final_record = record_store.write_backup_gate_record.call_args_list[-1].args[0]
        self.assertEqual(final_record.status, "fail")
        self.assertEqual(final_record.evidence["error_message"], "backup failed")
        self.assertEqual(
            final_record.evidence["database_dump_path"],
            "/volumes/data/backups/launchplane/cm/backup-gate-cm-prod-1/cm.dump",
        )

    def test_backup_gate_requires_database_name_in_runtime_environment_records(self) -> None:
        record_store = self._record_store()

        with (
            patch(
                "control_plane.workflows.odoo_prod_backup_gate.control_plane_runtime_environments.resolve_runtime_environment_values",
                return_value={},
            ),
            self.assertRaises(click.ClickException),
        ):
            execute_odoo_prod_backup_gate(
                control_plane_root=Path("/control-plane"),
                record_store=record_store,
                request=OdooProdBackupGateRequest(
                    context="cm",
                    backup_record_id="backup-gate-cm-prod-1",
                ),
            )

        record_store.write_backup_gate_record.assert_not_called()

    def test_backup_gate_requires_backup_root_in_runtime_environment_records(self) -> None:
        record_store = self._record_store()
        runtime_values = _runtime_values()
        runtime_values.pop("ODOO_BACKUP_ROOT")

        with (
            patch(
                "control_plane.workflows.odoo_prod_backup_gate.control_plane_runtime_environments.resolve_runtime_environment_values",
                return_value=runtime_values,
            ),
            self.assertRaises(click.ClickException),
        ):
            execute_odoo_prod_backup_gate(
                control_plane_root=Path("/control-plane"),
                record_store=record_store,
                request=OdooProdBackupGateRequest(
                    context="cm",
                    backup_record_id="backup-gate-cm-prod-1",
                ),
            )

        record_store.write_backup_gate_record.assert_not_called()

    def test_backup_verification_reads_exact_gate_and_returns_redacted_evidence(self) -> None:
        record_store = self._record_store()
        record_store.read_backup_gate_record.return_value = _passing_backup_record()

        with (
            patch(
                "control_plane.workflows.odoo_prod_backup_gate.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_gate.dokploy_post_deploy.run_compose_odoo_backup_verification",
                return_value=_passing_verification_evidence(),
            ) as run_verification_mock,
            patch(
                "control_plane.workflows.odoo_prod_backup_gate.python_secrets.token_hex",
                return_value="c" * 64,
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_gate.control_plane_runtime_environments.resolve_runtime_environment_values",
                return_value=_runtime_values(),
            ),
        ):
            result = execute_odoo_prod_backup_verification(
                control_plane_root=Path("/control-plane"),
                record_store=record_store,
                request=OdooProdBackupVerificationRequest(
                    context="cm",
                    backup_record_id="backup-gate-cm-prod-1",
                ),
            )

        self.assertEqual(result.verification_status, "pass")
        self.assertEqual(result.database_dump_sha256, "a" * 64)
        self.assertEqual(result.pg_restore_entry_count, 42)
        self.assertTrue(result.verification_record_id.startswith("odoo-backup-verification-"))
        self.assertNotIn("backup_root", result.model_dump())
        self.assertNotIn("database_dump_path", result.model_dump())
        record_store.read_backup_gate_record.assert_called_once_with("backup-gate-cm-prod-1")
        self.assertEqual(
            run_verification_mock.call_args.kwargs["manifest_path"],
            "/volumes/data/backups/launchplane/cm/backup-gate-cm-prod-1/manifest.json",
        )
        self.assertEqual(run_verification_mock.call_args.kwargs["verification_nonce"], "c" * 64)
        verification_record = record_store.write_backup_gate_record.call_args.args[0]
        self.assertEqual(verification_record.record_id, result.verification_record_id)
        self.assertEqual(verification_record.status, "pass")
        self.assertEqual(verification_record.evidence["backup_record_id"], result.backup_record_id)
        self.assertEqual(verification_record.evidence["verification_nonce"], "c" * 64)
        self.assertEqual(
            verification_record.evidence["database_dump_sha256"],
            "a" * 64,
        )

    def test_backup_verification_rejects_provider_result_for_different_request(self) -> None:
        record_store = self._record_store()
        record_store.read_backup_gate_record.return_value = _passing_backup_record()
        mismatched_evidence = _passing_verification_evidence()
        mismatched_evidence["verification_nonce"] = "d" * 64

        with (
            patch(
                "control_plane.workflows.odoo_prod_backup_gate.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_gate.dokploy_post_deploy.run_compose_odoo_backup_verification",
                return_value=mismatched_evidence,
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_gate.python_secrets.token_hex",
                return_value="c" * 64,
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_gate.control_plane_runtime_environments.resolve_runtime_environment_values",
                return_value=_runtime_values(),
            ),
        ):
            result = execute_odoo_prod_backup_verification(
                control_plane_root=Path("/control-plane"),
                record_store=record_store,
                request=OdooProdBackupVerificationRequest(
                    context="cm",
                    backup_record_id="backup-gate-cm-prod-1",
                ),
            )

        self.assertEqual(result.verification_status, "fail")
        self.assertEqual(result.failure_code, "provider_verification_failed")
        verification_record = record_store.write_backup_gate_record.call_args.args[0]
        self.assertEqual(verification_record.status, "fail")
        self.assertEqual(
            verification_record.evidence["failure_code"],
            "provider_verification_failed",
        )

    def test_backup_verification_rejects_path_authority_drift_before_provider_read(self) -> None:
        record_store = self._record_store()
        record_store.read_backup_gate_record.return_value = _passing_backup_record().model_copy(
            update={"evidence": {**_backup_evidence(), "manifest_path": "/unexpected/path"}}
        )

        with (
            patch(
                "control_plane.workflows.odoo_prod_backup_gate.control_plane_runtime_environments.resolve_runtime_environment_values",
                return_value=_runtime_values(),
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_gate.dokploy_post_deploy.run_compose_odoo_backup_verification"
            ) as run_verification_mock,
            self.assertRaisesRegex(click.ClickException, "path authority"),
        ):
            execute_odoo_prod_backup_verification(
                control_plane_root=Path("/control-plane"),
                record_store=record_store,
                request=OdooProdBackupVerificationRequest(
                    context="cm",
                    backup_record_id="backup-gate-cm-prod-1",
                ),
            )

        run_verification_mock.assert_not_called()

    def test_backup_verification_rejects_non_passing_backup_record(self) -> None:
        record_store = self._record_store()
        record_store.read_backup_gate_record.return_value = _passing_backup_record().model_copy(
            update={"status": "fail"}
        )

        with (
            patch(
                "control_plane.workflows.odoo_prod_backup_gate.control_plane_runtime_environments.resolve_runtime_environment_values",
                return_value=_runtime_values(),
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_gate.dokploy_post_deploy.run_compose_odoo_backup_verification"
            ) as run_verification_mock,
            self.assertRaisesRegex(click.ClickException, "exact passing"),
        ):
            execute_odoo_prod_backup_verification(
                control_plane_root=Path("/control-plane"),
                record_store=record_store,
                request=OdooProdBackupVerificationRequest(
                    context="cm",
                    backup_record_id="backup-gate-cm-prod-1",
                ),
            )

        run_verification_mock.assert_not_called()

    def test_backup_verification_redacts_provider_failure(self) -> None:
        record_store = self._record_store()
        record_store.read_backup_gate_record.return_value = _passing_backup_record()

        with (
            patch(
                "control_plane.workflows.odoo_prod_backup_gate.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_gate.dokploy_post_deploy.run_compose_odoo_backup_verification",
                side_effect=click.ClickException(
                    "failed reading /volumes/data/private tenant backup with token=secret"
                ),
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_gate.control_plane_runtime_environments.resolve_runtime_environment_values",
                return_value=_runtime_values(),
            ),
        ):
            result = execute_odoo_prod_backup_verification(
                control_plane_root=Path("/control-plane"),
                record_store=record_store,
                request=OdooProdBackupVerificationRequest(
                    context="cm",
                    backup_record_id="backup-gate-cm-prod-1",
                ),
            )

        self.assertEqual(result.verification_status, "fail")
        self.assertEqual(result.failure_code, "provider_verification_failed")
        self.assertNotIn("private", result.model_dump_json())
        self.assertNotIn("secret", result.model_dump_json())


if __name__ == "__main__":
    unittest.main()
