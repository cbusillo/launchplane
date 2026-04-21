from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, TypeVar

from pydantic import BaseModel
from sqlalchemy import JSON, Index, Integer, String, create_engine, desc, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.idempotency_record import build_launchplane_idempotency_record_id
from control_plane.contracts.preview_generation_record import PreviewGenerationRecord
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.promotion_record import PromotionRecord
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.contracts.secret_record import SecretAuditEvent, SecretBinding, SecretRecord, SecretVersion
from control_plane.storage.filesystem import FilesystemRecordStore

RecordModel = TypeVar("RecordModel", bound=BaseModel)
ConnectionFactory = Callable[[], Any]
PayloadDict = dict[str, Any]
PayloadJsonType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


class LaunchplaneBackupGateRow(Base):
    __tablename__ = "launchplane_backup_gates"
    __table_args__ = (
        Index("launchplane_backup_gates_context_instance_idx", "context", "instance", desc("created_at")),
    )

    record_id: Mapped[str] = mapped_column(String, primary_key=True)
    context: Mapped[str] = mapped_column(String, nullable=False)
    instance: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneDeploymentRow(Base):
    __tablename__ = "launchplane_deployments"
    __table_args__ = (
        Index(
            "launchplane_deployments_context_instance_idx",
            "context",
            "instance",
            desc("deploy_finished_at"),
            desc("deploy_started_at"),
        ),
    )

    record_id: Mapped[str] = mapped_column(String, primary_key=True)
    context: Mapped[str] = mapped_column(String, nullable=False)
    instance: Mapped[str] = mapped_column(String, nullable=False)
    artifact_id: Mapped[str] = mapped_column(String, nullable=False)
    source_git_ref: Mapped[str] = mapped_column(String, nullable=False)
    deploy_started_at: Mapped[str] = mapped_column(String, nullable=False)
    deploy_finished_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplanePromotionRow(Base):
    __tablename__ = "launchplane_promotions"
    __table_args__ = (
        Index(
            "launchplane_promotions_context_path_idx",
            "context",
            "from_instance",
            "to_instance",
            desc("deploy_finished_at"),
            desc("deploy_started_at"),
        ),
    )

    record_id: Mapped[str] = mapped_column(String, primary_key=True)
    context: Mapped[str] = mapped_column(String, nullable=False)
    from_instance: Mapped[str] = mapped_column(String, nullable=False)
    to_instance: Mapped[str] = mapped_column(String, nullable=False)
    artifact_id: Mapped[str] = mapped_column(String, nullable=False)
    deploy_started_at: Mapped[str] = mapped_column(String, nullable=False)
    deploy_finished_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneInventoryRow(Base):
    __tablename__ = "launchplane_inventory"
    __table_args__ = (Index("launchplane_inventory_updated_idx", desc("updated_at")),)

    context: Mapped[str] = mapped_column(String, primary_key=True)
    instance: Mapped[str] = mapped_column(String, primary_key=True)
    artifact_id: Mapped[str] = mapped_column(String, nullable=False)
    source_git_ref: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    deployment_record_id: Mapped[str] = mapped_column(String, nullable=False)
    promotion_record_id: Mapped[str] = mapped_column(String, nullable=False)
    promoted_from_instance: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplanePreviewRow(Base):
    __tablename__ = "launchplane_preview_records"
    __table_args__ = (
        Index(
            "launchplane_preview_records_lookup_idx",
            "context",
            "anchor_repo",
            "anchor_pr_number",
            desc("updated_at"),
        ),
    )

    preview_id: Mapped[str] = mapped_column(String, primary_key=True)
    context: Mapped[str] = mapped_column(String, nullable=False)
    anchor_repo: Mapped[str] = mapped_column(String, nullable=False)
    anchor_pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplanePreviewGenerationRow(Base):
    __tablename__ = "launchplane_preview_generations"
    __table_args__ = (
        Index(
            "launchplane_preview_generations_preview_idx",
            "preview_id",
            desc("sequence"),
            desc("requested_at"),
        ),
    )

    generation_id: Mapped[str] = mapped_column(String, primary_key=True)
    preview_id: Mapped[str] = mapped_column(String, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False)
    requested_at: Mapped[str] = mapped_column(String, nullable=False)
    finished_at: Mapped[str] = mapped_column(String, nullable=False)
    artifact_id: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneReleaseTupleRow(Base):
    __tablename__ = "launchplane_release_tuples"
    __table_args__ = (Index("launchplane_release_tuples_minted_idx", desc("minted_at")),)

    context: Mapped[str] = mapped_column(String, primary_key=True)
    channel: Mapped[str] = mapped_column(String, primary_key=True)
    tuple_id: Mapped[str] = mapped_column(String, nullable=False)
    artifact_id: Mapped[str] = mapped_column(String, nullable=False)
    minted_at: Mapped[str] = mapped_column(String, nullable=False)
    provenance: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneIdempotencyRow(Base):
    __tablename__ = "launchplane_idempotency_records"
    __table_args__ = (
        Index(
            "launchplane_idempotency_scope_route_key_idx",
            "scope",
            "route_path",
            "idempotency_key",
            unique=True,
        ),
    )

    record_id: Mapped[str] = mapped_column(String, primary_key=True)
    scope: Mapped[str] = mapped_column(String, nullable=False)
    route_path: Mapped[str] = mapped_column(String, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    response_status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    response_trace_id: Mapped[str] = mapped_column(String, nullable=False)
    recorded_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneSecretRow(Base):
    __tablename__ = "launchplane_secrets"
    __table_args__ = (
        Index(
            "launchplane_secrets_scope_name_idx",
            "scope",
            "integration",
            "name",
            "context",
            "instance",
            unique=True,
        ),
        Index("launchplane_secrets_lookup_idx", "integration", "context", "instance", desc("updated_at")),
    )

    secret_id: Mapped[str] = mapped_column(String, primary_key=True)
    scope: Mapped[str] = mapped_column(String, nullable=False)
    integration: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    context: Mapped[str] = mapped_column(String, nullable=False)
    instance: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    current_version_id: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneSecretVersionRow(Base):
    __tablename__ = "launchplane_secret_versions"
    __table_args__ = (
        Index("launchplane_secret_versions_secret_idx", "secret_id", desc("created_at")),
    )

    version_id: Mapped[str] = mapped_column(String, primary_key=True)
    secret_id: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneSecretBindingRow(Base):
    __tablename__ = "launchplane_secret_bindings"
    __table_args__ = (
        Index(
            "launchplane_secret_bindings_lookup_idx",
            "integration",
            "context",
            "instance",
            "binding_key",
            desc("updated_at"),
        ),
    )

    binding_id: Mapped[str] = mapped_column(String, primary_key=True)
    secret_id: Mapped[str] = mapped_column(String, nullable=False)
    integration: Mapped[str] = mapped_column(String, nullable=False)
    binding_key: Mapped[str] = mapped_column(String, nullable=False)
    context: Mapped[str] = mapped_column(String, nullable=False)
    instance: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneSecretAuditEventRow(Base):
    __tablename__ = "launchplane_secret_audit_events"
    __table_args__ = (
        Index("launchplane_secret_audit_events_secret_idx", "secret_id", desc("recorded_at")),
    )

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    secret_id: Mapped[str] = mapped_column(String, nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    recorded_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


def _artifact_id_from_model(model: BaseModel) -> str:
    artifact_identity = getattr(model, "artifact_identity", None)
    artifact_id = getattr(artifact_identity, "artifact_id", "")
    return artifact_id if isinstance(artifact_id, str) else ""


def _build_engine(database_url: str, *, connection_factory: ConnectionFactory | None = None):
    engine_kwargs: dict[str, Any] = {}
    if connection_factory is not None:
        engine_kwargs["creator"] = connection_factory
    if database_url.startswith("sqlite"):
        engine_kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(database_url, **engine_kwargs)


class PostgresRecordStore:
    def __init__(
        self,
        *,
        database_url: str,
        connection_factory: ConnectionFactory | None = None,
    ) -> None:
        self.database_url = database_url
        self._engine = _build_engine(database_url, connection_factory=connection_factory)
        self._session_factory = sessionmaker(self._engine, expire_on_commit=False)

    @property
    def backend_name(self) -> str:
        return "postgres"

    def ensure_schema(self) -> None:
        Base.metadata.create_all(self._engine)

    def close(self) -> None:
        self._engine.dispose()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            return None

    def _payload_dict(self, model: BaseModel) -> PayloadDict:
        return model.model_dump(mode="json", exclude_none=True)

    def _read_payload(self, *, model_type: type[RecordModel], payload: PayloadDict) -> RecordModel:
        return model_type.model_validate(payload)

    def _write_row(self, row: Base) -> None:
        with self._session_factory() as session:
            session.merge(row)
            session.commit()

    def _read_model(
        self,
        *,
        model_type: type[RecordModel],
        orm_model: type[Base],
        filters: Sequence[object],
    ) -> RecordModel:
        statement = select(orm_model).where(*filters).limit(1)
        with self._session_factory() as session:
            row = session.scalar(statement)
            if row is None:
                raise FileNotFoundError(f"No Launchplane record found in {orm_model.__tablename__} for {tuple(filters)!r}")
            return self._read_payload(model_type=model_type, payload=getattr(row, "payload"))

    def _list_models(
        self,
        *,
        model_type: type[RecordModel],
        orm_model: type[Base],
        filters: Sequence[object] = (),
        order_by: Sequence[object],
        limit: int | None = None,
    ) -> tuple[RecordModel, ...]:
        statement = select(orm_model)
        if filters:
            statement = statement.where(*filters)
        statement = statement.order_by(*order_by)
        if limit is not None:
            statement = statement.limit(limit)
        with self._session_factory() as session:
            rows = session.scalars(statement).all()
            return tuple(self._read_payload(model_type=model_type, payload=row.payload) for row in rows)

    def write_backup_gate_record(self, record: BackupGateRecord) -> None:
        self._write_row(
            LaunchplaneBackupGateRow(
                record_id=record.record_id,
                context=record.context,
                instance=record.instance,
                created_at=record.created_at,
                status=record.status,
                payload=self._payload_dict(record),
            )
        )

    def read_backup_gate_record(self, record_id: str) -> BackupGateRecord:
        return self._read_model(
            model_type=BackupGateRecord,
            orm_model=LaunchplaneBackupGateRow,
            filters=(LaunchplaneBackupGateRow.record_id == record_id,),
        )

    def list_backup_gate_records(
        self,
        *,
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[BackupGateRecord, ...]:
        filters: list[object] = []
        if context_name:
            filters.append(LaunchplaneBackupGateRow.context == context_name)
        if instance_name:
            filters.append(LaunchplaneBackupGateRow.instance == instance_name)
        return self._list_models(
            model_type=BackupGateRecord,
            orm_model=LaunchplaneBackupGateRow,
            filters=filters,
            order_by=(LaunchplaneBackupGateRow.created_at.desc(), LaunchplaneBackupGateRow.record_id.desc()),
            limit=limit,
        )

    def write_idempotency_record(self, record: LaunchplaneIdempotencyRecord) -> None:
        self._write_row(
            LaunchplaneIdempotencyRow(
                record_id=record.record_id,
                scope=record.scope,
                route_path=record.route_path,
                idempotency_key=record.idempotency_key,
                request_fingerprint=record.request_fingerprint,
                response_status_code=record.response_status_code,
                response_trace_id=record.response_trace_id,
                recorded_at=record.recorded_at,
                payload=self._payload_dict(record),
            )
        )

    def read_idempotency_record(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
    ) -> LaunchplaneIdempotencyRecord | None:
        record_id = build_launchplane_idempotency_record_id(
            scope=scope,
            route_path=route_path,
            idempotency_key=idempotency_key,
        )
        statement = select(LaunchplaneIdempotencyRow).where(LaunchplaneIdempotencyRow.record_id == record_id).limit(1)
        with self._session_factory() as session:
            row = session.scalar(statement)
            if row is None:
                return None
            return self._read_payload(model_type=LaunchplaneIdempotencyRecord, payload=row.payload)

    def write_deployment_record(self, record: DeploymentRecord) -> None:
        self._write_row(
            LaunchplaneDeploymentRow(
                record_id=record.record_id,
                context=record.context,
                instance=record.instance,
                artifact_id=_artifact_id_from_model(record),
                source_git_ref=record.source_git_ref,
                deploy_started_at=record.deploy.started_at,
                deploy_finished_at=record.deploy.finished_at,
                payload=self._payload_dict(record),
            )
        )

    def read_deployment_record(self, record_id: str) -> DeploymentRecord:
        return self._read_model(
            model_type=DeploymentRecord,
            orm_model=LaunchplaneDeploymentRow,
            filters=(LaunchplaneDeploymentRow.record_id == record_id,),
        )

    def list_deployment_records(
        self,
        *,
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[DeploymentRecord, ...]:
        filters: list[object] = []
        if context_name:
            filters.append(LaunchplaneDeploymentRow.context == context_name)
        if instance_name:
            filters.append(LaunchplaneDeploymentRow.instance == instance_name)
        return self._list_models(
            model_type=DeploymentRecord,
            orm_model=LaunchplaneDeploymentRow,
            filters=filters,
            order_by=(
                LaunchplaneDeploymentRow.deploy_finished_at.desc(),
                LaunchplaneDeploymentRow.deploy_started_at.desc(),
                LaunchplaneDeploymentRow.record_id.desc(),
            ),
            limit=limit,
        )

    def write_promotion_record(self, record: PromotionRecord) -> None:
        self._write_row(
            LaunchplanePromotionRow(
                record_id=record.record_id,
                context=record.context,
                from_instance=record.from_instance,
                to_instance=record.to_instance,
                artifact_id=record.artifact_identity.artifact_id,
                deploy_started_at=record.deploy.started_at,
                deploy_finished_at=record.deploy.finished_at,
                payload=self._payload_dict(record),
            )
        )

    def read_promotion_record(self, record_id: str) -> PromotionRecord:
        return self._read_model(
            model_type=PromotionRecord,
            orm_model=LaunchplanePromotionRow,
            filters=(LaunchplanePromotionRow.record_id == record_id,),
        )

    def list_promotion_records(
        self,
        *,
        context_name: str = "",
        from_instance_name: str = "",
        to_instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[PromotionRecord, ...]:
        filters: list[object] = []
        if context_name:
            filters.append(LaunchplanePromotionRow.context == context_name)
        if from_instance_name:
            filters.append(LaunchplanePromotionRow.from_instance == from_instance_name)
        if to_instance_name:
            filters.append(LaunchplanePromotionRow.to_instance == to_instance_name)
        return self._list_models(
            model_type=PromotionRecord,
            orm_model=LaunchplanePromotionRow,
            filters=filters,
            order_by=(
                LaunchplanePromotionRow.deploy_finished_at.desc(),
                LaunchplanePromotionRow.deploy_started_at.desc(),
                LaunchplanePromotionRow.record_id.desc(),
            ),
            limit=limit,
        )

    def write_environment_inventory(self, record: EnvironmentInventory) -> None:
        self._write_row(
            LaunchplaneInventoryRow(
                context=record.context,
                instance=record.instance,
                artifact_id=_artifact_id_from_model(record),
                source_git_ref=record.source_git_ref,
                updated_at=record.updated_at,
                deployment_record_id=record.deployment_record_id,
                promotion_record_id=record.promotion_record_id,
                promoted_from_instance=record.promoted_from_instance,
                payload=self._payload_dict(record),
            )
        )

    def read_environment_inventory(self, *, context_name: str, instance_name: str) -> EnvironmentInventory:
        return self._read_model(
            model_type=EnvironmentInventory,
            orm_model=LaunchplaneInventoryRow,
            filters=(LaunchplaneInventoryRow.context == context_name, LaunchplaneInventoryRow.instance == instance_name),
        )

    def list_environment_inventory(self) -> tuple[EnvironmentInventory, ...]:
        return self._list_models(
            model_type=EnvironmentInventory,
            orm_model=LaunchplaneInventoryRow,
            order_by=(LaunchplaneInventoryRow.context.asc(), LaunchplaneInventoryRow.instance.asc()),
        )

    def write_preview_record(self, record: PreviewRecord) -> None:
        self._write_row(
            LaunchplanePreviewRow(
                preview_id=record.preview_id,
                context=record.context,
                anchor_repo=record.anchor_repo,
                anchor_pr_number=record.anchor_pr_number,
                state=record.state,
                updated_at=record.updated_at,
                payload=self._payload_dict(record),
            )
        )

    def read_preview_record(self, preview_id: str) -> PreviewRecord:
        return self._read_model(
            model_type=PreviewRecord,
            orm_model=LaunchplanePreviewRow,
            filters=(LaunchplanePreviewRow.preview_id == preview_id,),
        )

    def list_preview_records(
        self,
        *,
        context_name: str = "",
        anchor_repo: str = "",
        anchor_pr_number: int | None = None,
        limit: int | None = None,
    ) -> tuple[PreviewRecord, ...]:
        filters: list[object] = []
        if context_name:
            filters.append(LaunchplanePreviewRow.context == context_name)
        if anchor_repo:
            filters.append(LaunchplanePreviewRow.anchor_repo == anchor_repo)
        if anchor_pr_number is not None:
            filters.append(LaunchplanePreviewRow.anchor_pr_number == anchor_pr_number)
        return self._list_models(
            model_type=PreviewRecord,
            orm_model=LaunchplanePreviewRow,
            filters=filters,
            order_by=(LaunchplanePreviewRow.updated_at.desc(), LaunchplanePreviewRow.preview_id.desc()),
            limit=limit,
        )

    def write_preview_generation_record(self, record: PreviewGenerationRecord) -> None:
        self._write_row(
            LaunchplanePreviewGenerationRow(
                generation_id=record.generation_id,
                preview_id=record.preview_id,
                sequence=record.sequence,
                state=record.state,
                requested_at=record.requested_at,
                finished_at=record.finished_at,
                artifact_id=record.artifact_id,
                payload=self._payload_dict(record),
            )
        )

    def read_preview_generation_record(self, generation_id: str) -> PreviewGenerationRecord:
        return self._read_model(
            model_type=PreviewGenerationRecord,
            orm_model=LaunchplanePreviewGenerationRow,
            filters=(LaunchplanePreviewGenerationRow.generation_id == generation_id,),
        )

    def list_preview_generation_records(
        self,
        *,
        preview_id: str = "",
        limit: int | None = None,
    ) -> tuple[PreviewGenerationRecord, ...]:
        filters: list[object] = []
        if preview_id:
            filters.append(LaunchplanePreviewGenerationRow.preview_id == preview_id)
        return self._list_models(
            model_type=PreviewGenerationRecord,
            orm_model=LaunchplanePreviewGenerationRow,
            filters=filters,
            order_by=(
                LaunchplanePreviewGenerationRow.sequence.desc(),
                LaunchplanePreviewGenerationRow.requested_at.desc(),
                LaunchplanePreviewGenerationRow.generation_id.desc(),
            ),
            limit=limit,
        )

    def write_release_tuple_record(self, record: ReleaseTupleRecord) -> None:
        self._write_row(
            LaunchplaneReleaseTupleRow(
                context=record.context,
                channel=record.channel,
                tuple_id=record.tuple_id,
                artifact_id=record.artifact_id,
                minted_at=record.minted_at,
                provenance=record.provenance,
                payload=self._payload_dict(record),
            )
        )

    def read_release_tuple_record(self, *, context_name: str, channel_name: str) -> ReleaseTupleRecord:
        return self._read_model(
            model_type=ReleaseTupleRecord,
            orm_model=LaunchplaneReleaseTupleRow,
            filters=(LaunchplaneReleaseTupleRow.context == context_name, LaunchplaneReleaseTupleRow.channel == channel_name),
        )

    def list_release_tuple_records(self) -> tuple[ReleaseTupleRecord, ...]:
        return self._list_models(
            model_type=ReleaseTupleRecord,
            orm_model=LaunchplaneReleaseTupleRow,
            order_by=(LaunchplaneReleaseTupleRow.context.asc(), LaunchplaneReleaseTupleRow.channel.asc()),
        )

    def write_secret_record(self, record: SecretRecord) -> None:
        self._write_row(
            LaunchplaneSecretRow(
                secret_id=record.secret_id,
                scope=record.scope,
                integration=record.integration,
                name=record.name,
                context=record.context,
                instance=record.instance,
                status=record.status,
                current_version_id=record.current_version_id,
                updated_at=record.updated_at,
                payload=self._payload_dict(record),
            )
        )

    def read_secret_record(self, secret_id: str) -> SecretRecord:
        return self._read_model(
            model_type=SecretRecord,
            orm_model=LaunchplaneSecretRow,
            filters=(LaunchplaneSecretRow.secret_id == secret_id,),
        )

    def find_secret_record(
        self,
        *,
        scope: str,
        integration: str,
        name: str,
        context: str = "",
        instance: str = "",
    ) -> SecretRecord | None:
        records = self._list_models(
            model_type=SecretRecord,
            orm_model=LaunchplaneSecretRow,
            filters=(
                LaunchplaneSecretRow.scope == scope,
                LaunchplaneSecretRow.integration == integration,
                LaunchplaneSecretRow.name == name,
                LaunchplaneSecretRow.context == context,
                LaunchplaneSecretRow.instance == instance,
            ),
            order_by=(LaunchplaneSecretRow.updated_at.desc(), LaunchplaneSecretRow.secret_id.desc()),
            limit=1,
        )
        return records[0] if records else None

    def list_secret_records(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[SecretRecord, ...]:
        filters: list[object] = []
        if integration:
            filters.append(LaunchplaneSecretRow.integration == integration)
        if context_name:
            filters.append(LaunchplaneSecretRow.context == context_name)
        if instance_name:
            filters.append(LaunchplaneSecretRow.instance == instance_name)
        return self._list_models(
            model_type=SecretRecord,
            orm_model=LaunchplaneSecretRow,
            filters=filters,
            order_by=(LaunchplaneSecretRow.updated_at.desc(), LaunchplaneSecretRow.secret_id.desc()),
            limit=limit,
        )

    def write_secret_version(self, version: SecretVersion) -> None:
        self._write_row(
            LaunchplaneSecretVersionRow(
                version_id=version.version_id,
                secret_id=version.secret_id,
                created_at=version.created_at,
                payload=self._payload_dict(version),
            )
        )

    def read_secret_version(self, version_id: str) -> SecretVersion:
        return self._read_model(
            model_type=SecretVersion,
            orm_model=LaunchplaneSecretVersionRow,
            filters=(LaunchplaneSecretVersionRow.version_id == version_id,),
        )

    def list_secret_versions(self, *, secret_id: str) -> tuple[SecretVersion, ...]:
        return self._list_models(
            model_type=SecretVersion,
            orm_model=LaunchplaneSecretVersionRow,
            filters=(LaunchplaneSecretVersionRow.secret_id == secret_id,),
            order_by=(LaunchplaneSecretVersionRow.created_at.desc(), LaunchplaneSecretVersionRow.version_id.desc()),
        )

    def write_secret_binding(self, binding: SecretBinding) -> None:
        self._write_row(
            LaunchplaneSecretBindingRow(
                binding_id=binding.binding_id,
                secret_id=binding.secret_id,
                integration=binding.integration,
                binding_key=binding.binding_key,
                context=binding.context,
                instance=binding.instance,
                status=binding.status,
                updated_at=binding.updated_at,
                payload=self._payload_dict(binding),
            )
        )

    def list_secret_bindings(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[SecretBinding, ...]:
        filters: list[object] = []
        if integration:
            filters.append(LaunchplaneSecretBindingRow.integration == integration)
        if context_name:
            filters.append(LaunchplaneSecretBindingRow.context == context_name)
        if instance_name:
            filters.append(LaunchplaneSecretBindingRow.instance == instance_name)
        return self._list_models(
            model_type=SecretBinding,
            orm_model=LaunchplaneSecretBindingRow,
            filters=filters,
            order_by=(LaunchplaneSecretBindingRow.updated_at.desc(), LaunchplaneSecretBindingRow.binding_id.desc()),
            limit=limit,
        )

    def write_secret_audit_event(self, event: SecretAuditEvent) -> None:
        self._write_row(
            LaunchplaneSecretAuditEventRow(
                event_id=event.event_id,
                secret_id=event.secret_id,
                event_type=event.event_type,
                recorded_at=event.recorded_at,
                payload=self._payload_dict(event),
            )
        )

    def list_secret_audit_events(self, *, secret_id: str) -> tuple[SecretAuditEvent, ...]:
        return self._list_models(
            model_type=SecretAuditEvent,
            orm_model=LaunchplaneSecretAuditEventRow,
            filters=(LaunchplaneSecretAuditEventRow.secret_id == secret_id,),
            order_by=(LaunchplaneSecretAuditEventRow.recorded_at.desc(), LaunchplaneSecretAuditEventRow.event_id.desc()),
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
