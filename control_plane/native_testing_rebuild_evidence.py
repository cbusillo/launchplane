from collections.abc import Callable
from datetime import datetime
from functools import partial
from typing import Any, Protocol, TypeVar, cast

from sqlalchemy.exc import SQLAlchemyError
from pydantic import ValidationError

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    build_launchplane_mutation_reservation_id,
)
from control_plane.contracts.native_testing_rebuild_evidence import (
    NativeEvidenceSource,
    NativeCandidateRead,
    NativeCandidateSelector,
    NativeRouteRead,
    NativeRouteSelector,
    NativeTestingExecutionBinding,
    NativeFailureRead,
    NativeTestingEvidenceItem,
    NativeTestingRebuildEvidence,
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.contracts.testing_candidate import (
    TestingCandidateAllocation,
    testing_candidate_namespace,
)
from control_plane.dokploy.testing_route_execution import TestingRouteExecutionIntent
from control_plane.dokploy.testing_route import TestingLaneRoute
from control_plane.dokploy.native_copy_stream import require_registered_remote_server
from control_plane.contracts.testing_publication import TestingPublicationRecord
from control_plane.contracts.testing_rebuild import (
    REBUILD_SCOPE,
    REBUILD_ROUTE,
    TestingRebuildRecord,
    TestingRouteObservation,
    UnavailableTestingPredecessor,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_publication_storage import read_publication
from control_plane.testing_candidate_runtime import candidate_application_intent
from control_plane.testing_rebuild_storage import RecordTestingRebuildStore, RebuildRecordStore
from control_plane.traffic_switch import require_route_authority
from control_plane.copy_readback_diagnostics import copy_failure_summary

T = TypeVar("T")


class NativeTestingEvidenceStore(Protocol):
    def read_idempotency_record(
        self, *, scope: str, route_path: str, idempotency_key: str
    ) -> LaunchplaneIdempotencyRecord | None: ...
    def read_original_mutation_intent(
        self, reservation: LaunchplaneIdempotencyRecord
    ) -> dict[str, Any]: ...
    def read_testing_candidate(self, namespace: str) -> TestingCandidateAllocation | None: ...
    def read_deployment_record(self, record_id: str) -> DeploymentRecord: ...


def _read(
    source: NativeEvidenceSource, read: Callable[[], T | None]
) -> tuple[T | None, NativeTestingEvidenceItem]:
    try:
        value = read()
    except FileNotFoundError:
        return None, NativeTestingEvidenceItem(
            source=source, availability="missing", coverage="retained_record_only"
        )
    except (OSError, SQLAlchemyError, ValidationError):
        return None, NativeTestingEvidenceItem(
            source=source, availability="unavailable", coverage="retained_record_only"
        )
    except (ValueError, TypeError, KeyError):
        return None, _unbound(source)
    return value, NativeTestingEvidenceItem(
        source=source,
        availability="present" if value is not None else "missing",
        coverage="retained_record_only",
    )


def _unbound(source: NativeEvidenceSource) -> NativeTestingEvidenceItem:
    return NativeTestingEvidenceItem(
        source=source, availability="unbound", binding="mismatched", coverage="retained_record_only"
    )


def _bound_reservation(
    record: LaunchplaneIdempotencyRecord, *, scope: str, route: str, key: str, fingerprint: str
) -> bool:
    return (
        record.scope,
        record.route_path,
        record.idempotency_key,
        record.record_id,
        record.request_fingerprint,
    ) == (
        scope,
        route,
        key,
        build_launchplane_mutation_reservation_id(
            scope=scope, route_path=route, idempotency_key=key
        ),
        fingerprint,
    )


def _reservation_item(
    record: LaunchplaneIdempotencyRecord, source: NativeEvidenceSource
) -> NativeTestingEvidenceItem:
    return NativeTestingEvidenceItem(
        source=source,
        availability="present",
        binding="matched",
        coverage="retained_record_only",
        state=record.state,
        attempt=record.attempt,
        observed_at=datetime.fromisoformat(record.updated_at) if record.updated_at else None,
    )


def _checkpoint_matches(
    checkpoint: TestingRebuildRecord,
    operation: OdooStableTargetReplacementOperationRecord,
    reservation: LaunchplaneIdempotencyRecord,
    intent: dict[str, Any],
) -> bool:
    from control_plane.testing_rebuild_service import rebuild_source

    plan = checkpoint.plan
    identity = plan.new.runtime_identity
    try:
        source = rebuild_source(operation)
    except ValueError:
        return False
    return (
        intent.get("operation_id") == operation.operation_id
        and canonical_json_sha256(intent) == reservation.reconciliation_key == plan.authority_digest
        and intent.get("old") == plan.old.model_dump(mode="json")
        and (plan.product, plan.context, plan.instance, plan.operation_id)
        == (operation.product, operation.context, operation.instance, operation.operation_id)
        and plan.new.copy_id
        == testing_candidate_namespace(plan.product, plan.context, plan.operation_id)
        and (identity.artifact_id, identity.source_git_ref)
        == (operation.request.artifact_id, operation.request.source_git_ref)
        and plan.source == source
    )


def _terminal_probe(
    checkpoint: TestingRebuildRecord,
    operation: OdooStableTargetReplacementOperationRecord,
    reservation: LaunchplaneIdempotencyRecord,
) -> NativeTestingEvidenceItem:
    probe = checkpoint.candidate_probe
    if probe is not None and (
        probe.observed_at.utcoffset() is None
        or not checkpoint.began_at <= probe.observed_at <= checkpoint.updated_at
        or probe.operation_attempt is None
        or probe.operation_attempt > operation.attempt
        or probe.reservation_attempt is None
        or probe.reservation_attempt > reservation.attempt
    ):
        return _unbound("checkpoint_probe")
    if probe is not None:
        return NativeTestingEvidenceItem(
            source="checkpoint_probe",
            availability="present" if probe.state is not None else "null",
            binding="matched",
            coverage="checkpoint_probe_only",
            state=probe.state,
            observed_at=probe.observed_at,
            attempt=probe.reservation_attempt,
            http_status=probe.http_status,
            failure_kind=probe.failure_kind,
            verified=probe.verified,
            runtime_identity_matches=probe.runtime_identity_matches,
        )
    route = checkpoint.route
    if route is None:
        return NativeTestingEvidenceItem(
            source="checkpoint_probe", availability="null", binding="matched"
        )
    if isinstance(route, TestingRouteObservation):
        copy = (
            checkpoint.plan.new
            if route.slot_id == checkpoint.plan.new.copy_id
            else checkpoint.plan.old
        )
        if copy.private_url is None:
            return _unbound("checkpoint_probe")
        try:
            require_route_authority(
                route,
                route_id=checkpoint.plan.route_id,
                host=checkpoint.plan.host,
                target_url=copy.private_url,
                slot_id=copy.copy_id,
            )
        except ValueError:
            return _unbound("checkpoint_probe")
    if (
        not isinstance(route, TestingRouteObservation)
        or route.slot_id != checkpoint.plan.new.copy_id
    ):
        return NativeTestingEvidenceItem(
            source="checkpoint_probe",
            availability="not_retained",
            binding="matched",
            coverage="candidate_probe_not_retained",
        )
    health = route.public_health
    if health is None:
        return NativeTestingEvidenceItem(
            source="checkpoint_probe", availability="null", binding="matched"
        )
    return NativeTestingEvidenceItem(
        source="checkpoint_probe",
        availability="present",
        binding="matched",
        coverage="checkpoint_probe_only",
        state=health.status,
        observed_at=route.observed_at,
        verified=health.verified,
        runtime_identity_matches=health.observed_runtime_identity
        == checkpoint.plan.new.runtime_identity
        if health.observed_runtime_identity is not None
        else None,
    )


def _route_receipt(
    store: NativeTestingEvidenceStore,
    operation: OdooStableTargetReplacementOperationRecord,
    reservation: LaunchplaneIdempotencyRecord,
    copy_id: str,
    source: NativeEvidenceSource,
) -> tuple[LaunchplaneIdempotencyRecord | None, NativeTestingEvidenceItem]:
    phase = "route:" + copy_id
    key = operation.operation_id + ":" + phase
    fingerprint = canonical_json_sha256({"intent": reservation.reconciliation_key, "phase": phase})
    record, evidence = _read(
        source,
        lambda: store.read_idempotency_record(
            scope=REBUILD_SCOPE, route_path=REBUILD_ROUTE + ":effect", idempotency_key=key
        ),
    )
    if record is None:
        return None, evidence
    if not _bound_reservation(
        record,
        scope=REBUILD_SCOPE,
        route=REBUILD_ROUTE + ":effect",
        key=key,
        fingerprint=fingerprint,
    ) or (
        record.reconciliation_key != fingerprint
        or record.provider_target_key != reservation.provider_target_key + ":" + phase
    ):
        return None, _unbound(source)
    markers = {
        name: record.response_payload.get(name)
        for name in ("not_dispatched", "route_write_not_started")
    }
    if any(value is not None and type(value) is not bool for value in markers.values()):
        return None, NativeTestingEvidenceItem(
            source=source,
            availability="unavailable",
            binding="matched",
            coverage="retained_record_only",
        )
    return record, _reservation_item(record, source).model_copy(
        update={
            "effect_checkpoint_recorded": bool(record.provider_effect_started_at),
            **markers,
        }
    )


def _selection(
    store: NativeTestingEvidenceStore,
    operation: OdooStableTargetReplacementOperationRecord,
    reservation: LaunchplaneIdempotencyRecord,
    checkpoint: TestingRebuildRecord,
) -> tuple[TestingPublicationRecord | None, NativeTestingEvidenceItem]:
    key = canonical_json_sha256(
        {
            "product": operation.product,
            "context": operation.context,
            "operation": operation.operation_id,
        }
    )
    record, evidence = _read(
        "publication_receipt", lambda: read_publication(cast(PostgresRecordStore, store), key)
    )
    if record is None:
        return None, evidence
    assert isinstance(record, TestingPublicationRecord)
    plan = record.plan
    candidate = plan.candidate
    if (
        candidate.product,
        candidate.context,
        candidate.operation_id,
        candidate.namespace,
        candidate.compose_id,
        candidate.intent_sha256,
        plan.mutation_record_id,
        plan.provider_target_key,
    ) != (
        operation.product,
        operation.context,
        operation.operation_id,
        checkpoint.plan.new.copy_id,
        checkpoint.plan.new.target_id,
        candidate_application_intent(
            product=operation.product,
            context=operation.context,
            artifact_id=operation.request.artifact_id,
            source=checkpoint.plan.source,
            migrated_source=operation.request.migrated_source,
        ),
        reservation.record_id,
        reservation.provider_target_key,
    ) or plan.deployment.record_id != checkpoint.plan.new.runtime_identity.deployment_record_id:
        return None, _unbound("publication_receipt")
    return record, NativeTestingEvidenceItem(
        source="publication_receipt",
        availability="present",
        binding="matched",
        coverage="retained_record_only",
        state=record.state,
        observed_at=record.updated_at,
    )


def _deployment(
    store: NativeTestingEvidenceStore, checkpoint: TestingRebuildRecord
) -> NativeTestingEvidenceItem:
    if not callable(getattr(store, "read_deployment_record", None)):
        return NativeTestingEvidenceItem(source="deployment_record")
    identity = checkpoint.plan.new.runtime_identity
    record, evidence = _read(
        "deployment_record", lambda: store.read_deployment_record(identity.deployment_record_id)
    )
    if record is None:
        return evidence
    if record.record_id != identity.deployment_record_id or record.runtime_identity != identity:
        return _unbound("deployment_record")
    return NativeTestingEvidenceItem(
        source="deployment_record",
        availability="present",
        binding="matched",
        coverage="retained_record_only",
    )


def _private_status(evidence: NativeTestingEvidenceItem) -> dict[str, Any]:
    return evidence.model_dump(
        include={"availability", "binding", "coverage", "observed_at", "attempt"}
    )


def _failure_read(checkpoint: TestingRebuildRecord) -> NativeFailureRead:
    markers = dict(
        part.split("=", 1)
        for part in copy_failure_summary(checkpoint.preparation_failure_evidence).split()
    )
    if not markers:
        return NativeFailureRead(availability="not_retained", coverage="retained_record_only")
    return NativeFailureRead.model_validate(
        {
            "availability": "present",
            "binding": "matched",
            "coverage": "retained_record_only",
            "evidence_id": markers.get("copy_failure_evidence_id"),
            "stage": markers.get("copy_failure_stage"),
            "kind": markers.get("copy_failure_kind"),
            "exit_status": markers.get("copy_failure_exit_status"),
        }
    )


def _readiness(checkpoint: TestingRebuildRecord) -> NativeTestingEvidenceItem:
    source: NativeEvidenceSource = "checkpoint_private_readiness"
    if checkpoint.error == "candidate_private_readiness_failed":
        return NativeTestingEvidenceItem(
            source=source,
            availability="present",
            binding="matched",
            coverage="retained_record_only",
            state="fail",
            observed_at=checkpoint.private_readiness_failed_at,
        )
    readiness = checkpoint.readiness
    if readiness is not None and (readiness.copy_id, readiness.host) != (
        checkpoint.plan.new.copy_id,
        checkpoint.plan.host,
    ):
        return _unbound(source)
    return NativeTestingEvidenceItem(
        source=source,
        availability="present" if readiness else "null",
        binding="matched",
        coverage="retained_record_only",
        state="pass" if readiness and readiness.healthy else None,
        observed_at=readiness.observed_at if readiness else None,
        runtime_identity_matches=readiness.runtime_identity == checkpoint.plan.new.runtime_identity
        if readiness
        else None,
    )


def _candidate_read(
    store: NativeTestingEvidenceStore,
    operation: OdooStableTargetReplacementOperationRecord,
    checkpoint: TestingRebuildRecord,
    intent: dict[str, Any],
) -> NativeCandidateRead:
    if not callable(getattr(store, "read_testing_candidate", None)):
        return NativeCandidateRead()
    candidate, evidence = _read(
        "checkpoint_preparation", lambda: store.read_testing_candidate(checkpoint.plan.new.copy_id)
    )
    if candidate is None:
        return NativeCandidateRead(**_private_status(evidence))
    plan = checkpoint.plan
    if (
        candidate.product,
        candidate.context,
        candidate.operation_id,
        candidate.namespace,
        candidate.intent_sha256,
        candidate.compose_id,
        candidate.resources_binding_sha256,
        candidate.server_id,
        candidate.serving_target_id,
    ) != (
        operation.product,
        operation.context,
        operation.operation_id,
        plan.new.copy_id,
        candidate_application_intent(
            product=operation.product,
            context=operation.context,
            artifact_id=operation.request.artifact_id,
            source=plan.source,
            migrated_source=operation.request.migrated_source,
        ),
        plan.new.target_id,
        plan.new.resource_binding,
        intent.get("server_id"),
        plan.old.target_id,
    ):
        return NativeCandidateRead(
            availability="unbound", binding="mismatched", coverage="retained_record_only"
        )
    application = candidate.application
    if application is None:
        return NativeCandidateRead(
            availability="present",
            coverage="retained_record_only",
            observed_at=candidate.created_at,
        )
    if (
        application.plan.runtime_identity != plan.new.runtime_identity
        or application.plan.base_url != plan.base_url
        or application.plan.source != plan.source
        or (
            application.prepared is not None
            and application.prepared.private_endpoint != plan.new.private_url
        )
    ):
        return NativeCandidateRead(
            availability="unbound", binding="mismatched", coverage="retained_record_only"
        )
    try:
        selector = NativeCandidateSelector(
            namespace=candidate.namespace,
            server_id=require_registered_remote_server(candidate.server_id),
            project_id=candidate.project_id,
            environment_id=candidate.environment_id,
            compose_id=candidate.compose_id,
            resources_binding_sha256=candidate.resources_binding_sha256,
            runtime_identity=application.plan.runtime_identity,
        )
    except ValueError:
        return NativeCandidateRead(availability="unavailable", coverage="retained_record_only")
    return NativeCandidateRead(
        availability="present",
        binding="matched",
        coverage="retained_record_only",
        observed_at=candidate.created_at,
        selector=selector,
    )


def _route_read(
    store: NativeTestingEvidenceStore,
    receipt: LaunchplaneIdempotencyRecord | None,
    evidence: NativeTestingEvidenceItem,
    checkpoint: TestingRebuildRecord,
    intent: dict[str, Any],
    *,
    inverse: bool,
    publication: TestingPublicationRecord | None,
) -> NativeRouteRead:
    if receipt is None:
        return NativeRouteRead(**_private_status(evidence))
    read_intent: Callable[[], dict[str, Any]] = partial(
        store.read_original_mutation_intent, receipt
    )
    original, retained = _read(evidence.source, read_intent)
    if original is None:
        return NativeRouteRead(**_private_status(retained))
    if "route_execution" not in original:
        return NativeRouteRead(availability="not_retained", coverage="retained_record_only")
    try:
        execution = TestingRouteExecutionIntent.model_validate(original["route_execution"])
        require_registered_remote_server(execution.server_id)
    except ValueError:
        return NativeRouteRead(availability="unavailable", coverage="retained_record_only")
    plan = checkpoint.plan
    desired = plan.old if inverse else plan.new
    if (execution.operation_id, execution.server_id, execution.route_id, execution.backend) != (
        plan.operation_id,
        intent.get("server_id"),
        plan.route_id,
        desired.private_url,
    ):
        return NativeRouteRead(
            availability="unbound", binding="mismatched", coverage="retained_record_only"
        )
    route = checkpoint.route
    observed = (
        route
        if isinstance(route, TestingRouteObservation) and route.slot_id == desired.copy_id
        else None
    )
    if observed is not None and desired.private_url is not None:
        try:
            require_route_authority(
                observed,
                route_id=plan.route_id,
                host=plan.host,
                target_url=desired.private_url,
                slot_id=desired.copy_id,
            )
        except ValueError:
            observed = None
    else:
        observed = None
    try:
        selector = NativeRouteSelector(
            server_id=execution.server_id,
            copy_id=desired.copy_id,
            route_id=execution.route_id,
            route_digest=execution.route_digest,
            document_sha256=execution.document_sha256,
            backend=execution.backend,
        )
    except ValueError:
        return NativeRouteRead(availability="unavailable", coverage="retained_record_only")
    expected_route = publication.plan.route if publication else intent.get("unavailable_route")
    digest_matches = None
    if expected_route is not None:
        try:
            expected = TestingLaneRoute.model_validate(expected_route)
        except ValueError:
            return NativeRouteRead(availability="unavailable", coverage="retained_record_only")
        digest_matches = (
            execution.route_digest == expected.digest
            and (
                expected.product,
                expected.context,
                expected.host,
                expected.route_id,
                expected.new_backend,
            )
            == (plan.product, plan.context, plan.host, plan.route_id, plan.new.private_url)
            and expected.old_backend == plan.old.private_url
        )
        if not digest_matches:
            return NativeRouteRead(
                availability="unbound",
                binding="mismatched",
                coverage="retained_record_only",
                route_digest_matches=False,
            )
    return NativeRouteRead(
        availability="present",
        binding="matched" if digest_matches else "unverified",
        coverage="retained_record_only",
        observed_at=evidence.observed_at,
        attempt=receipt.attempt,
        selector=selector,
        route_digest_matches=digest_matches,
        observed_backend_matches=observed.target_url == execution.backend if observed else None,
        backend_observed_at=observed.observed_at if observed else None,
    )


def _read_native_testing_rebuild_evidence(
    record_store: object, operation: OdooStableTargetReplacementOperationRecord, *, private: bool
) -> NativeTestingExecutionBinding:
    fields: dict[str, Any] = {
        name: NativeTestingEvidenceItem(source=cast(NativeEvidenceSource, source))
        for name, source in (
            ("reservation", "native_reservation"),
            ("original_intent", "native_original_intent"),
            ("checkpoint", "native_checkpoint"),
            ("terminal_probe", "checkpoint_probe"),
            ("candidate_preparation", "checkpoint_preparation"),
            ("candidate_readiness", "checkpoint_private_readiness"),
            ("deployment", "deployment_record"),
            ("forward_route", "forward_route_receipt"),
            ("inverse_route", "inverse_route_receipt"),
            ("selection", "publication_receipt"),
        )
    }

    private_fields: dict[str, Any] = {}

    def result() -> NativeTestingExecutionBinding:
        return NativeTestingExecutionBinding(
            operation_id=operation.operation_id,
            product=operation.product,
            context=operation.context,
            instance=operation.instance,
            native_evidence=NativeTestingRebuildEvidence(
                operation_attempt=operation.attempt, **fields
            ),
            **private_fields,
        )

    if not operation.is_native_testing_rebuild or not callable(
        getattr(record_store, "read_idempotency_record", None)
    ):
        return result()
    store = cast(NativeTestingEvidenceStore, record_store)
    reservation, fields["reservation"] = _read(
        "native_reservation",
        lambda: store.read_idempotency_record(
            scope=REBUILD_SCOPE, route_path=REBUILD_ROUTE, idempotency_key=operation.operation_id
        ),
    )
    if reservation is None:
        return result()
    if not _bound_reservation(
        reservation,
        scope=REBUILD_SCOPE,
        route=REBUILD_ROUTE,
        key=operation.operation_id,
        fingerprint=operation.request_fingerprint,
    ):
        fields["reservation"] = _unbound("native_reservation")
        return result()
    fields["reservation"] = _reservation_item(reservation, "native_reservation")
    if not callable(getattr(store, "read_original_mutation_intent", None)):
        return result()
    read_intent: Callable[[], dict[str, Any]] = partial(
        store.read_original_mutation_intent, reservation
    )
    retained_intent: tuple[dict[str, Any] | None, NativeTestingEvidenceItem] = _read(
        "native_original_intent", read_intent
    )
    intent, fields["original_intent"] = retained_intent
    if intent is None:
        return result()
    if not intent:
        fields["original_intent"] = NativeTestingEvidenceItem(
            source="native_original_intent", availability="missing"
        )
        return result()
    if (
        intent.get("operation_id") != operation.operation_id
        or canonical_json_sha256(intent) != reservation.reconciliation_key
    ):
        fields["original_intent"] = _unbound("native_original_intent")
        fields["checkpoint"] = _unbound("native_checkpoint")
        return result()
    fields["original_intent"] = NativeTestingEvidenceItem(
        source="native_original_intent",
        availability="present",
        binding="matched",
        coverage="retained_record_only",
    )
    if "state" in intent:
        return result()
    checkpoint, fields["checkpoint"] = _read(
        "native_checkpoint",
        lambda: RecordTestingRebuildStore(cast(RebuildRecordStore, record_store)).load_operation(
            product=operation.product,
            context=operation.context,
            operation_id=operation.operation_id,
        ),
    )
    if checkpoint is None:
        return result()
    if not _checkpoint_matches(checkpoint, operation, reservation, intent):
        fields["checkpoint"] = _unbound("native_checkpoint")
        return result()
    fields["checkpoint"] = NativeTestingEvidenceItem(
        source="native_checkpoint",
        availability="present",
        binding="matched",
        coverage="retained_record_only",
        state=checkpoint.step,
        observed_at=checkpoint.updated_at,
    )
    fields["candidate_preparation"] = NativeTestingEvidenceItem(
        source="checkpoint_preparation",
        availability="present" if checkpoint.preparation is not None else "null",
        binding="matched",
        coverage="retained_record_only",
    )
    fields["candidate_readiness"] = _readiness(checkpoint)
    fields["predecessor_snapshot"] = (
        checkpoint.plan.old.availability
        if isinstance(checkpoint.plan.old, UnavailableTestingPredecessor)
        else "identity_recorded"
    )
    fields["terminal_probe"] = _terminal_probe(checkpoint, operation, reservation)
    for name in ("publication_started", "retirement_started", "rollback_started"):
        fields[name] = getattr(checkpoint, name)
    forward, fields["forward_route"] = _route_receipt(
        store, operation, reservation, checkpoint.plan.new.copy_id, "forward_route_receipt"
    )
    inverse, fields["inverse_route"] = _route_receipt(
        store, operation, reservation, checkpoint.plan.old.copy_id, "inverse_route_receipt"
    )
    publication, fields["selection"] = _selection(store, operation, reservation, checkpoint)
    fields["deployment"] = _deployment(store, checkpoint)
    if private:
        try:
            private_fields["execution_server_id"] = require_registered_remote_server(
                intent.get("server_id")
            )
        except ValueError:
            private_fields["execution_server_id"] = None
        private_fields.update(
            reservation_id=reservation.record_id,
            intent_sha256=reservation.reconciliation_key,
            checkpoint_plan_sha256=checkpoint.plan.digest,
            failure_reference=_failure_read(checkpoint),
            candidate=_candidate_read(store, operation, checkpoint, intent),
            forward_route=_route_read(
                store,
                forward,
                fields["forward_route"],
                checkpoint,
                intent,
                inverse=False,
                publication=publication,
            ),
            inverse_route=_route_read(
                store,
                inverse,
                fields["inverse_route"],
                checkpoint,
                intent,
                inverse=True,
                publication=publication,
            ),
        )
    return result()


def read_native_testing_rebuild_evidence(
    record_store: object, operation: OdooStableTargetReplacementOperationRecord
) -> NativeTestingRebuildEvidence:
    return _read_native_testing_rebuild_evidence(
        record_store, operation, private=False
    ).native_evidence


def read_native_testing_execution_binding(
    record_store: object, operation: OdooStableTargetReplacementOperationRecord
) -> NativeTestingExecutionBinding:
    return _read_native_testing_rebuild_evidence(record_store, operation, private=True)
