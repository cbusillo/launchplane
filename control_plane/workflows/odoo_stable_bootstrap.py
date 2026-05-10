from __future__ import annotations

from pathlib import Path
import time
from typing import Callable, Literal, Protocol

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane import dokploy as control_plane_dokploy
from control_plane.contracts.deployment_record import DeploymentRecord, ResolvedTargetEvidence
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.promotion_record import HealthcheckEvidence, PostDeployUpdateEvidence
from control_plane.contracts.ship_request import ShipRequest
from control_plane.workflows.inventory import build_environment_inventory
from control_plane.workflows.odoo_post_deploy import OdooPostDeployRequest, execute_odoo_post_deploy
from control_plane.workflows.odoo_stable_target_replacement import (
    _read_lane,
    _target_base_url,
    _target_health_url,
    _verify_canonical_url,
    _verify_health_url,
    _verify_logo_route,
)
from control_plane.workflows.ship import (
    build_deployment_record,
    generate_deployment_record_id,
    utc_now_timestamp,
)

ODOO_STABLE_BOOTSTRAP_CONFIRMATION = "bootstrap cm testing"
ODOO_STABLE_BOOTSTRAP_VERIFY_RETRY_INTERVAL_SECONDS = 5


class OdooStableBootstrapStore(Protocol):
    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord: ...

    def read_dokploy_target_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetRecord: ...

    def read_dokploy_target_id_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetIdRecord: ...

    def read_environment_inventory(
        self, *, context_name: str, instance_name: str
    ) -> EnvironmentInventory: ...

    def write_deployment_record(self, record: DeploymentRecord) -> object: ...

    def write_environment_inventory(self, record: EnvironmentInventory) -> object: ...


class OdooStableBootstrapRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    context: str = "cm"
    instance: str
    confirmation: str
    verify_health: bool = True
    verify_canonical: bool = True
    verify_logo: bool = True
    timeout_seconds: int | None = Field(default=None, ge=1)
    health_timeout_seconds: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _validate_request(self) -> "OdooStableBootstrapRequest":
        self.product = self.product.strip()
        self.context = self.context.strip().lower()
        self.instance = self.instance.strip().lower()
        self.confirmation = self.confirmation.strip().lower()
        if not self.product:
            raise ValueError("Odoo stable bootstrap requires product.")
        if not self.context:
            raise ValueError("Odoo stable bootstrap requires context.")
        if not self.instance:
            raise ValueError("Odoo stable bootstrap requires instance.")
        return self


class OdooStableBootstrapResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    context: str
    instance: str
    deployment_record_id: str = ""
    bootstrap_status: Literal["pass", "fail"]
    post_deploy_status: Literal["pass", "fail", "skipped"] = "skipped"
    health_status: Literal["pass", "fail", "skipped"] = "skipped"
    canonical_status: Literal["pass", "fail", "skipped"] = "skipped"
    logo_status: Literal["pass", "fail", "skipped"] = "skipped"
    target_id: str = ""
    target_name: str = ""
    artifact_id: str = ""
    source_git_ref: str = ""
    error_message: str = ""


def _assert_cm_testing_bootstrap_allowed(request: OdooStableBootstrapRequest) -> None:
    if request.product != "odoo-tenant-cm" or request.context != "cm" or request.instance != "testing":
        raise click.ClickException(
            "Odoo stable bootstrap is currently limited to odoo-tenant-cm cm/testing."
        )
    if request.confirmation != ODOO_STABLE_BOOTSTRAP_CONFIRMATION:
        raise click.ClickException(
            f"Odoo stable bootstrap requires confirmation {ODOO_STABLE_BOOTSTRAP_CONFIRMATION!r}."
        )


def _build_bootstrap_ship_request(
    *,
    context: str,
    instance: str,
    target_record: DokployTargetRecord,
    artifact_id: str,
    source_git_ref: str,
) -> ShipRequest:
    return ShipRequest(
        artifact_id=artifact_id,
        context=context,
        instance=instance,
        source_git_ref=source_git_ref,
        target_name=target_record.target_name or f"{context}-{instance}",
        target_type="compose",
        deploy_mode="dokploy-compose-schedule",
        wait=True,
        verify_health=False,
        no_cache=False,
        destination_health=HealthcheckEvidence(status="pending"),
    )


