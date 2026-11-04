import base64
from collections import defaultdict
from datetime import datetime, timezone
import json
from typing import Any, Literal, cast

from sqlalchemy import Numeric, String, func, inspect, or_, select, union_all
from sqlalchemy.engine import Connection, Engine

from control_plane.contracts.canonical_json import nonvalidating_compact_sorted_json_sha256
from control_plane.contracts.read_coverage import ReadCoverage
from control_plane.contracts.public_ingress_monitoring import PublicIngressObservationRecord
from control_plane.lane_health_history import HistoryObservation, project_observation
from control_plane.monitoring_authority_history import (
    MonitoringAuthorityBatchStart,
    MonitoringAuthorityHistory,
    MonitoringAuthorityLaneEvidence,
    MonitoringAuthorityQueryError,
    MonitoringAuthorityRevision,
    MonitoringAuthorityUnavailable,
    monitoring_observations_sha256,
)
from control_plane.storage.migrations.monitoring_authority_v1 import history, INVALID_CONFIRMATION
from control_plane.storage.schema_invariants import monitoring_authority_capture_errors


def _snapshot(connection: Connection) -> dict[str, Any]:
    epoch = (
        select(history.c.sequence)
        .where(history.c.operation == "epoch")
        .order_by(history.c.sequence.desc())
        .limit(1)
        .scalar_subquery()
    )
    row = (
        connection.execute(
            select(
                func.pg_snapshot_xmin(func.pg_current_snapshot()).cast(String).label("xmin"),
                func.clock_timestamp().label("at"),
                select(
                    func.pg_control_system().table_valued("system_identifier").c.system_identifier
                )
                .scalar_subquery()
                .label("cluster_id"),
                epoch.label("epoch_sequence"),
            )
        )
        .mappings()
        .one()
    )
    return cast(dict[str, Any], dict(row))


def begin_batch(engine: Engine) -> MonitoringAuthorityBatchStart | None:
    if engine.dialect.name != "postgresql":
        return None
    with engine.begin() as connection:
        if not inspect(connection).has_table(history.name):
            return None
        errors = monitoring_authority_capture_errors(connection)
        snapshot = _snapshot(connection)
    if snapshot["epoch_sequence"] is None:
        raise MonitoringAuthorityUnavailable("Monitoring authority has no installation epoch")
    return MonitoringAuthorityBatchStart(
        started_at=snapshot["at"],
        cluster_id=str(snapshot["cluster_id"]),
        epoch_sequence=snapshot["epoch_sequence"],
        capture_errors=tuple(errors),
    )


def complete_batch(
    engine: Engine,
    *,
    start: MonitoringAuthorityBatchStart,
    checked_at: str,
    records: tuple[PublicIngressObservationRecord, ...],
) -> None:
    by_lane: dict[tuple[str, str, str], list[HistoryObservation]] = defaultdict(list)
    for record in records:
        by_lane[(record.product, record.context, record.instance)].append(
            project_observation(record)
        )
    lanes = tuple(
        MonitoringAuthorityLaneEvidence(
            product=lane[0],
            context=lane[1],
            instance=lane[2],
            observation_ids=tuple(sorted(record.record_id for record in observations)),
            observations_sha256=monitoring_observations_sha256(observations),
        )
        for lane, observations in sorted(by_lane.items())
    )
    with engine.begin() as connection:
        errors = [*start.capture_errors, *monitoring_authority_capture_errors(connection)]
        snapshot = _snapshot(connection)
        if str(snapshot["cluster_id"]) != start.cluster_id:
            errors.append("Database incarnation changed during the batch")
        if snapshot["epoch_sequence"] != start.epoch_sequence:
            errors.append("Monitoring authority epoch changed during the batch")
        connection.execute(
            history.insert().values(
                recorded_at=snapshot["at"]
                .astimezone(timezone.utc)
                .isoformat(timespec="microseconds"),
                source="",
                source_key="",
                transaction_id=func.pg_current_xact_id().cast(String),
                product="",
                context="",
                instance="",
                operation="confirmation",
                payload={
                    "xmin": str(snapshot["xmin"]),
                    "cluster_id": str(snapshot["cluster_id"]),
                    "epoch_sequence": start.epoch_sequence,
                    "started_at": start.started_at.isoformat(),
                    "checked_at": checked_at,
                    "lanes": [lane.model_dump(mode="json") for lane in lanes],
                    "capture_valid": not errors,
                    "capture_errors": sorted(set(errors)),
                },
            )
        )


def _project(row: dict[str, Any], product: str) -> MonitoringAuthorityRevision:
    payload = dict(row["payload"])
    visible = not product or row["product"] in {"", product}
    material = nonvalidating_compact_sorted_json_sha256(payload) if visible else ""
    if not visible:
        payload = {}
    elif row["source"] == "launchplane_private_health_endpoints":
        payload = {key: value for key, value in payload.items() if key not in {"url", "tls_ca_pem"}}
    elif row["source"] == "launchplane_route_bindings" and payload.get("external_ingress"):
        payload["external_ingress"] = {"domains": payload["external_ingress"]["domains"]}
    elif row["operation"] == "confirmation" and product:
        payload["lanes"] = [lane for lane in payload["lanes"] if lane["product"] == product]
        material = nonvalidating_compact_sorted_json_sha256(payload)
    return MonitoringAuthorityRevision.model_validate(
        {
            **row,
            "product": row["product"] if visible else "",
            "context": row["context"] if visible else "",
            "instance": row["instance"] if visible else "",
            "scope_visible": visible,
            "material_sha256": material,
            "payload": payload,
        }
    )


