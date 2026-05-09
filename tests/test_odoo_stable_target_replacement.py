import json
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

import click

from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
    ProductLaneProfile,
    ProductPreviewProfile,
)
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    DeploymentEvidence,
)
from control_plane.dokploy import JsonValue
from control_plane.workflows.odoo_stable_target_replacement import (
    DokployRequest,
    OdooStableTargetReplacementRequest,
    build_odoo_stable_target_replacement_plan,
)


class _Store:
    def __init__(
        self,
        *,
        profile: LaunchplaneProductProfileRecord | None = None,
        target_record: DokployTargetRecord | None = None,
        target_id_record: DokployTargetIdRecord | None = None,
        inventory: EnvironmentInventory | None = None,
    ) -> None:
        self.profile = profile or _profile()
        self.target_record = target_record
        self.target_id_record = target_id_record
        self.inventory = inventory

    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord:
        if product != self.profile.product:
            raise FileNotFoundError(product)
        return self.profile

    def read_dokploy_target_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetRecord:
        if self.target_record is None:
            raise FileNotFoundError(f"{context_name}/{instance_name}")
        return self.target_record

    def read_dokploy_target_id_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetIdRecord:
        if self.target_id_record is None:
            raise FileNotFoundError(f"{context_name}/{instance_name}")
        return self.target_id_record

    def read_environment_inventory(
        self, *, context_name: str, instance_name: str
    ) -> EnvironmentInventory:
        if self.inventory is None:
            raise FileNotFoundError(f"{context_name}/{instance_name}")
        return self.inventory


def _profile(driver_id: str = "odoo") -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord(
        product="odoo-tenant-cm",
        display_name="Odoo CM",
        repository="cbusillo/odoo-tenant-cm",
        driver_id=driver_id,
        image=ProductImageProfile(repository="ghcr.io/cbusillo/odoo-tenant-cm"),
        runtime_port=8069,
        health_path="/web/health",
        lanes=(ProductLaneProfile(instance="testing", context="cm"),),
        preview=ProductPreviewProfile(enabled=True, context="cm"),
        updated_at="2026-05-09T00:00:00Z",
        source="test",
    )


def _target_record(target_type: str = "compose") -> DokployTargetRecord:
    return DokployTargetRecord(
        context="cm",
        instance="testing",
        project_name="odoo",
        target_type=target_type,  # type: ignore[arg-type]
        target_name="cm-testing",
        domains=("cm-testing.shinycomputers.com",),
        updated_at="2026-05-09T00:00:00Z",
    )


def _target_id_record() -> DokployTargetIdRecord:
    return DokployTargetIdRecord(
        context="cm",
        instance="testing",
        target_id="compose-cm-testing",
        updated_at="2026-05-09T00:00:00Z",
    )


def _inventory() -> EnvironmentInventory:
    return EnvironmentInventory(
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
        updated_at="2026-05-09T00:00:00Z",
        deployment_record_id="deployment-cm-testing",
    )


def _request(path: str, query: object | None = None, **_: object) -> JsonValue:
    if path == "/api/domain.byComposeId" and query == {"composeId": "compose-cm-testing"}:
        return [{"host": "cm-testing.shinycomputers.com", "domainId": "domain-cm"}]
    return []


class OdooStableTargetReplacementTests(unittest.TestCase):
    def test_build_plan_reports_ready_when_records_and_volume_contract_exist(self) -> None:
        identity = json.dumps(
            {"deployment_record_id": "deployment-cm-testing", "artifact_id": "artifact"}
        )
        with (
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.control_plane_dokploy.read_dokploy_config",
                return_value=("host", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.control_plane_dokploy.fetch_dokploy_target_payload",
                return_value={
                    "name": "cm-testing",
                    "sourceType": "raw",
                    "composePath": "docker-compose.yml",
                    "composeFile": "services: {}",
                    "env": "\n".join(
                        (
                            "ODOO_DATA_VOLUME=cm_testing_odoo_data",
                            "ODOO_LOG_VOLUME=cm_testing_odoo_logs",
                            "ODOO_DB_VOLUME=cm_testing_odoo_db",
                            f"LAUNCHPLANE_RUNTIME_IDENTITY_JSON={identity}",
                        )
                    ),
                },
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.control_plane_dokploy.latest_deployment_for_target",
                return_value={"deploymentId": "deploy-123", "status": "success"},
            ),
        ):
            plan = build_odoo_stable_target_replacement_plan(
                control_plane_root=Path("."),
                record_store=_Store(
                    target_record=_target_record(),
                    target_id_record=_target_id_record(),
                    inventory=_inventory(),
                ),
                request=OdooStableTargetReplacementRequest(
                    product="odoo-tenant-cm", instance="testing"
                ),
                dokploy_request=cast(DokployRequest, _request),
            )

        self.assertEqual(plan.plan_status, "ready")
        self.assertEqual(plan.context, "cm")
        self.assertEqual(plan.expected_artifact_id, "artifact-cm-testing")
        self.assertIsNotNone(plan.current_target)
        assert plan.current_target is not None
        self.assertEqual(plan.current_target.required_volume_keys_missing, ())
        self.assertTrue(plan.current_target.runtime_identity_present)
        self.assertEqual(plan.current_target.domain_hosts, ("cm-testing.shinycomputers.com",))

    def test_build_plan_blocks_without_target_records(self) -> None:
        plan = build_odoo_stable_target_replacement_plan(
            control_plane_root=Path("."),
            record_store=_Store(),
            request=OdooStableTargetReplacementRequest(
                product="odoo-tenant-cm", instance="testing"
            ),
        )

        self.assertEqual(plan.plan_status, "blocked")
        self.assertIn(
            "Launchplane has no Dokploy target record for this lane.", plan.blockers
        )
        self.assertIn(
            "Launchplane has no Dokploy target-id record for this lane.", plan.blockers
        )

    def test_build_plan_blocks_missing_volume_contract(self) -> None:
        with (
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.control_plane_dokploy.read_dokploy_config",
                return_value=("host", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.control_plane_dokploy.fetch_dokploy_target_payload",
                return_value={"name": "cm-testing", "env": "ODOO_DATA_VOLUME=data"},
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.control_plane_dokploy.latest_deployment_for_target",
                return_value=None,
            ),
        ):
            plan = build_odoo_stable_target_replacement_plan(
                control_plane_root=Path("."),
                record_store=_Store(
                    target_record=_target_record(),
                    target_id_record=_target_id_record(),
                ),
                request=OdooStableTargetReplacementRequest(
                    product="odoo-tenant-cm", instance="testing"
                ),
                dokploy_request=cast(DokployRequest, _request),
            )

        self.assertEqual(plan.plan_status, "blocked")
        self.assertIn("ODOO_LOG_VOLUME", plan.blockers[0])
        self.assertIn("ODOO_DB_VOLUME", plan.blockers[0])
        self.assertIn("Current target does not expose a Launchplane runtime identity yet.", plan.warnings)

    def test_build_plan_rejects_non_odoo_profile(self) -> None:
        with self.assertRaises(click.ClickException):
            build_odoo_stable_target_replacement_plan(
                control_plane_root=Path("."),
                record_store=_Store(profile=_profile(driver_id="generic-web")),
                request=OdooStableTargetReplacementRequest(
                    product="odoo-tenant-cm", instance="testing"
                ),
            )


if __name__ == "__main__":
    unittest.main()
