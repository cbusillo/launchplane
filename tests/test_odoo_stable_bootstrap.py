import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

import click

from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
    ProductLaneProfile,
)
from control_plane.dokploy import DokployTargetDefinition
from control_plane.contracts.promotion_record import ArtifactIdentityReference, DeploymentEvidence
from control_plane.workflows.odoo_post_deploy import OdooPostDeployResult
from control_plane.workflows.odoo_stable_bootstrap import (
    ODOO_STABLE_BOOTSTRAP_CONFIRMATION,
    OdooStableBootstrapRequest,
    execute_odoo_stable_bootstrap,
    _run_verification_with_retry,
)


class _Store:
    def __init__(self) -> None:
        self.profile = LaunchplaneProductProfileRecord(
            product="odoo-tenant-cm",
            display_name="Odoo CM",
            repository="cbusillo/odoo-tenant-cm",
            driver_id="odoo",
            image=ProductImageProfile(repository="ghcr.io/cbusillo/odoo-tenant-cm"),
            runtime_port=8069,
            health_path="/web/health",
            lanes=(
                ProductLaneProfile(
                    instance="testing",
                    context="cm",
                    base_url="https://cm-testing.shinycomputers.com",
                    health_url="https://cm-testing.shinycomputers.com/web/health",
                ),
            ),
            updated_at="2026-05-10T00:00:00Z",
            source="test",
        )
        self.target_record = DokployTargetRecord(
            context="cm",
            instance="testing",
            project_name="odoo",
            target_type="compose",
            target_name="cm-testing",
            domains=("cm-testing.shinycomputers.com",),
            deploy_timeout_seconds=900,
            healthcheck_timeout_seconds=30,
            updated_at="2026-05-10T00:00:00Z",
        )
        self.target_id_record = DokployTargetIdRecord(
            context="cm",
            instance="testing",
            target_id="compose-cm-testing",
            updated_at="2026-05-10T00:00:00Z",
        )
        self.inventory = EnvironmentInventory(
            context="cm",
            instance="testing",
            artifact_identity=ArtifactIdentityReference(artifact_id="artifact-cm-testing"),
            source_git_ref="abc123",
            deploy=DeploymentEvidence(
                status="pass",
                target_type="compose",
                target_name="cm-testing",
                deploy_mode="dokploy-compose-api",
            ),
            updated_at="2026-05-10T00:00:00Z",
            deployment_record_id="deployment-cm-testing-old",
        )
        self.deployment_records: list[DeploymentRecord] = []
        self.environment_inventories: list[EnvironmentInventory] = []
        self.missing_target_record = False
        self.missing_target_id_record = False
        self.missing_inventory = False

    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord:
        if product != self.profile.product:
            raise FileNotFoundError(product)
        return self.profile

    def read_dokploy_target_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetRecord:
        if self.missing_target_record:
            raise FileNotFoundError(f"{context_name}/{instance_name}")
        if (context_name, instance_name) != ("cm", "testing"):
            raise FileNotFoundError(f"{context_name}/{instance_name}")
        return self.target_record

    def read_dokploy_target_id_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetIdRecord:
        if self.missing_target_id_record:
            raise FileNotFoundError(f"{context_name}/{instance_name}")
        if (context_name, instance_name) != ("cm", "testing"):
            raise FileNotFoundError(f"{context_name}/{instance_name}")
        return self.target_id_record

    def read_environment_inventory(
        self, *, context_name: str, instance_name: str
    ) -> EnvironmentInventory:
        if self.missing_inventory:
            raise FileNotFoundError(f"{context_name}/{instance_name}")
        if (context_name, instance_name) != ("cm", "testing"):
            raise FileNotFoundError(f"{context_name}/{instance_name}")
        return self.inventory

    def write_deployment_record(self, record: DeploymentRecord) -> None:
        self.deployment_records.append(record)

    def write_environment_inventory(self, record: EnvironmentInventory) -> None:
        self.environment_inventories.append(record)


