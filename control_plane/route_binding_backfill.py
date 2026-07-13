from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.edge_endpoint_record import EdgeEndpointRecord
from control_plane.contracts.ingress_route_audit_record import IngressRouteAuditRecord
from control_plane.contracts.route_binding_record import (
    EnvironmentRouteBindingRecord,
    RouteBindingDomain,
    RouteBindingIngress,
    RouteBindingProviderTarget,
    RouteBindingSource,
    RouteBindingTls,
)


_EVIDENCE_SCAN_LIMIT = 1000
_EVIDENCE_MAX_AGE = timedelta(hours=24)


class RouteBindingBackfillStore(Protocol):
    def read_provider_target_record(
        self, *, context_name: str, instance_name: str
    ) -> ProviderTargetRecord: ...

    def read_dokploy_target_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetRecord: ...

    def read_dokploy_target_id_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetIdRecord: ...

    def list_edge_endpoint_records(
        self,
        *,
        provider: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[EdgeEndpointRecord, ...]: ...

    def list_ingress_route_audit_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        limit: int | None = None,
    ) -> tuple[IngressRouteAuditRecord, ...]: ...


class RouteBindingBackfillFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    detail: str


class RouteBindingBackfillRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    context: str
    instance: str
    source_label: str = "operator-backfill"
    evaluated_at: str

    @model_validator(mode="after")
    def _validate_request(self) -> "RouteBindingBackfillRequest":
        if self.schema_version != 1:
            raise ValueError("Unsupported route binding backfill schema version")
        self.product = _required_text(self.product, "route binding backfill requires product")
        self.context = _required_text(self.context, "route binding backfill requires context")
        self.instance = _required_text(self.instance, "route binding backfill requires instance")
        self.source_label = _required_text(
            self.source_label, "route binding backfill requires source_label"
        )
        self.evaluated_at = _required_text(
            self.evaluated_at, "route binding backfill requires evaluated_at"
        )
        _parse_utc_timestamp(self.evaluated_at)
        return self


class RouteBindingBackfillPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    findings: tuple[RouteBindingBackfillFinding, ...] = ()
    record: EnvironmentRouteBindingRecord | None = None


