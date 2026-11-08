from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from control_plane.contracts.data_provenance import DataProvenance, FreshnessStatus
from control_plane.contracts.product_topology_read_model import ProductEnvironmentTopology

if TYPE_CHECKING:
    from control_plane.contracts.product_environment_read_model import (
        ProductEnvironmentDetail,
        ProductEnvironmentSummary,
    )

SignalTone = FreshnessStatus | Literal["warning", "danger"]
NEGATIVE_TLS_STATES = {"expired", "hostname_mismatch", "untrusted", "self_signed", "unreachable"}
FAILED_IDENTITY_STATES = {"mismatch", "malformed"}
ROUTE_DIAGNOSTICS = {
    "missing_route_authority",
    "route_authority_disabled",
    "stale_route_authority",
    "domain_divergence",
    "provider_placement_missing",
    "placement_divergence",
    "external_ingress_internals_unsupported",
    "ingress_ownership_unknown",
    "ingress_divergence",
    "tls_ownership_unknown",
    "tls_ownership_divergence",
}
LAYER_LABELS = {
    "dns": "DNS",
    "tls": "Certificate",
    "runtime_identity": "Runtime identity",
    "network": "Network",
    "redirect": "Redirect",
    "http": "HTTP",
    "configuration": "Configuration",
    "provider": "Provider",
    "unknown": "Unknown layer",
}


class ProductLaneSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tone: SignalTone
    sidebar_tone: SignalTone
    description: str
    reasons: tuple[str, ...] = ()
    attention_reasons: tuple[str, ...] = ()
    warning_reasons: tuple[str, ...] = ()
    release_healthy: bool = False
    public_observation_gap: str | None = None


class ProductLaneStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evaluated_at: str
    current: ProductLaneSignal
    transitions: tuple[ProductLaneStatusTransition, ...] = ()


class ProductLaneStatusTransition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    after: str
    signal: ProductLaneSignal


def _timestamp(value: str) -> float:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
    except ValueError:
        return float("inf")


def _expired(provenance: DataProvenance, now: float) -> bool:
    return (
        provenance.freshness_status in {"verified", "recorded"}
        and _timestamp(provenance.stale_after) < now
    )


def _trust(evidence: object, now: float) -> str:
    provenance = getattr(evidence, "provenance")
    return "stale" if _expired(provenance, now) else getattr(evidence, "trust_state")


def public_observation_gap(topology: ProductEnvironmentTopology, now: float) -> str | None:
    if topology.desired.public_website == "none":
        return None
    ingress = topology.observed.ingress
    if (
        not ingress.probe_effective
        or ingress.status != "pass"
        or _trust(ingress, now) != "verified"
    ):
        return "Public route observation incomplete"
    if ingress.runtime_identity_status != "match":
        return "Public runtime identity unverified"
    expected = [domain for domain in topology.desired.domains if domain.tls_expected]
    if not expected:
        return "Public HTTPS domain missing"
    if any(
        domain.status in {"expiring", "unknown", "unsupported"}
        for domain in topology.observed.tls_domains
    ):
        return "Public TLS needs review"
    if all(
        any(
            observed.domain_name == domain.domain_name
            and observed.status == "valid"
            and _trust(observed, now) == "verified"
            and observed.public_name_match
            and observed.validated_address_count > 0
            and observed.days_remaining is not None
            and observed.days_remaining > 0
            for observed in topology.observed.tls_domains
        )
        for domain in expected
    ):
        return None
    return "Public DNS/TLS observation incomplete"


def _runtime_gap(
    environment: ProductEnvironmentSummary | ProductEnvironmentDetail,
    now: float,
    *,
    require_checks: bool = True,
) -> str | None:
    placement = environment.topology.observed.placement
    if placement.runtime_identity_status != "match" or _trust(placement, now) != "verified":
        return "The running production release is not verified"
    checks = [
        check
        for check in environment.health_monitoring.checks
        if check.probe_effective and not check.advisory
    ]
    if (require_checks and not checks) or any(
        check.status != "pass"
        or _trust(check, now) != "verified"
        or _timestamp(check.provenance.stale_after) < now
        for check in checks
    ):
        return "Current production health is not verified"
    freshness = (
        "stale"
        if _expired(environment.provenance, now)
        else environment.provenance.freshness_status
    )
    return (
        "Current production evidence is not verified"
        if checks and freshness != "verified"
        else None
    )


