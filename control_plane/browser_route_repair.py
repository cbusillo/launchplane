"""Narrow browser reviews for existing record-only route repair operations."""

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.route_binding_record import (
    EnvironmentRouteBindingReadModel,
    redacted_route_binding_record,
)
from control_plane.dokploy.api import JsonObject
from control_plane.dokploy_target_comparison import provider_target_comparison_fingerprint
from control_plane.route_binding_reconcile import (
    RouteBindingExpectedCurrent,
    RouteBindingReconcileFinding,
    RouteBindingReconcilePlan,
)
from control_plane.workflows.dokploy_target_adoption import (
    DokployTargetAdoptionRecordStore,
    DokployTargetDomainRepairPlan,
    FetchDokployTargetPayload,
    normalize_dokploy_domain_hosts,
    normalize_dokploy_domain_host,
    plan_dokploy_target_domain_authority,
)


class BrowserRouteRepairRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    reviewed_plan_sha256: str = ""

    @model_validator(mode="after")
    def validate_request(self) -> "BrowserRouteRepairRequest":
        self.product = self.product.strip()
        self.reviewed_plan_sha256 = self.reviewed_plan_sha256.strip().lower()
        if not self.product:
            raise ValueError("Browser route repair requires the lane product.")
        if self.reviewed_plan_sha256 and not re.fullmatch(
            r"[0-9a-f]{64}", self.reviewed_plan_sha256
        ):
            raise ValueError("Browser route repair requires a SHA-256 reviewed plan digest.")
        return self

    def validate_mode(self, mode: str) -> None:
        if mode == "dry-run" and self.reviewed_plan_sha256:
            raise ValueError("Browser route repair dry-run rejects a reviewed plan digest.")
        if mode == "apply" and not self.reviewed_plan_sha256:
            raise ValueError("Browser route repair apply requires its reviewed plan digest.")


class RouteRepairSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity: str
    version: str


class DokployDomainRepairReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["repair-domain-authority"] = "repair-domain-authority"
    product: str
    context: str
    instance: str
    status: Literal["ready", "unchanged"]
    plan_sha256: str
    target_type: Literal["application", "compose"]
    target_id: str
    target_name: str
    current_domains: tuple[str, ...]
    proposed_domains: tuple[str, ...]
    observed_domains: tuple[str, ...]
    proposed_primary_domain: str
    source_records: tuple[RouteRepairSource, ...]
    applied: bool = False


class RouteBindingRepairReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["reconcile-route-binding"] = "reconcile-route-binding"
    product: str
    context: str
    instance: str
    status: Literal["ready", "unchanged", "blocked", "conflict"]
    binding_operation: Literal["create", "refresh", "none"]
    plan_sha256: str
    expected_current: RouteBindingExpectedCurrent
    current_record_sha256: str
    current_record: EnvironmentRouteBindingReadModel | None = Field(
        default=None, json_schema_extra={"x-launchplane-optional-response": True}
    )
    proposed_record: EnvironmentRouteBindingReadModel | None = Field(
        default=None, json_schema_extra={"x-launchplane-optional-response": True}
    )
    current_domains: tuple[str, ...]
    proposed_domains: tuple[str, ...]
    findings: tuple[RouteBindingReconcileFinding, ...]
    source_records: tuple[RouteRepairSource, ...]
    applied: bool = False


