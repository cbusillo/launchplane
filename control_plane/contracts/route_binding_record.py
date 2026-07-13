from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from control_plane.contracts.data_provenance import FreshnessStatus


RouteBindingStatus = Literal["active", "disabled"]
RouteBindingTerminationKind = Literal["edge", "direct", "none"]
RouteBindingIngressProvider = str
RouteBindingTlsOwner = Literal["launchplane", "provider", "external", "none"]

_SECRET_KEY_TOKENS = (
    "secret",
    "token",
    "password",
    "passwd",
    "credential",
    "private_key",
    "apikey",
    "api_key",
)
_EVIDENCE_VALUE_FORBIDDEN_TOKENS = ("://", "/", "\\")


class RouteBindingProviderTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str
    target_category: str
    provider_target_type: str = ""
    target_name: str
    provider_evidence: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_target(self) -> "RouteBindingProviderTarget":
        self.provider_id = _required_text(
            self.provider_id, "route binding provider target requires provider_id"
        ).lower()
        self.target_category = _required_text(
            self.target_category, "route binding provider target requires target_category"
        ).lower()
        self.provider_target_type = self.provider_target_type.strip().lower()
        self.target_name = _required_text(
            self.target_name, "route binding provider target requires target_name"
        )
        self.provider_evidence = _normalized_provider_evidence(self.provider_evidence)
        return self


