"""Create empty replacement resources while retaining the old testing target."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.disposable_lane import (
    DisposableLaneSafety,
    TESTING_REPLACEMENT_RESOURCES_ROUTE,
)
from control_plane.contracts.disposable_lane import DisposableTestingResourcePreparation
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    is_exclusive_product_context,
    product_context_owner_map,
)
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.disposable_lanes import (
    disposable_lane_binding_sha256,
    disposable_lane_configuration,
)
from control_plane.dokploy import api, post_deploy, source
from control_plane.dokploy_target_setup_http import (
    DokployTargetSetupEnvelope,
    execute_dokploy_target_setup,
    fetch_dokploy_compose_domains_for_target_setup,
)
from control_plane.odoo_testing_resources import OdooTestingResources, resource_readback_passes
from control_plane.provider_operations import (
    ProviderMutationOutcome,
    ProviderMutationRejectedError,
    ProviderObservation,
    ProviderOperationLease,
    provider_operation_response_payload,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.product_authority_bundle import (
    ProductAuthorityBundle,
    RuntimeEnvironmentWrite,
)
from control_plane.testing_replacement_recovery import service_testing_operation
from control_plane.workflows.ship import utc_now_timestamp

ROUTE = TESTING_REPLACEMENT_RESOURCES_ROUTE


class TestingReplacementResourcesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    context: str = Field(min_length=1)
    instance: Literal["testing"] = "testing"
    operation_id: str = Field(min_length=1)
    mode: Literal["dry-run", "apply", "readback"] = "dry-run"
    reason: str = Field(min_length=1)
    reviewed_plan_sha256: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")


class TestingReplacementResourcesPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    context: str
    instance: Literal["testing"]
    operation_id: str
    plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    retained_target_preserved: Literal[True]
    fresh_storage_required: Literal[True]
    published_host_ports: Literal[False]
    host_ssh_mounts: Literal[False]
    restore_started: Literal[False]
    applied: bool


class TestingReplacementResourcesResult(TestingReplacementResourcesPlan):
    resources_verified: Literal[True]
    resources_binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    schedule_id: str = Field(min_length=1)


class TestingReplacementResourcesReadback(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str = Field(min_length=1)
    context: str = Field(min_length=1)
    instance: Literal["testing"]
    operation_id: str = Field(min_length=1)
    resources_verified: Literal[True]
    resources_binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    retained_target_preserved: Literal[True]
    production_resources: Literal["separation_ready"]
    restore_started: Literal[False]
    schedule_id: str = Field(min_length=1)


def resource_context(
    store: PostgresRecordStore,
    *,
    product: str,
    request: TestingReplacementResourcesRequest,
) -> tuple[
    LaunchplaneProductProfileRecord,
    OdooStableTargetReplacementOperationRecord,
    DisposableTestingResourcePreparation,
    DokployTargetRecord,
    DokployTargetIdRecord,
    ProviderTargetRecord,
]:
    profile = store.read_product_profile_record(product)
    if (
        profile.driver_id != "odoo"
        or profile.lifecycle_state != "active"
        or not is_exclusive_product_context(
            context=request.context,
            product=product,
            owners=product_context_owner_map(store.list_product_profile_records()),
        )
        or len(
            [
                lane
                for lane in profile.lanes
                if (lane.context, lane.instance) == (request.context, "testing")
            ]
        )
        != 1
    ):
        raise ValueError("Replacement resources require an exact active owned Odoo testing lane.")
    operation = store.read_odoo_stable_target_replacement_operation_record(request.operation_id)
    if (
        not service_testing_operation(operation)
        or operation.status != "reconciliation_required"
        or (operation.product, operation.context, operation.instance)
        != (product, request.context, "testing")
    ):
        raise ValueError("The retained testing operation is unavailable or changed.")
    preparation = store.read_disposable_resource_preparation(operation.operation_id)
    if preparation is None or not preparation.resource_namespace:
        raise ValueError("Capture the retained target before preparing replacement resources.")
    target = store.read_dokploy_target_record(context_name=request.context, instance_name="testing")
    target_id = store.read_dokploy_target_id_record(
        context_name=request.context, instance_name="testing"
    )
    provider = store.read_provider_target_record(
        context_name=request.context, instance_name="testing"
    )
    if target.target_type != "compose" or provider.target_id != target_id.target_id:
        raise ValueError("The tracked testing target does not agree with its provider binding.")
    if target.policies.staff_testing_hold is not None:
        raise ValueError("A staff-testing hold protects this lane.")
    expected_target_id = preparation.replacement_target_id or preparation.retained_target_id
    if (
        target_id.target_id != expected_target_id
        or any(
            item.target_id == target_id.target_id
            and (item.context, item.instance) != (request.context, "testing")
            for item in store.list_dokploy_target_id_records()
        )
        or any(
            item.provider_id == "dokploy"
            and item.target_id == target_id.target_id
            and (item.context, item.instance) != (request.context, "testing")
            for item in store.list_physical_provider_target_records()
        )
    ):
        raise ValueError("The testing target changed or aliases another lane.")
    return profile, operation, preparation, target, target_id, provider


def resource_plan(
    store: PostgresRecordStore, *, product: str, request: TestingReplacementResourcesRequest
) -> dict[str, object]:
    profile, operation, preparation, target, target_id, _ = resource_context(
        store, product=product, request=request
    )
    binding = disposable_lane_binding_sha256(
        profile,
        target,
        target_id,
        configuration=disposable_lane_configuration(store, request.context, "testing"),
    )
    digest = canonical_json_sha256(
        {
            "product": product,
            "request": request.model_dump(exclude={"mode", "reviewed_plan_sha256"}),
            "binding": binding,
            "preparation": preparation.model_dump(mode="json"),
            "operation": operation.model_dump(mode="json"),
        }
    )
    return {
        "product": product,
        "context": request.context,
        "instance": "testing",
        "operation_id": operation.operation_id,
        "plan_sha256": digest,
        "retained_target_preserved": True,
        "fresh_storage_required": True,
        "published_host_ports": False,
        "host_ssh_mounts": False,
        "restore_started": False,
        "applied": False,
    }


def run_resource_schedule(
    *,
    root: Path,
    store: PostgresRecordStore,
    resources: OdooTestingResources,
    mode: Literal["prepare", "readback"],
    checkpoint: Callable[[], None],
) -> dict[str, str]:
    target = store.read_dokploy_target_record(
        context_name=resources.context, instance_name="testing"
    )
    host, token = source.read_dokploy_config(control_plane_root=root)
    payload = api.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type="compose",
        target_id=resources.target_id,
    )
    # This stage has no application or credential. A caller cannot use resource
    # preparation as permission to inspect or overwrite an already running lane.
    if (
        payload.get("composeId") != resources.target_id
        or payload.get("appName") != resources.namespace
        or payload.get("sourceType") != "raw"
        or payload.get("env") not in (None, "")
        or payload.get("composeFile") not in (None, "")
        or payload.get("autoDeploy") not in (None, False)
        or api.list_deployments_for_target(
            host=host, token=token, target_type="compose", target_id=resources.target_id
        )
    ):
        raise ValueError("Resource preparation requires the service-created empty compose.")
    if fetch_dokploy_compose_domains_for_target_setup(
        host=host, token=token, compose_id=resources.target_id
    ):
        raise ValueError("The fresh compose already has a route.")
    schedule_type, lookup_id, app_name, server_id = post_deploy._resolve_dokploy_schedule_runtime(
        host=host,
        token=token,
        compose_id=resources.target_id,
        compose_name=target.target_name,
        target_payload=payload,
    )
    name = "Launchplane testing replacement resources"
    schedule_app_name = resources.namespace
    existing = api.find_matching_dokploy_schedule(
        host=host,
        token=token,
        target_id=lookup_id,
        schedule_type=schedule_type,
        schedule_name=name,
        app_name=schedule_app_name,
    )
    if post_deploy._has_running_schedule_deployment(existing):
        raise ValueError("Replacement resource inspection is already running.")
    checkpoint()
    evidence = post_deploy.run_manual_data_schedule(
        host=host,
        token=token,
        target_definition=source.DokployTargetDefinition(
            **target.model_dump(exclude={"schema_version", "updated_at", "source_label"}),
            target_id=resources.target_id,
        ),
        schedule_lookup_id=lookup_id,
        schedule_type=schedule_type,
        schedule_name=name,
        schedule_app_name=schedule_app_name,
        schedule_payload={
            "name": name,
            "cronExpression": post_deploy.DOKPLOY_MANUAL_ONLY_CRON_EXPRESSION,
            "appName": schedule_app_name,
            "shellType": "bash",
            "scheduleType": schedule_type,
            "command": "control-plane testing replacement resources",
            "script": resources.script(mode),
            "serverId": server_id,
            "userId": lookup_id if schedule_type == "dokploy-server" else None,
            "enabled": False,
            "timezone": "UTC",
        },
        resolved_schedule_execution_timeout_seconds=300,
        before_provider_mutation=lambda _phase: checkpoint(),
    )
    checkpoint()
    if not resource_readback_passes(evidence, resources):
        raise ValueError("Replacement storage did not pass its actual backing-store readback.")
    return evidence


@dataclass
class TestingReplacementResourceAdapter:
    root: Path
    store: PostgresRecordStore
    product: str
    request: TestingReplacementResourcesRequest
    trace_id: str

    def target_key(self) -> str:
        return f"testing-replacement-resources:{self.product}:{self.request.context}"

    def reconciliation_key(self) -> str:
        return canonical_json_sha256(
            {"operation": self.request.operation_id, "product": self.product}
        )

    def readback(self) -> dict[str, object]:
        with self.store.testing_replacement_execution_lock(self.request.operation_id) as old_check:
            if old_check is None:
                raise ValueError("The retained executor is active.")
            with self.store.serialize_disposable_testing_resources() as metadata_check:
                profile, operation, preparation, target, target_id, _ = resource_context(
                    self.store, product=self.product, request=self.request
                )
                resources = OdooTestingResources(
                    product=self.product,
                    context=self.request.context,
                    operation_id=operation.operation_id,
                    target_id=preparation.replacement_target_id,
                    namespace=preparation.resource_namespace,
                )
                if preparation.resources_binding_sha256 != resources.binding_sha256:
                    raise ValueError("Replacement allocation has no matching completion receipt.")
                runtime = next(
                    iter(
                        self.store.list_runtime_environment_records(
                            scope="instance",
                            context_name=self.request.context,
                            instance_name="testing",
                        )
                    ),
                    None,
                )
                if runtime is None or any(
                    runtime.env.get(key) != value for key, value in resources.volumes.items()
                ):
                    raise ValueError("Testing volume authority changed after resource preparation.")

                def checkpoint() -> None:
                    old_check()
                    metadata_check()
                    resource_context(self.store, product=self.product, request=self.request)

                evidence = run_resource_schedule(
                    root=self.root,
                    store=self.store,
                    resources=resources,
                    mode="readback",
                    checkpoint=checkpoint,
                )
                checkpoint()
                binding = disposable_lane_binding_sha256(
                    profile,
                    target,
                    target_id,
                    configuration=disposable_lane_configuration(
                        self.store, self.request.context, "testing"
                    ),
                )
                now = datetime.now(timezone.utc)
                reference = f"service:testing-resources:{resources.binding_sha256}"
                self.store.write_disposable_lane_safety(
                    DisposableLaneSafety(
                        product=self.product,
                        context=self.request.context,
                        instance="testing",
                        observed_by="service:testing-replacement-resources",
                        observer_kind="director_agent",
                        binding_sha256=binding,
                        usage="unused",
                        data="reproducible",
                        production_resources="separation_ready",
                        usage_evidence=reference,
                        data_evidence=reference,
                        isolation_evidence=reference,
                        replaces_operation_id=operation.operation_id,
                        replaced_target_id=preparation.retained_target_id,
                        observed_at=now,
                        expires_at=now + timedelta(minutes=5),
                    )
                )
                return {
                    "product": self.product,
                    "context": self.request.context,
                    "instance": "testing",
                    "operation_id": operation.operation_id,
                    "resources_verified": True,
                    "resources_binding_sha256": resources.binding_sha256,
                    "binding_sha256": binding,
                    "retained_target_preserved": True,
                    "production_resources": "separation_ready",
                    "restore_started": False,
                    "schedule_id": evidence.get("schedule_id", ""),
                }

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        # An interrupted create may have made an unrecorded provider object. Do
        # not infer absence or repeat it from a missing local receipt. A complete
        # service allocation can be adopted only after a new actual readback.
        try:
            verified = self.readback()
        except (ValueError, FileNotFoundError):
            return ProviderObservation("unknown")
        return ProviderObservation(
            "present",
            response_payload=provider_operation_response_payload(
                trace_id=self.trace_id,
                records={"operation": self.request.operation_id},
                result={
                    "product": self.product,
                    "context": self.request.context,
                    "instance": "testing",
                    "operation_id": self.request.operation_id,
                    "plan_sha256": self.request.reviewed_plan_sha256,
                    "retained_target_preserved": True,
                    "fresh_storage_required": True,
                    "published_host_ports": False,
                    "host_ssh_mounts": False,
                    "restore_started": False,
                    "applied": True,
                    "resources_verified": True,
                    "resources_binding_sha256": verified["resources_binding_sha256"],
                    "schedule_id": verified["schedule_id"],
                },
            ),
        )

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        with self.store.testing_replacement_execution_lock(
            self.request.operation_id
        ) as old_checkpoint:
            if old_checkpoint is None:
                raise ProviderMutationRejectedError(ValueError("The retained executor is active."))
            with self.store.serialize_disposable_testing_resources() as metadata_checkpoint:
                try:
                    plan = resource_plan(self.store, product=self.product, request=self.request)
                except (ValueError, FileNotFoundError) as error:
                    raise ProviderMutationRejectedError(
                        ValueError("The owned testing lane or retained operation changed.")
                    ) from error
                if plan["plan_sha256"] != self.request.reviewed_plan_sha256:
                    raise ProviderMutationRejectedError(
                        ValueError("The reviewed resource plan changed.")
                    )
                profile, operation, preparation, target, target_id, provider = resource_context(
                    self.store, product=self.product, request=self.request
                )

                def checkpoint() -> None:
                    lease.assert_current()
                    old_checkpoint()
                    metadata_checkpoint()
                    resource_context(self.store, product=self.product, request=self.request)

                if not preparation.replacement_target_id:
                    host, token = source.read_dokploy_config(control_plane_root=self.root)
                    old_payload = api.fetch_dokploy_target_payload(
                        host=host,
                        token=token,
                        target_type="compose",
                        target_id=target_id.target_id,
                    )
                    server_id = str(old_payload.get("serverId") or "").strip()
                    if not server_id:
                        raise ProviderMutationRejectedError(
                            ValueError("Explicit testing placement is unavailable.")
                        )
                    lease.checkpoint_effect("create_empty_replacement")
                    execute_dokploy_target_setup(
                        control_plane_root_path=self.root,
                        record_store=self.store,
                        request=DokployTargetSetupEnvelope(
                            mode="apply",
                            operation="create-compose",
                            context=self.request.context,
                            instance="testing",
                            target_name=preparation.resource_namespace,
                            project_name=preparation.resource_namespace,
                            environment_name=preparation.resource_namespace,
                            server_id=server_id,
                            expected_current_provider_target=provider.to_deployed_target_reference(),
                            replaces_operation_id=operation.operation_id,
                            reason=self.request.reason,
                        ),
                        required_context_owner=(self.product, self.request.context),
                        disposable_checkpoint=checkpoint,
                    )
                    new_id = self.store.read_dokploy_target_id_record(
                        context_name=self.request.context, instance_name="testing"
                    )
                    if new_id.target_id == preparation.retained_target_id:
                        raise ValueError("Replacement creation retained the old target identity.")
                    updated = preparation.model_copy(
                        update={"replacement_target_id": new_id.target_id}
                    )
                    self.store.compare_and_write_disposable_resource_preparation(
                        expected=preparation,
                        replacement=updated,
                    )
                    preparation = updated
                resources = OdooTestingResources(
                    product=self.product,
                    context=self.request.context,
                    operation_id=operation.operation_id,
                    target_id=preparation.replacement_target_id,
                    namespace=preparation.resource_namespace,
                )
                lease.checkpoint_effect("prepare_fresh_storage")
                evidence = run_resource_schedule(
                    root=self.root,
                    store=self.store,
                    resources=resources,
                    mode="prepare",
                    checkpoint=checkpoint,
                )
                current_runtime = next(
                    iter(
                        self.store.list_runtime_environment_records(
                            scope="instance",
                            context_name=self.request.context,
                            instance_name="testing",
                        )
                    ),
                    None,
                )
                stamp = utc_now_timestamp()
                runtime = RuntimeEnvironmentRecord(
                    **(
                        {
                            **current_runtime.model_dump(),
                            "env": {**current_runtime.env, **resources.volumes},
                            "updated_at": stamp,
                            "source_label": "service:testing-replacement-resources",
                        }
                        if current_runtime
                        else {
                            "scope": "instance",
                            "context": self.request.context,
                            "instance": "testing",
                            "env": resources.volumes,
                            "updated_at": stamp,
                            "source_label": "service:testing-replacement-resources",
                        }
                    )
                )
                checkpoint()
                self.store.write_product_authority_bundle(
                    ProductAuthorityBundle(
                        required_context_owner=(self.product, self.request.context),
                        expected_product_profiles=(profile,),
                        runtime_environment_writes=(
                            RuntimeEnvironmentWrite(
                                record=runtime,
                                expected_record=current_runtime,
                                expected_absent=current_runtime is None,
                            ),
                        ),
                    )
                )
                updated = preparation.model_copy(
                    update={"resources_binding_sha256": resources.binding_sha256}
                )
                self.store.compare_and_write_disposable_resource_preparation(
                    expected=preparation, replacement=updated
                )
                checkpoint()
                current_target = self.store.read_dokploy_target_record(
                    context_name=self.request.context, instance_name="testing"
                )
                current_id = self.store.read_dokploy_target_id_record(
                    context_name=self.request.context, instance_name="testing"
                )
                binding = disposable_lane_binding_sha256(
                    profile,
                    current_target,
                    current_id,
                    configuration=disposable_lane_configuration(
                        self.store, self.request.context, "testing"
                    ),
                )
                now = datetime.now(timezone.utc)
                reference = f"service:testing-resources:{resources.binding_sha256}"
                self.store.write_disposable_lane_safety(
                    DisposableLaneSafety(
                        product=self.product,
                        context=self.request.context,
                        instance="testing",
                        observed_by="service:testing-replacement-resources",
                        observer_kind="director_agent",
                        binding_sha256=binding,
                        usage="unused",
                        data="reproducible",
                        production_resources="separation_ready",
                        usage_evidence=reference,
                        data_evidence=reference,
                        isolation_evidence=reference,
                        replaces_operation_id=operation.operation_id,
                        replaced_target_id=preparation.retained_target_id,
                        observed_at=now,
                        expires_at=now + timedelta(minutes=5),
                    )
                )
                return ProviderMutationOutcome(
                    202,
                    provider_operation_response_payload(
                        trace_id=self.trace_id,
                        records={"operation": operation.operation_id},
                        result={
                            **plan,
                            "applied": True,
                            "resources_verified": True,
                            "resources_binding_sha256": resources.binding_sha256,
                            "schedule_id": evidence.get("schedule_id", ""),
                        },
                    ),
                )
