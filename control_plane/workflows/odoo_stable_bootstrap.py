from __future__ import annotations

from pathlib import Path
import time
from typing import Callable, Literal, Protocol

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane import dokploy as control_plane_dokploy
from control_plane import odoo_instance_overrides as control_plane_odoo_instance_overrides
from control_plane.contracts.deployment_record import DeploymentRecord, ResolvedTargetEvidence
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.odoo_instance_override_record import OdooInstanceOverrideRecord
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.contracts.promotion_record import (
    BootstrapEvidence,
    HealthcheckEvidence,
    PostDeployUpdateEvidence,
)
from control_plane.contracts.ship_request import ShipRequest
from control_plane.workflows.inventory import build_environment_inventory
from control_plane.workflows.odoo_post_deploy import OdooPostDeployRequest, execute_odoo_post_deploy
from control_plane.workflows.odoo_stable_target_replacement import (
    DokployRequest,
    _domains_for_target,
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

ODOO_STABLE_BOOTSTRAP_VERIFY_RETRY_INTERVAL_SECONDS = 5


class OdooStableBootstrapStore(Protocol):
    def read_odoo_instance_override_record(
        self, *, context_name: str, instance_name: str
    ) -> OdooInstanceOverrideRecord: ...

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
    bootstrap_run_status: Literal["pass", "fail", "skipped"] = "skipped"
    readiness_status: Literal["pass", "fail", "verification_failed", "skipped"] = "skipped"
    post_deploy_status: Literal["pass", "fail", "skipped"] = "skipped"
    health_status: Literal["pass", "fail", "skipped"] = "skipped"
    canonical_status: Literal["pass", "fail", "skipped"] = "skipped"
    logo_status: Literal["pass", "fail", "skipped"] = "skipped"
    target_id: str = ""
    target_name: str = ""
    artifact_id: str = ""
    source_git_ref: str = ""
    error_message: str = ""


def _normalize_domain(raw_domain: str) -> str:
    return raw_domain.strip().lower().removeprefix("https://").removeprefix("http://").rstrip("/")


def _assert_bootstrap_policy_allows_request(
    *,
    request: OdooStableBootstrapRequest,
    lane: ProductLaneProfile,
    target_record: DokployTargetRecord,
    domain_hosts: tuple[str, ...],
    check_domains: bool = True,
) -> None:
    policy = lane.odoo_stable_bootstrap
    if not policy.enabled:
        raise click.ClickException(
            f"Odoo stable bootstrap is not enabled for {request.product} {request.context}/{request.instance}."
        )
    if request.confirmation != policy.confirmation:
        raise click.ClickException(
            f"Odoo stable bootstrap requires confirmation {policy.confirmation!r}."
        )
    if policy.data_source_mode != "empty":
        raise click.ClickException(
            f"Unsupported Odoo stable bootstrap data source mode {policy.data_source_mode!r}."
        )
    if policy.expected_target_name and target_record.target_name != policy.expected_target_name:
        raise click.ClickException(
            "Odoo stable bootstrap target proof failed: expected target "
            f"{policy.expected_target_name!r}, observed {target_record.target_name!r}."
        )
    if check_domains:
        target_domains = {
            _normalize_domain(domain) for domain in domain_hosts if domain.strip()
        }
        missing_domains = tuple(
            domain for domain in policy.expected_domains if domain not in target_domains
        )
        if missing_domains:
            raise click.ClickException(
                "Odoo stable bootstrap target proof failed: missing expected domain(s) "
                + ", ".join(missing_domains)
            )
    if policy.require_health_verification and not request.verify_health:
        raise click.ClickException("Odoo stable bootstrap policy requires health verification.")
    if policy.require_canonical_verification and not request.verify_canonical:
        raise click.ClickException("Odoo stable bootstrap policy requires canonical verification.")
    if policy.require_logo_verification and not request.verify_logo:
        raise click.ClickException("Odoo stable bootstrap policy requires logo verification.")


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
    bootstrap: BootstrapEvidence | None = None,
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
            bootstrap=bootstrap,
            post_deploy_update=post_deploy_update,
            destination_health=destination_health,
        )
    )


