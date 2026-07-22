from __future__ import annotations

from urllib.parse import urlsplit

from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)


class IngressRouteInstanceScopeError(ValueError):
    def __init__(self, *, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def validate_ingress_route_instance_scope(
    *,
    profile: LaunchplaneProductProfileRecord,
    context: str,
    instance: str,
    requested_domains: tuple[str, ...],
) -> None:
    normalized_context = context.strip()
    normalized_instance = instance.strip()
    matching_lanes = tuple(
        lane
        for lane in profile.lanes
        if lane.context.strip() == normalized_context
        and lane.instance.strip() == normalized_instance
    )
    if len(matching_lanes) != 1:
        raise IngressRouteInstanceScopeError(
            code="invalid_ingress_instance_scope",
            message="Ingress route instance scope does not identify exactly one product lane.",
        )

    requested = frozenset(_normalized_domain(domain) for domain in requested_domains)
    lane = matching_lanes[0]
    authorized_domains = _lane_public_domains(lane, require_domains=True)
    if not requested.issubset(authorized_domains):
        raise IngressRouteInstanceScopeError(
            code="ingress_route_domain_scope_mismatch",
            message="Ingress route domains are not owned by the requested product lane.",
        )

    for other_lane in profile.lanes:
        if other_lane is lane:
            continue
        if requested & _lane_public_domains(other_lane, require_domains=False):
            raise IngressRouteInstanceScopeError(
                code="ingress_route_domain_scope_ambiguous",
                message="Ingress route domains are shared with another product lane.",
            )


def _lane_public_domains(
    lane: ProductLaneProfile,
    *,
    require_domains: bool,
) -> frozenset[str]:
    urls: list[tuple[str, str]] = [
        ("base_url", lane.base_url),
        ("health_url", lane.health_url),
    ]
    urls.extend(
        (f"health check {check.name!r}", check.url)
        for check in lane.health_monitoring.checks
        if check.enabled and check.kind == "public_http" and check.url.strip()
    )
    domains = {
        _public_https_domain(value=value, label=label) for label, value in urls if value.strip()
    }
    if require_domains and not domains:
        raise IngressRouteInstanceScopeError(
            code="ingress_route_lane_domains_missing",
            message="Ingress route instance scope requires a DB-backed public HTTPS domain.",
        )
    return frozenset(domains)


def _public_https_domain(*, value: str, label: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError as error:
        raise IngressRouteInstanceScopeError(
            code="ingress_route_lane_url_invalid",
            message=f"Product lane {label} is not a valid public HTTPS URL.",
        ) from error
    domain = (parsed.hostname or "").strip().rstrip(".").lower()
    if (
        parsed.scheme.lower() != "https"
        or not domain
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        raise IngressRouteInstanceScopeError(
            code="ingress_route_lane_url_invalid",
            message=f"Product lane {label} must be a credential-free HTTPS URL on port 443.",
        )
    return domain


def _normalized_domain(value: str) -> str:
    normalized = value.strip().rstrip(".").lower()
    if not normalized:
        raise IngressRouteInstanceScopeError(
            code="ingress_route_domain_scope_mismatch",
            message="Ingress route domains must be non-empty DNS names.",
        )
    return normalized
