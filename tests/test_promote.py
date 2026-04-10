import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import click
from click.testing import CliRunner

from control_plane.cli import _resolve_dokploy_target, _sync_artifact_image_reference_for_target, main
from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.deployment_record import DeploymentRecord, ResolvedTargetEvidence
from control_plane.contracts.promotion_record import CompatibilityPromotionRequest
from control_plane.contracts.ship_request import CompatibilityShipRequest
from control_plane.workflows.ship import build_compatibility_deployment_record
from control_plane.workflows.promote import build_promotion_record
from control_plane.workflows.promote import build_compatibility_promotion_record


class PromoteWorkflowTests(unittest.TestCase):
    def test_build_promotion_record_returns_pending_record(self) -> None:
        record = build_promotion_record(
            record_id="promotion-20260410-182231-opw-testing-prod",
            artifact_id="artifact-20260410-f45db648",
            context_name="opw",
            from_instance_name="testing",
            to_instance_name="prod",
            target_name="opw-prod",
            target_type="compose",
            deploy_mode="dokploy-compose-api",
            deployment_id="",
        )

        self.assertEqual(record.artifact_identity.artifact_id, "artifact-20260410-f45db648")
        self.assertEqual(record.deploy.status, "pending")
        self.assertEqual(record.deploy.target_name, "opw-prod")
        self.assertEqual(record.from_instance, "testing")

    def test_build_compatibility_promotion_record_marks_success_after_waited_ship(self) -> None:
        request = CompatibilityPromotionRequest(
            artifact_id="compatibility-opw-abc123",
            source_git_ref="abc123",
            context="opw",
            from_instance="testing",
            to_instance="prod",
            target_name="opw-prod",
            target_type="compose",
            deploy_mode="dokploy-compose-api",
            wait=True,
            verify_health=True,
            destination_health={
                "verified": False,
                "urls": ["https://prod.example.com/web/health"],
                "timeout_seconds": 45,
                "status": "pending",
            },
            source_health={
                "verified": True,
                "urls": ["https://testing.example.com/web/health"],
                "timeout_seconds": 30,
                "status": "pass",
            },
            backup_gate={"required": True, "status": "pass", "evidence": {"snapshot": "snap-1"}},
        )

        record = build_compatibility_promotion_record(
            request=request,
            record_id="promotion-1",
            deployment_id="delegated-ship",
            deployment_status="pass",
        )

        self.assertEqual(record.deploy.status, "pass")
        self.assertTrue(record.destination_health.verified)
        self.assertEqual(record.destination_health.status, "pass")
        self.assertTrue(record.post_deploy_update.attempted)
        self.assertEqual(record.post_deploy_update.status, "pass")

    def test_build_compatibility_deployment_record_marks_pending_health_for_async_ship(self) -> None:
        request = CompatibilityShipRequest(
            context="opw",
            instance="prod",
            source_git_ref="abc123",
            target_name="opw-prod",
            target_type="compose",
            deploy_mode="dokploy-compose-api",
            artifact_id="artifact-sha256-image456",
            wait=False,
            verify_health=True,
            destination_health={
                "verified": False,
                "urls": ["https://prod.example.com/web/health"],
                "timeout_seconds": 45,
                "status": "pending",
            },
        )

        record = build_compatibility_deployment_record(
            request=request,
            record_id="deployment-1",
            deployment_id="delegated-ship",
            deployment_status="pending",
            started_at="2026-04-10T18:22:31Z",
            finished_at="",
        )

        self.assertIsInstance(record, DeploymentRecord)
        self.assertEqual(record.artifact_identity.artifact_id, "artifact-sha256-image456")
        self.assertEqual(record.deploy.status, "pending")
        self.assertEqual(record.post_deploy_update.status, "skipped")
        self.assertEqual(record.destination_health.status, "pending")
        self.assertFalse(record.destination_health.verified)

    def test_build_compatibility_deployment_record_marks_post_deploy_update_success_for_waited_compose_ship(self) -> None:
        request = CompatibilityShipRequest(
            context="opw",
            instance="prod",
            source_git_ref="abc123",
            target_name="opw-prod",
            target_type="compose",
            deploy_mode="dokploy-compose-api",
            wait=True,
            verify_health=True,
            destination_health={
                "verified": False,
                "urls": ["https://prod.example.com/web/health"],
                "timeout_seconds": 45,
                "status": "pending",
            },
        )

        record = build_compatibility_deployment_record(
            request=request,
            record_id="deployment-compose-pass",
            deployment_id="control-plane-dokploy",
            deployment_status="pass",
            started_at="2026-04-10T18:22:31Z",
            finished_at="2026-04-10T18:24:00Z",
        )

        self.assertTrue(record.post_deploy_update.attempted)
        self.assertEqual(record.post_deploy_update.status, "pass")
        self.assertIn("canonical odoo-ai platform update workflow", record.post_deploy_update.detail)

    def test_build_compatibility_deployment_record_carries_branch_sync_evidence(self) -> None:
        request = CompatibilityShipRequest(
            context="opw",
            instance="prod",
            source_git_ref="origin/opw-prod",
            target_name="opw-prod",
            target_type="compose",
            deploy_mode="dokploy-compose-api",
            branch_sync={
                "source_git_ref": "origin/opw-prod",
                "source_commit": "abc123",
                "target_branch": "prod",
                "remote_branch_commit_before": "def456",
                "branch_update_required": True,
            },
        )

        record = build_compatibility_deployment_record(
            request=request,
            record_id="deployment-branch-sync",
            deployment_id="delegated-ship",
            deployment_status="pending",
            started_at="2026-04-10T18:22:31Z",
            finished_at="",
            resolved_target={
                "target_type": "compose",
                "target_id": "compose-123",
                "target_name": "opw-prod",
            },
        )

        self.assertEqual(record.branch_sync.source_commit, "abc123")
        self.assertEqual(record.branch_sync.target_branch, "prod")
        self.assertTrue(record.branch_sync.branch_update_required)
        self.assertEqual(record.resolved_target.target_id, "compose-123")


