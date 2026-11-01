from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.dokploy_target_record import DokployTargetStaffTestingHold
from control_plane.contracts.testing_rebuild import REBUILD_ROUTE
from control_plane.contracts.odoo_stable_target_replacement import (
    OdooStableTargetReplacementApplyResult,
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.contracts.testing_activation import (
    TestingActivationRecord,
    TestingActivationRequest,
    TestingActivationFailure,
    TestingActivationRefusal,
)
from control_plane.contracts.testing_application import TestingApplicationSpecification
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
    require_activation_hold_lift,
    target_without_hold,
)
from control_plane.testing_application_activation import (
    ApplicationActivationAdapter,
    undispatched_application_recipient,
)
from control_plane.testing_application_capabilities import retained_application_capability_spec
from control_plane.testing_application_credentials import credential_binding_names
from control_plane.testing_application_runtime import (
    inspect_application_state,
    retire_application_runtime,
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
    NativeTestingRebuildPending,
    check_testing_rebuild_protection,
)
from control_plane.testing_separation_resources import (
    ROUTE as RESOURCES_ROUTE,
    SeparationResourcesAdapter,
    SeparationResourcesRequest,
)


class RebuildRouteUnsupported(TestingActivationRefusal):
    pass


class GenericWebRebuildActivationAdapter(ApplicationActivationAdapter):
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

    def apply(self, key: str, lease: ProviderOperationLease) -> ProviderMutationOutcome:
        try:
            return super().apply(key, lease)
        except ProviderMutationRejectedError as error:
            if not isinstance(error.cause, RebuildRouteUnsupported) or not isinstance(
                lease, ProviderEvidenceLease
            ):
                raise
            record = self.current()
            reservation = lease.evidence_reservation()
            if reservation.provider_effect_started_at or not undispatched_application_recipient(
                record, reservation
            ):
                raise
            with self.evidence(lease):
                lease.assert_current()
                self.require_unchanged(record)
                self.require_recovery_unused(record)
                outcome = self.refuse(
                    record, TestingActivationFailure(kind="authority_refused_before_recipient")
                )
                return replace(outcome, provider_effect_performed=False)


FINALIZATION_CLEAR_PHASE = "clear_interlock"
FINALIZATION_HEALTH_FAILED_CLEAR_PHASE = "clear_interlock_after_health_failure"


