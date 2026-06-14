from __future__ import annotations

import ipaddress
import json
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


USER_AGENT = "Launchplane notifications/1.0"


def public_url_error(url: str) -> str:
    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "invalid_url"
    hostname = (parsed.hostname or "").lower()
    if hostname in {"localhost", "127.0.0.1", "::1"}:
        return "private_url"
    try:
        ip_address = ipaddress.ip_address(hostname)
        if (
            ip_address.is_private
            or ip_address.is_loopback
            or ip_address.is_link_local
            or ip_address.is_multicast
            or ip_address.is_reserved
            or ip_address.is_unspecified
        ):
            return "private_url"
    except ValueError:
        pass
    if (
        hostname.endswith(".local")
        or hostname.endswith(".internal")
        or hostname.endswith(".invalid")
    ):
        return "private_url"
    return ""


def public_discord_url_error(url: str) -> str:
    public_error = public_url_error(url)
    if public_error:
        return public_error
    parsed = urlsplit(url.strip())
    hostname = (parsed.hostname or "").lower()
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        return "invalid_url"
    if hostname not in {"discord.com", "discordapp.com"} and not (
        hostname.endswith(".discord.com") or hostname.endswith(".discordapp.com")
    ):
        return "invalid_url"
    return ""


def post_discord_webhook(webhook_url: str, payload: dict[str, object]) -> None:
    request = Request(
        webhook_url,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        data=json.dumps(payload).encode("utf-8"),
    )
    with urlopen(request, timeout=15) as response:  # noqa: S310 - operator-configured webhook URL with SSRF guard.
        if not 200 <= response.status < 300:
            raise RuntimeError(f"Discord webhook returned HTTP {response.status}")
