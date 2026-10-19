"""The native testing rebuild inside the existing durable replacement action."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
import json
import logging
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlsplit

import click
from pydantic import Field

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.durable_operation_authorization import (
    DurableOperationAuthorizationDeniedError,
)
from control_plane.contracts.promotion_record import HealthcheckEvidence
from control_plane.contracts.odoo_stable_target_replacement import (
    OdooStableTargetReplacementApplyResult,
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.contracts.prepared_public_site import FrozenRecord
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    is_exclusive_product_context,
    product_context_owner_map,
)
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.contracts.testing_candidate_application import TestingCandidateApplicationPlan
from control_plane.contracts.testing_candidate import (
    TestingCandidateAllocation,
    testing_candidate_namespace,
)
from control_plane.contracts.testing_publication import (
    TestingPublicationPlan,
    TestingServingSnapshot,
)
from control_plane.contracts.testing_rebuild import (
    REBUILD_SCOPE,
    REBUILD_ROUTE,
    TestingCopy,
    TestingCopyReadiness,
    TestingCopyReceipt,
    TestingRebuildPlan,
    TestingRebuildRecord,
    TestingRouteObservation,
)
from control_plane.dokploy import api, post_deploy, source
from control_plane.dokploy.native_copy_stream import (
    require_registered_remote_server,
    NativeSourceUnavailable,
    NativeSourceAuthorizationDenied,
)
from control_plane.dokploy.testing_retained_copy import retained_copy_operation
from control_plane.dokploy.testing_route import (
    TestingLaneRoute,
    TestingRoutePending,
    compile_testing_route,
    testing_ingress_entrypoints,
    observe_testing_route,
    testing_route_id,
)
from control_plane.dokploy.testing_route_transport import (
    DokployTestingRouteTransport,
)
from control_plane.dokploy.testing_route_execution import (
    TestingRouteExecutionIntent,
)
from control_plane.testing_route_effects import (
    TestingRouteEffects,
    TestingRouteFenceController,
    ROUTE_PREPARING,
)
from control_plane.lane_usage import evaluate_lane_usage
from control_plane.odoo_testing_usage import read_odoo_testing_usage
from control_plane.provider_operations import (
    ProviderEvidenceLease,
    DurableProviderOperationResult,
    ProviderMutationOutcome,
    ProviderObservation,
    ProviderOperationLease,
    run_durable_provider_operation,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_candidate_allocation import (
    observe_candidate_creation,
    observe_candidate_storage,
    prepare_candidate_storage,
    require_candidate_target_custody,
)
from control_plane.testing_candidate_application import (
    observe_candidate_configuration,
    prepare_candidate_application,
    candidate_private_readiness,
    abandon_candidate_application,
    candidate_restore_terminal,
    candidate_build_terminal,
    CandidateObservationUnavailable,
    CandidatePrivateReadinessFailed,
    candidate_terminal_custody_evidence,
)
from control_plane.testing_candidate_runtime import (
    CandidateRuntimeInputsChanged,
    testing_profile_binding,
    CandidateSource,
    TestingCandidateRuntimeInputs,
    candidate_application_intent,
    candidate_runtime_inputs,
)
from control_plane.testing_publication import plan_testing_publication
from control_plane.testing_publication_storage import (
    effective_after,
    prepare_publication,
    publish,
    read_publication,
    read_snapshot,
    serving_snapshot_matches,
)
from control_plane.testing_rebuild_storage import RecordTestingRebuildStore
from control_plane.testing_replacement_resources import IncompleteTestingResources
from control_plane.testing_serving_copy import published_testing_copy
from control_plane.workflows.generic_web_deploy_provider import generic_web_provider_target_key
from control_plane.workflows.testing_rebuild import (
    TestingRebuildPreparationFailure,
    TestingRebuildObservationUnavailable,
    TestingRebuildRouteFailure,
    execute_testing_rebuild,
)


class NativeTestingRebuildPending(ValueError):
    """The original effects need another observation, never an alternate dispatch."""


class NativeTestingRebuildAdmissionChanged(ValueError):
    """A source-defined admission refusal with a safe, useful operator cause."""


class TestingRebuildRefused(ValueError):
    """A fixed operator-facing prerequisite, without provider or secret text."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class NativeTestingRebuildIntent(FrozenRecord):
    operation_id: str
    profile: LaunchplaneProductProfileRecord
    before: TestingServingSnapshot
    server_id: str
    project: str
    old: TestingCopy
    retained: dict[str, Any]
    public_entrypoints: tuple[str, ...]
    old_auto_deploy: bool | None = Field(default=None, exclude_if=lambda value: value is None)
    recorded_template: dict[str, Any] | None = None

    @property
    def digest(self) -> str:
        return canonical_json_sha256(self.model_dump(mode="json"))


def native_rebuild_effects_started(
    store: PostgresRecordStore, operation: OdooStableTargetReplacementOperationRecord
) -> bool:
    return store.native_testing_rebuild_effects_started(operation)


def rebuild_source(operation: OdooStableTargetReplacementOperationRecord) -> CandidateSource:
    modes: dict[str, CandidateSource] = {
        "empty": "empty",
        "production_restore": "production",
        "upstream_restore": "upstream",
    }
    if not operation.is_native_testing_rebuild or operation.request.data_source_mode not in modes:
        raise ValueError("A native rebuild requires its explicit testing source.")
    return modes[operation.request.data_source_mode]


