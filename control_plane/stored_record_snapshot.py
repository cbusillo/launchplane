"""Scrub stored payloads without serializing away their historical shape."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
import secrets
import time
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.engine import Engine

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
    StoredRecordSnapshot,
)
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.postgres import (
    LaunchplaneAuthzPolicyRow,
    LaunchplaneInventoryRow,
    LaunchplaneMergeTrainPolicyRow,
    LaunchplaneMergeTrainRunRow,
    LaunchplaneProductProfileRow,
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


class SnapshotUnavailable(ValueError):
    pass


class _Scrubber:
    def __init__(self) -> None:
        self._salt = secrets.token_bytes(32)

    def _digest(self, value: str) -> str:
        return hmac.new(self._salt, value.encode(), hashlib.sha256).hexdigest()

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
        if protocol and name == "default_bump" and value in {"patch", "minor", "major"}:
            return value
        digest = self._digest(value)
        if name.endswith("_at") or name in {"started_at", "finished_at"}:
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
                return value
            except ValueError:
                pass
        if value.isdecimal():
            return str(int(digest[:12], 16))
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
        self, value: Any, schema: Mapping[str, Any], root: Mapping[str, Any], name: str = ""
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
                else "string"
            )
            schema = next(
                (item for item in candidates if item.get("type") == kind or "$ref" in item), {}
            )
            return self.scrub(value, schema, root, name)
        if isinstance(value, dict):
            properties = schema.get("properties", {})
            additional = schema.get("additionalProperties", {})
            return {
                key if properties else self.string(key, "map_key"): self.scrub(
                    item,
                    properties.get(key, additional if isinstance(additional, dict) else {}),
                    root,
                    key,
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self.scrub(item, schema.get("items", {}), root, name) for item in value]
        if isinstance(value, str):
            if value in schema.get("enum", []) or ("const" in schema and value == schema["const"]):
                return value
            return self.string(value, name, protocol=bool(schema))
        return value


def export_stored_record_snapshot(engine: Engine) -> StoredRecordSnapshot:
    scrubber = _Scrubber()
    tables: dict[str, list[dict[str, Any]]] = {}
    row_count = 0
    byte_count = 0
    deadline = time.monotonic() + 20
    # One consistent, explicitly read-only PostgreSQL transaction. CI never
    # receives this engine, its URL, keys or SQL; it receives only the scrubbed JSON.
    with engine.connect() as connection:
        if engine.dialect.name == "postgresql":
            connection = connection.execution_options(isolation_level="REPEATABLE READ")
            connection.execute(text("SET TRANSACTION READ ONLY"))
            connection.execute(text("SET LOCAL statement_timeout = '15000ms'"))
        revision = str(
            connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        )
        for row_type, model_type in SNAPSHOT_RECORD_TYPES:
            table = row_type.__table__
            schema = model_type.model_json_schema()
            rows: list[dict[str, Any]] = []
            remaining = int((deadline - time.monotonic()) * 1000)
            if remaining <= 0:
                raise SnapshotUnavailable("snapshot_time_limit")
            if engine.dialect.name == "postgresql":
                connection.execute(
                    text(f"SET LOCAL statement_timeout = '{min(remaining, 15000)}ms'")
                )
            for raw in connection.execute(select(table).limit(SNAPSHOT_MAX_ROWS + 1)).mappings():
                row_count += 1
                if row_count > SNAPSHOT_MAX_ROWS:
                    raise SnapshotUnavailable("snapshot_row_limit")
                byte_count += len(json.dumps(dict(raw)).encode())
                if byte_count > SNAPSHOT_MAX_BYTES or time.monotonic() > deadline:
                    raise SnapshotUnavailable("snapshot_export_limit")
                payload = scrubber.scrub(raw["payload"], schema, schema)
                # Check the source digest first. A scrubbed digest is evidence
                # about the scrubbed policy; it cannot attest production authority.
                if model_type is LaunchplaneAuthzPolicyRecord:
                    model_type.model_validate(raw["payload"])
                    payload["policy_sha256"] = authz_policy_sha256(
                        LaunchplaneAuthzPolicy.model_validate(payload["policy"])
                    )
                elif model_type is MergeTrainPolicyRecord:
                    original = model_type.model_validate(raw["payload"])
                    policy_type = type(original.policy)
                    payload["policy_sha256"] = policy_type.model_validate(
                        payload["policy"], context={"historical_policy_record": True}
                    ).policy_sha256
                row = {
                    key: scrubber.string(value, key) if isinstance(value, str) else value
                    for key, value in raw.items()
                    if key != "payload"
                }
                # Indexed projections share the payload's scrubbed values.
                for key in row:
                    if key in payload:
                        if raw[key] != raw["payload"].get(key):
                            raise SnapshotUnavailable("snapshot_projection_conflict")
                        row[key] = payload[key]
                row["payload"] = payload
                rows.append(row)
            tables[table.name] = rows
        snapshot = StoredRecordSnapshot(
            captured_at=datetime.now(timezone.utc).isoformat(),
            database_schema_revision=revision,
            tables=tables,
        )
    active_policies = [
        row for row in tables[LaunchplaneAuthzPolicyRow.__tablename__] if row["status"] == "active"
    ]
    if len(active_policies) != 1:
        raise SnapshotUnavailable("snapshot_requires_one_active_policy")
    if len(snapshot.model_dump_json().encode()) > SNAPSHOT_MAX_BYTES:
        raise SnapshotUnavailable("snapshot_byte_limit")
    return snapshot
