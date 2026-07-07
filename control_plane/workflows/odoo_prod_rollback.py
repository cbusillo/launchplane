from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.promotion_record import (
    HealthcheckEvidence,
    PromotionRecord,
    ReleaseStatus,
    RollbackExecutionEvidence,
)
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.contracts.odoo_stable_target_replacement import (
    OdooStableTargetReplacementApplyRequest,
)
from control_plane.workflows.inventory import build_environment_inventory
from control_plane.workflows.odoo_stable_target_replacement import (
    OdooStableTargetReplacementStore,
    execute_odoo_stable_target_replacement_apply,
)
from control_plane.workflows.ship import utc_now_timestamp


@dataclass(frozen=True)
class _RollbackSource:
    artifact_id: str
    source_git_ref: str
    result_source_channel: str
    promoted_from_instance: str
    snapshot_name: str
    detail: str


class OdooProdRollbackStore(OdooStableTargetReplacementStore, Protocol):
    def read_release_tuple_record(
        self, *, context_name: str, channel_name: str
    ) -> ReleaseTupleRecord: ...

    def read_artifact_manifest(self, artifact_id: str) -> ArtifactIdentityManifest: ...

    def read_environment_inventory(
        self, *, context_name: str, instance_name: str
    ) -> EnvironmentInventory: ...

    def read_promotion_record(self, promotion_record_id: str) -> PromotionRecord: ...

    def write_deployment_record(self, record: DeploymentRecord) -> None: ...

    def read_deployment_record(self, record_id: str) -> DeploymentRecord: ...

    def write_environment_inventory(self, inventory: EnvironmentInventory) -> None: ...

    def write_release_tuple_record(self, record: ReleaseTupleRecord) -> None: ...

    def write_promotion_record(self, record: PromotionRecord) -> None: ...


def _require_record_store(record_store: object) -> OdooProdRollbackStore:
    required_methods = (
        "read_release_tuple_record",
        "read_artifact_manifest",
        "read_environment_inventory",
        "read_promotion_record",
        "read_product_profile_record",
        "read_dokploy_target_record",
        "read_dokploy_target_id_record",
        "write_deployment_record",
        "read_deployment_record",
        "write_environment_inventory",
        "write_release_tuple_record",
        "write_promotion_record",
    )
    missing_methods = tuple(
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    )
    if missing_methods:
        raise click.ClickException(
            "Odoo prod rollback requires a DB-backed Launchplane record store. "
            f"Missing methods: {', '.join(missing_methods)}."
        )
    return cast(OdooProdRollbackStore, record_store)


class OdooProdRollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str
    instance: str = "prod"
    source_channel: Literal["testing"] = "testing"
    promotion_record_id: str = ""
    artifact_id: str = ""
    reason: str = ""
    wait: bool = True
    timeout_seconds: int | None = Field(default=None, ge=1)
    verify_health: bool = True
    health_timeout_seconds: int | None = Field(default=None, ge=1)
    no_cache: bool = False

    @model_validator(mode="after")
    def _validate_request(self) -> "OdooProdRollbackRequest":
        self.context = self.context.strip().lower()
        self.instance = self.instance.strip().lower()
        self.promotion_record_id = self.promotion_record_id.strip()
        self.artifact_id = self.artifact_id.strip()
        self.reason = self.reason.strip()
        if not self.context:
            raise ValueError("Odoo prod rollback requires context.")
        if self.instance != "prod":
            raise ValueError("Odoo prod rollback requires instance 'prod'.")
        if self.verify_health and not self.wait:
            raise ValueError("Odoo prod rollback health verification requires wait=true.")
        return self


class OdooProdRollbackResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: str
    instance: str
    source_channel: str
    artifact_id: str
    promotion_record_id: str
    deployment_record_id: str = ""
    release_tuple_id: str = ""
    rollback_status: Literal["pass", "fail"]
    rollback_health_status: Literal["pass", "fail", "skipped"] = "skipped"
    rollback_started_at: str = ""
    rollback_finished_at: str = ""
    post_deploy_status: Literal["pass", "fail", "skipped"] = "skipped"
    error_message: str = ""


def _read_source_release_tuple(
    *,
    record_store: OdooProdRollbackStore,
    request: OdooProdRollbackRequest,
) -> ReleaseTupleRecord:
    try:
        return record_store.read_release_tuple_record(
            context_name=request.context,
            channel_name=request.source_channel,
        )
    except FileNotFoundError as exc:
        raise click.ClickException(
            f"Odoo prod rollback requires a DB-backed {request.context}/{request.source_channel} release tuple."
        ) from exc