class ArtifactImageOverrideTests(unittest.TestCase):
    def test_sync_artifact_image_reference_sets_exact_image_reference(self) -> None:
        resolved_target = ResolvedTargetEvidence(target_type="compose", target_id="compose-123", target_name="opw-prod")
        artifact_manifest = ArtifactIdentityManifest.model_validate(
            {
                "artifact_id": "artifact-sha256-image456",
                "odoo_ai_commit": "abc123",
                "enterprise_base_digest": "sha256:enterprise123",
                "image": {
                    "repository": "ghcr.io/cbusillo/odoo-private",
                    "digest": "sha256:image456",
                    "tags": ["sha-abc123"],
                },
            }
        )
        captured_update: dict[str, object] = {}

        with patch(
            "control_plane.dokploy.read_dokploy_config",
            return_value=("https://dokploy.example.com", "token-123"),
        ), patch(
            "control_plane.dokploy.fetch_dokploy_target_payload",
            return_value={"env": "DOCKER_IMAGE=odoo-ai\nDOCKER_IMAGE_TAG=latest"},
        ), patch(
            "control_plane.dokploy.update_dokploy_target_env",
            side_effect=lambda **kwargs: captured_update.update(kwargs),
        ):
            _sync_artifact_image_reference_for_target(
                artifact_manifest=artifact_manifest,
                resolved_target=resolved_target,
            )

        self.assertEqual(captured_update["target_type"], "compose")
        self.assertEqual(captured_update["target_id"], "compose-123")
        self.assertIn(
            "DOCKER_IMAGE_REFERENCE=ghcr.io/cbusillo/odoo-private@sha256:image456",
            str(captured_update["env_text"]),
        )

    def test_sync_artifact_image_reference_clears_stale_override_without_manifest(self) -> None:
        resolved_target = ResolvedTargetEvidence(target_type="compose", target_id="compose-123", target_name="opw-prod")
        captured_update: dict[str, object] = {}

        with patch(
            "control_plane.dokploy.read_dokploy_config",
            return_value=("https://dokploy.example.com", "token-123"),
        ), patch(
            "control_plane.dokploy.fetch_dokploy_target_payload",
            return_value={
                "env": (
                    "DOCKER_IMAGE=odoo-ai\n"
                    "DOCKER_IMAGE_TAG=latest\n"
                    "DOCKER_IMAGE_REFERENCE=ghcr.io/cbusillo/odoo-private@sha256:stale"
                )
            },
        ), patch(
            "control_plane.dokploy.update_dokploy_target_env",
            side_effect=lambda **kwargs: captured_update.update(kwargs),
        ):
            _sync_artifact_image_reference_for_target(
                artifact_manifest=None,
                resolved_target=resolved_target,
            )

        self.assertNotIn("DOCKER_IMAGE_REFERENCE", str(captured_update["env_text"]))
        self.assertIn("DOCKER_IMAGE=odoo-ai", str(captured_update["env_text"]))

    def test_ship_artifact_manifest_bypasses_branch_sync_execution(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            artifact_dir = state_dir / "artifacts"
            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / "artifact-sha256-image456.json").write_text(
                json.dumps(
                    {
                        "artifact_id": "artifact-sha256-image456",
                        "odoo_ai_commit": "abc123",
                        "enterprise_base_digest": "sha256:enterprise123",
                        "image": {
                            "repository": "ghcr.io/cbusillo/odoo-private",
                            "digest": "sha256:image456",
                            "tags": ["sha-abc123"],
                        },
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            platform_dir = repo_root / "platform"
            platform_dir.mkdir(parents=True, exist_ok=True)
            (platform_dir / "dokploy.toml").write_text(
                """
schema_version = 2

[defaults]
target_type = "compose"

[[targets]]
context = "opw"
instance = "prod"
target_id = "compose-123"
""".strip(),
                encoding="utf-8",
            )
            input_file = repo_root / "ship-request.json"
            input_file.write_text(
                CompatibilityShipRequest(
                    context="opw",
                    instance="prod",
                    source_git_ref="abc123",
                    target_name="opw-prod",
                    target_type="compose",
                    deploy_mode="dokploy-compose-api",
                    wait=True,
                    verify_health=False,
                    branch_sync={
                        "source_git_ref": "origin/main",
                        "source_commit": "abc123",
                        "target_branch": "opw-prod",
                        "remote_branch_commit_before": "def456",
                        "branch_update_required": True,
                    },
                ).model_dump_json(indent=2),
                encoding="utf-8",
            )
            captured_commands: list[list[str]] = []

            with patch(
                "control_plane.cli._run_command",
                side_effect=lambda command, cwd=None: captured_commands.append(command),
            ), patch(
                "control_plane.cli._sync_artifact_image_reference_for_target",
                side_effect=lambda **_kwargs: None,
            ), patch(
                "control_plane.cli._execute_dokploy_deploy",
                side_effect=lambda **_kwargs: None,
            ):
                result = runner.invoke(
                    main,
                    [
                        "ship",
                        "compatibility-execute",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                        "--odoo-ai-root",
                        str(repo_root),
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertEqual(len(captured_commands), 1)
            self.assertIn("update", captured_commands[0])
            deployment_files = sorted((state_dir / "deployments").glob("*.json"))
            self.assertEqual(len(deployment_files), 1)
            persisted_payload = deployment_files[0].read_text(encoding="utf-8")
            self.assertIn('"artifact_id": "artifact-sha256-image456"', persisted_payload)
            self.assertIn('"skipped_for_artifact_image": true', persisted_payload)
            self.assertIn('"branch_update_required": false', persisted_payload)
            self.assertIn('"applied": false', persisted_payload)


class PromoteCliTests(unittest.TestCase):
    def test_compatibility_execute_persists_record_and_executes_control_plane_ship(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            input_file = repo_root / "promotion-request.json"
            input_file.write_text(
                CompatibilityPromotionRequest(
                    artifact_id="compatibility-opw-abc123",
                    source_git_ref="abc123",
                    context="opw",
                    from_instance="testing",
                    to_instance="prod",
                    target_name="opw-prod",
                    target_type="compose",
                    deploy_mode="dokploy-compose-api",
                    wait=True,
                    verify_health=True,
                    health_timeout_seconds=45,
                    source_health={
                        "verified": True,
                        "urls": ["https://testing.example.com/web/health"],
                        "timeout_seconds": 30,
                        "status": "pass",
                    },
                    backup_gate={"required": True, "status": "pass", "evidence": {"snapshot": "snap-1"}},
                    destination_health={
                        "verified": False,
                        "urls": ["https://prod.example.com/web/health"],
                        "timeout_seconds": 45,
                        "status": "pending",
                    },
                ).model_dump_json(indent=2),
                encoding="utf-8",
            )
            with patch(
                "control_plane.cli._export_ship_request_via_odoo_ai",
                return_value=CompatibilityShipRequest(
                    context="opw",
                    instance="prod",
                    source_git_ref="abc123",
                    target_name="opw-prod",
                    target_type="compose",
                    deploy_mode="dokploy-compose-api",
                    artifact_id="compatibility-opw-abc123",
                    wait=True,
                    verify_health=True,
                    destination_health={
                        "verified": False,
                        "urls": ["https://prod.example.com/web/health"],
                        "timeout_seconds": 45,
                        "status": "pending",
                    },
                ),
            ), patch(
                "control_plane.cli._execute_compatibility_ship",
                return_value=(
                    state_dir / "deployments" / "deployment-1.json",
                    DeploymentRecord(
                        record_id="deployment-1",
                        artifact_identity={"artifact_id": "compatibility-opw-abc123"},
                        context="opw",
                        instance="prod",
                        source_git_ref="abc123",
                        delegated_executor="control-plane.dokploy",
                        deploy={
                            "target_name": "opw-prod",
                            "target_type": "compose",
                            "deploy_mode": "dokploy-compose-api",
                            "deployment_id": "control-plane-dokploy",
                            "status": "pass",
                            "started_at": "2026-04-10T18:22:31Z",
                            "finished_at": "2026-04-10T18:24:00Z",
                        },
                    ),
                ),
            ):
                result = runner.invoke(
                    main,
                    [
                        "promote",
                        "compatibility-execute",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                        "--odoo-ai-root",
                        str(repo_root),
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            promotion_files = sorted((state_dir / "promotions").glob("*.json"))
            self.assertEqual(len(promotion_files), 1)
            persisted_payload = promotion_files[0].read_text(encoding="utf-8")
            self.assertIn('"status": "pass"', persisted_payload)
            self.assertIn('"artifact_id": "compatibility-opw-abc123"', persisted_payload)
            self.assertIn('"deployment_id": "control-plane-dokploy"', persisted_payload)

    def test_inventory_show_reads_current_environment_state(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            inventory_dir = state_dir / "inventory"
            inventory_dir.mkdir(parents=True, exist_ok=True)
            (inventory_dir / "opw-prod.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "context": "opw",
                        "instance": "prod",
                        "artifact_identity": {"artifact_id": "artifact-sha256-image456", "manifest_version": 1},
                        "source_git_ref": "abc123",
                        "deploy": {
                            "target_name": "opw-prod",
                            "target_type": "compose",
                            "deploy_mode": "dokploy-compose-api",
                            "deployment_id": "control-plane-dokploy",
                            "status": "pass",
                            "started_at": "2026-04-10T18:22:31Z",
                            "finished_at": "2026-04-10T18:24:00Z",
                        },
                        "post_deploy_update": {
                            "attempted": True,
                            "status": "pass",
                            "detail": "Odoo-specific post-deploy update completed through the canonical odoo-ai platform update workflow.",
                        },
                        "destination_health": {
                            "verified": True,
                            "urls": ["https://prod.example.com/web/health"],
                            "timeout_seconds": 45,
                            "status": "pass",
                        },
                        "updated_at": "2026-04-10T18:24:01Z",
                        "deployment_record_id": "deployment-1",
                        "promotion_record_id": "promotion-1",
                        "promoted_from_instance": "testing",
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "inventory",
                    "show",
                    "--state-dir",
                    str(state_dir),
                    "--context",
                    "opw",
                    "--instance",
                    "prod",
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn('"artifact_id": "artifact-sha256-image456"', result.output)
            self.assertIn('"promoted_from_instance": "testing"', result.output)

    def test_compatibility_execute_prefers_stored_artifact_manifest_for_commit(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            artifact_dir = state_dir / "artifacts"
            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / "artifact-sha256-image456.json").write_text(
                json.dumps(
                    {
                        "artifact_id": "artifact-sha256-image456",
                        "odoo_ai_commit": "abc123",
                        "enterprise_base_digest": "sha256:enterprise123",
                        "image": {
                            "repository": "ghcr.io/cbusillo/odoo-private",
                            "digest": "sha256:image456",
                            "tags": ["sha-abc123"],
                        },
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            input_file = repo_root / "promotion-request.json"
            input_file.write_text(
                CompatibilityPromotionRequest(
                    artifact_id="compatibility-opw-abc123",
                    source_git_ref="abc123",
                    context="opw",
                    from_instance="testing",
                    to_instance="prod",
                    target_name="opw-prod",
                    target_type="compose",
                    deploy_mode="dokploy-compose-api",
                    dry_run=True,
                ).model_dump_json(indent=2),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "promote",
                    "compatibility-execute",
                    "--state-dir",
                    str(state_dir),
                    "--input-file",
                    str(input_file),
                    "--odoo-ai-root",
                    str(repo_root),
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn('"artifact_id": "artifact-sha256-image456"', result.output)

    def test_ship_compatibility_execute_persists_environment_inventory_after_success(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            input_file = repo_root / "ship-request.json"
            input_file.write_text(
                CompatibilityShipRequest(
                    context="opw",
                    instance="prod",
                    source_git_ref="abc123",
                    target_name="opw-prod",
                    target_type="compose",
                    deploy_mode="dokploy-compose-api",
                    artifact_id="artifact-sha256-image456",
                    wait=True,
                    verify_health=True,
                    destination_health={
                        "verified": False,
                        "urls": ["https://prod.example.com/web/health"],
                        "timeout_seconds": 45,
                        "status": "pending",
                    },
                ).model_dump_json(indent=2),
                encoding="utf-8",
            )

            with patch(
                "control_plane.cli._resolve_dokploy_target",
                return_value=(
                    ResolvedTargetEvidence(target_type="compose", target_id="compose-123", target_name="opw-prod"),
                    600,
                ),
            ), patch(
                "control_plane.cli._sync_artifact_image_reference_for_target",
                side_effect=lambda **_kwargs: None,
            ), patch(
                "control_plane.cli._execute_dokploy_deploy",
                side_effect=lambda **_kwargs: None,
            ), patch(
                "control_plane.cli._run_post_deploy_update_via_odoo_ai",
                side_effect=lambda **_kwargs: None,
            ), patch(
                "control_plane.cli._wait_for_ship_healthcheck",
                side_effect=lambda **_kwargs: None,
            ):
                result = runner.invoke(
                    main,
                    [
                        "ship",
                        "compatibility-execute",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                        "--odoo-ai-root",
                        str(repo_root),
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            inventory_file = state_dir / "inventory" / "opw-prod.json"
            self.assertTrue(inventory_file.exists())
            persisted_payload = inventory_file.read_text(encoding="utf-8")
            self.assertIn('"artifact_id": "artifact-sha256-image456"', persisted_payload)
            self.assertIn('"deployment_record_id": "deployment-', persisted_payload)
            self.assertIn('canonical odoo-ai platform update workflow', persisted_payload)

    def test_promote_compatibility_execute_persists_inventory_with_promotion_linkage(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            input_file = repo_root / "promotion-request.json"
            input_file.write_text(
                CompatibilityPromotionRequest(
                    artifact_id="compatibility-opw-abc123",
                    source_git_ref="abc123",
                    context="opw",
                    from_instance="testing",
                    to_instance="prod",
                    target_name="opw-prod",
                    target_type="compose",
                    deploy_mode="dokploy-compose-api",
                    wait=True,
                    verify_health=True,
                    health_timeout_seconds=45,
                    source_health={
                        "verified": True,
                        "urls": ["https://testing.example.com/web/health"],
                        "timeout_seconds": 30,
                        "status": "pass",
                    },
                    backup_gate={"required": True, "status": "pass", "evidence": {"snapshot": "snap-1"}},
                    destination_health={
                        "verified": False,
                        "urls": ["https://prod.example.com/web/health"],
                        "timeout_seconds": 45,
                        "status": "pending",
                    },
                ).model_dump_json(indent=2),
                encoding="utf-8",
            )

            with patch(
                "control_plane.cli._export_ship_request_via_odoo_ai",
                return_value=CompatibilityShipRequest(
                    context="opw",
                    instance="prod",
                    source_git_ref="abc123",
                    target_name="opw-prod",
                    target_type="compose",
                    deploy_mode="dokploy-compose-api",
                    artifact_id="artifact-sha256-image456",
                    wait=True,
                    verify_health=True,
                    destination_health={
                        "verified": False,
                        "urls": ["https://prod.example.com/web/health"],
                        "timeout_seconds": 45,
                        "status": "pending",
                    },
                ),
            ), patch(
                "control_plane.cli._execute_compatibility_ship",
                return_value=(
                    state_dir / "deployments" / "deployment-1.json",
                    DeploymentRecord(
                        record_id="deployment-1",
                        artifact_identity={"artifact_id": "artifact-sha256-image456"},
                        context="opw",
                        instance="prod",
                        source_git_ref="abc123",
                        delegated_executor="control-plane.dokploy",
                        deploy={
                            "target_name": "opw-prod",
                            "target_type": "compose",
                            "deploy_mode": "dokploy-compose-api",
                            "deployment_id": "control-plane-dokploy",
                            "status": "pass",
                            "started_at": "2026-04-10T18:22:31Z",
                            "finished_at": "2026-04-10T18:24:00Z",
                        },
                        post_deploy_update={
                            "attempted": True,
                            "status": "pass",
                            "detail": "Odoo-specific post-deploy update completed through the canonical odoo-ai platform update workflow.",
                        },
                        destination_health={
                            "verified": True,
                            "urls": ["https://prod.example.com/web/health"],
                            "timeout_seconds": 45,
                            "status": "pass",
                        },
                    ),
                ),
            ):
                result = runner.invoke(
                    main,
                    [
                        "promote",
                        "compatibility-execute",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                        "--odoo-ai-root",
                        str(repo_root),
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            inventory_file = state_dir / "inventory" / "opw-prod.json"
            self.assertTrue(inventory_file.exists())
            persisted_payload = inventory_file.read_text(encoding="utf-8")
            self.assertIn('"artifact_id": "artifact-sha256-image456"', persisted_payload)
            self.assertIn('"promotion_record_id": "promotion-', persisted_payload)
            self.assertIn('"promoted_from_instance": "testing"', persisted_payload)

    def test_ship_compatibility_plan_validates_request(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            input_file = repo_root / "ship-request.json"
            input_file.write_text(
                CompatibilityShipRequest(
                    context="opw",
                    instance="prod",
                    source_git_ref="abc123",
                    target_name="opw-prod",
                    target_type="compose",
                    deploy_mode="dokploy-compose-api",
                    destination_health={
                        "verified": False,
                        "urls": ["https://prod.example.com/web/health"],
                        "timeout_seconds": 45,
                        "status": "pending",
                    },
                ).model_dump_json(indent=2),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "ship",
                    "compatibility-plan",
                    "--input-file",
                    str(input_file),
                ],
            )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn('"source_git_ref": "abc123"', result.output)

    def test_ship_compatibility_execute_prefers_stored_artifact_manifest_for_commit(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            artifact_dir = state_dir / "artifacts"
            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / "artifact-sha256-image456.json").write_text(
                json.dumps(
                    {
                        "artifact_id": "artifact-sha256-image456",
                        "odoo_ai_commit": "abc123",
                        "enterprise_base_digest": "sha256:enterprise123",
                        "image": {
                            "repository": "ghcr.io/cbusillo/odoo-private",
                            "digest": "sha256:image456",
                            "tags": ["sha-abc123"],
                        },
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            input_file = repo_root / "ship-request.json"
            input_file.write_text(
                CompatibilityShipRequest(
                    context="opw",
                    instance="prod",
                    source_git_ref="abc123",
                    target_name="opw-prod",
                    target_type="compose",
                    deploy_mode="dokploy-compose-api",
                    dry_run=True,
                ).model_dump_json(indent=2),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "ship",
                    "compatibility-execute",
                    "--state-dir",
                    str(state_dir),
                    "--input-file",
                    str(input_file),
                    "--odoo-ai-root",
                    str(repo_root),
                ],
            )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn('"artifact_id": "artifact-sha256-image456"', result.output)

    def test_ship_compatibility_execute_runs_dokploy_in_control_plane(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            platform_dir = repo_root / "platform"
            platform_dir.mkdir(parents=True, exist_ok=True)
            (platform_dir / "dokploy.toml").write_text(
                """
schema_version = 2

[defaults]
target_type = "compose"

[[targets]]
context = "opw"
instance = "prod"
target_id = "compose-123"
""".strip(),
                encoding="utf-8",
            )
            input_file = repo_root / "ship-request.json"
            input_file.write_text(
                CompatibilityShipRequest(
                    context="opw",
                    instance="prod",
                    source_git_ref="abc123",
                    target_name="opw-prod",
                    target_type="compose",
                    deploy_mode="dokploy-compose-api",
                    wait=True,
                    verify_health=False,
                    branch_sync={
                        "source_git_ref": "origin/opw-prod",
                        "source_commit": "abc123",
                        "target_branch": "prod",
                            "remote_branch_commit_before": "def456",
                            "branch_update_required": True,
                            "applied": False,
                        },
                    ).model_dump_json(indent=2),
                encoding="utf-8",
            )
            captured_commands: list[list[str]] = []

            with patch(
                "control_plane.cli._run_command",
                side_effect=lambda command, cwd=None: captured_commands.append(command),
            ), patch(
                "control_plane.cli._sync_artifact_image_reference_for_target",
                side_effect=lambda **_kwargs: None,
            ), patch(
                "control_plane.cli._execute_dokploy_deploy",
                side_effect=lambda **_kwargs: None,
            ):
                result = runner.invoke(
                    main,
                    [
                        "ship",
                        "compatibility-execute",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                        "--odoo-ai-root",
                        str(repo_root),
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertEqual(len(captured_commands), 2)
            self.assertEqual(captured_commands[0][:3], ["git", "push", "origin"])
            self.assertIn("update", captured_commands[1])
            deployment_files = sorted((state_dir / "deployments").glob("*.json"))
            self.assertEqual(len(deployment_files), 1)
            persisted_payload = deployment_files[0].read_text(encoding="utf-8")
            self.assertIn('"status": "pass"', persisted_payload)
            self.assertIn('"delegated_executor": "control-plane.dokploy"', persisted_payload)
            self.assertIn('"deployment_id": "control-plane-dokploy"', persisted_payload)
            self.assertIn('"source_commit": "abc123"', persisted_payload)
            self.assertIn('"applied": true', persisted_payload)
            self.assertIn('"target_id": "compose-123"', persisted_payload)

    def test_ship_compatibility_execute_resolves_artifact_from_stored_manifest(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            artifact_dir = state_dir / "artifacts"
            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / "artifact-sha256-image456.json").write_text(
                json.dumps(
                    {
                        "artifact_id": "artifact-sha256-image456",
                        "odoo_ai_commit": "abc123",
                        "enterprise_base_digest": "sha256:enterprise123",
                        "image": {
                            "repository": "ghcr.io/cbusillo/odoo-private",
                            "digest": "sha256:image456",
                            "tags": ["sha-abc123"],
                        },
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            platform_dir = repo_root / "platform"
            platform_dir.mkdir(parents=True, exist_ok=True)
            (platform_dir / "dokploy.toml").write_text(
                """
schema_version = 2

[defaults]
target_type = "compose"

[[targets]]
context = "opw"
instance = "prod"
target_id = "compose-123"
""".strip(),
                encoding="utf-8",
            )
            input_file = repo_root / "ship-request.json"
            input_file.write_text(
                CompatibilityShipRequest(
                    context="opw",
                    instance="prod",
                    source_git_ref="abc123",
                    target_name="opw-prod",
                    target_type="compose",
                    deploy_mode="dokploy-compose-api",
                    wait=True,
                    verify_health=False,
                ).model_dump_json(indent=2),
                encoding="utf-8",
            )

            captured_sync_calls: list[dict[str, object]] = []

            with patch(
                "control_plane.cli._sync_artifact_image_reference_for_target",
                side_effect=lambda **kwargs: captured_sync_calls.append(kwargs),
            ), patch(
                "control_plane.cli._execute_dokploy_deploy",
                side_effect=lambda **_kwargs: None,
            ), patch(
                "control_plane.cli._run_post_deploy_update_via_odoo_ai",
                side_effect=lambda **_kwargs: None,
            ):
                result = runner.invoke(
                    main,
                    [
                        "ship",
                        "compatibility-execute",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                        "--odoo-ai-root",
                        str(repo_root),
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertEqual(len(captured_sync_calls), 1)
            self.assertEqual(
                captured_sync_calls[0]["artifact_manifest"].artifact_id,
                "artifact-sha256-image456",
            )
            deployment_files = sorted((state_dir / "deployments").glob("*.json"))
            self.assertEqual(len(deployment_files), 1)
            persisted_payload = deployment_files[0].read_text(encoding="utf-8")
            self.assertIn('"artifact_id": "artifact-sha256-image456"', persisted_payload)

    def test_ship_compatibility_execute_runs_health_verification_from_control_plane(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            platform_dir = repo_root / "platform"
            platform_dir.mkdir(parents=True, exist_ok=True)
            (platform_dir / "dokploy.toml").write_text(
                """
schema_version = 2

[defaults]
target_type = "compose"

[[targets]]
context = "opw"
instance = "prod"
target_id = "compose-123"
""".strip(),
                encoding="utf-8",
            )
            input_file = repo_root / "ship-request.json"
            input_file.write_text(
                CompatibilityShipRequest(
                    context="opw",
                    instance="prod",
                    source_git_ref="abc123",
                    target_name="opw-prod",
                    target_type="compose",
                    deploy_mode="dokploy-compose-api",
                    wait=True,
                    verify_health=True,
                    destination_health={
                        "verified": False,
                        "urls": ["https://prod.example.com/web/health"],
                        "timeout_seconds": 45,
                        "status": "pending",
                    },
                ).model_dump_json(indent=2),
                encoding="utf-8",
            )
            captured_commands: list[list[str]] = []
            captured_health_urls: list[str] = []

            with patch(
                "control_plane.cli._run_command",
                side_effect=lambda command, cwd=None: captured_commands.append(command),
            ), patch(
                "control_plane.cli._sync_artifact_image_reference_for_target",
                side_effect=lambda **_kwargs: None,
            ), patch(
                "control_plane.cli._execute_dokploy_deploy",
                side_effect=lambda **_kwargs: None,
            ), patch(
                "control_plane.cli._wait_for_ship_healthcheck",
                side_effect=lambda url, timeout_seconds: captured_health_urls.append(url),
            ):
                result = runner.invoke(
                    main,
                    [
                        "ship",
                        "compatibility-execute",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                        "--odoo-ai-root",
                        str(repo_root),
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertEqual(len(captured_commands), 1)
            self.assertIn("update", captured_commands[0])
            self.assertEqual(captured_health_urls, ["https://prod.example.com/web/health"])

    def test_ship_compatibility_execute_marks_record_failed_when_health_verification_fails(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            input_file = repo_root / "ship-request.json"
            input_file.write_text(
                CompatibilityShipRequest(
                    context="opw",
                    instance="prod",
                    source_git_ref="abc123",
                    target_name="opw-prod",
                    target_type="compose",
                    deploy_mode="dokploy-compose-api",
                    wait=True,
                    verify_health=True,
                    destination_health={
                        "verified": False,
                        "urls": ["https://prod.example.com/web/health"],
                        "timeout_seconds": 45,
                        "status": "pending",
                    },
                ).model_dump_json(indent=2),
                encoding="utf-8",
            )

            with patch(
                "control_plane.cli._run_command",
                side_effect=lambda command, cwd=None: None,
            ), patch(
                "control_plane.cli._resolve_dokploy_target",
                return_value=(
                    ResolvedTargetEvidence(target_type="compose", target_id="compose-123", target_name="opw-prod"),
                    600,
                ),
            ), patch(
                "control_plane.cli._sync_artifact_image_reference_for_target",
                side_effect=lambda **_kwargs: None,
            ), patch(
                "control_plane.cli._execute_dokploy_deploy",
                side_effect=lambda **_kwargs: None,
            ), patch(
                "control_plane.cli._wait_for_ship_healthcheck",
                side_effect=click.ClickException("Healthcheck failed"),
            ):
                result = runner.invoke(
                    main,
                    [
                        "ship",
                        "compatibility-execute",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                        "--odoo-ai-root",
                        str(repo_root),
                    ],
                )

            self.assertNotEqual(result.exit_code, 0)
            deployment_files = sorted((state_dir / "deployments").glob("*.json"))
            self.assertEqual(len(deployment_files), 1)
            persisted_payload = deployment_files[0].read_text(encoding="utf-8")
            self.assertIn('"status": "fail"', persisted_payload)
            self.assertIn('"post_deploy_update": {', persisted_payload)
            self.assertIn('"attempted": true', persisted_payload)
            self.assertIn('"status": "fail"', persisted_payload)
            self.assertIn('canonical odoo-ai platform update workflow', persisted_payload)

    def test_ship_compatibility_execute_persists_failed_deployment_record(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            input_file = repo_root / "ship-request.json"
            input_file.write_text(
                CompatibilityShipRequest(
                    context="opw",
                    instance="prod",
                    source_git_ref="abc123",
                    target_name="opw-prod",
                    target_type="compose",
                    deploy_mode="dokploy-compose-api",
                    wait=True,
                    verify_health=True,
                    destination_health={
                        "verified": False,
                        "urls": ["https://prod.example.com/web/health"],
                        "timeout_seconds": 45,
                        "status": "pending",
                    },
                ).model_dump_json(indent=2),
                encoding="utf-8",
            )

            with patch(
                "control_plane.cli._resolve_dokploy_target",
                return_value=(
                    ResolvedTargetEvidence(target_type="compose", target_id="compose-123", target_name="opw-prod"),
                    600,
                ),
            ), patch(
                "control_plane.cli._sync_artifact_image_reference_for_target",
                side_effect=lambda **_kwargs: None,
            ), patch(
                "control_plane.cli._execute_dokploy_deploy",
                side_effect=subprocess.CalledProcessError(1, ["uv", "run"]),
            ):
                result = runner.invoke(
                    main,
                    [
                        "ship",
                        "compatibility-execute",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                        "--odoo-ai-root",
                        str(repo_root),
                    ],
                )

            self.assertNotEqual(result.exit_code, 0)
            deployment_files = sorted((state_dir / "deployments").glob("*.json"))
            self.assertEqual(len(deployment_files), 1)
            persisted_payload = deployment_files[0].read_text(encoding="utf-8")
            self.assertIn('"status": "fail"', persisted_payload)
            self.assertIn('"finished_at": "', persisted_payload)

    def test_ship_compatibility_execute_fails_closed_when_branch_sync_apply_fails(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            input_file = repo_root / "ship-request.json"
            input_file.write_text(
                CompatibilityShipRequest(
                    context="opw",
                    instance="prod",
                    source_git_ref="origin/opw-prod",
                    target_name="opw-prod",
                    target_type="compose",
                    deploy_mode="dokploy-compose-api",
                    branch_sync={
                        "source_git_ref": "origin/opw-prod",
                        "source_commit": "abc123",
                        "target_branch": "prod",
                        "remote_branch_commit_before": "def456",
                        "branch_update_required": True,
                    },
                ).model_dump_json(indent=2),
                encoding="utf-8",
            )
            captured_commands: list[list[str]] = []

            def capture_and_fail(command: list[str], cwd: Path | None = None) -> None:
                captured_commands.append(command)
                if command[:3] == ["git", "push", "origin"]:
                    raise subprocess.CalledProcessError(1, command)

            with patch("control_plane.cli._run_command", side_effect=capture_and_fail):
                result = runner.invoke(
                    main,
                    [
                        "ship",
                        "compatibility-execute",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                        "--odoo-ai-root",
                        str(repo_root),
                    ],
                )

            self.assertNotEqual(result.exit_code, 0)
            self.assertEqual(len(captured_commands), 1)
            self.assertEqual(captured_commands[0][:3], ["git", "push", "origin"])
            deployment_files = sorted((state_dir / "deployments").glob("*.json"))
            self.assertEqual(len(deployment_files), 1)
            persisted_payload = deployment_files[0].read_text(encoding="utf-8")
            self.assertIn('"status": "fail"', persisted_payload)
            self.assertIn('"applied": false', persisted_payload)

    def test_ship_compatibility_execute_fails_closed_when_target_resolution_fails(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            input_file = repo_root / "ship-request.json"
            input_file.write_text(
                CompatibilityShipRequest(
                    context="opw",
                    instance="prod",
                    source_git_ref="abc123",
                    target_name="opw-prod",
                    target_type="compose",
                    deploy_mode="dokploy-compose-api",
                ).model_dump_json(indent=2),
                encoding="utf-8",
            )

            with patch(
                "control_plane.cli._resolve_dokploy_target",
                side_effect=click.ClickException("Target resolution failed"),
            ):
                result = runner.invoke(
                    main,
                    [
                        "ship",
                        "compatibility-execute",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                        "--odoo-ai-root",
                        str(repo_root),
                    ],
                )

            self.assertNotEqual(result.exit_code, 0)
            deployment_files = sorted((state_dir / "deployments").glob("*.json"))
            self.assertEqual(len(deployment_files), 1)
            persisted_payload = deployment_files[0].read_text(encoding="utf-8")
            self.assertIn('"status": "fail"', persisted_payload)

    def test_resolve_dokploy_target_reads_target_id_and_timeout_from_source_of_truth(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            platform_dir = repo_root / "platform"
            platform_dir.mkdir(parents=True, exist_ok=True)
            (platform_dir / "dokploy.toml").write_text(
                """
schema_version = 2

[profiles.opw]
target_type = "compose"
deploy_timeout_seconds = 7200

[[targets]]
profile = "opw"
context = "opw"
instance = "prod"
target_id = "compose-123"
""".strip(),
                encoding="utf-8",
            )

            resolved_target, deploy_timeout_seconds = _resolve_dokploy_target(
                odoo_ai_root=repo_root,
                request=CompatibilityShipRequest(
                    context="opw",
                    instance="prod",
                    source_git_ref="abc123",
                    target_name="opw-prod",
                    target_type="compose",
                    deploy_mode="dokploy-compose-api",
                ),
            )

        self.assertEqual(resolved_target.target_id, "compose-123")
        self.assertEqual(resolved_target.target_name, "opw-prod")
        self.assertEqual(deploy_timeout_seconds, 7200)


if __name__ == "__main__":
    unittest.main()
