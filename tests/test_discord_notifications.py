from __future__ import annotations

from collections.abc import Mapping
import socket
import unittest
from unittest.mock import patch

from control_plane import notifications, outbound_http
from control_plane.notifications import DiscordNotificationNotSentError
from tests.test_outbound_http import _FakeConnection, _FakeResponse, _resolver_for


class _DiscordConnection(_FakeConnection):
    def __init__(self, stage: str) -> None:
        super().__init__(_FakeResponse(status=204))
        self.stage = stage

    def connect(self) -> None:
        if self.stage == "connect":
            raise TimeoutError("before request")

    def request(
        self,
        method: str,
        url: str,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().request(method, url, body, headers)
        if self.stage == "response":
            raise TimeoutError("after request")


class DiscordNotificationTransportTests(unittest.TestCase):
    def test_only_dns_and_preconnect_failures_are_proven_not_sent(self) -> None:
        for stage in ("dns", "connect", "response"):
            with self.subTest(stage=stage):
                connection = _DiscordConnection(stage)
                with (
                    patch.object(notifications, "_public_http_connection", return_value=connection),
                    patch.object(
                        outbound_http,
                        "_system_address_resolver",
                        side_effect=socket.gaierror()
                        if stage == "dns"
                        else _resolver_for("93.184.216.34"),
                    ),
                ):
                    with self.assertRaises(
                        DiscordNotificationNotSentError if stage != "response" else TimeoutError
                    ):
                        notifications.post_discord_webhook(
                            "https://discord.com/api/webhooks/test/test", {"content": "test"}
                        )
                self.assertEqual(len(connection.requests), int(stage == "response"))
                if stage != "dns":
                    self.assertTrue(connection.closed)
