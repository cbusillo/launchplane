from __future__ import annotations

import re
import secrets as python_secrets
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol

import click
from pydantic import ValidationError

from control_plane import runtime_environments as control_plane_runtime_environments
from control_plane.contracts.artifact_identity import (
    ArtifactIdentityManifest,
    artifact_manifest_matches_image_repository,
)
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.odoo_prod_retained_volume_backup_import import (
    OdooProdRetainedVolumeBackupImportApplyRequest,
    OdooProdRetainedVolumeBackupImportCompletionEvidence,
    OdooProdRetainedVolumeBackupImportInspectionEvidence,
    OdooProdRetainedVolumeBackupImportPlan,
    OdooProdRetainedVolumeBackupImportRequest,
    OdooProdRetainedVolumeBackupImportResult,
    OdooProdRetainedVolumeBackupImportStep,
    build_odoo_prod_retained_volume_backup_import_plan_fingerprint,
)
from control_plane.contracts.odoo_prod_retained_volume_backup_import_operation import (
    OdooProdRetainedVolumeBackupImportOperationPhase,
)
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.runtime_identity import RuntimeIdentity, parse_runtime_identity_payload
from control_plane.dokploy import api as dokploy_api
from control_plane.dokploy import post_deploy as dokploy_post_deploy
from control_plane.dokploy import source as dokploy_source
from control_plane.workflows.odoo_prod_backup_gate import (
    RETAINED_VOLUME_BACKUP_IMPORT_SOURCE,
)
from control_plane.workflows.ship import utc_now_timestamp


_PATH_COMPONENT_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_VOLUME_DATA_ROOT = PurePosixPath("/volumes/data")
_BACKUP_HEADROOM_BYTES = 64 * 1024 * 1024


class OdooProdRetainedVolumeBackupImportStore(Protocol):
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

    def write_backup_gate_record(self, record: BackupGateRecord) -> object: ...


ImportPhaseCheckpoint = Callable[
    [OdooProdRetainedVolumeBackupImportOperationPhase, dict[str, str]], None
]
ImportProviderEffectCheckpoint = Callable[
    [OdooProdRetainedVolumeBackupImportOperationPhase, str], None
]


