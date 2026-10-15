"""Bound an independently qualified testing generation to its own restore route."""

from dataclasses import dataclass
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.disposable_lane import (
    DisposableLaneSafety,
    TESTING_REPLACEMENT_BOOTSTRAP_ROUTE,
)
from control_plane.contracts.dokploy_target_record import PublicHostEffect
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.disposable_lanes import (
    disposable_lane_binding_sha256,
    disposable_lane_configuration,
)
from control_plane.dokploy import compose, source
from control_plane.dokploy_target_setup_http import fetch_dokploy_compose_domains_for_target_setup
from control_plane.live_target_runtime import evaluate_runtime_key_safety_for_live_target_sync
from control_plane.odoo_testing_resources import OdooTestingResources
from control_plane.odoo_testing_restore import (
    FreshOdooTestingGeneration,
    generation_binding,
    encoded_restore_source,
)
from control_plane.provider_operations import (
    ProviderMutationOutcome,
    ProviderObservation,
    ProviderOperationLease,
    provider_operation_response_payload,
)
from control_plane.runtime_environments import site_delivered_secret_bindings_from_store
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.product_authority_bundle import (
    ProductAuthorityBundle,
    ProviderTargetWrite,
)
from control_plane.testing_replacement_resources import (
    TestingReplacementResourcesRequest,
    resource_context,
    run_resource_schedule,
)
from control_plane.workflows.ship import utc_now_timestamp

ROUTE = TESTING_REPLACEMENT_BOOTSTRAP_ROUTE


def bootstrap_request_sha256(request: TestingReplacementResourcesRequest) -> str:
    return canonical_json_sha256(
        request.model_dump(mode="json", exclude={"mode", "reviewed_plan_sha256"})
    )


class TestingReplacementBootstrapRequest(TestingReplacementResourcesRequest):
    domain_host: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
    certificate_type: Literal["none", "letsencrypt"] = "none"


class TestingReplacementBootstrapPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product: str
    context: str
    instance: Literal["testing"] = "testing"
    operation_id: str
    domain_host: str
    source_instance: Literal["prod"] = "prod"
    plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    resources_binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    retained_target_preserved: Literal[True] = True
    production_modified: Literal[False] = False
    runtime_verified: Literal[False] = False
    reconcile_requested: bool = False
    applied: bool = False


def bootstrap_plan(
    store: PostgresRecordStore, product: str, request: TestingReplacementBootstrapRequest
) -> TestingReplacementBootstrapPlan:
    profile, operation, allocation, target, target_id, _ = resource_context(
        store, product=product, request=request
    )
    resources = OdooTestingResources(
        product=product,
        context=request.context,
        operation_id=operation.operation_id,
        target_id=allocation.replacement_target_id,
        namespace=allocation.resource_namespace,
    )
    if (
        allocation.resources_binding_sha256 != resources.binding_sha256
        or target_id.target_id != resources.target_id
    ):
        raise ValueError("Fresh resource qualification is incomplete.")
    lane = next(
        lane
        for lane in profile.lanes
        if (lane.context, lane.instance) == (request.context, "testing")
    )
    generation = store.read_fresh_odoo_testing_generation(product, request.context)
    if generation is not None and (
        generation.operation_id != operation.operation_id
        or generation.target_id != resources.target_id
        or generation.request_sha256 != bootstrap_request_sha256(request)
    ):
        raise ValueError("Bootstrap request differs from its recorded generation.")
    production = [lane for lane in profile.lanes if lane.instance == "prod"]
    if (
        len(production) != 1
        or not lane.odoo_data_policy.allows_rebuild_source("upstream_restore")
        or lane.odoo_data_policy.upstream_source != f"{product}:{production[0].context}:prod"
    ):
        raise ValueError("Prepare the testing-only same-product production restore policy first.")
    old_hosts = {urlsplit(value).hostname for value in (lane.base_url, lane.health_url)}
    old_hosts.update(target.domains)
    # The retained hostname is never moved or independently duplicated.
    if (generation is None and request.domain_host in old_hosts) or any(
        request.domain_host in {*other.domains, *other.public_hosts}
        and (generation is None or (other.context, other.instance) != (request.context, "testing"))
        for other in store.list_dokploy_target_records()
    ):
        raise ValueError("Fresh testing requires an independent, unbound hostname.")
    bindings = site_delivered_secret_bindings_from_store(
        record_store=store, context_name=request.context, instance_name="testing"
    )
    safety = evaluate_runtime_key_safety_for_live_target_sync(
        record_store=store,
        context_name=request.context,
        instance_name="testing",
        required_binding_keys=tuple(sorted(bindings)),
    )
    if safety.get("status") not in {"pass", "skipped"} or safety.get("reported"):
        raise ValueError("Testing credentials lack current non-production safety proof.")
    for binding in bindings.values():
        record = store.read_secret_record(binding.secret_id)
        if (
            record.scope != "context_instance"
            or record.context != request.context
            or record.instance != "testing"
        ):
            raise ValueError("Fresh testing cannot carry a shared or production credential.")
    source_values: dict[str, str] = {}
    for scope in ("context", "instance"):
        for runtime_record in store.list_runtime_environment_records(context_name=request.context):
            if runtime_record.scope == scope and runtime_record.instance == (
                "testing" if scope == "instance" else ""
            ):
                source_values.update({key: str(value) for key, value in runtime_record.env.items()})
    source_values.update({key: str(value) for key, value in target.env.items()})
    encoded_restore_source(source_values)
    digest = canonical_json_sha256(
        {
            "request": request.model_dump(mode="json", exclude={"mode", "reviewed_plan_sha256"}),
            "profile": profile.model_dump(mode="json"),
            "allocation": allocation.model_dump(mode="json"),
            "configuration": [
                item.model_dump(mode="json")
                for item in disposable_lane_configuration(store, request.context, "testing")
            ],
        }
    )
    return TestingReplacementBootstrapPlan(
        product=product,
        context=request.context,
        operation_id=operation.operation_id,
        domain_host=request.domain_host,
        plan_sha256=generation.bootstrap_plan_sha256 if generation is not None else digest,
        resources_binding_sha256=resources.binding_sha256,
    )


