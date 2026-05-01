from __future__ import annotations

import time
from pathlib import Path
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    BackupGateEvidence,
    DeploymentEvidence,
    HealthcheckEvidence,
    PromotionRecord,
    ReleaseStatus,
)
from control_plane.workflows.generic_web_deploy import (
    GenericWebDeployRequest,
    execute_generic_web_deploy,
    resolve_generic_web_profile_lane,
)
from control_plane.workflows.inventory import build_environment_inventory
from control_plane.workflows.promote import generate_promotion_record_id
from control_plane.workflows.ship import utc_now_timestamp

DEFAULT_GENERIC_WEB_HEALTH_TIMEOUT_SECONDS = 60


class GenericWebProdPromotionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    artifact_id: str
    source_git_ref: str
    from_instance: str = "testing"
    to_instance: str = "prod"
    backup_record_id: str = ""
    backup_required: bool = False
    wait: bool = True
    timeout_seconds: int | None = Field(default=None, ge=1)
    verify_health: bool = True
    health_timeout_seconds: int = Field(default=DEFAULT_GENERIC_WEB_HEALTH_TIMEOUT_SECONDS, ge=1)
    dry_run: bool = False
    no_cache: bool = False

    @model_validator(mode="after")
    def _validate_request(self) -> "GenericWebProdPromotionRequest":
        self.product = self.product.strip()
        self.artifact_id = self.artifact_id.strip()
        self.source_git_ref = self.source_git_ref.strip()
        self.from_instance = self.from_instance.strip().lower()
        self.to_instance = self.to_instance.strip().lower()
        self.backup_record_id = self.backup_record_id.strip()
        if not self.product:
            raise ValueError("generic web prod promotion requires product")
        if not self.artifact_id:
            raise ValueError("generic web prod promotion requires artifact_id")
        if not self.source_git_ref:
            raise ValueError("generic web prod promotion requires source_git_ref")
        if self.from_instance == self.to_instance:
            raise ValueError("generic web prod promotion source and destination must differ")
        if self.from_instance != "testing" or self.to_instance != "prod":
            raise ValueError("generic web prod promotion requires testing -> prod")
        if self.backup_required and not self.backup_record_id:
            raise ValueError(
                "generic web prod promotion requires backup_record_id when backup_required=true"
            )
        if not self.wait:
            raise ValueError("generic web prod promotion requires wait=true")
        return self


class GenericWebProdPromotionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    context: str
    from_instance: str
    to_instance: str
    artifact_id: str
    backup_record_id: str = ""
    promotion_record_id: str
    deployment_record_id: str = ""
    inventory_record_id: str = ""
    promotion_status: Literal["pending", "pass", "fail"]
    deployment_status: ReleaseStatus = "skipped"
    backup_status: ReleaseStatus = "skipped"
    source_health_status: ReleaseStatus = "skipped"
    destination_health_status: ReleaseStatus = "skipped"
    target_name: str = ""
    target_type: str = ""
    target_id: str = ""
    dry_run: bool = False
    error_message: str = ""


def resolve_generic_web_promotion_lanes(
    *, record_store: object, request: GenericWebProdPromotionRequest
) -> tuple[LaunchplaneProductProfileRecord, ProductLaneProfile, ProductLaneProfile]:
    source_profile, source_lane = resolve_generic_web_profile_lane(
        record_store=record_store,
        request=GenericWebDeployRequest(
            product=request.product,
            instance=request.from_instance,
            artifact_id=request.artifact_id,
            source_git_ref=request.source_git_ref,
        ),
    )
    destination_profile, destination_lane = resolve_generic_web_profile_lane(
        record_store=record_store,
        request=GenericWebDeployRequest(
            product=request.product,
            instance=request.to_instance,
            artifact_id=request.artifact_id,
            source_git_ref=request.source_git_ref,
        ),
    )
    if source_profile.product != destination_profile.product:
        raise click.ClickException("Generic web promotion resolved inconsistent product profiles.")
    if source_lane.context != destination_lane.context:
        raise click.ClickException(
            "Generic web promotion currently requires source and destination lanes to share a context. "
            f"Resolved source={source_lane.context} destination={destination_lane.context}."
        )
    return source_profile, source_lane, destination_lane


