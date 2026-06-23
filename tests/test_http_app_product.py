import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import (
    Any,
    cast,
)
from unittest.mock import patch

from a2wsgi import WSGIMiddleware
from starlette.types import ASGIApp

from control_plane import secrets as control_plane_secrets
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.work_graph_read_model import WorkGraphPlanningIssueFacts
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import (
    BearerIdentityConfig,
    LaunchplaneAuthzPolicy,
)
from control_plane.service_human_auth import (
    HumanSessionManager,
    InMemoryHumanSessionStore,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.work_graph_issue_inbox import (
    GitHubIssueInboxReadModel,
    GitHubIssueInboxReconcileResult,
)
from tests.async_case import AsyncTestCase
from tests.http_app_test_support import (
    _asgi_get,
    _asgi_request,
    _get_agent_context,
    _get_config_status,
    _get_product,
    _get_product_activity,
    _get_product_environment,
    _get_product_environments,
    _get_product_profile,
    _get_product_profiles,
    _get_products,
    _get_protected_artifacts,
    _get_repo_product_mapping,
    _get_work_graph_issue_inbox,
    _get_work_graph_snapshot,
    _github_human_artifact_protection_policy,
    _github_human_identity,
    _github_human_work_graph_rank_policy,
    _github_oauth_config,
    _local_admin_work_graph_rank_policy,
    _local_operator_artifact_protection_policy,
    _local_operator_bearer_config,
    _local_operator_product_environment_read_policy,
    _local_operator_work_graph_rank_policy,
    _MissingProductReadStore,
    _post_product_profile,
    _post_work_graph_issue_inbox_reconcile,
    _post_work_graph_rank,
    _product_environment_read_policy,
    _product_profile_read_policy,
    _product_profile_write_policy,
    _ProductProfileReplayOnlyStore,
    _RejectingVerifier,
    _seed_agent_context_read_records,
    _seed_empty_agent_context_read_store,
    _seed_product_environment_read_records,
    _terminal_agent_launchplane_read_policy,
    _terminal_agent_product_environment_read_policy,
    _terminal_agent_work_graph_rank_policy,
    _work_graph_read_policy,
)
from tests.test_protected_artifacts import _seed_store as seed_protected_artifact_store
from tests.test_service import (
    _generic_site_profile_payload,
    _identity,
    _product_profile_payload,
    _sqlite_database_url,
    _StubVerifier,
    _work_graph_snapshot_payload,
    create_launchplane_service_app,
)


class FastApiProductEnvironmentConfigStatusTests(AsyncTestCase):
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


class FastApiProductEnvironmentReadTests(AsyncTestCase):
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


class FastApiProductProfileTests(AsyncTestCase):
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


class FastApiProtectedArtifactsTests(AsyncTestCase):
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
