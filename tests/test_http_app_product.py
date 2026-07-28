import asyncio
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from unittest.mock import patch

from control_plane import secrets as control_plane_secrets
from control_plane.contracts.private_health_endpoint_record import PrivateHealthEndpointRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.runtime_key_safety_policy import RuntimeSecretSafetyRule
from control_plane.contracts.work_graph_read_model import WorkGraphPlanningIssueFacts
from control_plane.http_app import (
    create_launchplane_fastapi_app,
    idempotency_request_fingerprint,
    idempotency_scope,
)
from control_plane.product_config_service import product_config_write_prerequisites
from control_plane.service_auth import (
    BearerIdentityConfig,
    GitHubActionsIdentity,
    LaunchplaneAuthzPolicy,
)
from control_plane.service_human_auth import (
    HumanSessionManager,
    InMemoryHumanSessionStore,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import (
    PostgresRecordStore,
    ProductProfileCompareWriteResult,
)
from control_plane.work_graph_issue_inbox import (
    GitHubIssueInboxReadModel,
    GitHubIssueInboxReconcileResult,
)
from control_plane.work_graph_service import (
    WorkGraphIssueInboxReconcileResponse,
    WorkGraphRankResponse,
)
from tests.http_app_test_support import (
    _asgi_get,
    _asgi_request,
    _browser_mutation_headers,
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
    _post_product_expected_config,
    _post_product_health_monitoring,
    _post_product_profile,
    _post_product_preview_tls,
    _post_work_graph_issue_inbox_reconcile,
    _post_work_graph_rank,
    _product_environment_read_policy,
    _product_expected_config_policy,
    _product_health_monitoring_policy,
    _product_profile_read_policy,
    _product_preview_tls_policy,
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
from tests.support.protected_artifacts import seed_protected_artifact_store
from tests.support.auth import _identity, _StubVerifier
from tests.support.product_reads import (
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
)
from tests.support.profiles import (
    _generic_site_profile_payload,
    _product_profile_payload,
)
from tests.support.stores import _sqlite_database_url, _write_runtime_key_safety_policy
from tests.support.work_graph import _work_graph_snapshot_payload


def _product_expected_config_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "product": "sellyouroutboard",
        "mode": "dry-run",
        "reason": "Test expected config metadata update.",
        "source_label": "product-expected-config-test",
        "managed_secret_bindings": [
            {
                "binding_key": "SMTP_PASSWORD",
                "integration": "runtime_environment",
                "context": "sellyouroutboard-prod",
                "instance": "prod",
            }
        ],
    }


def _product_expected_config_identity() -> GitHubActionsIdentity:
    return _identity(
        workflow_ref="every/verireel/.github/workflows/product-expected-config.yml@refs/heads/main",
        event_name="workflow_dispatch",
    )


def _product_preview_tls_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "product": "odoo-product",
        "mode": "dry-run",
        "domain_certificate_type": "letsencrypt",
        "reason": "Enable trusted preview TLS.",
        "reviewed_plan_sha256": "",
    }


def _product_preview_tls_profile() -> LaunchplaneProductProfileRecord:
    payload = _product_profile_payload()
    payload["product"] = "odoo-product"
    payload["driver_id"] = "odoo"
    preview = cast(dict[str, object], payload["preview"])
    preview["domain_certificate_type"] = "none"
    return LaunchplaneProductProfileRecord.model_validate(payload)


def _product_preview_tls_identity() -> GitHubActionsIdentity:
    return _identity(
        workflow_ref="every/verireel/.github/workflows/product-preview-tls.yml@refs/heads/main",
        event_name="workflow_dispatch",
    )


def _product_health_monitoring_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "product": "odoo-product",
        "context": "cm",
        "instance": "testing",
        "check_name": "public-ingress",
        "check_kind": "public_http",
        "monitoring_intent": "public",
        "enabled": True,
        "require_runtime_identity": True,
        "private_endpoint_key": "",
        "mode": "dry-run",
        "reason": "Require strict public runtime identity.",
        "reviewed_plan_sha256": "",
    }


def _product_health_monitoring_profile() -> LaunchplaneProductProfileRecord:
    payload = _product_profile_payload()
    payload["product"] = "odoo-product"
    payload["driver_id"] = "odoo"
    lanes = cast(list[dict[str, object]], payload["lanes"])
    lanes[0]["context"] = "cm"
    lanes[0]["instance"] = "testing"
    lanes[0]["base_url"] = "https://cm-testing.example.com"
    lanes[0]["health_url"] = "https://cm-testing.example.com/launchplane/health"
    lanes[0]["health_monitoring"] = {
        "monitoring_intent": "public",
        "checks": [
            {
                "name": "public-ingress",
                "kind": "public_http",
                "enabled": True,
                "url": "",
                "require_runtime_identity": False,
            }
        ],
    }
    return LaunchplaneProductProfileRecord.model_validate(payload)


