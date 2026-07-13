from __future__ import annotations

import base64
from collections.abc import Callable
import hashlib
from pathlib import Path
from typing import Literal, Protocol, cast

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane import dokploy as control_plane_dokploy
from control_plane import runtime_environments as control_plane_runtime_environments
from control_plane import secrets as control_plane_secrets
from control_plane.contracts.deploy_target import (
    DeployedTargetReference,
    DeployTargetCategory,
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
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
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


class GenericWebProviderReconciliationTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str
    instance: str
    provider_id: str
    target_category: DeployTargetCategory
    provider_target_type: str
    target_type: DeployTargetCompatibilityType
    target_id: str
    target_name: str
    deploy_mode: str
    provider_deploy_mode: str
    deploy_timeout_seconds: int = Field(ge=1)


_GENERIC_WEB_RECONCILIATION_KEY_PREFIX = "generic-web-provider-target:"
_GENERIC_WEB_PROVIDER_SUCCESS_STATUSES = frozenset(
    {"success", "succeeded", "done", "completed", "healthy", "finished"}
)


def generic_web_provider_deployment_succeeded(status: str) -> bool:
    return status.strip().lower() in _GENERIC_WEB_PROVIDER_SUCCESS_STATUSES


def build_generic_web_provider_target_key(
    resolved_deploy_target: GenericWebResolvedDeployTarget,
) -> str:
    ship_request = resolved_deploy_target.ship_request
    identity = "\x1f".join(
        (
            ship_request.provider_id,
            ship_request.context,
            ship_request.instance,
            resolved_deploy_target.resolved_target.target_type,
            resolved_deploy_target.resolved_target.target_id,
        )
    )
    return f"generic-web-provider-target:{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"


def build_generic_web_provider_reconciliation_key(
    resolved_deploy_target: GenericWebResolvedDeployTarget,
) -> str:
    ship_request = resolved_deploy_target.ship_request
    snapshot = GenericWebProviderReconciliationTarget(
        context=ship_request.context,
        instance=ship_request.instance,
        provider_id=ship_request.provider_id,
        target_category=ship_request.target_category,
        provider_target_type=ship_request.provider_target_type,
        target_type=resolved_deploy_target.resolved_target.target_type,
        target_id=resolved_deploy_target.resolved_target.target_id,
        target_name=resolved_deploy_target.resolved_target.target_name,
        deploy_mode=ship_request.deploy_mode,
        provider_deploy_mode=ship_request.provider_deploy_mode,
        deploy_timeout_seconds=resolved_deploy_target.deploy_timeout_seconds,
    )
    encoded = base64.urlsafe_b64encode(snapshot.model_dump_json().encode("utf-8")).decode("ascii")
    return f"{_GENERIC_WEB_RECONCILIATION_KEY_PREFIX}{encoded.rstrip('=')}"


def resolve_generic_web_provider_reconciliation_target(
    *,
    reconciliation_key: str,
    request_artifact_id: str,
    request_source_git_ref: str,
    request_timeout_seconds: int | None,
    request_no_cache: bool,
    normalized_artifact_id: str,
    lane: ProductLaneProfile,
) -> GenericWebResolvedDeployTarget:
    normalized_key = reconciliation_key.strip()
    if not normalized_key.startswith(_GENERIC_WEB_RECONCILIATION_KEY_PREFIX):
        raise ValueError("Generic web provider reconciliation key is invalid.")
    encoded = normalized_key.removeprefix(_GENERIC_WEB_RECONCILIATION_KEY_PREFIX)
    try:
        decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        snapshot = GenericWebProviderReconciliationTarget.model_validate_json(decoded)
    except Exception as error:
        raise ValueError("Generic web provider reconciliation key is invalid.") from error
    context_name = lane.context.strip()
    instance_name = lane.instance.strip()
    if snapshot.context != context_name or snapshot.instance != instance_name:
        raise ValueError("Generic web provider reconciliation target does not match the lane.")
    del request_artifact_id
    ship_request = ShipRequest(
        artifact_id=normalized_artifact_id,
        context=context_name,
        instance=instance_name,
        source_git_ref=request_source_git_ref,
        target_name=snapshot.target_name,
        target_type=snapshot.target_type,
        deploy_mode=snapshot.deploy_mode,
        provider_id=snapshot.provider_id,
        target_category=snapshot.target_category,
        provider_target_type=snapshot.provider_target_type,
        provider_deploy_mode=snapshot.provider_deploy_mode,
        wait=True,
        timeout_seconds=request_timeout_seconds,
        verify_health=False,
        no_cache=request_no_cache,
        destination_health=HealthcheckEvidence(status="skipped"),
    )
    resolved_target = ResolvedTargetEvidence(
        target_type=snapshot.target_type,
        target_id=snapshot.target_id,
        target_name=snapshot.target_name,
    )
    return GenericWebResolvedDeployTarget(
        ship_request=ship_request,
        resolved_target=resolved_target,
        deployed_target=DeployedTargetReference(
            provider_id=snapshot.provider_id,
            target_category=snapshot.target_category,
            target_id=snapshot.target_id,
            display_name=snapshot.target_name,
            provider_target_type=snapshot.provider_target_type,
        ),
        deploy_timeout_seconds=snapshot.deploy_timeout_seconds,
    )


class GenericWebProviderDeploymentObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Literal["present", "absent", "unknown"]
    deployment_status: str = ""
    deployment_id: str = ""
    started_at: str = ""
    finished_at: str = ""
    error_message: str = ""

    @model_validator(mode="after")
    def _validate_terminal_evidence(self) -> "GenericWebProviderDeploymentObservation":
        if self.outcome != "present":
            return self
        required_evidence = {
            "deployment_status": self.deployment_status,
            "deployment_id": self.deployment_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }
        missing = [name for name, value in required_evidence.items() if not value.strip()]
        if missing:
            raise ValueError(
                "Observed generic web provider deployments require exact terminal evidence: "
                + ", ".join(missing)
                + "."
            )
        return self


class DokployGenericWebDeployStore(control_plane_secrets.SecretReadStore, Protocol):
    def read_provider_target_record(
        self, *, context_name: str, instance_name: str
    ) -> ProviderTargetRecord: ...

    def read_dokploy_target_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetRecord: ...

    def read_dokploy_target_id_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetIdRecord: ...

    def list_runtime_environment_records(
        self, *, context_name: str = "", instance_name: str = ""
    ) -> tuple[RuntimeEnvironmentRecord, ...]: ...


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
        deployment_title: str,
        before_provider_mutation: Callable[[str], None],
        effect_started: Callable[[], None],
    ) -> None: ...

    def observe_artifact_deploy(
        self,
        *,
        control_plane_root: Path,
        resolved_deploy_target: GenericWebResolvedDeployTarget,
        deployment_title: str,
    ) -> GenericWebProviderDeploymentObservation: ...


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
        context_name = lane.context.strip()
        instance_name = lane.instance.strip()
        try:
            provider_target = dokploy_store.read_provider_target_record(
                context_name=context_name,
                instance_name=instance_name,
            )
        except FileNotFoundError as exc:
            raise click.ClickException(
                "Missing provider-target authority for "
                f"{context_name}/{instance_name}. Run provider-target audit/backfill before deploying."
            ) from exc
        target_record = dokploy_store.read_dokploy_target_record(
            context_name=context_name,
            instance_name=instance_name,
        )
        target_id_record = dokploy_store.read_dokploy_target_id_record(
            context_name=context_name,
            instance_name=instance_name,
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

        environment_values = _resolve_store_runtime_environment_values(
            record_store=dokploy_store,
            context_name=context_name,
            instance_name=instance_name,
            target_environment=target_definition.env,
        )
        configured_ship_mode = control_plane_dokploy.resolve_dokploy_ship_mode(
            context_name,
            instance_name,
            environment_values,
        )
        deploy_mode = _resolve_dokploy_deploy_mode(
            configured_ship_mode=configured_ship_mode,
            target_type=provider_target_type,
        )
        target_name = provider_target.display_name.strip() or fallback_target_name
        ship_request = ShipRequest(
            artifact_id=normalized_artifact_id,
            context=context_name,
            instance=instance_name,
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
        deployment_title: str,
        before_provider_mutation: Callable[[str], None],
        effect_started: Callable[[], None],
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
            deployment_title=deployment_title,
            before_provider_mutation=before_provider_mutation,
            effect_started=effect_started,
        )

    def observe_artifact_deploy(
        self,
        *,
        control_plane_root: Path,
        resolved_deploy_target: GenericWebResolvedDeployTarget,
        deployment_title: str,
    ) -> GenericWebProviderDeploymentObservation:
        host, token = control_plane_dokploy.read_dokploy_config(
            control_plane_root=control_plane_root
        )
        target = resolved_deploy_target.resolved_target
        deployment = control_plane_dokploy.deployment_for_target_by_title(
            host=host,
            token=token,
            target_type=target.target_type,
            target_id=target.target_id,
            title=deployment_title,
        )
        status = control_plane_dokploy.deployment_status(deployment)
        if deployment is None:
            return GenericWebProviderDeploymentObservation(outcome="absent")
        if status in {"", "pending", "queued", "running"}:
            return GenericWebProviderDeploymentObservation(
                outcome="unknown",
                deployment_status=status,
            )
        if status not in {
            "success",
            "succeeded",
            "done",
            "completed",
            "healthy",
            "finished",
            "failed",
            "error",
            "canceled",
            "cancelled",
            "killed",
            "unhealthy",
            "timeout",
        }:
            return GenericWebProviderDeploymentObservation(
                outcome="unknown",
                deployment_status=status,
            )
        assert deployment is not None
        return GenericWebProviderDeploymentObservation(
            outcome="present",
            deployment_status=status,
            deployment_id=control_plane_dokploy.deployment_key(deployment),
            started_at=str(
                deployment.get("startedAt") or deployment.get("started_at") or ""
            ).strip(),
            finished_at=str(
                deployment.get("finishedAt") or deployment.get("finished_at") or ""
            ).strip(),
            error_message=str(
                deployment.get("errorMessage") or deployment.get("error_message") or ""
            ).strip(),
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
        "list_runtime_environment_records",
        "read_secret_record",
        "list_secret_records",
        "read_secret_version",
        "list_secret_versions",
        "list_secret_bindings",
        "list_secret_audit_events",
    )
    missing_methods = tuple(
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    )
    if missing_methods:
        raise click.ClickException(
            "Generic web Dokploy deploy requires DB-backed provider-target, Dokploy target, "
            "runtime-environment, and managed-secret records. "
            f"Missing methods: {', '.join(missing_methods)}."
        )
    return cast(DokployGenericWebDeployStore, record_store)


def _resolve_store_runtime_environment_values(
    *,
    record_store: DokployGenericWebDeployStore,
    context_name: str,
    instance_name: str,
    target_environment: dict[str, str],
) -> dict[str, str]:
    definition = (
        control_plane_runtime_environments.load_optional_runtime_environment_definition_from_store(
            record_store=record_store
        )
    )
    merged_values: dict[str, str] = {}
    if definition is not None:
        merged_values.update(
            control_plane_runtime_environments.resolve_optional_values_from_definition(
                definition=definition,
                context_name=context_name,
                instance_name=instance_name,
            )
        )
    merged_values.update(target_environment)
    return control_plane_secrets.overlay_runtime_environment_secret_values_from_store(
        environment_values=merged_values,
        record_store=record_store,
        context_name=context_name,
        instance_name=instance_name,
    )


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
