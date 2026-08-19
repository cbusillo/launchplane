from collections.abc import Callable
from pathlib import Path
from typing import cast
import unittest
from unittest.mock import patch

import click

from control_plane.contracts.deploy_target import DeployedTargetReference, ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.deployment_record import DeploymentRecord, ResolvedTargetEvidence
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.promotion_record import HealthcheckEvidence, PostDeployUpdateEvidence
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
from control_plane.dokploy import api as dokploy_api
from control_plane import secrets as control_plane_secrets
from control_plane.generic_web_deploy_http import GenericWebDeployEnvelope
from control_plane.generic_web_deploy_provider_adapter import (
    GenericWebDeployProviderMutationAdapter,
)
from control_plane.workflows.generic_web_deploy import (
    GenericWebDeployRequest,
    GenericWebDeployStore,
    GenericWebPostDeployContext,
    GenericWebPostDeployGuard,
    execute_generic_web_deploy,
    normalize_generic_web_artifact_id,
    record_observed_generic_web_deploy,
    resolve_generic_web_profile_lane,
)
from control_plane.workflows.generic_web_deploy_provider import DokployGenericWebDeployProvider
from control_plane.workflows.generic_web_deploy_provider import (
    GenericWebLegacyDeploymentCorrelation,
    GenericWebLegacyDeploymentCorrelationEvidence,
    GenericWebProviderDeploymentObservation,
)
from control_plane.workflows.generic_web_deploy_provider import (
    GenericWebResolvedDeployTarget,
    build_generic_web_provider_reconciliation_key,
    correlate_legacy_dokploy_deployment,
    resolve_generic_web_provider_reconciliation_target,
)
from control_plane.workflows.ship import build_deployment_record


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
        self.deployments = [
            existing for existing in self.deployments if existing.record_id != record.record_id
        ]
        self.deployments.append(record)

    def read_deployment_record(self, record_id: str) -> DeploymentRecord:
        for record in self.deployments:
            if record.record_id == record_id:
                return record
        raise FileNotFoundError(record_id)

    def write_environment_inventory(self, record: EnvironmentInventory) -> None:
        self.inventories = [
            existing
            for existing in self.inventories
            if (existing.context, existing.instance) != (record.context, record.instance)
        ]
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

    @staticmethod
    def list_secret_audit_events(*, secret_id: str) -> tuple[SecretAuditEvent, ...]:
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