def capture_rebuild_intent(
    root: Path, store: PostgresRecordStore, operation: OdooStableTargetReplacementOperationRecord
) -> NativeTestingRebuildIntent:
    rebuild_source(operation)
    profile = store.read_product_profile_record(operation.product)
    if (
        not profile.is_active
        or profile.driver_id != "odoo"
        or not is_exclusive_product_context(
            context=operation.context,
            product=operation.product,
            owners=product_context_owner_map(store.list_product_profile_records()),
        )
    ):
        raise ValueError("Rebuild requires an exclusive active Odoo testing context.")
    lane = next(
        item
        for item in profile.lanes
        if (item.context, item.instance) == (operation.context, "testing")
    )
    before = read_snapshot(store, product=operation.product, context=operation.context)
    if (before.target.context, before.target.instance, before.target.target_type) != (
        operation.context,
        "testing",
        "compose",
    ) or before.provider.target_id != before.target_id.target_id:
        raise ValueError("The testing target records disagree.")
    host, token = source.read_dokploy_config(
        control_plane_root=root, database_url=store.database_url
    )
    provider = api.fetch_dokploy_target_payload(
        host=host, token=token, target_type="compose", target_id=before.target_id.target_id
    )
    if (provider.get("composeId"), provider.get("name")) != (
        before.target_id.target_id,
        before.target.target_name,
    ) or not provider.get("appName"):
        raise ValueError("The actual testing target differs from its record.")
    old_auto_deploy = provider.get("autoDeploy")
    if not isinstance(old_auto_deploy, bool):
        raise ValueError("The original testing automatic-deploy setting is unavailable.")
    server = require_registered_remote_server(provider.get("serverId"))
    project = str(provider["appName"])
    retained = retained_copy_operation(
        host=host,
        token=token,
        server_id=server,
        payload={
            "mode": "read",
            "project": project,
            "context": operation.context,
        },
    )
    identity = RuntimeIdentity.model_validate(retained["identity"])
    transport = DokployTestingRouteTransport(
        host=host,
        token=token,
        server_id=server,
        old_project=project,
        product=operation.product,
        context=operation.context,
        public_host=str(urlsplit(lane.base_url).hostname),
        entrypoints=(),
        old_runtime_identity=identity,
    )
    try:
        frame = transport.read()
    except NativeSourceAuthorizationDenied:
        raise
    except (ValueError, click.ClickException) as error:
        raise TestingRebuildRefused(
            "testing_route_read_unavailable", "The applied testing ingress could not be read."
        ) from error
    previous = published_testing_copy(
        store, product=operation.product, context=operation.context, for_rebuild_admission=True
    )
    template = previous.plan.route.template if previous else None
    endpoints = frame["old_endpoints"]
    if not endpoints:
        raise TestingRebuildRefused(
            "testing_route_backend_unavailable",
            "The original testing private backend is unavailable.",
        )
    raw = frame["raw"]
    try:
        points = testing_ingress_entrypoints(raw, str(urlsplit(lane.base_url).hostname))
    except ValueError as error:
        raise TestingRebuildRefused(
            "testing_route_entrypoints_unavailable", "The testing application's ingress is unbound."
        ) from error
    # Bind the actual selected backend, not an arbitrary address on the old process.
    owned = json.loads(frame["owned_file"]) if frame["owned_file"] is not None else None
    namespace = testing_candidate_namespace(
        operation.product, operation.context, operation.operation_id
    )
    possible = []
    for endpoint in endpoints:
        try:
            compile_testing_route(
                product=operation.product,
                context=operation.context,
                host=str(urlsplit(lane.base_url).hostname),
                old_backend=endpoint,
                old_backend_addresses=tuple(endpoints),
                new_backend=f"http://{namespace}:{profile.runtime_port}",
                raw=raw,
                public_entrypoints=points,
                owned_document=owned,
                recorded_template=template,
            )
        except ValueError:
            continue
        possible.append(endpoint)
    if not possible:
        raise TestingRebuildRefused(
            "testing_route_compilation_failed",
            "The observed testing ingress cannot be safely compiled for its retained copy.",
        )
    # The host binds all these aliases to the same exact, unshared web process.
    # An owned route already restricts this set to its recorded selected alias.
    endpoint = sorted(possible)[0]
    return NativeTestingRebuildIntent(
        operation_id=operation.operation_id,
        profile=profile,
        before=before,
        server_id=server,
        project=project,
        old=TestingCopy(
            copy_id=before.target_id.target_id,
            target_id=before.target_id.target_id,
            private_url=endpoint,
            resource_binding=retained["physical_binding"],
            runtime_identity=identity,
        ),
        retained=retained,
        public_entrypoints=points,
        old_auto_deploy=old_auto_deploy,
        recorded_template=template,
    )


def check_testing_rebuild_protection(
    root: Path,
    store: PostgresRecordStore,
    operation: OdooStableTargetReplacementOperationRecord,
    intent: NativeTestingRebuildIntent,
) -> None:
    target = store.read_dokploy_target_record(
        context_name=operation.context, instance_name="testing"
    )
    if target.policies.staff_testing_hold is not None:
        raise TestingRebuildRefused(
            "testing_staff_hold", "This testing lane is held for staff testing."
        )
    safety = store.read_disposable_lane_safety(operation.product, operation.context, "testing")
    if safety is not None:
        if safety.data == "only_copy":
            raise TestingRebuildRefused(
                "testing_needed_copy", "Testing holds the only needed copy of data."
            )
        if safety.production_resources in {"shared", "separation_ready"}:
            raise TestingRebuildRefused(
                "testing_production_resources_shared",
                "Testing must be separated from its shared production resources before rebuilding.",
            )
    usage = read_odoo_testing_usage(
        target=intent.before.target,
        target_id=intent.before.target_id,
        control_plane_root=root,
        database_url=store.database_url,
    )
    reported_usage = "in_use" if safety is not None and safety.usage == "in_use" else usage.usage
    decision = evaluate_lane_usage(
        store,
        profile=intent.profile,
        target=target,
        target_id=intent.before.target_id,
        instance="testing",
        usage=reported_usage,
        require_unused=reported_usage != "unknown",
    )
    if decision.protected:
        raise TestingRebuildRefused("testing_in_use", "Someone is using this testing lane.")


class NativeTestingRebuildAdapter:
    def __init__(
        self,
        root: Path,
        store: PostgresRecordStore,
        operation: OdooStableTargetReplacementOperationRecord,
        intent: NativeTestingRebuildIntent,
        authorize: Callable[[str], None],
    ) -> None:
        self.root, self.store, self.operation, self.intent, self.authorize = (
            root,
            store,
            operation,
            intent,
            authorize,
        )
        self.namespace = testing_candidate_namespace(
            operation.product, operation.context, operation.operation_id
        )
        self.checkpoints = RecordTestingRebuildStore(store)

    def target_key(self) -> str:
        return generic_web_provider_target_key(
            provider_id="dokploy",
            context=self.operation.context,
            instance="testing",
            target_type="compose",
            target_id=self.intent.old.target_id,
        )

    def reconciliation_key(self) -> str:
        return self.intent.digest

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        if reconciliation_key != self.intent.digest:
            return ProviderObservation("unknown")
        # Each component persists its original dispatch before sending it. The
        # exclusive executor can safely continue those observations; none treats
        # a missing/old outcome as permission to repeat its provider command.
        with self.store.testing_replacement_execution_lock(self.operation.operation_id) as check:
            if check is None:
                return ProviderObservation("unknown")
            return ProviderObservation("absent", retry_safe=True)

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        with self.store.testing_replacement_execution_lock(self.operation.operation_id) as check:
            if check is None:
                raise NativeTestingRebuildPending("The original rebuild still has an executor.")
            executor = NativeTestingRebuildExecution(self, lease, check)
            try:
                record = executor.run()
            except NativeTestingRebuildAdmissionChanged as error:
                result = executor.finish_admission_refusal(error)
            else:
                result = rebuild_result(record, executor.allocation)
            assert isinstance(lease, ProviderEvidenceLease)
            return ProviderMutationOutcome(
                200,
                {
                    "result": result.model_dump(mode="json"),
                },
                provider_effect_performed=bool(
                    lease.evidence_reservation().provider_effect_started_at
                ),
            )


