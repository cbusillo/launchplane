from collections.abc import Callable
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from typing import Literal, cast, get_args

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    LaunchplaneMutationReservationState,
)
from control_plane.contracts.promotion_record import ReleaseStatus
from control_plane.contracts.runtime_identity import RuntimeIdentityStatus
from control_plane.contracts.testing_activation import (
    TestingActivationRecord,
    TestingActivationRequest,
    TestingActivationRefusal,
)
from control_plane.contracts.testing_rebuild import REBUILD_SCOPE
from control_plane.dokploy.testing_route import route_document
from control_plane.dokploy.testing_route_execution import (
    TestingRouteExecutionIntent,
    TestingRouteWriterAttempt,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.provider_operations import ProviderObservation, build_provider_operation_key
from control_plane.testing_activation import ROUTE, activation_record, preparation_resources
from control_plane.testing_separation_inspection import inspect_preparation_programs
from control_plane.testing_separation_resources import (
    SeparationResourcesStatus,
    SeparationProgramInspection,
)
from control_plane.testing_route_effects import (
    ROUTE_PREPARING,
    ROUTE_WRITING,
    ROUTE_WRITER_RECEIPT,
)
from control_plane.testing_application_inspection import ApplicationCandidateInspection

_logger = logging.getLogger(__name__)

RouteCheckpoint = Literal["not_recorded", "preparing", "writing", "unrecognized"]
RouteWriterOutcome = Literal[
    "applied", "excluded", "busy", "unknown", "retired", "superseded", "unrecognized"
]


class TestingActivationRouteInspection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    direction: Literal["forward", "inverse"]
    status: Literal["not_found", "unbound", "bound"]
    state: LaunchplaneMutationReservationState | None = None
    attempt: int | None = Field(default=None, ge=1)
    checkpoint: RouteCheckpoint | None = None
    updated_at: str = ""
    route_write_not_started: bool | None = None
    writer_status: Literal["not_read", "not_found", "unbound", "bound"] = "not_read"
    writer_state: LaunchplaneMutationReservationState | None = None
    writer_attempt: int | None = Field(default=None, ge=1)
    writer_checkpoint: RouteCheckpoint | None = None
    writer_updated_at: str = ""
    writer_outcome: RouteWriterOutcome | None = None


def route_checkpoint(phase: str) -> RouteCheckpoint:
    if not phase:
        return "not_recorded"
    if phase == ROUTE_PREPARING:
        return "preparing"
    if phase == ROUTE_WRITING:
        return "writing"
    return "unrecognized"


def route_receipt_matches(
    row: LaunchplaneIdempotencyRecord,
    *,
    scope: str,
    path: str,
    key: str,
    fingerprint: str,
    target: str,
) -> bool:
    return (
        row.scope,
        row.route_path,
        row.idempotency_key,
        row.request_fingerprint,
        row.reconciliation_key,
        row.provider_target_key,
    ) == (scope, path, key, fingerprint, fingerprint, target)


def inspect_route_writer(
    store: PostgresRecordStore,
    execution: TestingRouteExecutionIntent,
    observed: TestingActivationRouteInspection,
) -> TestingActivationRouteInspection:
    assert observed.attempt is not None
    key = execution.key(attempt=observed.attempt)
    writer = store.read_idempotency_record(
        scope=REBUILD_SCOPE, route_path=ROUTE_WRITER_RECEIPT, idempotency_key=key
    )
    if writer is None:
        return observed.model_copy(update={"writer_status": "not_found"})
    unbound = observed.model_copy(update={"writer_status": "unbound"})
    try:
        original = TestingRouteWriterAttempt.model_validate(
            store.read_original_mutation_intent(writer).get("route_writer")
        )
    except (FileNotFoundError, ValueError, TypeError):
        return unbound
    if (
        original.execution != execution
        or original.key != key
        or not route_receipt_matches(
            writer,
            scope=REBUILD_SCOPE,
            path=ROUTE_WRITER_RECEIPT,
            key=key,
            fingerprint=canonical_json_sha256(original.model_dump(mode="json")),
            target="testing-route-writer:" + key,
        )
    ):
        return unbound
    outcome = writer.response_payload.get("writer_outcome") if writer.state == "completed" else None
    return observed.model_copy(
        update={
            "writer_status": "bound",
            "writer_state": writer.state,
            "writer_attempt": writer.attempt,
            "writer_checkpoint": route_checkpoint(writer.provider_effect_phase),
            "writer_updated_at": writer.updated_at,
            "writer_outcome": None
            if outcome is None
            else cast(RouteWriterOutcome, outcome)
            if outcome in get_args(RouteWriterOutcome)
            else "unrecognized",
        }
    )


def inspect_application_route_receipts(
    store: PostgresRecordStore, record: TestingActivationRecord
) -> tuple[TestingActivationRouteInspection, ...]:
    readiness = record.application_readiness
    if readiness is None or readiness.route is None or readiness.route_execution is None:
        return ()
    result = []
    for inverse in (False, True):
        observed = TestingActivationRouteInspection(
            direction="inverse" if inverse else "forward",
            status="not_found",
        )
        suffix = ":route-inverse" if inverse else ":route"
        path, key = ROUTE.format(product=record.product) + suffix, record.plan_sha256 + suffix
        child = store.read_idempotency_record(
            scope=record.scope, route_path=path, idempotency_key=key
        )
        if child is None:
            result.append(observed)
            continue
        observed = observed.model_copy(update={"status": "unbound"})
        expected = readiness.route_execution
        if inverse:
            expected = expected.model_copy(
                update={
                    "backend": readiness.route.old_backend,
                    "document_sha256": canonical_json_sha256(
                        route_document(readiness.route, readiness.route.old_backend)
                    ),
                }
            )
        try:
            execution = TestingRouteExecutionIntent.model_validate(
                store.read_original_mutation_intent(child).get("route_execution")
            )
        except (FileNotFoundError, ValueError, TypeError):
            result.append(observed)
            continue
        exclude = {"custody"} if inverse else set()
        if execution.model_dump(exclude=exclude) != expected.model_dump(
            exclude=exclude
        ) or not route_receipt_matches(
            child,
            scope=record.scope,
            path=path,
            key=key,
            fingerprint=readiness.route.digest,
            target="testing-activation-route:" + record.product + ":" + record.context,
        ):
            result.append(observed)
            continue
        not_started = (
            child.response_payload.get("route_write_not_started")
            if child.state == "completed"
            else None
        )
        observed = observed.model_copy(
            update={
                "status": "bound",
                "state": child.state,
                "attempt": child.attempt,
                "checkpoint": route_checkpoint(child.provider_effect_phase),
                "updated_at": child.updated_at,
                "route_write_not_started": not_started if isinstance(not_started, bool) else None,
            }
        )
        result.append(inspect_route_writer(store, execution, observed))
    return tuple(result)


class TestingActivationInspection(SeparationResourcesStatus):
    activation_phase: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,128}$")
    predecessor_recorded: bool
    external_capability_count: int = Field(ge=0)
    programs: tuple[SeparationProgramInspection, ...]
    inspected_at: AwareDatetime
    qualification: Literal["not_evaluated"] = "not_evaluated"
    routes: tuple[TestingActivationRouteInspection, ...] = Field(default=(), max_length=2)
    private_health_status: ReleaseStatus | None = None
    private_health_verified: bool | None = None
    private_identity_status: RuntimeIdentityStatus | None = None
    public_health_status: ReleaseStatus | None = None
    public_health_verified: bool | None = None
    public_identity_status: RuntimeIdentityStatus | None = None
    candidate: ApplicationCandidateInspection | None = None


