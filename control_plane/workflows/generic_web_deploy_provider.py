from __future__ import annotations

import base64
from collections.abc import Callable
from datetime import datetime, timedelta
import hashlib
from pathlib import Path
import re
from typing import Literal, Protocol, cast, runtime_checkable

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane import runtime_environments as control_plane_runtime_environments
from control_plane import secrets as control_plane_secrets
from control_plane.contracts.deploy_target import (
    DeployedTargetReference,
    DeployTargetCategory,
    DeployTargetCompatibilityType,
    ProviderTargetRecord,
)
from control_plane.contracts.deploy_reference import validate_provider_deploy_reference
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.deployment_record import ResolvedTargetEvidence
from control_plane.contracts.idempotency_record import parse_launchplane_mutation_timestamp
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.workflows.generic_web_application_snapshot import ApplicationLifecycleSnapshot
from control_plane.workflows.generic_web_application_lifecycle import (
    GenericWebApplicationLifecycle,
    dokploy_application_lifecycle,
)
from control_plane.contracts.promotion_record import HealthcheckEvidence
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.contracts.ship_request import ShipRequest
from control_plane.workflows.dokploy_deploy import (
    dokploy_deploy_artifact_reference,
    execute_dokploy_artifact_deploy,
)
from control_plane.dokploy import api as dokploy_api
from control_plane.dokploy import runtime_evidence as dokploy_runtime_evidence
from control_plane.dokploy import source as dokploy_source


class GenericWebResolvedDeployTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ship_request: ShipRequest
    resolved_target: ResolvedTargetEvidence
    deployed_target: DeployedTargetReference | None = None
    deploy_timeout_seconds: int = Field(ge=1)
    provider_dispatch_evidence: dict[str, object] = Field(default_factory=dict)


class GenericWebProviderReconciliationTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    original_event_deploy: dict[str, object] | None = None
    qualification_authority_digest: str = Field(default="", exclude_if=lambda value: not value)
    product: str = ""
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
    return f"generic-web-provider-target:{hashlib.sha256(identity.encode()).hexdigest()}"


