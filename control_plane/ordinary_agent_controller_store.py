"""Translate legacy controller coordination into joined ordinary-job operations."""

from dataclasses import dataclass, field
from typing import Protocol

from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidateRecord,
    MergeTrainBatchLandingPlanRecord,
)
from control_plane.contracts.merge_train_stack_collapse import MergeTrainStackCollapsePlanRecord
from control_plane.contracts.merge_train_controller_state import (
    MergeTrainControllerLeaseLostError,
    MergeTrainControllerStateRecord,
)
from control_plane.contracts.ordinary_agent_effect import (
    OrdinaryAgentClaimedJob,
    OrdinaryAgentControllerFence,
    OrdinaryAgentControllerStore,
    OrdinaryAgentProgressRecord,
)
from control_plane.contracts.ordinary_agent_session_lifecycle import OrdinaryAgentJobBinding
from control_plane.ordinary_agent_noop_route import OrdinaryNoOpLandingRoute
from control_plane.merge_train_structural_provenance import (
    ordinary_candidate_is_exact_landing_dependency,
)


class OrdinaryAgentControllerReadStore(Protocol):
    def list_merge_train_controller_state_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[MergeTrainControllerStateRecord, ...]: ...


@dataclass
class OrdinaryAgentControllerAdapter:
    """Reads are planning evidence; only the joined store can grant or renew work.

    An ordinary successor write advances active_record_id atomically. Legacy
    callbacks may still carry the prior wrapper, so checkpointing rereads that
    authoritative pointer instead of restoring stale progress.
    """

    claimed: OrdinaryAgentClaimedJob
    store: OrdinaryAgentControllerStore
    reader: OrdinaryAgentControllerReadStore
    _acquired_fence: OrdinaryAgentControllerFence | None = field(default=None, init=False)
    _yield_confirmed: bool = field(default=False, init=False)

    @property
    def controller_acquired(self) -> bool:
        return self._acquired_fence is not None

    @property
    def yield_confirmed(self) -> bool:
        """Whether this adapter's latest acquired controller was durably yielded."""
        return self._yield_confirmed

    @property
    def acquired_fence(self) -> OrdinaryAgentControllerFence:
        if self._acquired_fence is None:
            raise MergeTrainControllerLeaseLostError("ordinary controller not acquired")
        return self._acquired_fence

    @property
    def binding(self) -> OrdinaryAgentJobBinding:
        request = self.claimed.request
        return OrdinaryAgentJobBinding(
            request_id=request.request_id,
            scope_sha256=request.scope_sha256,
            binding_revision=request.binding_revision,
        )

    def _require_target(self, repository: str, base_branch: str) -> None:
        target = self.claimed.request.target
        if repository.lower() != target.repository.lower() or base_branch != target.base_branch:
            raise MergeTrainControllerLeaseLostError("ordinary controller target mismatch")

    def list_merge_train_controller_state_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[MergeTrainControllerStateRecord, ...]:
        self._require_target(repository, base_branch)
        records = self.reader.list_merge_train_controller_state_records(
            repository=repository, base_branch=base_branch, status=status
        )
        matching = tuple(
            record
            for record in records
            if record.ordinary_job_binding == self.binding
            and record.repository.lower() == repository.lower()
            and record.base_branch == base_branch
        )
        return matching if limit is None else matching[:limit]

    def acquire_merge_train_controller_state_record(
        self,
        *,
        repository: str,
        base_branch: str,
        policy_key: str,
        policy_sha256: str,
        lease_owner: str,
        lease_seconds: int,
        initial_active_action: str,
        initial_active_phase: str,
        adoptable_active_actions: tuple[str, ...],
    ) -> MergeTrainControllerStateRecord:
        self._require_target(repository, base_branch)
        # The generic trace owner is deliberately not forwarded as authority.
        record = self.store.acquire_ordinary_merge_train_controller_state_record(
            claim_fence=self.claimed.claim_fence,
            expected_binding_revision=self.claimed.request.binding_revision,
            policy_key=policy_key,
            policy_sha256=policy_sha256,
            lease_seconds=lease_seconds,
            initial_active_action=initial_active_action,
            initial_active_phase=initial_active_phase,
            adoptable_active_actions=adoptable_active_actions,
        )
        self._acquired_fence = OrdinaryAgentControllerFence(
            controller_key=record.controller_key,
            lease_owner=record.lease_owner,
            lease_acquired_at=record.lease_acquired_at,
        )
        self._yield_confirmed = False
        return record

    def compare_and_set_merge_train_controller_state_record(
        self,
        *,
        record: MergeTrainControllerStateRecord,
        expected_lease_owner: str,
        expected_lease_acquired_at: str,
        lease_seconds: int,
    ) -> MergeTrainControllerStateRecord:
        self._require_target(record.repository, record.base_branch)
        if record.ordinary_job_binding != self.binding:
            raise MergeTrainControllerLeaseLostError("ordinary controller binding mismatch")
        fence = OrdinaryAgentControllerFence(
            controller_key=record.controller_key,
            lease_owner=expected_lease_owner,
            lease_acquired_at=expected_lease_acquired_at,
        )
        if not record.lease_owner and not record.lease_acquired_at:
            # Release remains possible after cancellation. The store preserves
            # actual effect history; caller-provided error prose is not persisted.
            yielded = self.store.yield_ordinary_merge_train_controller_state_record(
                request_id=self.claimed.request.request_id,
                expected_binding_revision=self.claimed.request.binding_revision,
                controller_fence=fence,
            )
            self._yield_confirmed = fence == self._acquired_fence
            return yielded
        records = self.list_merge_train_controller_state_records(
            repository=record.repository, base_branch=record.base_branch, limit=1
        )
        if not records:
            raise MergeTrainControllerLeaseLostError("ordinary controller state missing")
        current = records[0]
        if (
            current.controller_key != fence.controller_key
            or current.lease_owner != fence.lease_owner
            or current.lease_acquired_at != fence.lease_acquired_at
        ):
            raise MergeTrainControllerLeaseLostError("ordinary controller fence changed")
        return self.store.compare_and_set_ordinary_merge_train_controller_state_record(
            request_id=self.claimed.request.request_id,
            expected_binding_revision=self.claimed.request.binding_revision,
            controller_fence=fence,
            record=record.model_copy(update={"active_record_id": current.active_record_id}),
            lease_seconds=lease_seconds,
        )

    def release_terminal_history(self) -> MergeTrainControllerStateRecord | None:
        """Release an expired worker's owned controller without acquiring authority."""
        fence = self.claimed.controller_fence
        if fence is None:
            return None
        return self.store.yield_ordinary_merge_train_controller_state_record(
            request_id=self.claimed.request.request_id,
            expected_binding_revision=self.claimed.request.binding_revision,
            controller_fence=fence,
        )


