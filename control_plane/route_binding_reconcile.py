from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.edge_endpoint_record import EdgeEndpointRecord
from control_plane.contracts.private_health_endpoint_record import PrivateHealthEndpointRecord
from control_plane.contracts.ingress_route_audit_record import (
    IngressRouteAuditRecord,
    TESTING_PRIVATE_INGRESS_AUDIT_PREFIX,
)
from control_plane.contracts.route_binding_record import (
    EnvironmentRouteBindingRecord,
    RouteBindingDomain,
    RouteBindingIngress,
    RouteBindingProviderTarget,
    RouteBindingSource,
    RouteBindingTls,
    route_binding_record_sha256,
)


_EVIDENCE_SCAN_LIMIT = 1000
_EVIDENCE_FRESHNESS_WINDOW = timedelta(hours=24)
_EVIDENCE_REFRESH_THRESHOLD = timedelta(hours=12)


class RouteBindingReconcileStore(Protocol):
    def list_private_health_endpoint_records(self) -> tuple[PrivateHealthEndpointRecord, ...]: ...

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

    def read_route_binding_record(
        self,
        *,
        product: str,
        context_name: str,
        instance_name: str,
    ) -> EnvironmentRouteBindingRecord: ...


class RouteBindingReconcileFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    detail: str


class RouteBindingExpectedCurrent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: Literal["absent", "present"]
    record_sha256: str = ""

    @model_validator(mode="after")
    def _validate_expected_current(self) -> "RouteBindingExpectedCurrent":
        self.record_sha256 = self.record_sha256.strip().lower()
        if self.state == "absent":
            if self.record_sha256:
                raise ValueError("absent route binding expectation cannot include record_sha256")
            return self
        if len(self.record_sha256) != 64:
            raise ValueError("present route binding expectation requires a SHA-256 record digest")
        try:
            int(self.record_sha256, 16)
        except ValueError as error:
            raise ValueError(
                "present route binding expectation requires a SHA-256 record digest"
            ) from error
        return self


class RouteBindingReconcileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    context: str
    instance: str
    expected_current: RouteBindingExpectedCurrent = Field(
        default_factory=lambda: RouteBindingExpectedCurrent(state="absent")
    )
    source_label: str = "operator-reconcile"
    evaluated_at: str

    @model_validator(mode="after")
    def _validate_request(self) -> "RouteBindingReconcileRequest":
        if self.schema_version != 1:
            raise ValueError("Unsupported route binding reconcile schema version")
        self.product = _required_text(self.product, "route binding reconcile requires product")
        self.context = _required_text(self.context, "route binding reconcile requires context")
        self.instance = _required_text(self.instance, "route binding reconcile requires instance")
        self.source_label = _required_text(
            self.source_label, "route binding reconcile requires source_label"
        )
        self.evaluated_at = _required_text(
            self.evaluated_at, "route binding reconcile requires evaluated_at"
        )
        _parse_utc_timestamp(self.evaluated_at)
        return self


class RouteBindingSourceChanged(ValueError):
    """An exact reviewed source row was removed or changed before its atomic write."""


class RouteBindingReviewSources(BaseModel):
    """Private exact source snapshots for the browser's commit guard."""

    model_config = ConfigDict(extra="forbid")

    provider_target: ProviderTargetRecord
    dokploy_target: DokployTargetRecord
    dokploy_target_id: DokployTargetIdRecord
    edge_endpoint: EdgeEndpointRecord
    ingress_audit: IngressRouteAuditRecord


class RouteBindingReconcilePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ready", "unchanged", "blocked", "conflict"]
    operation: Literal["create", "refresh", "none"] = "none"
    findings: tuple[RouteBindingReconcileFinding, ...] = ()
    review_target_id: str = Field(default="", exclude=True)
    review_sources: RouteBindingReviewSources | None = Field(default=None, exclude=True)
    current_record_sha256: str = ""
    candidate_record_sha256: str = ""
    current_record: EnvironmentRouteBindingRecord | None = Field(default=None, exclude=True)
    record: EnvironmentRouteBindingRecord | None = Field(default=None, exclude=True)


