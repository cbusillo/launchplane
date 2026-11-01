from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.dokploy_target_record import DokployTargetStaffTestingHold
from control_plane.contracts.testing_rebuild import REBUILD_ROUTE, REBUILD_SCOPE
from control_plane.contracts.odoo_stable_target_replacement import (
    OdooStableTargetReplacementApplyResult,
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.contracts.testing_activation import (
    TestingActivationRecord,
    TestingActivationRequest,
    TestingActivationRefusal,
)
from control_plane.contracts.testing_application import TestingApplicationSpecification
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    is_exclusive_product_context,
    product_context_owner_map,
)
from control_plane.dokploy import api, source
from control_plane.dokploy.testing_route_transport import DOKPLOY_INGRESS_NETWORK
from control_plane.durable_operation_authorization import launchplane_identity_from_durable_caller
from control_plane.integration_allowances import target_record_sha256
from control_plane.product_retirement import require_unmounted_retirement_application
from control_plane.provider_operations import (
    DurableProviderMutationAdapter,
    ProviderEvidenceLease,
    ProviderMutationOutcome,
    ProviderMutationRejectedError,
    ProviderObservation,
    ProviderOperationLease,
    run_durable_provider_operation,
)
from control_plane.service_auth import launchplane_identity_actor
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_activation import (
    ROUTE as ACTIVATION_ROUTE,
    activation_record,
    completed_application_activation,
    same_reservation_intent,
    require_activation_hold_lift,
    target_without_hold,
)
from control_plane.testing_application_activation import (
    ApplicationActivationAdapter,
    ApplicationRouteAdmissionContradiction,
)
from control_plane.testing_application_capabilities import retained_application_capability_spec
from control_plane.testing_application_credentials import credential_binding_names
from control_plane.testing_application_runtime import (
    inspect_application_state,
    retire_application_runtime,
)
from control_plane.testing_rebuild_application_state import (
    RetainedApplicationState,
    application_mount_matches,
    retained_application_state,
    rebuild_address,
)
from control_plane.testing_lane_hold import (
    TestingHoldApplyRequest,
    apply_testing_hold_plan,
    build_testing_hold_plan,
)
from control_plane.testing_rebuild_driver import (
    TESTING_REBUILD_EXECUTION_UNAVAILABLE_CODE,
    TestingRebuildRefused,
    resolve_testing_rebuild_driver,
    testing_rebuild_interlock_reason,
)
from control_plane.testing_rebuild_service import (
    GenericWebTestingRebuildIntent,
    NativeTestingRebuildAdmissionChanged,
    NativeTestingRebuildPending,
    check_testing_rebuild_protection,
)
from control_plane.testing_candidate_runtime import testing_profile_binding
from control_plane.workflows.generic_web_deploy_provider import generic_web_provider_target_key
from control_plane.testing_separation_resources import (
    ROUTE as RESOURCES_ROUTE,
    SeparationResourcesAdapter,
    SeparationResourcesRequest,
    SeparationResourcesRefused,
)


class RebuildRouteUnsupported(ApplicationRouteAdmissionContradiction):
    pass


class GenericWebRebuildActivationAdapter(ApplicationActivationAdapter):
    recovering = False

    def require_unchanged(self, record: TestingActivationRecord) -> None:
        if self.recovering and (record.published or record.failure is not None):
            self.require_recovery_custody(record)
        else:
            super().require_unchanged(record)

    def verify_provider(self, record: TestingActivationRecord) -> None:
        if self.recovering and record.published:
            self.verify_recovery_provider(record)
        else:
            super().verify_provider(record)

    def route_authority(
        self, record: TestingActivationRecord, *, check_private: bool = True
    ) -> tuple[str, str]:
        readiness = record.application_readiness
        if self.recovering and readiness is not None and readiness.route is not None:
            return readiness.route.host, "https://" + readiness.route.host
        return super().route_authority(record, check_private=check_private)

    def route_admission(self, record: TestingActivationRecord) -> tuple[Any, dict[str, Any]] | None:
        from control_plane.dokploy.testing_route import admit_testing_route_predecessor

        admission = super().route_admission(record)
        if admission is not None:
            transport, arguments = admission
            arguments["require_owned_middlewares"] = True
            try:
                admit_testing_route_predecessor(**arguments)
            except ValueError as error:
                raise RebuildRouteUnsupported(
                    "The testing route depends on predecessor-owned configuration."
                ) from error
            return transport, arguments
        return None


FINALIZATION_CLEAR_PHASE = "clear_interlock"
FINALIZATION_HEALTH_FAILED_CLEAR_PHASE = "clear_interlock_after_health_failure"


def capture_rebuild_application_specification(
    store: PostgresRecordStore, intent: GenericWebTestingRebuildIntent
) -> TestingApplicationSpecification:
    previous = activation_record(store, intent.profile.product, intent.before.target.context)
    if (
        previous is None
        or previous.target_id != intent.before.target_id.target_id
        or previous.request.application is None
    ):
        return default_rebuild_application_specification(intent)
    if not completed_application_activation(store, previous):
        raise TestingRebuildRefused(
            "testing_rebuild_application_unverified",
            "The testing application needs its completed current activation.",
        )
    return TestingApplicationSpecification.model_validate(
        previous.request.application.model_dump(mode="json")
        | {
            "runtime_port": intent.profile.runtime_port,
            "health_path": intent.profile.health_path,
            "state": intent.state.model_dump(mode="json"),
        }
    )


def default_rebuild_application_specification(
    intent: GenericWebTestingRebuildIntent,
) -> TestingApplicationSpecification:
    return TestingApplicationSpecification(
        runtime_port=intent.profile.runtime_port,
        health_path=intent.profile.health_path,
        network_name=DOKPLOY_INGRESS_NETWORK,
        state=intent.state,
    )


def rebuild_application_specification(
    intent: GenericWebTestingRebuildIntent,
) -> TestingApplicationSpecification:
    if intent.before.target.target_type != "application":
        raise TestingRebuildRefused(
            TESTING_REBUILD_EXECUTION_UNAVAILABLE_CODE,
            "This rebuild requires an application testing recipient.",
        )
    return intent.application or default_rebuild_application_specification(intent)


