from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
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
from control_plane.route_binding_reconcile import (
    RouteBindingExpectedCurrent,
    RouteBindingReconcileFinding,
)


_ATTESTATION_FRESHNESS_WINDOW = timedelta(days=30)
_ATTESTATION_REFRESH_THRESHOLD = timedelta(days=15)


class ExternalRouteBindingReconcileStore(Protocol):
    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord: ...

    def read_provider_target_record(
        self, *, context_name: str, instance_name: str
    ) -> ProviderTargetRecord: ...

    def read_route_binding_record(
        self,
        *,
        product: str,
        context_name: str,
        instance_name: str,
    ) -> EnvironmentRouteBindingRecord: ...


class ExternalRouteBindingReconcileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    context: str
    instance: str
    expected_current: RouteBindingExpectedCurrent = Field(
        default_factory=lambda: RouteBindingExpectedCurrent(state="absent")
    )
    desired_status: Literal["active", "disabled"] = "active"
    source_label: str = "operator-external-reconcile"
    evaluated_at: str

    @model_validator(mode="after")
    def _validate_request(self) -> "ExternalRouteBindingReconcileRequest":
        if self.schema_version != 1:
            raise ValueError("Unsupported external route binding reconcile schema version")
        self.product = _required_text(
            self.product, "external route binding reconcile requires product"
        )
        self.context = _required_text(
            self.context, "external route binding reconcile requires context"
        )
        self.instance = _required_text(
            self.instance, "external route binding reconcile requires instance"
        )
        self.source_label = _required_text(
            self.source_label, "external route binding reconcile requires source_label"
        )
        self.evaluated_at = _required_text(
            self.evaluated_at, "external route binding reconcile requires evaluated_at"
        )
        _parse_utc_timestamp(self.evaluated_at)
        return self


class ExternalRouteBindingReconcilePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ready", "unchanged", "blocked", "conflict"]
    operation: Literal["create", "refresh", "replace", "relinquish", "none"] = "none"
    findings: tuple[RouteBindingReconcileFinding, ...] = ()
    current_record_sha256: str = ""
    candidate_record_sha256: str = ""
    current_record: EnvironmentRouteBindingRecord | None = Field(default=None, exclude=True)
    record: EnvironmentRouteBindingRecord | None = Field(default=None, exclude=True)


