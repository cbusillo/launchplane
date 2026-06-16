import json
import unittest
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from collections.abc import MutableMapping
from typing import Any, cast
from urllib.parse import urlencode
from unittest.mock import patch

from a2wsgi import WSGIMiddleware
from fastapi import FastAPI
from jwt import InvalidTokenError
from starlette.types import ASGIApp

from control_plane import secrets as control_plane_secrets
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import (
    BearerIdentityConfig,
    GitHubHumanIdentity,
    GitHubActionsIdentity,
    LaunchplaneAuthzPolicy,
)
from control_plane.service_human_auth import (
    GitHubOAuthConfig,
    HumanSessionManager,
    InMemoryHumanSessionStore,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.test_service import create_launchplane_service_app
from tests.test_service import _generic_site_profile_payload, _identity, _sqlite_database_url
from tests.test_service import _StubVerifier
from tests.test_protected_artifacts import _seed_store as seed_protected_artifact_store


class FastApiHealthContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_returns_typed_public_safe_payload(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            record_store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_environment_read_policy(context="example-site"),
                record_store_factory=lambda: record_store,
            )

            response = await _asgi_get(app, "/v1/health")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(set(payload), {"status", "storage_backend", "trace_id"})
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["storage_backend"], "filesystem")
        self.assertTrue(str(payload["trace_id"]).startswith("launchplane_req_"))

    async def test_openapi_includes_health_contract(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_environment_read_policy(context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        route = openapi["paths"]["/v1/health"]["get"]
        self.assertEqual(route["operationId"], "read_launchplane_health")
        success_schema = route["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertEqual(success_schema["$ref"], "#/components/schemas/HealthResponse")
        health_schema = openapi["components"]["schemas"]["HealthResponse"]
        self.assertEqual(
            set(health_schema["properties"]), {"status", "storage_backend", "trace_id"}
        )
        self.assertEqual(health_schema["additionalProperties"], False)
        example_text = json.dumps(health_schema.get("examples", []))
        self.assertIn("launchplane_req_00000000000000000000000000000000", example_text)
        self.assertNotIn("example-site", example_text)
        self.assertNotIn("shinycomputers", example_text)


class FastApiProductEnvironmentConfigStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_config_status_redacts_expected_config_status(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_generic_site_profile_payload())
            )
            store.write_runtime_environment_record(
                RuntimeEnvironmentRecord(
                    scope="instance",
                    context="example-site",
                    instance="prod",
                    env={"INTERNAL_CALLBACK_URL": "https://internal.example-site.invalid"},
                    updated_at="2026-05-02T22:32:00Z",
                    source_label="test",
                )
            )
            with patch.dict(
                "os.environ",
                {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
                clear=True,
            ):
                control_plane_secrets.write_secret_value(
                    record_store=store,
                    scope="context_instance",
                    integration=control_plane_secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
                    name="SMTP_PASSWORD",
                    plaintext_value="super-secret-password",
                    binding_key="SMTP_PASSWORD",
                    context_name="example-site",
                    instance_name="prod",
                    actor="test",
                )
            store.close()
            app_store = PostgresRecordStore(database_url=database_url)

            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_environment_read_policy(context="example-site"),
                record_store_factory=lambda: app_store,
            )

            response = await _get_config_status(app)
            app_store.close()

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        response_text = json.dumps(payload)
        config_status = payload["config_status"]
        runtime_statuses = {
            item["key"]: item["status"] for item in config_status["runtime_settings"]
        }
        secret_statuses = {
            item["binding_key"]: item["status"] for item in config_status["managed_secrets"]
        }
        self.assertEqual(
            runtime_statuses,
            {"INTERNAL_CALLBACK_URL": "configured", "RESEND_FROM_EMAIL": "missing"},
        )
        self.assertEqual(
            secret_statuses,
            {"SMTP_PASSWORD": "configured", "RESEND_API_KEY": "missing"},
        )
        self.assertNotIn("https://internal.example-site.invalid", response_text)
        self.assertNotIn("super-secret-password", response_text)

    async def test_config_status_uses_lane_authorization(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_generic_site_profile_payload())
            )
            store.close()
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_environment_read_policy(context="launchplane"),
                record_store_factory=lambda: app_store,
            )

            response = await _get_config_status(app)
            app_store.close()

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    async def test_config_status_accepts_local_operator_bearer_identity(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_generic_site_profile_payload())
            )
            store.close()
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_local_operator_product_environment_read_policy(
                    context="example-site"
                ),
                record_store_factory=lambda: app_store,
                bearer_identity_config=BearerIdentityConfig(
                    local_operator_token="local-operator-token",
                    local_operator_subject="local-owner-agent",
                    local_operator_token_label="local-owner-read",
                ),
            )

            response = await _get_config_status(
                app,
                authorization="Bearer local-operator-token",
            )
            app_store.close()

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["config_status"]["product"], "example-site")

    async def test_config_status_owner_agent_identity_fails_closed_without_metadata(
        self,
    ) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_local_operator_product_environment_read_policy(context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=BearerIdentityConfig(
                local_operator_token="local-operator-token",
            ),
        )

        response = await _get_config_status(
            app,
            authorization="Bearer local-operator-token",
        )

        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authentication_required")
        self.assertEqual(
            payload["error"]["message"],
            "LAUNCHPLANE_LOCAL_OPERATOR_SUBJECT is required for configured bearer auth.",
        )

    async def test_config_status_returns_not_found_for_unknown_environment(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_generic_site_profile_payload())
            )
            store.close()
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_environment_read_policy(context="example-site"),
                record_store_factory=lambda: app_store,
            )

            response = await _get_config_status(app, environment="staging")
            app_store.close()

        self.assertEqual(response.status_code, 404)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "not_found")
        self.assertEqual(
            payload["error"]["message"],
            "Product 'example-site' has no environment 'staging'.",
        )

    async def test_config_status_requires_bearer_identity(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_environment_read_policy(context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _get_config_status(app, authorization="")

        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authentication_required")
        self.assertEqual(response.headers["WWW-Authenticate"], 'Bearer realm="Launchplane API"')

    async def test_config_status_rejects_invalid_bearer_token(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_product_environment_read_policy(context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _get_config_status(app)

        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authentication_required")
        self.assertEqual(payload["error"]["message"], "signature verification failed")
        self.assertEqual(response.headers["WWW-Authenticate"], 'Bearer realm="Launchplane API"')

    async def test_config_status_requires_product_read_store(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_environment_read_policy(context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _get_config_status(app)

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "database_storage_required")

    async def test_config_status_validation_errors_use_launchplane_error_shape(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_environment_read_policy(context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(
            app,
            "/v1/products/ /environments/prod/config-status",
            headers={"Authorization": "Bearer valid-token"},
        )

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertIn("trace_id", payload)
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertNotIn("detail", payload)

    async def test_openapi_includes_config_status_route(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_environment_read_policy(context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        route = openapi["paths"]["/v1/products/{product}/environments/{environment}/config-status"][
            "get"
        ]
        self.assertIn("ProductEnvironmentConfigStatusResponse", json.dumps(route))
        self.assertIn("LaunchplaneErrorResponse", json.dumps(route))

    async def test_fastapi_app_can_mount_legacy_wsgi_fallback(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            record_store = FilesystemRecordStore(state_dir=root / "state")
            policy = _product_environment_read_policy(context="example-site")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                record_store_factory=lambda: record_store,
            )
            legacy_app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                local_record_store_for_tests=record_store,
            )
            app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

            openapi_response = await _asgi_get(app, "/openapi.json")
            health_response = await _asgi_get(app, "/v1/health")

        self.assertEqual(openapi_response.status_code, 200)
        self.assertIn(
            "/v1/health",
            openapi_response.json()["paths"],
        )
        self.assertIn(
            "/v1/products/{product}/environments/{environment}/config-status",
            openapi_response.json()["paths"],
        )
        self.assertEqual(health_response.status_code, 200)
        health_payload = health_response.json()
        self.assertEqual(health_payload["status"], "ok")
        self.assertEqual(health_payload["storage_backend"], "filesystem")
        self.assertIn("trace_id", health_payload)


class FastApiProtectedArtifactsTests(unittest.IsolatedAsyncioTestCase):
    async def test_protected_artifacts_returns_launchplane_inventory(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            record_store = FilesystemRecordStore(Path(temporary_directory_name) / "state")
            seed_protected_artifact_store(record_store)
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_local_operator_artifact_protection_policy(
                    products=("verireel",),
                    contexts=("*",),
                ),
                record_store_factory=lambda: record_store,
                bearer_identity_config=_local_operator_bearer_config(),
            )

            response = await _get_protected_artifacts(app, product="verireel")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        protected_artifacts = payload["protected_artifacts"]
        self.assertEqual(protected_artifacts["product"], "verireel")
        self.assertIn("artifact-verireel-prod", protected_artifacts["artifact_ids"])
        self.assertIn("artifact-preview-verireel-pr-196", protected_artifacts["artifact_ids"])
        self.assertNotIn("artifact-preview-verireel-pr-195", protected_artifacts["artifact_ids"])

    async def test_protected_artifacts_requires_wildcard_context_for_whole_product(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            record_store = FilesystemRecordStore(Path(temporary_directory_name) / "state")
            seed_protected_artifact_store(record_store)
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_local_operator_artifact_protection_policy(
                    products=("verireel",),
                    contexts=("verireel",),
                ),
                record_store_factory=lambda: record_store,
                bearer_identity_config=_local_operator_bearer_config(),
            )

            response = await _get_protected_artifacts(app, product="verireel")

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")
        self.assertNotIn("authz", payload)

    async def test_protected_artifacts_allows_scoped_context_read(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            record_store = FilesystemRecordStore(Path(temporary_directory_name) / "state")
            seed_protected_artifact_store(record_store)
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_local_operator_artifact_protection_policy(
                    products=("verireel",),
                    contexts=("verireel",),
                ),
                record_store_factory=lambda: record_store,
                bearer_identity_config=_local_operator_bearer_config(),
            )

            response = await _get_protected_artifacts(
                app,
                product="verireel",
                context="verireel",
            )

        self.assertEqual(response.status_code, 200)
        protected_artifacts = response.json()["protected_artifacts"]
        self.assertEqual(protected_artifacts["product"], "verireel")
        self.assertEqual(protected_artifacts["context"], "verireel")

    async def test_protected_artifacts_rejects_wrong_product_scope(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            record_store = FilesystemRecordStore(Path(temporary_directory_name) / "state")
            seed_protected_artifact_store(record_store)
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_local_operator_artifact_protection_policy(
                    products=("other-product",),
                    contexts=("*",),
                ),
                record_store_factory=lambda: record_store,
                bearer_identity_config=_local_operator_bearer_config(),
            )

            response = await _get_protected_artifacts(app, product="verireel")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_protected_artifacts_requires_product_query(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_local_operator_artifact_protection_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=_local_operator_bearer_config(),
        )

        response = await _get_protected_artifacts(app, product="")

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "invalid_query")

    async def test_protected_artifacts_requires_bearer_identity(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_local_operator_artifact_protection_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _get_protected_artifacts(
            app,
            product="verireel",
            authorization="",
        )

        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authentication_required")
        self.assertEqual(response.headers["WWW-Authenticate"], 'Bearer realm="Launchplane API"')

    async def test_protected_artifacts_accepts_human_session_when_mounted_over_wsgi(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            record_store = FilesystemRecordStore(state_dir=root / "state")
            seed_protected_artifact_store(record_store)
            policy = _github_human_artifact_protection_policy(
                products=("verireel",),
                contexts=("*",),
            )
            oauth_config = _github_oauth_config()
            session_store = InMemoryHumanSessionStore()
            session_manager = HumanSessionManager(
                config=oauth_config,
                session_store=session_store,
            )
            human_session = session_manager.issue(_github_human_identity())
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=policy,
                record_store_factory=lambda: record_store,
                human_session_manager=session_manager,
            )
            legacy_app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_RejectingVerifier(),
                authz_policy=policy,
                local_record_store_for_tests=record_store,
                github_oauth_config=oauth_config,
                human_session_store=session_store,
            )
            app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

            response = await _get_protected_artifacts(
                app,
                product="verireel",
                authorization="",
                headers={"Cookie": session_manager.session_cookie_header(human_session)},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertIn("trace_id", payload)
        self.assertEqual(payload["protected_artifacts"]["product"], "verireel")
        self.assertNotIn("Set-Cookie", response.headers)

    async def test_protected_artifacts_prefers_bearer_identity_over_human_cookie(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            record_store = FilesystemRecordStore(state_dir=root / "state")
            seed_protected_artifact_store(record_store)
            oauth_config = _github_oauth_config()
            session_manager = HumanSessionManager(
                config=oauth_config,
                session_store=InMemoryHumanSessionStore(),
            )
            human_session = session_manager.issue(_github_human_identity())
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_local_operator_artifact_protection_policy(
                    products=("verireel",),
                    contexts=("*",),
                ),
                record_store_factory=lambda: record_store,
                bearer_identity_config=_local_operator_bearer_config(),
                human_session_manager=session_manager,
            )

            response = await _get_protected_artifacts(
                app,
                product="verireel",
                authorization="Bearer local-operator-token",
                headers={"Cookie": session_manager.session_cookie_header(human_session)},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["protected_artifacts"]["product"], "verireel")

    async def test_protected_artifacts_rejects_terminal_agent_without_artifact_grant(
        self,
    ) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_local_operator_artifact_protection_policy(
                products=("verireel",),
                contexts=("*",),
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=BearerIdentityConfig(
                terminal_agent_token="terminal-agent-token",
                terminal_agent_subject="worker-agent",
                terminal_agent_token_label="terminal-agent-read",
            ),
        )

        response = await _get_protected_artifacts(
            app,
            product="verireel",
            authorization="Bearer terminal-agent-token",
        )

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")
        self.assertNotIn("authz", payload)

    async def test_protected_artifacts_requires_supported_store(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_local_operator_artifact_protection_policy(
                products=("verireel",),
                contexts=("*",),
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=_local_operator_bearer_config(),
        )

        response = await _get_protected_artifacts(app, product="verireel")

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "database_storage_required")

    async def test_openapi_includes_protected_artifacts_contract(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_local_operator_artifact_protection_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        route = openapi["paths"]["/v1/artifacts/protected"]["get"]
        self.assertEqual(route["operationId"], "read_protected_artifacts")
        success_schema = route["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertEqual(success_schema["$ref"], "#/components/schemas/ProtectedArtifactsResponse")
        self.assertEqual(
            route["responses"]["503"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/LaunchplaneErrorResponse",
        )
        self.assertIn("LaunchplaneErrorResponse", json.dumps(route))
        response_schema = openapi["components"]["schemas"]["ProtectedArtifactsResponse"]
        self.assertEqual(response_schema["additionalProperties"], False)
        example_text = json.dumps(response_schema.get("examples", []))
        self.assertIn("example-product", example_text)
        self.assertNotIn("verireel", example_text)
        self.assertNotIn("cbusillo", example_text)
        self.assertNotIn("shinycomputers", example_text)

    async def test_fastapi_protected_artifacts_precedes_legacy_wsgi_fallback(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            record_store = FilesystemRecordStore(state_dir=root / "state")
            seed_protected_artifact_store(record_store)
            policy = _local_operator_artifact_protection_policy(
                products=("verireel",),
                contexts=("*",),
            )
            bearer_config = _local_operator_bearer_config()
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=policy,
                record_store_factory=lambda: record_store,
                bearer_identity_config=bearer_config,
            )
            legacy_app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_RejectingVerifier(),
                authz_policy=policy,
                local_record_store_for_tests=record_store,
            )
            app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

            response = await _get_protected_artifacts(app, product="verireel")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("trace_id", payload)
        self.assertNotIn("authz", payload)


class FastApiDriverDescriptorTests(unittest.IsolatedAsyncioTestCase):
    async def test_driver_descriptors_return_provider_neutral_metadata(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_driver_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=_local_operator_bearer_config(),
        )

        list_response = await _get_driver_descriptors(app)
        show_response = await _get_driver_descriptor(app, "odoo")

        self.assertEqual(list_response.status_code, 200)
        list_payload = list_response.json()
        self.assertEqual(
            [driver["driver_id"] for driver in list_payload["drivers"]],
            ["generic-web", "ingress", "odoo", "verireel"],
        )
        ingress_driver = next(
            driver for driver in list_payload["drivers"] if driver["driver_id"] == "ingress"
        )
        self.assertEqual(ingress_driver["context_patterns"], [])
        self.assertNotIn("Dokploy", json.dumps(list_payload["drivers"]))
        self.assertTrue(str(list_payload["trace_id"]).startswith("launchplane_req_"))

        self.assertEqual(show_response.status_code, 200)
        show_payload = show_response.json()
        self.assertEqual(show_payload["driver"]["driver_id"], "odoo")
        rollback_actions = [
            action
            for action in show_payload["driver"]["actions"]
            if action["action_id"] == "prod_rollback"
        ]
        self.assertEqual(rollback_actions[0]["safety"], "destructive")

    async def test_driver_descriptor_returns_not_found_for_unknown_driver(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_driver_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=_local_operator_bearer_config(),
        )

        response = await _get_driver_descriptor(app, "missing")

        self.assertEqual(response.status_code, 404)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "not_found")

    async def test_driver_descriptors_require_bearer_or_human_identity(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_driver_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=_local_operator_bearer_config(),
        )

        response = await _get_driver_descriptors(app, authorization="")

        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authentication_required")
        self.assertEqual(response.headers["WWW-Authenticate"], 'Bearer realm="Launchplane API"')

    async def test_driver_descriptor_requires_bearer_or_human_identity(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_driver_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=_local_operator_bearer_config(),
        )

        response = await _get_driver_descriptor(app, "odoo", authorization="")

        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authentication_required")
        self.assertEqual(response.headers["WWW-Authenticate"], 'Bearer realm="Launchplane API"')

    async def test_driver_descriptors_reject_wrong_context_grant(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_driver_read_policy(context="other-context"),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=_local_operator_bearer_config(),
        )

        response = await _get_driver_descriptors(app)

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    async def test_driver_descriptor_rejects_wrong_context_grant(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_driver_read_policy(context="other-context"),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=_local_operator_bearer_config(),
        )

        response = await _get_driver_descriptor(app, "odoo")

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    async def test_driver_descriptors_accept_human_session_when_mounted_over_wsgi(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            oauth_config = _github_oauth_config()
            session_store = InMemoryHumanSessionStore()
            session_manager = HumanSessionManager(
                config=oauth_config,
                session_store=session_store,
            )
            human_session = session_manager.issue(_github_human_identity())
            policy = _github_human_driver_read_policy()
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=policy,
                record_store_factory=lambda: _MissingProductReadStore(),
                human_session_manager=session_manager,
            )
            legacy_app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_RejectingVerifier(),
                authz_policy=policy,
                github_oauth_config=oauth_config,
                human_session_store=session_store,
                control_plane_root_path=root,
            )
            app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

            response = await _get_driver_descriptors(
                app,
                authorization="",
                headers={"Cookie": session_manager.session_cookie_header(human_session)},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["drivers"][0]["driver_id"], "generic-web")
        self.assertNotIn("Set-Cookie", response.headers)

    async def test_openapi_includes_driver_descriptor_contracts(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_driver_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=_local_operator_bearer_config(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        list_route = openapi["paths"]["/v1/drivers"]["get"]
        show_route = openapi["paths"]["/v1/drivers/{driver_id}"]["get"]
        self.assertEqual(list_route["operationId"], "read_driver_descriptors")
        self.assertEqual(show_route["operationId"], "read_driver_descriptor")
        self.assertEqual(
            list_route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/DriverDescriptorsResponse",
        )
        self.assertEqual(
            show_route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/DriverDescriptorResponse",
        )
        self.assertIn("LaunchplaneErrorResponse", json.dumps(list_route))
        self.assertIn("LaunchplaneErrorResponse", json.dumps(show_route))
        self.assertEqual(
            openapi["components"]["schemas"]["DriverDescriptorsResponse"]["additionalProperties"],
            False,
        )
        self.assertEqual(
            openapi["components"]["schemas"]["DriverDescriptorResponse"]["additionalProperties"],
            False,
        )

    async def test_fastapi_driver_descriptors_precede_legacy_wsgi_fallback(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = _driver_read_policy()
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=policy,
                record_store_factory=lambda: _MissingProductReadStore(),
                bearer_identity_config=_local_operator_bearer_config(),
            )
            legacy_app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

            response = await _get_driver_descriptors(app)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("trace_id", payload)
        self.assertNotIn("authz", payload)


def _product_environment_read_policy(*, context: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "every/verireel",
                    "workflow_refs": [
                        "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                    ],
                    "event_names": ["pull_request"],
                    "products": ["example-site"],
                    "contexts": [context],
                    "actions": ["product_environment.read"],
                }
            ]
        }
    )


def _driver_read_policy(*, context: str = "launchplane") -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "local_operators": [
                {
                    "subjects": ["local-owner-agent"],
                    "token_labels": ["local-owner-read"],
                    "products": ["launchplane"],
                    "contexts": [context],
                    "actions": ["driver.read"],
                }
            ]
        }
    )


def _github_human_driver_read_policy(*, context: str = "launchplane") -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_humans": [
                {
                    "logins": ["example-operator"],
                    "roles": ["admin"],
                    "products": ["launchplane"],
                    "contexts": [context],
                    "actions": ["driver.read"],
                }
            ]
        }
    )


def _local_operator_product_environment_read_policy(*, context: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "local_operators": [
                {
                    "subjects": ["local-owner-agent"],
                    "token_labels": ["local-owner-read"],
                    "products": ["example-site"],
                    "contexts": [context],
                    "actions": ["product_environment.read"],
                }
            ]
        }
    )


def _local_operator_artifact_protection_policy(
    *,
    products: tuple[str, ...] = ("*",),
    contexts: tuple[str, ...] = ("*",),
) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "local_operators": [
                {
                    "subjects": ["local-owner-agent"],
                    "token_labels": ["local-owner-read"],
                    "products": list(products),
                    "contexts": list(contexts),
                    "actions": ["artifact_protection.read"],
                }
            ]
        }
    )


