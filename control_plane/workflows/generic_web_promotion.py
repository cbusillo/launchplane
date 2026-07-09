from __future__ import annotations

import time
from pathlib import Path
from typing import Literal, Protocol, cast
from urllib.parse import quote

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.deploy_target import DeployTargetCategory
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.dokploy_target_record import DokployTargetType
from control_plane.contracts.environment_inventory import EnvironmentInventory
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
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.workflows.generic_web_deploy import (
    GenericWebDeployStore,
    GenericWebDeployRequest,
    execute_generic_web_deploy,
    normalize_generic_web_artifact_id,
    product_profile_uses_generic_web_base,
)
from control_plane.workflows.inventory import build_environment_inventory
from control_plane.workflows.launchplane import (
    github_api_request,
    resolve_launchplane_github_token,
)
from control_plane.workflows.runtime_identity_health import (
    RuntimeIdentityHealthcheckError,
    healthcheck_evidence_with_runtime_identity,
    wait_for_healthcheck_with_retry,
    wait_for_runtime_identity_healthcheck_with_retry,
)
from control_plane.workflows.promote import generate_promotion_record_id
from control_plane.workflows.ship import utc_now_timestamp

DEFAULT_GENERIC_WEB_HEALTH_TIMEOUT_SECONDS = 60


class GenericWebPromotionStore(GenericWebDeployStore, Protocol):
    def read_environment_inventory(
        self, *, context_name: str, instance_name: str
    ) -> EnvironmentInventory: ...

    def read_backup_gate_record(self, record_id: str) -> BackupGateRecord: ...

    def read_deployment_record(self, record_id: str) -> DeploymentRecord: ...

    def write_promotion_record(self, record: PromotionRecord) -> object: ...

    def write_environment_inventory(self, record: EnvironmentInventory) -> object: ...


class GenericWebProdPromotionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    artifact_id: str = ""
    source_git_ref: str = ""
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
    release_tag: str = ""

    @model_validator(mode="after")
    def _validate_request(self) -> "GenericWebProdPromotionRequest":
        self.product = self.product.strip()
        self.artifact_id = self.artifact_id.strip()
        self.source_git_ref = self.source_git_ref.strip()
        self.from_instance = self.from_instance.strip().lower()
        self.to_instance = self.to_instance.strip().lower()
        self.backup_record_id = self.backup_record_id.strip()
        self.release_tag = self.release_tag.strip()
        if not self.product:
            raise ValueError("generic web prod promotion requires product")
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
    source_git_ref: str = ""
    backup_record_id: str = ""
    promotion_record_id: str
    deployment_record_id: str = ""
    inventory_record_id: str = ""
    promotion_status: Literal["pending", "pass", "fail"]
    deployment_status: ReleaseStatus = "skipped"
    backup_status: ReleaseStatus = "skipped"
    source_health_status: ReleaseStatus = "skipped"
    destination_health_status: ReleaseStatus = "skipped"
    release_status: ReleaseStatus = "skipped"
    release_tag: str = ""
    release_url: str = ""
    target_name: str = ""
    target_id: str = ""
    target_category: DeployTargetCategory = "unknown"
    provider_id: str = ""
    provider_target_type: str = ""
    dry_run: bool = False
    error_message: str = ""

    @model_validator(mode="after")
    def _validate_result(self) -> "GenericWebProdPromotionResult":
        self.provider_id = self.provider_id.strip().lower()
        self.provider_target_type = self.provider_target_type.strip().lower()
        if self.target_category == "unknown":
            if self.provider_target_type == "application":
                self.target_category = "application"
            elif self.provider_target_type == "compose":
                self.target_category = "compose"
        if not self.provider_id and self.target_category in {"application", "compose"}:
            self.provider_id = "dokploy"
        return self


class GenericWebProdPromotionTargetResultFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_name: str
    target_id: str
    target_category: DeployTargetCategory
    provider_id: str
    provider_target_type: str
    target_type: str


