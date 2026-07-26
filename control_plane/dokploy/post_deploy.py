import base64
import json
import re
import shlex

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Literal, TypeVar

import click

from control_plane.contracts.odoo_prod_retained_volume_backup_import import (
    ODOO_PROD_RETAINED_VOLUME_BACKUP_IMPORT_FAILURE_STAGE_BY_CODE,
    OdooProdRetainedVolumeBackupImportInspectionEvidence,
)
from control_plane.dokploy import api
from control_plane.dokploy.source import (
    DEFAULT_DOKPLOY_DEPLOY_TIMEOUT_SECONDS,
    DokployTargetDefinition,
)


DOKPLOY_DATA_WORKFLOW_SCHEDULE_NAME = "platform-data-workflow"
DOKPLOY_ODOO_BOOTSTRAP_SCHEDULE_NAME = "platform-odoo-bootstrap"
DOKPLOY_ODOO_BACKUP_GATE_SCHEDULE_NAME = "platform-odoo-backup-gate"
DOKPLOY_ODOO_BACKUP_VERIFICATION_SCHEDULE_NAME = "platform-odoo-backup-verification"
DOKPLOY_ODOO_BACKUP_RESTORE_SCHEDULE_PREFIX = "platform-odoo-backup-restore"
DOKPLOY_ODOO_RETAINED_VOLUME_BACKUP_IMPORT_INSPECT_SCHEDULE_NAME = (
    "platform-odoo-retained-volume-backup-import-inspect"
)
DOKPLOY_ODOO_RETAINED_VOLUME_BACKUP_IMPORT_APPLY_SCHEDULE_NAME = (
    "platform-odoo-retained-volume-backup-import-apply"
)
DOKPLOY_MANUAL_ONLY_CRON_EXPRESSION = "0 0 31 2 *"
DOKPLOY_RUNNING_DEPLOYMENT_STATUSES = {"pending", "queued", "running", "in_progress", "starting"}
DOKPLOY_CANCELLED_DEPLOYMENT_STATUSES = {"cancelled", "canceled"}
DOKPLOY_SUCCESS_DEPLOYMENT_STATUSES = {
    "done",
    "success",
    "succeeded",
    "completed",
    "finished",
    "healthy",
}
POST_DEPLOY_UPDATE_IGNORED_ENV_KEYS = {
    "DOKPLOY_HOST",
    "DOKPLOY_TOKEN",
}
POST_DEPLOY_UPDATE_ALLOWED_ENV_KEYS = {
    "ODOO_DB_NAME",
    "ODOO_FILESTORE_PATH",
    "ODOO_DATA_WORKFLOW_LOCK_FILE",
}
ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY = "ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64"
LAUNCHPLANE_INSTANCE_OVERRIDES_REQUIRED_ENV_KEY = "LAUNCHPLANE_INSTANCE_OVERRIDES_REQUIRED"
LAUNCHPLANE_WEBSITE_BOOTSTRAP_REQUIRED_ENV_KEY = "LAUNCHPLANE_WEBSITE_BOOTSTRAP_REQUIRED"
ODOO_RUNTIME_OVERRIDE_TARGET_ENV_KEYS = frozenset(
    {
        ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY,
        LAUNCHPLANE_INSTANCE_OVERRIDES_REQUIRED_ENV_KEY,
        LAUNCHPLANE_WEBSITE_BOOTSTRAP_REQUIRED_ENV_KEY,
    }
)
ODOO_UPSTREAM_RESTORE_WORKFLOW_ENV_KEYS = (
    "ODOO_UPSTREAM_HOST",
    "ODOO_UPSTREAM_USER",
    "ODOO_UPSTREAM_DB_NAME",
    "ODOO_UPSTREAM_DB_USER",
    "ODOO_UPSTREAM_FILESTORE_PATH",
)
DEFAULT_DATA_WORKFLOW_LOCK_PATH = "/volumes/data/.data_workflow_in_progress"
DEFAULT_ODOO_BACKUP_ROOT = "/volumes/data/backups/launchplane"
ODOO_POST_DEPLOY_BOOLEAN_READBACK_MARKERS = frozenset(
    {
        "log_available",
        "odoo_instance_overrides_payload_present",
        "website_bootstrap_domain_set",
        "website_bootstrap_domain_matches_canonical",
        "website_bootstrap_homepage_url_set",
        "website_bootstrap_homepage_url_matches",
        "website_bootstrap_homepage_page_found",
        "website_bootstrap_primary_page_xmlid_found",
        "website_bootstrap_homepage_matches_page",
        "website_bootstrap_logo_present",
        "website_bootstrap_applied",
    }
)
ODOO_POST_DEPLOY_NUMERIC_READBACK_MARKERS = frozenset({"website_bootstrap_website_id"})
ODOO_POST_DEPLOY_READBACK_MARKERS = (
    ODOO_POST_DEPLOY_BOOLEAN_READBACK_MARKERS | ODOO_POST_DEPLOY_NUMERIC_READBACK_MARKERS
)
ODOO_BACKUP_GATE_RESULT_MARKER = "LAUNCHPLANE_ODOO_BACKUP_GATE_RESULT_B64"
ODOO_BACKUP_GATE_RESULT_FIELDS = frozenset(
    {
        "schema_version",
        "backup_nonce",
        "backup_record_id",
        "database_name",
        "database_dump_sha256",
        "filestore_archive_sha256",
        "database_dump_size",
        "filestore_archive_size",
    }
)
ODOO_BACKUP_VERIFICATION_RESULT_MARKER = "LAUNCHPLANE_ODOO_BACKUP_VERIFICATION_RESULT_B64"
ODOO_BACKUP_VERIFICATION_RESULT_FIELDS = frozenset(
    {
        "schema_version",
        "verification_nonce",
        "backup_record_id",
        "database_name",
        "verification_status",
        "manifest_status",
        "sha256_status",
        "pg_restore_status",
        "tar_status",
        "staging_space_status",
        "database_dump_sha256",
        "filestore_archive_sha256",
        "database_dump_size",
        "filestore_archive_size",
        "pg_restore_entry_count",
        "filestore_member_count",
        "filestore_unpacked_size",
        "data_volume_free_bytes",
        "staging_required_bytes",
        "failure_code",
    }
)
ODOO_BACKUP_RESTORE_RESULT_MARKER = "LAUNCHPLANE_ODOO_BACKUP_RESTORE_RESULT_B64"
ODOO_BACKUP_RESTORE_RESULT_FIELDS = frozenset(
    {"schema_version", "operation_id", "phase", "evidence"}
)
ODOO_RETAINED_VOLUME_BACKUP_IMPORT_INSPECT_RESULT_MARKER = (
    "LAUNCHPLANE_ODOO_RETAINED_VOLUME_BACKUP_IMPORT_INSPECT_RESULT_B64"
)
ODOO_RETAINED_VOLUME_BACKUP_IMPORT_INSPECT_FAILURE_MARKER = (
    "LAUNCHPLANE_ODOO_RETAINED_VOLUME_BACKUP_IMPORT_INSPECT_FAILURE"
)
ODOO_RETAINED_VOLUME_BACKUP_IMPORT_INSPECT_RESULT_FIELDS = frozenset(
    {
        "schema_version",
        "inspection_nonce",
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
        "source_db_project_label",
        "source_db_role_label",
        "source_data_project_label",
        "source_data_role_label",
        "source_pg_version",
        "source_pg_control_sha256",
        "source_pg_control_size",
        "source_pg_system_identifier",
        "source_pg_cluster_state",
        "source_pg_checkpoint_location",
        "source_pg_checkpoint_redo_location",
        "source_pg_checkpoint_timeline_id",
        "source_pg_checkpoint_time",
        "source_db_volume_used_bytes",
        "source_filestore_file_count",
        "source_filestore_size_bytes",
        "active_data_free_bytes",
        "staging_clone_volume_absent",
        "backup_destination_absent",
        "postgres_image_id",
        "script_runner_image_id",
    }
)
ODOO_RETAINED_VOLUME_BACKUP_IMPORT_APPLY_RESULT_MARKER = (
    "LAUNCHPLANE_ODOO_RETAINED_VOLUME_BACKUP_IMPORT_APPLY_RESULT_B64"
)
ODOO_RETAINED_VOLUME_BACKUP_IMPORT_APPLY_RESULT_FIELDS = frozenset(
    {
        "schema_version",
        "import_nonce",
        "operation_id",
        "plan_fingerprint",
        "backup_record_id",
        "source_db_volume",
        "source_data_volume",
        "staging_clone_volume",
        "source_database_name",
        "destination_database_name",
        "database_user",
        "source_pg_control_sha256",
        "clone_pg_control_sha256",
        "database_dump_sha256",
        "filestore_archive_sha256",
        "database_dump_size",
        "filestore_archive_size",
        "source_filestore_file_count",
        "source_filestore_size_bytes",
    }
)


class OdooRetainedVolumeBackupImportInspectionFailure(click.ClickException):
    def __init__(self, *, evidence: api.JsonObject) -> None:
        self.evidence = evidence
        super().__init__(
            "Dokploy Odoo retained-volume backup import inspection returned bounded failure evidence."
        )


_RetainedInspectionCallResult = TypeVar("_RetainedInspectionCallResult")
_DOKPLOY_EVIDENCE_ID_PATTERN = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._:-]{0,199}")


def _bounded_dokploy_evidence_id(value: str) -> str:
    normalized_value = value.strip()
    if not _DOKPLOY_EVIDENCE_ID_PATTERN.fullmatch(normalized_value):
        return ""
    return normalized_value


def _retained_volume_inspection_provider_id(
    value: str,
    *,
    failure_identity: tuple[str, str] | None,
) -> str:
    if failure_identity is None:
        return value
    return _bounded_dokploy_evidence_id(value)


def _retained_volume_inspection_failure(
    *,
    failure_identity: tuple[str, str],
    failure_code: str,
    inspection_schedule_id: str = "",
    inspection_deployment_id: str = "",
) -> OdooRetainedVolumeBackupImportInspectionFailure:
    failure_stage = ODOO_PROD_RETAINED_VOLUME_BACKUP_IMPORT_FAILURE_STAGE_BY_CODE[failure_code]
    return OdooRetainedVolumeBackupImportInspectionFailure(
        evidence={
            "schema_version": 1,
            "inspection_nonce": failure_identity[0],
            "backup_record_id": failure_identity[1],
            "failure_stage": failure_stage,
            "failure_code": failure_code,
            "inspection_schedule_id": _bounded_dokploy_evidence_id(inspection_schedule_id),
            "inspection_deployment_id": _bounded_dokploy_evidence_id(inspection_deployment_id),
        }
    )


def _run_retained_volume_inspection_provider_call(
    *,
    callback: Callable[[], _RetainedInspectionCallResult],
    failure_identity: tuple[str, str] | None,
    failure_code: str,
    inspection_schedule_id: str = "",
    inspection_deployment_id: str = "",
) -> _RetainedInspectionCallResult:
    try:
        return callback()
    except OdooRetainedVolumeBackupImportInspectionFailure:
        raise
    except Exception:
        if failure_identity is None:
            raise
        raise _retained_volume_inspection_failure(
            failure_identity=failure_identity,
            failure_code=failure_code,
            inspection_schedule_id=inspection_schedule_id,
            inspection_deployment_id=inspection_deployment_id,
        ) from None


OdooBackupRestorePhase = Literal[
    "database_restore",
    "filestore_stage",
    "web_quiesce",
    "filestore_activate",
]


def run_compose_post_deploy_update(
    *,
    host: str,
    token: str,
    target_definition: DokployTargetDefinition,
    env_file: Path | None,
    workflow_environment_overrides: Mapping[str, str] | None = None,
    required_workflow_environment_keys: tuple[str, ...] = (),
    protected_shopify_store_keys: tuple[str, ...] = (),
    run_destructive_restore: bool = False,
    before_provider_mutation: Callable[[str], None] | None = None,
    deployment_title: str = "",
) -> dict[str, str]:
    compose_id = target_definition.target_id.strip()
    compose_name = (
        target_definition.target_name.strip()
        or f"{target_definition.context}-{target_definition.instance}"
    )
    if not compose_id:
        raise click.ClickException(
            f"Dokploy compose target {target_definition.context}/{target_definition.instance} requires target_id for post-deploy update."
        )
    target_payload = api.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type="compose",
        target_id=compose_id,
    )
    current_env_map = api.parse_dokploy_env_text(str(target_payload.get("env") or ""))
    desired_env_map = _apply_post_deploy_env_file_overrides(
        current_env_map=current_env_map,
        env_file=env_file,
    )
    resolved_workflow_environment_overrides = dict(workflow_environment_overrides or {})
    resolved_required_workflow_environment_keys = tuple(required_workflow_environment_keys)
    runtime_override_target_environment = {
        key: value
        for key, value in resolved_workflow_environment_overrides.items()
        if key in ODOO_RUNTIME_OVERRIDE_TARGET_ENV_KEYS
    }
    for key in ODOO_RUNTIME_OVERRIDE_TARGET_ENV_KEYS:
        desired_env_map.pop(key, None)
    desired_env_map.update(runtime_override_target_environment)
    if run_destructive_restore:
        upstream_restore_environment = _resolve_upstream_restore_workflow_environment(
            desired_env_map=desired_env_map,
        )
        resolved_workflow_environment_overrides.update(upstream_restore_environment)
        resolved_required_workflow_environment_keys = tuple(
            sorted(
                {
                    *resolved_required_workflow_environment_keys,
                    *ODOO_UPSTREAM_RESTORE_WORKFLOW_ENV_KEYS,
                }
            )
        )
    schedule_timeout_seconds = (
        target_definition.deploy_timeout_seconds or DEFAULT_DOKPLOY_DEPLOY_TIMEOUT_SECONDS
    )
    if desired_env_map != current_env_map:
        if before_provider_mutation is not None:
            before_provider_mutation("post_deploy_target_update")
        api.update_dokploy_target_env(
            host=host,
            token=token,
            target_type="compose",
            target_id=compose_id,
            target_payload=target_payload,
            env_text=api.serialize_dokploy_env_text(desired_env_map),
        )
        latest_compose_deployment = api.latest_deployment_for_target(
            host=host,
            token=token,
            target_type="compose",
            target_id=compose_id,
        )
        if before_provider_mutation is not None:
            before_provider_mutation("post_deploy_deploy_trigger")
        if deployment_title:
            api.trigger_deployment(
                host=host,
                token=token,
                target_type="compose",
                target_id=compose_id,
                no_cache=False,
                title=deployment_title,
            )
            api.wait_for_target_deployment(
                host=host,
                token=token,
                target_type="compose",
                target_id=compose_id,
                before_key=api.deployment_key(latest_compose_deployment),
                timeout_seconds=schedule_timeout_seconds,
                deployment_title=deployment_title,
            )
        else:
            api.trigger_deployment(
                host=host,
                token=token,
                target_type="compose",
                target_id=compose_id,
                no_cache=False,
            )
            api.wait_for_target_deployment(
                host=host,
                token=token,
                target_type="compose",
                target_id=compose_id,
                before_key=api.deployment_key(latest_compose_deployment),
                timeout_seconds=schedule_timeout_seconds,
            )
        target_payload = api.fetch_dokploy_target_payload(
            host=host,
            token=token,
            target_type="compose",
            target_id=compose_id,
        )
        refreshed_env_map = api.parse_dokploy_env_text(str(target_payload.get("env") or ""))
        missing_runtime_override_keys = sorted(
            key
            for key, value in runtime_override_target_environment.items()
            if refreshed_env_map.get(key, "") != value
        )
        if missing_runtime_override_keys:
            raise click.ClickException(
                "Compose post-deploy update did not persist runtime override key(s): "
                + ", ".join(missing_runtime_override_keys)
            )

    database_name = desired_env_map.get("ODOO_DB_NAME", "").strip()
    if not database_name:
        raise click.ClickException(
            "Compose post-deploy update requires ODOO_DB_NAME in the live target environment or explicit env file."
        )
    filestore_path = (
        desired_env_map.get("ODOO_FILESTORE_PATH") or "/volumes/data/filestore"
    ).strip() or "/volumes/data/filestore"
    data_workflow_lock_path = (
        desired_env_map.get("ODOO_DATA_WORKFLOW_LOCK_FILE") or DEFAULT_DATA_WORKFLOW_LOCK_PATH
    ).strip() or DEFAULT_DATA_WORKFLOW_LOCK_PATH
    schedule_type, schedule_lookup_id, compose_app_name, schedule_server_id = (
        _resolve_dokploy_schedule_runtime(
            host=host,
            token=token,
            compose_id=compose_id,
            compose_name=compose_name,
            target_payload=target_payload,
        )
    )
    schedule_name = DOKPLOY_DATA_WORKFLOW_SCHEDULE_NAME
    schedule_app_name = _build_dokploy_data_workflow_schedule_app_name(
        context_name=target_definition.context,
        instance_name=target_definition.instance,
    )
    existing_schedule = api.find_matching_dokploy_schedule(
        host=host,
        token=token,
        target_id=schedule_lookup_id,
        schedule_type=schedule_type,
        schedule_name=schedule_name,
        app_name=schedule_app_name,
    )
    if _has_running_schedule_deployment(existing_schedule):
        raise click.ClickException(
            "Dokploy-managed post-deploy update already has a running schedule deployment for "
            f"{target_definition.context}/{target_definition.instance}."
        )
    schedule_script = _build_dokploy_data_workflow_script(
        compose_app_name=compose_app_name,
        database_name=database_name,
        filestore_path=filestore_path,
        clear_stale_lock=_should_clear_stale_data_workflow_lock(existing_schedule),
        data_workflow_lock_path=data_workflow_lock_path,
        workflow_mode="restore" if run_destructive_restore else "maintenance",
        workflow_environment_overrides=resolved_workflow_environment_overrides,
        required_workflow_environment_keys=resolved_required_workflow_environment_keys,
        protected_shopify_store_keys=protected_shopify_store_keys,
    )
    schedule_payload: api.JsonObject = {
        "name": schedule_name,
        "cronExpression": DOKPLOY_MANUAL_ONLY_CRON_EXPRESSION,
        "appName": schedule_app_name,
        "shellType": "bash",
        "scheduleType": schedule_type,
        "command": "control-plane post-deploy update",
        "script": schedule_script,
        "serverId": schedule_server_id,
        "userId": schedule_lookup_id if schedule_type == "dokploy-server" else None,
        "enabled": False,
        "timezone": "UTC",
    }
    if before_provider_mutation is not None:
        before_provider_mutation("post_deploy_schedule_upsert")
    schedule = api.upsert_dokploy_schedule(
        host=host,
        token=token,
        target_id=schedule_lookup_id,
        schedule_type=schedule_type,
        schedule_name=schedule_name,
        app_name=schedule_app_name,
        schedule_payload=schedule_payload,
    )
    schedule_id = api.schedule_key(schedule)
    if not schedule_id:
        raise click.ClickException(
            f"Dokploy schedule {schedule_name!r} for {target_definition.context}/{target_definition.instance} did not expose a schedule id."
        )
    latest_schedule_deployment = api.latest_deployment_for_schedule(
        host=host,
        token=token,
        schedule_id=schedule_id,
    )
    if before_provider_mutation is not None:
        before_provider_mutation("post_deploy_schedule_trigger")
    api.dokploy_request(
        host=host,
        token=token,
        path="/api/schedule.runManually",
        method="POST",
        payload={"scheduleId": schedule_id},
        timeout_seconds=schedule_timeout_seconds,
    )
    completed_schedule_deployment_key = api.deployment_key_from_wait_result(
        api.wait_for_dokploy_schedule_deployment(
            host=host,
            token=token,
            schedule_id=schedule_id,
            before_key=api.deployment_key(latest_schedule_deployment),
            timeout_seconds=schedule_timeout_seconds,
        )
    )
    if not completed_schedule_deployment_key:
        raise click.ClickException("Dokploy schedule deployment completed without a deployment id.")
    completed_schedule_deployment = api.latest_deployment_for_schedule(
        host=host,
        token=token,
        schedule_id=schedule_id,
    )
    if api.deployment_key(completed_schedule_deployment) != completed_schedule_deployment_key:
        completed_schedule_deployment = None
    return _read_odoo_post_deploy_log_markers(
        host=host,
        token=token,
        schedule_id=schedule_id,
        schedule_deployment_key=completed_schedule_deployment_key,
        deployment_id=completed_schedule_deployment_key,
        deployment=completed_schedule_deployment,
    )


