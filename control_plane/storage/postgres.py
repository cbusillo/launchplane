from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any, TypeVar

from pydantic import BaseModel

from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.preview_generation_record import PreviewGenerationRecord
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.promotion_record import PromotionRecord
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.storage.filesystem import FilesystemRecordStore

RecordModel = TypeVar("RecordModel", bound=BaseModel)
ConnectionFactory = Callable[[], Any]


SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS harbor_backup_gates (
        record_id TEXT PRIMARY KEY,
        context TEXT NOT NULL,
        instance TEXT NOT NULL,
        created_at TEXT NOT NULL,
        status TEXT NOT NULL,
        payload JSONB NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS harbor_backup_gates_context_instance_idx ON harbor_backup_gates (context, instance, created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS harbor_deployments (
        record_id TEXT PRIMARY KEY,
        context TEXT NOT NULL,
        instance TEXT NOT NULL,
        artifact_id TEXT NOT NULL,
        source_git_ref TEXT NOT NULL,
        deploy_started_at TEXT NOT NULL,
        deploy_finished_at TEXT NOT NULL,
        payload JSONB NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS harbor_deployments_context_instance_idx ON harbor_deployments (context, instance, deploy_finished_at DESC, deploy_started_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS harbor_promotions (
        record_id TEXT PRIMARY KEY,
        context TEXT NOT NULL,
        from_instance TEXT NOT NULL,
        to_instance TEXT NOT NULL,
        artifact_id TEXT NOT NULL,
        deploy_started_at TEXT NOT NULL,
        deploy_finished_at TEXT NOT NULL,
        payload JSONB NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS harbor_promotions_context_path_idx ON harbor_promotions (context, from_instance, to_instance, deploy_finished_at DESC, deploy_started_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS harbor_inventory (
        context TEXT NOT NULL,
        instance TEXT NOT NULL,
        artifact_id TEXT NOT NULL,
        source_git_ref TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        deployment_record_id TEXT NOT NULL,
        promotion_record_id TEXT NOT NULL,
        promoted_from_instance TEXT NOT NULL,
        payload JSONB NOT NULL,
        PRIMARY KEY (context, instance)
    )
    """,
    "CREATE INDEX IF NOT EXISTS harbor_inventory_updated_idx ON harbor_inventory (updated_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS harbor_preview_records (
        preview_id TEXT PRIMARY KEY,
        context TEXT NOT NULL,
        anchor_repo TEXT NOT NULL,
        anchor_pr_number INTEGER NOT NULL,
        state TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        payload JSONB NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS harbor_preview_records_lookup_idx ON harbor_preview_records (context, anchor_repo, anchor_pr_number, updated_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS harbor_preview_generations (
        generation_id TEXT PRIMARY KEY,
        preview_id TEXT NOT NULL,
        sequence INTEGER NOT NULL,
        state TEXT NOT NULL,
        requested_at TEXT NOT NULL,
        finished_at TEXT NOT NULL,
        artifact_id TEXT NOT NULL,
        payload JSONB NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS harbor_preview_generations_preview_idx ON harbor_preview_generations (preview_id, sequence DESC, requested_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS harbor_release_tuples (
        context TEXT NOT NULL,
        channel TEXT NOT NULL,
        tuple_id TEXT NOT NULL,
        artifact_id TEXT NOT NULL,
        minted_at TEXT NOT NULL,
        provenance TEXT NOT NULL,
        payload JSONB NOT NULL,
        PRIMARY KEY (context, channel)
    )
    """,
    "CREATE INDEX IF NOT EXISTS harbor_release_tuples_minted_idx ON harbor_release_tuples (minted_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS harbor_idempotency_keys (
        idempotency_key TEXT PRIMARY KEY,
        route TEXT NOT NULL,
        first_seen_at TEXT NOT NULL,
        response_payload JSONB NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS harbor_audit_events (
        event_id TEXT PRIMARY KEY,
        event_type TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        payload JSONB NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS harbor_audit_events_recorded_idx ON harbor_audit_events (recorded_at DESC)",
)


def _artifact_id_from_model(model: BaseModel) -> str:
    artifact_identity = getattr(model, "artifact_identity", None)
    artifact_id = getattr(artifact_identity, "artifact_id", "")
    return artifact_id if isinstance(artifact_id, str) else ""


def _default_connection_factory(database_url: str) -> ConnectionFactory:
    def connect() -> Any:
        import psycopg

        return psycopg.connect(database_url)

    return connect


class PostgresRecordStore:
    def __init__(
        self,
        *,
        database_url: str,
        connection_factory: ConnectionFactory | None = None,
    ) -> None:
        self.database_url = database_url
        self._connection_factory = connection_factory or _default_connection_factory(database_url)

    @property
    def backend_name(self) -> str:
        return "postgres"

    def ensure_schema(self) -> None:
        with self._connection_factory() as connection:
            with connection.cursor() as cursor:
                for statement in SCHEMA_STATEMENTS:
                    cursor.execute(statement)
            connection.commit()

    def _payload_json(self, model: BaseModel) -> str:
        return json.dumps(model.model_dump(mode="json", exclude_none=True), sort_keys=True)

    def _fetch_payload_text(self, row: object) -> str:
        if isinstance(row, dict):
            payload = row.get("payload")
        else:
            payload = row[0] if row is not None else None
        if not isinstance(payload, str):
            raise RuntimeError("Postgres record store expected payload text from query row.")
        return payload

    def _write_model(
        self,
        *,
        table: str,
        columns: Sequence[str],
        values: Sequence[object],
        conflict_columns: Sequence[str],
        model: BaseModel,
    ) -> None:
        assignment_columns = [*columns, "payload"]
        placeholders = ["%s" for _ in values] + ["%s::jsonb"]
        update_clause = ", ".join(f"{column} = EXCLUDED.{column}" for column in assignment_columns)
        query = (
            f"INSERT INTO {table} ({', '.join(assignment_columns)}) "
            f"VALUES ({', '.join(placeholders)}) "
            f"ON CONFLICT ({', '.join(conflict_columns)}) DO UPDATE SET {update_clause}"
        )
        params = (*values, self._payload_json(model))
        with self._connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, params)
            connection.commit()

    def _read_model(
        self,
        *,
        model_type: type[RecordModel],
        table: str,
        where_clause: str,
        params: Sequence[object],
    ) -> RecordModel:
        query = f"SELECT payload::text FROM {table} WHERE {where_clause}"
        with self._connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, params)
                row = cursor.fetchone()
        if row is None:
            raise FileNotFoundError(f"No Harbor record found in {table} for {params!r}")
        return model_type.model_validate(json.loads(self._fetch_payload_text(row)))

    def _list_models(
        self,
        *,
        model_type: type[RecordModel],
        table: str,
        filters: Sequence[tuple[str, object]] = (),
        order_by: str,
        limit: int | None = None,
    ) -> tuple[RecordModel, ...]:
        clauses: list[str] = []
        params: list[object] = []
        for clause, value in filters:
            clauses.append(clause)
            params.append(value)
        query = f"SELECT payload::text FROM {table}"
        if clauses:
            query += f" WHERE {' AND '.join(clauses)}"
        query += f" ORDER BY {order_by}"
        if limit is not None:
            query += " LIMIT %s"
            params.append(limit)
        with self._connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, params)
                rows = cursor.fetchall()
        return tuple(model_type.model_validate(json.loads(self._fetch_payload_text(row))) for row in rows)

    def write_backup_gate_record(self, record: BackupGateRecord) -> None:
        self._write_model(
            table="harbor_backup_gates",
            columns=("record_id", "context", "instance", "created_at", "status"),
            values=(record.record_id, record.context, record.instance, record.created_at, record.status),
            conflict_columns=("record_id",),
            model=record,
        )

    def read_backup_gate_record(self, record_id: str) -> BackupGateRecord:
        return self._read_model(
            model_type=BackupGateRecord,
            table="harbor_backup_gates",
            where_clause="record_id = %s",
            params=(record_id,),
        )

    def list_backup_gate_records(
        self,
        *,
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[BackupGateRecord, ...]:
        filters: list[tuple[str, object]] = []
        if context_name:
            filters.append(("context = %s", context_name))
        if instance_name:
            filters.append(("instance = %s", instance_name))
        return self._list_models(
            model_type=BackupGateRecord,
            table="harbor_backup_gates",
            filters=filters,
            order_by="created_at DESC, record_id DESC",
            limit=limit,
        )

    def write_deployment_record(self, record: DeploymentRecord) -> None:
        self._write_model(
            table="harbor_deployments",
            columns=(
                "record_id",
                "context",
                "instance",
                "artifact_id",
                "source_git_ref",
                "deploy_started_at",
                "deploy_finished_at",
            ),
            values=(
                record.record_id,
                record.context,
                record.instance,
                _artifact_id_from_model(record),
                record.source_git_ref,
                record.deploy.started_at,
                record.deploy.finished_at,
            ),
            conflict_columns=("record_id",),
            model=record,
        )

    def read_deployment_record(self, record_id: str) -> DeploymentRecord:
        return self._read_model(
            model_type=DeploymentRecord,
            table="harbor_deployments",
            where_clause="record_id = %s",
            params=(record_id,),
        )

    def list_deployment_records(
        self,
        *,
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[DeploymentRecord, ...]:
        filters: list[tuple[str, object]] = []
        if context_name:
            filters.append(("context = %s", context_name))
        if instance_name:
            filters.append(("instance = %s", instance_name))
        return self._list_models(
            model_type=DeploymentRecord,
            table="harbor_deployments",
            filters=filters,
            order_by="deploy_finished_at DESC, deploy_started_at DESC, record_id DESC",
            limit=limit,
        )

    def write_promotion_record(self, record: PromotionRecord) -> None:
        self._write_model(
            table="harbor_promotions",
            columns=(
                "record_id",
                "context",
                "from_instance",
                "to_instance",
                "artifact_id",
                "deploy_started_at",
                "deploy_finished_at",
            ),
            values=(
                record.record_id,
                record.context,
                record.from_instance,
                record.to_instance,
                record.artifact_identity.artifact_id,
                record.deploy.started_at,
                record.deploy.finished_at,
            ),
            conflict_columns=("record_id",),
            model=record,
        )

    def read_promotion_record(self, record_id: str) -> PromotionRecord:
        return self._read_model(
            model_type=PromotionRecord,
            table="harbor_promotions",
            where_clause="record_id = %s",
            params=(record_id,),
        )

    def list_promotion_records(
        self,
        *,
        context_name: str = "",
        from_instance_name: str = "",
        to_instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[PromotionRecord, ...]:
        filters: list[tuple[str, object]] = []
        if context_name:
            filters.append(("context = %s", context_name))
        if from_instance_name:
            filters.append(("from_instance = %s", from_instance_name))
        if to_instance_name:
            filters.append(("to_instance = %s", to_instance_name))
        return self._list_models(
            model_type=PromotionRecord,
            table="harbor_promotions",
            filters=filters,
            order_by="deploy_finished_at DESC, deploy_started_at DESC, record_id DESC",
            limit=limit,
        )

    def write_environment_inventory(self, record: EnvironmentInventory) -> None:
        self._write_model(
            table="harbor_inventory",
            columns=(
                "context",
                "instance",
                "artifact_id",
                "source_git_ref",
                "updated_at",
                "deployment_record_id",
                "promotion_record_id",
                "promoted_from_instance",
            ),
            values=(
                record.context,
                record.instance,
                _artifact_id_from_model(record),
                record.source_git_ref,
                record.updated_at,
                record.deployment_record_id,
                record.promotion_record_id,
                record.promoted_from_instance,
            ),
            conflict_columns=("context", "instance"),
            model=record,
        )

    def read_environment_inventory(self, *, context_name: str, instance_name: str) -> EnvironmentInventory:
        return self._read_model(
            model_type=EnvironmentInventory,
            table="harbor_inventory",
            where_clause="context = %s AND instance = %s",
            params=(context_name, instance_name),
        )

    def list_environment_inventory(self) -> tuple[EnvironmentInventory, ...]:
        return self._list_models(
            model_type=EnvironmentInventory,
            table="harbor_inventory",
            order_by="context ASC, instance ASC",
        )

    def write_preview_record(self, record: PreviewRecord) -> None:
        self._write_model(
            table="harbor_preview_records",
            columns=("preview_id", "context", "anchor_repo", "anchor_pr_number", "state", "updated_at"),
            values=(
                record.preview_id,
                record.context,
                record.anchor_repo,
                record.anchor_pr_number,
                record.state,
                record.updated_at,
            ),
            conflict_columns=("preview_id",),
            model=record,
        )

    def read_preview_record(self, preview_id: str) -> PreviewRecord:
        return self._read_model(
            model_type=PreviewRecord,
            table="harbor_preview_records",
            where_clause="preview_id = %s",
            params=(preview_id,),
        )

    def list_preview_records(
        self,
        *,
        context_name: str = "",
        anchor_repo: str = "",
        anchor_pr_number: int | None = None,
        limit: int | None = None,
    ) -> tuple[PreviewRecord, ...]:
        filters: list[tuple[str, object]] = []
        if context_name:
            filters.append(("context = %s", context_name))
        if anchor_repo:
            filters.append(("anchor_repo = %s", anchor_repo))
        if anchor_pr_number is not None:
            filters.append(("anchor_pr_number = %s", anchor_pr_number))
        return self._list_models(
            model_type=PreviewRecord,
            table="harbor_preview_records",
            filters=filters,
            order_by="updated_at DESC, preview_id DESC",
            limit=limit,
        )

    def write_preview_generation_record(self, record: PreviewGenerationRecord) -> None:
        self._write_model(
            table="harbor_preview_generations",
            columns=(
                "generation_id",
                "preview_id",
                "sequence",
                "state",
                "requested_at",
                "finished_at",
                "artifact_id",
            ),
            values=(
                record.generation_id,
                record.preview_id,
                record.sequence,
                record.state,
                record.requested_at,
                record.finished_at,
                record.artifact_id,
            ),
            conflict_columns=("generation_id",),
            model=record,
        )

    def read_preview_generation_record(self, generation_id: str) -> PreviewGenerationRecord:
        return self._read_model(
            model_type=PreviewGenerationRecord,
            table="harbor_preview_generations",
            where_clause="generation_id = %s",
            params=(generation_id,),
        )

    def list_preview_generation_records(
        self,
        *,
        preview_id: str = "",
        limit: int | None = None,
    ) -> tuple[PreviewGenerationRecord, ...]:
        filters: list[tuple[str, object]] = []
        if preview_id:
            filters.append(("preview_id = %s", preview_id))
        return self._list_models(
            model_type=PreviewGenerationRecord,
            table="harbor_preview_generations",
            filters=filters,
            order_by="sequence DESC, requested_at DESC, generation_id DESC",
            limit=limit,
        )

    def write_release_tuple_record(self, record: ReleaseTupleRecord) -> None:
        self._write_model(
            table="harbor_release_tuples",
            columns=("context", "channel", "tuple_id", "artifact_id", "minted_at", "provenance"),
            values=(
                record.context,
                record.channel,
                record.tuple_id,
                record.artifact_id,
                record.minted_at,
                record.provenance,
            ),
            conflict_columns=("context", "channel"),
            model=record,
        )

    def read_release_tuple_record(self, *, context_name: str, channel_name: str) -> ReleaseTupleRecord:
        return self._read_model(
            model_type=ReleaseTupleRecord,
            table="harbor_release_tuples",
            where_clause="context = %s AND channel = %s",
            params=(context_name, channel_name),
        )

    def list_release_tuple_records(self) -> tuple[ReleaseTupleRecord, ...]:
        return self._list_models(
            model_type=ReleaseTupleRecord,
            table="harbor_release_tuples",
            order_by="context ASC, channel ASC",
        )

    def import_core_records_from_filesystem(self, filesystem_store: FilesystemRecordStore) -> dict[str, int]:
        counts = {
            "backup_gates": 0,
            "deployments": 0,
            "promotions": 0,
            "inventory": 0,
            "preview_records": 0,
            "preview_generations": 0,
            "release_tuples": 0,
        }
        for record in filesystem_store.list_backup_gate_records():
            self.write_backup_gate_record(record)
            counts["backup_gates"] += 1
        for record in filesystem_store.list_deployment_records():
            self.write_deployment_record(record)
            counts["deployments"] += 1
        for record in filesystem_store.list_promotion_records():
            self.write_promotion_record(record)
            counts["promotions"] += 1
        for record in filesystem_store.list_environment_inventory():
            self.write_environment_inventory(record)
            counts["inventory"] += 1
        for record in filesystem_store.list_preview_records():
            self.write_preview_record(record)
            counts["preview_records"] += 1
        for record in filesystem_store.list_preview_generation_records():
            self.write_preview_generation_record(record)
            counts["preview_generations"] += 1
        for record in filesystem_store.list_release_tuple_records():
            self.write_release_tuple_record(record)
            counts["release_tuples"] += 1
        return counts
