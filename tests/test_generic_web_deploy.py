import unittest
from pathlib import Path
from unittest.mock import patch

import click

from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
    ProductLaneProfile,
    ProductPreviewProfile,
)
from control_plane.dokploy import DokploySourceOfTruth, DokployTargetDefinition
from control_plane.workflows.generic_web_deploy import (
    GenericWebDeployRequest,
    execute_generic_web_deploy,
    resolve_generic_web_profile_lane,
)


class _GenericWebDeployStore:
    def __init__(self, profile: LaunchplaneProductProfileRecord) -> None:
        self.profile = profile
        self.deployments = []

    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord:
        if product != self.profile.product:
            raise FileNotFoundError(product)
        return self.profile

    def write_deployment_record(self, record) -> None:
        self.deployments.append(record)


def _profile() -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord(
        product="sellyouroutboard",
        display_name="SellYourOutboard.com",
        repository="cbusillo/sellyouroutboard",
        driver_id="generic-web",
        image=ProductImageProfile(repository="ghcr.io/cbusillo/sellyouroutboard"),
        runtime_port=3000,
        health_path="/api/health",
        lanes=(
            ProductLaneProfile(
                instance="testing",
                context="sellyouroutboard-testing",
                base_url="https://testing.sellyouroutboard.com",
                health_url="https://testing.sellyouroutboard.com/api/health",
            ),
        ),
        preview=ProductPreviewProfile(
            enabled=True,
            context="sellyouroutboard-testing",
            slug_template="pr-{number}",
        ),
        updated_at="2026-04-30T21:00:00Z",
        source="test",
    )


def _request(instance: str = "testing") -> GenericWebDeployRequest:
    return GenericWebDeployRequest(
        product="sellyouroutboard",
        instance=instance,
        artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
        source_git_ref="abc123",
    )


def _source_of_truth() -> DokploySourceOfTruth:
    return DokploySourceOfTruth(
        schema_version=1,
        targets=(
            DokployTargetDefinition(
                context="sellyouroutboard-testing",
                instance="testing",
                target_type="application",
                target_id="target-123",
                target_name="sellyouroutboard-testing-app",
            ),
        ),
    )


class GenericWebDeployTests(unittest.TestCase):
    def test_execute_generic_web_deploy_writes_pass_record_for_profile_lane(self) -> None:
        store = _GenericWebDeployStore(_profile())

        with (
            patch(
                "control_plane.workflows.generic_web_deploy.control_plane_dokploy.read_control_plane_dokploy_source_of_truth",
                return_value=_source_of_truth(),
            ),
            patch(
                "control_plane.workflows.generic_web_deploy.control_plane_runtime_environments.resolve_runtime_environment_values",
                return_value={},
            ),
            patch(
                "control_plane.workflows.generic_web_deploy.control_plane_dokploy.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch("control_plane.workflows.generic_web_deploy.execute_dokploy_artifact_deploy") as deploy,
        ):
            result = execute_generic_web_deploy(
                control_plane_root=Path("."),
                record_store=store,
                request=_request(),
            )

        self.assertEqual(result.deploy_status, "pass")
        self.assertEqual(result.context, "sellyouroutboard-testing")
        self.assertEqual(result.target_id, "target-123")
        self.assertEqual(len(store.deployments), 1)
        self.assertEqual(store.deployments[0].deploy.status, "pass")
        self.assertEqual(store.deployments[0].resolved_target.target_name, "sellyouroutboard-testing-app")
        deploy.assert_called_once()

    def test_execute_generic_web_deploy_records_failure_when_provider_fails(self) -> None:
        store = _GenericWebDeployStore(_profile())

        with (
            patch(
                "control_plane.workflows.generic_web_deploy.control_plane_dokploy.read_control_plane_dokploy_source_of_truth",
                return_value=_source_of_truth(),
            ),
            patch(
                "control_plane.workflows.generic_web_deploy.control_plane_runtime_environments.resolve_runtime_environment_values",
                return_value={},
            ),
            patch(
                "control_plane.workflows.generic_web_deploy.control_plane_dokploy.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.generic_web_deploy.execute_dokploy_artifact_deploy",
                side_effect=click.ClickException("provider failed"),
            ),
        ):
            result = execute_generic_web_deploy(
                control_plane_root=Path("."),
                record_store=store,
                request=_request(),
            )

        self.assertEqual(result.deploy_status, "fail")
        self.assertEqual(result.error_message, "provider failed")
        self.assertEqual(len(store.deployments), 1)
        self.assertEqual(store.deployments[0].deploy.status, "fail")

    def test_resolve_generic_web_profile_lane_rejects_missing_lane(self) -> None:
        store = _GenericWebDeployStore(_profile())

        with self.assertRaises(click.ClickException):
            resolve_generic_web_profile_lane(record_store=store, request=_request(instance="prod"))

        self.assertEqual(store.deployments, [])


if __name__ == "__main__":
    unittest.main()