def build_odoo_prod_retained_volume_backup_import_plan(
    *,
    control_plane_root: Path,
    record_store: OdooProdRetainedVolumeBackupImportStore,
    operation_id: str,
    request: OdooProdRetainedVolumeBackupImportRequest,
    phase_checkpoint: ImportPhaseCheckpoint,
    provider_effect_checkpoint: ImportProviderEffectCheckpoint,
    timeout_seconds: int | None = None,
) -> OdooProdRetainedVolumeBackupImportPlan:
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
    inventory = _read_inventory(
        record_store=record_store,
        context=request.context,
        instance=request.instance,
    )
    blockers: list[str] = []
    warnings: list[str] = []

    if target_record.target_type != "compose":
        blockers.append("Retained-volume backup import requires a compose target.")

    runtime_values = _runtime_values_from_store(
        record_store=record_store,
        target_record=target_record,
        context=request.context,
        instance=request.instance,
    )
    destination_database_name = runtime_values.get("ODOO_DB_NAME", "").strip()
    database_user = runtime_values.get("ODOO_DB_USER", "").strip()
    active_db_volume = runtime_values.get("ODOO_DB_VOLUME", "").strip()
    active_data_volume = runtime_values.get("ODOO_DATA_VOLUME", "").strip()
    active_log_volume = runtime_values.get("ODOO_LOG_VOLUME", "").strip()
    if destination_database_name != request.expected_destination_database_name:
        blockers.append(
            "DB-backed ODOO_DB_NAME does not match the reviewed destination database name."
        )
    if database_user != request.expected_database_user:
        blockers.append("DB-backed ODOO_DB_USER does not match the reviewed database user.")
    if active_db_volume != request.expected_active_db_volume:
        blockers.append("DB-backed ODOO_DB_VOLUME does not match the reviewed active volume.")
    if active_data_volume != request.expected_active_data_volume:
        blockers.append("DB-backed ODOO_DATA_VOLUME does not match the reviewed active volume.")
    if active_log_volume != request.expected_active_log_volume:
        blockers.append("DB-backed ODOO_LOG_VOLUME does not match the reviewed active volume.")
    if destination_database_name in {"postgres", "template0", "template1"}:
        blockers.append("Retained-volume backup import cannot target a PostgreSQL system database.")

    filestore_path = _normalized_absolute_path(
        runtime_values.get("ODOO_FILESTORE_PATH", ""),
        label="ODOO_FILESTORE_PATH",
        blockers=blockers,
    )
    backup_root = _normalized_absolute_path(
        runtime_values.get("ODOO_BACKUP_ROOT", ""),
        label="ODOO_BACKUP_ROOT",
        blockers=blockers,
    )
    if backup_root:
        allowed_backup_root = PurePosixPath(dokploy_post_deploy.DEFAULT_ODOO_BACKUP_ROOT)
        backup_root_path = PurePosixPath(backup_root)
        if (
            backup_root_path != allowed_backup_root
            and allowed_backup_root not in backup_root_path.parents
        ):
            blockers.append(
                "DB-backed ODOO_BACKUP_ROOT is outside the dedicated Launchplane backup root."
            )
    backup_paths = _backup_paths(
        backup_root=backup_root,
        database_name=destination_database_name,
        backup_record_id=request.backup_record_id,
        filestore_path=filestore_path,
    )
    filestore_relative_path = _filestore_relative_path(
        filestore_path=filestore_path,
        destination_database_name=destination_database_name,
        blockers=blockers,
    )
    backup_relative_paths = _backup_relative_paths(backup_paths=backup_paths, blockers=blockers)

    try:
        existing_backup_record = record_store.read_backup_gate_record(request.backup_record_id)
    except FileNotFoundError:
        existing_backup_record = None
    if existing_backup_record is not None and not _matching_pending_import_record(
        record=existing_backup_record,
        operation_id=operation_id,
        request=request,
    ):
        blockers.append("The reviewed backup_record_id already exists and cannot be overwritten.")

    current_artifact_id = (
        inventory.artifact_identity.artifact_id if inventory.artifact_identity else ""
    )
    if current_artifact_id != request.expected_current_artifact_id:
        blockers.append("Current production artifact changed after retained-volume review.")
    runtime_identity = inventory.runtime_identity
    if runtime_identity is None:
        blockers.append("Current production inventory lacks runtime identity evidence.")
    else:
        _validate_runtime_identity(
            runtime_identity=runtime_identity,
            inventory=inventory,
            product=request.product,
            context=request.context,
            instance=request.instance,
            artifact_id=request.expected_current_artifact_id,
            blockers=blockers,
        )

    try:
        artifact_manifest = record_store.read_artifact_manifest(
            request.expected_current_artifact_id
        )
    except FileNotFoundError:
        artifact_manifest = None
        blockers.append("Current artifact manifest was not found.")
    if artifact_manifest is not None:
        if not artifact_manifest_matches_image_repository(
            artifact_manifest,
            expected_repository=profile.image.repository,
        ):
            blockers.append("Current artifact image repository does not match product authority.")
        if artifact_manifest.source_commit != inventory.source_git_ref:
            blockers.append("Current artifact source ref does not match production inventory.")
        if runtime_identity is not None:
            expected_image_reference = _artifact_image_reference(artifact_manifest)
            if runtime_identity.image_reference != expected_image_reference:
                blockers.append("Current runtime identity image does not match artifact authority.")

    target_payload: dokploy_api.JsonObject = {}
    try:
        host, token = dokploy_source.read_dokploy_config(control_plane_root=control_plane_root)
        target_payload = dokploy_api.fetch_dokploy_target_payload(
            host=host,
            token=token,
            target_type="compose",
            target_id=target_id_record.target_id,
        )
    except (click.ClickException, OSError):
        host = ""
        token = ""
        blockers.append("Exact live target evidence could not be read.")
    live_env = dokploy_api.parse_dokploy_env_text(str(target_payload.get("env") or ""))
    live_target_name = str(target_payload.get("name") or "").strip()
    target_name = target_record.target_name.strip() or live_target_name
    if target_record.target_name.strip() and live_target_name != target_record.target_name.strip():
        blockers.append("Live target name drifted from the DB-backed target record.")
    target_compose_project = str(target_payload.get("appName") or "").strip()
    if not target_name or not target_compose_project:
        blockers.append("Exact live target name and compose project are required.")
    for key, expected_value in (
        ("ODOO_DB_NAME", destination_database_name),
        ("ODOO_DB_USER", database_user),
        ("ODOO_DB_VOLUME", active_db_volume),
        ("ODOO_DATA_VOLUME", active_data_volume),
        ("ODOO_LOG_VOLUME", active_log_volume),
    ):
        if expected_value and live_env.get(key, "").strip() != expected_value:
            blockers.append(f"Live {key} drifted from DB-backed runtime authority.")
    accepted_runtime_identity = runtime_identity
    if runtime_identity is not None:
        live_runtime_identity = parse_runtime_identity_payload(
            live_env.get("LAUNCHPLANE_RUNTIME_IDENTITY_JSON", "")
        )
        if live_runtime_identity is None or not _live_runtime_identity_env_matches(
            live_env=live_env,
            live_runtime_identity=live_runtime_identity,
        ):
            blockers.append(
                "Live target runtime identity drifted from production inventory authority."
            )
        elif live_runtime_identity == runtime_identity:
            accepted_runtime_identity = live_runtime_identity
        elif _recorded_failed_deployment_matches_live_identity(
            record_store=record_store,
            live_runtime_identity=live_runtime_identity,
            inventory_runtime_identity=runtime_identity,
            target_id=target_id_record.target_id,
            target_name=target_name,
        ):
            accepted_runtime_identity = live_runtime_identity
            warnings.append(
                "Live runtime identity belongs to a recorded failed deployment; "
                "the import plan is bound to that exact repair state."
            )
        else:
            blockers.append(
                "Live target runtime identity drifted from production inventory authority."
            )

    inspection_evidence: OdooProdRetainedVolumeBackupImportInspectionEvidence | None = None
    if not blockers:
        inspection_nonce = python_secrets.token_hex(32)

        def provider_checkpoint(effect_name: str) -> None:
            provider_effect_checkpoint("inspection_started", effect_name)

        try:
            raw_inspection = (
                dokploy_post_deploy.run_compose_odoo_retained_volume_backup_import_inspection(
                    host=host,
                    token=token,
                    target_definition=_target_definition(
                        target_record=target_record,
                        target_id_record=target_id_record,
                    ),
                    expected_compose_app_name=target_compose_project,
                    inspection_nonce=inspection_nonce,
                    backup_record_id=request.backup_record_id,
                    active_db_volume=active_db_volume,
                    active_data_volume=active_data_volume,
                    active_log_volume=active_log_volume,
                    source_db_volume=request.source_db_volume,
                    source_data_volume=request.source_data_volume,
                    staging_clone_volume=request.staging_clone_volume,
                    source_database_name=request.source_database_name,
                    destination_database_name=destination_database_name,
                    database_user=database_user,
                    expected_source_compose_project=request.expected_source_compose_project,
                    filestore_relative_path=filestore_relative_path,
                    backup_dir_relative_path=backup_relative_paths.get("backup_dir", ""),
                    timeout_seconds=timeout_seconds,
                    before_provider_mutation=provider_checkpoint,
                )
            )
            inspection_evidence = (
                OdooProdRetainedVolumeBackupImportInspectionEvidence.model_validate(raw_inspection)
            )
        except (click.ClickException, OSError, ValidationError):
            blockers.append("Retained-volume provider inspection did not complete.")
        if inspection_evidence is not None:
            phase_checkpoint(
                "inspected",
                {
                    "inspection_deployment_id": inspection_evidence.inspection_deployment_id,
                    "source_pg_control_sha256": inspection_evidence.source_pg_control_sha256,
                },
            )
            _validate_inspection_evidence(
                request=request,
                evidence=inspection_evidence,
                expected_inspection_nonce=inspection_nonce,
                destination_database_name=destination_database_name,
                database_user=database_user,
                active_db_volume=active_db_volume,
                active_data_volume=active_data_volume,
                active_log_volume=active_log_volume,
                blockers=blockers,
            )

    active_data_required_bytes = 0
    if inspection_evidence is not None:
        active_data_required_bytes = (
            2
            * (
                inspection_evidence.source_db_volume_used_bytes
                + inspection_evidence.source_filestore_size_bytes
            )
            + _BACKUP_HEADROOM_BYTES
        )
        if inspection_evidence.active_data_free_bytes < active_data_required_bytes:
            blockers.append(
                "Active data volume lacks conservative space for the imported logical backup."
            )

    plan_values = {
        "product": request.product,
        "context": request.context,
        "instance": request.instance,
        "target_id": target_id_record.target_id,
        "target_name": target_name,
        "target_compose_project": target_compose_project,
        "backup_record_id": request.backup_record_id,
        "expected_current_artifact_id": request.expected_current_artifact_id,
        "inventory_updated_at": inventory.updated_at,
        "runtime_identity": accepted_runtime_identity,
        "active_db_volume": request.expected_active_db_volume,
        "active_data_volume": request.expected_active_data_volume,
        "active_log_volume": request.expected_active_log_volume,
        "source_db_volume": request.source_db_volume,
        "source_data_volume": request.source_data_volume,
        "source_database_name": request.source_database_name,
        "destination_database_name": request.expected_destination_database_name,
        "database_user": request.expected_database_user,
        "staging_clone_volume": request.staging_clone_volume,
        "expected_source_compose_project": request.expected_source_compose_project,
        "filestore_path": filestore_path,
        **backup_paths,
        "active_data_required_bytes": active_data_required_bytes,
        "warnings": tuple(warnings),
        "steps": _plan_steps(blocked=bool(blockers)),
        **_inspection_plan_values(inspection_evidence),
    }
    if blockers:
        return OdooProdRetainedVolumeBackupImportPlan.model_validate(
            {
                "plan_status": "blocked",
                "plan_fingerprint": "",
                "blockers": tuple(blockers),
                **plan_values,
            }
        )
    provisional_plan = OdooProdRetainedVolumeBackupImportPlan.model_validate(
        {
            "plan_status": "blocked",
            "plan_fingerprint": "",
            "blockers": ("fingerprint-pending",),
            **plan_values,
        }
    )
    plan_fingerprint = build_odoo_prod_retained_volume_backup_import_plan_fingerprint(
        provisional_plan
    )
    ready_plan = OdooProdRetainedVolumeBackupImportPlan.model_validate(
        {
            "plan_status": "ready",
            "plan_fingerprint": plan_fingerprint,
            "blockers": (),
            **plan_values,
        }
    )
    phase_checkpoint(
        "planned",
        {
            "plan_fingerprint": ready_plan.plan_fingerprint,
            "backup_record_id": ready_plan.backup_record_id,
        },
    )
    return ready_plan


