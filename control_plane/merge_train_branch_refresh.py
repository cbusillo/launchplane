"""The merge train's record of the branch refreshes it asked the provider for."""

from datetime import datetime
from typing import Protocol, cast

from control_plane.contracts.merge_train_branch_refresh_record import (
    MergeTrainBranchRefreshRecord,
    build_merge_train_branch_refresh_record,
)
from control_plane.merge_admission import MergeAdmissionDeniedError
from control_plane.merge_train_github import (
    MergeTrainBranchRefreshRecorder,
    MergeTrainBranchRefreshReadStore as MergeTrainBranchRefreshReadStore,
)


class MergeTrainBranchRefreshWriteStore(Protocol):
    def write_merge_train_branch_refresh_record(
        self, record: MergeTrainBranchRefreshRecord
    ) -> object: ...


def optional_merge_train_branch_refresh_store(
    record_store: object,
) -> MergeTrainBranchRefreshWriteStore | None:
    if callable(getattr(record_store, "write_merge_train_branch_refresh_record", None)):
        return cast(MergeTrainBranchRefreshWriteStore, record_store)
    return None


def optional_merge_train_branch_refresh_read_store(
    record_store: object,
) -> MergeTrainBranchRefreshReadStore | None:
    if callable(getattr(record_store, "list_merge_train_branch_refresh_records", None)):
        return cast(MergeTrainBranchRefreshReadStore, record_store)
    return None


def require_merge_train_client_review_read_store(
    record_store: object, *, route: str
) -> MergeTrainBranchRefreshReadStore:
    """Return the store that names Client-review labels, or refuse the route."""
    review_store = optional_merge_train_branch_refresh_read_store(record_store)
    if review_store is None or not callable(
        getattr(review_store, "list_product_profile_records", None)
    ):
        raise MergeAdmissionDeniedError(
            f"{route} requires a readable Client-review profile store.",
            reason_code="client_review_profiles_unavailable",
        )
    return review_store


def merge_train_branch_refresh_recorder(
    *, store: MergeTrainBranchRefreshWriteStore, base_branch: str, trace_id: str
) -> MergeTrainBranchRefreshRecorder:
    """Record each refresh one controller pass makes on one base branch."""

    def record(
        *,
        repository: str,
        pull_request_number: int,
        expected_head_sha: str,
        result_head_sha: str,
        merged_base_sha: str,
        requested_at: datetime,
    ) -> None:
        store.write_merge_train_branch_refresh_record(
            build_merge_train_branch_refresh_record(
                repository=repository,
                base_branch=base_branch,
                pull_request_number=pull_request_number,
                expected_head_sha=expected_head_sha,
                result_head_sha=result_head_sha,
                merged_base_sha=merged_base_sha,
                requested_at=requested_at,
                trace_id=trace_id,
            )
        )

    return record
