"""Run fixed allocation programs on the provider host, outside retained applications."""

from collections.abc import Callable
from pathlib import Path

from control_plane.dokploy import api, source, post_deploy
from control_plane.testing_separation_resources import (
    _find_storage_schedule,
    _require_terminal_storage_history,
)


def run_application_host_program(
    *,
    root: Path,
    retained_target_id: str,
    context: str,
    namespace: str,
    name: str,
    prepare_script: str,
    readback_script: str,
    before_effect: Callable[[str], None],
) -> dict[str, str]:
    """An existing uncertain program must finish before a separate readback can run."""
    host, token = source.read_dokploy_config(control_plane_root=root)
    placement = api.fetch_dokploy_target_payload(
        host=host, token=token, target_type="application", target_id=retained_target_id
    )
    if placement.get("applicationId") != retained_target_id:
        raise ValueError("Retained application placement changed.")
    kind, lookup, _, server = post_deploy._resolve_dokploy_schedule_runtime(
        host=host,
        token=token,
        compose_id=retained_target_id,
        compose_name=context,
        target_payload=placement,
    )
    original = _find_storage_schedule(host, token, lookup, kind, name, namespace)
    script = prepare_script
    if original is not None:
        if original.get("script") != prepare_script or original.get("enabled") is not False:
            raise ValueError("The original host program provenance changed.")
        _require_terminal_storage_history(host, token, api.schedule_key(original), original=True)
        name += " readback"
        script = readback_script
        previous = _find_storage_schedule(host, token, lookup, kind, name, namespace)
        if previous is not None:
            if previous.get("script") != script or previous.get("enabled") is not False:
                raise ValueError("The host readback program provenance changed.")
            _require_terminal_storage_history(
                host, token, api.schedule_key(previous), original=False
            )
    return post_deploy.run_manual_data_schedule(
        host=host,
        token=token,
        target_definition=source.DokployTargetDefinition(
            context=context,
            instance="testing",
            target_type="application",
            target_id=retained_target_id,
        ),
        schedule_lookup_id=lookup,
        schedule_type=kind,
        schedule_name=name,
        schedule_app_name=namespace,
        schedule_payload={
            "name": name,
            "appName": namespace,
            "shellType": "bash",
            "scheduleType": kind,
            "script": script,
            "command": "control-plane independent application resource proof",
            "cronExpression": post_deploy.DOKPLOY_MANUAL_ONLY_CRON_EXPRESSION,
            "serverId": server,
            "userId": lookup if kind == "dokploy-server" else None,
            "enabled": False,
            "timezone": "UTC",
        },
        resolved_schedule_execution_timeout_seconds=300,
        before_provider_mutation=before_effect,
    )
