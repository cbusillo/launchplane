from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any, Literal, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import Request, urlopen
import hashlib
import json
import os
import smtplib
import socket
import ssl

from pydantic import BaseModel, ConfigDict, Field

from control_plane import secrets as control_plane_secrets
from control_plane.contracts.lane_summary import LaunchplaneLaneSummary
from control_plane.contracts.outbox_delivery import (
    OutboxDeliveryRecord,
    build_outbox_dedupe_key,
    build_outbox_delivery_id,
    retry_outbox_delivery,
)
from control_plane.contracts.product_health_monitoring_migration import (
    canonical_health_check_record_token,
)
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneHealthCheck,
    ProductLaneMonitoringIntent,
    ProductLaneProfile,
    product_lane_monitoring_incident_eligible,
    product_lane_monitoring_probe_effective,
    product_profile_record_sha256,
)
from control_plane.contracts.private_health_endpoint_record import (
    PrivateHealthEndpointRecord,
    private_health_endpoint_record_sha256,
)
from control_plane.contracts.public_ingress_monitoring import (
    PUBLIC_TLS_EXPIRING_DAYS,
    PUBLIC_TLS_STALE_AFTER_SECONDS,
    PublicIngressCheckKind,
    PublicIngressFailureCode,
    PublicIngressIncidentEvent,
    PublicIngressIncidentEventRecord,
    PublicIngressIncidentMaterialFingerprint,
    PublicIngressIncidentRecord,
    PublicIngressIncidentReminderStateRecord,
    PublicIngressIncidentRouteAuthority,
    PublicIngressIncidentRuntimeMismatch,
    PublicIngressNotificationAttemptRecord,
    PublicIngressNotificationDeliveryStatus,
    PublicIngressNotificationDestination,
    PublicIngressNotificationDestinationKind,
    PublicIngressNotificationPolicyRecord,
    PublicIngressObservationRecord,
    PublicIngressObservationStatus,
    PublicIngressTargetKind,
    PublicIngressTlsObservation,
    PublicIngressTlsProbeEvidence,
    PublicIngressTlsRecordedEvidence,
    PublicIngressTargetObservation,
    build_public_ingress_incident_event_id,
    build_public_ingress_incident_id,
    build_public_ingress_material_fingerprint,
    build_public_ingress_notification_attempt_id,
    build_public_ingress_observation_id,
    build_public_ingress_reconciliation_observation_id,
    build_public_ingress_reminder_state_id,
    build_public_ingress_tls_check_name,
    public_ingress_material_fingerprint_sha256,
    public_ingress_failure_layer,
    public_ingress_incident_record_sha256,
    public_ingress_incident_severity,
)
from control_plane.contracts.route_binding_record import EnvironmentRouteBindingRecord
from control_plane.contracts.route_binding_record import RouteBindingDomain
from control_plane.contracts.route_binding_record import RouteBindingTerminationKind
from control_plane.contracts.route_binding_record import RouteBindingTlsOwner
from control_plane.contracts.route_binding_record import route_binding_record_sha256
from control_plane.contracts.runtime_identity import (
    RuntimeIdentity,
    RuntimeIdentityStatus,
    health_payload_runtime_identity_status,
)
from control_plane.drivers.registry import read_driver_descriptor
from control_plane.notifications import (
    post_discord_webhook,
    public_discord_url_error,
    public_url_error,
)
from control_plane.outbound_http import PublicHttpDestinationError
from control_plane.outbound_http import PublicTlsProbeResult
from control_plane.outbound_http import probe_public_tls
from control_plane.outbound_http import request_private_http
from control_plane.outbound_http import request_public_http
from control_plane.workflows.odoo_verification import (
    default_odoo_health_url,
    is_legacy_derived_odoo_health_url,
)
from control_plane.workflows.ship import utc_now_timestamp


MAX_REDIRECTS = 10
USER_AGENT = "Launchplane public-ingress-monitor/1.0"
PUBLIC_INGRESS_GITHUB_TOKEN_ENV_KEY = "LAUNCHPLANE_PUBLIC_INGRESS_GITHUB_TOKEN"
MISSING_AUTHORITY_SHA256 = "missing"


PublicIngressRouteBindingSourceKind = Literal["operator", "backfill", "service"]