def plan_external_route_binding_reconcile(
    *,
    record_store: ExternalRouteBindingReconcileStore,
    request: ExternalRouteBindingReconcileRequest,
) -> ExternalRouteBindingReconcilePlan:
    current_record = _read_current_record(record_store=record_store, request=request)
    current_record_sha256 = (
        route_binding_record_sha256(current_record) if current_record is not None else ""
    )
    expectation_conflict = _expected_current_conflict(
        request=request,
        current_record=current_record,
        current_record_sha256=current_record_sha256,
    )
    if expectation_conflict is not None:
        return expectation_conflict
    if request.desired_status == "disabled":
        return _plan_external_relinquish(
            request=request,
            current_record=current_record,
            current_record_sha256=current_record_sha256,
        )

    findings: list[RouteBindingReconcileFinding] = []
    profile = _read_product_profile(record_store=record_store, request=request, findings=findings)
    provider_target = _read_provider_target(
        record_store=record_store, request=request, findings=findings
    )
    if profile is None or provider_target is None:
        return _blocked_plan(
            findings=findings,
            current_record=current_record,
            current_record_sha256=current_record_sha256,
        )
    lane = _resolve_lane(profile=profile, request=request, findings=findings)
    if lane is None:
        return _blocked_plan(
            findings=findings,
            current_record=current_record,
            current_record_sha256=current_record_sha256,
        )
    domains = _external_domains(profile=profile, lane=lane, findings=findings)
    if not domains or findings:
        return _blocked_plan(
            findings=findings,
            current_record=current_record,
            current_record_sha256=current_record_sha256,
        )

    source_record_ids = (
        f"product-profile:{profile.product}",
        f"provider-target:{provider_target.context}:{provider_target.instance}",
    )
    source_versions, stale_after, freshness_finding = _source_freshness(
        evaluated_at=request.evaluated_at,
        evidence=(
            (source_record_ids[0], profile.updated_at),
            (source_record_ids[1], provider_target.updated_at),
        ),
    )
    if freshness_finding is not None:
        return _blocked_plan(
            findings=(freshness_finding,),
            current_record=current_record,
            current_record_sha256=current_record_sha256,
        )

    primary_domain = next(domain.domain_name for domain in domains if domain.role == "primary")
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
            provider="external",
            endpoint_key=f"external:{primary_domain}",
            termination_kind="edge",
        ),
        domains=domains,
        tls=RouteBindingTls(owner="external"),
        status="active",
        source=RouteBindingSource(
            source_kind="operator",
            source_label=request.source_label,
            source_record_ids=source_record_ids,
            source_versions=source_versions,
            refreshed_at=request.evaluated_at,
            freshness_status="recorded",
            stale_after=stale_after,
        ),
        updated_at=request.evaluated_at,
    )
    candidate_record_sha256 = route_binding_record_sha256(record)
    if current_record is None:
        return ExternalRouteBindingReconcilePlan(
            status="ready",
            operation="create",
            candidate_record_sha256=candidate_record_sha256,
            record=record,
        )
    if not _is_external_operator_binding(current_record):
        return _conflict_plan(
            code="route_binding_ownership_conflict",
            detail=(
                "External reconcile cannot replace a managed or differently owned "
                "route-binding record."
            ),
            current_record=current_record,
            current_record_sha256=current_record_sha256,
            candidate_record_sha256=candidate_record_sha256,
        )
    if _route_binding_authority_projection(current_record) != _route_binding_authority_projection(
        record
    ):
        return ExternalRouteBindingReconcilePlan(
            status="ready",
            operation="replace",
            findings=(
                RouteBindingReconcileFinding(
                    code="external_route_authority_change",
                    detail=(
                        "DB-backed product or provider-target authority changed; apply will "
                        "replace the current operator-owned external binding under CAS."
                    ),
                ),
            ),
            current_record_sha256=current_record_sha256,
            candidate_record_sha256=candidate_record_sha256,
            current_record=current_record,
            record=record,
        )
    if _route_binding_evidence_projection(current_record) != _route_binding_evidence_projection(
        record
    ) or _route_binding_refresh_due(
        current_record=current_record,
        evaluated_at=request.evaluated_at,
    ):
        return ExternalRouteBindingReconcilePlan(
            status="ready",
            operation="refresh",
            current_record_sha256=current_record_sha256,
            candidate_record_sha256=candidate_record_sha256,
            current_record=current_record,
            record=record,
        )
    return ExternalRouteBindingReconcilePlan(
        status="unchanged",
        current_record_sha256=current_record_sha256,
        candidate_record_sha256=current_record_sha256,
        current_record=current_record,
        record=current_record,
    )


def _plan_external_relinquish(
    *,
    request: ExternalRouteBindingReconcileRequest,
    current_record: EnvironmentRouteBindingRecord | None,
    current_record_sha256: str,
) -> ExternalRouteBindingReconcilePlan:
    if current_record is None:
        return _conflict_plan(
            code="external_route_binding_missing",
            detail="External route authority cannot be relinquished because no binding exists.",
        )
    if not _is_external_operator_binding(current_record):
        return _conflict_plan(
            code="route_binding_ownership_conflict",
            detail=(
                "External reconcile cannot relinquish a managed or differently owned "
                "route-binding record."
            ),
            current_record=current_record,
            current_record_sha256=current_record_sha256,
        )
    if current_record.status == "disabled":
        return ExternalRouteBindingReconcilePlan(
            status="unchanged",
            current_record_sha256=current_record_sha256,
            candidate_record_sha256=current_record_sha256,
            current_record=current_record,
            record=current_record,
        )
    evaluated_timestamp = _parse_utc_timestamp(request.evaluated_at)
    replacement_record = current_record.model_copy(
        update={
            "status": "disabled",
            "source": current_record.source.model_copy(
                update={
                    "source_label": request.source_label,
                    "refreshed_at": request.evaluated_at,
                    "stale_after": _format_utc_timestamp(
                        evaluated_timestamp + _ATTESTATION_FRESHNESS_WINDOW
                    ),
                }
            ),
            "updated_at": request.evaluated_at,
        }
    )
    return ExternalRouteBindingReconcilePlan(
        status="ready",
        operation="relinquish",
        findings=(
            RouteBindingReconcileFinding(
                code="external_route_authority_relinquish",
                detail=(
                    "Apply will disable the operator-owned external binding so managed "
                    "reconcile can establish replacement authority after provider mutation."
                ),
            ),
        ),
        current_record_sha256=current_record_sha256,
        candidate_record_sha256=route_binding_record_sha256(replacement_record),
        current_record=current_record,
        record=replacement_record,
    )