@dataclass
class TestingReplacementBootstrapAdapter:
    root: Path
    store: PostgresRecordStore
    product: str
    request: TestingReplacementBootstrapRequest
    trace_id: str

    def target_key(self) -> str:
        return f"testing-bootstrap:{self.product}:{self.request.context}"

    def reconciliation_key(self) -> str:
        return canonical_json_sha256(
            {"operation": self.request.operation_id, "domain": self.request.domain_host}
        )

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        # A completed record is adopted only after exact metadata/provider readback.
        try:
            result = self.readback()
        except (ValueError, RuntimeError, OSError):
            return ProviderObservation("unknown")
        return ProviderObservation(
            "present",
            response_payload=provider_operation_response_payload(
                trace_id=self.trace_id,
                records={"operation": self.request.operation_id},
                result=result,
            ),
        )

    def readback(self) -> dict[str, object]:
        generation = self.store.read_fresh_odoo_testing_generation(
            self.product, self.request.context
        )
        if (
            generation is None
            or generation.operation_id != self.request.operation_id
            or generation.domain_host != self.request.domain_host
        ):
            raise ValueError("Testing bootstrap generation is unavailable.")
        target_id = self.store.read_dokploy_target_id_record(
            context_name=self.request.context, instance_name="testing"
        )
        profile = self.store.read_product_profile_record(self.product)
        lane = next(
            lane
            for lane in profile.lanes
            if (lane.context, lane.instance) == (self.request.context, "testing")
        )
        if (
            target_id.target_id != generation.target_id
            or urlsplit(lane.base_url).hostname != generation.domain_host
        ):
            raise ValueError("Testing bootstrap authority has changed.")
        host, token = source.read_dokploy_config(control_plane_root=self.root)
        domains = fetch_dokploy_compose_domains_for_target_setup(
            host=host, token=token, compose_id=generation.target_id
        )
        if {str(item.get("host") or "") for item in domains} != {generation.domain_host}:
            raise ValueError("The independent testing route is unverified.")
        operation = self.store.read_odoo_stable_target_replacement_operation_record(
            generation.operation_id
        )
        if (
            operation.status != "superseded"
            or operation.disposable_supersession is None
            or operation.disposable_supersession.replacement_target_id != generation.target_id
        ):
            raise ValueError("Testing bootstrap supersession is incomplete.")
        return TestingReplacementBootstrapPlan(
            product=self.product,
            context=self.request.context,
            operation_id=generation.operation_id,
            domain_host=generation.domain_host,
            plan_sha256=generation.bootstrap_plan_sha256,
            resources_binding_sha256=generation.resources_binding_sha256,
            applied=True,
            reconcile_requested=True,
        ).model_dump(mode="json")

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        with self.store.testing_replacement_execution_lock(self.request.operation_id) as old_check:
            if old_check is None:
                raise ValueError("The retained executor is active.")
            with self.store.serialize_disposable_testing_resources():
                plan = bootstrap_plan(self.store, self.product, self.request)
                if plan.plan_sha256 != self.request.reviewed_plan_sha256:
                    raise ValueError("The reviewed bootstrap plan changed.")
                profile, operation, allocation, target, target_id, provider = resource_context(
                    self.store, product=self.product, request=self.request
                )

            def check() -> None:
                lease.assert_current()
                old_check()
                with self.store.serialize_disposable_testing_resources():
                    if bootstrap_plan(self.store, self.product, self.request) != plan:
                        raise ValueError("Testing bootstrap metadata changed.")

            resources = OdooTestingResources(
                product=self.product,
                context=self.request.context,
                operation_id=operation.operation_id,
                target_id=target_id.target_id,
                namespace=allocation.resource_namespace,
            )
            existing_generation = self.store.read_fresh_odoo_testing_generation(
                self.product, self.request.context
            )
            run_resource_schedule(
                root=self.root,
                store=self.store,
                resources=resources,
                mode="readback",
                checkpoint=check,
                allowed_domain_hosts=(existing_generation.domain_host,)
                if existing_generation
                else (),
            )
            generation = existing_generation or FreshOdooTestingGeneration(
                product=self.product,
                context=self.request.context,
                operation_id=operation.operation_id,
                target_id=target_id.target_id,
                namespace=allocation.resource_namespace,
                domain_host=self.request.domain_host,
                resources_binding_sha256=resources.binding_sha256,
                bootstrap_plan_sha256=plan.plan_sha256,
                request_sha256=bootstrap_request_sha256(self.request),
                certificate_type=self.request.certificate_type,
                prepared_at=utc_now_timestamp(),
            )
            self.store.write_fresh_odoo_testing_generation(generation)
            host, token = source.read_dokploy_config(control_plane_root=self.root)
            lane = next(
                lane
                for lane in profile.lanes
                if (lane.context, lane.instance) == (self.request.context, "testing")
            )
            base_url = f"https://{generation.domain_host}"
            health_path = urlsplit(lane.health_url).path or "/launchplane/health"
            updated_profile = profile.model_copy(
                update={
                    "lanes": tuple(
                        candidate.model_copy(
                            update={"base_url": base_url, "health_url": base_url + health_path}
                        )
                        if candidate == lane
                        else candidate
                        for candidate in profile.lanes
                    ),
                    "updated_at": utc_now_timestamp(),
                }
            )
            updated_profile.validate_write_contract()
            updated_target = target.model_copy(
                update={
                    "domains": (generation.domain_host,),
                    "source_type": "raw",
                    "updated_at": utc_now_timestamp(),
                }
            )
            updated_provider = ProviderTargetRecord.from_dokploy_records(
                target_record=updated_target, target_id_record=target_id
            )

            def route(checkpoint: Callable[[str, str, int], None]) -> None:
                lease.checkpoint_effect("bind_independent_testing_route")
                domain_id = compose.ensure_compose_web_domain_route(
                    host=host,
                    token=token,
                    compose_id=generation.target_id,
                    domain_host=generation.domain_host,
                    runtime_port=profile.runtime_port,
                    certificate_type=generation.certificate_type,
                )
                checkpoint(generation.domain_host, domain_id, profile.runtime_port)

            check()
            self.store.write_product_public_hosts_bundle(
                ProductAuthorityBundle(
                    required_context_owner=(self.product, self.request.context),
                    expected_product_profiles=(profile,),
                    product_profiles=(updated_profile,),
                    dokploy_targets=(updated_target,),
                    provider_target_writes=(
                        ProviderTargetWrite(record=updated_provider, expected_record=provider),
                    ),
                ),
                expected_target=target,
                expected_target_id=target_id,
                expected_provider_target=provider,
                apply_provider=route,
                pending_effects=(
                    PublicHostEffect(
                        host=generation.domain_host,
                        provider_host=host,
                        target_id=generation.target_id,
                        runtime_port=profile.runtime_port,
                    ),
                ),
            )
            with self.store.serialize_disposable_testing_resources():
                old_check()
                current_profile = self.store.read_product_profile_record(self.product)
                current_target = self.store.read_dokploy_target_record(
                    context_name=self.request.context, instance_name="testing"
                )
                binding = disposable_lane_binding_sha256(
                    current_profile,
                    current_target,
                    target_id,
                    configuration=disposable_lane_configuration(
                        self.store, self.request.context, "testing"
                    ),
                )
                now = datetime.now(timezone.utc)
                reference = "service:fresh-testing-bootstrap:" + generation_binding(generation)
                self.store.write_disposable_lane_safety(
                    DisposableLaneSafety(
                        product=self.product,
                        context=self.request.context,
                        instance="testing",
                        observed_by="service:fresh-testing-bootstrap",
                        observer_kind="director_agent",
                        binding_sha256=binding,
                        usage="unused",
                        data="reproducible",
                        production_resources="isolated",
                        usage_evidence=reference,
                        data_evidence=reference,
                        isolation_evidence=reference,
                        replaces_operation_id=operation.operation_id,
                        replaced_target_id=allocation.retained_target_id,
                        observed_at=now,
                        expires_at=now + timedelta(minutes=5),
                    )
                )
            # The supersession transaction has its own complete metadata/alias
            # exclusion; the historical operation is retained without safe_to_release.
        if not self.store.supersede_disposable_testing_replacement(operation):
            raise ValueError("The retained testing fence could not be superseded.")
        from control_plane.contracts.product_reconcile import ProductReconcileTarget

        self.store.request_product_reconcile(
            ProductReconcileTarget(product=self.product, target_kind="testing"), utc_now_timestamp()
        )
        return ProviderMutationOutcome(
            202,
            provider_operation_response_payload(
                trace_id=self.trace_id,
                records={"operation": operation.operation_id},
                result=plan.model_copy(
                    update={"applied": True, "reconcile_requested": True}
                ).model_dump(mode="json"),
            ),
        )
