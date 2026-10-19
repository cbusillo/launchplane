"""Bounded projection of original native drill custody from existing records."""

from __future__ import annotations

from typing import NamedTuple

from sqlalchemy import and_, func, literal, or_, select, union_all
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
    ]
    candidate_where = [
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
    # A future native maintenance tombstone should qualify this resource CTE
    # through NOT EXISTS on its original target/resources binding. Do not erase
    # immutable publication/candidate records, and do not invent a new ledger.
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