def resolve_generic_web_promotion_lanes(
    *, record_store: GenericWebPromotionStore, request: GenericWebProdPromotionRequest
) -> tuple[LaunchplaneProductProfileRecord, ProductLaneProfile, ProductLaneProfile]:
    profile = record_store.read_product_profile_record(request.product)
    if not product_profile_uses_generic_web_base(profile):
        raise click.ClickException(
            f"Product {profile.product!r} is configured for driver {profile.driver_id!r}, "
            "not generic-web or a generic-web based driver."
        )
    source_lane = next(
        (lane for lane in profile.lanes if lane.instance == request.from_instance),
        None,
    )
    destination_lane = next(
        (lane for lane in profile.lanes if lane.instance == request.to_instance),
        None,
    )
    if source_lane is None:
        raise click.ClickException(
            f"Product {profile.product!r} has no generic-web lane for instance "
            f"{request.from_instance!r}."
        )
    if destination_lane is None:
        raise click.ClickException(
            f"Product {profile.product!r} has no generic-web lane for instance "
            f"{request.to_instance!r}."
        )
    source_lane = source_lane.model_copy(
        update={"context": source_lane.context.strip(), "instance": source_lane.instance.strip()}
    )
    destination_lane = destination_lane.model_copy(
        update={
            "context": destination_lane.context.strip(),
            "instance": destination_lane.instance.strip(),
        }
    )
    if source_lane.context != destination_lane.context:
        raise click.ClickException(
            "Generic web promotion currently requires source and destination lanes to share a context. "
            f"Resolved source={source_lane.context} destination={destination_lane.context}."
        )
    return profile, source_lane, destination_lane


def execute_generic_web_prod_promotion(
    *,
    control_plane_root: Path,
    record_store: GenericWebPromotionStore,
    request: GenericWebProdPromotionRequest,
) -> GenericWebProdPromotionResult:
    profile, source_lane, destination_lane = resolve_generic_web_promotion_lanes(
        record_store=record_store,
        request=request,
    )
    request = _resolve_source_inventory_inputs(
        record_store=record_store,
        profile=profile,
        request=request,
        source_lane=source_lane,
    )
    if request.release_tag:
        _preflight_github_release(
            control_plane_root=control_plane_root,
            profile=profile,
            context=destination_lane.context,
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
            source_git_ref=request.source_git_ref,
            backup_record_id=request.backup_record_id,
            promotion_record_id=promotion_record_id,
            promotion_status="pending",
            backup_status=backup_gate.status,
            source_health_status=source_health.status,
            destination_health_status=destination_health.status,
            release_status="pending" if request.release_tag else "skipped",
            release_tag=request.release_tag,
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
        profile=profile,
        lane=destination_lane,
    )
    deployment_record = _read_deployment_record(
        record_store=record_store,
        deployment_record_id=deploy_result.deployment_record_id,
    )
    if deploy_result.deploy_status == "pass":
        try:
            destination_health = _verify_health_evidence_with_identity(
                destination_health,
                expected_runtime_identity=deployment_record.runtime_identity,
            )
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
                target_type=_promotion_target_type(
                    deploy_result_target_type=(
                        deploy_result.provider_target_type or deploy_result.target_category
                    ),
                    deployment_record=deployment_record,
                ),
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
        target_type=_promotion_target_type(
            deploy_result_target_type=deploy_result.provider_target_type
            or deploy_result.target_category,
            deployment_record=deployment_record,
        ),
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
    final_result = _result_from_record(
        request=request,
        record=final_record,
        deployment_record=deployment_record,
        inventory_record_id=inventory_record_id,
        target_id=deploy_result.target_id,
        dry_run=False,
        error_message=deploy_result.error_message
        or _health_failure_detail(final_record.destination_health),
    )
    if final_result.promotion_status != "pass" or not request.release_tag:
        return final_result
    try:
        release_url = _create_or_verify_github_release(
            control_plane_root=control_plane_root,
            profile=profile,
            context=destination_lane.context,
            request=request,
            promotion_record_id=promotion_record_id,
            deployment_record_id=deploy_result.deployment_record_id,
            inventory_record_id=inventory_record_id,
        )
    except click.ClickException as error:
        return final_result.model_copy(
            update={
                "release_status": "fail",
                "release_tag": request.release_tag,
                "error_message": str(error),
            }
        )
    return final_result.model_copy(
        update={
            "release_status": "pass",
            "release_tag": request.release_tag,
            "release_url": release_url,
        }
    )


