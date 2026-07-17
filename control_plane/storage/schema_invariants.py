from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

EXPECTED_ALEMBIC_HEAD_REVISION = "f3b5d7e9a1c2"


class SchemaInspectorProtocol(Protocol):
    def get_indexes(self, table_name: str) -> Sequence[Mapping[str, object]]:
        raise NotImplementedError

    def get_columns(self, table_name: str) -> Sequence[Mapping[str, object]]:
        raise NotImplementedError

    def get_pk_constraint(self, table_name: str) -> Mapping[str, object]:
        raise NotImplementedError


@dataclass(frozen=True)
class CriticalColumnType:
    table_name: str
    column_name: str
    accepted_type_tokens: tuple[str, ...]


@dataclass(frozen=True)
class CriticalIndex:
    table_name: str
    index_name: str
    column_names: tuple[str, ...]
    unique: bool = False
    predicate_tokens: tuple[str, ...] = ()


@dataclass(frozen=True)
class CriticalPrimaryKey:
    table_name: str
    column_names: tuple[str, ...]


CRITICAL_POSTGRES_COLUMN_TYPES: tuple[CriticalColumnType, ...] = (
    CriticalColumnType(
        "launchplane_idempotency_records",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_idempotency_records",
        "response_status_code",
        ("integer", "int4"),
    ),
    CriticalColumnType(
        "launchplane_idempotency_records",
        "attempt",
        ("integer", "int4"),
    ),
    CriticalColumnType(
        "launchplane_every_code_work_requests",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_every_code_work_requests",
        "fencing_token",
        ("integer", "int4"),
    ),
    CriticalColumnType(
        "launchplane_every_code_work_requests",
        "attempt",
        ("integer", "int4"),
    ),
    CriticalColumnType(
        "launchplane_odoo_stable_bootstrap_operations",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_odoo_stable_bootstrap_operations",
        "attempt",
        ("integer", "int4"),
    ),
    CriticalColumnType(
        "launchplane_odoo_stable_target_replacement_operations",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_odoo_stable_target_replacement_operations",
        "attempt",
        ("integer", "int4"),
    ),
    CriticalColumnType(
        "launchplane_verireel_prod_backup_gate_operations",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_verireel_prod_backup_gate_operations",
        "attempt",
        ("integer", "int4"),
    ),
    CriticalColumnType(
        "launchplane_route_bindings",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_outbox_deliveries",
        "payload",
        ("jsonb",),
    ),
    CriticalColumnType(
        "launchplane_outbox_deliveries",
        "attempt",
        ("integer", "int4"),
    ),
    CriticalColumnType(
        "launchplane_outbox_deliveries",
        "max_attempts",
        ("integer", "int4"),
    ),
    CriticalColumnType(
        "launchplane_merge_train_controller_states",
        "payload",
        ("jsonb",),
    ),
)

_ACTIVE_OPERATION_PREDICATE_TOKENS = ("status", "pending", "running")

CRITICAL_SCHEMA_INDEXES: tuple[CriticalIndex, ...] = (
    CriticalIndex(
        "launchplane_idempotency_records",
        "launchplane_idempotency_scope_route_key_idx",
        ("scope", "route_path", "idempotency_key"),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_idempotency_records",
        "launchplane_idempotency_state_lease_idx",
        ("state", "lease_expires_at", "updated_at"),
    ),
    CriticalIndex(
        "launchplane_idempotency_records",
        "launchplane_idempotency_active_reconciliation_idx",
        ("provider_target_key",),
        unique=True,
        predicate_tokens=("provider_target_key", "running", "reconcile_required"),
    ),
    CriticalIndex(
        "launchplane_every_code_work_requests",
        "launchplane_every_code_work_requests_lease_idx",
        ("state", "lease_expires_at"),
    ),
    CriticalIndex(
        "launchplane_odoo_stable_bootstrap_operations",
        "launchplane_odoo_bootstrap_operation_idempotency_idx",
        ("idempotency_key", "updated_at"),
    ),
    CriticalIndex(
        "launchplane_odoo_stable_bootstrap_operations",
        "launchplane_odoo_bootstrap_active_lane_uidx",
        ("product", "context", "instance"),
        unique=True,
        predicate_tokens=_ACTIVE_OPERATION_PREDICATE_TOKENS,
    ),
    CriticalIndex(
        "launchplane_odoo_stable_bootstrap_operations",
        "launchplane_odoo_bootstrap_worker_claim_idx",
        ("status", "lease_expires_at", "updated_at"),
    ),
    CriticalIndex(
        "launchplane_odoo_stable_target_replacement_operations",
        "launchplane_odoo_replacement_operation_idempotency_idx",
        ("idempotency_scope", "idempotency_key", "updated_at"),
    ),
    CriticalIndex(
        "launchplane_odoo_stable_target_replacement_operations",
        "launchplane_odoo_replacement_active_lane_uidx",
        ("product", "context", "instance"),
        unique=True,
        predicate_tokens=_ACTIVE_OPERATION_PREDICATE_TOKENS,
    ),
    CriticalIndex(
        "launchplane_odoo_stable_target_replacement_operations",
        "launchplane_odoo_replacement_worker_claim_idx",
        ("status", "lease_expires_at", "updated_at"),
    ),
    CriticalIndex(
        "launchplane_verireel_prod_backup_gate_operations",
        "launchplane_verireel_backup_gate_active_record_uidx",
        ("backup_record_id",),
        unique=True,
        predicate_tokens=_ACTIVE_OPERATION_PREDICATE_TOKENS,
    ),
    CriticalIndex(
        "launchplane_verireel_prod_backup_gate_operations",
        "launchplane_verireel_backup_gate_worker_claim_idx",
        ("status", "lease_expires_at", "updated_at"),
    ),
    CriticalIndex(
        "launchplane_route_bindings",
        "launchplane_route_bindings_lookup_idx",
        ("product", "context", "status", "instance"),
    ),
    CriticalIndex(
        "launchplane_route_bindings",
        "launchplane_route_bindings_updated_idx",
        ("updated_at",),
    ),
    CriticalIndex(
        "launchplane_outbox_deliveries",
        "launchplane_outbox_deliveries_dedupe_uidx",
        ("dedupe_key",),
        unique=True,
    ),
    CriticalIndex(
        "launchplane_outbox_deliveries",
        "launchplane_outbox_deliveries_claim_idx",
        ("state", "next_attempt_at", "lease_expires_at", "created_at"),
    ),
    CriticalIndex(
        "launchplane_merge_train_controller_states",
        "launchplane_merge_train_controller_states_repository_base_idx",
        ("repository", "base_branch", "updated_at"),
    ),
    CriticalIndex(
        "launchplane_merge_train_controller_states",
        "launchplane_merge_train_controller_states_status_idx",
        ("status", "updated_at"),
    ),
    CriticalIndex(
        "launchplane_merge_train_controller_states",
        "launchplane_merge_train_controller_states_lease_idx",
        ("status", "lease_expires_at", "updated_at"),
    ),
)

