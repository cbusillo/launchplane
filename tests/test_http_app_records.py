import json
import unittest
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
from control_plane.workflows.public_ingress_monitor import (
    PublicIngressMonitorResult,
    public_ingress_managed_secret_resolver,
)
from tests.http_app_test_support import (
    _asgi_get,
    _asgi_request,
    _backup_gate_evidence_payload,
    _backup_gate_write_identity,
    _backup_gate_write_policy,
    _deployment_evidence_payload,
    _deployment_read_record,
    _deployment_write_identity,
    _deployment_write_policy,
    _environment_inventory_read_record,
    _FailingOnceIdempotencyPreviewGenerationStore,
    _get_context_secret_statuses,
    _get_deployment_record,
    _get_environment_inventory,
    _get_instance_secret_statuses,
    _get_promotion_record,
    _get_recent_operations,
    _get_secret_status,
    _github_human_backup_gate_write_policy,
    _github_human_deployment_write_policy,
    _github_human_identity,
    _github_human_preview_destroyed_write_policy,
    _github_human_preview_generation_write_policy,
    _github_human_promotion_write_policy,
    _github_human_runner_host_hygiene_audit_write_policy,
    _github_human_runner_lane_registration_audit_write_policy,
    _github_oauth_config,
    _MissingProductReadStore,
    _post_backup_gate_evidence,
    _post_deployment_evidence,
    _post_preview_destroyed_evidence,
    _post_preview_generation_evidence,
    _post_promotion_evidence,
    _post_public_ingress_monitor,
    _post_runner_host_hygiene_audit_evidence,
    _post_runner_lane_registration_audit_evidence,
    _preview_destroyed_evidence_payload,
    _preview_destroyed_write_identity,
    _preview_destroyed_write_policy,
    _preview_generation_evidence_payload,
    _preview_generation_write_identity,
    _preview_generation_write_policy,
    _preview_record_for_destroy,
    _promotion_evidence_payload,
    _promotion_evidence_store,
    _promotion_read_record,
    _promotion_write_identity,
    _promotion_write_policy,
    _public_ingress_incident,
    _public_ingress_monitor_identity,
    _public_ingress_monitor_policy,
    _PublicIngressMonitorIdempotencyReplayStore,
    _RecentOperationsProbeStore,
    _record_read_policy,
    _RejectingVerifier,
    _runner_host_hygiene_audit_payload,
    _runner_host_hygiene_audit_write_identity,
    _runner_host_hygiene_audit_write_policy,
    _runner_lane_registration_audit_payload,
    _runner_lane_registration_audit_write_identity,
    _runner_lane_registration_audit_write_policy,
    _SecretStatusProbeStore,
    _terminal_agent_backup_gate_write_policy,
    _terminal_agent_deployment_write_policy,
    _terminal_agent_preview_destroyed_write_policy,
    _terminal_agent_preview_generation_write_policy,
    _terminal_agent_promotion_write_policy,
    _terminal_agent_runner_host_hygiene_audit_write_policy,
    _terminal_agent_runner_lane_registration_audit_write_policy,
    _write_recent_operations_records,
    _write_secret_status_records,
)
from tests.test_service import (
    _identity,
    _sqlite_database_url,
    _StubVerifier,
    create_launchplane_service_app,
)


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