@dataclass(frozen=True)
class BrowserDomainRepairPlan:
    repair: DokployTargetDomainRepairPlan
    review: DokployDomainRepairReview


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def plan_browser_domain_repair(
    *,
    record_store: DokployTargetAdoptionRecordStore,
    product: str,
    context: str,
    instance: str,
    host: str,
    token: str,
    fetch_target_payload: FetchDokployTargetPayload,
    fetch_target_domains: Callable[
        [str, str, Literal["application", "compose"], str], tuple[JsonObject, ...]
    ],
) -> BrowserDomainRepairPlan:
    target = record_store.read_dokploy_target_record(context_name=context, instance_name=instance)
    target_id = record_store.read_dokploy_target_id_record(
        context_name=context, instance_name=instance
    )
    provider = record_store.read_provider_target_record(
        context_name=context, instance_name=instance
    )
    observed = fetch_target_domains(host, token, target.target_type, target_id.target_id)
    domains = tuple(
        sorted({normalize_dokploy_domain_host(str(row.get("host") or "")) for row in observed})
    )
    # An already-canonical stored host order remains authoritative (including its primary host).
    try:
        stored_hosts = normalize_dokploy_domain_hosts(
            tuple(
                (urlsplit(domain if "://" in domain else f"//{domain}").hostname or "")
                for domain in target.domains
            )
        )
    except ValueError:
        stored_hosts = ()
    if set(stored_hosts) == set(domains) and len(stored_hosts) == len(domains):
        domains = stored_hosts
    repair = plan_dokploy_target_domain_authority(
        record_store=record_store,
        host=host,
        token=token,
        context=context,
        instance=instance,
        target_type=target.target_type,
        target_id=target_id.target_id,
        domains=domains,
        expected_current_provider_target=provider.to_deployed_target_reference(),
        fetch_target_payload=fetch_target_payload,
        fetch_target_domains=lambda *_args: observed,
    )
    current = repair.current_target
    current_provider = repair.current_provider_target
    canonical = ProviderTargetRecord.from_dokploy_records(
        target_record=current, target_id_record=repair.target_id
    )
    # Arbitrary private provider evidence stays private. Reuse its existing keyed comparison.
    comparison: object = current_provider.to_deployed_target_reference().model_dump(mode="json")
    if (
        current_provider.provider_evidence
        and current_provider.provider_evidence != canonical.provider_evidence
    ):
        comparison = provider_target_comparison_fingerprint(current_provider)
    sources = (
        RouteRepairSource(
            identity=f"dokploy-target:{context}:{instance}", version=current.updated_at
        ),
        RouteRepairSource(
            identity=f"dokploy-target-id:{context}:{instance}", version=repair.target_id.updated_at
        ),
        RouteRepairSource(
            identity=f"provider-target:{context}:{instance}", version=current_provider.updated_at
        ),
    )
    evidence = {
        "schema_version": 1,
        "operation": "repair-domain-authority",
        "product": product,
        "context": context,
        "instance": instance,
        "target_type": current.target_type,
        "target_id": repair.target_id.target_id,
        "target_name": current.target_name,
        "current_domains": current.domains,
        "proposed_domains": repair.result.requested_domains,
        "provider_comparison": comparison,
        "source_records": [source.model_dump(mode="json") for source in sources],
    }
    return BrowserDomainRepairPlan(
        repair=repair,
        review=DokployDomainRepairReview(
            product=product,
            context=context,
            instance=instance,
            status="unchanged" if current.domains == repair.result.requested_domains else "ready",
            plan_sha256=_digest(evidence),
            target_type=current.target_type,
            target_id=repair.target_id.target_id,
            target_name=current.target_name,
            current_domains=current.domains,
            proposed_domains=repair.result.requested_domains,
            observed_domains=tuple(sorted(set(repair.result.live_provider_domains))),
            proposed_primary_domain=repair.result.requested_domains[0],
            source_records=sources,
        ),
    )


def binding_repair_review(
    *,
    product: str,
    context: str,
    instance: str,
    plan: RouteBindingReconcilePlan,
    expected_current: RouteBindingExpectedCurrent,
    applied: bool = False,
) -> RouteBindingRepairReview:
    current = redacted_route_binding_record(plan.current_record) if plan.current_record else None
    proposed = redacted_route_binding_record(plan.record) if plan.record else None
    semantic_record: dict[str, object] | None = None
    if plan.record is not None:
        semantic_record = plan.record.model_dump(mode="json")
        semantic_record.pop("updated_at", None)
        source = semantic_record.get("source")
        if isinstance(source, dict):
            source.pop("refreshed_at", None)
            source.pop("stale_after", None)
    evidence = {
        "schema_version": 1,
        "operation": "reconcile-route-binding",
        "product": product,
        "context": context,
        "instance": instance,
        "status": plan.status,
        "binding_operation": plan.operation,
        "expected_current": expected_current.model_dump(mode="json"),
        "candidate": semantic_record,
        "target_id": plan.review_target_id,
    }
    sources = tuple(
        RouteRepairSource(identity=identity, version=version)
        for identity, version in (plan.record.source.source_versions.items() if plan.record else ())
    )
    return RouteBindingRepairReview(
        product=product,
        context=context,
        instance=instance,
        status=plan.status,
        binding_operation=plan.operation,
        plan_sha256=_digest(evidence),
        expected_current=expected_current,
        current_record_sha256=plan.current_record_sha256,
        current_record=current,
        proposed_record=proposed,
        current_domains=tuple(domain.domain_name for domain in current.domains) if current else (),
        proposed_domains=tuple(domain.domain_name for domain in proposed.domains)
        if proposed
        else (),
        findings=plan.findings,
        source_records=sources,
        applied=applied,
    )
