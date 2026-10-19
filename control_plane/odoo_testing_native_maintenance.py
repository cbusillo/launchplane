"""Reclaim only qualified passing daily copies, preserving generic rebuild custody."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, exists, func, select
from sqlalchemy.orm import aliased

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    build_launchplane_mutation_reservation_id,
)
from control_plane.contracts.odoo_testing_drill import (
    NATIVE_DRILL_MAINTENANCE_ROUTE,
    NATIVE_DRILL_QUALIFICATION_ROUTE,
)
from control_plane.contracts.testing_candidate import TestingCandidateAllocation
from control_plane.contracts.testing_publication import TestingPublicationRecord
from control_plane.contracts.testing_rebuild import REBUILD_ROUTE, REBUILD_SCOPE
from control_plane.dokploy import api, compose, post_deploy, source
from control_plane.dokploy.testing_native_maintenance import native_maintenance_operation
from control_plane.dokploy.testing_route import observe_testing_route
from control_plane.odoo_testing_drill import DRILL_SCOPE, DrillPrerequisite, DrillSkipped
from control_plane.odoo_testing_drill_usage import DRILL_RECENT_USE_SECONDS
from control_plane.odoo_testing_restore import TESTING_COPY_COMPLETION_PARAMETER, generation_binding
from control_plane.provider_operations import (
    ProviderEvidenceLease,
    ProviderMutationOutcome,
    ProviderObservation,
    ProviderOperationLease,
    run_durable_provider_operation,
)
from control_plane.storage.postgres import (
    ORIGINAL_MUTATION_INTENT_ROUTE,
    LaunchplaneIdempotencyRow,
    PostgresRecordStore,
)
from control_plane.storage.testing_drill_custody import native_drill_maintenance_candidates
from control_plane.testing_candidate_application import candidate_configuration_sha256
from control_plane.testing_publication_storage import (
    read_publication,
)
from control_plane.testing_route_effects import ROUTE_WRITER_RECEIPT
from control_plane.testing_serving_copy import published_testing_copy
from control_plane._odoo_testing_storage_program import MAX_DISPOSALS_PER_PASS


def _publication(
    store: PostgresRecordStore, product: str, context: str, operation_id: str
) -> TestingPublicationRecord | None:
    return read_publication(
        store,
        canonical_json_sha256({"product": product, "context": context, "operation": operation_id}),
    )


def _qualification(store: PostgresRecordStore, namespace: str) -> dict[str, Any] | None:
    record = store.read_idempotency_record(
        scope=DRILL_SCOPE, route_path=NATIVE_DRILL_QUALIFICATION_ROUTE, idempotency_key=namespace
    )
    if record is None:
        return None
    spec = record.response_payload
    if record.state != "completed" or record.request_fingerprint != canonical_json_sha256(spec):
        raise DrillPrerequisite("native_testing_copy_recovery_or_retention_required")
    return spec


def qualifying_retirement(executor: Any) -> dict[str, Any] | None:
    """Capture the actual retirement runtime, including intervening ordinary deploys."""
    operation, store, intent = executor.operation, executor.store, executor.intent
    candidate = store.find_testing_candidate_by_target(intent.old.target_id)
    if candidate is None or candidate.application is None:
        return None
    try:
        creator = store.read_odoo_stable_target_replacement_operation_record(candidate.operation_id)
    except FileNotFoundError:
        # Historical/manual candidates without original drill lineage retain
        # the generic native storage-preserving stop behavior.
        return None
    if creator.idempotency_scope != DRILL_SCOPE:
        return None
    from control_plane.testing_rebuild_service import NativeTestingRebuildPending

    def protected() -> NativeTestingRebuildPending:
        return NativeTestingRebuildPending(
            "Original drill-copy history requires recovery or retention.",
            code="native_testing_copy_recovery_or_retention_required",
        )

    if creator.status != "pass" or not creator.is_native_testing_rebuild:
        raise protected()
    publication = _publication(store, operation.product, operation.context, creator.operation_id)
    generation = intent.before.generation
    if (
        publication is None
        or publication.state != "applied"
        or generation is None
        or publication.plan.after.generation is None
    ):
        raise protected()
    if publication.plan.candidate != candidate or generation_binding(
        generation
    ) != generation_binding(publication.plan.after.generation):
        raise protected()
    try:
        existing = _qualification(store, candidate.namespace)
    except DrillPrerequisite as error:
        from control_plane.testing_rebuild_service import NativeTestingRebuildPending

        raise NativeTestingRebuildPending(str(error), code=str(error)) from error
    if existing is not None:
        if existing["identity"]["retiring_operation_id"] != operation.operation_id:
            from control_plane.testing_rebuild_service import NativeTestingRebuildPending

            raise NativeTestingRebuildPending(
                "An original retirement already owns this physical copy.",
                code="native_testing_copy_original_retirement_owned",
            )
        return existing
    provider = executor.old_provider()
    runtime = intent.before.runtime
    if runtime is None or not runtime.env.get("ODOO_DB_NAME"):
        raise protected()
    identity = {
        "namespace": candidate.namespace,
        "target_id": candidate.compose_id,
        "server_id": candidate.server_id,
        "product": candidate.product,
        "context": candidate.context,
        "creator_operation_id": creator.operation_id,
        "creator_publication": publication.plan.key,
        "creator_publication_sha256": publication.plan.digest,
        "retiring_operation_id": operation.operation_id,
        "retiring_intent_sha256": intent.digest,
        "resources_binding": candidate.resources_binding_sha256,
        "physical_binding": intent.old.resource_binding,
        "copy_binding": generation_binding(generation),
        "configuration_sha256": candidate_configuration_sha256(provider),
        "runtime_identity_sha256": canonical_json_sha256(intent.retained["identity"]),
        "database_name": runtime.env["ODOO_DB_NAME"],
    }
    spec = {
        "retirement_protocol": 1,
        "identity": identity,
        "identity_sha256": canonical_json_sha256(identity),
        "generation": generation.model_dump(mode="json"),
        "compose_project": intent.project,
        "database_name": runtime.env["ODOO_DB_NAME"],
        "database_data_directory": compose.ODOO_DATABASE_DATA_DIRECTORY,
        "image_reference": intent.old.runtime_identity.image_reference,
        "runtime_identity": intent.retained["identity"],
        "storage_maintenance": True,
        "copy_witness_parameter": TESTING_COPY_COMPLETION_PARAMETER,
        "recent_use_seconds": DRILL_RECENT_USE_SECONDS,
    }
    stamp = datetime.now(UTC).isoformat()
    store.write_idempotency_record_once(
        LaunchplaneIdempotencyRecord(
            record_id=build_launchplane_mutation_reservation_id(
                scope=DRILL_SCOPE,
                route_path=NATIVE_DRILL_QUALIFICATION_ROUTE,
                idempotency_key=candidate.namespace,
            ),
            scope=DRILL_SCOPE,
            route_path=NATIVE_DRILL_QUALIFICATION_ROUTE,
            idempotency_key=candidate.namespace,
            request_fingerprint=canonical_json_sha256(spec),
            created_at=stamp,
            updated_at=stamp,
            recorded_at=stamp,
            response_status_code=200,
            response_trace_id=operation.operation_id,
            response_payload=spec,
        )
    )
    if _qualification(store, candidate.namespace) != spec:
        raise ValueError("Another original native retirement already owns this copy.")
    return spec


def retire_qualified_copy(executor: Any, spec: dict[str, Any]) -> None:
    """Settle the original stop independently of deletion qualification."""
    from control_plane.testing_rebuild_service import NativeTestingRebuildPending

    store, operation = executor.store, executor.operation
    phase = "retire-old"
    fingerprint = canonical_json_sha256({"intent": executor.intent.digest, "phase": phase})
    key = operation.operation_id + ":" + phase
    attempt_route = REBUILD_ROUTE + ":retirement-admission"
    last: dict[str, Any] = {}

    def pending(result: dict[str, Any]) -> NativeTestingRebuildPending:
        code = "native_testing_copy_retirement_" + str(
            result.get("reason", result.get("status", "unknown"))
        )
        return NativeTestingRebuildPending(code, code=code)

    def remote(
        mode: str,
        admission: dict[str, Any] | None = None,
        dispatch_guard: Callable[[Callable[[], None]], None] | None = None,
    ) -> dict[str, Any]:
        result = native_maintenance_operation(
            host=executor.host,
            token=executor.token,
            server_id=executor.intent.server_id,
            payload={**spec, **(admission or {}), "mode": mode},
            dispatch_guard=dispatch_guard,
        )
        last.clear()
        last.update(result)
        return result

    def stopped(result: dict[str, Any]) -> bool:
        return result.get("status") in {"eligible", "recent_use", "unqualified", "disposed"}

    class Retirement:
        def target_key(self) -> str:
            return str(executor.adapter.target_key()) + ":" + phase

        def reconciliation_key(self) -> str:
            return fingerprint

        def observe(
            self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
        ) -> ProviderObservation:
            result = remote("inspect-original")
            if stopped(result):
                return ProviderObservation("present", 200, {"retirement": "stopped"})
            current = store.read_idempotency_record(
                scope=REBUILD_SCOPE, route_path=REBUILD_ROUTE + ":effect", idempotency_key=key
            )
            admission = (
                store.read_idempotency_record(
                    scope=REBUILD_SCOPE,
                    route_path=attempt_route,
                    idempotency_key=key + ":" + str(current.attempt),
                )
                if current is not None
                else None
            )
            # An expired WAL-protocol admission, observed under the original host
            # locks, excludes delayed first dispatch. Receipt absence alone does not.
            excluded_missing = (
                result.get("status") == "missing"
                and spec.get("retirement_protocol") == 1
                and admission is not None
                and (
                    result.get("host_boot_id") != admission.response_payload["host_boot_id"]
                    or float(result["host_monotonic"])
                    >= float(admission.response_payload["host_expires_at"])
                )
            )
            if result.get("status") == "resumable_retirement" or excluded_missing:
                return ProviderObservation(
                    "absent", retry_safe=True, preserve_effect_checkpoint=True
                )
            return ProviderObservation("unknown" if provider_effect_phase else "absent")

        def apply(
            self, provider_operation_key: str, lease: ProviderOperationLease
        ) -> ProviderMutationOutcome:
            assert isinstance(lease, ProviderEvidenceLease)
            reservation = lease.evidence_reservation()
            admission_key = key + ":" + str(reservation.attempt)
            admission = store.read_idempotency_record(
                scope=REBUILD_SCOPE, route_path=attempt_route, idempotency_key=admission_key
            )
            if admission is None:
                clock = remote("inspect-original")
                if stopped(clock):
                    return ProviderMutationOutcome(
                        200, {"retirement": "stopped"}, provider_effect_performed=False
                    )
                if clock.get("status") not in {"missing", "resumable_retirement"}:
                    raise pending(clock)
                stamp = datetime.now(UTC)
                values = {
                    "expires_at": stamp.timestamp() + 120,
                    "host_boot_id": str(clock["host_boot_id"]),
                    "host_expires_at": float(clock["host_monotonic"]) + 120,
                }
                store.write_idempotency_record_once(
                    LaunchplaneIdempotencyRecord(
                        record_id=build_launchplane_mutation_reservation_id(
                            scope=REBUILD_SCOPE,
                            route_path=attempt_route,
                            idempotency_key=admission_key,
                        ),
                        scope=REBUILD_SCOPE,
                        route_path=attempt_route,
                        idempotency_key=admission_key,
                        request_fingerprint=str(spec["identity_sha256"]),
                        created_at=stamp.isoformat(),
                        updated_at=stamp.isoformat(),
                        recorded_at=stamp.isoformat(),
                        response_status_code=200,
                        response_trace_id=operation.operation_id,
                        response_payload=values,
                    )
                )
                admission = store.read_idempotency_record(
                    scope=REBUILD_SCOPE, route_path=attempt_route, idempotency_key=admission_key
                )
            assert admission is not None
            if admission.request_fingerprint != spec["identity_sha256"]:
                raise ValueError("Original retirement admission changed copy identity.")

            def dispatch(send: Callable[[], None]) -> None:
                executor.guard(recovery=True)
                target = store.read_dokploy_target_record(
                    context_name=operation.context, instance_name="testing"
                )
                if target.policies.staff_testing_hold is not None:
                    raise NativeTestingRebuildPending("staff_testing_hold")
                lease.assert_current()
                executor.lease.checkpoint_effect(phase)
                original = lease.evidence_reservation()
                if original.provider_effect_started_at:
                    if original.provider_effect_phase != phase:
                        raise pending({"status": "protected"})
                else:
                    lease.checkpoint_effect(phase)
                send()

            result = remote("qualify-retirement", admission.response_payload, dispatch)
            if not stopped(result):
                raise pending(result)
            return ProviderMutationOutcome(200, {"retirement": "stopped"})

    result = run_durable_provider_operation(
        store=store,
        scope=REBUILD_SCOPE,
        route_path=REBUILD_ROUTE + ":effect",
        idempotency_key=key,
        request_fingerprint=fingerprint,
        lease_owner=executor.lease.evidence_reservation().lease_owner,
        response_trace_id=operation.operation_id,
        adapter=Retirement(),
    )
    if result.status not in {"completed", "adopted", "replayed"}:
        raise pending(last)


def _settled_route_children(store: PostgresRecordStore, operation_id: str) -> bool:
    intent = aliased(LaunchplaneIdempotencyRow)
    child = aliased(LaunchplaneIdempotencyRow)
    original = intent.payload["response_payload"]["original_intent"]["route_writer"]
    with store._session_factory() as session:
        bad = session.scalar(
            select(
                exists(
                    select(child.record_id)
                    .join(
                        intent,
                        and_(
                            intent.scope == REBUILD_SCOPE,
                            intent.route_path == ORIGINAL_MUTATION_INTENT_ROUTE,
                            intent.idempotency_key == child.record_id,
                        ),
                    )
                    .where(
                        child.scope == REBUILD_SCOPE,
                        child.route_path == ROUTE_WRITER_RECEIPT,
                        original["execution"]["operation_id"].as_string() == operation_id,
                        ~and_(
                            child.state == "completed",
                            func.coalesce(
                                child.payload["response_payload"]["writer_outcome"].as_string(), ""
                            ).in_(("applied", "excluded", "busy", "superseded", "retired")),
                        ),
                    )
                )
            )
        )
    return not bad


def _require_completed_copy(
    store: PostgresRecordStore, candidate: TestingCandidateAllocation, spec: dict[str, Any]
) -> None:
    identity = spec["identity"]
    if (
        identity["namespace"],
        identity["target_id"],
        identity["resources_binding"],
        identity["creator_operation_id"],
    ) != (
        candidate.namespace,
        candidate.compose_id,
        candidate.resources_binding_sha256,
        candidate.operation_id,
    ):
        raise DrillPrerequisite("native_testing_copy_recovery_or_retention_required")
    for name in ("creator_operation_id", "retiring_operation_id"):
        try:
            operation = store.read_odoo_stable_target_replacement_operation_record(identity[name])
        except FileNotFoundError as error:
            raise DrillPrerequisite("native_testing_copy_original_execution_unsettled") from error
        publication = _publication(
            store, candidate.product, candidate.context, operation.operation_id
        )
        if (
            operation.status != "pass"
            or not operation.is_native_testing_rebuild
            or (name == "creator_operation_id" and operation.idempotency_scope != DRILL_SCOPE)
            or (operation.product, operation.context, operation.instance)
            != (candidate.product, candidate.context, "testing")
            or publication is None
            or publication.state != "applied"
            or not _settled_route_children(store, operation.operation_id)
        ):
            raise DrillPrerequisite("native_testing_copy_original_execution_unsettled")
        if name == "creator_operation_id":
            if (
                publication.plan.candidate != candidate
                or publication.plan.digest != identity["creator_publication_sha256"]
            ):
                raise DrillPrerequisite("native_testing_copy_recovery_or_retention_required")
        elif publication.plan.before.target_id.target_id != candidate.compose_id:
            raise DrillPrerequisite("native_testing_copy_recovery_or_retention_required")
        parent = store.read_idempotency_record(
            scope=REBUILD_SCOPE, route_path=REBUILD_ROUTE, idempotency_key=operation.operation_id
        )
        if (
            parent is None
            or parent.state != "completed"
            or parent.record_id != publication.plan.mutation_record_id
            or parent.provider_target_key != publication.plan.provider_target_key
            or parent.request_fingerprint != operation.request_fingerprint
        ):
            raise DrillPrerequisite("native_testing_copy_original_execution_unsettled")
        from control_plane.testing_rebuild_service import NativeTestingRebuildIntent

        intent = NativeTestingRebuildIntent.model_validate(
            store.read_original_mutation_intent(parent)
        )
        if (
            intent.operation_id != operation.operation_id
            or intent.profile.product != candidate.product
            or intent.old.target_id != publication.plan.before.target_id.target_id
            or intent.before.target_id != publication.plan.before.target_id
        ):
            raise DrillPrerequisite("native_testing_copy_original_execution_unsettled")
        phase = "route:" + publication.plan.candidate.namespace
        forward = store.read_idempotency_record(
            scope=REBUILD_SCOPE,
            route_path=REBUILD_ROUTE + ":effect",
            idempotency_key=operation.operation_id + ":" + phase,
        )
        if (
            forward is None
            or forward.state != "completed"
            or forward.response_payload.get("route_write_not_started") is True
            or forward.request_fingerprint
            != canonical_json_sha256({"intent": intent.digest, "phase": phase})
            or forward.provider_target_key != parent.provider_target_key + ":" + phase
        ):
            raise DrillPrerequisite("native_testing_copy_original_route_unsettled")
        if name == "retiring_operation_id" and intent.digest != identity["retiring_intent_sha256"]:
            raise DrillPrerequisite("native_testing_copy_recovery_or_retention_required")
    for phase in ("disable-old-auto-deploy", "retire-old"):
        record = store.read_idempotency_record(
            scope=REBUILD_SCOPE,
            route_path=REBUILD_ROUTE + ":effect",
            idempotency_key=identity["retiring_operation_id"] + ":" + phase,
        )
        if (
            record is None
            or record.state != "completed"
            or record.response_payload.get("not_dispatched") is True
            or record.request_fingerprint
            != canonical_json_sha256({"intent": identity["retiring_intent_sha256"], "phase": phase})
        ):
            raise DrillPrerequisite("native_testing_copy_retirement_unsettled")


def reconcile_native_drill_storage(executor: Any) -> None:
    """Bound cleanup before the next automatic allocation; unknown failures stay original."""
    operation, store = executor.operation, executor.store
    if operation.idempotency_scope != DRILL_SCOPE:
        return
    current = published_testing_copy(store, product=operation.product, context=operation.context)
    if current is None:
        raise DrillPrerequisite("native_testing_copy_publication_required")
    profile = store.read_product_profile_record(operation.product)
    lane = next(
        item
        for item in profile.lanes
        if (item.context, item.instance) == (operation.context, "testing")
    )
    budget = lane.odoo_testing_drill_storage
    if budget is None:
        raise DrillPrerequisite("testing_storage_budget_required")
    protected = (current.plan.after.target_id.target_id, current.plan.before.target_id.target_id)
    candidates = native_drill_maintenance_candidates(
        store,
        operation.product,
        operation.context,
        protected_targets=protected,
        retained_bindings=budget.retained_copy_bindings,
        retained_namespaces=budget.retained_candidate_namespaces,
        current_operation_id=operation.operation_id,
        limit=MAX_DISPOSALS_PER_PASS,
    )
    for candidate in candidates:
        spec = _qualification(store, candidate.namespace)
        if spec is None:
            raise DrillPrerequisite("native_testing_copy_recovery_or_retention_required")
        _reconcile_copy(executor, candidate, spec)
    if len(candidates) == MAX_DISPOSALS_PER_PASS and native_drill_maintenance_candidates(
        store,
        operation.product,
        operation.context,
        protected_targets=protected,
        retained_bindings=budget.retained_copy_bindings,
        retained_namespaces=budget.retained_candidate_namespaces,
        current_operation_id=operation.operation_id,
        limit=1,
    ):
        raise DrillPrerequisite("native_testing_copy_cleanup_pending")


def _reconcile_copy(
    executor: Any, candidate: TestingCandidateAllocation, spec: dict[str, Any]
) -> None:
    store, operation, identity = executor.store, executor.operation, spec["identity"]

    def guard() -> None:
        executor.guard()
        _require_completed_copy(store, candidate, spec)
        current = published_testing_copy(
            store, product=operation.product, context=operation.context
        )
        profile = store.read_product_profile_record(operation.product)
        lane = next(
            item
            for item in profile.lanes
            if (item.context, item.instance) == (operation.context, "testing")
        )
        budget = lane.odoo_testing_drill_storage
        if (
            current is None
            or budget is None
            or candidate.compose_id
            in {current.plan.after.target_id.target_id, current.plan.before.target_id.target_id}
            or candidate.namespace in budget.retained_candidate_namespaces
            or identity["copy_binding"] in budget.retained_copy_bindings
        ):
            raise DrillPrerequisite("native_testing_copy_protected")
        safety = store.read_disposable_lane_safety(operation.product, operation.context, "testing")
        if safety is not None and (
            safety.data == "only_copy"
            or safety.production_resources in {"shared", "separation_ready"}
        ):
            raise DrillPrerequisite("native_testing_copy_protected")
        if any(
            item.target_id == candidate.compose_id
            for item in store.list_dokploy_target_id_records()
        ):
            raise DrillPrerequisite("native_testing_copy_has_serving_alias")
        reservation = store.read_provider_target_record(
            context_name=candidate.namespace, instance_name="testing-candidate"
        )
        if (reservation.provider_id, reservation.target_category, reservation.target_id) != (
            "dokploy",
            "compose",
            candidate.compose_id,
        ):
            raise DrillPrerequisite("native_testing_copy_reservation_changed")
        provider = api.fetch_dokploy_target_payload(
            host=executor.host,
            token=executor.token,
            target_type="compose",
            target_id=candidate.compose_id,
        )
        if (
            provider.get("autoDeploy") is not False
            or provider.get("serverId") != candidate.server_id
            or provider.get("appName") != spec["compose_project"]
            or candidate_configuration_sha256(provider) != identity["configuration_sha256"]
        ):
            raise DrillPrerequisite("native_testing_copy_configuration_changed")
        from control_plane.dokploy_target_setup_http import (
            fetch_dokploy_compose_domains_for_target_setup,
        )

        if fetch_dokploy_compose_domains_for_target_setup(
            host=executor.host, token=executor.token, compose_id=candidate.compose_id
        ):
            raise DrillPrerequisite("native_testing_copy_has_public_domain")
        if not post_deploy.compose_data_workflow_is_quiescent(
            host=executor.host,
            token=executor.token,
            target_definition=source.DokployTargetDefinition(
                context=candidate.context,
                instance="testing",
                target_type="compose",
                target_id=candidate.compose_id,
            ),
        ):
            raise DrillPrerequisite("native_testing_copy_has_active_workflow")
        deployments = api.list_deployments_for_target(
            host=executor.host,
            token=executor.token,
            target_type="compose",
            target_id=candidate.compose_id,
            require_complete=True,
        )
        if any(
            api.deployment_status(item)
            not in api._DEPLOYMENT_SUCCESS_STATUSES | api._DEPLOYMENT_FAILURE_STATUSES
            for item in deployments
        ):
            raise DrillPrerequisite("native_testing_copy_has_active_deployment")
        frame = executor.transport.read()
        import json

        owned = json.loads(frame["owned_file"]) if frame.get("owned_file") is not None else None
        if (
            observe_testing_route(current.plan.route, frame["raw"], owned)
            != current.plan.route.new_backend
            or frame.get("execution_fence", {}).get("status") != "ready"
        ):
            raise DrillPrerequisite("native_testing_copy_route_execution_unverified")

    def remote(
        mode: str,
        dispatch_guard: Callable[[Callable[[], None]], None] | None = None,
        *,
        expires_at: float = 0,
        host_boot_id: str = "",
        host_expires_at: float = 0,
    ) -> dict[str, Any]:
        return native_maintenance_operation(
            host=executor.host,
            token=executor.token,
            server_id=candidate.server_id,
            payload={
                **spec,
                "mode": mode,
                "expires_at": expires_at,
                "host_boot_id": host_boot_id,
                "host_expires_at": host_expires_at,
            },
            dispatch_guard=dispatch_guard,
        )

    class Disposal:
        def target_key(self) -> str:
            return "native-testing-copy-disposal:" + candidate.namespace

        def reconciliation_key(self) -> str:
            return str(spec["identity_sha256"])

        def observe(
            self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
        ) -> ProviderObservation:
            result = remote("inspect-original")
            if (
                result.get("status") == "disposed"
                and result.get("identity_sha256") == spec["identity_sha256"]
            ):
                return ProviderObservation(
                    "present", 200, {"status": "disposed", "identity": identity}
                )
            current = store.read_idempotency_record(
                scope=DRILL_SCOPE,
                route_path=NATIVE_DRILL_MAINTENANCE_ROUTE,
                idempotency_key=candidate.namespace,
            )
            admission = (
                store.read_idempotency_record(
                    scope=DRILL_SCOPE,
                    route_path=NATIVE_DRILL_MAINTENANCE_ROUTE + ":attempt",
                    idempotency_key=candidate.namespace + ":" + str(current.attempt),
                )
                if current is not None
                else None
            )
            excluded = admission is not None and (
                result.get("host_boot_id") != admission.response_payload["host_boot_id"]
                or float(result["host_monotonic"])
                >= float(admission.response_payload["host_expires_at"])
            )
            if (
                provider_effect_phase
                and excluded
                and result.get("status") in {"missing", "eligible", "resumable"}
            ):
                return ProviderObservation(
                    "absent", retry_safe=True, preserve_effect_checkpoint=True
                )
            return ProviderObservation("unknown" if provider_effect_phase else "absent")

        def apply(
            self, provider_operation_key: str, lease: ProviderOperationLease
        ) -> ProviderMutationOutcome:
            def dispatch(send: Callable[[], None]) -> None:
                guard()
                lease.assert_current()
                executor.lease.assert_current()
                executor.lease.checkpoint_effect("native-copy-disposal")
                assert isinstance(lease, ProviderEvidenceLease)
                original = lease.evidence_reservation()
                if original.provider_effect_started_at:
                    if original.provider_effect_phase != "native-copy-disposal":
                        raise DrillPrerequisite(
                            "native_testing_copy_recovery_or_retention_required"
                        )
                else:
                    lease.checkpoint_effect("native-copy-disposal")
                from control_plane.storage.testing_native_disposal import dispatch_native_disposal

                assert isinstance(lease, ProviderEvidenceLease)
                binding = executor.intent.before.binding
                if binding is None:
                    raise DrillPrerequisite("native_testing_copy_publication_required")
                dispatch_native_disposal(
                    store,
                    operation,
                    identity,
                    str(binding.source.source_versions.get("testing_publication", "")),
                    lease.evidence_reservation(),
                    send,
                )

            assert isinstance(lease, ProviderEvidenceLease)
            reservation = lease.evidence_reservation()
            admission_key = candidate.namespace + ":" + str(reservation.attempt)
            admission = store.read_idempotency_record(
                scope=DRILL_SCOPE,
                route_path=NATIVE_DRILL_MAINTENANCE_ROUTE + ":attempt",
                idempotency_key=admission_key,
            )
            if admission is None:
                stamp = datetime.now(UTC)
                payload = {
                    "identity_sha256": spec["identity_sha256"],
                    "expires_at": stamp.timestamp() + 120,
                    "host_boot_id": str(inspection["host_boot_id"]),
                    "host_expires_at": float(inspection["host_monotonic"]) + 120,
                }
                store.write_idempotency_record_once(
                    LaunchplaneIdempotencyRecord(
                        record_id=build_launchplane_mutation_reservation_id(
                            scope=DRILL_SCOPE,
                            route_path=NATIVE_DRILL_MAINTENANCE_ROUTE + ":attempt",
                            idempotency_key=admission_key,
                        ),
                        scope=DRILL_SCOPE,
                        route_path=NATIVE_DRILL_MAINTENANCE_ROUTE + ":attempt",
                        idempotency_key=admission_key,
                        request_fingerprint=str(spec["identity_sha256"]),
                        created_at=stamp.isoformat(),
                        updated_at=stamp.isoformat(),
                        recorded_at=stamp.isoformat(),
                        response_status_code=200,
                        response_trace_id=operation.operation_id,
                        response_payload=payload,
                    )
                )
                admission = store.read_idempotency_record(
                    scope=DRILL_SCOPE,
                    route_path=NATIVE_DRILL_MAINTENANCE_ROUTE + ":attempt",
                    idempotency_key=admission_key,
                )
            assert admission is not None
            if admission.request_fingerprint != spec["identity_sha256"]:
                raise ValueError("Native maintenance attempt changed original copy identity.")
            result = remote(
                "dispose-original",
                dispatch,
                expires_at=float(admission.response_payload["expires_at"]),
                host_boot_id=str(admission.response_payload["host_boot_id"]),
                host_expires_at=float(admission.response_payload["host_expires_at"]),
            )
            if result.get("status") == "recent_use":
                raise DrillSkipped("native_testing_copy_recent_successful_signin")
            if (
                result.get("status") != "disposed"
                or result.get("identity_sha256") != spec["identity_sha256"]
            ):
                raise DrillPrerequisite(
                    "native_testing_copy_cleanup_" + str(result.get("status", "unknown"))
                )
            return ProviderMutationOutcome(200, {"status": "disposed", "identity": identity})

    guard()
    inspection = remote("inspect-original")
    if inspection.get("status") == "recent_use":
        raise DrillSkipped("native_testing_copy_recent_successful_signin")
    if inspection.get("status") in {"unknown", "incomplete", "active", "observation_unavailable"}:
        raise DrillPrerequisite("native_testing_copy_cleanup_pending")
    if inspection.get("status") in {
        "missing",
        "unqualified",
        "protected",
        "recovery_required",
        "resumable_retirement",
    }:
        raise DrillPrerequisite("native_testing_copy_recovery_or_retention_required")
    assert isinstance(executor.lease, ProviderEvidenceLease)
    result = run_durable_provider_operation(
        store=store,
        scope=DRILL_SCOPE,
        route_path=NATIVE_DRILL_MAINTENANCE_ROUTE,
        idempotency_key=candidate.namespace,
        request_fingerprint=str(spec["identity_sha256"]),
        lease_owner=executor.lease.evidence_reservation().lease_owner,
        response_trace_id=operation.operation_id,
        adapter=Disposal(),
        original_intent={"native_copy": identity},
    )
    if result.status not in {"completed", "adopted", "replayed"}:
        raise DrillPrerequisite("native_testing_copy_cleanup_pending")
