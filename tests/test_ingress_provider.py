import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import click

from control_plane import secrets
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.secret_record import SecretRecord
from control_plane.npmplus import NpmplusClient
from control_plane.runtime_environments import resolve_site_runtime_environment_from_store
from control_plane.service_runtime_settings import SERVICE_CONTEXT
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows import ingress_provider
from control_plane.workflows.ingress_provider import (
    NPMPLUS_BASE_URL_ENV_KEY,
    NPMPLUS_IDENTITY_ENV_KEY,
    NPMPLUS_SECRET_ENV_KEY,
    NpmplusIngressProvider,
    build_npmplus_ingress_client_from_env,
    default_ingress_provider,
)
from tests.support.stores import sqlite_database_url


class RecordIngressProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = self.enterContext(TemporaryDirectory())
        self.store = PostgresRecordStore(
            database_url=sqlite_database_url(Path(temporary) / "records.sqlite")
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.enterContext(
            patch.dict(
                os.environ,
                {secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-ingress-key-" * 4},
                clear=True,
            )
        )
        self.endpoint = RuntimeEnvironmentRecord(
            scope="context",
            context=SERVICE_CONTEXT,
            env={NPMPLUS_BASE_URL_ENV_KEY: "https://edge.example.test"},
            updated_at="2026-10-28T00:00:00Z",
        )
        self.store.write_runtime_environment_record(self.endpoint)
        for key, value in (
            (NPMPLUS_IDENTITY_ENV_KEY, "synthetic-service-identity"),
            (NPMPLUS_SECRET_ENV_KEY, "synthetic-service-secret"),
        ):
            secrets.write_secret_value(
                record_store=self.store,
                scope="context",
                integration=secrets.LAUNCHPLANE_SERVICE_SECRET_INTEGRATION,
                name=key.lower(),
                plaintext_value=value,
                binding_key=key,
                context_name=SERVICE_CONTEXT,
            )

    def test_record_reader_uses_exact_service_values_and_never_authenticates(self) -> None:
        with patch.object(NpmplusClient, "authenticate") as authenticate:
            result = ingress_provider.build_npmplus_ingress_client_from_records(
                record_store=self.store
            )
        self.assertIsInstance(result, NpmplusClient)
        credentials = result._credentials
        self.assertEqual(credentials.base_url, self.endpoint.env[NPMPLUS_BASE_URL_ENV_KEY])
        self.assertEqual(credentials.identity, "synthetic-service-identity")
        self.assertEqual(credentials.secret, "synthetic-service-secret")
        authenticate.assert_not_called()

    def test_record_reader_refuses_missing_or_ambiguous_endpoint_despite_environment(self) -> None:
        with patch.dict(
            os.environ,
            {
                NPMPLUS_BASE_URL_ENV_KEY: "https://legacy.example.test",
                NPMPLUS_IDENTITY_ENV_KEY: "legacy-identity",
                NPMPLUS_SECRET_ENV_KEY: "legacy-secret",
            },
        ):
            for records in ((), (self.endpoint, self.endpoint)):
                with (
                    self.subTest(records=len(records)),
                    patch.object(
                        self.store, "list_runtime_environment_records", return_value=records
                    ),
                ):
                    with self.assertRaises(click.ClickException):
                        ingress_provider.build_npmplus_ingress_client_from_records(
                            record_store=self.store
                        )

    def test_record_reader_refuses_wrong_endpoint_scope_and_unsafe_urls(self) -> None:
        for updates in (
            {"scope": "global"},
            {"context": "another-service"},
            {"instance": "testing"},
            {"env": {NPMPLUS_BASE_URL_ENV_KEY: 42}},
            {"env": {NPMPLUS_BASE_URL_ENV_KEY: ""}},
            {"env": {NPMPLUS_BASE_URL_ENV_KEY: "https://user:secret@edge.example.test"}},
            {"env": {NPMPLUS_BASE_URL_ENV_KEY: "https://edge.example.test?token=secret"}},
        ):
            with (
                self.subTest(updates=updates),
                patch.object(
                    self.store,
                    "list_runtime_environment_records",
                    return_value=(self.endpoint.model_copy(update=updates),),
                ),
            ):
                with self.assertRaises(click.ClickException):
                    ingress_provider.build_npmplus_ingress_client_from_records(
                        record_store=self.store
                    )

    def test_record_reader_refuses_missing_disabled_or_ambiguous_secret_bindings(self) -> None:
        bindings = self.store.list_secret_bindings()
        identity = next(b for b in bindings if b.binding_key == NPMPLUS_IDENTITY_ENV_KEY)
        for selected in (
            (),
            tuple(b for b in bindings if b != identity),
            tuple(b for b in bindings if b == identity),
            (*bindings, identity.model_copy(update={"binding_id": "duplicate"})),
            tuple(b.model_copy(update={"status": "disabled"}) for b in bindings),
            tuple(b.model_copy(update={"context": "other"}) for b in bindings),
            tuple(b.model_copy(update={"instance": "testing"}) for b in bindings),
            tuple(b.model_copy(update={"integration": "runtime_environment"}) for b in bindings),
        ):
            with (
                self.subTest(bindings=selected),
                patch.object(self.store, "list_secret_bindings", return_value=selected),
            ):
                with self.assertRaises(click.ClickException):
                    ingress_provider.build_npmplus_ingress_client_from_records(
                        record_store=self.store
                    )

    def test_service_credentials_never_enter_product_runtime_render(self) -> None:
        rendered = resolve_site_runtime_environment_from_store(
            record_store=self.store,
            context_name="product-site",
            instance_name="testing",
            target_environment={},
        )
        self.assertNotIn(NPMPLUS_IDENTITY_ENV_KEY, rendered.values)
        self.assertNotIn(NPMPLUS_SECRET_ENV_KEY, rendered.values)

    def test_record_reader_refuses_mismatched_or_disabled_secret_records(self) -> None:
        record = self.store.list_secret_records()[0]
        read = self.store.read_secret_record
        for updates in (
            {"secret_id": "another-secret"},
            {"scope": "global"},
            {"context": "another-service"},
            {"instance": "testing"},
            {"integration": "runtime_environment"},
            {"status": "disabled"},
        ):

            def substituted(secret_id: str) -> SecretRecord:
                return (
                    record.model_copy(update=updates)
                    if secret_id == record.secret_id
                    else read(secret_id)
                )

            with (
                self.subTest(updates=updates),
                patch.object(self.store, "read_secret_record", side_effect=substituted),
            ):
                with self.assertRaises(click.ClickException):
                    ingress_provider.build_npmplus_ingress_client_from_records(
                        record_store=self.store
                    )

    def test_record_reader_refuses_wrong_secret_version_ownership(self) -> None:
        version = self.store.read_secret_version(
            self.store.list_secret_records()[0].current_version_id
        )
        for updates in ({"version_id": "wrong-version"}, {"secret_id": "wrong-secret"}):
            with (
                self.subTest(updates=updates),
                patch.object(
                    self.store,
                    "read_secret_version",
                    return_value=version.model_copy(update=updates),
                ),
            ):
                with self.assertRaises(click.ClickException):
                    ingress_provider.build_npmplus_ingress_client_from_records(
                        record_store=self.store
                    )


class IngressProviderTests(unittest.TestCase):
    def test_build_npmplus_ingress_client_from_env_fails_closed_when_missing_env(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(click.ClickException) as raised:
                build_npmplus_ingress_client_from_env()

        message = str(raised.exception)
        self.assertIn(NPMPLUS_BASE_URL_ENV_KEY, message)
        self.assertIn(NPMPLUS_IDENTITY_ENV_KEY, message)
        self.assertIn(NPMPLUS_SECRET_ENV_KEY, message)

    def test_build_npmplus_ingress_client_from_env_builds_client(self) -> None:
        with patch.dict(
            os.environ,
            {
                NPMPLUS_BASE_URL_ENV_KEY: "https://npmplus.example.test",
                NPMPLUS_IDENTITY_ENV_KEY: "launchplane@example.test",
                NPMPLUS_SECRET_ENV_KEY: "secret",
            },
            clear=True,
        ):
            client = build_npmplus_ingress_client_from_env()

        self.assertIsInstance(client, NpmplusClient)

    def test_default_ingress_provider_wraps_npmplus_provider(self) -> None:
        with patch.dict(
            os.environ,
            {
                NPMPLUS_BASE_URL_ENV_KEY: "https://npmplus.example.test",
                NPMPLUS_IDENTITY_ENV_KEY: "launchplane@example.test",
                NPMPLUS_SECRET_ENV_KEY: "secret",
            },
            clear=True,
        ):
            provider = default_ingress_provider()

        self.assertIsInstance(provider, NpmplusIngressProvider)
        self.assertEqual(provider.provider_id, "npmplus")
        self.assertEqual(provider.delegated_executor, "control-plane.npmplus")


if __name__ == "__main__":
    unittest.main()
