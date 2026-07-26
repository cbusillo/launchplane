from __future__ import annotations

from pathlib import Path
from typing import Literal, Protocol

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane import release_tuples as control_plane_release_tuples
from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    BackupGateEvidence,
    DeploymentEvidence,
    HealthcheckEvidence,
    PostDeployUpdateEvidence,
    PromotionRecord,
)
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.contracts.odoo_stable_target_replacement import (
    OdooStableTargetReplacementApplyRequest,
)
from control_plane.workflows.odoo_stable_target_replacement import (
    OdooStableTargetReplacementStore,
    execute_odoo_stable_target_replacement_apply,
)
from control_plane.workflows.odoo_prod_backup_gate import BACKUP_GATE_SOURCE
from control_plane.workflows.ship import utc_now_timestamp
from control_plane.workflows.inventory import build_environment_inventory


class OdooProdPromotionStore(OdooStableTargetReplacementStore, Protocol):
    def read_artifact_manifest(self, artifact_id: str) -> ArtifactIdentityManifest: ...

    def read_backup_gate_record(self, record_id: str) -> BackupGateRecord: ...

    def read_deployment_record(self, record_id: str) -> DeploymentRecord: ...

    def read_release_tuple_record(
        self, *, context_name: str, channel_name: str
    ) -> ReleaseTupleRecord: ...

    def write_environment_inventory(self, record: EnvironmentInventory) -> object: ...

    def write_promotion_record(self, record: PromotionRecord) -> object: ...

    def write_release_tuple_record(self, record: ReleaseTupleRecord) -> object: ...


class OdooProdPromotionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str
    from_instance: str = "testing"
    to_instance: str = "prod"
    product: str = ""
    artifact_id: str
    backup_record_id: str
    source_git_ref: str = ""
    wait: bool = True
    timeout_seconds: int | None = Field(default=None, ge=1)
    verify_health: bool = True
    health_timeout_seconds: int | None = Field(default=None, ge=1)
    no_cache: bool = False

    @model_validator(mode="after")
    def _validate_request(self) -> "OdooProdPromotionRequest":
        self.context = self.context.strip().lower()
        self.from_instance = self.from_instance.strip().lower()
        self.to_instance = self.to_instance.strip().lower()
        self.product = self.product.strip()
        self.artifact_id = self.artifact_id.strip()
        self.backup_record_id = self.backup_record_id.strip()
        self.source_git_ref = self.source_git_ref.strip()
        if not self.context:
            raise ValueError("Odoo prod promotion requires context.")
        if self.from_instance != "testing" or self.to_instance != "prod":
            raise ValueError("Odoo prod promotion requires testing -> prod.")
        if not self.artifact_id:
            raise ValueError("Odoo prod promotion requires artifact_id.")
        if not self.backup_record_id:
            raise ValueError("Odoo prod promotion requires backup_record_id.")
        if not self.wait:
            raise ValueError(
                "Odoo prod promotion requires wait=true so Launchplane can verify the stable lane."
            )
        if self.verify_health and not self.wait:
            raise ValueError("Odoo prod promotion health verification requires wait=true.")
        return self


class OdooProdPromotionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: str
    from_instance: str
    to_instance: str
    artifact_id: str
    backup_record_id: str
    promotion_record_id: str = ""
    deployment_record_id: str = ""
    release_tuple_id: str = ""
    promotion_status: Literal["pass", "fail"]
    deployment_status: Literal["pending", "pass", "fail", "skipped"] = "skipped"
    post_deploy_status: Literal["pending", "pass", "fail", "skipped"] = "skipped"
    destination_health_status: Literal["pending", "pass", "fail", "skipped"] = "skipped"
    error_message: str = ""


