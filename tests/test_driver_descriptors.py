import ast
import inspect
import json
import textwrap
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal
from unittest.mock import patch

from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.authz_scope import exclusively_instance_scoped_authz_actions
from control_plane.contracts.driver_descriptor import (
    DriverActionDescriptor,
    DriverCapabilityDescriptor,
    DriverDescriptor,
)
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
    ProductLaneProfile,
    ProductPreviewProfile,
)
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.preview_summary import LaunchplanePreviewSummary
from control_plane.drivers import (
    generic_web_dispatch,
    generic_web_preview_dispatch,
    native_routes,
    registry,
)
from control_plane.drivers.generic_web_preview_dispatch import (
    GenericWebPreviewDesiredStateEnvelope,
)
from control_plane.odoo_artifact_publish_inputs_http import (
    ODOO_ARTIFACT_PUBLISH_INPUTS_ROUTE,
    OdooArtifactPublishInputsEnvelope,
)
from control_plane.odoo_app_maintenance_http import ODOO_APP_MAINTENANCE_ROUTE
from control_plane.odoo_artifact_publish_http import ODOO_ARTIFACT_PUBLISH_ROUTE
from control_plane.odoo_post_deploy_http import ODOO_POST_DEPLOY_ROUTE
from control_plane.odoo_preview_apply_http import (
    ODOO_PREVIEW_APPLY_INPUTS_ROUTE,
    ODOO_PREVIEW_APPLY_ROUTE,
)
from control_plane.odoo_prod_backup_gate_http import (
    ODOO_PROD_BACKUP_GATE_ROUTE,
    ODOO_PROD_BACKUP_VERIFICATION_ROUTE,
)
from control_plane.odoo_prod_backup_restore_http import (
    ODOO_PROD_BACKUP_RESTORE_APPLY_ROUTE,
    ODOO_PROD_BACKUP_RESTORE_PLAN_ROUTE,
)
from control_plane.odoo_prod_promotion_http import (
    ODOO_PROD_PROMOTION_INPUTS_ROUTE,
    ODOO_PROD_PROMOTION_ROUTE,
    ODOO_PROD_PROMOTION_RUN_ROUTE,
)
from control_plane.odoo_prod_rollback_http import ODOO_PROD_ROLLBACK_ROUTE
from control_plane.odoo_stable_bootstrap_http import ODOO_STABLE_BOOTSTRAP_ROUTE
from control_plane import verireel_nonprod_http, verireel_prod_http, verireel_read_http
from control_plane.drivers.registry import (
    build_driver_context_view,
    effective_driver_actions,
    list_driver_descriptors,
    read_driver_descriptor,
)
from control_plane.drivers.route_paths import INGRESS_ROUTE_APPLY_ROUTE
from control_plane.service_auth import GitHubActionsIdentity, LaunchplaneAuthzPolicy
from control_plane.storage.filesystem import FilesystemRecordStore


class _StubVerifier:
    def __init__(self, identity: GitHubActionsIdentity):
        self.identity = identity

    def verify(self, token: str) -> GitHubActionsIdentity:
        if token != "valid-token":
            raise ValueError("OIDC bearer token is required.")
        return self.identity


def _identity() -> GitHubActionsIdentity:
    return GitHubActionsIdentity(
        repository="every/verireel",
        repository_owner="every",
        workflow_ref="every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main",
        job_workflow_ref="",
        ref="refs/heads/main",
        ref_type="branch",
        event_name="pull_request",
        environment="",
        subject="repo:every/verireel:pull_request",
        sha="6b3c9d7e8f901234567890abcdef1234567890ab",
        raw_claims={
            "repository": "every/verireel",
            "workflow_ref": "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main",
        },
    )


class _PreviewStore:
    def __init__(self) -> None:
        self.preview_generation_limit: int | None = None

    def list_preview_summaries(
        self, *, context_name: str, generation_limit: int
    ) -> tuple[LaunchplanePreviewSummary, ...]:
        self.preview_generation_limit = generation_limit
        return (
            LaunchplanePreviewSummary(
                preview=PreviewRecord(
                    preview_id="preview-web-pr-7",
                    context=context_name,
                    anchor_repo="every/web",
                    anchor_pr_number=7,
                    anchor_pr_url="https://github.com/every/web/pull/7",
                    preview_label="preview",
                    canonical_url="https://pr-7.example.test",
                    state="active",
                    created_at="2026-04-30T20:00:00Z",
                    updated_at="2026-04-30T20:01:00Z",
                    eligible_at="2026-04-30T20:00:00Z",
                )
            ),
        )


class _ProfileStore(_PreviewStore):
    def __init__(self, *profiles: LaunchplaneProductProfileRecord) -> None:
        super().__init__()
        self.profiles = tuple(profiles)

    def list_product_profile_records(
        self, *, driver_id: str | None = None
    ) -> tuple[LaunchplaneProductProfileRecord, ...]:
        normalized_driver_id = (driver_id or "").strip()
        if not normalized_driver_id:
            return self.profiles
        return tuple(
            profile for profile in self.profiles if profile.driver_id == normalized_driver_id
        )


def _product_profile(*, driver_id: str = "generic-web") -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord(
        product="odoo-tenant-cm",
        display_name="CM Odoo",
        repository="cbusillo/odoo-tenant-cm",
        driver_id=driver_id,
        image=ProductImageProfile(repository="ghcr.io/cbusillo/odoo-tenant-cm"),
        runtime_port=8069,
        health_path="/web/health",
        lanes=(
            ProductLaneProfile(
                instance="testing",
                context="cm",
                base_url="https://cm-testing.example.com",
                health_url="https://cm-testing.example.com/web/health",
            ),
        ),
        preview=ProductPreviewProfile(
            enabled=True,
            context="cm",
            app_name_prefix="cm-odoo-preview",
        ),
        updated_at="2026-05-25T12:00:00Z",
        source="test",
    )


def _named_product_profile(
    *,
    product: str,
    display_name: str,
    driver_id: str,
    context: str,
    instance: str = "testing",
) -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord(
        product=product,
        display_name=display_name,
        repository=f"example/{product}",
        driver_id=driver_id,
        image=ProductImageProfile(repository=f"registry.example/{product}"),
        runtime_port=8069,
        health_path="/web/health",
        lanes=(
            ProductLaneProfile(
                instance=instance,
                context=context,
                base_url=f"https://{context}.example.test",
                health_url=f"https://{context}.example.test/web/health",
            ),
        ),
        preview=ProductPreviewProfile(
            enabled=True,
            context=context,
            app_name_prefix=f"{product}-preview",
        ),
        updated_at="2026-07-23T00:00:00Z",
        source="test",
    )


