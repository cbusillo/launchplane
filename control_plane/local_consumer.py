"""Owner-run local receiver. Real values stay inside this process and private files."""

import base64
import fcntl
import json
import logging
import os
import secrets
import socket
import stat
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from websockets.sync.client import connect

from control_plane.contracts.runtime_identity import runtime_identity_from_health_payload
from control_plane.local_consumer_contract import (
    VERIFY_ROUTE,
    ConsumerPackage,
    ConsumerRequest,
    ConsumerVerificationResponse,
    canonical,
    digest,
    https_origin,
)


def _private_file(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or info.st_size > 65536
        ):
            raise ValueError("A private owner-only regular file is required.")
        return os.read(descriptor, 65537)
    finally:
        os.close(descriptor)


def _write_once(path: Path, value: bytes) -> None:
    """An exact original retry can finish partial installation, never replace a file."""
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        if not secrets.compare_digest(_private_file(path), value):
            raise ValueError("The destination changed; preserve it and prepare another directory.")
        return
    # Preserve uncertain bytes for explicit recovery instead of overwriting.
    with os.fdopen(descriptor, "wb") as output:
        output.write(value)
        output.flush()
        os.fsync(output.fileno())


def _directory(path: Path) -> Path:
    path = path.expanduser().absolute()
    if path != path.resolve():
        raise ValueError("The testing directory must not traverse symlinks.")
    info = path.stat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise ValueError("An owner-only testing directory is required.")
    return path


@contextmanager
def _locked(directory: Path) -> Iterator[None]:
    path = directory / ".receiver.lock"
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        _private_file(path)
        descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def prepare_consumer(
    *,
    directory: Path,
    service_origin: str,
    product: str,
    context: str,
    endpoint_key: str,
    server_url: str,
) -> ConsumerRequest:
    destination = directory.expanduser().absolute()
    if destination.parent != destination.parent.resolve():
        raise ValueError("The destination parent must not traverse symlinks.")
    parent = destination.parent.stat()
    if parent.st_uid != os.getuid() or parent.st_mode & 0o022:
        raise ValueError("Choose a private, owner-controlled parent directory.")
    if destination.exists():
        raise FileExistsError(destination)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    request = ConsumerRequest(
        request_id=secrets.token_hex(32),
        product=product,
        context=context,
        service_origin=https_origin(service_origin),
        endpoint_key=endpoint_key,
        server_url=server_url,
        directory=str(destination),
        public_key=private_key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode(),
    )
    # Validate before creating anything. Exclusive creation preserves existing clients.
    destination.mkdir(mode=0o700)
    _write_once(
        destination / "recipient.pem",
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    _write_once(destination / "request.json", canonical(request))
    return request


def _verify_service(package: ConsumerPackage) -> None:
    with requests.Session() as session:
        session.trust_env = False
        response = session.post(
            package.request.service_origin + VERIFY_ROUTE,
            json=package.model_dump(mode="json"),
            timeout=15,
            allow_redirects=False,
            verify=os.environ.get("SSL_CERT_FILE") or True,
        )
        if response.status_code != 200 or len(response.content) > 4096:
            raise ValueError("The original service could not verify the delivery.")
        result = ConsumerVerificationResponse.model_validate(response.json())
        if result.package_digest != digest(package):
            raise ValueError("The service receipt does not match the original package.")


def _decrypt(package: ConsumerPackage, directory: Path) -> str:
    private_key = serialization.load_pem_private_key(
        _private_file(directory / "recipient.pem"), password=None
    )
    if not isinstance(private_key, rsa.RSAPrivateKey):
        raise ValueError("The private recipient is unavailable.")
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    if public_key.decode() != package.request.public_key:
        raise ValueError("The recipient no longer matches the reviewed destination.")
    key = private_key.decrypt(
        base64.b64decode(package.wrapped_key, validate=True),
        padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    return (
        AESGCM(key)
        .decrypt(
            base64.b64decode(package.nonce, validate=True),
            base64.b64decode(package.ciphertext, validate=True),
            canonical(package.plan),
        )
        .decode()
    )


def _verify_bridge(request: ConsumerRequest, token: str) -> None:
    health_url = (
        request.server_url.replace("wss://", "https://", 1).removesuffix("/agent-session/connect")
        + "/health"
    )
    with requests.Session() as session:
        session.trust_env = False
        response = session.get(
            health_url,
            timeout=15,
            allow_redirects=False,
            verify=os.environ.get("SSL_CERT_FILE") or True,
        )
        if response.status_code != 200 or len(response.content) > 65536:
            raise ValueError("The testing runtime is not ready.")
        payload = response.json()
        identity = runtime_identity_from_health_payload(payload)
        if (
            identity is None
            or (identity.product, identity.context, identity.instance)
            != (request.product, request.context, request.environment)
            or identity.environment_kind != "stable"
            or payload.get("status") != "ok"
        ):
            raise ValueError("The running bridge is not this product's stable testing lane.")
    # Authentication is checked before the upgrade. Closing without a hello
    # creates no mirrored session and sends no agent or Discord command.
    server = urlsplit(request.server_url)
    assert server.hostname is not None
    transport_logger = logging.Logger("launchplane.local-consumer.transport")
    transport_logger.disabled = True
    # A preconnected socket makes websockets refuse *all* redirects, including
    # same-origin redirects to another lane or an unauthenticated endpoint.
    with socket.create_connection((server.hostname, server.port or 443), timeout=15) as transport:
        with connect(
            request.server_url,
            sock=transport,
            additional_headers={"Authorization": f"Bearer {token}"},
            proxy=None,
            open_timeout=15,
            close_timeout=2,
            logger=transport_logger,
        ):
            pass


def receive_consumer(directory: Path, package_file: Path) -> dict[str, str]:
    directory = _directory(directory)
    request = ConsumerRequest.model_validate_json(_private_file(directory / "request.json"))
    if request.directory != str(directory):
        raise ValueError("The original directory changed.")
    # Downloads contain ciphertext only. Bound reads still prevent an accidental
    # unrelated/oversized input from becoming a memory or logging surface.
    with package_file.open("rb") as input_file:
        encoded = input_file.read(65537)
    if len(encoded) > 65536:
        raise ValueError("The encrypted delivery is too large.")
    package = ConsumerPackage.model_validate_json(encoded)
    if package.request != request or package.plan.request_fingerprint != digest(request):
        raise ValueError("The delivery does not match the original reviewed destination.")
    package.plan.require_fresh()
    with _locked(directory):
        _verify_service(package)
        token = _decrypt(package, directory)
        if not token or len(token.encode()) > 8192 or token != token.strip():
            raise ValueError("The delivered bridge credential has an unsupported format.")
        _verify_bridge(request, token)
        _verify_service(package)
        token_path = directory / "bridge.token"
        config_path = directory / "codex-bridge.toml"
        config = (
            f"server_url = {json.dumps(request.server_url)}\n"
            f"token_file = {json.dumps(str(token_path))}\n"
            'host_label = "Testing bridge"\n'
        ).encode()
        _write_once(token_path, token.encode())
        _write_once(config_path, config)
        _write_once(directory / "delivery.json", canonical(package))
    return {"version_id": package.plan.version_id, "config_file": str(config_path)}
