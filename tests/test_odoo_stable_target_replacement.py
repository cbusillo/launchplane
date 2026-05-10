import json
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

import click

from control_plane.contracts.artifact_identity import (
    ArtifactImageReference,
    ArtifactIdentityManifest,
)
from control_plane.contracts.deployment_record import DeploymentRecord
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
from control_plane.workflows.odoo_post_deploy import OdooPostDeployResult
from control_plane.workflows.odoo_stable_target_replacement import (
    DokployRequest,
    OdooStableTargetReplacementApplyRequest,
    OdooStableTargetReplacementRequest,
    build_odoo_stable_target_replacement_plan,
    execute_odoo_stable_target_replacement_apply,
)


class _Store:
    def __init__(
        self,
        *,
        profile: LaunchplaneProductProfileRecord | None = None,
        target_record: DokployTargetRecord | None = None,
        target_id_record: DokployTargetIdRecord | None = None,
        inventory: EnvironmentInventory | None = None,
        artifact_manifest: ArtifactIdentityManifest | None = None,
        artifact_manifests: tuple[ArtifactIdentityManifest, ...] = (),
    ) -> None:
        self.profile = profile or _profile()
        self.target_record = target_record
        self.target_id_record = target_id_record
        self.inventory = inventory
        self.artifact_manifests = {
            manifest.artifact_id: manifest
            for manifest in (artifact_manifests or (artifact_manifest or _artifact_manifest(),))
        }
        self.deployment_records: list[DeploymentRecord] = []
        self.environment_inventories: list[EnvironmentInventory] = []

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

    def read_artifact_manifest(self, artifact_id: str) -> ArtifactIdentityManifest:
        if artifact_id not in self.artifact_manifests:
            raise FileNotFoundError(artifact_id)
        return self.artifact_manifests[artifact_id]

    def write_deployment_record(self, record: DeploymentRecord) -> None:
        self.deployment_records.append(record)

    def write_environment_inventory(self, record: EnvironmentInventory) -> None:
        self.environment_inventories.append(record)


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