CRITICAL_PRIMARY_KEYS: tuple[CriticalPrimaryKey, ...] = (
    CriticalPrimaryKey(
        "launchplane_route_bindings",
        ("product", "context", "instance"),
    ),
)


def verify_postgres_schema_invariants(engine: Engine) -> None:
    backend_name = engine.url.get_backend_name()
    if backend_name != "postgresql":
        raise RuntimeError(
            "Launchplane shared storage requires PostgreSQL for hosted service startup; "
            f"got {backend_name!r}."
        )
    _verify_alembic_head(engine)
    inspector = inspect(engine)
    errors = [
        *critical_column_type_errors(inspector),
        *critical_index_errors(
            inspector=inspector,
            table_names=set(inspector.get_table_names()),
            index_definitions=postgres_index_definitions(engine),
        ),
        *critical_primary_key_errors(
            inspector,
            table_names=set(inspector.get_table_names()),
        ),
    ]
    if errors:
        joined_errors = "; ".join(errors)
        raise RuntimeError(
            "Launchplane shared storage schema is missing required PostgreSQL "
            f"invariant(s): {joined_errors}. Run Alembic migrations before "
            "starting the hosted service."
        )


def critical_column_type_errors(
    inspector: SchemaInspectorProtocol,
    *,
    table_names: set[str] | None = None,
) -> list[str]:
    errors: list[str] = []
    for expected_type in CRITICAL_POSTGRES_COLUMN_TYPES:
        if table_names is not None and expected_type.table_name not in table_names:
            continue
        columns = {
            str(column.get("name", "")): column
            for column in inspector.get_columns(expected_type.table_name)
        }
        column = columns.get(expected_type.column_name)
        if column is None:
            errors.append(
                f"{expected_type.table_name}.{expected_type.column_name} missing type metadata"
            )
            continue
        observed_type = _normalized_type_name(column.get("type"))
        if not any(token in observed_type for token in expected_type.accepted_type_tokens):
            errors.append(
                f"{expected_type.table_name}.{expected_type.column_name} has type "
                f"{observed_type or '<unknown>'}; expected one of "
                f"{', '.join(expected_type.accepted_type_tokens)}"
            )
    return errors


def critical_primary_key_errors(
    inspector: SchemaInspectorProtocol,
    *,
    table_names: set[str] | None = None,
) -> list[str]:
    errors: list[str] = []
    for expected_key in CRITICAL_PRIMARY_KEYS:
        if table_names is not None and expected_key.table_name not in table_names:
            continue
        constraint = inspector.get_pk_constraint(expected_key.table_name)
        observed_columns = tuple(
            str(column_name)
            for column_name in _object_sequence(constraint.get("constrained_columns"))
            if str(column_name)
        )
        if observed_columns != expected_key.column_names:
            observed_summary = ", ".join(observed_columns) or "<none>"
            expected_summary = ", ".join(expected_key.column_names)
            errors.append(
                f"{expected_key.table_name} has primary key ({observed_summary}); "
                f"expected ({expected_summary})"
            )
    return errors


