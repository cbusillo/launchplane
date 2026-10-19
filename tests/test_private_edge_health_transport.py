"""Health transport keeps a private socket and the original verified TLS name."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import socket
import ssl
import unittest
from unittest.mock import Mock, patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from control_plane import outbound_http
from control_plane.outbound_http import (
    PublicHttpDestination,
    PublicHttpDestinationError,
    ResolvedHttpAddress,
    request_private_edge_health,
)
from tests.support.tls import server_tls_context
from tests.test_outbound_http import _address_info, _resolver_for, _FakeConnection, _FakeResponse


class PrivateEdgeHealthTransportTests(unittest.TestCase):
    def test_endpoint_dns_never_selects_the_socket_and_no_credentials_are_sent(self) -> None:
        connection = _FakeConnection(_FakeResponse(status=200))
        resolve = Mock(return_value=(_address_info("10.77.0.9", 443),))
        destinations: list[PublicHttpDestination] = []

        def connect(destination: PublicHttpDestination, timeout: float) -> _FakeConnection:
            destinations.append(destination)
            return connection

        request_private_edge_health(
            "https://bridge.example.test/health",
            edge_api_url="http://edge.internal:81",
            timeout_seconds=3,
            resolver=resolve,
            connection_factory=connect,
        )
        resolve.assert_called_once_with("edge.internal", 443)
        self.assertEqual(destinations[0].hostname, "bridge.example.test")
        self.assertEqual(str(destinations[0].addresses[0].ip), "10.77.0.9")
        self.assertEqual(connection.requests, [("GET", "/health", None, {})])

    def test_public_rebound_or_mixed_edge_origin_is_refused_before_connect(self) -> None:
        for ips in (("93.184.216.34",), ("10.77.0.9", "93.184.216.34"), ("127.0.0.1",)):
            with self.subTest(ips=ips):
                connect = Mock()
                with self.assertRaises(PublicHttpDestinationError):
                    request_private_edge_health(
                        "https://bridge.example.test/health",
                        edge_api_url="http://edge.internal:81",
                        timeout_seconds=3,
                        resolver=_resolver_for(*ips),
                        connection_factory=connect,
                    )
                connect.assert_not_called()

    def test_non_health_paths_credentials_queries_and_redirects_are_refused(self) -> None:
        for url in (
            "https://bridge.example.test/agent-session/connect",
            "https://bridge.example.test/health?token=fixture",
            "https://bridge.example.test/health#fragment",
            "http://bridge.example.test/health",
            "https://bridge.example.test:8443/health",
            "https://user:password@bridge.example.test/health",
        ):
            with self.subTest(url=url), self.assertRaises((ValueError, PublicHttpDestinationError)):
                request_private_edge_health(
                    url, edge_api_url="http://10.77.0.9:81", timeout_seconds=3
                )
        connect = Mock(
            return_value=_FakeConnection(
                _FakeResponse(
                    status=302, headers=(("Location", "https://elsewhere.example.test/health"),)
                )
            )
        )
        with self.assertRaises(RuntimeError):
            request_private_edge_health(
                "https://bridge.example.test/health",
                edge_api_url="http://edge.internal:81",
                timeout_seconds=3,
                resolver=_resolver_for("10.77.0.9"),
                connection_factory=connect,
            )
        connect.assert_called_once()

    def test_real_tls_validates_name_chain_and_sends_only_health(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test edge")])
            now = datetime.now(timezone.utc)
            certificate = (
                x509.CertificateBuilder()
                .subject_name(name)
                .issuer_name(name)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=1))
                .not_valid_after(now + timedelta(hours=1))
                .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                .add_extension(
                    x509.SubjectAlternativeName([x509.DNSName("bridge.example.test")]),
                    critical=False,
                )
                .sign(key, hashes.SHA256())
            )
            certificate_file, key_file = root / "test-cert.pem", root / "test-key.pem"
            certificate_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
            key_file.write_bytes(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
            key_file.chmod(0o600)
            requests: list[tuple[str, dict[str, str]]] = []

            class Handler(BaseHTTPRequestHandler):
                def do_GET(self) -> None:
                    requests.append((self.path, dict(self.headers)))
                    body = b'{"status":"ok","runtime_identity":{"instance":"testing"}}'
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, format: str, *args: object) -> None:
                    pass

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            server.socket = server_tls_context(certificate_file, key_file).wrap_socket(
                server.socket, server_side=True
            )
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                trusted = ssl.create_default_context(cafile=str(certificate_file))
                for hostname, context, succeeds in (
                    ("bridge.example.test", trusted, True),
                    ("wrong.example.test", trusted, False),
                    ("bridge.example.test", None, False),
                ):
                    with self.subTest(hostname=hostname, trusted=context is not None):

                        def connect(
                            destination: PublicHttpDestination, timeout: float
                        ) -> outbound_http._HttpConnection:
                            # Only the network hop is mapped to an ephemeral local server.
                            # The policy resolves a private edge and TLS keeps the real name.
                            local = ResolvedHttpAddress(
                                socket.AF_INET,
                                socket.SOCK_STREAM,
                                socket.IPPROTO_TCP,
                                ("127.0.0.1", server.server_port),
                                ip_address("127.0.0.1"),
                            )
                            return outbound_http._private_http_connection(
                                replace(destination, addresses=(local,)), timeout, context
                            )

                        def fetch() -> outbound_http.PublicHttpResponse:
                            return request_private_edge_health(
                                f"https://{hostname}/health",
                                edge_api_url="http://edge.internal:81",
                                timeout_seconds=3,
                                resolver=_resolver_for("10.77.0.9"),
                                connection_factory=connect,
                            )

                        with patch.object(
                            socket,
                            "getaddrinfo",
                            side_effect=AssertionError("must not resolve after validation"),
                        ):
                            if succeeds:
                                self.assertEqual(fetch().status_code, 200)
                            else:
                                with self.assertRaises(ssl.SSLCertVerificationError):
                                    fetch()
                self.assertEqual(len(requests), 1)
                path, headers = requests[0]
                self.assertEqual(path, "/health")
                self.assertEqual(headers["Host"], "bridge.example.test")
                self.assertFalse(
                    {"authorization", "cookie", "proxy-authorization"}
                    & {key.lower() for key in headers}
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
