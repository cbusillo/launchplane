"""Candidate-side rehearsal. The only input is a scrubbed service export."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

from alembic import command
from sqlalchemy import MetaData, Table, insert, inspect
from sqlalchemy.engine import Engine, make_url

from control_plane.contracts.stored_record_snapshot import (
    SNAPSHOT_MAX_AGE_SECONDS,
    SNAPSHOT_MAX_BYTES,
    SNAPSHOT_MAX_ROWS,
    StoredRecordPreflightResult,
    StoredRecordSnapshot,
    CompressedStoredRecordSnapshot,
    StoredRecordSnapshotResponse,
)
from control_plane.service_auth import GitHubActionsIdentity, LaunchplaneAuthzPolicy
from control_plane.service_bootstrap import create_launchplane_service_application
from control_plane.drivers import native_routes
from control_plane.storage.postgres import PostgresRecordStore, _build_engine
from control_plane.storage.schema_migration import alembic_config, migrate_schema
from control_plane.stored_record_snapshot import SNAPSHOT_RECORD_TYPES, snapshot_failure_diagnostics
from control_plane.stored_record_snapshot_codec import iter_snapshot_rows


class _ClosedVerifier:
    def verify(self, token: str) -> GitHubActionsIdentity:
        raise ValueError("Rehearsal accepts no caller credentials.")


def validate_snapshot(
    snapshot: StoredRecordSnapshot | CompressedStoredRecordSnapshot, *, now: datetime
) -> None:
    captured = datetime.fromisoformat(snapshot.captured_at.replace("Z", "+00:00"))
    if (
        captured.tzinfo is None
        or not 0 <= (now - captured).total_seconds() <= SNAPSHOT_MAX_AGE_SECONDS
    ):
        raise ValueError("Snapshot is stale or has an invalid capture time.")
    expected_tables = {row_type.__tablename__ for row_type, _ in SNAPSHOT_RECORD_TYPES}
    tables = (
        snapshot.tables if isinstance(snapshot, StoredRecordSnapshot) else snapshot.table_counts
    )
    if set(tables) != expected_tables:
        raise ValueError("Snapshot table coverage is incomplete or unsupported.")
    row_count = 0
    active_policy_count = 0
    for table, row in iter_snapshot_rows(snapshot):
        row_count += 1
        if row_count > SNAPSHOT_MAX_ROWS:
            raise ValueError("Snapshot exceeds its row bound.")
        if table == "launchplane_authz_policies" and row.get("status") == "active":
            active_policy_count += 1
    if active_policy_count != 1:
        raise ValueError("Snapshot requires exactly one active authorization policy.")


def check_restored_records(store: PostgresRecordStore, *, state_dir: Path) -> int:
    # Real storage readers apply their supported historical transformations.
    # Do not validate with only new defaults, or allow db-invalid startup fallback.
    try:
        policies = store.list_authz_policy_records()
    except Exception as error:
        setattr(error, "snapshot_record_family", "launchplane_authz_policies")
        raise
    count = len(policies)
    for family, reader in (
        ("launchplane_product_profiles", store.list_product_profile_records),
        ("launchplane_inventory", store.list_environment_inventory),
        ("launchplane_merge_train_policies", store.list_merge_train_policy_records),
        ("launchplane_merge_train_runs", store.list_merge_train_run_records),
    ):
        try:
            count += len(reader())
        except Exception as error:
            setattr(error, "snapshot_record_family", family)
            raise
    if sum(record.status == "active" for record in policies) != 1:
        raise ValueError("Restored active policy is missing or ambiguous.")
    native_routes._validate_native_descriptor_driver_routes()
    application = create_launchplane_service_application(
        state_dir=state_dir,
        bootstrap_authz_policy=LaunchplaneAuthzPolicy(),
        verifier=_ClosedVerifier(),
        service_record_store=store,
    )
    if application is None:
        raise ValueError("Candidate application construction failed.")
    native_routes._validate_native_fastapi_driver_routes(application)
    runtime = application.state.authz_policy_runtime
    active = next(record for record in policies if record.status == "active")
    if runtime.source != "db" or runtime.policy_sha256 != active.policy_sha256:
        raise ValueError(
            "Candidate startup authorization did not resolve the restored active policy."
        )
    return count


def restore_snapshot(
    engine: Engine, snapshot: StoredRecordSnapshot | CompressedStoredRecordSnapshot
) -> str:
    """Restore into an empty rehearsal database, then run the hosted migrations."""
    database_url = engine.url.render_as_string(hide_password=False)
    if inspect(engine).get_table_names():
        raise ValueError("Preflight requires an empty isolated database.")
    validate_snapshot(snapshot, now=datetime.now(timezone.utc))
    command.upgrade(alembic_config(database_url), snapshot.database_schema_revision)
    with engine.begin() as connection:
        tables = {
            row_type.__tablename__: Table(
                row_type.__tablename__, MetaData(), autoload_with=connection
            )
            for row_type, _ in SNAPSHOT_RECORD_TYPES
        }
        for name, row in iter_snapshot_rows(snapshot):
            connection.execute(insert(tables[name]), row)
    return migrate_schema(database_url=database_url)


def preflight_stored_records(
    snapshot: StoredRecordSnapshot | CompressedStoredRecordSnapshot, *, database_url: str
) -> StoredRecordPreflightResult:
    validate_snapshot(snapshot, now=datetime.now(timezone.utc))
    url = make_url(database_url)
    # The runner owns this one private network/database. Arbitrary/live DB URLs
    # and local checkout live-target commands are never accepted here.
    if (
        url.get_backend_name() != "postgresql"
        or url.host != "snapshot-db"
        or url.database != "snapshot"
    ):
        raise ValueError("Preflight requires its isolated snapshot-db/snapshot database.")
    engine = _build_engine(database_url)
    try:
        revision = restore_snapshot(engine, snapshot)
    finally:
        engine.dispose()
    store = PostgresRecordStore(database_url=database_url)
    try:
        store.verify_schema()
        with TemporaryDirectory() as directory:
            count = check_restored_records(store, state_dir=Path(directory))
        return StoredRecordPreflightResult(records_checked=count, database_schema_revision=revision)
    finally:
        store.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.snapshot.stat().st_size > SNAPSHOT_MAX_BYTES + 4096:
            raise ValueError("Snapshot exceeds its byte bound.")
        response = StoredRecordSnapshotResponse.model_validate_json(args.snapshot.read_bytes())
        result = preflight_stored_records(
            response.snapshot, database_url=os.environ["LAUNCHPLANE_PREFLIGHT_DATABASE_URL"]
        )
        print(result.model_dump_json())
        return 0
    except Exception as error:
        # Pydantic, SQLAlchemy and migration messages can contain record values.
        print(
            json.dumps({"status": "failed", **snapshot_failure_diagnostics(error)}), file=sys.stderr
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