def _read_artifact_manifest(
    *,
    record_store: OdooProdRollbackStore,
    artifact_id: str,
) -> ArtifactIdentityManifest:
    try:
        return record_store.read_artifact_manifest(artifact_id)
    except FileNotFoundError as exc:
        raise click.ClickException(
            f"Odoo prod rollback requires artifact manifest {artifact_id!r} in Launchplane records."
        ) from exc


def _resolve_rollback_source(
    *,
    request: OdooProdRollbackRequest,
    artifact_manifest: ArtifactIdentityManifest,
    source_tuple: ReleaseTupleRecord | None,
) -> _RollbackSource:
    if source_tuple is not None:
        source_git_ref = source_tuple.repo_shas.get(
            f"tenant-{request.context}", artifact_manifest.source_commit
        )
        return _RollbackSource(
            artifact_id=source_tuple.artifact_id,
            source_git_ref=source_git_ref,
            result_source_channel=request.source_channel,
            promoted_from_instance=request.source_channel,
            snapshot_name=f"release-tuple:{source_tuple.tuple_id}",
            detail=(
                f"Rolled {request.context}/{request.instance} back to "
                f"{request.source_channel} release tuple {source_tuple.tuple_id}."
            ),
        )
    return _RollbackSource(
        artifact_id=artifact_manifest.artifact_id,
        source_git_ref=artifact_manifest.source_commit,
        result_source_channel="artifact",
        promoted_from_instance="explicit-artifact",
        snapshot_name=f"artifact:{artifact_manifest.artifact_id}",
        detail=(
            f"Rolled {request.context}/{request.instance} back to explicit "
            f"Launchplane artifact {artifact_manifest.artifact_id}."
        ),
    )


def _read_prod_inventory(
    *,
    record_store: OdooProdRollbackStore,
    request: OdooProdRollbackRequest,
) -> EnvironmentInventory:
    try:
        return record_store.read_environment_inventory(
            context_name=request.context,
            instance_name=request.instance,
        )
    except FileNotFoundError as exc:
        raise click.ClickException(
            f"Odoo prod rollback requires current inventory for {request.context}/{request.instance}."
        ) from exc


def _read_promotion_record(
    *,
    record_store: OdooProdRollbackStore,
    promotion_record_id: str,
) -> PromotionRecord:
    try:
        return record_store.read_promotion_record(promotion_record_id)
    except FileNotFoundError as exc:
        raise click.ClickException(
            f"Odoo prod rollback requires promotion record {promotion_record_id!r}."
        ) from exc


def _resolve_promotion_record(
    *,
    record_store: OdooProdRollbackStore,
    request: OdooProdRollbackRequest,
) -> PromotionRecord:
    promotion_record_id = request.promotion_record_id
    if not promotion_record_id:
        promotion_record_id = _read_prod_inventory(
            record_store=record_store,
            request=request,
        ).promotion_record_id.strip()
    if not promotion_record_id:
        raise click.ClickException(
            f"Odoo prod rollback could not resolve a current promotion record for {request.context}/{request.instance}."
        )
    promotion_record = _read_promotion_record(
        record_store=record_store,
        promotion_record_id=promotion_record_id,
    )
    if (
        promotion_record.context != request.context
        or promotion_record.to_instance != request.instance
    ):
        raise click.ClickException(
            "Odoo prod rollback promotion record does not match the requested prod lane. "
            f"Record={promotion_record.context}/{promotion_record.to_instance} "
            f"request={request.context}/{request.instance}."
        )
    return promotion_record


def _write_rollback_state(
    *,
    record_store: OdooProdRollbackStore,
    promotion_record: PromotionRecord,
    snapshot_name: str,
    status: ReleaseStatus,
    health_status: ReleaseStatus,
    started_at: str,
    finished_at: str,
    detail: str,
) -> PromotionRecord:
    updated_record = promotion_record.model_copy(
        update={
            "rollback": RollbackExecutionEvidence(
                attempted=True,
                status=status,
                detail=detail,
                snapshot_name=snapshot_name,
                started_at=started_at,
                finished_at=finished_at,
            ),
            "rollback_health": HealthcheckEvidence(
                verified=False,
                status=health_status,
            ),
        }
    )
    record_store.write_promotion_record(updated_record)
    return updated_record


