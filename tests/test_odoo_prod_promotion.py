import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import click
from pydantic import ValidationError

from control_plane.contracts.artifact_identity import (
    ArtifactImageReference,
    ArtifactIdentityManifest,
)
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    DeploymentEvidence,
    HealthcheckEvidence,
    PostDeployUpdateEvidence,
)
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.contracts.odoo_stable_target_replacement import (
    OdooStableTargetReplacementApplyResult,
)
from control_plane.workflows.odoo_prod_promotion import (
    OdooProdPromotionRequest,
    execute_odoo_prod_promotion,
)


def _artifact_manifest() -> ArtifactIdentityManifest:
    return ArtifactIdentityManifest(
        artifact_id="artifact-cm-new",
        source_commit="848bf1b69ff3adbe9b255c61c7b8f5ca04efbcbb",
        enterprise_base_digest="sha256:enterprise",
        image=ArtifactImageReference(
            repository="ghcr.io/cbusillo/odoo-tenant-cm",
            digest="sha256:905b7cb67817e278f4111ca0618c2a1417576b5d03d42ee3292e1ea97f348023",
        ),
    )


def _backup_gate() -> BackupGateRecord:
    return BackupGateRecord(
        record_id="backup-gate-cm-prod-1",
        context="cm",
        instance="prod",
        created_at="2026-04-27T00:00:00Z",
        source="test",
        status="pass",
        evidence={"snapshot": "backup.tar.gz"},
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
            urls=("https://cm-prod.example/launchplane/health",),
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


def _replacement_result() -> OdooStableTargetReplacementApplyResult:
    return OdooStableTargetReplacementApplyResult(
        product="odoo-tenant-cm",
        context="cm",
        instance="prod",
        strategy="recreate-in-place",
        deployment_record_id="deployment-cm-prod",
        deploy_status="pass",
        post_deploy_status="pass",
        health_status="pass",
        canonical_status="pass",
        logo_status="pass",
        artifact_id="artifact-cm-new",
    )


class OdooProdPromotionWorkflowTests(unittest.TestCase):
    def test_promotion_request_accepts_profile_owned_context(self) -> None:
        request = OdooProdPromotionRequest(
            context=" New-Site ",
            artifact_id="artifact-new-site-123",
            backup_record_id="backup-gate-new-site-prod-1",
        )

        self.assertEqual(request.context, "new-site")

    def test_promotion_request_rejects_blank_context(self) -> None:
        with self.assertRaisesRegex(ValidationError, "requires context"):
            OdooProdPromotionRequest(
                context=" ",
                artifact_id="artifact-new-site-123",
                backup_record_id="backup-gate-new-site-prod-1",
            )

    def test_promotion_delegates_deployment_to_target_replacement(self) -> None:
        record_store = Mock()
        record_store.read_artifact_manifest.return_value = _artifact_manifest()
        record_store.read_release_tuple_record.return_value = _source_tuple()
        record_store.read_backup_gate_record.return_value = _backup_gate()
        record_store.read_deployment_record.return_value = _deployment_record()

        with (
            patch(
                "control_plane.workflows.promote.generate_promotion_record_id",
                return_value="promotion-cm-testing-to-prod",
            ),
            patch(
                "control_plane.workflows.odoo_prod_promotion.execute_odoo_stable_target_replacement_apply",
                return_value=_replacement_result(),
            ) as apply_mock,
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
                    timeout_seconds=600,
                    health_timeout_seconds=180,
                    no_cache=True,
                ),
            )

        self.assertEqual(result.promotion_status, "pass")
        self.assertEqual(result.promotion_record_id, "promotion-cm-testing-to-prod")
        self.assertEqual(result.deployment_record_id, "deployment-cm-prod")
        self.assertEqual(result.release_tuple_id, "cm-prod-artifact-cm-new")
        self.assertEqual(record_store.write_promotion_record.call_count, 2)
        record_store.write_environment_inventory.assert_called_once()
        record_store.write_release_tuple_record.assert_called_once()
        replacement_request = apply_mock.call_args.kwargs["request"]
        self.assertEqual(replacement_request.product, "odoo-tenant-cm")
        self.assertEqual(replacement_request.instance, "prod")
        self.assertEqual(replacement_request.artifact_id, "artifact-cm-new")
        self.assertEqual(replacement_request.data_source_mode, "existing")
        self.assertTrue(replacement_request.verify_health)
        self.assertTrue(replacement_request.verify_canonical)
        self.assertTrue(replacement_request.verify_logo)

    def test_failed_target_replacement_records_failed_promotion_result(self) -> None:
        record_store = Mock()
        record_store.read_artifact_manifest.return_value = _artifact_manifest()
        record_store.read_release_tuple_record.return_value = _source_tuple()
        record_store.read_backup_gate_record.return_value = _backup_gate()

        with (
            patch(
                "control_plane.workflows.promote.generate_promotion_record_id",
                return_value="promotion-cm-testing-to-prod",
            ),
            patch(
                "control_plane.workflows.odoo_prod_promotion.execute_odoo_stable_target_replacement_apply",
                side_effect=click.ClickException("target replacement failed"),
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
        self.assertIn("target replacement failed", result.error_message)
        final_record = record_store.write_promotion_record.call_args_list[-1].args[0]
        self.assertEqual(final_record.deploy.status, "fail")


if __name__ == "__main__":
    unittest.main()
