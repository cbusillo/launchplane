"""Prepare only the candidate's Dokploy Compose project, with its web held."""

from control_plane.contracts.odoo_cutover import OdooCutoverPlan
from control_plane.dokploy import api
from control_plane.dokploy.slot_compose import render_odoo_app_slot_compose
from control_plane.dokploy.slot_maintenance import maintenance_payload, payload_digest


def prepare_dokploy_odoo_slot(
    plan: OdooCutoverPlan,
    *,
    host: str,
    token: str,
    data_volume: str,
    private_network: str,
    timeout_seconds: int = 600,
) -> None:
    old = api.fetch_dokploy_target_payload(
        host=host, token=token, target_type="compose", target_id=plan.old.compose_id
    )
    candidate = api.fetch_dokploy_target_payload(
        host=host, token=token, target_type="compose", target_id=plan.new.compose_id
    )
    if (
        not old.get("appName")
        or not candidate.get("appName")
        or old["appName"] == candidate["appName"]
        or candidate.get("domains")
    ):
        raise ValueError("candidate must be a separate private app project without public domains")
    # Compatible maintenance may reapply managed settings to the shared DB.
    # Compare their authoritative values in memory; never log or copy them.
    if plan.compatibility.classification == "compatible":
        old_env = api.parse_dokploy_env_text(str(old.get("env") or ""))
        candidate_env = api.parse_dokploy_env_text(str(candidate.get("env") or ""))
        for environment in (old_env, candidate_env):
            environment.pop("LAUNCHPLANE_RUNTIME_IDENTITY_JSON", None)
        if old_env != candidate_env:
            raise ValueError("compatible slots require identical managed runtime settings")
    title = "launchplane-cutover-" + payload_digest(maintenance_payload(plan))
    rendered = render_odoo_app_slot_compose(
        plan, data_volume=data_volume, private_network=private_network
    )
    previous = api.deployment_for_target_by_title(
        host=host, token=token, target_type="compose", target_id=plan.new.compose_id, title=title
    )
    if previous is not None:
        if candidate.get("composeFile") != rendered or candidate.get("sourceType") != "raw":
            raise ValueError("recorded candidate deployment differs from its immutable definition")
        if api.deployment_status(previous) in {"error", "failed", "cancelled", "canceled"}:
            raise ValueError(
                "candidate preparation failed; recover its recorded deployment before retry"
            )
    known = frozenset(
        api.deployment_key(item)
        for item in api.list_deployments_for_target(
            host=host,
            token=token,
            target_type="compose",
            target_id=plan.new.compose_id,
        )
    )
    if previous is None:
        api.dokploy_request(
            host=host,
            token=token,
            path="/api/compose.update",
            method="POST",
            payload={
                "composeId": plan.new.compose_id,
                "composeFile": rendered,
                "sourceType": "raw",
                "composeType": "docker-compose",
            },
        )
        applied = api.fetch_dokploy_target_payload(
            host=host, token=token, target_type="compose", target_id=plan.new.compose_id
        )
        if applied.get("composeFile") != rendered or applied.get("sourceType") != "raw":
            raise ValueError("private candidate Compose definition was not read back")
        api.trigger_deployment(
            host=host,
            token=token,
            target_type="compose",
            target_id=plan.new.compose_id,
            no_cache=False,
            title=title,
        )
    api.wait_for_titled_or_sole_new_target_deployment(
        host=host,
        token=token,
        target_type="compose",
        target_id=plan.new.compose_id,
        deployment_title=title,
        known_deployment_keys=known,
        timeout_seconds=timeout_seconds,
    )
    observed_old = api.fetch_dokploy_target_payload(
        host=host, token=token, target_type="compose", target_id=plan.old.compose_id
    )
    if observed_old != old:
        raise ValueError("serving project changed during private candidate preparation")