def run_compose_odoo_stable_bootstrap(
    *,
    host: str,
    token: str,
    target_definition: DokployTargetDefinition,
    env_file: Path | None,
    workflow_environment_overrides: Mapping[str, str] | None = None,
    required_workflow_environment_keys: tuple[str, ...] = (),
    timeout_seconds: int | None = None,
) -> None:
    compose_id = target_definition.target_id.strip()
    compose_name = (
        target_definition.target_name.strip()
        or f"{target_definition.context}-{target_definition.instance}"
    )
    if not compose_id:
        raise click.ClickException(
            f"Dokploy compose target {target_definition.context}/{target_definition.instance} requires target_id for Odoo bootstrap."
        )
    target_payload = api.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type="compose",
        target_id=compose_id,
    )
    observed_compose_name = str(target_payload.get("name") or "").strip()
    if observed_compose_name and observed_compose_name != compose_name:
        raise click.ClickException(
            "Odoo stable bootstrap target proof failed: live Dokploy compose name "
            f"{observed_compose_name!r} does not match expected {compose_name!r}."
        )
    current_env_map = api.parse_dokploy_env_text(str(target_payload.get("env") or ""))
    desired_env_map = _apply_post_deploy_env_file_overrides(
        current_env_map=current_env_map,
        env_file=env_file,
    )
    schedule_timeout_seconds = (
        timeout_seconds
        or target_definition.deploy_timeout_seconds
        or DEFAULT_DOKPLOY_DEPLOY_TIMEOUT_SECONDS
    )
    if desired_env_map != current_env_map:
        api.update_dokploy_target_env(
            host=host,
            token=token,
            target_type="compose",
            target_id=compose_id,
            target_payload=target_payload,
            env_text=api.serialize_dokploy_env_text(desired_env_map),
        )
        latest_compose_deployment = api.latest_deployment_for_target(
            host=host,
            token=token,
            target_type="compose",
            target_id=compose_id,
        )
        api.trigger_deployment(
            host=host,
            token=token,
            target_type="compose",
            target_id=compose_id,
            no_cache=False,
        )
        api.wait_for_target_deployment(
            host=host,
            token=token,
            target_type="compose",
            target_id=compose_id,
            before_key=api.deployment_key(latest_compose_deployment),
            timeout_seconds=schedule_timeout_seconds,
        )

    database_name = desired_env_map.get("ODOO_DB_NAME", "").strip()
    if not database_name:
        raise click.ClickException(
            "Odoo stable bootstrap requires ODOO_DB_NAME in the live target environment or explicit env file."
        )
    filestore_path = (
        desired_env_map.get("ODOO_FILESTORE_PATH") or "/volumes/data/filestore"
    ).strip() or "/volumes/data/filestore"
    data_workflow_lock_path = (
        desired_env_map.get("ODOO_DATA_WORKFLOW_LOCK_FILE") or DEFAULT_DATA_WORKFLOW_LOCK_PATH
    ).strip() or DEFAULT_DATA_WORKFLOW_LOCK_PATH
    schedule_type, schedule_lookup_id, compose_app_name, schedule_server_id = (
        _resolve_dokploy_schedule_runtime(
            host=host,
            token=token,
            compose_id=compose_id,
            compose_name=compose_name,
            target_payload=target_payload,
        )
    )
    schedule_app_name = (
        f"platform-{target_definition.context}-{target_definition.instance}-odoo-bootstrap"
    )
    existing_schedule = api.find_matching_dokploy_schedule(
        host=host,
        token=token,
        target_id=schedule_lookup_id,
        schedule_type=schedule_type,
        schedule_name=DOKPLOY_ODOO_BOOTSTRAP_SCHEDULE_NAME,
        app_name=schedule_app_name,
    )
    if _has_running_schedule_deployment(existing_schedule):
        raise click.ClickException(
            "Dokploy-managed Odoo bootstrap already has a running schedule deployment for "
            f"{target_definition.context}/{target_definition.instance}."
        )
    schedule_script = _build_dokploy_data_workflow_script(
        compose_app_name=compose_app_name,
        database_name=database_name,
        filestore_path=filestore_path,
        clear_stale_lock=_should_clear_stale_data_workflow_lock(existing_schedule),
        data_workflow_lock_path=data_workflow_lock_path,
        workflow_mode="bootstrap",
        workflow_environment_overrides=workflow_environment_overrides or {},
        required_workflow_environment_keys=required_workflow_environment_keys,
    )
    schedule_payload: api.JsonObject = {
        "name": DOKPLOY_ODOO_BOOTSTRAP_SCHEDULE_NAME,
        "cronExpression": DOKPLOY_MANUAL_ONLY_CRON_EXPRESSION,
        "appName": schedule_app_name,
        "shellType": "bash",
        "scheduleType": schedule_type,
        "command": "control-plane odoo stable bootstrap",
        "script": schedule_script,
        "serverId": schedule_server_id,
        "userId": schedule_lookup_id if schedule_type == "dokploy-server" else None,
        "enabled": False,
        "timezone": "UTC",
    }
    schedule = api.upsert_dokploy_schedule(
        host=host,
        token=token,
        target_id=schedule_lookup_id,
        schedule_type=schedule_type,
        schedule_name=DOKPLOY_ODOO_BOOTSTRAP_SCHEDULE_NAME,
        app_name=schedule_app_name,
        schedule_payload=schedule_payload,
    )
    schedule_id = api.schedule_key(schedule)
    if not schedule_id:
        raise click.ClickException(
            f"Dokploy Odoo bootstrap schedule for {target_definition.context}/{target_definition.instance} did not expose a schedule id."
        )
    latest_schedule_deployment = api.latest_deployment_for_schedule(
        host=host,
        token=token,
        schedule_id=schedule_id,
    )
    api.dokploy_request(
        host=host,
        token=token,
        path="/api/schedule.runManually",
        method="POST",
        payload={"scheduleId": schedule_id},
        timeout_seconds=schedule_timeout_seconds,
    )
    api.wait_for_dokploy_schedule_deployment(
        host=host,
        token=token,
        schedule_id=schedule_id,
        before_key=api.deployment_key(latest_schedule_deployment),
        timeout_seconds=schedule_timeout_seconds,
    )


def run_compose_odoo_backup_gate(
    *,
    host: str,
    token: str,
    target_definition: DokployTargetDefinition,
    backup_nonce: str,
    backup_record_id: str,
    database_name: str,
    filestore_path: str,
    backup_root: str,
    timeout_seconds: int | None = None,
) -> api.JsonObject:
    normalized_backup_nonce = backup_nonce.strip()
    if not normalized_backup_nonce:
        raise click.ClickException("Odoo backup gate requires a request nonce.")
    compose_id = target_definition.target_id.strip()
    compose_name = (
        target_definition.target_name.strip()
        or f"{target_definition.context}-{target_definition.instance}"
    )
    if not compose_id:
        raise click.ClickException(
            f"Dokploy compose target {target_definition.context}/{target_definition.instance} requires target_id for Odoo backup gate."
        )
    target_payload = api.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type="compose",
        target_id=compose_id,
    )
    normalized_database_name = database_name.strip()
    if not normalized_database_name:
        raise click.ClickException(
            "Odoo backup gate requires a non-empty database_name resolved from Launchplane runtime records."
        )
    normalized_filestore_path = filestore_path.strip() or "/volumes/data/filestore"
    normalized_backup_root = backup_root.strip() or DEFAULT_ODOO_BACKUP_ROOT
    schedule_timeout_seconds = (
        timeout_seconds
        or target_definition.deploy_timeout_seconds
        or DEFAULT_DOKPLOY_DEPLOY_TIMEOUT_SECONDS
    )
    schedule_type, schedule_lookup_id, compose_app_name, schedule_server_id = (
        _resolve_dokploy_schedule_runtime(
            host=host,
            token=token,
            compose_id=compose_id,
            compose_name=compose_name,
            target_payload=target_payload,
        )
    )
    schedule_app_name = (
        f"platform-{target_definition.context}-{target_definition.instance}-odoo-backup-gate"
    )
    schedule_script = _build_dokploy_odoo_backup_gate_script(
        compose_app_name=compose_app_name,
        backup_nonce=normalized_backup_nonce,
        database_name=normalized_database_name,
        filestore_path=normalized_filestore_path,
        backup_root=normalized_backup_root,
        backup_record_id=backup_record_id,
    )
    schedule_payload: api.JsonObject = {
        "name": DOKPLOY_ODOO_BACKUP_GATE_SCHEDULE_NAME,
        "cronExpression": DOKPLOY_MANUAL_ONLY_CRON_EXPRESSION,
        "appName": schedule_app_name,
        "shellType": "bash",
        "scheduleType": schedule_type,
        "command": "control-plane odoo backup gate",
        "script": schedule_script,
        "serverId": schedule_server_id,
        "userId": schedule_lookup_id if schedule_type == "dokploy-server" else None,
        "enabled": False,
        "timezone": "UTC",
    }
    schedule = api.upsert_dokploy_schedule(
        host=host,
        token=token,
        target_id=schedule_lookup_id,
        schedule_type=schedule_type,
        schedule_name=DOKPLOY_ODOO_BACKUP_GATE_SCHEDULE_NAME,
        app_name=schedule_app_name,
        schedule_payload=schedule_payload,
    )
    schedule_id = api.schedule_key(schedule)
    if not schedule_id:
        raise click.ClickException(
            f"Dokploy Odoo backup gate schedule for {target_definition.context}/{target_definition.instance} did not expose a schedule id."
        )
    latest_schedule_deployment = api.latest_deployment_for_schedule(
        host=host,
        token=token,
        schedule_id=schedule_id,
    )
    api.dokploy_request(
        host=host,
        token=token,
        path="/api/schedule.runManually",
        method="POST",
        payload={"scheduleId": schedule_id},
        timeout_seconds=schedule_timeout_seconds,
    )
    wait_result = api.wait_for_dokploy_schedule_deployment(
        host=host,
        token=token,
        schedule_id=schedule_id,
        before_key=api.deployment_key(latest_schedule_deployment),
        timeout_seconds=schedule_timeout_seconds,
    )
    deployment_id = api.deployment_key_from_wait_result(wait_result)
    if not deployment_id:
        raise click.ClickException(
            "Dokploy Odoo backup gate did not return an exact deployment id."
        )
    log_lines = api.fetch_dokploy_deployment_logs(
        host=host,
        token=token,
        deployment_id=deployment_id,
        line_count=api.MAX_DOKPLOY_LOG_LINE_COUNT,
    )
    return extract_odoo_backup_gate_result({"logs": list(log_lines)})


def extract_odoo_backup_gate_result(payload: api.JsonValue) -> api.JsonObject:
    encoded_results: list[str] = []
    marker_prefix = f"{ODOO_BACKUP_GATE_RESULT_MARKER}="
    for line in api.normalize_dokploy_log_payload(payload):
        normalized_line = line.strip()
        if normalized_line.startswith(marker_prefix):
            encoded_results.append(normalized_line.removeprefix(marker_prefix).strip())
    if len(encoded_results) != 1:
        raise click.ClickException("Dokploy Odoo backup gate returned no unique bounded result.")
    try:
        decoded_payload = base64.b64decode(encoded_results[0], validate=True)
        parsed_payload = json.loads(decoded_payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise click.ClickException(
            "Dokploy Odoo backup gate returned an invalid bounded result."
        ) from error
    result = api.as_json_object(parsed_payload)
    if result is None or set(result) != ODOO_BACKUP_GATE_RESULT_FIELDS:
        raise click.ClickException(
            "Dokploy Odoo backup gate returned an unexpected bounded result shape."
        )
    return result


def run_compose_odoo_backup_verification(
    *,
    host: str,
    token: str,
    target_definition: DokployTargetDefinition,
    verification_nonce: str,
    backup_record_id: str,
    database_name: str,
    filestore_path: str,
    backup_dir: str,
    database_dump_path: str,
    filestore_archive_path: str,
    manifest_path: str,
    timeout_seconds: int | None = None,
) -> api.JsonObject:
    normalized_verification_nonce = verification_nonce.strip()
    if not normalized_verification_nonce:
        raise click.ClickException("Odoo backup verification requires a request nonce.")
    compose_id = target_definition.target_id.strip()
    compose_name = (
        target_definition.target_name.strip()
        or f"{target_definition.context}-{target_definition.instance}"
    )
    if not compose_id:
        raise click.ClickException(
            "Dokploy compose target requires target_id for Odoo backup verification."
        )
    target_payload = api.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type="compose",
        target_id=compose_id,
    )
    schedule_timeout_seconds = (
        timeout_seconds
        or target_definition.deploy_timeout_seconds
        or DEFAULT_DOKPLOY_DEPLOY_TIMEOUT_SECONDS
    )
    schedule_type, schedule_lookup_id, compose_app_name, schedule_server_id = (
        _resolve_dokploy_schedule_runtime(
            host=host,
            token=token,
            compose_id=compose_id,
            compose_name=compose_name,
            target_payload=target_payload,
        )
    )
    schedule_app_name = f"platform-{target_definition.context}-{target_definition.instance}-odoo-backup-verification"
    schedule_payload: api.JsonObject = {
        "name": DOKPLOY_ODOO_BACKUP_VERIFICATION_SCHEDULE_NAME,
        "cronExpression": DOKPLOY_MANUAL_ONLY_CRON_EXPRESSION,
        "appName": schedule_app_name,
        "shellType": "bash",
        "scheduleType": schedule_type,
        "command": "control-plane odoo backup verification",
        "script": _build_dokploy_odoo_backup_verification_script(
            compose_app_name=compose_app_name,
            verification_nonce=normalized_verification_nonce,
            backup_record_id=backup_record_id,
            database_name=database_name,
            filestore_path=filestore_path,
            backup_dir=backup_dir,
            database_dump_path=database_dump_path,
            filestore_archive_path=filestore_archive_path,
            manifest_path=manifest_path,
        ),
        "serverId": schedule_server_id,
        "userId": schedule_lookup_id if schedule_type == "dokploy-server" else None,
        "enabled": False,
        "timezone": "UTC",
    }
    schedule = api.upsert_dokploy_schedule(
        host=host,
        token=token,
        target_id=schedule_lookup_id,
        schedule_type=schedule_type,
        schedule_name=DOKPLOY_ODOO_BACKUP_VERIFICATION_SCHEDULE_NAME,
        app_name=schedule_app_name,
        schedule_payload=schedule_payload,
    )
    schedule_id = api.schedule_key(schedule)
    if not schedule_id:
        raise click.ClickException("Dokploy Odoo backup verification schedule has no schedule id.")
    latest_schedule_deployment = api.latest_deployment_for_schedule(
        host=host,
        token=token,
        schedule_id=schedule_id,
    )
    api.dokploy_request(
        host=host,
        token=token,
        path="/api/schedule.runManually",
        method="POST",
        payload={"scheduleId": schedule_id},
        timeout_seconds=schedule_timeout_seconds,
    )
    wait_result = api.wait_for_dokploy_schedule_deployment(
        host=host,
        token=token,
        schedule_id=schedule_id,
        before_key=api.deployment_key(latest_schedule_deployment),
        timeout_seconds=schedule_timeout_seconds,
    )
    deployment_id = api.deployment_key_from_wait_result(wait_result)
    if not deployment_id:
        raise click.ClickException(
            "Dokploy Odoo backup verification did not return an exact deployment id."
        )
    log_lines = api.fetch_dokploy_deployment_logs(
        host=host,
        token=token,
        deployment_id=deployment_id,
        line_count=api.MAX_DOKPLOY_LOG_LINE_COUNT,
    )
    return extract_odoo_backup_verification_result({"logs": list(log_lines)})