RouteMetadataExpectation = tuple[Any, type[Any], str]


class _FakeFastApiRoute:
    def __init__(
        self,
        *,
        path: str,
        methods: frozenset[str],
        endpoint: object,
    ) -> None:
        self.path = path
        self.methods = methods
        self.endpoint = endpoint


class _FakeFastApiApp:
    def __init__(self, routes: tuple[_FakeFastApiRoute, ...]) -> None:
        self.routes = routes


def _fake_native_descriptor(*actions: DriverActionDescriptor) -> DriverDescriptor:
    return DriverDescriptor(
        driver_id="fake-native",
        label="Fake native",
        product="fake-native",
        description="Test-only native descriptor driver.",
        provider_boundary="Test-only provider boundary.",
        actions=actions,
    )


def _fake_native_action(
    *,
    action_id: str = "ping",
    route_path: str = "/v1/drivers/fake-native/ping",
    method: Literal["GET", "POST"] = "POST",
    authz_action: str = "fake_native.ping",
    alternate_authz_actions: tuple[str, ...] = (),
) -> DriverActionDescriptor:
    return DriverActionDescriptor(
        action_id=action_id,
        label=action_id.replace("_", " ").title(),
        description="Test descriptor-backed native route.",
        safety="safe_write",
        scope="context",
        method=method,
        route_path=route_path,
        authz_action=authz_action,
        alternate_authz_actions=alternate_authz_actions,
    )