class PublicIngressMonitorStore(Protocol):
    def list_product_profile_records(
        self, *, driver_id: str = ""
    ) -> tuple[LaunchplaneProductProfileRecord, ...]: ...

    def list_public_ingress_observation_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        check_name: str = "",
        check_kind: str = "",
        limit: int | None = None,
    ) -> tuple[PublicIngressObservationRecord, ...]: ...

    def write_public_ingress_observation_record(
        self, record: PublicIngressObservationRecord
    ) -> object: ...

    def list_public_ingress_incident_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        check_name: str = "",
        check_kind: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[PublicIngressIncidentRecord, ...]: ...

    def write_public_ingress_incident_record(
        self, record: PublicIngressIncidentRecord
    ) -> object: ...

    def list_public_ingress_notification_policy_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[PublicIngressNotificationPolicyRecord, ...]: ...

    def list_public_ingress_notification_attempt_records(
        self,
        *,
        incident_id: str = "",
        event: str = "",
        destination_kind: str = "",
        limit: int | None = None,
    ) -> tuple[PublicIngressNotificationAttemptRecord, ...]: ...

    def write_public_ingress_notification_attempt_record(
        self, record: PublicIngressNotificationAttemptRecord
    ) -> object: ...

    def list_public_ingress_incident_event_records(
        self,
        *,
        incident_id: str = "",
        event: str = "",
        limit: int | None = None,
    ) -> tuple[PublicIngressIncidentEventRecord, ...]: ...

    def write_public_ingress_incident_event_record(
        self, record: PublicIngressIncidentEventRecord
    ) -> object: ...

    def list_public_ingress_incident_reminder_state_records(
        self,
        *,
        incident_id: str = "",
        policy_id: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[PublicIngressIncidentReminderStateRecord, ...]: ...

    def write_public_ingress_incident_reminder_state_record(
        self, record: PublicIngressIncidentReminderStateRecord
    ) -> object: ...

    def read_private_health_endpoint_record(
        self, endpoint_key: str
    ) -> PrivateHealthEndpointRecord: ...

    def list_route_binding_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[EnvironmentRouteBindingRecord, ...]: ...


@dataclass(frozen=True)
class PublicIngressMonitorTarget:
    product: str
    repository: str
    driver_id: str
    context: str
    instance: str
    base_url: str
    health_url: str
    check_name: str
    check_kind: PublicIngressCheckKind
    profile_sha256: str
    monitoring_intent: ProductLaneMonitoringIntent
    incident_eligible: bool
    expected_runtime_identity: RuntimeIdentity | None
    require_runtime_identity: bool
    provider: str = ""
    provider_check: str = ""
    private_endpoint_key: str = ""
    private_endpoint_sha256: str = ""
    private_endpoint_material_sha256: str = ""
    route_binding_sha256: str = ""
    recovery_observation_threshold: int = 1
    resolution_failure_code: PublicIngressFailureCode | None = None
    resolution_failure_summary: str = ""
    tls_domain_name: str = ""
    tls_domain_role: Literal["primary", "alias"] = "primary"
    tls_owner: RouteBindingTlsOwner = "none"
    tls_ingress_provider: str = ""
    tls_termination_kind: RouteBindingTerminationKind = "none"
    tls_source_kind: PublicIngressRouteBindingSourceKind = "service"
    tls_source_label: str = ""
    tls_source_record_ids: tuple[str, ...] = ()
    tls_source_versions: dict[str, str] | None = None
    tls_refreshed_at: str = ""
    tls_recorded_at: str = ""
    tls_freshness_status: Literal["verified", "recorded", "stale", "missing", "unsupported"] = (
        "recorded"
    )
    tls_stale_after: str = ""
    tls_provider_evidence: dict[str, str] | None = None


@dataclass(frozen=True)
class HttpObservation:
    status_code: int
    final_url: str
    redirect_count: int
    payload: object = None


@dataclass(frozen=True)
class PublicIngressNotificationDelivery:
    delivery_status: PublicIngressNotificationDeliveryStatus
    action: str = ""
    external_url: str = ""
    external_id: str = ""
    error_message: str = ""


class PublicIngressNotificationDrivers(Protocol):
    def send(
        self,
        *,
        destination: PublicIngressNotificationDestination,
        event: PublicIngressIncidentEvent,
        incident: PublicIngressIncidentRecord,
        observation: PublicIngressObservationRecord,
        previous_attempts: tuple[PublicIngressNotificationAttemptRecord, ...] = (),
    ) -> PublicIngressNotificationDelivery: ...


class PublicIngressMonitorResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checked_at: str
    target_count: int = Field(ge=0)
    pass_count: int = Field(default=0, ge=0)
    fail_count: int = Field(default=0, ge=0)
    skipped_count: int = Field(default=0, ge=0)
    reconciliation_count: int = Field(default=0, ge=0)
    authority_changed_count: int = Field(default=0, ge=0)
    open_incident_count: int = Field(default=0, ge=0)
    resolved_incident_count: int = Field(default=0, ge=0)
    delivery_attempt_count: int = Field(default=0, ge=0)
    records: tuple[PublicIngressObservationRecord, ...] = ()
    incidents: tuple[PublicIngressIncidentRecord, ...] = ()
    incident_events: tuple[PublicIngressIncidentEventRecord, ...] = ()
    reminder_states: tuple[PublicIngressIncidentReminderStateRecord, ...] = ()
    delivery_attempts: tuple[PublicIngressNotificationAttemptRecord, ...] = ()


@dataclass(frozen=True)
class _StoredMonitorTransition:
    observation: PublicIngressObservationRecord
    incidents: tuple[PublicIngressIncidentRecord, ...]
    incident_events: tuple[PublicIngressIncidentEventRecord, ...]
    reminder_states: tuple[PublicIngressIncidentReminderStateRecord, ...]
    delivery_attempts: tuple[PublicIngressNotificationAttemptRecord, ...]
    authority_changed: bool = False


@dataclass(frozen=True)
class _PlannedIncidentTransition:
    observation: PublicIngressObservationRecord
    incidents: tuple[PublicIngressIncidentRecord, ...]
    incident_events: tuple[PublicIngressIncidentEventRecord, ...]
    reminder_states: tuple[PublicIngressIncidentReminderStateRecord, ...]
    expected_open_incident_id: str = ""
    expected_open_incident_state_version: int = 0
    expected_open_incident_sha256: str = ""


HttpGet = Callable[[str, int], HttpObservation]
TlsGet = Callable[[str, int], PublicTlsProbeResult]


def discover_public_ingress_monitor_targets(
    record_store: PublicIngressMonitorStore,
    *,
    profiles: tuple[LaunchplaneProductProfileRecord, ...] | None = None,
) -> tuple[PublicIngressMonitorTarget, ...]:
    targets: list[PublicIngressMonitorTarget] = []
    product_profiles = profiles or record_store.list_product_profile_records()
    for profile in product_profiles:
        for lane in profile.lanes:
            for check in lane.health_monitoring.checks:
                if not check.enabled or not product_lane_monitoring_probe_effective(
                    monitoring_intent=lane.health_monitoring.monitoring_intent,
                    check_kind=check.kind,
                ):
                    continue
                if check.kind == "public_http" and not _profile_uses_generic_web(profile):
                    continue
                target = _health_check_monitor_target(
                    profile=profile,
                    lane=lane,
                    check=check,
                    record_store=record_store,
                )
                if target is None:
                    continue
                targets.append(target)
            for tls_target in _tls_monitor_targets(
                record_store=record_store,
                profile=profile,
                lane=lane,
            ):
                targets.append(tls_target)
    return tuple(targets)


def _health_check_monitor_target(
    *,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    check: ProductLaneHealthCheck,
    record_store: object,
) -> PublicIngressMonitorTarget | None:
    base_url = ""
    health_url = ""
    resolution_failure_code: PublicIngressFailureCode | None = None
    resolution_failure_summary = ""
    private_endpoint_sha256 = ""
    private_endpoint_material_sha256 = ""
    if check.kind == "public_http":
        base_url = lane.base_url.strip().rstrip("/")
        health_url = check.url.strip() or _monitor_health_url(
            profile=profile, lane=lane, base_url=base_url
        )
        if not (base_url or health_url):
            health_url = f"invalid-health-check://{canonical_health_check_record_token(check.name) or 'public-ingress'}"
            resolution_failure_code = "invalid_url"
            resolution_failure_summary = (
                "Enabled public HTTP health monitoring has no lane-owned URL."
            )
    elif check.kind == "private_http":
        if check.private_endpoint_key:
            resolution = _resolved_private_health_url(
                record_store=record_store,
                profile=profile,
                lane=lane,
                endpoint_key=check.private_endpoint_key,
            )
            health_url = resolution.url
            resolution_failure_code = resolution.failure_code
            resolution_failure_summary = resolution.summary
            private_endpoint_sha256 = resolution.authority_sha256
            private_endpoint_material_sha256 = resolution.material_authority_sha256
        if not health_url and not resolution_failure_code:
            return None
    else:
        if not (check.provider.strip() and check.provider_check.strip()):
            return None
    return PublicIngressMonitorTarget(
        product=profile.product,
        repository=profile.repository,
        driver_id=profile.driver_id,
        context=lane.context,
        instance=lane.instance,
        base_url=base_url,
        health_url=health_url,
        check_name=check.name,
        check_kind=check.kind,
        profile_sha256=product_profile_record_sha256(profile),
        monitoring_intent=lane.health_monitoring.monitoring_intent,
        incident_eligible=product_lane_monitoring_incident_eligible(
            monitoring_intent=lane.health_monitoring.monitoring_intent,
            check_kind=check.kind,
        ),
        expected_runtime_identity=_expected_runtime_identity(
            record_store=record_store,
            lane=lane,
        ),
        require_runtime_identity=check.require_runtime_identity,
        recovery_observation_threshold=check.recovery_observation_threshold,
        provider=check.provider,
        provider_check=check.provider_check,
        private_endpoint_key=check.private_endpoint_key,
        private_endpoint_sha256=private_endpoint_sha256,
        private_endpoint_material_sha256=private_endpoint_material_sha256,
        resolution_failure_code=resolution_failure_code,
        resolution_failure_summary=resolution_failure_summary,
    )


@dataclass(frozen=True)
class PrivateHealthEndpointResolution:
    url: str = ""
    failure_code: PublicIngressFailureCode | None = None
    summary: str = ""
    authority_sha256: str = ""
    material_authority_sha256: str = ""


def _resolved_private_health_url(
    *,
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    endpoint_key: str,
) -> PrivateHealthEndpointResolution:
    read_endpoint = getattr(record_store, "read_private_health_endpoint_record", None)
    if not callable(read_endpoint):
        return PrivateHealthEndpointResolution(
            failure_code="private_endpoint_not_found",
            summary="Private health endpoint storage is not available.",
        )
    try:
        endpoint = read_endpoint(endpoint_key)
    except (FileNotFoundError, KeyError, LookupError):
        return PrivateHealthEndpointResolution(
            failure_code="private_endpoint_not_found",
            summary=f"Private health endpoint {endpoint_key!r} was not found.",
            authority_sha256=MISSING_AUTHORITY_SHA256,
        )
    if not isinstance(endpoint, PrivateHealthEndpointRecord):
        endpoint = PrivateHealthEndpointRecord.model_validate(endpoint)
    authority_sha256 = private_health_endpoint_record_sha256(endpoint)
    material_authority_sha256 = _private_health_endpoint_material_sha256(endpoint)
    if endpoint.status != "active":
        return PrivateHealthEndpointResolution(
            failure_code="private_endpoint_disabled",
            summary=f"Private health endpoint {endpoint_key!r} is {endpoint.status}.",
            authority_sha256=authority_sha256,
            material_authority_sha256=material_authority_sha256,
        )
    if endpoint.product != profile.product:
        return PrivateHealthEndpointResolution(
            failure_code="private_endpoint_mismatch",
            summary=f"Private health endpoint {endpoint_key!r} belongs to another product.",
            authority_sha256=authority_sha256,
            material_authority_sha256=material_authority_sha256,
        )
    if endpoint.context != lane.context or endpoint.instance != lane.instance:
        return PrivateHealthEndpointResolution(
            failure_code="private_endpoint_mismatch",
            summary=f"Private health endpoint {endpoint_key!r} belongs to another lane.",
            authority_sha256=authority_sha256,
            material_authority_sha256=material_authority_sha256,
        )
    return PrivateHealthEndpointResolution(
        url=endpoint.url,
        authority_sha256=authority_sha256,
        material_authority_sha256=material_authority_sha256,
    )


def _private_health_endpoint_material_sha256(endpoint: PrivateHealthEndpointRecord) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "endpoint_key": endpoint.endpoint_key,
                "product": endpoint.product,
                "context": endpoint.context,
                "instance": endpoint.instance,
                "url": endpoint.url,
                "status": endpoint.status,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def run_public_ingress_monitor_once(
    *,
    record_store: PublicIngressMonitorStore,
    checked_at: str = "",
    timeout_seconds: int = 10,
    notify: bool = True,
    http_get: HttpGet | None = None,
    private_http_get: HttpGet | None = None,
    tls_get: TlsGet | None = None,
    notification_drivers: PublicIngressNotificationDrivers | None = None,
) -> PublicIngressMonitorResult:
    observed_at = checked_at.strip() or utc_now_timestamp()
    public_get = http_get or fetch_public_ingress_url
    private_get = private_http_get or fetch_private_health_url
    tls_probe = tls_get or fetch_public_tls
    profiles = record_store.list_product_profile_records()
    targets = discover_public_ingress_monitor_targets(record_store, profiles=profiles)
    eligible_target_keys = {
        _monitor_target_key(target) for target in targets if target.incident_eligible
    }
    records: list[PublicIngressObservationRecord] = []
    incidents: list[PublicIngressIncidentRecord] = []
    incident_events: list[PublicIngressIncidentEventRecord] = []
    reminder_states: list[PublicIngressIncidentReminderStateRecord] = []
    delivery_attempts: list[PublicIngressNotificationAttemptRecord] = []
    authority_changed_count = 0
    for target in targets:
        record = check_public_ingress_target(
            target=target,
            checked_at=observed_at,
            timeout_seconds=timeout_seconds,
            http_get=(private_get if target.check_kind == "private_http" else public_get),
            tls_get=tls_probe,
        )
        stored_transition = _store_monitor_observation(
            record_store=record_store,
            record=record,
            incident_eligible=target.incident_eligible,
            expected_profile_sha256=target.profile_sha256,
            expected_private_endpoint_key=target.private_endpoint_key,
            expected_private_endpoint_sha256=target.private_endpoint_sha256,
            expected_route_binding_sha256=target.route_binding_sha256,
            recovery_observation_threshold=target.recovery_observation_threshold,
            notify=notify,
            notification_drivers=notification_drivers,
        )
        records.append(stored_transition.observation)
        incidents.extend(stored_transition.incidents)
        incident_events.extend(stored_transition.incident_events)
        reminder_states.extend(stored_transition.reminder_states)
        delivery_attempts.extend(stored_transition.delivery_attempts)
        authority_changed_count += int(stored_transition.authority_changed)
    for record, profile_sha256 in _monitoring_reconciliation_observations(
        record_store=record_store,
        profiles=profiles,
        eligible_target_keys=eligible_target_keys,
        observed_at=observed_at,
    ):
        stored_transition = _store_monitor_observation(
            record_store=record_store,
            record=record,
            incident_eligible=False,
            expected_profile_sha256=profile_sha256,
            recovery_observation_threshold=1,
            notify=notify,
            notification_drivers=notification_drivers,
        )
        records.append(stored_transition.observation)
        incidents.extend(stored_transition.incidents)
        incident_events.extend(stored_transition.incident_events)
        reminder_states.extend(stored_transition.reminder_states)
        delivery_attempts.extend(stored_transition.delivery_attempts)
        authority_changed_count += int(stored_transition.authority_changed)
    return PublicIngressMonitorResult(
        checked_at=observed_at,
        target_count=len(targets),
        pass_count=sum(
            1 for record in records if record.purpose == "probe" and record.status == "pass"
        ),
        fail_count=sum(
            1 for record in records if record.purpose == "probe" and record.status == "fail"
        ),
        skipped_count=sum(
            1 for record in records if record.purpose == "probe" and record.status == "skipped"
        ),
        reconciliation_count=sum(1 for record in records if record.purpose == "reconciliation"),
        authority_changed_count=authority_changed_count,
        open_incident_count=sum(1 for incident in incidents if incident.status == "open"),
        resolved_incident_count=sum(1 for incident in incidents if incident.status == "resolved"),
        delivery_attempt_count=len(delivery_attempts),
        records=tuple(records),
        incidents=tuple(incidents),
        incident_events=tuple(incident_events),
        reminder_states=tuple(reminder_states),
        delivery_attempts=tuple(delivery_attempts),
    )


def _store_monitor_observation(
    *,
    record_store: PublicIngressMonitorStore,
    record: PublicIngressObservationRecord,
    incident_eligible: bool,
    expected_profile_sha256: str,
    expected_private_endpoint_key: str = "",
    expected_private_endpoint_sha256: str = "",
    expected_route_binding_sha256: str = "",
    recovery_observation_threshold: int,
    notify: bool,
    notification_drivers: PublicIngressNotificationDrivers | None,
) -> _StoredMonitorTransition:
    transition_writer = getattr(record_store, "write_public_ingress_transition_with_outbox", None)
    plan: _PlannedIncidentTransition | None = None
    outbox_deliveries: tuple[OutboxDeliveryRecord, ...] = ()
    for _attempt in range(3):
        plan = _plan_public_ingress_incident_transition(
            record_store=record_store,
            record=record,
            incident_eligible=incident_eligible,
            recovery_observation_threshold=recovery_observation_threshold,
        )
        outbox_deliveries = (
            _public_ingress_notification_outbox_deliveries(
                record_store=record_store,
                incident_records=plan.incidents,
                incident_events=plan.incident_events,
                observation=plan.observation,
            )
            if notify
            else ()
        )
        if not callable(transition_writer):
            record_store.write_public_ingress_observation_record(plan.observation)
            for incident in plan.incidents:
                record_store.write_public_ingress_incident_record(incident)
            for incident_event in plan.incident_events:
                record_store.write_public_ingress_incident_event_record(incident_event)
            for reminder_state in plan.reminder_states:
                record_store.write_public_ingress_incident_reminder_state_record(reminder_state)
            break
        write_result = transition_writer(
            observation=plan.observation,
            incidents=plan.incidents,
            incident_events=plan.incident_events,
            reminder_states=plan.reminder_states,
            outbox_deliveries=outbox_deliveries,
            expected_open_incident_id=plan.expected_open_incident_id,
            expected_open_incident_state_version=plan.expected_open_incident_state_version,
            expected_open_incident_sha256=plan.expected_open_incident_sha256,
            expected_profile_sha256=expected_profile_sha256,
            expected_private_endpoint_key=expected_private_endpoint_key,
            expected_private_endpoint_sha256=expected_private_endpoint_sha256,
            expected_route_binding_sha256=expected_route_binding_sha256,
        )
        write_status = getattr(write_result, "status", "written")
        if write_status == "incident_changed":
            continue
        if write_status == "authority_changed":
            return _StoredMonitorTransition(
                observation=record.model_copy(update={"incident_id": "", "incident_event_id": ""}),
                incidents=(),
                incident_events=(),
                reminder_states=(),
                delivery_attempts=(),
                authority_changed=True,
            )
        break
    else:
        raise RuntimeError("public ingress incident changed during three transition attempts")
    if plan is None:
        raise RuntimeError("public ingress transition planning did not produce a plan")
    delivery_attempts: list[PublicIngressNotificationAttemptRecord] = []
    incidents_by_id = {incident.incident_id: incident for incident in plan.incidents}
    for incident_event in plan.incident_events:
        if not incident_event.delivery_eligible:
            continue
        incident = incidents_by_id[incident_event.incident_id]
        if notify and notification_drivers is not None:
            direct_destination_kinds: (
                tuple[PublicIngressNotificationDestinationKind, ...] | None
            ) = None
            if callable(transition_writer) and any(
                _outbox_incident_event_id(delivery) == incident_event.event_id
                for delivery in outbox_deliveries
            ):
                direct_destination_kinds = ("email", "discord")
            delivery_attempts.extend(
                deliver_public_ingress_incident_notifications(
                    record_store=record_store,
                    incident_event=incident_event,
                    incident=incident,
                    observation=plan.observation,
                    drivers=notification_drivers,
                    destination_kinds=direct_destination_kinds,
                )
            )
    return _StoredMonitorTransition(
        observation=plan.observation,
        incidents=plan.incidents,
        incident_events=plan.incident_events,
        reminder_states=plan.reminder_states,
        delivery_attempts=tuple(delivery_attempts),
    )


def _monitor_target_key(
    target: PublicIngressMonitorTarget,
) -> tuple[str, str, str, str, str]:
    return (
        target.product,
        target.context,
        target.instance,
        canonical_health_check_record_token(target.check_name),
        target.check_kind,
    )


def _incident_target_key(
    incident: PublicIngressIncidentRecord,
) -> tuple[str, str, str, str, str]:
    return (
        incident.product,
        incident.context,
        incident.instance,
        canonical_health_check_record_token(incident.check_name),
        incident.check_kind,
    )


def _monitoring_reconciliation_observations(
    *,
    record_store: PublicIngressMonitorStore,
    profiles: tuple[LaunchplaneProductProfileRecord, ...],
    eligible_target_keys: set[tuple[str, str, str, str, str]],
    observed_at: str,
) -> tuple[tuple[PublicIngressObservationRecord, str], ...]:
    reconciliations: list[tuple[PublicIngressObservationRecord, str]] = []
    for profile in profiles:
        for lane in profile.lanes:
            open_incidents = record_store.list_public_ingress_incident_records(
                product=profile.product,
                context_name=lane.context,
                instance_name=lane.instance,
                status="open",
            )
            for incident in open_incidents:
                target_key = _incident_target_key(incident)
                if target_key in eligible_target_keys:
                    continue
                reconciliations.append(
                    (
                        _monitoring_reconciliation_observation(
                            incident=incident,
                            monitoring_intent=lane.health_monitoring.monitoring_intent,
                            observed_at=observed_at,
                        ),
                        product_profile_record_sha256(profile),
                    )
                )
    return tuple(reconciliations)


def _monitoring_reconciliation_observation(
    *,
    incident: PublicIngressIncidentRecord,
    monitoring_intent: ProductLaneMonitoringIntent,
    observed_at: str,
) -> PublicIngressObservationRecord:
    summary = (
        f"Monitoring configuration changed for {incident.product}/{incident.instance}; "
        f"{incident.check_kind} check {incident.check_name!r} is no longer incident-eligible "
        f"under {monitoring_intent} intent."
    )
    return PublicIngressObservationRecord(
        record_id=build_public_ingress_reconciliation_observation_id(
            product=incident.product,
            context=incident.context,
            instance=incident.instance,
            observed_at=observed_at,
            check_name=incident.check_name,
        ),
        product=incident.product,
        repository=incident.repository,
        driver_id=incident.driver_id,
        context=incident.context,
        instance=incident.instance,
        check_name=incident.check_name,
        check_kind=incident.check_kind,
        purpose="reconciliation",
        monitoring_intent=monitoring_intent,
        observed_at=observed_at,
        status="skipped",
        failure_code="monitoring_intent_changed",
        targets=(
            PublicIngressTargetObservation(
                target="monitoring_intent",
                url=f"monitoring-intent://{monitoring_intent}",
                status="skipped",
                failure_code="monitoring_intent_changed",
                summary=summary,
            ),
        ),
        summary=summary,
    )


def deliver_public_ingress_incident_notifications(
    *,
    record_store: PublicIngressMonitorStore,
    incident_event: PublicIngressIncidentEventRecord,
    incident: PublicIngressIncidentRecord,
    observation: PublicIngressObservationRecord,
    drivers: PublicIngressNotificationDrivers,
    destination_kinds: tuple[PublicIngressNotificationDestinationKind, ...] | None = None,
) -> tuple[PublicIngressNotificationAttemptRecord, ...]:
    attempts: list[PublicIngressNotificationAttemptRecord] = []
    event = _public_ingress_event_value(incident_event.event)
    policies = _matching_notification_policies(record_store=record_store, incident=incident)
    if incident_event.policy_id:
        policies = tuple(
            policy for policy in policies if policy.policy_id == incident_event.policy_id
        )
    for policy in policies:
        for destination in policy.destinations:
            if destination_kinds is not None and destination.kind not in destination_kinds:
                continue
            attempt_id = build_public_ingress_notification_attempt_id(
                incident_id=incident.incident_id,
                event=event,
                policy_id=policy.policy_id,
                destination_id=destination.destination_id,
                incident_event_id=incident_event.event_id,
            )
            existing_attempt = _notification_attempt(
                record_store=record_store,
                attempt_id=attempt_id,
                incident_id=incident.incident_id,
                event=event,
            )
            if existing_attempt is not None:
                attempts.append(existing_attempt)
                continue
            if destination.status != "enabled":
                delivery = PublicIngressNotificationDelivery(
                    delivery_status="skipped",
                    action="destination_disabled",
                )
            else:
                previous_attempts = record_store.list_public_ingress_notification_attempt_records(
                    incident_id=incident.incident_id,
                    destination_kind=destination.kind,
                )
                delivery = drivers.send(
                    destination=destination,
                    event=event,
                    incident=incident,
                    observation=observation,
                    previous_attempts=previous_attempts,
                )
            attempt = PublicIngressNotificationAttemptRecord(
                attempt_id=attempt_id,
                incident_id=incident.incident_id,
                event=event,
                policy_id=policy.policy_id,
                destination_id=destination.destination_id,
                destination_kind=destination.kind,
                delivery_status=delivery.delivery_status,
                attempted_at=observation.observed_at,
                observation_id=observation.record_id,
                incident_event_id=incident_event.event_id,
                external_url=delivery.external_url,
                external_id=delivery.external_id,
                action=delivery.action,
                error_message=delivery.error_message,
            )
            record_store.write_public_ingress_notification_attempt_record(attempt)
            attempts.append(attempt)
    return tuple(attempts)


def _public_ingress_notification_outbox_deliveries(
    *,
    record_store: PublicIngressMonitorStore,
    incident_records: tuple[PublicIngressIncidentRecord, ...],
    incident_events: tuple[PublicIngressIncidentEventRecord, ...],
    observation: PublicIngressObservationRecord,
) -> tuple[OutboxDeliveryRecord, ...]:
    deliveries: list[OutboxDeliveryRecord] = []
    incidents_by_id = {incident.incident_id: incident for incident in incident_records}
    for incident_event in incident_events:
        if not incident_event.delivery_eligible:
            continue
        incident = incidents_by_id[incident_event.incident_id]
        event = _public_ingress_event_value(incident_event.event)
        policies = _matching_notification_policies(record_store=record_store, incident=incident)
        if incident_event.policy_id:
            policies = tuple(
                policy for policy in policies if policy.policy_id == incident_event.policy_id
            )
        for policy in policies:
            for destination in policy.destinations:
                if destination.kind != "github_issue" or destination.status != "enabled":
                    continue
                attempt_id = build_public_ingress_notification_attempt_id(
                    incident_id=incident.incident_id,
                    event=event,
                    policy_id=policy.policy_id,
                    destination_id=destination.destination_id,
                    incident_event_id=incident_event.event_id,
                )
                existing_attempt = _notification_attempt(
                    record_store=record_store,
                    attempt_id=attempt_id,
                    incident_id=incident.incident_id,
                    event=event,
                )
                if existing_attempt is not None:
                    continue
                previous_attempts = record_store.list_public_ingress_notification_attempt_records(
                    incident_id=incident.incident_id,
                    destination_kind="github_issue",
                )
                body = public_ingress_incident_notification_body(
                    event=event,
                    incident=incident,
                    observation=observation,
                    marker=attempt_id,
                )
                dedupe_key = build_outbox_dedupe_key(
                    kind="public_ingress_notification",
                    parts=(attempt_id,),
                )
                created_at = incident_event.occurred_at
                deliveries.append(
                    OutboxDeliveryRecord(
                        delivery_id=build_outbox_delivery_id(
                            kind="public_ingress_notification",
                            dedupe_key=dedupe_key,
                        ),
                        kind="public_ingress_notification",
                        aggregate_type="public_ingress_incident",
                        aggregate_id=incident.incident_id,
                        dedupe_key=dedupe_key,
                        created_at=created_at,
                        updated_at=created_at,
                        next_attempt_at=created_at,
                        payload={
                            "attempt": {
                                "attempt_id": attempt_id,
                                "incident_id": incident.incident_id,
                                "event": event,
                                "policy_id": policy.policy_id,
                                "destination_id": destination.destination_id,
                                "destination_kind": destination.kind,
                                "attempted_at": observation.observed_at,
                                "observation_id": incident_event.observation_id,
                                "incident_event_id": incident_event.event_id,
                            },
                            "destination": _github_notification_destination_payload(destination),
                            "body": body,
                            "existing_issue_url": _latest_external_url(previous_attempts),
                            "marker": attempt_id,
                            "incident_marker": _public_ingress_incident_marker(
                                incident.incident_id
                            ),
                        },
                    )
                )
    return tuple(deliveries)


def _outbox_incident_event_id(delivery: OutboxDeliveryRecord) -> str:
    attempt = delivery.payload.get("attempt")
    if not isinstance(attempt, dict):
        return ""
    value = attempt.get("incident_event_id")
    return value.strip() if isinstance(value, str) else ""


def reconcile_public_ingress_incident(
    *,
    record_store: PublicIngressMonitorStore,
    record: PublicIngressObservationRecord,
    incident_eligible: bool = True,
    write_records: bool = True,
) -> tuple[PublicIngressIncidentRecord, ...]:
    plan = _plan_public_ingress_incident_transition(
        record_store=record_store,
        record=record,
        incident_eligible=incident_eligible,
        recovery_observation_threshold=1,
    )
    if write_records:
        for incident in plan.incidents:
            record_store.write_public_ingress_incident_record(incident)
        for incident_event in plan.incident_events:
            record_store.write_public_ingress_incident_event_record(incident_event)
        for reminder_state in plan.reminder_states:
            record_store.write_public_ingress_incident_reminder_state_record(reminder_state)
    return plan.incidents


def _plan_public_ingress_incident_transition(
    *,
    record_store: PublicIngressMonitorStore,
    record: PublicIngressObservationRecord,
    incident_eligible: bool,
    recovery_observation_threshold: int,
) -> _PlannedIncidentTransition:
    open_incidents = _open_incidents(record_store=record_store, record=record)
    existing = min(
        open_incidents,
        key=lambda incident: (incident.opened_at, incident.incident_id),
        default=None,
    )
    expected_incident_id = existing.incident_id if existing is not None else ""
    expected_state_version = existing.state_version if existing is not None else 0
    expected_incident_sha256 = (
        public_ingress_incident_record_sha256(existing) if existing is not None else ""
    )
    incident_events: list[PublicIngressIncidentEventRecord] = []
    reconciled_duplicates: list[PublicIngressIncidentRecord] = []
    reconciled_reminder_states: list[PublicIngressIncidentReminderStateRecord] = []
    for duplicate in open_incidents:
        if existing is not None and duplicate.incident_id == existing.incident_id:
            continue
        if duplicate.schema_version < 2:
            duplicate, baseline_event = _upgrade_legacy_incident(
                record_store=record_store,
                existing=duplicate,
            )
            incident_events.append(baseline_event)
        resolved_duplicate, resolved_event = _reconcile_duplicate_incident(
            incident=duplicate,
            reconciled_at=record.observed_at,
        )
        reconciled_duplicates.append(resolved_duplicate)
        incident_events.append(resolved_event)
        reconciled_reminder_states.extend(
            _resolved_reminder_states(
                record_store=record_store,
                incident=resolved_duplicate,
                updated_at=record.observed_at,
            )
        )

    def with_reconciled_reminder_states(
        states: tuple[PublicIngressIncidentReminderStateRecord, ...] = (),
    ) -> tuple[PublicIngressIncidentReminderStateRecord, ...]:
        return (*reconciled_reminder_states, *states)

    if existing is not None and existing.schema_version < 2:
        existing, baseline_event = _upgrade_legacy_incident(
            record_store=record_store,
            existing=existing,
        )
        incident_events.append(baseline_event)
    if existing is not None:
        existing = _expire_incident_silence(existing, observed_at=record.observed_at)

    if not incident_eligible and record.purpose == "reconciliation" and existing is not None:
        incident, resolved_event = _resolve_incident(
            incident=existing,
            record=record,
            reason="monitoring_intent_changed",
            summary=(
                "Monitoring intent changed; this check is no longer incident-eligible. "
                f"Previous incident evidence: {existing.summary}"
            ),
        )
        reminder_states = _resolved_reminder_states(
            record_store=record_store,
            incident=incident,
            updated_at=record.observed_at,
        )
        incident_events.append(resolved_event)
        return _PlannedIncidentTransition(
            observation=record.model_copy(
                update={
                    "incident_id": incident.incident_id,
                    "incident_event_id": resolved_event.event_id,
                }
            ),
            incidents=(incident, *reconciled_duplicates),
            incident_events=tuple(incident_events),
            reminder_states=with_reconciled_reminder_states(reminder_states),
            expected_open_incident_id=expected_incident_id,
            expected_open_incident_state_version=expected_state_version,
            expected_open_incident_sha256=expected_incident_sha256,
        )
    if not incident_eligible:
        return _PlannedIncidentTransition(
            observation=record,
            incidents=tuple(reconciled_duplicates),
            incident_events=tuple(incident_events),
            reminder_states=with_reconciled_reminder_states(),
            expected_open_incident_id=expected_incident_id,
            expected_open_incident_state_version=expected_state_version,
            expected_open_incident_sha256=expected_incident_sha256,
        )
    if record.status == "fail":
        fingerprint = _observation_material_fingerprint(record)
        fingerprint_sha256 = public_ingress_material_fingerprint_sha256(fingerprint)
        linked_observation = record.model_copy(
            update={
                "material_fingerprint": fingerprint,
                "material_fingerprint_sha256": fingerprint_sha256,
            }
        )
        if existing is None:
            incident_id = build_public_ingress_incident_id(
                product=record.product,
                context=record.context,
                instance=record.instance,
                check_name=record.check_name,
                opened_at=record.observed_at,
            )
            event_id = build_public_ingress_incident_event_id(
                incident_id=incident_id,
                event="opened",
                material_fingerprint_sha256=fingerprint_sha256,
            )
            incident = PublicIngressIncidentRecord(
                schema_version=2,
                incident_id=incident_id,
                product=record.product,
                repository=record.repository,
                driver_id=record.driver_id,
                context=record.context,
                instance=record.instance,
                check_name=record.check_name,
                check_kind=record.check_kind,
                status="open",
                opened_at=record.observed_at,
                opened_observation_id=record.record_id,
                latest_observation_id=record.record_id,
                latest_observed_at=record.observed_at,
                failure_code=record.failure_code or "unknown_error",
                state_version=1,
                severity=fingerprint.severity,
                material_fingerprint=fingerprint,
                material_fingerprint_sha256=fingerprint_sha256,
                material_fingerprint_complete=True,
                latest_material_event_id=event_id,
                latest_material_event="opened",
                latest_material_event_at=record.observed_at,
                recovery_observation_threshold=recovery_observation_threshold,
                summary=record.summary,
            )
            incident_event = PublicIngressIncidentEventRecord(
                event_id=event_id,
                incident_id=incident_id,
                event="opened",
                reason="incident_opened",
                occurred_at=record.observed_at,
                observation_id=record.record_id,
                material_fingerprint_sha256=fingerprint_sha256,
                severity=fingerprint.severity,
                summary=record.summary,
            )
            reminder_states = _material_event_reminder_states(
                record_store=record_store,
                incident=incident,
                incident_event=incident_event,
            )
            return _PlannedIncidentTransition(
                observation=linked_observation.model_copy(
                    update={
                        "incident_id": incident_id,
                        "incident_event_id": event_id,
                    }
                ),
                incidents=(incident, *reconciled_duplicates),
                incident_events=(incident_event,),
                reminder_states=with_reconciled_reminder_states(reminder_states),
                expected_open_incident_id="",
                expected_open_incident_state_version=0,
                expected_open_incident_sha256="",
            )

        material_changed = _material_incident_changed(
            incident=existing,
            fingerprint=fingerprint,
            fingerprint_sha256=fingerprint_sha256,
        )
        if (
            not material_changed
            and not existing.material_fingerprint_complete
            and existing.material_fingerprint_sha256 != fingerprint_sha256
        ):
            event_id = build_public_ingress_incident_event_id(
                incident_id=existing.incident_id,
                event="baseline",
                material_fingerprint_sha256=fingerprint_sha256,
                previous_event_id=existing.latest_material_event_id,
            )
            baseline_event = PublicIngressIncidentEventRecord(
                event_id=event_id,
                incident_id=existing.incident_id,
                event="baseline",
                reason="migration_baseline",
                occurred_at=record.observed_at,
                observation_id=record.record_id,
                material_fingerprint_sha256=fingerprint_sha256,
                previous_material_fingerprint_sha256=existing.material_fingerprint_sha256,
                severity=fingerprint.severity,
                delivery_eligible=False,
                summary="Legacy incident material state enriched without notification delivery.",
            )
            incident = existing.model_copy(
                update={
                    "latest_observation_id": record.record_id,
                    "latest_observed_at": record.observed_at,
                    "failure_code": record.failure_code,
                    "state_version": existing.state_version + 1,
                    "severity": fingerprint.severity,
                    "material_fingerprint": fingerprint,
                    "material_fingerprint_sha256": fingerprint_sha256,
                    "material_fingerprint_complete": True,
                    "latest_material_event_id": event_id,
                    "latest_material_event": "baseline",
                    "latest_material_event_at": record.observed_at,
                    "recovery_observation_threshold": recovery_observation_threshold,
                    "consecutive_recovery_observations": 0,
                    "latest_recovery_observation_id": "",
                    "summary": record.summary,
                }
            )
            incident_events.append(baseline_event)
            return _PlannedIncidentTransition(
                observation=linked_observation.model_copy(
                    update={
                        "incident_id": incident.incident_id,
                        "incident_event_id": event_id,
                    }
                ),
                incidents=(incident, *reconciled_duplicates),
                incident_events=tuple(incident_events),
                reminder_states=with_reconciled_reminder_states(
                    _material_event_reminder_states(
                        record_store=record_store,
                        incident=incident,
                        incident_event=baseline_event,
                    )
                ),
                expected_open_incident_id=expected_incident_id,
                expected_open_incident_state_version=expected_state_version,
                expected_open_incident_sha256=expected_incident_sha256,
            )
        if material_changed:
            notification_state = existing.notification_state
            notification_state_changed_at = existing.notification_state_changed_at
            notification_state_reason = existing.notification_state_reason
            silenced_until = existing.silenced_until
            delivery_eligible = notification_state != "silenced"
            suppression_reason = "" if delivery_eligible else "silenced"
            if notification_state == "acknowledged":
                notification_state = "active"
                notification_state_changed_at = record.observed_at
                notification_state_reason = "material_change"
            event_id = build_public_ingress_incident_event_id(
                incident_id=existing.incident_id,
                event="updated",
                material_fingerprint_sha256=fingerprint_sha256,
                previous_event_id=existing.latest_material_event_id,
            )
            incident = existing.model_copy(
                update={
                    "latest_observation_id": record.record_id,
                    "latest_observed_at": record.observed_at,
                    "failure_code": record.failure_code,
                    "state_version": existing.state_version + 1,
                    "severity": fingerprint.severity,
                    "material_fingerprint": fingerprint,
                    "material_fingerprint_sha256": fingerprint_sha256,
                    "material_fingerprint_complete": True,
                    "latest_material_event_id": event_id,
                    "latest_material_event": "updated",
                    "latest_material_event_at": record.observed_at,
                    "notification_state": notification_state,
                    "notification_state_changed_at": notification_state_changed_at,
                    "notification_state_reason": notification_state_reason,
                    "silenced_until": silenced_until,
                    "recovery_observation_threshold": recovery_observation_threshold,
                    "consecutive_recovery_observations": 0,
                    "latest_recovery_observation_id": "",
                    "summary": record.summary,
                }
            )
            incident_event = PublicIngressIncidentEventRecord(
                event_id=event_id,
                incident_id=incident.incident_id,
                event="updated",
                reason="material_change",
                occurred_at=record.observed_at,
                observation_id=record.record_id,
                material_fingerprint_sha256=fingerprint_sha256,
                previous_material_fingerprint_sha256=existing.material_fingerprint_sha256,
                severity=fingerprint.severity,
                delivery_eligible=delivery_eligible,
                suppression_reason=cast(Any, suppression_reason),
                summary=record.summary,
            )
            reminder_states = _material_event_reminder_states(
                record_store=record_store,
                incident=incident,
                incident_event=incident_event,
            )
            incident_events.append(incident_event)
            return _PlannedIncidentTransition(
                observation=linked_observation.model_copy(
                    update={
                        "incident_id": incident.incident_id,
                        "incident_event_id": event_id,
                    }
                ),
                incidents=(incident, *reconciled_duplicates),
                incident_events=tuple(incident_events),
                reminder_states=with_reconciled_reminder_states(reminder_states),
                expected_open_incident_id=expected_incident_id,
                expected_open_incident_state_version=expected_state_version,
                expected_open_incident_sha256=expected_incident_sha256,
            )

        if existing.latest_observation_id == record.record_id:
            incident = existing
        else:
            incident = existing.model_copy(
                update={
                    "latest_observation_id": record.record_id,
                    "latest_observed_at": record.observed_at,
                    "failure_code": record.failure_code,
                    "state_version": existing.state_version + 1,
                    "severity": fingerprint.severity,
                    "material_fingerprint": fingerprint,
                    "material_fingerprint_sha256": fingerprint_sha256,
                    "material_fingerprint_complete": True,
                    "recovery_observation_threshold": recovery_observation_threshold,
                    "consecutive_recovery_observations": 0,
                    "latest_recovery_observation_id": "",
                    "summary": record.summary,
                }
            )
        reminder_events, reminder_states = _due_reminder_transition(
            record_store=record_store,
            incident=incident,
            observation=record,
        )
        incident_events.extend(reminder_events)
        return _PlannedIncidentTransition(
            observation=linked_observation.model_copy(update={"incident_id": incident.incident_id}),
            incidents=(incident, *reconciled_duplicates),
            incident_events=tuple(incident_events),
            reminder_states=with_reconciled_reminder_states(reminder_states),
            expected_open_incident_id=expected_incident_id,
            expected_open_incident_state_version=expected_state_version,
            expected_open_incident_sha256=expected_incident_sha256,
        )

    if existing is None:
        return _PlannedIncidentTransition(
            observation=record,
            incidents=tuple(reconciled_duplicates),
            incident_events=tuple(incident_events),
            reminder_states=with_reconciled_reminder_states(),
            expected_open_incident_id="",
            expected_open_incident_state_version=0,
            expected_open_incident_sha256="",
        )
    linked_observation = record.model_copy(update={"incident_id": existing.incident_id})
    if record.status == "pass":
        if existing.latest_recovery_observation_id == record.record_id:
            recovery_count = existing.consecutive_recovery_observations
        else:
            recovery_count = existing.consecutive_recovery_observations + 1
        if recovery_count >= recovery_observation_threshold:
            incident, resolved_event = _resolve_incident(
                incident=existing,
                record=record,
                reason="recovered",
                summary=record.summary,
                recovery_count=recovery_count,
                recovery_observation_threshold=recovery_observation_threshold,
            )
            incident_events.append(resolved_event)
            return _PlannedIncidentTransition(
                observation=linked_observation.model_copy(
                    update={"incident_event_id": resolved_event.event_id}
                ),
                incidents=(incident, *reconciled_duplicates),
                incident_events=tuple(incident_events),
                reminder_states=with_reconciled_reminder_states(
                    _resolved_reminder_states(
                        record_store=record_store,
                        incident=incident,
                        updated_at=record.observed_at,
                    )
                ),
                expected_open_incident_id=expected_incident_id,
                expected_open_incident_state_version=expected_state_version,
                expected_open_incident_sha256=expected_incident_sha256,
            )
        incident = existing.model_copy(
            update={
                "latest_observation_id": record.record_id,
                "latest_observed_at": record.observed_at,
                "state_version": (
                    existing.state_version
                    if existing.latest_recovery_observation_id == record.record_id
                    else existing.state_version + 1
                ),
                "recovery_observation_threshold": recovery_observation_threshold,
                "consecutive_recovery_observations": recovery_count,
                "latest_recovery_observation_id": record.record_id,
            }
        )
        return _PlannedIncidentTransition(
            observation=linked_observation,
            incidents=(incident, *reconciled_duplicates),
            incident_events=tuple(incident_events),
            reminder_states=with_reconciled_reminder_states(),
            expected_open_incident_id=expected_incident_id,
            expected_open_incident_state_version=expected_state_version,
            expected_open_incident_sha256=expected_incident_sha256,
        )
    return _PlannedIncidentTransition(
        observation=linked_observation,
        incidents=(existing, *reconciled_duplicates),
        incident_events=tuple(incident_events),
        reminder_states=with_reconciled_reminder_states(),
        expected_open_incident_id=expected_incident_id,
        expected_open_incident_state_version=expected_state_version,
        expected_open_incident_sha256=expected_incident_sha256,
    )


def _upgrade_legacy_incident(
    *,
    record_store: PublicIngressMonitorStore,
    existing: PublicIngressIncidentRecord,
) -> tuple[PublicIngressIncidentRecord, PublicIngressIncidentEventRecord]:
    historical_observation = _legacy_incident_observation(
        record_store=record_store,
        incident=existing,
    )
    if historical_observation is not None:
        fingerprint = _observation_material_fingerprint(historical_observation)
        fingerprint_complete = True
        observation_id = historical_observation.record_id
        occurred_at = historical_observation.observed_at
    else:
        fingerprint = _legacy_incident_fingerprint(existing)
        fingerprint_complete = False
        observation_id = existing.latest_observation_id
        occurred_at = existing.latest_observed_at
    fingerprint_sha256 = public_ingress_material_fingerprint_sha256(fingerprint)
    event_id = build_public_ingress_incident_event_id(
        incident_id=existing.incident_id,
        event="baseline",
        material_fingerprint_sha256=fingerprint_sha256,
    )
    baseline = PublicIngressIncidentEventRecord(
        event_id=event_id,
        incident_id=existing.incident_id,
        event="baseline",
        reason="migration_baseline",
        occurred_at=occurred_at,
        observation_id=observation_id,
        material_fingerprint_sha256=fingerprint_sha256,
        severity=fingerprint.severity,
        delivery_eligible=False,
        summary="Existing incident material state adopted without notification delivery.",
    )
    return (
        existing.model_copy(
            update={
                "schema_version": 2,
                "state_version": max(existing.state_version, 1),
                "severity": fingerprint.severity,
                "material_fingerprint": fingerprint,
                "material_fingerprint_sha256": fingerprint_sha256,
                "material_fingerprint_complete": fingerprint_complete,
                "latest_material_event_id": event_id,
                "latest_material_event": "baseline",
                "latest_material_event_at": occurred_at,
                "recovery_observation_threshold": 1,
            }
        ),
        baseline,
    )


def _legacy_incident_observation(
    *,
    record_store: PublicIngressMonitorStore,
    incident: PublicIngressIncidentRecord,
) -> PublicIngressObservationRecord | None:
    observations = record_store.list_public_ingress_observation_records(
        product=incident.product,
        context_name=incident.context,
        instance_name=incident.instance,
        check_name=incident.check_name,
        check_kind=incident.check_kind,
        limit=25,
    )
    return next(
        (
            observation
            for observation in observations
            if observation.record_id == incident.latest_observation_id
            and observation.status == "fail"
        ),
        None,
    )


def _legacy_incident_fingerprint(
    incident: PublicIngressIncidentRecord,
) -> PublicIngressIncidentMaterialFingerprint:
    digest = hashlib.sha256(incident.incident_id.encode("utf-8")).hexdigest()
    route_kind = {
        "private_http": "private_endpoint",
        "provider": "provider",
        "tls": "route_binding",
    }.get(incident.check_kind, "public_urls")
    authority_kwargs: dict[str, object] = {
        "kind": route_kind,
        "target_keys": (f"legacy-route-sha256:{digest}",),
    }
    if route_kind == "private_endpoint":
        authority_kwargs.update(
            private_endpoint_key="legacy-private-endpoint",
            private_endpoint_sha256=digest,
        )
    elif route_kind == "provider":
        authority_kwargs.update(provider="legacy-provider", provider_check="legacy-check")
    elif route_kind == "route_binding":
        authority_kwargs.update(domain_name=f"legacy-{digest[:16]}.invalid")
    target_kind = {
        "private_http": "private_health_url",
        "provider": "provider",
        "tls": "tls_domain",
    }.get(incident.check_kind, "health_url")
    return PublicIngressIncidentMaterialFingerprint(
        check_kind=incident.check_kind,
        failure_code=incident.failure_code,
        failure_layer=public_ingress_failure_layer(incident.failure_code),
        severity=public_ingress_incident_severity(incident.failure_code),
        affected_targets=(cast(Any, target_kind),),
        route_authority=PublicIngressIncidentRouteAuthority.model_validate(authority_kwargs),
        runtime_mismatch=(
            PublicIngressIncidentRuntimeMismatch(status="unverifiable")
            if incident.failure_code == "wrong_runtime_identity"
            else None
        ),
        tls_status="unknown" if incident.check_kind == "tls" else "",
    )


def _observation_material_fingerprint(
    record: PublicIngressObservationRecord,
) -> PublicIngressIncidentMaterialFingerprint:
    if record.material_fingerprint is not None:
        return record.material_fingerprint
    return build_public_ingress_material_fingerprint(record)


def _material_incident_changed(
    *,
    incident: PublicIngressIncidentRecord,
    fingerprint: PublicIngressIncidentMaterialFingerprint,
    fingerprint_sha256: str,
) -> bool:
    if incident.material_fingerprint_sha256 == fingerprint_sha256:
        return False
    if incident.material_fingerprint_complete or incident.material_fingerprint is None:
        return True
    previous = incident.material_fingerprint
    return (
        previous.check_kind != fingerprint.check_kind
        or previous.failure_code != fingerprint.failure_code
        or previous.failure_layer != fingerprint.failure_layer
        or previous.severity != fingerprint.severity
    )


def _expire_incident_silence(
    incident: PublicIngressIncidentRecord,
    *,
    observed_at: str,
) -> PublicIngressIncidentRecord:
    if incident.notification_state != "silenced":
        return incident
    try:
        silence_expired = _parse_utc_timestamp(observed_at) >= _parse_utc_timestamp(
            incident.silenced_until
        )
    except ValueError:
        silence_expired = True
    if not silence_expired:
        return incident
    return incident.model_copy(
        update={
            "notification_state": "active",
            "notification_state_changed_at": observed_at,
            "notification_state_reason": "silence_expired",
            "silenced_until": "",
        }
    )


def _resolve_incident(
    *,
    incident: PublicIngressIncidentRecord,
    record: PublicIngressObservationRecord,
    reason: Literal["recovered", "monitoring_intent_changed"],
    summary: str,
    recovery_count: int = 0,
    recovery_observation_threshold: int | None = None,
) -> tuple[PublicIngressIncidentRecord, PublicIngressIncidentEventRecord]:
    event_id = build_public_ingress_incident_event_id(
        incident_id=incident.incident_id,
        event="resolved",
        material_fingerprint_sha256=incident.material_fingerprint_sha256,
        previous_event_id=incident.latest_material_event_id,
    )
    resolved = incident.model_copy(
        update={
            "status": "resolved",
            "latest_observation_id": record.record_id,
            "latest_observed_at": record.observed_at,
            "resolved_at": record.observed_at,
            "resolved_observation_id": record.record_id,
            "resolution_reason": reason,
            "state_version": incident.state_version + 1,
            "latest_material_event_id": event_id,
            "latest_material_event": "resolved",
            "latest_material_event_at": record.observed_at,
            "recovery_observation_threshold": (
                recovery_observation_threshold or incident.recovery_observation_threshold
            ),
            "consecutive_recovery_observations": recovery_count,
            "latest_recovery_observation_id": (record.record_id if reason == "recovered" else ""),
            "summary": summary,
        }
    )
    return (
        resolved,
        PublicIngressIncidentEventRecord(
            event_id=event_id,
            incident_id=incident.incident_id,
            event="resolved",
            reason=reason,
            occurred_at=record.observed_at,
            observation_id=record.record_id,
            material_fingerprint_sha256=incident.material_fingerprint_sha256,
            previous_material_fingerprint_sha256=incident.material_fingerprint_sha256,
            severity=incident.severity,
            summary=summary,
        ),
    )


def _reconcile_duplicate_incident(
    *,
    incident: PublicIngressIncidentRecord,
    reconciled_at: str,
) -> tuple[PublicIngressIncidentRecord, PublicIngressIncidentEventRecord]:
    event_id = build_public_ingress_incident_event_id(
        incident_id=incident.incident_id,
        event="resolved",
        material_fingerprint_sha256=incident.material_fingerprint_sha256,
        previous_event_id=incident.latest_material_event_id,
    )
    summary = (
        "Duplicate open incident reconciled into the canonical incident. "
        f"Preserved evidence: {incident.summary}"
    )
    resolved = incident.model_copy(
        update={
            "status": "resolved",
            "resolved_at": reconciled_at,
            "resolved_observation_id": incident.latest_observation_id,
            "resolution_reason": "duplicate_reconciled",
            "state_version": incident.state_version + 1,
            "latest_material_event_id": event_id,
            "latest_material_event": "resolved",
            "latest_material_event_at": reconciled_at,
            "summary": summary,
        }
    )
    return (
        resolved,
        PublicIngressIncidentEventRecord(
            event_id=event_id,
            incident_id=incident.incident_id,
            event="resolved",
            reason="duplicate_reconciled",
            occurred_at=reconciled_at,
            observation_id=incident.latest_observation_id,
            material_fingerprint_sha256=incident.material_fingerprint_sha256,
            previous_material_fingerprint_sha256=incident.material_fingerprint_sha256,
            severity=incident.severity,
            summary=summary,
        ),
    )


def _material_event_reminder_states(
    *,
    record_store: PublicIngressMonitorStore,
    incident: PublicIngressIncidentRecord,
    incident_event: PublicIngressIncidentEventRecord,
) -> tuple[PublicIngressIncidentReminderStateRecord, ...]:
    policies = _matching_notification_policies(record_store=record_store, incident=incident)
    existing_states = _incident_reminder_states(
        record_store=record_store, incident_id=incident.incident_id
    )
    states: list[PublicIngressIncidentReminderStateRecord] = []
    matching_policy_ids = {policy.policy_id for policy in policies}
    for policy in policies:
        states.append(
            PublicIngressIncidentReminderStateRecord(
                reminder_state_id=build_public_ingress_reminder_state_id(
                    incident_id=incident.incident_id,
                    policy_id=policy.policy_id,
                ),
                incident_id=incident.incident_id,
                policy_id=policy.policy_id,
                status=("active" if incident.notification_state == "active" else "suppressed"),
                material_event_id=incident_event.event_id,
                interval_seconds=policy.reminder_interval_seconds,
                window_anchor_at=incident_event.occurred_at,
                next_reminder_at=_format_utc_timestamp(
                    _parse_utc_timestamp(incident_event.occurred_at)
                    + timedelta(seconds=policy.reminder_interval_seconds)
                ),
                updated_at=incident_event.occurred_at,
            )
        )
    states.extend(
        state.model_copy(
            update={
                "status": "policy_inactive",
                "next_reminder_at": "",
                "updated_at": incident_event.occurred_at,
            }
        )
        for state in existing_states
        if state.policy_id not in matching_policy_ids
    )
    return tuple(states)


def _due_reminder_transition(
    *,
    record_store: PublicIngressMonitorStore,
    incident: PublicIngressIncidentRecord,
    observation: PublicIngressObservationRecord,
) -> tuple[
    tuple[PublicIngressIncidentEventRecord, ...],
    tuple[PublicIngressIncidentReminderStateRecord, ...],
]:
    policies = _matching_notification_policies(record_store=record_store, incident=incident)
    existing_states = {
        state.policy_id: state
        for state in _incident_reminder_states(
            record_store=record_store, incident_id=incident.incident_id
        )
    }
    events: list[PublicIngressIncidentEventRecord] = []
    states: list[PublicIngressIncidentReminderStateRecord] = []
    observed_at = _parse_utc_timestamp(observation.observed_at)
    matching_policy_ids = {policy.policy_id for policy in policies}
    for policy in policies:
        state = existing_states.get(policy.policy_id)
        if (
            state is None
            or state.material_event_id != incident.latest_material_event_id
            or state.interval_seconds != policy.reminder_interval_seconds
        ):
            state = PublicIngressIncidentReminderStateRecord(
                reminder_state_id=build_public_ingress_reminder_state_id(
                    incident_id=incident.incident_id,
                    policy_id=policy.policy_id,
                ),
                incident_id=incident.incident_id,
                policy_id=policy.policy_id,
                status="active",
                material_event_id=incident.latest_material_event_id,
                interval_seconds=policy.reminder_interval_seconds,
                window_anchor_at=incident.latest_material_event_at,
                next_reminder_at=_format_utc_timestamp(
                    _parse_utc_timestamp(incident.latest_material_event_at)
                    + timedelta(seconds=policy.reminder_interval_seconds)
                ),
                updated_at=observation.observed_at,
            )
        anchor = _parse_utc_timestamp(state.window_anchor_at)
        elapsed_seconds = max(0, int((observed_at - anchor).total_seconds()))
        window_index = elapsed_seconds // policy.reminder_interval_seconds
        notification_active = incident.notification_state == "active"
        if notification_active and window_index >= 1 and window_index > state.last_window_index:
            window_started_at = _format_utc_timestamp(
                anchor + timedelta(seconds=window_index * policy.reminder_interval_seconds)
            )
            window_ended_at = _format_utc_timestamp(
                anchor + timedelta(seconds=(window_index + 1) * policy.reminder_interval_seconds)
            )
            event_id = build_public_ingress_incident_event_id(
                incident_id=incident.incident_id,
                event="reminder",
                material_fingerprint_sha256=incident.material_fingerprint_sha256,
                previous_event_id=incident.latest_material_event_id,
                policy_id=policy.policy_id,
                reminder_window_index=window_index,
                reminder_window_started_at=window_started_at,
            )
            events.append(
                PublicIngressIncidentEventRecord(
                    event_id=event_id,
                    incident_id=incident.incident_id,
                    event="reminder",
                    reason="reminder_due",
                    occurred_at=observation.observed_at,
                    observation_id=observation.record_id,
                    material_fingerprint_sha256=incident.material_fingerprint_sha256,
                    previous_material_fingerprint_sha256=incident.material_fingerprint_sha256,
                    severity=incident.severity,
                    policy_id=policy.policy_id,
                    reminder_window_index=window_index,
                    reminder_window_started_at=window_started_at,
                    reminder_window_ended_at=window_ended_at,
                    summary=f"Public ingress incident remains unresolved: {incident.summary}",
                )
            )
            state = state.model_copy(
                update={
                    "status": "active",
                    "last_window_index": window_index,
                    "last_reminder_event_id": event_id,
                    "last_reminded_at": observation.observed_at,
                    "next_reminder_at": window_ended_at,
                    "updated_at": observation.observed_at,
                }
            )
        else:
            next_window_index = max(1, state.last_window_index + 1)
            state = state.model_copy(
                update={
                    "status": "active" if notification_active else "suppressed",
                    "next_reminder_at": _format_utc_timestamp(
                        anchor
                        + timedelta(seconds=next_window_index * policy.reminder_interval_seconds)
                    ),
                    "updated_at": observation.observed_at,
                }
            )
        states.append(state)
    states.extend(
        state.model_copy(
            update={
                "status": "policy_inactive",
                "next_reminder_at": "",
                "updated_at": observation.observed_at,
            }
        )
        for policy_id, state in existing_states.items()
        if policy_id not in matching_policy_ids
    )
    return tuple(events), tuple(states)


def _resolved_reminder_states(
    *,
    record_store: PublicIngressMonitorStore,
    incident: PublicIngressIncidentRecord,
    updated_at: str,
) -> tuple[PublicIngressIncidentReminderStateRecord, ...]:
    return tuple(
        state.model_copy(
            update={
                "status": "resolved",
                "next_reminder_at": "",
                "updated_at": updated_at,
            }
        )
        for state in _incident_reminder_states(
            record_store=record_store, incident_id=incident.incident_id
        )
    )


def _incident_reminder_states(
    *,
    record_store: PublicIngressMonitorStore,
    incident_id: str,
) -> tuple[PublicIngressIncidentReminderStateRecord, ...]:
    reader = getattr(record_store, "list_public_ingress_incident_reminder_state_records", None)
    if not callable(reader):
        return ()
    return tuple(reader(incident_id=incident_id))


def _incident_event(*, incident: PublicIngressIncidentRecord) -> PublicIngressIncidentEvent:
    if incident.status == "resolved":
        return "resolved"
    if incident.latest_material_event in {"opened", "updated"}:
        return cast(PublicIngressIncidentEvent, incident.latest_material_event)
    if incident.opened_observation_id == incident.latest_observation_id:
        return "opened"
    return "updated"


def _matching_notification_policies(
    *, record_store: PublicIngressMonitorStore, incident: PublicIngressIncidentRecord
) -> tuple[PublicIngressNotificationPolicyRecord, ...]:
    policies = record_store.list_public_ingress_notification_policy_records(
        product=incident.product,
        context_name=incident.context,
        instance_name=incident.instance,
        status="enabled",
    )
    return tuple(policy for policy in policies if policy.matches(incident))


def _notification_attempt(
    *,
    record_store: PublicIngressMonitorStore,
    attempt_id: str,
    incident_id: str,
    event: PublicIngressIncidentEvent,
) -> PublicIngressNotificationAttemptRecord | None:
    return next(
        (
            attempt
            for attempt in record_store.list_public_ingress_notification_attempt_records(
                incident_id=incident_id,
                event=event,
            )
            if attempt.attempt_id == attempt_id
        ),
        None,
    )


def check_public_ingress_target(
    *,
    target: PublicIngressMonitorTarget,
    checked_at: str,
    timeout_seconds: int,
    http_get: HttpGet,
    tls_get: TlsGet,
) -> PublicIngressObservationRecord:
    target_observations: list[PublicIngressTargetObservation] = []
    for target_kind, url in _target_urls(target):
        target_observations.append(
            _check_url(
                target_kind=target_kind,
                url=url,
                timeout_seconds=timeout_seconds,
                target=target,
                http_get=http_get,
                tls_get=tls_get,
                checked_at=checked_at,
            )
        )
    status, failure_code = _record_status(target_observations)
    summary = _record_summary(target=target, status=status, observations=target_observations)
    observation = PublicIngressObservationRecord(
        schema_version=2,
        record_id=build_public_ingress_observation_id(
            product=target.product,
            context=target.context,
            instance=target.instance,
            observed_at=checked_at,
            check_name=target.check_name,
        ),
        product=target.product,
        repository=target.repository,
        driver_id=target.driver_id,
        context=target.context,
        instance=target.instance,
        check_name=target.check_name,
        check_kind=target.check_kind,
        purpose="probe",
        monitoring_intent=target.monitoring_intent,
        observed_at=checked_at,
        status=status,
        failure_code=failure_code,
        base_url=target.base_url,
        health_url=target.health_url,
        expected_runtime_identity=target.expected_runtime_identity,
        targets=tuple(target_observations),
        summary=summary,
    )
    if observation.status != "fail":
        return observation
    material_fingerprint = build_public_ingress_material_fingerprint(
        observation,
        private_endpoint_key=target.private_endpoint_key,
        private_endpoint_sha256=target.private_endpoint_material_sha256,
        provider=target.provider,
        provider_check=target.provider_check,
    )
    return observation.model_copy(
        update={
            "material_fingerprint": material_fingerprint,
            "material_fingerprint_sha256": public_ingress_material_fingerprint_sha256(
                material_fingerprint
            ),
        }
    )


def fetch_public_ingress_url(url: str, timeout_seconds: int) -> HttpObservation:
    response = request_public_http(
        url,
        headers={"User-Agent": USER_AGENT},
        timeout_seconds=timeout_seconds,
        max_redirects=MAX_REDIRECTS,
    )
    payload: object = None
    if response.body and "json" in response.header("content-type").lower():
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
    return HttpObservation(
        status_code=response.status_code,
        final_url=response.final_url,
        redirect_count=response.redirect_count,
        payload=payload,
    )


def fetch_private_health_url(url: str, timeout_seconds: int) -> HttpObservation:
    response = request_private_http(
        url,
        headers={"User-Agent": USER_AGENT},
        timeout_seconds=timeout_seconds,
        max_redirects=MAX_REDIRECTS,
    )
    payload: object = None
    if response.body and "json" in response.header("content-type").lower():
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
    return HttpObservation(
        status_code=response.status_code,
        final_url=response.final_url,
        redirect_count=response.redirect_count,
        payload=payload,
    )


def fetch_public_tls(public_name: str, timeout_seconds: int) -> PublicTlsProbeResult:
    return probe_public_tls(
        public_name,
        timeout_seconds=timeout_seconds,
        expiring_days=PUBLIC_TLS_EXPIRING_DAYS,
    )


def _check_url(
    *,
    target_kind: PublicIngressTargetKind,
    url: str,
    timeout_seconds: int,
    target: PublicIngressMonitorTarget,
    http_get: HttpGet,
    tls_get: TlsGet,
    checked_at: str,
) -> PublicIngressTargetObservation:
    if target.check_kind == "tls":
        return _check_tls_target(
            target_kind=target_kind,
            url=url,
            timeout_seconds=timeout_seconds,
            target=target,
            tls_get=tls_get,
            checked_at=checked_at,
        )
    if target.check_kind == "provider":
        return _provider_check_unavailable(target=target)
    if target.resolution_failure_code is not None:
        return PublicIngressTargetObservation(
            target=target_kind,
            url=url,
            status="fail",
            failure_code=target.resolution_failure_code,
            summary=target.resolution_failure_summary
            or "Private health endpoint could not be resolved.",
        )
    public_url_error = None
    if target.check_kind != "private_http":
        public_url_error = _public_url_error(url)
    if public_url_error is not None:
        return PublicIngressTargetObservation(
            target=target_kind,
            url=url,
            status="fail",
            failure_code=public_url_error,
            summary=_failure_summary(public_url_error),
        )
    try:
        observation = http_get(url, timeout_seconds)
    except HTTPError as error:
        return PublicIngressTargetObservation(
            target=target_kind,
            url=url,
            status="fail",
            failure_code=(
                "health_status_error"
                if target_kind in {"health_url", "private_health_url"}
                else "http_error"
            ),
            http_status=error.code,
            final_url=error.url or "",
            summary=f"HTTP {error.code}",
        )
    except TimeoutError:
        return _failed_target(target_kind=target_kind, url=url, code="connection_timeout")
    except PublicHttpDestinationError as error:
        return _failed_target(
            target_kind=target_kind,
            url=url,
            code=error.code,
            summary=str(error),
        )
    except URLError as error:
        return _url_error_observation(target_kind=target_kind, url=url, error=error)
    except ssl.SSLError:
        return _failed_target(target_kind=target_kind, url=url, code="tls_failure")
    except RuntimeError as error:
        message = str(error).lower()
        code: PublicIngressFailureCode = "redirect_loop"
        if "self redirect" in message:
            code = "self_redirect"
        return _failed_target(
            target_kind=target_kind,
            url=url,
            code=code,
            summary=str(error),
        )
    except OSError as error:
        return _failed_target(
            target_kind=target_kind,
            url=url,
            code="unknown_error",
            summary=str(error),
        )
    if not 200 <= observation.status_code < 300:
        return PublicIngressTargetObservation(
            target=target_kind,
            url=url,
            status="fail",
            failure_code=(
                "health_status_error"
                if target_kind in {"health_url", "private_health_url"}
                else "http_error"
            ),
            http_status=observation.status_code,
            final_url=observation.final_url,
            redirect_count=observation.redirect_count,
            summary=f"HTTP {observation.status_code}",
        )
    runtime_status: RuntimeIdentityStatus = "unchecked"
    runtime_detail = ""
    observed_runtime_identity: RuntimeIdentity | None = None
    if (
        target_kind in {"health_url", "private_health_url"}
        and target.require_runtime_identity
        and target.expected_runtime_identity is None
    ):
        return PublicIngressTargetObservation(
            target=target_kind,
            url=url,
            status="fail",
            failure_code="wrong_runtime_identity",
            http_status=observation.status_code,
            final_url=observation.final_url,
            redirect_count=observation.redirect_count,
            runtime_identity_status="unverifiable",
            runtime_identity_detail=(
                "No expected runtime identity is recorded for this strict public health check."
            ),
            summary="Expected runtime identity is unavailable.",
        )
    if (
        target_kind in {"health_url", "private_health_url"}
        and target.expected_runtime_identity is not None
    ):
        runtime_status, runtime_detail, observed_runtime_identity = (
            health_payload_runtime_identity_status(
                expected=target.expected_runtime_identity,
                payload=observation.payload,
                json_parse_failed=False,
            )
        )
        hard_runtime_identity_failure = runtime_status in {"malformed", "mismatch"} or (
            target.require_runtime_identity and runtime_status != "match"
        )
        if hard_runtime_identity_failure:
            return PublicIngressTargetObservation(
                target=target_kind,
                url=url,
                status="fail",
                failure_code="wrong_runtime_identity",
                http_status=observation.status_code,
                final_url=observation.final_url,
                redirect_count=observation.redirect_count,
                runtime_identity_status=runtime_status,
                runtime_identity_detail=runtime_detail,
                observed_runtime_identity=observed_runtime_identity,
                summary=runtime_detail or "Runtime identity did not match expected deployment.",
            )
    return PublicIngressTargetObservation(
        target=target_kind,
        url=url,
        status="pass",
        http_status=observation.status_code,
        final_url=observation.final_url,
        redirect_count=observation.redirect_count,
        runtime_identity_status=runtime_status,
        runtime_identity_detail=runtime_detail,
        observed_runtime_identity=observed_runtime_identity,
        summary=_successful_target_summary(target),
    )


def _check_tls_target(
    *,
    target_kind: PublicIngressTargetKind,
    url: str,
    timeout_seconds: int,
    target: PublicIngressMonitorTarget,
    tls_get: TlsGet,
    checked_at: str,
) -> PublicIngressTargetObservation:
    domain_name = target.tls_domain_name.strip().lower()
    if not domain_name:
        domain_name = "missing.invalid"
        probe_result = PublicTlsProbeResult(
            status="unknown",
            failure_code="unknown_error",
            summary="TLS monitor target is missing a bound domain.",
        )
    else:
        try:
            probe_result = tls_get(domain_name, timeout_seconds)
        except TimeoutError:
            probe_result = PublicTlsProbeResult(
                status="unreachable",
                failure_code="connection_timeout",
                summary="Public TLS probe timed out.",
            )
        except Exception as error:  # noqa: BLE001 - probe failures become monitor evidence.
            probe_result = PublicTlsProbeResult(
                status="unknown",
                failure_code="unknown_error",
                summary=_bounded_probe_error_summary(error),
            )

    tls_evidence = _tls_observation(
        target=target,
        public_name=domain_name,
        probe_result=probe_result,
        checked_at=checked_at,
    )
    target_status: PublicIngressObservationStatus = (
        "pass" if probe_result.status == "valid" else "fail"
    )
    failure_code = probe_result.failure_code
    if probe_result.status == "unsupported" and failure_code == "tls_unsupported":
        target_status = "skipped"
    return PublicIngressTargetObservation(
        target=target_kind,
        url=url,
        status=target_status,
        failure_code=failure_code,
        tls=tls_evidence,
        summary=probe_result.summary,
    )


def _provider_check_unavailable(
    *,
    target: PublicIngressMonitorTarget,
) -> PublicIngressTargetObservation:
    return PublicIngressTargetObservation(
        target="provider",
        url=f"provider://{target.provider}/{target.provider_check}",
        status="fail",
        failure_code="provider_check_unavailable",
        summary=(
            f"Provider health check {target.provider}/{target.provider_check} "
            "is not wired to a monitor driver."
        ),
    )


def _profile_uses_generic_web(profile: LaunchplaneProductProfileRecord) -> bool:
    if profile.driver_id == "generic-web":
        return True
    try:
        return read_driver_descriptor(profile.driver_id).base_driver_id == "generic-web"
    except FileNotFoundError:
        return False


def _expected_runtime_identity(
    *, record_store: object, lane: ProductLaneProfile
) -> RuntimeIdentity | None:
    read_lane_summary = getattr(record_store, "read_lane_summary", None)
    if not callable(read_lane_summary):
        return None
    try:
        lane_summary = read_lane_summary(
            context_name=lane.context,
            instance_name=lane.instance,
        )
    except (FileNotFoundError, KeyError):
        return None
    if not isinstance(lane_summary, LaunchplaneLaneSummary):
        return None
    if lane_summary.inventory is not None:
        return lane_summary.inventory.runtime_identity
    if lane_summary.latest_deployment is not None:
        return lane_summary.latest_deployment.runtime_identity
    return None


def _open_incidents(
    *, record_store: PublicIngressMonitorStore, record: PublicIngressObservationRecord
) -> tuple[PublicIngressIncidentRecord, ...]:
    incidents = record_store.list_public_ingress_incident_records(
        product=record.product,
        context_name=record.context,
        instance_name=record.instance,
        check_name=record.check_name,
        check_kind=record.check_kind,
        status="open",
    )
    return tuple(
        incident
        for incident in incidents
        if incident.product == record.product
        and incident.context == record.context
        and incident.instance == record.instance
        and canonical_health_check_record_token(incident.check_name)
        == canonical_health_check_record_token(record.check_name)
        and incident.check_kind == record.check_kind
    )


def _target_urls(
    target: PublicIngressMonitorTarget,
) -> tuple[tuple[PublicIngressTargetKind, str], ...]:
    urls: list[tuple[PublicIngressTargetKind, str]] = []
    if target.check_kind == "tls":
        domain_name = target.tls_domain_name.strip().lower() or "missing.invalid"
        urls.append(("tls_domain", f"https://{domain_name}/"))
        return tuple(urls)
    if target.check_kind == "provider":
        urls.append(("provider", f"provider://{target.provider}/{target.provider_check}"))
        return tuple(urls)
    if target.check_kind == "private_http":
        if target.health_url:
            urls.append(("private_health_url", target.health_url))
        elif target.resolution_failure_code is not None:
            urls.append(("private_health_url", f"private-endpoint://{target.private_endpoint_key}"))
        return tuple(urls)
    if target.base_url:
        urls.append(("base_url", target.base_url))
    if target.health_url and target.health_url != target.base_url:
        urls.append(("health_url", target.health_url))
    return tuple(urls)


def _record_status(
    observations: list[PublicIngressTargetObservation],
) -> tuple[PublicIngressObservationStatus, PublicIngressFailureCode | None]:
    failing = next(
        (observation for observation in observations if observation.status == "fail"), None
    )
    if failing is not None:
        return "fail", failing.failure_code
    if all(observation.status == "skipped" for observation in observations):
        skipped_failure_code = next(
            (
                observation.failure_code
                for observation in observations
                if observation.failure_code is not None
            ),
            None,
        )
        return "skipped", skipped_failure_code
    return "pass", None


def _record_summary(
    *,
    target: PublicIngressMonitorTarget,
    status: PublicIngressObservationStatus,
    observations: list[PublicIngressTargetObservation],
) -> str:
    if status == "pass":
        return f"{_check_label(target)} is reachable for {target.product}/{target.instance}."
    failing = next(
        (observation for observation in observations if observation.status == "fail"), None
    )
    if failing is not None:
        return f"{_check_label(target)} failed for {target.product}/{target.instance}: {failing.summary}"
    return f"{_check_label(target)} monitoring skipped {target.product}/{target.instance}."


def _check_label(target: PublicIngressMonitorTarget) -> str:
    if target.check_kind == "tls":
        return "Public TLS"
    if target.check_kind == "private_http":
        return "Private health check"
    if target.check_kind == "provider":
        return "Provider health check"
    return "Public ingress"


def _successful_target_summary(target: PublicIngressMonitorTarget) -> str:
    if target.check_kind == "tls":
        return "Public TLS certificate is valid."
    if target.check_kind == "private_http":
        return "Private health check returned a successful response."
    return "Public ingress returned a successful response."


def _public_url_error(url: str) -> PublicIngressFailureCode | None:
    error = public_url_error(url)
    if error == "invalid_url":
        return "invalid_url"
    if error == "private_url":
        return "private_url"
    return None


def _url_error_observation(
    *, target_kind: PublicIngressTargetKind, url: str, error: URLError
) -> PublicIngressTargetObservation:
    reason = error.reason
    code: PublicIngressFailureCode = "unknown_error"
    if isinstance(reason, TimeoutError):
        code = "connection_timeout"
    elif isinstance(reason, ssl.SSLError):
        code = "tls_failure"
    elif isinstance(reason, socket.gaierror):
        code = "dns_failure"
    return _failed_target(target_kind=target_kind, url=url, code=code, summary=str(reason))


def _failed_target(
    *,
    target_kind: PublicIngressTargetKind,
    url: str,
    code: PublicIngressFailureCode,
    summary: str = "",
) -> PublicIngressTargetObservation:
    return PublicIngressTargetObservation(
        target=target_kind,
        url=url,
        status="fail",
        failure_code=code,
        summary=summary.strip() or _failure_summary(code),
    )


def _failure_summary(code: PublicIngressFailureCode) -> str:
    return code.replace("_", " ")


def _tls_monitor_targets(
    *,
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
) -> tuple[PublicIngressMonitorTarget, ...]:
    if not product_lane_monitoring_probe_effective(
        monitoring_intent=lane.health_monitoring.monitoring_intent,
        check_kind="tls",
    ):
        return ()
    list_route_bindings = getattr(record_store, "list_route_binding_records", None)
    if not callable(list_route_bindings):
        return ()
    route_bindings = list_route_bindings(
        product=profile.product,
        context_name=lane.context,
        instance_name=lane.instance,
        status="active",
        limit=1,
    )
    route_binding = next(iter(route_bindings), None)
    if route_binding is None or not isinstance(route_binding, EnvironmentRouteBindingRecord):
        return ()
    targets: list[PublicIngressMonitorTarget] = []
    for domain in route_binding.domains:
        if not isinstance(domain, RouteBindingDomain):
            continue
        targets.append(
            PublicIngressMonitorTarget(
                product=profile.product,
                repository=profile.repository,
                driver_id=profile.driver_id,
                context=lane.context,
                instance=lane.instance,
                base_url="",
                health_url="",
                check_name=build_public_ingress_tls_check_name(domain.domain_name),
                check_kind="tls",
                profile_sha256=product_profile_record_sha256(profile),
                monitoring_intent=lane.health_monitoring.monitoring_intent,
                incident_eligible=product_lane_monitoring_incident_eligible(
                    monitoring_intent=lane.health_monitoring.monitoring_intent,
                    check_kind="tls",
                ),
                expected_runtime_identity=None,
                require_runtime_identity=False,
                tls_domain_name=domain.domain_name,
                tls_domain_role=domain.role,
                tls_owner=route_binding.tls.owner,
                tls_ingress_provider=route_binding.ingress.provider,
                tls_termination_kind=route_binding.ingress.termination_kind,
                tls_source_kind=route_binding.source.source_kind,
                tls_source_label=route_binding.source.source_label,
                tls_source_record_ids=route_binding.source.source_record_ids,
                tls_source_versions=dict(route_binding.source.source_versions),
                tls_refreshed_at=route_binding.source.refreshed_at,
                tls_recorded_at=route_binding.updated_at,
                tls_freshness_status=route_binding.source.freshness_status,
                tls_stale_after=route_binding.source.stale_after,
                tls_provider_evidence=dict(route_binding.tls.provider_evidence),
                route_binding_sha256=route_binding_record_sha256(route_binding),
            )
        )
    return tuple(targets)


def _bounded_probe_error_summary(error: BaseException, *, limit: int = 512) -> str:
    normalized_summary = " ".join(
        (str(error) or error.__class__.__name__).replace("\n", " ").split()
    )
    if len(normalized_summary) <= limit:
        return normalized_summary
    return f"{normalized_summary[: limit - 1].rstrip()}…"


def _tls_observation(
    *,
    target: PublicIngressMonitorTarget,
    public_name: str,
    probe_result: PublicTlsProbeResult,
    checked_at: str,
) -> PublicIngressTlsObservation:
    stale_after = _format_utc_timestamp(
        _parse_utc_timestamp(checked_at) + timedelta(seconds=PUBLIC_TLS_STALE_AFTER_SECONDS)
    )
    certificate = probe_result.certificate
    return PublicIngressTlsObservation(
        status=probe_result.status,
        public_name=public_name,
        issuer=certificate.issuer if certificate is not None else "",
        subject=certificate.subject if certificate is not None else "",
        not_before=certificate.not_before if certificate is not None else "",
        not_after=certificate.not_after if certificate is not None else "",
        days_remaining=certificate.days_remaining if certificate is not None else None,
        public_name_match=certificate.public_name_match if certificate is not None else False,
        public_name_match_source=(
            certificate.public_name_match_source if certificate is not None else "none"
        ),
        presented_san_count=certificate.presented_san_count if certificate is not None else 0,
        presented_name_evidence=(
            certificate.presented_name_evidence if certificate is not None else ()
        ),
        recorded=PublicIngressTlsRecordedEvidence(
            domain_name=public_name,
            domain_role=target.tls_domain_role,
            route_binding_status="active",
            owner=target.tls_owner,
            ingress_provider=target.tls_ingress_provider,
            termination_kind=target.tls_termination_kind,
            source_kind=target.tls_source_kind,
            source_label=target.tls_source_label,
            source_record_ids=target.tls_source_record_ids,
            source_versions=dict(target.tls_source_versions or {}),
            refreshed_at=target.tls_refreshed_at,
            recorded_at=target.tls_recorded_at,
            freshness_status=target.tls_freshness_status,
            stale_after=target.tls_stale_after,
            provider_evidence=dict(target.tls_provider_evidence or {}),
        ),
        probe=PublicIngressTlsProbeEvidence(
            observed_at=checked_at,
            validated_address_count=probe_result.validated_address_count,
            sni_hostname=public_name,
            freshness_status="verified" if probe_result.status == "valid" else "recorded",
            stale_after=stale_after,
        ),
    )


def _parse_utc_timestamp(value: str) -> datetime:
    normalized_value = value.strip()
    if normalized_value.endswith("Z"):
        normalized_value = f"{normalized_value[:-1]}+00:00"
    parsed = datetime.fromisoformat(normalized_value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_utc_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _health_url(base_url: str, health_path: str) -> str:
    if not base_url.strip():
        return ""
    normalized_health_path = health_path.strip()
    if not normalized_health_path.startswith("/"):
        normalized_health_path = f"/{normalized_health_path}"
    return f"{base_url.rstrip('/')}{normalized_health_path}"


def _monitor_health_url(
    *, profile: LaunchplaneProductProfileRecord, lane: ProductLaneProfile, base_url: str
) -> str:
    lane_health_url = lane.health_url.strip()
    if profile.driver_id == "odoo":
        if lane_health_url and not is_legacy_derived_odoo_health_url(
            health_url=lane_health_url,
            base_url=base_url,
            profile_health_path=profile.health_path,
        ):
            return lane_health_url
        return default_odoo_health_url(base_url=base_url)
    if lane_health_url:
        return lane_health_url
    return _health_url(base_url, profile.health_path).strip()


def _normalized_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    path = parsed.path or "/"
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            path.rstrip("/") or "/",
            parsed.query,
            "",
        )
    )


