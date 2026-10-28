from unittest import TestCase

from control_plane.openapi_export import canonical_openapi_document
from tests.support.openapi import ui_openapi_document


class DocsContractsTests(TestCase):
    def test_ui_openapi_matches_canonical_ui_operations(self) -> None:
        canonical_openapi = canonical_openapi_document()
        ui_openapi = ui_openapi_document()

        read_operations = canonical_openapi["x-launchplane-ui-read-operations"]
        write_operations = canonical_openapi["x-launchplane-ui-write-operations"]
        self.assertEqual(
            set(read_operations) | set(write_operations),
            set(ui_openapi["paths"]),
        )
        for route_path, path_item in ui_openapi["paths"].items():
            expected_operations = {
                method: operations[route_path]
                for method, operations in (("get", read_operations), ("post", write_operations))
                if route_path in operations
            }
            self.assertEqual(set(path_item) - {"parameters"}, set(expected_operations))
            for method, operation_id in expected_operations.items():
                self.assertEqual(path_item[method]["operationId"], operation_id)
