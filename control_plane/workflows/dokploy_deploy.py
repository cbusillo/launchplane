from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlsplit

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
    before_provider_mutation: Callable[[str], None] | None = None,
    effect_started: Callable[[], None] | None = None,
) -> None:
    target_payload = control_plane_dokploy.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type=target_type,
        target_id=target_id,
    )
    if target_type == "application":
        _prepare_saved_registry_login(
            host=host,
            token=token,
            artifact_id=artifact_id,
            target_payload=target_payload,
        )
        if runtime_identity is not None:
            env_text = control_plane_dokploy.render_dokploy_env_text_with_overrides(
                str(target_payload.get("env") or ""),
                updates=runtime_identity_env(runtime_identity),
            )
            if before_provider_mutation is not None:
                before_provider_mutation("target_update")
            if effect_started is not None:
                effect_started()
            control_plane_dokploy.update_dokploy_target_env(
                host=host,
                token=token,
                target_type=target_type,
                target_id=target_id,
                target_payload=target_payload,
                env_text=env_text,
            )
        if before_provider_mutation is not None:
            before_provider_mutation("target_update")
        if effect_started is not None:
            effect_started()
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
        if before_provider_mutation is not None:
            before_provider_mutation("target_update")
        if effect_started is not None:
            effect_started()
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


def _prepare_saved_registry_login(
    *,
    host: str,
    token: str,
    artifact_id: str,
    target_payload: control_plane_dokploy.JsonObject,
) -> None:
    username = str(target_payload.get("username") or "").strip()
    password = str(target_payload.get("password") or "").strip()
    if username and password:
        return

    registry_id = str(target_payload.get("registryId") or "").strip()
    if not registry_id:
        return
    try:
        registry_payload = control_plane_dokploy.as_json_object(
            control_plane_dokploy.dokploy_request(
                host=host,
                token=token,
                path="/api/registry.one",
                query={"registryId": registry_id},
            )
        )
    except click.ClickException:
        raise click.ClickException("Dokploy saved registry lookup failed.") from None
    if registry_payload is None:
        raise click.ClickException("Dokploy saved registry lookup returned an invalid payload.")
    returned_registry_id = str(registry_payload.get("registryId") or "").strip()
    if returned_registry_id != registry_id:
        raise click.ClickException("Dokploy saved registry lookup returned the wrong registry.")

    registry_host = _canonical_registry_host(str(registry_payload.get("registryUrl") or ""))
    if registry_host != _docker_image_registry_host(artifact_id):
        raise click.ClickException(
            "Dokploy saved registry does not match the Docker image registry."
        )

    deployment_server_id = str(target_payload.get("serverId") or "").strip()
    build_server_id = str(target_payload.get("buildServerId") or "").strip()
    login_server_ids: list[str | None] = [deployment_server_id or None]
    if build_server_id and build_server_id != deployment_server_id:
        login_server_ids.append(build_server_id)

    for server_id in login_server_ids:
        login_payload: control_plane_dokploy.JsonObject = {"registryId": registry_id}
        if server_id is not None:
            login_payload["serverId"] = server_id
        try:
            control_plane_dokploy.dokploy_request(
                host=host,
                token=token,
                path="/api/registry.testRegistryById",
                method="POST",
                payload=login_payload,
            )
        except click.ClickException:
            raise click.ClickException(
                "Dokploy saved registry login failed for the target server."
            ) from None


def _docker_image_registry_host(image_reference: str) -> str:
    normalized_reference = image_reference.strip()
    first_component = normalized_reference.split("/", 1)[0]
    if "/" not in normalized_reference or not (
        "." in first_component or ":" in first_component or first_component == "localhost"
    ):
        return "docker.io"
    return _canonical_registry_host(first_component)


def _canonical_registry_host(registry_url: str) -> str:
    normalized_url = registry_url.strip()
    if not normalized_url:
        return "docker.io"
    parsed_url = urlsplit(normalized_url if "://" in normalized_url else f"//{normalized_url}")
    registry_host = (parsed_url.netloc or parsed_url.path.split("/", 1)[0]).lower()
    registry_host = registry_host.rsplit("@", 1)[-1]
    if registry_host in {"index.docker.io", "registry-1.docker.io"}:
        return "docker.io"
    return registry_host


def execute_dokploy_artifact_deploy(
    *,
    host: str,
    token: str,
    ship_request: ShipRequest,
    resolved_target: ResolvedTargetEvidence,
    deploy_timeout_seconds: int,
    runtime_identity: RuntimeIdentity | None = None,
    deployment_title: str = "",
    before_provider_mutation: Callable[[str], None] | None = None,
    effect_started: Callable[[], None] | None = None,
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
        before_provider_mutation=before_provider_mutation,
        effect_started=effect_started,
    )
    if before_provider_mutation is not None:
        before_provider_mutation("deploy_trigger")
    if effect_started is not None:
        effect_started()
    control_plane_dokploy.trigger_deployment(
        host=host,
        token=token,
        target_type=resolved_target.target_type,
        target_id=resolved_target.target_id,
        no_cache=ship_request.no_cache,
        title=deployment_title,
    )
    if deployment_title:
        control_plane_dokploy.wait_for_target_deployment(
            host=host,
            token=token,
            target_type=resolved_target.target_type,
            target_id=resolved_target.target_id,
            before_key=control_plane_dokploy.deployment_key(latest_before),
            timeout_seconds=deploy_timeout_seconds,
            deployment_title=deployment_title,
        )
    else:
        control_plane_dokploy.wait_for_target_deployment(
            host=host,
            token=token,
            target_type=resolved_target.target_type,
            target_id=resolved_target.target_id,
            before_key=control_plane_dokploy.deployment_key(latest_before),
            timeout_seconds=deploy_timeout_seconds,
        )
