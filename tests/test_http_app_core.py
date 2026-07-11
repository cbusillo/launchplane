import base64
import hashlib
import json
import unittest
from datetime import (
    datetime,
    timedelta,
    timezone,
)
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import (
    parse_qs,
    urlparse,
)

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.routing import APIRoute

from control_plane.http_app import (
    LaunchplaneErrorResponse,
    LaunchplaneAuthzPolicyRuntime,
    _LaunchplaneFastAPI,
    _openapi_model_schema,
    create_launchplane_fastapi_app,
)
from control_plane.contracts.verireel_prod_backup_gate import VeriReelProdBackupGateRequest
from control_plane.contracts.verireel_prod_backup_gate_operation import (
    VeriReelProdBackupGateOperationRecord,
)
from control_plane.service_auth import (
    BearerIdentityConfig,
    GitHubHumanIdentity,
    LaunchplaneAuthzPolicy,
)
from control_plane.service_human_auth import (
    HumanSessionManager,
    InMemoryHumanSessionStore,
    LaunchplaneHumanSession,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.launchplane_self_deploy import LAUNCHPLANE_IMAGE_REFERENCE_ENV_KEY
from tests.http_app_test_support import (
    _asgi_get,
    _asgi_request,
    _driver_read_policy,
    _EmptyStore,
    _github_actions_launchplane_service_reconcile_policy,
    _github_human_identity,
    _github_oauth_config,
    _local_operator_bearer_config,
    _local_operator_launchplane_service_read_policy,
    _local_operator_launchplane_service_reconcile_policy,
    _MissingProductReadStore,
    _odoo_operation_status_identity,
    _odoo_operation_status_policy,
    _pending_odoo_stable_bootstrap_record,
    _product_environment_read_policy,
    _RejectingVerifier,
    _running_odoo_stable_bootstrap_record,
    _running_odoo_target_replacement_record,
    _stale_odoo_stable_bootstrap_record,
    _StubFastApiGitHubOAuthClient,
    _terminal_agent_launchplane_service_reconcile_policy,
)
from tests.test_service import (
    _identity,
    _sqlite_database_url,
    _StubVerifier,
)


class FastApiConstructionMetadataTests(unittest.TestCase):
    def test_openapi_model_schema_cache_returns_independent_payloads(self) -> None:
        first_schema = _openapi_model_schema(VeriReelProdBackupGateRequest)
        second_schema = _openapi_model_schema(VeriReelProdBackupGateRequest)

        self.assertEqual(first_schema, second_schema)
        self.assertIsNot(first_schema, second_schema)
        first_schema["properties"].clear()
        self.assertNotEqual(first_schema, second_schema)

    def test_error_response_metadata_matches_fastapi_variants(self) -> None:
        def read_example() -> dict[str, str]:
            return {"status": "ok"}

        baseline_app = FastAPI()
        optimized_app = _LaunchplaneFastAPI()
        for app in (baseline_app, optimized_app):
            app.add_api_route(
                "/hidden",
                read_example,
                methods=["GET"],
                include_in_schema=False,
                responses={400: {"model": LaunchplaneErrorResponse}},
            )
            app.add_api_route(
                "/anchor",
                read_example,
                methods=["GET"],
                responses={400: {"model": LaunchplaneErrorResponse}},
            )
            app.add_api_route(
                "/overlay",
                read_example,
                methods=["GET"],
                responses={
                    409: {
                        "model": LaunchplaneErrorResponse,
                        "content": {
                            "application/json": {"schema": {"description": "retained overlay"}}
                        },
                    }
                },
            )
            app.add_api_route(
                "/plain-text",
                read_example,
                methods=["GET"],
                response_class=PlainTextResponse,
                responses={503: {"model": LaunchplaneErrorResponse}},
            )

        baseline_openapi = baseline_app.openapi()
        optimized_openapi = optimized_app.openapi()

        self.assertEqual(optimized_openapi, baseline_openapi)
        overlay_schema = optimized_openapi["paths"]["/overlay"]["get"]["responses"]["409"][
            "content"
        ]["application/json"]["schema"]
        self.assertEqual(overlay_schema["description"], "retained overlay")
        self.assertEqual(
            optimized_openapi["paths"]["/plain-text"]["get"]["responses"]["503"]["content"][
                "text/plain"
            ]["schema"]["$ref"],
            "#/components/schemas/LaunchplaneErrorResponse",
        )

    def test_error_response_anchor_commits_after_route_registration(self) -> None:
        def read_example() -> dict[str, str]:
            return {"status": "ok"}

        app = _LaunchplaneFastAPI()
        with patch.object(FastAPI, "add_api_route", side_effect=RuntimeError("failed route")):
            with self.assertRaisesRegex(RuntimeError, "failed route"):
                app.add_api_route(
                    "/failed",
                    read_example,
                    methods=["GET"],
                    responses={400: {"model": LaunchplaneErrorResponse}},
                )

        app.add_api_route(
            "/valid",
            read_example,
            methods=["GET"],
            responses={400: {"model": LaunchplaneErrorResponse}},
        )
        openapi = app.openapi()

        self.assertEqual(
            openapi["paths"]["/valid"]["get"]["responses"]["400"]["content"]["application/json"][
                "schema"
            ]["$ref"],
            "#/components/schemas/LaunchplaneErrorResponse",
        )
        self.assertIn("LaunchplaneErrorResponse", openapi["components"]["schemas"])

    def test_error_response_metadata_reuses_one_model_field(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        model_response_count = 0
        referenced_response_count = 0
        for route in app.routes:
            if not isinstance(route, APIRoute) or not route.include_in_schema:
                continue
            for response in route.responses.values():
                if response.get("model") is LaunchplaneErrorResponse:
                    model_response_count += 1
                response_schema = (
                    response.get("content", {}).get("application/json", {}).get("schema", {})
                )
                if response_schema.get("$ref") == ("#/components/schemas/LaunchplaneErrorResponse"):
                    referenced_response_count += 1

        openapi = app.openapi()
        openapi_reference_count = 0
        for path_item in openapi["paths"].values():
            for operation in path_item.values():
                if not isinstance(operation, dict):
                    continue
                for response in operation.get("responses", {}).values():
                    response_schema = (
                        response.get("content", {}).get("application/json", {}).get("schema", {})
                    )
                    if response_schema.get("$ref") == (
                        "#/components/schemas/LaunchplaneErrorResponse"
                    ):
                        openapi_reference_count += 1

        self.assertEqual(model_response_count, 1)
        self.assertGreater(referenced_response_count, 500)
        self.assertEqual(
            openapi_reference_count,
            model_response_count + referenced_response_count,
        )
        self.assertIn(
            "LaunchplaneErrorResponse",
            openapi["components"]["schemas"],
        )


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


class FastApiAuthSessionReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_github_oauth_login_redirects_to_authorization_url(self) -> None:
        oauth_client = _StubFastApiGitHubOAuthClient(_github_human_identity())
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_local_operator_launchplane_service_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            human_session_manager=HumanSessionManager(
                config=_github_oauth_config(),
                session_store=InMemoryHumanSessionStore(),
            ),
            github_oauth_client=oauth_client,
        )

        response = await _asgi_get(app, "/auth/github/login?return_to=/ui")

        self.assertEqual(response.status_code, 302)
        location = response.headers["Location"]
        self.assertTrue(location.startswith("https://github.example/authorize?"))
        query = parse_qs(urlparse(location).query)
        self.assertEqual(query["state"], [oauth_client.authorization_state])
        self.assertTrue(query["challenge"][0])

    async def test_github_oauth_login_rejects_when_auth_is_not_configured(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_local_operator_launchplane_service_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/auth/github/login")

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "auth_not_configured")

    async def test_github_oauth_login_rejects_without_session_manager(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_local_operator_launchplane_service_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            github_oauth_client=_StubFastApiGitHubOAuthClient(_github_human_identity()),
        )

        response = await _asgi_get(app, "/auth/github/login")

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "auth_not_configured")

    async def test_github_oauth_callback_issues_session_cookie(self) -> None:
        session_store = InMemoryHumanSessionStore()
        session_manager = HumanSessionManager(
            config=_github_oauth_config(),
            session_store=session_store,
        )
        oauth_client = _StubFastApiGitHubOAuthClient(_github_human_identity())
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_local_operator_launchplane_service_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            human_session_manager=session_manager,
            github_oauth_client=oauth_client,
        )
        login_response = await _asgi_get(app, "/auth/github/login?return_to=/ui")
        state = parse_qs(urlparse(login_response.headers["Location"]).query)["state"][0]

        response = await _asgi_get(
            app,
            f"/auth/github/callback?code=github-code&state={state}",
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/ui")
        self.assertIn("launchplane_session=", response.headers["Set-Cookie"])
        self.assertIn("HttpOnly", response.headers["Set-Cookie"])
        self.assertIn("SameSite=Lax", response.headers["Set-Cookie"])
        self.assertTrue(oauth_client.code_verifier)

    async def test_github_oauth_callback_session_survives_app_recreation(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            session_manager = HumanSessionManager(
                config=_github_oauth_config(),
                session_store=store,
            )
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_local_operator_launchplane_service_read_policy(),
                record_store_factory=lambda: store,
                human_session_manager=session_manager,
                github_oauth_client=_StubFastApiGitHubOAuthClient(_github_human_identity()),
            )
            login_response = await _asgi_get(app, "/auth/github/login")
            state = parse_qs(urlparse(login_response.headers["Location"]).query)["state"][0]
            callback_response = await _asgi_get(
                app,
                f"/auth/github/callback?code=github-code&state={state}",
            )

            recreated_store = PostgresRecordStore(database_url=database_url)
            recreated_session_manager = HumanSessionManager(
                config=_github_oauth_config(),
                session_store=recreated_store,
            )
            recreated_app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_local_operator_launchplane_service_read_policy(),
                record_store_factory=lambda: recreated_store,
                human_session_manager=recreated_session_manager,
            )
            session_response = await _asgi_get(
                recreated_app,
                "/v1/auth/session",
                headers={"Cookie": callback_response.headers["Set-Cookie"]},
            )

        self.assertEqual(callback_response.status_code, 302)
        self.assertEqual(session_response.status_code, 200)
        payload = session_response.json()
        self.assertEqual(payload["identity"]["login"], "example-operator")
        self.assertEqual(payload["identity"]["role"], "admin")

    async def test_github_oauth_callback_sanitizes_external_return_to(self) -> None:
        session_manager = HumanSessionManager(
            config=_github_oauth_config(),
            session_store=InMemoryHumanSessionStore(),
        )
        oauth_client = _StubFastApiGitHubOAuthClient(_github_human_identity())
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_local_operator_launchplane_service_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            human_session_manager=session_manager,
            github_oauth_client=oauth_client,
        )
        login_response = await _asgi_get(
            app,
            "/auth/github/login?return_to=https%3A%2F%2Fevil.example%2Fui",
        )
        state = parse_qs(urlparse(login_response.headers["Location"]).query)["state"][0]

        response = await _asgi_get(
            app,
            f"/auth/github/callback?code=github-code&state={state}",
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/")

    async def test_github_oauth_callback_rejects_missing_code_or_state(self) -> None:
        session_manager = HumanSessionManager(
            config=_github_oauth_config(),
            session_store=InMemoryHumanSessionStore(),
        )
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_local_operator_launchplane_service_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            human_session_manager=session_manager,
            github_oauth_client=_StubFastApiGitHubOAuthClient(_github_human_identity()),
        )

        response = await _asgi_get(app, "/auth/github/callback?state=missing")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_oauth_callback")

    async def test_github_oauth_callback_rejects_reused_state(self) -> None:
        session_manager = HumanSessionManager(
            config=_github_oauth_config(),
            session_store=InMemoryHumanSessionStore(),
        )
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_local_operator_launchplane_service_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            human_session_manager=session_manager,
            github_oauth_client=_StubFastApiGitHubOAuthClient(_github_human_identity()),
        )
        login_response = await _asgi_get(app, "/auth/github/login")
        state = parse_qs(urlparse(login_response.headers["Location"]).query)["state"][0]
        await _asgi_get(app, f"/auth/github/callback?code=github-code&state={state}")

        response = await _asgi_get(
            app,
            f"/auth/github/callback?code=github-code&state={state}",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_oauth_callback")

    async def test_github_oauth_callback_rejects_unauthorized_identity(self) -> None:
        session_manager = HumanSessionManager(
            config=_github_oauth_config(),
            session_store=InMemoryHumanSessionStore(),
        )
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_local_operator_launchplane_service_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            human_session_manager=session_manager,
            github_oauth_client=_StubFastApiGitHubOAuthClient(
                _github_human_identity(), permission_error=True
            ),
        )
        login_response = await _asgi_get(app, "/auth/github/login")
        state = parse_qs(urlparse(login_response.headers["Location"]).query)["state"][0]

        response = await _asgi_get(
            app,
            f"/auth/github/callback?code=github-code&state={state}",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_github_oauth_callback_maps_client_failure(self) -> None:
        session_manager = HumanSessionManager(
            config=_github_oauth_config(),
            session_store=InMemoryHumanSessionStore(),
        )
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_local_operator_launchplane_service_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            human_session_manager=session_manager,
            github_oauth_client=_StubFastApiGitHubOAuthClient(
                _github_human_identity(), fail_fetch=True
            ),
        )
        login_response = await _asgi_get(app, "/auth/github/login")
        state = parse_qs(urlparse(login_response.headers["Location"]).query)["state"][0]

        with self.assertLogs("control_plane.http_app", level="ERROR") as captured_logs:
            response = await _asgi_get(
                app,
                f"/auth/github/callback?code=github-code&state={state}",
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_oauth_callback")
        self.assertTrue(
            any("GitHub OAuth callback failed" in entry for entry in captured_logs.output)
        )

    async def test_session_read_rejects_missing_human_session(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_local_operator_launchplane_service_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/v1/auth/session")

        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authentication_required")
        self.assertEqual(payload["error"]["message"], "Sign in with GitHub to access Launchplane.")
        self.assertFalse(payload["configured"])
        self.assertTrue(str(payload["trace_id"]).startswith("launchplane_req_"))
        self.assertNotIn("WWW-Authenticate", response.headers)

    async def test_session_read_rejects_missing_cookie_when_auth_is_configured(self) -> None:
        session_manager = HumanSessionManager(
            config=_github_oauth_config(),
            session_store=InMemoryHumanSessionStore(),
        )
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_local_operator_launchplane_service_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            human_session_manager=session_manager,
        )

        response = await _asgi_get(app, "/v1/auth/session")

        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertEqual(payload["error"]["code"], "authentication_required")
        self.assertTrue(payload["configured"])
        self.assertNotIn("WWW-Authenticate", response.headers)

    async def test_session_read_returns_github_human_identity(self) -> None:
        session_store = InMemoryHumanSessionStore()
        session_manager = HumanSessionManager(
            config=_github_oauth_config(),
            session_store=session_store,
        )
        human_session = session_manager.issue(
            GitHubHumanIdentity(
                login="example-operator",
                github_id=123,
                name="Example Operator",
                email="operator@example.com",
                organizations=frozenset({"z-org", "a-org"}),
                teams=frozenset({"z-org/admins", "a-org/operators"}),
                role="admin",
            )
        )
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_local_operator_launchplane_service_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            human_session_manager=session_manager,
        )

        response = await _asgi_get(
            app,
            "/v1/auth/session",
            headers={"Cookie": session_manager.session_cookie_header(human_session)},
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Set-Cookie", response.headers)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(str(payload["trace_id"]).startswith("launchplane_req_"))
        self.assertEqual(
            payload["identity"],
            {
                "provider": "github",
                "login": "example-operator",
                "github_id": 123,
                "name": "Example Operator",
                "email": "operator@example.com",
                "organizations": ["a-org", "z-org"],
                "teams": ["a-org/operators", "z-org/admins"],
                "role": "admin",
            },
        )

    async def test_session_read_renews_expiring_human_session(self) -> None:
        session_store = InMemoryHumanSessionStore()
        now = datetime.now(timezone.utc)
        session_manager = HumanSessionManager(
            config=_github_oauth_config(),
            session_store=session_store,
            now=lambda: now,
        )
        expiring_session = LaunchplaneHumanSession(
            session_id="expiring-session",
            identity=_github_human_identity(role="read_only"),
            created_at=now - timedelta(days=13),
            expires_at=now + timedelta(hours=12),
        )
        session_store.write_session(expiring_session)
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_local_operator_launchplane_service_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            human_session_manager=session_manager,
        )

        response = await _asgi_get(
            app,
            "/v1/auth/session",
            headers={"Cookie": session_manager.session_cookie_header(expiring_session)},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("launchplane_session=", response.headers["Set-Cookie"])
        self.assertIn("Max-Age=1209600", response.headers["Set-Cookie"])
        renewed_session = session_store.read_session("expiring-session")
        self.assertIsNotNone(renewed_session)
        assert renewed_session is not None
        self.assertGreater(renewed_session.expires_at, expiring_session.expires_at)

    async def test_logout_deletes_session_and_clears_cookie(self) -> None:
        session_store = InMemoryHumanSessionStore()
        session_manager = HumanSessionManager(
            config=_github_oauth_config(),
            session_store=session_store,
        )
        human_session = session_manager.issue(_github_human_identity())
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_local_operator_launchplane_service_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            human_session_manager=session_manager,
        )

        response = await _asgi_request(
            app,
            "POST",
            "/auth/logout",
            headers={"Cookie": session_manager.session_cookie_header(human_session)},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertTrue(str(response.json()["trace_id"]).startswith("launchplane_req_"))
        self.assertIsNone(session_store.read_session(human_session.session_id))
        self.assertIn("launchplane_session=", response.headers["Set-Cookie"])
        self.assertIn("Max-Age=0", response.headers["Set-Cookie"])
        self.assertIn("HttpOnly", response.headers["Set-Cookie"])
        self.assertIn("SameSite=Lax", response.headers["Set-Cookie"])
        self.assertNotIn("Secure", response.headers["Set-Cookie"])

    async def test_logout_clears_cookie_when_auth_is_not_configured(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_local_operator_launchplane_service_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_request(app, "POST", "/auth/logout")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertEqual(
            response.headers["Set-Cookie"],
            "launchplane_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax",
        )

    async def test_logout_clears_cookie_when_auth_is_configured_without_cookie(self) -> None:
        session_manager = HumanSessionManager(
            config=_github_oauth_config(),
            session_store=InMemoryHumanSessionStore(),
        )
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_local_operator_launchplane_service_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            human_session_manager=session_manager,
        )

        response = await _asgi_request(app, "POST", "/auth/logout")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertIn("launchplane_session=", response.headers["Set-Cookie"])
        self.assertIn("Max-Age=0", response.headers["Set-Cookie"])
        self.assertIn("HttpOnly", response.headers["Set-Cookie"])
        self.assertIn("SameSite=Lax", response.headers["Set-Cookie"])

    async def test_openapi_includes_auth_session_contract(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_local_operator_launchplane_service_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        route = openapi["paths"]["/v1/auth/session"]["get"]
        self.assertEqual(route["operationId"], "read_human_auth_session")
        success_schema = route["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertEqual(success_schema["$ref"], "#/components/schemas/AuthSessionResponse")
        rejected_schema = route["responses"]["401"]["content"]["application/json"]["schema"]
        self.assertEqual(
            rejected_schema["$ref"], "#/components/schemas/AuthSessionRequiredResponse"
        )
        self.assertEqual(
            openapi["components"]["schemas"]["AuthSessionResponse"]["additionalProperties"],
            False,
        )
        self.assertEqual(
            openapi["components"]["schemas"]["AuthSessionRequiredResponse"]["additionalProperties"],
            False,
        )
        logout_route = openapi["paths"]["/auth/logout"]["post"]
        self.assertEqual(logout_route["operationId"], "logout_human_auth_session")
        logout_schema = logout_route["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertEqual(logout_schema["$ref"], "#/components/schemas/AuthLogoutResponse")
        self.assertEqual(
            openapi["components"]["schemas"]["AuthLogoutResponse"]["additionalProperties"],
            False,
        )
        login_route = openapi["paths"]["/auth/github/login"]["get"]
        self.assertEqual(login_route["operationId"], "login_github_oauth")
        self.assertIn("302", login_route["responses"])
        login_rejected_schema = login_route["responses"]["503"]["content"]["application/json"][
            "schema"
        ]
        self.assertEqual(
            login_rejected_schema["$ref"], "#/components/schemas/LaunchplaneErrorResponse"
        )
        callback_route = openapi["paths"]["/auth/github/callback"]["get"]
        self.assertEqual(callback_route["operationId"], "complete_github_oauth_callback")
        self.assertIn("302", callback_route["responses"])
        for status_code in ("400", "403", "503"):
            callback_schema = callback_route["responses"][status_code]["content"][
                "application/json"
            ]["schema"]
            self.assertEqual(
                callback_schema["$ref"], "#/components/schemas/LaunchplaneErrorResponse"
            )


class FastApiOperatorUiTests(unittest.IsolatedAsyncioTestCase):
    async def test_ui_route_serves_static_shell_without_authentication(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            ui_root = root / "control_plane" / "ui_static"
            asset_root = ui_root / "assets"
            asset_root.mkdir(parents=True)
            (ui_root / "index.html").write_text(
                '<html><head><script type="module" src="/ui/assets/app.js"></script></head></html>',
                encoding="utf-8",
            )
            (asset_root / "app.js").write_text("console.log('launchplane ui');\n", encoding="utf-8")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                record_store_factory=lambda: _MissingProductReadStore(),
                control_plane_root_path=root,
            )

            shell_response = await _asgi_get(app, "/ui")
            asset_response = await _asgi_get(app, "/ui/assets/app.js")

        self.assertEqual(shell_response.status_code, 200)
        self.assertEqual(shell_response.headers["Content-Type"], "text/html")
        self.assertIn(b"/ui/assets/app.js", shell_response.body)
        self.assertEqual(shell_response.headers["Cache-Control"], "no-store")
        self.assertEqual(asset_response.status_code, 200)
        self.assertIn(
            asset_response.headers["Content-Type"],
            {"text/javascript", "application/javascript"},
        )
        self.assertIn(b"launchplane ui", asset_response.body)
        self.assertEqual(
            asset_response.headers["Cache-Control"],
            "public, max-age=31536000, immutable",
        )

    async def test_root_route_serves_ui_shell_without_authentication(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            ui_root = root / "control_plane" / "ui_static"
            ui_root.mkdir(parents=True)
            (ui_root / "index.html").write_text(
                '<html><head><script type="module" src="/ui/assets/app.js"></script></head></html>',
                encoding="utf-8",
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                record_store_factory=lambda: _MissingProductReadStore(),
                control_plane_root_path=root,
            )

            response = await _asgi_get(app, "/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Content-Type"], "text/html")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn(b"/ui/assets/app.js", response.body)

    async def test_ui_route_falls_back_to_shell_for_nested_paths(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            ui_root = root / "control_plane" / "ui_static"
            ui_root.mkdir(parents=True)
            (ui_root / "index.html").write_text("<html>Launchplane UI</html>", encoding="utf-8")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                record_store_factory=lambda: _MissingProductReadStore(),
                control_plane_root_path=root,
            )

            response = await _asgi_get(app, "/ui/contexts/verireel")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Content-Type"], "text/html")
        self.assertIn(b"Launchplane UI", response.body)

    async def test_ui_asset_route_rejects_parent_directory_segments(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            ui_root = root / "control_plane" / "ui_static"
            ui_root.mkdir(parents=True)
            (ui_root / "index.html").write_text("<html>Launchplane UI</html>", encoding="utf-8")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                record_store_factory=lambda: _MissingProductReadStore(),
                control_plane_root_path=root,
            )

            response = await _asgi_get(app, "/ui/assets/%2e%2e/index.html")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.headers["Content-Type"], "application/json")
        self.assertNotIn(b"Launchplane UI", response.body)

    async def test_unknown_route_uses_launchplane_error_envelope(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/missing-route")

        self.assertEqual(response.status_code, 404)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertTrue(str(payload["trace_id"]).startswith("launchplane_req_"))
        self.assertEqual(payload["error"]["code"], "not_found")
        self.assertEqual(payload["error"]["message"], "No Launchplane route for /missing-route.")

    async def test_unsupported_method_uses_launchplane_error_envelope(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_request(app, "PUT", "/v1/health")

        self.assertEqual(response.status_code, 405)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "method_not_allowed")


class FastApiServiceRuntimeReadTests(unittest.IsolatedAsyncioTestCase):
    def _verireel_operation_record(
        self,
        *,
        status: str = "pending",
        phase: str = "created",
        lease_owner: str = "",
        lease_expires_at: str = "",
        heartbeat_at: str = "",
        attempt: int = 0,
    ) -> VeriReelProdBackupGateOperationRecord:
        request = VeriReelProdBackupGateRequest(
            backup_record_id="backup-gate-verireel-prod-run-12345-attempt-1"
        )
        return VeriReelProdBackupGateOperationRecord.model_validate(
            {
                "operation_id": "verireel-operation-1",
                "product": "verireel",
                "context": "verireel",
                "instance": "prod",
                "backup_record_id": request.backup_record_id,
                "request_fingerprint": request.model_dump_json(),
                "request": request.model_dump(mode="json"),
                "status": status,
                "phase": phase,
                "created_at": "2026-04-25T00:00:00Z",
                "updated_at": "2026-04-25T00:01:00Z",
                "started_at": "2026-04-25T00:01:00Z" if lease_owner else "",
                "lease_owner": lease_owner,
                "lease_expires_at": lease_expires_at,
                "heartbeat_at": heartbeat_at,
                "attempt": attempt,
            }
        )

    def _local_operator_launchplane_service_verireel_reconcile_policy(
        self,
    ) -> LaunchplaneAuthzPolicy:
        return LaunchplaneAuthzPolicy.model_validate(
            {
                "local_operators": [
                    {
                        "subjects": ["local-owner-agent"],
                        "token_labels": ["local-owner-write"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": ["launchplane_service.reconcile_verireel_workers"],
                    }
                ]
            }
        )

    async def test_runtime_reports_current_image_and_policy_metadata(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            policy = _local_operator_launchplane_service_read_policy()
            policy_text = "schema_version = 1\n"
            record_store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            authz_policy_runtime = LaunchplaneAuthzPolicyRuntime(
                policy,
                policy_sha256="resolved-policy-sha256",
                source="db",
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                authz_policy_runtime=authz_policy_runtime,
                record_store_factory=lambda: record_store,
                bearer_identity_config=_local_operator_bearer_config(),
            )
            with patch.dict(
                "os.environ",
                {
                    LAUNCHPLANE_IMAGE_REFERENCE_ENV_KEY: "ghcr.io/example/launchplane@sha256:test",
                    "LAUNCHPLANE_SERVICE_AUDIENCE": "launchplane.example.com",
                    "LAUNCHPLANE_POLICY_B64": base64.b64encode(policy_text.encode("utf-8")).decode(
                        "ascii"
                    ),
                },
                clear=True,
            ):
                response = await _asgi_get(
                    app,
                    "/v1/service/runtime",
                    headers={"Authorization": "Bearer local-operator-token"},
                )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["trace_id"].startswith("launchplane_req_"))
        runtime = payload["runtime"]
        self.assertEqual(runtime["storage_backend"], "filesystem")
        self.assertEqual(
            runtime["docker_image_reference"],
            "ghcr.io/example/launchplane@sha256:test",
        )
        self.assertEqual(runtime["service_audience"], "launchplane.example.com")
        self.assertEqual(runtime["authz_policy_sha256"], "resolved-policy-sha256")
        self.assertEqual(runtime["authz_policy_source"], "db")
        self.assertEqual(
            runtime["bootstrap_authz_policy_sha256"],
            hashlib.sha256(policy_text.encode("utf-8")).hexdigest(),
        )

    async def test_worker_status_reports_queue_status(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            record_store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            record_store.write_odoo_stable_bootstrap_operation_record(
                _pending_odoo_stable_bootstrap_record()
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_local_operator_launchplane_service_read_policy(),
                record_store_factory=lambda: record_store,
                bearer_identity_config=_local_operator_bearer_config(),
            )

            response = await _asgi_get(
                app,
                "/v1/service/odoo-workers/status",
                headers={"Authorization": "Bearer local-operator-token"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["trace_id"].startswith("launchplane_req_"))
        worker_status = payload["worker_status"]
        self.assertEqual(worker_status["status"], "ok")
        self.assertEqual(worker_status["pending_count"], 1)
        self.assertEqual(worker_status["running_count"], 0)
        self.assertEqual(worker_status["operations"][0]["operation_id"], "bootstrap-cm-testing")
        self.assertNotIn("request", worker_status["operations"][0])

    async def test_verireel_worker_status_reports_queue_status(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            record_store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            record_store.write_verireel_prod_backup_gate_operation_record(
                self._verireel_operation_record()
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_local_operator_launchplane_service_read_policy(),
                record_store_factory=lambda: record_store,
                bearer_identity_config=_local_operator_bearer_config(),
            )

            response = await _asgi_get(
                app,
                "/v1/service/verireel-workers/status",
                headers={"Authorization": "Bearer local-operator-token"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["trace_id"].startswith("launchplane_req_"))
        worker_status = payload["worker_status"]
        self.assertEqual(worker_status["status"], "ok")
        self.assertEqual(worker_status["pending_count"], 1)
        self.assertEqual(worker_status["running_count"], 0)
        self.assertEqual(worker_status["operations"][0]["operation_id"], "verireel-operation-1")
        self.assertEqual(
            worker_status["operations"][0]["backup_record_id"],
            "backup-gate-verireel-prod-run-12345-attempt-1",
        )
        self.assertNotIn("request", worker_status["operations"][0])

    async def test_verireel_worker_status_reports_stalled_queue_status(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            record_store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            record_store.write_verireel_prod_backup_gate_operation_record(
                self._verireel_operation_record(
                    status="running",
                    phase="backup_gate",
                    lease_owner="old-worker",
                    lease_expires_at="2000-01-01T00:00:00Z",
                    heartbeat_at="2000-01-01T00:00:00Z",
                    attempt=1,
                )
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_local_operator_launchplane_service_read_policy(),
                record_store_factory=lambda: record_store,
                bearer_identity_config=_local_operator_bearer_config(),
            )

            response = await _asgi_get(
                app,
                "/v1/service/verireel-workers/status",
                headers={"Authorization": "Bearer local-operator-token"},
            )

        self.assertEqual(response.status_code, 200)
        worker_status = response.json()["worker_status"]
        self.assertEqual(worker_status["status"], "stalled")
        self.assertEqual(worker_status["running_count"], 1)
        self.assertEqual(worker_status["stalled_count"], 1)
        self.assertTrue(worker_status["operations"][0]["lease_expired"])

    async def test_worker_reconcile_recovers_stale_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            record_store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            record_store.write_odoo_stable_bootstrap_operation_record(
                _stale_odoo_stable_bootstrap_record()
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_local_operator_launchplane_service_reconcile_policy(),
                record_store_factory=lambda: record_store,
                bearer_identity_config=_local_operator_bearer_config(
                    token_label="local-owner-write"
                ),
            )

            response = await _asgi_request(
                app,
                "POST",
                "/v1/service/odoo-workers/reconcile",
                headers={"Authorization": "Bearer local-operator-token"},
                payload={},
            )
            operation = record_store.read_odoo_stable_bootstrap_operation_record(
                "bootstrap-cm-testing"
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["trace_id"].startswith("launchplane_req_"))
        self.assertEqual(
            payload["reconcile_result"],
            {
                "reconciled_bootstrap_ids": ["bootstrap-cm-testing"],
                "reconciled_replacement_ids": [],
                "reconciled_count": 1,
            },
        )
        self.assertEqual(operation.status, "pending")
        self.assertEqual(operation.lease_owner, "")

    async def test_verireel_worker_reconcile_recovers_stale_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            record_store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            record_store.write_verireel_prod_backup_gate_operation_record(
                self._verireel_operation_record(
                    status="running",
                    phase="created",
                    lease_owner="old-worker",
                    lease_expires_at="2000-01-01T00:00:00Z",
                    heartbeat_at="2000-01-01T00:00:00Z",
                    attempt=1,
                )
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=self._local_operator_launchplane_service_verireel_reconcile_policy(),
                record_store_factory=lambda: record_store,
                bearer_identity_config=_local_operator_bearer_config(
                    token_label="local-owner-write"
                ),
            )

            response = await _asgi_request(
                app,
                "POST",
                "/v1/service/verireel-workers/reconcile",
                headers={"Authorization": "Bearer local-operator-token"},
                payload={},
            )
            operation = record_store.read_verireel_prod_backup_gate_operation_record(
                "verireel-operation-1"
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["trace_id"].startswith("launchplane_req_"))
        self.assertEqual(
            payload["reconcile_result"],
            {
                "reconciled_operation_ids": ["verireel-operation-1"],
                "reconciled_count": 1,
            },
        )
        self.assertEqual(operation.status, "pending")
        self.assertEqual(operation.lease_owner, "")

    async def test_worker_reconcile_accepts_github_actions_identity(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            record_store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            record_store.write_odoo_stable_bootstrap_operation_record(
                _stale_odoo_stable_bootstrap_record()
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_github_actions_launchplane_service_reconcile_policy(),
                record_store_factory=lambda: record_store,
            )

            response = await _asgi_request(
                app,
                "POST",
                "/v1/service/odoo-workers/reconcile",
                headers={"Authorization": "Bearer valid-token"},
                payload={},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["reconcile_result"]["reconciled_bootstrap_ids"],
            ["bootstrap-cm-testing"],
        )

    async def test_worker_reconcile_requires_reconcile_authz(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_local_operator_launchplane_service_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=_local_operator_bearer_config(token_label="local-owner-write"),
        )

        response = await _asgi_request(
            app,
            "POST",
            "/v1/service/odoo-workers/reconcile",
            headers={"Authorization": "Bearer local-operator-token"},
            payload={},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_verireel_worker_reconcile_requires_verireel_reconcile_authz(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_local_operator_launchplane_service_reconcile_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=_local_operator_bearer_config(token_label="local-owner-write"),
        )

        response = await _asgi_request(
            app,
            "POST",
            "/v1/service/verireel-workers/reconcile",
            headers={"Authorization": "Bearer local-operator-token"},
            payload={},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_worker_reconcile_rejects_terminal_agent_mutation(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_terminal_agent_launchplane_service_reconcile_policy(),
            record_store_factory=lambda: _EmptyStore(),
            bearer_identity_config=BearerIdentityConfig(
                terminal_agent_token="terminal-agent-token",
                terminal_agent_subject="local-owner-agent",
                terminal_agent_token_label="local-owner-read",
            ),
        )

        response = await _asgi_request(
            app,
            "POST",
            "/v1/service/odoo-workers/reconcile",
            headers={"Authorization": "Bearer terminal-agent-token"},
            payload={},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_verireel_worker_reconcile_rejects_terminal_agent_mutation(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=self._local_operator_launchplane_service_verireel_reconcile_policy(),
            record_store_factory=lambda: _EmptyStore(),
            bearer_identity_config=BearerIdentityConfig(
                terminal_agent_token="terminal-agent-token",
                terminal_agent_subject="local-owner-agent",
                terminal_agent_token_label="local-owner-read",
            ),
        )

        response = await _asgi_request(
            app,
            "POST",
            "/v1/service/verireel-workers/reconcile",
            headers={"Authorization": "Bearer terminal-agent-token"},
            payload={},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_worker_reconcile_validates_max_attempts(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_local_operator_launchplane_service_reconcile_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=_local_operator_bearer_config(token_label="local-owner-write"),
        )

        response = await _asgi_request(
            app,
            "POST",
            "/v1/service/odoo-workers/reconcile?max_attempts=0",
            headers={"Authorization": "Bearer local-operator-token"},
            payload={},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_query")

    async def test_verireel_worker_reconcile_validates_max_attempts(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=self._local_operator_launchplane_service_verireel_reconcile_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=_local_operator_bearer_config(token_label="local-owner-write"),
        )

        response = await _asgi_request(
            app,
            "POST",
            "/v1/service/verireel-workers/reconcile?max_attempts=0",
            headers={"Authorization": "Bearer local-operator-token"},
            payload={},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_query")

    async def test_worker_reconcile_requires_operation_record_storage(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_local_operator_launchplane_service_reconcile_policy(),
            record_store_factory=lambda: _EmptyStore(),
            bearer_identity_config=_local_operator_bearer_config(token_label="local-owner-write"),
        )

        response = await _asgi_request(
            app,
            "POST",
            "/v1/service/odoo-workers/reconcile",
            headers={"Authorization": "Bearer local-operator-token"},
            payload={},
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "operation_record_storage_required")

    async def test_verireel_worker_reconcile_requires_operation_record_storage(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=self._local_operator_launchplane_service_verireel_reconcile_policy(),
            record_store_factory=lambda: _EmptyStore(),
            bearer_identity_config=_local_operator_bearer_config(token_label="local-owner-write"),
        )

        response = await _asgi_request(
            app,
            "POST",
            "/v1/service/verireel-workers/reconcile",
            headers={"Authorization": "Bearer local-operator-token"},
            payload={},
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "operation_record_storage_required")

    async def test_worker_status_requires_service_read_authz(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_driver_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=_local_operator_bearer_config(),
        )

        response = await _asgi_get(
            app,
            "/v1/service/odoo-workers/status",
            headers={"Authorization": "Bearer local-operator-token"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_verireel_worker_status_requires_service_read_authz(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_driver_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=_local_operator_bearer_config(),
        )

        response = await _asgi_get(
            app,
            "/v1/service/verireel-workers/status",
            headers={"Authorization": "Bearer local-operator-token"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_service_runtime_routes_require_authentication(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_local_operator_launchplane_service_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=_local_operator_bearer_config(),
        )

        runtime_response = await _asgi_get(app, "/v1/service/runtime")
        worker_response = await _asgi_get(app, "/v1/service/odoo-workers/status")
        verireel_worker_response = await _asgi_get(app, "/v1/service/verireel-workers/status")
        reconcile_response = await _asgi_request(
            app,
            "POST",
            "/v1/service/odoo-workers/reconcile",
            payload={},
        )
        verireel_reconcile_response = await _asgi_request(
            app,
            "POST",
            "/v1/service/verireel-workers/reconcile",
            payload={},
        )

        self.assertEqual(runtime_response.status_code, 401)
        self.assertEqual(worker_response.status_code, 401)
        self.assertEqual(verireel_worker_response.status_code, 401)
        self.assertEqual(reconcile_response.status_code, 401)
        self.assertEqual(verireel_reconcile_response.status_code, 401)
        self.assertEqual(runtime_response.json()["error"]["code"], "authentication_required")
        self.assertEqual(worker_response.json()["error"]["code"], "authentication_required")
        self.assertEqual(
            verireel_worker_response.json()["error"]["code"], "authentication_required"
        )
        self.assertEqual(reconcile_response.json()["error"]["code"], "authentication_required")
        self.assertEqual(
            verireel_reconcile_response.json()["error"]["code"], "authentication_required"
        )

    async def test_runtime_rejects_human_session_without_service_read_authz(self) -> None:
        oauth_config = _github_oauth_config()
        session_store = InMemoryHumanSessionStore()
        session_manager = HumanSessionManager(config=oauth_config, session_store=session_store)
        human_session = session_manager.issue(_github_human_identity(role="read_only"))
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_humans": [
                        {
                            "logins": ["example-operator"],
                            "roles": ["read_only"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["driver.read"],
                        }
                    ]
                }
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
            human_session_manager=session_manager,
        )

        response = await _asgi_get(
            app,
            "/v1/service/runtime",
            headers={"Cookie": session_manager.session_cookie_header(human_session)},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_worker_status_validates_recent_terminal_limit(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            record_store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_local_operator_launchplane_service_read_policy(),
                record_store_factory=lambda: record_store,
                bearer_identity_config=_local_operator_bearer_config(),
            )

            response = await _asgi_get(
                app,
                "/v1/service/odoo-workers/status?recent_terminal_limit=101",
                headers={"Authorization": "Bearer local-operator-token"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_query")

    async def test_verireel_worker_status_validates_recent_terminal_limit(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            record_store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_local_operator_launchplane_service_read_policy(),
                record_store_factory=lambda: record_store,
                bearer_identity_config=_local_operator_bearer_config(),
            )

            response = await _asgi_get(
                app,
                "/v1/service/verireel-workers/status?recent_terminal_limit=101",
                headers={"Authorization": "Bearer local-operator-token"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_query")

    async def test_worker_status_requires_operation_record_storage(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_local_operator_launchplane_service_read_policy(),
            record_store_factory=lambda: _EmptyStore(),
            bearer_identity_config=_local_operator_bearer_config(),
        )

        response = await _asgi_get(
            app,
            "/v1/service/odoo-workers/status",
            headers={"Authorization": "Bearer local-operator-token"},
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "operation_record_storage_required")

    async def test_verireel_worker_status_requires_operation_record_storage(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_local_operator_launchplane_service_read_policy(),
            record_store_factory=lambda: _EmptyStore(),
            bearer_identity_config=_local_operator_bearer_config(),
        )

        response = await _asgi_get(
            app,
            "/v1/service/verireel-workers/status",
            headers={"Authorization": "Bearer local-operator-token"},
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "operation_record_storage_required")

    async def test_openapi_includes_service_runtime_contracts(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_local_operator_launchplane_service_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=_local_operator_bearer_config(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        runtime_route = openapi["paths"]["/v1/service/runtime"]["get"]
        worker_route = openapi["paths"]["/v1/service/odoo-workers/status"]["get"]
        reconcile_route = openapi["paths"]["/v1/service/odoo-workers/reconcile"]["post"]
        verireel_worker_route = openapi["paths"]["/v1/service/verireel-workers/status"]["get"]
        verireel_reconcile_route = openapi["paths"]["/v1/service/verireel-workers/reconcile"][
            "post"
        ]
        self.assertEqual(runtime_route["operationId"], "read_launchplane_runtime")
        self.assertEqual(
            runtime_route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/LaunchplaneRuntimeResponse",
        )
        self.assertEqual(
            worker_route["operationId"],
            "read_odoo_stable_operation_worker_status",
        )
        self.assertEqual(
            worker_route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/OdooStableOperationWorkerStatusResponse",
        )
        self.assertEqual(
            reconcile_route["operationId"],
            "reconcile_odoo_stable_operation_workers",
        )
        self.assertEqual(
            reconcile_route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/OdooStableOperationWorkerReconcileResponse",
        )
        self.assertEqual(
            verireel_worker_route["operationId"],
            "read_verireel_prod_backup_gate_operation_worker_status",
        )
        self.assertEqual(
            verireel_worker_route["responses"]["200"]["content"]["application/json"]["schema"][
                "$ref"
            ],
            "#/components/schemas/VeriReelProdBackupGateOperationWorkerStatusResponse",
        )
        self.assertEqual(
            verireel_reconcile_route["operationId"],
            "reconcile_verireel_prod_backup_gate_operation_workers",
        )
        self.assertEqual(
            verireel_reconcile_route["responses"]["200"]["content"]["application/json"]["schema"][
                "$ref"
            ],
            "#/components/schemas/VeriReelProdBackupGateOperationWorkerReconcileResponse",
        )
        self.assertEqual(
            openapi["components"]["schemas"]["LaunchplaneRuntimeResponse"]["additionalProperties"],
            False,
        )
        self.assertEqual(
            openapi["components"]["schemas"]["OdooStableOperationWorkerStatusResponse"][
                "additionalProperties"
            ],
            False,
        )
        self.assertEqual(
            openapi["components"]["schemas"]["OdooStableOperationWorkerReconcileResponse"][
                "additionalProperties"
            ],
            False,
        )
        self.assertEqual(
            openapi["components"]["schemas"]["VeriReelProdBackupGateOperationWorkerStatusResponse"][
                "additionalProperties"
            ],
            False,
        )
        self.assertEqual(
            openapi["components"]["schemas"][
                "VeriReelProdBackupGateOperationWorkerReconcileResponse"
            ]["additionalProperties"],
            False,
        )
        self.assertTrue(set(runtime_route["responses"]) >= {"200", "401", "403"})
        self.assertTrue(set(worker_route["responses"]) >= {"200", "400", "401", "403", "503"})
        self.assertTrue(set(reconcile_route["responses"]) >= {"200", "400", "401", "403", "503"})
        self.assertTrue(
            set(verireel_worker_route["responses"]) >= {"200", "400", "401", "403", "503"}
        )
        self.assertTrue(
            set(verireel_reconcile_route["responses"]) >= {"200", "400", "401", "403", "503"}
        )


class FastApiOdooOperationStatusReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_stable_bootstrap_operation_status_returns_native_payload(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            record_store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            record_store.write_odoo_stable_bootstrap_operation_record(
                _running_odoo_stable_bootstrap_record()
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_odoo_operation_status_identity()),
                authz_policy=_odoo_operation_status_policy(action="odoo_stable_bootstrap.execute"),
                record_store_factory=lambda: record_store,
            )

            response = await _asgi_get(
                app,
                "/v1/drivers/odoo/stable-bootstrap/operations/operation-cm-testing",
                headers={"Authorization": "Bearer valid-token"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["trace_id"].startswith("launchplane_req_"))
        self.assertEqual(payload["operation"]["operation_id"], "operation-cm-testing")
        self.assertEqual(payload["operation"]["status"], "running")
        self.assertEqual(
            payload["operation"]["poll_url"],
            "/v1/drivers/odoo/stable-bootstrap/operations/operation-cm-testing",
        )
        self.assertNotIn("result", payload)

    async def test_target_replacement_operation_status_returns_native_payload(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            record_store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            record_store.write_odoo_stable_target_replacement_operation_record(
                _running_odoo_target_replacement_record()
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_odoo_operation_status_identity()),
                authz_policy=_odoo_operation_status_policy(
                    action="odoo_target_replacement_apply.execute"
                ),
                record_store_factory=lambda: record_store,
            )

            response = await _asgi_get(
                app,
                "/v1/drivers/odoo/target-replacement/operations/operation-cm-testing",
                headers={"Authorization": "Bearer valid-token"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["trace_id"].startswith("launchplane_req_"))
        self.assertEqual(payload["operation"]["operation_id"], "operation-cm-testing")
        self.assertEqual(payload["operation"]["status"], "running")
        self.assertEqual(
            payload["operation"]["poll_url"],
            "/v1/drivers/odoo/target-replacement/operations/operation-cm-testing",
        )
        self.assertNotIn("result", payload)

    async def test_operation_status_routes_require_authentication(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_odoo_operation_status_identity()),
            authz_policy=_odoo_operation_status_policy(action="odoo_stable_bootstrap.execute"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(
            app,
            "/v1/drivers/odoo/stable-bootstrap/operations/operation-cm-testing",
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "authentication_required")

    async def test_operation_status_authorizes_against_stored_operation_context(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            record_store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            record_store.write_odoo_stable_bootstrap_operation_record(
                _running_odoo_stable_bootstrap_record()
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_odoo_operation_status_identity()),
                authz_policy=_odoo_operation_status_policy(
                    action="odoo_stable_bootstrap.execute",
                    contexts=("prod",),
                ),
                record_store_factory=lambda: record_store,
            )

            response = await _asgi_get(
                app,
                "/v1/drivers/odoo/stable-bootstrap/operations/operation-cm-testing",
                headers={"Authorization": "Bearer valid-token"},
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_operation_status_missing_record_returns_not_found(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            record_store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_odoo_operation_status_identity()),
                authz_policy=_odoo_operation_status_policy(action="odoo_stable_bootstrap.execute"),
                record_store_factory=lambda: record_store,
            )

            response = await _asgi_get(
                app,
                "/v1/drivers/odoo/stable-bootstrap/operations/missing-operation",
                headers={"Authorization": "Bearer valid-token"},
            )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")

    async def test_operation_status_requires_only_read_operation_storage(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_odoo_operation_status_identity()),
            authz_policy=_odoo_operation_status_policy(action="odoo_stable_bootstrap.execute"),
            record_store_factory=lambda: _EmptyStore(),
        )

        response = await _asgi_get(
            app,
            "/v1/drivers/odoo/stable-bootstrap/operations/operation-cm-testing",
            headers={"Authorization": "Bearer valid-token"},
        )

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["error"]["code"], "database_storage_required")
        self.assertIn("read_odoo_stable_bootstrap_operation_record", payload["error"]["message"])

    async def test_openapi_includes_odoo_operation_status_contracts(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_odoo_operation_status_identity()),
            authz_policy=_odoo_operation_status_policy(action="odoo_stable_bootstrap.execute"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        bootstrap_route = openapi["paths"][
            "/v1/drivers/odoo/stable-bootstrap/operations/{operation_id}"
        ]["get"]
        replacement_route = openapi["paths"][
            "/v1/drivers/odoo/target-replacement/operations/{operation_id}"
        ]["get"]
        self.assertEqual(
            bootstrap_route["operationId"],
            "read_odoo_stable_bootstrap_operation_status",
        )
        self.assertEqual(
            replacement_route["operationId"],
            "read_odoo_target_replacement_operation_status",
        )
        self.assertEqual(
            bootstrap_route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/OdooStableBootstrapOperationStatusResponse",
        )
        self.assertEqual(
            replacement_route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/OdooStableTargetReplacementOperationStatusResponse",
        )
        self.assertTrue(set(bootstrap_route["responses"]) >= {"200", "401", "403", "404", "503"})
        self.assertTrue(set(replacement_route["responses"]) >= {"200", "401", "403", "404", "503"})
