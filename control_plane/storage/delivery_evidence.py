"""Rebuildable delivery-monitor indexes; source records still own every outcome.

There is one compact row per retained source, and at most one pending version
per row. Acknowledgement compares the version read, so a concurrent writer cannot
be acknowledged accidentally. Processed signatures have the source's lifecycle,
not an ever-growing per-product receipt. Release dependencies are bounded by the
release's fixed step plan (including one failure recovery per promotion).
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    Index,
    JSON,
    MetaData,
    String,
    Table,
    and_,
    create_engine,
    delete,
    event,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.pool import NullPool

from control_plane.contracts.deployment_record import DeploymentRecord, deployment_record_passed

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
    sources.c.queued_at,
    sources.c.key,
)
Index(
    "delivery_evidence_pending_product_idx",
    sources.c.product,
    sources.c.pending,
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


def _insert(connection: Connection, table: Table) -> Any:
    return (postgres_insert if connection.dialect.name == "postgresql" else sqlite_insert)(table)


def source_changed(connection: Connection, table: str, payload: Mapping[str, Any]) -> None:
    """Called in the source writer's transaction, including worker state changes."""
    source_id = str(payload.get("operation_id") or payload.get("record_id") or "")
    if table in STEP_TYPES:
        _lock_dependency(connection, table + ":" + source_id)
        connection.execute(
            update(sources)
            .where(
                sources.c.key.in_(
                    select(dependencies.c.decision_key).where(
                        dependencies.c.dependency_key == table + ":" + source_id
                    )
                )
            )
            .values(
                pending=True,
                baseline=False,
                version=uuid4().hex,
                queued_at=datetime.now(timezone.utc).isoformat(),
            )
        )
    kind = SOURCE_KINDS.get(table)
    if kind is None:
        return
    values = _source_values(kind, payload)
    if not values:
        return
    statement = _insert(connection, sources).values(**values)
    # Keep the acknowledged signatures when a source changes.
    connection.execute(
        statement.on_conflict_do_update(
            index_elements=[sources.c.key],
            set_={
                name: value for name, value in values.items() if name not in {"key", "processed"}
            },
        )
    )


