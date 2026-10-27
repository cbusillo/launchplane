from hashlib import sha256
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneHealthCheck,
    ProductLaneProfile,
)
from control_plane.contracts.route_binding_record import EnvironmentRouteBindingRecord
from control_plane.contracts.public_dns import PublicDnsExpectedTarget


@dataclass(frozen=True)
class SplitIngressMonitoringPolicy:
    expectations: tuple[PublicDnsExpectedTarget, ...]
    recovery_threshold: int
    conflict: str = ""


def split_ingress_monitoring_policy(
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    domains: tuple[str, ...],
    host: str,
) -> SplitIngressMonitoringPolicy:
    canonical_lane = next(
        (
            item
            for item in profile.lanes
            if (item.context, item.instance) == (lane.context, lane.instance)
        ),
        lane,
    )
    checks = tuple(
        check
        for check in canonical_lane.health_monitoring.checks
        if check.kind == "public_http" and check.enabled
    )
    threshold = max((check.recovery_observation_threshold for check in checks), default=1)
    supported_urls = {_url_key(canonical_lane.base_url)} | {
        _url_key(split_ingress_health_url(profile, canonical_lane, domain)) for domain in domains
    }
    unsupported = tuple(
        check.name for check in checks if check.url and _url_key(check.url) not in supported_urls
    )
    if unsupported:
        return SplitIngressMonitoringPolicy(
            (),
            threshold,
            f"Split monitoring cannot replace these additional public checks: {', '.join(unsupported)}.",
        )
    expectations = tuple(target for check in checks for target in check.expected_dns_targets)
    if any(target.public_name not in domains for target in expectations):
        return SplitIngressMonitoringPolicy(
            (), threshold, "A configured DNS expectation is outside the recorded route hosts."
        )
    matching = tuple(target for target in expectations if target.public_name == host)
    if len({target.addresses for target in matching}) > 1:
        return SplitIngressMonitoringPolicy(
            (), threshold, f"Configured DNS expectations disagree for {host}."
        )
    return SplitIngressMonitoringPolicy(matching[:1], threshold)


def split_ingress_health_url(
    profile: LaunchplaneProductProfileRecord, lane: ProductLaneProfile, host: str
) -> str:
    health = urlsplit(lane.health_url)
    path = health.path or profile.health_path or "/"
    return urlunsplit(("https", host, f"/{path.lstrip('/')}", health.query, ""))


def split_ingress_check_base_url(lane: ProductLaneProfile, health_url: str) -> str:
    base = lane.base_url.strip().rstrip("/")
    return (
        base
        if urlsplit(base).hostname == urlsplit(health_url).hostname
        and _url_key(base) != _url_key(health_url)
        else ""
    )


def _url_key(url: str) -> tuple[str, str, str, str]:
    parsed = urlsplit(url.strip())
    authority = parsed.netloc
    if "@" not in authority and parsed.hostname:
        try:
            port = parsed.port
        except ValueError:
            pass
        else:
            host = parsed.hostname
            authority = f"[{host}]" if ":" in host else host
            if port is not None and port != {"https": 443, "http": 80}.get(parsed.scheme):
                authority = f"{authority}:{port}"
    return parsed.scheme, authority, parsed.path or "/", parsed.query


def read_split_ingress_binding(
    record_store: object, profile: LaunchplaneProductProfileRecord, lane: ProductLaneProfile
) -> EnvironmentRouteBindingRecord | None:
    reader = getattr(record_store, "read_route_binding_record", None)
    if not callable(reader):
        return None
    try:
        binding = reader(
            product=profile.product, context_name=lane.context, instance_name=lane.instance
        )
    except FileNotFoundError:
        return None
    return (
        binding
        if isinstance(binding, EnvironmentRouteBindingRecord) and binding.external_ingress
        else None
    )


def split_ingress_monitoring_lane(
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    binding: EnvironmentRouteBindingRecord | None,
) -> ProductLaneProfile:
    if binding is None or binding.external_ingress is None:
        return lane
    policies = {
        domain.domain_name: split_ingress_monitoring_policy(
            profile, lane, tuple(d.domain_name for d in binding.domains), domain.domain_name
        )
        for domain in binding.domains
    }
    checks = tuple(
        ProductLaneHealthCheck(
            name=split_ingress_check_name(domain.domain_name),
            kind="public_http",
            url=split_ingress_health_url(profile, lane, domain.domain_name),
            require_runtime_identity=True,
            expected_dns_targets=policies[domain.domain_name].expectations,
            recovery_observation_threshold=policies[domain.domain_name].recovery_threshold,
        )
        for domain in binding.domains
    )
    derived_names = {check.name for check in checks}
    return lane.model_copy(
        update={
            "health_monitoring": lane.health_monitoring.model_copy(
                update={
                    "checks": tuple(
                        check
                        for check in lane.health_monitoring.checks
                        if check.kind != "public_http" and check.name not in derived_names
                    )
                    + checks,
                }
            ),
        }
    )


def split_ingress_check_name(domain_name: str) -> str:
    return f"HTTPS {domain_name} ({sha256(domain_name.encode()).hexdigest()[:12]})"


def split_ingress_custody_hosts(
    binding: EnvironmentRouteBindingRecord | None, url: str
) -> tuple[str, ...]:
    if binding is None or binding.external_ingress is None:
        return ()
    host = urlsplit(url).hostname
    if host not in {domain.domain_name for domain in binding.domains}:
        return ()
    external = binding.external_ingress.domains
    if host in external:
        return external
    return tuple(
        domain.domain_name for domain in binding.domains if domain.domain_name not in external
    )
