from __future__ import annotations

import hashlib
import json
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.data_provenance import FreshnessStatus
from control_plane.contracts.product_health_monitoring_migration import (
    canonical_health_check_record_token,
)
from control_plane.contracts.product_health_monitoring_migration import health_check_record_token
from control_plane.contracts.product_profile_record import ProductLaneMonitoringIntent
from control_plane.contracts.route_binding_record import RouteBindingStatus
from control_plane.contracts.route_binding_record import RouteBindingTerminationKind
from control_plane.contracts.route_binding_record import RouteBindingTlsOwner
from control_plane.contracts.runtime_identity import RuntimeIdentity, RuntimeIdentityStatus


PublicIngressObservationStatus = Literal["pass", "fail", "skipped"]
PublicIngressObservationPurpose = Literal["probe", "reconciliation"]
PublicIngressIncidentStatus = Literal["open", "resolved"]
PublicIngressIncidentResolutionReason = Literal[
    "recovered",
    "monitoring_intent_changed",
    "duplicate_reconciled",
]
PublicIngressIncidentEvent = Literal["opened", "updated", "reminder", "resolved"]
PublicIngressIncidentEventKind = PublicIngressIncidentEvent | Literal["baseline"]
PublicIngressIncidentEventReason = Literal[
    "incident_opened",
    "material_change",
    "reminder_due",
    "recovered",
    "monitoring_intent_changed",
    "migration_baseline",
    "duplicate_reconciled",
]
PublicIngressIncidentSeverity = Literal["warning", "critical"]
PublicIngressIncidentNotificationState = Literal["active", "acknowledged", "silenced"]
PublicIngressIncidentEventSuppressionReason = Literal[
    "",
    "acknowledged",
    "silenced",
    "migration_reconciliation",
]
PublicIngressIncidentReminderStatus = Literal[
    "active",
    "suppressed",
    "policy_inactive",
    "resolved",
]
PublicIngressFailureLayer = Literal[
    "configuration",
    "dns",
    "network",
    "redirect",
    "http",
    "tls",
    "runtime_identity",
    "provider",
    "unknown",
]
PublicIngressNotificationDestinationKind = Literal["github_issue", "email", "discord"]
PublicIngressNotificationStatus = Literal["enabled", "disabled"]
PublicIngressNotificationDeliveryStatus = Literal["delivered", "skipped", "failed"]
PublicIngressCheckKind = Literal["public_http", "private_http", "provider", "tls"]
PublicIngressHealthCheckKind = PublicIngressCheckKind
PublicIngressNotificationCheckKind = PublicIngressCheckKind
PublicIngressTargetKind = Literal[
    "base_url",
    "health_url",
    "private_health_url",
    "monitoring_intent",
    "provider",
    "tls_domain",
]
PublicIngressFailureCode = Literal[
    "connection_timeout",
    "dns_failure",
    "health_status_error",
    "http_error",
    "invalid_url",
    "private_endpoint_disabled",
    "private_endpoint_mismatch",
    "private_endpoint_not_found",
    "private_url",
    "monitoring_intent_changed",
    "provider_check_unavailable",
    "redirect_loop",
    "self_redirect",
    "tls_chain_failure",
    "tls_expired",
    "tls_expiring",
    "tls_failure",
    "tls_hostname_mismatch",
    "tls_self_signed",
    "tls_unsupported",
    "wrong_runtime_identity",
    "unknown_error",
]

PublicIngressTlsStatus = Literal[
    "valid",
    "expiring",
    "expired",
    "hostname_mismatch",
    "untrusted",
    "self_signed",
    "unreachable",
    "unknown",
    "unsupported",
]
PublicIngressTlsNameMatchSource = Literal["san", "subject", "none"]
PublicIngressRouteBindingSourceKind = Literal["operator", "backfill", "service"]

PUBLIC_TLS_EXPIRING_DAYS = 14
PUBLIC_HTTP_STALE_AFTER_SECONDS = 2 * 60 * 60
PUBLIC_TLS_STALE_AFTER_SECONDS = 2 * 60 * 60
PUBLIC_INGRESS_DEFAULT_REMINDER_INTERVAL_SECONDS = 6 * 60 * 60
PUBLIC_INGRESS_MIN_REMINDER_INTERVAL_SECONDS = 15 * 60
PUBLIC_INGRESS_MAX_REMINDER_INTERVAL_SECONDS = 7 * 24 * 60 * 60


class PublicIngressTlsRecordedEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain_name: str
    domain_role: Literal["primary", "alias"]
    route_binding_status: RouteBindingStatus = "active"
    owner: RouteBindingTlsOwner
    ingress_provider: str = ""
    termination_kind: RouteBindingTerminationKind
    source_kind: PublicIngressRouteBindingSourceKind = "service"
    source_label: str
    source_record_ids: tuple[str, ...] = ()
    source_versions: dict[str, str] = Field(default_factory=dict)
    refreshed_at: str
    recorded_at: str
    freshness_status: FreshnessStatus = "recorded"
    stale_after: str = ""
    provider_evidence: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_recorded_evidence(self) -> "PublicIngressTlsRecordedEvidence":
        self.domain_name = _required_text(
            self.domain_name, "public ingress TLS evidence requires domain_name"
        ).lower()
        self.ingress_provider = self.ingress_provider.strip().lower()
        self.source_label = _required_text(
            self.source_label, "public ingress TLS evidence requires source_label"
        )
        self.refreshed_at = _required_text(
            self.refreshed_at, "public ingress TLS evidence requires refreshed_at"
        )
        self.recorded_at = _required_text(
            self.recorded_at, "public ingress TLS evidence requires recorded_at"
        )
        self.stale_after = self.stale_after.strip()
        self.source_record_ids = tuple(
            _required_text(
                source_record_id,
                "public ingress TLS evidence source_record_ids must be non-empty",
            )
            for source_record_id in self.source_record_ids
        )
        self.source_versions = {
            _required_text(
                source_record_id,
                "public ingress TLS evidence source version ids must be non-empty",
            ): _required_text(
                source_version,
                "public ingress TLS evidence source versions must be non-empty",
            )
            for source_record_id, source_version in self.source_versions.items()
        }
        self.provider_evidence = {
            _required_text(
                evidence_key,
                "public ingress TLS evidence provider evidence keys must be non-empty",
            ): _required_text(
                evidence_value,
                "public ingress TLS evidence provider evidence values must be non-empty",
            )
            for evidence_key, evidence_value in self.provider_evidence.items()
        }
        return self


class PublicIngressTlsProbeEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["active_probe"] = "active_probe"
    observed_at: str
    validated_address_count: int = Field(default=0, ge=0)
    sni_hostname: str
    freshness_status: FreshnessStatus = "verified"
    stale_after: str = ""

    @model_validator(mode="after")
    def _validate_probe_evidence(self) -> "PublicIngressTlsProbeEvidence":
        self.observed_at = _required_text(
            self.observed_at, "public ingress TLS probe evidence requires observed_at"
        )
        self.sni_hostname = _required_text(
            self.sni_hostname, "public ingress TLS probe evidence requires sni_hostname"
        ).lower()
        self.stale_after = self.stale_after.strip()
        return self


class PublicIngressTlsObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: PublicIngressTlsStatus
    public_name: str
    incident_id: str = ""
    issuer: str = ""
    subject: str = ""
    not_before: str = ""
    not_after: str = ""
    days_remaining: int | None = None
    public_name_match: bool = False
    public_name_match_source: PublicIngressTlsNameMatchSource = "none"
    presented_san_count: int = Field(default=0, ge=0)
    presented_name_evidence: tuple[str, ...] = ()
    recorded: PublicIngressTlsRecordedEvidence
    probe: PublicIngressTlsProbeEvidence

    @model_validator(mode="after")
    def _validate_tls_observation(self) -> "PublicIngressTlsObservation":
        self.public_name = _required_text(
            self.public_name, "public ingress TLS observation requires public_name"
        ).lower()
        self.incident_id = self.incident_id.strip()
        self.issuer = self.issuer.strip()
        self.subject = self.subject.strip()
        self.not_before = self.not_before.strip()
        self.not_after = self.not_after.strip()
        self.presented_name_evidence = tuple(
            _required_text(
                presented_name,
                "public ingress TLS presented_name_evidence must be non-empty",
            ).lower()
            for presented_name in self.presented_name_evidence
        )
        if self.status in {
            "valid",
            "expiring",
            "expired",
            "hostname_mismatch",
            "untrusted",
            "self_signed",
        }:
            if not (self.not_before and self.not_after):
                raise ValueError(
                    "public ingress TLS certificate states require not_before and not_after"
                )
        return self


class PublicIngressTargetObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: PublicIngressTargetKind
    url: str
    status: PublicIngressObservationStatus
    failure_code: PublicIngressFailureCode | None = None
    http_status: int | None = Field(default=None, ge=100, le=599)
    final_url: str = ""
    redirect_count: int = Field(default=0, ge=0)
    runtime_identity_status: RuntimeIdentityStatus = "unchecked"
    runtime_identity_detail: str = ""
    observed_runtime_identity: RuntimeIdentity | None = None
    tls: PublicIngressTlsObservation | None = None
    summary: str

    @model_validator(mode="after")
    def _validate_observation(self) -> "PublicIngressTargetObservation":
        self.url = _required_text(self.url, "public ingress target observation requires url")
        self.final_url = self.final_url.strip()
        self.summary = _required_text(
            self.summary, "public ingress target observation requires summary"
        )
        if self.status == "pass" and self.failure_code is not None:
            raise ValueError("passing public ingress target cannot include failure_code")
        if self.status == "fail" and self.failure_code is None:
            raise ValueError("failing public ingress target requires failure_code")
        if self.status == "skipped" and self.failure_code not in {
            None,
            "monitoring_intent_changed",
            "private_url",
            "tls_unsupported",
        }:
            raise ValueError("skipped public ingress target has an unsupported failure_code")
        if self.target == "tls_domain":
            if self.tls is None:
                raise ValueError("TLS public ingress target requires TLS evidence")
        elif self.tls is not None:
            raise ValueError("non-TLS public ingress targets cannot include TLS evidence")
        return self


class PublicIngressObservationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    record_id: str
    product: str
    repository: str = ""
    driver_id: str = ""
    context: str
    instance: str
    check_name: str = "public-ingress"
    check_kind: PublicIngressCheckKind = "public_http"
    purpose: PublicIngressObservationPurpose = "probe"
    monitoring_intent: ProductLaneMonitoringIntent | Literal["legacy"] = "legacy"
    observed_at: str
    status: PublicIngressObservationStatus
    failure_code: PublicIngressFailureCode | None = None
    base_url: str = ""
    health_url: str = ""
    expected_runtime_identity: RuntimeIdentity | None = None
    targets: tuple[PublicIngressTargetObservation, ...] = ()
    incident_id: str = ""
    incident_event_id: str = ""
    material_fingerprint: PublicIngressIncidentMaterialFingerprint | None = None
    material_fingerprint_sha256: str = ""
    notification_key: str = ""
    notification_sent: bool = False
    summary: str

    @model_validator(mode="after")
    def _validate_record(self) -> "PublicIngressObservationRecord":
        self.record_id = _required_text(
            self.record_id, "public ingress observation requires record_id"
        )
        self.product = _required_text(self.product, "public ingress observation requires product")
        self.context = _required_text(self.context, "public ingress observation requires context")
        self.instance = _required_text(
            self.instance, "public ingress observation requires instance"
        )
        self.observed_at = _required_text(
            self.observed_at, "public ingress observation requires observed_at"
        )
        self.repository = self.repository.strip()
        self.driver_id = self.driver_id.strip()
        self.check_name = _required_text(
            self.check_name, "public ingress observation requires check_name"
        )
        self.base_url = self.base_url.strip()
        self.health_url = self.health_url.strip()
        self.incident_id = self.incident_id.strip()
        self.incident_event_id = self.incident_event_id.strip()
        self.material_fingerprint_sha256 = self.material_fingerprint_sha256.strip().lower()
        self.notification_key = self.notification_key.strip()
        self.summary = _required_text(self.summary, "public ingress observation requires summary")
        if self.status == "pass" and self.failure_code is not None:
            raise ValueError("passing public ingress observation cannot include failure_code")
        if self.status == "fail" and self.failure_code is None:
            raise ValueError("failing public ingress observation requires failure_code")
        if self.status == "skipped" and self.failure_code not in {
            None,
            "monitoring_intent_changed",
            "private_url",
            "tls_unsupported",
        }:
            raise ValueError("skipped public ingress observation has an unsupported failure_code")
        if not self.targets:
            raise ValueError("public ingress observation requires at least one target")
        if self.material_fingerprint is not None:
            expected_fingerprint = public_ingress_material_fingerprint_sha256(
                self.material_fingerprint
            )
            if self.material_fingerprint_sha256 != expected_fingerprint:
                raise ValueError("public ingress observation material fingerprint digest mismatch")
        if self.status == "pass" and any(target.status != "pass" for target in self.targets):
            raise ValueError("passing public ingress observation requires all targets to pass")
        if self.status == "fail" and not any(target.status == "fail" for target in self.targets):
            raise ValueError("failing public ingress observation requires a failing target")
        if self.purpose == "reconciliation":
            if self.status != "skipped" or self.failure_code != "monitoring_intent_changed":
                raise ValueError(
                    "public ingress reconciliation observation requires skipped monitoring_intent_changed status"
                )
            if any(target.target != "monitoring_intent" for target in self.targets):
                raise ValueError(
                    "public ingress reconciliation observation requires monitoring_intent targets"
                )
        elif any(target.target == "monitoring_intent" for target in self.targets):
            raise ValueError(
                "public ingress probe observations cannot use monitoring_intent targets"
            )
        return self


