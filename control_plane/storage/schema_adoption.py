from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from typing import Protocol, cast

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from control_plane.storage.postgres import Base, _build_engine
from control_plane.storage.schema_invariants import (
    critical_column_type_errors,
    critical_index_errors,
    postgres_index_definitions,
)

LEGACY_BASELINE_REVISION = "fe94a0486977"
LEGACY_CURRENT_SCHEMA_REVISION = "b1c3d5e7f9a1"
LEGACY_CURRENT_SCHEMA_MARKER_TABLE = "launchplane_preview_enablement"

LEGACY_BASELINE_SCHEMA: dict[str, frozenset[str]] = {
    "launchplane_artifact_manifests": frozenset(
        {"artifact_id", "source_commit", "image_repository", "image_digest", "payload"}
    ),
    "launchplane_backup_gates": frozenset(
        {"record_id", "context", "instance", "created_at", "status", "payload"}
    ),
    "launchplane_deployments": frozenset(
        {
            "record_id",
            "context",
            "instance",
            "artifact_id",
            "source_git_ref",
            "deploy_started_at",
            "deploy_finished_at",
            "payload",
        }
    ),
    "launchplane_inventory": frozenset(
        {
            "context",
            "instance",
            "artifact_id",
            "source_git_ref",
            "updated_at",
            "deployment_record_id",
            "promotion_record_id",
            "promoted_from_instance",
            "payload",
        }
    ),
    "launchplane_merge_train_batches": frozenset(
        {"batch_id", "policy_id", "base_branch", "state", "updated_at", "payload"}
    ),
    "launchplane_merge_train_policies": frozenset(
        {"policy_id", "base_branch", "is_active", "updated_at", "payload"}
    ),
    "launchplane_merge_train_runs": frozenset(
        {"run_id", "batch_id", "state", "updated_at", "payload"}
    ),
    "launchplane_odoo_instance_overrides": frozenset(
        {"record_id", "product", "context", "instance", "updated_at", "payload"}
    ),
    "launchplane_preview_desired_states": frozenset(
        {"preview_id", "context", "desired_state", "updated_at", "payload"}
    ),
    "launchplane_preview_generations": frozenset(
        {
            "generation_id",
            "preview_id",
            "sequence",
            "state",
            "requested_at",
            "finished_at",
            "artifact_id",
            "payload",
        }
    ),
    "launchplane_preview_records": frozenset(
        {
            "preview_id",
            "context",
            "anchor_repo",
            "anchor_pr_number",
            "state",
            "updated_at",
            "payload",
        }
    ),
    "launchplane_promotions": frozenset(
        {
            "record_id",
            "context",
            "from_instance",
            "to_instance",
            "artifact_id",
            "deploy_started_at",
            "deploy_finished_at",
            "payload",
        }
    ),
    "launchplane_release_tuples": frozenset(
        {"tuple_id", "product", "context", "lane", "updated_at", "payload"}
    ),
    "launchplane_runtime_environment_deletes": frozenset(
        {"event_id", "product", "environment", "status", "requested_at", "payload"}
    ),
    "launchplane_runtime_environments": frozenset(
        {"environment_id", "product", "environment", "updated_at", "payload"}
    ),
    "launchplane_secrets": frozenset({"secret_id", "scope", "updated_at", "payload"}),
    "launchplane_workflow_authz_grants": frozenset(
        {"grant_id", "subject", "action", "scope", "updated_at", "payload"}
    ),
}

LEGACY_CURRENT_SCHEMA: dict[str, frozenset[str]] = {
    **LEGACY_BASELINE_SCHEMA,
    "launchplane_preview_enablement": frozenset(
        {
            "record_id",
            "context",
            "anchor_repo",
            "anchor_pr_number",
            "pr_state",
            "updated_at",
            "payload",
        }
    ),
}

LEGACY_KNOWN_LATER_TABLES = frozenset(Base.metadata.tables) - frozenset(LEGACY_CURRENT_SCHEMA)


class SchemaAdoptionError(RuntimeError):
    pass


class SchemaInspectorProtocol(Protocol):
    def get_table_names(self) -> Sequence[str]:
        raise NotImplementedError

    def get_columns(self, table_name: str) -> Sequence[Mapping[str, object]]:
        raise NotImplementedError

    def get_indexes(self, table_name: str) -> Sequence[Mapping[str, object]]:
        raise NotImplementedError


