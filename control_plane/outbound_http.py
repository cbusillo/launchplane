from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from http.client import HTTPConnection, HTTPSConnection
from ipaddress import IPv4Address, IPv6Address, ip_address
from time import monotonic
from typing import Literal, Protocol, cast
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit
import socket
import ssl

from cryptography import x509
from cryptography.x509.oid import NameOID


PublicUrlError = Literal["invalid_url", "private_url"]
PublicHttpDestinationErrorCode = Literal["dns_failure", "invalid_url", "private_url"]
SocketAddress = tuple[str, int] | tuple[str, int, int, int]
ResolvedAddressInfo = tuple[int, int, int, str, SocketAddress]
AddressResolver = Callable[[str, int], tuple[ResolvedAddressInfo, ...]]
PublicTlsNameMatchSource = Literal["san", "subject", "none"]
PublicTlsProbeStatus = Literal[
    "valid",
    "expiring",
    "expired",
    "hostname_mismatch",
    "untrusted",
    "self_signed",
    "unreachable",
    "unknown",
    "unsupported",
]
PublicTlsFailureCode = Literal[
    "connection_timeout",
    "dns_failure",
    "invalid_url",
    "private_url",
    "tls_chain_failure",
    "tls_expired",
    "tls_expiring",
    "tls_failure",
    "tls_hostname_mismatch",
    "tls_self_signed",
    "tls_unsupported",
    "unknown_error",
]
NowProvider = Callable[[], datetime]


@dataclass(frozen=True)
class ResolvedHttpAddress:
    family: int
    socket_type: int
    protocol: int
    socket_address: SocketAddress
    ip: IPv4Address | IPv6Address


@dataclass(frozen=True)
class PublicHttpDestination:
    url: str
    scheme: Literal["http", "https"]
    hostname: str
    port: int
    request_target: str
    addresses: tuple[ResolvedHttpAddress, ...]

    @property
    def origin(self) -> tuple[str, str, int]:
        return self.scheme, self.hostname, self.port


@dataclass(frozen=True)
class PublicHttpResponse:
    status_code: int
    final_url: str
    redirect_count: int
    headers: tuple[tuple[str, str], ...]
    body: bytes

    def header(self, name: str) -> str:
        normalized_name = name.lower()
        return next(
            (value for key, value in self.headers if key.lower() == normalized_name),
            "",
        )


