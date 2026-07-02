from __future__ import annotations

import click

from control_plane import dokploy as control_plane_dokploy
from control_plane.contracts.deployment_record import ResolvedTargetEvidence
from control_plane.contracts.runtime_identity import RuntimeIdentity, runtime_identity_env
from control_plane.contracts.ship_request import ShipRequest


def update_dokploy_target_artifact(
    *,
    host: str,
    token: str,
    target_type: str,
    target_id: str,
    artifact_id: str,
    runtime_identity: RuntimeIdentity | None = None,
) -> None:
    target_payload = control_plane_dokploy.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type=target_type,
        target_id=target_id,
    )
    if target_type == "application":
        if runtime_identity is not None:
            env_text = control_plane_dokploy.render_dokploy_env_text_with_overrides(
                str(target_payload.get("env") or ""),
                updates=runtime_identity_env(runtime_identity),
            )
            control_plane_dokploy.update_dokploy_target_env(
                host=host,
                token=token,
                target_type=target_type,
                target_id=target_id,
                target_payload=target_payload,
                env_text=env_text,
            )
        control_plane_dokploy.dokploy_request(
            host=host,
            token=token,
            path="/api/application.saveDockerProvider",
            method="POST",
            payload={
                "applicationId": target_id,
                "dockerImage": artifact_id,
                "username": target_payload.get("username"),
                "password": target_payload.get("password"),
                "registryUrl": target_payload.get("registryUrl"),
            },
        )
        return

    if target_type == "compose":
        env_text = control_plane_dokploy.render_dokploy_env_text_with_overrides(
            str(target_payload.get("env") or ""),
            updates={
                "DOCKER_IMAGE_REFERENCE": artifact_id,
                **(runtime_identity_env(runtime_identity) if runtime_identity is not None else {}),
            },
        )
        control_plane_dokploy.update_dokploy_target_env(
            host=host,
            token=token,
            target_type=target_type,
            target_id=target_id,
            target_payload=target_payload,
            env_text=env_text,
        )
        return

    raise click.ClickException(f"Unsupported Dokploy target type: {target_type}")


def execute_dokploy_artifact_deploy(
    *,
    host: str,
    token: str,
    ship_request: ShipRequest,
    resolved_target: ResolvedTargetEvidence,
    deploy_timeout_seconds: int,
    runtime_identity: RuntimeIdentity | None = None,
) -> None:
    latest_before = control_plane_dokploy.latest_deployment_for_target(
        host=host,
        token=token,
        target_type=resolved_target.target_type,
        target_id=resolved_target.target_id,
    )
    update_dokploy_target_artifact(
        host=host,
        token=token,
        target_type=resolved_target.target_type,
        target_id=resolved_target.target_id,
        artifact_id=ship_request.artifact_id,
        runtime_identity=runtime_identity,
    )
    control_plane_dokploy.trigger_deployment(
        host=host,
        token=token,
        target_type=resolved_target.target_type,
        target_id=resolved_target.target_id,
        no_cache=ship_request.no_cache,
    )
    control_plane_dokploy.wait_for_target_deployment(
        host=host,
        token=token,
        target_type=resolved_target.target_type,
        target_id=resolved_target.target_id,
        before_key=control_plane_dokploy.deployment_key(latest_before),
        timeout_seconds=deploy_timeout_seconds,
    )
