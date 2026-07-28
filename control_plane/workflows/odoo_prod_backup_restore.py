from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol
from urllib.parse import urlparse

import click

from control_plane import live_target_runtime as control_plane_live_target_runtime
from control_plane import release_tuples as control_plane_release_tuples
from control_plane import runtime_environments as control_plane_runtime_environments
from control_plane.contracts.artifact_identity import (
    ArtifactIdentityManifest,
    artifact_manifest_matches_image_repository,
)
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.deployment_record import DeploymentRecord, ResolvedTargetEvidence
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.odoo_prod_backup_restore import (
    OdooProdBackupRestoreApplyRequest,
    OdooProdBackupRestorePlan,
    OdooProdBackupRestoreRequest,
    OdooProdBackupRestoreResult,
    OdooProdBackupRestoreStep,
    build_odoo_prod_backup_restore_plan_fingerprint,
)
from control_plane.contracts.odoo_prod_backup_restore_operation import (
    OdooProdBackupRestoreOperationPhase,
    OdooProdBackupRestoreOperationRecord,
    odoo_prod_backup_restore_operation_is_verification_replay_claim,
)
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.contracts.promotion_record import HealthcheckEvidence, PostDeployUpdateEvidence
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.runtime_identity import (
    RuntimeIdentity,
    parse_runtime_identity_payload,
    runtime_identity_env,
)
from control_plane.contracts.ship_request import ShipRequest
from control_plane.dokploy import api as dokploy_api
from control_plane.dokploy import post_deploy as dokploy_post_deploy
from control_plane.dokploy import source as dokploy_source
from control_plane.durable_operation_authorization import DurableOperationAuthorizationDeniedError
from control_plane.runtime_key_safety import RuntimeKeySafetyPolicyReadStore
from control_plane.workflows.inventory import build_environment_inventory
from control_plane.workflows.odoo_post_deploy import OdooPostDeployRequest, execute_odoo_post_deploy
from control_plane.workflows.odoo_prod_backup_gate import (
    BACKUP_VERIFICATION_SOURCE,
    VERIFIABLE_BACKUP_GATE_SOURCES,
)
from control_plane.workflows.odoo_verification import (
    OdooVerificationEvidence,
    default_odoo_health_url,
    is_legacy_derived_odoo_health_url,
    verify_odoo_stable_readiness,
)
from control_plane.workflows.runtime_identity_health import (
    RuntimeIdentityHealthcheckError,
    healthcheck_evidence_with_runtime_identity,
    wait_for_runtime_identity_healthcheck_with_retry,
)
from control_plane.workflows.ship import (
    build_deployment_record,
    generate_deployment_record_id,
    utc_now_timestamp,
)


ODOO_PROD_BACKUP_RESTORE_VERIFY_RETRY_INTERVAL_SECONDS = 5
ODOO_PROD_BACKUP_RESTORE_VOLUME_KEYS = (
    "ODOO_DATA_VOLUME",
    "ODOO_LOG_VOLUME",
    "ODOO_DB_VOLUME",
)


class OdooProdBackupRestoreStore(RuntimeKeySafetyPolicyReadStore, Protocol):
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

    def read_deployment_record(self, record_id: str) -> DeploymentRecord: ...

    def read_artifact_manifest(self, artifact_id: str) -> ArtifactIdentityManifest: ...

    def read_backup_gate_record(self, record_id: str) -> BackupGateRecord: ...

    def list_runtime_environment_records(
        self, *, context_name: str = "", instance_name: str = ""
    ) -> tuple[RuntimeEnvironmentRecord, ...]: ...

    def write_deployment_record(self, record: DeploymentRecord) -> object: ...

    def write_environment_inventory(self, record: EnvironmentInventory) -> object: ...

    def write_release_tuple_record(self, record: ReleaseTupleRecord) -> object: ...


RestorePhaseCheckpoint = Callable[[OdooProdBackupRestoreOperationPhase, dict[str, str]], None]
RestoreProviderEffectCheckpoint = Callable[[OdooProdBackupRestoreOperationPhase, str], None]