class PublicHttpDestinationError(ValueError):
    def __init__(self, code: PublicHttpDestinationErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: PublicHttpDestinationErrorCode = code


class _HttpResponse(Protocol):
    status: int

    def getheader(self, name: str, default: str | None = None) -> str | None: ...

    def getheaders(self) -> list[tuple[str, str]]: ...

    def read(self, amount: int | None = None) -> bytes: ...

    def read1(self, amount: int = -1) -> bytes: ...

    def close(self) -> None: ...


class _HttpConnection(Protocol):
    sock: socket.socket | None

    def request(
        self,
        method: str,
        url: str,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None: ...

    def getresponse(self) -> _HttpResponse: ...

    def close(self) -> None: ...


PublicHttpConnectionFactory = Callable[[PublicHttpDestination, float], _HttpConnection]


@dataclass(frozen=True)
class PublicTlsCertificate:
    issuer: str
    subject: str
    not_before: str
    not_after: str
    days_remaining: int
    public_name_match: bool
    public_name_match_source: PublicTlsNameMatchSource
    presented_san_count: int
    presented_name_evidence: tuple[str, ...]


@dataclass(frozen=True)
class PublicTlsProbeResult:
    status: PublicTlsProbeStatus
    failure_code: PublicTlsFailureCode | None
    summary: str
    validated_address_count: int = 0
    certificate: PublicTlsCertificate | None = None


FetchPublicTlsCertificate = Callable[
    [PublicHttpDestination, float, datetime],
    tuple[PublicTlsCertificate, int],
]
VerifyPublicTlsHandshake = Callable[[PublicHttpDestination, float], None]


def public_url_error(url: str) -> PublicUrlError | Literal[""]:
    try:
        parsed, hostname, _port = _parse_http_url(url)
    except ValueError:
        return "invalid_url"
    if parsed.username is not None or parsed.password is not None:
        return "invalid_url"
    if _private_hostname(hostname):
        return "private_url"
    try:
        address = ip_address(hostname)
    except ValueError:
        return ""
    if not _is_public_address(address):
        return "private_url"
    return ""


def resolve_public_http_destination(
    url: str,
    *,
    resolver: AddressResolver | None = None,
) -> PublicHttpDestination:
    structural_error = public_url_error(url)
    if structural_error:
        raise PublicHttpDestinationError(
            structural_error,
            f"Public HTTP destination is not allowed: {structural_error}.",
        )
    parsed, hostname, port = _parse_http_url(url)
    resolve = resolver or _system_address_resolver
    try:
        address_info = resolve(hostname, port)
    except socket.gaierror as error:
        raise PublicHttpDestinationError(
            "dns_failure",
            f"Public HTTP destination DNS resolution failed for {hostname}.",
        ) from error
    if not address_info:
        raise PublicHttpDestinationError(
            "dns_failure",
            f"Public HTTP destination DNS resolution returned no addresses for {hostname}.",
        )

    resolved_addresses: list[ResolvedHttpAddress] = []
    seen_addresses: set[tuple[int, int, int, SocketAddress]] = set()
    for family, socket_type, protocol, _canonical_name, socket_address in address_info:
        if family not in {socket.AF_INET, socket.AF_INET6}:
            raise PublicHttpDestinationError(
                "dns_failure",
                f"Public HTTP destination resolved to unsupported address family {family}.",
            )
        if socket_type != socket.SOCK_STREAM:
            raise PublicHttpDestinationError(
                "dns_failure",
                "Public HTTP destination resolved to an unsupported socket type.",
            )
        try:
            resolved_ip = ip_address(socket_address[0].split("%", 1)[0])
        except ValueError as error:
            raise PublicHttpDestinationError(
                "dns_failure",
                "Public HTTP destination resolved to an invalid IP address.",
            ) from error
        if not _is_public_address(resolved_ip):
            raise PublicHttpDestinationError(
                "private_url",
                (
                    "Public HTTP destination DNS answers include a non-public address; "
                    "mixed public/private answer sets are rejected."
                ),
            )
        address_key = (family, socket_type, protocol, socket_address)
        if address_key in seen_addresses:
            continue
        seen_addresses.add(address_key)
        resolved_addresses.append(
            ResolvedHttpAddress(
                family=family,
                socket_type=socket_type,
                protocol=protocol,
                socket_address=socket_address,
                ip=resolved_ip,
            )
        )
    if not resolved_addresses:
        raise PublicHttpDestinationError(
            "dns_failure",
            f"Public HTTP destination DNS resolution returned no usable addresses for {hostname}.",
        )

    scheme = cast(Literal["http", "https"], parsed.scheme.lower())
    path = parsed.path or "/"
    return PublicHttpDestination(
        url=urlunsplit((scheme, parsed.netloc, path, parsed.query, "")),
        scheme=scheme,
        hostname=hostname,
        port=port,
        request_target=urlunsplit(("", "", path, parsed.query, "")),
        addresses=tuple(resolved_addresses),
    )


def request_public_http(
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    body: bytes | None = None,
    timeout_seconds: float,
    max_redirects: int = 10,
    max_response_body_bytes: int = 256 * 1024,
    resolver: AddressResolver | None = None,
    connection_factory: PublicHttpConnectionFactory | None = None,
) -> PublicHttpResponse:
    if timeout_seconds <= 0:
        raise ValueError("Public HTTP timeout must be positive.")
    if max_redirects < 0:
        raise ValueError("Public HTTP redirect limit must not be negative.")
    if max_response_body_bytes < 0:
        raise ValueError("Public HTTP response body limit must not be negative.")

    deadline = monotonic() + timeout_seconds
    current_url = url
    current_method = method.strip().upper() or "GET"
    current_body = body
    current_headers = _safe_request_headers(headers or {})
    redirect_count = 0
    open_connection = connection_factory or _public_http_connection

    while True:
        destination = resolve_public_http_destination(current_url, resolver=resolver)
        connection = open_connection(destination, _remaining_seconds(deadline))
        response: _HttpResponse | None = None
        try:
            connection.request(
                current_method,
                destination.request_target,
                body=current_body,
                headers=current_headers,
            )
            response = connection.getresponse()
            response_headers = tuple(response.getheaders())
            location = response.getheader("Location", "") or ""
            if response.status in {301, 302, 303, 307, 308} and location:
                response_body = b""
            else:
                response_body = _read_response_body(
                    response=response,
                    connection=connection,
                    deadline=deadline,
                    max_response_body_bytes=max_response_body_bytes,
                )
                if len(response_body) > max_response_body_bytes:
                    response_body = response_body[:max_response_body_bytes]
            status_code = response.status
        finally:
            if response is not None:
                response.close()
            connection.close()

        if status_code not in {301, 302, 303, 307, 308} or not location:
            return PublicHttpResponse(
                status_code=status_code,
                final_url=destination.url,
                redirect_count=redirect_count,
                headers=response_headers,
                body=response_body,
            )
        if redirect_count >= max_redirects:
            raise RuntimeError(f"redirect loop after {max_redirects} redirects")

        next_url = urljoin(destination.url, location)
        try:
            next_structural_destination = _parse_http_url(next_url)
        except ValueError as error:
            raise PublicHttpDestinationError(
                "invalid_url",
                "Public HTTP redirect destination is not a valid HTTP URL.",
            ) from error
        if _normalized_url(destination.url) == _normalized_url(next_url):
            raise RuntimeError(f"self redirect from {destination.url}")
        next_origin = (
            next_structural_destination[0].scheme.lower(),
            next_structural_destination[1],
            next_structural_destination[2],
        )
        if destination.origin != next_origin:
            current_headers = _without_sensitive_headers(current_headers)
        if status_code == 303 or (
            status_code in {301, 302} and current_method not in {"GET", "HEAD"}
        ):
            current_method = "GET"
            current_body = None
            current_headers = _without_entity_headers(current_headers)
        current_url = next_url
        redirect_count += 1


def request_private_http(
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    body: bytes | None = None,
    timeout_seconds: float,
    max_redirects: int = 10,
    max_response_body_bytes: int = 256 * 1024,
    connection_factory: PublicHttpConnectionFactory | None = None,
) -> PublicHttpResponse:
    if timeout_seconds <= 0:
        raise ValueError("Private HTTP timeout must be positive.")
    if max_redirects < 0:
        raise ValueError("Private HTTP redirect limit must not be negative.")
    if max_response_body_bytes < 0:
        raise ValueError("Private HTTP response body limit must not be negative.")
    deadline = monotonic() + timeout_seconds
    open_connection = connection_factory or _private_http_connection
    current_url = url
    current_method = method.strip().upper() or "GET"
    current_body = body
    current_headers = _safe_request_headers(headers or {})
    redirect_count = 0

    while True:
        parsed, hostname, port = _parse_http_url(current_url)
        scheme = cast(Literal["http", "https"], parsed.scheme.lower())
        path = parsed.path or "/"
        destination = PublicHttpDestination(
            url=urlunsplit((scheme, parsed.netloc, path, parsed.query, "")),
            scheme=scheme,
            hostname=hostname,
            port=port,
            request_target=urlunsplit(("", "", path, parsed.query, "")),
            addresses=(),
        )
        connection = open_connection(destination, _remaining_seconds(deadline))
        response: _HttpResponse | None = None
        try:
            connection.request(
                current_method,
                destination.request_target,
                body=current_body,
                headers=current_headers,
            )
            response = connection.getresponse()
            response_headers = tuple(response.getheaders())
            location = response.getheader("Location", "") or ""
            if response.status in {301, 302, 303, 307, 308} and location:
                response_body = b""
            else:
                response_body = _read_response_body(
                    response=response,
                    connection=connection,
                    deadline=deadline,
                    max_response_body_bytes=max_response_body_bytes,
                )
                if len(response_body) > max_response_body_bytes:
                    response_body = response_body[:max_response_body_bytes]
            status_code = response.status
        finally:
            if response is not None:
                response.close()
            connection.close()

        if status_code not in {301, 302, 303, 307, 308} or not location:
            return PublicHttpResponse(
                status_code=status_code,
                final_url=destination.url,
                redirect_count=redirect_count,
                headers=response_headers,
                body=response_body,
            )
        if redirect_count >= max_redirects:
            raise RuntimeError(f"redirect loop after {max_redirects} redirects")
        next_url = urljoin(destination.url, location)
        next_parsed, next_hostname, next_port = _parse_http_url(next_url)
        if _normalized_url(destination.url) == _normalized_url(next_url):
            raise RuntimeError(f"self redirect from {destination.url}")
        next_origin = (next_parsed.scheme.lower(), next_hostname, next_port)
        if destination.origin != next_origin:
            current_headers = _without_sensitive_headers(current_headers)
        if status_code == 303 or (
            status_code in {301, 302} and current_method not in {"GET", "HEAD"}
        ):
            current_method = "GET"
            current_body = None
            current_headers = _without_entity_headers(current_headers)
        current_url = next_url
        redirect_count += 1


def probe_public_tls(
    public_name: str,
    *,
    timeout_seconds: float,
    resolver: AddressResolver | None = None,
    fetch_certificate: FetchPublicTlsCertificate | None = None,
    verify_handshake: VerifyPublicTlsHandshake | None = None,
    now: NowProvider | None = None,
    expiring_days: int = 14,
) -> PublicTlsProbeResult:
    if timeout_seconds <= 0:
        raise ValueError("Public TLS timeout must be positive.")
    if expiring_days < 0:
        raise ValueError("Public TLS expiring_days must not be negative.")
    now_provider = now or _utc_now
    observed_now = _normalized_utc_time(now_provider())

    try:
        destination = resolve_public_http_destination(
            f"https://{public_name.strip()}/",
            resolver=resolver,
        )
    except PublicHttpDestinationError as error:
        if error.code == "dns_failure":
            return PublicTlsProbeResult(
                status="unreachable",
                failure_code="dns_failure",
                summary=_bounded_tls_summary(str(error)),
            )
        if error.code in {"private_url", "invalid_url"}:
            return PublicTlsProbeResult(
                status="unknown",
                failure_code=cast(PublicTlsFailureCode, error.code),
                summary=_bounded_tls_summary(str(error)),
            )
        return PublicTlsProbeResult(
            status="unknown",
            failure_code="unknown_error",
            summary=_bounded_tls_summary(str(error)),
        )

    deadline = monotonic() + timeout_seconds
    fetch = fetch_certificate or _fetch_public_tls_certificate
    verify = verify_handshake or _verify_public_tls_destination
    certificate: PublicTlsCertificate | None = None
    validated_address_count = len(destination.addresses)
    try:
        certificate, validated_address_count = fetch(
            destination,
            _remaining_seconds(deadline),
            observed_now,
        )
    except TimeoutError:
        return PublicTlsProbeResult(
            status="unreachable",
            failure_code="connection_timeout",
            summary="Public TLS probe timed out.",
            validated_address_count=validated_address_count,
        )
    except ssl.SSLError as error:
        detail = _bounded_tls_error_summary(error)
        if _tls_protocol_is_unsupported(detail):
            return PublicTlsProbeResult(
                status="unsupported",
                failure_code="tls_unsupported",
                summary=detail,
                validated_address_count=validated_address_count,
            )
        return PublicTlsProbeResult(
            status="unknown",
            failure_code="tls_failure",
            summary=detail,
            validated_address_count=validated_address_count,
        )
    except OSError as error:
        detail = _bounded_tls_error_summary(error)
        return PublicTlsProbeResult(
            status="unreachable",
            failure_code="tls_failure",
            summary=detail,
            validated_address_count=validated_address_count,
        )
    except Exception as error:  # noqa: BLE001 - malformed peer evidence becomes probe evidence.
        return PublicTlsProbeResult(
            status="unknown",
            failure_code="tls_failure",
            summary=_bounded_tls_error_summary(error),
            validated_address_count=validated_address_count,
        )

    try:
        verify(destination, _remaining_seconds(deadline))
    except TimeoutError:
        return PublicTlsProbeResult(
            status="unreachable",
            failure_code="connection_timeout",
            summary="Public TLS verification timed out.",
            validated_address_count=validated_address_count,
            certificate=certificate,
        )
    except ssl.SSLCertVerificationError as error:
        certificate = _refresh_public_tls_certificate(
            certificate,
            now=_normalized_utc_time(now_provider()),
        )
        verification_message = _bounded_tls_error_summary(error)
        verification_message_lower = verification_message.lower()
        verification_code = getattr(error, "verify_code", None)
        if verification_code == 10 or (certificate is not None and certificate.days_remaining < 0):
            return PublicTlsProbeResult(
                status="expired",
                failure_code="tls_expired",
                summary=verification_message,
                validated_address_count=validated_address_count,
                certificate=certificate,
            )
        if verification_code == 62 or (
            "hostname" in verification_message_lower
            or "doesn't match" in verification_message_lower
        ):
            return PublicTlsProbeResult(
                status="hostname_mismatch",
                failure_code="tls_hostname_mismatch",
                summary=verification_message,
                validated_address_count=validated_address_count,
                certificate=certificate,
            )
        if verification_code in {18, 19} or (
            "self-signed" in verification_message_lower
            or "self signed" in verification_message_lower
        ):
            return PublicTlsProbeResult(
                status="self_signed",
                failure_code="tls_self_signed",
                summary=verification_message,
                validated_address_count=validated_address_count,
                certificate=certificate,
            )
        return PublicTlsProbeResult(
            status="untrusted",
            failure_code="tls_chain_failure",
            summary=verification_message,
            validated_address_count=validated_address_count,
            certificate=certificate,
        )
    except ssl.CertificateError as error:
        return PublicTlsProbeResult(
            status="hostname_mismatch",
            failure_code="tls_hostname_mismatch",
            summary=_bounded_tls_error_summary(error),
            validated_address_count=validated_address_count,
            certificate=certificate,
        )
    except ssl.SSLError as error:
        detail = _bounded_tls_error_summary(error)
        if _tls_protocol_is_unsupported(detail):
            return PublicTlsProbeResult(
                status="unsupported",
                failure_code="tls_unsupported",
                summary=detail,
                validated_address_count=validated_address_count,
                certificate=certificate,
            )
        return PublicTlsProbeResult(
            status="unknown",
            failure_code="tls_failure",
            summary=detail,
            validated_address_count=validated_address_count,
            certificate=certificate,
        )
    except OSError as error:
        detail = _bounded_tls_error_summary(error)
        return PublicTlsProbeResult(
            status="unreachable",
            failure_code="tls_failure",
            summary=detail,
            validated_address_count=validated_address_count,
            certificate=certificate,
        )
    except Exception as error:  # noqa: BLE001 - unexpected verifier failures become probe evidence.
        return PublicTlsProbeResult(
            status="unknown",
            failure_code="tls_failure",
            summary=_bounded_tls_error_summary(error),
            validated_address_count=validated_address_count,
            certificate=certificate,
        )

    certificate = _refresh_public_tls_certificate(
        certificate,
        now=_normalized_utc_time(now_provider()),
    )
    assert certificate is not None
    if certificate.days_remaining < 0:
        return PublicTlsProbeResult(
            status="expired",
            failure_code="tls_expired",
            summary="TLS certificate is expired.",
            validated_address_count=validated_address_count,
            certificate=certificate,
        )
    if certificate.days_remaining <= expiring_days:
        return PublicTlsProbeResult(
            status="expiring",
            failure_code="tls_expiring",
            summary=(f"TLS certificate expires within {certificate.days_remaining} day(s)."),
            validated_address_count=validated_address_count,
            certificate=certificate,
        )
    return PublicTlsProbeResult(
        status="valid",
        failure_code=None,
        summary="TLS certificate is valid.",
        validated_address_count=validated_address_count,
        certificate=certificate,
    )


def _read_response_body(
    *,
    response: _HttpResponse,
    connection: _HttpConnection,
    deadline: float,
    max_response_body_bytes: int,
) -> bytes:
    body_parts: list[bytes] = []
    remaining_bytes = max_response_body_bytes + 1
    while remaining_bytes > 0:
        _set_socket_timeout(connection, _remaining_seconds(deadline))
        body_part = response.read1(min(64 * 1024, remaining_bytes))
        if not body_part:
            break
        body_parts.append(body_part)
        remaining_bytes -= len(body_part)
    return b"".join(body_parts)


def _parse_http_url(url: str) -> tuple[SplitResult, str, int]:
    normalized_url = url.strip()
    try:
        parsed = urlsplit(normalized_url)
        port = parsed.port
    except ValueError as error:
        raise ValueError("Invalid HTTP URL.") from error
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").strip().rstrip(".").lower()
    if scheme not in {"http", "https"} or not parsed.netloc or not hostname:
        raise ValueError("Invalid HTTP URL.")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("HTTP URLs with user information are not allowed.")
    if "%" in hostname:
        raise ValueError("Scoped hostnames are not allowed.")
    return parsed, hostname, port if port is not None else (443 if scheme == "https" else 80)


def _private_hostname(hostname: str) -> bool:
    return hostname == "localhost" or hostname.endswith(
        (".localhost", ".local", ".internal", ".invalid")
    )


def _is_public_address(address: IPv4Address | IPv6Address) -> bool:
    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or not address.is_global
    ):
        return False
    if isinstance(address, IPv6Address):
        if address.ipv4_mapped is not None and not _is_public_address(address.ipv4_mapped):
            return False
        if address.sixtofour is not None and not _is_public_address(address.sixtofour):
            return False
        if address.teredo is not None and any(
            not _is_public_address(embedded_address) for embedded_address in address.teredo
        ):
            return False
    return True


def _system_address_resolver(hostname: str, port: int) -> tuple[ResolvedAddressInfo, ...]:
    address_info = socket.getaddrinfo(
        hostname,
        port,
        family=socket.AF_UNSPEC,
        type=socket.SOCK_STREAM,
    )
    return tuple(cast(ResolvedAddressInfo, item) for item in address_info)


class _ValidatedAddressConnector:
    def __init__(self, addresses: tuple[ResolvedHttpAddress, ...]) -> None:
        self.addresses = addresses

    def __call__(
        self,
        _address: tuple[str, int],
        timeout: object,
        source_address: tuple[str, int] | None,
    ) -> socket.socket:
        timeout_seconds = float(timeout) if isinstance(timeout, int | float) else None
        deadline = monotonic() + timeout_seconds if timeout_seconds is not None else None
        last_error: OSError | None = None
        for address in self.addresses:
            connection_socket = socket.socket(
                address.family,
                address.socket_type,
                address.protocol,
            )
            try:
                if source_address is not None:
                    connection_socket.bind(source_address)
                if deadline is not None:
                    connection_socket.settimeout(_remaining_seconds(deadline))
                connection_socket.connect(address.socket_address)
                return connection_socket
            except OSError as error:
                last_error = error
                connection_socket.close()
        if last_error is not None:
            raise last_error
        raise OSError("Public HTTP destination has no validated addresses.")


def _public_http_connection(
    destination: PublicHttpDestination,
    timeout_seconds: float,
) -> _HttpConnection:
    if destination.scheme == "https":
        connection: HTTPConnection = HTTPSConnection(
            destination.hostname,
            destination.port,
            timeout=timeout_seconds,
        )
    else:
        connection = HTTPConnection(
            destination.hostname,
            destination.port,
            timeout=timeout_seconds,
        )
    setattr(connection, "_create_connection", _ValidatedAddressConnector(destination.addresses))
    return cast(_HttpConnection, connection)


def _private_http_connection(
    destination: PublicHttpDestination,
    timeout_seconds: float,
) -> _HttpConnection:
    connection_type = HTTPSConnection if destination.scheme == "https" else HTTPConnection
    return cast(
        _HttpConnection,
        connection_type(destination.hostname, destination.port, timeout=timeout_seconds),
    )


def _remaining_seconds(deadline: float) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError("HTTP request timed out.")
    return remaining


def _set_socket_timeout(connection: _HttpConnection, timeout_seconds: float) -> None:
    if connection.sock is not None:
        connection.sock.settimeout(timeout_seconds)


def _safe_request_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in {"host", "content-length", "transfer-encoding"}
    }


