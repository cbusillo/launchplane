from collections.abc import Callable
from control_plane.contracts.provider_target_origin import ProviderTargetOrigin

from dataclasses import dataclass
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from typing import Protocol, cast

import click
from sqlalchemy.exc import SQLAlchemyError

from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.deployment_record import deployment_record_passed
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.disposable_lanes import (
    DisposableLaneSafetyUnknown,
    DisposableLaneStore,
    require_disposable_lane,
    resolve_disposable_observation_binding,
)
from control_plane.generic_web_testing_inputs import (
    TESTING_DISPATCH_REFUSED_PHASE,
    testing_input_authority,
)
from control_plane.generic_web_testing_settlement import (
    TESTING_DISPATCH_REFUSED_SETTLED,
    TESTING_INERT_INVENTORY_REFUSAL_SETTLED,
    TESTING_PRE_IMAGE_SETTLED,
    TESTING_POST_IMAGE_SETTLEMENT_PHASE,
    settle_testing_pre_image_attempt,
    testing_pre_image_settlement_required,
)
from control_plane.workflows.generic_web_application_snapshot import (
    ApplicationLifecycleBaselineChanged,
    has_recorded_application_lifecycle,
    generic_web_provider_deployment_record_id,
    recorded_application_lifecycle,
)
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
    resolve_generic_web_deploy_lane,
)
from control_plane.provider_operations import (
    ProviderEvidenceLease,
    ProviderMutationOutcome,
    ProviderMutationRejectedError,
    ProviderMutationUnknownError,
    ProviderObservation,
    ProviderOperationLease,
    provider_operation_response_payload,
    provider_operation_title,
    build_provider_operation_key,
)
from control_plane.workflows.generic_web_execution import (
    EXECUTION_EVIDENCE_KEY,
    ExecutionReceipt,
    ExecutionRecorder,
    ExecutionObservation,
    execution_receipt,
    observe_execution,
    require_recorded_execution,
    evidence_digest,
)
from control_plane.dokploy.api import JsonObject
from control_plane.workflows.generic_web_deploy import (
    GenericWebDeployStore,
    GENERIC_WEB_UNOBSERVED_DEPLOYMENT_ID,
    normalize_generic_web_artifact_id,
    record_observed_generic_web_deploy,
    build_observed_generic_web_deploy,
    generic_web_record_result,
)
from control_plane.workflows.inventory import build_environment_inventory
from control_plane.workflows.generic_web_deploy_provider import (
    GenericWebDeployLegacyCorrelationProvider,
    GenericWebApplicationExecutionProvider,
    GenericWebDeployProvider,
    GenericWebDeployRuntimeArtifactProvider,
    GenericWebLegacyDeploymentCorrelation,
    GenericWebProviderDeploymentObservation,
    GenericWebProviderDeploymentFailed,
    GenericWebResolvedDeployTarget,
    GenericWebRuntimeCloseOutEvidence,
    build_generic_web_provider_reconciliation_key,
    decode_generic_web_provider_reconciliation_target,
    build_generic_web_provider_target_key,
    default_generic_web_deploy_provider,
    evaluate_generic_web_runtime_close_out,
    generic_web_provider_deployment_succeeded,
    qualify_testing_application_observation,
    resolve_generic_web_provider_reconciliation_target,
)
from control_plane.workflows.generic_web_post_deploy import (
    generic_web_post_deploy_authority_digest,
    generic_web_post_deploy_executor_for_profile,
)
from control_plane.workflows.ship import utc_now_timestamp


__all__ = [
    "_GenericWebDeployProviderInspection",
    "GenericWebDeployProviderMutationAdapter",
]