def execute_odoo_prod_rollback(
    *,
    control_plane_root: Path,
    record_store: object,
    request: OdooProdRollbackRequest,
) -> OdooProdRollbackResult:
    typed_record_store = _require_record_store(record_store)
    source_tuple: ReleaseTupleRecord | None = None
    if request.artifact_id:
        artifact_manifest = _read_artifact_manifest(
            record_store=typed_record_store,
            artifact_id=request.artifact_id,
        )
    else:
        source_tuple = _read_source_release_tuple(record_store=typed_record_store, request=request)
        artifact_manifest = _read_artifact_manifest(
            record_store=typed_record_store,
            artifact_id=source_tuple.artifact_id,
        )
    rollback_source = _resolve_rollback_source(
        request=request,
        artifact_manifest=artifact_manifest,
        source_tuple=source_tuple,
    )
    promotion_record = _resolve_promotion_record(record_store=typed_record_store, request=request)
    started_at = utc_now_timestamp()
    _write_rollback_state(
        record_store=typed_record_store,
        promotion_record=promotion_record,
        snapshot_name=rollback_source.snapshot_name,
        status="pending",
        health_status="skipped",
        started_at=started_at,
        finished_at="",
        detail="Odoo prod rollback deployment is pending.",
    )

    replacement_result = None
    health_status: Literal["pass", "fail", "skipped"] = (
        "fail" if request.verify_health else "skipped"
    )
    try:
        replacement_result = execute_odoo_stable_target_replacement_apply(
            control_plane_root=control_plane_root,
            record_store=typed_record_store,
            request=OdooStableTargetReplacementApplyRequest(
                product=f"odoo-tenant-{request.context}",
                instance=request.instance,
                artifact_id=rollback_source.artifact_id,
                source_git_ref=rollback_source.source_git_ref,
                allow_empty_data=True,
                data_source_mode="existing",
                verify_health=request.verify_health,
                verify_canonical=request.verify_health,
                verify_logo=request.verify_health,
                timeout_seconds=request.timeout_seconds,
                health_timeout_seconds=request.health_timeout_seconds,
                no_cache=request.no_cache,
            ),
        )
        deployment_record = typed_record_store.read_deployment_record(
            replacement_result.deployment_record_id
        )
        health_status = replacement_result.health_status
        if replacement_result.deploy_status != "pass":
            raise click.ClickException(
                replacement_result.error_message or "Odoo prod rollback deploy failed."
            )
        if replacement_result.post_deploy_status != "pass":
            raise click.ClickException(
                replacement_result.error_message or "Odoo prod rollback post-deploy failed."
            )
        if request.verify_health and replacement_result.health_status != "pass":
            raise click.ClickException(
                replacement_result.error_message or "Odoo prod rollback health verification failed."
            )
    except (click.ClickException, OSError) as error:
        finished_at = utc_now_timestamp()
        deployment_record_id = ""
        post_deploy_status: Literal["pass", "fail", "skipped"] = "skipped"
        if replacement_result is not None:
            deployment_record_id = replacement_result.deployment_record_id
            health_status = replacement_result.health_status
            post_deploy_status = replacement_result.post_deploy_status
        _write_rollback_state(
            record_store=typed_record_store,
            promotion_record=promotion_record,
            snapshot_name=rollback_source.snapshot_name,
            status="fail",
            health_status=health_status,
            started_at=started_at,
            finished_at=finished_at,
            detail=str(error),
        )
        return OdooProdRollbackResult(
            context=request.context,
            instance=request.instance,
            source_channel=rollback_source.result_source_channel,
            artifact_id=rollback_source.artifact_id,
            promotion_record_id=promotion_record.record_id,
            deployment_record_id=deployment_record_id,
            rollback_status="fail",
            rollback_health_status=health_status,
            rollback_started_at=started_at,
            rollback_finished_at=finished_at,
            post_deploy_status=post_deploy_status,
            error_message=str(error),
        )

    finished_at = utc_now_timestamp()
    typed_record_store.write_environment_inventory(
        build_environment_inventory(
            deployment_record=deployment_record,
            updated_at=finished_at,
            promotion_record_id=promotion_record.record_id,
            promoted_from_instance=rollback_source.promoted_from_instance,
        )
    )
    _write_rollback_state(
        record_store=typed_record_store,
        promotion_record=promotion_record,
        snapshot_name=rollback_source.snapshot_name,
        status="pass",
        health_status=health_status,
        started_at=started_at,
        finished_at=finished_at,
        detail=rollback_source.detail,
    )
    return OdooProdRollbackResult(
        context=request.context,
        instance=request.instance,
        source_channel=rollback_source.result_source_channel,
        artifact_id=rollback_source.artifact_id,
        promotion_record_id=promotion_record.record_id,
        deployment_record_id=replacement_result.deployment_record_id,
        release_tuple_id=replacement_result.release_tuple_id,
        rollback_status="pass",
        rollback_health_status=health_status,
        rollback_started_at=started_at,
        rollback_finished_at=finished_at,
        post_deploy_status=replacement_result.post_deploy_status,
    )