def _github_human_artifact_protection_policy(
    *,
    products: tuple[str, ...] = ("*",),
    contexts: tuple[str, ...] = ("*",),
) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_humans": [
                {
                    "logins": ["example-operator"],
                    "roles": ["admin"],
                    "products": list(products),
                    "contexts": list(contexts),
                    "actions": ["artifact_protection.read"],
                }
            ]
        }
    )


def _github_oauth_config() -> GitHubOAuthConfig:
    return GitHubOAuthConfig(
        client_id="example-client-id",
        client_secret="example-client-secret",
        public_url="https://launchplane.example",
        session_secret="example-session-secret",
        cookie_secure=False,
    )


def _github_human_identity() -> GitHubHumanIdentity:
    return GitHubHumanIdentity(
        login="example-operator",
        github_id=123,
        name="Example Operator",
        email="operator@example.com",
        organizations=frozenset({"example-org"}),
        teams=frozenset({"example-org/launchplane-operators"}),
        role="admin",
    )


def _local_operator_bearer_config() -> BearerIdentityConfig:
    return BearerIdentityConfig(
        local_operator_token="local-operator-token",
        local_operator_subject="local-owner-agent",
        local_operator_token_label="local-owner-read",
    )


@dataclass(frozen=True)
class _AsgiResponse:
    status_code: int
    headers: "_CaseInsensitiveHeaders"
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


