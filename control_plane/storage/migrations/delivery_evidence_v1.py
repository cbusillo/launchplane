"""Frozen schema and capture SQL for delivery-evidence migration v1.

Later migrations own later schemas. Keep this version independent of runtime
models so a historical upgrade can read the records it originally supported.
"""

from __future__ import annotations
from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
from typing import Any
from uuid import uuid4
from sqlalchemy import Boolean, Column, ForeignKey, Index, JSON, MetaData, String, Table, text
from sqlalchemy.engine import Connection

metadata = MetaData()
sources = Table(
    "launchplane_delivery_evidence_sources",
    metadata,
    Column("key", String, primary_key=True),
    Column("kind", String, nullable=False),
    Column("source_id", String, nullable=False),
    Column("product", String, nullable=False),
    Column("context", String, nullable=False),
    Column("instance", String, nullable=False),
    Column("version", String, nullable=False),
    Column("pending", Boolean, nullable=False),
    Column("queued_at", String, nullable=False),
    Column("recheck_at", String, nullable=False),
    Column("baseline", Boolean, nullable=False),
    Column("processed", JSON, nullable=False),
    Column("finished_at", String, nullable=False),
    Column("started_at", String, nullable=False),
    Column("passed", Boolean, nullable=False),
    Column("artifact_key", String, nullable=False),
    Column("source_key", String, nullable=False),
)
Index(
    "delivery_evidence_pending_lane_idx",
    sources.c.context,
    sources.c.instance,
    sources.c.pending,
    sources.c.baseline,
    sources.c.queued_at,
    sources.c.key,
)
Index(
    "delivery_evidence_pending_product_idx",
    sources.c.product,
    sources.c.pending,
    sources.c.baseline,
    sources.c.queued_at,
    sources.c.key,
)
Index(
    "delivery_evidence_latest_idx",
    sources.c.context,
    sources.c.instance,
    sources.c.kind,
    sources.c.finished_at,
    sources.c.started_at,
    sources.c.source_id,
)
Index(
    "delivery_evidence_success_idx",
    sources.c.context,
    sources.c.instance,
    sources.c.passed,
    sources.c.finished_at,
)
Index(
    "delivery_evidence_artifact_idx",
    sources.c.context,
    sources.c.instance,
    sources.c.artifact_key,
    sources.c.passed,
    sources.c.finished_at,
)
Index(
    "delivery_evidence_source_idx",
    sources.c.context,
    sources.c.instance,
    sources.c.source_key,
    sources.c.passed,
    sources.c.finished_at,
)
Index(
    "delivery_evidence_recheck_lane_idx",
    sources.c.context,
    sources.c.instance,
    sources.c.recheck_at,
)
Index("delivery_evidence_recheck_product_idx", sources.c.product, sources.c.recheck_at)
dependencies = Table(
    "launchplane_delivery_evidence_dependencies",
    metadata,
    Column("dependency_key", String, primary_key=True),
    Column("decision_key", String, ForeignKey(sources.c.key, ondelete="CASCADE"), primary_key=True),
)
checkpoints = Table(
    "launchplane_delivery_evidence_checkpoints",
    metadata,
    Column("key", String, primary_key=True),
    Column("payload", JSON, nullable=False),
)

# These are storage record types, not mutable product/runtime configuration.
SOURCE_KINDS = {
    "launchplane_deployments": "deployment",
    "launchplane_promotions": "promotion",
    "launchplane_release_review_decisions": "decision",
    "launchplane_odoo_prod_promotion_operations": "operation",
}
FILESYSTEM_TYPES = {
    "deployments": "launchplane_deployments",
    "promotions": "launchplane_promotions",
    "launchplane_release_review_decisions": "launchplane_release_review_decisions",
}
STEP_TYPES = {
    "launchplane_idempotency_records",
    "launchplane_verireel_prod_backup_gate_operations",
    "launchplane_odoo_prod_promotion_operations",
    "launchplane_odoo_prod_rollback_operations",
}


