from __future__ import annotations

from collections.abc import Mapping
import socket
import unittest
from unittest.mock import MagicMock, patch

from control_plane import outbound_http
from control_plane.outbound_http import (
    AddressResolver,
    PublicHttpDestination,
    PublicHttpDestinationError,
    PublicHttpResponse,
    ResolvedAddressInfo,
    request_private_http,
    request_public_http,
    resolve_public_http_destination,
)
from control_plane.notifications import post_discord_webhook


_PUBLIC_IPV4 = "93.184.216.34"
_PUBLIC_IPV6 = "2606:4700:4700::1111"


def _address_info(ip: str, port: int) -> ResolvedAddressInfo:
    if ":" in ip:
        return socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port, 0, 0)
    return socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port)


def _resolver_for(*ips: str) -> AddressResolver:
    def resolve(_hostname: str, port: int) -> tuple[ResolvedAddressInfo, ...]:
        return tuple(_address_info(ip, port) for ip in ips)

    return resolve


class _FakeResponse:
    def __init__(
        self,
        *,
        status: int,
        headers: tuple[tuple[str, str], ...] = (),
        body: bytes = b"",
    ) -> None:
        self.status = status
        self.headers = headers
        self.body = body
        self.body_offset = 0
        self.closed = False

    def getheader(self, name: str, default: str | None = None) -> str | None:
        normalized_name = name.lower()
        return next(
            (value for key, value in self.headers if key.lower() == normalized_name),
            default,
        )

    def getheaders(self) -> list[tuple[str, str]]:
        return list(self.headers)

    def read(self, amount: int | None = None) -> bytes:
        return self.body if amount is None else self.body[:amount]

    def read1(self, amount: int = -1) -> bytes:
        if self.body_offset >= len(self.body):
            return b""
        if amount < 0:
            amount = len(self.body) - self.body_offset
        body_part = self.body[self.body_offset : self.body_offset + amount]
        self.body_offset += len(body_part)
        return body_part

    def close(self) -> None:
        self.closed = True


