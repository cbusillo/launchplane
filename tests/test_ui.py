import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from click.testing import CliRunner

from control_plane.cli import main
from control_plane.ui import render_environment_status_dashboard, render_inventory_overview_dashboard


def _write_inventory_record(
    inventory_dir: Path,
    *,
    context_name: str,
    instance_name: str,
    artifact_id: str,
    deployment_record_id: str,
) -> None:
    (inventory_dir / f"{context_name}-{instance_name}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "context": context_name,
                "instance": instance_name,
                "artifact_identity": {"artifact_id": artifact_id, "manifest_version": 1},
                "source_git_ref": f"{context_name}-ref",
                "deploy": {
                    "target_name": f"{context_name}-{instance_name}",
                    "target_type": "compose",
                    "deploy_mode": "dokploy-compose-api",
                    "deployment_id": deployment_record_id,
                    "status": "pass",
                },
                "post_deploy_update": {
                    "attempted": True,
                    "status": "pass",
                    "detail": f"{context_name} ready",
                },
                "destination_health": {
                    "verified": True,
                    "urls": [f"https://{context_name}.example.com/web/health"],
                    "timeout_seconds": 30,
                    "status": "pass",
                },
                "updated_at": "2026-04-11T05:30:00Z",
                "deployment_record_id": deployment_record_id,
                "promotion_record_id": "",
                "promoted_from_instance": "",
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _write_deployment_record(
    deployment_dir: Path,
    *,
    context_name: str,
    instance_name: str,
    artifact_id: str,
    deployment_record_id: str,
) -> None:
    (deployment_dir / f"{deployment_record_id}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "record_id": deployment_record_id,
                "artifact_identity": {"artifact_id": artifact_id, "manifest_version": 1},
                "context": context_name,
                "instance": instance_name,
                "source_git_ref": f"{context_name}-ref",
                "resolved_target": {
                    "target_type": "compose",
                    "target_id": f"compose-{context_name}",
                    "target_name": f"{context_name}-{instance_name}",
                },
                "deploy": {
                    "target_name": f"{context_name}-{instance_name}",
                    "target_type": "compose",
                    "deploy_mode": "dokploy-compose-api",
                    "deployment_id": deployment_record_id,
                    "status": "pass",
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _write_backup_gate_record(backup_gate_dir: Path, *, record_id: str, context_name: str, instance_name: str) -> None:
    (backup_gate_dir / f"{record_id}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "record_id": record_id,
                "context": context_name,
                "instance": instance_name,
                "created_at": "2026-04-11T05:00:00Z",
                "source": "prod-gate",
                "status": "pass",
                "evidence": {"snapshot": f"{context_name}-snapshot", "storage": "pbs"},
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _write_promotion_record(
    promotion_dir: Path,
    *,
    record_id: str,
    artifact_id: str,
    backup_record_id: str,
    context_name: str,
    from_instance_name: str,
    to_instance_name: str,
    deployment_record_id: str,
) -> None:
    (promotion_dir / f"{record_id}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "record_id": record_id,
                "artifact_identity": {"artifact_id": artifact_id, "manifest_version": 1},
                "backup_record_id": backup_record_id,
                "context": context_name,
                "from_instance": from_instance_name,
                "to_instance": to_instance_name,
                "source_health": {
                    "verified": True,
                    "urls": [f"https://{context_name}-{from_instance_name}.example.com/web/health"],
                    "timeout_seconds": 30,
                    "status": "pass",
                },
                "backup_gate": {
                    "required": True,
                    "status": "pass",
                    "evidence": {"snapshot": f"{context_name}-snapshot"},
                },
                "deploy": {
                    "target_name": f"{context_name}-{to_instance_name}",
                    "target_type": "compose",
                    "deploy_mode": "dokploy-compose-api",
                    "deployment_id": deployment_record_id,
                    "status": "pass",
                },
                "post_deploy_update": {
                    "attempted": True,
                    "status": "pass",
                    "detail": f"{context_name} promoted",
                },
                "destination_health": {
                    "verified": True,
                    "urls": [f"https://{context_name}.example.com/web/health"],
                    "timeout_seconds": 30,
                    "status": "pass",
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )


class InventoryOverviewUiTests(unittest.TestCase):
    def test_render_inventory_overview_dashboard_includes_summary_and_cards(self) -> None:
        html = render_inventory_overview_dashboard(
            [
                {
                    "context": "acme",
                    "instance": "prod",
                    "live": {
                        "artifact_id": "artifact-acme",
                        "source_git_ref": "acme-ref",
                        "updated_at": "2026-04-11T05:30:00Z",
                        "deployment_record_id": "deployment-acme",
                        "promotion_record_id": "promotion-acme",
                        "promoted_from_instance": "testing",
                        "deploy_status": "pass",
                        "post_deploy_update_status": "pass",
                        "destination_health_status": "pass",
                    },
                    "live_promotion": {"backup_record_id": "backup-acme"},
                    "authorized_backup_gate": {"status": "pass"},
                    "latest_promotion": {"record_id": "promotion-acme"},
                    "latest_deployment": {"record_id": "deployment-acme", "deployment_id": "deploy-1"},
                }
            ],
            context_name="acme",
        )

        self.assertIn("Inventory overview for acme", html)
        self.assertIn("Control Plane Operator View", html)
        self.assertIn("artifact-acme", html)
        self.assertIn("promotion-acme", html)
        self.assertIn("environment-filter", html)

    def test_ui_inventory_overview_writes_static_dashboard_file(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            inventory_dir = state_dir / "inventory"
            deployment_dir = state_dir / "deployments"
            inventory_dir.mkdir(parents=True, exist_ok=True)
            deployment_dir.mkdir(parents=True, exist_ok=True)
            _write_inventory_record(
                inventory_dir,
                context_name="zeta",
                instance_name="testing",
                artifact_id="artifact-zeta",
                deployment_record_id="deployment-zeta",
            )
            _write_deployment_record(
                deployment_dir,
                context_name="zeta",
                instance_name="testing",
                artifact_id="artifact-zeta",
                deployment_record_id="deployment-zeta",
            )
            output_file = repo_root / "tmp" / "inventory-overview.html"

            result = runner.invoke(
                main,
                [
                    "ui",
                    "inventory-overview",
                    "--state-dir",
                    str(state_dir),
                    "--context",
                    "zeta",
                    "--output-file",
                    str(output_file),
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            rendered_html = output_file.read_text(encoding="utf-8")
            self.assertEqual(rendered_html.count('<article class="environment-card"'), 1)
            self.assertIn("Inventory overview for zeta", rendered_html)
            self.assertIn("artifact-zeta", rendered_html)
            self.assertIn("deployment-zeta", rendered_html)

    def test_render_environment_status_dashboard_includes_backup_and_timeline(self) -> None:
        html = render_environment_status_dashboard(
            {
                "context": "acme",
                "instance": "prod",
                "live": {
                    "artifact_id": "artifact-acme",
                    "source_git_ref": "acme-ref",
                    "updated_at": "2026-04-11T05:30:00Z",
                    "deployment_record_id": "deployment-acme",
                    "promotion_record_id": "promotion-acme",
                    "promoted_from_instance": "testing",
                    "deploy_status": "pass",
                    "post_deploy_update_status": "pass",
                    "destination_health_status": "pass",
                },
                "live_promotion": {
                    "record_id": "promotion-acme",
                    "artifact_id": "artifact-acme",
                    "backup_record_id": "backup-acme",
                    "from_instance": "testing",
                    "to_instance": "prod",
                    "deploy_status": "pass",
                    "destination_health_status": "pass",
                },
                "authorized_backup_gate": {
                    "record_id": "backup-acme",
                    "status": "pass",
                    "source": "prod-gate",
                    "created_at": "2026-04-11T05:00:00Z",
                    "evidence": {"snapshot": "acme-snapshot"},
                },
                "latest_promotion": {
                    "record_id": "promotion-acme",
                    "from_instance": "testing",
                    "deploy_status": "pass",
                },
                "latest_deployment": {
                    "record_id": "deployment-acme",
                    "deployment_id": "deploy-1",
                    "target_name": "acme-prod",
                    "deploy_status": "pass",
                },
            }
        )

        self.assertIn("acme / prod", html)
        self.assertIn("Backup gate", html)
        self.assertIn("promotion-acme", html)
        self.assertIn("deployment-acme", html)
        self.assertIn("Suggested next step", html)
        self.assertIn("Production is promotion-managed", html)
        self.assertIn("promote resolve", html)
        self.assertIn("acme-snapshot", html)

    def test_ui_environment_status_writes_static_dashboard_file(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            inventory_dir = state_dir / "inventory"
            deployment_dir = state_dir / "deployments"
            promotion_dir = state_dir / "promotions"
            backup_gate_dir = state_dir / "backup_gates"
            inventory_dir.mkdir(parents=True, exist_ok=True)
            deployment_dir.mkdir(parents=True, exist_ok=True)
            promotion_dir.mkdir(parents=True, exist_ok=True)
            backup_gate_dir.mkdir(parents=True, exist_ok=True)
            _write_inventory_record(
                inventory_dir,
                context_name="acme",
                instance_name="prod",
                artifact_id="artifact-acme",
                deployment_record_id="deployment-acme",
            )
            inventory_path = inventory_dir / "acme-prod.json"
            inventory_payload = json.loads(inventory_path.read_text(encoding="utf-8"))
            inventory_payload["promotion_record_id"] = "promotion-acme"
            inventory_payload["promoted_from_instance"] = "testing"
            inventory_path.write_text(json.dumps(inventory_payload, indent=2), encoding="utf-8")
            _write_deployment_record(
                deployment_dir,
                context_name="acme",
                instance_name="prod",
                artifact_id="artifact-acme",
                deployment_record_id="deployment-acme",
            )
            _write_backup_gate_record(
                backup_gate_dir,
                record_id="backup-acme",
                context_name="acme",
                instance_name="prod",
            )
            _write_promotion_record(
                promotion_dir,
                record_id="promotion-acme",
                artifact_id="artifact-acme",
                backup_record_id="backup-acme",
                context_name="acme",
                from_instance_name="testing",
                to_instance_name="prod",
                deployment_record_id="deployment-acme",
            )
            output_file = repo_root / "tmp" / "environment-status.html"

            result = runner.invoke(
                main,
                [
                    "ui",
                    "environment-status",
                    "--state-dir",
                    str(state_dir),
                    "--context",
                    "acme",
                    "--instance",
                    "prod",
                    "--output-file",
                    str(output_file),
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            rendered_html = output_file.read_text(encoding="utf-8")
            self.assertIn("acme / prod", rendered_html)
            self.assertIn("backup-acme", rendered_html)
            self.assertIn("promotion-acme", rendered_html)
            self.assertIn("deployment-acme", rendered_html)
            self.assertIn("Suggested next step", rendered_html)
            self.assertIn("Production is promotion-managed", rendered_html)
            self.assertIn("promote execute --input-file tmp/promotion-request.json", rendered_html)
            self.assertIn("pbs", rendered_html)


if __name__ == "__main__":
    unittest.main()