class OrdinaryAgentProgressReadStore(Protocol):
    def list_ordinary_merge_train_batch_candidate_dependencies(
        self,
        *,
        landing_plan_record: MergeTrainBatchLandingPlanRecord,
    ) -> tuple[MergeTrainBatchCandidateRecord, ...]: ...

    def list_merge_train_batch_candidate_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[MergeTrainBatchCandidateRecord, ...]: ...

    def list_merge_train_batch_landing_plan_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[MergeTrainBatchLandingPlanRecord, ...]: ...

    def list_merge_train_stack_collapse_plan_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[MergeTrainStackCollapsePlanRecord, ...]: ...


@dataclass
class OrdinaryAgentProgressAdapter:
    controller: OrdinaryAgentControllerAdapter
    reader: OrdinaryAgentProgressReadStore
    no_op_route: OrdinaryNoOpLandingRoute | None = None

    def _filter[Record: OrdinaryAgentProgressRecord](
        self,
        records: tuple[Record, ...],
        *,
        repository: str,
        base_branch: str,
        status: str,
        limit: int | None,
    ) -> tuple[Record, ...]:
        self.controller._require_target(repository, base_branch)
        result: list[Record] = []
        for record in records:
            if record.ordinary_job_binding != self.controller.binding:
                continue
            if isinstance(record, MergeTrainBatchCandidateRecord):
                repository_value, branch_value = (
                    record.candidate.repository,
                    record.candidate.base_branch,
                )
            elif isinstance(record, MergeTrainBatchLandingPlanRecord):
                repository_value, branch_value = (
                    record.landing_plan.repository,
                    record.landing_plan.base_branch,
                )
            else:
                repository_value, branch_value = record.plan.repository, record.plan.base_branch
            if (
                repository_value.lower() == repository.lower()
                and branch_value == base_branch
                and (not status or record.status == status)
            ):
                result.append(record)
        return tuple(result if limit is None else result[:limit])

    def list_merge_train_batch_candidate_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[MergeTrainBatchCandidateRecord, ...]:
        self.controller._require_target(repository, base_branch)
        return self._filter(
            self.reader.list_merge_train_batch_candidate_records(
                repository=repository, base_branch=base_branch, status=status
            ),
            repository=repository,
            base_branch=base_branch,
            status=status,
            limit=limit,
        )

    def list_ordinary_merge_train_batch_candidate_dependencies(
        self,
        *,
        landing_plan_record: MergeTrainBatchLandingPlanRecord,
    ) -> tuple[MergeTrainBatchCandidateRecord, ...]:
        plan = landing_plan_record.landing_plan
        self.controller._require_target(plan.repository, plan.base_branch)
        if landing_plan_record.ordinary_job_binding != self.controller.binding:
            raise MergeTrainControllerLeaseLostError("ordinary progress binding mismatch")
        records = self.reader.list_ordinary_merge_train_batch_candidate_dependencies(
            landing_plan_record=landing_plan_record,
        )
        return tuple(
            record
            for record in records
            if ordinary_candidate_is_exact_landing_dependency(
                candidate_record=record,
                landing_plan_record=landing_plan_record,
            )
        )

    def list_merge_train_batch_landing_plan_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[MergeTrainBatchLandingPlanRecord, ...]:
        self.controller._require_target(repository, base_branch)
        return self._filter(
            self.reader.list_merge_train_batch_landing_plan_records(
                repository=repository, base_branch=base_branch, status=status
            ),
            repository=repository,
            base_branch=base_branch,
            status=status,
            limit=limit,
        )

    def list_merge_train_stack_collapse_plan_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[MergeTrainStackCollapsePlanRecord, ...]:
        self.controller._require_target(repository, base_branch)
        return self._filter(
            self.reader.list_merge_train_stack_collapse_plan_records(
                repository=repository, base_branch=base_branch, status=status
            ),
            repository=repository,
            base_branch=base_branch,
            status=status,
            limit=limit,
        )

    def _write(self, record: OrdinaryAgentProgressRecord) -> OrdinaryAgentProgressRecord:
        if record.ordinary_job_binding != self.controller.binding or record.status != "active":
            raise MergeTrainControllerLeaseLostError("ordinary progress binding mismatch")
        target = self.controller.claimed.request.target
        records = self.controller.list_merge_train_controller_state_records(
            repository=target.repository, base_branch=target.base_branch, limit=1
        )
        if not records:
            raise MergeTrainControllerLeaseLostError("ordinary progress controller missing")
        current = records[0]
        fence = self.controller.acquired_fence
        if (
            current.controller_key != fence.controller_key
            or current.lease_owner != fence.lease_owner
            or current.lease_acquired_at != fence.lease_acquired_at
        ):
            raise MergeTrainControllerLeaseLostError("ordinary progress fence changed")
        # This is the fence acquired for this claim, not a new authority lookup.
        # Joined storage checks its generation, current authority and predecessor.
        if self.no_op_route is not None and self.no_op_route.armed:
            return self.no_op_route.finalize(
                record=record,
                controller_fence=fence,
                predecessor_record_id=current.active_record_id,
            )
        return self.controller.store.write_ordinary_merge_train_record(
            request_id=self.controller.claimed.request.request_id,
            expected_binding_revision=self.controller.claimed.request.binding_revision,
            controller_fence=fence,
            record=record,
            expected_predecessor_record_id=current.active_record_id or None,
        )

    def write_merge_train_batch_candidate_record(
        self, record: MergeTrainBatchCandidateRecord
    ) -> object:
        return self._write(record)

    def write_merge_train_batch_landing_plan_record(
        self, record: MergeTrainBatchLandingPlanRecord
    ) -> object:
        return self._write(record)

    def write_merge_train_stack_collapse_plan_record(
        self, record: MergeTrainStackCollapsePlanRecord
    ) -> object:
        return self._write(record)
