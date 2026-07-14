import shlex

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Literal

import click

from control_plane.dokploy import api
from control_plane.dokploy.source import (
    DEFAULT_DOKPLOY_DEPLOY_TIMEOUT_SECONDS,
    DokployTargetDefinition,
)


DOKPLOY_DATA_WORKFLOW_SCHEDULE_NAME = "platform-data-workflow"
DOKPLOY_ODOO_BOOTSTRAP_SCHEDULE_NAME = "platform-odoo-bootstrap"
DOKPLOY_ODOO_BACKUP_GATE_SCHEDULE_NAME = "platform-odoo-backup-gate"
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
    backup_record_id: str,
    database_name: str,
    filestore_path: str,
    backup_root: str,
    timeout_seconds: int | None = None,
) -> None:
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
    api.wait_for_dokploy_schedule_deployment(
        host=host,
        token=token,
        schedule_id=schedule_id,
        before_key=api.deployment_key(latest_schedule_deployment),
        timeout_seconds=schedule_timeout_seconds,
    )


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
    database_name: str,
    filestore_path: str,
    backup_root: str,
    backup_record_id: str,
) -> str:
    normalized_filestore_path = filestore_path.strip() or "/volumes/data/filestore"
    normalized_backup_root = backup_root.strip() or DEFAULT_ODOO_BACKUP_ROOT
    quoted_compose_app_name = shlex.quote(compose_app_name)
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
docker exec -u root \
    -e BACKUP_DIR="${{backup_dir}}" \
    "${{script_runner_container_id}}" \
    /bin/bash -lc 'install -d -m 700 "$BACKUP_DIR"'

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

docker exec \
    -e MANIFEST_PATH="${{manifest_path}}" \
    -e BACKUP_RECORD_ID="${{backup_record_id}}" \
    -e DATABASE_NAME="${{database_name}}" \
    -e BACKUP_DIR="${{backup_dir}}" \
    -e DATABASE_DUMP_PATH="${{database_dump_path}}" \
    -e FILESTORE_ARCHIVE_PATH="${{filestore_archive_path}}" \
    -e DATABASE_DUMP_SIZE="${{database_dump_size}}" \
    -e FILESTORE_ARCHIVE_SIZE="${{filestore_archive_size}}" \
    "${{script_runner_container_id}}" \
    python3 - <<'PY'
import json
import os
from datetime import datetime, timezone

payload = {{
    "backup_record_id": os.environ["BACKUP_RECORD_ID"],
    "database_name": os.environ["DATABASE_NAME"],
    "backup_dir": os.environ["BACKUP_DIR"],
    "database_dump_path": os.environ["DATABASE_DUMP_PATH"],
    "filestore_archive_path": os.environ["FILESTORE_ARCHIVE_PATH"],
    "database_dump_size": os.environ["DATABASE_DUMP_SIZE"],
    "filestore_archive_size": os.environ["FILESTORE_ARCHIVE_SIZE"],
    "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
}}
with open(os.environ["MANIFEST_PATH"], "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY

echo "Odoo backup gate complete: ${{backup_dir}}"
start_web_container
trap - EXIT
"""


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
