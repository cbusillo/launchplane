"""Bounded projection of original native drill custody from existing records."""

from __future__ import annotations

from typing import Any, NamedTuple

from sqlalchemy import and_, exists, func, literal, or_, select, union_all
from sqlalchemy.orm import aliased
from sqlalchemy.engine import RowMapping

from control_plane.contracts.testing_candidate import TestingCandidateAllocation
from control_plane.contracts.testing_publication import TestingPublicationRecord
from control_plane.odoo_testing_drill import DRILL_ROUTE, DRILL_SCOPE, lane_key
from control_plane.odoo_testing_restore import generation_binding
from control_plane.storage.postgres import (
    LaunchplaneIdempotencyRow,
    LaunchplaneOdooStableTargetReplacementOperationRow,
    LaunchplaneTestingCandidateRow,
    PostgresRecordStore,
)
from control_plane.testing_publication_storage import PUBLICATION_ROUTE, PUBLICATION_SCOPE
from control_plane.contracts.odoo_testing_drill import (
    NATIVE_DRILL_MAINTENANCE_ROUTE,
    NATIVE_DRILL_QUALIFICATION_ROUTE,
)
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.testing_rebuild import REBUILD_ROUTE, REBUILD_SCOPE
from control_plane.storage.postgres import ORIGINAL_MUTATION_INTENT_ROUTE


def outstanding_native_retirements(
    store: PostgresRecordStore,
    product: str,
    context: str,
    *,
    retained_bindings: tuple[str, ...],
    retained_namespaces: tuple[str, ...],
    limit: int,
    session: Any = None,
) -> tuple[tuple[LaunchplaneIdempotencyRecord, dict[str, Any]], ...]:
    """Original retirement custody, before any immediate-predecessor exemption."""
    child = aliased(LaunchplaneIdempotencyRow)
    intent = aliased(LaunchplaneIdempotencyRow)
    spec = intent.payload["response_payload"]["original_intent"]["native_retirement"]
    identity = spec["identity"]
    qualification = aliased(LaunchplaneIdempotencyRow)
    qualified = exists(
        select(qualification.record_id).where(
            qualification.scope == DRILL_SCOPE,
            qualification.route_path == NATIVE_DRILL_QUALIFICATION_ROUTE,
            qualification.idempotency_key == identity["namespace"].as_string(),
            qualification.state == "completed",
            qualification.payload["response_trace_id"].as_string()
            == identity["retiring_operation_id"].as_string(),
            qualification.payload["response_payload"]["identity_sha256"].as_string()
            == spec["identity_sha256"].as_string(),
        )
    )
    statement = (
        select(child.payload, spec)
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
            child.route_path == REBUILD_ROUTE + ":effect",
            identity["product"].as_string() == product,
            identity["context"].as_string() == context,
            identity["namespace"].as_string().not_in(retained_namespaces),
            identity["copy_binding"].as_string().not_in(retained_bindings),
            ~and_(
                child.state == "completed",
                func.coalesce(child.payload["response_payload"]["retirement"].as_string(), "")
                == "stopped",
                qualified,
            ),
        )
        .order_by(child.created_at, child.record_id)
        .limit(limit)
    )

    def read(held: Any) -> tuple[tuple[LaunchplaneIdempotencyRecord, dict[str, Any]], ...]:
        return tuple(
            (LaunchplaneIdempotencyRecord.model_validate(row[0]), row[1])
            for row in held.execute(statement)
        )

    if session is not None:
        return read(session)
    with store._session_factory() as held:
        return read(held)


def _disposed(resource: Any, namespace: Any = None, resources_binding: Any = None) -> Any:
    receipt = aliased(LaunchplaneIdempotencyRow)
    payload = receipt.payload["response_payload"]
    conditions = [
        receipt.scope == DRILL_SCOPE,
        receipt.route_path == NATIVE_DRILL_MAINTENANCE_ROUTE,
        receipt.state == "completed",
        payload["status"].as_string() == "disposed",
        payload["identity"]["target_id"].as_string() == resource,
    ]
    if namespace is not None:
        conditions.extend(
            (
                receipt.idempotency_key == namespace,
                payload["identity"]["namespace"].as_string() == namespace,
                payload["identity"]["resources_binding"].as_string() == resources_binding,
            )
        )
    return exists(select(receipt.record_id).where(*conditions))


