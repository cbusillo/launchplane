from __future__ import annotations

import unittest

from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.runtime_key_safety_policy import RuntimeKeySafetyPolicyRecord
from control_plane.contracts.secret_record import (
    SecretAuditEvent,
    SecretBinding,
    SecretRecord,
    SecretVersion,
)
from control_plane.product_config import ProductConfigError
from control_plane.product_config_service import (
    apply_product_config_service_request,
    product_config_service_error,
)


class _FailingProductConfigStore:
    def list_runtime_environment_records(
        self, *, context_name: str = "", instance_name: str = ""
    ) -> tuple[RuntimeEnvironmentRecord, ...]:
        return ()

    def write_runtime_environment_record(self, record: RuntimeEnvironmentRecord) -> None:
        raise AssertionError("dry-run test should not write runtime environment records")

    def list_runtime_key_safety_policy_records(
        self,
        *,
        status: str = "",
        limit: int | None = None,
    ) -> tuple[RuntimeKeySafetyPolicyRecord, ...]:
        return ()

    def read_secret_record(self, secret_id: str) -> SecretRecord:
        raise FileNotFoundError(secret_id)

    def list_secret_records(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[SecretRecord, ...]:
        return ()

    def read_secret_version(self, version_id: str) -> SecretVersion:
        raise FileNotFoundError(version_id)

    def list_secret_versions(self, *, secret_id: str) -> tuple[SecretVersion, ...]:
        return ()

    def list_secret_bindings(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[SecretBinding, ...]:
        return ()

    def list_secret_audit_events(self, *, secret_id: str) -> tuple[SecretAuditEvent, ...]:
        return ()

    def find_secret_record(
        self,
        *,
        scope: str,
        integration: str,
        name: str,
        context: str = "",
        instance: str = "",
    ) -> SecretRecord | None:
        return None

    def write_secret_record(self, record: SecretRecord) -> None:
        raise AssertionError("dry-run test should not write secret records")

    def write_secret_version(self, version: SecretVersion) -> None:
        raise AssertionError("dry-run test should not write secret versions")

    def write_secret_binding(self, binding: SecretBinding) -> None:
        raise AssertionError("dry-run test should not write secret bindings")

    def write_secret_audit_event(self, event: SecretAuditEvent) -> None:
        raise AssertionError("dry-run test should not write secret audit events")


class ProductConfigServiceTests(unittest.TestCase):
    def test_service_request_maps_domain_error_without_raising(self) -> None:
        driver_result, error = apply_product_config_service_request(
            record_store=_FailingProductConfigStore(),
            payload={
                "schema_version": 1,
                "product": "example-site",
                "context": "example-site-prod",
                "instance": "prod",
                "runtime_env": {"env": {"API_TOKEN": "secret-shaped"}},
            },
            mode="dry-run",
            actor="tester",
            source_label="test",
        )

        self.assertIsNone(driver_result)
        self.assertIsNotNone(error)
        assert error is not None
        self.assertEqual(error.status_code, 400)
        self.assertEqual(error.code, "invalid_request")
        self.assertEqual(
            error.message,
            "Product config request failed validation.",
        )

    def test_runtime_key_safety_unavailable_error_is_service_unavailable(self) -> None:
        error = product_config_service_error(
            ProductConfigError(
                "runtime key safety unavailable", code="runtime_key_safety_unavailable"
            )
        )

        self.assertEqual(error.status_code, 503)
        self.assertEqual(error.code, "runtime_key_safety_unavailable")
        self.assertEqual(
            error.message,
            "Launchplane runtime key-safety policy is unavailable.",
        )

    def test_runtime_key_safety_failed_error_is_bad_request(self) -> None:
        error = product_config_service_error(
            ProductConfigError("unsafe runtime key", code="runtime_key_safety_failed")
        )

        self.assertEqual(error.status_code, 400)
        self.assertEqual(error.code, "runtime_key_safety_failed")
        self.assertEqual(error.message, "Product config runtime key-safety gate failed.")


if __name__ == "__main__":
    unittest.main()
