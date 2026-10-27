from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial, wraps
from inspect import signature
from typing import Any
from threading import Lock
import asyncio
import anyio
from anyio import CapacityLimiter
from anyio.lowlevel import RunVar

from sqlalchemy import event, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError
from pydantic import BaseModel, ConfigDict
from typing import Literal


class MaintenanceHold(BaseModel):
    model_config = ConfigDict(extra="ignore")

    hold_id: str
    state: Literal["draining", "held", "release_requested", "released"]
    expires_at: datetime
    reason: str = ""


MAINTENANCE_LOCK = "launchplane:database-maintenance"
MAINTENANCE_RECORD = "database-maintenance"


class DatabaseMaintenanceHeld(RuntimeError):
    pass


@dataclass
class _Lease:
    connection: Connection
    references: int = 1
    lock: Lock = field(default_factory=Lock)


@dataclass
class _Operation:
    database: str
    store: object
    lease: _Lease
    active: bool = True
    released: bool = False


_operation: ContextVar[_Operation | None] = ContextVar("database_operation", default=None)
_scope_limiter: RunVar[CapacityLimiter] = RunVar("maintenance_scope_limiter")


def _hold(connection: Connection, *, for_update: bool = False) -> dict[str, Any] | None:
    if connection.scalar(text("SELECT to_regclass('launchplane_service_deploy_drains')")) is None:
        return None
    payload = connection.scalar(
        text(
            "SELECT payload FROM launchplane_service_deploy_drains WHERE record_id = :record"
            + (" FOR UPDATE" if for_update else "")
        ),
        {"record": MAINTENANCE_RECORD},
    )
    return (
        MaintenanceHold.model_validate(payload).model_dump(mode="json")
        if payload is not None
        else None
    )


def _active(connection: Connection, hold: dict[str, Any] | None) -> bool:
    return bool(
        hold
        and hold["state"] != "released"
        and datetime.fromisoformat(hold["expires_at"])
        > connection.scalar(text("SELECT clock_timestamp()"))
    )


def _acquire_scope(store: object, parent: _Operation | None) -> _Operation | None:
    from control_plane.storage.postgres import PostgresRecordStore

    if not isinstance(store, PostgresRecordStore) or store.database_dialect_name != "postgresql":
        return None
    if parent is not None and parent.database == store.database_url:
        with parent.lease.lock:
            if parent.active:
                parent.lease.references += 1
                return _Operation(store.database_url, store, parent.lease)
    assert store._maintenance_engine is not None
    connection = store._maintenance_engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    acquired = False
    try:
        acquired = bool(
            connection.scalar(
                text("SELECT pg_try_advisory_lock_shared(hashtextextended(:key, 0))"),
                {"key": MAINTENANCE_LOCK},
            )
        )
        if not acquired or _active(connection, _hold(connection)):
            raise DatabaseMaintenanceHeld(
                "Launchplane is holding new operations for database maintenance."
            )
        return _Operation(store.database_url, store, _Lease(connection))
    except BaseException:
        try:
            if acquired:
                connection.execute(
                    text("SELECT pg_advisory_unlock_shared(hashtextextended(:key, 0))"),
                    {"key": MAINTENANCE_LOCK},
                )
        except DBAPIError:
            connection.invalidate()
        finally:
            connection.close()
        raise


def _release_scope(scope: _Operation) -> None:
    with scope.lease.lock:
        if scope.released:
            return
        scope.active = False
        scope.released = True
        scope.lease.references -= 1
        if scope.lease.references:
            return
    connection = scope.lease.connection
    try:
        connection.execute(
            text("SELECT pg_advisory_unlock_shared(hashtextextended(:key, 0))"),
            {"key": MAINTENANCE_LOCK},
        )
    except DBAPIError:
        connection.invalidate()
        raise
    finally:
        connection.close()


@contextmanager
def operation_guard(store: object) -> Iterator[None]:
    scope = _acquire_scope(store, _operation.get())
    if scope is None:
        yield
        return
    token = _operation.set(scope)
    try:
        yield
    finally:
        _operation.reset(token)
        _release_scope(scope)


async def _scope_io[Result](operation: Callable[[], Result]) -> Result:
    try:
        limiter = _scope_limiter.get()
    except LookupError:
        limiter = CapacityLimiter(4)
        _scope_limiter.set(limiter)
    return await anyio.to_thread.run_sync(operation, limiter=limiter)


async def _settle_task[Result](task: asyncio.Task[Result]) -> Result:
    while True:
        with anyio.CancelScope(shield=True):
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.cancelled():
                    raise