class NativeTestingRebuildExecution:
    def __init__(
        self,
        adapter: NativeTestingRebuildAdapter,
        lease: ProviderOperationLease,
        check_lock: Callable[[], None],
    ) -> None:
        self.adapter, self.lease, self.check_lock = adapter, lease, check_lock
        self.root, self.store, self.operation, self.intent = (
            adapter.root,
            adapter.store,
            adapter.operation,
            adapter.intent,
        )
        self.host: str
        self.token: str
        self.inputs: TestingCandidateRuntimeInputs | None = None
        self.allocation: TestingCandidateAllocation
        self.plan: TestingRebuildPlan
        self.route: TestingLaneRoute
        self.transport: DokployTestingRouteTransport
        self.publication: TestingPublicationPlan | None = None

    @property
    def base_url(self) -> str:
        return next(
            item.base_url
            for item in self.intent.profile.lanes
            if (item.context, item.instance) == (self.operation.context, "testing")
        ).rstrip("/")

    def cutover_started(self) -> bool:
        plan = getattr(self, "plan", None)
        if plan is None:
            return False
        original = self.store.read_idempotency_record(
            scope=REBUILD_SCOPE,
            route_path=REBUILD_ROUTE + ":effect",
            idempotency_key=self.operation.operation_id + ":route:" + plan.new.copy_id,
        )
        return original is not None and bool(original.provider_effect_started_at)

    def route_exclusion(self, plan: TestingRebuildPlan) -> bool:
        phase = "route:" + plan.new.copy_id
        original = self.store.read_idempotency_record(
            scope=REBUILD_SCOPE,
            route_path=REBUILD_ROUTE + ":effect",
            idempotency_key=self.operation.operation_id + ":" + phase,
        )
        return (
            original is not None
            and original.state == "completed"
            and original.response_payload.get("route_write_not_started") is True
            and original.request_fingerprint
            == canonical_json_sha256({"intent": self.intent.digest, "phase": phase})
            and original.provider_target_key == self.adapter.target_key() + ":" + phase
        )

    def finish_admission_refusal(
        self, error: NativeTestingRebuildAdmissionChanged
    ) -> OdooStableTargetReplacementApplyResult:
        self.check_lock()
        self.lease.assert_current()
        self.adapter.authorize("testing_rebuild_checkpoint")
        for copy_id in (self.intent.old.copy_id, self.adapter.namespace):
            route = self.store.read_idempotency_record(
                scope=REBUILD_SCOPE,
                route_path=REBUILD_ROUTE + ":effect",
                idempotency_key=self.operation.operation_id + ":route:" + copy_id,
            )
            if route is not None and route.provider_effect_started_at:
                raise NativeTestingRebuildPending(str(error)) from error
        checkpoint = None
        if hasattr(self, "plan"):
            try:
                checkpoint = self.adapter.checkpoints.load(self.plan)
            except FileNotFoundError:
                pass
        if checkpoint is not None and (
            checkpoint.step not in {"preparing", "prepared", "switching"}
            or checkpoint.publication_started
            or checkpoint.retirement_started
            or checkpoint.rollback_started
        ):
            raise NativeTestingRebuildPending(str(error)) from error
        current = read_snapshot(
            self.store, product=self.operation.product, context=self.operation.context
        )
        if (
            current.target_id.target_id != self.intent.old.target_id
            or current.provider.target_id != self.intent.before.provider.target_id
        ):
            raise NativeTestingRebuildPending(str(error)) from error
        candidate = self.store.read_testing_candidate(self.adapter.namespace)
        if candidate is not None:
            if candidate.effect in {"project_id", "environment_id", "compose_id"} and not getattr(
                candidate, candidate.effect
            ):
                candidate = observe_candidate_creation(
                    root=self.root,
                    store=self.store,
                    namespace=candidate.namespace,
                    guard=lambda: self.guard(recovery=True),
                )
                if candidate is None:
                    raise NativeTestingRebuildPending(
                        "Observe the original candidate creation before ending admission."
                    ) from error
            if candidate.effect == "storage" and not candidate.resources_binding_sha256:
                try:
                    candidate = observe_candidate_storage(
                        root=self.root,
                        store=self.store,
                        allocation=candidate,
                        lease=self.lease,
                        guard=lambda: self.guard(recovery=True),
                    )
                except IncompleteTestingResources:
                    # The existing inspector refuses active schedules and
                    # distinguishes terminal, empty/incomplete resources from
                    # unknown or unsafe backing data. Retain the failed copy;
                    # admission refusal must not dispatch preparation again.
                    self.guard(recovery=True)
            app = candidate.application
            if app is not None and app.phase in {"configuring", "configured"}:
                candidate = observe_candidate_configuration(
                    root=self.root,
                    store=self.store,
                    record=candidate,
                    lease=self.lease,
                    guard=lambda: self.guard(recovery=True),
                    retain_configured=True,
                )
                app = candidate.application
            if app is not None and app.phase in {"deploying", "restoring"}:
                candidate_terminal_custody_evidence(
                    root=self.root, store=self.store, record=candidate
                )
                self.guard(recovery=True)
        frame = self.transport.read()
        self.route = compile_testing_route(
            product=self.operation.product,
            context=self.operation.context,
            host=str(urlsplit(self.base_url).hostname),
            old_backend=self.intent.old.private_url,
            old_backend_addresses=tuple(frame["old_endpoints"]),
            new_backend=f"http://{self.adapter.namespace}:{self.intent.profile.runtime_port}",
            raw=frame["raw"],
            public_entrypoints=self.intent.public_entrypoints,
            owned_document=json.loads(frame["owned_file"])
            if frame["owned_file"] is not None
            else None,
            recorded_template=self.intent.recorded_template,
        )
        if (
            observe_testing_route(
                self.route,
                frame["raw"],
                json.loads(frame["owned_file"]) if frame["owned_file"] is not None else None,
            )
            != self.intent.old.private_url
        ):
            raise NativeTestingRebuildPending(str(error)) from error
        if checkpoint is not None:
            if candidate is None:
                raise NativeTestingRebuildPending("The original candidate custody is unavailable.")
            observation = self.current_route(checkpoint.plan)
            if observation.slot_id != checkpoint.plan.old.copy_id:
                raise NativeTestingRebuildPending(str(error)) from error
            now = datetime.now(UTC)
            failed = TestingRebuildRecord.model_validate(
                {
                    **checkpoint.model_dump(),
                    "step": "failed",
                    "error": "testing_rebuild_admission_changed",
                    "admission_refusal": str(error),
                    "route": observation,
                    "updated_at": now,
                    "ended_at": now,
                }
            )
            self.check_lock()
            self.lease.assert_current()
            self.adapter.checkpoints.save(failed)
            result = rebuild_result(failed, candidate)
        else:
            result = OdooStableTargetReplacementApplyResult(
                product=self.operation.product,
                context=self.operation.context,
                instance="testing",
                strategy="replace-and-cutover",
                deploy_status="fail",
                target_id=self.intent.old.target_id,
                artifact_id=self.operation.request.artifact_id,
                error_code="testing_rebuild_admission_changed",
                error_message=str(error),
            )
        logging.warning(
            "Testing rebuild %s ended before cutover: %s", self.operation.operation_id, error
        )
        return result

    def guard(self, plan: TestingRebuildPlan | None = None, *, recovery: bool = False) -> None:
        self.check_lock()
        self.lease.assert_current()
        self.adapter.authorize("testing_rebuild_checkpoint")
        if not recovery and not is_exclusive_product_context(
            context=self.operation.context,
            product=self.operation.product,
            owners=product_context_owner_map(self.store.list_product_profile_records()),
        ):
            raise NativeTestingRebuildAdmissionChanged(
                "The testing context no longer belongs exclusively to this product."
            )
        if not recovery:
            try:
                current_profile = testing_profile_binding(
                    self.store.read_product_profile_record(self.operation.product),
                    self.operation.context,
                )
            except (ValueError, FileNotFoundError) as error:
                raise NativeTestingRebuildAdmissionChanged(
                    "The admitted testing runtime is no longer configured."
                ) from error
            if current_profile != testing_profile_binding(
                self.intent.profile, self.operation.context
            ):
                raise NativeTestingRebuildAdmissionChanged(
                    "The original testing runtime profile changed."
                )
        target = self.store.read_dokploy_target_record(
            context_name=self.operation.context, instance_name="testing"
        )
        if not recovery and target.policies.staff_testing_hold is not None:
            raise NativeTestingRebuildPending("Staff testing holds this lane.")
        self.old_provider()
        current_target = self.store.read_dokploy_target_id_record(
            context_name=self.operation.context, instance_name="testing"
        ).target_id
        candidate = self.store.read_testing_candidate(self.adapter.namespace)
        if candidate is not None and candidate.compose_id:
            if current_target == candidate.compose_id:
                published_testing_copy(
                    self.store,
                    product=self.operation.product,
                    context=self.operation.context,
                    recovery_operation_id=self.operation.operation_id if recovery else "",
                )
            else:
                try:
                    require_candidate_target_custody(self.store, candidate)
                except (ValueError, FileNotFoundError) as error:
                    if recovery:
                        raise NativeTestingRebuildPending(
                            "Recovery is paused: the original candidate custody cannot be verified. "
                            "Resolve its physical reservation or conflicting lane alias first."
                        ) from error
                    raise
        if any(
            item.target_id in {self.intent.old.target_id, current_target}
            and (item.context, item.instance) != (self.operation.context, "testing")
            for item in self.store.list_dokploy_target_id_records()
        ):
            if recovery:
                raise NativeTestingRebuildPending(
                    "Recovery is paused: a rebuild copy aliases another serving lane. "
                    "Resolve the conflicting ownership before continuing this operation."
                )
            raise NativeTestingRebuildAdmissionChanged(
                "A rebuild copy aliases another serving lane."
            )

        safety = self.store.read_disposable_lane_safety(
            self.operation.product, self.operation.context, "testing"
        )
        if safety is not None and (
            safety.data == "only_copy"
            or safety.production_resources in {"shared", "separation_ready"}
        ):
            if recovery:
                raise NativeTestingRebuildPending(
                    "Recovery is paused by protected-copy or production-resource evidence. "
                    "Refresh and resolve that safety evidence before continuing this operation."
                )
            raise NativeTestingRebuildAdmissionChanged(
                "The lane has a recorded protected copy or shared production resource."
            )
        if not recovery and not self.cutover_started():
            usage = read_odoo_testing_usage(
                target=self.intent.before.target,
                target_id=self.intent.before.target_id,
                control_plane_root=self.root,
                database_url=self.store.database_url,
            )
            reported_usage = (
                "in_use" if safety is not None and safety.usage == "in_use" else usage.usage
            )
            decision = evaluate_lane_usage(
                self.store,
                profile=self.intent.profile,
                target=target,
                target_id=self.intent.before.target_id,
                instance="testing",
                usage=reported_usage,
                require_unused=reported_usage != "unknown",
            )
            if decision.protected:
                raise NativeTestingRebuildPending("The retained testing copy is in use.")
        # The original route effect checkpoints the final admission check.
        # Completing that same cutover must not re-qualify historical use after
        # its own health/identity publication invalidates the old down evidence.
        retained_copy_operation(
            host=self.host,
            token=self.token,
            server_id=self.intent.server_id,
            payload=self.retained_payload("read"),
        )
        current = read_snapshot(
            self.store, product=self.operation.product, context=self.operation.context
        )
        publication = read_publication(self.store, self.publication_key)
        expected = (
            effective_after(publication)
            if publication is not None and publication.state == "applied"
            else self.intent.before
        )
        if not recovery and not serving_snapshot_matches(current, expected):
            raise NativeTestingRebuildAdmissionChanged(
                "Testing serving configuration changed outside the original rebuild."
            )
        if not post_deploy.compose_data_workflow_is_quiescent(
            host=self.host,
            token=self.token,
            target_definition=source.DokployTargetDefinition(
                context=self.operation.context,
                instance="testing",
                target_type="compose",
                target_id=self.intent.old.target_id,
            ),
        ):
            raise NativeTestingRebuildPending(
                "The retained testing copy still has a data workflow."
            )
        deployments = api.list_deployments_for_target(
            host=self.host,
            token=self.token,
            target_type="compose",
            target_id=self.intent.old.target_id,
            require_complete=True,
        )
        if any(
            api.deployment_status(item)
            not in api._DEPLOYMENT_SUCCESS_STATUSES | api._DEPLOYMENT_FAILURE_STATUSES
            for item in deployments
        ):
            raise NativeTestingRebuildPending(
                "The retained testing copy still has an unresolved deployment."
            )

    @property
    def publication_key(self) -> str:
        return canonical_json_sha256(
            {
                "product": self.operation.product,
                "context": self.operation.context,
                "operation": self.operation.operation_id,
            }
        )

    def old_provider(self) -> dict[str, Any]:
        provider = api.fetch_dokploy_target_payload(
            host=self.host,
            token=self.token,
            target_type="compose",
            target_id=self.intent.old.target_id,
        )
        if provider.get("composeType") != "docker-compose":
            raise ValueError("Native retirement requires a Docker Compose testing target.")
        if (
            provider.get("composeId"),
            provider.get("name"),
            provider.get("appName"),
            provider.get("serverId"),
        ) != (
            self.intent.old.target_id,
            self.intent.before.target.target_name,
            self.intent.project,
            self.intent.server_id,
        ):
            raise ValueError("The retained testing provider placement changed.")
        return provider

    def retained_payload(self, mode: str) -> dict[str, Any]:
        return {
            "mode": mode,
            "project": self.intent.project,
            "context": self.operation.context,
            "identity": self.intent.retained["identity"],
            "resource_binding": self.intent.retained["resource_binding"],
            "physical_binding": self.intent.old.resource_binding,
        }

    def build_plan(
        self, allocation: TestingCandidateAllocation, app_plan: TestingCandidateApplicationPlan
    ) -> TestingRebuildPlan:
        if (app_plan.artifact.artifact_id, app_plan.artifact.source_commit) != (
            self.operation.request.artifact_id,
            self.operation.request.source_git_ref,
        ):
            raise ValueError("Candidate differs from the original admitted artifact/source pair.")
        new = TestingCopy(
            copy_id=allocation.namespace,
            target_id=allocation.compose_id,
            private_url=f"http://{allocation.namespace}:{self.intent.profile.runtime_port}",
            resource_binding=allocation.resources_binding_sha256,
            runtime_identity=app_plan.runtime_identity,
        )
        return TestingRebuildPlan(
            operation_id=self.operation.operation_id,
            product=self.operation.product,
            context=self.operation.context,
            base_url=self.base_url,
            route_id=testing_route_id(self.operation.product, self.operation.context),
            authority_digest=self.intent.digest,
            source=app_plan.source,
            source_reference=""
            if app_plan.source == "empty"
            else app_plan.native_source.binding_sha256
            if app_plan.native_source is not None
            else canonical_json_sha256(app_plan.upstream_source),
            old=self.intent.old,
            new=new,
        )

    def current_candidate_inputs(
        self, allocation: TestingCandidateAllocation
    ) -> TestingCandidateRuntimeInputs:
        try:
            return candidate_runtime_inputs(
                root=self.root,
                store=self.store,
                allocation=allocation,
                artifact_id=self.operation.request.artifact_id,
                source=rebuild_source(self.operation),
                admitted_profile=self.intent.profile,
            )
        except CandidateRuntimeInputsChanged as error:
            raise NativeTestingRebuildAdmissionChanged(
                "The admitted candidate runtime inputs are no longer valid."
            ) from error

    def run(self) -> TestingRebuildRecord:
        allocation = self.store.read_testing_candidate(self.adapter.namespace)
        historical: TestingRebuildRecord | None = None
        try:
            checkpoint = self.adapter.checkpoints.load_operation(
                product=self.operation.product,
                context=self.operation.context,
                operation_id=self.operation.operation_id,
            )
        except FileNotFoundError:
            pass
        else:
            if (
                checkpoint.plan.old != self.intent.old
                or checkpoint.plan.authority_digest != self.intent.digest
                or checkpoint.plan.new.copy_id != self.adapter.namespace
                or checkpoint.plan.base_url != self.base_url
                or checkpoint.plan.source != rebuild_source(self.operation)
            ):
                raise ValueError("The original checkpoint belongs to another rebuild intent.")
            self.plan = checkpoint.plan
            if allocation is not None and (
                checkpoint.plan.new.target_id != allocation.compose_id
                or allocation.application is not None
                and checkpoint.plan != self.build_plan(allocation, allocation.application.plan)
            ):
                raise ValueError("The original checkpoint belongs to another candidate plan.")
            if checkpoint.step == "failed" and checkpoint.admission_refusal:
                if allocation is None:
                    raise NativeTestingRebuildPending(
                        "The original candidate custody is unavailable."
                    )
                self.allocation = allocation
                return checkpoint
        if allocation is not None and allocation.application is not None:
            historical_plan = self.build_plan(allocation, allocation.application.plan)
            try:
                historical = self.adapter.checkpoints.load(historical_plan)
            except FileNotFoundError:
                pass
            else:
                if historical.plan != historical_plan:
                    raise ValueError("The original rebuild plan changed.")
                self.plan = historical_plan
                if historical.step in {"complete", "failed"}:
                    self.allocation, self.plan = allocation, historical_plan
                    return historical
        self.host, self.token = source.read_dokploy_config(
            control_plane_root=self.root, database_url=self.store.database_url
        )
        self.transport = DokployTestingRouteTransport(
            host=self.host,
            token=self.token,
            server_id=self.intent.server_id,
            old_project=self.intent.project,
            product=self.operation.product,
            context=self.operation.context,
            public_host=str(urlsplit(self.base_url).hostname),
            entrypoints=self.intent.public_entrypoints,
            old_runtime_identity=self.intent.old.runtime_identity,
        )
        existing_publication = read_publication(self.store, self.publication_key)
        if historical is not None and historical.step == "switching":
            dispatched = self.store.read_idempotency_record(
                scope=REBUILD_SCOPE,
                route_path=REBUILD_ROUTE + ":effect",
                idempotency_key=self.operation.operation_id
                + ":route:"
                + historical.plan.new.copy_id,
            )
            if (
                dispatched is not None
                and dispatched.provider_effect_started_at
                and not self.route_exclusion(historical.plan)
            ):
                # A failed candidate/guard must not end this operation while its
                # original proxy writer could still move traffic later. Observe
                # that dispatch before any preparation/readiness continuation.
                self.adapter.authorize("testing_rebuild_observe")
                if existing_publication is None:
                    raise NativeTestingRebuildPending("The original route receipt is unavailable.")
                self.route = existing_publication.plan.route
                try:
                    self.switch(
                        historical.plan,
                        historical.plan.old,
                        historical.plan.new,
                        reconcile_only=True,
                    )
                except TestingRebuildRouteFailure:
                    if not self.route_exclusion(historical.plan):
                        raise NativeTestingRebuildPending(
                            "The original route writer is not proven excluded."
                        ) from None
        if existing_publication is None:
            self.guard()
            if allocation is None:
                allocation = TestingCandidateAllocation(
                    namespace=self.adapter.namespace,
                    product=self.operation.product,
                    context=self.operation.context,
                    operation_id=self.operation.operation_id,
                    intent_sha256=candidate_application_intent(
                        product=self.operation.product,
                        context=self.operation.context,
                        artifact_id=self.operation.request.artifact_id,
                        source=rebuild_source(self.operation),
                    ),
                    serving_target_id=self.intent.old.target_id,
                    server_id=self.intent.server_id,
                    created_at=datetime.now(UTC),
                )
            if allocation.effect in {"project_id", "environment_id", "compose_id"} and not getattr(
                allocation, allocation.effect
            ):
                observed = observe_candidate_creation(
                    root=self.root,
                    store=self.store,
                    namespace=allocation.namespace,
                    guard=self.guard,
                )
                if observed is None:
                    raise NativeTestingRebuildPending(
                        "The original candidate allocation is still unknown."
                    )
                allocation = observed
            if allocation.application is None:
                allocation = prepare_candidate_storage(
                    root=self.root,
                    store=self.store,
                    allocation=allocation,
                    lease=self.lease,
                    guard=self.guard,
                )
            else:
                # Empty-storage qualification precedes application execution.
                # A resumed populated copy is proved by its original custody
                # and private readiness, never by another empty-volume gate.
                require_candidate_target_custody(self.store, allocation)
            self.inputs = self.current_candidate_inputs(allocation)
            app_plan = self.inputs.plan
        else:
            self.publication = existing_publication.plan
            allocation = self.store.read_testing_candidate(self.adapter.namespace)
            if allocation != self.publication.candidate:
                raise ValueError("The original publication candidate custody changed.")
            assert allocation is not None and allocation.application is not None
            app_plan = allocation.application.plan
            if existing_publication.state == "planned" and not self.cutover_started():
                self.guard()
                self.inputs = self.current_candidate_inputs(allocation)
        self.allocation = allocation
        self.plan = self.build_plan(allocation, app_plan)
        if self.publication is not None:
            self.route = self.publication.route
        else:
            frame = self.transport.read()
            self.route = compile_testing_route(
                product=self.operation.product,
                context=self.operation.context,
                host=self.plan.host,
                old_backend=self.plan.old.private_url,
                old_backend_addresses=tuple(frame["old_endpoints"]),
                new_backend=self.plan.new.private_url,
                raw=frame["raw"],
                public_entrypoints=self.intent.public_entrypoints,
                owned_document=json.loads(frame["owned_file"])
                if frame["owned_file"] is not None
                else None,
                recorded_template=self.intent.recorded_template,
            )
        return execute_testing_rebuild(self.plan, self.adapter.checkpoints, self)

    @contextmanager
    def exclusive(self, plan: TestingRebuildPlan) -> Iterator[None]:
        self.check_lock()
        yield

    def prepare(self, plan: TestingRebuildPlan) -> TestingCopyReceipt:
        assert self.inputs is not None
        try:
            self.allocation = prepare_candidate_application(
                root=self.root,
                store=self.store,
                allocation=self.allocation,
                inputs=self.inputs,
                lease=self.lease,
                guard=self.guard,
            )
        except CandidatePrivateReadinessFailed:
            self.failed_private_readiness(plan)
        except (ValueError, api.DokployDeploymentFailed, api.DokployScheduleExecutionFailed):
            current = self.store.read_testing_candidate(self.allocation.namespace)
            if current is not None and current.application is not None:
                if current.application.phase in {"configuring", "configured"}:
                    current = observe_candidate_configuration(
                        root=self.root,
                        store=self.store,
                        record=current,
                        lease=self.lease,
                        guard=self.guard,
                        retain_configured=True,
                    )
                app = current.application
                if (
                    app is not None
                    and app.phase == "abandoned"
                    and app.abandonment_evidence
                    in {app.unapplied_configuration_evidence, app.retained_configuration_evidence}
                ):
                    self.allocation = current
                    raise TestingRebuildPreparationFailure(
                        copy_id=plan.new.copy_id, evidence_id=app.abandonment_evidence
                    ) from None
            if (
                current is not None
                and current.application is not None
                and current.application.phase == "deploying"
            ):
                evidence = candidate_build_terminal(
                    host=self.host, token=self.token, record=current
                )
                if evidence.rsplit(":", 1)[-1] in api._DEPLOYMENT_FAILURE_STATUSES:
                    self.allocation = abandon_candidate_application(
                        root=self.root,
                        store=self.store,
                        namespace=current.namespace,
                        lease=self.lease,
                        guard=self.guard,
                    )
                    raise TestingRebuildPreparationFailure(
                        copy_id=plan.new.copy_id, evidence_id=evidence
                    ) from None
            if (
                current is not None
                and current.application is not None
                and current.application.phase == "restoring"
            ):
                evidence = candidate_restore_terminal(
                    host=self.host, token=self.token, record=current, require_success=False
                )
                if evidence.rsplit(":", 1)[-1] in api._DEPLOYMENT_FAILURE_STATUSES:
                    self.allocation = abandon_candidate_application(
                        root=self.root,
                        store=self.store,
                        namespace=current.namespace,
                        lease=self.lease,
                        guard=self.guard,
                    )
                    raise TestingRebuildPreparationFailure(
                        copy_id=plan.new.copy_id, evidence_id=evidence
                    ) from None
            raise
        app = self.allocation.application
        assert app is not None and app.prepared is not None
        prepared = app.prepared
        return TestingCopyReceipt(
            plan_digest=plan.digest,
            copy_id=plan.new.copy_id,
            evidence_id=prepared.source_witness,
            independent_resources_verified=True,
            local_credentials_verified=True,
            live_connections_removed_before_start=True,
        )

    def readiness(self, plan: TestingRebuildPlan) -> TestingCopyReadiness:
        assert self.inputs is not None and self.allocation.application is not None
        try:
            prepared = candidate_private_readiness(
                host=self.host,
                token=self.token,
                allocation=self.allocation,
                inputs=self.inputs,
                deployment_id=self.allocation.application.deployment_id,
            )
        except CandidatePrivateReadinessFailed:
            self.failed_private_readiness(plan)
        assert isinstance(self.lease, ProviderEvidenceLease)
        self.publication = self.publication or plan_testing_publication(
            self.store,
            candidate=self.allocation,
            before=self.intent.before,
            route=self.route,
            reservation=self.lease.evidence_reservation(),
            now=datetime.now(UTC),
            admitted_profile=self.intent.profile,
        )
        prepare_publication(
            self.store, plan=self.publication, reservation=self.lease.evidence_reservation()
        )
        return TestingCopyReadiness(
            copy_id=plan.new.copy_id,
            host=plan.host,
            observed_at=prepared.observed_at,
            runtime_identity=prepared.runtime_identity,
            healthy=True,
            evidence_id=prepared.source_witness,
        )

    def failed_private_readiness(self, plan: TestingRebuildPlan) -> NoReturn:
        from control_plane.dokploy import DEFAULT_DOKPLOY_DEPLOY_TIMEOUT_SECONDS

        assert self.inputs is not None
        if self.cutover_started():
            raise NativeTestingRebuildPending(
                "Observe the original route before candidate failure."
            )
        checkpoint = self.adapter.checkpoints.load(plan)
        now = datetime.now(UTC)
        if checkpoint.private_readiness_failed_at is None:
            self.adapter.checkpoints.save(
                checkpoint.model_copy(
                    update={
                        "private_readiness_failed_at": now,
                        "updated_at": now,
                    }
                )
            )
            raise NativeTestingRebuildPending(
                "The private candidate is within its readiness grace."
            )
        grace = (
            self.intent.before.target.deploy_timeout_seconds
            or DEFAULT_DOKPLOY_DEPLOY_TIMEOUT_SECONDS
        )
        if (now - checkpoint.private_readiness_failed_at).total_seconds() < grace:
            raise NativeTestingRebuildPending(
                "The private candidate is within its readiness grace."
            )
        self.guard(recovery=True)
        current = self.store.read_testing_candidate(self.allocation.namespace)
        if current is None or current != self.allocation:
            raise NativeTestingRebuildPending("The original private candidate custody changed.")
        evidence = candidate_terminal_custody_evidence(
            root=self.root, store=self.store, record=current
        )
        self.guard(recovery=True)
        raise TestingRebuildPreparationFailure(
            copy_id=plan.new.copy_id,
            evidence_id=evidence
            + ":private-readiness:"
            + checkpoint.private_readiness_failed_at.isoformat(),
            private_readiness=True,
        )

    def current_route(self, plan: TestingRebuildPlan) -> TestingRouteObservation:
        frame = self.transport.read()
        backend = observe_testing_route(
            self.route,
            frame["raw"],
            json.loads(frame["owned_file"]) if frame["owned_file"] is not None else None,
        )
        copy = plan.new if backend == plan.new.private_url else plan.old
        return TestingRouteObservation(
            route_id=plan.route_id,
            host=plan.host,
            target_url=backend,
            slot_id=copy.copy_id,
            applied=True,
            authoritative_router_count=1,
            observed_at=datetime.now(UTC),
            public_health=read_public_testing_health(
                self.base_url, self.intent.profile.health_path
            ),
        )

    def switch(
        self,
        plan: TestingRebuildPlan,
        previous: TestingCopy,
        desired: TestingCopy,
        *,
        reconcile_only: bool,
    ) -> None:
        phase = "route:" + desired.copy_id
        prior = self.store.read_idempotency_record(
            scope=REBUILD_SCOPE,
            route_path=REBUILD_ROUTE + ":effect",
            idempotency_key=self.operation.operation_id + ":" + phase,
        )
        original = self.store.read_original_mutation_intent(prior) if prior is not None else {}
        execution = (
            TestingRouteExecutionIntent.model_validate(original["route_execution"])
            if "route_execution" in original
            else self.transport.execution_intent(
                self.route, desired.private_url, operation_id=self.operation.operation_id
            )
            if prior is None
            else None
        )
        if execution is not None and (
            execution.operation_id,
            execution.server_id,
            execution.route_id,
            execution.route_digest,
            execution.backend,
        ) != (
            self.operation.operation_id,
            self.intent.server_id,
            self.route.route_id,
            self.route.digest,
            desired.private_url,
        ):
            raise ValueError("The original route execution intent changed.")

        def perform(dispatch: Callable[[Callable[[], None]], None]) -> None:
            if execution is None:
                raise NativeTestingRebuildPending("A legacy route writer remains observation-only.")
            raise NativeTestingRebuildPending("Route writes require their durable physical child.")

        self.run_effect(
            phase,
            lambda: self.route_backend() == desired.private_url,
            perform,
            reconcile_only=reconcile_only,
            recovery=desired == plan.old,
            route_execution=execution,
            route_publish=lambda attempt, dispatch: self.transport.publish(
                self.route,
                desired.private_url,
                expected_backend=previous.private_url,
                dispatch_guard=dispatch,
                desired_runtime_identity=desired.runtime_identity,
                execution=attempt.execution,
                execution_key=attempt.key,
                writer_attempt=attempt,
            ),
        )

    def route_backend(self) -> str:
        frame = self.transport.read()
        return observe_testing_route(
            self.route,
            frame["raw"],
            json.loads(frame["owned_file"]) if frame["owned_file"] is not None else None,
        )

    def publish(
        self, plan: TestingRebuildPlan, desired: TestingCopy, *, forward_started: bool
    ) -> None:
        assert self.publication is not None and isinstance(self.lease, ProviderEvidenceLease)
        self.guard(recovery=True)
        observation = self.current_route(plan) if desired == plan.new else None
        publish(
            self.store,
            plan=self.publication,
            reservation=self.lease.evidence_reservation(),
            now=datetime.now(UTC),
            observation=observation,
            inverse=desired == plan.old,
        )
        if desired == plan.old:
            self.restore_old_auto_deploy(plan)

    def restore_old_auto_deploy(self, plan: TestingRebuildPlan) -> None:
        original_disable = self.store.read_idempotency_record(
            scope=REBUILD_SCOPE,
            route_path=REBUILD_ROUTE + ":effect",
            idempotency_key=self.operation.operation_id + ":disable-old-auto-deploy",
        )
        if original_disable is None:
            return
        if (
            original_disable.provider_target_key
            != self.adapter.target_key() + ":disable-old-auto-deploy"
            or original_disable.request_fingerprint
            != canonical_json_sha256(
                {"intent": self.intent.digest, "phase": "disable-old-auto-deploy"}
            )
        ):
            raise ValueError("The original automatic-deploy effect has different custody.")
        if (
            original_disable.state == "completed"
            and original_disable.response_payload.get("not_dispatched") is True
        ):
            return
        if original_disable.state != "completed" or self.retirement_exclusion(plan) is None:
            raise NativeTestingRebuildPending(
                "Observe the original retirement before restoring deploys."
            )
        enabled = self.intent.old_auto_deploy
        if enabled is None:
            raise NativeTestingRebuildPending(
                "The original automatic-deploy setting is unrecorded."
            )

        def restore(dispatch: Callable[[Callable[[], None]], None]) -> None:
            def send() -> None:
                api.dokploy_request(
                    host=self.host,
                    token=self.token,
                    path="/api/compose.update",
                    method="POST",
                    payload={"composeId": self.intent.old.target_id, "autoDeploy": enabled},
                )

            dispatch(send)

        self.run_effect(
            "restore-old-auto-deploy",
            lambda: self.old_provider().get("autoDeploy") is enabled,
            restore,
            recovery=True,
        )

    def retire_old(self, plan: TestingRebuildPlan) -> None:
        def disable_auto_deploy(dispatch: Callable[[Callable[[], None]], None]) -> None:
            def send() -> None:
                api.dokploy_request(
                    host=self.host,
                    token=self.token,
                    path="/api/compose.update",
                    method="POST",
                    payload={"composeId": self.intent.old.target_id, "autoDeploy": False},
                )

            dispatch(send)

        self.run_effect(
            "disable-old-auto-deploy",
            lambda: self.old_provider().get("autoDeploy") is False,
            disable_auto_deploy,
            recovery=True,
        )

        def stop_compose() -> None:
            api.dokploy_request(
                host=self.host,
                token=self.token,
                path="/api/compose.stop",
                method="POST",
                payload={"composeId": self.intent.old.target_id},
            )

        self.run_effect(
            "retire-old",
            lambda: bool(
                retained_copy_operation(
                    host=self.host,
                    token=self.token,
                    server_id=self.intent.server_id,
                    payload=self.retained_payload("read"),
                )["stopped"]
            ),
            lambda dispatch: dispatch(stop_compose),
            recovery=True,
        )

    def retirement_exclusion(self, plan: TestingRebuildPlan) -> TestingCopyReadiness | None:
        stop = self.store.read_idempotency_record(
            scope=REBUILD_SCOPE,
            route_path=REBUILD_ROUTE + ":effect",
            idempotency_key=self.operation.operation_id + ":retire-old",
        )
        if (
            stop is None
            or stop.state != "completed"
            or stop.response_payload.get("not_dispatched") is not True
        ):
            return None
        if (
            stop.provider_effect_started_at
            or stop.provider_target_key != self.adapter.target_key() + ":retire-old"
            or stop.request_fingerprint
            != canonical_json_sha256({"intent": self.intent.digest, "phase": "retire-old"})
        ):
            raise ValueError("The original stop exclusion has different physical custody.")
        proof = retained_copy_operation(
            host=self.host,
            token=self.token,
            server_id=self.intent.server_id,
            payload={
                **self.retained_payload("readiness"),
                "health_path": self.intent.profile.health_path,
            },
        )
        if proof.get("healthy") is not True:
            raise NativeTestingRebuildPending(
                "The excluded retirement needs fresh retained-copy health."
            )
        return TestingCopyReadiness(
            copy_id=plan.old.copy_id,
            host=plan.host,
            observed_at=datetime.now(UTC),
            runtime_identity=plan.old.runtime_identity,
            healthy=True,
            evidence_id=stop.record_id + ":private-health:" + str(proof["web_id"]),
        )

    def unstarted_retirement(self, plan: TestingRebuildPlan) -> TestingCopyReadiness | None:
        # Exclude all outstanding retirement writes before releasing the lane's
        # original fence. Unknown dispatched effects are observed, never retried.
        for phase in ("disable-old-auto-deploy", "retire-old"):
            prior = self.store.read_idempotency_record(
                scope=REBUILD_SCOPE,
                route_path=REBUILD_ROUTE + ":effect",
                idempotency_key=self.operation.operation_id + ":" + phase,
            )
            if prior is not None and prior.provider_effect_started_at:
                if phase == "retire-old":
                    return None
                self.run_effect(
                    phase,
                    lambda: self.old_provider().get("autoDeploy") is False,
                    lambda dispatch: None,
                    reconcile_only=True,
                    recovery=True,
                )
            elif prior is None or prior.state != "completed":
                self.run_effect(
                    phase,
                    lambda: False,
                    lambda dispatch: None,
                    exclude_undispatched=True,
                    recovery=True,
                )
        return self.retirement_exclusion(plan)

    def run_effect(
        self,
        phase: str,
        present: Callable[[], bool],
        execute: Callable[[Callable[[Callable[[], None]], None]], object],
        *,
        reconcile_only: bool = False,
        exclude_undispatched: bool = False,
        recovery: bool = False,
        route_execution: TestingRouteExecutionIntent | None = None,
        route_publish: Callable[..., object] | None = None,
    ) -> DurableProviderOperationResult:
        parent = self
        forward_route = phase == "route:" + self.plan.new.copy_id
        inverse_route = phase == "route:" + self.plan.old.copy_id
        assert isinstance(self.lease, ProviderEvidenceLease)
        evidence_lease = self.lease
        route_effect = (
            TestingRouteEffects(
                controller=TestingRouteFenceController(
                    store=self.store,
                    transport=self.transport,
                    execution=route_execution,
                    guard=lambda: self.guard(recovery=recovery),
                    checkpoint=self.lease.checkpoint_effect,
                    lease_owner=lambda: evidence_lease.evidence_reservation().lease_owner,
                ),
                phase=phase,
                present=present,
                publish=route_publish,
                recovery_guard=lambda: self.guard(recovery=True),
                inverse=inverse_route,
            )
            if route_execution is not None and route_publish is not None
            else None
        )

        class Effect:
            def target_key(self) -> str:
                return parent.adapter.target_key() + ":" + phase

            def reconciliation_key(self) -> str:
                return canonical_json_sha256({"intent": parent.intent.digest, "phase": phase})

            def observe(
                self,
                provider_operation_key: str,
                provider_effect_phase: str,
                reconciliation_key: str,
            ) -> ProviderObservation:
                if exclude_undispatched:
                    return ProviderObservation("unknown" if provider_effect_phase else "absent")
                if route_effect is not None:
                    if prior is None:
                        return ProviderObservation("unknown")
                    return route_effect.observe(provider_effect_phase, prior.attempt)
                if (forward_route or inverse_route) and provider_effect_phase:
                    return ProviderObservation("unknown")
                if present():
                    return ProviderObservation("present", 200)
                return ProviderObservation("unknown" if provider_effect_phase else "absent")

            def apply(
                self, provider_operation_key: str, lease: ProviderOperationLease
            ) -> ProviderMutationOutcome:
                if exclude_undispatched:
                    parent.guard(recovery=recovery)
                    lease.assert_current()
                    return ProviderMutationOutcome(
                        200,
                        {"not_dispatched": True, "cause": "candidate_public_health_failed"},
                        provider_effect_performed=False,
                    )
                if route_effect is not None:
                    return route_effect.apply(lease)
                if present():
                    return ProviderMutationOutcome(200, provider_effect_performed=False)

                def dispatch(send: Callable[[], None]) -> None:
                    if phase in {"disable-old-auto-deploy", "retire-old"}:
                        target = parent.store.read_dokploy_target_record(
                            context_name=parent.operation.context,
                            instance_name="testing",
                        )
                        if target.policies.staff_testing_hold is not None:
                            raise NativeTestingRebuildPending(
                                "Staff testing holds retirement of the old copy."
                            )
                    parent.guard(recovery=recovery)
                    lease.assert_current()
                    parent.lease.checkpoint_effect(phase)
                    lease.checkpoint_effect(phase)
                    send()

                execute(dispatch)
                if not present():
                    raise NativeTestingRebuildPending("The original effect has not converged.")
                return ProviderMutationOutcome(200)

        prior = self.store.read_idempotency_record(
            scope=REBUILD_SCOPE,
            route_path=REBUILD_ROUTE + ":effect",
            idempotency_key=self.operation.operation_id + ":" + phase,
        )
        assert isinstance(self.lease, ProviderEvidenceLease)
        if exclude_undispatched and prior is not None and prior.provider_effect_started_at:
            raise NativeTestingRebuildPending("A dispatched retirement effect cannot be excluded.")
        result = run_durable_provider_operation(
            store=self.store,
            scope=REBUILD_SCOPE,
            route_path=REBUILD_ROUTE + ":effect",
            idempotency_key=self.operation.operation_id + ":" + phase,
            request_fingerprint=canonical_json_sha256(
                {"intent": self.intent.digest, "phase": phase}
            ),
            lease_owner=self.lease.evidence_reservation().lease_owner,
            response_trace_id=self.operation.operation_id,
            adapter=Effect(),
            original_intent={"route_execution": route_execution.model_dump(mode="json")}
            if route_execution is not None
            else None,
            # The child fence, including a missing reservation before any send,
            # proves whether this original effect may dispatch.
            allow_mutation=not (
                reconcile_only
                and prior is not None
                and prior.provider_effect_started_at
                and not inverse_route
                and not (
                    route_effect is not None
                    and (
                        prior.provider_effect_phase == ROUTE_PREPARING
                        or route_effect.writer_was_busy(prior.attempt)
                    )
                )
            ),
        )
        if result.status not in {"completed", "adopted", "replayed"}:
            raise NativeTestingRebuildPending("The original rebuild effect requires observation.")
        if forward_route and result.response_payload.get("route_write_not_started") is True:
            raise TestingRebuildRouteFailure("The original forward route writer was excluded.")
        if not exclude_undispatched and (
            result.response_payload.get("not_dispatched") is True or not present()
        ):
            raise NativeTestingRebuildPending(
                "The original effect was excluded or its outcome changed."
            )
        return result