def build_odoo_prod_backup_restore_plan(
    *,
    control_plane_root: Path,
    record_store: OdooProdBackupRestoreStore,
    request: OdooProdBackupRestoreRequest,
) -> OdooProdBackupRestorePlan:
    profile = _read_product_profile(record_store=record_store, product=request.product)
    lane = _read_prod_lane(profile=profile, context=request.context)
    target_record = _read_target_record(
        record_store=record_store,
        context=request.context,
        instance=request.instance,
    )
    target_id_record = _read_target_id_record(
        record_store=record_store,
        context=request.context,
        instance=request.instance,
    )
    inventory = _read_inventory(
        record_store=record_store,
        context=request.context,
        instance=request.instance,
    )
    blockers: list[str] = []
    warnings: list[str] = []

    if target_record.target_type != "compose":
        blockers.append("Odoo production backup restore requires a compose target.")

    runtime_values = _runtime_values_from_store(
        record_store=record_store,
        target_record=target_record,
        context=request.context,
        instance=request.instance,
    )
    database_name = runtime_values.get("ODOO_DB_NAME", "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", database_name):
        blockers.append("DB-backed ODOO_DB_NAME is missing or path-unsafe.")
    if database_name in {"postgres", "template0", "template1"}:
        blockers.append(
            "Odoo production backup restore cannot target a PostgreSQL system database."
        )
    filestore_path = _normalized_absolute_path(
        runtime_values.get("ODOO_FILESTORE_PATH", "") or "/volumes/data/filestore",
        label="ODOO_FILESTORE_PATH",
        blockers=blockers,
    )
    backup_root = _normalized_absolute_path(
        runtime_values.get("ODOO_BACKUP_ROOT", ""),
        label="ODOO_BACKUP_ROOT",
        blockers=blockers,
    )
    expected_paths = _backup_paths(
        backup_root=backup_root,
        database_name=database_name,
        backup_record_id=request.backup_record_id,
        filestore_path=filestore_path,
    )

    desired_volumes = {
        key: runtime_values.get(key, "").strip() for key in ODOO_PROD_BACKUP_RESTORE_VOLUME_KEYS
    }
    if desired_volumes["ODOO_DATA_VOLUME"] != request.expected_data_volume:
        blockers.append("DB-backed ODOO_DATA_VOLUME does not match the reviewed request.")
    if desired_volumes["ODOO_LOG_VOLUME"] != request.expected_log_volume:
        blockers.append("DB-backed ODOO_LOG_VOLUME does not match the reviewed request.")
    if desired_volumes["ODOO_DB_VOLUME"] != request.expected_new_db_volume:
        blockers.append(
            "DB-backed ODOO_DB_VOLUME must name the reviewed fresh restore volume before planning."
        )

    try:
        host, token = dokploy_source.read_dokploy_config(control_plane_root=control_plane_root)
        target_payload = dokploy_api.fetch_dokploy_target_payload(
            host=host,
            token=token,
            target_type="compose",
            target_id=target_id_record.target_id,
        )
    except (click.ClickException, OSError) as error:
        raise click.ClickException(
            "Odoo production backup restore could not read exact live target evidence."
        ) from error
    live_env = dokploy_api.parse_dokploy_env_text(str(target_payload.get("env") or ""))
    live_volumes = {
        key: live_env.get(key, "").strip() for key in ODOO_PROD_BACKUP_RESTORE_VOLUME_KEYS
    }
    if live_volumes["ODOO_DB_VOLUME"] != request.expected_old_db_volume:
        blockers.append("Live ODOO_DB_VOLUME does not match the reviewed corrupt volume.")
    if live_volumes["ODOO_DATA_VOLUME"] != request.expected_data_volume:
        blockers.append("Live ODOO_DATA_VOLUME drifted from DB-backed restore authority.")
    if live_volumes["ODOO_LOG_VOLUME"] != request.expected_log_volume:
        blockers.append("Live ODOO_LOG_VOLUME drifted from DB-backed restore authority.")
    if live_volumes["ODOO_DB_VOLUME"] == desired_volumes["ODOO_DB_VOLUME"]:
        blockers.append(
            "Odoo production backup restore requires intentional old-to-new DB-volume drift."
        )

    current_artifact_id = (
        inventory.artifact_identity.artifact_id if inventory.artifact_identity else ""
    )
    if current_artifact_id != request.expected_current_artifact_id:
        blockers.append("Current inventory artifact changed after restore review.")
    source_git_ref = inventory.source_git_ref.strip()
    if not source_git_ref:
        blockers.append("Current inventory does not include exact source git evidence.")
    try:
        artifact_manifest = record_store.read_artifact_manifest(
            request.expected_current_artifact_id
        )
    except FileNotFoundError:
        artifact_manifest = None
        blockers.append("Current artifact manifest was not found.")
    image_reference = ""
    if artifact_manifest is not None:
        if not artifact_manifest_matches_image_repository(
            artifact_manifest,
            expected_repository=profile.image.repository,
        ):
            blockers.append("Current artifact image repository does not match product authority.")
        if artifact_manifest.source_commit != source_git_ref:
            blockers.append("Current artifact source ref does not match environment inventory.")
        image_reference = _artifact_image_reference(artifact_manifest)
        live_image_reference = live_env.get("DOCKER_IMAGE_REFERENCE", "").strip()
        if live_image_reference and live_image_reference != image_reference:
            blockers.append("Live target image reference differs from current artifact authority.")

    backup_record = _read_backup_record(
        record_store=record_store,
        record_id=request.backup_record_id,
        sources=VERIFIABLE_BACKUP_GATE_SOURCES,
        context=request.context,
        instance=request.instance,
        label="backup gate",
        blockers=blockers,
    )
    if backup_record is not None:
        _validate_backup_gate_paths(
            record=backup_record,
            expected_paths=expected_paths,
            blockers=blockers,
        )
    verification_record = _read_backup_record(
        record_store=record_store,
        record_id=request.verification_record_id,
        sources=frozenset({BACKUP_VERIFICATION_SOURCE}),
        context=request.context,
        instance=request.instance,
        label="backup verification",
        blockers=blockers,
    )
    verification_evidence = _verification_evidence(
        record=verification_record,
        backup_record=backup_record,
        expected_paths=expected_paths,
        blockers=blockers,
    )

    base_url = lane.base_url.strip().rstrip("/")
    parsed_base_url = urlparse(base_url)
    expected_domain_hosts = tuple(
        sorted({domain.strip().lower() for domain in target_record.domains if domain.strip()})
    )
    if parsed_base_url.scheme != "https" or not parsed_base_url.hostname:
        blockers.append("Production restore requires an HTTPS base_url in the product lane record.")
    elif parsed_base_url.hostname.lower() not in expected_domain_hosts:
        blockers.append("Production base_url host is not present in the DB-backed target domains.")
    if not expected_domain_hosts:
        blockers.append("Production restore requires DB-backed expected domains.")
    lane_health_url = lane.health_url.strip()
    health_url = (
        lane_health_url
        if lane_health_url
        and not is_legacy_derived_odoo_health_url(
            health_url=lane_health_url,
            base_url=base_url,
            profile_health_path=profile.health_path,
        )
        else default_odoo_health_url(base_url=base_url)
    )

    filestore_database_path = _filestore_database_path(
        filestore_path=filestore_path,
        database_name=database_name,
    )
    restore_suffix = hashlib.sha256(
        (
            request.backup_record_id
            + "\0"
            + request.verification_record_id
            + "\0"
            + request.expected_new_db_volume
        ).encode("utf-8")
    ).hexdigest()[:16]
    filestore_parent = PurePosixPath(filestore_database_path).parent
    filestore_staging_path = (
        filestore_parent / f".launchplane-restore-{database_name}-{restore_suffix}"
    ).as_posix()
    filestore_quarantine_path = (
        filestore_parent / f"{database_name}.quarantine-{restore_suffix}"
    ).as_posix()

    steps = _restore_steps(blocked=bool(blockers))
    plan_values = {
        "product": request.product,
        "context": request.context,
        "instance": request.instance,
        "backup_record_id": request.backup_record_id,
        "backup_record_created_at": backup_record.created_at if backup_record else "",
        "verification_record_id": request.verification_record_id,
        "verification_record_created_at": (
            verification_record.created_at if verification_record else ""
        ),
        "target_id": target_id_record.target_id,
        "target_name": target_record.target_name or str(target_payload.get("name") or "").strip(),
        "expected_current_artifact_id": request.expected_current_artifact_id,
        "expected_source_git_ref": source_git_ref,
        "image_reference": image_reference,
        "database_name": database_name,
        "database_dump_path": expected_paths.get("database_dump_path", ""),
        "filestore_archive_path": expected_paths.get("filestore_archive_path", ""),
        "manifest_path": expected_paths.get("manifest_path", ""),
        **verification_evidence,
        "old_db_volume": request.expected_old_db_volume,
        "new_db_volume": request.expected_new_db_volume,
        "data_volume": request.expected_data_volume,
        "log_volume": request.expected_log_volume,
        "filestore_path": filestore_path,
        "filestore_staging_path": filestore_staging_path,
        "filestore_quarantine_path": filestore_quarantine_path,
        "base_url": base_url,
        "health_url": health_url,
        "expected_domain_hosts": expected_domain_hosts,
        "warnings": tuple(warnings),
        "steps": steps,
    }
    if blockers:
        return OdooProdBackupRestorePlan.model_validate(
            {
                "plan_status": "blocked",
                "plan_fingerprint": "",
                "blockers": tuple(blockers),
                **plan_values,
            }
        )
    provisional_plan = OdooProdBackupRestorePlan.model_validate(
        {
            "plan_status": "blocked",
            "plan_fingerprint": "",
            "blockers": ("fingerprint-pending",),
            **plan_values,
        }
    )
    fingerprint = build_odoo_prod_backup_restore_plan_fingerprint(provisional_plan)
    return OdooProdBackupRestorePlan.model_validate(
        {
            "plan_status": "ready",
            "plan_fingerprint": fingerprint,
            "blockers": (),
            **plan_values,
        }
    )