def execute_generic_web_prod_promotion(
    *,
    control_plane_root: Path,
    record_store: object,
    request: GenericWebProdPromotionRequest,
) -> GenericWebProdPromotionResult:
    profile, source_lane, destination_lane = resolve_generic_web_promotion_lanes(
        record_store=record_store,
        request=request,
    )
    promotion_record_id = generate_promotion_record_id(
        context_name=destination_lane.context,
        from_instance_name=source_lane.instance,
        to_instance_name=destination_lane.instance,
    )
    source_health = _health_evidence_for_lane(
        lane=source_lane,
        request=request,
        health_path=profile.health_path,
        status="pending" if request.verify_health else "skipped",
    )
    destination_health = _health_evidence_for_lane(
        lane=destination_lane,
        request=request,
        health_path=profile.health_path,
        status="pending" if request.verify_health else "skipped",
    )
    backup_gate = _resolve_backup_gate(
        record_store=record_store,
        request=request,
        context=destination_lane.context,
    )

    if request.dry_run:
        return GenericWebProdPromotionResult(
            product=request.product,
            context=destination_lane.context,
            from_instance=source_lane.instance,
            to_instance=destination_lane.instance,
            artifact_id=request.artifact_id,
            backup_record_id=request.backup_record_id,
            promotion_record_id=promotion_record_id,
            promotion_status="pending",
            backup_status=backup_gate.status,
            source_health_status=source_health.status,
            destination_health_status=destination_health.status,
            dry_run=True,
        )

    try:
        source_health = _verify_health_evidence(source_health)
    except click.ClickException as error:
        failed_record = _build_promotion_record(
            request=request,
            promotion_record_id=promotion_record_id,
            context=destination_lane.context,
            source_health=_mark_health_failed(source_health),
            backup_gate=backup_gate,
            destination_health=_mark_health_skipped(destination_health),
            deployment_record=None,
            deployment_status="fail",
            target_name=_fallback_target_name(request=request, lane=destination_lane),
            target_type="application",
            deployment_record_id="",
        )
        record_store.write_promotion_record(failed_record)
        return _result_from_record(
            request=request,
            record=failed_record,
            deployment_record=None,
            inventory_record_id="",
            target_id="",
            dry_run=False,
            error_message=str(error),
        )

    deploy_result = execute_generic_web_deploy(
        control_plane_root=control_plane_root,
        record_store=record_store,
        request=GenericWebDeployRequest(
            product=request.product,
            instance=destination_lane.instance,
            artifact_id=request.artifact_id,
            source_git_ref=request.source_git_ref,
            timeout_seconds=request.timeout_seconds,
            no_cache=request.no_cache,
        ),
        lane=destination_lane,
    )
    deployment_record = _read_deployment_record(
        record_store=record_store,
        deployment_record_id=deploy_result.deployment_record_id,
    )
    if deploy_result.deploy_status == "pass":
        try:
            destination_health = _verify_health_evidence(destination_health)
        except click.ClickException as error:
            destination_health = _mark_health_failed(destination_health)
            _write_deployment_health(
                record_store=record_store,
                deployment_record=deployment_record,
                destination_health=destination_health,
            )
            final_record = _build_promotion_record(
                request=request,
                promotion_record_id=promotion_record_id,
                context=destination_lane.context,
                source_health=source_health,
                backup_gate=backup_gate,
                destination_health=destination_health,
                deployment_record=deployment_record,
                deployment_status="fail",
                target_name=deploy_result.target_name,
                target_type=deploy_result.target_type or "application",
                deployment_record_id=deploy_result.deployment_record_id,
            )
            record_store.write_promotion_record(final_record)
            return _result_from_record(
                request=request,
                record=final_record,
                deployment_record=deployment_record,
                inventory_record_id="",
                target_id=deploy_result.target_id,
                dry_run=False,
                error_message=str(error),
            )
    else:
        destination_health = _mark_health_skipped(destination_health)

    deployment_record = _write_deployment_health(
        record_store=record_store,
        deployment_record=deployment_record,
        destination_health=destination_health,
    )
    final_record = _build_promotion_record(
        request=request,
        promotion_record_id=promotion_record_id,
        context=destination_lane.context,
        source_health=source_health,
        backup_gate=backup_gate,
        destination_health=destination_health,
        deployment_record=deployment_record,
        deployment_status=deploy_result.deploy_status,
        target_name=deploy_result.target_name,
        target_type=deploy_result.target_type or "application",
        deployment_record_id=deploy_result.deployment_record_id,
    )
    record_store.write_promotion_record(final_record)
    inventory_record_id = ""
    if deployment_record.deploy.status == "pass" and destination_health.status in {
        "pass",
        "skipped",
    }:
        inventory = build_environment_inventory(
            deployment_record=deployment_record,
            updated_at=utc_now_timestamp(),
            promotion_record_id=final_record.record_id,
            promoted_from_instance=final_record.from_instance,
        )
        record_store.write_environment_inventory(inventory)
        inventory_record_id = f"{inventory.context}-{inventory.instance}"
    return _result_from_record(
        request=request,
        record=final_record,
        deployment_record=deployment_record,
        inventory_record_id=inventory_record_id,
        target_id=deploy_result.target_id,
        dry_run=False,
        error_message=deploy_result.error_message,
    )


