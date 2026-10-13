"""Scrub stored payloads without serializing away their historical shape."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
import secrets
from time import monotonic
from typing import Any, Literal, overload

from pydantic import BaseModel, ValidationError
from sqlalchemy import LargeBinary, Text, case, cast, func, select, text
from sqlalchemy.engine import Connection, Engine, MappingResult
from sqlalchemy.exc import DBAPIError
from sqlalchemy.sql import ColumnElement

from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
)
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.merge_train_policy import MergeTrainPolicyRecord
from control_plane.contracts.merge_train_run_record import MergeTrainRunRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.stored_record_snapshot import (
    SNAPSHOT_MAX_BYTES,
    SNAPSHOT_MAX_ROWS,
    CompressedStoredRecordSnapshot,
    StoredRecordSnapshot,
)
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.stored_record_snapshot_codec import SnapshotRowEncoder
from control_plane.merge_train import MergeTrainDryRunResult, MergeTrainDryRunSnapshot
from control_plane.workflows.merge_train_worker import MergeTrainWorkerStepResult
from control_plane.storage.postgres import (
    LaunchplaneAuthzPolicyRow,
    LaunchplaneInventoryRow,
    LaunchplaneMergeTrainPolicyRow,
    LaunchplaneMergeTrainRunRow,
    LaunchplaneProductProfileRow,
    PostgresRecordStore,
)

# This is a record-format allowlist, never a list of runtime identities. Secret,
# session, provider, environment-value and worker tables cannot enter the export.
SNAPSHOT_RECORD_TYPES: tuple[tuple[Any, type[BaseModel]], ...] = (
    (LaunchplaneAuthzPolicyRow, LaunchplaneAuthzPolicyRecord),
    (LaunchplaneProductProfileRow, LaunchplaneProductProfileRecord),
    (LaunchplaneInventoryRow, EnvironmentInventory),
    (LaunchplaneMergeTrainPolicyRow, MergeTrainPolicyRecord),
    (LaunchplaneMergeTrainRunRow, MergeTrainRunRecord),
)


SnapshotAuthorization = Callable[
    [tuple[LaunchplaneProductProfileRecord, ...], tuple[EnvironmentInventory, ...]], bool
]


class SnapshotAuthorizationDenied(PermissionError):
    pass


class SnapshotUnavailable(ValueError):
    def __init__(self, reason: str, **budgets: int | str) -> None:
        super().__init__(reason)
        self.budgets = budgets


def snapshot_failure_diagnostics(error: Exception) -> dict[str, Any]:
    """Emit code-owned field paths and classifications without record inputs."""
    result: dict[str, Any] = {"error_type": type(error).__name__}
    if isinstance(error, DBAPIError) and getattr(error.orig, "sqlstate", None) == "57014":
        result["reason"] = "snapshot_statement_canceled"
    if isinstance(error, SnapshotUnavailable):
        result["reason"] = str(error)
        for key in ("rows_exported", "source_row_bytes", "scrubbed_bytes"):
            value = error.budgets.get(key)
            if isinstance(value, int) and value >= 0:
                result[key] = value
    family = (
        error.budgets.get("record_family", "")
        if isinstance(error, SnapshotUnavailable)
        else getattr(error, "snapshot_record_family", "")
    )
    if family in {row_type.__tablename__ for row_type, _ in SNAPSHOT_RECORD_TYPES}:
        result["record_family"] = family
    if isinstance(error, ValidationError):
        field_names: set[str] = set()
        for _, model in SNAPSHOT_RECORD_TYPES:
            schema = model.model_json_schema()
            for definition in (schema, *schema.get("$defs", {}).values()):
                field_names.update(definition.get("properties", {}))
        result["validation"] = [
            {
                "type": item["type"],
                "loc": [
                    part if isinstance(part, int) or part in field_names else "dynamic_field"
                    for part in item["loc"]
                ],
            }
            for item in error.errors(include_input=False, include_context=False, include_url=False)[
                :10
            ]
        ]
    return result


class _Scrubber:
    def __init__(self) -> None:
        self._salt = secrets.token_bytes(32)
        self._numbers: dict[str, int] = {}
        self._used_numbers: set[int] = set()

    def _digest(self, value: str) -> str:
        return hmac.new(self._salt, value.encode(), hashlib.sha256).hexdigest()

    def number(self, value: str) -> int:
        # History may contain large decimal audit strings. Retain fixed-size
        # salted keys, not the source text, across streamed rows.
        digest = self._digest(value)
        if digest not in self._numbers:
            # Indexed PR numbers use PostgreSQL Integer. Keep numeric identities
            # consistent across strings/payloads/projections and avoid collisions.
            maximum = (1 << 31) - 1
            candidate = int(digest[:12], 16) % maximum + 1
            while candidate in self._used_numbers or str(candidate) == value:
                candidate = candidate % maximum + 1
            self._numbers[digest] = candidate
            self._used_numbers.add(candidate)
        return self._numbers[digest]

    def string(self, value: str, name: str, *, protocol: bool = False) -> str:
        if not value or value == "*":
            return value
        # Action names and driver identifiers are code protocol, not identities.
        if (
            protocol
            and name in {"actions", "driver_id"}
            and re.fullmatch(r"[a-z_][a-z_.-]*", value)
        ):
            return value
        if protocol and (name.endswith("_at") or name in {"started_at", "finished_at"}):
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
                return value
            except ValueError:
                pass
        if value.isdecimal():
            return str(self.number(value))
        digest = self._digest(value)
        if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
            return digest[: len(value)]
        if name == "slug_template":
            return f"snapshot-{digest[:12]}" + ("-{number}" if "{number}" in value else "")
        if name.endswith("url") or name == "urls":
            return f"https://snapshot-{digest[:12]}.invalid"
        if name in {"repository", "repositories"}:
            return f"snapshot/{digest[:12]}"
        if name in {"workflow_ref", "workflow_refs", "job_workflow_refs"}:
            return f"snapshot/repo/.github/workflows/{digest[:12]}.yml@refs/heads/main"
        if value.startswith("sha256:") and re.fullmatch(r"sha256:[0-9a-f]{64}", value):
            return "sha256:" + digest
        if name.endswith("sha256") or name.endswith("sha"):
            return digest
        if value.startswith("/"):
            return f"/snapshot-{digest[:12]}"
        return f"snapshot-{digest[:12]}"

    def scrub(
        self,
        value: Any,
        schema: Mapping[str, Any],
        root: Mapping[str, Any],
        name: str = "",
        *,
        declared: bool = False,
    ) -> Any:
        if "$ref" in schema:
            schema = root["$defs"][schema["$ref"].rsplit("/", 1)[-1]]
        if "anyOf" in schema:
            candidates = schema["anyOf"]
            kind = (
                "object"
                if isinstance(value, dict)
                else "array"
                if isinstance(value, list)
                else "boolean"
                if isinstance(value, bool)
                else "integer"
                if isinstance(value, int)
                else "number"
                if isinstance(value, float)
                else "null"
                if value is None
                else "string"
            )
            schema = next(
                (item for item in candidates if item.get("type") == kind or "$ref" in item), {}
            )
            return self.scrub(value, schema, root, name, declared=declared)
        if isinstance(value, str) and value.strip() in schema.get(
            "x-launchplane-snapshot-protocol", []
        ):
            return value
        if isinstance(value, dict):
            properties = schema.get("properties", {})
            additional = schema.get("additionalProperties", {})
            scrubbed = {
                key: self.scrub(
                    item,
                    properties.get(key, additional if isinstance(additional, dict) else {}),
                    root,
                    key,
                    declared=key in properties,
                )
                for key, item in value.items()
            }
            if value.get("policy_key") == f"{value.get('repository')}:{value.get('base_branch')}":
                scrubbed["policy_key"] = f"{scrubbed['repository']}:{scrubbed['base_branch']}"
            return scrubbed
        if isinstance(value, list):
            return [
                self.scrub(item, schema.get("items", {}), root, name, declared=declared)
                for item in value
            ]
        if isinstance(value, str):
            if value in schema.get("enum", []) or ("const" in schema and value == schema["const"]):
                return value
            return self.string(value, name, protocol=declared)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if (
                not declared
                or name.endswith("_id")
                or name.endswith("_ids")
                or name
                in {
                    "number",
                    "selected_pr_number",
                    "pull_request_number",
                    "root_pull_request_number",
                    "queue_order",
                    "stack_order",
                }
            ):
                pseudonym = self.number(str(value))
                return float(pseudonym) if isinstance(value, float) else pseudonym
        return value


def _statement_timeout_ms(remaining: int) -> int:
    return min(remaining, 15000) // 1000 * 1000 or remaining


@contextmanager
def _snapshot_cursor(
    connection: Connection,
    statement: Any,
    refuse: Callable[[str], SnapshotUnavailable],
) -> Iterator[MappingResult]:
    try:
        with connection.execute(statement.execution_options(yield_per=1)) as result:
            yield result.mappings()
    except DBAPIError as error:
        if getattr(error.orig, "sqlstate", None) == "57014":
            raise refuse("snapshot_statement_canceled") from error
        raise


@overload
def export_stored_record_snapshot(
    engine: Engine,
    *,
    authorize_records: SnapshotAuthorization | None = None,
    format_version: Literal[1] = 1,
) -> StoredRecordSnapshot: ...


@overload
def export_stored_record_snapshot(
    engine: Engine,
    *,
    authorize_records: SnapshotAuthorization | None = None,
    format_version: Literal[2],
) -> CompressedStoredRecordSnapshot: ...


def export_stored_record_snapshot(
    engine: Engine,
    *,
    authorize_records: SnapshotAuthorization | None = None,
    format_version: Literal[1, 2] = 1,
) -> StoredRecordSnapshot | CompressedStoredRecordSnapshot:
    scrubber = _Scrubber()
    tables: dict[str, list[dict[str, Any]]] = {}
    profiles: list[LaunchplaneProductProfileRecord] = []
    inventory: list[EnvironmentInventory] = []
    row_count = 0
    scrubbed_bytes = 0
    source_row_bytes = 0
    family = ""
    deadline = monotonic() + 20
    encoder = SnapshotRowEncoder() if format_version == 2 else None
    active_policy_count = 0

    def refuse(reason: str) -> SnapshotUnavailable:
        return SnapshotUnavailable(
            reason,
            record_family=family,
            rows_exported=row_count,
            source_row_bytes=source_row_bytes,
            scrubbed_bytes=scrubbed_bytes,
        )

    # One consistent, explicitly read-only PostgreSQL transaction. CI never
    # receives this engine, its URL, keys or SQL; it receives only the scrubbed JSON.
    with engine.connect() as connection:
        if engine.dialect.name == "postgresql":
            connection = connection.execution_options(isolation_level="REPEATABLE READ")
            connection.execute(text("SET TRANSACTION READ ONLY"))
            remaining = int((deadline - monotonic()) * 1000)
            if remaining <= 0:
                raise refuse("snapshot_time_limit")
            connection.execute(
                text(f"SET LOCAL statement_timeout = '{_statement_timeout_ms(remaining)}ms'")
            )
        revision = str(
            connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        )
        for row_type, model_type in SNAPSHOT_RECORD_TYPES:
            table = row_type.__table__
            family = table.name
            schema = model_type.model_json_schema()
            rows: list[dict[str, Any]] = []
            if encoder is not None:
                encoder.table_counts[table.name] = 0
            remaining = int((deadline - monotonic()) * 1000)
            if remaining <= 0:
                raise refuse("snapshot_time_limit")
            if engine.dialect.name == "postgresql":
                statement_timeout = _statement_timeout_ms(remaining)
                connection.execute(text(f"SET LOCAL statement_timeout = '{statement_timeout}ms'"))
            # Bound one raw row before the driver decodes its JSON. Cumulative
            # raw history is not the response size: free text shrinks on scrubbing.
            sizes = []
            for column in table.c:
                value = cast(column, Text)
                size = (
                    func.octet_length(value)
                    if engine.dialect.name == "postgresql"
                    else func.length(cast(value, LargeBinary))
                )
                sizes.append(func.coalesce(size, 0))
            row_bytes: ColumnElement[Any] = sizes[0]
            for size in sizes[1:]:
                row_bytes += size
            statement = select(table, row_bytes.label("snapshot_source_row_bytes"))
            if row_type is LaunchplaneMergeTrainRunRow:
                # Run history grows forever. Rehearse current per-lane state;
                # never delete or retain production history as a deploy operation.
                ranked = select(
                    table.c.run_id,
                    func.row_number()
                    .over(
                        partition_by=(table.c.repository, table.c.base_branch),
                        order_by=(table.c.recorded_at.desc(), table.c.run_id.desc()),
                    )
                    .label("snapshot_rank"),
                ).subquery()
                statement = statement.join(ranked, table.c.run_id == ranked.c.run_id).where(
                    ranked.c.snapshot_rank == 1
                )
            # OFFSET 0 keeps PostgreSQL from flattening the size calculation into
            # every CASE expression. Measure JSON text once per row on the server.
            sized = statement.limit(SNAPSHOT_MAX_ROWS + 1).offset(0).subquery()
            bounded = select(
                *(
                    case(
                        (
                            sized.c.snapshot_source_row_bytes <= SNAPSHOT_MAX_BYTES,
                            sized.c[column.name],
                        ),
                    ).label(column.name)
                    for column in table.c
                ),
                sized.c.snapshot_source_row_bytes,
            )
            # A server cursor fetches one bounded row at a time instead of
            # buffering every raw revision. Closing it also covers refusal paths.
            with _snapshot_cursor(connection, bounded, refuse) as result:
                for raw in result:
                    source_row_bytes = raw["snapshot_source_row_bytes"]
                    if row_count >= SNAPSHOT_MAX_ROWS:
                        raise refuse("snapshot_row_limit")
                    if monotonic() > deadline:
                        raise refuse("snapshot_time_limit")
                    if source_row_bytes > SNAPSHOT_MAX_BYTES:
                        raise refuse("snapshot_source_row_byte_limit")
                    if authorize_records is not None:
                        if model_type is LaunchplaneProductProfileRecord:
                            profiles.append(
                                PostgresRecordStore._read_product_profile_payload(raw["payload"])
                            )
                        elif model_type is EnvironmentInventory:
                            inventory.append(EnvironmentInventory.model_validate(raw["payload"]))
                    payload = scrubber.scrub(raw["payload"], schema, schema)
                    if model_type is MergeTrainRunRecord:
                        for key, nested_type in (
                            ("dry_run_result", MergeTrainDryRunResult),
                            ("snapshot", MergeTrainDryRunSnapshot),
                            ("worker_step_result", MergeTrainWorkerStepResult),
                        ):
                            nested_schema = nested_type.model_json_schema()
                            if key in raw["payload"]:
                                payload[key] = scrubber.scrub(
                                    raw["payload"][key], nested_schema, nested_schema
                                )
                    # Check the source digest first. A scrubbed digest is evidence
                    # about the scrubbed policy; it cannot attest production authority.
                    if model_type is LaunchplaneAuthzPolicyRecord:
                        model_type.model_validate(raw["payload"])
                        payload["policy_sha256"] = authz_policy_sha256(
                            LaunchplaneAuthzPolicy.model_validate(payload["policy"])
                        )
                    elif model_type is MergeTrainPolicyRecord:
                        original = MergeTrainPolicyRecord.model_validate(raw["payload"])
                        policy_type = type(original.policy)
                        payload["policy_sha256"] = policy_type.model_validate(
                            payload["policy"], context={"historical_policy_record": True}
                        ).policy_sha256
                    row = {
                        key: scrubber.string(value, key) if isinstance(value, str) else value
                        for key, value in raw.items()
                        if key not in {"payload", "snapshot_source_row_bytes"}
                    }
                    # Indexed projections share the payload's scrubbed values.
                    for key in row:
                        if key in payload:
                            if raw[key] != raw["payload"].get(key):
                                raise refuse("snapshot_projection_conflict")
                            row[key] = payload[key]
                    row["payload"] = payload
                    if encoder is None:
                        scrubbed_bytes += sum(
                            len(chunk.encode())
                            for chunk in json.JSONEncoder(
                                separators=(",", ":"), ensure_ascii=False
                            ).iterencode(row)
                        )
                        if scrubbed_bytes > SNAPSHOT_MAX_BYTES:
                            raise refuse("snapshot_byte_limit")
                        rows.append(row)
                    else:
                        try:
                            encoder.add(table.name, row)
                        except ValueError as error:
                            scrubbed_bytes = encoder.expanded_bytes
                            raise refuse(str(error)) from error
                        scrubbed_bytes = encoder.expanded_bytes
                    if row_type is LaunchplaneAuthzPolicyRow and row["status"] == "active":
                        active_policy_count += 1
                    if monotonic() > deadline:
                        raise refuse("snapshot_time_limit")
                    row_count += 1
                    # FETCH is a new statement on the server cursor. Clip its
                    # timeout to the budget left after processing this row.
                    remaining = int((deadline - monotonic()) * 1000)
                    if remaining <= 0:
                        raise refuse("snapshot_time_limit")
                    if engine.dialect.name == "postgresql":
                        next_timeout = _statement_timeout_ms(remaining)
                        if next_timeout < statement_timeout:
                            connection.execute(
                                text(f"SET LOCAL statement_timeout = '{next_timeout}ms'")
                            )
                            statement_timeout = next_timeout
            tables[table.name] = rows
        if authorize_records is not None and not authorize_records(
            tuple(profiles), tuple(inventory)
        ):
            raise SnapshotAuthorizationDenied("Snapshot records exceed the reader scope.")
        if active_policy_count != 1:
            raise SnapshotUnavailable("snapshot_requires_one_active_policy")
        captured_at = datetime.now(timezone.utc).isoformat()
        snapshot: StoredRecordSnapshot | CompressedStoredRecordSnapshot
        if encoder is None:
            snapshot = StoredRecordSnapshot(
                captured_at=captured_at,
                database_schema_revision=revision,
                tables=tables,
            )
        else:
            try:
                snapshot = encoder.finish(captured_at=captured_at, revision=revision)
            except ValueError as error:
                raise refuse(str(error)) from error
    # Envelope overhead and final encoding apply to the whole export, not the
    # last record family visited. rows_exported always counts all accepted rows.
    family = ""
    if len(snapshot.model_dump_json().encode()) > SNAPSHOT_MAX_BYTES:
        raise refuse("snapshot_byte_limit")
    if monotonic() > deadline:
        raise refuse("snapshot_time_limit")
    return snapshot
