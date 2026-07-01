import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from control_plane import service as control_plane_service
from control_plane.http_app import create_launchplane_fastapi_app
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
from control_plane.drivers.generic_web_preview_dispatch import (
    GenericWebPreviewDesiredStateEnvelope,
)
from control_plane.odoo_artifact_publish_inputs_http import (
    ODOO_ARTIFACT_PUBLISH_INPUTS_ACTION,
    ODOO_ARTIFACT_PUBLISH_INPUTS_ROUTE,
    OdooArtifactPublishInputsEnvelope,
)
from control_plane.odoo_preview_apply_http import (
    OdooPreviewApplyEnvelope,
    OdooPreviewApplyInputsEnvelope,
)
from control_plane.drivers.registry import (
    build_driver_context_view,
    effective_driver_actions,
    list_driver_descriptors,
    read_driver_descriptor,
)
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
        self.assertIn("source_ref_deployable", capability_ids)
        self.assertNotIn("legacy_source_ref_deployable", capability_ids)
        self.assertIn("health_checked", capability_ids)
        self.assertIn("previewable", capability_ids)
        self.assertIn("preview_inventory_managed", capability_ids)
        self.assertIn("pr_feedback", capability_ids)
        capabilities = {
            capability.capability_id: capability for capability in descriptor.capabilities
        }
        self.assertNotIn("source_ref_deploy", capabilities["image_deployable"].actions)
        self.assertEqual(
            capabilities["source_ref_deployable"].actions,
            ("source_ref_deploy",),
        )
        actions = {action.action_id: action for action in descriptor.actions}
        self.assertEqual(actions["stable_deploy"].route_path, "/v1/drivers/generic-web/deploy")
        self.assertEqual(actions["stable_deploy"].safety, "mutation")
        self.assertEqual(
            actions["source_ref_deploy"].route_path,
            "/v1/drivers/generic-web/source-ref-deploy",
        )
        self.assertEqual(actions["source_ref_deploy"].safety, "mutation")
        self.assertEqual(
            actions["source_ref_deploy"].authz_action,
            "generic_web_source_ref_deploy.execute",
        )
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
        service_route_metadata = control_plane_service._driver_route_metadata_from_descriptors()

        self.assertTrue(descriptor_post_route_metadata)
        self.assertLessEqual(
            set(descriptor_post_route_metadata),
            control_plane_service._NATIVE_FASTAPI_DRIVER_ROUTE_PATHS,
        )
        control_plane_service._validate_native_descriptor_driver_routes()
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
        self.assertNotIn(
            "/v1/drivers/launchplane/self-deploy",
            control_plane_service._driver_route_metadata_from_descriptors(),
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

        with patch("control_plane.service.list_driver_descriptors", return_value=(descriptor,)):
            with self.assertRaisesRegex(
                ValueError,
                "POST driver descriptor routes must be implemented as native FastAPI routes",
            ):
                control_plane_service._validate_native_descriptor_driver_routes()

        with (
            patch("control_plane.service.list_driver_descriptors", return_value=(descriptor,)),
            patch(
                "control_plane.service._NATIVE_FASTAPI_DRIVER_ROUTE_PATHS",
                frozenset({"/v1/drivers/fake-native/ping"}),
            ),
        ):
            control_plane_service._validate_native_descriptor_driver_routes()

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

        native_post_routes = control_plane_service._fastapi_route_paths_by_method(app, "POST")
        self.assertLessEqual(
            control_plane_service._NATIVE_FASTAPI_DRIVER_ROUTE_PATHS,
            native_post_routes,
        )
        control_plane_service._validate_native_fastapi_driver_route_paths(app)

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

        missing_route_path = sorted(control_plane_service._NATIVE_FASTAPI_DRIVER_ROUTE_PATHS)[0]
        app = _App(
            tuple(
                _Route(route_path)
                for route_path in control_plane_service._NATIVE_FASTAPI_DRIVER_ROUTE_PATHS
                if route_path != missing_route_path
            )
        )

        with self.assertRaisesRegex(
            ValueError,
            "Native FastAPI driver routes must be registered by the FastAPI app",
        ):
            control_plane_service._validate_native_fastapi_driver_route_paths(app)

    def test_generic_web_execution_metadata_matches_descriptors(self) -> None:
        self.assert_route_metadata_matches_descriptor(
            driver_id="generic-web",
            route_metadata_by_action={
                "stable_deploy": (
                    control_plane_service._GENERIC_WEB_DEPLOY_ROUTE,
                    control_plane_service.GenericWebDeployEnvelope,
                    "deploy driver",
                ),
                "source_ref_deploy": (
                    control_plane_service._GENERIC_WEB_SOURCE_REF_DEPLOY_ROUTE,
                    control_plane_service.GenericWebSourceRefDeployEnvelope,
                    "source-ref deploy driver",
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
                    GenericWebPreviewDesiredStateEnvelope,
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
        odoo_descriptor = read_driver_descriptor("odoo")
        odoo_actions = {action.action_id: action for action in odoo_descriptor.actions}
        self.assertEqual(
            odoo_actions["artifact_publish_inputs"].route_path,
            ODOO_ARTIFACT_PUBLISH_INPUTS_ROUTE,
        )
        self.assertEqual(
            odoo_actions["artifact_publish_inputs"].authz_action,
            ODOO_ARTIFACT_PUBLISH_INPUTS_ACTION,
        )
        self.assertEqual(
            OdooArtifactPublishInputsEnvelope.model_json_schema()["title"],
            "OdooArtifactPublishInputsEnvelope",
        )
        self.assert_route_metadata_matches_descriptor(
            driver_id="odoo",
            route_metadata_by_action={
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
                    control_plane_service._ODOO_PROD_BACKUP_GATE_METADATA,
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
                    control_plane_service._ODOO_PROD_ROLLBACK_METADATA,
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
            control_plane_service._driver_route_metadata_from_descriptors(),
        )
        self.assert_route_metadata_matches_descriptor(
            driver_id="odoo",
            route_metadata_by_action={
                "preview_apply": (
                    control_plane_service._ODOO_PREVIEW_APPLY_ROUTE,
                    OdooPreviewApplyEnvelope,
                    "apply Odoo preview",
                ),
                "preview_apply_inputs": (
                    control_plane_service._ODOO_PREVIEW_APPLY_INPUTS_ROUTE,
                    OdooPreviewApplyInputsEnvelope,
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
        self.assertIn(
            "/v1/drivers/generic-web/prod-promotion",
            control_plane_service._NATIVE_FASTAPI_DRIVER_ROUTE_PATHS,
        )
        self.assertIn(
            "/v1/drivers/generic-web/prod-promotion-workflow",
            control_plane_service._NATIVE_FASTAPI_DRIVER_ROUTE_PATHS,
        )
        self.assertIn(
            control_plane_service._GENERIC_WEB_PREVIEW_VERIFICATION_ROUTE.route_path,
            control_plane_service._GENERIC_WEB_BASE_DRIVER_ROUTE_PATHS,
        )
        self.assertNotIn(
            control_plane_service._ODOO_PREVIEW_APPLY_INPUTS_ROUTE.route_path,
            control_plane_service._GENERIC_WEB_BASE_DRIVER_ROUTE_PATHS,
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
