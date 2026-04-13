import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from click.testing import CliRunner

from control_plane.cli import main
from control_plane.ui import (
    render_environment_contract_dashboard,
    render_environment_status_dashboard,
    render_inventory_overview_dashboard,
)


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


def _write_artifact_manifest(artifact_dir: Path, *, artifact_id: str) -> None:
    (artifact_dir / f"{artifact_id}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "artifact_id": artifact_id,
                "source_commit": "abcdef1234567890",
                "enterprise_base_digest": "sha256:enterprisebase",
                "addon_sources": [
                    {"repository": "git@github.com:every/opw-addons.git", "ref": "main"},
                    {"repository": "git@github.com:every/shared-addons.git", "ref": "stable"},
                ],
                "openupgrade_inputs": {
                    "addon_repository": "git@github.com:OCA/OpenUpgrade.git",
                    "install_spec": "openupgradelib==3.10.0",
                },
                "build_flags": {
                    "addon_skip_flags": ["skip_demo"],
                    "values": {"ODOO_VERSION": "18.0"},
                },
                "image": {
                    "repository": "ghcr.io/every/opw",
                    "digest": "sha256:artifactdigest",
                    "tags": [artifact_id, "latest-testing"],
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

    def test_render_environment_contract_dashboard_redacts_sensitive_values(self) -> None:
        html = render_environment_contract_dashboard(
            {
                "context": "opw",
                "instance": "local",
                "source_file": "/tmp/runtime-environments.toml",
                "available_contexts": (
                    {"context": "cm", "instance_count": 4},
                    {"context": "opw", "instance_count": 3},
                ),
                "available_instances": ("local", "testing", "prod"),
                "layer_summaries": (
                    {"label": "Global shared", "count": 2, "note": "Applies everywhere."},
                    {"label": "opw shared", "count": 1, "note": "Context defaults."},
                    {"label": "local instance", "count": 1, "note": "Instance overrides."},
                    {"label": "Resolved", "count": 3, "note": "Merged."},
                ),
                "resolved_rows": (
                    {
                        "key": "ODOO_DB_PASSWORD",
                        "value": "super-secret-password",
                        "source": "instance",
                        "overrides": ("global",),
                    },
                    {
                        "key": "ENV_OVERRIDE_CONFIG_PARAM__WEB__BASE__URL",
                        "value": "https://opw-local.example.com",
                        "source": "context",
                        "overrides": (),
                    },
                ),
                "global_rows": (
                    {
                        "key": "ODOO_DB_USER",
                        "value": "odoo",
                        "source": "global",
                        "overrides": (),
                    },
                ),
                "context_rows": (),
                "instance_rows": (),
            }
        )

        self.assertIn("Environment contract for opw/local", html)
        self.assertIn("Final merged environment", html)
        self.assertIn("ENV_OVERRIDE_CONFIG_PARAM__WEB__BASE__URL", html)
        self.assertIn("https://opw-local.example.com", html)
        self.assertIn("[redacted ending word]", html)
        self.assertNotIn("super-secret-password", html)

    def test_ui_environment_contract_writes_static_dashboard_file(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            environments_file = control_plane_root / "config" / "runtime-environments.toml"
            environments_file.parent.mkdir(parents=True, exist_ok=True)
            environments_file.write_text(
                """
schema_version = 1

[shared_env]
ODOO_DB_USER = "odoo"
ODOO_MASTER_PASSWORD = "shared-master"

[contexts.opw.shared_env]
ENV_OVERRIDE_DISABLE_CRON = true

[contexts.opw.instances.local.env]
ODOO_DB_PASSWORD = "local-secret"
ENV_OVERRIDE_CONFIG_PARAM__WEB__BASE__URL = "https://opw-local.example.com"
""".strip()
                + "\n",
                encoding="utf-8",
            )
            output_file = control_plane_root / "tmp" / "environment-contract.html"

            with patch("control_plane.cli._control_plane_root", return_value=control_plane_root):
                result = runner.invoke(
                    main,
                    [
                        "ui",
                        "environment-contract",
                        "--context",
                        "opw",
                        "--instance",
                        "local",
                        "--output-file",
                        str(output_file),
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            rendered_html = output_file.read_text(encoding="utf-8")
            self.assertIn("Environment contract for opw/local", rendered_html)
            self.assertIn("https://opw-local.example.com", rendered_html)
            self.assertIn("ODOO_MASTER_PASSWORD", rendered_html)
            self.assertIn("[redacted ending ster]", rendered_html)
            self.assertNotIn("shared-master", rendered_html)

    def test_ui_build_site_generates_linked_pages(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            state_dir = control_plane_root / "state"
            inventory_dir = state_dir / "inventory"
            deployment_dir = state_dir / "deployments"
            promotion_dir = state_dir / "promotions"
            backup_gate_dir = state_dir / "backup_gates"
            artifact_dir = state_dir / "artifacts"
            inventory_dir.mkdir(parents=True, exist_ok=True)
            deployment_dir.mkdir(parents=True, exist_ok=True)
            promotion_dir.mkdir(parents=True, exist_ok=True)
            backup_gate_dir.mkdir(parents=True, exist_ok=True)
            artifact_dir.mkdir(parents=True, exist_ok=True)
            _write_inventory_record(
                inventory_dir,
                context_name="opw",
                instance_name="testing",
                artifact_id="artifact-opw",
                deployment_record_id="deployment-opw",
            )
            inventory_record_path = inventory_dir / "opw-testing.json"
            inventory_payload = json.loads(inventory_record_path.read_text(encoding="utf-8"))
            inventory_payload["promotion_record_id"] = "promotion-opw"
            inventory_payload["promoted_from_instance"] = "local"
            inventory_record_path.write_text(
                json.dumps(inventory_payload, indent=2),
                encoding="utf-8",
            )
            _write_deployment_record(
                deployment_dir,
                context_name="opw",
                instance_name="testing",
                artifact_id="artifact-opw",
                deployment_record_id="deployment-opw",
            )
            _write_backup_gate_record(
                backup_gate_dir,
                record_id="backup-opw",
                context_name="opw",
                instance_name="testing",
            )
            _write_promotion_record(
                promotion_dir,
                record_id="promotion-opw",
                artifact_id="artifact-opw",
                backup_record_id="backup-opw",
                context_name="opw",
                from_instance_name="local",
                to_instance_name="testing",
                deployment_record_id="deployment-opw",
            )
            _write_artifact_manifest(artifact_dir, artifact_id="artifact-opw")
            environments_file = control_plane_root / "config" / "runtime-environments.toml"
            environments_file.parent.mkdir(parents=True, exist_ok=True)
            environments_file.write_text(
                """
schema_version = 1

[shared_env]
ODOO_DB_USER = "odoo"

[contexts.opw.shared_env]
ENV_OVERRIDE_DISABLE_CRON = true

[contexts.opw.instances.local.env]
ODOO_DB_PASSWORD = "local-secret"

[contexts.opw.instances.testing.env]
ODOO_DB_PASSWORD = "testing-secret"
""".strip()
                + "\n",
                encoding="utf-8",
            )
            output_dir = control_plane_root / "tmp" / "operator-ui"

            with patch("control_plane.cli._control_plane_root", return_value=control_plane_root):
                result = runner.invoke(
                    main,
                    [
                        "ui",
                        "build-site",
                        "--state-dir",
                        str(state_dir),
                        "--context",
                        "opw",
                        "--output-dir",
                        str(output_dir),
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            index_html = (output_dir / "index.html").read_text(encoding="utf-8")
            overview_html = (output_dir / "inventory-overview.html").read_text(encoding="utf-8")
            status_html = (output_dir / "environments" / "opw-testing-status.html").read_text(encoding="utf-8")
            contract_html = (output_dir / "contracts" / "opw-testing-contract.html").read_text(encoding="utf-8")
            deployment_record_html = (
                output_dir / "records" / "deployments" / "deployment-opw.html"
            ).read_text(encoding="utf-8")
            promotion_record_html = (
                output_dir / "records" / "promotions" / "promotion-opw.html"
            ).read_text(encoding="utf-8")
            backup_gate_record_html = (
                output_dir / "records" / "backup-gates" / "backup-opw.html"
            ).read_text(encoding="utf-8")
            artifact_record_html = (
                output_dir / "records" / "artifacts" / "artifact-opw.html"
            ).read_text(encoding="utf-8")

            self.assertIn("Operator cockpit for opw", index_html)
            self.assertIn("inventory-overview.html", index_html)
            self.assertIn("contracts/opw-testing-contract.html", index_html)
            self.assertIn("environments/opw-testing-status.html", index_html)
            self.assertIn("index.html", overview_html)
            self.assertIn("environments/opw-testing-status.html", overview_html)
            self.assertIn("records/artifacts/artifact-opw.html", overview_html)
            self.assertIn("../inventory-overview.html", status_html)
            self.assertIn("../contracts/opw-testing-contract.html", status_html)
            self.assertIn("../records/artifacts/artifact-opw.html", status_html)
            self.assertIn("../records/deployments/deployment-opw.html", status_html)
            self.assertIn("../records/promotions/promotion-opw.html", status_html)
            self.assertIn("../records/backup-gates/backup-opw.html", status_html)
            self.assertIn("../environments/opw-testing-status.html", contract_html)
            self.assertIn("../../records/artifacts/artifact-opw.html", deployment_record_html)
            self.assertIn("../../records/artifacts/artifact-opw.html", promotion_record_html)
            self.assertIn("../../environments/opw-testing-status.html", deployment_record_html)
            self.assertIn("../../environments/opw-local-status.html", promotion_record_html)
            self.assertIn("../../records/backup-gates/backup-opw.html", promotion_record_html)
            self.assertIn("../../environments/opw-testing-status.html", backup_gate_record_html)
            self.assertIn("abcdef1234567890", artifact_record_html)
            self.assertIn("../../environments/opw-testing-status.html", artifact_record_html)
            self.assertIn("../../records/deployments/deployment-opw.html", artifact_record_html)
            self.assertIn("../../records/promotions/promotion-opw.html", artifact_record_html)

    def test_ui_build_site_generates_placeholder_status_when_inventory_missing(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            state_dir = control_plane_root / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            environments_file = control_plane_root / "config" / "runtime-environments.toml"
            environments_file.parent.mkdir(parents=True, exist_ok=True)
            environments_file.write_text(
                """
schema_version = 1

[shared_env]
ODOO_DB_USER = "odoo"

[contexts.opw.instances.local.env]
ODOO_DB_PASSWORD = "local-secret"
""".strip()
                + "\n",
                encoding="utf-8",
            )
            output_dir = control_plane_root / "tmp" / "operator-ui"

            with patch("control_plane.cli._control_plane_root", return_value=control_plane_root):
                result = runner.invoke(
                    main,
                    [
                        "ui",
                        "build-site",
                        "--context",
                        "opw",
                        "--output-dir",
                        str(output_dir),
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            index_html = (output_dir / "index.html").read_text(encoding="utf-8")
            status_html = (output_dir / "environments" / "opw-local-status.html").read_text(encoding="utf-8")

            self.assertIn("No live inventory record yet", index_html)
            self.assertIn("environments/opw-local-status.html", index_html)
            self.assertIn("No live inventory record yet", status_html)
            self.assertIn("../contracts/opw-local-contract.html", status_html)


if __name__ == "__main__":
    unittest.main()
