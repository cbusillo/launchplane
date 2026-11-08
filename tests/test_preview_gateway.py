import asyncio
from contextlib import suppress
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

from control_plane.contracts.idempotency_record import build_launchplane_mutation_reservation_id
import yaml

from control_plane.dokploy.compose import ODOO_DEFAULT_WORKERS, render_odoo_raw_compose_file
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.preview_gateway import (
    PREVIEW_EVENTED_PORT,
    PREVIEW_GATEWAY_PROGRAM,
    PREVIEW_GATEWAY_SERVICE,
    PREVIEW_GATEWAY_INSPECTION_LIBRARY,
    preview_backend_alias,
    PREVIEW_IDLE_SECONDS,
)


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
        self.backend_port = self.backend.sockets[0].getsockname()[1]
        self.addAsyncCleanup(self.close_server, self.backend)
        self.server = await asyncio.start_server(
            lambda reader, writer: self.gateway.connection(
                reader, writer, "127.0.0.1", self.backend_port
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

    async def test_evented_listener_routes_upgrades_and_preserves_threaded_http(self) -> None:
        start_server = asyncio.start_server
        open_connection = asyncio.open_connection
        listeners: dict[int, asyncio.Server] = {}
        received: list[tuple[int, bytes]] = []
        ready = asyncio.Event()
        http_port = self.backend_port
        evented_port = PREVIEW_EVENTED_PORT
        upgrade = (
            b"HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n"
        )
        frame = b"\x81\x02ok"

        async def backend(
            upstream_reader: asyncio.StreamReader, upstream_writer: asyncio.StreamWriter
        ) -> None:
            handshake = await upstream_reader.readuntil(b"\r\n\r\n")
            received.append((upstream_writer.get_extra_info("sockname")[1], handshake))
            upstream_writer.write(upgrade)
            await upstream_writer.drain()
            upstream_writer.write(await upstream_reader.readexactly(len(frame)))
            await upstream_writer.drain()
            upstream_writer.close()
            await upstream_writer.wait_closed()

        evented = await start_server(backend, "127.0.0.1", 0)
        self.addAsyncCleanup(self.close_server, evented)
        evented_backend_port = evented.sockets[0].getsockname()[1]

        async def start_listener(callback: Any, _host: str, port: int) -> asyncio.Server:
            server = await start_server(callback, "127.0.0.1", 0)
            listeners[port] = server
            if len(listeners) == 2:
                ready.set()
            return server

        async def connect_backend(
            _host: str, port: int
        ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
            return await open_connection(
                "127.0.0.1", evented_backend_port if port == evented_port else http_port
            )

        for workers in (None, 4, 0):
            with self.subTest(workers=workers):
                listeners.clear()
                received.clear()
                ready.clear()
                with (
                    patch.object(asyncio, "start_server", side_effect=start_listener),
                    patch.object(asyncio, "open_connection", side_effect=connect_backend),
                ):
                    identity = RuntimeIdentity(
                        context="fixture",
                        instance="fixture-preview",
                        environment_kind="preview",
                        deployment_record_id="fixture-deploy",
                        artifact_id="fixture-artifact",
                        source_git_ref="fixture-source",
                        image_reference="fixture-image",
                        preview_id="fixture-preview",
                        preview_generation_id="fixture-generation",
                    )
                    services = yaml.safe_load(
                        render_odoo_raw_compose_file(
                            image_reference=identity.image_reference,
                            hold_web_until_integration_readback=False,
                            runtime_port=http_port,
                            publish_host_ports=False,
                            mount_workflow_ssh=False,
                            preview_identity=identity,
                            preview_workers=workers,
                        )
                    )["services"]
                    command = services[PREVIEW_GATEWAY_SERVICE]["command"]
                    arguments = [str(argument).replace("$$", "$") for argument in command]
                    namespace: dict[str, Any] = {"__name__": "rendered_gateway_fixture"}
                    exec(arguments[0], namespace)
                    gateway = namespace["PreviewGateway"](
                        self.identity,
                        self.root / "rendered-state.json",
                        idle_seconds=PREVIEW_IDLE_SECONDS,
                        clock=lambda: self.now,
                    )
                    serving = asyncio.create_task(
                        gateway.serve("fixture-backend", int(arguments[1]), int(arguments[2]))
                    )
                    try:
                        await asyncio.wait_for(ready.wait(), timeout=3)
                        http_listener = listeners[http_port].sockets[0].getsockname()[1]
                        reader, writer = await open_connection("127.0.0.1", http_listener)
                        writer.write(b"POST /save HTTP/1.1\r\nContent-Length: 4\r\n\r\ndata")
                        await writer.drain()
                        writer.write_eof()
                        self.assertTrue(
                            (await asyncio.wait_for(reader.read(), 3)).startswith(b"HTTP/1.1 200")
                        )
                        writer.close()
                        await writer.wait_closed()
                        websocket_listener = listeners[evented_port].sockets[0].getsockname()[1]
                        reader, writer = await open_connection("127.0.0.1", websocket_listener)
                        request = b"GET /websocket?version=fixture HTTP/1.1\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n"
                        writer.write(request)
                        await writer.drain()
                        if (ODOO_DEFAULT_WORKERS if workers is None else workers) > 0:
                            self.assertEqual(
                                await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3), upgrade
                            )
                            self.assertEqual(gateway.active, 1)
                            self.now += PREVIEW_IDLE_SECONDS
                            self.assertFalse(
                                gateway.control(
                                    {
                                        **self.sleep_request(),
                                        "boot_id": gateway.boot_id,
                                        "sequence": gateway.sequence,
                                    }
                                )["accepted"]
                            )
                            writer.write(frame)
                            await writer.drain()
                            self.assertEqual(
                                await asyncio.wait_for(reader.readexactly(len(frame)), 3), frame
                            )
                            self.assertEqual(received, [(evented_backend_port, request)])
                        else:
                            writer.write_eof()
                            self.assertTrue(
                                (await asyncio.wait_for(reader.read(), 3)).startswith(
                                    b"HTTP/1.1 200"
                                )
                            )
                            self.assertIn(request, self.forwarded)
                        writer.close()
                        await writer.wait_closed()
                    finally:
                        serving.cancel()
                        with suppress(asyncio.CancelledError):
                            await serving

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
                    "action": "waking",
                    "record_id": self.wake_record,
                    "sleep_record_id": self.sleep_record,
                }
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
                "action": "waking",
                "record_id": self.wake_record,
                "sleep_record_id": self.sleep_record,
            }
        )
        self.gateway.control(
            {
                "action": "awake",
                "record_id": self.wake_record,
                "sleep_record_id": self.sleep_record,
            }
        )
        self.assertTrue((await pending).startswith(b"HTTP/1.1 200"))
        self.assertEqual(self.forwarded, [request])

    async def test_admitted_wake_receipt_survives_restart_and_rejects_another_operation(
        self,
    ) -> None:
        self.sleep()
        self.assertTrue(
            self.gateway.control(
                {
                    "action": "waking",
                    "record_id": self.wake_record,
                    "sleep_record_id": self.sleep_record,
                }
            )["accepted"]
        )
        restarted = self.gateway_type(
            self.identity, self.root / "state.json", idle_seconds=PREVIEW_IDLE_SECONDS
        )
        self.assertEqual(restarted.wake_record_id, self.wake_record)
        self.assertFalse(restarted.awake.is_set())
        self.assertFalse(
            restarted.control(
                {
                    "action": "waking",
                    "record_id": self.sleep_record,
                    "sleep_record_id": self.sleep_record,
                }
            )["accepted"]
        )
        self.assertTrue(
            restarted.control(
                {
                    "action": "awake",
                    "record_id": self.wake_record,
                    "sleep_record_id": self.sleep_record,
                }
            )["accepted"]
        )

    async def test_transient_backend_failure_does_not_create_a_permanent_wake_request(self) -> None:
        await self.close_server(self.backend)
        response = await self.request(b"GET / HTTP/1.1\r\n\r\n")
        self.assertTrue(response.startswith(b"HTTP/1.1 503"))
        self.assertFalse(self.gateway.requested)
        self.now += PREVIEW_IDLE_SECONDS
        self.assertTrue(self.gateway.control(self.sleep_request())["accepted"])