def execute_odoo_prod_backup_restore_apply(
    *,
    control_plane_root: Path,
    record_store: OdooProdBackupRestoreStore,
    operation_id: str,
    request: OdooProdBackupRestoreApplyRequest,
    phase_checkpoint: RestorePhaseCheckpoint,
    provider_effect_checkpoint: RestoreProviderEffectCheckpoint,
) -> OdooProdBackupRestoreResult:
    plan = build_odoo_prod_backup_restore_plan(
        control_plane_root=control_plane_root,
        record_store=record_store,
        request=OdooProdBackupRestoreRequest.model_validate(
            request.model_dump(
                mode="json",
                exclude={
                    "plan_fingerprint",
                    "confirmation",
                    "no_cache",
                    "timeout_seconds",
                    "health_timeout_seconds",
                },
            )
        ),
    )
    if plan.plan_status != "ready":
        raise click.ClickException(
            "Odoo production backup restore apply requires a ready plan: "
            + "; ".join(plan.blockers)
        )
    if plan.plan_fingerprint != request.plan_fingerprint:
        raise click.ClickException(
            "Odoo production backup restore plan fingerprint changed before apply."
        )

    profile = _read_product_profile(record_store=record_store, product=request.product)
    _read_prod_lane(profile=profile, context=request.context)
    target_record = _read_target_record(
        record_store=record_store,
        context=request.context,
        instance=request.instance,
    )
    target_id_record = _read_target_id_record(
        record_store=record_store,
        context=request.context,
        instance=request.instance,
    )
    artifact_manifest = record_store.read_artifact_manifest(request.expected_current_artifact_id)
    target_definition = _target_definition(
        target_record=target_record,
        target_id_record=target_id_record,
    )
    deploy_timeout_seconds = (
        request.timeout_seconds
        or target_record.deploy_timeout_seconds
        or dokploy_source.DEFAULT_DOKPLOY_DEPLOY_TIMEOUT_SECONDS
    )
    health_timeout_seconds = (
        request.health_timeout_seconds
        or target_record.healthcheck_timeout_seconds
        or dokploy_source.DEFAULT_DOKPLOY_HEALTH_TIMEOUT_SECONDS
    )
    deployment_record_id = generate_deployment_record_id(
        context_name=plan.context,
        instance_name=plan.instance,
    )
    started_at = utc_now_timestamp()
    runtime_identity = RuntimeIdentity(
        product=plan.product,
        context=plan.context,
        instance=plan.instance,
        environment_kind="stable",
        deployment_record_id=deployment_record_id,
        artifact_id=plan.expected_current_artifact_id,
        source_git_ref=plan.expected_source_git_ref,
        image_reference=plan.image_reference,
    )
    ship_request = ShipRequest(
        artifact_id=plan.expected_current_artifact_id,
        context=plan.context,
        instance=plan.instance,
        source_git_ref=plan.expected_source_git_ref,
        target_name=plan.target_name,
        target_type="compose",
        provider_id="dokploy",
        target_category="compose",
        provider_target_type="compose",
        deploy_mode="dokploy-compose-api",
        wait=True,
        timeout_seconds=deploy_timeout_seconds,
        verify_health=True,
        health_timeout_seconds=health_timeout_seconds,
        no_cache=request.no_cache,
        destination_health=HealthcheckEvidence(status="pending"),
    )
    resolved_target = ResolvedTargetEvidence(
        target_type="compose",
        target_id=plan.target_id,
        target_name=plan.target_name,
    )
    runtime_source = {
        "restore_plan_fingerprint": plan.plan_fingerprint,
        "backup_record_id": plan.backup_record_id,
        "backup_verification_record_id": plan.verification_record_id,
        "database_dump_sha256": plan.database_dump_sha256,
        "filestore_archive_sha256": plan.filestore_archive_sha256,
        "old_db_volume": plan.old_db_volume,
        "new_db_volume": plan.new_db_volume,
        "data_volume": plan.data_volume,
        "log_volume": plan.log_volume,
        "filestore_quarantine_path": plan.filestore_quarantine_path,
    }
    record_store.write_deployment_record(
        build_deployment_record(
            request=ship_request,
            record_id=deployment_record_id,
            deployment_id="control-plane-dokploy",
            deployment_status="pending",
            started_at=started_at,
            finished_at="",
            resolved_target=resolved_target,
            runtime_source=runtime_source,
            runtime_identity=runtime_identity,
            destination_health=HealthcheckEvidence(status="pending"),
        )
    )

    phase_evidence: dict[str, dict[str, str]] = {}
    statuses: dict[str, str] = {
        "database_restore_status": "skipped",
        "filestore_stage_status": "skipped",
        "web_quiesce_status": "skipped",
        "filestore_activation_status": "skipped",
        "deployment_status": "skipped",
        "post_deploy_status": "skipped",
        "health_status": "skipped",
        "canonical_status": "skipped",
        "logo_status": "skipped",
        "runtime_identity_status": "skipped",
    }

    def result(
        *,
        restore_status: str,
        error_message: str = "",
        verification_evidence: OdooVerificationEvidence | None = None,
        release_tuple_id: str = "",
    ) -> OdooProdBackupRestoreResult:
        return OdooProdBackupRestoreResult.model_validate(
            {
                "product": plan.product,
                "context": plan.context,
                "instance": plan.instance,
                "backup_record_id": plan.backup_record_id,
                "verification_record_id": plan.verification_record_id,
                "plan_fingerprint": plan.plan_fingerprint,
                "deployment_record_id": deployment_record_id,
                "release_tuple_id": release_tuple_id,
                "restore_status": restore_status,
                "old_db_volume": plan.old_db_volume,
                "new_db_volume": plan.new_db_volume,
                "data_volume": plan.data_volume,
                "log_volume": plan.log_volume,
                "filestore_quarantine_path": plan.filestore_quarantine_path,
                "database_dump_sha256": plan.database_dump_sha256,
                "filestore_archive_sha256": plan.filestore_archive_sha256,
                "verification_evidence": verification_evidence or OdooVerificationEvidence(),
                "phase_evidence": phase_evidence,
                "error_message": error_message,
                **statuses,
            }
        )

    def provider_checkpoint(
        phase: OdooProdBackupRestoreOperationPhase,
    ) -> Callable[[str], None]:
        def checkpoint(effect_name: str) -> None:
            provider_effect_checkpoint(phase, effect_name)

        return checkpoint

    phase_checkpoint(
        "validated",
        {
            "plan_fingerprint": plan.plan_fingerprint,
            "backup_record_id": plan.backup_record_id,
            "verification_record_id": plan.verification_record_id,
        },
    )

    try:
        host, token = dokploy_source.read_dokploy_config(control_plane_root=control_plane_root)
        database_evidence = dokploy_post_deploy.run_compose_odoo_backup_restore_database(
            host=host,
            token=token,
            target_definition=target_definition,
            operation_id=operation_id,
            database_name=plan.database_name,
            database_dump_path=plan.database_dump_path,
            database_dump_sha256=plan.database_dump_sha256,
            old_db_volume=plan.old_db_volume,
            new_db_volume=plan.new_db_volume,
            data_volume=plan.data_volume,
            log_volume=plan.log_volume,
            timeout_seconds=deploy_timeout_seconds,
            before_provider_mutation=provider_checkpoint("database_restore_started"),
        )
        phase_evidence["database_restore"] = database_evidence
        statuses["database_restore_status"] = "pass"
        phase_checkpoint("database_restored", database_evidence)

        filestore_stage_evidence = (
            dokploy_post_deploy.run_compose_odoo_backup_restore_filestore_stage(
                host=host,
                token=token,
                target_definition=target_definition,
                operation_id=operation_id,
                database_name=plan.database_name,
                filestore_path=plan.filestore_path,
                filestore_archive_path=plan.filestore_archive_path,
                filestore_archive_sha256=plan.filestore_archive_sha256,
                filestore_member_count=plan.filestore_member_count,
                filestore_unpacked_size=plan.filestore_unpacked_size,
                filestore_staging_path=plan.filestore_staging_path,
                filestore_quarantine_path=plan.filestore_quarantine_path,
                data_volume=plan.data_volume,
                log_volume=plan.log_volume,
                timeout_seconds=deploy_timeout_seconds,
                before_provider_mutation=provider_checkpoint("filestore_stage_started"),
            )
        )
        phase_evidence["filestore_stage"] = filestore_stage_evidence
        statuses["filestore_stage_status"] = "pass"
        phase_checkpoint("filestore_staged", filestore_stage_evidence)

        quiesce_evidence = dokploy_post_deploy.run_compose_odoo_backup_restore_web_quiesce(
            host=host,
            token=token,
            target_definition=target_definition,
            operation_id=operation_id,
            old_db_volume=plan.old_db_volume,
            data_volume=plan.data_volume,
            log_volume=plan.log_volume,
            timeout_seconds=deploy_timeout_seconds,
            before_provider_mutation=provider_checkpoint("web_quiesce_started"),
        )
        phase_evidence["web_quiesce"] = quiesce_evidence
        statuses["web_quiesce_status"] = "pass"
        phase_checkpoint("web_quiesced", quiesce_evidence)

        activation_evidence = (
            dokploy_post_deploy.run_compose_odoo_backup_restore_filestore_activate(
                host=host,
                token=token,
                target_definition=target_definition,
                operation_id=operation_id,
                database_name=plan.database_name,
                filestore_path=plan.filestore_path,
                filestore_staging_path=plan.filestore_staging_path,
                filestore_quarantine_path=plan.filestore_quarantine_path,
                data_volume=plan.data_volume,
                log_volume=plan.log_volume,
                timeout_seconds=deploy_timeout_seconds,
                before_provider_mutation=provider_checkpoint("filestore_activate_started"),
            )
        )
        phase_evidence["filestore_activate"] = activation_evidence
        statuses["filestore_activation_status"] = "pass"
        phase_checkpoint("filestore_activated", activation_evidence)

        target_payload = dokploy_api.fetch_dokploy_target_payload(
            host=host,
            token=token,
            target_type="compose",
            target_id=target_id_record.target_id,
        )
        current_env = dokploy_api.parse_dokploy_env_text(str(target_payload.get("env") or ""))
        _require_exact_live_volumes(
            env=current_env,
            db_volume=plan.old_db_volume,
            data_volume=plan.data_volume,
            log_volume=plan.log_volume,
        )
        runtime_values = _runtime_values_from_store(
            record_store=record_store,
            target_record=target_record,
            context=plan.context,
            instance=plan.instance,
        )
        try:
            runtime_secret_binding_keys = (
                control_plane_live_target_runtime.require_product_profile_runtime_secret_keys(
                    record_store=record_store,
                    product_name=plan.product,
                    context_name=plan.context,
                    instance_name=plan.instance,
                )
            )
            runtime_key_safety = (
                control_plane_live_target_runtime.evaluate_runtime_key_safety_for_live_target_sync(
                    record_store=record_store,
                    context_name=plan.context,
                    instance_name=plan.instance,
                    required_binding_keys=tuple(sorted(runtime_secret_binding_keys)),
                )
            )
        except control_plane_live_target_runtime.LiveTargetRuntimeError as error:
            raise click.ClickException(
                control_plane_live_target_runtime.runtime_key_safety_error_message(error)
            ) from error
        runtime_source.update(
            {
                f"runtime_key_safety_{key}": str(value)
                for key, value in runtime_key_safety.items()
                if key in {"required", "status", "policy_record_id", "policy_sha256"}
            }
        )
        desired_env = dict(current_env)
        desired_env.update(runtime_values)
        desired_env["ODOO_DATA_VOLUME"] = plan.data_volume
        desired_env["ODOO_LOG_VOLUME"] = plan.log_volume
        desired_env["ODOO_DB_VOLUME"] = plan.new_db_volume
        desired_env["PLATFORM_CONTEXT"] = plan.context
        desired_env["PLATFORM_INSTANCE"] = plan.instance
        desired_env["DOCKER_IMAGE_REFERENCE"] = plan.image_reference
        desired_env.update(runtime_identity_env(runtime_identity))
        provider_effect_checkpoint("target_env_update_started", "target_env_update")
        dokploy_api.update_dokploy_target_env(
            host=host,
            token=token,
            target_type="compose",
            target_id=target_id_record.target_id,
            target_payload=target_payload,
            env_text=dokploy_api.serialize_dokploy_env_text(desired_env),
        )
        refreshed_payload = dokploy_api.fetch_dokploy_target_payload(
            host=host,
            token=token,
            target_type="compose",
            target_id=target_id_record.target_id,
        )
        refreshed_env = dokploy_api.parse_dokploy_env_text(str(refreshed_payload.get("env") or ""))
        _require_exact_live_volumes(
            env=refreshed_env,
            db_volume=plan.new_db_volume,
            data_volume=plan.data_volume,
            log_volume=plan.log_volume,
        )
        env_evidence = {
            "old_db_volume": plan.old_db_volume,
            "new_db_volume": plan.new_db_volume,
            "data_volume": plan.data_volume,
            "log_volume": plan.log_volume,
        }
        phase_evidence["target_env_update"] = env_evidence
        phase_checkpoint("target_env_updated", env_evidence)

        latest_before = dokploy_api.latest_deployment_for_target(
            host=host,
            token=token,
            target_type="compose",
            target_id=target_id_record.target_id,
        )
        provider_effect_checkpoint("deployment_started", "deploy_trigger")
        dokploy_api.trigger_deployment(
            host=host,
            token=token,
            target_type="compose",
            target_id=target_id_record.target_id,
            no_cache=request.no_cache,
        )
        deployment_wait_result = dokploy_api.wait_for_target_deployment(
            host=host,
            token=token,
            target_type="compose",
            target_id=target_id_record.target_id,
            before_key=dokploy_api.deployment_key(latest_before),
            timeout_seconds=deploy_timeout_seconds,
        )
        deploy_evidence = {"provider_deployment": deployment_wait_result}
        phase_evidence["deployment"] = deploy_evidence
        statuses["deployment_status"] = "pass"
        phase_checkpoint("deployed", deploy_evidence)

        post_deploy_result = execute_odoo_post_deploy(
            control_plane_root=control_plane_root,
            record_store=record_store,
            request=OdooPostDeployRequest(
                context=plan.context,
                instance=plan.instance,
                phase="deploy",
            ),
            run_destructive_restore=False,
            provider_effect_checkpoint=provider_checkpoint("post_deploy_started"),
        )
        post_deploy_evidence = PostDeployUpdateEvidence(
            attempted=True,
            status=post_deploy_result.post_deploy_status,
            detail=post_deploy_result.error_message
            or "Odoo post-deploy completed after verified production backup restore.",
            evidence=post_deploy_result.override_evidence,
        )
        phase_evidence["post_deploy"] = {
            "status": post_deploy_result.post_deploy_status,
            **post_deploy_result.override_evidence,
        }
        statuses["post_deploy_status"] = post_deploy_result.post_deploy_status
        if post_deploy_result.post_deploy_status != "pass":
            _write_failed_deployment(
                record_store=record_store,
                ship_request=ship_request,
                deployment_record_id=deployment_record_id,
                started_at=started_at,
                resolved_target=resolved_target,
                runtime_source=runtime_source,
                runtime_identity=runtime_identity,
                post_deploy_update=post_deploy_evidence,
                error_message=post_deploy_result.error_message or "Odoo post-deploy failed.",
            )
            return result(
                restore_status="fail",
                error_message=post_deploy_result.error_message or "Odoo post-deploy failed.",
            )
        phase_checkpoint(
            "post_deploy_completed",
            {"status": post_deploy_result.post_deploy_status},
        )

        phase_checkpoint(
            "verification_started",
            {
                "base_url": plan.base_url,
                "health_url": plan.health_url,
            },
        )
        verification = verify_odoo_stable_readiness(
            base_url=plan.base_url,
            health_url=plan.health_url,
            verify_health=True,
            verify_canonical=True,
            verify_logo=True,
            timeout_seconds=health_timeout_seconds,
            retry_interval_seconds=ODOO_PROD_BACKUP_RESTORE_VERIFY_RETRY_INTERVAL_SECONDS,
        )
        statuses["health_status"] = verification.health_status
        statuses["canonical_status"] = verification.canonical_status
        statuses["logo_status"] = verification.logo_status
        if verification.error_message:
            _write_failed_deployment(
                record_store=record_store,
                ship_request=ship_request,
                deployment_record_id=deployment_record_id,
                started_at=started_at,
                resolved_target=resolved_target,
                runtime_source=runtime_source,
                runtime_identity=runtime_identity,
                post_deploy_update=post_deploy_evidence,
                error_message=verification.error_message,
            )
            return result(
                restore_status="fail",
                verification_evidence=verification.evidence,
                error_message=verification.error_message,
            )
        destination_health = _verify_runtime_identity(
            health_url=plan.health_url,
            timeout_seconds=health_timeout_seconds,
            expected_runtime_identity=runtime_identity,
        )
        statuses["runtime_identity_status"] = destination_health.runtime_identity_status
        if destination_health.runtime_identity_status != "match":
            error_message = "Odoo runtime identity verification failed: " + (
                destination_health.runtime_identity_detail
                or destination_health.runtime_identity_status
            )
            _write_failed_deployment(
                record_store=record_store,
                ship_request=ship_request,
                deployment_record_id=deployment_record_id,
                started_at=started_at,
                resolved_target=resolved_target,
                runtime_source=runtime_source,
                runtime_identity=runtime_identity,
                post_deploy_update=post_deploy_evidence,
                error_message=error_message,
            )
            return result(
                restore_status="fail",
                verification_evidence=verification.evidence,
                error_message=error_message,
            )
    except DurableOperationAuthorizationDeniedError:
        raise
    except (click.ClickException, OSError, ValueError) as error:
        _write_failed_deployment(
            record_store=record_store,
            ship_request=ship_request,
            deployment_record_id=deployment_record_id,
            started_at=started_at,
            resolved_target=resolved_target,
            runtime_source=runtime_source,
            runtime_identity=runtime_identity,
            error_message=str(error),
        )
        return result(restore_status="fail", error_message=str(error))

    finished_at = utc_now_timestamp()
    final_runtime_identity = runtime_identity.model_copy(update={"deployed_at": finished_at})
    deployment_record = build_deployment_record(
        request=ship_request,
        record_id=deployment_record_id,
        deployment_id="control-plane-dokploy",
        deployment_status="pass",
        started_at=started_at,
        finished_at=finished_at,
        resolved_target=resolved_target,
        runtime_source=runtime_source,
        runtime_identity=final_runtime_identity,
        post_deploy_update=post_deploy_evidence,
        destination_health=destination_health,
    )
    record_store.write_deployment_record(deployment_record)
    record_store.write_environment_inventory(
        build_environment_inventory(deployment_record=deployment_record, updated_at=finished_at)
    )
    release_tuple_id = _write_release_tuple(
        record_store=record_store,
        deployment_record=deployment_record,
        artifact_manifest=artifact_manifest,
        minted_at=finished_at,
    )
    return result(
        restore_status="pass",
        verification_evidence=verification.evidence,
        release_tuple_id=release_tuple_id,
    )