def build_generic_web_provider_reconciliation_key(
    resolved_deploy_target: GenericWebResolvedDeployTarget,
    *,
    product: str = "",
    original_event_deploy: dict[str, object] | None = None,
    qualification_authority_digest: str = "",
) -> str:
    ship_request = resolved_deploy_target.ship_request
    snapshot = GenericWebProviderReconciliationTarget(
        product=product.strip(),
        original_event_deploy=original_event_deploy,
        qualification_authority_digest=qualification_authority_digest,
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
    encoded = base64.urlsafe_b64encode(snapshot.model_dump_json(exclude_none=True).encode()).decode(
        "ascii"
    )
    return f"{_GENERIC_WEB_RECONCILIATION_KEY_PREFIX}{encoded.rstrip('=')}"


def decode_generic_web_provider_reconciliation_target(
    reconciliation_key: str,
) -> GenericWebProviderReconciliationTarget:
    normalized_key = reconciliation_key.strip()
    if not normalized_key.startswith(_GENERIC_WEB_RECONCILIATION_KEY_PREFIX):
        raise ValueError("Generic web provider reconciliation key is invalid.")
    encoded = normalized_key.removeprefix(_GENERIC_WEB_RECONCILIATION_KEY_PREFIX)
    try:
        decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        snapshot = GenericWebProviderReconciliationTarget.model_validate_json(decoded)
    except Exception as error:
        raise ValueError("Generic web provider reconciliation key is invalid.") from error
    if not snapshot.context.strip() or not snapshot.instance.strip():
        raise ValueError("Generic web provider reconciliation target identity is incomplete.")
    return snapshot


def resolve_generic_web_provider_reconciliation_target(
    *,
    reconciliation_key: str,
    request_artifact_id: str,
    request_source_git_ref: str,
    request_timeout_seconds: int | None,
    request_no_cache: bool,
    normalized_artifact_id: str,
    lane: ProductLaneProfile,
    request_deploy_reference: str = "",
) -> GenericWebResolvedDeployTarget:
    snapshot = decode_generic_web_provider_reconciliation_target(reconciliation_key)
    context_name = lane.context.strip()
    instance_name = lane.instance.strip()
    if snapshot.context != context_name or snapshot.instance != instance_name:
        raise ValueError("Generic web provider reconciliation target does not match the lane.")
    del request_artifact_id
    ship_request = ShipRequest(
        artifact_id=normalized_artifact_id,
        deploy_reference=validate_provider_deploy_reference(
            artifact_id=normalized_artifact_id,
            deploy_reference=request_deploy_reference,
            label="Generic web deploy",
        ),
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


class GenericWebProviderDeploymentFailed(click.ClickException):
    """A correlated terminal provider failure, with complete deployment references."""

    def __init__(self, observation: GenericWebProviderDeploymentObservation) -> None:
        if observation.outcome != "present" or observation.deployment_status not in {
            "failed",
            "error",
            "canceled",
            "cancelled",
            "killed",
            "unhealthy",
            "timeout",
        }:
            raise ValueError("A provider failure requires determinate terminal failure evidence.")
        started = parse_launchplane_mutation_timestamp(
            observation.started_at, field_name="started_at"
        )
        finished = parse_launchplane_mutation_timestamp(
            observation.finished_at, field_name="finished_at"
        )
        if finished < started:
            raise ValueError("Provider failure timestamps are out of order.")
        self.observation = observation
        super().__init__(observation.error_message or "Generic web provider deployment failed.")


class GenericWebLegacyDeploymentCorrelationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    deployment_id_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    deployment_title_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    deployment_created_at: str
    deployment_started_at: str
    deployment_finished_at: str
    artifact_reference_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class GenericWebLegacyDeploymentCorrelation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observation: GenericWebProviderDeploymentObservation
    digest_evidence: GenericWebLegacyDeploymentCorrelationEvidence

    @model_validator(mode="after")
    def _validate_observation(self) -> "GenericWebLegacyDeploymentCorrelation":
        if self.observation.outcome != "present":
            raise ValueError("Legacy deployment correlation requires a present observation.")
        return self


class GenericWebRuntimeArtifactObservation(BaseModel):
    """Read-only runtime facts for one exact provider target.

    ``target_artifact_reference`` is the image the provider target is configured
    to run. ``running_container_images`` lists the configured image of every
    running container that belongs to the target.
    """

    model_config = ConfigDict(extra="forbid")

    target_artifact_reference: str
    running_container_images: tuple[str, ...]
    running_container_deployment_record_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_aligned_record_ids(self) -> "GenericWebRuntimeArtifactObservation":
        if self.running_container_deployment_record_ids and len(
            self.running_container_deployment_record_ids
        ) != len(self.running_container_images):
            raise ValueError("Runtime deployment record ids must align with running images.")
        return self


class GenericWebRuntimeCloseOutEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    artifact_reference_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    running_container_count: int = Field(ge=1)
    matching_container_count: int = Field(ge=1)


_IMMUTABLE_ARTIFACT_REFERENCE_PATTERN = re.compile(r"^[^\s@]+@sha256:[a-f0-9]{64}$")


def _image_repository(image_reference: str) -> str:
    """Return the canonical repository of an image reference, without tag or digest."""

    reference = image_reference.strip().split("@", 1)[0]
    registry_and_path, separator, last_segment = reference.rpartition("/")
    repository = f"{registry_and_path}{separator}{last_segment.split(':', 1)[0]}".lower()
    first_segment, _, remainder = repository.partition("/")
    if not remainder:
        return f"docker.io/library/{repository}"
    if first_segment == "index.docker.io":
        first_segment = "docker.io"
    elif "." not in first_segment and ":" not in first_segment and first_segment != "localhost":
        return f"docker.io/{repository}"
    if first_segment == "docker.io" and "/" not in remainder:
        return f"docker.io/library/{remainder}"
    return f"{first_segment}/{remainder}"


def evaluate_generic_web_runtime_close_out(
    *,
    observation: GenericWebRuntimeArtifactObservation,
    expected_artifact_reference: str,
    expected_deployment_record_id: str = "",
) -> GenericWebRuntimeCloseOutEvidence:
    """Prove that an original deploy's exact image is configured and running.

    Raises ``ValueError`` unless the target is configured for the exact
    immutable original image, at least one running container runs it, and no
    running container runs any other reference of the same image repository.
    With ``expected_deployment_record_id``, every running container of that
    repository must also carry that deployment record id, which ties the running
    containers to the operation that set it rather than an earlier deploy of the
    same image.
    """

    expected_reference = expected_artifact_reference.strip()
    if not _IMMUTABLE_ARTIFACT_REFERENCE_PATTERN.fullmatch(expected_reference):
        raise ValueError("Runtime close-out requires an immutable original artifact reference.")
    if observation.target_artifact_reference.strip() != expected_reference:
        raise ValueError("Runtime close-out target artifact does not match the original deploy.")
    expected_repository = _image_repository(expected_reference)
    running_images = tuple(image.strip() for image in observation.running_container_images)
    repository_images = tuple(
        image for image in running_images if _image_repository(image) == expected_repository
    )
    if not repository_images:
        raise ValueError("Runtime close-out found no running container for the original image.")
    if any(image != expected_reference for image in repository_images):
        raise ValueError("Runtime close-out found a running container with another image.")
    expected_record_id = expected_deployment_record_id.strip()
    if expected_record_id:
        record_ids = observation.running_container_deployment_record_ids
        if len(record_ids) != len(running_images) or any(
            record_id.strip() != expected_record_id
            for image, record_id in zip(running_images, record_ids, strict=True)
            if _image_repository(image) == expected_repository
        ):
            raise ValueError("Runtime close-out containers were not created by this deployment.")
    return GenericWebRuntimeCloseOutEvidence(
        artifact_reference_sha256=_legacy_correlation_sha256(expected_reference),
        running_container_count=len(running_images),
        matching_container_count=len(repository_images),
    )


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
        request_deploy_reference: str = "",
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
    ) -> GenericWebProviderDeploymentObservation | None:
        """Deploy and wait; return an untitled deployment observation or ``None``.

        ``None`` means the deployment carrying ``deployment_title`` finished, so
        the caller observes it by title. A returned observation is the only new
        provider deployment, which did not carry the title; the caller must
        prove its effect from runtime evidence before treating it as complete.
        """
        ...

    def observe_artifact_deploy(
        self,
        *,
        control_plane_root: Path,
        resolved_deploy_target: GenericWebResolvedDeployTarget,
        deployment_title: str,
    ) -> GenericWebProviderDeploymentObservation: ...


@runtime_checkable
class GenericWebDeployLegacyCorrelationProvider(Protocol):
    def observe_legacy_artifact_deploy(
        self,
        *,
        control_plane_root: Path,
        resolved_deploy_target: GenericWebResolvedDeployTarget,
        provider_effect_started_at: str,
    ) -> GenericWebLegacyDeploymentCorrelation: ...


@runtime_checkable
class GenericWebDeployRuntimeArtifactProvider(Protocol):
    def observe_runtime_artifact(
        self,
        *,
        control_plane_root: Path,
        resolved_deploy_target: GenericWebResolvedDeployTarget,
    ) -> GenericWebRuntimeArtifactObservation: ...


@runtime_checkable
class GenericWebDeployNoLiveEffectProvider(Protocol):
    def observe_testing_no_live_effect(
        self,
        *,
        control_plane_root: Path,
        target: GenericWebProviderReconciliationTarget,
        operation_title: str,
        provider_effect_phase: str,
    ) -> dict[str, object] | None: ...


class TestingEventDeployRecoveryRefused(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class DokployGenericWebDeployProvider:
    provider_id = "dokploy"
    delegated_executor = "control-plane.dokploy"

    def application_lifecycle(
        self,
        *,
        control_plane_root: Path,
        snapshot: ApplicationLifecycleSnapshot,
        target_type: str,
        target_id: str,
        checkpoint: Callable[[str], None],
        effect_started: Callable[[], None],
    ) -> GenericWebApplicationLifecycle:
        return dokploy_application_lifecycle(
            control_plane_root=control_plane_root,
            snapshot=snapshot,
            target_type=target_type,
            target_id=target_id,
            checkpoint=checkpoint,
            effect_started=effect_started,
        )

    def _read_provider_config(self, *, control_plane_root: Path) -> tuple[str, str]:
        if self.provider_id != "dokploy":
            raise RuntimeError("Dokploy provider identity is invalid.")
        return dokploy_source.read_dokploy_config(control_plane_root=control_plane_root)

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
        request_deploy_reference: str = "",
    ) -> GenericWebResolvedDeployTarget:
        del control_plane_root, request_artifact_id, profile
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
        configured_ship_mode = dokploy_source.resolve_dokploy_ship_mode(
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
            deploy_reference=validate_provider_deploy_reference(
                artifact_id=normalized_artifact_id,
                deploy_reference=request_deploy_reference,
                label="Generic web deploy",
            ),
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
        deploy_timeout_seconds = dokploy_source.resolve_ship_timeout_seconds(
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
    ) -> GenericWebProviderDeploymentObservation | None:
        host, token = self._read_provider_config(control_plane_root=control_plane_root)

        def retain_dispatch(known_ids: frozenset[str]) -> None:
            resolved_deploy_target.provider_dispatch_evidence = {
                "deployment_title": deployment_title,
                "target_id": resolved_deploy_target.resolved_target.target_id,
                "known_deployment_ids": sorted(known_ids),
            }
            before_provider_mutation("provider_dispatch_prepared")

        try:
            untitled_deployment = execute_dokploy_artifact_deploy(
                host=host,
                token=token,
                ship_request=resolved_deploy_target.ship_request,
                resolved_target=resolved_deploy_target.resolved_target,
                deploy_timeout_seconds=resolved_deploy_target.deploy_timeout_seconds,
                runtime_identity=runtime_identity,
                deployment_title=deployment_title,
                before_provider_mutation=before_provider_mutation,
                effect_started=effect_started,
                dispatch_evidence_checkpoint=retain_dispatch,
                accept_sole_new_deployment=(
                    resolved_deploy_target.resolved_target.target_type == "compose"
                ),
            )
        except dokploy_api.DokployDeploymentFailed as error:
            if error.deployment is None:
                raise
            try:
                observation = _terminal_deployment_observation(error.deployment)
            except ValueError:
                raise error from None
            try:
                failure = GenericWebProviderDeploymentFailed(observation)
            except ValueError:
                raise error from None
            raise failure from error
        if untitled_deployment is None:
            return None
        try:
            return _terminal_deployment_observation(untitled_deployment)
        except ValueError as error:
            raise click.ClickException(
                "Untitled Dokploy deployment has incomplete terminal evidence."
            ) from error

    def observe_artifact_deploy(
        self,
        *,
        control_plane_root: Path,
        resolved_deploy_target: GenericWebResolvedDeployTarget,
        deployment_title: str,
    ) -> GenericWebProviderDeploymentObservation:
        host, token = self._read_provider_config(control_plane_root=control_plane_root)
        target = resolved_deploy_target.resolved_target
        deployment = dokploy_api.deployment_for_target_by_title(
            host=host,
            token=token,
            target_type=target.target_type,
            target_id=target.target_id,
            title=deployment_title,
        )
        status = dokploy_api.deployment_status(deployment)
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
            deployment_id=dokploy_api.deployment_key(deployment),
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

    def observe_legacy_artifact_deploy(
        self,
        *,
        control_plane_root: Path,
        resolved_deploy_target: GenericWebResolvedDeployTarget,
        provider_effect_started_at: str,
    ) -> GenericWebLegacyDeploymentCorrelation:
        host, token = self._read_provider_config(control_plane_root=control_plane_root)
        target = resolved_deploy_target.resolved_target
        deployments = dokploy_api.list_deployments_for_target(
            host=host,
            token=token,
            target_type=target.target_type,
            target_id=target.target_id,
        )
        target_payload = dokploy_api.fetch_dokploy_target_payload(
            host=host,
            token=token,
            target_type=target.target_type,
            target_id=target.target_id,
        )
        return correlate_legacy_dokploy_deployment(
            deployments=deployments,
            target_payload=target_payload,
            target_type=target.target_type,
            target_id=target.target_id,
            provider_effect_started_at=provider_effect_started_at,
            expected_artifact_reference=dokploy_deploy_artifact_reference(
                ship_request=resolved_deploy_target.ship_request,
                target_type=target.target_type,
            ),
        )

    def observe_testing_no_live_effect(
        self,
        *,
        control_plane_root: Path,
        target: GenericWebProviderReconciliationTarget,
        operation_title: str,
        provider_effect_phase: str,
    ) -> dict[str, object] | None:
        """Read-only proof of absent dispatch or a terminal, correlated dispatch.

        Running-container filters and permissive history parsers cannot prove
        absence: paused/stopped containers, missing lists and malformed provider
        responses are all refusals here.
        """
        if (
            target.instance != "testing"
            or target.target_type != "compose"
            or target.provider_id != self.provider_id
        ):
            return None
        if provider_effect_phase not in {"", "target_update", "deploy_trigger"}:
            raise TestingEventDeployRecoveryRefused("provider_dispatch_absence_unproven")
        host, token = self._read_provider_config(control_plane_root=control_plane_root)
        payload = dokploy_api.fetch_dokploy_target_payload(
            host=host, token=token, target_type="compose", target_id=target.target_id
        )
        app_name = payload.get("appName")
        if (
            payload.get("composeId") != target.target_id
            or not isinstance(app_name, str)
            or not app_name.strip()
            or payload.get("composeType") != "docker-compose"
            or payload.get("composeStatus") not in {"idle", "error", "done"}
        ):
            raise TestingEventDeployRecoveryRefused("provider_target_not_idle_compose")
        schedules = dokploy_api.dokploy_request(
            host=host,
            token=token,
            path="/api/schedule.list",
            query={"id": target.target_id, "scheduleType": "compose"},
        )
        if schedules != []:
            raise TestingEventDeployRecoveryRefused("provider_schedules_unproven_or_present")
        history = dokploy_api.dokploy_request(
            host=host,
            token=token,
            path="/api/deployment.allByType",
            query={"id": target.target_id, "type": "compose"},
        )
        if provider_effect_phase == "deploy_trigger":
            if (
                not isinstance(history, list)
                or not history
                or any(not isinstance(item, dict) for item in history)
            ):
                raise TestingEventDeployRecoveryRefused("provider_deployment_history_unproven")
            deployments = cast(list[dokploy_api.JsonObject], history)
            # Every returned row must have complete terminal evidence. One
            # correlated row alone cannot exclude another queued/running effect.
            try:
                if any(
                    dokploy_api.deployment_status(item) not in {"done", "error"}
                    or item.get("composeId") != target.target_id
                    for item in deployments
                ):
                    raise ValueError("Deployment is not terminal on the original target.")
                observations = [_terminal_deployment_observation(item) for item in deployments]
                for observation in observations:
                    started = parse_launchplane_mutation_timestamp(
                        observation.started_at, field_name="started_at"
                    )
                    finished = parse_launchplane_mutation_timestamp(
                        observation.finished_at, field_name="finished_at"
                    )
                    if finished < started:
                        raise ValueError("Deployment timestamps are out of order.")
            except ValueError as error:
                raise TestingEventDeployRecoveryRefused(
                    "provider_deployments_not_terminal"
                ) from error
            matched = [item for item in deployments if item.get("title") == operation_title]
            if not operation_title or len(matched) != 1:
                raise TestingEventDeployRecoveryRefused("provider_dispatch_correlation_unproven")
            ids = [item.deployment_id for item in observations]
            if len(set(ids)) != len(ids):
                raise TestingEventDeployRecoveryRefused("provider_deployment_history_ambiguous")
        elif history != []:
            raise TestingEventDeployRecoveryRefused(
                "provider_deployment_history_unproven_or_present"
            )
        # A trigger may sit in the queue before any deployment row exists.
        # queueList returns the complete job inventory; HTTP/read errors and
        # unknown shapes are refused. Neither history nor composeStatus alone
        # is negative dispatch evidence.
        jobs = dokploy_api.dokploy_request(host=host, token=token, path="/api/deployment.queueList")
        if not isinstance(jobs, list):
            raise TestingEventDeployRecoveryRefused("provider_queue_unproven")
        for job in jobs:
            if not isinstance(job, dict) or not isinstance(job.get("data"), dict):
                raise TestingEventDeployRecoveryRefused("provider_queue_unproven")
            data = job["data"]
            assert isinstance(data, dict)
            if data.get("composeId") == target.target_id and job.get("state") not in {
                "completed",
                "failed",
            }:
                raise TestingEventDeployRecoveryRefused("provider_dispatch_unproven")
        if provider_effect_phase == "deploy_trigger":
            # This proves the provider job ended, not that it had no effect.
            # Running application containers are expected after a terminal deploy.
            # The caller excludes drivers with post-deploy/data executors and
            # holds the old worker out until atomic failed retirement completes.
            return {
                "state": "quiescent_safe_to_retry",
                "proof": "correlated_terminal_dispatch",
                "deployment_count": len(deployments),
                "schedule_count": 0,
                "pending_target_jobs": 0,
                "operation_marker_sha256": hashlib.sha256(operation_title.encode()).hexdigest(),
                "deployment_evidence_sha256": hashlib.sha256(
                    repr(
                        sorted(
                            (
                                item.deployment_id,
                                item.deployment_status,
                                item.started_at,
                                item.finished_at,
                            )
                            for item in observations
                        )
                    ).encode()
                ).hexdigest(),
            }
        # The name-filtered Dokploy endpoint converts provider/SSH failures to
        # an empty list. A positive host read and inspections are additional
        # safeguards, not a complete inventory proof. The pre-dispatch phase,
        # empty history and queue proof above establish no held-operation effect.
        if "dokploy" in app_name.lower():
            raise TestingEventDeployRecoveryRefused("provider_container_inventory_incomplete")
        query: dict[str, str | int] = {}
        server_id = payload.get("serverId")
        if server_id is not None and not isinstance(server_id, str):
            raise TestingEventDeployRecoveryRefused("provider_server_unproven")
        if isinstance(server_id, str) and server_id.strip():
            query["serverId"] = server_id
        containers = dokploy_api.dokploy_request(
            host=host, token=token, path="/api/docker.getContainers", query=query
        )
        if not isinstance(containers, list) or not containers:
            raise TestingEventDeployRecoveryRefused("provider_container_inventory_unproven")
        for container in containers:
            if not isinstance(container, dict):
                raise TestingEventDeployRecoveryRefused("provider_container_identity_unproven")
            container_id = container.get("containerId")
            if not isinstance(container_id, str) or not container_id:
                raise TestingEventDeployRecoveryRefused("provider_container_identity_unproven")
            inspected = dokploy_api.dokploy_request(
                host=host,
                token=token,
                path="/api/docker.getConfig",
                query={**query, "containerId": container_id},
            )
            if not isinstance(inspected, dict):
                raise TestingEventDeployRecoveryRefused("provider_container_identity_unproven")
            config = inspected.get("Config")
            if not isinstance(config, dict):
                raise TestingEventDeployRecoveryRefused("provider_container_identity_unproven")
            labels = config.get("Labels")
            if labels is not None and not isinstance(labels, dict):
                raise TestingEventDeployRecoveryRefused("provider_container_identity_unproven")
            if isinstance(labels, dict) and labels.get("com.docker.compose.project") == app_name:
                raise TestingEventDeployRecoveryRefused("provider_containers_present")
        # No values or provider coordinates are part of the public audit.
        return {
            "state": "no_live_effect",
            "proof": "not_dispatched",
            "deployment_count": 0,
            "observed_target_container_count": 0,
            "schedule_count": 0,
            "pending_target_jobs": 0,
            "operation_marker_sha256": hashlib.sha256(operation_title.encode()).hexdigest(),
        }

    def observe_runtime_artifact(
        self,
        *,
        control_plane_root: Path,
        resolved_deploy_target: GenericWebResolvedDeployTarget,
    ) -> GenericWebRuntimeArtifactObservation:
        target = resolved_deploy_target.resolved_target
        if target.target_type != "compose":
            raise ValueError("Runtime close-out supports compose targets only.")
        host, token = self._read_provider_config(control_plane_root=control_plane_root)
        target_payload = dokploy_api.fetch_dokploy_target_payload(
            host=host,
            token=token,
            target_type=target.target_type,
            target_id=target.target_id,
        )
        if (
            _dokploy_target_payload_id(target_payload=target_payload, target_type="compose")
            != target.target_id.strip()
        ):
            raise ValueError("Runtime close-out target identity is not exact.")
        try:
            running_containers = dokploy_runtime_evidence.fetch_compose_running_container_runtime(
                host=host,
                token=token,
                app_name=str(target_payload.get("appName") or ""),
                server_id=str(target_payload.get("serverId") or ""),
            )
        except dokploy_runtime_evidence.DokployEvidenceProviderError as error:
            raise click.ClickException("Runtime close-out provider read failed.") from error
        return GenericWebRuntimeArtifactObservation(
            target_artifact_reference=_dokploy_target_artifact_reference(
                target_payload=target_payload,
                target_type="compose",
            ),
            running_container_images=tuple(image for image, _ in running_containers),
            running_container_deployment_record_ids=tuple(
                record_id for _, record_id in running_containers
            ),
        )


def _terminal_deployment_observation(
    deployment: dokploy_api.JsonObject,
) -> GenericWebProviderDeploymentObservation:
    return GenericWebProviderDeploymentObservation(
        outcome="present",
        deployment_status=dokploy_api.deployment_status(deployment),
        deployment_id=dokploy_api.deployment_key(deployment),
        started_at=str(deployment.get("startedAt") or deployment.get("started_at") or "").strip(),
        finished_at=str(
            deployment.get("finishedAt") or deployment.get("finished_at") or ""
        ).strip(),
        error_message=str(
            deployment.get("errorMessage") or deployment.get("error_message") or ""
        ).strip(),
    )


def correlate_legacy_dokploy_deployment(
    *,
    deployments: list[dokploy_api.JsonObject],
    target_payload: dokploy_api.JsonObject,
    target_type: str,
    target_id: str,
    provider_effect_started_at: str,
    expected_artifact_reference: str,
) -> GenericWebLegacyDeploymentCorrelation:
    effect_started_at, _ = _parse_legacy_provider_timestamp(
        provider_effect_started_at,
        field_name="provider_effect_started_at",
    )
    window_started_at = effect_started_at - timedelta(seconds=5)
    window_finished_at = effect_started_at + timedelta(seconds=65)
    parsed_deployments: list[tuple[dokploy_api.JsonObject, datetime, dict[str, str]]] = []
    for deployment in deployments:
        parsed_timestamps = _parse_all_legacy_deployment_timestamps(deployment)
        created_at = _required_legacy_deployment_timestamp(
            parsed_timestamps,
            deployment,
            aliases=("createdAt", "created_at"),
            field_name="created_at",
        )[0]
        parsed_deployments.append((deployment, created_at, parsed_timestamps))
    if not any(created_at < window_started_at for _, created_at, _ in parsed_deployments):
        raise ValueError("Legacy deployment correlation requires older retained history.")
    candidates = [
        parsed
        for parsed in parsed_deployments
        if window_started_at <= parsed[1] <= window_finished_at
    ]
    if len(candidates) != 1:
        raise ValueError("Legacy deployment correlation requires exactly one window candidate.")
    candidate, candidate_created_at, candidate_timestamps = candidates[0]
    if candidate_created_at != max(created_at for _, created_at, _ in parsed_deployments):
        raise ValueError("Legacy deployment correlation candidate is not newest overall.")
    title = _required_legacy_deployment_string(candidate, "title")
    normalized_title = title.casefold()
    if normalized_title.startswith(("launchplane operation ", "launchplane post-deploy ")):
        raise ValueError("Legacy deployment correlation candidate has a Launchplane title.")
    status = dokploy_api.deployment_status(candidate)
    if not generic_web_provider_deployment_succeeded(status):
        raise ValueError("Legacy deployment correlation candidate is not successful.")
    deployment_id = dokploy_api.deployment_key(candidate).strip()
    if not deployment_id:
        raise ValueError("Legacy deployment correlation candidate has no deployment id.")
    started_at, normalized_started_at = _required_legacy_deployment_timestamp(
        candidate_timestamps,
        candidate,
        aliases=("startedAt", "started_at"),
        field_name="started_at",
    )
    finished_at, normalized_finished_at = _required_legacy_deployment_timestamp(
        candidate_timestamps,
        candidate,
        aliases=("finishedAt", "finished_at"),
        field_name="finished_at",
    )
    if finished_at < started_at:
        raise ValueError("Legacy deployment correlation candidate timestamps are inconsistent.")
    normalized_target_id = target_id.strip()
    if (
        not normalized_target_id
        or _dokploy_deployment_target_id(
            deployment=candidate,
            target_type=target_type,
        )
        != normalized_target_id
        or _dokploy_target_payload_id(
            target_payload=target_payload,
            target_type=target_type,
        )
        != normalized_target_id
    ):
        raise ValueError("Legacy deployment correlation target identity is not exact.")
    expected_reference = expected_artifact_reference.strip()
    if (
        not expected_reference
        or _dokploy_target_artifact_reference(
            target_payload=target_payload,
            target_type=target_type,
        )
        != expected_reference
    ):
        raise ValueError("Legacy deployment correlation artifact reference does not match.")
    normalized_created_at = _normalized_legacy_deployment_timestamp(
        candidate_timestamps,
        candidate,
        aliases=("createdAt", "created_at"),
        field_name="created_at",
    )
    return GenericWebLegacyDeploymentCorrelation(
        observation=GenericWebProviderDeploymentObservation(
            outcome="present",
            deployment_status=status,
            deployment_id=deployment_id,
            started_at=normalized_started_at,
            finished_at=normalized_finished_at,
        ),
        digest_evidence=GenericWebLegacyDeploymentCorrelationEvidence(
            deployment_id_sha256=_legacy_correlation_sha256(deployment_id),
            deployment_title_sha256=_legacy_correlation_sha256(title),
            deployment_created_at=normalized_created_at,
            deployment_started_at=normalized_started_at,
            deployment_finished_at=normalized_finished_at,
            artifact_reference_sha256=_legacy_correlation_sha256(expected_reference),
        ),
    )


def _parse_all_legacy_deployment_timestamps(
    deployment: dokploy_api.JsonObject,
) -> dict[str, str]:
    parsed_timestamps: dict[str, str] = {}
    for field_name, value in deployment.items():
        if not (field_name.endswith("At") or field_name.endswith("_at")):
            continue
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            raise ValueError("Legacy deployment timestamps must be strings.")
        _, normalized = _parse_legacy_provider_timestamp(value, field_name=field_name)
        parsed_timestamps[field_name] = normalized
    return parsed_timestamps


def _required_legacy_deployment_timestamp(
    parsed_timestamps: dict[str, str],
    deployment: dokploy_api.JsonObject,
    *,
    aliases: tuple[str, ...],
    field_name: str,
) -> tuple[datetime, str]:
    normalized = _normalized_legacy_deployment_timestamp(
        parsed_timestamps,
        deployment,
        aliases=aliases,
        field_name=field_name,
    )
    return _parse_legacy_provider_timestamp(normalized, field_name=field_name)


def _normalized_legacy_deployment_timestamp(
    parsed_timestamps: dict[str, str],
    deployment: dokploy_api.JsonObject,
    *,
    aliases: tuple[str, ...],
    field_name: str,
) -> str:
    values = {
        parsed_timestamps[alias]
        for alias in aliases
        if alias in deployment and alias in parsed_timestamps
    }
    if len(values) != 1:
        raise ValueError(f"Legacy deployment correlation requires exact {field_name} evidence.")
    return values.pop()


def _parse_legacy_provider_timestamp(value: str, *, field_name: str) -> tuple[datetime, str]:
    utc_timestamp = parse_launchplane_mutation_timestamp(value, field_name=field_name)
    return (
        utc_timestamp,
        utc_timestamp.isoformat(timespec="microseconds").replace("+00:00", "Z"),
    )


def _required_legacy_deployment_string(
    deployment: dokploy_api.JsonObject,
    field_name: str,
) -> str:
    value = deployment.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Legacy deployment correlation requires {field_name}.")
    return value.strip()


def _dokploy_target_payload_id(*, target_payload: dokploy_api.JsonObject, target_type: str) -> str:
    field_name = {"application": "applicationId", "compose": "composeId"}.get(target_type)
    if field_name is None:
        raise ValueError("Legacy deployment correlation target type is unsupported.")
    value = target_payload.get(field_name)
    return value.strip() if isinstance(value, str) else ""


def _dokploy_deployment_target_id(*, deployment: dokploy_api.JsonObject, target_type: str) -> str:
    field_name = {"application": "applicationId", "compose": "composeId"}.get(target_type)
    if field_name is None:
        raise ValueError("Legacy deployment correlation target type is unsupported.")
    value = deployment.get(field_name)
    return value.strip() if isinstance(value, str) else ""


def _dokploy_target_artifact_reference(
    *, target_payload: dokploy_api.JsonObject, target_type: str
) -> str:
    if target_type == "application":
        value = target_payload.get("dockerImage")
        return value.strip() if isinstance(value, str) else ""
    if target_type == "compose":
        raw_env = target_payload.get("env")
        if not isinstance(raw_env, str):
            return ""
        references: list[str] = []
        for raw_line in raw_env.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            key, separator, value = line.partition("=")
            if separator and key.strip() == "DOCKER_IMAGE_REFERENCE":
                references.append(value.strip())
        return references[0] if len(references) == 1 else ""
    raise ValueError("Legacy deployment correlation target type is unsupported.")


def _legacy_correlation_sha256(value: str) -> str:
    return hashlib.sha256(value.strip().encode()).hexdigest()


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
) -> dokploy_source.DokployTargetDefinition:
    if target_record.context != target_id_record.context:
        raise click.ClickException("Dokploy target context must match target-id context.")
    if target_record.instance != target_id_record.instance:
        raise click.ClickException("Dokploy target instance must match target-id instance.")
    return dokploy_source.DokployTargetDefinition(
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
        public_hosts=target_record.public_hosts,
        policies=target_record.policies,
    )


def _dokploy_target_type_from_provider_target(
    *, provider_target: ProviderTargetRecord
) -> DeployTargetCompatibilityType:
    if provider_target.provider_target_type == "application":
        return "application"
    if provider_target.provider_target_type == "compose":
        return "compose"
    raise click.ClickException(
        "Provider-target type mismatch for "
        f"{provider_target.context}/{provider_target.instance}: "
        "Dokploy execution only supports provider-target type 'application' or 'compose'."
    )


def _validate_dokploy_provider_target(
    *,
    provider_target: ProviderTargetRecord,
    target_definition: dokploy_source.DokployTargetDefinition,
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