def public_http_health_check(
    *,
    name: str = "public-ingress",
    require_runtime_identity: bool = False,
) -> ProductLaneHealthCheck:
    return ProductLaneHealthCheck(
        name=name,
        kind="public_http",
        require_runtime_identity=require_runtime_identity,
    )


class PublicIngressNotificationDriverSet:
    def __init__(
        self,
        *,
        github_client: Callable[[str, dict[str, object]], dict[str, object]] | None = None,
        email_sender: Callable[[PublicIngressNotificationDestination, EmailMessage], object]
        | None = None,
        discord_sender: Callable[[str, dict[str, object]], object] | None = None,
        secret_resolver: Callable[[str], str] | None = None,
        incident_secret_resolver: Callable[[str, PublicIngressIncidentRecord], str] | None = None,
    ) -> None:
        self.github_client = github_client or _gh_issue_client
        self.email_sender = email_sender or _send_email_message
        self.discord_sender = discord_sender or _post_discord_webhook
        self.secret_resolver = secret_resolver or (lambda _secret_name: "")
        self.incident_secret_resolver = incident_secret_resolver

    def send(
        self,
        *,
        destination: PublicIngressNotificationDestination,
        event: PublicIngressIncidentEvent,
        incident: PublicIngressIncidentRecord,
        observation: PublicIngressObservationRecord,
        previous_attempts: tuple[PublicIngressNotificationAttemptRecord, ...] = (),
    ) -> PublicIngressNotificationDelivery:
        try:
            if destination.kind == "github_issue":
                return _deliver_github_issue_notification(
                    destination=destination,
                    event=event,
                    incident=incident,
                    observation=observation,
                    previous_attempts=previous_attempts,
                    github_client=self.github_client,
                )
            if destination.kind == "email":
                return _deliver_email_notification(
                    destination=destination,
                    event=event,
                    incident=incident,
                    observation=observation,
                    email_sender=self.email_sender,
                    secret_resolver=lambda secret_name: self._resolve_secret(secret_name, incident),
                )
            if destination.kind == "discord":
                return _deliver_discord_notification(
                    destination=destination,
                    event=event,
                    incident=incident,
                    observation=observation,
                    discord_sender=self.discord_sender,
                    secret_resolver=lambda secret_name: self._resolve_secret(secret_name, incident),
                )
        except Exception as error:  # noqa: BLE001 - delivery failures are recorded per destination.
            return PublicIngressNotificationDelivery(
                delivery_status="failed",
                action="provider_error",
                error_message=str(error) or error.__class__.__name__,
            )
        return PublicIngressNotificationDelivery(
            delivery_status="failed",
            action="unsupported_destination",
            error_message=f"Unsupported public ingress notification destination: {destination.kind}",
        )

    def _resolve_secret(self, secret_name: str, incident: PublicIngressIncidentRecord) -> str:
        if self.incident_secret_resolver is not None:
            return self.incident_secret_resolver(secret_name, incident)
        return self.secret_resolver(secret_name)