def execute_odoo_prod_backup_restore_verification_replay(
    *,
    control_plane_root: Path,
    record_store: OdooProdBackupRestoreStore,
    operation: OdooProdBackupRestoreOperationRecord,
    phase_checkpoint: RestorePhaseCheckpoint,
    provider_effect_checkpoint: RestoreProviderEffectCheckpoint,
) -> OdooProdBackupRestoreResult:
    if not odoo_prod_backup_restore_operation_is_verification_replay_claim(operation):
        raise click.ClickException(
            "Odoo production restore replay requires an eligible attempt-2 claim."
        )
    stored_result = operation.result
    if stored_result is None:
        raise click.ClickException(
            "Odoo production restore replay requires stored result evidence."
        )
    plan = operation.plan
    deployment_record_id = stored_result.deployment_record_id.strip()
    if not deployment_record_id:
        raise click.ClickException("Odoo production restore replay requires deployment evidence.")

    try:
        profile = _read_product_profile(record_store=record_store, product=operation.product)
        lane = _read_prod_lane(profile=profile, context=operation.context)
        target_record = _read_target_record(
            record_store=record_store,
            context=operation.context,
            instance=operation.instance,
        )
        target_id_record = _read_target_id_record(
            record_store=record_store,
            context=operation.context,
            instance=operation.instance,
        )
        artifact_manifest = record_store.read_artifact_manifest(plan.expected_current_artifact_id)
        deployment_record = record_store.read_deployment_record(deployment_record_id)
        _require_restore_replay_record_authority(
            profile=profile,
            lane=lane,
            target_record=target_record,
            target_id_record=target_id_record,
            artifact_manifest=artifact_manifest,
            plan=plan,
        )
        runtime_identity = _existing_restore_runtime_identity(
            deployment_record=deployment_record,
            plan=plan,
            deployment_record_id=deployment_record_id,
        )
    except (click.ClickException, OSError, ValueError) as error:
        raise click.ClickException(
            "Odoo production restore replay record authority is unavailable."
        ) from error
    ship_request = _ship_request_from_restore_plan(
        plan=plan,
        target_record=target_record,
        timeout_seconds=operation.request.timeout_seconds,
        health_timeout_seconds=operation.request.health_timeout_seconds,
        no_cache=operation.request.no_cache,
    )
    resolved_target = ResolvedTargetEvidence(
        target_type="compose",
        target_id=plan.target_id,
        target_name=plan.target_name,
    )
    runtime_source = {
        **{
            key: value
            for key, value in deployment_record.runtime_source.items()
            if key != "restore_error"
        },
        "restore_replay": "verification_only",
        "restore_replay_attempt": str(operation.attempt),
    }
    phase_evidence = dict(stored_result.phase_evidence)
    statuses: dict[str, str] = {
        "database_restore_status": stored_result.database_restore_status,
        "filestore_stage_status": stored_result.filestore_stage_status,
        "web_quiesce_status": stored_result.web_quiesce_status,
        "filestore_activation_status": stored_result.filestore_activation_status,
        "deployment_status": stored_result.deployment_status,
        "post_deploy_status": "skipped",
        "health_status": "skipped",
        "canonical_status": "skipped",
        "logo_status": "skipped",
        "runtime_identity_status": "skipped",
    }
    health_timeout_seconds = ship_request.health_timeout_seconds or (
        target_record.healthcheck_timeout_seconds
        or dokploy_source.DEFAULT_DOKPLOY_HEALTH_TIMEOUT_SECONDS
    )

    def result(
        *,
        restore_status: str,
        error_message: str = "",
        verification_evidence: OdooVerificationEvidence | None = None,
        release_tuple_id: str = "",
    ) -> OdooProdBackupRestoreResult:
        return OdooProdBackupRestoreResult.model_validate(
            {
                "product": plan.product,
                "context": plan.context,
                "instance": plan.instance,
                "backup_record_id": plan.backup_record_id,
                "verification_record_id": plan.verification_record_id,
                "plan_fingerprint": plan.plan_fingerprint,
                "deployment_record_id": deployment_record_id,
                "release_tuple_id": release_tuple_id,
                "restore_status": restore_status,
                "old_db_volume": plan.old_db_volume,
                "new_db_volume": plan.new_db_volume,
                "data_volume": plan.data_volume,
                "log_volume": plan.log_volume,
                "filestore_quarantine_path": plan.filestore_quarantine_path,
                "database_dump_sha256": plan.database_dump_sha256,
                "filestore_archive_sha256": plan.filestore_archive_sha256,
                "verification_evidence": verification_evidence or OdooVerificationEvidence(),
                "phase_evidence": phase_evidence,
                "error_message": error_message,
                **statuses,
            }
        )

    def replay_failure(
        *,
        code: str,
        post_deploy_update: PostDeployUpdateEvidence | None = None,
        verification_evidence: OdooVerificationEvidence | None = None,
    ) -> OdooProdBackupRestoreResult:
        error_message = f"Odoo production restore replay failed: {code}."
        phase_evidence["verification_replay"] = {
            "status": "fail",
            "code": code,
            "attempt": str(operation.attempt),
        }
        _write_failed_deployment(
            record_store=record_store,
            ship_request=ship_request,
            deployment_record_id=deployment_record_id,
            deployment_id=deployment_record.deploy.deployment_id or "control-plane-dokploy",
            started_at=deployment_record.deploy.started_at,
            resolved_target=resolved_target,
            runtime_source=runtime_source,
            runtime_identity=runtime_identity,
            post_deploy_update=(
                post_deploy_update
                if post_deploy_update is not None
                else deployment_record.post_deploy_update
            ),
            error_message=error_message,
        )
        return result(
            restore_status="fail",
            verification_evidence=verification_evidence,
            error_message=error_message,
        )

    try:
        host, token = dokploy_source.read_dokploy_config(control_plane_root=control_plane_root)
        target_payload = dokploy_api.fetch_dokploy_target_payload(
            host=host,
            token=token,
            target_type="compose",
            target_id=target_id_record.target_id,
        )
    except (click.ClickException, OSError, ValueError):
        return replay_failure(code="live_target_read_failed")

    try:
        live_env = dokploy_api.parse_dokploy_env_text(str(target_payload.get("env") or ""))
        if str(target_payload.get("name") or "").strip() != plan.target_name:
            raise click.ClickException(
                "Odoo production restore replay live target identity changed."
            )
        _require_exact_live_volumes(
            env=live_env,
            db_volume=plan.new_db_volume,
            data_volume=plan.data_volume,
            log_volume=plan.log_volume,
        )
        _require_live_runtime_identity(
            env=live_env,
            expected_runtime_identity=runtime_identity,
        )
    except (click.ClickException, ValueError):
        return replay_failure(code="live_target_authority_changed")

    phase_checkpoint(
        "post_deploy_started",
        {
            "replay": "verification_only",
            "deployment_record_id": deployment_record_id,
        },
    )
    try:
        post_deploy_result = execute_odoo_post_deploy(
            control_plane_root=control_plane_root,
            record_store=record_store,
            request=OdooPostDeployRequest(
                context=plan.context,
                instance=plan.instance,
                phase="deploy",
            ),
            run_destructive_restore=False,
            provider_effect_checkpoint=lambda effect: provider_effect_checkpoint(
                "post_deploy_started", effect
            ),
        )
    except DurableOperationAuthorizationDeniedError:
        raise
    except (click.ClickException, OSError, ValueError):
        return replay_failure(code="post_deploy_execution_failed")

    post_deploy_evidence = PostDeployUpdateEvidence(
        attempted=True,
        status=post_deploy_result.post_deploy_status,
        detail=(
            "Odoo post-deploy completed during restore verification replay."
            if post_deploy_result.post_deploy_status == "pass"
            else "Odoo post-deploy failed during restore verification replay."
        ),
        evidence=post_deploy_result.override_evidence,
    )
    phase_evidence["post_deploy"] = {
        "status": post_deploy_result.post_deploy_status,
        "replay": "verification_only",
        **post_deploy_result.override_evidence,
    }
    statuses["post_deploy_status"] = post_deploy_result.post_deploy_status
    if post_deploy_result.post_deploy_status != "pass":
        return replay_failure(
            code="post_deploy_failed",
            post_deploy_update=post_deploy_evidence,
        )
    phase_checkpoint(
        "post_deploy_completed",
        {"status": post_deploy_result.post_deploy_status, "replay": "verification_only"},
    )
    phase_checkpoint(
        "verification_started",
        {
            "base_url": plan.base_url,
            "health_url": plan.health_url,
            "replay": "verification_only",
        },
    )
    try:
        verification = verify_odoo_stable_readiness(
            base_url=plan.base_url,
            health_url=plan.health_url,
            verify_health=True,
            verify_canonical=True,
            verify_logo=True,
            timeout_seconds=health_timeout_seconds,
            retry_interval_seconds=ODOO_PROD_BACKUP_RESTORE_VERIFY_RETRY_INTERVAL_SECONDS,
        )
    except (click.ClickException, OSError, ValueError):
        return replay_failure(
            code="readiness_execution_failed",
            post_deploy_update=post_deploy_evidence,
        )
    statuses["health_status"] = verification.health_status
    statuses["canonical_status"] = verification.canonical_status
    statuses["logo_status"] = verification.logo_status
    if verification.error_message:
        return replay_failure(
            code="readiness_failed",
            post_deploy_update=post_deploy_evidence,
            verification_evidence=verification.evidence,
        )
    try:
        destination_health = _verify_runtime_identity(
            health_url=plan.health_url,
            timeout_seconds=health_timeout_seconds,
            expected_runtime_identity=runtime_identity,
        )
    except (click.ClickException, OSError, ValueError):
        return replay_failure(
            code="runtime_identity_execution_failed",
            post_deploy_update=post_deploy_evidence,
            verification_evidence=verification.evidence,
        )
    statuses["runtime_identity_status"] = destination_health.runtime_identity_status
    if destination_health.runtime_identity_status != "match":
        return replay_failure(
            code="runtime_identity_failed",
            post_deploy_update=post_deploy_evidence,
            verification_evidence=verification.evidence,
        )

    phase_evidence["verification_replay"] = {
        "status": "pass",
        "attempt": str(operation.attempt),
    }

    finished_at = utc_now_timestamp()
    final_runtime_identity = runtime_identity.model_copy(update={"deployed_at": finished_at})
    deployment_record = build_deployment_record(
        request=ship_request,
        record_id=deployment_record_id,
        deployment_id=deployment_record.deploy.deployment_id or "control-plane-dokploy",
        deployment_status="pass",
        started_at=deployment_record.deploy.started_at,
        finished_at=finished_at,
        resolved_target=resolved_target,
        runtime_source=runtime_source,
        runtime_identity=final_runtime_identity,
        post_deploy_update=post_deploy_evidence,
        destination_health=destination_health,
    )
    record_store.write_deployment_record(deployment_record)
    record_store.write_environment_inventory(
        build_environment_inventory(deployment_record=deployment_record, updated_at=finished_at)
    )
    release_tuple_id = _write_release_tuple(
        record_store=record_store,
        deployment_record=deployment_record,
        artifact_manifest=artifact_manifest,
        minted_at=finished_at,
    )
    return result(
        restore_status="pass",
        verification_evidence=verification.evidence,
        release_tuple_id=release_tuple_id,
    )


