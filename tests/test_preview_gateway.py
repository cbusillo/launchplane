import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

from control_plane.contracts.idempotency_record import build_launchplane_mutation_reservation_id
from control_plane.preview_gateway import PREVIEW_GATEWAY_PROGRAM, PREVIEW_IDLE_SECONDS


class PreviewGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.now = 0.0
        namespace: dict[str, Any] = {"__name__": "gateway_fixture"}
        exec(PREVIEW_GATEWAY_PROGRAM, namespace)
        self.gateway_type = namespace["PreviewGateway"]
        self.identity = json.dumps({"preview_id": "fixture-preview", "generation": "fixture"})
        self.gateway = self.gateway_type(
            self.identity,
            self.root / "state.json",
            idle_seconds=PREVIEW_IDLE_SECONDS,
            clock=lambda: self.now,
        )
        self.sleep_record = build_launchplane_mutation_reservation_id(
            scope="fixture", route_path="idle", idempotency_key="sleep"
        )
        self.wake_record = build_launchplane_mutation_reservation_id(
            scope="fixture", route_path="idle", idempotency_key="wake"
        )
        self.forwarded: list[bytes] = []
        self.backend = await asyncio.start_server(self.backend_request, "127.0.0.1", 0)
        self.addAsyncCleanup(self.close_server, self.backend)
        self.server = await asyncio.start_server(
            lambda reader, writer: self.gateway.connection(
                reader, writer, "127.0.0.1", self.backend.sockets[0].getsockname()[1]
            ),
            "127.0.0.1",
            0,
        )
        self.addAsyncCleanup(self.close_server, self.server)

    @staticmethod
    async def close_server(server: asyncio.Server) -> None:
        server.close()
        await server.wait_closed()

    async def backend_request(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        request = await reader.read()
        self.forwarded.append(request)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 7\r\nConnection: close\r\n\r\nfixture")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    def sleep_request(self) -> dict[str, object]:
        return {
            "action": "sleep",
            "record_id": self.sleep_record,
            "boot_id": self.gateway.boot_id,
            "sequence": self.gateway.sequence,
        }

    def sleep(self) -> None:
        self.now += PREVIEW_IDLE_SECONDS
        self.assertTrue(self.gateway.control(self.sleep_request())["accepted"])

    async def request(self, payload: bytes) -> bytes:
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", self.server.sockets[0].getsockname()[1]
        )
        writer.write(payload)
        await writer.drain()
        writer.write_eof()
        try:
            return await asyncio.wait_for(reader.read(), timeout=3)
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_activity_and_stale_observations_prevent_sleep(self) -> None:
        self.assertFalse(self.gateway.control(self.sleep_request())["accepted"])
        self.now += PREVIEW_IDLE_SECONDS
        stale = self.sleep_request()
        self.gateway.visit()
        self.assertFalse(self.gateway.control(stale)["accepted"])
        self.now += PREVIEW_IDLE_SECONDS
        self.gateway.active = 1
        self.assertFalse(self.gateway.control(self.sleep_request())["accepted"])
        self.gateway.active = 0
        self.assertTrue(self.gateway.control(self.sleep_request())["accepted"])

    async def test_request_bytes_survive_proxying_and_client_half_close(self) -> None:
        request = b"POST /save HTTP/1.1\r\nContent-Length: 12\r\n\r\nprivate-body"
        response = await self.request(request)
        self.assertTrue(response.startswith(b"HTTP/1.1 200"))
        self.assertEqual(self.forwarded, [request])

    async def test_sleeping_visit_requests_wake_without_forwarding(self) -> None:
        self.sleep()
        response = await self.request(b"GET /web/login HTTP/1.1\r\n\r\n")
        self.assertTrue(response.startswith(b"HTTP/1.1 503"))
        self.assertIn(b"Cache-Control: no-store", response)
        self.assertTrue(self.gateway.status()["requested"])
        self.assertFalse(self.forwarded)
        self.assertFalse(
            self.gateway.control(
                {"action": "awake", "record_id": self.wake_record, "sleep_record_id": "foreign"}
            )["accepted"]
        )
        self.assertTrue(
            self.gateway.control(
                {
                    "action": "awake",
                    "record_id": self.wake_record,
                    "sleep_record_id": self.sleep_record,
                }
            )["accepted"]
        )
        self.assertTrue(
            (await self.request(b"GET /web/login HTTP/1.1\r\n\r\n")).startswith(b"HTTP/1.1 200")
        )

    async def test_sleep_receipt_survives_gateway_restart_but_not_foreign_identity(self) -> None:
        self.sleep()
        restarted = self.gateway_type(
            self.identity, self.root / "state.json", idle_seconds=PREVIEW_IDLE_SECONDS
        )
        self.assertEqual(restarted.status()["sleep_record_id"], self.sleep_record)
        self.assertFalse(restarted.awake.is_set())
        with self.assertRaises(ValueError):
            self.gateway_type(
                "another generation", self.root / "state.json", idle_seconds=PREVIEW_IDLE_SECONDS
            )

    async def test_failed_state_write_keeps_serving_mode_unchanged(self) -> None:
        self.now += PREVIEW_IDLE_SECONDS
        for operation in ("os.replace", "os.fsync"):
            with (
                self.subTest(operation=operation),
                patch(operation, side_effect=OSError("fixture disk failure")),
            ):
                with self.assertRaises(OSError):
                    self.gateway.control(self.sleep_request())
                self.assertFalse(list(self.root.iterdir()))
        self.assertEqual(self.gateway.mode, "awake")
        self.assertTrue(self.gateway.awake.is_set())
        self.assertTrue((await self.request(b"GET / HTTP/1.1\r\n\r\n")).startswith(b"HTTP/1.1 200"))

    async def test_sleeping_post_is_forwarded_once_only_after_wake(self) -> None:
        self.sleep()
        request = b"POST /save HTTP/1.1\r\nContent-Length: 4\r\n\r\ndata"
        pending = asyncio.create_task(self.request(request))

        async def wait_for_visit() -> None:
            while not self.gateway.requested:
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_visit(), timeout=3)
        self.assertFalse(self.forwarded)
        self.gateway.control(
            {
                "action": "awake",
                "record_id": self.wake_record,
                "sleep_record_id": self.sleep_record,
            }
        )
        self.assertTrue((await pending).startswith(b"HTTP/1.1 200"))
        self.assertEqual(self.forwarded, [request])