class PreviewGatewayInspectionTests(unittest.TestCase):
    def test_native_inspection_binds_worker_count_and_keeps_legacy_gateways(self) -> None:
        identity = json.dumps({"preview_id": "fixture"})
        networks = {
            "private": {"Aliases": [preview_backend_alias(identity)]},
            "dokploy-network": {},
        }
        web: dict[str, Any] = {
            "Config": {
                "Image": "fixture-image",
                "Env": ["LAUNCHPLANE_RUNTIME_IDENTITY_JSON=" + identity, "ODOO_WORKERS=4"],
            },
            "NetworkSettings": {"Networks": networks},
        }
        gateway: dict[str, Any] = {
            "Image": "fixture-image-id",
            "Config": {
                "Image": "fixture-image",
                "Entrypoint": ["/venv/bin/python3", "-I", "-S", "-c"],
                "Cmd": [PREVIEW_GATEWAY_PROGRAM, "8069"],
                "Env": ["LAUNCHPLANE_RUNTIME_IDENTITY_JSON=" + identity],
            },
            "HostConfig": {"CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges:true"]},
            "NetworkSettings": {"Networks": networks},
        }
        namespace: dict[str, Any] = {
            "json": json,
            "docker": lambda *_: json.dumps([{"Config": {"Env": []}}]),
        }
        exec(PREVIEW_GATEWAY_INSPECTION_LIBRARY, namespace)
        inspect_gateway = namespace["inspect_preview_gateway"]
        inspect_gateway(gateway, web)
        gateway["Config"]["Cmd"].append(str(PREVIEW_EVENTED_PORT))
        inspect_gateway(gateway, web)
        web["Config"]["Env"][-1] = "ODOO_WORKERS=0"
        with self.assertRaises(RuntimeError):
            inspect_gateway(gateway, web)
        gateway["Config"]["Cmd"][-1] = gateway["Config"]["Cmd"][1]
        inspect_gateway(gateway, web)
        for backend_port in ("0", "-1", "invalid"):
            with self.subTest(backend_port=backend_port):
                gateway["Config"]["Cmd"][-1] = backend_port
                with self.assertRaises(RuntimeError):
                    inspect_gateway(gateway, web)