def public_ingress_secret_read_store(
    record_store: object,
) -> control_plane_secrets.SecretReadStore | None:
    if callable(getattr(record_store, "read_secret_record", None)) and callable(
        getattr(record_store, "read_secret_version", None)
    ):
        return cast(control_plane_secrets.SecretReadStore, record_store)
    return None


def public_ingress_managed_secret_resolver(
    *,
    record_store: control_plane_secrets.SecretReadStore,
) -> Callable[[str, PublicIngressIncidentRecord], str]:
    def resolve(secret_id: str, incident: PublicIngressIncidentRecord) -> str:
        normalized_secret_id = secret_id.strip()
        if not normalized_secret_id:
            return ""
        try:
            record = record_store.read_secret_record(normalized_secret_id)
        except Exception:  # noqa: BLE001 - delivery records capture missing secrets per destination.
            return ""
        if record.status != control_plane_secrets.SECRET_STATUS_CONFIGURED:
            return ""
        if not control_plane_secrets._scope_matches_record(
            record,
            context_name=incident.context,
            instance_name=incident.instance,
        ):
            return ""
        try:
            version = record_store.read_secret_version(record.current_version_id)
            return control_plane_secrets._decrypt_secret_value(version.ciphertext, version.key_id)
        except Exception:  # noqa: BLE001 - delivery records capture unreadable secrets per destination.
            return ""

    return resolve