def _without_sensitive_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in {"authorization", "cookie", "proxy-authorization"}
    }


def _without_entity_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in {"content-encoding", "content-language", "content-type"}
    }


def _normalized_url(url: str) -> str:
    parsed, hostname, port = _parse_http_url(url)
    path = parsed.path or "/"
    return urlunsplit(
        (
            parsed.scheme.lower(),
            f"{hostname}:{port}",
            path.rstrip("/") or "/",
            parsed.query,
            "",
        )
    )


def _fetch_public_tls_certificate(
    destination: PublicHttpDestination,
    timeout_seconds: float,
    observed_now: datetime,
) -> tuple[PublicTlsCertificate, int]:
    deadline = monotonic() + timeout_seconds
    connection_socket = _ValidatedAddressConnector(destination.addresses)(
        (destination.hostname, destination.port),
        _remaining_seconds(deadline),
        None,
    )
    try:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        with context.wrap_socket(
            connection_socket,
            server_hostname=destination.hostname,
            do_handshake_on_connect=False,
        ) as tls_socket:
            tls_socket.settimeout(_remaining_seconds(deadline))
            tls_socket.do_handshake()
            certificate_der = tls_socket.getpeercert(binary_form=True)
    finally:
        if connection_socket.fileno() != -1:
            connection_socket.close()
    if not certificate_der:
        raise ssl.SSLError("Peer did not present a TLS certificate.")
    return (
        _parse_public_tls_certificate(
            certificate_der, public_name=destination.hostname, now=observed_now
        ),
        len(destination.addresses),
    )


