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
        for route_path, operation_id in read_operations.items():
            self.assertEqual(set(ui_openapi["paths"][route_path]), {"get"})
            self.assertEqual(ui_openapi["paths"][route_path]["get"]["operationId"], operation_id)
        for route_path, operation_id in write_operations.items():
            self.assertEqual(set(ui_openapi["paths"][route_path]), {"post"})
            self.assertEqual(ui_openapi["paths"][route_path]["post"]["operationId"], operation_id)
