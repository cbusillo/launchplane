"""Persist actual monitor output with synthetic HTTP responses; no network calls."""

from datetime import datetime, timedelta
from typing import Any

from control_plane.contracts.product_profile_record import ProductLaneHealthMonitoringPolicy
from control_plane.contracts.public_ingress_monitoring import health_monitor_interval_seconds
from control_plane.workflows.public_ingress_monitor import (
    HttpObservation,
    check_public_ingress_target,
    effective_health_check_monitor_target,
)


def record_lane_health(
    store: Any,
    product: str,
    context: str,
    now: datetime,
    *,
    instance: str = "testing",
    status: int = 503,
    configure: bool = True,
    observed_at: tuple[datetime, ...] | None = None,
) -> tuple[str, ...]:
    profile = store.read_product_profile_record(product)
    if configure:
        profile = profile.model_copy(
            update={
                "lanes": tuple(
                    lane.model_copy(
                        update={
                            "base_url": "https://testing.example.com",
                            "health_url": "https://testing.example.com/healthz",
                            "health_monitoring": ProductLaneHealthMonitoringPolicy.model_validate(
                                {
                                    "monitoring_intent": "public",
                                    "checks": [{"name": "public-ingress", "kind": "public_http"}],
                                }
                            ),
                        }
                    )
                    if (lane.context, lane.instance) == (context, instance)
                    else lane
                    for lane in profile.lanes
                )
            }
        )
        store.write_product_profile_record(profile)
    lane = next(
        lane for lane in profile.lanes if (lane.context, lane.instance) == (context, instance)
    )
    cadence = timedelta(seconds=health_monitor_interval_seconds(instance))
    ids = []
    for check in lane.health_monitoring.checks:
        target = effective_health_check_monitor_target(
            profile=profile, lane=lane, check=check, record_store=store
        )
        if target is None:
            continue
        for moment in observed_at or (now - cadence * 1.25, now - cadence * 0.25):
            record = check_public_ingress_target(
                target=target,
                checked_at=moment.isoformat(),
                timeout_seconds=1,
                http_get=lambda url, _: HttpObservation(status, url, 0),
                tls_get=lambda *_: (_ for _ in ()).throw(AssertionError("Unexpected TLS call")),
            )
            store.write_public_ingress_observation_record(record)
            ids.append(record.record_id)
    return tuple(ids)