def execute_odoo_prod_retained_volume_backup_import_apply(
    *,
    control_plane_root: Path,
    record_store: OdooProdRetainedVolumeBackupImportStore,
    operation_id: str,
    reviewed_plan: OdooProdRetainedVolumeBackupImportPlan,
    request: OdooProdRetainedVolumeBackupImportApplyRequest,
    phase_checkpoint: ImportPhaseCheckpoint,
    provider_effect_checkpoint: ImportProviderEffectCheckpoint,
) -> OdooProdRetainedVolumeBackupImportResult:
    current_plan = build_odoo_prod_retained_volume_backup_import_plan(
        control_plane_root=control_plane_root,
        record_store=record_store,
        operation_id=operation_id,
        request=OdooProdRetainedVolumeBackupImportRequest.model_validate(
            request.model_dump(
                mode="json",
                exclude={
                    "plan_operation_id",
                    "plan_fingerprint",
                    "confirmation",
                    "timeout_seconds",
                },
            )
        ),
        phase_checkpoint=phase_checkpoint,
        provider_effect_checkpoint=provider_effect_checkpoint,
        timeout_seconds=request.timeout_seconds,
    )
    if current_plan.plan_status != "ready":
        raise click.ClickException(
            "Odoo retained-volume backup import apply requires a ready current plan: "
            + "; ".join(current_plan.blockers)
        )
    if (
        reviewed_plan.plan_fingerprint != request.plan_fingerprint
        or current_plan.plan_fingerprint != request.plan_fingerprint
    ):
        raise click.ClickException(
            "Odoo retained-volume backup import plan fingerprint changed before apply."
        )

    phase_checkpoint(
        "validated",
        {
            "plan_operation_id": request.plan_operation_id,
            "plan_fingerprint": current_plan.plan_fingerprint,
        },
    )
    pending_record = _backup_gate_record(
        plan=current_plan,
        status="pending",
        evidence={
            **_backup_record_evidence(plan=current_plan),
            "operation_id": operation_id,
        },
    )
    record_store.write_backup_gate_record(pending_record)
    phase_checkpoint(
        "backup_record_pending",
        {"backup_record_id": current_plan.backup_record_id},
    )

    phase_evidence: dict[str, dict[str, str]] = {}
    import_nonce = python_secrets.token_hex(32)
    schedule_deployment_id = ""
    try:
        host, token = dokploy_source.read_dokploy_config(control_plane_root=control_plane_root)

        def provider_checkpoint(effect_name: str) -> None:
            provider_effect_checkpoint("provider_import_started", effect_name)

        raw_completion = dokploy_post_deploy.run_compose_odoo_retained_volume_backup_import_apply(
            host=host,
            token=token,
            target_definition=_target_definition_from_plan(current_plan),
            expected_compose_app_name=current_plan.target_compose_project,
            import_nonce=import_nonce,
            operation_id=operation_id,
            plan_fingerprint=current_plan.plan_fingerprint,
            backup_record_id=current_plan.backup_record_id,
            active_db_volume=current_plan.active_db_volume,
            active_data_volume=current_plan.active_data_volume,
            active_log_volume=current_plan.active_log_volume,
            source_db_volume=current_plan.source_db_volume,
            source_data_volume=current_plan.source_data_volume,
            staging_clone_volume=current_plan.staging_clone_volume,
            source_database_name=current_plan.source_database_name,
            destination_database_name=current_plan.destination_database_name,
            database_user=current_plan.database_user,
            expected_source_compose_project=(current_plan.expected_source_compose_project),
            expected_source_pg_control_sha256=(current_plan.source_pg_control_sha256),
            expected_source_pg_version=current_plan.source_pg_version,
            expected_postgres_image_id=current_plan.postgres_image_id,
            expected_script_runner_image_id=current_plan.script_runner_image_id,
            expected_source_filestore_file_count=(current_plan.source_filestore_file_count),
            expected_source_filestore_size_bytes=(current_plan.source_filestore_size_bytes),
            expected_active_data_required_bytes=(current_plan.active_data_required_bytes),
            filestore_relative_path=_filestore_relative_path(
                filestore_path=current_plan.filestore_path,
                destination_database_name=current_plan.destination_database_name,
                blockers=[],
            ),
            backup_dir_relative_path=_relative_to_data_root(current_plan.backup_dir),
            database_dump_relative_path=_relative_to_data_root(current_plan.database_dump_path),
            filestore_archive_relative_path=_relative_to_data_root(
                current_plan.filestore_archive_path
            ),
            manifest_relative_path=_relative_to_data_root(current_plan.manifest_path),
            timeout_seconds=request.timeout_seconds,
            before_provider_mutation=provider_checkpoint,
        )
        schedule_deployment_id = str(raw_completion.pop("schedule_deployment_id", ""))
        completion = OdooProdRetainedVolumeBackupImportCompletionEvidence.model_validate(
            raw_completion
        )
        _require_exact_completion(
            completion=completion,
            plan=current_plan,
            operation_id=operation_id,
            import_nonce=import_nonce,
        )
        if not schedule_deployment_id:
            raise click.ClickException(
                "Odoo retained-volume backup import completion lacked exact schedule deployment evidence."
            )
    except (click.ClickException, OSError, ValidationError) as error:
        failed_evidence = _backup_record_evidence(plan=current_plan)
        failed_evidence["import_nonce"] = import_nonce
        failed_evidence["operation_id"] = operation_id
        if schedule_deployment_id:
            failed_evidence["schedule_deployment_id"] = schedule_deployment_id
        failed_evidence["error_message"] = str(error)
        record_store.write_backup_gate_record(
            _backup_gate_record(
                plan=current_plan,
                status="fail",
                evidence=failed_evidence,
            )
        )
        return OdooProdRetainedVolumeBackupImportResult(
            product=current_plan.product,
            context=current_plan.context,
            instance=current_plan.instance,
            backup_record_id=current_plan.backup_record_id,
            plan_fingerprint=current_plan.plan_fingerprint,
            import_status="fail",
            backup_status="fail",
            source_db_volume=current_plan.source_db_volume,
            source_data_volume=current_plan.source_data_volume,
            staging_clone_volume=current_plan.staging_clone_volume,
            source_database_name=current_plan.source_database_name,
            destination_database_name=current_plan.destination_database_name,
            database_user=current_plan.database_user,
            schedule_deployment_id=schedule_deployment_id,
            phase_evidence=phase_evidence,
            error_message=str(error),
        )

    completion_evidence = {
        key: str(value).lower() if isinstance(value, bool) else str(value)
        for key, value in completion.model_dump(mode="json").items()
    }
    completion_evidence["schedule_deployment_id"] = schedule_deployment_id
    phase_evidence["provider_import"] = completion_evidence
    phase_checkpoint("provider_import_completed", completion_evidence)
    record_store.write_backup_gate_record(
        _backup_gate_record(
            plan=current_plan,
            status="pass",
            evidence={
                **_backup_record_evidence(plan=current_plan),
                **completion_evidence,
            },
        )
    )
    phase_checkpoint(
        "backup_record_written",
        {
            "backup_record_id": current_plan.backup_record_id,
            "schedule_deployment_id": schedule_deployment_id,
        },
    )
    return OdooProdRetainedVolumeBackupImportResult(
        product=current_plan.product,
        context=current_plan.context,
        instance=current_plan.instance,
        backup_record_id=current_plan.backup_record_id,
        plan_fingerprint=current_plan.plan_fingerprint,
        import_status="pass",
        backup_status="pass",
        source_db_volume=current_plan.source_db_volume,
        source_data_volume=current_plan.source_data_volume,
        staging_clone_volume=current_plan.staging_clone_volume,
        source_database_name=current_plan.source_database_name,
        destination_database_name=current_plan.destination_database_name,
        database_user=current_plan.database_user,
        schedule_deployment_id=schedule_deployment_id,
        database_dump_sha256=completion.database_dump_sha256,
        filestore_archive_sha256=completion.filestore_archive_sha256,
        phase_evidence=phase_evidence,
    )