def inspect_generic_web_testing_rebuild(
    root: Path, store: PostgresRecordStore, intent: GenericWebTestingRebuildIntent
) -> TestingApplicationSpecification:
    specification = rebuild_application_specification(intent)
    if intent.before.target.policies.staff_testing_hold is not None:
        raise TestingRebuildRefused(
            "testing_staff_hold", "The existing testing hold remains protected."
        )
    host, token = source.read_dokploy_config(control_plane_root=root)
    payload = api.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type="application",
        target_id=intent.before.target_id.target_id,
    )
    state = retained_application_state(store, intent)
    if (
        payload.get("applicationId") != intent.before.target_id.target_id
        or not application_mount_matches(payload, state)
        or any(
            (row.context, row.instance) != (intent.before.target.context, "testing")
            and row.target_id == intent.before.target_id.target_id
            for row in store.list_dokploy_target_id_records()
        )
    ):
        raise TestingRebuildRefused(
            "testing_rebuild_state_unverified",
            "The testing application must match its captured state and have no shared target.",
        )
    credential_binding_names(
        store, intent.profile.product, intent.before.target.context, specification
    )
    retained = retained_application_capability_spec(
        store, root, intent.profile.product, intent.before.target.context
    )
    require_predecessor_metadata(
        payload,
        target_id=intent.before.target_id.target_id,
        application=str(retained["application"]),
        server_id=intent.retained_volumes[0].server_id
        if intent.retained_volumes
        else str(payload.get("serverId") or ""),
        state=state,
    )
    require_disabled_predecessor_schedules(host, token, intent.before.target_id.target_id)
    inspect_application_state(
        host=host,
        token=token,
        server_id=str(payload.get("serverId") or ""),
        application=str(retained["application"]),
        **state,
    )
    serving = activation_record(store, intent.profile.product, intent.before.target.context)
    if serving is not None:
        unresolved_predecessors(store, serving)
    return specification


def rebuild_operation(
    store: PostgresRecordStore, record: TestingActivationRecord
) -> OdooStableTargetReplacementOperationRecord | None:
    if not record.request.preparation_key.endswith(":resources"):
        return None
    try:
        operation = store.read_odoo_stable_target_replacement_operation_record(
            record.request.preparation_key.removesuffix(":resources")
        )
    except FileNotFoundError:
        return None
    if (
        operation.is_native_testing_rebuild
        and operation.product == record.product
        and operation.context == record.context
        and operation.idempotency_scope == record.scope
    ):
        return operation
    return None


def rebuild_finalization(
    store: PostgresRecordStore, record: TestingActivationRecord
) -> LaunchplaneIdempotencyRecord | None:
    operation = rebuild_operation(store, record)
    if operation is None:
        return None
    row = store.read_idempotency_record(
        scope=record.scope,
        route_path=REBUILD_ROUTE + ":finalization",
        idempotency_key=operation.operation_id + ":finalization",
    )
    if row is None or row.state != "completed" or row.reconciliation_key != record.plan_sha256:
        return None
    result = row.response_payload.get("result")
    runtime_source = result.get("runtime_source") if isinstance(result, dict) else None
    if (
        isinstance(result, dict)
        and result.get("target_id") == record.target_id
        and isinstance(runtime_source, dict)
        and runtime_source.get("testing_activation") == record.plan_sha256
    ):
        return row
    return None


def predecessor_retirement_binding(
    successor: TestingActivationRecord, predecessor: TestingActivationRecord
) -> str:
    return canonical_json_sha256(
        {"successor": successor.plan_sha256, "predecessor": predecessor.plan_sha256}
    )


def unresolved_predecessors(
    store: PostgresRecordStore, record: TestingActivationRecord
) -> list[TestingActivationRecord]:
    if rebuild_operation(store, record) is None:
        return []
    if not completed_application_activation(store, record):
        raise NativeTestingRebuildPending("The previous serving activation is not complete.")
    finalization = rebuild_finalization(store, record)
    if finalization is None:
        raise NativeTestingRebuildPending("The previous rebuild finalization is unavailable.")
    source = finalization.response_payload["result"]["runtime_source"]
    raw_identifiers = source.get("retained_predecessors")
    raw_plans = source.get("retained_predecessor_plans")
    if not isinstance(raw_identifiers, str) or not isinstance(raw_plans, str):
        raise NativeTestingRebuildPending(
            "The previous unresolved predecessor evidence is unavailable."
        )
    identifiers = raw_identifiers.split(",") if raw_identifiers else []
    plans = raw_plans.split(",") if raw_plans else []
    if (
        len(identifiers) != len(plans)
        or len(set(plans)) != len(plans)
        or len(set(identifiers)) != len(identifiers)
    ):
        raise NativeTestingRebuildPending(
            "The previous unresolved predecessor evidence is ambiguous."
        )
    candidates = [
        TestingActivationRecord.model_validate(value)
        for value in store.list_testing_application_activations(record.product, record.context)
    ]
    unresolved = []
    for plan, identifier in zip(plans, identifiers, strict=True):
        matches = [
            value
            for value in candidates
            if value.plan_sha256 == plan
            and value.retained_target_id.target_id == identifier
            and completed_application_activation(store, value)
            and rebuild_operation(store, value) is not None
        ]
        if len(matches) != 1:
            raise NativeTestingRebuildPending(
                "An unresolved predecessor has no exact completed activation."
            )
        unresolved.append(matches[0])
    return unresolved


def rebuild_predecessors(
    store: PostgresRecordStore, record: TestingActivationRecord
) -> list[TestingActivationRecord]:
    previous = [
        TestingActivationRecord.model_validate(value)
        for value in store.list_testing_application_activations(record.product, record.context)
        if value.get("target_id") == record.retained_target_id.target_id
        and value.get("published") is True
    ]
    if len(previous) > 1:
        raise NativeTestingRebuildPending("The previous serving activation is ambiguous.")
    carried = unresolved_predecessors(store, previous[0]) if previous else []
    if any(
        value.retained_target_id.target_id
        in {record.target_id, record.retained_target_id.target_id}
        for value in carried
    ):
        raise NativeTestingRebuildPending(
            "The unresolved predecessor set aliases the current generation."
        )
    return [record, *carried]


def require_predecessor_metadata(
    payload: api.JsonObject,
    *,
    target_id: str,
    application: str,
    server_id: str,
    state: RetainedApplicationState | None = None,
) -> None:
    if (
        payload.get("applicationId") != target_id
        or payload.get("appName") != application
        or not server_id
        or str(payload.get("serverId") or "") != server_id
        or not application_mount_matches(payload, state)
        or payload.get("previewDeployments") != []
    ):
        raise TestingRebuildRefused(
            "testing_rebuild_predecessor_changed",
            "The original predecessor placement, mounts or previews are unverified.",
        )


def require_disabled_predecessor_schedules(
    host: str, token: str, target_id: str
) -> list[api.JsonObject]:
    schedules = api.dokploy_request(
        host=host,
        token=token,
        path="/api/schedule.list",
        query={"id": target_id, "scheduleType": "application"},
    )
    if (
        not isinstance(schedules, list)
        or len(schedules) > 10000
        or any(
            not isinstance(schedule, dict)
            or schedule.get("applicationId") != target_id
            or schedule.get("enabled") is not False
            or not isinstance(schedule.get("scheduleId"), str)
            or not schedule["scheduleId"]
            for schedule in schedules
        )
    ):
        raise TestingRebuildRefused(
            "testing_rebuild_predecessor_schedule_active",
            "An original predecessor schedule is enabled or unobserved.",
        )
    return cast(list[api.JsonObject], schedules)


