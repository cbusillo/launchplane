import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from cryptography.fernet import Fernet
from httpx2 import Response

from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.npmplus import PREVIEW_DNS_BINDING, PREVIEW_DNS_INTEGRATION
from control_plane.service_auth import (
    BearerIdentityConfig,
    LaunchplaneIdentity,
)
from control_plane.storage.product_authority_bundle import ProductAuthorityBundle
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import _post_product_config_apply
from tests.support.auth import _StubVerifier, _identity, local_operator_policy
from tests.support.stores import sqlite_database_url


class PreviewDnsSecretInputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        directory = self.enterContext(TemporaryDirectory())
        self.store = PostgresRecordStore(
            database_url=sqlite_database_url(Path(directory) / "state.sqlite")
        )
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        self.policy = local_operator_policy(
            actions=("product_config.plan", "product_config.apply"),
            products=("launchplane",),
            contexts=("launchplane",),
            subject="director",
            token_label="test",
        )
        self.policy_record = self.store.seed_authz_policy_if_absent(
            LaunchplaneAuthzPolicyRecord(
                record_id="policy",
                source="test",
                updated_at="2026-11-06T00:00:00Z",
                policy=self.policy,
            )
        )
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_SECRET_KEYS_JSON": json.dumps(
                        {"active_key_id": "test", "keys": {"test": Fernet.generate_key().decode()}}
                    ),
                },
            )
        )
        self.app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=self.policy,
            record_store_factory=lambda: self.store,
            bearer_identity_config=BearerIdentityConfig(
                local_operator_token="operator-token",
                local_operator_subject="director",
                local_operator_token_label="test",
            ),
        )
        self.secret: dict[str, object] = {
            "scope": "context",
            "integration": PREVIEW_DNS_INTEGRATION,
            "name": PREVIEW_DNS_BINDING,
            "binding_key": PREVIEW_DNS_BINDING,
            "value": "fictional-private-dns-token",
        }
        self.payload: dict[str, object] = {
            "schema_version": 1,
            "mode": "dry-run",
            "product": "launchplane",
            "context": "launchplane",
            "reason": "Authorized DNS credential transfer",
            "source_label": "test",
            "secrets": [self.secret],
        }

    async def request(self, payload: dict[str, object], key: str = "") -> Response:
        return await _post_product_config_apply(
            self.app,
            payload,
            authorization="Bearer operator-token",
            idempotency_key=key,
        )

    async def test_first_service_credential_uses_existing_plan_apply_and_replays(self) -> None:
        plan = await self.request(self.payload)
        self.assertEqual(plan.status_code, 202, plan.text)
        self.assertEqual(self.store.list_secret_records(), ())
        self.payload["mode"] = "apply"
        applied = await self.request(self.payload, "dns-entry")
        self.assertEqual(applied.status_code, 202, applied.text)
        replayed = await self.request(self.payload, "dns-entry")
        self.assertEqual(replayed.status_code, 202, replayed.text)
        self.assertTrue(replayed.json()["replayed"])
        records = self.store.list_secret_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].integration, PREVIEW_DNS_INTEGRATION)
        self.assertEqual(len(self.store.list_secret_versions(secret_id=records[0].secret_id)), 1)
        self.assertNotIn("fictional-private-dns-token", plan.text + applied.text + replayed.text)
        self.assertEqual(self.store.list_runtime_environment_records(), ())
        self.assertEqual(self.store.list_product_profile_records(), ())

    async def test_apply_without_matching_plan_writes_nothing(self) -> None:
        self.payload["mode"] = "apply"
        response = await self.request(self.payload, "no-plan")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "matching_dry_run_required")
        self.assertEqual(self.store.list_secret_records(), ())

    async def test_another_key_cannot_overwrite_the_filled_service_slot(self) -> None:
        await self.request(self.payload)
        self.payload["mode"] = "apply"
        applied = await self.request(self.payload, "original-entry")
        self.assertEqual(applied.status_code, 202, applied.text)
        before = self.store.list_secret_records()
        response = await self.request(self.payload, "another-entry")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "service_secret_already_configured")
        self.assertEqual(self.store.list_secret_records(), before)

    async def test_policy_is_rechecked_inside_the_atomic_write(self) -> None:
        await self.request(self.payload)
        self.payload["mode"] = "apply"
        writer = self.store.write_preview_dns_secret_bundle

        def revoke_before_commit(
            bundle: ProductAuthorityBundle, *, identity: LaunchplaneIdentity
        ) -> None:
            result = self.store.compare_and_write_authz_policy_record(
                expected_record=self.policy_record,
                replacement_record=LaunchplaneAuthzPolicyRecord(
                    record_id="revoked",
                    revision=self.policy_record.revision + 1,
                    source="test",
                    updated_at=self.policy_record.updated_at,
                    policy=self.policy.model_copy(update={"local_operators": ()}),
                ),
            )
            self.assertEqual(result.status, "written")
            writer(bundle, identity=identity)

        with patch.object(
            self.store, "write_preview_dns_secret_bundle", side_effect=revoke_before_commit
        ):
            response = await self.request(self.payload, "revoked")
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.store.list_secret_records(), ())
        self.assertNotIn("fictional-private-dns-token", response.text)

    def test_store_refuses_unavailable_policy_without_writing(self) -> None:
        from control_plane.product_config import plan_product_config_authority_bundle
        from control_plane.service_auth import LocalOperatorIdentity
        from control_plane.storage.product_authority_bundle import ServiceIdentityAuthorityError

        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=sqlite_database_url(Path(directory) / "empty.sqlite")
            )
            self.addCleanup(store.close)
            store.ensure_schema()
            _, bundle = plan_product_config_authority_bundle(
                record_store=store,
                payload=self.payload,
                mode="apply",
                actor="test",
                source_label="test",
            )
            with self.assertRaises(ServiceIdentityAuthorityError):
                store.write_preview_dns_secret_bundle(
                    bundle,
                    identity=LocalOperatorIdentity(
                        subject="director",
                        token_label="test",
                        director_agent=True,
                    ),
                )
            self.assertEqual(store.list_secret_records(), ())

    def test_store_refuses_a_mixed_bundle_even_after_http_planning(self) -> None:
        from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
        from control_plane.product_config import plan_product_config_authority_bundle
        from control_plane.service_auth import LocalOperatorIdentity
        from control_plane.storage.product_authority_bundle import ServiceIdentityAuthorityError

        _, bundle = plan_product_config_authority_bundle(
            record_store=self.store,
            payload=self.payload,
            mode="apply",
            actor="test",
            source_label="test",
        )
        bundle = bundle.model_copy(
            update={
                "runtime_environments": (
                    RuntimeEnvironmentRecord(
                        scope="context",
                        context="launchplane",
                        env={"UNRELATED_SETTING": "value"},
                        updated_at=self.policy_record.updated_at,
                        source_label="test",
                    ),
                )
            }
        )
        with self.assertRaises(ServiceIdentityAuthorityError):
            self.store.write_preview_dns_secret_bundle(
                bundle,
                identity=LocalOperatorIdentity(
                    subject="director",
                    token_label="test",
                    director_agent=True,
                ),
            )
        self.assertEqual(self.store.list_secret_records(), ())
        self.assertEqual(self.store.list_runtime_environment_records(), ())

    async def test_racing_entry_cannot_replace_a_committed_secret(self) -> None:
        from control_plane.product_config import plan_product_config_authority_bundle

        await self.request(self.payload)
        self.payload["mode"] = "apply"
        writer = self.store.write_preview_dns_secret_bundle

        def fill_before_commit(
            bundle: ProductAuthorityBundle, *, identity: LaunchplaneIdentity
        ) -> None:
            _, competing = plan_product_config_authority_bundle(
                record_store=self.store,
                payload=self.payload,
                mode="apply",
                actor="test",
                source_label="competing input",
            )
            self.store.write_product_authority_bundle(competing)
            writer(bundle, identity=identity)

        with patch.object(
            self.store, "write_preview_dns_secret_bundle", side_effect=fill_before_commit
        ):
            response = await self.request(self.payload, "racing-entry")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "service_secret_already_configured")
        records = self.store.list_secret_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].updated_by, "test")
        self.assertEqual(len(self.store.list_secret_versions(secret_id=records[0].secret_id)), 1)

    async def test_foreign_purpose_or_mixed_runtime_bundle_cannot_claim_service_context(
        self,
    ) -> None:
        for change in ({"integration": "onboarding_github_app"}, {"binding_key": "other"}):
            with self.subTest(change=change), patch.dict(self.secret, change):
                response = await self.request(self.payload)
                self.assertEqual(response.status_code, 403, response.text)
        self.payload["runtime_env"] = {"SETTING": "value"}
        response = await self.request(self.payload)
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.store.list_secret_records(), ())

    async def test_scoped_agent_cannot_use_director_service_input(self) -> None:
        from tests.support.auth import scoped_agent_transport

        with scoped_agent_transport():
            response = await self.request(self.payload)
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.store.list_secret_records(), ())
