from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import Request, urlopen
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
)
from control_plane.contracts.product_health_monitoring_migration import (
    canonical_health_check_record_token,
)
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneHealthCheck,
    ProductLaneHealthCheckKind,
    ProductLaneProfile,
)
from control_plane.contracts.private_health_endpoint_record import PrivateHealthEndpointRecord
from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressFailureCode,
    PublicIngressIncidentEvent,
    PublicIngressIncidentRecord,
    PublicIngressNotificationAttemptRecord,
    PublicIngressNotificationDeliveryStatus,
    PublicIngressNotificationDestination,
    PublicIngressNotificationPolicyRecord,
    PublicIngressObservationRecord,
    PublicIngressObservationStatus,
    PublicIngressTargetKind,
    PublicIngressTargetObservation,
    build_public_ingress_lane_incident_id,
    build_public_ingress_notification_attempt_id,
    build_public_ingress_observation_id,
)
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

    def read_private_health_endpoint_record(
        self, endpoint_key: str
    ) -> PrivateHealthEndpointRecord: ...


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
    check_kind: ProductLaneHealthCheckKind
    expected_runtime_identity: RuntimeIdentity | None
    require_runtime_identity: bool
    provider: str = ""
    provider_check: str = ""
    private_endpoint_key: str = ""
    resolution_failure_code: PublicIngressFailureCode | None = None
    resolution_failure_summary: str = ""


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
    open_incident_count: int = Field(default=0, ge=0)
    resolved_incident_count: int = Field(default=0, ge=0)
    delivery_attempt_count: int = Field(default=0, ge=0)
    records: tuple[PublicIngressObservationRecord, ...] = ()
    incidents: tuple[PublicIngressIncidentRecord, ...] = ()
    delivery_attempts: tuple[PublicIngressNotificationAttemptRecord, ...] = ()


HttpGet = Callable[[str, int], HttpObservation]


def discover_public_ingress_monitor_targets(
    record_store: PublicIngressMonitorStore,
) -> tuple[PublicIngressMonitorTarget, ...]:
    targets: list[PublicIngressMonitorTarget] = []
    for profile in record_store.list_product_profile_records():
        for lane in profile.lanes:
            for check in lane.health_monitoring.checks:
                if not check.enabled:
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
    if check.kind == "public_http":
        base_url = lane.base_url.strip().rstrip("/")
        health_url = check.url.strip() or _monitor_health_url(
            profile=profile, lane=lane, base_url=base_url
        )
        if not (base_url or health_url):
            return None
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
        expected_runtime_identity=_expected_runtime_identity(
            record_store=record_store,
            lane=lane,
        ),
        require_runtime_identity=check.require_runtime_identity,
        provider=check.provider,
        provider_check=check.provider_check,
        private_endpoint_key=check.private_endpoint_key,
        resolution_failure_code=resolution_failure_code,
        resolution_failure_summary=resolution_failure_summary,
    )


@dataclass(frozen=True)
class PrivateHealthEndpointResolution:
    url: str = ""
    failure_code: PublicIngressFailureCode | None = None
    summary: str = ""


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
        )
    if not isinstance(endpoint, PrivateHealthEndpointRecord):
        endpoint = PrivateHealthEndpointRecord.model_validate(endpoint)
    if endpoint.status != "active":
        return PrivateHealthEndpointResolution(
            failure_code="private_endpoint_disabled",
            summary=f"Private health endpoint {endpoint_key!r} is {endpoint.status}.",
        )
    if endpoint.product != profile.product:
        return PrivateHealthEndpointResolution(
            failure_code="private_endpoint_mismatch",
            summary=f"Private health endpoint {endpoint_key!r} belongs to another product.",
        )
    if endpoint.context != lane.context or endpoint.instance != lane.instance:
        return PrivateHealthEndpointResolution(
            failure_code="private_endpoint_mismatch",
            summary=f"Private health endpoint {endpoint_key!r} belongs to another lane.",
        )
    return PrivateHealthEndpointResolution(url=endpoint.url)