def plan_route_binding_backfill(
    *,
    record_store: RouteBindingBackfillStore,
    request: RouteBindingBackfillRequest,
) -> RouteBindingBackfillPlan:
    findings: list[RouteBindingBackfillFinding] = []
    provider_target = _read_provider_target(
        record_store=record_store, request=request, findings=findings
    )
    dokploy_target = _read_dokploy_target(
        record_store=record_store, request=request, findings=findings
    )
    dokploy_target_id = _read_dokploy_target_id(
        record_store=record_store, request=request, findings=findings
    )
    if provider_target is None or dokploy_target is None or dokploy_target_id is None:
        return RouteBindingBackfillPlan(status="blocked", findings=tuple(findings))
    expected_provider_target = ProviderTargetRecord.from_dokploy_records(
        target_record=dokploy_target,
        target_id_record=dokploy_target_id,
    )
    if _provider_target_projection(provider_target) != _provider_target_projection(
        expected_provider_target
    ):
        return RouteBindingBackfillPlan(
            status="blocked",
            findings=(
                RouteBindingBackfillFinding(
                    code="provider_target_projection_conflict",
                    detail=(
                        "The provider-target record does not match the canonical projection "
                        "of the tracked Dokploy target and target-id records."
                    ),
                ),
            ),
        )

    domains, domain_finding = _normalized_domains(dokploy_target.domains)
    if domain_finding is not None:
        findings.append(domain_finding)
        return RouteBindingBackfillPlan(status="blocked", findings=tuple(findings))
    if not domains:
        findings.append(
            RouteBindingBackfillFinding(
                code="domains_missing",
                detail="Tracked provider target has no recorded domains for this environment.",
            )
        )
        return RouteBindingBackfillPlan(status="blocked", findings=tuple(findings))

    edge_endpoint = _resolve_edge_endpoint(
        record_store=record_store,
        provider_target=provider_target,
        findings=findings,
    )
    audit_record = _resolve_ingress_audit(
        record_store=record_store,
        request=request,
        domains=domains,
        findings=findings,
    )
    if edge_endpoint is None or audit_record is None:
        return RouteBindingBackfillPlan(status="blocked", findings=tuple(findings))
    if audit_record.edge_endpoint_key != edge_endpoint.endpoint_key:
        return RouteBindingBackfillPlan(
            status="blocked",
            findings=(
                *findings,
                RouteBindingBackfillFinding(
                    code="ingress_edge_endpoint_conflict",
                    detail=(
                        "The matching ingress audit points at a different edge endpoint than "
                        "the resolved active edge endpoint."
                    ),
                ),
            ),
        )
    if audit_record.tls_owner == "unknown":
        return RouteBindingBackfillPlan(
            status="blocked",
            findings=(
                *findings,
                RouteBindingBackfillFinding(
                    code="tls_ownership_unknown",
                    detail="The matching ingress audit does not record TLS ownership evidence.",
                ),
            ),
        )

    source_record_ids = (
        f"provider-target:{request.context}:{request.instance}",
        f"dokploy-target:{request.context}:{request.instance}",
        f"dokploy-target-id:{request.context}:{request.instance}",
        f"edge-endpoint:{edge_endpoint.endpoint_key}",
        f"ingress-audit:{audit_record.record_id}",
    )
    source_versions, stale_after, freshness_finding = _source_freshness(
        evaluated_at=request.evaluated_at,
        evidence=(
            (source_record_ids[0], provider_target.updated_at),
            (source_record_ids[1], dokploy_target.updated_at),
            (source_record_ids[2], dokploy_target_id.updated_at),
            (source_record_ids[3], edge_endpoint.updated_at),
            (source_record_ids[4], audit_record.recorded_at),
        ),
    )
    if freshness_finding is not None:
        return RouteBindingBackfillPlan(
            status="blocked",
            findings=(*findings, freshness_finding),
        )

    record = EnvironmentRouteBindingRecord(
        product=request.product,
        context=request.context,
        instance=request.instance,
        provider_target=RouteBindingProviderTarget(
            provider_id=provider_target.provider_id,
            target_category=provider_target.target_category,
            provider_target_type=provider_target.provider_target_type,
            target_name=provider_target.display_name,
            provider_evidence={
                "target_record": f"{provider_target.context}:{provider_target.instance}",
            },
        ),
        ingress=RouteBindingIngress(
            provider=audit_record.provider,
            endpoint_key=edge_endpoint.endpoint_key,
            termination_kind="edge",
            provider_evidence={
                "audit_record": audit_record.record_id,
            },
        ),
        domains=tuple(
            RouteBindingDomain(domain_name=domain, role="primary" if index == 0 else "alias")
            for index, domain in enumerate(domains)
        ),
        tls=RouteBindingTls(
            owner=audit_record.tls_owner,
            provider_evidence=(
                {
                    "audit_record": audit_record.record_id,
                    "provider_certificate_ref": audit_record.provider_certificate_ref,
                }
                if audit_record.tls_owner != "none" and audit_record.provider_certificate_ref
                else {}
            ),
        ),
        status="active",
        source=RouteBindingSource(
            source_kind="backfill",
            source_label=request.source_label,
            source_record_ids=source_record_ids,
            source_versions=source_versions,
            refreshed_at=request.evaluated_at,
            freshness_status="recorded",
            stale_after=stale_after,
        ),
        updated_at=request.evaluated_at,
    )
    return RouteBindingBackfillPlan(status="ready", findings=(), record=record)


def _read_provider_target(
    *,
    record_store: RouteBindingBackfillStore,
    request: RouteBindingBackfillRequest,
    findings: list[RouteBindingBackfillFinding],
) -> ProviderTargetRecord | None:
    try:
        return record_store.read_provider_target_record(
            context_name=request.context,
            instance_name=request.instance,
        )
    except FileNotFoundError:
        findings.append(
            RouteBindingBackfillFinding(
                code="provider_target_missing",
                detail="No provider-target record exists for the requested context/instance.",
            )
        )
        return None


