import json
import unittest

from control_plane.drivers.registry import list_driver_descriptors, read_driver_descriptor


class DriverDescriptorRegistryTests(unittest.TestCase):
    def test_registry_lists_product_drivers_without_provider_vocabulary(self) -> None:
        descriptors = list_driver_descriptors()

        self.assertEqual([descriptor.driver_id for descriptor in descriptors], ["odoo", "verireel"])
        descriptor_json = json.dumps(
            [descriptor.model_dump(mode="json") for descriptor in descriptors], sort_keys=True
        )
        self.assertNotIn("Dokploy", descriptor_json)
        self.assertNotIn("launchplane/self-deploy", descriptor_json)

    def test_odoo_descriptor_marks_prod_rollback_as_destructive(self) -> None:
        descriptor = read_driver_descriptor("odoo")
        actions = {action.action_id: action for action in descriptor.actions}

        self.assertEqual(actions["prod_backup_gate"].safety, "safe_write")
        self.assertEqual(actions["prod_promotion"].safety, "mutation")
        self.assertEqual(actions["prod_rollback"].safety, "destructive")
        self.assertEqual(actions["prod_rollback"].route_path, "/v1/drivers/odoo/prod-rollback")

    def test_verireel_descriptor_exposes_preview_and_stable_capabilities(self) -> None:
        descriptor = read_driver_descriptor("verireel")
        actions = {action.action_id: action for action in descriptor.actions}

        self.assertEqual(actions["preview_inventory"].safety, "read")
        self.assertEqual(actions["preview_refresh"].scope, "preview")
        self.assertEqual(actions["preview_destroy"].safety, "destructive")
        self.assertEqual(actions["prod_rollback"].safety, "destructive")

    def test_unknown_driver_descriptor_is_missing(self) -> None:
        with self.assertRaises(FileNotFoundError):
            read_driver_descriptor("missing")


if __name__ == "__main__":
    unittest.main()
