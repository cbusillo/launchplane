from pathlib import Path
from unittest import TestCase


class DocsContractsTests(TestCase):
    def test_v2_route_migration_discipline_is_documented(self) -> None:
        service_boundary = Path("docs/service-boundary.md").read_text(encoding="utf-8")
        foundation_adr = Path("docs/v2-foundation-adr.md").read_text(encoding="utf-8")
        retirement_doc = Path("docs/compatibility-retirement.md").read_text(encoding="utf-8")

        self.assertIn("Native Route Migration Checklist", service_boundary)
        self.assertIn("native FastAPI route owns the path", service_boundary)
        self.assertIn("stable `operation_id`", service_boundary)
        self.assertIn("maximum body-size behavior", service_boundary)
        self.assertIn("`400`, `413`, `401`, and `403`", service_boundary)
        self.assertIn("one route family at a time", foundation_adr)
        self.assertIn("OpenAPI examples are contract examples", foundation_adr)
        self.assertIn("route-family by", retirement_doc)
        self.assertIn("name the removal condition", retirement_doc)

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

    def test_odoo_base_image_promotion_owner_is_documented(self) -> None:
        records_doc = Path("docs/records.md").read_text(encoding="utf-8")

        self.assertIn("odoo-docker` owns Odoo base-image build and promotion", records_doc)
        self.assertIn("candidate, testing, and stable image tracks", records_doc)
        self.assertIn("Launchplane does not create a", records_doc)
        self.assertIn("separate base-image promotion record today", records_doc)
        self.assertIn("Add a Launchplane-owned base-image promotion record only if", records_doc)
