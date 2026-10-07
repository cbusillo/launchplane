from dataclasses import fields
from contextlib import nullcontext
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
    def test_native_and_driver_preview_refusals_do_not_provision_or_advance_a_generation(
        self,
    ) -> None:
        from urllib.error import HTTPError
        from email.message import Message
        from control_plane.lane_movement import LaneBuild
        from control_plane.verireel_read_http import (
            VeriReelPreviewRefreshEnvelope,
            apply_verireel_preview_refresh_result,
            should_store_verireel_result_idempotency,
        )
        from control_plane.workflows.verireel_preview_driver import VeriReelPreviewRefreshRequest
        from control_plane.generic_web_preview_http import (
            apply_generic_web_preview_refresh_result,
            should_store_generic_web_preview_idempotency,
        )
        from control_plane.drivers.generic_web_preview_dispatch import (
            GenericWebPreviewRefreshEnvelope,
        )
        from control_plane.workflows.generic_web_preview import GenericWebPreviewRefreshRequest
        from tests.test_generic_web_preview_extensions import _driver_profile

        profile = _driver_profile()
        store = Mock()
        store.serialize_preview_refresh.return_value = nullcontext()
        store.list_preview_records.return_value = (Mock(state="active"),)
        store.list_deployment_records.return_value = ()
        store.read_product_profile_record.return_value = profile
        store.read_artifact_manifest.side_effect = FileNotFoundError
        desired_image = f"{profile.image.repository}@{_digest(OLDER)}"
        transport = Mock()
        with (
            patch(
                "control_plane.lane_movement.current_preview_builds",
                return_value=(
                    LaneBuild(
                        "current",
                        DEPLOYABLE,
                        f"{profile.image.repository}@{_digest(DEPLOYABLE)}",
                        pull_request_number=42,
                    ),
                ),
            ),
            patch("control_plane.lane_movement._source_transport", return_value=transport),
            patch("control_plane.verireel_read_http.execute_verireel_preview_refresh") as provider,
            patch(
                "control_plane.verireel_read_http.resolve_next_launchplane_preview_generation_identity"
            ) as generation,
        ):
            for outage in (False, True):
                transport.get_json.side_effect = (
                    HTTPError("https://api.example.test", 503, "failed", Message(), None)
                    if outage
                    else None
                )
                transport.get_json.return_value = {
                    "status": "behind",
                    "base_commit": {"sha": DEPLOYABLE},
                }
                for entry in ("native", "driver"):
                    with self.subTest(entry=entry, outage=outage):
                        if entry == "native":
                            records, result = apply_verireel_preview_refresh_result(
                                control_plane_root=Path("."),
                                record_store=store,
                                request=VeriReelPreviewRefreshEnvelope(
                                    product=profile.product,
                                    refresh=VeriReelPreviewRefreshRequest(
                                        context=profile.preview.context,
                                        anchor_repo="verireel",
                                        anchor_pr_number=42,
                                        anchor_pr_url="https://example.test/pr/42",
                                        anchor_head_sha=OLDER,
                                        preview_slug="pr-42",
                                        image_reference=desired_image,
                                    ),
                                ),
                            )
                            cached = should_store_verireel_result_idempotency(result)
                        else:
                            records, result = apply_generic_web_preview_refresh_result(
                                control_plane_root=Path("."),
                                record_store=store,
                                profile=profile,
                                request=GenericWebPreviewRefreshEnvelope(
                                    product=profile.product,
                                    refresh=GenericWebPreviewRefreshRequest(
                                        product=profile.product,
                                        anchor_pr_number=42,
                                        anchor_pr_url="https://example.test/pr/42",
                                        anchor_head_sha=OLDER,
                                        image_reference=desired_image,
                                    ),
                                ),
                            )
                            cached = should_store_generic_web_preview_idempotency(result)
                        self.assertEqual(records, {})
                        self.assertEqual(result["refresh_status"], "blocked")
                        self.assertEqual(cached, not outage)
            provider.assert_not_called()
            generation.assert_not_called()
        self.assertEqual(store.write_deployment_record.call_count, 4)

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
                    store.list_deployment_records.return_value = ()
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
