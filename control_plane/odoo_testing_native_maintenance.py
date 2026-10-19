"""Reclaim only qualified passing daily copies, preserving generic rebuild custody."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

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
from control_plane.storage.testing_drill_custody import (
    native_drill_maintenance_candidates,
    outstanding_native_retirements,
    native_retirement_resolved,
)
from control_plane.storage.testing_native_disposal import (
    native_admission_family,
    register_native_admission,
    dispatch_native_retirement,
    native_closure,
    close_native_admissions,
    native_custody_lost,
    note_native_custody_loss,
)
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
    prior = store.read_idempotency_record(
        scope=REBUILD_SCOPE,
        route_path=REBUILD_ROUTE + ":effect",
        idempotency_key=operation.operation_id + ":retire-old",
    )
    if prior is not None:
        original = store.read_original_mutation_intent(prior).get("native_retirement")
        if isinstance(original, dict):
            return original
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
        "retirement_protocol": 2,
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
    # Proposals belong to the original child effect. Namespace qualification is
    # published only after exact stopped completion, never after exclusion.
    return spec


def _publish_qualification(store: PostgresRecordStore, spec: dict[str, Any]) -> None:
    stamp = datetime.now(UTC).isoformat()
    store.write_idempotency_record_once(
        LaunchplaneIdempotencyRecord(
            record_id=build_launchplane_mutation_reservation_id(
                scope=DRILL_SCOPE,
                route_path=NATIVE_DRILL_QUALIFICATION_ROUTE,
                idempotency_key=spec["identity"]["namespace"],
            ),
            scope=DRILL_SCOPE,
            route_path=NATIVE_DRILL_QUALIFICATION_ROUTE,
            idempotency_key=spec["identity"]["namespace"],
            request_fingerprint=canonical_json_sha256(spec),
            created_at=stamp,
            updated_at=stamp,
            recorded_at=stamp,
            response_status_code=200,
            response_trace_id=spec["identity"]["retiring_operation_id"],
            response_payload=spec,
        )
    )
    if _qualification(store, spec["identity"]["namespace"]) != spec:
        raise DrillPrerequisite("native_testing_copy_recovery_or_retention_required")


def _family_excluded(family: list[dict[str, Any]], observation: dict[str, Any]) -> bool:
    # CP wall time is never host exclusion. Every admitted payload is checked;
    # insertion order and greatest attempt say nothing about greatest deadline.
    return all(
        observation.get("host_boot_id") != item["host_boot_id"]
        or float(observation["host_monotonic"]) >= float(item["host_expires_at"])
        for item in family
    )


def _receipt_namespaces(family: list[dict[str, Any]]) -> list[str]:
    return [str(item["namespace_sha256"]) for item in family]


def retire_qualified_copy(
    executor: Any, spec: dict[str, Any], *, exclude: bool = False, observation_only: bool = False
) -> Literal["stopped", "preserved"]:
    """Settle the original stop independently of deletion qualification."""
    from control_plane.testing_rebuild_service import NativeTestingRebuildPending

    store: PostgresRecordStore = executor.store
    operation = executor.operation
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
        current = store.read_idempotency_record(
            scope=REBUILD_SCOPE, route_path=REBUILD_ROUTE + ":effect", idempotency_key=key
        )
        if current is not None and native_custody_lost(store, current):
            raise pending({"reason": "native_copy_receipt_untrusted"})
        result = native_maintenance_operation(
            host=executor.host,
            token=executor.token,
            server_id=executor.intent.server_id,
            payload={**spec, **(admission or {}), "mode": mode},
            dispatch_guard=dispatch_guard,
        )
        if current is not None and result.get("reason") == "native_copy_receipt_untrusted":
            note_native_custody_loss(store, current, result)
            raise pending(result)
        last.clear()
        last.update(result)
        return result

    def stopped(result: dict[str, Any]) -> bool:
        return result.get("status") in {"eligible", "recent_use", "unqualified", "disposed"}

    def child() -> LaunchplaneIdempotencyRecord | None:
        return store.read_idempotency_record(
            scope=REBUILD_SCOPE, route_path=REBUILD_ROUTE + ":effect", idempotency_key=key
        )

    def inspect_family(
        current: LaunchplaneIdempotencyRecord,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        family = native_admission_family(store, current, attempt_route)
        anchors = _receipt_namespaces(family)
        closure = native_closure(store, current)
        if closure is not None:
            anchors.append(str(closure["proof"]["namespace_sha256"]))
        return family, remote("inspect-original", {"receipt_namespaces": anchors})

    def preserve(
        lease: ProviderOperationLease, family: list[dict[str, Any]], clock: dict[str, Any]
    ) -> dict[str, Any]:
        assert isinstance(lease, ProviderEvidenceLease)
        executor.lease.assert_current()
        lease.assert_current()
        current = lease.evidence_reservation()
        existing = next((item for item in family if item["attempt"] == current.attempt), None)
        if existing is None and not _family_excluded(family, clock):
            raise pending(clock)
        if not clock.get("namespace_sha256"):
            raise pending(clock)
        admission = register_native_admission(
            store,
            current,
            attempt_route,
            family,
            {
                "expires_at": datetime.now(UTC).timestamp() + 120,
                "host_boot_id": str(clock["host_boot_id"]),
                "host_expires_at": float(clock["host_monotonic"]) + 120,
                "namespace_sha256": clock["namespace_sha256"],
            },
        )
        family = native_admission_family(store, current, attempt_route)
        anchors = _receipt_namespaces(family)
        closure = native_closure(store, current)
        if closure is not None:
            anchors.append(str(closure["proof"]["namespace_sha256"]))

        def dispatch(send: Callable[[], None]) -> None:
            executor.lease.assert_current()
            lease.assert_current()
            executor.lease.checkpoint_effect(phase)
            if not lease.evidence_reservation().provider_effect_started_at:
                lease.checkpoint_effect(phase)
            dispatch_native_retirement(
                store, operation, lease.evidence_reservation(), attempt_route, send
            )

        proof = remote("seal-retirement", {**admission, "receipt_namespaces": anchors}, dispatch)
        if (
            proof.get("status") == "preserved"
            and proof.get("identity_sha256") == spec["identity_sha256"]
        ):
            close_native_admissions(
                store,
                lease.evidence_reservation(),
                attempt_route,
                family,
                proof,
                rollback=exclude,
                parent=executor.lease.evidence_reservation(),
            )
        return proof

    class Retirement:
        def target_key(self) -> str:
            return str(executor.adapter.target_key()) + ":" + phase

        def reconciliation_key(self) -> str:
            return fingerprint

        def observe(
            self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
        ) -> ProviderObservation:
            current = child()
            assert current is not None
            family, result = inspect_family(current)
            closure = native_closure(store, current)
            if closure is not None or result.get("status") == "preserved":
                if result.get("status") != "preserved":
                    return ProviderObservation("unknown")
                if closure is None or exclude:
                    executor.lease.assert_current()
                    close_native_admissions(
                        store,
                        current,
                        attempt_route,
                        family,
                        result,
                        rollback=exclude,
                        parent=executor.lease.evidence_reservation(),
                    )
                return ProviderObservation(
                    "present",
                    200,
                    {
                        "retirement": "preserved",
                        "reason": "native_testing_copy_recovery_or_retention_required",
                    },
                )
            if stopped(result):
                return ProviderObservation("present", 200, {"retirement": "stopped"})
            if (exclude or result.get("status") == "protected") and _family_excluded(
                family, result
            ):
                return ProviderObservation(
                    "absent", retry_safe=True, preserve_effect_checkpoint=True
                )
            if (
                spec.get("retirement_protocol") == 2
                and _family_excluded(family, result)
                and result.get("status") in {"missing", "resumable_retirement"}
            ):
                return ProviderObservation(
                    "absent", retry_safe=True, preserve_effect_checkpoint=True
                )
            return ProviderObservation("unknown" if provider_effect_phase or family else "absent")

        def apply(
            self, provider_operation_key: str, lease: ProviderOperationLease
        ) -> ProviderMutationOutcome:
            assert isinstance(lease, ProviderEvidenceLease)
            reservation = lease.evidence_reservation()
            family, clock = inspect_family(reservation)
            if stopped(clock):
                return ProviderMutationOutcome(
                    200, {"retirement": "stopped"}, provider_effect_performed=False
                )
            if exclude or clock.get("status") in {"protected", "preserved"}:
                proof = preserve(lease, family, clock)
                if proof.get("status") == "preserved":
                    return ProviderMutationOutcome(
                        200,
                        {
                            "retirement": "preserved",
                            "reason": "native_testing_copy_recovery_or_retention_required",
                        },
                        provider_effect_performed=False,
                    )
                raise pending(proof)
            if (
                clock.get("status") not in {"missing", "resumable_retirement"}
                or not _family_excluded(family, clock)
                or not clock.get("namespace_sha256")
            ):
                raise pending(clock)
            stamp = datetime.now(UTC)
            values = {
                "expires_at": stamp.timestamp() + 120,
                "host_boot_id": str(clock["host_boot_id"]),
                "host_expires_at": float(clock["host_monotonic"]) + 120,
                "namespace_sha256": clock["namespace_sha256"],
            }
            reservation = lease.evidence_reservation()
            admission = register_native_admission(store, reservation, attempt_route, family, values)

            def dispatch(send: Callable[[], None]) -> None:
                executor.guard(recovery=True)
                disabled = store.read_idempotency_record(
                    scope=REBUILD_SCOPE,
                    route_path=REBUILD_ROUTE + ":effect",
                    idempotency_key=operation.operation_id + ":disable-old-auto-deploy",
                )
                provider = executor.old_provider()
                if (
                    disabled is None
                    or disabled.state != "completed"
                    or disabled.response_payload.get("not_dispatched") is True
                    or disabled.request_fingerprint
                    != canonical_json_sha256(
                        {"intent": executor.intent.digest, "phase": "disable-old-auto-deploy"}
                    )
                    or provider.get("autoDeploy") is not False
                    or candidate_configuration_sha256(provider)
                    != spec["identity"]["configuration_sha256"]
                ):
                    raise pending({"reason": "native_copy_custody_changed"})
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
                dispatch_native_retirement(
                    store, operation, lease.evidence_reservation(), attempt_route, send
                )

            result = remote(
                "qualify-retirement",
                {**admission, "receipt_namespaces": [admission["namespace_sha256"]]},
                dispatch,
            )
            if not stopped(result):
                if result.get("status") == "protected":
                    current = lease.evidence_reservation()
                    family = native_admission_family(store, current, attempt_route)
                    proof = preserve(lease, family, result)
                    if proof.get("status") == "preserved":
                        return ProviderMutationOutcome(
                            200,
                            {
                                "retirement": "preserved",
                                "reason": "native_testing_copy_recovery_or_retention_required",
                            },
                        )
                raise pending(result)
            return ProviderMutationOutcome(200, {"retirement": "stopped"})

    prior = child()
    if prior is not None and native_closure(store, prior) is not None and exclude:
        family, proof = inspect_family(prior)
        executor.lease.assert_current()
        close_native_admissions(
            store,
            prior,
            attempt_route,
            family,
            proof,
            rollback=True,
            parent=executor.lease.evidence_reservation(),
        )
    result = run_durable_provider_operation(
        store=store,
        scope=REBUILD_SCOPE,
        route_path=REBUILD_ROUTE + ":effect",
        idempotency_key=key,
        request_fingerprint=fingerprint,
        lease_owner=executor.lease.evidence_reservation().lease_owner,
        response_trace_id=operation.operation_id,
        adapter=Retirement(),
        original_intent={"native_retirement": spec},
        allow_mutation=not observation_only,
    )
    if result.status not in {"completed", "adopted", "replayed"}:
        raise pending(last)
    disposition = str(result.response_payload.get("retirement", "unknown"))
    if disposition == "stopped":
        _publish_qualification(store, spec)
        return "stopped"
    if disposition == "preserved":
        return "preserved"
    raise pending(last)


def native_retirement_excluded(executor: Any, stop: LaunchplaneIdempotencyRecord) -> bool:
    """Only a sticky rollback request, sealed against every original payload, asks for inverse."""
    closure = native_closure(executor.store, stop)
    if closure is None or closure.get("rollback_requested") is not True:
        return False
    from control_plane.testing_rebuild_service import NativeTestingRebuildPending

    if native_custody_lost(executor.store, stop):
        return False
    spec = executor.store.read_original_mutation_intent(stop).get("native_retirement")
    if not isinstance(spec, dict):
        return False
    if stop.state != "completed":
        # A lost completion after the durable rollback closure must not turn
        # back into healthy forward progress. Adopt only its original seal.
        retire_qualified_copy(executor, spec, exclude=True, observation_only=True)
        refreshed = executor.store.read_idempotency_record(
            scope=stop.scope, route_path=stop.route_path, idempotency_key=stop.idempotency_key
        )
        if refreshed is None:
            return False
        stop = refreshed
    if stop.response_payload.get("retirement") != "preserved":
        return False
    family = native_admission_family(executor.store, stop, REBUILD_ROUTE + ":retirement-admission")
    proof = native_maintenance_operation(
        host=executor.host,
        token=executor.token,
        server_id=executor.intent.server_id,
        payload={
            **spec,
            "mode": "inspect-original",
            "receipt_namespaces": [
                *_receipt_namespaces(family),
                str(closure["proof"]["namespace_sha256"]),
            ],
        },
    )
    if proof.get("reason") == "native_copy_receipt_untrusted":
        note_native_custody_loss(executor.store, stop, proof)
    if (
        proof.get("status") != "preserved"
        or proof.get("no_stop_dispatched") is not True
        or proof.get("identity_sha256") != spec["identity_sha256"]
    ):
        raise NativeTestingRebuildPending("native_testing_copy_retirement_exclusion_unproven")
    return True


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
        if (
            phase == "retire-old"
            and spec.get("retirement_protocol") == 2
            and (
                record.response_payload.get("retirement") != "stopped"
                or store.read_original_mutation_intent(record).get("native_retirement") != spec
            )
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
    observe_outstanding_native_retirements(executor)
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
    recent_namespaces: list[str] = []
    for candidate in candidates:
        spec = _qualification(store, candidate.namespace)
        if spec is None:
            raise DrillPrerequisite("native_testing_copy_recovery_or_retention_required")
        try:
            _reconcile_copy(executor, candidate, spec)
        except DrillSkipped as error:
            if str(error) != "native_testing_copy_recent_successful_signin":
                raise
            recent_namespaces.append(candidate.namespace)
    if len(candidates) == MAX_DISPOSALS_PER_PASS and native_drill_maintenance_candidates(
        store,
        operation.product,
        operation.context,
        protected_targets=protected,
        retained_bindings=budget.retained_copy_bindings,
        retained_namespaces=(*budget.retained_candidate_namespaces, *recent_namespaces),
        current_operation_id=operation.operation_id,
        limit=1,
    ):
        raise DrillPrerequisite("native_testing_copy_cleanup_pending")


def observe_outstanding_native_retirements(executor: Any) -> None:
    """Reach original children before a passed producer's successor allocates."""
    store, operation = executor.store, executor.operation
    profile = store.read_product_profile_record(operation.product)
    lane = next(
        item
        for item in profile.lanes
        if (item.context, item.instance) == (operation.context, "testing")
    )
    budget = lane.odoo_testing_drill_storage
    if budget is None:
        raise DrillPrerequisite("testing_storage_budget_required")
    outstanding = outstanding_native_retirements(
        store,
        operation.product,
        operation.context,
        retained_bindings=budget.retained_copy_bindings,
        retained_namespaces=budget.retained_candidate_namespaces,
        limit=MAX_DISPOSALS_PER_PASS,
    )
    unresolved = False
    for child, spec in outstanding:
        if not observe_native_retirement_custody(executor, child, spec):
            unresolved = True
    if unresolved or len(outstanding) == MAX_DISPOSALS_PER_PASS:
        raise DrillPrerequisite("native_testing_copy_recovery_or_retention_required")