def public_ingress_notification_drivers(
    *,
    record_store: object,
) -> PublicIngressNotificationDriverSet:
    secret_store = public_ingress_secret_read_store(record_store)
    if secret_store is None:
        return PublicIngressNotificationDriverSet()
    return PublicIngressNotificationDriverSet(
        incident_secret_resolver=public_ingress_managed_secret_resolver(record_store=secret_store)
    )


def deliver_public_ingress_notification_outbox_delivery(
    *,
    record: OutboxDeliveryRecord,
    drivers: PublicIngressNotificationDriverSet,
    mark_provider_started: Callable[[str, str], None],
) -> OutboxDeliveryRecord:
    delivery_record = record
    try:
        payload = record.payload
        attempt_payload = _dict_payload(payload.get("attempt"))
        destination = PublicIngressNotificationDestination.model_validate(
            _dict_payload(payload.get("destination"))
        )
        body = _required_payload_text(payload, "body")
        marker = _required_payload_text(payload, "marker")
        existing_issue_url = _optional_payload_text(payload, "existing_issue_url")
        incident_marker = _optional_payload_text(payload, "incident_marker")
        event = _public_ingress_event_value(attempt_payload.get("event"))
        if (
            event != "opened"
            and not existing_issue_url
            and destination.github_issue_number is None
            and incident_marker
        ):
            incident_response = drivers.github_client(
                "find_marker",
                {
                    "repository": destination.github_repository,
                    "issue_number": None,
                    "issue_url": "",
                    "marker": incident_marker,
                },
            )
            existing_issue_url = str(incident_response.get("url", "")).strip()
        if (
            event == "resolved"
            and not existing_issue_url
            and destination.github_issue_number is None
        ):
            return _skipped_public_ingress_outbox_delivery(
                record=record,
                payload=payload,
                attempt_payload=attempt_payload,
                marker=marker,
            )
        action = (
            "comment"
            if destination.github_issue_number is not None or existing_issue_url
            else "create"
        )
        if event == "resolved" and existing_issue_url:
            action = "close"
        provider_operation_key = ":".join(
            (
                "public_ingress_notification",
                str(attempt_payload.get("attempt_id") or "").strip(),
                action,
            )
        )
        delivery_record = record.model_copy(
            update={
                "provider_operation_key": provider_operation_key,
                "provider_id": "github",
            }
        )
        if record.provider_operation_key:
            reconciled_response = drivers.github_client(
                "find_marker",
                {
                    "repository": destination.github_repository,
                    "issue_number": destination.github_issue_number,
                    "issue_url": existing_issue_url,
                    "marker": marker,
                },
            )
            if reconciled_response:
                if action == "close":
                    reconciled_response = drivers.github_client(
                        "ensure_closed",
                        {
                            "repository": destination.github_repository,
                            "issue_number": destination.github_issue_number,
                            "issue_url": existing_issue_url,
                        },
                    )
                return _delivered_public_ingress_outbox_delivery(
                    record=delivery_record,
                    payload=payload,
                    attempt_payload=attempt_payload,
                    action=action,
                    marker=marker,
                    response=reconciled_response,
                )
        else:
            mark_provider_started(provider_operation_key, "github")
        response = drivers.github_client(
            action,
            {
                "repository": destination.github_repository,
                "title": f"Public ingress incident: {attempt_payload.get('incident_id', '')}",
                "body": body,
                "labels": [destination.github_label] if destination.github_label else [],
                "issue_number": destination.github_issue_number,
                "issue_url": existing_issue_url,
            },
        )
        return _delivered_public_ingress_outbox_delivery(
            record=delivery_record,
            payload=payload,
            attempt_payload=attempt_payload,
            action=action,
            marker=marker,
            response=response,
        )
    except (ValueError, KeyError) as error:
        return delivery_record.model_copy(
            update={
                "state": "failed",
                "error_code": _public_ingress_provider_safe_error_code(error),
                "lease_owner": "",
                "lease_expires_at": "",
            }
        )
    except Exception as error:  # noqa: BLE001 - provider failures remain bounded retries.
        return retry_outbox_delivery(
            delivery_record,
            error_code=_public_ingress_provider_safe_error_code(error),
        )


