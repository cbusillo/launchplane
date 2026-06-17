import json
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from collections.abc import Callable, MutableMapping
from typing import Any, cast
from urllib.parse import urlencode
from unittest.mock import patch

from a2wsgi import WSGIMiddleware
from fastapi import FastAPI
from jwt import InvalidTokenError
from starlette.types import ASGIApp

from control_plane import secrets as control_plane_secrets
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.deployment_record import DeploymentRecord, ResolvedTargetEvidence
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
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
    LaunchplaneHumanSession,
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
        for status_code in ("400", "401", "403", "409", "503"):
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
        for status_code in ("400", "401", "403", "409", "503"):
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
        for status_code in ("400", "401", "403", "409", "503"):
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
        for status_code in ("400", "401", "403", "409", "503"):
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
        for status_code in ("400", "401", "403", "409", "503"):
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
        for status_code in ("400", "401", "403", "409", "503"):
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
    payload: dict[str, object] | None = None,
) -> _AsgiResponse:
    request_path, separator, raw_query_string = path.partition("?")
    request_headers_dict = dict(headers or {})
    body = b""
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        request_headers_dict.setdefault("Content-Type", "application/json")
    request_headers_dict.setdefault("Content-Length", str(len(body)))
    request_headers = [
        (key.lower().encode("ascii"), value.encode("latin-1"))
        for key, value in request_headers_dict.items()
    ]
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


class _MissingProductReadStore:
    pass


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
