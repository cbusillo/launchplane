import json
import unittest

from cryptography.fernet import Fernet

from control_plane.cli import _build_launchplane_service_runtime_contract
from control_plane.cli import _launchplane_service_preflight_blockers
from control_plane.cli import _launchplane_service_preflight_warnings


class LaunchplaneServicePreflightTests(unittest.TestCase):
    _DATABASE_URL = "postgresql+psycopg://launchplane:test@db.internal:5432/launchplane"
    _MANAGED_STORE_STATUS = {
        "inspectable": True,
        "error": "",
        "dokploy_secret_bindings": {"DOKPLOY_HOST": True, "DOKPLOY_TOKEN": True},
        "authz_policy_record_count": 1,
        "dokploy_target_id_record_count": 1,
        "runtime_environment_record_count": 1,
    }

    @staticmethod
    def _canonical_key_ring() -> str:
        return json.dumps(
            {
                "active_key_id": "root-2026-08",
                "keys": {"root-2026-08": Fernet.generate_key().decode("ascii")},
            },
            separators=(",", ":"),
        )

    def _runtime_contract(self, env_map: dict[str, str]) -> dict[str, object]:
        return _build_launchplane_service_runtime_contract(
            env_map=env_map,
            database_url=self._DATABASE_URL,
            database_scheme="postgresql+psycopg",
            database_host="db.internal",
            managed_store_status=self._MANAGED_STORE_STATUS,
        )

    @staticmethod
    def _blockers(runtime_contract: dict[str, object]) -> list[str]:
        return _launchplane_service_preflight_blockers(
            target_type="application",
            source_type="docker",
            custom_git_url="",
            custom_git_branch="",
            custom_git_ssh_key_id="",
            compose_path="",
            git_access_mode="",
            runtime_contract=runtime_contract,
        )

    def test_canonical_only_configuration_passes_preflight(self) -> None:
        canonical_key_ring = self._canonical_key_ring()
        runtime_contract = self._runtime_contract(
            {
                "DOCKER_IMAGE_REFERENCE": "ghcr.io/cbusillo/launchplane@sha256:test",
                "LAUNCHPLANE_SECRET_KEYS_JSON": canonical_key_ring,
                "LAUNCHPLANE_POLICY_B64": "dGVzdA==",
            }
        )

        self.assertTrue(runtime_contract["secret_key_configuration_valid"])
        self.assertFalse(runtime_contract["master_encryption_key_present"])
        self.assertEqual(self._blockers(runtime_contract), [])
        self.assertEqual(
            _launchplane_service_preflight_warnings(
                compose_status="success",
                runtime_contract=runtime_contract,
            ),
            [],
        )
        self.assertNotIn(canonical_key_ring, json.dumps(runtime_contract))

    def test_legacy_only_configuration_passes_with_migration_warning(self) -> None:
        runtime_contract = self._runtime_contract(
            {
                "LAUNCHPLANE_MASTER_ENCRYPTION_KEY": "test-key",
                "LAUNCHPLANE_POLICY_B64": "dGVzdA==",
            }
        )

        self.assertTrue(runtime_contract["secret_key_configuration_valid"])
        self.assertEqual(self._blockers(runtime_contract), [])
        self.assertIn(
            "migration-only legacy managed-secret root",
            " ".join(
                _launchplane_service_preflight_warnings(
                    compose_status="running",
                    runtime_contract=runtime_contract,
                )
            ),
        )

    def test_malformed_canonical_configuration_fails_preflight(self) -> None:
        runtime_contract = self._runtime_contract(
            {
                "LAUNCHPLANE_SECRET_KEYS_JSON": "not-json",
                "LAUNCHPLANE_POLICY_B64": "dGVzdA==",
            }
        )

        self.assertFalse(runtime_contract["secret_key_configuration_valid"])
        blockers = " ".join(self._blockers(runtime_contract))
        self.assertIn("Invalid LAUNCHPLANE_SECRET_KEYS_JSON", blockers)
        self.assertNotIn("not-json", blockers)

    def test_dual_root_mismatch_fails_preflight_without_exposing_values(self) -> None:
        legacy_key = Fernet.generate_key().decode("ascii")
        mismatched_key = Fernet.generate_key().decode("ascii")
        runtime_contract = self._runtime_contract(
            {
                "LAUNCHPLANE_SECRET_KEYS_JSON": json.dumps(
                    {
                        "active_key_id": "root-2026-08",
                        "keys": {
                            "root-2026-08": Fernet.generate_key().decode("ascii"),
                            "launchplane-master-key": mismatched_key,
                        },
                    },
                    separators=(",", ":"),
                ),
                "LAUNCHPLANE_MASTER_ENCRYPTION_KEY": legacy_key,
                "LAUNCHPLANE_POLICY_B64": "dGVzdA==",
            }
        )

        self.assertFalse(runtime_contract["secret_key_configuration_valid"])
        blockers = " ".join(self._blockers(runtime_contract))
        self.assertIn("does not match LAUNCHPLANE_MASTER_ENCRYPTION_KEY", blockers)
        self.assertNotIn(legacy_key, blockers)
        self.assertNotIn(mismatched_key, blockers)

    def test_missing_secret_root_fails_preflight(self) -> None:
        runtime_contract = self._runtime_contract({"LAUNCHPLANE_POLICY_B64": "dGVzdA=="})

        self.assertFalse(runtime_contract["secret_key_configuration_valid"])
        blockers = " ".join(self._blockers(runtime_contract))
        self.assertIn("Launchplane managed secrets require", blockers)


if __name__ == "__main__":
    unittest.main()
