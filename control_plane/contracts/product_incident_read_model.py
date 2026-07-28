from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field

from control_plane.contracts.data_provenance import DataProvenance, FreshnessStatus
from control_plane.contracts.outbox_delivery import OutboxDeliveryRecord, OutboxDeliveryState
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
)
from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressCheckKind,
    PublicIngressFailureCode,
    PublicIngressFailureLayer,
    PublicIngressIncidentEventKind,
    PublicIngressIncidentEventReason,
    PublicIngressIncidentEventRecord,
    PublicIngressIncidentEventSuppressionReason,
    PublicIngressIncidentMaterialFingerprint,
    PublicIngressIncidentNotificationState,
    PublicIngressIncidentRecord,
    PublicIngressIncidentReminderStateRecord,
    PublicIngressIncidentReminderStatus,
    PublicIngressIncidentResolutionReason,
    PublicIngressIncidentSeverity,
    PublicIngressIncidentStatus,
    PublicIngressNotificationAttemptRecord,
    PublicIngressNotificationDeliveryStatus,
    PublicIngressNotificationDestinationKind,
    PublicIngressObservationPurpose,
    PublicIngressObservationRecord,
    PublicIngressObservationStatus,
    PublicIngressTargetKind,
    PublicIngressTlsStatus,
)


ProductIncidentStatusFilter = Literal["all", "open", "resolved"]


@dataclass(frozen=True)
class ProductIncidentEnvironmentScope:
    product: str
    display_name: str
    environment: str
    context: str
    instance: str
    recorded_at: str