def rebuild_result(
    record: TestingRebuildRecord, allocation: TestingCandidateAllocation
) -> OdooStableTargetReplacementApplyResult:
    app = allocation.application
    complete = record.step == "complete"
    return OdooStableTargetReplacementApplyResult(
        product=record.plan.product,
        context=record.plan.context,
        instance="testing",
        strategy="replace-and-cutover",
        deployment_record_id=record.plan.new.runtime_identity.deployment_record_id
        if complete
        else "",
        deploy_status="pass" if complete else "fail",
        post_deploy_status="pass" if complete else "fail",
        health_status="pass" if complete else "fail",
        health_url=record.plan.base_url,
        runtime_identity_injected=complete,
        target_id=record.plan.new.target_id if complete else record.plan.old.target_id,
        target_name=allocation.namespace if complete else "",
        artifact_id=record.plan.new.runtime_identity.artifact_id,
        image_reference=record.plan.new.runtime_identity.image_reference,
        runtime_source={
            "testing_rebuild": record.plan.operation_id,
            "testing_candidate": allocation.namespace,
            "copy_source": app.plan.source if app else record.plan.source,
        },
        error_code=record.error,
        error_message=record.admission_refusal
        or "The candidate failed; the original testing copy remains selected."
        if not complete
        else "",
    )