def _read_product_profile(
    *, record_store: OdooProdBackupRestoreStore, product: str
) -> LaunchplaneProductProfileRecord:
    try:
        profile = record_store.read_product_profile_record(product)
    except FileNotFoundError as error:
        raise click.ClickException(
            f"Launchplane has no product profile for {product!r}."
        ) from error
    if profile.driver_id != "odoo":
        raise click.ClickException("Odoo production backup restore requires an Odoo product.")
    return profile


def _read_prod_lane(
    *, profile: LaunchplaneProductProfileRecord, context: str
) -> ProductLaneProfile:
    matching = tuple(
        lane
        for lane in profile.lanes
        if lane.instance.strip().lower() == "prod" and lane.context.strip().lower() == context
    )
    if len(matching) != 1:
        raise click.ClickException(
            "Odoo production backup restore requires exactly one matching prod lane."
        )
    return matching[0]


def _read_target_record(
    *, record_store: OdooProdBackupRestoreStore, context: str, instance: str
) -> DokployTargetRecord:
    try:
        return record_store.read_dokploy_target_record(
            context_name=context,
            instance_name=instance,
        )
    except FileNotFoundError as error:
        raise click.ClickException(
            "Odoo production backup restore target record is missing."
        ) from error


def _read_target_id_record(
    *, record_store: OdooProdBackupRestoreStore, context: str, instance: str
) -> DokployTargetIdRecord:
    try:
        return record_store.read_dokploy_target_id_record(
            context_name=context,
            instance_name=instance,
        )
    except FileNotFoundError as error:
        raise click.ClickException(
            "Odoo production backup restore target id is missing."
        ) from error