class ProductIncidentReadStore(Protocol):
    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord: ...

    def read_public_ingress_incident_record(
        self, incident_id: str
    ) -> PublicIngressIncidentRecord: ...

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

    def list_public_ingress_observation_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        check_name: str = "",
        check_kind: str = "",
        incident_id: str = "",
        limit: int | None = None,
    ) -> tuple[PublicIngressObservationRecord, ...]: ...

    def list_public_ingress_incident_event_records(
        self,
        *,
        incident_id: str = "",
        event: str = "",
        limit: int | None = None,
    ) -> tuple[PublicIngressIncidentEventRecord, ...]: ...

    def list_public_ingress_incident_reminder_state_records(
        self,
        *,
        incident_id: str = "",
        policy_id: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[PublicIngressIncidentReminderStateRecord, ...]: ...

    def list_public_ingress_notification_attempt_records(
        self,
        *,
        incident_id: str = "",
        event: str = "",
        destination_kind: str = "",
        limit: int | None = None,
    ) -> tuple[PublicIngressNotificationAttemptRecord, ...]: ...

    def list_outbox_delivery_records(
        self,
        *,
        states: tuple[str, ...] = (),
        kind: str = "",
        aggregate_type: str = "",
        aggregate_id: str = "",
        limit: int | None = None,
    ) -> tuple[OutboxDeliveryRecord, ...]: ...


class ProductIncidentReadModelCapabilityError(RuntimeError):
    pass


class ProductIncidentMaterialEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_kind: PublicIngressCheckKind
    failure_code: PublicIngressFailureCode
    failure_layer: PublicIngressFailureLayer
    severity: PublicIngressIncidentSeverity
    affected_targets: tuple[PublicIngressTargetKind, ...] = ()
    route_authority_kind: str = ""
    runtime_identity_status: str = ""
    runtime_identity_mismatched_fields: tuple[str, ...] = ()
    tls_status: PublicIngressTlsStatus | Literal[""] = ""


class ProductIncidentSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    incident_id: str
    product: str
    display_name: str
    environment: str
    context: str
    instance: str
    check_name: str
    check_kind: PublicIngressCheckKind
    status: PublicIngressIncidentStatus
    severity: PublicIngressIncidentSeverity
    failure_code: PublicIngressFailureCode
    failure_layer: PublicIngressFailureLayer
    summary: str
    opened_at: str
    latest_observed_at: str
    resolved_at: str = ""
    resolution_reason: PublicIngressIncidentResolutionReason | Literal[""] = ""
    notification_state: PublicIngressIncidentNotificationState
    notification_state_changed_at: str = ""
    silenced_until: str = ""
    latest_material_event_id: str = ""
    latest_material_event: PublicIngressIncidentEventKind | Literal[""] = ""
    latest_material_event_at: str = ""
    material_fingerprint_sha256: str = ""
    recovery_observation_threshold: int = Field(ge=1)
    consecutive_recovery_observations: int = Field(ge=0)
    next_reminder_at: str = ""
    last_reminded_at: str = ""
    trust_state: FreshnessStatus = "recorded"
    provenance: DataProvenance


class ProductIncidentObservationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: str
    purpose: PublicIngressObservationPurpose
    observed_at: str
    status: PublicIngressObservationStatus
    failure_code: PublicIngressFailureCode | Literal[""] = ""
    summary: str
    incident_event_id: str = ""
    material_fingerprint_sha256: str = ""
    notification_sent: bool = False


class ProductIncidentEventSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    event: PublicIngressIncidentEventKind
    reason: PublicIngressIncidentEventReason
    occurred_at: str
    observation_id: str
    severity: PublicIngressIncidentSeverity
    material_fingerprint_sha256: str
    previous_material_fingerprint_sha256: str = ""
    delivery_eligible: bool
    suppression_reason: PublicIngressIncidentEventSuppressionReason = ""
    reminder_window_index: int | None = None
    reminder_window_started_at: str = ""
    reminder_window_ended_at: str = ""
    summary: str


class ProductIncidentReminderSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reminder_state_id: str
    status: PublicIngressIncidentReminderStatus
    material_event_id: str
    interval_seconds: int
    last_window_index: int
    last_reminded_at: str = ""
    next_reminder_at: str = ""
    updated_at: str


class ProductIncidentNotificationAttemptSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt_id: str
    event: str
    destination_kind: PublicIngressNotificationDestinationKind
    delivery_status: PublicIngressNotificationDeliveryStatus
    attempted_at: str
    observation_id: str
    incident_event_id: str = ""
    external_url: str = ""
    action: str = ""
    error_message: str = ""


class ProductIncidentOutboxDeliverySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    delivery_id: str
    state: OutboxDeliveryState
    created_at: str
    updated_at: str
    next_attempt_at: str
    attempt: int
    max_attempts: int
    external_url: str = ""
    action: str = ""
    error_code: str = ""


class ProductEnvironmentIncidentList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    display_name: str
    environment: str
    context: str
    instance: str
    incidents: tuple[ProductIncidentSummary, ...] = ()
    trust_state: FreshnessStatus = "recorded"
    provenance: DataProvenance


class ProductIncidentDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident: ProductIncidentSummary
    material_evidence: ProductIncidentMaterialEvidence | None = None
    observations: tuple[ProductIncidentObservationSummary, ...] = ()
    events: tuple[ProductIncidentEventSummary, ...] = ()
    reminders: tuple[ProductIncidentReminderSummary, ...] = ()
    notification_attempts: tuple[ProductIncidentNotificationAttemptSummary, ...] = ()
    outbox_deliveries: tuple[ProductIncidentOutboxDeliverySummary, ...] = ()


_PRODUCT_INCIDENT_READ_STORE_METHODS = (
    "read_product_profile_record",
    "read_public_ingress_incident_record",
    "list_public_ingress_incident_records",
    "list_public_ingress_observation_records",
    "list_public_ingress_incident_event_records",
    "list_public_ingress_incident_reminder_state_records",
    "list_public_ingress_notification_attempt_records",
    "list_outbox_delivery_records",
)


def require_product_incident_read_store(record_store: object) -> ProductIncidentReadStore:
    missing_methods = tuple(
        method_name
        for method_name in _PRODUCT_INCIDENT_READ_STORE_METHODS
        if not callable(getattr(record_store, method_name, None))
    )
    if missing_methods:
        missing = ", ".join(missing_methods)
        raise ProductIncidentReadModelCapabilityError(
            "Product incident reads require DB-backed Launchplane storage; "
            f"missing store method(s): {missing}."
        )
    return cast(ProductIncidentReadStore, record_store)


def build_product_environment_incident_list(
    *,
    record_store: ProductIncidentReadStore,
    product: str,
    environment: str,
    status: ProductIncidentStatusFilter = "all",
    limit: int = 20,
    scope: ProductIncidentEnvironmentScope | None = None,
) -> ProductEnvironmentIncidentList:
    incident_scope = _resolve_scope(
        record_store=record_store,
        product=product,
        environment=environment,
        scope=scope,
    )
    if status == "all":
        open_incidents = record_store.list_public_ingress_incident_records(
            product=product,
            context_name=incident_scope.context,
            instance_name=incident_scope.instance,
            status="open",
            limit=None,
        )
        resolved_incidents = record_store.list_public_ingress_incident_records(
            product=product,
            context_name=incident_scope.context,
            instance_name=incident_scope.instance,
            status="resolved",
            limit=limit,
        )
        incidents = (*_sort_open_incidents(open_incidents), *resolved_incidents)[:limit]
    else:
        records = record_store.list_public_ingress_incident_records(
            product=product,
            context_name=incident_scope.context,
            instance_name=incident_scope.instance,
            status=status,
            limit=None if status == "open" else limit,
        )
        incidents = _sort_open_incidents(records)[:limit] if status == "open" else records
    summaries = tuple(
        _incident_summary(
            record_store=record_store,
            scope=incident_scope,
            incident=incident,
        )
        for incident in incidents
    )
    refreshed_at = max(
        (incident.latest_observed_at for incident in incidents),
        default=incident_scope.recorded_at,
    )
    return ProductEnvironmentIncidentList(
        product=product,
        display_name=incident_scope.display_name,
        environment=incident_scope.environment,
        context=incident_scope.context,
        instance=incident_scope.instance,
        incidents=summaries,
        provenance=DataProvenance(
            source_kind="record",
            source_record_id=summaries[0].incident_id if summaries else incident_scope.product,
            recorded_at=refreshed_at,
            refreshed_at=refreshed_at,
            freshness_status="recorded",
            detail=(
                "Launchplane public ingress incident records for this environment."
                if summaries
                else "Launchplane recorded no public ingress incidents for this environment."
            ),
        ),
    )


def build_product_environment_incident_detail(
    *,
    record_store: ProductIncidentReadStore,
    product: str,
    environment: str,
    incident_id: str,
    observation_limit: int = 100,
    scope: ProductIncidentEnvironmentScope | None = None,
) -> ProductIncidentDetail:
    incident_scope = _resolve_scope(
        record_store=record_store,
        product=product,
        environment=environment,
        scope=scope,
    )
    try:
        incident = record_store.read_public_ingress_incident_record(incident_id)
    except FileNotFoundError as error:
        raise FileNotFoundError(
            f"Public ingress incident '{incident_id}' was not found."
        ) from error
    if (
        incident.product != product
        or incident.context != incident_scope.context
        or incident.instance != incident_scope.instance
    ):
        raise FileNotFoundError(f"Public ingress incident '{incident_id}' was not found.")
    observations = record_store.list_public_ingress_observation_records(
        incident_id=incident_id,
        limit=observation_limit,
    )
    events = record_store.list_public_ingress_incident_event_records(
        incident_id=incident_id,
        limit=200,
    )
    reminders = record_store.list_public_ingress_incident_reminder_state_records(
        incident_id=incident_id,
        limit=100,
    )
    attempts = record_store.list_public_ingress_notification_attempt_records(
        incident_id=incident_id,
        limit=200,
    )
    outbox_deliveries = record_store.list_outbox_delivery_records(
        kind="public_ingress_notification",
        aggregate_type="public_ingress_incident",
        aggregate_id=incident_id,
        limit=200,
    )
    return ProductIncidentDetail(
        incident=_incident_summary(
            record_store=record_store,
            scope=incident_scope,
            incident=incident,
        ),
        material_evidence=_material_evidence(incident.material_fingerprint),
        observations=tuple(
            ProductIncidentObservationSummary(
                record_id=record.record_id,
                purpose=record.purpose,
                observed_at=record.observed_at,
                status=record.status,
                failure_code=record.failure_code or "",
                summary=_observation_summary(record),
                incident_event_id=record.incident_event_id,
                material_fingerprint_sha256=record.material_fingerprint_sha256,
                notification_sent=record.notification_sent,
            )
            for record in observations
        ),
        events=tuple(
            ProductIncidentEventSummary(
                event_id=record.event_id,
                event=record.event,
                reason=record.reason,
                occurred_at=record.occurred_at,
                observation_id=record.observation_id,
                severity=record.severity,
                material_fingerprint_sha256=record.material_fingerprint_sha256,
                previous_material_fingerprint_sha256=(record.previous_material_fingerprint_sha256),
                delivery_eligible=record.delivery_eligible,
                suppression_reason=record.suppression_reason,
                reminder_window_index=record.reminder_window_index,
                reminder_window_started_at=record.reminder_window_started_at,
                reminder_window_ended_at=record.reminder_window_ended_at,
                summary=_event_summary(record),
            )
            for record in events
        ),
        reminders=tuple(
            ProductIncidentReminderSummary(
                reminder_state_id=record.reminder_state_id,
                status=record.status,
                material_event_id=record.material_event_id,
                interval_seconds=record.interval_seconds,
                last_window_index=record.last_window_index,
                last_reminded_at=record.last_reminded_at,
                next_reminder_at=(record.next_reminder_at if record.status == "active" else ""),
                updated_at=record.updated_at,
            )
            for record in reminders
        ),
        notification_attempts=tuple(
            ProductIncidentNotificationAttemptSummary(
                attempt_id=record.attempt_id,
                event=record.event,
                destination_kind=record.destination_kind,
                delivery_status=record.delivery_status,
                attempted_at=record.attempted_at,
                observation_id=record.observation_id,
                incident_event_id=record.incident_event_id,
                external_url=record.external_url,
                action=record.action,
                error_message=(
                    "Delivery failed."
                    if record.delivery_status == "failed" and record.error_message
                    else ""
                ),
            )
            for record in attempts
        ),
        outbox_deliveries=tuple(
            ProductIncidentOutboxDeliverySummary(
                delivery_id=record.delivery_id,
                state=record.state,
                created_at=record.created_at,
                updated_at=record.updated_at,
                next_attempt_at=record.next_attempt_at,
                attempt=record.attempt,
                max_attempts=record.max_attempts,
                external_url=record.external_url,
                action=record.action,
                error_code=record.error_code,
            )
            for record in sorted(
                outbox_deliveries,
                key=lambda delivery: (delivery.created_at, delivery.delivery_id),
                reverse=True,
            )
        ),
    )


def _resolve_scope(
    *,
    record_store: ProductIncidentReadStore,
    product: str,
    environment: str,
    scope: ProductIncidentEnvironmentScope | None,
) -> ProductIncidentEnvironmentScope:
    if scope is not None:
        if scope.product != product or scope.environment != environment:
            raise ValueError(
                "The authorized incident scope does not match the requested environment."
            )
        return scope
    try:
        profile = record_store.read_product_profile_record(product)
        lane = next(candidate for candidate in profile.lanes if candidate.instance == environment)
    except (FileNotFoundError, StopIteration) as error:
        raise FileNotFoundError(
            f"Product '{product}' has no environment '{environment}'."
        ) from error
    return ProductIncidentEnvironmentScope(
        product=profile.product,
        display_name=profile.display_name,
        environment=lane.instance,
        context=lane.context,
        instance=lane.instance,
        recorded_at=profile.updated_at,
    )


def _incident_summary(
    *,
    record_store: ProductIncidentReadStore,
    scope: ProductIncidentEnvironmentScope,
    incident: PublicIngressIncidentRecord,
) -> ProductIncidentSummary:
    reminder_states = record_store.list_public_ingress_incident_reminder_state_records(
        incident_id=incident.incident_id,
        limit=100,
    )
    next_reminder_at = min(
        (
            state.next_reminder_at
            for state in reminder_states
            if state.status == "active" and state.next_reminder_at
        ),
        default="",
    )
    last_reminded_at = max(
        (state.last_reminded_at for state in reminder_states if state.last_reminded_at),
        default="",
    )
    failure_layer: PublicIngressFailureLayer = "unknown"
    if incident.material_fingerprint is not None:
        failure_layer = incident.material_fingerprint.failure_layer
    return ProductIncidentSummary(
        incident_id=incident.incident_id,
        product=incident.product,
        display_name=scope.display_name,
        environment=scope.environment,
        context=incident.context,
        instance=incident.instance,
        check_name=incident.check_name,
        check_kind=incident.check_kind,
        status=incident.status,
        severity=incident.severity,
        failure_code=incident.failure_code,
        failure_layer=failure_layer,
        summary=_incident_summary_text(incident),
        opened_at=incident.opened_at,
        latest_observed_at=incident.latest_observed_at,
        resolved_at=incident.resolved_at,
        resolution_reason=incident.resolution_reason,
        notification_state=incident.notification_state,
        notification_state_changed_at=incident.notification_state_changed_at,
        silenced_until=incident.silenced_until,
        latest_material_event_id=incident.latest_material_event_id,
        latest_material_event=incident.latest_material_event,
        latest_material_event_at=incident.latest_material_event_at,
        material_fingerprint_sha256=incident.material_fingerprint_sha256,
        recovery_observation_threshold=incident.recovery_observation_threshold,
        consecutive_recovery_observations=incident.consecutive_recovery_observations,
        next_reminder_at=next_reminder_at,
        last_reminded_at=last_reminded_at,
        provenance=DataProvenance(
            source_kind="record",
            source_record_id=incident.incident_id,
            recorded_at=incident.opened_at,
            refreshed_at=incident.latest_observed_at,
            freshness_status="recorded",
            detail="Launchplane public ingress incident occurrence.",
        ),
    )


def _incident_summary_text(incident: PublicIngressIncidentRecord) -> str:
    if incident.status == "resolved":
        return (
            "Launchplane resolved the incident with reason "
            f"'{incident.resolution_reason or 'not_recorded'}'."
        )
    failure_layer = (
        incident.material_fingerprint.failure_layer
        if incident.material_fingerprint is not None
        else "unknown"
    )
    return (
        f"Launchplane recorded a {incident.severity} incident at the {failure_layer} layer "
        f"with failure code '{incident.failure_code}'."
    )


def _observation_summary(record: PublicIngressObservationRecord) -> str:
    if record.status == "pass":
        return f"Launchplane recorded a passing {record.purpose} observation."
    if record.status == "skipped":
        return f"Launchplane recorded a skipped {record.purpose} observation."
    return (
        f"Launchplane recorded a failing {record.purpose} observation with failure code "
        f"'{record.failure_code or 'not_recorded'}'."
    )


def _event_summary(record: PublicIngressIncidentEventRecord) -> str:
    return (
        f"Launchplane recorded the '{record.event}' incident event with reason '{record.reason}'."
    )


def _material_evidence(
    fingerprint: PublicIngressIncidentMaterialFingerprint | None,
) -> ProductIncidentMaterialEvidence | None:
    if fingerprint is None:
        return None
    runtime_identity_status = ""
    mismatched_fields: tuple[str, ...] = ()
    if fingerprint.runtime_mismatch is not None:
        runtime_identity_status = fingerprint.runtime_mismatch.status
        mismatched_fields = fingerprint.runtime_mismatch.mismatched_fields
    return ProductIncidentMaterialEvidence(
        check_kind=fingerprint.check_kind,
        failure_code=fingerprint.failure_code,
        failure_layer=fingerprint.failure_layer,
        severity=fingerprint.severity,
        affected_targets=fingerprint.affected_targets,
        route_authority_kind=fingerprint.route_authority.kind,
        runtime_identity_status=runtime_identity_status,
        runtime_identity_mismatched_fields=mismatched_fields,
        tls_status=fingerprint.tls_status,
    )


def _sort_open_incidents(
    incidents: tuple[PublicIngressIncidentRecord, ...],
) -> tuple[PublicIngressIncidentRecord, ...]:
    return tuple(
        sorted(
            incidents,
            key=lambda incident: (
                incident.severity == "critical",
                incident.opened_at,
                incident.incident_id,
            ),
            reverse=True,
        )
    )
