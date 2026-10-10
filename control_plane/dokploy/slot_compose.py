"""An app-only candidate project, with web held behind its exact run receipt."""

import hashlib
import json

from control_plane.contracts.odoo_cutover import OdooCutoverPlan
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.dokploy.slot_maintenance import maintenance_payload, payload_digest


def slot_runtime_identity(plan: OdooCutoverPlan, identity: RuntimeIdentity) -> dict[str, object]:
    if (
        identity.artifact_id != plan.candidate.artifact_id
        or identity.source_git_ref != plan.candidate.source_commit
    ):
        raise ValueError("runtime identity does not match the candidate source artifact")
    payload = identity.model_dump(mode="json")
    payload.update(
        {
            "slot": plan.new.slot_id,
            "release_id": plan.release_id,
            "image_digest": plan.new.binding.image_digest,
            "source_revision": identity.source_git_ref,
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
    digest = payload_digest(maintenance_payload(plan))
    marker = f"/volumes/data/.launchplane-cutovers/{digest}/web-ready"
    expected = hashlib.sha256(plan.new.model_dump_json().encode()).hexdigest()
    web = (
        f'while [ "$(cat {marker} 2>/dev/null)" != "{expected}" ]; do sleep 1; done; '
        "exec /venv/bin/python /volumes/scripts/run_odoo_startup.py -c /tmp/platform.odoo.conf"
    )
    base = {
        "image": plan.new.image,
        "pull_policy": "always",
        "restart": "unless-stopped",
        "env_file": [".env"],
        "networks": ["private"],
        "volumes": ["data:/volumes/data"],
        "labels": {"traefik.enable": "false"},
        "environment": {"ODOO_MAX_CRON_THREADS": "0", "ODOO_DB_NAME": plan.new.binding.database},
        "entrypoint": ["/bin/sh", "-c"],
    }
    return json.dumps(
        {
            "services": {
                "web": {**base, "command": web.replace("$", "$$")},
                "script-runner": {**base, "command": "exec sleep infinity"},
            },
            "volumes": {"data": {"external": True, "name": data_volume}},
            "networks": {"private": {"external": True, "name": private_network}},
        },
        sort_keys=True,
    )
