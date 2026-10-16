import json
import os
import unittest
from copy import deepcopy
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, UTC
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import patch

from cryptography.fernet import Fernet
from typing import Any

from control_plane import odoo_diagnostic_identity as diagnostic
from control_plane import odoo_runtime_reads as reads
from control_plane import odoo_business_hours as hours
from control_plane import odoo_import_diagnostics as imports
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.secrets import SecretWriteStore, write_secret_value
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import LaunchplaneAuthzPolicy, BearerIdentityConfig
from tests.support.auth import _identity, _StubVerifier
from tests.http_app_test_support import (
    _post_product_config_apply,
    _github_oauth_config,
    _github_human_identity,
    _browser_mutation_headers,
)
from control_plane.service_human_auth import HumanSessionManager, InMemoryHumanSessionStore
from control_plane.storage.product_authority_bundle import ProductAuthorityBundle


class DiagnosticIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = FilesystemRecordStore(state_dir=Path(temp.name))
        encryption = patch.dict(
            os.environ,
            {
                "LAUNCHPLANE_MASTER_ENCRYPTION_KEY": Fernet.generate_key().decode(),
                "LAUNCHPLANE_SECRET_KEYS_JSON": "",
            },
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
            name=diagnostic.diagnostic_lane_secret_name(self.runtime.instance),
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

    def test_fixed_lane_name_keeps_rotation_recoverable_after_a_stray_record(self) -> None:
        write_secret_value(
            record_store=cast(SecretWriteStore, self.store),
            scope="context_instance",
            integration=diagnostic.DIAGNOSTIC_SECRET_INTEGRATION,
            binding_key=diagnostic.DIAGNOSTIC_IDENTITY_KEY,
            name="mistyped-reader-name",
            context_name=self.runtime.context,
            instance_name=self.runtime.instance,
            plaintext_value=json.dumps(self.payload),
        )
        rotated = {**self.payload, "password": "synthetic-rotated-password"}
        self.write_identity(rotated)
        self.assertEqual(self.read().password.get_secret_value(), rotated["password"])

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
            for outcome in (
                "missing",
                "other_user",
                "failed_login",
                "login_error",
                "transport_error",
                "logout_error",
            ):
                with self.subTest(reader=reader, outcome=outcome), ExitStack() as stack:
                    calls: list[str] = []
                    transport = reads.OdooRuntimeReadError(
                        "odoo_read_unavailable", "Transport unavailable"
                    )

                    def rpc(
                        opener: object, origin: str, path: str, *args: object, **kwargs: object
                    ) -> object:
                        calls.append(path)
                        if path.endswith("authenticate"):
                            if outcome == "login_error":
                                raise reads.OdooRuntimeReadError(
                                    reads.ODOO_READ_REJECTED, "Private input"
                                )
                            if outcome == "transport_error":
                                raise transport
                            return {"uid": False if outcome == "failed_login" else 999}
                        if path.endswith("destroy"):
                            if outcome == "failed_login":
                                raise reads.OdooRuntimeReadError("odoo_read_rejected", "No session")
                            if outcome == "logout_error":
                                raise transport
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
                        replace(connection, diagnostic_identity=None)
                        if outcome == "missing"
                        else connection
                    )
                    with self.assertRaises(reads.OdooRuntimeReadError) as caught:
                        reader(selected)
                    self.assertEqual(
                        caught.exception.code,
                        transport.code
                        if outcome == "transport_error"
                        else diagnostic.DIAGNOSTIC_IDENTITY_UNAVAILABLE,
                    )
                    if outcome == "missing":
                        self.assertEqual(calls, [])
                    elif outcome in {"failed_login", "login_error", "transport_error"}:
                        self.assertEqual(len(calls), 1)
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
        with self.assertRaises(reads.OdooRuntimeReadError) as rejected:
            reads.require_diagnostic_user(production, {"uid": False})
        self.assertEqual(rejected.exception.code, "odoo_authentication_failed")
        production.credentials["ODOO_ADMIN_PASSWORD"] = ""
        with self.assertRaises(reads.OdooRuntimeReadError) as missing:
            reads.diagnostic_authentication(production)
        self.assertEqual(missing.exception.code, "odoo_credentials_missing")


class PreviewDiagnosticConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_supported_preview_secret_write_readback_and_ownership_fence(self) -> None:
        with (
            TemporaryDirectory() as temp,
            patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_MASTER_ENCRYPTION_KEY": Fernet.generate_key().decode(),
                    "LAUNCHPLANE_SECRET_KEYS_JSON": "",
                },
            ),
        ):
            store = PostgresRecordStore(database_url=f"sqlite+pysqlite:///{temp}/records.sqlite")
            store.ensure_schema()
            self.addCleanup(store.close)
            profile = LaunchplaneProductProfileRecord.model_validate(
                {
                    "product": "example-site",
                    "display_name": "Example",
                    "repository": "every/example",
                    "driver_id": "odoo",
                    "image": {"repository": "ghcr.io/example/site"},
                    "runtime_port": 8069,
                    "health_path": "/launchplane/health",
                    "preview": {
                        "enabled": True,
                        "context": "preview-context",
                        "app_name_prefix": "example",
                    },
                    "lanes": [
                        {
                            "context": "stable-context",
                            "instance": "testing",
                            "base_url": "https://testing.example.invalid",
                        }
                    ],
                    "updated_at": "2026-10-16T00:00:00Z",
                    "source": "test",
                }
            )
            store.write_product_profile_record(profile)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "local_admins": [
                        {
                            "subjects": ["fixture-admin"],
                            "token_labels": ["fixture-label"],
                            "products": [profile.product],
                            "contexts": [profile.preview.context, "stable-context"],
                            "actions": ["product_config.plan", "product_config.apply"],
                        }
                    ],
                    "github_humans": [
                        {
                            "logins": [_github_human_identity().login],
                            "roles": ["admin"],
                            "products": [profile.product],
                            "contexts": [profile.preview.context],
                            "actions": ["product_config.plan", "product_config.apply"],
                        }
                    ],
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": [profile.product],
                            "contexts": [profile.preview.context, "stable-context"],
                            "actions": ["product_config.plan", "product_config.apply"],
                        }
                    ],
                }
            )
            sessions = HumanSessionManager(
                config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                record_store_factory=lambda: store,
                human_session_manager=sessions,
                bearer_identity_config=BearerIdentityConfig(
                    local_admin_token="synthetic-admin-token",
                    local_admin_subject="fixture-admin",
                    local_admin_token_label="fixture-label",
                ),
            )
            runtime = RuntimeIdentity(
                product=profile.product,
                context=profile.preview.context,
                instance="example-pr-42",
                environment_kind="preview",
                deployment_record_id="deployment",
                artifact_id="artifact",
                source_git_ref="source",
                preview_id="preview42",
                preview_generation_id="generation42",
            )
            identity: dict[str, Any] = dict(
                product=runtime.product,
                context=runtime.context,
                instance=runtime.instance,
                database="preview_db",
                login="service-reader",
                password="synthetic-preview-password",
                user_id=17,
            )
            payload: dict[str, Any] = {
                "mode": "dry-run",
                "product": profile.product,
                "context": runtime.context,
                "reason": "TEST preview diagnostic activation",
                "secrets": [
                    {
                        "scope": "context",
                        "integration": diagnostic.DIAGNOSTIC_SECRET_INTEGRATION,
                        "name": diagnostic.diagnostic_preview_secret_name(runtime.instance),
                        "binding_key": diagnostic.DIAGNOSTIC_IDENTITY_KEY,
                        "value": json.dumps(identity),
                    }
                ],
            }

            async def send(body: dict[str, Any], key: str) -> Any:
                return await _post_product_config_apply(
                    app, body, idempotency_key=key, authorization="Bearer synthetic-admin-token"
                )

            for target in ("preview", "padded", "testing"):
                denied = deepcopy(payload)
                denied["mode"] = "apply"
                if target == "padded":
                    denied["secrets"][0]["integration"] = (
                        " " + diagnostic.DIAGNOSTIC_SECRET_INTEGRATION + " "
                    )
                elif target == "testing":
                    denied.update(context="stable-context", instance="testing")
                    denied["secrets"][0].update(
                        scope="context_instance",
                        name="reader",
                        value=json.dumps(
                            {**identity, "context": "stable-context", "instance": "testing"}
                        ),
                    )
                workflow = await _post_product_config_apply(
                    app, denied, idempotency_key="workflow-refusal-" + target
                )
                self.assertEqual(workflow.status_code, 403, workflow.text)
                self.assertEqual(store.list_secret_records(), ())
            missing_reason = deepcopy(payload)
            missing_reason.pop("reason")
            self.assertEqual((await send(missing_reason, "missing-reason")).status_code, 400)
            self.assertEqual((await send({**payload, "mode": "apply"}, "")).status_code, 400)
            self.assertEqual(
                (await send({**payload, "mode": "apply"}, "before-plan")).status_code, 409
            )

            planned = await send(payload, "preview-plan")
            self.assertEqual(planned.status_code, 202, planned.text)
            applied = await send({**payload, "mode": "apply"}, "preview-apply")
            self.assertEqual(applied.status_code, 202, applied.text)
            selected = diagnostic.read_diagnostic_identity(store, runtime, identity["database"])
            self.assertEqual(selected.password.get_secret_value(), identity["password"])
            self.assertNotIn(identity["password"], planned.text + applied.text)
            self.assertEqual(store.list_runtime_environment_records(), ())
            with self.assertRaises(ValueError):
                diagnostic.read_diagnostic_identity(
                    store,
                    runtime.model_copy(update={"instance": "example-pr-43"}),
                    identity["database"],
                )

            stable_payload = deepcopy(payload)
            stable_payload.update(context="stable-context", instance="testing")
            stable_identity = {**identity, "context": "stable-context", "instance": "testing"}
            stable_payload["secrets"][0].update(
                scope="context_instance",
                name=diagnostic.diagnostic_lane_secret_name("testing"),
                value=json.dumps(stable_identity),
            )
            stable_plan = await send(stable_payload, "stable-plan")
            self.assertEqual(stable_plan.status_code, 202, stable_plan.text)
            stable_apply = await send({**stable_payload, "mode": "apply"}, "stable-apply")
            self.assertEqual(stable_apply.status_code, 202, stable_apply.text)
            stable_runtime = runtime.model_copy(
                update={
                    "context": "stable-context",
                    "instance": "testing",
                    "environment_kind": "stable",
                }
            )
            self.assertEqual(
                diagnostic.read_diagnostic_identity(
                    store, stable_runtime, identity["database"]
                ).user_id,
                identity["user_id"],
            )
            wrong_name = deepcopy(stable_payload)
            wrong_name["secrets"][0]["name"] = "a-second-record"
            for mode in ("dry-run", "apply"):
                wrong = await send({**wrong_name, "mode": mode}, "wrong-stable-name-" + mode)
                self.assertEqual(wrong.status_code, 400, wrong.text)

            for role in ("admin", "read_only"):
                session = sessions.issue(_github_human_identity(role=role))

                async def browser_send(body: dict[str, Any], key: str) -> Any:
                    return await _post_product_config_apply(
                        app,
                        body,
                        authorization="",
                        headers=_browser_mutation_headers(sessions, session),
                        idempotency_key=key,
                    )

                browser_plan = await browser_send(payload, "browser-plan-" + role)
                if role == "admin":
                    self.assertEqual(browser_plan.status_code, 202, browser_plan.text)
                    browser_apply = await browser_send(
                        {**payload, "mode": "apply"}, "browser-apply"
                    )
                    self.assertEqual(browser_apply.status_code, 202, browser_apply.text)
                else:
                    self.assertEqual(browser_plan.status_code, 403, browser_plan.text)
                    browser_apply = await browser_send(
                        {**payload, "mode": "apply"}, "browser-apply-" + role
                    )
                    self.assertEqual(browser_apply.status_code, 403, browser_apply.text)

            for kind in ("runtime", "integration", "name", "copy", "foreign_product"):
                unsafe = deepcopy(payload)
                if kind == "runtime":
                    unsafe["runtime_env"] = {"env": {"EXAMPLE_SETTING": "test"}}
                elif kind == "integration":
                    unsafe["secrets"][0]["integration"] = "runtime_environment"
                elif kind == "name":
                    unsafe["secrets"][0]["name"] = "arbitrary"
                elif kind == "copy":
                    unsafe["secrets"][0].pop("value")
                    unsafe["secrets"][0]["copy_from"] = {
                        "context": "stable-context",
                        "instance": "prod",
                        "version_id": "inert",
                    }
                else:
                    unsafe["product"] = "foreign-product"
                response = await send(unsafe, "preview-refuse-" + kind)
                self.assertGreaterEqual(response.status_code, 400, response.text)
                self.assertLess(response.status_code, 500, response.text)
                self.assertEqual(store.list_runtime_environment_records(), ())

            original = store.write_product_authority_bundle

            def changed_context(bundle: ProductAuthorityBundle) -> None:
                store.write_product_profile_record(
                    profile.model_copy(
                        update={
                            "product": "concurrent-product",
                            "preview": profile.preview.model_copy(update={"enabled": False}),
                            "lanes": (
                                profile.lanes[0].model_copy(update={"context": runtime.context}),
                            ),
                        }
                    )
                )
                original(bundle)

            identity["password"] = "rotated-synthetic-password"
            changed = deepcopy(payload)
            changed["mode"] = "apply"
            changed["secrets"][0]["value"] = json.dumps(identity)
            changed_plan = await send({**changed, "mode": "dry-run"}, "changed-plan")
            self.assertEqual(changed_plan.status_code, 202, changed_plan.text)
            before = tuple(store.list_secret_records())
            with patch.object(store, "write_product_authority_bundle", side_effect=changed_context):
                refused = await send(changed, "preview-race")
            self.assertEqual(refused.status_code, 409, refused.text)
            self.assertEqual(tuple(store.list_secret_records()), before)
            self.assertEqual(store.read_product_profile_record(profile.product), profile)
