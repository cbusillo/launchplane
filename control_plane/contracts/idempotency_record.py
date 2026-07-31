from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


LaunchplaneMutationReservationState = Literal[
    "running",
    "completed",
    "reconcile_required",
]


class LaunchplaneIdempotencyRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=2, ge=1)
    record_id: str
    scope: str
    route_path: str
    idempotency_key: str
    request_fingerprint: str
    state: LaunchplaneMutationReservationState = "completed"
    lease_owner: str = ""
    lease_expires_at: str = ""
    attempt: int = Field(default=1, ge=1)
    reconciliation_key: str = ""
    provider_target_key: str = ""
    provider_effect_phase: str = ""
    provider_effect_started_at: str = ""
    created_at: str = ""
    updated_at: str = ""
    response_status_code: int | None = Field(default=None, ge=100, le=599)
    response_trace_id: str = ""
    recorded_at: str = ""
    response_payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_record(self) -> "LaunchplaneIdempotencyRecord":
        self.record_id = _normalize_required(
            self.record_id,
            "Mutation reservation requires record_id.",
        )
        self.scope = _normalize_required(
            self.scope,
            "Mutation reservation requires scope.",
        )
        self.route_path = _normalize_required(
            self.route_path,
            "Mutation reservation requires route_path.",
        )
        self.idempotency_key = _normalize_required(
            self.idempotency_key,
            "Mutation reservation requires idempotency_key.",
        )
        self.request_fingerprint = _normalize_required(
            self.request_fingerprint,
            "Mutation reservation requires request_fingerprint.",
        )
        self.lease_owner = self.lease_owner.strip()
        self.lease_expires_at = _normalize_mutation_timestamp(
            self.lease_expires_at,
            field_name="lease_expires_at",
            required=False,
        )
        self.reconciliation_key = self.reconciliation_key.strip()
        self.provider_target_key = self.provider_target_key.strip()
        if self.provider_target_key and not self.reconciliation_key:
            raise ValueError("Provider target keys require a reconciliation key.")
        self.provider_effect_phase = self.provider_effect_phase.strip()
        self.provider_effect_started_at = _normalize_mutation_timestamp(
            self.provider_effect_started_at,
            field_name="provider_effect_started_at",
            required=False,
        )
        if bool(self.provider_effect_phase) != bool(self.provider_effect_started_at):
            raise ValueError(
                "Provider effect checkpoints require both phase and started_at evidence."
            )
        if self.provider_effect_phase and not self.reconciliation_key:
            raise ValueError("Provider effect checkpoints require a reconciliation key.")
        recorded_at = self.recorded_at.strip()
        self.created_at = _normalize_mutation_timestamp(
            self.created_at.strip() or recorded_at,
            field_name="created_at",
            required=False,
        )
        self.updated_at = _normalize_mutation_timestamp(
            self.updated_at.strip() or recorded_at or self.created_at,
            field_name="updated_at",
            required=False,
        )
        self.response_trace_id = self.response_trace_id.strip()
        self.recorded_at = _normalize_mutation_timestamp(
            recorded_at,
            field_name="recorded_at",
            required=False,
        )
        if self.state == "running":
            if not self.lease_owner:
                raise ValueError("Running mutation reservations require lease_owner.")
            if not self.lease_expires_at:
                raise ValueError("Running mutation reservations require lease_expires_at.")
            if not self.created_at or not self.updated_at:
                raise ValueError("Running mutation reservations require timestamps.")
            if parse_launchplane_mutation_timestamp(
                self.lease_expires_at,
                field_name="lease_expires_at",
            ) <= parse_launchplane_mutation_timestamp(
                self.updated_at,
                field_name="updated_at",
            ):
                raise ValueError("Running mutation reservation leases must expire in the future.")
            if self.response_status_code is not None or self.response_trace_id or self.recorded_at:
                raise ValueError("Running mutation reservations must not include a response.")
            if self.response_payload:
                raise ValueError("Running mutation reservations must not include response_payload.")
        elif self.state == "completed":
            if self.response_status_code is None:
                raise ValueError("Completed mutation reservations require response_status_code.")
            if not self.response_trace_id:
                raise ValueError("Completed mutation reservations require response_trace_id.")
            if not self.recorded_at:
                raise ValueError("Completed mutation reservations require recorded_at.")
            if not self.created_at or not self.updated_at:
                raise ValueError("Completed mutation reservations require timestamps.")
        else:
            if not self.lease_owner:
                raise ValueError("Reconcile-required mutation reservations require lease_owner.")
            if not self.reconciliation_key:
                raise ValueError(
                    "Reconcile-required mutation reservations require reconciliation_key."
                )
            if not self.created_at or not self.updated_at:
                raise ValueError("Reconcile-required mutation reservations require timestamps.")
            if self.response_status_code is not None or self.response_trace_id or self.recorded_at:
                raise ValueError(
                    "Reconcile-required mutation reservations must not include a response."
                )
            if self.response_payload:
                raise ValueError(
                    "Reconcile-required mutation reservations must not include response_payload."
                )
        return self


