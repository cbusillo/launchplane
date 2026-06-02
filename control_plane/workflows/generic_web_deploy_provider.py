from __future__ import annotations

from pathlib import Path
from typing import Protocol, cast

import click
from pydantic import BaseModel, ConfigDict, Field

from control_plane import dokploy as control_plane_dokploy
from control_plane import runtime_environments as control_plane_runtime_environments
from control_plane.contracts.deploy_target import (
    DeployedTargetReference,
    DeployTargetCompatibilityType,
    ProviderTargetRecord,
)
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.deployment_record import ResolvedTargetEvidence
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.contracts.promotion_record import HealthcheckEvidence
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.contracts.ship_request import ShipRequest
from control_plane.workflows.dokploy_deploy import execute_dokploy_artifact_deploy


class GenericWebResolvedDeployTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ship_request: ShipRequest
    resolved_target: ResolvedTargetEvidence
    deployed_target: DeployedTargetReference | None = None
    deploy_timeout_seconds: int = Field(ge=1)


class DokployGenericWebDeployStore(Protocol):
    def read_provider_target_record(
        self, *, context_name: str, instance_name: str
    ) -> ProviderTargetRecord: ...

    def read_dokploy_target_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetRecord: ...

    def read_dokploy_target_id_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetIdRecord: ...


class GenericWebDeployProvider(Protocol):
    provider_id: str
    delegated_executor: str

    def resolve_deploy_target(
        self,
        *,
        control_plane_root: Path,
        request_artifact_id: str,
        request_source_git_ref: str,
        request_timeout_seconds: int | None,
        request_no_cache: bool,
        record_store: object,
        profile: LaunchplaneProductProfileRecord,
        lane: ProductLaneProfile,
        normalized_artifact_id: str,
        fallback_target_name: str,
    ) -> GenericWebResolvedDeployTarget: ...

    def execute_artifact_deploy(
        self,
        *,
        control_plane_root: Path,
        resolved_deploy_target: GenericWebResolvedDeployTarget,
        runtime_identity: RuntimeIdentity,
    ) -> None: ...


class DokployGenericWebDeployProvider:
    provider_id = "dokploy"
    delegated_executor = "control-plane.dokploy"

    def resolve_deploy_target(
        self,
        *,
        control_plane_root: Path,
        request_artifact_id: str,
        request_source_git_ref: str,
        request_timeout_seconds: int | None,
        request_no_cache: bool,
        record_store: object,
        profile: LaunchplaneProductProfileRecord,
        lane: ProductLaneProfile,
        normalized_artifact_id: str,
        fallback_target_name: str,
    ) -> GenericWebResolvedDeployTarget:
        del request_artifact_id, profile
        dokploy_store = _require_dokploy_deploy_store(record_store)
        try:
            provider_target = dokploy_store.read_provider_target_record(
                context_name=lane.context,
                instance_name=lane.instance,
            )
        except FileNotFoundError as exc:
            raise click.ClickException(
                "Missing provider-target authority for "
                f"{lane.context}/{lane.instance}. Run provider-target audit/backfill before deploying."
            ) from exc
        target_record = dokploy_store.read_dokploy_target_record(
            context_name=lane.context,
            instance_name=lane.instance,
        )
        target_id_record = dokploy_store.read_dokploy_target_id_record(
            context_name=lane.context,
            instance_name=lane.instance,
        )
        target_definition = _dokploy_target_definition_for_lane(
            target_record=target_record,
            target_id_record=target_id_record,
        )
        _validate_dokploy_provider_target(
            provider_target=provider_target,
            target_definition=target_definition,
        )
        provider_target_type = _dokploy_target_type_from_provider_target(
            provider_target=provider_target
        )

        environment_values = control_plane_runtime_environments.resolve_runtime_environment_values(
            control_plane_root=control_plane_root,
            context_name=lane.context,
            instance_name=lane.instance,
        )
        configured_ship_mode = control_plane_dokploy.resolve_dokploy_ship_mode(
            lane.context,
            lane.instance,
            environment_values,
        )
        deploy_mode = _resolve_dokploy_deploy_mode(
            configured_ship_mode=configured_ship_mode,
            target_type=provider_target_type,
        )
        target_name = provider_target.display_name.strip() or fallback_target_name
        ship_request = ShipRequest(
            artifact_id=normalized_artifact_id,
            context=lane.context,
            instance=lane.instance,
            source_git_ref=request_source_git_ref,
            target_name=target_name,
            target_type=provider_target_type,
            deploy_mode=deploy_mode,
            provider_id=self.provider_id,
            target_category=provider_target.target_category,
            provider_target_type=provider_target_type,
            provider_deploy_mode=deploy_mode,
            wait=True,
            timeout_seconds=request_timeout_seconds,
            verify_health=False,
            no_cache=request_no_cache,
            destination_health=HealthcheckEvidence(status="skipped"),
        )
        resolved_target = ResolvedTargetEvidence(
            target_type=provider_target_type,
            target_id=provider_target.target_id,
            target_name=target_name,
        )
        deployed_target = provider_target.to_deployed_target_reference()
        deploy_timeout_seconds = control_plane_dokploy.resolve_ship_timeout_seconds(
            timeout_override_seconds=request_timeout_seconds,
            target_definition=target_definition,
        )
        return GenericWebResolvedDeployTarget(
            ship_request=ship_request,
            resolved_target=resolved_target,
            deployed_target=deployed_target,
            deploy_timeout_seconds=deploy_timeout_seconds,
        )

    def execute_artifact_deploy(
        self,
        *,
        control_plane_root: Path,
        resolved_deploy_target: GenericWebResolvedDeployTarget,
        runtime_identity: RuntimeIdentity,
    ) -> None:
        host, token = control_plane_dokploy.read_dokploy_config(
            control_plane_root=control_plane_root
        )
        execute_dokploy_artifact_deploy(
            host=host,
            token=token,
            ship_request=resolved_deploy_target.ship_request,
            resolved_target=resolved_deploy_target.resolved_target,
            deploy_timeout_seconds=resolved_deploy_target.deploy_timeout_seconds,
            runtime_identity=runtime_identity,
        )