def read_history(
    engine: Engine, *, product: str, limit: int, cursor: str | None
) -> MonitoringAuthorityHistory:
    if engine.dialect.name != "postgresql":
        raise MonitoringAuthorityUnavailable("Retained authority history requires PostgreSQL")
    if not 1 <= limit <= 500:
        raise MonitoringAuthorityQueryError("Read between one and 500 authority revisions")
    with engine.begin() as connection:
        if not inspect(connection).has_table(history.name):
            raise MonitoringAuthorityUnavailable("Monitoring authority capture is not installed")
        errors = monitoring_authority_capture_errors(connection)
        current = _snapshot(connection)
        snapshot = current
        after = 0
        if cursor is not None:
            try:
                data = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
                if (
                    not isinstance(data, list)
                    or len(data) != 5
                    or data[0] != product
                    or data[1] != str(current["cluster_id"])
                    or type(data[2]) is not int
                    or not 0 < data[2] <= int(current["xmin"])
                    or type(data[3]) is not int
                    or data[3] < 0
                    or data[4] != current["epoch_sequence"]
                ):
                    raise ValueError
                snapshot = {**current, "xmin": data[2]}
                after = data[3]
            except (ValueError, TypeError, UnicodeError) as error:
                raise MonitoringAuthorityQueryError(
                    "Authority cursor does not match this scope"
                ) from error
        settled = history.c.transaction_id.cast(Numeric) < int(snapshot["xmin"])
        epoch_row = (
            connection.execute(
                select(history)
                .where(history.c.operation == "epoch", settled)
                .order_by(history.c.sequence.desc())
                .limit(1)
            )
            .mappings()
            .one_or_none()
        )
        epoch = (
            _project(cast(dict[str, Any], dict(epoch_row)), "") if epoch_row is not None else None
        )
        confirmed_since = None
        if epoch is None:
            errors.append("The installation epoch is not settled in this snapshot")
        elif epoch.payload.get("cluster_id") != str(snapshot["cluster_id"]):
            errors.append("Database incarnation differs from the installation epoch")
        else:
            confirmation = connection.scalar(
                select(history.c.recorded_at)
                .where(
                    history.c.operation == "confirmation",
                    settled,
                    history.c.sequence > epoch.sequence,
                )
                .order_by(history.c.sequence)
                .limit(1)
            )
            if confirmation is not None:
                confirmed_since = datetime.fromisoformat(cast(str, confirmation))
            if (
                connection.scalar(
                    select(history.c.sequence).where(INVALID_CONFIRMATION, settled).limit(1)
                )
                is not None
            ):
                errors.append("A retained confirmation records incomplete capture")
        if errors:
            confirmed_since = None
        rows_query = select(history).where(settled)
        if product:
            scoped = (
                select(
                    *history.c,
                    func.lag(history.c.product)
                    .over(
                        partition_by=(history.c.source, history.c.source_key),
                        order_by=history.c.sequence,
                    )
                    .label("previous_product"),
                )
                .where(settled, history.c.source != "")
                .subquery()
            )
            scoped_rows = select(*(scoped.c[column.name] for column in history.c)).where(
                or_(
                    scoped.c.product == product,
                    scoped.c.previous_product == product,
                )
            )
            combined = union_all(
                scoped_rows, select(history).where(settled, history.c.source == "")
            ).subquery()
            rows_query = select(combined)
            sequence = combined.c.sequence
        else:
            sequence = history.c.sequence
        rows = (
            connection.execute(
                rows_query.where(sequence > after).order_by(sequence).limit(limit + 1)
            )
            .mappings()
            .all()
        )
        page = rows[:limit]
        next_cursor = None
        if len(rows) > limit:
            next_cursor = base64.urlsafe_b64encode(
                json.dumps(
                    [
                        product,
                        str(snapshot["cluster_id"]),
                        int(snapshot["xmin"]),
                        page[-1]["sequence"],
                        current["epoch_sequence"],
                    ]
                ).encode()
            ).decode()
    authorization: Literal["scoped", "complete"] = "scoped" if product else "complete"
    return MonitoringAuthorityHistory(
        product=product,
        read_at=snapshot["at"],
        cluster_id=str(snapshot["cluster_id"]),
        settled_before_transaction=int(snapshot["xmin"]),
        epoch=epoch,
        confirmed_since=confirmed_since,
        capture_errors=tuple(sorted(set(errors))),
        coverage=ReadCoverage(
            population="settled_monitoring_authority_history",
            source="unknown" if errors else "complete",
            authorization=authorization,
            status="unknown" if errors else authorization,
            exclusions=("earlier_authority", "unsettled_transactions", "restore_continuity"),
        ),
        cursor=cursor,
        next_cursor=next_cursor,
        revisions=tuple(_project(cast(dict[str, Any], dict(row)), product) for row in page),
    )
