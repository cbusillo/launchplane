from __future__ import annotations

import unittest
from unittest.mock import patch

from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.secret_record import SecretBinding, SecretRecord, SecretVersion
from control_plane.provider_delivery_inspection_profile import (
    PROVIDER_DELIVERY_INSPECTION_APP_ID_ENV_KEY,
    PROVIDER_DELIVERY_INSPECTION_INTEGRATION,
    PROVIDER_DELIVERY_INSPECTION_PERMISSIONS,
    PROVIDER_DELIVERY_INSPECTION_PROFILE_ID,
    ProviderDeliveryInspectionProfileError,
    resolve_provider_delivery_inspection_profile,
)


class _ProfileStore:
    def __init__(self) -> None:
        self.runtime_records: tuple[RuntimeEnvironmentRecord, ...] = (
            RuntimeEnvironmentRecord(
                scope="context",
                context="launchplane",
                env={
                    PROVIDER_DELIVERY_INSPECTION_APP_ID_ENV_KEY: 42,
                    "UNRELATED": "preserved",
                },
                updated_at="2026-09-11T20:00:00Z",
            ),
        )
        self.secret_records: tuple[SecretRecord, ...] = (
            SecretRecord(
                secret_id="inspection-app-key",
                scope="context",
                context="launchplane",
                integration=PROVIDER_DELIVERY_INSPECTION_INTEGRATION,
                name="provider inspection App private key",
                current_version_id="inspection-app-key-v1",
                created_at="2026-09-11T20:00:00Z",
                updated_at="2026-09-11T20:00:00Z",
            ),
        )
        self.secret_versions = {
            "inspection-app-key-v1": SecretVersion(
                version_id="inspection-app-key-v1",
                secret_id="inspection-app-key",
                created_at="2026-09-11T20:00:00Z",
                ciphertext="encrypted-private-key",
            )
        }
        self.secret_bindings: tuple[SecretBinding, ...] = (
            SecretBinding(
                binding_id="inspection-app-binding",
                secret_id="inspection-app-key",
                integration=PROVIDER_DELIVERY_INSPECTION_INTEGRATION,
                binding_key="private_key",
                context="launchplane",
                created_at="2026-09-11T20:00:00Z",
                updated_at="2026-09-11T20:00:00Z",
            ),
        )

    def list_runtime_environment_records(
        self, **kwargs: object
    ) -> tuple[RuntimeEnvironmentRecord, ...]:
        self.runtime_query = kwargs
        return self.runtime_records

    def list_secret_records(self, **kwargs: object) -> tuple[SecretRecord, ...]:
        self.secret_record_query = kwargs
        return self.secret_records

    def read_secret_version(self, version_id: str) -> SecretVersion:
        try:
            return self.secret_versions[version_id]
        except KeyError as error:
            raise FileNotFoundError(version_id) from error

    def list_secret_bindings(self, **kwargs: object) -> tuple[SecretBinding, ...]:
        self.secret_binding_query = kwargs
        return self.secret_bindings


class ProviderDeliveryInspectionProfileTests(unittest.TestCase):
    @patch(
        "control_plane.provider_delivery_inspection_profile."
        "control_plane_secrets._decrypt_secret_value",
        return_value="private-key-value",
    )
    def test_resolves_exact_db_profile_without_exposing_private_key(self, _: object) -> None:
        store = _ProfileStore()

        profile = resolve_provider_delivery_inspection_profile(record_store=store)

        self.assertEqual(profile.profile_id, PROVIDER_DELIVERY_INSPECTION_PROFILE_ID)
        self.assertEqual(profile.app_id, 42)
        self.assertEqual(profile.identity.app_id, 42)
        self.assertEqual(profile.permissions, PROVIDER_DELIVERY_INSPECTION_PERMISSIONS)
        self.assertEqual(profile.secret_version_id, "inspection-app-key-v1")
        self.assertNotIn("private-key-value", repr(profile))
        self.assertEqual(
            store.runtime_query,
            {"scope": "context", "context_name": "launchplane", "instance_name": ""},
        )
        self.assertEqual(store.secret_record_query["limit"], None)
        self.assertEqual(store.secret_binding_query["limit"], None)

    @patch(
        "control_plane.provider_delivery_inspection_profile."
        "control_plane_secrets._decrypt_secret_value",
        return_value="private-key-value",
    )
    def test_profile_digest_tracks_selected_binding_not_unrelated_runtime_values(
        self, _: object
    ) -> None:
        store = _ProfileStore()
        first = resolve_provider_delivery_inspection_profile(record_store=store)
        runtime = store.runtime_records[0]
        store.runtime_records = (
            runtime.model_copy(
                update={
                    "env": {
                        **runtime.env,
                        "UNRELATED": "changed",
                    },
                    "updated_at": "2026-09-11T20:01:00Z",
                }
            ),
        )
        unchanged = resolve_provider_delivery_inspection_profile(record_store=store)
        self.assertEqual(first.profile_sha256, unchanged.profile_sha256)

        record = store.secret_records[0]
        store.secret_records = (
            record.model_copy(update={"current_version_id": "inspection-app-key-v2"}),
        )
        store.secret_versions["inspection-app-key-v2"] = store.secret_versions[
            "inspection-app-key-v1"
        ].model_copy(update={"version_id": "inspection-app-key-v2"})
        rotated = resolve_provider_delivery_inspection_profile(record_store=store)
        self.assertNotEqual(first.profile_sha256, rotated.profile_sha256)

    def test_rejects_missing_ambiguous_or_inexact_profile_before_decryption(self) -> None:
        store = _ProfileStore()
        store.runtime_records = ()
        with self.assertRaisesRegex(ProviderDeliveryInspectionProfileError, "runtime record"):
            resolve_provider_delivery_inspection_profile(record_store=store)

        store = _ProfileStore()
        store.runtime_records = (
            store.runtime_records[0].model_copy(
                update={"env": {PROVIDER_DELIVERY_INSPECTION_APP_ID_ENV_KEY: True}}
            ),
        )
        with self.assertRaisesRegex(ProviderDeliveryInspectionProfileError, "App id"):
            resolve_provider_delivery_inspection_profile(record_store=store)

        store = _ProfileStore()
        store.secret_bindings = (*store.secret_bindings, store.secret_bindings[0])
        with self.assertRaisesRegex(ProviderDeliveryInspectionProfileError, "one exact"):
            resolve_provider_delivery_inspection_profile(record_store=store)

    def test_transient_store_error_is_not_reclassified_as_durable_absence(self) -> None:
        class _FailingStore(_ProfileStore):
            def list_runtime_environment_records(
                self, **kwargs: object
            ) -> tuple[RuntimeEnvironmentRecord, ...]:
                raise RuntimeError("database temporarily unavailable")

        with self.assertRaisesRegex(RuntimeError, "temporarily unavailable"):
            resolve_provider_delivery_inspection_profile(record_store=_FailingStore())


if __name__ == "__main__":
    unittest.main()
