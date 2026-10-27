from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import re
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

from control_plane.storage.migrations.versions import (
    e7c1d5e9a3b6_drop_retired_delivery_tables as retirement,
)
from control_plane.storage.schema_migration import (
    SCHEMA_MIGRATION_TARGET_REVISION,
    migrate_schema_on_connection,
    schema_migration_action,
    schema_migration_target,
)
from control_plane.storage.schema_invariants import verify_postgres_schema_invariants
from control_plane.database_maintenance_host import RecoveryRequired


MAINTENANCE_LOCKS = (
    "launchplane:schema-migration",
    "launchplane:service-deploy-provider-dispatch",
    "launchplane:service-deploy-release-admission",
)


def require_no_other_clients(
    connection: Connection, *, dump_client: dict[str, Any] | None = None
) -> None:
    connection.execute(text("SELECT pg_stat_clear_snapshot()"))
    clients = (
        connection.execute(
            text(
                "SELECT pid,backend_start,application_name,backend_xid,client_addr IS NULL AS local,usename=current_user AS own_role "
                "FROM pg_stat_activity WHERE datname=current_database() AND backend_type='client backend' AND pid<>pg_backend_pid()"
            )
        )
        .mappings()
        .all()
    )
    if dump_client is not None and len(clients) == 1:
        client = clients[0]
        if (
            client["application_name"] == dump_client["name"]
            and client["local"]
            and client["own_role"]
            and client["backend_xid"] is None
        ):
            identity = (client["pid"], client["backend_start"])
            if "identity" not in dump_client:
                dump_client["identity"] = identity
            if dump_client["identity"] == identity:
                clients = []
    if clients:
        raise RuntimeError("Another database client is present; the writer hold is unproved.")
    if connection.scalar(
        text("SELECT count(*) FROM pg_prepared_xacts WHERE database=current_database()")
    ):
        raise RuntimeError("A prepared transaction is present.")


@contextmanager
def locked_database(
    connection: Connection,
    *,
    expires_at: datetime,
    disconnect_clients: Callable[[], None] | None = None,
) -> Iterator[Connection]:
    if connection.dialect.name != "postgresql" or expires_at.tzinfo is None:
        raise ValueError("A PostgreSQL database and timezone-aware deadline are required.")
    if connection.in_transaction():
        raise ValueError("The keeper needs an idle connection before taking its snapshot.")
    acquired = []
    try:
        for key in MAINTENANCE_LOCKS:
            if not connection.scalar(
                text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))"), {"key": key}
            ):
                raise RuntimeError(
                    "A migration, release or self-deploy still owns its operation lock."
                )
            acquired.append(key)
        tables = sorted(inspect(connection).get_table_names(schema="public"))
        connection.commit()
        with connection.execution_options(isolation_level="REPEATABLE READ").begin():
            connection.execute(text("SET LOCAL lock_timeout='1s'"))
            names = ", ".join(
                "public." + connection.dialect.identifier_preparer.quote(name) for name in tables
            )
            if not names:
                raise RuntimeError("No application tables were found.")
            connection.execute(text(f"LOCK TABLE {names} IN EXCLUSIVE MODE NOWAIT"))
            if disconnect_clients is not None:
                disconnect_clients()
            if tables != sorted(inspect(connection).get_table_names(schema="public")):
                raise RuntimeError("The table set changed while acquiring the hold.")
            remaining = (
                expires_at - connection.scalar(text("SELECT clock_timestamp()"))
            ).total_seconds()
            if not 0 < remaining <= 3600:
                raise ValueError("The maintenance deadline must be within one hour.")
            connection.execute(
                text("SELECT set_config('transaction_timeout', :budget, true)"),
                {"budget": str(max(1, int(remaining * 1000)))},
            )
            connection.execute(text("SET LOCAL idle_in_transaction_session_timeout=0"))
            require_no_other_clients(connection)
            unsupported = connection.scalar(
                text(
                    "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema' "
                    "AND c.relkind IN ('r','p','f','m') "
                    "AND (n.nspname<>'public' OR c.relkind IN ('f','m'))"
                )
            )
            if unsupported or connection.scalar(
                text("SELECT count(*) FROM pg_largeobject_metadata")
            ):
                raise RuntimeError("Unsupported database objects require a qualified hold first.")
            yield connection
    finally:
        connection.rollback()
        if not connection.invalidated:
            for key in reversed(acquired):
                connection.execute(
                    text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"), {"key": key}
                )
            connection.commit()