def run_public_ingress_monitor_once(
    *,
    record_store: PublicIngressMonitorStore,
    checked_at: str = "",
    timeout_seconds: int = 10,
    notify: bool = True,
    http_get: HttpGet | None = None,
    private_http_get: HttpGet | None = None,
    notification_drivers: PublicIngressNotificationDrivers | None = None,
) -> PublicIngressMonitorResult:
    observed_at = checked_at.strip() or utc_now_timestamp()
    public_get = http_get or fetch_public_ingress_url
    private_get = private_http_get or fetch_private_health_url
    records: list[PublicIngressObservationRecord] = []
    incidents: list[PublicIngressIncidentRecord] = []
    delivery_attempts: list[PublicIngressNotificationAttemptRecord] = []
    open_incident_count = 0
    resolved_incident_count = 0
    for target in discover_public_ingress_monitor_targets(record_store):
        previous_record = _latest_observation(record_store=record_store, target=target)
        record = check_public_ingress_target(
            target=target,
            checked_at=observed_at,
            timeout_seconds=timeout_seconds,
            http_get=(private_get if target.check_kind == "private_http" else public_get),
        )
        records.append(record)
        incident_records = reconcile_public_ingress_incident(
            record_store=record_store,
            record=record,
            previous_record=previous_record,
            write_records=False,
        )
        outbox_deliveries = (
            _public_ingress_notification_outbox_deliveries(
                record_store=record_store,
                incident_records=incident_records,
                observation=record,
                previous_record=previous_record,
            )
            if notify
            else ()
        )
        transition_writer = getattr(
            record_store, "write_public_ingress_transition_with_outbox", None
        )
        if callable(transition_writer) and outbox_deliveries:
            transition_writer(
                observation=record,
                incidents=incident_records,
                outbox_deliveries=outbox_deliveries,
            )
        else:
            record_store.write_public_ingress_observation_record(record)
            for incident in incident_records:
                record_store.write_public_ingress_incident_record(incident)
        incidents.extend(incident_records)
        open_incident_count += sum(1 for incident in incident_records if incident.status == "open")
        resolved_incident_count += sum(
            1 for incident in incident_records if incident.status == "resolved"
        )
        for incident in incident_records:
            if (
                notify
                and notification_drivers is not None
                and not (callable(transition_writer) and outbox_deliveries)
            ):
                delivery_attempts.extend(
                    deliver_public_ingress_incident_notifications(
                        record_store=record_store,
                        event=_incident_event(incident=incident, previous_record=previous_record),
                        incident=incident,
                        observation=record,
                        drivers=notification_drivers,
                    )
                )
    return PublicIngressMonitorResult(
        checked_at=observed_at,
        target_count=len(records),
        pass_count=sum(1 for record in records if record.status == "pass"),
        fail_count=sum(1 for record in records if record.status == "fail"),
        skipped_count=sum(1 for record in records if record.status == "skipped"),
        open_incident_count=open_incident_count,
        resolved_incident_count=resolved_incident_count,
        delivery_attempt_count=len(delivery_attempts),
        records=tuple(records),
        incidents=tuple(incidents),
        delivery_attempts=tuple(delivery_attempts),
    )


def deliver_public_ingress_incident_notifications(
    *,
    record_store: PublicIngressMonitorStore,
    event: PublicIngressIncidentEvent,
    incident: PublicIngressIncidentRecord,
    observation: PublicIngressObservationRecord,
    drivers: PublicIngressNotificationDrivers,
) -> tuple[PublicIngressNotificationAttemptRecord, ...]:
    attempts: list[PublicIngressNotificationAttemptRecord] = []
    for policy in _matching_notification_policies(record_store=record_store, incident=incident):
        for destination in policy.destinations:
            attempt_id = build_public_ingress_notification_attempt_id(
                incident_id=incident.incident_id,
                event=event,
                policy_id=policy.policy_id,
                destination_id=destination.destination_id,
                observation_id=observation.record_id,
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
    observation: PublicIngressObservationRecord,
    previous_record: PublicIngressObservationRecord | None,
) -> tuple[OutboxDeliveryRecord, ...]:
    deliveries: list[OutboxDeliveryRecord] = []
    for incident in incident_records:
        event = _incident_event(incident=incident, previous_record=previous_record)
        for policy in _matching_notification_policies(record_store=record_store, incident=incident):
            for destination in policy.destinations:
                if destination.kind != "github_issue" or destination.status != "enabled":
                    continue
                attempt_id = build_public_ingress_notification_attempt_id(
                    incident_id=incident.incident_id,
                    event=event,
                    policy_id=policy.policy_id,
                    destination_id=destination.destination_id,
                    observation_id=observation.record_id,
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
                created_at = observation.observed_at
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
                                "observation_id": observation.record_id,
                            },
                            "destination": _github_notification_destination_payload(destination),
                            "body": body,
                            "existing_issue_url": _latest_external_url(previous_attempts),
                            "marker": attempt_id,
                        },
                    )
                )
    return tuple(deliveries)


