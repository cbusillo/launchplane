import unittest
from typing import Literal

from control_plane.contracts.odoo_preview_runtime_plan import (
    OdooPreviewProviderCapabilities,
    OdooPreviewRuntimeBindingEvidence,
    OdooPreviewRuntimePlan,
    OdooPreviewRuntimePlanRequest,
    OdooPreviewRuntimeTargetEvidence,
    plan_odoo_preview_runtime,
)
from control_plane.workflows.odoo_preview_runtime import (
    OdooPreviewDokployDryRunRequest,
    OdooPreviewDokployEndpointSpec,
    build_odoo_preview_dokploy_dry_run,
)


def _capabilities() -> OdooPreviewProviderCapabilities:
    return OdooPreviewProviderCapabilities(
        can_create_compose=True,
        can_update_compose_env=True,
        can_deploy_compose=True,
        can_bind_domain=True,
        can_delete_compose=True,
        can_delete_domain=True,
    )


def _endpoint_spec() -> OdooPreviewDokployEndpointSpec:
    return OdooPreviewDokployEndpointSpec(
        compose_create_path="/api/compose.create",
        compose_delete_path="/api/compose.delete",
    )


def _bindings() -> tuple[OdooPreviewRuntimeBindingEvidence, ...]:
    return (
        OdooPreviewRuntimeBindingEvidence(
            key="WEB_BASE_URL",
            source="runtime_environment",
            scope="preview",
        ),
        OdooPreviewRuntimeBindingEvidence(
            key="ODOO_ADMIN_PASSWORD",
            source="managed_secret",
            scope="preview",
        ),
    )


def _target() -> OdooPreviewRuntimeTargetEvidence:
    return OdooPreviewRuntimeTargetEvidence(
        target_id="compose-cm-pr-45",
        target_name="cm-pr-45",
        context="cm-preview",
        instance="pr-45",
        environment_kind="preview",
        domain="pr-45.cm-preview.example.test",
    )


def _runtime_plan(
    *,
    operation: Literal["refresh", "destroy"] = "refresh",
    target: OdooPreviewRuntimeTargetEvidence | None = None,
) -> OdooPreviewRuntimePlan:
    return plan_odoo_preview_runtime(
        request=OdooPreviewRuntimePlanRequest(
            operation=operation,
            product="odoo-tenant-cm",
            repository="cbusillo/odoo-tenant-cm",
            pr_number=45,
            preview_slug="pr-45",
            preview_url="https://pr-45.cm-preview.example.test",
            strategy="isolated_dokploy_compose",
            image_reference="ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123",
            source_git_ref="abc123",
            target=target,
            provider_capabilities=_capabilities(),
            runtime_bindings=_bindings(),
            required_runtime_keys=("WEB_BASE_URL", "ODOO_ADMIN_PASSWORD"),
        )
    )


class OdooPreviewDokployDryRunTests(unittest.TestCase):
    def test_refresh_create_dry_run_requires_explicit_create_and_delete_paths(self) -> None:
        plan = build_odoo_preview_dokploy_dry_run(
            request=OdooPreviewDokployDryRunRequest(runtime_plan=_runtime_plan())
        )

        self.assertEqual(plan.status, "blocked")
        self.assertIn("endpoint_path_missing", {blocker.code for blocker in plan.blockers})
        self.assertEqual(plan.operations, ())

    def test_refresh_create_dry_run_renders_ordered_provider_operations(self) -> None:
        plan = build_odoo_preview_dokploy_dry_run(
            request=OdooPreviewDokployDryRunRequest(
                runtime_plan=_runtime_plan(),
                endpoint_spec=_endpoint_spec(),
                environment_id="env-cm-preview",
            )
        )

        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.domain_host, "pr-45.cm-preview.example.test")
        self.assertEqual(
            [operation.name for operation in plan.operations],
            [
                "compose_create",
                "compose_update_raw_source",
                "compose_update_env",
                "domain_lookup",
                "domain_create_or_update",
                "compose_deploy",
                "smoke_check",
            ],
        )
        self.assertEqual(
            [operation.name for operation in plan.rollback_operations],
            ["domain_delete", "compose_delete"],
        )
        self.assertTrue(
            any(
                operation.secret_payload
                for operation in plan.operations
                if operation.name == "compose_update_env"
            )
        )

    def test_refresh_existing_runtime_does_not_plan_delete_rollback(self) -> None:
        plan = build_odoo_preview_dokploy_dry_run(
            request=OdooPreviewDokployDryRunRequest(
                runtime_plan=_runtime_plan(target=_target()),
                endpoint_spec=OdooPreviewDokployEndpointSpec(),
            )
        )

        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.compose_ref, "compose-cm-pr-45")
        self.assertEqual(plan.operations[0].name, "compose_update_raw_source")
        self.assertEqual(plan.rollback_operations, ())

    def test_destroy_dry_run_renders_domain_then_compose_delete(self) -> None:
        plan = build_odoo_preview_dokploy_dry_run(
            request=OdooPreviewDokployDryRunRequest(
                runtime_plan=_runtime_plan(operation="destroy", target=_target()),
                endpoint_spec=_endpoint_spec(),
            )
        )

        self.assertEqual(plan.status, "ready")
        self.assertEqual(
            [operation.name for operation in plan.operations],
            ["domain_lookup", "domain_delete", "compose_delete"],
        )
        self.assertEqual(plan.rollback_operations, ())

    def test_blocked_runtime_plan_blocks_provider_dry_run(self) -> None:
        runtime_plan = plan_odoo_preview_runtime(
            request=OdooPreviewRuntimePlanRequest(
                operation="refresh",
                product="odoo-tenant-cm",
                repository="cbusillo/odoo-tenant-cm",
                pr_number=45,
                preview_slug="pr-45",
                strategy="staged_compose_mvp",
                provider_capabilities=_capabilities(),
            )
        )

        plan = build_odoo_preview_dokploy_dry_run(
            request=OdooPreviewDokployDryRunRequest(
                runtime_plan=runtime_plan,
                endpoint_spec=_endpoint_spec(),
            )
        )

        self.assertEqual(plan.status, "blocked")
        self.assertIn("runtime_plan_not_ready", {blocker.code for blocker in plan.blockers})

    def test_no_cache_refresh_uses_redeploy_endpoint(self) -> None:
        plan = build_odoo_preview_dokploy_dry_run(
            request=OdooPreviewDokployDryRunRequest(
                runtime_plan=_runtime_plan(target=_target()),
                endpoint_spec=OdooPreviewDokployEndpointSpec(),
                no_cache=True,
            )
        )

        deploy_operations = tuple(
            operation for operation in plan.operations if operation.name == "compose_deploy"
        )
        self.assertEqual(len(deploy_operations), 1)
        self.assertEqual(deploy_operations[0].path, "/api/compose.redeploy")


if __name__ == "__main__":
    unittest.main()
