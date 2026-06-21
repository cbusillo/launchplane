import base64
import hashlib
import json
import os
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from collections.abc import Callable, MutableMapping
from typing import Any, Literal, cast
from urllib.parse import parse_qs, urlencode, urlparse
from unittest.mock import patch

from a2wsgi import WSGIMiddleware
from click import ClickException
from fastapi import FastAPI
from jwt import InvalidTokenError
from starlette.types import ASGIApp

from control_plane import secrets as control_plane_secrets
from control_plane.contracts.agent_write_intent import (
    AgentWriteIntentRecord,
    AgentWriteIntentRequest,
    build_agent_write_intent_record_id,
    evaluate_agent_write_intent,
)
from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.deployment_record import DeploymentRecord, ResolvedTargetEvidence
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.every_code_notifications import (
    EveryCodeNotificationAttemptRecord,
    EveryCodeNotificationDestination,
    EveryCodeNotificationPolicyRecord,
)
from control_plane.contracts.every_code_preview_gate_record import EveryCodePreviewGateRecord
from control_plane.contracts.every_code_pr_feedback_record import EveryCodePrFeedbackRecord
from control_plane.contracts.every_code_work_request import (
    EveryCodeWorkRequestRecord,
    EveryCodeWorkRequestStatusUpdate,
    apply_every_code_work_request_status,
)
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.ingress_route_audit_record import (
    IngressRouteAuditOperation,
    IngressRouteAuditRecord,
)
from control_plane.contracts.merge_train_policy import (
    MergeTrainPolicyRecord,
    parse_merge_train_policy_toml,
)
from control_plane.contracts.odoo_stable_bootstrap_operation import (
    OdooStableBootstrapOperationRecord,
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.preview_desired_state_record import PreviewDesiredStateRecord
from control_plane.contracts.preview_generation_record import (
    PreviewGenerationState,
    PreviewGenerationRecord,
    PreviewPullRequestSummary,
)
from control_plane.contracts.preview_lifecycle_plan_record import PreviewLifecycleDesiredPreview
from control_plane.contracts.preview_pr_feedback_notifications import (
    PreviewPrFeedbackNotificationAttemptRecord,
    PreviewPrFeedbackNotificationDestination,
    PreviewPrFeedbackNotificationPolicyRecord,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressIncidentRecord,
    PublicIngressNotificationDestination,
    PublicIngressNotificationPolicyRecord,
)
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    DeploymentEvidence,
    PromotionRecord,
)
from control_plane.contracts.runner_host_hygiene import (
    RunnerHostHygieneApplyAuditRecord,
    RunnerHostHygieneApplyPolicy,
    RunnerHostHygieneApplyRequest,
    RunnerHostHygieneObservation,
    RunnerHostHygienePolicy,
    evaluate_runner_host_hygiene,
    plan_runner_host_hygiene_apply,
)
from control_plane.contracts.runner_lane_inventory import build_runner_lane_inventory
from control_plane.contracts.runner_lane_registration import (
    RunnerLaneRegistrationAuditRecord,
    RunnerLaneRegistrationPolicy,
    RunnerLaneRegistrationRequest,
    plan_runner_lane_registration,
)
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.runtime_key_safety_policy import (
    RuntimeKeySafetyPolicyRecord,
    RuntimeSecretSafetyRule,
)
from control_plane.contracts.work_graph_read_model import WorkGraphPlanningIssueFacts
from control_plane.http_app import (
    AcceptedEvidenceResponse,
    LaunchplaneAuthzPolicyRuntime,
    create_launchplane_fastapi_app,
    store_product_config_dry_run_record,
)
from control_plane.service_auth import (
    BearerIdentityConfig,
    GitHubHumanIdentity,
    GitHubActionsIdentity,
    LaunchplaneAuthzPolicy,
    LocalOperatorIdentity,
    agent_authz_audit,
)
from control_plane.service_human_auth import (
    GitHubOAuthConfig,
    HumanSessionManager,
    InMemoryHumanSessionStore,
    LaunchplaneHumanSession,
)
from control_plane.contracts.secret_record import SecretBinding
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.work_graph_issue_inbox import (
    GitHubIssueInboxReadModel,
    GitHubIssueInboxReconcileResult,
)
from control_plane.workflows.launchplane_self_deploy import LAUNCHPLANE_IMAGE_REFERENCE_ENV_KEY
from control_plane.workflows.public_ingress_monitor import (
    PublicIngressMonitorResult,
    public_ingress_managed_secret_resolver,
)
from control_plane.workflows.npmplus_ingress import (
    NpmplusIngressApplyResult,
    NpmplusIngressOperation,
)
from tests.test_service import create_launchplane_service_app
from tests.test_service import (
    _FakeNpmplusIngressClient,
    _FakeIngressProvider,
    _generic_site_profile_payload,
    _edge_endpoint_apply_payload,
    _edge_endpoint_record,
    _identity,
    _ingress_canary_route_record,
    _ingress_canary_route_record_apply_payload,
    _local_operator_policy,
    _merge_train_policy_table,
    _merge_train_run_record,
    _merge_train_service_identity,
    _merge_train_service_policy,
    _private_health_endpoint_record,
    _private_health_endpoint_apply_payload,
    _product_profile_payload,
    _product_profile_payload_with_prod,
    _npmplus_proxy_host,
    _npmplus_ingress_route_payload,
    _meta_product_config_payload,
    _product_config_payload,
    _product_config_secrets,
    _seed_merge_train_policy,
    _seed_tracked_target_records,
    _sqlite_database_url,
    _write_runtime_key_safety_policy,
    _work_graph_snapshot_payload,
)
from tests.test_service import _StubVerifier
from tests.merge_train_policy_fixtures import build_test_merge_train_policy_with_codex_skills
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
        self.assertEqual(response.headers["Cache-Control"], "no-store")

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

    async def test_github_oauth_routes_precede_wsgi_fallback(self) -> None:
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
        fallback_calls: list[str] = []

        def fallback_app(
            environ: dict[str, object], start_response: Callable[..., object]
        ) -> list[bytes]:
            fallback_calls.append(str(environ.get("PATH_INFO", "")))
            start_response("599 Legacy Fallback", [("Content-Type", "application/json")])
            return [b'{"status":"legacy"}']

        app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, fallback_app))))
        login_response = await _asgi_get(app, "/auth/github/login")
        state = parse_qs(urlparse(login_response.headers["Location"]).query)["state"][0]
        callback_response = await _asgi_get(
            app,
            f"/auth/github/callback?code=github-code&state={state}",
        )

        self.assertEqual(login_response.status_code, 302)
        self.assertEqual(callback_response.status_code, 302)
        self.assertEqual(fallback_calls, [])

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

    async def test_session_read_native_route_precedes_wsgi_fallback(self) -> None:
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
        fallback_calls: list[str] = []

        def fallback_app(
            environ: dict[str, object], start_response: Callable[..., object]
        ) -> list[bytes]:
            fallback_calls.append(str(environ.get("PATH_INFO", "")))
            start_response("599 Legacy Fallback", [("Content-Type", "application/json")])
            return [b'{"status":"legacy"}']

        app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, fallback_app))))

        response = await _asgi_get(
            app,
            "/v1/auth/session",
            headers={"Cookie": session_manager.session_cookie_header(human_session)},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["identity"]["login"], "example-operator")
        self.assertEqual(fallback_calls, [])

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

    async def test_logout_native_route_precedes_wsgi_fallback(self) -> None:
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
        fallback_calls: list[str] = []

        def fallback_app(
            environ: dict[str, object], start_response: Callable[..., object]
        ) -> list[bytes]:
            fallback_calls.append(str(environ.get("PATH_INFO", "")))
            start_response("599 Legacy Fallback", [("Content-Type", "application/json")])
            return [b'{"status":"legacy"}']

        app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, fallback_app))))

        response = await _asgi_request(
            app,
            "POST",
            "/auth/logout",
            headers={"Cookie": session_manager.session_cookie_header(human_session)},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertEqual(fallback_calls, [])

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


class FastApiServiceRuntimeReadTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_service_runtime_routes_require_authentication(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_local_operator_launchplane_service_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=_local_operator_bearer_config(),
        )

        runtime_response = await _asgi_get(app, "/v1/service/runtime")
        worker_response = await _asgi_get(app, "/v1/service/odoo-workers/status")
        reconcile_response = await _asgi_request(
            app,
            "POST",
            "/v1/service/odoo-workers/reconcile",
            payload={},
        )

        self.assertEqual(runtime_response.status_code, 401)
        self.assertEqual(worker_response.status_code, 401)
        self.assertEqual(reconcile_response.status_code, 401)
        self.assertEqual(runtime_response.json()["error"]["code"], "authentication_required")
        self.assertEqual(worker_response.json()["error"]["code"], "authentication_required")
        self.assertEqual(reconcile_response.json()["error"]["code"], "authentication_required")

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
        self.assertTrue(set(runtime_route["responses"]) >= {"200", "401", "403"})
        self.assertTrue(set(worker_route["responses"]) >= {"200", "400", "401", "403", "503"})
        self.assertTrue(set(reconcile_route["responses"]) >= {"200", "400", "401", "403", "503"})

    async def test_fastapi_service_runtime_precedes_legacy_wsgi_fallback(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            record_store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_local_operator_launchplane_service_read_policy(),
                record_store_factory=lambda: record_store,
                bearer_identity_config=_local_operator_bearer_config(),
            )
            legacy_app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=root,
                local_record_store_for_tests=record_store,
            )
            app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

            response = await _asgi_get(
                app,
                "/v1/service/runtime",
                headers={"Authorization": "Bearer local-operator-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")


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

    async def test_fastapi_operation_status_precedes_legacy_wsgi_fallback(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            record_store = FilesystemRecordStore(state_dir=state_dir)
            record_store.write_odoo_stable_bootstrap_operation_record(
                _running_odoo_stable_bootstrap_record()
            )
            record_store.write_odoo_stable_target_replacement_operation_record(
                _running_odoo_target_replacement_record()
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_odoo_operation_status_identity()),
                authz_policy=_odoo_operation_status_policy(
                    action="odoo_stable_bootstrap.execute",
                    actions=(
                        "odoo_stable_bootstrap.execute",
                        "odoo_target_replacement_apply.execute",
                    ),
                ),
                record_store_factory=lambda: record_store,
            )
            legacy_app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=root,
                local_record_store_for_tests=record_store,
            )
            app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

            bootstrap_response = await _asgi_get(
                app,
                "/v1/drivers/odoo/stable-bootstrap/operations/operation-cm-testing",
                headers={"Authorization": "Bearer valid-token"},
            )
            replacement_response = await _asgi_get(
                app,
                "/v1/drivers/odoo/target-replacement/operations/operation-cm-testing",
                headers={"Authorization": "Bearer valid-token"},
            )

        self.assertEqual(bootstrap_response.status_code, 200)
        self.assertEqual(replacement_response.status_code, 200)
        self.assertEqual(bootstrap_response.json()["status"], "ok")
        self.assertEqual(replacement_response.json()["status"], "ok")


class FastApiDeploymentEvidenceStoreGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_deployment_evidence_accepts_store_without_promotion_methods(self) -> None:
        store = _DeploymentEvidenceOnlyStore()
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_deployment_write_identity()),
            authz_policy=_deployment_write_policy(context="example-site"),
            record_store_factory=lambda: store,
        )

        response = await _post_deployment_evidence(app, _deployment_evidence_payload())

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(payload["records"]["deployment_record_id"], "deployment-example-site-prod")
        self.assertEqual(
            store.deployment_records["deployment-example-site-prod"]["context"],
            "example-site",
        )
        self.assertEqual(
            store.environment_inventories[0]["deployment_record_id"],
            "deployment-example-site-prod",
        )

    async def test_deployment_evidence_replays_idempotency_before_deployment_gate(self) -> None:
        store = _IdempotencyOnlyReplayStore()
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_deployment_write_identity()),
            authz_policy=_deployment_write_policy(context="example-site"),
            record_store_factory=lambda: store,
        )
        request_payload = _deployment_evidence_payload()

        first_response = await _post_deployment_evidence(
            app,
            request_payload,
            idempotency_key="deployment-example-site-prod",
        )
        store.write_deployment_record = None
        store.write_environment_inventory = None
        second_response = await _post_deployment_evidence(
            app,
            request_payload,
            idempotency_key="deployment-example-site-prod",
        )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        first_payload = first_response.json()
        second_payload = second_response.json()
        self.assertEqual(second_payload["records"], first_payload["records"])
        self.assertTrue(second_payload["replayed"])
        self.assertEqual(second_payload["original_trace_id"], first_payload["trace_id"])
        self.assertEqual(store.read_idempotency_calls, 2)
        self.assertEqual(store.write_deployment_calls, 1)
        self.assertEqual(store.write_environment_inventory_calls, 1)


class FastApiNotificationPolicyApplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_ingress_notification_policy_apply_writes_db_policy(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_notification_policy_apply_policy(
                        action="public_ingress_notification_policy.apply",
                        product="launchplane",
                        context="launchplane",
                    ),
                    record_store_factory=lambda: store,
                )
                policy_record = _public_ingress_notification_policy_record()

                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/public-ingress/notification-policies/apply",
                    headers={
                        "Authorization": "Bearer valid-token",
                        "Idempotency-Key": "public-ingress-notification-policy-test",
                    },
                    payload={
                        "schema_version": 1,
                        "mode": "apply",
                        "policy": policy_record.model_dump(mode="json"),
                    },
                )
                records = store.list_public_ingress_notification_policy_records(
                    product="launchplane", context_name="launchplane", status="enabled"
                )
            finally:
                store.close()

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(
            payload["records"],
            {"public_ingress_notification_policy_id": policy_record.policy_id},
        )
        self.assertEqual(payload["result"]["mode"], "apply")
        self.assertTrue(payload["result"]["changed"])
        self.assertEqual(records, (policy_record,))

    async def test_public_ingress_notification_policy_dry_run_does_not_write(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_notification_policy_apply_policy(
                        action="public_ingress_notification_policy.apply",
                        product="launchplane",
                        context="launchplane",
                    ),
                    record_store_factory=lambda: store,
                )

                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/public-ingress/notification-policies/apply",
                    headers={"Authorization": "Bearer valid-token"},
                    payload={
                        "schema_version": 1,
                        "mode": "dry-run",
                        "policy": _public_ingress_notification_policy_record(
                            policy_id="public-ingress-notification-launchplane-dry-run"
                        ).model_dump(mode="json"),
                    },
                )
                records = store.list_public_ingress_notification_policy_records()
            finally:
                store.close()

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["result"]["mode"], "dry-run")
        self.assertFalse(payload["result"]["changed"])
        self.assertEqual(records, ())

    async def test_public_ingress_notification_policy_replays_idempotent_response(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_notification_policy_apply_policy(
                        action="public_ingress_notification_policy.apply",
                        product="launchplane",
                        context="launchplane",
                    ),
                    record_store_factory=lambda: store,
                )
                payload = {
                    "schema_version": 1,
                    "mode": "apply",
                    "policy": _public_ingress_notification_policy_record().model_dump(mode="json"),
                }

                first_response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/public-ingress/notification-policies/apply",
                    headers={
                        "Authorization": "Bearer valid-token",
                        "Idempotency-Key": "public-ingress-notification-policy-replay",
                    },
                    payload=payload,
                )
                second_response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/public-ingress/notification-policies/apply",
                    headers={
                        "Authorization": "Bearer valid-token",
                        "Idempotency-Key": "public-ingress-notification-policy-replay",
                    },
                    payload=payload,
                )
            finally:
                store.close()

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        first_payload = first_response.json()
        second_payload = second_response.json()
        self.assertEqual(second_payload["records"], first_payload["records"])
        self.assertEqual(second_payload["result"], first_payload["result"])
        self.assertTrue(second_payload["replayed"])
        self.assertEqual(second_payload["original_trace_id"], first_payload["trace_id"])

    async def test_public_ingress_notification_policy_rejects_conflicting_idempotency_key(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_notification_policy_apply_policy(
                        action="public_ingress_notification_policy.apply",
                        product="launchplane",
                        context="launchplane",
                    ),
                    record_store_factory=lambda: store,
                )
                first_payload = {
                    "schema_version": 1,
                    "mode": "apply",
                    "policy": _public_ingress_notification_policy_record().model_dump(mode="json"),
                }
                conflicting_payload = {
                    "schema_version": 1,
                    "mode": "apply",
                    "policy": _public_ingress_notification_policy_record(
                        policy_id="public-ingress-notification-launchplane-conflict"
                    ).model_dump(mode="json"),
                }

                await _asgi_request(
                    app,
                    "POST",
                    "/v1/public-ingress/notification-policies/apply",
                    headers={
                        "Authorization": "Bearer valid-token",
                        "Idempotency-Key": "public-ingress-notification-policy-conflict",
                    },
                    payload=first_payload,
                )
                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/public-ingress/notification-policies/apply",
                    headers={
                        "Authorization": "Bearer valid-token",
                        "Idempotency-Key": "public-ingress-notification-policy-conflict",
                    },
                    payload=conflicting_payload,
                )
            finally:
                store.close()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "idempotency_key_reused")

    async def test_every_code_notification_policy_apply_writes_db_policy(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_notification_policy_apply_policy(
                        action="every_code_notification_policy.apply",
                        product="launchplane",
                        context="launchplane",
                    ),
                    record_store_factory=lambda: store,
                )
                policy_record = _every_code_notification_policy_record()

                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/every-code/notification-policies/apply",
                    headers={
                        "Authorization": "Bearer valid-token",
                        "Idempotency-Key": "every-code-notification-policy-test",
                    },
                    payload={
                        "schema_version": 1,
                        "mode": "apply",
                        "policy": policy_record.model_dump(mode="json"),
                    },
                )
                records = store.list_every_code_notification_policy_records(
                    repository="cbusillo/code", status="enabled"
                )
            finally:
                store.close()

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(
            payload["records"],
            {"every_code_notification_policy_id": policy_record.policy_id},
        )
        self.assertEqual(payload["result"]["mode"], "apply")
        self.assertEqual(records, (policy_record,))

    async def test_every_code_notification_policy_dry_run_does_not_write(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_notification_policy_apply_policy(
                        action="every_code_notification_policy.apply",
                        product="launchplane",
                        context="launchplane",
                    ),
                    record_store_factory=lambda: store,
                )

                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/every-code/notification-policies/apply",
                    headers={"Authorization": "Bearer valid-token"},
                    payload={
                        "schema_version": 1,
                        "mode": "dry-run",
                        "policy": _every_code_notification_policy_record(
                            policy_id="every-code-notification-launchplane-dry-run"
                        ).model_dump(mode="json"),
                    },
                )
                records = store.list_every_code_notification_policy_records()
            finally:
                store.close()

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["result"]["mode"], "dry-run")
        self.assertFalse(payload["result"]["changed"])
        self.assertEqual(records, ())

    async def test_every_code_notification_policy_replays_idempotent_response(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_notification_policy_apply_policy(
                        action="every_code_notification_policy.apply",
                        product="launchplane",
                        context="launchplane",
                    ),
                    record_store_factory=lambda: store,
                )
                payload = {
                    "schema_version": 1,
                    "mode": "apply",
                    "policy": _every_code_notification_policy_record().model_dump(mode="json"),
                }

                first_response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/every-code/notification-policies/apply",
                    headers={
                        "Authorization": "Bearer valid-token",
                        "Idempotency-Key": "every-code-notification-policy-replay",
                    },
                    payload=payload,
                )
                second_response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/every-code/notification-policies/apply",
                    headers={
                        "Authorization": "Bearer valid-token",
                        "Idempotency-Key": "every-code-notification-policy-replay",
                    },
                    payload=payload,
                )
            finally:
                store.close()

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        first_payload = first_response.json()
        second_payload = second_response.json()
        self.assertEqual(second_payload["records"], first_payload["records"])
        self.assertEqual(second_payload["result"], first_payload["result"])
        self.assertTrue(second_payload["replayed"])
        self.assertEqual(second_payload["original_trace_id"], first_payload["trace_id"])

    async def test_every_code_notification_policy_rejects_conflicting_idempotency_key(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_notification_policy_apply_policy(
                        action="every_code_notification_policy.apply",
                        product="launchplane",
                        context="launchplane",
                    ),
                    record_store_factory=lambda: store,
                )
                first_payload = {
                    "schema_version": 1,
                    "mode": "apply",
                    "policy": _every_code_notification_policy_record().model_dump(mode="json"),
                }
                conflicting_payload = {
                    "schema_version": 1,
                    "mode": "apply",
                    "policy": _every_code_notification_policy_record(
                        policy_id="every-code-notification-launchplane-conflict"
                    ).model_dump(mode="json"),
                }

                await _asgi_request(
                    app,
                    "POST",
                    "/v1/every-code/notification-policies/apply",
                    headers={
                        "Authorization": "Bearer valid-token",
                        "Idempotency-Key": "every-code-notification-policy-conflict",
                    },
                    payload=first_payload,
                )
                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/every-code/notification-policies/apply",
                    headers={
                        "Authorization": "Bearer valid-token",
                        "Idempotency-Key": "every-code-notification-policy-conflict",
                    },
                    payload=conflicting_payload,
                )
            finally:
                store.close()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "idempotency_key_reused")

    async def test_public_ingress_notification_policy_rejects_mismatched_scope(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_notification_policy_apply_policy(
                        action="public_ingress_notification_policy.apply",
                        product="other-product",
                        context="other-context",
                    ),
                    record_store_factory=lambda: store,
                )

                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/public-ingress/notification-policies/apply",
                    headers={"Authorization": "Bearer valid-token"},
                    payload={
                        "schema_version": 1,
                        "mode": "apply",
                        "policy": _public_ingress_notification_policy_record().model_dump(
                            mode="json"
                        ),
                    },
                )
                records = store.list_public_ingress_notification_policy_records()
            finally:
                store.close()

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")
        self.assertEqual(records, ())

    async def test_every_code_notification_policy_rejects_mismatched_scope(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_notification_policy_apply_policy(
                        action="every_code_notification_policy.apply",
                        product="other-product",
                        context="other-context",
                    ),
                    record_store_factory=lambda: store,
                )

                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/every-code/notification-policies/apply",
                    headers={"Authorization": "Bearer valid-token"},
                    payload={
                        "schema_version": 1,
                        "mode": "apply",
                        "policy": _every_code_notification_policy_record().model_dump(mode="json"),
                    },
                )
                records = store.list_every_code_notification_policy_records()
            finally:
                store.close()

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")
        self.assertEqual(records, ())

    async def test_preview_pr_feedback_notification_policy_apply_writes_db_policy(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_notification_policy_apply_policy(
                        action="preview_pr_feedback_notification_policy.apply",
                        product="sellyouroutboard",
                        context="sellyouroutboard",
                    ),
                    record_store_factory=lambda: store,
                )
                policy_record = _preview_pr_feedback_notification_policy_record()

                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/previews/pr-feedback/notification-policies/apply",
                    headers={
                        "Authorization": "Bearer valid-token",
                        "Idempotency-Key": "preview-pr-feedback-notification-policy-test",
                    },
                    payload={
                        "schema_version": 1,
                        "mode": "apply",
                        "policy": policy_record.model_dump(mode="json"),
                    },
                )
                records = store.list_preview_pr_feedback_notification_policy_records(
                    product="sellyouroutboard",
                    context_name="sellyouroutboard",
                    repository="cbusillo/sellyouroutboard",
                    status="enabled",
                )
            finally:
                store.close()

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(
            payload["records"],
            {"preview_pr_feedback_notification_policy_id": policy_record.policy_id},
        )
        self.assertEqual(payload["result"]["mode"], "apply")
        self.assertEqual(records, (policy_record,))

    async def test_preview_pr_feedback_notification_policy_replays_idempotent_response(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_notification_policy_apply_policy(
                        action="preview_pr_feedback_notification_policy.apply",
                        product="sellyouroutboard",
                        context="sellyouroutboard",
                    ),
                    record_store_factory=lambda: store,
                )
                payload = {
                    "schema_version": 1,
                    "mode": "apply",
                    "policy": _preview_pr_feedback_notification_policy_record().model_dump(
                        mode="json"
                    ),
                }

                first_response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/previews/pr-feedback/notification-policies/apply",
                    headers={
                        "Authorization": "Bearer valid-token",
                        "Idempotency-Key": "preview-pr-feedback-notification-policy-replay",
                    },
                    payload=payload,
                )
                second_response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/previews/pr-feedback/notification-policies/apply",
                    headers={
                        "Authorization": "Bearer valid-token",
                        "Idempotency-Key": "preview-pr-feedback-notification-policy-replay",
                    },
                    payload=payload,
                )
            finally:
                store.close()

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        first_payload = first_response.json()
        second_payload = second_response.json()
        self.assertEqual(second_payload["records"], first_payload["records"])
        self.assertEqual(second_payload["result"], first_payload["result"])
        self.assertTrue(second_payload["replayed"])
        self.assertEqual(second_payload["original_trace_id"], first_payload["trace_id"])

    async def test_preview_pr_feedback_notification_policy_rejects_conflicting_idempotency_key(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_notification_policy_apply_policy(
                        action="preview_pr_feedback_notification_policy.apply",
                        product="sellyouroutboard",
                        context="sellyouroutboard",
                    ),
                    record_store_factory=lambda: store,
                )
                first_payload = {
                    "schema_version": 1,
                    "mode": "apply",
                    "policy": _preview_pr_feedback_notification_policy_record().model_dump(
                        mode="json"
                    ),
                }
                conflicting_payload = {
                    "schema_version": 1,
                    "mode": "apply",
                    "policy": _preview_pr_feedback_notification_policy_record(
                        policy_id="preview-pr-feedback-notification-syo-conflict"
                    ).model_dump(mode="json"),
                }

                await _asgi_request(
                    app,
                    "POST",
                    "/v1/previews/pr-feedback/notification-policies/apply",
                    headers={
                        "Authorization": "Bearer valid-token",
                        "Idempotency-Key": "preview-pr-feedback-notification-policy-conflict",
                    },
                    payload=first_payload,
                )
                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/previews/pr-feedback/notification-policies/apply",
                    headers={
                        "Authorization": "Bearer valid-token",
                        "Idempotency-Key": "preview-pr-feedback-notification-policy-conflict",
                    },
                    payload=conflicting_payload,
                )
            finally:
                store.close()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "idempotency_key_reused")

    async def test_preview_pr_feedback_notification_policy_dry_run_does_not_write(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_notification_policy_apply_policy(
                        action="preview_pr_feedback_notification_policy.apply",
                        product="sellyouroutboard",
                        context="sellyouroutboard",
                    ),
                    record_store_factory=lambda: store,
                )

                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/previews/pr-feedback/notification-policies/apply",
                    headers={"Authorization": "Bearer valid-token"},
                    payload={
                        "schema_version": 1,
                        "mode": "dry-run",
                        "policy": _preview_pr_feedback_notification_policy_record(
                            policy_id="preview-pr-feedback-notification-syo-dry-run"
                        ).model_dump(mode="json"),
                    },
                )
                records = store.list_preview_pr_feedback_notification_policy_records(
                    product="sellyouroutboard",
                    context_name="sellyouroutboard",
                    repository="cbusillo/sellyouroutboard",
                )
            finally:
                store.close()

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["result"]["mode"], "dry-run")
        self.assertFalse(payload["result"]["changed"])
        self.assertEqual(records, ())

    async def test_preview_pr_feedback_notification_policy_rejects_wildcard_scope(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_notification_policy_apply_policy(
                        action="preview_pr_feedback_notification_policy.apply",
                        product="launchplane",
                        context="launchplane",
                    ),
                    record_store_factory=lambda: store,
                )

                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/previews/pr-feedback/notification-policies/apply",
                    headers={
                        "Authorization": "Bearer valid-token",
                        "Idempotency-Key": "preview-pr-feedback-notification-global",
                    },
                    payload={
                        "schema_version": 1,
                        "mode": "apply",
                        "policy": _preview_pr_feedback_notification_policy_record(
                            policy_id="preview-pr-feedback-notification-global",
                            product="",
                            context="",
                            repository="",
                        ).model_dump(mode="json"),
                    },
                )
                records = store.list_preview_pr_feedback_notification_policy_records()
            finally:
                store.close()

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_policy_scope")
        self.assertEqual(records, ())

    async def test_preview_pr_feedback_notification_policy_rejects_mismatched_scope(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_notification_policy_apply_policy(
                        action="preview_pr_feedback_notification_policy.apply",
                        product="launchplane",
                        context="launchplane",
                    ),
                    record_store_factory=lambda: store,
                )

                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/previews/pr-feedback/notification-policies/apply",
                    headers={
                        "Authorization": "Bearer valid-token",
                        "Idempotency-Key": "preview-pr-feedback-notification-denied",
                    },
                    payload={
                        "schema_version": 1,
                        "mode": "apply",
                        "policy": _preview_pr_feedback_notification_policy_record().model_dump(
                            mode="json"
                        ),
                    },
                )
            finally:
                store.close()

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_notification_policy_apply_local_operator_requires_reason(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_local_operator_policy(
                        actions=(
                            "public_ingress_notification_policy.apply",
                            "every_code_notification_policy.apply",
                            "preview_pr_feedback_notification_policy.apply",
                        ),
                        products=("launchplane", "sellyouroutboard"),
                        contexts=("launchplane", "sellyouroutboard"),
                        token_label="local-owner-read",
                    ),
                    record_store_factory=lambda: store,
                    bearer_identity_config=_local_operator_bearer_config(),
                )
                cases = (
                    (
                        "/v1/public-ingress/notification-policies/apply",
                        _public_ingress_notification_policy_record(),
                    ),
                    (
                        "/v1/every-code/notification-policies/apply",
                        _every_code_notification_policy_record(),
                    ),
                    (
                        "/v1/previews/pr-feedback/notification-policies/apply",
                        _preview_pr_feedback_notification_policy_record(),
                    ),
                )

                responses = []
                for path, policy_record in cases:
                    responses.append(
                        await _asgi_request(
                            app,
                            "POST",
                            path,
                            headers={"Authorization": "Bearer local-operator-token"},
                            payload={
                                "schema_version": 1,
                                "mode": "apply",
                                "policy": policy_record.model_dump(mode="json"),
                            },
                        )
                    )
            finally:
                store.close()

        for response in responses:
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"]["code"], "reason_required")

    async def test_notification_policy_apply_accepts_local_operator_reason(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_local_operator_policy(
                        actions=("public_ingress_notification_policy.apply",),
                        products=("launchplane",),
                        contexts=("launchplane",),
                        token_label="local-owner-read",
                    ),
                    record_store_factory=lambda: store,
                    bearer_identity_config=_local_operator_bearer_config(),
                )
                policy_record = _public_ingress_notification_policy_record(
                    policy_id="public-ingress-notification-local-operator"
                )

                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/public-ingress/notification-policies/apply",
                    headers={
                        "Authorization": "Bearer local-operator-token",
                        "Idempotency-Key": "public-ingress-local-operator-apply",
                    },
                    payload={
                        "schema_version": 1,
                        "mode": "apply",
                        "reason": "Enable local operator ingress notifications.",
                        "policy": policy_record.model_dump(mode="json"),
                    },
                )
                records = store.list_public_ingress_notification_policy_records(
                    product="launchplane", context_name="launchplane", status="enabled"
                )
            finally:
                store.close()

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["result"]["mode"], "apply")
        self.assertEqual(records, (policy_record,))

    async def test_notification_policy_apply_requires_database_storage(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_notification_policy_apply_policy(
                    action="public_ingress_notification_policy.apply",
                    product="launchplane",
                    context="launchplane",
                ),
                record_store_factory=lambda: store,
            )

            response = await _asgi_request(
                app,
                "POST",
                "/v1/public-ingress/notification-policies/apply",
                headers={"Authorization": "Bearer valid-token"},
                payload={
                    "schema_version": 1,
                    "mode": "dry-run",
                    "policy": _public_ingress_notification_policy_record().model_dump(mode="json"),
                },
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "database_required")

    async def test_notification_policy_apply_routes_precede_legacy_wsgi_fallback(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                policy = _notification_policy_apply_policy(
                    action="public_ingress_notification_policy.apply",
                    product="launchplane",
                    context="launchplane",
                )
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=policy,
                    record_store_factory=lambda: store,
                )
                legacy_app = create_launchplane_service_app(
                    state_dir=root / "state",
                    verifier=_RejectingVerifier(),
                    authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                    local_record_store_for_tests=store,
                )
                app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/public-ingress/notification-policies/apply",
                    headers={"Authorization": "Bearer valid-token"},
                    payload={
                        "schema_version": 1,
                        "mode": "dry-run",
                        "policy": _public_ingress_notification_policy_record().model_dump(
                            mode="json"
                        ),
                    },
                )
            finally:
                store.close()

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["result"]["mode"], "dry-run")

    async def test_openapi_includes_notification_policy_apply_routes(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        paths = response.json()["paths"]
        self.assertIn("/v1/public-ingress/notification-policies/apply", paths)
        self.assertIn("/v1/every-code/notification-policies/apply", paths)
        self.assertIn("/v1/previews/pr-feedback/notification-policies/apply", paths)


class FastApiRuntimeKeySafetyPolicyApplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_key_safety_policy_apply_reconciles_rules_and_replays(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                _write_runtime_key_safety_policy(database_url=database_url)
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_runtime_key_safety_policy_apply_policy(
                        action="runtime_key_safety.write"
                    ),
                    record_store_factory=lambda: store,
                )
                payload = _runtime_key_safety_policy_apply_payload()

                first_response = await _post_runtime_key_safety_policy_apply(
                    app,
                    payload,
                    idempotency_key="runtime-key-safety-policy:test",
                )
                active_policy = store.list_runtime_key_safety_policy_records(
                    status="active", limit=1
                )[0]
                replay_response = await _post_runtime_key_safety_policy_apply(
                    app,
                    payload,
                    idempotency_key="runtime-key-safety-policy:test",
                )
            finally:
                store.close()

        self.assertEqual(first_response.status_code, 202)
        first_payload = first_response.json()
        self.assertEqual(first_payload["status"], "accepted")
        self.assertEqual(
            set(first_payload["records"]),
            {"runtime_key_safety_policy_record_id"},
        )
        self.assertEqual(
            first_payload["records"]["runtime_key_safety_policy_record_id"],
            active_policy.record_id,
        )
        self.assertEqual(first_payload["result"]["changed"], True)
        self.assertEqual(
            first_payload["result"]["runtime_key_safety_policy"]["binding_keys"],
            ["RESEND_API_KEY", "SMTP_PASSWORD"],
        )
        self.assertEqual(
            [rule.binding_key for rule in active_policy.rules],
            ["RESEND_API_KEY", "SMTP_PASSWORD"],
        )
        self.assertEqual(replay_response.status_code, 202)
        replay_payload = replay_response.json()
        self.assertEqual(replay_payload["records"], first_payload["records"])
        self.assertEqual(replay_payload["result"], first_payload["result"])
        self.assertTrue(replay_payload["replayed"])
        self.assertEqual(replay_payload["original_trace_id"], first_payload["trace_id"])

    async def test_runtime_key_safety_policy_apply_rejects_conflicting_idempotency_key(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_runtime_key_safety_policy_apply_policy(
                        action="runtime_key_safety.write"
                    ),
                    record_store_factory=lambda: store,
                )
                first_payload = _runtime_key_safety_policy_apply_payload()
                conflicting_payload = _runtime_key_safety_policy_apply_payload()
                conflicting_rules = cast(list[dict[str, object]], conflicting_payload["rules"])
                conflicting_rules[1] = {
                    "binding_key": "MAILGUN_API_KEY",
                    "secret_class": "prod_only",
                    "allowed_contexts": ["sellyouroutboard"],
                    "allowed_instances": ["prod"],
                }

                first_response = await _post_runtime_key_safety_policy_apply(
                    app,
                    first_payload,
                    idempotency_key="runtime-key-safety-policy:conflict",
                )
                second_response = await _post_runtime_key_safety_policy_apply(
                    app,
                    conflicting_payload,
                    idempotency_key="runtime-key-safety-policy:conflict",
                )
                active_policy = store.list_runtime_key_safety_policy_records(
                    status="active", limit=1
                )[0]
            finally:
                store.close()

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 409)
        payload = second_response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "idempotency_key_reused")
        self.assertEqual(
            [rule.binding_key for rule in active_policy.rules],
            ["RESEND_API_KEY", "SMTP_PASSWORD"],
        )

    async def test_runtime_key_safety_policy_apply_rejects_without_permission(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_runtime_key_safety_policy_apply_policy(
                        action="product_profile.read"
                    ),
                    record_store_factory=lambda: store,
                )

                response = await _post_runtime_key_safety_policy_apply(
                    app,
                    _runtime_key_safety_policy_apply_payload(),
                    idempotency_key="runtime-key-safety-policy:unauthorized",
                )
                active_records = store.list_runtime_key_safety_policy_records(status="active")
            finally:
                store.close()

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")
        self.assertEqual(active_records, ())

    async def test_runtime_key_safety_policy_apply_rejects_human_session_mutation(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            oauth_config = _github_oauth_config()
            session_manager = HumanSessionManager(
                config=oauth_config,
                session_store=InMemoryHumanSessionStore(),
            )
            human_session = session_manager.issue(_github_human_identity())
            try:
                app = create_launchplane_fastapi_app(
                    verifier=_RejectingVerifier(),
                    authz_policy=_github_human_runtime_key_safety_policy_apply_policy(),
                    record_store_factory=lambda: store,
                    human_session_manager=session_manager,
                )

                response = await _post_runtime_key_safety_policy_apply(
                    app,
                    _runtime_key_safety_policy_apply_payload(),
                    authorization="",
                    headers={"Cookie": session_manager.session_cookie_header(human_session)},
                )
                active_records = store.list_runtime_key_safety_policy_records(status="active")
            finally:
                store.close()

        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authentication_required")
        self.assertEqual(active_records, ())

    async def test_runtime_key_safety_policy_apply_requires_database_storage(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_runtime_key_safety_policy_apply_policy(
                    action="runtime_key_safety.write"
                ),
                record_store_factory=lambda: store,
            )

            response = await _post_runtime_key_safety_policy_apply(
                app,
                _runtime_key_safety_policy_apply_payload(),
            )

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "database_required")

    async def test_runtime_key_safety_policy_apply_native_route_precedes_wsgi_fallback(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                policy = _runtime_key_safety_policy_apply_policy(action="runtime_key_safety.write")
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=policy,
                    record_store_factory=lambda: store,
                )
                legacy_app = create_launchplane_service_app(
                    state_dir=root / "state",
                    verifier=_RejectingVerifier(),
                    authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                    local_record_store_for_tests=store,
                )
                app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

                response = await _post_runtime_key_safety_policy_apply(
                    app,
                    _runtime_key_safety_policy_apply_payload(),
                )
            finally:
                store.close()

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "accepted")

    async def test_openapi_includes_runtime_key_safety_policy_apply_route(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        route = openapi["paths"]["/v1/runtime-key-safety/policies/apply"]["post"]
        self.assertEqual(route["operationId"], "apply_runtime_key_safety_policy")
        self.assertEqual(
            route["requestBody"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/RuntimeKeySafetyPolicyApplyEnvelope",
        )
        self.assertEqual(
            route["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/AcceptedEvidenceResponse",
        )
        for status_code in ("400", "401", "403", "409", "503"):
            self.assertIn("LaunchplaneErrorResponse", json.dumps(route["responses"][status_code]))


class FastApiBackupGateEvidenceStoreGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_backup_gate_evidence_accepts_store_with_only_backup_gate_method(self) -> None:
        store = _BackupGateEvidenceOnlyStore()
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_backup_gate_write_identity()),
            authz_policy=_backup_gate_write_policy(context="example-site"),
            record_store_factory=lambda: store,
        )

        response = await _post_backup_gate_evidence(app, _backup_gate_evidence_payload())

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(
            payload["records"]["backup_gate_record_id"], "backup-gate-example-site-prod"
        )
        self.assertEqual(
            store.backup_gate_records["backup-gate-example-site-prod"]["context"],
            "example-site",
        )

    async def test_backup_gate_evidence_requires_backup_gate_store(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_backup_gate_write_identity()),
            authz_policy=_backup_gate_write_policy(context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_backup_gate_evidence(app, _backup_gate_evidence_payload())

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "database_storage_required")

    async def test_backup_gate_evidence_replays_idempotency_before_backup_gate_gate(self) -> None:
        store = _IdempotencyOnlyBackupGateReplayStore()
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_backup_gate_write_identity()),
            authz_policy=_backup_gate_write_policy(context="example-site"),
            record_store_factory=lambda: store,
        )
        request_payload = _backup_gate_evidence_payload()

        first_response = await _post_backup_gate_evidence(
            app,
            request_payload,
            idempotency_key="backup-gate-example-site-prod",
        )
        store.write_backup_gate_record = None
        second_response = await _post_backup_gate_evidence(
            app,
            request_payload,
            idempotency_key="backup-gate-example-site-prod",
        )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        first_payload = first_response.json()
        second_payload = second_response.json()
        self.assertEqual(second_payload["records"], first_payload["records"])
        self.assertTrue(second_payload["replayed"])
        self.assertEqual(second_payload["original_trace_id"], first_payload["trace_id"])
        self.assertEqual(store.read_idempotency_calls, 2)
        self.assertEqual(store.write_backup_gate_calls, 1)


class FastApiPromotionEvidenceStoreGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_promotion_evidence_accepts_record_only_store_without_deployment_methods(
        self,
    ) -> None:
        store = _PromotionEvidenceOnlyStore()
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_promotion_write_identity()),
            authz_policy=_promotion_write_policy(context="example-site"),
            record_store_factory=lambda: store,
        )

        response = await _post_promotion_evidence(
            app,
            _promotion_evidence_payload(link_deployment=False),
        )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(
            payload["records"],
            {"promotion_record_id": "promotion-example-site-testing-to-prod"},
        )
        self.assertEqual(
            store.promotion_records["promotion-example-site-testing-to-prod"]["context"],
            "example-site",
        )

    async def test_promotion_evidence_requires_linked_deployment_store_methods(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_promotion_write_identity()),
            authz_policy=_promotion_write_policy(context="example-site"),
            record_store_factory=lambda: _PromotionEvidenceOnlyStore(),
        )

        response = await _post_promotion_evidence(app, _promotion_evidence_payload())

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "database_storage_required")

    async def test_promotion_evidence_requires_promotion_store(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_promotion_write_identity()),
            authz_policy=_promotion_write_policy(context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_promotion_evidence(
            app,
            _promotion_evidence_payload(link_deployment=False),
        )

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "database_storage_required")

    async def test_promotion_evidence_replays_idempotency_before_promotion_gate(self) -> None:
        store = _IdempotencyOnlyPromotionReplayStore()
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_promotion_write_identity()),
            authz_policy=_promotion_write_policy(context="example-site"),
            record_store_factory=lambda: store,
        )
        request_payload = _promotion_evidence_payload(link_deployment=False)

        first_response = await _post_promotion_evidence(
            app,
            request_payload,
            idempotency_key="promotion-example-site-testing-to-prod",
        )
        # The second request must replay before capability checks or write calls.
        store.write_promotion_record = None
        second_response = await _post_promotion_evidence(
            app,
            request_payload,
            idempotency_key="promotion-example-site-testing-to-prod",
        )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        first_payload = first_response.json()
        second_payload = second_response.json()
        self.assertEqual(second_payload["records"], first_payload["records"])
        self.assertTrue(second_payload["replayed"])
        self.assertEqual(second_payload["original_trace_id"], first_payload["trace_id"])
        self.assertEqual(store.read_idempotency_calls, 2)
        self.assertEqual(store.write_promotion_calls, 1)


class FastApiPreviewGenerationEvidenceStoreGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_generation_evidence_requires_preview_generation_store(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_preview_generation_write_identity()),
            authz_policy=_preview_generation_write_policy(context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_preview_generation_evidence(
            app,
            _preview_generation_evidence_payload(),
        )

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "database_storage_required")

    async def test_preview_generation_evidence_replays_idempotency_before_store_gate(
        self,
    ) -> None:
        store = _IdempotencyOnlyPreviewGenerationReplayStore()
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_preview_generation_write_identity()),
            authz_policy=_preview_generation_write_policy(context="example-site"),
            record_store_factory=lambda: store,
        )
        request_payload = _preview_generation_evidence_payload()

        first_response = await _post_preview_generation_evidence(
            app,
            request_payload,
            idempotency_key="preview-generation-example-site-pr-42",
        )
        store.write_preview_generation_evidence_records = None
        second_response = await _post_preview_generation_evidence(
            app,
            request_payload,
            idempotency_key="preview-generation-example-site-pr-42",
        )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        first_payload = first_response.json()
        second_payload = second_response.json()
        self.assertEqual(second_payload["records"], first_payload["records"])
        self.assertTrue(second_payload["replayed"])
        self.assertEqual(second_payload["original_trace_id"], first_payload["trace_id"])
        self.assertEqual(store.read_idempotency_calls, 2)
        self.assertEqual(store.write_preview_generation_evidence_calls, 1)


class FastApiPreviewDestroyedEvidenceStoreGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_destroyed_evidence_requires_preview_destroyed_store(
        self,
    ) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_preview_destroyed_write_identity()),
            authz_policy=_preview_destroyed_write_policy(context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_preview_destroyed_evidence(
            app,
            _preview_destroyed_evidence_payload(),
        )

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "database_storage_required")

    async def test_preview_destroyed_evidence_replays_idempotency_before_store_gate(
        self,
    ) -> None:
        store = _IdempotencyOnlyPreviewDestroyedReplayStore()
        store.seed_preview(_preview_record_for_destroy())
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_preview_destroyed_write_identity()),
            authz_policy=_preview_destroyed_write_policy(context="example-site"),
            record_store_factory=lambda: store,
        )
        request_payload = _preview_destroyed_evidence_payload()

        first_response = await _post_preview_destroyed_evidence(
            app,
            request_payload,
            idempotency_key="preview-destroyed-example-site-pr-42",
        )
        store.write_preview_record = None
        second_response = await _post_preview_destroyed_evidence(
            app,
            request_payload,
            idempotency_key="preview-destroyed-example-site-pr-42",
        )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        first_payload = first_response.json()
        second_payload = second_response.json()
        self.assertEqual(second_payload["records"], first_payload["records"])
        self.assertTrue(second_payload["replayed"])
        self.assertEqual(second_payload["original_trace_id"], first_payload["trace_id"])
        self.assertEqual(store.read_idempotency_calls, 2)
        self.assertEqual(store.write_preview_record_calls, 1)


class FastApiRunnerHostHygieneAuditEvidenceStoreGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_runner_host_hygiene_audit_evidence_requires_audit_store(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_runner_host_hygiene_audit_write_identity()),
            authz_policy=_runner_host_hygiene_audit_write_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_runner_host_hygiene_audit_evidence(
            app,
            _runner_host_hygiene_audit_payload(),
        )

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "database_storage_required")

    async def test_runner_host_hygiene_audit_replays_idempotency_before_store_gate(
        self,
    ) -> None:
        store = _IdempotencyOnlyRunnerHostHygieneAuditReplayStore()
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_runner_host_hygiene_audit_write_identity()),
            authz_policy=_runner_host_hygiene_audit_write_policy(),
            record_store_factory=lambda: store,
        )
        request_payload = _runner_host_hygiene_audit_payload()

        first_response = await _post_runner_host_hygiene_audit_evidence(
            app,
            request_payload,
            idempotency_key="runner-host-hygiene:chris-testing:planned",
        )
        store.write_runner_host_hygiene_audit_record = None
        second_response = await _post_runner_host_hygiene_audit_evidence(
            app,
            request_payload,
            idempotency_key="runner-host-hygiene:chris-testing:planned",
        )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        first_payload = first_response.json()
        second_payload = second_response.json()
        self.assertEqual(second_payload["records"], first_payload["records"])
        self.assertEqual(second_payload["result"], first_payload["result"])
        self.assertTrue(second_payload["replayed"])
        self.assertEqual(second_payload["original_trace_id"], first_payload["trace_id"])
        self.assertEqual(store.read_idempotency_calls, 2)
        self.assertEqual(store.write_runner_host_hygiene_audit_calls, 1)


class FastApiRunnerLaneRegistrationAuditEvidenceStoreGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_runner_lane_registration_audit_evidence_requires_audit_store(
        self,
    ) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_runner_lane_registration_audit_write_identity()),
            authz_policy=_runner_lane_registration_audit_write_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_runner_lane_registration_audit_evidence(
            app,
            _runner_lane_registration_audit_payload(),
        )

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "database_storage_required")

    async def test_runner_lane_registration_audit_replays_idempotency_before_store_gate(
        self,
    ) -> None:
        store = _IdempotencyOnlyRunnerLaneRegistrationAuditReplayStore()
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_runner_lane_registration_audit_write_identity()),
            authz_policy=_runner_lane_registration_audit_write_policy(),
            record_store_factory=lambda: store,
        )
        request_payload = _runner_lane_registration_audit_payload()

        first_response = await _post_runner_lane_registration_audit_evidence(
            app,
            request_payload,
            idempotency_key="runner-lane-registration:cm-website:planned",
        )
        store.write_runner_lane_registration_audit_record = None
        second_response = await _post_runner_lane_registration_audit_evidence(
            app,
            request_payload,
            idempotency_key="runner-lane-registration:cm-website:planned",
        )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        first_payload = first_response.json()
        second_payload = second_response.json()
        self.assertEqual(second_payload["records"], first_payload["records"])
        self.assertEqual(second_payload["result"], first_payload["result"])
        self.assertTrue(second_payload["replayed"])
        self.assertEqual(second_payload["original_trace_id"], first_payload["trace_id"])
        self.assertEqual(store.read_idempotency_calls, 2)
        self.assertEqual(store.write_runner_lane_registration_audit_calls, 1)


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

    async def test_config_status_accepts_terminal_agent_bearer_identity(self) -> None:
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
                verifier=_RejectingVerifier(),
                authz_policy=_terminal_agent_product_environment_read_policy(
                    context="example-site"
                ),
                record_store_factory=lambda: app_store,
                bearer_identity_config=BearerIdentityConfig(
                    terminal_agent_token="terminal-read-token",
                    terminal_agent_subject="local-owner-agent",
                    terminal_agent_token_label="local-owner-read",
                ),
            )

            response = await _get_config_status(
                app,
                authorization="Bearer terminal-read-token",
            )
            app_store.close()

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        response_text = json.dumps(payload)
        self.assertEqual(payload["config_status"]["product"], "example-site")
        self.assertNotIn("https://internal.example-site.invalid", response_text)
        self.assertNotIn("super-secret-password", response_text)

    async def test_config_status_rejects_wrong_terminal_agent_bearer_token(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_terminal_agent_product_environment_read_policy(context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=BearerIdentityConfig(
                terminal_agent_token="terminal-read-token",
                terminal_agent_subject="local-owner-agent",
                terminal_agent_token_label="local-owner-read",
            ),
        )

        response = await _get_config_status(app, authorization="Bearer wrong-token")

        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authentication_required")

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


class FastApiProductEnvironmentReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_repo_product_mapping_returns_managed_and_awareness_repos(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _seed_agent_context_read_records(database_url)
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_environment_read_policy(
                    context="launchplane", products=("launchplane", "example-site")
                ),
                record_store_factory=lambda: app_store,
            )

            response = await _get_repo_product_mapping(app)
            app_store.close()

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(set(payload), {"status", "trace_id", "mapping", "source"})
        repositories = payload["mapping"]["repositories"]
        by_repository = {repository["repository"]: repository for repository in repositories}
        self.assertEqual(by_repository["every/example-site"]["classification"], "managed_runtime")
        self.assertEqual(by_repository["every/example-site"]["product"], "example-site")
        self.assertEqual(by_repository["every/example-site"]["environments"], ["testing", "prod"])
        self.assertEqual(by_repository["cbusillo/tooling"]["classification"], "active_awareness")
        self.assertEqual(payload["source"]["product_count"], 1)
        self.assertEqual(payload["source"]["work_request_count"], 2)

    async def test_agent_context_returns_thin_aggregated_read_models(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _seed_agent_context_read_records(database_url)
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_environment_read_policy(
                    contexts=("launchplane", "example-site"),
                    products=("launchplane", "example-site"),
                ),
                record_store_factory=lambda: app_store,
            )

            response = await _get_agent_context(app, repository="every/example-site")
            app_store.close()

        self.assertEqual(response.status_code, 200)
        context = response.json()["context"]
        self.assertEqual(context["schema_version"], 1)
        self.assertEqual(context["repository"], "every/example-site")
        sections = context["sections"]
        self.assertEqual(sections["repo_product_mapping"]["status"], "available")
        self.assertEqual(sections["work_graph_snapshot"]["status"], "available")
        self.assertEqual(sections["every_code_summary"]["status"], "available")
        self.assertEqual(sections["preview_readiness"]["status"], "available")
        summary = sections["every_code_summary"]["payload"]["summary"]
        self.assertEqual(summary["repository"], "every/example-site")
        self.assertEqual(summary["summaries"][0]["issue_number"], 190)
        readiness = sections["preview_readiness"]["payload"]["readiness"]
        self.assertEqual(readiness["repository"], "every/example-site")
        self.assertEqual(readiness["items"][0]["readiness_status"], "ready")

    async def test_agent_context_marks_work_graph_unavailable_without_dropping_sections(
        self,
    ) -> None:
        def unavailable_planning_facts() -> tuple[WorkGraphPlanningIssueFacts, ...]:
            raise RuntimeError("planning provider unavailable")

        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _seed_empty_agent_context_read_store(database_url)
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_environment_read_policy(
                    context="launchplane", products=("launchplane",)
                ),
                record_store_factory=lambda: app_store,
                work_graph_planning_facts_provider=unavailable_planning_facts,
            )

            response = await _get_agent_context(app)
            app_store.close()

        self.assertEqual(response.status_code, 200)
        sections = response.json()["context"]["sections"]
        self.assertEqual(sections["repo_product_mapping"]["status"], "available")
        self.assertEqual(sections["work_graph_snapshot"]["status"], "unavailable")
        self.assertEqual(sections["work_graph_snapshot"]["reason_code"], "work_graph_unavailable")
        self.assertEqual(sections["every_code_summary"]["status"], "available")
        self.assertEqual(sections["preview_readiness"]["status"], "available")

    async def test_work_graph_snapshot_returns_launchplane_assembled_snapshot(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _seed_agent_context_read_records(database_url)
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_work_graph_read_policy(),
                record_store_factory=lambda: app_store,
            )

            response = await _get_work_graph_snapshot(app)
            app_store.close()

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(set(payload), {"status", "trace_id", "snapshot", "source"})
        self.assertEqual(payload["source"]["product_count"], 1)
        self.assertEqual(payload["source"]["work_request_count"], 2)
        self.assertEqual(payload["source"]["planning_fact_count"], 0)
        snapshot = payload["snapshot"]
        self.assertEqual(snapshot["schema_version"], 1)
        repos_by_name = {repo["repository"]: repo for repo in snapshot["repos"]}
        self.assertEqual(repos_by_name["every/example-site"]["classification"], "managed_runtime")
        self.assertEqual(repos_by_name["every/example-site"]["product"], "example-site")
        self.assertEqual(repos_by_name["cbusillo/tooling"]["classification"], "active_awareness")
        issues_by_key = {
            (issue["repository"], issue["number"]): issue for issue in snapshot["issues"]
        }
        self.assertEqual(issues_by_key[("every/example-site", 190)]["focus"], "Next")

    async def test_work_graph_snapshot_uses_compact_planning_facts_when_available(
        self,
    ) -> None:
        def planning_facts() -> tuple[WorkGraphPlanningIssueFacts, ...]:
            return (
                WorkGraphPlanningIssueFacts.model_validate(
                    {
                        "repository": "every/example-site",
                        "number": 190,
                        "focus": "Now",
                        "manager": "Chris",
                        "finish_line": "Project fields are visible in the cockpit.",
                        "labels": ("plan", "plan:active"),
                        "blocking": 1,
                        "updated_at": "2026-05-06T03:54:00Z",
                    }
                ),
            )

        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _seed_agent_context_read_records(database_url)
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_work_graph_read_policy(),
                record_store_factory=lambda: app_store,
                work_graph_planning_facts_provider=planning_facts,
            )

            response = await _get_work_graph_snapshot(app)
            app_store.close()

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["source"]["planning_fact_count"], 1)
        issues_by_key = {
            (issue["repository"], issue["number"]): issue for issue in payload["snapshot"]["issues"]
        }
        issue = issues_by_key[("every/example-site", 190)]
        self.assertEqual(issue["focus"], "Now")
        self.assertEqual(issue["manager"], "Chris")
        self.assertEqual(issue["finish_line"], "Project fields are visible in the cockpit.")
        self.assertEqual(issue["labels"], ["every-code", "plan", "plan:active"])
        self.assertEqual(issue["blocking"], 1)
        self.assertEqual(issue["updated_at"], "2026-05-06T03:54:00Z")

    async def test_work_graph_snapshot_rejects_unauthorized_identity(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _get_work_graph_snapshot(app)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_work_graph_snapshot_requires_database_backed_store(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_work_graph_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _get_work_graph_snapshot(app)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "database_storage_required")

    async def test_work_graph_rank_returns_ranked_queue(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_work_graph_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_work_graph_rank(
            app,
            payload={"snapshot": _work_graph_snapshot_payload(), "limit": 1},
        )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(payload["records"], {})
        queue = payload["result"]["queue"]
        self.assertEqual(len(queue["items"]), 1)
        self.assertEqual(queue["hidden_count"], 1)
        self.assertEqual(queue["items"][0]["number"], 190)
        self.assertEqual(queue["items"][0]["recommendation"], "deep_work")

    async def test_work_graph_rank_rejects_unauthorized_identity(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_work_graph_rank(
            app,
            payload={"snapshot": _work_graph_snapshot_payload()},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_work_graph_rank_rejects_owner_agent_bearer_identities(
        self,
    ) -> None:
        cases = (
            (
                "terminal_agent",
                _terminal_agent_work_graph_rank_policy(),
                BearerIdentityConfig(
                    terminal_agent_token="terminal-read-token",
                    terminal_agent_subject="local-owner-agent",
                    terminal_agent_token_label="local-owner-read",
                ),
                "terminal-read-token",
            ),
            (
                "local_operator",
                _local_operator_work_graph_rank_policy(),
                _local_operator_bearer_config(),
                "local-operator-token",
            ),
            (
                "local_admin",
                _local_admin_work_graph_rank_policy(),
                BearerIdentityConfig(
                    local_admin_token="local-admin-token",
                    local_admin_subject="local-owner-agent",
                    local_admin_token_label="local-owner-admin",
                ),
                "local-admin-token",
            ),
        )
        for label, policy, bearer_config, token in cases:
            with self.subTest(identity=label):
                app = create_launchplane_fastapi_app(
                    verifier=_RejectingVerifier(),
                    authz_policy=policy,
                    bearer_identity_config=bearer_config,
                    record_store_factory=lambda: _MissingProductReadStore(),
                )

                response = await _post_work_graph_rank(
                    app,
                    payload={"snapshot": _work_graph_snapshot_payload(), "limit": 1},
                    authorization=f"Bearer {token}",
                )

                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_work_graph_rank_rejects_unclassified_issue(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_work_graph_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )
        snapshot = _work_graph_snapshot_payload()
        snapshot["repos"] = []

        response = await _post_work_graph_rank(app, payload={"snapshot": snapshot})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")

    async def test_human_session_can_rank_work_graph_snapshot(self) -> None:
        oauth_config = _github_oauth_config()
        session_store = InMemoryHumanSessionStore()
        session_manager = HumanSessionManager(config=oauth_config, session_store=session_store)
        human_session = session_manager.issue(_github_human_identity())
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_github_human_work_graph_rank_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            human_session_manager=session_manager,
        )

        response = await _post_work_graph_rank(
            app,
            payload={"snapshot": _work_graph_snapshot_payload(), "limit": 1},
            authorization="",
            headers={"Cookie": session_manager.session_cookie_header(human_session)},
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["result"]["queue"]["items"][0]["number"], 190)

    async def test_work_graph_issue_inbox_returns_provider_payload(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_work_graph_read_policy(products=("launchplane",)),
            record_store_factory=lambda: _MissingProductReadStore(),
            work_graph_issue_inbox_provider=lambda: GitHubIssueInboxReadModel.model_validate(
                {
                    "generated_at": "2026-05-21T12:00:00Z",
                    "project_configured": True,
                    "repository_count": 1,
                    "issue_count": 2,
                    "stale_project_item_count": 1,
                    "repositories": [
                        {
                            "repository": "cbusillo/launchplane",
                            "issue_count": 2,
                            "present_in_project_count": 1,
                            "missing_from_project_count": 1,
                            "issues": [
                                {
                                    "key": "cbusillo/launchplane#697",
                                    "repository": "cbusillo/launchplane",
                                    "number": 697,
                                    "title": "Add read-only grouped GitHub issue inbox",
                                    "url": "https://github.com/cbusillo/launchplane/issues/697",
                                    "state": "OPEN",
                                    "project_status": "missing",
                                    "present_in_project": False,
                                },
                                {
                                    "key": "cbusillo/launchplane#601",
                                    "repository": "cbusillo/launchplane",
                                    "number": 601,
                                    "title": "Closed Project item",
                                    "url": "https://github.com/cbusillo/launchplane/issues/601",
                                    "state": "closed",
                                    "project_status": "closed",
                                    "present_in_project": True,
                                },
                            ],
                        }
                    ],
                }
            ),
        )

        response = await _get_work_graph_issue_inbox(app)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["configured"])
        inbox = payload["inbox"]
        self.assertEqual(inbox["repository_count"], 1)
        self.assertEqual(inbox["stale_project_item_count"], 1)
        self.assertEqual(inbox["repositories"][0]["issues"][0]["key"], "cbusillo/launchplane#697")
        self.assertIs(inbox["repositories"][0]["issues"][0]["present_in_project"], False)
        self.assertEqual(inbox["repositories"][0]["issues"][1]["project_status"], "closed")

    async def test_work_graph_issue_inbox_returns_empty_payload_without_provider(
        self,
    ) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_work_graph_read_policy(products=("launchplane",)),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _get_work_graph_issue_inbox(app)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["configured"])
        self.assertEqual(payload["inbox"]["generated_at"], "")
        self.assertFalse(payload["inbox"]["project_configured"])
        self.assertEqual(payload["inbox"]["repository_count"], 0)
        self.assertEqual(payload["inbox"]["issue_count"], 0)
        self.assertEqual(payload["inbox"]["repositories"], [])

    async def test_work_graph_issue_inbox_rejects_unauthorized_identity(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            work_graph_issue_inbox_provider=lambda: GitHubIssueInboxReadModel(
                generated_at="2026-05-21T12:00:00Z",
                repository_count=0,
                issue_count=0,
            ),
        )

        response = await _get_work_graph_issue_inbox(app)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_work_graph_issue_inbox_reconcile_dry_run_returns_missing_items(
        self,
    ) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_work_graph_read_policy(products=("launchplane",)),
            record_store_factory=lambda: _MissingProductReadStore(),
            work_graph_issue_inbox_reconcile_provider=lambda request: (
                GitHubIssueInboxReconcileResult.model_validate(
                    {
                        "generated_at": "2026-05-21T12:00:00Z",
                        "mode": request.mode,
                        "repository_count": 1,
                        "issue_count": 1,
                        "would_add_count": 1,
                        "items": [
                            {
                                "key": "cbusillo/launchplane#698",
                                "repository": "cbusillo/launchplane",
                                "number": 698,
                                "title": "Reconcile missing GitHub issues into Code Plans",
                                "url": "https://github.com/cbusillo/launchplane/issues/698",
                                "action": "would_add",
                            }
                        ],
                    }
                )
            ),
        )

        response = await _post_work_graph_issue_inbox_reconcile(
            app,
            payload={"mode": "dry_run"},
        )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        reconcile = payload["result"]["reconcile"]
        self.assertEqual(reconcile["mode"], "dry_run")
        self.assertEqual(reconcile["would_add_count"], 1)
        self.assertEqual(reconcile["items"][0]["action"], "would_add")

    async def test_work_graph_issue_inbox_reconcile_apply_requires_reconcile_action(
        self,
    ) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_work_graph_read_policy(products=("launchplane",)),
            record_store_factory=lambda: _MissingProductReadStore(),
            work_graph_issue_inbox_reconcile_provider=lambda request: (
                GitHubIssueInboxReconcileResult.model_validate(
                    {
                        "generated_at": "2026-05-21T12:00:00Z",
                        "mode": request.mode,
                        "repository_count": 0,
                        "issue_count": 0,
                    }
                )
            ),
        )

        response = await _post_work_graph_issue_inbox_reconcile(
            app,
            payload={"mode": "apply"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_work_graph_issue_inbox_reconcile_apply_returns_counts(self) -> None:
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_actions": [
                    {
                        "repository": "every/verireel",
                        "workflow_refs": [
                            "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                        ],
                        "event_names": ["pull_request"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": ["work_graph.issue_inbox.reconcile"],
                    }
                ]
            }
        )
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=policy,
            record_store_factory=lambda: _MissingProductReadStore(),
            work_graph_issue_inbox_reconcile_provider=lambda request: (
                GitHubIssueInboxReconcileResult.model_validate(
                    {
                        "generated_at": "2026-05-21T12:00:00Z",
                        "mode": request.mode,
                        "repository_count": 1,
                        "issue_count": 1,
                        "added_count": 1,
                        "items": [
                            {
                                "key": "cbusillo/launchplane#698",
                                "repository": "cbusillo/launchplane",
                                "number": 698,
                                "url": "https://github.com/cbusillo/launchplane/issues/698",
                                "action": "added",
                            }
                        ],
                    }
                )
            ),
        )

        response = await _post_work_graph_issue_inbox_reconcile(
            app,
            payload={"mode": "apply"},
        )

        self.assertEqual(response.status_code, 202)
        reconcile = response.json()["result"]["reconcile"]
        self.assertEqual(reconcile["added_count"], 1)
        self.assertEqual(reconcile["items"][0]["action"], "added")

    async def test_work_graph_issue_inbox_reconcile_returns_invalid_request_without_provider(
        self,
    ) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_work_graph_read_policy(products=("launchplane",)),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_work_graph_issue_inbox_reconcile(
            app,
            payload={"mode": "dry_run"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")

    async def test_terminal_agent_cannot_reconcile_work_graph_issue_inbox(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_terminal_agent_work_graph_rank_policy(),
            bearer_identity_config=BearerIdentityConfig(
                terminal_agent_token="terminal-read-token",
                terminal_agent_subject="local-owner-agent",
                terminal_agent_token_label="local-owner-read",
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
            work_graph_issue_inbox_reconcile_provider=lambda request: (
                GitHubIssueInboxReconcileResult.model_validate(
                    {
                        "generated_at": "2026-05-21T12:00:00Z",
                        "mode": request.mode,
                        "repository_count": 0,
                        "issue_count": 0,
                    }
                )
            ),
        )

        response = await _post_work_graph_issue_inbox_reconcile(
            app,
            payload={"mode": "dry_run"},
            authorization="Bearer terminal-read-token",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_agent_context_reads_require_identity(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_environment_read_policy(
                context="launchplane", products=("launchplane",)
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        responses = [
            await _get_repo_product_mapping(app, authorization=""),
            await _get_agent_context(app, authorization=""),
            await _get_work_graph_snapshot(app, authorization=""),
            await _post_work_graph_rank(
                app,
                payload={"snapshot": _work_graph_snapshot_payload()},
                authorization="",
            ),
            await _get_work_graph_issue_inbox(app, authorization=""),
            await _post_work_graph_issue_inbox_reconcile(
                app,
                payload={"mode": "dry_run"},
                authorization="",
            ),
        ]

        for response in responses:
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json()["error"]["code"], "authentication_required")

    async def test_agent_context_reads_reject_unauthorized_identity(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        mapping_response = await _get_repo_product_mapping(app)
        context_response = await _get_agent_context(app)

        self.assertEqual(mapping_response.status_code, 403)
        self.assertEqual(context_response.status_code, 403)
        self.assertEqual(mapping_response.json()["error"]["code"], "authorization_denied")
        self.assertEqual(context_response.json()["error"]["code"], "authorization_denied")

    async def test_agent_context_reads_require_database_backed_store(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_environment_read_policy(
                context="launchplane", products=("launchplane",)
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        mapping_response = await _get_repo_product_mapping(app)
        context_response = await _get_agent_context(app)

        self.assertEqual(mapping_response.status_code, 503)
        self.assertEqual(context_response.status_code, 503)
        self.assertEqual(mapping_response.json()["error"]["code"], "database_storage_required")
        self.assertEqual(context_response.json()["error"]["code"], "database_storage_required")

    async def test_agent_context_reads_reject_filesystem_store(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            record_store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            record_store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_generic_site_profile_payload())
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_environment_read_policy(
                    context="launchplane", products=("launchplane",)
                ),
                record_store_factory=lambda: record_store,
            )

            mapping_response = await _get_repo_product_mapping(app)
            context_response = await _get_agent_context(app)

        self.assertEqual(mapping_response.status_code, 503)
        self.assertEqual(context_response.status_code, 503)
        self.assertEqual(mapping_response.json()["error"]["code"], "database_storage_required")
        self.assertEqual(context_response.json()["error"]["code"], "database_storage_required")
        self.assertIn("postgres_storage", mapping_response.json()["error"]["message"])
        self.assertIn("postgres_storage", context_response.json()["error"]["message"])

    async def test_agent_context_accepts_terminal_agent_identity(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _seed_agent_context_read_records(database_url)
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_terminal_agent_launchplane_read_policy(),
                record_store_factory=lambda: app_store,
                bearer_identity_config=BearerIdentityConfig(
                    terminal_agent_token="terminal-read-token",
                    terminal_agent_subject="local-owner-agent",
                    terminal_agent_token_label="local-owner-read",
                ),
            )

            response = await _get_agent_context(
                app,
                repository="every/example-site",
                authorization="Bearer terminal-read-token",
            )
            mapping_response = await _get_repo_product_mapping(
                app,
                authorization="Bearer terminal-read-token",
            )
            app_store.close()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(mapping_response.status_code, 200)
        sections = response.json()["context"]["sections"]
        self.assertEqual(sections["repo_product_mapping"]["status"], "available")
        self.assertEqual(sections["work_graph_snapshot"]["status"], "available")
        self.assertEqual(sections["every_code_summary"]["status"], "available")
        self.assertEqual(sections["preview_readiness"]["status"], "available")

    async def test_openapi_includes_agent_context_read_contracts(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_environment_read_policy(
                context="launchplane", products=("launchplane",)
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        expected_routes = {
            "/v1/repo-product-mapping": (
                "read_repo_product_mapping",
                "RepoProductMappingResponse",
            ),
            "/v1/agent/context": ("read_agent_context", "AgentContextResponse"),
            "/v1/work-graph/snapshot": (
                "read_work_graph_snapshot",
                "WorkGraphSnapshotResponse",
            ),
            "/v1/work-graph/github/issues": (
                "read_work_graph_issue_inbox",
                "WorkGraphIssueInboxResponse",
            ),
        }
        for path, (operation_id, response_model_name) in expected_routes.items():
            route = openapi["paths"][path]["get"]
            self.assertEqual(route["operationId"], operation_id)
            self.assertEqual(
                route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
                f"#/components/schemas/{response_model_name}",
            )
            for status_code in ("401", "403", "503"):
                self.assertIn(
                    "LaunchplaneErrorResponse", json.dumps(route["responses"][status_code])
                )
            self.assertFalse(
                openapi["components"]["schemas"][response_model_name]["additionalProperties"]
            )
        rank_route = openapi["paths"]["/v1/work-graph/rank"]["post"]
        self.assertEqual(rank_route["operationId"], "rank_work_graph_snapshot")
        self.assertEqual(
            rank_route["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/AcceptedEvidenceResponse",
        )
        for status_code in ("400", "401", "403"):
            self.assertIn(
                "LaunchplaneErrorResponse", json.dumps(rank_route["responses"][status_code])
            )
        reconcile_route = openapi["paths"]["/v1/work-graph/github/issues/reconcile"]["post"]
        self.assertEqual(
            reconcile_route["operationId"],
            "reconcile_work_graph_issue_inbox",
        )
        self.assertEqual(
            reconcile_route["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/AcceptedEvidenceResponse",
        )
        for status_code in ("400", "401", "403"):
            self.assertIn(
                "LaunchplaneErrorResponse",
                json.dumps(reconcile_route["responses"][status_code]),
            )

    async def test_fastapi_agent_context_reads_precede_legacy_wsgi_fallback(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _seed_agent_context_read_records(database_url)
            app_store = PostgresRecordStore(database_url=database_url)
            policy = _work_graph_read_policy()
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                record_store_factory=lambda: app_store,
            )
            legacy_app = create_launchplane_service_app(
                state_dir=root / "legacy-state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({}),
                control_plane_root_path=root,
            )
            app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

            mapping_response = await _get_repo_product_mapping(app)
            context_response = await _get_agent_context(app, repository="every/example-site")
            snapshot_response = await _get_work_graph_snapshot(app)
            issue_inbox_response = await _get_work_graph_issue_inbox(app)
            app_store.close()

        self.assertEqual(mapping_response.status_code, 200)
        self.assertEqual(context_response.status_code, 200)
        self.assertEqual(snapshot_response.status_code, 200)
        self.assertEqual(issue_inbox_response.status_code, 200)

    async def test_list_products_returns_db_backed_overviews(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _seed_product_environment_read_records(database_url)
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_environment_read_policy(
                    context="launchplane", products=("launchplane", "example-site")
                ),
                record_store_factory=lambda: app_store,
            )

            response = await _get_products(app)
            app_store.close()

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        response_text = json.dumps(payload)
        self.assertEqual(set(payload), {"status", "trace_id", "products"})
        self.assertEqual(payload["status"], "ok")
        self.assertEqual([product["product"] for product in payload["products"]], ["example-site"])
        self.assertEqual(payload["products"][0]["driver_id"], "generic-web")
        self.assertNotIn("https://internal.example-site.invalid", response_text)
        self.assertNotIn("super-secret-password", response_text)

    async def test_read_product_returns_overview(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _seed_product_environment_read_records(database_url)
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_environment_read_policy(context="launchplane"),
                record_store_factory=lambda: app_store,
            )

            response = await _get_product(app)
            app_store.close()

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(set(payload), {"status", "trace_id", "product"})
        product = payload["product"]
        self.assertEqual(product["product"], "example-site")
        self.assertEqual(product["driver_id"], "generic-web")
        self.assertEqual(
            [environment["environment"] for environment in product["environments"]],
            ["testing", "prod"],
        )

    async def test_read_product_activity_returns_timeline(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _seed_product_environment_read_records(database_url)
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_environment_read_policy(context="launchplane"),
                record_store_factory=lambda: app_store,
            )

            response = await _get_product_activity(app)
            app_store.close()

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(set(payload), {"status", "trace_id", "activity"})
        self.assertEqual(payload["activity"]["product"], "example-site")
        self.assertEqual(payload["activity"]["events"][0]["event_type"], "authz_policy")

    async def test_list_product_environments_returns_redacted_summaries(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _seed_product_environment_read_records(database_url)
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_environment_read_policy(context="launchplane"),
                record_store_factory=lambda: app_store,
            )

            response = await _get_product_environments(app)
            app_store.close()

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        response_text = json.dumps(payload)
        self.assertEqual(payload["product"], "example-site")
        self.assertEqual(
            [environment["environment"] for environment in payload["environments"]],
            ["testing", "prod"],
        )
        self.assertNotIn("https://internal.example-site.invalid", response_text)
        self.assertNotIn("super-secret-password", response_text)

    async def test_read_product_environment_redacts_runtime_and_secret_values(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _seed_product_environment_read_records(database_url)
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_environment_read_policy(context="example-site"),
                record_store_factory=lambda: app_store,
            )

            response = await _get_product_environment(app)
            app_store.close()

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        response_text = json.dumps(payload)
        environment = payload["environment"]
        self.assertEqual(environment["product"], "example-site")
        self.assertEqual(environment["environment"], "prod")
        self.assertEqual(environment["target"]["target_name"], "example-site-prod")
        self.assertTrue(environment["target"]["target_id_recorded"])
        self.assertEqual(environment["runtime_settings"][0]["env_keys"], ["INTERNAL_CALLBACK_URL"])
        self.assertEqual(environment["managed_secrets"][0]["binding_key"], "SMTP_PASSWORD")
        self.assertNotIn("https://internal.example-site.invalid", response_text)
        self.assertNotIn("super-secret-password", response_text)

    async def test_product_environment_reads_require_identity(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_environment_read_policy(context="launchplane"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        responses = [
            await _get_products(app, authorization=""),
            await _get_product(app, authorization=""),
            await _get_product_activity(app, authorization=""),
            await _get_product_environments(app, authorization=""),
            await _get_product_environment(app, authorization=""),
        ]

        for response in responses:
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json()["error"]["code"], "authentication_required")

    async def test_list_products_rejects_unauthorized_identity(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _get_products(app)

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    async def test_product_environment_reads_reject_unauthorized_context(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _seed_product_environment_read_records(database_url)
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_environment_read_policy(context="launchplane"),
                record_store_factory=lambda: app_store,
            )

            response = await _get_product_environment(app)
            app_store.close()

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    async def test_product_environment_reads_return_not_found(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _seed_product_environment_read_records(database_url)
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_environment_read_policy(context="example-site"),
                record_store_factory=lambda: app_store,
            )

            missing_product = await _get_product_environment(app, product="missing-site")
            missing_environment = await _get_product_environment(app, environment="staging")
            app_store.close()

        self.assertEqual(missing_product.status_code, 404)
        self.assertEqual(
            missing_product.json()["error"]["message"], "Product 'missing-site' was not found."
        )
        self.assertEqual(missing_environment.status_code, 404)
        self.assertEqual(
            missing_environment.json()["error"]["message"],
            "Product 'example-site' has no environment 'staging'.",
        )

    async def test_product_environment_reads_require_db_backed_store(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_environment_read_policy(
                context="launchplane", products=("launchplane", "example-site")
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        list_response = await _get_products(app)
        detail_response = await _get_product_environment(app)

        self.assertEqual(list_response.status_code, 503)
        self.assertEqual(detail_response.status_code, 503)
        self.assertEqual(list_response.json()["error"]["code"], "database_storage_required")
        self.assertEqual(detail_response.json()["error"]["code"], "database_storage_required")

    async def test_product_environment_reads_accept_local_operator_identity(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _seed_product_environment_read_records(database_url)
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_local_operator_product_environment_read_policy(
                    context="example-site"
                ),
                record_store_factory=lambda: app_store,
                bearer_identity_config=_local_operator_bearer_config(),
            )

            response = await _get_product_environment(
                app,
                authorization="Bearer local-operator-token",
            )
            app_store.close()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["environment"]["product"], "example-site")

    async def test_product_environment_reads_accept_terminal_agent_identity(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _seed_product_environment_read_records(database_url)
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_terminal_agent_product_environment_read_policy(
                    context="example-site"
                ),
                record_store_factory=lambda: app_store,
                bearer_identity_config=BearerIdentityConfig(
                    terminal_agent_token="terminal-read-token",
                    terminal_agent_subject="local-owner-agent",
                    terminal_agent_token_label="local-owner-read",
                ),
            )

            response = await _get_product_environment(
                app,
                authorization="Bearer terminal-read-token",
            )
            app_store.close()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["environment"]["product"], "example-site")

    async def test_openapi_includes_product_environment_read_contracts(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_environment_read_policy(context="launchplane"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        expected_routes = {
            "/v1/products": ("list_products", "ProductEnvironmentListResponse"),
            "/v1/products/{product}": ("read_product", "ProductOverviewResponse"),
            "/v1/products/{product}/activity": (
                "read_product_activity",
                "ProductActivityResponse",
            ),
            "/v1/products/{product}/environments": (
                "list_product_environments",
                "ProductEnvironmentsResponse",
            ),
            "/v1/products/{product}/environments/{environment}": (
                "read_product_environment",
                "ProductEnvironmentResponse",
            ),
        }
        for path, (operation_id, response_model_name) in expected_routes.items():
            route = openapi["paths"][path]["get"]
            self.assertEqual(route["operationId"], operation_id)
            self.assertEqual(
                route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
                f"#/components/schemas/{response_model_name}",
            )
            for status_code in ("400", "401", "403", "404", "503"):
                self.assertIn(
                    "LaunchplaneErrorResponse", json.dumps(route["responses"][status_code])
                )
            self.assertFalse(
                openapi["components"]["schemas"][response_model_name]["additionalProperties"]
            )

    async def test_fastapi_product_environment_reads_precede_legacy_wsgi_fallback(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _seed_product_environment_read_records(database_url)
            app_store = PostgresRecordStore(database_url=database_url)
            policy = _product_environment_read_policy(
                contexts=("launchplane", "example-site"),
                products=("launchplane", "example-site"),
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                record_store_factory=lambda: app_store,
            )
            legacy_app = create_launchplane_service_app(
                state_dir=root / "legacy-state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({}),
                control_plane_root_path=root,
            )
            app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

            list_response = await _get_products(app)
            overview_response = await _get_product(app)
            activity_response = await _get_product_activity(app)
            environments_response = await _get_product_environments(app)
            config_status_response = await _get_config_status(app)
            app_store.close()

        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(overview_response.status_code, 200)
        self.assertEqual(activity_response.status_code, 200)
        self.assertEqual(environments_response.status_code, 200)
        self.assertEqual(config_status_response.status_code, 200)


class FastApiMergeTrainReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_admission_reads_store_decision(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            _seed_merge_train_policy(state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )

            response = await _asgi_get(
                app,
                "/v1/work-graph/merge-train/admission?repository=cbusillo/sellyouroutboard&base_branch=main",
                headers={"Authorization": "Bearer valid-token"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["admission"]["repository"], "cbusillo/sellyouroutboard")
        self.assertEqual(payload["admission"]["base_branch"], "main")
        self.assertEqual(payload["admission"]["status"], "admitted")
        self.assertEqual(payload["admission"]["reason_code"], "no_prior_run")

    async def test_controller_status_reads_stored_dry_run(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            _seed_merge_train_policy(state_dir)
            run_record = _merge_train_run_record(recorded_at="2026-05-20T12:00:00Z")
            store.write_merge_train_run_record(run_record)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )

            response = await _asgi_get(
                app,
                "/v1/work-graph/merge-train/controller/status?repository=cbusillo/sellyouroutboard&base_branch=main",
                headers={"Authorization": "Bearer valid-token"},
            )

        self.assertEqual(response.status_code, 200)
        controller_status = response.json()["controller_status"]
        self.assertEqual(controller_status["repository"], "cbusillo/sellyouroutboard")
        self.assertEqual(controller_status["base_branch"], "main")
        self.assertEqual(controller_status["latest_run"]["run_id"], run_record.run_id)
        self.assertEqual(controller_status["latest_dry_run"]["queue_count"], 1)
        self.assertEqual(controller_status["latest_dry_run"]["selected_pr_number"], 1)

    async def test_policy_targets_lists_authorized_policy_targets(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            policy_record = _seed_merge_train_policy(
                state_dir,
                policy=MergeTrainPolicyRecord(
                    record_id="merge-train-policy-targets-fastapi-test",
                    status="active",
                    source="test",
                    updated_at="2026-05-13T21:00:00Z",
                    policy=parse_merge_train_policy_toml(
                        "\n\n".join(
                            (
                                "schema_version = 1",
                                _merge_train_policy_table("cbusillo/sellyouroutboard", "release"),
                                _merge_train_policy_table(
                                    "cbusillo/codex-skills",
                                    "main",
                                    scheduler_enabled=True,
                                    scheduler_runner_mode="level1",
                                    scheduler_mutate=True,
                                ),
                            )
                        )
                    ),
                ),
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )

            response = await _asgi_get(
                app,
                "/v1/work-graph/merge-train/policy-targets",
                headers={"Authorization": "Bearer valid-token"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["policy"]["record_id"], policy_record.record_id)
        self.assertEqual(payload["policy"]["policy_sha256"], policy_record.policy_sha256)
        self.assertEqual(
            [(target["repository"], target["base_branch"]) for target in payload["targets"]],
            [("cbusillo/codex-skills", "main"), ("cbusillo/sellyouroutboard", "release")],
        )
        self.assertEqual(payload["targets"][0]["scheduler"]["runner_mode"], "level1")

    async def test_policy_targets_allows_local_operator_visibility(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            _seed_merge_train_policy(
                state_dir,
                policy=MergeTrainPolicyRecord(
                    record_id="merge-train-policy-targets-local-operator-fastapi-test",
                    status="active",
                    source="test",
                    updated_at="2026-05-13T21:00:00Z",
                    policy=build_test_merge_train_policy_with_codex_skills(),
                ),
            )
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_local_operator_policy(
                    actions=("merge_train.policy_targets",),
                    products=("launchplane",),
                    contexts=("launchplane",),
                ),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(
                    local_operator_token="local-operator-token",
                    local_operator_subject="local-owner-agent",
                    local_operator_token_label="local-owner-write",
                ),
            )

            response = await _asgi_get(
                app,
                "/v1/work-graph/merge-train/policy-targets",
                headers={"Authorization": "Bearer local-operator-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [
                (target["repository"], target["base_branch"])
                for target in response.json()["targets"]
            ],
            [("cbusillo/codex-skills", "main"), ("cbusillo/sellyouroutboard", "main")],
        )

    async def test_admission_rejects_unauthorized_identity(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            _seed_merge_train_policy(state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                record_store_factory=lambda: store,
            )

            response = await _asgi_get(
                app,
                "/v1/work-graph/merge-train/admission?repository=cbusillo/sellyouroutboard&base_branch=main",
                headers={"Authorization": "Bearer valid-token"},
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_openapi_includes_merge_train_read_contracts(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_merge_train_service_identity()),
            authz_policy=_merge_train_service_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        expected_routes = {
            "/v1/work-graph/merge-train/admission": "MergeTrainAdmissionResponse",
            "/v1/work-graph/merge-train/controller/status": "MergeTrainControllerStatusResponse",
            "/v1/work-graph/merge-train/policy-targets": "MergeTrainPolicyTargetsResponse",
        }
        for path, response_model_name in expected_routes.items():
            route = openapi["paths"][path]["get"]
            success_schema = route["responses"]["200"]["content"]["application/json"]["schema"]
            self.assertEqual(success_schema["$ref"], f"#/components/schemas/{response_model_name}")
            self.assertTrue(set(route["responses"]) >= {"200", "401", "403", "503"})
            self.assertEqual(
                openapi["components"]["schemas"][response_model_name]["additionalProperties"],
                False,
            )

    async def test_fastapi_merge_train_reads_precede_legacy_wsgi_fallback(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            _seed_merge_train_policy(state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                record_store_factory=lambda: store,
            )
            legacy_app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=root,
                local_record_store_for_tests=store,
            )
            app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

            response = await _asgi_get(
                app,
                "/v1/work-graph/merge-train/admission?repository=cbusillo/sellyouroutboard&base_branch=main",
                headers={"Authorization": "Bearer valid-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["admission"]["status"], "admitted")


class FastApiAgentWriteIntentEvaluateTests(unittest.IsolatedAsyncioTestCase):
    async def test_evaluate_returns_allowed_dry_run_without_execution(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_agent_write_intent_policy(
                    actions=("every_code_work_request.rerun",),
                    product="launchplane",
                    context="launchplane",
                ),
                record_store_factory=lambda: store,
            )

            response = await _post_agent_write_intent_evaluate(
                app,
                _agent_write_intent_payload(
                    intent="every_code_rerun",
                    mode="dry_run",
                    product="launchplane",
                    context="launchplane",
                    reason="Check whether rerun can be requested safely.",
                ),
                idempotency_key="agent-write-intent-rerun",
            )
            record_pointer = response.json()["result"]["record"]
            record = store.read_agent_write_intent_record(record_pointer["record_id"])

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["records"], {})
        intent = payload["result"]["intent"]
        self.assertEqual(intent["status"], "allowed")
        self.assertEqual(intent["authz_action"], "every_code_work_request.rerun")
        self.assertFalse(intent["safe_to_execute"])
        self.assertEqual(intent["reason_code"], "authorized")
        self.assertEqual(intent["audit"]["decision"], "allowed")
        self.assertEqual(intent["audit"]["subject"]["action_safety"], "safe_write")
        self.assertEqual(record.evaluation.status, "allowed")
        self.assertEqual(record.evaluation.intent, "every_code_rerun")
        self.assertEqual(record.idempotency_key, "agent-write-intent-rerun")
        self.assertEqual(record.request.source_url, _AGENT_WRITE_INTENT_SOURCE_URL)
        self.assertEqual(record.trace_id, payload["trace_id"])

    async def test_evaluate_replays_idempotent_request(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_agent_write_intent_policy(
                    actions=("every_code_work_request.rerun",),
                    product="launchplane",
                    context="launchplane",
                ),
                record_store_factory=lambda: store,
            )
            payload = _agent_write_intent_payload(
                intent="every_code_rerun",
                mode="dry_run",
                product="launchplane",
                context="launchplane",
            )

            first_response = await _post_agent_write_intent_evaluate(
                app,
                payload,
                idempotency_key="agent-write-intent-replay",
            )
            second_response = await _post_agent_write_intent_evaluate(
                app,
                payload,
                idempotency_key="agent-write-intent-replay",
            )
            records = store.list_agent_write_intent_records(
                product="launchplane",
                context_name="launchplane",
            )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        second_payload = second_response.json()
        self.assertTrue(second_payload["replayed"])
        self.assertEqual(second_payload["original_trace_id"], first_response.json()["trace_id"])
        self.assertEqual(len(records), 1)

    async def test_evaluate_replays_before_write_store_check(self) -> None:
        payload = _agent_write_intent_payload(
            intent="every_code_rerun",
            mode="dry_run",
            product="launchplane",
            context="launchplane",
        )
        record_store = _AgentWriteIntentEvaluateReplayOnlyStore(
            payload=payload,
            idempotency_key="agent-write-intent-replay",
        )
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_agent_write_intent_policy(
                actions=("every_code_work_request.rerun",),
                product="launchplane",
                context="launchplane",
            ),
            record_store_factory=lambda: record_store,
        )

        response = await _post_agent_write_intent_evaluate(
            app,
            payload,
            idempotency_key="agent-write-intent-replay",
        )

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json()["replayed"])
        self.assertEqual(response.json()["original_trace_id"], "launchplane_req_original")
        self.assertEqual(record_store.read_idempotency_calls, 1)

    async def test_evaluate_rejects_reused_idempotency_key(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_agent_write_intent_policy(
                    actions=("every_code_work_request.rerun",),
                    product="launchplane",
                    context="launchplane",
                ),
                record_store_factory=lambda: store,
            )

            first_response = await _post_agent_write_intent_evaluate(
                app,
                _agent_write_intent_payload(
                    intent="every_code_rerun",
                    mode="dry_run",
                    product="launchplane",
                    context="launchplane",
                ),
                idempotency_key="agent-write-intent-reused",
            )
            conflict_response = await _post_agent_write_intent_evaluate(
                app,
                _agent_write_intent_payload(
                    intent="every_code_rerun",
                    mode="dry_run",
                    product="launchplane",
                    context="launchplane",
                    reason="Different request reason.",
                ),
                idempotency_key="agent-write-intent-reused",
            )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(conflict_response.status_code, 409)
        self.assertEqual(conflict_response.json()["error"]["code"], "idempotency_key_reused")

    async def test_evaluate_denies_ungranted_intent_as_preflight_result(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_agent_write_intent_policy(
                    actions=("every_code_work_request.rerun",),
                    product="launchplane",
                    context="launchplane",
                ),
                record_store_factory=lambda: store,
            )

            response = await _post_agent_write_intent_evaluate(
                app,
                _agent_write_intent_payload(
                    intent="promotion_dispatch",
                    mode="apply",
                    product="verireel",
                    context="verireel",
                    reason="Request prod promotion dispatch.",
                ),
            )
            record = store.read_agent_write_intent_record(
                response.json()["result"]["record"]["record_id"]
            )

        self.assertEqual(response.status_code, 202)
        intent = response.json()["result"]["intent"]
        self.assertEqual(intent["status"], "denied")
        self.assertEqual(intent["reason_code"], "authorization_denied")
        self.assertFalse(intent["safe_to_execute"])
        self.assertEqual(intent["audit"]["decision"], "denied")
        self.assertEqual(intent["audit"]["subject"]["action_safety"], "prod")
        self.assertEqual(record.evaluation.status, "denied")

    async def test_evaluate_requires_dry_run_for_config_apply(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_agent_write_intent_policy(
                    actions=("product_config.apply",),
                    product="verireel",
                    context="verireel-testing",
                ),
                record_store_factory=lambda: store,
            )

            response = await _post_agent_write_intent_evaluate(
                app,
                _agent_write_intent_payload(
                    intent="product_config_apply",
                    mode="apply",
                    product="verireel",
                    context="verireel-testing",
                    reason="Apply product config after review.",
                ),
            )

        self.assertEqual(response.status_code, 202)
        intent = response.json()["result"]["intent"]
        self.assertEqual(intent["status"], "denied")
        self.assertEqual(intent["reason_code"], "dry_run_required")
        self.assertFalse(intent["safe_to_execute"])
        self.assertEqual(intent["audit"]["reason_code"], "dry_run_required")

    async def test_terminal_agent_evaluate_checks_scoped_policy(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_terminal_agent_write_intent_policy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(
                    terminal_agent_token="terminal-read-token",
                    terminal_agent_subject="local-owner-agent",
                    terminal_agent_token_label="local-owner-read",
                ),
            )

            response = await _post_agent_write_intent_evaluate(
                app,
                _agent_write_intent_payload(
                    intent="every_code_rerun",
                    mode="dry_run",
                    product="launchplane",
                    context="launchplane",
                    reason="Check whether local agent can request a rerun.",
                ),
                authorization="Bearer terminal-read-token",
            )

        self.assertEqual(response.status_code, 202)
        intent = response.json()["result"]["intent"]
        self.assertEqual(intent["status"], "allowed")
        self.assertEqual(intent["audit"]["subject"]["subject_type"], "terminal_agent")
        self.assertFalse(intent["audit"]["subject"]["approval_capable"])

    async def test_evaluate_checks_secret_binding_policy_without_reveal(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(root / "launchplane.sqlite3")
            )
            store.ensure_schema()
            _seed_agent_write_intent_secret_binding(store, binding_instance="prod")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_agent_write_intent_policy(
                    actions=("product_config.apply", "product_config.apply.secret"),
                    product="sellyouroutboard",
                    context="sellyouroutboard",
                ),
                record_store_factory=lambda: store,
            )

            response = await _post_agent_write_intent_evaluate(
                app,
                _agent_write_intent_payload(
                    intent="product_config_apply",
                    mode="dry_run",
                    product="sellyouroutboard",
                    context="sellyouroutboard",
                    source_url="https://github.com/cbusillo/launchplane/issues/387",
                    reason="Preflight managed secret-backed product config.",
                    secret_bindings=["SMTP_PASSWORD"],
                    destination={
                        "kind": "runtime_environment",
                        "context": "sellyouroutboard",
                        "instance": "prod",
                    },
                ),
            )

        response_text = json.dumps(response.json(), sort_keys=True)
        self.assertEqual(response.status_code, 202)
        intent = response.json()["result"]["intent"]
        self.assertEqual(intent["status"], "allowed")
        self.assertEqual(intent["secret_evidence"]["status"], "pass")
        self.assertEqual(intent["secret_evidence"]["checked_binding_keys"], ["SMTP_PASSWORD"])
        self.assertEqual(intent["secret_evidence"]["findings"], [])
        self.assertNotIn("smtp-secret-value", response_text)
        self.assertNotIn("ciphertext", response_text)

    async def test_evaluate_denies_secret_without_secret_action_grant(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(root / "launchplane.sqlite3")
            )
            store.ensure_schema()
            _seed_agent_write_intent_secret_binding(store, binding_instance="prod")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_agent_write_intent_policy(
                    actions=("product_config.apply",),
                    product="sellyouroutboard",
                    context="sellyouroutboard",
                ),
                record_store_factory=lambda: store,
            )

            response = await _post_agent_write_intent_evaluate(
                app,
                _agent_write_intent_payload(
                    intent="product_config_apply",
                    mode="dry_run",
                    product="sellyouroutboard",
                    context="sellyouroutboard",
                    secret_bindings=["SMTP_PASSWORD"],
                    destination={
                        "kind": "runtime_environment",
                        "context": "sellyouroutboard",
                        "instance": "prod",
                    },
                ),
            )

        self.assertEqual(response.status_code, 202)
        intent = response.json()["result"]["intent"]
        self.assertEqual(intent["status"], "denied")
        self.assertEqual(intent["reason_code"], "authorization_denied")
        self.assertEqual(intent["secret_evidence"]["status"], "pass")

    async def test_evaluate_denies_disallowed_secret_destination(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(root / "launchplane.sqlite3")
            )
            store.ensure_schema()
            _seed_agent_write_intent_secret_binding(store, binding_instance="testing")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_agent_write_intent_policy(
                    actions=("product_config.apply", "product_config.apply.secret"),
                    product="sellyouroutboard",
                    context="sellyouroutboard",
                ),
                record_store_factory=lambda: store,
            )

            response = await _post_agent_write_intent_evaluate(
                app,
                _agent_write_intent_payload(
                    intent="product_config_apply",
                    mode="dry_run",
                    product="sellyouroutboard",
                    context="sellyouroutboard",
                    secret_bindings=["SMTP_PASSWORD"],
                    destination={
                        "kind": "runtime_environment",
                        "context": "sellyouroutboard",
                        "instance": "testing",
                    },
                ),
            )

        self.assertEqual(response.status_code, 202)
        intent = response.json()["result"]["intent"]
        self.assertEqual(intent["status"], "denied")
        self.assertEqual(intent["reason_code"], "secret_evidence_denied")
        self.assertEqual(intent["secret_evidence"]["status"], "fail")
        self.assertEqual(
            intent["secret_evidence"]["findings"][0]["code"],
            "secret_class_not_allowed",
        )

    async def test_evaluate_requires_agent_write_intent_record_storage(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_agent_write_intent_policy(
                actions=("every_code_work_request.rerun",),
                product="launchplane",
                context="launchplane",
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_agent_write_intent_evaluate(
            app,
            _agent_write_intent_payload(
                intent="every_code_rerun",
                mode="dry_run",
                product="launchplane",
                context="launchplane",
            ),
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "database_storage_required")

    async def test_openapi_includes_agent_write_intent_evaluate_contract(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_agent_write_intent_policy(
                actions=("every_code_work_request.rerun",),
                product="launchplane",
                context="launchplane",
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        route = openapi["paths"]["/v1/agent/write-intents/evaluate"]["post"]
        self.assertEqual(route["operationId"], "evaluate_agent_write_intent")
        self.assertEqual(
            route["requestBody"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/AgentWriteIntentRequest",
        )
        self.assertEqual(
            route["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/AcceptedEvidenceResponse",
        )
        for status_code in ("400", "401", "409", "503"):
            self.assertIn("LaunchplaneErrorResponse", json.dumps(route["responses"][status_code]))

    async def test_fastapi_evaluate_precedes_legacy_wsgi_fallback(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_agent_write_intent_policy(
                    actions=("every_code_work_request.rerun",),
                    product="launchplane",
                    context="launchplane",
                ),
                record_store_factory=lambda: store,
            )
            legacy_app = create_launchplane_service_app(
                state_dir=root / "legacy-state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=root,
            )
            app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

            response = await _post_agent_write_intent_evaluate(
                app,
                _agent_write_intent_payload(
                    intent="every_code_rerun",
                    mode="dry_run",
                    product="launchplane",
                    context="launchplane",
                ),
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["result"]["intent"]["status"], "allowed")


class FastApiEveryCodeReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_code_work_request_create_queues_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_every_code_work_request_write_policy(),
                record_store_factory=lambda: store,
            )

            response = await _post_every_code_work_request_create(
                app,
                _every_code_work_request_create_payload(),
                idempotency_key="every-code-create-code-123",
            )
            stored_requests = store.list_every_code_work_request_records(state="queued")

        payload = response.json()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(payload["records"]["state"], "queued")
        self.assertEqual(payload["result"]["request"]["state"], "queued")
        self.assertEqual(payload["records"]["request_id"], stored_requests[0].request_id)
        self.assertEqual(stored_requests[0].repository, "cbusillo/code")

    async def test_every_code_work_request_create_replays_idempotency(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_every_code_work_request_write_policy(),
                record_store_factory=lambda: store,
            )
            payload = _every_code_work_request_create_payload()

            first_response = await _post_every_code_work_request_create(
                app,
                payload,
                idempotency_key="every-code-create-code-123",
            )
            second_response = await _post_every_code_work_request_create(
                app,
                payload,
                idempotency_key="every-code-create-code-123",
            )
            stored_requests = store.list_every_code_work_request_records(state="queued")

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        self.assertTrue(second_response.json()["replayed"])
        self.assertEqual(
            second_response.json()["original_trace_id"],
            first_response.json()["trace_id"],
        )
        self.assertEqual(len(stored_requests), 1)

    async def test_every_code_work_request_create_rejects_reused_idempotency_key(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_every_code_work_request_write_policy(),
                record_store_factory=lambda: store,
            )

            first_response = await _post_every_code_work_request_create(
                app,
                _every_code_work_request_create_payload(),
                idempotency_key="every-code-create-code-123",
            )
            conflict_response = await _post_every_code_work_request_create(
                app,
                _every_code_work_request_create_payload(issue_number=124),
                idempotency_key="every-code-create-code-123",
            )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(conflict_response.status_code, 409)
        self.assertEqual(conflict_response.json()["error"]["code"], "idempotency_key_reused")

    async def test_every_code_work_request_create_rejects_unauthorized_identity(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
            )

            response = await _post_every_code_work_request_create(
                app,
                _every_code_work_request_create_payload(),
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_every_code_work_request_create_rejects_invalid_record_payload(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_every_code_work_request_write_policy(),
                record_store_factory=lambda: store,
            )

            response = await _post_every_code_work_request_create(
                app,
                {
                    **_every_code_work_request_create_payload(),
                    "repository": "cbusillo-code",
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")

    async def test_every_code_work_request_create_rejects_worker_token(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_every_code_work_request_write_policy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_work_request_create(
                app,
                _every_code_work_request_create_payload(),
                authorization="Bearer worker-token",
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "authentication_required")

    async def test_every_code_work_request_create_requires_store_capability(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_every_code_work_request_write_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_every_code_work_request_create(
            app,
            _every_code_work_request_create_payload(),
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "database_storage_required")

    async def test_every_code_work_request_claim_accepts_worker_token(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            seeded = _seed_every_code_claim_request(store)
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_work_request_claim(
                app,
                {"request_id": seeded.request_id, "host": "Chris-Studio"},
                authorization="Bearer worker-token",
                idempotency_key="every-code-worker-claim",
            )
            stored_request = store.read_every_code_work_request_record(seeded.request_id)

        payload = response.json()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(payload["records"]["request_id"], seeded.request_id)
        self.assertEqual(payload["records"]["state"], "claimed")
        self.assertEqual(payload["result"]["request"]["state"], "claimed")
        self.assertEqual(payload["result"]["request"]["claimed_by_host"], "Chris-Studio")
        self.assertEqual(stored_request.state, "claimed")
        self.assertEqual(stored_request.claimed_by_host, "Chris-Studio")

    async def test_every_code_work_request_claim_accepts_authorized_identity(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            seeded = _seed_every_code_claim_request(store)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_every_code_work_request_claim_policy(),
                record_store_factory=lambda: store,
            )

            response = await _post_every_code_work_request_claim(
                app,
                {"request_id": seeded.request_id, "host": "Runner-Host"},
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["result"]["request"]["claimed_by_host"], "Runner-Host")

    async def test_every_code_work_request_claim_replays_authorized_idempotency(
        self,
    ) -> None:
        payload: dict[str, object] = {
            "request_id": "every-code-cbusillo-code-123-test",
            "host": "Runner-Host",
        }
        store = _EveryCodeClaimReplayOnlyStore(
            payload=payload,
            idempotency_key="every-code-claim-replay",
        )
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_every_code_work_request_claim_policy(),
            record_store_factory=lambda: store,
        )

        response = await _post_every_code_work_request_claim(
            app,
            payload,
            idempotency_key="every-code-claim-replay",
        )

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json()["replayed"])
        self.assertEqual(response.json()["original_trace_id"], "launchplane_req_original")
        self.assertEqual(store.read_idempotency_calls, 1)
        self.assertEqual(store.claim_calls, 0)

    async def test_every_code_work_request_claim_rejects_missing_worker_token(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            seeded = _seed_every_code_claim_request(store)
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_work_request_claim(
                app,
                {"request_id": seeded.request_id, "host": "Chris-Studio"},
                authorization="",
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "authentication_required")

    async def test_every_code_work_request_claim_rejects_unauthorized_identity(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            seeded = _seed_every_code_claim_request(store)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
            )

            response = await _post_every_code_work_request_claim(
                app,
                {"request_id": seeded.request_id, "host": "Chris-Studio"},
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_every_code_work_request_claim_rejects_invalid_payload(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_work_request_claim(
                app,
                {"request_id": "", "host": "Chris-Studio"},
                authorization="Bearer worker-token",
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_payload")

    async def test_every_code_work_request_claim_returns_not_found(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_work_request_claim(
                app,
                {"request_id": "every-code-cbusillo-code-123-test", "host": "Chris-Studio"},
                authorization="Bearer worker-token",
            )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")

    async def test_every_code_work_request_claim_rejects_already_claimed(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            seeded = _seed_every_code_claim_request(store)
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            first_response = await _post_every_code_work_request_claim(
                app,
                {"request_id": seeded.request_id, "host": "Chris-Studio"},
                authorization="Bearer worker-token",
            )
            second_response = await _post_every_code_work_request_claim(
                app,
                {"request_id": seeded.request_id, "host": "Other-Host"},
                authorization="Bearer worker-token",
            )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 409)
        self.assertEqual(second_response.json()["error"]["code"], "work_request_already_claimed")

    async def test_every_code_work_request_claim_requires_store_capability(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
        )

        response = await _post_every_code_work_request_claim(
            app,
            {"request_id": "every-code-cbusillo-code-123-test", "host": "Chris-Studio"},
            authorization="Bearer worker-token",
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "database_storage_required")

    async def test_every_code_work_request_status_accepts_worker_token(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            seeded = _seed_every_code_claim_request(store)
            claimed = store.claim_every_code_work_request_record(
                request_id=seeded.request_id,
                host="Chris-Studio",
                claimed_at="2026-05-05T22:01:00Z",
            )
            if claimed is None:
                raise AssertionError("expected seeded request to be claimable")
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_work_request_status(
                app,
                {
                    "request_id": seeded.request_id,
                    "host": "Chris-Studio",
                    "state": "running",
                    "updated_at": "2026-05-05T22:02:00Z",
                },
                authorization="Bearer worker-token",
            )
            stored_request = store.read_every_code_work_request_record(seeded.request_id)

        payload = response.json()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(payload["records"]["request_id"], seeded.request_id)
        self.assertEqual(payload["records"]["state"], "running")
        self.assertEqual(payload["result"]["request"]["state"], "running")
        self.assertEqual(payload["result"]["request"]["started_at"], "2026-05-05T22:02:00Z")
        self.assertEqual(stored_request.state, "running")

    async def test_every_code_work_request_status_accepts_authorized_identity(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            seeded = _seed_every_code_claim_request(store)
            claimed = store.claim_every_code_work_request_record(
                request_id=seeded.request_id,
                host="Runner-Host",
                claimed_at="2026-05-05T22:01:00Z",
            )
            if claimed is None:
                raise AssertionError("expected seeded request to be claimable")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_every_code_work_request_status_policy(),
                record_store_factory=lambda: store,
            )

            response = await _post_every_code_work_request_status(
                app,
                {
                    "request_id": seeded.request_id,
                    "host": "Runner-Host",
                    "state": "done",
                    "result_pr_url": "https://github.com/cbusillo/code/pull/26",
                    "result_summary": "Opened a PR with the requested fix.",
                },
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["records"]["state"], "done")
        self.assertEqual(
            response.json()["result"]["request"]["result_pr_url"],
            "https://github.com/cbusillo/code/pull/26",
        )

    async def test_every_code_work_request_status_replays_authorized_idempotency(self) -> None:
        payload: dict[str, object] = {
            "request_id": "every-code-cbusillo-code-123-test",
            "host": "Runner-Host",
            "state": "done",
            "result_pr_url": "https://github.com/cbusillo/code/pull/26",
        }
        store = _EveryCodeStatusReplayOnlyStore(
            payload=payload,
            idempotency_key="every-code-status-replay",
        )
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_every_code_work_request_status_policy(),
            record_store_factory=lambda: store,
        )

        response = await _post_every_code_work_request_status(
            app,
            payload,
            idempotency_key="every-code-status-replay",
        )

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json()["replayed"])
        self.assertEqual(response.json()["original_trace_id"], "launchplane_req_original")
        self.assertEqual(store.read_idempotency_calls, 1)
        self.assertEqual(store.read_calls, 0)
        self.assertEqual(store.write_calls, 0)

    async def test_every_code_work_request_status_rejects_missing_worker_token(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            seeded = _seed_every_code_claim_request(store)
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_work_request_status(
                app,
                {"request_id": seeded.request_id, "host": "Chris-Studio", "state": "running"},
                authorization="",
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "authentication_required")

    async def test_every_code_work_request_status_rejects_unauthorized_identity(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            seeded = _seed_every_code_claim_request(store)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
            )

            response = await _post_every_code_work_request_status(
                app,
                {"request_id": seeded.request_id, "host": "Chris-Studio", "state": "running"},
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_every_code_work_request_status_rejects_invalid_payload(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            seeded = _seed_every_code_claim_request(store)
            claimed = store.claim_every_code_work_request_record(
                request_id=seeded.request_id,
                host="Chris-Studio",
                claimed_at="2026-05-05T22:01:00Z",
            )
            if claimed is None:
                raise AssertionError("expected seeded request to be claimable")
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_work_request_status(
                app,
                {"request_id": seeded.request_id, "host": "Other-Host", "state": "running"},
                authorization="Bearer worker-token",
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_payload")

    async def test_every_code_work_request_status_returns_not_found(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_work_request_status(
                app,
                {
                    "request_id": "every-code-cbusillo-code-123-test",
                    "host": "Chris-Studio",
                    "state": "running",
                },
                authorization="Bearer worker-token",
            )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")

    async def test_every_code_work_request_status_requires_store_capability(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
        )

        response = await _post_every_code_work_request_status(
            app,
            {
                "request_id": "every-code-cbusillo-code-123-test",
                "host": "Chris-Studio",
                "state": "running",
            },
            authorization="Bearer worker-token",
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "database_storage_required")

    async def test_every_code_work_request_status_sends_blocked_notifications(self) -> None:
        sent_payloads: list[tuple[str, dict[str, object]]] = []

        def send_discord(webhook_url: str, payload: dict[str, object]) -> None:
            sent_payloads.append((webhook_url, payload))

        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                "os.environ",
                {
                    control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: (
                        "test-master-key"
                    ),
                },
                clear=True,
            ),
        ):
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            try:
                store.ensure_schema()
                seeded = _seed_every_code_claim_request(store)
                claimed = store.claim_every_code_work_request_record(
                    request_id=seeded.request_id,
                    host="Chris-Studio",
                    claimed_at="2026-05-05T22:01:00Z",
                )
                if claimed is None:
                    raise AssertionError("expected seeded request to be claimable")
                secret_result = control_plane_secrets.write_secret_value(
                    record_store=store,
                    scope="context_instance",
                    integration="every-code-notifications",
                    name="discord webhook",
                    plaintext_value="https://discord.com/api/webhooks/test/webhook",
                    binding_key="DISCORD_WEBHOOK",
                    context_name="launchplane",
                    instance_name="every-code",
                    actor="test",
                    source_label="test",
                )
                store.write_every_code_notification_policy_record(
                    EveryCodeNotificationPolicyRecord(
                        policy_id="every-code-notification-discord",
                        repository="cbusillo/code",
                        status="enabled",
                        created_at="2026-06-14T18:10:00Z",
                        updated_at="2026-06-14T18:10:00Z",
                        source="test",
                        destinations=(
                            EveryCodeNotificationDestination(
                                destination_id="discord",
                                kind="discord",
                                discord_webhook_secret=str(secret_result["secret_id"]),
                            ),
                        ),
                    )
                )
                app = create_launchplane_fastapi_app(
                    verifier=_RejectingVerifier(),
                    authz_policy=LaunchplaneAuthzPolicy(),
                    record_store_factory=lambda: store,
                    bearer_identity_config=BearerIdentityConfig(
                        every_code_worker_token="worker-token"
                    ),
                    every_code_discord_sender=send_discord,
                )

                response = await _post_every_code_work_request_status(
                    app,
                    {
                        "request_id": seeded.request_id,
                        "host": "Chris-Studio",
                        "state": "blocked",
                        "error_message": "Every Code bot auth actor mismatch.",
                    },
                    authorization="Bearer worker-token",
                )
                attempts = store.list_every_code_notification_attempt_records(
                    request_id=seeded.request_id,
                    event="work_request_blocked",
                )
            finally:
                store.close()

        payload = response.json()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(payload["records"]["state"], "blocked")
        self.assertEqual(len(sent_payloads), 1)
        webhook_url, discord_payload = sent_payloads[0]
        self.assertEqual(webhook_url, "https://discord.com/api/webhooks/test/webhook")
        self.assertIn("embeds", discord_payload)
        self.assertEqual(payload["result"]["notifications"][0]["delivery_status"], "delivered")
        self.assertEqual(attempts[0].delivery_status, "delivered")

    async def test_every_code_work_request_rerun_accepts_worker_token(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            seeded = _seed_every_code_claim_request(store)
            claimed = store.claim_every_code_work_request_record(
                request_id=seeded.request_id,
                host="Chris-Studio",
                claimed_at="2026-05-05T22:01:00Z",
            )
            if claimed is None:
                raise AssertionError("expected seeded request to be claimable")
            blocked = apply_every_code_work_request_status(
                claimed,
                EveryCodeWorkRequestStatusUpdate(
                    state="blocked",
                    host="Chris-Studio",
                    updated_at="2026-05-05T22:05:00Z",
                    result_pr_url="https://github.com/cbusillo/code/pull/26",
                    result_summary="Detached session went stale.",
                    error_message="Detached session went stale.",
                ),
            )
            store.write_every_code_work_request_record(blocked)
            intent = _seed_every_code_rerun_intent(
                store,
                idempotency_key="every-code-rerun-intent:123",
            )
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_work_request_rerun(
                app,
                {
                    "request_id": seeded.request_id,
                    "trigger_actor": "cbusillo",
                    "source_url": "https://github.com/cbusillo/code/issues/123",
                    "agent_write_intent_record_id": intent.record_id,
                },
                authorization="Bearer worker-token",
                idempotency_key="every-code-rerun-intent:123",
            )
            stored_request = store.read_every_code_work_request_record(seeded.request_id)

        payload = response.json()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(payload["records"]["request_id"], seeded.request_id)
        self.assertEqual(payload["records"]["state"], "queued")
        self.assertEqual(payload["records"]["agent_write_intent_record_id"], intent.record_id)
        self.assertEqual(payload["result"]["request"]["trigger_actor"], "cbusillo")
        self.assertEqual(stored_request.state, "queued")
        self.assertEqual(stored_request.claimed_by_host, "")
        self.assertEqual(stored_request.result_pr_url, "")
        self.assertEqual(stored_request.error_message, "")

    async def test_every_code_work_request_rerun_accepts_authorized_identity(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            seeded = _seed_every_code_claim_request(store)
            claimed = store.claim_every_code_work_request_record(
                request_id=seeded.request_id,
                host="Runner-Host",
                claimed_at="2026-05-05T22:01:00Z",
            )
            if claimed is None:
                raise AssertionError("expected seeded request to be claimable")
            done = apply_every_code_work_request_status(
                claimed,
                EveryCodeWorkRequestStatusUpdate(
                    state="done",
                    host="Runner-Host",
                    updated_at="2026-05-05T22:05:00Z",
                    result_pr_url="https://github.com/cbusillo/code/pull/26",
                ),
            )
            store.write_every_code_work_request_record(done)
            intent = _seed_every_code_rerun_intent(store)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_every_code_work_request_rerun_policy(),
                record_store_factory=lambda: store,
            )

            response = await _post_every_code_work_request_rerun(
                app,
                {
                    "request_id": seeded.request_id,
                    "trigger_actor": "ops",
                    "agent_write_intent_record_id": intent.record_id,
                },
                idempotency_key="every-code-rerun-code-123",
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["records"]["state"], "queued")
        self.assertEqual(response.json()["result"]["request"]["trigger_actor"], "ops")

    async def test_every_code_work_request_rerun_uses_matching_intent_evidence(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            seeded = _seed_every_code_claim_request(store)
            claimed = store.claim_every_code_work_request_record(
                request_id=seeded.request_id,
                host="Chris-Studio",
                claimed_at="2026-05-05T22:01:00Z",
            )
            if claimed is None:
                raise AssertionError("expected seeded request to be claimable")
            blocked = apply_every_code_work_request_status(
                claimed,
                EveryCodeWorkRequestStatusUpdate(
                    state="blocked",
                    host="Chris-Studio",
                    updated_at="2026-05-05T22:05:00Z",
                    error_message="Needs another pass.",
                ),
            )
            store.write_every_code_work_request_record(blocked)
            intent = _seed_every_code_rerun_intent(store)
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_work_request_rerun(
                app,
                {"request_id": seeded.request_id, "trigger_actor": "ops"},
                authorization="Bearer worker-token",
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            response.json()["records"]["agent_write_intent_record_id"],
            intent.record_id,
        )
        self.assertEqual(response.json()["records"]["state"], "queued")

    async def test_every_code_work_request_rerun_replays_authorized_idempotency(
        self,
    ) -> None:
        payload: dict[str, object] = {
            "request_id": "every-code-cbusillo-code-123-test",
            "agent_write_intent_record_id": "agent-write-intent-test",
        }
        store = _EveryCodeRerunReplayOnlyStore(
            payload=payload,
            idempotency_key="every-code-rerun-replay",
        )
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_every_code_work_request_rerun_policy(),
            record_store_factory=lambda: store,
        )

        response = await _post_every_code_work_request_rerun(
            app,
            payload,
            idempotency_key="every-code-rerun-replay",
        )

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json()["replayed"])
        self.assertEqual(response.json()["original_trace_id"], "launchplane_req_original")
        self.assertEqual(store.read_idempotency_calls, 1)
        self.assertEqual(store.read_calls, 0)
        self.assertEqual(store.write_calls, 0)

    async def test_every_code_work_request_rerun_rejects_invalid_inputs(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            seeded = _seed_every_code_claim_request(store)
            claimed = store.claim_every_code_work_request_record(
                request_id=seeded.request_id,
                host="Chris-Studio",
                claimed_at="2026-05-05T22:01:00Z",
            )
            if claimed is None:
                raise AssertionError("expected seeded request to be claimable")
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            active_response = await _post_every_code_work_request_rerun(
                app,
                {"request_id": seeded.request_id},
                authorization="Bearer worker-token",
            )
            invalid_response = await _post_every_code_work_request_rerun(
                app,
                {"request_id": ""},
                authorization="Bearer worker-token",
            )

        self.assertEqual(active_response.status_code, 409)
        self.assertEqual(active_response.json()["error"]["code"], "agent_write_intent_required")
        self.assertEqual(invalid_response.status_code, 400)
        self.assertEqual(invalid_response.json()["error"]["code"], "invalid_payload")

    async def test_every_code_work_request_rerun_validation_error_omits_records(
        self,
    ) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
        )

        response = await _asgi_request(
            app,
            "POST",
            "/v1/every-code/work-requests/rerun",
            headers={
                "Authorization": "Bearer worker-token",
                "Content-Type": "application/json",
            },
            raw_body=b'{"request_id":',
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")
        self.assertNotIn("records", response.json())

    async def test_every_code_work_request_rerun_rejects_non_terminal_request(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            seeded = _seed_every_code_claim_request(store)
            intent = _seed_every_code_rerun_intent(store)
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_work_request_rerun(
                app,
                {
                    "request_id": seeded.request_id,
                    "agent_write_intent_record_id": intent.record_id,
                },
                authorization="Bearer worker-token",
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_payload")

    async def test_every_code_work_request_rerun_requires_write_intent_evidence(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            seeded = _seed_every_code_claim_request(store)
            claimed = store.claim_every_code_work_request_record(
                request_id=seeded.request_id,
                host="Chris-Studio",
                claimed_at="2026-05-05T22:01:00Z",
            )
            if claimed is None:
                raise AssertionError("expected seeded request to be claimable")
            blocked = apply_every_code_work_request_status(
                claimed,
                EveryCodeWorkRequestStatusUpdate(
                    state="blocked",
                    host="Chris-Studio",
                    updated_at="2026-05-05T22:05:00Z",
                    error_message="Needs another pass.",
                ),
            )
            store.write_every_code_work_request_record(blocked)
            wrong_source_intent = _seed_every_code_rerun_intent(
                store,
                source_url="https://github.com/cbusillo/code/issues/999",
            )
            wrong_context_intent = _seed_every_code_rerun_intent(
                store,
                context="other-context",
            )
            stale_intent = _seed_every_code_rerun_intent(
                store,
                recorded_at="2026-01-01T00:00:00Z",
            )
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            missing_response = await _post_every_code_work_request_rerun(
                app,
                {"request_id": seeded.request_id},
                authorization="Bearer worker-token",
            )
            mismatch_response = await _post_every_code_work_request_rerun(
                app,
                {
                    "request_id": seeded.request_id,
                    "source_url": "https://github.com/cbusillo/code/issues/123",
                    "agent_write_intent_record_id": wrong_source_intent.record_id,
                },
                authorization="Bearer worker-token",
            )
            mismatch_without_source_response = await _post_every_code_work_request_rerun(
                app,
                {
                    "request_id": seeded.request_id,
                    "agent_write_intent_record_id": wrong_source_intent.record_id,
                },
                authorization="Bearer worker-token",
            )
            wrong_context_response = await _post_every_code_work_request_rerun(
                app,
                {
                    "request_id": seeded.request_id,
                    "agent_write_intent_record_id": wrong_context_intent.record_id,
                },
                authorization="Bearer worker-token",
            )
            stale_response = await _post_every_code_work_request_rerun(
                app,
                {
                    "request_id": seeded.request_id,
                    "agent_write_intent_record_id": stale_intent.record_id,
                },
                authorization="Bearer worker-token",
            )
            not_found_response = await _post_every_code_work_request_rerun(
                app,
                {
                    "request_id": seeded.request_id,
                    "agent_write_intent_record_id": "agent-write-intent-missing",
                },
                authorization="Bearer worker-token",
            )

        self.assertEqual(missing_response.status_code, 409)
        self.assertEqual(missing_response.json()["error"]["code"], "agent_write_intent_required")
        self.assertEqual(mismatch_response.status_code, 409)
        self.assertEqual(
            mismatch_response.json()["error"]["code"],
            "agent_write_intent_source_mismatch",
        )
        self.assertEqual(
            mismatch_response.json()["records"]["agent_write_intent_record_id"],
            wrong_source_intent.record_id,
        )
        self.assertEqual(mismatch_without_source_response.status_code, 409)
        self.assertEqual(
            mismatch_without_source_response.json()["error"]["code"],
            "agent_write_intent_source_mismatch",
        )
        self.assertEqual(wrong_context_response.status_code, 409)
        self.assertEqual(
            wrong_context_response.json()["error"]["code"],
            "agent_write_intent_scope_mismatch",
        )
        self.assertEqual(stale_response.status_code, 409)
        self.assertEqual(stale_response.json()["error"]["code"], "agent_write_intent_stale")
        self.assertEqual(not_found_response.status_code, 404)
        self.assertEqual(
            not_found_response.json()["error"]["code"],
            "agent_write_intent_not_found",
        )

    async def test_every_code_work_request_rerun_returns_not_found_for_missing_request(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            intent = _seed_every_code_rerun_intent(store)
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_work_request_rerun(
                app,
                {
                    "request_id": "every-code-cbusillo-code-123-missing",
                    "agent_write_intent_record_id": intent.record_id,
                },
                authorization="Bearer worker-token",
            )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")

    async def test_every_code_work_request_rerun_requires_store_capability(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
        )

        response = await _post_every_code_work_request_rerun(
            app,
            {"request_id": "every-code-cbusillo-code-123-test"},
            authorization="Bearer worker-token",
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "database_storage_required")
        self.assertNotIn("records", response.json())

    async def test_every_code_pr_feedback_write_stores_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_pr_feedback(
                app,
                _every_code_pr_feedback_payload(),
                authorization="Bearer worker-token",
            )
            records = store.list_every_code_pr_feedback_records(
                request_id="every-code-cbusillo-code-123-test",
                status="pending",
            )

        payload = response.json()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(payload["records"]["feedback_id"], records[0].feedback_id)
        self.assertEqual(payload["result"]["feedback"]["feedback_id"], records[0].feedback_id)
        self.assertEqual(len(records), 1)

    async def test_every_code_pr_feedback_write_rejects_missing_worker_token(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_every_code_read_policy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_pr_feedback(
                app,
                _every_code_pr_feedback_payload(),
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "authentication_required")

    async def test_every_code_pr_feedback_write_rejects_invalid_payload(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_pr_feedback(
                app,
                {**_every_code_pr_feedback_payload(), "repository": "cbusillo-code"},
                authorization="Bearer worker-token",
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_payload")

    async def test_every_code_pr_feedback_write_requires_store_capability(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
        )

        response = await _post_every_code_pr_feedback(
            app,
            _every_code_pr_feedback_payload(),
            authorization="Bearer worker-token",
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "database_storage_required")

    async def test_every_code_pr_feedback_status_write_updates_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            feedback_record = EveryCodePrFeedbackRecord.model_validate(
                _every_code_pr_feedback_payload()
            )
            store.write_every_code_pr_feedback_record(feedback_record)
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_pr_feedback_status(
                app,
                {
                    "feedback_id": feedback_record.feedback_id,
                    "request_id": feedback_record.request_id,
                    "status": "applied",
                },
                authorization="Bearer worker-token",
            )
            applied_records = store.list_every_code_pr_feedback_records(
                request_id=feedback_record.request_id,
                status="applied",
            )

        payload = response.json()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(payload["records"]["feedback_id"], feedback_record.feedback_id)
        self.assertEqual(payload["records"]["status"], "applied")
        self.assertEqual(payload["result"]["feedback"]["status"], "applied")
        self.assertEqual(len(applied_records), 1)
        self.assertEqual(applied_records[0].feedback_id, feedback_record.feedback_id)

    async def test_every_code_pr_feedback_status_write_rejects_missing_worker_token(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            feedback_record = EveryCodePrFeedbackRecord.model_validate(
                _every_code_pr_feedback_payload()
            )
            store.write_every_code_pr_feedback_record(feedback_record)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_every_code_read_policy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_pr_feedback_status(
                app,
                {
                    "feedback_id": feedback_record.feedback_id,
                    "request_id": feedback_record.request_id,
                    "status": "applied",
                },
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "authentication_required")

    async def test_every_code_pr_feedback_status_write_rejects_invalid_payload(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_pr_feedback_status(
                app,
                {
                    "feedback_id": "",
                    "request_id": "every-code-cbusillo-code-123-test",
                    "status": "applied",
                },
                authorization="Bearer worker-token",
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_payload")

    async def test_every_code_pr_feedback_status_write_returns_not_found(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_pr_feedback_status(
                app,
                {
                    "feedback_id": "missing-feedback",
                    "request_id": "every-code-cbusillo-code-123-test",
                    "status": "applied",
                },
                authorization="Bearer worker-token",
            )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")

    async def test_every_code_pr_feedback_status_write_rejects_final_feedback(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            feedback_record = EveryCodePrFeedbackRecord.model_validate(
                {**_every_code_pr_feedback_payload(), "status": "applied"}
            )
            store.write_every_code_pr_feedback_record(feedback_record)
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_pr_feedback_status(
                app,
                {
                    "feedback_id": feedback_record.feedback_id,
                    "request_id": feedback_record.request_id,
                    "status": "ignored",
                },
                authorization="Bearer worker-token",
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "feedback_already_final")

    async def test_every_code_pr_feedback_status_write_requires_store_capability(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
        )

        response = await _post_every_code_pr_feedback_status(
            app,
            {
                "feedback_id": "feedback-1",
                "request_id": "every-code-cbusillo-code-123-test",
                "status": "applied",
            },
            authorization="Bearer worker-token",
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "database_storage_required")

    async def test_every_code_preview_gate_write_stores_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_preview_gate(
                app,
                _every_code_preview_gate_payload(),
                authorization="Bearer worker-token",
            )
            records = store.list_every_code_preview_gate_records(
                request_id="every-code-cbusillo-code-123-test",
                status="ready",
            )

        payload = response.json()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(payload["records"]["gate_id"], records[0].gate_id)
        self.assertEqual(payload["result"]["gate"]["gate_id"], records[0].gate_id)
        self.assertEqual(len(records), 1)

    async def test_every_code_preview_gate_write_rejects_missing_worker_token(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_every_code_read_policy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_preview_gate(
                app,
                _every_code_preview_gate_payload(),
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "authentication_required")

    async def test_every_code_preview_gate_write_rejects_invalid_payload(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_preview_gate(
                app,
                {**_every_code_preview_gate_payload(), "repository": "cbusillo-code"},
                authorization="Bearer worker-token",
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_payload")

    async def test_every_code_preview_gate_write_requires_store_capability(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
        )

        response = await _post_every_code_preview_gate(
            app,
            _every_code_preview_gate_payload(),
            authorization="Bearer worker-token",
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "database_storage_required")

    async def test_every_code_read_routes_return_native_payloads(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            seeded = _seed_every_code_read_records(store)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_every_code_read_policy(),
                record_store_factory=lambda: store,
            )

            summary_response = await _get_every_code_summary(
                app,
                repository="cbusillo/code",
                issue_number="123",
                state="queued",
                limit="1",
                offset="0",
            )
            readiness_response = await _get_preview_readiness(
                app,
                repository="cbusillo/code",
                pr_number="31",
                status="blocked",
            )
            list_response = await _get_every_code_work_requests(
                app,
                state="queued",
                repository="cbusillo/code",
            )
            read_response = await _get_every_code_work_request(
                app,
                seeded["request_id"],
            )
            feedback_response = await _get_every_code_pr_feedback(
                app,
                request_id=seeded["request_id"],
                repository="cbusillo/code",
                pr_number="31",
                status="pending",
            )
            gates_response = await _get_every_code_preview_gates(
                app,
                request_id=seeded["request_id"],
                repository="cbusillo/code",
                pr_number="31",
                status="blocked",
            )
            notification_response = await _get_every_code_notification_attempts(
                app,
                request_id=seeded["request_id"],
                event="work_request_blocked",
                destination_kind="discord",
            )
            preview_notification_response = await _get_preview_pr_feedback_notification_attempts(
                app,
                feedback_id=seeded["preview_feedback_id"],
                event="delivery_skipped",
                destination_kind="discord",
            )

        for response in (
            summary_response,
            readiness_response,
            list_response,
            read_response,
            feedback_response,
            gates_response,
            notification_response,
            preview_notification_response,
        ):
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "ok")
            self.assertIn("trace_id", response.json())

        summary = summary_response.json()["summary"]
        self.assertEqual(summary["repository"], "cbusillo/code")
        self.assertEqual(summary["issue_number"], 123)
        self.assertEqual(summary["state_filter"], "queued")
        self.assertEqual(summary["summaries"][0]["request_id"], seeded["request_id"])
        self.assertEqual(summary["summaries"][0]["summary_status"], "active")

        readiness = readiness_response.json()["readiness"]
        self.assertEqual(readiness["repository"], "cbusillo/code")
        self.assertEqual(readiness["pr_number"], 31)
        self.assertEqual(readiness["status_filter"], "blocked")
        self.assertEqual(readiness["items"][0]["gate_id"], seeded["gate_id"])
        self.assertEqual(readiness["items"][0]["readiness_status"], "needs_attention")

        self.assertEqual(list_response.json()["requests"][0]["request_id"], seeded["request_id"])
        self.assertEqual(read_response.json()["request"]["request_id"], seeded["request_id"])
        self.assertEqual(
            feedback_response.json()["feedback"][0]["feedback_id"], seeded["feedback_id"]
        )
        self.assertEqual(gates_response.json()["gates"][0]["gate_id"], seeded["gate_id"])
        self.assertEqual(
            notification_response.json()["attempts"][0]["attempt_id"],
            seeded["notification_attempt_id"],
        )
        self.assertEqual(
            preview_notification_response.json()["attempts"][0]["attempt_id"],
            seeded["preview_notification_attempt_id"],
        )

    async def test_every_code_worker_token_reads_without_policy(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            seeded = _seed_every_code_read_records(store)
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            responses = (
                await _get_every_code_summary(
                    app,
                    repository="cbusillo/code",
                    authorization="Bearer worker-token",
                ),
                await _get_preview_readiness(
                    app,
                    repository="cbusillo/code",
                    authorization="Bearer worker-token",
                ),
                await _get_every_code_work_request(
                    app,
                    seeded["request_id"],
                    authorization="Bearer worker-token",
                ),
                await _get_every_code_pr_feedback(
                    app,
                    request_id=seeded["request_id"],
                    authorization="Bearer worker-token",
                ),
            )

        for response in responses:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "ok")

    async def test_every_code_reads_reject_unauthorized_identity(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            _seed_every_code_read_records(store)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
            )

            responses = (
                await _get_every_code_work_requests(app),
                await _get_preview_readiness(app),
                await _get_every_code_notification_attempts(app),
                await _get_preview_pr_feedback_notification_attempts(app),
            )

        for response in responses:
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_every_code_reads_require_store_capability(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_every_code_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        responses = (
            await _get_every_code_work_requests(app),
            await _get_every_code_notification_attempts(app),
            await _get_preview_pr_feedback_notification_attempts(app),
        )

        for response in responses:
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["error"]["code"], "database_storage_required")

    async def test_every_code_reads_reject_invalid_query_values(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            _seed_every_code_read_records(store)
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            offset_response = await _get_every_code_work_requests(
                app,
                offset="-1",
                authorization="Bearer worker-token",
            )
            pr_number_response = await _get_every_code_pr_feedback(
                app,
                pr_number="not-a-number",
                authorization="Bearer worker-token",
            )
            status_response = await _get_preview_readiness(
                app,
                status="unknown",
                authorization="Bearer worker-token",
            )

        self.assertEqual(offset_response.status_code, 400)
        self.assertEqual(offset_response.json()["error"]["code"], "invalid_payload")
        self.assertIn("offset must be non-negative", offset_response.json()["error"]["message"])
        self.assertEqual(pr_number_response.status_code, 400)
        self.assertEqual(pr_number_response.json()["error"]["code"], "invalid_payload")
        self.assertEqual(status_response.status_code, 400)
        self.assertEqual(status_response.json()["error"]["code"], "invalid_payload")

    async def test_openapi_includes_every_code_read_contracts(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_every_code_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        expected_routes = {
            "/v1/every-code/summary": (
                "read_every_code_summary",
                "EveryCodeSummaryResponse",
            ),
            "/v1/previews/readiness": (
                "read_preview_readiness",
                "PreviewReadinessResponse",
            ),
            "/v1/every-code/work-requests": (
                "list_every_code_work_requests",
                "EveryCodeWorkRequestRecordsResponse",
            ),
            "/v1/every-code/work-requests/{request_id}": (
                "read_every_code_work_request",
                "EveryCodeWorkRequestRecordResponse",
            ),
            "/v1/every-code/pr-feedback": (
                "list_every_code_pr_feedback",
                "EveryCodePrFeedbackRecordsResponse",
            ),
            "/v1/every-code/preview-gates": (
                "list_every_code_preview_gates",
                "EveryCodePreviewGateRecordsResponse",
            ),
            "/v1/every-code/notification-attempts": (
                "list_every_code_notification_attempts",
                "EveryCodeNotificationAttemptRecordsResponse",
            ),
            "/v1/previews/pr-feedback/notification-attempts": (
                "list_preview_pr_feedback_notification_attempts",
                "PreviewPrFeedbackNotificationAttemptRecordsResponse",
            ),
        }
        for path, (operation_id, response_model_name) in expected_routes.items():
            route = openapi["paths"][path]["get"]
            self.assertEqual(route["operationId"], operation_id)
            self.assertEqual(
                route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
                f"#/components/schemas/{response_model_name}",
            )
            self.assertFalse(
                openapi["components"]["schemas"][response_model_name]["additionalProperties"]
            )
            for status_code in ("400", "401", "403", "404", "503"):
                self.assertIn(
                    "LaunchplaneErrorResponse", json.dumps(route["responses"][status_code])
                )
        create_route = openapi["paths"]["/v1/every-code/work-requests/create"]["post"]
        self.assertEqual(create_route["operationId"], "create_every_code_work_request")
        self.assertEqual(
            create_route["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/AcceptedEvidenceResponse",
        )
        for status_code in ("400", "401", "403", "409", "503"):
            self.assertIn(
                "LaunchplaneErrorResponse", json.dumps(create_route["responses"][status_code])
            )
        work_request_status_route = openapi["paths"]["/v1/every-code/work-requests/status"]["post"]
        self.assertEqual(
            work_request_status_route["operationId"],
            "write_every_code_work_request_status",
        )
        self.assertEqual(
            work_request_status_route["requestBody"]["content"]["application/json"]["schema"][
                "title"
            ],
            "EveryCodeWorkRequestStatusEnvelope",
        )
        self.assertFalse(
            work_request_status_route["requestBody"]["content"]["application/json"]["schema"][
                "additionalProperties"
            ]
        )
        self.assertEqual(
            set(
                work_request_status_route["requestBody"]["content"]["application/json"]["schema"][
                    "required"
                ]
            ),
            {"request_id", "host", "state"},
        )
        self.assertEqual(
            work_request_status_route["responses"]["202"]["content"]["application/json"]["schema"][
                "$ref"
            ],
            "#/components/schemas/AcceptedEvidenceResponse",
        )
        for status_code in ("400", "401", "403", "404", "409", "503"):
            self.assertIn(
                "LaunchplaneErrorResponse",
                json.dumps(work_request_status_route["responses"][status_code]),
            )
        worker_write_routes = {
            "/v1/every-code/pr-feedback": "write_every_code_pr_feedback",
            "/v1/every-code/preview-gates": "write_every_code_preview_gate",
        }
        for path, operation_id in worker_write_routes.items():
            route = openapi["paths"][path]["post"]
            self.assertEqual(route["operationId"], operation_id)
            self.assertEqual(
                route["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
                "#/components/schemas/AcceptedEvidenceResponse",
            )
            for status_code in ("400", "401", "403", "503"):
                self.assertIn(
                    "LaunchplaneErrorResponse", json.dumps(route["responses"][status_code])
                )
            self.assertNotIn("409", route["responses"])
        status_route = openapi["paths"]["/v1/every-code/pr-feedback/status"]["post"]
        self.assertEqual(status_route["operationId"], "write_every_code_pr_feedback_status")
        self.assertEqual(
            status_route["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/AcceptedEvidenceResponse",
        )
        for status_code in ("400", "401", "403", "404", "409", "503"):
            self.assertIn(
                "LaunchplaneErrorResponse", json.dumps(status_route["responses"][status_code])
            )

    async def test_fastapi_every_code_reads_precede_legacy_wsgi_fallback(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            seeded = _seed_every_code_read_records(store)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_every_code_read_policy(),
                record_store_factory=lambda: store,
            )
            legacy_app = create_launchplane_service_app(
                state_dir=root / "legacy-state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=root,
            )
            app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

            work_request_response = await _get_every_code_work_request(
                app,
                seeded["request_id"],
            )
            notification_response = await _get_preview_pr_feedback_notification_attempts(
                app,
                feedback_id=seeded["preview_feedback_id"],
            )

        self.assertEqual(work_request_response.status_code, 200)
        self.assertEqual(notification_response.status_code, 200)


class FastApiProductProfileTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_product_profiles_returns_profiles_for_driver(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            record_store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            record_store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            record_store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(
                    {**_product_profile_payload("verireel"), "driver_id": "other-driver"}
                )
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_profile_read_policy(product="launchplane"),
                record_store_factory=lambda: record_store,
            )

            response = await _get_product_profiles(app, driver_id="generic-web")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(set(payload), {"status", "trace_id", "driver_id", "profiles"})
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["trace_id"].startswith("launchplane_req_"))
        self.assertEqual(payload["driver_id"], "generic-web")
        self.assertEqual(
            [profile["product"] for profile in payload["profiles"]],
            ["sellyouroutboard"],
        )

    async def test_show_product_profile_returns_profile(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            record_store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            record_store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_profile_read_policy(product="sellyouroutboard"),
                record_store_factory=lambda: record_store,
            )

            response = await _get_product_profile(app)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(set(payload), {"status", "trace_id", "profile"})
        self.assertEqual(payload["profile"]["product"], "sellyouroutboard")
        self.assertEqual(payload["profile"]["driver_id"], "generic-web")
        self.assertEqual(payload["profile"]["preview"]["slug_template"], "pr-{number}")

    async def test_write_product_profile_persists_authorized_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            record_store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_profile_write_policy(product="sellyouroutboard"),
                record_store_factory=lambda: record_store,
            )

            response = await _post_product_profile(
                app,
                _product_profile_payload(),
                idempotency_key="profile-sellyouroutboard",
            )
            stored_profile = record_store.read_product_profile_record("sellyouroutboard")

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["records"], {"product_profile": "sellyouroutboard"})
        self.assertNotIn("result", payload)
        self.assertEqual(stored_profile.driver_id, "generic-web")
        self.assertEqual(stored_profile.preview.slug_template, "pr-{number}")

    async def test_write_product_profile_replays_idempotent_request(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            record_store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_profile_write_policy(product="sellyouroutboard"),
                record_store_factory=lambda: record_store,
            )
            payload = _product_profile_payload()

            first_response = await _post_product_profile(
                app,
                payload,
                idempotency_key="profile-sellyouroutboard",
            )
            second_response = await _post_product_profile(
                app,
                payload,
                idempotency_key="profile-sellyouroutboard",
            )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        first_payload = first_response.json()
        second_payload = second_response.json()
        self.assertEqual(second_payload["records"], first_payload["records"])
        self.assertTrue(second_payload["replayed"])
        self.assertEqual(second_payload["original_trace_id"], first_payload["trace_id"])

    async def test_write_product_profile_replays_before_write_store_check(self) -> None:
        payload = _product_profile_payload()
        record_store = _ProductProfileReplayOnlyStore(
            payload=payload,
            idempotency_key="profile-sellyouroutboard",
        )
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_profile_write_policy(product="sellyouroutboard"),
            record_store_factory=lambda: record_store,
        )

        response = await _post_product_profile(
            app,
            payload,
            idempotency_key="profile-sellyouroutboard",
        )

        self.assertEqual(response.status_code, 202)
        response_payload = response.json()
        self.assertTrue(response_payload["replayed"])
        self.assertEqual(response_payload["records"], {"product_profile": "sellyouroutboard"})
        self.assertEqual(response_payload["original_trace_id"], "launchplane_req_original")
        self.assertEqual(record_store.read_idempotency_calls, 1)

    async def test_write_product_profile_rejects_reused_idempotency_key(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            record_store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_profile_write_policy(product="sellyouroutboard"),
                record_store_factory=lambda: record_store,
            )
            changed_payload = {
                **_product_profile_payload(),
                "display_name": "Changed Product Name",
            }

            await _post_product_profile(
                app,
                _product_profile_payload(),
                idempotency_key="profile-sellyouroutboard",
            )
            response = await _post_product_profile(
                app,
                changed_payload,
                idempotency_key="profile-sellyouroutboard",
            )

        self.assertEqual(response.status_code, 409)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "idempotency_key_reused")

    async def test_write_product_profile_authenticates_before_malformed_body(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_profile_write_policy(product="sellyouroutboard"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_request(
            app,
            "POST",
            "/v1/product-profiles",
            raw_body=b"{",
            headers={"Content-Type": "application/json"},
        )

        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authentication_required")

    async def test_write_product_profile_reports_schema_validation_message(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_profile_write_policy(product="sellyouroutboard"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_product_profile(app, {"schema_version": 1})

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertEqual(payload["error"]["message"], "Request payload failed validation.")

    async def test_write_product_profile_rejects_inert_health_monitoring(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            record_store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_profile_write_policy(product="sellyouroutboard"),
                record_store_factory=lambda: record_store,
            )
            profile_payload = _product_profile_payload()
            profile_payload["lanes"] = (
                {
                    "instance": "testing",
                    "context": "sellyouroutboard-testing",
                    "health_monitoring": {
                        "checks": [{"name": "public-ingress", "kind": "public_http"}]
                    },
                },
            )

            response = await _post_product_profile(
                app,
                profile_payload,
                idempotency_key="profile-sellyouroutboard",
            )

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertEqual(
            payload["error"]["message"],
            "public HTTP health check requires base_url or explicit health_url",
        )

    async def test_write_product_profile_rejects_unauthorized_product(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_profile_write_policy(product="verireel"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_product_profile(app, _product_profile_payload())

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    async def test_write_product_profile_requires_matching_store_method(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_profile_write_policy(product="sellyouroutboard"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_product_profile(app, _product_profile_payload())

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "database_storage_required")
        self.assertIn("write_product_profile_record", payload["error"]["message"])

    async def test_list_product_profiles_accepts_every_code_worker_token(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            record_store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            record_store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({}),
                record_store_factory=lambda: record_store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            list_response = await _get_product_profiles(
                app,
                driver_id="generic-web",
                authorization="Bearer worker-token",
            )
            show_response = await _get_product_profile(
                app,
                authorization="Bearer worker-token",
            )

        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(list_response.json()["profiles"][0]["product"], "sellyouroutboard")
        self.assertEqual(show_response.status_code, 401)
        self.assertEqual(show_response.json()["error"]["code"], "authentication_required")

    async def test_product_profile_reads_require_identity(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_profile_read_policy(product="launchplane"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        list_response = await _get_product_profiles(app, authorization="")
        show_response = await _get_product_profile(app, authorization="")

        self.assertEqual(list_response.status_code, 401)
        self.assertEqual(show_response.status_code, 401)
        self.assertEqual(list_response.json()["error"]["code"], "authentication_required")
        self.assertEqual(show_response.json()["error"]["code"], "authentication_required")

    async def test_list_product_profiles_rejects_unauthorized_caller(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_profile_read_policy(product="sellyouroutboard"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _get_product_profiles(app)

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")
        self.assertNotIn("authz", payload)

    async def test_show_product_profile_rejects_unauthorized_caller(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            record_store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            record_store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_profile_read_policy(product="verireel"),
                record_store_factory=lambda: record_store,
            )

            response = await _get_product_profile(app)

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")
        self.assertNotIn("authz", payload)

    async def test_local_operator_token_cannot_read_product_profiles_without_grant(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            record_store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            record_store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({}),
                record_store_factory=lambda: record_store,
                bearer_identity_config=_local_operator_bearer_config(),
            )

            list_response = await _get_product_profiles(
                app,
                authorization="Bearer local-operator-token",
            )
            show_response = await _get_product_profile(
                app,
                authorization="Bearer local-operator-token",
            )

        self.assertEqual(list_response.status_code, 403)
        self.assertEqual(show_response.status_code, 403)
        self.assertEqual(list_response.json()["error"]["code"], "authorization_denied")
        self.assertEqual(show_response.json()["error"]["code"], "authorization_denied")
        self.assertNotIn("authz", list_response.json())
        self.assertNotIn("authz", show_response.json())

    async def test_show_product_profile_returns_404_for_unknown_product(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            record_store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_profile_read_policy(product="sellyouroutboard"),
                record_store_factory=lambda: record_store,
            )

            response = await _get_product_profile(app)

        self.assertEqual(response.status_code, 404)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "not_found")

    async def test_product_profile_reads_require_matching_store_methods(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_profile_read_policy(product="launchplane"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        list_response = await _get_product_profiles(app)
        show_response = await _get_product_profile(app)

        self.assertEqual(list_response.status_code, 503)
        self.assertEqual(show_response.status_code, 503)
        self.assertIn("list_product_profile_records", list_response.json()["error"]["message"])
        self.assertIn("read_product_profile_record", show_response.json()["error"]["message"])

    async def test_openapi_includes_product_profile_contracts(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_profile_read_policy(product="launchplane"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        list_route = openapi["paths"]["/v1/product-profiles"]["get"]
        write_route = openapi["paths"]["/v1/product-profiles"]["post"]
        show_route = openapi["paths"]["/v1/product-profiles/{product}"]["get"]
        self.assertEqual(list_route["operationId"], "list_product_profiles")
        self.assertEqual(write_route["operationId"], "write_product_profile")
        self.assertEqual(show_route["operationId"], "read_product_profile")
        self.assertEqual(
            list_route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/ProductProfileListResponse",
        )
        self.assertEqual(
            write_route["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/AcceptedEvidenceResponse",
        )
        self.assertEqual(
            write_route["requestBody"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/LaunchplaneProductProfileRecord",
        )
        self.assertEqual(
            show_route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/ProductProfileResponse",
        )
        for status_code in ("400", "401", "403", "409", "503"):
            self.assertIn(status_code, write_route["responses"])
        self.assertEqual(
            openapi["components"]["schemas"]["ProductProfileListResponse"]["additionalProperties"],
            False,
        )
        self.assertEqual(
            openapi["components"]["schemas"]["ProductProfileResponse"]["additionalProperties"],
            False,
        )

    async def test_fastapi_product_profile_reads_precede_legacy_wsgi_fallback(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            record_store = FilesystemRecordStore(state_dir=root / "state")
            record_store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["launchplane", "sellyouroutboard"],
                            "contexts": ["launchplane"],
                            "actions": ["product_profile.read"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                record_store_factory=lambda: record_store,
            )
            legacy_app = create_launchplane_service_app(
                state_dir=root / "legacy-state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({}),
                control_plane_root_path=root,
            )
            app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

            list_response = await _get_product_profiles(app, driver_id="generic-web")
            show_response = await _get_product_profile(app)

        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(show_response.status_code, 200)
        self.assertEqual(list_response.json()["profiles"][0]["product"], "sellyouroutboard")
        self.assertEqual(show_response.json()["profile"]["product"], "sellyouroutboard")

    async def test_fastapi_product_profile_write_precedes_legacy_wsgi_fallback(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            record_store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_profile_write_policy(product="sellyouroutboard"),
                record_store_factory=lambda: record_store,
            )
            legacy_app = create_launchplane_service_app(
                state_dir=root / "legacy-state",
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({}),
                control_plane_root_path=root,
            )
            app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

            response = await _post_product_profile(app, _product_profile_payload())
            stored_profile = record_store.read_product_profile_record("sellyouroutboard")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["records"]["product_profile"], "sellyouroutboard")
        self.assertEqual(stored_profile.driver_id, "generic-web")


class FastApiProductConfigApplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_product_config_dry_run_returns_redacted_plan_without_writes(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _write_runtime_key_safety_policy(database_url=database_url)
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_config_policy(action="product_config.plan"),
                record_store_factory=lambda: app_store,
            )

            with patch.dict(
                os.environ,
                {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
                clear=True,
            ):
                response = await _post_product_config_apply(
                    app,
                    _product_config_payload(),
                    idempotency_key="product-config-dry-run",
                )
            runtime_records = app_store.list_runtime_environment_records()
            secret_records = app_store.list_secret_records()
            app_store.close()

        payload = response.json()
        response_text = json.dumps(payload, sort_keys=True)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(payload["result"]["mode"], "dry-run")
        self.assertEqual(payload["result"]["runtime_environment"]["action"], "created")
        self.assertEqual(payload["result"]["secrets"][0]["action"], "created")
        self.assertNotIn("smtp-secret-value", response_text)
        self.assertNotIn("https://www.sellyouroutboard.com", response_text)
        self.assertEqual(runtime_records, ())
        self.assertEqual(secret_records, ())

    async def test_product_config_apply_writes_runtime_and_managed_secret(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _write_runtime_key_safety_policy(database_url=database_url)
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_config_policy(action="product_config.apply"),
                record_store_factory=lambda: app_store,
            )
            request_payload = {**_product_config_payload(), "mode": "apply"}

            with patch.dict(
                os.environ,
                {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
                clear=True,
            ):
                response = await _post_product_config_apply(
                    app,
                    request_payload,
                    idempotency_key="product-config-apply",
                )
                runtime_records = app_store.list_runtime_environment_records()
                secret_records = app_store.list_secret_records()
                secret_binding = app_store.list_secret_bindings(limit=None)[0]
            app_store.close()

        payload = response.json()
        response_text = json.dumps(payload, sort_keys=True)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(payload["result"]["mode"], "apply")
        self.assertEqual(payload["result"]["runtime_environment"]["action"], "created")
        self.assertEqual(payload["result"]["secrets"][0]["action"], "created")
        self.assertNotIn("smtp-secret-value", response_text)
        self.assertNotIn("https://www.sellyouroutboard.com", response_text)
        self.assertEqual(len(runtime_records), 1)
        self.assertEqual(
            runtime_records[0],
            RuntimeEnvironmentRecord(
                scope="instance",
                context="sellyouroutboard-prod",
                instance="prod",
                env={
                    "CONTACT_EMAIL_MODE": "smtp",
                    "SELLYOUROUTBOARD_SITE_URL": "https://www.sellyouroutboard.com",
                },
                updated_at=runtime_records[0].updated_at,
                source_label="product-config-api-test",
            ),
        )
        self.assertEqual(len(secret_records), 1)
        self.assertEqual(secret_records[0].name, "SMTP_PASSWORD")
        self.assertEqual(secret_binding.binding_key, "SMTP_PASSWORD")

    async def test_product_config_apply_reports_live_target_runtime_next_action(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _write_runtime_key_safety_policy(database_url=database_url)
            _seed_tracked_target_records(
                database_url=database_url,
                context="sellyouroutboard-prod",
                instance="prod",
                target_id="application-syo-prod",
                target_type="application",
                target_name="syo-prod-app",
            )
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_config_policy(action="product_config.apply"),
                record_store_factory=lambda: app_store,
            )

            with patch.dict(
                os.environ,
                {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
                clear=True,
            ):
                response = await _post_product_config_apply(
                    app,
                    {**_product_config_payload(), "mode": "apply"},
                    idempotency_key="product-config-live-sync",
                )
            app_store.close()

        self.assertEqual(response.status_code, 202)
        result = response.json()["result"]
        self.assertEqual(result["status"], "records_applied_live_sync_required")
        next_action = result["next_actions"][0]
        self.assertEqual(next_action["kind"], "live_target_runtime_apply")
        self.assertEqual(next_action["dry_run"]["endpoint"], "/v1/live-target-runtime/apply")
        self.assertEqual(next_action["apply"]["endpoint"], "/v1/live-target-runtime/apply")
        self.assertEqual(next_action["target"]["target_type"], "application")
        self.assertEqual(next_action["target"]["target_name"], "syo-prod-app")
        self.assertNotIn("smtp-secret-value", json.dumps(response.json(), sort_keys=True))

    async def test_product_config_human_admin_session_can_apply(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _write_runtime_key_safety_policy(
                database_url=database_url,
                rules=(
                    RuntimeSecretSafetyRule(
                        binding_key="META_CONVERSIONS_API_TOKEN",
                        secret_class="prod_only",
                        allowed_contexts=("sellyouroutboard",),
                        allowed_instances=("prod",),
                    ),
                ),
            )
            app_store = PostgresRecordStore(database_url=database_url)
            oauth_config = _github_oauth_config()
            session_store = InMemoryHumanSessionStore()
            session_manager = HumanSessionManager(
                config=oauth_config,
                session_store=session_store,
            )
            human_session = session_manager.issue(_github_human_identity())
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_github_human_product_config_policy(action="product_config.apply"),
                record_store_factory=lambda: app_store,
                human_session_manager=session_manager,
            )

            with patch.dict(
                os.environ,
                {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
                clear=True,
            ):
                response = await _post_product_config_apply(
                    app,
                    _meta_product_config_payload(mode="apply"),
                    authorization="",
                    headers={"Cookie": session_manager.session_cookie_header(human_session)},
                    idempotency_key="product-config-human-apply",
                )
                runtime_records = app_store.list_runtime_environment_records()
                secret_records = app_store.list_secret_records()
            app_store.close()

        response_text = json.dumps(response.json(), sort_keys=True)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["result"]["mode"], "apply")
        self.assertNotIn("meta-conversions-api-secret-value", response_text)
        self.assertEqual(len(runtime_records), 1)
        self.assertEqual(runtime_records[0].context, "sellyouroutboard")
        self.assertEqual(len(secret_records), 1)
        self.assertEqual(secret_records[0].name, "META_CONVERSIONS_API_TOKEN")

    async def test_product_config_apply_requires_apply_authorization(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            app_store = PostgresRecordStore(database_url=database_url)
            app_store.ensure_schema()
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_config_policy(action="product_config.plan"),
                record_store_factory=lambda: app_store,
            )

            response = await _post_product_config_apply(
                app,
                {**_product_config_payload(), "mode": "apply"},
            )
            app_store.close()

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_product_config_rejects_unauthorized_context(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_config_policy(
                    action="product_config.plan",
                    context="sellyouroutboard-testing",
                ),
                record_store_factory=lambda: app_store,
            )

            response = await _post_product_config_apply(app, _product_config_payload())
            app_store.close()

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_product_config_rejects_read_only_human_apply(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            app_store = PostgresRecordStore(database_url=database_url)
            oauth_config = _github_oauth_config()
            session_store = InMemoryHumanSessionStore()
            session_manager = HumanSessionManager(
                config=oauth_config,
                session_store=session_store,
            )
            human_session = session_manager.issue(_github_human_identity(role="read_only"))
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_github_human_product_config_policy(
                    action="product_config.apply",
                    role="read_only",
                ),
                record_store_factory=lambda: app_store,
                human_session_manager=session_manager,
            )

            response = await _post_product_config_apply(
                app,
                _meta_product_config_payload(mode="apply"),
                authorization="",
                headers={"Cookie": session_manager.session_cookie_header(human_session)},
                idempotency_key="product-config-read-only-human-apply",
            )
            app_store.close()

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_product_config_terminal_agent_remains_read_only(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_product_config_policy(action="product_config.apply"),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=BearerIdentityConfig(
                terminal_agent_token="terminal-agent-token",
                terminal_agent_subject="terminal-agent",
                terminal_agent_token_label="terminal-read-token",
            ),
        )

        response = await _post_product_config_apply(
            app,
            _meta_product_config_payload(mode="apply"),
            authorization="Bearer terminal-agent-token",
            idempotency_key="product-config-terminal-agent",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_product_config_requires_configured_local_operator_identity(
        self,
    ) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_local_operator_policy(
                actions=("product_config.plan",),
                products=("sellyouroutboard",),
                contexts=("sellyouroutboard",),
                token_label="configured-write-token",
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=BearerIdentityConfig(
                local_operator_token="local-operator-token",
            ),
        )

        response = await _post_product_config_apply(
            app,
            _meta_product_config_payload(reason="Dry-run Meta config from local operator."),
            authorization="Bearer local-operator-token",
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "authentication_required")

    async def test_product_config_local_operator_allows_configured_identity(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _write_runtime_key_safety_policy(
                database_url=database_url,
                rules=(
                    RuntimeSecretSafetyRule(
                        binding_key="META_CONVERSIONS_API_TOKEN",
                        secret_class="prod_only",
                        allowed_contexts=("sellyouroutboard",),
                        allowed_instances=("prod",),
                    ),
                ),
            )
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_local_operator_policy(
                    actions=("product_config.plan",),
                    products=("sellyouroutboard",),
                    contexts=("sellyouroutboard",),
                    subject="configured-local-owner",
                    token_label="configured-write-token",
                ),
                record_store_factory=lambda: app_store,
                bearer_identity_config=BearerIdentityConfig(
                    local_operator_token="local-operator-token",
                    local_operator_subject="configured-local-owner",
                    local_operator_token_label="configured-write-token",
                ),
            )

            with patch.dict(
                os.environ,
                {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
                clear=True,
            ):
                response = await _post_product_config_apply(
                    app,
                    _meta_product_config_payload(reason="Dry-run Meta config from local operator."),
                    authorization="Bearer local-operator-token",
                    idempotency_key="product-config-configured-local-operator",
                )
            app_store.close()

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["result"]["mode"], "dry-run")

    async def test_product_config_terminal_agent_rejects_before_payload_validation(
        self,
    ) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_product_config_policy(action="product_config.apply"),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=BearerIdentityConfig(
                terminal_agent_token="terminal-agent-token",
                terminal_agent_subject="terminal-agent",
                terminal_agent_token_label="terminal-read-token",
            ),
        )

        response = await _post_product_config_apply(
            app,
            {},
            authorization="Bearer terminal-agent-token",
            raw_body=b"not-json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_product_config_rejects_missing_master_key_for_secret_bundle(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            app_store = PostgresRecordStore(database_url=database_url)
            app_store.ensure_schema()
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_config_policy(action="product_config.plan"),
                record_store_factory=lambda: app_store,
            )

            with patch.dict(os.environ, {}, clear=True):
                response = await _post_product_config_apply(app, _product_config_payload())
            app_store.close()

        payload = response.json()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(payload["error"]["code"], "secret_configuration_required")
        self.assertEqual(
            payload["error"]["message"],
            "Launchplane service is missing required secret write configuration.",
        )
        self.assertNotIn("LAUNCHPLANE_MASTER_ENCRYPTION_KEY", json.dumps(payload))

    async def test_product_config_requires_runtime_key_safety_policy(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            app_store = PostgresRecordStore(database_url=database_url)
            app_store.ensure_schema()
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_config_policy(action="product_config.plan"),
                record_store_factory=lambda: app_store,
            )

            with patch.dict(
                os.environ,
                {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
                clear=True,
            ):
                response = await _post_product_config_apply(
                    app,
                    _product_config_payload(),
                    idempotency_key="product-config-missing-key-policy",
                )
            app_store.close()

        payload = response.json()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(payload["error"]["code"], "runtime_key_safety_unavailable")
        self.assertEqual(
            payload["error"]["message"],
            "Launchplane runtime key-safety policy is unavailable.",
        )

    async def test_product_config_rejects_runtime_env_target_override(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_config_policy(action="product_config.plan"),
                record_store_factory=lambda: app_store,
            )
            request_payload = _product_config_payload()
            runtime_env = dict(cast(dict[str, object], request_payload["runtime_env"]))
            runtime_env["context"] = "sellyouroutboard-testing"
            request_payload["runtime_env"] = runtime_env

            response = await _post_product_config_apply(app, request_payload)
            app_store.close()

        payload = response.json()
        response_text = json.dumps(payload, sort_keys=True)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertEqual(payload["error"]["message"], "Product config request failed validation.")
        self.assertNotIn("sellyouroutboard-testing", response_text)

    async def test_product_config_rejects_secret_scope_override(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_config_policy(action="product_config.plan"),
                record_store_factory=lambda: app_store,
            )
            request_payload = _product_config_payload()
            request_secrets = _product_config_secrets(request_payload)
            request_payload["secrets"] = [{**request_secrets[0], "scope": "global"}]

            response = await _post_product_config_apply(app, request_payload)
            app_store.close()

        payload = response.json()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertEqual(payload["error"]["message"], "Product config request failed validation.")

    async def test_product_config_rejects_secret_target_override(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_config_policy(action="product_config.plan"),
                record_store_factory=lambda: app_store,
            )
            request_payload = _product_config_payload()
            request_secrets = _product_config_secrets(request_payload)
            request_payload["secrets"] = [
                {
                    **request_secrets[0],
                    "context": "sellyouroutboard-testing",
                }
            ]

            response = await _post_product_config_apply(app, request_payload)
            app_store.close()

        payload = response.json()
        response_text = json.dumps(payload, sort_keys=True)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertEqual(payload["error"]["message"], "Product config request failed validation.")
        self.assertNotIn("sellyouroutboard-testing", response_text)

    async def test_product_config_local_operator_apply_requires_matching_dry_run(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            app_store = PostgresRecordStore(database_url=database_url)
            app_store.ensure_schema()
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_local_operator_policy(
                    actions=("product_config.apply",),
                    products=("sellyouroutboard",),
                    contexts=("sellyouroutboard",),
                    token_label="local-owner-write",
                ),
                record_store_factory=lambda: app_store,
                bearer_identity_config=BearerIdentityConfig(
                    local_operator_token="local-operator-token",
                    local_operator_subject="local-owner-agent",
                    local_operator_token_label="local-owner-write",
                ),
            )

            response = await _post_product_config_apply(
                app,
                _meta_product_config_payload(
                    mode="apply",
                    reason="Apply Meta config after dry-run.",
                ),
                authorization="Bearer local-operator-token",
                idempotency_key="product-config-local-operator-apply-missing-dry-run",
            )
            app_store.close()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "matching_dry_run_required")

    async def test_product_config_local_operator_apply_succeeds_after_dry_run(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _write_runtime_key_safety_policy(
                database_url=database_url,
                rules=(
                    RuntimeSecretSafetyRule(
                        binding_key="META_CONVERSIONS_API_TOKEN",
                        secret_class="prod_only",
                        allowed_contexts=("sellyouroutboard",),
                        allowed_instances=("prod",),
                    ),
                ),
            )
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_local_operator_policy(
                    actions=("product_config.plan", "product_config.apply"),
                    products=("sellyouroutboard",),
                    contexts=("sellyouroutboard",),
                    token_label="local-owner-write",
                ),
                record_store_factory=lambda: app_store,
                bearer_identity_config=BearerIdentityConfig(
                    local_operator_token="local-operator-token",
                    local_operator_subject="local-owner-agent",
                    local_operator_token_label="local-owner-write",
                ),
            )

            with patch.dict(
                os.environ,
                {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
                clear=True,
            ):
                dry_run_response = await _post_product_config_apply(
                    app,
                    _meta_product_config_payload(
                        reason="Dry-run Meta config before terminal apply."
                    ),
                    authorization="Bearer local-operator-token",
                    idempotency_key="product-config-local-operator-dry-run",
                )
                repeat_dry_run_response = await _post_product_config_apply(
                    app,
                    _meta_product_config_payload(
                        reason="Dry-run Meta config before terminal apply."
                    ),
                    authorization="Bearer local-operator-token",
                    idempotency_key="product-config-local-operator-dry-run-repeat",
                )
                apply_response = await _post_product_config_apply(
                    app,
                    _meta_product_config_payload(
                        mode="apply",
                        reason="Apply Meta config after review.",
                    ),
                    authorization="Bearer local-operator-token",
                    idempotency_key="product-config-local-operator-apply",
                )
                runtime_records = app_store.list_runtime_environment_records()
                secret_records = app_store.list_secret_records()
            app_store.close()

        self.assertEqual(dry_run_response.status_code, 202)
        self.assertEqual(repeat_dry_run_response.status_code, 202)
        self.assertEqual(apply_response.status_code, 202)
        self.assertEqual(apply_response.json()["result"]["mode"], "apply")
        self.assertEqual(len(runtime_records), 1)
        self.assertEqual(len(secret_records), 1)

    async def test_product_config_dry_run_marker_accepts_concurrent_matching_insert(
        self,
    ) -> None:
        store = _ConcurrentProductConfigDryRunMarkerStore()

        store_product_config_dry_run_record(
            record_store=store,
            identity=LocalOperatorIdentity(
                subject="local-owner-agent",
                token_label="local-owner-write",
            ),
            request_payload=_meta_product_config_payload(
                reason="Dry-run Meta config before terminal apply."
            ),
            trace_id="launchplane_req_product_config_dry_run",
            response=AcceptedEvidenceResponse(
                trace_id="launchplane_req_product_config_dry_run",
                records={},
                result={"mode": "dry-run"},
            ),
        )

        self.assertEqual(store.read_calls, 2)
        self.assertEqual(store.write_calls, 1)

    async def test_product_config_dry_run_marker_reraises_without_concurrent_insert(
        self,
    ) -> None:
        store = _ConcurrentProductConfigDryRunMarkerStore(after_write="missing")

        with self.assertRaisesRegex(RuntimeError, "simulated duplicate dry-run marker write"):
            store_product_config_dry_run_record(
                record_store=store,
                identity=LocalOperatorIdentity(
                    subject="local-owner-agent",
                    token_label="local-owner-write",
                ),
                request_payload=_meta_product_config_payload(
                    reason="Dry-run Meta config before terminal apply."
                ),
                trace_id="launchplane_req_product_config_dry_run",
                response=AcceptedEvidenceResponse(
                    trace_id="launchplane_req_product_config_dry_run",
                    records={},
                    result={"mode": "dry-run"},
                ),
            )

        self.assertEqual(store.read_calls, 2)
        self.assertEqual(store.write_calls, 1)

    async def test_product_config_dry_run_marker_reraises_mismatched_concurrent_insert(
        self,
    ) -> None:
        store = _ConcurrentProductConfigDryRunMarkerStore(after_write="mismatched")

        with self.assertRaisesRegex(RuntimeError, "simulated duplicate dry-run marker write"):
            store_product_config_dry_run_record(
                record_store=store,
                identity=LocalOperatorIdentity(
                    subject="local-owner-agent",
                    token_label="local-owner-write",
                ),
                request_payload=_meta_product_config_payload(
                    reason="Dry-run Meta config before terminal apply."
                ),
                trace_id="launchplane_req_product_config_dry_run",
                response=AcceptedEvidenceResponse(
                    trace_id="launchplane_req_product_config_dry_run",
                    records={},
                    result={"mode": "dry-run"},
                ),
            )

        self.assertEqual(store.read_calls, 2)
        self.assertEqual(store.write_calls, 1)

    async def test_product_config_idempotency_replay_and_conflict(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _write_runtime_key_safety_policy(database_url=database_url)
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_config_policy(action="product_config.apply"),
                record_store_factory=lambda: app_store,
            )
            payload = {**_product_config_payload(), "mode": "apply"}
            changed_payload = {
                **payload,
                "runtime_env": {"scope": "instance", "env": {"CONTACT_EMAIL_MODE": "api"}},
            }

            with patch.dict(
                os.environ,
                {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
                clear=True,
            ):
                first_response = await _post_product_config_apply(
                    app,
                    payload,
                    idempotency_key="product-config-idempotent",
                )
                replay_response = await _post_product_config_apply(
                    app,
                    payload,
                    idempotency_key="product-config-idempotent",
                )
                conflict_response = await _post_product_config_apply(
                    app,
                    changed_payload,
                    idempotency_key="product-config-idempotent",
                )
            app_store.close()

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(replay_response.status_code, 202)
        self.assertTrue(replay_response.json()["replayed"])
        self.assertEqual(
            replay_response.json()["original_trace_id"], first_response.json()["trace_id"]
        )
        self.assertEqual(conflict_response.status_code, 409)
        self.assertEqual(conflict_response.json()["error"]["code"], "idempotency_key_reused")

    async def test_product_config_requires_database_storage(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_config_policy(action="product_config.plan"),
                record_store_factory=lambda: store,
            )

            response = await _post_product_config_apply(
                app,
                _product_config_payload(),
                idempotency_key="product-config-filesystem-store",
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "database_required")

    async def test_product_config_validation_errors_are_sanitized(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_config_policy(action="product_config.plan"),
                record_store_factory=lambda: app_store,
            )
            request_payload = _product_config_payload()
            request_payload["secrets"] = []
            request_payload["runtime_env"] = {"scope": "instance", "env": {"API_TOKEN": "nope"}}

            response = await _post_product_config_apply(app, request_payload)
            app_store.close()

        payload = response.json()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertEqual(payload["error"]["message"], "Product config request failed validation.")
        self.assertNotIn("API_TOKEN", json.dumps(payload, sort_keys=True))

    async def test_openapi_includes_product_config_apply_contract(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_config_policy(action="product_config.plan"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        route = response.json()["paths"]["/v1/product-config/apply"]["post"]
        self.assertEqual(route["operationId"], "apply_product_config")
        self.assertEqual(
            route["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/AcceptedEvidenceResponse",
        )
        self.assertEqual(
            route["requestBody"]["content"]["application/json"]["schema"]["title"],
            "ProductConfigApplyEnvelope",
        )
        for status_code in ("400", "401", "403", "409", "503"):
            self.assertIn(status_code, route["responses"])


class FastApiProductContextCutoverTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_cutover_apply_updates_profile_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                store.write_product_profile_record(
                    LaunchplaneProductProfileRecord.model_validate(
                        _product_profile_payload_with_prod()
                    )
                )
            finally:
                store.close()
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_profile_write_policy(product="sellyouroutboard"),
                record_store_factory=lambda: app_store,
            )

            payload: dict[str, object] = {
                "product": "sellyouroutboard",
                "source_context": "sellyouroutboard-testing",
                "target_context": "sellyouroutboard",
                "mode": "apply",
                "display_name": "SellYourOutboard",
                "source_label": "test:context-cutover",
            }
            response = await _post_context_cutover_apply(
                app,
                payload,
                idempotency_key="profile-context-cutover",
            )
            replay_response = await _post_context_cutover_apply(
                app,
                payload,
                idempotency_key="profile-context-cutover",
            )
            stored_profile = app_store.read_product_profile_record("sellyouroutboard")
            app_store.close()

        self.assertEqual(response.status_code, 202)
        body = response.json()
        replay_body = replay_response.json()
        self.assertEqual(body["records"], {"product_profile": "sellyouroutboard"})
        self.assertEqual(replay_response.status_code, 202)
        self.assertEqual(replay_body["records"], {"product_profile": "sellyouroutboard"})
        self.assertEqual(replay_body["result"], body["result"])
        self.assertEqual(body["result"]["profile"]["display_name"], "SellYourOutboard")
        self.assertEqual(stored_profile.display_name, "SellYourOutboard")
        self.assertEqual({lane.context for lane in stored_profile.lanes}, {"sellyouroutboard"})
        self.assertEqual(stored_profile.preview.context, "sellyouroutboard")

    async def test_context_cutover_apply_rejects_contexts_outside_product_boundary(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                store.write_product_profile_record(
                    LaunchplaneProductProfileRecord.model_validate(
                        _product_profile_payload_with_prod()
                    )
                )
            finally:
                store.close()
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_profile_write_policy(product="sellyouroutboard"),
                record_store_factory=lambda: app_store,
            )

            response = await _post_context_cutover_apply(
                app,
                {
                    "product": "sellyouroutboard",
                    "source_context": "verireel-testing",
                    "target_context": "sellyouroutboard",
                    "mode": "dry-run",
                    "display_name": "SellYourOutboard",
                },
                idempotency_key="profile-context-cutover-cross-product",
            )
            app_store.close()

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "context_not_in_product_boundary")

    async def test_legacy_context_cleanup_apply_returns_redacted_dry_run(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            profile_payload = _product_profile_payload_with_prod()
            profile_lanes = cast(tuple[dict[str, object], ...], profile_payload["lanes"])
            profile_payload["lanes"] = tuple(
                {**lane, "context": "sellyouroutboard"} for lane in profile_lanes
            )
            profile_preview = cast(dict[str, object], profile_payload["preview"])
            profile_payload["preview"] = {
                **profile_preview,
                "context": "sellyouroutboard",
            }
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                store.write_product_profile_record(
                    LaunchplaneProductProfileRecord.model_validate(profile_payload)
                )
            finally:
                store.close()
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_profile_write_policy(product="sellyouroutboard"),
                record_store_factory=lambda: app_store,
            )

            response = await _post_legacy_context_cleanup_apply(
                app,
                {
                    "product": "sellyouroutboard",
                    "source_context": "sellyouroutboard-testing",
                    "target_context": "sellyouroutboard",
                    "mode": "dry-run",
                },
                idempotency_key="legacy-context-cleanup-dry-run",
            )
            app_store.close()

        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertEqual(body["records"], {"product_profile": "sellyouroutboard"})
        self.assertFalse(body["result"]["blocked"])
        self.assertEqual(body["result"]["groups"]["runtime_environment_records"], [])

    async def test_context_apply_authenticates_before_malformed_body(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_profile_write_policy(product="sellyouroutboard"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_context_cutover_apply(
            app,
            {},
            authorization="",
            raw_body=b"{",
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "authentication_required")

    async def test_context_apply_rejects_malformed_body_after_authentication(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_profile_write_policy(product="sellyouroutboard"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_context_cutover_apply(
            app,
            {},
            raw_body=b"\xff",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")

    async def test_context_cutover_apply_reports_schema_validation_message(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_profile_write_policy(product="sellyouroutboard"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_context_cutover_apply(app, {"product": "sellyouroutboard"})

        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["error"]["code"], "invalid_request")
        self.assertEqual(body["error"]["message"], "Request payload failed validation.")

    async def test_context_cutover_apply_requires_database_storage(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_profile_write_policy(product="sellyouroutboard"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_context_cutover_apply(
            app,
            {
                "product": "sellyouroutboard",
                "source_context": "sellyouroutboard-testing",
                "target_context": "sellyouroutboard",
            },
        )

        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertEqual(body["error"]["code"], "database_required")
        self.assertEqual(
            body["error"]["message"],
            "Product context cutover requires Launchplane database storage.",
        )

    async def test_context_cutover_apply_requires_database_before_idempotency_replay(
        self,
    ) -> None:
        payload: dict[str, object] = {
            "product": "sellyouroutboard",
            "source_context": "sellyouroutboard-testing",
            "target_context": "sellyouroutboard",
        }
        store = _ProductContextApplyReplayOnlyStore(
            route_path="/v1/product-profiles/context-cutover/apply",
            payload=payload,
            idempotency_key="context-cutover-replay",
        )
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_profile_write_policy(product="sellyouroutboard"),
            record_store_factory=lambda: store,
        )

        response = await _post_context_cutover_apply(
            app,
            payload,
            idempotency_key="context-cutover-replay",
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "database_required")
        self.assertEqual(store.read_idempotency_calls, 0)

    async def test_legacy_context_cleanup_apply_requires_database_before_idempotency_replay(
        self,
    ) -> None:
        payload: dict[str, object] = {
            "product": "sellyouroutboard",
            "source_context": "sellyouroutboard-testing",
            "target_context": "sellyouroutboard",
            "mode": "dry-run",
        }
        store = _ProductContextApplyReplayOnlyStore(
            route_path="/v1/product-profiles/legacy-context-cleanup/apply",
            payload=payload,
            idempotency_key="legacy-context-cleanup-replay",
        )
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_profile_write_policy(product="sellyouroutboard"),
            record_store_factory=lambda: store,
        )

        response = await _post_legacy_context_cleanup_apply(
            app,
            payload,
            idempotency_key="legacy-context-cleanup-replay",
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "database_required")
        self.assertEqual(store.read_idempotency_calls, 0)

    async def test_openapi_includes_context_apply_contracts(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_profile_write_policy(product="sellyouroutboard"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        cutover_route = openapi["paths"]["/v1/product-profiles/context-cutover/apply"]["post"]
        cleanup_route = openapi["paths"]["/v1/product-profiles/legacy-context-cleanup/apply"][
            "post"
        ]
        self.assertEqual(cutover_route["operationId"], "apply_product_context_cutover")
        self.assertEqual(
            cleanup_route["operationId"],
            "apply_product_legacy_context_cleanup",
        )
        self.assertEqual(
            cutover_route["requestBody"]["content"]["application/json"]["schema"]["title"],
            "ProductContextCutoverRequest",
        )
        self.assertEqual(
            cleanup_route["requestBody"]["content"]["application/json"]["schema"]["title"],
            "LegacyContextCleanupRequest",
        )
        for route in (cutover_route, cleanup_route):
            self.assertEqual(
                route["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
                "#/components/schemas/AcceptedEvidenceResponse",
            )
            for status_code in ("400", "401", "403", "404", "409", "503"):
                self.assertIn(status_code, route["responses"])

    async def test_fastapi_context_apply_precedes_legacy_wsgi_fallback(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                store.write_product_profile_record(
                    LaunchplaneProductProfileRecord.model_validate(
                        _product_profile_payload_with_prod()
                    )
                )
            finally:
                store.close()
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_profile_write_policy(product="sellyouroutboard"),
                record_store_factory=lambda: app_store,
            )
            legacy_app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({}),
                control_plane_root_path=root,
            )
            app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

            response = await _post_context_cutover_apply(
                app,
                {
                    "product": "sellyouroutboard",
                    "source_context": "sellyouroutboard-testing",
                    "target_context": "sellyouroutboard",
                    "mode": "dry-run",
                },
            )
            app_store.close()

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["records"], {"product_profile": "sellyouroutboard"})

    async def test_context_cutover_audit_returns_redacted_metadata(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _write_context_cutover_audit_records(database_url)
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_profile_read_policy(product="sellyouroutboard"),
                record_store_factory=lambda: app_store,
            )

            response = await _get_context_cutover_audit(app)
            app_store.close()

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        payload_text = json.dumps(payload, sort_keys=True)
        self.assertEqual(set(payload), {"status", "trace_id", "audit"})
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["trace_id"].startswith("launchplane_req_"))
        self.assertEqual(payload["audit"]["status"], "ok")
        self.assertEqual(payload["audit"]["product"], "sellyouroutboard")
        self.assertEqual(
            payload["audit"]["contexts"]["source"]["runtime_environment_records"][0]["env_keys"],
            ["TAWK_PROPERTY_ID"],
        )
        self.assertEqual(
            payload["audit"]["contexts"]["target"]["runtime_environment_records"][0]["env_keys"],
            ["TAWK_WIDGET_ID"],
        )
        self.assertIn("SMTP_PASSWORD", payload_text)
        self.assertNotIn("property-legacy", payload_text)
        self.assertNotIn("widget-canonical", payload_text)
        self.assertNotIn("smtp-password-secret", payload_text)
        self.assertNotIn("ciphertext", payload_text)
        self.assertNotIn("plaintext", payload_text)

    async def test_context_cutover_audit_requires_identity(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_profile_read_policy(product="sellyouroutboard"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _get_context_cutover_audit(app, authorization="")

        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authentication_required")

    async def test_context_cutover_audit_rejects_unowned_context(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _write_context_cutover_audit_records(database_url)
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_profile_read_policy(product="sellyouroutboard"),
                record_store_factory=lambda: app_store,
            )

            response = await _get_context_cutover_audit(
                app,
                source_context="other-site",
                target_context="sellyouroutboard",
            )
            app_store.close()

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "context_not_in_product_boundary")

    async def test_context_cutover_audit_invalid_request_is_generic(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _write_context_cutover_audit_records(database_url)
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_profile_read_policy(product="sellyouroutboard"),
                record_store_factory=lambda: app_store,
            )

            response = await _get_context_cutover_audit(
                app,
                source_context="sellyouroutboard",
                target_context="sellyouroutboard",
            )
            app_store.close()

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "invalid_context_cutover_audit_request")
        self.assertEqual(
            payload["error"]["message"],
            "Context cutover audit request is invalid.",
        )

    async def test_context_cutover_audit_rejects_unauthorized_product(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _write_context_cutover_audit_records(database_url)
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_profile_read_policy(product="verireel"),
                record_store_factory=lambda: app_store,
            )

            response = await _get_context_cutover_audit(app)
            app_store.close()

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")
        self.assertNotIn("authz", payload)

    async def test_context_cutover_audit_requires_database_storage(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_profile_read_policy(product="sellyouroutboard"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _get_context_cutover_audit(app)

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "database_storage_required")
        self.assertIn("read_product_profile_record", payload["error"]["message"])

    async def test_openapi_includes_context_cutover_audit_contract(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_profile_read_policy(product="sellyouroutboard"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        route = openapi["paths"]["/v1/product-profiles/{product}/context-cutover-audit"]["get"]
        self.assertEqual(route["operationId"], "read_product_context_cutover_audit")
        self.assertEqual(
            route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/ProductContextCutoverAuditResponse",
        )
        self.assertIn("LaunchplaneErrorResponse", json.dumps(route))
        for status_code in ("400", "401", "403", "404", "503"):
            self.assertIn(status_code, route["responses"])
        self.assertEqual(
            openapi["components"]["schemas"]["ProductContextCutoverAuditResponse"][
                "additionalProperties"
            ],
            False,
        )

    async def test_fastapi_context_cutover_audit_precedes_legacy_wsgi_fallback(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _write_context_cutover_audit_records(database_url)
            app_store = PostgresRecordStore(database_url=database_url)
            policy = _product_profile_read_policy(product="sellyouroutboard")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                record_store_factory=lambda: app_store,
            )
            legacy_app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({}),
                control_plane_root_path=root,
            )
            app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

            response = await _get_context_cutover_audit(app)
            app_store.close()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")


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

    async def test_driver_descriptors_renew_expiring_human_session_cookie(self) -> None:
        session_store = InMemoryHumanSessionStore()
        oauth_config = _github_oauth_config()
        session_manager = HumanSessionManager(
            config=oauth_config,
            session_store=session_store,
        )
        session = LaunchplaneHumanSession(
            session_id="expiring-session",
            identity=_github_human_identity(),
            created_at=datetime.now(timezone.utc) - timedelta(days=13),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=12),
        )
        session_store.write_session(session)
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_github_human_driver_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            human_session_manager=session_manager,
        )

        response = await _get_driver_descriptors(
            app,
            authorization="",
            headers={"Cookie": session_manager.session_cookie_header(session)},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("launchplane_session=", response.headers["Set-Cookie"])
        self.assertIn("Max-Age=1209600", response.headers["Set-Cookie"])
        renewed_session = session_store.read_session("expiring-session")
        self.assertIsNotNone(renewed_session)
        assert renewed_session is not None
        self.assertGreater(renewed_session.expires_at, session.expires_at)

    async def test_driver_descriptors_preserve_renewed_session_cookie_on_denial(
        self,
    ) -> None:
        session_store = InMemoryHumanSessionStore()
        oauth_config = _github_oauth_config()
        session_manager = HumanSessionManager(
            config=oauth_config,
            session_store=session_store,
        )
        session = LaunchplaneHumanSession(
            session_id="expiring-session",
            identity=_github_human_identity(),
            created_at=datetime.now(timezone.utc) - timedelta(days=13),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=12),
        )
        session_store.write_session(session)
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_github_human_driver_read_policy(context="other-context"),
            record_store_factory=lambda: _MissingProductReadStore(),
            human_session_manager=session_manager,
        )

        response = await _get_driver_descriptors(
            app,
            authorization="",
            headers={"Cookie": session_manager.session_cookie_header(session)},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")
        self.assertIn("launchplane_session=", response.headers["Set-Cookie"])
        renewed_session = session_store.read_session("expiring-session")
        self.assertIsNotNone(renewed_session)
        assert renewed_session is not None
        self.assertGreater(renewed_session.expires_at, session.expires_at)

    async def test_driver_descriptor_preserves_renewed_session_cookie_on_validation_error(
        self,
    ) -> None:
        session_store = InMemoryHumanSessionStore()
        oauth_config = _github_oauth_config()
        session_manager = HumanSessionManager(
            config=oauth_config,
            session_store=session_store,
        )
        session = LaunchplaneHumanSession(
            session_id="expiring-session",
            identity=_github_human_identity(),
            created_at=datetime.now(timezone.utc) - timedelta(days=13),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=12),
        )
        session_store.write_session(session)
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_github_human_driver_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            human_session_manager=session_manager,
        )

        response = await _get_driver_descriptor(
            app,
            "bad driver id",
            authorization="",
            headers={"Cookie": session_manager.session_cookie_header(session)},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")
        self.assertIn("launchplane_session=", response.headers["Set-Cookie"])
        renewed_session = session_store.read_session("expiring-session")
        self.assertIsNotNone(renewed_session)
        assert renewed_session is not None
        self.assertGreater(renewed_session.expires_at, session.expires_at)

    async def test_driver_descriptors_use_session_when_bearer_header_is_malformed(
        self,
    ) -> None:
        oauth_config = _github_oauth_config()
        session_store = InMemoryHumanSessionStore()
        session_manager = HumanSessionManager(
            config=oauth_config,
            session_store=session_store,
        )
        human_session = session_manager.issue(_github_human_identity())
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_github_human_driver_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            human_session_manager=session_manager,
        )

        response = await _get_driver_descriptors(
            app,
            authorization="Token malformed",
            headers={"Cookie": session_manager.session_cookie_header(human_session)},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["drivers"][0]["driver_id"], "generic-web")

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


class FastApiDriverContextViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_driver_instance_view_returns_lane_summary(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            record_store = _driver_context_store(Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_driver_read_policy(context="example-site"),
                record_store_factory=lambda: record_store,
                bearer_identity_config=_local_operator_bearer_config(),
            )

            response = await _get_driver_instance_view(app, "example-site", "testing")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["view"]["context"], "example-site")
        self.assertEqual(payload["view"]["instance"], "testing")
        self.assertEqual(payload["view"]["drivers"][0]["driver_id"], "example-site")
        self.assertEqual(
            payload["view"]["drivers"][0]["descriptor"]["base_driver_id"], "generic-web"
        )
        available_actions = {
            action["action_id"]: action
            for action in payload["view"]["drivers"][0]["available_actions"]
        }
        self.assertEqual(
            available_actions["prod_promotion"]["route_path"],
            "/v1/drivers/generic-web/prod-promotion",
        )
        self.assertEqual(
            payload["view"]["drivers"][0]["lane_summary"]["latest_deployment"]["record_id"],
            "deployment-example-site-testing",
        )

    async def test_driver_context_view_returns_context_summary(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            record_store = _driver_context_store(Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_driver_read_policy(context="example-site"),
                record_store_factory=lambda: record_store,
                bearer_identity_config=_local_operator_bearer_config(),
            )

            response = await _get_driver_context_view(app, "example-site")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["view"]["context"], "example-site")
        self.assertEqual(payload["view"]["instance"], "")
        self.assertEqual(payload["view"]["drivers"][0]["driver_id"], "example-site")
        self.assertEqual(
            payload["view"]["drivers"][0]["descriptor"]["base_driver_id"], "generic-web"
        )

    async def test_driver_context_view_requires_bearer_or_human_identity(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_driver_read_policy(context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=_local_operator_bearer_config(),
        )

        response = await _get_driver_context_view(app, "example-site", authorization="")

        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authentication_required")
        self.assertEqual(response.headers["WWW-Authenticate"], 'Bearer realm="Launchplane API"')

    async def test_driver_context_view_rejects_wrong_context_grant(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_driver_read_policy(context="other-context"),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=_local_operator_bearer_config(),
        )

        response = await _get_driver_instance_view(app, "example-site", "testing")

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    async def test_driver_context_view_accepts_human_session_when_mounted_over_wsgi(
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
            policy = _github_human_driver_read_policy(context="example-site")
            record_store = _driver_context_store(root / "state")
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
                github_oauth_config=oauth_config,
                human_session_store=session_store,
                control_plane_root_path=root,
            )
            app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

            response = await _get_driver_context_view(
                app,
                "example-site",
                authorization="",
                headers={"Cookie": session_manager.session_cookie_header(human_session)},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["view"]["drivers"][0]["driver_id"], "example-site")
        self.assertEqual(
            payload["view"]["drivers"][0]["descriptor"]["base_driver_id"], "generic-web"
        )
        self.assertNotIn("Set-Cookie", response.headers)

    async def test_openapi_includes_driver_view_contracts(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_driver_read_policy(context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=_local_operator_bearer_config(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        context_route = openapi["paths"]["/v1/contexts/{context}/driver-view"]["get"]
        instance_route = openapi["paths"][
            "/v1/contexts/{context}/instances/{instance}/driver-view"
        ]["get"]
        self.assertEqual(context_route["operationId"], "read_driver_context_view")
        self.assertEqual(instance_route["operationId"], "read_driver_instance_view")
        self.assertEqual(
            context_route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/DriverContextViewResponse",
        )
        self.assertEqual(
            instance_route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/DriverContextViewResponse",
        )
        self.assertIn("LaunchplaneErrorResponse", json.dumps(context_route))
        self.assertIn("LaunchplaneErrorResponse", json.dumps(instance_route))
        self.assertEqual(
            openapi["components"]["schemas"]["DriverContextViewResponse"]["additionalProperties"],
            False,
        )


class FastApiDokployTargetInspectReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_dokploy_target_inspect_reads_redacted_provider_identity(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _seed_dokploy_target_inspect_records(database_url)
            app_store = PostgresRecordStore(database_url=database_url)
            with (
                patch(
                    "control_plane.http_app.control_plane_dokploy.read_dokploy_config",
                    return_value=("https://dokploy.example.invalid", "token"),
                ) as read_dokploy_config,
                patch(
                    "control_plane.dokploy_target_inspect.control_plane_dokploy.fetch_dokploy_target_payload",
                    return_value={
                        "id": "compose-cm-prod",
                        "name": "cm-prod",
                        "serverId": "server-123",
                        "environment": {
                            "id": "env-prod",
                            "name": "prod",
                            "project": {"id": "project-odoo", "name": "odoo"},
                        },
                        "env": "ODOO_DB_PASSWORD=secret\nDISABLE_ODOO_ONLINE=true\n",
                    },
                ),
            ):
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_record_read_policy(
                        action="dokploy_target.inspect",
                        context="launchplane",
                    ),
                    database_url=database_url,
                    record_store_factory=lambda: app_store,
                    control_plane_root_path=root,
                )

                response = await _get_dokploy_target_inspect(
                    app,
                    context="cm_website",
                    instance="prod",
                )
                app_store.close()

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["inspect"]["target_id"], "compose-cm-prod")
        self.assertEqual(payload["inspect"]["tracked_target"]["target_name"], "cm-prod")
        self.assertEqual(payload["inspect"]["provider"]["environment"]["id"], "env-prod")
        self.assertEqual(
            payload["inspect"]["provider"]["env"]["keys"],
            ["DISABLE_ODOO_ONLINE", "ODOO_DB_PASSWORD"],
        )
        self.assertTrue(payload["inspect"]["provider_payload_redacted"])
        self.assertNotIn("secret", str(payload))
        self.assertNotIn("provider_evidence", str(payload))
        read_dokploy_config.assert_called_once_with(
            control_plane_root=root,
            database_url=database_url,
        )

    async def test_dokploy_target_inspect_rejects_without_authz(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                database_url=database_url,
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            response = await _get_dokploy_target_inspect(
                app,
                target_type="compose",
                target_id="compose-123",
            )
            store.close()

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    async def test_dokploy_target_inspect_requires_database_storage(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(
                action="dokploy_target.inspect",
                context="launchplane",
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _get_dokploy_target_inspect(
            app,
            target_type="compose",
            target_id="compose-123",
        )

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "database_required")

    async def test_dokploy_target_inspect_rejects_invalid_query_mode(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_record_read_policy(
                    action="dokploy_target.inspect",
                    context="launchplane",
                ),
                database_url=database_url,
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            response = await _get_dokploy_target_inspect(
                app,
                context="cm_website",
                instance="prod",
                target_type="compose",
                target_id="compose-123",
            )
            store.close()

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "invalid_dokploy_target_inspect")

    async def test_dokploy_target_inspect_returns_not_found_for_unknown_route(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            with patch(
                "control_plane.http_app.control_plane_dokploy.read_dokploy_config",
                return_value=("https://dokploy.example.invalid", "token"),
            ):
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_record_read_policy(
                        action="dokploy_target.inspect",
                        context="launchplane",
                    ),
                    database_url=database_url,
                    record_store_factory=lambda: store,
                    control_plane_root_path=root,
                )

                response = await _get_dokploy_target_inspect(
                    app,
                    context="missing",
                    instance="prod",
                )
                store.close()

        self.assertEqual(response.status_code, 404)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "not_found")

    async def test_openapi_includes_dokploy_target_inspect_contract(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(
                action="dokploy_target.inspect",
                context="launchplane",
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        inspect_route = openapi["paths"]["/v1/dokploy-targets/inspect"]["get"]
        self.assertEqual(inspect_route["operationId"], "read_dokploy_target_inspect")
        self.assertEqual(
            inspect_route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/DokployTargetInspectResponse",
        )
        self.assertIn("LaunchplaneErrorResponse", json.dumps(inspect_route))
        self.assertEqual(
            openapi["components"]["schemas"]["DokployTargetInspectResponse"][
                "additionalProperties"
            ],
            False,
        )

    async def test_fastapi_dokploy_target_inspect_precedes_legacy_wsgi_fallback(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _seed_dokploy_target_inspect_records(database_url)
            app_store = PostgresRecordStore(database_url=database_url)
            with (
                patch(
                    "control_plane.http_app.control_plane_dokploy.read_dokploy_config",
                    return_value=("https://dokploy.example.invalid", "token"),
                ),
                patch(
                    "control_plane.dokploy_target_inspect.control_plane_dokploy.fetch_dokploy_target_payload",
                    return_value={"id": "compose-cm-prod", "name": "cm-prod"},
                ),
            ):
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_record_read_policy(
                        action="dokploy_target.inspect",
                        context="launchplane",
                    ),
                    database_url=database_url,
                    record_store_factory=lambda: app_store,
                    control_plane_root_path=root,
                )
                legacy_app = create_launchplane_service_app(
                    state_dir=root / "state",
                    verifier=_StubVerifier(_identity()),
                    authz_policy=LaunchplaneAuthzPolicy.model_validate({}),
                    control_plane_root_path=root,
                )
                app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

                response = await _get_dokploy_target_inspect(
                    app,
                    context="cm_website",
                    instance="prod",
                )
                app_store.close()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")


class FastApiDokployTargetSetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_openapi_includes_dokploy_target_setup_contract(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(
                action="dokploy_target.setup",
                context="launchplane",
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        setup_route = openapi["paths"]["/v1/dokploy-targets/setup"]["post"]
        self.assertEqual(setup_route["operationId"], "setup_dokploy_target")
        self.assertEqual(
            setup_route["requestBody"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/DokployTargetSetupEnvelope",
        )
        self.assertEqual(
            setup_route["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/AcceptedEvidenceResponse",
        )
        self.assertIn("LaunchplaneErrorResponse", json.dumps(setup_route))
        self.assertEqual(
            openapi["components"]["schemas"]["DokployTargetSetupEnvelope"]["additionalProperties"],
            False,
        )

    async def test_dokploy_target_setup_requires_database_storage(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_record_read_policy(
                    action="dokploy_target.setup",
                    context="launchplane",
                ),
                record_store_factory=lambda: store,
            )

            response = await _asgi_request(
                app,
                "POST",
                "/v1/dokploy-targets/setup",
                headers={"Authorization": "Bearer valid-token"},
                payload={
                    "schema_version": 1,
                    "mode": "dry-run",
                    "operation": "create-compose",
                    "product": "launchplane",
                    "context": "cm_website",
                    "instance": "testing",
                    "target_name": "cm-website-testing",
                    "project_name": "Odoo",
                    "environment_name": "production",
                    "server_id": "server-123",
                },
            )

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "database_required")

    async def test_fastapi_dokploy_target_setup_precedes_legacy_wsgi_fallback(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            app_store = PostgresRecordStore(database_url=database_url)
            app_store.ensure_schema()
            with patch(
                "control_plane.dokploy_target_setup_http.control_plane_dokploy.read_dokploy_config",
                return_value=("https://dokploy.example.invalid", "token"),
            ):
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_record_read_policy(
                        action="dokploy_target.setup",
                        context="launchplane",
                    ),
                    database_url=database_url,
                    record_store_factory=lambda: app_store,
                    control_plane_root_path=root,
                )
                legacy_app = create_launchplane_service_app(
                    state_dir=root / "state",
                    verifier=_StubVerifier(_identity()),
                    authz_policy=LaunchplaneAuthzPolicy.model_validate({}),
                    control_plane_root_path=root,
                )
                app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/dokploy-targets/setup",
                    headers={"Authorization": "Bearer valid-token"},
                    payload={
                        "schema_version": 1,
                        "mode": "dry-run",
                        "operation": "create-compose",
                        "product": "launchplane",
                        "context": "cm_website",
                        "instance": "testing",
                        "target_name": "cm-website-testing",
                        "project_name": "Odoo",
                        "environment_name": "production",
                        "server_id": "server-123",
                        "domains": ["cm-website-testing.example.invalid"],
                        "runtime_port": 8069,
                        "deploy_timeout_seconds": 900,
                    },
                )
                app_store.close()

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(payload["result"]["mode"], "dry-run")


class FastApiTrackedTargetLogsReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_tracked_target_logs_returns_redacted_application_logs(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _seed_tracked_target_records(
                database_url=database_url,
                context="sellyouroutboard-testing",
                instance="testing",
                target_id="app-123",
                target_type="application",
                target_name="syo-testing-app",
            )
            app_store = PostgresRecordStore(database_url=database_url)
            with (
                patch(
                    "control_plane.tracked_target_logs.control_plane_dokploy.read_dokploy_config",
                    return_value=("https://dokploy.example.com", "secret-token"),
                ),
                patch(
                    "control_plane.tracked_target_logs.control_plane_dokploy.fetch_dokploy_target_payload",
                    return_value={"appName": "syo-testing-gfbiqh", "serverId": "server-1"},
                ),
                patch(
                    "control_plane.tracked_target_logs.control_plane_dokploy.fetch_dokploy_application_logs",
                    return_value=("contact form submitted",),
                ) as logs_mock,
            ):
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_record_read_policy(
                        action="target_logs.read",
                        context="sellyouroutboard-testing",
                    ),
                    record_store_factory=lambda: app_store,
                    control_plane_root_path=root,
                )
                response = await _get_tracked_target_logs(
                    app,
                    "sellyouroutboard-testing",
                    "testing",
                    lines="2",
                    since="5m",
                    search="contact",
                )
                app_store.close()

        self.assertEqual(response.status_code, 200)
        logs_mock.assert_called_once_with(
            host="https://dokploy.example.com",
            token="secret-token",
            application_id="app-123",
            line_count=2,
            since="5m",
            search="contact",
        )
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["context"], "sellyouroutboard-testing")
        self.assertEqual(payload["instance"], "testing")
        self.assertEqual(payload["target"]["target_name"], "syo-testing-app")
        self.assertEqual(payload["target"]["app_name"], "syo-testing-gfbiqh")
        self.assertEqual(payload["request"], {"line_count": 2, "since": "5m", "search": "contact"})
        self.assertEqual(payload["logs"]["lines"], ["contact form submitted"])
        self.assertTrue(payload["logs"]["redacted"])
        self.assertNotIn("secret-token", json.dumps(payload))

    async def test_tracked_target_logs_redacts_raw_secret_values_from_provider_logs(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _seed_tracked_target_records(
                database_url=database_url,
                context="sellyouroutboard-testing",
                instance="testing",
                target_id="app-123",
                target_type="application",
                target_name="syo-testing-app",
            )
            app_store = PostgresRecordStore(database_url=database_url)
            with (
                patch(
                    "control_plane.tracked_target_logs.control_plane_dokploy.read_dokploy_config",
                    return_value=("https://dokploy.example.com", "secret-token"),
                ),
                patch(
                    "control_plane.tracked_target_logs.control_plane_dokploy.fetch_dokploy_target_payload",
                    return_value={"appName": "syo-testing-gfbiqh", "serverId": "server-1"},
                ),
                patch(
                    "control_plane.tracked_target_logs.control_plane_dokploy.fetch_dokploy_application_logs",
                    return_value=("API_TOKEN=plain-secret-value",),
                ),
            ):
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_record_read_policy(
                        action="target_logs.read",
                        context="sellyouroutboard-testing",
                    ),
                    record_store_factory=lambda: app_store,
                    control_plane_root_path=root,
                )
                response = await _get_tracked_target_logs(
                    app,
                    "sellyouroutboard-testing",
                    "testing",
                    lines="2",
                )
                app_store.close()

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["logs"]["lines"], ["API_TOKEN=[redacted]"])
        self.assertNotIn("plain-secret-value", json.dumps(payload))

    async def test_tracked_target_logs_normalizes_uppercase_path_values(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _seed_tracked_target_records(
                database_url=database_url,
                context="sellyouroutboard-testing",
                instance="testing",
                target_id="app-123",
                target_type="application",
                target_name="syo-testing-app",
            )
            app_store = PostgresRecordStore(database_url=database_url)
            with (
                patch(
                    "control_plane.tracked_target_logs.control_plane_dokploy.read_dokploy_config",
                    return_value=("https://dokploy.example.com", "secret-token"),
                ),
                patch(
                    "control_plane.tracked_target_logs.control_plane_dokploy.fetch_dokploy_target_payload",
                    return_value={"appName": "syo-testing-gfbiqh", "serverId": "server-1"},
                ),
                patch(
                    "control_plane.tracked_target_logs.control_plane_dokploy.fetch_dokploy_application_logs",
                    return_value=("contact form submitted",),
                ),
            ):
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_record_read_policy(
                        action="target_logs.read",
                        context="SELLYOUROUTBOARD-TESTING",
                    ),
                    record_store_factory=lambda: app_store,
                    control_plane_root_path=root,
                )
                response = await _get_tracked_target_logs(
                    app,
                    "SELLYOUROUTBOARD-TESTING",
                    "TESTING",
                    lines="2",
                )
                app_store.close()

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["context"], "sellyouroutboard-testing")
        self.assertEqual(payload["instance"], "testing")

    async def test_tracked_target_logs_returns_redacted_compose_logs(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _seed_tracked_target_records(
                database_url=database_url,
                context="cm_website",
                instance="testing",
                target_id="compose-123",
                target_type="compose",
                target_name="cm-website-testing",
            )
            app_store = PostgresRecordStore(database_url=database_url)
            with (
                patch(
                    "control_plane.tracked_target_logs.control_plane_dokploy.read_dokploy_config",
                    return_value=("https://dokploy.example.com", "secret-token"),
                ),
                patch(
                    "control_plane.tracked_target_logs.control_plane_dokploy.fetch_dokploy_target_payload",
                    return_value={"appName": "cm-website-testing-iul0ql", "serverId": "server-1"},
                ),
                patch(
                    "control_plane.tracked_target_logs.control_plane_dokploy.fetch_dokploy_compose_logs",
                    return_value=("booting", "ODOO_ADMIN_PASSWORD=[redacted]"),
                ) as logs_mock,
            ):
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_record_read_policy(
                        action="target_logs.read",
                        context="cm_website",
                    ),
                    record_store_factory=lambda: app_store,
                    control_plane_root_path=root,
                )
                response = await _get_tracked_target_logs(
                    app,
                    "cm_website",
                    "testing",
                    lines="2",
                    since="5m",
                )
                app_store.close()

        self.assertEqual(response.status_code, 200)
        logs_mock.assert_called_once_with(
            host="https://dokploy.example.com",
            token="secret-token",
            compose_id="compose-123",
            app_name="cm-website-testing-iul0ql",
            server_id="server-1",
            line_count=2,
            since="5m",
            search="",
        )
        payload = response.json()
        self.assertEqual(payload["target"]["target_type"], "compose")
        self.assertEqual(payload["target"]["target_name"], "cm-website-testing")
        self.assertEqual(payload["target"]["app_name"], "cm-website-testing-iul0ql")
        self.assertEqual(payload["logs"]["lines"], ["booting", "ODOO_ADMIN_PASSWORD=[redacted]"])
        self.assertNotIn("secret-token", json.dumps(payload))

    async def test_tracked_target_logs_delegates_compose_log_search_to_provider(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _seed_tracked_target_records(
                database_url=database_url,
                context="cm_website",
                instance="testing",
                target_id="compose-123",
                target_type="compose",
                target_name="cm-website-testing",
            )
            app_store = PostgresRecordStore(database_url=database_url)
            with (
                patch(
                    "control_plane.tracked_target_logs.control_plane_dokploy.read_dokploy_config",
                    return_value=("https://dokploy.example.com", "secret-token"),
                ),
                patch(
                    "control_plane.tracked_target_logs.control_plane_dokploy.fetch_dokploy_target_payload",
                    return_value={"appName": "cm-website-testing-iul0ql", "serverId": "server-1"},
                ),
                patch(
                    "control_plane.tracked_target_logs.control_plane_dokploy.fetch_dokploy_compose_logs",
                    return_value=("website_bootstrap_applied name=Cell Mechanic",),
                ) as logs_mock,
            ):
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_record_read_policy(
                        action="target_logs.read",
                        context="cm_website",
                    ),
                    record_store_factory=lambda: app_store,
                    control_plane_root_path=root,
                )
                response = await _get_tracked_target_logs(
                    app,
                    "cm_website",
                    "testing",
                    lines="2",
                    since="2h",
                    search="website_bootstrap_applied",
                )
                app_store.close()

        self.assertEqual(response.status_code, 200)
        logs_mock.assert_called_once_with(
            host="https://dokploy.example.com",
            token="secret-token",
            compose_id="compose-123",
            app_name="cm-website-testing-iul0ql",
            server_id="server-1",
            line_count=2,
            since="2h",
            search="website_bootstrap_applied",
        )
        payload = response.json()
        self.assertEqual(
            payload["request"],
            {"line_count": 2, "since": "2h", "search": "website_bootstrap_applied"},
        )
        self.assertEqual(payload["logs"]["lines"], ["website_bootstrap_applied name=Cell Mechanic"])

    async def test_tracked_target_logs_requires_identity(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(
                action="target_logs.read",
                context="sellyouroutboard-testing",
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _get_tracked_target_logs(
            app,
            "sellyouroutboard-testing",
            "testing",
            authorization="",
        )

        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authentication_required")

    async def test_tracked_target_logs_requires_authz_action(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(
                action="driver.read",
                context="sellyouroutboard-testing",
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _get_tracked_target_logs(
            app,
            "sellyouroutboard-testing",
            "testing",
        )

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    async def test_tracked_target_logs_requires_db_backed_storage(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(
                action="target_logs.read",
                context="sellyouroutboard-testing",
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _get_tracked_target_logs(
            app,
            "sellyouroutboard-testing",
            "testing",
        )

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "database_required")

    async def test_tracked_target_logs_returns_invalid_request_for_missing_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            app_store = PostgresRecordStore(database_url=database_url)
            app_store.ensure_schema()
            with patch(
                "control_plane.tracked_target_logs.control_plane_dokploy.read_dokploy_config"
            ) as read_config_mock:
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_record_read_policy(
                        action="target_logs.read",
                        context="sellyouroutboard-testing",
                    ),
                    record_store_factory=lambda: app_store,
                    control_plane_root_path=root,
                )
                response = await _get_tracked_target_logs(
                    app,
                    "sellyouroutboard-testing",
                    "missing",
                )
                app_store.close()

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "invalid_request")
        read_config_mock.assert_not_called()

    async def test_tracked_target_logs_reports_provider_unavailable(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _seed_tracked_target_records(
                database_url=database_url,
                context="sellyouroutboard-testing",
                instance="testing",
                target_id="app-123",
                target_type="application",
                target_name="syo-testing-app",
            )
            app_store = PostgresRecordStore(database_url=database_url)
            with patch(
                "control_plane.tracked_target_logs.control_plane_dokploy.read_dokploy_config",
                side_effect=ClickException("Dokploy credentials unavailable."),
            ):
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_record_read_policy(
                        action="target_logs.read",
                        context="sellyouroutboard-testing",
                    ),
                    record_store_factory=lambda: app_store,
                    control_plane_root_path=root,
                )
                response = await _get_tracked_target_logs(
                    app,
                    "sellyouroutboard-testing",
                    "testing",
                )
                app_store.close()

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "target_logs_unavailable")

    async def test_tracked_target_logs_validates_query_values(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(
                action="target_logs.read",
                context="sellyouroutboard-testing",
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        line_response = await _get_tracked_target_logs(
            app,
            "sellyouroutboard-testing",
            "testing",
            lines="0",
        )
        since_response = await _get_tracked_target_logs(
            app,
            "sellyouroutboard-testing",
            "testing",
            since="yesterday",
        )
        max_line_response = await _get_tracked_target_logs(
            app,
            "sellyouroutboard-testing",
            "testing",
            lines="1001",
        )

        self.assertEqual(line_response.status_code, 400)
        self.assertEqual(since_response.status_code, 400)
        self.assertEqual(max_line_response.status_code, 400)
        self.assertEqual(line_response.json()["error"]["code"], "invalid_query")
        self.assertEqual(since_response.json()["error"]["code"], "invalid_query")
        self.assertEqual(max_line_response.json()["error"]["code"], "invalid_query")

    async def test_openapi_includes_tracked_target_logs_contract(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(
                action="target_logs.read",
                context="sellyouroutboard-testing",
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        route = openapi["paths"]["/v1/contexts/{context}/instances/{instance}/logs"]["get"]
        self.assertEqual(route["operationId"], "read_tracked_target_logs")
        self.assertEqual(
            route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/TrackedTargetLogsResponse",
        )
        self.assertIn("LaunchplaneErrorResponse", json.dumps(route))
        self.assertIn("400", route["responses"])
        self.assertIn("401", route["responses"])
        self.assertIn("403", route["responses"])
        self.assertIn("503", route["responses"])
        self.assertEqual(
            openapi["components"]["schemas"]["TrackedTargetLogsResponse"]["additionalProperties"],
            False,
        )

    async def test_fastapi_tracked_target_logs_precedes_legacy_wsgi_fallback(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _seed_tracked_target_records(
                database_url=database_url,
                context="sellyouroutboard-testing",
                instance="testing",
                target_id="app-123",
                target_type="application",
                target_name="syo-testing-app",
            )
            app_store = PostgresRecordStore(database_url=database_url)
            with (
                patch(
                    "control_plane.tracked_target_logs.control_plane_dokploy.read_dokploy_config",
                    return_value=("https://dokploy.example.com", "secret-token"),
                ),
                patch(
                    "control_plane.tracked_target_logs.control_plane_dokploy.fetch_dokploy_target_payload",
                    return_value={"appName": "syo-testing-gfbiqh", "serverId": "server-1"},
                ),
                patch(
                    "control_plane.tracked_target_logs.control_plane_dokploy.fetch_dokploy_application_logs",
                    return_value=("contact form submitted",),
                ),
            ):
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_record_read_policy(
                        action="target_logs.read",
                        context="sellyouroutboard-testing",
                    ),
                    record_store_factory=lambda: app_store,
                    control_plane_root_path=root,
                )
                legacy_app = create_launchplane_service_app(
                    state_dir=root / "state",
                    verifier=_StubVerifier(_identity()),
                    authz_policy=LaunchplaneAuthzPolicy.model_validate({}),
                    control_plane_root_path=root,
                )
                app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

                response = await _get_tracked_target_logs(
                    app,
                    "sellyouroutboard-testing",
                    "testing",
                    lines="2",
                )
                app_store.close()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")


class FastApiEdgeEndpointReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_edge_endpoint_read_returns_record_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            store.write_edge_endpoint_record(_edge_endpoint_record())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_record_read_policy(
                    action="edge_endpoint.read",
                    context="launchplane",
                ),
                record_store_factory=lambda: store,
            )

            response = await _get_edge_endpoint_record(app, "cm-prod-dokploy")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["record"]["endpoint_key"], "cm-prod-dokploy")
        self.assertEqual(payload["record"]["server_name"], "docker-cm-prod")
        self.assertEqual(payload["record"]["upstream_host"], "100.73.170.113")

    async def test_edge_endpoint_list_filters_records(self) -> None:
        active_record = _edge_endpoint_record()
        disabled_record = active_record.model_copy(
            update={
                "endpoint_key": "disabled-edge",
                "server_name": "docker-disabled",
                "upstream_host": "100.73.170.114",
                "status": "disabled",
            }
        )
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            store.write_edge_endpoint_record(active_record)
            store.write_edge_endpoint_record(disabled_record)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_record_read_policy(
                    action="edge_endpoint.read",
                    context="launchplane",
                ),
                record_store_factory=lambda: store,
            )

            response = await _get_edge_endpoint_records(
                app,
                provider="dokploy",
                status="active",
                limit="1",
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["limit"], 1)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["records"][0]["endpoint_key"], "cm-prod-dokploy")

    async def test_edge_endpoint_reads_require_identity(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(action="edge_endpoint.read", context="launchplane"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _get_edge_endpoint_records(app, authorization="")

        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authentication_required")

    async def test_edge_endpoint_reads_require_authz_action(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(action="driver.read", context="launchplane"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _get_edge_endpoint_record(app, "cm-prod-dokploy")

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    async def test_edge_endpoint_reads_require_record_storage(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(action="edge_endpoint.read", context="launchplane"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _get_edge_endpoint_records(app)

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "database_storage_required")

    async def test_edge_endpoint_read_returns_not_found_for_missing_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_record_read_policy(
                    action="edge_endpoint.read",
                    context="launchplane",
                ),
                record_store_factory=lambda: store,
            )

            response = await _get_edge_endpoint_record(app, "missing-edge")

        self.assertEqual(response.status_code, 404)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "not_found")

    async def test_edge_endpoint_list_validates_limit(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(action="edge_endpoint.read", context="launchplane"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        low_response = await _get_edge_endpoint_records(app, limit="0")
        high_response = await _get_edge_endpoint_records(app, limit="101")

        self.assertEqual(low_response.status_code, 400)
        self.assertEqual(high_response.status_code, 400)
        self.assertEqual(low_response.json()["error"]["code"], "invalid_query")
        self.assertEqual(high_response.json()["error"]["code"], "invalid_query")

    async def test_openapi_includes_edge_endpoint_read_contracts(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(action="edge_endpoint.read", context="launchplane"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        list_route = openapi["paths"]["/v1/edge-endpoints/records"]["get"]
        read_route = openapi["paths"]["/v1/edge-endpoints/records/{endpoint_key}"]["get"]
        self.assertEqual(list_route["operationId"], "list_edge_endpoint_records")
        self.assertEqual(read_route["operationId"], "read_edge_endpoint_record")
        self.assertEqual(
            list_route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/EdgeEndpointRecordsResponse",
        )
        self.assertEqual(
            read_route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/EdgeEndpointRecordResponse",
        )
        self.assertIn("LaunchplaneErrorResponse", json.dumps(list_route))
        self.assertIn("LaunchplaneErrorResponse", json.dumps(read_route))
        self.assertEqual(
            openapi["components"]["schemas"]["EdgeEndpointRecordResponse"]["additionalProperties"],
            False,
        )
        self.assertEqual(
            openapi["components"]["schemas"]["EdgeEndpointRecordsResponse"]["additionalProperties"],
            False,
        )

    async def test_fastapi_edge_endpoint_reads_precede_legacy_wsgi_fallback(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_edge_endpoint_record(_edge_endpoint_record())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_record_read_policy(
                    action="edge_endpoint.read",
                    context="launchplane",
                ),
                record_store_factory=lambda: store,
            )
            legacy_app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({}),
                control_plane_root_path=root,
            )
            app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

            response = await _get_edge_endpoint_record(app, "cm-prod-dokploy")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")


class FastApiPrivateHealthEndpointReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_health_endpoint_read_returns_record_for_authorized_workflow(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            store.write_private_health_endpoint_record(_private_health_endpoint_record())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_private_health_endpoint_read_policy(),
                record_store_factory=lambda: store,
            )

            response = await _get_private_health_endpoint_record(
                app,
                "repairshopr-sync-prod-runtime",
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["record"]["endpoint_key"], "repairshopr-sync-prod-runtime")
        self.assertEqual(payload["record"]["product"], "repairshopr-sync")
        self.assertEqual(payload["record"]["url"], "http://10.0.0.5:8000/health")

    async def test_private_health_endpoint_list_filters_records(self) -> None:
        active_record = _private_health_endpoint_record()
        disabled_record = active_record.model_copy(
            update={
                "endpoint_key": "repairshopr-sync-disabled-runtime",
                "instance": "disabled",
                "url": "http://10.0.0.6:8000/health",
                "status": "disabled",
            }
        )
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            store.write_private_health_endpoint_record(active_record)
            store.write_private_health_endpoint_record(disabled_record)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_private_health_endpoint_read_policy(),
                record_store_factory=lambda: store,
            )

            response = await _get_private_health_endpoint_records(
                app,
                instance="prod",
                status="active",
                limit="1",
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["product"], "repairshopr-sync")
        self.assertEqual(payload["context"], "repairshopr-sync")
        self.assertEqual(payload["instance"], "prod")
        self.assertEqual(payload["limit"], 1)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["records"][0]["endpoint_key"], "repairshopr-sync-prod-runtime")

    async def test_private_health_endpoint_reads_require_identity(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_private_health_endpoint_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _get_private_health_endpoint_records(app, authorization="")

        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authentication_required")

    async def test_private_health_endpoint_reads_require_product_and_context(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_private_health_endpoint_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        missing_product = await _get_private_health_endpoint_records(app, product="")
        missing_context = await _get_private_health_endpoint_record(
            app,
            "repairshopr-sync-prod-runtime",
            context="",
        )

        self.assertEqual(missing_product.status_code, 400)
        self.assertEqual(missing_context.status_code, 400)
        self.assertEqual(missing_product.json()["error"]["code"], "invalid_query")
        self.assertEqual(missing_context.json()["error"]["code"], "invalid_query")

    async def test_private_health_endpoint_reads_require_authz_action(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(
                action="edge_endpoint.read",
                context="repairshopr-sync",
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _get_private_health_endpoint_record(
            app,
            "repairshopr-sync-prod-runtime",
        )

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    async def test_private_health_endpoint_reads_require_record_storage(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_private_health_endpoint_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _get_private_health_endpoint_records(app)

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "database_storage_required")

    async def test_private_health_endpoint_read_returns_not_found_for_missing_record(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_private_health_endpoint_read_policy(),
                record_store_factory=lambda: store,
            )

            response = await _get_private_health_endpoint_record(app, "missing-runtime")

        self.assertEqual(response.status_code, 404)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "not_found")

    async def test_private_health_endpoint_read_returns_not_found_outside_scope(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            store.write_private_health_endpoint_record(_private_health_endpoint_record())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_private_health_endpoint_read_policy(),
                record_store_factory=lambda: store,
            )

            response = await _get_private_health_endpoint_record(
                app,
                "repairshopr-sync-prod-runtime",
                instance="preview",
            )

        self.assertEqual(response.status_code, 404)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "not_found")

    async def test_private_health_endpoint_list_validates_limit(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_private_health_endpoint_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        low_response = await _get_private_health_endpoint_records(app, limit="0")
        high_response = await _get_private_health_endpoint_records(app, limit="101")

        self.assertEqual(low_response.status_code, 400)
        self.assertEqual(high_response.status_code, 400)
        self.assertEqual(low_response.json()["error"]["code"], "invalid_query")
        self.assertEqual(high_response.json()["error"]["code"], "invalid_query")

    async def test_openapi_includes_private_health_endpoint_read_contracts(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_private_health_endpoint_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        list_route = openapi["paths"]["/v1/private-health-endpoints/records"]["get"]
        read_route = openapi["paths"]["/v1/private-health-endpoints/records/{endpoint_key}"]["get"]
        self.assertEqual(list_route["operationId"], "list_private_health_endpoint_records")
        self.assertEqual(read_route["operationId"], "read_private_health_endpoint_record")
        self.assertEqual(
            list_route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/PrivateHealthEndpointRecordsResponse",
        )
        self.assertEqual(
            read_route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/PrivateHealthEndpointRecordResponse",
        )
        self.assertIn("LaunchplaneErrorResponse", json.dumps(list_route))
        self.assertIn("LaunchplaneErrorResponse", json.dumps(read_route))
        self.assertEqual(
            openapi["components"]["schemas"]["PrivateHealthEndpointRecordResponse"][
                "additionalProperties"
            ],
            False,
        )
        self.assertEqual(
            openapi["components"]["schemas"]["PrivateHealthEndpointRecordsResponse"][
                "additionalProperties"
            ],
            False,
        )

    async def test_fastapi_private_health_endpoint_reads_precede_legacy_wsgi_fallback(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_private_health_endpoint_record(_private_health_endpoint_record())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_private_health_endpoint_read_policy(),
                record_store_factory=lambda: store,
            )
            legacy_app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({}),
                control_plane_root_path=root,
            )
            app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

            response = await _get_private_health_endpoint_record(
                app,
                "repairshopr-sync-prod-runtime",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")


class FastApiEndpointApplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_edge_endpoint_apply_writes_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_notification_policy_apply_policy(
                    action="edge_endpoint.apply",
                    product="launchplane",
                    context="launchplane",
                ),
                record_store_factory=lambda: store,
            )

            response = await _asgi_request(
                app,
                "POST",
                "/v1/edge-endpoints/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "edge-endpoint-apply-test",
                },
                payload=_edge_endpoint_apply_payload(mode="apply"),
            )
            stored_record = store.read_edge_endpoint_record("cm-prod-dokploy")

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(payload["records"]["edge_endpoint_key"], "cm-prod-dokploy")
        self.assertEqual(payload["records"]["edge_endpoint_status"], "applied")
        self.assertEqual(payload["result"]["mode"], "apply")
        self.assertEqual(payload["result"]["endpoint_status"], "applied")
        self.assertEqual(stored_record.server_name, "docker-cm-prod")

    async def test_edge_endpoint_dry_run_does_not_write_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_notification_policy_apply_policy(
                    action="edge_endpoint.apply",
                    product="launchplane",
                    context="launchplane",
                ),
                record_store_factory=lambda: store,
            )

            response = await _asgi_request(
                app,
                "POST",
                "/v1/edge-endpoints/apply",
                headers={"Authorization": "Bearer valid-token"},
                payload=_edge_endpoint_apply_payload(mode="dry-run"),
            )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["records"]["edge_endpoint_status"], "planned")
        self.assertEqual(payload["result"]["mode"], "dry-run")
        with self.assertRaises(FileNotFoundError):
            store.read_edge_endpoint_record("cm-prod-dokploy")

    async def test_edge_endpoint_apply_requires_idempotency_key_before_store_gate(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_notification_policy_apply_policy(
                action="edge_endpoint.apply",
                product="launchplane",
                context="launchplane",
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_request(
            app,
            "POST",
            "/v1/edge-endpoints/apply",
            headers={"Authorization": "Bearer valid-token"},
            payload=_edge_endpoint_apply_payload(mode="apply"),
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "idempotency_key_required")

    async def test_edge_endpoint_apply_replays_and_rejects_conflicting_idempotency_key(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_notification_policy_apply_policy(
                    action="edge_endpoint.apply",
                    product="launchplane",
                    context="launchplane",
                ),
                record_store_factory=lambda: store,
            )
            payload = _edge_endpoint_apply_payload(mode="apply")
            conflicting_payload = _edge_endpoint_apply_payload(mode="apply")
            endpoint = cast(dict[str, object], conflicting_payload["endpoint"])
            endpoint["upstream_port"] = 8443

            first_response = await _asgi_request(
                app,
                "POST",
                "/v1/edge-endpoints/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "edge-endpoint-replay-test",
                },
                payload=payload,
            )
            replay_response = await _asgi_request(
                app,
                "POST",
                "/v1/edge-endpoints/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "edge-endpoint-replay-test",
                },
                payload=payload,
            )
            conflict_response = await _asgi_request(
                app,
                "POST",
                "/v1/edge-endpoints/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "edge-endpoint-replay-test",
                },
                payload=conflicting_payload,
            )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(replay_response.status_code, 202)
        first_payload = first_response.json()
        replay_payload = replay_response.json()
        self.assertTrue(replay_payload["replayed"])
        self.assertEqual(replay_payload["original_trace_id"], first_payload["trace_id"])
        self.assertEqual(replay_payload["records"], first_payload["records"])
        self.assertEqual(conflict_response.status_code, 409)
        self.assertEqual(conflict_response.json()["error"]["code"], "idempotency_key_reused")

    async def test_private_health_endpoint_apply_writes_record_with_legacy_records_shape(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_notification_policy_apply_policy(
                    action="private_health_endpoint.apply",
                    product="repairshopr-sync",
                    context="repairshopr-sync",
                ),
                record_store_factory=lambda: store,
            )

            response = await _asgi_request(
                app,
                "POST",
                "/v1/private-health-endpoints/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "private-health-endpoint-apply-test",
                },
                payload=_private_health_endpoint_apply_payload(mode="apply"),
            )
            stored_record = store.read_private_health_endpoint_record(
                "repairshopr-sync-prod-runtime"
            )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["records"], {})
        self.assertEqual(payload["result"]["endpoint_key"], "repairshopr-sync-prod-runtime")
        self.assertEqual(payload["result"]["endpoint_status"], "applied")
        self.assertEqual(stored_record.url, "http://10.0.0.5:8000/health")

    async def test_private_health_endpoint_dry_run_does_not_write_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_notification_policy_apply_policy(
                    action="private_health_endpoint.apply",
                    product="repairshopr-sync",
                    context="repairshopr-sync",
                ),
                record_store_factory=lambda: store,
            )

            response = await _asgi_request(
                app,
                "POST",
                "/v1/private-health-endpoints/apply",
                headers={"Authorization": "Bearer valid-token"},
                payload=_private_health_endpoint_apply_payload(mode="dry-run"),
            )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["records"], {})
        self.assertEqual(payload["result"]["mode"], "dry-run")
        self.assertEqual(payload["result"]["endpoint_status"], "planned")
        with self.assertRaises(FileNotFoundError):
            store.read_private_health_endpoint_record("repairshopr-sync-prod-runtime")

    async def test_private_health_endpoint_apply_requires_idempotency_key_before_store_gate(
        self,
    ) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_notification_policy_apply_policy(
                action="private_health_endpoint.apply",
                product="repairshopr-sync",
                context="repairshopr-sync",
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_request(
            app,
            "POST",
            "/v1/private-health-endpoints/apply",
            headers={"Authorization": "Bearer valid-token"},
            payload=_private_health_endpoint_apply_payload(mode="apply"),
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "idempotency_key_required")

    async def test_private_health_endpoint_apply_rejects_public_url(self) -> None:
        payload = _private_health_endpoint_apply_payload(mode="apply")
        endpoint = cast(dict[str, object], payload["endpoint"])
        endpoint["url"] = "https://repairshopr-sync.example.test/health"
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_notification_policy_apply_policy(
                    action="private_health_endpoint.apply",
                    product="repairshopr-sync",
                    context="repairshopr-sync",
                ),
                record_store_factory=lambda: store,
            )

            response = await _asgi_request(
                app,
                "POST",
                "/v1/private-health-endpoints/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "private-health-public-url-test",
                },
                payload=payload,
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")

    async def test_private_health_endpoint_apply_rejects_cross_scope_overwrite(self) -> None:
        existing_record = _private_health_endpoint_record().model_copy(
            update={
                "endpoint_key": "shared-runtime",
                "product": "other-product",
                "context": "other-product",
            }
        )
        payload = _private_health_endpoint_apply_payload(mode="apply")
        endpoint = cast(dict[str, object], payload["endpoint"])
        endpoint["endpoint_key"] = "shared-runtime"
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            store.write_private_health_endpoint_record(existing_record)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_notification_policy_apply_policy(
                    action="private_health_endpoint.apply",
                    product="repairshopr-sync",
                    context="repairshopr-sync",
                ),
                record_store_factory=lambda: store,
            )

            response = await _asgi_request(
                app,
                "POST",
                "/v1/private-health-endpoints/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "private-health-cross-scope-test",
                },
                payload=payload,
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "conflicting_private_health_endpoint")

    async def test_private_health_endpoint_apply_replays_and_rejects_conflicting_idempotency_key(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_notification_policy_apply_policy(
                    action="private_health_endpoint.apply",
                    product="repairshopr-sync",
                    context="repairshopr-sync",
                ),
                record_store_factory=lambda: store,
            )
            payload = _private_health_endpoint_apply_payload(mode="apply")
            conflicting_payload = _private_health_endpoint_apply_payload(mode="apply")
            endpoint = cast(dict[str, object], conflicting_payload["endpoint"])
            endpoint["url"] = "http://10.0.0.6:8000/health"

            first_response = await _asgi_request(
                app,
                "POST",
                "/v1/private-health-endpoints/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "private-health-replay-test",
                },
                payload=payload,
            )
            replay_response = await _asgi_request(
                app,
                "POST",
                "/v1/private-health-endpoints/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "private-health-replay-test",
                },
                payload=payload,
            )
            conflict_response = await _asgi_request(
                app,
                "POST",
                "/v1/private-health-endpoints/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "private-health-replay-test",
                },
                payload=conflicting_payload,
            )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(replay_response.status_code, 202)
        first_payload = first_response.json()
        replay_payload = replay_response.json()
        self.assertTrue(replay_payload["replayed"])
        self.assertEqual(replay_payload["original_trace_id"], first_payload["trace_id"])
        self.assertEqual(replay_payload["result"], first_payload["result"])
        self.assertEqual(conflict_response.status_code, 409)
        self.assertEqual(conflict_response.json()["error"]["code"], "idempotency_key_reused")

    async def test_endpoint_apply_routes_require_exact_authz_scope(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            edge_app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_notification_policy_apply_policy(
                    action="edge_endpoint.apply",
                    product="repairshopr-sync",
                    context="repairshopr-sync",
                ),
                record_store_factory=lambda: store,
            )
            private_app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_notification_policy_apply_policy(
                    action="private_health_endpoint.apply",
                    product="launchplane",
                    context="launchplane",
                ),
                record_store_factory=lambda: store,
            )

            edge_response = await _asgi_request(
                edge_app,
                "POST",
                "/v1/edge-endpoints/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "edge-authz-test",
                },
                payload=_edge_endpoint_apply_payload(mode="apply"),
            )
            private_response = await _asgi_request(
                private_app,
                "POST",
                "/v1/private-health-endpoints/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "private-health-authz-test",
                },
                payload=_private_health_endpoint_apply_payload(mode="apply"),
            )

        self.assertEqual(edge_response.status_code, 403)
        self.assertEqual(private_response.status_code, 403)
        self.assertEqual(edge_response.json()["error"]["code"], "authorization_denied")
        self.assertEqual(private_response.json()["error"]["code"], "authorization_denied")

    async def test_endpoint_apply_routes_precede_legacy_wsgi_fallback(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate(
                    {
                        "github_actions": [
                            {
                                "repository": "every/verireel",
                                "workflow_refs": [
                                    "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                                ],
                                "event_names": ["pull_request"],
                                "products": ["launchplane", "repairshopr-sync"],
                                "contexts": ["launchplane", "repairshopr-sync"],
                                "actions": [
                                    "edge_endpoint.apply",
                                    "private_health_endpoint.apply",
                                ],
                            }
                        ]
                    }
                ),
                record_store_factory=lambda: store,
            )
            legacy_app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=root,
                local_record_store_for_tests=store,
            )
            app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

            edge_response = await _asgi_request(
                app,
                "POST",
                "/v1/edge-endpoints/apply",
                headers={"Authorization": "Bearer valid-token"},
                payload=_edge_endpoint_apply_payload(mode="dry-run"),
            )
            private_response = await _asgi_request(
                app,
                "POST",
                "/v1/private-health-endpoints/apply",
                headers={"Authorization": "Bearer valid-token"},
                payload=_private_health_endpoint_apply_payload(mode="dry-run"),
            )

        self.assertEqual(edge_response.status_code, 202)
        self.assertEqual(private_response.status_code, 202)
        self.assertEqual(edge_response.json()["result"]["mode"], "dry-run")
        self.assertEqual(private_response.json()["result"]["mode"], "dry-run")

    async def test_openapi_includes_endpoint_apply_routes(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        edge_route = openapi["paths"]["/v1/edge-endpoints/apply"]["post"]
        private_route = openapi["paths"]["/v1/private-health-endpoints/apply"]["post"]
        self.assertEqual(edge_route["operationId"], "apply_edge_endpoint")
        self.assertEqual(private_route["operationId"], "apply_private_health_endpoint")
        self.assertEqual(
            edge_route["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/AcceptedEvidenceResponse",
        )
        self.assertEqual(
            private_route["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/AcceptedEvidenceResponse",
        )


class FastApiPreviewDesiredStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_desired_state_discovers_and_records_labeled_prs(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_notification_policy_apply_policy(
                    action="preview_desired_state.discover",
                    product="verireel",
                    context="verireel-testing",
                ),
                control_plane_root_path=root,
                record_store_factory=lambda: store,
            )
            record = PreviewDesiredStateRecord(
                desired_state_id="preview-desired-state-verireel-testing-20260429T213000Z",
                product="verireel",
                context="verireel-testing",
                source="launchplane-preview-lifecycle",
                discovered_at="2026-04-29T21:30:00Z",
                repository="every/verireel",
                label="preview",
                anchor_repo="verireel",
                status="pass",
                desired_count=1,
                desired_previews=(
                    PreviewLifecycleDesiredPreview(
                        preview_slug="pr-42",
                        anchor_repo="verireel",
                        anchor_pr_number=42,
                        anchor_pr_url="https://github.com/every/verireel/pull/42",
                        head_sha="abc1234",
                    ),
                ),
            )

            with patch(
                "control_plane.http_app.discover_github_preview_desired_state",
                return_value=record,
            ) as discover:
                first_response = await _post_preview_desired_state(
                    app,
                    _preview_desired_state_payload(),
                    idempotency_key="preview-desired-state:verireel-testing",
                )
                replay_response = await _post_preview_desired_state(
                    app,
                    _preview_desired_state_payload(),
                    idempotency_key="preview-desired-state:verireel-testing",
                )
            records = store.list_preview_desired_state_records(context_name="verireel-testing")

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(replay_response.status_code, 202)
        payload = first_response.json()
        self.assertEqual(
            payload["records"]["preview_desired_state_id"],
            "preview-desired-state-verireel-testing-20260429T213000Z",
        )
        self.assertEqual(payload["result"]["desired_previews"][0]["preview_slug"], "pr-42")
        self.assertTrue(replay_response.json()["replayed"])
        self.assertEqual(len(records), 1)
        discover.assert_called_once()

    async def test_preview_desired_state_rejects_unauthorized_workflow(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_notification_policy_apply_policy(
                action="preview_lifecycle.plan",
                product="verireel",
                context="verireel-testing",
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_preview_desired_state(app, _preview_desired_state_payload())

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_generic_web_preview_desired_state_uses_profile_context(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_generic_web_preview_desired_state_identity()),
                authz_policy=_generic_web_preview_desired_state_policy(
                    context="sellyouroutboard-testing"
                ),
                control_plane_root_path=root,
                record_store_factory=lambda: store,
            )
            record = PreviewDesiredStateRecord(
                desired_state_id="preview-desired-state-syo-testing-1",
                product="sellyouroutboard",
                context="sellyouroutboard-testing",
                source="generic-web-preview",
                discovered_at="2026-04-30T21:00:00Z",
                repository="cbusillo/sellyouroutboard",
                label="preview",
                anchor_repo="sellyouroutboard",
                status="pass",
                desired_count=0,
            )

            with patch(
                "control_plane.http_app.discover_generic_web_preview_desired_state",
                return_value=record,
            ) as discover:
                response = await _post_generic_web_preview_desired_state(
                    app,
                    _generic_web_preview_desired_state_payload(),
                    idempotency_key="generic-web-preview-desired-state:syo",
                )
            records = store.list_preview_desired_state_records(
                context_name="sellyouroutboard-testing"
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            response.json()["records"]["preview_desired_state_id"],
            "preview-desired-state-syo-testing-1",
        )
        discover.assert_called_once()
        _, kwargs = discover.call_args
        self.assertEqual(kwargs["profile"].preview.context, "sellyouroutboard-testing")
        self.assertEqual(records[0].desired_state_id, "preview-desired-state-syo-testing-1")

    async def test_generic_web_preview_desired_state_rejects_wrong_context(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_generic_web_preview_desired_state_identity()),
                authz_policy=_generic_web_preview_desired_state_policy(context="different-context"),
                control_plane_root_path=root,
                record_store_factory=lambda: store,
            )

            response = await _post_generic_web_preview_desired_state(
                app,
                _generic_web_preview_desired_state_payload(),
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_generic_web_preview_desired_state_keeps_profile_errors_invalid_request(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            profile_payload = _product_profile_payload()
            profile_payload["preview"] = {
                "enabled": False,
                "context": "sellyouroutboard-testing",
                "slug_template": "pr-{number}",
            }
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(profile_payload)
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_generic_web_preview_desired_state_identity()),
                authz_policy=_generic_web_preview_desired_state_policy(
                    context="sellyouroutboard-testing"
                ),
                control_plane_root_path=root,
                record_store_factory=lambda: store,
            )

            with patch(
                "control_plane.http_app.discover_generic_web_preview_desired_state"
            ) as discover:
                response = await _post_generic_web_preview_desired_state(
                    app,
                    _generic_web_preview_desired_state_payload(),
                )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")
        discover.assert_not_called()

    async def test_openapi_includes_preview_desired_state_routes(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        self.assertEqual(
            openapi["paths"]["/v1/previews/desired-state"]["post"]["operationId"],
            "apply_preview_desired_state",
        )
        self.assertEqual(
            openapi["paths"]["/v1/drivers/generic-web/preview-desired-state"]["post"][
                "operationId"
            ],
            "apply_generic_web_preview_desired_state",
        )


class FastApiPreviewPrFeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_pr_feedback_records_skipped_delivery_without_token(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_preview_pr_feedback_identity()),
                authz_policy=_preview_pr_feedback_policy(action="preview_pr_feedback.write"),
                control_plane_root_path=root,
                record_store_factory=lambda: store,
            )

            response = await _post_preview_pr_feedback(app, _preview_pr_feedback_payload())
            feedback_records = store.list_preview_pr_feedback_records(
                context_name="verireel-testing"
            )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(
            payload["records"]["preview_pr_feedback_id"], feedback_records[0].feedback_id
        )
        self.assertEqual(payload["result"]["delivery_status"], "skipped")
        self.assertIn("Launchplane preview is ready", payload["result"]["comment_markdown"])
        self.assertIn("GITHUB_TOKEN", feedback_records[0].error_message)

    async def test_preview_pr_feedback_replays_idempotent_notification(self) -> None:
        sent_payloads: list[tuple[str, dict[str, object]]] = []

        def send_discord(webhook_url: str, payload: dict[str, object]) -> None:
            sent_payloads.append((webhook_url, payload))

        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
                clear=True,
            ),
        ):
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_preview_pr_feedback_identity()),
                authz_policy=_preview_pr_feedback_policy(action="preview_pr_feedback.write"),
                control_plane_root_path=root,
                record_store_factory=lambda: store,
                preview_pr_feedback_discord_sender=send_discord,
            )
            try:
                secret_result = control_plane_secrets.write_secret_value(
                    record_store=store,
                    scope="context_instance",
                    integration="preview-pr-feedback-notifications",
                    name="discord webhook",
                    plaintext_value="https://discord.com/api/webhooks/test/webhook",
                    binding_key="DISCORD_WEBHOOK",
                    context_name="launchplane",
                    instance_name="preview-feedback",
                    actor="test",
                    source_label="test",
                )
                store.write_preview_pr_feedback_notification_policy_record(
                    _preview_pr_feedback_notification_policy_record(
                        policy_id="preview-pr-feedback-notification-discord",
                        product="verireel",
                        context="verireel-testing",
                        repository="every/verireel",
                    ).model_copy(
                        update={
                            "destinations": (
                                PreviewPrFeedbackNotificationDestination(
                                    destination_id="discord",
                                    kind="discord",
                                    discord_webhook_secret=str(secret_result["secret_id"]),
                                ),
                            )
                        }
                    )
                )
                first_response = await _post_preview_pr_feedback(
                    app,
                    _preview_pr_feedback_payload(),
                    idempotency_key="preview-pr-feedback:verireel:42",
                )
                replay_response = await _post_preview_pr_feedback(
                    app,
                    _preview_pr_feedback_payload(),
                    idempotency_key="preview-pr-feedback:verireel:42",
                )
            finally:
                store.close()

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(replay_response.status_code, 202)
        self.assertTrue(replay_response.json()["replayed"])
        self.assertEqual(len(sent_payloads), 1)
        first_payload = first_response.json()
        self.assertEqual(
            first_payload["result"]["notifications"][0]["delivery_status"], "delivered"
        )
        sent_embeds = cast(list[dict[str, object]], sent_payloads[0][1]["embeds"])
        sent_embed = sent_embeds[0]
        sent_embed_fields = cast(list[dict[str, str]], sent_embed["fields"])
        sent_fields = {field["name"]: field["value"] for field in sent_embed_fields}
        self.assertEqual(sent_fields["Pull request"], "https://github.com/every/verireel/pull/42")
        self.assertEqual(
            sent_fields["Workflow"], "https://github.com/every/verireel/actions/runs/123"
        )

    async def test_preview_pr_feedback_dry_run_authorizes_without_writing(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_preview_pr_feedback_identity()),
                authz_policy=_preview_pr_feedback_policy(action="preview_pr_feedback.write"),
                record_store_factory=lambda: store,
            )

            response = await _post_preview_pr_feedback(
                app,
                _preview_pr_feedback_payload(dry_run=True),
            )
            feedback_records = store.list_preview_pr_feedback_records(
                context_name="verireel-testing"
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["result"]["preview_pr_feedback"], "authorized")
        self.assertEqual(feedback_records, ())

    async def test_preview_pr_feedback_accepts_lifecycle_refresh_grant(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_preview_pr_feedback_identity()),
                authz_policy=_preview_pr_feedback_policy(action="preview_refresh.execute"),
                control_plane_root_path=root,
                record_store_factory=lambda: store,
            )

            response = await _post_preview_pr_feedback(app, _preview_pr_feedback_payload())
            feedback_records = store.list_preview_pr_feedback_records(
                context_name="verireel-testing"
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(feedback_records[0].status, "ready")

    async def test_preview_pr_feedback_rejects_unauthorized_workflow(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_preview_pr_feedback_identity()),
            authz_policy=_preview_pr_feedback_policy(action="preview_generation.write"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_preview_pr_feedback(app, _preview_pr_feedback_payload())

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_openapi_includes_preview_pr_feedback_route(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        route = response.json()["paths"]["/v1/previews/pr-feedback"]["post"]
        self.assertEqual(route["operationId"], "apply_preview_pr_feedback")
        self.assertEqual(
            route["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/AcceptedEvidenceResponse",
        )


class FastApiIngressRouteApplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_ingress_route_dry_run_returns_plan_without_mutation(self) -> None:
        client = _FakeNpmplusIngressClient()
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_notification_policy_apply_policy(
                    action="ingress_route.plan",
                    product="launchplane",
                    context="reon-prod",
                ),
                record_store_factory=lambda: store,
                npmplus_ingress_client_factory=lambda: client,
            )

            response = await _asgi_request(
                app,
                "POST",
                "/v1/drivers/ingress/route-apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "npmplus-ingress-dry-run",
                },
                payload=_npmplus_ingress_route_payload(),
            )
            records = store.list_ingress_route_audit_records(
                product="launchplane", context_name="reon-prod"
            )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["result"]["status"], "planned")
        self.assertTrue(payload["result"]["dry_run"])
        self.assertEqual(payload["result"]["operations"][0]["action"], "create")
        self.assertEqual(client.calls, ["list"])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].mode, "dry-run")
        self.assertEqual(records[0].status, "planned")
        self.assertEqual(records[0].trace_id, payload["trace_id"])

    async def test_ingress_route_dry_run_idempotency_key_does_not_replay(self) -> None:
        client = _FakeNpmplusIngressClient()
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_notification_policy_apply_policy(
                    action="ingress_route.plan",
                    product="launchplane",
                    context="reon-prod",
                ),
                record_store_factory=lambda: store,
                npmplus_ingress_client_factory=lambda: client,
            )

            responses = [
                await _asgi_request(
                    app,
                    "POST",
                    "/v1/drivers/ingress/route-apply",
                    headers={
                        "Authorization": "Bearer valid-token",
                        "Idempotency-Key": "npmplus-ingress-dry-run",
                    },
                    payload=_npmplus_ingress_route_payload(),
                )
                for _ in range(2)
            ]

        self.assertEqual([response.status_code for response in responses], [202, 202])
        self.assertEqual(client.calls, ["list", "list"])
        self.assertNotIn("replayed", responses[1].json())

    async def test_ingress_route_apply_mutates_when_authorized(self) -> None:
        client = _FakeNpmplusIngressClient()
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_notification_policy_apply_policy(
                    action="ingress_route.apply",
                    product="launchplane",
                    context="reon-prod",
                ),
                record_store_factory=lambda: store,
                npmplus_ingress_client_factory=lambda: client,
            )

            response = await _asgi_request(
                app,
                "POST",
                "/v1/drivers/ingress/route-apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "npmplus-ingress-apply",
                },
                payload=_npmplus_ingress_route_payload(mode="apply"),
            )
            records = store.list_ingress_route_audit_records(
                product="launchplane", context_name="reon-prod"
            )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["result"]["status"], "applied")
        self.assertFalse(payload["result"]["dry_run"])
        self.assertEqual(payload["result"]["proxy_host"]["id"], 100)
        self.assertEqual(payload["records"]["ingress_route_audit_record_id"], records[-1].record_id)
        self.assertEqual(client.calls, ["list", "create"])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].mode, "apply")
        self.assertEqual(records[0].idempotency_key, "npmplus-ingress-apply")
        self.assertEqual(records[0].provider_host_id, 100)

    async def test_ingress_route_apply_replays_before_store_changes(self) -> None:
        client = _FakeNpmplusIngressClient()
        request_payload = _npmplus_ingress_route_payload(mode="apply")
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_notification_policy_apply_policy(
                    action="ingress_route.apply",
                    product="launchplane",
                    context="reon-prod",
                ),
                record_store_factory=lambda: store,
                npmplus_ingress_client_factory=lambda: client,
            )

            first_response = await _asgi_request(
                app,
                "POST",
                "/v1/drivers/ingress/route-apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "npmplus-ingress-apply-replay",
                },
                payload=request_payload,
            )
            replay_app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_notification_policy_apply_policy(
                    action="ingress_route.apply",
                    product="launchplane",
                    context="reon-prod",
                ),
                record_store_factory=lambda: store,
                npmplus_ingress_client_factory=lambda: _FakeNpmplusIngressClient(),
            )
            replay_response = await _asgi_request(
                replay_app,
                "POST",
                "/v1/drivers/ingress/route-apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "npmplus-ingress-apply-replay",
                },
                payload=request_payload,
            )
            records = store.list_ingress_route_audit_records(
                product="launchplane", context_name="reon-prod"
            )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(replay_response.status_code, 202)
        self.assertTrue(replay_response.json()["replayed"])
        self.assertEqual(
            replay_response.json()["original_trace_id"], first_response.json()["trace_id"]
        )
        self.assertEqual(client.calls, ["list", "create"])
        self.assertEqual(len(records), 1)

    async def test_ingress_route_apply_rejects_reused_idempotency_key_with_new_payload(
        self,
    ) -> None:
        client = _FakeNpmplusIngressClient()
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_notification_policy_apply_policy(
                    action="ingress_route.apply",
                    product="launchplane",
                    context="reon-prod",
                ),
                record_store_factory=lambda: store,
                npmplus_ingress_client_factory=lambda: client,
            )

            first_response = await _asgi_request(
                app,
                "POST",
                "/v1/drivers/ingress/route-apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "npmplus-ingress-conflict",
                },
                payload=_npmplus_ingress_route_payload(mode="apply"),
            )
            conflict_response = await _asgi_request(
                app,
                "POST",
                "/v1/drivers/ingress/route-apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "npmplus-ingress-conflict",
                },
                payload=_npmplus_ingress_route_payload(
                    mode="apply",
                    forward_host="192.0.2.11",
                ),
            )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(conflict_response.status_code, 409)
        self.assertEqual(conflict_response.json()["error"]["code"], "idempotency_key_reused")
        self.assertEqual(client.calls, ["list", "create"])

    async def test_ingress_route_resolves_edge_endpoint_key_to_ip(self) -> None:
        client = _FakeNpmplusIngressClient((_npmplus_proxy_host(),))
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            store.write_edge_endpoint_record(_edge_endpoint_record())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_notification_policy_apply_policy(
                    action="ingress_route.plan",
                    product="launchplane",
                    context="reon-prod",
                ),
                record_store_factory=lambda: store,
                npmplus_ingress_client_factory=lambda: client,
            )

            response = await _asgi_request(
                app,
                "POST",
                "/v1/drivers/ingress/route-apply",
                headers={"Authorization": "Bearer valid-token"},
                payload=_npmplus_ingress_route_payload(
                    edge_endpoint_key="cm-prod-dokploy",
                    forward_host="",
                    forward_scheme="http",
                    forward_port=80,
                ),
            )
            records = store.list_ingress_route_audit_records(
                product="launchplane", context_name="reon-prod"
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["result"]["status"], "unchanged")
        self.assertEqual(client.calls, ["list"])
        self.assertEqual(records[0].edge_endpoint_key, "cm-prod-dokploy")

    async def test_ingress_route_rejects_missing_edge_endpoint_key(self) -> None:
        client = _FakeNpmplusIngressClient()
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_notification_policy_apply_policy(
                    action="ingress_route.plan",
                    product="launchplane",
                    context="reon-prod",
                ),
                record_store_factory=lambda: store,
                npmplus_ingress_client_factory=lambda: client,
            )

            response = await _asgi_request(
                app,
                "POST",
                "/v1/drivers/ingress/route-apply",
                headers={"Authorization": "Bearer valid-token"},
                payload=_npmplus_ingress_route_payload(
                    edge_endpoint_key="cm-prod-dokploy",
                    forward_host="",
                ),
            )
            records = store.list_ingress_route_audit_records(
                product="launchplane", context_name="reon-prod"
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_edge_endpoint")
        self.assertEqual(client.calls, [])
        self.assertEqual(records, ())

    async def test_ingress_route_rejects_disabled_edge_endpoint_key(self) -> None:
        client = _FakeNpmplusIngressClient()
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            store.write_edge_endpoint_record(_edge_endpoint_record(status="disabled"))
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_notification_policy_apply_policy(
                    action="ingress_route.plan",
                    product="launchplane",
                    context="reon-prod",
                ),
                record_store_factory=lambda: store,
                npmplus_ingress_client_factory=lambda: client,
            )

            response = await _asgi_request(
                app,
                "POST",
                "/v1/drivers/ingress/route-apply",
                headers={"Authorization": "Bearer valid-token"},
                payload=_npmplus_ingress_route_payload(
                    edge_endpoint_key="cm-prod-dokploy",
                    forward_host="",
                ),
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_edge_endpoint")
        self.assertEqual(client.calls, [])

    async def test_ingress_route_apply_uses_provider_adapter(self) -> None:
        provider = _FakeIngressProvider(
            NpmplusIngressApplyResult(
                status="planned",
                dry_run=True,
                operations=(
                    NpmplusIngressOperation(
                        action="create",
                        domain_names=("ingress-canary.example.test",),
                        requires_apply=True,
                        change_categories=("route",),
                    ),
                ),
                proxy_host=None,
            )
        )
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_notification_policy_apply_policy(
                    action="ingress_route.plan",
                    product="launchplane",
                    context="reon-prod",
                ),
                record_store_factory=lambda: store,
                ingress_provider_factory=lambda: provider,
            )

            response = await _asgi_request(
                app,
                "POST",
                "/v1/drivers/ingress/route-apply",
                headers={"Authorization": "Bearer valid-token"},
                payload=_npmplus_ingress_route_payload(),
            )
            records = store.list_ingress_route_audit_records(
                product="launchplane", context_name="reon-prod"
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["records"]["ingress_provider"], "fake-ingress")
        self.assertEqual(response.json()["result"]["status"], "planned")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].provider, "fake-ingress")
        self.assertEqual(len(provider.requests), 1)

    async def test_ingress_route_apply_maps_provider_guard_to_invalid_request(self) -> None:
        class RejectingIngressProvider:
            provider_id = "npmplus"
            delegated_executor = "test"

            def apply_route(self, *, request: Any) -> Any:
                raise ClickException("Provider guard rejected ingress route apply.")

        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_notification_policy_apply_policy(
                    action="ingress_route.apply",
                    product="launchplane",
                    context="reon-prod",
                ),
                record_store_factory=lambda: store,
                ingress_provider_factory=RejectingIngressProvider,
            )

            response = await _asgi_request(
                app,
                "POST",
                "/v1/drivers/ingress/route-apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "npmplus-ingress-provider-reject",
                },
                payload=_npmplus_ingress_route_payload(mode="apply"),
            )
            records = store.list_ingress_route_audit_records(
                product="launchplane", context_name="reon-prod"
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")
        self.assertEqual(response.json()["error"]["message"], "Request could not be completed.")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].status, "pending")

    async def test_ingress_route_apply_requires_idempotency_key_before_store_gate(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_notification_policy_apply_policy(
                action="ingress_route.apply",
                product="launchplane",
                context="reon-prod",
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_request(
            app,
            "POST",
            "/v1/drivers/ingress/route-apply",
            headers={"Authorization": "Bearer valid-token"},
            payload=_npmplus_ingress_route_payload(mode="apply"),
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "idempotency_key_required")

    async def test_ingress_route_apply_rejects_human_session_mutation(self) -> None:
        client = _FakeNpmplusIngressClient()
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            oauth_config = _github_oauth_config()
            session_store = InMemoryHumanSessionStore()
            session_manager = HumanSessionManager(
                config=oauth_config,
                session_store=session_store,
            )
            human_session = session_manager.issue(_github_human_identity())
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_github_human_ingress_route_policy(
                    action="ingress_route.apply",
                    product="launchplane",
                    context="reon-prod",
                ),
                record_store_factory=lambda: store,
                human_session_manager=session_manager,
                npmplus_ingress_client_factory=lambda: client,
            )

            response = await _asgi_request(
                app,
                "POST",
                "/v1/drivers/ingress/route-apply",
                headers={
                    "Cookie": session_manager.session_cookie_header(human_session),
                    "Idempotency-Key": "npmplus-ingress-human-session",
                },
                payload=_npmplus_ingress_route_payload(mode="apply"),
            )
            records = store.list_ingress_route_audit_records(
                product="launchplane", context_name="reon-prod"
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "authentication_required")
        self.assertEqual(client.calls, [])
        self.assertEqual(records, ())

    async def test_ingress_route_apply_rejects_unauthorized_context(self) -> None:
        client = _FakeNpmplusIngressClient()
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_notification_policy_apply_policy(
                action="ingress_route.apply",
                product="launchplane",
                context="reon-prod",
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
            npmplus_ingress_client_factory=lambda: client,
        )

        response = await _asgi_request(
            app,
            "POST",
            "/v1/drivers/ingress/route-apply",
            headers={
                "Authorization": "Bearer valid-token",
                "Idempotency-Key": "npmplus-ingress-unauthorized",
            },
            payload=_npmplus_ingress_route_payload(mode="apply", context="cm-prod"),
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")
        self.assertEqual(client.calls, [])

    async def test_ingress_route_apply_routes_precede_legacy_wsgi_fallback(self) -> None:
        client = _FakeNpmplusIngressClient()
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_notification_policy_apply_policy(
                    action="ingress_route.plan",
                    product="launchplane",
                    context="reon-prod",
                ),
                record_store_factory=lambda: store,
                npmplus_ingress_client_factory=lambda: client,
            )
            legacy_app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=root,
                local_record_store_for_tests=store,
            )
            app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

            response = await _asgi_request(
                app,
                "POST",
                "/v1/drivers/ingress/route-apply",
                headers={"Authorization": "Bearer valid-token"},
                payload=_npmplus_ingress_route_payload(),
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["result"]["status"], "planned")

    async def test_openapi_includes_ingress_route_apply(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        route = response.json()["paths"]["/v1/drivers/ingress/route-apply"]["post"]
        self.assertEqual(route["operationId"], "apply_ingress_route")
        self.assertEqual(
            route["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/AcceptedEvidenceResponse",
        )


class FastApiIngressCanaryRouteApplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_ingress_canary_route_record_apply_writes_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_notification_policy_apply_policy(
                    action="ingress_canary_route.apply",
                    product="launchplane",
                    context="launchplane",
                ),
                record_store_factory=lambda: store,
            )

            response = await _asgi_request(
                app,
                "POST",
                "/v1/ingress/canary-routes/records/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "ingress-canary-record-apply",
                },
                payload=_ingress_canary_route_record_apply_payload(mode="apply"),
            )
            stored_record = store.read_ingress_canary_route_record("ingress-canary")

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["records"]["ingress_canary_route_key"], "ingress-canary")
        self.assertEqual(payload["records"]["ingress_canary_route_status"], "applied")
        self.assertEqual(payload["result"]["route_status"], "applied")
        self.assertNotIn("replayed", payload)
        self.assertNotIn("original_trace_id", payload)
        self.assertEqual(stored_record.domain_name, "ingress-canary.example.test")
        self.assertEqual(stored_record.edge_endpoint_key, "cm-prod-dokploy")

    async def test_ingress_canary_route_record_apply_replays_without_rewriting_record(
        self,
    ) -> None:
        request_payload = _ingress_canary_route_record_apply_payload(mode="apply")
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_notification_policy_apply_policy(
                    action="ingress_canary_route.apply",
                    product="launchplane",
                    context="launchplane",
                ),
                record_store_factory=lambda: store,
            )

            first_response = await _asgi_request(
                app,
                "POST",
                "/v1/ingress/canary-routes/records/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "ingress-canary-record-replay",
                },
                payload=request_payload,
            )
            store.write_ingress_canary_route_record(
                _ingress_canary_route_record().model_copy(
                    update={
                        "domain_name": "changed-canary.example.test",
                        "updated_at": "2026-06-11T01:00:00Z",
                    }
                )
            )
            replay_response = await _asgi_request(
                app,
                "POST",
                "/v1/ingress/canary-routes/records/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "ingress-canary-record-replay",
                },
                payload=request_payload,
            )
            stored_record = store.read_ingress_canary_route_record("ingress-canary")

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(replay_response.status_code, 202)
        first_payload = first_response.json()
        replay_payload = replay_response.json()
        self.assertTrue(replay_payload["replayed"])
        self.assertEqual(replay_payload["original_trace_id"], first_payload["trace_id"])
        self.assertEqual(replay_payload["records"], first_payload["records"])
        self.assertEqual(stored_record.domain_name, "changed-canary.example.test")

    async def test_ingress_canary_route_record_dry_run_does_not_write_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_notification_policy_apply_policy(
                    action="ingress_canary_route.apply",
                    product="launchplane",
                    context="launchplane",
                ),
                record_store_factory=lambda: store,
            )

            response = await _asgi_request(
                app,
                "POST",
                "/v1/ingress/canary-routes/records/apply",
                headers={"Authorization": "Bearer valid-token"},
                payload=_ingress_canary_route_record_apply_payload(mode="dry-run"),
            )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["records"]["ingress_canary_route_status"], "planned")
        self.assertEqual(payload["result"]["mode"], "dry-run")
        with self.assertRaises(FileNotFoundError):
            store.read_ingress_canary_route_record("ingress-canary")

    async def test_ingress_canary_route_record_apply_requires_idempotency_key_before_store_gate(
        self,
    ) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_notification_policy_apply_policy(
                action="ingress_canary_route.apply",
                product="launchplane",
                context="launchplane",
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_request(
            app,
            "POST",
            "/v1/ingress/canary-routes/records/apply",
            headers={"Authorization": "Bearer valid-token"},
            payload=_ingress_canary_route_record_apply_payload(mode="apply"),
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "idempotency_key_required")

    async def test_ingress_canary_route_apply_resolves_edge_endpoint_and_writes_audit(
        self,
    ) -> None:
        client = _FakeNpmplusIngressClient((_npmplus_proxy_host(id=78),))
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            store.write_edge_endpoint_record(_edge_endpoint_record())
            store.write_ingress_canary_route_record(_ingress_canary_route_record())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_notification_policy_apply_policy(
                    action="ingress_route.apply",
                    product="launchplane",
                    context="reon-prod",
                ),
                record_store_factory=lambda: store,
                npmplus_ingress_client_factory=lambda: client,
            )

            response = await _asgi_request(
                app,
                "POST",
                "/v1/ingress/canary-routes/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "ingress-canary-apply",
                },
                payload=_ingress_canary_route_apply_payload(),
            )
            records = store.list_ingress_route_audit_records(
                product="launchplane", context_name="reon-prod"
            )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["records"]["ingress_canary_route_key"], "ingress-canary")
        self.assertEqual(payload["records"]["ingress_route_audit_record_id"], records[-1].record_id)
        self.assertEqual(payload["records"]["ingress_provider"], "npmplus")
        self.assertEqual(payload["result"]["status"], "applied")
        self.assertFalse(payload["result"]["dry_run"])
        self.assertEqual(client.calls, ["list", "update:78"])
        self.assertEqual(records[-1].requested_domains, ("ingress-canary.example.test",))
        self.assertEqual(records[-1].edge_endpoint_key, "cm-prod-dokploy")
        self.assertEqual(records[-1].expected_host_id, 78)

    async def test_ingress_canary_route_apply_replays_before_record_changes(self) -> None:
        client = _FakeNpmplusIngressClient((_npmplus_proxy_host(id=78),))
        request_payload = _ingress_canary_route_apply_payload()
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            store.write_edge_endpoint_record(_edge_endpoint_record())
            store.write_ingress_canary_route_record(_ingress_canary_route_record())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_notification_policy_apply_policy(
                    action="ingress_route.apply",
                    product="launchplane",
                    context="reon-prod",
                ),
                record_store_factory=lambda: store,
                npmplus_ingress_client_factory=lambda: client,
            )

            first_response = await _asgi_request(
                app,
                "POST",
                "/v1/ingress/canary-routes/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "ingress-canary-replay",
                },
                payload=request_payload,
            )
            store.write_ingress_canary_route_record(
                _ingress_canary_route_record().model_copy(
                    update={
                        "domain_name": "changed-canary.example.test",
                        "expected_host_id": 99,
                        "status": "disabled",
                        "updated_at": "2026-06-11T01:00:00Z",
                    }
                )
            )
            replay_response = await _asgi_request(
                app,
                "POST",
                "/v1/ingress/canary-routes/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "ingress-canary-replay",
                },
                payload=request_payload,
            )
            records = store.list_ingress_route_audit_records(
                product="launchplane", context_name="reon-prod"
            )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(replay_response.status_code, 202)
        first_payload = first_response.json()
        replay_payload = replay_response.json()
        self.assertTrue(replay_payload["replayed"])
        self.assertEqual(replay_payload["original_trace_id"], first_payload["trace_id"])
        self.assertEqual(
            replay_payload["records"]["ingress_route_audit_record_id"],
            first_payload["records"]["ingress_route_audit_record_id"],
        )
        self.assertEqual(client.calls, ["list", "update:78"])
        self.assertEqual(len(records), 1)

    async def test_ingress_canary_route_apply_rejects_missing_record(self) -> None:
        client = _FakeNpmplusIngressClient()
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_notification_policy_apply_policy(
                    action="ingress_route.apply",
                    product="launchplane",
                    context="reon-prod",
                ),
                record_store_factory=lambda: store,
                npmplus_ingress_client_factory=lambda: client,
            )

            response = await _asgi_request(
                app,
                "POST",
                "/v1/ingress/canary-routes/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "ingress-canary-missing",
                },
                payload=_ingress_canary_route_apply_payload(),
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_ingress_canary_route")
        self.assertEqual(client.calls, [])

    async def test_ingress_canary_route_apply_rejects_disabled_record(self) -> None:
        client = _FakeNpmplusIngressClient()
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            store.write_ingress_canary_route_record(_ingress_canary_route_record(status="disabled"))
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_notification_policy_apply_policy(
                    action="ingress_route.apply",
                    product="launchplane",
                    context="reon-prod",
                ),
                record_store_factory=lambda: store,
                npmplus_ingress_client_factory=lambda: client,
            )

            response = await _asgi_request(
                app,
                "POST",
                "/v1/ingress/canary-routes/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "ingress-canary-disabled",
                },
                payload=_ingress_canary_route_apply_payload(reason="test disabled canary apply"),
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_ingress_canary_route")
        self.assertIn("not active", response.json()["error"]["message"])
        self.assertEqual(client.calls, [])

    async def test_ingress_canary_route_apply_rejects_missing_edge_endpoint(self) -> None:
        client = _FakeNpmplusIngressClient((_npmplus_proxy_host(id=78),))
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            store.write_ingress_canary_route_record(_ingress_canary_route_record())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_notification_policy_apply_policy(
                    action="ingress_route.apply",
                    product="launchplane",
                    context="reon-prod",
                ),
                record_store_factory=lambda: store,
                npmplus_ingress_client_factory=lambda: client,
            )

            response = await _asgi_request(
                app,
                "POST",
                "/v1/ingress/canary-routes/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "ingress-canary-missing-edge",
                },
                payload=_ingress_canary_route_apply_payload(reason="test missing edge endpoint"),
            )
            records = store.list_ingress_route_audit_records(
                product="launchplane", context_name="reon-prod"
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_edge_endpoint")
        self.assertEqual(client.calls, [])
        self.assertEqual(records, ())

    async def test_ingress_canary_route_apply_rejects_disabled_edge_endpoint(self) -> None:
        client = _FakeNpmplusIngressClient((_npmplus_proxy_host(id=78),))
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            store.write_edge_endpoint_record(_edge_endpoint_record(status="disabled"))
            store.write_ingress_canary_route_record(_ingress_canary_route_record())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_notification_policy_apply_policy(
                    action="ingress_route.apply",
                    product="launchplane",
                    context="reon-prod",
                ),
                record_store_factory=lambda: store,
                npmplus_ingress_client_factory=lambda: client,
            )

            response = await _asgi_request(
                app,
                "POST",
                "/v1/ingress/canary-routes/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "ingress-canary-disabled-edge",
                },
                payload=_ingress_canary_route_apply_payload(reason="test disabled edge endpoint"),
            )
            records = store.list_ingress_route_audit_records(
                product="launchplane", context_name="reon-prod"
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_edge_endpoint")
        self.assertIn("not active", response.json()["error"]["message"])
        self.assertEqual(client.calls, [])
        self.assertEqual(records, ())

    async def test_ingress_canary_route_apply_maps_provider_guard_to_invalid_request(
        self,
    ) -> None:
        class RejectingIngressProvider:
            provider_id = "npmplus"
            delegated_executor = "test"

            def apply_route(self, *, request: Any) -> Any:
                raise ClickException("Provider guard rejected canary route apply.")

        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            store.write_edge_endpoint_record(_edge_endpoint_record())
            store.write_ingress_canary_route_record(_ingress_canary_route_record())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_notification_policy_apply_policy(
                    action="ingress_route.apply",
                    product="launchplane",
                    context="reon-prod",
                ),
                record_store_factory=lambda: store,
                ingress_provider_factory=RejectingIngressProvider,
            )

            response = await _asgi_request(
                app,
                "POST",
                "/v1/ingress/canary-routes/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "ingress-canary-provider-reject",
                },
                payload=_ingress_canary_route_apply_payload(reason="test provider rejection"),
            )
            records = store.list_ingress_route_audit_records(
                product="launchplane", context_name="reon-prod"
            )
            idempotency_record = store.read_idempotency_record(
                scope="github-actions:every/verireel",
                route_path="/v1/ingress/canary-routes/apply",
                idempotency_key="ingress-canary-provider-reject",
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")
        self.assertEqual(response.json()["error"]["message"], "Request could not be completed.")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].status, "pending")
        self.assertIsNone(idempotency_record)

    async def test_ingress_canary_route_apply_authorizes_before_record_lookup(self) -> None:
        client = _FakeNpmplusIngressClient()
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
            record_store_factory=lambda: _MissingProductReadStore(),
            npmplus_ingress_client_factory=lambda: client,
        )

        response = await _asgi_request(
            app,
            "POST",
            "/v1/ingress/canary-routes/apply",
            headers={
                "Authorization": "Bearer valid-token",
                "Idempotency-Key": "ingress-canary-unauthorized",
            },
            payload=_ingress_canary_route_apply_payload(canary_key="missing-canary"),
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")
        self.assertEqual(client.calls, [])

    async def test_ingress_canary_route_apply_requires_idempotency_before_store_gate(
        self,
    ) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_notification_policy_apply_policy(
                action="ingress_route.apply",
                product="launchplane",
                context="reon-prod",
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_request(
            app,
            "POST",
            "/v1/ingress/canary-routes/apply",
            headers={"Authorization": "Bearer valid-token"},
            payload=_ingress_canary_route_apply_payload(),
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "idempotency_key_required")

    async def test_ingress_canary_apply_routes_precede_legacy_wsgi_fallback(self) -> None:
        client = _FakeNpmplusIngressClient((_npmplus_proxy_host(id=78),))
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_edge_endpoint_record(_edge_endpoint_record())
            store.write_ingress_canary_route_record(_ingress_canary_route_record())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate(
                    {
                        "github_actions": [
                            {
                                "repository": "every/verireel",
                                "workflow_refs": [
                                    "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                                ],
                                "event_names": ["pull_request"],
                                "products": ["launchplane"],
                                "contexts": ["launchplane", "reon-prod"],
                                "actions": [
                                    "ingress_canary_route.apply",
                                    "ingress_route.apply",
                                ],
                            }
                        ]
                    }
                ),
                record_store_factory=lambda: store,
                npmplus_ingress_client_factory=lambda: client,
            )
            legacy_app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=root,
                local_record_store_for_tests=store,
            )
            app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

            record_response = await _asgi_request(
                app,
                "POST",
                "/v1/ingress/canary-routes/records/apply",
                headers={"Authorization": "Bearer valid-token"},
                payload=_ingress_canary_route_record_apply_payload(mode="dry-run"),
            )
            apply_response = await _asgi_request(
                app,
                "POST",
                "/v1/ingress/canary-routes/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "ingress-canary-mounted-fallback-test",
                },
                payload=_ingress_canary_route_apply_payload(),
            )

        self.assertEqual(record_response.status_code, 202)
        self.assertEqual(apply_response.status_code, 202)
        self.assertEqual(record_response.json()["result"]["mode"], "dry-run")
        self.assertEqual(
            apply_response.json()["records"]["ingress_canary_route_key"], "ingress-canary"
        )

    async def test_openapi_includes_ingress_canary_apply_routes(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        record_route = openapi["paths"]["/v1/ingress/canary-routes/records/apply"]["post"]
        apply_route = openapi["paths"]["/v1/ingress/canary-routes/apply"]["post"]
        self.assertEqual(record_route["operationId"], "apply_ingress_canary_route_record")
        self.assertEqual(apply_route["operationId"], "apply_ingress_canary_route")
        self.assertEqual(
            record_route["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/AcceptedEvidenceResponse",
        )
        self.assertEqual(
            apply_route["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/AcceptedEvidenceResponse",
        )


class FastApiIngressCanaryRouteReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_ingress_canary_route_read_returns_record_for_authorized_workflow(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            store.write_ingress_canary_route_record(_ingress_canary_route_record())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_record_read_policy(
                    action="ingress_canary_route.read",
                    context="launchplane",
                ),
                record_store_factory=lambda: store,
            )

            response = await _get_ingress_canary_route_record(app, "ingress-canary")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["record"]["canary_key"], "ingress-canary")
        self.assertEqual(payload["record"]["domain_name"], "ingress-canary.example.test")
        self.assertEqual(payload["record"]["edge_endpoint_key"], "cm-prod-dokploy")

    async def test_ingress_canary_route_list_filters_records(self) -> None:
        active_record = _ingress_canary_route_record()
        disabled_record = active_record.model_copy(
            update={
                "canary_key": "disabled-canary",
                "domain_name": "disabled-canary.example.test",
                "expected_host_id": 79,
                "status": "disabled",
            }
        )
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            store.write_ingress_canary_route_record(active_record)
            store.write_ingress_canary_route_record(disabled_record)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_record_read_policy(
                    action="ingress_canary_route.read",
                    context="launchplane",
                ),
                record_store_factory=lambda: store,
            )

            response = await _get_ingress_canary_route_records(
                app,
                product="launchplane",
                context="reon-prod",
                status="active",
                limit="1",
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["limit"], 1)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["records"][0]["canary_key"], "ingress-canary")

    async def test_ingress_canary_route_reads_require_identity(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(
                action="ingress_canary_route.read",
                context="launchplane",
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _get_ingress_canary_route_records(app, authorization="")

        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authentication_required")

    async def test_ingress_canary_route_reads_require_authz_action(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(action="driver.read", context="launchplane"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _get_ingress_canary_route_record(app, "ingress-canary")

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    async def test_ingress_canary_route_reads_require_record_storage(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(
                action="ingress_canary_route.read",
                context="launchplane",
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _get_ingress_canary_route_records(app)

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "database_storage_required")

    async def test_ingress_canary_route_read_returns_not_found_for_missing_record(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_record_read_policy(
                    action="ingress_canary_route.read",
                    context="launchplane",
                ),
                record_store_factory=lambda: store,
            )

            response = await _get_ingress_canary_route_record(app, "missing-canary")

        self.assertEqual(response.status_code, 404)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "not_found")

    async def test_ingress_canary_route_list_validates_limit(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(
                action="ingress_canary_route.read",
                context="launchplane",
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        low_response = await _get_ingress_canary_route_records(app, limit="0")
        high_response = await _get_ingress_canary_route_records(app, limit="101")

        self.assertEqual(low_response.status_code, 400)
        self.assertEqual(high_response.status_code, 400)
        self.assertEqual(low_response.json()["error"]["code"], "invalid_query")
        self.assertEqual(high_response.json()["error"]["code"], "invalid_query")

    async def test_openapi_includes_ingress_canary_route_read_contracts(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(
                action="ingress_canary_route.read",
                context="launchplane",
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        list_route = openapi["paths"]["/v1/ingress/canary-routes/records"]["get"]
        read_route = openapi["paths"]["/v1/ingress/canary-routes/records/{canary_key}"]["get"]
        self.assertEqual(list_route["operationId"], "list_ingress_canary_route_records")
        self.assertEqual(read_route["operationId"], "read_ingress_canary_route_record")
        self.assertEqual(
            list_route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/IngressCanaryRouteRecordsResponse",
        )
        self.assertEqual(
            read_route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/IngressCanaryRouteRecordResponse",
        )
        self.assertIn("LaunchplaneErrorResponse", json.dumps(list_route))
        self.assertIn("LaunchplaneErrorResponse", json.dumps(read_route))
        self.assertEqual(
            openapi["components"]["schemas"]["IngressCanaryRouteRecordResponse"][
                "additionalProperties"
            ],
            False,
        )
        self.assertEqual(
            openapi["components"]["schemas"]["IngressCanaryRouteRecordsResponse"][
                "additionalProperties"
            ],
            False,
        )

    async def test_fastapi_ingress_canary_route_reads_precede_legacy_wsgi_fallback(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_ingress_canary_route_record(_ingress_canary_route_record())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_record_read_policy(
                    action="ingress_canary_route.read",
                    context="launchplane",
                ),
                record_store_factory=lambda: store,
            )
            legacy_app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({}),
                control_plane_root_path=root,
            )
            app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

            response = await _get_ingress_canary_route_record(app, "ingress-canary")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")


class FastApiIngressRouteAuditReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_ingress_route_audit_reads_return_native_payloads(self) -> None:
        planned_record = _ingress_route_audit_record()
        newer_applied_record = _ingress_route_audit_record(
            record_id="ingress-route-audit-applied",
            mode="apply",
            status="applied",
            dry_run=False,
            provider_host_id=79,
            trace_id="trace-audit-2",
            idempotency_key="audit-key-2",
            recorded_at="2026-06-02T00:00:00Z",
        )
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            store.write_ingress_route_audit_record(planned_record)
            store.write_ingress_route_audit_record(newer_applied_record)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_record_read_policy(action="ingress_route.plan", context="reon-prod"),
                record_store_factory=lambda: store,
            )

            list_response = await _get_ingress_route_audit_records(
                app,
                product="launchplane",
                context="reon-prod",
                status="planned",
                mode="dry-run",
                provider_host_id="78",
                trace_id="trace-audit-1",
                idempotency_key="audit-key-1",
                limit="1",
            )
            read_response = await _get_ingress_route_audit_record(
                app,
                planned_record.record_id,
                product="launchplane",
                context="reon-prod",
            )

        self.assertEqual(list_response.status_code, 200)
        list_payload = list_response.json()
        self.assertEqual(list_payload["status"], "ok")
        self.assertEqual(list_payload["product"], "launchplane")
        self.assertEqual(list_payload["context"], "reon-prod")
        self.assertEqual(list_payload["limit"], 1)
        self.assertEqual(list_payload["count"], 1)
        self.assertEqual(list_payload["records"][0]["record_id"], planned_record.record_id)
        self.assertEqual(read_response.status_code, 200)
        read_payload = read_response.json()
        self.assertEqual(read_payload["status"], "ok")
        self.assertEqual(read_payload["record"]["record_id"], planned_record.record_id)
        self.assertEqual(read_payload["record"]["provider_host_id"], 78)

    async def test_ingress_route_audit_read_hides_record_outside_requested_scope(
        self,
    ) -> None:
        record = _ingress_route_audit_record()
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            store.write_ingress_route_audit_record(record)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_ingress_route_audit_read_policy(contexts=("reon-prod", "cm-prod")),
                record_store_factory=lambda: store,
            )

            response = await _get_ingress_route_audit_record(
                app,
                record.record_id,
                product="launchplane",
                context="cm-prod",
            )

        self.assertEqual(response.status_code, 404)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "not_found")

    async def test_ingress_route_audit_reads_require_scoped_query(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(action="ingress_route.plan", context="reon-prod"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        list_response = await _get_ingress_route_audit_records(
            app,
            product="launchplane",
        )
        read_response = await _get_ingress_route_audit_record(
            app,
            "ingress-route-audit-test",
        )

        self.assertEqual(list_response.status_code, 400)
        self.assertEqual(read_response.status_code, 400)
        self.assertEqual(list_response.json()["error"]["code"], "invalid_query")
        self.assertEqual(read_response.json()["error"]["code"], "invalid_query")

    async def test_ingress_route_audit_reads_reject_unauthorized_context(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(action="ingress_route.plan", context="reon-prod"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _get_ingress_route_audit_records(
            app,
            product="launchplane",
            context="cm-prod",
        )

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    async def test_ingress_route_audit_reads_require_record_storage(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(action="ingress_route.plan", context="reon-prod"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _get_ingress_route_audit_records(
            app,
            product="launchplane",
            context="reon-prod",
        )

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "database_storage_required")

    async def test_ingress_route_audit_read_returns_not_found_for_missing_record(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_record_read_policy(action="ingress_route.plan", context="reon-prod"),
                record_store_factory=lambda: store,
            )

            response = await _get_ingress_route_audit_record(
                app,
                "missing-audit-record",
                product="launchplane",
                context="reon-prod",
            )

        self.assertEqual(response.status_code, 404)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "not_found")

    async def test_ingress_route_audit_reads_reject_invalid_query_values(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(action="ingress_route.plan", context="reon-prod"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        low_limit_response = await _get_ingress_route_audit_records(
            app,
            product="launchplane",
            context="reon-prod",
            limit="0",
        )
        high_limit_response = await _get_ingress_route_audit_records(
            app,
            product="launchplane",
            context="reon-prod",
            limit="101",
        )
        provider_host_response = await _get_ingress_route_audit_records(
            app,
            product="launchplane",
            context="reon-prod",
            provider_host_id="0",
        )

        self.assertEqual(low_limit_response.status_code, 400)
        self.assertEqual(high_limit_response.status_code, 400)
        self.assertEqual(provider_host_response.status_code, 400)
        self.assertEqual(low_limit_response.json()["error"]["code"], "invalid_query")
        self.assertEqual(high_limit_response.json()["error"]["code"], "invalid_query")
        self.assertEqual(provider_host_response.json()["error"]["code"], "invalid_query")

    async def test_openapi_includes_ingress_route_audit_read_contracts(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(action="ingress_route.plan", context="reon-prod"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        list_route = openapi["paths"]["/v1/ingress/route-audits/records"]["get"]
        read_route = openapi["paths"]["/v1/ingress/route-audits/records/{record_id}"]["get"]
        self.assertEqual(list_route["operationId"], "list_ingress_route_audit_records")
        self.assertEqual(read_route["operationId"], "read_ingress_route_audit_record")
        self.assertEqual(
            list_route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/IngressRouteAuditRecordsResponse",
        )
        self.assertEqual(
            read_route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/IngressRouteAuditRecordResponse",
        )
        self.assertIn("LaunchplaneErrorResponse", json.dumps(list_route))
        self.assertIn("LaunchplaneErrorResponse", json.dumps(read_route))
        self.assertEqual(
            openapi["components"]["schemas"]["IngressRouteAuditRecordResponse"][
                "additionalProperties"
            ],
            False,
        )
        self.assertEqual(
            openapi["components"]["schemas"]["IngressRouteAuditRecordsResponse"][
                "additionalProperties"
            ],
            False,
        )

    async def test_fastapi_ingress_route_audit_reads_precede_legacy_wsgi_fallback(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            record = _ingress_route_audit_record()
            store.write_ingress_route_audit_record(record)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_record_read_policy(action="ingress_route.plan", context="reon-prod"),
                record_store_factory=lambda: store,
            )
            legacy_app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({}),
                control_plane_root_path=root,
            )
            app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

            response = await _get_ingress_route_audit_record(
                app,
                record.record_id,
                product="launchplane",
                context="reon-prod",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")


class FastApiDeploymentPromotionReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_deployment_read_returns_record_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            store.write_deployment_record(_deployment_read_record())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_record_read_policy(
                    action="deployment.read",
                    context="example-site",
                ),
                record_store_factory=lambda: store,
            )

            response = await _get_deployment_record(app, "deployment-example-site-prod")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["trace_id"].startswith("launchplane_req_"))
        self.assertEqual(payload["record"]["record_id"], "deployment-example-site-prod")
        self.assertEqual(
            payload["record"]["resolved_target"]["target_id"],
            "target-example-site-prod",
        )

    async def test_deployment_read_requires_identity(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(action="deployment.read", context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _get_deployment_record(
            app,
            "deployment-example-site-prod",
            authorization="",
        )

        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authentication_required")

    async def test_deployment_read_rejects_wrong_context_grant(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            store.write_deployment_record(_deployment_read_record())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_record_read_policy(
                    action="deployment.read",
                    context="other-site",
                ),
                record_store_factory=lambda: store,
            )

            response = await _get_deployment_record(app, "deployment-example-site-prod")

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    async def test_deployment_read_returns_not_found_for_missing_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_record_read_policy(
                    action="deployment.read",
                    context="other-site",
                ),
                record_store_factory=lambda: store,
            )

            response = await _get_deployment_record(app, "deployment-example-site-prod")

        self.assertEqual(response.status_code, 404)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "not_found")

    async def test_deployment_read_requires_read_capable_store(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(action="deployment.read", context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _get_deployment_record(app, "deployment-example-site-prod")

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "database_storage_required")
        self.assertIn("read_deployment_record", payload["error"]["message"])

    async def test_promotion_read_returns_record_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            store.write_promotion_record(_promotion_read_record())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_record_read_policy(
                    action="promotion.read",
                    context="example-site",
                ),
                record_store_factory=lambda: store,
            )

            response = await _get_promotion_record(
                app,
                "promotion-example-site-testing-to-prod",
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["trace_id"].startswith("launchplane_req_"))
        self.assertEqual(
            payload["record"]["record_id"],
            "promotion-example-site-testing-to-prod",
        )
        self.assertEqual(payload["record"]["to_instance"], "prod")

    async def test_promotion_read_requires_identity(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(action="promotion.read", context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _get_promotion_record(
            app,
            "promotion-example-site-testing-to-prod",
            authorization="",
        )

        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authentication_required")

    async def test_promotion_read_rejects_wrong_context_grant(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            store.write_promotion_record(_promotion_read_record())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_record_read_policy(
                    action="promotion.read",
                    context="other-site",
                ),
                record_store_factory=lambda: store,
            )

            response = await _get_promotion_record(
                app,
                "promotion-example-site-testing-to-prod",
            )

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    async def test_promotion_read_returns_not_found_for_missing_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_record_read_policy(
                    action="promotion.read",
                    context="other-site",
                ),
                record_store_factory=lambda: store,
            )

            response = await _get_promotion_record(
                app,
                "promotion-example-site-testing-to-prod",
            )

        self.assertEqual(response.status_code, 404)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "not_found")

    async def test_promotion_read_requires_read_capable_store(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(action="promotion.read", context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _get_promotion_record(
            app,
            "promotion-example-site-testing-to-prod",
        )

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "database_storage_required")
        self.assertIn("read_promotion_record", payload["error"]["message"])

    async def test_openapi_includes_deployment_and_promotion_read_contracts(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(action="deployment.read", context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        deployment_route = openapi["paths"]["/v1/deployments/{record_id}"]["get"]
        promotion_route = openapi["paths"]["/v1/promotions/{record_id}"]["get"]
        self.assertEqual(deployment_route["operationId"], "read_deployment_record")
        self.assertEqual(promotion_route["operationId"], "read_promotion_record")
        self.assertEqual(
            deployment_route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/DeploymentRecordResponse",
        )
        self.assertEqual(
            promotion_route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/PromotionRecordResponse",
        )
        for route in (deployment_route, promotion_route):
            self.assertIn("LaunchplaneErrorResponse", json.dumps(route))
            self.assertIn("401", route["responses"])
            self.assertIn("403", route["responses"])
            self.assertIn("404", route["responses"])
            self.assertIn("503", route["responses"])
        self.assertEqual(
            openapi["components"]["schemas"]["DeploymentRecordResponse"]["additionalProperties"],
            False,
        )
        self.assertEqual(
            openapi["components"]["schemas"]["PromotionRecordResponse"]["additionalProperties"],
            False,
        )

    async def test_fastapi_record_reads_precede_legacy_wsgi_fallback(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_deployment_record(_deployment_read_record())
            store.write_promotion_record(_promotion_read_record())
            policy = _record_read_policy(
                action="deployment.read",
                context="example-site",
                extra_actions=("promotion.read",),
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                record_store_factory=lambda: store,
            )
            legacy_app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({}),
                control_plane_root_path=root,
            )
            app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

            deployment_response = await _get_deployment_record(
                app,
                "deployment-example-site-prod",
            )
            promotion_response = await _get_promotion_record(
                app,
                "promotion-example-site-testing-to-prod",
            )

        self.assertEqual(deployment_response.status_code, 200)
        self.assertEqual(promotion_response.status_code, 200)
        self.assertEqual(deployment_response.json()["status"], "ok")
        self.assertEqual(promotion_response.json()["status"], "ok")


class FastApiEnvironmentInventoryReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_inventory_read_returns_record_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            store.write_environment_inventory(_environment_inventory_read_record())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_record_read_policy(
                    action="inventory.read",
                    context="example-site",
                ),
                record_store_factory=lambda: store,
            )

            response = await _get_environment_inventory(app, "example-site", "prod")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["trace_id"].startswith("launchplane_req_"))
        self.assertEqual(payload["record"]["context"], "example-site")
        self.assertEqual(payload["record"]["instance"], "prod")
        self.assertEqual(payload["record"]["deployment_record_id"], "deployment-example-site-prod")

    async def test_inventory_read_requires_identity(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(action="inventory.read", context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _get_environment_inventory(
            app,
            "example-site",
            "prod",
            authorization="",
        )

        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authentication_required")

    async def test_inventory_read_rejects_wrong_context_grant(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(
                action="inventory.read",
                context="other-site",
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _get_environment_inventory(app, "example-site", "prod")

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    async def test_inventory_read_returns_not_found_for_missing_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_record_read_policy(
                    action="inventory.read",
                    context="example-site",
                ),
                record_store_factory=lambda: store,
            )

            response = await _get_environment_inventory(app, "example-site", "prod")

        self.assertEqual(response.status_code, 404)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "not_found")

    async def test_inventory_read_requires_read_capable_store(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(action="inventory.read", context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _get_environment_inventory(app, "example-site", "prod")

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "database_storage_required")
        self.assertIn("read_environment_inventory", payload["error"]["message"])

    async def test_openapi_includes_inventory_read_contract(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(action="inventory.read", context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        route = openapi["paths"]["/v1/inventory/{context}/{instance}"]["get"]
        self.assertEqual(route["operationId"], "read_environment_inventory")
        self.assertEqual(
            route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/EnvironmentInventoryResponse",
        )
        self.assertIn("LaunchplaneErrorResponse", json.dumps(route))
        self.assertIn("401", route["responses"])
        self.assertIn("403", route["responses"])
        self.assertIn("404", route["responses"])
        self.assertIn("503", route["responses"])
        self.assertEqual(
            openapi["components"]["schemas"]["EnvironmentInventoryResponse"][
                "additionalProperties"
            ],
            False,
        )

    async def test_fastapi_inventory_read_precedes_legacy_wsgi_fallback(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_environment_inventory(_environment_inventory_read_record())
            policy = _record_read_policy(
                action="inventory.read",
                context="example-site",
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                record_store_factory=lambda: store,
            )
            legacy_app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({}),
                control_plane_root_path=root,
            )
            app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

            response = await _get_environment_inventory(app, "example-site", "prod")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")


class FastApiRecentOperationsReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_recent_operations_returns_operator_read_model(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            _write_recent_operations_records(store)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_record_read_policy(
                    action="operations.read",
                    context="example-site",
                ),
                record_store_factory=lambda: store,
            )

            response = await _get_recent_operations(app, "example-site")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["trace_id"].startswith("launchplane_req_"))
        self.assertEqual(payload["context"], "example-site")
        self.assertEqual(payload["storage_backend"], "filesystem")
        self.assertEqual(len(payload["inventory"]), 1)
        self.assertEqual(len(payload["recent_deployments"]), 1)
        self.assertEqual(len(payload["recent_promotions"]), 1)
        self.assertEqual(len(payload["recent_previews"]), 1)
        self.assertEqual(payload["inventory"][0]["context"], "example-site")
        self.assertEqual(payload["recent_deployments"][0]["context"], "example-site")
        self.assertEqual(payload["recent_promotions"][0]["context"], "example-site")
        self.assertEqual(payload["recent_previews"][0]["context"], "example-site")

    async def test_recent_operations_requires_identity(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(action="operations.read", context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _get_recent_operations(
            app,
            "example-site",
            authorization="",
        )

        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authentication_required")

    async def test_recent_operations_rejects_wrong_context_before_store_access(
        self,
    ) -> None:
        store = _RecentOperationsProbeStore()
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(
                action="operations.read",
                context="other-site",
            ),
            record_store_factory=lambda: store,
        )

        response = await _get_recent_operations(app, "example-site")

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")
        self.assertEqual(store.calls, [])

    async def test_recent_operations_requires_read_capable_store(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(action="operations.read", context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _get_recent_operations(app, "example-site")

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "database_storage_required")
        self.assertIn("list_environment_inventory", payload["error"]["message"])

    async def test_openapi_includes_recent_operations_contract(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(action="operations.read", context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        route = openapi["paths"]["/v1/contexts/{context}/operations/recent"]["get"]
        self.assertEqual(route["operationId"], "read_recent_operations")
        self.assertEqual(
            route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/RecentOperationsResponse",
        )
        self.assertIn("LaunchplaneErrorResponse", json.dumps(route))
        self.assertIn("401", route["responses"])
        self.assertIn("403", route["responses"])
        self.assertIn("503", route["responses"])
        self.assertEqual(
            openapi["components"]["schemas"]["RecentOperationsResponse"]["additionalProperties"],
            False,
        )

    async def test_fastapi_recent_operations_precedes_legacy_wsgi_fallback(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            _write_recent_operations_records(store)
            policy = _record_read_policy(
                action="operations.read",
                context="example-site",
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                record_store_factory=lambda: store,
            )
            legacy_app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({}),
                control_plane_root_path=root,
            )
            app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

            response = await _get_recent_operations(app, "example-site")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")


class FastApiSecretStatusReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_secret_status_routes_return_metadata_only_models(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            secret_ids = _write_secret_status_records(database_url)
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_record_read_policy(
                    action="secret.list",
                    context="example-site",
                    extra_actions=("secret.read",),
                ),
                record_store_factory=lambda: app_store,
            )

            context_response = await _get_context_secret_statuses(app, "example-site")
            instance_response = await _get_instance_secret_statuses(
                app,
                "example-site",
                "prod",
            )
            show_response = await _get_secret_status(app, secret_ids["context"])
            app_store.close()

        self.assertEqual(context_response.status_code, 200)
        self.assertEqual(instance_response.status_code, 200)
        self.assertEqual(show_response.status_code, 200)
        context_payload = context_response.json()
        instance_payload = instance_response.json()
        show_payload = show_response.json()
        self.assertEqual(context_payload["status"], "ok")
        self.assertEqual(context_payload["context"], "example-site")
        self.assertEqual(context_payload["instance"], "")
        self.assertEqual(
            {secret["secret_id"] for secret in context_payload["secrets"]},
            {secret_ids["global"], secret_ids["context"]},
        )
        self.assertEqual(instance_payload["context"], "example-site")
        self.assertEqual(instance_payload["instance"], "prod")
        self.assertEqual(
            {secret["secret_id"] for secret in instance_payload["secrets"]},
            {secret_ids["global"], secret_ids["context"], secret_ids["instance"]},
        )
        self.assertNotIn(secret_ids["other_instance"], json.dumps(instance_payload))
        self.assertEqual(show_payload["status"], "ok")
        self.assertEqual(show_payload["secret"]["secret_id"], secret_ids["context"])
        self.assertEqual(show_payload["secret"]["context"], "example-site")
        self.assertEqual(
            show_payload["secret"]["binding"]["binding_key"],
            "GITHUB_WEBHOOK_SECRET",
        )
        response_text = json.dumps(
            {"context": context_payload, "instance": instance_payload, "show": show_payload}
        )
        self.assertNotIn("global-token", response_text)
        self.assertNotIn("plain-secret-value-alpha", response_text)
        self.assertNotIn("plain-secret-value-beta", response_text)
        self.assertNotIn("plain-secret-value-gamma", response_text)
        self.assertNotIn("ciphertext", response_text)

    async def test_secret_status_routes_require_identity(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(action="secret.list", context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        list_response = await _get_context_secret_statuses(
            app,
            "example-site",
            authorization="",
        )
        show_response = await _get_secret_status(
            app,
            "secret-runtime-environment-github-webhook-secret-example-site",
            authorization="",
        )

        self.assertEqual(list_response.status_code, 401)
        self.assertEqual(list_response.json()["error"]["code"], "authentication_required")
        self.assertEqual(show_response.status_code, 401)
        self.assertEqual(show_response.json()["error"]["code"], "authentication_required")

    async def test_secret_list_rejects_wrong_context_before_store_access(self) -> None:
        store = _SecretStatusProbeStore()
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(action="secret.list", context="other-site"),
            record_store_factory=lambda: store,
        )

        response = await _get_context_secret_statuses(app, "example-site")

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")
        self.assertEqual(store.calls, [])

    async def test_secret_read_uses_stored_context_for_authorization(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            secret_ids = _write_secret_status_records(database_url)
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_record_read_policy(action="secret.read", context="other-site"),
                record_store_factory=lambda: app_store,
            )

            response = await _get_secret_status(app, secret_ids["context"])
            app_store.close()

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")
        self.assertNotIn("plain-secret-value-alpha", json.dumps(payload))

    async def test_secret_status_routes_require_read_capable_store(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(
                action="secret.list",
                context="example-site",
                extra_actions=("secret.read",),
            ),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        list_response = await _get_context_secret_statuses(app, "example-site")
        show_response = await _get_secret_status(
            app,
            "secret-runtime-environment-github-webhook-secret-example-site",
        )

        self.assertEqual(list_response.status_code, 503)
        self.assertEqual(show_response.status_code, 503)
        self.assertEqual(list_response.json()["error"]["code"], "database_storage_required")
        self.assertEqual(show_response.json()["error"]["code"], "database_storage_required")
        self.assertIn("read_secret_record", list_response.json()["error"]["message"])
        self.assertIn("read_secret_record", show_response.json()["error"]["message"])

    async def test_secret_read_returns_not_found_for_missing_secret(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_record_read_policy(action="secret.read", context="example-site"),
                record_store_factory=lambda: store,
            )

            response = await _get_secret_status(app, "secret-runtime-environment-missing")
            store.close()

        self.assertEqual(response.status_code, 404)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "not_found")

    async def test_openapi_includes_secret_status_contracts(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(action="secret.list", context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        route_expectations = {
            "/v1/contexts/{context}/secrets": (
                "list_context_secret_statuses",
                "SecretStatusListResponse",
            ),
            "/v1/contexts/{context}/instances/{instance}/secrets": (
                "list_instance_secret_statuses",
                "SecretStatusListResponse",
            ),
            "/v1/secrets/{secret_id}": ("read_secret_status", "SecretStatusResponse"),
        }
        for path, (operation_id, response_model) in route_expectations.items():
            route = openapi["paths"][path]["get"]
            self.assertEqual(route["operationId"], operation_id)
            self.assertEqual(
                route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
                f"#/components/schemas/{response_model}",
            )
            self.assertIn("LaunchplaneErrorResponse", json.dumps(route))
            for status_code in ("401", "403", "404", "503"):
                self.assertIn(status_code, route["responses"])
        for schema_name in (
            "SecretStatusResponse",
            "SecretStatusListResponse",
            "SecretStatusReadModel",
            "SecretStatusBinding",
            "SecretStatusAuditEvent",
        ):
            self.assertEqual(
                openapi["components"]["schemas"][schema_name]["additionalProperties"],
                False,
            )
        schema_text = json.dumps(openapi["components"]["schemas"]["SecretStatusReadModel"])
        self.assertNotIn("ciphertext", schema_text)
        self.assertNotIn("plaintext", schema_text)

    async def test_fastapi_secret_status_precedes_legacy_wsgi_fallback(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            secret_ids = _write_secret_status_records(database_url)
            app_store = PostgresRecordStore(database_url=database_url)
            policy = _record_read_policy(
                action="secret.list",
                context="example-site",
                extra_actions=("secret.read",),
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                record_store_factory=lambda: app_store,
            )
            legacy_app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({}),
                control_plane_root_path=root,
            )
            app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

            list_response = await _get_context_secret_statuses(app, "example-site")
            show_response = await _get_secret_status(app, secret_ids["context"])
            app_store.close()

        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(show_response.status_code, 200)
        self.assertEqual(list_response.json()["status"], "ok")
        self.assertEqual(show_response.json()["status"], "ok")


class FastApiPreviewReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_read_returns_record_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            store.write_preview_record(_preview_read_record())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_record_read_policy(
                    action="preview.read",
                    context="example-site",
                ),
                record_store_factory=lambda: store,
            )

            response = await _get_preview_record(app, "preview-example-site-example-site-pr-42")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["trace_id"].startswith("launchplane_req_"))
        self.assertEqual(payload["record"]["preview_id"], "preview-example-site-example-site-pr-42")
        self.assertEqual(payload["record"]["context"], "example-site")
        self.assertEqual(payload["record"]["state"], "active")

    async def test_preview_history_returns_preview_and_generations(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            preview = _preview_read_record()
            store.write_preview_record(preview)
            store.write_preview_generation_record(
                _preview_generation_read_record(sequence=1, state="ready")
            )
            store.write_preview_generation_record(
                _preview_generation_read_record(sequence=2, state="verifying")
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_record_read_policy(
                    action="preview.read",
                    context="example-site",
                ),
                record_store_factory=lambda: store,
            )

            response = await _get_preview_history(app, preview.preview_id)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(
            payload["preview"]["preview_id"], "preview-example-site-example-site-pr-42"
        )
        self.assertNotIn("record", payload)
        self.assertEqual(
            [generation["sequence"] for generation in payload["generations"]],
            [2, 1],
        )
        self.assertEqual(payload["generations"][0]["state"], "verifying")

    async def test_preview_read_requires_identity(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(action="preview.read", context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _get_preview_record(
            app,
            "preview-example-site-example-site-pr-42",
            authorization="",
        )

        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authentication_required")

    async def test_preview_read_rejects_wrong_context_grant(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            store.write_preview_record(_preview_read_record())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_record_read_policy(
                    action="preview.read",
                    context="other-site",
                ),
                record_store_factory=lambda: store,
            )

            response = await _get_preview_record(app, "preview-example-site-example-site-pr-42")

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    async def test_preview_history_rejects_wrong_context_before_listing_generations(
        self,
    ) -> None:
        store = _PreviewHistoryProbeStore(_preview_read_record())
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(
                action="preview.read",
                context="other-site",
            ),
            record_store_factory=lambda: store,
        )

        response = await _get_preview_history(
            app,
            "preview-example-site-example-site-pr-42",
        )

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")
        self.assertEqual(store.list_preview_generation_calls, 0)

    async def test_preview_read_returns_not_found_for_missing_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_record_read_policy(
                    action="preview.read",
                    context="example-site",
                ),
                record_store_factory=lambda: store,
            )

            response = await _get_preview_record(app, "preview-example-site-example-site-pr-42")

        self.assertEqual(response.status_code, 404)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "not_found")

    async def test_preview_read_requires_read_capable_store(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(action="preview.read", context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _get_preview_record(app, "preview-example-site-example-site-pr-42")

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "database_storage_required")
        self.assertIn("read_preview_record", payload["error"]["message"])

    async def test_preview_history_requires_history_capable_store(self) -> None:
        store = _PreviewRecordOnlyStore(_preview_read_record())
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(action="preview.read", context="example-site"),
            record_store_factory=lambda: store,
        )

        response = await _get_preview_history(app, "preview-example-site-example-site-pr-42")

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "database_storage_required")
        self.assertIn("list_preview_generation_records", payload["error"]["message"])

    async def test_openapi_includes_preview_read_contracts(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_record_read_policy(action="preview.read", context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        preview_route = openapi["paths"]["/v1/previews/{preview_id}"]["get"]
        history_route = openapi["paths"]["/v1/previews/{preview_id}/history"]["get"]
        self.assertEqual(preview_route["operationId"], "read_preview_record")
        self.assertEqual(history_route["operationId"], "read_preview_history")
        self.assertEqual(
            preview_route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/PreviewRecordResponse",
        )
        self.assertEqual(
            history_route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/PreviewHistoryResponse",
        )
        for route in (preview_route, history_route):
            self.assertIn("LaunchplaneErrorResponse", json.dumps(route))
            self.assertIn("401", route["responses"])
            self.assertIn("403", route["responses"])
            self.assertIn("404", route["responses"])
            self.assertIn("503", route["responses"])
        self.assertEqual(
            openapi["components"]["schemas"]["PreviewRecordResponse"]["additionalProperties"],
            False,
        )
        self.assertEqual(
            openapi["components"]["schemas"]["PreviewHistoryResponse"]["additionalProperties"],
            False,
        )

    async def test_fastapi_preview_reads_precede_legacy_wsgi_fallback(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            preview = _preview_read_record()
            store.write_preview_record(preview)
            store.write_preview_generation_record(_preview_generation_read_record())
            policy = _record_read_policy(
                action="preview.read",
                context="example-site",
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                record_store_factory=lambda: store,
            )
            legacy_app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({}),
                control_plane_root_path=root,
            )
            app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

            preview_response = await _get_preview_record(app, preview.preview_id)
            history_response = await _get_preview_history(app, preview.preview_id)

        self.assertEqual(preview_response.status_code, 200)
        self.assertEqual(history_response.status_code, 200)
        self.assertEqual(preview_response.json()["status"], "ok")
        self.assertEqual(history_response.json()["status"], "ok")


class FastApiBackupGateEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_backup_gate_evidence_writes_record_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_backup_gate_write_identity()),
                authz_policy=_backup_gate_write_policy(context="example-site"),
                record_store_factory=lambda: store,
            )

            response = await _post_backup_gate_evidence(app, _backup_gate_evidence_payload())
            backup_gate = store.read_backup_gate_record("backup-gate-example-site-prod")

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(
            payload["records"],
            {"backup_gate_record_id": "backup-gate-example-site-prod"},
        )
        self.assertNotIn("replayed", payload)
        self.assertEqual(backup_gate.context, "example-site")
        self.assertEqual(backup_gate.instance, "prod")
        self.assertEqual(backup_gate.status, "pass")
        self.assertEqual(backup_gate.evidence["snapshot_name"], "snapshot-example-site-prod")

    async def test_backup_gate_evidence_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_backup_gate_write_identity()),
                authz_policy=_backup_gate_write_policy(context="other-site"),
                record_store_factory=lambda: store,
            )

            response = await _post_backup_gate_evidence(app, _backup_gate_evidence_payload())
            with self.assertRaises(FileNotFoundError):
                store.read_backup_gate_record("backup-gate-example-site-prod")

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    async def test_backup_gate_evidence_rejects_human_session_mutation(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            oauth_config = _github_oauth_config()
            session_store = InMemoryHumanSessionStore()
            session_manager = HumanSessionManager(
                config=oauth_config,
                session_store=session_store,
            )
            human_session = session_manager.issue(_github_human_identity())
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_github_human_backup_gate_write_policy(context="example-site"),
                record_store_factory=lambda: store,
                human_session_manager=session_manager,
            )

            response = await _post_backup_gate_evidence(
                app,
                _backup_gate_evidence_payload(),
                authorization="",
                headers={"Cookie": session_manager.session_cookie_header(human_session)},
            )
            with self.assertRaises(FileNotFoundError):
                store.read_backup_gate_record("backup-gate-example-site-prod")

        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authentication_required")

    async def test_backup_gate_evidence_rejects_terminal_agent_mutation(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_terminal_agent_backup_gate_write_policy(context="example-site"),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(
                    terminal_agent_token="terminal-agent-token",
                    terminal_agent_subject="local-owner-agent",
                    terminal_agent_token_label="local-owner-read",
                ),
            )

            response = await _post_backup_gate_evidence(
                app,
                _backup_gate_evidence_payload(),
                authorization="Bearer terminal-agent-token",
            )
            with self.assertRaises(FileNotFoundError):
                store.read_backup_gate_record("backup-gate-example-site-prod")

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    async def test_backup_gate_evidence_validation_errors_use_launchplane_shape(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_backup_gate_write_identity()),
                authz_policy=_backup_gate_write_policy(context="example-site"),
                record_store_factory=lambda: store,
            )
            invalid_payload = _backup_gate_evidence_payload()
            invalid_payload["product"] = ""

            response = await _post_backup_gate_evidence(app, invalid_payload)

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertNotIn("detail", payload)

    async def test_backup_gate_evidence_replays_idempotent_write(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_backup_gate_write_identity()),
                authz_policy=_backup_gate_write_policy(context="example-site"),
                record_store_factory=lambda: store,
            )
            request_payload = _backup_gate_evidence_payload()

            first_response = await _post_backup_gate_evidence(
                app,
                request_payload,
                idempotency_key="backup-gate-example-site-prod",
            )
            second_response = await _post_backup_gate_evidence(
                app,
                request_payload,
                idempotency_key="backup-gate-example-site-prod",
            )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        first_payload = first_response.json()
        second_payload = second_response.json()
        self.assertEqual(second_payload["records"], first_payload["records"])
        self.assertTrue(second_payload["replayed"])
        self.assertEqual(second_payload["original_trace_id"], first_payload["trace_id"])
        self.assertNotEqual(second_payload["trace_id"], first_payload["trace_id"])

    async def test_backup_gate_evidence_rejects_idempotency_key_reuse(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_backup_gate_write_identity()),
                authz_policy=_backup_gate_write_policy(context="example-site"),
                record_store_factory=lambda: store,
            )
            request_payload = _backup_gate_evidence_payload()
            changed_payload = _backup_gate_evidence_payload(
                record_id="backup-gate-example-site-prod-2"
            )

            first_response = await _post_backup_gate_evidence(
                app,
                request_payload,
                idempotency_key="backup-gate-example-site-prod",
            )
            second_response = await _post_backup_gate_evidence(
                app,
                changed_payload,
                idempotency_key="backup-gate-example-site-prod",
            )
            with self.assertRaises(FileNotFoundError):
                store.read_backup_gate_record("backup-gate-example-site-prod-2")

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 409)
        payload = second_response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "idempotency_key_reused")

    async def test_openapi_includes_backup_gate_evidence_contract(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_backup_gate_write_identity()),
            authz_policy=_backup_gate_write_policy(context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        route = openapi["paths"]["/v1/evidence/backup-gates"]["post"]
        self.assertEqual(route["operationId"], "write_backup_gate_evidence")
        self.assertEqual(
            route["requestBody"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/BackupGateEvidenceRequest",
        )
        self.assertEqual(
            route["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/AcceptedEvidenceResponse",
        )
        for status_code in ("400", "401", "403", "409", "413", "503"):
            self.assertIn("LaunchplaneErrorResponse", json.dumps(route["responses"][status_code]))
        self.assertEqual(
            openapi["components"]["schemas"]["BackupGateEvidenceRequest"]["additionalProperties"],
            False,
        )

    async def test_backup_gate_evidence_native_route_precedes_wsgi_fallback(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_backup_gate_write_identity()),
                authz_policy=_backup_gate_write_policy(context="example-site"),
                record_store_factory=lambda: store,
            )
            legacy_app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=root,
            )
            app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

            response = await _post_backup_gate_evidence(app, _backup_gate_evidence_payload())

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(
            payload["records"]["backup_gate_record_id"], "backup-gate-example-site-prod"
        )


class FastApiPublicIngressMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_ingress_monitor_runs_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_public_ingress_monitor_identity()),
                authz_policy=_public_ingress_monitor_policy(),
                record_store_factory=lambda: store,
            )
            with patch("control_plane.http_app.run_public_ingress_monitor_once") as run_monitor:
                run_monitor.return_value = PublicIngressMonitorResult(
                    checked_at="2026-05-29T12:00:00Z",
                    target_count=1,
                    pass_count=1,
                    records=(),
                )

                response = await _post_public_ingress_monitor(
                    app,
                    {"schema_version": 1, "product": "launchplane", "notify": False},
                    idempotency_key="public-ingress-monitor-test",
                )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(payload["records"], {})
        self.assertEqual(payload["result"]["target_count"], 1)
        run_monitor.assert_called_once()
        self.assertIsNone(run_monitor.call_args.kwargs["notification_drivers"])

    async def test_public_ingress_monitor_wires_notification_drivers(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_public_ingress_monitor_identity()),
                authz_policy=_public_ingress_monitor_policy(),
                record_store_factory=lambda: store,
            )
            with patch("control_plane.http_app.run_public_ingress_monitor_once") as run_monitor:
                run_monitor.return_value = PublicIngressMonitorResult(
                    checked_at="2026-05-29T12:00:00Z",
                    target_count=1,
                    pass_count=1,
                    records=(),
                )

                response = await _post_public_ingress_monitor(
                    app,
                    {"schema_version": 1, "product": "launchplane", "notify": True},
                    idempotency_key="public-ingress-monitor-notify-test",
                )

        self.assertEqual(response.status_code, 202)
        run_monitor.assert_called_once()
        self.assertIsNotNone(run_monitor.call_args.kwargs["notification_drivers"])

    async def test_public_ingress_notification_drivers_resolve_lane_scoped_secrets(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                with patch.dict(
                    "os.environ",
                    {
                        control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: (
                            "test-master-key"
                        )
                    },
                    clear=True,
                ):
                    secret_result = control_plane_secrets.write_secret_value(
                        record_store=store,
                        scope="context_instance",
                        integration="public-ingress-notifications",
                        name="discord webhook",
                        plaintext_value="https://discord.com/api/webhooks/test/webhook",
                        binding_key="DISCORD_WEBHOOK",
                        context_name="example-site",
                        instance_name="prod",
                        actor="test",
                        source_label="test",
                    )
                    resolver = public_ingress_managed_secret_resolver(record_store=store)
                    incident = _public_ingress_incident(context="example-site", instance="prod")
                    other_incident = _public_ingress_incident(
                        context="example-site", instance="preview"
                    )
                    resolved_value = resolver(str(secret_result["secret_id"]), incident)
                    unresolved_value = resolver(str(secret_result["secret_id"]), other_incident)
            finally:
                store.close()

        self.assertEqual(resolved_value, "https://discord.com/api/webhooks/test/webhook")
        self.assertEqual(unresolved_value, "")

    async def test_public_ingress_monitor_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_public_ingress_monitor_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                record_store_factory=lambda: store,
            )
            with patch("control_plane.http_app.run_public_ingress_monitor_once") as run_monitor:
                response = await _post_public_ingress_monitor(
                    app,
                    {"schema_version": 1, "product": "launchplane"},
                )

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")
        run_monitor.assert_not_called()

    async def test_public_ingress_monitor_rejects_invalid_product(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_public_ingress_monitor_identity()),
                authz_policy=_public_ingress_monitor_policy(),
                record_store_factory=lambda: store,
            )
            with patch("control_plane.http_app.run_public_ingress_monitor_once") as run_monitor:
                response = await _post_public_ingress_monitor(
                    app,
                    {"schema_version": 1, "product": "other-product"},
                )

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "invalid_request")
        run_monitor.assert_not_called()

    async def test_public_ingress_monitor_requires_monitor_storage(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_public_ingress_monitor_identity()),
            authz_policy=_public_ingress_monitor_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )
        with patch("control_plane.http_app.run_public_ingress_monitor_once") as run_monitor:
            response = await _post_public_ingress_monitor(
                app,
                {"schema_version": 1, "product": "launchplane"},
            )

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "database_storage_required")
        run_monitor.assert_not_called()

    async def test_public_ingress_monitor_replays_idempotent_run(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_public_ingress_monitor_identity()),
                authz_policy=_public_ingress_monitor_policy(),
                record_store_factory=lambda: store,
            )
            with patch("control_plane.http_app.run_public_ingress_monitor_once") as run_monitor:
                run_monitor.return_value = PublicIngressMonitorResult(
                    checked_at="2026-05-29T12:00:00Z",
                    target_count=1,
                    pass_count=1,
                    records=(),
                )
                request_payload = {"schema_version": 1, "product": "launchplane"}

                first_response = await _post_public_ingress_monitor(
                    app,
                    request_payload,
                    idempotency_key="public-ingress-monitor-replay",
                )
                second_response = await _post_public_ingress_monitor(
                    app,
                    request_payload,
                    idempotency_key="public-ingress-monitor-replay",
                )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        first_payload = first_response.json()
        second_payload = second_response.json()
        self.assertTrue(second_payload["replayed"])
        self.assertEqual(second_payload["original_trace_id"], first_payload["trace_id"])
        self.assertNotEqual(second_payload["trace_id"], first_payload["trace_id"])
        run_monitor.assert_called_once()

    async def test_public_ingress_monitor_replays_before_requiring_monitor_store(
        self,
    ) -> None:
        request_payload = {"schema_version": 1, "product": "launchplane"}
        replay_store = _PublicIngressMonitorIdempotencyReplayStore(
            payload=request_payload,
            idempotency_key="public-ingress-monitor-replay-only",
        )
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_public_ingress_monitor_identity()),
            authz_policy=_public_ingress_monitor_policy(),
            record_store_factory=lambda: replay_store,
        )
        with patch("control_plane.http_app.run_public_ingress_monitor_once") as run_monitor:
            response = await _post_public_ingress_monitor(
                app,
                request_payload,
                idempotency_key="public-ingress-monitor-replay-only",
            )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertTrue(payload["replayed"])
        self.assertEqual(payload["original_trace_id"], "launchplane_req_original")
        self.assertEqual(replay_store.read_idempotency_calls, 1)
        run_monitor.assert_not_called()

    async def test_public_ingress_monitor_rejects_idempotency_key_reuse(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_public_ingress_monitor_identity()),
                authz_policy=_public_ingress_monitor_policy(),
                record_store_factory=lambda: store,
            )
            with patch("control_plane.http_app.run_public_ingress_monitor_once") as run_monitor:
                run_monitor.return_value = PublicIngressMonitorResult(
                    checked_at="2026-05-29T12:00:00Z",
                    target_count=1,
                    pass_count=1,
                    records=(),
                )

                first_response = await _post_public_ingress_monitor(
                    app,
                    {"schema_version": 1, "product": "launchplane", "notify": False},
                    idempotency_key="public-ingress-monitor-reuse",
                )
                second_response = await _post_public_ingress_monitor(
                    app,
                    {"schema_version": 1, "product": "launchplane", "notify": True},
                    idempotency_key="public-ingress-monitor-reuse",
                )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 409)
        payload = second_response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "idempotency_key_reused")
        run_monitor.assert_called_once()

    async def test_public_ingress_monitor_get_is_not_registered(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_public_ingress_monitor_identity()),
            authz_policy=_public_ingress_monitor_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )
        with patch("control_plane.http_app.run_public_ingress_monitor_once") as run_monitor:
            response = await _asgi_get(
                app,
                "/v1/products/public-ingress-monitor/run-once",
                headers={"Authorization": "Bearer valid-token"},
            )

        self.assertEqual(response.status_code, 405)
        run_monitor.assert_not_called()

    async def test_openapi_includes_public_ingress_monitor_contract(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_public_ingress_monitor_identity()),
            authz_policy=_public_ingress_monitor_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        route = openapi["paths"]["/v1/products/public-ingress-monitor/run-once"]
        self.assertEqual(set(route.keys()), {"post"})
        self.assertEqual(route["post"]["operationId"], "run_public_ingress_monitor")
        self.assertEqual(
            route["post"]["requestBody"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/PublicIngressMonitorRunOnceRequest",
        )
        self.assertEqual(
            route["post"]["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/AcceptedEvidenceResponse",
        )
        self.assertEqual(
            openapi["components"]["schemas"]["PublicIngressMonitorRunOnceRequest"][
                "additionalProperties"
            ],
            False,
        )

    async def test_public_ingress_monitor_native_route_precedes_wsgi_fallback(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_public_ingress_monitor_identity()),
                authz_policy=_public_ingress_monitor_policy(),
                record_store_factory=lambda: store,
            )
            legacy_app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=root,
            )
            app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))
            with patch("control_plane.http_app.run_public_ingress_monitor_once") as run_monitor:
                run_monitor.return_value = PublicIngressMonitorResult(
                    checked_at="2026-05-29T12:00:00Z",
                    target_count=1,
                    pass_count=1,
                    records=(),
                )

                response = await _post_public_ingress_monitor(
                    app,
                    {"schema_version": 1, "product": "launchplane", "notify": False},
                )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "accepted")
        run_monitor.assert_called_once()


class FastApiPromotionEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_promotion_evidence_writes_record_and_inventory_for_authorized_workflow(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = _promotion_evidence_store(state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_promotion_write_identity()),
                authz_policy=_promotion_write_policy(context="example-site"),
                record_store_factory=lambda: store,
            )

            response = await _post_promotion_evidence(app, _promotion_evidence_payload())
            promotion = store.read_promotion_record("promotion-example-site-testing-to-prod")
            inventory = store.read_environment_inventory(
                context_name="example-site",
                instance_name="prod",
            )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(
            payload["records"],
            {
                "promotion_record_id": "promotion-example-site-testing-to-prod",
                "inventory_record_id": "example-site-prod",
            },
        )
        self.assertNotIn("replayed", payload)
        self.assertEqual(promotion.context, "example-site")
        self.assertEqual(promotion.from_instance, "testing")
        self.assertEqual(promotion.to_instance, "prod")
        self.assertEqual(promotion.deploy.status, "pass")
        self.assertEqual(promotion.backup_gate.status, "pass")
        self.assertEqual(inventory.deployment_record_id, "deployment-example-site-prod")
        self.assertEqual(inventory.promotion_record_id, promotion.record_id)
        self.assertEqual(inventory.promoted_from_instance, "testing")

    async def test_promotion_evidence_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = _promotion_evidence_store(state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_promotion_write_identity()),
                authz_policy=_promotion_write_policy(context="other-site"),
                record_store_factory=lambda: store,
            )

            response = await _post_promotion_evidence(app, _promotion_evidence_payload())
            with self.assertRaises(FileNotFoundError):
                store.read_promotion_record("promotion-example-site-testing-to-prod")

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    async def test_promotion_evidence_rejects_human_session_mutation(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = _promotion_evidence_store(state_dir)
            oauth_config = _github_oauth_config()
            session_store = InMemoryHumanSessionStore()
            session_manager = HumanSessionManager(
                config=oauth_config,
                session_store=session_store,
            )
            human_session = session_manager.issue(_github_human_identity())
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_github_human_promotion_write_policy(context="example-site"),
                record_store_factory=lambda: store,
                human_session_manager=session_manager,
            )

            response = await _post_promotion_evidence(
                app,
                _promotion_evidence_payload(),
                authorization="",
                headers={"Cookie": session_manager.session_cookie_header(human_session)},
            )
            with self.assertRaises(FileNotFoundError):
                store.read_promotion_record("promotion-example-site-testing-to-prod")

        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authentication_required")

    async def test_promotion_evidence_rejects_terminal_agent_mutation(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = _promotion_evidence_store(state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_terminal_agent_promotion_write_policy(context="example-site"),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(
                    terminal_agent_token="terminal-agent-token",
                    terminal_agent_subject="local-owner-agent",
                    terminal_agent_token_label="local-owner-read",
                ),
            )

            response = await _post_promotion_evidence(
                app,
                _promotion_evidence_payload(),
                authorization="Bearer terminal-agent-token",
            )
            with self.assertRaises(FileNotFoundError):
                store.read_promotion_record("promotion-example-site-testing-to-prod")

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    async def test_promotion_evidence_validation_errors_use_launchplane_shape(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = _promotion_evidence_store(Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_promotion_write_identity()),
                authz_policy=_promotion_write_policy(context="example-site"),
                record_store_factory=lambda: store,
            )
            invalid_payload = _promotion_evidence_payload()
            invalid_payload["product"] = ""

            response = await _post_promotion_evidence(app, invalid_payload)

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertNotIn("detail", payload)

    async def test_promotion_evidence_rejects_mismatched_linked_deployment(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = _promotion_evidence_store(state_dir, deployment_instance="testing")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_promotion_write_identity()),
                authz_policy=_promotion_write_policy(context="example-site"),
                record_store_factory=lambda: store,
            )

            response = await _post_promotion_evidence(app, _promotion_evidence_payload())
            with self.assertRaises(FileNotFoundError):
                store.read_promotion_record("promotion-example-site-testing-to-prod")

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "invalid_request")

    async def test_promotion_evidence_rejects_context_mismatched_linked_deployment(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = _promotion_evidence_store(state_dir, deployment_context="other-site")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_promotion_write_identity()),
                authz_policy=_promotion_write_policy(context="example-site"),
                record_store_factory=lambda: store,
            )

            response = await _post_promotion_evidence(app, _promotion_evidence_payload())
            with self.assertRaises(FileNotFoundError):
                store.read_promotion_record("promotion-example-site-testing-to-prod")

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "invalid_request")

    async def test_promotion_evidence_rejects_missing_linked_deployment(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_promotion_write_identity()),
                authz_policy=_promotion_write_policy(context="example-site"),
                record_store_factory=lambda: store,
            )

            response = await _post_promotion_evidence(app, _promotion_evidence_payload())
            with self.assertRaises(FileNotFoundError):
                store.read_promotion_record("promotion-example-site-testing-to-prod")

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "invalid_request")

    async def test_promotion_evidence_replays_idempotent_write(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = _promotion_evidence_store(state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_promotion_write_identity()),
                authz_policy=_promotion_write_policy(context="example-site"),
                record_store_factory=lambda: store,
            )
            request_payload = _promotion_evidence_payload()

            first_response = await _post_promotion_evidence(
                app,
                request_payload,
                idempotency_key="promotion-example-site-testing-to-prod",
            )
            second_response = await _post_promotion_evidence(
                app,
                request_payload,
                idempotency_key="promotion-example-site-testing-to-prod",
            )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        first_payload = first_response.json()
        second_payload = second_response.json()
        self.assertEqual(second_payload["records"], first_payload["records"])
        self.assertTrue(second_payload["replayed"])
        self.assertEqual(second_payload["original_trace_id"], first_payload["trace_id"])
        self.assertNotEqual(second_payload["trace_id"], first_payload["trace_id"])

    async def test_promotion_evidence_rejects_idempotency_key_reuse(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = _promotion_evidence_store(state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_promotion_write_identity()),
                authz_policy=_promotion_write_policy(context="example-site"),
                record_store_factory=lambda: store,
            )
            request_payload = _promotion_evidence_payload()
            changed_payload = _promotion_evidence_payload(
                record_id="promotion-example-site-testing-to-prod-2"
            )

            first_response = await _post_promotion_evidence(
                app,
                request_payload,
                idempotency_key="promotion-example-site-testing-to-prod",
            )
            second_response = await _post_promotion_evidence(
                app,
                changed_payload,
                idempotency_key="promotion-example-site-testing-to-prod",
            )
            with self.assertRaises(FileNotFoundError):
                store.read_promotion_record("promotion-example-site-testing-to-prod-2")

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 409)
        payload = second_response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "idempotency_key_reused")

    async def test_openapi_includes_promotion_evidence_contract(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_promotion_write_identity()),
            authz_policy=_promotion_write_policy(context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        route = openapi["paths"]["/v1/evidence/promotions"]["post"]
        self.assertEqual(route["operationId"], "write_promotion_evidence")
        request_schema = route["requestBody"]["content"]["application/json"]["schema"]
        self.assertEqual(request_schema["$ref"], "#/components/schemas/PromotionEvidenceRequest")
        success_schema = route["responses"]["202"]["content"]["application/json"]["schema"]
        self.assertEqual(success_schema["$ref"], "#/components/schemas/AcceptedEvidenceResponse")
        for status_code in ("400", "401", "403", "409", "413", "503"):
            error_schema = route["responses"][status_code]["content"]["application/json"]["schema"]
            self.assertEqual(error_schema["$ref"], "#/components/schemas/LaunchplaneErrorResponse")
        promotion_schema = openapi["components"]["schemas"]["PromotionEvidenceRequest"]
        self.assertFalse(promotion_schema["additionalProperties"])

    async def test_promotion_evidence_native_route_precedes_wsgi_fallback(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = _promotion_evidence_store(state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_promotion_write_identity()),
                authz_policy=_promotion_write_policy(context="example-site"),
                record_store_factory=lambda: store,
            )
            legacy_app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=root,
            )
            app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

            response = await _post_promotion_evidence(app, _promotion_evidence_payload())

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(
            payload["records"]["promotion_record_id"], "promotion-example-site-testing-to-prod"
        )


class FastApiPreviewGenerationEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_generation_evidence_writes_records_for_authorized_workflow(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_preview_generation_write_identity()),
                authz_policy=_preview_generation_write_policy(context="example-site"),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            response = await _post_preview_generation_evidence(
                app,
                _preview_generation_evidence_payload(),
            )
            preview = store.read_preview_record("preview-example-site-example-site-pr-42")
            generation = store.read_preview_generation_record(
                "preview-example-site-example-site-pr-42-generation-0001"
            )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(
            payload["records"]["preview_id"],
            "preview-example-site-example-site-pr-42",
        )
        self.assertEqual(
            payload["records"]["generation_id"],
            "preview-example-site-example-site-pr-42-generation-0001",
        )
        self.assertEqual(payload["records"]["transition"], "ready")
        self.assertNotIn("replayed", payload)
        self.assertEqual(preview.state, "active")
        self.assertEqual(preview.serving_generation_id, generation.generation_id)
        self.assertEqual(generation.state, "ready")
        self.assertEqual(generation.artifact_id, "ghcr.io/every/example-site:pr-42-abcdef")

    async def test_preview_generation_evidence_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_preview_generation_write_identity()),
                authz_policy=_preview_generation_write_policy(context="other-site"),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            response = await _post_preview_generation_evidence(
                app,
                _preview_generation_evidence_payload(),
            )
            self.assertEqual(store.list_preview_records(), ())

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    async def test_preview_generation_evidence_rejects_human_session_mutation(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            oauth_config = _github_oauth_config()
            session_store = InMemoryHumanSessionStore()
            session_manager = HumanSessionManager(
                config=oauth_config,
                session_store=session_store,
            )
            human_session = session_manager.issue(_github_human_identity())
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_github_human_preview_generation_write_policy(context="example-site"),
                record_store_factory=lambda: store,
                human_session_manager=session_manager,
                control_plane_root_path=root,
            )

            response = await _post_preview_generation_evidence(
                app,
                _preview_generation_evidence_payload(),
                authorization="",
                headers={"Cookie": session_manager.session_cookie_header(human_session)},
            )
            self.assertEqual(store.list_preview_records(), ())

        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authentication_required")

    async def test_preview_generation_evidence_rejects_terminal_agent_mutation(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_terminal_agent_preview_generation_write_policy(
                    context="example-site"
                ),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(
                    terminal_agent_token="terminal-agent-token",
                    terminal_agent_subject="local-owner-agent",
                    terminal_agent_token_label="local-owner-read",
                ),
                control_plane_root_path=root,
            )

            response = await _post_preview_generation_evidence(
                app,
                _preview_generation_evidence_payload(),
                authorization="Bearer terminal-agent-token",
            )
            self.assertEqual(store.list_preview_records(), ())

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    async def test_preview_generation_evidence_validation_errors_use_launchplane_shape(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_preview_generation_write_identity()),
                authz_policy=_preview_generation_write_policy(context="example-site"),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            invalid_payload = _preview_generation_evidence_payload()
            invalid_payload["product"] = ""

            response = await _post_preview_generation_evidence(app, invalid_payload)

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertNotIn("detail", payload)

    async def test_preview_generation_evidence_rejects_mismatched_generation_context(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_preview_generation_write_identity()),
                authz_policy=_preview_generation_write_policy(context="example-site"),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            invalid_payload = _preview_generation_evidence_payload()
            generation = cast(dict[str, object], invalid_payload["generation"])
            generation["context"] = "other-site"

            response = await _post_preview_generation_evidence(app, invalid_payload)

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertEqual(store.list_preview_records(), ())

    async def test_preview_generation_evidence_replays_idempotent_write(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_preview_generation_write_identity()),
                authz_policy=_preview_generation_write_policy(context="example-site"),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            request_payload = _preview_generation_evidence_payload()

            first_response = await _post_preview_generation_evidence(
                app,
                request_payload,
                idempotency_key="preview-generation-example-site-pr-42",
            )
            second_response = await _post_preview_generation_evidence(
                app,
                request_payload,
                idempotency_key="preview-generation-example-site-pr-42",
            )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        first_payload = first_response.json()
        second_payload = second_response.json()
        self.assertEqual(second_payload["records"], first_payload["records"])
        self.assertTrue(second_payload["replayed"])
        self.assertEqual(second_payload["original_trace_id"], first_payload["trace_id"])
        self.assertNotEqual(second_payload["trace_id"], first_payload["trace_id"])

    async def test_preview_generation_evidence_retry_after_idempotency_write_failure_converges(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = _FailingOnceIdempotencyPreviewGenerationStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_preview_generation_write_identity()),
                authz_policy=_preview_generation_write_policy(context="example-site"),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            request_payload = _preview_generation_evidence_payload()

            with self.assertRaises(RuntimeError):
                await _post_preview_generation_evidence(
                    app,
                    request_payload,
                    idempotency_key="preview-generation-example-site-pr-42",
                )
            generations_after_failure = store.list_preview_generation_records(
                preview_id="preview-example-site-example-site-pr-42"
            )

            retry_response = await _post_preview_generation_evidence(
                app,
                request_payload,
                idempotency_key="preview-generation-example-site-pr-42",
            )
            generations_after_retry = store.list_preview_generation_records(
                preview_id="preview-example-site-example-site-pr-42"
            )

        self.assertEqual(
            [record.generation_id for record in generations_after_failure],
            ["preview-example-site-example-site-pr-42-generation-0001"],
        )
        self.assertEqual(retry_response.status_code, 202)
        retry_payload = retry_response.json()
        self.assertEqual(
            retry_payload["records"]["generation_id"],
            "preview-example-site-example-site-pr-42-generation-0001",
        )
        self.assertNotIn("replayed", retry_payload)
        self.assertEqual(
            [record.generation_id for record in generations_after_retry],
            ["preview-example-site-example-site-pr-42-generation-0001"],
        )

    async def test_preview_generation_evidence_rejects_idempotency_key_reuse(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_preview_generation_write_identity()),
                authz_policy=_preview_generation_write_policy(context="example-site"),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            request_payload = _preview_generation_evidence_payload()
            changed_payload = _preview_generation_evidence_payload(anchor_pr_number=43)

            first_response = await _post_preview_generation_evidence(
                app,
                request_payload,
                idempotency_key="preview-generation-example-site-pr-42",
            )
            second_response = await _post_preview_generation_evidence(
                app,
                changed_payload,
                idempotency_key="preview-generation-example-site-pr-42",
            )
            preview_count = len(store.list_preview_records())

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 409)
        payload = second_response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "idempotency_key_reused")
        self.assertEqual(preview_count, 1)

    async def test_openapi_includes_preview_generation_evidence_contract(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_preview_generation_write_identity()),
            authz_policy=_preview_generation_write_policy(context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        route = openapi["paths"]["/v1/evidence/previews/generations"]["post"]
        self.assertEqual(route["operationId"], "write_preview_generation_evidence")
        request_schema = route["requestBody"]["content"]["application/json"]["schema"]
        self.assertEqual(
            request_schema["$ref"], "#/components/schemas/PreviewGenerationEvidenceEnvelope"
        )
        success_schema = route["responses"]["202"]["content"]["application/json"]["schema"]
        self.assertEqual(success_schema["$ref"], "#/components/schemas/AcceptedEvidenceResponse")
        for status_code in ("400", "401", "403", "409", "413", "503"):
            error_schema = route["responses"][status_code]["content"]["application/json"]["schema"]
            self.assertEqual(error_schema["$ref"], "#/components/schemas/LaunchplaneErrorResponse")
        envelope_schema = openapi["components"]["schemas"]["PreviewGenerationEvidenceEnvelope"]
        self.assertFalse(envelope_schema["additionalProperties"])

    async def test_preview_generation_evidence_native_route_precedes_wsgi_fallback(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_preview_generation_write_identity()),
                authz_policy=_preview_generation_write_policy(context="example-site"),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            legacy_app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=root,
            )
            app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

            response = await _post_preview_generation_evidence(
                app,
                _preview_generation_evidence_payload(),
            )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(
            payload["records"]["generation_id"],
            "preview-example-site-example-site-pr-42-generation-0001",
        )


class FastApiPreviewDestroyedEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_destroyed_evidence_writes_record_for_authorized_workflow(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_preview_record(_preview_record_for_destroy())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_preview_destroyed_write_identity()),
                authz_policy=_preview_destroyed_write_policy(context="example-site"),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            response = await _post_preview_destroyed_evidence(
                app,
                _preview_destroyed_evidence_payload(),
            )
            preview = store.read_preview_record("preview-example-site-example-site-pr-42")

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(
            payload["records"]["preview_id"],
            "preview-example-site-example-site-pr-42",
        )
        self.assertEqual(payload["records"]["transition"], "destroyed")
        self.assertNotIn("replayed", payload)
        self.assertEqual(preview.state, "destroyed")
        self.assertEqual(preview.destroyed_at, "2026-04-16T09:04:00Z")
        self.assertEqual(preview.destroy_reason, "external_preview_cleanup_completed")

    async def test_preview_destroyed_evidence_rejects_unauthorized_workflow(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_preview_record(_preview_record_for_destroy())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_preview_destroyed_write_identity()),
                authz_policy=_preview_destroyed_write_policy(context="other-site"),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            response = await _post_preview_destroyed_evidence(
                app,
                _preview_destroyed_evidence_payload(),
            )
            preview = store.read_preview_record("preview-example-site-example-site-pr-42")

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")
        self.assertEqual(preview.state, "active")

    async def test_preview_destroyed_evidence_requires_bearer_token(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_preview_destroyed_write_identity()),
                authz_policy=_preview_destroyed_write_policy(context="example-site"),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            response = await _post_preview_destroyed_evidence(
                app,
                _preview_destroyed_evidence_payload(),
                authorization="",
            )

        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authentication_required")

    async def test_preview_destroyed_evidence_rejects_human_session_mutation(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            oauth_config = _github_oauth_config()
            session_store = InMemoryHumanSessionStore()
            session_manager = HumanSessionManager(
                config=oauth_config,
                session_store=session_store,
            )
            human_session = session_manager.issue(_github_human_identity())
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_github_human_preview_destroyed_write_policy(context="example-site"),
                record_store_factory=lambda: store,
                human_session_manager=session_manager,
                control_plane_root_path=root,
            )

            response = await _post_preview_destroyed_evidence(
                app,
                _preview_destroyed_evidence_payload(),
                authorization="",
                headers={"Cookie": session_manager.session_cookie_header(human_session)},
            )
            self.assertEqual(store.list_preview_records(), ())

        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authentication_required")

    async def test_preview_destroyed_evidence_rejects_terminal_agent_mutation(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_terminal_agent_preview_destroyed_write_policy(context="example-site"),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(
                    terminal_agent_token="terminal-agent-token",
                    terminal_agent_subject="local-owner-agent",
                    terminal_agent_token_label="local-owner-read",
                ),
                control_plane_root_path=root,
            )

            response = await _post_preview_destroyed_evidence(
                app,
                _preview_destroyed_evidence_payload(),
                authorization="Bearer terminal-agent-token",
            )
            self.assertEqual(store.list_preview_records(), ())

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    async def test_preview_destroyed_evidence_validation_errors_use_launchplane_shape(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_preview_destroyed_write_identity()),
                authz_policy=_preview_destroyed_write_policy(context="example-site"),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            invalid_payload = _preview_destroyed_evidence_payload()
            invalid_payload["product"] = ""

            response = await _post_preview_destroyed_evidence(app, invalid_payload)

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertNotIn("detail", payload)

    async def test_preview_destroyed_evidence_rejects_missing_preview(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_preview_destroyed_write_identity()),
                authz_policy=_preview_destroyed_write_policy(context="example-site"),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            response = await _post_preview_destroyed_evidence(
                app,
                _preview_destroyed_evidence_payload(),
            )

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "invalid_request")

    async def test_preview_destroyed_evidence_replays_idempotent_write(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_preview_record(_preview_record_for_destroy())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_preview_destroyed_write_identity()),
                authz_policy=_preview_destroyed_write_policy(context="example-site"),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            request_payload = _preview_destroyed_evidence_payload()

            first_response = await _post_preview_destroyed_evidence(
                app,
                request_payload,
                idempotency_key="preview-destroyed-example-site-pr-42",
            )
            second_response = await _post_preview_destroyed_evidence(
                app,
                request_payload,
                idempotency_key="preview-destroyed-example-site-pr-42",
            )
            preview_count = len(store.list_preview_records())

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        first_payload = first_response.json()
        second_payload = second_response.json()
        self.assertEqual(second_payload["records"], first_payload["records"])
        self.assertTrue(second_payload["replayed"])
        self.assertEqual(second_payload["original_trace_id"], first_payload["trace_id"])
        self.assertNotEqual(second_payload["trace_id"], first_payload["trace_id"])
        self.assertEqual(preview_count, 1)

    async def test_preview_destroyed_evidence_rejects_idempotency_key_reuse(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_preview_record(_preview_record_for_destroy())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_preview_destroyed_write_identity()),
                authz_policy=_preview_destroyed_write_policy(context="example-site"),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            request_payload = _preview_destroyed_evidence_payload()
            changed_payload = _preview_destroyed_evidence_payload(anchor_pr_number=43)

            first_response = await _post_preview_destroyed_evidence(
                app,
                request_payload,
                idempotency_key="preview-destroyed-example-site-pr-42",
            )
            second_response = await _post_preview_destroyed_evidence(
                app,
                changed_payload,
                idempotency_key="preview-destroyed-example-site-pr-42",
            )
            preview_count = len(store.list_preview_records())

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 409)
        payload = second_response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "idempotency_key_reused")
        self.assertEqual(preview_count, 1)

    async def test_openapi_includes_preview_destroyed_evidence_contract(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_preview_destroyed_write_identity()),
            authz_policy=_preview_destroyed_write_policy(context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        route = openapi["paths"]["/v1/evidence/previews/destroyed"]["post"]
        self.assertEqual(route["operationId"], "write_preview_destroyed_evidence")
        request_schema = route["requestBody"]["content"]["application/json"]["schema"]
        self.assertEqual(
            request_schema["$ref"], "#/components/schemas/PreviewDestroyedEvidenceEnvelope"
        )
        success_schema = route["responses"]["202"]["content"]["application/json"]["schema"]
        self.assertEqual(success_schema["$ref"], "#/components/schemas/AcceptedEvidenceResponse")
        for status_code in ("400", "401", "403", "409", "413", "503"):
            error_schema = route["responses"][status_code]["content"]["application/json"]["schema"]
            self.assertEqual(error_schema["$ref"], "#/components/schemas/LaunchplaneErrorResponse")
        envelope_schema = openapi["components"]["schemas"]["PreviewDestroyedEvidenceEnvelope"]
        self.assertFalse(envelope_schema["additionalProperties"])

    async def test_preview_destroyed_evidence_native_route_precedes_wsgi_fallback(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_record(_preview_record_for_destroy())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_preview_destroyed_write_identity()),
                authz_policy=_preview_destroyed_write_policy(context="example-site"),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            legacy_app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=root,
            )
            app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

            response = await _post_preview_destroyed_evidence(
                app,
                _preview_destroyed_evidence_payload(),
            )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(payload["records"]["transition"], "destroyed")


class FastApiRunnerHostHygieneAuditEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_runner_host_hygiene_audit_evidence_writes_record_for_authorized_workflow(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_runner_host_hygiene_audit_write_identity()),
                authz_policy=_runner_host_hygiene_audit_write_policy(),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            response = await _post_runner_host_hygiene_audit_evidence(
                app,
                _runner_host_hygiene_audit_payload(),
            )
            records = store.list_runner_host_hygiene_audit_records(
                host_name="chris-testing",
                status="planned",
            )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(
            payload["records"],
            {
                "runner_host_hygiene_audit_record_key": (
                    "runner-host-hygiene/2026-05-23/chris-testing"
                ),
            },
        )
        self.assertEqual(
            payload["result"]["runner_host_hygiene_audit_record_key"],
            "runner-host-hygiene/2026-05-23/chris-testing",
        )
        self.assertEqual(payload["result"]["host_name"], "chris-testing")
        self.assertEqual(payload["result"]["audit_status"], "planned")
        self.assertFalse(payload["result"]["mutate"])
        self.assertEqual(payload["result"]["audit"]["status"], "planned")
        self.assertEqual(len(records), 1)
        self.assertEqual(
            records[0].audit_record_key,
            "runner-host-hygiene/2026-05-23/chris-testing",
        )
        self.assertFalse(records[0].request.mutate)

    async def test_runner_host_hygiene_audit_evidence_rejects_unauthorized_workflow(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_runner_host_hygiene_audit_write_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            response = await _post_runner_host_hygiene_audit_evidence(
                app,
                _runner_host_hygiene_audit_payload(),
            )
            records = store.list_runner_host_hygiene_audit_records()

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")
        self.assertEqual(records, ())

    async def test_runner_host_hygiene_audit_evidence_requires_bearer_token(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_runner_host_hygiene_audit_write_identity()),
                authz_policy=_runner_host_hygiene_audit_write_policy(),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            response = await _post_runner_host_hygiene_audit_evidence(
                app,
                _runner_host_hygiene_audit_payload(),
                authorization="",
            )

        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authentication_required")

    async def test_runner_host_hygiene_audit_evidence_rejects_human_session_mutation(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            oauth_config = _github_oauth_config()
            session_store = InMemoryHumanSessionStore()
            session_manager = HumanSessionManager(
                config=oauth_config,
                session_store=session_store,
            )
            human_session = session_manager.issue(_github_human_identity())
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_github_human_runner_host_hygiene_audit_write_policy(),
                record_store_factory=lambda: store,
                human_session_manager=session_manager,
                control_plane_root_path=root,
            )

            response = await _post_runner_host_hygiene_audit_evidence(
                app,
                _runner_host_hygiene_audit_payload(),
                authorization="",
                headers={"Cookie": session_manager.session_cookie_header(human_session)},
            )
            records = store.list_runner_host_hygiene_audit_records()

        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authentication_required")
        self.assertEqual(records, ())

    async def test_runner_host_hygiene_audit_evidence_rejects_terminal_agent_mutation(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_terminal_agent_runner_host_hygiene_audit_write_policy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(
                    terminal_agent_token="terminal-agent-token",
                    terminal_agent_subject="local-owner-agent",
                    terminal_agent_token_label="local-owner-read",
                ),
                control_plane_root_path=root,
            )

            response = await _post_runner_host_hygiene_audit_evidence(
                app,
                _runner_host_hygiene_audit_payload(),
                authorization="Bearer terminal-agent-token",
            )
            records = store.list_runner_host_hygiene_audit_records()

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")
        self.assertEqual(records, ())

    async def test_runner_host_hygiene_audit_evidence_rejects_non_launchplane_product(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_runner_host_hygiene_audit_write_identity()),
                authz_policy=_runner_host_hygiene_audit_write_policy(),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            response = await _post_runner_host_hygiene_audit_evidence(
                app,
                _runner_host_hygiene_audit_payload(product="odoo"),
            )

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertNotIn("detail", payload)

    async def test_runner_host_hygiene_audit_evidence_replays_idempotent_write(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_runner_host_hygiene_audit_write_identity()),
                authz_policy=_runner_host_hygiene_audit_write_policy(),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            request_payload = _runner_host_hygiene_audit_payload()

            first_response = await _post_runner_host_hygiene_audit_evidence(
                app,
                request_payload,
                idempotency_key="runner-host-hygiene:chris-testing:planned",
            )
            second_response = await _post_runner_host_hygiene_audit_evidence(
                app,
                request_payload,
                idempotency_key="runner-host-hygiene:chris-testing:planned",
            )
            records = store.list_runner_host_hygiene_audit_records(
                host_name="chris-testing",
                status="planned",
            )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        first_payload = first_response.json()
        second_payload = second_response.json()
        self.assertEqual(second_payload["records"], first_payload["records"])
        self.assertEqual(second_payload["result"], first_payload["result"])
        self.assertTrue(second_payload["replayed"])
        self.assertEqual(second_payload["original_trace_id"], first_payload["trace_id"])
        self.assertEqual(len(records), 1)

    async def test_runner_host_hygiene_audit_evidence_rejects_idempotency_key_reuse(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_runner_host_hygiene_audit_write_identity()),
                authz_policy=_runner_host_hygiene_audit_write_policy(),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            request_payload = _runner_host_hygiene_audit_payload()
            changed_payload = _runner_host_hygiene_audit_payload(
                audit_record_key="runner-host-hygiene/2026-05-23/chris-testing-retry"
            )

            first_response = await _post_runner_host_hygiene_audit_evidence(
                app,
                request_payload,
                idempotency_key="runner-host-hygiene:chris-testing:planned",
            )
            second_response = await _post_runner_host_hygiene_audit_evidence(
                app,
                changed_payload,
                idempotency_key="runner-host-hygiene:chris-testing:planned",
            )
            records = store.list_runner_host_hygiene_audit_records()

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 409)
        payload = second_response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "idempotency_key_reused")
        self.assertEqual(len(records), 1)

    async def test_openapi_includes_runner_host_hygiene_audit_evidence_contract(
        self,
    ) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_runner_host_hygiene_audit_write_identity()),
            authz_policy=_runner_host_hygiene_audit_write_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        route = openapi["paths"]["/v1/evidence/runner-host-hygiene/audits"]["post"]
        self.assertEqual(route["operationId"], "write_runner_host_hygiene_audit_evidence")
        request_schema = route["requestBody"]["content"]["application/json"]["schema"]
        self.assertEqual(
            request_schema["$ref"],
            "#/components/schemas/RunnerHostHygieneAuditEvidenceEnvelope",
        )
        success_schema = route["responses"]["202"]["content"]["application/json"]["schema"]
        self.assertEqual(success_schema["$ref"], "#/components/schemas/AcceptedEvidenceResponse")
        for status_code in ("400", "401", "403", "409", "413", "503"):
            error_schema = route["responses"][status_code]["content"]["application/json"]["schema"]
            self.assertEqual(error_schema["$ref"], "#/components/schemas/LaunchplaneErrorResponse")
        envelope_schema = openapi["components"]["schemas"]["RunnerHostHygieneAuditEvidenceEnvelope"]
        self.assertFalse(envelope_schema["additionalProperties"])

    async def test_runner_host_hygiene_audit_evidence_native_route_precedes_wsgi_fallback(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_runner_host_hygiene_audit_write_identity()),
                authz_policy=_runner_host_hygiene_audit_write_policy(),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            legacy_app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=root,
            )
            app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

            response = await _post_runner_host_hygiene_audit_evidence(
                app,
                _runner_host_hygiene_audit_payload(),
            )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(
            payload["records"]["runner_host_hygiene_audit_record_key"],
            "runner-host-hygiene/2026-05-23/chris-testing",
        )


class FastApiRunnerLaneRegistrationAuditEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_runner_lane_registration_audit_evidence_writes_record_for_authorized_workflow(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_runner_lane_registration_audit_write_identity()),
                authz_policy=_runner_lane_registration_audit_write_policy(),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            response = await _post_runner_lane_registration_audit_evidence(
                app,
                _runner_lane_registration_audit_payload(),
            )
            records = store.list_runner_lane_registration_audit_records(
                repository="cbusillo/odoo-tenant-cm-website",
                host_name="chris-testing",
                status="planned",
            )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(
            payload["records"],
            {
                "runner_lane_registration_audit_record_key": (
                    "runner-lane-registration/2026-06-08/cm-website/dry-run"
                ),
            },
        )
        self.assertEqual(
            payload["result"]["runner_lane_registration_audit_record_key"],
            "runner-lane-registration/2026-06-08/cm-website/dry-run",
        )
        self.assertEqual(payload["result"]["repository"], "cbusillo/odoo-tenant-cm-website")
        self.assertEqual(payload["result"]["host_name"], "chris-testing")
        self.assertEqual(payload["result"]["lane_name"], "cm-website-runner-1")
        self.assertEqual(payload["result"]["audit_status"], "planned")
        self.assertFalse(payload["result"]["mutate"])
        self.assertEqual(payload["result"]["audit"]["status"], "planned")
        self.assertEqual(len(records), 1)
        self.assertEqual(
            records[0].audit_record_key,
            "runner-lane-registration/2026-06-08/cm-website/dry-run",
        )
        self.assertFalse(records[0].request.mutate)

    async def test_runner_lane_registration_audit_evidence_rejects_unauthorized_workflow(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_runner_lane_registration_audit_write_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            response = await _post_runner_lane_registration_audit_evidence(
                app,
                _runner_lane_registration_audit_payload(),
            )
            records = store.list_runner_lane_registration_audit_records()

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")
        self.assertEqual(records, ())

    async def test_runner_lane_registration_audit_evidence_requires_bearer_token(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_runner_lane_registration_audit_write_identity()),
                authz_policy=_runner_lane_registration_audit_write_policy(),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            response = await _post_runner_lane_registration_audit_evidence(
                app,
                _runner_lane_registration_audit_payload(),
                authorization="",
            )

        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authentication_required")

    async def test_runner_lane_registration_audit_evidence_rejects_human_session_mutation(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            oauth_config = _github_oauth_config()
            session_store = InMemoryHumanSessionStore()
            session_manager = HumanSessionManager(
                config=oauth_config,
                session_store=session_store,
            )
            human_session = session_manager.issue(_github_human_identity())
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_github_human_runner_lane_registration_audit_write_policy(),
                record_store_factory=lambda: store,
                human_session_manager=session_manager,
                control_plane_root_path=root,
            )

            response = await _post_runner_lane_registration_audit_evidence(
                app,
                _runner_lane_registration_audit_payload(),
                authorization="",
                headers={"Cookie": session_manager.session_cookie_header(human_session)},
            )
            records = store.list_runner_lane_registration_audit_records()

        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authentication_required")
        self.assertEqual(records, ())

    async def test_runner_lane_registration_audit_evidence_rejects_terminal_agent_mutation(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_terminal_agent_runner_lane_registration_audit_write_policy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(
                    terminal_agent_token="terminal-agent-token",
                    terminal_agent_subject="local-owner-agent",
                    terminal_agent_token_label="local-owner-read",
                ),
                control_plane_root_path=root,
            )

            response = await _post_runner_lane_registration_audit_evidence(
                app,
                _runner_lane_registration_audit_payload(),
                authorization="Bearer terminal-agent-token",
            )
            records = store.list_runner_lane_registration_audit_records()

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")
        self.assertEqual(records, ())

    async def test_runner_lane_registration_audit_evidence_rejects_non_launchplane_product(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_runner_lane_registration_audit_write_identity()),
                authz_policy=_runner_lane_registration_audit_write_policy(),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )

            response = await _post_runner_lane_registration_audit_evidence(
                app,
                _runner_lane_registration_audit_payload(product="odoo"),
            )

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertNotIn("detail", payload)

    async def test_runner_lane_registration_audit_evidence_replays_idempotent_write(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_runner_lane_registration_audit_write_identity()),
                authz_policy=_runner_lane_registration_audit_write_policy(),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            request_payload = _runner_lane_registration_audit_payload()

            first_response = await _post_runner_lane_registration_audit_evidence(
                app,
                request_payload,
                idempotency_key="runner-lane-registration:cm-website:planned",
            )
            second_response = await _post_runner_lane_registration_audit_evidence(
                app,
                request_payload,
                idempotency_key="runner-lane-registration:cm-website:planned",
            )
            records = store.list_runner_lane_registration_audit_records(
                repository="cbusillo/odoo-tenant-cm-website",
                host_name="chris-testing",
                status="planned",
            )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        first_payload = first_response.json()
        second_payload = second_response.json()
        self.assertEqual(second_payload["records"], first_payload["records"])
        self.assertEqual(second_payload["result"], first_payload["result"])
        self.assertTrue(second_payload["replayed"])
        self.assertEqual(second_payload["original_trace_id"], first_payload["trace_id"])
        self.assertEqual(len(records), 1)

    async def test_runner_lane_registration_audit_evidence_rejects_idempotency_key_reuse(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_runner_lane_registration_audit_write_identity()),
                authz_policy=_runner_lane_registration_audit_write_policy(),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            request_payload = _runner_lane_registration_audit_payload()
            changed_payload = _runner_lane_registration_audit_payload(
                audit_record_key="runner-lane-registration/2026-06-08/cm-website/retry"
            )

            first_response = await _post_runner_lane_registration_audit_evidence(
                app,
                request_payload,
                idempotency_key="runner-lane-registration:cm-website:planned",
            )
            second_response = await _post_runner_lane_registration_audit_evidence(
                app,
                changed_payload,
                idempotency_key="runner-lane-registration:cm-website:planned",
            )
            records = store.list_runner_lane_registration_audit_records()

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 409)
        payload = second_response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "idempotency_key_reused")
        self.assertEqual(len(records), 1)

    async def test_openapi_includes_runner_lane_registration_audit_evidence_contract(
        self,
    ) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_runner_lane_registration_audit_write_identity()),
            authz_policy=_runner_lane_registration_audit_write_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        route = openapi["paths"]["/v1/evidence/runner-lane-registration/audits"]["post"]
        self.assertEqual(route["operationId"], "write_runner_lane_registration_audit_evidence")
        request_schema = route["requestBody"]["content"]["application/json"]["schema"]
        self.assertEqual(
            request_schema["$ref"],
            "#/components/schemas/RunnerLaneRegistrationAuditEvidenceEnvelope",
        )
        success_schema = route["responses"]["202"]["content"]["application/json"]["schema"]
        self.assertEqual(success_schema["$ref"], "#/components/schemas/AcceptedEvidenceResponse")
        for status_code in ("400", "401", "403", "409", "413", "503"):
            error_schema = route["responses"][status_code]["content"]["application/json"]["schema"]
            self.assertEqual(error_schema["$ref"], "#/components/schemas/LaunchplaneErrorResponse")
        envelope_schema = openapi["components"]["schemas"][
            "RunnerLaneRegistrationAuditEvidenceEnvelope"
        ]
        self.assertFalse(envelope_schema["additionalProperties"])

    async def test_runner_lane_registration_audit_evidence_native_route_precedes_wsgi_fallback(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_runner_lane_registration_audit_write_identity()),
                authz_policy=_runner_lane_registration_audit_write_policy(),
                record_store_factory=lambda: store,
                control_plane_root_path=root,
            )
            legacy_app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=root,
            )
            app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

            response = await _post_runner_lane_registration_audit_evidence(
                app,
                _runner_lane_registration_audit_payload(),
            )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(
            payload["records"]["runner_lane_registration_audit_record_key"],
            "runner-lane-registration/2026-06-08/cm-website/dry-run",
        )


class FastApiDeploymentEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_deployment_evidence_writes_record_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_deployment_write_identity()),
                authz_policy=_deployment_write_policy(context="example-site"),
                record_store_factory=lambda: store,
            )

            response = await _post_deployment_evidence(app, _deployment_evidence_payload())
            deployment = store.read_deployment_record("deployment-example-site-prod")
            inventory = store.read_environment_inventory(
                context_name="example-site",
                instance_name="prod",
            )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(
            payload["records"],
            {
                "deployment_record_id": "deployment-example-site-prod",
                "inventory_record_id": "example-site-prod",
            },
        )
        self.assertNotIn("replayed", payload)
        self.assertEqual(deployment.context, "example-site")
        self.assertEqual(deployment.instance, "prod")
        self.assertEqual(deployment.deploy.status, "pass")
        self.assertEqual(inventory.deployment_record_id, deployment.record_id)

    async def test_deployment_evidence_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_deployment_write_identity()),
                authz_policy=_deployment_write_policy(context="other-site"),
                record_store_factory=lambda: store,
            )

            response = await _post_deployment_evidence(app, _deployment_evidence_payload())
            with self.assertRaises(FileNotFoundError):
                store.read_deployment_record("deployment-example-site-prod")

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    async def test_deployment_evidence_rejects_human_session_mutation(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            oauth_config = _github_oauth_config()
            session_store = InMemoryHumanSessionStore()
            session_manager = HumanSessionManager(
                config=oauth_config,
                session_store=session_store,
            )
            human_session = session_manager.issue(_github_human_identity())
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_github_human_deployment_write_policy(context="example-site"),
                record_store_factory=lambda: store,
                human_session_manager=session_manager,
            )

            response = await _post_deployment_evidence(
                app,
                _deployment_evidence_payload(),
                authorization="",
                headers={"Cookie": session_manager.session_cookie_header(human_session)},
            )
            with self.assertRaises(FileNotFoundError):
                store.read_deployment_record("deployment-example-site-prod")

        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authentication_required")

    async def test_deployment_evidence_rejects_terminal_agent_mutation(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_terminal_agent_deployment_write_policy(context="example-site"),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(
                    terminal_agent_token="terminal-agent-token",
                    terminal_agent_subject="local-owner-agent",
                    terminal_agent_token_label="local-owner-read",
                ),
            )

            response = await _post_deployment_evidence(
                app,
                _deployment_evidence_payload(),
                authorization="Bearer terminal-agent-token",
            )
            with self.assertRaises(FileNotFoundError):
                store.read_deployment_record("deployment-example-site-prod")

        self.assertEqual(response.status_code, 403)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    async def test_deployment_evidence_validation_errors_use_launchplane_shape(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_deployment_write_identity()),
                authz_policy=_deployment_write_policy(context="example-site"),
                record_store_factory=lambda: store,
            )
            invalid_payload = _deployment_evidence_payload()
            invalid_payload["product"] = ""

            response = await _post_deployment_evidence(app, invalid_payload)

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertNotIn("detail", payload)

    async def test_deployment_evidence_requires_json_content_type(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_deployment_write_identity()),
            authz_policy=_deployment_write_policy(context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_request(
            app,
            "POST",
            "/v1/evidence/deployments",
            headers={
                "Authorization": "Bearer valid-token",
                "Content-Type": "text/plain",
            },
            raw_body=json.dumps(_deployment_evidence_payload()).encode("utf-8"),
        )

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertEqual(
            payload["error"]["message"],
            "Evidence ingress requests require Content-Type: application/json.",
        )

    async def test_deployment_evidence_accepts_json_content_type_with_charset(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_deployment_write_identity()),
                authz_policy=_deployment_write_policy(context="example-site"),
                record_store_factory=lambda: store,
            )

            response = await _asgi_request(
                app,
                "POST",
                "/v1/evidence/deployments",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Content-Type": "application/json; charset=utf-8",
                },
                raw_body=json.dumps(_deployment_evidence_payload()).encode("utf-8"),
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "accepted")

    async def test_deployment_evidence_rejects_invalid_content_length(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_deployment_write_identity()),
            authz_policy=_deployment_write_policy(context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_request(
            app,
            "POST",
            "/v1/evidence/deployments",
            headers={
                "Authorization": "Bearer valid-token",
                "Content-Type": "application/json",
                "Content-Length": "not-a-number",
            },
            raw_body=json.dumps(_deployment_evidence_payload()).encode("utf-8"),
        )

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertEqual(
            payload["error"]["message"],
            "Evidence ingress Content-Length must be an unsigned decimal integer.",
        )

    async def test_deployment_evidence_rejects_empty_content_length(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_deployment_write_identity()),
            authz_policy=_deployment_write_policy(context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_request(
            app,
            "POST",
            "/v1/evidence/deployments",
            headers={
                "Authorization": "Bearer valid-token",
                "Content-Type": "application/json",
                "Content-Length": "   ",
            },
            raw_body=json.dumps(_deployment_evidence_payload()).encode("utf-8"),
        )

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertEqual(
            payload["error"]["message"],
            "Evidence ingress Content-Length must be an unsigned decimal integer.",
        )

    async def test_deployment_evidence_rejects_plus_prefixed_content_length(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_deployment_write_identity()),
            authz_policy=_deployment_write_policy(context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_request(
            app,
            "POST",
            "/v1/evidence/deployments",
            headers={
                "Authorization": "Bearer valid-token",
                "Content-Type": "application/json",
                "Content-Length": "+2",
            },
            raw_body=json.dumps(_deployment_evidence_payload()).encode("utf-8"),
        )

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertEqual(
            payload["error"]["message"],
            "Evidence ingress Content-Length must be an unsigned decimal integer.",
        )

    async def test_deployment_evidence_rejects_duplicate_content_length(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_deployment_write_identity()),
            authz_policy=_deployment_write_policy(context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_request(
            app,
            "POST",
            "/v1/evidence/deployments",
            headers={
                "Authorization": "Bearer valid-token",
                "Content-Type": "application/json",
                "Content-Length": "2",
            },
            extra_headers=[("Content-Length", "200")],
            raw_body=json.dumps(_deployment_evidence_payload()).encode("utf-8"),
        )

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertEqual(
            payload["error"]["message"],
            "Evidence ingress requests require exactly one Content-Length header.",
        )

    async def test_deployment_evidence_rejects_negative_content_length(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_deployment_write_identity()),
            authz_policy=_deployment_write_policy(context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_request(
            app,
            "POST",
            "/v1/evidence/deployments",
            headers={
                "Authorization": "Bearer valid-token",
                "Content-Type": "application/json",
                "Content-Length": "-1",
            },
            raw_body=json.dumps(_deployment_evidence_payload()).encode("utf-8"),
        )

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertEqual(
            payload["error"]["message"],
            "Evidence ingress Content-Length must be an unsigned decimal integer.",
        )

    async def test_deployment_evidence_requires_bounded_content_length(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_deployment_write_identity()),
            authz_policy=_deployment_write_policy(context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_request(
            app,
            "POST",
            "/v1/evidence/deployments",
            headers={
                "Authorization": "Bearer valid-token",
                "Content-Type": "application/json",
            },
            raw_body=json.dumps(_deployment_evidence_payload()).encode("utf-8"),
            set_content_length=False,
        )

        self.assertEqual(response.status_code, 413)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "request_entity_too_large")
        self.assertEqual(
            payload["error"]["message"],
            "Evidence ingress requests require a bounded Content-Length.",
        )

    async def test_deployment_evidence_rejects_duplicate_chunked_transfer_encoding(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_deployment_write_identity()),
            authz_policy=_deployment_write_policy(context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_request(
            app,
            "POST",
            "/v1/evidence/deployments",
            headers={
                "Authorization": "Bearer valid-token",
                "Content-Type": "application/json",
                "Transfer-Encoding": "identity",
            },
            extra_headers=[("Transfer-Encoding", "chunked")],
            raw_body=json.dumps(_deployment_evidence_payload()).encode("utf-8"),
            set_content_length=False,
        )

        self.assertEqual(response.status_code, 413)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "request_entity_too_large")
        self.assertEqual(
            payload["error"]["message"],
            "Evidence ingress requests require a bounded Content-Length.",
        )

    async def test_deployment_evidence_rejects_chunked_transfer_encoding(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_deployment_write_identity()),
            authz_policy=_deployment_write_policy(context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_request(
            app,
            "POST",
            "/v1/evidence/deployments",
            headers={
                "Authorization": "Bearer valid-token",
                "Content-Type": "application/json",
                "Transfer-Encoding": "chunked",
            },
            raw_body=json.dumps(_deployment_evidence_payload()).encode("utf-8"),
            set_content_length=False,
        )

        self.assertEqual(response.status_code, 413)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "request_entity_too_large")
        self.assertEqual(
            payload["error"]["message"],
            "Evidence ingress requests require a bounded Content-Length.",
        )

    async def test_deployment_evidence_rejects_oversized_content_length(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_deployment_write_identity()),
            authz_policy=_deployment_write_policy(context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_request(
            app,
            "POST",
            "/v1/evidence/deployments",
            headers={
                "Authorization": "Bearer valid-token",
                "Content-Type": "application/json",
                "Content-Length": str(2 * 1024 * 1024 + 1),
            },
            raw_body=b"{}",
        )

        self.assertEqual(response.status_code, 413)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "request_entity_too_large")
        self.assertEqual(payload["error"]["message"], "Evidence ingress request body is too large.")

    async def test_deployment_evidence_rejects_stream_body_over_limit(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_deployment_write_identity()),
            authz_policy=_deployment_write_policy(context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_request(
            app,
            "POST",
            "/v1/evidence/deployments",
            headers={
                "Authorization": "Bearer valid-token",
                "Content-Type": "application/json",
                "Content-Length": "2",
            },
            raw_body=b"{" + (b" " * (2 * 1024 * 1024 + 1)),
        )

        self.assertEqual(response.status_code, 413)
        payload = response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "request_entity_too_large")
        self.assertEqual(payload["error"]["message"], "Evidence ingress request body is too large.")

    async def test_deployment_evidence_replays_idempotent_write(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_deployment_write_identity()),
                authz_policy=_deployment_write_policy(context="example-site"),
                record_store_factory=lambda: store,
            )
            request_payload = _deployment_evidence_payload()

            first_response = await _post_deployment_evidence(
                app,
                request_payload,
                idempotency_key="deployment-example-site-prod",
            )
            second_response = await _post_deployment_evidence(
                app,
                request_payload,
                idempotency_key="deployment-example-site-prod",
            )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        first_payload = first_response.json()
        second_payload = second_response.json()
        self.assertEqual(second_payload["records"], first_payload["records"])
        self.assertTrue(second_payload["replayed"])
        self.assertEqual(second_payload["original_trace_id"], first_payload["trace_id"])
        self.assertNotEqual(second_payload["trace_id"], first_payload["trace_id"])

    async def test_deployment_evidence_rejects_idempotency_key_reuse(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_deployment_write_identity()),
                authz_policy=_deployment_write_policy(context="example-site"),
                record_store_factory=lambda: store,
            )
            request_payload = _deployment_evidence_payload()
            changed_payload = _deployment_evidence_payload(
                record_id="deployment-example-site-prod-2"
            )

            first_response = await _post_deployment_evidence(
                app,
                request_payload,
                idempotency_key="deployment-example-site-prod",
            )
            second_response = await _post_deployment_evidence(
                app,
                changed_payload,
                idempotency_key="deployment-example-site-prod",
            )
            with self.assertRaises(FileNotFoundError):
                store.read_deployment_record("deployment-example-site-prod-2")

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 409)
        payload = second_response.json()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "idempotency_key_reused")

    async def test_openapi_includes_deployment_evidence_contract(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_deployment_write_identity()),
            authz_policy=_deployment_write_policy(context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        route = openapi["paths"]["/v1/evidence/deployments"]["post"]
        self.assertEqual(route["operationId"], "write_deployment_evidence")
        self.assertEqual(
            route["requestBody"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/DeploymentEvidenceRequest",
        )
        self.assertEqual(
            route["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/AcceptedEvidenceResponse",
        )
        for status_code in ("400", "401", "403", "409", "413", "503"):
            self.assertIn("LaunchplaneErrorResponse", json.dumps(route["responses"][status_code]))
        self.assertEqual(
            openapi["components"]["schemas"]["DeploymentEvidenceRequest"]["additionalProperties"],
            False,
        )

    async def test_deployment_evidence_native_route_precedes_wsgi_fallback(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_deployment_write_identity()),
                authz_policy=_deployment_write_policy(context="example-site"),
                record_store_factory=lambda: store,
            )
            legacy_app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=root,
            )
            app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

            response = await _post_deployment_evidence(app, _deployment_evidence_payload())

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(payload["records"]["deployment_record_id"], "deployment-example-site-prod")


def _product_environment_read_policy(
    *,
    context: str = "launchplane",
    contexts: tuple[str, ...] | None = None,
    products: tuple[str, ...] = ("example-site",),
) -> LaunchplaneAuthzPolicy:
    allowed_contexts = contexts if contexts is not None else (context,)
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "every/verireel",
                    "workflow_refs": [
                        "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                    ],
                    "event_names": ["pull_request"],
                    "products": list(products),
                    "contexts": list(allowed_contexts),
                    "actions": ["product_environment.read"],
                }
            ]
        }
    )


def _work_graph_read_policy(
    *,
    products: tuple[str, ...] = ("launchplane", "example-site"),
    contexts: tuple[str, ...] = ("launchplane", "example-site"),
) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "every/verireel",
                    "workflow_refs": [
                        "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                    ],
                    "event_names": ["pull_request"],
                    "products": list(products),
                    "contexts": list(contexts),
                    "actions": ["work_graph.rank", "product_environment.read"],
                }
            ]
        }
    )


def _github_human_work_graph_rank_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_humans": [
                {
                    "logins": ["example-operator"],
                    "roles": ["admin"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["work_graph.rank"],
                }
            ]
        }
    )


def _terminal_agent_work_graph_rank_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "terminal_agents": [
                {
                    "subjects": ["local-owner-agent"],
                    "token_labels": ["local-owner-read"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["work_graph.rank"],
                }
            ]
        }
    )


def _local_operator_work_graph_rank_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "local_operators": [
                {
                    "subjects": ["local-owner-agent"],
                    "token_labels": ["local-owner-read"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["work_graph.rank"],
                }
            ]
        }
    )


def _local_admin_work_graph_rank_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "local_admins": [
                {
                    "subjects": ["local-owner-agent"],
                    "token_labels": ["local-owner-admin"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["work_graph.rank"],
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


def _backup_gate_write_identity() -> GitHubActionsIdentity:
    return _identity(
        repository="every/example-site",
        workflow_ref="every/example-site/.github/workflows/backup-gate.yml@refs/heads/main",
        event_name="workflow_dispatch",
    )


def _backup_gate_write_policy(*, context: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "every/example-site",
                    "workflow_refs": [
                        "every/example-site/.github/workflows/backup-gate.yml@refs/heads/main"
                    ],
                    "event_names": ["workflow_dispatch"],
                    "products": ["example-site"],
                    "contexts": [context],
                    "actions": ["backup_gate.write"],
                }
            ]
        }
    )


def _github_human_backup_gate_write_policy(*, context: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_humans": [
                {
                    "logins": ["example-operator"],
                    "roles": ["admin"],
                    "products": ["example-site"],
                    "contexts": [context],
                    "actions": ["backup_gate.write"],
                }
            ]
        }
    )


def _terminal_agent_backup_gate_write_policy(*, context: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "terminal_agents": [
                {
                    "subjects": ["local-owner-agent"],
                    "token_labels": ["local-owner-read"],
                    "products": ["example-site"],
                    "contexts": [context],
                    "actions": ["backup_gate.write"],
                }
            ]
        }
    )


def _backup_gate_evidence_payload(
    *, record_id: str = "backup-gate-example-site-prod"
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "product": "example-site",
        "backup_gate": {
            "record_id": record_id,
            "context": "example-site",
            "instance": "prod",
            "created_at": "2026-04-21T18:05:00Z",
            "source": "example-site-prod-gate",
            "status": "pass",
            "evidence": {
                "snapshot_name": "snapshot-example-site-prod",
                "manifest_path": "scratch/prod-gates/snapshot-example-site-prod.json",
            },
        },
    }


def _promotion_write_identity() -> GitHubActionsIdentity:
    return _identity(
        repository="every/example-site",
        workflow_ref="every/example-site/.github/workflows/promote-prod.yml@refs/heads/main",
        event_name="workflow_dispatch",
    )


def _promotion_write_policy(*, context: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "every/example-site",
                    "workflow_refs": [
                        "every/example-site/.github/workflows/promote-prod.yml@refs/heads/main"
                    ],
                    "event_names": ["workflow_dispatch"],
                    "products": ["example-site"],
                    "contexts": [context],
                    "actions": ["promotion.write"],
                }
            ]
        }
    )


def _public_ingress_monitor_identity() -> GitHubActionsIdentity:
    return _identity(
        repository="cbusillo/launchplane",
        workflow_ref="cbusillo/launchplane/.github/workflows/public-ingress-monitor.yml@refs/heads/main",
        event_name="schedule",
    )


def _public_ingress_monitor_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "cbusillo/launchplane",
                    "workflow_refs": [
                        "cbusillo/launchplane/.github/workflows/public-ingress-monitor.yml@refs/heads/main"
                    ],
                    "event_names": ["schedule"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["public_ingress_monitor.run_once"],
                }
            ]
        }
    )


def _public_ingress_incident(
    *, context: str = "launchplane", instance: str = "prod"
) -> PublicIngressIncidentRecord:
    return PublicIngressIncidentRecord(
        incident_id=f"public-ingress-incident-{context}-{instance}",
        product="launchplane",
        context=context,
        instance=instance,
        status="open",
        opened_at="2026-05-29T12:00:00Z",
        opened_observation_id="public-ingress-observation-opened",
        latest_observation_id="public-ingress-observation-latest",
        latest_observed_at="2026-05-29T12:00:00Z",
        failure_code="http_error",
        summary="Public ingress failed.",
    )


def _github_human_promotion_write_policy(*, context: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_humans": [
                {
                    "logins": ["example-operator"],
                    "roles": ["admin"],
                    "products": ["example-site"],
                    "contexts": [context],
                    "actions": ["promotion.write"],
                }
            ]
        }
    )


def _terminal_agent_promotion_write_policy(*, context: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "terminal_agents": [
                {
                    "subjects": ["local-owner-agent"],
                    "token_labels": ["local-owner-read"],
                    "products": ["example-site"],
                    "contexts": [context],
                    "actions": ["promotion.write"],
                }
            ]
        }
    )


def _promotion_evidence_payload(
    *,
    record_id: str = "promotion-example-site-testing-to-prod",
    link_deployment: bool = True,
) -> dict[str, object]:
    promotion: dict[str, object] = {
        "record_id": record_id,
        "artifact_identity": {"artifact_id": "artifact-example-site-prod"},
        "backup_record_id": "backup-example-site-prod-20260420T155000Z",
        "context": "example-site",
        "from_instance": "testing",
        "to_instance": "prod",
        "backup_gate": {
            "required": True,
            "status": "pass",
            "evidence": {"recorded_by": "launchplane-service"},
        },
        "deploy": {
            "target_name": "example-site-prod",
            "target_type": "application",
            "deploy_mode": "runtime-provider-api",
            "deployment_id": "provider-deployment-example-site-prod",
            "status": "pass",
            "started_at": "2026-04-20T16:05:00Z",
            "finished_at": "2026-04-20T16:08:30Z",
        },
        "destination_health": {
            "verified": True,
            "urls": ["https://example.invalid/health"],
            "timeout_seconds": 45,
            "status": "pass",
        },
    }
    if link_deployment:
        promotion["deployment_record_id"] = "deployment-example-site-prod"
    return {
        "schema_version": 1,
        "product": "example-site",
        "promotion": promotion,
    }


def _promotion_evidence_store(
    state_dir: Path,
    *,
    deployment_context: str = "example-site",
    deployment_instance: str = "prod",
    artifact_id: str = "artifact-example-site-prod",
) -> FilesystemRecordStore:
    store = FilesystemRecordStore(state_dir=state_dir)
    store.write_deployment_record(
        DeploymentRecord(
            record_id="deployment-example-site-prod",
            artifact_identity=ArtifactIdentityReference(artifact_id=artifact_id),
            context=deployment_context,
            instance=deployment_instance,
            source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
            resolved_target=ResolvedTargetEvidence(
                target_type="application",
                target_id="target-example-site-prod",
                target_name="example-site-prod",
            ),
            deploy=DeploymentEvidence(
                target_name="example-site-prod",
                target_type="application",
                deploy_mode="runtime-provider-api",
                deployment_id="provider-deployment-example-site-prod",
                status="pass",
                started_at="2026-04-20T16:05:00Z",
                finished_at="2026-04-20T16:08:30Z",
            ),
        )
    )
    return store


def _preview_generation_write_identity() -> GitHubActionsIdentity:
    return _identity(
        repository="every/example-site",
        workflow_ref="every/example-site/.github/workflows/preview-control-plane.yml@refs/heads/main",
        event_name="pull_request",
    )


def _preview_generation_write_policy(*, context: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "every/example-site",
                    "workflow_refs": [
                        "every/example-site/.github/workflows/preview-control-plane.yml@refs/heads/main"
                    ],
                    "event_names": ["pull_request"],
                    "products": ["example-site"],
                    "contexts": [context],
                    "actions": ["preview_generation.write"],
                }
            ]
        }
    )


def _github_human_preview_generation_write_policy(*, context: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_humans": [
                {
                    "logins": ["example-operator"],
                    "roles": ["admin"],
                    "products": ["example-site"],
                    "contexts": [context],
                    "actions": ["preview_generation.write"],
                }
            ]
        }
    )


def _terminal_agent_preview_generation_write_policy(*, context: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "terminal_agents": [
                {
                    "subjects": ["local-owner-agent"],
                    "token_labels": ["local-owner-read"],
                    "products": ["example-site"],
                    "contexts": [context],
                    "actions": ["preview_generation.write"],
                }
            ]
        }
    )


def _preview_destroyed_write_identity() -> GitHubActionsIdentity:
    return _identity(
        repository="every/example-site",
        workflow_ref="every/example-site/.github/workflows/preview-control-plane.yml@refs/heads/main",
        event_name="pull_request",
    )


def _preview_destroyed_write_policy(*, context: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "every/example-site",
                    "workflow_refs": [
                        "every/example-site/.github/workflows/preview-control-plane.yml@refs/heads/main"
                    ],
                    "event_names": ["pull_request"],
                    "products": ["example-site"],
                    "contexts": [context],
                    "actions": ["preview_destroyed.write"],
                }
            ]
        }
    )


def _github_human_preview_destroyed_write_policy(*, context: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_humans": [
                {
                    "logins": ["example-operator"],
                    "roles": ["admin"],
                    "products": ["example-site"],
                    "contexts": [context],
                    "actions": ["preview_destroyed.write"],
                }
            ]
        }
    )


def _terminal_agent_preview_destroyed_write_policy(*, context: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "terminal_agents": [
                {
                    "subjects": ["local-owner-agent"],
                    "token_labels": ["local-owner-read"],
                    "products": ["example-site"],
                    "contexts": [context],
                    "actions": ["preview_destroyed.write"],
                }
            ]
        }
    )


def _preview_generation_evidence_payload(*, anchor_pr_number: int = 42) -> dict[str, object]:
    pr_url = f"https://github.com/every/example-site/pull/{anchor_pr_number}"
    return {
        "schema_version": 1,
        "product": "example-site",
        "preview": {
            "schema_version": 1,
            "context": "example-site",
            "anchor_repo": "example-site",
            "anchor_pr_number": anchor_pr_number,
            "anchor_pr_url": pr_url,
            "canonical_url": f"https://pr-{anchor_pr_number}.example.invalid",
            "state": "active",
            "updated_at": "2026-04-16T08:10:00Z",
            "eligible_at": "2026-04-16T08:10:00Z",
        },
        "generation": {
            "schema_version": 1,
            "context": "example-site",
            "anchor_repo": "example-site",
            "anchor_pr_number": anchor_pr_number,
            "anchor_pr_url": pr_url,
            "anchor_head_sha": "abcdef1234567890abcdef1234567890abcdef12",
            "state": "ready",
            "requested_reason": "external_preview_refresh",
            "requested_at": "2026-04-16T08:02:00Z",
            "ready_at": "2026-04-16T08:10:00Z",
            "finished_at": "2026-04-16T08:10:00Z",
            "resolved_manifest_fingerprint": f"example-preview-pr-{anchor_pr_number}-abcdef",
            "artifact_id": "ghcr.io/every/example-site:pr-42-abcdef",
            "deploy_status": "pass",
            "verify_status": "pass",
            "overall_health_status": "pass",
        },
    }


def _preview_record_for_destroy(*, anchor_pr_number: int = 42) -> PreviewRecord:
    preview_id = f"preview-example-site-example-site-pr-{anchor_pr_number}"
    return PreviewRecord(
        preview_id=preview_id,
        context="example-site",
        anchor_repo="example-site",
        anchor_pr_number=anchor_pr_number,
        anchor_pr_url=f"https://github.com/every/example-site/pull/{anchor_pr_number}",
        preview_label=f"example-site/example-site/pr-{anchor_pr_number}",
        canonical_url=f"https://pr-{anchor_pr_number}.example.invalid",
        state="active",
        created_at="2026-04-16T08:00:00Z",
        updated_at="2026-04-16T08:10:00Z",
        eligible_at="2026-04-16T08:00:00Z",
        active_generation_id=f"{preview_id}-generation-0001",
        serving_generation_id=f"{preview_id}-generation-0001",
        latest_generation_id=f"{preview_id}-generation-0001",
        latest_manifest_fingerprint=f"example-preview-pr-{anchor_pr_number}-abcdef",
    )


def _preview_read_record(*, anchor_pr_number: int = 42) -> PreviewRecord:
    return _preview_record_for_destroy(anchor_pr_number=anchor_pr_number)


def _preview_generation_read_record(
    *,
    anchor_pr_number: int = 42,
    sequence: int = 1,
    state: PreviewGenerationState = "ready",
) -> PreviewGenerationRecord:
    preview_id = f"preview-example-site-example-site-pr-{anchor_pr_number}"
    generation_id = f"{preview_id}-generation-{sequence:04d}"
    return PreviewGenerationRecord(
        generation_id=generation_id,
        preview_id=preview_id,
        sequence=sequence,
        state=state,
        requested_reason="external_preview_refresh",
        requested_at="2026-04-16T08:02:00Z",
        ready_at="2026-04-16T08:10:00Z" if state == "ready" else "",
        finished_at="2026-04-16T08:10:00Z" if state == "ready" else "",
        resolved_manifest_fingerprint=f"example-preview-pr-{anchor_pr_number}-abcdef",
        artifact_id=f"ghcr.io/every/example-site:pr-{anchor_pr_number}-abcdef",
        anchor_summary=PreviewPullRequestSummary(
            repo="example-site",
            pr_number=anchor_pr_number,
            head_sha="abcdef1234567890abcdef1234567890abcdef12",
            pr_url=f"https://github.com/every/example-site/pull/{anchor_pr_number}",
        ),
        deploy_status="pass" if state == "ready" else "pending",
        verify_status="pass" if state == "ready" else "pending",
        overall_health_status="pass" if state == "ready" else "pending",
    )


def _write_recent_operations_records(store: FilesystemRecordStore) -> None:
    store.write_environment_inventory(_environment_inventory_read_record())
    store.write_deployment_record(_deployment_read_record())
    store.write_promotion_record(_promotion_read_record())
    store.write_preview_record(_preview_read_record())


def _write_secret_status_records(database_url: str) -> dict[str, str]:
    store = PostgresRecordStore(database_url=database_url)
    store.ensure_schema()
    with patch.dict(
        "os.environ",
        {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
        clear=True,
    ):
        global_secret = control_plane_secrets.write_secret_value(
            record_store=store,
            scope="global",
            integration=control_plane_secrets.DOKPLOY_SECRET_INTEGRATION,
            name="token",
            plaintext_value="global-token",
            binding_key="DOKPLOY_TOKEN",
            actor="test",
        )
        context_secret = control_plane_secrets.write_secret_value(
            record_store=store,
            scope="context",
            integration=control_plane_secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
            name="GITHUB_WEBHOOK_SECRET",
            plaintext_value="plain-secret-value-alpha",
            binding_key="GITHUB_WEBHOOK_SECRET",
            context_name="example-site",
            actor="test",
        )
        instance_secret = control_plane_secrets.write_secret_value(
            record_store=store,
            scope="context_instance",
            integration=control_plane_secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
            name="SMTP_PASSWORD",
            plaintext_value="plain-secret-value-beta",
            binding_key="SMTP_PASSWORD",
            context_name="example-site",
            instance_name="prod",
            actor="test",
        )
        other_instance_secret = control_plane_secrets.write_secret_value(
            record_store=store,
            scope="context_instance",
            integration=control_plane_secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
            name="SMTP_PASSWORD",
            plaintext_value="plain-secret-value-gamma",
            binding_key="SMTP_PASSWORD",
            context_name="example-site",
            instance_name="testing",
            actor="test",
        )
    store.close()
    return {
        "global": str(global_secret["secret_id"]),
        "context": str(context_secret["secret_id"]),
        "instance": str(instance_secret["secret_id"]),
        "other_instance": str(other_instance_secret["secret_id"]),
    }


def _preview_destroyed_evidence_payload(*, anchor_pr_number: int = 42) -> dict[str, object]:
    return {
        "schema_version": 1,
        "product": "example-site",
        "destroy": {
            "schema_version": 1,
            "context": "example-site",
            "anchor_repo": "example-site",
            "anchor_pr_number": anchor_pr_number,
            "destroyed_at": "2026-04-16T09:04:00Z",
            "destroy_reason": "external_preview_cleanup_completed",
        },
    }


def _runner_host_hygiene_audit_write_identity() -> GitHubActionsIdentity:
    return _identity(
        repository="cbusillo/launchplane",
        workflow_ref=(
            "cbusillo/launchplane/.github/workflows/runner-host-hygiene.yml@refs/heads/main"
        ),
        event_name="workflow_dispatch",
    )


def _runner_host_hygiene_audit_write_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "cbusillo/launchplane",
                    "workflow_refs": [
                        "cbusillo/launchplane/.github/workflows/runner-host-hygiene.yml@refs/heads/main"
                    ],
                    "event_names": ["workflow_dispatch"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["runner_host_hygiene_audit.write"],
                }
            ]
        }
    )


def _github_human_runner_host_hygiene_audit_write_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_humans": [
                {
                    "logins": ["example-operator"],
                    "roles": ["admin"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["runner_host_hygiene_audit.write"],
                }
            ]
        }
    )


def _terminal_agent_runner_host_hygiene_audit_write_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "terminal_agents": [
                {
                    "subjects": ["local-owner-agent"],
                    "token_labels": ["local-owner-read"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["runner_host_hygiene_audit.write"],
                }
            ]
        }
    )


def _runner_host_hygiene_audit_payload(
    *,
    audit_record_key: str = "runner-host-hygiene/2026-05-23/chris-testing",
    product: str = "launchplane",
) -> dict[str, object]:
    report = evaluate_runner_host_hygiene(
        policy=RunnerHostHygienePolicy(required_warm_builders=("odoo-docker-chris-testing",)),
        observation=RunnerHostHygieneObservation(
            host_name="chris-testing",
            observed_at="2026-05-23T13:00:00Z",
            free_disk_bytes=500,
            warm_builders=("odoo-docker-chris-testing",),
        ),
    )
    request = RunnerHostHygieneApplyRequest(
        action="prune_docker_cache",
        host_name="chris-testing",
        mutate=False,
        retained_warm_builders=("odoo-docker-chris-testing",),
        audit_record_key=audit_record_key,
    )
    plan = plan_runner_host_hygiene_apply(
        policy=RunnerHostHygieneApplyPolicy(
            approved_hosts=("chris-testing",),
            required_retained_warm_builders=("odoo-docker-chris-testing",),
            allow_docker_cache_prune=True,
        ),
        request=request,
        report=report,
    )
    audit_record = RunnerHostHygieneApplyAuditRecord(
        audit_record_key=audit_record_key,
        status="planned",
        request=request,
        plan=plan,
        pre_apply_report=report,
        message="planned runner host hygiene apply; no host mutation was executed",
    )
    return {
        "schema_version": 1,
        "product": product,
        "audit": audit_record.model_dump(mode="json"),
    }


def _runner_lane_registration_audit_write_identity() -> GitHubActionsIdentity:
    return _identity(
        repository="cbusillo/launchplane",
        workflow_ref=(
            "cbusillo/launchplane/.github/workflows/runner-lane-registration.yml@refs/heads/main"
        ),
        event_name="workflow_dispatch",
    )


def _runner_lane_registration_audit_write_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "cbusillo/launchplane",
                    "workflow_refs": [
                        "cbusillo/launchplane/.github/workflows/runner-lane-registration.yml@refs/heads/main"
                    ],
                    "event_names": ["workflow_dispatch"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["runner_lane_registration_audit.write"],
                }
            ]
        }
    )


def _github_human_runner_lane_registration_audit_write_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_humans": [
                {
                    "logins": ["example-operator"],
                    "roles": ["admin"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["runner_lane_registration_audit.write"],
                }
            ]
        }
    )


def _terminal_agent_runner_lane_registration_audit_write_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "terminal_agents": [
                {
                    "subjects": ["local-owner-agent"],
                    "token_labels": ["local-owner-read"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["runner_lane_registration_audit.write"],
                }
            ]
        }
    )


def _runner_lane_registration_audit_payload(
    *,
    audit_record_key: str = "runner-lane-registration/2026-06-08/cm-website/dry-run",
    product: str = "launchplane",
) -> dict[str, object]:
    inventory = build_runner_lane_inventory(
        repository="cbusillo/odoo-tenant-cm-website",
        observed_at="2026-06-08T17:30:00Z",
        lanes=(),
    )
    request = RunnerLaneRegistrationRequest(
        repository="cbusillo/odoo-tenant-cm-website",
        host_name="chris-testing",
        lane_name="cm-website-runner-1",
        registration_root="/opt/actions-runners",
        labels=("self-hosted", "launchplane", "launchplane-managed"),
        mutate=False,
        audit_record_key=audit_record_key,
    )
    plan = plan_runner_lane_registration(
        policy=RunnerLaneRegistrationPolicy(
            allowed_repositories=("cbusillo/odoo-tenant-cm-website",),
            approved_hosts=("chris-testing",),
            allowed_registration_roots=("/opt/actions-runners",),
        ),
        request=request,
        inventory=inventory,
    )
    audit_record = RunnerLaneRegistrationAuditRecord(
        audit_record_key=audit_record_key,
        status="planned",
        request=request,
        plan=plan,
        pre_inventory=inventory,
        message="planned runner lane registration; no host mutation was executed",
    )
    return {
        "schema_version": 1,
        "product": product,
        "audit": audit_record.model_dump(mode="json"),
    }


def _deployment_write_identity() -> GitHubActionsIdentity:
    return _identity(
        repository="every/example-site",
        workflow_ref="every/example-site/.github/workflows/deploy-prod.yml@refs/heads/main",
        event_name="workflow_dispatch",
    )


def _deployment_write_policy(*, context: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "every/example-site",
                    "workflow_refs": [
                        "every/example-site/.github/workflows/deploy-prod.yml@refs/heads/main"
                    ],
                    "event_names": ["workflow_dispatch"],
                    "products": ["example-site"],
                    "contexts": [context],
                    "actions": ["deployment.write"],
                }
            ]
        }
    )


def _record_read_policy(
    *,
    action: str,
    context: str,
    extra_actions: tuple[str, ...] = (),
) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "every/verireel",
                    "workflow_refs": [
                        "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                    ],
                    "event_names": ["pull_request"],
                    "products": ["launchplane"],
                    "contexts": [context],
                    "actions": [action, *extra_actions],
                }
            ]
        }
    )


def _private_health_endpoint_read_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "every/verireel",
                    "workflow_refs": [
                        "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                    ],
                    "event_names": ["pull_request"],
                    "products": ["repairshopr-sync"],
                    "contexts": ["repairshopr-sync"],
                    "actions": ["private_health_endpoint.read"],
                }
            ]
        }
    )


def _github_human_deployment_write_policy(*, context: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_humans": [
                {
                    "logins": ["example-operator"],
                    "roles": ["admin"],
                    "products": ["example-site"],
                    "contexts": [context],
                    "actions": ["deployment.write"],
                }
            ]
        }
    )


def _terminal_agent_deployment_write_policy(*, context: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "terminal_agents": [
                {
                    "subjects": ["local-owner-agent"],
                    "token_labels": ["local-owner-read"],
                    "products": ["example-site"],
                    "contexts": [context],
                    "actions": ["deployment.write"],
                }
            ]
        }
    )


def _deployment_evidence_payload(
    *, record_id: str = "deployment-example-site-prod"
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "product": "example-site",
        "deployment": {
            "record_id": record_id,
            "artifact_identity": {"artifact_id": "artifact-example-site-prod"},
            "context": "example-site",
            "instance": "prod",
            "source_git_ref": "6b3c9d7e8f901234567890abcdef1234567890ab",
            "resolved_target": {
                "target_type": "application",
                "target_id": "target-example-site-prod",
                "target_name": "example-site-prod",
            },
            "deploy": {
                "target_name": "example-site-prod",
                "target_type": "application",
                "deploy_mode": "runtime-provider-api",
                "deployment_id": "provider-deployment-example-site-prod",
                "status": "pass",
                "started_at": "2026-04-20T15:30:00Z",
                "finished_at": "2026-04-20T15:32:00Z",
            },
            "post_deploy_update": {
                "attempted": True,
                "status": "pass",
                "detail": "Update completed.",
            },
            "destination_health": {
                "verified": True,
                "urls": ["https://example.invalid/health"],
                "timeout_seconds": 45,
                "status": "pass",
            },
        },
    }


def _deployment_read_record() -> DeploymentRecord:
    payload = _deployment_evidence_payload()["deployment"]
    return DeploymentRecord.model_validate(payload)


def _promotion_read_record() -> PromotionRecord:
    payload = _promotion_evidence_payload()["promotion"]
    return PromotionRecord.model_validate(payload)


def _environment_inventory_read_record() -> EnvironmentInventory:
    deployment = _deployment_read_record()
    return EnvironmentInventory(
        context=deployment.context,
        instance=deployment.instance,
        artifact_identity=deployment.artifact_identity,
        source_git_ref=deployment.source_git_ref,
        deploy=deployment.deploy,
        post_deploy_update=deployment.post_deploy_update,
        destination_health=deployment.destination_health,
        updated_at="2026-04-20T15:33:00Z",
        deployment_record_id=deployment.record_id,
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


def _driver_context_store(state_dir: Path) -> FilesystemRecordStore:
    store = FilesystemRecordStore(state_dir=state_dir)
    store.write_product_profile_record(
        LaunchplaneProductProfileRecord.model_validate(_generic_site_profile_payload())
    )
    store.write_deployment_record(
        DeploymentRecord(
            record_id="deployment-example-site-testing",
            artifact_identity=ArtifactIdentityReference(
                artifact_id="ghcr.io/every/example-site@sha256:abc123"
            ),
            context="example-site",
            instance="testing",
            source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
            resolved_target=ResolvedTargetEvidence(
                target_type="application",
                target_id="target-example-site-testing",
                target_name="example-site-testing",
            ),
            deploy=DeploymentEvidence(
                target_name="example-site-testing",
                target_type="application",
                deploy_mode="runtime-provider-api",
                deployment_id="provider-deployment-example-site-testing",
                status="pass",
                started_at="2026-04-20T15:30:00Z",
                finished_at="2026-04-20T15:32:00Z",
            ),
        )
    )
    return store


def _write_context_cutover_audit_records(database_url: str) -> None:
    store = PostgresRecordStore(database_url=database_url)
    store.ensure_schema()
    try:
        store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
        )
        store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="instance",
                context="sellyouroutboard-testing",
                instance="prod",
                env={"TAWK_PROPERTY_ID": "property-legacy"},
                updated_at="2026-05-01T00:03:00Z",
                source_label="legacy",
            )
        )
        store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="instance",
                context="sellyouroutboard",
                instance="prod",
                env={"TAWK_WIDGET_ID": "widget-canonical"},
                updated_at="2026-05-01T00:04:00Z",
                source_label="operator:mistake",
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
                name="smtp-password",
                plaintext_value="smtp-password-secret",
                binding_key="SMTP_PASSWORD",
                context_name="sellyouroutboard-testing",
                instance_name="prod",
                actor="test",
            )
    finally:
        store.close()


def _seed_product_environment_read_records(database_url: str) -> None:
    store = PostgresRecordStore(database_url=database_url)
    store.ensure_schema()
    try:
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
        store.write_dokploy_target_record(
            DokployTargetRecord(
                context="example-site",
                instance="prod",
                target_type="application",
                target_name="example-site-prod",
                updated_at="2026-05-02T22:33:00Z",
                source_label="test",
            )
        )
        store.write_dokploy_target_id_record(
            DokployTargetIdRecord(
                context="example-site",
                instance="prod",
                target_id="app-prod-123",
                updated_at="2026-05-02T22:33:00Z",
                source_label="test",
            )
        )
        store.write_provider_target_record(
            ProviderTargetRecord(
                context="example-site",
                instance="prod",
                provider_id="dokploy",
                target_category="application",
                target_id="app-prod-123",
                display_name="example-site-prod",
                provider_target_type="application",
                updated_at="2026-05-02T22:33:00Z",
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
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_actions": [
                    {
                        "repository": "every/verireel",
                        "workflow_refs": [
                            "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                        ],
                        "event_names": ["pull_request"],
                        "products": ["example-site"],
                        "contexts": ["launchplane"],
                        "actions": ["product_environment.read"],
                    }
                ]
            }
        )
        store.write_authz_policy_record(
            LaunchplaneAuthzPolicyRecord(
                record_id="launchplane-authz-policy-product-environment-read-test",
                source="test",
                updated_at="2026-05-02T22:35:00Z",
                policy=policy,
            )
        )
    finally:
        store.close()


def _seed_dokploy_target_inspect_records(database_url: str) -> None:
    store = PostgresRecordStore(database_url=database_url)
    store.ensure_schema()
    try:
        store.write_dokploy_target_record(
            DokployTargetRecord(
                context="cm_website",
                instance="prod",
                target_type="compose",
                target_name="cm-prod",
                project_name="odoo",
                updated_at="2026-06-14T00:00:00Z",
                source_label="test",
            )
        )
        store.write_dokploy_target_id_record(
            DokployTargetIdRecord(
                context="cm_website",
                instance="prod",
                target_id="compose-cm-prod",
                updated_at="2026-06-14T00:00:00Z",
                source_label="test",
            )
        )
        store.write_provider_target_record(
            ProviderTargetRecord(
                context="cm_website",
                instance="prod",
                provider_id="dokploy",
                target_category="compose",
                target_id="compose-cm-prod",
                display_name="cm-prod",
                provider_target_type="compose",
                provider_evidence={"project_name": "odoo"},
                updated_at="2026-06-14T00:00:00Z",
                source_label="test",
            )
        )
    finally:
        store.close()


def _seed_empty_agent_context_read_store(database_url: str) -> None:
    store = PostgresRecordStore(database_url=database_url)
    store.ensure_schema()
    store.close()


def _seed_agent_context_read_records(database_url: str) -> None:
    store = PostgresRecordStore(database_url=database_url)
    store.ensure_schema()
    try:
        store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(_generic_site_profile_payload())
        )
        request_id = "every-code-every-example-site-190-test"
        store.write_every_code_work_request_record(
            EveryCodeWorkRequestRecord(
                request_id=request_id,
                source="manual",
                state="queued",
                repository="every/example-site",
                issue_number=190,
                issue_url="https://github.com/every/example-site/issues/190",
                issue_title="Build operator chooser",
                trigger_label="every-code",
                trigger_actor="cbusillo",
                queued_at="2026-05-06T02:00:00Z",
                updated_at="2026-05-06T02:00:00Z",
            )
        )
        store.write_every_code_work_request_record(
            EveryCodeWorkRequestRecord(
                request_id="every-code-cbusillo-tooling-12-test",
                source="manual",
                state="queued",
                repository="cbusillo/tooling",
                issue_number=12,
                issue_url="https://github.com/cbusillo/tooling/issues/12",
                issue_title="Support repo follow-up",
                trigger_label="every-code",
                trigger_actor="cbusillo",
                queued_at="2026-05-08T18:00:00Z",
                updated_at="2026-05-08T18:00:00Z",
            )
        )
        store.write_every_code_preview_gate_record(
            EveryCodePreviewGateRecord(
                gate_id="every-code-preview-gate-every-example-site-190-31",
                request_id=request_id,
                repository="every/example-site",
                issue_number=190,
                issue_url="https://github.com/every/example-site/issues/190",
                pr_number=31,
                pr_url="https://github.com/every/example-site/pull/31",
                head_sha="abcdef1234567890",
                status="ready",
                created_at="2026-05-08T18:00:00Z",
                updated_at="2026-05-08T18:01:00Z",
                ready_at="2026-05-08T18:01:00Z",
                last_checked_at="2026-05-08T18:01:00Z",
            )
        )
    finally:
        store.close()


def _seed_every_code_read_records(store: Any) -> dict[str, str]:
    request_id = "every-code-cbusillo-code-123-test"
    gate_id = "every-code-preview-gate-cbusillo-code-31-test"
    feedback_id = "every-code-pr-feedback-cbusillo-code-31-review-1"
    preview_feedback_id = "preview-feedback-cbusillo-code-31-skipped"
    notification_attempt_id = "every-code-notification-cbusillo-code-123-test"
    preview_notification_attempt_id = "preview-pr-feedback-notification-cbusillo-code-31-skipped"
    store.write_every_code_work_request_record(
        EveryCodeWorkRequestRecord(
            request_id=request_id,
            source="manual",
            state="queued",
            repository="cbusillo/code",
            issue_number=123,
            issue_url="https://github.com/cbusillo/code/issues/123",
            issue_title="Finish v2 Every Code read cutover",
            trigger_label="every-code",
            trigger_actor="cbusillo",
            queued_at="2026-06-18T12:00:00Z",
            updated_at="2026-06-18T12:00:00Z",
        )
    )
    store.write_every_code_work_request_record(
        EveryCodeWorkRequestRecord(
            request_id="every-code-cbusillo-other-456-test",
            source="manual",
            state="done",
            repository="cbusillo/other",
            issue_number=456,
            issue_url="https://github.com/cbusillo/other/issues/456",
            issue_title="Unrelated completed request",
            trigger_label="every-code",
            trigger_actor="cbusillo",
            queued_at="2026-06-17T12:00:00Z",
            claimed_at="2026-06-17T12:01:00Z",
            claimed_by_host="test-host",
            started_at="2026-06-17T12:02:00Z",
            finished_at="2026-06-17T12:03:00Z",
            updated_at="2026-06-17T12:03:00Z",
            result_pr_url="https://github.com/cbusillo/other/pull/4",
        )
    )
    store.write_every_code_preview_gate_record(
        EveryCodePreviewGateRecord(
            gate_id=gate_id,
            request_id=request_id,
            repository="cbusillo/code",
            issue_number=123,
            issue_url="https://github.com/cbusillo/code/issues/123",
            pr_number=31,
            pr_url="https://github.com/cbusillo/code/pull/31",
            head_sha="abcdef1234567890",
            status="blocked",
            created_at="2026-06-18T12:05:00Z",
            updated_at="2026-06-18T12:06:00Z",
            blocked_at="2026-06-18T12:06:00Z",
            last_checked_at="2026-06-18T12:06:00Z",
            blocked_reason="Required preview checks failed.",
        )
    )
    store.write_every_code_pr_feedback_record(
        EveryCodePrFeedbackRecord(
            feedback_id=feedback_id,
            request_id=request_id,
            repository="cbusillo/code",
            pr_number=31,
            pr_url="https://github.com/cbusillo/code/pull/31",
            feedback_kind="pull_request_review_comment",
            github_delivery_id="delivery-feedback-1",
            github_node_id="PRRC_kwDOTest123",
            actor="reviewer",
            author_association="MEMBER",
            body="Please tighten the FastAPI read route coverage.",
            html_url="https://github.com/cbusillo/code/pull/31#discussion_r1",
            received_at="2026-06-18T12:07:00Z",
            status="pending",
        )
    )
    store.write_every_code_notification_attempt_record(
        EveryCodeNotificationAttemptRecord(
            attempt_id=notification_attempt_id,
            request_id=request_id,
            event="work_request_blocked",
            policy_id="every-code-notification-discord",
            destination_id="discord",
            destination_kind="discord",
            delivery_status="delivered",
            attempted_at="2026-06-18T12:08:00Z",
            action="posted_discord",
        )
    )
    store.write_preview_pr_feedback_notification_attempt_record(
        PreviewPrFeedbackNotificationAttemptRecord(
            attempt_id=preview_notification_attempt_id,
            feedback_id=preview_feedback_id,
            event="delivery_skipped",
            policy_id="preview-pr-feedback-notification-discord",
            destination_id="discord",
            destination_kind="discord",
            delivery_status="delivered",
            attempted_at="2026-06-18T12:09:00Z",
            action="posted_discord",
        )
    )
    return {
        "request_id": request_id,
        "gate_id": gate_id,
        "feedback_id": feedback_id,
        "preview_feedback_id": preview_feedback_id,
        "notification_attempt_id": notification_attempt_id,
        "preview_notification_attempt_id": preview_notification_attempt_id,
    }


def _seed_every_code_claim_request(store: Any) -> EveryCodeWorkRequestRecord:
    record = EveryCodeWorkRequestRecord(
        request_id="every-code-cbusillo-code-123-test",
        source="manual",
        state="queued",
        repository="cbusillo/code",
        issue_number=123,
        issue_url="https://github.com/cbusillo/code/issues/123",
        issue_title="Wire local automation",
        trigger_label="every-code",
        trigger_actor="cbusillo",
        queued_at="2026-05-05T22:00:00Z",
        updated_at="2026-05-05T22:00:00Z",
    )
    store.write_every_code_work_request_record(record)
    return record


def _every_code_read_policy() -> LaunchplaneAuthzPolicy:
    return _record_read_policy(
        action="every_code_work_request.read",
        context="launchplane",
        extra_actions=(
            "every_code_preview_gate.read",
            "every_code_pr_feedback.read",
            "every_code_notification_attempt.read",
            "preview_pr_feedback_notification_attempt.read",
        ),
    )


def _every_code_work_request_write_policy() -> LaunchplaneAuthzPolicy:
    return _record_read_policy(
        action="every_code_work_request.write",
        context="launchplane",
    )


def _every_code_work_request_claim_policy() -> LaunchplaneAuthzPolicy:
    return _record_read_policy(
        action="every_code_work_request.claim",
        context="launchplane",
    )


def _every_code_work_request_status_policy() -> LaunchplaneAuthzPolicy:
    return _record_read_policy(
        action="every_code_work_request.update",
        context="launchplane",
    )


def _every_code_work_request_rerun_policy() -> LaunchplaneAuthzPolicy:
    return _record_read_policy(
        action="every_code_work_request.rerun",
        context="launchplane",
    )


_AGENT_WRITE_INTENT_SOURCE_URL = "https://github.com/cbusillo/launchplane/issues/386"


def _agent_write_intent_policy(
    *, actions: tuple[str, ...], product: str, context: str
) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "every/verireel",
                    "workflow_refs": [
                        "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                    ],
                    "event_names": ["pull_request"],
                    "products": [product],
                    "contexts": [context],
                    "actions": list(actions),
                }
            ]
        }
    )


def _terminal_agent_write_intent_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "terminal_agents": [
                {
                    "subjects": ["local-owner-agent"],
                    "token_labels": ["local-owner-read"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["every_code_work_request.rerun"],
                }
            ]
        }
    )


def _agent_write_intent_payload(
    *,
    intent: str,
    mode: str,
    product: str,
    context: str,
    source_url: str = _AGENT_WRITE_INTENT_SOURCE_URL,
    reason: str = "Evaluate agent write intent.",
    secret_bindings: list[str] | None = None,
    destination: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "intent": intent,
        "mode": mode,
        "product": product,
        "context": context,
        "source_url": source_url,
        "reason": reason,
    }
    if secret_bindings is not None:
        payload["secret_bindings"] = secret_bindings
    if destination is not None:
        payload["destination"] = destination
    return payload


def _seed_agent_write_intent_secret_binding(store: Any, *, binding_instance: str) -> None:
    store.write_runtime_key_safety_policy_record(
        RuntimeKeySafetyPolicyRecord(
            record_id="runtime-key-safety-policy-write-intent-test",
            status="active",
            source="test",
            updated_at="2026-05-05T20:00:00Z",
            rules=(
                RuntimeSecretSafetyRule(
                    binding_key="SMTP_PASSWORD",
                    secret_class="prod_only",
                    allowed_contexts=("sellyouroutboard",),
                    allowed_instances=("prod",),
                ),
            ),
        )
    )
    store.write_secret_binding(
        SecretBinding(
            binding_id="secret-smtp-password-binding-smtp-password",
            secret_id="secret-smtp-password",
            integration=control_plane_secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
            binding_key="SMTP_PASSWORD",
            context="sellyouroutboard",
            instance=binding_instance,
            created_at="2026-05-05T20:00:00Z",
            updated_at="2026-05-05T20:00:00Z",
        )
    )


def _seed_every_code_rerun_intent(
    store: Any,
    *,
    source_url: str = "https://github.com/cbusillo/code/issues/123",
    context: str = "launchplane",
    idempotency_key: str = "",
    recorded_at: str = "",
    authorized: bool = True,
) -> AgentWriteIntentRecord:
    resolved_recorded_at = recorded_at or datetime.now(timezone.utc).replace(
        microsecond=0
    ).isoformat().replace("+00:00", "Z")
    request = AgentWriteIntentRequest(
        intent="every_code_rerun",
        mode="apply",
        product="launchplane",
        context=context,
        source_url=source_url,
        idempotency_key=idempotency_key,
        reason="Approved rerun for blocked Every Code request.",
    )
    audit = agent_authz_audit(
        identity=_identity(),
        action="every_code_work_request.rerun",
        product="launchplane",
        context=context,
        decision="allowed" if authorized else "denied",
        reason_code="authorized" if authorized else "authorization_denied",
        policy_source="test",
        policy_sha256="test-policy-sha256",
    )
    evaluation = evaluate_agent_write_intent(
        request=request,
        authorized=authorized,
        audit=audit,
    )
    record = AgentWriteIntentRecord(
        record_id=build_agent_write_intent_record_id(
            recorded_at=resolved_recorded_at,
            trace_id="launchplane_req_every_code_rerun_test",
            request=request,
            evaluation=evaluation,
        ),
        recorded_at=resolved_recorded_at,
        trace_id="launchplane_req_every_code_rerun_test",
        idempotency_key=idempotency_key,
        request=request,
        evaluation=evaluation,
    )
    store.write_agent_write_intent_record(record)
    return record


def _every_code_work_request_create_payload(*, issue_number: int = 123) -> dict[str, object]:
    return {
        "repository": "cbusillo/code",
        "issue_number": issue_number,
        "issue_url": f"https://github.com/cbusillo/code/issues/{issue_number}",
        "issue_title": "Wire local automation",
        "trigger_label": "every-code",
        "trigger_actor": "cbusillo",
        "source": "manual",
        "queued_at": "2026-05-05T22:00:00Z",
    }


def _every_code_pr_feedback_payload() -> dict[str, object]:
    return {
        "feedback_id": "every-code-pr-feedback-cbusillo-code-31-review-1",
        "request_id": "every-code-cbusillo-code-123-test",
        "repository": "cbusillo/code",
        "pr_number": 31,
        "pr_url": "https://github.com/cbusillo/code/pull/31",
        "feedback_kind": "pull_request_review_comment",
        "github_delivery_id": "delivery-feedback-1",
        "github_node_id": "PRRC_kwDOTest123",
        "actor": "reviewer",
        "author_association": "MEMBER",
        "body": "Please tighten the FastAPI write route coverage.",
        "html_url": "https://github.com/cbusillo/code/pull/31#discussion_r1",
        "received_at": "2026-06-18T12:07:00Z",
        "status": "pending",
    }


def _every_code_preview_gate_payload() -> dict[str, object]:
    return {
        "gate_id": "every-code-preview-gate-cbusillo-code-31-test",
        "request_id": "every-code-cbusillo-code-123-test",
        "repository": "cbusillo/code",
        "issue_number": 123,
        "issue_url": "https://github.com/cbusillo/code/issues/123",
        "pr_number": 31,
        "pr_url": "https://github.com/cbusillo/code/pull/31",
        "head_sha": "abcdef1234567890",
        "status": "ready",
        "created_at": "2026-06-18T12:05:00Z",
        "updated_at": "2026-06-18T12:06:00Z",
        "ready_at": "2026-06-18T12:06:00Z",
        "last_checked_at": "2026-06-18T12:06:00Z",
    }


def _notification_policy_apply_policy(
    *, action: str, product: str, context: str
) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "every/verireel",
                    "workflow_refs": [
                        "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                    ],
                    "event_names": ["pull_request"],
                    "products": [product],
                    "contexts": [context],
                    "actions": [action],
                }
            ]
        }
    )


def _generic_web_preview_desired_state_policy(*, context: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "cbusillo/sellyouroutboard",
                    "workflow_refs": [
                        "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/heads/main"
                    ],
                    "event_names": ["pull_request"],
                    "products": ["sellyouroutboard"],
                    "contexts": [context],
                    "actions": ["preview_desired_state.discover"],
                }
            ]
        }
    )


def _generic_web_preview_desired_state_identity() -> GitHubActionsIdentity:
    return _identity(
        repository="cbusillo/sellyouroutboard",
        workflow_ref=(
            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/heads/main"
        ),
    )


def _runtime_key_safety_policy_apply_policy(*, action: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "every/verireel",
                    "workflow_refs": [
                        "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                    ],
                    "event_names": ["pull_request"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": [action],
                }
            ]
        }
    )


def _github_human_runtime_key_safety_policy_apply_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_humans": [
                {
                    "logins": ["example-operator"],
                    "roles": ["admin"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["runtime_key_safety.write"],
                }
            ]
        }
    )


def _runtime_key_safety_policy_apply_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "product": "launchplane",
        "source_label": "test:runtime-key-safety-policy",
        "rules": [
            {
                "binding_key": "SMTP_PASSWORD",
                "secret_class": "prod_only",
                "allowed_contexts": ["sellyouroutboard"],
                "allowed_instances": ["prod"],
            },
            {
                "binding_key": "RESEND_API_KEY",
                "secret_class": "prod_only",
                "allowed_contexts": ["sellyouroutboard"],
                "allowed_instances": ["prod"],
            },
        ],
    }


def _github_human_ingress_route_policy(
    *, action: str, product: str, context: str
) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_humans": [
                {
                    "logins": ["example-operator"],
                    "roles": ["admin"],
                    "products": [product],
                    "contexts": [context],
                    "actions": [action],
                }
            ]
        }
    )


def _public_ingress_notification_policy_record(
    *, policy_id: str = "public-ingress-notification-launchplane"
) -> PublicIngressNotificationPolicyRecord:
    return PublicIngressNotificationPolicyRecord(
        policy_id=policy_id,
        product="launchplane",
        context="launchplane",
        status="enabled",
        created_at="2026-05-29T12:00:00Z",
        updated_at="2026-05-29T12:00:00Z",
        source="test",
        destinations=(
            PublicIngressNotificationDestination(
                destination_id="discord",
                kind="discord",
                discord_webhook_secret="secret-discord-webhook",
            ),
        ),
    )


def _every_code_notification_policy_record(
    *, policy_id: str = "every-code-notification-launchplane"
) -> EveryCodeNotificationPolicyRecord:
    return EveryCodeNotificationPolicyRecord(
        policy_id=policy_id,
        repository="cbusillo/code",
        status="enabled",
        created_at="2026-06-14T18:00:00Z",
        updated_at="2026-06-14T18:00:00Z",
        source="test",
        destinations=(
            EveryCodeNotificationDestination(
                destination_id="discord",
                kind="discord",
                discord_webhook_secret="secret-discord-webhook",
            ),
        ),
    )


def _preview_pr_feedback_notification_policy_record(
    *,
    policy_id: str = "preview-pr-feedback-notification-syo",
    product: str = "sellyouroutboard",
    context: str = "sellyouroutboard",
    repository: str = "cbusillo/sellyouroutboard",
) -> PreviewPrFeedbackNotificationPolicyRecord:
    return PreviewPrFeedbackNotificationPolicyRecord(
        policy_id=policy_id,
        product=product,
        context=context,
        repository=repository,
        status="enabled",
        created_at="2026-06-15T17:10:00Z",
        updated_at="2026-06-15T17:10:00Z",
        source="test",
        destinations=(
            PreviewPrFeedbackNotificationDestination(
                destination_id="discord",
                kind="discord",
                discord_webhook_secret="secret-discord-webhook",
            ),
        ),
    )


def _preview_pr_feedback_identity() -> GitHubActionsIdentity:
    return _identity(
        repository="every/verireel",
        workflow_ref=(
            "every/verireel/.github/workflows/preview-control-plane.yml@refs/pull/42/merge"
        ),
        event_name="pull_request",
    )


def _preview_pr_feedback_policy(*, action: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "every/verireel",
                    "workflow_refs": [
                        "every/verireel/.github/workflows/preview-control-plane.yml@refs/pull/42/merge"
                    ],
                    "event_names": ["pull_request"],
                    "products": ["verireel"],
                    "contexts": ["verireel-testing"],
                    "actions": [action],
                }
            ]
        }
    )


def _preview_pr_feedback_payload(
    *,
    status: str = "ready",
    dry_run: bool = False,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "product": "verireel",
        "context": "verireel-testing",
        "source": "preview-control-plane",
        "repository": "every/verireel",
        "anchor_repo": "verireel",
        "anchor_pr_number": 42,
        "anchor_pr_url": "https://github.com/every/verireel/pull/42",
        "status": status,
        "preview_url": "https://pr-42.preview.example",
        "immutable_image_reference": "ghcr.io/every/verireel:pr-42-a1b2c3d4",
        "refresh_image_reference": "ghcr.io/every/verireel:preview-pr-42",
        "revision": "a1b2c3d4",
        "run_url": "https://github.com/every/verireel/actions/runs/123",
    }
    if dry_run:
        payload["dry_run"] = True
    return payload


def _ingress_route_audit_record(
    *,
    record_id: str = "ingress-route-audit-test",
    product: str = "launchplane",
    context: str = "reon-prod",
    mode: Literal["dry-run", "apply"] = "dry-run",
    status: Literal["pending", "planned", "applied", "unchanged"] = "planned",
    dry_run: bool = True,
    provider_host_id: int | None = 78,
    trace_id: str = "trace-audit-1",
    idempotency_key: str = "audit-key-1",
    recorded_at: str = "2026-06-01T00:00:00Z",
) -> IngressRouteAuditRecord:
    return IngressRouteAuditRecord(
        record_id=record_id,
        product=product,
        context=context,
        mode=mode,
        status=status,
        dry_run=dry_run,
        requested_domains=("app.example.com",),
        edge_endpoint_key="edge-app",
        expected_host_id=None,
        provider_host_id=provider_host_id,
        operations=(
            IngressRouteAuditOperation(
                action="create",
                host_id=provider_host_id,
                domain_names=("app.example.com",),
                requires_apply=mode == "dry-run",
                change_categories=("create",),
            ),
        ),
        trace_id=trace_id,
        idempotency_key=idempotency_key,
        reason="test",
        recorded_at=recorded_at,
    )


def _ingress_route_audit_read_policy(*, contexts: tuple[str, ...]) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "every/verireel",
                    "workflow_refs": [
                        "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                    ],
                    "event_names": ["pull_request"],
                    "products": ["launchplane"],
                    "contexts": list(contexts),
                    "actions": ["ingress_route.plan"],
                }
            ]
        }
    )


def _product_profile_read_policy(*, product: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "every/verireel",
                    "workflow_refs": [
                        "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                    ],
                    "event_names": ["pull_request"],
                    "products": [product],
                    "contexts": ["launchplane"],
                    "actions": ["product_profile.read"],
                }
            ]
        }
    )


def _product_profile_write_policy(*, product: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "every/verireel",
                    "workflow_refs": [
                        "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                    ],
                    "event_names": ["pull_request"],
                    "products": [product],
                    "contexts": ["launchplane"],
                    "actions": ["product_profile.write"],
                }
            ]
        }
    )


def _product_config_policy(
    *,
    action: str,
    product: str = "sellyouroutboard",
    context: str = "sellyouroutboard-prod",
) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "every/verireel",
                    "workflow_refs": [
                        "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                    ],
                    "event_names": ["pull_request"],
                    "products": [product],
                    "contexts": [context],
                    "actions": [action],
                }
            ]
        }
    )


def _github_human_product_config_policy(
    *,
    action: str,
    product: str = "sellyouroutboard",
    context: str = "sellyouroutboard",
    role: str = "admin",
) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_humans": [
                {
                    "logins": ["example-operator"],
                    "roles": [role],
                    "products": [product],
                    "contexts": [context],
                    "actions": [action],
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


def _local_operator_launchplane_service_read_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "local_operators": [
                {
                    "subjects": ["local-owner-agent"],
                    "token_labels": ["local-owner-read"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["launchplane_service.read"],
                }
            ]
        }
    )


def _local_operator_launchplane_service_reconcile_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "local_operators": [
                {
                    "subjects": ["local-owner-agent"],
                    "token_labels": ["local-owner-write"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["launchplane_service.reconcile_odoo_workers"],
                }
            ]
        }
    )


def _github_actions_launchplane_service_reconcile_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "every/verireel",
                    "workflow_refs": [
                        "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                    ],
                    "event_names": ["pull_request"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["launchplane_service.reconcile_odoo_workers"],
                }
            ]
        }
    )


def _pending_odoo_stable_bootstrap_record() -> OdooStableBootstrapOperationRecord:
    return OdooStableBootstrapOperationRecord.model_validate(
        {
            "operation_id": "bootstrap-cm-testing",
            "product": "odoo-tenant-cm",
            "context": "cm",
            "instance": "testing",
            "idempotency_key": "bootstrap-cm-testing",
            "request_fingerprint": "fingerprint-123",
            "request": {
                "schema_version": 1,
                "product": "odoo-tenant-cm",
                "context": "cm",
                "instance": "testing",
                "confirmation": "bootstrap cm testing",
            },
            "status": "pending",
            "phase": "created",
            "created_at": "2026-05-17T00:00:00Z",
            "updated_at": "2026-05-17T00:00:00Z",
        }
    )


def _stale_odoo_stable_bootstrap_record() -> OdooStableBootstrapOperationRecord:
    return OdooStableBootstrapOperationRecord.model_validate(
        {
            "operation_id": "bootstrap-cm-testing",
            "product": "odoo-tenant-cm",
            "context": "cm",
            "instance": "testing",
            "idempotency_key": "bootstrap-cm-testing",
            "request_fingerprint": "fingerprint-123",
            "request": {
                "schema_version": 1,
                "product": "odoo-tenant-cm",
                "context": "cm",
                "instance": "testing",
                "confirmation": "bootstrap cm testing",
            },
            "status": "running",
            "phase": "created",
            "created_at": "2026-05-17T00:00:00Z",
            "updated_at": "2026-05-17T00:01:00Z",
            "started_at": "2026-05-17T00:01:00Z",
            "lease_owner": "old-worker",
            "lease_expires_at": "2000-01-01T00:00:00Z",
            "heartbeat_at": "2000-01-01T00:00:00Z",
            "attempt": 1,
        }
    )


def _running_odoo_stable_bootstrap_record() -> OdooStableBootstrapOperationRecord:
    return OdooStableBootstrapOperationRecord.model_validate(
        {
            "operation_id": "operation-cm-testing",
            "product": "odoo-tenant-cm",
            "context": "cm",
            "instance": "testing",
            "idempotency_key": "bootstrap-cm-testing",
            "request_fingerprint": "fingerprint-123",
            "request": {
                "schema_version": 1,
                "product": "odoo-tenant-cm",
                "context": "cm",
                "instance": "testing",
                "confirmation": "bootstrap cm testing",
            },
            "status": "running",
            "phase": "running",
            "created_at": "2026-05-17T00:00:00Z",
            "updated_at": "2026-05-17T00:01:00Z",
            "started_at": "2026-05-17T00:01:00Z",
        }
    )


def _running_odoo_target_replacement_record() -> OdooStableTargetReplacementOperationRecord:
    return OdooStableTargetReplacementOperationRecord.model_validate(
        {
            "operation_id": "operation-cm-testing",
            "product": "odoo-tenant-cm",
            "context": "cm",
            "instance": "testing",
            "idempotency_key": "apply-cm-testing",
            "request_fingerprint": "fingerprint-123",
            "request": {
                "schema_version": 1,
                "product": "odoo-tenant-cm",
                "instance": "testing",
                "strategy": "recreate-in-place",
                "allow_empty_data": False,
            },
            "status": "running",
            "phase": "running",
            "created_at": "2026-05-17T00:00:00Z",
            "updated_at": "2026-05-17T00:01:00Z",
            "started_at": "2026-05-17T00:01:00Z",
        }
    )


def _odoo_operation_status_identity() -> GitHubActionsIdentity:
    return _identity(
        repository="cbusillo/launchplane",
        workflow_ref=(
            "cbusillo/launchplane/.github/workflows/odoo-operation-status.yml@refs/heads/main"
        ),
        event_name="workflow_dispatch",
    )


def _odoo_operation_status_policy(
    *,
    action: str = "",
    actions: tuple[str, ...] = (),
    products: tuple[str, ...] = ("odoo-tenant-cm",),
    contexts: tuple[str, ...] = ("cm",),
) -> LaunchplaneAuthzPolicy:
    resolved_actions = actions or (action,)
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "cbusillo/launchplane",
                    "workflow_refs": [
                        "cbusillo/launchplane/.github/workflows/odoo-operation-status.yml@refs/heads/main"
                    ],
                    "event_names": ["workflow_dispatch"],
                    "products": list(products),
                    "contexts": list(contexts),
                    "actions": list(resolved_actions),
                }
            ]
        }
    )


def _terminal_agent_product_environment_read_policy(*, context: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "terminal_agents": [
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


def _terminal_agent_launchplane_read_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "terminal_agents": [
                {
                    "subjects": ["local-owner-agent"],
                    "token_labels": ["local-owner-read"],
                    "products": ["launchplane", "example-site"],
                    "contexts": ["launchplane", "example-site"],
                    "actions": ["product_environment.read"],
                }
            ]
        }
    )


def _terminal_agent_launchplane_service_reconcile_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "terminal_agents": [
                {
                    "subjects": ["local-owner-agent"],
                    "token_labels": ["local-owner-read"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["launchplane_service.reconcile_odoo_workers"],
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


def _github_human_identity(*, role: Literal["read_only", "admin"] = "admin") -> GitHubHumanIdentity:
    return GitHubHumanIdentity(
        login="example-operator",
        github_id=123,
        name="Example Operator",
        email="operator@example.com",
        organizations=frozenset({"example-org"}),
        teams=frozenset({"example-org/launchplane-operators"}),
        role=role,
    )


def _local_operator_bearer_config(*, token_label: str = "local-owner-read") -> BearerIdentityConfig:
    return BearerIdentityConfig(
        local_operator_token="local-operator-token",
        local_operator_subject="local-owner-agent",
        local_operator_token_label=token_label,
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


async def _get_repo_product_mapping(
    app: FastAPI,
    *,
    authorization: str = "Bearer valid-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    return await _asgi_get(app, "/v1/repo-product-mapping", headers=headers)


async def _get_agent_context(
    app: FastAPI,
    *,
    repository: str = "",
    authorization: str = "Bearer valid-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    suffix = f"?{urlencode({'repository': repository})}" if repository else ""
    return await _asgi_get(app, f"/v1/agent/context{suffix}", headers=headers)


async def _get_work_graph_snapshot(
    app: FastAPI,
    *,
    authorization: str = "Bearer valid-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    return await _asgi_get(app, "/v1/work-graph/snapshot", headers=headers)


async def _post_work_graph_rank(
    app: FastAPI,
    *,
    payload: dict[str, object],
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    return await _asgi_request(
        app,
        "POST",
        "/v1/work-graph/rank",
        headers=request_headers,
        payload=payload,
    )


async def _get_work_graph_issue_inbox(
    app: FastAPI,
    *,
    authorization: str = "Bearer valid-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    return await _asgi_get(app, "/v1/work-graph/github/issues", headers=headers)


async def _post_work_graph_issue_inbox_reconcile(
    app: FastAPI,
    *,
    payload: dict[str, object],
    authorization: str = "Bearer valid-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    return await _asgi_request(
        app,
        "POST",
        "/v1/work-graph/github/issues/reconcile",
        headers=headers,
        payload=payload,
    )


async def _get_every_code_summary(
    app: FastAPI,
    *,
    repository: str = "",
    issue_number: str = "",
    state: str = "",
    limit: str = "",
    offset: str = "",
    authorization: str = "Bearer valid-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    params = _query_params(
        repository=repository,
        issue_number=issue_number,
        state=state,
        limit=limit,
        offset=offset,
    )
    suffix = f"?{urlencode(params)}" if params else ""
    return await _asgi_get(app, f"/v1/every-code/summary{suffix}", headers=headers)


async def _get_preview_readiness(
    app: FastAPI,
    *,
    repository: str = "",
    pr_number: str = "",
    status: str = "",
    limit: str = "",
    offset: str = "",
    authorization: str = "Bearer valid-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    params = _query_params(
        repository=repository,
        pr_number=pr_number,
        status=status,
        limit=limit,
        offset=offset,
    )
    suffix = f"?{urlencode(params)}" if params else ""
    return await _asgi_get(app, f"/v1/previews/readiness{suffix}", headers=headers)


async def _get_every_code_work_requests(
    app: FastAPI,
    *,
    state: str = "",
    repository: str = "",
    limit: str = "",
    offset: str = "",
    authorization: str = "Bearer valid-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    params = _query_params(
        state=state,
        repository=repository,
        limit=limit,
        offset=offset,
    )
    suffix = f"?{urlencode(params)}" if params else ""
    return await _asgi_get(app, f"/v1/every-code/work-requests{suffix}", headers=headers)


async def _get_every_code_work_request(
    app: FastAPI,
    request_id: str,
    *,
    authorization: str = "Bearer valid-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    return await _asgi_get(
        app,
        f"/v1/every-code/work-requests/{request_id}",
        headers=headers,
    )


async def _post_agent_write_intent_evaluate(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/agent/write-intents/evaluate",
        headers=request_headers,
        payload=payload,
    )


async def _post_every_code_work_request_create(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/every-code/work-requests/create",
        headers=request_headers,
        payload=payload,
    )


async def _post_every_code_work_request_claim(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/every-code/work-requests/claim",
        headers=request_headers,
        payload=payload,
    )


async def _post_every_code_work_request_status(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/every-code/work-requests/status",
        headers=request_headers,
        payload=payload,
    )


async def _post_every_code_work_request_rerun(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/every-code/work-requests/rerun",
        headers=request_headers,
        payload=payload,
    )


async def _get_every_code_pr_feedback(
    app: FastAPI,
    *,
    request_id: str = "",
    repository: str = "",
    pr_number: str = "",
    status: str = "",
    limit: str = "",
    offset: str = "",
    authorization: str = "Bearer valid-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    params = _query_params(
        request_id=request_id,
        repository=repository,
        pr_number=pr_number,
        status=status,
        limit=limit,
        offset=offset,
    )
    suffix = f"?{urlencode(params)}" if params else ""
    return await _asgi_get(app, f"/v1/every-code/pr-feedback{suffix}", headers=headers)


async def _post_every_code_pr_feedback(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    return await _asgi_request(
        app,
        "POST",
        "/v1/every-code/pr-feedback",
        headers=headers,
        payload=payload,
    )


async def _post_every_code_pr_feedback_status(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    return await _asgi_request(
        app,
        "POST",
        "/v1/every-code/pr-feedback/status",
        headers=headers,
        payload=payload,
    )


async def _get_every_code_preview_gates(
    app: FastAPI,
    *,
    request_id: str = "",
    repository: str = "",
    pr_number: str = "",
    status: str = "",
    limit: str = "",
    offset: str = "",
    authorization: str = "Bearer valid-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    params = _query_params(
        request_id=request_id,
        repository=repository,
        pr_number=pr_number,
        status=status,
        limit=limit,
        offset=offset,
    )
    suffix = f"?{urlencode(params)}" if params else ""
    return await _asgi_get(app, f"/v1/every-code/preview-gates{suffix}", headers=headers)


async def _post_every_code_preview_gate(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    return await _asgi_request(
        app,
        "POST",
        "/v1/every-code/preview-gates",
        headers=headers,
        payload=payload,
    )


async def _get_every_code_notification_attempts(
    app: FastAPI,
    *,
    request_id: str = "",
    event: str = "",
    destination_kind: str = "",
    limit: str = "",
    authorization: str = "Bearer valid-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    params = _query_params(
        request_id=request_id,
        event=event,
        destination_kind=destination_kind,
        limit=limit,
    )
    suffix = f"?{urlencode(params)}" if params else ""
    return await _asgi_get(
        app,
        f"/v1/every-code/notification-attempts{suffix}",
        headers=headers,
    )


async def _get_preview_pr_feedback_notification_attempts(
    app: FastAPI,
    *,
    feedback_id: str = "",
    event: str = "",
    destination_kind: str = "",
    limit: str = "",
    authorization: str = "Bearer valid-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    params = _query_params(
        feedback_id=feedback_id,
        event=event,
        destination_kind=destination_kind,
        limit=limit,
    )
    suffix = f"?{urlencode(params)}" if params else ""
    return await _asgi_get(
        app,
        f"/v1/previews/pr-feedback/notification-attempts{suffix}",
        headers=headers,
    )


async def _post_preview_pr_feedback(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/previews/pr-feedback",
        headers=headers,
        payload=payload,
    )


def _query_params(**values: str) -> dict[str, str]:
    return {key: value for key, value in values.items() if value != ""}


async def _get_dokploy_target_inspect(
    app: FastAPI,
    *,
    context: str = "",
    instance: str = "",
    target_type: str = "",
    target_id: str = "",
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers.setdefault("Authorization", authorization)
    params = _query_params(
        context=context,
        instance=instance,
        target_type=target_type,
        target_id=target_id,
    )
    suffix = f"?{urlencode(params)}" if params else ""
    return await _asgi_get(app, f"/v1/dokploy-targets/inspect{suffix}", headers=request_headers)


async def _get_products(
    app: FastAPI,
    *,
    authorization: str = "Bearer valid-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    return await _asgi_get(app, "/v1/products", headers=headers)


async def _get_product(
    app: FastAPI,
    product: str = "example-site",
    *,
    authorization: str = "Bearer valid-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    return await _asgi_get(app, f"/v1/products/{product}", headers=headers)


async def _get_product_activity(
    app: FastAPI,
    product: str = "example-site",
    *,
    authorization: str = "Bearer valid-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    return await _asgi_get(app, f"/v1/products/{product}/activity", headers=headers)


async def _get_product_environments(
    app: FastAPI,
    product: str = "example-site",
    *,
    authorization: str = "Bearer valid-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    return await _asgi_get(app, f"/v1/products/{product}/environments", headers=headers)


async def _get_product_environment(
    app: FastAPI,
    product: str = "example-site",
    environment: str = "prod",
    *,
    authorization: str = "Bearer valid-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    return await _asgi_get(
        app,
        f"/v1/products/{product}/environments/{environment}",
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
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    return await _asgi_get(app, f"/v1/drivers/{driver_id}", headers=request_headers)


async def _get_driver_context_view(
    app: FastAPI,
    context: str,
    *,
    authorization: str = "Bearer local-operator-token",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    return await _asgi_get(
        app,
        f"/v1/contexts/{context}/driver-view",
        headers=request_headers,
    )


async def _get_driver_instance_view(
    app: FastAPI,
    context: str,
    instance: str,
    *,
    authorization: str = "Bearer local-operator-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    return await _asgi_get(
        app,
        f"/v1/contexts/{context}/instances/{instance}/driver-view",
        headers=headers,
    )


async def _get_tracked_target_logs(
    app: FastAPI,
    context: str,
    instance: str,
    *,
    lines: str = "",
    since: str = "",
    search: str = "",
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    params = {}
    if lines:
        params["lines"] = lines
    if since:
        params["since"] = since
    if search:
        params["search"] = search
    suffix = f"?{urlencode(params)}" if params else ""
    return await _asgi_get(
        app,
        f"/v1/contexts/{context}/instances/{instance}/logs{suffix}",
        headers=request_headers,
    )


async def _get_edge_endpoint_records(
    app: FastAPI,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
    limit: str = "",
    provider: str = "",
    status: str = "",
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    params = {}
    if limit:
        params["limit"] = limit
    if provider:
        params["provider"] = provider
    if status:
        params["status"] = status
    suffix = f"?{urlencode(params)}" if params else ""
    return await _asgi_get(
        app,
        f"/v1/edge-endpoints/records{suffix}",
        headers=request_headers,
    )


async def _get_edge_endpoint_record(
    app: FastAPI,
    endpoint_key: str,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    return await _asgi_get(
        app,
        f"/v1/edge-endpoints/records/{endpoint_key}",
        headers=request_headers,
    )


async def _get_private_health_endpoint_records(
    app: FastAPI,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
    product: str = "repairshopr-sync",
    context: str = "repairshopr-sync",
    instance: str = "",
    status: str = "",
    limit: str = "",
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    params = {}
    if product:
        params["product"] = product
    if context:
        params["context"] = context
    if instance:
        params["instance"] = instance
    if status:
        params["status"] = status
    if limit:
        params["limit"] = limit
    suffix = f"?{urlencode(params)}" if params else ""
    return await _asgi_get(
        app,
        f"/v1/private-health-endpoints/records{suffix}",
        headers=request_headers,
    )


async def _get_private_health_endpoint_record(
    app: FastAPI,
    endpoint_key: str,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
    product: str = "repairshopr-sync",
    context: str = "repairshopr-sync",
    instance: str = "prod",
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    params = {}
    if product:
        params["product"] = product
    if context:
        params["context"] = context
    if instance:
        params["instance"] = instance
    suffix = f"?{urlencode(params)}" if params else ""
    return await _asgi_get(
        app,
        f"/v1/private-health-endpoints/records/{endpoint_key}{suffix}",
        headers=request_headers,
    )


async def _get_ingress_canary_route_records(
    app: FastAPI,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
    product: str = "",
    context: str = "",
    status: str = "",
    limit: str = "",
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    params = {}
    if product:
        params["product"] = product
    if context:
        params["context"] = context
    if status:
        params["status"] = status
    if limit:
        params["limit"] = limit
    suffix = f"?{urlencode(params)}" if params else ""
    return await _asgi_get(
        app,
        f"/v1/ingress/canary-routes/records{suffix}",
        headers=request_headers,
    )


async def _get_ingress_canary_route_record(
    app: FastAPI,
    canary_key: str,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    return await _asgi_get(
        app,
        f"/v1/ingress/canary-routes/records/{canary_key}",
        headers=request_headers,
    )


def _ingress_canary_route_apply_payload(
    *,
    product: str = "launchplane",
    context: str = "reon-prod",
    canary_key: str = "ingress-canary",
    reason: str = "test canary apply",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "product": product,
        "context": context,
        "canary_key": canary_key,
        "reason": reason,
    }


async def _get_ingress_route_audit_records(
    app: FastAPI,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
    product: str = "",
    context: str = "",
    status: str = "",
    mode: str = "",
    provider_host_id: str = "",
    trace_id: str = "",
    idempotency_key: str = "",
    limit: str = "",
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    params = _query_params(
        product=product,
        context=context,
        status=status,
        mode=mode,
        provider_host_id=provider_host_id,
        trace_id=trace_id,
        idempotency_key=idempotency_key,
        limit=limit,
    )
    suffix = f"?{urlencode(params)}" if params else ""
    return await _asgi_get(
        app,
        f"/v1/ingress/route-audits/records{suffix}",
        headers=request_headers,
    )


async def _get_ingress_route_audit_record(
    app: FastAPI,
    record_id: str,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
    product: str = "",
    context: str = "",
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    params = _query_params(product=product, context=context)
    suffix = f"?{urlencode(params)}" if params else ""
    return await _asgi_get(
        app,
        f"/v1/ingress/route-audits/records/{record_id}{suffix}",
        headers=request_headers,
    )


async def _get_deployment_record(
    app: FastAPI,
    record_id: str,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    return await _asgi_get(app, f"/v1/deployments/{record_id}", headers=request_headers)


async def _get_promotion_record(
    app: FastAPI,
    record_id: str,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    return await _asgi_get(app, f"/v1/promotions/{record_id}", headers=request_headers)


async def _get_environment_inventory(
    app: FastAPI,
    context: str,
    instance: str,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    return await _asgi_get(
        app,
        f"/v1/inventory/{context}/{instance}",
        headers=request_headers,
    )


async def _get_recent_operations(
    app: FastAPI,
    context: str,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    return await _asgi_get(
        app,
        f"/v1/contexts/{context}/operations/recent",
        headers=request_headers,
    )


async def _get_product_profiles(
    app: FastAPI,
    *,
    driver_id: str = "",
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    suffix = f"?{urlencode({'driver_id': driver_id})}" if driver_id else ""
    return await _asgi_get(app, f"/v1/product-profiles{suffix}", headers=request_headers)


async def _get_product_profile(
    app: FastAPI,
    product: str = "sellyouroutboard",
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    return await _asgi_get(
        app,
        f"/v1/product-profiles/{product}",
        headers=request_headers,
    )


async def _post_product_profile(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/product-profiles",
        headers=request_headers,
        payload=payload,
    )


def _preview_desired_state_payload(*, label: str = "preview") -> dict[str, object]:
    return {
        "product": "verireel",
        "context": "verireel-testing",
        "source": "launchplane-preview-lifecycle",
        "repository": "every/verireel",
        "label": label,
        "anchor_repo": "verireel",
    }


async def _post_preview_desired_state(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/previews/desired-state",
        headers=request_headers,
        payload=payload,
    )


def _generic_web_preview_desired_state_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "product": "sellyouroutboard",
        "desired_state": {
            "schema_version": 1,
            "product": "sellyouroutboard",
        },
    }


async def _post_generic_web_preview_desired_state(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/drivers/generic-web/preview-desired-state",
        headers=request_headers,
        payload=payload,
    )


async def _post_product_config_apply(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
    raw_body: bytes | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/product-config/apply",
        headers=request_headers,
        payload=payload,
        raw_body=raw_body,
    )


async def _post_context_cutover_apply(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
    raw_body: bytes | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/product-profiles/context-cutover/apply",
        headers=request_headers,
        payload=payload,
        raw_body=raw_body,
    )


async def _post_legacy_context_cleanup_apply(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
    raw_body: bytes | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/product-profiles/legacy-context-cleanup/apply",
        headers=request_headers,
        payload=payload,
        raw_body=raw_body,
    )


async def _get_context_cutover_audit(
    app: FastAPI,
    *,
    product: str = "sellyouroutboard",
    source_context: str = "sellyouroutboard-testing",
    target_context: str = "sellyouroutboard",
    preview_context: str = "",
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    params = {
        "source_context": source_context,
        "target_context": target_context,
    }
    if preview_context:
        params["preview_context"] = preview_context
    return await _asgi_get(
        app,
        f"/v1/product-profiles/{product}/context-cutover-audit?{urlencode(params)}",
        headers=request_headers,
    )


async def _get_context_secret_statuses(
    app: FastAPI,
    context: str,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    return await _asgi_get(
        app,
        f"/v1/contexts/{context}/secrets",
        headers=request_headers,
    )


async def _get_instance_secret_statuses(
    app: FastAPI,
    context: str,
    instance: str,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    return await _asgi_get(
        app,
        f"/v1/contexts/{context}/instances/{instance}/secrets",
        headers=request_headers,
    )


async def _get_secret_status(
    app: FastAPI,
    secret_id: str,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    return await _asgi_get(app, f"/v1/secrets/{secret_id}", headers=request_headers)


async def _get_preview_record(
    app: FastAPI,
    preview_id: str,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    return await _asgi_get(app, f"/v1/previews/{preview_id}", headers=request_headers)


async def _get_preview_history(
    app: FastAPI,
    preview_id: str,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    return await _asgi_get(
        app,
        f"/v1/previews/{preview_id}/history",
        headers=request_headers,
    )


async def _post_backup_gate_evidence(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/evidence/backup-gates",
        headers=request_headers,
        payload=payload,
    )


async def _post_public_ingress_monitor(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/products/public-ingress-monitor/run-once",
        headers=request_headers,
        payload=payload,
    )


async def _post_promotion_evidence(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/evidence/promotions",
        headers=request_headers,
        payload=payload,
    )


async def _post_runtime_key_safety_policy_apply(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/runtime-key-safety/policies/apply",
        headers=request_headers,
        payload=payload,
    )


async def _post_preview_generation_evidence(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/evidence/previews/generations",
        headers=request_headers,
        payload=payload,
    )


async def _post_preview_destroyed_evidence(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/evidence/previews/destroyed",
        headers=request_headers,
        payload=payload,
    )


async def _post_runner_host_hygiene_audit_evidence(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/evidence/runner-host-hygiene/audits",
        headers=request_headers,
        payload=payload,
    )


async def _post_runner_lane_registration_audit_evidence(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/evidence/runner-lane-registration/audits",
        headers=request_headers,
        payload=payload,
    )


async def _post_deployment_evidence(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/evidence/deployments",
        headers=request_headers,
        payload=payload,
    )


async def _asgi_get(
    app: FastAPI, path: str, *, headers: dict[str, str] | None = None
) -> _AsgiResponse:
    return await _asgi_request(app, "GET", path, headers=headers)


async def _asgi_request(
    app: FastAPI,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    extra_headers: list[tuple[str, str]] | None = None,
    payload: dict[str, object] | None = None,
    raw_body: bytes | None = None,
    set_content_length: bool = True,
) -> _AsgiResponse:
    request_path, separator, raw_query_string = path.partition("?")
    request_headers_dict = dict(headers or {})
    body = b""
    if raw_body is not None:
        body = raw_body
    elif payload is not None:
        body = json.dumps(payload).encode("utf-8")
        request_headers_dict.setdefault("Content-Type", "application/json")
    if set_content_length:
        request_headers_dict.setdefault("Content-Length", str(len(body)))
    request_headers = [
        (key.lower().encode("ascii"), value.encode("latin-1"))
        for key, value in request_headers_dict.items()
    ]
    request_headers.extend(
        (key.lower().encode("ascii"), value.encode("latin-1")) for key, value in extra_headers or []
    )
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": request_path,
        "raw_path": request_path.encode("ascii"),
        "query_string": raw_query_string.encode("ascii") if separator else b"",
        "headers": request_headers,
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    messages = [
        {"type": "http.request", "body": body, "more_body": False},
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


class _StubFastApiGitHubOAuthClient:
    def __init__(
        self,
        identity: GitHubHumanIdentity,
        *,
        fail_fetch: bool = False,
        permission_error: bool = False,
    ) -> None:
        self.identity = identity
        self.fail_fetch = fail_fetch
        self.permission_error = permission_error
        self.authorization_state = ""
        self.code_verifier = ""

    def authorization_url(self, *, state: str, code_challenge: str) -> str:
        self.authorization_state = state
        return f"https://github.example/authorize?state={state}&challenge={code_challenge}"

    def fetch_identity(
        self,
        *,
        code: str,
        code_verifier: str,
        authz_policy: LaunchplaneAuthzPolicy,
    ) -> GitHubHumanIdentity:
        del authz_policy
        self.code_verifier = code_verifier
        if self.permission_error:
            raise PermissionError("not authorized")
        if self.fail_fetch or code != "github-code":
            raise ValueError("unexpected code")
        return self.identity


class _MissingProductReadStore:
    pass


class _ConcurrentProductConfigDryRunMarkerStore:
    def __init__(
        self, *, after_write: Literal["matching", "missing", "mismatched"] = "matching"
    ) -> None:
        self.after_write = after_write
        self.read_calls = 0
        self.write_calls = 0
        self._stored_record: LaunchplaneIdempotencyRecord | None = None

    def read_idempotency_record(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
    ) -> LaunchplaneIdempotencyRecord | None:
        self.read_calls += 1
        if self._stored_record is None:
            return None
        if (
            self._stored_record.scope != scope
            or self._stored_record.route_path != route_path
            or self._stored_record.idempotency_key != idempotency_key
        ):
            return None
        return self._stored_record

    def write_idempotency_record(self, record: LaunchplaneIdempotencyRecord) -> None:
        self.write_calls += 1
        if self.after_write == "matching":
            self._stored_record = record
        elif self.after_write == "mismatched":
            self._stored_record = record.model_copy(
                update={"request_fingerprint": f"mismatched-{record.request_fingerprint}"}
            )
        else:
            self._stored_record = None
        raise RuntimeError("simulated duplicate dry-run marker write")


class _EmptyStore:
    backend_name = "test-empty"

    def close(self) -> None:
        return None


class _SecretStatusProbeStore:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def read_secret_record(self, secret_id: str) -> object:
        self.calls.append(f"read_secret_record:{secret_id}")
        raise FileNotFoundError(secret_id)

    def list_secret_records(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[object, ...]:
        del integration, context_name, instance_name, limit
        self.calls.append("list_secret_records")
        return ()

    def read_secret_version(self, version_id: str) -> object:
        self.calls.append(f"read_secret_version:{version_id}")
        raise FileNotFoundError(version_id)

    def list_secret_versions(self, *, secret_id: str) -> tuple[object, ...]:
        self.calls.append(f"list_secret_versions:{secret_id}")
        return ()

    def list_secret_bindings(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[object, ...]:
        del integration, context_name, instance_name, limit
        self.calls.append("list_secret_bindings")
        return ()

    def list_secret_audit_events(self, *, secret_id: str) -> tuple[object, ...]:
        self.calls.append(f"list_secret_audit_events:{secret_id}")
        return ()


class _RecentOperationsProbeStore:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def list_environment_inventory(self) -> tuple[EnvironmentInventory, ...]:
        self.calls.append("list_environment_inventory")
        return ()

    def list_deployment_records(
        self,
        *,
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[DeploymentRecord, ...]:
        del context_name, instance_name, limit
        self.calls.append("list_deployment_records")
        return ()

    def list_promotion_records(
        self,
        *,
        context_name: str = "",
        from_instance_name: str = "",
        to_instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[PromotionRecord, ...]:
        del context_name, from_instance_name, to_instance_name, limit
        self.calls.append("list_promotion_records")
        return ()

    def list_preview_records(
        self,
        *,
        context_name: str = "",
        anchor_repo: str = "",
        anchor_pr_number: int | None = None,
        limit: int | None = None,
    ) -> tuple[PreviewRecord, ...]:
        del context_name, anchor_repo, anchor_pr_number, limit
        self.calls.append("list_preview_records")
        return ()


class _PreviewRecordOnlyStore:
    def __init__(self, record: PreviewRecord) -> None:
        self._record = record

    def read_preview_record(self, preview_id: str) -> PreviewRecord:
        if preview_id != self._record.preview_id:
            raise FileNotFoundError(f"No preview record found for {preview_id}.")
        return self._record


class _PreviewHistoryProbeStore:
    def __init__(self, record: PreviewRecord) -> None:
        self._record = record
        self.list_preview_generation_calls = 0

    def read_preview_record(self, preview_id: str) -> PreviewRecord:
        if preview_id != self._record.preview_id:
            raise FileNotFoundError(f"No preview record found for {preview_id}.")
        return self._record

    def list_preview_generation_records(
        self,
        *,
        preview_id: str = "",
        limit: int | None = None,
    ) -> tuple[PreviewGenerationRecord, ...]:
        del preview_id, limit
        self.list_preview_generation_calls += 1
        return ()


class _BackupGateEvidenceOnlyStore:
    def __init__(self) -> None:
        self.backup_gate_records: dict[str, dict[str, Any]] = {}

    def write_backup_gate_record(self, record: BackupGateRecord) -> None:
        self.backup_gate_records[record.record_id] = record.model_dump(mode="json")


class _IdempotencyOnlyBackupGateReplayStore:
    def __init__(self) -> None:
        self.read_idempotency_calls = 0
        self.write_backup_gate_calls = 0
        self._stored_record: Any | None = None
        self.write_backup_gate_record: Callable[[BackupGateRecord], None] | None = (
            self._write_backup_gate_record
        )

    def read_idempotency_record(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
    ) -> Any:
        self.read_idempotency_calls += 1
        if self._stored_record is None:
            return None
        if (
            self._stored_record.scope != scope
            or self._stored_record.route_path != route_path
            or self._stored_record.idempotency_key != idempotency_key
        ):
            return None
        return self._stored_record

    def write_idempotency_record(self, record: Any) -> None:
        self._stored_record = record

    def _write_backup_gate_record(self, record: BackupGateRecord) -> None:
        self.write_backup_gate_calls += 1


class _PublicIngressMonitorIdempotencyReplayStore:
    def __init__(self, *, payload: dict[str, object], idempotency_key: str) -> None:
        self.read_idempotency_calls = 0
        self._payload = payload
        self._idempotency_key = idempotency_key

    def read_idempotency_record(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
    ) -> LaunchplaneIdempotencyRecord | None:
        self.read_idempotency_calls += 1
        if (
            route_path != "/v1/products/public-ingress-monitor/run-once"
            or idempotency_key != self._idempotency_key
        ):
            return None
        return LaunchplaneIdempotencyRecord(
            record_id="idempotency-launchplane_req_original",
            scope=scope,
            route_path=route_path,
            idempotency_key=idempotency_key,
            request_fingerprint=hashlib.sha256(
                json.dumps(self._payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            response_status_code=202,
            response_trace_id="launchplane_req_original",
            recorded_at="2026-05-29T12:00:00Z",
            response_payload={
                "status": "accepted",
                "trace_id": "launchplane_req_original",
                "records": {},
                "result": {"target_count": 1},
            },
        )

    def write_idempotency_record(self, record: LaunchplaneIdempotencyRecord) -> None:
        raise AssertionError("idempotent replay must not write a new record")


class _ProductProfileReplayOnlyStore:
    def __init__(self, *, payload: dict[str, object], idempotency_key: str) -> None:
        self.read_idempotency_calls = 0
        self._payload = payload
        self._idempotency_key = idempotency_key

    def read_idempotency_record(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
    ) -> LaunchplaneIdempotencyRecord | None:
        self.read_idempotency_calls += 1
        if route_path != "/v1/product-profiles" or idempotency_key != self._idempotency_key:
            return None
        return LaunchplaneIdempotencyRecord(
            record_id="idempotency-launchplane_req_original",
            scope=scope,
            route_path=route_path,
            idempotency_key=idempotency_key,
            request_fingerprint=hashlib.sha256(
                json.dumps(self._payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            response_status_code=202,
            response_trace_id="launchplane_req_original",
            recorded_at="2026-05-29T12:00:00Z",
            response_payload={
                "status": "accepted",
                "trace_id": "launchplane_req_original",
                "records": {"product_profile": "sellyouroutboard"},
            },
        )

    def write_idempotency_record(self, record: LaunchplaneIdempotencyRecord) -> None:
        raise AssertionError("idempotent replay must not write a new record")


class _EveryCodeClaimReplayOnlyStore:
    def __init__(self, *, payload: dict[str, object], idempotency_key: str) -> None:
        self.read_idempotency_calls = 0
        self.claim_calls = 0
        self._payload = payload
        self._idempotency_key = idempotency_key

    def read_idempotency_record(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
    ) -> LaunchplaneIdempotencyRecord | None:
        self.read_idempotency_calls += 1
        if (
            route_path != "/v1/every-code/work-requests/claim"
            or idempotency_key != self._idempotency_key
        ):
            return None
        return LaunchplaneIdempotencyRecord(
            record_id="idempotency-launchplane_req_original",
            scope=scope,
            route_path=route_path,
            idempotency_key=idempotency_key,
            request_fingerprint=hashlib.sha256(
                json.dumps(self._payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            response_status_code=202,
            response_trace_id="launchplane_req_original",
            recorded_at="2026-05-29T12:00:00Z",
            response_payload={
                "status": "accepted",
                "trace_id": "launchplane_req_original",
                "records": {
                    "request_id": "every-code-cbusillo-code-123-test",
                    "state": "claimed",
                },
                "result": {
                    "request": {
                        "request_id": "every-code-cbusillo-code-123-test",
                        "state": "claimed",
                        "claimed_by_host": "Runner-Host",
                    }
                },
            },
        )

    def write_idempotency_record(self, record: LaunchplaneIdempotencyRecord) -> None:
        raise AssertionError("idempotent replay must not write a new record")

    def claim_every_code_work_request_record(
        self,
        *,
        request_id: str,
        host: str,
        claimed_at: str,
    ) -> EveryCodeWorkRequestRecord | None:
        del request_id, host, claimed_at
        self.claim_calls += 1
        raise AssertionError("idempotent replay must not claim a record")


class _EveryCodeStatusReplayOnlyStore:
    def __init__(self, *, payload: dict[str, object], idempotency_key: str) -> None:
        self.read_idempotency_calls = 0
        self.read_calls = 0
        self.write_calls = 0
        self._payload = payload
        self._idempotency_key = idempotency_key

    def read_idempotency_record(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
    ) -> LaunchplaneIdempotencyRecord | None:
        self.read_idempotency_calls += 1
        if (
            route_path != "/v1/every-code/work-requests/status"
            or idempotency_key != self._idempotency_key
        ):
            return None
        return LaunchplaneIdempotencyRecord(
            record_id="idempotency-launchplane_req_original",
            scope=scope,
            route_path=route_path,
            idempotency_key=idempotency_key,
            request_fingerprint=hashlib.sha256(
                json.dumps(self._payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            response_status_code=202,
            response_trace_id="launchplane_req_original",
            recorded_at="2026-05-29T12:00:00Z",
            response_payload={
                "status": "accepted",
                "trace_id": "launchplane_req_original",
                "records": {
                    "request_id": "every-code-cbusillo-code-123-test",
                    "state": "done",
                },
                "result": {
                    "request": {
                        "request_id": "every-code-cbusillo-code-123-test",
                        "state": "done",
                        "result_pr_url": "https://github.com/cbusillo/code/pull/26",
                    },
                    "notifications": [],
                },
            },
        )

    def write_idempotency_record(self, record: LaunchplaneIdempotencyRecord) -> None:
        raise AssertionError("idempotent replay must not write a new record")

    def read_every_code_work_request_record(self, request_id: str) -> EveryCodeWorkRequestRecord:
        del request_id
        self.read_calls += 1
        raise AssertionError("idempotent replay must not read a work request")

    def write_every_code_work_request_record(self, record: EveryCodeWorkRequestRecord) -> None:
        del record
        self.write_calls += 1
        raise AssertionError("idempotent replay must not write a work request")


class _EveryCodeRerunReplayOnlyStore:
    def __init__(self, *, payload: dict[str, object], idempotency_key: str) -> None:
        self.read_idempotency_calls = 0
        self.read_calls = 0
        self.write_calls = 0
        self._payload = payload
        self._idempotency_key = idempotency_key

    def read_idempotency_record(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
    ) -> LaunchplaneIdempotencyRecord | None:
        self.read_idempotency_calls += 1
        if (
            route_path != "/v1/every-code/work-requests/rerun"
            or idempotency_key != self._idempotency_key
        ):
            return None
        return LaunchplaneIdempotencyRecord(
            record_id="idempotency-launchplane_req_original",
            scope=scope,
            route_path=route_path,
            idempotency_key=idempotency_key,
            request_fingerprint=hashlib.sha256(
                json.dumps(self._payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            response_status_code=202,
            response_trace_id="launchplane_req_original",
            recorded_at="2026-05-29T12:00:00Z",
            response_payload={
                "status": "accepted",
                "trace_id": "launchplane_req_original",
                "records": {
                    "request_id": "every-code-cbusillo-code-123-test",
                    "state": "queued",
                    "agent_write_intent_record_id": "agent-write-intent-test",
                },
                "result": {
                    "request": {
                        "request_id": "every-code-cbusillo-code-123-test",
                        "state": "queued",
                        "trigger_actor": "cbusillo",
                    }
                },
            },
        )

    def write_idempotency_record(self, record: LaunchplaneIdempotencyRecord) -> None:
        del record
        raise AssertionError("idempotent replay must not write a new record")

    def read_every_code_work_request_record(self, request_id: str) -> EveryCodeWorkRequestRecord:
        del request_id
        self.read_calls += 1
        raise AssertionError("idempotent replay must not read a work request")

    def write_every_code_work_request_record(self, record: EveryCodeWorkRequestRecord) -> None:
        del record
        self.write_calls += 1
        raise AssertionError("idempotent replay must not write a work request")

    def read_agent_write_intent_record(self, record_id: str) -> AgentWriteIntentRecord:
        del record_id
        raise AssertionError("idempotent replay must not read write-intent evidence")

    def list_agent_write_intent_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        status: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[AgentWriteIntentRecord, ...]:
        del product, context_name, status, limit, offset
        raise AssertionError("idempotent replay must not list write-intent evidence")


class _AgentWriteIntentEvaluateReplayOnlyStore:
    def __init__(self, *, payload: dict[str, object], idempotency_key: str) -> None:
        self.read_idempotency_calls = 0
        self._payload = payload
        self._idempotency_key = idempotency_key

    def read_idempotency_record(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
    ) -> LaunchplaneIdempotencyRecord | None:
        self.read_idempotency_calls += 1
        if (
            route_path != "/v1/agent/write-intents/evaluate"
            or idempotency_key != self._idempotency_key
        ):
            return None
        return LaunchplaneIdempotencyRecord(
            record_id="idempotency-launchplane_req_original",
            scope=scope,
            route_path=route_path,
            idempotency_key=idempotency_key,
            request_fingerprint=hashlib.sha256(
                json.dumps(self._payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            response_status_code=202,
            response_trace_id="launchplane_req_original",
            recorded_at="2026-05-29T12:00:00Z",
            response_payload={
                "status": "accepted",
                "trace_id": "launchplane_req_original",
                "records": {},
                "result": {
                    "intent": {
                        "status": "allowed",
                        "intent": "every_code_rerun",
                    },
                    "record": {
                        "record_id": "agent-write-intent-original",
                        "recorded_at": "2026-05-29T12:00:00Z",
                    },
                },
            },
        )

    def write_idempotency_record(self, record: LaunchplaneIdempotencyRecord) -> None:
        del record
        raise AssertionError("idempotent replay must not write a new record")

    def write_agent_write_intent_record(self, record: AgentWriteIntentRecord) -> None:
        del record
        raise AssertionError("idempotent replay must not write write-intent evidence")


class _ProductContextApplyReplayOnlyStore:
    def __init__(
        self,
        *,
        route_path: str,
        payload: dict[str, object],
        idempotency_key: str,
    ) -> None:
        self.read_idempotency_calls = 0
        self._route_path = route_path
        self._payload = payload
        self._idempotency_key = idempotency_key

    def read_idempotency_record(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
    ) -> LaunchplaneIdempotencyRecord | None:
        self.read_idempotency_calls += 1
        if route_path != self._route_path or idempotency_key != self._idempotency_key:
            return None
        return LaunchplaneIdempotencyRecord(
            record_id="idempotency-launchplane_req_original",
            scope=scope,
            route_path=route_path,
            idempotency_key=idempotency_key,
            request_fingerprint=hashlib.sha256(
                json.dumps(self._payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            response_status_code=202,
            response_trace_id="launchplane_req_original",
            recorded_at="2026-05-29T12:00:00Z",
            response_payload={
                "status": "accepted",
                "trace_id": "launchplane_req_original",
                "records": {"product_profile": "sellyouroutboard"},
            },
        )

    def write_idempotency_record(self, record: LaunchplaneIdempotencyRecord) -> None:
        raise AssertionError("idempotent replay must not write a new record")


class _IdempotencyOnlyRunnerHostHygieneAuditReplayStore:
    def __init__(self) -> None:
        self.read_idempotency_calls = 0
        self.write_runner_host_hygiene_audit_calls = 0
        self._stored_record: Any | None = None
        self.write_runner_host_hygiene_audit_record: (
            Callable[[RunnerHostHygieneApplyAuditRecord], None] | None
        ) = self._write_runner_host_hygiene_audit_record

    def read_idempotency_record(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
    ) -> Any:
        self.read_idempotency_calls += 1
        if self._stored_record is None:
            return None
        if (
            self._stored_record.scope != scope
            or self._stored_record.route_path != route_path
            or self._stored_record.idempotency_key != idempotency_key
        ):
            return None
        return self._stored_record

    def write_idempotency_record(self, record: Any) -> None:
        self._stored_record = record

    def _write_runner_host_hygiene_audit_record(
        self,
        record: RunnerHostHygieneApplyAuditRecord,
    ) -> None:
        self.write_runner_host_hygiene_audit_calls += 1


class _IdempotencyOnlyRunnerLaneRegistrationAuditReplayStore:
    def __init__(self) -> None:
        self.read_idempotency_calls = 0
        self.write_runner_lane_registration_audit_calls = 0
        self._stored_record: Any | None = None
        self.write_runner_lane_registration_audit_record: (
            Callable[[RunnerLaneRegistrationAuditRecord], None] | None
        ) = self._write_runner_lane_registration_audit_record

    def read_idempotency_record(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
    ) -> Any:
        self.read_idempotency_calls += 1
        if self._stored_record is None:
            return None
        if (
            self._stored_record.scope != scope
            or self._stored_record.route_path != route_path
            or self._stored_record.idempotency_key != idempotency_key
        ):
            return None
        return self._stored_record

    def write_idempotency_record(self, record: Any) -> None:
        self._stored_record = record

    def _write_runner_lane_registration_audit_record(
        self,
        record: RunnerLaneRegistrationAuditRecord,
    ) -> None:
        self.write_runner_lane_registration_audit_calls += 1


class _PromotionEvidenceOnlyStore:
    def __init__(self) -> None:
        self.promotion_records: dict[str, dict[str, Any]] = {}

    def write_promotion_record(self, record: PromotionRecord) -> None:
        self.promotion_records[record.record_id] = record.model_dump(mode="json")


class _IdempotencyOnlyPromotionReplayStore:
    def __init__(self) -> None:
        self.read_idempotency_calls = 0
        self.write_promotion_calls = 0
        self._stored_record: Any | None = None
        self.write_promotion_record: Callable[[PromotionRecord], None] | None = (
            self._write_promotion_record
        )

    def read_idempotency_record(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
    ) -> Any:
        self.read_idempotency_calls += 1
        if self._stored_record is None:
            return None
        if (
            self._stored_record.scope != scope
            or self._stored_record.route_path != route_path
            or self._stored_record.idempotency_key != idempotency_key
        ):
            return None
        return self._stored_record

    def write_idempotency_record(self, record: Any) -> None:
        self._stored_record = record

    def _write_promotion_record(self, record: PromotionRecord) -> None:
        self.write_promotion_calls += 1


class _IdempotencyOnlyPreviewGenerationReplayStore:
    def __init__(self) -> None:
        self.read_idempotency_calls = 0
        self.write_preview_generation_evidence_calls = 0
        self._stored_record: Any | None = None
        self._preview_records: dict[str, Any] = {}
        self._generation_records: dict[str, Any] = {}
        self.write_preview_generation_evidence_records: Callable[..., tuple[str, str]] | None = (
            self._write_preview_generation_evidence_records
        )

    def read_idempotency_record(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
    ) -> Any:
        self.read_idempotency_calls += 1
        if self._stored_record is None:
            return None
        if (
            self._stored_record.scope != scope
            or self._stored_record.route_path != route_path
            or self._stored_record.idempotency_key != idempotency_key
        ):
            return None
        return self._stored_record

    def write_idempotency_record(self, record: Any) -> None:
        self._stored_record = record

    def list_preview_records(
        self,
        *,
        context_name: str = "",
        anchor_repo: str = "",
        anchor_pr_number: int | None = None,
        limit: int | None = None,
    ) -> tuple[Any, ...]:
        records = [
            record
            for record in self._preview_records.values()
            if (not context_name or record.context == context_name)
            and (not anchor_repo or record.anchor_repo == anchor_repo)
            and (anchor_pr_number is None or record.anchor_pr_number == anchor_pr_number)
        ]
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_preview_record(self, record: Any) -> str:
        self._preview_records[record.preview_id] = record
        return f"preview://{record.preview_id}"

    def list_preview_generation_records(
        self, *, preview_id: str = "", limit: int | None = None
    ) -> tuple[Any, ...]:
        records = [
            record
            for record in self._generation_records.values()
            if not preview_id or record.preview_id == preview_id
        ]
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_preview_generation_record(self, record: Any) -> str:
        self._generation_records[record.generation_id] = record
        return f"generation://{record.generation_id}"

    def _write_preview_generation_evidence_records(
        self,
        *,
        preview_record: Any,
        generation_record: Any,
    ) -> tuple[str, str]:
        self.write_preview_generation_evidence_calls += 1
        generation_path = self.write_preview_generation_record(generation_record)
        preview_path = self.write_preview_record(preview_record)
        return generation_path, preview_path


class _IdempotencyOnlyPreviewDestroyedReplayStore:
    def __init__(self) -> None:
        self.read_idempotency_calls = 0
        self.write_preview_record_calls = 0
        self._stored_record: Any | None = None
        self._preview_records: dict[str, Any] = {}
        self.write_preview_record: Callable[[Any], str] | None = self._write_preview_record

    def seed_preview(self, record: PreviewRecord) -> None:
        self._preview_records[record.preview_id] = record

    def read_idempotency_record(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
    ) -> Any:
        self.read_idempotency_calls += 1
        if self._stored_record is None:
            return None
        if (
            self._stored_record.scope != scope
            or self._stored_record.route_path != route_path
            or self._stored_record.idempotency_key != idempotency_key
        ):
            return None
        return self._stored_record

    def write_idempotency_record(self, record: Any) -> None:
        self._stored_record = record

    def list_preview_records(
        self,
        *,
        context_name: str = "",
        anchor_repo: str = "",
        anchor_pr_number: int | None = None,
        limit: int | None = None,
    ) -> tuple[Any, ...]:
        records = [
            record
            for record in self._preview_records.values()
            if (not context_name or record.context == context_name)
            and (not anchor_repo or record.anchor_repo == anchor_repo)
            and (anchor_pr_number is None or record.anchor_pr_number == anchor_pr_number)
        ]
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def _write_preview_record(self, record: Any) -> str:
        self.write_preview_record_calls += 1
        self._preview_records[record.preview_id] = record
        return f"preview://{record.preview_id}"


class _FailingOnceIdempotencyPreviewGenerationStore(FilesystemRecordStore):
    def __init__(self, *, state_dir: Path) -> None:
        super().__init__(state_dir=state_dir)
        self.fail_next_idempotency_write = True

    def write_idempotency_record(self, record: LaunchplaneIdempotencyRecord) -> Path:
        if self.fail_next_idempotency_write:
            self.fail_next_idempotency_write = False
            raise RuntimeError("idempotency write failed")
        return super().write_idempotency_record(record)


class _DeploymentEvidenceOnlyStore:
    def __init__(self) -> None:
        self.deployment_records: dict[str, dict[str, Any]] = {}
        self.environment_inventories: list[dict[str, Any]] = []

    def write_deployment_record(self, record: DeploymentRecord) -> None:
        self.deployment_records[record.record_id] = record.model_dump(mode="json")

    def write_environment_inventory(self, inventory: Any) -> None:
        self.environment_inventories.append(inventory.model_dump(mode="json"))


class _IdempotencyOnlyReplayStore:
    def __init__(self) -> None:
        self.read_idempotency_calls = 0
        self.write_deployment_calls = 0
        self.write_environment_inventory_calls = 0
        self._stored_record: Any | None = None
        self.write_deployment_record: Callable[[DeploymentRecord], None] | None = (
            self._write_deployment_record
        )
        self.write_environment_inventory: Callable[[Any], None] | None = (
            self._write_environment_inventory
        )

    def read_idempotency_record(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
    ) -> Any:
        self.read_idempotency_calls += 1
        if self._stored_record is None:
            return None
        if (
            self._stored_record.scope != scope
            or self._stored_record.route_path != route_path
            or self._stored_record.idempotency_key != idempotency_key
        ):
            return None
        return self._stored_record

    def write_idempotency_record(self, record: Any) -> None:
        self._stored_record = record

    def _write_deployment_record(self, record: DeploymentRecord) -> None:
        self.write_deployment_calls += 1

    def _write_environment_inventory(self, inventory: Any) -> None:
        self.write_environment_inventory_calls += 1


class _RejectingVerifier:
    def verify(self, token: str) -> GitHubActionsIdentity:
        raise InvalidTokenError("signature verification failed")
