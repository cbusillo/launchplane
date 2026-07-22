from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
import json
import unittest

from starlette.types import Message, Receive, Scope, Send

from control_plane.http_app import BoundedRequestBodyMiddleware


_WEBHOOK_PATH = "/v1/every-code/github-webhook"
_WEBHOOK_BODY_LIMIT = 2 * 1024 * 1024
_JSON_MUTATION_ROUTES = (
    ("/v1/product-config/apply", 2 * 1024 * 1024),
    ("/v1/product-profiles/health-monitoring/apply", 64 * 1024),
    ("/v1/secrets/reencrypt", 64 * 1024),
)


class WebhookBodyGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_json_mutation_routes_require_json_content_type(self) -> None:
        for path, _limit in _JSON_MUTATION_ROUTES:
            with self.subTest(path=path):
                guarded_bodies: list[bytes] = []
                response = await _invoke_guarded_webhook(
                    path=path,
                    headers=[("Content-Length", "2")],
                    chunks=(b"{}",),
                    guarded_bodies=guarded_bodies,
                )

                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"]["code"], "invalid_request")
                self.assertEqual(guarded_bodies, [])

    async def test_json_mutation_routes_reject_oversized_declared_bodies(self) -> None:
        for path, limit in _JSON_MUTATION_ROUTES:
            with self.subTest(path=path):
                guarded_bodies: list[bytes] = []
                response = await _invoke_guarded_webhook(
                    path=path,
                    headers=[
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(limit + 1)),
                    ],
                    chunks=(b"",),
                    guarded_bodies=guarded_bodies,
                )

                self.assertEqual(response.status_code, 413)
                self.assertEqual(response.receive_count, 0)
                self.assertEqual(guarded_bodies, [])

    async def test_json_mutation_routes_require_content_length(self) -> None:
        for path, _limit in _JSON_MUTATION_ROUTES:
            with self.subTest(path=path):
                guarded_bodies: list[bytes] = []
                response = await _invoke_guarded_webhook(
                    path=path,
                    headers=[("Content-Type", "application/json")],
                    chunks=(b"{}",),
                    guarded_bodies=guarded_bodies,
                )

                self.assertEqual(response.status_code, 413)
                self.assertEqual(response.receive_count, 0)
                self.assertEqual(guarded_bodies, [])

    async def test_json_mutation_routes_reject_chunked_transfer_encoding(self) -> None:
        for path, _limit in _JSON_MUTATION_ROUTES:
            with self.subTest(path=path):
                guarded_bodies: list[bytes] = []
                response = await _invoke_guarded_webhook(
                    path=path,
                    headers=[
                        ("Content-Type", "application/json"),
                        ("Transfer-Encoding", "chunked"),
                    ],
                    chunks=(b"{}",),
                    guarded_bodies=guarded_bodies,
                )

                self.assertEqual(response.status_code, 413)
                self.assertEqual(response.receive_count, 0)
                self.assertEqual(guarded_bodies, [])

    async def test_exact_declared_chunked_asgi_body_reaches_downstream(self) -> None:
        guarded_bodies: list[bytes] = []
        response = await _invoke_guarded_webhook(
            headers=[("Content-Length", "4")],
            chunks=(b"ab", b"cd"),
            guarded_bodies=guarded_bodies,
        )

        self.assertEqual(response.status_code, 204)
        self.assertEqual(guarded_bodies, [b"abcd"])

    async def test_transfer_encoding_chunked_is_rejected_before_downstream(self) -> None:
        guarded_bodies: list[bytes] = []
        response = await _invoke_guarded_webhook(
            headers=[("Transfer-Encoding", "chunked")],
            chunks=(b"{}",),
            guarded_bodies=guarded_bodies,
        )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["error"]["code"], "request_entity_too_large")
        self.assertEqual(guarded_bodies, [])

    async def test_forged_content_length_is_rejected_before_downstream(self) -> None:
        guarded_bodies: list[bytes] = []
        response = await _invoke_guarded_webhook(
            headers=[("Content-Length", "1")],
            chunks=(b"{}",),
            guarded_bodies=guarded_bodies,
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")
        self.assertIn("Content-Length does not match", response.json()["error"]["message"])
        self.assertEqual(guarded_bodies, [])

    async def test_declared_body_over_limit_is_rejected_without_reading_body(self) -> None:
        guarded_bodies: list[bytes] = []
        response = await _invoke_guarded_webhook(
            headers=[("Content-Length", str(_WEBHOOK_BODY_LIMIT + 1))],
            chunks=(b"",),
            guarded_bodies=guarded_bodies,
        )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.receive_count, 0)
        self.assertEqual(guarded_bodies, [])

    async def test_extremely_large_declared_length_fails_without_integer_conversion(self) -> None:
        guarded_bodies: list[bytes] = []
        response = await _invoke_guarded_webhook(
            headers=[("Content-Length", "9" * 5000)],
            chunks=(b"",),
            guarded_bodies=guarded_bodies,
        )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.receive_count, 0)
        self.assertEqual(guarded_bodies, [])

    async def test_observed_body_over_limit_is_rejected_while_streaming(self) -> None:
        guarded_bodies: list[bytes] = []
        response = await _invoke_guarded_webhook(
            headers=[("Content-Length", str(_WEBHOOK_BODY_LIMIT))],
            chunks=(b"a" * _WEBHOOK_BODY_LIMIT, b"b"),
            guarded_bodies=guarded_bodies,
        )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.receive_count, 2)
        self.assertEqual(guarded_bodies, [])

    async def test_client_disconnect_returns_without_sending_response(self) -> None:
        guarded_bodies: list[bytes] = []
        sent: list[Message] = []
        messages: list[Message] = [
            {"type": "http.request", "body": b"{}", "more_body": True},
            {"type": "http.disconnect"},
        ]
        app = BoundedRequestBodyMiddleware(_body_capturing_app(guarded_bodies))
        scope = _webhook_scope(headers=[("Content-Length", "4")])

        async def receive() -> Message:
            return messages.pop(0)

        async def send(message: Message) -> None:
            sent.append(message)

        await app(scope, receive, send)

        self.assertEqual(sent, [])
        self.assertEqual(guarded_bodies, [])


