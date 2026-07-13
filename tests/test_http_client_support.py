from __future__ import annotations

import unittest
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse

from tests.support.http import lifespan_client, request


class LifespanHttpContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_runs_the_application_lifespan(self) -> None:
        events: list[str] = []

        @asynccontextmanager
        async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
            events.append("startup")
            try:
                yield
            finally:
                events.append("shutdown")

        app = FastAPI(lifespan=lifespan)

        @app.get("/contract")
        async def contract() -> dict[str, str]:
            return {"status": "ok"}

        response = await request(app, "GET", "/contract")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertEqual(events, ["startup", "shutdown"])

    async def test_client_propagates_lifespan_state(self) -> None:
        @asynccontextmanager
        async def lifespan(_app: FastAPI) -> AsyncIterator[dict[str, str]]:
            yield {"contract_state": "ready"}

        app = FastAPI(lifespan=lifespan)

        @app.get("/state")
        async def state(request: Request) -> dict[str, str]:
            return {"state": request.state.contract_state}

        async with lifespan_client(app) as client:
            response = await client.get("/state")

        self.assertEqual(response.json(), {"state": "ready"})


class LifespanHttpClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_preserves_cookies_without_following_redirects(self) -> None:
        events: list[str] = []

        @asynccontextmanager
        async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
            events.append("startup")
            try:
                yield
            finally:
                events.append("shutdown")

        app = FastAPI(lifespan=lifespan)

        @app.get("/sign-in")
        async def sign_in() -> RedirectResponse:
            response = RedirectResponse("/profile", status_code=303)
            response.set_cookie("launchplane_session", "test-session", secure=True)
            return response

        @app.get("/profile")
        async def profile(request: Request) -> dict[str, str]:
            return {"session": request.cookies.get("launchplane_session", "")}

        async with lifespan_client(app) as client:
            sign_in_response = await client.get("/sign-in")
            profile_response = await client.get("/profile")

        self.assertEqual(sign_in_response.status_code, 303)
        self.assertEqual(sign_in_response.headers["location"], "/profile")
        self.assertEqual(profile_response.json(), {"session": "test-session"})
        self.assertEqual(events, ["startup", "shutdown"])
