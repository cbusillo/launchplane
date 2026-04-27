from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Literal

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.promotion_record import PromotionRecord
from control_plane.storage.filesystem import FilesystemRecordStore

SUPPORTED_ODOO_CONTEXTS = {"cm", "opw"}


class OdooProdPromotionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str
    from_instance: str = "testing"
    to_instance: str = "prod"
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
        self.artifact_id = self.artifact_id.strip()
        self.backup_record_id = self.backup_record_id.strip()
        self.source_git_ref = self.source_git_ref.strip()
        if self.context not in SUPPORTED_ODOO_CONTEXTS:
            supported = ", ".join(sorted(SUPPORTED_ODOO_CONTEXTS))
            raise ValueError(
                f"Odoo prod promotion supports contexts {supported}; got {self.context!r}."
            )
        if self.from_instance != "testing" or self.to_instance != "prod":
            raise ValueError("Odoo prod promotion requires testing -> prod.")
        if not self.artifact_id:
            raise ValueError("Odoo prod promotion requires artifact_id.")
        if not self.backup_record_id:
            raise ValueError("Odoo prod promotion requires backup_record_id.")
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
    record_store: FilesystemRecordStore,
    request: OdooProdPromotionRequest,
) -> OdooProdPromotionResult:
    del control_plane_root
    from control_plane import cli as control_plane_cli
    from control_plane import release_tuples as control_plane_release_tuples
    from control_plane.workflows.promote import (
        build_executed_promotion_record,
        generate_promotion_record_id,
    )

    promotion_request = control_plane_cli._resolve_native_promotion_request(
        context_name=request.context,
        from_instance_name=request.from_instance,
        to_instance_name=request.to_instance,
        artifact_id=request.artifact_id,
        backup_record_id=request.backup_record_id,
        source_git_ref=request.source_git_ref,
        wait=request.wait,
        timeout_override_seconds=request.timeout_seconds,
        verify_health=request.verify_health,
        health_timeout_override_seconds=request.health_timeout_seconds,
        dry_run=False,
        no_cache=request.no_cache,
        allow_dirty=False,
    )
    resolved_artifact_id = control_plane_cli._require_artifact_id(
        requested_artifact_id=promotion_request.artifact_id
    )
    control_plane_cli._read_artifact_manifest(
        record_store=record_store,
        artifact_id=resolved_artifact_id,
    )
    normalized_request = promotion_request.model_copy(update={"artifact_id": resolved_artifact_id})
    resolved_request, _backup_gate_record = control_plane_cli._resolve_backup_gate_for_promotion(
        request=normalized_request,
        record_store=record_store,
    )
    source_release_tuple = control_plane_cli._read_source_release_tuple_for_promotion(
        record_store=record_store,
        request=resolved_request,
    )
    record_id = generate_promotion_record_id(
        context_name=resolved_request.context,
        from_instance_name=resolved_request.from_instance,
        to_instance_name=resolved_request.to_instance,
    )
    pending_record = build_executed_promotion_record(
        request=resolved_request,
        record_id=record_id,
        deployment_id="",
        deployment_status="pending",
    )
    record_store.write_promotion_record(pending_record)

    try:
        ship_request = control_plane_cli._resolve_ship_request_for_promotion(
            request=resolved_request
        )
        _record_path, deployment_record = control_plane_cli._execute_ship(
            state_dir=state_dir,
            database_url=database_url,
            env_file=None,
            request=ship_request,
            mint_release_tuple=False,
        )
        if not isinstance(deployment_record, DeploymentRecord):
            raise click.ClickException(
                "Ship execution returned an unexpected non-record payload during Odoo prod promotion."
            )
        final_record = build_executed_promotion_record(
            request=resolved_request,
            record_id=record_id,
            deployment_record_id=deployment_record.record_id,
            deployment_id=deployment_record.deploy.deployment_id,
            deployment_status=deployment_record.deploy.status,
        )
    except (click.ClickException, json.JSONDecodeError, subprocess.CalledProcessError) as error:
        final_record = build_executed_promotion_record(
            request=resolved_request,
            record_id=record_id,
            deployment_id="control-plane-dokploy",
            deployment_status="fail",
        )
        record_store.write_promotion_record(final_record)
        return _result_from_record(
            record=final_record,
            deployment_record=None,
            release_tuple_id="",
            error_message=str(error),
        )

    record_store.write_promotion_record(final_record)
    release_tuple_id = ""
    if deployment_record.wait_for_completion and deployment_record.deploy.status == "pass":
        control_plane_cli._write_environment_inventory(
            record_store=record_store,
            deployment_record=deployment_record,
            promotion_record_id=final_record.record_id,
            promoted_from_instance=final_record.from_instance,
        )
        control_plane_cli._write_promoted_release_tuple(
            record_store=record_store,
            source_tuple=source_release_tuple,
            deployment_record=deployment_record,
            promotion_record=final_record,
        )
        if (
            source_release_tuple is not None
            and control_plane_release_tuples.should_mint_release_tuple_for_channel(
                final_record.to_instance
            )
        ):
            release_tuple_id = f"{final_record.context}-{final_record.to_instance}-{final_record.artifact_identity.artifact_id}"
    return _result_from_record(
        record=final_record,
        deployment_record=deployment_record,
        release_tuple_id=release_tuple_id,
        error_message="",
    )


def _result_from_record(
    *,
    record: PromotionRecord,
    deployment_record: DeploymentRecord | None,
    release_tuple_id: str,
    error_message: str,
) -> OdooProdPromotionResult:
    deployment_status = record.deploy.status
    return OdooProdPromotionResult(
        context=record.context,
        from_instance=record.from_instance,
        to_instance=record.to_instance,
        artifact_id=record.artifact_identity.artifact_id,
        backup_record_id=record.backup_record_id,
        promotion_record_id=record.record_id,
        deployment_record_id=deployment_record.record_id if deployment_record is not None else "",
        release_tuple_id=release_tuple_id,
        promotion_status="pass" if deployment_status == "pass" else "fail",
        deployment_status=deployment_status,
        post_deploy_status=record.post_deploy_update.status,
        destination_health_status=record.destination_health.status,
        error_message=error_message,
    )