def _runtime_tone(
    environment: ProductEnvironmentSummary | ProductEnvironmentDetail, now: float, gap: str | None
) -> SignalTone:
    checks = [check for check in environment.health_monitoring.checks if check.probe_effective]
    topology = environment.topology
    warnings = [warning for warning in topology.warnings if warning.code not in ROUTE_DIAGNOSTICS]
    other_warnings = [
        detail
        for detail in environment.warnings
        if not any(warning.detail == detail for warning in topology.warnings)
    ]
    freshness = (
        "stale"
        if _expired(environment.provenance, now)
        else environment.provenance.freshness_status
    )
    if not checks and freshness == "missing":
        return "missing"
    if (
        other_warnings
        or any(warning.severity != "info" for warning in warnings)
        or _runtime_gap(environment, now, require_checks=False) is not None
        or gap is not None
        or topology.desired.public_website != "none"
        and (
            not any(check.kind == "public_http" for check in checks)
            or _trust(topology.observed.ingress, now) == "stale"
            or any(_trust(domain, now) == "stale" for domain in topology.observed.tls_domains)
        )
        or _timestamp(environment.provenance.stale_after) < now
        or freshness == "stale"
    ):
        return "warning"
    if checks and freshness == "verified":
        return "verified"
    if freshness == "missing":
        return "missing"
    return "recorded" if environment.trust_state == "verified" else environment.trust_state


def _readable(value: str) -> str:
    return value.replace("_", " ").strip()