def read_public_testing_health(base_url: str, path: str) -> HealthcheckEvidence:
    """A redirect, timeout or missing identity never proves a healthy origin."""
    from urllib.error import HTTPError, URLError
    from urllib.request import HTTPRedirectHandler, Request, build_opener
    from control_plane.contracts.runtime_identity import runtime_identity_from_health_payload

    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(
            self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
        ) -> None:
            return None

    url = base_url.rstrip("/") + "/" + path.lstrip("/")
    try:
        with build_opener(NoRedirect()).open(Request(url, method="GET"), timeout=5) as response:
            if response.geturl() != url:
                return HealthcheckEvidence(status="pending", urls=(url,))
            payload = json.loads(response.read(262144).decode())
            identity = runtime_identity_from_health_payload(payload)
            return HealthcheckEvidence(
                verified=True,
                urls=(url,),
                timeout_seconds=5,
                status="pass",
                observed_runtime_identity=identity,
            )
    except HTTPError as error:
        error.close()
        return HealthcheckEvidence(verified=True, urls=(url,), timeout_seconds=5, status="fail")
    except (URLError, TimeoutError, OSError, ValueError):
        return HealthcheckEvidence(status="pending", urls=(url,))


def execute_native_testing_rebuild(
    *,
    root: Path,
    store: PostgresRecordStore,
    operation: OdooStableTargetReplacementOperationRecord,
    authorize: Callable[[str], None],
    lease_owner: str,
) -> OdooStableTargetReplacementApplyResult:
    try:
        return _execute_native_testing_rebuild(
            root=root,
            store=store,
            operation=operation,
            authorize=authorize,
            lease_owner=lease_owner,
        )
    except (
        CandidateObservationUnavailable,
        NativeSourceUnavailable,
        TestingRoutePending,
        TestingRebuildObservationUnavailable,
    ) as error:
        raise NativeTestingRebuildPending(
            "The original candidate needs another observation."
        ) from error
    except api.DokployRequestFailed as error:
        if error.status_code in {401, 403}:
            raise NativeSourceAuthorizationDenied(str(error)) from error
        if error.retryable or native_rebuild_effects_started(store, operation):
            raise NativeTestingRebuildPending(
                "The original provider outcome needs observation."
            ) from error
        raise
    except DurableOperationAuthorizationDeniedError:
        raise
    except TimeoutError:
        # The worker already preserves timeout uncertainty on the same operation.
        raise
    except NativeTestingRebuildPending:
        raise
    except Exception as error:
        if native_rebuild_effects_started(store, operation):
            raise NativeTestingRebuildPending(
                "The original rebuild effects remain fenced; continue their observation."
            ) from error
        raise