def plan_route_binding_reconcile(
    *,
    record_store: RouteBindingReconcileStore,
    request: RouteBindingReconcileRequest,
) -> RouteBindingReconcilePlan:
    try:
        current_record = record_store.read_route_binding_record(
            product=request.product,
            context_name=request.context,
            instance_name=request.instance,
        )
    except FileNotFoundError:
        current_record = None
    current_record_sha256 = (
        route_binding_record_sha256(current_record) if current_record is not None else ""
    )
    expected_current = request.expected_current
    if expected_current.state == "absent" and current_record is not None:
        return _conflict_plan(
            code="expected_current_present",
            detail=(
                "A route-binding record exists, but the request expected the tuple to be absent."
            ),
            current_record=current_record,
            current_record_sha256=current_record_sha256,
        )
    if expected_current.state == "present" and current_record is None:
        return _conflict_plan(
            code="expected_current_missing",
            detail=("No route-binding record exists, but the request expected a current record."),
        )
    if (
        expected_current.state == "present"
        and expected_current.record_sha256 != current_record_sha256
    ):
        return _conflict_plan(
            code="expected_current_changed",
            detail=("The current route-binding record changed after the caller inspected it."),
            current_record=current_record,
            current_record_sha256=current_record_sha256,
        )

    candidate = build_route_binding_candidate(
        record_store=record_store,
        request=request,
        current_record=current_record,
        current_record_sha256=current_record_sha256,
    )
    if candidate.record is None:
        return candidate
    record = candidate.record
    review_target_id = candidate.review_target_id
    candidate_record_sha256 = route_binding_record_sha256(record)
    if current_record is None:
        return RouteBindingReconcilePlan(
            status="ready",
            operation="create",
            candidate_record_sha256=candidate_record_sha256,
            review_target_id=review_target_id,
            review_sources=candidate.review_sources,
            record=record,
        )
    external_handoff = _is_relinquished_external_operator_binding(current_record)
    if current_record.source.source_kind not in {"backfill", "service"} and not external_handoff:
        return _conflict_plan(
            code="route_binding_ownership_conflict",
            detail=("Reconcile cannot replace a route-binding record set by an admin."),
            current_record=current_record,
            current_record_sha256=current_record_sha256,
            candidate_record_sha256=candidate_record_sha256,
        )
    if external_handoff:
        return RouteBindingReconcilePlan(
            status="ready",
            review_target_id=review_target_id,
            review_sources=candidate.review_sources,
            operation="refresh",
            findings=(
                RouteBindingReconcileFinding(
                    code="external_route_authority_handoff",
                    detail=(
                        "Managed reconcile will replace explicitly relinquished external "
                        "route authority with service-owned provider evidence."
                    ),
                ),
            ),
            current_record_sha256=current_record_sha256,
            candidate_record_sha256=candidate_record_sha256,
            current_record=current_record,
            record=record,
        )
    if route_binding_authority_projection(current_record) != (
        route_binding_authority_projection(record)
    ):
        return _conflict_plan(
            code="route_binding_authority_conflict",
            detail=(
                "Current source records describe different route authority. Reconcile will not "
                "silently change provider target, domains, ingress, TLS ownership, or status."
            ),
            current_record=current_record,
            current_record_sha256=current_record_sha256,
            candidate_record_sha256=candidate_record_sha256,
        )
    if _route_binding_evidence_projection(current_record) != (
        _route_binding_evidence_projection(record)
    ) or _route_binding_refresh_due(
        current_record=current_record,
        evaluated_at=request.evaluated_at,
    ):
        return RouteBindingReconcilePlan(
            status="ready",
            review_target_id=review_target_id,
            review_sources=candidate.review_sources,
            operation="refresh",
            current_record_sha256=current_record_sha256,
            candidate_record_sha256=candidate_record_sha256,
            current_record=current_record,
            record=record,
        )
    return RouteBindingReconcilePlan(
        status="unchanged",
        review_target_id=review_target_id,
        review_sources=candidate.review_sources,
        current_record_sha256=current_record_sha256,
        candidate_record_sha256=current_record_sha256,
        current_record=current_record,
        record=current_record,
    )


