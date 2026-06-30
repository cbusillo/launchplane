import unittest
from pathlib import Path
from typing import Literal, cast
from unittest.mock import Mock, patch

import click
from click import Command
from click.testing import CliRunner
from pydantic import ValidationError

from control_plane.cli import main
from control_plane.contracts.artifact_identity import (
    ArtifactIdentityManifest,
    ArtifactImageReference,
)
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    BackupGateEvidence,
    DeploymentEvidence,
    HealthcheckEvidence,
    PostDeployUpdateEvidence,
    PromotionRecord,
)
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.contracts.odoo_stable_target_replacement import (
    OdooStableTargetReplacementApplyResult,
)
from control_plane.workflows.odoo_prod_rollback import (
    OdooProdRollbackRequest,
    execute_odoo_prod_rollback,
)


CLI_MAIN = cast(Command, main)


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


def _deployment_record() -> DeploymentRecord:
    return DeploymentRecord(
        record_id="deployment-opw-prod-rollback",
        artifact_identity=ArtifactIdentityReference(artifact_id="artifact-opw-847c71c1db61785c"),
        context="opw",
        instance="prod",
        source_git_ref="9e09b858e1f93aa4a1f4b887b528ba7e5a999ee6",
        deploy=DeploymentEvidence(
            target_name="opw-prod",
            target_type="compose",
            deploy_mode="dokploy-compose-api",
            deployment_id="control-plane-dokploy",
            status="pass",
        ),
        post_deploy_update=PostDeployUpdateEvidence(attempted=True, status="pass"),
        destination_health=HealthcheckEvidence(
            verified=True,
            urls=("https://opw-prod.shinycomputers.com/web/health",),
            timeout_seconds=180,
            status="pass",
        ),
    )


def _replacement_result(
    *,
    deploy_status: Literal["pass", "fail"] = "pass",
    post_deploy_status: Literal["pass", "fail", "skipped"] = "pass",
    health_status: Literal["pass", "fail", "skipped"] = "pass",
    release_tuple_id: str = "opw-prod-artifact-opw-847c71c1db61785c",
    error_message: str = "",
) -> OdooStableTargetReplacementApplyResult:
    return OdooStableTargetReplacementApplyResult(
        product="odoo-tenant-opw",
        context="opw",
        instance="prod",
        strategy="recreate-in-place",
        deployment_record_id="deployment-opw-prod-rollback",
        release_tuple_id=release_tuple_id,
        deploy_status=deploy_status,
        post_deploy_status=post_deploy_status,
        health_status=health_status,
        canonical_status="pass",
        logo_status="pass",
        artifact_id="artifact-opw-847c71c1db61785c",
        image_reference="ghcr.io/cbusillo/odoo-tenant-opw@sha256:847c71c1db61785c0aa265949f45a74c5dd9535e62c89db26d5650684c340100",
        error_message=error_message,
    )


