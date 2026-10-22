"""Prepare independently backed empty storage without changing the retained lane."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.idempotency_record import parse_launchplane_mutation_timestamp
from control_plane.contracts.testing_application import TestingApplicationSpecification
from control_plane.disposable_lanes import disposable_lane_configuration
from control_plane.dokploy import api, post_deploy, source
from control_plane.generic_web_testing_exclusion import (
    _bind,
    bind_testing_recipient,
    _target_reservations,
    inspect_generic_web_testing_exclusion,
    require_retained_application_custody,
)
from control_plane.odoo_testing_resources import (
    GenericWebTestingResources,
    ApplicationTestingResources,
    OdooTestingResources,
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


def application_resource_generation(binding: str, provider_operation_key: str) -> str:
    """Bind fresh application state to its real original preparation, not only inputs."""
    if not provider_operation_key:
        raise ValueError("Application allocation requires its original operation identity.")
    return canonical_json_sha256(
        {"plan_sha256": binding, "provider_operation_key": provider_operation_key}
    )


class SeparationResourcesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    context: str = Field(min_length=1)
    mode: Literal["dry-run", "apply", "receipt"] = "dry-run"
    reason: str = Field(min_length=1)
    reviewed_plan_sha256: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")
    application: TestingApplicationSpecification | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class SeparationResourcesRefused(ValueError):
    """No new resource effect has been attempted."""


class SeparationResourcesPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    context: str
    instance: Literal["testing"] = "testing"
    plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    retained_target_preserved: Literal[True] = True
    hold_preserved: Literal[True] = True
    historical_settlement: Literal["not_evaluated"] = "not_evaluated"
    configuration_updated: Literal[False] = False
    deployment_requested: Literal[False] = False
    published_host_ports: Literal[False] = False
    credentials_installed: Literal[False] = False
    applied: bool = False


class SeparationResourcesResult(SeparationResourcesPlan):
    applied: Literal[True] = True
    resources_verified: Literal[True] = True
    resources_binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    volumes: dict[str, str]
    schedule_id: str
    readback_schedule_id: str = ""
    qualified_at: AwareDatetime
    retained_target_id: str = Field(default="", exclude_if=lambda value: not value)
    allocation_operation_key: str = Field(default="", exclude_if=lambda value: not value)
    application: TestingApplicationSpecification | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


def preparation_binding(
    store: PostgresRecordStore,
    product: str,
    context: str,
    actor: str,
    root: Path,
    application: TestingApplicationSpecification | None = None,
) -> tuple[str, str]:
    bindings = (
        bind_testing_recipient(store, product, context, "testing")
        if application is not None
        else _bind(store, product, context, "testing")
    )
    profile, target, target_id, provider = bindings
    if application is not None and target.target_type != "application":
        raise SeparationResourcesRefused("Application preparation requires an application target.")
    if application is not None:
        from control_plane.testing_application_credentials import credential_binding_names
        from control_plane.testing_application_capabilities import (
            retained_application_capability_spec,
        )

        credential_binding_names(store, product, context, application)
        retained_application_capability_spec(store, root, product, context)
    if not profile.is_active:
        raise SeparationResourcesRefused("Preparation requires an active product.")
    hold = target.policies.staff_testing_hold
    # This does not reinterpret a legacy hold. Even a genuine staff hold remains
    # in force: preparation cannot change its target, configuration or dispatch.
    if hold is None or hold.recorded_by != actor:
        raise SeparationResourcesRefused("Retain this caller's existing testing interlock.")
    protection = store.read_disposable_lane_safety(product, context, "testing")
    if protection and protection.usage == "in_use":
        raise SeparationResourcesRefused("Known staff use protects the retained recipient.")
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
    host, token = source.read_dokploy_config(control_plane_root=root)
    placement = api.fetch_dokploy_target_payload(
        host=host, token=token, target_type=target.target_type, target_id=target_id.target_id
    )
    app_name, server_id = placement.get("appName"), placement.get("serverId")
    if (
        placement.get("applicationId" if application is not None else "composeId")
        != target_id.target_id
        or not isinstance(app_name, str)
        or not app_name.strip()
        or (server_id is not None and not isinstance(server_id, str))
    ):
        raise SeparationResourcesRefused("The retained provider placement is unavailable.")
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
            "provider_host": host,
            "placement": {
                key: placement.get(key)
                for key in (
                    "applicationId" if application is not None else "composeId",
                    "serverId",
                    "appName",
                )
            },
            **(
                {"application": application.model_dump(mode="json")}
                if application is not None
                else {}
            ),
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

    def binding(self) -> tuple[str, str]:
        return preparation_binding(
            self.store,
            self.product,
            self.request.context,
            self.actor,
            self.root,
            self.request.application,
        )

    def target_key(self) -> str:
        # A distinct fence is safe ONLY for this fixed create-only program. No
        # old-recipient writer or deploy adapter may use this reservation.
        return f"testing-independent-storage:{self.product}:{self.request.context}"

    def reconciliation_key(self) -> str:
        return self.request.reviewed_plan_sha256

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        if reconciliation_key != self.request.reviewed_plan_sha256:
            return ProviderObservation(outcome="unknown")
        try:
            binding, old_key = self.binding()
            if binding != reconciliation_key:
                return ProviderObservation(outcome="unknown")
            with self.store.testing_replacement_execution_lock(
                testing_event_execution_lock_name(old_key)
            ) as check:
                if check is None:
                    return ProviderObservation(outcome="unknown")
                if self.request.application is not None:
                    require_retained_application_custody(
                        self.store,
                        product=self.product,
                        context=self.request.context,
                        check=check,
                    )
                resources = self.resources(binding, provider_operation_key)
                evidence = run_preparation_schedule(
                    root=self.root,
                    resources=resources,
                    mode="readback",
                    before_effect=lambda _phase: check(),
                )
                with self.store.serialize_disposable_testing_resources():
                    check()
                    if self.binding() != (binding, old_key) or not resource_readback_passes(
                        evidence, resources
                    ):
                        return ProviderObservation(outcome="unknown")
                    return ProviderObservation(
                        outcome="present",
                        response_status_code=202,
                        response_payload=provider_operation_response_payload(
                            trace_id=self.trace_id,
                            records={},
                            result=self.result(resources, evidence, provider_operation_key),
                        ),
                    )
        except Exception:
            # Drift, active or uncorrelated remote work and missing volumes
            # remain unknown. Readback never creates storage or releases old work.
            return ProviderObservation(outcome="unknown")

    def plan(self) -> dict[str, object]:
        binding, _ = self.binding()
        return SeparationResourcesPlan(
            product=self.product, context=self.request.context, plan_sha256=binding
        ).model_dump(mode="json")

    def resources(self, binding: str, provider_operation_key: str = "") -> OdooTestingResources:
        operation = (
            application_resource_generation(binding, provider_operation_key)
            if self.request.application is not None
            else binding
        )
        values = dict(
            product=self.product,
            context=self.request.context,
            operation_id=operation,
            target_id=self.store.read_dokploy_target_id_record(
                context_name=self.request.context, instance_name="testing"
            ).target_id,
            namespace=f"lp-testing-{operation[:32]}",
        )
        if self.request.application is not None:
            return ApplicationTestingResources.model_validate(
                {**values, "state_kind": self.request.application.state.kind}
            )
        return GenericWebTestingResources.model_validate(values)

    def result(
        self,
        resources: OdooTestingResources,
        evidence: dict[str, str],
        provider_operation_key: str = "",
    ) -> dict[str, object]:
        return SeparationResourcesResult(
            product=self.product,
            context=self.request.context,
            plan_sha256=self.request.reviewed_plan_sha256,
            resources_binding_sha256=resources.binding_sha256,
            volumes=resources.volumes,
            schedule_id=evidence.get("creation_schedule_id", evidence.get("schedule_id", "")),
            readback_schedule_id=(
                evidence.get("schedule_id", "") if "creation_schedule_id" in evidence else ""
            ),
            qualified_at=datetime.now(timezone.utc),
            application=self.request.application,
            retained_target_id=resources.target_id if self.request.application is not None else "",
            allocation_operation_key=provider_operation_key
            if self.request.application is not None
            else "",
        ).model_dump(mode="json")

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        started = False
        try:
            if self.request.application is None:
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
            binding, old_key = self.binding()
            if binding != self.request.reviewed_plan_sha256:
                raise SeparationResourcesRefused("Review the changed preparation inputs.")
            with self.store.testing_replacement_execution_lock(
                testing_event_execution_lock_name(old_key)
            ) as old_check:
                if old_check is None:
                    raise SeparationResourcesRefused("The original executor is active.")
                if self.request.application is not None:
                    require_retained_application_custody(
                        self.store,
                        product=self.product,
                        context=self.request.context,
                        check=old_check,
                    )

                def check() -> None:
                    lease.assert_current()
                    old_check()
                    if self.binding() != (binding, old_key):
                        raise ValueError("Retained authority changed during preparation.")

                def effect(phase: str) -> None:
                    nonlocal started
                    with self.store.serialize_disposable_testing_resources():
                        check()
                        lease.checkpoint_effect(phase)
                        started = True

                check()
                resources = self.resources(binding, provider_operation_key)
                if self.request.application is not None:
                    from control_plane.testing_application_capabilities import (
                        inspect_retained_application_capabilities,
                    )

                    inspect_retained_application_capabilities(
                        store=self.store,
                        root=self.root,
                        product=self.product,
                        context=self.request.context,
                        namespace=resources.namespace,
                        before_effect=effect,
                    )
                evidence = run_preparation_schedule(
                    root=self.root, resources=resources, before_effect=effect
                )
                with self.store.serialize_disposable_testing_resources():
                    check()
                    if not resource_readback_passes(evidence, resources):
                        raise ValueError("Independent empty backing storage was not proved.")
                    result = self.result(resources, evidence, provider_operation_key)
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
    resources: OdooTestingResources,
    before_effect: Callable[[str], None],
    mode: Literal["prepare", "readback"] = "prepare",
) -> dict[str, str]:
    host, token = source.read_dokploy_config(control_plane_root=root)
    application = isinstance(resources, ApplicationTestingResources)
    payload = api.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type="application" if application else "compose",
        target_id=resources.target_id,
    )
    if payload.get("applicationId" if application else "composeId") != resources.target_id:
        raise ValueError("Retained target placement identity changed.")
    kind, lookup, _, server = post_deploy._resolve_dokploy_schedule_runtime(
        host=host,
        token=token,
        compose_id=resources.target_id,
        compose_name=resources.context,
        target_payload=payload,
    )
    name = "Launchplane independent testing storage"
    original_name = name

    def find(schedule_name: str) -> api.JsonObject | None:
        return _find_storage_schedule(host, token, lookup, kind, schedule_name, resources.namespace)

    original = find(name)
    if mode == "prepare" and original is not None:
        raise ValueError("Retain the existing preparation schedule and original receipt.")

    def original_terminal() -> None:
        current = find(original_name)
        if (
            current is None
            or current != original
            or current.get("script") != resources.script("prepare")
            or current.get("enabled") is not False
        ):
            raise ValueError("Original preparation schedule provenance is unavailable.")
        _require_terminal_storage_history(host, token, api.schedule_key(current), original=True)

    if mode == "readback":
        original_terminal()
        name += " readback"
        existing_read = find(name)
        if existing_read is not None:
            if (
                existing_read.get("script") != resources.script("readback")
                or existing_read.get("enabled") is not False
            ):
                raise ValueError("The readback schedule has different provenance.")
            _require_terminal_storage_history(
                host, token, api.schedule_key(existing_read), original=False
            )
    result = post_deploy.run_manual_data_schedule(
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
            "script": resources.script(mode),
            "serverId": server,
            "userId": lookup if kind == "dokploy-server" else None,
            "enabled": False,
            "timezone": "UTC",
        },
        resolved_schedule_execution_timeout_seconds=300,
        before_provider_mutation=before_effect,
    )
    if mode == "readback":
        original_terminal()
        assert original is not None
        result["creation_schedule_id"] = api.schedule_key(original)
    return result


def _find_storage_schedule(
    host: str, token: str, lookup: str, kind: str, name: str, namespace: str
) -> api.JsonObject | None:
    rows = api.dokploy_request(
        host=host,
        token=token,
        path="/api/schedule.list",
        query={"id": lookup, "scheduleType": kind},
    )
    if (
        not isinstance(rows, list)
        or len(rows) > 10000
        or any(not isinstance(row, dict) for row in rows)
    ):
        raise ValueError("Complete schedule inventory is required.")
    matches = [
        row
        for row in rows
        if isinstance(row, dict) and row.get("name") == name and row.get("appName") == namespace
    ]
    if len(matches) > 1:
        raise ValueError("The service preparation schedule is ambiguous.")
    return matches[0] if matches else None


def _require_terminal_storage_history(
    host: str, token: str, schedule_id: str, *, original: bool
) -> None:
    if not schedule_id:
        raise ValueError("Preparation schedule identity is unavailable.")
    rows = api.dokploy_request(
        host=host,
        token=token,
        path="/api/deployment.allByType",
        query={"id": schedule_id, "type": "schedule"},
    )
    if not isinstance(rows, list) or not rows or len(rows) > 10000 or (original and len(rows) != 1):
        raise ValueError("Complete original schedule history is required.")
    identifiers = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("scheduleId") != schedule_id:
            raise ValueError("Schedule history is uncorrelated.")
        identifier = api.deployment_key(row)
        if (
            not identifier
            or identifier in identifiers
            or api.deployment_status(row) not in {"done", "error"}
        ):
            raise ValueError("A remote preparation may still execute.")
        identifiers.add(identifier)
        started_at, finished_at = row.get("startedAt"), row.get("finishedAt")
        if not isinstance(started_at, str) or not isinstance(finished_at, str):
            raise ValueError("Schedule completion timestamps are unavailable.")
        started = parse_launchplane_mutation_timestamp(started_at, field_name="startedAt")
        finished = parse_launchplane_mutation_timestamp(finished_at, field_name="finishedAt")
        if finished < started:
            raise ValueError("Schedule completion provenance is invalid.")