def _resolve_dokploy_deploy_mode(*, configured_ship_mode: str, target_type: str) -> str:
    if configured_ship_mode == "auto":
        return f"dokploy-{target_type}-api"
    return f"dokploy-{configured_ship_mode}-api"


def _require_dokploy_deploy_store(
    record_store: object,
) -> DokployGenericWebDeployStore:
    required_methods = (
        "read_provider_target_record",
        "read_dokploy_target_record",
        "read_dokploy_target_id_record",
    )
    missing_methods = tuple(
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    )
    if missing_methods:
        raise click.ClickException(
            "Generic web Dokploy deploy requires DB-backed provider-target and Dokploy target records. "
            f"Missing methods: {', '.join(missing_methods)}."
        )
    return cast(DokployGenericWebDeployStore, record_store)


def _dokploy_target_definition_for_lane(
    *,
    target_record: DokployTargetRecord,
    target_id_record: DokployTargetIdRecord,
) -> control_plane_dokploy.DokployTargetDefinition:
    if target_record.context != target_id_record.context:
        raise click.ClickException("Dokploy target context must match target-id context.")
    if target_record.instance != target_id_record.instance:
        raise click.ClickException("Dokploy target instance must match target-id instance.")
    return control_plane_dokploy.DokployTargetDefinition(
        context=target_record.context,
        instance=target_record.instance,
        project_name=target_record.project_name,
        target_type=target_record.target_type,
        target_id=target_id_record.target_id,
        target_name=target_record.target_name,
        git_branch=target_record.git_branch,
        source_git_ref=target_record.source_git_ref,
        source_type=target_record.source_type,
        custom_git_url=target_record.custom_git_url,
        custom_git_branch=target_record.custom_git_branch,
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


def _dokploy_target_type_from_provider_target(
    *, provider_target: ProviderTargetRecord
) -> DeployTargetCompatibilityType:
    if provider_target.provider_target_type not in {"application", "compose"}:
        raise click.ClickException(
            "Provider-target type mismatch for "
            f"{provider_target.context}/{provider_target.instance}: "
            "Dokploy execution only supports provider-target type 'application' or 'compose'."
        )
    return cast(DeployTargetCompatibilityType, provider_target.provider_target_type)


def _validate_dokploy_provider_target(
    *,
    provider_target: ProviderTargetRecord,
    target_definition: control_plane_dokploy.DokployTargetDefinition,
) -> None:
    expected_provider_id = "dokploy"
    if provider_target.provider_id != expected_provider_id:
        raise click.ClickException(
            "Provider-target identity mismatch for "
            f"{provider_target.context}/{provider_target.instance}: "
            f"expected provider {expected_provider_id!r}, found {provider_target.provider_id!r}."
        )
    if provider_target.provider_target_type != target_definition.target_type:
        raise click.ClickException(
            "Provider-target type mismatch for "
            f"{provider_target.context}/{provider_target.instance}: "
            f"provider-target has {provider_target.provider_target_type!r}, "
            f"Dokploy execution config has {target_definition.target_type!r}."
        )
    if provider_target.target_category != target_definition.target_type:
        raise click.ClickException(
            "Provider-target category mismatch for "
            f"{provider_target.context}/{provider_target.instance}: "
            f"provider-target has {provider_target.target_category!r}, "
            f"Dokploy execution config has {target_definition.target_type!r}."
        )
    if provider_target.target_id != target_definition.target_id:
        raise click.ClickException(
            "Provider-target id mismatch for "
            f"{provider_target.context}/{provider_target.instance}: "
            f"provider-target has {provider_target.target_id!r}, "
            f"Dokploy execution config has {target_definition.target_id!r}."
        )


def default_generic_web_deploy_provider() -> GenericWebDeployProvider:
    return DokployGenericWebDeployProvider()