def _read_dokploy_target(
    *,
    record_store: RouteBindingBackfillStore,
    request: RouteBindingBackfillRequest,
    findings: list[RouteBindingBackfillFinding],
) -> DokployTargetRecord | None:
    try:
        return record_store.read_dokploy_target_record(
            context_name=request.context,
            instance_name=request.instance,
        )
    except FileNotFoundError:
        findings.append(
            RouteBindingBackfillFinding(
                code="dokploy_target_missing",
                detail="No tracked Dokploy target record exists for the requested context/instance.",
            )
        )
        return None


def _read_dokploy_target_id(
    *,
    record_store: RouteBindingBackfillStore,
    request: RouteBindingBackfillRequest,
    findings: list[RouteBindingBackfillFinding],
) -> DokployTargetIdRecord | None:
    try:
        return record_store.read_dokploy_target_id_record(
            context_name=request.context,
            instance_name=request.instance,
        )
    except FileNotFoundError:
        findings.append(
            RouteBindingBackfillFinding(
                code="dokploy_target_id_missing",
                detail=(
                    "No tracked Dokploy target-id record exists for the requested context/instance."
                ),
            )
        )
        return None


def _resolve_edge_endpoint(
    *,
    record_store: RouteBindingBackfillStore,
    provider_target: ProviderTargetRecord,
    findings: list[RouteBindingBackfillFinding],
) -> EdgeEndpointRecord | None:
    active_endpoints = record_store.list_edge_endpoint_records(
        provider=provider_target.provider_id,
        status="active",
        limit=_EVIDENCE_SCAN_LIMIT + 1,
    )
    if len(active_endpoints) > _EVIDENCE_SCAN_LIMIT:
        findings.append(
            RouteBindingBackfillFinding(
                code="edge_endpoint_scan_limit_exceeded",
                detail=(
                    "Backfill cannot resolve edge endpoint evidence because the bounded "
                    "active-endpoint scan was exhausted."
                ),
            )
        )
        return None
    if not active_endpoints:
        findings.append(
            RouteBindingBackfillFinding(
                code="edge_endpoint_missing",
                detail="No active edge endpoint records are available for Dokploy-backed ingress.",
            )
        )
        return None
    project_name = provider_target.provider_evidence.get("project_name", "").strip()
    if not project_name:
        findings.append(
            RouteBindingBackfillFinding(
                code="edge_endpoint_evidence_missing",
                detail=(
                    "Provider-target evidence does not identify the edge endpoint server name."
                ),
            )
        )
        return None
    candidates = tuple(
        endpoint for endpoint in active_endpoints if endpoint.server_name == project_name
    )
    if len(candidates) != 1:
        findings.append(
            RouteBindingBackfillFinding(
                code="edge_endpoint_ambiguous" if candidates else "edge_endpoint_conflict",
                detail=(
                    "Backfill could not resolve exactly one active edge endpoint for the "
                    "provider target."
                ),
            )
        )
        return None
    return candidates[0]


