from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from typing import Any, TypeVar

from pydantic import BaseModel
from sqlalchemy import JSON, Index, Integer, String, create_engine, delete, desc, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.idempotency_record import build_launchplane_idempotency_record_id
from control_plane.contracts.lane_summary import LaunchplaneLaneSummary
from control_plane.contracts.odoo_instance_override_record import OdooInstanceOverrideRecord
from control_plane.contracts.preview_generation_record import PreviewGenerationRecord
from control_plane.contracts.preview_inventory_scan_record import PreviewInventoryScanRecord
from control_plane.contracts.preview_lifecycle_plan_record import PreviewLifecyclePlanRecord
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.preview_summary import LaunchplanePreviewSummary
from control_plane.contracts.promotion_record import PromotionRecord
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.secret_record import (
    SecretAuditEvent,
    SecretBinding,
    SecretRecord,
    SecretVersion,
)
from control_plane.service_auth import GitHubHumanIdentity
from control_plane.service_human_auth import HumanSessionStore, LaunchplaneHumanSession
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
        Index(
            "launchplane_backup_gates_context_instance_idx",
            "context",
            "instance",
            desc("created_at"),
        ),
    )

    record_id: Mapped[str] = mapped_column(String, primary_key=True)
    context: Mapped[str] = mapped_column(String, nullable=False)
    instance: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneArtifactManifestRow(Base):
    __tablename__ = "launchplane_artifact_manifests"
    __table_args__ = (Index("launchplane_artifact_manifests_artifact_idx", desc("artifact_id")),)

    artifact_id: Mapped[str] = mapped_column(String, primary_key=True)
    source_commit: Mapped[str] = mapped_column(String, nullable=False)
    image_repository: Mapped[str] = mapped_column(String, nullable=False)
    image_digest: Mapped[str] = mapped_column(String, nullable=False)
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


class LaunchplanePreviewInventoryScanRow(Base):
    __tablename__ = "launchplane_preview_inventory_scans"
    __table_args__ = (
        Index(
            "launchplane_preview_inventory_scans_context_idx",
            "context",
            desc("scanned_at"),
        ),
    )

    scan_id: Mapped[str] = mapped_column(String, primary_key=True)
    context: Mapped[str] = mapped_column(String, nullable=False)
    scanned_at: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    preview_count: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplanePreviewLifecyclePlanRow(Base):
    __tablename__ = "launchplane_preview_lifecycle_plans"
    __table_args__ = (
        Index(
            "launchplane_preview_lifecycle_plans_context_idx",
            "context",
            desc("planned_at"),
        ),
    )

    plan_id: Mapped[str] = mapped_column(String, primary_key=True)
    product: Mapped[str] = mapped_column(String, nullable=False)
    context: Mapped[str] = mapped_column(String, nullable=False)
    planned_at: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    inventory_scan_id: Mapped[str] = mapped_column(String, nullable=False)
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


class LaunchplaneDokployTargetIdRow(Base):
    __tablename__ = "launchplane_dokploy_target_ids"
    __table_args__ = (Index("launchplane_dokploy_target_ids_updated_idx", desc("updated_at")),)

    context: Mapped[str] = mapped_column(String, primary_key=True)
    instance: Mapped[str] = mapped_column(String, primary_key=True)
    target_id: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneDokployTargetRow(Base):
    __tablename__ = "launchplane_dokploy_targets"
    __table_args__ = (Index("launchplane_dokploy_targets_updated_idx", desc("updated_at")),)

    context: Mapped[str] = mapped_column(String, primary_key=True)
    instance: Mapped[str] = mapped_column(String, primary_key=True)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneRuntimeEnvironmentRow(Base):
    __tablename__ = "launchplane_runtime_environments"
    __table_args__ = (Index("launchplane_runtime_environments_updated_idx", desc("updated_at")),)

    scope: Mapped[str] = mapped_column(String, primary_key=True)
    context: Mapped[str] = mapped_column(String, primary_key=True)
    instance: Mapped[str] = mapped_column(String, primary_key=True)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[PayloadDict] = mapped_column(PayloadJsonType, nullable=False)