def _resolve_backup_gate(
    *, record_store: object, request: GenericWebProdPromotionRequest, context: str
) -> BackupGateEvidence:
    if not request.backup_record_id:
        return BackupGateEvidence(required=request.backup_required, status="skipped", evidence={})
    try:
        backup_record: BackupGateRecord = record_store.read_backup_gate_record(
            request.backup_record_id
        )
    except FileNotFoundError as exc:
        raise click.ClickException(
            f"Generic web prod promotion requires stored backup gate record '{request.backup_record_id}'."
        ) from exc
    if backup_record.instance != request.to_instance:
        raise click.ClickException(
            "Backup gate record instance does not match generic web prod promotion destination. "
            f"Record={backup_record.instance} request={request.to_instance}."
        )
    if backup_record.context != context:
        raise click.ClickException(
            "Backup gate record context does not match generic web prod promotion context. "
            f"Record={backup_record.context} request={context}."
        )
    if backup_record.required and backup_record.status != "pass":
        raise click.ClickException(
            f"Backup gate record '{backup_record.record_id}' must have status=pass before prod promotion."
        )
    if request.backup_required and not backup_record.required:
        raise click.ClickException(
            f"Backup gate record '{backup_record.record_id}' is marked required=false."
        )
    return BackupGateEvidence(
        required=backup_record.required or request.backup_required,
        status=backup_record.status,
        evidence=dict(backup_record.evidence),
    )


def _health_url_for_lane(*, lane: ProductLaneProfile, health_path: str) -> str:
    health_url = lane.health_url.strip()
    if health_url:
        return health_url
    base_url = lane.base_url.strip().rstrip("/")
    if base_url:
        normalized_health_path = health_path.strip() or "/api/health"
        if not normalized_health_path.startswith("/"):
            normalized_health_path = f"/{normalized_health_path}"
        return f"{base_url}{normalized_health_path}"
    return ""


def _health_evidence_for_lane(
    *,
    lane: ProductLaneProfile,
    request: GenericWebProdPromotionRequest,
    health_path: str,
    status: ReleaseStatus,
) -> HealthcheckEvidence:
    if not request.verify_health:
        return HealthcheckEvidence(status="skipped")
    health_url = _health_url_for_lane(lane=lane, health_path=health_path)
    if not health_url:
        return HealthcheckEvidence(status="skipped")
    return HealthcheckEvidence(
        urls=(health_url,),
        timeout_seconds=request.health_timeout_seconds,
        status=status,
    )


def _wait_for_healthcheck(*, url: str, timeout_seconds: int) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error = ""
    while time.monotonic() < deadline:
        try:
            request = Request(url, method="GET")
            with urlopen(request, timeout=min(5, timeout_seconds)) as response:
                if 200 <= response.status < 300:
                    return
                last_error = f"http {response.status}"
        except HTTPError as error:
            last_error = f"http {error.code}"
        except URLError as error:
            last_error = str(error.reason)
        time.sleep(1)
    raise click.ClickException(f"Healthcheck failed for {url}: {last_error or 'timeout'}")


def _verify_health_evidence(evidence: HealthcheckEvidence) -> HealthcheckEvidence:
    if evidence.status == "skipped" or not evidence.urls:
        return evidence
    timeout_seconds = evidence.timeout_seconds or DEFAULT_GENERIC_WEB_HEALTH_TIMEOUT_SECONDS
    healthcheck_errors: list[str] = []
    for health_url in evidence.urls:
        try:
            _wait_for_healthcheck(url=health_url, timeout_seconds=timeout_seconds)
            return HealthcheckEvidence(
                verified=True,
                urls=evidence.urls,
                timeout_seconds=timeout_seconds,
                status="pass",
            )
        except click.ClickException as error:
            healthcheck_errors.append(str(error))
    raise click.ClickException(
        "Healthcheck verification failed for all generic web URLs:\n"
        + "\n".join(healthcheck_errors)
    )


