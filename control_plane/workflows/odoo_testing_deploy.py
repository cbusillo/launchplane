from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Literal, Protocol

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane import release_tuples as control_plane_release_tuples
from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.promotion_record import HealthcheckEvidence
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.contracts.ship_request import ShipRequest


class OdooTestingDeployStore(Protocol):
    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord: ...

    def read_artifact_manifest(self, artifact_id: str) -> ArtifactIdentityManifest: ...

    def write_deployment_record(self, record: DeploymentRecord) -> Path | None: ...

    def write_environment_inventory(self, record: EnvironmentInventory) -> Path | None: ...

    def write_release_tuple_record(self, record: ReleaseTupleRecord) -> Path | None: ...


class OdooTestingDeployRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str
    instance: str = "testing"
    product: str = ""
    artifact_id: str
    source_git_ref: str
    wait: bool = True
    timeout_seconds: int | None = Field(default=None, ge=1)
    verify_health: bool = True
    health_timeout_seconds: int | None = Field(default=None, ge=1)
    no_cache: bool = False

    @model_validator(mode="after")
    def _validate_request(self) -> "OdooTestingDeployRequest":
        self.context = self.context.strip().lower()
        self.instance = self.instance.strip().lower()
        self.product = self.product.strip()
        self.artifact_id = self.artifact_id.strip()
        self.source_git_ref = self.source_git_ref.strip()
        if not self.context:
            raise ValueError("Odoo testing deploy requires context.")
        if self.instance != "testing":
            raise ValueError("Odoo testing deploy requires instance 'testing'.")
        if not self.artifact_id:
            raise ValueError("Odoo testing deploy requires artifact_id.")
        if not self.source_git_ref:
            raise ValueError("Odoo testing deploy requires source_git_ref.")
        if not self.wait:
            raise ValueError(
                "Odoo testing deploy requires wait=true so Launchplane can mint a release tuple."
            )
        return self


class OdooTestingDeployResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: str
    instance: str
    artifact_id: str
    deployment_record_id: str = ""
    release_tuple_id: str = ""
    deployment_status: Literal["pending", "pass", "fail", "skipped"] = "skipped"
    post_deploy_status: Literal["pending", "pass", "fail", "skipped"] = "skipped"
    destination_health_status: Literal["pending", "pass", "fail", "skipped"] = "skipped"
    error_message: str = ""


def _profile_health_url(
    *,
    record_store: OdooTestingDeployStore,
    product: str,
    context: str,
    instance: str,
) -> str:
    profile = record_store.read_product_profile_record(product.strip())
    for lane in profile.lanes:
        if lane.context == context and lane.instance == instance:
            return lane.health_url.strip()
    return ""


def _ship_request_with_profile_health_url(
    *,
    ship_request: ShipRequest,
    health_url: str,
) -> ShipRequest:
    normalized_health_url = health_url.strip()
    if not normalized_health_url:
        return ship_request
    destination_health = ship_request.destination_health
    if destination_health.urls:
        return ship_request
    timeout_seconds = destination_health.timeout_seconds or ship_request.health_timeout_seconds
    updated_health = HealthcheckEvidence(
        urls=(normalized_health_url,),
        timeout_seconds=timeout_seconds,
        status="pending",
    )
    return ship_request.model_copy(
        update={"verify_health": True, "destination_health": updated_health}
    )


