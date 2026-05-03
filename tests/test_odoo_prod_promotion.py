import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import click
from pydantic import ValidationError

from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    BackupGateEvidence,
    DeploymentEvidence,
    HealthcheckEvidence,
    PostDeployUpdateEvidence,
    PromotionRequest,
)
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.workflows.odoo_prod_promotion import (
    OdooProdPromotionRequest,
    execute_odoo_prod_promotion,
)


def _promotion_request() -> PromotionRequest:
    return PromotionRequest(
        artifact_id="artifact-cm-new",
        backup_record_id="backup-gate-cm-prod-1",
        source_git_ref="848bf1b69ff3adbe9b255c61c7b8f5ca04efbcbb",
        context="cm",
        from_instance="testing",
        to_instance="prod",
        target_name="cm-prod",
        target_type="compose",
        deploy_mode="dokploy-compose-api",
        wait=True,
        timeout_seconds=1800,
        verify_health=True,
        health_timeout_seconds=180,
        source_health=HealthcheckEvidence(
            urls=("https://cm-testing.example/web/health",),
            timeout_seconds=180,
            status="pending",
        ),
        backup_gate=BackupGateEvidence(
            status="pass",
            evidence={"backup_record_id": "backup-gate-cm-prod-1"},
        ),
        destination_health=HealthcheckEvidence(
            urls=("https://cm-prod.example/web/health",),
            timeout_seconds=180,
            status="pending",
        ),
    )


def _deployment_record() -> DeploymentRecord:
    return DeploymentRecord(
        record_id="deployment-cm-prod",
        artifact_identity=ArtifactIdentityReference(artifact_id="artifact-cm-new"),
        context="cm",
        instance="prod",
        source_git_ref="848bf1b69ff3adbe9b255c61c7b8f5ca04efbcbb",
        wait_for_completion=True,
        deploy=DeploymentEvidence(
            target_name="cm-prod",
            target_type="compose",
            deploy_mode="dokploy-compose-api",
            deployment_id="control-plane-dokploy",
            status="pass",
        ),
        post_deploy_update=PostDeployUpdateEvidence(
            attempted=True,
            status="pass",
            detail="Post-deploy passed.",
        ),
        destination_health=HealthcheckEvidence(
            verified=True,
            urls=("https://cm-prod.example/web/health",),
            timeout_seconds=180,
            status="pass",
        ),
    )


def _source_tuple() -> ReleaseTupleRecord:
    return ReleaseTupleRecord(
        tuple_id="cm-testing-artifact-cm-new",
        context="cm",
        channel="testing",
        artifact_id="artifact-cm-new",
        repo_shas={"tenant-cm": "848bf1b69ff3adbe9b255c61c7b8f5ca04efbcbb"},
        image_repository="ghcr.io/cbusillo/odoo-tenant-cm",
        image_digest="sha256:905b7cb67817e278f4111ca0618c2a1417576b5d03d42ee3292e1ea97f348023",
        deployment_record_id="deployment-cm-testing",
        provenance="ship",
        minted_at="2026-04-27T00:00:00Z",
    )


class OdooProdPromotionWorkflowTests(unittest.TestCase):
    def test_request_accepts_new_odoo_context(self) -> None:
        request = OdooProdPromotionRequest(
            context="  New-Site  ",
            artifact_id="artifact-new-site-005c291b63b6",
            backup_record_id="backup-gate-new-site-prod-1",
        )

        self.assertEqual(request.context, "new-site")

    def test_request_rejects_blank_context(self) -> None:
        with self.assertRaisesRegex(ValidationError, "requires context"):
            OdooProdPromotionRequest(
                context="   ",
                artifact_id="artifact-new-site-005c291b63b6",
                backup_record_id="backup-gate-new-site-prod-1",
            )

    def test_promotion_executes_existing_promotion_flow_and_writes_result(self) -> None:
        record_store = Mock()
        promotion_request = _promotion_request()
        deployment_record = _deployment_record()

        with (
            patch(
                "control_plane.cli._resolve_native_promotion_request",
                return_value=promotion_request,
            ),
            patch("control_plane.cli._read_artifact_manifest"),
            patch(
                "control_plane.cli._resolve_backup_gate_for_promotion",
                return_value=(promotion_request, Mock()),
            ),
            patch(
                "control_plane.cli._read_source_release_tuple_for_promotion",
                return_value=_source_tuple(),
            ),
            patch(
                "control_plane.workflows.promote.generate_promotion_record_id",
                return_value="promotion-cm-testing-to-prod",
            ),
            patch("control_plane.cli._resolve_ship_request_for_promotion", return_value=Mock()),
            patch("control_plane.cli._execute_ship", return_value=(None, deployment_record)),
            patch("control_plane.cli._write_environment_inventory") as write_inventory,
            patch("control_plane.cli._write_promoted_release_tuple") as write_tuple,
        ):
            result = execute_odoo_prod_promotion(
                control_plane_root=Path("/control-plane"),
                state_dir=Path("/state"),
                database_url="postgresql://launchplane.example/db",
                record_store=record_store,
                request=OdooProdPromotionRequest(
                    context="cm",
                    artifact_id="artifact-cm-new",
                    backup_record_id="backup-gate-cm-prod-1",
                    source_git_ref="848bf1b69ff3adbe9b255c61c7b8f5ca04efbcbb",
                ),
            )

        self.assertEqual(result.promotion_status, "pass")
        self.assertEqual(result.promotion_record_id, "promotion-cm-testing-to-prod")
        self.assertEqual(result.deployment_record_id, "deployment-cm-prod")
        self.assertEqual(result.release_tuple_id, "cm-prod-artifact-cm-new")
        self.assertEqual(record_store.write_promotion_record.call_count, 2)
        write_inventory.assert_called_once()
        write_tuple.assert_called_once()

    def test_failed_ship_records_failed_promotion_result(self) -> None:
        record_store = Mock()
        promotion_request = _promotion_request()

        with (
            patch(
                "control_plane.cli._resolve_native_promotion_request",
                return_value=promotion_request,
            ),
            patch("control_plane.cli._read_artifact_manifest"),
            patch(
                "control_plane.cli._resolve_backup_gate_for_promotion",
                return_value=(promotion_request, Mock()),
            ),
            patch(
                "control_plane.cli._read_source_release_tuple_for_promotion",
                return_value=_source_tuple(),
            ),
            patch(
                "control_plane.workflows.promote.generate_promotion_record_id",
                return_value="promotion-cm-testing-to-prod",
            ),
            patch("control_plane.cli._resolve_ship_request_for_promotion", return_value=Mock()),
            patch(
                "control_plane.cli._execute_ship", side_effect=click.ClickException("deploy failed")
            ),
        ):
            result = execute_odoo_prod_promotion(
                control_plane_root=Path("/control-plane"),
                state_dir=Path("/state"),
                database_url="postgresql://launchplane.example/db",
                record_store=record_store,
                request=OdooProdPromotionRequest(
                    context="cm",
                    artifact_id="artifact-cm-new",
                    backup_record_id="backup-gate-cm-prod-1",
                ),
            )

        self.assertEqual(result.promotion_status, "fail")
        self.assertIn("deploy failed", result.error_message)
        final_record = record_store.write_promotion_record.call_args_list[-1].args[0]
        self.assertEqual(final_record.deploy.status, "fail")


if __name__ == "__main__":
    unittest.main()