class _GuardResponse:
    def __init__(self, status_code: int, body: bytes, receive_count: int) -> None:
        self.status_code = status_code
        self.body = body
        self.receive_count = receive_count

    def json(self) -> dict[str, Any]:
        payload = json.loads(self.body.decode("utf-8"))
        assert isinstance(payload, dict)
        return payload


async def _invoke_guarded_webhook(
    *,
    path: str = _WEBHOOK_PATH,
    headers: list[tuple[str, str]],
    chunks: tuple[bytes, ...],
    guarded_bodies: list[bytes],
) -> _GuardResponse:
    downstream = _body_capturing_app(guarded_bodies)
    app = BoundedRequestBodyMiddleware(downstream)
    scope = _webhook_scope(path=path, headers=headers)
    messages: list[Message] = [
        {
            "type": "http.request",
            "body": chunk,
            "more_body": index < len(chunks) - 1,
        }
        for index, chunk in enumerate(chunks)
    ]
    sent: list[Message] = []
    receive_count = 0

    async def receive() -> Message:
        nonlocal receive_count
        receive_count += 1
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)
    start = next(message for message in sent if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"") for message in sent if message["type"] == "http.response.body"
    )
    return _GuardResponse(start["status"], body, receive_count)


def _webhook_scope(*, path: str = _WEBHOOK_PATH, headers: list[tuple[str, str]]) -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": [
            (name.lower().encode("ascii"), value.encode("latin-1")) for name, value in headers
        ],
        "client": ("testclient", 50000),
        "server": ("testserver", 443),
    }


def _body_capturing_app(
    guarded_bodies: list[bytes],
) -> Callable[[Scope, Receive, Send], Awaitable[None]]:
    async def app(_scope: Scope, receive: Receive, send: Send) -> None:
        body_parts: list[bytes] = []
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            body_parts.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        guarded_bodies.append(b"".join(body_parts))
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    return app


if __name__ == "__main__":
    unittest.main()
