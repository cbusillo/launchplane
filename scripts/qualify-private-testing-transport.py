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
import subprocess
from tempfile import TemporaryDirectory
import threading
import time
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import requests
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Request, Response
from websockets.sync.server import ServerConnection, serve

from control_plane.local_consumer import _verify_bridge, prepare_consumer
from control_plane.private_testing_transport import nginx_configuration
from control_plane.workflows.public_ingress_monitor import fetch_private_health_url


def qualify(binary: Path) -> None:
    with TemporaryDirectory(prefix="lp-private-tls-fixture-") as temporary:
        root = Path(temporary).resolve()
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Disposable fixture issuer")])
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
                x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )
        cert_file, key_file = root / "fixture.crt", root / "fixture.key"
        cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
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
                    service="127.0.0.1", runtime_port=backend.socket.getsockname()[1]
                )
                config = config.replace("listen 8443 ssl", f"listen 127.0.0.1:{listener_port} ssl")
                config = config.replace("/tmp/nginx.pid", str(root / "nginx.pid"))
                config = config.replace("/run/secrets/private_tls_certificate", str(cert_file))
                config = config.replace("/run/secrets/private_tls_key", str(key_file))
                (root / "nginx.conf").write_text(config)
                (root / "logs").mkdir()
                command = [str(binary), "-p", str(root), "-c", str(root / "nginx.conf")]
                checked = subprocess.run([*command, "-t"], capture_output=True)
                if checked.returncode:
                    raise RuntimeError(checked.stderr.decode())
                with (root / "nginx.log").open("wb") as log:
                    process = subprocess.Popen(
                        [*command, "-g", "daemon off; master_process off;"], stdout=log, stderr=log
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
                        with patch.dict(os.environ, {"SSL_CERT_FILE": str(cert_file)}):
                            monitor = fetch_private_health_url(origin + "/health", 5)
                            assert monitor.status_code == 200
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
                            with requests.Session() as session:
                                session.trust_env = False
                                response = session.get(
                                    origin + "/unrelated", verify=str(cert_file), timeout=5
                                )
                                assert response.status_code == 404
                                for url, bundle in (
                                    (origin + "/health", requests.utils.DEFAULT_CA_BUNDLE_PATH),
                                    (
                                        origin.replace("127.0.0.1", "localhost") + "/health",
                                        str(cert_file),
                                    ),
                                ):
                                    try:
                                        session.get(url, verify=bundle, timeout=5)
                                    except requests.exceptions.SSLError:
                                        pass
                                    else:
                                        raise AssertionError(
                                            "Untrusted or wrong-host TLS was accepted."
                                        )
                        assert not messages
                    finally:
                        process.terminate()
                        process.wait(timeout=10)
            finally:
                backend.shutdown()
                thread.join(timeout=5)
        print(
            "PASS: generated Nginx; monitor HTTPS; receiver WSS; issuer/hostname/auth/lane/path negatives; no messages"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nginx-binary", required=True, type=Path)
    qualify(parser.parse_args().nginx_binary.resolve(strict=True))