@asynccontextmanager
async def async_operation_guard(store: object) -> AsyncIterator[None]:
    task = asyncio.create_task(_scope_io(partial(_acquire_scope, store, _operation.get())))
    try:
        scope = await asyncio.shield(task)
    except asyncio.CancelledError:
        scope = await _settle_task(task)
        if scope is not None:
            await _settle_task(asyncio.create_task(_scope_io(partial(_release_scope, scope))))
        raise
    if scope is None:
        yield
        return
    token = _operation.set(scope)
    try:
        yield
    finally:
        with scope.lease.lock:
            scope.active = False
        _operation.reset(token)
        await _settle_task(asyncio.create_task(_scope_io(partial(_release_scope, scope))))


def maintenance_operation[**Parameters, Result](
    operation: Callable[Parameters, Result],
) -> Callable[Parameters, Result]:
    parameters = signature(operation)

    @wraps(operation)
    def guarded(*args: Parameters.args, **kwargs: Parameters.kwargs) -> Result:
        arguments = parameters.bind(*args, **kwargs).arguments
        store = arguments.get("record_store", arguments.get("store"))
        if store is None and "self" in arguments:
            store = arguments["self"].store
        with operation_guard(store):
            return operation(*args, **kwargs)

    return guarded


def install_transaction_guard(engine: Engine) -> None:
    if engine.dialect.name != "postgresql":
        return
    relation_known = False

    def guard(connection: Connection) -> None:
        nonlocal relation_known
        if not relation_known:
            relation_known = (
                connection.scalar(text("SELECT to_regclass('launchplane_service_deploy_drains')"))
                is not None
            )
        _guard_transaction(connection, relation_known=relation_known)

    event.listen(engine, "begin", guard)
    engine.update_execution_options(launchplane_maintenance_guarded=True)


def _guard_transaction(connection: Connection, *, relation_known: bool) -> None:
    hold_query = (
        "coalesce((SELECT payload->>'state' = 'held' AND "
        "(payload->>'expires_at')::timestamptz > clock_timestamp() "
        "FROM launchplane_service_deploy_drains WHERE record_id = :record), false)"
        if relation_known
        else "false"
    )
    admitted, held = connection.execute(
        text("SELECT pg_try_advisory_xact_lock_shared(hashtextextended(:key, 0)), " + hold_query),
        {"key": MAINTENANCE_LOCK, "record": MAINTENANCE_RECORD},
    ).one()
    if not admitted or held:
        raise DatabaseMaintenanceHeld("Launchplane database maintenance is holding transactions.")


def maintenance_thread_operation[**Parameters, Result](
    operation: Callable[Parameters, Result],
    /,
    *args: Parameters.args,
    **kwargs: Parameters.kwargs,
) -> Result:
    parent = _operation.get()
    with operation_guard(parent.store if parent is not None else None):
        return operation(*args, **kwargs)


async def maintenance_thread[**Parameters, Result](
    operation: Callable[Parameters, Result],
    /,
    *args: Parameters.args,
    **kwargs: Parameters.kwargs,
) -> Result:
    import asyncio

    def execute() -> Result:
        return maintenance_thread_operation(operation, *args, **kwargs)

    return await asyncio.to_thread(execute)


async def maintenance_threadpool[**Parameters, Result](
    operation: Callable[Parameters, Result],
    /,
    *args: Parameters.args,
    **kwargs: Parameters.kwargs,
) -> Result:
    from starlette.concurrency import run_in_threadpool

    return await run_in_threadpool(
        partial(maintenance_thread_operation, operation, *args, **kwargs)
    )


class DatabaseMaintenanceMiddleware:
    def __init__(self, app: Any, *, get_store: Callable[[], object]) -> None:
        self.app = app
        self.get_store = get_store

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        from starlette.responses import JSONResponse
        from uuid import uuid4

        if scope["type"] not in {"http", "websocket"} or scope.get("path") == "/v1/health":
            await self.app(scope, receive, send)
            return
        response_started = False

        async def track_send(message: Any) -> None:
            nonlocal response_started
            response_started = True
            await send(message)

        try:
            async with async_operation_guard(self.get_store()):
                await self.app(scope, receive, track_send)
        except DatabaseMaintenanceHeld:
            if response_started:
                raise
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1013})
                return
            await JSONResponse(
                status_code=503,
                content={
                    "trace_id": f"launchplane_req_{uuid4().hex}",
                    "error": {
                        "code": "database_maintenance",
                        "message": "Launchplane database maintenance is in progress.",
                    },
                },
                headers={"Retry-After": "30"},
            )(scope, receive, send)