def build_route_binding_candidate(
    *,
    record_store: RouteBindingReconcileStore,
    request: RouteBindingReconcileRequest,
    current_record: EnvironmentRouteBindingRecord | None = None,
    current_record_sha256: str = "",
    audit_record: IngressRouteAuditRecord | None = None,
) -> RouteBindingReconcilePlan:
    """Build desired authority from stored sources; this grants no replacement permission."""
    findings: list[RouteBindingReconcileFinding] = []
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
        return _blocked_plan(
            findings=findings,
            current_record=current_record,
            current_record_sha256=current_record_sha256,
        )
    expected_provider_target = ProviderTargetRecord.from_dokploy_records(
        target_record=dokploy_target,
        target_id_record=dokploy_target_id,
    )
    if provider_target_projection(provider_target) != provider_target_projection(
        expected_provider_target
    ):
        return _blocked_plan(
            findings=(
                RouteBindingReconcileFinding(
                    code="provider_target_projection_conflict",
                    detail=(
                        "The provider-target record does not match the canonical projection "
                        "of the tracked Dokploy target and target-id records."
                    ),
                ),
            ),
            current_record=current_record,
            current_record_sha256=current_record_sha256,
        )

    domains, domain_finding = _normalized_domains(dokploy_target.domains)
    if domain_finding is not None:
        findings.append(domain_finding)
        return _blocked_plan(
            findings=findings,
            current_record=current_record,
            current_record_sha256=current_record_sha256,
        )
    if not domains:
        findings.append(
            RouteBindingReconcileFinding(
                code="domains_missing",
                detail="Tracked provider target has no recorded domains for this environment.",
            )
        )
        return _blocked_plan(
            findings=findings,
            current_record=current_record,
            current_record_sha256=current_record_sha256,
        )

    if any(
        endpoint.status == "active"
        and endpoint.instance == "testing"
        and (urlsplit(endpoint.url).hostname or "").rstrip(".").lower() in domains
        for endpoint in record_store.list_private_health_endpoint_records()
    ):
        return _blocked_plan(
            findings=(
                RouteBindingReconcileFinding(
                    code="private_testing_endpoint_not_route_binding_evidence",
                    detail="An active private testing hostname cannot authorize a managed route binding.",
                ),
            ),
            current_record=current_record,
            current_record_sha256=current_record_sha256,
        )
    if audit_record is None:
        audit_record = _resolve_ingress_audit(
            record_store=record_store,
            request=request,
            domains=domains,
            findings=findings,
        )
    elif (
        audit_record.product != request.product
        or audit_record.context != request.context
        or audit_record.requested_domains != domains
        or audit_record.mode != "apply"
        or audit_record.status not in {"applied", "unchanged"}
    ):
        raise ValueError("The explicit ingress audit does not describe this route.")
    if audit_record is None:
        return _blocked_plan(
            findings=findings,
            current_record=current_record,
            current_record_sha256=current_record_sha256,
        )
    if audit_record.record_id.startswith(TESTING_PRIVATE_INGRESS_AUDIT_PREFIX):
        return _blocked_plan(
            findings=(
                *findings,
                RouteBindingReconcileFinding(
                    code="private_testing_ingress_not_route_binding_evidence",
                    detail="A private testing ingress receipt does not authorize a managed route binding.",
                ),
            ),
            current_record=current_record,
            current_record_sha256=current_record_sha256,
        )
    edge_endpoint = _resolve_edge_endpoint(
        record_store=record_store,
        provider_target=provider_target,
        audit_record=audit_record,
        findings=findings,
    )
    if edge_endpoint is None:
        return _blocked_plan(
            findings=findings,
            current_record=current_record,
            current_record_sha256=current_record_sha256,
        )
    if audit_record.tls_owner == "unknown":
        return _blocked_plan(
            findings=(
                *findings,
                RouteBindingReconcileFinding(
                    code="tls_ownership_unknown",
                    detail="The matching ingress audit does not record TLS ownership evidence.",
                ),
            ),
            current_record=current_record,
            current_record_sha256=current_record_sha256,
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
        return _blocked_plan(
            findings=(*findings, freshness_finding),
            current_record=current_record,
            current_record_sha256=current_record_sha256,
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
            source_kind="service",
            source_label=request.source_label,
            source_record_ids=source_record_ids,
            source_versions=source_versions,
            refreshed_at=request.evaluated_at,
            freshness_status="recorded",
            stale_after=stale_after,
        ),
        updated_at=request.evaluated_at,
    )
    return RouteBindingReconcilePlan(
        status="ready",
        operation="create",
        record=record,
        current_record=current_record,
        current_record_sha256=current_record_sha256,
        candidate_record_sha256=route_binding_record_sha256(record),
        review_target_id=provider_target.target_id,
        review_sources=RouteBindingReviewSources(
            provider_target=provider_target,
            dokploy_target=dokploy_target,
            dokploy_target_id=dokploy_target_id,
            edge_endpoint=edge_endpoint,
            ingress_audit=audit_record,
        ),
    )


