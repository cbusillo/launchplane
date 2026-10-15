"""Prepare independently backed empty storage without changing the retained lane."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.disposable_lanes import disposable_lane_configuration
from control_plane.dokploy import api, post_deploy, source
from control_plane.generic_web_testing_exclusion import (
    _bind,
    _target_reservations,
    inspect_generic_web_testing_exclusion,
)
from control_plane.live_target_runtime import evaluate_runtime_key_safety_for_live_target_sync
from control_plane.odoo_testing_resources import (
    GenericWebTestingResources,
    resource_readback_passes,
)
from control_plane.provider_operations import (
    ProviderMutationOutcome,
    ProviderMutationRejectedError,
    ProviderMutationUnknownError,
    ProviderObservation,
    ProviderOperationLease,
    provider_operation_response_payload,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_event_deploy_recovery import testing_event_execution_lock_name
from control_plane.workflows.generic_web_deploy_provider import generic_web_provider_target_key

ROUTE = "/v1/products/{product}/testing-separation-resources"


class SeparationResourcesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    context: str = Field(min_length=1)
    mode: Literal["dry-run", "apply"] = "dry-run"
    reason: str = Field(min_length=1)
    reviewed_plan_sha256: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")


class SeparationResourcesRefused(ValueError):
    """No new resource effect has been attempted."""


def preparation_binding(
    store: PostgresRecordStore, product: str, context: str, actor: str
) -> tuple[str, str]:
    bindings = _bind(store, product, context, "testing")
    profile, target, target_id, provider = bindings
    if not profile.is_active:
        raise SeparationResourcesRefused("Preparation requires an active product.")
    hold = target.policies.staff_testing_hold
    # This does not reinterpret a legacy hold. Even a genuine staff hold remains
    # in force: preparation cannot change its target, configuration or dispatch.
    if hold is None or hold.recorded_by != actor:
        raise SeparationResourcesRefused("Retain this caller's existing testing interlock.")
    key = generic_web_provider_target_key(
        provider_id=provider.provider_id,
        context=context,
        instance="testing",
        target_type=target.target_type,
        target_id=target_id.target_id,
    )
    reservations = _target_reservations(
        store, product=product, context=context, target_key=key, target_id=target_id.target_id
    )
    if not reservations:
        raise SeparationResourcesRefused("The retained historical reservation is required.")
    return canonical_json_sha256(
        {
            "records": [record.model_dump(mode="json") for record in bindings],
            "configuration": [
                row.model_dump(mode="json")
                for row in disposable_lane_configuration(store, context, "testing")
            ],
            "reservations": [row.model_dump(mode="json") for row in reservations],
            "key_policy": [
                row.model_dump(mode="json")
                for row in store.list_runtime_key_safety_policy_records(status="active", limit=1)
            ],
        }
    ), key


@dataclass
class SeparationResourcesAdapter:
    root: Path
    store: PostgresRecordStore
    product: str
    request: SeparationResourcesRequest
    actor: str
    trace_id: str

    def target_key(self) -> str:
        # A distinct fence is safe ONLY for this fixed create-only program. No
        # old-recipient writer or deploy adapter may use this reservation.
        return f"testing-independent-storage:{self.product}:{self.request.context}"

    def reconciliation_key(self) -> str:
        return self.request.reviewed_plan_sha256

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        # Unknown dispatch is never inferred absent, nor blindly reissued. This
        # receipt cannot settle the original deployment or an interrupted schedule.
        return ProviderObservation(outcome="unknown")

    def plan(self) -> dict[str, object]:
        binding, _ = preparation_binding(self.store, self.product, self.request.context, self.actor)
        return {
            "product": self.product,
            "context": self.request.context,
            "instance": "testing",
            "plan_sha256": binding,
            "retained_target_preserved": True,
            "hold_preserved": True,
            "historical_settlement": "not_evaluated",
            "configuration_updated": False,
            "deployment_requested": False,
            "published_host_ports": False,
            "credentials_installed": False,
            "applied": False,
        }

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        started = False
        try:
            observed = inspect_generic_web_testing_exclusion(
                store=self.store,
                root=self.root,
                product=self.product,
                context=self.request.context,
                instance="testing",
                trace_id=self.trace_id,
            )
            if observed.status != "excluded":
                raise SeparationResourcesRefused("Complete current exclusion is required.")
            binding, old_key = preparation_binding(
                self.store, self.product, self.request.context, self.actor
            )
            if binding != self.request.reviewed_plan_sha256:
                raise SeparationResourcesRefused("Review the changed preparation inputs.")
            with self.store.testing_replacement_execution_lock(
                testing_event_execution_lock_name(old_key)
            ) as old_check:
                if old_check is None:
                    raise SeparationResourcesRefused("The original executor is active.")

                def check() -> None:
                    lease.assert_current()
                    old_check()
                    if preparation_binding(
                        self.store, self.product, self.request.context, self.actor
                    ) != (binding, old_key):
                        raise ValueError("Retained authority changed during preparation.")
                    evaluate_runtime_key_safety_for_live_target_sync(
                        record_store=self.store,
                        context_name=self.request.context,
                        instance_name="testing",
                    )

                def effect(phase: str) -> None:
                    nonlocal started
                    with self.store.serialize_disposable_testing_resources():
                        check()
                        lease.checkpoint_effect(phase)
                        started = True

                check()
                target_id = self.store.read_dokploy_target_id_record(
                    context_name=self.request.context, instance_name="testing"
                ).target_id
                resources = GenericWebTestingResources(
                    product=self.product,
                    context=self.request.context,
                    operation_id=binding,
                    target_id=target_id,
                    namespace=f"lp-testing-{binding[:32]}",
                )
                evidence = run_preparation_schedule(
                    root=self.root, resources=resources, before_effect=effect
                )
                with self.store.serialize_disposable_testing_resources():
                    check()
                    if not resource_readback_passes(evidence, resources):
                        raise ValueError("Independent empty backing storage was not proved.")
                    result = {
                        **self.plan(),
                        "applied": True,
                        "resources_verified": True,
                        "resources_binding_sha256": resources.binding_sha256,
                        "volumes": resources.volumes,
                        "schedule_id": evidence.get("schedule_id", ""),
                    }
                return ProviderMutationOutcome(
                    202,
                    provider_operation_response_payload(
                        trace_id=self.trace_id, records={}, result=result
                    ),
                )
        except Exception as error:
            if not started:
                raise ProviderMutationRejectedError(
                    SeparationResourcesRefused("Preparation refused before provider work.")
                ) from error
            raise ProviderMutationUnknownError(
                "Preparation outcome is unknown; preserve its original key and resources."
            ) from error


def run_preparation_schedule(
    *,
    root: Path,
    resources: GenericWebTestingResources,
    before_effect: Callable[[str], None],
) -> dict[str, str]:
    host, token = source.read_dokploy_config(control_plane_root=root)
    payload = api.fetch_dokploy_target_payload(
        host=host, token=token, target_type="compose", target_id=resources.target_id
    )
    if payload.get("composeId") != resources.target_id:
        raise ValueError("Retained target placement identity changed.")
    kind, lookup, _, server = post_deploy._resolve_dokploy_schedule_runtime(
        host=host,
        token=token,
        compose_id=resources.target_id,
        compose_name=resources.context,
        target_payload=payload,
    )
    name = "Launchplane independent testing storage"
    existing = api.find_matching_dokploy_schedule(
        host=host,
        token=token,
        target_id=lookup,
        schedule_type=kind,
        schedule_name=name,
        app_name=resources.namespace,
    )
    # A foreign or interrupted schedule is never overwritten, even if disabled.
    if existing is not None:
        raise ValueError("Retain the existing preparation schedule and original receipt.")
    return post_deploy.run_manual_data_schedule(
        host=host,
        token=token,
        target_definition=source.DokployTargetDefinition(
            context=resources.context, instance="testing", target_id=resources.target_id
        ),
        schedule_lookup_id=lookup,
        schedule_type=kind,
        schedule_name=name,
        schedule_app_name=resources.namespace,
        schedule_payload={
            "name": name,
            "cronExpression": post_deploy.DOKPLOY_MANUAL_ONLY_CRON_EXPRESSION,
            "appName": resources.namespace,
            "shellType": "bash",
            "scheduleType": kind,
            "command": "control-plane independent testing storage",
            "script": resources.script("prepare"),
            "serverId": server,
            "userId": lookup if kind == "dokploy-server" else None,
            "enabled": False,
            "timezone": "UTC",
        },
        resolved_schedule_execution_timeout_seconds=300,
        before_provider_mutation=before_effect,
    )