def normalized_time(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def candidate_key(value: str) -> str:
    return hashlib.sha256(value.strip().encode()).hexdigest()[:24]


def source_values(kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    source_id = str(payload.get("operation_id") or payload.get("record_id") or "")
    instance = str(payload.get("to_instance") or payload.get("instance") or "")
    if kind != "decision" and instance != "prod":
        return {}
    deploy = payload.get("deploy", {})
    values: dict[str, Any] = {
        "key": kind + ":" + source_id,
        "kind": kind,
        "source_id": source_id,
        "product": str(payload.get("product", "")),
        "context": str(payload.get("context", "")),
        "instance": instance,
        "version": uuid4().hex,
        "pending": True,
        "queued_at": datetime.now(timezone.utc).isoformat(),
        "recheck_at": "",
        "baseline": False,
        "processed": {},
        "finished_at": str(deploy.get("finished_at", "")),
        "started_at": str(deploy.get("started_at", "")),
        "passed": False,
        "artifact_key": "",
        "source_key": "",
    }
    if kind == "decision":
        values["finished_at"] = str(payload.get("decided_at", ""))
    if kind == "operation":
        values["finished_at"] = str(payload.get("updated_at", ""))
    if kind == "deployment":
        values["passed"] = (
            deploy.get("status") == "pass"
            and payload.get("post_deploy_update", {}).get("status", "skipped")
            in {"pass", "skipped"}
            and payload.get("destination_health", {}).get("status", "skipped")
            in {"pass", "skipped"}
        )
        values["source_key"] = candidate_key(str(payload.get("source_git_ref", "")))
        artifact = payload.get("artifact_identity")
        if artifact:
            values["artifact_key"] = candidate_key(str(artifact.get("artifact_id", "")))
    for name in ("finished_at", "started_at"):
        values[name] = normalized_time(values[name])
    return values


def projection_insert(dialect: str, source_query: str, *, baseline: bool = False) -> str:
    """Frozen raw-JSON projection shared by the trigger and upgrade backfill."""
    postgres = dialect == "postgresql"

    def field(*path: str, default: str = "") -> str:
        expression = (
            "item.p #>> '{" + ",".join(path) + "}'"
            if postgres
            else "json_extract(item.p, '$." + ".".join(path) + "')"
        )
        return f"COALESCE({expression}, '{default}')"

    def time_value(value: str) -> str:
        return f"launchplane_delivery_time_v1({value})" if postgres else value

    def digest(value: str) -> str:
        return (
            f"left(encode(sha256(convert_to(btrim({value}), 'UTF8')), 'hex'), 24)"
            if postgres
            else "''"
        )

    stamp = (
        "to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US') || '+00:00'"
        if postgres
        else "strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')"
    )
    version = (
        "md5(random()::text || clock_timestamp()::text)"
        if postgres
        else "lower(hex(randomblob(16)))"
    )
    instance = f"COALESCE(NULLIF({field('to_instance')}, ''), {field('instance')})"
    finished = (
        f"CASE item.kind WHEN 'decision' THEN {field('decided_at')} "
        f"WHEN 'operation' THEN {field('updated_at')} ELSE {field('deploy', 'finished_at')} END"
    )
    passed = (
        f"item.kind = 'deployment' AND {field('deploy', 'status')} = 'pass' "
        f"AND {field('post_deploy_update', 'status', default='skipped')} IN ('pass', 'skipped') "
        f"AND {field('destination_health', 'status', default='skipped')} IN ('pass', 'skipped')"
    )
    columns = {
        "key": "item.kind || ':' || item.source_id",
        "kind": "item.kind",
        "source_id": "item.source_id",
        "product": field("product"),
        "context": field("context"),
        "instance": instance,
        "version": version,
        "pending": "true",
        "queued_at": stamp,
        "recheck_at": "''",
        "baseline": "true" if baseline else "false",
        "processed": "'{}'",
        "finished_at": time_value(finished),
        "started_at": time_value(field("deploy", "started_at")),
        "passed": "(" + passed + ")",
        "artifact_key": f"CASE WHEN item.kind = 'deployment' THEN {digest(field('artifact_identity', 'artifact_id'))} ELSE '' END",
        "source_key": f"CASE WHEN item.kind = 'deployment' THEN {digest(field('source_git_ref'))} ELSE '' END",
    }
    assignments = ", ".join(
        f"{name} = excluded.{name}" for name in columns if name not in {"key", "processed"}
    )
    return (
        f"INSERT INTO {sources.name} ({', '.join(columns)}) "
        f"SELECT {', '.join(columns.values())} FROM ({source_query}) AS item "
        f"WHERE item.kind <> '' AND (item.kind = 'decision' OR {instance} = 'prod') "
        f"ON CONFLICT (key) DO UPDATE SET {assignments}"
    )


def install_triggers(connection: Connection) -> None:
    postgres = connection.dialect.name == "postgresql"
    if postgres:
        connection.execute(
            text("""
            CREATE OR REPLACE FUNCTION launchplane_delivery_time_v1(value text) RETURNS text
            LANGUAGE plpgsql IMMUTABLE SET timezone = 'UTC' AS $$ BEGIN
                IF value = '' THEN RETURN ''; END IF;
                RETURN to_char(value::timestamptz AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US') || '+00:00';
            EXCEPTION WHEN OTHERS THEN RETURN value; END $$
        """)
        )
        capture = projection_insert(
            "postgresql",
            "SELECT captured_payload AS p, captured_kind AS kind, captured_id AS source_id",
        )
        connection.execute(
            text(
                """
            CREATE OR REPLACE FUNCTION launchplane_delivery_capture_v1() RETURNS trigger
            LANGUAGE plpgsql AS $$
            DECLARE captured_payload jsonb; captured_id text; captured_kind text := TG_ARGV[0];
            BEGIN
                IF TG_OP = 'DELETE' THEN captured_payload := OLD.payload::jsonb;
                ELSE captured_payload := NEW.payload::jsonb; END IF;
                captured_id := captured_payload->>TG_ARGV[1];
                IF TG_ARGV[2] = 'step' THEN
                    PERFORM pg_advisory_xact_lock(hashtextextended(
                        'delivery-dependency:' || TG_TABLE_NAME || ':' || captured_id, 0));
                    UPDATE launchplane_delivery_evidence_sources
                    SET pending = true, baseline = false,
                        version = md5(random()::text || clock_timestamp()::text),
                        queued_at = to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US') || '+00:00'
                    WHERE key IN (SELECT decision_key FROM launchplane_delivery_evidence_dependencies
                                  WHERE dependency_key = TG_TABLE_NAME || ':' || captured_id);
                END IF;
                IF captured_kind = '' THEN RETURN NULL; END IF;
                IF TG_OP = 'DELETE' THEN
                    DELETE FROM launchplane_delivery_evidence_dependencies WHERE decision_key = captured_kind || ':' || captured_id;
                    DELETE FROM launchplane_delivery_evidence_sources WHERE key = captured_kind || ':' || captured_id;
                ELSE
        """
                + capture
                + """;
                END IF;
                RETURN NULL;
            END $$
        """
            )
        )
    for name in sorted(set(SOURCE_KINDS) | STEP_TYPES):
        kind = SOURCE_KINDS.get(name, "")
        identity = "operation_id" if name.endswith("_operations") else "record_id"
        step = "step" if name in STEP_TYPES else ""
        trigger = name + "_delivery_capture"
        if postgres:
            connection.execute(text(f"DROP TRIGGER IF EXISTS {trigger} ON {name}"))
            connection.execute(
                text(
                    f"CREATE TRIGGER {trigger} AFTER INSERT OR UPDATE OR DELETE ON {name} "
                    f"FOR EACH ROW EXECUTE FUNCTION launchplane_delivery_capture_v1('{kind}', '{identity}', '{step}')"
                )
            )
        else:
            for operation in ("INSERT", "UPDATE", "DELETE"):
                row = "OLD" if operation == "DELETE" else "NEW"
                wake = ""
                if step:
                    wake = f"""UPDATE launchplane_delivery_evidence_sources
                        SET pending = 1, baseline = 0, version = lower(hex(randomblob(16))),
                            queued_at = strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')
                        WHERE key IN (SELECT decision_key FROM launchplane_delivery_evidence_dependencies
                                      WHERE dependency_key = '{name}:' || {row}.{identity});"""
                if operation == "DELETE":
                    capture = f"""DELETE FROM launchplane_delivery_evidence_dependencies WHERE decision_key = '{kind}:' || OLD.{identity};
                        DELETE FROM launchplane_delivery_evidence_sources WHERE key = '{kind}:' || OLD.{identity}"""
                elif kind:
                    capture = projection_insert(
                        "sqlite",
                        f"SELECT {row}.payload AS p, '{kind}' AS kind, {row}.{identity} AS source_id",
                    )
                else:
                    capture = "SELECT 1"
                connection.execute(
                    text(f"""CREATE TRIGGER IF NOT EXISTS {trigger}_{operation.lower()}
                    AFTER {operation} ON {name} BEGIN {wake} {capture}; END""")
                )


def remove_triggers(connection: Connection) -> None:
    postgres = connection.dialect.name == "postgresql"
    for name in sorted(set(SOURCE_KINDS) | STEP_TYPES):
        trigger = name + "_delivery_capture"
        if postgres:
            connection.execute(text(f"DROP TRIGGER IF EXISTS {trigger} ON {name}"))
        else:
            for operation in ("insert", "update", "delete"):
                connection.execute(text(f"DROP TRIGGER IF EXISTS {trigger}_{operation}"))
    if postgres:
        connection.execute(text("DROP FUNCTION IF EXISTS launchplane_delivery_capture_v1()"))
        connection.execute(text("DROP FUNCTION IF EXISTS launchplane_delivery_time_v1(text)"))
