from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from fastapi import HTTPException
from pydantic import BaseModel

from control_plane.service_auth import LaunchplaneIdentity


class ApiRouteRegistrar(Protocol):
    def add_api_route(
        self,
        path: str,
        endpoint: Callable[..., Any],
        **kwargs: Any,
    ) -> None: ...


class AuthorizationAllows(Protocol):
    def __call__(
        self,
        *,
        identity: LaunchplaneIdentity,
        action: str,
        product: str,
        context: str,
    ) -> bool: ...


class HttpErrorFactory(Protocol):
    def __call__(
        self,
        *,
        status_code: int,
        trace_id: str,
        code: str,
        message: str,
        authz: dict[str, object] | None = None,
    ) -> HTTPException: ...


@dataclass(frozen=True, slots=True)
class ReadRouteDependencies:
    read_identity: Callable[..., LaunchplaneIdentity]
    get_record_store: Callable[[], object]
    next_trace_id: Callable[[], str]
    authorization_allows: AuthorizationAllows
    http_error: HttpErrorFactory
    error_response_model: type[BaseModel]
