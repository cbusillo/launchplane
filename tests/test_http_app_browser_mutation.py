import unittest

from fastapi import FastAPI
from fastapi.routing import APIRoute

from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_human_auth import (
    HumanSessionManager,
    InMemoryHumanSessionStore,
    LaunchplaneHumanSession,
)
from tests.http_app_test_support import (
    _asgi_get,
    _browser_mutation_headers,
    _github_human_identity,
    _github_human_work_graph_rank_policy,
    _github_oauth_config,
    _MissingProductReadStore,
    _post_work_graph_rank,
    _RejectingVerifier,
    _work_graph_read_policy,
)
from tests.support.auth import _identity, _StubVerifier
from tests.support.work_graph import _work_graph_snapshot_payload


class FastApiBrowserMutationBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def test_cookie_capable_mutation_route_inventory_is_explicit(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_github_human_work_graph_rank_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )
        expected_routes = {
            "/auth/logout",
            "/v1/agent/write-intents/evaluate",
            "/v1/authz-policies/github-actions/grants",
            "/v1/authz-policies/github-actions/removals",
            "/v1/authz-policies/github-humans/grants",
            "/v1/authz-policies/local-admins/grants",
            "/v1/authz-policies/local-operators/grants",
            "/v1/authz-policies/terminal-agents/grants",
            "/v1/drivers/generic-web/prod-promotion",
            "/v1/drivers/generic-web/prod-promotion-workflow",
            "/v1/merge-train/policies/import",
            "/v1/product-config/apply",
            "/v1/work-graph/rank",
        }
        browser_dependency_names = {
            "read_browser_mutation_identity",
            "read_browser_work_graph_rank_identity",
        }
        expected_bearer_only_routes = {"/v1/secrets/reencrypt"}
        actual_routes = {"/auth/logout"}
        actual_bearer_only_routes: set[str] = set()
        unprotected_cookie_routes: set[str] = set()
        for route in app.routes:
            if (
                not isinstance(route, APIRoute)
                or route.methods is None
                or "POST" not in route.methods
            ):
                continue
            dependency_names = {
                dependency.call.__name__
                for dependency in route.dependant.dependencies
                if dependency.call is not None
            }
            if dependency_names & browser_dependency_names:
                actual_routes.add(route.path)
            if "read_bearer_identity" in dependency_names:
                actual_bearer_only_routes.add(route.path)
            if dependency_names & {"read_identity", "read_work_graph_rank_identity"}:
                unprotected_cookie_routes.add(route.path)

        self.assertEqual(actual_routes, expected_routes)
        self.assertEqual(actual_bearer_only_routes, expected_bearer_only_routes)
        self.assertEqual(unprotected_cookie_routes, set())

    def _human_app(
        self,
    ) -> tuple[FastAPI, HumanSessionManager, LaunchplaneHumanSession]:
        session_manager = HumanSessionManager(
            config=_github_oauth_config(),
            session_store=InMemoryHumanSessionStore(),
        )
        human_session = session_manager.issue(_github_human_identity())
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_github_human_work_graph_rank_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
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

                response = await _post_work_graph_rank(
                    app,
                    payload={"snapshot": _work_graph_snapshot_payload(), "limit": 1},
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

                response = await _post_work_graph_rank(
                    app,
                    payload={"snapshot": _work_graph_snapshot_payload(), "limit": 1},
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

        stale_response = await _post_work_graph_rank(
            app,
            payload={"snapshot": _work_graph_snapshot_payload(), "limit": 1},
            authorization="",
            headers=stale_headers,
        )
        valid_headers = _browser_mutation_headers(session_manager, human_session)
        accepted_response = await _post_work_graph_rank(
            app,
            payload={"snapshot": _work_graph_snapshot_payload(), "limit": 1},
            authorization="",
            headers=valid_headers,
        )
        replay_response = await _post_work_graph_rank(
            app,
            payload={"snapshot": _work_graph_snapshot_payload(), "limit": 1},
            authorization="",
            headers=valid_headers,
        )

        self.assertEqual(stale_response.status_code, 403)
        self.assertEqual(accepted_response.status_code, 202)
        self.assertEqual(replay_response.status_code, 403)
        self.assertEqual(replay_response.json()["error"]["code"], "browser_mutation_denied")

    async def test_github_actions_bearer_mutation_does_not_require_browser_headers(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
