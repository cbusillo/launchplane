import asyncio
import unittest
from unittest.mock import patch
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Query, Request
from fastapi.routing import APIRoute

from control_plane.http_route_preparation import PreparedAPIRoute
from tests.support.http import request


def _app(
    store: dict[str, str],
    *,
    allowed: bool = True,
    route_class: type[APIRoute] = PreparedAPIRoute,
    minimum: int = 1,
) -> FastAPI:
    app = FastAPI()
    app.router.route_class = route_class

    def current_store() -> dict[str, str]:
        return store

    def current_policy() -> bool:
        return allowed

    async def read(
        request: Request,
        value: Annotated[int, Query(ge=minimum)],
        records: Annotated[dict[str, str], Depends(current_store)],
        policy: Annotated[bool, Depends(current_policy)],
    ) -> dict[str, object]:
        return {
            "record": records["record"],
            "allowed": policy,
            "value": value,
            "state": getattr(request.app.state, "marker", None),
        }

    app.add_api_route("/example", read, methods=["GET"], tags=["example"])
    return app


class PreparedRouteIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_warm_route_serves_current_records_without_repeating_preparation(self) -> None:
        _app({"record": "warm-up"})
        with patch.object(APIRoute, "__init__", side_effect=AssertionError("route rebuilt")):
            app = _app({"record": "current"})
        self.assertEqual(
            (await request(app, "GET", "/example?value=2")).json()["record"], "current"
        )

    async def test_real_app_health_route_uses_warm_preparation(self) -> None:
        from control_plane.http_app import create_launchplane_fastapi_app
        from tests.http_app_test_support import _EmptyStore, _driver_read_policy
        from tests.support.auth import _StubVerifier, _identity

        def build() -> FastAPI:
            return create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_driver_read_policy(),
                record_store_factory=_EmptyStore,
            )

        build()
        prepared_paths: list[str] = []
        original = APIRoute.__init__

        def prepare(route: APIRoute, path: str, *args: Any, **kwargs: Any) -> None:
            prepared_paths.append(path)
            original(route, path, *args, **kwargs)

        with patch.object(APIRoute, "__init__", prepare):
            app = build()
        self.assertNotIn("/v1/health", prepared_paths)
        self.assertEqual((await request(app, "GET", "/v1/health")).status_code, 200)

    async def test_retained_apps_keep_separate_store_policy_state_and_overrides(self) -> None:
        first_store = {"record": "first"}
        first = _app(first_store)
        second = _app({"record": "second"}, allowed=False)
        first.state.marker = "first-state"
        first_route = next(route for route in first.routes if isinstance(route, APIRoute))
        store_dependency = first_route.dependant.dependencies[0].call
        assert store_dependency is not None
        first.dependency_overrides[store_dependency] = lambda: {"record": "override"}
        first_response, second_response = await asyncio.gather(
            request(first, "GET", "/example?value=2"),
            request(second, "GET", "/example?value=2"),
        )
        self.assertEqual(
            first_response.json(),
            {"record": "override", "allowed": True, "value": 2, "state": "first-state"},
        )
        self.assertEqual(
            second_response.json(),
            {"record": "second", "allowed": False, "value": 2, "state": None},
        )
        first.dependency_overrides.clear()
        first_store["record"] = "updated"
        self.assertEqual(
            (await request(first, "GET", "/example?value=2")).json()["record"], "updated"
        )

    async def test_validation_and_openapi_match_fresh_fastapi_routes(self) -> None:
        for minimum in (1, 4, 1):
            with self.subTest(minimum=minimum):
                baseline = _app({"record": "baseline"}, route_class=APIRoute, minimum=minimum)
                cached = _app({"record": "baseline"}, minimum=minimum)
                self.assertEqual(cached.openapi(), baseline.openapi())
                for value in ("0", "3", "4", "invalid"):
                    reference, observed = await asyncio.gather(
                        request(baseline, "GET", f"/example?value={value}"),
                        request(cached, "GET", f"/example?value={value}"),
                    )
                    self.assertEqual(observed.status_code, reference.status_code)
                    self.assertEqual(observed.json(), reference.json())

    async def test_route_metadata_mutation_does_not_reach_other_apps(self) -> None:
        first = _app({"record": "first"})
        first_route = next(route for route in first.routes if isinstance(route, APIRoute))
        first_route.tags.append("first-only")
        first_route.responses[418] = {"description": "first-only"}
        second = _app({"record": "second"})
        baseline = _app({"record": "second"}, route_class=APIRoute)
        self.assertEqual(second.openapi(), baseline.openapi())
        self.assertNotEqual(first.openapi(), second.openapi())

    async def test_callable_instance_dependencies_keep_their_own_behavior(self) -> None:
        class StoreReader:
            def __init__(self, value: str) -> None:
                self.value = value

            def __call__(self) -> str:
                return self.value

        for value in ("first", "second"):
            app = FastAPI()
            app.router.route_class = PreparedAPIRoute

            async def read(record: str = Depends(StoreReader(value))) -> dict[str, str]:
                return {"record": record}

            app.add_api_route("/example", read)
            self.assertEqual((await request(app, "GET", "/example")).json(), {"record": value})