class RouteBindingIngress(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: RouteBindingIngressProvider
    endpoint_key: str = ""
    termination_kind: RouteBindingTerminationKind
    provider_evidence: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_ingress(self) -> "RouteBindingIngress":
        self.provider = _required_text(
            self.provider, "route binding ingress requires provider"
        ).lower()
        self.endpoint_key = self.endpoint_key.strip()
        self.provider_evidence = _normalized_provider_evidence(self.provider_evidence)
        if self.provider == "none":
            if self.termination_kind != "none":
                raise ValueError(
                    "route binding ingress provider=none requires termination_kind=none"
                )
            if self.endpoint_key:
                raise ValueError("route binding ingress provider=none cannot include endpoint_key")
            if self.provider_evidence:
                raise ValueError(
                    "route binding ingress provider=none cannot include provider evidence"
                )
        else:
            if self.termination_kind == "none":
                raise ValueError("route binding ingress provider requires a termination kind")
            if self.termination_kind == "edge" and not self.endpoint_key:
                raise ValueError("edge-terminated route binding requires endpoint_key")
        return self


class RouteBindingDomain(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain_name: str
    role: Literal["primary", "alias"] = "primary"

    @field_validator("domain_name", mode="after")
    @classmethod
    def _validate_domain(cls, value: str) -> str:
        return _normalized_domain(value)


class RouteBindingTls(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner: RouteBindingTlsOwner
    provider_evidence: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_tls(self) -> "RouteBindingTls":
        self.provider_evidence = _normalized_provider_evidence(self.provider_evidence)
        if self.owner == "none":
            if self.provider_evidence:
                raise ValueError("route binding TLS owner=none cannot include provider evidence")
        return self


class RouteBindingProviderTargetReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str
    target_category: str
    provider_target_type: str = ""
    target_name: str


class RouteBindingIngressReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: RouteBindingIngressProvider
    endpoint_key: str = ""
    termination_kind: RouteBindingTerminationKind


class RouteBindingTlsReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner: RouteBindingTlsOwner


class RouteBindingSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_kind: Literal["operator", "backfill", "service"] = "operator"
    source_label: str
    source_record_ids: tuple[str, ...] = ()
    source_versions: dict[str, str] = Field(default_factory=dict)
    refreshed_at: str
    freshness_status: FreshnessStatus = "recorded"
    stale_after: str = ""

    @model_validator(mode="after")
    def _validate_source(self) -> "RouteBindingSource":
        self.source_label = _required_text(
            self.source_label, "route binding source requires source_label"
        )
        self.refreshed_at = _required_text(
            self.refreshed_at, "route binding source requires refreshed_at"
        )
        self.stale_after = self.stale_after.strip()
        source_ids: list[str] = []
        for raw_source_id in self.source_record_ids:
            source_id = raw_source_id.strip()
            if not source_id:
                raise ValueError("route binding source record ids must be non-empty")
            if source_id not in source_ids:
                source_ids.append(source_id)
        self.source_record_ids = tuple(source_ids)
        source_versions: dict[str, str] = {}
        for raw_source_id, raw_version in self.source_versions.items():
            source_id = raw_source_id.strip()
            version = raw_version.strip()
            if not source_id or not version:
                raise ValueError("route binding source versions must be non-empty")
            source_versions[source_id] = version
        if self.source_kind == "backfill" and set(source_versions) != set(source_ids):
            raise ValueError(
                "backfilled route binding source versions must match source record ids"
            )
        self.source_versions = source_versions
        return self


class EnvironmentRouteBindingRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    context: str
    instance: str
    provider_target: RouteBindingProviderTarget
    ingress: RouteBindingIngress
    domains: tuple[RouteBindingDomain, ...]
    tls: RouteBindingTls
    status: RouteBindingStatus = "active"
    source: RouteBindingSource
    updated_at: str

    @field_validator("product", "context", "instance", "updated_at", mode="after")
    @classmethod
    def _validate_required_text(cls, value: str) -> str:
        return _required_text(value, "route binding record requires non-empty text fields")

    @model_validator(mode="after")
    def _validate_record(self) -> "EnvironmentRouteBindingRecord":
        if self.schema_version != 1:
            raise ValueError("Unsupported environment route binding schema version")
        if not self.domains and self.ingress.termination_kind != "none":
            raise ValueError("route binding requires domains unless ingress termination is none")
        normalized_domains: list[RouteBindingDomain] = []
        seen_domains: set[str] = set()
        primary_count = 0
        for domain in self.domains:
            if domain.domain_name in seen_domains:
                raise ValueError("route binding domains must be unique")
            seen_domains.add(domain.domain_name)
            if domain.role == "primary":
                primary_count += 1
            normalized_domains.append(domain)
        if normalized_domains and primary_count != 1:
            raise ValueError("route binding requires exactly one primary domain")
        if self.tls.owner == "launchplane" and self.ingress.provider == "none":
            raise ValueError("Launchplane-owned TLS requires an ingress provider")
        return self

    @property
    def binding_key(self) -> str:
        return build_route_binding_key(
            product=self.product,
            context=self.context,
            instance=self.instance,
        )


class EnvironmentRouteBindingReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int
    product: str
    context: str
    instance: str
    provider_target: RouteBindingProviderTargetReadModel
    ingress: RouteBindingIngressReadModel
    domains: tuple[RouteBindingDomain, ...]
    tls: RouteBindingTlsReadModel
    status: RouteBindingStatus
    source: RouteBindingSource
    updated_at: str


def build_route_binding_key(*, product: str, context: str, instance: str) -> str:
    return json.dumps(
        [
            _required_text(product, "route binding key requires product"),
            _required_text(context, "route binding key requires context"),
            _required_text(instance, "route binding key requires instance"),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def redacted_route_binding_record(
    record: EnvironmentRouteBindingRecord,
) -> EnvironmentRouteBindingReadModel:
    return EnvironmentRouteBindingReadModel(
        schema_version=record.schema_version,
        product=record.product,
        context=record.context,
        instance=record.instance,
        provider_target=RouteBindingProviderTargetReadModel(
            provider_id=record.provider_target.provider_id,
            target_category=record.provider_target.target_category,
            provider_target_type=record.provider_target.provider_target_type,
            target_name=record.provider_target.target_name,
        ),
        ingress=RouteBindingIngressReadModel(
            provider=record.ingress.provider,
            endpoint_key=record.ingress.endpoint_key,
            termination_kind=record.ingress.termination_kind,
        ),
        domains=record.domains,
        tls=RouteBindingTlsReadModel(
            owner=record.tls.owner,
        ),
        status=record.status,
        source=record.source,
        updated_at=record.updated_at,
    )


def _required_text(value: str, message: str) -> str:
    normalized_value = value.strip()
    if not normalized_value:
        raise ValueError(message)
    return normalized_value


def _normalized_domain(value: str) -> str:
    domain_name = value.strip().rstrip(".").lower()
    if (
        not domain_name
        or any(character.isspace() for character in domain_name)
        or any(separator in domain_name for separator in (":", "/", "\\", ","))
        or "." not in domain_name
    ):
        raise ValueError("route binding domain_name must be one DNS name")
    return domain_name


def _normalized_provider_evidence(evidence: dict[str, str]) -> dict[str, str]:
    normalized_evidence: dict[str, str] = {}
    for raw_key, raw_value in evidence.items():
        key = raw_key.strip().lower()
        value = raw_value.strip()
        if not key:
            raise ValueError("route binding provider evidence keys must be non-empty")
        if any(token in key.replace("-", "_") for token in _SECRET_KEY_TOKENS):
            raise ValueError("route binding provider evidence cannot include secret-shaped keys")
        if not value:
            raise ValueError("route binding provider evidence values must be non-empty")
        if any(token in value for token in _EVIDENCE_VALUE_FORBIDDEN_TOKENS):
            raise ValueError(
                "route binding provider evidence values must not contain URLs or paths"
            )
        normalized_evidence[key] = value
    return normalized_evidence