def database_snapshot(connection: Connection) -> dict[str, Any]:
    for setting, value in (
        ("TimeZone", "UTC"),
        ("DateStyle", "ISO, MDY"),
        ("IntervalStyle", "postgres"),
        ("extra_float_digits", "3"),
        ("bytea_output", "hex"),
    ):
        connection.execute(
            text("SELECT set_config(:setting, :value, true)"), {"setting": setting, "value": value}
        )
    result: dict[str, Any] = {}
    for name in sorted(inspect(connection).get_table_names(schema="public")):
        quoted = connection.dialect.identifier_preparer.quote(name)
        columns = connection.execute(
            text(
                "SELECT a.attname, format_type(a.atttypid,a.atttypmod), a.attnotnull, "
                "a.attidentity, a.attgenerated, pg_get_expr(d.adbin,d.adrelid) "
                "FROM pg_attribute a LEFT JOIN pg_attrdef d "
                "ON d.adrelid=a.attrelid AND d.adnum=a.attnum "
                "WHERE a.attrelid=to_regclass(:name) AND a.attnum>0 AND NOT a.attisdropped "
                "ORDER BY a.attnum"
            ),
            {"name": f"public.{quoted}"},
        ).all()
        digest = hashlib.sha256()
        count = 0
        primary_key = (
            inspect(connection).get_pk_constraint(name, schema="public").get("constrained_columns")
            or []
        )
        ordering = (
            ", ".join(
                connection.dialect.identifier_preparer.quote(key) + '::text COLLATE "C"'
                for key in primary_key
            )
            or 'to_jsonb(t)::text COLLATE "C"'
        )
        with connection.execute(
            text(
                f"SELECT to_jsonb(t)::text FROM public.{quoted} t ORDER BY {ordering}"
            ).execution_options(stream_results=True)
        ) as rows:
            for row in rows:
                encoded = str(row[0]).encode()
                digest.update(len(encoded).to_bytes(8))
                digest.update(encoded)
                count += 1
        metadata = {}
        for category, query in (
            (
                "constraints",
                "SELECT pg_get_constraintdef(oid, true) FROM pg_constraint WHERE conrelid=to_regclass(:name) ORDER BY conname",
            ),
            (
                "indexes",
                "SELECT indexdef FROM pg_indexes WHERE schemaname='public' AND tablename=:table ORDER BY indexname",
            ),
            (
                "triggers",
                "SELECT jsonb_build_array(pg_get_triggerdef(oid, true), tgenabled, pg_get_functiondef(tgfoid))::text "
                "FROM pg_trigger WHERE tgrelid=to_regclass(:name) AND NOT tgisinternal ORDER BY tgname",
            ),
        ):
            metadata[category] = list(
                connection.execute(
                    text(query), {"name": f"public.{quoted}", "table": name}
                ).scalars()
            )
        metadata["columns"] = [list(row) for row in columns]
        result[name] = {
            "schema_sha256": hashlib.sha256(
                json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "rows": count,
            "sha256": digest.hexdigest(),
        }
    sequences = {}
    for name, owner in connection.execute(
        text(
            "SELECT c.relname, t.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "LEFT JOIN pg_depend d ON d.objid=c.oid AND d.classid='pg_class'::regclass AND d.deptype IN ('a','i') "
            "LEFT JOIN pg_class t ON t.oid=d.refobjid WHERE c.relkind='S' AND n.nspname='public' ORDER BY c.relname"
        )
    ).all():
        quoted = connection.dialect.identifier_preparer.quote(name)
        values = connection.execute(
            text(f"SELECT last_value, is_called FROM public.{quoted}")
        ).one()
        sequences[name] = {
            "owner": owner,
            "sha256": hashlib.sha256(json.dumps(list(values)).encode()).hexdigest(),
        }
    result["__sequences__"] = sequences
    return result


def retained_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    result = {
        name: value
        for name, value in snapshot.items()
        if name not in retirement.RETIRED_TABLES and name != "alembic_version"
    }
    result["__sequences__"] = {
        name: value
        for name, value in snapshot.get("__sequences__", {}).items()
        if value["owner"] not in retirement.RETIRED_TABLES
    }
    return result


def database_system_identifier(connection: Connection) -> str:
    return str(connection.scalar(text("SELECT system_identifier FROM pg_control_system()")))


def require_isolated_rehearsal(connection: Connection, source_identifier: object) -> str:
    current = database_system_identifier(connection)
    if (
        not isinstance(source_identifier, str)
        or not source_identifier
        or current == source_identifier
    ):
        raise RuntimeError("The rehearsal must run in a different PostgreSQL cluster.")
    return current


def require_retired_functions_absent(connection: Connection) -> None:
    if connection.scalar(
        text(
            "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='public' AND p.proname=ANY(:names)"
        ),
        {"names": list(retirement.RETIRED_TRIGGER_FUNCTIONS)},
    ):
        raise RuntimeError("A retired trigger function remains after the migration.")


def apply_retirement(
    connection: Connection, *, before: dict[str, Any], rehearsal: dict[str, Any]
) -> str:
    require_no_other_clients(connection)
    require_isolated_rehearsal(connection, rehearsal.get("system_identifier"))
    if rehearsal.get("restored") != before or rehearsal.get("retained") != retained_snapshot(
        before
    ):
        raise RuntimeError("The actual restored-backup rehearsal does not match the held database.")
    if not isinstance(rehearsal.get("backup_sha256"), str) or not re.fullmatch(
        r"[0-9a-f]{64}", rehearsal["backup_sha256"]
    ):
        raise RuntimeError("The verified private backup identity is missing.")
    if database_snapshot(connection) != before:
        raise RuntimeError("The held database changed after the verified backup.")
    if set(retirement.RETIRED_TABLES) - before.keys():
        raise RuntimeError("The approved retirement table set is incomplete.")
    current = str(connection.scalar(text("SELECT version_num FROM alembic_version")))
    target = schema_migration_target(current_revision=current, target_revision=retirement.revision)
    if rehearsal.get("revision") != target:
        raise RuntimeError("The rehearsal did not reach the planned revision.")
    migrate_schema_on_connection(connection, target_revision=retirement.revision)
    after = database_snapshot(connection)
    retained = retained_snapshot(after)
    if retained != rehearsal["retained"]:
        raise RuntimeError("The migration changed retained tables.")
    if set(before) - set(after) != set(retirement.RETIRED_TABLES):
        raise RuntimeError("The migration removed an unexpected table set.")
    final_revision = str(connection.scalar(text("SELECT version_num FROM alembic_version")))
    if final_revision != target:
        raise RuntimeError("The migration revision readback disagrees.")
    if schema_migration_action(
        current_revision=final_revision, target_revision=SCHEMA_MIGRATION_TARGET_REVISION
    ) not in {"current", "compatible_ahead"}:
        raise RuntimeError("The migrated schema is incompatible with the running image.")
    require_retired_functions_absent(connection)
    verify_postgres_schema_invariants(connection)
    return final_revision


def verify_committed_database(connection: Connection, proof: dict[str, Any]) -> str:
    if database_system_identifier(connection) != proof["system_identifier"]:
        raise RecoveryRequired("The recovery readback belongs to another database cluster.")
    require_no_other_clients(connection)
    observed = database_snapshot(connection)
    if observed not in (proof["before"], proof["after"]):
        raise RecoveryRequired("Committed state disagrees with both verified maintenance states.")
    try:
        verify_postgres_schema_invariants(connection)
        if observed == proof["after"] and proof["after"] != proof["before"]:
            require_retired_functions_absent(connection)
    except RuntimeError as error:
        raise RecoveryRequired("Committed schema invariants require recovery.") from error
    return str(connection.scalar(text("SELECT version_num FROM alembic_version")))


def disconnect_frozen_clients(connection: Connection, addresses: set[str]) -> None:
    connection.execute(text("SELECT pg_stat_clear_snapshot()"))
    if connection.scalar(
        text("SELECT count(*) FROM pg_prepared_xacts WHERE database=current_database()")
    ):
        raise RuntimeError("A prepared transaction prevents frozen-client admission.")
    clients = (
        connection.execute(
            text(
                "SELECT pid,backend_start,host(client_addr) AS address,state,backend_xid,usename "
                "FROM pg_stat_activity WHERE datname=current_database() "
                "AND backend_type='client backend' AND pid<>pg_backend_pid()"
            )
        )
        .mappings()
        .all()
    )
    if any(
        row["address"] not in addresses
        or row["state"] not in {"idle", "idle in transaction"}
        or row["backend_xid"] is not None
        or row["usename"] != connection.scalar(text("SELECT current_user"))
        for row in clients
    ):
        raise RuntimeError("A frozen database client has unresolved identity or active work.")
    if connection.scalar(
        text(
            "SELECT count(*) FROM pg_locks WHERE pid=ANY(:pids) AND "
            "(locktype='advisory' OR (locktype='relation' AND mode<>'AccessShareLock'))"
        ),
        {"pids": [int(row["pid"]) for row in clients]},
    ):
        raise RuntimeError("A frozen client still owns an execution or write lock.")
    for row in clients:
        connection.execute(text("SELECT pg_stat_clear_snapshot()"))
        terminated = connection.scalar(
            text(
                "SELECT pg_terminate_backend(pid,5000) FROM pg_stat_activity "
                "WHERE pid=:pid AND backend_start=:started AND datname=current_database() "
                "AND usename=current_user AND host(client_addr)=:address "
                "AND state IN ('idle','idle in transaction') AND backend_xid IS NULL"
            ),
            {"pid": row["pid"], "started": row["backend_start"], "address": row["address"]},
        )
        if terminated is not True:
            raise RuntimeError("A frozen client did not settle with exact termination readback.")
    require_no_other_clients(connection)
