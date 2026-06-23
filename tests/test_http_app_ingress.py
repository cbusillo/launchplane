import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import (
    Any,
    cast,
)

from a2wsgi import WSGIMiddleware
from click import ClickException
from starlette.types import ASGIApp

from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.service_human_auth import (
    HumanSessionManager,
    InMemoryHumanSessionStore,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.workflows.npmplus_ingress import (
    NpmplusIngressApplyResult,
    NpmplusIngressOperation,
)
from tests.async_case import AsyncTestCase
from tests.http_app_test_support import (
    _asgi_get,
    _asgi_request,
    _get_edge_endpoint_record,
    _get_edge_endpoint_records,
    _get_ingress_canary_route_record,
    _get_ingress_canary_route_records,
    _get_ingress_route_audit_record,
    _get_ingress_route_audit_records,
    _get_private_health_endpoint_record,
    _get_private_health_endpoint_records,
    _github_human_identity,
    _github_human_ingress_route_policy,
    _github_oauth_config,
    _ingress_canary_route_apply_payload,
    _ingress_route_audit_read_policy,
    _ingress_route_audit_record,
    _MissingProductReadStore,
    _notification_policy_apply_policy,
    _private_health_endpoint_read_policy,
    _record_read_policy,
    _RejectingVerifier,
)
from tests.test_service import (
    _edge_endpoint_apply_payload,
    _edge_endpoint_record,
    _FakeIngressProvider,
    _FakeNpmplusIngressClient,
    _identity,
    _ingress_canary_route_record,
    _ingress_canary_route_record_apply_payload,
    _npmplus_ingress_route_payload,
    _npmplus_proxy_host,
    _private_health_endpoint_apply_payload,
    _private_health_endpoint_record,
    _StubVerifier,
    create_launchplane_service_app,
)


class FastApiEdgeEndpointReadTests(AsyncTestCase):
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


class FastApiPrivateHealthEndpointReadTests(AsyncTestCase):
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


class FastApiEndpointApplyTests(AsyncTestCase):
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


class FastApiIngressRouteApplyTests(AsyncTestCase):
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


class FastApiIngressCanaryRouteApplyTests(AsyncTestCase):
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


class FastApiIngressCanaryRouteReadTests(AsyncTestCase):
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


class FastApiIngressRouteAuditReadTests(AsyncTestCase):
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