def _deliver_github_issue_notification(
    *,
    destination: PublicIngressNotificationDestination,
    event: PublicIngressIncidentEvent,
    incident: PublicIngressIncidentRecord,
    observation: PublicIngressObservationRecord,
    previous_attempts: tuple[PublicIngressNotificationAttemptRecord, ...],
    github_client: Callable[[str, dict[str, object]], dict[str, object]],
) -> PublicIngressNotificationDelivery:
    issue_url = _latest_external_url(previous_attempts)
    body = public_ingress_incident_notification_body(
        event=event, incident=incident, observation=observation
    )
    if event == "resolved" and not issue_url and destination.github_issue_number is None:
        return PublicIngressNotificationDelivery(
            delivery_status="skipped",
            action="no_prior_issue",
        )
    if event == "opened" or not issue_url:
        payload: dict[str, object] = {
            "repository": destination.github_repository,
            "title": f"Public ingress incident: {incident.product}/{incident.instance}",
            "body": body,
        }
        if destination.github_label:
            payload["labels"] = [destination.github_label]
        if destination.github_issue_number is not None:
            payload["issue_number"] = destination.github_issue_number
            response = github_client("comment", payload)
            return PublicIngressNotificationDelivery(
                delivery_status="delivered",
                action="commented_issue",
                external_url=str(response.get("url", "")).strip(),
                external_id=str(response.get("id", "")).strip(),
            )
        response = github_client("create", payload)
        return PublicIngressNotificationDelivery(
            delivery_status="delivered",
            action="created_issue",
            external_url=str(response.get("url", "")).strip(),
            external_id=str(response.get("id", "")).strip(),
        )
    payload = {
        "repository": destination.github_repository,
        "issue_url": issue_url,
        "body": body,
    }
    if event == "resolved":
        response = github_client("close", payload)
        return PublicIngressNotificationDelivery(
            delivery_status="delivered",
            action="closed_issue",
            external_url=str(response.get("url", issue_url)).strip(),
            external_id=str(response.get("id", "")).strip(),
        )
    response = github_client("comment", payload)
    return PublicIngressNotificationDelivery(
        delivery_status="delivered",
        action="commented_issue",
        external_url=str(response.get("url", issue_url)).strip(),
        external_id=str(response.get("id", "")).strip(),
    )


