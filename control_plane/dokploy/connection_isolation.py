"""Bounded repair/readback on the existing Odoo data-workflow schedule."""

import shlex
import hashlib

from control_plane.contracts.canonical_json import canonical_json_sha256

import click

from control_plane.dokploy import api, post_deploy
from control_plane.dokploy.source import DokployTargetDefinition
from control_plane.integration_readback import (
    INTEGRATION_READBACK_PASSED_PATH,
    INTEGRATION_READBACK_PROGRAM,
    integration_readback_policy,
    web_held_until_integration_readback,
)
from control_plane.odoo_connection_isolation import (
    CONNECTION_ISOLATION_PROGRAM,
    OdooConnectionIsolationMode,
)


def isolation_script(
    *,
    compose_app_name: str,
    database_name: str,
    instance: str,
    mode: OdooConnectionIsolationMode,
    readback_spec: str,
) -> str:
    if not web_held_until_integration_readback(instance):
        raise ValueError("Connection isolation refuses production.")
    return f"""#!/usr/bin/env bash
set -euo pipefail
project={shlex.quote(compose_app_name)}
database={shlex.quote(database_name)}
mode={shlex.quote(mode)}
resolve() {{
    local ids
    ids=$(docker ps -aq --filter "label=com.docker.compose.project=$project" \\
        --filter "label=com.docker.compose.service=$1" \\
        --filter "label=com.docker.compose.oneoff=False")
    [ -n "$ids" ] && [ "$(printf '%s\\n' "$ids" | wc -l | tr -d ' ')" = 1 ] || exit 1
    printf '%s' "$ids"
}}
runner=$(resolve script-runner)
if [ "$mode" = apply ]; then
    web=$(resolve web)
    [ "$(docker inspect -f '{{{{.Image}}}}' "$web")" = "$(docker inspect -f '{{{{.Image}}}}' "$runner")" ] || exit 1
    docker stop "$web" >/dev/null
    docker exec -u root "$runner" rm -f {shlex.quote(INTEGRATION_READBACK_PASSED_PATH)}
fi
docker exec -i "$runner" python3 - "$database" {shlex.quote(instance)} "$mode" <<'PY'
{CONNECTION_ISOLATION_PROGRAM}PY
docker exec -i "$runner" python3 - "$database" {shlex.quote(readback_spec)} "$mode" <<'PY'
{INTEGRATION_READBACK_PROGRAM}PY
if [ "$mode" = apply ]; then
    # Release only the actual current database and payload after full readback.
    payload=$(docker exec "$runner" sh -c 'printf %s "${{ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64-}}"')
    digest=$(printf '%s\\n%s' "$database" "$payload" | sha256sum | cut -d ' ' -f 1)
    docker exec -u root "$runner" sh -c 'printf %s "$1" > "$2"' _ \\
        "$digest" {shlex.quote(INTEGRATION_READBACK_PASSED_PATH)}
    docker start "$web" >/dev/null
    [ "$(docker inspect -f '{{{{.State.Status}}}}' "$web")" = running ]
fi
"""


def run_connection_isolation(
    *,
    host: str,
    token: str,
    target_definition: DokployTargetDefinition,
    mode: OdooConnectionIsolationMode,
    reviewed_plan_sha256: str = "",
) -> dict[str, str]:
    if not web_held_until_integration_readback(target_definition.instance):
        raise ValueError("Connection isolation refuses production.")
    payload = api.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type="compose",
        target_id=target_definition.target_id,
    )
    environment = api.parse_dokploy_env_text(str(payload.get("env") or ""))
    database = environment.get("ODOO_DB_NAME", "").strip()
    if not database:
        raise click.ClickException("Connection isolation requires a tracked database name.")
    plan_digest = canonical_json_sha256(
        {
            "context": target_definition.context,
            "instance": target_definition.instance,
            "target_id": target_definition.target_id,
            "database": database,
            "app_name": payload.get("appName"),
            "runtime_identity": environment.get("LAUNCHPLANE_RUNTIME_IDENTITY_JSON", ""),
            "repair_program": hashlib.sha256(CONNECTION_ISOLATION_PROGRAM.encode()).hexdigest(),
        }
    )
    if mode == "apply" and reviewed_plan_sha256 != plan_digest:
        raise click.ClickException("Connection isolation plan changed; run a new dry-run.")
    schedule_type, lookup_id, compose_app_name, server_id = (
        post_deploy._resolve_dokploy_schedule_runtime(
            host=host,
            token=token,
            compose_id=target_definition.target_id,
            compose_name=target_definition.target_name,
            target_payload=payload,
        )
    )
    app_name = post_deploy._build_dokploy_data_workflow_schedule_app_name(
        context_name=target_definition.context,
        instance_name=target_definition.instance,
    )
    name = post_deploy.DOKPLOY_DATA_WORKFLOW_SCHEDULE_NAME
    existing = api.find_matching_dokploy_schedule(
        host=host,
        token=token,
        target_id=lookup_id,
        schedule_type=schedule_type,
        schedule_name=name,
        app_name=app_name,
    )
    if post_deploy._has_running_schedule_deployment(existing):
        raise click.ClickException("Another data workflow is active on this lane.")
    policy = integration_readback_policy(
        instance_name=target_definition.instance,
        policies=target_definition.policies,
        workflow_mode="maintenance",
    )
    schedule_payload: api.JsonObject = {
        "name": name,
        "cronExpression": post_deploy.DOKPLOY_MANUAL_ONLY_CRON_EXPRESSION,
        "appName": app_name,
        "shellType": "bash",
        "scheduleType": schedule_type,
        "command": "control-plane Odoo copy connection isolation",
        "script": isolation_script(
            compose_app_name=compose_app_name,
            database_name=database,
            instance=target_definition.instance,
            mode=mode,
            readback_spec=policy.encoded_spec(),
        ),
        "serverId": server_id,
        "userId": lookup_id if schedule_type == "dokploy-server" else None,
        "enabled": False,
        "timezone": "UTC",
    }
    evidence = post_deploy.run_manual_data_schedule(
        host=host,
        token=token,
        target_definition=target_definition,
        schedule_lookup_id=lookup_id,
        schedule_type=schedule_type,
        schedule_name=name,
        schedule_app_name=app_name,
        schedule_payload=schedule_payload,
        resolved_schedule_execution_timeout_seconds=target_definition.deploy_timeout_seconds or 300,
    )
    evidence["connection_isolation_plan_sha256"] = plan_digest
    if mode != "dry-run":
        if evidence.get("connection_isolation_ok") != "true":
            raise post_deploy.OdooPostDeployReadbackFailure(
                "Connection isolation did not prove success.",
                evidence=evidence,
            )
        post_deploy.require_integration_readback_evidence(evidence, policy)
    elif evidence.get("connection_isolation_ok") not in {"true", "false"}:
        raise post_deploy.OdooPostDeployReadbackFailure(
            "Connection isolation dry-run returned no evidence.",
            evidence=evidence,
        )
    return evidence
