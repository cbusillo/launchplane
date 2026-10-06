from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

import click

from control_plane.contracts.odoo_target_replacement_failures import (
    OdooProviderEffectUncertainError,
)
from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.deployment_record import (
    DeploymentRecord,
    previous_passing_deployment,
)
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.odoo_prod_rollback_operation import (
    OdooProdRollbackRequest as OdooProdRollbackRequest,
    OdooProdRollbackResult as OdooProdRollbackResult,
    OdooProdRollbackTarget,
)
from control_plane.contracts.promotion_record import (
    HealthcheckEvidence,
    PromotionRecord,
    ReleaseStatus,
    RollbackExecutionEvidence,
)
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
    def list_deployment_records(
        self,
        *,
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[DeploymentRecord, ...]: ...

    def read_artifact_manifest(self, artifact_id: str) -> ArtifactIdentityManifest: ...

    def read_environment_inventory(
        self, *, context_name: str, instance_name: str
    ) -> EnvironmentInventory: ...

    def read_promotion_record(self, promotion_record_id: str) -> PromotionRecord: ...

    def write_deployment_record(self, record: DeploymentRecord) -> None: ...

    def read_deployment_record(self, record_id: str) -> DeploymentRecord: ...

    def write_environment_inventory(self, record: EnvironmentInventory) -> object: ...

    def write_promotion_record(self, record: PromotionRecord) -> None: ...


def _require_record_store(record_store: object) -> OdooProdRollbackStore:
    required_methods = (
        "list_deployment_records",
        "read_artifact_manifest",
        "read_environment_inventory",
        "read_promotion_record",
        "read_product_profile_record",
        "read_dokploy_target_record",
        "read_dokploy_target_id_record",
        "write_deployment_record",
        "read_deployment_record",
        "write_environment_inventory",
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


class OdooProdRollbackTargetMissingError(click.ClickException):
    """No earlier passing deployment exists to roll back to."""


def _read_previous_prod_deployment(
    *,
    record_store: OdooProdRollbackStore,
    request: OdooProdRollbackRequest,
) -> tuple[DeploymentRecord, str]:
    previous_deployment = previous_passing_deployment(
        record_store.list_deployment_records(
            context_name=request.context,
            instance_name=request.instance,
        )
    )
    if previous_deployment is None or previous_deployment.artifact_identity is None:
        raise OdooProdRollbackTargetMissingError(
            f"Odoo prod rollback found no earlier passing {request.context}/{request.instance} "
            "deployment of a different artifact. Pass artifact_id to choose one."
        )
    return previous_deployment, previous_deployment.artifact_identity.artifact_id


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
    previous_deployment: DeploymentRecord | None,
) -> _RollbackSource:
    if previous_deployment is not None:
        return _RollbackSource(
            artifact_id=artifact_manifest.artifact_id,
            source_git_ref=previous_deployment.source_git_ref,
            result_source_channel="previous-deployment",
            promoted_from_instance="previous-deployment",
            snapshot_name=f"deployment:{previous_deployment.record_id}",
            detail=(
                f"Rolled {request.context}/{request.instance} back to artifact "
                f"{artifact_manifest.artifact_id} from passing deployment "
                f"{previous_deployment.record_id}."
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
    health_evidence: HealthcheckEvidence | None = None,
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
            "rollback_health": health_evidence
            or HealthcheckEvidence(
                verified=False,
                status=health_status,
            ),
        }
    )
    record_store.write_promotion_record(updated_record)
    return updated_record


def resolve_odoo_prod_rollback_target(
    *,
    record_store: object,
    request: OdooProdRollbackRequest,
) -> OdooProdRollbackTarget:
    """Resolve the artifact a rollback will redeploy, without any effect.

    The explicit ``artifact_id`` wins; otherwise it is the previous passing prod
    deployment's artifact. The artifact manifest and the current promotion record
    must exist.
    """

    typed_record_store = _require_record_store(record_store)
    if request.artifact_id:
        target = OdooProdRollbackTarget(artifact_id=request.artifact_id)
    else:
        previous_deployment, artifact_id = _read_previous_prod_deployment(
            record_store=typed_record_store, request=request
        )
        target = OdooProdRollbackTarget(
            artifact_id=artifact_id, deployment_record_id=previous_deployment.record_id
        )
    _read_artifact_manifest(record_store=typed_record_store, artifact_id=target.artifact_id)
    _resolve_promotion_record(record_store=typed_record_store, request=request)
    return target


def _read_pinned_previous_deployment(
    *,
    record_store: OdooProdRollbackStore,
    target: OdooProdRollbackTarget,
) -> DeploymentRecord | None:
    if not target.deployment_record_id:
        return None
    try:
        deployment = record_store.read_deployment_record(target.deployment_record_id)
    except FileNotFoundError as exc:
        raise click.ClickException(
            f"Odoo prod rollback target deployment {target.deployment_record_id!r} is missing."
        ) from exc
    if (
        deployment.artifact_identity is None
        or deployment.artifact_identity.artifact_id != target.artifact_id
    ):
        raise click.ClickException(
            "Odoo prod rollback target deployment no longer names the recorded artifact."
        )
    return deployment


def execute_odoo_prod_rollback(
    *,
    control_plane_root: Path,
    record_store: object,
    product: str,
    request: OdooProdRollbackRequest,
    target: OdooProdRollbackTarget | None = None,
    provider_effect_checkpoint: Callable[[str], None] | None = None,
    hold_uncertain_effects: bool = False,
) -> OdooProdRollbackResult:
    """Roll prod back; a queued rollback passes the ``target`` it fixed at enqueue."""

    normalized_product = product.strip()
    if not normalized_product or normalized_product == "odoo":
        raise click.ClickException("Odoo prod rollback requires a DB-backed product profile key.")
    typed_record_store = _require_record_store(record_store)
    previous_deployment: DeploymentRecord | None = None
    artifact_id = request.artifact_id
    if target is not None:
        artifact_id = target.artifact_id
        previous_deployment = _read_pinned_previous_deployment(
            record_store=typed_record_store, target=target
        )
    elif not artifact_id:
        previous_deployment, artifact_id = _read_previous_prod_deployment(
            record_store=typed_record_store, request=request
        )
    artifact_manifest = _read_artifact_manifest(
        record_store=typed_record_store,
        artifact_id=artifact_id,
    )
    rollback_source = _resolve_rollback_source(
        request=request,
        artifact_manifest=artifact_manifest,
        previous_deployment=previous_deployment,
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

    provider_effect_started = False

    def before_provider_effect(effect_name: str) -> None:
        nonlocal provider_effect_started
        if provider_effect_checkpoint is not None:
            provider_effect_checkpoint(effect_name)
        provider_effect_started = True

    replacement_result = None
    health_status: Literal["pass", "fail", "skipped"] = (
        "fail" if request.verify_health else "skipped"
    )
    try:
        replacement_result = execute_odoo_stable_target_replacement_apply(
            control_plane_root=control_plane_root,
            record_store=typed_record_store,
            request=OdooStableTargetReplacementApplyRequest(
                product=normalized_product,
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
            provider_effect_checkpoint=before_provider_effect,
            hold_uncertain_effects=hold_uncertain_effects,
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
        if hold_uncertain_effects and provider_effect_started and isinstance(error, OSError):
            raise OdooProviderEffectUncertainError(
                "Odoo recovery provider effect requires reconciliation."
            ) from error
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
        health_evidence=deployment_record.destination_health,
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
