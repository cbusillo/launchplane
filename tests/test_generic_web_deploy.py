from pathlib import Path
import unittest
from unittest.mock import patch

import click

from control_plane.contracts.deploy_target import DeployedTargetReference
from control_plane.contracts.deployment_record import DeploymentRecord, ResolvedTargetEvidence
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.promotion_record import PostDeployUpdateEvidence
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
    ProductLaneProfile,
    ProductPreviewProfile,
)
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.contracts.ship_request import ShipRequest
from control_plane.dokploy import DokploySourceOfTruth, DokployTargetDefinition
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
        profile: LaunchplaneProductProfileRecord,
        lane: ProductLaneProfile,
        normalized_artifact_id: str,
        fallback_target_name: str,
    ) -> GenericWebResolvedDeployTarget:
        del control_plane_root, request_artifact_id, profile, fallback_target_name
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
            request=_request(),
            deploy_provider=FailingResolveProvider(),
        )

        self.assertEqual(result.deploy_status, "fail")
        self.assertEqual(result.error_message, "target missing")
        self.assertEqual(len(store.deployments), 1)
        self.assertEqual(store.deployments[0].delegated_executor, "control-plane.fake-cloud")
        self.assertEqual(store.deployments[0].deploy.provider_id, "fake-cloud")
        self.assertEqual(
            store.deployments[0].deploy.provider_deploy_mode,
            "fake-cloud-application-api",
        )
        self.assertEqual(store.inventories, [])

    def test_dokploy_provider_resolves_target_without_generic_web_dokploy_imports(
        self,
    ) -> None:
        provider = DokployGenericWebDeployProvider()

        with (
            patch(
                "control_plane.workflows.generic_web_deploy_provider.control_plane_dokploy.read_control_plane_dokploy_source_of_truth",
                return_value=_source_of_truth(),
            ),
            patch(
                "control_plane.workflows.generic_web_deploy_provider.control_plane_runtime_environments.resolve_runtime_environment_values",
                return_value={},
            ),
        ):
            resolved = provider.resolve_deploy_target(
                control_plane_root=Path("."),
                request_artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                request_source_git_ref="abc123",
                request_timeout_seconds=45,
                request_no_cache=True,
                profile=_profile(),
                lane=_profile().lanes[0],
                normalized_artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                fallback_target_name="fallback-target",
            )

        self.assertEqual(resolved.ship_request.provider_id, "dokploy")
        self.assertEqual(resolved.ship_request.deploy_mode, "dokploy-application-api")
        self.assertEqual(resolved.ship_request.target_category, "application")
        self.assertEqual(resolved.resolved_target.target_id, "target-123")
        self.assertEqual(resolved.deploy_timeout_seconds, 45)

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