def _write_bootstrap_inventory_pointer(
    *,
    record_store: OdooStableBootstrapStore,
    inventory: EnvironmentInventory,
    bootstrap_record_id: str,
) -> None:
    record_store.write_environment_inventory(
        inventory.model_copy(update={"bootstrap_record_id": bootstrap_record_id})
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
    bootstrap_run_status: Literal["pass", "fail", "skipped"] = "skipped",
    readiness_status: Literal["pass", "fail", "verification_failed", "skipped"] = "skipped",
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
        bootstrap_run_status=bootstrap_run_status,
        readiness_status=readiness_status,
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
        return record_store.read_dokploy_target_record(context_name=context, instance_name=instance)
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
        return record_store.read_environment_inventory(context_name=context, instance_name=instance)
    except FileNotFoundError as error:
        raise click.ClickException(
            f"Odoo stable bootstrap requires current Launchplane environment inventory for {context}/{instance}."
        ) from error


def _read_odoo_instance_override_record(
    *, record_store: OdooStableBootstrapStore, context: str, instance: str
) -> OdooInstanceOverrideRecord | None:
    try:
        return record_store.read_odoo_instance_override_record(
            context_name=context,
            instance_name=instance,
        )
    except FileNotFoundError:
        return None


def execute_odoo_stable_bootstrap(
    *,
    control_plane_root: Path,
    record_store: OdooStableBootstrapStore,
    request: OdooStableBootstrapRequest,
    env_file: Path | None = None,
    dokploy_request: DokployRequest = control_plane_dokploy.dokploy_request,
) -> OdooStableBootstrapResult:
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
    odoo_override_record = _read_odoo_instance_override_record(
        record_store=record_store,
        context=request.context,
        instance=request.instance,
    )
    if target_record.target_type != "compose":
        raise click.ClickException("Odoo stable bootstrap requires a compose target.")
    _assert_bootstrap_policy_allows_request(
        request=request,
        lane=lane,
        target_record=target_record,
        domain_hosts=target_record.domains,
        check_domains=False,
    )
    inventory = _read_bootstrap_inventory(
        record_store=record_store, context=request.context, instance=request.instance
    )
    artifact_id = ""
    if inventory.artifact_identity is not None:
        artifact_id = inventory.artifact_identity.artifact_id
    if not artifact_id.strip():
        raise click.ClickException("Odoo stable bootstrap requires inventory artifact evidence.")
    if not inventory.source_git_ref.strip():
        raise click.ClickException(
            "Odoo stable bootstrap requires inventory source git ref evidence."
        )

    host, token = control_plane_dokploy.read_dokploy_config(control_plane_root=control_plane_root)
    domain_hosts = _domains_for_target(
        host=host,
        token=token,
        target_type=target_record.target_type,
        target_id=target_id_record.target_id,
        request=dokploy_request,
    )
    resolved_domains = domain_hosts or target_record.domains
    _assert_bootstrap_policy_allows_request(
        request=request,
        lane=lane,
        target_record=target_record,
        domain_hosts=resolved_domains,
    )

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
            bootstrap=BootstrapEvidence(
                attempted=True,
                run_status="pending",
                readiness_status="pending",
                detail="Odoo stable bootstrap schedule is pending.",
            ),
            destination_health=HealthcheckEvidence(status="pending")
            if request.verify_health
            else HealthcheckEvidence(status="skipped"),
        )
    )

    try:
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
        workflow_environment_overrides: dict[str, str] = {}
        required_workflow_environment_keys: tuple[str, ...] = ()
        if odoo_override_record is not None and "deploy" in odoo_override_record.apply_on:
            post_deploy_environment = control_plane_odoo_instance_overrides.build_post_deploy_environment(
                odoo_override_record,
                protected_shopify_store_keys=control_plane_dokploy.protected_shopify_store_keys_for_target_definition(
                    target_definition
                ),
            )
            workflow_environment_overrides = post_deploy_environment.inline_environment
            required_workflow_environment_keys = (
                post_deploy_environment.required_container_environment_keys
            )
        control_plane_dokploy.run_compose_odoo_stable_bootstrap(
            host=host,
            token=token,
            target_definition=target_definition,
            env_file=env_file,
            workflow_environment_overrides=workflow_environment_overrides,
            required_workflow_environment_keys=required_workflow_environment_keys,
            timeout_seconds=request.timeout_seconds,
        )
    except click.ClickException as error:
        _write_failed_bootstrap_deployment(
            record_store=record_store,
            ship_request=ship_request,
            deployment_record_id=deployment_record_id,
            started_at=started_at,
            resolved_target=resolved_target,
            bootstrap=BootstrapEvidence(
                attempted=True,
                run_status="fail",
                readiness_status="fail",
                detail=str(error),
            ),
            destination_health=HealthcheckEvidence(status="skipped"),
        )
        _write_bootstrap_inventory_pointer(
            record_store=record_store,
            inventory=inventory,
            bootstrap_record_id=deployment_record_id,
        )
        return _base_result(
            request=request,
            deployment_record_id=deployment_record_id,
            target_record=target_record,
            target_id_record=target_id_record,
            inventory=inventory,
            bootstrap_status="fail",
            bootstrap_run_status="fail",
            readiness_status="fail",
            error_message=str(error),
        )

    try:
        post_deploy_result = execute_odoo_post_deploy(
            control_plane_root=control_plane_root,
            record_store=record_store,
            request=OdooPostDeployRequest(
                context=request.context, instance=request.instance, phase="deploy"
            ),
        )
    except click.ClickException as error:
        post_deploy_evidence = PostDeployUpdateEvidence(
            attempted=True,
            status="fail",
            detail=str(error),
        )
        _write_failed_bootstrap_deployment(
            record_store=record_store,
            ship_request=ship_request,
            deployment_record_id=deployment_record_id,
            started_at=started_at,
            resolved_target=resolved_target,
            bootstrap=BootstrapEvidence(
                attempted=True,
                run_status="pass",
                readiness_status="fail",
                detail=str(error),
            ),
            post_deploy_update=post_deploy_evidence,
            destination_health=HealthcheckEvidence(status="skipped"),
        )
        _write_bootstrap_inventory_pointer(
            record_store=record_store,
            inventory=inventory,
            bootstrap_record_id=deployment_record_id,
        )
        return _base_result(
            request=request,
            deployment_record_id=deployment_record_id,
            target_record=target_record,
            target_id_record=target_id_record,
            inventory=inventory,
            bootstrap_status="fail",
            bootstrap_run_status="pass",
            readiness_status="fail",
            post_deploy_status="fail",
            error_message=str(error),
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
            bootstrap=BootstrapEvidence(
                attempted=True,
                run_status="pass",
                readiness_status="fail",
                detail=post_deploy_result.error_message or "Odoo post-deploy failed.",
            ),
            post_deploy_update=post_deploy_evidence,
            destination_health=HealthcheckEvidence(status="skipped"),
        )
        _write_bootstrap_inventory_pointer(
            record_store=record_store,
            inventory=inventory,
            bootstrap_record_id=deployment_record_id,
        )
        return _base_result(
            request=request,
            deployment_record_id=deployment_record_id,
            target_record=target_record,
            target_id_record=target_id_record,
            inventory=inventory,
            bootstrap_status="fail",
            bootstrap_run_status="pass",
            readiness_status="fail",
            post_deploy_status=post_deploy_result.post_deploy_status,
            error_message=post_deploy_result.error_message or "Odoo post-deploy failed.",
        )

    base_url = _target_base_url(lane=lane, domains=resolved_domains)
    health_url = _target_health_url(profile=profile, lane=lane, domains=resolved_domains)
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
            bootstrap=BootstrapEvidence(
                attempted=True,
                run_status="pass",
                readiness_status="verification_failed",
                detail=str(error),
            ),
            post_deploy_update=post_deploy_evidence,
            destination_health=destination_health,
        )
        _write_bootstrap_inventory_pointer(
            record_store=record_store,
            inventory=inventory,
            bootstrap_record_id=deployment_record_id,
        )
        return _base_result(
            request=request,
            deployment_record_id=deployment_record_id,
            target_record=target_record,
            target_id_record=target_id_record,
            inventory=inventory,
            bootstrap_status="fail",
            bootstrap_run_status="pass",
            readiness_status="verification_failed",
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
        bootstrap=BootstrapEvidence(
            attempted=True,
            run_status="pass",
            readiness_status="pass",
            detail="Odoo stable bootstrap, post-deploy, and verification completed.",
        ),
        post_deploy_update=post_deploy_evidence,
        destination_health=destination_health,
    )
    record_store.write_deployment_record(deployment_record)
    record_store.write_environment_inventory(
        build_environment_inventory(
            deployment_record=deployment_record,
            updated_at=finished_at,
            bootstrap_record_id=deployment_record_id,
        )
    )
    return _base_result(
        request=request,
        deployment_record_id=deployment_record_id,
        target_record=target_record,
        target_id_record=target_id_record,
        inventory=inventory,
        bootstrap_status="pass",
        bootstrap_run_status="pass",
        readiness_status="pass",
        post_deploy_status="pass",
        health_status=health_status,
        canonical_status=canonical_status,
        logo_status=logo_status,
    )
