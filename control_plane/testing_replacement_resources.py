"""Create empty replacement resources while retaining the old testing target."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections.abc import Callable
from typing import Literal
import re

import click

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
from control_plane.dokploy.target_source_setup import require_empty_compose_source
from control_plane.dokploy_target_setup_http import (
    DokployTargetSetupEnvelope,
    execute_dokploy_target_setup,
    fetch_dokploy_compose_domains_for_target_setup,
)
from control_plane.odoo_testing_resources import (
    OdooTestingResources,
    resource_readback_passes,
    TESTING_RESOURCES_INCOMPLETE_MARKER,
)
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
from control_plane.workflows.dokploy_target_adoption import (
    adopt_dokploy_target,
    PROJECT_CREATE_PATH,
    ENVIRONMENT_CREATE_PATH,
)

ROUTE = TESTING_REPLACEMENT_RESOURCES_ROUTE


class IncompleteTestingResources(ValueError):
    """Actual terminal readback found only absent, service-named empty resources."""


class TestingResourcePreparationRefused(ValueError):
    """A clean refusal before a provider effect; a fresh plan can proceed."""


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
    if target.pending_public_host_effects:
        raise ValueError(
            "Pending route effects must remain with their current target until reconciled."
        )
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
    allowed_domain_hosts: tuple[str, ...] = (),
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
    require_empty_compose_source(payload, resources.target_id)
    if (
        not re.fullmatch(
            re.escape(resources.namespace) + r"(?:-[a-z0-9]{6})?", str(payload.get("appName") or "")
        )
        or payload.get("env") not in (None, "")
        or api.list_deployments_for_target(
            host=host,
            token=token,
            target_type="compose",
            target_id=resources.target_id,
            require_complete=True,
        )
    ):
        raise ValueError("Resource preparation requires the service-created empty compose.")
    actual_domains = fetch_dokploy_compose_domains_for_target_setup(
        host=host, token=token, compose_id=resources.target_id
    )
    if any(str(item.get("host") or "") not in allowed_domain_hosts for item in actual_domains):
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
    try:
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
                "script": resources.script(mode, compose_project=app_name),
                "serverId": server_id,
                "userId": lookup_id if schedule_type == "dokploy-server" else None,
                "enabled": False,
                "timezone": "UTC",
            },
            resolved_schedule_execution_timeout_seconds=300,
            before_provider_mutation=lambda _phase: checkpoint(),
        )
    except post_deploy.OdooPostDeployReadbackFailure as error:
        if mode == "readback" and error.evidence.get(TESTING_RESOURCES_INCOMPLETE_MARKER) == "true":
            raise IncompleteTestingResources("Fresh storage preparation is incomplete.") from error
        raise
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

    def created_placement(self, namespace: str) -> tuple[str, str, str]:
        """Read the uniquely named creation, including incomplete placement.

        Absence alone never proves a dispatched create finished. The caller
        combines this inventory with the exact last dispatched phase.
        """
        host, token = source.read_dokploy_config(control_plane_root=self.root)
        matches = [
            project
            for project in api.fetch_dokploy_projects(host=host, token=token)
            if project.get("name") == namespace
        ]
        if not matches:
            return "", "", ""
        if len(matches) != 1 or not matches[0].get("projectId"):
            raise ValueError("Fresh project inventory is ambiguous.")
        project = matches[0]
        environments = project.get("environments")
        if not isinstance(environments, list):
            raise ValueError("Fresh project placement is not empty.")
        matching = []
        for candidate in environments:
            if not isinstance(candidate, dict):
                raise ValueError("Fresh environment inventory is malformed.")
            for key in ("applications", "postgres", "mysql", "mariadb", "mongo", "redis"):
                if candidate.get(key):
                    raise ValueError("Fresh placement contains another resource.")
            if candidate.get("name") == namespace:
                matching.append(candidate)
            elif any(candidate.get(key) not in (None, []) for key in ("compose", "composes")):
                raise ValueError("Fresh project contains another compose.")
        if not matching:
            return str(project["projectId"]), "", ""
        if len(matching) != 1 or not matching[0].get("environmentId"):
            raise ValueError("Fresh environment inventory is ambiguous.")
        environment = matching[0]
        wrappers = [environment[key] for key in ("compose", "composes") if key in environment]
        if len(wrappers) != 1:
            raise ValueError("Fresh compose inventory is incomplete or ambiguous.")
        composes = wrappers[0]
        if not isinstance(composes, list) or len(composes) > 1:
            raise ValueError("Fresh compose inventory is incomplete or ambiguous.")
        compose_id = ""
        if composes:
            compose = composes[0]
            if (
                not isinstance(compose, dict)
                or compose.get("name") != namespace
                or not compose.get("composeId")
            ):
                raise ValueError("Fresh compose inventory has another identity.")
            compose_id = str(compose["composeId"])
        return str(project["projectId"]), str(environment["environmentId"]), compose_id

    def capture_checkpoint(self, old_check: Callable[[], None]) -> None:
        old_check()
        with self.store.serialize_disposable_testing_resources():
            resource_context(self.store, product=self.product, request=self.request)

    def recover_created_target(self, phase: str) -> ProviderObservation | None:
        preparation = self.store.read_disposable_resource_preparation(self.request.operation_id)
        if preparation is None or preparation.replacement_target_id:
            return None
        with self.store.testing_replacement_execution_lock(self.request.operation_id) as old_check:
            if old_check is None:
                return ProviderObservation("unknown")
            self.capture_checkpoint(old_check)
            project_id, environment_id, compose_id = self.created_placement(
                preparation.resource_namespace
            )
            if not compose_id:
                terminal = (
                    not phase
                    and not project_id
                    or phase == PROJECT_CREATE_PATH
                    and bool(project_id)
                    and not environment_id
                    or phase == ENVIRONMENT_CREATE_PATH
                    and bool(environment_id)
                )
                return (
                    ProviderObservation("absent", retry_safe=True)
                    if terminal
                    else ProviderObservation("unknown")
                )
            if any(
                item.target_id == compose_id for item in self.store.list_dokploy_target_id_records()
            ) or any(
                item.provider_id == "dokploy" and item.target_id == compose_id
                for item in self.store.list_physical_provider_target_records()
            ):
                raise ValueError("Unrecorded creation is already bound to another target.")
            profile, _, _, _, _, provider = resource_context(
                self.store, product=self.product, request=self.request
            )
            host, token = source.read_dokploy_config(control_plane_root=self.root)
            payload = api.fetch_dokploy_target_payload(
                host=host, token=token, target_type="compose", target_id=compose_id
            )
            require_empty_compose_source(payload, compose_id)
            if (
                payload.get("environmentId") != environment_id
                or payload.get("env") not in (None, "")
                or not re.fullmatch(
                    re.escape(preparation.resource_namespace) + r"(?:-[a-z0-9]{6})?",
                    str(payload.get("appName") or ""),
                )
            ):
                raise ValueError("Unrecorded creation has changed or acquired credentials.")
            if api.list_deployments_for_target(
                host=host,
                token=token,
                target_type="compose",
                target_id=compose_id,
                require_complete=True,
            ) or fetch_dokploy_compose_domains_for_target_setup(
                host=host, token=token, compose_id=compose_id
            ):
                raise ValueError("Unrecorded creation has runtime or route history.")

            def check() -> None:
                self.capture_checkpoint(old_check)

            with self.store.capture_disposable_resource_adoption(preparation, checkpoint=check):
                adopt_dokploy_target(
                    record_store=self.store,
                    host=host,
                    token=token,
                    context=self.request.context,
                    instance="testing",
                    target_type="compose",
                    target_id=compose_id,
                    project_name=preparation.resource_namespace,
                    target_name=preparation.resource_namespace,
                    expected_current_provider_target=provider.to_deployed_target_reference(),
                    source_label="service:testing-replacement-resources:recovered-capture",
                    apply=True,
                    fetch_target_payload=lambda h, t, k, i: api.fetch_dokploy_target_payload(
                        host=h, token=t, target_type=k, target_id=i
                    ),
                    required_context_owner=(self.product, self.request.context),
                    expected_product_profile=profile,
                )
        return None

    def authority_sha256(self) -> str:
        return canonical_json_sha256(
            {
                "profile": self.store.read_product_profile_record(self.product).model_dump(
                    mode="json"
                ),
                "configuration": [
                    item.model_dump(mode="json")
                    for item in disposable_lane_configuration(
                        self.store, self.request.context, "testing"
                    )
                ],
            }
        )

    def configuration_sha256(self) -> str:
        return canonical_json_sha256(
            {
                "authority": self.authority_sha256(),
                "target": self.store.read_dokploy_target_record(
                    context_name=self.request.context, instance_name="testing"
                ).model_dump(mode="json"),
                "target_id": self.store.read_dokploy_target_id_record(
                    context_name=self.request.context, instance_name="testing"
                ).model_dump(mode="json"),
                "provider": self.store.read_provider_target_record(
                    context_name=self.request.context, instance_name="testing"
                ).model_dump(mode="json"),
            }
        )

    def checkpoint(self, expected: str, old_check: Callable[[], None]) -> None:
        old_check()
        with self.store.serialize_disposable_testing_resources():
            resource_context(self.store, product=self.product, request=self.request)
            if self.configuration_sha256() != expected:
                raise ValueError("Testing authority changed during resource qualification.")

    def publish(self, resources: OdooTestingResources, schedule_id: str) -> dict[str, object]:
        profile, operation, preparation, target, target_id, _ = resource_context(
            self.store, product=self.product, request=self.request
        )
        current_runtime = next(
            iter(
                self.store.list_runtime_environment_records(
                    scope="instance", context_name=self.request.context, instance_name="testing"
                )
            ),
            None,
        )
        if any(
            key in target.env and target.env[key] != value
            for key, value in resources.volumes.items()
        ):
            raise ValueError("Target volume overrides disagree with the qualified allocation.")
        if any(
            binding.status == "configured"
            and binding.binding_key in resources.volumes
            and binding.context in {"", self.request.context}
            and binding.instance in {"", "testing"}
            for binding in self.store.list_secret_bindings(
                integration="runtime_environment", limit=None
            )
        ):
            raise ValueError("Qualified volume names cannot be replaced by a secret binding.")
        if preparation.resources_binding_sha256:
            if preparation.resources_binding_sha256 != resources.binding_sha256 or (
                current_runtime is None
                or any(
                    current_runtime.env.get(key) != value
                    for key, value in resources.volumes.items()
                )
            ):
                raise ValueError("Recorded testing resource authority changed.")
        else:
            runtime = RuntimeEnvironmentRecord(
                **(
                    {
                        **current_runtime.model_dump(),
                        "env": {**current_runtime.env, **resources.volumes},
                        "updated_at": utc_now_timestamp(),
                        "source_label": "service:testing-replacement-resources",
                    }
                    if current_runtime
                    else {
                        "scope": "instance",
                        "context": self.request.context,
                        "instance": "testing",
                        "env": resources.volumes,
                        "updated_at": utc_now_timestamp(),
                        "source_label": "service:testing-replacement-resources",
                    }
                )
            )
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
            self.store.compare_and_write_disposable_resource_preparation(
                expected=preparation,
                replacement=preparation.model_copy(
                    update={
                        "resources_binding_sha256": resources.binding_sha256,
                    }
                ),
            )
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
        return TestingReplacementResourcesReadback(
            product=self.product,
            context=self.request.context,
            instance="testing",
            operation_id=operation.operation_id,
            resources_verified=True,
            resources_binding_sha256=resources.binding_sha256,
            binding_sha256=binding,
            retained_target_preserved=True,
            production_resources="separation_ready",
            restore_started=False,
            schedule_id=schedule_id,
        ).model_dump(mode="json")

    def readback(self) -> dict[str, object]:
        with self.store.testing_replacement_execution_lock(self.request.operation_id) as old_check:
            if old_check is None:
                raise ValueError("The retained executor is active.")
            with self.store.serialize_disposable_testing_resources():
                _, operation, preparation, _, _, _ = resource_context(
                    self.store, product=self.product, request=self.request
                )
                resources = OdooTestingResources(
                    product=self.product,
                    context=self.request.context,
                    operation_id=operation.operation_id,
                    target_id=preparation.replacement_target_id,
                    namespace=preparation.resource_namespace,
                )
                expected = self.configuration_sha256()

            def check() -> None:
                self.checkpoint(expected, old_check)

            evidence = run_resource_schedule(
                root=self.root,
                store=self.store,
                resources=resources,
                mode="readback",
                checkpoint=check,
            )
            with self.store.serialize_disposable_testing_resources():
                check()
                return self.publish(resources, evidence.get("schedule_id", ""))

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        # Observation never repeats a provider write. Completed host work can be
        # adopted without its lost HTTP/metadata receipt, using actual readback.
        try:
            recovered = self.recover_created_target(provider_effect_phase)
            if recovered is not None:
                return recovered
            verified = self.readback()
        except IncompleteTestingResources:
            preparation = self.store.read_disposable_resource_preparation(self.request.operation_id)
            if (
                preparation is not None
                and preparation.replacement_target_id
                and not preparation.resources_binding_sha256
            ):
                # Retry only the storage phase against the already captured
                # empty compose. Apply cannot create another target. Every
                # existing volume must match labels, backing, emptiness and
                # non-use; an active remote schedule refused before this proof.
                return ProviderObservation("absent", retry_safe=True)
            return ProviderObservation("unknown")
        except (ValueError, FileNotFoundError, click.ClickException, RuntimeError, OSError):
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
        with self.store.testing_replacement_execution_lock(self.request.operation_id) as old_check:
            if old_check is None:
                raise ProviderMutationRejectedError(
                    TestingResourcePreparationRefused("The retained executor is active.")
                )
            with self.store.serialize_disposable_testing_resources():
                try:
                    plan = resource_plan(self.store, product=self.product, request=self.request)
                    _, operation, preparation, _, target_id, provider = resource_context(
                        self.store, product=self.product, request=self.request
                    )
                except (ValueError, FileNotFoundError) as error:
                    raise ProviderMutationRejectedError(
                        TestingResourcePreparationRefused(
                            "The owned testing lane or retained operation changed."
                        )
                    ) from error
                if preparation.reviewed_plan_sha256:
                    if preparation.reviewed_plan_sha256 != self.request.reviewed_plan_sha256:
                        raise ProviderMutationRejectedError(
                            TestingResourcePreparationRefused(
                                "Use the original reviewed resource plan."
                            )
                        )
                    plan["plan_sha256"] = preparation.reviewed_plan_sha256
                elif plan["plan_sha256"] != self.request.reviewed_plan_sha256:
                    raise ProviderMutationRejectedError(
                        TestingResourcePreparationRefused("The reviewed resource plan changed.")
                    )
                else:
                    updated = preparation.model_copy(
                        update={"reviewed_plan_sha256": self.request.reviewed_plan_sha256}
                    )
                    self.store.compare_and_write_disposable_resource_preparation(
                        expected=preparation,
                        replacement=updated,
                    )
                    preparation = updated
                expected = self.configuration_sha256()
                expected_authority = self.authority_sha256()
                expected_retained = self.store.read_dokploy_target_record(
                    context_name=self.request.context, instance_name="testing"
                )

            def check() -> None:
                lease.assert_current()
                self.checkpoint(expected, old_check)

            if not preparation.replacement_target_id:
                placement = self.created_placement(preparation.resource_namespace)
                if placement[2]:
                    raise ValueError(
                        "Existing fresh compose requires original-key observation before capture."
                    )
                host, token = source.read_dokploy_config(control_plane_root=self.root)
                old_payload = api.fetch_dokploy_target_payload(
                    host=host, token=token, target_type="compose", target_id=target_id.target_id
                )
                server_id = str(old_payload.get("serverId") or "").strip()
                if not server_id:
                    raise ProviderMutationRejectedError(
                        TestingResourcePreparationRefused(
                            "Explicit testing placement is unavailable."
                        )
                    )

                def capture_check() -> None:
                    lease.assert_current()
                    self.capture_checkpoint(old_check)

                with self.store.capture_disposable_resource_adoption(
                    preparation, checkpoint=capture_check
                ):
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
                        disposable_checkpoint=capture_check,
                        disposable_before_create=lease.checkpoint_effect,
                        disposable_placement=placement[:2],
                    )
                captured = self.store.read_disposable_resource_preparation(operation.operation_id)
                if captured is None or not captured.replacement_target_id:
                    raise ValueError("Atomic replacement target capture is unavailable.")
                preparation = captured
                with self.store.serialize_disposable_testing_resources():
                    current_target = self.store.read_dokploy_target_record(
                        context_name=self.request.context, instance_name="testing"
                    )
                    if (
                        self.authority_sha256() != expected_authority
                        or captured.retained_target_record != expected_retained
                        or current_target.env
                        or current_target.domains
                        or current_target.public_hosts
                    ):
                        raise ValueError("Authority changed while capturing fresh target custody.")
                    expected = self.configuration_sha256()
            resources = OdooTestingResources(
                product=self.product,
                context=self.request.context,
                operation_id=operation.operation_id,
                target_id=preparation.replacement_target_id,
                namespace=preparation.resource_namespace,
            )
            check()
            lease.checkpoint_effect("prepare_fresh_storage")
            evidence = run_resource_schedule(
                root=self.root,
                store=self.store,
                resources=resources,
                mode="prepare",
                checkpoint=check,
            )
            with self.store.serialize_disposable_testing_resources():
                check()
                verified = self.publish(resources, evidence.get("schedule_id", ""))
            return ProviderMutationOutcome(
                202,
                provider_operation_response_payload(
                    trace_id=self.trace_id,
                    records={"operation": operation.operation_id},
                    result={
                        **plan,
                        "applied": True,
                        "resources_verified": True,
                        "resources_binding_sha256": verified["resources_binding_sha256"],
                        "schedule_id": verified["schedule_id"],
                    },
                ),
            )
