"""add material incident events and reminders

Revision ID: d7f9a1b3c5e7
Revises: c6f8a0b2d4e6
Create Date: 2026-07-27 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator, Sequence
import hashlib
import json
from typing import Any, cast

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from control_plane.contracts.product_health_monitoring_migration import (
    canonical_health_check_record_token,
)
from control_plane.contracts.public_ingress_monitoring import (
    PUBLIC_INGRESS_DEFAULT_REMINDER_INTERVAL_SECONDS,
    PublicIngressCheckKind,
    PublicIngressFailureCode,
    PublicIngressIncidentEventRecord,
    PublicIngressIncidentMaterialFingerprint,
    PublicIngressIncidentResolutionReason,
    PublicIngressIncidentRouteAuthority,
    PublicIngressIncidentRuntimeMismatch,
    PublicIngressObservationRecord,
    PublicIngressTargetKind,
    build_public_ingress_incident_event_id,
    build_public_ingress_material_fingerprint,
    public_ingress_failure_layer,
    public_ingress_incident_severity,
    public_ingress_material_fingerprint_sha256,
)

revision: str = "d7f9a1b3c5e7"
down_revision: str | None = "c6f8a0b2d4e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OBSERVATIONS_TABLE = "launchplane_public_ingress_observations"
_INCIDENTS_TABLE = "launchplane_public_ingress_incidents"
_EVENTS_TABLE = "launchplane_public_ingress_incident_events"
_REMINDERS_TABLE = "launchplane_public_ingress_incident_reminders"
_POLICIES_TABLE = "launchplane_public_ingress_notification_policies"
_OBSERVATION_INCIDENT_INDEX = "launchplane_public_ingress_observations_incident_idx"
_OBSERVATION_CHECK_INDEX = "launchplane_public_ingress_observations_check_idx"
_OPEN_INCIDENT_INDEX = "launchplane_public_ingress_incidents_open_uidx"


def _table_exists(table_name: str) -> bool:
    return table_name in set(sa.inspect(op.get_bind()).get_table_names())


def _column_exists(table_name: str, column_name: str) -> bool:
    return column_name in {
        str(column["name"]) for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def _index_exists(table_name: str, index_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return index_name in {
        str(index["name"]) for index in sa.inspect(op.get_bind()).get_indexes(table_name)
    }


def upgrade() -> None:
    if not _column_exists(_OBSERVATIONS_TABLE, "incident_id"):
        op.add_column(
            _OBSERVATIONS_TABLE,
            sa.Column("incident_id", sa.String(), nullable=False, server_default=""),
        )
    if not _column_exists(_OBSERVATIONS_TABLE, "check_token"):
        op.add_column(
            _OBSERVATIONS_TABLE,
            sa.Column("check_token", sa.String(), nullable=False, server_default=""),
        )
    if not _column_exists(_OBSERVATIONS_TABLE, "check_kind"):
        op.add_column(
            _OBSERVATIONS_TABLE,
            sa.Column("check_kind", sa.String(), nullable=False, server_default="public_http"),
        )
    if not _column_exists(_INCIDENTS_TABLE, "check_token"):
        op.add_column(
            _INCIDENTS_TABLE,
            sa.Column("check_token", sa.String(), nullable=False, server_default=""),
        )
    if not _column_exists(_INCIDENTS_TABLE, "check_kind"):
        op.add_column(
            _INCIDENTS_TABLE,
            sa.Column("check_kind", sa.String(), nullable=False, server_default="public_http"),
        )
    if not _column_exists(_INCIDENTS_TABLE, "state_version"):
        op.add_column(
            _INCIDENTS_TABLE,
            sa.Column("state_version", sa.Integer(), nullable=False, server_default="1"),
        )
    _create_event_tables()
    _migrate_records()
    if not _index_exists(_OBSERVATIONS_TABLE, _OBSERVATION_INCIDENT_INDEX):
        op.create_index(
            _OBSERVATION_INCIDENT_INDEX,
            _OBSERVATIONS_TABLE,
            ["incident_id", sa.text("observed_at DESC")],
        )
    if not _index_exists(_OBSERVATIONS_TABLE, _OBSERVATION_CHECK_INDEX):
        op.create_index(
            _OBSERVATION_CHECK_INDEX,
            _OBSERVATIONS_TABLE,
            [
                "product",
                "context",
                "instance",
                "check_token",
                "check_kind",
                sa.text("observed_at DESC"),
            ],
        )
    if not _index_exists(_INCIDENTS_TABLE, _OPEN_INCIDENT_INDEX):
        op.create_index(
            _OPEN_INCIDENT_INDEX,
            _INCIDENTS_TABLE,
            ["product", "context", "instance", "check_token", "check_kind"],
            unique=True,
            postgresql_where=sa.text("status = 'open'"),
            sqlite_where=sa.text("status = 'open'"),
        )


def downgrade() -> None:
    _downgrade_payloads()
    op.drop_index(_OPEN_INCIDENT_INDEX, table_name=_INCIDENTS_TABLE)
    op.drop_index(_OBSERVATION_CHECK_INDEX, table_name=_OBSERVATIONS_TABLE)
    op.drop_index(_OBSERVATION_INCIDENT_INDEX, table_name=_OBSERVATIONS_TABLE)
    op.drop_table(_REMINDERS_TABLE)
    op.drop_table(_EVENTS_TABLE)
    op.drop_column(_INCIDENTS_TABLE, "state_version")
    op.drop_column(_INCIDENTS_TABLE, "check_kind")
    op.drop_column(_INCIDENTS_TABLE, "check_token")
    op.drop_column(_OBSERVATIONS_TABLE, "check_kind")
    op.drop_column(_OBSERVATIONS_TABLE, "check_token")
    op.drop_column(_OBSERVATIONS_TABLE, "incident_id")


def _create_event_tables() -> None:
    payload_type = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")
    if not _table_exists(_EVENTS_TABLE):
        op.create_table(
            _EVENTS_TABLE,
            sa.Column("event_id", sa.String(), nullable=False),
            sa.Column("incident_id", sa.String(), nullable=False),
            sa.Column("event", sa.String(), nullable=False),
            sa.Column("occurred_at", sa.String(), nullable=False),
            sa.Column("payload", payload_type, nullable=False),
            sa.PrimaryKeyConstraint("event_id"),
        )
    if not _index_exists(_EVENTS_TABLE, "launchplane_pi_incident_events_incident_idx"):
        op.create_index(
            "launchplane_pi_incident_events_incident_idx",
            _EVENTS_TABLE,
            ["incident_id", sa.text("occurred_at DESC")],
        )
    if not _index_exists(_EVENTS_TABLE, "launchplane_pi_incident_events_kind_idx"):
        op.create_index(
            "launchplane_pi_incident_events_kind_idx",
            _EVENTS_TABLE,
            ["event", sa.text("occurred_at DESC")],
        )
    if not _table_exists(_REMINDERS_TABLE):
        op.create_table(
            _REMINDERS_TABLE,
            sa.Column("reminder_state_id", sa.String(), nullable=False),
            sa.Column("incident_id", sa.String(), nullable=False),
            sa.Column("policy_id", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("next_reminder_at", sa.String(), nullable=False),
            sa.Column("updated_at", sa.String(), nullable=False),
            sa.Column("payload", payload_type, nullable=False),
            sa.PrimaryKeyConstraint("reminder_state_id"),
        )
    if not _index_exists(_REMINDERS_TABLE, "launchplane_pi_incident_reminders_due_idx"):
        op.create_index(
            "launchplane_pi_incident_reminders_due_idx",
            _REMINDERS_TABLE,
            ["status", "next_reminder_at"],
        )
    if not _index_exists(_REMINDERS_TABLE, "launchplane_pi_incident_reminders_incident_idx"):
        op.create_index(
            "launchplane_pi_incident_reminders_incident_idx",
            _REMINDERS_TABLE,
            ["incident_id", "policy_id"],
        )


def _migrate_records() -> None:
    connection = op.get_bind()
    metadata = sa.MetaData()
    observations = sa.Table(_OBSERVATIONS_TABLE, metadata, autoload_with=connection)
    incidents = sa.Table(_INCIDENTS_TABLE, metadata, autoload_with=connection)
    events = sa.Table(_EVENTS_TABLE, metadata, autoload_with=connection)
    policies = sa.Table(_POLICIES_TABLE, metadata, autoload_with=connection)

    canonical_incidents = _reconcile_duplicate_open_incidents()
    incident_windows = _incident_windows(canonical_incidents)

    latest_failing_observations: dict[str, dict[str, object]] = {}
    for row in _batched_table_rows(connection, observations, key_name="record_id"):
        observation_row: dict[str, object] = {
            "record_id": str(row["record_id"]),
            "product": str(row["product"]),
            "context": str(row["context"]),
            "instance": str(row["instance"]),
            "observed_at": str(row["observed_at"]),
            "payload": _payload_dict(row["payload"]),
        }
        incident_id = _incident_for_observation(observation_row, incident_windows)
        payload = observation_row["payload"]
        if not isinstance(payload, dict):
            continue
        payload["incident_id"] = incident_id
        payload.setdefault("incident_event_id", "")
        payload.setdefault("material_fingerprint", None)
        payload.setdefault("material_fingerprint_sha256", "")
        if payload.get("status") == "fail":
            try:
                observation = PublicIngressObservationRecord.model_validate(payload)
                fingerprint = build_public_ingress_material_fingerprint(observation)
                payload["material_fingerprint"] = fingerprint.model_dump(mode="json")
                payload["material_fingerprint_sha256"] = public_ingress_material_fingerprint_sha256(
                    fingerprint
                )
            except ValueError:
                pass
        connection.execute(
            observations.update()
            .where(observations.c.record_id == observation_row["record_id"])
            .values(
                incident_id=incident_id,
                check_token=canonical_health_check_record_token(
                    str(payload.get("check_name") or "public-ingress")
                ),
                check_kind=str(payload.get("check_kind") or "public_http"),
                payload=payload,
            )
        )
        if incident_id and payload.get("status") == "fail":
            previous = latest_failing_observations.get(incident_id)
            if previous is None or (
                str(observation_row["observed_at"]),
                str(observation_row["record_id"]),
            ) > (str(previous["observed_at"]), str(previous["record_id"])):
                latest_failing_observations[incident_id] = observation_row

    for row in _batched_table_rows(connection, incidents, key_name="incident_id"):
        incident_row: dict[str, object] = {
            "incident_id": str(row["incident_id"]),
            "product": str(row["product"]),
            "context": str(row["context"]),
            "instance": str(row["instance"]),
            "status": str(row["status"]),
            "opened_at": str(row["opened_at"]),
            "latest_observed_at": str(row["latest_observed_at"]),
            "payload": _payload_dict(row["payload"]),
        }
        payload = incident_row["payload"]
        if not isinstance(payload, dict):
            continue
        latest_failing_observation = latest_failing_observations.get(
            str(incident_row["incident_id"])
        )
        fingerprint, fingerprint_complete, observation_id = _incident_fingerprint(
            incident_row=incident_row,
            linked_observations=(
                [latest_failing_observation] if latest_failing_observation is not None else []
            ),
        )
        fingerprint_sha256 = public_ingress_material_fingerprint_sha256(fingerprint)
        baseline_at = str(
            payload.get("latest_observed_at")
            or incident_row["latest_observed_at"]
            or incident_row["opened_at"]
        )
        event_id = build_public_ingress_incident_event_id(
            incident_id=str(incident_row["incident_id"]),
            event="baseline",
            material_fingerprint_sha256=fingerprint_sha256,
        )
        baseline = PublicIngressIncidentEventRecord(
            event_id=event_id,
            incident_id=str(incident_row["incident_id"]),
            event="baseline",
            reason="migration_baseline",
            occurred_at=baseline_at,
            observation_id=observation_id,
            material_fingerprint_sha256=fingerprint_sha256,
            severity=fingerprint.severity,
            delivery_eligible=False,
            summary="Existing incident material state adopted during schema migration.",
        )
        latest_event = baseline
        migrated_events = [baseline]
        incident_status = str(payload.get("status") or incident_row["status"])
        if incident_status == "resolved":
            resolved_at = str(
                payload.get("resolved_at")
                or payload.get("latest_observed_at")
                or incident_row["latest_observed_at"]
                or baseline_at
            )
            resolved_observation_id = str(payload.get("resolved_observation_id") or observation_id)
            resolved_event = PublicIngressIncidentEventRecord(
                event_id=build_public_ingress_incident_event_id(
                    incident_id=str(incident_row["incident_id"]),
                    event="resolved",
                    material_fingerprint_sha256=fingerprint_sha256,
                    previous_event_id=baseline.event_id,
                ),
                incident_id=str(incident_row["incident_id"]),
                event="resolved",
                reason=_resolved_event_reason(payload),
                occurred_at=resolved_at,
                observation_id=resolved_observation_id,
                material_fingerprint_sha256=fingerprint_sha256,
                previous_material_fingerprint_sha256=fingerprint_sha256,
                severity=fingerprint.severity,
                delivery_eligible=False,
                suppression_reason="migration_reconciliation",
                summary="Existing resolved incident terminal state adopted during migration.",
            )
            migrated_events.append(resolved_event)
            latest_event = resolved_event
        payload.update(
            {
                "schema_version": 2,
                "state_version": 1,
                "severity": fingerprint.severity,
                "material_fingerprint": fingerprint.model_dump(mode="json"),
                "material_fingerprint_sha256": fingerprint_sha256,
                "material_fingerprint_complete": fingerprint_complete,
                "latest_material_event_id": latest_event.event_id,
                "latest_material_event": latest_event.event,
                "latest_material_event_at": latest_event.occurred_at,
                "notification_state": "active",
                "notification_state_changed_at": "",
                "notification_state_reason": "",
                "silenced_until": "",
                "recovery_observation_threshold": 1,
                "consecutive_recovery_observations": 0,
                "latest_recovery_observation_id": "",
            }
        )
        check_name = str(payload.get("check_name") or "public-ingress")
        check_kind = str(payload.get("check_kind") or "public_http")
        connection.execute(
            incidents.update()
            .where(incidents.c.incident_id == incident_row["incident_id"])
            .values(
                status=payload.get("status", incident_row["status"]),
                latest_observed_at=payload.get(
                    "latest_observed_at", incident_row["latest_observed_at"]
                ),
                check_token=canonical_health_check_record_token(check_name),
                check_kind=check_kind,
                state_version=1,
                payload=payload,
            )
        )
        for incident_event in migrated_events:
            connection.execute(events.insert().values(**_event_row(incident_event)))
        _link_observation_event(
            connection=connection,
            observations=observations,
            observation_id=baseline.observation_id,
            incident_id=baseline.incident_id,
            event_id=baseline.event_id,
        )
        if latest_event.event == "resolved":
            _link_observation_event(
                connection=connection,
                observations=observations,
                observation_id=latest_event.observation_id,
                incident_id=latest_event.incident_id,
                event_id=latest_event.event_id,
            )

    for row in _batched_table_rows(connection, policies, key_name="policy_id"):
        payload = _payload_dict(row["payload"])
        payload.setdefault(
            "reminder_interval_seconds", PUBLIC_INGRESS_DEFAULT_REMINDER_INTERVAL_SECONDS
        )
        connection.execute(
            policies.update()
            .where(policies.c.policy_id == row["policy_id"])
            .values(payload=payload)
        )


def _reconcile_duplicate_open_incidents() -> set[str]:
    connection = op.get_bind()
    metadata = sa.MetaData()
    incidents = sa.Table(_INCIDENTS_TABLE, metadata, autoload_with=connection)
    groups: dict[tuple[str, str, str, str, str], list[dict[str, object]]] = defaultdict(list)
    for source_row in _batched_table_rows(connection, incidents, key_name="incident_id"):
        row: dict[str, object] = {
            "incident_id": str(source_row["incident_id"]),
            "product": str(source_row["product"]),
            "context": str(source_row["context"]),
            "instance": str(source_row["instance"]),
            "status": str(source_row["status"]),
            "opened_at": str(source_row["opened_at"]),
            "latest_observed_at": str(source_row["latest_observed_at"]),
            "payload": _payload_dict(source_row["payload"]),
        }
        payload = row["payload"]
        if not isinstance(payload, dict) or str(payload.get("status") or row["status"]) != "open":
            continue
        key = (
            str(row["product"]),
            str(row["context"]),
            str(row["instance"]),
            canonical_health_check_record_token(str(payload.get("check_name") or "public-ingress")),
            str(payload.get("check_kind") or "public_http"),
        )
        groups[key].append(row)
    canonical_ids: set[str] = set()
    for rows in groups.values():
        rows.sort(key=lambda row: (str(row["opened_at"]), str(row["incident_id"])))
        canonical = rows[0]
        canonical_ids.add(str(canonical["incident_id"]))
        if len(rows) == 1:
            continue
        latest = max(
            rows,
            key=lambda row: (str(row["latest_observed_at"]), str(row["incident_id"])),
        )
        canonical_payload = canonical["payload"]
        latest_payload = latest["payload"]
        if isinstance(canonical_payload, dict) and isinstance(latest_payload, dict):
            for field_name in (
                "latest_observation_id",
                "latest_observed_at",
                "failure_code",
                "summary",
            ):
                if field_name in latest_payload:
                    canonical_payload[field_name] = latest_payload[field_name]
            canonical["latest_observed_at"] = latest["latest_observed_at"]
            connection.execute(
                incidents.update()
                .where(incidents.c.incident_id == canonical["incident_id"])
                .values(
                    latest_observed_at=canonical["latest_observed_at"],
                    payload=canonical_payload,
                )
            )
        for duplicate in rows[1:]:
            duplicate_payload = duplicate["payload"]
            if not isinstance(duplicate_payload, dict):
                continue
            duplicate_payload.update(
                {
                    "status": "resolved",
                    "resolved_at": str(duplicate["latest_observed_at"]),
                    "resolved_observation_id": str(
                        duplicate_payload.get("latest_observation_id") or "migration-reconciliation"
                    ),
                    "resolution_reason": "duplicate_reconciled",
                }
            )
            duplicate["status"] = "resolved"
            connection.execute(
                incidents.update()
                .where(incidents.c.incident_id == duplicate["incident_id"])
                .values(status="resolved", payload=duplicate_payload)
            )
    return canonical_ids


def _incident_windows(
    canonical_open_incident_ids: set[str],
) -> dict[tuple[str, str, str, str, str], list[dict[str, object]]]:
    windows: dict[tuple[str, str, str, str, str], list[dict[str, object]]] = defaultdict(list)
    connection = op.get_bind()
    metadata = sa.MetaData()
    incidents = sa.Table(_INCIDENTS_TABLE, metadata, autoload_with=connection)
    for row in _batched_table_rows(connection, incidents, key_name="incident_id"):
        payload = _payload_dict(row["payload"])
        incident_id = str(row["incident_id"])
        if str(payload.get("status") or row["status"]) == "open" and (
            canonical_open_incident_ids and incident_id not in canonical_open_incident_ids
        ):
            continue
        key = (
            str(row["product"]),
            str(row["context"]),
            str(row["instance"]),
            canonical_health_check_record_token(str(payload.get("check_name") or "public-ingress")),
            str(payload.get("check_kind") or "public_http"),
        )
        windows[key].append(
            {
                "incident_id": incident_id,
                "opened_at": str(payload.get("opened_at") or row["opened_at"]),
                "resolved_at": str(payload.get("resolved_at") or ""),
            }
        )
    for rows in windows.values():
        rows.sort(key=lambda row: (str(row["opened_at"]), str(row["incident_id"])))
    return windows


def _incident_for_observation(
    observation_row: dict[str, object],
    windows: dict[tuple[str, str, str, str, str], list[dict[str, object]]],
) -> str:
    payload = observation_row["payload"]
    if not isinstance(payload, dict):
        return ""
    key = (
        str(observation_row["product"]),
        str(observation_row["context"]),
        str(observation_row["instance"]),
        canonical_health_check_record_token(str(payload.get("check_name") or "public-ingress")),
        str(payload.get("check_kind") or "public_http"),
    )
    observed_at = str(observation_row["observed_at"])
    candidate = ""
    for window in windows.get(key, []):
        if observed_at < str(window["opened_at"]):
            continue
        resolved_at = str(window["resolved_at"])
        if resolved_at and observed_at > resolved_at:
            continue
        candidate = str(window["incident_id"])
    return candidate


def _incident_fingerprint(
    *,
    incident_row: dict[str, object],
    linked_observations: list[dict[str, object]],
) -> tuple[PublicIngressIncidentMaterialFingerprint, bool, str]:
    failing_observations = [
        row
        for row in linked_observations
        if isinstance(row["payload"], dict) and row["payload"].get("status") == "fail"
    ]
    failing_observations.sort(
        key=lambda row: (str(row["observed_at"]), str(row["record_id"])), reverse=True
    )
    if failing_observations:
        try:
            observation = PublicIngressObservationRecord.model_validate(
                failing_observations[0]["payload"]
            )
            fingerprint = build_public_ingress_material_fingerprint(observation)
            return fingerprint, True, observation.record_id
        except ValueError:
            pass
    payload = incident_row["payload"]
    if not isinstance(payload, dict):
        payload = {}
    failure_code = cast(
        PublicIngressFailureCode,
        str(payload.get("failure_code") or "unknown_error"),
    )
    check_kind = cast(
        PublicIngressCheckKind,
        str(payload.get("check_kind") or "public_http"),
    )
    target_kind = cast(
        PublicIngressTargetKind,
        {
            "private_http": "private_health_url",
            "provider": "provider",
            "tls": "tls_domain",
        }.get(check_kind, "health_url"),
    )
    route_kind = {
        "private_http": "private_endpoint",
        "provider": "provider",
        "tls": "route_binding",
    }.get(check_kind, "public_urls")
    route_digest = hashlib.sha256(str(incident_row["incident_id"]).encode("utf-8")).hexdigest()
    authority_kwargs: dict[str, object] = {
        "kind": route_kind,
        "target_keys": (f"legacy-route-sha256:{route_digest}",),
    }
    if route_kind == "private_endpoint":
        authority_kwargs.update(
            private_endpoint_key="legacy-private-endpoint",
            private_endpoint_sha256=route_digest,
        )
    elif route_kind == "provider":
        authority_kwargs.update(provider="legacy-provider", provider_check="legacy-check")
    elif route_kind == "route_binding":
        authority_kwargs.update(domain_name=f"legacy-{route_digest[:16]}.invalid")
    fingerprint = PublicIngressIncidentMaterialFingerprint(
        check_kind=check_kind,
        failure_code=failure_code,
        failure_layer=public_ingress_failure_layer(failure_code),
        severity=public_ingress_incident_severity(failure_code),
        affected_targets=(target_kind,),
        route_authority=PublicIngressIncidentRouteAuthority.model_validate(authority_kwargs),
        runtime_mismatch=(
            PublicIngressIncidentRuntimeMismatch(status="unverifiable")
            if failure_code == "wrong_runtime_identity"
            else None
        ),
        tls_status="unknown" if check_kind == "tls" else "",
    )
    observation_id = str(
        payload.get("latest_observation_id")
        or payload.get("opened_observation_id")
        or "migration-reconciliation"
    )
    return fingerprint, False, observation_id


def _batched_table_rows(
    connection: Any,
    table: sa.Table,
    *,
    key_name: str,
    batch_size: int = 500,
) -> Iterator[dict[str, object]]:
    key_column = table.c[key_name]
    last_key = ""
    while True:
        rows = (
            connection.execute(
                sa.select(table).where(key_column > last_key).order_by(key_column).limit(batch_size)
            )
            .mappings()
            .all()
        )
        if not rows:
            return
        for row in rows:
            yield {str(key): value for key, value in row.items()}
        last_key = str(rows[-1][key_name])


def _resolved_event_reason(
    payload: dict[str, object],
) -> PublicIngressIncidentResolutionReason:
    reason = str(payload.get("resolution_reason") or "recovered")
    if reason not in {"recovered", "monitoring_intent_changed", "duplicate_reconciled"}:
        reason = "recovered"
    return cast(PublicIngressIncidentResolutionReason, reason)


def _link_observation_event(
    *,
    connection: Any,
    observations: sa.Table,
    observation_id: str,
    incident_id: str,
    event_id: str,
) -> None:
    row = (
        connection.execute(
            sa.select(observations.c.incident_id, observations.c.payload)
            .where(observations.c.record_id == observation_id)
            .limit(1)
        )
        .mappings()
        .first()
    )
    if row is None:
        return
    payload = _payload_dict(row["payload"])
    if str(payload.get("incident_id") or row["incident_id"]) != incident_id:
        return
    payload["incident_event_id"] = event_id
    connection.execute(
        observations.update()
        .where(observations.c.record_id == observation_id)
        .values(payload=payload)
    )


def _event_row(event: PublicIngressIncidentEventRecord) -> dict[str, object]:
    return {
        "event_id": event.event_id,
        "incident_id": event.incident_id,
        "event": event.event,
        "occurred_at": event.occurred_at,
        "payload": event.model_dump(mode="json"),
    }


def _downgrade_payloads() -> None:
    connection = op.get_bind()
    metadata = sa.MetaData()
    observations = sa.Table(_OBSERVATIONS_TABLE, metadata, autoload_with=connection)
    incidents = sa.Table(_INCIDENTS_TABLE, metadata, autoload_with=connection)
    policies = sa.Table(_POLICIES_TABLE, metadata, autoload_with=connection)
    for row in connection.execute(sa.select(observations.c.record_id, observations.c.payload)):
        payload = _payload_dict(row.payload)
        for field_name in (
            "incident_id",
            "incident_event_id",
            "material_fingerprint",
            "material_fingerprint_sha256",
        ):
            payload.pop(field_name, None)
        connection.execute(
            observations.update()
            .where(observations.c.record_id == row.record_id)
            .values(payload=payload)
        )
    incident_fields = {
        "state_version",
        "severity",
        "material_fingerprint",
        "material_fingerprint_sha256",
        "material_fingerprint_complete",
        "latest_material_event_id",
        "latest_material_event",
        "latest_material_event_at",
        "notification_state",
        "notification_state_changed_at",
        "notification_state_reason",
        "silenced_until",
        "recovery_observation_threshold",
        "consecutive_recovery_observations",
        "latest_recovery_observation_id",
    }
    for row in connection.execute(sa.select(incidents.c.incident_id, incidents.c.payload)):
        payload = _payload_dict(row.payload)
        payload["schema_version"] = 1
        for field_name in incident_fields:
            payload.pop(field_name, None)
        connection.execute(
            incidents.update()
            .where(incidents.c.incident_id == row.incident_id)
            .values(payload=payload)
        )
    for row in connection.execute(sa.select(policies.c.policy_id, policies.c.payload)):
        payload = _payload_dict(row.payload)
        payload.pop("reminder_interval_seconds", None)
        connection.execute(
            policies.update().where(policies.c.policy_id == row.policy_id).values(payload=payload)
        )


def _payload_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        decoded = json.loads(value)
        if isinstance(decoded, dict):
            return dict(decoded)
    raise ValueError("public ingress migration expected an object payload")