def execute_odoo_prod_promotion(
    *,
    control_plane_root: Path,
    state_dir: Path,
    database_url: str | None,
    record_store: OdooProdPromotionStore,
    request: OdooProdPromotionRequest,
) -> OdooProdPromotionResult:
    del state_dir, database_url
    product = _resolve_product_profile_key(product=request.product, context=request.context)
    promotion_record_id = _promotion_record_id(
        context=request.context,
        from_instance=request.from_instance,
        to_instance=request.to_instance,
    )
    source_tuple: ReleaseTupleRecord | None = None
    try:
        artifact_manifest = record_store.read_artifact_manifest(request.artifact_id)
        source_tuple = _read_source_tuple(record_store=record_store, request=request)
        backup_gate = record_store.read_backup_gate_record(request.backup_record_id)
        if (
            backup_gate.source != BACKUP_GATE_SOURCE
            or not backup_gate.required
            or backup_gate.status != "pass"
            or backup_gate.context != request.context
            or backup_gate.instance != request.to_instance
        ):
            raise click.ClickException(
                "Odoo prod promotion requires the exact passing ordinary production backup gate."
            )
        pending_record = _build_promotion_record(
            record_id=promotion_record_id,
            request=request,
            deployment_record=None,
            deployment_status="pending",
            backup_gate=backup_gate,
        )
        record_store.write_promotion_record(pending_record)

        replacement_result = execute_odoo_stable_target_replacement_apply(
            control_plane_root=control_plane_root,
            record_store=record_store,
            request=OdooStableTargetReplacementApplyRequest(
                product=product,
                instance=request.to_instance,
                artifact_id=request.artifact_id,
                source_git_ref=request.source_git_ref or artifact_manifest.source_commit,
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
        deployment_record = _read_deployment_record_if_present(
            record_store=record_store,
            deployment_record_id=replacement_result.deployment_record_id,
        )
        deploy_status: Literal["pass", "fail"] = replacement_result.deploy_status
        final_record = _build_promotion_record(
            record_id=promotion_record_id,
            request=request,
            deployment_record=deployment_record,
            deployment_status=deploy_status,
            backup_gate=backup_gate,
            post_deploy_status=replacement_result.post_deploy_status,
            destination_health_status=replacement_result.health_status,
        )
        record_store.write_promotion_record(final_record)
        release_tuple_id = ""
        if deploy_status == "pass" and deployment_record is not None:
            record_store.write_environment_inventory(
                build_environment_inventory(
                    deployment_record=deployment_record,
                    updated_at=utc_now_timestamp(),
                    promotion_record_id=final_record.record_id,
                    promoted_from_instance=final_record.from_instance,
                )
            )
            release_tuple_id = _write_promoted_release_tuple(
                record_store=record_store,
                source_tuple=source_tuple,
                deployment_record=deployment_record,
                promotion_record=final_record,
            )
        return _result_from_record(
            record=final_record,
            release_tuple_id=release_tuple_id,
            error_message=replacement_result.error_message,
        )
    except click.ClickException as error:
        failed_record = _build_promotion_record(
            record_id=promotion_record_id,
            request=request,
            deployment_record=None,
            deployment_status="fail",
        )
        record_store.write_promotion_record(failed_record)
        return _result_from_record(
            record=failed_record,
            release_tuple_id="",
            error_message=str(error),
        )


def _promotion_record_id(*, context: str, from_instance: str, to_instance: str) -> str:
    from control_plane.workflows.promote import generate_promotion_record_id

    return generate_promotion_record_id(
        context_name=context,
        from_instance_name=from_instance,
        to_instance_name=to_instance,
    )


def _resolve_product_profile_key(*, product: str, context: str) -> str:
    del context
    normalized_product = product.strip()
    if not normalized_product or normalized_product == "odoo":
        raise ValueError("Odoo prod promotion requires a product-specific DB-backed profile key.")
    return normalized_product


def _read_source_tuple(
    *, record_store: OdooProdPromotionStore, request: OdooProdPromotionRequest
) -> ReleaseTupleRecord | None:
    if not control_plane_release_tuples.should_mint_release_tuple_for_channel(
        request.from_instance
    ):
        return None
    source_tuple = record_store.read_release_tuple_record(
        context_name=request.context,
        channel_name=request.from_instance,
    )
    return control_plane_release_tuples.require_source_release_tuple_for_promotion(
        source_tuple=source_tuple,
        artifact_id=request.artifact_id,
        context_name=request.context,
        from_channel_name=request.from_instance,
    )


def _read_deployment_record_if_present(
    *, record_store: OdooProdPromotionStore, deployment_record_id: str
) -> DeploymentRecord | None:
    if not deployment_record_id.strip():
        return None
    return record_store.read_deployment_record(deployment_record_id)


def _backup_gate_evidence(backup_gate: BackupGateRecord | None) -> BackupGateEvidence:
    if backup_gate is None:
        return BackupGateEvidence(status="pending")
    return BackupGateEvidence(
        required=backup_gate.required,
        status=backup_gate.status,
        evidence=dict(backup_gate.evidence),
    )


def _build_promotion_record(
    *,
    record_id: str,
    request: OdooProdPromotionRequest,
    deployment_record: DeploymentRecord | None,
    deployment_status: Literal["pending", "pass", "fail"],
    backup_gate: BackupGateRecord | None = None,
    post_deploy_status: Literal["pending", "pass", "fail", "skipped"] = "skipped",
    destination_health_status: Literal["pending", "pass", "fail", "skipped"] = "skipped",
) -> PromotionRecord:
    deployment_id = "control-plane-dokploy"
    target_name = f"{request.context}-{request.to_instance}"
    if deployment_record is not None:
        deployment_id = deployment_record.deploy.deployment_id
        target_name = deployment_record.deploy.target_name
        post_deploy_status = deployment_record.post_deploy_update.status
        destination_health_status = deployment_record.destination_health.status
    return PromotionRecord(
        record_id=record_id,
        artifact_identity=ArtifactIdentityReference(artifact_id=request.artifact_id),
        deployment_record_id=deployment_record.record_id if deployment_record is not None else "",
        backup_record_id=request.backup_record_id,
        context=request.context,
        from_instance=request.from_instance,
        to_instance=request.to_instance,
        backup_gate=_backup_gate_evidence(backup_gate),
        deploy=DeploymentEvidence(
            target_name=target_name,
            target_type="compose",
            deploy_mode="dokploy-compose-api",
            deployment_id=deployment_id,
            status=deployment_status,
        ),
        post_deploy_update=PostDeployUpdateEvidence(
            attempted=post_deploy_status != "skipped",
            status=post_deploy_status,
            detail="Odoo stable target replacement post-deploy maintenance completed."
            if post_deploy_status == "pass"
            else "Odoo stable target replacement post-deploy maintenance did not pass.",
        ),
        destination_health=HealthcheckEvidence(
            verified=destination_health_status == "pass",
            urls=deployment_record.destination_health.urls if deployment_record is not None else (),
            timeout_seconds=deployment_record.destination_health.timeout_seconds
            if deployment_record is not None
            else None,
            status=destination_health_status,
            runtime_identity_status=deployment_record.destination_health.runtime_identity_status
            if deployment_record is not None
            else "unchecked",
            runtime_identity_detail=deployment_record.destination_health.runtime_identity_detail
            if deployment_record is not None
            else "",
            observed_runtime_identity=deployment_record.destination_health.observed_runtime_identity
            if deployment_record is not None
            else None,
        ),
    )


def _write_promoted_release_tuple(
    *,
    record_store: OdooProdPromotionStore,
    source_tuple: ReleaseTupleRecord | None,
    deployment_record: DeploymentRecord,
    promotion_record: PromotionRecord,
) -> str:
    if source_tuple is None:
        return ""
    if not control_plane_release_tuples.should_mint_release_tuple_for_channel(
        promotion_record.to_instance
    ):
        return ""
    promoted_tuple = control_plane_release_tuples.build_promoted_release_tuple_record(
        source_tuple=source_tuple,
        to_channel_name=promotion_record.to_instance,
        deployment_record_id=deployment_record.record_id,
        promotion_record_id=promotion_record.record_id,
        minted_at=utc_now_timestamp(),
    )
    record_store.write_release_tuple_record(promoted_tuple)
    return promoted_tuple.tuple_id


def _result_from_record(
    *, record: PromotionRecord, release_tuple_id: str, error_message: str
) -> OdooProdPromotionResult:
    deployment_status = record.deploy.status
    return OdooProdPromotionResult(
        context=record.context,
        from_instance=record.from_instance,
        to_instance=record.to_instance,
        artifact_id=record.artifact_identity.artifact_id,
        backup_record_id=record.backup_record_id,
        promotion_record_id=record.record_id,
        deployment_record_id=record.deployment_record_id,
        release_tuple_id=release_tuple_id,
        promotion_status="pass" if deployment_status == "pass" else "fail",
        deployment_status=deployment_status,
        post_deploy_status=record.post_deploy_update.status,
        destination_health_status=record.destination_health.status,
        error_message=error_message,
    )