def _resolve_ingress_audit(
    *,
    record_store: RouteBindingBackfillStore,
    request: RouteBindingBackfillRequest,
    domains: tuple[str, ...],
    findings: list[RouteBindingBackfillFinding],
) -> IngressRouteAuditRecord | None:
    domain_set = set(domains)
    audit_records = record_store.list_ingress_route_audit_records(
        product=request.product,
        context_name=request.context,
        limit=_EVIDENCE_SCAN_LIMIT + 1,
    )
    if len(audit_records) > _EVIDENCE_SCAN_LIMIT:
        findings.append(
            RouteBindingBackfillFinding(
                code="ingress_audit_scan_limit_exceeded",
                detail=(
                    "Backfill cannot resolve ingress evidence because the bounded audit "
                    "scan was exhausted."
                ),
            )
        )
        return None
    matching_records: list[tuple[IngressRouteAuditRecord, datetime]] = []
    for record in audit_records:
        if record.mode != "apply" or not record.edge_endpoint_key.strip():
            continue
        requested_domains, requested_domain_finding = _normalized_domains(record.requested_domains)
        if requested_domain_finding is not None:
            continue
        if set(requested_domains) == domain_set:
            try:
                recorded_at = _parse_utc_timestamp(record.recorded_at)
            except ValueError:
                findings.append(
                    RouteBindingBackfillFinding(
                        code="ingress_audit_timestamp_invalid",
                        detail="A matching ingress audit has an invalid recorded_at timestamp.",
                    )
                )
                return None
            matching_records.append((record, recorded_at))
    if not matching_records:
        findings.append(
            RouteBindingBackfillFinding(
                code="ingress_audit_missing",
                detail=(
                    "Backfill requires exactly one applied ingress audit matching the "
                    "tracked domain set for the requested product/context."
                ),
            )
        )
        return None
    latest_recorded_at = max(recorded_at for _, recorded_at in matching_records)
    latest_records = tuple(
        record for record, recorded_at in matching_records if recorded_at == latest_recorded_at
    )
    if any(record.status not in {"applied", "unchanged"} for record in latest_records):
        findings.append(
            RouteBindingBackfillFinding(
                code="ingress_audit_unresolved",
                detail=(
                    "The latest matching ingress audit is not terminal, so route evidence "
                    "cannot be promoted."
                ),
            )
        )
        return None
    latest_routes = {
        (
            record.provider,
            record.edge_endpoint_key,
            record.tls_owner,
            record.provider_certificate_ref,
        )
        for record in latest_records
    }
    if len(latest_routes) != 1:
        findings.append(
            RouteBindingBackfillFinding(
                code="ingress_audit_ambiguous",
                detail=(
                    "The latest matching ingress audits disagree about provider or edge endpoint."
                ),
            )
        )
        return None
    return max(latest_records, key=lambda record: record.record_id)


def _provider_target_projection(record: ProviderTargetRecord) -> tuple[object, ...]:
    return (
        record.context,
        record.instance,
        record.provider_id,
        record.target_category,
        record.target_id,
        record.display_name,
        record.provider_target_type,
        tuple(sorted(record.provider_evidence.items())),
        record.updated_at,
    )


def _source_freshness(
    *,
    evaluated_at: str,
    evidence: tuple[tuple[str, str], ...],
) -> tuple[dict[str, str], str, RouteBindingBackfillFinding | None]:
    evaluated_timestamp = _parse_utc_timestamp(evaluated_at)
    source_versions: dict[str, str] = {}
    stale_timestamps: list[datetime] = []
    for source_id, version in evidence:
        try:
            source_timestamp = _parse_utc_timestamp(version)
        except ValueError:
            return (
                {},
                "",
                RouteBindingBackfillFinding(
                    code="evidence_timestamp_invalid",
                    detail=f"Route-binding evidence {source_id!r} has an invalid timestamp.",
                ),
            )
        if source_timestamp > evaluated_timestamp:
            return (
                {},
                "",
                RouteBindingBackfillFinding(
                    code="evidence_timestamp_future",
                    detail=f"Route-binding evidence {source_id!r} is timestamped in the future.",
                ),
            )
        stale_timestamp = source_timestamp + _EVIDENCE_MAX_AGE
        if stale_timestamp <= evaluated_timestamp:
            return (
                {},
                "",
                RouteBindingBackfillFinding(
                    code="evidence_stale",
                    detail=(
                        f"Route-binding evidence {source_id!r} is older than the 24-hour "
                        "freshness window."
                    ),
                ),
            )
        source_versions[source_id] = version
        stale_timestamps.append(stale_timestamp)
    return source_versions, _format_utc_timestamp(min(stale_timestamps)), None


def _parse_utc_timestamp(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError("timestamp must be ISO 8601") from error
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _format_utc_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalized_domains(
    raw_domains: tuple[str, ...],
) -> tuple[tuple[str, ...], RouteBindingBackfillFinding | None]:
    domains: list[str] = []
    for raw_domain in raw_domains:
        if not raw_domain.strip():
            continue
        try:
            domain = RouteBindingDomain(domain_name=raw_domain).domain_name
        except (ValueError, ValidationError):
            return (), RouteBindingBackfillFinding(
                code="domains_invalid",
                detail="Tracked route domains must be bare DNS names without schemes, paths, ports, or whitespace.",
            )
        if domain not in domains:
            domains.append(domain)
    return tuple(domains), None


def _required_text(value: str, message: str) -> str:
    normalized_value = value.strip()
    if not normalized_value:
        raise ValueError(message)
    return normalized_value
