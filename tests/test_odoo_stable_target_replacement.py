import base64
import json
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

import click

from control_plane import dokploy as control_plane_dokploy
from control_plane.odoo_instance_overrides import LAUNCHPLANE_INSTANCE_OVERRIDES_REQUIRED_ENV_KEY
from control_plane.odoo_instance_overrides import LAUNCHPLANE_WEBSITE_BOOTSTRAP_REQUIRED_ENV_KEY
from control_plane.odoo_instance_overrides import ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY
from control_plane.contracts.artifact_identity import (
    ArtifactImageReference,
    ArtifactIdentityManifest,
)
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.odoo_instance_override_record import OdooAddonSettingOverride
from control_plane.contracts.odoo_instance_override_record import OdooInstanceOverrideRecord
from control_plane.contracts.odoo_instance_override_record import OdooOverrideValue
from control_plane.contracts.odoo_instance_override_record import OdooWebsiteBootstrapPayload
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductExpectedConfigProfile,
    ProductImageProfile,
    ProductLaneProfile,
    ProductOdooLaneDataPolicy,
    ProductOdooPrelaunchRebuildPolicy,
    ProductPreviewProfile,
    ProductLaneHealthMonitoringPolicy,
    ProductSecretConfigRequirement,
)
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    DeploymentEvidence,
    HealthcheckEvidence,
)
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.runtime_key_safety_policy import (
    RuntimeKeySafetyPolicyRecord,
    RuntimeSecretSafetyRule,
)
from control_plane.contracts.secret_record import SecretBinding
from control_plane.contracts.odoo_stable_target_replacement import (
    OdooStableTargetReplacementApplyRequest,
    OdooStableTargetReplacementRequest,
)
from control_plane.dokploy import JsonObject, JsonValue
from control_plane.workflows.odoo_post_deploy import OdooPostDeployResult
from control_plane.workflows.odoo_stable_target_replacement import (
    DokployRequest,
    _merge_required_odoo_install_modules,
    _read_lane,
    build_odoo_stable_target_replacement_plan,
    execute_odoo_stable_target_replacement_apply,
    _target_health_url,
    _verify_required_runtime_identity_evidence,
)
from control_plane.workflows.runtime_identity_health import (
    HealthcheckPass,
    RuntimeIdentityHealthcheckError,
)
from control_plane.workflows.odoo_verification import (
    OdooVerificationEvidence,
    OdooVerificationResult,
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
        odoo_instance_override_record: OdooInstanceOverrideRecord | None = None,
        runtime_environment_records: tuple[RuntimeEnvironmentRecord, ...] | None = None,
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
        self.release_tuples: list[object] = []
        self.secret_bindings: tuple[SecretBinding, ...] = ()
        self.runtime_key_safety_policy_records: tuple[RuntimeKeySafetyPolicyRecord, ...] = ()
        self.odoo_instance_override_record = odoo_instance_override_record
        self.runtime_environment_records = (
            runtime_environment_records
            if runtime_environment_records is not None
            else _runtime_environment_records_for_profile(self.profile)
        )

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

    def read_odoo_instance_override_record(
        self, *, context_name: str, instance_name: str
    ) -> OdooInstanceOverrideRecord:
        if self.odoo_instance_override_record is None:
            raise FileNotFoundError(f"{context_name}/{instance_name}")
        return self.odoo_instance_override_record

    def list_runtime_environment_records(
        self, *, context_name: str = "", instance_name: str = ""
    ) -> tuple[RuntimeEnvironmentRecord, ...]:
        return tuple(
            record
            for record in self.runtime_environment_records
            if (not context_name or record.context == context_name)
            and (not instance_name or record.instance == instance_name)
        )

    def write_deployment_record(self, record: DeploymentRecord) -> None:
        self.deployment_records.append(record)

    def write_environment_inventory(self, record: EnvironmentInventory) -> None:
        self.environment_inventories.append(record)

    def write_odoo_instance_override_record(self, record: OdooInstanceOverrideRecord) -> None:
        self.odoo_instance_override_record = record

    def write_release_tuple_record(self, record: object) -> None:
        self.release_tuples.append(record)

    def list_secret_bindings(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[SecretBinding, ...]:
        records = tuple(
            binding
            for binding in self.secret_bindings
            if (not integration or binding.integration == integration)
            and (not context_name or binding.context == context_name)
            and (not instance_name or binding.instance == instance_name)
        )
        return records[:limit] if limit is not None else records

    def list_runtime_key_safety_policy_records(
        self,
        *,
        status: str = "",
        limit: int | None = None,
    ) -> tuple[RuntimeKeySafetyPolicyRecord, ...]:
        records = tuple(
            record
            for record in self.runtime_key_safety_policy_records
            if not status or record.status == status
        )
        return records[:limit] if limit is not None else records


def _profile(driver_id: str = "odoo") -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord(
        product="odoo-tenant-cm",
        display_name="Odoo CM",
        repository="cbusillo/odoo-tenant-cm",
        driver_id=driver_id,
        image=ProductImageProfile(repository="ghcr.io/cbusillo/odoo-tenant-cm"),
        runtime_port=8069,
        health_path="/web/health",
        lanes=(
            ProductLaneProfile(
                instance="testing",
                context="cm",
                health_monitoring=ProductLaneHealthMonitoringPolicy(checks=()),
            ),
        ),
        preview=ProductPreviewProfile(enabled=True, context="cm"),
        updated_at="2026-05-09T00:00:00Z",
        source="test",
    )


def _runtime_environment_records_for_profile(
    profile: LaunchplaneProductProfileRecord,
) -> tuple[RuntimeEnvironmentRecord, ...]:
    lane = profile.lanes[0]
    volume_prefix = f"{lane.context}_{lane.instance}"
    return (
        RuntimeEnvironmentRecord(
            scope="instance",
            context=lane.context,
            instance=lane.instance,
            env={
                "ODOO_DATA_VOLUME": f"{volume_prefix}_odoo_data",
                "ODOO_LOG_VOLUME": f"{volume_prefix}_odoo_logs",
                "ODOO_DB_VOLUME": f"{volume_prefix}_odoo_db",
            },
            updated_at="2026-07-25T00:00:00Z",
            source_label="test",
        ),
    )


def _profile_with_runtime_secret(
    *, binding_key: str = "ODOO_DB_PASSWORD"
) -> LaunchplaneProductProfileRecord:
    return _profile().model_copy(
        update={
            "expected_config": ProductExpectedConfigProfile(
                managed_secret_bindings=(
                    ProductSecretConfigRequirement(
                        binding_key=binding_key,
                        context="cm",
                        instance="testing",
                    ),
                )
            )
        }
    )


class OdooStableTargetReplacementLaneTests(unittest.TestCase):
    def test_legacy_profile_with_ambiguous_instance_fails_closed(self) -> None:
        profile = _profile().model_copy(
            update={
                "lanes": (
                    ProductLaneProfile(instance="prod", context="cm"),
                    ProductLaneProfile(instance="prod", context="cm-website"),
                )
            }
        )

        with self.assertRaisesRegex(click.ClickException, "multiple stable lanes"):
            _read_lane(profile=profile, instance="prod")


def _verification_result() -> OdooVerificationResult:
    return OdooVerificationResult(
        health_status="pass",
        canonical_status="pass",
        logo_status="pass",
        evidence=OdooVerificationEvidence(
            base_url="https://cm-testing.shinycomputers.com",
            health_url="https://cm-testing.shinycomputers.com/launchplane/health",
            canonical_url="https://cm-testing.shinycomputers.com",
            logo_urls=("https://cm-testing.shinycomputers.com/web/image/website/1/logo",),
        ),
    )


def _matching_runtime_identity_healthcheck(
    *, expected_runtime_identity: RuntimeIdentity, **_: object
) -> HealthcheckPass:
    return HealthcheckPass(
        payload={"runtime_identity": expected_runtime_identity.model_dump(mode="json")}
    )


def _opw_profile_with_prelaunch_policy(*, enabled: bool) -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord(
        product="odoo-tenant-opw",
        display_name="Odoo OPW",
        repository="cbusillo/odoo-tenant-opw",
        driver_id="odoo",
        image=ProductImageProfile(repository="ghcr.io/cbusillo/odoo-tenant-opw"),
        runtime_port=8069,
        health_path="/web/health",
        lanes=(
            ProductLaneProfile(
                instance="prod",
                context="opw",
                odoo_prelaunch_rebuild=ProductOdooPrelaunchRebuildPolicy(
                    enabled=enabled,
                    approval_issue_url="https://github.com/cbusillo/launchplane/issues/573"
                    if enabled
                    else "",
                    data_source_mode="upstream_restore",
                    confirmation="restore opw upstream" if enabled else "",
                    expected_target_name="opw-prod" if enabled else "",
                    expected_domains=("opw-prod.shinycomputers.com",) if enabled else (),
                ),
                odoo_data_policy=ProductOdooLaneDataPolicy(
                    data_authority="restorable" if enabled else "unknown",
                    allowed_rebuild_sources=("upstream_restore",) if enabled else (),
                    upstream_source="odoo-tenant-opw/opw/prod-upstream" if enabled else "",
                ),
                base_url="https://opw-prod.shinycomputers.com",
            ),
        ),
        updated_at="2026-05-10T00:00:00Z",
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


def _opw_target_record() -> DokployTargetRecord:
    return DokployTargetRecord(
        context="opw",
        instance="prod",
        project_name="odoo",
        target_type="compose",
        target_name="opw-prod",
        domains=("opw-prod.shinycomputers.com",),
        updated_at="2026-05-10T00:00:00Z",
    )


def _opw_target_record_with_stale_domain() -> DokployTargetRecord:
    return DokployTargetRecord(
        context="opw",
        instance="prod",
        project_name="odoo",
        target_type="compose",
        target_name="opw-prod",
        domains=("openwater.pro",),
        updated_at="2026-05-10T00:00:00Z",
    )


def _opw_target_id_record() -> DokployTargetIdRecord:
    return DokployTargetIdRecord(
        context="opw",
        instance="prod",
        target_id="compose-opw-prod",
        updated_at="2026-05-10T00:00:00Z",
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
        source_git_ref="abc1234",
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
    source_commit: str = "abc1234",
    digest: str = "sha256:artifact",
    image_repository: str = "ghcr.io/cbusillo/odoo-tenant-cm",
    odoo_install_modules: tuple[str, ...] = (
        "launchplane_settings",
        "disable_odoo_online",
    ),
) -> ArtifactIdentityManifest:
    return ArtifactIdentityManifest(
        artifact_id=artifact_id,
        source_commit=source_commit,
        enterprise_base_digest="sha256:enterprise",
        odoo_install_modules=odoo_install_modules,
        image=ArtifactImageReference(
            repository=image_repository,
            digest=digest,
            tags=("testing",),
        ),
    )


def _request(path: str, query: object | None = None, **_: object) -> JsonValue:
    if path == "/api/domain.byComposeId" and query == {"composeId": "compose-cm-testing"}:
        return [{"host": "cm-testing.shinycomputers.com", "domainId": "domain-cm"}]
    if path == "/api/domain.byComposeId" and query == {"composeId": "compose-opw-prod"}:
        return [{"host": "opw-prod.shinycomputers.com", "domainId": "domain-opw"}]
    return []


class OdooStableTargetReplacementTests(unittest.TestCase):
    def test_merge_required_odoo_install_modules_prepends_and_dedupes(self) -> None:
        self.assertEqual(
            _merge_required_odoo_install_modules("cm_website, disable_odoo_online, website"),
            "launchplane_settings,disable_odoo_online,cm_website,website",
        )

    def test_required_runtime_identity_evidence_records_exact_match(self) -> None:
        expected_runtime_identity = RuntimeIdentity(
            product="odoo-tenant-cm",
            context="cm",
            instance="testing",
            deployment_record_id="deployment-current",
            artifact_id="artifact-current",
            source_git_ref="abc1234",
        )

        with patch(
            "control_plane.workflows.odoo_stable_target_replacement.wait_for_runtime_identity_healthcheck_with_retry",
            side_effect=_matching_runtime_identity_healthcheck,
        ):
            evidence = _verify_required_runtime_identity_evidence(
                health_url="https://cm-testing.example.com/launchplane/health",
                timeout_seconds=30,
                expected_runtime_identity=expected_runtime_identity,
            )

        self.assertEqual(evidence.status, "pass")
        self.assertEqual(evidence.runtime_identity_status, "match")
        self.assertEqual(evidence.observed_runtime_identity, expected_runtime_identity)

    def test_required_runtime_identity_evidence_fails_closed_on_mismatch(self) -> None:
        expected_runtime_identity = RuntimeIdentity(
            product="odoo-tenant-cm",
            context="cm",
            instance="testing",
            deployment_record_id="deployment-current",
            artifact_id="artifact-current",
            source_git_ref="abc1234",
        )
        observed_runtime_identity = expected_runtime_identity.model_copy(
            update={"deployment_record_id": "deployment-stale"}
        )

        with patch(
            "control_plane.workflows.odoo_stable_target_replacement.wait_for_runtime_identity_healthcheck_with_retry",
            side_effect=RuntimeIdentityHealthcheckError(
                "runtime identity mismatch",
                healthcheck_pass=HealthcheckPass(
                    payload={"runtime_identity": observed_runtime_identity.model_dump(mode="json")}
                ),
            ),
        ):
            evidence = _verify_required_runtime_identity_evidence(
                health_url="https://cm-testing.example.com/launchplane/health",
                timeout_seconds=30,
                expected_runtime_identity=expected_runtime_identity,
            )

        self.assertEqual(evidence.status, "fail")
        self.assertEqual(evidence.runtime_identity_status, "mismatch")
        self.assertIn("deployment_record_id", evidence.runtime_identity_detail)
        self.assertEqual(evidence.observed_runtime_identity, observed_runtime_identity)

    def test_required_runtime_identity_evidence_records_unverifiable_timeout(self) -> None:
        expected_runtime_identity = RuntimeIdentity(
            product="odoo-tenant-cm",
            context="cm",
            instance="testing",
            deployment_record_id="deployment-current",
            artifact_id="artifact-current",
            source_git_ref="abc1234",
        )

        with patch(
            "control_plane.workflows.odoo_stable_target_replacement.wait_for_runtime_identity_healthcheck_with_retry",
            side_effect=RuntimeIdentityHealthcheckError("healthcheck timed out"),
        ):
            evidence = _verify_required_runtime_identity_evidence(
                health_url="https://cm-testing.example.com/launchplane/health",
                timeout_seconds=30,
                expected_runtime_identity=expected_runtime_identity,
            )

        self.assertEqual(evidence.status, "fail")
        self.assertEqual(evidence.runtime_identity_status, "unverifiable")
        self.assertIn("healthcheck timed out", evidence.runtime_identity_detail)

    def test_apply_rejects_disabled_health_when_runtime_identity_is_required(self) -> None:
        store = _Store(
            target_record=_target_record(),
            target_id_record=_target_id_record(),
            inventory=_inventory(),
        )

        with (
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_source.read_dokploy_config",
                return_value=("host", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.fetch_dokploy_target_payload",
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
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.latest_deployment_for_target",
                return_value={"deploymentId": "deploy-123", "status": "success"},
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.update_dokploy_target_env"
            ) as update_env,
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.trigger_deployment"
            ) as trigger_deployment,
        ):
            with self.assertRaisesRegex(
                click.ClickException,
                "requires health verification when the lane requires runtime identity",
            ):
                execute_odoo_stable_target_replacement_apply(
                    control_plane_root=Path("."),
                    record_store=store,
                    request=OdooStableTargetReplacementApplyRequest(
                        product="odoo-tenant-cm",
                        instance="testing",
                        verify_health=False,
                    ),
                    dokploy_request=cast(DokployRequest, _request),
                )

        self.assertEqual(store.deployment_records, [])
        self.assertEqual(store.environment_inventories, [])
        update_env.assert_not_called()
        trigger_deployment.assert_not_called()

    def test_build_plan_allows_issue_backed_opw_upstream_restore_policy(self) -> None:
        with (
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_source.read_dokploy_config",
                return_value=("host", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.fetch_dokploy_target_payload",
                return_value={
                    "name": "opw-prod",
                    "sourceType": "raw",
                    "composePath": "docker-compose.yml",
                    "composeFile": "services: {}",
                    "env": "\n".join(
                        (
                            "ODOO_DATA_VOLUME=cm_testing_odoo_data",
                            "ODOO_LOG_VOLUME=cm_testing_odoo_logs",
                            "ODOO_DB_VOLUME=cm_testing_odoo_db",
                            "ODOO_WEB_COMMAND=/odoo/odoo-bin",
                        )
                    ),
                },
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.latest_deployment_for_target",
                return_value={"deploymentId": "deploy-opw", "status": "success"},
            ),
        ):
            plan = build_odoo_stable_target_replacement_plan(
                control_plane_root=Path("."),
                record_store=_Store(
                    profile=_opw_profile_with_prelaunch_policy(enabled=True),
                    target_record=_opw_target_record(),
                    target_id_record=_opw_target_id_record(),
                ),
                request=OdooStableTargetReplacementRequest(
                    product="odoo-tenant-opw",
                    instance="prod",
                    allow_empty_data=True,
                    data_source_mode="upstream_restore",
                    confirmation="restore opw upstream",
                ),
                dokploy_request=cast(DokployRequest, _request),
            )

        self.assertEqual(plan.plan_status, "ready")
        self.assertTrue(plan.allow_empty_data)
        self.assertEqual(plan.data_source_mode, "upstream_restore")
        self.assertEqual(
            plan.approval_issue_url,
            "https://github.com/cbusillo/launchplane/issues/573",
        )

    def test_build_plan_proves_prelaunch_domain_from_live_target(self) -> None:
        with (
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_source.read_dokploy_config",
                return_value=("host", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.fetch_dokploy_target_payload",
                return_value={
                    "name": "opw-prod",
                    "sourceType": "raw",
                    "composePath": "docker-compose.yml",
                    "composeFile": "services: {}",
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
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.latest_deployment_for_target",
                return_value={"deploymentId": "deploy-opw", "status": "success"},
            ),
        ):
            plan = build_odoo_stable_target_replacement_plan(
                control_plane_root=Path("."),
                record_store=_Store(
                    profile=_opw_profile_with_prelaunch_policy(enabled=True),
                    target_record=_opw_target_record_with_stale_domain(),
                    target_id_record=_opw_target_id_record(),
                ),
                request=OdooStableTargetReplacementRequest(
                    product="odoo-tenant-opw",
                    instance="prod",
                    allow_empty_data=True,
                    data_source_mode="upstream_restore",
                    confirmation="restore opw upstream",
                ),
                dokploy_request=cast(DokployRequest, _request),
            )

        self.assertEqual(plan.plan_status, "ready")
        self.assertEqual(
            plan.expected_domain_hosts,
            ("opw-prod.shinycomputers.com",),
        )

    def test_build_plan_blocks_upstream_restore_without_issue_backed_policy(self) -> None:
        with (
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_source.read_dokploy_config",
                return_value=("host", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.fetch_dokploy_target_payload",
                return_value={
                    "name": "opw-prod",
                    "sourceType": "raw",
                    "composePath": "docker-compose.yml",
                    "composeFile": "services: {}",
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
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.latest_deployment_for_target",
                return_value={"deploymentId": "deploy-opw", "status": "success"},
            ),
        ):
            plan = build_odoo_stable_target_replacement_plan(
                control_plane_root=Path("."),
                record_store=_Store(
                    profile=_opw_profile_with_prelaunch_policy(enabled=False),
                    target_record=_opw_target_record(),
                    target_id_record=_opw_target_id_record(),
                ),
                request=OdooStableTargetReplacementRequest(
                    product="odoo-tenant-opw",
                    instance="prod",
                    allow_empty_data=True,
                    data_source_mode="upstream_restore",
                    confirmation="restore opw upstream",
                ),
                dokploy_request=cast(DokployRequest, _request),
            )

        self.assertEqual(plan.plan_status, "blocked")
        self.assertIn("prelaunch rebuild is not enabled", "; ".join(plan.blockers))

    def test_build_plan_blocks_upstream_restore_disallowed_by_lane_data_policy(self) -> None:
        profile = _opw_profile_with_prelaunch_policy(enabled=True)
        lane = profile.lanes[0].model_copy(update={"odoo_data_policy": ProductOdooLaneDataPolicy()})
        profile = profile.model_copy(update={"lanes": (lane,)})
        with (
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_source.read_dokploy_config",
                return_value=("host", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.fetch_dokploy_target_payload",
                return_value={
                    "name": "opw-prod",
                    "sourceType": "raw",
                    "composePath": "docker-compose.yml",
                    "composeFile": "services: {}",
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
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.latest_deployment_for_target",
                return_value={"deploymentId": "deploy-opw", "status": "success"},
            ),
        ):
            plan = build_odoo_stable_target_replacement_plan(
                control_plane_root=Path("."),
                record_store=_Store(
                    profile=profile,
                    target_record=_opw_target_record(),
                    target_id_record=_opw_target_id_record(),
                ),
                request=OdooStableTargetReplacementRequest(
                    product="odoo-tenant-opw",
                    instance="prod",
                    allow_empty_data=True,
                    data_source_mode="upstream_restore",
                    confirmation="restore opw upstream",
                ),
                dokploy_request=cast(DokployRequest, _request),
            )

        self.assertEqual(plan.plan_status, "blocked")
        self.assertIn("lane data policy", "; ".join(plan.blockers))
        self.assertIn("'upstream_restore'", "; ".join(plan.blockers))

    def test_build_plan_reports_ready_when_records_and_volume_contract_exist(self) -> None:
        identity = json.dumps(
            {"deployment_record_id": "deployment-cm-testing", "artifact_id": "artifact"}
        )
        with (
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_source.read_dokploy_config",
                return_value=("host", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.fetch_dokploy_target_payload",
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
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.latest_deployment_for_target",
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
        self.assertEqual(
            plan.current_target.live_volume_values,
            {
                "ODOO_DATA_VOLUME": "cm_testing_odoo_data",
                "ODOO_LOG_VOLUME": "cm_testing_odoo_logs",
                "ODOO_DB_VOLUME": "cm_testing_odoo_db",
            },
        )
        self.assertTrue(plan.current_target.runtime_identity_present)
        self.assertEqual(plan.current_target.domain_hosts, ("cm-testing.shinycomputers.com",))

    def test_build_plan_blocks_when_selected_artifact_omits_required_modules(self) -> None:
        with (
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_source.read_dokploy_config",
                return_value=("host", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.fetch_dokploy_target_payload",
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
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.latest_deployment_for_target",
                return_value={"deploymentId": "deploy-123", "status": "success"},
            ),
        ):
            plan = build_odoo_stable_target_replacement_plan(
                control_plane_root=Path("."),
                record_store=_Store(
                    target_record=_target_record(),
                    target_id_record=_target_id_record(),
                    inventory=_inventory(),
                    artifact_manifest=_artifact_manifest(odoo_install_modules=("cm_website",)),
                ),
                request=OdooStableTargetReplacementRequest(
                    product="odoo-tenant-cm", instance="testing"
                ),
                dokploy_request=cast(DokployRequest, _request),
            )

        self.assertEqual(plan.plan_status, "blocked")
        blockers = "; ".join(plan.blockers)
        self.assertIn("launchplane_settings", blockers)
        self.assertIn("disable_odoo_online", blockers)

    def test_build_plan_accepts_explicit_artifact_override(self) -> None:
        selected_manifest = _artifact_manifest(
            artifact_id="artifact-cm-selected",
            source_commit="feed123",
        )
        with (
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_source.read_dokploy_config",
                return_value=("host", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.fetch_dokploy_target_payload",
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
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.latest_deployment_for_target",
                return_value={"deploymentId": "deploy-123", "status": "success"},
            ),
        ):
            plan = build_odoo_stable_target_replacement_plan(
                control_plane_root=Path("."),
                record_store=_Store(
                    target_record=_target_record(),
                    target_id_record=_target_id_record(),
                    inventory=_inventory(),
                    artifact_manifests=(_artifact_manifest(), selected_manifest),
                ),
                request=OdooStableTargetReplacementRequest(
                    product="odoo-tenant-cm",
                    instance="testing",
                    artifact_id=selected_manifest.artifact_id,
                    source_git_ref=selected_manifest.source_commit,
                ),
                dokploy_request=cast(DokployRequest, _request),
            )

        self.assertEqual(plan.plan_status, "ready")
        self.assertEqual(plan.expected_artifact_id, selected_manifest.artifact_id)
        self.assertEqual(plan.expected_source_git_ref, selected_manifest.source_commit)

    def test_build_plan_blocks_explicit_artifact_from_foreign_repository(self) -> None:
        selected_manifest = _artifact_manifest(
            artifact_id="artifact-cm-foreign",
            source_commit="feed123",
            image_repository="ghcr.io/cbusillo/other-product",
        )
        with (
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_source.read_dokploy_config",
                return_value=("host", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.fetch_dokploy_target_payload",
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
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.latest_deployment_for_target",
                return_value={"deploymentId": "deploy-123", "status": "success"},
            ),
        ):
            plan = build_odoo_stable_target_replacement_plan(
                control_plane_root=Path("."),
                record_store=_Store(
                    target_record=_target_record(),
                    target_id_record=_target_id_record(),
                    inventory=_inventory(),
                    artifact_manifests=(_artifact_manifest(), selected_manifest),
                ),
                request=OdooStableTargetReplacementRequest(
                    product="odoo-tenant-cm",
                    instance="testing",
                    artifact_id=selected_manifest.artifact_id,
                    source_git_ref=selected_manifest.source_commit,
                ),
                dokploy_request=cast(DokployRequest, _request),
            )

        self.assertEqual(plan.plan_status, "blocked")
        self.assertIn(
            "Selected artifact image repository does not match product profile.",
            plan.blockers,
        )

    def test_build_plan_blocks_when_current_artifact_changed_after_preflight(self) -> None:
        with (
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_source.read_dokploy_config",
                return_value=("host", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.fetch_dokploy_target_payload",
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
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.latest_deployment_for_target",
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
                    product="odoo-tenant-cm",
                    instance="testing",
                    expected_current_artifact_id="artifact-cm-before-read",
                ),
                dokploy_request=cast(DokployRequest, _request),
            )

        self.assertEqual(plan.plan_status, "blocked")
        self.assertIn(
            "Current inventory artifact changed after operational readiness preflight.",
            plan.blockers,
        )

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

    def test_build_plan_reports_missing_product_profile_as_operator_error(self) -> None:
        with self.assertRaises(click.ClickException) as raised_error:
            build_odoo_stable_target_replacement_plan(
                control_plane_root=Path("."),
                record_store=_Store(),
                request=OdooStableTargetReplacementRequest(
                    product="missing-tenant", instance="testing"
                ),
            )

        self.assertIn(
            "Launchplane has no product profile record for 'missing-tenant'.",
            str(raised_error.exception),
        )

    def test_build_plan_blocks_missing_volume_contract(self) -> None:
        with (
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_source.read_dokploy_config",
                return_value=("host", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.fetch_dokploy_target_payload",
                return_value={"name": "cm-testing", "env": "ODOO_DATA_VOLUME=data"},
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.latest_deployment_for_target",
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

    def test_build_plan_blocks_existing_data_when_desired_volume_authority_drifted(
        self,
    ) -> None:
        runtime_records = _runtime_environment_records_for_profile(_profile())
        drifted_record = runtime_records[0].model_copy(
            update={
                "env": {
                    **runtime_records[0].env,
                    "ODOO_DATA_VOLUME": "cm_testing_replacement_data",
                }
            }
        )
        with (
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_source.read_dokploy_config",
                return_value=("host", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.fetch_dokploy_target_payload",
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
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.latest_deployment_for_target",
                return_value={"deploymentId": "deploy-123", "status": "success"},
            ),
        ):
            plan = build_odoo_stable_target_replacement_plan(
                control_plane_root=Path("."),
                record_store=_Store(
                    target_record=_target_record(),
                    target_id_record=_target_id_record(),
                    inventory=_inventory(),
                    runtime_environment_records=(drifted_record,),
                ),
                request=OdooStableTargetReplacementRequest(
                    product="odoo-tenant-cm",
                    instance="testing",
                    data_source_mode="existing",
                ),
                dokploy_request=cast(DokployRequest, _request),
            )

        self.assertEqual(plan.plan_status, "blocked")
        blocker = "; ".join(plan.blockers)
        self.assertIn("DB-backed desired authority", blocker)
        self.assertIn("ODOO_DATA_VOLUME", blocker)
        self.assertNotIn("cm_testing_odoo_data", blocker)
        self.assertNotIn("cm_testing_replacement_data", blocker)
        self.assertEqual(
            next(step for step in plan.steps if step.step_id == "volume-contract").status,
            "blocked",
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

    def test_target_health_url_uses_odoo_runtime_identity_path(self) -> None:
        profile = _profile().model_copy(update={"health_path": "/cm-website/health"})
        lane = profile.lanes[0]

        health_url = _target_health_url(
            profile=profile,
            lane=lane,
            domains=("cm-website-testing.shinycomputers.com",),
        )

        self.assertEqual(
            health_url,
            "https://cm-website-testing.shinycomputers.com/launchplane/health",
        )

    def test_target_health_url_ignores_stale_derived_product_health_url(self) -> None:
        profile = _profile().model_copy(update={"health_path": "/cm-website/health"})
        lane = profile.lanes[0].model_copy(
            update={
                "base_url": "https://cm-website-testing.shinycomputers.com/",
                "health_url": "HTTPS://CM-WEBSITE-TESTING.SHINYCOMPUTERS.COM/cm-website/health/",
            }
        )

        health_url = _target_health_url(
            profile=profile,
            lane=lane,
            domains=("cm-website-testing.shinycomputers.com",),
        )

        self.assertEqual(
            health_url,
            "https://cm-website-testing.shinycomputers.com/launchplane/health",
        )

    def test_target_health_url_allows_explicit_lane_override(self) -> None:
        profile = _profile().model_copy(update={"health_path": "/cm-website/health"})
        lane = profile.lanes[0].model_copy(
            update={"health_url": "https://internal.example.test/web/health"}
        )

        health_url = _target_health_url(
            profile=profile,
            lane=lane,
            domains=("cm-website-testing.shinycomputers.com",),
        )

        self.assertEqual(health_url, "https://internal.example.test/web/health")

    def test_apply_recreates_target_in_place_and_writes_breadcrumb_inventory(self) -> None:
        store = _Store(
            target_record=_target_record(),
            target_id_record=_target_id_record(),
            inventory=_inventory(),
            artifact_manifest=_artifact_manifest(
                odoo_install_modules=(
                    "launchplane_settings",
                    "disable_odoo_online",
                    "cm_website",
                )
            ),
            odoo_instance_override_record=OdooInstanceOverrideRecord(
                context="cm",
                instance="testing",
                website_bootstrap=OdooWebsiteBootstrapPayload(
                    tenant="cm",
                    name="Cell Mechanic",
                    canonical_url="https://cm-website-local.shinycomputers.com",
                    homepage_url="/cell-mechanic",
                    primary_page_xmlid="cm_website.website_page_cell_mechanic",
                    logo_path="addons/cm_website/static/src/img/cell_mechanic_logo_hi_res_v2.png",
                    logo_alt="Cell Mechanic",
                ),
                updated_at="2026-06-13T18:00:00Z",
            ),
        )
        persisted_env = ""
        persisted_compose_file = "services: {}"
        domain_records: list[JsonValue] = []
        deployment_records: list[JsonValue] = [{"deploymentId": "deploy-123", "status": "success"}]
        stale_payload_b64 = base64.b64encode(b'{"stale":true}').decode("ascii")
        route_name = control_plane_dokploy._traefik_route_name(
            domain_host="cm-testing.shinycomputers.com"
        )

        def _fetch_target_payload(**_: object) -> JsonValue:
            return cast(
                JsonObject,
                {
                    "name": "cm-testing",
                    "appName": "cm-testing",
                    "composeStatus": "done",
                    "composeType": "docker-compose",
                    "sourceType": "raw",
                    "serverId": "server-1",
                    "composePath": "docker-compose.yml",
                    "composeFile": persisted_compose_file,
                    "deployments": deployment_records,
                    "env": persisted_env
                    or "\n".join(
                        (
                            "ODOO_DATA_VOLUME=cm_testing_odoo_data",
                            "ODOO_LOG_VOLUME=cm_testing_odoo_logs",
                            "ODOO_DB_VOLUME=cm_testing_odoo_db",
                            "ODOO_ADDONS_PATH=/opt/project/addons,/opt/launchplane/addons,/odoo/addons",
                            "ODOO_INSTALL_MODULES=stale_module,disable_odoo_online",
                            f"{ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY}={stale_payload_b64}",
                            f"{LAUNCHPLANE_INSTANCE_OVERRIDES_REQUIRED_ENV_KEY}=true",
                        )
                    ),
                },
            )

        def _dokploy_request(path: str, query: object | None = None, **kwargs: object) -> JsonValue:
            if path == "/api/domain.byComposeId" and domain_records:
                return domain_records
            if path == "/api/docker.getContainersByAppNameMatch":
                self.assertEqual(
                    query,
                    {
                        "appName": "cm-testing",
                        "appType": "docker-compose",
                        "serverId": "server-1",
                    },
                )
                return [
                    {
                        "containerId": "container-web",
                        "name": "cm-testing-web-1",
                        "state": "running",
                        "status": "Up 1 minute",
                    },
                    {"containerId": "container-db", "name": "cm-testing-db-1"},
                ]
            if path == "/api/docker.getConfig":
                self.assertEqual(
                    query,
                    {"containerId": "container-web", "serverId": "server-1"},
                )
                return {
                    "Config": {
                        "Labels": {
                            "traefik.enable": "true",
                            "traefik.docker.network": "dokploy-network",
                            f"traefik.http.routers.{route_name}-web.rule": "Host(`cm-testing.shinycomputers.com`)",
                            f"traefik.http.routers.{route_name}-websecure.rule": "Host(`cm-testing.shinycomputers.com`)",
                        }
                    },
                    "NetworkSettings": {
                        "Networks": {"cm-testing_default": {}, "dokploy-network": {}}
                    },
                }
            return _request(path=path, query=query, **kwargs)

        def _ensure_domain(**_: object) -> str:
            domain_records[:] = [
                {
                    "domainId": "domain-cm-testing",
                    "host": "cm-testing.shinycomputers.com",
                    "serviceName": "web",
                    "port": 8069,
                    "https": True,
                    "certificateType": "none",
                    "domainType": "compose",
                    "path": "/",
                    "internalPath": "/",
                    "stripPath": False,
                    "uniqueConfigKey": 42,
                }
            ]
            return "domain-cm-testing"

        def _update_env(*, env_text: str, **_: object) -> None:
            nonlocal persisted_env
            persisted_env = env_text

        def _wait_for_deploy(**_: object) -> None:
            deployment_records.insert(0, {"deploymentId": "deploy-456", "status": "done"})

        post_deploy_result = OdooPostDeployResult(
            context="cm",
            instance="testing",
            phase="deploy",
            post_deploy_status="pass",
            override_status="pass",
            override_record_found=True,
            override_payload_rendered=True,
            override_count=2,
            website_bootstrap_included=True,
            override_evidence={
                "config_parameter_count": "1",
                "post_deploy_readback_log_available": "true",
                "post_deploy_readback_website_bootstrap_domain_matches_canonical": "true",
                "website_bootstrap_included": "true",
            },
        )
        rendered_compose_file = control_plane_dokploy.render_odoo_raw_compose_file(
            image_reference="ghcr.io/cbusillo/odoo-tenant-cm@sha256:artifact",
            domain_hosts=("cm-testing.shinycomputers.com",),
            runtime_port=8069,
        )

        def _sync_source(*, compose_file: str, **_: object) -> dict[str, str]:
            nonlocal persisted_compose_file
            persisted_compose_file = compose_file
            return {
                "source_type": "raw",
                "compose_sha256": control_plane_dokploy.compose_file_sha256(compose_file),
                "changed": "true",
            }

        with (
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_source.read_dokploy_config",
                return_value=("host", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.fetch_dokploy_target_payload",
                side_effect=_fetch_target_payload,
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.latest_deployment_for_target",
                return_value={"deploymentId": "deploy-123", "status": "success"},
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.control_plane_runtime_environments.resolve_runtime_environment_values",
                return_value={"ODOO_WORKERS": "2"},
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_compose.sync_dokploy_compose_raw_source",
                side_effect=_sync_source,
            ) as sync_source,
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_compose.render_odoo_raw_compose_file",
                return_value=rendered_compose_file,
            ) as render_compose,
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_compose.ensure_compose_web_domain_route"
            ) as ensure_domain,
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_compose.fetch_dokploy_converted_compose_file",
                return_value=rendered_compose_file,
            ) as fetch_converted_compose,
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.update_dokploy_target_env",
                side_effect=_update_env,
            ) as update_env,
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.trigger_deployment"
            ) as trigger_deploy,
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.wait_for_target_deployment"
            ) as wait_deploy,
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.execute_odoo_post_deploy",
                return_value=post_deploy_result,
            ) as post_deploy,
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.verify_odoo_stable_readiness",
                return_value=_verification_result(),
            ) as verify_readiness,
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.wait_for_runtime_identity_healthcheck_with_retry",
                side_effect=_matching_runtime_identity_healthcheck,
            ) as verify_runtime_identity,
        ):
            ensure_domain.side_effect = _ensure_domain
            wait_deploy.side_effect = _wait_for_deploy
            result = execute_odoo_stable_target_replacement_apply(
                control_plane_root=Path("."),
                record_store=store,
                request=OdooStableTargetReplacementApplyRequest(
                    product="odoo-tenant-cm", instance="testing"
                ),
                dokploy_request=cast(DokployRequest, _dokploy_request),
            )

        self.assertEqual(result.deploy_status, "pass")
        self.assertEqual(result.post_deploy_status, "pass")
        self.assertEqual(result.post_deploy_override_status, "pass")
        self.assertTrue(result.post_deploy_override_record_found)
        self.assertTrue(result.post_deploy_override_payload_rendered)
        self.assertEqual(result.post_deploy_override_count, 2)
        self.assertTrue(result.post_deploy_website_bootstrap_included)
        self.assertEqual(result.post_deploy_override_evidence["config_parameter_count"], "1")
        self.assertEqual(
            result.post_deploy_override_evidence[
                "post_deploy_readback_website_bootstrap_domain_matches_canonical"
            ],
            "true",
        )
        self.assertEqual(result.health_status, "pass")
        self.assertEqual(result.canonical_status, "pass")
        self.assertEqual(result.logo_status, "pass")
        self.assertTrue(result.runtime_identity_injected)
        self.assertEqual(result.target_name, "cm-testing")
        self.assertEqual(
            result.image_reference,
            "ghcr.io/cbusillo/odoo-tenant-cm@sha256:artifact",
        )
        render_compose.assert_called_once_with(
            image_reference="ghcr.io/cbusillo/odoo-tenant-cm@sha256:artifact",
            domain_hosts=("cm-testing.shinycomputers.com",),
            runtime_port=8069,
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
        self.assertEqual(fetch_converted_compose.call_count, 2)
        trigger_deploy.assert_called_once()
        wait_deploy.assert_called_once()
        post_deploy.assert_called_once()
        self.assertFalse(post_deploy.call_args.kwargs["run_destructive_restore"])
        self.assertIsNotNone(store.odoo_instance_override_record)
        assert store.odoo_instance_override_record is not None
        self.assertIsNotNone(store.odoo_instance_override_record.website_bootstrap)
        assert store.odoo_instance_override_record.website_bootstrap is not None
        self.assertEqual(
            store.odoo_instance_override_record.website_bootstrap.canonical_url,
            "https://cm-testing.shinycomputers.com",
        )
        verify_readiness.assert_called_once_with(
            base_url="https://cm-testing.shinycomputers.com",
            health_url="https://cm-testing.shinycomputers.com/launchplane/health",
            verify_health=True,
            verify_canonical=True,
            verify_logo=True,
            timeout_seconds=180,
            retry_interval_seconds=5,
        )
        self.assertEqual(
            result.health_url, "https://cm-testing.shinycomputers.com/launchplane/health"
        )
        self.assertEqual(result.canonical_url, "https://cm-testing.shinycomputers.com")
        self.assertEqual(
            result.logo_urls,
            ("https://cm-testing.shinycomputers.com/web/image/website/1/logo",),
        )
        self.assertEqual(result.verification_evidence.health_url, result.health_url)
        self.assertIn("LAUNCHPLANE_RUNTIME_IDENTITY_JSON=", persisted_env)
        self.assertIn("LAUNCHPLANE_DEPLOYMENT_RECORD_ID=", persisted_env)
        self.assertIn("LAUNCHPLANE_ARTIFACT_ID=artifact-cm-testing", persisted_env)
        self.assertIn("PLATFORM_CONTEXT=cm", persisted_env)
        self.assertIn("PLATFORM_INSTANCE=testing", persisted_env)
        self.assertNotIn("ODOO_WEB_COMMAND=/odoo/odoo-bin", persisted_env)
        self.assertIn("ODOO_WORKERS=2", persisted_env)
        persisted_env_map = control_plane_dokploy.parse_dokploy_env_text(persisted_env)
        self.assertEqual(
            persisted_env_map["ODOO_INSTALL_MODULES"],
            "launchplane_settings,disable_odoo_online,cm_website",
        )
        self.assertEqual(
            persisted_env_map["ODOO_ADDONS_PATH"],
            "/opt/project/addons,/opt/launchplane/addons,/odoo/addons,/opt/enterprise",
        )
        self.assertEqual(persisted_env_map[LAUNCHPLANE_WEBSITE_BOOTSTRAP_REQUIRED_ENV_KEY], "true")
        self.assertNotIn(
            LAUNCHPLANE_INSTANCE_OVERRIDES_REQUIRED_ENV_KEY,
            persisted_env_map,
        )
        persisted_override_payload = json.loads(
            base64.b64decode(persisted_env_map[ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY]).decode(
                "utf-8"
            )
        )
        self.assertEqual(
            persisted_override_payload["website_bootstrap"]["canonical_url"],
            "https://cm-testing.shinycomputers.com",
        )
        self.assertEqual(
            persisted_override_payload["website_bootstrap"]["homepage_url"],
            "/cell-mechanic",
        )
        self.assertGreaterEqual(len(store.deployment_records), 2)
        final_deployment = store.deployment_records[-1]
        self.assertEqual(final_deployment.deploy.status, "pass")
        self.assertEqual(
            final_deployment.post_deploy_update.evidence,
            post_deploy_result.override_evidence,
        )
        self.assertEqual(
            final_deployment.post_deploy_update.evidence["post_deploy_readback_log_available"],
            "true",
        )
        self.assertEqual(
            final_deployment.runtime_source["rendered_traefik_router_label_count"], "8"
        )
        self.assertEqual(
            final_deployment.runtime_source[
                "live_domain_cm-testing.shinycomputers.com_https_rule_present"
            ],
            "true",
        )
        self.assertEqual(
            final_deployment.runtime_source[
                "domain_route_domain_cm-testing.shinycomputers.com_record_present"
            ],
            "true",
        )
        self.assertEqual(
            final_deployment.runtime_source[
                "domain_route_domain_cm-testing.shinycomputers.com_service_name"
            ],
            "web",
        )
        self.assertEqual(
            final_deployment.runtime_source[
                "domain_route_domain_cm-testing.shinycomputers.com_port_matches_runtime"
            ],
            "true",
        )
        self.assertEqual(
            final_deployment.runtime_source["pre_deploy_compose_app_name"], "cm-testing"
        )
        self.assertEqual(
            final_deployment.runtime_source["post_deploy_latest_deployment_key"],
            "deploy-456",
        )
        self.assertEqual(
            final_deployment.runtime_source["converted_traefik_router_label_count"], "8"
        )
        self.assertEqual(
            final_deployment.runtime_source[
                "post_deploy_converted_domain_cm-testing.shinycomputers.com_https_rule_present"
            ],
            "true",
        )
        self.assertEqual(final_deployment.runtime_source["post_deploy_container_web_found"], "true")
        self.assertEqual(
            final_deployment.runtime_source["post_deploy_container_server_id_present"],
            "true",
        )
        self.assertEqual(
            final_deployment.runtime_source["post_deploy_container_traefik_enable"], "true"
        )
        self.assertEqual(
            final_deployment.runtime_source["post_deploy_container_traefik_network"],
            "dokploy-network",
        )
        self.assertEqual(
            final_deployment.runtime_source[
                "post_deploy_container_domain_cm-testing.shinycomputers.com_https_rule_present"
            ],
            "true",
        )
        self.assertEqual(
            final_deployment.runtime_source["post_deploy_container_has_dokploy_network"],
            "true",
        )
        self.assertEqual(
            final_deployment.runtime_source["runtime_override_payload_rendered"], "true"
        )
        self.assertEqual(
            final_deployment.runtime_source["runtime_override_website_bootstrap_required"],
            "true",
        )
        self.assertEqual(
            final_deployment.runtime_source["runtime_override_instance_required"], "false"
        )
        self.assertEqual(
            final_deployment.runtime_source["required_odoo_modules"],
            "launchplane_settings,disable_odoo_online",
        )
        self.assertEqual(
            final_deployment.runtime_source["artifact_odoo_install_modules"],
            "launchplane_settings,disable_odoo_online,cm_website",
        )
        self.assertEqual(
            final_deployment.runtime_source["odoo_install_modules"],
            "launchplane_settings,disable_odoo_online,cm_website",
        )
        self.assertEqual(result.runtime_source, final_deployment.runtime_source)
        assert final_deployment.runtime_identity is not None
        self.assertEqual(final_deployment.runtime_identity.product, "odoo-tenant-cm")
        self.assertEqual(
            final_deployment.runtime_identity.deployment_record_id,
            final_deployment.record_id,
        )
        self.assertEqual(final_deployment.destination_health.status, "pass")
        self.assertEqual(final_deployment.destination_health.runtime_identity_status, "match")
        assert final_deployment.destination_health.observed_runtime_identity is not None
        self.assertEqual(
            final_deployment.destination_health.observed_runtime_identity.deployment_record_id,
            final_deployment.record_id,
        )
        verify_runtime_identity.assert_called_once()
        self.assertEqual(len(store.environment_inventories), 1)
        self.assertEqual(
            store.environment_inventories[0].deployment_record_id,
            final_deployment.record_id,
        )
        self.assertEqual(
            store.environment_inventories[0].post_deploy_update.evidence,
            post_deploy_result.override_evidence,
        )

    def test_apply_can_deploy_explicit_stored_artifact(self) -> None:
        fresh_manifest = _artifact_manifest(
            artifact_id="artifact-cm-fresh",
            source_commit="feed123",
            digest="sha256:fresh",
            odoo_install_modules=(
                "launchplane_settings",
                "disable_odoo_online",
                "cm_website",
            ),
        )
        profile = _profile()
        profile = profile.model_copy(
            update={
                "lanes": (
                    profile.lanes[0].model_copy(
                        update={
                            "odoo_data_policy": ProductOdooLaneDataPolicy(
                                requires_runtime_identity=False
                            )
                        }
                    ),
                )
            }
        )
        store = _Store(
            profile=profile,
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
                        "ODOO_INSTALL_MODULES=cm_website,legacy_theme",
                    )
                ),
            }

        def _update_env(*, env_text: str, **_: object) -> None:
            nonlocal persisted_env
            persisted_env = env_text

        with (
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_source.read_dokploy_config",
                return_value=("host", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.fetch_dokploy_target_payload",
                side_effect=_fetch_target_payload,
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.latest_deployment_for_target",
                return_value={"deploymentId": "deploy-123", "status": "success"},
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.control_plane_runtime_environments.resolve_runtime_environment_values",
                return_value={},
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_compose.sync_dokploy_compose_raw_source"
            ) as sync_source,
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_compose.ensure_compose_web_domain_route"
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_compose.fetch_dokploy_converted_compose_file",
                return_value=control_plane_dokploy.render_odoo_raw_compose_file(
                    image_reference="ghcr.io/cbusillo/odoo-tenant-cm@sha256:fresh",
                    domain_hosts=("cm-testing.shinycomputers.com",),
                    runtime_port=8069,
                ),
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.update_dokploy_target_env",
                side_effect=_update_env,
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.trigger_deployment"
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.wait_for_target_deployment"
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
                "control_plane.workflows.odoo_stable_target_replacement.verify_odoo_stable_readiness",
                return_value=_verification_result(),
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.wait_for_runtime_identity_healthcheck_with_retry"
            ) as verify_runtime_identity,
        ):
            result = execute_odoo_stable_target_replacement_apply(
                control_plane_root=Path("."),
                record_store=store,
                request=OdooStableTargetReplacementApplyRequest(
                    product="odoo-tenant-cm",
                    instance="testing",
                    artifact_id="artifact-cm-fresh",
                    source_git_ref="feed123",
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
        persisted_env_map = control_plane_dokploy.parse_dokploy_env_text(persisted_env)
        self.assertEqual(
            persisted_env_map["ODOO_INSTALL_MODULES"],
            "launchplane_settings,disable_odoo_online,cm_website",
        )
        final_deployment = store.deployment_records[-1]
        self.assertEqual(final_deployment.source_git_ref, "feed123")
        assert final_deployment.runtime_identity is not None
        self.assertEqual(final_deployment.runtime_identity.artifact_id, "artifact-cm-fresh")
        self.assertEqual(final_deployment.runtime_identity.source_git_ref, "feed123")
        self.assertEqual(final_deployment.destination_health.runtime_identity_status, "unchecked")
        verify_runtime_identity.assert_not_called()
        self.assertEqual(sync_source.call_args.kwargs["compose_name"], "cm-testing")

    def test_apply_fails_required_runtime_identity_failures_before_inventory(self) -> None:
        store = _Store(
            target_record=_target_record(),
            target_id_record=_target_id_record(),
            inventory=_inventory(),
        )
        persisted_env = ""
        rendered_compose_file = control_plane_dokploy.render_odoo_raw_compose_file(
            image_reference="ghcr.io/cbusillo/odoo-tenant-cm@sha256:artifact",
            domain_hosts=("cm-testing.shinycomputers.com",),
            runtime_port=8069,
        )
        observed_runtime_identity = RuntimeIdentity(
            product="odoo-tenant-cm",
            context="cm",
            instance="testing",
            deployment_record_id="deployment-stale",
            artifact_id="artifact-cm-testing",
            source_git_ref="abc1234",
        )
        mismatched_evidence = HealthcheckEvidence(
            verified=True,
            urls=("https://cm-testing.shinycomputers.com/launchplane/health",),
            timeout_seconds=120,
            status="fail",
            runtime_identity_status="mismatch",
            runtime_identity_detail="Runtime identity mismatched fields: deployment_record_id",
            observed_runtime_identity=observed_runtime_identity,
        )
        unverifiable_evidence = HealthcheckEvidence(
            verified=True,
            urls=("https://cm-testing.shinycomputers.com/launchplane/health",),
            timeout_seconds=120,
            status="fail",
            runtime_identity_status="unverifiable",
            runtime_identity_detail="socket timed out",
        )

        def _fetch_target_payload(**_: object) -> JsonValue:
            return {
                "name": "cm-testing",
                "sourceType": "raw",
                "composePath": "docker-compose.yml",
                "composeFile": rendered_compose_file,
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
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_source.read_dokploy_config",
                return_value=("host", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.fetch_dokploy_target_payload",
                side_effect=_fetch_target_payload,
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.latest_deployment_for_target",
                return_value={"deploymentId": "deploy-123", "status": "success"},
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.control_plane_runtime_environments.resolve_runtime_environment_values",
                return_value={},
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_compose.sync_dokploy_compose_raw_source"
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_compose.ensure_compose_web_domain_route"
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_compose.fetch_dokploy_converted_compose_file",
                return_value=rendered_compose_file,
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.update_dokploy_target_env",
                side_effect=_update_env,
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.trigger_deployment"
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.wait_for_target_deployment"
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
                "control_plane.workflows.odoo_stable_target_replacement.verify_odoo_stable_readiness",
                return_value=_verification_result(),
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement._verify_required_runtime_identity_evidence",
                side_effect=(mismatched_evidence, unverifiable_evidence),
            ) as verify_runtime_identity,
        ):
            results = tuple(
                execute_odoo_stable_target_replacement_apply(
                    control_plane_root=Path("."),
                    record_store=store,
                    request=OdooStableTargetReplacementApplyRequest(
                        product="odoo-tenant-cm", instance="testing"
                    ),
                    dokploy_request=cast(DokployRequest, _request),
                )
                for _ in range(2)
            )

        self.assertTrue(all(result.deploy_status == "fail" for result in results))
        self.assertTrue(all(result.health_status == "fail" for result in results))
        self.assertTrue(
            all(
                "runtime identity verification failed" in result.error_message.lower()
                for result in results
            )
        )
        self.assertEqual(store.environment_inventories, [])
        self.assertEqual(store.release_tuples, [])
        failed_deployments = [
            record for record in store.deployment_records if record.deploy.status == "fail"
        ]
        self.assertEqual(
            [record.destination_health.runtime_identity_status for record in failed_deployments],
            ["mismatch", "unverifiable"],
        )
        self.assertEqual(
            failed_deployments[0].destination_health.observed_runtime_identity,
            observed_runtime_identity,
        )
        self.assertIsNone(failed_deployments[1].destination_health.observed_runtime_identity)
        self.assertEqual(verify_runtime_identity.call_count, 2)

    def test_apply_persists_post_deploy_failure_evidence(self) -> None:
        store = _Store(
            target_record=_target_record(),
            target_id_record=_target_id_record(),
            inventory=_inventory(),
            odoo_instance_override_record=OdooInstanceOverrideRecord(
                context="cm",
                instance="testing",
                source_label="test",
                config_parameters=(),
                website_bootstrap=OdooWebsiteBootstrapPayload(
                    tenant="cm",
                    name="Cell Mechanic",
                    canonical_url="https://old.example.com",
                    homepage_url="/cell-mechanic",
                    primary_page_xmlid="cm_website.website_page_cell_mechanic",
                    logo_path="addons/cm_website/static/src/img/cell_mechanic_logo_hi_res_v2.png",
                    logo_alt="Cell Mechanic",
                ),
                updated_at="2026-06-13T18:00:00Z",
            ),
        )
        persisted_env = ""
        rendered_compose_file = control_plane_dokploy.render_odoo_raw_compose_file(
            image_reference="ghcr.io/cbusillo/odoo-tenant-cm@sha256:artifact",
            domain_hosts=("cm-testing.shinycomputers.com",),
            runtime_port=8069,
        )
        post_deploy_result = OdooPostDeployResult(
            context="cm",
            instance="testing",
            phase="deploy",
            post_deploy_status="fail",
            override_status="fail",
            override_record_found=True,
            override_payload_rendered=True,
            error_message="Odoo post-deploy readback failed.",
            override_evidence={
                "post_deploy_readback_log_available": "true",
                "post_deploy_readback_website_bootstrap_domain_matches_canonical": "false",
            },
        )

        def _fetch_target_payload(**_: object) -> JsonValue:
            return {
                "name": "cm-testing",
                "sourceType": "raw",
                "composePath": "docker-compose.yml",
                "composeFile": rendered_compose_file,
                "env": persisted_env
                or "\n".join(
                    (
                        "ODOO_DATA_VOLUME=cm_testing_odoo_data",
                        "ODOO_LOG_VOLUME=cm_testing_odoo_logs",
                        "ODOO_DB_VOLUME=cm_testing_odoo_db",
                    )
                ),
                "appName": "cm-testing",
                "serverId": "server-123",
                "deployments": [{"deploymentId": "deploy-123", "status": "done"}],
            }

        def _update_env(*, env_text: str, **_: object) -> None:
            nonlocal persisted_env
            persisted_env = env_text

        def _wait_for_deploy(**_: object) -> None:
            return None

        with (
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_source.read_dokploy_config",
                return_value=("host", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.fetch_dokploy_target_payload",
                side_effect=_fetch_target_payload,
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.latest_deployment_for_target",
                return_value={"deploymentId": "deploy-123", "status": "success"},
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.control_plane_runtime_environments.resolve_runtime_environment_values",
                return_value={},
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_compose.sync_dokploy_compose_raw_source",
                return_value={"source_type": "raw", "changed": "true"},
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_compose.render_odoo_raw_compose_file",
                return_value=rendered_compose_file,
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_compose.ensure_compose_web_domain_route"
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_compose.fetch_dokploy_converted_compose_file",
                return_value=rendered_compose_file,
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.update_dokploy_target_env",
                side_effect=_update_env,
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.trigger_deployment"
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.wait_for_target_deployment",
                side_effect=_wait_for_deploy,
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.execute_odoo_post_deploy",
                return_value=post_deploy_result,
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.verify_odoo_stable_readiness"
            ) as verify_readiness,
        ):
            result = execute_odoo_stable_target_replacement_apply(
                control_plane_root=Path("."),
                record_store=store,
                request=OdooStableTargetReplacementApplyRequest(
                    product="odoo-tenant-cm", instance="testing"
                ),
                dokploy_request=cast(DokployRequest, _request),
            )

        self.assertEqual(result.deploy_status, "fail")
        self.assertEqual(result.post_deploy_status, "fail")
        self.assertEqual(result.runtime_source["post_deploy_latest_deployment_key"], "deploy-123")
        verify_readiness.assert_not_called()
        final_deployment = store.deployment_records[-1]
        self.assertEqual(final_deployment.deploy.status, "fail")
        self.assertEqual(final_deployment.runtime_source, result.runtime_source)
        self.assertEqual(
            final_deployment.post_deploy_update.evidence,
            post_deploy_result.override_evidence,
        )
        self.assertEqual(
            final_deployment.post_deploy_update.evidence[
                "post_deploy_readback_website_bootstrap_domain_matches_canonical"
            ],
            "false",
        )

    def test_apply_fails_before_provider_mutation_when_required_artifact_modules_missing(
        self,
    ) -> None:
        store = _Store(
            target_record=_target_record(),
            target_id_record=_target_id_record(),
            inventory=_inventory(),
            artifact_manifest=_artifact_manifest(odoo_install_modules=("cm_website",)),
        )

        with (
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_source.read_dokploy_config",
                return_value=("host", "token"),
            ) as read_config,
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.fetch_dokploy_target_payload",
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
                        )
                    ),
                    "appName": "cm-testing",
                    "serverId": "server-123",
                },
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.latest_deployment_for_target",
                return_value={"deploymentId": "deploy-123", "status": "success"},
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.update_dokploy_target_env"
            ) as update_env,
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.trigger_deployment"
            ) as trigger_deploy,
        ):
            with self.assertRaises(click.ClickException) as error_context:
                execute_odoo_stable_target_replacement_apply(
                    control_plane_root=Path("."),
                    record_store=store,
                    request=OdooStableTargetReplacementApplyRequest(
                        product="odoo-tenant-cm", instance="testing"
                    ),
                    dokploy_request=cast(DokployRequest, _request),
                )

        self.assertIn(
            "artifact odoo_install_modules to declare required module(s)",
            str(error_context.exception),
        )
        self.assertIn("launchplane_settings", str(error_context.exception))
        self.assertIn("disable_odoo_online", str(error_context.exception))
        read_config.assert_called_once()
        update_env.assert_not_called()
        trigger_deploy.assert_not_called()

    def test_apply_fails_before_deploy_when_override_secret_env_is_missing(self) -> None:
        store = _Store(
            target_record=_target_record(),
            target_id_record=_target_id_record(),
            inventory=_inventory(),
            odoo_instance_override_record=OdooInstanceOverrideRecord(
                context="cm",
                instance="testing",
                addon_settings=(
                    OdooAddonSettingOverride(
                        addon="openai",
                        setting="api_key",
                        value=OdooOverrideValue(
                            source="secret_binding",
                            secret_binding_id="secret-openai-api-key",
                        ),
                    ),
                ),
                updated_at="2026-06-13T18:00:00Z",
            ),
        )
        rendered_compose_file = control_plane_dokploy.render_odoo_raw_compose_file(
            image_reference="ghcr.io/cbusillo/odoo-tenant-cm@sha256:artifact",
            domain_hosts=("cm-testing.shinycomputers.com",),
            runtime_port=8069,
        )

        with (
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_source.read_dokploy_config",
                return_value=("host", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.fetch_dokploy_target_payload",
                return_value={
                    "name": "cm-testing",
                    "sourceType": "raw",
                    "composePath": "docker-compose.yml",
                    "composeFile": rendered_compose_file,
                    "env": "\n".join(
                        (
                            "ODOO_DATA_VOLUME=cm_testing_odoo_data",
                            "ODOO_LOG_VOLUME=cm_testing_odoo_logs",
                            "ODOO_DB_VOLUME=cm_testing_odoo_db",
                        )
                    ),
                    "appName": "cm-testing",
                    "serverId": "server-123",
                    "deployments": [{"deploymentId": "deploy-123", "status": "done"}],
                },
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.latest_deployment_for_target",
                return_value={"deploymentId": "deploy-123", "status": "success"},
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.control_plane_runtime_environments.resolve_runtime_environment_values",
                return_value={},
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_compose.sync_dokploy_compose_raw_source",
                return_value={"source_type": "raw", "changed": "true"},
            ) as sync_source,
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_compose.render_odoo_raw_compose_file",
                return_value=rendered_compose_file,
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_compose.ensure_compose_web_domain_route"
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_compose.fetch_dokploy_converted_compose_file",
                return_value=rendered_compose_file,
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.update_dokploy_target_env"
            ) as update_env,
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.trigger_deployment"
            ) as trigger_deploy,
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.execute_odoo_post_deploy"
            ) as post_deploy,
        ):
            result = execute_odoo_stable_target_replacement_apply(
                control_plane_root=Path("."),
                record_store=store,
                request=OdooStableTargetReplacementApplyRequest(
                    product="odoo-tenant-cm", instance="testing"
                ),
                dokploy_request=cast(DokployRequest, _request),
            )

        self.assertEqual(result.deploy_status, "fail")
        self.assertIn(
            "Odoo target replacement requires override secret env key(s) before deployment",
            result.error_message,
        )
        self.assertIn("ODOO_OVERRIDE_SECRET__ADDON__OPENAI__API_KEY", result.error_message)
        sync_source.assert_called_once()
        update_env.assert_not_called()
        trigger_deploy.assert_not_called()
        post_deploy.assert_not_called()

    def test_apply_requests_destructive_restore_for_upstream_restore_mode(self) -> None:
        profile = _opw_profile_with_prelaunch_policy(enabled=True)
        manifest = _artifact_manifest(
            artifact_id="artifact-opw-testing",
            source_commit="0f9a123",
            digest="sha256:opw",
        ).model_copy(
            update={
                "image": ArtifactImageReference(
                    repository="ghcr.io/cbusillo/odoo-tenant-opw", digest="sha256:opw"
                )
            }
        )
        store = _Store(
            profile=profile,
            target_record=_opw_target_record(),
            target_id_record=_opw_target_id_record(),
            inventory=EnvironmentInventory(
                context="opw",
                instance="prod",
                artifact_identity=ArtifactIdentityReference(artifact_id="artifact-opw-testing"),
                source_git_ref="0f9a123",
                deploy=DeploymentEvidence(
                    status="pass",
                    target_type="compose",
                    target_name="opw-prod",
                    deploy_mode="dokploy-compose-api",
                ),
                updated_at="2026-05-10T00:00:00Z",
                deployment_record_id="deployment-opw-prod",
            ),
            artifact_manifest=manifest,
        )
        persisted_env = ""

        def _fetch_target_payload(**_: object) -> JsonValue:
            return {
                "name": "opw-prod",
                "sourceType": "raw",
                "composePath": "docker-compose.yml",
                "composeFile": "services: {}",
                "env": persisted_env
                or "\n".join(
                    (
                        "ODOO_DATA_VOLUME=opw_prod_odoo_data",
                        "ODOO_LOG_VOLUME=opw_prod_odoo_logs",
                        "ODOO_DB_VOLUME=opw_prod_odoo_db",
                    )
                ),
            }

        def _update_env(*, env_text: str, **_: object) -> None:
            nonlocal persisted_env
            persisted_env = env_text

        with (
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_source.read_dokploy_config",
                return_value=("host", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.fetch_dokploy_target_payload",
                side_effect=_fetch_target_payload,
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.latest_deployment_for_target",
                return_value={"deploymentId": "deploy-opw", "status": "success"},
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.control_plane_runtime_environments.resolve_runtime_environment_values",
                return_value={},
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_compose.sync_dokploy_compose_raw_source"
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_compose.ensure_compose_web_domain_route"
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_compose.fetch_dokploy_converted_compose_file",
                return_value=control_plane_dokploy.render_odoo_raw_compose_file(
                    image_reference="ghcr.io/cbusillo/odoo-tenant-opw@sha256:opw",
                    domain_hosts=("opw-prod.shinycomputers.com",),
                    runtime_port=8069,
                ),
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.update_dokploy_target_env",
                side_effect=_update_env,
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.trigger_deployment"
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.wait_for_target_deployment"
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.execute_odoo_post_deploy",
                return_value=OdooPostDeployResult(
                    context="opw",
                    instance="prod",
                    phase="deploy",
                    post_deploy_status="pass",
                ),
            ) as post_deploy,
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.verify_odoo_stable_readiness",
                return_value=OdooVerificationResult(
                    health_status="pass",
                    canonical_status="pass",
                    logo_status="pass",
                    evidence=OdooVerificationEvidence(),
                ),
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.wait_for_runtime_identity_healthcheck_with_retry",
                side_effect=_matching_runtime_identity_healthcheck,
            ),
        ):
            result = execute_odoo_stable_target_replacement_apply(
                control_plane_root=Path("."),
                record_store=store,
                request=OdooStableTargetReplacementApplyRequest(
                    product="odoo-tenant-opw",
                    instance="prod",
                    allow_empty_data=True,
                    data_source_mode="upstream_restore",
                    confirmation="restore opw upstream",
                ),
                dokploy_request=cast(DokployRequest, _request),
            )

        self.assertEqual(result.deploy_status, "pass")
        self.assertTrue(post_deploy.call_args.kwargs["run_destructive_restore"])

    def test_apply_blocks_managed_runtime_secret_without_safety_policy(self) -> None:
        store = _Store(
            profile=_profile_with_runtime_secret(),
            target_record=_target_record(),
            target_id_record=_target_id_record(),
            inventory=_inventory(),
        )
        store.secret_bindings = (
            SecretBinding(
                binding_id="secret-odoo-db-password-binding",
                secret_id="secret-odoo-db-password",
                integration="runtime_environment",
                binding_key="ODOO_DB_PASSWORD",
                context="cm",
                instance="testing",
                status="configured",
                created_at="2026-05-05T22:45:00Z",
                updated_at="2026-05-05T22:45:00Z",
            ),
        )

        with (
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_source.read_dokploy_config",
                return_value=("host", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.fetch_dokploy_target_payload",
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
                        )
                    ),
                },
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.latest_deployment_for_target",
                return_value={"deploymentId": "deploy-123", "status": "success"},
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.control_plane_runtime_environments.resolve_runtime_environment_values",
                return_value={"ODOO_DB_PASSWORD": "managed-secret-value"},
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_compose.sync_dokploy_compose_raw_source"
            ) as sync_source,
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.update_dokploy_target_env"
            ) as update_env,
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.trigger_deployment"
            ) as trigger_deploy,
        ):
            result = execute_odoo_stable_target_replacement_apply(
                control_plane_root=Path("."),
                record_store=store,
                request=OdooStableTargetReplacementApplyRequest(
                    product="odoo-tenant-cm", instance="testing"
                ),
                dokploy_request=cast(DokployRequest, _request),
            )

        self.assertEqual(result.deploy_status, "fail")
        self.assertIn("Runtime key-safety policy is unavailable", result.error_message)
        sync_source.assert_not_called()
        update_env.assert_not_called()
        trigger_deploy.assert_not_called()

    def test_apply_reports_disabled_runtime_secret_binding_key(self) -> None:
        store = _Store(
            profile=_profile_with_runtime_secret(),
            target_record=_target_record(),
            target_id_record=_target_id_record(),
            inventory=_inventory(),
        )
        store.secret_bindings = (
            SecretBinding(
                binding_id="secret-odoo-db-password-binding",
                secret_id="secret-odoo-db-password",
                integration="runtime_environment",
                binding_key="ODOO_DB_PASSWORD",
                context="cm",
                instance="testing",
                status="disabled",
                created_at="2026-05-05T22:45:00Z",
                updated_at="2026-05-05T22:45:00Z",
            ),
        )
        store.runtime_key_safety_policy_records = (
            RuntimeKeySafetyPolicyRecord(
                record_id="runtime-key-safety-policy-test",
                status="active",
                source="test",
                updated_at="2026-05-05T22:45:00Z",
                rules=(
                    RuntimeSecretSafetyRule(
                        binding_key="ODOO_DB_PASSWORD",
                        secret_class="testing",
                        allowed_contexts=("cm",),
                        allowed_instances=("testing",),
                    ),
                ),
            ),
        )

        with (
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_source.read_dokploy_config",
                return_value=("host", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.fetch_dokploy_target_payload",
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
                        )
                    ),
                },
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.latest_deployment_for_target",
                return_value={"deploymentId": "deploy-123", "status": "success"},
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.control_plane_runtime_environments.resolve_runtime_environment_values",
                return_value={"ODOO_DB_PASSWORD": "managed-secret-value"},
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_compose.sync_dokploy_compose_raw_source"
            ) as sync_source,
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.update_dokploy_target_env"
            ) as update_env,
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.trigger_deployment"
            ) as trigger_deploy,
        ):
            result = execute_odoo_stable_target_replacement_apply(
                control_plane_root=Path("."),
                record_store=store,
                request=OdooStableTargetReplacementApplyRequest(
                    product="odoo-tenant-cm", instance="testing"
                ),
                dokploy_request=cast(DokployRequest, _request),
            )

        self.assertEqual(result.deploy_status, "fail")
        self.assertIn("binding_disabled", result.error_message)
        self.assertIn("ODOO_DB_PASSWORD", result.error_message)
        sync_source.assert_not_called()
        update_env.assert_not_called()
        trigger_deploy.assert_not_called()

    def test_apply_reports_unclassified_runtime_secret_binding_key(self) -> None:
        store = _Store(
            profile=_profile_with_runtime_secret(),
            target_record=_target_record(),
            target_id_record=_target_id_record(),
            inventory=_inventory(),
        )
        store.secret_bindings = (
            SecretBinding(
                binding_id="secret-odoo-db-password-binding",
                secret_id="secret-odoo-db-password",
                integration="runtime_environment",
                binding_key="ODOO_DB_PASSWORD",
                context="cm",
                instance="testing",
                status="configured",
                created_at="2026-05-05T22:45:00Z",
                updated_at="2026-05-05T22:45:00Z",
            ),
        )
        store.runtime_key_safety_policy_records = (
            RuntimeKeySafetyPolicyRecord(
                record_id="runtime-key-safety-policy-test",
                status="active",
                source="test",
                updated_at="2026-05-05T22:45:00Z",
                rules=(
                    RuntimeSecretSafetyRule(
                        binding_key="OTHER_PASSWORD",
                        secret_class="testing",
                        allowed_contexts=("cm",),
                        allowed_instances=("testing",),
                    ),
                ),
            ),
        )

        with (
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_source.read_dokploy_config",
                return_value=("host", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.fetch_dokploy_target_payload",
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
                        )
                    ),
                },
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.latest_deployment_for_target",
                return_value={"deploymentId": "deploy-123", "status": "success"},
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.control_plane_runtime_environments.resolve_runtime_environment_values",
                return_value={"ODOO_DB_PASSWORD": "managed-secret-value"},
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_compose.sync_dokploy_compose_raw_source"
            ) as sync_source,
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.update_dokploy_target_env"
            ) as update_env,
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.trigger_deployment"
            ) as trigger_deploy,
        ):
            result = execute_odoo_stable_target_replacement_apply(
                control_plane_root=Path("."),
                record_store=store,
                request=OdooStableTargetReplacementApplyRequest(
                    product="odoo-tenant-cm", instance="testing"
                ),
                dokploy_request=cast(DokployRequest, _request),
            )

        self.assertEqual(result.deploy_status, "fail")
        self.assertIn("unclassified_binding", result.error_message)
        self.assertIn("ODOO_DB_PASSWORD", result.error_message)
        sync_source.assert_not_called()
        update_env.assert_not_called()
        trigger_deploy.assert_not_called()

    def test_apply_records_runtime_key_safety_pass_evidence(self) -> None:
        store = _Store(
            profile=_profile_with_runtime_secret(),
            target_record=_target_record(),
            target_id_record=_target_id_record(),
            inventory=_inventory(),
        )
        store.secret_bindings = (
            SecretBinding(
                binding_id="secret-odoo-db-password-binding",
                secret_id="secret-odoo-db-password",
                integration="runtime_environment",
                binding_key="ODOO_DB_PASSWORD",
                context="cm",
                instance="testing",
                status="configured",
                created_at="2026-05-05T22:45:00Z",
                updated_at="2026-05-05T22:45:00Z",
            ),
            SecretBinding(
                binding_id="secret-unrelated-token-binding",
                secret_id="secret-unrelated-token",
                integration="runtime_environment",
                binding_key="UNRELATED_TOKEN",
                context="cm",
                instance="testing",
                status="disabled",
                created_at="2026-05-05T22:45:00Z",
                updated_at="2026-05-05T22:45:00Z",
            ),
        )
        store.runtime_key_safety_policy_records = (
            RuntimeKeySafetyPolicyRecord(
                record_id="runtime-key-safety-policy-test",
                status="active",
                source="test",
                updated_at="2026-05-05T22:45:00Z",
                rules=(
                    RuntimeSecretSafetyRule(
                        binding_key="ODOO_DB_PASSWORD",
                        secret_class="testing",
                        allowed_contexts=("cm",),
                        allowed_instances=("testing",),
                    ),
                ),
            ),
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
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_source.read_dokploy_config",
                return_value=("host", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.fetch_dokploy_target_payload",
                side_effect=_fetch_target_payload,
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.latest_deployment_for_target",
                return_value={"deploymentId": "deploy-123", "status": "success"},
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.control_plane_runtime_environments.resolve_runtime_environment_values",
                return_value={"ODOO_DB_PASSWORD": "managed-secret-value"},
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_compose.sync_dokploy_compose_raw_source"
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_compose.ensure_compose_web_domain_route"
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_compose.fetch_dokploy_converted_compose_file",
                return_value=control_plane_dokploy.render_odoo_raw_compose_file(
                    image_reference="ghcr.io/cbusillo/odoo-tenant-cm@sha256:artifact",
                    domain_hosts=("cm-testing.shinycomputers.com",),
                    runtime_port=8069,
                ),
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.update_dokploy_target_env",
                side_effect=_update_env,
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.trigger_deployment"
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.wait_for_target_deployment"
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
                "control_plane.workflows.odoo_stable_target_replacement.verify_odoo_stable_readiness",
                return_value=_verification_result(),
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.wait_for_runtime_identity_healthcheck_with_retry",
                side_effect=_matching_runtime_identity_healthcheck,
            ),
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
        final_deployment = store.deployment_records[-1]
        self.assertEqual(final_deployment.runtime_source["runtime_key_safety_required"], "True")
        self.assertEqual(final_deployment.runtime_source["runtime_key_safety_status"], "pass")
        self.assertEqual(
            final_deployment.runtime_source["runtime_key_safety_policy_record_id"],
            "runtime-key-safety-policy-test",
        )

    def test_apply_refuses_explicit_artifact_source_mismatch(self) -> None:
        store = _Store(
            target_record=_target_record(),
            target_id_record=_target_id_record(),
            inventory=_inventory(),
        )
        with (
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_source.read_dokploy_config",
                return_value=("host", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.fetch_dokploy_target_payload",
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
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.latest_deployment_for_target",
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

    def test_apply_refuses_artifact_from_another_product_repository(self) -> None:
        foreign_manifest = _artifact_manifest(artifact_id="artifact-foreign")
        foreign_manifest.image.repository = "ghcr.io/cbusillo/other-product"
        store = _Store(
            target_record=_target_record(),
            target_id_record=_target_id_record(),
            inventory=_inventory(),
            artifact_manifests=(_artifact_manifest(), foreign_manifest),
        )
        with (
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_source.read_dokploy_config",
                return_value=("host", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.fetch_dokploy_target_payload",
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
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.latest_deployment_for_target",
                return_value={"deploymentId": "deploy-123", "status": "success"},
            ),
        ):
            with self.assertRaisesRegex(click.ClickException, "does not match product profile"):
                execute_odoo_stable_target_replacement_apply(
                    control_plane_root=Path("."),
                    record_store=store,
                    request=OdooStableTargetReplacementApplyRequest(
                        product="odoo-tenant-cm",
                        instance="testing",
                        artifact_id="artifact-foreign",
                        source_git_ref="abc1234",
                    ),
                    dokploy_request=cast(DokployRequest, _request),
                )

    def test_apply_rechecks_expected_current_artifact_before_provider_effects(self) -> None:
        store = _Store(
            target_record=_target_record(),
            target_id_record=_target_id_record(),
            inventory=_inventory(),
        )
        provider_effects: list[str] = []
        with (
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_source.read_dokploy_config",
                return_value=("host", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.fetch_dokploy_target_payload",
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
                "control_plane.workflows.odoo_stable_target_replacement.dokploy_api.latest_deployment_for_target",
                return_value={"deploymentId": "deploy-123", "status": "success"},
            ),
        ):
            with self.assertRaisesRegex(click.ClickException, "ready replacement plan"):
                execute_odoo_stable_target_replacement_apply(
                    control_plane_root=Path("."),
                    record_store=store,
                    request=OdooStableTargetReplacementApplyRequest(
                        product="odoo-tenant-cm",
                        instance="testing",
                        expected_current_artifact_id="artifact-before-worker-claim",
                    ),
                    dokploy_request=cast(DokployRequest, _request),
                    provider_effect_checkpoint=provider_effects.append,
                )

        self.assertEqual(provider_effects, [])

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