def _source_values(kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
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
        record = DeploymentRecord.model_validate(payload)
        values["passed"] = deployment_record_passed(record)
        values["source_key"] = candidate_key(record.source_git_ref)
        if record.artifact_identity:
            values["artifact_key"] = candidate_key(record.artifact_identity.artifact_id)
    for name in ("finished_at", "started_at"):
        values[name] = normalized_time(values[name])
    return values


def source_deleted(connection: Connection, table: str, payload: Mapping[str, Any]) -> None:
    source_changed(
        connection, table, payload
    )  # Wake a dependent decision before removing evidence.
    kind = SOURCE_KINDS.get(table)
    if kind:
        key = kind + ":" + str(payload.get("operation_id") or payload.get("record_id"))
        connection.execute(delete(dependencies).where(dependencies.c.decision_key == key))
        connection.execute(delete(sources).where(sources.c.key == key))


def register_source_events(base: Any) -> None:
    """Mapper events cover merges, worker transitions and atomic evidence bundles."""
    for mapper in base.registry.mappers:
        table = mapper.local_table.name
        if table not in SOURCE_KINDS and table not in STEP_TYPES:
            continue

        def changed(_mapper: Any, connection: Connection, target: Any) -> None:
            source_changed(connection, target.__tablename__, target.payload)

        def removed(_mapper: Any, connection: Connection, target: Any) -> None:
            source_deleted(connection, target.__tablename__, target.payload)

        event.listen(mapper, "after_insert", changed)
        event.listen(mapper, "after_update", changed)
        event.listen(mapper, "after_delete", removed)


@dataclass(frozen=True)
class DeliveryEvidenceWork:
    key: str
    kind: str
    source_id: str
    version: str
    baseline: bool
    processed: dict[str, str]


class DeliveryEvidenceIndex:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.initialization_lock: Callable[[], AbstractContextManager[None]] = nullcontext

    @contextmanager
    def lane_lock(self, context: str, product: str) -> Iterator[bool]:
        name = "production-delivery:" + context + ":" + product
        if self.engine.dialect.name == "postgresql":
            from sqlalchemy import text

            with self.engine.begin() as connection:
                acquired = connection.scalar(
                    text("SELECT pg_try_advisory_xact_lock(hashtextextended(:name, 0))"),
                    {"name": name},
                )
                yield bool(acquired)
            return
        path = Path(str(self.engine.url.database)).with_suffix(
            ".delivery-" + candidate_key(name) + ".lock"
        )
        with path.open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @staticmethod
    def lane_filter(context: str, product: str) -> Any:
        return or_(
            and_(sources.c.context == context, sources.c.instance == "prod"),
            and_(sources.c.kind == "decision", sources.c.product == product),
        )

    def initialize(
        self, context: str, product: str, *, baseline: bool, legacy_seen: dict[str, str]
    ) -> dict[str, Any]:
        key = context + ":" + product
        with self.initialization_lock(), self.engine.begin() as connection:
            checkpoint = connection.scalar(
                select(checkpoints.c.payload).where(checkpoints.c.key == key)
            )
            if checkpoint is not None:
                return dict(checkpoint)
            lane = self.lane_filter(context, product)
            latest = {}
            for kind in SOURCE_KINDS.values():
                latest[kind] = (
                    connection.scalar(
                        select(sources.c.source_id)
                        .where(lane, sources.c.kind == kind)
                        .order_by(
                            sources.c.finished_at.desc(),
                            sources.c.started_at.desc(),
                            sources.c.source_id.desc(),
                        )
                        .limit(1)
                    )
                    or ""
                )
            latest["success"] = (
                connection.scalar(
                    select(sources.c.finished_at)
                    .where(lane, sources.c.kind == "deployment", sources.c.passed.is_(True))
                    .order_by(sources.c.finished_at.desc())
                    .limit(1)
                )
                or ""
            )
            checkpoint = {"latest": latest, "legacy_seen": legacy_seen, "initial": baseline}
            connection.execute(update(sources).where(lane).values(baseline=True))
            connection.execute(
                _insert(connection, checkpoints)
                .values(key=key, payload=checkpoint)
                .on_conflict_do_nothing(index_elements=[checkpoints.c.key])
            )
            return checkpoint

    def pending(
        self, context: str, product: str, *, limit: int, now: str = ""
    ) -> tuple[DeliveryEvidenceWork, ...]:
        cutoff = normalized_time(now or datetime.now(timezone.utc).isoformat())
        # Keep each scope as an independent indexed query. A combined scope OR
        # can make the planner scan all retained history before filtering work.
        scopes = (
            and_(sources.c.context == context, sources.c.instance == "prod"),
            and_(sources.c.product == product, sources.c.kind == "decision"),
        )
        states = (
            sources.c.pending.is_(True),
            and_(
                sources.c.pending.is_(False),
                sources.c.recheck_at > "",
                sources.c.recheck_at <= cutoff,
            ),
        )
        candidates = {}
        with self.engine.connect() as connection:
            for scope in scopes:
                for state in states:
                    for row in connection.execute(
                        select(sources)
                        .where(scope, state)
                        .order_by(sources.c.queued_at, sources.c.key)
                        .limit(limit)
                    ).mappings():
                        candidates[row["key"]] = dict(row)
        rows = sorted(candidates.values(), key=lambda row: (row["queued_at"], row["key"]))[:limit]
        return tuple(
            DeliveryEvidenceWork(
                **{
                    name: row[name]
                    for name in ("key", "kind", "source_id", "version", "baseline", "processed")
                }
            )
            for row in rows
        )

    def start(self, work: DeliveryEvidenceWork) -> None:
        # A timed recheck must become durable pending work before its deadline
        # is recomputed, including if the monitor crashes during persistence.
        with self.engine.begin() as connection:
            connection.execute(
                update(sources)
                .where(sources.c.key == work.key, sources.c.version == work.version)
                .values(pending=True)
            )

    def acknowledge(self, work: DeliveryEvidenceWork, processed: dict[str, str]) -> None:
        with self.engine.begin() as connection:
            allowed = {work.source_id}
            if work.kind == "decision":
                allowed = {
                    key.split(":", 1)[1]
                    for key in connection.execute(
                        select(dependencies.c.dependency_key).where(
                            dependencies.c.decision_key == work.key
                        )
                    ).scalars()
                }
            connection.execute(
                update(sources)
                .where(sources.c.key == work.key, sources.c.version == work.version)
                .values(
                    pending=False,
                    baseline=False,
                    processed={key: value for key, value in processed.items() if key in allowed},
                )
            )

    def reconcile_file(self, work: DeliveryEvidenceWork, payload: Mapping[str, Any]) -> None:
        values = _source_values(work.kind, payload)
        projection = {
            key: values[key]
            for key in ("finished_at", "started_at", "passed", "artifact_key", "source_key")
        }
        with self.engine.begin() as connection:
            connection.execute(
                update(sources)
                .where(sources.c.key == work.key, sources.c.version == work.version)
                .values(**projection)
            )

    def forget_missing(self, key: str, version: str = "") -> None:
        with self.engine.begin() as connection:
            condition = sources.c.key == key
            if version:
                condition = and_(condition, sources.c.version == version)
            connection.execute(delete(sources).where(condition))

    def retry_later(self, work: DeliveryEvidenceWork) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                update(sources)
                .where(sources.c.key == work.key, sources.c.version == work.version)
                .values(queued_at=datetime.now(timezone.utc).isoformat())
            )

    def bind(self, decision_id: str, dependency_keys: tuple[str, ...]) -> None:
        with self.engine.begin() as connection:
            for key in sorted(dependency_keys):
                _lock_dependency(connection, key)
            connection.execute(
                delete(dependencies).where(
                    dependencies.c.decision_key == "decision:" + decision_id,
                    dependencies.c.dependency_key.not_in(dependency_keys),
                )
            )
            next_checks = []
            for key in dependency_keys:
                if key.startswith("launchplane_idempotency_records:"):
                    from sqlalchemy import table, column

                    reservations = table(
                        "launchplane_idempotency_records",
                        column("record_id", String),
                        column("payload", JSON),
                    )
                    payload = connection.scalar(
                        select(reservations.c.payload).where(
                            reservations.c.record_id == key.split(":", 1)[1]
                        )
                    )
                    if payload and payload.get("state") == "running":
                        expiry = normalized_time(payload.get("lease_expires_at", ""))
                        if expiry > datetime.now(timezone.utc).isoformat():
                            next_checks.append(expiry)
                connection.execute(
                    _insert(connection, dependencies)
                    .values(dependency_key=key, decision_key="decision:" + decision_id)
                    .on_conflict_do_nothing()
                )

            connection.execute(
                update(sources)
                .where(sources.c.key == "decision:" + decision_id)
                .values(recheck_at=min(next_checks, default=""))
            )

    def has_release_dependency(self, dependency_key: str) -> bool:
        with self.engine.connect() as connection:
            return (
                connection.scalar(
                    select(dependencies.c.decision_key)
                    .where(dependencies.c.dependency_key == dependency_key)
                    .limit(1)
                )
                is not None
            )

    def latest_deployment_id(self, context: str) -> str:
        with self.engine.connect() as connection:
            return str(
                connection.scalar(
                    select(sources.c.source_id)
                    .where(
                        sources.c.kind == "deployment",
                        sources.c.context == context,
                        sources.c.instance == "prod",
                    )
                    .order_by(
                        sources.c.finished_at.desc(),
                        sources.c.started_at.desc(),
                        sources.c.source_id.desc(),
                    )
                    .limit(1)
                )
                or ""
            )

    def matching_success(
        self, context: str, key: str, opened_at: str, verify: Callable[[str], bool] | None = None
    ) -> bool:
        with self.engine.connect() as connection:
            for column in (sources.c.artifact_key, sources.c.source_key):
                source_id = connection.scalar(
                    select(sources.c.source_id)
                    .where(
                        sources.c.context == context,
                        sources.c.instance == "prod",
                        sources.c.passed.is_(True),
                        column == key,
                        sources.c.finished_at > normalized_time(opened_at),
                    )
                    .limit(1)
                )
                if source_id and (verify is None or verify(source_id)):
                    return True
        return False

    def finish_bootstrap(self, context: str, product: str) -> None:
        with self.engine.begin() as connection:
            checkpoint = connection.scalar(
                select(checkpoints.c.payload).where(checkpoints.c.key == context + ":" + product)
            )
            if (
                checkpoint is not None
                and not checkpoint.get("latest")
                and not checkpoint.get("legacy_seen")
            ):
                return
            pending_baseline = connection.scalar(
                select(sources.c.key)
                .where(
                    self.lane_filter(context, product),
                    sources.c.baseline.is_(True),
                    sources.c.pending.is_(True),
                )
                .limit(1)
            )
            if pending_baseline is None:
                connection.execute(
                    update(checkpoints)
                    .where(checkpoints.c.key == context + ":" + product)
                    .values(payload={"latest": {}, "legacy_seen": {}, "initial": False})
                )