def _read_product_profile(
    *, record_store: OdooProdRetainedVolumeBackupImportStore, product: str
) -> LaunchplaneProductProfileRecord:
    try:
        profile = record_store.read_product_profile_record(product)
    except FileNotFoundError as error:
        raise click.ClickException(
            f"Launchplane has no product profile for {product!r}."
        ) from error
    if profile.driver_id != "odoo":
        raise click.ClickException("Retained-volume backup import requires an Odoo product.")
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
            "Retained-volume backup import requires exactly one matching prod lane."
        )
    return matching[0]


def _read_target_record(
    *, record_store: OdooProdRetainedVolumeBackupImportStore, context: str, instance: str
) -> DokployTargetRecord:
    try:
        return record_store.read_dokploy_target_record(
            context_name=context,
            instance_name=instance,
        )
    except FileNotFoundError as error:
        raise click.ClickException(
            "Retained-volume backup import target record is missing."
        ) from error


def _read_target_id_record(
    *, record_store: OdooProdRetainedVolumeBackupImportStore, context: str, instance: str
) -> DokployTargetIdRecord:
    try:
        return record_store.read_dokploy_target_id_record(
            context_name=context,
            instance_name=instance,
        )
    except FileNotFoundError as error:
        raise click.ClickException("Retained-volume backup import target id is missing.") from error