def critical_index_errors(
    *,
    inspector: SchemaInspectorProtocol,
    table_names: set[str],
    expected_indexes: tuple[CriticalIndex, ...] = CRITICAL_SCHEMA_INDEXES,
    index_definitions: Mapping[tuple[str, str], str] | None = None,
) -> list[str]:
    errors: list[str] = []
    resolved_index_definitions = index_definitions or {}
    for expected_index in expected_indexes:
        if expected_index.table_name not in table_names:
            continue
        indexes_by_name = {
            str(index.get("name", "")): index
            for index in inspector.get_indexes(expected_index.table_name)
        }
        observed_index = indexes_by_name.get(expected_index.index_name)
        if observed_index is None:
            errors.append(
                f"{expected_index.table_name} missing required index {expected_index.index_name}"
            )
            continue
        observed_unique = bool(observed_index.get("unique", False))
        if observed_unique != expected_index.unique:
            expected_unique = "unique" if expected_index.unique else "non-unique"
            observed_unique_label = "unique" if observed_unique else "non-unique"
            errors.append(
                f"{expected_index.index_name} is {observed_unique_label}; "
                f"expected {expected_unique}"
            )
        observed_columns = _observed_index_columns(observed_index)
        if observed_columns != expected_index.column_names:
            errors.append(
                f"{expected_index.index_name} covers {', '.join(observed_columns) or '<none>'}; "
                f"expected {', '.join(expected_index.column_names)}"
            )
        if expected_index.predicate_tokens:
            predicate_text = _normalized_predicate_text(
                observed_index,
                resolved_index_definitions.get(
                    (expected_index.table_name, expected_index.index_name), ""
                ),
            )
            missing_tokens = [
                token for token in expected_index.predicate_tokens if token not in predicate_text
            ]
            if missing_tokens:
                errors.append(
                    f"{expected_index.index_name} has predicate "
                    f"{predicate_text or '<none>'}; expected tokens "
                    f"{', '.join(expected_index.predicate_tokens)}"
                )
    return errors


def _verify_alembic_head(engine: Engine) -> None:
    try:
        with engine.connect() as connection:
            version_rows = connection.execute(
                text("select version_num from alembic_version")
            ).fetchall()
    except SQLAlchemyError as error:
        raise RuntimeError(
            "Launchplane shared storage schema is missing Alembic version metadata. "
            "Run Alembic migrations before starting the hosted service."
        ) from error
    version_numbers = tuple(str(row[0]).strip() for row in version_rows if str(row[0]).strip())
    if version_numbers != (EXPECTED_ALEMBIC_HEAD_REVISION,):
        observed = ", ".join(version_numbers) if version_numbers else "<none>"
        raise RuntimeError(
            "Launchplane shared storage schema is not at the checked-in Alembic head: "
            f"observed {observed}; expected {EXPECTED_ALEMBIC_HEAD_REVISION}. Run "
            "Alembic migrations before starting the hosted service."
        )


def postgres_index_definitions(engine: Engine) -> dict[tuple[str, str], str]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "select tablename, indexname, indexdef "
                "from pg_indexes where schemaname = current_schema()"
            )
        ).mappings()
        return {
            (str(row["tablename"]), str(row["indexname"])): str(row["indexdef"]) for row in rows
        }


def _normalized_type_name(type_value: object) -> str:
    tokens = {
        type(type_value).__name__.lower(),
        str(type_value).lower(),
    }
    return " ".join(sorted(tokens))


def _observed_index_columns(index: Mapping[str, object]) -> tuple[str, ...]:
    column_names = _object_sequence(index.get("column_names"))
    expressions = _object_sequence(index.get("expressions"))
    observed_columns: list[str] = []
    for position, column_name in enumerate(column_names):
        normalized_column = _normalize_index_column(column_name)
        if not normalized_column and position < len(expressions):
            normalized_column = _normalize_index_column(expressions[position])
        if normalized_column:
            observed_columns.append(normalized_column)
    if not observed_columns:
        for expression in expressions:
            normalized_expression = _normalize_index_column(expression)
            if normalized_expression:
                observed_columns.append(normalized_expression)
    return tuple(observed_columns)


def _object_sequence(value: object) -> tuple[object, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return ()


def _normalize_index_column(value: object) -> str:
    if value is None:
        return ""
    normalized = str(value).strip().strip('"').lower()
    if not normalized:
        return ""
    normalized = normalized.split()[0].strip('"')
    return normalized.split("::", maxsplit=1)[0].strip('"')


def _normalized_predicate_text(index: Mapping[str, object], index_definition: str) -> str:
    predicate_parts = [index_definition]
    dialect_options = index.get("dialect_options")
    if isinstance(dialect_options, Mapping):
        for key, value in dialect_options.items():
            if str(key).endswith("_where") and value is not None:
                predicate_parts.append(str(value))
    return " ".join(predicate_parts).lower().replace('"', "")
