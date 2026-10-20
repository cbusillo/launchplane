import re
from urllib.parse import urlparse

_DNS_CASE_TRANSLATION = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def canonical_dns_hostname(value: str) -> str:
    """Compare DNS labels using locale-independent ASCII case folding."""
    return value.strip().rstrip(".").translate(_DNS_CASE_TRANSLATION)


def retained_hostname_identity(value: str) -> str:
    """Extract a retained domain/URL claim without accepting it as new input."""
    authority = value.strip().split("://", 1)[-1]
    authority = re.split(r"[/#?]", authority, maxsplit=1)[0].rsplit("@", 1)[-1]
    hostname = (
        authority[1:].split("]", 1)[0] if authority.startswith("[") else authority.split(":", 1)[0]
    )
    return canonical_dns_hostname(hostname)


def normalize_public_hosts(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > 64:
        raise ValueError("Public hosts require a bounded list of DNS hostnames.")
    hosts: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("Public hosts must be DNS hostnames.")
        host = item.strip().lower()
        labels = host.split(".")
        if (
            len(host) > 253
            or len(labels) < 2
            or any(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) is None
                for label in labels
            )
            or labels[-1].isdigit()
        ):
            raise ValueError("Public hosts must be DNS hostnames without schemes, ports or paths.")
        if host in hosts:
            raise ValueError("Public hosts must be unique.")
        hosts.append(host)
    return tuple(hosts)


def resolve_public_base_url(*, instance: str, public_hosts: tuple[str, ...]) -> str:
    """Resolve presentation intent without changing origin/health authority."""
    if instance == "prod" and public_hosts:
        return f"https://{public_hosts[0]}"
    return ""


def resolve_origin_base_url(*, domains: tuple[str, ...]) -> str:
    """Use the first recorded origin domain, never presentation intent."""
    for domain in domains:
        origin = domain.strip().rstrip("/")
        if origin:
            return origin if urlparse(origin).scheme else f"https://{origin}"
    return ""