def _string_field(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    return value.strip() if isinstance(value, str) else ""


class _TestingInventoryBaselineChanged(ApplicationLifecycleBaselineChanged):
    pass


class _DeploymentRecordReader(Protocol):
    def read_deployment_record(self, record_id: str) -> DeploymentRecord: ...


class _ProviderTargetReader(Protocol):
    def read_provider_target_record(
        self, *, context_name: str, instance_name: str
    ) -> ProviderTargetRecord: ...


class _ExecutionInventoryReader(Protocol):
    def read_environment_inventory(
        self, *, context_name: str, instance_name: str
    ) -> EnvironmentInventory: ...


class _TestingResultPublisher(Protocol):
    def publish_generic_web_testing_result(
        self,
        *,
        reservation: LaunchplaneIdempotencyRecord,
        expected_deployment: DeploymentRecord | None,
        expected_inventory: EnvironmentInventory | None,
        deployment: DeploymentRecord,
        inventory: EnvironmentInventory | None,
    ) -> None: ...


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
    execution: ExecutionObservation | None = None
    execution_record: DeploymentRecord | None = None
    execution_inventory: EnvironmentInventory | None = None


def _saved_testing_result_is_final(record: DeploymentRecord | None) -> bool:
    return (
        record is not None
        and record.deploy.status in {"pass", "fail"}
        and record.deploy.deployment_id != GENERIC_WEB_UNOBSERVED_DEPLOYMENT_ID
        and (
            record.deploy.status == "fail"
            or record.post_deploy_update.status == "fail"
            or record.destination_health.status == "fail"
            or (
                record.post_deploy_update.status in {"pass", "skipped"}
                and record.destination_health.status in {"pass", "skipped"}
            )
        )
    )


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
        deploy_provider: GenericWebDeployProvider | None = None,
        recorded_artifact: bool = False,
        retain_event_request: bool = False,
        post_deploy_authority_digest: str = "",
        superseded_testing_candidate: bool = False,
        standing_testing_permission: bool = False,
        complete_inert_inventory_refusal: bool = False,
    ) -> None:
        # Recovery replays a deploy Launchplane reserved; its artifact is a record.
        self._recorded_artifact = recorded_artifact
        self._retain_event_request = retain_event_request
        self._control_plane_root = control_plane_root
        self._record_store = cast(GenericWebDeployStore, record_store)
        self._deploy_request = deploy_request
        self._profile = profile
        self._lane = lane
        self._trace_id = trace_id
        self._deploy_provider: GenericWebDeployProvider = (
            deploy_provider
            or default_generic_web_deploy_provider(
                record_store=record_store, control_plane_root=control_plane_root
            )
        )
        self._resolved_deploy_target: GenericWebResolvedDeployTarget | None = None
        self._qualification_authority_digest = post_deploy_authority_digest
        self._reconciliation_qualification_digest: str | None = None
        self._proved_pre_image_operation = ""
        self._superseded_testing_candidate = superseded_testing_candidate
        self._settlement_operation = ""
        self._settlement_phase = ""
        self._settlement_no_effect_checkpoint = False
        self._standing_testing_permission = standing_testing_permission
        self._complete_inert_inventory_refusal = complete_inert_inventory_refusal
        self._standing_effect_started = False
        self._pre_effect_inventory_digest: str | None = None
        self._standing_binding_sha256 = ""
        self.execution_check: Callable[[], None] = lambda: None

    @property
    def profile(self) -> LaunchplaneProductProfileRecord:
        return self._profile

    @property
    def lane(self) -> ProductLaneProfile:
        return self._lane

    def _application_testing(self, target: GenericWebResolvedDeployTarget) -> bool:
        return (
            self._lane.instance == "testing"
            and self._lane.generic_web_cutover is None
            and target.resolved_target.target_type == "application"
        )

    def _execution_recorder(self, operation_key: str) -> ExecutionRecorder:
        record_id = self._deployment_record_id(operation_key)
        try:
            record = self._record_store.read_deployment_record(record_id)
            receipt = execution_receipt(record)
        except FileNotFoundError:
            record = None
            receipt = None
        if receipt is None:
            try:
                inventory = cast(
                    _ExecutionInventoryReader, self._record_store
                ).read_environment_inventory(
                    context_name=self._lane.context, instance_name=self._lane.instance
                )
            except FileNotFoundError:
                inventory = None
            receipt = ExecutionReceipt(
                inventory_baseline_digest=evidence_digest(
                    inventory.model_dump(mode="json") if inventory else None
                )
            )
        return ExecutionRecorder(self._record_store, record_id, receipt, expected_record=record)

    def _execution_evidence(
        self, operation_key: str, target: GenericWebResolvedDeployTarget, phase: str
    ) -> tuple[ExecutionObservation | None, DeploymentRecord | None]:
        if not self._application_testing(target):
            return None, None
        try:
            record = self._record_store.read_deployment_record(
                self._deployment_record_id(operation_key)
            )
        except FileNotFoundError:
            if phase.startswith(("application_", "post_deploy_")) or phase in {
                "records_commit",
                TESTING_POST_IMAGE_SETTLEMENT_PHASE,
            }:
                return ExecutionObservation("unknown", {"receipt": "missing"}), None
            return None, None
        ship = target.ship_request
        if (
            record.context,
            record.instance,
            record.source_git_ref,
            record.artifact_identity.artifact_id if record.artifact_identity else "",
            record.resolved_target,
        ) != (
            self._lane.context,
            self._lane.instance,
            ship.source_git_ref,
            ship.artifact_id,
            target.resolved_target,
        ):
            return ExecutionObservation("unknown", {"binding": "changed"}), record
        if (
            record.deployed_target is None
            or record.deployed_target.provider_id != self._deploy_provider.provider_id
            or record.deployed_target.target_id != target.resolved_target.target_id
        ):
            return ExecutionObservation("unknown", {"binding": "changed"}), record
        identity = record.runtime_identity
        if identity is not None and (
            identity.product,
            identity.context,
            identity.instance,
            identity.deployment_record_id,
            identity.artifact_id,
            identity.source_git_ref,
        ) != (
            self._profile.product,
            self._lane.context,
            self._lane.instance,
            record.record_id,
            ship.artifact_id,
            ship.source_git_ref,
        ):
            return ExecutionObservation("unknown", {"binding": "changed"}), record
        try:
            lifecycle = recorded_application_lifecycle(record)
            if lifecycle is not None and (
                lifecycle.product,
                lifecycle.provider_id,
                lifecycle.target_id,
            ) != (
                self._profile.product,
                self._deploy_provider.provider_id,
                target.resolved_target.target_id,
            ):
                return ExecutionObservation("unknown", {"binding": "changed"}), record
            receipt = execution_receipt(record)
            if receipt is None:
                if (
                    lifecycle is not None
                    or self._lane.generic_web_extensions.configured
                    or generic_web_post_deploy_executor_for_profile(
                        self._profile, instance=self._lane.instance
                    )
                    is not None
                    or phase.startswith(("application_", "post_deploy_"))
                    or phase == "records_commit"
                ):
                    return ExecutionObservation("unknown", {"receipt": "missing"}), record
                return None, record

            def read_history(schedule_id: str) -> tuple[JsonObject, ...]:
                if not isinstance(self._deploy_provider, GenericWebApplicationExecutionProvider):
                    raise ValueError("Provider cannot observe the original application execution.")
                return self._deploy_provider.application_execution_history(
                    control_plane_root=self._control_plane_root,
                    application_id=target.resolved_target.target_id,
                    schedule_id=schedule_id,
                )

            result = observe_execution(receipt, read_history)
            return ExecutionObservation(
                result.outcome,
                {
                    **result.digest_evidence,
                    "baseline": lifecycle.model_dump(mode="json") if lifecycle else None,
                    "authority": self._reconciliation_qualification_digest,
                    "testing_authority": target.testing_runtime_digest,
                },
            ), record
        except (ValueError, click.ClickException):
            return ExecutionObservation("unknown", {"receipt": "invalid"}), record

    @property
    def testing_settlement_pending(self) -> bool:
        return bool(self._settlement_operation)

    @property
    def testing_settlement_disposition(self) -> str:
        if not self.testing_settlement_pending:
            return ""
        return (
            TESTING_DISPATCH_REFUSED_SETTLED
            if self._settlement_phase == TESTING_DISPATCH_REFUSED_PHASE
            else TESTING_PRE_IMAGE_SETTLED
        )

    def _resolve_deploy_target(self) -> GenericWebResolvedDeployTarget:
        if self._resolved_deploy_target is None:
            if self._standing_testing_permission:
                self._standing_binding_sha256 = self._resolve_standing_testing_binding()
            if self._lane.generic_web_cutover is not None:
                from control_plane.generic_web_cutover import (
                    WebCutoverAuthorityStore,
                    resolve_web_cutover_target,
                )

                deploy = self._deploy_request.deploy
                self._resolved_deploy_target = resolve_web_cutover_target(
                    cast(WebCutoverAuthorityStore, self._record_store),
                    profile=self._profile,
                    lane=self._lane,
                    artifact_id=deploy.artifact_id,
                    source_git_ref=deploy.source_git_ref,
                    deploy_reference=deploy.deploy_reference,
                    provider_id=self._deploy_provider.provider_id,
                    timeout_seconds=deploy.timeout_seconds,
                )
                return self._resolved_deploy_target
            if not self._qualification_authority_digest:
                self._qualification_authority_digest = generic_web_post_deploy_authority_digest(
                    self._record_store, self._profile, self._lane
                )
            if self._reconciliation_qualification_digest is None:
                self._reconciliation_qualification_digest = self._qualification_authority_digest
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
                    recorded=self._recorded_artifact,
                ),
                request_deploy_reference=deploy.deploy_reference,
                fallback_target_name=f"{self._profile.product}-{self._lane.instance}",
            )
        return self._resolved_deploy_target

    def reconciliation_key(self) -> str:
        return build_generic_web_provider_reconciliation_key(
            self._resolve_deploy_target(),
            product=self._profile.product,
            original_event_deploy=self._deploy_request.model_dump(mode="json")
            if self._retain_event_request
            else None,
            qualification_authority_digest=self._reconciliation_qualification_digest or "",
        )

    def provider_target_origin(self) -> ProviderTargetOrigin:
        resolved = self._resolve_deploy_target()
        return ProviderTargetOrigin(
            product=self._profile.product,
            context=resolved.ship_request.context,
            instance=resolved.ship_request.instance,
            provider_id=resolved.ship_request.provider_id,
            target_type=resolved.resolved_target.target_type,
            target_id=resolved.resolved_target.target_id,
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
            provider_operation_key=provider_operation_key, inspection=inspection
        )

    def observe_with_effect_started_at(
        self,
        provider_operation_key: str,
        provider_effect_phase: str,
        reconciliation_key: str,
        provider_effect_started_at: str,
    ) -> ProviderObservation:
        inspection = self.inspect(
            provider_operation_key=provider_operation_key,
            provider_effect_phase=provider_effect_phase,
            reconciliation_key=reconciliation_key,
            legacy_provider_effect_started_at=provider_effect_started_at,
            no_effect_checkpoint=not provider_effect_phase and not provider_effect_started_at,
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
        if self._lane.generic_web_cutover is not None:
            if observation.outcome != "present":
                return ProviderObservation(
                    outcome=observation.outcome, retry_safe=inspection.retry_safe
                )
            from control_plane.workflows.generic_web_deploy import cutover_deploy_result

            record = cast(_DeploymentRecordReader, self._record_store).read_deployment_record(
                self._deployment_record_id(provider_operation_key)
            )
            result = cutover_deploy_result(
                record, product=self._profile.product, provider_id=self._deploy_provider.provider_id
            ).model_dump(mode="json")
            result.pop("provider_effect_attempted", None)
            result.pop("provider_failure_proved", None)
            return ProviderObservation(
                outcome="present",
                response_status_code=202,
                response_payload=provider_operation_response_payload(
                    trace_id=self._trace_id,
                    records={"deployment_record_id": record.record_id},
                    result=result,
                ),
            )
        if observation.outcome != "present" or inspection.resolved_deploy_target is None:
            return ProviderObservation(
                outcome=observation.outcome,
                retry_safe=inspection.retry_safe,
                preserve_effect_checkpoint=bool(
                    inspection.retry_safe
                    and inspection.resolved_deploy_target is not None
                    and inspection.resolved_deploy_target.testing_runtime_digest
                ),
            )
        if self._application_testing(inspection.resolved_deploy_target):
            saved_record = inspection.execution_record
            if (
                (inspection.execution is not None and inspection.execution.outcome != "terminal")
                or inspection.post_deploy_unobserved
                or saved_record is None
            ):
                return ProviderObservation(outcome="unknown")
            try:
                inventory = cast(
                    _ExecutionInventoryReader, self._record_store
                ).read_environment_inventory(
                    context_name=saved_record.context, instance_name=saved_record.instance
                )
            except FileNotFoundError:
                return ProviderObservation(outcome="unknown")
            expected = build_environment_inventory(
                deployment_record=saved_record, updated_at=inventory.updated_at
            )
            if (
                inventory != expected
                or saved_record.deploy.status != "pass"
                or saved_record.post_deploy_update.status not in {"pass", "skipped"}
                or not deployment_record_passed(saved_record)
                or saved_record.runtime_identity is None
                or saved_record.deploy.deployment_id != observation.deployment_id
            ):
                return ProviderObservation(outcome="unknown")
            records, result = generic_web_record_result(
                record=saved_record,
                product=self._profile.product,
                target=inspection.resolved_deploy_target,
                observation=observation,
            )
            return ProviderObservation(
                outcome="present",
                response_payload=provider_operation_response_payload(
                    trace_id=self._trace_id, records=records, result=result
                ),
                expected_binding_evidence=(saved_record, inventory),
            )
        resolved_target = inspection.resolved_deploy_target
        deployment_record_id = self._deployment_record_id(provider_operation_key)
        try:
            records, driver_result = record_observed_generic_web_deploy(
                record_store=self._record_store,
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
        no_effect_checkpoint: bool = False,
    ) -> _GenericWebDeployProviderInspection:
        self._proved_pre_image_operation = ""
        self._settlement_operation = ""
        if self._lane.generic_web_cutover is not None:
            return self._inspect_cutover(
                provider_operation_key,
                reconciliation_key,
                expected_provider_target_key,
                provider_effect_phase,
            )
        evidence = self.inspect_evidence(
            provider_operation_key=provider_operation_key,
            provider_effect_phase=provider_effect_phase,
            reconciliation_key=reconciliation_key,
            expected_provider_target_key=expected_provider_target_key,
        )
        if not evidence.identity_matches:
            return evidence
        if evidence.resolved_deploy_target is not None:
            execution, record = self._execution_evidence(
                provider_operation_key, evidence.resolved_deploy_target, provider_effect_phase
            )
            evidence = replace(evidence, execution_record=record)
            if execution is not None:
                if execution.outcome == "unknown":
                    return replace(
                        evidence,
                        observation=GenericWebProviderDeploymentObservation(outcome="unknown"),
                        execution=execution,
                        execution_record=record,
                    )
                evidence = replace(
                    evidence,
                    execution=execution,
                    execution_record=record,
                )
            if evidence.resolved_deploy_target is not None and self._application_testing(
                evidence.resolved_deploy_target
            ):
                try:
                    saved_inventory = cast(
                        _ExecutionInventoryReader, self._record_store
                    ).read_environment_inventory(
                        context_name=self._lane.context, instance_name=self._lane.instance
                    )
                except FileNotFoundError:
                    saved_inventory = None
                evidence = replace(evidence, execution_inventory=saved_inventory)
        observation = evidence.observation
        if (
            evidence.resolved_deploy_target is not None
            and self._application_testing(evidence.resolved_deploy_target)
            and observation.outcome == "present"
            and not generic_web_provider_deployment_succeeded(observation.deployment_status)
        ):
            try:
                GenericWebProviderDeploymentFailed(observation)
            except ValueError:
                return replace(
                    evidence, observation=GenericWebProviderDeploymentObservation(outcome="unknown")
                )
        resolved_target = evidence.resolved_deploy_target
        if observation.outcome != "present":
            if (
                observation.outcome == "absent"
                and resolved_target is not None
                and resolved_target.testing_runtime_digest
            ):
                try:
                    settle = testing_pre_image_settlement_required(
                        record_store=self._record_store,
                        profile=self._profile,
                        lane=self._lane,
                        target=resolved_target,
                        operation_key=provider_operation_key,
                        phase=provider_effect_phase,
                        superseded=self._superseded_testing_candidate,
                        no_effect_checkpoint=no_effect_checkpoint,
                    )
                    obsolete = self._superseded_testing_candidate or (
                        testing_input_authority(self._record_store, self._profile, self._lane)
                        != resolved_target.testing_runtime_digest
                    )
                except (FileNotFoundError, ValueError, click.ClickException):
                    return _GenericWebDeployProviderInspection(
                        observation=GenericWebProviderDeploymentObservation(outcome="unknown"),
                        resolved_deploy_target=resolved_target,
                    )
                if settle:
                    self._settlement_operation = provider_operation_key
                    self._settlement_phase = provider_effect_phase
                    self._settlement_no_effect_checkpoint = no_effect_checkpoint
                    return _GenericWebDeployProviderInspection(
                        observation=observation,
                        resolved_deploy_target=resolved_target,
                        retry_safe=True,
                        provider_evidence="deployment_absent_before_effect",
                    )
                if obsolete:
                    # An obsolete candidate must never execute its old image
                    # merely because ordinary same-artifact retries are safe.
                    return evidence
            if (
                observation.outcome == "absent"
                and resolved_target is not None
                and self._retryable_application_attempt(
                    provider_operation_key, resolved_target, provider_effect_phase
                )
            ):
                return _GenericWebDeployProviderInspection(
                    observation=observation,
                    resolved_deploy_target=resolved_target,
                    retry_safe=True,
                    provider_evidence="deployment_absent_before_effect",
                )
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
                if (
                    resolved_target is not None
                    and resolved_target.resolved_target.target_type == "compose"
                    and self._runtime_close_out_evidence(
                        resolved_target,
                        expected_deployment_record_id=self._deployment_record_id(
                            provider_operation_key
                        ),
                    )
                    is None
                ):
                    # A compose deployment matched only by time and target is
                    # adopted only when the original image provably runs, the
                    # same proof the initial deploy wait requires (#2531).
                    return _GenericWebDeployProviderInspection(
                        observation=GenericWebProviderDeploymentObservation(outcome="unknown"),
                        resolved_deploy_target=resolved_target,
                        provider_evidence="provider_status_unknown",
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
                        and provider_effect_phase
                        in {
                            "",
                            "target_update",
                            "runtime_inputs_verified",
                            "runtime_registry_prepare",
                            "runtime_environment_update",
                        }
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
        saved = evidence.execution_record
        saved_receipt = execution_receipt(saved) if saved is not None else None
        # Product execution and image settlement are independent proof. Legacy
        # statuses alone do not retain the original runtime qualification.
        final_failed_result = (
            self._application_testing(resolved_target)
            and evidence.execution is not None
            and evidence.execution.outcome == "terminal"
            and saved is not None
            and saved_receipt is not None
            and bool(saved_receipt.settled_image_deployment_id)
            and _saved_testing_result_is_final(saved)
            and not deployment_record_passed(saved)
            and observation.outcome == "present"
            and saved_receipt.settled_image_deployment_id
            == saved.deploy.deployment_id
            == observation.deployment_id
            and observation.deployment_status not in {"canceled", "cancelled", "killed"}
        )
        if not final_failed_result:
            observation = qualify_testing_application_observation(
                provider=self._deploy_provider,
                control_plane_root=self._control_plane_root,
                target=resolved_target,
                observation=observation,
                deployment_record_id=self._deployment_record_id(provider_operation_key),
            )
        if observation.outcome != "present":
            return replace(evidence, observation=observation)
        post_deploy_unobserved = generic_web_provider_deployment_succeeded(
            observation.deployment_status
        ) and (
            self._lane.generic_web_extensions.configured
            or generic_web_post_deploy_executor_for_profile(
                self._profile, instance=self._lane.instance
            )
            is not None
        )
        deployment_record_id = self._deployment_record_id(provider_operation_key)
        existing = None
        if callable(getattr(self._record_store, "read_deployment_record", None)):
            try:
                existing = cast(_DeploymentRecordReader, self._record_store).read_deployment_record(
                    deployment_record_id
                )
            except FileNotFoundError:
                pass
        receipt = recorded_application_lifecycle(existing) if existing else None
        if receipt is not None:
            post_deploy_unobserved = receipt.state != "qualified"
        if provider_effect_phase.startswith(("post_deploy_", "application_")):
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
            post_deploy_unobserved = receipt is not None and receipt.state != "qualified"
        if self._application_testing(resolved_target):
            if evidence.execution is not None:
                saved = evidence.execution_record
                post_deploy_unobserved = not _saved_testing_result_is_final(saved) or (
                    saved is not None
                    and deployment_record_passed(saved)
                    and not generic_web_provider_deployment_succeeded(observation.deployment_status)
                )
            elif post_deploy_unobserved:
                observation = GenericWebProviderDeploymentObservation(outcome="unknown")
        return _GenericWebDeployProviderInspection(
            observation=observation,
            resolved_deploy_target=resolved_target,
            post_deploy_unobserved=post_deploy_unobserved,
            provider_evidence=evidence.provider_evidence,
            provider_read_error_class=evidence.provider_read_error_class,
            legacy_correlation_digest_evidence=evidence.legacy_correlation_digest_evidence,
            execution=evidence.execution,
            execution_record=evidence.execution_record,
            execution_inventory=evidence.execution_inventory,
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

    def inspect_runtime_close_out(
        self,
        *,
        resolved_deploy_target: GenericWebResolvedDeployTarget,
        provider_operation_key: str = "",
    ) -> GenericWebRuntimeCloseOutEvidence | None:
        """Return close-out evidence only when the original image provably runs.

        Any unsupported provider, post-deploy phase, read failure, or mismatch
        returns ``None`` so the caller keeps holding the reservation.
        """

        if (
            has_recorded_application_lifecycle(
                self._record_store,
                context=self._lane.context,
                instance=self._lane.instance,
                deployment_record_id=self._deployment_record_id(provider_operation_key)
                if provider_operation_key
                else "",
                artifact_id=resolved_deploy_target.ship_request.artifact_id,
                source_git_ref=resolved_deploy_target.ship_request.source_git_ref,
            )
            or self._lane.generic_web_extensions.configured
            or (
                generic_web_post_deploy_executor_for_profile(
                    self._profile, instance=self._lane.instance
                )
                is not None
            )
        ):
            return None
        return self._runtime_close_out_evidence(resolved_deploy_target)

    def _runtime_close_out_evidence(
        self,
        resolved_deploy_target: GenericWebResolvedDeployTarget,
        *,
        expected_deployment_record_id: str = "",
    ) -> GenericWebRuntimeCloseOutEvidence | None:
        if not isinstance(self._deploy_provider, GenericWebDeployRuntimeArtifactProvider):
            return None
        try:
            observation = self._deploy_provider.observe_runtime_artifact(
                control_plane_root=self._control_plane_root,
                resolved_deploy_target=resolved_deploy_target,
            )
            return evaluate_generic_web_runtime_close_out(
                observation=observation,
                expected_artifact_reference=resolved_deploy_target.ship_request.artifact_id,
                expected_deployment_record_id=expected_deployment_record_id,
                expected_provider_reference=resolved_deploy_target.ship_request.deploy_reference,
            )
        except (FileNotFoundError, ValueError, click.ClickException):
            return None

    def inspect_evidence(
        self,
        *,
        provider_operation_key: str,
        provider_effect_phase: str,
        reconciliation_key: str,
        expected_provider_target_key: str = "",
    ) -> _GenericWebDeployProviderInspection:
        if self._lane.generic_web_cutover is not None:
            return self._inspect_cutover(
                provider_operation_key,
                reconciliation_key,
                expected_provider_target_key,
                provider_effect_phase,
            )
        try:
            snapshot = decode_generic_web_provider_reconciliation_target(reconciliation_key)
            # Older reservations omit this field. Their key must remain unchanged
            # even when execution needs a current qualification authority fallback.
            self._reconciliation_qualification_digest = snapshot.qualification_authority_digest
            self._qualification_authority_digest = (
                snapshot.qualification_authority_digest
                or generic_web_post_deploy_authority_digest(
                    self._record_store, self._profile, self._lane
                )
            )
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
                    recorded=self._recorded_artifact,
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
                if provider_effect_phase in {"", "target_update", "runtime_inputs_verified"}
                else "deployment_absent_after_effect"
            )
        else:
            provider_evidence = "provider_status_unknown"
        return _GenericWebDeployProviderInspection(
            observation=observation,
            resolved_deploy_target=resolved_target,
            provider_evidence=provider_evidence,
        )

    def _inspect_cutover(
        self,
        operation_key: str,
        reconciliation_key: str,
        expected_target_key: str,
        effect_phase: str = "",
    ) -> _GenericWebDeployProviderInspection:
        from control_plane.generic_web_cutover import WebCutoverAuthorityStore, compile_web_cutover
        from control_plane.workflows.generic_web_cutover import WebCutoverProvider, require_route
        from control_plane.contracts.generic_web_cutover import GenericWebCutoverPlan
        from control_plane.workflows.generic_web_deploy_provider import (
            build_generic_web_provider_target_key,
        )

        try:
            target = self._resolve_deploy_target()
            snapshot = decode_generic_web_provider_reconciliation_target(reconciliation_key)
            if (
                snapshot.target_id != target.resolved_target.target_id
                or snapshot.context != self._lane.context
                or snapshot.instance != self._lane.instance
                or snapshot.provider_id != self._deploy_provider.provider_id
                or (
                    expected_target_key
                    and expected_target_key != build_generic_web_provider_target_key(target)
                )
            ):
                raise ValueError("cutover reservation differs from lane authority")
            store = cast(WebCutoverAuthorityStore, self._record_store)
            try:
                record = store.read_deployment_record(self._deployment_record_id(operation_key))
            except FileNotFoundError:
                if effect_phase:
                    raise ValueError("effect journal lacks its cutover checkpoint")
                return _GenericWebDeployProviderInspection(
                    observation=GenericWebProviderDeploymentObservation(outcome="absent"),
                    retry_safe=True,
                    resolved_deploy_target=target,
                )
            cutover = record.generic_web_cutover
            if cutover is None:
                raise ValueError("cutover reservation lacks its bound checkpoint")
            deploy = self._deploy_request.deploy
            plan = compile_web_cutover(
                store,
                product=self._profile.product,
                lane=self._lane,
                artifact_id=deploy.artifact_id,
                source_git_ref=deploy.source_git_ref,
                deploy_reference=deploy.deploy_reference,
                record_id=record.record_id,
            )
            if cutover.step == "blocked" and not effect_phase:
                return _GenericWebDeployProviderInspection(
                    observation=GenericWebProviderDeploymentObservation(outcome="absent"),
                    retry_safe=True,
                    resolved_deploy_target=target,
                    provider_evidence="deployment_absent_before_effect",
                )
            factory = cast(
                Callable[[GenericWebCutoverPlan], WebCutoverProvider] | None,
                getattr(self._deploy_provider, "cutover_provider_factory", None),
            )
            if factory is None or cutover.step in {"blocked", "rolling_back", "rolled_back"}:
                raise ValueError("cutover requires its separate recovery or provider capability")
            provider = factory(plan)
            with provider.exclusive(plan):
                observed = provider.current_route(plan)
                slot = plan.old if observed.slot_id == plan.old.slot_id else plan.new
                require_route(observed, plan, slot)
            if cutover.step == "complete" and slot != plan.new:
                raise ValueError("completed cutover no longer owns public traffic")
            inventory = store.read_environment_inventory(
                context_name=self._lane.context, instance_name=self._lane.instance
            )
            if (
                cutover.step == "complete"
                and cutover.records_committed
                and record.deploy.status == "pass"
                and inventory.deployment_record_id == record.record_id
            ):
                return _GenericWebDeployProviderInspection(
                    observation=GenericWebProviderDeploymentObservation(
                        outcome="present",
                        deployment_status="success",
                        deployment_id=record.record_id,
                        started_at=record.deploy.started_at,
                        finished_at=record.deploy.finished_at,
                    ),
                    resolved_deploy_target=target,
                    provider_evidence="deployment_present",
                )
            # No terminal result exists, but the exact persisted controller is
            # restartable under the same renewed lease/authorization. This is
            # never a claim that a provider target/deployment is absent.
            return _GenericWebDeployProviderInspection(
                observation=GenericWebProviderDeploymentObservation(outcome="absent"),
                retry_safe=True,
                resolved_deploy_target=target,
                provider_evidence="cutover_checkpoint_recoverable",
            )
        except (FileNotFoundError, ValueError, TimeoutError, click.ClickException):
            return _GenericWebDeployProviderInspection(
                observation=GenericWebProviderDeploymentObservation(outcome="unknown"),
                provider_evidence="provider_status_unknown",
            )

    def _retryable_application_attempt(
        self,
        provider_operation_key: str,
        target: GenericWebResolvedDeployTarget,
        phase: str,
    ) -> bool:
        if not callable(getattr(self._record_store, "read_deployment_record", None)):
            return False
        try:
            record = cast(_DeploymentRecordReader, self._record_store).read_deployment_record(
                self._deployment_record_id(provider_operation_key)
            )
        except FileNotFoundError:
            return False
        receipt = recorded_application_lifecycle(record)
        matched = (
            receipt is not None
            and (
                (receipt.state == "restored" and record.deploy.status == "fail")
                or (
                    receipt.state in {"prepared", "held"}
                    and (
                        receipt.state == "prepared"
                        or (bool(target.testing_runtime_digest) and bool(phase))
                    )
                    and (
                        record.deploy.status == "pending"
                        or (bool(target.testing_runtime_digest) and record.deploy.status == "fail")
                    )
                    and phase
                    in {
                        "",
                        "application_lifecycle_prepared",
                        "application_schedule_quiesce",
                        "runtime_inputs_verified",
                        "runtime_registry_prepare",
                        "runtime_environment_update",
                    }
                )
            )
            and (
                receipt.product,
                receipt.context,
                receipt.instance,
                receipt.provider_id,
                receipt.target_id,
                receipt.artifact_id,
                receipt.source_git_ref,
            )
            == (
                self._profile.product,
                self._lane.context,
                self._lane.instance,
                self._deploy_provider.provider_id,
                target.resolved_target.target_id,
                target.ship_request.artifact_id,
                target.ship_request.source_git_ref,
            )
        )
        if matched:
            self._proved_pre_image_operation = provider_operation_key
        return matched

    def _deployment_record_id(self, provider_operation_key: str) -> str:
        return generic_web_provider_deployment_record_id(
            provider_operation_key,
            context=self._lane.context,
            instance=self._lane.instance,
        )

    def _require_testing_publication_authority(
        self,
        target: GenericWebResolvedDeployTarget,
        *,
        current_lane: bool,
        check_inputs: bool,
        check_profile: bool = True,
    ) -> None:
        from control_plane.generic_web_testing_inputs import (
            require_testing_application_target_authority,
        )

        self.execution_check()
        if check_profile and (
            self._record_store.read_product_profile_record(self._profile.product) != self._profile
        ):
            raise ProviderMutationUnknownError("Reviewed product ownership changed.")
        if current_lane:
            require_testing_application_target_authority(
                self._record_store,
                self._profile,
                self._lane,
                target_id=target.resolved_target.target_id,
                provider_id=self._deploy_provider.provider_id,
                driver_id=self._profile.driver_id,
            )
            if (
                check_inputs
                and target.testing_runtime_digest
                and testing_input_authority(self._record_store, self._profile, self._lane)
                != target.testing_runtime_digest
            ):
                raise ProviderMutationUnknownError("Original testing input authority changed.")

    def recover_execution(
        self,
        operation_key: str,
        lease: ProviderOperationLease,
        reviewed: _GenericWebDeployProviderInspection,
        *,
        settle: bool,
    ) -> ProviderMutationOutcome:
        try:
            return self._recover_execution(operation_key, lease, reviewed, settle=settle)
        except (FileNotFoundError, ValueError, click.ClickException, SQLAlchemyError) as error:
            raise ProviderMutationUnknownError(
                "Original testing recovery evidence changed or is unavailable."
            ) from error

    def _recover_execution(
        self,
        operation_key: str,
        lease: ProviderOperationLease,
        reviewed: _GenericWebDeployProviderInspection,
        *,
        settle: bool,
    ) -> ProviderMutationOutcome:
        """Publish or hold the original terminal result; never run its executor again."""
        from control_plane.workflows.generic_web_preview import (
            serialize_generic_web_product_operation,
        )
        from control_plane.workflows.generic_web_application_lifecycle import (
            application_lifecycle_for_lane,
            DokployApplicationLifecycle,
        )
        from control_plane.provider_operations import build_provider_operation_key

        target = reviewed.resolved_deploy_target
        if (
            target is None
            or (settle and (reviewed.execution is None or reviewed.execution_record is None))
            or not self._application_testing(target)
            or not isinstance(lease, ProviderEvidenceLease)
        ):
            raise ProviderMutationUnknownError("Original terminal execution is unavailable.")

        saved_inventory = reviewed.execution_inventory
        saved = reviewed.execution_record
        receipt = execution_receipt(saved) if saved is not None else None
        original_lane = (
            saved_inventory is None
            or saved_inventory.deployment_record_id == self._deployment_record_id(operation_key)
            or (
                receipt is not None
                and receipt.inventory_baseline_digest
                == evidence_digest(saved_inventory.model_dump(mode="json"))
            )
        )
        already_published = (
            saved_inventory is not None
            and saved_inventory.deployment_record_id == self._deployment_record_id(operation_key)
        )
        lifecycle_snapshot = recorded_application_lifecycle(saved) if saved is not None else None
        hold_needed = (
            settle
            and original_lane
            and not (
                saved is not None
                and saved.post_deploy_update.status == "fail"
                and (lifecycle_snapshot is None or lifecycle_snapshot.state == "held")
            )
        )
        publish_inventory = (
            original_lane
            and not already_published
            and generic_web_provider_deployment_succeeded(reviewed.observation.deployment_status)
            and (not settle or saved_inventory is not None)
        )
        current_lane = hold_needed or publish_inventory

        def authority_checkpoint() -> None:
            self._require_testing_publication_authority(
                target, current_lane=current_lane, check_inputs=not settle
            )

        guard = getattr(self._record_store, "provider_evidence_guard", None)
        with (
            serialize_generic_web_product_operation(
                record_store=self._record_store, product=self._profile.product, exclusive=True
            ),
            guard(lease.evidence_reservation, authority_checkpoint=authority_checkpoint)
            if guard
            else nullcontext(),
        ):

            def checkpoint(phase: str) -> None:
                self.execution_check()
                lease.assert_current()
                reservation = lease.evidence_reservation()
                if (
                    build_provider_operation_key(
                        scope=reservation.scope,
                        route_path=reservation.route_path,
                        idempotency_key=reservation.idempotency_key,
                        request_fingerprint=reservation.request_fingerprint,
                        reconciliation_key=reservation.reconciliation_key,
                    )
                    != operation_key
                ):
                    raise ProviderMutationUnknownError("Original recovery identity changed.")
                authority_checkpoint()
                lease.checkpoint_effect(phase)

            reservation = lease.evidence_reservation()
            fresh = self.inspect(
                provider_operation_key=operation_key,
                provider_effect_phase=reservation.provider_effect_phase,
                reconciliation_key=reservation.reconciliation_key,
                expected_provider_target_key=reservation.provider_target_key,
                legacy_provider_effect_started_at=reservation.provider_effect_started_at,
            )
            current, record = fresh.execution, fresh.execution_record
            if (
                (current is not None and current.outcome != "terminal")
                or record != reviewed.execution_record
                or (current is None) != (reviewed.execution is None)
                or (
                    current is not None
                    and reviewed.execution is not None
                    and evidence_digest(current.digest_evidence)
                    != evidence_digest(reviewed.execution.digest_evidence)
                )
            ):
                raise ProviderMutationUnknownError("Reviewed original execution changed.")
            observation = fresh.observation
            if (
                observation != reviewed.observation
                or fresh.execution_inventory != reviewed.execution_inventory
            ):
                raise ProviderMutationUnknownError("Reviewed image observation changed.")
            expected_record = record

            def receipt_update_record(receipt: ExecutionReceipt) -> DeploymentRecord:
                assert expected_record is not None
                expected = expected_record.model_copy(
                    update={
                        "post_deploy_update": expected_record.post_deploy_update.model_copy(
                            update={
                                "evidence": {
                                    **expected_record.post_deploy_update.evidence,
                                    EXECUTION_EVIDENCE_KEY: receipt.model_dump_json(),
                                }
                            }
                        )
                    }
                )
                if self._record_store.read_deployment_record(expected.record_id) != expected:
                    raise ProviderMutationUnknownError("Reviewed original deployment changed.")
                return expected

            if record is not None and current is not None:
                original = execution_receipt(record)
                assert original is not None
                completed = ExecutionRecorder(self._record_store, record.record_id, original)
                for index, entry in enumerate(original.entries):
                    if entry.kind == "schedule_run" and not entry.terminal:
                        completed.update(index, terminal="done")
                record = receipt_update_record(completed.receipt)
                expected_record = record
            if settle and hold_needed:
                assert record is not None
                checkpoint(TESTING_POST_IMAGE_SETTLEMENT_PHASE)
                snapshot = recorded_application_lifecycle(record)
                original_receipt = execution_receipt(record)
                assert original_receipt is not None
                recorder = ExecutionRecorder(self._record_store, record.record_id, original_receipt)
                lifecycle = application_lifecycle_for_lane(
                    control_plane_root=self._control_plane_root,
                    snapshot=snapshot,
                    provider=self._deploy_provider,
                    target_type="application",
                    target_id=target.resolved_target.target_id,
                    checkpoint=checkpoint,
                    effect_started=lambda: None,
                )
                if isinstance(lifecycle, DokployApplicationLifecycle):
                    lifecycle.execution_recorder = recorder
                if lifecycle is not None:
                    lifecycle.hold()
                    lifecycle.snapshot.state = "held"
                record = receipt_update_record(recorder.receipt)
                expected_record = record
                record = record.model_copy(
                    update={
                        "post_deploy_update": record.post_deploy_update.model_copy(
                            update={
                                "evidence": {
                                    **record.post_deploy_update.evidence,
                                    **(lifecycle.snapshot.evidence() if lifecycle else {}),
                                }
                            }
                        )
                    }
                )
            if settle and record is not None and record.post_deploy_update.status != "fail":
                record = record.model_copy(
                    update={
                        "post_deploy_update": record.post_deploy_update.model_copy(
                            update={
                                "attempted": True,
                                "status": "fail",
                                "detail": "Original terminal testing work did not qualify.",
                            }
                        )
                    }
                )
            elif not settle and reviewed.post_deploy_unobserved:
                raise ProviderMutationUnknownError(
                    "Unqualified testing work requires hold-only settlement."
                )
            checkpoint(TESTING_POST_IMAGE_SETTLEMENT_PHASE if settle else "records_commit")
            legacy_image_only = current is None and not fresh.post_deploy_unobserved and not settle
            if legacy_image_only and not _saved_testing_result_is_final(record):
                record, _legacy_records, _legacy_result = build_observed_generic_web_deploy(
                    record_store=self._record_store,
                    profile=self._profile,
                    lane=self._lane,
                    resolved_deploy_target=target,
                    observation=observation,
                    deployment_record_id=self._deployment_record_id(operation_key),
                    post_deploy_unobserved=False,
                )
                if expected_record is not None:
                    record = expected_record.model_copy(
                        update={
                            "deploy": record.deploy,
                            "post_deploy_update": record.post_deploy_update,
                            "runtime_identity": record.runtime_identity,
                        }
                    )
            if record is None:
                raise ProviderMutationUnknownError("The original testing result is unavailable.")
            observed_final_image = (
                record.deploy.status in {"pass", "fail"}
                and record.deploy.deployment_id != GENERIC_WEB_UNOBSERVED_DEPLOYMENT_ID
            )
            if observed_final_image:
                if record.deploy.deployment_id != observation.deployment_id:
                    raise ProviderMutationUnknownError("The original image observation changed.")
            if settle and (
                not observed_final_image
                or not generic_web_provider_deployment_succeeded(observation.deployment_status)
            ):
                record = record.model_copy(
                    update={
                        "deploy": record.deploy.model_copy(
                            update={
                                "status": "pass"
                                if generic_web_provider_deployment_succeeded(
                                    observation.deployment_status
                                )
                                else "fail",
                                "deployment_id": observation.deployment_id,
                                "started_at": observation.started_at,
                                "finished_at": observation.finished_at,
                            }
                        ),
                    }
                )
            elif not observed_final_image:
                raise ProviderMutationUnknownError("The original testing result is unfinished.")
            published = record
            records, result = generic_web_record_result(
                record=published,
                product=self._profile.product,
                target=target,
                observation=observation,
            )
            failed = (
                settle
                or not deployment_record_passed(published)
                or not generic_web_provider_deployment_succeeded(observation.deployment_status)
            )
            if failed:
                result.update(
                    deploy_status="fail",
                    error_message=published.post_deploy_update.detail or observation.error_message,
                )
            if settle:
                result["post_deploy_status"] = "fail"
            inventory = None
            if publish_inventory and (not failed or settle):
                inventory = build_environment_inventory(
                    deployment_record=published,
                    updated_at=published.deploy.finished_at or observation.finished_at,
                )
            cast(_TestingResultPublisher, self._record_store).publish_generic_web_testing_result(
                reservation=lease.evidence_reservation(),
                expected_deployment=expected_record,
                expected_inventory=reviewed.execution_inventory,
                deployment=published,
                inventory=inventory,
            )
            return ProviderMutationOutcome(
                response_status_code=502 if failed else 202,
                response_payload=provider_operation_response_payload(
                    trace_id=self._trace_id, records=records, result=result
                ),
            )

    def _resolve_standing_testing_binding(self) -> str:
        try:
            return resolve_disposable_observation_binding(
                cast(DisposableLaneStore, self._record_store),
                product=self._profile.product,
                context=self._lane.context,
                instance=self._lane.instance,
            ).binding_sha256
        except (FileNotFoundError, ValueError) as error:
            raise DisposableLaneSafetyUnknown(
                "Standing testing deployment lacks current lane qualification."
            ) from error

    def require_standing_testing_retry(self) -> None:
        self._standing_testing_permission = True
        self._standing_binding_sha256 = self._resolve_standing_testing_binding()
        self._require_standing_testing_admission()

    def _require_standing_testing_admission(self) -> None:
        if not self._standing_testing_permission or self._standing_effect_started:
            return
        try:
            profile, lane = resolve_generic_web_deploy_lane(
                record_store=self._record_store,
                product=self._profile.product,
                instance=self._lane.instance,
            )
            if (
                not profile.is_active
                or lane.instance != "testing"
                or lane.generic_web_cutover is not None
                or profile != self._profile
                or lane != self._lane
            ):
                raise DisposableLaneSafetyUnknown("The qualified testing lane changed.")
            binding = require_disposable_lane(
                cast(DisposableLaneStore, self._record_store),
                product=profile.product,
                context=lane.context,
                instance=lane.instance,
            )
            target = self._resolve_deploy_target()
            provider_target = cast(
                _ProviderTargetReader, self._record_store
            ).read_provider_target_record(context_name=lane.context, instance_name=lane.instance)
            expected = target.deployed_target
            resolved = target.resolved_target
            lane_store = cast(DisposableLaneStore, self._record_store)
            definition = lane_store.read_dokploy_target_record(
                context_name=lane.context, instance_name=lane.instance
            )
            target_id = lane_store.read_dokploy_target_id_record(
                context_name=lane.context, instance_name=lane.instance
            )
            if (
                binding != self._standing_binding_sha256
                or expected is None
                or provider_target.to_deployed_target_reference() != expected
                or resolved.target_id != expected.target_id
                or resolved.target_id != target_id.target_id
                or resolved.target_name != expected.display_name
                or resolved.target_type != definition.target_type
                or resolved.target_type
                != (expected.provider_target_type or expected.target_category)
            ):
                raise DisposableLaneSafetyUnknown(
                    "The prepared testing recipient or inputs changed."
                )
        except (FileNotFoundError, ValueError) as error:
            raise DisposableLaneSafetyUnknown(
                "Standing testing deployment lacks current lane qualification."
            ) from error

    def _proved_no_effect_lifecycle_retry(
        self,
        operation_key: str,
        lease: ProviderOperationLease,
        recorder: ExecutionRecorder | None,
        *,
        require_current_declarations: bool = True,
    ) -> bool:
        if recorder is None or recorder.expected_record is None:
            return False
        if not isinstance(lease, ProviderEvidenceLease):
            return False
        lease.assert_current()
        reservation = lease.evidence_reservation()
        record = recorder.expected_record
        receipt = execution_receipt(record)
        snapshot = recorded_application_lifecycle(record)
        target = self._resolve_deploy_target()
        proved_original = (
            not reservation.provider_effect_started_at
            and not reservation.provider_effect_phase
            and record.record_id == self._deployment_record_id(operation_key)
            and record.deploy.status == "pending"
            and record.deploy.deployment_id == GENERIC_WEB_UNOBSERVED_DEPLOYMENT_ID
            and receipt is not None
            and not receipt.entries
            and not receipt.settled_image_deployment_id
            and snapshot is not None
            and snapshot.mode == "deploy"
            and snapshot.state in {"prepared", "not_started"}
            and (snapshot.product, snapshot.context, snapshot.instance, snapshot.target_id)
            == (
                self._profile.product,
                self._lane.context,
                self._lane.instance,
                target.resolved_target.target_id,
            )
            and (snapshot.artifact_id, snapshot.source_git_ref)
            == (target.ship_request.artifact_id, target.ship_request.source_git_ref)
            and snapshot.provider_id == self._deploy_provider.provider_id
        )
        if not proved_original or not require_current_declarations:
            return proved_original
        assert snapshot is not None
        return snapshot.extensions == self._lane.generic_web_extensions

    def _current_testing_inventory_digest(self) -> str:
        try:
            inventory = cast(
                _ExecutionInventoryReader, self._record_store
            ).read_environment_inventory(
                context_name=self._lane.context, instance_name=self._lane.instance
            )
        except FileNotFoundError:
            inventory = None
        return evidence_digest(inventory.model_dump(mode="json") if inventory else None)

    def _require_effect_admission(self) -> None:
        if self._standing_effect_started:
            return
        self._require_standing_testing_admission()
        if (
            self._pre_effect_inventory_digest is not None
            and self._pre_effect_inventory_digest != self._current_testing_inventory_digest()
        ):
            raise _TestingInventoryBaselineChanged("The original testing inventory changed.")

    def _complete_inventory_refusal(
        self, operation_key: str, lease: ProviderOperationLease, recorder: ExecutionRecorder | None
    ) -> ProviderMutationOutcome | None:
        target = self._resolve_deploy_target()
        if (
            not self._complete_inert_inventory_refusal
            or not self._application_testing(target)
            or self._standing_effect_started
            or recorder is None
            or recorder.expected_record is None
        ):
            return None
        if not isinstance(lease, ProviderEvidenceLease):
            raise ProviderMutationUnknownError("Original no-effect lease is unavailable.")
        lease.assert_current()
        reservation = lease.evidence_reservation()
        if reservation.provider_effect_phase or reservation.provider_effect_started_at:
            return None
        record = recorder.expected_record
        receipt = execution_receipt(record)
        ship = target.ship_request
        if (
            operation_key
            != build_provider_operation_key(
                scope=reservation.scope,
                route_path=reservation.route_path,
                idempotency_key=reservation.idempotency_key,
                request_fingerprint=reservation.request_fingerprint,
                reconciliation_key=reservation.reconciliation_key,
            )
            or record.record_id != self._deployment_record_id(operation_key)
            or record.deploy.status != "pending"
            or record.deploy.deployment_id != GENERIC_WEB_UNOBSERVED_DEPLOYMENT_ID
            or receipt is None
            or receipt != recorder.receipt
            or not receipt.inventory_baseline_digest
            or receipt.inventory_baseline_digest != self._pre_effect_inventory_digest
            or receipt.entries
            or receipt.settled_image_deployment_id
            or (record.context, record.instance, record.source_git_ref, record.resolved_target)
            != (
                self._lane.context,
                self._lane.instance,
                ship.source_git_ref,
                target.resolved_target,
            )
            or record.artifact_identity is None
            or record.artifact_identity.artifact_id != ship.artifact_id
            or record.deployed_target is None
            or record.deployed_target != target.deployed_target
            or record.deployed_target.provider_id != self._deploy_provider.provider_id
        ):
            raise ProviderMutationUnknownError("Original inert testing attempt is not proved.")
        if recorded_application_lifecycle(
            record
        ) is not None and not self._proved_no_effect_lifecycle_retry(
            operation_key, lease, recorder, require_current_declarations=False
        ):
            raise ProviderMutationUnknownError("Original lifecycle inactivity is not proved.")
        try:
            inventory = cast(
                _ExecutionInventoryReader, self._record_store
            ).read_environment_inventory(context_name=record.context, instance_name=record.instance)
        except FileNotFoundError:
            inventory = None
        if receipt.inventory_baseline_digest == evidence_digest(
            inventory.model_dump(mode="json") if inventory else None
        ):
            return None
        failed = record.model_copy(
            update={
                "deploy": record.deploy.model_copy(
                    update={"status": "fail", "finished_at": utc_now_timestamp()}
                ),
                "post_deploy_update": record.post_deploy_update.model_copy(
                    update={
                        "attempted": False,
                        "status": "skipped",
                        "detail": "Testing inventory changed before any provider effect.",
                    }
                ),
            }
        )
        guard = getattr(self._record_store, "provider_evidence_guard")
        with guard(lease.evidence_reservation, authority_checkpoint=self.execution_check):
            cast(_TestingResultPublisher, self._record_store).publish_generic_web_testing_result(
                reservation=lease.evidence_reservation(),
                expected_deployment=record,
                expected_inventory=inventory,
                deployment=failed,
                inventory=None,
            )
        records, result = generic_web_record_result(
            record=failed,
            product=self._profile.product,
            target=target,
            observation=GenericWebProviderDeploymentObservation(
                outcome="unknown", error_message=failed.post_deploy_update.detail
            ),
        )
        payload = provider_operation_response_payload(
            trace_id=self._trace_id, records=records, result=result
        )
        payload["testing_settlement"] = TESTING_INERT_INVENTORY_REFUSAL_SETTLED
        return ProviderMutationOutcome(
            response_status_code=409, response_payload=payload, provider_effect_performed=False
        )

    def _record_no_effect_lifecycle_refusal(
        self, operation_key: str, lease: ProviderOperationLease, recorder: ExecutionRecorder | None
    ) -> None:
        if recorder is None or recorder.expected_record is None:
            return
        snapshot = recorded_application_lifecycle(recorder.expected_record)
        if snapshot is None or recorder.expected_record.deploy.status != "pending":
            return
        if not isinstance(lease, ProviderEvidenceLease):
            raise ProviderMutationUnknownError("Original no-effect lease is unavailable.")
        lease.assert_current()
        reservation = lease.evidence_reservation()
        if reservation.provider_effect_phase or reservation.provider_effect_started_at:
            return
        if not self._proved_no_effect_lifecycle_retry(
            operation_key, lease, recorder, require_current_declarations=False
        ):
            raise ProviderMutationUnknownError("Original no-effect retirement is not proved.")
        if snapshot.state == "not_started":
            return
        assert recorder is not None and recorder.expected_record is not None
        assert isinstance(lease, ProviderEvidenceLease)
        record = recorder.expected_record
        snapshot = recorded_application_lifecycle(record)
        assert snapshot is not None
        retired = record.model_copy(
            update={
                "post_deploy_update": record.post_deploy_update.model_copy(
                    update={
                        "evidence": {
                            **record.post_deploy_update.evidence,
                            **snapshot.model_copy(update={"state": "not_started"}).evidence(),
                        }
                    }
                )
            }
        )
        try:
            inventory = cast(
                _ExecutionInventoryReader, self._record_store
            ).read_environment_inventory(context_name=record.context, instance_name=record.instance)
        except FileNotFoundError:
            inventory = None
        guard = getattr(self._record_store, "provider_evidence_guard")
        with guard(lease.evidence_reservation, authority_checkpoint=self.execution_check):
            cast(_TestingResultPublisher, self._record_store).publish_generic_web_testing_result(
                reservation=lease.evidence_reservation(),
                expected_deployment=record,
                expected_inventory=inventory,
                deployment=retired,
                inventory=None,
            )
        recorder.expected_record = retired

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        self._standing_effect_started = self._settlement_operation == provider_operation_key
        self._pre_effect_inventory_digest = None
        guard = (
            getattr(self._record_store, "provider_evidence_guard", None)
            if self._standing_testing_permission
            or self._application_testing(self._resolve_deploy_target())
            else None
        )
        if self._standing_testing_permission and guard is None:
            raise ProviderMutationRejectedError(
                DisposableLaneSafetyUnknown("Testing effect admission requires durable storage.")
            )
        context = nullcontext()
        if guard is not None:
            if not isinstance(lease, ProviderEvidenceLease):
                raise ProviderMutationUnknownError(
                    "Application evidence requires its original execution lease."
                )
            context = guard(
                lease.evidence_reservation,
                effect_admission_checkpoint=self._require_effect_admission,
            )
        with context:
            try:
                self._require_standing_testing_admission()
            except DisposableLaneSafetyUnknown as error:
                raise ProviderMutationRejectedError(error) from error
            return self._apply(provider_operation_key, lease)

    def _apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        if self._settlement_operation == provider_operation_key:
            try:
                return settle_testing_pre_image_attempt(
                    record_store=self._record_store,
                    profile=self._profile,
                    lane=self._lane,
                    target=self._resolve_deploy_target(),
                    operation_key=provider_operation_key,
                    phase=self._settlement_phase,
                    no_effect_checkpoint=self._settlement_no_effect_checkpoint,
                    superseded=self._superseded_testing_candidate,
                    provider=self._deploy_provider,
                    control_plane_root=self._control_plane_root,
                    lease=lease,
                    check_execution=self.execution_check,
                    trace_id=self._trace_id,
                )
            except (FileNotFoundError, ValueError, click.ClickException) as error:
                raise ProviderMutationUnknownError(str(error)) from error
        effect_started = False
        if self._lane.generic_web_cutover is not None:
            try:
                previous = cast(_DeploymentRecordReader, self._record_store).read_deployment_record(
                    self._deployment_record_id(provider_operation_key)
                )
            except FileNotFoundError:
                pass
            else:
                effect_started = (
                    previous.generic_web_cutover is not None
                    and previous.generic_web_cutover.step != "blocked"
                )

        known_dispatch_refusal = False

        def checkpoint(phase: str) -> None:
            nonlocal effect_started, known_dispatch_refusal
            try:
                self.execution_check()
            except (RuntimeError, SQLAlchemyError) as error:
                # The runner releases only when no earlier effect checkpoint
                # exists; after an effect it preserves the reconciliation fence.
                raise ProviderMutationRejectedError(error) from error
            if phase == "provider_dispatch_prepared" or known_dispatch_refusal:
                lease.assert_current()
            else:
                lease.checkpoint_effect(phase)
                effect_started = True
                self._standing_effect_started = True
                known_dispatch_refusal = phase == TESTING_DISPATCH_REFUSED_PHASE

        proved_pre_image_retry = self._proved_pre_image_operation == provider_operation_key
        self._proved_pre_image_operation = ""
        resolved_target = self._resolve_deploy_target()
        recorder = (
            self._execution_recorder(provider_operation_key)
            if self._application_testing(resolved_target)
            else None
        )
        if recorder is not None and isinstance(lease, ProviderEvidenceLease):
            lease.assert_current()
            reservation = lease.evidence_reservation()
            if not reservation.provider_effect_phase and not reservation.provider_effect_started_at:
                self._pre_effect_inventory_digest = recorder.receipt.inventory_baseline_digest
        try:
            expected_inventory = (
                cast(_ExecutionInventoryReader, self._record_store).read_environment_inventory(
                    context_name=self._lane.context, instance_name=self._lane.instance
                )
                if recorder is not None
                else None
            )
        except FileNotFoundError:
            expected_inventory = None

        def publish_deployment(
            record: DeploymentRecord, inventory: EnvironmentInventory | None
        ) -> None:
            nonlocal expected_inventory
            assert recorder is not None
            progress = record.post_deploy_update.status == "pending"
            if not progress:
                require_recorded_execution(recorder.receipt)
            try:
                current_inventory = cast(
                    _ExecutionInventoryReader, self._record_store
                ).read_environment_inventory(
                    context_name=record.context, instance_name=record.instance
                )
            except FileNotFoundError:
                current_inventory = None
            if current_inventory != expected_inventory:
                if progress:
                    raise ProviderMutationUnknownError(
                        "Testing inventory changed before qualification."
                    )
                inventory = None
            guard = getattr(self._record_store, "provider_evidence_guard", None)
            if guard is None or not isinstance(lease, ProviderEvidenceLease):
                raise ProviderMutationUnknownError(
                    "Testing publication requires its original execution."
                )

            def authority() -> None:
                self._require_testing_publication_authority(
                    resolved_target,
                    current_lane=inventory is not None,
                    check_inputs=not progress
                    and inventory is not None
                    and deployment_record_passed(record),
                    check_profile=not progress,
                )

            with guard(lease.evidence_reservation, authority_checkpoint=authority):
                cast(
                    _TestingResultPublisher, self._record_store
                ).publish_generic_web_testing_result(
                    reservation=lease.evidence_reservation(),
                    expected_deployment=recorder.expected_record,
                    expected_inventory=current_inventory,
                    deployment=record,
                    inventory=inventory,
                )
            recorder.expected_record = record
            if inventory is not None:
                expected_inventory = inventory
            if progress:
                self._require_testing_publication_authority(
                    resolved_target,
                    current_lane=True,
                    check_inputs=True,
                )

        try:
            resolved_target = self._resolve_deploy_target()
            proved_no_effect_retry = False
            if recorder is not None and recorder.expected_record is not None:
                historical_no_effect = self._proved_no_effect_lifecycle_retry(
                    provider_operation_key, lease, recorder, require_current_declarations=False
                )
                if historical_no_effect:
                    if not self._proved_no_effect_lifecycle_retry(
                        provider_operation_key, lease, recorder
                    ):
                        raise ApplicationLifecycleBaselineChanged(
                            "The original testing baseline changed."
                        )
                    proved_no_effect_retry = True
            if self._pre_effect_inventory_digest is not None:
                self._require_effect_admission()
            records, result = execute_generic_web_deploy_result(
                control_plane_root=self._control_plane_root,
                record_store=self._record_store,
                request=self._deploy_request,
                profile=self._profile,
                lane=self._lane,
                provider_operation_title=provider_operation_title(provider_operation_key),
                deployment_record_id=self._deployment_record_id(provider_operation_key),
                deploy_provider=self._deploy_provider,
                post_deploy_authority_digest=self._qualification_authority_digest,
                execution_recorder=recorder,
                publish_deployment=publish_deployment
                if recorder is not None
                and getattr(self._record_store, "publish_generic_web_testing_result", None)
                is not None
                else None,
                resolved_deploy_target=resolved_target,
                provider_effect_checkpoint=checkpoint,
                before_records_commit=lambda: checkpoint("records_commit"),
                # Publishing a refusal checks ownership without inventing a
                # provider effect or replacing the last recoverable phase.
                before_failure_records_commit=(
                    (lambda: checkpoint("provider_dispatch_prepared"))
                    if recorder is not None or resolved_target.testing_runtime_digest
                    else None
                ),
                recorded_artifact=self._recorded_artifact,
                proved_pre_image_retry=proved_pre_image_retry,
                proved_no_effect_retry=proved_no_effect_retry,
                established_testing_predecessor=self._standing_testing_permission,
            )
        except TimeoutError as error:
            if self._lane.generic_web_cutover is not None:
                raise ProviderMutationUnknownError(type(error).__name__) from error
            raise
        except (FileNotFoundError, ValueError, ProviderMutationRejectedError) as error:
            if self._lane.generic_web_cutover is not None and effect_started:
                raise ProviderMutationUnknownError(type(error).__name__) from error
            try:
                self._record_no_effect_lifecycle_refusal(provider_operation_key, lease, recorder)
                if isinstance(error, _TestingInventoryBaselineChanged) and not effect_started:
                    completed = self._complete_inventory_refusal(
                        provider_operation_key, lease, recorder
                    )
                    if completed is not None:
                        return completed
            except Exception as publication_error:
                raise ProviderMutationUnknownError(str(publication_error)) from publication_error
            if isinstance(error, ProviderMutationRejectedError):
                raise
            raise ProviderMutationRejectedError(error)
        except click.ClickException as error:
            from control_plane.lane_movement import LaneMovementRefused

            if isinstance(error, LaneMovementRefused) and error.code == "source_order_unavailable":
                raise ProviderMutationRejectedError(error) from error
            raise ProviderMutationUnknownError(str(error)) from error
        provider_effect_attempted = result.pop("provider_effect_attempted", False) is True
        provider_failure_proved = result.pop("provider_failure_proved", False) is True
        if (
            _string_field(result, "deploy_status") == "fail"
            and provider_effect_attempted
            and not provider_failure_proved
        ):
            raise ProviderMutationUnknownError(
                _string_field(result, "error_message")
                or "Generic web provider outcome requires reconciliation."
            )
        return ProviderMutationOutcome(
            response_status_code=502 if provider_failure_proved else 202,
            response_payload=provider_operation_response_payload(
                trace_id=self._trace_id,
                records=records,
                result=result,
            ),
            durable=provider_failure_proved or should_store_generic_web_deploy_idempotency(result),
            provider_effect_performed=provider_effect_attempted,
        )
