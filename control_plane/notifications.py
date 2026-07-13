from __future__ import annotations

import json
from urllib.parse import urlsplit

from control_plane.outbound_http import public_url_error as public_url_error
from control_plane.outbound_http import request_public_http


USER_AGENT = "Launchplane notifications/1.0"


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


def post_discord_webhook(webhook_url: str, payload: dict[str, object]) -> None:
    url_error = public_discord_url_error(webhook_url)
    if url_error:
        raise ValueError(f"Discord webhook URL is not allowed: {url_error}.")
    response = request_public_http(
        webhook_url,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        body=json.dumps(payload).encode("utf-8"),
        timeout_seconds=15,
    )
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"Discord webhook returned HTTP {response.status_code}")
