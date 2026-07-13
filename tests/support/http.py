from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient, Response


@asynccontextmanager
async def lifespan_client(
    app: FastAPI,
    *,
    raise_server_exceptions: bool = True,
    base_url: str = "https://testserver",
) -> AsyncIterator[AsyncClient]:
    async with LifespanManager(app) as manager:
        transport = ASGITransport(
            app=manager.app,
            raise_app_exceptions=raise_server_exceptions,
        )
        async with AsyncClient(
            transport=transport,
            base_url=base_url,
            follow_redirects=False,
        ) as client:
            yield client


async def get(
    target: FastAPI | AsyncClient,
    path: str,
    *,
    headers: Mapping[str, str] | None = None,
) -> Response:
    return await request(target, "GET", path, headers=headers)


async def request(
    target: FastAPI | AsyncClient,
    method: str,
    path: str,
    *,
    headers: Mapping[str, str] | None = None,
    payload: Mapping[str, object] | None = None,
    raw_body: bytes | None = None,
    capture_server_error_response: bool = False,
) -> Response:
    request_headers = dict(headers or {})
    request_kwargs: dict[str, Any] = {}
    if raw_body is not None:
        request_kwargs["content"] = raw_body
    elif payload is not None:
        request_headers.setdefault("Content-Type", "application/json")
        request_kwargs["content"] = json.dumps(payload).encode("utf-8")
    if request_headers:
        request_kwargs["headers"] = request_headers

    if isinstance(target, AsyncClient):
        if capture_server_error_response:
            raise ValueError(
                "capture_server_error_response must be configured when creating the client"
            )
        return await target.request(method, path, **request_kwargs)
    async with lifespan_client(
        target,
        raise_server_exceptions=not capture_server_error_response,
    ) as client:
        return await client.request(method, path, **request_kwargs)