class LaunchplaneOdooInstanceOverrideRow(Base):
    __tablename__ = "launchplane_odoo_instance_overrides"
    __table_args__ = (Index("launchplane_odoo_instance_overrides_updated_idx", desc("updated_at")),)

    context: Mapped[str] = mapped_column(String, primary_key=True)
    instance: Mapped[str] = mapped_column(String, primary_key=True)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
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


class LaunchplaneHumanSessionRow(Base):
    __tablename__ = "launchplane_human_sessions"
    __table_args__ = (
        Index("launchplane_human_sessions_login_idx", "login", desc("created_at")),
        Index("launchplane_human_sessions_expires_idx", desc("expires_at")),
    )

    session_id: Mapped[str] = mapped_column(String, primary_key=True)
    login: Mapped[str] = mapped_column(String, nullable=False)
    github_id: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[str] = mapped_column(String, nullable=False)
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
        Index(
            "launchplane_secrets_lookup_idx",
            "integration",
            "context",
            "instance",
            desc("updated_at"),
        ),
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


def _human_session_payload(session: LaunchplaneHumanSession) -> PayloadDict:
    return {
        "session_id": session.session_id,
        "created_at": session.created_at.isoformat(),
        "expires_at": session.expires_at.isoformat(),
        "identity": {
            "login": session.identity.login,
            "github_id": session.identity.github_id,
            "name": session.identity.name,
            "email": session.identity.email,
            "organizations": sorted(session.identity.organizations),
            "teams": sorted(session.identity.teams),
            "role": session.identity.role,
        },
    }


def _human_session_from_payload(payload: PayloadDict) -> LaunchplaneHumanSession:
    identity_payload = payload.get("identity")
    if not isinstance(identity_payload, dict):
        raise ValueError("Launchplane human session payload is missing identity.")
    return LaunchplaneHumanSession(
        session_id=str(payload.get("session_id") or ""),
        created_at=datetime.fromisoformat(str(payload.get("created_at") or "")),
        expires_at=datetime.fromisoformat(str(payload.get("expires_at") or "")),
        identity=GitHubHumanIdentity(
            login=str(identity_payload.get("login") or ""),
            github_id=int(identity_payload.get("github_id") or 0),
            name=str(identity_payload.get("name") or ""),
            email=str(identity_payload.get("email") or ""),
            organizations=frozenset(
                str(value) for value in identity_payload.get("organizations", [])
            ),
            teams=frozenset(str(value) for value in identity_payload.get("teams", [])),
            role="admin" if identity_payload.get("role") == "admin" else "read_only",
        ),
    )


def _build_engine(database_url: str, *, connection_factory: ConnectionFactory | None = None):
    engine_kwargs: dict[str, Any] = {}
    if connection_factory is not None:
        engine_kwargs["creator"] = connection_factory
    if database_url.startswith("sqlite"):
        engine_kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(database_url, **engine_kwargs)


