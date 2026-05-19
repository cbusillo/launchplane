import unittest

from control_plane.contracts.odoo_preview_runtime_plan import (
    OdooPreviewProviderCapabilities,
    OdooPreviewRuntimeBindingEvidence,
    OdooPreviewRuntimePlanRequest,
    OdooPreviewRuntimeTargetEvidence,
    plan_odoo_preview_runtime,
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


def _preview_target() -> OdooPreviewRuntimeTargetEvidence:
    return OdooPreviewRuntimeTargetEvidence(
        target_id="compose-cm-pr-45",
        target_name="cm-odoo-preview-pr-45",
        context="cm-preview",
        instance="pr-45",
        environment_kind="preview",
        domain="pr-45.cm-preview.example.test",
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
        OdooPreviewRuntimeBindingEvidence(
            key="ODOO_MASTER_PASSWORD",
            source="managed_secret",
            scope="preview",
        ),
    )


def _request(**overrides: object) -> OdooPreviewRuntimePlanRequest:
    payload = {
        "operation": "refresh",
        "product": "odoo-tenant-cm",
        "repository": "cbusillo/odoo-tenant-cm",
        "pr_number": 45,
        "preview_slug": "pr-45",
        "preview_url": "https://pr-45.cm-preview.example.test",
        "strategy": "isolated_dokploy_compose",
        "image_reference": "ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123",
        "source_git_ref": "abc123",
        "target": _preview_target(),
        "provider_capabilities": _capabilities(),
        "runtime_bindings": _bindings(),
        "required_runtime_keys": (
            "WEB_BASE_URL",
            "ODOO_ADMIN_PASSWORD",
            "ODOO_MASTER_PASSWORD",
        ),
    }
    payload.update(overrides)
    return OdooPreviewRuntimePlanRequest.model_validate(payload)


class OdooPreviewRuntimePlanTests(unittest.TestCase):
    def test_refresh_plan_is_ready_for_isolated_preview_runtime(self) -> None:
        plan = plan_odoo_preview_runtime(request=_request())

        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.blockers, ())
        self.assertEqual(
            [action.action for action in plan.planned_actions],
            [
                "create_or_reuse_preview_compose",
                "render_preview_runtime_env",
                "bind_preview_domain",
                "deploy_preview_compose",
                "run_preview_smoke",
            ],
        )
        self.assertEqual(
            [action.action for action in plan.rollback_actions],
            ["delete_created_preview_domain", "delete_created_preview_compose"],
        )

    def test_stage_compose_strategy_blocks_refresh(self) -> None:
        plan = plan_odoo_preview_runtime(request=_request(strategy="staged_compose_mvp"))

        self.assertEqual(plan.status, "blocked")
        self.assertIn("runtime_strategy_not_isolated", {blocker.code for blocker in plan.blockers})

    def test_destroy_requires_preview_scoped_matching_target(self) -> None:
        plan = plan_odoo_preview_runtime(
            request=_request(
                operation="destroy",
                target=OdooPreviewRuntimeTargetEvidence(
                    target_id="compose-cm-testing",
                    target_name="cm-testing",
                    context="cm",
                    instance="testing",
                    environment_kind="testing",
                    domain="testing.cm.example.test",
                ),
            )
        )

        self.assertEqual(plan.status, "blocked")
        self.assertIn("runtime_target_not_preview", {blocker.code for blocker in plan.blockers})
        self.assertIn("runtime_target_slug_mismatch", {blocker.code for blocker in plan.blockers})

    def test_destroy_plan_deletes_only_matching_preview_runtime(self) -> None:
        plan = plan_odoo_preview_runtime(request=_request(operation="destroy"))

        self.assertEqual(plan.status, "ready")
        self.assertEqual(
            [action.action for action in plan.planned_actions],
            ["delete_preview_domain", "delete_preview_compose"],
        )
        self.assertTrue(all(action.target == "compose-cm-pr-45" for action in plan.planned_actions))

    def test_prod_scoped_secret_blocks_public_preview(self) -> None:
        bindings = (
            *_bindings(),
            OdooPreviewRuntimeBindingEvidence(
                key="SMTP_PASSWORD",
                source="managed_secret",
                scope="prod",
            ),
        )

        plan = plan_odoo_preview_runtime(request=_request(runtime_bindings=bindings))

        self.assertEqual(plan.status, "blocked")
        self.assertIn("prod_secret_binding", {blocker.code for blocker in plan.blockers})

    def test_default_odoo_credentials_block_public_preview(self) -> None:
        bindings = tuple(
            binding for binding in _bindings() if binding.key != "ODOO_ADMIN_PASSWORD"
        ) + (
            OdooPreviewRuntimeBindingEvidence(
                key="ODOO_ADMIN_PASSWORD",
                source="default",
                scope="preview",
            ),
        )

        plan = plan_odoo_preview_runtime(request=_request(runtime_bindings=bindings))

        self.assertEqual(plan.status, "blocked")
        self.assertIn("default_credential_binding", {blocker.code for blocker in plan.blockers})

    def test_missing_provider_rollback_capability_blocks_refresh(self) -> None:
        capabilities = _capabilities().model_copy(update={"can_delete_compose": False})

        plan = plan_odoo_preview_runtime(request=_request(provider_capabilities=capabilities))

        self.assertEqual(plan.status, "blocked")
        self.assertIn("provider_capability_missing", {blocker.code for blocker in plan.blockers})

    def test_missing_runtime_binding_blocks_refresh(self) -> None:
        plan = plan_odoo_preview_runtime(
            request=_request(required_runtime_keys=("WEB_BASE_URL", "ODOO_DB_PASSWORD"))
        )

        self.assertEqual(plan.status, "blocked")
        self.assertIn("runtime_env_missing", {blocker.code for blocker in plan.blockers})


if __name__ == "__main__":
    unittest.main()
