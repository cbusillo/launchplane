import unittest

from control_plane.contracts.odoo_runtime_environment import (
    merge_required_odoo_addons_path,
)


class OdooRuntimeEnvironmentTests(unittest.TestCase):
    def test_merge_required_addons_preserves_compose_default_when_not_explicit(self) -> None:
        self.assertEqual(merge_required_odoo_addons_path(""), "")

    def test_merge_required_addons_preserves_order_and_adds_missing_paths(self) -> None:
        self.assertEqual(
            merge_required_odoo_addons_path(
                "/opt/project/addons,/opt/launchplane/addons,/odoo/addons"
            ),
            "/opt/project/addons,/opt/launchplane/addons,/odoo/addons,/opt/enterprise",
        )

    def test_merge_required_addons_deduplicates_existing_paths(self) -> None:
        self.assertEqual(
            merge_required_odoo_addons_path(
                "/opt/project/addons,/opt/enterprise,/opt/launchplane/addons,/opt/enterprise"
            ),
            "/opt/project/addons,/opt/enterprise,/opt/launchplane/addons",
        )
