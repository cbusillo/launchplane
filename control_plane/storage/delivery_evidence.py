"""Delivery-monitor indexes and source-bound checkpoints; source records still own every outcome.

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
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
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


from control_plane.storage.migrations.delivery_evidence_v1 import (
    metadata as metadata,
    sources as sources,
    dependencies as dependencies,
    checkpoints as checkpoints,
    SOURCE_KINDS as SOURCE_KINDS,
    FILESYSTEM_TYPES as FILESYSTEM_TYPES,
    STEP_TYPES as STEP_TYPES,
    normalized_time as normalized_time,
    candidate_key as candidate_key,
    source_values,
    install_triggers as install_triggers,
)


def _insert(connection: Connection, table: Table) -> Any:
    return (postgres_insert if connection.dialect.name == "postgresql" else sqlite_insert)(table)


def source_changed(
    connection: Connection, table: str, payload: Mapping[str, Any], *, backfill: bool = False
) -> None:
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
    values = source_values(kind, payload)
    if not values:
        return
    values["baseline"] = backfill
    statement = _insert(connection, sources).values(**values)
    if backfill:
        connection.execute(statement.on_conflict_do_nothing(index_elements=[sources.c.key]))
        return
    # Keep the acknowledged signatures when a source changes.
    connection.execute(
        statement.on_conflict_do_update(
            index_elements=[sources.c.key],
            set_={
                name: value for name, value in values.items() if name not in {"key", "processed"}
            },
        )
    )


def register_source_events(base: Any) -> None:
    # SQLite cannot compute SHA-256 in portable SQL. Current writers enrich its
    # transactional trigger marker; raw/old writes stay pending until enrichment
    # by the monitor. PostgreSQL capture is entirely database-owned.
    for mapper in base.registry.mappers:
        if mapper.local_table.name not in SOURCE_KINDS:
            continue

        def changed(_mapper: Any, connection: Connection, target: Any) -> None:
            if connection.dialect.name == "sqlite":
                source_changed(connection, target.__tablename__, target.payload)

        event.listen(mapper, "after_insert", changed)
        event.listen(mapper, "after_update", changed)


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

    def checkpoint(self, context: str, product: str) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            payload = connection.scalar(
                select(checkpoints.c.payload).where(checkpoints.c.key == context + ":" + product)
            )
            return dict(payload) if payload is not None else None

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
            if latest["success"]:
                # Do not freeze a corrupt successful-delivery time into a
                # checkpoint that could never recover after source repair.
                datetime.fromisoformat(str(latest["success"]).replace("Z", "+00:00"))
            checkpoint = {"latest": latest, "legacy_seen": legacy_seen, "initial": baseline}
            if baseline:
                connection.execute(update(sources).where(lane).values(baseline=True))
            elif legacy_seen:
                identities = list(legacy_seen)
                for offset in range(0, len(identities), 500):
                    connection.execute(
                        update(sources)
                        .where(lane, sources.c.source_id.in_(identities[offset : offset + 500]))
                        .values(baseline=True)
                    )
            connection.execute(
                _insert(connection, checkpoints)
                .values(key=key, payload=checkpoint)
                .on_conflict_do_nothing(index_elements=[checkpoints.c.key])
            )
            return checkpoint

    def pending(
        self,
        context: str,
        product: str,
        *,
        limit: int,
        now: str = "",
        initial_latest: Mapping[str, Any] | None = None,
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
        priority = {
            kind + ":" + identity
            for kind, identity in (initial_latest or {}).items()
            if kind in SOURCE_KINDS.values() and identity
        }
        candidates = {}
        with self.engine.connect() as connection:
            for scope in scopes:
                for state in states:
                    for row in connection.execute(
                        select(sources)
                        .where(scope, state)
                        .order_by(sources.c.baseline, sources.c.queued_at, sources.c.key)
                        .limit(limit)
                    ).mappings():
                        candidates[row["key"]] = dict(row)
            if priority:
                # The previous scanner surfaced the newest historical failure
                # immediately. Read those fixed bootstrap identities by key so
                # they cannot wait behind many otherwise silent history pages.
                for row in connection.execute(
                    select(sources).where(
                        self.lane_filter(context, product),
                        sources.c.key.in_(priority),
                        sources.c.pending.is_(True),
                    )
                ).mappings():
                    candidates[row["key"]] = dict(row)
        rows = sorted(
            candidates.values(),
            key=lambda row: (
                row["baseline"],
                row["key"] not in priority if row["baseline"] else False,
                row["queued_at"],
                row["key"],
            ),
        )[:limit]
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

    def reconcile_source(self, work: DeliveryEvidenceWork, payload: Mapping[str, Any]) -> None:
        values = source_values(work.kind, payload)
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

    def bind(self, decision_id: str, dependency_keys: tuple[str, ...], *, now: str = "") -> None:
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
                        if expiry > normalized_time(now or datetime.now(timezone.utc).isoformat()):
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
def filesystem_index(state_dir: Path, *, backfill: bool = True) -> Iterator[DeliveryEvidenceIndex]:
    """Caller holds the filesystem store's bundle lock; first use backfills once."""
    state_dir.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        "sqlite+pysqlite:///" + str(state_dir / ".delivery-evidence.sqlite3"), poolclass=NullPool
    )
    try:
        metadata.create_all(engine)
        with engine.begin() as connection:
            if backfill and (
                connection.scalar(
                    select(checkpoints.c.key).where(checkpoints.c.key == "filesystem-backfill")
                )
                is None
            ):
                for directory, table in FILESYSTEM_TYPES.items():
                    for path in (state_dir / directory).glob("*.json"):
                        source_changed(
                            connection, table, json.loads(path.read_text()), backfill=True
                        )
                connection.execute(
                    checkpoints.insert().values(key="filesystem-backfill", payload={})
                )
        yield DeliveryEvidenceIndex(engine)
    finally:
        engine.dispose()


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
