import base64
import json
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

import click

from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.odoo_instance_override_record import (
    OdooConfigParameterOverride,
    OdooInstanceOverrideRecord,
    OdooOverrideValue,
    OdooWebsiteBootstrapPayload,
    OdooWebsiteBootstrapRoute,
)
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductOdooLaneDataPolicy,
    ProductOdooStableBootstrapPolicy,
    ProductImageProfile,
    ProductLaneProfile,
)
from control_plane.dokploy import DokployTargetDefinition
from control_plane.contracts.promotion_record import ArtifactIdentityReference, DeploymentEvidence
from control_plane.workflows.odoo_post_deploy import OdooPostDeployResult
from control_plane.workflows.odoo_stable_bootstrap import (
    OdooStableBootstrapRequest,
    execute_odoo_stable_bootstrap,
)
from control_plane.workflows.odoo_stable_target_replacement import DokployRequest
from control_plane.workflows.odoo_verification import (
    OdooVerificationEvidence,
    OdooVerificationResult,
)


_BOOTSTRAP_CONFIRMATION = "bootstrap cm testing"


def _dokploy_request(path: str, query: object | None = None, **_: object) -> object:
    if path == "/api/domain.byComposeId" and query == {"composeId": "compose-cm-testing"}:
        return [{"host": "cm-testing.shinycomputers.com", "domainId": "domain-cm"}]
    return []


def _mismatched_dokploy_request(
    path: str, query: object | None = None, **_: object
) -> object:
    if path == "/api/domain.byComposeId" and query == {"composeId": "compose-cm-testing"}:
        return [{"host": "cm-prod.shinycomputers.com", "domainId": "domain-cm"}]
    return []


def _verification_result() -> OdooVerificationResult:
    return OdooVerificationResult(
        health_status="pass",
        canonical_status="pass",
        logo_status="pass",
        evidence=OdooVerificationEvidence(
            base_url="https://cm-testing.shinycomputers.com",
            health_url="https://cm-testing.shinycomputers.com/web/health",
            canonical_url="https://cm-testing.shinycomputers.com",
            logo_urls=("https://cm-testing.shinycomputers.com/web/image/website/1/logo",),
        ),
    )


