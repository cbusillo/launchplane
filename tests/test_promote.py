import os
import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import click
from click.testing import CliRunner

from control_plane import dokploy as control_plane_dokploy
from control_plane import runtime_environments as control_plane_runtime_environments
from control_plane.cli import (
    _resolve_dokploy_target,
    _resolve_native_promotion_request,
    _resolve_native_ship_request,
    _sync_artifact_image_reference_for_target,
    main,
)
from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.deployment_record import DeploymentRecord, ResolvedTargetEvidence
from control_plane.contracts.promotion_record import DeploymentEvidence
from control_plane.contracts.promotion_record import PromotionRecord
from control_plane.contracts.promotion_record import PromotionRequest
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.contracts.ship_request import ShipRequest
from control_plane.workflows.ship import build_deployment_record
from control_plane.workflows.promote import build_promotion_record
from control_plane.workflows.promote import build_executed_promotion_record


def _write_artifact_manifest(
    state_dir: Path,
    *,
    artifact_id: str = "artifact-sha256-image456",
    source_commit: str = "abc1234",
) -> None:
    artifact_dir = state_dir / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / f"{artifact_id}.json").write_text(
        json.dumps(
            {
                "artifact_id": artifact_id,
                "source_commit": source_commit,
                "enterprise_base_digest": "sha256:enterprise123",
                "image": {
                    "repository": "ghcr.io/cbusillo/odoo-private",
                    "digest": "sha256:image456",
                    "tags": [f"sha-{source_commit}"],
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _write_release_tuple_record(
    state_dir: Path,
    *,
    context: str = "opw",
    channel: str = "testing",
    artifact_id: str = "artifact-sha256-image456",
    tenant_sha: str = "abc1234",
) -> None:
    release_tuple_dir = state_dir / "release_tuples"
    release_tuple_dir.mkdir(parents=True, exist_ok=True)
    (release_tuple_dir / f"{context}-{channel}.json").write_text(
        ReleaseTupleRecord(
            tuple_id=f"{context}-{channel}-{artifact_id}",
            context=context,
            channel=channel,
            artifact_id=artifact_id,
            repo_shas={f"tenant-{context}": tenant_sha},
            image_repository="ghcr.io/cbusillo/odoo-private",
            image_digest="sha256:image456",
            deployment_record_id="deployment-testing-1",
            provenance="ship",
            minted_at="2026-04-10T18:24:00Z",
        ).model_dump_json(indent=2),
        encoding="utf-8",
    )


def _write_backup_gate_record(
    state_dir: Path,
    *,
    record_id: str = "backup-opw-prod-20260410T182231Z",
    context: str = "opw",
    instance: str = "prod",
    status: str = "pass",
) -> None:
    backup_gate_dir = state_dir / "backup_gates"
    backup_gate_dir.mkdir(parents=True, exist_ok=True)
    (backup_gate_dir / f"{record_id}.json").write_text(
        BackupGateRecord(
            record_id=record_id,
            context=context,
            instance=instance,
            created_at="2026-04-10T18:22:31Z",
            source="prod-gate",
            status=status,
            evidence={
                "snapshot": "opw-predeploy-20260410-182231",
                "storage": "pbs",
            }
            if status == "pass"
            else {},
        ).model_dump_json(indent=2),
        encoding="utf-8",
    )


def _write_control_plane_dokploy_source_of_truth(repo_root: Path, payload: str) -> Path:
    source_file = repo_root / "config" / "dokploy.toml"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text(payload.strip(), encoding="utf-8")
    return source_file


class PromoteWorkflowTests(unittest.TestCase):
    def test_build_promotion_record_returns_pending_record(self) -> None:
        record = build_promotion_record(record_id="promotion-20260410-182231-opw-testing-prod",
                                        artifact_id="artifact-20260410-f45db648", context_name="opw",
                                        from_instance_name="testing", to_instance_name="prod", target_name="opw-prod",
                                        target_type="compose", deploy_mode="dokploy-compose-api")

        self.assertEqual(record.artifact_identity.artifact_id, "artifact-20260410-f45db648")
        self.assertEqual(record.deploy.status, "pending")
        self.assertEqual(record.deploy.target_name, "opw-prod")
        self.assertEqual(record.from_instance, "testing")

    def test_build_executed_promotion_record_marks_success_after_waited_ship(self) -> None:
        request = PromotionRequest(artifact_id="artifact-sha256-image456",
                                   backup_record_id="backup-opw-prod-20260410T182231Z", source_git_ref="abc123",
                                   context="opw", from_instance="testing", to_instance="prod", target_name="opw-prod",
                                   target_type="compose", deploy_mode="dokploy-compose-api", destination_health={
                "verified": False,
                "urls": ["https://prod.example.com/web/health"],
                "timeout_seconds": 45,
                "status": "pending",
            }, source_health={
                "verified": True,
                "urls": ["https://testing.example.com/web/health"],
                "timeout_seconds": 30,
                "status": "pass",
            }, backup_gate={"required": True, "status": "pass", "evidence": {"snapshot": "snap-1"}})

        record = build_executed_promotion_record(
            request=request,
            record_id="promotion-1",
            deployment_record_id="deployment-1",
            deployment_id="delegated-ship",
            deployment_status="pass",
        )

        self.assertEqual(record.deploy.status, "pass")
        self.assertEqual(record.deployment_record_id, "deployment-1")
        self.assertEqual(record.backup_record_id, "backup-opw-prod-20260410T182231Z")
        self.assertTrue(record.destination_health.verified)
        self.assertEqual(record.destination_health.status, "pass")
        self.assertTrue(record.post_deploy_update.attempted)
        self.assertEqual(record.post_deploy_update.status, "pass")

    def test_promotion_record_requires_backup_record_id_for_passing_backup_gate(self) -> None:
        with self.assertRaisesRegex(ValueError, "backup_record_id"):
            PromotionRecord(
                record_id="promotion-1",
                artifact_identity={"artifact_id": "artifact-sha256-image456"},
                context="opw",
                from_instance="testing",
                to_instance="prod",
                backup_gate={"required": True, "status": "pass", "evidence": {"snapshot": "snap-1"}},
                deploy=DeploymentEvidence(
                    target_name="opw-prod",
                    target_type="compose",
                    deploy_mode="dokploy-compose-api",
                ),
            )

    def test_build_deployment_record_marks_pending_health_for_async_ship(self) -> None:
        request = ShipRequest(context="opw", instance="prod", source_git_ref="abc123", target_name="opw-prod",
                              target_type="compose", deploy_mode="dokploy-compose-api",
                              artifact_id="artifact-sha256-image456", wait=False, destination_health={
                "verified": False,
                "urls": ["https://prod.example.com/web/health"],
                "timeout_seconds": 45,
                "status": "pending",
            })

        record = build_deployment_record(
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

    def test_build_deployment_record_marks_post_deploy_update_success_for_waited_compose_ship(self) -> None:
        request = ShipRequest(context="opw", instance="prod", source_git_ref="abc123",
                              artifact_id="artifact-sha256-image456", target_name="opw-prod", target_type="compose",
                              deploy_mode="dokploy-compose-api", destination_health={
                "verified": False,
                "urls": ["https://prod.example.com/web/health"],
                "timeout_seconds": 45,
                "status": "pending",
            })

        record = build_deployment_record(
            request=request,
            record_id="deployment-compose-pass",
            deployment_id="control-plane-dokploy",
            deployment_status="pass",
            started_at="2026-04-10T18:22:31Z",
            finished_at="2026-04-10T18:24:00Z",
        )

        self.assertTrue(record.post_deploy_update.attempted)
        self.assertEqual(record.post_deploy_update.status, "pass")
        self.assertIn("native control-plane Dokploy schedule workflow", record.post_deploy_update.detail)


class ArtifactImageOverrideTests(unittest.TestCase):
    def test_sync_artifact_image_reference_sets_exact_image_reference(self) -> None:
        resolved_target = ResolvedTargetEvidence(target_type="compose", target_id="compose-123", target_name="opw-prod")
        artifact_manifest = ArtifactIdentityManifest.model_validate(
            {
                "artifact_id": "artifact-sha256-image456",
                "source_commit": "abc1234",
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
            return_value={"env": "DOCKER_IMAGE=odoo-runtime\nDOCKER_IMAGE_TAG=latest"},
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

    def test_sync_artifact_image_reference_rejects_legacy_monorepo_target_source(self) -> None:
        resolved_target = ResolvedTargetEvidence(target_type="compose", target_id="compose-123", target_name="opw-testing")
        artifact_manifest = ArtifactIdentityManifest.model_validate(
            {
                "artifact_id": "artifact-sha256-image456",
                "source_commit": "abc1234",
                "enterprise_base_digest": "sha256:enterprise123",
                "image": {
                    "repository": "ghcr.io/cbusillo/odoo-private",
                    "digest": "sha256:image456",
                    "tags": ["sha-abc123"],
                },
            }
        )

        with patch(
            "control_plane.dokploy.read_dokploy_config",
            return_value=("https://dokploy.example.com", "token-123"),
        ), patch(
            "control_plane.dokploy.fetch_dokploy_target_payload",
            return_value={
                "sourceType": "git",
                "customGitUrl": "git@github.com:cbusillo/odoo-ai.git",
                "customGitBranch": "opw-testing",
                "env": "DOCKER_IMAGE=odoo-runtime\nDOCKER_IMAGE_TAG=latest",
            },
        ):
            with self.assertRaises(click.ClickException) as error_context:
                _sync_artifact_image_reference_for_target(
                    artifact_manifest=artifact_manifest,
                    resolved_target=resolved_target,
                )

        self.assertIn("legacy monorepo Dokploy source", str(error_context.exception))
        self.assertIn("odoo-ai", str(error_context.exception))

    def test_sync_artifact_image_reference_rejects_mutable_addon_refs(self) -> None:
        resolved_target = ResolvedTargetEvidence(target_type="compose", target_id="compose-123", target_name="opw-testing")
        artifact_manifest = ArtifactIdentityManifest.model_validate(
            {
                "artifact_id": "artifact-sha256-image456",
                "source_commit": "abc1234",
                "enterprise_base_digest": "sha256:enterprise123",
                "image": {
                    "repository": "ghcr.io/cbusillo/odoo-private",
                    "digest": "sha256:image456",
                    "tags": ["sha-abc123"],
                },
            }
        )

        with patch(
            "control_plane.dokploy.read_dokploy_config",
            return_value=("https://dokploy.example.com", "token-123"),
        ), patch(
            "control_plane.dokploy.fetch_dokploy_target_payload",
            return_value={
                "env": (
                    "DOCKER_IMAGE=odoo-runtime\n"
                    "DOCKER_IMAGE_TAG=latest\n"
                    "ODOO_ADDON_REPOSITORIES=cbusillo/disable_odoo_online@main,OCA/OpenUpgrade@89e649728027a8ab656b3aa4be18f4bd364db417"
                )
            },
        ):
            with self.assertRaises(click.ClickException) as error_context:
                _sync_artifact_image_reference_for_target(
                    artifact_manifest=artifact_manifest,
                    resolved_target=resolved_target,
                )

        self.assertIn("exact git SHAs", str(error_context.exception))
        self.assertIn("disable_odoo_online@main", str(error_context.exception))

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
                    "DOCKER_IMAGE=odoo-runtime\n"
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
        self.assertIn("DOCKER_IMAGE=odoo-runtime", str(captured_update["env_text"]))

    def test_ship_artifact_manifest_executes_without_git_push(self) -> None:
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
                        "source_commit": "abc1234",
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
            source_file = _write_control_plane_dokploy_source_of_truth(
                repo_root,
                """
schema_version = 2

[defaults]
target_type = "compose"

[[targets]]
context = "opw"
instance = "prod"
target_id = "compose-123"
""",
            )
            input_file = repo_root / "ship-request.json"
            input_file.write_text(
                ShipRequest(artifact_id="artifact-sha256-image456", context="opw", instance="prod",
                            source_git_ref="abc123", target_name="opw-prod", target_type="compose",
                            deploy_mode="dokploy-compose-api", verify_health=False).model_dump_json(indent=2),
                encoding="utf-8",
            )
            post_deploy_updates: list[dict[str, object]] = []

            with patch.dict(os.environ,
                            {control_plane_dokploy.CONTROL_PLANE_DOKPLOY_SOURCE_FILE_ENV_VAR: str(source_file)}), patch(
                "control_plane.cli._run_compose_post_deploy_update",
                side_effect=lambda **kwargs: post_deploy_updates.append(kwargs),
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
                        "execute",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertEqual(len(post_deploy_updates), 1)
            deployment_files = sorted((state_dir / "deployments").glob("*.json"))
            self.assertEqual(len(deployment_files), 1)
            persisted_payload = deployment_files[0].read_text(encoding="utf-8")
            self.assertIn('"artifact_id": "artifact-sha256-image456"', persisted_payload)
            self.assertNotIn('"branch_sync"', persisted_payload)


class PromoteCliTests(unittest.TestCase):
    def test_promote_execute_persists_record_and_executes_control_plane_ship(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            _write_artifact_manifest(state_dir)
            _write_backup_gate_record(state_dir)
            _write_release_tuple_record(state_dir)
            input_file = repo_root / "promotion-request.json"
            input_file.write_text(
                PromotionRequest(artifact_id="artifact-sha256-image456",
                                 backup_record_id="backup-opw-prod-20260410T182231Z", source_git_ref="abc123",
                                 context="opw", from_instance="testing", to_instance="prod", target_name="opw-prod",
                                 target_type="compose", deploy_mode="dokploy-compose-api", health_timeout_seconds=45,
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
                                 }).model_dump_json(indent=2),
                encoding="utf-8",
            )
            with patch(
                "control_plane.cli._resolve_ship_request_for_promotion",
                return_value=ShipRequest(artifact_id="artifact-sha256-image456", context="opw", instance="prod",
                                         source_git_ref="abc123", target_name="opw-prod", target_type="compose",
                                         deploy_mode="dokploy-compose-api", destination_health={
                        "verified": False,
                        "urls": ["https://prod.example.com/web/health"],
                        "timeout_seconds": 45,
                        "status": "pending",
                    }),
            ), patch(
                "control_plane.cli._execute_ship",
                return_value=(
                    state_dir / "deployments" / "deployment-1.json",
                    DeploymentRecord(record_id="deployment-1",
                                     artifact_identity={"artifact_id": "artifact-sha256-image456"}, context="opw",
                                     instance="prod", source_git_ref="abc123", deploy={
                            "target_name": "opw-prod",
                            "target_type": "compose",
                            "deploy_mode": "dokploy-compose-api",
                            "deployment_id": "control-plane-dokploy",
                            "status": "pass",
                            "started_at": "2026-04-10T18:22:31Z",
                            "finished_at": "2026-04-10T18:24:00Z",
                        }),
                ),
            ):
                result = runner.invoke(
                    main,
                    [
                        "promote",
                        "execute",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            promotion_files = sorted((state_dir / "promotions").glob("*.json"))
            self.assertEqual(len(promotion_files), 1)
            persisted_payload = promotion_files[0].read_text(encoding="utf-8")
            self.assertIn('"status": "pass"', persisted_payload)
            self.assertIn('"artifact_id": "artifact-sha256-image456"', persisted_payload)
            self.assertIn('"backup_record_id": "backup-opw-prod-20260410T182231Z"', persisted_payload)
            self.assertIn('"deployment_id": "control-plane-dokploy"', persisted_payload)
            self.assertIn('"snapshot": "opw-predeploy-20260410-182231"', persisted_payload)

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
                            "detail": "Odoo-specific post-deploy update completed through the native control-plane Dokploy schedule workflow.",
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

    def test_inventory_status_reads_live_state_and_authorizing_backup(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            inventory_dir = state_dir / "inventory"
            promotion_dir = state_dir / "promotions"
            inventory_dir.mkdir(parents=True, exist_ok=True)
            promotion_dir.mkdir(parents=True, exist_ok=True)
            _write_backup_gate_record(state_dir)
            (promotion_dir / "promotion-1.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "record_id": "promotion-1",
                        "artifact_identity": {"artifact_id": "artifact-sha256-image456", "manifest_version": 1},
                        "backup_record_id": "backup-opw-prod-20260410T182231Z",
                        "context": "opw",
                        "from_instance": "testing",
                        "to_instance": "prod",
                        "backup_gate": {
                            "required": True,
                            "status": "pass",
                            "evidence": {"snapshot": "opw-predeploy-20260410-182231"},
                        },
                        "deploy": {
                            "target_name": "opw-prod",
                            "target_type": "compose",
                            "deploy_mode": "dokploy-compose-api",
                            "deployment_id": "control-plane-dokploy",
                            "status": "pass",
                            "started_at": "2026-04-10T18:22:31Z",
                            "finished_at": "2026-04-10T18:24:00Z",
                        },
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
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
                            "detail": "Odoo-specific post-deploy update completed through the native control-plane Dokploy schedule workflow.",
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
            deployment_dir = state_dir / "deployments"
            deployment_dir.mkdir(parents=True, exist_ok=True)
            (deployment_dir / "deployment-1.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "record_id": "deployment-1",
                        "artifact_identity": {"artifact_id": "artifact-sha256-image456", "manifest_version": 1},
                        "context": "opw",
                        "instance": "prod",
                        "source_git_ref": "abc123",
                        "resolved_target": {
                            "target_type": "compose",
                            "target_id": "compose-123",
                            "target_name": "opw-prod",
                        },
                        "deploy": {
                            "target_name": "opw-prod",
                            "target_type": "compose",
                            "deploy_mode": "dokploy-compose-api",
                            "deployment_id": "control-plane-dokploy",
                            "status": "pass",
                            "started_at": "2026-04-10T18:22:31Z",
                            "finished_at": "2026-04-10T18:24:00Z",
                        },
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "inventory",
                    "status",
                    "--state-dir",
                    str(state_dir),
                    "--context",
                    "opw",
                    "--instance",
                    "prod",
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn('"record_id": "backup-opw-prod-20260410T182231Z"', result.output)
            self.assertIn('"promotion_record_id": "promotion-1"', result.output)
            self.assertIn('"latest_deployment"', result.output)

    def test_inventory_overview_returns_sorted_status_payloads_for_all_environments(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            inventory_dir = state_dir / "inventory"
            deployment_dir = state_dir / "deployments"
            inventory_dir.mkdir(parents=True, exist_ok=True)
            deployment_dir.mkdir(parents=True, exist_ok=True)
            (inventory_dir / "zeta-testing.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "context": "zeta",
                        "instance": "testing",
                        "artifact_identity": {"artifact_id": "artifact-zeta", "manifest_version": 1},
                        "source_git_ref": "zeta-ref",
                        "deploy": {
                            "target_name": "zeta-testing",
                            "target_type": "compose",
                            "deploy_mode": "dokploy-compose-api",
                            "deployment_id": "deployment-zeta",
                            "status": "pass",
                        },
                        "post_deploy_update": {
                            "attempted": True,
                            "status": "pass",
                            "detail": "zeta ready",
                        },
                        "destination_health": {
                            "verified": True,
                            "urls": ["https://zeta.example.com/web/health"],
                            "timeout_seconds": 30,
                            "status": "pass",
                        },
                        "updated_at": "2026-04-11T01:00:00Z",
                        "deployment_record_id": "deployment-zeta",
                        "promotion_record_id": "",
                        "promoted_from_instance": "",
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            (inventory_dir / "acme-prod.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "context": "acme",
                        "instance": "prod",
                        "artifact_identity": {"artifact_id": "artifact-acme", "manifest_version": 1},
                        "source_git_ref": "acme-ref",
                        "deploy": {
                            "target_name": "acme-prod",
                            "target_type": "compose",
                            "deploy_mode": "dokploy-compose-api",
                            "deployment_id": "deployment-acme",
                            "status": "pass",
                        },
                        "post_deploy_update": {
                            "attempted": True,
                            "status": "pass",
                            "detail": "acme ready",
                        },
                        "destination_health": {
                            "verified": True,
                            "urls": ["https://acme.example.com/web/health"],
                            "timeout_seconds": 45,
                            "status": "pass",
                        },
                        "updated_at": "2026-04-11T02:00:00Z",
                        "deployment_record_id": "deployment-acme",
                        "promotion_record_id": "",
                        "promoted_from_instance": "",
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            (deployment_dir / "deployment-zeta.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "record_id": "deployment-zeta",
                        "artifact_identity": {"artifact_id": "artifact-zeta", "manifest_version": 1},
                        "context": "zeta",
                        "instance": "testing",
                        "source_git_ref": "zeta-ref",
                        "resolved_target": {
                            "target_type": "compose",
                            "target_id": "compose-zeta",
                            "target_name": "zeta-testing",
                        },
                        "deploy": {
                            "target_name": "zeta-testing",
                            "target_type": "compose",
                            "deploy_mode": "dokploy-compose-api",
                            "deployment_id": "deployment-zeta",
                            "status": "pass",
                        },
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            (deployment_dir / "deployment-acme.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "record_id": "deployment-acme",
                        "artifact_identity": {"artifact_id": "artifact-acme", "manifest_version": 1},
                        "context": "acme",
                        "instance": "prod",
                        "source_git_ref": "acme-ref",
                        "resolved_target": {
                            "target_type": "compose",
                            "target_id": "compose-acme",
                            "target_name": "acme-prod",
                        },
                        "deploy": {
                            "target_name": "acme-prod",
                            "target_type": "compose",
                            "deploy_mode": "dokploy-compose-api",
                            "deployment_id": "deployment-acme",
                            "status": "pass",
                        },
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "inventory",
                    "overview",
                    "--state-dir",
                    str(state_dir),
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            payload = json.loads(result.output)
            self.assertEqual([(entry["context"], entry["instance"]) for entry in payload], [("acme", "prod"), ("zeta", "testing")])
            self.assertEqual(payload[0]["live"]["artifact_id"], "artifact-acme")
            self.assertEqual(payload[1]["latest_deployment"]["record_id"], "deployment-zeta")

    def test_inventory_overview_filters_to_requested_context(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            inventory_dir = state_dir / "inventory"
            deployment_dir = state_dir / "deployments"
            inventory_dir.mkdir(parents=True, exist_ok=True)
            deployment_dir.mkdir(parents=True, exist_ok=True)
            for context_name, instance_name in (("acme", "prod"), ("zeta", "testing")):
                (inventory_dir / f"{context_name}-{instance_name}.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "context": context_name,
                            "instance": instance_name,
                            "artifact_identity": {
                                "artifact_id": f"artifact-{context_name}",
                                "manifest_version": 1,
                            },
                            "source_git_ref": f"{context_name}-ref",
                            "deploy": {
                                "target_name": f"{context_name}-{instance_name}",
                                "target_type": "compose",
                                "deploy_mode": "dokploy-compose-api",
                                "deployment_id": f"deployment-{context_name}",
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
                            "updated_at": "2026-04-11T03:00:00Z",
                            "deployment_record_id": f"deployment-{context_name}",
                            "promotion_record_id": "",
                            "promoted_from_instance": "",
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                (deployment_dir / f"deployment-{context_name}.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "record_id": f"deployment-{context_name}",
                            "artifact_identity": {
                                "artifact_id": f"artifact-{context_name}",
                                "manifest_version": 1,
                            },
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
                                "deployment_id": f"deployment-{context_name}",
                                "status": "pass",
                            },
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )

            result = runner.invoke(
                main,
                [
                    "inventory",
                    "overview",
                    "--state-dir",
                    str(state_dir),
                    "--context",
                    "zeta",
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            payload = json.loads(result.output)
            self.assertEqual(len(payload), 1)
            self.assertEqual(payload[0]["context"], "zeta")
            self.assertEqual(payload[0]["instance"], "testing")

    def test_promote_execute_prefers_explicit_artifact_id(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            _write_backup_gate_record(state_dir)
            _write_release_tuple_record(state_dir)
            source_file = _write_control_plane_dokploy_source_of_truth(
                repo_root,
                """
schema_version = 2

[[targets]]
context = "opw"
instance = "prod"
target_id = "compose-123"
target_type = "compose"
target_name = "opw-prod"
domains = ["https://prod.example.com"]
""",
            )
            artifact_dir = state_dir / "artifacts"
            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / "artifact-sha256-image456.json").write_text(
                json.dumps(
                    {
                        "artifact_id": "artifact-sha256-image456",
                        "source_commit": "abc1234",
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
                PromotionRequest(
                    artifact_id="artifact-sha256-image456",
                    backup_record_id="backup-opw-prod-20260410T182231Z",
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

            with patch.dict(os.environ,
                            {control_plane_dokploy.CONTROL_PLANE_DOKPLOY_SOURCE_FILE_ENV_VAR: str(source_file)}):
                result = runner.invoke(
                    main,
                    [
                        "promote",
                        "execute",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn('"artifact_id": "artifact-sha256-image456"', result.output)

    def test_promote_record_persists_backup_record_id_when_provided(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"

            result = runner.invoke(
                main,
                [
                    "promote",
                    "record",
                    "--state-dir",
                    str(state_dir),
                    "--record-id",
                    "promotion-1",
                    "--artifact-id",
                    "artifact-sha256-image456",
                    "--backup-record-id",
                    "backup-opw-prod-20260410T182231Z",
                    "--context",
                    "opw",
                    "--from-instance",
                    "testing",
                    "--to-instance",
                    "prod",
                    "--target-name",
                    "opw-prod",
                    "--target-type",
                    "compose",
                    "--deploy-mode",
                    "dokploy-compose-api",
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            persisted_payload = (state_dir / "promotions" / "promotion-1.json").read_text(encoding="utf-8")
            self.assertIn('"backup_record_id": "backup-opw-prod-20260410T182231Z"', persisted_payload)

    def test_backup_gates_list_returns_latest_first(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            _write_backup_gate_record(state_dir)
            _write_backup_gate_record(state_dir, record_id="backup-opw-prod-20260411T182231Z")

            result = runner.invoke(
                main,
                [
                    "backup-gates",
                    "list",
                    "--state-dir",
                    str(state_dir),
                    "--context",
                    "opw",
                    "--instance",
                    "prod",
                    "--limit",
                    "1",
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn('"record_id": "backup-opw-prod-20260411T182231Z"', result.output)
            self.assertNotIn('"record_id": "backup-opw-prod-20260410T182231Z"', result.output)

    def test_promotions_list_returns_latest_first(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            promotion_dir = state_dir / "promotions"
            promotion_dir.mkdir(parents=True, exist_ok=True)
            for record_id, artifact_id, backup_record_id, started_at in (
                (
                    "promotion-20260410T182231Z-opw-testing-to-prod",
                    "artifact-1",
                    "backup-opw-prod-20260410T182231Z",
                    "2026-04-10T18:22:31Z",
                ),
                (
                    "promotion-20260411T182231Z-opw-testing-to-prod",
                    "artifact-2",
                    "backup-opw-prod-20260411T182231Z",
                    "2026-04-11T18:22:31Z",
                ),
            ):
                (promotion_dir / f"{record_id}.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "record_id": record_id,
                            "artifact_identity": {"artifact_id": artifact_id, "manifest_version": 1},
                            "backup_record_id": backup_record_id,
                            "context": "opw",
                            "from_instance": "testing",
                            "to_instance": "prod",
                            "backup_gate": {
                                "required": True,
                                "status": "pass",
                                "evidence": {"snapshot": "snap"},
                            },
                            "deploy": {
                                "target_name": "opw-prod",
                                "target_type": "compose",
                                "deploy_mode": "dokploy-compose-api",
                                "status": "pass",
                                "started_at": started_at,
                                "finished_at": started_at,
                            },
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )

            result = runner.invoke(
                main,
                [
                    "promotions",
                    "list",
                    "--state-dir",
                    str(state_dir),
                    "--context",
                    "opw",
                    "--to-instance",
                    "prod",
                    "--limit",
                    "1",
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn('"artifact_id": "artifact-2"', result.output)
            self.assertIn('"backup_record_id": "backup-opw-prod-20260411T182231Z"', result.output)

    def test_deployments_list_returns_latest_first(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            deployment_dir = state_dir / "deployments"
            deployment_dir.mkdir(parents=True, exist_ok=True)
            for record_id, artifact_id, source_git_ref, started_at in (
                (
                    "deployment-20260410T182231Z-opw-prod",
                    "artifact-1",
                    "abc123",
                    "2026-04-10T18:22:31Z",
                ),
                (
                    "deployment-20260411T182231Z-opw-prod",
                    "artifact-2",
                    "def456",
                    "2026-04-11T18:22:31Z",
                ),
            ):
                (deployment_dir / f"{record_id}.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "record_id": record_id,
                            "artifact_identity": {"artifact_id": artifact_id, "manifest_version": 1},
                            "context": "opw",
                            "instance": "prod",
                            "source_git_ref": source_git_ref,
                            "resolved_target": {
                                "target_type": "compose",
                                "target_id": "compose-123",
                                "target_name": "opw-prod",
                            },
                            "deploy": {
                                "target_name": "opw-prod",
                                "target_type": "compose",
                                "deploy_mode": "dokploy-compose-api",
                                "deployment_id": "control-plane-dokploy",
                                "status": "pass",
                                "started_at": started_at,
                                "finished_at": started_at,
                            },
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )

            result = runner.invoke(
                main,
                [
                    "deployments",
                    "list",
                    "--state-dir",
                    str(state_dir),
                    "--context",
                    "opw",
                    "--instance",
                    "prod",
                    "--limit",
                    "1",
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn('"record_id": "deployment-20260411T182231Z-opw-prod"', result.output)
            self.assertIn('"source_git_ref": "def456"', result.output)

    def test_deployments_write_persists_record(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            input_file = repo_root / "deployment-record.json"
            record = DeploymentRecord(
                record_id="deployment-20260411T182231Z-opw-prod",
                artifact_identity={"artifact_id": "artifact-2", "manifest_version": 1},
                context="opw",
                instance="prod",
                source_git_ref="def456",
                resolved_target=ResolvedTargetEvidence(
                    target_type="compose",
                    target_id="compose-123",
                    target_name="opw-prod",
                ),
                deploy=DeploymentEvidence(
                    target_name="opw-prod",
                    target_type="compose",
                    deploy_mode="dokploy-compose-api",
                    deployment_id="control-plane-dokploy",
                    status="pass",
                    started_at="2026-04-11T18:22:31Z",
                    finished_at="2026-04-11T18:22:31Z",
                ),
            )
            input_file.write_text(record.model_dump_json(indent=2), encoding="utf-8")

            result = runner.invoke(
                main,
                [
                    "deployments",
                    "write",
                    "--state-dir",
                    str(state_dir),
                    "--input-file",
                    str(input_file),
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            written_path = state_dir / "deployments" / "deployment-20260411T182231Z-opw-prod.json"
            self.assertEqual(Path(result.output.strip()), written_path)
            self.assertEqual(
                DeploymentRecord.model_validate(json.loads(written_path.read_text(encoding="utf-8"))),
                record,
            )

    def test_promotions_write_persists_record(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            input_file = repo_root / "promotion-record.json"
            record = PromotionRecord(
                record_id="promotion-20260411T182231Z-opw-testing-to-prod",
                artifact_identity={"artifact_id": "artifact-2", "manifest_version": 1},
                deployment_record_id="deployment-20260411T182231Z-opw-prod",
                backup_record_id="backup-opw-prod-20260410T182231Z",
                context="opw",
                from_instance="testing",
                to_instance="prod",
                backup_gate={"required": True, "status": "pass", "evidence": {"snapshot": "snap-1"}},
                deploy={
                    "target_name": "opw-prod",
                    "target_type": "compose",
                    "deploy_mode": "dokploy-compose-api",
                    "deployment_id": "control-plane-dokploy",
                    "status": "pass",
                    "started_at": "2026-04-11T18:22:31Z",
                    "finished_at": "2026-04-11T18:22:31Z",
                },
                destination_health={
                    "verified": True,
                    "urls": ["https://prod.example.com/web/health"],
                    "timeout_seconds": 45,
                    "status": "pass",
                },
            )
            input_file.write_text(record.model_dump_json(indent=2), encoding="utf-8")

            result = runner.invoke(
                main,
                [
                    "promotions",
                    "write",
                    "--state-dir",
                    str(state_dir),
                    "--input-file",
                    str(input_file),
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            written_path = state_dir / "promotions" / "promotion-20260411T182231Z-opw-testing-to-prod.json"
            self.assertEqual(Path(result.output.strip()), written_path)
            self.assertEqual(
                PromotionRecord.model_validate(json.loads(written_path.read_text(encoding="utf-8"))),
                record,
            )

    def test_inventory_write_from_deployment_persists_inventory(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            deployment_dir = state_dir / "deployments"
            deployment_dir.mkdir(parents=True, exist_ok=True)
            record = DeploymentRecord(
                record_id="deployment-20260411T182231Z-opw-prod",
                artifact_identity={"artifact_id": "artifact-2", "manifest_version": 1},
                context="opw",
                instance="prod",
                source_git_ref="def456",
                resolved_target=ResolvedTargetEvidence(
                    target_type="compose",
                    target_id="compose-123",
                    target_name="opw-prod",
                ),
                deploy=DeploymentEvidence(
                    target_name="opw-prod",
                    target_type="compose",
                    deploy_mode="dokploy-compose-api",
                    deployment_id="control-plane-dokploy",
                    status="pass",
                    started_at="2026-04-11T18:22:31Z",
                    finished_at="2026-04-11T18:22:31Z",
                ),
            )
            (deployment_dir / f"{record.record_id}.json").write_text(
                record.model_dump_json(indent=2),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "inventory",
                    "write-from-deployment",
                    "--state-dir",
                    str(state_dir),
                    "--record-id",
                    record.record_id,
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            written_path = state_dir / "inventory" / "opw-prod.json"
            self.assertEqual(Path(result.output.strip()), written_path)
            written_payload = json.loads(written_path.read_text(encoding="utf-8"))
            self.assertEqual(written_payload["context"], "opw")
            self.assertEqual(written_payload["instance"], "prod")
            self.assertEqual(written_payload["deployment_record_id"], record.record_id)
            self.assertEqual(written_payload["source_git_ref"], "def456")
            self.assertEqual(written_payload["artifact_identity"]["artifact_id"], "artifact-2")

    def test_inventory_write_from_promotion_persists_inventory(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            deployment_dir = state_dir / "deployments"
            promotion_dir = state_dir / "promotions"
            deployment_dir.mkdir(parents=True, exist_ok=True)
            promotion_dir.mkdir(parents=True, exist_ok=True)
            deployment_record = DeploymentRecord(
                record_id="deployment-20260411T182231Z-opw-prod",
                artifact_identity={"artifact_id": "artifact-2", "manifest_version": 1},
                context="opw",
                instance="prod",
                source_git_ref="def456",
                resolved_target=ResolvedTargetEvidence(
                    target_type="compose",
                    target_id="compose-123",
                    target_name="opw-prod",
                ),
                deploy=DeploymentEvidence(
                    target_name="opw-prod",
                    target_type="compose",
                    deploy_mode="dokploy-compose-api",
                    deployment_id="control-plane-dokploy",
                    status="pass",
                    started_at="2026-04-11T18:22:31Z",
                    finished_at="2026-04-11T18:22:31Z",
                ),
            )
            promotion_record = PromotionRecord(
                record_id="promotion-20260411T182231Z-opw-testing-to-prod",
                artifact_identity={"artifact_id": "artifact-2", "manifest_version": 1},
                deployment_record_id=deployment_record.record_id,
                backup_record_id="backup-opw-prod-20260410T182231Z",
                context="opw",
                from_instance="testing",
                to_instance="prod",
                backup_gate={"required": True, "status": "pass", "evidence": {"snapshot": "snap-1"}},
                deploy={
                    "target_name": "opw-prod",
                    "target_type": "compose",
                    "deploy_mode": "dokploy-compose-api",
                    "deployment_id": "control-plane-dokploy",
                    "status": "pass",
                    "started_at": "2026-04-11T18:22:31Z",
                    "finished_at": "2026-04-11T18:22:31Z",
                },
                destination_health={
                    "verified": True,
                    "urls": ["https://prod.example.com/web/health"],
                    "timeout_seconds": 45,
                    "status": "pass",
                },
            )
            (deployment_dir / f"{deployment_record.record_id}.json").write_text(
                deployment_record.model_dump_json(indent=2),
                encoding="utf-8",
            )
            (promotion_dir / f"{promotion_record.record_id}.json").write_text(
                promotion_record.model_dump_json(indent=2),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "inventory",
                    "write-from-promotion",
                    "--state-dir",
                    str(state_dir),
                    "--record-id",
                    promotion_record.record_id,
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            written_path = state_dir / "inventory" / "opw-prod.json"
            self.assertEqual(Path(result.output.strip()), written_path)
            written_payload = json.loads(written_path.read_text(encoding="utf-8"))
            self.assertEqual(written_payload["deployment_record_id"], deployment_record.record_id)
            self.assertEqual(written_payload["promotion_record_id"], promotion_record.record_id)
            self.assertEqual(written_payload["promoted_from_instance"], "testing")
            self.assertEqual(written_payload["artifact_identity"]["artifact_id"], "artifact-2")

    def test_inventory_write_from_promotion_requires_deployment_linkage(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            promotion_dir = state_dir / "promotions"
            promotion_dir.mkdir(parents=True, exist_ok=True)
            promotion_record = PromotionRecord(
                record_id="promotion-20260411T182231Z-opw-testing-to-prod",
                artifact_identity={"artifact_id": "artifact-2", "manifest_version": 1},
                backup_record_id="backup-opw-prod-20260410T182231Z",
                context="opw",
                from_instance="testing",
                to_instance="prod",
                backup_gate={"required": True, "status": "pass", "evidence": {"snapshot": "snap-1"}},
                deploy={
                    "target_name": "opw-prod",
                    "target_type": "compose",
                    "deploy_mode": "dokploy-compose-api",
                    "deployment_id": "control-plane-dokploy",
                    "status": "pass",
                    "started_at": "2026-04-11T18:22:31Z",
                    "finished_at": "2026-04-11T18:22:31Z",
                },
            )
            (promotion_dir / f"{promotion_record.record_id}.json").write_text(
                promotion_record.model_dump_json(indent=2),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "inventory",
                    "write-from-promotion",
                    "--state-dir",
                    str(state_dir),
                    "--record-id",
                    promotion_record.record_id,
                ],
            )

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("missing deployment_record_id", result.output)

    def test_release_tuples_write_from_promotion_persists_promoted_tuple(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            deployment_dir = state_dir / "deployments"
            promotion_dir = state_dir / "promotions"
            deployment_dir.mkdir(parents=True, exist_ok=True)
            promotion_dir.mkdir(parents=True, exist_ok=True)
            _write_release_tuple_record(state_dir, artifact_id="artifact-2", tenant_sha="def4567")
            deployment_record = DeploymentRecord(
                record_id="deployment-20260411T182231Z-opw-prod",
                artifact_identity={"artifact_id": "artifact-2", "manifest_version": 1},
                context="opw",
                instance="prod",
                source_git_ref="def456",
                resolved_target=ResolvedTargetEvidence(
                    target_type="compose",
                    target_id="compose-123",
                    target_name="opw-prod",
                ),
                deploy=DeploymentEvidence(
                    target_name="opw-prod",
                    target_type="compose",
                    deploy_mode="dokploy-compose-api",
                    deployment_id="control-plane-dokploy",
                    status="pass",
                    started_at="2026-04-11T18:22:31Z",
                    finished_at="2026-04-11T18:22:31Z",
                ),
            )
            promotion_record = PromotionRecord(
                record_id="promotion-20260411T182231Z-opw-testing-to-prod",
                artifact_identity={"artifact_id": "artifact-2", "manifest_version": 1},
                deployment_record_id=deployment_record.record_id,
                backup_record_id="backup-opw-prod-20260410T182231Z",
                context="opw",
                from_instance="testing",
                to_instance="prod",
                backup_gate={"required": True, "status": "pass", "evidence": {"snapshot": "snap-1"}},
                deploy={
                    "target_name": "opw-prod",
                    "target_type": "compose",
                    "deploy_mode": "dokploy-compose-api",
                    "deployment_id": "control-plane-dokploy",
                    "status": "pass",
                    "started_at": "2026-04-11T18:22:31Z",
                    "finished_at": "2026-04-11T18:22:31Z",
                },
            )
            (deployment_dir / f"{deployment_record.record_id}.json").write_text(
                deployment_record.model_dump_json(indent=2),
                encoding="utf-8",
            )
            (promotion_dir / f"{promotion_record.record_id}.json").write_text(
                promotion_record.model_dump_json(indent=2),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "release-tuples",
                    "write-from-promotion",
                    "--state-dir",
                    str(state_dir),
                    "--record-id",
                    promotion_record.record_id,
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            written_path = state_dir / "release_tuples" / "opw-prod.json"
            self.assertEqual(Path(result.output.strip()), written_path)
            written_record = ReleaseTupleRecord.model_validate(
                json.loads(written_path.read_text(encoding="utf-8"))
            )
            self.assertEqual(written_record.provenance, "promotion")
            self.assertEqual(written_record.promoted_from_channel, "testing")
            self.assertEqual(written_record.deployment_record_id, deployment_record.record_id)
            self.assertEqual(written_record.promotion_record_id, promotion_record.record_id)
            self.assertEqual(written_record.repo_shas["tenant-opw"], "def4567")

    def test_release_tuples_write_from_promotion_requires_deployment_linkage(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            promotion_dir = state_dir / "promotions"
            promotion_dir.mkdir(parents=True, exist_ok=True)
            promotion_record = PromotionRecord(
                record_id="promotion-20260411T182231Z-opw-testing-to-prod",
                artifact_identity={"artifact_id": "artifact-2", "manifest_version": 1},
                backup_record_id="backup-opw-prod-20260410T182231Z",
                context="opw",
                from_instance="testing",
                to_instance="prod",
                backup_gate={"required": True, "status": "pass", "evidence": {"snapshot": "snap-1"}},
                deploy={
                    "target_name": "opw-prod",
                    "target_type": "compose",
                    "deploy_mode": "dokploy-compose-api",
                    "deployment_id": "control-plane-dokploy",
                    "status": "pass",
                    "started_at": "2026-04-11T18:22:31Z",
                    "finished_at": "2026-04-11T18:22:31Z",
                },
            )
            (promotion_dir / f"{promotion_record.record_id}.json").write_text(
                promotion_record.model_dump_json(indent=2),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "release-tuples",
                    "write-from-promotion",
                    "--state-dir",
                    str(state_dir),
                    "--record-id",
                    promotion_record.record_id,
                ],
            )

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("missing deployment_record_id", result.output)

    def test_promote_execute_requires_stored_artifact_manifest(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            _write_backup_gate_record(state_dir)
            input_file = repo_root / "promotion-request.json"
            input_file.write_text(
                PromotionRequest(
                    artifact_id="artifact-missing",
                    backup_record_id="backup-opw-prod-20260410T182231Z",
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
                    "execute",
                    "--state-dir",
                    str(state_dir),
                    "--input-file",
                    str(input_file),
                ],
            )

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("stored artifact manifest", result.output)
            promotion_files = sorted((state_dir / "promotions").glob("*.json"))
            self.assertEqual(len(promotion_files), 0)

    def test_promote_execute_requires_stored_backup_gate_record(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            _write_artifact_manifest(state_dir)
            input_file = repo_root / "promotion-request.json"
            input_file.write_text(
                PromotionRequest(
                    artifact_id="artifact-sha256-image456",
                    backup_record_id="backup-missing",
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
                    "execute",
                    "--state-dir",
                    str(state_dir),
                    "--input-file",
                    str(input_file),
                ],
            )

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("stored backup gate record", result.output)

    def test_promote_execute_rejects_backup_gate_record_for_wrong_instance(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            _write_artifact_manifest(state_dir)
            _write_backup_gate_record(state_dir, instance="staging")
            input_file = repo_root / "promotion-request.json"
            input_file.write_text(
                PromotionRequest(
                    artifact_id="artifact-sha256-image456",
                    backup_record_id="backup-opw-prod-20260410T182231Z",
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
                    "execute",
                    "--state-dir",
                    str(state_dir),
                    "--input-file",
                    str(input_file),
                ],
            )

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("does not match promotion destination", result.output)

    def test_promote_execute_requires_current_source_release_tuple(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            _write_artifact_manifest(state_dir)
            _write_backup_gate_record(state_dir)
            input_file = repo_root / "promotion-request.json"
            input_file.write_text(
                PromotionRequest(
                    artifact_id="artifact-sha256-image456",
                    backup_record_id="backup-opw-prod-20260410T182231Z",
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
                    "execute",
                    "--state-dir",
                    str(state_dir),
                    "--input-file",
                    str(input_file),
                ],
            )

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("requires a current source release tuple", result.output)

    def test_ship_execute_persists_environment_inventory_after_success(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            _write_artifact_manifest(state_dir)
            input_file = repo_root / "ship-request.json"
            input_file.write_text(
                ShipRequest(artifact_id="artifact-sha256-image456", context="opw", instance="prod",
                            source_git_ref="abc123", target_name="opw-prod", target_type="compose",
                            deploy_mode="dokploy-compose-api", destination_health={
                        "verified": False,
                        "urls": ["https://prod.example.com/web/health"],
                        "timeout_seconds": 45,
                        "status": "pending",
                    }).model_dump_json(indent=2),
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
                "control_plane.cli._run_compose_post_deploy_update",
                side_effect=lambda **_kwargs: None,
            ), patch(
                "control_plane.cli._wait_for_ship_healthcheck",
                side_effect=lambda **_kwargs: None,
            ):
                result = runner.invoke(
                    main,
                    [
                        "ship",
                        "execute",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            inventory_file = state_dir / "inventory" / "opw-prod.json"
            self.assertTrue(inventory_file.exists())
            persisted_payload = inventory_file.read_text(encoding="utf-8")
            self.assertIn('"artifact_id": "artifact-sha256-image456"', persisted_payload)
            self.assertIn('"deployment_record_id": "deployment-', persisted_payload)
            self.assertIn('native control-plane Dokploy schedule workflow', persisted_payload)
            tuple_file = state_dir / "release_tuples" / "opw-prod.json"
            self.assertTrue(tuple_file.exists())
            tuple_payload = tuple_file.read_text(encoding="utf-8")
            self.assertIn('"tuple_id": "opw-prod-artifact-sha256-image456"', tuple_payload)
            self.assertIn('"tenant-opw": "abc1234"', tuple_payload)

    def test_promote_execute_persists_inventory_with_promotion_linkage(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            _write_artifact_manifest(state_dir)
            _write_backup_gate_record(state_dir)
            _write_release_tuple_record(state_dir)
            input_file = repo_root / "promotion-request.json"
            input_file.write_text(
                PromotionRequest(artifact_id="artifact-sha256-image456",
                                 backup_record_id="backup-opw-prod-20260410T182231Z", source_git_ref="abc123",
                                 context="opw", from_instance="testing", to_instance="prod", target_name="opw-prod",
                                 target_type="compose", deploy_mode="dokploy-compose-api", health_timeout_seconds=45,
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
                                 }).model_dump_json(indent=2),
                encoding="utf-8",
            )

            with patch(
                "control_plane.cli._resolve_ship_request_for_promotion",
                return_value=ShipRequest(context="opw", instance="prod", source_git_ref="abc123",
                                         target_name="opw-prod", target_type="compose",
                                         deploy_mode="dokploy-compose-api", artifact_id="artifact-sha256-image456",
                                         destination_health={
                                             "verified": False,
                                             "urls": ["https://prod.example.com/web/health"],
                                             "timeout_seconds": 45,
                                             "status": "pending",
                                         }),
            ), patch(
                "control_plane.cli._execute_ship",
                return_value=(
                    state_dir / "deployments" / "deployment-1.json",
                    DeploymentRecord(record_id="deployment-1",
                                     artifact_identity={"artifact_id": "artifact-sha256-image456"}, context="opw",
                                     instance="prod", source_git_ref="abc123", deploy={
                            "target_name": "opw-prod",
                            "target_type": "compose",
                            "deploy_mode": "dokploy-compose-api",
                            "deployment_id": "control-plane-dokploy",
                            "status": "pass",
                            "started_at": "2026-04-10T18:22:31Z",
                            "finished_at": "2026-04-10T18:24:00Z",
                        }, post_deploy_update={
                            "attempted": True,
                            "status": "pass",
                            "detail": "Odoo-specific post-deploy update completed through the native control-plane Dokploy schedule workflow.",
                        }, destination_health={
                            "verified": True,
                            "urls": ["https://prod.example.com/web/health"],
                            "timeout_seconds": 45,
                            "status": "pass",
                        }),
                ),
            ):
                result = runner.invoke(
                    main,
                    [
                        "promote",
                        "execute",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            inventory_file = state_dir / "inventory" / "opw-prod.json"
            self.assertTrue(inventory_file.exists())
            persisted_payload = inventory_file.read_text(encoding="utf-8")
            self.assertIn('"artifact_id": "artifact-sha256-image456"', persisted_payload)
            self.assertIn('"promotion_record_id": "promotion-', persisted_payload)
            self.assertIn('"promoted_from_instance": "testing"', persisted_payload)
            tuple_file = state_dir / "release_tuples" / "opw-prod.json"
            self.assertTrue(tuple_file.exists())
            tuple_payload = tuple_file.read_text(encoding="utf-8")
            self.assertIn('"provenance": "promotion"', tuple_payload)
            self.assertIn('"promoted_from_channel": "testing"', tuple_payload)

    def test_ship_plan_validates_request(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            input_file = repo_root / "ship-request.json"
            input_file.write_text(
                ShipRequest(
                    artifact_id="artifact-sha256-image456",
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
                    "plan",
                    "--input-file",
                    str(input_file),
                ],
            )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn('"source_git_ref": "abc123"', result.output)

    def test_ship_execute_prefers_stored_artifact_manifest_for_commit(self) -> None:
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
                        "source_commit": "abc1234",
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
                ShipRequest(
                    artifact_id="artifact-sha256-image456",
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
                    "execute",
                    "--state-dir",
                    str(state_dir),
                    "--input-file",
                    str(input_file),
                ],
            )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn('"artifact_id": "artifact-sha256-image456"', result.output)

    def test_ship_execute_runs_dokploy_in_control_plane(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            _write_artifact_manifest(state_dir)
            source_file = _write_control_plane_dokploy_source_of_truth(
                repo_root,
                """
schema_version = 2

[defaults]
target_type = "compose"

[[targets]]
context = "opw"
instance = "prod"
target_id = "compose-123"
""",
            )
            input_file = repo_root / "ship-request.json"
            input_file.write_text(
                ShipRequest(artifact_id="artifact-sha256-image456", context="opw", instance="prod",
                            source_git_ref="abc123", target_name="opw-prod", target_type="compose",
                            deploy_mode="dokploy-compose-api", verify_health=False).model_dump_json(indent=2),
                encoding="utf-8",
            )
            post_deploy_updates: list[dict[str, object]] = []

            with patch.dict(os.environ,
                            {control_plane_dokploy.CONTROL_PLANE_DOKPLOY_SOURCE_FILE_ENV_VAR: str(source_file)}), patch(
                "control_plane.cli._run_compose_post_deploy_update",
                side_effect=lambda **kwargs: post_deploy_updates.append(kwargs),
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
                        "execute",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertEqual(len(post_deploy_updates), 1)
            deployment_files = sorted((state_dir / "deployments").glob("*.json"))
            self.assertEqual(len(deployment_files), 1)
            persisted_payload = deployment_files[0].read_text(encoding="utf-8")
            self.assertIn('"status": "pass"', persisted_payload)
            self.assertIn('"delegated_executor": "control-plane.dokploy"', persisted_payload)
            self.assertIn('"deployment_id": "control-plane-dokploy"', persisted_payload)
            self.assertIn('"target_id": "compose-123"', persisted_payload)
            self.assertNotIn('"branch_sync"', persisted_payload)

    def test_ship_execute_resolves_artifact_from_stored_manifest(self) -> None:
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
                        "source_commit": "abc1234",
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
            source_file = _write_control_plane_dokploy_source_of_truth(
                repo_root,
                """
schema_version = 2

[defaults]
target_type = "compose"

[[targets]]
context = "opw"
instance = "prod"
target_id = "compose-123"
""",
            )
            input_file = repo_root / "ship-request.json"
            input_file.write_text(
                ShipRequest(artifact_id="artifact-sha256-image456", context="opw", instance="prod",
                            source_git_ref="abc123", target_name="opw-prod", target_type="compose",
                            deploy_mode="dokploy-compose-api", verify_health=False).model_dump_json(indent=2),
                encoding="utf-8",
            )

            captured_sync_calls: list[dict[str, object]] = []

            with patch.dict(os.environ,
                            {control_plane_dokploy.CONTROL_PLANE_DOKPLOY_SOURCE_FILE_ENV_VAR: str(source_file)}), patch(
                "control_plane.cli._sync_artifact_image_reference_for_target",
                side_effect=lambda **kwargs: captured_sync_calls.append(kwargs),
            ), patch(
                "control_plane.cli._execute_dokploy_deploy",
                side_effect=lambda **_kwargs: None,
            ), patch(
                "control_plane.cli._run_compose_post_deploy_update",
                side_effect=lambda **_kwargs: None,
            ):
                result = runner.invoke(
                    main,
                    [
                        "ship",
                        "execute",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
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

    def test_ship_execute_runs_health_verification_from_control_plane(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            _write_artifact_manifest(state_dir)
            source_file = _write_control_plane_dokploy_source_of_truth(
                repo_root,
                """
schema_version = 2

[defaults]
target_type = "compose"

[[targets]]
context = "opw"
instance = "prod"
target_id = "compose-123"
""",
            )
            input_file = repo_root / "ship-request.json"
            input_file.write_text(
                ShipRequest(artifact_id="artifact-sha256-image456", context="opw", instance="prod",
                            source_git_ref="abc123", target_name="opw-prod", target_type="compose",
                            deploy_mode="dokploy-compose-api", destination_health={
                        "verified": False,
                        "urls": ["https://prod.example.com/web/health"],
                        "timeout_seconds": 45,
                        "status": "pending",
                    }).model_dump_json(indent=2),
                encoding="utf-8",
            )
            post_deploy_updates: list[dict[str, object]] = []
            captured_health_urls: list[str] = []

            with patch.dict(os.environ,
                            {control_plane_dokploy.CONTROL_PLANE_DOKPLOY_SOURCE_FILE_ENV_VAR: str(source_file)}), patch(
                "control_plane.cli._run_compose_post_deploy_update",
                side_effect=lambda **kwargs: post_deploy_updates.append(kwargs),
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
                        "execute",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertEqual(len(post_deploy_updates), 1)
            self.assertEqual(captured_health_urls, ["https://prod.example.com/web/health"])

    def test_ship_execute_marks_record_failed_when_health_verification_fails(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            _write_artifact_manifest(state_dir)
            input_file = repo_root / "ship-request.json"
            input_file.write_text(
                ShipRequest(artifact_id="artifact-sha256-image456", context="opw", instance="prod",
                            source_git_ref="abc123", target_name="opw-prod", target_type="compose",
                            deploy_mode="dokploy-compose-api", destination_health={
                        "verified": False,
                        "urls": ["https://prod.example.com/web/health"],
                        "timeout_seconds": 45,
                        "status": "pending",
                    }).model_dump_json(indent=2),
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
                "control_plane.cli._run_compose_post_deploy_update",
                side_effect=lambda **_kwargs: None,
            ), patch(
                "control_plane.cli._wait_for_ship_healthcheck",
                side_effect=click.ClickException("Healthcheck failed"),
            ):
                result = runner.invoke(
                    main,
                    [
                        "ship",
                        "execute",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                    ],
                )

            self.assertNotEqual(result.exit_code, 0)
            deployment_files = sorted((state_dir / "deployments").glob("*.json"))
            self.assertEqual(len(deployment_files), 1)
            persisted_payload = deployment_files[0].read_text(encoding="utf-8")
            self.assertIn('"status": "fail"', persisted_payload)
            self.assertIn('"post_deploy_update": {', persisted_payload)
            self.assertIn('"attempted": true', persisted_payload)

    def test_ship_execute_accepts_first_passing_health_url_when_multiple_are_available(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            _write_artifact_manifest(state_dir)
            input_file = repo_root / "ship-request.json"
            input_file.write_text(
                ShipRequest(
                    artifact_id="artifact-sha256-image456",
                    context="opw",
                    instance="prod",
                    source_git_ref="abc123",
                    target_name="opw-prod",
                    target_type="compose",
                    deploy_mode="dokploy-compose-api",
                    destination_health={
                        "verified": False,
                        "urls": [
                            "https://prod-alt.example.com/web/health",
                            "https://prod.example.com/web/health",
                        ],
                        "timeout_seconds": 45,
                        "status": "pending",
                    },
                ).model_dump_json(indent=2),
                encoding="utf-8",
            )

            captured_health_urls: list[str] = []

            def _health_side_effect(url: str, timeout_seconds: int) -> None:
                self.assertEqual(timeout_seconds, 45)
                captured_health_urls.append(url)
                if url == "https://prod-alt.example.com/web/health":
                    raise click.ClickException("Healthcheck failed for https://prod-alt.example.com/web/health: http 401")

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
                "control_plane.cli._run_compose_post_deploy_update",
                side_effect=lambda **_kwargs: None,
            ), patch(
                "control_plane.cli._wait_for_ship_healthcheck",
                side_effect=_health_side_effect,
            ):
                result = runner.invoke(
                    main,
                    [
                        "ship",
                        "execute",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertEqual(
                captured_health_urls,
                [
                    "https://prod-alt.example.com/web/health",
                    "https://prod.example.com/web/health",
                ],
            )

    def test_ship_execute_marks_record_failed_when_all_health_urls_fail(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            _write_artifact_manifest(state_dir)
            input_file = repo_root / "ship-request.json"
            input_file.write_text(
                ShipRequest(
                    artifact_id="artifact-sha256-image456",
                    context="opw",
                    instance="prod",
                    source_git_ref="abc123",
                    target_name="opw-prod",
                    target_type="compose",
                    deploy_mode="dokploy-compose-api",
                    destination_health={
                        "verified": False,
                        "urls": [
                            "https://prod-alt.example.com/web/health",
                            "https://prod.example.com/web/health",
                        ],
                        "timeout_seconds": 45,
                        "status": "pending",
                    },
                ).model_dump_json(indent=2),
                encoding="utf-8",
            )

            def _health_side_effect(url: str, timeout_seconds: int) -> None:
                self.assertEqual(timeout_seconds, 45)
                raise click.ClickException(f"Healthcheck failed for {url}: http 401")

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
                "control_plane.cli._run_compose_post_deploy_update",
                side_effect=lambda **_kwargs: None,
            ), patch(
                "control_plane.cli._wait_for_ship_healthcheck",
                side_effect=_health_side_effect,
            ):
                result = runner.invoke(
                    main,
                    [
                        "ship",
                        "execute",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                    ],
                )

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("all resolved URLs", result.output)
            self.assertIn("prod-alt.example.com", result.output)
            self.assertIn("prod.example.com", result.output)
            deployment_files = sorted((state_dir / "deployments").glob("*.json"))
            self.assertEqual(len(deployment_files), 1)
            persisted_payload = deployment_files[0].read_text(encoding="utf-8")
            self.assertIn('"status": "fail"', persisted_payload)
            self.assertIn('native control-plane Dokploy schedule workflow', persisted_payload)

    def test_ship_execute_marks_post_deploy_update_failed_when_native_update_fails(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            _write_artifact_manifest(state_dir)
            input_file = repo_root / "ship-request.json"
            input_file.write_text(
                ShipRequest(artifact_id="artifact-sha256-image456", context="opw", instance="prod",
                            source_git_ref="abc123", target_name="opw-prod", target_type="compose",
                            deploy_mode="dokploy-compose-api", destination_health={
                        "verified": False,
                        "urls": ["https://prod.example.com/web/health"],
                        "timeout_seconds": 45,
                        "status": "pending",
                    }).model_dump_json(indent=2),
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
                "control_plane.cli._run_compose_post_deploy_update",
                side_effect=click.ClickException("Native post-deploy update failed"),
            ):
                result = runner.invoke(
                    main,
                    [
                        "ship",
                        "execute",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                    ],
                )

            self.assertNotEqual(result.exit_code, 0)
            deployment_files = sorted((state_dir / "deployments").glob("*.json"))
            self.assertEqual(len(deployment_files), 1)
            persisted_payload = deployment_files[0].read_text(encoding="utf-8")
            self.assertIn('"deploy": {', persisted_payload)
            self.assertIn('"status": "pass"', persisted_payload)
            self.assertIn('"post_deploy_update": {', persisted_payload)
            self.assertIn('"attempted": true', persisted_payload)
            self.assertIn('native control-plane Dokploy schedule workflow', persisted_payload)
            self.assertIn('"destination_health": {', persisted_payload)
            self.assertIn('"verified": false', persisted_payload)

    def test_ship_execute_persists_failed_deployment_record(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            _write_artifact_manifest(state_dir)
            input_file = repo_root / "ship-request.json"
            input_file.write_text(
                ShipRequest(artifact_id="artifact-sha256-image456", context="opw", instance="prod",
                            source_git_ref="abc123", target_name="opw-prod", target_type="compose",
                            deploy_mode="dokploy-compose-api", destination_health={
                        "verified": False,
                        "urls": ["https://prod.example.com/web/health"],
                        "timeout_seconds": 45,
                        "status": "pending",
                    }).model_dump_json(indent=2),
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
                        "execute",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                    ],
                )

            self.assertNotEqual(result.exit_code, 0)
            deployment_files = sorted((state_dir / "deployments").glob("*.json"))
            self.assertEqual(len(deployment_files), 1)
            persisted_payload = deployment_files[0].read_text(encoding="utf-8")
            self.assertIn('"status": "fail"', persisted_payload)
            self.assertIn('"finished_at": "', persisted_payload)

    def test_ship_execute_fails_closed_when_artifact_manifest_is_missing(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            input_file = repo_root / "ship-request.json"
            input_file.write_text(
                ShipRequest(
                    artifact_id="artifact-sha256-missing",
                    context="opw",
                    instance="prod",
                    source_git_ref="origin/opw-prod",
                    target_name="opw-prod",
                    target_type="compose",
                    deploy_mode="dokploy-compose-api",
                ).model_dump_json(indent=2),
                encoding="utf-8",
            )
            result = runner.invoke(
                main,
                [
                    "ship",
                    "execute",
                    "--state-dir",
                    str(state_dir),
                    "--input-file",
                    str(input_file),
                ],
            )

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("stored artifact manifest", result.output)
            deployment_files = sorted((state_dir / "deployments").glob("*.json"))
            self.assertEqual(len(deployment_files), 0)

    def test_ship_execute_allows_waited_compose_without_odoo_ai_root(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            _write_artifact_manifest(state_dir)
            input_file = repo_root / "ship-request.json"
            input_file.write_text(
                ShipRequest(artifact_id="artifact-sha256-image456", context="opw", instance="prod",
                            source_git_ref="abc123", target_name="opw-prod", target_type="compose",
                            deploy_mode="dokploy-compose-api", verify_health=False).model_dump_json(indent=2),
                encoding="utf-8",
            )

            post_deploy_updates: list[dict[str, object]] = []

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
                "control_plane.cli._run_compose_post_deploy_update",
                side_effect=lambda **kwargs: post_deploy_updates.append(kwargs),
            ):
                result = runner.invoke(
                    main,
                    [
                        "ship",
                        "execute",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertEqual(len(post_deploy_updates), 1)
            deployment_files = sorted((state_dir / "deployments").glob("*.json"))
            self.assertEqual(len(deployment_files), 1)
            self.assertIn('"status": "pass"', deployment_files[0].read_text(encoding="utf-8"))

    def test_ship_execute_allows_waited_application_without_odoo_ai_root(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            _write_artifact_manifest(state_dir)
            input_file = repo_root / "ship-request.json"
            input_file.write_text(
                ShipRequest(artifact_id="artifact-sha256-image456", context="opw", instance="prod",
                            source_git_ref="abc123", target_name="opw-prod", target_type="application",
                            deploy_mode="dokploy-application-api", verify_health=False).model_dump_json(indent=2),
                encoding="utf-8",
            )

            with patch(
                "control_plane.cli._resolve_dokploy_target",
                return_value=(
                    ResolvedTargetEvidence(target_type="application", target_id="application-123", target_name="opw-prod"),
                    600,
                ),
            ), patch(
                "control_plane.cli._sync_artifact_image_reference_for_target",
                side_effect=lambda **_kwargs: None,
            ), patch(
                "control_plane.cli._execute_dokploy_deploy",
                side_effect=lambda **_kwargs: None,
            ), patch(
                "control_plane.cli._run_compose_post_deploy_update",
                side_effect=AssertionError("application deploys must not run compose post-deploy update"),
            ):
                result = runner.invoke(
                    main,
                    [
                        "ship",
                        "execute",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            deployment_files = sorted((state_dir / "deployments").glob("*.json"))
            self.assertEqual(len(deployment_files), 1)
            persisted_payload = deployment_files[0].read_text(encoding="utf-8")
            self.assertIn('"target_type": "application"', persisted_payload)
            self.assertIn('"attempted": false', persisted_payload)
            self.assertIn('"status": "skipped"', persisted_payload)

    def test_ship_execute_allows_async_compose_without_odoo_ai_root(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            _write_artifact_manifest(state_dir)
            input_file = repo_root / "ship-request.json"
            input_file.write_text(
                ShipRequest(
                    artifact_id="artifact-sha256-image456",
                    context="opw",
                    instance="prod",
                    source_git_ref="abc123",
                    target_name="opw-prod",
                    target_type="compose",
                    deploy_mode="dokploy-compose-api",
                    wait=False,
                    verify_health=False,
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
                "control_plane.cli._run_compose_post_deploy_update",
                side_effect=AssertionError("async compose deploys must not run compose post-deploy update"),
            ):
                result = runner.invoke(
                    main,
                    [
                        "ship",
                        "execute",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            deployment_files = sorted((state_dir / "deployments").glob("*.json"))
            self.assertEqual(len(deployment_files), 1)
            persisted_payload = deployment_files[0].read_text(encoding="utf-8")
            self.assertIn('"target_type": "compose"', persisted_payload)
            self.assertIn('"attempted": false', persisted_payload)
            self.assertIn('"status": "skipped"', persisted_payload)

    def test_ship_execute_fails_closed_when_target_resolution_fails(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            _write_artifact_manifest(state_dir)
            input_file = repo_root / "ship-request.json"
            input_file.write_text(
                ShipRequest(
                    artifact_id="artifact-sha256-image456",
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
                        "execute",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
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
            source_file = _write_control_plane_dokploy_source_of_truth(
                repo_root,
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
""",
            )

            with patch.dict(os.environ,
                            {control_plane_dokploy.CONTROL_PLANE_DOKPLOY_SOURCE_FILE_ENV_VAR: str(source_file)}):
                resolved_target, deploy_timeout_seconds = _resolve_dokploy_target(
                    request=ShipRequest(
                        artifact_id="artifact-sha256-image456",
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

    def test_resolve_native_ship_request_reads_source_of_truth_and_env_healthcheck(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            source_file = _write_control_plane_dokploy_source_of_truth(
                repo_root,
                """
schema_version = 2

[[targets]]
context = "opw"
instance = "prod"
target_id = "compose-123"
target_type = "compose"
deploy_timeout_seconds = 7200
healthcheck_timeout_seconds = 55
""",
            )
            runtime_environments_file = repo_root / "runtime-environments.toml"
            runtime_environments_file.write_text(
                """
schema_version = 1

[contexts.opw.instances.prod.env]
ENV_OVERRIDE_CONFIG_PARAM__WEB__BASE__URL = "prod.example.com"
DOKPLOY_SHIP_MODE = "auto"
""".strip()
                + "\n",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {
                control_plane_dokploy.CONTROL_PLANE_DOKPLOY_SOURCE_FILE_ENV_VAR: str(source_file),
                control_plane_runtime_environments.RUNTIME_ENVIRONMENTS_FILE_ENV_VAR: str(runtime_environments_file),
            }):
                ship_request = _resolve_native_ship_request(
                    context_name="opw",
                    instance_name="prod",
                    artifact_id="artifact-sha256-image456",
                    source_git_ref="",
                    wait=True,
                    timeout_override_seconds=None,
                    verify_health=True,
                    health_timeout_override_seconds=None,
                    dry_run=False,
                    no_cache=False,
                    allow_dirty=False,
                )

        self.assertEqual(ship_request.target_name, "opw-prod")
        self.assertEqual(ship_request.target_type, "compose")
        self.assertEqual(ship_request.deploy_mode, "dokploy-compose-api")
        self.assertEqual(ship_request.source_git_ref, "origin/main")
        self.assertEqual(ship_request.destination_health.urls, ("https://prod.example.com/web/health",))
        self.assertEqual(ship_request.destination_health.timeout_seconds, 55)
        self.assertEqual(ship_request.destination_health.status, "pending")

    def test_ship_resolve_emits_typed_request_payload(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            source_file = _write_control_plane_dokploy_source_of_truth(
                repo_root,
                """
schema_version = 2

[[targets]]
context = "opw"
instance = "prod"
target_id = "compose-123"
target_type = "compose"
target_name = "opw-prod"
domains = ["https://prod.example.com"]
""",
            )

            with patch.dict(os.environ,
                            {control_plane_dokploy.CONTROL_PLANE_DOKPLOY_SOURCE_FILE_ENV_VAR: str(source_file)}):
                result = runner.invoke(
                    main,
                    [
                        "ship",
                        "resolve",
                        "--context",
                        "opw",
                        "--instance",
                        "prod",
                        "--artifact-id",
                        "artifact-sha256-image456",
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn('"target_name": "opw-prod"', result.output)
            self.assertIn('"deploy_mode": "dokploy-compose-api"', result.output)
            self.assertIn('"source_git_ref": "origin/main"', result.output)
            self.assertIn('"https://prod.example.com/web/health"', result.output)

    def test_resolve_native_promotion_request_reads_source_and_destination_targets(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            source_file = _write_control_plane_dokploy_source_of_truth(
                repo_root,
                """
schema_version = 2

[[targets]]
context = "opw"
instance = "testing"
target_id = "compose-testing"
target_type = "compose"
target_name = "opw-testing"
source_git_ref = "origin/opw-testing"
healthcheck_timeout_seconds = 25
domains = ["https://testing.example.com"]

[[targets]]
context = "opw"
instance = "prod"
target_id = "compose-prod"
target_type = "compose"
target_name = "opw-prod"
healthcheck_timeout_seconds = 55
domains = ["https://prod.example.com"]
""",
            )

            with patch.dict(os.environ,
                            {control_plane_dokploy.CONTROL_PLANE_DOKPLOY_SOURCE_FILE_ENV_VAR: str(source_file)}):
                promotion_request = _resolve_native_promotion_request(
                    context_name="opw",
                    from_instance_name="testing",
                    to_instance_name="prod",
                    artifact_id="artifact-sha256-image456",
                    backup_record_id="backup-opw-prod-20260410T182231Z",
                    source_git_ref="",
                    wait=True,
                    timeout_override_seconds=None,
                    verify_health=True,
                    health_timeout_override_seconds=None,
                    dry_run=False,
                    no_cache=False,
                    allow_dirty=False,
                )

        self.assertEqual(promotion_request.source_git_ref, "origin/opw-testing")
        self.assertEqual(promotion_request.target_name, "opw-prod")
        self.assertEqual(promotion_request.target_type, "compose")
        self.assertEqual(promotion_request.deploy_mode, "dokploy-compose-api")
        self.assertEqual(promotion_request.source_health.urls, ("https://testing.example.com/web/health",))
        self.assertEqual(promotion_request.source_health.timeout_seconds, 25)
        self.assertEqual(promotion_request.source_health.status, "pending")
        self.assertEqual(promotion_request.destination_health.urls, ("https://prod.example.com/web/health",))
        self.assertEqual(promotion_request.destination_health.timeout_seconds, 55)
        self.assertEqual(promotion_request.destination_health.status, "pending")
        self.assertEqual(promotion_request.backup_record_id, "backup-opw-prod-20260410T182231Z")

    def test_promote_resolve_emits_typed_request_payload(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            source_file = _write_control_plane_dokploy_source_of_truth(
                repo_root,
                """
schema_version = 2

[[targets]]
context = "opw"
instance = "testing"
target_id = "compose-testing"
target_type = "compose"
target_name = "opw-testing"
source_git_ref = "origin/opw-testing"
domains = ["https://testing.example.com"]

[[targets]]
context = "opw"
instance = "prod"
target_id = "compose-prod"
target_type = "compose"
target_name = "opw-prod"
domains = ["https://prod.example.com"]
""",
            )

            with patch.dict(os.environ,
                            {control_plane_dokploy.CONTROL_PLANE_DOKPLOY_SOURCE_FILE_ENV_VAR: str(source_file)}):
                result = runner.invoke(
                    main,
                    [
                        "promote",
                        "resolve",
                        "--context",
                        "opw",
                        "--from-instance",
                        "testing",
                        "--to-instance",
                        "prod",
                        "--artifact-id",
                        "artifact-sha256-image456",
                        "--backup-record-id",
                        "backup-opw-prod-20260410T182231Z",
                    ],
                )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn('"target_name": "opw-prod"', result.output)
            self.assertIn('"from_instance": "testing"', result.output)
            self.assertIn('"to_instance": "prod"', result.output)
            self.assertIn('"backup_record_id": "backup-opw-prod-20260410T182231Z"', result.output)
            self.assertIn('"deploy_mode": "dokploy-compose-api"', result.output)

    def test_promote_execute_rejects_target_metadata_drift_against_source_of_truth(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            _write_artifact_manifest(state_dir)
            _write_backup_gate_record(state_dir)
            _write_release_tuple_record(state_dir)
            source_file = _write_control_plane_dokploy_source_of_truth(
                repo_root,
                """
schema_version = 2

[[targets]]
context = "opw"
instance = "prod"
target_id = "compose-123"
target_type = "application"
target_name = "opw-prod"
domains = ["https://prod.example.com"]
""",
            )
            input_file = repo_root / "promotion-request.json"
            input_file.write_text(
                PromotionRequest(artifact_id="artifact-sha256-image456",
                                 backup_record_id="backup-opw-prod-20260410T182231Z", source_git_ref="abc123",
                                 context="opw", from_instance="testing", to_instance="prod", target_name="opw-prod",
                                 target_type="compose", deploy_mode="dokploy-compose-api").model_dump_json(indent=2),
                encoding="utf-8",
            )

            with patch.dict(os.environ,
                            {control_plane_dokploy.CONTROL_PLANE_DOKPLOY_SOURCE_FILE_ENV_VAR: str(source_file)}):
                result = runner.invoke(
                    main,
                    [
                        "promote",
                        "execute",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                    ],
                )

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("target_type does not match control-plane Dokploy source-of-truth", result.output)

    def test_promote_execute_dry_run_rejects_target_metadata_drift_against_source_of_truth(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            _write_artifact_manifest(state_dir)
            _write_backup_gate_record(state_dir)
            _write_release_tuple_record(state_dir)
            source_file = _write_control_plane_dokploy_source_of_truth(
                repo_root,
                """
schema_version = 2

[[targets]]
context = "opw"
instance = "prod"
target_id = "compose-123"
target_type = "application"
target_name = "opw-prod"
domains = ["https://prod.example.com"]
""",
            )
            input_file = repo_root / "promotion-request.json"
            input_file.write_text(
                PromotionRequest(
                    artifact_id="artifact-sha256-image456",
                    backup_record_id="backup-opw-prod-20260410T182231Z",
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

            with patch.dict(os.environ,
                            {control_plane_dokploy.CONTROL_PLANE_DOKPLOY_SOURCE_FILE_ENV_VAR: str(source_file)}):
                result = runner.invoke(
                    main,
                    [
                        "promote",
                        "execute",
                        "--state-dir",
                        str(state_dir),
                        "--input-file",
                        str(input_file),
                    ],
                )

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("target_type does not match control-plane Dokploy source-of-truth", result.output)


if __name__ == "__main__":
    unittest.main()
