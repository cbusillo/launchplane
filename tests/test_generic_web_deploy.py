from pathlib import Path
import unittest
from unittest.mock import patch

import click

from control_plane.contracts.deploy_target import DeployedTargetReference, ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.deployment_record import DeploymentRecord, ResolvedTargetEvidence
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.promotion_record import PostDeployUpdateEvidence
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
    ProductLaneHealthCheck,
    ProductLaneHealthMonitoringPolicy,
    ProductLaneProfile,
    ProductPreviewProfile,
)
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.contracts.secret_record import (
    SecretAuditEvent,
    SecretBinding,
    SecretRecord,
    SecretScope,
    SecretVersion,
)
from control_plane.contracts.ship_request import ShipRequest
from control_plane.dokploy import DokploySourceOfTruth, DokployTargetDefinition
from control_plane import secrets as control_plane_secrets
from control_plane.workflows.generic_web_deploy import (
    GenericWebDeployRequest,
    GenericWebDeployStore,
    GenericWebPostDeployContext,
    execute_generic_web_deploy,
    normalize_generic_web_artifact_id,
    resolve_generic_web_profile_lane,
)
from control_plane.workflows.generic_web_deploy_provider import DokployGenericWebDeployProvider
from control_plane.workflows.generic_web_deploy_provider import GenericWebResolvedDeployTarget


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


class _GenericWebDeployStore:
    def __init__(self, profile: LaunchplaneProductProfileRecord) -> None:
        self.profile = profile
        self.deployments: list[DeploymentRecord] = []
        self.inventories: list[EnvironmentInventory] = []

    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord:
        if product != self.profile.product:
            raise FileNotFoundError(product)
        return self.profile

    def write_deployment_record(self, record: DeploymentRecord) -> None:
        self.deployments.append(record)

    def write_environment_inventory(self, record: EnvironmentInventory) -> None:
        self.inventories.append(record)


