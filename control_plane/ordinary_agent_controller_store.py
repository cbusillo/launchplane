"""Translate legacy controller coordination into joined ordinary-job operations."""

from dataclasses import dataclass
from typing import Protocol

from control_plane.contracts.merge_train_controller_state import (
    MergeTrainControllerLeaseLostError,
    MergeTrainControllerStateRecord,
)
from control_plane.contracts.ordinary_agent_effect import (
    OrdinaryAgentClaimedJob,
    OrdinaryAgentControllerFence,
    OrdinaryAgentControllerStore,
)
from control_plane.contracts.ordinary_agent_session_lifecycle import OrdinaryAgentJobBinding


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
        return self.store.acquire_ordinary_merge_train_controller_state_record(
            claim_fence=self.claimed.claim_fence,
            expected_binding_revision=self.claimed.request.binding_revision,
            policy_key=policy_key,
            policy_sha256=policy_sha256,
            lease_seconds=lease_seconds,
            initial_active_action=initial_active_action,
            initial_active_phase=initial_active_phase,
            adoptable_active_actions=adoptable_active_actions,
        )

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
            return self.store.yield_ordinary_merge_train_controller_state_record(
                request_id=self.claimed.request.request_id,
                expected_binding_revision=self.claimed.request.binding_revision,
                controller_fence=fence,
            )
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
