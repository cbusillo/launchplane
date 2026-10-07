"""The shared durable generic-web promotion adapter for HTTP and Client releases."""

import hashlib
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path as FilePath
from typing import cast

import click
from starlette.exceptions import HTTPException as StarletteHTTPException

from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.generic_web_promotion_http import (
    GenericWebProdPromotionEnvelope,
    execute_generic_web_prod_promotion_result,
    generic_web_promotion_outcome_is_settled,
)
from control_plane.provider_operations import (
    ProviderMutationOutcome,
    ProviderMutationRejectedError,
    ProviderMutationUnknownError,
    ProviderObservation,
    ProviderOperationLease,
    ProviderEvidenceLease,
    provider_operation_title,
    provider_operation_response_payload as _provider_operation_response_payload,
)
from control_plane.storage.postgres import PostgresRecordStore

from control_plane.workflows.generic_web_deploy import normalize_generic_web_artifact_id
from control_plane.workflows.generic_web_deploy_provider import (
    GenericWebDeployProvider,
    GenericWebResolvedDeployTarget,
    build_generic_web_provider_reconciliation_key,
    build_generic_web_provider_target_key,
    default_generic_web_deploy_provider,
)
from control_plane.workflows.generic_web_promotion import (
    GenericWebPromotionStore,
    resolve_generic_web_promotion_inputs,
)


class PromotionTargetChanged(click.ClickException):
    """The cached provider target no longer names production's recorded target."""


def generic_web_promotion_deployment_id(
    provider_operation_key: str, lane: ProductLaneProfile
) -> str:
    operation_digest = hashlib.sha256(provider_operation_key.encode("utf-8")).hexdigest()[:24]
    return f"deployment-provider-operation-{operation_digest}-{lane.context}-{lane.instance}"


def require_generic_web_promotion_target(
    *,
    record_store: object,
    lane: ProductLaneProfile,
    resolved_deploy_target: GenericWebResolvedDeployTarget,
) -> None:
    if not isinstance(record_store, PostgresRecordStore):
        raise TypeError("Live generic-web promotion requires Launchplane database storage.")
    try:
        current = record_store.read_provider_target_record(
            context_name=lane.context,
            instance_name=lane.instance,
        )
    except FileNotFoundError as error:
        raise PromotionTargetChanged(
            "The production provider target is no longer available."
        ) from error
    expected = resolved_deploy_target.deployed_target
    resolved = resolved_deploy_target.resolved_target
    if (
        expected is None
        or current.to_deployed_target_reference() != expected
        or resolved.target_id != expected.target_id
        or resolved.target_name != expected.display_name
        or resolved.target_type != (expected.provider_target_type or expected.target_category)
    ):
        raise PromotionTargetChanged(
            "The production provider target changed before promotion execution."
        )


