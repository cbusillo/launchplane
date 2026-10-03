"""Read-only schema fixture; request and mutation tests must build their own app."""

from copy import deepcopy
from functools import cache
from typing import Any

from fastapi import FastAPI
from httpx2 import Response

from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import LaunchplaneAuthzPolicy
from tests.support.auth import _identity, _StubVerifier
from tests.support.http import get


@cache
def _schema_app() -> FastAPI:
    # Routes and schemas do not depend on grants or records. Keep this app private
    # so only OpenAPI reads can reuse its cached schema within a test process.
    return create_launchplane_fastapi_app(
        verifier=_StubVerifier(_identity()),
        authz_policy=LaunchplaneAuthzPolicy(),
        record_store_factory=object,
    )


async def openapi_response() -> Response:
    """Read the real HTTP endpoint with a fresh lifespan and client every time."""
    return await get(_schema_app(), "/openapi.json")


def openapi_document() -> dict[str, Any]:
    """Give direct schema assertions their own mutable copy."""
    return deepcopy(_schema_app().openapi())