def inspect_application_activation(
    *,
    root: Path,
    store: PostgresRecordStore,
    product: str,
    request: TestingActivationRequest,
    actor: str,
    scope: str,
    idempotency_key: str,
    trace_id: str,
    observe_original: Callable[[str, str, str], ProviderObservation],
    inspect_candidate: Callable[[TestingActivationRecord, str], ApplicationCandidateInspection]
    | None = None,
) -> TestingActivationInspection:
    if not idempotency_key or request.application is None:
        raise TestingActivationRefusal(
            "Inspection requires the original application key and request."
        )
    row = store.read_idempotency_record(
        scope=scope, route_path=ROUTE.format(product=product), idempotency_key=idempotency_key
    )
    original = store.read_testing_application_activation_operation(
        product, request.context, scope, request.preparation_key
    )
    record = activation_record(
        store, product, request.context, scope=scope, preparation_key=request.preparation_key
    )
    if (
        row is None
        or original is None
        or original.record_id != row.record_id
        or record is None
        or record.actor != actor
        or record.plan_sha256 != row.reconciliation_key
        or record.request.model_dump(exclude={"mode", "reviewed_plan_sha256"})
        != request.model_dump(exclude={"mode", "reviewed_plan_sha256"})
    ):
        raise TestingActivationRefusal("Inspection belongs to another original operation.")
    resources, preparation_id = preparation_resources(store, product, request, scope)
    if (
        preparation_id != record.preparation_record_id
        or resources.target_id != record.retained_target_id.target_id
        or record.namespace != resources.namespace + "-recipient"
    ):
        raise TestingActivationRefusal("Inspection preparation custody changed.")
    programs = inspect_preparation_programs(
        root=root,
        resources=resources,
        store=store,
        application=True,
        namespace=record.namespace,
        include_storage=False,
    )
    if row.state == "reconcile_required" and record.phase == "create_application_mount":
        observation = observe_original(
            build_provider_operation_key(
                scope=row.scope,
                route_path=row.route_path,
                idempotency_key=row.idempotency_key,
                request_fingerprint=row.request_fingerprint,
                reconciliation_key=row.reconciliation_key,
            ),
            row.provider_effect_phase,
            row.reconciliation_key,
        )
        _logger.warning(
            "%s",
            json.dumps(
                {
                    "event": "testing_activation_original_observation",
                    "trace_id": trace_id,
                    "outcome": observation.outcome,
                }
            ),
        )
    readiness = record.application_readiness
    private_health = readiness.private_health if readiness else None
    public_health = readiness.public_health if readiness else None
    candidate = None
    if inspect_candidate is not None and record.phase in {
        "restoring_application_route",
        "refusing_private_candidate",
        "refused",
    }:
        candidate = inspect_candidate(
            record,
            build_provider_operation_key(
                scope=row.scope,
                route_path=row.route_path,
                idempotency_key=row.idempotency_key,
                request_fingerprint=row.request_fingerprint,
                reconciliation_key=row.reconciliation_key,
            ),
        )
        if (
            activation_record(
                store,
                product,
                request.context,
                scope=scope,
                preparation_key=request.preparation_key,
            )
            != record
        ):
            candidate = ApplicationCandidateInspection(placement="unavailable")
    return TestingActivationInspection(
        product=product,
        context=request.context,
        state=row.state,
        plan_sha256=record.plan_sha256,
        provider_effect_phase=row.provider_effect_phase,
        created_at=row.created_at,
        updated_at=row.updated_at,
        lease_expires_at=row.lease_expires_at,
        provider_effect_started_at=row.provider_effect_started_at,
        activation_phase=record.phase,
        predecessor_recorded=record.predecessor is not None,
        external_capability_count=len(record.external_capabilities),
        programs=tuple(programs),
        inspected_at=datetime.now(timezone.utc),
        routes=inspect_application_route_receipts(store, record),
        private_health_status=private_health.status if private_health else None,
        private_health_verified=private_health.verified if private_health else None,
        private_identity_status=private_health.runtime_identity_status if private_health else None,
        public_health_status=public_health.status if public_health else None,
        public_health_verified=public_health.verified if public_health else None,
        public_identity_status=public_health.runtime_identity_status if public_health else None,
        candidate=candidate,
    )