class PostgresRecordStore(HumanSessionStore):
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
                raise FileNotFoundError(
                    f"No Launchplane record found in {orm_model.__tablename__} for {tuple(filters)!r}"
                )
            return self._read_payload(model_type=model_type, payload=getattr(row, "payload"))

    def _read_optional_model(
        self,
        *,
        model_type: type[RecordModel],
        orm_model: type[Base],
        filters: Sequence[object],
    ) -> RecordModel | None:
        try:
            return self._read_model(
                model_type=model_type,
                orm_model=orm_model,
                filters=filters,
            )
        except FileNotFoundError:
            return None

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
            return tuple(
                self._read_payload(model_type=model_type, payload=row.payload) for row in rows
            )

    def write_artifact_manifest(self, manifest: ArtifactIdentityManifest) -> None:
        self._write_row(
            LaunchplaneArtifactManifestRow(
                artifact_id=manifest.artifact_id,
                source_commit=manifest.source_commit,
                image_repository=manifest.image.repository,
                image_digest=manifest.image.digest,
                payload=self._payload_dict(manifest),
            )
        )

    def read_artifact_manifest(self, artifact_id: str) -> ArtifactIdentityManifest:
        return self._read_model(
            model_type=ArtifactIdentityManifest,
            orm_model=LaunchplaneArtifactManifestRow,
            filters=(LaunchplaneArtifactManifestRow.artifact_id == artifact_id,),
        )

    def list_artifact_manifests(self) -> tuple[ArtifactIdentityManifest, ...]:
        return self._list_models(
            model_type=ArtifactIdentityManifest,
            orm_model=LaunchplaneArtifactManifestRow,
            order_by=(LaunchplaneArtifactManifestRow.artifact_id.desc(),),
        )

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
            order_by=(
                LaunchplaneBackupGateRow.created_at.desc(),
                LaunchplaneBackupGateRow.record_id.desc(),
            ),
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
        statement = (
            select(LaunchplaneIdempotencyRow)
            .where(LaunchplaneIdempotencyRow.record_id == record_id)
            .limit(1)
        )
        with self._session_factory() as session:
            row = session.scalar(statement)
            if row is None:
                return None
            return self._read_payload(model_type=LaunchplaneIdempotencyRecord, payload=row.payload)

    def write_session(self, session: LaunchplaneHumanSession) -> None:
        self._write_row(
            LaunchplaneHumanSessionRow(
                session_id=session.session_id,
                login=session.identity.login,
                github_id=session.identity.github_id,
                role=session.identity.role,
                created_at=session.created_at.isoformat(),
                expires_at=session.expires_at.isoformat(),
                payload=_human_session_payload(session),
            )
        )

    def read_session(self, session_id: str) -> LaunchplaneHumanSession | None:
        statement = (
            select(LaunchplaneHumanSessionRow)
            .where(LaunchplaneHumanSessionRow.session_id == session_id)
            .limit(1)
        )
        with self._session_factory() as session:
            row = session.scalar(statement)
            if row is None:
                return None
            human_session = _human_session_from_payload(row.payload)
            if human_session.expires_at <= datetime.now(timezone.utc):
                session.delete(row)
                session.commit()
                return None
            return human_session

    def delete_session(self, session_id: str) -> None:
        with self._session_factory() as session:
            session.execute(
                delete(LaunchplaneHumanSessionRow).where(
                    LaunchplaneHumanSessionRow.session_id == session_id
                )
            )
            session.commit()

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

    def read_environment_inventory(
        self, *, context_name: str, instance_name: str
    ) -> EnvironmentInventory:
        return self._read_model(
            model_type=EnvironmentInventory,
            orm_model=LaunchplaneInventoryRow,
            filters=(
                LaunchplaneInventoryRow.context == context_name,
                LaunchplaneInventoryRow.instance == instance_name,
            ),
        )

    def list_environment_inventory(self) -> tuple[EnvironmentInventory, ...]:
        return self._list_models(
            model_type=EnvironmentInventory,
            orm_model=LaunchplaneInventoryRow,
            order_by=(
                LaunchplaneInventoryRow.context.asc(),
                LaunchplaneInventoryRow.instance.asc(),
            ),
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
            order_by=(
                LaunchplanePreviewRow.updated_at.desc(),
                LaunchplanePreviewRow.preview_id.desc(),
            ),
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

    def write_preview_inventory_scan_record(self, record: PreviewInventoryScanRecord) -> None:
        self._write_row(
            LaunchplanePreviewInventoryScanRow(
                scan_id=record.scan_id,
                context=record.context,
                scanned_at=record.scanned_at,
                source=record.source,
                status=record.status,
                preview_count=record.preview_count,
                payload=self._payload_dict(record),
            )
        )

    def list_preview_inventory_scan_records(
        self,
        *,
        context_name: str = "",
        limit: int | None = None,
    ) -> tuple[PreviewInventoryScanRecord, ...]:
        filters: list[object] = []
        if context_name:
            filters.append(LaunchplanePreviewInventoryScanRow.context == context_name)
        return self._list_models(
            model_type=PreviewInventoryScanRecord,
            orm_model=LaunchplanePreviewInventoryScanRow,
            filters=filters,
            order_by=(
                LaunchplanePreviewInventoryScanRow.scanned_at.desc(),
                LaunchplanePreviewInventoryScanRow.scan_id.desc(),
            ),
            limit=limit,
        )

    def write_preview_lifecycle_plan_record(self, record: PreviewLifecyclePlanRecord) -> None:
        self._write_row(
            LaunchplanePreviewLifecyclePlanRow(
                plan_id=record.plan_id,
                product=record.product,
                context=record.context,
                planned_at=record.planned_at,
                status=record.status,
                inventory_scan_id=record.inventory_scan_id,
                payload=self._payload_dict(record),
            )
        )

    def list_preview_lifecycle_plan_records(
        self,
        *,
        context_name: str = "",
        limit: int | None = None,
    ) -> tuple[PreviewLifecyclePlanRecord, ...]:
        filters: list[object] = []
        if context_name:
            filters.append(LaunchplanePreviewLifecyclePlanRow.context == context_name)
        return self._list_models(
            model_type=PreviewLifecyclePlanRecord,
            orm_model=LaunchplanePreviewLifecyclePlanRow,
            filters=filters,
            order_by=(
                LaunchplanePreviewLifecyclePlanRow.planned_at.desc(),
                LaunchplanePreviewLifecyclePlanRow.plan_id.desc(),
            ),
            limit=limit,
        )

    def read_preview_summary(
        self,
        *,
        preview_id: str,
        generation_limit: int | None = 10,
    ) -> LaunchplanePreviewSummary:
        preview = self.read_preview_record(preview_id)
        recent_generations = self.list_preview_generation_records(
            preview_id=preview_id,
            limit=generation_limit,
        )
        return LaunchplanePreviewSummary(
            preview=preview,
            latest_generation=next(iter(recent_generations), None),
            recent_generations=recent_generations,
        )

    def list_preview_summaries(
        self,
        *,
        context_name: str = "",
        anchor_repo: str = "",
        anchor_pr_number: int | None = None,
        preview_limit: int | None = None,
        generation_limit: int | None = 1,
    ) -> tuple[LaunchplanePreviewSummary, ...]:
        previews = self.list_preview_records(
            context_name=context_name,
            anchor_repo=anchor_repo,
            anchor_pr_number=anchor_pr_number,
            limit=preview_limit,
        )
        return tuple(
            self.read_preview_summary(
                preview_id=preview.preview_id,
                generation_limit=generation_limit,
            )
            for preview in previews
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

    def read_release_tuple_record(
        self, *, context_name: str, channel_name: str
    ) -> ReleaseTupleRecord:
        return self._read_model(
            model_type=ReleaseTupleRecord,
            orm_model=LaunchplaneReleaseTupleRow,
            filters=(
                LaunchplaneReleaseTupleRow.context == context_name,
                LaunchplaneReleaseTupleRow.channel == channel_name,
            ),
        )

    def list_release_tuple_records(self) -> tuple[ReleaseTupleRecord, ...]:
        return self._list_models(
            model_type=ReleaseTupleRecord,
            orm_model=LaunchplaneReleaseTupleRow,
            order_by=(
                LaunchplaneReleaseTupleRow.context.asc(),
                LaunchplaneReleaseTupleRow.channel.asc(),
            ),
        )

    def write_dokploy_target_id_record(self, record: DokployTargetIdRecord) -> None:
        self._write_row(
            LaunchplaneDokployTargetIdRow(
                context=record.context,
                instance=record.instance,
                target_id=record.target_id,
                updated_at=record.updated_at,
                payload=self._payload_dict(record),
            )
        )

    def read_dokploy_target_id_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetIdRecord:
        return self._read_model(
            model_type=DokployTargetIdRecord,
            orm_model=LaunchplaneDokployTargetIdRow,
            filters=(
                LaunchplaneDokployTargetIdRow.context == context_name,
                LaunchplaneDokployTargetIdRow.instance == instance_name,
            ),
        )

    def list_dokploy_target_id_records(self) -> tuple[DokployTargetIdRecord, ...]:
        return self._list_models(
            model_type=DokployTargetIdRecord,
            orm_model=LaunchplaneDokployTargetIdRow,
            order_by=(
                LaunchplaneDokployTargetIdRow.context.asc(),
                LaunchplaneDokployTargetIdRow.instance.asc(),
            ),
        )

    def write_dokploy_target_record(self, record: DokployTargetRecord) -> None:
        self._write_row(
            LaunchplaneDokployTargetRow(
                context=record.context,
                instance=record.instance,
                updated_at=record.updated_at,
                payload=self._payload_dict(record),
            )
        )

    def read_dokploy_target_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetRecord:
        return self._read_model(
            model_type=DokployTargetRecord,
            orm_model=LaunchplaneDokployTargetRow,
            filters=(
                LaunchplaneDokployTargetRow.context == context_name,
                LaunchplaneDokployTargetRow.instance == instance_name,
            ),
        )

    def list_dokploy_target_records(self) -> tuple[DokployTargetRecord, ...]:
        return self._list_models(
            model_type=DokployTargetRecord,
            orm_model=LaunchplaneDokployTargetRow,
            order_by=(
                LaunchplaneDokployTargetRow.context.asc(),
                LaunchplaneDokployTargetRow.instance.asc(),
            ),
        )

    def write_runtime_environment_record(self, record: RuntimeEnvironmentRecord) -> None:
        self._write_row(
            LaunchplaneRuntimeEnvironmentRow(
                scope=record.scope,
                context=record.context,
                instance=record.instance,
                updated_at=record.updated_at,
                payload=self._payload_dict(record),
            )
        )

    def list_runtime_environment_records(
        self,
        *,
        scope: str = "",
        context_name: str = "",
        instance_name: str = "",
    ) -> tuple[RuntimeEnvironmentRecord, ...]:
        filters: list[object] = []
        if scope:
            filters.append(LaunchplaneRuntimeEnvironmentRow.scope == scope)
        if context_name:
            filters.append(LaunchplaneRuntimeEnvironmentRow.context == context_name)
        if instance_name:
            filters.append(LaunchplaneRuntimeEnvironmentRow.instance == instance_name)
        return self._list_models(
            model_type=RuntimeEnvironmentRecord,
            orm_model=LaunchplaneRuntimeEnvironmentRow,
            filters=filters,
            order_by=(
                LaunchplaneRuntimeEnvironmentRow.scope.asc(),
                LaunchplaneRuntimeEnvironmentRow.context.asc(),
                LaunchplaneRuntimeEnvironmentRow.instance.asc(),
            ),
        )

    def read_lane_summary(self, *, context_name: str, instance_name: str) -> LaunchplaneLaneSummary:
        runtime_environment_records = (
            *self.list_runtime_environment_records(scope="global"),
            *self.list_runtime_environment_records(scope="context", context_name=context_name),
            *self.list_runtime_environment_records(
                scope="instance",
                context_name=context_name,
                instance_name=instance_name,
            ),
        )
        return LaunchplaneLaneSummary(
            context=context_name,
            instance=instance_name,
            inventory=self._read_optional_model(
                model_type=EnvironmentInventory,
                orm_model=LaunchplaneInventoryRow,
                filters=(
                    LaunchplaneInventoryRow.context == context_name,
                    LaunchplaneInventoryRow.instance == instance_name,
                ),
            ),
            release_tuple=self._read_optional_model(
                model_type=ReleaseTupleRecord,
                orm_model=LaunchplaneReleaseTupleRow,
                filters=(
                    LaunchplaneReleaseTupleRow.context == context_name,
                    LaunchplaneReleaseTupleRow.channel == instance_name,
                ),
            ),
            latest_deployment=next(
                iter(
                    self.list_deployment_records(
                        context_name=context_name,
                        instance_name=instance_name,
                        limit=1,
                    )
                ),
                None,
            ),
            latest_promotion=next(
                iter(
                    self.list_promotion_records(
                        context_name=context_name,
                        to_instance_name=instance_name,
                        limit=1,
                    )
                ),
                None,
            ),
            latest_backup_gate=next(
                iter(
                    self.list_backup_gate_records(
                        context_name=context_name,
                        instance_name=instance_name,
                        limit=1,
                    )
                ),
                None,
            ),
            dokploy_target_id=self._read_optional_model(
                model_type=DokployTargetIdRecord,
                orm_model=LaunchplaneDokployTargetIdRow,
                filters=(
                    LaunchplaneDokployTargetIdRow.context == context_name,
                    LaunchplaneDokployTargetIdRow.instance == instance_name,
                ),
            ),
            dokploy_target=self._read_optional_model(
                model_type=DokployTargetRecord,
                orm_model=LaunchplaneDokployTargetRow,
                filters=(
                    LaunchplaneDokployTargetRow.context == context_name,
                    LaunchplaneDokployTargetRow.instance == instance_name,
                ),
            ),
            runtime_environment_records=runtime_environment_records,
            odoo_instance_override=self._read_optional_model(
                model_type=OdooInstanceOverrideRecord,
                orm_model=LaunchplaneOdooInstanceOverrideRow,
                filters=(
                    LaunchplaneOdooInstanceOverrideRow.context == context_name,
                    LaunchplaneOdooInstanceOverrideRow.instance == instance_name,
                ),
            ),
            secret_bindings=self.list_secret_bindings(
                context_name=context_name,
                instance_name=instance_name,
            ),
        )

    def write_odoo_instance_override_record(self, record: OdooInstanceOverrideRecord) -> None:
        self._write_row(
            LaunchplaneOdooInstanceOverrideRow(
                context=record.context,
                instance=record.instance,
                updated_at=record.updated_at,
                payload=self._payload_dict(record),
            )
        )

    def read_odoo_instance_override_record(
        self, *, context_name: str, instance_name: str
    ) -> OdooInstanceOverrideRecord:
        return self._read_model(
            model_type=OdooInstanceOverrideRecord,
            orm_model=LaunchplaneOdooInstanceOverrideRow,
            filters=(
                LaunchplaneOdooInstanceOverrideRow.context == context_name,
                LaunchplaneOdooInstanceOverrideRow.instance == instance_name,
            ),
        )

    def list_odoo_instance_override_records(self) -> tuple[OdooInstanceOverrideRecord, ...]:
        return self._list_models(
            model_type=OdooInstanceOverrideRecord,
            orm_model=LaunchplaneOdooInstanceOverrideRow,
            order_by=(
                LaunchplaneOdooInstanceOverrideRow.context.asc(),
                LaunchplaneOdooInstanceOverrideRow.instance.asc(),
            ),
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
            order_by=(
                LaunchplaneSecretRow.updated_at.desc(),
                LaunchplaneSecretRow.secret_id.desc(),
            ),
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
            order_by=(
                LaunchplaneSecretRow.updated_at.desc(),
                LaunchplaneSecretRow.secret_id.desc(),
            ),
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
            order_by=(
                LaunchplaneSecretVersionRow.created_at.desc(),
                LaunchplaneSecretVersionRow.version_id.desc(),
            ),
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
            order_by=(
                LaunchplaneSecretBindingRow.updated_at.desc(),
                LaunchplaneSecretBindingRow.binding_id.desc(),
            ),
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
            order_by=(
                LaunchplaneSecretAuditEventRow.recorded_at.desc(),
                LaunchplaneSecretAuditEventRow.event_id.desc(),
            ),
        )

    def import_core_records_from_filesystem(
        self, filesystem_store: FilesystemRecordStore
    ) -> dict[str, int]:
        counts = {
            "artifacts": 0,
            "backup_gates": 0,
            "deployments": 0,
            "promotions": 0,
            "inventory": 0,
            "odoo_instance_overrides": 0,
            "preview_records": 0,
            "preview_generations": 0,
            "preview_inventory_scans": 0,
            "preview_lifecycle_plans": 0,
            "release_tuples": 0,
        }
        for record in filesystem_store.list_artifact_manifests():
            self.write_artifact_manifest(record)
            counts["artifacts"] += 1
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
        for record in filesystem_store.list_odoo_instance_override_records():
            self.write_odoo_instance_override_record(record)
            counts["odoo_instance_overrides"] += 1
        for record in filesystem_store.list_preview_records():
            self.write_preview_record(record)
            counts["preview_records"] += 1
        for record in filesystem_store.list_preview_generation_records():
            self.write_preview_generation_record(record)
            counts["preview_generations"] += 1
        if hasattr(filesystem_store, "list_preview_inventory_scan_records"):
            for record in filesystem_store.list_preview_inventory_scan_records():
                self.write_preview_inventory_scan_record(record)
                counts["preview_inventory_scans"] += 1
        if hasattr(filesystem_store, "list_preview_lifecycle_plan_records"):
            for record in filesystem_store.list_preview_lifecycle_plan_records():
                self.write_preview_lifecycle_plan_record(record)
                counts["preview_lifecycle_plans"] += 1
        for record in filesystem_store.list_release_tuple_records():
            self.write_release_tuple_record(record)
            counts["release_tuples"] += 1
        return counts
