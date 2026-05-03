import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import click
from click.testing import CliRunner
from pydantic import ValidationError

from control_plane.cli import main
from control_plane.contracts.artifact_identity import (
    ArtifactIdentityManifest,
    ArtifactImageReference,
)
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    BackupGateEvidence,
    DeploymentEvidence,
    HealthcheckEvidence,
    PromotionRecord,
)
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.workflows.odoo_post_deploy import OdooPostDeployResult
from control_plane.workflows.odoo_prod_rollback import (
    OdooProdRollbackRequest,
    execute_odoo_prod_rollback,
)


def _artifact_manifest() -> ArtifactIdentityManifest:
    return ArtifactIdentityManifest(
        artifact_id="artifact-opw-847c71c1db61785c",
        source_commit="9e09b858e1f93aa4a1f4b887b528ba7e5a999ee6",
        enterprise_base_digest="sha256:enterprise",
        image=ArtifactImageReference(
            repository="ghcr.io/cbusillo/odoo-tenant-opw",
            digest="sha256:847c71c1db61785c0aa265949f45a74c5dd9535e62c89db26d5650684c340100",
        ),
    )


def _previous_prod_artifact_manifest() -> ArtifactIdentityManifest:
    return ArtifactIdentityManifest(
        artifact_id="artifact-opw-previous-prod",
        source_commit="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        enterprise_base_digest="sha256:enterprise",
        image=ArtifactImageReference(
            repository="ghcr.io/cbusillo/odoo-tenant-opw",
            digest="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        ),
    )


def _release_tuple() -> ReleaseTupleRecord:
    return ReleaseTupleRecord(
        tuple_id="opw-testing-artifact-opw-847c71c1db61785c",
        context="opw",
        channel="testing",
        artifact_id="artifact-opw-847c71c1db61785c",
        repo_shas={"tenant-opw": "9e09b858e1f93aa4a1f4b887b528ba7e5a999ee6"},
        image_repository="ghcr.io/cbusillo/odoo-tenant-opw",
        image_digest="sha256:847c71c1db61785c0aa265949f45a74c5dd9535e62c89db26d5650684c340100",
        deployment_record_id="deployment-opw-testing",
        provenance="ship",
        minted_at="2026-04-17T20:32:32Z",
    )


def _promotion_record() -> PromotionRecord:
    return PromotionRecord(
        record_id="promotion-20260417T210945Z-opw-testing-to-prod",
        artifact_identity=ArtifactIdentityReference(artifact_id="artifact-opw-847c71c1db61785c"),
        deployment_record_id="deployment-opw-prod",
        backup_record_id="backup-opw-prod",
        context="opw",
        from_instance="testing",
        to_instance="prod",
        backup_gate=BackupGateEvidence(status="pass", evidence={"reason": "test"}),
        deploy=DeploymentEvidence(
            target_name="opw-prod",
            target_type="compose",
            deploy_mode="dokploy-compose-api",
            deployment_id="control-plane-dokploy",
            status="pass",
        ),
        destination_health=HealthcheckEvidence(
            verified=True,
            urls=("https://opw-prod.shinycomputers.com/web/health",),
            timeout_seconds=180,
            status="pass",
        ),
    )


def _inventory_record() -> EnvironmentInventory:
    return EnvironmentInventory(
        context="opw",
        instance="prod",
        artifact_identity=ArtifactIdentityReference(artifact_id="artifact-opw-847c71c1db61785c"),
        source_git_ref="9e09b858e1f93aa4a1f4b887b528ba7e5a999ee6",
        deploy=DeploymentEvidence(
            target_name="opw-prod",
            target_type="compose",
            deploy_mode="dokploy-compose-api",
            deployment_id="control-plane-dokploy",
            status="pass",
        ),
        destination_health=HealthcheckEvidence(
            verified=True,
            urls=("https://opw-prod.shinycomputers.com/web/health",),
            timeout_seconds=180,
            status="pass",
        ),
        updated_at="2026-04-17T21:11:47Z",
        deployment_record_id="deployment-opw-prod",
        promotion_record_id="promotion-20260417T210945Z-opw-testing-to-prod",
        promoted_from_instance="testing",
    )


def _target_record() -> DokployTargetRecord:
    return DokployTargetRecord(
        context="opw",
        instance="prod",
        target_type="compose",
        target_name="opw-prod",
        deploy_timeout_seconds=600,
        healthcheck_path="/web/health",
        healthcheck_timeout_seconds=180,
        domains=("opw-prod.shinycomputers.com",),
        env={"DOCKER_PULL_POLICY": "always"},
        updated_at="2026-04-24T14:06:57Z",
    )


def _target_id_record() -> DokployTargetIdRecord:
    return DokployTargetIdRecord(
        context="opw",
        instance="prod",
        target_id="opw-prod-compose-id",
        updated_at="2026-04-24T14:06:57Z",
    )


class OdooProdRollbackWorkflowTests(unittest.TestCase):
    def test_request_accepts_new_odoo_context(self) -> None:
        request = OdooProdRollbackRequest(context="  New-Site  ")

        self.assertEqual(request.context, "new-site")

    def test_request_rejects_blank_context(self) -> None:
        with self.assertRaisesRegex(ValidationError, "requires context"):
            OdooProdRollbackRequest(context="   ")

    def _record_store(self) -> Mock:
        record_store = Mock()
        record_store.read_release_tuple_record.return_value = _release_tuple()
        record_store.read_artifact_manifest.return_value = _artifact_manifest()
        record_store.read_environment_inventory.return_value = _inventory_record()
        record_store.read_promotion_record.return_value = _promotion_record()
        record_store.read_dokploy_target_record.return_value = _target_record()
        record_store.read_dokploy_target_id_record.return_value = _target_id_record()
        return record_store

    def test_rollback_to_testing_tuple_records_successful_evidence(self) -> None:
        record_store = self._record_store()

        with (
            patch(
                "control_plane.workflows.odoo_prod_rollback.control_plane_dokploy.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_prod_rollback.control_plane_dokploy.fetch_dokploy_target_payload",
                side_effect=[
                    {"env": "DOCKER_IMAGE_REFERENCE=old\n"},
                    {
                        "env": "DOCKER_IMAGE_REFERENCE=ghcr.io/cbusillo/odoo-tenant-opw@sha256:847c71c1db61785c0aa265949f45a74c5dd9535e62c89db26d5650684c340100"
                    },
                ],
            ),
            patch(
                "control_plane.workflows.odoo_prod_rollback.control_plane_runtime_environments.resolve_runtime_environment_values",
                return_value={},
            ),
            patch(
                "control_plane.workflows.odoo_prod_rollback.control_plane_dokploy.sync_dokploy_compose_raw_source"
            ),
            patch(
                "control_plane.workflows.odoo_prod_rollback.control_plane_dokploy.update_dokploy_target_env"
            ),
            patch(
                "control_plane.workflows.odoo_prod_rollback.control_plane_dokploy.latest_deployment_for_target",
                return_value={"deploymentId": "before"},
            ),
            patch(
                "control_plane.workflows.odoo_prod_rollback.control_plane_dokploy.trigger_deployment"
            ),
            patch(
                "control_plane.workflows.odoo_prod_rollback.control_plane_dokploy.wait_for_target_deployment"
            ),
            patch("control_plane.workflows.odoo_prod_rollback._verify_healthchecks"),
            patch(
                "control_plane.workflows.odoo_prod_rollback.execute_odoo_post_deploy",
                return_value=OdooPostDeployResult(
                    context="opw",
                    instance="prod",
                    phase="deploy",
                    post_deploy_status="pass",
                ),
            ),
        ):
            result = execute_odoo_prod_rollback(
                control_plane_root=Path("/control-plane"),
                record_store=record_store,
                request=OdooProdRollbackRequest(context="opw"),
            )

        self.assertEqual(result.rollback_status, "pass")
        self.assertEqual(result.rollback_health_status, "pass")
        self.assertEqual(result.post_deploy_status, "pass")
        self.assertEqual(result.artifact_id, "artifact-opw-847c71c1db61785c")
        record_store.write_deployment_record.assert_called()
        record_store.write_environment_inventory.assert_called_once()
        record_store.write_release_tuple_record.assert_called_once()
        final_promotion = record_store.write_promotion_record.call_args_list[-1].args[0]
        self.assertEqual(final_promotion.rollback.status, "pass")
        self.assertEqual(final_promotion.rollback_health.status, "pass")
        written_deployment = record_store.write_deployment_record.call_args_list[-1].args[0]
        self.assertIsInstance(written_deployment, DeploymentRecord)
        self.assertEqual(written_deployment.deploy.status, "pass")

    def test_explicit_artifact_rolls_back_without_testing_tuple_match(self) -> None:
        record_store = self._record_store()
        record_store.read_artifact_manifest.return_value = _previous_prod_artifact_manifest()

        with (
            patch(
                "control_plane.workflows.odoo_prod_rollback.control_plane_dokploy.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_prod_rollback.control_plane_dokploy.fetch_dokploy_target_payload",
                side_effect=[
                    {"env": "DOCKER_IMAGE_REFERENCE=old\n"},
                    {
                        "env": "DOCKER_IMAGE_REFERENCE=ghcr.io/cbusillo/odoo-tenant-opw@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                    },
                ],
            ),
            patch(
                "control_plane.workflows.odoo_prod_rollback.control_plane_runtime_environments.resolve_runtime_environment_values",
                return_value={},
            ),
            patch(
                "control_plane.workflows.odoo_prod_rollback.control_plane_dokploy.sync_dokploy_compose_raw_source"
            ),
            patch(
                "control_plane.workflows.odoo_prod_rollback.control_plane_dokploy.update_dokploy_target_env"
            ),
            patch(
                "control_plane.workflows.odoo_prod_rollback.control_plane_dokploy.latest_deployment_for_target",
                return_value={"deploymentId": "before"},
            ),
            patch(
                "control_plane.workflows.odoo_prod_rollback.control_plane_dokploy.trigger_deployment"
            ),
            patch(
                "control_plane.workflows.odoo_prod_rollback.control_plane_dokploy.wait_for_target_deployment"
            ),
            patch("control_plane.workflows.odoo_prod_rollback._verify_healthchecks"),
            patch(
                "control_plane.workflows.odoo_prod_rollback.execute_odoo_post_deploy",
                return_value=OdooPostDeployResult(
                    context="opw",
                    instance="prod",
                    phase="deploy",
                    post_deploy_status="pass",
                ),
            ),
        ):
            result = execute_odoo_prod_rollback(
                control_plane_root=Path("/control-plane"),
                record_store=record_store,
                request=OdooProdRollbackRequest(
                    context="opw",
                    artifact_id="artifact-opw-previous-prod",
                ),
            )

        self.assertEqual(result.rollback_status, "pass")
        self.assertEqual(result.source_channel, "artifact")
        self.assertEqual(result.artifact_id, "artifact-opw-previous-prod")
        record_store.read_release_tuple_record.assert_not_called()
        final_promotion = record_store.write_promotion_record.call_args_list[-1].args[0]
        self.assertEqual(
            final_promotion.rollback.snapshot_name, "artifact:artifact-opw-previous-prod"
        )
        inventory = record_store.write_environment_inventory.call_args.args[0]
        self.assertEqual(inventory.promoted_from_instance, "explicit-artifact")
        written_deployment = record_store.write_deployment_record.call_args_list[-1].args[0]
        self.assertEqual(
            written_deployment.artifact_identity.artifact_id,
            "artifact-opw-previous-prod",
        )

    def test_missing_explicit_artifact_fails_before_deploy(self) -> None:
        record_store = self._record_store()
        record_store.read_artifact_manifest.side_effect = FileNotFoundError

        with self.assertRaises(click.ClickException):
            execute_odoo_prod_rollback(
                control_plane_root=Path("/control-plane"),
                record_store=record_store,
                request=OdooProdRollbackRequest(
                    context="opw",
                    artifact_id="artifact-opw-other",
                ),
            )

        record_store.read_release_tuple_record.assert_not_called()
        record_store.write_deployment_record.assert_not_called()

    def test_failed_deploy_records_failed_rollback(self) -> None:
        record_store = self._record_store()

        with (
            patch(
                "control_plane.workflows.odoo_prod_rollback.control_plane_dokploy.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_prod_rollback.control_plane_dokploy.fetch_dokploy_target_payload",
                side_effect=[
                    {"env": ""},
                    {
                        "env": "DOCKER_IMAGE_REFERENCE=ghcr.io/cbusillo/odoo-tenant-opw@sha256:847c71c1db61785c0aa265949f45a74c5dd9535e62c89db26d5650684c340100"
                    },
                ],
            ),
            patch(
                "control_plane.workflows.odoo_prod_rollback.control_plane_runtime_environments.resolve_runtime_environment_values",
                return_value={},
            ),
            patch(
                "control_plane.workflows.odoo_prod_rollback.control_plane_dokploy.sync_dokploy_compose_raw_source"
            ),
            patch(
                "control_plane.workflows.odoo_prod_rollback.control_plane_dokploy.update_dokploy_target_env"
            ),
            patch(
                "control_plane.workflows.odoo_prod_rollback.control_plane_dokploy.latest_deployment_for_target"
            ),
            patch(
                "control_plane.workflows.odoo_prod_rollback.control_plane_dokploy.trigger_deployment",
                side_effect=click.ClickException("deploy failed"),
            ),
        ):
            result = execute_odoo_prod_rollback(
                control_plane_root=Path("/control-plane"),
                record_store=record_store,
                request=OdooProdRollbackRequest(context="opw"),
            )

        self.assertEqual(result.rollback_status, "fail")
        self.assertIn("deploy failed", result.error_message)
        final_promotion = record_store.write_promotion_record.call_args_list[-1].args[0]
        self.assertEqual(final_promotion.rollback.status, "fail")

    def test_rollback_cli_executes_driver(self) -> None:
        with (
            patch(
                "control_plane.cli.execute_odoo_prod_rollback",
                return_value=Mock(
                    rollback_status="pass",
                    model_dump=Mock(
                        return_value={
                            "context": "cm",
                            "instance": "prod",
                            "artifact_id": "artifact-cm-previous",
                            "promotion_record_id": "promotion-cm-prod",
                            "deployment_record_id": "deployment-cm-prod",
                            "release_tuple_id": "cm-prod-artifact-cm-previous",
                            "rollback_status": "pass",
                            "rollback_health_status": "pass",
                            "post_deploy_status": "pass",
                            "error_message": "",
                        }
                    ),
                ),
            ) as execute_mock,
            patch("control_plane.cli._store", return_value=Mock()),
        ):
            result = CliRunner().invoke(
                main,
                [
                    "odoo-rollbacks",
                    "execute",
                    "--database-url",
                    "postgresql://launchplane.example/db",
                    "--context",
                    "cm",
                    "--artifact-id",
                    "artifact-cm-previous",
                    "--reason",
                    "drill",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("artifact-cm-previous", result.output)
        execute_mock.assert_called_once()
        request = execute_mock.call_args.kwargs["request"]
        self.assertEqual(request.context, "cm")
        self.assertEqual(request.artifact_id, "artifact-cm-previous")
        self.assertEqual(request.reason, "drill")


if __name__ == "__main__":
    unittest.main()
