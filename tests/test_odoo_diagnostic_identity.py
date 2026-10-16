import json
import os
import unittest
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, UTC
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import patch

from cryptography.fernet import Fernet

from control_plane import odoo_diagnostic_identity as diagnostic
from control_plane import odoo_runtime_reads as reads
from control_plane import odoo_business_hours as hours
from control_plane import odoo_import_diagnostics as imports
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.secrets import SecretWriteStore, write_secret_value
from control_plane.storage.filesystem import FilesystemRecordStore


class DiagnosticIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = FilesystemRecordStore(state_dir=Path(temp.name))
        encryption = patch.dict(
            os.environ, {"LAUNCHPLANE_MASTER_ENCRYPTION_KEY": Fernet.generate_key().decode()}
        )
        encryption.start()
        self.addCleanup(encryption.stop)
        self.runtime = RuntimeIdentity(
            product="example-site",
            context="example",
            instance="testing",
            deployment_record_id="deployment",
            artifact_id="artifact",
            source_git_ref="revision",
        )
        self.payload = {
            "product": self.runtime.product,
            "context": self.runtime.context,
            "instance": self.runtime.instance,
            "database": "example_db",
            "login": "service-diagnostic",
            "password": "synthetic-diagnostic-password",
            "user_id": 17,
        }
        self.write_identity(self.payload)
        self.record = self.store.list_secret_records()[0]
        self.binding = self.store.list_secret_bindings()[0]
        self.version = self.store.read_secret_version(self.record.current_version_id)

    def write_identity(self, payload: dict[str, object]) -> None:
        write_secret_value(
            record_store=cast(SecretWriteStore, self.store),
            scope="context_instance",
            integration=diagnostic.DIAGNOSTIC_SECRET_INTEGRATION,
            binding_key=diagnostic.DIAGNOSTIC_IDENTITY_KEY,
            name="service-reader",
            context_name=self.runtime.context,
            instance_name=self.runtime.instance,
            plaintext_value=json.dumps(payload),
        )

    def read(self) -> diagnostic.OdooDiagnosticIdentity:
        return diagnostic.read_diagnostic_identity(self.store, self.runtime, "example_db")

    def test_reads_existing_encrypted_lane_record_without_runtime_delivery_or_writes(self) -> None:
        before = tuple(self.store.list_secret_audit_events(secret_id=self.record.secret_id))
        selected = self.read()
        self.assertEqual(selected.login, self.payload["login"])
        self.assertEqual(selected.password.get_secret_value(), self.payload["password"])
        self.assertEqual(selected.user_id, self.payload["user_id"])
        self.assertNotIn(str(self.payload["password"]), repr(selected))
        self.assertNotIn(str(self.payload["password"]), self.version.ciphertext)
        self.assertEqual(
            tuple(self.store.list_secret_audit_events(secret_id=self.record.secret_id)), before
        )

    def test_inherited_foreign_disabled_and_ambiguous_records_cannot_supply_identity(self) -> None:
        changes: tuple[dict[str, object], ...] = (
            {"scope": "global", "context": "", "instance": ""},
            {"scope": "context", "instance": ""},
            {"context": "another-site"},
            {"instance": "prod"},
            {"integration": "runtime_environment"},
            {"status": "disabled"},
        )
        for change in changes:
            with (
                self.subTest(change=change),
                patch.object(
                    self.store,
                    "list_secret_records",
                    return_value=(self.record.model_copy(update=change),),
                ),
                patch.object(diagnostic, "_decrypt_secret_value") as decrypt,
            ):
                with self.assertRaises(ValueError):
                    self.read()
                decrypt.assert_not_called()
        for binding_change in (
            {"context": "another-site"},
            {"instance": "prod"},
            {"integration": "odoo_walkthrough"},
            {"status": "disabled"},
            {"binding_key": "ODOO_ADMIN_PASSWORD"},
            {"secret_id": "another-secret"},
        ):
            with (
                self.subTest(change=binding_change),
                patch.object(
                    self.store,
                    "list_secret_bindings",
                    return_value=(self.binding.model_copy(update=binding_change),),
                ),
                patch.object(diagnostic, "_decrypt_secret_value") as decrypt,
            ):
                with self.assertRaises(ValueError):
                    self.read()
                decrypt.assert_not_called()
        duplicate = self.binding.model_copy(update={"binding_id": "duplicate"})
        with patch.object(
            self.store, "list_secret_bindings", return_value=(self.binding, duplicate)
        ):
            with self.assertRaises(ValueError):
                self.read()

    def test_payload_must_match_lane_database_and_service_account(self) -> None:
        for change in (
            {"product": "other"},
            {"context": "other"},
            {"instance": "prod"},
            {"database": "production_db"},
            {"password": ""},
            {"login": ""},
            {"login": "admin"},
            {"user_id": 1},
            {"user_id": True},
        ):
            with self.subTest(change=change):
                self.write_identity({**self.payload, **change})
                with self.assertRaises(ValueError) as caught:
                    self.read()
                self.assertNotIn(str(self.payload["password"]), str(caught.exception))
                self.assertTrue(caught.exception.__suppress_context__)

    def test_wrong_version_and_decryption_failure_are_unavailable_and_private(self) -> None:
        for version in (
            self.version.model_copy(update={"secret_id": "wrong"}),
            self.version.model_copy(update={"version_id": "wrong"}),
            self.version.model_copy(update={"ciphertext": "broken"}),
        ):
            with (
                self.subTest(version=version.version_id),
                patch.object(self.store, "read_secret_version", return_value=version),
            ):
                with self.assertRaises(ValueError) as caught:
                    self.read()
                self.assertNotIn(version.ciphertext, str(caught.exception))

    def connection(self) -> reads.OdooRuntimeConnection:
        selection = reads.OdooRuntimeSelection(self.runtime, "https://example.invalid")
        return reads.OdooRuntimeConnection(
            selection=selection,
            evidence=reads.OdooRuntimeEvidence(
                identity=self.runtime,
                target_id="target",
                target_name="target",
                container_id="container",
                observed_at="2026-10-16T00:00:00Z",
            ),
            host="https://provider.invalid",
            token="inert",
            server_id="server",
            app_name="app",
            credentials={
                "ODOO_DB_NAME": "example_db",
                "ODOO_ADMIN_LOGIN": "person",
                "ODOO_ADMIN_PASSWORD": "bootstrap-password",
            },
            diagnostic_identity=self.read(),
        )

    def test_missing_identity_refuses_before_authentication_and_uid_mismatch_is_not_accepted(
        self,
    ) -> None:
        connection = self.connection()
        self.assertEqual(
            reads.diagnostic_authentication(connection)["login"], self.payload["login"]
        )
        for identity in (None, self.read().model_copy(update={"instance": "prod"})):
            with self.assertRaises(reads.OdooRuntimeReadError):
                reads.diagnostic_authentication(replace(connection, diagnostic_identity=identity))
        reads.require_diagnostic_user(connection, {"uid": self.payload["user_id"]})
        for authenticated in ({"uid": 2}, {"uid": True}, {}, None):
            with self.assertRaises(reads.OdooRuntimeReadError):
                reads.require_diagnostic_user(connection, authenticated)

    def test_all_readers_refuse_missing_identity_and_wrong_user_before_model_reads(self) -> None:
        connection = self.connection()
        now = datetime.now(UTC)
        query = reads.OutgoingEmailQuery(
            subject="TEST diagnostic", recipient="test@example.invalid", created_after=now
        )
        for reader in (
            lambda value: reads.read_outgoing_email(value, query),
            lambda value: hours.read_company_calendar(value, company_id=7, observed_at=now),
            imports.read_applied_parameters,
        ):
            for missing in (True, False):
                with self.subTest(reader=reader, missing=missing), ExitStack() as stack:
                    calls: list[str] = []

                    def rpc(
                        opener: object, origin: str, path: str, *args: object, **kwargs: object
                    ) -> object:
                        calls.append(path)
                        if path.endswith("authenticate"):
                            return {"uid": 999}
                        if path.endswith("destroy"):
                            return None
                        self.fail("A mismatched account must never read diagnostic models")

                    for module in (reads, hours, imports):
                        stack.enter_context(
                            patch.object(module, "odoo_compose_has_domain", return_value=True)
                        )
                    for module in (reads, hours):
                        stack.enter_context(patch.object(module, "_rpc", side_effect=rpc))
                        stack.enter_context(
                            patch.object(
                                module,
                                "_http_json",
                                return_value={"runtime_identity": self.runtime.model_dump()},
                            )
                        )
                    selected = (
                        replace(connection, diagnostic_identity=None) if missing else connection
                    )
                    with self.assertRaises(reads.OdooRuntimeReadError):
                        reader(selected)
                    if missing:
                        self.assertEqual(calls, [])
                    else:
                        self.assertTrue(calls[0].endswith("authenticate"))
                        self.assertTrue(calls[-1].endswith("destroy"))

    def test_production_retains_its_configured_login_and_default_without_loading_identity(
        self,
    ) -> None:
        connection = self.connection()
        production = replace(
            connection,
            selection=replace(
                connection.selection, identity=self.runtime.model_copy(update={"instance": "prod"})
            ),
            diagnostic_identity=None,
        )
        self.assertEqual(
            reads.diagnostic_authentication(production),
            {
                "db": "example_db",
                "login": "person",
                "password": "bootstrap-password",
            },
        )
        production.credentials["ODOO_ADMIN_LOGIN"] = ""
        self.assertEqual(reads.diagnostic_authentication(production)["login"], "admin")
        reads.require_diagnostic_user(production, {"uid": 2})
