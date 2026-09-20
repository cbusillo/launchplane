import unittest

from pydantic import ValidationError

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from tests.support.profiles import _generic_site_profile_payload


class ProductOwnerProfileTests(unittest.TestCase):
    def test_profiles_written_before_owners_existed_load_without_an_owner(self) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(
            _generic_site_profile_payload(product="syo")
        )

        self.assertFalse(profile.owner.is_set)

    def test_owner_round_trips_and_mention_prefix_is_not_stored(self) -> None:
        payload = _generic_site_profile_payload(product="syo")
        payload["owner"] = {"github_login": "@site-owner", "github_id": "1234567"}

        profile = LaunchplaneProductProfileRecord.model_validate(payload)
        reloaded = LaunchplaneProductProfileRecord.model_validate(profile.model_dump(mode="json"))

        self.assertTrue(reloaded.owner.is_set)
        self.assertEqual(reloaded.owner.github_login, "site-owner")
        self.assertEqual(reloaded.owner.github_id, "1234567")

    def test_owner_identity_requires_the_immutable_id(self) -> None:
        payload = _generic_site_profile_payload(product="syo")
        payload["owner"] = {"github_login": "site-owner"}

        with self.assertRaises(ValidationError):
            LaunchplaneProductProfileRecord.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