class GenericWebProdPromotionProviderMutationAdapter:
    def __init__(
        self,
        *,
        control_plane_root: FilePath,
        record_store: object,
        promotion_request: GenericWebProdPromotionEnvelope,
        profile: LaunchplaneProductProfileRecord,
        lane: ProductLaneProfile,
        trace_id: str,
        validate_before_effect: Callable[[GenericWebResolvedDeployTarget], None],
        deploy_provider: GenericWebDeployProvider | None = None,
        validate_before_checkpoint: Callable[[], None] | None = None,
        settle_pre_effect_failure: bool = False,
    ) -> None:
        self._control_plane_root = control_plane_root
        self._record_store = record_store
        self._promotion_request = promotion_request
        self._profile = profile
        self._lane = lane
        self._trace_id = trace_id
        self._validate_before_effect = validate_before_effect
        self._deploy_provider = deploy_provider or default_generic_web_deploy_provider()
        self._validate_before_checkpoint = validate_before_checkpoint
        self._settle_pre_effect_failure = settle_pre_effect_failure
        self._resolved_deploy_target: GenericWebResolvedDeployTarget | None = None

    def resolve_deploy_target(self) -> GenericWebResolvedDeployTarget:
        if self._resolved_deploy_target is None:
            # A workflow may leave the artifact to Launchplane; resolve it from
            # testing inventory exactly as the promotion itself does, so the
            # target is resolved for the build that will be deployed.
            promotion = resolve_generic_web_promotion_inputs(
                record_store=cast(GenericWebPromotionStore, self._record_store),
                request=self._promotion_request.promotion,
            )
            self._resolved_deploy_target = self._deploy_provider.resolve_deploy_target(
                control_plane_root=self._control_plane_root,
                request_artifact_id=promotion.artifact_id,
                request_source_git_ref=promotion.source_git_ref,
                request_timeout_seconds=promotion.timeout_seconds,
                request_no_cache=promotion.no_cache,
                record_store=self._record_store,
                profile=self._profile,
                lane=self._lane,
                normalized_artifact_id=normalize_generic_web_artifact_id(
                    profile=self._profile,
                    artifact_id=promotion.artifact_id,
                ),
                fallback_target_name=f"{self._profile.product}-{self._lane.instance}",
                request_deploy_reference=promotion.deploy_reference,
            )
        return self._resolved_deploy_target

    def reconciliation_key(self) -> str:
        return build_generic_web_provider_reconciliation_key(
            self.resolve_deploy_target(),
            product=self._profile.product,
        )

    def target_key(self) -> str:
        return build_generic_web_provider_target_key(self.resolve_deploy_target())

    def observe(
        self,
        provider_operation_key: str,
        provider_effect_phase: str,
        reconciliation_key: str,
    ) -> ProviderObservation:
        del provider_operation_key, provider_effect_phase, reconciliation_key
        return ProviderObservation(outcome="unknown")

    def _deployment_record_id(self, provider_operation_key: str) -> str:
        return generic_web_promotion_deployment_id(provider_operation_key, self._lane)

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        provider_effect_attempted = False

        def checkpoint_provider_effect(phase: str) -> None:
            nonlocal provider_effect_attempted
            # Recovery must finish even if acceptance is withdrawn after deployment.
            if self._validate_before_checkpoint is not None and not phase.startswith("rollback_"):
                self._validate_before_checkpoint()
            lease.checkpoint_effect(phase)
            provider_effect_attempted = True

        try:
            self._validate_before_effect(self.resolve_deploy_target())
            evidence_guard: AbstractContextManager[None]
            if isinstance(self._record_store, PostgresRecordStore):
                if not isinstance(lease, ProviderEvidenceLease):
                    raise ValueError("Promotion requires an evidence-bound provider lease.")
                evidence_guard = self._record_store.provider_evidence_guard(
                    lease.evidence_reservation
                )
            else:
                evidence_guard = nullcontext()
            with evidence_guard:
                records, result = execute_generic_web_prod_promotion_result(
                    control_plane_root=self._control_plane_root,
                    record_store=self._record_store,
                    request=self._promotion_request,
                    deploy_provider=self._deploy_provider,
                    resolved_deploy_target=self.resolve_deploy_target(),
                    provider_operation_title=provider_operation_title(provider_operation_key),
                    deployment_record_id=self._deployment_record_id(provider_operation_key),
                    provider_effect_checkpoint=checkpoint_provider_effect,
                )
        except StarletteHTTPException as error:
            raise ProviderMutationRejectedError(error) from error
        except (FileNotFoundError, ValueError, click.ClickException) as error:
            from control_plane.lane_movement import LaneMovementRefused

            if isinstance(error, LaneMovementRefused) and error.code == "source_order_unavailable":
                raise ProviderMutationRejectedError(error) from error
            if provider_effect_attempted:
                raise ProviderMutationUnknownError(str(error)) from error
            if self._settle_pre_effect_failure:
                return ProviderMutationOutcome(
                    response_status_code=202,
                    response_payload=_provider_operation_response_payload(
                        trace_id=self._trace_id,
                        records={},
                        result={"promotion_status": "fail", "error_code": "promotion_not_ready"},
                    ),
                    durable=True,
                    provider_effect_performed=False,
                )
            raise ProviderMutationRejectedError(error) from error
        return ProviderMutationOutcome(
            response_status_code=202,
            response_payload=_provider_operation_response_payload(
                trace_id=self._trace_id,
                records=records.model_dump(mode="json"),
                result=result.model_dump(mode="json"),
            ),
            durable=generic_web_promotion_outcome_is_settled(result)
            or (self._settle_pre_effect_failure and not provider_effect_attempted),
            provider_effect_performed=provider_effect_attempted,
        )