def _read_inventory(
    *, record_store: OdooProdBackupRestoreStore, context: str, instance: str
) -> EnvironmentInventory:
    try:
        return record_store.read_environment_inventory(
            context_name=context,
            instance_name=instance,
        )
    except FileNotFoundError as error:
        raise click.ClickException(
            "Odoo production backup restore inventory is missing."
        ) from error


def _runtime_values_from_store(
    *,
    record_store: OdooProdBackupRestoreStore,
    target_record: DokployTargetRecord,
    context: str,
    instance: str,
) -> dict[str, str]:
    definition = (
        control_plane_runtime_environments.load_optional_runtime_environment_definition_from_store(
            record_store=record_store
        )
    )
    if definition is None:
        raise click.ClickException(
            "Odoo production backup restore requires DB-backed runtime environment records."
        )
    values = control_plane_runtime_environments.resolve_values_from_definition(
        definition=definition,
        context_name=context,
        instance_name=instance,
    )
    values.update(target_record.env)
    return {key: str(value) for key, value in values.items()}


def _normalized_absolute_path(
    raw_value: str,
    *,
    label: str,
    blockers: list[str],
) -> str:
    normalized_value = raw_value.strip()
    path = PurePosixPath(normalized_value)
    if not normalized_value or not path.is_absolute() or ".." in path.parts:
        blockers.append(f"DB-backed {label} is missing or path-unsafe.")
        return ""
    return path.as_posix()