def _write_failed_bootstrap_deployment(
    *,
    record_store: OdooStableBootstrapStore,
    ship_request: ShipRequest,
    deployment_record_id: str,
    started_at: str,
    resolved_target: ResolvedTargetEvidence,
    post_deploy_update: PostDeployUpdateEvidence | None = None,
    destination_health: HealthcheckEvidence | None = None,
) -> None:
    record_store.write_deployment_record(
        build_deployment_record(
            request=ship_request,
            record_id=deployment_record_id,
            deployment_id="control-plane-dokploy-schedule",
            deployment_status="fail",
            started_at=started_at,
            finished_at=utc_now_timestamp(),
            resolved_target=resolved_target,
            post_deploy_update=post_deploy_update,
            destination_health=destination_health,
        )
    )


def _run_verification_with_retry(
    verification: Callable[[], None],
    *,
    timeout_seconds: int,
    retry_interval_seconds: int = ODOO_STABLE_BOOTSTRAP_VERIFY_RETRY_INTERVAL_SECONDS,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: click.ClickException | None = None
    while True:
        try:
            verification()
            return
        except click.ClickException as error:
            last_error = error
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                raise last_error
            time.sleep(min(retry_interval_seconds, remaining_seconds))


def _base_result(
    *,
    request: OdooStableBootstrapRequest,
    deployment_record_id: str,
    target_record: DokployTargetRecord,
    target_id_record: DokployTargetIdRecord,
    inventory: EnvironmentInventory,
    bootstrap_status: Literal["pass", "fail"],
    post_deploy_status: Literal["pass", "fail", "skipped"] = "skipped",
    health_status: Literal["pass", "fail", "skipped"] = "skipped",
    canonical_status: Literal["pass", "fail", "skipped"] = "skipped",
    logo_status: Literal["pass", "fail", "skipped"] = "skipped",
    error_message: str = "",
) -> OdooStableBootstrapResult:
    artifact_id = ""
    if inventory.artifact_identity is not None:
        artifact_id = inventory.artifact_identity.artifact_id
    return OdooStableBootstrapResult(
        product=request.product,
        context=request.context,
        instance=request.instance,
        deployment_record_id=deployment_record_id,
        bootstrap_status=bootstrap_status,
        post_deploy_status=post_deploy_status,
        health_status=health_status,
        canonical_status=canonical_status,
        logo_status=logo_status,
        target_id=target_id_record.target_id,
        target_name=target_record.target_name,
        artifact_id=artifact_id,
        source_git_ref=inventory.source_git_ref,
        error_message=error_message,
    )


def _read_bootstrap_target_record(
    *, record_store: OdooStableBootstrapStore, context: str, instance: str
) -> DokployTargetRecord:
    try:
        return record_store.read_dokploy_target_record(
            context_name=context, instance_name=instance
        )
    except FileNotFoundError as error:
        raise click.ClickException(
            f"Odoo stable bootstrap requires a Launchplane Dokploy target record for {context}/{instance}."
        ) from error


def _read_bootstrap_target_id_record(
    *, record_store: OdooStableBootstrapStore, context: str, instance: str
) -> DokployTargetIdRecord:
    try:
        return record_store.read_dokploy_target_id_record(
            context_name=context, instance_name=instance
        )
    except FileNotFoundError as error:
        raise click.ClickException(
            f"Odoo stable bootstrap requires a Launchplane Dokploy target-id record for {context}/{instance}."
        ) from error


def _read_bootstrap_inventory(
    *, record_store: OdooStableBootstrapStore, context: str, instance: str
) -> EnvironmentInventory:
    try:
        return record_store.read_environment_inventory(
            context_name=context, instance_name=instance
        )
    except FileNotFoundError as error:
        raise click.ClickException(
            f"Odoo stable bootstrap requires current Launchplane environment inventory for {context}/{instance}."
        ) from error


def execute_odoo_stable_bootstrap(
    *,
    control_plane_root: Path,
    record_store: OdooStableBootstrapStore,
    request: OdooStableBootstrapRequest,
    env_file: Path | None = None,
) -> OdooStableBootstrapResult:
    _assert_cm_testing_bootstrap_allowed(request)
    profile = record_store.read_product_profile_record(request.product)
    lane = _read_lane(profile=profile, instance=request.instance)
    if lane.context.strip().lower() != request.context:
        raise click.ClickException(
            f"Product {request.product!r} lane {request.instance!r} belongs to context {lane.context!r}, not {request.context!r}."
        )
    target_record = _read_bootstrap_target_record(
        record_store=record_store, context=request.context, instance=request.instance
    )
    target_id_record = _read_bootstrap_target_id_record(
        record_store=record_store, context=request.context, instance=request.instance
    )
    if target_record.target_type != "compose":
        raise click.ClickException("Odoo stable bootstrap requires a compose target.")
    inventory = _read_bootstrap_inventory(
        record_store=record_store, context=request.context, instance=request.instance
    )
    artifact_id = ""
    if inventory.artifact_identity is not None:
        artifact_id = inventory.artifact_identity.artifact_id
    if not artifact_id.strip():
        raise click.ClickException("Odoo stable bootstrap requires inventory artifact evidence.")
    if not inventory.source_git_ref.strip():
        raise click.ClickException("Odoo stable bootstrap requires inventory source git ref evidence.")

    deployment_record_id = generate_deployment_record_id(
        context_name=request.context, instance_name=request.instance
    )
    started_at = utc_now_timestamp()
    ship_request = _build_bootstrap_ship_request(
        context=request.context,
        instance=request.instance,
        target_record=target_record,
        artifact_id=artifact_id,
        source_git_ref=inventory.source_git_ref,
    )
    resolved_target = ResolvedTargetEvidence(
        target_type="compose",
        target_id=target_id_record.target_id,
        target_name=target_record.target_name or f"{request.context}-{request.instance}",
    )
    record_store.write_deployment_record(
        build_deployment_record(
            request=ship_request,
            record_id=deployment_record_id,
            deployment_id="control-plane-dokploy-schedule",
            deployment_status="pending",
            started_at=started_at,
            finished_at="",
            resolved_target=resolved_target,
            destination_health=HealthcheckEvidence(status="pending")
            if request.verify_health
            else HealthcheckEvidence(status="skipped"),
        )
    )

    try:
        host, token = control_plane_dokploy.read_dokploy_config(
            control_plane_root=control_plane_root
        )
        target_definition = control_plane_dokploy.DokployTargetDefinition(
            context=request.context,
            instance=request.instance,
            target_type="compose",
            target_id=target_id_record.target_id,
            target_name=target_record.target_name,
            project_name=target_record.project_name,
            deploy_timeout_seconds=target_record.deploy_timeout_seconds,
            healthcheck_timeout_seconds=target_record.healthcheck_timeout_seconds,
        )
        control_plane_dokploy.run_compose_odoo_stable_bootstrap(
            host=host,
            token=token,
            target_definition=target_definition,
            env_file=env_file,
            timeout_seconds=request.timeout_seconds,
        )
    except click.ClickException as error:
        _write_failed_bootstrap_deployment(
            record_store=record_store,
            ship_request=ship_request,
            deployment_record_id=deployment_record_id,
            started_at=started_at,
            resolved_target=resolved_target,
            destination_health=HealthcheckEvidence(status="skipped"),
        )
        return _base_result(
            request=request,
            deployment_record_id=deployment_record_id,
            target_record=target_record,
            target_id_record=target_id_record,
            inventory=inventory,
            bootstrap_status="fail",
            error_message=str(error),
        )

    post_deploy_result = execute_odoo_post_deploy(
        control_plane_root=control_plane_root,
        record_store=record_store,
        request=OdooPostDeployRequest(
            context=request.context, instance=request.instance, phase="deploy"
        ),
    )
    post_deploy_evidence = PostDeployUpdateEvidence(
        attempted=True,
        status=post_deploy_result.post_deploy_status,
        detail=post_deploy_result.error_message
        or "Odoo post-deploy completed after stable bootstrap.",
    )
    if post_deploy_result.post_deploy_status != "pass":
        _write_failed_bootstrap_deployment(
            record_store=record_store,
            ship_request=ship_request,
            deployment_record_id=deployment_record_id,
            started_at=started_at,
            resolved_target=resolved_target,
            post_deploy_update=post_deploy_evidence,
            destination_health=HealthcheckEvidence(status="skipped"),
        )
        return _base_result(
            request=request,
            deployment_record_id=deployment_record_id,
            target_record=target_record,
            target_id_record=target_id_record,
            inventory=inventory,
            bootstrap_status="fail",
            post_deploy_status=post_deploy_result.post_deploy_status,
            error_message=post_deploy_result.error_message or "Odoo post-deploy failed.",
        )

    base_url = _target_base_url(lane=lane, domains=target_record.domains)
    health_url = _target_health_url(
        profile=profile, lane=lane, domains=target_record.domains
    )
    health_timeout_seconds = (
        request.health_timeout_seconds
        or target_record.healthcheck_timeout_seconds
        or control_plane_dokploy.DEFAULT_DOKPLOY_HEALTH_TIMEOUT_SECONDS
    )
    health_status: Literal["pass", "fail", "skipped"] = "skipped"
    canonical_status: Literal["pass", "fail", "skipped"] = "skipped"
    logo_status: Literal["pass", "fail", "skipped"] = "skipped"
    try:
        if request.verify_health:
            if not health_url:
                raise click.ClickException("Odoo stable bootstrap has no health URL.")
            _run_verification_with_retry(
                lambda: _verify_health_url(
                    health_url=health_url, timeout_seconds=health_timeout_seconds
                ),
                timeout_seconds=health_timeout_seconds,
            )
            health_status = "pass"
        if request.verify_canonical:
            if not base_url:
                raise click.ClickException("Odoo stable bootstrap has no base URL.")
            _verify_canonical_url(
                base_url=base_url,
                expected_base_url=base_url,
                timeout_seconds=health_timeout_seconds,
            )
            canonical_status = "pass"
        if request.verify_logo:
            if not base_url:
                raise click.ClickException("Odoo stable bootstrap has no base URL.")
            _verify_logo_route(base_url=base_url, timeout_seconds=health_timeout_seconds)
            logo_status = "pass"
    except click.ClickException as error:
        destination_health = HealthcheckEvidence(
            verified=health_status == "pass",
            urls=(health_url,) if health_url else (),
            timeout_seconds=health_timeout_seconds if health_url else None,
            status="fail",
        )
        _write_failed_bootstrap_deployment(
            record_store=record_store,
            ship_request=ship_request,
            deployment_record_id=deployment_record_id,
            started_at=started_at,
            resolved_target=resolved_target,
            post_deploy_update=post_deploy_evidence,
            destination_health=destination_health,
        )
        return _base_result(
            request=request,
            deployment_record_id=deployment_record_id,
            target_record=target_record,
            target_id_record=target_id_record,
            inventory=inventory,
            bootstrap_status="fail",
            post_deploy_status="pass",
            health_status=health_status if health_status == "pass" else "fail",
            canonical_status=canonical_status if canonical_status == "pass" else "fail",
            logo_status=logo_status if logo_status == "pass" else "fail",
            error_message=str(error),
        )

    finished_at = utc_now_timestamp()
    destination_health = HealthcheckEvidence(
        verified=request.verify_health,
        urls=(health_url,) if request.verify_health and health_url else (),
        timeout_seconds=health_timeout_seconds if request.verify_health and health_url else None,
        status=health_status,
    )
    deployment_record = build_deployment_record(
        request=ship_request,
        record_id=deployment_record_id,
        deployment_id="control-plane-dokploy-schedule",
        deployment_status="pass",
        started_at=started_at,
        finished_at=finished_at,
        resolved_target=resolved_target,
        post_deploy_update=post_deploy_evidence,
        destination_health=destination_health,
    )
    record_store.write_deployment_record(deployment_record)
    record_store.write_environment_inventory(
        build_environment_inventory(deployment_record=deployment_record, updated_at=finished_at)
    )
    return _base_result(
        request=request,
        deployment_record_id=deployment_record_id,
        target_record=target_record,
        target_id_record=target_id_record,
        inventory=inventory,
        bootstrap_status="pass",
        post_deploy_status="pass",
        health_status=health_status,
        canonical_status=canonical_status,
        logo_status=logo_status,
    )
