"""Job-bound admission history for the shared guard; no legacy admission grant."""

from dataclasses import dataclass

from control_plane.contracts.merge_admission_record import (
    MergeAdmissionRecord,
    MergeLandingOutcomeRecord,
)
from control_plane.merge_admission import MergeAdmissionDeniedError, MergeAdmissionRecordStore
from control_plane.ordinary_agent_controller_store import (
    OrdinaryAgentControllerAdapter,
    OrdinaryAgentProgressAdapter,
)


@dataclass
class OrdinaryAgentAdmissionAdapter:
    controller: OrdinaryAgentControllerAdapter
    progress: OrdinaryAgentProgressAdapter
    reader: MergeAdmissionRecordStore

    def _plan_ids(self) -> frozenset[tuple[str, str]]:
        target = self.controller.claimed.request.target
        return frozenset(
            (wrapper.record_id, wrapper.landing_plan.plan_id)
            for wrapper in self.progress.list_merge_train_batch_landing_plan_records(
                repository=target.repository, base_branch=target.base_branch
            )
        )

    def _matches(self, record: MergeAdmissionRecord, plan_ids: frozenset[tuple[str, str]]) -> bool:
        target = self.controller.claimed.request.target
        return (
            record.repository.lower() == target.repository.lower()
            and record.base_branch == target.base_branch
            and (record.landing_plan_record_id, record.landing_plan_id) in plan_ids
        )

    def create_merge_admission_record_if_absent(
        self, record: MergeAdmissionRecord
    ) -> tuple[MergeAdmissionRecord, bool]:
        raise MergeAdmissionDeniedError("Ordinary admission requires joined landing finalization.")

    def create_guarded_merge_admission_record_if_absent(
        self, record: MergeAdmissionRecord, *, admitted_at: str
    ) -> tuple[MergeAdmissionRecord, bool]:
        raise MergeAdmissionDeniedError("Ordinary admission requires joined landing finalization.")

    def read_merge_admission_record(self, admission_id: str) -> MergeAdmissionRecord:
        try:
            record = self.reader.read_merge_admission_record(admission_id)
        except FileNotFoundError:
            raise MergeAdmissionDeniedError("Ordinary admission history is unavailable.") from None
        if not self._matches(record, self._plan_ids()):
            raise MergeAdmissionDeniedError("Ordinary admission history is unavailable.")
        return record

    def list_merge_admission_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        pull_request_number: int | None = None,
        landing_plan_record_id: str = "",
        landing_plan_id: str = "",
        attempt_id: str = "",
        limit: int | None = None,
    ) -> tuple[MergeAdmissionRecord, ...]:
        target = self.controller.claimed.request.target
        self.controller._require_target(
            repository or target.repository, base_branch or target.base_branch
        )
        records = self.reader.list_merge_admission_records(
            repository=target.repository,
            base_branch=target.base_branch,
            pull_request_number=pull_request_number,
            landing_plan_record_id=landing_plan_record_id,
            landing_plan_id=landing_plan_id,
            attempt_id=attempt_id,
        )
        plan_ids = self._plan_ids()
        matching = tuple(record for record in records if self._matches(record, plan_ids))
        return matching if limit is None else matching[:limit]

    def create_merge_landing_outcome_record_if_absent(
        self, record: MergeLandingOutcomeRecord
    ) -> tuple[MergeLandingOutcomeRecord, bool]:
        self.read_merge_admission_record(record.admission_id)
        return self.controller.store.create_ordinary_merge_landing_outcome_record_if_absent(
            request_id=self.controller.claimed.request.request_id,
            expected_binding_revision=self.controller.claimed.request.binding_revision,
            record=record,
        )

    def list_merge_landing_outcome_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        pull_request_number: int | None = None,
        admission_id: str = "",
        status: str = "",
        observation_sequence: int | None = None,
        limit: int | None = None,
    ) -> tuple[MergeLandingOutcomeRecord, ...]:
        target = self.controller.claimed.request.target
        self.controller._require_target(
            repository or target.repository, base_branch or target.base_branch
        )
        admissions = {
            record.admission_id: record
            for record in self.list_merge_admission_records(
                repository=target.repository,
                base_branch=target.base_branch,
                pull_request_number=pull_request_number,
            )
        }
        if admission_id and admission_id not in admissions:
            return ()
        records = self.reader.list_merge_landing_outcome_records(
            repository=target.repository,
            base_branch=target.base_branch,
            pull_request_number=pull_request_number,
            admission_id=admission_id,
            status=status,
            observation_sequence=observation_sequence,
        )
        matching = tuple(
            record
            for record in records
            if record.admission_id in admissions
            and record.admission_binding_sha256
            == admissions[record.admission_id].admission_binding_sha256
            and record.repository.lower() == target.repository.lower()
            and record.base_branch == target.base_branch
        )
        return matching if limit is None else matching[:limit]