def _is_relinquished_external_operator_binding(
    record: EnvironmentRouteBindingRecord,
) -> bool:
    return (
        record.status == "disabled"
        and record.source.source_kind == "operator"
        and record.ingress.provider == "external"
        and record.ingress.termination_kind == "edge"
        and record.tls.owner == "external"
    )


def _blocked_plan(
    *,
    findings: tuple[RouteBindingReconcileFinding, ...] | list[RouteBindingReconcileFinding],
    current_record: EnvironmentRouteBindingRecord | None,
    current_record_sha256: str,
) -> RouteBindingReconcilePlan:
    return RouteBindingReconcilePlan(
        status="blocked",
        findings=tuple(findings),
        current_record_sha256=current_record_sha256,
        current_record=current_record,
    )


def _conflict_plan(
    *,
    code: str,
    detail: str,
    current_record: EnvironmentRouteBindingRecord | None = None,
    current_record_sha256: str = "",
    candidate_record_sha256: str = "",
) -> RouteBindingReconcilePlan:
    return RouteBindingReconcilePlan(
        status="conflict",
        findings=(RouteBindingReconcileFinding(code=code, detail=detail),),
        current_record_sha256=current_record_sha256,
        candidate_record_sha256=candidate_record_sha256,
        current_record=current_record,
    )


def _read_provider_target(
    *,
    record_store: RouteBindingReconcileStore,
    request: RouteBindingReconcileRequest,
    findings: list[RouteBindingReconcileFinding],
) -> ProviderTargetRecord | None:
    try:
        return record_store.read_provider_target_record(
            context_name=request.context,
            instance_name=request.instance,
        )
    except FileNotFoundError:
        findings.append(
            RouteBindingReconcileFinding(
                code="provider_target_missing",
                detail="No provider-target record exists for the requested context/instance.",
            )
        )
        return None


def _read_dokploy_target(
    *,
    record_store: RouteBindingReconcileStore,
    request: RouteBindingReconcileRequest,
    findings: list[RouteBindingReconcileFinding],
) -> DokployTargetRecord | None:
    try:
        return record_store.read_dokploy_target_record(
            context_name=request.context,
            instance_name=request.instance,
        )
    except FileNotFoundError:
        findings.append(
            RouteBindingReconcileFinding(
                code="dokploy_target_missing",
                detail="No tracked Dokploy target record exists for the requested context/instance.",
            )
        )
        return None


def _read_dokploy_target_id(
    *,
    record_store: RouteBindingReconcileStore,
    request: RouteBindingReconcileRequest,
    findings: list[RouteBindingReconcileFinding],
) -> DokployTargetIdRecord | None:
    try:
        return record_store.read_dokploy_target_id_record(
            context_name=request.context,
            instance_name=request.instance,
        )
    except FileNotFoundError:
        findings.append(
            RouteBindingReconcileFinding(
                code="dokploy_target_id_missing",
                detail=(
                    "No tracked Dokploy target-id record exists for the requested context/instance."
                ),
            )
        )
        return None