def _verify_public_tls_destination(
    destination: PublicHttpDestination,
    timeout_seconds: float,
) -> None:
    deadline = monotonic() + timeout_seconds
    connection_socket = _ValidatedAddressConnector(destination.addresses)(
        (destination.hostname, destination.port),
        _remaining_seconds(deadline),
        None,
    )
    try:
        context = ssl.create_default_context()
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        with context.wrap_socket(
            connection_socket,
            server_hostname=destination.hostname,
            do_handshake_on_connect=False,
        ) as tls_socket:
            tls_socket.settimeout(_remaining_seconds(deadline))
            tls_socket.do_handshake()
    finally:
        if connection_socket.fileno() != -1:
            connection_socket.close()


def _parse_public_tls_certificate(
    certificate_der: bytes,
    *,
    public_name: str,
    now: datetime,
) -> PublicTlsCertificate:
    certificate = x509.load_der_x509_certificate(certificate_der)
    not_before = _normalize_certificate_time(certificate.not_valid_before_utc)
    not_after = _normalize_certificate_time(certificate.not_valid_after_utc)
    days_remaining = int((not_after - now).total_seconds() // 86400)
    dns_san_names, ip_san_names = _certificate_san_names(certificate)
    common_name = _certificate_common_name(certificate)
    normalized_public_ip = _normalized_ip_literal(public_name)
    if normalized_public_ip:
        matched_names = tuple(name for name in ip_san_names if name == normalized_public_ip)
    else:
        matched_names = tuple(
            name for name in dns_san_names if _dns_name_matches(pattern=name, hostname=public_name)
        )
    if matched_names:
        public_name_match = True
        public_name_match_source: PublicTlsNameMatchSource = "san"
        presented_name_evidence = matched_names[:5]
    elif (
        not normalized_public_ip
        and not dns_san_names
        and common_name
        and _dns_name_matches(
            pattern=common_name,
            hostname=public_name,
        )
    ):
        public_name_match = True
        public_name_match_source = "subject"
        presented_name_evidence = (common_name,)
    else:
        public_name_match = False
        public_name_match_source = "none"
        presented_name_evidence = ()
    return PublicTlsCertificate(
        issuer=_bounded_certificate_name(certificate.issuer.rfc4514_string()),
        subject=_bounded_certificate_name(certificate.subject.rfc4514_string()),
        not_before=_format_certificate_time(not_before),
        not_after=_format_certificate_time(not_after),
        days_remaining=days_remaining,
        public_name_match=public_name_match,
        public_name_match_source=public_name_match_source,
        presented_san_count=len(dns_san_names) + len(ip_san_names),
        presented_name_evidence=presented_name_evidence,
    )


def _certificate_san_names(
    certificate: x509.Certificate,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    try:
        san_extension = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except x509.ExtensionNotFound:
        return (), ()
    dns_names: list[str] = []
    for dns_name in san_extension.value.get_values_for_type(x509.DNSName):
        normalized_name = _normalized_certificate_hostname(dns_name)
        if normalized_name and normalized_name not in dns_names:
            dns_names.append(normalized_name)
    ip_names: list[str] = []
    for certificate_ip in san_extension.value.get_values_for_type(x509.IPAddress):
        normalized_ip = str(certificate_ip)
        if normalized_ip not in ip_names:
            ip_names.append(normalized_ip)
    return tuple(dns_names), tuple(ip_names)


def _certificate_common_name(certificate: x509.Certificate) -> str:
    common_names = certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    for attribute in common_names:
        normalized_name = _normalized_certificate_hostname(attribute.value)
        if normalized_name:
            return normalized_name
    return ""


def _normalized_certificate_hostname(value: str | bytes) -> str:
    if isinstance(value, bytes):
        normalized_value = value.decode("utf-8", errors="ignore")
    else:
        normalized_value = value
    return normalized_value.strip().lower().rstrip(".")


def _dns_name_matches(*, pattern: str, hostname: str) -> bool:
    normalized_pattern = _normalized_dns_name(pattern, allow_wildcard=True)
    normalized_hostname = _normalized_dns_name(hostname, allow_wildcard=False)
    if not normalized_pattern or not normalized_hostname:
        return False
    if normalized_pattern == normalized_hostname:
        return True
    if not normalized_pattern.startswith("*."):
        return False
    suffix = normalized_pattern[1:]
    if not normalized_hostname.endswith(suffix):
        return False
    return normalized_hostname.count(".") == normalized_pattern.count(".")


def _normalized_dns_name(value: str, *, allow_wildcard: bool) -> str:
    normalized_value = value.strip().lower().rstrip(".")
    wildcard = allow_wildcard and normalized_value.startswith("*.")
    if wildcard:
        normalized_value = normalized_value[2:]
    if not normalized_value:
        return ""
    try:
        ascii_value = normalized_value.encode("idna").decode("ascii")
    except UnicodeError:
        return ""
    return f"*.{ascii_value}" if wildcard else ascii_value


def _normalized_ip_literal(value: str) -> str:
    try:
        return str(ip_address(value.strip().strip("[]")))
    except ValueError:
        return ""


def _bounded_certificate_name(value: str, *, limit: int = 256) -> str:
    normalized_value = " ".join(value.replace("\n", " ").split())
    if len(normalized_value) <= limit:
        return normalized_value
    return f"{normalized_value[: limit - 1].rstrip()}…"


def _bounded_tls_error_summary(error: BaseException) -> str:
    return _bounded_tls_summary(str(error) or error.__class__.__name__)


def _bounded_tls_summary(value: str, *, limit: int = 512) -> str:
    normalized_value = " ".join(value.replace("\n", " ").split())
    if not normalized_value:
        normalized_value = "Unknown TLS probe failure."
    if len(normalized_value) <= limit:
        return normalized_value
    return f"{normalized_value[: limit - 1].rstrip()}…"


def _refresh_public_tls_certificate(
    certificate: PublicTlsCertificate | None,
    *,
    now: datetime,
) -> PublicTlsCertificate | None:
    if certificate is None:
        return None
    try:
        not_after = datetime.fromisoformat(certificate.not_after.replace("Z", "+00:00"))
    except ValueError:
        return certificate
    days_remaining = int((_normalize_certificate_time(not_after) - now).total_seconds() // 86400)
    if days_remaining == certificate.days_remaining:
        return certificate
    return replace(certificate, days_remaining=days_remaining)


def _normalized_utc_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _normalize_certificate_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _format_certificate_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _tls_protocol_is_unsupported(detail: str) -> bool:
    lowered_detail = detail.lower()
    return any(
        token in lowered_detail
        for token in (
            "wrong version number",
            "unknown protocol",
            "unsupported protocol",
            "packet length too long",
            "http request",
        )
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
