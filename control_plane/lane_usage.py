"""One usage rule for disposable work; downtime never attests data or isolation."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from typing import Protocol, cast

from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressObservationRecord,
    health_monitor_interval_seconds,
)


class UsageStore(Protocol):
    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord: ...

    def read_dokploy_target_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetRecord: ...

    def read_dokploy_target_id_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetIdRecord: ...

    def list_public_ingress_observation_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        check_name: str = "",
        check_kind: str = "",
        incident_id: str = "",
        limit: int | None = None,
    ) -> tuple[PublicIngressObservationRecord, ...]: ...


@dataclass(frozen=True)
class LaneUsageDecision:
    protected: bool
    reason: str
    observation_ids: tuple[str, ...] = ()
    down_since: str = ""
    down_until: str = ""


def _timestamp(value: str) -> datetime:
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.utcoffset() is None:
        raise ValueError("Usage qualification needs an aware observation time.")
    return moment


def _down_observations(
    store: object,
    profile: LaunchplaneProductProfileRecord,
    target: DokployTargetRecord,
    target_id: DokployTargetIdRecord,
    now: datetime,
) -> tuple[PublicIngressObservationRecord, ...]:
    # Use the monitor's actual target resolution for every driver, including
    # inherited drivers and private endpoints. A diagnostic failure is not an
    # application outage, nor is one failed URL alongside a serving URL.
    from control_plane.workflows.public_ingress_monitor import effective_health_check_monitor_target

    reader = getattr(store, "list_public_ingress_observation_records", None)
    if not callable(reader):
        return ()
    if (target.context, target.instance) != (target_id.context, target_id.instance):
        return ()
    lanes = [
        lane
        for lane in profile.lanes
        if (lane.context, lane.instance) == (target.context, target.instance)
    ]
    if len(lanes) != 1 or target.instance == "prod":
        return ()
    lane = lanes[0]
    checks = [check for check in lane.health_monitoring.checks if check.enabled]
    if not checks:
        return ()
    interval = timedelta(seconds=health_monitor_interval_seconds(lane.instance))
    # Two independent monitor slots spanning at least one cadence, within a
    # two-cadence window. Missing/late monitoring cannot qualify an outage.
    earliest = max(_timestamp(item.updated_at) for item in (profile, target, target_id))
    accepted: list[PublicIngressObservationRecord] = []
    intervals: list[tuple[datetime, datetime]] = []
    for check in checks:
        monitor = effective_health_check_monitor_target(
            profile=profile, lane=lane, check=check, record_store=store
        )
        if monitor is None:
            continue
        if (
            monitor.check_kind not in {"public_http", "private_http"}
            or monitor.resolution_failure_code
        ):
            return ()
        records = reader(
            product=profile.product,
            context_name=lane.context,
            instance_name=lane.instance,
            check_name=check.name,
            check_kind=check.kind,
            limit=16,
        )
        window: list[PublicIngressObservationRecord] = []
        for record in records:
            moment = _timestamp(record.observed_at)
            if moment > now:
                return ()
            if moment < max(earliest, now - 2 * interval):
                continue
            urls = {url for url in (monitor.base_url, monitor.health_url) if url}
            if (
                (
                    record.product,
                    record.context,
                    record.instance,
                    record.check_name,
                    record.check_kind,
                )
                != (profile.product, lane.context, lane.instance, check.name, check.kind)
                or record.purpose != "probe"
                or record.status != "fail"
                or (record.base_url, record.health_url, record.expected_runtime_identity)
                != (monitor.base_url, monitor.health_url, monitor.expected_runtime_identity)
            ):
                return ()
            if {probe.url for probe in record.targets} != urls or any(
                probe.status != "fail"
                or probe.failure_code
                not in {
                    "connection_timeout",
                    "dns_failure",
                    "tls_failure",
                    "http_error",
                    "health_status_error",
                    "redirect_loop",
                    "self_redirect",
                }
                or (probe.http_status is not None and probe.http_status < 500)
                for probe in record.targets
            ):
                return ()
            window.append(record)
        if not window:
            return ()
        moments = sorted({_timestamp(record.observed_at) for record in window})
        if now - moments[-1] >= interval or moments[-1] - moments[0] < interval:
            return ()
        accepted.extend(window)
        intervals.append((moments[0], moments[-1]))
    if (
        not intervals
        or min(end for _, end in intervals) - max(start for start, _ in intervals) < interval
    ):
        return ()
    return tuple(accepted)


def evaluate_lane_usage(
    store: object,
    *,
    profile: LaunchplaneProductProfileRecord,
    target: DokployTargetRecord | None,
    target_id: DokployTargetIdRecord | None,
    instance: str,
    usage: str,
    require_unused: bool = True,
    now: datetime | None = None,
) -> LaneUsageDecision:
    """A hold always wins. Only verified non-production downtime overrides usage.

    Callers that historically protect only positive use (retirement, isolation
    and preview refresh) retain that contract through require_unused=False.
    Data custody, resource separation, execution and release gates are separate.
    """
    if (
        target is not None
        and target.policies.staff_testing_hold is not None
        and target.policies.staff_testing_hold.purpose == "staff_testing"
    ):
        return LaneUsageDecision(True, "staff_testing_hold")
    if usage == "unused":
        return LaneUsageDecision(False, "unused")
    if instance != "prod" and target is not None and target_id is not None:
        try:
            records = _down_observations(
                store, profile, target, target_id, now or datetime.now(timezone.utc)
            )
        except (ValueError, TypeError, AttributeError, FileNotFoundError):
            records = ()
        if records:
            ordered = sorted(records, key=lambda record: _timestamp(record.observed_at))
            decision = LaneUsageDecision(
                False,
                "verified_down",
                tuple(record.record_id for record in ordered),
                ordered[0].observed_at,
                ordered[-1].observed_at,
            )
            logging.getLogger(__name__).info(
                "Non-production repair usage qualified by recorded downtime",
                extra={
                    "product": profile.product,
                    "context": target.context,
                    "instance": instance,
                    "down_evidence": decision.__dict__,
                },
            )
            return decision
    return LaneUsageDecision(usage == "in_use" or require_unused, "usage_protected")


def lane_usage_protected(
    store: object,
    *,
    product: str,
    context: str,
    instance: str,
    usage: str,
    require_unused: bool = True,
    now: datetime | None = None,
) -> bool:
    """Convenience for guards that already require readable tracked authority."""
    typed_store = cast(UsageStore, store)
    profile = typed_store.read_product_profile_record(product)
    target = typed_store.read_dokploy_target_record(context_name=context, instance_name=instance)
    try:
        target_id = typed_store.read_dokploy_target_id_record(
            context_name=context, instance_name=instance
        )
    except FileNotFoundError:
        target_id = None
    return evaluate_lane_usage(
        store,
        profile=profile,
        target=target,
        target_id=target_id,
        instance=instance,
        usage=usage,
        require_unused=require_unused,
        now=now,
    ).protected
