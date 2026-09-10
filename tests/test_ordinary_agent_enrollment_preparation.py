from __future__ import annotations

import unittest
from control_plane.contracts.ordinary_agent_provider import ordinary_agent_enrollment_permissions
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from control_plane.contracts.repository_inventory import RepositoryInventoryRecord
from control_plane.ordinary_agent_enrollment_preparation import (
    PreparedOrdinaryAgentEnrollmentScope,
    prepare_ordinary_agent_enrollment_scope,
)
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.ordinary_agent_lifecycle import TARGET, setup_ordinary_agent_authority


class OrdinaryAgentEnrollmentPreparationTests(unittest.TestCase):
    def test_resolves_exact_current_records_and_rejects_inventory_retirement(self) -> None:
        store = PostgresRecordStore(database_url="sqlite+pysqlite:///:memory:")
        self.addCleanup(store.close)
        store.ensure_schema()
        policy, inventory = setup_ordinary_agent_authority(store)
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        calls: list[str] = []

        def provider(**kwargs: object) -> object:
            calls.append(str(kwargs["path"]))
            if kwargs["path"] == "/app":
                return {"id": 42}
            return {
                "id": 77,
                "app_id": 42,
                "account": {"id": 912001, "login": "example"},
                "permissions": dict(
                    item.split(":", 1) for item in ordinary_agent_enrollment_permissions()
                ),
            }

        def prepare(
            binding_id: str = "ordinary-agent-app-key-binding",
        ) -> PreparedOrdinaryAgentEnrollmentScope:
            return prepare_ordinary_agent_enrollment_scope(
                store=store,
                policy_record=policy,
                action="enroll",
                principal_id="agent_one",
                target=TARGET,
                github_app_id=42,
                secret_binding_id=binding_id,
                valid_from=1_789_000_000,
                expires_at=1_789_003_600,
                api_request=provider,
            )

        with patch(
            "control_plane.ordinary_agent_custody_enrollment."
            "control_plane_secrets._decrypt_secret_value",
            return_value=pem,
        ):
            prepared = prepare()
            self.assertEqual(prepared.policy.record_id, policy.record_id)
            self.assertEqual(prepared.custody.repository_inventory.record_id, inventory.record_id)
            self.assertIsNone(prepared.principal)
            self.assertNotIn(pem, repr(prepared))
            self.assertEqual(len(calls), 2)
            calls.clear()
            with self.assertRaises(ValueError):
                prepare("missing-binding")
            self.assertEqual(calls, [])

            retired = RepositoryInventoryRecord.model_validate(
                {
                    **inventory.model_dump(
                        exclude={"record_id", "inventory_digest", "inventory_revision"}
                    ),
                    "inventory_revision": 2,
                    "inventory_state": "retired",
                    "supersedes_record_id": inventory.record_id,
                }
            )
            store.write_repository_inventory_record(retired)
            with self.assertRaises(ValueError):
                prepare()
            self.assertEqual(calls, [])
            self.assertIsNone(store.read_current_ordinary_agent_principal(principal_id="agent_one"))