def _read_current_record(
    *,
    record_store: ExternalRouteBindingReconcileStore,
    request: ExternalRouteBindingReconcileRequest,
) -> EnvironmentRouteBindingRecord | None:
    try:
        return record_store.read_route_binding_record(
            product=request.product,
            context_name=request.context,
            instance_name=request.instance,
        )
    except FileNotFoundError:
        return None


def _expected_current_conflict(
    *,
    request: ExternalRouteBindingReconcileRequest,
    current_record: EnvironmentRouteBindingRecord | None,
    current_record_sha256: str,
) -> ExternalRouteBindingReconcilePlan | None:
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
            detail="No route-binding record exists, but the request expected a current record.",
        )
    if (
        expected_current.state == "present"
        and expected_current.record_sha256 != current_record_sha256
    ):
        return _conflict_plan(
            code="expected_current_changed",
            detail="The current route-binding record changed after the caller inspected it.",
            current_record=current_record,
            current_record_sha256=current_record_sha256,
        )
    return None


def _read_product_profile(
    *,
    record_store: ExternalRouteBindingReconcileStore,
    request: ExternalRouteBindingReconcileRequest,
    findings: list[RouteBindingReconcileFinding],
) -> LaunchplaneProductProfileRecord | None:
    try:
        profile = record_store.read_product_profile_record(request.product)
    except FileNotFoundError:
        findings.append(
            RouteBindingReconcileFinding(
                code="product_profile_missing",
                detail="No DB-backed product profile exists for the requested product.",
            )
        )
        return None
    if profile.product != request.product:
        findings.append(
            RouteBindingReconcileFinding(
                code="product_profile_mismatch",
                detail="The loaded product profile does not match the requested product.",
            )
        )
        return None
    return profile


def _read_provider_target(
    *,
    record_store: ExternalRouteBindingReconcileStore,
    request: ExternalRouteBindingReconcileRequest,
    findings: list[RouteBindingReconcileFinding],
) -> ProviderTargetRecord | None:
    try:
        provider_target = record_store.read_provider_target_record(
            context_name=request.context,
            instance_name=request.instance,
        )
    except FileNotFoundError:
        findings.append(
            RouteBindingReconcileFinding(
                code="provider_target_missing",
                detail="No provider-target record exists for the requested context and instance.",
            )
        )
        return None
    if provider_target.context != request.context or provider_target.instance != request.instance:
        findings.append(
            RouteBindingReconcileFinding(
                code="provider_target_mismatch",
                detail="The loaded provider-target record does not match the requested lane.",
            )
        )
        return None
    return provider_target


def _resolve_lane(
    *,
    profile: LaunchplaneProductProfileRecord,
    request: ExternalRouteBindingReconcileRequest,
    findings: list[RouteBindingReconcileFinding],
) -> ProductLaneProfile | None:
    matches = tuple(
        lane
        for lane in profile.lanes
        if lane.context == request.context and lane.instance == request.instance
    )
    if not matches:
        findings.append(
            RouteBindingReconcileFinding(
                code="product_lane_missing",
                detail="The product profile does not own the requested context and instance.",
            )
        )
        return None
    if len(matches) != 1:
        findings.append(
            RouteBindingReconcileFinding(
                code="product_lane_ambiguous",
                detail="The product profile owns the requested lane more than once.",
            )
        )
        return None
    return matches[0]


