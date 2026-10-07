from dataclasses import fields
from pathlib import Path
import unittest
from typing import Literal, cast
from unittest.mock import Mock, patch

from control_plane.contracts.deployment_record import ResolvedTargetEvidence
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.promotion_record import ArtifactIdentityReference, DeploymentEvidence
from control_plane.contracts.ship_request import ShipRequest
from control_plane.lane_movement import LaneMovementRefused
from control_plane.workflows.promotion_ship_execution import ShipExecutionCallbacks, execute_ship
from control_plane.workflows.verireel_stable_deploy import (
    VeriReelStableDeployRequest,
    execute_verireel_stable_deploy,
)
from tests.support.artifact_manifests import artifact_manifest_v2
from tests.test_product_reconcile import (
    DEPLOYABLE,
    OLDER,
    FakeGenericWebGitHub,
    _generic_web_profile,
    _digest,
)


class LaneMovementExecutionTests(unittest.TestCase):
    def test_native_and_ship_ancestor_refusals_precede_all_provider_effects(self) -> None:
        profile = _generic_web_profile()
        github = FakeGenericWebGitHub()
        github.add_run(10, OLDER)
        github.add_run(20, DEPLOYABLE)
        for lane in profile.lanes:
            for executor in ("native", "ship"):
                with self.subTest(lane=lane.instance, executor=executor):
                    artifact = f"{profile.image.repository}@{_digest(OLDER)}"
                    request = ShipRequest(
                        artifact_id=artifact,
                        context=lane.context,
                        instance=lane.instance,
                        source_git_ref=OLDER,
                        target_name="target",
                        target_type="application",
                        provider_id="dokploy",
                        target_category="application",
                        provider_target_type="application",
                        deploy_mode="dokploy-application-api",
                        verify_health=False,
                    )
                    store = Mock()
                    store.read_environment_inventory.return_value = EnvironmentInventory(
                        context=lane.context,
                        instance=lane.instance,
                        source_git_ref=DEPLOYABLE,
                        artifact_identity=ArtifactIdentityReference(
                            artifact_id=f"{profile.image.repository}@{_digest(DEPLOYABLE)}"
                        ),
                        deploy=DeploymentEvidence(
                            target_name="target",
                            target_type="application",
                            deploy_mode="dokploy-application-api",
                        ),
                        updated_at="2026-10-01T12:00:00Z",
                        deployment_record_id="serving",
                    )
                    store.list_product_profile_records.return_value = (profile,)
                    store.read_artifact_manifest.side_effect = FileNotFoundError
                    target = ResolvedTargetEvidence(
                        target_type="application", target_id="target-id", target_name="target"
                    )
                    with patch(
                        "control_plane.lane_movement._source_transport", return_value=github
                    ):
                        if executor == "native":
                            with (
                                patch(
                                    "control_plane.workflows.verireel_stable_deploy._resolve_ship_request",
                                    return_value=(request, target, 300),
                                ),
                                patch(
                                    "control_plane.workflows.verireel_stable_deploy.quiesce_verireel_billing_recovery_schedule"
                                ) as quiesce,
                                patch(
                                    "control_plane.workflows.verireel_stable_deploy._execute_dokploy_deploy"
                                ) as deploy,
                            ):
                                result = execute_verireel_stable_deploy(
                                    control_plane_root=Path("."),
                                    record_store=store,
                                    request=VeriReelStableDeployRequest(
                                        context=lane.context,
                                        instance=cast(Literal["testing", "prod"], lane.instance),
                                        artifact_id=artifact,
                                        source_git_ref=OLDER,
                                    ),
                                )
                                self.assertEqual(result.deploy_status, "fail")
                                quiesce.assert_not_called()
                                deploy.assert_not_called()
                        else:
                            manifest = artifact_manifest_v2(
                                image_repository=profile.image.repository
                            ).model_copy(update={"source_commit": OLDER})
                            callback_mocks = {
                                field.name: Mock() for field in fields(ShipExecutionCallbacks)
                            }
                            callbacks = ShipExecutionCallbacks(**callback_mocks)
                            callback_mocks["require_artifact_id"].return_value = artifact
                            callback_mocks["read_artifact_manifest"].return_value = manifest
                            callback_mocks[
                                "resolve_artifact_native_execution_request"
                            ].return_value = request
                            callback_mocks["resolve_dokploy_target"].return_value = (target, 300)
                            with self.assertRaises(LaneMovementRefused):
                                execute_ship(
                                    record_store=store,
                                    env_file=None,
                                    request=request,
                                    mint_release_tuple=False,
                                    callbacks=callbacks,
                                )
                            callback_mocks[
                                "sync_artifact_image_reference_for_target"
                            ].assert_not_called()
                            callback_mocks["execute_dokploy_deploy"].assert_not_called()
                    final = store.write_deployment_record.call_args.args[0]
                    self.assertEqual(final.deploy.status, "fail")
                    self.assertEqual(final.failure.code, "lane_movement.ancestor_build")