class _FakeConnection:
    sock: socket.socket | None = None

    def __init__(
        self,
        response: _FakeResponse,
        *,
        request_error: BaseException | None = None,
    ) -> None:
        self.response = response
        self.request_error = request_error
        self.requests: list[tuple[str, str, bytes | None, dict[str, str]]] = []
        self.closed = False

    def request(
        self,
        method: str,
        url: str,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        if self.request_error is not None:
            raise self.request_error
        self.requests.append((method, url, body, dict(headers or {})))

    def getresponse(self) -> _FakeResponse:
        return self.response

    def close(self) -> None:
        self.closed = True


class PublicOutboundHttpTests(unittest.TestCase):
    def test_normal_public_request_uses_validated_destination(self) -> None:
        connection = _FakeConnection(
            _FakeResponse(
                status=200,
                headers=(("Content-Type", "application/json"),),
                body=b'{"status":"ok"}',
            )
        )
        destinations: list[PublicHttpDestination] = []

        def connection_factory(
            destination: PublicHttpDestination, _timeout: float
        ) -> _FakeConnection:
            destinations.append(destination)
            return connection

        response = request_public_http(
            "https://public.example/health?full=1",
            headers={"User-Agent": "test"},
            timeout_seconds=5,
            resolver=_resolver_for(_PUBLIC_IPV4),
            connection_factory=connection_factory,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, b'{"status":"ok"}')
        self.assertEqual(destinations[0].hostname, "public.example")
        self.assertEqual(str(destinations[0].addresses[0].ip), _PUBLIC_IPV4)
        self.assertEqual(
            connection.requests,
            [("GET", "/health?full=1", None, {"User-Agent": "test"})],
        )

    def test_mixed_public_private_dns_answers_fail_closed(self) -> None:
        with self.assertRaises(PublicHttpDestinationError) as raised:
            resolve_public_http_destination(
                "https://mixed.example/health",
                resolver=_resolver_for(_PUBLIC_IPV4, "10.0.0.5"),
            )

        self.assertEqual(raised.exception.code, "private_url")

    def test_ipv6_public_answer_is_allowed_and_non_public_ranges_are_rejected(self) -> None:
        destination = resolve_public_http_destination(
            "https://ipv6.example/health",
            resolver=_resolver_for(_PUBLIC_IPV6),
        )
        self.assertEqual(str(destination.addresses[0].ip), _PUBLIC_IPV6)

        for address in ("::1", "fe80::1", "ff02::1", "2001:db8::1", "::"):
            with self.subTest(address=address):
                with self.assertRaises(PublicHttpDestinationError) as raised:
                    resolve_public_http_destination(
                        "https://ipv6.example/health",
                        resolver=_resolver_for(address),
                    )
                self.assertEqual(raised.exception.code, "private_url")

    def test_redirect_to_private_literal_is_rejected_before_second_connection(self) -> None:
        connections: list[_FakeConnection] = []

        def connection_factory(
            _destination: PublicHttpDestination, _timeout: float
        ) -> _FakeConnection:
            connection = _FakeConnection(
                _FakeResponse(
                    status=302,
                    headers=(("Location", "http://127.0.0.1/admin"),),
                )
            )
            connections.append(connection)
            return connection

        with self.assertRaises(PublicHttpDestinationError) as raised:
            request_public_http(
                "https://public.example/health",
                timeout_seconds=5,
                resolver=_resolver_for(_PUBLIC_IPV4),
                connection_factory=connection_factory,
            )

        self.assertEqual(raised.exception.code, "private_url")
        self.assertEqual(len(connections), 1)

    def test_public_redirect_revalidates_next_hop_and_reports_final_url(self) -> None:
        resolved_hostnames: list[str] = []

        def resolver(hostname: str, port: int) -> tuple[ResolvedAddressInfo, ...]:
            resolved_hostnames.append(hostname)
            return (_address_info(_PUBLIC_IPV4, port),)

        def connection_factory(
            destination: PublicHttpDestination, _timeout: float
        ) -> _FakeConnection:
            if destination.hostname == "public.example":
                return _FakeConnection(
                    _FakeResponse(
                        status=302,
                        headers=(("Location", "https://next.example/ready"),),
                    )
                )
            return _FakeConnection(_FakeResponse(status=200, body=b"ready"))

        response = request_public_http(
            "https://public.example/health",
            timeout_seconds=5,
            resolver=resolver,
            connection_factory=connection_factory,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.final_url, "https://next.example/ready")
        self.assertEqual(response.redirect_count, 1)
        self.assertEqual(resolved_hostnames, ["public.example", "next.example"])

    def test_each_redirect_hostname_is_resolved_and_revalidated(self) -> None:
        resolved_hostnames: list[str] = []

        def resolver(hostname: str, port: int) -> tuple[ResolvedAddressInfo, ...]:
            resolved_hostnames.append(hostname)
            address = _PUBLIC_IPV4 if hostname == "public.example" else "10.0.0.5"
            return (_address_info(address, port),)

        def connection_factory(
            _destination: PublicHttpDestination, _timeout: float
        ) -> _FakeConnection:
            return _FakeConnection(
                _FakeResponse(
                    status=302,
                    headers=(("Location", "https://rebound.example/secret"),),
                )
            )

        with self.assertRaises(PublicHttpDestinationError) as raised:
            request_public_http(
                "https://public.example/health",
                timeout_seconds=5,
                resolver=resolver,
                connection_factory=connection_factory,
            )

        self.assertEqual(raised.exception.code, "private_url")
        self.assertEqual(resolved_hostnames, ["public.example", "rebound.example"])

    def test_dns_rebinding_cannot_replace_validated_address_at_connect(self) -> None:
        destination = resolve_public_http_destination(
            "http://public.example/health",
            resolver=_resolver_for(_PUBLIC_IPV4),
        )
        connector = outbound_http._ValidatedAddressConnector(destination.addresses)
        connection_socket = MagicMock(spec=socket.socket)

        with (
            patch("control_plane.outbound_http.socket.socket", return_value=connection_socket),
            patch(
                "control_plane.outbound_http.socket.getaddrinfo",
                side_effect=AssertionError("connector must not re-resolve DNS"),
            ),
        ):
            connected_socket = connector((destination.hostname, destination.port), 5.0, None)

        self.assertIs(connected_socket, connection_socket)
        connection_socket.connect.assert_called_once_with((_PUBLIC_IPV4, 80))

    def test_request_timeout_is_propagated_and_connection_is_closed(self) -> None:
        connection = _FakeConnection(
            _FakeResponse(status=200),
            request_error=TimeoutError("timed out"),
        )

        with self.assertRaises(TimeoutError):
            request_public_http(
                "https://public.example/health",
                timeout_seconds=0.1,
                resolver=_resolver_for(_PUBLIC_IPV4),
                connection_factory=lambda _destination, _timeout: connection,
            )

        self.assertTrue(connection.closed)

    def test_response_body_read_enforces_absolute_deadline(self) -> None:
        connection = _FakeConnection(_FakeResponse(status=200, body=b"slow"))

        with (
            patch("control_plane.outbound_http.monotonic", side_effect=(0.0, 0.0, 0.9, 1.1)),
            self.assertRaises(TimeoutError),
        ):
            request_public_http(
                "https://public.example/health",
                timeout_seconds=1,
                resolver=_resolver_for(_PUBLIC_IPV4),
                connection_factory=lambda _destination, _timeout: connection,
            )

        self.assertTrue(connection.closed)

    def test_private_response_body_uses_same_absolute_deadline(self) -> None:
        connection = _FakeConnection(_FakeResponse(status=200, body=b"slow"))

        with (
            patch("control_plane.outbound_http.monotonic", side_effect=(0.0, 0.0, 0.9, 1.1)),
            self.assertRaises(TimeoutError),
        ):
            request_private_http(
                "http://10.0.0.5/health",
                timeout_seconds=1,
                connection_factory=lambda _destination, _timeout: connection,
            )

        self.assertTrue(connection.closed)

    def test_private_redirect_uses_bounded_client_for_each_hop(self) -> None:
        requested_hosts: list[str] = []

        def connection_factory(
            destination: PublicHttpDestination, _timeout: float
        ) -> _FakeConnection:
            requested_hosts.append(destination.hostname)
            if destination.hostname == "10.0.0.5":
                return _FakeConnection(
                    _FakeResponse(
                        status=302,
                        headers=(("Location", "http://10.0.0.6/ready"),),
                    )
                )
            return _FakeConnection(_FakeResponse(status=200, body=b"ready"))

        response = request_private_http(
            "http://10.0.0.5/health",
            timeout_seconds=5,
            connection_factory=connection_factory,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.redirect_count, 1)
        self.assertEqual(response.final_url, "http://10.0.0.6/ready")
        self.assertEqual(requested_hosts, ["10.0.0.5", "10.0.0.6"])

    def test_discord_webhook_uses_public_http_policy_client(self) -> None:
        with patch(
            "control_plane.notifications.request_public_http",
            return_value=PublicHttpResponse(
                status_code=204,
                final_url="https://discord.com/api/webhooks/test/webhook",
                redirect_count=0,
                headers=(),
                body=b"",
            ),
        ) as request:
            post_discord_webhook(
                "https://discord.com/api/webhooks/test/webhook",
                {"content": "hello"},
            )

        request.assert_called_once()
        self.assertEqual(request.call_args.kwargs["method"], "POST")
        self.assertIn(b'"content": "hello"', request.call_args.kwargs["body"])

    def test_discord_webhook_rejects_non_discord_destination_before_request(self) -> None:
        with patch("control_plane.notifications.request_public_http") as request:
            with self.assertRaises(ValueError):
                post_discord_webhook("https://example.com/hook", {"content": "hello"})

        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
