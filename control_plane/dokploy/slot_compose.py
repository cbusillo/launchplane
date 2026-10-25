"""An app-only candidate project, with web held behind its exact run receipt."""

import json
import shlex

from control_plane.contracts.odoo_cutover import OdooCutoverPlan
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.dokploy.slot_maintenance import slot_run_marker, slot_run_stamp


def held_slot_command(plan: OdooCutoverPlan, command: str) -> str:
    marker, stamp = slot_run_marker(plan), slot_run_stamp(plan)
    return (
        f'while [ "$(cat {shlex.quote(marker)} 2>/dev/null)" != "{stamp}" ]; do sleep 1; done; '
        f"exec {command}"
    )


def slot_runtime_identity(plan: OdooCutoverPlan, identity: RuntimeIdentity) -> dict[str, object]:
    if (
        identity.artifact_id != plan.candidate.artifact_id
        or identity.source_git_ref != plan.candidate.source_commit
    ):
        raise ValueError("runtime identity does not match the candidate source artifact")
    payload = identity.model_dump(mode="json")
    payload["source_revision"] = payload.pop("source_git_ref")
    payload.update(
        {
            "slot": plan.new.slot_id,
            "release_id": plan.release_id,
            "image_digest": plan.new.binding.image_digest,
        }
    )
    return payload


def render_odoo_app_slot_compose(
    plan: OdooCutoverPlan,
    *,
    data_volume: str,
    private_network: str,
) -> str:
    """No DB service, public port, Docker router, or old-project deployment.

    Lane records supply the pre-existing external DB/filestore network and
    volume. Their identities and managed env are never checked-in defaults.
    """
    if not data_volume or not private_network:
        raise ValueError("candidate requires explicit authoritative volume/network records")
    startup = "/venv/bin/python /volumes/scripts/run_odoo_startup.py -c /tmp/platform.odoo.conf"
    web = held_slot_command(plan, startup)
    scheduler = held_slot_command(
        plan,
        f"{startup} --scheduler --ready-file {shlex.quote(slot_run_marker(plan))} "
        f"--ready-token {slot_run_stamp(plan)}",
    )
    environment = {
        "ODOO_MAX_CRON_THREADS": "0",
        "ODOO_DB_NAME": plan.new.binding.database,
        "LAUNCHPLANE_RUNTIME_IDENTITY_JSON": json.dumps(
            slot_runtime_identity(plan, plan.new.runtime_identity), sort_keys=True
        ),
    }
    base = {
        "image": plan.new.image,
        "pull_policy": "always",
        "restart": "unless-stopped",
        "env_file": [".env"],
        "networks": ["private"],
        "volumes": ["data:/volumes/data"],
        "labels": {"traefik.enable": "false"},
        "healthcheck": {"disable": True},
        "environment": environment,
        "entrypoint": ["/bin/sh", "-c"],
    }
    return json.dumps(
        {
            "services": {
                "web": {**base, "command": web.replace("$", "$$")},
                "scheduler": {
                    **base,
                    "environment": {**environment, "ODOO_MAX_CRON_THREADS": "1"},
                    "command": scheduler.replace("$", "$$"),
                },
                "script-runner": {**base, "command": "exec sleep infinity"},
            },
            "volumes": {"data": {"external": True, "name": data_volume}},
            "networks": {"private": {"external": True, "name": private_network}},
        },
        sort_keys=True,
    )
