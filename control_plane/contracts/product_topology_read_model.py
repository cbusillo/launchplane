from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from control_plane.contracts.data_provenance import DataProvenance, FreshnessStatus
from control_plane.contracts.lane_summary import LaunchplaneLaneSummary
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.contracts.public_ingress_monitoring import (
    PUBLIC_HTTP_STALE_AFTER_SECONDS,
    PublicIngressIncidentRecord,
    PublicIngressObservationRecord,
    PublicIngressTargetObservation,
    PublicIngressTlsObservation,
    PublicIngressTlsStatus,
    build_public_ingress_tls_check_name,
)
from control_plane.contracts.route_binding_record import (
    EnvironmentRouteBindingRecord,
    RouteBindingTerminationKind,
    RouteBindingTlsOwner,
)
from control_plane.contracts.runtime_identity import RuntimeIdentity, RuntimeIdentityStatus


ProductTopologyAuthorityStatus = Literal["active", "disabled", "missing"]
ProductTopologyDomainRole = Literal["primary", "alias"]
ProductTopologyIngressPath = Literal[
    "edge_to_provider",
    "direct_to_provider",
    "none",
    "unknown",
]
ProductTopologyTerminationKind = RouteBindingTerminationKind | Literal["unknown"]
ProductTopologyTlsOwner = RouteBindingTlsOwner | Literal["unknown"]
ProductTopologyTlsTerminator = Literal["edge", "provider", "external", "none", "unknown"]
ProductObservedTlsStatus = PublicIngressTlsStatus | Literal["missing"]
ProductTopologyWarningScope = Literal[
    "authority",
    "placement",
    "domains",
    "ingress",
    "tls",
    "observation",
]
ProductTopologyWarningSeverity = Literal["warning", "error"]
ProductTopologyWarningCode = Literal[
    "missing_route_authority",
    "route_authority_disabled",
    "stale_route_authority",
    "desired_domain_unknown",
    "domain_divergence",
    "provider_placement_missing",
    "placement_divergence",
    "external_ingress_internals_unsupported",
    "ingress_ownership_unknown",
    "ingress_divergence",
    "tls_ownership_unknown",
    "tls_ownership_divergence",
    "tls_observation_missing",
    "stale_tls_observation",
    "tls_mismatch",
    "tls_expiring",
    "tls_expired",
    "tls_untrusted",
    "tls_unavailable",
    "tls_unsupported",
    "public_ingress_observation_missing",
    "stale_public_ingress_observation",
    "public_runtime_identity_unverified",
    "public_ingress_failure",
]


def _missing_provenance(detail: str) -> DataProvenance:
    return DataProvenance(
        source_kind="record",
        freshness_status="missing",
        detail=detail,
    )


