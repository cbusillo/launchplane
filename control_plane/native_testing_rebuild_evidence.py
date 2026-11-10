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
    NativeTestingEvidenceItem,
    NativeTestingRebuildEvidence,
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.contracts.testing_candidate import testing_candidate_namespace
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
from control_plane.testing_rebuild_storage import RecordTestingRebuildStore, RebuildRecordStore
from control_plane.traffic_switch import require_route_authority

T = TypeVar("T")


class NativeTestingEvidenceStore(Protocol):
    def read_idempotency_record(
        self, *, scope: str, route_path: str, idempotency_key: str
    ) -> LaunchplaneIdempotencyRecord | None: ...
    def read_original_mutation_intent(
        self, reservation: LaunchplaneIdempotencyRecord
    ) -> dict[str, Any]: ...
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
        probe.plan_digest != checkpoint.plan.digest
        or probe.observed_at.utcoffset() is None
        or not checkpoint.began_at <= probe.observed_at <= checkpoint.updated_at
        or probe.reservation_id != reservation.record_id
        or probe.operation_attempt != operation.attempt
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
        http_status=health.http_status,
        failure_kind=health.failure_kind,
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
) -> NativeTestingEvidenceItem:
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
        return evidence
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
        return _unbound(source)
    markers = {
        name: record.response_payload.get(name)
        for name in ("not_dispatched", "route_write_not_started")
    }
    if any(value is not None and type(value) is not bool for value in markers.values()):
        return NativeTestingEvidenceItem(
            source=source,
            availability="unavailable",
            binding="matched",
            coverage="retained_record_only",
        )
    return _reservation_item(record, source).model_copy(
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
) -> NativeTestingEvidenceItem:
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
        return evidence
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
        reservation.reconciliation_key,
        reservation.record_id,
        reservation.provider_target_key,
    ) or plan.deployment.record_id != checkpoint.plan.new.runtime_identity.deployment_record_id:
        return _unbound("publication_receipt")
    return NativeTestingEvidenceItem(
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


def read_native_testing_rebuild_evidence(
    record_store: object, operation: OdooStableTargetReplacementOperationRecord
) -> NativeTestingRebuildEvidence:
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

    def result() -> NativeTestingRebuildEvidence:
        return NativeTestingRebuildEvidence(operation_attempt=operation.attempt, **fields)

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
    fields["candidate_readiness"] = NativeTestingEvidenceItem(
        source="checkpoint_private_readiness",
        availability="present" if checkpoint.readiness is not None else "null",
        binding="matched",
        coverage="retained_record_only",
        observed_at=checkpoint.readiness.observed_at if checkpoint.readiness else None,
    )
    fields["predecessor_snapshot"] = (
        checkpoint.plan.old.availability
        if isinstance(checkpoint.plan.old, UnavailableTestingPredecessor)
        else "identity_recorded"
    )
    fields["terminal_probe"] = _terminal_probe(checkpoint, operation, reservation)
    for name in ("publication_started", "retirement_started", "rollback_started"):
        fields[name] = getattr(checkpoint, name)
    fields["forward_route"] = _route_receipt(
        store, operation, reservation, checkpoint.plan.new.copy_id, "forward_route_receipt"
    )
    fields["inverse_route"] = _route_receipt(
        store, operation, reservation, checkpoint.plan.old.copy_id, "inverse_route_receipt"
    )
    fields["selection"] = _selection(store, operation, reservation, checkpoint)
    fields["deployment"] = _deployment(store, checkpoint)
    return result()
