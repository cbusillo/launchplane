import asyncio
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from control_plane import secrets
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord, ScalarValue
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.secret_record import SecretScope
from control_plane.service_agent_identities import (
    AGENT_BEARER_SOURCE_KEY,
    load_service_agent_identities,
)
from control_plane.service_bootstrap import create_launchplane_service_application
from control_plane.service_auth import (
    BearerIdentityConfig,
    LaunchplaneAuthzPolicy,
    LocalAdminIdentity,
    LocalOperatorIdentity,
    TerminalAgentIdentity,
    bearer_identity_from_token,
)
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.auth import _StubVerifier, _identity
from tests.support.http import get as http_get
from tests.support.stores import sqlite_database_url
from tests.http_app_test_support import _product_environment_read_policy
from tests.support.http import request as http_request
from tests.support.profiles import _generic_site_profile_payload


class ServiceAgentIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = PostgresRecordStore(
            database_url=sqlite_database_url(Path(directory.name) / "state.sqlite3")
        )
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        self.enterContext(
            patch.dict(
                os.environ,
                {secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "isolated-test-key"},
                clear=True,
            )
        )
        self.bootstrap = BearerIdentityConfig(
            local_operator_token="operator-fixture",
            local_operator_subject="director-fixture",
            local_operator_token_label="operator-label",
            terminal_agent_token="terminal-fixture",
            terminal_agent_subject="director-fixture",
            terminal_agent_token_label="terminal-label",
            local_admin_token="recovery-fixture",
            local_admin_subject="recovery-admin",
            local_admin_token_label="recovery-label",
        )

    def write_selectors(self, **overrides: ScalarValue) -> None:
        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="context",
                context="launchplane",
                env={
                    AGENT_BEARER_SOURCE_KEY: "records",
                    "LAUNCHPLANE_LOCAL_OPERATOR_SUBJECT": self.bootstrap.local_operator_subject,
                    "LAUNCHPLANE_LOCAL_OPERATOR_TOKEN_LABEL": self.bootstrap.local_operator_token_label,
                    "LAUNCHPLANE_TERMINAL_AGENT_SUBJECT": self.bootstrap.terminal_agent_subject,
                    "LAUNCHPLANE_TERMINAL_AGENT_TOKEN_LABEL": self.bootstrap.terminal_agent_token_label,
                    **overrides,
                },
                updated_at="2026-10-28T00:00:00Z",
            )
        )

    def write_token(
        self,
        *,
        key: str,
        value: str,
        name: str = "",
        context: str = "launchplane",
        scope: SecretScope = "context",
        instance: str = "",
    ) -> None:
        secrets.write_secret_value(
            record_store=self.store,
            scope=scope,
            integration=secrets.LAUNCHPLANE_SERVICE_SECRET_INTEGRATION,
            name=name or key.lower().replace("_", "-"),
            binding_key=key,
            plaintext_value=value,
            context_name=context,
            instance_name=instance,
            actor="isolated-test",
        )

    def load(self) -> BearerIdentityConfig:
        return load_service_agent_identities(store=self.store, bootstrap=self.bootstrap)

    def test_absent_record_selectors_preserve_existing_bootstrap(self) -> None:
        self.assertEqual(self.load(), self.bootstrap)

    def test_selectors_without_explicit_activation_preserve_bootstrap(self) -> None:
        self.write_selectors()
        (record,) = self.store.list_runtime_environment_records(
            scope="context", context_name="launchplane"
        )
        del record.env[AGENT_BEARER_SOURCE_KEY]
        self.store.write_runtime_environment_record(record)
        self.assertEqual(self.load(), self.bootstrap)

    def test_unknown_source_disables_agents_without_bootstrap_fallback(self) -> None:
        self.write_selectors(**{AGENT_BEARER_SOURCE_KEY: "invalid"})
        config = self.load()
        self.assertEqual(config.local_operator_token, "")
        self.assertEqual(config.terminal_agent_token, "")
        self.assertEqual(config.local_admin_token, self.bootstrap.local_admin_token)

    def test_records_preserve_principal_types_selectors_and_admin_without_env_tokens(self) -> None:
        self.write_selectors()
        self.write_token(
            key="LAUNCHPLANE_LOCAL_OPERATOR_TOKEN", value=self.bootstrap.local_operator_token
        )
        self.write_token(
            key="LAUNCHPLANE_TERMINAL_AGENT_READ_TOKEN", value=self.bootstrap.terminal_agent_token
        )
        expected = self.bootstrap
        self.bootstrap = self.bootstrap.model_copy(
            update={
                "local_operator_token": "",
                "local_operator_subject": "",
                "local_operator_token_label": "",
                "terminal_agent_token": "",
                "terminal_agent_subject": "",
                "terminal_agent_token_label": "",
            }
        )
        config = self.load()
        for token, identity_type in (
            (expected.local_operator_token, LocalOperatorIdentity),
            (expected.terminal_agent_token, TerminalAgentIdentity),
            (expected.local_admin_token, LocalAdminIdentity),
        ):
            with self.subTest(identity_type=identity_type):
                actual = bearer_identity_from_token(token=token, config=config)
                self.assertIsInstance(actual, identity_type)
                self.assertEqual(actual, bearer_identity_from_token(token=token, config=expected))
        self.assertIsNone(bearer_identity_from_token(token="wrong-fixture", config=config))

    def test_missing_secret_does_not_reuse_bootstrap_token_or_admin(self) -> None:
        self.write_selectors()
        config = self.load()
        self.assertIsNone(
            bearer_identity_from_token(token=self.bootstrap.local_operator_token, config=config)
        )
        self.assertIsNone(
            bearer_identity_from_token(token=self.bootstrap.terminal_agent_token, config=config)
        )
        self.assertIsInstance(
            bearer_identity_from_token(token=self.bootstrap.local_admin_token, config=config),
            LocalAdminIdentity,
        )

    def test_partial_or_non_string_selectors_disable_only_the_affected_identity(self) -> None:
        self.write_token(
            key="LAUNCHPLANE_LOCAL_OPERATOR_TOKEN", value=self.bootstrap.local_operator_token
        )
        self.write_token(
            key="LAUNCHPLANE_TERMINAL_AGENT_READ_TOKEN", value=self.bootstrap.terminal_agent_token
        )
        for invalid_label in ("", 17):
            with self.subTest(invalid_label=invalid_label):
                self.write_selectors(LAUNCHPLANE_LOCAL_OPERATOR_TOKEN_LABEL=invalid_label)
                config = self.load()
                self.assertEqual(config.local_operator_token, "")
                self.assertEqual(config.terminal_agent_token, self.bootstrap.terminal_agent_token)
                self.assertEqual(config.local_admin_token, self.bootstrap.local_admin_token)

    def test_ambiguous_secret_bindings_disable_the_identity(self) -> None:
        self.write_selectors()
        for name in ("first-binding", "second-binding"):
            self.write_token(
                key="LAUNCHPLANE_LOCAL_OPERATOR_TOKEN",
                value=self.bootstrap.local_operator_token,
                name=name,
            )
        self.assertEqual(self.load().local_operator_token, "")

    def test_other_context_global_and_lane_secrets_cannot_supply_authentication(self) -> None:
        self.write_selectors()
        cases: tuple[tuple[SecretScope, str, str], ...] = (
            ("context", "other-site", ""),
            ("global", "", ""),
            ("context_instance", "launchplane", "testing"),
        )
        for scope, context, instance in cases:
            with self.subTest(scope=scope, context=context):
                self.write_token(
                    key="LAUNCHPLANE_LOCAL_OPERATOR_TOKEN",
                    value=self.bootstrap.local_operator_token,
                    context=context,
                    scope=scope,
                    instance=instance,
                )
                self.assertEqual(self.load().local_operator_token, "")

    def test_record_read_failure_disables_agents_and_keeps_independent_admin(self) -> None:
        with patch.object(
            self.store, "list_runtime_environment_records", side_effect=OSError("unavailable")
        ):
            config = self.load()
        self.assertEqual(config.local_operator_token, "")
        self.assertEqual(config.terminal_agent_token, "")
        self.assertEqual(config.local_admin_token, self.bootstrap.local_admin_token)

    def test_unreadable_secret_never_falls_back_to_env(self) -> None:
        self.write_selectors()
        self.write_token(
            key="LAUNCHPLANE_LOCAL_OPERATOR_TOKEN", value=self.bootstrap.local_operator_token
        )
        with patch.dict(
            os.environ, {secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "different-test-key"}
        ):
            config = self.load()
        self.assertEqual(config.local_operator_token, "")
        self.assertEqual(config.local_admin_token, self.bootstrap.local_admin_token)

    def test_service_authenticates_managed_token_and_refuses_wrong_token(self) -> None:
        self.write_selectors()
        self.write_token(
            key="LAUNCHPLANE_TERMINAL_AGENT_READ_TOKEN", value=self.bootstrap.terminal_agent_token
        )
        with TemporaryDirectory() as directory:
            app = create_launchplane_service_application(
                state_dir=Path(directory),
                control_plane_root_path=Path(directory),
                bootstrap_authz_policy=LaunchplaneAuthzPolicy(),
                verifier=_StubVerifier(_identity()),
                service_record_store=self.store,
                start_schedulers=False,
            )
            for token, expected_status in (
                (self.bootstrap.terminal_agent_token, 200),
                ("wrong-fixture", 401),
            ):
                with self.subTest(token=token):
                    response = asyncio.run(
                        http_get(
                            app,
                            "/v1/agent/context",
                            headers={"Authorization": f"Bearer {token}"},
                        )
                    )
                    self.assertEqual(response.status_code, expected_status, response.text)

    def test_general_config_writer_cannot_change_service_authentication(self) -> None:
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(
                _generic_site_profile_payload("launchplane")
            )
        )
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_environment_read_policy(
                context="launchplane",
                products=("launchplane",),
                actions=("product_config.plan", "product_config.apply"),
            ),
            record_store_factory=lambda: self.store,
        )
        for fields in (
            {"runtime_env": {AGENT_BEARER_SOURCE_KEY: "records"}},
            {
                "secrets": [
                    {
                        "integration": secrets.LAUNCHPLANE_SERVICE_SECRET_INTEGRATION,
                        "binding_key": "LAUNCHPLANE_LOCAL_OPERATOR_TOKEN",
                        "value": "untrusted-fixture",
                    }
                ]
            },
            {
                "secrets": [
                    {
                        "integration": secrets.LAUNCHPLANE_SERVICE_SECRET_INTEGRATION,
                        "name": "existing-identity-record",
                        "binding_key": "OTHER_SETTING",
                        "value": "untrusted-fixture",
                    }
                ]
            },
        ):
            with self.subTest(fields=fields):
                response = asyncio.run(
                    http_request(
                        app,
                        "POST",
                        "/v1/product-config/apply",
                        headers={"Authorization": "Bearer valid-token"},
                        payload={
                            "mode": "apply",
                            "product": "launchplane",
                            "context": "launchplane",
                            **fields,
                        },
                    )
                )
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(response.json()["error"]["code"], "authorization_denied")
        self.assertEqual(self.store.list_secret_records(), ())
        self.assertEqual(self.store.list_runtime_environment_records(), ())

    def test_authorized_admin_has_a_reviewed_write_only_path_for_existing_identity(self) -> None:
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(
                _generic_site_profile_payload("launchplane")
            )
        )
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "local_admins": [
                    {
                        "subjects": [self.bootstrap.local_admin_subject],
                        "token_labels": [self.bootstrap.local_admin_token_label],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": [
                            "product_config.plan",
                            "product_config.apply",
                            "authz_policy_grant.write",
                        ],
                    }
                ]
            }
        )
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=policy,
            record_store_factory=lambda: self.store,
            bearer_identity_config=self.bootstrap,
        )
        headers = {"Authorization": f"Bearer {self.bootstrap.local_admin_token}"}
        for mode in ("dry-run", "apply"):
            response = asyncio.run(
                http_request(
                    app,
                    "POST",
                    "/v1/product-config/apply",
                    headers={**headers, "Idempotency-Key": f"identity-fixture-{mode}"},
                    payload={
                        "mode": mode,
                        "product": "launchplane",
                        "context": "launchplane",
                        "reason": "Qualify existing fixture identity storage.",
                        "runtime_env": {
                            AGENT_BEARER_SOURCE_KEY: "records",
                            "LAUNCHPLANE_LOCAL_OPERATOR_SUBJECT": self.bootstrap.local_operator_subject,
                            "LAUNCHPLANE_LOCAL_OPERATOR_TOKEN_LABEL": self.bootstrap.local_operator_token_label,
                        },
                        "secrets": [
                            {
                                "integration": secrets.LAUNCHPLANE_SERVICE_SECRET_INTEGRATION,
                                "binding_key": "LAUNCHPLANE_LOCAL_OPERATOR_TOKEN",
                                "value": self.bootstrap.local_operator_token,
                            }
                        ],
                    },
                )
            )
            self.assertEqual(response.status_code, 202, response.text)
            self.assertNotIn(self.bootstrap.local_operator_token, response.text)
        self.assertEqual(self.load().local_operator_token, self.bootstrap.local_operator_token)