class DriverDescriptorRegistryTests(unittest.TestCase):
    def assert_route_metadata_matches_descriptor(
        self,
        *,
        driver_id: str,
        route_metadata_by_action: dict[str, RouteMetadataExpectation],
    ) -> None:
        descriptor = read_driver_descriptor(driver_id)
        actions = {action.action_id: action for action in descriptor.actions}

        for action_id, (
            execution_metadata,
            envelope_model,
            denial_message_fragment,
        ) in route_metadata_by_action.items():
            with self.subTest(driver_id=driver_id, action_id=action_id):
                self.assertIn(action_id, actions)
                self.assertEqual(
                    execution_metadata.route_path,
                    actions[action_id].route_path,
                )
                self.assertIs(execution_metadata.envelope_model, envelope_model)
                self.assertIn(
                    denial_message_fragment,
                    execution_metadata.denial_message,
                )

    def assert_route_paths_match_descriptor(
        self,
        *,
        driver_id: str,
        route_paths_by_action: dict[str, str],
    ) -> None:
        descriptor = read_driver_descriptor(driver_id)
        actions = {action.action_id: action for action in descriptor.actions}

        for action_id, route_path in route_paths_by_action.items():
            with self.subTest(driver_id=driver_id, action_id=action_id):
                self.assertIn(action_id, actions)
                self.assertEqual(route_path, actions[action_id].route_path)

    def test_registry_lists_product_drivers_without_provider_vocabulary(self) -> None:
        descriptors = list_driver_descriptors()

        self.assertEqual(
            [descriptor.driver_id for descriptor in descriptors],
            ["generic-web", "ingress", "odoo", "verireel"],
        )
        descriptor_json = json.dumps(
            [descriptor.model_dump(mode="json") for descriptor in descriptors], sort_keys=True
        )
        self.assertNotIn("NPMplus", descriptor_json)
        self.assertNotIn("Dokploy", descriptor_json)
        self.assertNotIn("launchplane/self-deploy", descriptor_json)

    def test_registry_descriptors_do_not_claim_runtime_product_contexts(self) -> None:
        self.assertTrue(
            all(not descriptor.context_patterns for descriptor in list_driver_descriptors())
        )

    def test_context_view_derives_one_odoo_descriptor_per_owned_profile(self) -> None:
        profiles = (
            _named_product_profile(
                product="test-odoo-primary",
                display_name="Test Odoo Primary",
                driver_id="odoo",
                context="test_primary",
            ),
            _named_product_profile(
                product="test-odoo-website",
                display_name="Test Odoo Website",
                driver_id="odoo",
                context="test_website",
            ),
            _named_product_profile(
                product="test-odoo-partner",
                display_name="Test Odoo Partner",
                driver_id="odoo",
                context="test_partner",
            ),
        )
        store = _ProfileStore(*profiles)

        for profile in profiles:
            with self.subTest(product=profile.product):
                context = profile.lanes[0].context
                view = build_driver_context_view(
                    record_store=store,
                    context_name=context,
                    instance_name="testing",
                )

                self.assertEqual(len(view.drivers), 1)
                product_driver = view.drivers[0]
                self.assertEqual(product_driver.driver_id, profile.product)
                self.assertEqual(product_driver.descriptor.product, profile.product)
                self.assertEqual(product_driver.descriptor.base_driver_id, "odoo")
                self.assertEqual(product_driver.descriptor.context_patterns, (context,))
                action_ids = [action.action_id for action in product_driver.available_actions]
                self.assertEqual(len(action_ids), len(set(action_ids)))
                self.assertIn("artifact_publish", action_ids)
                self.assertIn("stable_deploy", action_ids)

    def test_context_view_rejects_unknown_profile_driver(self) -> None:
        profile = _named_product_profile(
            product="test-unknown-product",
            display_name="Test Unknown Product",
            driver_id="missing-driver",
            context="test_unknown",
        )

        view = build_driver_context_view(
            record_store=_ProfileStore(profile),
            context_name="test_unknown",
            instance_name="testing",
        )

        self.assertEqual(view.drivers, ())

    def test_context_view_fails_closed_for_duplicate_profile_ownership(self) -> None:
        view = build_driver_context_view(
            record_store=_ProfileStore(
                _named_product_profile(
                    product="test-odoo-primary",
                    display_name="Test Odoo Primary",
                    driver_id="odoo",
                    context="test_shared",
                ),
                _named_product_profile(
                    product="test-odoo-conflict",
                    display_name="Test Odoo Conflict",
                    driver_id="missing-driver",
                    context="test_shared",
                ),
            ),
            context_name="test_shared",
            instance_name="testing",
        )

        self.assertEqual(view.drivers, ())

    def test_verireel_context_view_is_derived_from_product_profile(self) -> None:
        profile = _named_product_profile(
            product="test-video-product",
            display_name="Test Video Product",
            driver_id="verireel",
            context="test_video",
        )

        view = build_driver_context_view(
            record_store=_ProfileStore(profile),
            context_name="test_video",
            instance_name="testing",
        )

        self.assertEqual(len(view.drivers), 1)
        product_driver = view.drivers[0]
        self.assertEqual(product_driver.driver_id, profile.product)
        self.assertEqual(product_driver.descriptor.base_driver_id, "verireel")
        action_ids = {action.action_id for action in product_driver.available_actions}
        self.assertIn("preview_refresh", action_ids)
        self.assertIn("prod_rollback", action_ids)

    def test_instance_driver_view_requires_profile_owned_lane(self) -> None:
        profile = _named_product_profile(
            product="test-odoo-primary",
            display_name="Test Odoo Primary",
            driver_id="odoo",
            context="test_primary",
        )

        view = build_driver_context_view(
            record_store=_ProfileStore(profile),
            context_name="test_primary",
            instance_name="prod",
        )

        self.assertEqual(view.drivers, ())

    def test_ingress_descriptor_exposes_route_apply(self) -> None:
        descriptor = read_driver_descriptor("ingress")
        actions = {action.action_id: action for action in descriptor.actions}

        self.assertEqual(actions["route_apply"].route_path, INGRESS_ROUTE_APPLY_ROUTE)
        self.assertEqual(actions["route_apply"].authz_action, "ingress_route.apply")
        self.assertEqual(actions["route_apply"].alternate_authz_actions, ("ingress_route.plan",))
        self.assertIn("ingress_route.plan", actions["route_apply"].description)
        self.assertEqual(actions["route_apply"].safety, "mutation")

    def test_odoo_descriptor_marks_prod_rollback_as_destructive(self) -> None:
        descriptor = read_driver_descriptor("odoo")
        actions = {action.action_id: action for action in descriptor.actions}
        target_replacement_requirements = (
            "provider_target",
            "route_binding",
            "runtime_environment",
            "managed_secrets",
            "artifact",
        )

        self.assertEqual(descriptor.base_driver_id, "generic-web")
        self.assertEqual(actions["prod_backup_gate"].safety, "safe_write")
        self.assertEqual(actions["prod_promotion_run"].safety, "mutation")
        self.assertEqual(actions["prod_promotion"].safety, "mutation")
        self.assertEqual(actions["prod_rollback"].safety, "destructive")
        self.assertEqual(actions["prod_rollback"].route_path, "/v1/drivers/odoo/prod-rollback")
        self.assertNotIn("preview_refresh", actions)
        self.assertNotIn("preview_verification", actions)
        self.assertEqual(actions["stable_bootstrap"].safety, "destructive")
        self.assertEqual(
            actions["stable_bootstrap"].route_path,
            "/v1/drivers/odoo/stable-bootstrap",
        )
        self.assertEqual(
            actions["target_replacement_plan"].readiness_requirements,
            target_replacement_requirements,
        )
        self.assertEqual(
            actions["target_replacement_apply"].readiness_requirements,
            target_replacement_requirements,
        )
        setting_groups = {group.group_id: group for group in descriptor.setting_groups}
        self.assertIn("preview_domain_tls", setting_groups)
        self.assertIn(
            "preview.domain_certificate_type",
            setting_groups["preview_domain_tls"].fields,
        )

    def test_effective_odoo_actions_inherit_generic_web_preview_routes(self) -> None:
        descriptor = read_driver_descriptor("odoo")
        actions = {action.action_id: action for action in effective_driver_actions(descriptor)}

        self.assertEqual(
            actions["preview_refresh"].route_path,
            "/v1/drivers/generic-web/preview-refresh",
        )
        self.assertEqual(actions["preview_refresh"].scope, "preview")
        self.assertEqual(actions["preview_inventory"].safety, "safe_write")
        self.assertEqual(actions["preview_destroy"].safety, "destructive")
        self.assertEqual(
            actions["preview_verification"].route_path,
            "/v1/drivers/generic-web/preview-verification",
        )
        self.assertEqual(
            actions["prod_rollback_plan"].route_path,
            "/v1/drivers/generic-web/prod-rollback-plan",
        )

    def test_verireel_descriptor_exposes_preview_and_stable_capabilities(self) -> None:
        descriptor = read_driver_descriptor("verireel")
        actions = {action.action_id: action for action in descriptor.actions}

        self.assertEqual(descriptor.base_driver_id, "generic-web")
        self.assertEqual(actions["preview_inventory"].safety, "read")
        self.assertEqual(actions["preview_refresh"].scope, "preview")
        self.assertEqual(actions["preview_destroy"].safety, "destructive")
        self.assertEqual(actions["prod_rollback"].safety, "destructive")

    def test_generic_web_descriptor_is_provider_neutral_base_driver(self) -> None:
        descriptor = read_driver_descriptor("generic-web")
        capability_ids = {capability.capability_id for capability in descriptor.capabilities}

        self.assertEqual(descriptor.base_driver_id, "")
        self.assertEqual(descriptor.context_patterns, ())
        self.assertIn("image_deployable", capability_ids)
        self.assertNotIn("source_ref_deployable", capability_ids)
        self.assertNotIn("legacy_source_ref_deployable", capability_ids)
        self.assertIn("health_checked", capability_ids)
        self.assertIn("previewable", capability_ids)
        self.assertIn("preview_inventory_managed", capability_ids)
        self.assertIn("pr_feedback", capability_ids)
        capabilities = {
            capability.capability_id: capability for capability in descriptor.capabilities
        }
        self.assertNotIn("source_ref_deploy", capabilities["image_deployable"].actions)
        actions = {action.action_id: action for action in descriptor.actions}
        self.assertEqual(actions["stable_deploy"].route_path, "/v1/drivers/generic-web/deploy")
        self.assertEqual(actions["stable_deploy"].safety, "mutation")
        self.assertNotIn("source_ref_deploy", actions)
        self.assertEqual(
            actions["prod_rollback_plan"].route_path,
            "/v1/drivers/generic-web/prod-rollback-plan",
        )
        self.assertEqual(actions["prod_rollback_plan"].safety, "safe_write")
        self.assertEqual(
            actions["prod_rollback"].route_path,
            "/v1/drivers/generic-web/prod-rollback",
        )
        self.assertEqual(actions["prod_rollback"].safety, "destructive")
        self.assertEqual(
            actions["stable_verification"].route_path,
            "/v1/drivers/generic-web/stable-verification",
        )
        self.assertEqual(actions["stable_verification"].safety, "safe_write")
        self.assertFalse(actions["stable_verification"].operator_visible)
        self.assertEqual(
            actions["preview_desired_state"].route_path,
            "/v1/drivers/generic-web/preview-desired-state",
        )
        self.assertEqual(actions["preview_desired_state"].safety, "safe_write")
        self.assertEqual(
            actions["preview_refresh"].route_path,
            "/v1/drivers/generic-web/preview-refresh",
        )
        self.assertEqual(actions["preview_refresh"].safety, "mutation")
        self.assertEqual(
            actions["preview_inventory"].route_path,
            "/v1/drivers/generic-web/preview-inventory",
        )
        self.assertEqual(
            actions["preview_readiness"].route_path,
            "/v1/drivers/generic-web/preview-readiness",
        )
        self.assertEqual(actions["preview_readiness"].safety, "read")
        self.assertEqual(
            actions["preview_destroy"].route_path,
            "/v1/drivers/generic-web/preview-destroy",
        )
        self.assertEqual(actions["preview_destroy"].safety, "destructive")
        self.assertEqual(
            actions["preview_verification"].route_path,
            "/v1/drivers/generic-web/preview-verification",
        )
        self.assertEqual(actions["preview_verification"].safety, "safe_write")
        self.assertFalse(actions["preview_verification"].operator_visible)
        setting_groups = {group.group_id: group for group in descriptor.setting_groups}
        self.assertIn("preview_runtime_environment", setting_groups)
        preview_settings = setting_groups["preview_runtime_environment"]
        self.assertEqual(preview_settings.scope, "context")
        self.assertIn("LAUNCHPLANE_PREVIEW_BASE_URL", preview_settings.fields)
        self.assertIn("preview.copied_env_keys", preview_settings.fields)

    def test_generic_web_child_profile_inherits_base_preview_setting_groups(self) -> None:
        view = build_driver_context_view(
            record_store=_ProfileStore(_product_profile(driver_id="odoo")),
            context_name="cm",
        )

        product_driver = next(
            driver for driver in view.drivers if driver.driver_id == "odoo-tenant-cm"
        )
        setting_groups = {
            group.group_id: group for group in product_driver.descriptor.setting_groups
        }

        self.assertIn("preview_runtime_environment", setting_groups)
        self.assertIn(
            "LAUNCHPLANE_PREVIEW_BASE_URL",
            setting_groups["preview_runtime_environment"].fields,
        )

    def test_driver_actions_declare_route_authorization_metadata(self) -> None:
        route_actions = native_routes._driver_route_metadata_from_descriptors()

        self.assertTrue(
            all(route_metadata.authz_action for route_metadata in route_actions.values())
        )
        self.assertEqual(
            route_actions["/v1/drivers/verireel/testing-verification"].authz_action,
            "deployment.write",
        )
        self.assertEqual(
            route_actions[INGRESS_ROUTE_APPLY_ROUTE].alternate_authz_actions,
            ("ingress_route.plan",),
        )
        self.assertEqual(route_actions[INGRESS_ROUTE_APPLY_ROUTE].method, "POST")
        self.assertEqual(
            route_actions["/v1/drivers/odoo/target-replacement-apply"].scope,
            "instance",
        )
        self.assertEqual(
            route_actions["/v1/drivers/generic-web/prod-promotion-workflow"].scope,
            "instance",
        )
        self.assertFalse(
            route_actions["/v1/drivers/verireel/testing-verification"].operator_visible
        )
        self.assertEqual(
            route_actions["/v1/drivers/verireel/preview-verification"].authz_action,
            "preview_generation.write",
        )
        self.assertFalse(
            route_actions["/v1/drivers/verireel/preview-verification"].operator_visible
        )
        self.assertEqual(
            route_actions["/v1/drivers/generic-web/preview-verification"].authz_action,
            "preview_generation.write",
        )
        self.assertFalse(
            route_actions["/v1/drivers/generic-web/preview-verification"].operator_visible
        )
        self.assertEqual(
            route_actions["/v1/drivers/generic-web/stable-verification"].authz_action,
            "deployment.write",
        )
        self.assertFalse(
            route_actions["/v1/drivers/generic-web/stable-verification"].operator_visible
        )
        self.assertNotIn("/v1/drivers/odoo/preview-verification", route_actions)
        self.assertNotIn("/v1/drivers/odoo/stable-verification", route_actions)

    def test_service_accepts_descriptor_post_driver_routes(self) -> None:
        descriptor_post_route_metadata = {
            action.route_path: (descriptor.driver_id, action.action_id, action.authz_action)
            for descriptor in list_driver_descriptors()
            for action in descriptor.actions
            if action.method == "POST" and action.route_path.startswith("/v1/drivers/")
        }
        native_route_metadata = native_routes._driver_route_metadata_from_descriptors()

        self.assertTrue(descriptor_post_route_metadata)
        self.assertLessEqual(
            set(descriptor_post_route_metadata),
            native_routes._NATIVE_FASTAPI_DRIVER_ROUTE_PATHS,
        )
        native_routes._validate_native_descriptor_driver_routes()
        for route_path, (
            driver_id,
            action_id,
            authz_action,
        ) in descriptor_post_route_metadata.items():
            self.assertEqual(native_route_metadata[route_path].driver_id, driver_id)
            self.assertEqual(native_route_metadata[route_path].action_id, action_id)
            self.assertEqual(
                native_routes._descriptor_driver_route_metadata(route_path).authz_action,
                authz_action,
            )
        self.assertNotIn(
            "/v1/drivers/launchplane/self-deploy",
            native_routes._driver_route_metadata_from_descriptors(),
        )

    def test_post_descriptor_route_requires_native_fastapi_route(self) -> None:
        descriptor = DriverDescriptor(
            driver_id="fake-native",
            label="Fake native",
            product="fake-native",
            description="Test-only native descriptor driver.",
            provider_boundary="Test-only provider boundary.",
            actions=(
                DriverActionDescriptor(
                    action_id="ping",
                    label="Ping",
                    description="Test descriptor-backed native route.",
                    safety="safe_write",
                    scope="context",
                    method="POST",
                    route_path="/v1/drivers/fake-native/ping",
                    authz_action="fake_native.ping",
                ),
            ),
        )

        with patch(
            "control_plane.drivers.native_routes.list_driver_descriptors",
            return_value=(descriptor,),
        ):
            with self.assertRaisesRegex(
                ValueError,
                "POST driver descriptor routes must be implemented as native FastAPI routes",
            ):
                native_routes._validate_native_descriptor_driver_routes()

        with (
            patch(
                "control_plane.drivers.native_routes.list_driver_descriptors",
                return_value=(descriptor,),
            ),
            patch(
                "control_plane.drivers.native_routes._NATIVE_FASTAPI_DRIVER_ROUTE_PATHS",
                frozenset({"/v1/drivers/fake-native/ping"}),
            ),
        ):
            native_routes._validate_native_descriptor_driver_routes()

    def test_descriptor_driver_route_rejects_missing_authorization_action(self) -> None:
        descriptor = _fake_native_descriptor(_fake_native_action(authz_action=""))

        with patch(
            "control_plane.drivers.native_routes.list_driver_descriptors",
            return_value=(descriptor,),
        ):
            with self.assertRaisesRegex(ValueError, "must declare authz_action"):
                native_routes._driver_route_metadata_from_descriptors()

    def test_descriptor_driver_route_rejects_blank_alternate_authorization_action(
        self,
    ) -> None:
        descriptor = _fake_native_descriptor(_fake_native_action(alternate_authz_actions=("",)))

        with patch(
            "control_plane.drivers.native_routes.list_driver_descriptors",
            return_value=(descriptor,),
        ):
            with self.assertRaisesRegex(ValueError, "blank alternate_authz_action"):
                native_routes._driver_route_metadata_from_descriptors()

    def test_descriptor_driver_route_rejects_duplicate_path(self) -> None:
        descriptor = _fake_native_descriptor(
            _fake_native_action(action_id="ping"),
            _fake_native_action(action_id="pong", authz_action="fake_native.pong"),
        )

        with patch(
            "control_plane.drivers.native_routes.list_driver_descriptors",
            return_value=(descriptor,),
        ):
            with self.assertRaisesRegex(ValueError, "Duplicate driver action route path"):
                native_routes._driver_route_metadata_from_descriptors()

    def test_descriptor_driver_route_rejects_invalid_method(self) -> None:
        invalid_action = _fake_native_action().model_copy(update={"method": "PATCH"})
        descriptor = _fake_native_descriptor(invalid_action)

        with patch(
            "control_plane.drivers.native_routes.list_driver_descriptors",
            return_value=(descriptor,),
        ):
            with self.assertRaisesRegex(ValueError, "must declare method GET or POST"):
                native_routes._driver_route_metadata_from_descriptors()

    def test_descriptor_driver_route_rejects_noncanonical_path(self) -> None:
        descriptor = _fake_native_descriptor(_fake_native_action(route_path="/v1/fake-native/ping"))

        with patch(
            "control_plane.drivers.native_routes.list_driver_descriptors",
            return_value=(descriptor,),
        ):
            with self.assertRaisesRegex(ValueError, "canonical /v1/drivers/ route_path"):
                native_routes._driver_route_metadata_from_descriptors()

    def test_native_fastapi_driver_route_validation_rejects_method_drift(self) -> None:
        route_path = "/v1/drivers/fake-native/ping"
        descriptor = _fake_native_descriptor(_fake_native_action(route_path=route_path))

        def endpoint() -> None:
            return None

        with (
            patch(
                "control_plane.drivers.native_routes.list_driver_descriptors",
                return_value=(descriptor,),
            ),
            patch(
                "control_plane.drivers.native_routes._NATIVE_FASTAPI_DRIVER_ROUTE_PATHS",
                frozenset({route_path}),
            ),
        ):
            native_routes._bind_native_fastapi_driver_handler(
                route_path=route_path,
                endpoint=endpoint,
                declared_methods=frozenset({"POST"}),
            )
            app = _FakeFastApiApp(
                (
                    _FakeFastApiRoute(
                        path=route_path,
                        methods=frozenset({"GET"}),
                        endpoint=endpoint,
                    ),
                )
            )

            with self.assertRaisesRegex(
                ValueError,
                "route methods must match descriptor metadata",
            ):
                native_routes._validate_native_fastapi_driver_routes(app)

    def test_native_fastapi_driver_route_validation_rejects_duplicate_registration(
        self,
    ) -> None:
        route_path = "/v1/drivers/fake-native/ping"
        descriptor = _fake_native_descriptor(_fake_native_action(route_path=route_path))

        def endpoint() -> None:
            return None

        with (
            patch(
                "control_plane.drivers.native_routes.list_driver_descriptors",
                return_value=(descriptor,),
            ),
            patch(
                "control_plane.drivers.native_routes._NATIVE_FASTAPI_DRIVER_ROUTE_PATHS",
                frozenset({route_path}),
            ),
        ):
            native_routes._bind_native_fastapi_driver_handler(
                route_path=route_path,
                endpoint=endpoint,
                declared_methods=frozenset({"POST"}),
            )
            route = _FakeFastApiRoute(
                path=route_path,
                methods=frozenset({"POST"}),
                endpoint=endpoint,
            )

            with self.assertRaisesRegex(ValueError, "registered exactly once"):
                native_routes._validate_native_fastapi_driver_routes(
                    _FakeFastApiApp((route, route))
                )

    def test_native_fastapi_driver_route_validation_rejects_handler_authz_drift(
        self,
    ) -> None:
        route_path = "/v1/drivers/fake-native/ping"
        descriptor = _fake_native_descriptor(_fake_native_action(route_path=route_path))

        def endpoint() -> None:
            return None

        with (
            patch(
                "control_plane.drivers.native_routes.list_driver_descriptors",
                return_value=(descriptor,),
            ),
            patch(
                "control_plane.drivers.native_routes._NATIVE_FASTAPI_DRIVER_ROUTE_PATHS",
                frozenset({route_path}),
            ),
        ):
            route_metadata = native_routes._bind_native_fastapi_driver_handler(
                route_path=route_path,
                endpoint=endpoint,
                declared_methods=frozenset({"POST"}),
            )
            setattr(
                endpoint,
                native_routes._NATIVE_DRIVER_ROUTE_METADATA_ATTRIBUTE,
                replace(route_metadata, authz_action="fake_native.drift"),
            )
            app = _FakeFastApiApp(
                (
                    _FakeFastApiRoute(
                        path=route_path,
                        methods=frozenset({"POST"}),
                        endpoint=endpoint,
                    ),
                )
            )

            with self.assertRaisesRegex(
                ValueError,
                "handler authorization metadata must match descriptor metadata",
            ):
                native_routes._validate_native_fastapi_driver_routes(app)

    def test_native_fastapi_driver_routes_are_registered(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: FilesystemRecordStore(root / "state"),
                control_plane_root_path=root,
                state_dir=root / "state",
            )

        native_post_routes = native_routes._fastapi_route_paths_by_method(app, "POST")
        self.assertLessEqual(
            native_routes._NATIVE_FASTAPI_DRIVER_ROUTE_PATHS,
            native_post_routes,
        )
        native_routes._validate_native_fastapi_driver_routes(app)
        ingress_route = next(
            route
            for route in app.routes
            if getattr(route, "path", None) == INGRESS_ROUTE_APPLY_ROUTE
        )
        ingress_endpoint = getattr(ingress_route, "endpoint", None)
        self.assertEqual(
            native_routes._native_driver_route_authz_action(ingress_endpoint),
            "ingress_route.apply",
        )
        self.assertEqual(
            native_routes._native_driver_route_alternate_authz_action(ingress_endpoint),
            "ingress_route.plan",
        )

    def test_instance_scoped_native_handlers_pass_resolved_instances(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: FilesystemRecordStore(root / "state"),
                control_plane_root_path=root,
                state_dir=root / "state",
            )

        routes_by_path = {
            getattr(route, "path", ""): getattr(route, "endpoint", None) for route in app.routes
        }
        route_metadata = native_routes._driver_route_metadata_from_descriptors()
        for route_path, metadata in route_metadata.items():
            if metadata.scope != "instance":
                continue
            with self.subTest(route_path=route_path):
                endpoint = routes_by_path[route_path]
                assert endpoint is not None
                source = textwrap.dedent(inspect.getsource(endpoint))
                syntax_tree = ast.parse(source)
                authorization_calls = [
                    node
                    for node in ast.walk(syntax_tree)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "_native_driver_route_authorization_allows"
                ]
                self.assertEqual(len(authorization_calls), 1)
                keyword_names = {keyword.arg for keyword in authorization_calls[0].keywords}
                self.assertIn("instances", keyword_names)

    def test_native_fastapi_driver_route_validation_fails_closed(self) -> None:
        class _Route:
            path: str
            methods: frozenset[str]

            def __init__(self, path: str) -> None:
                self.path = path
                self.methods = frozenset({"POST"})

        class _App:
            routes: tuple[_Route, ...]

            def __init__(self, routes: tuple[_Route, ...]) -> None:
                self.routes = routes

        missing_route_path = sorted(native_routes._NATIVE_FASTAPI_DRIVER_ROUTE_PATHS)[0]
        app = _App(
            tuple(
                _Route(route_path)
                for route_path in native_routes._NATIVE_FASTAPI_DRIVER_ROUTE_PATHS
                if route_path != missing_route_path
            )
        )

        with self.assertRaisesRegex(
            ValueError,
            "Native FastAPI driver routes must be registered by the FastAPI app",
        ):
            native_routes._validate_native_fastapi_driver_routes(app)

    def test_generic_web_execution_metadata_matches_descriptors(self) -> None:
        self.assert_route_metadata_matches_descriptor(
            driver_id="generic-web",
            route_metadata_by_action={
                "stable_deploy": (
                    generic_web_dispatch._GENERIC_WEB_DEPLOY_ROUTE,
                    generic_web_dispatch.GenericWebDeployEnvelope,
                    "deploy driver",
                ),
                "prod_promotion": (
                    generic_web_dispatch._GENERIC_WEB_PROD_PROMOTION_ROUTE,
                    generic_web_dispatch.GenericWebProdPromotionEnvelope,
                    "prod promotion driver",
                ),
                "prod_promotion_workflow": (
                    generic_web_dispatch._GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ROUTE,
                    generic_web_dispatch.GenericWebPromotionWorkflowEnvelope,
                    "prod promotion workflow",
                ),
                "prod_rollback_plan": (
                    generic_web_dispatch._GENERIC_WEB_ROLLBACK_PLAN_ROUTE,
                    generic_web_dispatch.GenericWebRollbackPlanEnvelope,
                    "rollback",
                ),
                "prod_rollback": (
                    generic_web_dispatch._GENERIC_WEB_ROLLBACK_ROUTE,
                    generic_web_dispatch.GenericWebRollbackEnvelope,
                    "rollback",
                ),
                "stable_verification": (
                    generic_web_dispatch._GENERIC_WEB_STABLE_VERIFICATION_ROUTE,
                    generic_web_dispatch.GenericWebStableVerificationEnvelope,
                    "stable verification",
                ),
                "preview_desired_state": (
                    generic_web_preview_dispatch._GENERIC_WEB_PREVIEW_DESIRED_STATE_ROUTE,
                    GenericWebPreviewDesiredStateEnvelope,
                    "preview desired state",
                ),
                "preview_inventory": (
                    generic_web_preview_dispatch._GENERIC_WEB_PREVIEW_INVENTORY_ROUTE,
                    generic_web_preview_dispatch.GenericWebPreviewInventoryEnvelope,
                    "preview inventory",
                ),
                "preview_refresh": (
                    generic_web_preview_dispatch._GENERIC_WEB_PREVIEW_REFRESH_ROUTE,
                    generic_web_preview_dispatch.GenericWebPreviewRefreshEnvelope,
                    "refresh generic",
                ),
                "preview_readiness": (
                    generic_web_preview_dispatch._GENERIC_WEB_PREVIEW_READINESS_ROUTE,
                    generic_web_preview_dispatch.GenericWebPreviewReadinessEnvelope,
                    "preview readiness",
                ),
                "preview_destroy": (
                    generic_web_preview_dispatch._GENERIC_WEB_PREVIEW_DESTROY_ROUTE,
                    generic_web_preview_dispatch.GenericWebPreviewDestroyEnvelope,
                    "destroy generic",
                ),
                "preview_verification": (
                    generic_web_preview_dispatch._GENERIC_WEB_PREVIEW_VERIFICATION_ROUTE,
                    generic_web_preview_dispatch.GenericWebPreviewVerificationEnvelope,
                    "preview verification",
                ),
            },
        )

    def test_odoo_artifact_execution_metadata_matches_descriptors(self) -> None:
        odoo_descriptor = read_driver_descriptor("odoo")
        odoo_actions = {action.action_id: action for action in odoo_descriptor.actions}
        self.assertEqual(
            odoo_actions["artifact_publish_inputs"].route_path,
            ODOO_ARTIFACT_PUBLISH_INPUTS_ROUTE,
        )
        self.assertEqual(
            odoo_actions["artifact_publish_inputs"].authz_action,
            "odoo_artifact_publish_inputs.read",
        )
        self.assertEqual(
            OdooArtifactPublishInputsEnvelope.model_json_schema()["title"],
            "OdooArtifactPublishInputsEnvelope",
        )
        self.assert_route_paths_match_descriptor(
            driver_id="odoo",
            route_paths_by_action={
                "artifact_publish": ODOO_ARTIFACT_PUBLISH_ROUTE,
                "post_deploy": ODOO_POST_DEPLOY_ROUTE,
                "app_maintenance": ODOO_APP_MAINTENANCE_ROUTE,
                "stable_bootstrap": ODOO_STABLE_BOOTSTRAP_ROUTE,
            },
        )

    def test_odoo_prod_execution_metadata_matches_descriptors(self) -> None:
        self.assert_route_paths_match_descriptor(
            driver_id="odoo",
            route_paths_by_action={
                "prod_backup_gate": ODOO_PROD_BACKUP_GATE_ROUTE,
                "prod_backup_verification": ODOO_PROD_BACKUP_VERIFICATION_ROUTE,
                "prod_backup_restore_plan": ODOO_PROD_BACKUP_RESTORE_PLAN_ROUTE,
                "prod_backup_restore_apply": ODOO_PROD_BACKUP_RESTORE_APPLY_ROUTE,
                "prod_promotion_inputs": ODOO_PROD_PROMOTION_INPUTS_ROUTE,
                "prod_promotion_run": ODOO_PROD_PROMOTION_RUN_ROUTE,
                "prod_promotion": ODOO_PROD_PROMOTION_ROUTE,
                "prod_rollback": ODOO_PROD_ROLLBACK_ROUTE,
            },
        )
        odoo_actions = {
            action.action_id: action for action in read_driver_descriptor("odoo").actions
        }
        verification_action = odoo_actions["prod_backup_verification"]
        self.assertEqual(verification_action.safety, "safe_write")
        self.assertEqual(verification_action.scope, "instance")
        self.assertEqual(
            verification_action.authz_action,
            "odoo_prod_backup_verification.execute",
        )
        self.assertEqual(verification_action.writes_records, ("backup_gate",))
        self.assertIn(
            verification_action.authz_action,
            exclusively_instance_scoped_authz_actions(),
        )
        restore_action = odoo_actions["prod_backup_restore_apply"]
        self.assertEqual(restore_action.safety, "destructive")
        self.assertEqual(restore_action.scope, "instance")
        self.assertEqual(
            restore_action.authz_action,
            "odoo_prod_backup_restore_apply.execute",
        )
        self.assertIn(
            restore_action.authz_action,
            exclusively_instance_scoped_authz_actions(),
        )

    def test_odoo_preview_execution_metadata_matches_descriptors(self) -> None:
        self.assertNotIn(
            "/v1/drivers/odoo/preview-refresh",
            native_routes._driver_route_metadata_from_descriptors(),
        )
        self.assertNotIn(
            "/v1/drivers/odoo/preview-destroy",
            native_routes._driver_route_metadata_from_descriptors(),
        )
        self.assertNotIn(
            "/v1/drivers/odoo/preview-desired-state",
            native_routes._driver_route_metadata_from_descriptors(),
        )
        self.assertNotIn(
            "/v1/drivers/odoo/preview-inventory",
            native_routes._driver_route_metadata_from_descriptors(),
        )
        self.assertNotIn(
            "/v1/drivers/odoo/preview-readiness",
            native_routes._driver_route_metadata_from_descriptors(),
        )
        self.assertNotIn(
            "/v1/drivers/odoo/preview-verification",
            native_routes._driver_route_metadata_from_descriptors(),
        )
        self.assertNotIn(
            "/v1/drivers/odoo/prod-rollback-plan",
            native_routes._driver_route_metadata_from_descriptors(),
        )
        self.assertNotIn(
            "/v1/drivers/odoo/prod-rollback-plan",
            native_routes._driver_route_metadata_from_descriptors(),
        )
        self.assert_route_paths_match_descriptor(
            driver_id="odoo",
            route_paths_by_action={
                "preview_apply": ODOO_PREVIEW_APPLY_ROUTE,
                "preview_apply_inputs": ODOO_PREVIEW_APPLY_INPUTS_ROUTE,
            },
        )

    def test_verireel_prod_execution_metadata_matches_descriptors(self) -> None:
        self.assert_route_metadata_matches_descriptor(
            driver_id="verireel",
            route_metadata_by_action={
                "prod_deploy": (
                    verireel_prod_http._VERIREEL_PROD_DEPLOY_ROUTE,
                    verireel_prod_http.VeriReelProdDeployEnvelope,
                    "prod deploy driver",
                ),
                "prod_backup_gate": (
                    verireel_prod_http._VERIREEL_PROD_BACKUP_GATE_ROUTE,
                    verireel_prod_http.VeriReelProdBackupGateEnvelope,
                    "prod backup gate driver",
                ),
                "prod_promotion": (
                    verireel_prod_http._VERIREEL_PROD_PROMOTION_ROUTE,
                    verireel_prod_http.VeriReelProdPromotionEnvelope,
                    "prod promotion driver",
                ),
                "prod_rollback": (
                    verireel_prod_http._VERIREEL_PROD_ROLLBACK_ROUTE,
                    verireel_prod_http.VeriReelProdRollbackEnvelope,
                    "prod rollback driver",
                ),
            },
        )

    def test_verireel_stable_execution_metadata_matches_descriptors(self) -> None:
        self.assert_route_metadata_matches_descriptor(
            driver_id="verireel",
            route_metadata_by_action={
                "testing_deploy": (
                    verireel_nonprod_http._VERIREEL_TESTING_DEPLOY_ROUTE,
                    verireel_nonprod_http.VeriReelTestingDeployEnvelope,
                    "testing deploy driver",
                ),
                "testing_verification": (
                    verireel_read_http._VERIREEL_TESTING_VERIFICATION_ROUTE,
                    verireel_read_http.VeriReelTestingVerificationEnvelope,
                    "testing verification",
                ),
                "stable_environment": (
                    verireel_read_http._VERIREEL_STABLE_ENVIRONMENT_ROUTE,
                    verireel_read_http.VeriReelStableEnvironmentEnvelope,
                    "stable environment",
                ),
                "runtime_verification": (
                    verireel_read_http._VERIREEL_RUNTIME_VERIFICATION_ROUTE,
                    verireel_read_http.VeriReelRuntimeVerificationEnvelope,
                    "runtime verification driver",
                ),
                "app_maintenance": (
                    verireel_nonprod_http._VERIREEL_APP_MAINTENANCE_ROUTE,
                    verireel_nonprod_http.VeriReelAppMaintenanceEnvelope,
                    "app maintenance driver",
                ),
            },
        )

    def test_verireel_preview_execution_metadata_matches_descriptors(self) -> None:
        self.assert_route_metadata_matches_descriptor(
            driver_id="verireel",
            route_metadata_by_action={
                "preview_refresh": (
                    verireel_read_http._VERIREEL_PREVIEW_REFRESH_ROUTE,
                    verireel_read_http.VeriReelPreviewRefreshEnvelope,
                    "preview refresh driver",
                ),
                "preview_inventory": (
                    verireel_read_http._VERIREEL_PREVIEW_INVENTORY_ROUTE,
                    verireel_read_http.VeriReelPreviewInventoryEnvelope,
                    "preview inventory",
                ),
                "preview_destroy": (
                    verireel_read_http._VERIREEL_PREVIEW_DESTROY_ROUTE,
                    verireel_read_http.VeriReelPreviewDestroyEnvelope,
                    "preview destroy driver",
                ),
                "preview_verification": (
                    verireel_read_http._VERIREEL_PREVIEW_VERIFICATION_ROUTE,
                    verireel_read_http.VeriReelPreviewVerificationEnvelope,
                    "preview verification",
                ),
            },
        )

    def test_native_route_set_includes_generic_web_routes(self) -> None:
        self.assertIn(
            "/v1/drivers/generic-web/prod-promotion",
            native_routes._NATIVE_FASTAPI_DRIVER_ROUTE_PATHS,
        )
        self.assertIn(
            "/v1/drivers/generic-web/prod-promotion-workflow",
            native_routes._NATIVE_FASTAPI_DRIVER_ROUTE_PATHS,
        )
        self.assertIn(
            generic_web_preview_dispatch._GENERIC_WEB_PREVIEW_VERIFICATION_ROUTE.route_path,
            native_routes._NATIVE_FASTAPI_DRIVER_ROUTE_PATHS,
        )

    def test_preview_read_model_is_capability_driven_not_verireel_named(self) -> None:
        descriptor = DriverDescriptor(
            driver_id="custom-web",
            base_driver_id="generic-web",
            label="Custom web",
            product="custom-web",
            description="Custom web product extending generic-web.",
            context_patterns=("custom-web-preview",),
            provider_boundary=registry.PROVIDER_BOUNDARY_NOTE,
            capabilities=(
                DriverCapabilityDescriptor(
                    capability_id="preview_lifecycle",
                    label="Preview lifecycle",
                    description="Preview lifecycle for a custom web product.",
                    panels=("preview_inventory",),
                ),
            ),
        )

        with patch.object(registry, "_DESCRIPTORS", (registry.GENERIC_WEB_DRIVER, descriptor)):
            view = build_driver_context_view(
                record_store=_ProfileStore(
                    _named_product_profile(
                        product="custom-web",
                        display_name="Custom web",
                        driver_id="custom-web",
                        context="custom-web-preview",
                    )
                ),
                context_name="custom-web-preview",
            )

        self.assertEqual(view.drivers[0].driver_id, "custom-web")
        self.assertEqual(
            view.drivers[0].preview_summaries[0].preview.preview_id, "preview-web-pr-7"
        )
        preview_inventory_provenance = view.drivers[0].preview_inventory_provenance
        self.assertIsNotNone(preview_inventory_provenance)
        assert preview_inventory_provenance is not None
        self.assertEqual(
            preview_inventory_provenance.detail,
            "Preview identity record exists, but no generation evidence is recorded.",
        )

    def test_driver_context_view_materializes_odoo_profile_inheritance(self) -> None:
        view = build_driver_context_view(
            record_store=_ProfileStore(_product_profile(driver_id="odoo")),
            context_name="cm",
        )

        drivers = {driver.driver_id: driver for driver in view.drivers}

        self.assertEqual(tuple(drivers), ("odoo-tenant-cm",))
        self.assertEqual(drivers["odoo-tenant-cm"].descriptor.base_driver_id, "odoo")
        inherited_actions = {
            action.action_id: action for action in drivers["odoo-tenant-cm"].available_actions
        }
        self.assertIn("artifact_publish", inherited_actions)
        self.assertEqual(
            inherited_actions["stable_deploy"].route_path,
            "/v1/drivers/generic-web/deploy",
        )
        self.assertEqual(
            inherited_actions["prod_promotion"].route_path,
            "/v1/drivers/odoo/prod-promotion",
        )
        self.assertEqual(
            inherited_actions["stable_verification"].route_path,
            "/v1/drivers/generic-web/stable-verification",
        )
        self.assertEqual(
            inherited_actions["preview_refresh"].route_path,
            "/v1/drivers/generic-web/preview-refresh",
        )
        self.assertEqual(
            inherited_actions["preview_verification"].route_path,
            "/v1/drivers/generic-web/preview-verification",
        )

    def test_unknown_driver_descriptor_is_missing(self) -> None:
        with self.assertRaises(FileNotFoundError):
            read_driver_descriptor("missing")


if __name__ == "__main__":
    unittest.main()