def _resolve_ship_request_with_health_fallback(
    *,
    request: OdooTestingDeployRequest,
    record_store: OdooTestingDeployStore,
) -> ShipRequest:
    from control_plane import cli as control_plane_cli

    try:
        return control_plane_cli._resolve_native_ship_request(
            context_name=request.context,
            instance_name=request.instance,
            artifact_id=request.artifact_id,
            source_git_ref=request.source_git_ref,
            wait=request.wait,
            timeout_override_seconds=request.timeout_seconds,
            verify_health=request.verify_health,
            health_timeout_override_seconds=request.health_timeout_seconds,
            dry_run=False,
            no_cache=request.no_cache,
            allow_dirty=False,
        )
    except click.ClickException as error:
        if not _is_missing_target_health_url_error(error):
            raise
        health_url = _profile_health_url(
            record_store=record_store,
            product=request.product or f"odoo-tenant-{request.context}",
            context=request.context,
            instance=request.instance,
        )
        if not _should_retry_with_profile_health_url(error=error, health_url=health_url):
            raise
        fallback_request = control_plane_cli._resolve_native_ship_request(
            context_name=request.context,
            instance_name=request.instance,
            artifact_id=request.artifact_id,
            source_git_ref=request.source_git_ref,
            wait=request.wait,
            timeout_override_seconds=request.timeout_seconds,
            verify_health=False,
            health_timeout_override_seconds=request.health_timeout_seconds,
            dry_run=False,
            no_cache=request.no_cache,
            allow_dirty=False,
        )
        return _ship_request_with_profile_health_url(
            ship_request=fallback_request,
            health_url=health_url,
        )


def _should_retry_with_profile_health_url(*, error: click.ClickException, health_url: str) -> bool:
    if not health_url.strip():
        return False
    return _is_missing_target_health_url_error(error)


def _is_missing_target_health_url_error(error: click.ClickException) -> bool:
    return "no target domain/URL was resolved" in str(error)


def execute_odoo_testing_deploy(
    *,
    control_plane_root: Path,
    state_dir: Path,
    database_url: str | None,
    record_store: OdooTestingDeployStore,
    request: OdooTestingDeployRequest,
) -> OdooTestingDeployResult:
    del control_plane_root
    from control_plane import cli as control_plane_cli

    try:
        ship_request = _resolve_ship_request_with_health_fallback(
            request=request,
            record_store=record_store,
        )
        resolved_artifact_id = control_plane_cli._require_artifact_id(
            requested_artifact_id=ship_request.artifact_id
        )
        control_plane_cli._read_artifact_manifest(
            record_store=record_store,
            artifact_id=resolved_artifact_id,
        )
        normalized_request = ship_request.model_copy(update={"artifact_id": resolved_artifact_id})
        _record_path, deployment_record = control_plane_cli._execute_ship(
            state_dir=state_dir,
            database_url=database_url,
            env_file=None,
            request=normalized_request,
            mint_release_tuple=True,
        )
        if not isinstance(deployment_record, DeploymentRecord):
            raise click.ClickException(
                "Ship execution returned an unexpected non-record payload during Odoo testing deploy."
            )
    except (click.ClickException, json.JSONDecodeError, subprocess.CalledProcessError) as error:
        return OdooTestingDeployResult(
            context=request.context,
            instance=request.instance,
            artifact_id=request.artifact_id,
            deployment_status="fail",
            error_message=str(error),
        )

    release_tuple_id = ""
    if deployment_record.wait_for_completion and deployment_record.deploy.status == "pass":
        release_tuple_id = _release_tuple_id_for_deployment(deployment_record)
    return OdooTestingDeployResult(
        context=deployment_record.context,
        instance=deployment_record.instance,
        artifact_id=_deployment_artifact_id(deployment_record),
        deployment_record_id=deployment_record.record_id,
        release_tuple_id=release_tuple_id,
        deployment_status=deployment_record.deploy.status,
        post_deploy_status=deployment_record.post_deploy_update.status,
        destination_health_status=deployment_record.destination_health.status,
    )


def _release_tuple_id_for_deployment(deployment_record: DeploymentRecord) -> str:
    if not control_plane_release_tuples.should_mint_release_tuple_for_channel(
        deployment_record.instance
    ):
        return ""
    return (
        f"{deployment_record.context}-{deployment_record.instance}-"
        f"{_deployment_artifact_id(deployment_record)}"
    )


def _deployment_artifact_id(deployment_record: DeploymentRecord) -> str:
    artifact_identity = deployment_record.artifact_identity
    if artifact_identity is None:
        return ""
    return artifact_identity.artifact_id
