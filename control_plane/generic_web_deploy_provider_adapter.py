import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import click

from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.generic_web_deploy_http import (
    GenericWebDeployEnvelope,
    execute_generic_web_deploy_result,
    should_store_generic_web_deploy_idempotency,
)
from control_plane.provider_operations import (
    ProviderMutationOutcome,
    ProviderMutationRejectedError,
    ProviderMutationUnknownError,
    ProviderObservation,
    ProviderOperationLease,
    provider_operation_response_payload,
    provider_operation_title,
)
from control_plane.workflows.generic_web_deploy import (
    GenericWebDeployStore,
    normalize_generic_web_artifact_id,
    record_observed_generic_web_deploy,
)
from control_plane.workflows.generic_web_deploy_provider import (
    GenericWebDeployProvider,
    GenericWebProviderDeploymentObservation,
    GenericWebResolvedDeployTarget,
    build_generic_web_provider_reconciliation_key,
    build_generic_web_provider_target_key,
    default_generic_web_deploy_provider,
    generic_web_provider_deployment_succeeded,
    resolve_generic_web_provider_reconciliation_target,
)
from control_plane.workflows.odoo_generic_web_post_deploy import (
    generic_web_post_deploy_executor_for_driver_id,
)


__all__ = [
    "_GenericWebDeployProviderInspection",
    "GenericWebDeployProviderMutationAdapter",
]


@dataclass(frozen=True, slots=True)
class _GenericWebDeployProviderInspection:
    observation: GenericWebProviderDeploymentObservation
    resolved_deploy_target: GenericWebResolvedDeployTarget | None = None
    retry_safe: bool = False
    identity_matches: bool = True
    post_deploy_unobserved: bool = False