class OdooStableBootstrapTests(unittest.TestCase):
    def test_execute_runs_bootstrap_post_deploy_and_verification(self) -> None:
        store = _Store()
        captured_bootstrap_runs: list[dict[str, object]] = []
        with (
            patch(
                "control_plane.workflows.odoo_stable_bootstrap.control_plane_dokploy.read_dokploy_config",
                return_value=("https://dokploy.example.com", "token-123"),
            ),
            patch(
                "control_plane.workflows.odoo_stable_bootstrap.control_plane_dokploy.run_compose_odoo_stable_bootstrap",
                side_effect=lambda **kwargs: captured_bootstrap_runs.append(kwargs),
            ),
            patch(
                "control_plane.workflows.odoo_stable_bootstrap.execute_odoo_post_deploy",
                return_value=OdooPostDeployResult(
                    context="cm",
                    instance="testing",
                    phase="deploy",
                    post_deploy_status="pass",
                ),
            ) as post_deploy_mock,
            patch(
                "control_plane.workflows.odoo_stable_bootstrap._verify_health_url",
                side_effect=lambda **_kwargs: None,
            ) as health_mock,
            patch(
                "control_plane.workflows.odoo_stable_bootstrap._verify_canonical_url",
                side_effect=lambda **_kwargs: None,
            ) as canonical_mock,
            patch(
                "control_plane.workflows.odoo_stable_bootstrap._verify_logo_route",
                side_effect=lambda **_kwargs: None,
            ) as logo_mock,
            patch(
                "control_plane.workflows.odoo_stable_bootstrap.utc_now_timestamp",
                side_effect=("2026-05-10T02:00:00Z", "2026-05-10T02:05:00Z"),
            ),
            patch(
                "control_plane.workflows.odoo_stable_bootstrap.generate_deployment_record_id",
                return_value="deployment-cm-testing-bootstrap",
            ),
        ):
            result = execute_odoo_stable_bootstrap(
                control_plane_root=Path("/tmp/launchplane"),
                record_store=store,
                request=OdooStableBootstrapRequest(
                    product="odoo-tenant-cm",
                    context="cm",
                    instance="testing",
                    confirmation=ODOO_STABLE_BOOTSTRAP_CONFIRMATION,
                ),
            )

        self.assertEqual(result.bootstrap_status, "pass")
        self.assertEqual(result.post_deploy_status, "pass")
        self.assertEqual(result.health_status, "pass")
        self.assertEqual(result.canonical_status, "pass")
        self.assertEqual(result.logo_status, "pass")
        self.assertEqual(len(captured_bootstrap_runs), 1)
        target_definition = cast(
            DokployTargetDefinition, captured_bootstrap_runs[0]["target_definition"]
        )
        self.assertEqual(target_definition.target_id, "compose-cm-testing")
        self.assertEqual(captured_bootstrap_runs[0]["timeout_seconds"], None)
        post_deploy_mock.assert_called_once()
        self.assertEqual(post_deploy_mock.call_args.kwargs["request"].phase, "deploy")
        health_mock.assert_called_once()
        self.assertEqual(
            health_mock.call_args.kwargs["health_url"],
            "https://cm-testing.shinycomputers.com/web/health",
        )
        canonical_mock.assert_called_once()
        self.assertEqual(
            canonical_mock.call_args.kwargs["base_url"],
            "https://cm-testing.shinycomputers.com",
        )
        logo_mock.assert_called_once()
        self.assertEqual(len(store.deployment_records), 2)
        self.assertEqual(store.deployment_records[-1].deploy.status, "pass")
        self.assertEqual(len(store.environment_inventories), 1)
        self.assertEqual(
            store.environment_inventories[0].deployment_record_id,
            "deployment-cm-testing-bootstrap",
        )

    def test_execute_refuses_non_cm_testing(self) -> None:
        store = _Store()
        with self.assertRaises(click.ClickException) as raised_error:
            execute_odoo_stable_bootstrap(
                control_plane_root=Path("/tmp/launchplane"),
                record_store=store,
                request=OdooStableBootstrapRequest(
                    product="odoo-tenant-cm",
                    context="cm",
                    instance="prod",
                    confirmation=ODOO_STABLE_BOOTSTRAP_CONFIRMATION,
                ),
            )

        self.assertIn("cm/testing", str(raised_error.exception))

    def test_execute_reports_missing_target_records_as_controlled_errors(self) -> None:
        request = OdooStableBootstrapRequest(
            product="odoo-tenant-cm",
            context="cm",
            instance="testing",
            confirmation=ODOO_STABLE_BOOTSTRAP_CONFIRMATION,
        )

        for missing_attribute, expected_message in (
            ("missing_target_record", "Dokploy target record"),
            ("missing_target_id_record", "Dokploy target-id record"),
            ("missing_inventory", "environment inventory"),
        ):
            with self.subTest(missing_attribute=missing_attribute):
                store = _Store()
                setattr(store, missing_attribute, True)
                with self.assertRaises(click.ClickException) as raised_error:
                    execute_odoo_stable_bootstrap(
                        control_plane_root=Path("/tmp/launchplane"),
                        record_store=store,
                        request=request,
                    )

                self.assertIn(expected_message, str(raised_error.exception))

    def test_execute_records_failure_when_bootstrap_schedule_fails(self) -> None:
        store = _Store()
        with (
            patch(
                "control_plane.workflows.odoo_stable_bootstrap.control_plane_dokploy.read_dokploy_config",
                return_value=("https://dokploy.example.com", "token-123"),
            ),
            patch(
                "control_plane.workflows.odoo_stable_bootstrap.control_plane_dokploy.run_compose_odoo_stable_bootstrap",
                side_effect=click.ClickException("schedule failed"),
            ),
            patch(
                "control_plane.workflows.odoo_stable_bootstrap.utc_now_timestamp",
                side_effect=("2026-05-10T02:00:00Z", "2026-05-10T02:01:00Z"),
            ),
            patch(
                "control_plane.workflows.odoo_stable_bootstrap.generate_deployment_record_id",
                return_value="deployment-cm-testing-bootstrap",
            ),
        ):
            result = execute_odoo_stable_bootstrap(
                control_plane_root=Path("/tmp/launchplane"),
                record_store=store,
                request=OdooStableBootstrapRequest(
                    product="odoo-tenant-cm",
                    context="cm",
                    instance="testing",
                    confirmation=ODOO_STABLE_BOOTSTRAP_CONFIRMATION,
                ),
            )

        self.assertEqual(result.bootstrap_status, "fail")
        self.assertIn("schedule failed", result.error_message)
        self.assertEqual(store.deployment_records[-1].deploy.status, "fail")
        self.assertEqual(store.environment_inventories, [])

    def test_verification_retry_allows_transient_startup_failure(self) -> None:
        attempts = 0

        def flaky_verification() -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise click.ClickException("temporary health 404")

        with patch(
            "control_plane.workflows.odoo_stable_bootstrap.time.sleep",
            side_effect=lambda _seconds: None,
        ) as sleep_mock:
            _run_verification_with_retry(
                flaky_verification,
                timeout_seconds=30,
                retry_interval_seconds=5,
            )

        self.assertEqual(attempts, 2)
        sleep_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