def native_drill_maintenance_candidates(
    store: PostgresRecordStore,
    product: str,
    context: str,
    *,
    protected_targets: tuple[str, ...],
    retained_bindings: tuple[str, ...],
    retained_namespaces: tuple[str, ...],
    current_operation_id: str,
    limit: int,
) -> tuple[TestingCandidateAllocation, ...]:
    """Bound normal cleanup and exceptional allocation backlog before capture."""
    candidate = LaunchplaneTestingCandidateRow
    operation = LaunchplaneOdooStableTargetReplacementOperationRow
    payload = candidate.payload
    statement = (
        select(candidate.payload)
        .join(operation, operation.operation_id == payload["operation_id"].as_string())
        .where(
            operation.product == product,
            operation.context == context,
            operation.instance == "testing",
            operation.idempotency_scope == DRILL_SCOPE,
            operation.operation_id != current_operation_id,
            payload["product"].as_string() == product,
            payload["context"].as_string() == context,
            payload["namespace"].as_string() == candidate.namespace,
            candidate.namespace.not_in(retained_namespaces),
            func.coalesce(
                payload["application"]["prepared"]["source_witness"].as_string(), ""
            ).not_in(retained_bindings),
            func.coalesce(payload["compose_id"].as_string(), "").not_in(protected_targets),
            or_(
                func.coalesce(payload["effect"].as_string(), "") != "",
                func.coalesce(payload["project_id"].as_string(), "") != "",
                func.coalesce(payload["compose_id"].as_string(), "") != "",
            ),
            ~_disposed(
                payload["compose_id"].as_string(),
                candidate.namespace,
                payload["resources_binding_sha256"].as_string(),
            ),
        )
        .order_by(operation.created_at.asc(), candidate.namespace.asc())
        .limit(limit)
    )
    with store._session_factory() as session:
        return tuple(
            TestingCandidateAllocation.model_validate(payload)
            for payload in session.scalars(statement)
        )


class NativeDrillCustodyEvidence(NamedTuple):
    total: int
    copy_bindings: tuple[str, ...]
    candidate_namespaces: tuple[str, ...]


