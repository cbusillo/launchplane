import unittest

from control_plane.generic_web_onboarding import (
    GenericWebOnboardingIntent,
    build_generic_web_onboarding_manifest,
    generic_web_onboarding_plan_sha256,
)
from control_plane.workflows.product_onboarding import build_product_profile_record


class GenericWebOnboardingTests(unittest.TestCase):
    def test_intent_defaults_and_manifest_are_conventional(self) -> None:
        intent = GenericWebOnboardingIntent(
            product="demo-web",
            display_name="Demo Web",
            repository="example/demo-web",
            repository_id="123",
            repository_owner_id="456",
            image_repository="ghcr.io/example/demo-web",
            runtime_port=3000,
            health_path="/healthz",
        )

        manifest = build_generic_web_onboarding_manifest(
            intent=intent,
            target_id="dokploy-app-1",
        )

        self.assertEqual(intent.testing_context, "demo-web-testing")
        self.assertEqual(intent.preview_context, "demo-web-preview")
        self.assertEqual(manifest.driver_id, "generic-web")
        self.assertEqual(manifest.repository_id, "123")
        self.assertEqual(manifest.repository_owner_id, "456")
        self.assertEqual(manifest.default_branch, "main")
        self.assertEqual(len(manifest.lanes), 1)
        self.assertEqual(manifest.lanes[0].instance, "testing")
        self.assertTrue(manifest.preview.enabled)
        self.assertEqual(manifest.preview.domain_certificate_type, "none")
        self.assertEqual(len(manifest.provider_targets), 1)
        self.assertEqual(manifest.provider_targets[0].target_id, "dokploy-app-1")
        self.assertEqual(manifest.provider_targets[0].target_type, "application")

        profile = build_product_profile_record(
            manifest=manifest,
            updated_at="2026-08-05T00:00:00Z",
        )
        self.assertEqual(profile.repository_id, "123")
        self.assertEqual(profile.repository_owner_id, "456")
        self.assertEqual(profile.default_branch, "main")

    def test_plan_digest_is_deterministic_and_input_bound(self) -> None:
        intent = GenericWebOnboardingIntent(
            product="demo-web",
            display_name="Demo Web",
            repository="example/demo-web",
            repository_id="123",
            repository_owner_id="456",
            image_repository="ghcr.io/example/demo-web",
            runtime_port=3000,
            health_path="/healthz",
        )

        first = generic_web_onboarding_plan_sha256(intent)
        second = generic_web_onboarding_plan_sha256(intent.model_copy())
        changed = generic_web_onboarding_plan_sha256(
            intent.model_copy(update={"runtime_port": 8080})
        )

        self.assertEqual(first, second)
        self.assertNotEqual(first, changed)

    def test_apply_requires_resolved_target_id(self) -> None:
        intent = GenericWebOnboardingIntent(
            product="demo-web",
            display_name="Demo Web",
            repository="example/demo-web",
            repository_id="123",
            repository_owner_id="456",
            image_repository="ghcr.io/example/demo-web",
            runtime_port=3000,
            health_path="/healthz",
        )

        with self.assertRaisesRegex(ValueError, "resolved target_id"):
            build_generic_web_onboarding_manifest(intent=intent, target_id="")

    def test_repository_identity_must_be_numeric(self) -> None:
        with self.assertRaisesRegex(ValueError, "repository_id"):
            GenericWebOnboardingIntent(
                product="demo-web",
                display_name="Demo Web",
                repository="example/demo-web",
                repository_id="not-numeric",
                repository_owner_id="456",
                image_repository="ghcr.io/example/demo-web",
                runtime_port=3000,
                health_path="/healthz",
            )

if __name__ == "__main__":
    unittest.main()