def _github_notification_action_name(action: str) -> str:
    if action == "create":
        return "created_issue"
    if action == "close":
        return "closed_issue"
    return "commented_issue"


def _delivered_public_ingress_outbox_delivery(
    *,
    record: OutboxDeliveryRecord,
    payload: dict[str, object],
    attempt_payload: dict[str, object],
    action: str,
    marker: str,
    response: dict[str, object],
) -> OutboxDeliveryRecord:
    attempt = PublicIngressNotificationAttemptRecord(
        attempt_id=str(attempt_payload["attempt_id"]),
        incident_id=str(attempt_payload["incident_id"]),
        event=_public_ingress_event_value(attempt_payload.get("event")),
        policy_id=str(attempt_payload["policy_id"]),
        destination_id=str(attempt_payload["destination_id"]),
        destination_kind="github_issue",
        delivery_status="delivered",
        attempted_at=str(attempt_payload["attempted_at"]),
        observation_id=str(attempt_payload["observation_id"]),
        incident_event_id=str(attempt_payload.get("incident_event_id") or ""),
        external_url=str(response.get("url", "")).strip(),
        external_id=str(response.get("id", "")).strip() or marker,
        action=_github_notification_action_name(action),
    )
    return record.model_copy(
        update={
            "state": "delivered",
            "provider_id": "github",
            "external_id": attempt.external_id,
            "external_url": attempt.external_url,
            "action": attempt.action,
            "error_code": "",
            "lease_owner": "",
            "lease_expires_at": "",
            "payload": {**payload, "attempt_result": attempt.model_dump(mode="json")},
        }
    )