def observe_native_retirement_custody(
    executor: Any, child: LaunchplaneIdempotencyRecord, spec: dict[str, Any]
) -> bool:
    """Observe/adopt only the original stop; a passed producer is not a resume route."""
    store = executor.store
    if store.read_original_mutation_intent(child).get("native_retirement") != spec:
        raise DrillPrerequisite("native_testing_copy_retirement_unsettled")
    if native_custody_lost(store, child):
        return False
    if native_retirement_resolved(store, child):
        return True

    class OriginalObservation:
        def target_key(self) -> str:
            return str(child.provider_target_key)

        def reconciliation_key(self) -> str:
            return str(child.reconciliation_key)

        def observe(self, *_args: Any) -> ProviderObservation:
            current = store.read_idempotency_record(
                scope=child.scope,
                route_path=child.route_path,
                idempotency_key=child.idempotency_key,
            )
            if current is None or native_custody_lost(store, current):
                return ProviderObservation("unknown")
            anchors = _receipt_namespaces(
                native_admission_family(
                    store,
                    current,
                    REBUILD_ROUTE + ":retirement-admission",
                )
            )
            closure = native_closure(store, current)
            if closure is not None:
                anchors.append(str(closure["proof"]["namespace_sha256"]))
            result = native_maintenance_operation(
                host=executor.host,
                token=executor.token,
                server_id=spec["identity"]["server_id"],
                payload={**spec, "mode": "inspect-original", "receipt_namespaces": anchors},
            )
            if result.get("reason") == "native_copy_receipt_untrusted":
                note_native_custody_loss(store, current, result)
            if result.get("status") in {"eligible", "recent_use", "unqualified", "disposed"}:
                return ProviderObservation("present", 200, {"retirement": "stopped"})
            return ProviderObservation("unknown")

        def apply(self, *_args: Any) -> ProviderMutationOutcome:
            raise AssertionError("Original custody observation cannot dispatch")

    result = run_durable_provider_operation(
        store=store,
        scope=child.scope,
        route_path=child.route_path,
        idempotency_key=child.idempotency_key,
        request_fingerprint=child.request_fingerprint,
        lease_owner=executor.operation.lease_owner or "native-custody-observer",
        response_trace_id=spec["identity"]["retiring_operation_id"],
        adapter=OriginalObservation(),
        original_intent={"native_retirement": spec},
        allow_mutation=False,
    )
    if (
        result.status not in {"completed", "adopted", "replayed"}
        or result.response_payload.get("retirement") != "stopped"
    ):
        return False
    candidate = store.read_testing_candidate(spec["identity"]["namespace"])
    if candidate is None:
        return False
    # Original stopped proof plus the exact successful native producer/retirer
    # invariants permits the same qualification as immediate completion. No
    # new intent, provider effect or passed-parent resume route is invented.
    _require_completed_copy(store, candidate, spec)
    _publish_qualification(store, spec)
    return True


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
        receipt_namespaces: list[str] | None = None,
    ) -> dict[str, Any]:
        current = store.read_idempotency_record(
            scope=DRILL_SCOPE,
            route_path=NATIVE_DRILL_MAINTENANCE_ROUTE,
            idempotency_key=candidate.namespace,
        )
        original = store.read_idempotency_record(
            scope=REBUILD_SCOPE,
            route_path=REBUILD_ROUTE + ":effect",
            idempotency_key=identity["retiring_operation_id"] + ":retire-old",
        )
        if (
            original is None
            or store.read_original_mutation_intent(original).get("native_retirement") != spec
        ):
            raise DrillPrerequisite("native_testing_copy_retirement_unsettled")
        anchors = _receipt_namespaces(
            native_admission_family(store, original, REBUILD_ROUTE + ":retirement-admission")
        )
        closure = native_closure(store, original)
        if closure is not None:
            anchors.append(str(closure["proof"]["namespace_sha256"]))
        anchors.extend(receipt_namespaces or [])
        if any(
            native_custody_lost(store, record)
            for record in (current, original)
            if record is not None
        ):
            raise DrillPrerequisite("native_testing_copy_recovery_or_retention_required")
        result = native_maintenance_operation(
            host=executor.host,
            token=executor.token,
            server_id=candidate.server_id,
            payload={
                **spec,
                "mode": mode,
                "expires_at": expires_at,
                "host_boot_id": host_boot_id,
                "host_expires_at": host_expires_at,
                "receipt_namespaces": anchors,
            },
            dispatch_guard=dispatch_guard,
        )
        if result.get("reason") == "native_copy_receipt_untrusted":
            authority = current or original
            if authority is not None:
                note_native_custody_loss(store, authority, result)
        return result

    class Disposal:
        def target_key(self) -> str:
            return "native-testing-copy-disposal:" + candidate.namespace

        def reconciliation_key(self) -> str:
            return str(spec["identity_sha256"])

        def observe(
            self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
        ) -> ProviderObservation:
            current = store.read_idempotency_record(
                scope=DRILL_SCOPE,
                route_path=NATIVE_DRILL_MAINTENANCE_ROUTE,
                idempotency_key=candidate.namespace,
            )
            assert current is not None
            family = native_admission_family(
                store, current, NATIVE_DRILL_MAINTENANCE_ROUTE + ":attempt"
            )
            result = remote("inspect-original", receipt_namespaces=_receipt_namespaces(family))
            if (
                result.get("status") == "disposed"
                and result.get("identity_sha256") == spec["identity_sha256"]
            ):
                return ProviderObservation(
                    "present", 200, {"status": "disposed", "identity": identity}
                )
            excluded = _family_excluded(family, result)
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
            family = native_admission_family(
                store, reservation, NATIVE_DRILL_MAINTENANCE_ROUTE + ":attempt"
            )
            clock = remote("inspect-original", receipt_namespaces=_receipt_namespaces(family))
            if not _family_excluded(family, clock) or not clock.get("namespace_sha256"):
                raise DrillPrerequisite("native_testing_copy_cleanup_pending")
            stamp = datetime.now(UTC)
            reservation = lease.evidence_reservation()
            admission = register_native_admission(
                store,
                reservation,
                NATIVE_DRILL_MAINTENANCE_ROUTE + ":attempt",
                family,
                {
                    "identity_sha256": spec["identity_sha256"],
                    "expires_at": stamp.timestamp() + 120,
                    "host_boot_id": str(clock["host_boot_id"]),
                    "host_expires_at": float(clock["host_monotonic"]) + 120,
                    "namespace_sha256": clock["namespace_sha256"],
                },
            )
            result = remote(
                "dispose-original",
                dispatch,
                expires_at=float(admission["expires_at"]),
                host_boot_id=str(admission["host_boot_id"]),
                host_expires_at=float(admission["host_expires_at"]),
                receipt_namespaces=[str(admission["namespace_sha256"])],
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
        original_intent={"native_copy": identity, "admission_protocol": 2},
    )
    if result.status not in {"completed", "adopted", "replayed"}:
        raise DrillPrerequisite(
            "native_testing_copy_recovery_or_retention_required"
            if result.status == "conflict"
            else "native_testing_copy_cleanup_pending"
        )
