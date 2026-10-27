from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from functools import wraps
from inspect import signature
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError
from pydantic import BaseModel, ConfigDict
from typing import Literal


class MaintenanceHold(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hold_id: str
    state: Literal["draining", "held", "release_requested", "released"]
    expires_at: datetime


MAINTENANCE_LOCK = "launchplane:database-maintenance"
MAINTENANCE_RECORD = "database-maintenance"


class DatabaseMaintenanceHeld(RuntimeError):
    pass


@dataclass
class _Operation:
    database: str
    store: object
    active: bool = True


_operation: ContextVar[_Operation | None] = ContextVar("database_operation", default=None)


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


@contextmanager
def operation_guard(store: object) -> Iterator[None]:
    from control_plane.storage.postgres import PostgresRecordStore

    if not isinstance(store, PostgresRecordStore) or store.database_dialect_name != "postgresql":
        yield
        return
    parent = _operation.get()
    assert store._preview_lock_engine is not None
    with store._preview_lock_engine.connect().execution_options(
        isolation_level="AUTOCOMMIT"
    ) as connection:
        acquired = connection.scalar(
            text("SELECT pg_try_advisory_lock_shared(hashtextextended(:key, 0))"),
            {"key": MAINTENANCE_LOCK},
        )
        if not acquired:
            raise DatabaseMaintenanceHeld("Launchplane database maintenance is holding operations.")
        current = _Operation(store.database_url, store)
        token = _operation.set(current)
        try:
            hold = _hold(connection)
            continuing = (
                parent is not None and parent.active and parent.database == store.database_url
            )
            if _active(connection, hold) and not continuing:
                raise DatabaseMaintenanceHeld("Launchplane is draining for database maintenance.")
            yield
        finally:
            current.active = False
            _operation.reset(token)
            connection.execute(
                text("SELECT pg_advisory_unlock_shared(hashtextextended(:key, 0))"),
                {"key": MAINTENANCE_LOCK},
            )


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
    if engine.dialect.name == "postgresql":
        event.listen(engine, "begin", _guard_transaction)


def _guard_transaction(connection: Connection) -> None:
    if not connection.scalar(
        text("SELECT pg_try_advisory_xact_lock_shared(hashtextextended(:key, 0))"),
        {"key": MAINTENANCE_LOCK},
    ):
        raise DatabaseMaintenanceHeld("Launchplane database maintenance is holding transactions.")
    hold = _hold(connection)
    if _active(connection, hold) and hold is not None and hold["state"] == "held":
        raise DatabaseMaintenanceHeld("Launchplane database maintenance has not been released.")


def maintenance_thread_operation[**Parameters, Result](
    operation: Callable[Parameters, Result],
    /,
    *args: Parameters.args,
    **kwargs: Parameters.kwargs,
) -> Result:
    parent = _operation.get()
    with operation_guard(parent.store if parent is not None and parent.active else None):
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
            with operation_guard(self.get_store()):
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
            "expires_at": (now + timedelta(seconds=duration_seconds)).isoformat(),
        }
        _write_hold(connection, hold)
        remaining = (
            datetime.fromisoformat(hold["expires_at"])
            - connection.scalar(text("SELECT clock_timestamp()"))
        ).total_seconds()
        connection.execute(text("BEGIN"))
        connection.execute(
            text("SELECT set_config('transaction_timeout', :timeout, true)"),
            {"timeout": str(max(1, int(remaining * 1000)))},
        )
        deadline = monotonic() + drain_seconds
        while not _try_lock(connection, MAINTENANCE_LOCK):
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