class ProductTopologyDomain(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain_name: str
    role: ProductTopologyDomainRole = "primary"
    tls_expected: bool = False


class ProductTopologyPlacement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = ""
    target_type: str = ""
    target_name: str = ""
    provider_target_type: str = ""
    provider_target_record_present: bool = False
    trust_state: FreshnessStatus = "missing"
    provenance: DataProvenance = Field(
        default_factory=lambda: _missing_provenance(
            "Launchplane has no provider-neutral placement authority for this environment."
        )
    )


class ProductTopologyIngress(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = ""
    endpoint_key: str = ""
    termination_kind: ProductTopologyTerminationKind = "unknown"
    path: ProductTopologyIngressPath = "unknown"
    trust_state: FreshnessStatus = "missing"
    provenance: DataProvenance = Field(
        default_factory=lambda: _missing_provenance(
            "Launchplane has no provider-neutral ingress authority for this environment."
        )
    )


class ProductTopologyTlsOwnership(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner: ProductTopologyTlsOwner = "unknown"
    terminator: ProductTopologyTlsTerminator = "unknown"
    provider: str = ""
    trust_state: FreshnessStatus = "missing"
    provenance: DataProvenance = Field(
        default_factory=lambda: _missing_provenance(
            "Launchplane has no provider-neutral TLS ownership authority for this environment."
        )
    )


class ProductDesiredTopology(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = ""
    health_url: str = ""
    domains: tuple[ProductTopologyDomain, ...] = ()
    trust_state: FreshnessStatus = "missing"
    provenance: DataProvenance = Field(
        default_factory=lambda: _missing_provenance(
            "The product profile does not declare public URL intent for this environment."
        )
    )


class ProductProviderRecordedTopology(BaseModel):
    model_config = ConfigDict(extra="forbid")

    authority_status: ProductTopologyAuthorityStatus = "missing"
    placement: ProductTopologyPlacement = Field(default_factory=ProductTopologyPlacement)
    domains: tuple[ProductTopologyDomain, ...] = ()
    ingress: ProductTopologyIngress = Field(default_factory=ProductTopologyIngress)
    tls: ProductTopologyTlsOwnership = Field(default_factory=ProductTopologyTlsOwnership)
    trust_state: FreshnessStatus = "missing"
    provenance: DataProvenance = Field(
        default_factory=lambda: _missing_provenance(
            "Launchplane has no environment route-binding record for this environment."
        )
    )


class ProductObservedPlacement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_runtime_identity: RuntimeIdentity | None = None
    observed_runtime_identity: RuntimeIdentity | None = None
    runtime_identity_status: RuntimeIdentityStatus = "unchecked"
    runtime_identity_detail: str = ""
    trust_state: FreshnessStatus = "missing"
    provenance: DataProvenance = Field(
        default_factory=lambda: _missing_provenance(
            "Launchplane has no runtime identity observation for this environment."
        )
    )


class ProductObservedIngress(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = "missing"
    failure_code: str = ""
    observed_at: str = ""
    record_id: str = ""
    summary: str = ""
    incident_status: str = ""
    incident_id: str = ""
    expected_runtime_identity: RuntimeIdentity | None = None
    observed_runtime_identity: RuntimeIdentity | None = None
    runtime_identity_status: RuntimeIdentityStatus = "unchecked"
    runtime_identity_detail: str = ""
    stale_after: str = ""
    trust_state: FreshnessStatus = "missing"
    provenance: DataProvenance = Field(
        default_factory=lambda: _missing_provenance(
            "Launchplane has no public ingress observation for this environment."
        )
    )


class ProductObservedTlsDomain(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain_name: str
    role: ProductTopologyDomainRole = "primary"
    status: ProductObservedTlsStatus = "missing"
    failure_code: str = ""
    summary: str = ""
    likely_failure_cause: str = ""
    issuer: str = ""
    subject: str = ""
    not_before: str = ""
    not_after: str = ""
    days_remaining: int | None = None
    public_name_match: bool = False
    public_name_match_source: str = "none"
    presented_san_count: int = Field(default=0, ge=0)
    presented_name_evidence: tuple[str, ...] = ()
    validated_address_count: int = Field(default=0, ge=0)
    observed_at: str = ""
    stale_after: str = ""
    record_id: str = ""
    incident_status: str = ""
    incident_id: str = ""
    trust_state: FreshnessStatus = "missing"
    provenance: DataProvenance = Field(
        default_factory=lambda: _missing_provenance(
            "Launchplane has no TLS observation for this domain."
        )
    )


class ProductObservedTopology(BaseModel):
    model_config = ConfigDict(extra="forbid")

    placement: ProductObservedPlacement = Field(default_factory=ProductObservedPlacement)
    ingress: ProductObservedIngress = Field(default_factory=ProductObservedIngress)
    tls_domains: tuple[ProductObservedTlsDomain, ...] = ()
    trust_state: FreshnessStatus = "missing"


class ProductTopologyWarning(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: ProductTopologyWarningCode
    scope: ProductTopologyWarningScope
    severity: ProductTopologyWarningSeverity
    detail: str
    domain_name: str = ""


class ProductEnvironmentTopology(BaseModel):
    model_config = ConfigDict(extra="forbid")

    desired: ProductDesiredTopology = Field(default_factory=ProductDesiredTopology)
    provider_recorded: ProductProviderRecordedTopology = Field(
        default_factory=ProductProviderRecordedTopology
    )
    observed: ProductObservedTopology = Field(default_factory=ProductObservedTopology)
    warnings: tuple[ProductTopologyWarning, ...] = ()
    trust_state: FreshnessStatus = "missing"


@dataclass(frozen=True)
class _ObservedTlsEvidence:
    projection: ProductObservedTlsDomain
    target: PublicIngressTargetObservation
    tls: PublicIngressTlsObservation


def build_product_environment_topology(
    *,
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    lane_summary: LaunchplaneLaneSummary | None,
    now: datetime | None = None,
) -> ProductEnvironmentTopology:
    evaluated_at = now or datetime.now(timezone.utc)
    desired = _desired_topology(profile=profile, lane=lane)
    route_binding = _read_route_binding_record(
        record_store=record_store,
        product=profile.product,
        context=lane.context,
        instance=lane.instance,
    )
    provider_recorded = _provider_recorded_topology(
        route_binding=route_binding,
        lane_summary=lane_summary,
        now=evaluated_at,
    )
    observed, tls_evidence = _observed_topology(
        record_store=record_store,
        profile=profile,
        lane=lane,
        lane_summary=lane_summary,
        desired=desired,
        provider_recorded=provider_recorded,
        now=evaluated_at,
    )
    warnings = _topology_warnings(
        lane=lane,
        desired=desired,
        route_binding=route_binding,
        provider_recorded=provider_recorded,
        observed=observed,
        lane_summary=lane_summary,
        tls_evidence=tls_evidence,
        now=evaluated_at,
    )
    return ProductEnvironmentTopology(
        desired=desired,
        provider_recorded=provider_recorded,
        observed=observed,
        warnings=warnings,
        trust_state=_combine_trust_states(
            (
                desired.trust_state,
                provider_recorded.trust_state,
                observed.trust_state,
            ),
            fallback="missing",
        ),
    )


def _desired_topology(
    *, profile: LaunchplaneProductProfileRecord, lane: ProductLaneProfile
) -> ProductDesiredTopology:
    domains = _desired_domains(lane)
    trust_state: FreshnessStatus = "recorded" if lane.base_url or lane.health_url else "missing"
    return ProductDesiredTopology(
        base_url=lane.base_url,
        health_url=lane.health_url,
        domains=domains,
        trust_state=trust_state,
        provenance=DataProvenance(
            source_kind="record",
            source_record_id=profile.product,
            recorded_at=profile.updated_at,
            refreshed_at=profile.updated_at,
            freshness_status=trust_state,
            detail="Launchplane product-profile URL intent for this environment.",
        ),
    )


def _desired_domains(lane: ProductLaneProfile) -> tuple[ProductTopologyDomain, ...]:
    domains: dict[str, ProductTopologyDomain] = {}
    url_sources: tuple[tuple[ProductTopologyDomainRole, str], ...] = (
        ("primary", lane.base_url),
        ("alias", lane.health_url),
    )
    for role, value in url_sources:
        domain_name, tls_expected = _url_domain(value)
        if not domain_name:
            continue
        existing = domains.get(domain_name)
        if existing is None:
            domains[domain_name] = ProductTopologyDomain(
                domain_name=domain_name,
                role=role,
                tls_expected=tls_expected,
            )
            continue
        domains[domain_name] = existing.model_copy(
            update={
                "role": "primary" if existing.role == "primary" or role == "primary" else "alias",
                "tls_expected": existing.tls_expected or tls_expected,
            }
        )
    return tuple(domains.values())


def _url_domain(value: str) -> tuple[str, bool]:
    normalized_value = value.strip()
    if not normalized_value:
        return "", False
    try:
        parsed = urlsplit(normalized_value)
        domain_name = (parsed.hostname or "").strip().rstrip(".").lower()
    except ValueError:
        return "", False
    return domain_name, parsed.scheme.lower() == "https"


def _read_route_binding_record(
    *, record_store: object, product: str, context: str, instance: str
) -> EnvironmentRouteBindingRecord | None:
    method = getattr(record_store, "read_route_binding_record", None)
    if not callable(method):
        return None
    try:
        record = method(
            product=product,
            context_name=context,
            instance_name=instance,
        )
    except FileNotFoundError:
        return None
    if not isinstance(record, EnvironmentRouteBindingRecord):
        return None
    return record


def _provider_recorded_topology(
    *,
    route_binding: EnvironmentRouteBindingRecord | None,
    lane_summary: LaunchplaneLaneSummary | None,
    now: datetime,
) -> ProductProviderRecordedTopology:
    if route_binding is None:
        return ProductProviderRecordedTopology()
    trust_state = _effective_freshness(
        route_binding.source.freshness_status,
        stale_after=route_binding.source.stale_after,
        now=now,
    )
    provenance = DataProvenance(
        source_kind="record",
        source_record_id=route_binding.binding_key,
        recorded_at=route_binding.updated_at,
        refreshed_at=route_binding.source.refreshed_at,
        freshness_status=trust_state,
        stale_after=route_binding.source.stale_after,
        detail="Launchplane provider-neutral environment route-binding record.",
    )
    placement = ProductTopologyPlacement(
        provider=route_binding.provider_target.provider_id,
        target_type=route_binding.provider_target.target_category,
        target_name=route_binding.provider_target.target_name,
        provider_target_type=route_binding.provider_target.provider_target_type,
        provider_target_record_present=bool(
            lane_summary is not None and lane_summary.provider_target is not None
        ),
        trust_state=trust_state,
        provenance=provenance,
    )
    ingress = ProductTopologyIngress(
        provider=route_binding.ingress.provider,
        endpoint_key=route_binding.ingress.endpoint_key,
        termination_kind=route_binding.ingress.termination_kind,
        path=_ingress_path(route_binding.ingress.termination_kind),
        trust_state=trust_state,
        provenance=provenance,
    )
    tls = ProductTopologyTlsOwnership(
        owner=route_binding.tls.owner,
        terminator=_tls_terminator(
            owner=route_binding.tls.owner,
            termination_kind=route_binding.ingress.termination_kind,
        ),
        provider=_tls_terminator_provider(route_binding),
        trust_state=trust_state,
        provenance=provenance,
    )
    return ProductProviderRecordedTopology(
        authority_status=route_binding.status,
        placement=placement,
        domains=tuple(
            ProductTopologyDomain(
                domain_name=domain.domain_name,
                role=domain.role,
                tls_expected=route_binding.tls.owner != "none",
            )
            for domain in route_binding.domains
        ),
        ingress=ingress,
        tls=tls,
        trust_state=trust_state,
        provenance=provenance,
    )


def _ingress_path(termination_kind: RouteBindingTerminationKind) -> ProductTopologyIngressPath:
    if termination_kind == "edge":
        return "edge_to_provider"
    if termination_kind == "direct":
        return "direct_to_provider"
    return "none"


def _tls_terminator(
    *, owner: RouteBindingTlsOwner, termination_kind: RouteBindingTerminationKind
) -> ProductTopologyTlsTerminator:
    if owner == "external":
        return "external"
    if owner == "none":
        return "none"
    if termination_kind == "edge":
        return "edge"
    if termination_kind == "direct":
        return "provider"
    return "unknown"


def _tls_terminator_provider(route_binding: EnvironmentRouteBindingRecord) -> str:
    if route_binding.tls.owner == "external":
        return ""
    if route_binding.ingress.termination_kind == "edge":
        return route_binding.ingress.provider
    if route_binding.ingress.termination_kind == "direct":
        return route_binding.provider_target.provider_id
    return ""


def _observed_topology(
    *,
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    lane_summary: LaunchplaneLaneSummary | None,
    desired: ProductDesiredTopology,
    provider_recorded: ProductProviderRecordedTopology,
    now: datetime,
) -> tuple[ProductObservedTopology, tuple[_ObservedTlsEvidence, ...]]:
    placement = _observed_placement(lane_summary)
    ingress = _observed_ingress(
        record_store=record_store,
        product=profile.product,
        context=lane.context,
        instance=lane.instance,
        now=now,
    )
    domain_roles: dict[str, ProductTopologyDomainRole] = {
        domain.domain_name: domain.role for domain in desired.domains
    }
    for domain in provider_recorded.domains:
        domain_roles[domain.domain_name] = domain.role
    tls_evidence = tuple(
        evidence
        for domain_name, role in domain_roles.items()
        if (
            evidence := _observed_tls_evidence(
                record_store=record_store,
                product=profile.product,
                context=lane.context,
                instance=lane.instance,
                domain_name=domain_name,
                role=role,
                now=now,
            )
        )
        is not None
    )
    expected_tls_domains = {
        domain.domain_name for domain in desired.domains if domain.tls_expected
    } | {domain.domain_name for domain in provider_recorded.domains if domain.tls_expected}
    states: list[FreshnessStatus] = []
    if placement.runtime_identity_status != "unchecked":
        states.append(placement.trust_state)
    if ingress.status != "missing":
        states.append(ingress.trust_state)
    observed_tls_by_domain = {
        evidence.projection.domain_name: evidence.projection for evidence in tls_evidence
    }
    for domain_name in expected_tls_domains:
        observation = observed_tls_by_domain.get(domain_name)
        states.append(observation.trust_state if observation is not None else "missing")
    return (
        ProductObservedTopology(
            placement=placement,
            ingress=ingress,
            tls_domains=tuple(evidence.projection for evidence in tls_evidence),
            trust_state=_combine_trust_states(states, fallback="missing"),
        ),
        tls_evidence,
    )


def _observed_placement(
    lane_summary: LaunchplaneLaneSummary | None,
) -> ProductObservedPlacement:
    if lane_summary is None:
        return ProductObservedPlacement()
    expected_identity: RuntimeIdentity | None = None
    observed_identity: RuntimeIdentity | None = None
    status: RuntimeIdentityStatus = "unchecked"
    detail = ""
    if lane_summary.inventory is not None:
        expected_identity = lane_summary.inventory.runtime_identity
        observed_identity = lane_summary.inventory.destination_health.observed_runtime_identity
        status = lane_summary.inventory.destination_health.runtime_identity_status
        detail = lane_summary.inventory.destination_health.runtime_identity_detail
    elif lane_summary.latest_deployment is not None:
        expected_identity = lane_summary.latest_deployment.runtime_identity
        observed_identity = (
            lane_summary.latest_deployment.destination_health.observed_runtime_identity
        )
        status = lane_summary.latest_deployment.destination_health.runtime_identity_status
        detail = lane_summary.latest_deployment.destination_health.runtime_identity_detail
    if status == "unchecked":
        trust_state: FreshnessStatus = "missing"
    else:
        trust_state = lane_summary.provenance.freshness_status
        if trust_state in {"recorded", "verified"}:
            trust_state = "verified"
    return ProductObservedPlacement(
        expected_runtime_identity=expected_identity,
        observed_runtime_identity=observed_identity,
        runtime_identity_status=status,
        runtime_identity_detail=detail,
        trust_state=trust_state,
        provenance=lane_summary.provenance,
    )


def _observed_ingress(
    *,
    record_store: object,
    product: str,
    context: str,
    instance: str,
    now: datetime,
) -> ProductObservedIngress:
    records = _optional_records(
        record_store,
        "list_public_ingress_observation_records",
        product=product,
        context_name=context,
        instance_name=instance,
        check_kind="public_http",
        limit=50,
    )
    observations = tuple(
        record for record in records if isinstance(record, PublicIngressObservationRecord)
    )
    latest = next(
        (record for record in observations if _public_runtime_identity_target(record) is not None),
        observations[0] if observations else None,
    )
    if latest is None:
        return ProductObservedIngress()
    incident = _latest_open_incident(
        record_store=record_store,
        product=product,
        context=context,
        instance=instance,
        check_name=latest.check_name,
        check_kind="public_http",
    )
    observed_timestamp = _parse_timestamp(latest.observed_at)
    stale_after = (
        observed_timestamp + timedelta(seconds=PUBLIC_HTTP_STALE_AFTER_SECONDS)
        if observed_timestamp is not None
        else None
    )
    stale_after_value = (
        stale_after.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
        if stale_after is not None
        else ""
    )
    if latest.status == "skipped":
        trust_state: FreshnessStatus = "unsupported"
    elif observed_timestamp is None:
        trust_state = "stale"
    else:
        trust_state = _effective_freshness(
            "verified",
            stale_after=stale_after_value,
            now=now,
        )
    runtime_target = _public_runtime_identity_target(latest)
    provenance = DataProvenance(
        source_kind="provider",
        source_record_id=latest.record_id,
        recorded_at=latest.observed_at,
        refreshed_at=latest.observed_at,
        freshness_status=trust_state,
        stale_after=stale_after_value,
        detail="Launchplane public ingress active observation.",
    )
    return ProductObservedIngress(
        status=latest.status,
        failure_code=latest.failure_code or "",
        observed_at=latest.observed_at,
        record_id=latest.record_id,
        summary=latest.summary,
        incident_status=incident.status if incident is not None else "",
        incident_id=incident.incident_id if incident is not None else "",
        expected_runtime_identity=latest.expected_runtime_identity,
        observed_runtime_identity=(
            runtime_target.observed_runtime_identity if runtime_target is not None else None
        ),
        runtime_identity_status=(
            runtime_target.runtime_identity_status if runtime_target is not None else "unchecked"
        ),
        runtime_identity_detail=(
            runtime_target.runtime_identity_detail if runtime_target is not None else ""
        ),
        stale_after=stale_after_value,
        trust_state=trust_state,
        provenance=provenance,
    )


def _public_runtime_identity_target(
    record: PublicIngressObservationRecord,
) -> PublicIngressTargetObservation | None:
    return next(
        (
            target
            for target_kind in ("health_url", "base_url")
            for target in record.targets
            if target.target == target_kind and target.runtime_identity_status != "unchecked"
        ),
        None,
    )


def _observed_tls_evidence(
    *,
    record_store: object,
    product: str,
    context: str,
    instance: str,
    domain_name: str,
    role: ProductTopologyDomainRole,
    now: datetime,
) -> _ObservedTlsEvidence | None:
    check_name = build_public_ingress_tls_check_name(domain_name)
    records = _optional_records(
        record_store,
        "list_public_ingress_observation_records",
        product=product,
        context_name=context,
        instance_name=instance,
        check_name=check_name,
        check_kind="tls",
        limit=1,
    )
    latest = next(
        (record for record in records if isinstance(record, PublicIngressObservationRecord)),
        None,
    )
    if latest is None:
        return None
    target = next(
        (
            candidate
            for candidate in latest.targets
            if candidate.target == "tls_domain"
            and candidate.tls is not None
            and candidate.tls.public_name == domain_name
        ),
        None,
    )
    if target is None or target.tls is None:
        return None
    tls = target.tls
    incident = _latest_open_incident(
        record_store=record_store,
        product=product,
        context=context,
        instance=instance,
        check_name=latest.check_name,
        check_kind="tls",
    )
    trust_state = _tls_probe_trust_state(tls=tls, now=now)
    provenance = DataProvenance(
        source_kind="provider",
        source_record_id=latest.record_id,
        recorded_at=tls.probe.observed_at,
        refreshed_at=tls.probe.observed_at,
        freshness_status=trust_state,
        stale_after=tls.probe.stale_after,
        detail="Launchplane active TLS certificate observation.",
    )
    projection = ProductObservedTlsDomain(
        domain_name=tls.public_name,
        role=tls.recorded.domain_role or role,
        status=tls.status,
        failure_code=target.failure_code or "",
        summary=target.summary,
        likely_failure_cause=_tls_likely_failure_cause(
            status=tls.status,
            failure_code=target.failure_code or "",
        ),
        issuer=tls.issuer,
        subject=tls.subject,
        not_before=tls.not_before,
        not_after=tls.not_after,
        days_remaining=tls.days_remaining,
        public_name_match=tls.public_name_match,
        public_name_match_source=tls.public_name_match_source,
        presented_san_count=tls.presented_san_count,
        presented_name_evidence=tls.presented_name_evidence,
        validated_address_count=tls.probe.validated_address_count,
        observed_at=tls.probe.observed_at,
        stale_after=tls.probe.stale_after,
        record_id=latest.record_id,
        incident_status=incident.status if incident is not None else "",
        incident_id=(incident.incident_id if incident is not None else tls.incident_id),
        trust_state=trust_state,
        provenance=provenance,
    )
    return _ObservedTlsEvidence(projection=projection, target=target, tls=tls)


def _latest_open_incident(
    *,
    record_store: object,
    product: str,
    context: str,
    instance: str,
    check_name: str,
    check_kind: str,
) -> PublicIngressIncidentRecord | None:
    records = _optional_records(
        record_store,
        "list_public_ingress_incident_records",
        product=product,
        context_name=context,
        instance_name=instance,
        check_name=check_name,
        check_kind=check_kind,
        status="open",
        limit=1,
    )
    return next(
        (record for record in records if isinstance(record, PublicIngressIncidentRecord)),
        None,
    )


def _topology_warnings(
    *,
    lane: ProductLaneProfile,
    desired: ProductDesiredTopology,
    route_binding: EnvironmentRouteBindingRecord | None,
    provider_recorded: ProductProviderRecordedTopology,
    observed: ProductObservedTopology,
    lane_summary: LaunchplaneLaneSummary | None,
    tls_evidence: tuple[_ObservedTlsEvidence, ...],
    now: datetime,
) -> tuple[ProductTopologyWarning, ...]:
    warnings: list[ProductTopologyWarning] = []
    if (lane.base_url or lane.health_url) and not desired.domains:
        warnings.append(
            _warning(
                code="desired_domain_unknown",
                scope="domains",
                severity="error",
                detail="The product profile declares URLs whose public domain could not be parsed.",
            )
        )
    if route_binding is None:
        warnings.extend(
            (
                _warning(
                    code="missing_route_authority",
                    scope="authority",
                    severity="error",
                    detail=(
                        "No provider-neutral route-binding record exists; provider records are not "
                        "used as reassuring topology authority."
                    ),
                ),
                _warning(
                    code="ingress_ownership_unknown",
                    scope="ingress",
                    severity="error",
                    detail="Ingress ownership and termination are unknown without route authority.",
                ),
                _warning(
                    code="tls_ownership_unknown",
                    scope="tls",
                    severity="error",
                    detail="TLS ownership and termination are unknown without route authority.",
                ),
            )
        )
    else:
        if route_binding.status == "disabled":
            warnings.append(
                _warning(
                    code="route_authority_disabled",
                    scope="authority",
                    severity="error",
                    detail="The provider-neutral route-binding record is disabled.",
                )
            )
        if provider_recorded.trust_state == "stale":
            warnings.append(
                _warning(
                    code="stale_route_authority",
                    scope="authority",
                    severity=(
                        "error" if route_binding.ingress.provider == "external" else "warning"
                    ),
                    detail="The provider-neutral route-binding evidence is stale.",
                )
            )
        desired_names = {domain.domain_name for domain in desired.domains}
        recorded_names = {domain.domain_name for domain in provider_recorded.domains}
        if desired_names and desired_names != recorded_names:
            warnings.append(
                _warning(
                    code="domain_divergence",
                    scope="domains",
                    severity="error",
                    detail=(
                        "Product-profile domains and provider-recorded route-binding domains "
                        f"diverge (desired={_domain_list(desired_names)}; "
                        f"recorded={_domain_list(recorded_names)})."
                    ),
                )
            )
        if route_binding.ingress.provider == "none":
            warnings.append(
                _warning(
                    code="ingress_ownership_unknown",
                    scope="ingress",
                    severity="warning",
                    detail="The route binding records no ingress owner or termination path.",
                )
            )
        if route_binding.tls.owner == "none" and any(
            domain.tls_expected for domain in desired.domains
        ):
            warnings.append(
                _warning(
                    code="tls_ownership_unknown",
                    scope="tls",
                    severity="error",
                    detail="HTTPS is desired but the route binding records no TLS owner.",
                )
            )
        warnings.extend(
            _placement_warnings(
                route_binding=route_binding,
                lane_summary=lane_summary,
                observed=observed,
            )
        )
        if route_binding.ingress.provider == "external":
            warnings.append(
                _warning(
                    code="external_ingress_internals_unsupported",
                    scope="ingress",
                    severity="warning",
                    detail=(
                        "Ingress is externally managed; Launchplane verifies public behavior "
                        "but does not claim access to the proxy's internal configuration."
                    ),
                )
            )
            if observed.ingress.status == "missing":
                warnings.append(
                    _warning(
                        code="public_ingress_observation_missing",
                        scope="observation",
                        severity="error",
                        detail=(
                            "Externally managed ingress has no public HTTP observation for "
                            "the requested lane."
                        ),
                    )
                )
            elif observed.ingress.trust_state == "stale":
                warnings.append(
                    _warning(
                        code="stale_public_ingress_observation",
                        scope="observation",
                        severity="error",
                        detail=(
                            "The latest public HTTP observation for externally managed ingress "
                            "is stale."
                        ),
                    )
                )
            if (
                observed.ingress.status != "missing"
                and observed.ingress.runtime_identity_status != "match"
            ):
                warnings.append(
                    _warning(
                        code="public_runtime_identity_unverified",
                        scope="observation",
                        severity="error",
                        detail=(
                            "The public ingress observation did not prove the exact expected "
                            "runtime identity."
                        ),
                    )
                )

    observed_tls_by_domain = {
        evidence.projection.domain_name: evidence for evidence in tls_evidence
    }
    expected_tls_domains = {
        domain.domain_name for domain in desired.domains if domain.tls_expected
    } | {domain.domain_name for domain in provider_recorded.domains if domain.tls_expected}
    for domain_name in sorted(expected_tls_domains):
        if domain_name not in observed_tls_by_domain:
            warnings.append(
                _warning(
                    code="tls_observation_missing",
                    scope="tls",
                    severity=(
                        "error"
                        if route_binding is not None
                        and route_binding.ingress.provider == "external"
                        else "warning"
                    ),
                    domain_name=domain_name,
                    detail="No TLS certificate observation is recorded for the expected domain.",
                )
            )
    for evidence in tls_evidence:
        warnings.extend(
            _tls_warnings(
                route_binding=route_binding,
                evidence=evidence,
                now=now,
            )
        )
    if observed.ingress.status == "fail":
        warnings.append(
            _warning(
                code="public_ingress_failure",
                scope="observation",
                severity="error",
                detail="The latest public ingress observation failed.",
            )
        )
    return _deduplicated_warnings(warnings)


def _placement_warnings(
    *,
    route_binding: EnvironmentRouteBindingRecord,
    lane_summary: LaunchplaneLaneSummary | None,
    observed: ProductObservedTopology,
) -> tuple[ProductTopologyWarning, ...]:
    warnings: list[ProductTopologyWarning] = []
    provider_target = lane_summary.provider_target if lane_summary is not None else None
    if provider_target is None:
        warnings.append(
            _warning(
                code="provider_placement_missing",
                scope="placement",
                severity="warning",
                detail=(
                    "Route authority names a provider target, but no physical provider-target "
                    "record corroborates it."
                ),
            )
        )
    elif (
        provider_target.provider_id != route_binding.provider_target.provider_id
        or provider_target.target_category != route_binding.provider_target.target_category
        or provider_target.provider_target_type
        != route_binding.provider_target.provider_target_type
        or provider_target.display_name != route_binding.provider_target.target_name
    ):
        warnings.append(
            _warning(
                code="placement_divergence",
                scope="placement",
                severity="error",
                detail=(
                    "Provider-neutral route authority and the physical provider-target record "
                    "disagree on provider, category, type, or display name."
                ),
            )
        )
    if observed.placement.runtime_identity_status in {
        "mismatch",
        "missing",
        "malformed",
        "unverifiable",
    }:
        warnings.append(
            _warning(
                code="placement_divergence",
                scope="placement",
                severity="error",
                detail=(
                    "Observed runtime identity does not verify the expected environment placement."
                ),
            )
        )
    return tuple(warnings)


def _tls_warnings(
    *,
    route_binding: EnvironmentRouteBindingRecord | None,
    evidence: _ObservedTlsEvidence,
    now: datetime,
) -> tuple[ProductTopologyWarning, ...]:
    projection = evidence.projection
    tls = evidence.tls
    warnings: list[ProductTopologyWarning] = []
    if projection.trust_state == "stale":
        warnings.append(
            _warning(
                code="stale_tls_observation",
                scope="tls",
                severity=(
                    "error"
                    if route_binding is not None and route_binding.ingress.provider == "external"
                    else "warning"
                ),
                domain_name=projection.domain_name,
                detail="The latest TLS observation is stale.",
            )
        )
    if route_binding is not None:
        recorded_domains = {domain.domain_name: domain.role for domain in route_binding.domains}
        if recorded_domains.get(tls.recorded.domain_name) != tls.recorded.domain_role:
            warnings.append(
                _warning(
                    code="domain_divergence",
                    scope="domains",
                    severity="error",
                    domain_name=projection.domain_name,
                    detail=(
                        "The TLS observation's recorded domain snapshot diverges from current "
                        "route authority."
                    ),
                )
            )
        if (
            tls.recorded.ingress_provider != route_binding.ingress.provider
            or tls.recorded.termination_kind != route_binding.ingress.termination_kind
        ):
            warnings.append(
                _warning(
                    code="ingress_divergence",
                    scope="ingress",
                    severity="error",
                    domain_name=projection.domain_name,
                    detail=(
                        "The TLS observation's recorded ingress snapshot diverges from current "
                        "route authority."
                    ),
                )
            )
        if tls.recorded.owner != route_binding.tls.owner:
            warnings.append(
                _warning(
                    code="tls_ownership_divergence",
                    scope="tls",
                    severity="error",
                    domain_name=projection.domain_name,
                    detail=(
                        "The TLS observation's recorded ownership snapshot diverges from current "
                        "route authority."
                    ),
                )
            )
        if (
            _effective_freshness(
                tls.recorded.freshness_status,
                stale_after=tls.recorded.stale_after,
                now=now,
            )
            == "stale"
        ):
            warnings.append(
                _warning(
                    code="stale_route_authority",
                    scope="authority",
                    severity="warning",
                    domain_name=projection.domain_name,
                    detail="The route-binding snapshot captured by the TLS observation is stale.",
                )
            )
    status = projection.status
    if status == "hostname_mismatch":
        warnings.append(
            _warning(
                code="tls_mismatch",
                scope="tls",
                severity="error",
                domain_name=projection.domain_name,
                detail=(
                    "The TLS terminator presented a certificate that does not cover the requested "
                    "public domain."
                ),
            )
        )
    elif status == "expiring":
        warnings.append(
            _warning(
                code="tls_expiring",
                scope="tls",
                severity="warning",
                domain_name=projection.domain_name,
                detail="The observed certificate is within the rotation window.",
            )
        )
    elif status == "expired":
        warnings.append(
            _warning(
                code="tls_expired",
                scope="tls",
                severity="error",
                domain_name=projection.domain_name,
                detail="The TLS terminator presented an expired certificate.",
            )
        )
    elif status in {"untrusted", "self_signed"}:
        warnings.append(
            _warning(
                code="tls_untrusted",
                scope="tls",
                severity="error",
                domain_name=projection.domain_name,
                detail="The TLS terminator presented an untrusted certificate chain.",
            )
        )
    elif status in {"unreachable", "unknown"}:
        warnings.append(
            _warning(
                code="tls_unavailable",
                scope="tls",
                severity="error",
                domain_name=projection.domain_name,
                detail="Launchplane could not verify the TLS terminator for the public domain.",
            )
        )
    elif status == "unsupported":
        warnings.append(
            _warning(
                code="tls_unsupported",
                scope="tls",
                severity="warning",
                domain_name=projection.domain_name,
                detail="The TLS endpoint requires an unsupported protocol or capability.",
            )
        )
    return tuple(warnings)


def _warning(
    *,
    code: ProductTopologyWarningCode,
    scope: ProductTopologyWarningScope,
    severity: ProductTopologyWarningSeverity,
    detail: str,
    domain_name: str = "",
) -> ProductTopologyWarning:
    return ProductTopologyWarning(
        code=code,
        scope=scope,
        severity=severity,
        detail=detail,
        domain_name=domain_name,
    )


def _deduplicated_warnings(
    warnings: list[ProductTopologyWarning],
) -> tuple[ProductTopologyWarning, ...]:
    deduplicated: list[ProductTopologyWarning] = []
    seen: set[tuple[str, str, str]] = set()
    for warning in warnings:
        key = (warning.code, warning.domain_name, warning.detail)
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(warning)
    return tuple(deduplicated)


def _domain_list(domains: set[str]) -> str:
    return ", ".join(sorted(domains)) or "none"


def _tls_probe_trust_state(*, tls: PublicIngressTlsObservation, now: datetime) -> FreshnessStatus:
    effective = _effective_freshness(
        tls.probe.freshness_status,
        stale_after=tls.probe.stale_after,
        now=now,
    )
    if effective in {"stale", "missing", "unsupported"}:
        return effective
    if tls.status == "unsupported":
        return "unsupported"
    if tls.status == "unknown":
        return "recorded"
    return "verified"


def _tls_likely_failure_cause(*, status: PublicIngressTlsStatus, failure_code: str) -> str:
    if status == "valid":
        return ""
    if status == "expiring":
        return "The certificate is valid but needs rotation before its validity window closes."
    if status == "expired":
        return "The configured TLS terminator is serving an expired certificate."
    if status == "hostname_mismatch":
        return (
            "The domain is reaching a TLS terminator whose certificate binding does not include "
            "the requested public name."
        )
    if status == "untrusted":
        return "The TLS terminator is serving a certificate chain the public probe cannot trust."
    if status == "self_signed":
        return "The TLS terminator is serving a self-signed certificate."
    if status == "unreachable":
        if failure_code == "dns_failure":
            return "The public domain did not resolve to a reachable TLS endpoint."
        if failure_code == "connection_timeout":
            return (
                "The public domain resolved, but the TLS endpoint did not respond before timeout."
            )
        return "The public domain could not complete a TLS connection to its recorded terminator."
    if status == "unsupported":
        return "The endpoint requires a TLS protocol or capability Launchplane does not support."
    return "The active TLS probe could not classify the certificate or termination failure."


def _effective_freshness(
    status: FreshnessStatus, *, stale_after: str, now: datetime
) -> FreshnessStatus:
    if status in {"missing", "unsupported", "stale"}:
        return status
    stale_at = _parse_timestamp(stale_after)
    if stale_at is not None and now.astimezone(timezone.utc) > stale_at:
        return "stale"
    return status


def _parse_timestamp(value: str) -> datetime | None:
    normalized_value = value.strip()
    if not normalized_value:
        return None
    if normalized_value.endswith("Z"):
        normalized_value = f"{normalized_value[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized_value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _optional_records(
    record_store: object, method_name: str, **kwargs: object
) -> tuple[object, ...]:
    method = getattr(record_store, method_name, None)
    if not callable(method):
        return ()
    return tuple(method(**kwargs))


def _combine_trust_states(
    states: list[FreshnessStatus] | tuple[FreshnessStatus, ...],
    *,
    fallback: FreshnessStatus,
) -> FreshnessStatus:
    ordered_states = tuple(state for state in states if state)
    if not ordered_states:
        return fallback
    for status in ("missing", "stale", "recorded", "verified", "unsupported"):
        if status in ordered_states:
            return status
    return fallback
