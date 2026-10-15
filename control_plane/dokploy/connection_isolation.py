"""Bounded repair/readback on dedicated, mutually excluded testing schedules."""

import shlex
import hashlib
from dataclasses import dataclass
from collections.abc import Callable
from typing import Literal

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.runtime_identity import parse_runtime_identity_payload

import click

from control_plane.dokploy import api, post_deploy
from control_plane.dokploy.source import DokployTargetDefinition
from control_plane.integration_readback import (
    INTEGRATION_READBACK_PASSED_PATH,
    INTEGRATION_READBACK_PROGRAM,
    IntegrationReadbackPolicy,
    integration_readback_policy,
    web_held_until_integration_readback,
)
from control_plane.odoo_connection_isolation import (
    CONNECTION_ISOLATION_PROGRAM,
    ConnectionIsolationPreconditionFailed,
    OdooConnectionIsolationMode,
)
from control_plane.connection_isolation_qualification import (
    RESOURCE_QUALIFICATION_PROGRAM,
    resource_inventory_script,
)


CONNECTION_ISOLATION_SCHEDULE_NAME = "platform-odoo-connection-isolation"


def isolation_schedule_app_name(context: str, instance: str, provider_operation_key: str) -> str:
    base = post_deploy._build_dokploy_data_workflow_schedule_app_name(
        context_name=context,
        instance_name=instance,
    )
    suffix = (
        hashlib.sha256(provider_operation_key.encode()).hexdigest()
        if provider_operation_key
        else "observe"
    )
    return f"{base}-isolation-{suffix}"