def _backup_paths(
    *,
    backup_root: str,
    database_name: str,
    backup_record_id: str,
    filestore_path: str,
) -> dict[str, str]:
    if not backup_root or not database_name:
        return {}
    backup_dir = f"{backup_root}/{database_name}/{backup_record_id}"
    return {
        "database_name": database_name,
        "filestore_path": filestore_path,
        "backup_root": backup_root,
        "backup_dir": backup_dir,
        "database_dump_path": f"{backup_dir}/{database_name}.dump",
        "filestore_archive_path": f"{backup_dir}/{database_name}-filestore.tar.gz",
        "manifest_path": f"{backup_dir}/manifest.json",
    }


def _read_backup_record(
    *,
    record_store: OdooProdBackupRestoreStore,
    record_id: str,
    sources: frozenset[str],
    context: str,
    instance: str,
    label: str,
    blockers: list[str],
) -> BackupGateRecord | None:
    try:
        record = record_store.read_backup_gate_record(record_id)
    except FileNotFoundError:
        blockers.append(f"Exact passing Odoo {label} record was not found.")
        return None
    if (
        record.record_id != record_id
        or record.context != context
        or record.instance != instance
        or record.source not in sources
        or not record.required
        or record.status != "pass"
    ):
        blockers.append(f"Odoo {label} record does not match exact production authority.")
    return record


def _validate_backup_gate_paths(
    *, record: BackupGateRecord, expected_paths: dict[str, str], blockers: list[str]
) -> None:
    required_keys = (
        "database_name",
        "filestore_path",
        "backup_root",
        "backup_dir",
        "database_dump_path",
        "filestore_archive_path",
        "manifest_path",
    )
    if any(record.evidence.get(key) != expected_paths.get(key) for key in required_keys):
        blockers.append("Backup-gate paths differ from current DB-backed restore authority.")


def _verification_evidence(
    *,
    record: BackupGateRecord | None,
    backup_record: BackupGateRecord | None,
    expected_paths: dict[str, str],
    blockers: list[str],
) -> dict[str, object]:
    defaults: dict[str, object] = {
        "verification_nonce": "",
        "verification_status": "",
        "manifest_status": "",
        "sha256_status": "",
        "pg_restore_status": "",
        "tar_status": "",
        "staging_space_status": "",
        "database_dump_sha256": "",
        "filestore_archive_sha256": "",
        "database_dump_size": 0,
        "filestore_archive_size": 0,
        "pg_restore_entry_count": 0,
        "filestore_member_count": 0,
        "filestore_unpacked_size": 0,
        "data_volume_free_bytes": 0,
        "staging_required_bytes": 0,
    }
    if record is None:
        return defaults
    evidence = record.evidence
    if backup_record is None or evidence.get("backup_record_id") != backup_record.record_id:
        blockers.append("Backup verification record does not bind the exact backup gate.")
    if (
        backup_record is not None
        and evidence.get("backup_record_created_at") != backup_record.created_at
    ):
        blockers.append("Backup verification record does not bind backup creation evidence.")
    required_path_keys = (
        "database_name",
        "filestore_path",
        "backup_root",
        "backup_dir",
        "database_dump_path",
        "filestore_archive_path",
        "manifest_path",
    )
    if any(evidence.get(key) != expected_paths.get(key) for key in required_path_keys):
        blockers.append("Backup verification paths differ from DB-backed restore authority.")
    if evidence.get("verification_status") != "pass" or evidence.get("failure_code"):
        blockers.append("Backup verification evidence is not passing.")
    verification_nonce = evidence.get("verification_nonce", "")
    if not isinstance(verification_nonce, str) or not re.fullmatch(
        r"[0-9a-f]{64}", verification_nonce
    ):
        blockers.append("Backup verification evidence does not include its request nonce.")
        verification_nonce = ""
    for status_key in (
        "manifest_status",
        "sha256_status",
        "pg_restore_status",
        "tar_status",
        "staging_space_status",
    ):
        if evidence.get(status_key) != "pass":
            blockers.append(f"Backup verification {status_key} is not passing.")
    database_digest = evidence.get("database_dump_sha256", "")
    filestore_digest = evidence.get("filestore_archive_sha256", "")
    if not re.fullmatch(r"[0-9a-f]{64}", database_digest):
        blockers.append("Backup verification database dump hash is invalid.")
    if not re.fullmatch(r"[0-9a-f]{64}", filestore_digest):
        blockers.append("Backup verification filestore archive hash is invalid.")
    parsed: dict[str, object] = {
        "verification_nonce": verification_nonce,
        "verification_status": str(evidence.get("verification_status", "")),
        "manifest_status": str(evidence.get("manifest_status", "")),
        "sha256_status": str(evidence.get("sha256_status", "")),
        "pg_restore_status": str(evidence.get("pg_restore_status", "")),
        "tar_status": str(evidence.get("tar_status", "")),
        "staging_space_status": str(evidence.get("staging_space_status", "")),
        "database_dump_sha256": database_digest,
        "filestore_archive_sha256": filestore_digest,
    }
    for key in (
        "database_dump_size",
        "filestore_archive_size",
        "pg_restore_entry_count",
        "filestore_member_count",
        "filestore_unpacked_size",
        "data_volume_free_bytes",
        "staging_required_bytes",
    ):
        try:
            value = int(evidence.get(key, "0"))
        except ValueError:
            value = 0
        if value < 1:
            blockers.append(f"Backup verification {key} must be positive.")
        parsed[key] = value
    data_volume_free_bytes = parsed["data_volume_free_bytes"]
    staging_required_bytes = parsed["staging_required_bytes"]
    if (
        not isinstance(data_volume_free_bytes, int)
        or not isinstance(staging_required_bytes, int)
        or data_volume_free_bytes < staging_required_bytes
    ):
        blockers.append("Backup verification staging-space evidence is insufficient.")
    return parsed


def _filestore_database_path(*, filestore_path: str, database_name: str) -> str:
    path = PurePosixPath(filestore_path)
    if path.name == database_name:
        return path.as_posix()
    return (path / database_name).as_posix()


def _artifact_image_reference(manifest: ArtifactIdentityManifest) -> str:
    return f"{manifest.image.repository}@{manifest.image.digest}"


def _restore_steps(*, blocked: bool) -> tuple[OdooProdBackupRestoreStep, ...]:
    status: Literal["ready", "blocked"] = "blocked" if blocked else "ready"
    return (
        OdooProdBackupRestoreStep(
            step_id="restore-database-volume",
            status=status,
            message="Create and fully restore the verified dump into the fresh DB volume.",
        ),
        OdooProdBackupRestoreStep(
            step_id="stage-filestore",
            status=status,
            message="Revalidate and safely extract the filestore into sibling staging.",
        ),
        OdooProdBackupRestoreStep(
            step_id="quiesce-and-activate",
            status=status,
            message="Quiesce web, quarantine the old filestore, and activate staged files.",
        ),
        OdooProdBackupRestoreStep(
            step_id="switch-database-volume",
            status=status,
            message="Switch only the DB volume to DB-backed desired authority and deploy.",
        ),
        OdooProdBackupRestoreStep(
            step_id="verify-production-runtime",
            status=status,
            message="Run post-deploy, health, canonical, logo, and runtime identity checks.",
        ),
    )


def _target_definition(
    *, target_record: DokployTargetRecord, target_id_record: DokployTargetIdRecord
) -> dokploy_source.DokployTargetDefinition:
    payload = target_record.model_dump(
        mode="json",
        exclude={"schema_version", "updated_at", "source_label"},
    )
    payload["target_id"] = target_id_record.target_id
    return dokploy_source.DokployTargetDefinition.model_validate(payload)


