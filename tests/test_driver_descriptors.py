import json
import unittest
from typing import Any
from unittest.mock import patch

from control_plane import service as control_plane_service
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
from control_plane.drivers import registry
from control_plane.drivers.registry import (
    build_driver_context_view,
    effective_driver_actions,
    list_driver_descriptors,
    read_driver_descriptor,
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


RouteMetadataExpectation = tuple[Any, type[Any], str]


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

    def test_ingress_descriptor_exposes_route_apply(self) -> None:
        descriptor = read_driver_descriptor("ingress")
        actions = {action.action_id: action for action in descriptor.actions}

        self.assertEqual(actions["route_apply"].route_path, "/v1/drivers/ingress/route-apply")
        self.assertEqual(actions["route_apply"].authz_action, "ingress_route.apply")
        self.assertEqual(actions["route_apply"].alternate_authz_actions, ("ingress_route.plan",))
        self.assertIn("ingress_route.plan", actions["route_apply"].description)
        self.assertEqual(actions["route_apply"].safety, "mutation")

    def test_odoo_descriptor_marks_prod_rollback_as_destructive(self) -> None:
        descriptor = read_driver_descriptor("odoo")
        actions = {action.action_id: action for action in descriptor.actions}
        route_aliases = {
            route_alias.action_id: route_alias for route_alias in descriptor.route_aliases
        }

        self.assertEqual(descriptor.base_driver_id, "generic-web")
        self.assertEqual(actions["prod_backup_gate"].safety, "safe_write")
        self.assertEqual(actions["prod_promotion_run"].safety, "mutation")
        self.assertEqual(actions["prod_promotion"].safety, "mutation")
        self.assertEqual(actions["prod_rollback"].safety, "destructive")
        self.assertEqual(actions["prod_rollback"].route_path, "/v1/drivers/odoo/prod-rollback")
        self.assertNotIn("preview_refresh", actions)
        self.assertNotIn("preview_refresh", route_aliases)
        self.assertNotIn("preview_destroy", route_aliases)
        self.assertNotIn("preview_desired_state", route_aliases)
        self.assertNotIn("preview_inventory", route_aliases)
        self.assertNotIn("preview_readiness", route_aliases)
        self.assertNotIn("preview_verification", actions)
        self.assertNotIn("preview_verification", route_aliases)
        self.assertNotIn("stable_verification", route_aliases)
        self.assertNotIn("prod_rollback_plan", route_aliases)
        self.assertEqual(actions["stable_bootstrap"].safety, "destructive")
        self.assertEqual(
            actions["stable_bootstrap"].route_path,
            "/v1/drivers/odoo/stable-bootstrap",
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
        self.assertIn("health_checked", capability_ids)
        self.assertIn("previewable", capability_ids)
        self.assertIn("preview_inventory_managed", capability_ids)
        self.assertIn("pr_feedback", capability_ids)
        actions = {action.action_id: action for action in descriptor.actions}
        self.assertEqual(actions["stable_deploy"].route_path, "/v1/drivers/generic-web/deploy")
        self.assertEqual(actions["stable_deploy"].safety, "mutation")
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
        route_actions = control_plane_service._driver_route_metadata_from_descriptors()

        self.assertTrue(
            all(route_metadata.authz_action for route_metadata in route_actions.values())
        )
        self.assertEqual(
            route_actions["/v1/drivers/verireel/testing-verification"].authz_action,
            "deployment.write",
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
        descriptor_post_route_metadata.update(
            {
                route_alias.route_path: (
                    descriptor.driver_id,
                    route_alias.action_id,
                    route_alias.authz_action,
                )
                for descriptor in list_driver_descriptors()
                for route_alias in descriptor.route_aliases
                if route_alias.method == "POST"
                and route_alias.route_path.startswith("/v1/drivers/")
            }
        )
        service_route_metadata = control_plane_service._driver_route_metadata_from_descriptors()

        self.assertTrue(descriptor_post_route_metadata)
        self.assertLessEqual(
            set(descriptor_post_route_metadata), control_plane_service._build_write_routes()
        )
        for route_path, (
            driver_id,
            action_id,
            authz_action,
        ) in descriptor_post_route_metadata.items():
            self.assertEqual(service_route_metadata[route_path].driver_id, driver_id)
            self.assertEqual(service_route_metadata[route_path].action_id, action_id)
            self.assertEqual(
                control_plane_service._descriptor_driver_authz_action(route_path), authz_action
            )
        self.assertIn(
            "/v1/drivers/launchplane/self-deploy",
            control_plane_service._build_write_routes(),
        )

    def test_descriptor_dispatch_registration_requires_descriptor_route(self) -> None:
        descriptor = DriverDescriptor(
            driver_id="fake-dispatch",
            label="Fake dispatch",
            product="fake-dispatch",
            description="Test-only descriptor dispatch driver.",
            provider_boundary="Test-only provider boundary.",
            actions=(
                DriverActionDescriptor(
                    action_id="ping",
                    label="Ping",
                    description="Test descriptor-backed dispatch route.",
                    safety="safe_write",
                    scope="context",
                    method="POST",
                    route_path="/v1/drivers/fake-dispatch/ping",
                    authz_action="fake_dispatch.ping",
                ),
            ),
        )
        route = control_plane_service._DescriptorDriverDispatchRoute(
            execution_metadata=control_plane_service._DriverRouteExecutionMetadata(
                route_path="/v1/drivers/fake-dispatch/ping",
                envelope_model=control_plane_service.GenericWebStableVerificationEnvelope,
                denial_message="Workflow cannot execute fake dispatch.",
            ),
            context_resolver=lambda request: control_plane_service._DescriptorDriverDispatchContext(
                product=request.product,
                context=request.verification.context,
            ),
            handler=lambda _request, _resolved_context, _record_store, _root_path: (
                control_plane_service._DescriptorDriverDispatchResult(result={})
            ),
        )

        with (
            patch("control_plane.service.list_driver_descriptors", return_value=(descriptor,)),
            patch(
                "control_plane.service._required_descriptor_driver_dispatch_route_paths",
                return_value=frozenset(),
            ),
        ):
            control_plane_service._validate_descriptor_driver_dispatch_routes(
                {"/v1/drivers/fake-dispatch/ping": route}
            )

        with (
            patch("control_plane.service.list_driver_descriptors", return_value=()),
            patch(
                "control_plane.service._required_descriptor_driver_dispatch_route_paths",
                return_value=frozenset(),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "must be declared by a driver descriptor"):
                control_plane_service._validate_descriptor_driver_dispatch_routes(
                    {"/v1/drivers/fake-dispatch/ping": route}
                )

    def test_stable_verification_registered_in_descriptor_dispatch(self) -> None:
        dispatch_routes = control_plane_service._descriptor_driver_dispatch_routes()

        self.assertIn(
            control_plane_service._GENERIC_WEB_STABLE_VERIFICATION_ROUTE.route_path,
            dispatch_routes,
        )
        control_plane_service._validate_descriptor_driver_dispatch_routes(dispatch_routes)

    def test_rollback_plan_registered_in_descriptor_dispatch(self) -> None:
        dispatch_routes = control_plane_service._descriptor_driver_dispatch_routes()

        self.assertIn(
            control_plane_service._GENERIC_WEB_ROLLBACK_PLAN_ROUTE.route_path,
            dispatch_routes,
        )
        control_plane_service._validate_descriptor_driver_dispatch_routes(dispatch_routes)

    def test_preview_verification_registered_in_descriptor_dispatch(self) -> None:
        dispatch_routes = control_plane_service._descriptor_driver_dispatch_routes()

        self.assertIn(
            control_plane_service._GENERIC_WEB_PREVIEW_VERIFICATION_ROUTE.route_path,
            dispatch_routes,
        )
        control_plane_service._validate_descriptor_driver_dispatch_routes(dispatch_routes)

    def test_verireel_preview_verification_registered_in_descriptor_dispatch(
        self,
    ) -> None:
        dispatch_routes = control_plane_service._descriptor_driver_dispatch_routes()

        self.assertIn(
            control_plane_service._VERIREEL_PREVIEW_VERIFICATION_ROUTE.route_path,
            dispatch_routes,
        )
        control_plane_service._validate_descriptor_driver_dispatch_routes(dispatch_routes)

    def test_verireel_testing_verification_registered_in_descriptor_dispatch(
        self,
    ) -> None:
        dispatch_routes = control_plane_service._descriptor_driver_dispatch_routes()

        self.assertIn(
            control_plane_service._VERIREEL_TESTING_VERIFICATION_ROUTE.route_path,
            dispatch_routes,
        )
        control_plane_service._validate_descriptor_driver_dispatch_routes(dispatch_routes)

    def test_verireel_testing_deploy_registered_in_descriptor_dispatch(self) -> None:
        dispatch_routes = control_plane_service._descriptor_driver_dispatch_routes()

        self.assertIn(
            control_plane_service._VERIREEL_TESTING_DEPLOY_ROUTE.route_path,
            dispatch_routes,
        )
        control_plane_service._validate_descriptor_driver_dispatch_routes(dispatch_routes)

    def test_stable_verification_dispatch_registration_requires_descriptor_route(self) -> None:
        dispatch_routes = control_plane_service._descriptor_driver_dispatch_routes()
        descriptor_without_stable_verification = registry.GENERIC_WEB_DRIVER.model_copy(
            update={
                "actions": tuple(
                    action
                    for action in registry.GENERIC_WEB_DRIVER.actions
                    if action.route_path
                    != control_plane_service._GENERIC_WEB_STABLE_VERIFICATION_ROUTE.route_path
                )
            }
        )

        with patch.object(
            registry,
            "_DESCRIPTORS",
            (
                descriptor_without_stable_verification,
                *(
                    descriptor
                    for descriptor in registry._DESCRIPTORS
                    if descriptor.driver_id != "generic-web"
                ),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "must be declared by a driver descriptor"):
                control_plane_service._validate_descriptor_driver_dispatch_routes(dispatch_routes)

    def test_rollback_plan_dispatch_registration_requires_descriptor_route(self) -> None:
        dispatch_routes = control_plane_service._descriptor_driver_dispatch_routes()
        descriptor_without_rollback_plan = registry.GENERIC_WEB_DRIVER.model_copy(
            update={
                "actions": tuple(
                    action
                    for action in registry.GENERIC_WEB_DRIVER.actions
                    if action.route_path
                    != control_plane_service._GENERIC_WEB_ROLLBACK_PLAN_ROUTE.route_path
                )
            }
        )

        with patch.object(
            registry,
            "_DESCRIPTORS",
            (
                descriptor_without_rollback_plan,
                *(
                    descriptor
                    for descriptor in registry._DESCRIPTORS
                    if descriptor.driver_id != "generic-web"
                ),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "must be declared by a driver descriptor"):
                control_plane_service._validate_descriptor_driver_dispatch_routes(dispatch_routes)

    def test_preview_verification_dispatch_registration_requires_descriptor_route(
        self,
    ) -> None:
        dispatch_routes = control_plane_service._descriptor_driver_dispatch_routes()
        descriptor_without_preview_verification = registry.GENERIC_WEB_DRIVER.model_copy(
            update={
                "actions": tuple(
                    action
                    for action in registry.GENERIC_WEB_DRIVER.actions
                    if action.route_path
                    != control_plane_service._GENERIC_WEB_PREVIEW_VERIFICATION_ROUTE.route_path
                )
            }
        )

        with patch.object(
            registry,
            "_DESCRIPTORS",
            (
                descriptor_without_preview_verification,
                *(
                    descriptor
                    for descriptor in registry._DESCRIPTORS
                    if descriptor.driver_id != "generic-web"
                ),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "must be declared by a driver descriptor"):
                control_plane_service._validate_descriptor_driver_dispatch_routes(dispatch_routes)

    def test_verireel_preview_verification_dispatch_registration_requires_descriptor_route(
        self,
    ) -> None:
        dispatch_routes = control_plane_service._descriptor_driver_dispatch_routes()
        descriptor_without_preview_verification = registry.VERIREEL_DRIVER.model_copy(
            update={
                "actions": tuple(
                    action
                    for action in registry.VERIREEL_DRIVER.actions
                    if action.route_path
                    != control_plane_service._VERIREEL_PREVIEW_VERIFICATION_ROUTE.route_path
                )
            }
        )

        with patch.object(
            registry,
            "_DESCRIPTORS",
            (
                descriptor_without_preview_verification,
                *(
                    descriptor
                    for descriptor in registry._DESCRIPTORS
                    if descriptor.driver_id != "verireel"
                ),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "must be declared by a driver descriptor"):
                control_plane_service._validate_descriptor_driver_dispatch_routes(dispatch_routes)

    def test_verireel_testing_verification_dispatch_registration_requires_descriptor_route(
        self,
    ) -> None:
        dispatch_routes = control_plane_service._descriptor_driver_dispatch_routes()
        descriptor_without_testing_verification = registry.VERIREEL_DRIVER.model_copy(
            update={
                "actions": tuple(
                    action
                    for action in registry.VERIREEL_DRIVER.actions
                    if action.route_path
                    != control_plane_service._VERIREEL_TESTING_VERIFICATION_ROUTE.route_path
                )
            }
        )

        with patch.object(
            registry,
            "_DESCRIPTORS",
            (
                descriptor_without_testing_verification,
                *(
                    descriptor
                    for descriptor in registry._DESCRIPTORS
                    if descriptor.driver_id != "verireel"
                ),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "must be declared by a driver descriptor"):
                control_plane_service._validate_descriptor_driver_dispatch_routes(dispatch_routes)

    def test_verireel_testing_deploy_dispatch_registration_requires_descriptor_route(
        self,
    ) -> None:
        dispatch_routes = control_plane_service._descriptor_driver_dispatch_routes()
        descriptor_without_testing_deploy = registry.VERIREEL_DRIVER.model_copy(
            update={
                "actions": tuple(
                    action
                    for action in registry.VERIREEL_DRIVER.actions
                    if action.route_path
                    != control_plane_service._VERIREEL_TESTING_DEPLOY_ROUTE.route_path
                )
            }
        )

        with patch.object(
            registry,
            "_DESCRIPTORS",
            (
                descriptor_without_testing_deploy,
                *(
                    descriptor
                    for descriptor in registry._DESCRIPTORS
                    if descriptor.driver_id != "verireel"
                ),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "must be declared by a driver descriptor"):
                control_plane_service._validate_descriptor_driver_dispatch_routes(dispatch_routes)

    def test_stable_verification_descriptor_requires_dispatch_registration(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be registered by the service"):
            control_plane_service._validate_descriptor_driver_dispatch_routes({})

    def test_rollback_plan_descriptor_requires_dispatch_registration(self) -> None:
        dispatch_routes = dict(control_plane_service._descriptor_driver_dispatch_routes())
        dispatch_routes.pop(control_plane_service._GENERIC_WEB_ROLLBACK_PLAN_ROUTE.route_path)

        with self.assertRaisesRegex(ValueError, "must be registered by the service"):
            control_plane_service._validate_descriptor_driver_dispatch_routes(dispatch_routes)

    def test_preview_verification_descriptor_requires_dispatch_registration(self) -> None:
        dispatch_routes = dict(control_plane_service._descriptor_driver_dispatch_routes())
        dispatch_routes.pop(
            control_plane_service._GENERIC_WEB_PREVIEW_VERIFICATION_ROUTE.route_path
        )

        with self.assertRaisesRegex(ValueError, "must be registered by the service"):
            control_plane_service._validate_descriptor_driver_dispatch_routes(dispatch_routes)

    def test_verireel_preview_verification_descriptor_requires_dispatch_registration(
        self,
    ) -> None:
        dispatch_routes = dict(control_plane_service._descriptor_driver_dispatch_routes())
        dispatch_routes.pop(control_plane_service._VERIREEL_PREVIEW_VERIFICATION_ROUTE.route_path)

        with self.assertRaisesRegex(ValueError, "must be registered by the service"):
            control_plane_service._validate_descriptor_driver_dispatch_routes(dispatch_routes)

    def test_verireel_testing_verification_descriptor_requires_dispatch_registration(
        self,
    ) -> None:
        dispatch_routes = dict(control_plane_service._descriptor_driver_dispatch_routes())
        dispatch_routes.pop(control_plane_service._VERIREEL_TESTING_VERIFICATION_ROUTE.route_path)

        with self.assertRaisesRegex(ValueError, "must be registered by the service"):
            control_plane_service._validate_descriptor_driver_dispatch_routes(dispatch_routes)

    def test_verireel_testing_deploy_descriptor_requires_dispatch_registration(
        self,
    ) -> None:
        dispatch_routes = dict(control_plane_service._descriptor_driver_dispatch_routes())
        dispatch_routes.pop(control_plane_service._VERIREEL_TESTING_DEPLOY_ROUTE.route_path)

        with self.assertRaisesRegex(ValueError, "must be registered by the service"):
            control_plane_service._validate_descriptor_driver_dispatch_routes(dispatch_routes)

    def test_generic_web_execution_metadata_matches_descriptors(self) -> None:
        self.assert_route_metadata_matches_descriptor(
            driver_id="generic-web",
            route_metadata_by_action={
                "stable_deploy": (
                    control_plane_service._GENERIC_WEB_DEPLOY_ROUTE,
                    control_plane_service.GenericWebDeployEnvelope,
                    "deploy driver",
                ),
                "prod_promotion": (
                    control_plane_service._GENERIC_WEB_PROD_PROMOTION_ROUTE,
                    control_plane_service.GenericWebProdPromotionEnvelope,
                    "prod promotion driver",
                ),
                "prod_promotion_workflow": (
                    control_plane_service._GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ROUTE,
                    control_plane_service.GenericWebPromotionWorkflowEnvelope,
                    "prod promotion workflow",
                ),
                "prod_rollback_plan": (
                    control_plane_service._GENERIC_WEB_ROLLBACK_PLAN_ROUTE,
                    control_plane_service.GenericWebRollbackPlanEnvelope,
                    "rollback",
                ),
                "prod_rollback": (
                    control_plane_service._GENERIC_WEB_ROLLBACK_ROUTE,
                    control_plane_service.GenericWebRollbackEnvelope,
                    "rollback",
                ),
                "stable_verification": (
                    control_plane_service._GENERIC_WEB_STABLE_VERIFICATION_ROUTE,
                    control_plane_service.GenericWebStableVerificationEnvelope,
                    "stable verification",
                ),
                "preview_desired_state": (
                    control_plane_service._GENERIC_WEB_PREVIEW_DESIRED_STATE_ROUTE,
                    control_plane_service.GenericWebPreviewDesiredStateEnvelope,
                    "preview desired state",
                ),
                "preview_inventory": (
                    control_plane_service._GENERIC_WEB_PREVIEW_INVENTORY_ROUTE,
                    control_plane_service.GenericWebPreviewInventoryEnvelope,
                    "preview inventory",
                ),
                "preview_refresh": (
                    control_plane_service._GENERIC_WEB_PREVIEW_REFRESH_ROUTE,
                    control_plane_service.GenericWebPreviewRefreshEnvelope,
                    "refresh generic",
                ),
                "preview_readiness": (
                    control_plane_service._GENERIC_WEB_PREVIEW_READINESS_ROUTE,
                    control_plane_service.GenericWebPreviewReadinessEnvelope,
                    "preview readiness",
                ),
                "preview_destroy": (
                    control_plane_service._GENERIC_WEB_PREVIEW_DESTROY_ROUTE,
                    control_plane_service.GenericWebPreviewDestroyEnvelope,
                    "destroy generic",
                ),
                "preview_verification": (
                    control_plane_service._GENERIC_WEB_PREVIEW_VERIFICATION_ROUTE,
                    control_plane_service.GenericWebPreviewVerificationEnvelope,
                    "preview verification",
                ),
            },
        )

    def test_odoo_artifact_execution_metadata_matches_descriptors(self) -> None:
        self.assert_route_metadata_matches_descriptor(
            driver_id="odoo",
            route_metadata_by_action={
                "artifact_publish_inputs": (
                    control_plane_service._ODOO_ARTIFACT_PUBLISH_INPUTS_ROUTE,
                    control_plane_service.OdooArtifactPublishInputsEnvelope,
                    "artifact publish inputs",
                ),
                "artifact_publish": (
                    control_plane_service._ODOO_ARTIFACT_PUBLISH_ROUTE,
                    control_plane_service.OdooArtifactPublishEnvelope,
                    "artifact publish evidence",
                ),
                "post_deploy": (
                    control_plane_service._ODOO_POST_DEPLOY_ROUTE,
                    control_plane_service.OdooPostDeployEnvelope,
                    "post-deploy driver",
                ),
                "stable_bootstrap": (
                    control_plane_service._ODOO_STABLE_BOOTSTRAP_ROUTE,
                    control_plane_service.OdooStableBootstrapEnvelope,
                    "stable bootstrap",
                ),
            },
        )

    def test_odoo_prod_execution_metadata_matches_descriptors(self) -> None:
        self.assert_route_metadata_matches_descriptor(
            driver_id="odoo",
            route_metadata_by_action={
                "prod_backup_gate": (
                    control_plane_service._ODOO_PROD_BACKUP_GATE_ROUTE,
                    control_plane_service.OdooProdBackupGateEnvelope,
                    "prod backup-gate driver",
                ),
                "prod_promotion_inputs": (
                    control_plane_service._ODOO_PROD_PROMOTION_INPUTS_ROUTE,
                    control_plane_service.OdooProdPromotionInputsEnvelope,
                    "prod promotion inputs",
                ),
                "prod_promotion_run": (
                    control_plane_service._ODOO_PROD_PROMOTION_RUN_ROUTE,
                    control_plane_service.OdooProdPromotionRunEnvelope,
                    "prod promotion run",
                ),
                "prod_promotion": (
                    control_plane_service._ODOO_PROD_PROMOTION_ROUTE,
                    control_plane_service.OdooProdPromotionEnvelope,
                    "prod promotion driver",
                ),
                "prod_rollback": (
                    control_plane_service._ODOO_PROD_ROLLBACK_ROUTE,
                    control_plane_service.OdooProdRollbackEnvelope,
                    "prod rollback driver",
                ),
            },
        )

    def test_odoo_preview_execution_metadata_matches_descriptors(self) -> None:
        self.assertNotIn(
            "/v1/drivers/odoo/preview-refresh",
            control_plane_service._driver_route_metadata_from_descriptors(),
        )
        self.assertNotIn(
            "/v1/drivers/odoo/preview-destroy",
            control_plane_service._driver_route_metadata_from_descriptors(),
        )
        self.assertNotIn(
            "/v1/drivers/odoo/preview-desired-state",
            control_plane_service._driver_route_metadata_from_descriptors(),
        )
        self.assertNotIn(
            "/v1/drivers/odoo/preview-inventory",
            control_plane_service._driver_route_metadata_from_descriptors(),
        )
        self.assertNotIn(
            "/v1/drivers/odoo/preview-readiness",
            control_plane_service._driver_route_metadata_from_descriptors(),
        )
        self.assertNotIn(
            "/v1/drivers/odoo/preview-verification",
            control_plane_service._driver_route_metadata_from_descriptors(),
        )
        self.assertNotIn(
            "/v1/drivers/odoo/prod-rollback-plan",
            control_plane_service._driver_route_metadata_from_descriptors(),
        )
        self.assertNotIn(
            "/v1/drivers/odoo/prod-rollback-plan",
            control_plane_service._build_write_routes(),
        )
        self.assert_route_metadata_matches_descriptor(
            driver_id="odoo",
            route_metadata_by_action={
                "preview_apply": (
                    control_plane_service._ODOO_PREVIEW_APPLY_ROUTE,
                    control_plane_service.OdooPreviewApplyEnvelope,
                    "apply Odoo preview",
                ),
                "preview_apply_inputs": (
                    control_plane_service._ODOO_PREVIEW_APPLY_INPUTS_ROUTE,
                    control_plane_service.OdooPreviewApplyInputsEnvelope,
                    "preview apply inputs",
                ),
            },
        )

    def test_verireel_prod_execution_metadata_matches_descriptors(self) -> None:
        self.assert_route_metadata_matches_descriptor(
            driver_id="verireel",
            route_metadata_by_action={
                "prod_deploy": (
                    control_plane_service._VERIREEL_PROD_DEPLOY_ROUTE,
                    control_plane_service.VeriReelProdDeployEnvelope,
                    "prod deploy driver",
                ),
                "prod_backup_gate": (
                    control_plane_service._VERIREEL_PROD_BACKUP_GATE_ROUTE,
                    control_plane_service.VeriReelProdBackupGateEnvelope,
                    "prod backup gate driver",
                ),
                "prod_promotion": (
                    control_plane_service._VERIREEL_PROD_PROMOTION_ROUTE,
                    control_plane_service.VeriReelProdPromotionEnvelope,
                    "prod promotion driver",
                ),
                "prod_rollback": (
                    control_plane_service._VERIREEL_PROD_ROLLBACK_ROUTE,
                    control_plane_service.VeriReelProdRollbackEnvelope,
                    "prod rollback driver",
                ),
            },
        )

    def test_verireel_stable_execution_metadata_matches_descriptors(self) -> None:
        self.assert_route_metadata_matches_descriptor(
            driver_id="verireel",
            route_metadata_by_action={
                "testing_deploy": (
                    control_plane_service._VERIREEL_TESTING_DEPLOY_ROUTE,
                    control_plane_service.VeriReelTestingDeployEnvelope,
                    "testing deploy driver",
                ),
                "testing_verification": (
                    control_plane_service._VERIREEL_TESTING_VERIFICATION_ROUTE,
                    control_plane_service.VeriReelTestingVerificationEnvelope,
                    "testing verification",
                ),
                "stable_environment": (
                    control_plane_service._VERIREEL_STABLE_ENVIRONMENT_ROUTE,
                    control_plane_service.VeriReelStableEnvironmentEnvelope,
                    "stable environment",
                ),
                "runtime_verification": (
                    control_plane_service._VERIREEL_RUNTIME_VERIFICATION_ROUTE,
                    control_plane_service.VeriReelRuntimeVerificationEnvelope,
                    "runtime verification driver",
                ),
                "app_maintenance": (
                    control_plane_service._VERIREEL_APP_MAINTENANCE_ROUTE,
                    control_plane_service.VeriReelAppMaintenanceEnvelope,
                    "app maintenance driver",
                ),
            },
        )

    def test_verireel_preview_execution_metadata_matches_descriptors(self) -> None:
        self.assert_route_metadata_matches_descriptor(
            driver_id="verireel",
            route_metadata_by_action={
                "preview_refresh": (
                    control_plane_service._VERIREEL_PREVIEW_REFRESH_ROUTE,
                    control_plane_service.VeriReelPreviewRefreshEnvelope,
                    "preview refresh driver",
                ),
                "preview_inventory": (
                    control_plane_service._VERIREEL_PREVIEW_INVENTORY_ROUTE,
                    control_plane_service.VeriReelPreviewInventoryEnvelope,
                    "preview inventory",
                ),
                "preview_destroy": (
                    control_plane_service._VERIREEL_PREVIEW_DESTROY_ROUTE,
                    control_plane_service.VeriReelPreviewDestroyEnvelope,
                    "preview destroy driver",
                ),
                "preview_verification": (
                    control_plane_service._VERIREEL_PREVIEW_VERIFICATION_ROUTE,
                    control_plane_service.VeriReelPreviewVerificationEnvelope,
                    "preview verification",
                ),
            },
        )

    def test_route_policy_sets_use_execution_metadata(self) -> None:
        self.assertEqual(
            control_plane_service._HUMAN_IDENTITY_MUTATION_ROUTES,
            frozenset(
                {
                    control_plane_service._GENERIC_WEB_PROD_PROMOTION_ROUTE.route_path,
                    control_plane_service._GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ROUTE.route_path,
                    control_plane_service._NPMPLUS_INGRESS_APPLY_ROUTE.route_path,
                    "/v1/agent/write-intents/evaluate",
                    "/v1/product-config/apply",
                    "/v1/authz-policies/github-actions/grants",
                    "/v1/authz-policies/github-actions/removals",
                    "/v1/authz-policies/github-humans/grants",
                    "/v1/authz-policies/terminal-agents/grants",
                    "/v1/authz-policies/local-operators/grants",
                    "/v1/authz-policies/local-admins/grants",
                    "/v1/merge-train/policies/import",
                }
            ),
        )
        self.assertIn(
            "/v1/drivers/generic-web/prod-promotion",
            control_plane_service._HUMAN_IDENTITY_MUTATION_ROUTES,
        )
        self.assertEqual(
            control_plane_service._NON_IDEMPOTENT_DRIVER_RESULT_ROUTES,
            frozenset(
                {
                    control_plane_service._ODOO_STABLE_BOOTSTRAP_ROUTE.route_path,
                    control_plane_service._ODOO_TARGET_REPLACEMENT_APPLY_ROUTE.route_path,
                    control_plane_service._VERIREEL_STABLE_ENVIRONMENT_ROUTE.route_path,
                    control_plane_service._VERIREEL_RUNTIME_VERIFICATION_ROUTE.route_path,
                    control_plane_service._VERIREEL_PREVIEW_INVENTORY_ROUTE.route_path,
                }
            ),
        )
        self.assertIn(
            "/v1/drivers/verireel/preview-inventory",
            control_plane_service._NON_IDEMPOTENT_DRIVER_RESULT_ROUTES,
        )
        self.assertEqual(
            control_plane_service._PENDING_RESULT_IDEMPOTENCY_SKIP_ROUTES,
            frozenset({control_plane_service._VERIREEL_PROD_BACKUP_GATE_ROUTE.route_path}),
        )
        self.assertIn(
            "/v1/drivers/verireel/prod-backup-gate",
            control_plane_service._PENDING_RESULT_IDEMPOTENCY_SKIP_ROUTES,
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
                record_store=_PreviewStore(),
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

    def test_driver_context_view_includes_generic_web_base_for_child_profile(self) -> None:
        view = build_driver_context_view(
            record_store=_ProfileStore(_product_profile(driver_id="odoo")),
            context_name="cm",
        )

        drivers = {driver.driver_id: driver for driver in view.drivers}

        self.assertIn("odoo", drivers)
        self.assertIn("odoo-tenant-cm", drivers)
        self.assertEqual(drivers["odoo-tenant-cm"].descriptor.base_driver_id, "generic-web")
        inherited_actions = {
            action.action_id: action for action in drivers["odoo-tenant-cm"].available_actions
        }
        self.assertEqual(
            inherited_actions["stable_deploy"].route_path,
            "/v1/drivers/generic-web/deploy",
        )
        self.assertEqual(
            inherited_actions["prod_promotion"].route_path,
            "/v1/drivers/generic-web/prod-promotion",
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