@contextmanager
def filesystem_index(state_dir: Path) -> Iterator[DeliveryEvidenceIndex]:
    """Caller holds the filesystem store's bundle lock; first use backfills once."""
    state_dir.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        "sqlite+pysqlite:///" + str(state_dir / ".delivery-evidence.sqlite3"), poolclass=NullPool
    )
    try:
        metadata.create_all(engine)
        with engine.begin() as connection:
            if (
                connection.scalar(
                    select(checkpoints.c.key).where(checkpoints.c.key == "filesystem-backfill")
                )
                is None
            ):
                for directory, table in FILESYSTEM_TYPES.items():
                    for path in (state_dir / directory).glob("*.json"):
                        source_changed(connection, table, json.loads(path.read_text()))
                connection.execute(
                    checkpoints.insert().values(key="filesystem-backfill", payload={})
                )
        yield DeliveryEvidenceIndex(engine)
    finally:
        engine.dispose()


def install_delete_triggers(connection: Connection) -> None:
    """Bulk retention deletes must retire derived state as well as ORM deletes."""
    from sqlalchemy import text

    if connection.dialect.name == "postgresql":
        connection.execute(
            text("""
            CREATE OR REPLACE FUNCTION launchplane_delivery_source_deleted() RETURNS trigger
            LANGUAGE plpgsql AS $$
            DECLARE deleted_source_id text := to_jsonb(OLD)->>TG_ARGV[1];
                    evidence_key text := TG_ARGV[0] || ':' || deleted_source_id;
            BEGIN
                PERFORM pg_advisory_xact_lock(hashtextextended(
                    'delivery-dependency:' || TG_TABLE_NAME || ':' || deleted_source_id, 0));
                UPDATE launchplane_delivery_evidence_sources
                SET pending = true, baseline = false,
                    version = md5(random()::text || clock_timestamp()::text)
                WHERE key IN (SELECT decision_key FROM launchplane_delivery_evidence_dependencies
                              WHERE dependency_key = TG_TABLE_NAME || ':' || deleted_source_id);
                DELETE FROM launchplane_delivery_evidence_dependencies WHERE decision_key = evidence_key;
                DELETE FROM launchplane_delivery_evidence_sources WHERE key = evidence_key;
                RETURN OLD;
            END $$
        """)
        )
    for table_name in sorted(set(SOURCE_KINDS) | STEP_TYPES):
        kind = SOURCE_KINDS.get(table_name, "")
        field = "operation_id" if table_name.endswith("_operations") else "record_id"
        trigger = table_name + "_delivery_delete"
        if connection.dialect.name == "postgresql":
            connection.execute(text(f"DROP TRIGGER IF EXISTS {trigger} ON {table_name}"))
            connection.execute(
                text(
                    f"CREATE TRIGGER {trigger} AFTER DELETE ON {table_name} FOR EACH ROW "
                    f"EXECUTE FUNCTION launchplane_delivery_source_deleted('{kind}', '{field}')"
                )
            )
        else:
            key = f"'{kind}:' || OLD.{field}"
            connection.execute(
                text(f"""
                CREATE TRIGGER IF NOT EXISTS {trigger} AFTER DELETE ON {table_name}
                BEGIN
                    UPDATE launchplane_delivery_evidence_sources
                    SET pending = 1, baseline = 0, version = hex(randomblob(16))
                    WHERE key IN (SELECT decision_key FROM launchplane_delivery_evidence_dependencies
                                  WHERE dependency_key = '{table_name}:' || OLD.{field});
                    DELETE FROM launchplane_delivery_evidence_dependencies WHERE decision_key = {key};
                    DELETE FROM launchplane_delivery_evidence_sources WHERE key = {key};
                END
            """)
            )


def remove_delete_triggers(connection: Connection) -> None:
    from sqlalchemy import text

    for table_name in sorted(set(SOURCE_KINDS) | STEP_TYPES):
        suffix = " ON " + table_name if connection.dialect.name == "postgresql" else ""
        connection.execute(text(f"DROP TRIGGER IF EXISTS {table_name}_delivery_delete{suffix}"))
    if connection.dialect.name == "postgresql":
        connection.execute(text("DROP FUNCTION IF EXISTS launchplane_delivery_source_deleted()"))


def _lock_dependency(connection: Connection, key: str) -> None:
    # Binding and worker completion must serialize even when the operation was
    # created before its first monitor pass. Otherwise an uncommitted completion
    # could miss the new binding while the monitor still reads its old outcome.
    if connection.dialect.name == "postgresql":
        from sqlalchemy import text

        connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": "delivery-dependency:" + key},
        )