def _resolve_edge_endpoint(
    *,
    record_store: RouteBindingReconcileStore,
    provider_target: ProviderTargetRecord,
    audit_record: IngressRouteAuditRecord,
    findings: list[RouteBindingReconcileFinding],
) -> EdgeEndpointRecord | None:
    active_endpoints = record_store.list_edge_endpoint_records(
        provider=provider_target.provider_id,
        status="active",
        limit=_EVIDENCE_SCAN_LIMIT + 1,
    )
    if len(active_endpoints) > _EVIDENCE_SCAN_LIMIT:
        findings.append(
            RouteBindingReconcileFinding(
                code="edge_endpoint_scan_limit_exceeded",
                detail=(
                    "Reconcile cannot resolve edge endpoint evidence because the bounded "
                    "active-endpoint scan was exhausted."
                ),
            )
        )
        return None
    if not active_endpoints:
        findings.append(
            RouteBindingReconcileFinding(
                code="edge_endpoint_missing",
                detail="No active edge endpoint records are available for Dokploy-backed ingress.",
            )
        )
        return None
    candidates = tuple(
        endpoint
        for endpoint in active_endpoints
        if endpoint.endpoint_key == audit_record.edge_endpoint_key
    )
    if len(candidates) != 1:
        findings.append(
            RouteBindingReconcileFinding(
                code=(
                    "ingress_edge_endpoint_ambiguous"
                    if candidates
                    else "ingress_edge_endpoint_missing"
                ),
                detail=(
                    "Reconcile could not resolve exactly one active edge endpoint named by "
                    "the matching ingress audit."
                ),
            )
        )
        return None
    return candidates[0]