def _mark_health_failed(evidence: HealthcheckEvidence) -> HealthcheckEvidence:
    if evidence.status == "skipped":
        return evidence
    return HealthcheckEvidence(
        verified=bool(evidence.urls),
        urls=evidence.urls,
        timeout_seconds=evidence.timeout_seconds,
        status="fail",
    )


def _mark_health_skipped(evidence: HealthcheckEvidence) -> HealthcheckEvidence:
    return HealthcheckEvidence(
        urls=evidence.urls,
        timeout_seconds=evidence.timeout_seconds,
        status="skipped",
    )


def _read_deployment_record(*, record_store: object, deployment_record_id: str) -> DeploymentRecord:
    try:
        return record_store.read_deployment_record(deployment_record_id)
    except FileNotFoundError as exc:
        raise click.ClickException(
            f"Generic web prod promotion could not read deployment record '{deployment_record_id}'."
        ) from exc


def _write_deployment_health(
    *,
    record_store: object,
    deployment_record: DeploymentRecord,
    destination_health: HealthcheckEvidence,
) -> DeploymentRecord:
    updated_record = deployment_record.model_copy(
        update={
            "verify_destination_health": destination_health.status != "skipped",
            "destination_health": destination_health,
        }
    )
    record_store.write_deployment_record(updated_record)
    return updated_record


def _build_promotion_record(
    *,
    request: GenericWebProdPromotionRequest,
    promotion_record_id: str,
    context: str,
    source_health: HealthcheckEvidence,
    backup_gate: BackupGateEvidence,
    destination_health: HealthcheckEvidence,
    deployment_record: DeploymentRecord | None,
    deployment_status: ReleaseStatus,
    target_name: str,
    target_type: str,
    deployment_record_id: str,
) -> PromotionRecord:
    target_deploy_mode = "dokploy-application-api"
    target_deployment_id = "control-plane-dokploy"
    resolved_target_name = target_name or _fallback_target_name(
        request=request,
        lane=ProductLaneProfile(instance=request.to_instance, context=context),
    )
    if deployment_record is not None:
        target_deploy_mode = deployment_record.deploy.deploy_mode
        target_deployment_id = deployment_record.deploy.deployment_id
        resolved_target_name = deployment_record.deploy.target_name
        target_type = deployment_record.deploy.target_type
    return PromotionRecord(
        record_id=promotion_record_id,
        artifact_identity=ArtifactIdentityReference(artifact_id=request.artifact_id),
        deployment_record_id=deployment_record_id,
        backup_record_id=request.backup_record_id,
        context=context,
        from_instance=request.from_instance,
        to_instance=request.to_instance,
        source_health=source_health,
        backup_gate=backup_gate,
        deploy=DeploymentEvidence(
            target_name=resolved_target_name,
            target_type=target_type,
            deploy_mode=target_deploy_mode,
            deployment_id=target_deployment_id,
            status=deployment_status,
            started_at=deployment_record.deploy.started_at if deployment_record is not None else "",
            finished_at=deployment_record.deploy.finished_at
            if deployment_record is not None
            else "",
        ),
        destination_health=destination_health,
    )


def _fallback_target_name(
    *, request: GenericWebProdPromotionRequest, lane: ProductLaneProfile
) -> str:
    return f"{request.product}-{lane.instance}"


def _result_from_record(
    *,
    request: GenericWebProdPromotionRequest,
    record: PromotionRecord,
    deployment_record: DeploymentRecord | None,
    inventory_record_id: str,
    target_id: str,
    dry_run: bool,
    error_message: str,
) -> GenericWebProdPromotionResult:
    return GenericWebProdPromotionResult(
        product=request.product,
        context=record.context,
        from_instance=record.from_instance,
        to_instance=record.to_instance,
        artifact_id=record.artifact_identity.artifact_id,
        backup_record_id=record.backup_record_id,
        promotion_record_id=record.record_id,
        deployment_record_id=deployment_record.record_id if deployment_record is not None else "",
        inventory_record_id=inventory_record_id,
        promotion_status="pass"
        if record.deploy.status == "pass"
        and record.destination_health.status in {"pass", "skipped"}
        else "fail",
        deployment_status=record.deploy.status,
        backup_status=record.backup_gate.status,
        source_health_status=record.source_health.status,
        destination_health_status=record.destination_health.status,
        target_name=record.deploy.target_name,
        target_type=record.deploy.target_type,
        target_id=target_id,
        dry_run=dry_run,
        error_message=error_message,
    )