def rebuild_application_specification(
    intent: GenericWebTestingRebuildIntent,
) -> TestingApplicationSpecification:
    if intent.state.kind != "none" or intent.before.target.target_type != "application":
        raise TestingRebuildRefused(
            TESTING_REBUILD_EXECUTION_UNAVAILABLE_CODE,
            "This rebuild currently supports applications without persistent state.",
        )
    return TestingApplicationSpecification(
        runtime_port=intent.profile.runtime_port,
        health_path=intent.profile.health_path,
        network_name=DOKPLOY_INGRESS_NETWORK,
        state=intent.state,
    )


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
    if (
        payload.get("applicationId") != intent.before.target_id.target_id
        or payload.get("mounts") != []
        or any(
            (row.context, row.instance) != (intent.before.target.context, "testing")
            and row.target_id == intent.before.target_id.target_id
            for row in store.list_dokploy_target_id_records()
        )
    ):
        raise TestingRebuildRefused(
            "testing_rebuild_state_unverified",
            "The testing application must have no persistent mounts or shared target.",
        )
    credential_binding_names(
        store, intent.profile.product, intent.before.target.context, specification
    )
    retained = retained_application_capability_spec(
        store, root, intent.profile.product, intent.before.target.context
    )
    inspect_application_state(
        host=host,
        token=token,
        server_id=str(payload.get("serverId") or ""),
        application=str(retained["application"]),
        mode="stateless",
    )
    return specification


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
        self.guard(self.name + ":admission")
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
            return ProviderMutationOutcome(200, {"result": result.model_dump(mode="json")})


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
        retired = False
        if self.record.failure is None:
            if not self.record.published or self.record.application_readiness is None:
                raise NativeTestingRebuildPending(
                    "The original candidate is not qualified for retirement."
                )
            retired = self.execution.retire(self.activation, self.record, lease=lease)
            health = self.activation.read_edge_health(
                self.record, expected=self.record.application_readiness.runtime_identity
            )
            if not health.verified or health.status not in {"pass", "fail"}:
                raise NativeTestingRebuildPending(
                    "The final same-address health remains unobserved."
                )
            health_failed = (
                not retired or health.status == "fail" or health.runtime_identity_status != "match"
            )

        def checkpoint(phase: str) -> None:
            if phase == FINALIZATION_CLEAR_PHASE and retired:
                self.execution.require_predecessor_absent(self.record)
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

    def activation(self) -> TestingActivationRecord | None:
        return activation_record(
            self.store,
            self.operation.product,
            self.operation.context,
            scope=self.scope,
            preparation_key=self.resources_key,
        )

    def guard(self, phase: str) -> None:
        self.check()
        self.lease.assert_current()
        self.parent.authorize(phase)
        driver = resolve_testing_rebuild_driver(self.store, self.operation.product)
        if driver.profile != self.intent.profile or driver.lane.context != self.operation.context:
            raise NativeTestingRebuildPending("The original rebuild profile changed.")
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
            and row.target_id in {expected_id, self.intent.before.target_id.target_id}
            for row in self.store.list_dokploy_target_id_records()
        ):
            raise NativeTestingRebuildPending("A shared target is protected from this rebuild.")
        target = self.store.read_dokploy_target_record(
            context_name=self.operation.context, instance_name="testing"
        )
        if current is None and target_without_hold(target) != target_without_hold(
            self.intent.before.target
        ):
            raise NativeTestingRebuildPending("The retained testing target changed.")
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
        self.guard("testing_rebuild_interlock")
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
            verified = require_activation_hold_lift(self.store, request, self.actor, self.root)
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
            adapter=RebuildChildAdapter(child, self.lease, self.guard, name),
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
        self.run_child(
            adapter,
            route=route,
            key=self.resources_key,
            request=adapter.request.model_dump(mode="json"),
            name="resources",
        )

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
        adapter = GenericWebRebuildActivationAdapter(
            self.root,
            self.store,
            self.operation.product,
            request,
            self.actor,
            self.scope,
            self.operation.operation_id,
        )
        if original is None:
            captured = adapter.current()
            if captured.image_reference != self.operation.request.artifact_id:
                raise NativeTestingRebuildPending("The selected verified build changed.")
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

    def retire(
        self,
        adapter: ApplicationActivationAdapter,
        record: TestingActivationRecord,
        *,
        lease: ProviderOperationLease,
    ) -> bool:
        self.guard("testing_rebuild_retirement")
        adapter.require_unchanged(record)
        readiness = record.application_readiness
        if not record.published or readiness is None:
            raise NativeTestingRebuildPending("The original candidate has not been published.")
        old_id = record.retained_target_id.target_id
        if old_id != self.intent.before.target_id.target_id or old_id == record.target_id:
            raise ValueError("Retirement must identify the original independent predecessor.")
        if (
            readiness.route is None
            or not isinstance(readiness.route.old_backend, str)
            or readiness.route_execution is None
            or record.predecessor is None
        ):
            raise NativeTestingRebuildPending(
                "Original physical predecessor custody is unavailable."
            )
        application = urlsplit(readiness.route.old_backend).hostname or ""
        host, token = source.read_dokploy_config(control_plane_root=self.root)
        payload = None
        try:
            payload = api.fetch_dokploy_target_payload(
                host=host, token=token, target_type="application", target_id=old_id
            )
        except api.DokployRequestFailed as error:
            if error.status_code == 404:
                pass
            else:
                raise
        if payload is not None:
            adapter.verify_provider(record)
            health = adapter.read_edge_health(record, expected=readiness.runtime_identity)
            if (
                health.verified
                and health.status == "fail"
                and health.runtime_identity_status == "match"
            ):
                return False
            if (
                not health.verified
                or health.status != "pass"
                or health.runtime_identity_status != "match"
            ):
                raise NativeTestingRebuildPending(
                    "Current same-address candidate health is unavailable."
                )
            if (
                payload.get("appName") != application
                or str(payload.get("serverId") or "") != readiness.route_execution.server_id
                or payload.get("previewDeployments") not in (None, [])
            ):
                raise NativeTestingRebuildPending(
                    "The original predecessor placement or previews changed."
                )
            if payload.get("autoDeploy") is not False:
                self.guard("testing_rebuild_disable_old_deploys")
                lease.checkpoint_effect("disable_old_deploys")
                api.dokploy_request(
                    host=host,
                    token=token,
                    method="POST",
                    path="/api/application.update",
                    payload={"applicationId": old_id, "autoDeploy": False},
                )
            self.require_retirement_quiescent(
                host, token, old_id, application, readiness.route_execution.server_id
            )
            require_unmounted_retirement_application(control_plane_root=self.root, target_id=old_id)
            inspect_application_state(
                host=host,
                token=token,
                server_id=readiness.route_execution.server_id,
                application=application,
                service_id=record.predecessor.service_id,
                mode="stateless",
            )
            self.guard("testing_rebuild_retire_predecessor")
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
            FINALIZATION_CLEAR_PHASE,
            FINALIZATION_HEALTH_FAILED_CLEAR_PHASE,
        }:
            raise NativeTestingRebuildPending(
                "The original predecessor deletion authority is unobserved."
            )

        def dispatch(remove: Callable[[], None]) -> None:
            self.guard("testing_rebuild_remove_retired_service")
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
            service_id=record.predecessor.service_id,
            dispatch_guard=dispatch,
        )
        self.require_predecessor_absent(record)
        return True

    def require_predecessor_absent(self, record: TestingActivationRecord) -> None:
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
        self, host: str, token: str, target_id: str, application: str, server_id: str
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
            or payload.get("mounts") != []
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
        schedules = api.dokploy_request(
            host=host,
            token=token,
            path="/api/schedule.list",
            query={"id": target_id, "scheduleType": "application"},
        )
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
        if finalization is not None:
            record = self.activation()
            if record is None:
                raise NativeTestingRebuildPending("The original activation custody is unavailable.")
            adapter = GenericWebRebuildActivationAdapter(
                self.root,
                self.store,
                self.operation.product,
                record.request,
                self.actor,
                self.scope,
                self.operation.operation_id,
            )
            return self.finalize(adapter, record)
        self.guard("testing_rebuild_admission")
        existing = self.activation()
        if existing is None:
            inspect_generic_web_testing_rebuild(self.root, self.store, self.intent)
        if existing is None or not existing.published:
            self.interlock(hold=True)
        self.prepare()
        adapter, record = self.activate()
        return self.finalize(adapter, record)

    def result(
        self, record: TestingActivationRecord, *, candidate_health_failed: bool = False
    ) -> OdooStableTargetReplacementApplyResult:
        ready = record.published and record.failure is None
        readiness = record.application_readiness
        return OdooStableTargetReplacementApplyResult(
            product=self.operation.product,
            context=self.operation.context,
            instance="testing",
            strategy="replace-and-cutover",
            deploy_status="pass" if ready else "fail",
            health_status="pass" if ready and not candidate_health_failed else "fail",
            post_deploy_status="pass" if ready else "fail",
            deployment_record_id=readiness.runtime_identity.deployment_record_id
            if ready and readiness is not None
            else "",
            runtime_identity_injected=ready,
            target_id=record.target_id if ready else record.retained_target_id.target_id,
            target_name=record.namespace if ready else record.retained_target.target_name,
            artifact_id=record.image_reference,
            image_reference=record.image_reference,
            health_url=next(
                lane.base_url for lane in self.intent.profile.lanes if lane.instance == "testing"
            ),
            runtime_source={
                "testing_rebuild": self.operation.operation_id,
                "testing_activation": record.plan_sha256,
                "copy_source": "empty",
            },
            error_code=record.failure.kind
            if record.failure is not None
            else "candidate_public_health_failed"
            if candidate_health_failed
            else "",
            error_message="The candidate failed; the original testing copy remains selected."
            if not ready
            else "The new testing copy failed its final health check. You can deploy a fix to testing."
            if candidate_health_failed
            else "",
        )
