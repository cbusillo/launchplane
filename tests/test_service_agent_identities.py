import asyncio
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from control_plane import secrets
from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.runtime_environment_record import (
    RuntimeEnvironmentRecord,
    ScalarValue,
    RuntimeEnvironmentDeleteEvent,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.secret_record import SecretScope
from control_plane.dokploy.memory_capacity import PREVIEW_MEMORY_FLOORS_SETTING
from control_plane.http_routes.mutation_support import idempotency_scope
from control_plane.product_config import plan_product_config_authority_bundle
from control_plane.service_runtime_settings import (
    ServiceRuntimeSettingRequest,
    apply_service_runtime_setting,
)
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
from control_plane.storage.postgres import PostgresRecordStore, DbOnlyMutationRequest
from control_plane.storage.product_authority_bundle import (
    ProductAuthorityBundle,
    RuntimeEnvironmentWrite,
    RuntimeEnvironmentDelete,
)
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

    def admin_policy(self) -> LaunchplaneAuthzPolicy:
        return LaunchplaneAuthzPolicy.model_validate(
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

    def test_general_bundle_cannot_activate_or_remove_authentication_selectors(self) -> None:
        self.write_selectors()
        (before,) = self.store.list_runtime_environment_records(
            scope="context", context_name="launchplane"
        )
        for env in (
            {"OTHER_SETTING": "kept"},
            {**before.env, "LAUNCHPLANE_LOCAL_OPERATOR_SUBJECT": "other-fixture"},
        ):
            with self.subTest(env=env):
                after = before.model_copy(update={"env": env})
                with self.assertRaises(PermissionError):
                    self.store.write_product_authority_bundle(
                        ProductAuthorityBundle(
                            runtime_environment_writes=(
                                RuntimeEnvironmentWrite(record=after, expected_record=before),
                            )
                        )
                    )
                self.assertEqual(
                    self.store.list_runtime_environment_records(
                        scope="context", context_name="launchplane"
                    ),
                    (before,),
                )

    def test_general_bundle_cannot_displace_authentication_binding_with_unrelated_identity(
        self,
    ) -> None:
        self.write_token(
            key="LAUNCHPLANE_LOCAL_OPERATOR_TOKEN", value=self.bootstrap.local_operator_token
        )
        (before,) = self.store.list_secret_bindings()
        after = before.model_copy(
            update={"integration": "runtime_environment", "binding_key": "OTHER_SETTING"}
        )
        with self.assertRaises(PermissionError):
            self.store.write_product_authority_bundle(
                ProductAuthorityBundle(secret_bindings=(after,))
            )
        self.assertEqual(self.store.list_secret_bindings(), (before,))

    def test_general_bundle_cannot_delete_the_authentication_source_record(self) -> None:
        self.write_selectors()
        (before,) = self.store.list_runtime_environment_records(
            scope="context", context_name="launchplane"
        )
        event = RuntimeEnvironmentDeleteEvent(
            event_id="fixture-deletion",
            recorded_at=before.updated_at,
            scope=before.scope,
            context=before.context,
            env_keys=tuple(before.env),
            env_value_count=len(before.env),
        )
        with self.assertRaises(PermissionError):
            self.store.write_product_authority_bundle(
                ProductAuthorityBundle(
                    delete_runtime_environments=(
                        RuntimeEnvironmentDelete(expected_record=before, event=event),
                    )
                )
            )
        self.assertEqual(
            self.store.list_runtime_environment_records(
                scope="context", context_name="launchplane"
            ),
            (before,),
        )

    def test_administrator_authority_is_rechecked_after_planning(self) -> None:
        active = LaunchplaneAuthzPolicyRecord(
            record_id="active-admin",
            source="test",
            updated_at="2026-10-28T00:00:00Z",
            policy=self.admin_policy(),
        )
        active = self.store.seed_authz_policy_if_absent(active)
        _, bundle = plan_product_config_authority_bundle(
            record_store=self.store,
            payload={
                "product": "launchplane",
                "context": "launchplane",
                "runtime_env": {AGENT_BEARER_SOURCE_KEY: "records"},
            },
            mode="apply",
            actor="isolated-test",
            source_label="isolated-test",
        )
        revoked = LaunchplaneAuthzPolicyRecord(
            record_id="revoked-admin",
            revision=active.revision + 1,
            source="test",
            updated_at=active.updated_at,
            policy=LaunchplaneAuthzPolicy(),
        )
        change = self.store.compare_and_write_authz_policy_record(
            expected_record=active, replacement_record=revoked
        )
        self.assertEqual(change.status, "written")
        identity = LocalAdminIdentity(
            subject=self.bootstrap.local_admin_subject,
            token_label=self.bootstrap.local_admin_token_label,
        )
        with self.assertRaises(PermissionError):
            self.store.write_product_authority_bundle(bundle, identity=identity)
        self.assertEqual(self.store.list_runtime_environment_records(), ())
        self.assertEqual(
            bearer_identity_from_token(token=self.bootstrap.local_admin_token, config=self.load()),
            identity,
        )

    def test_general_bundle_cannot_rotate_a_token_through_an_innocuous_alias(self) -> None:
        self.write_selectors()
        self.write_token(
            key="LAUNCHPLANE_LOCAL_OPERATOR_TOKEN", value=self.bootstrap.local_operator_token
        )
        (record,) = self.store.list_secret_records()
        _, bundle = plan_product_config_authority_bundle(
            record_store=self.store,
            payload={
                "product": "launchplane",
                "context": "launchplane",
                "secrets": [
                    {
                        "integration": secrets.LAUNCHPLANE_SERVICE_SECRET_INTEGRATION,
                        "name": record.name,
                        "binding_key": "OTHER_SETTING",
                        "value": "untrusted-fixture",
                    }
                ],
            },
            mode="apply",
            actor="isolated-test",
            source_label="isolated-test",
        )
        with self.assertRaises(PermissionError):
            self.store.write_product_authority_bundle(bundle)
        self.assertEqual(self.load().local_operator_token, self.bootstrap.local_operator_token)

    def test_onboarding_cannot_activate_preposition_or_displace_authentication(self) -> None:
        self.write_token(
            key="LAUNCHPLANE_LOCAL_OPERATOR_TOKEN", value=self.bootstrap.local_operator_token
        )
        (binding,) = self.store.list_secret_bindings()
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_environment_read_policy(
                products=("launchplane",), actions=("product_onboarding.apply",)
            ),
            record_store_factory=lambda: self.store,
        )
        cases: tuple[dict[str, object], ...] = (
            {
                "runtime_environments": [
                    {
                        "scope": "context",
                        "context": "launchplane",
                        "env": {AGENT_BEARER_SOURCE_KEY: "records"},
                    }
                ]
            },
            {
                "secret_bindings": [
                    {
                        "integration": secrets.LAUNCHPLANE_SERVICE_SECRET_INTEGRATION,
                        "context": "launchplane",
                        "binding_key": "LAUNCHPLANE_LOCAL_OPERATOR_TOKEN",
                        "secret_id": binding.secret_id,
                        "status": "configured",
                    }
                ]
            },
            {
                "secret_bindings": [
                    {
                        "binding_id": binding.binding_id,
                        "integration": "runtime_environment",
                        "context": "launchplane",
                        "binding_key": "OTHER_SETTING",
                        "secret_id": binding.secret_id,
                        "status": "configured",
                    }
                ]
            },
        )
        for index, fields in enumerate(cases):
            with self.subTest(fields=fields):
                key = f"onboarding-identity-{index}"
                response = asyncio.run(
                    http_request(
                        app,
                        "POST",
                        "/v1/product-onboarding/apply",
                        headers={"Authorization": "Bearer valid-token", "Idempotency-Key": key},
                        payload={
                            "product": "launchplane",
                            "manifest": {
                                "product": "launchplane",
                                "display_name": "Synthetic Service",
                                "repository": "example/synthetic-service",
                                "image_repository": "ghcr.io/example/synthetic-service",
                                "lanes": [{"instance": "testing", "context": "launchplane"}],
                                **fields,
                            },
                        },
                    )
                )
                self.assertEqual(response.status_code, 403, response.text)
                self.assertIsNone(
                    self.store.read_idempotency_record(
                        scope=idempotency_scope(_identity()),
                        route_path="/v1/product-onboarding/apply",
                        idempotency_key=key,
                    )
                )
                self.assertEqual(self.store.list_secret_bindings(), (binding,))
                self.assertEqual(self.store.list_product_profile_records(), ())
                self.assertEqual(self.store.list_runtime_environment_records(), ())

    def test_unrelated_service_setting_and_credentials_preserve_authentication(self) -> None:
        self.write_selectors()
        self.write_token(
            key="LAUNCHPLANE_LOCAL_OPERATOR_TOKEN", value=self.bootstrap.local_operator_token
        )
        before = self.load()
        request = ServiceRuntimeSettingRequest(
            key=PREVIEW_MEMORY_FLOORS_SETTING,
            value='{"synthetic-host":5368709120}',
            reason="Synthetic setting qualification.",
        )

        def apply_setting(setting: ServiceRuntimeSettingRequest) -> dict[str, object]:
            return apply_service_runtime_setting(
                store=self.store,
                request=setting,
                actor="isolated-test",
                trace_id="setting-fixture",
                idempotency_key="setting-fixture",
            )

        plan = apply_setting(request)
        apply_setting(
            request.model_copy(
                update={"mode": "apply", "expected_plan_digest": plan["plan_digest"]}
            )
        )
        _, bundle = plan_product_config_authority_bundle(
            record_store=self.store,
            payload={
                "product": "launchplane",
                "context": "launchplane",
                "secrets": [
                    {
                        "integration": secrets.LAUNCHPLANE_SERVICE_SECRET_INTEGRATION,
                        "binding_key": "OTHER_SERVICE_CREDENTIAL",
                        "value": "unrelated-fixture",
                    }
                ],
            },
            mode="apply",
            actor="isolated-test",
            source_label="isolated-test",
        )
        self.store.write_product_authority_bundle(bundle)
        self.assertEqual(self.load(), before)

    def test_current_token_version_cannot_be_overwritten_by_a_general_bundle(self) -> None:
        self.write_selectors()
        self.write_token(
            key="LAUNCHPLANE_LOCAL_OPERATOR_TOKEN", value=self.bootstrap.local_operator_token
        )
        (record,) = self.store.list_secret_records()
        version = self.store.read_secret_version(record.current_version_id)
        ciphertext, key_id = secrets._encrypt_secret_value("untrusted-fixture")
        with self.assertRaises(PermissionError):
            self.store.write_product_authority_bundle(
                ProductAuthorityBundle(
                    secret_versions=(
                        version.model_copy(update={"ciphertext": ciphertext, "key_id": key_id}),
                    )
                )
            )
        self.assertEqual(self.load().local_operator_token, self.bootstrap.local_operator_token)

    def test_refused_reserved_bundle_leaves_no_mutation_receipt(self) -> None:
        bundle = ProductAuthorityBundle(
            runtime_environments=(
                RuntimeEnvironmentRecord(
                    scope="context",
                    context="launchplane",
                    env={AGENT_BEARER_SOURCE_KEY: "records"},
                    updated_at="2026-10-28T00:00:00Z",
                ),
            )
        )
        mutation = DbOnlyMutationRequest(
            scope="fixture",
            route_path="/fixture",
            idempotency_key="refused-fixture",
            request_fingerprint="fixture",
            lease_owner="fixture",
            response_status_code=200,
            response_trace_id="fixture",
            response_payload={"status": "ok"},
        )
        with self.assertRaises(PermissionError):
            self.store.compare_and_write_product_authority_bundle(bundle, mutation=mutation)
        self.assertEqual(self.store.list_runtime_environment_records(), ())
        self.assertIsNone(
            self.store.read_idempotency_record(
                scope=mutation.scope,
                route_path=mutation.route_path,
                idempotency_key=mutation.idempotency_key,
            )
        )

    def test_authorized_admin_has_a_reviewed_write_only_path_for_existing_identity(self) -> None:
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(
                _generic_site_profile_payload("launchplane")
            )
        )
        policy = self.admin_policy()
        self.store.seed_authz_policy_if_absent(
            LaunchplaneAuthzPolicyRecord(
                record_id="identity-admin-policy",
                source="test",
                updated_at="2026-10-28T00:00:00Z",
                policy=policy,
            )
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
