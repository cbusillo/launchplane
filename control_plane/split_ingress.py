from hashlib import sha256
from urllib.parse import urlsplit

from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneHealthCheck,
    ProductLaneProfile,
)
from control_plane.contracts.route_binding_record import EnvironmentRouteBindingRecord


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
    path = urlsplit(lane.health_url).path or profile.health_path or "/"
    checks = tuple(
        ProductLaneHealthCheck(
            name=split_ingress_check_name(domain.domain_name),
            kind="public_http",
            url=f"https://{domain.domain_name}/{path.lstrip('/')}",
            require_runtime_identity=True,
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
                        if check.name not in derived_names
                    )
                    + checks,
                }
            ),
        }
    )


def split_ingress_check_name(domain_name: str) -> str:
    return f"HTTPS {domain_name} ({sha256(domain_name.encode()).hexdigest()[:12]})"