def _execute_native_testing_rebuild(
    *,
    root: Path,
    store: PostgresRecordStore,
    operation: OdooStableTargetReplacementOperationRecord,
    authorize: Callable[[str], None],
    lease_owner: str,
) -> OdooStableTargetReplacementApplyResult:
    authorize("testing_rebuild_admission")
    found = store.lookup_existing_mutation_reservation(
        route_path=REBUILD_ROUTE,
        idempotency_key=operation.operation_id,
        request_fingerprint=operation.request_fingerprint,
    )
    if found.status == "missing":
        intent = capture_rebuild_intent(root, store, operation)
    elif found.record is not None and found.status == "found":
        intent = NativeTestingRebuildIntent.model_validate(
            store.read_original_mutation_intent(found.record)
        )
    else:
        raise ValueError("The original testing rebuild reservation is unavailable or conflicts.")
    if intent.operation_id != operation.operation_id or intent.profile.product != operation.product:
        raise ValueError("The original testing rebuild intent belongs to another operation.")
    adapter = NativeTestingRebuildAdapter(root, store, operation, intent, authorize)
    result = run_durable_provider_operation(
        store=store,
        scope=REBUILD_SCOPE,
        route_path=REBUILD_ROUTE,
        idempotency_key=operation.operation_id,
        request_fingerprint=operation.request_fingerprint,
        lease_owner=lease_owner,
        response_trace_id=operation.operation_id,
        adapter=adapter,
        original_intent=intent.model_dump(mode="json"),
    )
    if result.status not in {"completed", "adopted", "replayed"}:
        raise NativeTestingRebuildPending("Continue observing the original testing rebuild.")
    return OdooStableTargetReplacementApplyResult.model_validate(result.response_payload["result"])