class _DokployGenericWebDeployStore(_GenericWebDeployStore):
    def __init__(
        self,
        profile: LaunchplaneProductProfileRecord,
        *,
        provider_target: ProviderTargetRecord | None = None,
        dokploy_target: DokployTargetRecord | None = None,
        dokploy_target_id: DokployTargetIdRecord | None = None,
        runtime_environment_records: tuple[RuntimeEnvironmentRecord, ...] = (),
        secret_records: tuple[SecretRecord, ...] = (),
        secret_versions: tuple[SecretVersion, ...] = (),
        secret_bindings: tuple[SecretBinding, ...] = (),
    ) -> None:
        super().__init__(profile)
        self.runtime_environment_records = runtime_environment_records
        self.secret_records = secret_records
        self.secret_versions = secret_versions
        self.secret_bindings = secret_bindings
        self.provider_target = provider_target or ProviderTargetRecord(
            context="sellyouroutboard-testing",
            instance="testing",
            provider_id="dokploy",
            target_category="application",
            target_id="target-123",
            display_name="provider-target-app",
            provider_target_type="application",
            updated_at="2026-04-30T22:00:00Z",
            source_label="test",
        )
        self.dokploy_target = dokploy_target or DokployTargetRecord(
            context="sellyouroutboard-testing",
            instance="testing",
            target_type="application",
            target_name="dokploy-config-app",
            updated_at="2026-04-30T22:00:00Z",
            source_label="test",
        )
        self.dokploy_target_id = dokploy_target_id or DokployTargetIdRecord(
            context="sellyouroutboard-testing",
            instance="testing",
            target_id="target-123",
            updated_at="2026-04-30T22:00:00Z",
            source_label="test",
        )

    def read_provider_target_record(
        self, *, context_name: str, instance_name: str
    ) -> ProviderTargetRecord:
        if (
            context_name == self.provider_target.context
            and instance_name == self.provider_target.instance
        ):
            return self.provider_target
        raise FileNotFoundError(f"{context_name}/{instance_name}")

    def read_dokploy_target_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetRecord:
        if (
            context_name == self.dokploy_target.context
            and instance_name == self.dokploy_target.instance
        ):
            return self.dokploy_target
        raise FileNotFoundError(f"{context_name}/{instance_name}")

    def read_dokploy_target_id_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetIdRecord:
        if (
            context_name == self.dokploy_target_id.context
            and instance_name == self.dokploy_target_id.instance
        ):
            return self.dokploy_target_id
        raise FileNotFoundError(f"{context_name}/{instance_name}")

    def list_runtime_environment_records(
        self, *, context_name: str = "", instance_name: str = ""
    ) -> tuple[RuntimeEnvironmentRecord, ...]:
        return tuple(
            record
            for record in self.runtime_environment_records
            if (not context_name or record.context == context_name)
            and (not instance_name or record.instance == instance_name)
        )

    def list_secret_records(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[SecretRecord, ...]:
        records = tuple(
            record
            for record in self.secret_records
            if (not integration or record.integration == integration)
            and (not context_name or record.context == context_name)
            and (not instance_name or record.instance == instance_name)
        )
        return records[:limit] if limit is not None else records

    def read_secret_record(self, secret_id: str) -> SecretRecord:
        for record in self.secret_records:
            if record.secret_id == secret_id:
                return record
        raise FileNotFoundError(secret_id)

    def read_secret_version(self, version_id: str) -> SecretVersion:
        for version in self.secret_versions:
            if version.version_id == version_id:
                return version
        raise FileNotFoundError(version_id)

    def list_secret_versions(self, *, secret_id: str) -> tuple[SecretVersion, ...]:
        return tuple(version for version in self.secret_versions if version.secret_id == secret_id)

    def list_secret_bindings(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[SecretBinding, ...]:
        bindings = tuple(
            binding
            for binding in self.secret_bindings
            if (not integration or binding.integration == integration)
            and (not context_name or binding.context == context_name)
            and (not instance_name or binding.instance == instance_name)
        )
        return bindings[:limit] if limit is not None else bindings

    def list_secret_audit_events(self, *, secret_id: str) -> tuple[SecretAuditEvent, ...]:
        del secret_id
        return ()


def _profile(*, driver_id: str = "generic-web") -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord(
        product="sellyouroutboard",
        display_name="SellYourOutboard.com",
        repository="cbusillo/sellyouroutboard",
        driver_id=driver_id,
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


def _source_ref_worker_profile() -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord(
        product="repairshopr-sync",
        display_name="RepairShopr Sync",
        repository="cbusillo/repairshopr_api",
        driver_id="generic-web",
        image=ProductImageProfile(),
        lanes=(
            ProductLaneProfile(
                instance="prod",
                context="repairshopr-sync",
                health_monitoring=ProductLaneHealthMonitoringPolicy(checks=()),
            ),
        ),
        updated_at="2026-06-12T20:00:00Z",
        source="test",
    )


def _runtime_secret_record(
    *,
    secret_id: str,
    scope: SecretScope,
    name: str,
    version_id: str,
    context: str = "",
    instance: str = "",
) -> SecretRecord:
    return SecretRecord(
        secret_id=secret_id,
        scope=scope,
        integration=control_plane_secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
        name=name,
        context=context,
        instance=instance,
        description="Runtime secret test fixture",
        current_version_id=version_id,
        created_at="2026-04-30T22:00:00Z",
        updated_at="2026-04-30T22:00:00Z",
    )


def _runtime_secret_version(
    *, secret_id: str, version_id: str, plaintext_value: str
) -> SecretVersion:
    return SecretVersion(
        version_id=version_id,
        secret_id=secret_id,
        created_at="2026-04-30T22:00:00Z",
        ciphertext=control_plane_secrets._encrypt_secret_value(plaintext_value),
    )


def _runtime_secret_binding(
    *,
    secret_id: str,
    binding_key: str,
    context: str = "",
    instance: str = "",
    binding_id_suffix: str | None = None,
    updated_at: str = "2026-04-30T22:00:00Z",
) -> SecretBinding:
    return SecretBinding(
        binding_id=f"{secret_id}-binding-{binding_id_suffix or binding_key.lower().replace('_', '-')}",
        secret_id=secret_id,
        integration=control_plane_secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
        binding_key=binding_key,
        context=context,
        instance=instance,
        created_at="2026-04-30T22:00:00Z",
        updated_at=updated_at,
    )


def _source_ref_worker_lane() -> ProductLaneProfile:
    return ProductLaneProfile(
        instance="prod",
        context="repairshopr-sync",
        health_monitoring=ProductLaneHealthMonitoringPolicy(checks=()),
    )


def _request(instance: str = "testing") -> GenericWebDeployRequest:
    return GenericWebDeployRequest(
        product="sellyouroutboard",
        instance=instance,
        artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
        source_git_ref="abc123",
    )


class _FakeGenericWebDeployProvider:
    provider_id = "fake-cloud"
    delegated_executor = "control-plane.fake-cloud"

    def __init__(self, *, deploy_error: click.ClickException | None = None) -> None:
        self.deploy_error = deploy_error
        self.resolved_targets: list[GenericWebResolvedDeployTarget] = []
        self.runtime_identities: list[RuntimeIdentity] = []

    def resolve_deploy_target(
        self,
        *,
        control_plane_root: Path,
        request_artifact_id: str,
        request_source_git_ref: str,
        request_timeout_seconds: int | None,
        request_no_cache: bool,
        record_store: object,
        profile: LaunchplaneProductProfileRecord,
        lane: ProductLaneProfile,
        normalized_artifact_id: str,
        fallback_target_name: str,
    ) -> GenericWebResolvedDeployTarget:
        del control_plane_root, request_artifact_id, record_store, profile, fallback_target_name
        resolved = GenericWebResolvedDeployTarget(
            ship_request=ShipRequest(
                artifact_id=normalized_artifact_id,
                context=lane.context,
                instance=lane.instance,
                source_git_ref=request_source_git_ref,
                target_name="sellyouroutboard-testing-app",
                target_type="application",
                deploy_mode="fake-cloud-service-api",
                provider_id=self.provider_id,
                target_category="service",
                provider_target_type="managed-service",
                provider_deploy_mode="service-api",
                wait=True,
                timeout_seconds=request_timeout_seconds,
                verify_health=False,
                no_cache=request_no_cache,
            ),
            resolved_target=ResolvedTargetEvidence(
                target_type="application",
                target_id="target-123",
                target_name="sellyouroutboard-testing-app",
            ),
            deployed_target=DeployedTargetReference(
                provider_id=self.provider_id,
                target_category="service",
                target_id="target-123",
                display_name="sellyouroutboard-testing-app",
                provider_target_type="managed-service",
            ),
            deploy_timeout_seconds=request_timeout_seconds or 600,
        )
        self.resolved_targets.append(resolved)
        return resolved

    def execute_artifact_deploy(
        self,
        *,
        control_plane_root: Path,
        resolved_deploy_target: GenericWebResolvedDeployTarget,
        runtime_identity: RuntimeIdentity,
    ) -> None:
        del control_plane_root, resolved_deploy_target
        self.runtime_identities.append(runtime_identity)
        if self.deploy_error is not None:
            raise self.deploy_error


class GenericWebDeployTests(unittest.TestCase):
    def test_normalize_generic_web_artifact_id_qualifies_bare_release_tag(self) -> None:
        artifact_id = normalize_generic_web_artifact_id(
            profile=_profile(),
            artifact_id="sha-2da6435e10cade0870ed5cbdf40c8048594f8b1c",
        )

        self.assertEqual(
            artifact_id,
            "ghcr.io/cbusillo/sellyouroutboard:sha-2da6435e10cade0870ed5cbdf40c8048594f8b1c",
        )

    def test_normalize_generic_web_artifact_id_rejects_other_repositories(self) -> None:
        with self.assertRaises(click.ClickException):
            normalize_generic_web_artifact_id(
                profile=_profile(),
                artifact_id="ghcr.io/cbusillo/other-app:sha-abc123",
            )

    def test_normalize_generic_web_artifact_id_rejects_worker_profile_without_image(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            click.ClickException,
            "Configure an immutable image repository before using generic-web deploy",
        ):
            normalize_generic_web_artifact_id(
                profile=_source_ref_worker_profile(),
                artifact_id="sha-2da6435e10cade0870ed5cbdf40c8048594f8b1c",
            )

    def test_product_profile_write_contract_rejects_inert_health_monitoring(
        self,
    ) -> None:
        profile = LaunchplaneProductProfileRecord(
            product="repairshopr-sync",
            display_name="RepairShopr Sync",
            repository="cbusillo/repairshopr_api",
            driver_id="generic-web",
            image=ProductImageProfile(),
            lanes=(
                ProductLaneProfile(
                    instance="prod",
                    context="repairshopr-sync",
                    health_monitoring=ProductLaneHealthMonitoringPolicy(
                        checks=(ProductLaneHealthCheck(name="public-ingress"),)
                    ),
                ),
            ),
            updated_at="2026-06-12T20:00:00Z",
            source="test",
        )

        with self.assertRaisesRegex(ValueError, "requires base_url or explicit health_url"):
            profile.validate_write_contract()

    def test_product_profile_write_contract_rejects_base_url_without_health_path(
        self,
    ) -> None:
        profile = LaunchplaneProductProfileRecord(
            product="repairshopr-sync",
            display_name="RepairShopr Sync",
            repository="cbusillo/repairshopr_api",
            driver_id="generic-web",
            image=ProductImageProfile(),
            lanes=(
                ProductLaneProfile(
                    instance="prod",
                    context="repairshopr-sync",
                    base_url="https://repairshopr-sync.example.test",
                    health_monitoring=ProductLaneHealthMonitoringPolicy(
                        checks=(ProductLaneHealthCheck(name="public-ingress"),)
                    ),
                ),
            ),
            updated_at="2026-06-12T20:00:00Z",
            source="test",
        )

        with self.assertRaisesRegex(ValueError, "base_url requires health_path"):
            profile.validate_write_contract()

    def test_product_profile_write_contract_accepts_private_endpoint_key(
        self,
    ) -> None:
        profile = LaunchplaneProductProfileRecord(
            product="repairshopr-sync",
            display_name="RepairShopr Sync",
            repository="cbusillo/repairshopr_api",
            driver_id="generic-web",
            image=ProductImageProfile(),
            lanes=(
                ProductLaneProfile(
                    instance="prod",
                    context="repairshopr-sync",
                    health_monitoring=ProductLaneHealthMonitoringPolicy(
                        checks=(
                            ProductLaneHealthCheck(
                                name="private-runtime",
                                kind="private_http",
                                private_endpoint_key="repairshopr-sync-prod-runtime",
                            ),
                        )
                    ),
                ),
            ),
            updated_at="2026-06-12T20:00:00Z",
            source="test",
        )

        self.assertIs(profile.validate_write_contract(), profile)

    def test_private_http_check_rejects_inline_url(self) -> None:
        with self.assertRaisesRegex(ValueError, "must use private_endpoint_key"):
            ProductLaneHealthCheck(
                name="private-runtime",
                kind="private_http",
                url="http://10.0.0.5:8080/health",
            )

    def test_public_http_check_rejects_private_endpoint_key(self) -> None:
        with self.assertRaisesRegex(ValueError, "only private HTTP"):
            ProductLaneHealthCheck(
                name="public-ingress",
                private_endpoint_key="repairshopr-sync-prod-runtime",
            )

    def test_product_profile_rejects_zero_runtime_port_with_health_path(self) -> None:
        with self.assertRaisesRegex(ValueError, "runtime_port=0 cannot set health_path"):
            LaunchplaneProductProfileRecord(
                product="repairshopr-sync",
                display_name="RepairShopr Sync",
                repository="cbusillo/repairshopr_api",
                driver_id="generic-web",
                image=ProductImageProfile(),
                runtime_port=0,
                health_path="/health",
                lanes=(_source_ref_worker_lane(),),
                updated_at="2026-06-12T20:00:00Z",
                source="test",
            )

    def test_product_profile_rejects_runtime_port_without_health_path(self) -> None:
        with self.assertRaisesRegex(ValueError, "runtime_port requires health_path"):
            LaunchplaneProductProfileRecord(
                product="repairshopr-sync",
                display_name="RepairShopr Sync",
                repository="cbusillo/repairshopr_api",
                driver_id="generic-web",
                image=ProductImageProfile(),
                runtime_port=8000,
                lanes=(_source_ref_worker_lane(),),
                updated_at="2026-06-12T20:00:00Z",
                source="test",
            )

    def test_execute_generic_web_deploy_writes_pass_record_for_profile_lane(self) -> None:
        store = _GenericWebDeployStore(_profile())
        deploy_provider = _FakeGenericWebDeployProvider()

        result = execute_generic_web_deploy(
            control_plane_root=Path("."),
            record_store=store,
            request=_request(),
            deploy_provider=deploy_provider,
        )

        self.assertEqual(result.deploy_status, "pass")
        self.assertEqual(result.context, "sellyouroutboard-testing")
        self.assertEqual(result.target_id, "target-123")
        self.assertEqual(result.target_category, "service")
        self.assertEqual(result.provider_id, "fake-cloud")
        self.assertEqual(result.provider_target_type, "managed-service")
        self.assertFalse(hasattr(result, "target_type"))
        self.assertEqual(len(store.deployments), 1)
        self.assertEqual(store.deployments[0].deploy.status, "pass")
        self.assertEqual(store.deployments[0].post_deploy_update.status, "skipped")
        self.assertEqual(len(store.inventories), 1)
        self.assertEqual(store.inventories[0].context, "sellyouroutboard-testing")
        self.assertEqual(store.inventories[0].instance, "testing")
        self.assertEqual(store.inventories[0].source_git_ref, "abc123")
        self.assertEqual(
            store.inventories[0].deployment_record_id,
            store.deployments[0].record_id,
        )
        runtime_identity = store.deployments[0].runtime_identity
        assert runtime_identity is not None
        self.assertEqual(runtime_identity.product, "sellyouroutboard")
        self.assertEqual(runtime_identity.context, "sellyouroutboard-testing")
        self.assertEqual(runtime_identity.instance, "testing")
        self.assertEqual(runtime_identity.deployment_record_id, store.deployments[0].record_id)
        self.assertEqual(
            runtime_identity.artifact_id,
            "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
        )
        self.assertEqual(runtime_identity.source_git_ref, "abc123")
        self.assertEqual(store.inventories[0].runtime_identity, runtime_identity)
        artifact_identity = store.deployments[0].artifact_identity
        assert artifact_identity is not None
        self.assertEqual(
            artifact_identity.artifact_id,
            "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
        )
        self.assertIsNotNone(store.deployments[0].resolved_target)
        resolved_target = store.deployments[0].resolved_target
        assert resolved_target is not None
        self.assertEqual(resolved_target.target_name, "sellyouroutboard-testing-app")
        self.assertEqual(
            store.deployments[0].delegated_executor,
            "control-plane.fake-cloud",
        )
        self.assertEqual(store.deployments[0].deploy.provider_id, "fake-cloud")
        self.assertEqual(store.deployments[0].deploy.target_category, "service")
        deployed_target = store.deployments[0].deployed_target
        assert deployed_target is not None
        self.assertEqual(deployed_target.provider_id, "fake-cloud")
        self.assertEqual(deployed_target.target_category, "service")
        self.assertEqual(deployed_target.provider_target_type, "managed-service")
        self.assertEqual(len(deploy_provider.runtime_identities), 1)
        self.assertEqual(
            deploy_provider.runtime_identities[0].deployment_record_id,
            store.deployments[0].record_id,
        )

    def test_execute_generic_web_deploy_records_failure_for_source_ref_worker_profile(
        self,
    ) -> None:
        store = _GenericWebDeployStore(_source_ref_worker_profile())
        deploy_provider = _FakeGenericWebDeployProvider()

        result = execute_generic_web_deploy(
            control_plane_root=Path("."),
            record_store=store,
            request=GenericWebDeployRequest(
                product="repairshopr-sync",
                instance="prod",
                artifact_id="refs/heads/main",
                source_git_ref="abc123",
            ),
            deploy_provider=deploy_provider,
        )

        self.assertEqual(result.deploy_status, "fail")
        self.assertIn("requires product image.repository", result.error_message)
        self.assertIn("immutable image repository", result.error_message)
        self.assertNotIn("source-ref deploy", result.error_message)
        self.assertEqual(len(store.deployments), 1)
        self.assertEqual(store.deployments[0].deploy.status, "fail")
        self.assertEqual(store.deployments[0].context, "repairshopr-sync")
        self.assertEqual(store.deployments[0].instance, "prod")
        artifact_identity = store.deployments[0].artifact_identity
        assert artifact_identity is not None
        self.assertEqual(artifact_identity.artifact_id, "refs/heads/main")
        self.assertEqual(deploy_provider.resolved_targets, [])

    def test_execute_generic_web_deploy_runs_post_deploy_extension_when_supplied(
        self,
    ) -> None:
        store = _GenericWebDeployStore(_profile())
        contexts: list[GenericWebPostDeployContext] = []

        def post_deploy(
            _root: Path,
            _store: GenericWebDeployStore,
            context: GenericWebPostDeployContext,
        ) -> PostDeployUpdateEvidence:
            contexts.append(context)
            return PostDeployUpdateEvidence(
                attempted=True,
                status="pass",
                detail="Product post-deploy extension completed.",
            )

        result = execute_generic_web_deploy(
            control_plane_root=Path("."),
            record_store=store,
            request=_request(),
            post_deploy_executor=post_deploy,
            deploy_provider=_FakeGenericWebDeployProvider(),
        )

        self.assertEqual(result.deploy_status, "pass")
        self.assertEqual(result.post_deploy_status, "pass")
        self.assertEqual(store.deployments[0].post_deploy_update.status, "pass")
        self.assertEqual(store.inventories[0].post_deploy_update.status, "pass")
        self.assertEqual(len(contexts), 1)
        self.assertEqual(contexts[0].product, "sellyouroutboard")
        self.assertEqual(contexts[0].deployment_record_id, store.deployments[0].record_id)
        self.assertEqual(contexts[0].target_id, "target-123")
        self.assertEqual(contexts[0].target_category, "service")
        self.assertEqual(contexts[0].provider_id, "fake-cloud")
        self.assertEqual(contexts[0].provider_target_type, "managed-service")
        self.assertEqual(contexts[0].target_type, "application")

    def test_execute_generic_web_deploy_keeps_deploy_pass_when_post_deploy_extension_fails(
        self,
    ) -> None:
        store = _GenericWebDeployStore(_profile())

        def post_deploy(
            _root: Path,
            _store: GenericWebDeployStore,
            _context: GenericWebPostDeployContext,
        ) -> PostDeployUpdateEvidence:
            raise click.ClickException("post deploy failed")

        result = execute_generic_web_deploy(
            control_plane_root=Path("."),
            record_store=store,
            request=_request(),
            post_deploy_executor=post_deploy,
            deploy_provider=_FakeGenericWebDeployProvider(),
        )

        self.assertEqual(result.deploy_status, "pass")
        self.assertEqual(result.post_deploy_status, "fail")
        self.assertEqual(result.error_message, "post deploy failed")
        self.assertEqual(result.target_category, "service")
        self.assertEqual(result.provider_id, "fake-cloud")
        self.assertEqual(result.provider_target_type, "managed-service")
        self.assertEqual(store.deployments[0].deploy.status, "pass")
        self.assertEqual(store.deployments[0].post_deploy_update.status, "fail")
        self.assertIsNotNone(store.deployments[0].runtime_identity)
        self.assertEqual(len(store.inventories), 1)
        self.assertEqual(store.inventories[0].post_deploy_update.status, "fail")
        self.assertEqual(
            store.inventories[0].deployment_record_id,
            store.deployments[0].record_id,
        )

    def test_execute_generic_web_deploy_records_unexpected_post_deploy_exception(
        self,
    ) -> None:
        store = _GenericWebDeployStore(_profile())

        def post_deploy(
            _root: Path,
            _store: GenericWebDeployStore,
            _context: GenericWebPostDeployContext,
        ) -> PostDeployUpdateEvidence:
            raise RuntimeError("unexpected post deploy failure")

        result = execute_generic_web_deploy(
            control_plane_root=Path("."),
            record_store=store,
            request=_request(),
            post_deploy_executor=post_deploy,
            deploy_provider=_FakeGenericWebDeployProvider(),
        )

        self.assertEqual(result.deploy_status, "pass")
        self.assertEqual(result.post_deploy_status, "fail")
        self.assertEqual(result.error_message, "unexpected post deploy failure")
        self.assertEqual(store.deployments[0].deploy.status, "pass")
        self.assertEqual(store.deployments[0].post_deploy_update.status, "fail")
        self.assertEqual(
            store.deployments[0].post_deploy_update.detail,
            "unexpected post deploy failure",
        )
        self.assertIsNotNone(store.deployments[0].runtime_identity)
        self.assertEqual(len(store.inventories), 1)
        self.assertEqual(store.inventories[0].post_deploy_update.status, "fail")

    def test_execute_generic_web_deploy_treats_returned_post_deploy_failure_as_failed_extension(
        self,
    ) -> None:
        store = _GenericWebDeployStore(_profile())

        def post_deploy(
            _root: Path,
            _store: GenericWebDeployStore,
            _context: GenericWebPostDeployContext,
        ) -> PostDeployUpdateEvidence:
            return PostDeployUpdateEvidence(
                attempted=True,
                status="fail",
                detail="returned failure",
            )

        result = execute_generic_web_deploy(
            control_plane_root=Path("."),
            record_store=store,
            request=_request(),
            post_deploy_executor=post_deploy,
            deploy_provider=_FakeGenericWebDeployProvider(),
        )

        self.assertEqual(result.deploy_status, "pass")
        self.assertEqual(result.post_deploy_status, "fail")
        self.assertEqual(result.error_message, "returned failure")
        self.assertEqual(store.deployments[0].deploy.status, "pass")
        self.assertEqual(store.deployments[0].post_deploy_update.status, "fail")
        self.assertEqual(store.deployments[0].post_deploy_update.detail, "returned failure")
        self.assertIsNotNone(store.deployments[0].runtime_identity)
        self.assertEqual(len(store.inventories), 1)
        self.assertEqual(store.inventories[0].post_deploy_update.status, "fail")

    def test_execute_generic_web_deploy_uses_qualified_bare_tag(self) -> None:
        store = _GenericWebDeployStore(_profile())

        deploy_provider = _FakeGenericWebDeployProvider()
        execute_generic_web_deploy(
            control_plane_root=Path("."),
            record_store=store,
            request=GenericWebDeployRequest(
                product="sellyouroutboard",
                instance="testing",
                artifact_id="sha-2da6435e10cade0870ed5cbdf40c8048594f8b1c",
                source_git_ref="2da6435e10cade0870ed5cbdf40c8048594f8b1c",
            ),
            deploy_provider=deploy_provider,
        )

        deploy_request = deploy_provider.resolved_targets[0].ship_request
        expected_artifact_id = (
            "ghcr.io/cbusillo/sellyouroutboard:sha-2da6435e10cade0870ed5cbdf40c8048594f8b1c"
        )
        self.assertEqual(deploy_request.artifact_id, expected_artifact_id)
        artifact_identity = store.deployments[0].artifact_identity
        assert artifact_identity is not None
        self.assertEqual(artifact_identity.artifact_id, expected_artifact_id)

    def test_execute_generic_web_deploy_records_failure_when_provider_fails(self) -> None:
        store = _GenericWebDeployStore(_profile())

        result = execute_generic_web_deploy(
            control_plane_root=Path("."),
            record_store=store,
            request=_request(),
            deploy_provider=_FakeGenericWebDeployProvider(
                deploy_error=click.ClickException("provider failed")
            ),
        )

        self.assertEqual(result.deploy_status, "fail")
        self.assertEqual(result.error_message, "provider failed")
        self.assertEqual(result.target_category, "service")
        self.assertEqual(result.provider_id, "fake-cloud")
        self.assertEqual(result.provider_target_type, "managed-service")
        self.assertEqual(len(store.deployments), 1)
        self.assertEqual(store.deployments[0].deploy.status, "fail")
        self.assertEqual(store.inventories, [])

    def test_execute_generic_web_deploy_records_provider_resolution_failures_against_provider(
        self,
    ) -> None:
        class FailingResolveProvider(_FakeGenericWebDeployProvider):
            def resolve_deploy_target(
                self,
                *,
                control_plane_root: Path,
                request_artifact_id: str,
                request_source_git_ref: str,
                request_timeout_seconds: int | None,
                request_no_cache: bool,
                record_store: object,
                profile: LaunchplaneProductProfileRecord,
                lane: ProductLaneProfile,
                normalized_artifact_id: str,
                fallback_target_name: str,
            ) -> GenericWebResolvedDeployTarget:
                del (
                    control_plane_root,
                    request_artifact_id,
                    request_source_git_ref,
                    request_timeout_seconds,
                    request_no_cache,
                    record_store,
                    profile,
                    lane,
                    normalized_artifact_id,
                    fallback_target_name,
                )
                raise click.ClickException("target missing")

        store = _GenericWebDeployStore(_profile())

        result = execute_generic_web_deploy(
            control_plane_root=Path("."),
            record_store=store,
            request=GenericWebDeployRequest(
                product="sellyouroutboard",
                instance="testing",
                artifact_id="sha-2da6435e10cade0870ed5cbdf40c8048594f8b1c",
                source_git_ref="2da6435e10cade0870ed5cbdf40c8048594f8b1c",
            ),
            deploy_provider=FailingResolveProvider(),
        )

        expected_artifact_id = (
            "ghcr.io/cbusillo/sellyouroutboard:sha-2da6435e10cade0870ed5cbdf40c8048594f8b1c"
        )
        self.assertEqual(result.deploy_status, "fail")
        self.assertEqual(result.error_message, "target missing")
        self.assertEqual(len(store.deployments), 1)
        self.assertEqual(store.deployments[0].delegated_executor, "control-plane.fake-cloud")
        self.assertEqual(store.deployments[0].deploy.provider_id, "fake-cloud")
        self.assertEqual(
            store.deployments[0].deploy.provider_target_type,
            "application",
        )
        self.assertEqual(
            store.deployments[0].deploy.provider_deploy_mode,
            "fake-cloud-application-api",
        )
        artifact_identity = store.deployments[0].artifact_identity
        assert artifact_identity is not None
        self.assertEqual(artifact_identity.artifact_id, expected_artifact_id)
        self.assertEqual(store.inventories, [])

    def test_execute_generic_web_deploy_rejects_invalid_image_artifact_without_record(
        self,
    ) -> None:
        store = _GenericWebDeployStore(_profile())

        with self.assertRaisesRegex(click.ClickException, "must use the product image repository"):
            execute_generic_web_deploy(
                control_plane_root=Path("."),
                record_store=store,
                request=GenericWebDeployRequest(
                    product="sellyouroutboard",
                    instance="testing",
                    artifact_id="ghcr.io/cbusillo/other-app:sha-abc123",
                    source_git_ref="abc123",
                ),
                deploy_provider=_FakeGenericWebDeployProvider(),
            )

        self.assertEqual(store.deployments, [])
        self.assertEqual(store.inventories, [])

    def test_dokploy_provider_resolves_target_without_generic_web_dokploy_imports(
        self,
    ) -> None:
        provider = DokployGenericWebDeployProvider()
        store = _DokployGenericWebDeployStore(_profile())

        resolved = provider.resolve_deploy_target(
            control_plane_root=Path("."),
            request_artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
            request_source_git_ref="abc123",
            request_timeout_seconds=45,
            request_no_cache=True,
            record_store=store,
            profile=_profile(),
            lane=_profile().lanes[0],
            normalized_artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
            fallback_target_name="fallback-target",
        )

        self.assertEqual(resolved.ship_request.provider_id, "dokploy")
        self.assertEqual(resolved.ship_request.deploy_mode, "dokploy-application-api")
        self.assertEqual(resolved.ship_request.target_category, "application")
        self.assertEqual(resolved.ship_request.provider_target_type, "application")
        self.assertEqual(resolved.ship_request.target_name, "provider-target-app")
        self.assertEqual(resolved.resolved_target.target_id, "target-123")
        self.assertEqual(resolved.resolved_target.target_name, "provider-target-app")
        deployed_target = resolved.deployed_target
        assert deployed_target is not None
        self.assertEqual(deployed_target.display_name, "provider-target-app")
        self.assertEqual(resolved.deploy_timeout_seconds, 45)

    def test_dokploy_provider_blocks_missing_provider_target_authority(self) -> None:
        class MissingProviderTargetStore(_DokployGenericWebDeployStore):
            def read_provider_target_record(
                self, *, context_name: str, instance_name: str
            ) -> ProviderTargetRecord:
                raise FileNotFoundError(f"{context_name}/{instance_name}")

        provider = DokployGenericWebDeployProvider()
        store = MissingProviderTargetStore(_profile())

        with self.assertRaisesRegex(click.ClickException, "Missing provider-target authority"):
            provider.resolve_deploy_target(
                control_plane_root=Path("."),
                request_artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                request_source_git_ref="abc123",
                request_timeout_seconds=45,
                request_no_cache=True,
                record_store=store,
                profile=_profile(),
                lane=_profile().lanes[0],
                normalized_artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                fallback_target_name="fallback-target",
            )

    def test_dokploy_provider_blocks_target_id_mismatch(self) -> None:
        provider = DokployGenericWebDeployProvider()
        store = _DokployGenericWebDeployStore(
            _profile(),
            provider_target=ProviderTargetRecord(
                context="sellyouroutboard-testing",
                instance="testing",
                provider_id="dokploy",
                target_category="application",
                target_id="provider-target-id",
                display_name="provider-target-app",
                provider_target_type="application",
                updated_at="2026-04-30T22:00:00Z",
                source_label="test",
            ),
        )

        with self.assertRaisesRegex(click.ClickException, "Provider-target id mismatch"):
            provider.resolve_deploy_target(
                control_plane_root=Path("."),
                request_artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                request_source_git_ref="abc123",
                request_timeout_seconds=45,
                request_no_cache=True,
                record_store=store,
                profile=_profile(),
                lane=_profile().lanes[0],
                normalized_artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                fallback_target_name="fallback-target",
            )

    def test_dokploy_provider_resolves_selected_lane_without_global_target_scan(
        self,
    ) -> None:
        class StoreWithDangerousListMethods(_DokployGenericWebDeployStore):
            def list_dokploy_target_records(self) -> tuple[DokployTargetRecord, ...]:
                raise AssertionError("deploy resolution must not scan all Dokploy targets")

            def list_dokploy_target_id_records(self) -> tuple[DokployTargetIdRecord, ...]:
                raise AssertionError("deploy resolution must not scan all Dokploy target ids")

        provider = DokployGenericWebDeployProvider()
        store = StoreWithDangerousListMethods(_profile())

        resolved = provider.resolve_deploy_target(
            control_plane_root=Path("."),
            request_artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
            request_source_git_ref="abc123",
            request_timeout_seconds=45,
            request_no_cache=True,
            record_store=store,
            profile=_profile(),
            lane=_profile().lanes[0],
            normalized_artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
            fallback_target_name="fallback-target",
        )

        self.assertEqual(resolved.resolved_target.target_id, "target-123")

    def test_dokploy_provider_uses_db_runtime_environment_ship_mode_override(
        self,
    ) -> None:
        provider = DokployGenericWebDeployProvider()
        store = _DokployGenericWebDeployStore(
            _profile(),
            runtime_environment_records=(
                RuntimeEnvironmentRecord(
                    scope="context",
                    context="sellyouroutboard-testing",
                    env={"DOKPLOY_SHIP_MODE": "compose"},
                    updated_at="2026-04-30T22:00:00Z",
                    source_label="test",
                ),
            ),
        )

        resolved = provider.resolve_deploy_target(
            control_plane_root=Path("."),
            request_artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
            request_source_git_ref="abc123",
            request_timeout_seconds=45,
            request_no_cache=True,
            record_store=store,
            profile=_profile(),
            lane=_profile().lanes[0],
            normalized_artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
            fallback_target_name="fallback-target",
        )

        self.assertEqual(resolved.ship_request.deploy_mode, "dokploy-compose-api")

    def test_dokploy_provider_ignores_sparse_runtime_environment_records(
        self,
    ) -> None:
        provider = DokployGenericWebDeployProvider()
        store = _DokployGenericWebDeployStore(
            _profile(),
            runtime_environment_records=(
                RuntimeEnvironmentRecord(
                    scope="context",
                    context="other-context",
                    env={"DOKPLOY_SHIP_MODE": "compose"},
                    updated_at="2026-04-30T22:00:00Z",
                    source_label="test",
                ),
            ),
        )

        resolved = provider.resolve_deploy_target(
            control_plane_root=Path("."),
            request_artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
            request_source_git_ref="abc123",
            request_timeout_seconds=45,
            request_no_cache=True,
            record_store=store,
            profile=_profile(),
            lane=_profile().lanes[0],
            normalized_artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
            fallback_target_name="fallback-target",
        )

        self.assertEqual(resolved.ship_request.deploy_mode, "dokploy-application-api")

    def test_dokploy_provider_uses_managed_runtime_secret_ship_mode_override(
        self,
    ) -> None:
        provider = DokployGenericWebDeployProvider()
        secret_id = "secret-runtime-ship-mode-syo-testing"
        version_id = f"{secret_id}-version-current"

        with patch.dict(
            "os.environ",
            {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
        ):
            store = _DokployGenericWebDeployStore(
                _profile(),
                secret_records=(
                    _runtime_secret_record(
                        secret_id=secret_id,
                        scope="context",
                        name="dokploy-ship-mode",
                        version_id=version_id,
                        context="sellyouroutboard-testing",
                    ),
                ),
                secret_versions=(
                    _runtime_secret_version(
                        secret_id=secret_id,
                        version_id=version_id,
                        plaintext_value="compose",
                    ),
                ),
                secret_bindings=(
                    _runtime_secret_binding(
                        secret_id=secret_id,
                        binding_key="STALE_DOKPLOY_SHIP_MODE",
                        context="other-context",
                        binding_id_suffix="stale",
                        updated_at="2026-04-30T22:01:00Z",
                    ),
                    _runtime_secret_binding(
                        secret_id=secret_id,
                        binding_key="DOKPLOY_SHIP_MODE",
                        context="sellyouroutboard-testing",
                    ),
                ),
            )
            resolved = provider.resolve_deploy_target(
                control_plane_root=Path("."),
                request_artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                request_source_git_ref="abc123",
                request_timeout_seconds=45,
                request_no_cache=True,
                record_store=store,
                profile=_profile(),
                lane=_profile().lanes[0],
                normalized_artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                fallback_target_name="fallback-target",
            )

        self.assertEqual(resolved.ship_request.deploy_mode, "dokploy-compose-api")

    def test_dokploy_provider_prefers_context_instance_runtime_secret_over_global(
        self,
    ) -> None:
        provider = DokployGenericWebDeployProvider()
        global_secret_id = "secret-runtime-ship-mode-global"
        global_version_id = f"{global_secret_id}-version-current"
        instance_secret_id = "secret-runtime-ship-mode-syo-testing-instance"
        instance_version_id = f"{instance_secret_id}-version-current"

        with patch.dict(
            "os.environ",
            {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
        ):
            store = _DokployGenericWebDeployStore(
                _profile(),
                secret_records=(
                    _runtime_secret_record(
                        secret_id=global_secret_id,
                        scope="global",
                        name="dokploy-ship-mode-global",
                        version_id=global_version_id,
                    ),
                    _runtime_secret_record(
                        secret_id=instance_secret_id,
                        scope="context_instance",
                        name="dokploy-ship-mode-testing",
                        version_id=instance_version_id,
                        context="sellyouroutboard-testing",
                        instance="testing",
                    ),
                ),
                secret_versions=(
                    _runtime_secret_version(
                        secret_id=global_secret_id,
                        version_id=global_version_id,
                        plaintext_value="application",
                    ),
                    _runtime_secret_version(
                        secret_id=instance_secret_id,
                        version_id=instance_version_id,
                        plaintext_value="compose",
                    ),
                ),
                secret_bindings=(
                    _runtime_secret_binding(
                        secret_id=global_secret_id,
                        binding_key="DOKPLOY_SHIP_MODE",
                    ),
                    _runtime_secret_binding(
                        secret_id=instance_secret_id,
                        binding_key="DOKPLOY_SHIP_MODE",
                        context="sellyouroutboard-testing",
                        instance="testing",
                    ),
                ),
            )
            resolved = provider.resolve_deploy_target(
                control_plane_root=Path("."),
                request_artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                request_source_git_ref="abc123",
                request_timeout_seconds=45,
                request_no_cache=True,
                record_store=store,
                profile=_profile(),
                lane=_profile().lanes[0],
                normalized_artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                fallback_target_name="fallback-target",
            )

        self.assertEqual(resolved.ship_request.deploy_mode, "dokploy-compose-api")

    def test_resolve_generic_web_profile_lane_rejects_missing_lane(self) -> None:
        store = _GenericWebDeployStore(_profile())

        with self.assertRaises(click.ClickException):
            resolve_generic_web_profile_lane(record_store=store, request=_request(instance="prod"))

        self.assertEqual(store.deployments, [])

    def test_resolve_generic_web_profile_lane_accepts_based_driver(self) -> None:
        store = _GenericWebDeployStore(_profile(driver_id="odoo"))

        profile, lane = resolve_generic_web_profile_lane(record_store=store, request=_request())

        self.assertEqual(profile.driver_id, "odoo")
        self.assertEqual(lane.instance, "testing")

    def test_resolve_generic_web_profile_lane_rejects_unbased_driver(self) -> None:
        store = _GenericWebDeployStore(_profile(driver_id="missing-driver"))

        with self.assertRaises(click.ClickException):
            resolve_generic_web_profile_lane(record_store=store, request=_request())


if __name__ == "__main__":
    unittest.main()
