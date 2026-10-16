"""Exercise generated Nginx routes against a disposable, fictional bridge.

Requires an explicitly selected Nginx build, never an installed service or Docker
context. Nothing is installed. All listeners, keys and data belong to this run.
"""

import argparse
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import logging
import os
from pathlib import Path
import socket
import signal
import ssl
import subprocess
from tempfile import TemporaryDirectory
import threading
import time
from typing import Any, cast
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
import requests
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Request, Response
from websockets.sync.server import ServerConnection, serve

from control_plane.local_consumer import _verify_bridge, prepare_consumer
from control_plane.private_testing_transport import (
    PrivateTestingTransportIntent,
    nginx_configuration,
    nginx_entrypoint,
    transport_overlay,
)
from control_plane.workflows.public_ingress_monitor import fetch_private_health_url


def qualify(binary: Path, compose_binary: Path) -> None:
    with TemporaryDirectory(prefix="lp-private-tls-fixture-") as temporary:
        root = Path(temporary).resolve()
        ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Disposable fixture issuer")])
        now = datetime.now(timezone.utc)
        ca_certificate = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
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
        certificate = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Fixture bridge")]))
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(hours=1))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                critical=False,
            )
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(
                x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                critical=False,
            )
            .sign(ca_key, hashes.SHA256())
        )
        cert_file, key_file = root / "fixture.crt", root / "fixture.key"
        cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        ca_file = root / "fixture-ca.pem"
        ca_file.write_bytes(ca_certificate.public_bytes(serialization.Encoding.PEM))
        key_file.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        key_file.chmod(0o600)
        state = {"instance": "testing"}
        fake_value = "fictional-bridge-value-only"
        messages: list[str | bytes] = []

        def respond(status: int, payload: bytes = b"") -> Response:
            return Response(
                status,
                "fixture",
                Headers({"Content-Type": "application/json", "Content-Length": str(len(payload))}),
                payload,
            )

        def intercept(connection: ServerConnection, request: Request) -> Response | None:
            if request.path == "/health":
                return respond(
                    200,
                    json.dumps(
                        {
                            "status": "ok",
                            "runtime_identity": {
                                "product": "fixture",
                                "context": "fixture",
                                "instance": state["instance"],
                                "environment_kind": "stable",
                                "deployment_record_id": "fixture-deployment",
                                "artifact_id": "fixture-artifact",
                                "source_git_ref": "fixture-source",
                            },
                        }
                    ).encode(),
                )
            if request.headers.get("Authorization") != f"Bearer {fake_value}":
                return respond(401)
            return None

        def handler(connection: ServerConnection) -> None:
            messages.extend(connection)

        logger = logging.Logger("fixture")
        logger.disabled = True
        with serve(handler, "127.0.0.1", 0, process_request=intercept, logger=logger) as backend:
            thread = threading.Thread(target=backend.serve_forever)
            thread.start()
            try:
                with socket.socket() as reservation:
                    reservation.bind(("127.0.0.1", 0))
                    listener_port = reservation.getsockname()[1]
                # Relocate only mount paths, pid and listener port into this run's
                # namespace. Route directives are the production generator's output.
                config = nginx_configuration(
                    service="127.0.0.1",
                    runtime_port=backend.socket.getsockname()[1],
                    listen=f"127.0.0.1:{listener_port}",
                    pid_file=str(root / "nginx.pid"),
                    certificate_file=str(cert_file),
                    key_file=str(key_file),
                )
                intent = PrivateTestingTransportIntent(
                    context="fixture",
                    endpoint_key="fixture",
                    private_ip=ipaddress.IPv4Address("10.77.0.9"),
                    listener_port=9443,
                    runtime_service="bridge",
                    nginx_image="fixture@sha256:" + "a" * 64,
                    ca_bundle_sha256="b" * 64,
                    certificate_sha256="c" * 64,
                    monitor_ca_bundle_path=str(ca_file),
                    receiver_ca_bundle_path=str(ca_file),
                    trust_review="disposable isolated Compose qualification",
                )
                overlay = cast(
                    dict[str, Any],
                    transport_overlay(
                        intent,
                        namespace="fixture-tls",
                        runtime_port=backend.socket.getsockname()[1],
                    ),
                )
                overlay["services"]["bridge"] = {"image": intent.nginx_image}
                compose_file = root / "compose.json"
                compose_file.write_text(json.dumps(overlay))
                composed = subprocess.run(
                    [
                        str(compose_binary),
                        "--project-name",
                        "lp-private-tls-fixture",
                        "-f",
                        str(compose_file),
                        "config",
                        "--format",
                        "json",
                    ],
                    env={
                        "PATH": os.environ["PATH"],
                        "DOCKER_CONFIG": str(root / "docker-config"),
                        "COMPOSE_DISABLE_ENV_FILE": "true",
                    },
                    capture_output=True,
                    text=True,
                    check=True,
                )
                if composed.stderr:
                    raise AssertionError("Compose reported a warning for the actual overlay.")
                materialized = json.loads(composed.stdout)
                # Round-trip a real generated artifact, not a copied command literal.
                if (
                    materialized["services"]["fixture-tls"]["entrypoint"]
                    != overlay["services"]["fixture-tls"]["entrypoint"]
                ):
                    raise AssertionError("Compose altered the generated proxy invocation.")
                (root / "logs").mkdir()
                command = nginx_entrypoint(
                    config,
                    configuration_file=str(root / "nginx.conf"),
                    nginx_binary=str(binary),
                    nginx_arguments=("-p", str(root)),
                )
                checked = subprocess.run(
                    nginx_entrypoint(
                        config,
                        configuration_file=str(root / "nginx.conf"),
                        nginx_binary=str(binary),
                        nginx_arguments=("-p", str(root), "-t"),
                    ),
                    capture_output=True,
                )
                if checked.returncode:
                    raise RuntimeError(checked.stderr.decode())
                with (root / "nginx.log").open("wb") as log:
                    process = subprocess.Popen(
                        command, stdout=log, stderr=log, start_new_session=True
                    )
                    try:
                        deadline = time.monotonic() + 10
                        while True:
                            try:
                                with socket.create_connection(
                                    ("127.0.0.1", listener_port), timeout=1
                                ):
                                    break
                            except ConnectionRefusedError:
                                if process.poll() is not None or time.monotonic() >= deadline:
                                    raise RuntimeError("Fixture Nginx failed to start.")
                                threading.Event().wait(0.02)
                        origin = f"https://127.0.0.1:{listener_port}"
                        consumer = prepare_consumer(
                            directory=root / "consumer",
                            service_origin=origin,
                            product="fixture",
                            context="fixture",
                            endpoint_key="fixture-testing",
                            server_url=origin.replace("https:", "wss:") + "/agent-session/connect",
                        )
                        monitor = fetch_private_health_url(
                            origin + "/health", 5, tls_ca_pem=ca_file.read_text()
                        )
                        if monitor.status_code != 200:
                            raise AssertionError("Private monitor did not reach the runtime.")
                        with patch.dict(os.environ, {"SSL_CERT_FILE": str(ca_file)}):
                            _verify_bridge(consumer, fake_value)
                            try:
                                _verify_bridge(consumer, "wrong-fictional-value")
                            except InvalidStatus:
                                pass
                            else:
                                raise AssertionError("Wrong bridge value was accepted.")
                            state["instance"] = "prod"
                            try:
                                _verify_bridge(consumer, fake_value)
                            except ValueError:
                                pass
                            else:
                                raise AssertionError("Production identity was accepted.")
                            state["instance"] = "testing"
                            with requests.Session() as session:
                                session.trust_env = False
                                response = session.get(
                                    origin + "/unrelated", verify=str(ca_file), timeout=5
                                )
                                if response.status_code != 404:
                                    raise AssertionError("Unrelated route was accepted.")
                        for wrong_host, bundle in (
                            (False, requests.utils.DEFAULT_CA_BUNDLE_PATH),
                            (True, str(ca_file)),
                        ):
                            bad_origin = (
                                origin.replace("127.0.0.1", "localhost") if wrong_host else origin
                            )
                            bad_consumer = consumer.model_copy(
                                update={
                                    "server_url": bad_origin.replace("https:", "wss:")
                                    + "/agent-session/connect"
                                }
                            )
                            with patch.dict(os.environ, {"SSL_CERT_FILE": bundle}):
                                try:
                                    fetch_private_health_url(
                                        bad_origin + "/health",
                                        5,
                                        tls_ca_pem=Path(bundle).read_text(),
                                    )
                                except ssl.SSLError:
                                    pass
                                else:
                                    raise AssertionError(
                                        "Monitor accepted untrusted or wrong-host TLS."
                                    )
                                try:
                                    _verify_bridge(bad_consumer, fake_value)
                                except requests.exceptions.SSLError:
                                    pass
                                else:
                                    raise AssertionError(
                                        "Receiver accepted untrusted or wrong-host TLS."
                                    )
                        if messages:
                            raise AssertionError("Transport qualification sent bridge messages.")
                    finally:
                        process.terminate()
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            try:
                                os.killpg(process.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                            process.wait(timeout=10)
            finally:
                backend.shutdown()
                thread.join(timeout=5)
        print(
            "PASS: Compose overlay; generated entrypoint/Nginx; monitor HTTPS; receiver WSS; issuer/hostname/auth/lane/path negatives; no messages"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nginx-binary", required=True, type=Path)
    parser.add_argument("--compose-binary", required=True, type=Path)
    arguments = parser.parse_args()
    qualify(
        arguments.nginx_binary.resolve(strict=True), arguments.compose_binary.resolve(strict=True)
    )