def _verification_failure_result() -> OdooVerificationResult:
    return OdooVerificationResult(
        health_status="pass",
        canonical_status="fail",
        logo_status="skipped",
        evidence=OdooVerificationEvidence(
            base_url="https://cm-testing.shinycomputers.com",
            health_url="https://cm-testing.shinycomputers.com/web/health",
            canonical_url="https://cm-testing.shinycomputers.com",
        ),
        error_message="canonical failed",
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
                    odoo_stable_bootstrap=ProductOdooStableBootstrapPolicy(
                        enabled=True,
                        approval_issue_url="https://github.com/cbusillo/launchplane/issues/573",
                        confirmation=_BOOTSTRAP_CONFIRMATION,
                        expected_target_name="cm-testing",
                        expected_domains=("cm-testing.shinycomputers.com",),
                    ),
                    odoo_data_policy=ProductOdooLaneDataPolicy(
                        data_authority="resettable",
                        allowed_rebuild_sources=("empty",),
                        requires_backup_before_destroy=False,
                        requires_restore_proof=False,
                    ),
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

    def read_odoo_instance_override_record(
        self, *, context_name: str, instance_name: str
    ) -> OdooInstanceOverrideRecord:
        raise FileNotFoundError(f"{context_name}/{instance_name}")


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
                "control_plane.workflows.odoo_stable_bootstrap.verify_odoo_stable_readiness",
                return_value=_verification_result(),
            ) as verify_readiness,
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
                    confirmation=_BOOTSTRAP_CONFIRMATION,
                ),
                dokploy_request=cast(DokployRequest, _dokploy_request),
            )

        self.assertEqual(result.bootstrap_status, "pass")
        self.assertEqual(result.bootstrap_run_status, "pass")
        self.assertEqual(result.readiness_status, "pass")
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
        verify_readiness.assert_called_once_with(
            base_url="https://cm-testing.shinycomputers.com",
            health_url="https://cm-testing.shinycomputers.com/web/health",
            verify_health=True,
            verify_canonical=True,
            verify_logo=True,
            timeout_seconds=30,
            retry_interval_seconds=5,
        )
        self.assertEqual(result.health_url, "https://cm-testing.shinycomputers.com/web/health")
        self.assertEqual(result.canonical_url, "https://cm-testing.shinycomputers.com")
        self.assertEqual(
            result.logo_urls,
            ("https://cm-testing.shinycomputers.com/web/image/website/1/logo",),
        )
        self.assertEqual(result.verification_evidence.health_url, result.health_url)
        self.assertEqual(len(store.deployment_records), 2)
        self.assertEqual(store.deployment_records[-1].bootstrap.run_status, "pass")
        self.assertEqual(store.deployment_records[-1].bootstrap.readiness_status, "pass")
        self.assertEqual(store.deployment_records[-1].deploy.status, "pass")
        self.assertEqual(len(store.environment_inventories), 1)
        self.assertEqual(
            store.environment_inventories[0].deployment_record_id,
            "deployment-cm-testing-bootstrap",
        )

    def test_execute_passes_override_payload_to_bootstrap_runner(self) -> None:
        class OverrideStore(_Store):
            def read_odoo_instance_override_record(
                self, *, context_name: str, instance_name: str
            ) -> OdooInstanceOverrideRecord:
                if (context_name, instance_name) != ("cm", "testing"):
                    raise FileNotFoundError(f"{context_name}/{instance_name}")
                return OdooInstanceOverrideRecord(
                    context="cm",
                    instance="testing",
                    apply_on=("deploy",),
                    config_parameters=(
                        OdooConfigParameterOverride(
                            key="web.base.url",
                            value=OdooOverrideValue(
                                source="literal",
                                value="https://cm-testing.shinycomputers.com",
                            ),
                        ),
                    ),
                    website_bootstrap=OdooWebsiteBootstrapPayload(
                        tenant="cm",
                        name="Cell Mechanic",
                        canonical_url="https://cm-testing.shinycomputers.com",
                        homepage_url="/cell-mechanic",
                        logo_path="addons/cm_website/static/src/img/logo.png",
                        logo_alt="Cell Mechanic",
                        routes=(
                            OdooWebsiteBootstrapRoute(
                                name="Cell Mechanic",
                                url="/cell-mechanic",
                                module="cm_website",
                                homepage=True,
                            ),
                        ),
                    ),
                    updated_at="2026-05-10T00:00:00Z",
                )

        store = OverrideStore()
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
            ),
            patch(
                "control_plane.workflows.odoo_stable_bootstrap.verify_odoo_stable_readiness",
                return_value=_verification_result(),
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
                    confirmation=_BOOTSTRAP_CONFIRMATION,
                ),
                dokploy_request=cast(DokployRequest, _dokploy_request),
            )

        self.assertEqual(result.bootstrap_status, "pass")
        workflow_environment = cast(
            "dict[str, str]", captured_bootstrap_runs[0]["workflow_environment_overrides"]
        )
        self.assertIn("ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64", workflow_environment)
        decoded_payload = json.loads(
            base64.b64decode(workflow_environment["ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64"]).decode(
                "utf-8"
            )
        )
        self.assertEqual(decoded_payload["website_bootstrap"]["name"], "Cell Mechanic")
        self.assertEqual(
            decoded_payload["website_bootstrap"]["canonical_url"],
            "https://cm-testing.shinycomputers.com",
        )
        self.assertEqual(
            store.environment_inventories[0].bootstrap_record_id,
            "deployment-cm-testing-bootstrap",
        )

    def test_execute_proves_bootstrap_domain_from_live_target(self) -> None:
        store = _Store()
        store.target_record = store.target_record.model_copy(
            update={"domains": ("cellmechanic.com",)}
        )
        with (
            patch(
                "control_plane.workflows.odoo_stable_bootstrap.control_plane_dokploy.read_dokploy_config",
                return_value=("https://dokploy.example.com", "token-123"),
            ),
            patch(
                "control_plane.workflows.odoo_stable_bootstrap.control_plane_dokploy.run_compose_odoo_stable_bootstrap",
            ),
            patch(
                "control_plane.workflows.odoo_stable_bootstrap.execute_odoo_post_deploy",
                return_value=OdooPostDeployResult(
                    context="cm",
                    instance="testing",
                    phase="deploy",
                    post_deploy_status="pass",
                ),
            ),
            patch(
                "control_plane.workflows.odoo_stable_bootstrap.verify_odoo_stable_readiness",
                return_value=_verification_result(),
            ),
        ):
            result = execute_odoo_stable_bootstrap(
                control_plane_root=Path("/tmp/launchplane"),
                record_store=store,
                request=OdooStableBootstrapRequest(
                    product="odoo-tenant-cm",
                    context="cm",
                    instance="testing",
                    confirmation=_BOOTSTRAP_CONFIRMATION,
                ),
                dokploy_request=cast(DokployRequest, _dokploy_request),
            )

        self.assertEqual(result.bootstrap_status, "pass")
        self.assertEqual(store.environment_inventories[0].bootstrap.run_status, "pass")

    def test_execute_refuses_lane_without_bootstrap_policy(self) -> None:
        store = _Store()
        store.profile = store.profile.model_copy(
            update={
                "lanes": (
                    ProductLaneProfile(
                        instance="testing",
                        context="cm",
                        base_url="https://cm-testing.shinycomputers.com",
                        health_url="https://cm-testing.shinycomputers.com/web/health",
                    ),
                )
            }
        )
        with self.assertRaises(click.ClickException) as raised_error:
            execute_odoo_stable_bootstrap(
                control_plane_root=Path("/tmp/launchplane"),
                record_store=store,
                request=OdooStableBootstrapRequest(
                    product="odoo-tenant-cm",
                    context="cm",
                    instance="testing",
                    confirmation=_BOOTSTRAP_CONFIRMATION,
                ),
                dokploy_request=cast(DokployRequest, _dokploy_request),
            )

        self.assertIn("not enabled", str(raised_error.exception))

    def test_enabled_bootstrap_policy_requires_issue_backed_approval(self) -> None:
        with self.assertRaises(ValueError) as raised_error:
            ProductOdooStableBootstrapPolicy(
                enabled=True,
                confirmation=_BOOTSTRAP_CONFIRMATION,
                expected_target_name="cm-testing",
                expected_domains=("cm-testing.shinycomputers.com",),
            )

        self.assertIn("approval_issue_url", str(raised_error.exception))

    def test_execute_refuses_wrong_confirmation(self) -> None:
        store = _Store()
        with self.assertRaises(click.ClickException) as raised_error:
            execute_odoo_stable_bootstrap(
                control_plane_root=Path("/tmp/launchplane"),
                record_store=store,
                request=OdooStableBootstrapRequest(
                    product="odoo-tenant-cm",
                    context="cm",
                    instance="testing",
                    confirmation="bootstrap prod",
                ),
            )

        self.assertIn("requires confirmation", str(raised_error.exception))

    def test_execute_refuses_disallowed_data_policy_source(self) -> None:
        store = _Store()
        lane = store.profile.lanes[0].model_copy(
            update={"odoo_data_policy": ProductOdooLaneDataPolicy()}
        )
        store.profile = store.profile.model_copy(update={"lanes": (lane,)})
        with self.assertRaises(click.ClickException) as raised_error:
            execute_odoo_stable_bootstrap(
                control_plane_root=Path("/tmp/launchplane"),
                record_store=store,
                request=OdooStableBootstrapRequest(
                    product="odoo-tenant-cm",
                    context="cm",
                    instance="testing",
                    confirmation=_BOOTSTRAP_CONFIRMATION,
                ),
            )

        self.assertIn("lane data policy", str(raised_error.exception))
        self.assertIn("'empty'", str(raised_error.exception))

    def test_execute_refuses_mismatched_target_name(self) -> None:
        store = _Store()
        store.target_record = store.target_record.model_copy(update={"target_name": "cm-prod"})
        with self.assertRaises(click.ClickException) as raised_error:
            execute_odoo_stable_bootstrap(
                control_plane_root=Path("/tmp/launchplane"),
                record_store=store,
                request=OdooStableBootstrapRequest(
                    product="odoo-tenant-cm",
                    context="cm",
                    instance="testing",
                    confirmation=_BOOTSTRAP_CONFIRMATION,
                ),
                dokploy_request=cast(DokployRequest, _dokploy_request),
            )

        self.assertIn("target proof failed", str(raised_error.exception))

    def test_execute_refuses_mismatched_domain(self) -> None:
        store = _Store()
        store.target_record = store.target_record.model_copy(
            update={"domains": ("cm-prod.shinycomputers.com",)}
        )
        with (
            patch(
                "control_plane.workflows.odoo_stable_bootstrap.control_plane_dokploy.read_dokploy_config",
                return_value=("https://dokploy.example.com", "token-123"),
            ),
            self.assertRaises(click.ClickException) as raised_error,
        ):
            execute_odoo_stable_bootstrap(
                control_plane_root=Path("/tmp/launchplane"),
                record_store=store,
                request=OdooStableBootstrapRequest(
                    product="odoo-tenant-cm",
                    context="cm",
                    instance="testing",
                    confirmation=_BOOTSTRAP_CONFIRMATION,
                ),
                dokploy_request=cast(DokployRequest, _mismatched_dokploy_request),
            )

        self.assertIn("missing expected domain", str(raised_error.exception))

    def test_execute_refuses_disabled_required_verification(self) -> None:
        store = _Store()
        with self.assertRaises(click.ClickException) as raised_error:
            execute_odoo_stable_bootstrap(
                control_plane_root=Path("/tmp/launchplane"),
                record_store=store,
                request=OdooStableBootstrapRequest(
                    product="odoo-tenant-cm",
                    context="cm",
                    instance="testing",
                    confirmation=_BOOTSTRAP_CONFIRMATION,
                    verify_logo=False,
                ),
            )

        self.assertIn("requires logo verification", str(raised_error.exception))

    def test_execute_reports_missing_target_records_as_controlled_errors(self) -> None:
        request = OdooStableBootstrapRequest(
            product="odoo-tenant-cm",
            context="cm",
            instance="testing",
            confirmation=_BOOTSTRAP_CONFIRMATION,
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
                    confirmation=_BOOTSTRAP_CONFIRMATION,
                ),
                dokploy_request=cast(DokployRequest, _dokploy_request),
            )

        self.assertEqual(result.bootstrap_status, "fail")
        self.assertEqual(result.bootstrap_run_status, "fail")
        self.assertEqual(result.readiness_status, "fail")
        self.assertIn("schedule failed", result.error_message)
        self.assertEqual(store.deployment_records[-1].deploy.status, "fail")
        self.assertEqual(store.deployment_records[-1].bootstrap.run_status, "fail")
        self.assertEqual(store.deployment_records[-1].bootstrap.readiness_status, "fail")
        self.assertEqual(len(store.environment_inventories), 1)
        self.assertEqual(
            store.environment_inventories[0].deployment_record_id,
            "deployment-cm-testing-old",
        )
        self.assertEqual(
            store.environment_inventories[0].bootstrap_record_id,
            "deployment-cm-testing-bootstrap",
        )

    def test_execute_records_bootstrap_success_when_post_deploy_fails(self) -> None:
        store = _Store()
        with (
            patch(
                "control_plane.workflows.odoo_stable_bootstrap.control_plane_dokploy.read_dokploy_config",
                return_value=("https://dokploy.example.com", "token-123"),
            ),
            patch(
                "control_plane.workflows.odoo_stable_bootstrap.control_plane_dokploy.run_compose_odoo_stable_bootstrap",
                side_effect=lambda **_kwargs: None,
            ),
            patch(
                "control_plane.workflows.odoo_stable_bootstrap.execute_odoo_post_deploy",
                return_value=OdooPostDeployResult(
                    context="cm",
                    instance="testing",
                    phase="deploy",
                    post_deploy_status="fail",
                    error_message="override failed",
                ),
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
                    confirmation=_BOOTSTRAP_CONFIRMATION,
                ),
                dokploy_request=cast(DokployRequest, _dokploy_request),
            )

        self.assertEqual(result.bootstrap_status, "fail")
        self.assertEqual(result.bootstrap_run_status, "pass")
        self.assertEqual(result.readiness_status, "fail")
        self.assertEqual(result.post_deploy_status, "fail")
        self.assertEqual(store.deployment_records[-1].deploy.status, "fail")
        self.assertEqual(store.deployment_records[-1].bootstrap.run_status, "pass")
        self.assertEqual(store.deployment_records[-1].bootstrap.readiness_status, "fail")
        self.assertEqual(len(store.environment_inventories), 1)
        self.assertEqual(
            store.environment_inventories[0].deployment_record_id,
            "deployment-cm-testing-old",
        )
        self.assertEqual(
            store.environment_inventories[0].bootstrap_record_id,
            "deployment-cm-testing-bootstrap",
        )

    def test_execute_records_bootstrap_success_when_post_deploy_raises(self) -> None:
        store = _Store()
        with (
            patch(
                "control_plane.workflows.odoo_stable_bootstrap.control_plane_dokploy.read_dokploy_config",
                return_value=("https://dokploy.example.com", "token-123"),
            ),
            patch(
                "control_plane.workflows.odoo_stable_bootstrap.control_plane_dokploy.run_compose_odoo_stable_bootstrap",
                side_effect=lambda **_kwargs: None,
            ),
            patch(
                "control_plane.workflows.odoo_stable_bootstrap.execute_odoo_post_deploy",
                side_effect=click.ClickException("post-deploy exploded"),
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
                    confirmation=_BOOTSTRAP_CONFIRMATION,
                ),
                dokploy_request=cast(DokployRequest, _dokploy_request),
            )

        self.assertEqual(result.bootstrap_status, "fail")
        self.assertEqual(result.bootstrap_run_status, "pass")
        self.assertEqual(result.readiness_status, "fail")
        self.assertEqual(result.post_deploy_status, "fail")
        self.assertIn("post-deploy exploded", result.error_message)
        self.assertEqual(store.deployment_records[-1].deploy.status, "fail")
        self.assertEqual(store.deployment_records[-1].bootstrap.run_status, "pass")
        self.assertEqual(store.deployment_records[-1].bootstrap.readiness_status, "fail")
        self.assertEqual(store.deployment_records[-1].post_deploy_update.status, "fail")
        self.assertEqual(len(store.environment_inventories), 1)
        self.assertEqual(
            store.environment_inventories[0].deployment_record_id,
            "deployment-cm-testing-old",
        )
        self.assertEqual(
            store.environment_inventories[0].bootstrap_record_id,
            "deployment-cm-testing-bootstrap",
        )

    def test_execute_records_verification_failure_without_hiding_bootstrap_success(self) -> None:
        store = _Store()
        with (
            patch(
                "control_plane.workflows.odoo_stable_bootstrap.control_plane_dokploy.read_dokploy_config",
                return_value=("https://dokploy.example.com", "token-123"),
            ),
            patch(
                "control_plane.workflows.odoo_stable_bootstrap.control_plane_dokploy.run_compose_odoo_stable_bootstrap",
                side_effect=lambda **_kwargs: None,
            ),
            patch(
                "control_plane.workflows.odoo_stable_bootstrap.execute_odoo_post_deploy",
                return_value=OdooPostDeployResult(
                    context="cm",
                    instance="testing",
                    phase="deploy",
                    post_deploy_status="pass",
                ),
            ),
            patch(
                "control_plane.workflows.odoo_stable_bootstrap.verify_odoo_stable_readiness",
                return_value=_verification_failure_result(),
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
                    confirmation=_BOOTSTRAP_CONFIRMATION,
                ),
                dokploy_request=cast(DokployRequest, _dokploy_request),
            )

        self.assertEqual(result.bootstrap_status, "fail")
        self.assertEqual(result.bootstrap_run_status, "pass")
        self.assertEqual(result.readiness_status, "verification_failed")
        self.assertEqual(result.health_status, "pass")
        self.assertEqual(result.canonical_status, "fail")
        self.assertEqual(store.deployment_records[-1].deploy.status, "fail")
        self.assertEqual(store.deployment_records[-1].bootstrap.run_status, "pass")
        self.assertEqual(
            store.deployment_records[-1].bootstrap.readiness_status,
            "verification_failed",
        )
        self.assertEqual(len(store.environment_inventories), 1)
        self.assertEqual(
            store.environment_inventories[0].deployment_record_id,
            "deployment-cm-testing-old",
        )
        self.assertEqual(
            store.environment_inventories[0].bootstrap_record_id,
            "deployment-cm-testing-bootstrap",
        )

if __name__ == "__main__":
    unittest.main()
