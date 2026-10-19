"""One exact private HTTP application origin, without provider routing overrides."""

from collections.abc import Iterable

from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.public_hosts import normalize_public_hosts
from control_plane.dokploy.api import JsonObject, JsonValue


def _domain_value_matches(actual: object, expected: JsonValue) -> bool:
    if expected is None:
        return actual in (None, "")
    if isinstance(expected, bool):
        return actual is expected
    return actual == expected


def private_application_domain_payload(
    *, hostname: str, runtime_port: int, application_id: str
) -> JsonObject:
    return {
        "host": hostname,
        "path": "/",
        "internalPath": "/",
        "port": runtime_port,
        "https": False,
        "applicationId": application_id,
        "certificateType": "none",
        "customCertResolver": None,
        "composeId": None,
        "serviceName": None,
        "domainType": "application",
        "previewDeploymentId": None,
        "stripPath": False,
    }


def require_private_application_domains(
    domains: object, *, hostname: str | None, runtime_port: int, application_id: str
) -> None:
    """Accept no origin or the exact origin created by supported private ingress."""
    if not isinstance(domains, (list, tuple)) or len(domains) > 1:
        raise ValueError("The application's hostname authority is foreign or changed.")
    if not domains:
        return
    if hostname is None:
        raise ValueError("The application has no owned testing hostname.")
    expected = private_application_domain_payload(
        hostname=hostname, runtime_port=runtime_port, application_id=application_id
    )
    # Keep the create payload stable for reviewed pending-operation snapshots.
    # These additional domain.one fields must retain their inert schema defaults.
    optional_defaults: JsonObject = {
        "customEntrypoint": None,
        "middlewares": [],
        "forwardAuthEnabled": False,
        "enabled": True,
    }
    domain = domains[0]
    if (
        not isinstance(domain, dict)
        or not isinstance(domain.get("domainId"), str)
        or not domain["domainId"].strip()
        or any(not _domain_value_matches(domain.get(key), value) for key, value in expected.items())
        or any(
            not _domain_value_matches(domain.get(key, value), value)
            for key, value in optional_defaults.items()
        )
    ):
        raise ValueError("The application's hostname authority is foreign or changed.")


def require_exclusive_testing_application_hostname(
    *, hostname: str | None, target: DokployTargetRecord, targets: Iterable[DokployTargetRecord]
) -> None:
    """Retain existing lane claims, including unresolved public-host effects."""
    if (
        target.public_hosts
        or target.pending_public_host_effects
        or set(target.domains) - ({hostname} if hostname else set())
        or (hostname is not None and normalize_public_hosts((hostname,)) != (hostname,))
    ):
        raise ValueError("Private ingress requires one exclusively owned testing hostname.")
    if hostname is None:
        return
    for other in targets:
        if (other.context, other.instance) == (target.context, target.instance):
            continue
        for claim in (
            *other.domains,
            *other.public_hosts,
            *(effect.host for effect in other.pending_public_host_effects),
        ):
            normalized = claim.strip().rstrip(".").lower()
            if (
                normalized == hostname
                or normalized.startswith("~")
                or (normalized.startswith("*.") and hostname.endswith(normalized[1:]))
            ):
                raise ValueError("The hostname belongs to another lane.")