def _artifact_manifest(
    *,
    artifact_id: str = "artifact-cm-testing",
    source_commit: str = "abc123",
    digest: str = "sha256:artifact",
) -> ArtifactIdentityManifest:
    return ArtifactIdentityManifest(
        artifact_id=artifact_id,
        source_commit=source_commit,
        enterprise_base_digest="sha256:enterprise",
        image=ArtifactImageReference(
            repository="ghcr.io/cbusillo/odoo-tenant-cm",
            digest=digest,
            tags=("testing",),
        ),
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
        self.assertIn("Launchplane has no Dokploy target record for this lane.", plan.blockers)
        self.assertIn("Launchplane has no Dokploy target-id record for this lane.", plan.blockers)

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
        self.assertIn(
            "Current target does not expose a Launchplane runtime identity yet.", plan.warnings
        )

    def test_build_plan_rejects_non_odoo_profile(self) -> None:
        with self.assertRaises(click.ClickException):
            build_odoo_stable_target_replacement_plan(
                control_plane_root=Path("."),
                record_store=_Store(profile=_profile(driver_id="generic-web")),
                request=OdooStableTargetReplacementRequest(
                    product="odoo-tenant-cm", instance="testing"
                ),
            )

    def test_apply_recreates_target_in_place_and_writes_breadcrumb_inventory(self) -> None:
        store = _Store(
            target_record=_target_record(),
            target_id_record=_target_id_record(),
            inventory=_inventory(),
        )
        persisted_env = ""

        def _fetch_target_payload(**_: object) -> JsonValue:
            return {
                "name": "cm-testing",
                "sourceType": "raw",
                "composePath": "docker-compose.yml",
                "composeFile": "services: {}",
                "env": persisted_env
                or "\n".join(
                    (
                        "ODOO_DATA_VOLUME=cm_testing_odoo_data",
                        "ODOO_LOG_VOLUME=cm_testing_odoo_logs",
                        "ODOO_DB_VOLUME=cm_testing_odoo_db",
                    )
                ),
            }

        def _update_env(*, env_text: str, **_: object) -> None:
            nonlocal persisted_env
            persisted_env = env_text

        post_deploy_result = OdooPostDeployResult(
            context="cm",
            instance="testing",
            phase="deploy",
            post_deploy_status="pass",
        )

        with (
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.control_plane_dokploy.read_dokploy_config",
                return_value=("host", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.control_plane_dokploy.fetch_dokploy_target_payload",
                side_effect=_fetch_target_payload,
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.control_plane_dokploy.latest_deployment_for_target",
                return_value={"deploymentId": "deploy-123", "status": "success"},
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.control_plane_runtime_environments.resolve_runtime_environment_values",
                return_value={"ODOO_WORKERS": "2"},
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.control_plane_dokploy.sync_dokploy_compose_raw_source"
            ) as sync_source,
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.control_plane_dokploy.ensure_compose_web_domain_route"
            ) as ensure_domain,
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.control_plane_dokploy.update_dokploy_target_env",
                side_effect=_update_env,
            ) as update_env,
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.control_plane_dokploy.trigger_deployment"
            ) as trigger_deploy,
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.control_plane_dokploy.wait_for_target_deployment"
            ) as wait_deploy,
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.execute_odoo_post_deploy",
                return_value=post_deploy_result,
            ) as post_deploy,
            patch(
                "control_plane.workflows.odoo_stable_target_replacement._verify_health_url"
            ) as verify_health,
            patch(
                "control_plane.workflows.odoo_stable_target_replacement._verify_canonical_url"
            ) as verify_canonical,
            patch(
                "control_plane.workflows.odoo_stable_target_replacement._verify_logo_route"
            ) as verify_logo,
        ):
            result = execute_odoo_stable_target_replacement_apply(
                control_plane_root=Path("."),
                record_store=store,
                request=OdooStableTargetReplacementApplyRequest(
                    product="odoo-tenant-cm", instance="testing"
                ),
                dokploy_request=cast(DokployRequest, _request),
            )

        self.assertEqual(result.deploy_status, "pass")
        self.assertEqual(result.post_deploy_status, "pass")
        self.assertEqual(result.health_status, "pass")
        self.assertEqual(result.canonical_status, "pass")
        self.assertEqual(result.logo_status, "pass")
        self.assertTrue(result.runtime_identity_injected)
        self.assertEqual(result.target_name, "cm-testing")
        self.assertEqual(
            result.image_reference,
            "ghcr.io/cbusillo/odoo-tenant-cm@sha256:artifact",
        )
        sync_source.assert_called_once()
        self.assertEqual(sync_source.call_args.kwargs["compose_name"], "cm-testing")
        ensure_domain.assert_called_once_with(
            host="host",
            token="token",
            compose_id="compose-cm-testing",
            domain_host="cm-testing.shinycomputers.com",
            runtime_port=8069,
        )
        update_env.assert_called_once()
        trigger_deploy.assert_called_once()
        wait_deploy.assert_called_once()
        post_deploy.assert_called_once()
        verify_health.assert_called_once()
        verify_canonical.assert_called_once()
        verify_logo.assert_called_once()
        self.assertIn("LAUNCHPLANE_RUNTIME_IDENTITY_JSON=", persisted_env)
        self.assertIn("LAUNCHPLANE_DEPLOYMENT_RECORD_ID=", persisted_env)
        self.assertIn("LAUNCHPLANE_ARTIFACT_ID=artifact-cm-testing", persisted_env)
        self.assertIn("ODOO_WORKERS=2", persisted_env)
        self.assertGreaterEqual(len(store.deployment_records), 2)
        final_deployment = store.deployment_records[-1]
        self.assertEqual(final_deployment.deploy.status, "pass")
        assert final_deployment.runtime_identity is not None
        self.assertEqual(final_deployment.runtime_identity.product, "odoo-tenant-cm")
        self.assertEqual(
            final_deployment.runtime_identity.deployment_record_id,
            final_deployment.record_id,
        )
        self.assertEqual(len(store.environment_inventories), 1)
        self.assertEqual(
            store.environment_inventories[0].deployment_record_id,
            final_deployment.record_id,
        )

    def test_apply_can_deploy_explicit_stored_artifact(self) -> None:
        fresh_manifest = _artifact_manifest(
            artifact_id="artifact-cm-fresh",
            source_commit="fresh-sha",
            digest="sha256:fresh",
        )
        store = _Store(
            target_record=_target_record(),
            target_id_record=_target_id_record(),
            inventory=_inventory(),
            artifact_manifests=(_artifact_manifest(), fresh_manifest),
        )
        persisted_env = ""

        def _fetch_target_payload(**_: object) -> JsonValue:
            return {
                "name": "cm-testing",
                "sourceType": "raw",
                "composePath": "docker-compose.yml",
                "composeFile": "services: {}",
                "env": persisted_env
                or "\n".join(
                    (
                        "ODOO_DATA_VOLUME=cm_testing_odoo_data",
                        "ODOO_LOG_VOLUME=cm_testing_odoo_logs",
                        "ODOO_DB_VOLUME=cm_testing_odoo_db",
                    )
                ),
            }

        def _update_env(*, env_text: str, **_: object) -> None:
            nonlocal persisted_env
            persisted_env = env_text

        with (
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.control_plane_dokploy.read_dokploy_config",
                return_value=("host", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.control_plane_dokploy.fetch_dokploy_target_payload",
                side_effect=_fetch_target_payload,
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.control_plane_dokploy.latest_deployment_for_target",
                return_value={"deploymentId": "deploy-123", "status": "success"},
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.control_plane_runtime_environments.resolve_runtime_environment_values",
                return_value={},
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.control_plane_dokploy.sync_dokploy_compose_raw_source"
            ) as sync_source,
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.control_plane_dokploy.ensure_compose_web_domain_route"
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.control_plane_dokploy.update_dokploy_target_env",
                side_effect=_update_env,
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.control_plane_dokploy.trigger_deployment"
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.control_plane_dokploy.wait_for_target_deployment"
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.execute_odoo_post_deploy",
                return_value=OdooPostDeployResult(
                    context="cm",
                    instance="testing",
                    phase="deploy",
                    post_deploy_status="pass",
                ),
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement._verify_health_url"
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement._verify_canonical_url"
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement._verify_logo_route"
            ),
        ):
            result = execute_odoo_stable_target_replacement_apply(
                control_plane_root=Path("."),
                record_store=store,
                request=OdooStableTargetReplacementApplyRequest(
                    product="odoo-tenant-cm",
                    instance="testing",
                    artifact_id="artifact-cm-fresh",
                    source_git_ref="fresh-sha",
                ),
                dokploy_request=cast(DokployRequest, _request),
            )

        self.assertEqual(result.deploy_status, "pass")
        self.assertEqual(result.artifact_id, "artifact-cm-fresh")
        self.assertEqual(
            result.image_reference,
            "ghcr.io/cbusillo/odoo-tenant-cm@sha256:fresh",
        )
        self.assertIn("LAUNCHPLANE_ARTIFACT_ID=artifact-cm-fresh", persisted_env)
        final_deployment = store.deployment_records[-1]
        self.assertEqual(final_deployment.source_git_ref, "fresh-sha")
        assert final_deployment.runtime_identity is not None
        self.assertEqual(final_deployment.runtime_identity.artifact_id, "artifact-cm-fresh")
        self.assertEqual(sync_source.call_args.kwargs["compose_name"], "cm-testing")

    def test_apply_refuses_explicit_artifact_source_mismatch(self) -> None:
        store = _Store(
            target_record=_target_record(),
            target_id_record=_target_id_record(),
            inventory=_inventory(),
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
                    "env": "\n".join(
                        (
                            "ODOO_DATA_VOLUME=cm_testing_odoo_data",
                            "ODOO_LOG_VOLUME=cm_testing_odoo_logs",
                            "ODOO_DB_VOLUME=cm_testing_odoo_db",
                        )
                    ),
                },
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.control_plane_dokploy.latest_deployment_for_target",
                return_value={"deploymentId": "deploy-123", "status": "success"},
            ),
        ):
            with self.assertRaisesRegex(click.ClickException, "source ref does not match"):
                execute_odoo_stable_target_replacement_apply(
                    control_plane_root=Path("."),
                    record_store=store,
                    request=OdooStableTargetReplacementApplyRequest(
                        product="odoo-tenant-cm",
                        instance="testing",
                        artifact_id="artifact-cm-testing",
                        source_git_ref="wrong-sha",
                    ),
                    dokploy_request=cast(DokployRequest, _request),
                )

    def test_apply_refuses_blocked_plan(self) -> None:
        with self.assertRaises(click.ClickException):
            execute_odoo_stable_target_replacement_apply(
                control_plane_root=Path("."),
                record_store=_Store(),
                request=OdooStableTargetReplacementApplyRequest(
                    product="odoo-tenant-cm", instance="testing"
                ),
            )


if __name__ == "__main__":
    unittest.main()