async def _get_config_status(
    app: FastAPI,
    *,
    product: str = "example-site",
    environment: str = "prod",
    authorization: str = "Bearer valid-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    return await _asgi_get(
        app,
        f"/v1/products/{product}/environments/{environment}/config-status",
        headers=headers,
    )


async def _get_protected_artifacts(
    app: FastAPI,
    *,
    product: str,
    context: str = "",
    authorization: str = "Bearer local-operator-token",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    params: dict[str, str] = {}
    if product:
        params["product"] = product
    if context:
        params["context"] = context
    query_string = urlencode(params)
    suffix = f"?{query_string}" if query_string else ""
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    return await _asgi_get(
        app,
        f"/v1/artifacts/protected{suffix}",
        headers=request_headers,
    )


async def _get_driver_descriptors(
    app: FastAPI,
    *,
    authorization: str = "Bearer local-operator-token",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    return await _asgi_get(app, "/v1/drivers", headers=request_headers)


async def _get_driver_descriptor(
    app: FastAPI,
    driver_id: str,
    *,
    authorization: str = "Bearer local-operator-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    return await _asgi_get(app, f"/v1/drivers/{driver_id}", headers=headers)


async def _asgi_get(
    app: FastAPI, path: str, *, headers: dict[str, str] | None = None
) -> _AsgiResponse:
    request_path, separator, raw_query_string = path.partition("?")
    request_headers = [
        (key.lower().encode("ascii"), value.encode("latin-1"))
        for key, value in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": request_path,
        "raw_path": request_path.encode("ascii"),
        "query_string": raw_query_string.encode("ascii") if separator else b"",
        "headers": request_headers,
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    messages = [
        {"type": "http.request", "body": b"", "more_body": False},
    ]
    sent: list[MutableMapping[str, Any]] = []

    async def receive() -> dict[str, Any]:
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)

    start = next(message for message in sent if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"") for message in sent if message["type"] == "http.response.body"
    )
    response_headers = _CaseInsensitiveHeaders(
        {key.decode("latin-1"): value.decode("latin-1") for key, value in start.get("headers", [])}
    )
    return _AsgiResponse(
        status_code=start["status"],
        headers=response_headers,
        body=body,
    )


class _CaseInsensitiveHeaders(dict[str, str]):
    def __init__(self, headers: dict[str, str]) -> None:
        super().__init__((key.lower(), value) for key, value in headers.items())

    def __getitem__(self, key: str) -> str:
        return super().__getitem__(key.lower())


class _MissingProductReadStore:
    pass


class _RejectingVerifier:
    def verify(self, token: str) -> GitHubActionsIdentity:
        raise InvalidTokenError("signature verification failed")