def _resolve_ingress_audit(
    *,
    record_store: RouteBindingReconcileStore,
    request: RouteBindingReconcileRequest,
    domains: tuple[str, ...],
    findings: list[RouteBindingReconcileFinding],
) -> IngressRouteAuditRecord | None:
    domain_set = set(domains)
    audit_records = record_store.list_ingress_route_audit_records(
        product=request.product,
        context_name=request.context,
        limit=_EVIDENCE_SCAN_LIMIT + 1,
    )
    if len(audit_records) > _EVIDENCE_SCAN_LIMIT:
        findings.append(
            RouteBindingReconcileFinding(
                code="ingress_audit_scan_limit_exceeded",
                detail=(
                    "Reconcile cannot resolve ingress evidence because the bounded audit "
                    "scan was exhausted."
                ),
            )
        )
        return None
    matching_records: list[tuple[IngressRouteAuditRecord, datetime]] = []
    for record in audit_records:
        if record.mode != "apply":
            continue
        requested_domains, requested_domain_finding = _normalized_domains(record.requested_domains)
        if requested_domain_finding is not None:
            continue
        if set(requested_domains) == domain_set:
            try:
                recorded_at = _parse_utc_timestamp(record.recorded_at)
            except ValueError:
                findings.append(
                    RouteBindingReconcileFinding(
                        code="ingress_audit_timestamp_invalid",
                        detail="A matching ingress audit has an invalid recorded_at timestamp.",
                    )
                )
                return None
            matching_records.append((record, recorded_at))
    if not matching_records:
        findings.append(
            RouteBindingReconcileFinding(
                code="ingress_audit_missing",
                detail=(
                    "Reconcile requires exactly one applied ingress audit matching the "
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
            RouteBindingReconcileFinding(
                code="ingress_audit_unresolved",
                detail=(
                    "The latest matching ingress audit is not terminal, so route evidence "
                    "cannot be promoted."
                ),
            )
        )
        return None
    if any(not record.edge_endpoint_key.strip() for record in latest_records):
        findings.append(
            RouteBindingReconcileFinding(
                code="ingress_audit_edge_endpoint_missing",
                detail=(
                    "The latest matching ingress audit does not name an edge endpoint, so "
                    "route evidence cannot be promoted."
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
            RouteBindingReconcileFinding(
                code="ingress_audit_ambiguous",
                detail=(
                    "The latest matching ingress audits disagree about provider or edge endpoint."
                ),
            )
        )
        return None
    return max(latest_records, key=lambda record: record.record_id)


def provider_target_projection(record: ProviderTargetRecord) -> tuple[object, ...]:
    # Identity only: a lane setting written to the Dokploy target record (a testing
    # hold, an integration allowance) moves its updated_at without changing the target.
    return (
        record.context,
        record.instance,
        record.provider_id,
        record.target_category,
        record.target_id,
        record.display_name,
        record.provider_target_type,
        tuple(sorted(record.provider_evidence.items())),
    )


def route_binding_authority_projection(
    record: EnvironmentRouteBindingRecord,
) -> tuple[object, ...]:
    return (
        record.schema_version,
        record.product,
        record.context,
        record.instance,
        record.provider_target.provider_id,
        record.provider_target.target_category,
        record.provider_target.provider_target_type,
        record.provider_target.target_name,
        record.ingress.provider,
        record.ingress.endpoint_key,
        record.ingress.termination_kind,
        tuple(sorted((domain.role, domain.domain_name) for domain in record.domains)),
        record.tls.owner,
        record.status,
    )


def _route_binding_evidence_projection(
    record: EnvironmentRouteBindingRecord,
) -> tuple[object, ...]:
    return (
        tuple(sorted(record.provider_target.provider_evidence.items())),
        tuple(sorted(record.ingress.provider_evidence.items())),
        tuple(sorted(record.tls.provider_evidence.items())),
        record.source.source_kind,
        record.source.source_label,
        record.source.source_record_ids,
        tuple(sorted(record.source.source_versions.items())),
        record.source.freshness_status,
    )


def _route_binding_refresh_due(
    *,
    current_record: EnvironmentRouteBindingRecord,
    evaluated_at: str,
) -> bool:
    evaluated_timestamp = _parse_utc_timestamp(evaluated_at)
    try:
        stale_after = _parse_utc_timestamp(current_record.source.stale_after)
    except ValueError:
        return True
    if stale_after > evaluated_timestamp + _EVIDENCE_FRESHNESS_WINDOW:
        return True
    return stale_after <= evaluated_timestamp + _EVIDENCE_REFRESH_THRESHOLD


def _source_freshness(
    *,
    evaluated_at: str,
    evidence: tuple[tuple[str, str], ...],
) -> tuple[dict[str, str], str, RouteBindingReconcileFinding | None]:
    evaluated_timestamp = _parse_utc_timestamp(evaluated_at)
    source_versions: dict[str, str] = {}
    for source_id, version in evidence:
        try:
            source_timestamp = _parse_utc_timestamp(version)
        except ValueError:
            return (
                {},
                "",
                RouteBindingReconcileFinding(
                    code="evidence_timestamp_invalid",
                    detail=f"Route-binding evidence {source_id!r} has an invalid timestamp.",
                ),
            )
        if source_timestamp > evaluated_timestamp:
            return (
                {},
                "",
                RouteBindingReconcileFinding(
                    code="evidence_timestamp_future",
                    detail=f"Route-binding evidence {source_id!r} is timestamped in the future.",
                ),
            )
        source_versions[source_id] = version
    return (
        source_versions,
        _format_utc_timestamp(evaluated_timestamp + _EVIDENCE_FRESHNESS_WINDOW),
        None,
    )


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
) -> tuple[tuple[str, ...], RouteBindingReconcileFinding | None]:
    domains: list[str] = []
    for raw_domain in raw_domains:
        if not raw_domain.strip():
            continue
        try:
            domain = RouteBindingDomain(domain_name=raw_domain).domain_name
        except (ValueError, ValidationError):
            return (), RouteBindingReconcileFinding(
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
