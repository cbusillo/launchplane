import unittest
from typing import Literal
from unittest.mock import ANY, patch

from control_plane.contracts.odoo_preview_runtime_plan import (
    OdooPreviewProviderCapabilities,
    OdooPreviewRuntimeBindingEvidence,
    OdooPreviewRuntimePlan,
    OdooPreviewRuntimePlanRequest,
    OdooPreviewRuntimeTargetEvidence,
    plan_odoo_preview_runtime,
)
from control_plane.workflows.odoo_preview_runtime import (
    OdooPreviewDokployApplyRequest,
    OdooPreviewDokployDryRunRequest,
    OdooPreviewDokployEndpointSpec,
    build_odoo_preview_dokploy_dry_run,
    execute_odoo_preview_dokploy_apply,
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
    return OdooPreviewDokployEndpointSpec()


def _bindings() -> tuple[OdooPreviewRuntimeBindingEvidence, ...]:
    return (
        OdooPreviewRuntimeBindingEvidence(
            key="WEB_BASE_URL",
            source="runtime_environment",
        ),
        OdooPreviewRuntimeBindingEvidence(
            key="ODOO_ADMIN_PASSWORD",
            source="managed_secret",
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


def _environment_values() -> dict[str, str]:
    return {
        "ODOO_DB_NAME": "cm_pr_45",
        "ODOO_DB_USER": "odoo",
        "ODOO_DB_PASSWORD": "safe-db",
        "ODOO_DATA_VOLUME": "cm_pr_45_data",
        "ODOO_LOG_VOLUME": "cm_pr_45_logs",
        "ODOO_DB_VOLUME": "cm_pr_45_db",
        "ODOO_MASTER_PASSWORD": "safe-master",
        "ODOO_ADMIN_PASSWORD": "safe-admin",
    }


class OdooPreviewDokployDryRunTests(unittest.TestCase):
    def test_refresh_create_dry_run_blocks_missing_create_and_delete_paths(self) -> None:
        plan = build_odoo_preview_dokploy_dry_run(
            request=OdooPreviewDokployDryRunRequest(
                runtime_plan=_runtime_plan(),
                endpoint_spec=OdooPreviewDokployEndpointSpec(
                    compose_create_path="",
                    compose_delete_path="",
                ),
            )
        )

        self.assertEqual(plan.status, "blocked")
        self.assertIn("endpoint_path_missing", {blocker.code for blocker in plan.blockers})
        self.assertEqual(plan.operations, ())

    def test_refresh_create_dry_run_blocks_missing_environment_id(self) -> None:
        plan = build_odoo_preview_dokploy_dry_run(
            request=OdooPreviewDokployDryRunRequest(
                runtime_plan=_runtime_plan(),
                endpoint_spec=_endpoint_spec(),
            )
        )

        self.assertEqual(plan.status, "blocked")
        self.assertIn("environment_id_missing", {blocker.code for blocker in plan.blockers})

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
        self.assertEqual(plan.operations[0].path, "/api/compose.create")
        self.assertEqual(
            plan.operations[0].payload_keys,
            ("name", "appName", "environmentId", "composeType"),
        )
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
        compose_delete = plan.rollback_operations[-1]
        self.assertEqual(compose_delete.path, "/api/compose.delete")
        self.assertIn("deleteVolumes", compose_delete.payload_keys)
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
        self.assertIn("deleteVolumes", plan.operations[-1].payload_keys)
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

    def test_apply_refresh_creates_updates_deploys_and_smokes_compose(self) -> None:
        dry_run = build_odoo_preview_dokploy_dry_run(
            request=OdooPreviewDokployDryRunRequest(
                runtime_plan=_runtime_plan(),
                endpoint_spec=_endpoint_spec(),
                environment_id="env-cm-preview",
            )
        )
        requests: list[dict[str, object]] = []

        def _fake_dokploy_request(**kwargs: object) -> object:
            requests.append(dict(kwargs))
            if kwargs["path"] == "/api/compose.create":
                return {"composeId": "compose-cm-pr-45"}
            return {}

        with (
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.dokploy_request",
                side_effect=_fake_dokploy_request,
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.fetch_dokploy_target_payload",
                return_value={"composeId": "compose-cm-pr-45", "environmentId": "env-cm-preview"},
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.sync_dokploy_compose_raw_source",
            ) as sync_source,
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.update_dokploy_target_env",
            ) as update_env,
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.ensure_compose_web_domain_route",
                return_value="domain-cm-pr-45",
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.latest_deployment_for_target",
                return_value={"deploymentId": "before"},
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.trigger_deployment",
            ) as trigger_deployment,
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.wait_for_target_deployment",
            ) as wait_deploy,
            patch(
                "control_plane.workflows.odoo_preview_runtime._wait_for_smoke_check",
            ) as smoke_check,
        ):
            result = execute_odoo_preview_dokploy_apply(
                control_plane_root=ANY,
                request=OdooPreviewDokployApplyRequest(
                    dry_run_plan=dry_run,
                    image_reference="ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123",
                    environment_values=_environment_values(),
                ),
            )

        self.assertEqual(result.status, "pass")
        self.assertEqual(result.compose_id, "compose-cm-pr-45")
        self.assertTrue(result.created_compose)
        self.assertEqual([request["path"] for request in requests], ["/api/compose.create"])
        create_payload = requests[0]["payload"]
        self.assertIsInstance(create_payload, dict)
        assert isinstance(create_payload, dict)
        self.assertEqual(create_payload["environmentId"], "env-cm-preview")
        sync_source.assert_called_once()
        update_env.assert_called_once()
        trigger_deployment.assert_called_once_with(
            host="https://dokploy.example",
            token="token",
            target_type="compose",
            target_id="compose-cm-pr-45",
            no_cache=False,
        )
        wait_deploy.assert_called_once()
        smoke_check.assert_called_once()

    def test_apply_blocks_missing_runtime_env_before_provider_calls(self) -> None:
        dry_run = build_odoo_preview_dokploy_dry_run(
            request=OdooPreviewDokployDryRunRequest(
                runtime_plan=_runtime_plan(target=_target()),
                endpoint_spec=_endpoint_spec(),
            )
        )

        with patch(
            "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.read_dokploy_config"
        ) as read_dokploy_config:
            result = execute_odoo_preview_dokploy_apply(
                control_plane_root=ANY,
                request=OdooPreviewDokployApplyRequest(
                    dry_run_plan=dry_run,
                    image_reference="ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123",
                    environment_values={"ODOO_DB_NAME": "cm_pr_45"},
                ),
            )

        self.assertEqual(result.status, "blocked")
        self.assertIn("ODOO_DB_USER", result.error_message)
        read_dokploy_config.assert_not_called()

    def test_apply_destroy_deletes_domain_then_compose_with_volumes(self) -> None:
        dry_run = build_odoo_preview_dokploy_dry_run(
            request=OdooPreviewDokployDryRunRequest(
                runtime_plan=_runtime_plan(operation="destroy", target=_target()),
                endpoint_spec=_endpoint_spec(),
            )
        )
        requests: list[dict[str, object]] = []

        def _fake_dokploy_request(**kwargs: object) -> object:
            requests.append(dict(kwargs))
            if kwargs["path"] == "/api/domain.byComposeId":
                return [
                    {
                        "domainId": "domain-cm-pr-45",
                        "host": "pr-45.cm-preview.example.test",
                    }
                ]
            return {}

        with (
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.odoo_preview_runtime.control_plane_dokploy.dokploy_request",
                side_effect=_fake_dokploy_request,
            ),
        ):
            result = execute_odoo_preview_dokploy_apply(
                control_plane_root=ANY,
                request=OdooPreviewDokployApplyRequest(
                    dry_run_plan=dry_run,
                    image_reference="ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123",
                    environment_values=_environment_values(),
                ),
            )

        self.assertEqual(result.status, "pass")
        self.assertEqual(
            [request["path"] for request in requests],
            ["/api/domain.byComposeId", "/api/domain.delete", "/api/compose.delete"],
        )
        delete_payload = requests[-1]["payload"]
        self.assertIsInstance(delete_payload, dict)
        assert isinstance(delete_payload, dict)
        self.assertEqual(delete_payload["composeId"], "compose-cm-pr-45")
        self.assertTrue(delete_payload["deleteVolumes"])


if __name__ == "__main__":
    unittest.main()
