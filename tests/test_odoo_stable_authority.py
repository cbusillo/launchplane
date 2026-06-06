from pathlib import Path
from unittest import TestCase


_PRODUCTION_MUTATION_WORKFLOW_PATHS = (
    Path(".github/workflows/product-context-cutover.yml"),
    Path(".github/workflows/product-context-cutover-audit.yml"),
    Path(".github/workflows/product-legacy-context-cleanup.yml"),
    Path(".github/workflows/odoo-config-parameter-override.yml"),
    Path(".github/workflows/odoo-target-replacement-plan.yml"),
    Path(".github/workflows/odoo-target-replacement-apply.yml"),
    Path(".github/workflows/odoo-stable-bootstrap.yml"),
    Path(".github/workflows/odoo-website-bootstrap-override.yml"),
)


class OdooStableAuthorityTests(TestCase):
    def test_production_mutation_workflows_do_not_default_to_real_topology(
        self,
    ) -> None:
        forbidden_tokens = (
            "default: sellyouroutboard",
            "default: odoo-tenant-",
            "default: cm",
            "default: opw",
            "SellYourOutboard",
            "sellyouroutboard-testing",
            "https://cm-testing.shinycomputers.com",
            "allowed_targets=",
            "odoo-tenant-cm:",
            "odoo-tenant-opw:",
        )
        offenders: list[str] = []
        for workflow_path in _PRODUCTION_MUTATION_WORKFLOW_PATHS:
            workflow_text = workflow_path.read_text(encoding="utf-8")
            for token in forbidden_tokens:
                if token in workflow_text:
                    offenders.append(f"{workflow_path.as_posix()}: {token}")

        self.assertEqual(offenders, [])

    def test_reusable_testing_deploy_uses_target_replacement_apply(self) -> None:
        workflow = Path(".github/workflows/reusable-odoo-testing-deploy.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("route-path: /v1/drivers/odoo/target-replacement-apply", workflow)
        self.assertIn("poll_url=result.poll_url", workflow)
        self.assertIn('${LAUNCHPLANE_URL}${POLL_URL}', workflow)
        self.assertNotIn("route-path: /v1/drivers/odoo/testing-deploy", workflow)
        self.assertNotIn("odoo_testing_deploy.execute", workflow)

    def test_odoo_stable_workflows_do_not_use_retired_testing_deploy_route(self) -> None:
        retired_tokens = (
            "/v1/drivers/odoo/testing-deploy",
            "odoo_testing_deploy.execute",
            "control_plane.workflows.odoo_testing_deploy",
        )
        scanned_paths = tuple(
            path
            for root in (Path("control_plane"), Path(".github/workflows"), Path("scripts/deploy"))
            for path in root.rglob("*")
            if path.is_file() and path.suffix in {".py", ".sh", ".yml", ".yaml"}
        )

        offenders: list[str] = []
        for path in scanned_paths:
            text = path.read_text(encoding="utf-8")
            for token in retired_tokens:
                if token in text:
                    offenders.append(f"{path.as_posix()}: {token}")

        self.assertEqual(offenders, [])

    def test_prod_rollback_has_no_direct_provider_mutation(self) -> None:
        rollback_source = Path("control_plane/workflows/odoo_prod_rollback.py").read_text(
            encoding="utf-8"
        )
        required_tokens = (
            "execute_odoo_stable_target_replacement_apply",
            "OdooStableTargetReplacementApplyRequest",
        )
        forbidden_tokens = (
            "control_plane_dokploy.trigger_deployment",
            "control_plane_dokploy.sync_dokploy_compose_raw_source",
            "control_plane_dokploy.update_dokploy_target_env",
            "execute_odoo_post_deploy",
            "render_odoo_raw_compose_file",
            "wait_for_target_deployment",
        )

        for token in required_tokens:
            self.assertIn(token, rollback_source)
        for token in forbidden_tokens:
            self.assertNotIn(token, rollback_source)

    def test_documents_name_target_replacement_as_canonical_rollback_deployer(self) -> None:
        operations_doc = Path("docs/operations.md").read_text(encoding="utf-8")
        records_doc = Path("docs/records.md").read_text(encoding="utf-8")
        service_boundary_doc = Path("docs/service-boundary.md").read_text(encoding="utf-8")

        self.assertIn("through the stable target replacement executor", operations_doc)
        self.assertIn("delegates the rollback deploy to stable target replacement", records_doc)
        self.assertIn("delegates the provider mutation to\nstable target replacement", service_boundary_doc)