def _resolve_backup_gate(
    *, record_store: GenericWebPromotionStore, request: GenericWebProdPromotionRequest, context: str
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


def _resolve_source_inventory_inputs(
    *,
    record_store: GenericWebPromotionStore,
    profile: LaunchplaneProductProfileRecord,
    request: GenericWebProdPromotionRequest,
    source_lane: ProductLaneProfile,
) -> GenericWebProdPromotionRequest:
    try:
        source_inventory = record_store.read_environment_inventory(
            context_name=source_lane.context,
            instance_name=source_lane.instance,
        )
    except FileNotFoundError as exc:
        raise click.ClickException(
            "Generic web prod promotion requires current source environment inventory. "
            f"No inventory was found for {source_lane.context}/{source_lane.instance}."
        ) from exc
    inventory_artifact_id = ""
    if source_inventory.artifact_identity is not None:
        inventory_artifact_id = source_inventory.artifact_identity.artifact_id.strip()
    inventory_source_git_ref = source_inventory.source_git_ref.strip()
    if not inventory_artifact_id or not inventory_source_git_ref:
        raise click.ClickException(
            "Generic web prod promotion requires source inventory with artifact identity "
            "and source git ref."
        )
    normalized_inventory_artifact_id = normalize_generic_web_artifact_id(
        profile=profile,
        artifact_id=inventory_artifact_id,
    )
    requested_artifact_id = ""
    if request.artifact_id:
        requested_artifact_id = normalize_generic_web_artifact_id(
            profile=profile,
            artifact_id=request.artifact_id,
        )
    artifact_matches = not requested_artifact_id or (
        requested_artifact_id == normalized_inventory_artifact_id
    )
    source_matches = not request.source_git_ref or _revisions_match(
        request.source_git_ref,
        inventory_source_git_ref,
    )
    if not artifact_matches or not source_matches:
        raise click.ClickException(
            "Generic web prod promotion request does not match current source inventory. "
            f"Inventory artifact={normalized_inventory_artifact_id} "
            f"source_ref={inventory_source_git_ref}; "
            f"request artifact={requested_artifact_id or '<default>'} "
            f"source_ref={request.source_git_ref or '<default>'}."
        )
    return request.model_copy(
        update={
            "artifact_id": normalized_inventory_artifact_id,
            "source_git_ref": inventory_source_git_ref,
        }
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
    wait_for_healthcheck_with_retry(
        url=url,
        timeout_seconds=timeout_seconds,
        sleep=time.sleep,
        monotonic=time.monotonic,
    )


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
        except (TimeoutError, OSError) as error:
            healthcheck_errors.append(str(error))
    raise click.ClickException(
        "Healthcheck verification failed for all generic web URLs:\n"
        + "\n".join(healthcheck_errors)
    )


def _verify_health_evidence_with_identity(
    evidence: HealthcheckEvidence,
    *,
    expected_runtime_identity: RuntimeIdentity | None,
) -> HealthcheckEvidence:
    if evidence.status == "skipped" or not evidence.urls:
        return evidence
    if expected_runtime_identity is None:
        return _verify_health_evidence(evidence)
    timeout_seconds = evidence.timeout_seconds or DEFAULT_GENERIC_WEB_HEALTH_TIMEOUT_SECONDS
    healthcheck_errors: list[str] = []
    for health_url in evidence.urls:
        try:
            healthcheck_pass = wait_for_runtime_identity_healthcheck_with_retry(
                url=health_url,
                timeout_seconds=timeout_seconds,
                expected_runtime_identity=expected_runtime_identity,
                sleep=time.sleep,
                monotonic=time.monotonic,
            )
            verified_evidence = healthcheck_evidence_with_runtime_identity(
                HealthcheckEvidence(
                    verified=True,
                    urls=evidence.urls,
                    timeout_seconds=timeout_seconds,
                    status="pass",
                ),
                expected_runtime_identity=expected_runtime_identity,
                healthcheck_pass=healthcheck_pass,
            )
            if verified_evidence.runtime_identity_status != "match":
                return verified_evidence.model_copy(update={"status": "fail"})
            return verified_evidence
        except RuntimeIdentityHealthcheckError as error:
            healthcheck_errors.append(str(error))
            if error.healthcheck_pass is not None:
                return healthcheck_evidence_with_runtime_identity(
                    HealthcheckEvidence(
                        verified=True,
                        urls=evidence.urls,
                        timeout_seconds=timeout_seconds,
                        status="fail",
                    ),
                    expected_runtime_identity=expected_runtime_identity,
                    healthcheck_pass=error.healthcheck_pass,
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
    return evidence.model_copy(
        update={
            "verified": bool(evidence.urls),
            "status": "fail",
        }
    )


def _health_failure_detail(evidence: HealthcheckEvidence) -> str:
    if evidence.status != "fail":
        return ""
    return evidence.runtime_identity_detail


def _mark_health_skipped(evidence: HealthcheckEvidence) -> HealthcheckEvidence:
    return HealthcheckEvidence(
        urls=evidence.urls,
        timeout_seconds=evidence.timeout_seconds,
        status="skipped",
    )


def _read_deployment_record(
    *, record_store: GenericWebPromotionStore, deployment_record_id: str
) -> DeploymentRecord:
    try:
        return record_store.read_deployment_record(deployment_record_id)
    except FileNotFoundError as exc:
        raise click.ClickException(
            f"Generic web prod promotion could not read deployment record '{deployment_record_id}'."
        ) from exc


def _write_deployment_health(
    *,
    record_store: GenericWebPromotionStore,
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


def _promotion_target_type(
    *, deploy_result_target_type: str, deployment_record: DeploymentRecord | None
) -> DokployTargetType:
    if deployment_record is not None:
        return deployment_record.deploy.target_type
    if deploy_result_target_type in {"compose", "application"}:
        return cast(DokployTargetType, deploy_result_target_type)
    return "application"


def _result_target_fields(
    *,
    record: PromotionRecord,
    deployment_record: DeploymentRecord | None,
    target_id: str,
) -> GenericWebProdPromotionTargetResultFields:
    target_category: DeployTargetCategory = record.deploy.target_category or "unknown"
    provider_id = record.deploy.provider_id
    provider_target_type = str(record.deploy.target_type)
    if deployment_record is not None and deployment_record.deployed_target is not None:
        deployed_target = deployment_record.deployed_target
        target_category = deployed_target.target_category
        provider_id = deployed_target.provider_id
        provider_target_type = deployed_target.provider_target_type
        target_id = deployed_target.target_id
    if not provider_target_type:
        provider_target_type = record.deploy.target_type
    return GenericWebProdPromotionTargetResultFields(
        target_name=record.deploy.target_name,
        target_id=target_id,
        target_category=target_category,
        provider_id=provider_id,
        provider_target_type=provider_target_type,
        target_type=provider_target_type or target_category,
    )


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
    target_type: DokployTargetType,
    deployment_record_id: str,
) -> PromotionRecord:
    target_deploy_mode = "dokploy-application-api"
    target_deployment_id = "control-plane-dokploy"
    provider_id = "dokploy"
    target_category: DeployTargetCategory = target_type
    provider_target_type = str(target_type)
    provider_deploy_mode = target_deploy_mode
    resolved_target_name = target_name or _fallback_target_name(
        request=request,
        lane=ProductLaneProfile(instance=request.to_instance, context=context),
    )
    if deployment_record is not None:
        target_deploy_mode = deployment_record.deploy.deploy_mode
        target_deployment_id = deployment_record.deploy.deployment_id
        resolved_target_name = deployment_record.deploy.target_name
        target_type = deployment_record.deploy.target_type
        provider_id = deployment_record.deploy.provider_id
        target_category = deployment_record.deploy.target_category or target_type
        provider_target_type = deployment_record.deploy.provider_target_type
        provider_deploy_mode = deployment_record.deploy.provider_deploy_mode
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
            provider_id=provider_id,
            target_category=target_category,
            provider_target_type=provider_target_type,
            provider_deploy_mode=provider_deploy_mode,
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


def _create_or_verify_github_release(
    *,
    control_plane_root: Path,
    profile: LaunchplaneProductProfileRecord,
    context: str,
    request: GenericWebProdPromotionRequest,
    promotion_record_id: str,
    deployment_record_id: str,
    inventory_record_id: str,
) -> str:
    owner, repo = _repository_parts(profile.repository)
    token = resolve_launchplane_github_token(
        control_plane_root=control_plane_root,
        context_name=context,
    )
    if not token:
        raise click.ClickException(
            f"Generic web prod promotion requires GitHub token for context '{context}' to create release '{request.release_tag}'."
        )
    tag_target_sha = _github_tag_target_sha(
        owner=owner,
        repo=repo,
        release_tag=request.release_tag,
        token=token,
    )
    if tag_target_sha and not _revisions_match(tag_target_sha, request.source_git_ref):
        raise click.ClickException(
            f"GitHub tag '{request.release_tag}' points at '{tag_target_sha}', not promoted revision '{request.source_git_ref}'."
        )
    existing_release = _github_release_for_tag(
        owner=owner,
        repo=repo,
        release_tag=request.release_tag,
        token=token,
    )
    if existing_release is not None:
        return _release_html_url(existing_release)

    release_payload = github_api_request(
        path=f"/repos/{owner}/{repo}/releases",
        token=token,
        method="POST",
        body={
            "tag_name": request.release_tag,
            "target_commitish": request.source_git_ref,
            "name": request.release_tag,
            "body": _github_release_body(
                request=request,
                promotion_record_id=promotion_record_id,
                deployment_record_id=deployment_record_id,
                inventory_record_id=inventory_record_id,
            ),
            "draft": False,
            "prerelease": False,
        },
    )
    if not isinstance(release_payload, dict):
        raise click.ClickException(
            f"GitHub release create response for {owner}/{repo} {request.release_tag} must be an object."
        )
    return _release_html_url(release_payload)


def _preflight_github_release(
    *,
    control_plane_root: Path,
    profile: LaunchplaneProductProfileRecord,
    context: str,
    request: GenericWebProdPromotionRequest,
) -> None:
    owner, repo = _repository_parts(profile.repository)
    token = resolve_launchplane_github_token(
        control_plane_root=control_plane_root,
        context_name=context,
    )
    if not token:
        raise click.ClickException(
            f"Generic web prod promotion requires GitHub token for context '{context}' "
            f"to validate release '{request.release_tag}'."
        )
    tag_target_sha = _github_tag_target_sha(
        owner=owner,
        repo=repo,
        release_tag=request.release_tag,
        token=token,
    )
    if tag_target_sha and not _revisions_match(tag_target_sha, request.source_git_ref):
        raise click.ClickException(
            f"GitHub tag '{request.release_tag}' points at '{tag_target_sha}', "
            f"not promoted revision '{request.source_git_ref}'."
        )
    _github_release_for_tag(
        owner=owner,
        repo=repo,
        release_tag=request.release_tag,
        token=token,
    )


def _repository_parts(repository: str) -> tuple[str, str]:
    owner, separator, repo = repository.strip().partition("/")
    if not owner or separator != "/" or not repo:
        raise click.ClickException(
            f"Generic web prod promotion requires product repository in OWNER/REPO form, got {repository!r}."
        )
    return owner, repo


def _github_release_for_tag(
    *, owner: str, repo: str, release_tag: str, token: str
) -> dict[str, object] | None:
    try:
        payload = github_api_request(
            path=f"/repos/{owner}/{repo}/releases/tags/{quote(release_tag, safe='')}",
            token=token,
        )
    except click.ClickException as error:
        if _github_not_found(error):
            return None
        raise
    if not isinstance(payload, dict):
        raise click.ClickException(
            f"GitHub release lookup response for {owner}/{repo} {release_tag} must be an object."
        )
    return payload


def _github_tag_target_sha(*, owner: str, repo: str, release_tag: str, token: str) -> str:
    try:
        payload = github_api_request(
            path=f"/repos/{owner}/{repo}/git/ref/tags/{quote(release_tag, safe='')}",
            token=token,
        )
    except click.ClickException as error:
        if _github_not_found(error):
            return ""
        raise
    if not isinstance(payload, dict):
        raise click.ClickException(
            f"GitHub tag lookup response for {owner}/{repo} {release_tag} must be an object."
        )
    target = _github_ref_object_sha(payload)
    if _github_ref_object_type(payload) != "tag":
        return target
    annotated_payload = github_api_request(
        path=f"/repos/{owner}/{repo}/git/tags/{quote(target, safe='')}",
        token=token,
    )
    if not isinstance(annotated_payload, dict):
        raise click.ClickException(
            f"GitHub annotated tag response for {owner}/{repo} {release_tag} must be an object."
        )
    return _github_ref_object_sha(annotated_payload)


def _github_ref_object_sha(payload: dict[str, object]) -> str:
    obj = payload.get("object")
    if not isinstance(obj, dict):
        raise click.ClickException("GitHub tag response is missing object data.")
    sha = obj.get("sha")
    if not isinstance(sha, str) or not sha.strip():
        raise click.ClickException("GitHub tag response is missing object sha.")
    return sha.strip()


def _github_ref_object_type(payload: dict[str, object]) -> str:
    obj = payload.get("object")
    if not isinstance(obj, dict):
        raise click.ClickException("GitHub tag response is missing object data.")
    object_type = obj.get("type")
    return object_type.strip() if isinstance(object_type, str) else ""


def _github_not_found(error: click.ClickException) -> bool:
    message = str(error)
    return "HTTP Error 404" in message or "404: Not Found" in message


def _revisions_match(left: str, right: str) -> bool:
    normalized_left = left.strip()
    normalized_right = right.strip()
    if not normalized_left or not normalized_right:
        return False
    return (
        normalized_left == normalized_right
        or (len(normalized_left) >= 7 and normalized_right.startswith(normalized_left))
        or (len(normalized_right) >= 7 and normalized_left.startswith(normalized_right))
    )


def _release_html_url(payload: dict[str, object]) -> str:
    html_url = payload.get("html_url")
    return html_url.strip() if isinstance(html_url, str) else ""


def _github_release_body(
    *,
    request: GenericWebProdPromotionRequest,
    promotion_record_id: str,
    deployment_record_id: str,
    inventory_record_id: str,
) -> str:
    return "\n".join(
        (
            f"Promoted {request.product} to {request.to_instance}.",
            "",
            f"- Artifact: `{request.artifact_id}`",
            f"- Source git ref: `{request.source_git_ref}`",
            f"- Promotion record: `{promotion_record_id}`",
            f"- Deployment record: `{deployment_record_id}`",
            f"- Inventory record: `{inventory_record_id}`",
        )
    )


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
    target_fields = _result_target_fields(
        record=record,
        deployment_record=deployment_record,
        target_id=target_id,
    )
    return GenericWebProdPromotionResult(
        product=request.product,
        context=record.context,
        from_instance=record.from_instance,
        to_instance=record.to_instance,
        artifact_id=record.artifact_identity.artifact_id,
        source_git_ref=request.source_git_ref,
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
        release_tag=request.release_tag,
        target_name=target_fields.target_name,
        target_id=target_fields.target_id,
        target_category=target_fields.target_category,
        provider_id=target_fields.provider_id,
        provider_target_type=target_fields.provider_target_type,
        dry_run=dry_run,
        error_message=error_message,
    )