def _write_hold(connection: Connection, hold: dict[str, Any]) -> None:
    from control_plane.storage.postgres import LaunchplaneServiceDeployDrainRow
    from sqlalchemy.dialects.postgresql import insert

    statement = insert(LaunchplaneServiceDeployDrainRow).values(
        record_id=MAINTENANCE_RECORD, payload=hold
    )
    connection.execute(
        statement.on_conflict_do_update(
            index_elements=[LaunchplaneServiceDeployDrainRow.record_id],
            set_={"payload": statement.excluded.payload},
        )
    )


@contextmanager
def maintenance_window(
    engine: Engine, *, duration_seconds: int, reason: str, drain_seconds: int = 60
) -> Iterator[dict[str, Any]]:
    from datetime import timedelta
    from time import monotonic, sleep
    from uuid import uuid4

    if engine.dialect.name != "postgresql":
        raise ValueError("Database maintenance requires PostgreSQL.")
    if engine.get_execution_options().get("launchplane_maintenance_guarded"):
        raise ValueError(
            "Maintenance needs a dedicated operator engine, not a guarded store engine."
        )
    if not 1 <= drain_seconds < duration_seconds <= 3600 or not reason.strip():
        raise ValueError("Maintenance requires a reason and a bounded drain/window.")
    hold_id = str(uuid4())
    connection = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    acquired: list[str] = []
    hold: dict[str, Any] | None = None
    try:
        control_key = f"{MAINTENANCE_LOCK}:control"
        if not _try_lock(connection, control_key):
            raise DatabaseMaintenanceHeld("Another maintenance controller is running.")
        acquired.append(control_key)
        if _active(connection, _hold(connection)):
            raise DatabaseMaintenanceHeld("An earlier maintenance hold has not expired.")
        now = connection.scalar(text("SELECT clock_timestamp()"))
        hold = {
            "hold_id": hold_id,
            "state": "draining",
            "reason": reason.strip(),
            "expires_at": (now + timedelta(seconds=duration_seconds)).isoformat(),
        }
        _write_hold(connection, hold)
        remaining = (
            datetime.fromisoformat(hold["expires_at"])
            - connection.scalar(text("SELECT clock_timestamp()"))
        ).total_seconds()
        connection.execute(text("BEGIN"))
        connection.execute(text("SET LOCAL idle_in_transaction_session_timeout = 0"))
        connection.execute(
            text("SELECT set_config('transaction_timeout', :timeout, true)"),
            {"timeout": str(max(1, int(remaining * 1000)))},
        )
        deadline = monotonic() + drain_seconds
        while True:
            current = _hold(connection)
            if current is not None and current["state"] == "release_requested":
                raise DatabaseMaintenanceHeld("Maintenance was released while draining.")
            if _try_lock(connection, MAINTENANCE_LOCK):
                break
            if monotonic() >= deadline:
                raise DatabaseMaintenanceHeld("Maintenance could not drain active operations.")
            sleep(0.05)
        acquired.append(MAINTENANCE_LOCK)
        from control_plane.storage.schema_migration import _SCHEMA_MIGRATION_LOCK_KEY

        for key in (_SCHEMA_MIGRATION_LOCK_KEY, "launchplane:service-deploy-provider-dispatch"):
            if not _try_lock(connection, key):
                raise DatabaseMaintenanceHeld("A migration or deployment is still running.")
            acquired.append(key)
        hold = {**hold, "state": "held"}
        with engine.begin() as receipt:
            current = _hold(receipt, for_update=True)
            if current is None or current["hold_id"] != hold_id or current["state"] != "draining":
                raise DatabaseMaintenanceHeld("Maintenance custody changed during drain.")
            _write_hold(receipt, hold)
        backend_pid = connection.scalar(text("SELECT pg_backend_pid()"))
        yield {**hold, "backend_pid": backend_pid}
    finally:
        try:
            if hold is not None:
                with engine.begin() as cleanup:
                    current = _hold(cleanup, for_update=True)
                    if current is not None and current.get("hold_id") == hold_id:
                        _write_hold(cleanup, {**current, "state": "released"})
        finally:
            try:
                if not connection.invalidated:
                    connection.execute(text("ROLLBACK"))
                    for key in reversed(acquired):
                        connection.execute(
                            text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"),
                            {"key": key},
                        )
            except DBAPIError:
                connection.invalidate()
            finally:
                connection.close()


def _try_lock(connection: Connection, key: str) -> bool:
    return bool(
        connection.scalar(
            text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))"), {"key": key}
        )
    )