class GenericWebDeployProviderMutationAdapter:
    def __init__(
        self,
        *,
        control_plane_root: Path,
        record_store: object,
        deploy_request: GenericWebDeployEnvelope,
        profile: LaunchplaneProductProfileRecord,
        lane: ProductLaneProfile,
        trace_id: str,
    ) -> None:
        self._control_plane_root = control_plane_root
        self._record_store = record_store
        self._deploy_request = deploy_request
        self._profile = profile
        self._lane = lane
        self._trace_id = trace_id
        self._deploy_provider: GenericWebDeployProvider = default_generic_web_deploy_provider()
        self._resolved_deploy_target: GenericWebResolvedDeployTarget | None = None

    def _resolve_deploy_target(self) -> GenericWebResolvedDeployTarget:
        if self._resolved_deploy_target is None:
            deploy = self._deploy_request.deploy
            self._resolved_deploy_target = self._deploy_provider.resolve_deploy_target(
                control_plane_root=self._control_plane_root,
                request_artifact_id=deploy.artifact_id,
                request_source_git_ref=deploy.source_git_ref,
                request_timeout_seconds=deploy.timeout_seconds,
                request_no_cache=deploy.no_cache,
                record_store=self._record_store,
                profile=self._profile,
                lane=self._lane,
                normalized_artifact_id=normalize_generic_web_artifact_id(
                    profile=self._profile,
                    artifact_id=deploy.artifact_id,
                ),
                request_deploy_reference=deploy.deploy_reference,
                fallback_target_name=f"{self._profile.product}-{self._lane.instance}",
            )
        return self._resolved_deploy_target

    def reconciliation_key(self) -> str:
        return build_generic_web_provider_reconciliation_key(
            self._resolve_deploy_target(),
            product=self._profile.product,
        )

    def target_key(self) -> str:
        return build_generic_web_provider_target_key(self._resolve_deploy_target())

    def observe(
        self,
        provider_operation_key: str,
        provider_effect_phase: str,
        reconciliation_key: str,
    ) -> ProviderObservation:
        inspection = self.inspect(
            provider_operation_key=provider_operation_key,
            provider_effect_phase=provider_effect_phase,
            reconciliation_key=reconciliation_key,
        )
        observation = inspection.observation
        if observation.outcome != "present" or inspection.resolved_deploy_target is None:
            return ProviderObservation(
                outcome=observation.outcome,
                retry_safe=inspection.retry_safe,
            )
        resolved_target = inspection.resolved_deploy_target
        deployment_record_id = self._deployment_record_id(provider_operation_key)
        try:
            records, driver_result = record_observed_generic_web_deploy(
                record_store=cast(GenericWebDeployStore, self._record_store),
                profile=self._profile,
                lane=self._lane,
                resolved_deploy_target=resolved_target,
                observation=observation,
                deployment_record_id=deployment_record_id,
                post_deploy_unobserved=inspection.post_deploy_unobserved,
            )
        except (FileNotFoundError, ValueError, click.ClickException):
            return ProviderObservation(outcome="unknown")
        terminal_failure = str(driver_result.get("deploy_status", "")).strip() == "fail"
        return ProviderObservation(
            outcome="present",
            response_status_code=502 if terminal_failure else 202,
            response_payload=provider_operation_response_payload(
                trace_id=self._trace_id,
                records=records,
                result=driver_result,
            ),
        )

    def inspect(
        self,
        *,
        provider_operation_key: str,
        provider_effect_phase: str,
        reconciliation_key: str,
        expected_provider_target_key: str = "",
    ) -> _GenericWebDeployProviderInspection:
        try:
            deploy = self._deploy_request.deploy
            resolved_target = resolve_generic_web_provider_reconciliation_target(
                reconciliation_key=reconciliation_key,
                request_artifact_id=deploy.artifact_id,
                request_source_git_ref=deploy.source_git_ref,
                request_timeout_seconds=deploy.timeout_seconds,
                request_no_cache=deploy.no_cache,
                normalized_artifact_id=normalize_generic_web_artifact_id(
                    profile=self._profile,
                    artifact_id=deploy.artifact_id,
                ),
                request_deploy_reference=deploy.deploy_reference,
                lane=self._lane,
            )
            if (
                expected_provider_target_key.strip()
                and build_generic_web_provider_target_key(resolved_target)
                != expected_provider_target_key.strip()
            ):
                return _GenericWebDeployProviderInspection(
                    observation=GenericWebProviderDeploymentObservation(outcome="unknown"),
                    identity_matches=False,
                )
            self._resolved_deploy_target = resolved_target
            observation = self._deploy_provider.observe_artifact_deploy(
                control_plane_root=self._control_plane_root,
                resolved_deploy_target=resolved_target,
                deployment_title=provider_operation_title(provider_operation_key),
            )
        except (FileNotFoundError, ValueError, click.ClickException):
            return _GenericWebDeployProviderInspection(
                observation=GenericWebProviderDeploymentObservation(outcome="unknown")
            )
        if observation.outcome != "present":
            if observation.outcome == "absent" and provider_effect_phase == "deploy_trigger":
                return _GenericWebDeployProviderInspection(
                    observation=GenericWebProviderDeploymentObservation(outcome="unknown")
                )
            return _GenericWebDeployProviderInspection(
                observation=observation,
                retry_safe=(
                    observation.outcome == "absent"
                    and provider_effect_phase in {"", "target_update"}
                ),
            )
        post_deploy_unobserved = (
            generic_web_provider_deployment_succeeded(observation.deployment_status)
            and generic_web_post_deploy_executor_for_driver_id(self._profile.driver_id) is not None
        )
        deployment_record_id = self._deployment_record_id(provider_operation_key)
        if provider_effect_phase.startswith("post_deploy_"):
            read_deployment_record = getattr(self._record_store, "read_deployment_record", None)
            if not callable(read_deployment_record):
                return _GenericWebDeployProviderInspection(
                    observation=GenericWebProviderDeploymentObservation(outcome="unknown")
                )
            try:
                read_deployment_record(deployment_record_id)
            except FileNotFoundError:
                return _GenericWebDeployProviderInspection(
                    observation=GenericWebProviderDeploymentObservation(outcome="unknown")
                )
            post_deploy_unobserved = False
        return _GenericWebDeployProviderInspection(
            observation=observation,
            resolved_deploy_target=resolved_target,
            post_deploy_unobserved=post_deploy_unobserved,
        )

    def _deployment_record_id(self, provider_operation_key: str) -> str:
        operation_digest = hashlib.sha256(provider_operation_key.encode("utf-8")).hexdigest()[:24]
        return (
            f"deployment-provider-operation-{operation_digest}-"
            f"{self._lane.context}-{self._lane.instance}"
        )

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        try:
            records, result = execute_generic_web_deploy_result(
                control_plane_root=self._control_plane_root,
                record_store=self._record_store,
                request=self._deploy_request,
                profile=self._profile,
                lane=self._lane,
                provider_operation_title=provider_operation_title(provider_operation_key),
                deployment_record_id=self._deployment_record_id(provider_operation_key),
                deploy_provider=self._deploy_provider,
                resolved_deploy_target=self._resolve_deploy_target(),
                provider_effect_checkpoint=lease.checkpoint_effect,
            )
        except (FileNotFoundError, ValueError) as error:
            raise ProviderMutationRejectedError(error)
        except click.ClickException as error:
            raise ProviderMutationUnknownError(str(error)) from error
        provider_effect_attempted = result.pop("provider_effect_attempted", False) is True
        if str(result.get("deploy_status", "")).strip() == "fail" and provider_effect_attempted:
            raise ProviderMutationUnknownError(
                str(result.get("error_message", "")).strip()
                or "Generic web provider outcome requires reconciliation."
            )
        return ProviderMutationOutcome(
            response_status_code=202,
            response_payload=provider_operation_response_payload(
                trace_id=self._trace_id,
                records=records,
                result=result,
            ),
            durable=should_store_generic_web_deploy_idempotency(result),
            provider_effect_performed=provider_effect_attempted,
        )
