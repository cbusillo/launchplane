from __future__ import annotations

import unittest

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.product_onboarding_manifest import ProductOnboardingManifest
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.secret_record import SecretBinding
from control_plane.product_onboarding_service import build_product_onboarding_service_result
from control_plane.storage.product_authority_bundle import ProductAuthorityBundle
from control_plane.workflows.product_onboarding import apply_product_onboarding_manifest


class _ProductOnboardingStore:
    def __init__(self) -> None:
        self.product_profiles: list[LaunchplaneProductProfileRecord] = []
        self.dokploy_targets: list[DokployTargetRecord] = []
        self.dokploy_target_ids: list[DokployTargetIdRecord] = []
        self.provider_targets: list[ProviderTargetRecord] = []
        self.runtime_environments: list[RuntimeEnvironmentRecord] = []
        self.secret_bindings: list[SecretBinding] = []

    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord:
        for record in reversed(self.product_profiles):
            if record.product == product:
                return record
        raise KeyError(product)

    def write_product_profile_record(self, record: LaunchplaneProductProfileRecord) -> None:
        self.product_profiles.append(record)

    def write_dokploy_target_record(self, record: DokployTargetRecord) -> None:
        self.dokploy_targets.append(record)

    def write_dokploy_target_id_record(self, record: DokployTargetIdRecord) -> None:
        self.dokploy_target_ids.append(record)

    def list_physical_provider_target_records(self) -> tuple[ProviderTargetRecord, ...]:
        return tuple(self.provider_targets)

    def write_provider_target_record(self, record: ProviderTargetRecord) -> None:
        self.provider_targets.append(record)

    def write_runtime_environment_record(self, record: RuntimeEnvironmentRecord) -> None:
        self.runtime_environments.append(record)

    def list_runtime_environment_records(
        self,
        *,
        scope: str = "",
        context_name: str = "",
        instance_name: str = "",
    ) -> tuple[RuntimeEnvironmentRecord, ...]:
        return tuple(
            record
            for record in self.runtime_environments
            if (not scope or record.scope == scope)
            and (not context_name or record.context == context_name)
            and (not instance_name or record.instance == instance_name)
        )

    def list_secret_bindings(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[SecretBinding, ...]:
        bindings = tuple(
            binding
            for binding in self.secret_bindings
            if (not integration or binding.integration == integration)
            and (not context_name or binding.context == context_name)
            and (not instance_name or binding.instance == instance_name)
        )
        return bindings[:limit] if limit is not None else bindings

    def write_secret_binding(self, binding: SecretBinding) -> None:
        self.secret_bindings.append(binding)

    def write_product_authority_bundle(self, bundle: ProductAuthorityBundle) -> None:
        for profile_record in bundle.product_profiles:
            self.write_product_profile_record(profile_record)
        for target_record in bundle.dokploy_targets:
            self.write_dokploy_target_record(target_record)
        for target_id_record in bundle.dokploy_target_ids:
            self.write_dokploy_target_id_record(target_id_record)
        for provider_target_write in bundle.provider_target_writes:
            self.write_provider_target_record(provider_target_write.record)
        for runtime_record in bundle.runtime_environments:
            self.write_runtime_environment_record(runtime_record)
        for binding in bundle.secret_bindings:
            self.write_secret_binding(binding)


class ProductOnboardingServiceTests(unittest.TestCase):
    def test_service_result_summarizes_records_without_secret_ids(self) -> None:
        manifest = ProductOnboardingManifest.model_validate(
            {
                "product": "discord-blue",
                "display_name": "Discord Blue",
                "repository": "cbusillo/discord-blue",
                "driver_id": "generic-web",
                "image_repository": "ghcr.io/cbusillo/discord-blue",
                "runtime_port": 8787,
                "health_path": "/health",
                "lanes": [
                    {
                        "instance": "prod",
                        "context": "discord-blue",
                        "base_url": "https://discord-blue.example.test",
                    }
                ],
                "provider_targets": [
                    {
                        "context": "discord-blue",
                        "instance": "prod",
                        "target_id": "app-discord-blue",
                        "target_type": "application",
                        "target_name": "discord-blue",
                        "env": {"DISCORD_BLUE_TARGET_ENV": "stored-only"},
                        "domains": ["discord-blue.example.test"],
                        "healthcheck_enabled": False,
                    }
                ],
                "runtime_environments": [
                    {
                        "scope": "instance",
                        "context": "discord-blue",
                        "instance": "prod",
                        "env": {"DISCORD_BLUE_STATE_DIR": "/var/lib/discord-blue"},
                    }
                ],
                "secret_bindings": [
                    {
                        "binding_key": "DISCORD_TOKEN",
                        "context": "discord-blue",
                        "instance": "prod",
                    }
                ],
                "updated_at": "2026-05-04T18:00:00Z",
                "source_label": "test:discord-blue-onboarding",
            }
        )
        store = _ProductOnboardingStore()
        onboarding_result = apply_product_onboarding_manifest(record_store=store, manifest=manifest)

        result, driver_result = build_product_onboarding_service_result(onboarding_result)

        self.assertEqual(result["product_profile"], "discord-blue")
        self.assertEqual(result["provider_target_count"], 1)
        self.assertEqual(result["provider_target_id_count"], 1)
        self.assertNotIn("dokploy_target_count", result)
        self.assertNotIn("dokploy_target_id_count", result)
        self.assertEqual(result["runtime_environment_record_count"], 1)
        self.assertEqual(result["secret_binding_count"], 1)
        self.assertEqual(len(store.provider_targets), 1)
        self.assertEqual(driver_result["product"], "discord-blue")
        self.assertEqual(
            driver_result["product_profile"],
            {
                "product": "discord-blue",
                "updated_at": "2026-05-04T18:00:00Z",
            },
        )
        self.assertEqual(
            driver_result["provider_targets"],
            [
                {
                    "context": "discord-blue",
                    "instance": "prod",
                    "target_type": "application",
                    "target_name": "discord-blue",
                    "target_id_recorded": True,
                    "updated_at": "2026-05-04T18:00:00Z",
                }
            ],
        )
        self.assertIn("provider_targets", driver_result)
        self.assertIn("provider_target_ids", driver_result)
        self.assertNotIn("dokploy_targets", driver_result)
        self.assertNotIn("dokploy_target_ids", driver_result)
        self.assertNotIn("runtime_environment_records", driver_result)
        self.assertNotIn("secret_bindings", driver_result)
        self.assertNotIn("secret_id", str(driver_result))
        self.assertNotIn("app-discord-blue", str(driver_result))
        self.assertNotIn("DISCORD_BLUE_TARGET_ENV", str(driver_result))
        self.assertNotIn("discord-blue.example.test", str(driver_result))
        self.assertNotIn("test:discord-blue-onboarding", str(driver_result))


if __name__ == "__main__":
    unittest.main()
