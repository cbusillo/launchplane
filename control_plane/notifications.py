from __future__ import annotations

import json
from http.client import HTTPConnection
from typing import cast
from urllib.parse import urlsplit

from control_plane.outbound_http import public_url_error as public_url_error
from control_plane.outbound_http import request_public_http
from control_plane.outbound_http import (
    PublicHttpDestination,
    PublicHttpDestinationError,
    _HttpConnection,
    _public_http_connection,
)


USER_AGENT = "Launchplane notifications/1.0"
DISCORD_CRITICAL_ALERT_COLOR = 0xC62828


def public_discord_url_error(url: str) -> str:
    public_error = public_url_error(url)
    if public_error:
        return public_error
    parsed = urlsplit(url.strip())
    hostname = (parsed.hostname or "").lower()
    if hostname not in {"discord.com", "discordapp.com"} and not (
        hostname.endswith(".discord.com") or hostname.endswith(".discordapp.com")
    ):
        return "invalid_url"
    return ""


class DiscordNotificationError(RuntimeError):
    """A provider response, distinguishable from a lost HTTP response."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"Discord webhook returned HTTP {status_code}")


class DiscordNotificationNotSentError(RuntimeError):
    pass


def _connected_discord_http(
    destination: PublicHttpDestination, timeout_seconds: float
) -> _HttpConnection:
    connection = cast(HTTPConnection, _public_http_connection(destination, timeout_seconds))
    try:
        connection.connect()
    except Exception as error:
        connection.close()
        raise DiscordNotificationNotSentError(
            "Discord connection failed before sending."
        ) from error
    return cast(_HttpConnection, connection)


def post_discord_webhook(webhook_url: str, payload: dict[str, object]) -> None:
    url_error = public_discord_url_error(webhook_url)
    if url_error:
        raise ValueError(f"Discord webhook URL is not allowed: {url_error}.")
    connection_factory_called = False

    def connect(destination: PublicHttpDestination, timeout_seconds: float) -> _HttpConnection:
        nonlocal connection_factory_called
        connection_factory_called = True
        return _connected_discord_http(destination, timeout_seconds)

    try:
        response = request_public_http(
            webhook_url,
            method="POST",
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            body=json.dumps(payload).encode("utf-8"),
            timeout_seconds=15,
            max_redirects=0,
            connection_factory=connect,
        )
    except PublicHttpDestinationError as error:
        raise DiscordNotificationNotSentError(
            "Discord destination failed before sending."
        ) from error
    except TimeoutError as error:
        if not connection_factory_called:
            raise DiscordNotificationNotSentError(
                "Discord request budget expired before connecting."
            ) from error
        raise
    if not 200 <= response.status_code < 300:
        raise DiscordNotificationError(response.status_code)
