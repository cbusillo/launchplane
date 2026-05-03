import json
import unittest
from unittest.mock import patch

from control_plane import service as control_plane_service
from control_plane.contracts.driver_descriptor import (
    DriverCapabilityDescriptor,
    DriverDescriptor,
)
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.preview_summary import LaunchplanePreviewSummary
from control_plane.drivers import registry
from control_plane.drivers.registry import (
    build_driver_context_view,
    list_driver_descriptors,
    read_driver_descriptor,
)


class _PreviewStore:
    def list_preview_summaries(
        self, *, context_name: str, generation_limit: int
    ) -> tuple[LaunchplanePreviewSummary, ...]:
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


class DriverDescriptorRegistryTests(unittest.TestCase):
    def test_registry_lists_product_drivers_without_provider_vocabulary(self) -> None:
        descriptors = list_driver_descriptors()

        self.assertEqual(
            [descriptor.driver_id for descriptor in descriptors],
            ["generic-web", "odoo", "verireel"],
        )
        descriptor_json = json.dumps(
            [descriptor.model_dump(mode="json") for descriptor in descriptors], sort_keys=True
        )
        self.assertNotIn("Dokploy", descriptor_json)
        self.assertNotIn("launchplane/self-deploy", descriptor_json)

    def test_odoo_descriptor_marks_prod_rollback_as_destructive(self) -> None:
        descriptor = read_driver_descriptor("odoo")
        actions = {action.action_id: action for action in descriptor.actions}

        self.assertEqual(actions["prod_backup_gate"].safety, "safe_write")
        self.assertEqual(actions["prod_promotion"].safety, "mutation")
        self.assertEqual(actions["prod_rollback"].safety, "destructive")
        self.assertEqual(actions["prod_rollback"].route_path, "/v1/drivers/odoo/prod-rollback")

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

    def test_driver_actions_declare_route_authorization_metadata(self) -> None:
        route_actions = {
            action.route_path: action
            for descriptor in list_driver_descriptors()
            for action in descriptor.actions
            if action.route_path
        }

        self.assertTrue(all(action.authz_action for action in route_actions.values()))
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

    def test_generic_web_readiness_execution_metadata_matches_descriptor(self) -> None:
        descriptor = read_driver_descriptor("generic-web")
        actions = {action.action_id: action for action in descriptor.actions}
        readiness_action = actions["preview_readiness"]
        execution_metadata = control_plane_service._GENERIC_WEB_PREVIEW_READINESS_ROUTE

        self.assertEqual(execution_metadata.route_path, readiness_action.route_path)
        self.assertIs(
            execution_metadata.envelope_model,
            control_plane_service.GenericWebPreviewReadinessEnvelope,
        )
        self.assertIn("preview readiness", execution_metadata.denial_message)

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

        with patch.object(registry, "_DESCRIPTORS", (descriptor,)):
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

    def test_unknown_driver_descriptor_is_missing(self) -> None:
        with self.assertRaises(FileNotFoundError):
            read_driver_descriptor("missing")


if __name__ == "__main__":
    unittest.main()
