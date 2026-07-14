import asyncio
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

import uvicorn

from control_plane import service as control_plane_service
from control_plane import service_bootstrap
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.drivers import native_routes
from control_plane.http_app import ResolvedLaunchplaneAuthzPolicy
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.auth import _StubVerifier, _identity
from tests.support.http import get as http_get
from tests.support.http import request as http_request
from tests.support.profiles import _generic_site_profile_payload
from tests.support.stores import _sqlite_database_url


async def _http_get_for_service_test(app: Any, path: str) -> Any:
    return await http_get(app, path)


async def _http_request_for_service_test(
    app: Any,
    *,
    method: str,
    path: str,
    authorization: str = "",
    payload: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    return await http_request(
        app,
        method,
        path,
        headers=request_headers,
        payload=payload,
    )


class _ClosableStore:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class LaunchplaneServiceBootstrapTests(unittest.TestCase):
    def test_service_serve_runs_fastapi_app(self) -> None:
        fastapi_response: Any | None = None
        ui_response: Any | None = None
        denied_config_response: Any | None = None
        grant_response: Any | None = None
        authorized_config_response: Any | None = None

        def capture_uvicorn_run(app: Any, **_kwargs: object) -> None:
            nonlocal fastapi_response, ui_response, denied_config_response
            nonlocal grant_response, authorized_config_response
            fastapi_response = asyncio.run(_http_get_for_service_test(app, "/openapi.json"))
            ui_response = asyncio.run(_http_get_for_service_test(app, "/ui"))
            denied_config_response = asyncio.run(
                _http_request_for_service_test(
                    app,
                    method="GET",
                    path="/v1/products/example-site/environments/prod/config-status",
                    authorization="Bearer valid-token",
                )
            )
            grant_response = asyncio.run(
                _http_request_for_service_test(
                    app,
                    method="POST",
                    path="/v1/authz-policies/github-actions/grants",
                    authorization="Bearer valid-token",
                    payload={
                        "schema_version": 1,
                        "product": "launchplane",
                        "mode": "apply",
                        "reason": "Grant FastAPI config-status read for runtime policy refresh test.",
                        "related_issue": "cbusillo/launchplane#1323",
                        "grant": {
                            "repository": "cbusillo/launchplane",
                            "workflow_refs": [
                                "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["example-site"],
                            "contexts": ["example-site"],
                            "actions": ["product_environment.read"],
                            "source_label": "test:fastapi-runtime-policy-refresh",
                        },
                    },
                    headers={"Idempotency-Key": "authz-grant:fastapi-runtime-refresh"},
                )
            )
            authorized_config_response = asyncio.run(
                _http_request_for_service_test(
                    app,
                    method="GET",
                    path="/v1/products/example-site/environments/prod/config-status",
                    authorization="Bearer valid-token",
                )
            )

        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            ui_root = root / "control_plane" / "ui_static"
            ui_root.mkdir(parents=True)
            (ui_root / "index.html").write_text("<html>Launchplane UI</html>", encoding="utf-8")
            policy_file = root / "policy.toml"
            policy_file.write_text(
                "\n".join(
                    (
                        "schema_version = 1",
                        "",
                        "[[github_actions]]",
                        'repository = "cbusillo/launchplane"',
                        "workflow_refs = [",
                        '  "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main",',
                        "]",
                        'event_names = ["workflow_dispatch"]',
                        'products = ["launchplane"]',
                        'contexts = ["launchplane"]',
                        'actions = ["authz_policy_grant.write"]',
                        "",
                    )
                ),
                encoding="utf-8",
            )
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_generic_site_profile_payload())
            )
            store.close()

            with (
                patch("uvicorn.run", side_effect=capture_uvicorn_run) as run_uvicorn,
                patch("control_plane.service_bootstrap.control_plane_root", return_value=root),
                patch(
                    "control_plane.service_bootstrap.GitHubOidcVerifier",
                    return_value=_StubVerifier(
                        _identity(
                            repository="cbusillo/launchplane",
                            workflow_ref=(
                                "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                            ),
                            event_name="workflow_dispatch",
                        )
                    ),
                ),
            ):
                control_plane_service.serve_launchplane_service(
                    state_dir=root / "state",
                    policy_file=policy_file,
                    host="127.0.0.1",
                    port=8080,
                    audience="launchplane-service",
                    database_url=database_url,
                )

            run_uvicorn.assert_called_once()

        self.assertIsNotNone(fastapi_response)
        self.assertIsNotNone(ui_response)
        self.assertIsNotNone(denied_config_response)
        self.assertIsNotNone(grant_response)
        self.assertIsNotNone(authorized_config_response)
        assert fastapi_response is not None
        assert ui_response is not None
        assert denied_config_response is not None
        assert grant_response is not None
        assert authorized_config_response is not None
        self.assertEqual(fastapi_response.status_code, 200)
        self.assertIn(
            "/v1/products/{product}/environments/{environment}/config-status",
            fastapi_response.json()["paths"],
        )
        self.assertEqual(ui_response.status_code, 200)
        self.assertEqual(ui_response.headers["content-type"], "text/html")
        self.assertIn(b"Launchplane UI", ui_response.content)
        self.assertEqual(
            denied_config_response.status_code,
            403,
            msg=denied_config_response.content.decode("utf-8"),
        )
        self.assertEqual(grant_response.status_code, 202)
        self.assertEqual(grant_response.json()["result"]["changed"], True)
        self.assertEqual(authorized_config_response.status_code, 200)
        self.assertEqual(
            authorized_config_response.json()["config_status"]["product"], "example-site"
        )

    def test_descriptor_validation_fails_before_store_creation_or_policy_write(self) -> None:
        policy = LaunchplaneAuthzPolicy()
        with (
            patch.object(
                native_routes,
                "_validate_native_descriptor_driver_routes",
                side_effect=ValueError("descriptor metadata invalid"),
            ),
            patch.object(
                service_bootstrap, "load_authz_policy", return_value=policy
            ) as load_policy,
            patch.object(
                service_bootstrap,
                "GitHubOidcVerifier",
                return_value=_StubVerifier(_identity()),
            ),
            patch.object(service_bootstrap, "build_shared_record_store") as build_store,
            patch.object(service_bootstrap, "resolve_launchplane_authz_policy") as resolve_policy,
        ):
            with self.assertRaisesRegex(ValueError, "descriptor metadata invalid"):
                service_bootstrap.serve_launchplane_service(
                    state_dir=Path("unused-state"),
                    policy_file=Path("unused-policy.toml"),
                    host="127.0.0.1",
                    port=8080,
                    audience="launchplane-service",
                    database_url="postgresql+psycopg://unused",
                )

        load_policy.assert_called_once_with(Path("unused-policy.toml"))
        build_store.assert_not_called()
        resolve_policy.assert_not_called()

    def test_app_construction_failure_closes_store(self) -> None:
        store = _ClosableStore()
        policy = LaunchplaneAuthzPolicy()
        resolved_policy = ResolvedLaunchplaneAuthzPolicy(
            policy=policy,
            policy_sha256="test-policy-sha",
            source="test",
        )
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(
                native_routes,
                "_validate_native_descriptor_driver_routes",
            ),
            patch.object(service_bootstrap, "load_authz_policy", return_value=policy),
            patch.object(
                service_bootstrap,
                "GitHubOidcVerifier",
                return_value=_StubVerifier(_identity()),
            ),
            patch.object(service_bootstrap, "build_shared_record_store", return_value=store),
            patch.object(
                service_bootstrap,
                "resolve_launchplane_authz_policy",
                return_value=resolved_policy,
            ),
            patch.object(
                service_bootstrap,
                "create_launchplane_fastapi_app",
                side_effect=RuntimeError("app construction failed"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "app construction failed"):
                service_bootstrap.serve_launchplane_service(
                    state_dir=Path("unused-state"),
                    policy_file=Path("unused-policy.toml"),
                    host="127.0.0.1",
                    port=8080,
                    audience="launchplane-service",
                    database_url="postgresql+psycopg://unused",
                )

        self.assertTrue(store.closed)

    def test_route_validation_failure_closes_store(self) -> None:
        store = _ClosableStore()
        policy = LaunchplaneAuthzPolicy()
        resolved_policy = ResolvedLaunchplaneAuthzPolicy(
            policy=policy,
            policy_sha256="test-policy-sha",
            source="test",
        )
        app = object()
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(
                native_routes,
                "_validate_native_descriptor_driver_routes",
            ),
            patch.object(
                native_routes,
                "_validate_native_fastapi_driver_route_paths",
                side_effect=ValueError("route validation failed"),
            ),
            patch.object(service_bootstrap, "load_authz_policy", return_value=policy),
            patch.object(
                service_bootstrap,
                "GitHubOidcVerifier",
                return_value=_StubVerifier(_identity()),
            ),
            patch.object(service_bootstrap, "build_shared_record_store", return_value=store),
            patch.object(
                service_bootstrap,
                "resolve_launchplane_authz_policy",
                return_value=resolved_policy,
            ),
            patch.object(service_bootstrap, "create_launchplane_fastapi_app", return_value=app),
            patch.object(uvicorn, "run") as run_uvicorn,
        ):
            with self.assertRaisesRegex(ValueError, "route validation failed"):
                service_bootstrap.serve_launchplane_service(
                    state_dir=Path("unused-state"),
                    policy_file=Path("unused-policy.toml"),
                    host="127.0.0.1",
                    port=8080,
                    audience="launchplane-service",
                    database_url="postgresql+psycopg://unused",
                )

        self.assertTrue(store.closed)
        run_uvicorn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