def extract_odoo_backup_verification_result(payload: api.JsonValue) -> api.JsonObject:
    encoded_results: list[str] = []
    marker_prefix = f"{ODOO_BACKUP_VERIFICATION_RESULT_MARKER}="
    for line in api.normalize_dokploy_log_payload(payload):
        normalized_line = line.strip()
        if normalized_line.startswith(marker_prefix):
            encoded_results.append(normalized_line.removeprefix(marker_prefix).strip())
    if len(encoded_results) != 1:
        raise click.ClickException(
            "Dokploy Odoo backup verification returned no unique bounded result."
        )
    try:
        decoded_payload = base64.b64decode(encoded_results[0], validate=True)
        parsed_payload = json.loads(decoded_payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise click.ClickException(
            "Dokploy Odoo backup verification returned an invalid bounded result."
        ) from error
    result = api.as_json_object(parsed_payload)
    if result is None or set(result) != ODOO_BACKUP_VERIFICATION_RESULT_FIELDS:
        raise click.ClickException(
            "Dokploy Odoo backup verification returned an unexpected bounded result shape."
        )
    return result


def run_compose_odoo_retained_volume_backup_import_inspection(
    *,
    host: str,
    token: str,
    target_definition: DokployTargetDefinition,
    expected_compose_app_name: str,
    inspection_nonce: str,
    backup_record_id: str,
    active_db_volume: str,
    active_data_volume: str,
    active_log_volume: str,
    source_db_volume: str,
    source_data_volume: str,
    staging_clone_volume: str,
    source_database_name: str,
    destination_database_name: str,
    database_user: str,
    expected_source_compose_project: str,
    filestore_relative_path: str,
    backup_dir_relative_path: str,
    timeout_seconds: int | None = None,
    before_provider_mutation: Callable[[str], None] | None = None,
) -> api.JsonObject:
    failure_identity = (inspection_nonce, backup_record_id)
    compose_id = target_definition.target_id.strip()
    compose_name = (
        target_definition.target_name.strip()
        or f"{target_definition.context}-{target_definition.instance}"
    )
    if not compose_id:
        raise _retained_volume_inspection_failure(
            failure_identity=failure_identity,
            failure_code="provider_target_identity_mismatch",
        )
    target_payload = _run_retained_volume_inspection_provider_call(
        callback=lambda: api.fetch_dokploy_target_payload(
            host=host,
            token=token,
            target_type="compose",
            target_id=compose_id,
        ),
        failure_identity=failure_identity,
        failure_code="provider_target_read_failed",
    )
    _, _, compose_app_name, _ = _run_retained_volume_inspection_provider_call(
        callback=lambda: _resolve_dokploy_schedule_runtime(
            host=host,
            token=token,
            compose_id=compose_id,
            compose_name=compose_name,
            target_payload=target_payload,
        ),
        failure_identity=failure_identity,
        failure_code="provider_schedule_runtime_resolution_failed",
    )
    if compose_app_name != expected_compose_app_name.strip():
        raise _retained_volume_inspection_failure(
            failure_identity=failure_identity,
            failure_code="provider_target_identity_mismatch",
        )
    result, deployment_id = _run_compose_odoo_retained_volume_backup_import_schedule(
        host=host,
        token=token,
        target_definition=target_definition,
        schedule_name=DOKPLOY_ODOO_RETAINED_VOLUME_BACKUP_IMPORT_INSPECT_SCHEDULE_NAME,
        command="control-plane odoo retained-volume backup import inspect",
        script=_build_dokploy_odoo_retained_volume_backup_import_inspection_script(
            compose_app_name=compose_app_name,
            inspection_nonce=inspection_nonce,
            backup_record_id=backup_record_id,
            active_db_volume=active_db_volume,
            active_data_volume=active_data_volume,
            active_log_volume=active_log_volume,
            source_db_volume=source_db_volume,
            source_data_volume=source_data_volume,
            staging_clone_volume=staging_clone_volume,
            source_database_name=source_database_name,
            destination_database_name=destination_database_name,
            database_user=database_user,
            expected_source_compose_project=expected_source_compose_project,
            filestore_relative_path=filestore_relative_path,
            backup_dir_relative_path=backup_dir_relative_path,
        ),
        marker=ODOO_RETAINED_VOLUME_BACKUP_IMPORT_INSPECT_RESULT_MARKER,
        expected_fields=ODOO_RETAINED_VOLUME_BACKUP_IMPORT_INSPECT_RESULT_FIELDS,
        failure_marker=ODOO_RETAINED_VOLUME_BACKUP_IMPORT_INSPECT_FAILURE_MARKER,
        failure_identity=failure_identity,
        timeout_seconds=timeout_seconds,
        before_provider_mutation=before_provider_mutation,
    )
    result["inspection_deployment_id"] = deployment_id
    return result


def run_compose_odoo_retained_volume_backup_import_apply(
    *,
    host: str,
    token: str,
    target_definition: DokployTargetDefinition,
    expected_compose_app_name: str,
    import_nonce: str,
    operation_id: str,
    plan_fingerprint: str,
    backup_record_id: str,
    active_db_volume: str,
    active_data_volume: str,
    active_log_volume: str,
    source_db_volume: str,
    source_data_volume: str,
    staging_clone_volume: str,
    source_database_name: str,
    destination_database_name: str,
    database_user: str,
    expected_source_compose_project: str,
    expected_source_pg_control_sha256: str,
    expected_source_pg_version: str,
    expected_postgres_image_id: str,
    expected_script_runner_image_id: str,
    expected_source_filestore_file_count: int,
    expected_source_filestore_size_bytes: int,
    expected_active_data_required_bytes: int,
    filestore_relative_path: str,
    backup_dir_relative_path: str,
    database_dump_relative_path: str,
    filestore_archive_relative_path: str,
    manifest_relative_path: str,
    timeout_seconds: int | None = None,
    before_provider_mutation: Callable[[str], None] | None = None,
) -> api.JsonObject:
    compose_app_name = _compose_app_name_for_restore(
        host=host,
        token=token,
        target_definition=target_definition,
    )
    if compose_app_name != expected_compose_app_name.strip():
        raise click.ClickException(
            "Odoo retained-volume backup import compose project drifted before apply."
        )
    result, deployment_id = _run_compose_odoo_retained_volume_backup_import_schedule(
        host=host,
        token=token,
        target_definition=target_definition,
        schedule_name=DOKPLOY_ODOO_RETAINED_VOLUME_BACKUP_IMPORT_APPLY_SCHEDULE_NAME,
        command="control-plane odoo retained-volume backup import apply",
        script=_build_dokploy_odoo_retained_volume_backup_import_apply_script(
            compose_app_name=compose_app_name,
            import_nonce=import_nonce,
            operation_id=operation_id,
            plan_fingerprint=plan_fingerprint,
            backup_record_id=backup_record_id,
            active_db_volume=active_db_volume,
            active_data_volume=active_data_volume,
            active_log_volume=active_log_volume,
            source_db_volume=source_db_volume,
            source_data_volume=source_data_volume,
            staging_clone_volume=staging_clone_volume,
            source_database_name=source_database_name,
            destination_database_name=destination_database_name,
            database_user=database_user,
            expected_source_compose_project=expected_source_compose_project,
            expected_source_pg_control_sha256=expected_source_pg_control_sha256,
            expected_source_pg_version=expected_source_pg_version,
            expected_postgres_image_id=expected_postgres_image_id,
            expected_script_runner_image_id=expected_script_runner_image_id,
            expected_source_filestore_file_count=expected_source_filestore_file_count,
            expected_source_filestore_size_bytes=expected_source_filestore_size_bytes,
            expected_active_data_required_bytes=expected_active_data_required_bytes,
            filestore_relative_path=filestore_relative_path,
            backup_dir_relative_path=backup_dir_relative_path,
            database_dump_relative_path=database_dump_relative_path,
            filestore_archive_relative_path=filestore_archive_relative_path,
            manifest_relative_path=manifest_relative_path,
        ),
        marker=ODOO_RETAINED_VOLUME_BACKUP_IMPORT_APPLY_RESULT_MARKER,
        expected_fields=ODOO_RETAINED_VOLUME_BACKUP_IMPORT_APPLY_RESULT_FIELDS,
        timeout_seconds=timeout_seconds,
        before_provider_mutation=before_provider_mutation,
    )
    result["schedule_deployment_id"] = deployment_id
    return result


def _run_compose_odoo_retained_volume_backup_import_schedule(
    *,
    host: str,
    token: str,
    target_definition: DokployTargetDefinition,
    schedule_name: str,
    command: str,
    script: str,
    marker: str,
    expected_fields: frozenset[str],
    timeout_seconds: int | None,
    before_provider_mutation: Callable[[str], None] | None,
    failure_marker: str = "",
    failure_identity: tuple[str, str] | None = None,
) -> tuple[api.JsonObject, str]:
    compose_id = target_definition.target_id.strip()
    compose_name = (
        target_definition.target_name.strip()
        or f"{target_definition.context}-{target_definition.instance}"
    )
    if not compose_id:
        raise click.ClickException(
            "Odoo retained-volume backup import requires an exact compose target id."
        )
    target_payload = _run_retained_volume_inspection_provider_call(
        callback=lambda: api.fetch_dokploy_target_payload(
            host=host,
            token=token,
            target_type="compose",
            target_id=compose_id,
        ),
        failure_identity=failure_identity,
        failure_code="provider_target_read_failed",
    )
    schedule_timeout_seconds = (
        timeout_seconds
        or target_definition.deploy_timeout_seconds
        or DEFAULT_DOKPLOY_DEPLOY_TIMEOUT_SECONDS
    )
    schedule_type, schedule_lookup_id, _, schedule_server_id = (
        _run_retained_volume_inspection_provider_call(
            callback=lambda: _resolve_dokploy_schedule_runtime(
                host=host,
                token=token,
                compose_id=compose_id,
                compose_name=compose_name,
                target_payload=target_payload,
            ),
            failure_identity=failure_identity,
            failure_code="provider_schedule_runtime_resolution_failed",
        )
    )
    schedule_payload: api.JsonObject = {
        "name": schedule_name,
        "cronExpression": DOKPLOY_MANUAL_ONLY_CRON_EXPRESSION,
        "appName": (
            f"platform-{target_definition.context}-{target_definition.instance}-"
            "odoo-retained-volume-backup-import"
        ),
        "shellType": "bash",
        "scheduleType": schedule_type,
        "command": command,
        "script": script,
        "serverId": schedule_server_id,
        "userId": schedule_lookup_id if schedule_type == "dokploy-server" else None,
        "enabled": False,
        "timezone": "UTC",
    }
    if before_provider_mutation is not None:
        before_provider_mutation("schedule_upsert")
    schedule = _run_retained_volume_inspection_provider_call(
        callback=lambda: api.upsert_dokploy_schedule(
            host=host,
            token=token,
            target_id=schedule_lookup_id,
            schedule_type=schedule_type,
            schedule_name=schedule_name,
            app_name=str(schedule_payload["appName"]),
            schedule_payload=schedule_payload,
        ),
        failure_identity=failure_identity,
        failure_code="provider_schedule_upsert_failed",
    )
    schedule_id = _retained_volume_inspection_provider_id(
        _run_retained_volume_inspection_provider_call(
            callback=lambda: api.schedule_key(schedule),
            failure_identity=failure_identity,
            failure_code="provider_schedule_identity_missing",
        ),
        failure_identity=failure_identity,
    )
    if not schedule_id:
        if failure_identity is not None:
            raise _retained_volume_inspection_failure(
                failure_identity=failure_identity,
                failure_code="provider_schedule_identity_missing",
            )
        raise click.ClickException(
            "Dokploy Odoo retained-volume backup import schedule has no schedule id."
        )
    latest_schedule_deployment = _run_retained_volume_inspection_provider_call(
        callback=lambda: api.latest_deployment_for_schedule(
            host=host,
            token=token,
            schedule_id=schedule_id,
        ),
        failure_identity=failure_identity,
        failure_code="provider_schedule_baseline_read_failed",
        inspection_schedule_id=schedule_id,
    )
    raw_before_deployment_id = _run_retained_volume_inspection_provider_call(
        callback=lambda: api.deployment_key(latest_schedule_deployment),
        failure_identity=failure_identity,
        failure_code="provider_schedule_baseline_read_failed",
        inspection_schedule_id=schedule_id,
    )
    before_deployment_id = _retained_volume_inspection_provider_id(
        raw_before_deployment_id,
        failure_identity=failure_identity,
    )
    if (
        failure_identity is not None
        and latest_schedule_deployment is not None
        and not before_deployment_id
    ):
        raise _retained_volume_inspection_failure(
            failure_identity=failure_identity,
            failure_code="provider_schedule_baseline_read_failed",
            inspection_schedule_id=schedule_id,
        )
    if before_provider_mutation is not None:
        before_provider_mutation("schedule_trigger")
    _run_retained_volume_inspection_provider_call(
        callback=lambda: api.dokploy_request(
            host=host,
            token=token,
            path="/api/schedule.runManually",
            method="POST",
            payload={"scheduleId": schedule_id},
            timeout_seconds=schedule_timeout_seconds,
        ),
        failure_identity=failure_identity,
        failure_code="provider_schedule_trigger_failed",
        inspection_schedule_id=schedule_id,
    )
    try:
        wait_result = api.wait_for_dokploy_schedule_deployment(
            host=host,
            token=token,
            schedule_id=schedule_id,
            before_key=before_deployment_id,
            timeout_seconds=schedule_timeout_seconds,
        )
    except api.DokployDeploymentFailed as error:
        if not failure_marker or failure_identity is None:
            raise
        failed_deployment_id = _bounded_dokploy_evidence_id(error.deployment_id)
        if not failed_deployment_id:
            raise _retained_volume_inspection_failure(
                failure_identity=failure_identity,
                failure_code="provider_deployment_identity_missing",
                inspection_schedule_id=schedule_id,
            ) from None
        try:
            failure_log_lines = api.fetch_dokploy_deployment_logs(
                host=host,
                token=token,
                deployment_id=failed_deployment_id,
                line_count=api.MAX_DOKPLOY_LOG_LINE_COUNT,
            )
        except Exception:
            raise _retained_volume_inspection_failure(
                failure_identity=failure_identity,
                failure_code="provider_result_log_read_failed",
                inspection_schedule_id=schedule_id,
                inspection_deployment_id=failed_deployment_id,
            ) from None
        try:
            failure_evidence = _validate_retained_volume_inspection_failure_identity(
                _extract_retained_volume_inspection_failure(
                    {"logs": list(failure_log_lines)},
                    marker=failure_marker,
                ),
                failure_identity=failure_identity,
            )
        except Exception:
            raise _retained_volume_inspection_failure(
                failure_identity=failure_identity,
                failure_code="provider_result_invalid",
                inspection_schedule_id=schedule_id,
                inspection_deployment_id=failed_deployment_id,
            ) from None
        failure_evidence["inspection_schedule_id"] = schedule_id
        failure_evidence["inspection_deployment_id"] = failed_deployment_id
        raise OdooRetainedVolumeBackupImportInspectionFailure(evidence=failure_evidence) from None
    except Exception:
        if failure_identity is None:
            raise
        raise _retained_volume_inspection_failure(
            failure_identity=failure_identity,
            failure_code="provider_schedule_wait_failed",
            inspection_schedule_id=schedule_id,
        ) from None
    deployment_id = _retained_volume_inspection_provider_id(
        _run_retained_volume_inspection_provider_call(
            callback=lambda: api.deployment_key_from_wait_result(wait_result),
            failure_identity=failure_identity,
            failure_code="provider_deployment_identity_missing",
            inspection_schedule_id=schedule_id,
        ),
        failure_identity=failure_identity,
    )
    if not deployment_id:
        if failure_identity is not None:
            raise _retained_volume_inspection_failure(
                failure_identity=failure_identity,
                failure_code="provider_deployment_identity_missing",
                inspection_schedule_id=schedule_id,
            )
        raise click.ClickException(
            "Dokploy Odoo retained-volume backup import did not return an exact deployment id."
        )
    log_lines = _run_retained_volume_inspection_provider_call(
        callback=lambda: api.fetch_dokploy_deployment_logs(
            host=host,
            token=token,
            deployment_id=deployment_id,
            line_count=api.MAX_DOKPLOY_LOG_LINE_COUNT,
        ),
        failure_identity=failure_identity,
        failure_code="provider_result_log_read_failed",
        inspection_schedule_id=schedule_id,
        inspection_deployment_id=deployment_id,
    )
    if failure_marker and failure_identity is not None:
        try:
            completed_failure_evidence = _find_retained_volume_inspection_failure(
                {"logs": list(log_lines)},
                marker=failure_marker,
            )
            if completed_failure_evidence is not None:
                completed_failure_evidence = _validate_retained_volume_inspection_failure_identity(
                    completed_failure_evidence,
                    failure_identity=failure_identity,
                )
        except Exception:
            raise _retained_volume_inspection_failure(
                failure_identity=failure_identity,
                failure_code="provider_result_invalid",
                inspection_schedule_id=schedule_id,
                inspection_deployment_id=deployment_id,
            ) from None
        if completed_failure_evidence is not None:
            completed_failure_evidence["inspection_schedule_id"] = schedule_id
            completed_failure_evidence["inspection_deployment_id"] = deployment_id
            raise OdooRetainedVolumeBackupImportInspectionFailure(
                evidence=completed_failure_evidence
            ) from None
    result = _run_retained_volume_inspection_provider_call(
        callback=lambda: _extract_bounded_schedule_result(
            {"logs": list(log_lines)},
            marker=marker,
            expected_fields=expected_fields,
            label="Odoo retained-volume backup import",
        ),
        failure_identity=failure_identity,
        failure_code="provider_result_invalid",
        inspection_schedule_id=schedule_id,
        inspection_deployment_id=deployment_id,
    )
    if failure_identity is not None:
        _run_retained_volume_inspection_provider_call(
            callback=lambda: OdooProdRetainedVolumeBackupImportInspectionEvidence.model_validate(
                {**result, "inspection_deployment_id": deployment_id}
            ),
            failure_identity=failure_identity,
            failure_code="provider_result_invalid",
            inspection_schedule_id=schedule_id,
            inspection_deployment_id=deployment_id,
        )
    return result, deployment_id


def _extract_retained_volume_inspection_failure(
    payload: api.JsonValue,
    *,
    marker: str,
) -> api.JsonObject:
    failure_evidence = _find_retained_volume_inspection_failure(
        payload,
        marker=marker,
    )
    if failure_evidence is None:
        raise click.ClickException(
            "Dokploy Odoo retained-volume backup import inspection returned no unique bounded failure evidence."
        )
    return failure_evidence


def _find_retained_volume_inspection_failure(
    payload: api.JsonValue,
    *,
    marker: str,
) -> api.JsonObject | None:
    bounded_values: list[str] = []
    marker_prefix = f"{marker}="
    for line in api.normalize_dokploy_log_payload(payload):
        normalized_line = line.strip()
        if normalized_line.startswith(marker_prefix):
            bounded_values.append(normalized_line.removeprefix(marker_prefix).strip())
    if not bounded_values:
        return None
    if len(bounded_values) != 1:
        raise click.ClickException(
            "Dokploy Odoo retained-volume backup import inspection returned no unique bounded failure evidence."
        )
    parts = bounded_values[0].split("|")
    if len(parts) != 4:
        raise click.ClickException(
            "Dokploy Odoo retained-volume backup import inspection returned invalid bounded failure evidence."
        )
    inspection_nonce, backup_record_id, failure_stage, failure_code = parts
    if (
        len(inspection_nonce) != 64
        or any(character not in "0123456789abcdef" for character in inspection_nonce)
        or not backup_record_id
        or ODOO_PROD_RETAINED_VOLUME_BACKUP_IMPORT_FAILURE_STAGE_BY_CODE.get(failure_code)
        != failure_stage
    ):
        raise click.ClickException(
            "Dokploy Odoo retained-volume backup import inspection returned invalid bounded failure evidence."
        )
    return {
        "schema_version": 1,
        "inspection_nonce": inspection_nonce,
        "backup_record_id": backup_record_id,
        "failure_stage": failure_stage,
        "failure_code": failure_code,
    }


def _validate_retained_volume_inspection_failure_identity(
    failure_evidence: api.JsonObject,
    *,
    failure_identity: tuple[str, str],
) -> api.JsonObject:
    inspection_nonce, backup_record_id = failure_identity
    if (
        failure_evidence.get("inspection_nonce") != inspection_nonce
        or failure_evidence.get("backup_record_id") != backup_record_id
    ):
        raise click.ClickException(
            "Dokploy Odoo retained-volume backup import inspection returned invalid bounded failure evidence."
        )
    return failure_evidence


def _extract_bounded_schedule_result(
    payload: api.JsonValue,
    *,
    marker: str,
    expected_fields: frozenset[str],
    label: str,
) -> api.JsonObject:
    encoded_results: list[str] = []
    marker_prefix = f"{marker}="
    for line in api.normalize_dokploy_log_payload(payload):
        normalized_line = line.strip()
        if normalized_line.startswith(marker_prefix):
            encoded_results.append(normalized_line.removeprefix(marker_prefix).strip())
    if len(encoded_results) != 1:
        raise click.ClickException(f"Dokploy {label} returned no unique bounded result.")
    try:
        decoded_payload = base64.b64decode(encoded_results[0], validate=True)
        parsed_payload = json.loads(decoded_payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise click.ClickException(
            f"Dokploy {label} returned an invalid bounded result."
        ) from error
    result = api.as_json_object(parsed_payload)
    if result is None or set(result) != expected_fields:
        raise click.ClickException(f"Dokploy {label} returned an unexpected bounded result shape.")
    return result


def run_compose_odoo_backup_restore_database(
    *,
    host: str,
    token: str,
    target_definition: DokployTargetDefinition,
    operation_id: str,
    database_name: str,
    database_dump_path: str,
    database_dump_sha256: str,
    old_db_volume: str,
    new_db_volume: str,
    data_volume: str,
    log_volume: str,
    timeout_seconds: int | None = None,
    before_provider_mutation: Callable[[str], None] | None = None,
) -> dict[str, str]:
    return _run_compose_odoo_backup_restore_phase(
        host=host,
        token=token,
        target_definition=target_definition,
        operation_id=operation_id,
        phase="database_restore",
        script=_build_dokploy_odoo_backup_restore_database_script(
            compose_app_name=_compose_app_name_for_restore(
                host=host,
                token=token,
                target_definition=target_definition,
            ),
            operation_id=operation_id,
            database_name=database_name,
            database_dump_path=database_dump_path,
            database_dump_sha256=database_dump_sha256,
            old_db_volume=old_db_volume,
            new_db_volume=new_db_volume,
            data_volume=data_volume,
            log_volume=log_volume,
        ),
        timeout_seconds=timeout_seconds,
        before_provider_mutation=before_provider_mutation,
    )


def run_compose_odoo_backup_restore_filestore_stage(
    *,
    host: str,
    token: str,
    target_definition: DokployTargetDefinition,
    operation_id: str,
    database_name: str,
    filestore_path: str,
    filestore_archive_path: str,
    filestore_archive_sha256: str,
    filestore_member_count: int,
    filestore_unpacked_size: int,
    filestore_staging_path: str,
    filestore_quarantine_path: str,
    data_volume: str,
    log_volume: str,
    timeout_seconds: int | None = None,
    before_provider_mutation: Callable[[str], None] | None = None,
) -> dict[str, str]:
    return _run_compose_odoo_backup_restore_phase(
        host=host,
        token=token,
        target_definition=target_definition,
        operation_id=operation_id,
        phase="filestore_stage",
        script=_build_dokploy_odoo_backup_restore_filestore_stage_script(
            compose_app_name=_compose_app_name_for_restore(
                host=host,
                token=token,
                target_definition=target_definition,
            ),
            operation_id=operation_id,
            database_name=database_name,
            filestore_path=filestore_path,
            filestore_archive_path=filestore_archive_path,
            filestore_archive_sha256=filestore_archive_sha256,
            filestore_member_count=filestore_member_count,
            filestore_unpacked_size=filestore_unpacked_size,
            filestore_staging_path=filestore_staging_path,
            filestore_quarantine_path=filestore_quarantine_path,
            data_volume=data_volume,
            log_volume=log_volume,
        ),
        timeout_seconds=timeout_seconds,
        before_provider_mutation=before_provider_mutation,
    )


def run_compose_odoo_backup_restore_web_quiesce(
    *,
    host: str,
    token: str,
    target_definition: DokployTargetDefinition,
    operation_id: str,
    old_db_volume: str,
    data_volume: str,
    log_volume: str,
    timeout_seconds: int | None = None,
    before_provider_mutation: Callable[[str], None] | None = None,
) -> dict[str, str]:
    return _run_compose_odoo_backup_restore_phase(
        host=host,
        token=token,
        target_definition=target_definition,
        operation_id=operation_id,
        phase="web_quiesce",
        script=_build_dokploy_odoo_backup_restore_web_quiesce_script(
            compose_app_name=_compose_app_name_for_restore(
                host=host,
                token=token,
                target_definition=target_definition,
            ),
            operation_id=operation_id,
            old_db_volume=old_db_volume,
            data_volume=data_volume,
            log_volume=log_volume,
        ),
        timeout_seconds=timeout_seconds,
        before_provider_mutation=before_provider_mutation,
    )


def run_compose_odoo_backup_restore_filestore_activate(
    *,
    host: str,
    token: str,
    target_definition: DokployTargetDefinition,
    operation_id: str,
    database_name: str,
    filestore_path: str,
    filestore_staging_path: str,
    filestore_quarantine_path: str,
    data_volume: str,
    log_volume: str,
    timeout_seconds: int | None = None,
    before_provider_mutation: Callable[[str], None] | None = None,
) -> dict[str, str]:
    return _run_compose_odoo_backup_restore_phase(
        host=host,
        token=token,
        target_definition=target_definition,
        operation_id=operation_id,
        phase="filestore_activate",
        script=_build_dokploy_odoo_backup_restore_filestore_activate_script(
            compose_app_name=_compose_app_name_for_restore(
                host=host,
                token=token,
                target_definition=target_definition,
            ),
            operation_id=operation_id,
            database_name=database_name,
            filestore_path=filestore_path,
            filestore_staging_path=filestore_staging_path,
            filestore_quarantine_path=filestore_quarantine_path,
            data_volume=data_volume,
            log_volume=log_volume,
        ),
        timeout_seconds=timeout_seconds,
        before_provider_mutation=before_provider_mutation,
    )


def _compose_app_name_for_restore(
    *,
    host: str,
    token: str,
    target_definition: DokployTargetDefinition,
) -> str:
    compose_id = target_definition.target_id.strip()
    compose_name = (
        target_definition.target_name.strip()
        or f"{target_definition.context}-{target_definition.instance}"
    )
    if not compose_id:
        raise click.ClickException("Odoo backup restore requires a compose target id.")
    target_payload = api.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type="compose",
        target_id=compose_id,
    )
    _, _, compose_app_name, _ = _resolve_dokploy_schedule_runtime(
        host=host,
        token=token,
        compose_id=compose_id,
        compose_name=compose_name,
        target_payload=target_payload,
    )
    return compose_app_name


def _run_compose_odoo_backup_restore_phase(
    *,
    host: str,
    token: str,
    target_definition: DokployTargetDefinition,
    operation_id: str,
    phase: OdooBackupRestorePhase,
    script: str,
    timeout_seconds: int | None,
    before_provider_mutation: Callable[[str], None] | None,
) -> dict[str, str]:
    compose_id = target_definition.target_id.strip()
    compose_name = (
        target_definition.target_name.strip()
        or f"{target_definition.context}-{target_definition.instance}"
    )
    normalized_operation_id = operation_id.strip()
    if not compose_id or not normalized_operation_id:
        raise click.ClickException("Odoo backup restore phase requires target and operation ids.")
    target_payload = api.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type="compose",
        target_id=compose_id,
    )
    schedule_timeout_seconds = (
        timeout_seconds
        or target_definition.deploy_timeout_seconds
        or DEFAULT_DOKPLOY_DEPLOY_TIMEOUT_SECONDS
    )
    schedule_type, schedule_lookup_id, _, schedule_server_id = _resolve_dokploy_schedule_runtime(
        host=host,
        token=token,
        compose_id=compose_id,
        compose_name=compose_name,
        target_payload=target_payload,
    )
    schedule_name = f"{DOKPLOY_ODOO_BACKUP_RESTORE_SCHEDULE_PREFIX}-{phase}"
    schedule_app_name = (
        f"platform-{target_definition.context}-{target_definition.instance}-odoo-backup-restore"
    )
    schedule_payload: api.JsonObject = {
        "name": schedule_name,
        "cronExpression": DOKPLOY_MANUAL_ONLY_CRON_EXPRESSION,
        "appName": schedule_app_name,
        "shellType": "bash",
        "scheduleType": schedule_type,
        "command": f"control-plane odoo backup restore {phase}",
        "script": script,
        "serverId": schedule_server_id,
        "userId": schedule_lookup_id if schedule_type == "dokploy-server" else None,
        "enabled": False,
        "timezone": "UTC",
    }
    if before_provider_mutation is not None:
        before_provider_mutation(f"{phase}_schedule_upsert")
    schedule = api.upsert_dokploy_schedule(
        host=host,
        token=token,
        target_id=schedule_lookup_id,
        schedule_type=schedule_type,
        schedule_name=schedule_name,
        app_name=schedule_app_name,
        schedule_payload=schedule_payload,
    )
    schedule_id = api.schedule_key(schedule)
    if not schedule_id:
        raise click.ClickException(f"Dokploy Odoo backup restore {phase} schedule has no id.")
    latest_schedule_deployment = api.latest_deployment_for_schedule(
        host=host,
        token=token,
        schedule_id=schedule_id,
    )
    if before_provider_mutation is not None:
        before_provider_mutation(f"{phase}_schedule_trigger")
    api.dokploy_request(
        host=host,
        token=token,
        path="/api/schedule.runManually",
        method="POST",
        payload={"scheduleId": schedule_id},
        timeout_seconds=schedule_timeout_seconds,
    )
    wait_result = api.wait_for_dokploy_schedule_deployment(
        host=host,
        token=token,
        schedule_id=schedule_id,
        before_key=api.deployment_key(latest_schedule_deployment),
        timeout_seconds=schedule_timeout_seconds,
    )
    deployment_id = api.deployment_key_from_wait_result(wait_result)
    if not deployment_id:
        raise click.ClickException(
            f"Dokploy Odoo backup restore {phase} did not return a deployment id."
        )
    log_lines = api.fetch_dokploy_deployment_logs(
        host=host,
        token=token,
        deployment_id=deployment_id,
        line_count=api.MAX_DOKPLOY_LOG_LINE_COUNT,
    )
    return extract_odoo_backup_restore_result(
        {"logs": list(log_lines)},
        operation_id=normalized_operation_id,
        phase=phase,
    )


