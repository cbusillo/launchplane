"""Reviewed, value-free browser synchronization of a generic-web stable lane."""

from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
import json
from pathlib import Path
from typing import cast

import click
from pydantic import BaseModel, ConfigDict

from control_plane import live_target_runtime as runtime
from control_plane import secrets
from control_plane.contracts.product_profile_record import product_context_owner_map
from control_plane.contracts.runtime_sync import (
    RuntimeSyncAvailability,
    RuntimeSyncModeAvailability,
    RuntimeSyncReview,
)
from control_plane.contracts.secret_record import SecretBinding
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.disposable_lanes import disposable_lane_configuration, DisposableLaneStore
from control_plane.dokploy import api as dokploy_api, source as dokploy_source
from control_plane.integration_allowances import canonical_sha256
from control_plane.provider_operations import (
    ProviderMutationOutcome,
    ProviderMutationRejectedError,
    ProviderMutationUnknownError,
    ProviderObservation,
    ProviderOperationLease,
    provider_operation_response_payload,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.generic_web_deploy_provider import (
    dokploy_generic_web_provider_target_key,
)


class RuntimeSyncRefused(ValueError):
    def __init__(self, code: str = "runtime_sync_unavailable") -> None:
        super().__init__("Runtime sync requires a fresh review of an available lane.")
        self.code = code


def _stale_payload(trace_id: str) -> dict[str, object]:
    return {
        "trace_id": trace_id,
        "error": {"code": "stale", "message": "Runtime sync inputs changed; review again."},
    }


class RuntimeSyncIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review: RuntimeSyncReview
    authority_commitment: str
    desired_commitment: str
    target_commitment: str
    baseline_commitment: str
    target_key: str


def _private_commitment(value: object, purpose: str) -> str:
    return secrets.keyed_secret_payload_fingerprint(
        json.dumps(value, sort_keys=True, separators=(",", ":")), purpose=purpose
    )


def require_runtime_sync_lane(
    store: PostgresRecordStore, product: str, context: str, instance: str
) -> None:
    profile = store.read_product_profile_record(product)
    physical: tuple[ProviderTargetRecord | DokployTargetIdRecord, ...] = (
        *store.list_dokploy_target_id_records(),
        *store.list_provider_target_records(),
    )
    if (
        not profile.is_active
        or profile.driver_id != "generic-web"
        or not any(lane.context == context and lane.instance == instance for lane in profile.lanes)
        or product_context_owner_map(store.list_product_profile_records()).get(context)
        != frozenset({product})
    ):
        raise RuntimeSyncRefused("runtime_sync_lane_unqualified")
    target = store.read_dokploy_target_record(context_name=context, instance_name=instance)
    target_id = store.read_dokploy_target_id_record(context_name=context, instance_name=instance)
    provider = store.read_provider_target_record(context_name=context, instance_name=instance)

    def aliases_recipient(item: ProviderTargetRecord | DokployTargetIdRecord) -> bool:
        if item.target_id != target_id.target_id or (item.context, item.instance) == (
            context,
            instance,
        ):
            return False
        if isinstance(item, ProviderTargetRecord):
            if item.provider_id != provider.provider_id:
                return False
            # An unknown provider category cannot prove a separate namespace.
            return not item.provider_target_type or item.provider_target_type == target.target_type
        definition = store.read_dokploy_target_record(
            context_name=item.context, instance_name=item.instance
        )
        return definition.target_type == target.target_type

    if (
        not target_id.target_id
        or provider.provider_id != "dokploy"
        or provider.target_id != target_id.target_id
        or provider.provider_target_type != target.target_type
        or provider.target_category != target.target_type
        or any(aliases_recipient(item) for item in physical)
    ):
        raise RuntimeSyncRefused("runtime_sync_lane_unqualified")


def runtime_sync_availability(
    store: object, *, administrator: bool, product: str, context: str, instance: str
) -> RuntimeSyncAvailability:
    reason = "A signed-in administrator is required."
    if administrator:
        reason = "Runtime sync requires complete, exclusively owned generic-web lane records."
        if isinstance(store, PostgresRecordStore):
            try:
                profile = store.read_product_profile_record(product)
                if profile.driver_id != "generic-web":
                    reason = "Runtime sync is not supported for this lane yet."
                else:
                    require_runtime_sync_lane(store, product, context, instance)
                    secrets.validate_secret_key_configuration()
                    dokploy_source.read_dokploy_config(
                        control_plane_root=Path("."), database_url=store.database_url
                    )
                    reason = ""
            except (ValueError, FileNotFoundError, click.ClickException):
                pass
    availability = RuntimeSyncModeAvailability(available=not reason, reason=reason)
    return RuntimeSyncAvailability(plan=availability, apply=availability.model_copy())


def _authority(store: PostgresRecordStore, request: runtime.LiveTargetRuntimeApplyEnvelope) -> str:
    require_runtime_sync_lane(store, request.product, request.context, request.instance)
    records = (
        store.read_product_profile_record(request.product),
        store.read_dokploy_target_record(
            context_name=request.context, instance_name=request.instance
        ),
        store.read_dokploy_target_id_record(
            context_name=request.context, instance_name=request.instance
        ),
        store.read_provider_target_record(
            context_name=request.context, instance_name=request.instance
        ),
        *disposable_lane_configuration(
            cast(DisposableLaneStore, store), request.context, request.instance
        ),
        *store.list_runtime_key_safety_policy_records(status="active", limit=1),
    )
    return _private_commitment(
        [item.model_dump(mode="json") for item in records], "browser-runtime-sync-authority"
    )


@dataclass(frozen=True)
class PreparedRuntimeSync:
    prepared: runtime.PreparedLiveTargetRuntime
    intent: RuntimeSyncIntent
    safety_keys: tuple[str, ...]
    suppliers: tuple[SecretBinding, ...]


def _key_safety(store: PostgresRecordStore, captured: PreparedRuntimeSync) -> None:
    runtime.evaluate_runtime_key_safety_for_live_target_sync(
        record_store=store,
        context_name=captured.intent.review.context,
        instance_name=captured.intent.review.instance,
        required_binding_keys=captured.safety_keys,
        secret_bindings=captured.suppliers,
    )


def prepare_runtime_sync(
    store: PostgresRecordStore, root: Path, request: runtime.LiveTargetRuntimeApplyEnvelope
) -> PreparedRuntimeSync:
    authority = _authority(store, request)
    prepared = runtime.prepare_live_target_runtime_environment(
        control_plane_root=root,
        database_url=store.database_url,
        product_name=request.product,
        context_name=request.context,
        instance_name=request.instance,
    )
    provider = store.read_provider_target_record(
        context_name=request.context, instance_name=request.instance
    )
    desired = _private_commitment(
        [authority, prepared.desired_env_map, sorted(prepared.retired_keys)],
        "browser-runtime-sync-desired",
    )
    target = _private_commitment(
        [
            prepared.host,
            provider.model_dump(mode="json"),
            prepared.target_definition.target_id,
            prepared.target_definition.target_type,
        ],
        "browser-runtime-sync-target",
    )
    baseline = _private_commitment(prepared.live_env_map, "browser-runtime-sync-baseline")
    delta = runtime.runtime_env_live_target_delta(
        desired_env_map=prepared.desired_env_map,
        live_env_map=prepared.live_env_map,
        retired_keys=prepared.retired_keys,
    )
    definition = prepared.target_definition
    evidence = {
        "operation": "runtime-sync",
        "schema_version": 1,
        "product": request.product,
        "context": request.context,
        "instance": request.instance,
        "target": target,
        "desired": desired,
        "baseline": baseline,
        "deploy": False,
        "delta": delta,
    }
    review = RuntimeSyncReview(
        product=request.product,
        context=request.context,
        instance=request.instance,
        plan_sha256=canonical_sha256(evidence),
        target_fingerprint=target,
        target_id=definition.target_id,
        target_type=definition.target_type,
        target_name=definition.target_name,
        missing_keys=tuple(cast(list[str], delta["missing_keys"])),
        different_keys=tuple(cast(list[str], delta["different_keys"])),
        changed_keys=tuple(cast(list[str], delta["changed_keys"])),
        retired_keys_present=tuple(cast(list[str], delta.get("retired_keys_present", []))),
        desired_key_count=cast(int, delta["desired_key_count"]),
        live_key_count=cast(int, delta["live_key_count"]),
        unchanged_key_count=cast(int, delta["unchanged_key_count"]),
    )
    suppliers = store.list_secret_bindings(integration="runtime_environment", limit=None)
    required = runtime.require_product_profile_runtime_secret_keys(
        record_store=store,
        product_name=request.product,
        context_name=request.context,
        instance_name=request.instance,
    )
    transported = runtime.transported_runtime_secret_keys(
        record_store=store,
        context_name=request.context,
        instance_name=request.instance,
        transported_keys=prepared.desired_env_map,
    )
    captured = PreparedRuntimeSync(
        prepared,
        RuntimeSyncIntent(
            review=review,
            authority_commitment=authority,
            desired_commitment=desired,
            target_commitment=target,
            baseline_commitment=baseline,
            target_key=dokploy_generic_web_provider_target_key(provider),
        ),
        tuple(sorted(required | transported)),
        tuple(suppliers),
    )
    _key_safety(store, captured)
    if _authority(store, request) != authority:
        raise RuntimeSyncRefused("stale")
    return captured


def runtime_sync_result(captured: PreparedRuntimeSync, *, applied: bool) -> dict[str, object]:
    result = runtime.apply_prepared_live_target_runtime_environment(
        captured.prepared,
        apply_changes=False,
        deploy=False,
        no_cache=False,
        deploy_timeout_seconds=None,
        deploy_trigger=runtime.trigger_and_wait_for_dokploy_target_deploy,
    )
    result["runtime_sync_review"] = captured.intent.review.model_copy(
        update={"applied": applied}
    ).model_dump(mode="json")
    return result


@dataclass
class ReviewedRuntimeSync:
    store: PostgresRecordStore
    root: Path
    request: runtime.LiveTargetRuntimeApplyEnvelope
    intent: RuntimeSyncIntent
    trace_id: str

    def target_key(self) -> str:
        return self.intent.target_key

    def reconciliation_key(self) -> str:
        return canonical_sha256([self.intent.desired_commitment, self.intent.target_commitment])

    @contextmanager
    def guard(self) -> Iterator[None]:
        with (
            self.store.serialize_disposable_testing_resources()
            if self.store.database_dialect_name == "postgresql"
            else nullcontext()
        ):
            yield

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        try:
            if reconciliation_key != self.reconciliation_key():
                return ProviderObservation(outcome="unknown")
            if (
                self.request.browser_review is None
                or self.request.browser_review.reviewed_plan_sha256
                != self.intent.review.plan_sha256
            ):
                if provider_effect_phase:
                    return ProviderObservation(outcome="unknown")
                return ProviderObservation(
                    outcome="present",
                    response_status_code=409,
                    response_payload=_stale_payload(self.trace_id),
                )
            captured = prepare_runtime_sync(self.store, self.root, self.request)
            if (captured.intent.desired_commitment, captured.intent.target_commitment) != (
                self.intent.desired_commitment,
                self.intent.target_commitment,
            ):
                return ProviderObservation(outcome="unknown")
            if captured.intent.review.changed_keys:
                return ProviderObservation(outcome="unknown" if provider_effect_phase else "absent")
            result = runtime_sync_result(captured, applied=True)
            result["mode"] = "apply"
            result["apply"] = {
                "applied": True,
                "env_updated": bool(provider_effect_phase),
                "verification": {"status": "pass"},
                "recovered_by_readback": True,
            }
            result["runtime_sync_review"] = self.intent.review.model_copy(
                update={"applied": True}
            ).model_dump(mode="json")
            return ProviderObservation(
                outcome="present",
                response_payload=provider_operation_response_payload(
                    trace_id=self.trace_id,
                    records={},
                    result=result,
                ),
            )
        except Exception:
            return ProviderObservation(outcome="unknown")

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        effect_started = False
        try:
            if (
                self.request.browser_review is None
                or self.request.browser_review.reviewed_plan_sha256
                != self.intent.review.plan_sha256
            ):
                raise RuntimeSyncRefused("stale")
            captured = prepare_runtime_sync(self.store, self.root, self.request)
            if captured.intent != self.intent:
                raise RuntimeSyncRefused("stale")

            def check(baseline: dict[str, str]) -> None:
                nonlocal effect_started
                if (
                    _authority(self.store, self.request) != self.intent.authority_commitment
                    or dokploy_source.read_dokploy_config(
                        control_plane_root=self.root, database_url=self.store.database_url
                    )[0]
                    != captured.prepared.host
                    or _private_commitment(baseline, "browser-runtime-sync-baseline")
                    != self.intent.baseline_commitment
                ):
                    raise RuntimeSyncRefused("stale")
                _key_safety(self.store, captured)
                lease.checkpoint_effect("runtime_environment_sync")
                effect_started = True

            # An unchanged review still needs an authoritative pre-effect comparison;
            # it completes without manufacturing a provider write or effect checkpoint.
            if not captured.intent.review.changed_keys:
                with self.guard():
                    prepared = captured.prepared
                    payload = dokploy_api.fetch_dokploy_target_payload(
                        host=prepared.host,
                        token=prepared.token,
                        target_type=prepared.target_definition.target_type,
                        target_id=prepared.target_definition.target_id,
                    )
                    if (
                        _authority(self.store, self.request) != self.intent.authority_commitment
                        or dokploy_source.read_dokploy_config(
                            control_plane_root=self.root, database_url=self.store.database_url
                        )[0]
                        != captured.prepared.host
                    ):
                        raise RuntimeSyncRefused("stale")
                    if (
                        _private_commitment(
                            dokploy_api.parse_dokploy_env_text(str(payload.get("env") or "")),
                            "browser-runtime-sync-baseline",
                        )
                        != self.intent.baseline_commitment
                    ):
                        raise RuntimeSyncRefused("stale")
                    _key_safety(self.store, captured)
                result = runtime_sync_result(captured, applied=True)
                result["mode"] = "apply"
                result["apply"] = {
                    "applied": True,
                    "env_updated": False,
                    "verification": {"status": "pass"},
                }
            else:
                result = runtime.apply_prepared_live_target_runtime_environment(
                    captured.prepared,
                    apply_changes=True,
                    deploy=False,
                    no_cache=False,
                    deploy_timeout_seconds=None,
                    deploy_trigger=runtime.trigger_and_wait_for_dokploy_target_deploy,
                    provider_mutation_guard=self.guard,
                    before_provider_env_write=check,
                )
            result["runtime_sync_review"] = self.intent.review.model_copy(
                update={"applied": True}
            ).model_dump(mode="json")
        except Exception as error:
            if effect_started:
                raise ProviderMutationUnknownError("Runtime sync outcome is unknown.") from error
            if isinstance(error, RuntimeSyncRefused) and error.code == "stale":
                # Terminal evidence owns this key even if the old inputs later
                # return. A delayed retry cannot dispatch after the UI settles it.
                return ProviderMutationOutcome(
                    response_status_code=409,
                    provider_effect_performed=False,
                    response_payload=_stale_payload(self.trace_id),
                )
            refusal = error if isinstance(error, RuntimeSyncRefused) else RuntimeSyncRefused()
            raise ProviderMutationRejectedError(refusal) from error
        return ProviderMutationOutcome(
            response_status_code=202,
            provider_effect_performed=effect_started,
            response_payload=provider_operation_response_payload(
                trace_id=self.trace_id, records={}, result=result
            ),
        )