def verify_existing_schema_for_stamp(
    *,
    inspector: SchemaInspectorProtocol,
    expected_schema: Mapping[str, frozenset[str]],
    index_definitions: Mapping[tuple[str, str], str] | None = None,
    verify_column_types: bool = False,
) -> None:
    existing_tables = set(inspector.get_table_names())
    expected_tables = set(expected_schema)
    errors: list[str] = []

    missing_tables = sorted(expected_tables - existing_tables)
    if missing_tables:
        errors.append(f"missing tables: {', '.join(missing_tables)}")

    unexpected_managed_tables = sorted((existing_tables - expected_tables) & LEGACY_KNOWN_LATER_TABLES)
    if unexpected_managed_tables:
        errors.append(
            "has Launchplane tables beyond the adoption revision: "
            f"{', '.join(unexpected_managed_tables)}"
        )

    for table_name in sorted(existing_tables & expected_tables):
        expected_columns = set(expected_schema[table_name])
        observed_columns = {
            str(column.get("name", "")).strip()
            for column in inspector.get_columns(table_name)
            if str(column.get("name", "")).strip()
        }
        missing_columns = sorted(expected_columns - observed_columns)
        unexpected_columns = sorted(observed_columns - expected_columns)
        if missing_columns:
            errors.append(f"{table_name} missing columns: {', '.join(missing_columns)}")
        if unexpected_columns:
            errors.append(
                f"{table_name} has unexpected columns: {', '.join(unexpected_columns)}"
            )

    errors.extend(
        critical_index_errors(
            inspector=inspector,
            table_names=existing_tables,
            index_definitions=index_definitions,
        )
    )
    if verify_column_types:
        errors.extend(critical_column_type_errors(inspector, table_names=existing_tables))

    if errors:
        joined_errors = "; ".join(errors)
        raise SchemaAdoptionError(
            "Existing Launchplane database schema does not match the revision-managed "
            f"table shape required before Alembic stamp adoption: {joined_errors}."
        )


def expected_schema_for_existing_tables(existing_tables: set[str]) -> Mapping[str, frozenset[str]]:
    if LEGACY_CURRENT_SCHEMA_MARKER_TABLE in existing_tables:
        return LEGACY_CURRENT_SCHEMA
    return LEGACY_BASELINE_SCHEMA


def schema_stamp_revision_for_engine(engine: Engine) -> str:
    inspector = cast(SchemaInspectorProtocol, inspect(engine))
    existing_tables = set(inspector.get_table_names())
    expected_schema = expected_schema_for_existing_tables(existing_tables)
    has_current_marker_table = LEGACY_CURRENT_SCHEMA_MARKER_TABLE in existing_tables

    if "alembic_version" in existing_tables:
        with engine.connect() as connection:
            version_rows = connection.execute(text("select version_num from alembic_version")).fetchall()
        version_numbers = {str(row[0]).strip() for row in version_rows if str(row[0]).strip()}
        if version_numbers == {LEGACY_BASELINE_REVISION} and has_current_marker_table:
            verify_existing_schema_for_stamp(
                inspector=inspector,
                expected_schema=expected_schema,
                index_definitions=_index_definitions_for_engine(engine),
                verify_column_types=_uses_postgresql(engine),
            )
            return LEGACY_CURRENT_SCHEMA_REVISION
        return ""

    if not existing_tables.intersection(expected_schema):
        return ""

    verify_existing_schema_for_stamp(
        inspector=inspector,
        expected_schema=expected_schema,
        index_definitions=_index_definitions_for_engine(engine),
        verify_column_types=_uses_postgresql(engine),
    )
    if has_current_marker_table:
        return LEGACY_CURRENT_SCHEMA_REVISION
    return LEGACY_BASELINE_REVISION


def schema_stamp_revision(database_url: str) -> str:
    engine = _build_engine(database_url)
    try:
        return schema_stamp_revision_for_engine(engine)
    finally:
        engine.dispose()


def _index_definitions_for_engine(engine: Engine) -> Mapping[tuple[str, str], str] | None:
    if _uses_postgresql(engine):
        return postgres_index_definitions(engine)
    return None


def _uses_postgresql(engine: Engine) -> bool:
    return engine.url.get_backend_name() == "postgresql"


def main() -> int:
    database_url = os.environ.get("LAUNCHPLANE_DATABASE_URL", "").strip()
    if not database_url:
        print("LAUNCHPLANE_DATABASE_URL is required for schema adoption.", file=sys.stderr)
        return 1
    try:
        revision = schema_stamp_revision(database_url)
    except Exception as error:
        print(f"Could not verify legacy Launchplane database schema: {error}", file=sys.stderr)
        return 1
    if revision:
        print(revision)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
