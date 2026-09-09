from __future__ import annotations

import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from control_plane.contracts.ordinary_agent import OrdinaryAgentTarget
from control_plane.contracts.ordinary_agent_enrollment import OrdinaryAgentPolicyBinding
from control_plane.contracts.ordinary_agent_lifecycle import OrdinaryAgentManagedSecretBinding
from control_plane.contracts.repository_inventory import RepositoryInventoryRecord
from control_plane.contracts.secret_record import SecretBinding, SecretRecord, SecretVersion
from control_plane.github_app_identity import GitHubAppIdentityError
from control_plane.ordinary_agent_custody import OrdinaryAgentCustodyError
from control_plane.ordinary_agent_custody_enrollment import (
    ProviderInspectedCustodyCandidate,
    build_provider_inspected_custody_candidate,
)
from control_plane.storage.postgres import PostgresRecordStore


class OrdinaryAgentCustodyEnrollmentTests(unittest.TestCase):
    private_key: str

    @classmethod
    def setUpClass(cls) -> None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.private_key = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()

    def setUp(self) -> None:
        self.store = PostgresRecordStore(database_url="sqlite+pysqlite:///:memory:")
        self.store.ensure_schema()
        self.store.write_secret_record(
            SecretRecord(
                secret_id="secret-app",
                scope="global",
                integration="ordinary_agent_github_app",
                name="ordinary agent GitHub App private key",
                current_version_id="version-1",
                created_at="2026-09-08T12:00:00Z",
                updated_at="2026-09-08T12:00:00Z",
            )
        )
        self.store.write_secret_version(
            SecretVersion(
                version_id="version-1",
                secret_id="secret-app",
                created_at="2026-09-08T12:00:00Z",
                ciphertext="encrypted-private-key",
            )
        )
        self.store.write_secret_binding(
            SecretBinding(
                binding_id="binding-app",
                secret_id="secret-app",
                integration="ordinary_agent_github_app",
                binding_key="private_key",
                created_at="2026-09-08T12:00:00Z",
                updated_at="2026-09-08T12:00:00Z",
            )
        )
        self.decrypt = patch(
            "control_plane.ordinary_agent_custody_enrollment."
            "control_plane_secrets._decrypt_secret_value",
            return_value=self.private_key,
        )
        self.decrypt.start()

    def tearDown(self) -> None:
        self.decrypt.stop()
        self.store.close()

    def test_builds_exact_candidate_from_read_only_provider_inspection(self) -> None:
        calls: list[dict[str, object]] = []

        def api_request(**kwargs: object) -> object:
            calls.append(dict(kwargs))
            if kwargs["path"] == "/app":
                return {"id": 42}
            return {
                "id": 77,
                "app_id": 42,
                "account": {"id": 456, "login": "example"},
                "permissions": {
                    "contents": "write",
                    "metadata": "read",
                    "pull_requests": "write",
                },
            }

        result = build_provider_inspected_custody_candidate(
            record_store=self.store,
            principal_id="agent_one",
            policy=self._policy(),
            repository_inventory=self._inventory(),
            managed_secret=self._managed_secret(),
            github_app_id=42,
            valid_from=1_789_000_000,
            expires_at=1_789_003_600,
            api_request=api_request,
        )

        candidate = result.candidate
        self.assertEqual(candidate.candidate_kind, "provider_inspected")
        self.assertEqual(candidate.repository_inventory.record_id, self._inventory().record_id)
        self.assertEqual(candidate.github_app_id, 42)
        self.assertEqual(result.installation_id, 77)
        self.assertEqual(candidate.provider_inspection_sha256, result.provider_inspection_sha256)
        self.assertEqual(
            candidate.effect_profiles,
            ("guarded_merge", "head_refresh", "pr_disposition"),
        )
        self.assertEqual(
            tuple((item.name, item.access) for item in candidate.permissions),
            (("contents", "write"), ("metadata", "read"), ("pull_requests", "write")),
        )
        self.assertEqual(len(calls), 2)
        self.assertTrue(all("method" not in call for call in calls))
        self.assertNotIn(self.private_key, repr(result))

    def test_rejects_inventory_and_secret_drift_before_provider_read(self) -> None:
        calls: list[dict[str, object]] = []
        retired = self._inventory().model_copy(update={"inventory_state": "retired"})
        with self.assertRaisesRegex(OrdinaryAgentCustodyError, "tracked repository"):
            self._build(
                repository_inventory=retired, api_request=lambda **kwargs: calls.append(kwargs)
            )

        stale_secret = self._managed_secret().model_copy(
            update={"secret_version_id": "version-old"}
        )
        with self.assertRaisesRegex(OrdinaryAgentCustodyError, "unavailable"):
            self._build(
                managed_secret=stale_secret, api_request=lambda **kwargs: calls.append(kwargs)
            )
        self.assertEqual(calls, [])

    def test_rejects_provider_permission_expansion(self) -> None:
        def api_request(**kwargs: object) -> object:
            if kwargs["path"] == "/app":
                return {"id": 42}
            return {
                "id": 77,
                "app_id": 42,
                "account": {"id": 456, "login": "example"},
                "permissions": {
                    "contents": "write",
                    "metadata": "read",
                    "pull_requests": "write",
                    "workflows": "write",
                },
            }

        with self.assertRaisesRegex(GitHubAppIdentityError, "beyond"):
            self._build(api_request=api_request)

    def _build(self, **overrides: object) -> ProviderInspectedCustodyCandidate:
        values = {
            "record_store": self.store,
            "principal_id": "agent_one",
            "policy": self._policy(),
            "repository_inventory": self._inventory(),
            "managed_secret": self._managed_secret(),
            "github_app_id": 42,
            "valid_from": 1_789_000_000,
            "expires_at": 1_789_003_600,
        }
        values.update(overrides)
        return build_provider_inspected_custody_candidate(**values)  # type: ignore[arg-type]

    @staticmethod
    def _policy() -> OrdinaryAgentPolicyBinding:
        return OrdinaryAgentPolicyBinding(
            record_id="authz-policy-3",
            revision=3,
            policy_sha256="a" * 64,
            managed_set_id="ordinary-agents",
            managed_rule_id="agent-one",
            target=OrdinaryAgentTarget(
                repository_id=123,
                repository="example/repo",
                base_branch="main",
            ),
        )

    @staticmethod
    def _inventory() -> RepositoryInventoryRecord:
        return RepositoryInventoryRecord(
            repository_id="123",
            repository_owner_id="456",
            repository="example/repo",
            inventory_state="tracked",
            inventory_revision=3,
            recorded_at="2026-09-08T12:00:00Z",
            source="test",
            reason="test",
        )

    @staticmethod
    def _managed_secret() -> OrdinaryAgentManagedSecretBinding:
        return OrdinaryAgentManagedSecretBinding(
            binding_id="binding-app",
            secret_id="secret-app",
            secret_version_id="version-1",
            integration="ordinary_agent_github_app",
            binding_key="private_key",
        )


if __name__ == "__main__":
    unittest.main()
