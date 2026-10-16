"""Actual isolated HTTPS proof of testing-endpoint trust without global trust changes."""

from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
from pathlib import Path
import os
import ssl
from tempfile import TemporaryDirectory
from threading import Thread
from typing import Any
import unittest
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from control_plane.contracts.private_health_endpoint_record import (
    PrivateHealthEndpointRecord,
    private_health_endpoint_record_sha256,
)
from control_plane.contracts.product_profile_record import (
    ProductLaneProfile,
    ProductLaneHealthMonitoringPolicy,
    ProductLaneHealthCheck,
)
from control_plane.workflows.public_ingress_monitor import (
    fetch_private_health_url,
    run_public_ingress_monitor_once,
)
from tests import test_public_ingress_monitor as monitor_fixture


def certificate_fixture() -> tuple[str, bytes, bytes]:
    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Disposable testing issuer")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(issuer)
        .issuer_name(issuer)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Fixture")]))
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    return (
        ca.public_bytes(serialization.Encoding.PEM).decode(),
        leaf.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )


class PrivateTestingTrustTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.ca, self.leaf, self.key = certificate_fixture()
        certificate = self.root / "fictional.crt"
        key = self.root / "fictional.key"
        certificate.write_bytes(self.leaf)
        key.write_bytes(self.key)
        key.chmod(0o600)

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/health")
                    self.end_headers()
                    return
                payload = b'{"status":"ok"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate, key)
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        thread = Thread(target=self.server.serve_forever, daemon=True)
        thread.start()

        def stop() -> None:
            self.server.shutdown()
            self.server.server_close()
            thread.join()

        self.addCleanup(stop)
        self.url = f"https://127.0.0.1:{self.server.server_port}/health"
        self.endpoint = PrivateHealthEndpointRecord(
            endpoint_key="fixture-testing",
            product="example-site",
            context="example-site",
            instance="testing",
            url=self.url,
            tls_ca_pem=self.ca,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )

    def test_actual_private_tls_scoped_chain_hostname_and_default_context_isolation(self) -> None:
        roots = ssl.create_default_context().get_ca_certs(binary_form=True)
        self.assertEqual(fetch_private_health_url(self.url, 3, tls_ca_pem=self.ca).status_code, 200)
        for url, ca in (
            (self.url, ""),
            (self.url.replace("127.0.0.1", "localhost"), self.ca),
            (self.url, certificate_fixture()[0]),
        ):
            with self.subTest(url=url), self.assertRaises(ssl.SSLError):
                fetch_private_health_url(url, 3, tls_ca_pem=ca)
        self.assertEqual(roots, ssl.create_default_context().get_ca_certs(binary_form=True))
        inherited = self.root / "inherited-ca.pem"
        inherited.write_text(self.ca)
        with (
            patch.dict(os.environ, {"SSL_CERT_FILE": str(inherited)}),
            self.assertRaises(ssl.SSLError),
        ):
            fetch_private_health_url(self.url, 3, tls_ca_pem=certificate_fixture()[0])
        with self.assertRaises(RuntimeError):
            fetch_private_health_url(
                self.url.removesuffix("/health") + "/redirect", 3, tls_ca_pem=self.ca
            )

    def test_monitor_uses_the_exact_endpoint_ca_and_refuses_after_its_trust_is_cleared(
        self,
    ) -> None:
        lane = ProductLaneProfile(
            instance="testing",
            context="example-site",
            health_monitoring=ProductLaneHealthMonitoringPolicy(
                monitoring_intent="private",
                checks=(
                    ProductLaneHealthCheck(
                        name="private-runtime",
                        kind="private_http",
                        private_endpoint_key=self.endpoint.endpoint_key,
                    ),
                ),
            ),
        )
        store = monitor_fixture._Store((monitor_fixture._profile(lane=lane),))
        store.private_health_endpoints[self.endpoint.endpoint_key] = self.endpoint
        first = run_public_ingress_monitor_once(
            record_store=store, checked_at=self.endpoint.updated_at, notify=False
        )
        self.assertEqual(first.pass_count, 1)
        old_digest = private_health_endpoint_record_sha256(self.endpoint)
        store.private_health_endpoints[self.endpoint.endpoint_key] = self.endpoint.model_copy(
            update={"tls_ca_pem": ""}
        )
        second = run_public_ingress_monitor_once(
            record_store=store, checked_at=self.endpoint.updated_at, notify=False
        )
        self.assertEqual(second.fail_count, 1)
        self.assertNotEqual(
            old_digest,
            private_health_endpoint_record_sha256(
                store.private_health_endpoints[self.endpoint.endpoint_key]
            ),
        )

    def test_public_material_only_and_production_scope_refuse(self) -> None:
        for changes in (
            {"instance": "prod"},
            {"url": self.url.replace("https:", "http:")},
            {"tls_ca_pem": self.key.decode()},
            {"tls_ca_pem": self.ca + self.key.decode()},
            {"tls_ca_pem": self.leaf.decode()},
        ):
            with self.subTest(changes=list(changes)), self.assertRaises(ValueError):
                PrivateHealthEndpointRecord.model_validate(
                    {**self.endpoint.model_dump(), **changes}
                )
