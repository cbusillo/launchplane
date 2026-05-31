from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

IngressRouteAuditMode = Literal["dry-run", "apply"]
IngressRouteAuditStatus = Literal["pending", "planned", "applied", "unchanged"]


class IngressRouteAuditOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str
    host_id: int | None = Field(default=None, ge=1)
    domain_names: tuple[str, ...]
    requires_apply: bool


class IngressRouteAuditRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    record_id: str
    product: str
    context: str
    provider: str = "npmplus"
    mode: IngressRouteAuditMode
    status: IngressRouteAuditStatus
    dry_run: bool
    requested_domains: tuple[str, ...]
    expected_host_id: int | None = Field(default=None, ge=1)
    provider_host_id: int | None = Field(default=None, ge=1)
    operations: tuple[IngressRouteAuditOperation, ...]
    trace_id: str
    idempotency_key: str = ""
    reason: str
    recorded_at: str

    @model_validator(mode="after")
    def _validate_record(self) -> "IngressRouteAuditRecord":
        self.record_id = _required_text(self.record_id, "ingress route audit requires record_id")
        self.product = _required_text(self.product, "ingress route audit requires product")
        self.context = _required_text(self.context, "ingress route audit requires context")
        self.provider = _required_text(self.provider, "ingress route audit requires provider")
        self.trace_id = _required_text(self.trace_id, "ingress route audit requires trace_id")
        self.reason = _required_text(self.reason, "ingress route audit requires reason")
        self.recorded_at = _required_text(
            self.recorded_at, "ingress route audit requires recorded_at"
        )
        self.idempotency_key = self.idempotency_key.strip()
        normalized_domains = tuple(
            dict.fromkeys(
                domain.strip().lower() for domain in self.requested_domains if domain.strip()
            )
        )
        if not normalized_domains:
            raise ValueError("ingress route audit requires requested_domains")
        self.requested_domains = normalized_domains
        if not self.operations:
            raise ValueError("ingress route audit requires operations")
        if self.mode == "dry-run" and not self.dry_run:
            raise ValueError("dry-run ingress audit must have dry_run=true")
        if self.mode == "apply" and self.dry_run:
            raise ValueError("apply ingress audit must have dry_run=false")
        return self


def build_ingress_route_audit_record_id(
    *,
    trace_id: str,
    product: str,
    context: str,
    domains: tuple[str, ...],
) -> str:
    normalized_trace_id = _required_text(trace_id, "ingress route audit id requires trace_id")
    material = json.dumps(
        {
            "trace_id": normalized_trace_id,
            "product": product.strip(),
            "context": context.strip(),
            "domains": sorted(domain.strip().lower() for domain in domains if domain.strip()),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return f"ingress-route-audit-{normalized_trace_id}-{digest}"


def _required_text(value: str, message: str) -> str:
    normalized_value = value.strip()
    if not normalized_value:
        raise ValueError(message)
    return normalized_value