def isolation_script(
    *,
    compose_app_name: str,
    database_name: str,
    instance: str,
    mode: OdooConnectionIsolationMode,
    readback_spec: str,
    context: str = "",
    runtime_identity: str = "",
    reviewed_resource_sha256: str = "",
    operation_digest: str = "",
) -> str:
    if not web_held_until_integration_readback(instance):
        raise ValueError("Connection isolation refuses production.")
    return f"""#!/usr/bin/env bash
set -euo pipefail
project={shlex.quote(compose_app_name)}
database={shlex.quote(database_name)}
mode={shlex.quote(mode)}
context={shlex.quote(context)}
runtime_identity={shlex.quote(runtime_identity)}
reviewed_resources={shlex.quote(reviewed_resource_sha256)}
operation_digest={shlex.quote(operation_digest)}
if [ -n "$operation_digest" ]; then
    printf 'connection_isolation_operation_sha256=%s\n' "$operation_digest"
fi
if [ "$mode" = apply ]; then
    [[ "$reviewed_resources" =~ ^[a-f0-9]{{64}}$ ]] || exit 1
fi
{post_deploy.odoo_testing_schedule_lock_script(compose_app_name)}
resolve() {{
    local ids
    ids=$(docker ps -aq --filter "label=com.docker.compose.project=$project" \\
        --filter "label=com.docker.compose.service=$1" \\
        --filter "label=com.docker.compose.oneoff=False")
    [ -n "$ids" ] && [ "$(printf '%s\\n' "$ids" | wc -l | tr -d ' ')" = 1 ] || exit 1
    printf '%s' "$ids"
}}
runner=$(resolve script-runner)
web=$(resolve web)
{post_deploy.odoo_testing_idle_runner_script("runner")}
{resource_inventory_script(program=RESOURCE_QUALIFICATION_PROGRAM)}
if [ "$mode" = apply ]; then
    [ "$(docker inspect -f '{{{{.Image}}}}' "$web")" = "$(docker inspect -f '{{{{.Image}}}}' "$runner")" ] || exit 1
    docker stop "$web" >/dev/null
    docker exec -u root "$runner" rm -f {shlex.quote(INTEGRATION_READBACK_PASSED_PATH)}
fi
isolation_status=0
docker exec -e ODOO_DB_HOST="$database_address" -i "$runner" python3 - "$database" {shlex.quote(instance)} "$mode" <<'PY' || isolation_status=$?
{CONNECTION_ISOLATION_PROGRAM}PY
readback_status=0
docker exec -e ODOO_DB_HOST="$database_address" -i "$runner" python3 - "$database" {shlex.quote(readback_spec)} "$mode" <<'PY' || readback_status=$?
{INTEGRATION_READBACK_PROGRAM}PY
[ "$isolation_status" -eq 0 ] && [ "$readback_status" -eq 0 ] || exit 1
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
    before_mutation: Callable[[str], None] | None = None,
    reviewed_resource_sha256: str = "",
    provider_operation_key: str = "",
    expected_product: str = "",
) -> dict[str, str]:
    if not web_held_until_integration_readback(target_definition.instance):
        raise ValueError("Connection isolation refuses production.")
    if mode == "apply" and not provider_operation_key:
        raise click.ClickException("Connection isolation apply requires its service operation key.")
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
    runtime = parse_runtime_identity_payload(
        environment.get("LAUNCHPLANE_RUNTIME_IDENTITY_JSON", "")
    )
    if (
        runtime is None
        or (runtime.context, runtime.instance) != (target_definition.context, "testing")
        or (expected_product and runtime.product != expected_product)
    ):
        raise ConnectionIsolationPreconditionFailed("runtime_unverified")
    policy = integration_readback_policy(
        instance_name=target_definition.instance,
        policies=target_definition.policies,
        workflow_mode="maintenance",
    )
    plan_digest = connection_isolation_plan_sha256(target_definition, payload, environment, policy)
    if mode == "apply" and reviewed_plan_sha256 != plan_digest:
        raise ConnectionIsolationPreconditionFailed("plan_changed")
    schedule_type, lookup_id, compose_app_name, server_id = (
        post_deploy._resolve_dokploy_schedule_runtime(
            host=host,
            token=token,
            compose_id=target_definition.target_id,
            compose_name=target_definition.target_name,
            target_payload=payload,
        )
    )
    # A job admitted before this service revision may predate the execution lock.
    shared_workflow = api.find_matching_dokploy_schedule(
        host=host,
        token=token,
        target_id=lookup_id,
        schedule_type=schedule_type,
        schedule_name=post_deploy.DOKPLOY_DATA_WORKFLOW_SCHEDULE_NAME,
        app_name=post_deploy._build_dokploy_data_workflow_schedule_app_name(
            context_name=target_definition.context,
            instance_name=target_definition.instance,
        ),
    )
    if post_deploy._has_running_schedule_deployment(shared_workflow):
        raise ConnectionIsolationPreconditionFailed("workflow_active")
    app_name = isolation_schedule_app_name(
        target_definition.context,
        target_definition.instance,
        provider_operation_key if mode == "apply" else "",
    )
    name = CONNECTION_ISOLATION_SCHEDULE_NAME
    existing = api.find_matching_dokploy_schedule(
        host=host,
        token=token,
        target_id=lookup_id,
        schedule_type=schedule_type,
        schedule_name=name,
        app_name=app_name,
    )
    if post_deploy._has_running_schedule_deployment(existing):
        raise ConnectionIsolationPreconditionFailed("workflow_active")
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
            context=target_definition.context,
            runtime_identity=environment.get("LAUNCHPLANE_RUNTIME_IDENTITY_JSON", ""),
            reviewed_resource_sha256=reviewed_resource_sha256 if mode == "apply" else "",
            operation_digest=hashlib.sha256(provider_operation_key.encode()).hexdigest()
            if provider_operation_key
            else "",
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
        resolved_schedule_execution_timeout_seconds=300,
        before_provider_mutation=before_mutation if mode == "apply" else None,
    )
    evidence["connection_isolation_plan_sha256"] = plan_digest
    if evidence.get("connection_isolation_resources_ok") != "true":
        raise post_deploy.OdooPostDeployReadbackFailure(
            "Testing resources were not qualified.", evidence=evidence
        )
    if mode != "dry-run":
        if evidence.get("connection_isolation_ok") != "true":
            raise post_deploy.OdooPostDeployReadbackFailure(
                "Connection isolation did not prove success.", evidence=evidence
            )
        post_deploy.require_integration_readback_evidence(evidence, policy)
    elif evidence.get("connection_isolation_ok") not in {"true", "false"}:
        raise post_deploy.OdooPostDeployReadbackFailure(
            "Connection isolation dry-run returned no evidence.", evidence=evidence
        )
    return evidence


def connection_isolation_plan_sha256(
    target_definition: DokployTargetDefinition,
    payload: api.JsonObject,
    environment: dict[str, str],
    policy: IntegrationReadbackPolicy,
) -> str:
    return canonical_json_sha256(
        {
            "context": target_definition.context,
            "instance": target_definition.instance,
            "target_id": target_definition.target_id,
            "database": environment.get("ODOO_DB_NAME", "").strip(),
            "app_name": payload.get("appName"),
            "runtime_identity": environment.get("LAUNCHPLANE_RUNTIME_IDENTITY_JSON", ""),
            "execution_program": hashlib.sha256(
                isolation_script(
                    compose_app_name=str(payload.get("appName") or ""),
                    database_name=environment.get("ODOO_DB_NAME", "").strip(),
                    instance=target_definition.instance,
                    mode="apply",
                    readback_spec=policy.encoded_spec(),
                    context=target_definition.context,
                    runtime_identity=environment.get("LAUNCHPLANE_RUNTIME_IDENTITY_JSON", ""),
                ).encode()
            ).hexdigest(),
            "readback_spec": policy.encoded_spec(),
        }
    )


def reviewed_isolation_script_matches(
    script: object,
    *,
    target: DokployTargetDefinition,
    operation_digest: str,
    reviewed_plan_sha256: str,
    reviewed_resource_sha256: str,
) -> bool:
    """Verify the original provider artifact, independent of later lane/source changes."""
    if target.instance != "testing" or not isinstance(script, str) or len(script) > 1024 * 1024:
        return False
    fields: dict[str, str] = {}
    names = {
        "project",
        "database",
        "mode",
        "context",
        "runtime_identity",
        "reviewed_resources",
        "operation_digest",
    }
    for line in script.split("resolve() {", 1)[0].splitlines():
        name = line.partition("=")[0]
        if name not in names:
            continue
        try:
            words = shlex.split(line)
        except ValueError:
            return False
        if name in fields or len(words) != 1:
            return False
        fields[name] = words[0].split("=", 1)[1]
    if (
        set(fields) != names
        or fields["mode"] != "apply"
        or fields["context"] != target.context
        or not fields["database"]
        or fields["operation_digest"] != operation_digest
        or fields["reviewed_resources"] != reviewed_resource_sha256
    ):
        return False
    runtime = parse_runtime_identity_payload(fields["runtime_identity"])
    if runtime is None or (runtime.context, runtime.instance) != (target.context, "testing"):
        return False
    canonical_script = script
    for name in ("reviewed_resources", "operation_digest"):
        assignment = f"{name}={shlex.quote(fields[name])}\n"
        if canonical_script.count(assignment) != 1:
            return False
        canonical_script = canonical_script.replace(assignment, f"{name}=''\n", 1)
    specifications = []
    for line in script.splitlines():
        if line.startswith("docker exec ") and line.endswith("|| readback_status=$?"):
            try:
                words = shlex.split(line)
                index = words.index("python3")
            except ValueError:
                return False
            if len(words) <= index + 3 or words[index + 1 : index + 3] != ["-", "$database"]:
                return False
            specifications.append(words[index + 3])
    if len(specifications) != 1:
        return False
    return reviewed_plan_sha256 == canonical_json_sha256(
        {
            "context": target.context,
            "instance": "testing",
            "target_id": target.target_id,
            "database": fields["database"],
            "app_name": fields["project"],
            "runtime_identity": fields["runtime_identity"],
            "execution_program": hashlib.sha256(canonical_script.encode()).hexdigest(),
            "readback_spec": specifications[0],
        }
    )


@dataclass(frozen=True)
class ConnectionIsolationObservation:
    outcome: Literal["present", "absent", "unknown"]
    evidence: dict[str, str]
    passed: bool = False
    retry_safe: bool = False


def observe_connection_isolation_operation(
    *,
    host: str,
    token: str,
    target_definition: DokployTargetDefinition,
    provider_operation_key: str,
    provider_effect_phase: str,
    reviewed_plan_sha256: str = "",
    reviewed_resource_sha256: str = "",
) -> ConnectionIsolationObservation:
    """Settle only a terminal deployment bearing this exact operation's marker."""
    unknown = ConnectionIsolationObservation("unknown", {})
    if target_definition.instance != "testing":
        return unknown
    try:
        payload = api.fetch_dokploy_target_payload(
            host=host,
            token=token,
            target_type="compose",
            target_id=target_definition.target_id,
        )
        schedule_type, lookup_id, _, _ = post_deploy._resolve_dokploy_schedule_runtime(
            host=host,
            token=token,
            compose_id=target_definition.target_id,
            compose_name=target_definition.target_name,
            target_payload=payload,
        )
        schedule = api.find_matching_dokploy_schedule(
            host=host,
            token=token,
            target_id=lookup_id,
            schedule_type=schedule_type,
            schedule_name=CONNECTION_ISOLATION_SCHEDULE_NAME,
            app_name=isolation_schedule_app_name(
                target_definition.context,
                target_definition.instance,
                provider_operation_key,
            ),
        )
        schedule_id = api.schedule_key(schedule) if schedule else ""
        if not schedule_id:
            # An inactive schedule upsert cannot run the target. A trigger-phase
            # absence never proves that an uncertain queued request cannot run.
            return (
                ConnectionIsolationObservation(
                    "present", {"connection_isolation_triggered": "false"}
                )
                if provider_effect_phase == post_deploy.ODOO_POST_DEPLOY_SCHEDULE_UPSERT
                else unknown
            )
        deployments = api.extract_deployments(
            api.dokploy_request(
                host=host,
                token=token,
                path="/api/deployment.allByType",
                query={"id": schedule_id, "type": "schedule"},
            )
        )
        if any(
            api._deployment_status(item) in post_deploy.DOKPLOY_RUNNING_DEPLOYMENT_STATUSES
            for item in deployments
        ):
            return unknown
        expected = hashlib.sha256(provider_operation_key.encode()).hexdigest()
        matching: dict[str, tuple[api.JsonObject, dict[str, str]]] = {}
        logs_fetched = False
        for deployment in deployments:
            deployment_id = api.deployment_key(deployment)
            if not deployment_id:
                continue
            evidence = post_deploy._read_odoo_post_deploy_log_markers(
                host=host,
                token=token,
                schedule_id=schedule_id,
                schedule_deployment_key=deployment_id,
                deployment_id=deployment_id,
                deployment=deployment,
            )
            logs_fetched = evidence.get("log_available") == "true"
            if evidence.get("connection_isolation_operation_sha256") == expected:
                matching[deployment_id] = deployment, evidence
        if len(matching) == 1:
            deployment, evidence = next(iter(matching.values()))
            status = api._deployment_status(deployment)
            success = status in post_deploy.DOKPLOY_SUCCESS_DEPLOYMENT_STATUSES
            # Interrupted transports may leave container execs behind. A normal
            # error settles only after the readback program closed its SQL connection.
            terminal = success or (
                status in {"failed", "error"}
                and evidence.get("connection_isolation_resources_ok") == "true"
                and all(
                    evidence.get(key) in {"true", "false"}
                    for key in (
                        "connection_isolation_ok",
                        "integration_readback_ok",
                    )
                )
            )
            if terminal:
                passed = success and all(
                    evidence.get(key) == "true"
                    for key in (
                        "connection_isolation_ok",
                        "connection_isolation_resources_ok",
                        "integration_readback_ok",
                    )
                )
                return ConnectionIsolationObservation("present", evidence, passed=passed)
        if (
            not deployments
            and provider_effect_phase == post_deploy.ODOO_POST_DEPLOY_SCHEDULE_UPSERT
        ):
            return ConnectionIsolationObservation(
                "present", {"connection_isolation_triggered": "false"}
            )
        if (
            not matching
            and len(deployments) == 1
            and schedule is not None
            and schedule.get("enabled") is False
            and logs_fetched
            and provider_effect_phase == post_deploy.ODOO_POST_DEPLOY_SCHEDULE_TRIGGER
            and api._deployment_status(deployments[0])
            in post_deploy.DOKPLOY_SUCCESS_DEPLOYMENT_STATUSES
        ):
            if reviewed_isolation_script_matches(
                schedule.get("script"),
                target=target_definition,
                operation_digest=expected,
                reviewed_plan_sha256=reviewed_plan_sha256,
                reviewed_resource_sha256=reviewed_resource_sha256,
            ):
                # Positive normal completion and the original reviewed artifact
                # permit failure settlement, never success without log evidence.
                return ConnectionIsolationObservation(
                    "present",
                    {"connection_isolation_terminal_unverified": "true"},
                )
    except (click.ClickException, KeyError, TypeError, ValueError):
        return unknown
    return unknown
