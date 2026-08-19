import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

import click

from control_plane.contracts.generic_web_deploy_recovery import (
    GenericWebDeployProviderEvidenceClassification,
    GenericWebDeployProviderReadErrorClass,
)
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
    GenericWebDeployLegacyCorrelationProvider,
    GenericWebDeployProvider,
    GenericWebLegacyDeploymentCorrelation,
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


def _string_field(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    return value.strip() if isinstance(value, str) else ""


class _DeploymentRecordReader(Protocol):
    def read_deployment_record(self, record_id: str) -> object: ...


@dataclass(frozen=True, slots=True)
class _GenericWebDeployProviderInspection:
    observation: GenericWebProviderDeploymentObservation
    resolved_deploy_target: GenericWebResolvedDeployTarget | None = None
    retry_safe: bool = False
    identity_matches: bool = True
    post_deploy_unobserved: bool = False
    provider_evidence: GenericWebDeployProviderEvidenceClassification = "not_inspected"
    provider_read_error_class: GenericWebDeployProviderReadErrorClass = ""
    legacy_correlation_digest_evidence: dict[str, object] | None = None


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
        return self.provider_observation_from_inspection(
            provider_operation_key=provider_operation_key,
            inspection=inspection,
        )

    def provider_observation_from_inspection(
        self,
        *,
        provider_operation_key: str,
        inspection: _GenericWebDeployProviderInspection,
    ) -> ProviderObservation:
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
        terminal_failure = _string_field(driver_result, "deploy_status") == "fail"
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
        legacy_provider_effect_started_at: str = "",
    ) -> _GenericWebDeployProviderInspection:
        evidence = self.inspect_evidence(
            provider_operation_key=provider_operation_key,
            provider_effect_phase=provider_effect_phase,
            reconciliation_key=reconciliation_key,
            expected_provider_target_key=expected_provider_target_key,
        )
        if not evidence.identity_matches:
            return evidence
        observation = evidence.observation
        resolved_target = evidence.resolved_deploy_target
        if observation.outcome != "present":
            if observation.outcome == "absent" and provider_effect_phase == "deploy_trigger":
                (
                    legacy_correlation,
                    legacy_provider_evidence,
                    legacy_read_error_class,
                ) = self._inspect_legacy_artifact_deploy(
                    resolved_target=resolved_target,
                    provider_effect_started_at=legacy_provider_effect_started_at,
                )
                if legacy_correlation is None:
                    return _GenericWebDeployProviderInspection(
                        observation=GenericWebProviderDeploymentObservation(outcome="unknown"),
                        resolved_deploy_target=resolved_target,
                        provider_evidence=legacy_provider_evidence,
                        provider_read_error_class=legacy_read_error_class,
                    )
                observation = legacy_correlation.observation
                evidence = _GenericWebDeployProviderInspection(
                    observation=observation,
                    resolved_deploy_target=resolved_target,
                    provider_evidence="deployment_correlated_legacy",
                    legacy_correlation_digest_evidence=(
                        legacy_correlation.digest_evidence.model_dump(mode="json")
                    ),
                )
            else:
                return _GenericWebDeployProviderInspection(
                    observation=observation,
                    resolved_deploy_target=resolved_target,
                    retry_safe=(
                        observation.outcome == "absent"
                        and provider_effect_phase in {"", "target_update"}
                    ),
                    provider_evidence=evidence.provider_evidence,
                    provider_read_error_class=evidence.provider_read_error_class,
                )
        if resolved_target is None:
            return _GenericWebDeployProviderInspection(
                observation=GenericWebProviderDeploymentObservation(outcome="unknown"),
                provider_evidence="provider_read_failed",
                provider_read_error_class="target_resolution_failed",
            )
        post_deploy_unobserved = (
            generic_web_provider_deployment_succeeded(observation.deployment_status)
            and generic_web_post_deploy_executor_for_driver_id(self._profile.driver_id) is not None
        )
        deployment_record_id = self._deployment_record_id(provider_operation_key)
        if provider_effect_phase.startswith("post_deploy_"):
            read_deployment_record_value = getattr(
                self._record_store, "read_deployment_record", None
            )
            if not callable(read_deployment_record_value):
                return _GenericWebDeployProviderInspection(
                    observation=GenericWebProviderDeploymentObservation(outcome="unknown"),
                    provider_evidence=evidence.provider_evidence,
                )
            record_reader = cast(_DeploymentRecordReader, self._record_store)
            try:
                record_reader.read_deployment_record(deployment_record_id)
            except FileNotFoundError:
                return _GenericWebDeployProviderInspection(
                    observation=GenericWebProviderDeploymentObservation(outcome="unknown"),
                    provider_evidence=evidence.provider_evidence,
                )
            post_deploy_unobserved = False
        return _GenericWebDeployProviderInspection(
            observation=observation,
            resolved_deploy_target=resolved_target,
            post_deploy_unobserved=post_deploy_unobserved,
            provider_evidence=evidence.provider_evidence,
            provider_read_error_class=evidence.provider_read_error_class,
            legacy_correlation_digest_evidence=evidence.legacy_correlation_digest_evidence,
        )

    def _inspect_legacy_artifact_deploy(
        self,
        *,
        resolved_target: GenericWebResolvedDeployTarget | None,
        provider_effect_started_at: str,
    ) -> tuple[
        GenericWebLegacyDeploymentCorrelation | None,
        GenericWebDeployProviderEvidenceClassification,
        GenericWebDeployProviderReadErrorClass,
    ]:
        normalized_effect_started_at = provider_effect_started_at.strip()
        if resolved_target is None or not normalized_effect_started_at:
            return None, "deployment_absent_after_effect", ""
        if not isinstance(
            self._deploy_provider,
            GenericWebDeployLegacyCorrelationProvider,
        ):
            return None, "deployment_absent_after_effect", ""
        try:
            return (
                self._deploy_provider.observe_legacy_artifact_deploy(
                    control_plane_root=self._control_plane_root,
                    resolved_deploy_target=resolved_target,
                    provider_effect_started_at=normalized_effect_started_at,
                ),
                "deployment_correlated_legacy",
                "",
            )
        except ValueError:
            return None, "provider_status_unknown", ""
        except (FileNotFoundError, click.ClickException):
            return None, "provider_read_failed", "provider_request_failed"

    def inspect_evidence(
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
                    provider_evidence="provider_read_failed",
                    provider_read_error_class="target_resolution_failed",
                )
            self._resolved_deploy_target = resolved_target
        except (FileNotFoundError, ValueError, click.ClickException):
            return _GenericWebDeployProviderInspection(
                observation=GenericWebProviderDeploymentObservation(outcome="unknown"),
                provider_evidence="provider_read_failed",
                provider_read_error_class="target_resolution_failed",
            )
        try:
            observation = self._deploy_provider.observe_artifact_deploy(
                control_plane_root=self._control_plane_root,
                resolved_deploy_target=resolved_target,
                deployment_title=provider_operation_title(provider_operation_key),
            )
        except (FileNotFoundError, ValueError, click.ClickException):
            return _GenericWebDeployProviderInspection(
                observation=GenericWebProviderDeploymentObservation(outcome="unknown"),
                resolved_deploy_target=resolved_target,
                provider_evidence="provider_read_failed",
                provider_read_error_class="provider_request_failed",
            )
        if observation.outcome == "present":
            provider_evidence: GenericWebDeployProviderEvidenceClassification = "deployment_present"
        elif observation.outcome == "absent":
            provider_evidence = (
                "deployment_absent_before_effect"
                if provider_effect_phase in {"", "target_update"}
                else "deployment_absent_after_effect"
            )
        else:
            provider_evidence = "provider_status_unknown"
        return _GenericWebDeployProviderInspection(
            observation=observation,
            resolved_deploy_target=resolved_target,
            provider_evidence=provider_evidence,
        )

    def _deployment_record_id(self, provider_operation_key: str) -> str:
        digest = hashlib.sha256()
        digest.update(provider_operation_key.encode())
        operation_digest = digest.hexdigest()[:24]
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
        if _string_field(result, "deploy_status") == "fail" and provider_effect_attempted:
            raise ProviderMutationUnknownError(
                _string_field(result, "error_message")
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
