import asyncio
from contextlib import contextmanager
from collections.abc import Iterator
from threading import Event
import unittest
from unittest.mock import patch

from control_plane.database_maintenance import (
    DatabaseMaintenanceHeld,
    DatabaseMaintenanceMiddleware,
    maintenance_operation,
    maintenance_thread,
)


class DatabaseMaintenanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_maintenance_refuses_before_handler_effects(self) -> None:
        effects: list[str] = []
        messages: list[dict[str, object]] = []

        async def app(scope: object, receive: object, send: object) -> None:
            effects.append("provider")

        async def receive() -> dict[str, object]:
            return {"type": "http.request"}

        async def send(message: dict[str, object]) -> None:
            messages.append(message)

        with patch(
            "control_plane.database_maintenance._acquire_scope",
            side_effect=DatabaseMaintenanceHeld,
        ):
            middleware = DatabaseMaintenanceMiddleware(app, get_store=object)
            await middleware({"type": "http", "path": "/v1/action"}, receive, send)
        self.assertFalse(effects)
        self.assertEqual(messages[0]["status"], 503)

    async def test_disconnected_thread_keeps_its_own_operation_scope(self) -> None:
        entered, finish, exited = Event(), Event(), Event()

        @contextmanager
        def guard(store: object) -> Iterator[None]:
            entered.set()
            try:
                yield
            finally:
                exited.set()

        def provider() -> None:
            finish.wait(5)

        with patch("control_plane.database_maintenance.operation_guard", guard):
            task = asyncio.create_task(maintenance_thread(provider))
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(exited.is_set())
            finish.set()
            self.assertTrue(await asyncio.to_thread(exited.wait, 5))

    async def test_operation_refuses_before_entering_provider_code(self) -> None:
        effects: list[str] = []

        @maintenance_operation
        def execute(*, record_store: object) -> None:
            effects.append("provider")

        with patch(
            "control_plane.database_maintenance.operation_guard",
            side_effect=DatabaseMaintenanceHeld,
        ):
            with self.assertRaises(DatabaseMaintenanceHeld):
                execute(record_store=object())
        self.assertFalse(effects)