@dataclass
class RebuildChildLease:
    child: ProviderOperationLease
    parent: ProviderOperationLease
    guard: Callable[[str], None]
    name: str

    def assert_current(self) -> None:
        self.parent.assert_current()
        self.child.assert_current()

    def evidence_reservation(self) -> LaunchplaneIdempotencyRecord:
        if not isinstance(self.child, ProviderEvidenceLease):
            raise ValueError("Testing rebuild children require their original evidence lease.")
        return self.child.evidence_reservation()

    def checkpoint_effect(self, phase: str) -> None:
        self.guard(self.name + ":" + phase)
        self.parent.checkpoint_effect(self.name + ":" + phase)
        self.child.checkpoint_effect(phase)


@dataclass
class RebuildChildAdapter:
    child: DurableProviderMutationAdapter
    parent: ProviderOperationLease
    guard: Callable[[str], None]
    name: str
    read_only_observation: bool = False

    def target_key(self) -> str:
        return self.child.target_key()

    def reconciliation_key(self) -> str:
        return self.child.reconciliation_key()

    def observe(self, key: str, phase: str, reconciliation_key: str) -> ProviderObservation:
        self.parent.assert_current()
        if not self.read_only_observation:
            self.guard(self.name + ":observe")
        return self.child.observe(key, phase, reconciliation_key)

    def apply(self, key: str, lease: ProviderOperationLease) -> ProviderMutationOutcome:
        try:
            self.guard(self.name + ":admission")
        except (NativeTestingRebuildAdmissionChanged, TestingRebuildRefused) as error:
            if isinstance(lease, ProviderEvidenceLease) and not (
                lease.evidence_reservation().provider_effect_started_at
            ):
                raise ProviderMutationRejectedError(error) from error
            raise
        return self.child.apply(key, RebuildChildLease(lease, self.parent, self.guard, self.name))


@dataclass
class GenericWebTestingRebuildAdapter:
    root: Path
    store: PostgresRecordStore
    operation: OdooStableTargetReplacementOperationRecord
    intent: GenericWebTestingRebuildIntent
    authorize: Callable[[str], None]

    def target_key(self) -> str:
        return f"testing-rebuild:{self.operation.product}:{self.operation.context}"

    def reconciliation_key(self) -> str:
        return canonical_json_sha256(self.intent.model_dump(mode="json"))

    def observe(self, key: str, phase: str, reconciliation_key: str) -> ProviderObservation:
        if reconciliation_key != self.reconciliation_key():
            return ProviderObservation("unknown")
        with self.store.testing_replacement_execution_lock(self.operation.operation_id) as check:
            if check is None:
                return ProviderObservation("unknown")
            return ProviderObservation("absent", retry_safe=True, preserve_effect_checkpoint=True)

    def apply(self, key: str, lease: ProviderOperationLease) -> ProviderMutationOutcome:
        with self.store.testing_replacement_execution_lock(self.operation.operation_id) as check:
            if check is None:
                raise NativeTestingRebuildPending("The original rebuild still has an executor.")
            executor = GenericWebTestingRebuildExecution(self, lease, check)
            result = executor.run()
            if not isinstance(lease, ProviderEvidenceLease):
                raise ValueError("Testing rebuild requires its original evidence lease.")
            return ProviderMutationOutcome(
                200,
                {"result": result.model_dump(mode="json")},
                provider_effect_performed=bool(
                    lease.evidence_reservation().provider_effect_started_at
                ),
            )


@dataclass
class GenericWebTestingRebuildFinalizer:
    execution: GenericWebTestingRebuildExecution
    activation: ApplicationActivationAdapter
    record: TestingActivationRecord

    def target_key(self) -> str:
        return self.execution.parent.target_key() + ":finalization"

    def reconciliation_key(self) -> str:
        return self.record.plan_sha256

    def observe(self, key: str, phase: str, reconciliation_key: str) -> ProviderObservation:
        if (
            reconciliation_key != self.reconciliation_key()
            or self.execution.activation() != self.record
        ):
            return ProviderObservation("unknown")
        if phase in {FINALIZATION_CLEAR_PHASE, FINALIZATION_HEALTH_FAILED_CLEAR_PHASE}:
            target = self.execution.store.read_dokploy_target_record(
                context_name=self.record.context, instance_name="testing"
            )
            if not self.execution.owns_interlock(target.policies.staff_testing_hold):
                return ProviderObservation(
                    "present",
                    response_status_code=200,
                    response_payload={
                        "result": self.execution.result(
                            self.record,
                            candidate_health_failed=phase == FINALIZATION_HEALTH_FAILED_CLEAR_PHASE,
                        ).model_dump(mode="json")
                    },
                )
        return ProviderObservation("absent", retry_safe=True, preserve_effect_checkpoint=True)

    def apply(self, key: str, lease: ProviderOperationLease) -> ProviderMutationOutcome:
        health_failed = False
        if self.record.failure is None:
            if not self.record.published or self.record.application_readiness is None:
                raise NativeTestingRebuildPending(
                    "The original candidate is not qualified for retirement."
                )
            for predecessor in rebuild_predecessors(self.execution.store, self.record):
                self.execution.retire_predecessor(self.activation, self.record, predecessor)
            self.activation.require_unchanged(self.record)
            self.activation.verify_provider(self.record)
            health = self.activation.read_edge_health(
                self.record, expected=self.record.application_readiness.runtime_identity
            )
            if not health.verified or health.status not in {"pass", "fail"}:
                raise NativeTestingRebuildPending(
                    "The final same-address health remains unobserved."
                )
            health_failed = health.status == "fail" or health.runtime_identity_status != "match"

        def checkpoint(phase: str) -> None:
            if phase == FINALIZATION_CLEAR_PHASE and self.record.failure is None:
                for predecessor, retired in self.execution.retirement_results(self.record):
                    if retired:
                        self.execution.require_predecessor_absent(predecessor)
            lease.checkpoint_effect(
                FINALIZATION_HEALTH_FAILED_CLEAR_PHASE
                if phase == FINALIZATION_CLEAR_PHASE and health_failed
                else phase
            )

        self.execution.interlock(
            hold=False,
            activation=self.record if self.record.published else None,
            checkpoint=checkpoint,
        )
        return ProviderMutationOutcome(
            200,
            {
                "result": self.execution.result(
                    self.record, candidate_health_failed=health_failed
                ).model_dump(mode="json")
            },
        )


@dataclass
class GenericWebPredecessorRetirement:
    execution: GenericWebTestingRebuildExecution
    activation: ApplicationActivationAdapter
    successor: TestingActivationRecord
    predecessor: TestingActivationRecord

    def target_key(self) -> str:
        return self.execution.parent.target_key() + ":retirement:" + self.predecessor.plan_sha256

    def reconciliation_key(self) -> str:
        return predecessor_retirement_binding(self.successor, self.predecessor)

    def observe(self, key: str, phase: str, reconciliation_key: str) -> ProviderObservation:
        if reconciliation_key != self.reconciliation_key():
            return ProviderObservation("unknown")
        return ProviderObservation("absent", retry_safe=True, preserve_effect_checkpoint=True)

    def apply(self, key: str, lease: ProviderOperationLease) -> ProviderMutationOutcome:
        retired = self.execution.retire(
            self.activation,
            self.successor,
            lease=lease,
            predecessor=None if self.predecessor == self.successor else self.predecessor,
        )
        if not isinstance(lease, ProviderEvidenceLease):
            raise ValueError("Predecessor retirement requires its original evidence lease.")
        return ProviderMutationOutcome(
            200,
            {"retired": retired, "target_id": self.predecessor.retained_target_id.target_id},
            provider_effect_performed=bool(lease.evidence_reservation().provider_effect_started_at),
        )


