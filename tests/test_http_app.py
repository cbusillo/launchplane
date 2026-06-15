import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from jwt import InvalidTokenError

from control_plane import secrets as control_plane_secrets
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import GitHubActionsIdentity, LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from tests.test_service import _generic_site_profile_payload, _identity, _sqlite_database_url
from tests.test_service import _StubVerifier


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

            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_environment_read_policy(context="example-site"),
                database_url=database_url,
            )

            response = await _get_config_status(app)

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
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_environment_read_policy(context="launchplane"),
                database_url=database_url,
            )

            response = await _get_config_status(app)

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
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_product_environment_read_policy(context="example-site"),
                database_url=database_url,
            )

            response = await _get_config_status(app, environment="staging")

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

    async def test_openapi_includes_config_status_route(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_product_environment_read_policy(context="example-site"),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get("/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        route = openapi["paths"]["/v1/products/{product}/environments/{environment}/config-status"][
            "get"
        ]
        self.assertIn("ProductEnvironmentConfigStatusResponse", json.dumps(route))
        self.assertIn("LaunchplaneErrorResponse", json.dumps(route))


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


async def _get_config_status(
    app: FastAPI,
    *,
    product: str = "example-site",
    environment: str = "prod",
    authorization: str = "Bearer valid-token",
) -> Response:
    headers = {"Authorization": authorization} if authorization else {}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        return await client.get(
            f"/v1/products/{product}/environments/{environment}/config-status",
            headers=headers,
        )


class _MissingProductReadStore:
    pass


class _RejectingVerifier:
    def verify(self, token: str) -> GitHubActionsIdentity:
        raise InvalidTokenError("signature verification failed")