class OdooProdRollbackWorkflowTests(unittest.TestCase):
    def test_rollback_request_accepts_profile_owned_context(self) -> None:
        request = OdooProdRollbackRequest(context=" New-Site ")

        self.assertEqual(request.context, "new-site")

    def test_rollback_request_rejects_blank_context(self) -> None:
        with self.assertRaisesRegex(ValidationError, "requires context"):
            OdooProdRollbackRequest(context=" ")

    def test_rollback_request_rejects_non_testing_source_channel(self) -> None:
        with self.assertRaisesRegex(ValidationError, "Input should be 'testing'"):
            OdooProdRollbackRequest(context="opw", source_channel="prod")  # type: ignore[arg-type]

    def _record_store(self) -> Mock:
        record_store = Mock()
        record_store.read_release_tuple_record.return_value = _release_tuple()
        record_store.read_artifact_manifest.return_value = _artifact_manifest()
        record_store.read_environment_inventory.return_value = _inventory_record()
        record_store.read_promotion_record.return_value = _promotion_record()
        record_store.read_deployment_record.return_value = _deployment_record()
        return record_store

    def test_rollback_to_testing_tuple_delegates_to_target_replacement(self) -> None:
        record_store = self._record_store()

        with patch(
            "control_plane.workflows.odoo_prod_rollback.execute_odoo_stable_target_replacement_apply",
            return_value=_replacement_result(),
        ) as replacement_apply:
            result = execute_odoo_prod_rollback(
                control_plane_root=Path("/control-plane"),
                record_store=record_store,
                request=OdooProdRollbackRequest(context="opw"),
            )

        self.assertEqual(result.rollback_status, "pass")
        self.assertEqual(result.rollback_health_status, "pass")
        self.assertEqual(result.post_deploy_status, "pass")
        self.assertEqual(result.deployment_record_id, "deployment-opw-prod-rollback")
        self.assertEqual(result.release_tuple_id, "opw-prod-artifact-opw-847c71c1db61785c")
        replacement_apply.assert_called_once()
        replacement_request = replacement_apply.call_args.kwargs["request"]
        self.assertEqual(replacement_request.product, "odoo-tenant-opw")
        self.assertEqual(replacement_request.instance, "prod")
        self.assertEqual(replacement_request.artifact_id, "artifact-opw-847c71c1db61785c")
        self.assertEqual(
            replacement_request.source_git_ref,
            "9e09b858e1f93aa4a1f4b887b528ba7e5a999ee6",
        )
        self.assertTrue(replacement_request.verify_canonical)
        self.assertTrue(replacement_request.verify_logo)
        record_store.write_deployment_record.assert_not_called()
        record_store.write_release_tuple_record.assert_not_called()
        record_store.write_environment_inventory.assert_called_once()
        inventory = record_store.write_environment_inventory.call_args.args[0]
        self.assertEqual(inventory.deployment_record_id, "deployment-opw-prod-rollback")
        self.assertEqual(inventory.promotion_record_id, _promotion_record().record_id)
        self.assertEqual(inventory.promoted_from_instance, "testing")
        final_promotion = record_store.write_promotion_record.call_args_list[-1].args[0]
        self.assertEqual(final_promotion.rollback.status, "pass")
        self.assertEqual(final_promotion.rollback_health.status, "pass")

    def test_explicit_artifact_rolls_back_without_testing_tuple_match(self) -> None:
        record_store = self._record_store()
        record_store.read_artifact_manifest.return_value = _previous_prod_artifact_manifest()

        with patch(
            "control_plane.workflows.odoo_prod_rollback.execute_odoo_stable_target_replacement_apply",
            return_value=_replacement_result(
                release_tuple_id="opw-prod-artifact-opw-previous-prod"
            ),
        ) as replacement_apply:
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
        replacement_request = replacement_apply.call_args.kwargs["request"]
        self.assertEqual(replacement_request.artifact_id, "artifact-opw-previous-prod")
        self.assertEqual(replacement_request.source_git_ref, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        final_promotion = record_store.write_promotion_record.call_args_list[-1].args[0]
        self.assertEqual(
            final_promotion.rollback.snapshot_name, "artifact:artifact-opw-previous-prod"
        )
        inventory = record_store.write_environment_inventory.call_args.args[0]
        self.assertEqual(inventory.promoted_from_instance, "explicit-artifact")

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
        record_store.write_promotion_record.assert_not_called()

    def test_failed_deploy_records_failed_rollback(self) -> None:
        record_store = self._record_store()

        with patch(
            "control_plane.workflows.odoo_prod_rollback.execute_odoo_stable_target_replacement_apply",
            return_value=_replacement_result(
                deploy_status="fail",
                post_deploy_status="skipped",
                health_status="skipped",
                release_tuple_id="",
                error_message="deploy failed",
            ),
        ):
            result = execute_odoo_prod_rollback(
                control_plane_root=Path("/control-plane"),
                record_store=record_store,
                request=OdooProdRollbackRequest(context="opw"),
            )

        self.assertEqual(result.rollback_status, "fail")
        self.assertEqual(result.deployment_record_id, "deployment-opw-prod-rollback")
        self.assertIn("deploy failed", result.error_message)
        final_promotion = record_store.write_promotion_record.call_args_list[-1].args[0]
        self.assertEqual(final_promotion.rollback.status, "fail")

    def test_rollback_cli_group_is_retired(self) -> None:
        result = CliRunner().invoke(
            CLI_MAIN,
            [
                "odoo-rollbacks",
            ],
        )

        self.assertEqual(result.exit_code, 2, result.output)
        self.assertIn("No such command 'odoo-rollbacks'", result.output)

    def test_main_help_omits_retired_rollback_group(self) -> None:
        result = CliRunner().invoke(CLI_MAIN, ["--help"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("odoo-rollbacks", result.output)


if __name__ == "__main__":
    unittest.main()
