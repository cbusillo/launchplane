"""Native PostgreSQL storage for the bounded historical merge disposition."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import desc, func, or_, select, text

from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidateRecord,
    MergeTrainBatchLandingPlanRecord,
)
from control_plane.contracts.merge_train_controller_state import MergeTrainControllerStateRecord
from control_plane.contracts.merge_train_historical_completion import (
    MergeTrainHistoricalCompletionProviderEvidence,
)
from control_plane.contracts.merge_train_policy import MergeTrainPolicyRecord
from control_plane.contracts.merge_train_stack_collapse import MergeTrainStackCollapsePlanRecord
from control_plane.merge_train_historical_completion import (
    HistoricalCompletionAssessmentFailure,
    HistoricalCompletionSnapshot,
    HistoricalCompletionSnapshotStore,
    read_historical_completion_snapshot,
)
from control_plane.merge_train_historical_disposition import (
    HistoricalDispositionAuthority,
    HistoricalDispositionError,
    HistoricalDispositionRequest,
    build_historical_disposition,
    historical_disposition_record_id,
)

if TYPE_CHECKING:
    from control_plane.storage.postgres import PostgresRecordStore


class _SessionSnapshotStore(HistoricalCompletionSnapshotStore):
    """Session-bound adapter used by the shared B1 reader."""

    def __init__(self, store: PostgresRecordStore, session: Any) -> None:
        self._store = store
        self._session = session

    def _models(self) -> Any:
        from control_plane.storage.postgres import (
            LaunchplaneMergeAdmissionRow,
            LaunchplaneMergeTrainBatchCandidateRow,
            LaunchplaneMergeTrainBatchLandingPlanRow,
            LaunchplaneMergeTrainControllerStateRow,
            LaunchplaneMergeTrainStackCollapsePlanRow,
        )

        return (
            LaunchplaneMergeAdmissionRow,
            LaunchplaneMergeTrainBatchCandidateRow,
            LaunchplaneMergeTrainBatchLandingPlanRow,
            LaunchplaneMergeTrainControllerStateRow,
            LaunchplaneMergeTrainStackCollapsePlanRow,
        )

    def list_merge_train_controller_state_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[MergeTrainControllerStateRecord, ...]:
        _, _, _, row_type, _ = self._models()
        filters: list[object] = []
        if repository:
            filters.append(row_type.repository == repository)
        if base_branch:
            filters.append(row_type.base_branch == base_branch)
        if status:
            filters.append(row_type.status == status)
        statement = (
            select(row_type)
            .where(*cast(Any, filters))
            .order_by(row_type.updated_at.desc(), row_type.controller_key.desc())
        )
        if limit is not None:
            statement = statement.limit(max(limit, 0))
        return tuple(
            self._store._read_payload(
                model_type=MergeTrainControllerStateRecord, payload=row.payload
            )
            for row in self._session.scalars(statement)
        )

    def list_merge_train_policy_records(
        self, *, status: str = "", limit: int | None = None
    ) -> tuple[MergeTrainPolicyRecord, ...]:
        from control_plane.storage.postgres import LaunchplaneMergeTrainPolicyRow

        statement = select(LaunchplaneMergeTrainPolicyRow)
        if status:
            statement = statement.where(LaunchplaneMergeTrainPolicyRow.status == status)
        statement = statement.order_by(
            LaunchplaneMergeTrainPolicyRow.updated_at.desc(),
            LaunchplaneMergeTrainPolicyRow.record_id.desc(),
        )
        if limit is not None:
            statement = statement.limit(max(limit, 0))
        return tuple(
            self._store._read_payload(model_type=MergeTrainPolicyRecord, payload=row.payload)
            for row in self._session.scalars(statement)
        )

    def list_merge_train_batch_landing_plan_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        status: str = "",
        record_id: str = "",
        limit: int | None = None,
    ) -> tuple[MergeTrainBatchLandingPlanRecord, ...]:
        _, _, row_type, _, _ = self._models()
        filters: list[object] = []
        if repository:
            filters.append(row_type.repository == repository)
        if base_branch:
            filters.append(row_type.base_branch == base_branch)
        if status:
            filters.append(row_type.status == status)
        if record_id:
            filters.append(row_type.record_id == record_id)
        statement = (
            select(row_type)
            .where(*cast(Any, filters))
            .order_by(row_type.updated_at.desc(), row_type.record_id.desc())
        )
        if limit is not None:
            statement = statement.limit(max(limit, 0))
        return tuple(
            self._store._read_payload(
                model_type=MergeTrainBatchLandingPlanRecord, payload=row.payload
            )
            for row in self._session.scalars(statement)
        )

    def list_merge_train_batch_candidate_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        status: str = "",
        batch_id: str = "",
        limit: int | None = None,
    ) -> tuple[MergeTrainBatchCandidateRecord, ...]:
        _, row_type, _, _, _ = self._models()
        filters: list[object] = []
        if repository:
            filters.append(row_type.repository == repository)
        if base_branch:
            filters.append(row_type.base_branch == base_branch)
        if status:
            filters.append(row_type.status == status)
        if batch_id:
            filters.append(row_type.batch_id == batch_id)
        statement = (
            select(row_type)
            .where(*cast(Any, filters))
            .order_by(row_type.updated_at.desc(), row_type.record_id.desc())
        )
        if limit is not None:
            statement = statement.limit(max(limit, 0))
        return tuple(
            self._store._read_payload(
                model_type=MergeTrainBatchCandidateRecord, payload=row.payload
            )
            for row in self._session.scalars(statement)
        )

    def list_merge_train_stack_collapse_plan_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        status: str = "",
        root_pull_request_number: int | None = None,
        limit: int | None = None,
    ) -> tuple[MergeTrainStackCollapsePlanRecord, ...]:
        _, _, _, _, row_type = self._models()
        filters: list[object] = []
        if repository:
            filters.append(row_type.repository == repository)
        if base_branch:
            filters.append(row_type.base_branch == base_branch)
        if status:
            filters.append(row_type.status == status)
        if root_pull_request_number is not None:
            filters.append(row_type.root_pull_request_number == root_pull_request_number)
        statement = (
            select(row_type)
            .where(*cast(Any, filters))
            .order_by(row_type.updated_at.desc(), row_type.record_id.desc())
        )
        if limit is not None:
            statement = statement.limit(max(limit, 0))
        return tuple(
            self._store._read_payload(
                model_type=MergeTrainStackCollapsePlanRecord, payload=row.payload
            )
            for row in self._session.scalars(statement)
        )

    def list_merge_admission_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        pull_request_number: int | None = None,
        landing_plan_id: str = "",
        limit: int | None = None,
    ) -> tuple[Any, ...]:
        row_type, _, _, _, _ = self._models()
        from control_plane.contracts.merge_admission_record import MergeAdmissionRecord

        filters: list[object] = []
        if repository:
            filters.append(row_type.repository == repository)
        if base_branch:
            filters.append(row_type.base_branch == base_branch)
        if pull_request_number is not None:
            filters.append(row_type.pull_request_number == pull_request_number)
        if landing_plan_id:
            filters.append(row_type.landing_plan_id == landing_plan_id)
        statement = (
            select(row_type)
            .where(*cast(Any, filters))
            .order_by(row_type.created_at.desc(), row_type.admission_id.desc())
        )
        if limit is not None:
            statement = statement.limit(max(limit, 0))
        return tuple(
            self._store._read_payload(model_type=MergeAdmissionRecord, payload=row.payload)
            for row in self._session.scalars(statement)
        )

    def has_ordinary_merge_train_target_fence(self, *, repository: str, base_branch: str) -> bool:
        from control_plane.storage.postgres import (
            LaunchplaneMergeTrainBatchCandidateRow,
            LaunchplaneMergeTrainBatchLandingPlanRow,
            LaunchplaneMergeTrainStackCollapsePlanRow,
            LaunchplaneOrdinaryAgentEffectRow,
        )

        active_rows: tuple[tuple[Any, type[Any]], ...] = (
            (LaunchplaneMergeTrainBatchCandidateRow, MergeTrainBatchCandidateRecord),
            (LaunchplaneMergeTrainBatchLandingPlanRow, MergeTrainBatchLandingPlanRecord),
            (LaunchplaneMergeTrainStackCollapsePlanRow, MergeTrainStackCollapsePlanRecord),
        )
        for row_type, model_type in active_rows:
            statement = select(row_type).where(
                row_type.status == "active",
                row_type.repository == repository,
                row_type.base_branch == base_branch,
            )
            for row in self._session.scalars(statement):
                record = self._store._read_payload(model_type=model_type, payload=row.payload)
                if record.ordinary_job_binding is not None:
                    return True
        target = LaunchplaneOrdinaryAgentEffectRow.payload["target"]
        target_repository = LaunchplaneOrdinaryAgentEffectRow.payload["target"]["repository"]
        target_base_branch = LaunchplaneOrdinaryAgentEffectRow.payload["target"]["base_branch"]
        malformed_target = self._session.scalar(
            select(LaunchplaneOrdinaryAgentEffectRow.effect_id)
            .where(
                or_(
                    func.jsonb_typeof(target).is_(None),
                    func.jsonb_typeof(target) != "object",
                    func.jsonb_typeof(target_repository).is_(None),
                    func.jsonb_typeof(target_repository) != "string",
                    func.jsonb_typeof(target_base_branch).is_(None),
                    func.jsonb_typeof(target_base_branch) != "string",
                    func.btrim(target_repository.as_string()) == "",
                    func.btrim(target_base_branch.as_string()) == "",
                )
            )
            .limit(1)
        )
        if malformed_target is not None:
            raise ValueError("ordinary_effect_ambiguous")
        target_match = self._session.scalar(
            select(LaunchplaneOrdinaryAgentEffectRow.effect_id)
            .where(
                func.lower(func.btrim(target_repository.as_string())) == repository,
                func.btrim(target_base_branch.as_string()) == base_branch,
            )
            .limit(1)
        )
        if target_match is not None:
            return True
        return False


def _lock_timeout(store: Any, session: Any) -> None:
    if store.database_dialect_name == "postgresql":
        session.execute(text("SET LOCAL lock_timeout = '1s'"))
        session.execute(text("SET LOCAL statement_timeout = '5s'"))


def _active_merge_policy(store: PostgresRecordStore, session: Any) -> MergeTrainPolicyRecord:
    from control_plane.storage.postgres import LaunchplaneMergeTrainPolicyRow

    store._lock_merge_train_policy_write(session)
    rows = tuple(
        session.scalars(
            select(LaunchplaneMergeTrainPolicyRow)
            .where(LaunchplaneMergeTrainPolicyRow.status == "active")
            .order_by(desc(LaunchplaneMergeTrainPolicyRow.updated_at))
            .limit(2)
            .with_for_update()
        )
    )
    if len(rows) != 1:
        raise HistoricalDispositionError("merge_policy_unavailable")
    try:
        record = store._read_payload(model_type=MergeTrainPolicyRecord, payload=rows[0].payload)
    except (TypeError, ValueError) as error:
        raise HistoricalDispositionError("merge_policy_unavailable") from error
    row = rows[0]
    if (
        row.record_id != record.record_id
        or row.status != record.status
        or row.source != record.source
        or row.updated_at != record.updated_at
        or row.policy_sha256 != record.policy_sha256
    ):
        raise HistoricalDispositionError("merge_policy_changed")
    return record


def _active_authz_policy(store: PostgresRecordStore, session: Any) -> LaunchplaneAuthzPolicyRecord:
    from control_plane.storage.postgres import LaunchplaneAuthzPolicyRow

    store._lock_active_authz_policy(session)
    rows = tuple(
        session.scalars(
            select(LaunchplaneAuthzPolicyRow)
            .where(LaunchplaneAuthzPolicyRow.status == "active")
            .order_by(desc(LaunchplaneAuthzPolicyRow.revision))
            .limit(2)
            .with_for_update()
        )
    )
    if len(rows) != 1:
        raise HistoricalDispositionError("authz_policy_unavailable")
    try:
        record = store._read_authz_policy_row(rows[0])
    except (TypeError, ValueError) as error:
        raise HistoricalDispositionError("authz_policy_unavailable") from error
    row = rows[0]
    if (
        row.record_id != record.record_id
        or row.status != record.status
        or row.source != record.source
        or row.updated_at != record.updated_at
        or row.revision != record.revision
        or row.policy_sha256 != record.policy_sha256
    ):
        raise HistoricalDispositionError("authorization_changed")
    return record


def _service_authz(request: HistoricalDispositionRequest, policy: MergeTrainPolicyRecord) -> Any:
    try:
        return policy.policy.find_repository_policy(
            repository=request.repository, base_branch=request.base_branch
        ).service_authz
    except ValueError as error:
        raise HistoricalDispositionError("merge_policy_unavailable") from error


def _validate_authority(
    request: HistoricalDispositionRequest,
    *,
    policy: MergeTrainPolicyRecord,
    authz: LaunchplaneAuthzPolicyRecord,
    expected_policy: MergeTrainPolicyRecord | None = None,
    expected_authz: LaunchplaneAuthzPolicyRecord | None = None,
) -> None:
    if expected_policy is not None and (
        policy.record_id != expected_policy.record_id
        or policy.policy_sha256 != expected_policy.policy_sha256
    ):
        raise HistoricalDispositionError("merge_policy_changed")
    if expected_authz is not None and (
        authz.record_id != expected_authz.record_id
        or authz.revision != expected_authz.revision
        or authz.policy_sha256 != expected_authz.policy_sha256
    ):
        raise HistoricalDispositionError("authorization_changed")
    service_authz = _service_authz(request, policy)
    if not authz.policy.allows(
        identity=request.identity,
        action=service_authz.action,
        product=service_authz.product,
        context=service_authz.context,
    ):
        raise HistoricalDispositionError("authorization_denied", status_code=403)


def _validate_replay(
    request: HistoricalDispositionRequest,
    record: LaunchplaneIdempotencyRecord,
    service_authz: Any,
) -> None:
    if (
        record.scope != request.scope
        or record.route_path != request.route_path
        or record.idempotency_key != request.idempotency_key
        or record.request_fingerprint != request.request_fingerprint
    ):
        raise HistoricalDispositionError("idempotency_key_reused")
    if record.state == "running":
        raise HistoricalDispositionError("mutation_in_progress")
    if record.state == "reconcile_required":
        raise HistoricalDispositionError("mutation_reconciliation_required")
    payload = record.response_payload
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        raise HistoricalDispositionError("idempotency_record_invalid")
    if (
        result.get("repository") != request.repository
        or result.get("base_branch") != request.base_branch
    ):
        raise HistoricalDispositionError("idempotency_target_changed")
    if result.get("controller_action") != "record_historical_completion":
        raise HistoricalDispositionError("idempotency_action_changed")
    disposition = result.get("historical_completion_disposition")
    authorization = (
        disposition.get("disposition_authorization") if isinstance(disposition, dict) else None
    )
    if not isinstance(authorization, dict) or any(
        authorization.get(field) != getattr(service_authz, field)
        for field in ("action", "product", "context")
    ):
        raise HistoricalDispositionError("idempotency_authority_changed")


def authorize_merge_train_historical_completion(
    store: PostgresRecordStore, request: HistoricalDispositionRequest
) -> HistoricalDispositionAuthority:
    if store.database_dialect_name != "postgresql":
        raise HistoricalDispositionError("database_storage_required", status_code=503)
    with store._session_factory() as session:
        store._begin_serialized_write(session)
        _lock_timeout(store, session)
        # Ordinary worker context locks authz before merge policy.
        authz = _active_authz_policy(store, session)
        policy = _active_merge_policy(store, session)
        _validate_authority(request, policy=policy, authz=authz)
        replay = None
        if request.idempotency_key:
            row = session.scalar(
                store._idempotency_statement(
                    scope=request.scope,
                    route_path=request.route_path,
                    idempotency_key=request.idempotency_key,
                    for_update=True,
                )
            )
            if row is not None:
                replay = store._read_payload(
                    model_type=LaunchplaneIdempotencyRecord, payload=row.payload
                )
                _validate_replay(
                    request,
                    replay,
                    _service_authz(request, policy),
                )
        session.rollback()
        return HistoricalDispositionAuthority(policy=policy, authz=authz, replay_record=replay)


def _strict_stack_overlap(
    reader: _SessionSnapshotStore,
    request: HistoricalDispositionRequest,
    snapshot: HistoricalCompletionSnapshot,
) -> None:
    selected = {entry.pull_request_number for entry in snapshot.landing.landing_plan.entries}
    stacks = reader.list_merge_train_stack_collapse_plan_records(
        repository=request.repository,
        base_branch=request.base_branch,
        status="active",
        limit=None,
    )
    if any(
        stack.plan.root_pull_request_number in selected
        or any(entry.pull_request_number in selected for entry in stack.plan.entries)
        for stack in stacks
    ):
        raise HistoricalDispositionError("stack_batch_unsupported")


def _read_snapshot_locked(
    store: Any,
    session: Any,
    request: HistoricalDispositionRequest,
) -> HistoricalCompletionSnapshot:
    reader = _SessionSnapshotStore(store, session)
    try:
        snapshot = read_historical_completion_snapshot(
            store=reader,
            repository=request.repository,
            base_branch=request.base_branch,
            selector=request.selector,
        )
    except HistoricalCompletionAssessmentFailure as error:
        raise HistoricalDispositionError(error.reason) from error
    except (TypeError, ValueError) as error:
        reason = (
            "ordinary_effect_ambiguous"
            if str(error) == "ordinary_effect_ambiguous"
            else "snapshot_unavailable"
        )
        raise HistoricalDispositionError(reason) from error
    try:
        _strict_stack_overlap(reader, request, snapshot)
        _all_target_admissions(
            session,
            request,
            (entry.pull_request_number for entry in snapshot.landing.landing_plan.entries),
        )
    except HistoricalDispositionError:
        raise
    except (TypeError, ValueError) as error:
        raise HistoricalDispositionError("snapshot_unavailable") from error
    return snapshot


def read_merge_train_historical_completion_snapshot(
    store: PostgresRecordStore,
    request: HistoricalDispositionRequest,
    authority: HistoricalDispositionAuthority,
) -> HistoricalCompletionSnapshot:
    if store.database_dialect_name != "postgresql":
        raise HistoricalDispositionError("database_storage_required", status_code=503)
    from control_plane.storage.postgres import LaunchplaneMergeTrainControllerStateRow

    with store._session_factory() as session:
        store._begin_serialized_write(session)
        _lock_timeout(store, session)
        snapshot_controller_key = historical_controller_key(request)
        store._advisory_lock_merge_train_controller(session, snapshot_controller_key)
        controller_row = session.scalar(
            select(LaunchplaneMergeTrainControllerStateRow)
            .where(
                LaunchplaneMergeTrainControllerStateRow.controller_key == snapshot_controller_key
            )
            .with_for_update()
        )
        if controller_row is None:
            raise HistoricalDispositionError("controller_unavailable")
        _validate_controller_projection(store, controller_row)
        # Ordinary worker context locks authz before merge policy.
        authz = _active_authz_policy(store, session)
        policy = _active_merge_policy(store, session)
        _validate_authority(
            request,
            policy=policy,
            authz=authz,
            expected_policy=authority.policy,
            expected_authz=authority.authz,
        )
        _ensure_no_successor(store, session, request)
        snapshot = _read_snapshot_locked(store, session, request)
        session.rollback()
        return snapshot


def historical_controller_key(request: HistoricalDispositionRequest) -> str:
    from control_plane.contracts.merge_train_controller_state import (
        build_merge_train_controller_key,
    )

    return build_merge_train_controller_key(
        repository=request.repository, base_branch=request.base_branch
    )


def _ensure_no_successor(
    store: PostgresRecordStore, session: Any, request: HistoricalDispositionRequest
) -> None:
    from control_plane.storage.postgres import (
        LaunchplaneMergeTrainBatchLandingPlanRow,
        LaunchplaneMergeTrainControllerStateRow,
    )

    successor_id = historical_disposition_record_id(request)
    row = session.get(LaunchplaneMergeTrainBatchLandingPlanRow, successor_id)
    if row is None:
        return
    try:
        successor = store._read_payload(
            model_type=MergeTrainBatchLandingPlanRecord, payload=row.payload
        )
    except (TypeError, ValueError) as error:
        raise HistoricalDispositionError("successor_conflict", record_id=successor_id) from error
    evidence = successor.historical_completion
    selector = request.selector
    predecessor = session.get(
        LaunchplaneMergeTrainBatchLandingPlanRow, selector.expected_active_record_id
    )
    controller = session.get(
        LaunchplaneMergeTrainControllerStateRow, historical_controller_key(request)
    )
    if (
        successor.record_id != successor_id
        or successor.schema_version != 2
        or evidence is None
        or evidence.schema_version != 2
        or evidence.disposition_authorization is None
        or row.status != successor.status
        or row.source != successor.source
        or row.updated_at != successor.updated_at
        or row.repository != successor.landing_plan.repository
        or row.base_branch != successor.landing_plan.base_branch
        or row.batch_id != successor.landing_plan.batch_id
        or row.plan_id != successor.landing_plan.plan_id
        or predecessor is None
        or predecessor.status != "superseded"
        or predecessor.payload.get("status") != "superseded"
        or controller is None
        or controller.payload.get("active_record_id") == selector.expected_active_record_id
        or successor.status not in {"active", "superseded"}
        or successor.landing_plan.repository != request.repository
        or successor.landing_plan.base_branch != request.base_branch
        or evidence.source_landing_plan_record_id != selector.expected_active_record_id
        or evidence.controller_key != historical_controller_key(request)
        or evidence.repository != request.repository
        or evidence.base_branch != request.base_branch
        or evidence.landing_plan_id != selector.expected_landing_plan_id
        or evidence.source_landing_plan_sha256 != successor.landing_plan.landing_plan_sha256
        or evidence.batch_id != successor.landing_plan.batch_id
        or evidence.policy_sha256 != selector.expected_policy_sha256
        or evidence.candidate_sha != selector.expected_effect_sha
        or evidence.candidate_sha256 != successor.landing_plan.candidate_sha256
        or evidence.policy_key != successor.landing_plan.policy_key
        or tuple(
            (
                entry.position,
                entry.pull_request_number,
                entry.expected_head_sha,
                entry.expected_head_tree_sha,
            )
            for entry in evidence.provider_evidence.entries
        )
        != tuple(
            (
                entry.position,
                entry.pull_request_number,
                entry.expected_head_sha,
                entry.expected_head_tree_sha,
            )
            for entry in selector.expected_entries
        )
    ):
        raise HistoricalDispositionError("successor_conflict", record_id=successor_id)
    raise HistoricalDispositionError("already_recorded", record_id=successor_id)


def _validate_controller_projection(
    store: PostgresRecordStore, row: Any
) -> MergeTrainControllerStateRecord:
    record = store._read_payload(model_type=MergeTrainControllerStateRecord, payload=row.payload)
    if (
        row.controller_key != record.controller_key
        or row.repository != record.repository
        or row.base_branch != record.base_branch
        or row.status != record.status
        or row.policy_key != record.policy_key
        or row.policy_sha256 != record.policy_sha256
        or row.updated_at != record.updated_at
        or row.lease_owner != record.lease_owner
        or row.lease_expires_at != record.lease_expires_at
        or row.active_action != record.active_action
        or row.active_phase != record.active_phase
    ):
        raise HistoricalDispositionError("controller_binding_changed")
    return record


def _all_target_admissions(
    session: Any,
    request: HistoricalDispositionRequest,
    pull_requests: Iterable[int],
) -> None:
    from control_plane.storage.postgres import LaunchplaneMergeAdmissionRow

    numbers = tuple(pull_requests)
    if not numbers:
        raise HistoricalDispositionError("selector_mismatch")
    rows = session.scalars(
        select(LaunchplaneMergeAdmissionRow.admission_id).where(
            LaunchplaneMergeAdmissionRow.repository == request.repository,
            LaunchplaneMergeAdmissionRow.base_branch == request.base_branch,
            LaunchplaneMergeAdmissionRow.pull_request_number.in_(numbers),
        )
    )
    if next(iter(rows), None) is not None:
        raise HistoricalDispositionError("admission_present")


def finalize_merge_train_historical_completion(
    store: PostgresRecordStore,
    *,
    request: HistoricalDispositionRequest,
    authority: HistoricalDispositionAuthority,
    snapshot: HistoricalCompletionSnapshot,
    provider_evidence: MergeTrainHistoricalCompletionProviderEvidence,
    trace_id: str,
) -> LaunchplaneIdempotencyRecord:
    if store.database_dialect_name != "postgresql":
        raise HistoricalDispositionError("database_storage_required", status_code=503)
    if not request.idempotency_key:
        raise HistoricalDispositionError("idempotency_key_required", status_code=400)
    controller_key = historical_controller_key(request)
    if (
        snapshot.controller.controller_key != controller_key
        or snapshot.controller.repository != request.repository
        or snapshot.controller.base_branch != request.base_branch
    ):
        raise HistoricalDispositionError("selector_mismatch")
    from control_plane.storage.postgres import (
        LaunchplaneMergeTrainBatchLandingPlanRow,
        LaunchplaneMergeTrainControllerStateRow,
    )

    with store._session_factory() as session:
        store._begin_serialized_write(session)
        _lock_timeout(store, session)
        store._advisory_lock_merge_train_controller(session, controller_key)
        controller_row = session.scalar(
            select(LaunchplaneMergeTrainControllerStateRow)
            .where(LaunchplaneMergeTrainControllerStateRow.controller_key == controller_key)
            .with_for_update()
        )
        if controller_row is None:
            raise HistoricalDispositionError("controller_unavailable")
        _validate_controller_projection(store, controller_row)
        # Ordinary worker context locks authz before merge policy.
        authz = _active_authz_policy(store, session)
        policy = _active_merge_policy(store, session)
        _validate_authority(
            request,
            policy=policy,
            authz=authz,
            expected_policy=authority.policy,
            expected_authz=authority.authz,
        )
        if request.idempotency_key:
            idem_row = session.scalar(
                store._idempotency_statement(
                    scope=request.scope,
                    route_path=request.route_path,
                    idempotency_key=request.idempotency_key,
                    for_update=True,
                )
            )
            if idem_row is not None:
                replay = store._read_payload(
                    model_type=LaunchplaneIdempotencyRecord, payload=idem_row.payload
                )
                _validate_replay(
                    request,
                    replay,
                    _service_authz(request, policy),
                )
                if replay.state == "completed":
                    session.rollback()
                    return replay
        _ensure_no_successor(store, session, request)
        current = _read_snapshot_locked(store, session, request)
        if current != snapshot:
            raise HistoricalDispositionError("store_state_changed")
        bundle = build_historical_disposition(
            request=request,
            authority=HistoricalDispositionAuthority(policy=policy, authz=authz),
            snapshot=snapshot,
            provider_evidence=provider_evidence,
            recorded_at=store._database_mutation_timestamp(session),
            trace_id=trace_id,
        )
        predecessor_row = session.get(
            LaunchplaneMergeTrainBatchLandingPlanRow,
            snapshot.landing.record_id,
            with_for_update=True,
        )
        if predecessor_row is None or predecessor_row.payload != store._payload_dict(
            snapshot.landing
        ):
            raise HistoricalDispositionError("store_state_changed")
        if (
            predecessor_row.status != "active"
            or predecessor_row.source != snapshot.landing.source
            or predecessor_row.updated_at != snapshot.landing.updated_at
            or predecessor_row.repository != snapshot.landing.landing_plan.repository
            or predecessor_row.base_branch != snapshot.landing.landing_plan.base_branch
            or predecessor_row.batch_id != snapshot.landing.landing_plan.batch_id
            or predecessor_row.plan_id != snapshot.landing.landing_plan.plan_id
        ):
            raise HistoricalDispositionError("landing_plan_binding_changed")
        session.add(
            LaunchplaneMergeTrainBatchLandingPlanRow(
                record_id=bundle.successor.record_id,
                status=bundle.successor.status,
                source=bundle.successor.source,
                updated_at=bundle.successor.updated_at,
                repository=bundle.successor.landing_plan.repository,
                base_branch=bundle.successor.landing_plan.base_branch,
                batch_id=bundle.successor.landing_plan.batch_id,
                plan_id=bundle.successor.landing_plan.plan_id,
                payload=store._payload_dict(bundle.successor),
            )
        )
        predecessor_row.status = "superseded"
        predecessor_row.payload = {
            **cast(dict[str, object], predecessor_row.payload),
            "status": "superseded",
        }
        store._sync_merge_train_controller_state_row(controller_row, bundle.controller)
        if idem_row is not None:
            raise HistoricalDispositionError("idempotency_key_reused")
        session.add(store._idempotency_row(bundle.idempotency_record))
        session.commit()
        return bundle.idempotency_record