def _product_health_monitoring_identity(
    *,
    job_workflow_ref: str = (
        "cbusillo/launchplane/.github/workflows/reusable-product-health-monitoring.yml@"
        "88584ae2800bceabc9d448eba7defddc5da75ec1"
    ),
) -> GitHubActionsIdentity:
    return _identity(
        workflow_ref=(
            "every/verireel/.github/workflows/product-health-monitoring.yml@refs/heads/main"
        ),
        job_workflow_ref=job_workflow_ref,
        event_name="workflow_dispatch",
    )


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
        self.assertFalse(config_status["write_availability"]["runtime_settings"]["plan"]["enabled"])
        self.assertIn(
            "Caller is not authorized to plan product configuration.",
            config_status["write_availability"]["runtime_settings"]["plan"]["disabled_reasons"],
        )

    async def test_config_status_exposes_authoritative_product_config_availability(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _write_runtime_key_safety_policy(
                database_url=database_url,
                rules=(
                    RuntimeSecretSafetyRule(
                        binding_key="SMTP_PASSWORD",
                        secret_class="prod_only",
                        allowed_contexts=("example-site",),
                        allowed_instances=("prod",),
                    ),
                    RuntimeSecretSafetyRule(
                        binding_key="RESEND_API_KEY",
                        secret_class="prod_only",
                        allowed_contexts=("example-site",),
                        allowed_instances=("prod",),
                    ),
                ),
            )
            store = PostgresRecordStore(database_url=database_url)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_generic_site_profile_payload())
            )
            store.close()
            app_store = PostgresRecordStore(database_url=database_url)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_environment_read_policy(
                    context="example-site",
                    actions=(
                        "product_environment.read",
                        "product_config.plan",
                        "product_config.apply",
                    ),
                ),
                record_store_factory=lambda: app_store,
            )

            with patch.dict(
                "os.environ",
                {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
                clear=True,
            ):
                response = await _get_config_status(app)
            app_store.close()

        self.assertEqual(response.status_code, 200)
        availability = response.json()["config_status"]["write_availability"]
        for input_kind in ("runtime_settings", "managed_secrets"):
            self.assertTrue(availability[input_kind]["plan"]["enabled"])
            self.assertTrue(availability[input_kind]["apply"]["enabled"])
            self.assertTrue(availability[input_kind]["apply"]["requires_matching_dry_run"])
            self.assertTrue(availability[input_kind]["apply"]["requires_idempotency_key"])
            self.assertEqual(
                availability[input_kind]["apply"]["confirmation_text"],
                "APPLY example-site/prod",
            )
        self.assertEqual(
            availability["runtime_settings"]["plan"]["route_path"],
            "/v1/products/{product}/environments/{environment}/config/apply",
        )

    def test_config_status_does_not_advertise_filesystem_writes(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            profile = LaunchplaneProductProfileRecord.model_validate(
                _generic_site_profile_payload()
            )
            lane = next(candidate for candidate in profile.lanes if candidate.instance == "prod")

            with patch.dict(
                "os.environ",
                {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
                clear=True,
            ):
                prerequisites = product_config_write_prerequisites(
                    store,
                    profile=profile,
                    lane=lane,
                )

        self.assertFalse(prerequisites.storage_ready)

    async def test_config_status_checks_every_runtime_secret_policy_rule(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            _write_runtime_key_safety_policy(
                database_url=database_url,
                context_name="example-site",
            )
            store = PostgresRecordStore(database_url=database_url)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_generic_site_profile_payload())
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_environment_read_policy(
                    context="example-site",
                    actions=(
                        "product_environment.read",
                        "product_config.plan",
                        "product_config.apply",
                    ),
                ),
                record_store_factory=lambda: store,
            )

            with patch.dict(
                "os.environ",
                {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
                clear=True,
            ):
                response = await _get_config_status(app)
            store.close()

        self.assertEqual(response.status_code, 200)
        managed_secrets = response.json()["config_status"]["write_availability"]["managed_secrets"]
        self.assertFalse(managed_secrets["plan"]["enabled"])
        self.assertIn(
            "Runtime key-safety policy is unavailable.",
            managed_secrets["plan"]["disabled_reasons"],
        )

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
        WorkGraphRankResponse.model_validate(payload)
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
            headers=_browser_mutation_headers(session_manager, human_session),
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
        WorkGraphIssueInboxReconcileResponse.model_validate(payload)
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
            "#/components/schemas/WorkGraphRankResponse",
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
            "#/components/schemas/WorkGraphIssueInboxReconcileResponse",
        )
        for status_code in ("400", "401", "403"):
            self.assertIn(
                "LaunchplaneErrorResponse",
                json.dumps(reconcile_route["responses"][status_code]),
            )

        expected_write_routes = {
            "/v1/product-config/apply": (
                "apply_product_config",
                "ProductConfigApplyResponse",
                ("400", "401", "403", "409", "503"),
            ),
            "/v1/drivers/generic-web/prod-promotion": (
                "apply_generic_web_prod_promotion",
                "GenericWebProdPromotionResponse",
                ("400", "401", "403", "404", "409", "503"),
            ),
            "/v1/drivers/generic-web/prod-promotion-workflow": (
                "dispatch_generic_web_prod_promotion_workflow",
                "GenericWebPromotionWorkflowResponse",
                ("400", "401", "403", "404", "409", "503"),
            ),
            "/v1/products/{product}/environments/{environment}/promotion/dry-run": (
                "dry_run_product_promotion",
                "ProductPromotionDryRunResponse",
                ("400", "401", "403", "404", "409", "503"),
            ),
            "/v1/products/{product}/environments/{environment}/promotion/workflow-dispatch": (
                "dispatch_product_promotion_workflow",
                "ProductPromotionWorkflowDispatchResponse",
                ("400", "401", "403", "404", "409", "503"),
            ),
        }
        for path, (
            operation_id,
            response_model_name,
            error_statuses,
        ) in expected_write_routes.items():
            route = openapi["paths"][path]["post"]
            self.assertEqual(route["operationId"], operation_id)
            self.assertEqual(
                route["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
                f"#/components/schemas/{response_model_name}",
            )
            self.assertFalse(
                openapi["components"]["schemas"][response_model_name]["additionalProperties"]
            )
            for status_code in error_statuses:
                self.assertIn(
                    "LaunchplaneErrorResponse",
                    json.dumps(route["responses"][status_code]),
                )
            self.assertNotIn("422", route["responses"])

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
        authz_event = payload["activity"]["events"][0]
        self.assertEqual(authz_event["event_type"], "authz_policy")
        self.assertEqual(authz_event["product"], "example-site")
        self.assertEqual(authz_event["action_id"], "authz_policy.grant")
        self.assertEqual(authz_event["title"], "Example Site authorization granted")
        self.assertIn("test", authz_event["summary"])
        self.assertIn("example-site.read", authz_event["summary"])

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
        prod_topology = payload["environments"][1]["topology"]
        self.assertEqual(
            prod_topology["provider_recorded"]["domains"][0]["domain_name"],
            "example-site.example",
        )
        self.assertEqual(
            prod_topology["observed"]["tls_domains"][0]["status"],
            "hostname_mismatch",
        )
        self.assertNotIn("https://internal.example-site.invalid", response_text)
        self.assertNotIn("super-secret-password", response_text)
        self.assertNotIn("provider-host-private-123", response_text)
        self.assertNotIn("edge-host-private-456", response_text)
        self.assertNotIn("certificate-private-789", response_text)

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
        self.assertNotIn("target_id", environment["target"])
        topology = environment["topology"]
        self.assertEqual(topology["desired"]["domains"][0]["domain_name"], "example-site.example")
        self.assertEqual(
            topology["provider_recorded"]["placement"]["target_name"],
            "example-site-prod",
        )
        self.assertEqual(topology["provider_recorded"]["ingress"]["path"], "edge_to_provider")
        self.assertEqual(topology["provider_recorded"]["tls"]["owner"], "launchplane")
        observed_tls = topology["observed"]["tls_domains"][0]
        self.assertEqual(observed_tls["status"], "hostname_mismatch")
        self.assertEqual(observed_tls["failure_code"], "tls_hostname_mismatch")
        self.assertEqual(observed_tls["incident_status"], "open")
        self.assertIn("certificate binding", observed_tls["likely_failure_cause"])
        self.assertIn("tls_mismatch", {warning["code"] for warning in topology["warnings"]})
        self.assertEqual(environment["runtime_settings"][0]["env_keys"], ["INTERNAL_CALLBACK_URL"])
        self.assertEqual(environment["managed_secrets"][0]["binding_key"], "SMTP_PASSWORD")
        self.assertNotIn("https://internal.example-site.invalid", response_text)
        self.assertNotIn("super-secret-password", response_text)
        self.assertNotIn("app-prod-123", response_text)
        self.assertNotIn("provider-host-private-123", response_text)
        self.assertNotIn("edge-host-private-456", response_text)
        self.assertNotIn("certificate-private-789", response_text)

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
        self.assertNotIn("provider-host-private-123", response.text)
        self.assertNotIn("certificate-private-789", response.text)

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
        self.assertNotIn("provider-host-private-123", response.text)
        self.assertNotIn("certificate-private-789", response.text)

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
        topology_schema = openapi["components"]["schemas"]["ProductEnvironmentTopology"]
        self.assertEqual(
            set(topology_schema["properties"]),
            {"desired", "provider_recorded", "observed", "warnings", "trust_state"},
        )
        warning_schema = openapi["components"]["schemas"]["ProductTopologyWarning"]
        warning_codes = set(warning_schema["properties"]["code"]["enum"])
        self.assertTrue(
            {
                "missing_route_authority",
                "domain_divergence",
                "stale_route_authority",
                "tls_ownership_unknown",
                "tls_mismatch",
            }.issubset(warning_codes)
        )
        target_properties = openapi["components"]["schemas"]["ProductTargetSummary"]["properties"]
        self.assertNotIn("target_id", target_properties)


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
            profile_payload = _product_profile_payload()
            preview_payload = cast(dict[str, object], profile_payload["preview"])
            preview_payload["domain_certificate_type"] = "letsencrypt"

            response = await _post_product_profile(
                app,
                profile_payload,
                idempotency_key="profile-sellyouroutboard",
            )
            stored_profile = record_store.read_product_profile_record("sellyouroutboard")

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["records"], {"product_profile": "sellyouroutboard"})
        self.assertNotIn("result", payload)
        self.assertEqual(stored_profile.driver_id, "generic-web")
        self.assertEqual(stored_profile.preview.slug_template, "pr-{number}")
        self.assertEqual(stored_profile.preview.domain_certificate_type, "letsencrypt")

    async def test_write_product_profile_requires_bounded_apply_for_monitoring_changes(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            record_store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            existing_payload = _product_profile_payload()
            existing_lanes = cast(list[dict[str, object]], existing_payload["lanes"])
            existing_lanes[0]["health_monitoring"] = {
                "monitoring_intent": "public",
                "checks": [{"name": "public-ingress", "kind": "public_http"}],
            }
            existing_profile = LaunchplaneProductProfileRecord.model_validate(existing_payload)
            record_store.write_product_profile_record(existing_profile)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_profile_write_policy(product="sellyouroutboard"),
                record_store_factory=lambda: record_store,
            )
            replacement_payload = existing_profile.model_dump(mode="json")
            replacement_payload["lanes"][0]["health_monitoring"]["monitoring_intent"] = "prelaunch"

            response = await _post_product_profile(
                app,
                replacement_payload,
                idempotency_key="profile-monitoring-change",
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["error"]["code"],
            "health_monitoring_bounded_apply_required",
        )

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
                        "monitoring_intent": "public",
                        "checks": [{"name": "public-ingress", "kind": "public_http"}],
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

    async def test_apply_product_expected_config_dry_run_reports_additions_without_write(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            record_store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            profile = LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            record_store.write_product_profile_record(profile)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_product_expected_config_identity()),
                authz_policy=_product_expected_config_policy(),
                record_store_factory=lambda: record_store,
            )

            response = await _post_product_expected_config(
                app,
                _product_expected_config_payload(),
                idempotency_key="expected-config-dry-run",
            )
            stored_profile = record_store.read_product_profile_record("sellyouroutboard")

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["records"], {"product_profile": "sellyouroutboard"})
        self.assertEqual(payload["result"]["mode"], "dry-run")
        self.assertTrue(payload["result"]["changed"])
        self.assertEqual(
            payload["result"]["managed_secret_bindings"]["added"],
            [
                {
                    "binding_key": "SMTP_PASSWORD",
                    "integration": "runtime_environment",
                    "context": "sellyouroutboard-prod",
                    "instance": "prod",
                }
            ],
        )
        self.assertEqual(stored_profile.expected_config.managed_secret_bindings, ())

    async def test_apply_product_expected_config_persists_additive_binding(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            record_store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            record_store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_product_expected_config_identity()),
                authz_policy=_product_expected_config_policy(),
                record_store_factory=lambda: record_store,
            )

            response = await _post_product_expected_config(
                app,
                {**_product_expected_config_payload(), "mode": "apply"},
                idempotency_key="expected-config-apply",
            )
            stored_profile = record_store.read_product_profile_record("sellyouroutboard")

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["result"]["summary"]["managed_secret_binding_add_count"], 1)
        self.assertEqual(
            [
                requirement.binding_key
                for requirement in stored_profile.expected_config.managed_secret_bindings
            ],
            ["SMTP_PASSWORD"],
        )
        self.assertEqual(stored_profile.source, "product-expected-config-test")

    async def test_apply_product_expected_config_replays_idempotent_request(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            record_store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            record_store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_product_expected_config_identity()),
                authz_policy=_product_expected_config_policy(),
                record_store_factory=lambda: record_store,
            )
            payload = {**_product_expected_config_payload(), "mode": "apply"}

            first_response = await _post_product_expected_config(
                app,
                payload,
                idempotency_key="expected-config-replay",
            )
            second_response = await _post_product_expected_config(
                app,
                payload,
                idempotency_key="expected-config-replay",
            )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        self.assertTrue(second_response.json()["replayed"])
        self.assertEqual(
            second_response.json()["original_trace_id"], first_response.json()["trace_id"]
        )

    async def test_apply_product_expected_config_rejects_reused_idempotency_key(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            record_store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            record_store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_product_expected_config_identity()),
                authz_policy=_product_expected_config_policy(),
                record_store_factory=lambda: record_store,
            )

            await _post_product_expected_config(
                app,
                _product_expected_config_payload(),
                idempotency_key="expected-config-conflict",
            )
            response = await _post_product_expected_config(
                app,
                {
                    **_product_expected_config_payload(),
                    "managed_secret_bindings": [
                        {
                            "binding_key": "API_TOKEN",
                            "integration": "runtime_environment",
                            "context": "sellyouroutboard-prod",
                            "instance": "prod",
                        }
                    ],
                },
                idempotency_key="expected-config-conflict",
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "idempotency_key_reused")

    async def test_apply_product_expected_config_rejects_dry_run_key_for_apply(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            record_store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            record_store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_product_expected_config_identity()),
                authz_policy=_product_expected_config_policy(),
                record_store_factory=lambda: record_store,
            )

            await _post_product_expected_config(
                app,
                _product_expected_config_payload(),
                idempotency_key="expected-config-cross-mode",
            )
            response = await _post_product_expected_config(
                app,
                {**_product_expected_config_payload(), "mode": "apply"},
                idempotency_key="expected-config-cross-mode",
            )
            stored_profile = record_store.read_product_profile_record("sellyouroutboard")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "idempotency_key_reused")
        self.assertEqual(stored_profile.expected_config.managed_secret_bindings, ())

    async def test_apply_product_expected_config_rejects_unauthorized_workflow(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_profile_write_policy(product="sellyouroutboard"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_product_expected_config(
            app,
            _product_expected_config_payload(),
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_apply_product_expected_config_reports_missing_profile(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            record_store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_product_expected_config_identity()),
                authz_policy=_product_expected_config_policy(),
                record_store_factory=lambda: record_store,
            )

            response = await _post_product_expected_config(
                app,
                _product_expected_config_payload(),
            )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")

    async def test_apply_product_expected_config_rejects_empty_change(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_product_expected_config_identity()),
            authz_policy=_product_expected_config_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_product_expected_config(
            app,
            {
                "schema_version": 1,
                "product": "sellyouroutboard",
                "mode": "dry-run",
                "reason": "Test empty request.",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")

    async def test_apply_product_expected_config_rejects_instance_without_context(
        self,
    ) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_product_expected_config_identity()),
            authz_policy=_product_expected_config_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_product_expected_config(
            app,
            {
                **_product_expected_config_payload(),
                "managed_secret_bindings": [
                    {
                        "binding_key": "SMTP_PASSWORD",
                        "integration": "runtime_environment",
                        "instance": "prod",
                    }
                ],
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")

    async def test_apply_product_health_monitoring_dry_run_preserves_profile(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_product_profile_record(_product_health_monitoring_profile())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_product_health_monitoring_identity()),
                authz_policy=_product_health_monitoring_policy(),
                record_store_factory=lambda: store,
            )

            response = await _post_product_health_monitoring(
                app,
                _product_health_monitoring_payload(),
            )
            stored_profile = store.read_product_profile_record("odoo-product")

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(
            payload["records"],
            {
                "product_profile": "odoo-product",
                "context": "cm",
                "instance": "testing",
                "health_check": "public-ingress",
            },
        )
        self.assertEqual(payload["result"]["operation"], "update")
        self.assertFalse(payload["result"]["current_require_runtime_identity"])
        self.assertTrue(payload["result"]["requested_require_runtime_identity"])
        self.assertRegex(payload["result"]["plan_sha256"], r"^[0-9a-f]{64}$")
        self.assertFalse(
            stored_profile.lanes[0].health_monitoring.checks[0].require_runtime_identity
        )

    async def test_apply_product_health_monitoring_plans_private_intent_from_registered_endpoint(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_product_profile_record(_product_health_monitoring_profile())
            store.write_private_health_endpoint_record(
                PrivateHealthEndpointRecord(
                    endpoint_key="cm-testing-runtime",
                    product="odoo-product",
                    context="cm",
                    instance="testing",
                    url="http://10.0.0.5:8069/launchplane/health",
                    status="active",
                    updated_at="2026-07-27T16:55:00Z",
                )
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_product_health_monitoring_identity()),
                authz_policy=_product_health_monitoring_policy(),
                record_store_factory=lambda: store,
            )

            response = await _post_product_health_monitoring(
                app,
                {
                    **_product_health_monitoring_payload(),
                    "check_name": "private-runtime",
                    "check_kind": "private_http",
                    "monitoring_intent": "private",
                    "private_endpoint_key": "cm-testing-runtime",
                },
            )
            stored_profile = store.read_product_profile_record("odoo-product")
            store.close()

        self.assertEqual(response.status_code, 202)
        result = response.json()["result"]
        self.assertEqual(result["requested_check_kind"], "private_http")
        self.assertEqual(result["requested_monitoring_intent"], "private")
        self.assertEqual(result["private_endpoint_key"], "cm-testing-runtime")
        self.assertEqual(result["resolved_url"], "")
        self.assertEqual(stored_profile.lanes[0].health_monitoring.monitoring_intent, "public")

    async def test_apply_product_health_monitoring_rejects_missing_private_endpoint(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_product_profile_record(_product_health_monitoring_profile())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_product_health_monitoring_identity()),
                authz_policy=_product_health_monitoring_policy(),
                record_store_factory=lambda: store,
            )

            response = await _post_product_health_monitoring(
                app,
                {
                    **_product_health_monitoring_payload(),
                    "check_name": "private-runtime",
                    "check_kind": "private_http",
                    "monitoring_intent": "private",
                    "private_endpoint_key": "missing-runtime",
                },
            )
            store.close()

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_health_monitoring_target")

    async def test_apply_product_health_monitoring_persists_and_replays_reviewed_plan(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            original_profile = _product_health_monitoring_profile()
            store.write_product_profile_record(original_profile)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_product_health_monitoring_identity()),
                authz_policy=_product_health_monitoring_policy(),
                record_store_factory=lambda: store,
            )

            dry_run_response = await _post_product_health_monitoring(
                app,
                _product_health_monitoring_payload(),
            )
            apply_payload = {
                **_product_health_monitoring_payload(),
                "mode": "apply",
                "reviewed_plan_sha256": dry_run_response.json()["result"]["plan_sha256"],
            }
            apply_response = await _post_product_health_monitoring(
                app,
                apply_payload,
                idempotency_key="product-health-monitoring-apply",
            )
            replay_response = await _post_product_health_monitoring(
                app,
                apply_payload,
                idempotency_key="product-health-monitoring-apply",
            )
            stored_profile = store.read_product_profile_record("odoo-product")

        self.assertEqual(apply_response.status_code, 202)
        self.assertTrue(apply_response.json()["result"]["applied"])
        self.assertEqual(replay_response.status_code, 202)
        self.assertTrue(replay_response.json()["replayed"])
        self.assertTrue(
            stored_profile.lanes[0].health_monitoring.checks[0].require_runtime_identity
        )
        self.assertEqual(stored_profile.source, "service:product-health-monitoring")
        self.assertEqual(stored_profile.repository, original_profile.repository)

    async def test_apply_product_health_monitoring_rejects_stale_plan(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            original_profile = _product_health_monitoring_profile()
            store.write_product_profile_record(original_profile)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_product_health_monitoring_identity()),
                authz_policy=_product_health_monitoring_policy(),
                record_store_factory=lambda: store,
            )

            dry_run_response = await _post_product_health_monitoring(
                app,
                _product_health_monitoring_payload(),
            )
            store.write_product_profile_record(
                original_profile.model_copy(
                    update={
                        "updated_at": "2026-07-22T02:00:00Z",
                        "source": "service:concurrent-update",
                    }
                )
            )
            apply_response = await _post_product_health_monitoring(
                app,
                {
                    **_product_health_monitoring_payload(),
                    "mode": "apply",
                    "reviewed_plan_sha256": dry_run_response.json()["result"]["plan_sha256"],
                },
                idempotency_key="product-health-monitoring-stale",
            )

        self.assertEqual(apply_response.status_code, 409)
        self.assertEqual(apply_response.json()["error"]["code"], "stale")

    async def test_apply_product_health_monitoring_requires_exact_instance_authority(
        self,
    ) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_product_health_monitoring_identity()),
            authz_policy=_product_health_monitoring_policy(instance="testing"),
            record_store_factory=lambda: FilesystemRecordStore(state_dir=Path("state")),
        )

        response = await _post_product_health_monitoring(
            app,
            {
                **_product_health_monitoring_payload(),
                "instance": "prod",
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_apply_product_health_monitoring_rejects_schema_v1_authority(self) -> None:
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_actions": [
                    {
                        "repository": "every/verireel",
                        "workflow_refs": [
                            "every/verireel/.github/workflows/"
                            "product-health-monitoring.yml@refs/heads/main"
                        ],
                        "job_workflow_refs": [
                            "cbusillo/launchplane/.github/workflows/"
                            "reusable-product-health-monitoring.yml@"
                            "88584ae2800bceabc9d448eba7defddc5da75ec1"
                        ],
                        "event_names": ["workflow_dispatch"],
                        "products": ["odoo-product"],
                        "contexts": ["cm"],
                        "actions": ["product_profile.health_monitoring.plan"],
                    }
                ]
            }
        )
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_product_health_monitoring_identity()),
            authz_policy=policy,
            record_store_factory=lambda: FilesystemRecordStore(state_dir=Path("state")),
        )

        response = await _post_product_health_monitoring(
            app,
            _product_health_monitoring_payload(),
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_apply_product_health_monitoring_requires_pinned_worker_identity(
        self,
    ) -> None:
        for job_workflow_ref in (
            "",
            "cbusillo/launchplane/.github/workflows/"
            "reusable-product-health-monitoring.yml@" + "b" * 40,
        ):
            with self.subTest(job_workflow_ref=job_workflow_ref):
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(
                        _product_health_monitoring_identity(
                            job_workflow_ref=job_workflow_ref,
                        )
                    ),
                    authz_policy=_product_health_monitoring_policy(),
                    record_store_factory=lambda: FilesystemRecordStore(state_dir=Path("state")),
                )

                response = await _post_product_health_monitoring(
                    app,
                    _product_health_monitoring_payload(),
                )

                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_apply_product_health_monitoring_rejects_non_public_check(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            profile_payload = _product_health_monitoring_profile().model_dump(mode="json")
            profile_payload["lanes"][0]["health_monitoring"]["monitoring_intent"] = "prelaunch"
            profile_payload["lanes"][0]["health_monitoring"]["checks"] = [
                {
                    "name": "provider-health",
                    "kind": "provider",
                    "enabled": True,
                    "provider": "example",
                    "provider_check": "ready",
                }
            ]
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(profile_payload)
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_product_health_monitoring_identity()),
                authz_policy=_product_health_monitoring_policy(),
                record_store_factory=lambda: store,
            )

            response = await _post_product_health_monitoring(
                app,
                {
                    **_product_health_monitoring_payload(),
                    "check_name": "provider-health",
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["error"]["code"],
            "unsupported_health_check_kind",
        )

    async def test_apply_product_health_monitoring_requires_idempotency_key(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_product_health_monitoring_identity()),
            authz_policy=_product_health_monitoring_policy(),
            record_store_factory=lambda: FilesystemRecordStore(state_dir=Path("state")),
        )

        response = await _post_product_health_monitoring(
            app,
            {
                **_product_health_monitoring_payload(),
                "mode": "apply",
                "reviewed_plan_sha256": "a" * 64,
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "idempotency_key_required")

    async def test_apply_product_health_monitoring_requires_database_storage(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name))
            store.write_product_profile_record(_product_health_monitoring_profile())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_product_health_monitoring_identity()),
                authz_policy=_product_health_monitoring_policy(),
                record_store_factory=lambda: store,
            )

            response = await _post_product_health_monitoring(
                app,
                _product_health_monitoring_payload(),
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "database_required")

    async def test_openapi_includes_product_health_monitoring_contract(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_product_health_monitoring_identity()),
            authz_policy=_product_health_monitoring_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        route = response.json()["paths"]["/v1/product-profiles/health-monitoring/apply"]["post"]
        self.assertEqual(route["operationId"], "apply_product_health_monitoring")

    async def test_apply_product_preview_tls_dry_run_preserves_profile(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_product_profile_record(_product_preview_tls_profile())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_product_preview_tls_identity()),
                authz_policy=_product_preview_tls_policy(),
                record_store_factory=lambda: store,
            )

            response = await _post_product_preview_tls(
                app,
                _product_preview_tls_payload(),
            )
            stored_profile = store.read_product_profile_record("odoo-product")

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["records"], {"product_profile": "odoo-product"})
        self.assertEqual(payload["result"]["current_value"], "none")
        self.assertEqual(payload["result"]["requested_value"], "letsencrypt")
        self.assertTrue(payload["result"]["changed"])
        self.assertRegex(payload["result"]["plan_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(stored_profile.preview.domain_certificate_type, "none")

    async def test_apply_product_preview_tls_dry_run_always_reads_fresh_profile(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            original_profile = _product_preview_tls_profile()
            store.write_product_profile_record(original_profile)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_product_preview_tls_identity()),
                authz_policy=_product_preview_tls_policy(),
                record_store_factory=lambda: store,
            )

            first_response = await _post_product_preview_tls(
                app,
                _product_preview_tls_payload(),
                idempotency_key="ignored-dry-run-key",
            )
            store.write_product_profile_record(
                original_profile.model_copy(
                    update={
                        "updated_at": "2026-07-12T02:00:00Z",
                        "source": "service:concurrent-update",
                    }
                )
            )
            second_response = await _post_product_preview_tls(
                app,
                _product_preview_tls_payload(),
                idempotency_key="ignored-dry-run-key",
            )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        self.assertNotEqual(
            first_response.json()["result"]["plan_sha256"],
            second_response.json()["result"]["plan_sha256"],
        )
        self.assertNotIn("replayed", second_response.json())

    async def test_apply_product_preview_tls_persists_reviewed_plan(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            original_profile = _product_preview_tls_profile()
            store.write_product_profile_record(original_profile)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_product_preview_tls_identity()),
                authz_policy=_product_preview_tls_policy(),
                record_store_factory=lambda: store,
            )

            dry_run_response = await _post_product_preview_tls(
                app,
                _product_preview_tls_payload(),
            )
            plan_sha256 = dry_run_response.json()["result"]["plan_sha256"]
            apply_response = await _post_product_preview_tls(
                app,
                {
                    **_product_preview_tls_payload(),
                    "mode": "apply",
                    "reviewed_plan_sha256": plan_sha256,
                },
                idempotency_key="product-preview-tls-apply",
            )
            stored_profile = store.read_product_profile_record("odoo-product")

        self.assertEqual(apply_response.status_code, 202)
        self.assertEqual(stored_profile.preview.domain_certificate_type, "letsencrypt")
        self.assertEqual(stored_profile.source, "service:product-preview-tls")
        self.assertEqual(stored_profile.repository, original_profile.repository)
        self.assertEqual(stored_profile.lanes, original_profile.lanes)

    async def test_apply_product_preview_tls_no_change_preserves_audit_fields(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            original_profile = _product_preview_tls_profile()
            original_profile = original_profile.model_copy(
                update={
                    "preview": original_profile.preview.model_copy(
                        update={"domain_certificate_type": "letsencrypt"}
                    )
                }
            )
            store.write_product_profile_record(original_profile)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_product_preview_tls_identity()),
                authz_policy=_product_preview_tls_policy(),
                record_store_factory=lambda: store,
            )

            dry_run_response = await _post_product_preview_tls(
                app,
                _product_preview_tls_payload(),
            )
            plan_sha256 = dry_run_response.json()["result"]["plan_sha256"]
            apply_payload = {
                **_product_preview_tls_payload(),
                "mode": "apply",
                "reviewed_plan_sha256": plan_sha256,
            }
            apply_response = await _post_product_preview_tls(
                app,
                apply_payload,
                idempotency_key="product-preview-tls-no-change-apply",
            )
            replay_response = await _post_product_preview_tls(
                app,
                apply_payload,
                idempotency_key="product-preview-tls-no-change-apply",
            )
            stored_profile = store.read_product_profile_record("odoo-product")
            stored_reservation = store.read_idempotency_record(
                scope=idempotency_scope(_product_preview_tls_identity()),
                route_path="/v1/product-profiles/preview-tls/apply",
                idempotency_key="product-preview-tls-no-change-apply",
            )

        self.assertEqual(apply_response.status_code, 202)
        self.assertFalse(apply_response.json()["result"]["changed"])
        self.assertEqual(replay_response.status_code, 202)
        self.assertTrue(replay_response.json()["replayed"])
        self.assertEqual(stored_profile.updated_at, original_profile.updated_at)
        self.assertEqual(stored_profile.source, original_profile.source)
        self.assertIsNotNone(stored_reservation)
        assert stored_reservation is not None
        self.assertEqual(stored_reservation.state, "completed")
        self.assertEqual(stored_reservation.attempt, 1)
        self.assertEqual(
            stored_reservation.response_trace_id,
            apply_response.json()["trace_id"],
        )

    async def test_apply_product_preview_tls_reports_running_reservation(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_product_profile_record(_product_preview_tls_profile())
            identity = _product_preview_tls_identity()
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(identity),
                authz_policy=_product_preview_tls_policy(),
                record_store_factory=lambda: store,
            )
            dry_run_response = await _post_product_preview_tls(
                app,
                _product_preview_tls_payload(),
            )
            apply_payload = {
                **_product_preview_tls_payload(),
                "mode": "apply",
                "reviewed_plan_sha256": dry_run_response.json()["result"]["plan_sha256"],
            }
            route_path = "/v1/product-profiles/preview-tls/apply"
            store.reserve_mutation(
                scope=idempotency_scope(identity),
                route_path=route_path,
                idempotency_key="product-preview-tls-running",
                request_fingerprint=idempotency_request_fingerprint(
                    route_path=route_path,
                    payload=apply_payload,
                ),
                lease_owner="worker-running",
            )

            response = await _post_product_preview_tls(
                app,
                apply_payload,
                idempotency_key="product-preview-tls-running",
            )
            store.close()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "mutation_in_progress")

    async def test_apply_product_preview_tls_releases_expired_db_only_reservation(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_product_profile_record(_product_preview_tls_profile())
            identity = _product_preview_tls_identity()
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(identity),
                authz_policy=_product_preview_tls_policy(),
                record_store_factory=lambda: store,
            )
            dry_run_response = await _post_product_preview_tls(
                app,
                _product_preview_tls_payload(),
            )
            apply_payload = {
                **_product_preview_tls_payload(),
                "mode": "apply",
                "reviewed_plan_sha256": dry_run_response.json()["result"]["plan_sha256"],
            }
            route_path = "/v1/product-profiles/preview-tls/apply"
            idempotency_key = "product-preview-tls-expired"
            clock = {"now": "2026-07-12T01:00:00Z"}
            with patch.object(
                store,
                "_database_mutation_timestamp",
                side_effect=lambda _session: clock["now"],
            ):
                store.reserve_mutation(
                    scope=idempotency_scope(identity),
                    route_path=route_path,
                    idempotency_key=idempotency_key,
                    request_fingerprint=idempotency_request_fingerprint(
                        route_path=route_path,
                        payload=apply_payload,
                    ),
                    lease_owner="orphaned-worker",
                    lease_seconds=60,
                )
                clock["now"] = "2026-07-12T01:02:00Z"
                response = await _post_product_preview_tls(
                    app,
                    apply_payload,
                    idempotency_key=idempotency_key,
                )
            stored_record = store.read_idempotency_record(
                scope=idempotency_scope(identity),
                route_path=route_path,
                idempotency_key=idempotency_key,
            )
            store.close()

        self.assertEqual(response.status_code, 202)
        self.assertIsNotNone(stored_record)
        assert stored_record is not None
        self.assertEqual(stored_record.state, "completed")
        self.assertEqual(stored_record.attempt, 1)
        self.assertEqual(stored_record.created_at, "2026-07-12T01:02:00Z")
        self.assertEqual(stored_record.recorded_at, "2026-07-12T01:02:00Z")

    async def test_apply_product_preview_tls_reports_reconcile_required_reservation(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_product_profile_record(_product_preview_tls_profile())
            identity = _product_preview_tls_identity()
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(identity),
                authz_policy=_product_preview_tls_policy(),
                record_store_factory=lambda: store,
            )
            dry_run_response = await _post_product_preview_tls(
                app,
                _product_preview_tls_payload(),
            )
            apply_payload = {
                **_product_preview_tls_payload(),
                "mode": "apply",
                "reviewed_plan_sha256": dry_run_response.json()["result"]["plan_sha256"],
            }
            route_path = "/v1/product-profiles/preview-tls/apply"
            acquired = store.reserve_mutation(
                scope=idempotency_scope(identity),
                route_path=route_path,
                idempotency_key="product-preview-tls-reconcile",
                request_fingerprint=idempotency_request_fingerprint(
                    route_path=route_path,
                    payload=apply_payload,
                ),
                lease_owner="worker-reconcile",
            )
            bound = store.bind_mutation_reconciliation_key(
                reservation=acquired.record,
                reconciliation_key="provider-operation-reconcile",
            )
            assert bound.record is not None
            store.mark_mutation_reconcile_required(
                reservation=bound.record,
                reconciliation_key="provider-operation-reconcile",
            )

            response = await _post_product_preview_tls(
                app,
                apply_payload,
                idempotency_key="product-preview-tls-reconcile",
            )
            store.close()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["error"]["code"],
            "mutation_reconciliation_required",
        )

    async def test_apply_product_preview_tls_serializes_concurrent_noop_retries(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            original_profile = _product_preview_tls_profile()
            original_profile = original_profile.model_copy(
                update={
                    "preview": original_profile.preview.model_copy(
                        update={"domain_certificate_type": "letsencrypt"}
                    )
                }
            )
            store.write_product_profile_record(original_profile)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_product_preview_tls_identity()),
                authz_policy=_product_preview_tls_policy(),
                record_store_factory=lambda: store,
            )
            dry_run_response = await _post_product_preview_tls(
                app,
                _product_preview_tls_payload(),
            )
            apply_payload = {
                **_product_preview_tls_payload(),
                "mode": "apply",
                "reviewed_plan_sha256": dry_run_response.json()["result"]["plan_sha256"],
            }

            def apply_request() -> Any:
                return asyncio.run(
                    _post_product_preview_tls(
                        app,
                        apply_payload,
                        idempotency_key="product-preview-tls-concurrent-noop",
                    )
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                responses = tuple(executor.map(lambda _: apply_request(), range(2)))

        self.assertEqual([response.status_code for response in responses], [202, 202])
        self.assertEqual(
            sorted(bool(response.json().get("replayed")) for response in responses),
            [False, True],
        )

    async def test_apply_product_preview_tls_replays_idempotent_apply(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_product_profile_record(_product_preview_tls_profile())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_product_preview_tls_identity()),
                authz_policy=_product_preview_tls_policy(),
                record_store_factory=lambda: store,
            )

            dry_run_response = await _post_product_preview_tls(
                app,
                _product_preview_tls_payload(),
            )
            apply_payload = {
                **_product_preview_tls_payload(),
                "mode": "apply",
                "reviewed_plan_sha256": dry_run_response.json()["result"]["plan_sha256"],
            }
            first_response = await _post_product_preview_tls(
                app,
                apply_payload,
                idempotency_key="product-preview-tls-replay-apply",
            )
            second_response = await _post_product_preview_tls(
                app,
                apply_payload,
                idempotency_key="product-preview-tls-replay-apply",
            )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        self.assertTrue(second_response.json()["replayed"])
        self.assertEqual(
            second_response.json()["original_trace_id"],
            first_response.json()["trace_id"],
        )

    async def test_apply_product_preview_tls_replays_after_initial_race_miss(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_product_profile_record(_product_preview_tls_profile())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_product_preview_tls_identity()),
                authz_policy=_product_preview_tls_policy(),
                record_store_factory=lambda: store,
            )
            dry_run_response = await _post_product_preview_tls(
                app,
                _product_preview_tls_payload(),
            )
            apply_payload = {
                **_product_preview_tls_payload(),
                "mode": "apply",
                "reviewed_plan_sha256": dry_run_response.json()["result"]["plan_sha256"],
            }
            first_response = await _post_product_preview_tls(
                app,
                apply_payload,
                idempotency_key="product-preview-tls-race-apply",
            )
            read_idempotency_record = store.read_idempotency_record
            read_count = 0

            def miss_first_idempotency_read(**kwargs: str) -> object:
                nonlocal read_count
                read_count += 1
                if read_count == 1:
                    return None
                return read_idempotency_record(**kwargs)

            with patch.object(
                store,
                "read_idempotency_record",
                side_effect=miss_first_idempotency_read,
            ):
                second_response = await _post_product_preview_tls(
                    app,
                    apply_payload,
                    idempotency_key="product-preview-tls-race-apply",
                )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        self.assertTrue(second_response.json()["replayed"])
        self.assertEqual(
            second_response.json()["original_trace_id"],
            first_response.json()["trace_id"],
        )

    async def test_apply_product_preview_tls_rejects_stale_plan(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            original_profile = _product_preview_tls_profile()
            store.write_product_profile_record(original_profile)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_product_preview_tls_identity()),
                authz_policy=_product_preview_tls_policy(),
                record_store_factory=lambda: store,
            )

            dry_run_response = await _post_product_preview_tls(
                app,
                _product_preview_tls_payload(),
            )
            plan_sha256 = dry_run_response.json()["result"]["plan_sha256"]
            store.write_product_profile_record(
                original_profile.model_copy(
                    update={
                        "updated_at": "2026-07-12T02:00:00Z",
                        "source": "concurrent-profile-update",
                    }
                )
            )
            apply_response = await _post_product_preview_tls(
                app,
                {
                    **_product_preview_tls_payload(),
                    "mode": "apply",
                    "reviewed_plan_sha256": plan_sha256,
                },
                idempotency_key="product-preview-tls-stale-apply",
            )
            stored_profile = store.read_product_profile_record("odoo-product")

        self.assertEqual(apply_response.status_code, 409)
        self.assertEqual(apply_response.json()["error"]["code"], "stale")
        self.assertEqual(stored_profile.preview.domain_certificate_type, "none")

    async def test_apply_product_preview_tls_stale_plan_releases_expired_reservation(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            original_profile = _product_preview_tls_profile()
            store.write_product_profile_record(original_profile)
            identity = _product_preview_tls_identity()
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(identity),
                authz_policy=_product_preview_tls_policy(),
                record_store_factory=lambda: store,
            )
            dry_run_response = await _post_product_preview_tls(
                app,
                _product_preview_tls_payload(),
            )
            route_path = "/v1/product-profiles/preview-tls/apply"
            idempotency_key = "product-preview-tls-expired-stale"
            apply_payload = {
                **_product_preview_tls_payload(),
                "mode": "apply",
                "reviewed_plan_sha256": dry_run_response.json()["result"]["plan_sha256"],
            }
            clock = {"now": "2026-07-12T01:00:00Z"}
            with patch.object(
                store,
                "_database_mutation_timestamp",
                side_effect=lambda _session: clock["now"],
            ):
                store.reserve_mutation(
                    scope=idempotency_scope(identity),
                    route_path=route_path,
                    idempotency_key=idempotency_key,
                    request_fingerprint=idempotency_request_fingerprint(
                        route_path=route_path,
                        payload=apply_payload,
                    ),
                    lease_owner="orphaned-worker",
                    lease_seconds=60,
                )
                store.write_product_profile_record(
                    original_profile.model_copy(
                        update={
                            "updated_at": "2026-07-12T02:00:00Z",
                            "source": "concurrent-profile-update",
                        }
                    )
                )
                clock["now"] = "2026-07-12T01:02:00Z"
                response = await _post_product_preview_tls(
                    app,
                    apply_payload,
                    idempotency_key=idempotency_key,
                )
            stored_record = store.read_idempotency_record(
                scope=idempotency_scope(identity),
                route_path=route_path,
                idempotency_key=idempotency_key,
            )
            store.close()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "stale")
        self.assertIsNone(stored_record)

    async def test_apply_product_preview_tls_requires_idempotency_key(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_product_preview_tls_identity()),
            authz_policy=_product_preview_tls_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_product_preview_tls(
            app,
            {
                **_product_preview_tls_payload(),
                "mode": "apply",
                "reviewed_plan_sha256": "a" * 64,
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "idempotency_key_required")

    async def test_apply_product_preview_tls_rejects_unauthorized_workflow(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_product_preview_tls_identity()),
            authz_policy=LaunchplaneAuthzPolicy.model_validate({}),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_product_preview_tls(app, _product_preview_tls_payload())

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_apply_product_preview_tls_rejects_cross_product_target(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_product_preview_tls_identity()),
            authz_policy=_product_preview_tls_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_product_preview_tls(
            app,
            {
                **_product_preview_tls_payload(),
                "product": "different-product",
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_apply_product_preview_tls_rejects_concurrent_profile_change(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_product_profile_record(_product_preview_tls_profile())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_product_preview_tls_identity()),
                authz_policy=_product_preview_tls_policy(),
                record_store_factory=lambda: store,
            )
            dry_run_response = await _post_product_preview_tls(
                app,
                _product_preview_tls_payload(),
            )

            with patch.object(
                store,
                "compare_and_write_product_profile_record",
                return_value=ProductProfileCompareWriteResult(status="changed"),
            ):
                apply_response = await _post_product_preview_tls(
                    app,
                    {
                        **_product_preview_tls_payload(),
                        "mode": "apply",
                        "reviewed_plan_sha256": dry_run_response.json()["result"]["plan_sha256"],
                    },
                    idempotency_key="product-preview-tls-concurrent-apply",
                )
            stored_profile = store.read_product_profile_record("odoo-product")

        self.assertEqual(apply_response.status_code, 409)
        self.assertEqual(apply_response.json()["error"]["code"], "stale")
        self.assertEqual(stored_profile.preview.domain_certificate_type, "none")

    async def test_apply_product_preview_tls_reports_missing_profile(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_product_preview_tls_identity()),
                authz_policy=_product_preview_tls_policy(),
                record_store_factory=lambda: store,
            )

            response = await _post_product_preview_tls(app, _product_preview_tls_payload())

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")

    async def test_apply_product_preview_tls_rejects_non_odoo_profile(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            profile = _product_preview_tls_profile().model_copy(update={"driver_id": "generic-web"})
            store.write_product_profile_record(profile)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_product_preview_tls_identity()),
                authz_policy=_product_preview_tls_policy(),
                record_store_factory=lambda: store,
            )

            response = await _post_product_preview_tls(app, _product_preview_tls_payload())

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "unsupported_product_driver")
        self.assertEqual(
            response.json()["error"]["message"],
            "Product preview TLS policy requires the Odoo driver.",
        )

    async def test_apply_product_preview_tls_reports_profile_removed_during_apply(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_product_profile_record(_product_preview_tls_profile())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_product_preview_tls_identity()),
                authz_policy=_product_preview_tls_policy(),
                record_store_factory=lambda: store,
            )
            dry_run_response = await _post_product_preview_tls(
                app,
                _product_preview_tls_payload(),
            )

            with patch.object(
                store,
                "compare_and_write_product_profile_record",
                return_value=ProductProfileCompareWriteResult(status="missing"),
            ):
                apply_response = await _post_product_preview_tls(
                    app,
                    {
                        **_product_preview_tls_payload(),
                        "mode": "apply",
                        "reviewed_plan_sha256": dry_run_response.json()["result"]["plan_sha256"],
                    },
                    idempotency_key="product-preview-tls-missing-apply",
                )

        self.assertEqual(apply_response.status_code, 404)
        self.assertEqual(apply_response.json()["error"]["code"], "not_found")

    async def test_apply_product_preview_tls_requires_database_storage(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_product_preview_tls_identity()),
            authz_policy=_product_preview_tls_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_product_preview_tls(app, _product_preview_tls_payload())

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "database_required")

    async def test_openapi_includes_product_preview_tls_contract(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_product_preview_tls_identity()),
            authz_policy=_product_preview_tls_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        route = response.json()["paths"]["/v1/product-profiles/preview-tls/apply"]["post"]
        self.assertEqual(route["operationId"], "apply_product_preview_tls")
        self.assertEqual(
            route["requestBody"]["content"]["application/json"]["schema"]["title"],
            "ProductPreviewTlsApplyRequest",
        )
        self.assertEqual(
            route["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/AcceptedEvidenceResponse",
        )
        for status_code in ("400", "401", "403", "404", "409", "503"):
            self.assertIn(status_code, route["responses"])

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

    async def test_protected_artifacts_accepts_human_session_identity(self) -> None:
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
                authz_policy=_github_human_artifact_protection_policy(
                    products=("verireel",),
                    contexts=("*",),
                ),
                record_store_factory=lambda: record_store,
                human_session_manager=session_manager,
            )

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