def reconcile_public_ingress_incident(
    *,
    record_store: PublicIngressMonitorStore,
    record: PublicIngressObservationRecord,
    previous_record: PublicIngressObservationRecord | None,
    write_records: bool = True,
) -> tuple[PublicIngressIncidentRecord, ...]:
    open_incidents = _open_incidents(record_store=record_store, record=record)
    if record.status == "fail":
        incident_id = build_public_ingress_lane_incident_id(
            product=record.product,
            context=record.context,
            instance=record.instance,
            check_name=record.check_name,
        )
        open_incident = next(
            (incident for incident in open_incidents if incident.incident_id == incident_id), None
        )
        if open_incident is not None:
            incident = open_incident.model_copy(
                update={
                    "latest_observation_id": record.record_id,
                    "latest_observed_at": record.observed_at,
                    "failure_code": record.failure_code,
                    "summary": record.summary,
                }
            )
        else:
            incident = PublicIngressIncidentRecord(
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
                summary=record.summary,
            )
        if write_records:
            record_store.write_public_ingress_incident_record(incident)
        return (incident,)
    if record.status == "pass" and open_incidents:
        resolved_incidents: list[PublicIngressIncidentRecord] = []
        for open_incident in open_incidents:
            incident = open_incident.model_copy(
                update={
                    "status": "resolved",
                    "latest_observation_id": record.record_id,
                    "latest_observed_at": record.observed_at,
                    "resolved_at": record.observed_at,
                    "resolved_observation_id": record.record_id,
                    "summary": record.summary,
                }
            )
            if write_records:
                record_store.write_public_ingress_incident_record(incident)
            resolved_incidents.append(incident)
        return tuple(resolved_incidents)
    return ()


def _incident_event(
    *, incident: PublicIngressIncidentRecord, previous_record: PublicIngressObservationRecord | None
) -> PublicIngressIncidentEvent:
    if incident.status == "resolved":
        return "resolved"
    if previous_record is not None and previous_record.status == "fail":
        return "updated"
    return "opened"


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
            )
        )
    status, failure_code = _record_status(target_observations)
    summary = _record_summary(target=target, status=status, observations=target_observations)
    return PublicIngressObservationRecord(
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
        observed_at=checked_at,
        status=status,
        failure_code=failure_code,
        base_url=target.base_url,
        health_url=target.health_url,
        expected_runtime_identity=target.expected_runtime_identity,
        targets=tuple(target_observations),
        summary=summary,
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


def _check_url(
    *,
    target_kind: PublicIngressTargetKind,
    url: str,
    timeout_seconds: int,
    target: PublicIngressMonitorTarget,
    http_get: HttpGet,
) -> PublicIngressTargetObservation:
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


def _latest_observation(
    *, record_store: PublicIngressMonitorStore, target: PublicIngressMonitorTarget
) -> PublicIngressObservationRecord | None:
    records = record_store.list_public_ingress_observation_records(
        product=target.product,
        context_name=target.context,
        instance_name=target.instance,
        check_name=target.check_name,
        check_kind=target.check_kind,
        limit=1,
    )
    return next(iter(records), None)


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
        return "skipped", "private_url"
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
    if target.check_kind == "private_http":
        return "Private health check"
    if target.check_kind == "provider":
        return "Provider health check"
    return "Public ingress"


def _successful_target_summary(target: PublicIngressMonitorTarget) -> str:
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
        action = "create" if not existing_issue_url else "comment"
        if attempt_payload.get("event") == "resolved" and existing_issue_url:
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
    except Exception as error:  # noqa: BLE001 - worker stores bounded provider-safe errors.
        return delivery_record.model_copy(
            update={
                "state": "failed",
                "error_code": _public_ingress_provider_safe_error_code(error),
                "lease_owner": "",
                "lease_expires_at": "",
            }
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
    for target in observation.targets:
        lines.append(f"- {target.target}: {target.status} {target.summary}")
    if marker.strip():
        lines.append("")
        lines.append(f"<!-- launchplane-public-ingress-notification:{marker.strip()} -->")
    return "\n".join(lines)


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
