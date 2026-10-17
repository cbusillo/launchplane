from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

from control_plane.contracts.data_provenance import DataProvenance
from control_plane.contracts.product_health_monitoring_migration import (
    canonical_health_check_record_token,
)
from control_plane.contracts.product_profile_record import ProductLaneProfile
from control_plane.contracts.public_dns import PublicDnsObservation
from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressObservationRecord,
    health_monitor_freshness_seconds,
)


class PublicDnsDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_name: str
    public_name: str
    status: Literal["match", "mismatch", "unavailable", "missing_expectation", "stale"]
    expected_addresses: tuple[str, ...] = ()
    observed_addresses: tuple[str, ...] = ()
    failure_code: str = ""
    provenance: DataProvenance


def build_public_dns_diagnostics(
    *,
    record_store: object,
    product: str,
    lane: ProductLaneProfile,
    now: datetime,
) -> tuple[PublicDnsDiagnostic, ...]:
    if lane.health_monitoring.monitoring_intent == "private":
        return ()
    reader = getattr(record_store, "list_public_ingress_observation_records", None)
    diagnostics: list[PublicDnsDiagnostic] = []
    for check in lane.health_monitoring.checks:
        if not check.enabled or check.kind != "public_http":
            continue
        records = (
            reader(
                product=product,
                context_name=lane.context,
                instance_name=lane.instance,
                check_kind="public_http",
                check_name=check.name,
                limit=50,
            )
            if callable(reader)
            else ()
        )
        latest = next(
            (
                record
                for record in records
                if isinstance(record, PublicIngressObservationRecord)
                and record.purpose == "probe"
                and canonical_health_check_record_token(record.check_name)
                == canonical_health_check_record_token(check.name)
            ),
            None,
        )
        names = {
            urlsplit(value).hostname
            for value in (lane.base_url, check.url or lane.health_url or lane.base_url)
            if value
        }
        expectations = {item.public_name: item.addresses for item in check.expected_dns_targets}
        for name in sorted(name for name in names if name):
            expected = expectations.get(name, ())
            evidence = (
                next(
                    (
                        target.dns
                        for target in sorted(
                            latest.targets,
                            key=lambda target: target.dns is None or target.dns.status == "match",
                        )
                        if target.dns is not None and target.dns.public_name == name
                    ),
                    None,
                )
                if latest is not None
                else None
            )
            diagnostics.append(
                _project_dns(
                    check_name=check.name,
                    name=name,
                    expected=expected,
                    evidence=evidence,
                    record=latest,
                    now=now,
                    interval=health_monitor_freshness_seconds(lane.instance),
                )
            )
    return tuple(diagnostics)


def _project_dns(
    *,
    check_name: str,
    name: str,
    expected: tuple[str, ...],
    evidence: PublicDnsObservation | None,
    record: PublicIngressObservationRecord | None,
    now: datetime,
    interval: int,
) -> PublicDnsDiagnostic:
    provenance = DataProvenance(
        source_kind="record",
        freshness_status="missing",
        detail="Explicit service-owned DNS expectations or current comparison are unavailable.",
    )
    status: Literal["match", "mismatch", "unavailable", "missing_expectation", "stale"] = (
        "unavailable" if expected else "missing_expectation"
    )
    observed: tuple[str, ...] = ()
    failure_code = ""
    if expected and evidence is not None and record is not None:
        observed = evidence.observed_addresses
        failure_code = evidence.failure_code
        try:
            observed_at = datetime.fromisoformat(record.observed_at.replace("Z", "+00:00"))
            deadline = observed_at + timedelta(seconds=interval)
            fresh = observed_at.tzinfo is not None and observed_at <= now <= deadline
            stale_after = deadline.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        except (ValueError, TypeError):
            fresh, stale_after = False, ""
        fresh = fresh and evidence.expected_addresses == expected
        status = evidence.status if fresh else "stale"
        provenance = DataProvenance(
            source_kind="provider",
            source_record_id=record.record_id,
            recorded_at=record.observed_at,
            refreshed_at=record.observed_at,
            stale_after=stale_after,
            freshness_status="verified" if fresh else "stale",
            detail="Monitor resolver A/AAAA comparison with explicit expectations; exact answer-set equality is required.",
        )
    return PublicDnsDiagnostic(
        check_name=check_name,
        public_name=name,
        status=status,
        expected_addresses=expected,
        observed_addresses=observed,
        failure_code=failure_code,
        provenance=provenance,
    )
