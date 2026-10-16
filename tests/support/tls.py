"""TLS server contexts for local tests using disposable certificates."""

from pathlib import Path
import ssl


def server_tls_context(certificate: Path, private_key: Path) -> ssl.SSLContext:
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certificate, private_key)
    return context
