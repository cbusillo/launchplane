from __future__ import annotations

import json
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI


@dataclass(frozen=True)
class RawAsgiResponse:
    status_code: int
    headers: Mapping[str, str]
    content: bytes

    def json(self) -> Any:
        return json.loads(self.content.decode("utf-8"))


async def request(
    app: FastAPI,
    method: str,
    path: str,
    *,
    headers: Mapping[str, str] | None = None,
    extra_headers: Sequence[tuple[str, str]] = (),
    raw_body: bytes = b"",
    set_content_length: bool = True,
) -> RawAsgiResponse:
    request_path, separator, raw_query_string = path.partition("?")
    request_headers = list((headers or {}).items())
    if set_content_length and not any(
        name.lower() == "content-length" for name, _ in request_headers
    ):
        request_headers.append(("Content-Length", str(len(raw_body))))
    request_headers.extend(extra_headers)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": request_path,
        "raw_path": request_path.encode("ascii"),
        "query_string": raw_query_string.encode("ascii") if separator else b"",
        "headers": [
            (name.lower().encode("ascii"), value.encode("latin-1"))
            for name, value in request_headers
        ],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    messages = [{"type": "http.request", "body": raw_body, "more_body": False}]
    sent: list[MutableMapping[str, Any]] = []

    async def receive() -> dict[str, Any]:
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)

    start = next(message for message in sent if message["type"] == "http.response.start")
    content = b"".join(
        message.get("body", b"") for message in sent if message["type"] == "http.response.body"
    )
    return RawAsgiResponse(
        status_code=start["status"],
        headers={
            key.decode("latin-1"): value.decode("latin-1")
            for key, value in start.get("headers", [])
        },
        content=content,
    )