def _skipped_public_ingress_outbox_delivery(
    *,
    record: OutboxDeliveryRecord,
    payload: dict[str, object],
    attempt_payload: dict[str, object],
    marker: str,
) -> OutboxDeliveryRecord:
    attempt = PublicIngressNotificationAttemptRecord(
        attempt_id=str(attempt_payload["attempt_id"]),
        incident_id=str(attempt_payload["incident_id"]),
        event=_public_ingress_event_value(attempt_payload.get("event")),
        policy_id=str(attempt_payload["policy_id"]),
        destination_id=str(attempt_payload["destination_id"]),
        destination_kind="github_issue",
        delivery_status="skipped",
        attempted_at=str(attempt_payload["attempted_at"]),
        observation_id=str(attempt_payload["observation_id"]),
        incident_event_id=str(attempt_payload.get("incident_event_id") or ""),
        external_id=marker,
        action="no_prior_issue",
    )
    return record.model_copy(
        update={
            "state": "delivered",
            "provider_id": "github",
            "external_id": marker,
            "external_url": "",
            "action": "skipped_no_prior_issue",
            "error_code": "",
            "lease_owner": "",
            "lease_expires_at": "",
            "payload": {**payload, "attempt_result": attempt.model_dump(mode="json")},
        }
    )


def _github_notification_destination_payload(
    destination: PublicIngressNotificationDestination,
) -> dict[str, object]:
    return {
        "destination_id": destination.destination_id,
        "kind": destination.kind,
        "status": destination.status,
        "github_repository": destination.github_repository,
        "github_issue_number": destination.github_issue_number,
        "github_label": destination.github_label,
    }


def _dict_payload(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("outbox public ingress payload is malformed")
    return {str(key): item for key, item in value.items()}


def _required_payload_text(payload: dict[str, object], key: str) -> str:
    value = _optional_payload_text(payload, key)
    if not value:
        raise ValueError(f"outbox public ingress payload missing {key}")
    return value


def _optional_payload_text(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    return value.strip() if isinstance(value, str) else ""


def _public_ingress_provider_safe_error_code(error: Exception) -> str:
    if isinstance(error, (ValueError, KeyError)):
        return "invalid_outbox_payload"
    return "github_provider_error"


def _public_ingress_event_value(value: object) -> PublicIngressIncidentEvent:
    if value == "opened":
        return "opened"
    if value == "updated":
        return "updated"
    if value == "reminder":
        return "reminder"
    if value == "resolved":
        return "resolved"
    raise ValueError("outbox public ingress payload has invalid event")


def _deliver_email_notification(
    *,
    destination: PublicIngressNotificationDestination,
    event: PublicIngressIncidentEvent,
    incident: PublicIngressIncidentRecord,
    observation: PublicIngressObservationRecord,
    email_sender: Callable[[PublicIngressNotificationDestination, EmailMessage], object],
    secret_resolver: Callable[[str], str],
) -> PublicIngressNotificationDelivery:
    smtp_username = secret_resolver(destination.smtp_username_secret).strip()
    smtp_password = secret_resolver(destination.smtp_password_secret).strip()
    if not smtp_username or not smtp_password:
        return PublicIngressNotificationDelivery(
            delivery_status="failed",
            action="missing_smtp_secret",
            error_message="SMTP credential secrets could not be resolved.",
        )
    message = EmailMessage()
    message["From"] = destination.email_from
    message["To"] = ", ".join(destination.email_to)
    message["X-Launchplane-SMTP-Username"] = smtp_username
    message["X-Launchplane-SMTP-Password"] = smtp_password
    message["Subject"] = (
        f"[Launchplane] Public ingress {event}: {incident.product}/{incident.instance}"
    )
    message.set_content(
        public_ingress_incident_notification_body(
            event=event, incident=incident, observation=observation
        )
    )
    email_sender(destination, message)
    return PublicIngressNotificationDelivery(delivery_status="delivered", action="sent_email")


def _deliver_discord_notification(
    *,
    destination: PublicIngressNotificationDestination,
    event: PublicIngressIncidentEvent,
    incident: PublicIngressIncidentRecord,
    observation: PublicIngressObservationRecord,
    discord_sender: Callable[[str, dict[str, object]], object],
    secret_resolver: Callable[[str], str],
) -> PublicIngressNotificationDelivery:
    webhook_url = secret_resolver(destination.discord_webhook_secret).strip()
    if not webhook_url:
        return PublicIngressNotificationDelivery(
            delivery_status="failed",
            action="missing_discord_webhook",
            error_message="Discord webhook secret could not be resolved.",
        )
    public_url_error = public_discord_url_error(webhook_url)
    if public_url_error:
        return PublicIngressNotificationDelivery(
            delivery_status="failed",
            action="invalid_discord_webhook",
            error_message=f"Discord webhook URL is not public: {public_url_error}",
        )
    color = 0x2E7D32 if event == "resolved" else 0xC62828
    discord_sender(
        webhook_url,
        {
            "embeds": [
                {
                    "title": f"Public ingress {event}: {incident.product}/{incident.instance}",
                    "description": public_ingress_incident_notification_body(
                        event=event, incident=incident, observation=observation
                    ),
                    "color": color,
                }
            ]
        },
    )
    return PublicIngressNotificationDelivery(delivery_status="delivered", action="posted_discord")


def public_ingress_incident_notification_body(
    *,
    event: PublicIngressIncidentEvent,
    incident: PublicIngressIncidentRecord,
    observation: PublicIngressObservationRecord,
    marker: str = "",
) -> str:
    lines = [
        f"Launchplane public ingress incident {event}: {incident.product}/{incident.instance}",
        "",
        f"- status: {incident.status}",
        f"- incident_id: {incident.incident_id}",
        f"- context: {incident.context}",
        f"- observed_at: {observation.observed_at}",
        f"- observation_id: {observation.record_id}",
        f"- failure_code: {incident.failure_code}",
        f"- summary: {incident.summary}",
    ]
    if incident.status == "resolved":
        lines.append(f"- resolution_reason: {incident.resolution_reason}")
    for target in observation.targets:
        lines.append(f"- {target.target}: {target.status} {target.summary}")
    if marker.strip():
        lines.append("")
        lines.append(
            f"<!-- launchplane-public-ingress-incident:{_public_ingress_incident_marker(incident.incident_id)} -->"
        )
        lines.append(f"<!-- launchplane-public-ingress-notification:{marker.strip()} -->")
    return "\n".join(lines)


def _public_ingress_incident_marker(incident_id: str) -> str:
    return hashlib.sha256(incident_id.strip().encode("utf-8")).hexdigest()


def _latest_external_url(attempts: tuple[PublicIngressNotificationAttemptRecord, ...]) -> str:
    return next(
        (
            attempt.external_url
            for attempt in attempts
            if attempt.destination_kind == "github_issue" and attempt.external_url.strip()
        ),
        "",
    )


def _gh_issue_client(
    action: str, payload: dict[str, object], *, token: str | None = None
) -> dict[str, object]:
    resolved_token = _public_ingress_github_token(token)
    if action == "find_marker":
        return _find_github_issue_notification_marker(
            payload=payload,
            token=resolved_token,
        )
    if action == "ensure_closed":
        repository, issue_number = _github_issue_reference(payload)
        issue = _github_api_request(
            method="GET",
            path=f"/repos/{repository}/issues/{issue_number}",
            token=resolved_token,
        )
        if not isinstance(issue, dict):
            raise RuntimeError("GitHub issue lookup response must be a JSON object.")
        if str(issue.get("state") or "").strip().lower() != "closed":
            issue = _github_api_request(
                method="PATCH",
                path=f"/repos/{repository}/issues/{issue_number}",
                token=resolved_token,
                body={"state": "closed", "state_reason": "completed"},
            )
            if not isinstance(issue, dict):
                raise RuntimeError("GitHub issue close response must be a JSON object.")
        return {
            "url": str(issue.get("html_url") or issue.get("url") or "").strip(),
            "id": str(issue.get("node_id") or issue.get("id") or "").strip(),
        }
    if action == "create":
        repository = _github_repository_path(str(payload["repository"]))
        body: dict[str, object] = {
            "title": str(payload["title"]),
            "body": str(payload["body"]),
        }
        labels = payload.get("labels", [])
        if isinstance(labels, list):
            body["labels"] = [str(label) for label in labels]
        response = _github_api_request(
            method="POST",
            path=f"/repos/{repository}/issues",
            token=resolved_token,
            body=body,
        )
    elif action == "comment":
        repository, issue_number = _github_issue_reference(payload)
        response = _github_api_request(
            method="POST",
            path=f"/repos/{repository}/issues/{issue_number}/comments",
            token=resolved_token,
            body={"body": str(payload["body"])},
        )
    elif action == "close":
        repository, issue_number = _github_issue_reference(payload)
        _github_api_request(
            method="POST",
            path=f"/repos/{repository}/issues/{issue_number}/comments",
            token=resolved_token,
            body={"body": str(payload["body"])},
        )
        response = _github_api_request(
            method="PATCH",
            path=f"/repos/{repository}/issues/{issue_number}",
            token=resolved_token,
            body={"state": "closed", "state_reason": "completed"},
        )
    else:
        raise ValueError(f"unsupported GitHub notification action: {action}")
    if not isinstance(response, dict):
        raise RuntimeError("GitHub issue notification response must be a JSON object.")
    return {
        "url": str(response.get("html_url") or response.get("url") or "").strip(),
        "id": str(response.get("node_id") or response.get("id") or "").strip(),
    }


def _find_github_issue_notification_marker(
    *, payload: dict[str, object], token: str
) -> dict[str, object]:
    repository = _github_repository_path(str(payload["repository"]))
    marker = str(payload.get("marker", "")).strip()
    if not marker:
        raise ValueError("GitHub issue marker lookup requires marker.")
    issue_number = payload.get("issue_number")
    issue_url = str(payload.get("issue_url", "")).strip()
    if issue_number is not None or issue_url:
        issue_repository, resolved_issue_number = _github_issue_reference(payload)
        if issue_repository != repository:
            raise ValueError("GitHub issue marker repository must match notification repository.")
        issue = _github_api_request(
            method="GET",
            path=f"/repos/{repository}/issues/{resolved_issue_number}",
            token=token,
        )
        if isinstance(issue, dict) and _github_payload_contains_marker(issue, marker):
            return _github_issue_marker_response(issue)
        comments = _github_api_request(
            method="GET",
            path=f"/repos/{repository}/issues/{resolved_issue_number}/comments?per_page=100",
            token=token,
        )
        if isinstance(comments, list):
            for comment in comments:
                if isinstance(comment, dict) and _github_payload_contains_marker(comment, marker):
                    return _github_issue_marker_response(comment)
        return {}
    query = quote(f'repo:{repository} type:issue in:body "{marker}"', safe="")
    search = _github_api_request(
        method="GET",
        path=f"/search/issues?q={query}&per_page=10",
        token=token,
    )
    if isinstance(search, dict):
        items = search.get("items")
        if isinstance(items, list):
            for issue in items:
                if isinstance(issue, dict) and _github_payload_contains_marker(issue, marker):
                    return _github_issue_marker_response(issue)
    return {}


def _github_payload_contains_marker(payload: dict[str, object], marker: str) -> bool:
    body = payload.get("body")
    return isinstance(body, str) and marker in body


def _github_issue_marker_response(payload: dict[str, object]) -> dict[str, object]:
    return {
        "url": str(payload.get("html_url") or payload.get("url") or "").strip(),
        "id": str(payload.get("node_id") or payload.get("id") or "").strip(),
    }


def _public_ingress_github_token(token: str | None = None) -> str:
    resolved = (
        token if token is not None else os.environ.get(PUBLIC_INGRESS_GITHUB_TOKEN_ENV_KEY, "")
    ).strip()
    if not resolved:
        raise RuntimeError(
            f"{PUBLIC_INGRESS_GITHUB_TOKEN_ENV_KEY} is required for public ingress "
            "GitHub notifications. Configure a managed automation token; "
            "Launchplane does not fall back to active local gh auth."
        )
    return resolved


def _github_api_request(
    *, method: str, path: str, token: str, body: dict[str, object] | None = None
) -> object:
    request_body = None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if body is not None:
        request_body = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(
        url=f"https://api.github.com{path}",
        method=method,
        headers=headers,
        data=request_body,
    )
    try:
        with urlopen(request, timeout=15) as response:
            response_text = response.read().decode("utf-8")
            return json.loads(response_text) if response_text.strip() else None
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace").strip()
        message = detail or str(error)
        raise RuntimeError(f"GitHub API request failed for {path}: {message}") from error
    except (URLError, OSError) as error:
        raise RuntimeError(f"GitHub API request failed for {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise RuntimeError(f"GitHub API response for {path} was not valid JSON.") from error


def _github_issue_reference(payload: dict[str, object]) -> tuple[str, int]:
    repository = _github_repository_path(str(payload["repository"]))
    issue_number = payload.get("issue_number")
    if issue_number is not None:
        return repository, _github_issue_number(issue_number)
    issue_url = str(payload.get("issue_url", "")).strip()
    if not issue_url:
        raise ValueError("GitHub issue notification requires issue_url or issue_number.")
    url_repository, url_issue_number = _github_issue_url_reference(issue_url)
    if url_repository != repository:
        raise ValueError("GitHub issue URL repository must match notification repository.")
    return repository, url_issue_number


def _github_issue_url_reference(issue_url: str) -> tuple[str, int]:
    parsed = urlsplit(issue_url.strip())
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 4 or parts[2] != "issues":
        raise ValueError("GitHub issue URL must use /owner/repo/issues/number.")
    repository = _github_repository_path(f"{parts[0]}/{parts[1]}")
    return repository, _github_issue_number(parts[3])


def _github_repository_path(repository: str) -> str:
    parts = [part.strip() for part in repository.strip().split("/") if part.strip()]
    if len(parts) != 2:
        raise ValueError("GitHub repository must use owner/name.")
    return f"{parts[0]}/{parts[1]}"


def _github_issue_number(value: object) -> int:
    try:
        issue_number = int(str(value))
    except ValueError as error:
        raise ValueError("GitHub issue number must be an integer.") from error
    if issue_number <= 0:
        raise ValueError("GitHub issue number must be positive.")
    return issue_number


def _send_email_message(
    destination: PublicIngressNotificationDestination, message: EmailMessage
) -> None:
    with smtplib.SMTP(destination.smtp_host, destination.smtp_port, timeout=15) as smtp:
        smtp.starttls()
        username = message["X-Launchplane-SMTP-Username"]
        password = message["X-Launchplane-SMTP-Password"]
        if not username or not password:
            raise RuntimeError("SMTP credentials were not resolved.")
        smtp.login(str(username), str(password))
        del message["X-Launchplane-SMTP-Username"]
        del message["X-Launchplane-SMTP-Password"]
        smtp.send_message(message)


def _post_discord_webhook(webhook_url: str, payload: dict[str, object]) -> None:
    post_discord_webhook(webhook_url, payload)
