"""Stage testing configuration without changing running resources or claiming disposability."""

from contextlib import nullcontext
from pathlib import Path
from typing import cast

from control_plane import live_target_runtime as runtime
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.disposable_lanes import (
    DisposableLaneStore,
    disposable_lane_binding_sha256,
    disposable_lane_configuration,
)
from control_plane.integration_allowances import canonical_sha256
from control_plane.product_config_service import resolve_product_config_profile
from control_plane.provider_operations import (
    ProviderMutationOutcome,
    ProviderMutationRejectedError,
    ProviderMutationUnknownError,
    ProviderObservation,
    ProviderOperationLease,
    provider_operation_response_payload,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.generic_web_deploy_provider import generic_web_provider_target_key


def separation_binding(
    store: PostgresRecordStore, request: runtime.LiveTargetRuntimeApplyEnvelope, actor: str
) -> tuple[str, str]:
    """Bind the recipient and inputs; usage/custody observations are irrelevant to staging."""
    if not request.separation_only or request.instance != "testing" or request.deploy:
        raise ValueError("Separation only stages testing configuration with deployment disabled.")
    profile = resolve_product_config_profile(
        store, product=request.product, context=request.context, instance=request.instance
    )
    if not profile.is_active or profile.driver_id != "generic-web":
        raise ValueError("Configuration separation requires an active generic-web testing lane.")
    target = store.read_dokploy_target_record(
        context_name=request.context, instance_name=request.instance
    )
    target_id = store.read_dokploy_target_id_record(
        context_name=request.context, instance_name=request.instance
    )
    provider = store.read_provider_target_record(
        context_name=request.context, instance_name=request.instance
    )
    if (
        target.target_type != "compose"
        or provider.provider_id != "dokploy"
        or provider.provider_target_type != "compose"
        or provider.target_id != target_id.target_id
    ):
        raise ValueError("The testing recipient is ambiguous or unsupported.")
    physical: tuple[ProviderTargetRecord | DokployTargetIdRecord, ...] = (
        *store.list_dokploy_target_id_records(),
        *store.list_provider_target_records(),
    )
    if any(
        item.target_id == target_id.target_id
        and (item.context, item.instance) != (request.context, request.instance)
        for item in physical
    ):
        raise ValueError(
            "The testing target aliases another lane; shared targets cannot be changed."
        )
    evidence = store.read_disposable_lane_safety(request.product, request.context, request.instance)
    if evidence is not None and evidence.usage == "in_use":
        raise ValueError(
            "Recorded person use must be resolved before changing testing configuration."
        )
    hold = target.policies.staff_testing_hold
    if hold is None or hold.purpose != "isolation_staging" or hold.recorded_by != actor:
        raise ValueError(
            "Separation requires this caller's isolation-staging interlock, never a staff hold."
        )
    key = generic_web_provider_target_key(
        provider_id="dokploy",
        context=request.context,
        instance=request.instance,
        target_type="compose",
        target_id=target_id.target_id,
    )
    return canonical_sha256(
        [
            disposable_lane_binding_sha256(
                profile,
                target,
                target_id,
                configuration=disposable_lane_configuration(
                    cast(DisposableLaneStore, store), request.context, request.instance
                ),
            ),
            provider.model_dump(mode="json"),
            [
                p.model_dump(mode="json")
                for p in store.list_runtime_key_safety_policy_records(status="active", limit=1)
            ],
        ]
    ), key


class TestingRuntimeSeparation:
    def __init__(
        self,
        *,
        store: PostgresRecordStore,
        root: Path,
        request: runtime.LiveTargetRuntimeApplyEnvelope,
        actor: str,
        binding: str,
        target_key: str,
        trace_id: str,
    ) -> None:
        self.store, self.root, self.request, self.actor = store, root, request, actor
        self.binding, self.key, self.trace_id = binding, target_key, trace_id

    def target_key(self) -> str:
        # Share the deploy fence. An unknown deploy is never cleared or bypassed.
        return self.key

    def reconciliation_key(self) -> str:
        return canonical_sha256([self.key, self.binding])

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        # A lost provider write cannot be proved absent. Never redispatch it.
        return ProviderObservation(outcome="unknown")

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        effect_started = False

        def checkpoint() -> None:
            nonlocal effect_started
            current, key = separation_binding(self.store, self.request, self.actor)
            if (current, key) != (self.binding, self.key):
                raise ValueError("Separation inputs changed; review a fresh plan.")
            lease.checkpoint_effect("testing_environment_staging")
            effect_started = True

        # PostgreSQL excludes authority insert/update races, including a new hold
        # or target alias, through the one env-only provider write and readback.
        # SQLite is a deterministic rehearsal and publishes no safety facts.
        guard = (
            self.store.serialize_disposable_testing_resources()
            if self.store.database_dialect_name == "postgresql"
            else nullcontext()
        )
        try:
            with guard:
                current, key = separation_binding(self.store, self.request, self.actor)
                if (current, key) != (self.binding, self.key):
                    raise ValueError("Separation inputs changed; review a fresh plan.")
                runtime.evaluate_runtime_key_safety_for_live_target_sync(
                    record_store=self.store,
                    context_name=self.request.context,
                    instance_name=self.request.instance,
                )
                result = runtime.apply_live_target_runtime_environment(
                    control_plane_root=self.root,
                    database_url=self.store.database_url,
                    product_name=self.request.product,
                    context_name=self.request.context,
                    instance_name="testing",
                    apply_changes=True,
                    deploy=False,
                    no_cache=False,
                    deploy_timeout_seconds=None,
                    deploy_trigger=runtime.trigger_and_wait_for_dokploy_target_deploy,
                    before_provider_mutation=checkpoint,
                )
        except Exception as error:
            if not effect_started:
                raise ProviderMutationRejectedError(error) from error
            raise ProviderMutationUnknownError(
                "Testing environment staging outcome is unknown."
            ) from error
        return ProviderMutationOutcome(
            response_status_code=202,
            response_payload=provider_operation_response_payload(
                trace_id=self.trace_id,
                records={},
                result=result,
            ),
            provider_effect_performed=effect_started,
        )
