import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import FastAPI
from fastapi.routing import APIRoute
from httpx2 import Response

from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.http_app import _AUTHZ_POLICY_MANAGED_RECONCILE_ROUTE
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.service_human_auth import (
    HumanSessionManager,
    InMemoryHumanSessionStore,
    LaunchplaneHumanSession,
)
from tests.http_app_test_support import (
    _asgi_get,
    _asgi_request,
    _browser_mutation_headers,
    _github_human_identity,
    _github_human_product_config_policy,
    _github_oauth_config,
    _product_profile_write_policy,
    _RejectingVerifier,
)
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.auth import StubVerifier, identity
from tests.support.profiles import product_profile_payload
from tests.support.stores import sqlite_database_url

_PRODUCT = "browser-fixture"
_MUTATION_ROUTE = f"/v1/product-profiles/{_PRODUCT}/owner"


async def _clear_product_client(
    app: FastAPI,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> Response:
    request_headers = {**(headers or {}), "Idempotency-Key": "clear-browser-fixture-client"}
    if authorization:
        request_headers["Authorization"] = authorization
    return await _asgi_request(
        app,
        "POST",
        _MUTATION_ROUTE,
        headers=request_headers,
        payload={"mode": "apply", "clear": True, "reason": "Clear synthetic fixture Client."},
    )


class FastApiBrowserMutationBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def _store(self) -> PostgresRecordStore:
        temporary_directory = TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        store = PostgresRecordStore(
            database_url=sqlite_database_url(Path(temporary_directory.name) / "launchplane.sqlite3")
        )
        self.addCleanup(store.close)
        store.ensure_schema()
        profile = product_profile_payload(_PRODUCT)
        profile["owner"] = {"github_login": "synthetic-client", "github_id": "4242"}
        store.write_product_profile_record(LaunchplaneProductProfileRecord.model_validate(profile))
        return store

    def test_cookie_mutation_routes_do_not_use_plain_read_identity(self) -> None:
        store = self._store()
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_github_human_product_config_policy(
                action="product_profile.write", product=_PRODUCT, context="launchplane"
            ),
            record_store_factory=lambda: store,
        )
        unguarded = set()
        for route in app.routes:
            if not isinstance(route, APIRoute) or "POST" not in (route.methods or ()):
                continue
            dependency_names = {
                dependency.call.__name__
                for dependency in route.dependant.dependencies
                if dependency.call is not None
            }
            if "read_identity" in dependency_names:
                unguarded.add(route.path)
        # This mode-aware handler consumes browser mutation authority only in
        # its apply arm; all other cookie-capable mutations use guarded dependencies.
        self.assertEqual(unguarded, {_AUTHZ_POLICY_MANAGED_RECONCILE_ROUTE})

    def _human_app(self) -> tuple[FastAPI, HumanSessionManager, LaunchplaneHumanSession]:
        store = self._store()
        session_manager = HumanSessionManager(
            config=_github_oauth_config(),
            session_store=InMemoryHumanSessionStore(),
        )
        human_session = session_manager.issue(_github_human_identity())
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_github_human_product_config_policy(
                action="product_profile.write", product=_PRODUCT, context="launchplane"
            ),
            record_store_factory=lambda: store,
            human_session_manager=session_manager,
        )
        return app, session_manager, human_session

    async def test_session_read_returns_no_store_single_use_csrf_token(self) -> None:
        app, session_manager, human_session = self._human_app()

        response = await _asgi_get(
            app,
            "/v1/auth/session",
            headers={"Cookie": session_manager.session_cookie_header(human_session)},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json()["csrf_token"], session_manager.csrf_token(human_session))

    async def test_human_mutation_rejects_missing_or_cross_site_origin(self) -> None:
        for origin in (None, "https://attacker.example"):
            with self.subTest(origin=origin):
                app, session_manager, human_session = self._human_app()
                headers = _browser_mutation_headers(session_manager, human_session)
                if origin is None:
                    headers.pop("Origin")
                else:
                    headers["Origin"] = origin

                response = await _clear_product_client(
                    app,
                    authorization="",
                    headers=headers,
                )

                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["error"]["code"], "browser_mutation_denied")

    async def test_human_mutation_rejects_missing_or_cross_site_fetch_metadata(self) -> None:
        for header_name, header_value in (
            ("Sec-Fetch-Site", None),
            ("Sec-Fetch-Site", "cross-site"),
            ("Sec-Fetch-Mode", "navigate"),
            ("Sec-Fetch-Dest", "document"),
        ):
            with self.subTest(header_name=header_name, header_value=header_value):
                app, session_manager, human_session = self._human_app()
                headers = _browser_mutation_headers(session_manager, human_session)
                if header_value is None:
                    headers.pop(header_name)
                else:
                    headers[header_name] = header_value

                response = await _clear_product_client(
                    app,
                    authorization="",
                    headers=headers,
                )

                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["error"]["code"], "browser_mutation_denied")

    async def test_human_mutation_rejects_stale_and_replayed_csrf_tokens(self) -> None:
        app, session_manager, human_session = self._human_app()
        stale_session = session_manager.issue(_github_human_identity())
        stale_headers = _browser_mutation_headers(session_manager, human_session)
        stale_headers["X-CSRF-Token"] = session_manager.csrf_token(stale_session)

        stale_response = await _clear_product_client(
            app,
            authorization="",
            headers=stale_headers,
        )
        valid_headers = _browser_mutation_headers(session_manager, human_session)
        accepted_response = await _clear_product_client(
            app,
            authorization="",
            headers=valid_headers,
        )
        replay_response = await _clear_product_client(
            app,
            authorization="",
            headers=valid_headers,
        )

        self.assertEqual(stale_response.status_code, 403)
        self.assertEqual(accepted_response.status_code, 202)
        self.assertTrue(accepted_response.json()["result"]["changed"])
        self.assertEqual(replay_response.status_code, 403)
        self.assertEqual(replay_response.json()["error"]["code"], "browser_mutation_denied")

    async def test_github_actions_bearer_mutation_does_not_require_browser_headers(self) -> None:
        store = self._store()
        app = create_launchplane_fastapi_app(
            verifier=StubVerifier(identity()),
            authz_policy=_product_profile_write_policy(product=_PRODUCT),
            record_store_factory=lambda: store,
        )

        response = await _clear_product_client(app)

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json()["result"]["changed"])
        self.assertFalse(store.read_product_profile_record(_PRODUCT).owner.is_set)


if __name__ == "__main__":
    unittest.main()
