import json
from pathlib import Path
from unittest import TestCase


class DocsContractsTests(TestCase):
    def test_ui_openapi_matches_canonical_ui_operations(self) -> None:
        canonical_openapi = json.loads(
            Path("frontend/generated/openapi-canonical.json").read_text(encoding="utf-8")
        )
        ui_openapi = json.loads(
            Path("frontend/generated/openapi-ui.json").read_text(encoding="utf-8")
        )
        browser_write_contract = Path("frontend/src/browser-write-contract.ts").read_text(
            encoding="utf-8"
        )

        read_operations = canonical_openapi["x-launchplane-ui-read-operations"]
        write_operations = canonical_openapi["x-launchplane-ui-write-operations"]
        self.assertEqual(
            set(read_operations) | set(write_operations),
            set(ui_openapi["paths"]),
        )
        for route_path, operation_id in read_operations.items():
            self.assertEqual(set(ui_openapi["paths"][route_path]), {"get"})
            self.assertEqual(ui_openapi["paths"][route_path]["get"]["operationId"], operation_id)
        for route_path, operation_id in write_operations.items():
            self.assertEqual(set(ui_openapi["paths"][route_path]), {"post"})
            self.assertEqual(ui_openapi["paths"][route_path]["post"]["operationId"], operation_id)
        for route_path in write_operations:
            self.assertIn(route_path, browser_write_contract)