def _ship_request_from_restore_plan(
    *,
    plan: OdooProdBackupRestorePlan,
    target_record: DokployTargetRecord,
    timeout_seconds: int | None,
    health_timeout_seconds: int | None,
    no_cache: bool,
) -> ShipRequest:
    return ShipRequest(
        artifact_id=plan.expected_current_artifact_id,
        context=plan.context,
        instance=plan.instance,
        source_git_ref=plan.expected_source_git_ref,
        target_name=plan.target_name,
        target_type="compose",
        provider_id="dokploy",
        target_category="compose",
        provider_target_type="compose",
        deploy_mode="dokploy-compose-api",
        wait=True,
        timeout_seconds=(
            timeout_seconds
            or target_record.deploy_timeout_seconds
            or dokploy_source.DEFAULT_DOKPLOY_DEPLOY_TIMEOUT_SECONDS
        ),
        verify_health=True,
        health_timeout_seconds=(
            health_timeout_seconds
            or target_record.healthcheck_timeout_seconds
            or dokploy_source.DEFAULT_DOKPLOY_HEALTH_TIMEOUT_SECONDS
        ),
        no_cache=no_cache,
        destination_health=HealthcheckEvidence(status="pending"),
    )


def _require_restore_replay_record_authority(
    *,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    target_record: DokployTargetRecord,
    target_id_record: DokployTargetIdRecord,
    artifact_manifest: ArtifactIdentityManifest,
    plan: OdooProdBackupRestorePlan,
) -> None:
    lane_base_url = lane.base_url.strip().rstrip("/")
    lane_health_url = lane.health_url.strip()
    resolved_health_url = (
        lane_health_url
        if lane_health_url
        and not is_legacy_derived_odoo_health_url(
            health_url=lane_health_url,
            base_url=lane_base_url,
            profile_health_path=profile.health_path,
        )
        else default_odoo_health_url(base_url=lane_base_url)
    )
    expected_domain_hosts = tuple(
        sorted({domain.strip().lower() for domain in target_record.domains if domain.strip()})
    )
    if (
        lane_base_url != plan.base_url
        or resolved_health_url != plan.health_url
        or target_record.target_type != "compose"
        or target_id_record.target_id != plan.target_id
        or (
            target_record.target_name.strip()
            and target_record.target_name.strip() != plan.target_name
        )
        or expected_domain_hosts != plan.expected_domain_hosts
        or artifact_manifest.artifact_id != plan.expected_current_artifact_id
        or artifact_manifest.source_commit != plan.expected_source_git_ref
        or _artifact_image_reference(artifact_manifest) != plan.image_reference
        or not artifact_manifest_matches_image_repository(
            artifact_manifest,
            expected_repository=profile.image.repository,
        )
    ):
        raise click.ClickException("Odoo production restore replay record authority changed.")


def _existing_restore_runtime_identity(
    *,
    deployment_record: DeploymentRecord,
    plan: OdooProdBackupRestorePlan,
    deployment_record_id: str,
) -> RuntimeIdentity:
    runtime_identity = deployment_record.runtime_identity
    resolved_target = deployment_record.resolved_target
    artifact_identity = deployment_record.artifact_identity
    if (
        deployment_record.record_id != deployment_record_id
        or deployment_record.context != plan.context
        or deployment_record.instance != plan.instance
        or deployment_record.source_git_ref != plan.expected_source_git_ref
        or artifact_identity is None
        or artifact_identity.artifact_id != plan.expected_current_artifact_id
        or runtime_identity is None
        or runtime_identity.product != plan.product
        or runtime_identity.context != plan.context
        or runtime_identity.instance != plan.instance
        or runtime_identity.deployment_record_id != deployment_record_id
        or runtime_identity.artifact_id != plan.expected_current_artifact_id
        or runtime_identity.source_git_ref != plan.expected_source_git_ref
        or runtime_identity.image_reference != plan.image_reference
        or runtime_identity.deployed_at.strip()
        or deployment_record.deploy.status != "fail"
        or resolved_target is None
        or resolved_target.target_type != "compose"
        or resolved_target.target_id != plan.target_id
        or resolved_target.target_name != plan.target_name
    ):
        raise click.ClickException(
            "Odoo production restore replay requires exact failed deployment runtime identity."
        )
    return runtime_identity


def _require_live_runtime_identity(
    *,
    env: Mapping[str, str],
    expected_runtime_identity: RuntimeIdentity,
) -> None:
    live_runtime_identity = parse_runtime_identity_payload(
        env.get("LAUNCHPLANE_RUNTIME_IDENTITY_JSON", "")
    )
    if live_runtime_identity is None:
        raise click.ClickException(
            "Odoo production restore replay live runtime identity is missing or malformed."
        )
    if live_runtime_identity != expected_runtime_identity:
        raise click.ClickException("Odoo production restore replay live runtime identity changed.")


def _require_exact_live_volumes(
    *,
    env: Mapping[str, str],
    db_volume: str,
    data_volume: str,
    log_volume: str,
) -> None:
    expected = {
        "ODOO_DB_VOLUME": db_volume,
        "ODOO_DATA_VOLUME": data_volume,
        "ODOO_LOG_VOLUME": log_volume,
    }
    mismatches = tuple(key for key, value in expected.items() if env.get(key, "").strip() != value)
    if mismatches:
        raise click.ClickException(
            "Odoo production backup restore live volume authority changed for: "
            + ", ".join(mismatches)
        )


def _verify_runtime_identity(
    *,
    health_url: str,
    timeout_seconds: int,
    expected_runtime_identity: RuntimeIdentity,
) -> HealthcheckEvidence:
    try:
        response = wait_for_runtime_identity_healthcheck_with_retry(
            url=health_url,
            timeout_seconds=timeout_seconds,
            expected_runtime_identity=expected_runtime_identity,
            sleep=time.sleep,
            monotonic=time.monotonic,
        )
        return healthcheck_evidence_with_runtime_identity(
            HealthcheckEvidence(
                verified=True,
                urls=(health_url,),
                timeout_seconds=timeout_seconds,
                status="pass",
            ),
            expected_runtime_identity=expected_runtime_identity,
            healthcheck_pass=response,
        )
    except RuntimeIdentityHealthcheckError as error:
        evidence = HealthcheckEvidence(
            verified=False,
            urls=(health_url,),
            timeout_seconds=timeout_seconds,
            status="fail",
        )
        if error.healthcheck_pass is None:
            return evidence.model_copy(
                update={
                    "runtime_identity_status": "missing",
                    "runtime_identity_detail": str(error),
                }
            )
        return healthcheck_evidence_with_runtime_identity(
            evidence,
            expected_runtime_identity=expected_runtime_identity,
            healthcheck_pass=error.healthcheck_pass,
        )


def _write_failed_deployment(
    *,
    record_store: OdooProdBackupRestoreStore,
    ship_request: ShipRequest,
    deployment_record_id: str,
    started_at: str,
    resolved_target: ResolvedTargetEvidence,
    runtime_source: dict[str, str],
    runtime_identity: RuntimeIdentity,
    error_message: str,
    post_deploy_update: PostDeployUpdateEvidence | None = None,
    deployment_id: str = "control-plane-dokploy",
) -> None:
    record_store.write_deployment_record(
        build_deployment_record(
            request=ship_request,
            record_id=deployment_record_id,
            deployment_id=deployment_id,
            deployment_status="fail",
            started_at=started_at,
            finished_at=utc_now_timestamp(),
            resolved_target=resolved_target,
            runtime_source={**runtime_source, "restore_error": error_message[:1000]},
            runtime_identity=runtime_identity,
            post_deploy_update=post_deploy_update or PostDeployUpdateEvidence(status="skipped"),
            destination_health=HealthcheckEvidence(status="fail"),
        )
    )


def _write_release_tuple(
    *,
    record_store: OdooProdBackupRestoreStore,
    deployment_record: DeploymentRecord,
    artifact_manifest: ArtifactIdentityManifest,
    minted_at: str,
) -> str:
    if not control_plane_release_tuples.should_mint_release_tuple_for_channel(
        deployment_record.instance
    ):
        return ""
    release_tuple = control_plane_release_tuples.build_release_tuple_record_from_artifact_manifest(
        context_name=deployment_record.context,
        channel_name=deployment_record.instance,
        artifact_manifest=artifact_manifest,
        deployment_record_id=deployment_record.record_id,
        minted_at=minted_at,
    )
    record_store.write_release_tuple_record(release_tuple)
    return release_tuple.tuple_id
