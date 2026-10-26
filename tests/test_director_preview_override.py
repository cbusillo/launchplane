import unittest

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from tests.support.profiles import _odoo_preview_profile_payload


class RetiredPreviewOverrideTests(unittest.TestCase):
    def test_profile_settings_write_requires_managed_preview_scope(self) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
        profile.preview.override_env = {"ODOO_PROXY_MODE": "True"}
        with self.assertRaisesRegex(ValueError, "managed product configuration"):
            profile.validate_write_contract()