def extract_odoo_backup_restore_result(
    payload: api.JsonValue,
    *,
    operation_id: str,
    phase: OdooBackupRestorePhase,
) -> dict[str, str]:
    encoded_results: list[str] = []
    marker_prefix = f"{ODOO_BACKUP_RESTORE_RESULT_MARKER}="
    for line in api.normalize_dokploy_log_payload(payload):
        normalized_line = line.strip()
        if normalized_line.startswith(marker_prefix):
            encoded_results.append(normalized_line.removeprefix(marker_prefix).strip())
    if len(encoded_results) != 1:
        raise click.ClickException("Dokploy Odoo backup restore returned no unique bounded result.")
    try:
        decoded_payload = base64.b64decode(encoded_results[0], validate=True)
        parsed_payload = json.loads(decoded_payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise click.ClickException(
            "Dokploy Odoo backup restore returned an invalid bounded result."
        ) from error
    result = api.as_json_object(parsed_payload)
    if result is None or set(result) != ODOO_BACKUP_RESTORE_RESULT_FIELDS:
        raise click.ClickException(
            "Dokploy Odoo backup restore returned an unexpected bounded result shape."
        )
    if result.get("schema_version") != 1:
        raise click.ClickException("Dokploy Odoo backup restore returned an unsupported result.")
    if result.get("operation_id") != operation_id or result.get("phase") != phase:
        raise click.ClickException(
            "Dokploy Odoo backup restore result did not match the exact operation phase."
        )
    evidence = api.as_json_object(result.get("evidence"))
    if evidence is None or len(evidence) > 32:
        raise click.ClickException("Dokploy Odoo backup restore evidence was not bounded.")
    normalized_evidence: dict[str, str] = {}
    for key, value in evidence.items():
        if not isinstance(value, str) or len(key) > 100 or len(value) > 4096:
            raise click.ClickException("Dokploy Odoo backup restore evidence was not bounded.")
        normalized_evidence[key] = value
    return normalized_evidence


def extract_odoo_post_deploy_readback_markers(deployment: api.JsonObject | None) -> dict[str, str]:
    if deployment is None:
        return {}
    markers: dict[str, str] = {}
    for line in api.normalize_dokploy_log_payload(deployment):
        key, separator, raw_value = line.partition("=")
        if not separator:
            continue
        normalized_key = key.strip()
        normalized_value = raw_value.strip().lower()
        if normalized_key not in ODOO_POST_DEPLOY_READBACK_MARKERS:
            continue
        if normalized_key in ODOO_POST_DEPLOY_BOOLEAN_READBACK_MARKERS and normalized_value not in {
            "true",
            "false",
        }:
            continue
        if (
            normalized_key in ODOO_POST_DEPLOY_NUMERIC_READBACK_MARKERS
            and not normalized_value.isdigit()
        ):
            continue
        markers[normalized_key] = normalized_value
    return markers


def _read_odoo_post_deploy_log_markers(
    *,
    host: str,
    token: str,
    schedule_id: str,
    schedule_deployment_key: str,
    deployment_id: str,
    deployment: api.JsonObject | None = None,
) -> dict[str, str]:
    evidence = {
        "schedule_id": schedule_id,
        "schedule_deployment_key": schedule_deployment_key,
    }
    if deployment_id:
        evidence["schedule_deployment_id"] = deployment_id
    inline_log_lines = api.normalize_dokploy_log_payload(deployment) if deployment else ()
    if inline_log_lines:
        return {
            **evidence,
            "log_available": "true",
            **extract_odoo_post_deploy_readback_markers({"logs": list(inline_log_lines)}),
        }
    if not deployment_id:
        return {**evidence, "log_available": "false"}
    try:
        deployment_log_lines = api.fetch_dokploy_deployment_logs(
            host=host,
            token=token,
            deployment_id=deployment_id,
            line_count=api.MAX_DOKPLOY_LOG_LINE_COUNT,
        )
    except click.ClickException:
        return {**evidence, "log_available": "false"}
    markers = extract_odoo_post_deploy_readback_markers({"logs": list(deployment_log_lines)})
    return {**evidence, "log_available": "true", **markers}


def _build_dokploy_data_workflow_schedule_app_name(*, context_name: str, instance_name: str) -> str:
    return f"platform-{context_name}-{instance_name}-data-workflow"


def _resolve_dokploy_schedule_runtime(
    *,
    host: str,
    token: str,
    compose_id: str,
    compose_name: str,
    target_payload: api.JsonObject,
) -> tuple[str, str, str, str | None]:
    compose_app_name = str(target_payload.get("appName") or "").strip()
    if not compose_app_name:
        raise click.ClickException(
            f"Dokploy compose {compose_name!r} ({compose_id}) has no appName in API response."
        )
    compose_server_id = str(target_payload.get("serverId") or "").strip()
    if compose_server_id:
        return "server", compose_server_id, compose_app_name, compose_server_id
    user_id = api.resolve_dokploy_user_id(host=host, token=token)
    return "dokploy-server", user_id, compose_app_name, None


def _build_dokploy_data_workflow_script(
    *,
    compose_app_name: str,
    database_name: str,
    filestore_path: str,
    clear_stale_lock: bool,
    data_workflow_lock_path: str,
    workflow_mode: Literal["maintenance", "bootstrap", "restore"] = "maintenance",
    workflow_environment_overrides: Mapping[str, str] | None = None,
    required_workflow_environment_keys: tuple[str, ...] = (),
    protected_shopify_store_keys: tuple[str, ...] = (),
) -> str:
    normalized_filestore_path = filestore_path.strip() or "/volumes/data/filestore"
    quoted_compose_app_name = shlex.quote(compose_app_name)
    quoted_database_name = shlex.quote(database_name)
    quoted_filestore_path = shlex.quote(normalized_filestore_path)
    quoted_lock_path = shlex.quote(data_workflow_lock_path)
    workflow_environment_lines = _render_docker_exec_environment_lines(
        workflow_environment_overrides or {}
    )
    effective_required_workflow_environment_keys = tuple(
        sorted(
            {
                *required_workflow_environment_keys,
                *(
                    tuple(workflow_environment_overrides or {})
                    if workflow_environment_overrides
                    else ()
                ),
            }
        )
    )
    required_workflow_environment_lines = _render_required_environment_key_lines(
        effective_required_workflow_environment_keys
    )
    protected_shopify_store_key_lines = _render_bash_array_assignment_lines(
        "protected_shopify_store_keys",
        protected_shopify_store_keys,
    )
    workflow_arguments_by_mode = {
        "maintenance": "--post-deploy-maintenance",
        "bootstrap": "--bootstrap",
        "restore": "",
    }
    workflow_label_by_mode = {
        "maintenance": "post-deploy maintenance",
        "bootstrap": "bootstrap",
        "restore": "restore",
    }
    workflow_arguments = workflow_arguments_by_mode[workflow_mode]
    workflow_label = workflow_label_by_mode[workflow_mode]
    readback_marker_patterns = "|".join(sorted(ODOO_POST_DEPLOY_READBACK_MARKERS))
    return f"""#!/usr/bin/env bash
set -euo pipefail

compose_project={quoted_compose_app_name}
database_name={quoted_database_name}
filestore_root={quoted_filestore_path}
workflow_ssh_dir=/tmp/platform-data-workflow-ssh
workflow_arguments=({workflow_arguments})
workflow_environment=()
{workflow_environment_lines}
required_workflow_environment_keys=()
{required_workflow_environment_lines}
protected_shopify_store_keys=()
{protected_shopify_store_key_lines}
clear_stale_lock={"1" if clear_stale_lock else "0"}
data_workflow_lock_path={quoted_lock_path}
web_was_running=0

resolve_container_id() {{
    local service_name="$1"
    local container_id
    container_id=$(docker ps -aq \
        --filter "label=com.docker.compose.project=${{compose_project}}" \
        --filter "label=com.docker.compose.service=${{service_name}}" | head -n 1)
    if [ -z "${{container_id}}" ]; then
        echo "Missing container for service '${{service_name}}' in project '${{compose_project}}'." >&2
        exit 1
    fi
    printf '%s' "${{container_id}}"
}}

ensure_running() {{
    local container_id="$1"
    local service_name="$2"
    local current_status
    current_status=$(docker inspect -f '{{{{.State.Status}}}}' "${{container_id}}")
    if [ "${{current_status}}" != "running" ]; then
        echo "Starting ${{service_name}} container ${{container_id}}"
        docker start "${{container_id}}" >/dev/null
    fi
}}

start_web_container() {{
    if [ "${{web_was_running}}" != "1" ]; then
        return
    fi
    local current_status
    current_status=$(docker inspect -f '{{{{.State.Status}}}}' "${{web_container_id}}" 2>/dev/null || true)
    if [ "${{current_status}}" != "running" ]; then
        echo "Starting web container ${{web_container_id}}"
        docker start "${{web_container_id}}" >/dev/null || true
    fi
}}

exit_trap() {{
    local exit_status="$?"
    start_web_container
    exit "${{exit_status}}"
}}

database_container_id=$(resolve_container_id "database")
script_runner_container_id=$(resolve_container_id "script-runner")
web_container_id=$(resolve_container_id "web")

ensure_running "${{database_container_id}}" "database"
ensure_running "${{script_runner_container_id}}" "script-runner"
workflow_uid=$(docker exec "${{script_runner_container_id}}" id -u)
workflow_gid=$(docker exec "${{script_runner_container_id}}" id -g)

if [ "${{clear_stale_lock}}" = "1" ]; then
    echo "Clearing stale data workflow lock ${{data_workflow_lock_path}}"
    docker exec -u root "${{script_runner_container_id}}" rm -f "${{data_workflow_lock_path}}"
fi

trap exit_trap EXIT

web_status=$(docker inspect -f '{{{{.State.Status}}}}' "${{web_container_id}}")
if [ "${{web_status}}" = "running" ]; then
    web_was_running=1
    echo "Stopping web container ${{web_container_id}}"
    docker stop "${{web_container_id}}" >/dev/null
fi

if [ "${{#required_workflow_environment_keys[@]}}" -gt 0 ]; then
    docker exec \
        "${{workflow_environment[@]}}" \
        "${{script_runner_container_id}}" \
        /bin/bash -lc '
        set -euo pipefail
        for key_name in "$@"; do
            if [ -z "${{!key_name+x}}" ]; then
                echo "Missing required Odoo override environment key: $key_name" >&2
                exit 1
            fi
        done
        if [ -n "${{ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64:-}}" ]; then
            echo "odoo_instance_overrides_payload_present=true"
        fi
    ' _ "${{required_workflow_environment_keys[@]}}"
fi

echo "Normalizing filestore ownership for ${{database_name}}"
workflow_identity_key=$(docker exec -u root \
    -e ODOO_DATABASE_NAME="${{database_name}}" \
    -e ODOO_FILESTORE_ROOT="${{filestore_root}}" \
    -e DATA_WORKFLOW_SSH_DIR="${{DATA_WORKFLOW_SSH_DIR:-/root/.ssh}}" \
    -e DATA_WORKFLOW_SSH_KEY="${{DATA_WORKFLOW_SSH_KEY:-}}" \
    -e WORKFLOW_UID="${{workflow_uid}}" \
    -e WORKFLOW_GID="${{workflow_gid}}" \
    -e WORKFLOW_SSH_DIR="${{workflow_ssh_dir}}" \
    "${{script_runner_container_id}}" \
    /bin/bash -lc '
        set -euo pipefail
        target_owner=$(stat -c "%u:%g" /volumes/data)
        filestore_database_path="$ODOO_FILESTORE_ROOT"
        if [ "$(basename "$filestore_database_path")" != "$ODOO_DATABASE_NAME" ]; then
            filestore_database_path="$filestore_database_path/$ODOO_DATABASE_NAME"
        fi
        mkdir -p "$ODOO_FILESTORE_ROOT" "$filestore_database_path"
        chown -R "$target_owner" "$filestore_database_path"
        chmod -R ug+rwX "$filestore_database_path"

        rm -rf "$WORKFLOW_SSH_DIR"
        install -d -m 700 -o "$WORKFLOW_UID" -g "$WORKFLOW_GID" "$WORKFLOW_SSH_DIR"

        if [ -f "$DATA_WORKFLOW_SSH_DIR/known_hosts" ]; then
            install -m 600 -o "$WORKFLOW_UID" -g "$WORKFLOW_GID" \
                "$DATA_WORKFLOW_SSH_DIR/known_hosts" "$WORKFLOW_SSH_DIR/known_hosts"
        fi

        source_key_path="$DATA_WORKFLOW_SSH_KEY"
        if [ -z "$source_key_path" ]; then
            for candidate_key in id_ed25519 id_ecdsa id_rsa id_dsa; do
                if [ -f "$DATA_WORKFLOW_SSH_DIR/$candidate_key" ]; then
                    source_key_path="$DATA_WORKFLOW_SSH_DIR/$candidate_key"
                    break
                fi
            done
        fi
        workflow_identity_key=""
        if [ -n "$source_key_path" ] && [ -f "$source_key_path" ]; then
            workflow_identity_key="$WORKFLOW_SSH_DIR/$(basename "$source_key_path")"
            install -m 600 -o "$WORKFLOW_UID" -g "$WORKFLOW_GID" \
                "$source_key_path" "$workflow_identity_key"
        fi
        printf "%s" "$workflow_identity_key"
    ')

echo "Running Odoo {workflow_label} in container ${{script_runner_container_id}}"
workflow_output_file=$(mktemp)
set +e
docker exec \
    -e DATA_WORKFLOW_SSH_DIR="${{workflow_ssh_dir}}" \
    -e DATA_WORKFLOW_SSH_KEY="$workflow_identity_key" \
    "${{workflow_environment[@]}}" \
    "${{script_runner_container_id}}" \
    python3 -u /volumes/scripts/run_odoo_data_workflows.py "${{workflow_arguments[@]}}" \
    2>&1 | tee "$workflow_output_file"
workflow_exit_status=${{PIPESTATUS[0]}}
set -e

echo "Odoo {workflow_label} readback markers:"
grep -E '^({readback_marker_patterns})=' "$workflow_output_file" \
    | sort -u \
    || true
rm -f "$workflow_output_file"
if [ "$workflow_exit_status" -ne 0 ]; then
    exit "$workflow_exit_status"
fi

if [ "${{#protected_shopify_store_keys[@]}}" -gt 0 ]; then
    echo "Checking protected Shopify store keys for ${{database_name}}"
    docker exec "${{script_runner_container_id}}" python3 - "${{database_name}}" "${{protected_shopify_store_keys[@]}}" <<'PY'
import os
import sys

import psycopg2

database_name = sys.argv[1]
protected_store_keys = {{value.strip().lower() for value in sys.argv[2:] if value.strip()}}

connection = psycopg2.connect(
    host=(os.environ.get("ODOO_DB_HOST") or "database").strip(),
    port=(os.environ.get("ODOO_DB_PORT") or "5432").strip(),
    user=(os.environ.get("ODOO_DB_USER") or "odoo").strip(),
    password=os.environ.get("ODOO_DB_PASSWORD") or "",
    dbname=database_name,
)
try:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT value FROM ir_config_parameter WHERE key = %s LIMIT 1",
            ("shopify.shop_url_key",),
        )
        row = cursor.fetchone()
finally:
    connection.close()

current_store_key = str(row[0]).strip() if row and row[0] is not None else ""
normalized_store_key = current_store_key.lower()
if normalized_store_key in protected_store_keys:
    protected_list = ", ".join(sorted(protected_store_keys))
    raise SystemExit(
        "Protected Shopify store key is not allowed on this Dokploy lane. "
        f"db={{database_name}} current={{current_store_key or '<empty>'}} protected={{protected_list}}"
    )

print(f"shopify_store_key_guard_pass db={{database_name}} value={{current_store_key or '<empty>'}}")
PY
fi

start_web_container
trap - EXIT
"""


def _build_dokploy_odoo_backup_gate_script(
    *,
    compose_app_name: str,
    backup_nonce: str,
    database_name: str,
    filestore_path: str,
    backup_root: str,
    backup_record_id: str,
) -> str:
    normalized_filestore_path = filestore_path.strip() or "/volumes/data/filestore"
    normalized_backup_root = backup_root.strip() or DEFAULT_ODOO_BACKUP_ROOT
    quoted_compose_app_name = shlex.quote(compose_app_name)
    quoted_backup_nonce = shlex.quote(backup_nonce)
    quoted_database_name = shlex.quote(database_name)
    quoted_filestore_path = shlex.quote(normalized_filestore_path)
    quoted_backup_root = shlex.quote(normalized_backup_root)
    quoted_backup_record_id = shlex.quote(backup_record_id)
    return f"""#!/usr/bin/env bash
set -euo pipefail

compose_project={quoted_compose_app_name}
database_name={quoted_database_name}
filestore_root={quoted_filestore_path}
backup_root={quoted_backup_root}
backup_record_id={quoted_backup_record_id}
backup_nonce={quoted_backup_nonce}
web_was_running=0

resolve_container_id() {{
    local service_name="$1"
    local container_id
    container_id=$(docker ps -aq \
        --filter "label=com.docker.compose.project=${{compose_project}}" \
        --filter "label=com.docker.compose.service=${{service_name}}" | head -n 1)
    if [ -z "${{container_id}}" ]; then
        echo "Missing container for service '${{service_name}}' in project '${{compose_project}}'." >&2
        exit 1
    fi
    printf '%s' "${{container_id}}"
}}

ensure_running() {{
    local container_id="$1"
    local service_name="$2"
    local current_status
    current_status=$(docker inspect -f '{{{{.State.Status}}}}' "${{container_id}}")
    if [ "${{current_status}}" != "running" ]; then
        echo "Starting ${{service_name}} container ${{container_id}}"
        docker start "${{container_id}}" >/dev/null
    fi
}}

start_web_container() {{
    if [ "${{web_was_running}}" != "1" ]; then
        return
    fi
    local current_status
    current_status=$(docker inspect -f '{{{{.State.Status}}}}' "${{web_container_id}}" 2>/dev/null || true)
    if [ "${{current_status}}" != "running" ]; then
        echo "Starting web container ${{web_container_id}}"
        docker start "${{web_container_id}}" >/dev/null || true
    fi
}}

exit_trap() {{
    local exit_status="$?"
    start_web_container
    exit "${{exit_status}}"
}}

database_container_id=$(resolve_container_id "database")
script_runner_container_id=$(resolve_container_id "script-runner")
web_container_id=$(resolve_container_id "web")
ensure_running "${{database_container_id}}" "database"
ensure_running "${{script_runner_container_id}}" "script-runner"

trap exit_trap EXIT

if [ "$(docker inspect -f '{{{{.State.Status}}}}' "${{web_container_id}}")" = "running" ]; then
    web_was_running=1
    echo "Stopping web container ${{web_container_id}} for backup consistency"
    docker stop "${{web_container_id}}" >/dev/null
fi

backup_dir="${{backup_root}}/${{database_name}}/${{backup_record_id}}"
database_dump_path="${{backup_dir}}/${{database_name}}.dump"
filestore_archive_path="${{backup_dir}}/${{database_name}}-filestore.tar.gz"
manifest_path="${{backup_dir}}/manifest.json"

echo "Creating Odoo backup gate directory ${{backup_dir}}"
script_runner_uid=$(docker exec "${{script_runner_container_id}}" id -u)
script_runner_gid=$(docker exec "${{script_runner_container_id}}" id -g)
docker exec -u root \
    -e BACKUP_ROOT="${{backup_root}}" \
    -e DATABASE_NAME="${{database_name}}" \
    -e BACKUP_DIR="${{backup_dir}}" \
    -e SCRIPT_RUNNER_UID="${{script_runner_uid}}" \
    -e SCRIPT_RUNNER_GID="${{script_runner_gid}}" \
    "${{script_runner_container_id}}" \
    /bin/bash -lc '
        set -euo pipefail
        install -d -m 700 -o "$SCRIPT_RUNNER_UID" -g "$SCRIPT_RUNNER_GID" "$BACKUP_ROOT"
        install -d -m 700 -o "$SCRIPT_RUNNER_UID" -g "$SCRIPT_RUNNER_GID" "$BACKUP_ROOT/$DATABASE_NAME"
        install -d -m 700 -o "$SCRIPT_RUNNER_UID" -g "$SCRIPT_RUNNER_GID" "$BACKUP_DIR"
    '

echo "Capturing database dump for ${{database_name}}"
docker exec \
    -e ODOO_DATABASE_NAME="${{database_name}}" \
    -e DATABASE_DUMP_PATH="${{database_dump_path}}" \
    "${{script_runner_container_id}}" \
    /bin/bash -lc '
        set -euo pipefail
        export PGPASSWORD="${{ODOO_DB_PASSWORD:-}}"
        pg_dump \
            --host "${{ODOO_DB_HOST:-database}}" \
            --port "${{ODOO_DB_PORT:-5432}}" \
            --username "${{ODOO_DB_USER:-odoo}}" \
            --format custom \
            --file "$DATABASE_DUMP_PATH" \
            "$ODOO_DATABASE_NAME"
        test -s "$DATABASE_DUMP_PATH"
    '

echo "Capturing filestore archive for ${{database_name}}"
docker exec \
    -e ODOO_DATABASE_NAME="${{database_name}}" \
    -e ODOO_FILESTORE_ROOT="${{filestore_root}}" \
    -e FILESTORE_ARCHIVE_PATH="${{filestore_archive_path}}" \
    "${{script_runner_container_id}}" \
    /bin/bash -lc '
        set -euo pipefail
        filestore_database_path="$ODOO_FILESTORE_ROOT"
        if [ "$(basename "$filestore_database_path")" != "$ODOO_DATABASE_NAME" ]; then
            filestore_database_path="$filestore_database_path/$ODOO_DATABASE_NAME"
        fi
        if [ ! -d "$filestore_database_path" ]; then
            echo "Missing filestore path: $filestore_database_path" >&2
            exit 1
        fi
        tar -C "$(dirname "$filestore_database_path")" -czf "$FILESTORE_ARCHIVE_PATH" "$(basename "$filestore_database_path")"
        test -s "$FILESTORE_ARCHIVE_PATH"
    '

database_dump_size=$(docker exec "${{script_runner_container_id}}" stat -c %s "${{database_dump_path}}")
filestore_archive_size=$(docker exec "${{script_runner_container_id}}" stat -c %s "${{filestore_archive_path}}")

docker exec -i \
    -e MANIFEST_PATH="${{manifest_path}}" \
    -e BACKUP_RECORD_ID="${{backup_record_id}}" \
    -e DATABASE_NAME="${{database_name}}" \
    -e BACKUP_DIR="${{backup_dir}}" \
    -e DATABASE_DUMP_PATH="${{database_dump_path}}" \
    -e FILESTORE_ARCHIVE_PATH="${{filestore_archive_path}}" \
    -e DATABASE_DUMP_SIZE="${{database_dump_size}}" \
    -e FILESTORE_ARCHIVE_SIZE="${{filestore_archive_size}}" \
    -e BACKUP_NONCE="${{backup_nonce}}" \
    -e RESULT_MARKER={shlex.quote(ODOO_BACKUP_GATE_RESULT_MARKER)} \
    "${{script_runner_container_id}}" \
    python3 - <<'PY'
import base64
import json
import hashlib
import os
from datetime import datetime, timezone

def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

payload = {{
    "schema_version": 1,
    "backup_record_id": os.environ["BACKUP_RECORD_ID"],
    "database_name": os.environ["DATABASE_NAME"],
    "backup_dir": os.environ["BACKUP_DIR"],
    "database_dump_path": os.environ["DATABASE_DUMP_PATH"],
    "filestore_archive_path": os.environ["FILESTORE_ARCHIVE_PATH"],
    "manifest_path": os.environ["MANIFEST_PATH"],
    "database_dump_size": int(os.environ["DATABASE_DUMP_SIZE"]),
    "filestore_archive_size": int(os.environ["FILESTORE_ARCHIVE_SIZE"]),
    "database_dump_sha256": sha256_file(os.environ["DATABASE_DUMP_PATH"]),
    "filestore_archive_sha256": sha256_file(os.environ["FILESTORE_ARCHIVE_PATH"]),
    "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
}}
with open(os.environ["MANIFEST_PATH"], "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\\n")

result_payload = {{
    "schema_version": payload["schema_version"],
    "backup_nonce": os.environ["BACKUP_NONCE"],
    "backup_record_id": payload["backup_record_id"],
    "database_name": payload["database_name"],
    "database_dump_sha256": payload["database_dump_sha256"],
    "filestore_archive_sha256": payload["filestore_archive_sha256"],
    "database_dump_size": payload["database_dump_size"],
    "filestore_archive_size": payload["filestore_archive_size"],
}}
encoded_result = base64.b64encode(
    json.dumps(result_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
).decode("ascii")
print(f"{{os.environ['RESULT_MARKER']}}={{encoded_result}}", flush=True)
PY

echo "Odoo backup gate complete: ${{backup_dir}}"
start_web_container
trap - EXIT
"""


def _build_dokploy_odoo_backup_verification_script(
    *,
    compose_app_name: str,
    verification_nonce: str,
    backup_record_id: str,
    database_name: str,
    filestore_path: str,
    backup_dir: str,
    database_dump_path: str,
    filestore_archive_path: str,
    manifest_path: str,
) -> str:
    return f"""#!/usr/bin/env bash
set -euo pipefail

compose_project={shlex.quote(compose_app_name)}
script_runner_container_id=$(docker ps -q \
    --filter "label=com.docker.compose.project=${{compose_project}}" \
    --filter "label=com.docker.compose.service=script-runner" | head -n 1)
if [ -z "${{script_runner_container_id}}" ]; then
    echo "Odoo backup verification requires a running script-runner container." >&2
    exit 1
fi

docker exec -i \
    -e VERIFICATION_NONCE={shlex.quote(verification_nonce)} \
    -e BACKUP_RECORD_ID={shlex.quote(backup_record_id)} \
    -e DATABASE_NAME={shlex.quote(database_name)} \
    -e FILESTORE_ROOT={shlex.quote(filestore_path)} \
    -e BACKUP_DIR={shlex.quote(backup_dir)} \
    -e DATABASE_DUMP_PATH={shlex.quote(database_dump_path)} \
    -e FILESTORE_ARCHIVE_PATH={shlex.quote(filestore_archive_path)} \
    -e MANIFEST_PATH={shlex.quote(manifest_path)} \
    -e RESULT_MARKER={shlex.quote(ODOO_BACKUP_VERIFICATION_RESULT_MARKER)} \
    "${{script_runner_container_id}}" \
    python3 - <<'PY'
import base64
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path, PurePosixPath


class VerificationFailure(Exception):
    pass


def fail(code, check=""):
    if check:
        result[check] = "fail"
    raise VerificationFailure(code)


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_size(value):
    if isinstance(value, bool):
        fail("manifest_size_invalid", "manifest_status")
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    fail("manifest_size_invalid", "manifest_status")


def path_size(path, failure_code, check):
    try:
        return path.stat().st_size
    except OSError:
        fail(failure_code, check)


def require_regular_file(path, failure_code, check):
    try:
        invalid_file = path.is_symlink() or not path.is_file()
    except OSError:
        fail(failure_code, check)
    if invalid_file:
        fail(failure_code, check)


verification_nonce = os.environ["VERIFICATION_NONCE"]
backup_record_id = os.environ["BACKUP_RECORD_ID"]
database_name = os.environ["DATABASE_NAME"]

result = {{
    "schema_version": 1,
    "verification_nonce": verification_nonce,
    "backup_record_id": backup_record_id,
    "database_name": database_name,
    "verification_status": "fail",
    "manifest_status": "not_run",
    "sha256_status": "not_run",
    "pg_restore_status": "not_run",
    "tar_status": "not_run",
    "staging_space_status": "not_run",
    "database_dump_sha256": "",
    "filestore_archive_sha256": "",
    "database_dump_size": 0,
    "filestore_archive_size": 0,
    "pg_restore_entry_count": 0,
    "filestore_member_count": 0,
    "filestore_unpacked_size": 0,
    "data_volume_free_bytes": 0,
    "staging_required_bytes": 0,
    "failure_code": "verification_error",
}}
active_check = "manifest_status"
unexpected_failure_code = "manifest_path_verification_error"

try:
    filestore_root = Path(os.environ["FILESTORE_ROOT"])
    backup_dir = Path(os.environ["BACKUP_DIR"])
    database_dump_path = Path(os.environ["DATABASE_DUMP_PATH"])
    filestore_archive_path = Path(os.environ["FILESTORE_ARCHIVE_PATH"])
    manifest_path = Path(os.environ["MANIFEST_PATH"])

    require_regular_file(manifest_path, "manifest_missing", "manifest_status")
    unexpected_failure_code = "manifest_read_verification_error"
    if (
        path_size(manifest_path, "manifest_metadata_unreadable", "manifest_status")
        > 1024 * 1024
    ):
        fail("manifest_too_large", "manifest_status")
    try:
        manifest_text = manifest_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        fail("manifest_decode_error", "manifest_status")
    except OSError:
        fail("manifest_read_error", "manifest_status")
    try:
        manifest = json.loads(manifest_text)
    except json.JSONDecodeError:
        fail("manifest_decode_error", "manifest_status")
    unexpected_failure_code = "manifest_validation_error"
    if not isinstance(manifest, dict):
        fail("manifest_identity_mismatch", "manifest_status")
    manifest_schema_version = manifest.get("schema_version")
    if manifest_schema_version not in (None, 1):
        fail("manifest_identity_mismatch", "manifest_status")
    expected_identity = {{
        "backup_record_id": backup_record_id,
        "database_name": database_name,
    }}
    if any(manifest.get(key) != value for key, value in expected_identity.items()):
        fail("manifest_identity_mismatch", "manifest_status")
    expected_paths = {{
        "backup_dir": str(backup_dir),
        "database_dump_path": str(database_dump_path),
        "filestore_archive_path": str(filestore_archive_path),
    }}
    if any(manifest.get(key) != value for key, value in expected_paths.items()):
        fail("manifest_path_mismatch", "manifest_status")
    manifest_path_value = manifest.get("manifest_path")
    if manifest_path_value is not None and manifest_path_value != str(manifest_path):
        fail("manifest_path_mismatch", "manifest_status")
    if manifest_schema_version == 1 and manifest_path_value != str(manifest_path):
        fail("manifest_path_mismatch", "manifest_status")

    unexpected_failure_code = "artifact_path_verification_error"
    require_regular_file(database_dump_path, "artifact_missing", "manifest_status")
    require_regular_file(filestore_archive_path, "artifact_missing", "manifest_status")
    unexpected_failure_code = "artifact_metadata_verification_error"
    result["database_dump_size"] = path_size(
        database_dump_path, "artifact_missing", "manifest_status"
    )
    result["filestore_archive_size"] = path_size(
        filestore_archive_path, "artifact_missing", "manifest_status"
    )
    if (
        manifest_size(manifest.get("database_dump_size"))
        != result["database_dump_size"]
        or manifest_size(manifest.get("filestore_archive_size"))
        != result["filestore_archive_size"]
    ):
        fail("artifact_size_mismatch", "manifest_status")
    result["manifest_status"] = "pass"

    active_check = "sha256_status"
    unexpected_failure_code = "verification_error"
    result["database_dump_sha256"] = file_sha256(database_dump_path)
    result["filestore_archive_sha256"] = file_sha256(filestore_archive_path)
    manifest_database_dump_sha256 = manifest.get("database_dump_sha256")
    manifest_filestore_archive_sha256 = manifest.get("filestore_archive_sha256")
    manifest_hashes_present = (
        manifest_database_dump_sha256 is not None
        or manifest_filestore_archive_sha256 is not None
    )
    if manifest_schema_version == 1 or manifest_hashes_present:
        if (
            manifest_database_dump_sha256 != result["database_dump_sha256"]
            or manifest_filestore_archive_sha256
            != result["filestore_archive_sha256"]
        ):
            fail("artifact_hash_mismatch", "sha256_status")
    result["sha256_status"] = "pass"

    active_check = "pg_restore_status"
    restore_list = subprocess.run(
        ["pg_restore", "--list", str(database_dump_path)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if restore_list.returncode != 0:
        fail("database_dump_invalid", "pg_restore_status")
    result["pg_restore_entry_count"] = sum(
        1
        for line in restore_list.stdout.splitlines()
        if line.strip() and not line.lstrip().startswith(";")
    )
    if result["pg_restore_entry_count"] < 1:
        fail("database_dump_invalid", "pg_restore_status")
    restore_read = subprocess.run(
        [
            "pg_restore",
            "--no-owner",
            "--no-acl",
            "--file=/dev/null",
            str(database_dump_path),
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if restore_read.returncode != 0:
        fail("database_dump_invalid", "pg_restore_status")
    result["pg_restore_status"] = "pass"

    active_check = "tar_status"
    seen_members = set()
    top_level_directory_seen = False
    try:
        with tarfile.open(filestore_archive_path, mode="r:gz") as archive:
            for member in archive:
                member_path = PurePosixPath(member.name)
                if (
                    member_path.is_absolute()
                    or not member_path.parts
                    or any(part in {{"", ".", ".."}} for part in member_path.parts)
                    or member_path.parts[0] != database_name
                    or (not member.isfile() and not member.isdir())
                ):
                    fail("filestore_members_unsafe", "tar_status")
                normalized_member = member_path.as_posix()
                if normalized_member in seen_members:
                    fail("filestore_members_unsafe", "tar_status")
                seen_members.add(normalized_member)
                result["filestore_member_count"] += 1
                if member.isfile():
                    result["filestore_unpacked_size"] += member.size
                if member.isdir() and normalized_member.rstrip("/") == database_name:
                    top_level_directory_seen = True
    except (OSError, tarfile.TarError):
        fail("filestore_archive_invalid", "tar_status")
    if result["filestore_member_count"] < 1:
        fail("filestore_archive_invalid", "tar_status")
    if not top_level_directory_seen:
        fail("filestore_top_level_mismatch", "tar_status")
    result["tar_status"] = "pass"

    active_check = "staging_space_status"
    filestore_database_path = filestore_root
    if filestore_database_path.name != database_name:
        filestore_database_path = filestore_database_path / database_name
    staging_parent = filestore_database_path.parent
    if not staging_parent.is_dir():
        fail("staging_path_unavailable", "staging_space_status")
    result["data_volume_free_bytes"] = shutil.disk_usage(staging_parent).free
    result["staging_required_bytes"] = result["filestore_unpacked_size"]
    if result["data_volume_free_bytes"] < result["staging_required_bytes"]:
        fail("staging_space_insufficient", "staging_space_status")
    result["staging_space_status"] = "pass"
    result["verification_status"] = "pass"
    result["failure_code"] = ""
except VerificationFailure as error:
    result["failure_code"] = str(error)
except Exception:
    result[active_check] = "fail"
    result["failure_code"] = unexpected_failure_code

encoded_result = base64.b64encode(
    json.dumps(result, separators=(",", ":"), sort_keys=True).encode("utf-8")
).decode("ascii")
print(f"{{os.environ['RESULT_MARKER']}}={{encoded_result}}")
PY
"""


def _build_dokploy_odoo_retained_volume_backup_import_inspection_script(
    *,
    compose_app_name: str,
    inspection_nonce: str,
    backup_record_id: str,
    active_db_volume: str,
    active_data_volume: str,
    active_log_volume: str,
    source_db_volume: str,
    source_data_volume: str,
    staging_clone_volume: str,
    source_database_name: str,
    destination_database_name: str,
    database_user: str,
    expected_source_compose_project: str,
    filestore_relative_path: str,
    backup_dir_relative_path: str,
) -> str:
    return f"""#!/usr/bin/env bash
set -Eeuo pipefail

compose_project={shlex.quote(compose_app_name)}
inspection_nonce={shlex.quote(inspection_nonce)}
backup_record_id={shlex.quote(backup_record_id)}
failure_marker={shlex.quote(ODOO_RETAINED_VOLUME_BACKUP_IMPORT_INSPECT_FAILURE_MARKER)}
failure_stage=active_runtime
failure_code=active_runtime_inspection_failed
active_db_volume={shlex.quote(active_db_volume)}
active_data_volume={shlex.quote(active_data_volume)}
active_log_volume={shlex.quote(active_log_volume)}
source_db_volume={shlex.quote(source_db_volume)}
source_data_volume={shlex.quote(source_data_volume)}
staging_clone_volume={shlex.quote(staging_clone_volume)}
source_database_name={shlex.quote(source_database_name)}
destination_database_name={shlex.quote(destination_database_name)}
database_user={shlex.quote(database_user)}
expected_source_compose_project={shlex.quote(expected_source_compose_project)}
filestore_relative_path={shlex.quote(filestore_relative_path)}
backup_dir_relative_path={shlex.quote(backup_dir_relative_path)}

emit_inspection_failure() {{
    local exit_status="$1"
    if [ "${{exit_status}}" -ne 0 ]; then
        printf '%s=%s|%s|%s|%s\n' \
            "${{failure_marker}}" \
            "${{inspection_nonce}}" \
            "${{backup_record_id}}" \
            "${{failure_stage}}" \
            "${{failure_code}}"
    fi
    return "${{exit_status}}"
}}

trap 'emit_inspection_failure "$?"' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

resolve_single_container() {{
    local service_name="$1"
    local container_ids
    container_ids=$(docker ps -q \
        --filter "label=com.docker.compose.project=${{compose_project}}" \
        --filter "label=com.docker.compose.service=${{service_name}}")
    if [ "$(printf '%s\n' "${{container_ids}}" | sed '/^$/d' | wc -l | tr -d ' ')" != "1" ]; then
        echo "Expected exactly one running ${{service_name}} container for ${{compose_project}}." >&2
        exit 1
    fi
    printf '%s' "${{container_ids}}"
}}

resolve_single_container_any_state() {{
    local service_name="$1"
    local container_ids
    container_ids=$(docker ps -aq \
        --filter "label=com.docker.compose.project=${{compose_project}}" \
        --filter "label=com.docker.compose.service=${{service_name}}")
    if [ "$(printf '%s\n' "${{container_ids}}" | sed '/^$/d' | wc -l | tr -d ' ')" != "1" ]; then
        echo "Expected exactly one ${{service_name}} container for ${{compose_project}}." >&2
        exit 1
    fi
    printf '%s' "${{container_ids}}"
}}

require_volume() {{
    docker volume inspect "$1" >/dev/null
}}

volume_label() {{
    local volume_name="$1"
    local label_name="$2"
    docker volume inspect -f "{{{{ index .Labels \\"${{label_name}}\\" }}}}" "${{volume_name}}"
}}

container_mounts_volume() {{
    local container_id="$1"
    local volume_name="$2"
    docker inspect -f '{{{{range .Mounts}}}}{{{{println .Name}}}}{{{{end}}}}' \
        "${{container_id}}" | grep -Fx -- "${{volume_name}}" >/dev/null
}}

failure_stage=active_runtime
failure_code=active_volume_missing
require_volume "${{active_db_volume}}"
require_volume "${{active_data_volume}}"
require_volume "${{active_log_volume}}"

failure_stage=source_safety
failure_code=source_volume_missing
require_volume "${{source_db_volume}}"
require_volume "${{source_data_volume}}"

failure_code=source_volume_usage_read_failed
source_db_container_ids=
if ! source_db_container_ids=$(docker ps -q --filter "volume=${{source_db_volume}}"); then
    exit 1
fi
source_data_container_ids=
if ! source_data_container_ids=$(docker ps -q --filter "volume=${{source_data_volume}}"); then
    exit 1
fi
failure_code=source_volume_in_use
if [ -n "${{source_db_container_ids}}" ]; then
    echo "Retained source DB volume is mounted by a running container." >&2
    exit 1
fi
if [ -n "${{source_data_container_ids}}" ]; then
    echo "Retained source data volume is mounted by a running container." >&2
    exit 1
fi

failure_stage=active_runtime
failure_code=active_database_container_unavailable
database_container_id=$(resolve_single_container_any_state database)
failure_code=script_runner_container_unavailable
script_runner_container_id=$(resolve_single_container script-runner)
failure_code=active_runtime_inspection_failed
postgres_image_id=$(docker inspect -f '{{{{.Image}}}}' "${{database_container_id}}")
script_runner_image_id=$(docker inspect -f '{{{{.Image}}}}' "${{script_runner_container_id}}")
postgres_binary_version=$(docker run --rm --read-only --network none \
    --entrypoint postgres "${{postgres_image_id}}" --version)
active_database_user=$(docker inspect -f '{{{{range .Config.Env}}}}{{{{println .}}}}{{{{end}}}}' \
    "${{database_container_id}}" | sed -n 's/^POSTGRES_USER=//p' | tail -n 1)
active_destination_database_name=$(docker inspect \
    -f '{{{{range .Config.Env}}}}{{{{println .}}}}{{{{end}}}}' \
    "${{script_runner_container_id}}" | sed -n 's/^ODOO_DB_NAME=//p' | tail -n 1)
failure_code=active_runtime_identity_mismatch
if [ "${{active_database_user}}" != "${{database_user}}" ]; then
    echo "Active PostgreSQL user does not match runtime authority." >&2
    exit 1
fi
if [ "${{active_destination_database_name}}" != "${{destination_database_name}}" ]; then
    echo "Active destination database name does not match runtime authority." >&2
    exit 1
fi
failure_code=active_volume_mount_mismatch
if ! container_mounts_volume "${{database_container_id}}" "${{active_db_volume}}" || \
   ! container_mounts_volume "${{script_runner_container_id}}" "${{active_data_volume}}" || \
   ! container_mounts_volume "${{script_runner_container_id}}" "${{active_log_volume}}"; then
    echo "Active container volume mounts do not match runtime authority." >&2
    exit 1
fi
failure_code=active_image_invalid
case "${{postgres_image_id}}" in
    sha256:[0-9a-f][0-9a-f]*) ;;
    *) echo "Active PostgreSQL container did not expose an immutable image id." >&2; exit 1 ;;
esac
case "${{script_runner_image_id}}" in
    sha256:[0-9a-f][0-9a-f]*) ;;
    *) echo "Active script-runner container did not expose an immutable image id." >&2; exit 1 ;;
esac
failure_code=active_postgres_version_mismatch
case "${{postgres_binary_version}}" in
    "postgres (PostgreSQL) 17" | "postgres (PostgreSQL) 17."*) ;;
    *) echo "Active PostgreSQL image is not major version 17." >&2; exit 1 ;;
esac

failure_stage=source_safety
failure_code=source_volume_label_read_failed
source_db_project_label=$(volume_label "${{source_db_volume}}" com.docker.compose.project)
source_db_role_label=$(volume_label "${{source_db_volume}}" com.docker.compose.volume)
source_data_project_label=$(volume_label "${{source_data_volume}}" com.docker.compose.project)
source_data_role_label=$(volume_label "${{source_data_volume}}" com.docker.compose.volume)

failure_stage=source_database
failure_code=source_postgres_metadata_read_failed
source_pg_version=$(docker run --rm --read-only --network none --user 0:0 \
    --mount "type=volume,src=${{source_db_volume}},dst=/source,readonly" \
    --entrypoint /bin/bash "${{postgres_image_id}}" \
    -lc 'cat /source/PG_VERSION')
source_pg_control_sha256=$(docker run --rm --read-only --network none --user 0:0 \
    --mount "type=volume,src=${{source_db_volume}},dst=/source,readonly" \
    --entrypoint /bin/bash "${{postgres_image_id}}" \
    -lc 'sha256sum /source/global/pg_control | awk '"'"'{{print $1}}'"'"'')
source_pg_control_size=$(docker run --rm --read-only --network none --user 0:0 \
    --mount "type=volume,src=${{source_db_volume}},dst=/source,readonly" \
    --entrypoint /bin/bash "${{postgres_image_id}}" \
    -lc 'stat -c %s /source/global/pg_control')
pg_controldata_output=$(docker run --rm --read-only --network none --user 0:0 \
    --mount "type=volume,src=${{source_db_volume}},dst=/source,readonly" \
    --entrypoint /bin/bash "${{postgres_image_id}}" \
    -lc 'LC_ALL=C pg_controldata /source')
source_pg_system_identifier=$(printf '%s\n' "${{pg_controldata_output}}" | \
    sed -n 's/^Database system identifier:[[:space:]]*//p' | head -n 1)
source_pg_cluster_state=$(printf '%s\n' "${{pg_controldata_output}}" | \
    sed -n 's/^Database cluster state:[[:space:]]*//p' | head -n 1)
source_pg_checkpoint_location=$(printf '%s\n' "${{pg_controldata_output}}" | \
    sed -n 's/^Latest checkpoint location:[[:space:]]*//p' | head -n 1)
source_pg_checkpoint_redo_location=$(printf '%s\n' "${{pg_controldata_output}}" | \
    sed -n "s/^Latest checkpoint's REDO location:[[:space:]]*//p" | head -n 1)
source_pg_checkpoint_timeline_id=$(printf '%s\n' "${{pg_controldata_output}}" | \
    sed -n "s/^Latest checkpoint's TimeLineID:[[:space:]]*//p" | head -n 1)
source_pg_checkpoint_time=$(printf '%s\n' "${{pg_controldata_output}}" | \
    sed -n 's/^Time of latest checkpoint:[[:space:]]*//p' | head -n 1)

failure_code=source_db_measurement_failed
source_db_volume_used_bytes=$(docker run --rm --read-only --network none --user 0:0 \
    --mount "type=volume,src=${{source_db_volume}},dst=/source,readonly" \
    --entrypoint python3 "${{script_runner_image_id}}" -c '
import os
total = 0
for root, directories, files in os.walk("/source", followlinks=False):
    for name in directories + files:
        path = os.path.join(root, name)
        if os.path.islink(path):
            raise SystemExit("retained DB volume contains a symbolic link")
        total += os.lstat(path).st_size
print(total)
')

failure_stage=source_filestore
failure_code=source_filestore_measurement_failed
filestore_metrics=$(docker run --rm --read-only --network none --user 0:0 \
    --mount "type=volume,src=${{source_data_volume}},dst=/source-data,readonly" \
    --entrypoint python3 "${{script_runner_image_id}}" -c '
import os
import stat
import sys
root = os.path.join("/source-data", sys.argv[1], sys.argv[2])
if not os.path.isdir(root) or os.path.islink(root):
    raise SystemExit("retained filestore path is missing or unsafe")
count = 0
size = 0
for current_root, directories, files in os.walk(root, followlinks=False):
    for directory in directories:
        path = os.path.join(current_root, directory)
        if os.path.islink(path):
            raise SystemExit("retained filestore contains a symbolic link")
    for name in files:
        path = os.path.join(current_root, name)
        mode = os.lstat(path).st_mode
        if not stat.S_ISREG(mode):
            raise SystemExit("retained filestore contains a non-regular file")
        count += 1
        size += os.lstat(path).st_size
print(f"{{count}}:{{size}}")
' "${{filestore_relative_path}}" "${{source_database_name}}")
source_filestore_file_count=${{filestore_metrics%%:*}}
source_filestore_size_bytes=${{filestore_metrics#*:}}

failure_stage=destination
failure_code=active_data_space_read_failed
active_data_free_bytes=$(docker run --rm --read-only --network none --user 0:0 \
    --mount "type=volume,src=${{active_data_volume}},dst=/active-data,readonly" \
    --entrypoint python3 "${{script_runner_image_id}}" \
    -c 'import shutil; print(shutil.disk_usage("/active-data").free)')

failure_code=destination_check_failed
staging_volume_names=
if ! staging_volume_names=$(docker volume ls -q --filter "name=${{staging_clone_volume}}"); then
    exit 1
fi
if printf '%s\n' "${{staging_volume_names}}" | grep -Fx -- "${{staging_clone_volume}}" >/dev/null; then
    staging_clone_volume_absent=false
else
    staging_clone_volume_absent=true
fi
backup_destination_absent=$(docker run --rm --read-only --network none --user 0:0 \
    --mount "type=volume,src=${{active_data_volume}},dst=/active-data,readonly" \
    --entrypoint python3 "${{script_runner_image_id}}" -c '
import os
import sys
print("true" if not os.path.lexists(os.path.join("/active-data", sys.argv[1])) else "false")
' "${{backup_dir_relative_path}}")

failure_stage=result
failure_code=result_emit_failed
docker exec -i \
    -e RESULT_MARKER={shlex.quote(ODOO_RETAINED_VOLUME_BACKUP_IMPORT_INSPECT_RESULT_MARKER)} \
    -e INSPECTION_NONCE="${{inspection_nonce}}" \
    -e BACKUP_RECORD_ID="${{backup_record_id}}" \
    -e ACTIVE_DB_VOLUME="${{active_db_volume}}" \
    -e ACTIVE_DATA_VOLUME="${{active_data_volume}}" \
    -e ACTIVE_LOG_VOLUME="${{active_log_volume}}" \
    -e SOURCE_DB_VOLUME="${{source_db_volume}}" \
    -e SOURCE_DATA_VOLUME="${{source_data_volume}}" \
    -e STAGING_CLONE_VOLUME="${{staging_clone_volume}}" \
    -e SOURCE_DATABASE_NAME="${{source_database_name}}" \
    -e DESTINATION_DATABASE_NAME="${{destination_database_name}}" \
    -e DATABASE_USER="${{database_user}}" \
    -e SOURCE_DB_PROJECT_LABEL="${{source_db_project_label}}" \
    -e SOURCE_DB_ROLE_LABEL="${{source_db_role_label}}" \
    -e SOURCE_DATA_PROJECT_LABEL="${{source_data_project_label}}" \
    -e SOURCE_DATA_ROLE_LABEL="${{source_data_role_label}}" \
    -e SOURCE_PG_VERSION="${{source_pg_version}}" \
    -e SOURCE_PG_CONTROL_SHA256="${{source_pg_control_sha256}}" \
    -e SOURCE_PG_CONTROL_SIZE="${{source_pg_control_size}}" \
    -e SOURCE_PG_SYSTEM_IDENTIFIER="${{source_pg_system_identifier}}" \
    -e SOURCE_PG_CLUSTER_STATE="${{source_pg_cluster_state}}" \
    -e SOURCE_PG_CHECKPOINT_LOCATION="${{source_pg_checkpoint_location}}" \
    -e SOURCE_PG_CHECKPOINT_REDO_LOCATION="${{source_pg_checkpoint_redo_location}}" \
    -e SOURCE_PG_CHECKPOINT_TIMELINE_ID="${{source_pg_checkpoint_timeline_id}}" \
    -e SOURCE_PG_CHECKPOINT_TIME="${{source_pg_checkpoint_time}}" \
    -e SOURCE_DB_VOLUME_USED_BYTES="${{source_db_volume_used_bytes}}" \
    -e SOURCE_FILESTORE_FILE_COUNT="${{source_filestore_file_count}}" \
    -e SOURCE_FILESTORE_SIZE_BYTES="${{source_filestore_size_bytes}}" \
    -e ACTIVE_DATA_FREE_BYTES="${{active_data_free_bytes}}" \
    -e STAGING_CLONE_VOLUME_ABSENT="${{staging_clone_volume_absent}}" \
    -e BACKUP_DESTINATION_ABSENT="${{backup_destination_absent}}" \
    -e POSTGRES_IMAGE_ID="${{postgres_image_id}}" \
    -e SCRIPT_RUNNER_IMAGE_ID="${{script_runner_image_id}}" \
    "${{script_runner_container_id}}" python3 - <<'PY'
import base64
import json
import os

payload = {{
    "schema_version": 1,
    "inspection_nonce": os.environ["INSPECTION_NONCE"],
    "backup_record_id": os.environ["BACKUP_RECORD_ID"],
    "active_db_volume": os.environ["ACTIVE_DB_VOLUME"],
    "active_data_volume": os.environ["ACTIVE_DATA_VOLUME"],
    "active_log_volume": os.environ["ACTIVE_LOG_VOLUME"],
    "source_db_volume": os.environ["SOURCE_DB_VOLUME"],
    "source_data_volume": os.environ["SOURCE_DATA_VOLUME"],
    "staging_clone_volume": os.environ["STAGING_CLONE_VOLUME"],
    "source_database_name": os.environ["SOURCE_DATABASE_NAME"],
    "destination_database_name": os.environ["DESTINATION_DATABASE_NAME"],
    "database_user": os.environ["DATABASE_USER"],
    "source_db_project_label": os.environ["SOURCE_DB_PROJECT_LABEL"],
    "source_db_role_label": os.environ["SOURCE_DB_ROLE_LABEL"],
    "source_data_project_label": os.environ["SOURCE_DATA_PROJECT_LABEL"],
    "source_data_role_label": os.environ["SOURCE_DATA_ROLE_LABEL"],
    "source_pg_version": os.environ["SOURCE_PG_VERSION"],
    "source_pg_control_sha256": os.environ["SOURCE_PG_CONTROL_SHA256"],
    "source_pg_control_size": int(os.environ["SOURCE_PG_CONTROL_SIZE"]),
    "source_pg_system_identifier": os.environ["SOURCE_PG_SYSTEM_IDENTIFIER"],
    "source_pg_cluster_state": os.environ["SOURCE_PG_CLUSTER_STATE"],
    "source_pg_checkpoint_location": os.environ["SOURCE_PG_CHECKPOINT_LOCATION"],
    "source_pg_checkpoint_redo_location": os.environ["SOURCE_PG_CHECKPOINT_REDO_LOCATION"],
    "source_pg_checkpoint_timeline_id": os.environ["SOURCE_PG_CHECKPOINT_TIMELINE_ID"],
    "source_pg_checkpoint_time": os.environ["SOURCE_PG_CHECKPOINT_TIME"],
    "source_db_volume_used_bytes": int(os.environ["SOURCE_DB_VOLUME_USED_BYTES"]),
    "source_filestore_file_count": int(os.environ["SOURCE_FILESTORE_FILE_COUNT"]),
    "source_filestore_size_bytes": int(os.environ["SOURCE_FILESTORE_SIZE_BYTES"]),
    "active_data_free_bytes": int(os.environ["ACTIVE_DATA_FREE_BYTES"]),
    "staging_clone_volume_absent": os.environ["STAGING_CLONE_VOLUME_ABSENT"] == "true",
    "backup_destination_absent": os.environ["BACKUP_DESTINATION_ABSENT"] == "true",
    "postgres_image_id": os.environ["POSTGRES_IMAGE_ID"],
    "script_runner_image_id": os.environ["SCRIPT_RUNNER_IMAGE_ID"],
}}
encoded = base64.b64encode(
    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
).decode("ascii")
print(f"{{os.environ['RESULT_MARKER']}}={{encoded}}", flush=True)
PY
"""


def _build_dokploy_odoo_retained_volume_backup_import_apply_script(
    *,
    compose_app_name: str,
    import_nonce: str,
    operation_id: str,
    plan_fingerprint: str,
    backup_record_id: str,
    active_db_volume: str,
    active_data_volume: str,
    active_log_volume: str,
    source_db_volume: str,
    source_data_volume: str,
    staging_clone_volume: str,
    source_database_name: str,
    destination_database_name: str,
    database_user: str,
    expected_source_compose_project: str,
    expected_source_pg_control_sha256: str,
    expected_source_pg_version: str,
    expected_postgres_image_id: str,
    expected_script_runner_image_id: str,
    expected_source_filestore_file_count: int,
    expected_source_filestore_size_bytes: int,
    expected_active_data_required_bytes: int,
    filestore_relative_path: str,
    backup_dir_relative_path: str,
    database_dump_relative_path: str,
    filestore_archive_relative_path: str,
    manifest_relative_path: str,
) -> str:
    return f"""#!/usr/bin/env bash
set -euo pipefail

compose_project={shlex.quote(compose_app_name)}
import_nonce={shlex.quote(import_nonce)}
operation_id={shlex.quote(operation_id)}
plan_fingerprint={shlex.quote(plan_fingerprint)}
backup_record_id={shlex.quote(backup_record_id)}
active_db_volume={shlex.quote(active_db_volume)}
active_data_volume={shlex.quote(active_data_volume)}
active_log_volume={shlex.quote(active_log_volume)}
source_db_volume={shlex.quote(source_db_volume)}
source_data_volume={shlex.quote(source_data_volume)}
staging_clone_volume={shlex.quote(staging_clone_volume)}
source_database_name={shlex.quote(source_database_name)}
destination_database_name={shlex.quote(destination_database_name)}
database_user={shlex.quote(database_user)}
expected_source_compose_project={shlex.quote(expected_source_compose_project)}
expected_source_pg_control_sha256={shlex.quote(expected_source_pg_control_sha256)}
expected_source_pg_version={shlex.quote(expected_source_pg_version)}
expected_postgres_image_id={shlex.quote(expected_postgres_image_id)}
expected_script_runner_image_id={shlex.quote(expected_script_runner_image_id)}
expected_source_filestore_file_count={expected_source_filestore_file_count}
expected_source_filestore_size_bytes={expected_source_filestore_size_bytes}
expected_active_data_required_bytes={expected_active_data_required_bytes}
filestore_relative_path={shlex.quote(filestore_relative_path)}
backup_dir_relative_path={shlex.quote(backup_dir_relative_path)}
database_dump_relative_path={shlex.quote(database_dump_relative_path)}
filestore_archive_relative_path={shlex.quote(filestore_archive_relative_path)}
manifest_relative_path={shlex.quote(manifest_relative_path)}
name_suffix=${{plan_fingerprint:0:16}}
clone_container="launchplane-retained-db-${{name_suffix}}"
clone_network="launchplane-retained-net-${{name_suffix}}"
cleanup_failed=0
clone_container_created=0
clone_network_created=0

cleanup() {{
    local exit_status="$?"
    if [ "${{clone_container_created}}" = "1" ]; then
        docker stop --time 30 "${{clone_container}}" >/dev/null 2>&1 || cleanup_failed=1
        docker rm -f "${{clone_container}}" >/dev/null 2>&1 || cleanup_failed=1
    fi
    if [ "${{clone_network_created}}" = "1" ]; then
        docker network rm "${{clone_network}}" >/dev/null 2>&1 || cleanup_failed=1
    fi
    if [ "${{exit_status}}" = "0" ] && [ "${{cleanup_failed}}" != "0" ]; then
        exit 1
    fi
    exit "${{exit_status}}"
}}
trap cleanup EXIT

resolve_single_container() {{
    local service_name="$1"
    local container_ids
    container_ids=$(docker ps -q \
        --filter "label=com.docker.compose.project=${{compose_project}}" \
        --filter "label=com.docker.compose.service=${{service_name}}")
    if [ "$(printf '%s\n' "${{container_ids}}" | sed '/^$/d' | wc -l | tr -d ' ')" != "1" ]; then
        echo "Expected exactly one running ${{service_name}} container for ${{compose_project}}." >&2
        exit 1
    fi
    printf '%s' "${{container_ids}}"
}}

resolve_single_container_any_state() {{
    local service_name="$1"
    local container_ids
    container_ids=$(docker ps -aq \
        --filter "label=com.docker.compose.project=${{compose_project}}" \
        --filter "label=com.docker.compose.service=${{service_name}}")
    if [ "$(printf '%s\n' "${{container_ids}}" | sed '/^$/d' | wc -l | tr -d ' ')" != "1" ]; then
        echo "Expected exactly one ${{service_name}} container for ${{compose_project}}." >&2
        exit 1
    fi
    printf '%s' "${{container_ids}}"
}}

volume_label() {{
    local volume_name="$1"
    local label_name="$2"
    docker volume inspect -f "{{{{ index .Labels \\"${{label_name}}\\" }}}}" "${{volume_name}}"
}}

container_mounts_volume() {{
    local container_id="$1"
    local volume_name="$2"
    docker inspect -f '{{{{range .Mounts}}}}{{{{println .Name}}}}{{{{end}}}}' \
        "${{container_id}}" | grep -Fx -- "${{volume_name}}" >/dev/null
}}

for volume_name in \
    "${{active_db_volume}}" "${{active_data_volume}}" "${{active_log_volume}}" \
    "${{source_db_volume}}" "${{source_data_volume}}"; do
    docker volume inspect "${{volume_name}}" >/dev/null
done
if docker volume inspect "${{staging_clone_volume}}" >/dev/null 2>&1; then
    echo "Staging clone volume already exists." >&2
    exit 1
fi
if [ -n "$(docker ps -q --filter "volume=${{source_db_volume}}")" ] || \
   [ -n "$(docker ps -q --filter "volume=${{source_data_volume}}")" ]; then
    echo "A retained source volume is mounted by a running container." >&2
    exit 1
fi
if [ "$(volume_label "${{source_db_volume}}" com.docker.compose.project)" != \
     "${{expected_source_compose_project}}" ] || \
   [ "$(volume_label "${{source_data_volume}}" com.docker.compose.project)" != \
     "${{expected_source_compose_project}}" ] || \
   [ "$(volume_label "${{source_db_volume}}" com.docker.compose.volume)" != "odoo_db" ] || \
   [ "$(volume_label "${{source_data_volume}}" com.docker.compose.volume)" != "odoo_data" ]; then
    echo "Retained source volume labels drifted from the reviewed plan." >&2
    exit 1
fi

database_container_id=$(resolve_single_container_any_state database)
script_runner_container_id=$(resolve_single_container script-runner)
postgres_image_id=$(docker inspect -f '{{{{.Image}}}}' "${{database_container_id}}")
script_runner_image_id=$(docker inspect -f '{{{{.Image}}}}' "${{script_runner_container_id}}")
postgres_binary_version=$(docker run --rm --read-only --network none \
    --entrypoint postgres "${{postgres_image_id}}" --version)
active_database_user=$(docker inspect -f '{{{{range .Config.Env}}}}{{{{println .}}}}{{{{end}}}}' \
    "${{database_container_id}}" | sed -n 's/^POSTGRES_USER=//p' | tail -n 1)
active_destination_database_name=$(docker inspect \
    -f '{{{{range .Config.Env}}}}{{{{println .}}}}{{{{end}}}}' \
    "${{script_runner_container_id}}" | sed -n 's/^ODOO_DB_NAME=//p' | tail -n 1)
if [ "${{postgres_image_id}}" != "${{expected_postgres_image_id}}" ] || \
   [ "${{script_runner_image_id}}" != "${{expected_script_runner_image_id}}" ] || \
   [ "${{active_database_user}}" != "${{database_user}}" ] || \
   [ "${{active_destination_database_name}}" != "${{destination_database_name}}" ]; then
    echo "Active provider image or database identity drifted from the reviewed plan." >&2
    exit 1
fi
case "${{postgres_binary_version}}" in
    "postgres (PostgreSQL) 17" | "postgres (PostgreSQL) 17."*) ;;
    *) echo "Active PostgreSQL image is not major version 17." >&2; exit 1 ;;
esac
if ! container_mounts_volume "${{database_container_id}}" "${{active_db_volume}}" || \
   ! container_mounts_volume "${{script_runner_container_id}}" "${{active_data_volume}}" || \
   ! container_mounts_volume "${{script_runner_container_id}}" "${{active_log_volume}}"; then
    echo "Active container volume mounts drifted from the reviewed plan." >&2
    exit 1
fi

source_pg_version=$(docker run --rm --read-only --network none --user 0:0 \
    --mount "type=volume,src=${{source_db_volume}},dst=/source,readonly" \
    --entrypoint /bin/bash "${{postgres_image_id}}" -lc 'cat /source/PG_VERSION')
source_pg_control_sha256=$(docker run --rm --read-only --network none --user 0:0 \
    --mount "type=volume,src=${{source_db_volume}},dst=/source,readonly" \
    --entrypoint /bin/bash "${{postgres_image_id}}" \
    -lc 'sha256sum /source/global/pg_control | awk '"'"'{{print $1}}'"'"'')
if [ "${{source_pg_version}}" != "${{expected_source_pg_version}}" ] || \
   [ "${{source_pg_control_sha256}}" != "${{expected_source_pg_control_sha256}}" ]; then
    echo "Retained PostgreSQL source drifted from the reviewed plan." >&2
    exit 1
fi

filestore_metrics=$(docker run --rm --read-only --network none --user 0:0 \
    --mount "type=volume,src=${{source_data_volume}},dst=/source-data,readonly" \
    --entrypoint python3 "${{script_runner_image_id}}" -c '
import os
import stat
import sys
root = os.path.join("/source-data", sys.argv[1], sys.argv[2])
if not os.path.isdir(root) or os.path.islink(root):
    raise SystemExit("retained filestore path is missing or unsafe")
count = 0
size = 0
for current_root, directories, files in os.walk(root, followlinks=False):
    for directory in directories:
        if os.path.islink(os.path.join(current_root, directory)):
            raise SystemExit("retained filestore contains a symbolic link")
    for name in files:
        path = os.path.join(current_root, name)
        mode = os.lstat(path).st_mode
        if not stat.S_ISREG(mode):
            raise SystemExit("retained filestore contains a non-regular file")
        count += 1
        size += os.lstat(path).st_size
print(f"{{count}}:{{size}}")
' "${{filestore_relative_path}}" "${{source_database_name}}")
source_filestore_file_count=${{filestore_metrics%%:*}}
source_filestore_size_bytes=${{filestore_metrics#*:}}
if [ "${{source_filestore_file_count}}" != "${{expected_source_filestore_file_count}}" ] || \
   [ "${{source_filestore_size_bytes}}" != "${{expected_source_filestore_size_bytes}}" ]; then
    echo "Retained filestore drifted from the reviewed plan." >&2
    exit 1
fi

active_data_free_bytes=$(docker run --rm --read-only --network none --user 0:0 \
    --mount "type=volume,src=${{active_data_volume}},dst=/active-data,readonly" \
    --entrypoint python3 "${{script_runner_image_id}}" \
    -c 'import shutil; print(shutil.disk_usage("/active-data").free)')
if [ "${{active_data_free_bytes}}" -lt "${{expected_active_data_required_bytes}}" ]; then
    echo "Active data volume no longer has the reviewed free space." >&2
    exit 1
fi
backup_destination_absent=$(docker run --rm --read-only --network none --user 0:0 \
    --mount "type=volume,src=${{active_data_volume}},dst=/active-data,readonly" \
    --entrypoint python3 "${{script_runner_image_id}}" -c '
import os
import sys
print("true" if not os.path.lexists(os.path.join("/active-data", sys.argv[1])) else "false")
' "${{backup_dir_relative_path}}")
if [ "${{backup_destination_absent}}" != "true" ]; then
    echo "Backup destination already exists." >&2
    exit 1
fi
if docker container inspect "${{clone_container}}" >/dev/null 2>&1 || \
   docker network inspect "${{clone_network}}" >/dev/null 2>&1; then
    echo "Isolated clone container or network name already exists." >&2
    exit 1
fi

created_staging_clone_volume=$(docker volume create \
    --label io.launchplane.recovery=true \
    --label "io.launchplane.recovery.operation_id=${{operation_id}}" \
    --label "io.launchplane.recovery.backup_record_id=${{backup_record_id}}" \
    --label "io.launchplane.recovery.plan_fingerprint=${{plan_fingerprint}}" \
    --label "io.launchplane.recovery.import_nonce=${{import_nonce}}" \
    --label "io.launchplane.recovery.source_volume=${{source_db_volume}}" \
    "${{staging_clone_volume}}")
if [ "${{created_staging_clone_volume}}" != "${{staging_clone_volume}}" ] || \
   [ "$(volume_label "${{staging_clone_volume}}" io.launchplane.recovery)" != "true" ] || \
   [ "$(volume_label "${{staging_clone_volume}}" io.launchplane.recovery.operation_id)" != \
     "${{operation_id}}" ] || \
   [ "$(volume_label "${{staging_clone_volume}}" io.launchplane.recovery.backup_record_id)" != \
     "${{backup_record_id}}" ] || \
   [ "$(volume_label "${{staging_clone_volume}}" io.launchplane.recovery.plan_fingerprint)" != \
     "${{plan_fingerprint}}" ] || \
   [ "$(volume_label "${{staging_clone_volume}}" io.launchplane.recovery.import_nonce)" != \
     "${{import_nonce}}" ] || \
   [ "$(volume_label "${{staging_clone_volume}}" io.launchplane.recovery.source_volume)" != \
     "${{source_db_volume}}" ]; then
    echo "Staging clone volume was not created with the exact recovery labels." >&2
    exit 1
fi

docker run --rm --read-only --network none --user 0:0 \
    --mount "type=volume,src=${{source_db_volume}},dst=/source,readonly" \
    --mount "type=volume,src=${{staging_clone_volume}},dst=/clone" \
    --tmpfs /tmp:rw,noexec,nosuid,size=64m \
    --entrypoint /bin/bash "${{script_runner_image_id}}" -lc '
        set -euo pipefail
        cp -a /source/. /clone/
        sync
    '

clone_pg_control_sha256=$(docker run --rm --read-only --network none --user 0:0 \
    --mount "type=volume,src=${{staging_clone_volume}},dst=/clone,readonly" \
    --entrypoint /bin/bash "${{postgres_image_id}}" \
    -lc 'sha256sum /clone/global/pg_control | awk '"'"'{{print $1}}'"'"'')
if [ "${{clone_pg_control_sha256}}" != "${{source_pg_control_sha256}}" ]; then
    echo "Staging clone pg_control fingerprint does not match the retained source." >&2
    exit 1
fi
docker run --rm --read-only --network none --user 0:0 \
    --mount "type=volume,src=${{staging_clone_volume}},dst=/clone" \
    --entrypoint /bin/bash "${{script_runner_image_id}}" \
    -lc 'rm -f /clone/postmaster.pid'

docker network create --internal \
    --label io.launchplane.recovery=true \
    --label "io.launchplane.recovery.operation_id=${{operation_id}}" \
    "${{clone_network}}" >/dev/null
clone_network_created=1
docker create \
    --name "${{clone_container}}" \
    --network "${{clone_network}}" \
    --label io.launchplane.recovery=true \
    --label "io.launchplane.recovery.operation_id=${{operation_id}}" \
    --mount "type=volume,src=${{staging_clone_volume}},dst=/var/lib/postgresql/data" \
    "${{postgres_image_id}}" >/dev/null
clone_container_created=1
docker start "${{clone_container}}" >/dev/null

ready=0
for _attempt in $(seq 1 60); do
    if docker exec -u postgres "${{clone_container}}" \
        pg_isready --host /var/run/postgresql --username "${{database_user}}" >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 2
done
if [ "${{ready}}" != "1" ]; then
    echo "Staging PostgreSQL clone did not become ready." >&2
    exit 1
fi
database_exists=$(docker exec -u postgres "${{clone_container}}" \
    psql --host /var/run/postgresql --username "${{database_user}}" --dbname postgres \
    --no-psqlrc --tuples-only --no-align --set=source_database="${{source_database_name}}" \
    --command "SELECT 1 FROM pg_database WHERE datname = :'source_database';")
if [ "${{database_exists}}" != "1" ]; then
    echo "Requested retained source database does not exist in the staging clone." >&2
    exit 1
fi

script_runner_uid=$(docker exec "${{script_runner_container_id}}" id -u)
script_runner_gid=$(docker exec "${{script_runner_container_id}}" id -g)
docker run --rm --network none --read-only --user 0:0 \
    --mount "type=volume,src=${{active_data_volume}},dst=/active-data" \
    --entrypoint /bin/bash "${{script_runner_image_id}}" -c '
        set -euo pipefail
        backup_dir="/active-data/$1"
        if [ -e "$backup_dir" ]; then
            echo "Backup destination already exists." >&2
            exit 1
        fi
        backup_parent=$(dirname "$backup_dir")
        install -d -m 700 -o "$2" -g "$3" "$backup_parent"
        mkdir -m 700 "$backup_dir"
        chown "$2:$3" "$backup_dir"
    ' -- "${{backup_dir_relative_path}}" "${{script_runner_uid}}" "${{script_runner_gid}}"

docker exec -u postgres "${{clone_container}}" \
    pg_dump --host /var/run/postgresql --username "${{database_user}}" \
    --format custom "${{source_database_name}}" | \
docker run --rm -i --network none --read-only --user 0:0 \
    --mount "type=volume,src=${{active_data_volume}},dst=/active-data" \
    --entrypoint /bin/bash "${{script_runner_image_id}}" -c '
        set -euo pipefail
        umask 077
        cat > "/active-data/$1"
        test -s "/active-data/$1"
    ' -- "${{database_dump_relative_path}}"

docker run --rm --network none --read-only --user 0:0 \
    --mount "type=volume,src=${{source_data_volume}},dst=/source-data,readonly" \
    --mount "type=volume,src=${{active_data_volume}},dst=/active-data" \
    --tmpfs /tmp:rw,noexec,nosuid,size=64m \
    --entrypoint python3 "${{script_runner_image_id}}" -c '
import os
import sys
import tarfile
source_path = os.path.join("/source-data", sys.argv[1], sys.argv[2])
archive_path = os.path.join("/active-data", sys.argv[3])
if not os.path.isdir(source_path) or os.path.islink(source_path):
    raise SystemExit("retained filestore path is missing or unsafe")
with tarfile.open(archive_path, "w:gz", dereference=False) as archive:
    archive.add(source_path, arcname=sys.argv[4], recursive=True)
if not os.path.isfile(archive_path) or os.path.getsize(archive_path) < 1:
    raise SystemExit("retained filestore archive is empty")
' "${{filestore_relative_path}}" "${{source_database_name}}" \
    "${{filestore_archive_relative_path}}" "${{destination_database_name}}"

docker run --rm -i --network none --read-only --user 0:0 \
    --mount "type=volume,src=${{active_data_volume}},dst=/active-data" \
    --entrypoint python3 "${{script_runner_image_id}}" - \
    "${{database_dump_relative_path}}" "${{filestore_archive_relative_path}}" \
    "${{manifest_relative_path}}" "${{backup_dir_relative_path}}" <<'PY'
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

database_dump_path = os.path.join("/active-data", sys.argv[1])
filestore_archive_path = os.path.join("/active-data", sys.argv[2])
manifest_path = os.path.join("/active-data", sys.argv[3])
backup_dir = os.path.join("/active-data", sys.argv[4])
payload = {{
    "schema_version": 1,
    "backup_record_id": {backup_record_id!r},
    "database_name": {destination_database_name!r},
    "backup_dir": backup_dir.replace("/active-data", "/volumes/data", 1),
    "database_dump_path": database_dump_path.replace("/active-data", "/volumes/data", 1),
    "filestore_archive_path": filestore_archive_path.replace("/active-data", "/volumes/data", 1),
    "manifest_path": manifest_path.replace("/active-data", "/volumes/data", 1),
    "database_dump_size": os.path.getsize(database_dump_path),
    "filestore_archive_size": os.path.getsize(filestore_archive_path),
    "database_dump_sha256": sha256_file(database_dump_path),
    "filestore_archive_sha256": sha256_file(filestore_archive_path),
    "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
}}
with open(manifest_path, "x", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\\n")
PY

docker run --rm --network none --read-only --user 0:0 \
    --mount "type=volume,src=${{active_data_volume}},dst=/active-data" \
    --entrypoint /bin/bash "${{script_runner_image_id}}" -c '
        set -euo pipefail
        chown "$1:$2" "/active-data/$3" "/active-data/$4" "/active-data/$5"
        chmod 600 "/active-data/$3" "/active-data/$4" "/active-data/$5"
    ' -- "${{script_runner_uid}}" "${{script_runner_gid}}" \
    "${{database_dump_relative_path}}" "${{filestore_archive_relative_path}}" \
    "${{manifest_relative_path}}"

docker run --rm -i --network none --read-only --user 0:0 \
    --mount "type=volume,src=${{active_data_volume}},dst=/active-data,readonly" \
    --entrypoint python3 "${{script_runner_image_id}}" - \
    "${{database_dump_relative_path}}" "${{filestore_archive_relative_path}}" \
    "${{source_pg_control_sha256}}" "${{clone_pg_control_sha256}}" <<'PY'
import base64
import hashlib
import json
import os
import sys

def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

database_dump_path = os.path.join("/active-data", sys.argv[1])
filestore_archive_path = os.path.join("/active-data", sys.argv[2])
payload = {{
    "schema_version": 1,
    "import_nonce": {import_nonce!r},
    "operation_id": {operation_id!r},
    "plan_fingerprint": {plan_fingerprint!r},
    "backup_record_id": {backup_record_id!r},
    "source_db_volume": {source_db_volume!r},
    "source_data_volume": {source_data_volume!r},
    "staging_clone_volume": {staging_clone_volume!r},
    "source_database_name": {source_database_name!r},
    "destination_database_name": {destination_database_name!r},
    "database_user": {database_user!r},
    "source_pg_control_sha256": sys.argv[3],
    "clone_pg_control_sha256": sys.argv[4],
    "database_dump_sha256": sha256_file(database_dump_path),
    "filestore_archive_sha256": sha256_file(filestore_archive_path),
    "database_dump_size": os.path.getsize(database_dump_path),
    "filestore_archive_size": os.path.getsize(filestore_archive_path),
    "source_filestore_file_count": {expected_source_filestore_file_count},
    "source_filestore_size_bytes": {expected_source_filestore_size_bytes},
}}
encoded = base64.b64encode(
    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
).decode("ascii")
print(f"{{{ODOO_RETAINED_VOLUME_BACKUP_IMPORT_APPLY_RESULT_MARKER!r}}}={{encoded}}", flush=True)
PY
"""


def _build_dokploy_odoo_backup_restore_database_script(
    *,
    compose_app_name: str,
    operation_id: str,
    database_name: str,
    database_dump_path: str,
    database_dump_sha256: str,
    old_db_volume: str,
    new_db_volume: str,
    data_volume: str,
    log_volume: str,
) -> str:
    header = "\n".join(
        (
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            f"compose_project={shlex.quote(compose_app_name)}",
            f"operation_id={shlex.quote(operation_id)}",
            f"database_name={shlex.quote(database_name)}",
            f"database_dump_path={shlex.quote(database_dump_path)}",
            f"expected_database_dump_sha256={shlex.quote(database_dump_sha256)}",
            f"old_db_volume={shlex.quote(old_db_volume)}",
            f"new_db_volume={shlex.quote(new_db_volume)}",
            f"data_volume={shlex.quote(data_volume)}",
            f"log_volume={shlex.quote(log_volume)}",
            f"result_marker={shlex.quote(ODOO_BACKUP_RESTORE_RESULT_MARKER)}",
            "",
        )
    )
    return (
        header
        + r"""resolve_container_id() {
    local service_name="$1"
    local container_id
    container_id=$(docker ps -aq \
        --filter "label=com.docker.compose.project=${compose_project}" \
        --filter "label=com.docker.compose.service=${service_name}" | head -n 1)
    if [ -z "${container_id}" ]; then
        echo "Missing ${service_name} container for ${compose_project}." >&2
        exit 1
    fi
    printf '%s' "${container_id}"
}

container_mount_source() {
    local container_id="$1"
    local destination="$2"
    docker inspect "${container_id}" | python3 -c '
import json
import sys
destination = sys.argv[1]
payload = json.load(sys.stdin)
mounts = payload[0].get("Mounts", []) if payload else []
matches = [mount.get("Name") or mount.get("Source") or "" for mount in mounts if mount.get("Destination") == destination]
if len(matches) != 1 or not matches[0]:
    raise SystemExit(1)
print(matches[0])
' "${destination}"
}

require_mount() {
    local container_id="$1"
    local destination="$2"
    local expected_source="$3"
    local actual_source
    actual_source=$(container_mount_source "${container_id}" "${destination}")
    if [ "${actual_source}" != "${expected_source}" ]; then
        echo "Unexpected mount source for ${destination}." >&2
        exit 1
    fi
}

volume_label() {
    local volume_name="$1"
    local label_name="$2"
    docker volume inspect -f "{{ index .Labels \"${label_name}\" }}" "${volume_name}"
}

database_container_id=$(resolve_container_id database)
script_runner_container_id=$(resolve_container_id script-runner)
web_container_id=$(resolve_container_id web)
require_mount "${database_container_id}" /var/lib/postgresql/data "${old_db_volume}"
require_mount "${script_runner_container_id}" /volumes/data "${data_volume}"
require_mount "${web_container_id}" /volumes/data "${data_volume}"
require_mount "${web_container_id}" /volumes/logs "${log_volume}"

if docker volume inspect "${new_db_volume}" >/dev/null 2>&1; then
    echo "Restore DB volume already exists; refusing to overwrite it." >&2
    exit 1
fi

postgres_image=$(docker inspect -f '{{.Image}}' "${database_container_id}")
case "${postgres_image}" in
    sha256:[0-9a-f][0-9a-f]*) ;;
    *)
        echo "Odoo backup restore requires an immutable PostgreSQL image id." >&2
        exit 1
        ;;
esac
postgres_binary_version=$(docker run --rm --read-only --network none \
    --entrypoint postgres "${postgres_image}" --version)
case "${postgres_binary_version}" in
    "postgres (PostgreSQL) 17" | "postgres (PostgreSQL) 17."*) ;;
    *)
        echo "Odoo backup restore requires the current PostgreSQL 17 image." >&2
        exit 1
        ;;
esac

db_user=""
db_password=""
while IFS= read -r env_line; do
    case "${env_line}" in
        POSTGRES_USER=*) db_user=${env_line#POSTGRES_USER=} ;;
        POSTGRES_PASSWORD=*) db_password=${env_line#POSTGRES_PASSWORD=} ;;
    esac
done < <(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "${database_container_id}")
if [ -z "${db_user}" ] || [ -z "${db_password}" ]; then
    echo "Current database container does not expose required PostgreSQL credentials." >&2
    exit 1
fi

actual_database_dump_sha256=$(docker exec "${script_runner_container_id}" sha256sum "${database_dump_path}" | awk '{print $1}')
if [ "${actual_database_dump_sha256}" != "${expected_database_dump_sha256}" ]; then
    echo "Database dump hash changed after reviewed restore plan." >&2
    exit 1
fi

network_name=$(docker inspect -f '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}' "${script_runner_container_id}" | head -n 1)
if [ -z "${network_name}" ]; then
    echo "Script runner has no Docker network for restore staging." >&2
    exit 1
fi

operation_digest=$(python3 - "${operation_id}" <<'PY'
import hashlib
import sys
print(hashlib.sha256(sys.argv[1].encode("utf-8")).hexdigest()[:20])
PY
)
restore_container_name="launchplane-odoo-restore-${operation_digest}"
restore_database_host="launchplane-odoo-restore-db-${operation_digest}"

created_new_db_volume=$(docker volume create \
    --label "launchplane.restore.operation=${operation_id}" \
    --label "launchplane.restore.preserve=true" \
    "${new_db_volume}")
if [ "${created_new_db_volume}" != "${new_db_volume}" ] || \
   [ "$(volume_label "${new_db_volume}" launchplane.restore.operation)" != \
     "${operation_id}" ] || \
   [ "$(volume_label "${new_db_volume}" launchplane.restore.preserve)" != "true" ]; then
    echo "Restore DB volume was not created with the exact recovery labels." >&2
    exit 1
fi

docker run --rm \
    --volume "${new_db_volume}:/var/lib/postgresql/data" \
    --entrypoint /bin/sh \
    "${postgres_image}" \
    -c 'test -z "$(ls -A /var/lib/postgresql/data)"'

restore_container_started=0
cleanup_restore_container() {
    if [ "${restore_container_started}" = "1" ]; then
        docker rm -f "${restore_container_name}" >/dev/null 2>&1 || true
    fi
}
trap cleanup_restore_container EXIT

docker run -d \
    --name "${restore_container_name}" \
    --network "${network_name}" \
    --network-alias "${restore_database_host}" \
    --volume "${new_db_volume}:/var/lib/postgresql/data" \
    --env "POSTGRES_USER=${db_user}" \
    --env "POSTGRES_PASSWORD=${db_password}" \
    --env POSTGRES_DB=postgres \
    "${postgres_image}" >/dev/null
restore_container_started=1

for _ in $(seq 1 90); do
    if docker exec "${restore_container_name}" pg_isready -U "${db_user}" -d postgres -h 127.0.0.1 >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
docker exec "${restore_container_name}" pg_isready -U "${db_user}" -d postgres -h 127.0.0.1 >/dev/null

case "${database_name}" in
    postgres | template0 | template1)
        echo "Refusing to restore into a PostgreSQL system database." >&2
        exit 1
        ;;
esac
docker exec "${restore_container_name}" createdb --username "${db_user}" "${database_name}"
docker exec \
    --env "PGPASSWORD=${db_password}" \
    "${script_runner_container_id}" \
    pg_restore \
        --host "${restore_database_host}" \
        --port 5432 \
        --username "${db_user}" \
        --dbname "${database_name}" \
        --exit-on-error \
        --no-owner \
        --no-acl \
        "${database_dump_path}"

restored_table_count=$(docker exec \
    --env "PGPASSWORD=${db_password}" \
    "${restore_container_name}" \
    psql --tuples-only --no-align --username "${db_user}" --dbname "${database_name}" \
    --command "SELECT count(*) FROM pg_catalog.pg_class WHERE relkind IN ('r','p') AND relnamespace NOT IN (SELECT oid FROM pg_catalog.pg_namespace WHERE nspname LIKE 'pg_%' OR nspname = 'information_schema');")
if ! [[ "${restored_table_count}" =~ ^[1-9][0-9]*$ ]]; then
    echo "Restored database does not contain application tables." >&2
    exit 1
fi

docker stop --time 60 "${restore_container_name}" >/dev/null
docker rm "${restore_container_name}" >/dev/null
restore_container_started=0
trap - EXIT

python3 - "${result_marker}" "${operation_id}" "${new_db_volume}" "${actual_database_dump_sha256}" "${restored_table_count}" "${postgres_image}" <<'PY'
import base64
import json
import sys

marker, operation_id, volume, digest, table_count, image = sys.argv[1:]
payload = {
    "schema_version": 1,
    "operation_id": operation_id,
    "phase": "database_restore",
    "evidence": {
        "new_db_volume": volume,
        "database_dump_sha256": digest,
        "restored_table_count": table_count,
        "postgres_image": image,
    },
}
encoded = base64.b64encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).decode()
print(marker + "=" + encoded)
PY
"""
    )


def _build_dokploy_odoo_backup_restore_filestore_stage_script(
    *,
    compose_app_name: str,
    operation_id: str,
    database_name: str,
    filestore_path: str,
    filestore_archive_path: str,
    filestore_archive_sha256: str,
    filestore_member_count: int,
    filestore_unpacked_size: int,
    filestore_staging_path: str,
    filestore_quarantine_path: str,
    data_volume: str,
    log_volume: str,
) -> str:
    header = "\n".join(
        (
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            f"compose_project={shlex.quote(compose_app_name)}",
            f"operation_id={shlex.quote(operation_id)}",
            f"database_name={shlex.quote(database_name)}",
            f"filestore_path={shlex.quote(filestore_path)}",
            f"filestore_archive_path={shlex.quote(filestore_archive_path)}",
            f"expected_filestore_archive_sha256={shlex.quote(filestore_archive_sha256)}",
            f"expected_filestore_member_count={filestore_member_count}",
            f"expected_filestore_unpacked_size={filestore_unpacked_size}",
            f"filestore_staging_path={shlex.quote(filestore_staging_path)}",
            f"filestore_quarantine_path={shlex.quote(filestore_quarantine_path)}",
            f"data_volume={shlex.quote(data_volume)}",
            f"log_volume={shlex.quote(log_volume)}",
            f"result_marker={shlex.quote(ODOO_BACKUP_RESTORE_RESULT_MARKER)}",
            "",
        )
    )
    return (
        header
        + r"""resolve_container_id() {
    local service_name="$1"
    docker ps -aq \
        --filter "label=com.docker.compose.project=${compose_project}" \
        --filter "label=com.docker.compose.service=${service_name}" | head -n 1
}

container_mount_source() {
    local container_id="$1"
    local destination="$2"
    docker inspect "${container_id}" | python3 -c '
import json
import sys
destination = sys.argv[1]
payload = json.load(sys.stdin)
matches = [mount.get("Name") or mount.get("Source") or "" for mount in payload[0].get("Mounts", []) if mount.get("Destination") == destination]
if len(matches) != 1 or not matches[0]:
    raise SystemExit(1)
print(matches[0])
' "${destination}"
}

script_runner_container_id=$(resolve_container_id script-runner)
web_container_id=$(resolve_container_id web)
if [ -z "${script_runner_container_id}" ] || [ -z "${web_container_id}" ]; then
    echo "Odoo backup restore requires script-runner and web containers." >&2
    exit 1
fi
if [ "$(container_mount_source "${script_runner_container_id}" /volumes/data)" != "${data_volume}" ] || \
   [ "$(container_mount_source "${web_container_id}" /volumes/data)" != "${data_volume}" ] || \
   [ "$(container_mount_source "${web_container_id}" /volumes/logs)" != "${log_volume}" ]; then
    echo "Odoo backup restore data/log volume authority changed." >&2
    exit 1
fi

docker exec -u root -i \
    --env "OPERATION_ID=${operation_id}" \
    --env "DATABASE_NAME=${database_name}" \
    --env "FILESTORE_PATH=${filestore_path}" \
    --env "FILESTORE_ARCHIVE_PATH=${filestore_archive_path}" \
    --env "EXPECTED_FILESTORE_ARCHIVE_SHA256=${expected_filestore_archive_sha256}" \
    --env "EXPECTED_FILESTORE_MEMBER_COUNT=${expected_filestore_member_count}" \
    --env "EXPECTED_FILESTORE_UNPACKED_SIZE=${expected_filestore_unpacked_size}" \
    --env "FILESTORE_STAGING_PATH=${filestore_staging_path}" \
    --env "FILESTORE_QUARANTINE_PATH=${filestore_quarantine_path}" \
    "${script_runner_container_id}" \
    python3 - <<'PY'
import hashlib
import os
import shutil
import tarfile
from pathlib import Path, PurePosixPath

database_name = os.environ["DATABASE_NAME"]
filestore_root = Path(os.environ["FILESTORE_PATH"])
archive_path = Path(os.environ["FILESTORE_ARCHIVE_PATH"])
staging_path = Path(os.environ["FILESTORE_STAGING_PATH"])
quarantine_path = Path(os.environ["FILESTORE_QUARANTINE_PATH"])
current_path = filestore_root if filestore_root.name == database_name else filestore_root / database_name
expected_member_count = int(os.environ["EXPECTED_FILESTORE_MEMBER_COUNT"])
expected_unpacked_size = int(os.environ["EXPECTED_FILESTORE_UNPACKED_SIZE"])

if not current_path.is_dir() or current_path.is_symlink():
    raise SystemExit("Current filestore is unavailable for quarantine ownership evidence.")
if staging_path.parent != current_path.parent or quarantine_path.parent != current_path.parent:
    raise SystemExit("Filestore staging and quarantine paths must be siblings of the live filestore.")
if staging_path.exists() or quarantine_path.exists():
    raise SystemExit("Filestore staging or quarantine path already exists.")
if archive_path.is_symlink() or not archive_path.is_file():
    raise SystemExit("Filestore archive is unavailable.")

digest = hashlib.sha256()
with archive_path.open("rb") as handle:
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
if digest.hexdigest() != os.environ["EXPECTED_FILESTORE_ARCHIVE_SHA256"]:
    raise SystemExit("Filestore archive hash changed after reviewed restore plan.")
if shutil.disk_usage(current_path.parent).free < expected_unpacked_size:
    raise SystemExit("Insufficient data-volume space for filestore staging.")

owner = current_path.stat()
member_count = 0
unpacked_size = 0
seen = set()
members = []
with tarfile.open(archive_path, mode="r:gz") as archive:
    for member in archive.getmembers():
        member_path = PurePosixPath(member.name)
        if (
            member_path.is_absolute()
            or not member_path.parts
            or any(part in {"", ".", ".."} for part in member_path.parts)
            or member_path.parts[0] != database_name
            or (not member.isfile() and not member.isdir())
        ):
            raise SystemExit("Filestore archive contains an unsafe member.")
        normalized = member_path.as_posix()
        if normalized in seen:
            raise SystemExit("Filestore archive contains duplicate members.")
        seen.add(normalized)
        members.append(member)
        member_count += 1
        if member.isfile():
            unpacked_size += member.size
    if member_count != expected_member_count or unpacked_size != expected_unpacked_size:
        raise SystemExit("Filestore archive shape changed after reviewed restore plan.")
    if database_name not in {PurePosixPath(member.name).as_posix().rstrip("/") for member in members if member.isdir()}:
        raise SystemExit("Filestore archive lacks the expected top-level database directory.")
    try:
        staging_path.mkdir(mode=0o700)
        for member in members:
            relative_path = PurePosixPath(member.name)
            destination = staging_path.joinpath(*relative_path.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if member.isdir():
                destination.mkdir(exist_ok=True)
            else:
                source = archive.extractfile(member)
                if source is None:
                    raise SystemExit("Filestore archive member could not be read.")
                with destination.open("xb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
            os.chmod(destination, member.mode & 0o777)
            os.chown(destination, owner.st_uid, owner.st_gid)
        os.chown(staging_path, owner.st_uid, owner.st_gid)
    except BaseException:
        shutil.rmtree(staging_path, ignore_errors=True)
        raise
PY

python3 - "${result_marker}" "${operation_id}" "${filestore_staging_path}" "${filestore_archive_sha256}" "${filestore_member_count}" "${filestore_unpacked_size}" <<'PY'
import base64
import json
import sys
marker, operation_id, staging_path, digest, member_count, unpacked_size = sys.argv[1:]
payload = {
    "schema_version": 1,
    "operation_id": operation_id,
    "phase": "filestore_stage",
    "evidence": {
        "filestore_staging_path": staging_path,
        "filestore_archive_sha256": digest,
        "filestore_member_count": member_count,
        "filestore_unpacked_size": unpacked_size,
    },
}
encoded = base64.b64encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).decode()
print(marker + "=" + encoded)
PY
"""
    )


def _build_dokploy_odoo_backup_restore_web_quiesce_script(
    *,
    compose_app_name: str,
    operation_id: str,
    old_db_volume: str,
    data_volume: str,
    log_volume: str,
) -> str:
    header = "\n".join(
        (
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            f"compose_project={shlex.quote(compose_app_name)}",
            f"operation_id={shlex.quote(operation_id)}",
            f"old_db_volume={shlex.quote(old_db_volume)}",
            f"data_volume={shlex.quote(data_volume)}",
            f"log_volume={shlex.quote(log_volume)}",
            f"result_marker={shlex.quote(ODOO_BACKUP_RESTORE_RESULT_MARKER)}",
            "",
        )
    )
    return (
        header
        + r"""resolve_container_id() {
    local service_name="$1"
    docker ps -aq \
        --filter "label=com.docker.compose.project=${compose_project}" \
        --filter "label=com.docker.compose.service=${service_name}" | head -n 1
}

container_mount_source() {
    docker inspect "$1" | python3 -c '
import json
import sys
payload = json.load(sys.stdin)
matches = [mount.get("Name") or mount.get("Source") or "" for mount in payload[0].get("Mounts", []) if mount.get("Destination") == sys.argv[1]]
if len(matches) != 1 or not matches[0]:
    raise SystemExit(1)
print(matches[0])
' "$2"
}

database_container_id=$(resolve_container_id database)
web_container_id=$(resolve_container_id web)
if [ -z "${database_container_id}" ] || [ -z "${web_container_id}" ]; then
    echo "Odoo backup restore requires database and web containers." >&2
    exit 1
fi
if [ "$(container_mount_source "${database_container_id}" /var/lib/postgresql/data)" != "${old_db_volume}" ] || \
   [ "$(container_mount_source "${web_container_id}" /volumes/data)" != "${data_volume}" ] || \
   [ "$(container_mount_source "${web_container_id}" /volumes/logs)" != "${log_volume}" ]; then
    echo "Odoo backup restore live volume authority changed before quiesce." >&2
    exit 1
fi

web_was_running=false
if [ "$(docker inspect -f '{{.State.Status}}' "${web_container_id}")" = "running" ]; then
    web_was_running=true
    docker stop --time 60 "${web_container_id}" >/dev/null
fi
if [ "$(docker inspect -f '{{.State.Status}}' "${web_container_id}")" = "running" ]; then
    echo "Odoo web container did not quiesce." >&2
    exit 1
fi

python3 - "${result_marker}" "${operation_id}" "${web_was_running}" <<'PY'
import base64
import json
import sys
marker, operation_id, web_was_running = sys.argv[1:]
payload = {
    "schema_version": 1,
    "operation_id": operation_id,
    "phase": "web_quiesce",
    "evidence": {"web_was_running": web_was_running},
}
encoded = base64.b64encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).decode()
print(marker + "=" + encoded)
PY
"""
    )


def _build_dokploy_odoo_backup_restore_filestore_activate_script(
    *,
    compose_app_name: str,
    operation_id: str,
    database_name: str,
    filestore_path: str,
    filestore_staging_path: str,
    filestore_quarantine_path: str,
    data_volume: str,
    log_volume: str,
) -> str:
    header = "\n".join(
        (
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            f"compose_project={shlex.quote(compose_app_name)}",
            f"operation_id={shlex.quote(operation_id)}",
            f"database_name={shlex.quote(database_name)}",
            f"filestore_path={shlex.quote(filestore_path)}",
            f"filestore_staging_path={shlex.quote(filestore_staging_path)}",
            f"filestore_quarantine_path={shlex.quote(filestore_quarantine_path)}",
            f"data_volume={shlex.quote(data_volume)}",
            f"log_volume={shlex.quote(log_volume)}",
            f"result_marker={shlex.quote(ODOO_BACKUP_RESTORE_RESULT_MARKER)}",
            "",
        )
    )
    return (
        header
        + r"""resolve_container_id() {
    local service_name="$1"
    docker ps -aq \
        --filter "label=com.docker.compose.project=${compose_project}" \
        --filter "label=com.docker.compose.service=${service_name}" | head -n 1
}

container_mount_source() {
    docker inspect "$1" | python3 -c '
import json
import sys
payload = json.load(sys.stdin)
matches = [mount.get("Name") or mount.get("Source") or "" for mount in payload[0].get("Mounts", []) if mount.get("Destination") == sys.argv[1]]
if len(matches) != 1 or not matches[0]:
    raise SystemExit(1)
print(matches[0])
' "$2"
}

script_runner_container_id=$(resolve_container_id script-runner)
web_container_id=$(resolve_container_id web)
if [ -z "${script_runner_container_id}" ] || [ -z "${web_container_id}" ]; then
    echo "Odoo backup restore requires script-runner and web containers." >&2
    exit 1
fi
if [ "$(docker inspect -f '{{.State.Status}}' "${web_container_id}")" = "running" ]; then
    echo "Odoo web container must remain quiesced during filestore activation." >&2
    exit 1
fi
if [ "$(container_mount_source "${script_runner_container_id}" /volumes/data)" != "${data_volume}" ] || \
   [ "$(container_mount_source "${web_container_id}" /volumes/data)" != "${data_volume}" ] || \
   [ "$(container_mount_source "${web_container_id}" /volumes/logs)" != "${log_volume}" ]; then
    echo "Odoo backup restore data/log volume authority changed before activation." >&2
    exit 1
fi

docker exec -u root -i \
    --env "DATABASE_NAME=${database_name}" \
    --env "FILESTORE_PATH=${filestore_path}" \
    --env "FILESTORE_STAGING_PATH=${filestore_staging_path}" \
    --env "FILESTORE_QUARANTINE_PATH=${filestore_quarantine_path}" \
    --env "RESULT_MARKER=${result_marker}" \
    --env "OPERATION_ID=${operation_id}" \
    "${script_runner_container_id}" \
    python3 - <<'PY'
import base64
import json
import os
import sys
from pathlib import Path

database_name = os.environ["DATABASE_NAME"]
filestore_root = Path(os.environ["FILESTORE_PATH"])
staging_path = Path(os.environ["FILESTORE_STAGING_PATH"])
quarantine_path = Path(os.environ["FILESTORE_QUARANTINE_PATH"])
live_path = filestore_root if filestore_root.name == database_name else filestore_root / database_name
staged_path = staging_path / database_name

if staging_path.parent != live_path.parent or quarantine_path.parent != live_path.parent:
    raise SystemExit("Filestore activation paths are not siblings of the live filestore.")
if live_path.is_symlink() or staged_path.is_symlink():
    raise SystemExit("Live or staged filestore cannot be a symlink.")
if not live_path.is_dir() or not staged_path.is_dir():
    raise SystemExit("Live or staged filestore is unavailable.")
if quarantine_path.exists() or quarantine_path.is_symlink():
    raise SystemExit("Filestore quarantine path already exists.")

live_path.rename(quarantine_path)
try:
    staged_path.rename(live_path)
except Exception:
    quarantine_path.rename(live_path)
    raise SystemExit("Filestore activation failed; restored the prior live path.")
try:
    staging_path.rmdir()
except OSError:
    pass

payload = {
    "schema_version": 1,
    "operation_id": os.environ["OPERATION_ID"],
    "phase": "filestore_activate",
    "evidence": {
        "filestore_live_path": str(live_path),
        "filestore_quarantine_path": str(quarantine_path),
    },
}
encoded = base64.b64encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).decode()
print(os.environ["RESULT_MARKER"] + "=" + encoded)
PY
"""
    )


def _render_docker_exec_environment_lines(environment_values: Mapping[str, str]) -> str:
    lines: list[str] = []
    for key_name in sorted(environment_values):
        normalized_key = key_name.strip()
        if not normalized_key:
            raise click.ClickException(
                "Post-deploy workflow environment override keys must be non-empty."
            )
        if not normalized_key.replace("_", "A").isalnum() or normalized_key[0].isdigit():
            raise click.ClickException(
                f"Invalid post-deploy workflow environment override key: {normalized_key!r}."
            )
        value = str(environment_values[key_name])
        lines.append(f"workflow_environment+=(-e {shlex.quote(f'{normalized_key}={value}')})")
    return "\n".join(lines)


def _render_required_environment_key_lines(environment_keys: tuple[str, ...]) -> str:
    lines: list[str] = []
    for key_name in sorted(set(environment_keys)):
        normalized_key = key_name.strip()
        if not normalized_key:
            raise click.ClickException(
                "Required post-deploy workflow environment keys must be non-empty."
            )
        if not normalized_key.replace("_", "A").isalnum() or normalized_key[0].isdigit():
            raise click.ClickException(
                f"Invalid required post-deploy workflow environment key: {normalized_key!r}."
            )
        lines.append(f"required_workflow_environment_keys+=({shlex.quote(normalized_key)})")
    return "\n".join(lines)


def _render_bash_array_assignment_lines(array_name: str, values: tuple[str, ...]) -> str:
    lines: list[str] = []
    for raw_value in values:
        normalized_value = raw_value.strip()
        if not normalized_value:
            raise click.ClickException(f"{array_name} values must be non-empty.")
        lines.append(f"{array_name}+=({shlex.quote(normalized_value)})")
    return "\n".join(lines)


def _schedule_deployments(schedule: api.JsonObject | None) -> tuple[api.JsonObject, ...]:
    if not isinstance(schedule, dict):
        return ()
    raw_deployments = schedule.get("deployments")
    if not isinstance(raw_deployments, list):
        return ()
    return tuple(api._collect_object_items(raw_deployments))


def _has_running_schedule_deployment(schedule: api.JsonObject | None) -> bool:
    return any(
        api._deployment_status(deployment) in DOKPLOY_RUNNING_DEPLOYMENT_STATUSES
        for deployment in _schedule_deployments(schedule)
    )


def _should_clear_stale_data_workflow_lock(schedule: api.JsonObject | None) -> bool:
    deployments = _schedule_deployments(schedule)
    if not deployments or _has_running_schedule_deployment(schedule):
        return False
    for deployment in deployments:
        deployment_status_value = api._deployment_status(deployment)
        if deployment_status_value in DOKPLOY_CANCELLED_DEPLOYMENT_STATUSES:
            return True
        if deployment_status_value in DOKPLOY_SUCCESS_DEPLOYMENT_STATUSES:
            return False
    return False


def _apply_post_deploy_env_file_overrides(
    *, current_env_map: dict[str, str], env_file: Path | None
) -> dict[str, str]:
    if env_file is None:
        return dict(current_env_map)
    desired_env_map = dict(current_env_map)
    unsupported_keys: list[str] = []
    for env_key, env_value in api.parse_dokploy_env_text(
        env_file.read_text(encoding="utf-8")
    ).items():
        if env_key in POST_DEPLOY_UPDATE_IGNORED_ENV_KEYS:
            continue
        if env_key not in POST_DEPLOY_UPDATE_ALLOWED_ENV_KEYS:
            unsupported_keys.append(env_key)
            continue
        desired_env_map[env_key] = env_value
    if unsupported_keys:
        allowed_key_list = ", ".join(sorted(POST_DEPLOY_UPDATE_ALLOWED_ENV_KEYS))
        unsupported_key_list = ", ".join(sorted(unsupported_keys))
        raise click.ClickException(
            "Compose post-deploy env overlay only supports "
            f"{allowed_key_list}. Unsupported keys: {unsupported_key_list}."
        )
    return desired_env_map


def _resolve_upstream_restore_workflow_environment(
    *, desired_env_map: Mapping[str, str]
) -> dict[str, str]:
    upstream_environment: dict[str, str] = {}
    missing_keys: list[str] = []
    for env_key in ODOO_UPSTREAM_RESTORE_WORKFLOW_ENV_KEYS:
        value = str(desired_env_map.get(env_key) or "").strip()
        if not value:
            missing_keys.append(env_key)
            continue
        upstream_environment[env_key] = value
    if missing_keys:
        raise click.ClickException(
            "Compose post-deploy restore requires upstream Odoo source environment keys in the live target environment. "
            f"Missing keys: {', '.join(missing_keys)}."
        )
    return upstream_environment