class PublicIngressIncidentRouteAuthority(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["public_urls", "private_endpoint", "provider", "route_binding"]
    target_keys: tuple[str, ...] = ()
    private_endpoint_key: str = ""
    private_endpoint_sha256: str = ""
    provider: str = ""
    provider_check: str = ""
    domain_name: str = ""
    route_binding_status: RouteBindingStatus | Literal[""] = ""
    ingress_provider: str = ""
    termination_kind: RouteBindingTerminationKind | Literal[""] = ""
    tls_owner: RouteBindingTlsOwner | Literal[""] = ""

    @model_validator(mode="after")
    def _validate_route_authority(self) -> "PublicIngressIncidentRouteAuthority":
        self.target_keys = tuple(
            sorted(
                {
                    _required_text(
                        value,
                        "public ingress material route authority target keys must be non-empty",
                    )
                    for value in self.target_keys
                }
            )
        )
        self.private_endpoint_key = self.private_endpoint_key.strip()
        self.private_endpoint_sha256 = self.private_endpoint_sha256.strip().lower()
        self.provider = self.provider.strip().lower()
        self.provider_check = self.provider_check.strip().lower()
        self.domain_name = self.domain_name.strip().lower().rstrip(".")
        self.ingress_provider = self.ingress_provider.strip().lower()
        if self.kind == "public_urls" and not self.target_keys:
            raise ValueError("public URL route authority requires target_keys")
        if self.kind == "private_endpoint" and not self.private_endpoint_key:
            raise ValueError("private endpoint route authority requires private_endpoint_key")
        if self.kind == "provider" and not (self.provider and self.provider_check):
            raise ValueError("provider route authority requires provider and provider_check")
        if self.kind == "route_binding" and not self.domain_name:
            raise ValueError("route binding authority requires domain_name")
        return self


class PublicIngressIncidentRuntimeIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str = ""
    context: str
    instance: str
    environment_kind: str = "stable"
    deployment_record_id: str
    artifact_id: str
    source_git_ref: str


class PublicIngressIncidentRuntimeMismatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["mismatch", "missing", "malformed", "unverifiable"]
    mismatched_fields: tuple[str, ...] = ()
    expected_runtime_identity: PublicIngressIncidentRuntimeIdentity | None = None
    observed_runtime_identity: PublicIngressIncidentRuntimeIdentity | None = None

    @model_validator(mode="after")
    def _validate_runtime_mismatch(self) -> "PublicIngressIncidentRuntimeMismatch":
        self.mismatched_fields = tuple(
            sorted(
                {
                    _required_text(
                        value,
                        "public ingress runtime mismatch fields must be non-empty",
                    )
                    for value in self.mismatched_fields
                }
            )
        )
        return self


class PublicIngressIncidentMaterialFingerprint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    check_kind: PublicIngressCheckKind
    failure_code: PublicIngressFailureCode
    failure_layer: PublicIngressFailureLayer
    severity: PublicIngressIncidentSeverity
    affected_targets: tuple[PublicIngressTargetKind, ...]
    route_authority: PublicIngressIncidentRouteAuthority
    runtime_mismatch: PublicIngressIncidentRuntimeMismatch | None = None
    tls_status: PublicIngressTlsStatus | Literal[""] = ""

    @model_validator(mode="after")
    def _validate_material_fingerprint(self) -> "PublicIngressIncidentMaterialFingerprint":
        self.affected_targets = tuple(sorted(set(self.affected_targets)))
        if not self.affected_targets:
            raise ValueError("public ingress material fingerprint requires affected_targets")
        if self.failure_code == "wrong_runtime_identity" and self.runtime_mismatch is None:
            raise ValueError("runtime identity failure requires typed mismatch evidence")
        if self.failure_code != "wrong_runtime_identity" and self.runtime_mismatch is not None:
            raise ValueError("only runtime identity failures can include mismatch evidence")
        if self.check_kind == "tls" and not self.tls_status:
            raise ValueError("TLS material fingerprint requires tls_status")
        if self.check_kind != "tls" and self.tls_status:
            raise ValueError("non-TLS material fingerprint cannot include tls_status")
        return self


class PublicIngressIncidentRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    incident_id: str
    product: str
    repository: str = ""
    driver_id: str = ""
    context: str
    instance: str
    check_name: str = "public-ingress"
    check_kind: PublicIngressCheckKind = "public_http"
    status: PublicIngressIncidentStatus
    opened_at: str
    opened_observation_id: str
    latest_observation_id: str
    latest_observed_at: str
    failure_code: PublicIngressFailureCode
    resolved_at: str = ""
    resolved_observation_id: str = ""
    resolution_reason: PublicIngressIncidentResolutionReason | Literal[""] = ""
    state_version: int = Field(default=0, ge=0)
    severity: PublicIngressIncidentSeverity = "critical"
    material_fingerprint: PublicIngressIncidentMaterialFingerprint | None = None
    material_fingerprint_sha256: str = ""
    material_fingerprint_complete: bool = False
    latest_material_event_id: str = ""
    latest_material_event: PublicIngressIncidentEventKind | Literal[""] = ""
    latest_material_event_at: str = ""
    notification_state: PublicIngressIncidentNotificationState = "active"
    notification_state_changed_at: str = ""
    notification_state_reason: str = ""
    silenced_until: str = ""
    recovery_observation_threshold: int = Field(default=1, ge=1, le=10)
    consecutive_recovery_observations: int = Field(default=0, ge=0)
    latest_recovery_observation_id: str = ""
    summary: str

    @model_validator(mode="after")
    def _validate_incident(self) -> "PublicIngressIncidentRecord":
        self.incident_id = _required_text(
            self.incident_id, "public ingress incident requires incident_id"
        )
        self.product = _required_text(self.product, "public ingress incident requires product")
        self.context = _required_text(self.context, "public ingress incident requires context")
        self.instance = _required_text(self.instance, "public ingress incident requires instance")
        self.opened_at = _required_text(
            self.opened_at, "public ingress incident requires opened_at"
        )
        self.opened_observation_id = _required_text(
            self.opened_observation_id,
            "public ingress incident requires opened_observation_id",
        )
        self.latest_observation_id = _required_text(
            self.latest_observation_id,
            "public ingress incident requires latest_observation_id",
        )
        self.latest_observed_at = _required_text(
            self.latest_observed_at,
            "public ingress incident requires latest_observed_at",
        )
        self.repository = self.repository.strip()
        self.driver_id = self.driver_id.strip()
        self.check_name = _required_text(
            self.check_name, "public ingress incident requires check_name"
        )
        self.resolved_at = self.resolved_at.strip()
        self.resolved_observation_id = self.resolved_observation_id.strip()
        self.material_fingerprint_sha256 = self.material_fingerprint_sha256.strip().lower()
        self.latest_material_event_id = self.latest_material_event_id.strip()
        self.latest_material_event_at = self.latest_material_event_at.strip()
        self.notification_state_changed_at = self.notification_state_changed_at.strip()
        self.notification_state_reason = self.notification_state_reason.strip()
        self.silenced_until = self.silenced_until.strip()
        self.latest_recovery_observation_id = self.latest_recovery_observation_id.strip()
        self.summary = _required_text(self.summary, "public ingress incident requires summary")
        if self.schema_version >= 2:
            if self.state_version < 1:
                raise ValueError("public ingress incident schema v2 requires state_version")
            if self.material_fingerprint is None or not self.material_fingerprint_sha256:
                raise ValueError("public ingress incident schema v2 requires material fingerprint")
            if not self.latest_material_event_id or not self.latest_material_event_at:
                raise ValueError("public ingress incident schema v2 requires latest material event")
            if not self.latest_material_event:
                raise ValueError("public ingress incident schema v2 requires latest event kind")
        if self.material_fingerprint is not None:
            expected_fingerprint = public_ingress_material_fingerprint_sha256(
                self.material_fingerprint
            )
            if self.material_fingerprint_sha256 != expected_fingerprint:
                raise ValueError("public ingress incident material fingerprint digest mismatch")
            if self.severity != self.material_fingerprint.severity:
                raise ValueError("public ingress incident severity must match material fingerprint")
        if self.notification_state == "silenced" and not self.silenced_until:
            raise ValueError("silenced public ingress incident requires silenced_until")
        if self.notification_state != "silenced" and self.silenced_until:
            raise ValueError("only silenced public ingress incidents can include silenced_until")
        if self.consecutive_recovery_observations > self.recovery_observation_threshold:
            raise ValueError("public ingress incident recovery count exceeds threshold")
        if self.status == "resolved":
            if not self.resolved_at:
                raise ValueError("resolved public ingress incident requires resolved_at")
            if not self.resolved_observation_id:
                raise ValueError(
                    "resolved public ingress incident requires resolved_observation_id"
                )
            if not self.resolution_reason:
                self.resolution_reason = "recovered"
        if self.status == "open" and (
            self.resolved_at or self.resolved_observation_id or self.resolution_reason
        ):
            raise ValueError("open public ingress incident cannot include resolution fields")
        return self


class PublicIngressIncidentEventRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    event_id: str
    incident_id: str
    event: PublicIngressIncidentEventKind
    reason: PublicIngressIncidentEventReason
    occurred_at: str
    observation_id: str
    material_fingerprint_sha256: str
    previous_material_fingerprint_sha256: str = ""
    severity: PublicIngressIncidentSeverity
    policy_id: str = ""
    reminder_window_index: int | None = Field(default=None, ge=1)
    reminder_window_started_at: str = ""
    reminder_window_ended_at: str = ""
    delivery_eligible: bool = True
    suppression_reason: PublicIngressIncidentEventSuppressionReason = ""
    summary: str

    @model_validator(mode="after")
    def _validate_event(self) -> "PublicIngressIncidentEventRecord":
        self.event_id = _required_text(
            self.event_id, "public ingress incident event requires event_id"
        )
        self.incident_id = _required_text(
            self.incident_id, "public ingress incident event requires incident_id"
        )
        self.occurred_at = _required_text(
            self.occurred_at, "public ingress incident event requires occurred_at"
        )
        self.observation_id = _required_text(
            self.observation_id, "public ingress incident event requires observation_id"
        )
        self.material_fingerprint_sha256 = _required_text(
            self.material_fingerprint_sha256,
            "public ingress incident event requires material_fingerprint_sha256",
        ).lower()
        self.previous_material_fingerprint_sha256 = (
            self.previous_material_fingerprint_sha256.strip().lower()
        )
        self.policy_id = self.policy_id.strip()
        self.reminder_window_started_at = self.reminder_window_started_at.strip()
        self.reminder_window_ended_at = self.reminder_window_ended_at.strip()
        self.summary = _required_text(
            self.summary, "public ingress incident event requires summary"
        )
        if self.event == "reminder":
            if not self.policy_id or self.reminder_window_index is None:
                raise ValueError("public ingress reminder event requires policy window identity")
            if not self.reminder_window_started_at or not self.reminder_window_ended_at:
                raise ValueError("public ingress reminder event requires bounded window")
        elif self.policy_id or self.reminder_window_index is not None:
            raise ValueError("non-reminder public ingress event cannot include reminder identity")
        if self.event == "baseline" and self.delivery_eligible:
            raise ValueError("public ingress migration baseline cannot be delivery eligible")
        if self.delivery_eligible and self.suppression_reason:
            raise ValueError("delivery-eligible incident event cannot include suppression reason")
        if not self.delivery_eligible and not self.suppression_reason and self.event != "baseline":
            raise ValueError("suppressed incident event requires suppression reason")
        return self


class PublicIngressIncidentReminderStateRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    reminder_state_id: str
    incident_id: str
    policy_id: str
    status: PublicIngressIncidentReminderStatus = "active"
    material_event_id: str
    interval_seconds: int = Field(
        ge=PUBLIC_INGRESS_MIN_REMINDER_INTERVAL_SECONDS,
        le=PUBLIC_INGRESS_MAX_REMINDER_INTERVAL_SECONDS,
    )
    window_anchor_at: str
    last_window_index: int = Field(default=0, ge=0)
    last_reminder_event_id: str = ""
    last_reminded_at: str = ""
    next_reminder_at: str = ""
    updated_at: str

    @model_validator(mode="after")
    def _validate_reminder_state(self) -> "PublicIngressIncidentReminderStateRecord":
        self.reminder_state_id = _required_text(
            self.reminder_state_id, "public ingress reminder state requires reminder_state_id"
        )
        self.incident_id = _required_text(
            self.incident_id, "public ingress reminder state requires incident_id"
        )
        self.policy_id = _required_text(
            self.policy_id, "public ingress reminder state requires policy_id"
        )
        self.material_event_id = _required_text(
            self.material_event_id, "public ingress reminder state requires material_event_id"
        )
        self.window_anchor_at = _required_text(
            self.window_anchor_at, "public ingress reminder state requires window_anchor_at"
        )
        self.last_reminder_event_id = self.last_reminder_event_id.strip()
        self.last_reminded_at = self.last_reminded_at.strip()
        self.next_reminder_at = self.next_reminder_at.strip()
        self.updated_at = _required_text(
            self.updated_at, "public ingress reminder state requires updated_at"
        )
        if self.status == "active" and not self.next_reminder_at:
            raise ValueError("active public ingress reminder state requires next_reminder_at")
        if self.last_window_index and not (self.last_reminder_event_id and self.last_reminded_at):
            raise ValueError("reminded public ingress state requires event and timestamp")
        return self


class PublicIngressNotificationDestination(BaseModel):
    model_config = ConfigDict(extra="forbid")

    destination_id: str
    kind: PublicIngressNotificationDestinationKind
    status: PublicIngressNotificationStatus = "enabled"
    github_repository: str = ""
    github_issue_number: int | None = Field(default=None, ge=1)
    github_label: str = ""
    email_to: tuple[str, ...] = ()
    email_from: str = ""
    smtp_host: str = ""
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_username_secret: str = ""
    smtp_password_secret: str = ""
    discord_webhook_secret: str = ""

    @model_validator(mode="after")
    def _validate_destination(self) -> "PublicIngressNotificationDestination":
        self.destination_id = _required_text(
            self.destination_id, "public ingress notification destination requires destination_id"
        )
        self.github_repository = self.github_repository.strip()
        self.github_label = self.github_label.strip()
        self.email_from = self.email_from.strip()
        self.email_to = tuple(_normalize_optional_text(value) for value in self.email_to)
        if any(not value for value in self.email_to):
            raise ValueError("public ingress email notification recipients must be non-empty")
        self.smtp_host = self.smtp_host.strip()
        self.smtp_username_secret = self.smtp_username_secret.strip()
        self.smtp_password_secret = self.smtp_password_secret.strip()
        self.discord_webhook_secret = self.discord_webhook_secret.strip()
        if self.kind == "github_issue":
            if not self.github_repository or "/" not in self.github_repository:
                raise ValueError(
                    "public ingress GitHub notification destination requires owner/name repository"
                )
            if self.github_issue_number is None and not self.github_label:
                raise ValueError(
                    "public ingress GitHub notification destination requires issue number or label"
                )
        elif self.kind == "email":
            if not self.email_to:
                raise ValueError("public ingress email notification destination requires email_to")
            if not self.email_from:
                raise ValueError(
                    "public ingress email notification destination requires email_from"
                )
            if not self.smtp_host:
                raise ValueError("public ingress email notification destination requires smtp_host")
            if not self.smtp_username_secret:
                raise ValueError(
                    "public ingress email notification destination requires smtp_username_secret"
                )
            if not self.smtp_password_secret:
                raise ValueError(
                    "public ingress email notification destination requires smtp_password_secret"
                )
        elif self.kind == "discord":
            if not self.discord_webhook_secret:
                raise ValueError(
                    "public ingress Discord notification destination requires discord_webhook_secret"
                )
        return self


class PublicIngressNotificationPolicyRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    policy_id: str
    product: str = ""
    context: str = ""
    instance: str = ""
    check_name: str = ""
    check_kind: PublicIngressNotificationCheckKind | Literal[""] = ""
    status: PublicIngressNotificationStatus = "enabled"
    reminder_interval_seconds: int = Field(
        default=PUBLIC_INGRESS_DEFAULT_REMINDER_INTERVAL_SECONDS,
        ge=PUBLIC_INGRESS_MIN_REMINDER_INTERVAL_SECONDS,
        le=PUBLIC_INGRESS_MAX_REMINDER_INTERVAL_SECONDS,
    )
    destinations: tuple[PublicIngressNotificationDestination, ...] = ()
    created_at: str
    updated_at: str
    source: str = ""

    @model_validator(mode="after")
    def _validate_policy(self) -> "PublicIngressNotificationPolicyRecord":
        self.policy_id = _required_text(
            self.policy_id, "public ingress notification policy requires policy_id"
        )
        self.product = self.product.strip()
        self.context = self.context.strip()
        self.instance = self.instance.strip()
        self.check_name = self.check_name.strip()
        self.created_at = _required_text(
            self.created_at, "public ingress notification policy requires created_at"
        )
        self.updated_at = _required_text(
            self.updated_at, "public ingress notification policy requires updated_at"
        )
        self.source = self.source.strip()
        if not self.destinations:
            raise ValueError("public ingress notification policy requires destinations")
        destination_ids = [destination.destination_id for destination in self.destinations]
        if len(set(destination_ids)) != len(destination_ids):
            raise ValueError("public ingress notification destination ids must be unique")
        if self.instance and not self.context:
            raise ValueError(
                "public ingress notification policy with instance scope requires context"
            )
        if self.check_name and not self.instance:
            raise ValueError(
                "public ingress notification policy with check_name scope requires instance"
            )
        if self.check_kind and not (self.context and self.instance):
            raise ValueError(
                "public ingress notification policy with check_kind scope requires lane scope"
            )
        return self

    def matches(self, incident: PublicIngressIncidentRecord) -> bool:
        return (
            (not self.product or self.product == incident.product)
            and (not self.context or self.context == incident.context)
            and (not self.instance or self.instance == incident.instance)
            and (
                not self.check_name
                or canonical_health_check_record_token(self.check_name)
                == canonical_health_check_record_token(incident.check_name)
            )
            and (not self.check_kind or self.check_kind == incident.check_kind)
        )


class PublicIngressNotificationAttemptRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    attempt_id: str
    incident_id: str
    event: PublicIngressIncidentEvent
    policy_id: str
    destination_id: str
    destination_kind: PublicIngressNotificationDestinationKind
    delivery_status: PublicIngressNotificationDeliveryStatus
    attempted_at: str
    observation_id: str
    incident_event_id: str = ""
    external_url: str = ""
    external_id: str = ""
    action: str = ""
    error_message: str = ""

    @model_validator(mode="after")
    def _validate_attempt(self) -> "PublicIngressNotificationAttemptRecord":
        self.attempt_id = _required_text(
            self.attempt_id, "public ingress notification attempt requires attempt_id"
        )
        self.incident_id = _required_text(
            self.incident_id, "public ingress notification attempt requires incident_id"
        )
        self.policy_id = _required_text(
            self.policy_id, "public ingress notification attempt requires policy_id"
        )
        self.destination_id = _required_text(
            self.destination_id, "public ingress notification attempt requires destination_id"
        )
        self.attempted_at = _required_text(
            self.attempted_at, "public ingress notification attempt requires attempted_at"
        )
        self.observation_id = _required_text(
            self.observation_id,
            "public ingress notification attempt requires observation_id",
        )
        self.incident_event_id = self.incident_event_id.strip()
        self.external_url = self.external_url.strip()
        self.external_id = self.external_id.strip()
        self.action = self.action.strip()
        self.error_message = self.error_message.strip()
        if self.delivery_status == "failed" and not self.error_message:
            raise ValueError("failed public ingress notification attempt requires error_message")
        if self.delivery_status == "delivered" and not self.action:
            raise ValueError("delivered public ingress notification attempt requires action")
        return self


def build_public_ingress_observation_id(
    *, product: str, context: str, instance: str, observed_at: str, check_name: str = ""
) -> str:
    check_token = _check_record_token(check_name)
    return "-".join(
        _record_token(value)
        for value in ("public-ingress", product, context, instance, check_token, observed_at)
        if _record_token(value)
    )


def build_public_ingress_reconciliation_observation_id(
    *, product: str, context: str, instance: str, observed_at: str, check_name: str = ""
) -> str:
    check_token = _check_record_token(check_name)
    return "-".join(
        _record_token(value)
        for value in (
            "public-ingress-reconciliation",
            product,
            context,
            instance,
            check_token,
            observed_at,
        )
        if _record_token(value)
    )


def build_public_ingress_incident_id(
    *, product: str, context: str, instance: str, opened_at: str, check_name: str = ""
) -> str:
    check_token = _check_record_token(check_name)
    return "-".join(
        _record_token(value)
        for value in (
            "public-ingress-incident",
            product,
            context,
            instance,
            check_token,
            opened_at,
        )
        if _record_token(value)
    )


def build_public_ingress_lane_incident_id(
    *, product: str, context: str, instance: str, check_name: str = ""
) -> str:
    check_token = _check_record_token(check_name)
    return "-".join(
        _record_token(value)
        for value in ("public-ingress-incident", product, context, instance, check_token)
        if _record_token(value)
    )


def build_public_ingress_notification_attempt_id(
    *,
    incident_id: str,
    event: str,
    policy_id: str,
    destination_id: str,
    incident_event_id: str = "",
    observation_id: str = "",
) -> str:
    event_identity = incident_event_id.strip() or observation_id.strip()
    if not event_identity:
        raise ValueError("public ingress notification attempt requires event identity")
    digest = hashlib.sha256(
        json.dumps(
            {
                "incident_id": incident_id.strip(),
                "event": event.strip(),
                "policy_id": policy_id.strip(),
                "destination_id": destination_id.strip(),
                "incident_event_id": event_identity,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:16]
    return "-".join(
        _record_token(value)
        for value in (
            "public-ingress-notification",
            incident_id,
            event,
            policy_id,
            destination_id,
            digest,
        )
        if _record_token(value)
    )


def build_public_ingress_incident_event_id(
    *,
    incident_id: str,
    event: PublicIngressIncidentEventKind,
    material_fingerprint_sha256: str,
    previous_event_id: str = "",
    policy_id: str = "",
    reminder_window_index: int | None = None,
    reminder_window_started_at: str = "",
) -> str:
    digest = hashlib.sha256(
        json.dumps(
            {
                "incident_id": incident_id.strip(),
                "event": event,
                "material_fingerprint_sha256": material_fingerprint_sha256.strip().lower(),
                "previous_event_id": previous_event_id.strip(),
                "policy_id": policy_id.strip(),
                "reminder_window_index": reminder_window_index,
                "reminder_window_started_at": reminder_window_started_at.strip(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:20]
    return "-".join(
        _record_token(value)
        for value in ("public-ingress-event", incident_id, event, digest)
        if _record_token(value)
    )


def build_public_ingress_reminder_state_id(*, incident_id: str, policy_id: str) -> str:
    digest = hashlib.sha256(
        json.dumps(
            {"incident_id": incident_id.strip(), "policy_id": policy_id.strip()},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:16]
    return "-".join(
        _record_token(value)
        for value in ("public-ingress-reminder", incident_id, policy_id, digest)
        if _record_token(value)
    )


def public_ingress_material_fingerprint_sha256(
    fingerprint: PublicIngressIncidentMaterialFingerprint,
) -> str:
    return hashlib.sha256(
        json.dumps(
            fingerprint.model_dump(mode="json", exclude_none=True),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def public_ingress_incident_record_sha256(record: PublicIngressIncidentRecord) -> str:
    return hashlib.sha256(
        json.dumps(
            record.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def public_ingress_failure_layer(code: PublicIngressFailureCode) -> PublicIngressFailureLayer:
    if code == "dns_failure":
        return "dns"
    if code in {"connection_timeout", "private_url"}:
        return "network"
    if code in {"redirect_loop", "self_redirect"}:
        return "redirect"
    if code in {"health_status_error", "http_error"}:
        return "http"
    if code.startswith("tls_"):
        return "tls"
    if code == "wrong_runtime_identity":
        return "runtime_identity"
    if code in {
        "invalid_url",
        "private_endpoint_disabled",
        "private_endpoint_mismatch",
        "private_endpoint_not_found",
        "monitoring_intent_changed",
    }:
        return "configuration"
    if code == "provider_check_unavailable":
        return "provider"
    return "unknown"


def public_ingress_incident_severity(
    code: PublicIngressFailureCode,
) -> PublicIngressIncidentSeverity:
    if code == "tls_expiring":
        return "warning"
    return "critical"


def build_public_ingress_material_fingerprint(
    observation: PublicIngressObservationRecord,
    *,
    private_endpoint_key: str = "",
    private_endpoint_sha256: str = "",
    provider: str = "",
    provider_check: str = "",
) -> PublicIngressIncidentMaterialFingerprint:
    if observation.status != "fail" or observation.failure_code is None:
        raise ValueError("material incident fingerprint requires a failing observation")
    failing_targets = tuple(target for target in observation.targets if target.status == "fail")
    if not failing_targets:
        raise ValueError("material incident fingerprint requires failing targets")
    affected_targets = tuple(target.target for target in failing_targets)
    route_authority: PublicIngressIncidentRouteAuthority
    tls_status: PublicIngressTlsStatus | Literal[""] = ""
    if observation.check_kind == "private_http":
        endpoint_key = private_endpoint_key.strip()
        if not endpoint_key:
            endpoint_key = _private_endpoint_key_from_targets(failing_targets)
        route_authority = PublicIngressIncidentRouteAuthority(
            kind="private_endpoint",
            target_keys=(f"private-endpoint:{endpoint_key}",),
            private_endpoint_key=endpoint_key,
            private_endpoint_sha256=private_endpoint_sha256.strip().lower()
            or _private_target_digest(failing_targets),
        )
    elif observation.check_kind == "provider":
        normalized_provider = provider.strip().lower()
        normalized_provider_check = provider_check.strip().lower()
        if not (normalized_provider and normalized_provider_check):
            normalized_provider, normalized_provider_check = _provider_target_parts(failing_targets)
        route_authority = PublicIngressIncidentRouteAuthority(
            kind="provider",
            target_keys=(f"provider:{normalized_provider}:{normalized_provider_check}",),
            provider=normalized_provider,
            provider_check=normalized_provider_check,
        )
    elif observation.check_kind == "tls":
        tls_target = failing_targets[0]
        if tls_target.tls is None:
            raise ValueError("TLS material incident fingerprint requires TLS evidence")
        recorded = tls_target.tls.recorded
        tls_status = tls_target.tls.status
        route_authority = PublicIngressIncidentRouteAuthority(
            kind="route_binding",
            target_keys=(f"tls-domain:{recorded.domain_name}",),
            domain_name=recorded.domain_name,
            route_binding_status=recorded.route_binding_status,
            ingress_provider=recorded.ingress_provider,
            termination_kind=recorded.termination_kind,
            tls_owner=recorded.owner,
        )
    else:
        route_authority = PublicIngressIncidentRouteAuthority(
            kind="public_urls",
            target_keys=tuple(_canonical_material_url(target.url) for target in failing_targets),
        )
    runtime_mismatch = _runtime_mismatch(observation, failing_targets)
    return PublicIngressIncidentMaterialFingerprint(
        check_kind=observation.check_kind,
        failure_code=observation.failure_code,
        failure_layer=public_ingress_failure_layer(observation.failure_code),
        severity=public_ingress_incident_severity(observation.failure_code),
        affected_targets=affected_targets,
        route_authority=route_authority,
        runtime_mismatch=runtime_mismatch,
        tls_status=tls_status,
    )


def _runtime_mismatch(
    observation: PublicIngressObservationRecord,
    targets: tuple[PublicIngressTargetObservation, ...],
) -> PublicIngressIncidentRuntimeMismatch | None:
    target = next(
        (candidate for candidate in targets if candidate.failure_code == "wrong_runtime_identity"),
        None,
    )
    if target is None:
        return None
    status = target.runtime_identity_status
    if status not in {"mismatch", "missing", "malformed", "unverifiable"}:
        status = "unverifiable"
    mismatched_fields: tuple[str, ...] = ()
    if status == "mismatch":
        mismatched_fields = tuple(
            field_name.strip()
            for field_name in target.runtime_identity_detail.partition(":")[2].split(",")
            if field_name.strip()
        )
    return PublicIngressIncidentRuntimeMismatch(
        status=status,
        mismatched_fields=mismatched_fields,
        expected_runtime_identity=_material_runtime_identity(observation.expected_runtime_identity),
        observed_runtime_identity=_material_runtime_identity(target.observed_runtime_identity),
    )


def _material_runtime_identity(
    identity: RuntimeIdentity | None,
) -> PublicIngressIncidentRuntimeIdentity | None:
    if identity is None:
        return None
    return PublicIngressIncidentRuntimeIdentity(
        product=identity.product,
        context=identity.context,
        instance=identity.instance,
        environment_kind=identity.environment_kind,
        deployment_record_id=identity.deployment_record_id,
        artifact_id=identity.artifact_id,
        source_git_ref=identity.source_git_ref,
    )


def _canonical_material_url(url: str) -> str:
    normalized_url = url.strip()
    parsed = urlsplit(normalized_url)
    hostname = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError:
        port = None
    if not hostname or parsed.scheme.lower() not in {"http", "https"}:
        digest = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()
        return f"invalid-url-sha256:{digest}"
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    authority = hostname if port in {None, default_port} else f"{hostname}:{port}"
    path = parsed.path or "/"
    return urlunsplit((parsed.scheme.lower(), authority, path, "", ""))


def _private_endpoint_key_from_targets(
    targets: tuple[PublicIngressTargetObservation, ...],
) -> str:
    for target in targets:
        parsed = urlsplit(target.url)
        if parsed.scheme == "private-endpoint" and parsed.netloc:
            return parsed.netloc
    return "legacy-private-endpoint"


def _private_target_digest(targets: tuple[PublicIngressTargetObservation, ...]) -> str:
    return hashlib.sha256(
        json.dumps(
            sorted(_canonical_material_url(target.url) for target in targets),
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _provider_target_parts(
    targets: tuple[PublicIngressTargetObservation, ...],
) -> tuple[str, str]:
    parsed = urlsplit(targets[0].url)
    provider = parsed.netloc.strip().lower()
    provider_check = parsed.path.strip("/").lower()
    if not (provider and provider_check):
        raise ValueError("provider material incident fingerprint requires provider identity")
    return provider, provider_check


def build_public_ingress_tls_check_name(domain_name: str) -> str:
    normalized_domain = (
        _required_text(domain_name, "public ingress TLS check name requires domain_name")
        .lower()
        .rstrip(".")
    )
    digest = hashlib.sha256(normalized_domain.encode("utf-8")).hexdigest()[:12]
    return f"tls-{normalized_domain}-{digest}"


def _record_token(value: str) -> str:
    return health_check_record_token(value)


def _check_record_token(check_name: str) -> str:
    # Preserve legacy public-ingress observation and incident ids for the default
    # public HTTP check while still separating every explicitly named non-default check.
    return canonical_health_check_record_token(check_name)


def _required_text(value: str, message: str) -> str:
    normalized_value = value.strip()
    if not normalized_value:
        raise ValueError(message)
    return normalized_value


def _normalize_optional_text(value: str) -> str:
    return value.strip()


PublicIngressObservationRecord.model_rebuild()