def _external_domains(
    *,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    findings: list[RouteBindingReconcileFinding],
) -> tuple[RouteBindingDomain, ...]:
    primary_domain = _https_domain(
        value=lane.base_url,
        label="product lane base_url",
        findings=findings,
        required=True,
    )
    health_domain = _https_domain(
        value=lane.health_url,
        label="product lane health_url",
        findings=findings,
        required=False,
    )
    strict_checks = tuple(
        check
        for check in lane.health_monitoring.checks
        if check.enabled and check.kind == "public_http" and check.require_runtime_identity
    )
    if not strict_checks:
        findings.append(
            RouteBindingReconcileFinding(
                code="strict_public_runtime_identity_check_missing",
                detail=(
                    "External route authority requires an enabled public HTTP check that "
                    "requires runtime identity."
                ),
            )
        )
    declared_domains = {domain for domain in (primary_domain, health_domain) if domain}
    for check in strict_checks:
        check_url = check.url or lane.health_url or lane.base_url
        check_domain = _https_domain(
            value=check_url,
            label=f"public HTTP check {check.name!r}",
            findings=findings,
            required=True,
        )
        if check_domain and check_domain not in declared_domains:
            findings.append(
                RouteBindingReconcileFinding(
                    code="strict_public_runtime_identity_domain_unowned",
                    detail=(
                        "The strict public runtime-identity check targets a domain not owned "
                        "by the lane base_url or health_url."
                    ),
                )
            )
    if not primary_domain:
        return ()
    domains = [RouteBindingDomain(domain_name=primary_domain, role="primary")]
    if health_domain and health_domain != primary_domain:
        domains.append(RouteBindingDomain(domain_name=health_domain, role="alias"))
    return tuple(domains)


def _https_domain(
    *,
    value: str,
    label: str,
    findings: list[RouteBindingReconcileFinding],
    required: bool,
) -> str:
    normalized_value = value.strip()
    if not normalized_value:
        if required:
            findings.append(
                RouteBindingReconcileFinding(
                    code="public_url_missing",
                    detail=f"External route authority requires {label}.",
                )
            )
        return ""
    try:
        parsed = urlsplit(normalized_value)
        port = parsed.port
    except ValueError:
        findings.append(
            RouteBindingReconcileFinding(
                code="public_url_invalid",
                detail=f"{label} is not a valid public HTTPS URL.",
            )
        )
        return ""
    domain = (parsed.hostname or "").strip().rstrip(".").lower()
    if (
        parsed.scheme.lower() != "https"
        or not domain
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        findings.append(
            RouteBindingReconcileFinding(
                code="public_https_url_required",
                detail=f"{label} must be a credential-free HTTPS URL on the standard TLS port.",
            )
        )
        return ""
    return domain


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
                    detail=f"External route-binding evidence {source_id!r} has an invalid timestamp.",
                ),
            )
        if source_timestamp > evaluated_timestamp:
            return (
                {},
                "",
                RouteBindingReconcileFinding(
                    code="evidence_timestamp_future",
                    detail=f"External route-binding evidence {source_id!r} is future-dated.",
                ),
            )
        source_versions[source_id] = version
    stale_after = _format_utc_timestamp(evaluated_timestamp + _ATTESTATION_FRESHNESS_WINDOW)
    return source_versions, stale_after, None


def _is_external_operator_binding(record: EnvironmentRouteBindingRecord) -> bool:
    return (
        record.source.source_kind == "operator"
        and record.ingress.provider == "external"
        and record.ingress.termination_kind == "edge"
        and record.tls.owner == "external"
    )


def _route_binding_authority_projection(
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
    if stale_after > evaluated_timestamp + _ATTESTATION_FRESHNESS_WINDOW:
        return True
    return stale_after <= evaluated_timestamp + _ATTESTATION_REFRESH_THRESHOLD


def _blocked_plan(
    *,
    findings: tuple[RouteBindingReconcileFinding, ...] | list[RouteBindingReconcileFinding],
    current_record: EnvironmentRouteBindingRecord | None,
    current_record_sha256: str,
) -> ExternalRouteBindingReconcilePlan:
    return ExternalRouteBindingReconcilePlan(
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
) -> ExternalRouteBindingReconcilePlan:
    return ExternalRouteBindingReconcilePlan(
        status="conflict",
        findings=(RouteBindingReconcileFinding(code=code, detail=detail),),
        current_record_sha256=current_record_sha256,
        candidate_record_sha256=candidate_record_sha256,
        current_record=current_record,
    )


def _required_text(value: str, message: str) -> str:
    normalized_value = value.strip()
    if not normalized_value:
        raise ValueError(message)
    return normalized_value


def _parse_utc_timestamp(value: str) -> datetime:
    normalized_value = value.strip()
    if normalized_value.endswith("Z"):
        normalized_value = f"{normalized_value[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized_value)
    except ValueError as error:
        raise ValueError("timestamp must be ISO-8601") from error
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _format_utc_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