class GenericWebTestingRebuildExecution:
    def __init__(
        self,
        adapter: GenericWebTestingRebuildAdapter,
        lease: ProviderOperationLease,
        check: Callable[[], None],
    ) -> None:
        self.parent, self.lease, self.check = adapter, lease, check
        self.root, self.store, self.operation, self.intent = (
            adapter.root,
            adapter.store,
            adapter.operation,
            adapter.intent,
        )
        authorization = self.operation.authorization
        if authorization is None:
            raise ValueError("Testing rebuild requires its original caller authority.")
        self.actor = launchplane_identity_actor(
            launchplane_identity_from_durable_caller(authorization.caller)
        )
        self.scope = self.operation.idempotency_scope
        self.resources_key = self.operation.operation_id + ":resources"
        self.activation_key = self.operation.operation_id + ":activation"
        self.specification = rebuild_application_specification(self.intent)
        self.recovering = False

    def activation(self) -> TestingActivationRecord | None:
        return activation_record(
            self.store,
            self.operation.product,
            self.operation.context,
            scope=self.scope,
            preparation_key=self.resources_key,
        )

    def external_comparison_profiles(self) -> tuple[LaunchplaneProductProfileRecord, ...]:
        self.guard("testing_rebuild_external_comparison", recovery=self.recovering)
        return self.store.list_product_profile_records()

    def retirement_intent(self, record: TestingActivationRecord) -> GenericWebTestingRebuildIntent:
        if record.request.preparation_key == self.resources_key:
            intent = self.intent
        else:
            operation = rebuild_operation(self.store, record)
            original = (
                self.store.read_idempotency_record(
                    scope=REBUILD_SCOPE,
                    route_path=REBUILD_ROUTE,
                    idempotency_key=operation.operation_id,
                )
                if operation is not None
                else None
            )
            if original is None or original.state != "completed":
                raise NativeTestingRebuildPending("The retained predecessor intent is unavailable.")
            intent = GenericWebTestingRebuildIntent.model_validate(
                self.store.read_original_mutation_intent(original)
            )
        if (
            intent.profile.product != record.product
            or intent.before.target.context != record.context
            or intent.before.target_id.target_id != record.retained_target_id.target_id
        ):
            raise NativeTestingRebuildPending("The original predecessor state belongs elsewhere.")
        return intent

    def retirement_state(self, record: TestingActivationRecord) -> RetainedApplicationState:
        return retained_application_state(self.store, self.retirement_intent(record))

    def application_adapter(
        self, request: TestingActivationRequest
    ) -> GenericWebRebuildActivationAdapter:
        adapter = GenericWebRebuildActivationAdapter(
            self.root,
            self.store,
            self.operation.product,
            request,
            self.actor,
            self.scope,
            self.operation.operation_id,
        )
        adapter.recovering = self.recovering
        adapter.authorize_external = self.external_comparison_profiles
        return adapter

    def guard(self, phase: str, *, recovery: bool = False) -> None:
        self.check()
        self.lease.assert_current()
        self.parent.authorize(phase)
        if self.intent.private_ingress and not (recovery or self.recovering):
            from control_plane.testing_application_private_route import (
                PrivateIngressAuthorityChanged,
                require_private_ingress_records,
            )

            try:
                require_private_ingress_records(
                    self.store,
                    self.operation.product,
                    self.operation.context,
                    self.intent.private_ingress,
                )
            except PrivateIngressAuthorityChanged as error:
                raise NativeTestingRebuildAdmissionChanged(str(error)) from error
        if not is_exclusive_product_context(
            context=self.operation.context,
            product=self.operation.product,
            owners=product_context_owner_map(self.store.list_product_profile_records()),
        ):
            raise NativeTestingRebuildPending("Original recovery requires exclusive lane custody.")
        if not recovery and not self.recovering:
            try:
                driver = resolve_testing_rebuild_driver(self.store, self.operation.product)
                driver.require_execution()
                changed = (
                    testing_profile_binding(driver.profile, self.operation.context)
                    != testing_profile_binding(self.intent.profile, self.operation.context)
                    or driver.state != self.intent.state
                )
            except (ValueError, FileNotFoundError) as error:
                raise NativeTestingRebuildAdmissionChanged(
                    "The admitted testing runtime is no longer configured."
                ) from error
            if changed:
                raise NativeTestingRebuildAdmissionChanged("The original testing runtime changed.")
        current = self.activation()
        target_id = self.store.read_dokploy_target_id_record(
            context_name=self.operation.context, instance_name="testing"
        )
        expected_id = (
            current.target_id
            if current is not None and current.published
            else self.intent.before.target_id.target_id
        )
        if target_id.target_id != expected_id:
            raise NativeTestingRebuildPending("The testing recipient changed outside this rebuild.")
        if any(
            (row.context, row.instance) != (self.operation.context, "testing")
            and row.target_id == expected_id
            for row in self.store.list_dokploy_target_id_records()
        ):
            raise NativeTestingRebuildPending("A shared target is protected from this rebuild.")
        target = self.store.read_dokploy_target_record(
            context_name=self.operation.context, instance_name="testing"
        )
        if current is None and target_without_hold(target) != target_without_hold(
            self.intent.before.target
        ):
            raise NativeTestingRebuildAdmissionChanged("The retained testing target changed.")
        check_testing_rebuild_protection(
            self.root, self.store, self.operation, self.intent, actor=self.actor
        )

    def owns_interlock(self, hold: DokployTargetStaffTestingHold | None) -> bool:
        return (
            hold is not None
            and hold.purpose == "isolation_staging"
            and hold.recorded_by == self.actor
            and hold.reason == testing_rebuild_interlock_reason(self.operation.operation_id)
        )

    def interlock(
        self,
        *,
        hold: bool,
        activation: TestingActivationRecord | None = None,
        checkpoint: Callable[[str], None] | None = None,
    ) -> None:
        self.guard("testing_rebuild_interlock", recovery=not hold)
        target = self.store.read_dokploy_target_record(
            context_name=self.operation.context, instance_name="testing"
        )
        before = target.policies.staff_testing_hold
        reason = testing_rebuild_interlock_reason(self.operation.operation_id)
        if before is not None and not self.owns_interlock(before):
            raise NativeTestingRebuildPending("A pre-existing testing hold remains protected.")
        request = TestingHoldApplyRequest(
            product=self.operation.product,
            context=self.operation.context,
            instance="testing",
            hold=hold,
            purpose="isolation_staging",
            reason=reason,
            activation_plan_sha256=activation.plan_sha256 if activation is not None else "",
        )
        verified = False
        if activation is not None:
            verified = require_activation_hold_lift(
                self.store, request, self.actor, self.root, recovery=self.recovering
            )
        plan, _ = build_testing_hold_plan(
            record_store=self.store, request=request, actor=self.actor
        )
        (checkpoint or self.lease.checkpoint_effect)(
            "set_interlock" if hold else FINALIZATION_CLEAR_PHASE
        )
        result = apply_testing_hold_plan(
            record_store=self.store,
            request=request.model_copy(
                update={"mode": "apply", "reviewed_plan_sha256": plan.plan_sha256}
            ),
            actor=self.actor,
            required_context_owner=(self.operation.product, self.operation.context),
            activation_verified=verified,
        )
        if not result.read_back_matches:
            raise NativeTestingRebuildPending("The original rebuild interlock is unobserved.")

    def run_child(
        self,
        child: DurableProviderMutationAdapter,
        *,
        route: str,
        key: str,
        request: dict[str, Any],
        name: str,
        original_intent: dict[str, Any] | None = None,
        parent_lease: ProviderOperationLease | None = None,
    ) -> dict[str, object]:
        self.guard(name + ":admission")
        if not isinstance(self.lease, ProviderEvidenceLease):
            raise ValueError("Testing rebuild requires its original evidence lease.")
        result = run_durable_provider_operation(
            store=self.store,
            scope=self.scope,
            route_path=route,
            idempotency_key=key,
            request_fingerprint=canonical_json_sha256(
                {"product": self.operation.product, **request}
            ),
            lease_owner=self.lease.evidence_reservation().lease_owner,
            response_trace_id=self.operation.operation_id,
            adapter=RebuildChildAdapter(
                child, parent_lease if parent_lease is not None else self.lease, self.guard, name
            ),
            original_intent=original_intent,
        )
        if result.status not in {"completed", "adopted", "replayed"}:
            raise NativeTestingRebuildPending("Continue the original " + name + " operation.")
        return result.response_payload

    def prepare(self) -> None:
        route = RESOURCES_ROUTE.format(product=self.operation.product)
        row = self.store.read_idempotency_record(
            scope=self.scope, route_path=route, idempotency_key=self.resources_key
        )
        request = SeparationResourcesRequest(
            context=self.operation.context,
            reason=self.operation.request.reason,
            application=self.specification,
        )
        adapter = SeparationResourcesAdapter(
            self.root,
            self.store,
            self.operation.product,
            request,
            self.actor,
            self.operation.operation_id,
        )
        binding = row.reconciliation_key if row is not None else adapter.binding()[0]
        adapter.request = request.model_copy(
            update={"mode": "apply", "reviewed_plan_sha256": binding}
        )
        result = self.run_child(
            adapter,
            route=route,
            key=self.resources_key,
            request=adapter.request.model_dump(mode="json"),
            name="resources",
        )
        from control_plane.testing_separation_resources import (
            parse_preparation_receipt,
            SeparationResourcesRefusal,
        )

        if isinstance(parse_preparation_receipt(result["result"]), SeparationResourcesRefusal):
            raise NativeTestingRebuildAdmissionChanged("The original resource preparation refused.")

    def activate(self) -> tuple[GenericWebRebuildActivationAdapter, TestingActivationRecord]:
        route = ACTIVATION_ROUTE.format(product=self.operation.product)
        row = self.store.read_idempotency_record(
            scope=self.scope, route_path=route, idempotency_key=self.activation_key
        )
        original = self.store.read_original_mutation_intent(row) if row is not None else None
        if original is not None:
            captured = TestingActivationRecord.model_validate(original["testing_activation"])
            request = captured.request.model_copy(
                update={"mode": "apply", "reviewed_plan_sha256": captured.plan_sha256}
            )
        else:
            target = self.store.read_dokploy_target_record(
                context_name=self.operation.context, instance_name="testing"
            )
            request = TestingActivationRequest(
                context=self.operation.context,
                preparation_key=self.resources_key,
                interlock_record_sha256=target_record_sha256(target),
                source_commit=self.operation.request.source_git_ref,
                volume_keys={},
                port_keys=(),
                reason=self.operation.request.reason,
                application=self.specification,
            )
        adapter = self.application_adapter(request)
        if original is None:
            captured = adapter.current()
            if captured.image_reference != self.operation.request.artifact_id:
                raise NativeTestingRebuildAdmissionChanged("The selected verified build changed.")
            original = {"testing_activation": captured.model_dump(mode="json")}
            adapter.request = request.model_copy(
                update={"mode": "apply", "reviewed_plan_sha256": captured.plan_sha256}
            )
        self.run_child(
            adapter,
            route=route,
            key=self.activation_key,
            request=adapter.request.model_dump(mode="json"),
            name="activation",
            original_intent=original,
        )
        return adapter, adapter.current()

    def retire_predecessor(
        self,
        adapter: ApplicationActivationAdapter,
        record: TestingActivationRecord,
        predecessor: TestingActivationRecord,
    ) -> bool:
        result = self.run_child(
            GenericWebPredecessorRetirement(self, adapter, record, predecessor),
            route=REBUILD_ROUTE + ":retirement",
            key=self.operation.operation_id + ":retirement:" + predecessor.plan_sha256,
            request={"successor": record.plan_sha256, "predecessor": predecessor.plan_sha256},
            name="retirement",
            original_intent={"testing_activation": predecessor.model_dump(mode="json")},
        )
        retired = result.get("retired")
        if (
            not isinstance(retired, bool)
            or result.get("target_id") != predecessor.retained_target_id.target_id
        ):
            raise NativeTestingRebuildPending("The original predecessor retirement is unobserved.")
        return retired

    def retirement_results(
        self, record: TestingActivationRecord
    ) -> list[tuple[TestingActivationRecord, bool]]:
        results = []
        for predecessor in rebuild_predecessors(self.store, record):
            child = self.store.read_idempotency_record(
                scope=self.scope,
                route_path=REBUILD_ROUTE + ":retirement",
                idempotency_key=self.operation.operation_id
                + ":retirement:"
                + predecessor.plan_sha256,
            )
            retired = child.response_payload.get("retired") if child is not None else None
            if (
                child is None
                or child.state != "completed"
                or not isinstance(retired, bool)
                or child.reconciliation_key != predecessor_retirement_binding(record, predecessor)
                or child.response_payload.get("target_id")
                != predecessor.retained_target_id.target_id
            ):
                raise NativeTestingRebuildPending("Original predecessor retirement is unobserved.")
            results.append((predecessor, retired))
        return results

    def retire(
        self,
        adapter: ApplicationActivationAdapter,
        record: TestingActivationRecord,
        *,
        lease: ProviderOperationLease,
        predecessor: TestingActivationRecord | None = None,
    ) -> bool:
        self.guard("testing_rebuild_retirement")
        adapter.require_unchanged(record)
        original_phase = (
            lease.evidence_reservation().provider_effect_phase
            if isinstance(lease, ProviderEvidenceLease)
            else ""
        )
        removal_started = original_phase in {
            "retire_predecessor",
            "remove_retired_service",
        } or original_phase.startswith("retirement_volume:")
        deploy_setting_unobserved = original_phase == "disable_old_deploys"

        def retain_predecessor(cause: ValueError | None = None) -> bool:
            if removal_started or deploy_setting_unobserved:
                raise NativeTestingRebuildPending(
                    "The original predecessor effect remains unsettled. "
                    + (str(cause) if cause else "")
                ) from cause
            return False

        successor_readiness = record.application_readiness
        if not record.published or successor_readiness is None:
            raise NativeTestingRebuildPending("The original candidate has not been published.")
        if (
            predecessor is not None
            and predecessor not in rebuild_predecessors(self.store, record)[1:]
        ):
            raise NativeTestingRebuildPending(
                "The earlier predecessor is outside the original unresolved set."
            )
        retained = predecessor or record
        state = self.retirement_state(retained)
        readiness = retained.application_readiness
        old_id = retained.retained_target_id.target_id
        if (
            predecessor is None
            and old_id != self.intent.before.target_id.target_id
            or old_id == record.target_id
        ):
            raise ValueError("Retirement must identify the original independent predecessor.")
        if (
            readiness is None
            or readiness.route is None
            or not isinstance(readiness.route.old_backend, str)
            or readiness.route_execution is None
            or retained.predecessor is None
        ):
            raise NativeTestingRebuildPending(
                "Original physical predecessor custody is unavailable."
            )
        application = urlsplit(readiness.route.old_backend).hostname or ""
        try:
            self.require_retirement_custody(retained)
        except NativeTestingRebuildPending:
            if (
                isinstance(lease, ProviderEvidenceLease)
                and not lease.evidence_reservation().provider_effect_started_at
            ):
                return False
            raise
        host, token = source.read_dokploy_config(control_plane_root=self.root)
        payload = None
        try:
            payload = api.fetch_dokploy_target_payload(
                host=host, token=token, target_type="application", target_id=old_id
            )
        except api.DokployRequestFailed as error:
            if error.status_code == 404:
                deploy_setting_unobserved = False
            else:
                raise
        if payload is not None:
            deploy_setting_unobserved = deploy_setting_unobserved and not (
                payload.get("applicationId") == old_id and payload.get("autoDeploy") is False
            )
            adapter.verify_provider(record)
            health = adapter.read_edge_health(record, expected=successor_readiness.runtime_identity)
            if (
                health.verified
                and health.status == "fail"
                and health.runtime_identity_status == "match"
            ):
                return retain_predecessor()
            if (
                not health.verified
                or health.status != "pass"
                or health.runtime_identity_status != "match"
            ):
                raise NativeTestingRebuildPending(
                    "Current same-address candidate health is unavailable."
                )
            try:
                require_predecessor_metadata(
                    payload,
                    target_id=old_id,
                    application=application,
                    server_id=readiness.route_execution.server_id,
                    state=state,
                )
                require_disabled_predecessor_schedules(host, token, old_id)
            except TestingRebuildRefused as error:
                return retain_predecessor(error)
            if payload.get("autoDeploy") is not False:
                if removal_started:
                    raise NativeTestingRebuildPending(
                        "The original predecessor deploy setting changed."
                    )
                self.guard("testing_rebuild_disable_old_deploys")
                lease.checkpoint_effect("disable_old_deploys")
                api.dokploy_request(
                    host=host,
                    token=token,
                    method="POST",
                    path="/api/application.update",
                    payload={"applicationId": old_id, "autoDeploy": False},
                )
            try:
                self.require_retirement_quiescent(
                    host,
                    token,
                    old_id,
                    application,
                    readiness.route_execution.server_id,
                    state=state,
                )
            except NativeTestingRebuildPending as error:
                if predecessor is not None:
                    return retain_predecessor(error)
                raise
            deploy_setting_unobserved = False
            if state["mode"] == "stateless":
                require_unmounted_retirement_application(
                    control_plane_root=self.root, target_id=old_id
                )
            inspect_application_state(
                host=host,
                token=token,
                server_id=readiness.route_execution.server_id,
                application=application,
                service_id=retained.predecessor.service_id,
                **state,
            )
            self.guard("testing_rebuild_retire_predecessor")
            self.require_retirement_custody(retained)
            lease.checkpoint_effect("retire_predecessor")
            try:
                api.delete_dokploy_application(host=host, token=token, application_id=old_id)
            except api.DokployRequestFailed as error:
                if error.status_code != 404:
                    raise
        try:
            api.fetch_dokploy_target_payload(
                host=host, token=token, target_type="application", target_id=old_id
            )
        except api.DokployRequestFailed as error:
            if error.status_code != 404:
                raise
        else:
            raise NativeTestingRebuildPending("Original predecessor retirement remains unobserved.")
        if not isinstance(
            lease, ProviderEvidenceLease
        ) or lease.evidence_reservation().provider_effect_phase not in {
            "retire_predecessor",
            "remove_retired_service",
        }:
            try:
                self.require_predecessor_service_absent(retained)
            except ValueError:
                return retain_predecessor()
            return self.retire_volume(adapter, record, retained, lease)

        def dispatch(remove: Callable[[], None]) -> None:
            self.guard("testing_rebuild_remove_retired_service")
            self.require_retirement_custody(retained)
            try:
                api.fetch_dokploy_target_payload(
                    host=host, token=token, target_type="application", target_id=old_id
                )
            except api.DokployRequestFailed as error:
                if error.status_code != 404:
                    raise
            else:
                raise NativeTestingRebuildPending("The predecessor metadata reappeared.")
            lease.checkpoint_effect("remove_retired_service")
            remove()

        retire_application_runtime(
            host=host,
            token=token,
            server_id=readiness.route_execution.server_id,
            application=application,
            service_id=retained.predecessor.service_id,
            dispatch_guard=dispatch,
            volume_name=state["volume_name"],
            mount_path=state["mount_path"],
            volume_binding=state["volume_binding"],
        )
        self.require_predecessor_service_absent(retained)
        return self.retire_volume(adapter, record, retained, lease)

    def retire_volume(
        self,
        activation: ApplicationActivationAdapter,
        successor: TestingActivationRecord,
        predecessor: TestingActivationRecord,
        lease: ProviderOperationLease,
    ) -> bool:
        from control_plane.testing_rebuild_volume_retirement import (
            TestingRebuildVolumeRetirement,
            VOLUME_RETIREMENT_PERMIT_SECONDS,
        )

        intent = self.retirement_intent(predecessor)
        if not intent.retained_volumes:
            return True
        volume = intent.retained_volumes[0]
        adapter = TestingRebuildVolumeRetirement(self, activation, successor, predecessor, volume)
        route = REBUILD_ROUTE + ":volume-retirement"
        key = self.operation.operation_id + ":volume:" + adapter.reconciliation_key()
        row = self.store.read_idempotency_record(
            scope=self.scope, route_path=route, idempotency_key=key
        )
        if row is not None:
            original = self.store.read_original_mutation_intent(row)
        else:
            assert predecessor.predecessor is not None
            host, token = source.read_dokploy_config(control_plane_root=self.root)
            clock = inspect_application_state(
                host=host,
                token=token,
                server_id=volume.server_id,
                application=volume.application_name,
                service_id=predecessor.predecessor.service_id,
                mode="absent",
                include_clock=True,
            )
            if clock is None:
                raise NativeTestingRebuildPending("Fresh original host custody is unavailable.")
            original = {
                "volume": volume.model_dump(mode="json"),
                "retirement": {
                    "binding": adapter.reconciliation_key(),
                    "fresh_until": clock.received_at + VOLUME_RETIREMENT_PERMIT_SECONDS,
                    "boot_id": clock.boot_id,
                    "monotonic_deadline": clock.monotonic + VOLUME_RETIREMENT_PERMIT_SECONDS,
                },
            }
        result = self.run_child(
            adapter,
            route=route,
            key=key,
            request={"retirement": adapter.reconciliation_key()},
            name="retirement_volume",
            original_intent=original,
            parent_lease=lease,
        )
        retired = result.get("retired")
        if not isinstance(retired, bool):
            raise NativeTestingRebuildPending("Original volume retirement is unobserved.")
        if retired:
            self.require_predecessor_absent(predecessor)
        return retired

    def require_retirement_custody(self, record: TestingActivationRecord) -> None:
        target_id = record.retained_target_id.target_id
        if any(
            row.target_id == target_id for row in self.store.list_dokploy_target_id_records()
        ) or any(row.target_id == target_id for row in self.store.list_provider_target_records()):
            raise NativeTestingRebuildPending("A referenced predecessor remains protected.")
        key = generic_web_provider_target_key(
            provider_id=record.retained_provider.provider_id,
            context=record.context,
            instance="testing",
            target_type="application",
            target_id=target_id,
        )
        if any(
            row.provider_target_key == key
            and not any(
                same_reservation_intent(row, original) for original in record.retained_reservations
            )
            for row in self.store.list_held_provider_target_reservations()
        ):
            raise NativeTestingRebuildPending("An unrelated predecessor writer remains protected.")

    def require_predecessor_absent(self, record: TestingActivationRecord) -> None:
        from control_plane.retained_volume_sweep_execution import _host_result

        self.require_predecessor_service_absent(record)
        intent = self.retirement_intent(record)
        for volume in intent.retained_volumes:
            status, _ = _host_result(
                self.root,
                volume,
                "dry-run",
                "testing-rebuild-volume-absence:" + record.plan_sha256,
            )
            if status != "absent":
                raise ValueError("Original predecessor volume absence is unobserved.")

    def require_predecessor_service_absent(self, record: TestingActivationRecord) -> None:
        readiness = record.application_readiness
        assert readiness is not None and readiness.route is not None
        assert readiness.route_execution is not None and record.predecessor is not None
        host, token = source.read_dokploy_config(control_plane_root=self.root)
        inspect_application_state(
            host=host,
            token=token,
            server_id=readiness.route_execution.server_id,
            application=urlsplit(readiness.route.old_backend or "").hostname or "",
            service_id=record.predecessor.service_id,
            mode="absent",
        )

    def require_retirement_quiescent(
        self,
        host: str,
        token: str,
        target_id: str,
        application: str,
        server_id: str,
        *,
        state: RetainedApplicationState | None = None,
    ) -> None:
        payload = api.fetch_dokploy_target_payload(
            host=host, token=token, target_type="application", target_id=target_id
        )
        if (
            payload.get("applicationId") != target_id
            or payload.get("appName") != application
            or str(payload.get("serverId") or "") != server_id
            or payload.get("autoDeploy") is not False
            or payload.get("applicationStatus") not in {"idle", "done", "error"}
            or not application_mount_matches(payload, state)
            or payload.get("previewDeployments") != []
        ):
            raise NativeTestingRebuildPending(
                "The original predecessor is still active or changed."
            )
        history = api.list_deployments_for_target(
            host=host,
            token=token,
            target_type="application",
            target_id=target_id,
            require_complete=True,
        )
        if any(
            row.get("applicationId") != target_id
            or api.deployment_status(row)
            not in api._DEPLOYMENT_SUCCESS_STATUSES | api._DEPLOYMENT_FAILURE_STATUSES
            for row in history
        ):
            raise NativeTestingRebuildPending(
                "An original predecessor deployment remains unsettled."
            )
        queue = api.dokploy_request(host=host, token=token, path="/api/deployment.queueList")
        try:
            schedules = require_disabled_predecessor_schedules(host, token, target_id)
        except TestingRebuildRefused as error:
            raise NativeTestingRebuildPending(str(error)) from error
        if (
            not isinstance(queue, list)
            or len(queue) > 10000
            or not isinstance(schedules, list)
            or len(schedules) > 10000
        ):
            raise NativeTestingRebuildPending(
                "Original predecessor jobs or schedules are unobserved."
            )
        schedule_ids: set[str] = set()
        for schedule in schedules:
            if not isinstance(schedule, dict) or (
                schedule.get("applicationId") != target_id
                or schedule.get("enabled") is not False
                or not isinstance(schedule.get("scheduleId"), str)
                or not schedule["scheduleId"]
                or schedule["scheduleId"] in schedule_ids
            ):
                raise NativeTestingRebuildPending(
                    "An original predecessor schedule remains active or unobserved."
                )
            identifier = schedule["scheduleId"]
            assert isinstance(identifier, str)
            schedule_ids.add(identifier)
            executions = api.dokploy_request(
                host=host,
                token=token,
                path="/api/deployment.allByType",
                query={"id": identifier, "type": "schedule"},
            )
            if (
                not isinstance(executions, list)
                or len(executions) > 10000
                or any(
                    not isinstance(row, dict)
                    or row.get("scheduleId") != identifier
                    or not api.deployment_key(row)
                    or api.deployment_status(row)
                    not in api._DEPLOYMENT_SUCCESS_STATUSES | api._DEPLOYMENT_FAILURE_STATUSES
                    for row in executions
                )
            ):
                raise NativeTestingRebuildPending(
                    "An original predecessor schedule execution remains unsettled."
                )
        for row in queue:
            if not isinstance(row, dict):
                raise NativeTestingRebuildPending("The provider deployment queue is incomplete.")
            data = row.get("data")
            if not isinstance(data, dict):
                raise NativeTestingRebuildPending("The provider deployment queue is incomplete.")
            schedule_id = data.get("scheduleId")
            if (
                (schedule_id is not None and not isinstance(schedule_id, str))
                or not isinstance(row.get("state"), str)
                or not any(
                    isinstance(data.get(name), str) and data[name]
                    for name in ("applicationId", "composeId", "scheduleId")
                )
            ):
                raise NativeTestingRebuildPending("The provider deployment queue is incomplete.")
            if (data.get("applicationId") == target_id or schedule_id in schedule_ids) and row[
                "state"
            ] not in {
                "completed",
                "failed",
            }:
                raise NativeTestingRebuildPending("An original predecessor job remains active.")

    def finalize(
        self,
        adapter: ApplicationActivationAdapter,
        record: TestingActivationRecord,
    ) -> OdooStableTargetReplacementApplyResult:
        self.check()
        self.lease.assert_current()
        self.parent.authorize("testing_rebuild_finalization")
        if not isinstance(self.lease, ProviderEvidenceLease):
            raise ValueError("Testing rebuild requires its original evidence lease.")
        result = run_durable_provider_operation(
            store=self.store,
            scope=self.scope,
            route_path=REBUILD_ROUTE + ":finalization",
            idempotency_key=self.operation.operation_id + ":finalization",
            request_fingerprint=canonical_json_sha256(
                {"testing_rebuild": self.operation.operation_id, "activation": record.plan_sha256}
            ),
            lease_owner=self.lease.evidence_reservation().lease_owner,
            response_trace_id=self.operation.operation_id,
            adapter=RebuildChildAdapter(
                GenericWebTestingRebuildFinalizer(self, adapter, record),
                self.lease,
                self.guard,
                "finalization",
                read_only_observation=True,
            ),
            original_intent={"testing_activation": record.model_dump(mode="json")},
        )
        if result.status not in {"completed", "adopted", "replayed"}:
            raise NativeTestingRebuildPending("Continue the original testing rebuild finalization.")
        return OdooStableTargetReplacementApplyResult.model_validate(
            result.response_payload["result"]
        )

    def run(self) -> OdooStableTargetReplacementApplyResult:
        finalization = self.store.read_idempotency_record(
            scope=self.scope,
            route_path=REBUILD_ROUTE + ":finalization",
            idempotency_key=self.operation.operation_id + ":finalization",
        )
        record = self.activation()
        if (
            finalization is not None
            or record is not None
            and (record.published or record.failure is not None)
        ):
            if record is None:
                raise NativeTestingRebuildPending("The original activation custody is unavailable.")
            adapter = self.application_adapter(record.request)
            self.recovering = adapter.recovering = True
            return self.finalize(adapter, record)
        try:
            self.guard("testing_rebuild_admission")
            if record is None:
                inspect_generic_web_testing_rebuild(self.root, self.store, self.intent)
                self.interlock(hold=True)
                self.prepare()
            adapter, record = self.activate()
        except NativeTestingRebuildPending:
            raise
        except (
            NativeTestingRebuildAdmissionChanged,
            TestingRebuildRefused,
            TestingActivationRefusal,
            SeparationResourcesRefused,
        ) as error:
            return self.finish_admission_refusal(error)
        self.recovering = adapter.recovering = True
        return self.finalize(adapter, record)

    def finish_admission_refusal(self, error: ValueError) -> OdooStableTargetReplacementApplyResult:
        self.recovering = True
        self.guard("testing_rebuild_admission_refusal", recovery=True)
        record = self.activation()
        if record is not None:
            adapter, record = self.activate()
            if record.failure is None:
                raise NativeTestingRebuildPending("Original candidate refusal remains unobserved.")
            return self.finalize(adapter, record)
        for route, key in (
            (RESOURCES_ROUTE.format(product=self.operation.product), self.resources_key),
            (ACTIVATION_ROUTE.format(product=self.operation.product), self.activation_key),
        ):
            child = self.store.read_idempotency_record(
                scope=self.scope, route_path=route, idempotency_key=key
            )
            if child is not None and (child.state != "completed" or key == self.activation_key):
                raise NativeTestingRebuildPending(
                    "Observe the original child custody before ending admission."
                ) from error
        from control_plane.testing_publication_storage import read_snapshot

        current = read_snapshot(
            self.store, product=self.operation.product, context=self.operation.context
        )
        if (
            current.target_id != self.intent.before.target_id
            or current.provider != self.intent.before.provider
            or current.binding != self.intent.before.binding
            or target_without_hold(current.target) != target_without_hold(self.intent.before.target)
        ):
            raise NativeTestingRebuildPending(
                "Original serving custody changed before refusal."
            ) from error
        if self.owns_interlock(current.target.policies.staff_testing_hold):
            self.interlock(hold=False)
        elif current.target.policies.staff_testing_hold is not None:
            raise NativeTestingRebuildPending(
                "A foreign hold protects the refused original."
            ) from error
        return OdooStableTargetReplacementApplyResult(
            product=self.operation.product,
            context=self.operation.context,
            instance="testing",
            strategy="replace-and-cutover",
            deploy_status="fail",
            target_id=self.intent.before.target_id.target_id,
            artifact_id=self.operation.request.artifact_id,
            error_code="testing_rebuild_admission_changed",
            error_message=str(error),
        )

    def result(
        self, record: TestingActivationRecord, *, candidate_health_failed: bool = False
    ) -> OdooStableTargetReplacementApplyResult:
        ready = record.published and record.failure is None
        readiness = record.application_readiness
        retained_records = (
            [predecessor for predecessor, retired in self.retirement_results(record) if not retired]
            if ready
            else []
        )
        retained = [value.retained_target_id.target_id for value in retained_records]
        error_message = ""
        if not ready:
            error_message = "The candidate failed; the original testing copy remains selected."
        elif candidate_health_failed:
            error_message = "The new testing copy failed its final health check. You can deploy a fix to testing."
        if retained:
            error_message += (
                " Retirement of previous testing copies is unverified. Testing can still be updated. "
                "A later testing rebuild checks these copies again while this copy is serving."
            )
        return OdooStableTargetReplacementApplyResult(
            product=self.operation.product,
            context=self.operation.context,
            instance="testing",
            strategy="replace-and-cutover",
            deploy_status="pass" if ready else "fail",
            health_status="pass" if ready and not candidate_health_failed else "fail",
            post_deploy_status="pass" if ready and not retained else "fail",
            deployment_record_id=readiness.runtime_identity.deployment_record_id
            if ready and readiness is not None
            else "",
            runtime_identity_injected=ready,
            target_id=record.target_id if ready else record.retained_target_id.target_id,
            target_name=record.namespace if ready else record.retained_target.target_name,
            artifact_id=record.image_reference,
            image_reference=record.image_reference,
            health_url=rebuild_address(self.intent),
            runtime_source={
                "testing_rebuild": self.operation.operation_id,
                "testing_activation": record.plan_sha256,
                "copy_source": "empty",
                "retained_predecessors": ",".join(retained),
                "retained_predecessor_plans": ",".join(
                    value.plan_sha256 for value in retained_records
                ),
            },
            error_code=record.failure.kind
            if record.failure is not None
            else "candidate_public_health_failed"
            if candidate_health_failed
            else "testing_predecessor_retirement_unverified"
            if retained
            else "",
            error_message=error_message.strip(),
        )