def build_launchplane_idempotency_record_id(*, response_trace_id: str) -> str:
    normalized_trace_id = response_trace_id.strip()
    if not normalized_trace_id:
        raise ValueError("idempotency record id requires response_trace_id")
    return f"idempotency-{normalized_trace_id}"


def build_launchplane_mutation_reservation_id(
    *,
    scope: str,
    route_path: str,
    idempotency_key: str,
) -> str:
    normalized_scope = _normalize_required(scope, "Mutation reservation requires scope.")
    normalized_route_path = _normalize_required(
        route_path,
        "Mutation reservation requires route_path.",
    )
    normalized_idempotency_key = _normalize_required(
        idempotency_key,
        "Mutation reservation requires idempotency_key.",
    )
    record_digest = hashlib.sha256(
        "\x1f".join(
            (
                normalized_scope,
                normalized_route_path,
                normalized_idempotency_key,
            )
        ).encode("utf-8")
    ).hexdigest()
    return f"mutation-reservation-{record_digest}"


def build_launchplane_mutation_reservation(
    *,
    scope: str,
    route_path: str,
    idempotency_key: str,
    request_fingerprint: str,
    lease_owner: str,
    lease_expires_at: str,
    reserved_at: str,
    reconciliation_key: str = "",
    provider_target_key: str = "",
) -> LaunchplaneIdempotencyRecord:
    normalized_scope = _normalize_required(scope, "Mutation reservation requires scope.")
    normalized_route_path = _normalize_required(
        route_path,
        "Mutation reservation requires route_path.",
    )
    normalized_idempotency_key = _normalize_required(
        idempotency_key,
        "Mutation reservation requires idempotency_key.",
    )
    return LaunchplaneIdempotencyRecord(
        record_id=build_launchplane_mutation_reservation_id(
            scope=normalized_scope,
            route_path=normalized_route_path,
            idempotency_key=normalized_idempotency_key,
        ),
        scope=normalized_scope,
        route_path=normalized_route_path,
        idempotency_key=normalized_idempotency_key,
        request_fingerprint=request_fingerprint,
        state="running",
        lease_owner=lease_owner,
        lease_expires_at=lease_expires_at,
        reconciliation_key=reconciliation_key,
        provider_target_key=provider_target_key,
        created_at=reserved_at,
        updated_at=reserved_at,
    )


def complete_launchplane_mutation_reservation(
    reservation: LaunchplaneIdempotencyRecord,
    *,
    response_status_code: int,
    response_trace_id: str,
    completed_at: str,
    response_payload: dict[str, Any],
) -> LaunchplaneIdempotencyRecord:
    if reservation.state != "running":
        raise ValueError("Only running mutation reservations can be completed.")
    payload = reservation.model_dump(mode="json")
    payload.update(
        {
            "state": "completed",
            "updated_at": completed_at,
            "response_status_code": response_status_code,
            "response_trace_id": response_trace_id,
            "recorded_at": completed_at,
            "response_payload": response_payload,
        }
    )
    return LaunchplaneIdempotencyRecord.model_validate(payload)


def _normalize_required(value: str, message: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(message)
    return normalized


def format_launchplane_mutation_timestamp(value: datetime) -> str:
    normalized = value
    if normalized.tzinfo is None or normalized.utcoffset() is None:
        raise ValueError("Mutation reservation timestamps require a timezone.")
    return (
        normalized.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def parse_launchplane_mutation_timestamp(
    value: str,
    *,
    field_name: str,
) -> datetime:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"Mutation reservation requires {field_name}.")
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(
            f"Mutation reservation {field_name} must be an ISO-8601 timestamp."
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"Mutation reservation {field_name} requires a timezone.")
    return parsed.astimezone(timezone.utc)


def _normalize_mutation_timestamp(
    value: str,
    *,
    field_name: str,
    required: bool,
) -> str:
    normalized = value.strip()
    if not normalized:
        if required:
            raise ValueError(f"Mutation reservation requires {field_name}.")
        return ""
    return format_launchplane_mutation_timestamp(
        parse_launchplane_mutation_timestamp(normalized, field_name=field_name)
    )
