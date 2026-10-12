import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from fastapi import HTTPException
from pydantic import BaseModel

from control_plane.service_auth import AuthorizationTarget, LaunchplaneIdentity

LAUNCHPLANE_SERVICE_CONTEXT = "launchplane"

_LOGGER = logging.getLogger(__name__)


async def run_in_worker_thread[Result](
    operation: Callable[[], Result],
    *,
    on_finished: Callable[[], None] | None = None,
) -> Result:
    """Run a blocking provider operation, with its whole credential lifetime, off the loop.

    A client disconnect cancels only the wait. The worker thread still finishes, so a
    token it minted is still revoked, and a late failure is logged instead of being
    left unretrieved. `on_finished` runs on the event loop when the worker finishes,
    not when the client disconnects.
    """
    task = asyncio.ensure_future(asyncio.to_thread(operation))
    if on_finished is not None:
        task.add_done_callback(lambda _: on_finished())
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        task.add_done_callback(_log_failure_after_disconnect)
        raise


def _log_failure_after_disconnect(task: asyncio.Future[Any]) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        _LOGGER.warning(
            "Provider operation finished with %s after its client disconnected",
            type(error).__name__,
        )


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
        target: AuthorizationTarget | None = None,
        inherit_agent_read_role: bool = True,
    ) -> bool: ...


class ReadRoleProducts(Protocol):
    def __call__(self, *, identity: LaunchplaneIdentity, context: str) -> frozenset[str]: ...


def no_read_role_products(*, identity: LaunchplaneIdentity, context: str) -> frozenset[str]:
    return frozenset()


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
    read_role_products: ReadRoleProducts = no_read_role_products