def _unique(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _signal(
    environment: ProductEnvironmentSummary | ProductEnvironmentDetail, now: float
) -> ProductLaneSignal:
    topology = environment.topology
    monitoring = environment.health_monitoring
    reasons: list[str] = []
    warnings: list[str] = []
    for check in monitoring.checks:
        if not check.probe_effective:
            continue
        if check.advisory:
            if (
                check.status != "pass"
                or _trust(check, now) != "verified"
                or check.incident_status == "open"
            ):
                warnings.append(
                    check.summary.strip() or f"Recovery check {check.name} is not verified"
                )
            continue
        if check.status == "fail":
            reasons.append(
                check.summary.strip()
                or f"Health check {check.name} failed: {_readable(check.failure_code) or 'unknown failure'}"
            )
        elif check.incident_status == "open" and not any(
            incident.check_name == check.name for incident in monitoring.open_incidents
        ):
            reasons.append(f"Health check {check.name} has an open incident")
    identity = topology.observed.placement.runtime_identity_status
    if identity in FAILED_IDENTITY_STATES:
        reasons.append(f"Runtime identity {identity}")
    reasons.extend(
        warning.detail
        for warning in topology.warnings
        if warning.code not in ROUTE_DIAGNOSTICS and warning.severity == "error"
    )
    for domain in topology.observed.tls_domains:
        if domain.status in NEGATIVE_TLS_STATES:
            (warnings if domain.advisory else reasons).append(
                f"TLS for {domain.domain_name}: {_readable(domain.status)}"
            )
    gap = public_observation_gap(topology, now)
    runtime_tone = _runtime_tone(environment, now, gap)
    runtime_gap = _runtime_gap(environment, now)
    if (
        environment.environment == "prod"
        and monitoring.monitoring_intent != "prelaunch"
        and runtime_gap
    ):
        reasons.append(runtime_gap)
    incidents = []
    for incident in monitoring.open_incidents:
        recovery = incident.severity == "warning" and (
            any(
                check.advisory
                and check.name == incident.check_name
                and check.kind == incident.check_kind
                for check in monitoring.checks
            )
            or incident.check_kind == "tls"
            and any(
                domain.incident_id == incident.incident_id and domain.advisory
                for domain in topology.observed.tls_domains
            )
        )
        if recovery:
            warnings.append(f"Recovery path: {_readable(incident.failure_code)}")
        else:
            incidents.append(incident)
    observed = topology.observed
    public_evidence = [
        observed.ingress,
        *(domain for domain in observed.tls_domains if not domain.advisory),
    ]
    observed_stale = (
        _trust(observed.placement, now) == "stale"
        or topology.desired.public_website != "none"
        and any(_trust(item, now) == "stale" for item in public_evidence)
    )
    release_healthy = (
        not reasons
        and runtime_gap is None
        and (
            topology.desired.public_website == "none"
            or any(
                check.probe_effective and not check.advisory and check.kind == "public_http"
                for check in monitoring.checks
            )
        )
        and observed.trust_state == "verified"
        and not observed_stale
        and all(
            environment.environment == "prod"
            and incident.failure_layer == "provider"
            and incident.severity == "warning"
            for incident in incidents
        )
    )
    delivery_only = bool(incidents) and release_healthy
    if not delivery_only:
        for incident in incidents:
            severity = _readable(incident.severity) or "Open"
            reasons.append(
                f"{severity[0].upper()}{severity[1:]} incident open: {_readable(incident.failure_code) or 'unknown failure'}"
            )
    tone = (
        "danger"
        if reasons
        else runtime_tone
        if runtime_tone in {"missing", "stale"}
        else "warning"
        if delivery_only or warnings
        else runtime_tone
    )
    warning_reasons = (
        ()
        if reasons
        else _unique(
            warnings
            + (
                [
                    f"{_readable(incident.failure_code)}; the current production release is healthy"
                    for incident in incidents
                ]
                if delivery_only
                else []
            )
        )
    )
    layers = [
        LAYER_LABELS.get(incident.failure_layer, "Unknown layer")
        for incident in monitoring.open_incidents
    ]
    for check in monitoring.checks:
        if check.probe_effective and check.status == "fail":
            if check.failure_code == "dns_failure":
                layers.append(LAYER_LABELS["dns"])
            if check.failure_code.startswith("tls_"):
                layers.append(LAYER_LABELS["tls"])
            if check.failure_code == "wrong_runtime_identity":
                layers.append(LAYER_LABELS["runtime_identity"])
    if identity in FAILED_IDENTITY_STATES:
        layers.append(LAYER_LABELS["runtime_identity"])
    if any(domain.status in NEGATIVE_TLS_STATES for domain in topology.observed.tls_domains):
        layers.append(LAYER_LABELS["tls"])
    sidebar_reasons = list(reasons) if tone == "danger" else []
    if tone == "warning":
        sidebar_reasons.extend(warning_reasons)
        if gap:
            sidebar_reasons.append(gap)
        if identity != "match":
            sidebar_reasons.append("Runtime identity unverified")
        sidebar_reasons.extend(
            warning.detail for warning in topology.warnings if warning.severity == "warning"
        )
        sidebar_reasons.extend(environment.warnings)
        if not sidebar_reasons:
            sidebar_reasons.append("Health or runtime evidence not yet verified")
    stale = (
        _expired(environment.provenance, now)
        or environment.provenance.freshness_status == "stale"
        or observed_stale
        or any(
            check.probe_effective and not check.advisory and _trust(check, now) == "stale"
            for check in monitoring.checks
        )
    )
    sidebar_tone = "stale" if tone == "stale" or tone == "warning" and stale else tone
    state = {
        "verified": "Healthy",
        "danger": "Alert",
        "warning": "Warning",
        "unsupported": "Unsupported",
        "recorded": "Unknown (recorded, not verified)",
    }.get(tone, "Unknown (unverified)")
    description = (
        "Stale evidence; awaiting current observations"
        if sidebar_tone == "stale"
        else state
        + (f"; {', '.join(_unique(layers))}" if layers else "")
        + (f"; {'; '.join(_unique(sidebar_reasons))}" if sidebar_reasons else "")
    )
    return ProductLaneSignal(
        tone=tone,
        sidebar_tone=sidebar_tone,
        description=description,
        reasons=_unique(sidebar_reasons),
        attention_reasons=_unique(reasons),
        warning_reasons=warning_reasons,
        release_healthy=release_healthy,
        public_observation_gap=gap,
    )


def build_product_lane_status(
    environment: ProductEnvironmentSummary | ProductEnvironmentDetail,
    *,
    now: datetime | None = None,
) -> ProductLaneStatus:
    now = now or datetime.now(timezone.utc)
    topology = environment.topology
    recorded = topology.provider_recorded
    evidence = [
        environment,
        *environment.health_monitoring.checks,
        recorded,
        recorded.placement,
        recorded.ingress,
        recorded.tls,
        topology.observed.placement,
        topology.observed.ingress,
        *topology.observed.tls_domains,
    ]
    deadlines = sorted(
        {
            _timestamp(item.provenance.stale_after)
            for item in evidence
            if item.provenance.stale_after
            and isfinite(_timestamp(item.provenance.stale_after))
            and _timestamp(item.provenance.stale_after) >= now.timestamp()
        }
    )
    current = _signal(environment, now.timestamp())
    previous = current
    transitions = []
    for deadline in deadlines:
        signal = _signal(environment, deadline + 0.000001)
        if signal != previous:
            transitions.append(
                ProductLaneStatusTransition(
                    after=datetime.fromtimestamp(deadline, timezone.utc).isoformat(), signal=signal
                )
            )
            previous = signal
    return ProductLaneStatus(
        evaluated_at=now.isoformat(), current=current, transitions=tuple(transitions)
    )