def _missing_image_profile() -> LaunchplaneProductProfileRecord:
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
    encrypt_secret_value = cast(
        Callable[[str], tuple[str, str]],
        getattr(control_plane_secrets, "_encrypt_secret_value"),
    )
    ciphertext, key_id = encrypt_secret_value(plaintext_value)
    return SecretVersion(
        version_id=version_id,
        secret_id=secret_id,
        created_at="2026-04-30T22:00:00Z",
        ciphertext=ciphertext,
        key_id=key_id,
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


def _missing_image_lane() -> ProductLaneProfile:
    return ProductLaneProfile(
        instance="prod",
        context="repairshopr-sync",
        health_monitoring=ProductLaneHealthMonitoringPolicy(checks=()),
    )


def _request(
    instance: str = "testing",
    *,
    timeout_seconds: int | None = None,
) -> GenericWebDeployRequest:
    return GenericWebDeployRequest(
        product="sellyouroutboard",
        instance=instance,
        artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
        source_git_ref="abc123",
        timeout_seconds=timeout_seconds,
    )


class _FakeGenericWebDeployProvider:
    provider_id = "fake-cloud"
    delegated_executor = "control-plane.fake-cloud"

    def __init__(
        self,
        *,
        deploy_error: click.ClickException | None = None,
        observation: GenericWebProviderDeploymentObservation | None = None,
        target_id: str = "target-123",
        target_name: str = "sellyouroutboard-testing-app",
    ) -> None:
        self.deploy_error = deploy_error
        self.observation = observation or GenericWebProviderDeploymentObservation(outcome="unknown")
        self.target_id = target_id
        self.target_name = target_name
        self.resolved_targets: list[GenericWebResolvedDeployTarget] = []
        self.runtime_identities: list[RuntimeIdentity] = []
        self.deployment_titles: list[str] = []
        self.observed_target_ids: list[str] = []

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
        request_deploy_reference: str = "",
    ) -> GenericWebResolvedDeployTarget:
        del control_plane_root, request_artifact_id, record_store, profile, fallback_target_name
        resolved = GenericWebResolvedDeployTarget(
            ship_request=ShipRequest(
                artifact_id=normalized_artifact_id,
                deploy_reference=request_deploy_reference,
                context=lane.context,
                instance=lane.instance,
                source_git_ref=request_source_git_ref,
                target_name=self.target_name,
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
                target_id=self.target_id,
                target_name=self.target_name,
            ),
            deployed_target=DeployedTargetReference(
                provider_id=self.provider_id,
                target_category="service",
                target_id=self.target_id,
                display_name=self.target_name,
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
        deployment_title: str,
        before_provider_mutation: Callable[[str], None],
        effect_started: Callable[[], None],
    ) -> None:
        del control_plane_root, resolved_deploy_target
        self.runtime_identities.append(runtime_identity)
        self.deployment_titles.append(deployment_title)
        before_provider_mutation("deploy_trigger")
        effect_started()
        if self.deploy_error is not None:
            raise self.deploy_error

    def observe_artifact_deploy(
        self,
        *,
        control_plane_root: Path,
        resolved_deploy_target: GenericWebResolvedDeployTarget,
        deployment_title: str,
    ) -> GenericWebProviderDeploymentObservation:
        del control_plane_root, deployment_title
        self.observed_target_ids.append(resolved_deploy_target.resolved_target.target_id)
        return self.observation


class _LegacyFakeGenericWebDeployProvider(_FakeGenericWebDeployProvider):
    def __init__(self) -> None:
        super().__init__(observation=GenericWebProviderDeploymentObservation(outcome="absent"))
        self.legacy_observation_calls = 0

    def observe_legacy_artifact_deploy(
        self, **_kwargs: object
    ) -> GenericWebLegacyDeploymentCorrelation:
        self.legacy_observation_calls += 1
        return GenericWebLegacyDeploymentCorrelation(
            observation=GenericWebProviderDeploymentObservation(
                outcome="present",
                deployment_status="done",
                deployment_id="deployment-legacy",
                started_at="2026-08-15T12:00:02.000000Z",
                finished_at="2026-08-15T12:00:20.000000Z",
            ),
            digest_evidence=GenericWebLegacyDeploymentCorrelationEvidence(
                deployment_id_sha256="1" * 64,
                deployment_title_sha256="2" * 64,
                deployment_created_at="2026-08-15T12:00:01.000000Z",
                deployment_started_at="2026-08-15T12:00:02.000000Z",
                deployment_finished_at="2026-08-15T12:00:20.000000Z",
                artifact_reference_sha256="3" * 64,
            ),
        )


class GenericWebDeployTests(unittest.TestCase):
    @staticmethod
    def _provider_mutation_adapter(
        *,
        profile: LaunchplaneProductProfileRecord,
        store: _GenericWebDeployStore,
        provider: _FakeGenericWebDeployProvider,
        deploy_request: GenericWebDeployRequest | None = None,
    ) -> GenericWebDeployProviderMutationAdapter:
        adapter = GenericWebDeployProviderMutationAdapter(
            control_plane_root=Path("."),
            record_store=store,
            deploy_request=GenericWebDeployEnvelope(
                product=profile.product,
                deploy=deploy_request or _request(),
            ),
            profile=profile,
            lane=profile.lanes[0],
            trace_id="generic-web-provider-test",
        )
        adapter._deploy_provider = provider
        return adapter

    def test_provider_target_key_is_stable_across_request_timeout_changes(self) -> None:
        profile = _profile()
        store = _GenericWebDeployStore(profile)
        provider = _FakeGenericWebDeployProvider()
        short_timeout = self._provider_mutation_adapter(
            profile=profile,
            store=store,
            provider=provider,
            deploy_request=_request(timeout_seconds=300),
        )
        long_timeout = self._provider_mutation_adapter(
            profile=profile,
            store=store,
            provider=provider,
            deploy_request=_request(timeout_seconds=600),
        )

        self.assertEqual(short_timeout.target_key(), long_timeout.target_key())
        self.assertNotEqual(
            short_timeout.reconciliation_key(),
            long_timeout.reconciliation_key(),
        )

    def test_provider_inspection_does_not_materialize_observed_records(self) -> None:
        profile = _profile()
        store = _GenericWebDeployStore(profile)
        provider = _FakeGenericWebDeployProvider()
        provider.observation = GenericWebProviderDeploymentObservation(
            outcome="present",
            deployment_status="success",
            deployment_id="deployment-provider-1",
            started_at="2026-08-16T15:00:00Z",
            finished_at="2026-08-16T15:01:00Z",
        )
        adapter = self._provider_mutation_adapter(
            profile=profile,
            store=store,
            provider=provider,
        )
        reconciliation_key = adapter.reconciliation_key()
        provider_target_key = adapter.target_key()

        with patch(
            "control_plane.generic_web_deploy_provider_adapter.record_observed_generic_web_deploy"
        ) as materialize:
            inspection = adapter.inspect(
                provider_operation_key="provider-operation:test-inspection",
                provider_effect_phase="deploy_trigger",
                reconciliation_key=reconciliation_key,
                expected_provider_target_key=provider_target_key,
            )

        self.assertEqual(inspection.observation.outcome, "present")
        self.assertTrue(inspection.identity_matches)
        materialize.assert_not_called()
        self.assertEqual(store.deployments, [])
        self.assertEqual(store.inventories, [])

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
                profile=_missing_image_profile(),
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
                        monitoring_intent="public",
                        checks=(ProductLaneHealthCheck(name="public-ingress"),),
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
                        monitoring_intent="public",
                        checks=(ProductLaneHealthCheck(name="public-ingress"),),
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
                        monitoring_intent="private",
                        checks=(
                            ProductLaneHealthCheck(
                                name="private-runtime",
                                kind="private_http",
                                private_endpoint_key="repairshopr-sync-prod-runtime",
                            ),
                        ),
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
                lanes=(_missing_image_lane(),),
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
                lanes=(_missing_image_lane(),),
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

    def test_execute_generic_web_deploy_uses_deploy_reference_for_runtime_image(self) -> None:
        artifact_id = (
            "ghcr.io/cbusillo/sellyouroutboard@sha256:"
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        )
        deploy_reference = "ghcr.io/cbusillo/sellyouroutboard:sha-abcdef1234567890"
        store = _GenericWebDeployStore(_profile())
        deploy_provider = _FakeGenericWebDeployProvider()

        result = execute_generic_web_deploy(
            control_plane_root=Path("."),
            record_store=store,
            request=GenericWebDeployRequest(
                product="sellyouroutboard",
                instance="testing",
                artifact_id=artifact_id,
                deploy_reference=deploy_reference,
                source_git_ref="abcdef1234567890",
            ),
            deploy_provider=deploy_provider,
        )

        self.assertEqual(result.deploy_status, "pass")
        artifact_identity = store.deployments[0].artifact_identity
        self.assertIsNotNone(artifact_identity)
        assert artifact_identity is not None
        self.assertEqual(artifact_identity.artifact_id, artifact_id)
        self.assertIsNotNone(store.deployments[0].runtime_identity)
        assert store.deployments[0].runtime_identity is not None
        self.assertEqual(store.deployments[0].runtime_identity.artifact_id, artifact_id)
        self.assertEqual(store.deployments[0].runtime_identity.image_reference, deploy_reference)
        self.assertEqual(deploy_provider.runtime_identities[0].artifact_id, artifact_id)
        self.assertEqual(deploy_provider.runtime_identities[0].image_reference, deploy_reference)

    def test_execute_generic_web_deploy_records_invalid_deploy_reference_before_effect(
        self,
    ) -> None:
        artifact_id = (
            "ghcr.io/cbusillo/sellyouroutboard@sha256:"
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        )
        store = _GenericWebDeployStore(_profile())
        deploy_provider = _FakeGenericWebDeployProvider()

        result = execute_generic_web_deploy(
            control_plane_root=Path("."),
            record_store=store,
            request=GenericWebDeployRequest(
                product="sellyouroutboard",
                instance="testing",
                artifact_id=artifact_id,
                deploy_reference="ghcr.io/cbusillo/other:sha-abcdef1234567890",
                source_git_ref="abcdef1234567890",
            ),
            deploy_provider=deploy_provider,
        )

        self.assertEqual(result.deploy_status, "fail")
        self.assertIn("deploy_reference must use the same image repository", result.error_message)
        self.assertFalse(result.provider_effect_attempted)
        self.assertEqual(deploy_provider.runtime_identities, [])
        self.assertEqual(len(store.deployments), 1)
        self.assertEqual(store.deployments[0].deploy.status, "fail")
        self.assertIsNone(store.deployments[0].runtime_identity)

    def test_execute_generic_web_deploy_records_failure_for_missing_image_profile(
        self,
    ) -> None:
        store = _GenericWebDeployStore(_missing_image_profile())
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
        effect_phases: list[str] = []
        post_deploy_titles: list[str] = []

        def post_deploy(
            _root: Path,
            _store: GenericWebDeployStore,
            context: GenericWebPostDeployContext,
            guard: GenericWebPostDeployGuard,
        ) -> PostDeployUpdateEvidence:
            contexts.append(context)
            post_deploy_titles.append(guard.deployment_title)
            guard.checkpoint_effect("post_deploy_schedule_trigger")
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
            deploy_provider=_FakeGenericWebDeployProvider(
                observation=GenericWebProviderDeploymentObservation(
                    outcome="present",
                    deployment_status="success",
                    deployment_id="deployment-provider-exact",
                    started_at="2026-07-13T12:00:00Z",
                    finished_at="2026-07-13T12:01:00Z",
                )
            ),
            provider_operation_title="Launchplane operation generic-test",
            provider_effect_checkpoint=effect_phases.append,
        )

        self.assertEqual(result.deploy_status, "pass")
        self.assertEqual(result.post_deploy_status, "pass")
        self.assertEqual(result.deploy_started_at, "2026-07-13T12:00:00Z")
        self.assertEqual(result.deploy_finished_at, "2026-07-13T12:01:00Z")
        self.assertEqual(store.deployments[0].deploy.deployment_id, "deployment-provider-exact")
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
        self.assertEqual(
            effect_phases,
            ["deploy_trigger", "post_deploy_started", "post_deploy_schedule_trigger"],
        )
        self.assertTrue(post_deploy_titles[0].startswith("Launchplane post-deploy "))

    def test_execute_generic_web_deploy_canonicalizes_padded_lane_identity(self) -> None:
        profile = _profile()
        lane = profile.lanes[0]
        padded_lane = lane.model_copy(
            update={
                "context": f"  {lane.context}  ",
                "instance": f"  {lane.instance}  ",
            }
        )
        store = _GenericWebDeployStore(profile)

        result = execute_generic_web_deploy(
            control_plane_root=Path("."),
            record_store=store,
            request=_request(),
            profile=profile,
            lane=padded_lane,
            deploy_provider=_FakeGenericWebDeployProvider(
                observation=GenericWebProviderDeploymentObservation(
                    outcome="present",
                    deployment_status="success",
                    deployment_id="deployment-canonical-lane",
                    started_at="2026-07-13T12:00:00Z",
                    finished_at="2026-07-13T12:01:00Z",
                )
            ),
            provider_operation_title="Launchplane operation canonical-lane",
        )

        self.assertEqual(result.context, lane.context)
        self.assertEqual(result.instance, lane.instance)
        self.assertEqual(store.deployments[0].context, lane.context)
        self.assertEqual(store.deployments[0].instance, lane.instance)
        assert store.deployments[0].runtime_identity is not None
        self.assertEqual(store.deployments[0].runtime_identity.context, lane.context)
        self.assertEqual(store.deployments[0].runtime_identity.instance, lane.instance)

    def test_durable_generic_web_deploy_requires_operation_title(self) -> None:
        with self.assertRaisesRegex(ValueError, "require a provider operation title"):
            execute_generic_web_deploy(
                control_plane_root=Path("."),
                record_store=_GenericWebDeployStore(_profile()),
                request=_request(),
                deploy_provider=_FakeGenericWebDeployProvider(),
                provider_effect_checkpoint=lambda _phase: None,
            )

    def test_execute_generic_web_deploy_keeps_deploy_pass_when_post_deploy_extension_fails(
        self,
    ) -> None:
        store = _GenericWebDeployStore(_profile())

        def post_deploy(
            _root: Path,
            _store: GenericWebDeployStore,
            _context: GenericWebPostDeployContext,
            _guard: GenericWebPostDeployGuard,
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
            _guard: GenericWebPostDeployGuard,
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
            _guard: GenericWebPostDeployGuard,
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
                request_deploy_reference: str = "",
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
                    request_deploy_reference,
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

        self.assertEqual(store.deployments, [])

    def test_record_observed_generic_web_deploy_repairs_missing_records(self) -> None:
        profile = _profile(driver_id="odoo")
        lane = profile.lanes[0]
        store = _GenericWebDeployStore(profile)
        provider = _FakeGenericWebDeployProvider()
        request = _request()
        resolved_target = provider.resolve_deploy_target(
            control_plane_root=Path("."),
            request_artifact_id=request.artifact_id,
            request_source_git_ref=request.source_git_ref,
            request_timeout_seconds=request.timeout_seconds,
            request_no_cache=request.no_cache,
            record_store=store,
            profile=profile,
            lane=lane,
            normalized_artifact_id=request.artifact_id,
            fallback_target_name="unused",
        )

        records, result = record_observed_generic_web_deploy(
            record_store=store,
            profile=profile,
            lane=lane,
            resolved_deploy_target=resolved_target,
            observation=GenericWebProviderDeploymentObservation(
                outcome="present",
                deployment_status="success",
                deployment_id="deployment-observed",
                started_at="2026-07-13T12:00:00Z",
                finished_at="2026-07-13T12:01:00Z",
            ),
            deployment_record_id="deployment-recovered",
            post_deploy_unobserved=True,
        )

        self.assertEqual(records, {"deployment_record_id": "deployment-recovered"})
        self.assertEqual(result["deploy_status"], "pass")
        self.assertEqual(result["post_deploy_status"], "fail")
        self.assertEqual(len(store.deployments), 1)
        self.assertEqual(store.deployments[0].record_id, "deployment-recovered")
        self.assertEqual(store.deployments[0].post_deploy_update.status, "fail")
        self.assertEqual(len(store.inventories), 1)

    def test_record_observed_generic_web_deploy_preserves_existing_post_deploy_evidence(
        self,
    ) -> None:
        profile = _profile(driver_id="odoo")
        lane = profile.lanes[0]
        store = _GenericWebDeployStore(profile)
        provider = _FakeGenericWebDeployProvider()
        request = _request()

        execute_generic_web_deploy(
            control_plane_root=Path("."),
            record_store=store,
            request=request,
            profile=profile,
            lane=lane,
            deploy_provider=provider,
            deployment_record_id="deployment-existing",
            post_deploy_executor=lambda _root, _store, _context, _guard: PostDeployUpdateEvidence(
                attempted=True,
                status="pass",
                detail="post-deploy already completed",
            ),
        )
        resolved_target = provider.resolved_targets[-1]
        padded_lane = lane.model_copy(
            update={
                "context": f"  {lane.context}  ",
                "instance": f"  {lane.instance}  ",
            }
        )

        _, result = record_observed_generic_web_deploy(
            record_store=store,
            profile=profile,
            lane=padded_lane,
            resolved_deploy_target=resolved_target,
            observation=GenericWebProviderDeploymentObservation(
                outcome="present",
                deployment_status="success",
                deployment_id="deployment-observed",
                started_at="2026-07-13T12:00:00Z",
                finished_at="2026-07-13T12:01:00Z",
            ),
            deployment_record_id="deployment-existing",
            post_deploy_unobserved=True,
        )

        self.assertEqual(result["post_deploy_status"], "pass")
        self.assertEqual(result["context"], lane.context)
        self.assertEqual(result["instance"], lane.instance)
        self.assertEqual(result["error_message"], "")
        self.assertEqual(len(store.deployments), 1)
        self.assertEqual(store.deployments[0].post_deploy_update.status, "pass")
        self.assertEqual(len(store.inventories), 1)

    def test_provider_adapter_retries_absent_target_update_but_not_trigger(self) -> None:
        profile = _profile()
        store = _GenericWebDeployStore(profile)
        provider = _FakeGenericWebDeployProvider(
            observation=GenericWebProviderDeploymentObservation(outcome="absent")
        )
        adapter = self._provider_mutation_adapter(
            profile=profile,
            store=store,
            provider=provider,
        )
        reconciliation_key = adapter.reconciliation_key()

        update_observation = adapter.observe(
            "provider-operation:test",
            "target_update",
            reconciliation_key,
        )
        trigger_observation = adapter.observe(
            "provider-operation:test",
            "deploy_trigger",
            reconciliation_key,
        )

        self.assertEqual(update_observation.outcome, "absent")
        self.assertTrue(update_observation.retry_safe)
        self.assertEqual(trigger_observation.outcome, "unknown")
        self.assertFalse(trigger_observation.retry_safe)

    def test_provider_adapter_rebinds_safe_retry_to_stored_target_snapshot(self) -> None:
        profile = _profile()
        store = _GenericWebDeployStore(profile)
        original_provider = _FakeGenericWebDeployProvider(target_id="target-original")
        original_adapter = self._provider_mutation_adapter(
            profile=profile,
            store=store,
            provider=original_provider,
        )
        original_key = original_adapter.reconciliation_key()
        replacement_provider = _FakeGenericWebDeployProvider(
            target_id="target-replacement",
            observation=GenericWebProviderDeploymentObservation(outcome="absent"),
        )
        recovered_adapter = self._provider_mutation_adapter(
            profile=profile,
            store=store,
            provider=replacement_provider,
        )
        replacement_key = recovered_adapter.reconciliation_key()

        observation = recovered_adapter.observe(
            "provider-operation:test-rebind",
            "target_update",
            original_key,
        )

        self.assertNotEqual(replacement_key, original_key)
        self.assertEqual(observation.outcome, "absent")
        self.assertTrue(observation.retry_safe)
        self.assertEqual(replacement_provider.observed_target_ids, ["target-original"])
        self.assertEqual(recovered_adapter.reconciliation_key(), original_key)

    def test_provider_adapter_repairs_failed_terminal_deployment_as_502(self) -> None:
        profile = _profile()
        store = _GenericWebDeployStore(profile)
        provider = _FakeGenericWebDeployProvider(
            observation=GenericWebProviderDeploymentObservation(
                outcome="present",
                deployment_status="failed",
                deployment_id="deployment-failed",
                started_at="2026-07-13T12:00:00Z",
                finished_at="2026-07-13T12:01:00Z",
                error_message="provider deployment failed",
            )
        )
        adapter = self._provider_mutation_adapter(
            profile=profile,
            store=store,
            provider=provider,
        )
        reconciliation_key = adapter.reconciliation_key()

        observation = adapter.observe(
            "provider-operation:test-failed",
            "deploy_trigger",
            reconciliation_key,
        )

        self.assertEqual(observation.outcome, "present")
        self.assertEqual(observation.response_status_code, 502)
        result = cast(dict[str, object], observation.response_payload["result"])
        self.assertEqual(result["deploy_status"], "fail")
        self.assertEqual(result["error_message"], "provider deployment failed")
        self.assertEqual(len(store.deployments), 1)
        self.assertEqual(store.deployments[0].deploy.status, "fail")

    def test_provider_adapter_repairs_placeholder_failure_with_observed_evidence(
        self,
    ) -> None:
        profile = _profile()
        store = _GenericWebDeployStore(profile)
        provider = _FakeGenericWebDeployProvider(
            observation=GenericWebProviderDeploymentObservation(
                outcome="present",
                deployment_status="failed",
                deployment_id="deployment-failed-exact",
                started_at="2026-07-13T12:00:00Z",
                finished_at="2026-07-13T12:01:00Z",
                error_message="provider deployment failed",
            )
        )
        adapter = self._provider_mutation_adapter(
            profile=profile,
            store=store,
            provider=provider,
        )
        provider_operation_key = "provider-operation:test-failed-repair"
        resolved_target = adapter._resolve_deploy_target()
        deployment_record_id = adapter._deployment_record_id(provider_operation_key)
        store.write_deployment_record(
            build_deployment_record(
                request=resolved_target.ship_request,
                record_id=deployment_record_id,
                deployment_id="control-plane-dokploy",
                deployment_status="fail",
                started_at="2026-07-13T11:59:00Z",
                finished_at="2026-07-13T11:59:30Z",
                resolved_target=resolved_target.resolved_target,
                deployed_target=resolved_target.deployed_target,
                delegated_executor=provider.delegated_executor,
            )
        )

        observation = adapter.observe(
            provider_operation_key,
            "deploy_trigger",
            adapter.reconciliation_key(),
        )

        self.assertEqual(observation.outcome, "present")
        self.assertEqual(observation.response_status_code, 502)
        repaired = store.read_deployment_record(deployment_record_id)
        self.assertEqual(repaired.deploy.deployment_id, "deployment-failed-exact")
        self.assertEqual(repaired.deploy.started_at, "2026-07-13T12:00:00Z")
        self.assertEqual(repaired.deploy.finished_at, "2026-07-13T12:01:00Z")

    def test_provider_adapter_keeps_post_deploy_phase_unknown_until_record_exists(
        self,
    ) -> None:
        profile = _profile(driver_id="odoo")
        store = _GenericWebDeployStore(profile)
        provider = _FakeGenericWebDeployProvider(
            observation=GenericWebProviderDeploymentObservation(
                outcome="present",
                deployment_status="success",
                deployment_id="deployment-success",
                started_at="2026-07-13T12:00:00Z",
                finished_at="2026-07-13T12:01:00Z",
            )
        )
        adapter = self._provider_mutation_adapter(
            profile=profile,
            store=store,
            provider=provider,
        )
        reconciliation_key = adapter.reconciliation_key()

        observation = adapter.observe(
            "provider-operation:test-post-deploy",
            "post_deploy_schedule_trigger",
            reconciliation_key,
        )

        self.assertEqual(observation.outcome, "unknown")
        self.assertEqual(store.deployments, [])

    def test_provider_adapter_legacy_correlation_is_adoption_only_and_marks_post_deploy(
        self,
    ) -> None:
        profile = _profile(driver_id="odoo")
        store = _GenericWebDeployStore(profile)
        provider = _LegacyFakeGenericWebDeployProvider()
        adapter = self._provider_mutation_adapter(
            profile=profile,
            store=store,
            provider=provider,
        )

        inspection = adapter.inspect(
            provider_operation_key="provider-operation:legacy-correlation",
            provider_effect_phase="deploy_trigger",
            reconciliation_key=adapter.reconciliation_key(),
            legacy_provider_effect_started_at="2026-08-15T12:00:00Z",
        )

        self.assertEqual(inspection.observation.outcome, "present")
        self.assertEqual(inspection.provider_evidence, "deployment_correlated_legacy")
        self.assertFalse(inspection.retry_safe)
        self.assertTrue(inspection.post_deploy_unobserved)
        self.assertIsNotNone(inspection.legacy_correlation_digest_evidence)
        self.assertEqual(len(provider.observed_target_ids), 1)
        self.assertEqual(provider.legacy_observation_calls, 1)

    def test_provider_adapter_observe_uses_effect_start_for_legacy_correlation(self) -> None:
        profile = _profile()
        store = _GenericWebDeployStore(profile)
        provider = _LegacyFakeGenericWebDeployProvider()
        adapter = self._provider_mutation_adapter(
            profile=profile,
            store=store,
            provider=provider,
        )

        observation = adapter.observe_with_effect_started_at(
            "provider-operation:legacy-correlation",
            "deploy_trigger",
            adapter.reconciliation_key(),
            "2026-08-15T12:00:00Z",
        )

        self.assertEqual(observation.outcome, "present")
        self.assertEqual(observation.response_status_code, 202)
        self.assertEqual(provider.legacy_observation_calls, 1)
        self.assertEqual(len(store.deployments), 1)

    def test_present_provider_observation_requires_exact_terminal_evidence(self) -> None:
        with self.assertRaisesRegex(ValueError, "deployment_id, started_at, finished_at"):
            GenericWebProviderDeploymentObservation(
                outcome="present",
                deployment_status="failed",
            )

    def test_reconciliation_target_matches_canonicalized_lane_identity(self) -> None:
        profile = _profile()
        provider = _FakeGenericWebDeployProvider()
        lane = profile.lanes[0]
        request = _request()
        resolved_target = provider.resolve_deploy_target(
            control_plane_root=Path("."),
            request_artifact_id=request.artifact_id,
            request_source_git_ref=request.source_git_ref,
            request_timeout_seconds=request.timeout_seconds,
            request_no_cache=request.no_cache,
            record_store=_GenericWebDeployStore(profile),
            profile=profile,
            lane=lane,
            normalized_artifact_id=request.artifact_id,
            fallback_target_name="unused",
        )
        padded_lane = lane.model_copy(
            update={
                "context": f"  {lane.context}  ",
                "instance": f"  {lane.instance}  ",
            }
        )

        recovered = resolve_generic_web_provider_reconciliation_target(
            reconciliation_key=build_generic_web_provider_reconciliation_key(resolved_target),
            request_artifact_id=request.artifact_id,
            request_source_git_ref=request.source_git_ref,
            request_timeout_seconds=request.timeout_seconds,
            request_no_cache=request.no_cache,
            normalized_artifact_id=request.artifact_id,
            lane=padded_lane,
        )

        self.assertEqual(recovered.ship_request.context, lane.context)
        self.assertEqual(recovered.ship_request.instance, lane.instance)

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

    def test_dokploy_provider_observes_exact_terminal_operation_marker(self) -> None:
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
        with (
            patch(
                "control_plane.workflows.generic_web_deploy_provider."
                "dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.generic_web_deploy_provider."
                "dokploy_api.deployment_for_target_by_title",
                return_value={
                    "deploymentId": "deployment-123",
                    "title": "Launchplane operation abc",
                    "status": "done",
                    "startedAt": "2026-07-13T01:00:00Z",
                    "finishedAt": "2026-07-13T01:01:00Z",
                },
            ) as observe_deployment,
        ):
            observation = provider.observe_artifact_deploy(
                control_plane_root=Path("."),
                resolved_deploy_target=resolved,
                deployment_title="Launchplane operation abc",
            )

        self.assertEqual(observation.outcome, "present")
        self.assertEqual(observation.deployment_status, "done")
        self.assertEqual(observation.deployment_id, "deployment-123")
        observe_deployment.assert_called_once_with(
            host="https://dokploy.example",
            token="token",
            target_type="application",
            target_id="target-123",
            title="Launchplane operation abc",
        )

    def test_dokploy_provider_reports_missing_operation_marker_as_absent(self) -> None:
        provider = DokployGenericWebDeployProvider()
        store = _DokployGenericWebDeployStore(_profile())
        resolved = provider.resolve_deploy_target(
            control_plane_root=Path("."),
            request_artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
            request_source_git_ref="abc123",
            request_timeout_seconds=45,
            request_no_cache=False,
            record_store=store,
            profile=_profile(),
            lane=_profile().lanes[0],
            normalized_artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
            fallback_target_name="fallback-target",
        )
        with (
            patch(
                "control_plane.workflows.generic_web_deploy_provider."
                "dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.generic_web_deploy_provider."
                "dokploy_api.deployment_for_target_by_title",
                return_value=None,
            ),
        ):
            observation = provider.observe_artifact_deploy(
                control_plane_root=Path("."),
                resolved_deploy_target=resolved,
                deployment_title="Launchplane operation missing",
            )

        self.assertEqual(observation.outcome, "absent")

    def test_legacy_dokploy_correlation_accepts_exact_application_and_compose_evidence(
        self,
    ) -> None:
        success_cases: tuple[tuple[str, dokploy_api.JsonObject, str, str], ...] = (
            (
                "application",
                {
                    "applicationId": "target-123",
                    "dockerImage": "ghcr.io/example/app:sha-abc123",
                },
                "ghcr.io/example/app:sha-abc123",
                "2026-08-15T11:59:55Z",
            ),
            (
                "compose",
                {
                    "composeId": "target-123",
                    "env": "OTHER=value\nDOCKER_IMAGE_REFERENCE=ghcr.io/example/app@sha256:abc123\n",
                },
                "ghcr.io/example/app@sha256:abc123",
                "2026-08-15T12:01:05Z",
            ),
        )
        for target_type, target_payload, expected_reference, candidate_created_at in success_cases:
            with self.subTest(target_type=target_type):
                target_field = "applicationId" if target_type == "application" else "composeId"
                correlation = correlate_legacy_dokploy_deployment(
                    deployments=[
                        {
                            "deploymentId": "deployment-old",
                            target_field: "target-123",
                            "title": "older deployment",
                            "status": "done",
                            "createdAt": "2026-08-15T11:59:54Z",
                            "startedAt": "2026-08-15T11:59:54Z",
                            "finishedAt": "2026-08-15T11:59:54.500Z",
                        },
                        {
                            "deploymentId": "deployment-candidate",
                            target_field: "target-123",
                            "title": "abc123",
                            "status": "done",
                            "createdAt": candidate_created_at,
                            "startedAt": candidate_created_at,
                            "finishedAt": "2026-08-15T12:01:06Z",
                        },
                    ],
                    target_payload=target_payload,
                    target_type=target_type,
                    target_id="target-123",
                    provider_effect_started_at="2026-08-15T12:00:00Z",
                    expected_artifact_reference=expected_reference,
                )

            self.assertEqual(correlation.observation.outcome, "present")
            self.assertEqual(correlation.observation.deployment_status, "done")
            self.assertEqual(
                correlation.digest_evidence.deployment_created_at,
                candidate_created_at.replace("Z", ".000000Z")
                if "." not in candidate_created_at
                else candidate_created_at,
            )
            self.assertNotIn(
                "deployment-candidate",
                correlation.digest_evidence.model_dump_json(),
            )
            self.assertNotIn("abc123", correlation.digest_evidence.model_dump_json())

    def test_legacy_dokploy_correlation_rejects_every_ambiguous_invariant(self) -> None:
        candidate: dokploy_api.JsonObject = {
            "deploymentId": "deployment-candidate",
            "applicationId": "target-123",
            "title": "abc123",
            "status": "done",
            "createdAt": "2026-08-15T12:00:01Z",
            "startedAt": "2026-08-15T12:00:02Z",
            "finishedAt": "2026-08-15T12:00:20Z",
        }
        older: dokploy_api.JsonObject = {
            "deploymentId": "deployment-old",
            "applicationId": "target-123",
            "title": "older",
            "status": "done",
            "createdAt": "2026-08-15T11:59:54Z",
            "startedAt": "2026-08-15T11:59:54Z",
            "finishedAt": "2026-08-15T11:59:54.500Z",
        }
        target_payload: dokploy_api.JsonObject = {
            "applicationId": "target-123",
            "dockerImage": "ghcr.io/example/app:sha-abc123",
        }

        cases: list[
            tuple[
                str,
                list[dokploy_api.JsonObject],
                dokploy_api.JsonObject,
                str,
                str,
            ]
        ] = [
            (
                "no older history",
                [dict(candidate)],
                dict(target_payload),
                "application",
                "target-123",
            ),
            (
                "no window candidate",
                [dict(older), {**candidate, "createdAt": "2026-08-15T12:01:06Z"}],
                dict(target_payload),
                "application",
                "target-123",
            ),
            (
                "multiple window candidates",
                [dict(older), dict(candidate), {**candidate, "deploymentId": "deployment-second"}],
                dict(target_payload),
                "application",
                "target-123",
            ),
            (
                "candidate not newest",
                [
                    dict(older),
                    dict(candidate),
                    {
                        **candidate,
                        "deploymentId": "deployment-newer",
                        "createdAt": "2026-08-15T12:01:06Z",
                    },
                ],
                dict(target_payload),
                "application",
                "target-123",
            ),
            (
                "launchplane title",
                [dict(older), {**candidate, "title": "Launchplane operation abcdef"}],
                dict(target_payload),
                "application",
                "target-123",
            ),
            (
                "launchplane post-deploy title",
                [dict(older), {**candidate, "title": "Launchplane post-deploy abcdef"}],
                dict(target_payload),
                "application",
                "target-123",
            ),
            (
                "non-success status",
                [dict(older), {**candidate, "status": "failed"}],
                dict(target_payload),
                "application",
                "target-123",
            ),
            (
                "missing deployment id",
                [
                    dict(older),
                    {key: value for key, value in candidate.items() if key != "deploymentId"},
                ],
                dict(target_payload),
                "application",
                "target-123",
            ),
            (
                "missing started at",
                [
                    dict(older),
                    {key: value for key, value in candidate.items() if key != "startedAt"},
                ],
                dict(target_payload),
                "application",
                "target-123",
            ),
            (
                "missing finished at",
                [
                    dict(older),
                    {key: value for key, value in candidate.items() if key != "finishedAt"},
                ],
                dict(target_payload),
                "application",
                "target-123",
            ),
            (
                "invalid retained timestamp",
                [{**older, "updatedAt": "not-a-time"}, dict(candidate)],
                dict(target_payload),
                "application",
                "target-123",
            ),
            (
                "target identity mismatch",
                [dict(older), {**candidate, "applicationId": "target-other"}],
                dict(target_payload),
                "application",
                "target-123",
            ),
            (
                "artifact mismatch",
                [dict(older), dict(candidate)],
                {**target_payload, "dockerImage": "ghcr.io/example/app:sha-other"},
                "application",
                "target-123",
            ),
            (
                "ambiguous compose artifact",
                [
                    {**older, "composeId": "target-123"},
                    {**candidate, "composeId": "target-123"},
                ],
                {
                    "composeId": "target-123",
                    "env": (
                        "DOCKER_IMAGE_REFERENCE=ghcr.io/example/app:sha-abc123\n"
                        "DOCKER_IMAGE_REFERENCE=ghcr.io/example/app:sha-other\n"
                    ),
                },
                "compose",
                "target-123",
            ),
        ]
        for name, deployments, payload, target_type, target_id in cases:
            with self.subTest(name=name), self.assertRaises(ValueError):
                correlate_legacy_dokploy_deployment(
                    deployments=deployments,
                    target_payload=payload,
                    target_type=target_type,
                    target_id=target_id,
                    provider_effect_started_at="2026-08-15T12:00:00Z",
                    expected_artifact_reference="ghcr.io/example/app:sha-abc123",
                )

    def test_dokploy_legacy_observation_uses_only_history_and_current_target_reads(self) -> None:
        provider = DokployGenericWebDeployProvider()
        resolved = GenericWebResolvedDeployTarget(
            ship_request=ShipRequest(
                artifact_id=f"ghcr.io/example/app@sha256:{'a' * 64}",
                deploy_reference="ghcr.io/example/app:sha-abc123",
                context="example-testing",
                instance="testing",
                source_git_ref="abc123",
                target_name="example",
                target_type="application",
                provider_id="dokploy",
                target_category="application",
                provider_target_type="application",
                deploy_mode="dokploy-application-api",
                provider_deploy_mode="dokploy-application-api",
                destination_health=HealthcheckEvidence(status="skipped"),
            ),
            resolved_target=ResolvedTargetEvidence(
                target_type="application",
                target_id="target-123",
                target_name="example",
            ),
            deploy_timeout_seconds=45,
        )
        with (
            patch(
                "control_plane.workflows.generic_web_deploy_provider."
                "dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.generic_web_deploy_provider."
                "dokploy_api.list_deployments_for_target",
                return_value=[
                    {
                        "deploymentId": "deployment-old",
                        "applicationId": "target-123",
                        "title": "older",
                        "status": "done",
                        "createdAt": "2026-08-15T11:59:54Z",
                        "startedAt": "2026-08-15T11:59:54Z",
                        "finishedAt": "2026-08-15T11:59:54.500Z",
                    },
                    {
                        "deploymentId": "deployment-candidate",
                        "applicationId": "target-123",
                        "title": "abc123",
                        "status": "done",
                        "createdAt": "2026-08-15T12:00:01Z",
                        "startedAt": "2026-08-15T12:00:02Z",
                        "finishedAt": "2026-08-15T12:00:20Z",
                    },
                ],
            ) as history_read,
            patch(
                "control_plane.workflows.generic_web_deploy_provider."
                "dokploy_api.fetch_dokploy_target_payload",
                return_value={
                    "applicationId": "target-123",
                    "dockerImage": "ghcr.io/example/app:sha-abc123",
                },
            ) as target_read,
            patch(
                "control_plane.workflows.generic_web_deploy_provider."
                "dokploy_api.trigger_deployment",
                side_effect=AssertionError("provider write"),
            ),
        ):
            correlation = provider.observe_legacy_artifact_deploy(
                control_plane_root=Path("."),
                resolved_deploy_target=resolved,
                provider_effect_started_at="2026-08-15T12:00:00Z",
            )

        self.assertEqual(correlation.observation.outcome, "present")
        history_read.assert_called_once_with(
            host="https://dokploy.example",
            token="token",
            target_type="application",
            target_id="target-123",
        )
        target_read.assert_called_once_with(
            host="https://dokploy.example",
            token="token",
            target_type="application",
            target_id="target-123",
        )

    def test_reconciliation_key_reconstructs_original_target_after_authority_change(
        self,
    ) -> None:
        profile = _profile()
        lane = profile.lanes[0]
        request = _request()
        store = _DokployGenericWebDeployStore(profile)
        provider = DokployGenericWebDeployProvider()
        original = provider.resolve_deploy_target(
            control_plane_root=Path("."),
            request_artifact_id=request.artifact_id,
            request_source_git_ref=request.source_git_ref,
            request_timeout_seconds=request.timeout_seconds,
            request_no_cache=request.no_cache,
            record_store=store,
            profile=profile,
            lane=lane,
            normalized_artifact_id=request.artifact_id,
            fallback_target_name="fallback-target",
        )
        reconciliation_key = build_generic_web_provider_reconciliation_key(original)
        store.provider_target = store.provider_target.model_copy(
            update={"target_id": "target-replacement", "display_name": "replacement-target"}
        )
        store.dokploy_target_id = store.dokploy_target_id.model_copy(
            update={"target_id": "target-replacement"}
        )

        current = provider.resolve_deploy_target(
            control_plane_root=Path("."),
            request_artifact_id=request.artifact_id,
            request_source_git_ref=request.source_git_ref,
            request_timeout_seconds=request.timeout_seconds,
            request_no_cache=request.no_cache,
            record_store=store,
            profile=profile,
            lane=lane,
            normalized_artifact_id=request.artifact_id,
            fallback_target_name="fallback-target",
        )
        recovered = resolve_generic_web_provider_reconciliation_target(
            reconciliation_key=reconciliation_key,
            request_artifact_id=request.artifact_id,
            request_source_git_ref=request.source_git_ref,
            request_timeout_seconds=request.timeout_seconds,
            request_no_cache=request.no_cache,
            normalized_artifact_id=request.artifact_id,
            lane=lane,
        )

        self.assertEqual(current.resolved_target.target_id, "target-replacement")
        self.assertEqual(recovered.resolved_target.target_id, "target-123")
        self.assertEqual(recovered.resolved_target.target_name, "provider-target-app")

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
