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
            preview_base_url="https://demo-preview.example.com",
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
        self.assertEqual(len(manifest.runtime_environments), 1)
        self.assertEqual(manifest.runtime_environments[0].scope, "context")
        self.assertEqual(manifest.runtime_environments[0].context, "demo-web-preview")
        self.assertEqual(
            manifest.runtime_environments[0].env,
            {"LAUNCHPLANE_PREVIEW_BASE_URL": "https://demo-preview.example.com"},
        )
        self.assertEqual(
            manifest.expected_config.runtime_environment_keys[0].key,
            "LAUNCHPLANE_PREVIEW_BASE_URL",
        )
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
            preview_base_url="https://demo-preview.example.com",
        )

        first = generic_web_onboarding_plan_sha256(intent)
        second = generic_web_onboarding_plan_sha256(intent.model_copy())
        changed = generic_web_onboarding_plan_sha256(
            intent.model_copy(update={"runtime_port": 8080})
        )
        changed_preview_base_url = generic_web_onboarding_plan_sha256(
            intent.model_copy(update={"preview_base_url": "https://other-preview.example.com"})
        )

        self.assertEqual(first, second)
        self.assertNotEqual(first, changed)
        self.assertNotEqual(first, changed_preview_base_url)

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
            preview_base_url="https://demo-preview.example.com",
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
                preview_base_url="https://demo-preview.example.com",
            )

    def test_preview_base_url_is_required_and_root_scoped(self) -> None:
        required_fields = {
            "product": "demo-web",
            "display_name": "Demo Web",
            "repository": "example/demo-web",
            "repository_id": "123",
            "repository_owner_id": "456",
            "image_repository": "ghcr.io/example/demo-web",
            "runtime_port": 3000,
            "health_path": "/healthz",
        }

        for preview_base_url, message in (
            ("", "requires preview_base_url"),
            ("demo-preview.example.com", "must use http or https"),
            ("https://*.demo-preview.example.com", "not a wildcard"),
            ("https://192.0.2.10", "not an IP address"),
            ("https://demo-preview.example.com/path", "must be a root URL"),
            ("https://user:password@demo-preview.example.com", "cannot contain credentials"),
        ):
            with self.subTest(preview_base_url=preview_base_url):
                with self.assertRaisesRegex(ValueError, message):
                    GenericWebOnboardingIntent.model_validate(
                        {
                            **required_fields,
                            "preview_base_url": preview_base_url,
                        }
                    )

    def test_preview_base_url_is_normalized_for_digest_and_storage(self) -> None:
        intent = GenericWebOnboardingIntent(
            product="demo-web",
            display_name="Demo Web",
            repository="example/demo-web",
            repository_id="123",
            repository_owner_id="456",
            image_repository="ghcr.io/example/demo-web",
            runtime_port=3000,
            health_path="/healthz",
            preview_base_url="HTTPS://Demo-Preview.Example.COM/",
        )

        self.assertEqual(intent.preview_base_url, "https://demo-preview.example.com")


if __name__ == "__main__":
    unittest.main()
