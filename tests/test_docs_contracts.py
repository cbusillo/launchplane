from pathlib import Path
from unittest import TestCase


class DocsContractsTests(TestCase):
    def test_post_v2_transition_plans_are_issue_backed(self) -> None:
        docs_index = Path("docs/README.md").read_text(encoding="utf-8")
        service_boundary = Path("docs/service-boundary.md").read_text(encoding="utf-8")

        self.assertFalse(Path("docs/v2-foundation-adr.md").exists())
        self.assertFalse(Path("docs/compatibility-retirement.md").exists())
        self.assertNotIn("v2-foundation-adr.md", docs_index)
        self.assertNotIn("compatibility-retirement.md", docs_index)
        self.assertIn("Service Route Checklist", service_boundary)
        self.assertIn("FastAPI owns the production path", service_boundary)
        self.assertIn("stable `operation_id`", service_boundary)
        self.assertIn("maximum body-size behavior", service_boundary)
        self.assertIn("`400`, `413`, `401`, and `403`", service_boundary)
        self.assertIn("deletes obsolete compatibility code", service_boundary)
        self.assertIn("issue-backed removal condition", service_boundary)

    def test_product_environment_evidence_includes_config_status(self) -> None:
        workflow_text = Path(".github/workflows/product-environment-evidence.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "/v1/products/${product}/environments/${environment}/config-status",
            workflow_text,
        )
        self.assertIn("(.config_status // .environment // .) as $config_status", workflow_text)
        self.assertIn("config-status-summary.json", workflow_text)
        self.assertIn("product-environment-evidence-results/*-summary.json", workflow_text)

    def test_post_v2_smoke_evidence_is_durable_and_sanitized(self) -> None:
        deploy_workflow = Path(".github/workflows/deploy-launchplane.yml").read_text(
            encoding="utf-8"
        )
        odoo_smoke_workflow = Path(".github/workflows/odoo-driver-route-smoke.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("Capture v2 deployed smoke evidence", deploy_workflow)
        self.assertIn("launchplane-v2-deployed-smoke.json", deploy_workflow)
        self.assertIn("actions/upload-artifact@v7", deploy_workflow)
        self.assertIn("/v1/health", deploy_workflow)
        self.assertIn("/v1/service/runtime", deploy_workflow)
        self.assertIn("/openapi.json", deploy_workflow)
        self.assertIn("/v1/work-graph/snapshot", deploy_workflow)
        self.assertIn("image_reference", deploy_workflow)
        self.assertIn("jq -s '{status: \"ok\", results: .}'", deploy_workflow)

        self.assertIn("odoo-driver-route-smoke-registration.jsonl", odoo_smoke_workflow)
        self.assertIn("odoo-driver-route-smoke-results.jsonl", odoo_smoke_workflow)
        self.assertIn("Upload route smoke evidence", odoo_smoke_workflow)
        self.assertIn("not 404 and not 5xx", odoo_smoke_workflow)
        self.assertIn("Public registration probes", odoo_smoke_workflow)
        self.assertIn("Authenticated route probes", odoo_smoke_workflow)

    def test_product_repo_integration_contract_prefers_image_deploy(self) -> None:
        product_repo_contract = Path("docs/product-repo-contract.md").read_text(encoding="utf-8")
        dokploy_service_contract = Path("docs/dokploy-service-deployments.md").read_text(
            encoding="utf-8"
        )
        service_boundary = Path("docs/service-boundary.md").read_text(encoding="utf-8")

        self.assertIn("Canonical Image Deploy Connector", product_repo_contract)
        self.assertIn("Image-backed generic-web deploy", product_repo_contract)
        self.assertIn("repairshopr_api", product_repo_contract)
        self.assertIn("deployment-20260630T034901Z-repairshopr-sync-prod", product_repo_contract)
        self.assertIn("baseline for retiring older source-ref", product_repo_contract)
        self.assertIn("reusable-product-repo-config-authority.yml@main", product_repo_contract)
        self.assertIn("pinned Launchplane tool checkout", product_repo_contract)
        self.assertIn("Product repos build, test, smoke, and publish", product_repo_contract)
        self.assertIn("Launchplane derives lifecycle meaning", product_repo_contract)
        self.assertIn("Operators act through Launchplane, not around it", product_repo_contract)
        self.assertIn("changes the hiding place, not the ownership boundary", product_repo_contract)
        self.assertIn("operator-seeded GitHub variables", product_repo_contract)
        self.assertIn("scoped adapter", product_repo_contract)
        self.assertIn("inputs. They are not checked-in product topology", product_repo_contract)
        self.assertIn("#1528 owns reducing that bridge", product_repo_contract)

        self.assertIn("stable product-repo integration surface", dokploy_service_contract)
        self.assertIn("RepairShopr Sync is the first live canary", dokploy_service_contract)
        self.assertIn(
            "without source-ref deploy or direct Dokploy mutation", dokploy_service_contract
        )
        self.assertIn("source-ref deploy bridge is retired", dokploy_service_contract)
        self.assertIn("do not use the bridge", dokploy_service_contract)
        self.assertIn("publish immutable images", dokploy_service_contract)

        self.assertIn("canonical product-repo integration surface", service_boundary)
        self.assertIn("source-ref deploy route is retired", service_boundary)

    def test_odoo_base_image_promotion_owner_is_documented(self) -> None:
        records_doc = Path("docs/records.md").read_text(encoding="utf-8")

        self.assertIn("odoo-docker` owns Odoo base-image build and promotion", records_doc)
        self.assertIn("candidate, testing, and stable image tracks", records_doc)
        self.assertIn("Launchplane does not create a", records_doc)
        self.assertIn("separate base-image promotion record today", records_doc)
        self.assertIn("Add a Launchplane-owned base-image promotion record only if", records_doc)

    def test_driver_contract_keeps_lifecycle_fixtures_in_launchplane(self) -> None:
        driver_development = Path("docs/driver-development.md").read_text(encoding="utf-8")

        self.assertIn(
            "Drivers exist to move lifecycle knowledge out of product repos", driver_development
        )
        self.assertIn("product-specific hard-coding inside Launchplane", driver_development)
        self.assertIn("Lifecycle fixtures follow the same boundary", driver_development)
        self.assertIn("contract builders own fixtures", driver_development)
        self.assertIn("Odoo is the reference complex-product case", driver_development)
