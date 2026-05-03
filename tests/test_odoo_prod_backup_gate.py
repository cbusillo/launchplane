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
    execute_odoo_prod_backup_gate,
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


class OdooProdBackupGateWorkflowTests(unittest.TestCase):
    def test_backup_gate_request_accepts_profile_owned_context(self) -> None:
        request = OdooProdBackupGateRequest(
            context=" New-Site ",
            backup_record_id="backup-gate-new-site-prod-1",
        )

        self.assertEqual(request.context, "new-site")

    def test_backup_gate_request_rejects_blank_context(self) -> None:
        with self.assertRaises(ValidationError):
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
                "control_plane.workflows.odoo_prod_backup_gate.control_plane_dokploy.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_gate.control_plane_dokploy.run_compose_odoo_backup_gate"
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
                "control_plane.workflows.odoo_prod_backup_gate.control_plane_dokploy.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_prod_backup_gate.control_plane_dokploy.run_compose_odoo_backup_gate",
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


if __name__ == "__main__":
    unittest.main()
