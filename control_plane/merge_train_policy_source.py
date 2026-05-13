from typing import Protocol, cast

from control_plane.contracts.merge_train_policy import MergeTrainPolicyRecord


class MergeTrainPolicyStoreMissingError(RuntimeError):
    pass


class _MergeTrainPolicyRecordStore(Protocol):
    def list_merge_train_policy_records(
        self, *, status: str = "", limit: int | None = None
    ) -> tuple[MergeTrainPolicyRecord, ...]: ...


def resolve_merge_train_policy_record(record_store: object) -> MergeTrainPolicyRecord:
    list_records = getattr(record_store, "list_merge_train_policy_records", None)
    if not callable(list_records):
        raise MergeTrainPolicyStoreMissingError("merge train policy store is unavailable")
    store = cast(_MergeTrainPolicyRecordStore, record_store)
    records = store.list_merge_train_policy_records(status="active", limit=1)
    if records:
        return records[0]
    raise MergeTrainPolicyStoreMissingError("active merge train policy record is missing")