def _read_inventory(
    *, record_store: OdooProdRetainedVolumeBackupImportStore, context: str, instance: str
) -> EnvironmentInventory:
    try:
        return record_store.read_environment_inventory(
            context_name=context,
            instance_name=instance,
        )
    except FileNotFoundError as error:
        raise click.ClickException(
            "Retained-volume backup import production inventory is missing."
        ) from error


def _runtime_values_from_store(
    *,
    record_store: OdooProdRetainedVolumeBackupImportStore,
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
            "Retained-volume backup import requires DB-backed runtime environment records."
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
        return {
            "backup_root": backup_root,
            "backup_dir": "",
            "database_dump_path": "",
            "filestore_archive_path": "",
            "manifest_path": "",
        }
    backup_dir = f"{backup_root}/{database_name}/{backup_record_id}"
    return {
        "backup_root": backup_root,
        "backup_dir": backup_dir,
        "database_dump_path": f"{backup_dir}/{database_name}.dump",
        "filestore_archive_path": f"{backup_dir}/{database_name}-filestore.tar.gz",
        "manifest_path": f"{backup_dir}/manifest.json",
    }


def _filestore_relative_path(
    *,
    filestore_path: str,
    destination_database_name: str,
    blockers: list[str],
) -> str:
    if not filestore_path:
        return ""
    path = PurePosixPath(filestore_path)
    filestore_root = path.parent if path.name == destination_database_name else path
    try:
        relative_path = filestore_root.relative_to(_VOLUME_DATA_ROOT)
    except ValueError:
        blockers.append("DB-backed ODOO_FILESTORE_PATH must remain under /volumes/data.")
        return ""
    if not relative_path.parts or ".." in relative_path.parts:
        blockers.append("DB-backed ODOO_FILESTORE_PATH cannot target the data-volume root.")
        return ""
    return relative_path.as_posix()


def _backup_relative_paths(*, backup_paths: dict[str, str], blockers: list[str]) -> dict[str, str]:
    relative_paths: dict[str, str] = {}
    for key in (
        "backup_dir",
        "database_dump_path",
        "filestore_archive_path",
        "manifest_path",
    ):
        path_value = backup_paths.get(key, "")
        if not path_value:
            relative_paths[key] = ""
            continue
        try:
            relative_paths[key] = (
                PurePosixPath(path_value).relative_to(_VOLUME_DATA_ROOT).as_posix()
            )
        except ValueError:
            blockers.append("Backup artifact paths must remain on the active data volume.")
            relative_paths[key] = ""
    return relative_paths


def _relative_to_data_root(path_value: str) -> str:
    try:
        relative_path = PurePosixPath(path_value).relative_to(_VOLUME_DATA_ROOT)
    except ValueError as error:
        raise click.ClickException(
            "Retained-volume backup import artifact path escaped the active data volume."
        ) from error
    if not relative_path.parts or ".." in relative_path.parts:
        raise click.ClickException(
            "Retained-volume backup import artifact path is not safely relative."
        )
    return relative_path.as_posix()


def _validate_runtime_identity(
    *,
    runtime_identity: RuntimeIdentity,
    inventory: EnvironmentInventory,
    product: str,
    context: str,
    instance: str,
    artifact_id: str,
    blockers: list[str],
) -> None:
    if (
        runtime_identity.product != product
        or runtime_identity.context != context
        or runtime_identity.instance != instance
        or runtime_identity.environment_kind != "stable"
        or runtime_identity.deployment_record_id != inventory.deployment_record_id
        or runtime_identity.artifact_id != artifact_id
        or runtime_identity.source_git_ref != inventory.source_git_ref
    ):
        blockers.append("Current production runtime identity is internally inconsistent.")


def _live_runtime_identity_env_matches(
    *,
    live_env: dict[str, str],
    live_runtime_identity: RuntimeIdentity,
) -> bool:
    return all(
        live_env.get(key, "").strip() == expected_value
        for key, expected_value in (
            (
                "LAUNCHPLANE_DEPLOYMENT_RECORD_ID",
                live_runtime_identity.deployment_record_id,
            ),
            ("LAUNCHPLANE_ARTIFACT_ID", live_runtime_identity.artifact_id),
            ("LAUNCHPLANE_SOURCE_GIT_REF", live_runtime_identity.source_git_ref),
        )
    )


def _recorded_failed_deployment_matches_live_identity(
    *,
    record_store: OdooProdRetainedVolumeBackupImportStore,
    live_runtime_identity: RuntimeIdentity,
    inventory_runtime_identity: RuntimeIdentity,
    target_id: str,
    target_name: str,
) -> bool:
    if (
        live_runtime_identity.deployment_record_id
        == inventory_runtime_identity.deployment_record_id
        or live_runtime_identity.deployed_at.strip()
        or live_runtime_identity.model_dump(
            mode="json",
            exclude={"deployment_record_id", "deployed_at"},
        )
        != inventory_runtime_identity.model_dump(
            mode="json",
            exclude={"deployment_record_id", "deployed_at"},
        )
    ):
        return False
    try:
        deployment_record = record_store.read_deployment_record(
            live_runtime_identity.deployment_record_id
        )
    except FileNotFoundError:
        return False
    resolved_target = deployment_record.resolved_target
    artifact_identity = deployment_record.artifact_identity
    return (
        deployment_record.record_id == live_runtime_identity.deployment_record_id
        and deployment_record.context == live_runtime_identity.context
        and deployment_record.instance == live_runtime_identity.instance
        and deployment_record.source_git_ref == live_runtime_identity.source_git_ref
        and artifact_identity is not None
        and artifact_identity.artifact_id == live_runtime_identity.artifact_id
        and deployment_record.runtime_identity == live_runtime_identity
        and deployment_record.deploy.status == "fail"
        and resolved_target is not None
        and resolved_target.target_type == "compose"
        and resolved_target.target_id == target_id
        and resolved_target.target_name == target_name
    )


def _artifact_image_reference(manifest: ArtifactIdentityManifest) -> str:
    return f"{manifest.image.repository}@{manifest.image.digest}"


def _target_definition(
    *, target_record: DokployTargetRecord, target_id_record: DokployTargetIdRecord
) -> dokploy_source.DokployTargetDefinition:
    payload = target_record.model_dump(
        mode="json",
        exclude={"schema_version", "updated_at", "source_label"},
    )
    payload["target_id"] = target_id_record.target_id
    return dokploy_source.DokployTargetDefinition.model_validate(payload)


def _target_definition_from_plan(
    plan: OdooProdRetainedVolumeBackupImportPlan,
) -> dokploy_source.DokployTargetDefinition:
    return dokploy_source.DokployTargetDefinition(
        context=plan.context,
        instance=plan.instance,
        target_id=plan.target_id,
        target_name=plan.target_name,
        target_type="compose",
    )


def _validate_inspection_evidence(
    *,
    request: OdooProdRetainedVolumeBackupImportRequest,
    evidence: OdooProdRetainedVolumeBackupImportInspectionEvidence,
    expected_inspection_nonce: str,
    destination_database_name: str,
    database_user: str,
    active_db_volume: str,
    active_data_volume: str,
    active_log_volume: str,
    blockers: list[str],
) -> None:
    exact_values = {
        "inspection_nonce": expected_inspection_nonce,
        "backup_record_id": request.backup_record_id,
        "active_db_volume": active_db_volume,
        "active_data_volume": active_data_volume,
        "active_log_volume": active_log_volume,
        "source_db_volume": request.source_db_volume,
        "source_data_volume": request.source_data_volume,
        "staging_clone_volume": request.staging_clone_volume,
        "source_database_name": request.source_database_name,
        "destination_database_name": destination_database_name,
        "database_user": database_user,
    }
    if any(getattr(evidence, key) != value for key, value in exact_values.items()):
        blockers.append("Provider inspection did not match the exact reviewed request.")
    if (
        evidence.source_db_project_label != request.expected_source_compose_project
        or evidence.source_data_project_label != request.expected_source_compose_project
        or evidence.source_db_role_label != "odoo_db"
        or evidence.source_data_role_label != "odoo_data"
    ):
        blockers.append("Retained source volumes do not have the required compose labels.")
    if evidence.source_pg_version != "17":
        blockers.append("Retained source DB volume is not a PostgreSQL 17 cluster.")
    if evidence.source_pg_cluster_state != "shut down":
        blockers.append("Retained source DB volume was not cleanly shut down.")
    if not evidence.staging_clone_volume_absent:
        blockers.append("The reviewed staging clone volume already exists.")
    if not evidence.backup_destination_absent:
        blockers.append("The reviewed backup destination already exists.")
    if evidence.source_db_volume_used_bytes < 1:
        blockers.append("Retained source DB volume did not expose non-empty cluster data.")


def _inspection_plan_values(
    evidence: OdooProdRetainedVolumeBackupImportInspectionEvidence | None,
) -> dict[str, object]:
    if evidence is None:
        return {}
    return evidence.model_dump(
        mode="json",
        exclude={
            "schema_version",
            "backup_record_id",
            "active_db_volume",
            "active_data_volume",
            "active_log_volume",
            "source_db_volume",
            "source_data_volume",
            "staging_clone_volume",
            "source_database_name",
            "destination_database_name",
            "database_user",
        },
    )


def _plan_steps(*, blocked: bool) -> tuple[OdooProdRetainedVolumeBackupImportStep, ...]:
    status: Literal["ready", "blocked"] = "blocked" if blocked else "ready"
    return (
        OdooProdRetainedVolumeBackupImportStep(
            step_id="inspect-retained-volumes",
            status=status,
            message=(
                "Mount retained DB/data volumes read-only and bind compose labels, PostgreSQL 17 control metadata, and filestore size."
            ),
        ),
        OdooProdRetainedVolumeBackupImportStep(
            step_id="clone-retained-db-volume",
            status=status,
            message=(
                "Create a fresh recovery-labeled staging volume and copy the retained DB cluster without modifying the source."
            ),
        ),
        OdooProdRetainedVolumeBackupImportStep(
            step_id="capture-standard-backup",
            status=status,
            message=(
                "Start PostgreSQL only on the clone and write a standard logical dump, renamed filestore archive, and schema-v1 manifest."
            ),
        ),
        OdooProdRetainedVolumeBackupImportStep(
            step_id="preserve-recovery-clone",
            status=status,
            message=(
                "Remove the isolated clone container/network while preserving the staging volume for recovery evidence."
            ),
        ),
    )


def _backup_gate_record(
    *,
    plan: OdooProdRetainedVolumeBackupImportPlan,
    status: str,
    evidence: dict[str, str],
) -> BackupGateRecord:
    return BackupGateRecord.model_validate(
        {
            "record_id": plan.backup_record_id,
            "context": plan.context,
            "instance": plan.instance,
            "created_at": utc_now_timestamp(),
            "source": RETAINED_VOLUME_BACKUP_IMPORT_SOURCE,
            "required": True,
            "status": status,
            "evidence": evidence,
        }
    )


def _backup_record_evidence(*, plan: OdooProdRetainedVolumeBackupImportPlan) -> dict[str, str]:
    runtime_identity = plan.runtime_identity
    return {
        "database_name": plan.destination_database_name,
        "filestore_path": plan.filestore_path,
        "backup_root": plan.backup_root,
        "backup_dir": plan.backup_dir,
        "database_dump_path": plan.database_dump_path,
        "filestore_archive_path": plan.filestore_archive_path,
        "manifest_path": plan.manifest_path,
        "plan_fingerprint": plan.plan_fingerprint,
        "expected_current_artifact_id": plan.expected_current_artifact_id,
        "runtime_deployment_record_id": (
            runtime_identity.deployment_record_id if runtime_identity is not None else ""
        ),
        "source_db_volume": plan.source_db_volume,
        "source_data_volume": plan.source_data_volume,
        "staging_clone_volume": plan.staging_clone_volume,
        "source_database_name": plan.source_database_name,
        "destination_database_name": plan.destination_database_name,
        "database_user": plan.database_user,
        "source_compose_project": plan.expected_source_compose_project,
        "source_db_project_label": plan.source_db_project_label,
        "source_db_role_label": plan.source_db_role_label,
        "source_data_project_label": plan.source_data_project_label,
        "source_data_role_label": plan.source_data_role_label,
        "source_pg_version": plan.source_pg_version,
        "source_pg_control_sha256": plan.source_pg_control_sha256,
        "source_pg_control_size": str(plan.source_pg_control_size),
        "source_pg_system_identifier": plan.source_pg_system_identifier,
        "source_pg_cluster_state": plan.source_pg_cluster_state,
        "source_pg_checkpoint_location": plan.source_pg_checkpoint_location,
        "source_pg_checkpoint_redo_location": plan.source_pg_checkpoint_redo_location,
        "source_pg_checkpoint_timeline_id": plan.source_pg_checkpoint_timeline_id,
        "source_pg_checkpoint_time": plan.source_pg_checkpoint_time,
        "source_filestore_file_count": str(plan.source_filestore_file_count),
        "source_filestore_size_bytes": str(plan.source_filestore_size_bytes),
        "active_data_free_bytes": str(plan.active_data_free_bytes),
        "active_data_required_bytes": str(plan.active_data_required_bytes),
        "postgres_image_id": plan.postgres_image_id,
        "script_runner_image_id": plan.script_runner_image_id,
        "inspection_deployment_id": plan.inspection_deployment_id,
    }


def _matching_pending_import_record(
    *,
    record: BackupGateRecord,
    operation_id: str,
    request: OdooProdRetainedVolumeBackupImportRequest,
) -> bool:
    return (
        record.record_id == request.backup_record_id
        and record.context == request.context
        and record.instance == request.instance
        and record.source == RETAINED_VOLUME_BACKUP_IMPORT_SOURCE
        and record.required
        and record.status == "pending"
        and record.evidence.get("operation_id") == operation_id
    )


def _require_exact_completion(
    *,
    completion: OdooProdRetainedVolumeBackupImportCompletionEvidence,
    plan: OdooProdRetainedVolumeBackupImportPlan,
    operation_id: str,
    import_nonce: str,
) -> None:
    exact_values = {
        "import_nonce": import_nonce,
        "operation_id": operation_id,
        "plan_fingerprint": plan.plan_fingerprint,
        "backup_record_id": plan.backup_record_id,
        "source_db_volume": plan.source_db_volume,
        "source_data_volume": plan.source_data_volume,
        "staging_clone_volume": plan.staging_clone_volume,
        "source_database_name": plan.source_database_name,
        "destination_database_name": plan.destination_database_name,
        "database_user": plan.database_user,
        "source_pg_control_sha256": plan.source_pg_control_sha256,
        "source_filestore_file_count": plan.source_filestore_file_count,
        "source_filestore_size_bytes": plan.source_filestore_size_bytes,
    }
    if any(getattr(completion, key) != value for key, value in exact_values.items()):
        raise click.ClickException(
            "Odoo retained-volume backup import completion did not match the exact request."
        )
