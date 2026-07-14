from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import socket
import ssl
import unittest
from unittest.mock import MagicMock, patch

from cryptography import x509

from control_plane import outbound_http
from control_plane.outbound_http import (
    AddressResolver,
    PublicHttpDestination,
    PublicHttpDestinationError,
    PublicHttpResponse,
    PublicTlsCertificate,
    ResolvedAddressInfo,
    probe_public_tls,
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

    def test_public_tls_probe_reports_valid_certificate(self) -> None:
        fetched_destinations: list[PublicHttpDestination] = []
        verified_destinations: list[PublicHttpDestination] = []

        def fetch_certificate(
            destination: PublicHttpDestination,
            _timeout: float,
            _now: datetime,
        ) -> tuple[PublicTlsCertificate, int]:
            fetched_destinations.append(destination)
            return (
                PublicTlsCertificate(
                    issuer="CN=Example Issuer",
                    subject="CN=public.example",
                    not_before="2026-07-01T00:00:00Z",
                    not_after="2026-08-30T00:00:00Z",
                    days_remaining=40,
                    public_name_match=True,
                    public_name_match_source="san",
                    presented_san_count=2,
                    presented_name_evidence=("public.example",),
                ),
                len(destination.addresses),
            )

        def verify_handshake(destination: PublicHttpDestination, _timeout: float) -> None:
            verified_destinations.append(destination)

        result = probe_public_tls(
            "public.example",
            timeout_seconds=5,
            resolver=_resolver_for(_PUBLIC_IPV4),
            fetch_certificate=fetch_certificate,
            verify_handshake=verify_handshake,
            now=lambda: datetime(2026, 7, 14, tzinfo=timezone.utc),
        )

        self.assertEqual(result.status, "valid")
        self.assertIsNone(result.failure_code)
        self.assertEqual(result.validated_address_count, 1)
        self.assertEqual(fetched_destinations[0].hostname, "public.example")
        self.assertEqual(verified_destinations[0].hostname, "public.example")

    def test_public_tls_probe_maps_hostname_mismatch_and_chain_failures(self) -> None:
        certificate = PublicTlsCertificate(
            issuer="CN=Issuer",
            subject="CN=other.example",
            not_before="2026-07-01T00:00:00Z",
            not_after="2026-07-21T00:00:00Z",
            days_remaining=10,
            public_name_match=False,
            public_name_match_source="none",
            presented_san_count=1,
            presented_name_evidence=(),
        )

        mismatch_result = probe_public_tls(
            "public.example",
            timeout_seconds=5,
            resolver=_resolver_for(_PUBLIC_IPV4),
            fetch_certificate=lambda destination, _timeout, _now: (
                certificate,
                len(destination.addresses),
            ),
            verify_handshake=lambda _destination, _timeout: (_ for _ in ()).throw(
                ssl.CertificateError("hostname mismatch")
            ),
            now=lambda: datetime(2026, 7, 14, tzinfo=timezone.utc),
        )
        chain_failure_result = probe_public_tls(
            "public.example",
            timeout_seconds=5,
            resolver=_resolver_for(_PUBLIC_IPV4),
            fetch_certificate=lambda destination, _timeout, _now: (
                certificate,
                len(destination.addresses),
            ),
            verify_handshake=lambda _destination, _timeout: (_ for _ in ()).throw(
                ssl.SSLCertVerificationError("unable to get local issuer certificate")
            ),
            now=lambda: datetime(2026, 7, 14, tzinfo=timezone.utc),
        )

        self.assertEqual(mismatch_result.status, "hostname_mismatch")
        self.assertEqual(mismatch_result.failure_code, "tls_hostname_mismatch")
        self.assertEqual(chain_failure_result.status, "untrusted")
        self.assertEqual(chain_failure_result.failure_code, "tls_chain_failure")

    def test_public_tls_certificate_does_not_fall_back_to_common_name_when_san_exists(
        self,
    ) -> None:
        certificate = MagicMock()
        certificate.not_valid_before_utc = datetime(2026, 7, 1, tzinfo=timezone.utc)
        certificate.not_valid_after_utc = datetime(2026, 8, 30, tzinfo=timezone.utc)
        certificate.issuer.rfc4514_string.return_value = "CN=Example Issuer"
        certificate.subject.rfc4514_string.return_value = "CN=public.example"
        certificate.extensions.get_extension_for_class.return_value.value.get_values_for_type.side_effect = (
            lambda name_type: ("other.example",) if name_type is x509.DNSName else ()
        )
        common_name = MagicMock()
        common_name.value = "public.example"
        certificate.subject.get_attributes_for_oid.return_value = (common_name,)

        with patch(
            "control_plane.outbound_http.x509.load_der_x509_certificate",
            return_value=certificate,
        ):
            parsed = outbound_http._parse_public_tls_certificate(
                b"certificate",
                public_name="public.example",
                now=datetime(2026, 7, 14, tzinfo=timezone.utc),
            )

        self.assertFalse(parsed.public_name_match)
        self.assertEqual(parsed.public_name_match_source, "none")
        self.assertEqual(parsed.presented_san_count, 1)
        self.assertEqual(parsed.presented_name_evidence, ())

    def test_public_tls_certificate_uses_common_name_only_without_dns_sans(self) -> None:
        certificate = MagicMock()
        certificate.not_valid_before_utc = datetime(2026, 7, 1, tzinfo=timezone.utc)
        certificate.not_valid_after_utc = datetime(2026, 8, 30, tzinfo=timezone.utc)
        certificate.issuer.rfc4514_string.return_value = "CN=Example Issuer"
        certificate.subject.rfc4514_string.return_value = "CN=public.example"
        common_name = MagicMock()
        common_name.value = "public.example"
        certificate.subject.get_attributes_for_oid.return_value = (common_name,)

        with (
            patch(
                "control_plane.outbound_http.x509.load_der_x509_certificate",
                return_value=certificate,
            ),
            patch("control_plane.outbound_http._certificate_san_names", return_value=((), ())),
        ):
            parsed = outbound_http._parse_public_tls_certificate(
                b"certificate",
                public_name="public.example",
                now=datetime(2026, 7, 14, tzinfo=timezone.utc),
            )

        self.assertTrue(parsed.public_name_match)
        self.assertEqual(parsed.public_name_match_source, "subject")
        self.assertEqual(parsed.presented_san_count, 0)
        self.assertEqual(parsed.presented_name_evidence, ("public.example",))

    def test_public_tls_certificate_reports_matching_ip_san(self) -> None:
        certificate = MagicMock()
        certificate.not_valid_before_utc = datetime(2026, 7, 1, tzinfo=timezone.utc)
        certificate.not_valid_after_utc = datetime(2026, 8, 30, tzinfo=timezone.utc)
        certificate.issuer.rfc4514_string.return_value = "CN=Example Issuer"
        certificate.subject.rfc4514_string.return_value = "CN=ignored.example"
        certificate.subject.get_attributes_for_oid.return_value = ()

        with (
            patch(
                "control_plane.outbound_http.x509.load_der_x509_certificate",
                return_value=certificate,
            ),
            patch(
                "control_plane.outbound_http._certificate_san_names",
                return_value=((), (_PUBLIC_IPV4,)),
            ),
        ):
            parsed = outbound_http._parse_public_tls_certificate(
                b"certificate",
                public_name=_PUBLIC_IPV4,
                now=datetime(2026, 7, 14, tzinfo=timezone.utc),
            )

        self.assertTrue(parsed.public_name_match)
        self.assertEqual(parsed.public_name_match_source, "san")
        self.assertEqual(parsed.presented_san_count, 1)
        self.assertEqual(parsed.presented_name_evidence, (_PUBLIC_IPV4,))

    def test_public_tls_probe_maps_expiring_expired_self_signed_timeout_and_unsupported(
        self,
    ) -> None:
        valid_certificate = PublicTlsCertificate(
            issuer="CN=Issuer",
            subject="CN=public.example",
            not_before="2026-07-01T00:00:00Z",
            not_after="2026-07-21T00:00:00Z",
            days_remaining=7,
            public_name_match=True,
            public_name_match_source="san",
            presented_san_count=1,
            presented_name_evidence=("public.example",),
        )
        expired_certificate = PublicTlsCertificate(
            issuer="CN=Issuer",
            subject="CN=public.example",
            not_before="2026-05-01T00:00:00Z",
            not_after="2026-06-01T00:00:00Z",
            days_remaining=-1,
            public_name_match=True,
            public_name_match_source="san",
            presented_san_count=1,
            presented_name_evidence=("public.example",),
        )

        expiring_result = probe_public_tls(
            "public.example",
            timeout_seconds=5,
            resolver=_resolver_for(_PUBLIC_IPV4),
            fetch_certificate=lambda destination, _timeout, _now: (
                valid_certificate,
                len(destination.addresses),
            ),
            verify_handshake=lambda _destination, _timeout: None,
            now=lambda: datetime(2026, 7, 14, tzinfo=timezone.utc),
            expiring_days=14,
        )
        expired_result = probe_public_tls(
            "public.example",
            timeout_seconds=5,
            resolver=_resolver_for(_PUBLIC_IPV4),
            fetch_certificate=lambda destination, _timeout, _now: (
                expired_certificate,
                len(destination.addresses),
            ),
            verify_handshake=lambda _destination, _timeout: (_ for _ in ()).throw(
                ssl.SSLCertVerificationError("certificate has expired")
            ),
            now=lambda: datetime(2026, 7, 14, tzinfo=timezone.utc),
        )
        self_signed_result = probe_public_tls(
            "public.example",
            timeout_seconds=5,
            resolver=_resolver_for(_PUBLIC_IPV4),
            fetch_certificate=lambda destination, _timeout, _now: (
                valid_certificate,
                len(destination.addresses),
            ),
            verify_handshake=lambda _destination, _timeout: (_ for _ in ()).throw(
                ssl.SSLCertVerificationError("self-signed certificate")
            ),
            now=lambda: datetime(2026, 7, 14, tzinfo=timezone.utc),
        )
        timeout_result = probe_public_tls(
            "public.example",
            timeout_seconds=5,
            resolver=_resolver_for(_PUBLIC_IPV4),
            fetch_certificate=lambda _destination, _timeout, _now: (_ for _ in ()).throw(
                TimeoutError("timed out")
            ),
            now=lambda: datetime(2026, 7, 14, tzinfo=timezone.utc),
        )
        unsupported_result = probe_public_tls(
            "public.example",
            timeout_seconds=5,
            resolver=_resolver_for(_PUBLIC_IPV4),
            fetch_certificate=lambda _destination, _timeout, _now: (_ for _ in ()).throw(
                ssl.SSLError("wrong version number")
            ),
            now=lambda: datetime(2026, 7, 14, tzinfo=timezone.utc),
        )

        self.assertEqual(expiring_result.status, "expiring")
        self.assertEqual(expiring_result.failure_code, "tls_expiring")
        self.assertEqual(expired_result.status, "expired")
        self.assertEqual(expired_result.failure_code, "tls_expired")
        self.assertEqual(self_signed_result.status, "self_signed")
        self.assertEqual(self_signed_result.failure_code, "tls_self_signed")
        self.assertEqual(timeout_result.status, "unreachable")
        self.assertEqual(timeout_result.failure_code, "connection_timeout")
        self.assertEqual(unsupported_result.status, "unsupported")
        self.assertEqual(unsupported_result.failure_code, "tls_unsupported")

    def test_public_tls_probe_maps_dns_and_private_destination_fail_closed(self) -> None:
        dns_failure = probe_public_tls(
            "public.example",
            timeout_seconds=5,
            resolver=lambda _hostname, _port: (_ for _ in ()).throw(socket.gaierror("dns failed")),
            now=lambda: datetime(2026, 7, 14, tzinfo=timezone.utc),
        )
        private_destination = probe_public_tls(
            "localhost",
            timeout_seconds=5,
            now=lambda: datetime(2026, 7, 14, tzinfo=timezone.utc),
        )

        self.assertEqual(dns_failure.status, "unreachable")
        self.assertEqual(dns_failure.failure_code, "dns_failure")
        self.assertEqual(private_destination.status, "unknown")
        self.assertEqual(private_destination.failure_code, "private_url")

    def test_public_tls_probe_treats_unexpected_eof_as_failure(self) -> None:
        result = probe_public_tls(
            "public.example",
            timeout_seconds=5,
            resolver=_resolver_for(_PUBLIC_IPV4),
            fetch_certificate=lambda _destination, _timeout, _now: (_ for _ in ()).throw(
                ssl.SSLError("unexpected eof while reading")
            ),
            now=lambda: datetime(2026, 7, 14, tzinfo=timezone.utc),
        )

        self.assertEqual(result.status, "unknown")
        self.assertEqual(result.failure_code, "tls_failure")

    def test_public_tls_probe_rechecks_expiry_after_verification(self) -> None:
        certificate = PublicTlsCertificate(
            issuer="CN=Issuer",
            subject="CN=public.example",
            not_before="2026-07-01T00:00:00Z",
            not_after="2026-07-14T12:00:00Z",
            days_remaining=0,
            public_name_match=True,
            public_name_match_source="san",
            presented_san_count=1,
            presented_name_evidence=("public.example",),
        )
        observed_times = iter(
            (
                datetime(2026, 7, 14, 11, 59, 59, tzinfo=timezone.utc),
                datetime(2026, 7, 14, 12, 0, 1, tzinfo=timezone.utc),
            )
        )

        result = probe_public_tls(
            "public.example",
            timeout_seconds=5,
            resolver=_resolver_for(_PUBLIC_IPV4),
            fetch_certificate=lambda destination, _timeout, _now: (
                certificate,
                len(destination.addresses),
            ),
            verify_handshake=lambda _destination, _timeout: (_ for _ in ()).throw(
                ssl.SSLCertVerificationError("certificate verify failed")
            ),
            now=lambda: next(observed_times),
        )

        self.assertEqual(result.status, "expired")
        self.assertEqual(result.failure_code, "tls_expired")
        assert result.certificate is not None
        self.assertEqual(result.certificate.days_remaining, -1)

    def test_public_tls_handshakes_apply_timeout_before_handshake(self) -> None:
        destination = resolve_public_http_destination(
            "https://public.example/",
            resolver=_resolver_for(_PUBLIC_IPV4),
        )
        connection_socket = MagicMock()
        connection_socket.fileno.return_value = -1
        tls_socket = MagicMock()
        tls_socket.getpeercert.return_value = b"certificate"
        tls_context = MagicMock()
        tls_context.wrap_socket.return_value.__enter__.return_value = tls_socket
        connect_timeouts: list[float] = []
        handshake_timeouts: list[float] = []
        events: list[str] = []

        def record_handshake_timeout(timeout: float) -> None:
            handshake_timeouts.append(timeout)
            events.append("timeout")

        def connect_to_validated_address(
            _address: tuple[str, int],
            timeout: float,
            _source: tuple[str, int] | None,
        ) -> MagicMock:
            connect_timeouts.append(timeout)
            return connection_socket

        tls_socket.settimeout.side_effect = record_handshake_timeout
        tls_socket.do_handshake.side_effect = lambda: events.append("handshake")
        parsed_certificate = PublicTlsCertificate(
            issuer="CN=Issuer",
            subject="CN=public.example",
            not_before="2026-07-01T00:00:00Z",
            not_after="2026-08-30T00:00:00Z",
            days_remaining=47,
            public_name_match=True,
            public_name_match_source="san",
            presented_san_count=1,
            presented_name_evidence=("public.example",),
        )

        with (
            patch(
                "control_plane.outbound_http._ValidatedAddressConnector",
                return_value=connect_to_validated_address,
            ),
            patch(
                "control_plane.outbound_http.ssl.create_default_context", return_value=tls_context
            ),
            patch(
                "control_plane.outbound_http.monotonic",
                side_effect=(100.0, 101.0, 104.0, 200.0, 201.0, 204.0),
            ),
            patch(
                "control_plane.outbound_http._parse_public_tls_certificate",
                return_value=parsed_certificate,
            ),
        ):
            outbound_http._fetch_public_tls_certificate(
                destination,
                5,
                datetime(2026, 7, 14, tzinfo=timezone.utc),
            )
            outbound_http._verify_public_tls_destination(destination, 5)

        self.assertEqual(events, ["timeout", "handshake", "timeout", "handshake"])
        self.assertEqual(connect_timeouts, [4.0, 4.0])
        self.assertEqual(handshake_timeouts, [1.0, 1.0])
        self.assertEqual(tls_context.minimum_version, ssl.TLSVersion.TLSv1_2)
        for call in tls_context.wrap_socket.call_args_list:
            self.assertFalse(call.kwargs["do_handshake_on_connect"])


if __name__ == "__main__":
    unittest.main()
