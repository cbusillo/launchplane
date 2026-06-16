import json
import unittest
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from collections.abc import MutableMapping
from typing import Any, cast
from unittest.mock import patch

from a2wsgi import WSGIMiddleware
from fastapi import FastAPI
from jwt import InvalidTokenError
from starlette.types import ASGIApp

from control_plane import secrets as control_plane_secrets
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import GitHubActionsIdentity, LaunchplaneAuthzPolicy
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.test_service import create_launchplane_service_app
from tests.test_service import _generic_site_profile_payload, _identity, _sqlite_database_url
from tests.test_service import _StubVerifier


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


async def _asgi_get(
    app: FastAPI, path: str, *, headers: dict[str, str] | None = None
) -> _AsgiResponse:
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
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
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
