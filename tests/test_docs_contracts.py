from pathlib import Path
from unittest import TestCase


class DocsContractsTests(TestCase):
    def test_odoo_base_image_promotion_owner_is_documented(self) -> None:
        records_doc = Path("docs/records.md").read_text(encoding="utf-8")

        self.assertIn("odoo-docker` owns Odoo base-image build and promotion", records_doc)
        self.assertIn("candidate, testing, and stable image tracks", records_doc)
        self.assertIn("Launchplane does not create a", records_doc)
        self.assertIn("separate base-image promotion record today", records_doc)
        self.assertIn("Add a Launchplane-owned base-image promotion record only if", records_doc)