def native_drill_custody_evidence(
    store: PostgresRecordStore,
    product: str,
    context: str,
    serving_target_id: str | None,
    *,
    limit: int,
) -> NativeDrillCustodyEvidence:
    slot = aliased(LaunchplaneIdempotencyRow)
    publication = aliased(LaunchplaneIdempotencyRow)
    operation = LaunchplaneOdooStableTargetReplacementOperationRow
    candidate = LaunchplaneTestingCandidateRow
    sp = slot.payload["response_payload"]
    pp = publication.payload["response_payload"]
    cp = candidate.payload
    pub_candidate = pp["plan"]["candidate"]

    slots = (
        select(sp["operation_id"].as_string().label("operation_id"))
        .where(
            slot.scope == DRILL_SCOPE,
            slot.route_path == DRILL_ROUTE,
            slot.idempotency_key.like(lane_key(product, context) + ":%"),
            sp["native_rebuild"].as_boolean().is_(True),
        )
        .distinct()
        .cte("native_drill_slots")
    )
    # The normalized operation lane plus all JSON custody identity components
    # are joined explicitly. Never match only an operation-looking substring.
    history = slots.join(
        operation,
        and_(
            operation.operation_id == slots.c.operation_id,
            operation.product == product,
            operation.context == context,
            operation.instance == "testing",
            operation.idempotency_scope == DRILL_SCOPE,
        ),
    ).outerjoin(
        publication,
        and_(
            publication.scope == PUBLICATION_SCOPE,
            publication.route_path == PUBLICATION_ROUTE,
            pub_candidate["operation_id"].as_string() == operation.operation_id,
            pub_candidate["product"].as_string() == product,
            pub_candidate["context"].as_string() == context,
        ),
    )
    candidate_history = history.join(
        candidate,
        and_(
            cp["operation_id"].as_string() == operation.operation_id,
            cp["product"].as_string() == product,
            cp["context"].as_string() == context,
            cp["namespace"].as_string() == candidate.namespace,
        ),
    )

    before = pp["plan"]["before"]
    after = pp["plan"]["after"]
    previous_target = before["target_id"]["target_id"].as_string()
    candidate_target = func.coalesce(
        func.nullif(cp["compose_id"].as_string(), ""), candidate.namespace
    )
    previous_where = [
        pp["state"].as_string() == "applied",
        previous_target.is_not(None),
        previous_target != "",
        ~_disposed(previous_target),
    ]
    candidate_where = [
        ~_disposed(
            cp["compose_id"].as_string(),
            candidate.namespace,
            cp["resources_binding_sha256"].as_string(),
        ),
        or_(
            *(
                cp[field].as_string().not_in(("",))
                for field in (
                    "effect",
                    "project_id",
                    "environment_id",
                    "compose_id",
                    "resources_binding_sha256",
                )
            ),
            cp["application"]["plan"]["namespace"].as_string().is_not(None),
        ),
        or_(
            operation.status.not_in(("pending", "running")),
            pp["state"].as_string().in_(("applied", "reversed")),
        ),
    ]
    if serving_target_id is not None:
        previous_where.append(previous_target != serving_target_id)
        # The original Python condition compares compose_id, which is blank
        # before compose allocation. The fallback namespace remains attention.
        candidate_where.append(cp["compose_id"].as_string() != serving_target_id)

    predecessors = (
        select(
            previous_target.label("resource"),
            literal(0).label("priority"),
            literal("predecessor").label("kind"),
            publication.record_id.label("source_id"),
            publication.payload.label("publication_payload"),
            publication.idempotency_key.label("publication_key"),
            publication.request_fingerprint.label("publication_fingerprint"),
            literal(None, type_=candidate.payload.type).label("candidate_payload"),
            literal("").label("candidate_namespace"),
            before["generation"]["product"].as_string().is_not(None).label("has_binding"),
        )
        .select_from(history)
        .where(*previous_where)
    )
    candidates = (
        select(
            candidate_target.label("resource"),
            literal(1).label("priority"),
            literal("candidate").label("kind"),
            candidate.namespace.label("source_id"),
            publication.payload.label("publication_payload"),
            publication.idempotency_key.label("publication_key"),
            publication.request_fingerprint.label("publication_fingerprint"),
            candidate.payload.label("candidate_payload"),
            candidate.namespace.label("candidate_namespace"),
            and_(
                pp["state"].as_string() == "applied",
                after["generation"]["completed_at"].as_string().not_in(("",)),
            ).label("has_binding"),
        )
        .select_from(candidate_history)
        .where(*candidate_where)
    )
    resources = union_all(predecessors, candidates).cte("native_drill_resources")
    ranked = select(
        *resources.c,
        func.row_number()
        .over(
            partition_by=resources.c.resource,
            order_by=(resources.c.priority, resources.c.source_id),
        )
        .label("position"),
    ).cte("native_drill_ranked_custody")
    retained = select(*ranked.c).where(ranked.c.position == 1).cte("native_drill_retained_custody")
    # Exact disposal tombstones filter physical custody above; immutable
    # publication and candidate records remain the provenance for the total.
    with store._session_factory() as session:
        total = int(session.scalar(select(func.count()).select_from(retained)) or 0)
        binding_rows = (
            session.execute(
                select(retained)
                .where(retained.c.has_binding.is_(True))
                .order_by(retained.c.resource)
                .limit(limit)
            )
            .mappings()
            .all()
        )
        namespace_rows = (
            session.execute(
                select(retained)
                .where(retained.c.candidate_namespace != "")
                .order_by(retained.c.resource)
                .limit(limit)
            )
            .mappings()
            .all()
        )

    def sampled_publication(row: RowMapping) -> TestingPublicationRecord:
        record = TestingPublicationRecord.model_validate(
            row["publication_payload"]["response_payload"]
        )
        if (
            record.plan.key != row["publication_key"]
            or record.plan.digest != row["publication_fingerprint"]
        ):
            raise ValueError("Stored testing publication provenance is inconsistent.")
        return record

    bindings = []
    for row in binding_rows:
        record = sampled_publication(row)
        generation = (
            record.plan.before.generation
            if row["kind"] == "predecessor"
            else record.plan.after.generation
        )
        assert generation is not None
        bindings.append(generation_binding(generation))
    namespaces = []
    for row in namespace_rows:
        allocated = TestingCandidateAllocation.model_validate(row["candidate_payload"])
        if allocated.namespace != row["candidate_namespace"]:
            raise ValueError("Stored testing candidate provenance is inconsistent.")
        namespaces.append(allocated.namespace)
    return NativeDrillCustodyEvidence(total, tuple(bindings), tuple(namespaces))
