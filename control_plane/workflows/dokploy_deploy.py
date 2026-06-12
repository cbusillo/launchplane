from __future__ import annotations

from typing import Protocol

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane import dokploy as control_plane_dokploy
from control_plane.contracts.deployment_record import ResolvedTargetEvidence
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.runtime_identity import RuntimeIdentity, runtime_identity_env
from control_plane.contracts.ship_request import ShipRequest


class DokployComposeSourceRefDeployRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str
    instance: str
    source_git_ref: str
    provider_source_ref: str
    timeout_seconds: int | None = Field(default=None, ge=1)
    no_cache: bool = False

    @model_validator(mode="after")
    def _validate_request(self) -> "DokployComposeSourceRefDeployRequest":
        self.context = self.context.strip()
        self.instance = self.instance.strip()
        self.source_git_ref = self.source_git_ref.strip()
        self.provider_source_ref = self.provider_source_ref.strip()
        if not self.context:
            raise ValueError("Dokploy compose source deploy requires context.")
        if not self.instance:
            raise ValueError("Dokploy compose source deploy requires instance.")
        if not self.source_git_ref:
            raise ValueError("Dokploy compose source deploy requires source_git_ref.")
        if not self.provider_source_ref:
            raise ValueError("Dokploy compose source deploy requires provider_source_ref.")
        return self


class DokployComposeSourceRefDeployResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: str
    instance: str
    target_id: str
    target_name: str = ""
    source_git_ref: str
    provider_source_ref: str
    original_source_ref: str
    restored_source_ref: str
    deploy_status: str


class DokployComposeSourceRefDeployStore(Protocol):
    def read_dokploy_target_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetRecord: ...

    def read_dokploy_target_id_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetIdRecord: ...


def _required_live_dokploy_compose_source_ref(
    target_payload: control_plane_dokploy.JsonObject,
) -> str:
    source_ref = str(target_payload.get("customGitBranch") or "").strip()
    if not source_ref:
        raise click.ClickException(
            "Dokploy source-ref deploy cannot verify the live compose source ref."
        )
    return source_ref


def update_dokploy_compose_source_ref(
    *,
    host: str,
    token: str,
    target_record: DokployTargetRecord,
    target_id: str,
    target_payload: control_plane_dokploy.JsonObject,
    provider_source_ref: str,
) -> None:
    target_definition = control_plane_dokploy.DokployTargetDefinition(
        context=target_record.context,
        instance=target_record.instance,
        project_name=target_record.project_name,
        target_type=target_record.target_type,
        target_id=target_id,
        target_name=target_record.target_name,
        git_branch=target_record.git_branch,
        source_git_ref=target_record.source_git_ref,
        source_type=target_record.source_type,
        custom_git_url=target_record.custom_git_url,
        custom_git_branch=provider_source_ref,
        compose_path=target_record.compose_path,
        watch_paths=target_record.watch_paths,
        enable_submodules=target_record.enable_submodules,
        require_test_gate=target_record.require_test_gate,
        require_prod_gate=target_record.require_prod_gate,
        deploy_timeout_seconds=target_record.deploy_timeout_seconds,
        healthcheck_enabled=target_record.healthcheck_enabled,
        healthcheck_path=target_record.healthcheck_path,
        healthcheck_timeout_seconds=target_record.healthcheck_timeout_seconds,
        env=target_record.env,
        domains=target_record.domains,
        policies=target_record.policies,
    )
    control_plane_dokploy.update_dokploy_target_source(
        host=host,
        token=token,
        target_definition=target_definition,
        target_payload=target_payload,
    )


def execute_dokploy_compose_source_ref_deploy(
    *,
    host: str,
    token: str,
    record_store: DokployComposeSourceRefDeployStore,
    request: DokployComposeSourceRefDeployRequest,
) -> DokployComposeSourceRefDeployResult:
    target_record = record_store.read_dokploy_target_record(
        context_name=request.context,
        instance_name=request.instance,
    )
    target_id_record = record_store.read_dokploy_target_id_record(
        context_name=request.context,
        instance_name=request.instance,
    )
    if target_record.target_type != "compose":
        raise click.ClickException(
            "Dokploy source-ref deploy requires a compose target. "
            f"Configured target type: {target_record.target_type}."
        )
    if target_record.context != target_id_record.context:
        raise click.ClickException("Dokploy target context must match target-id context.")
    if target_record.instance != target_id_record.instance:
        raise click.ClickException("Dokploy target instance must match target-id instance.")

    target_id = target_id_record.target_id.strip()
    target_payload = control_plane_dokploy.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type="compose",
        target_id=target_id,
    )
    original_source_ref = _required_live_dokploy_compose_source_ref(target_payload)

    latest_before = control_plane_dokploy.latest_deployment_for_target(
        host=host,
        token=token,
        target_type="compose",
        target_id=target_id,
    )
    deploy_error: Exception | None = None
    deploy_status = "fail"
    restored_source_ref = ""
    source_ref_update_attempted = False
    try:
        source_ref_update_attempted = True
        update_dokploy_compose_source_ref(
            host=host,
            token=token,
            target_id=target_id,
            target_payload=target_payload,
            target_record=target_record,
            provider_source_ref=request.provider_source_ref,
        )
        control_plane_dokploy.trigger_deployment(
            host=host,
            token=token,
            target_type="compose",
            target_id=target_id,
            no_cache=request.no_cache,
        )
        control_plane_dokploy.wait_for_target_deployment(
            host=host,
            token=token,
            target_type="compose",
            target_id=target_id,
            before_key=control_plane_dokploy.deployment_key(latest_before),
            timeout_seconds=(
                request.timeout_seconds
                or target_record.deploy_timeout_seconds
                or control_plane_dokploy.DEFAULT_DOKPLOY_DEPLOY_TIMEOUT_SECONDS
            ),
        )
        deploy_status = "pass"
    except Exception as exc:
        deploy_error = exc
    finally:
        if source_ref_update_attempted:
            try:
                restore_payload = control_plane_dokploy.fetch_dokploy_target_payload(
                    host=host,
                    token=token,
                    target_type="compose",
                    target_id=target_id,
                )
                live_source_ref = _required_live_dokploy_compose_source_ref(restore_payload)
                if live_source_ref == original_source_ref:
                    restored_source_ref = original_source_ref
                elif live_source_ref == request.provider_source_ref:
                    update_dokploy_compose_source_ref(
                        host=host,
                        token=token,
                        target_id=target_id,
                        target_payload=restore_payload,
                        target_record=target_record,
                        provider_source_ref=original_source_ref,
                    )
                    restored_source_ref = original_source_ref
                else:
                    raise click.ClickException(
                        "Dokploy source-ref deploy cannot restore because the compose source ref "
                        f"changed from {request.provider_source_ref!r} to {live_source_ref!r}."
                    )
            except Exception as exc:
                if deploy_status == "pass":
                    raise click.ClickException(
                        "Dokploy source-ref deploy succeeded, but restoring the compose source ref failed."
                    ) from exc
                raise click.ClickException(
                    "Dokploy source-ref deploy failed, and restoring the compose source ref also "
                    f"failed. Deploy error: {deploy_error}"
                ) from exc

    if deploy_error is not None:
        raise deploy_error

    return DokployComposeSourceRefDeployResult(
        context=request.context,
        instance=request.instance,
        target_id=target_id,
        target_name=target_record.target_name,
        source_git_ref=request.source_git_ref,
        provider_source_ref=request.provider_source_ref,
        original_source_ref=original_source_ref,
        restored_source_ref=restored_source_ref,
        deploy_status=deploy_status,
    )


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
