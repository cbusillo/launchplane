"""Hold a repository's merge train while a Client-accepted release built from it runs.

Landing a change moves the product's testing lane, and a release whose testing
candidate changes stops before its next step. So while Launchplane runs a release
the Client accepted, the repositories that release was built from get no new
candidate and no new landing. The hold is read from Launchplane's own release
records on every pass; nobody sets or clears it.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from control_plane.client_release import accepted_release_can_start, read_client_release_run
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.release_review import ReleaseReviewDecisionRecord
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.release_review import (
    ReleaseEvidenceUnavailable,
    ReleaseReviewStore,
    release_version,
)


@dataclass(frozen=True)
class MergeTrainReleaseHold:
    product: str

    def blocking_reason(self) -> dict[str, str]:
        return {
            "code": "product_release_in_progress",
            "message": f"Landings into this repository wait while the Client-accepted release "
            f"of {self.product} runs.",
        }


MergeTrainReleaseHoldReader = Callable[[str], MergeTrainReleaseHold | None]


class MergeTrainReleaseHoldStore(ReleaseReviewStore, Protocol):
    def list_product_profile_records(
        self, *, driver_id: str = ""
    ) -> tuple[LaunchplaneProductProfileRecord, ...]: ...


def merge_train_release_hold(
    store: MergeTrainReleaseHoldStore, repository: str
) -> MergeTrainReleaseHold | None:
    """Return the in-flight release holding ``repository``, if there is one."""

    for profile in store.list_product_profile_records():
        decisions = store.list_release_review_decision_records(product=profile.product, limit=1)
        if not decisions or decisions[0].decision != "accepted":
            continue
        decision = decisions[0]
        if repository.casefold() not in _release_repositories(decision):
            continue
        if _release_in_flight(store, profile, decision):
            return MergeTrainReleaseHold(product=profile.product)
    return None


def merge_train_release_hold_reader(store: object) -> MergeTrainReleaseHoldReader | None:
    """Read holds from the service store. Client releases run only on that store."""

    if not isinstance(store, PostgresRecordStore):
        return None

    def read(repository: str) -> MergeTrainReleaseHold | None:
        return merge_train_release_hold(store, repository)

    return read


def _release_repositories(decision: ReleaseReviewDecisionRecord) -> set[str]:
    checklist = decision.checklist
    return {checklist.repository.casefold()} | {
        source.repository.casefold() for source in checklist.shared_sources
    }


def _release_in_flight(
    store: MergeTrainReleaseHoldStore,
    profile: LaunchplaneProductProfileRecord,
    decision: ReleaseReviewDecisionRecord,
) -> bool:
    run = read_client_release_run(store=store, profile=profile, decision=decision)
    if run is None or run.state in {"passed", "stopped"}:
        return False
    if run.state == "running":
        return True
    # A waiting release continues only while its decision still starts a release
    # and testing still holds the accepted candidate; otherwise it can never run.
    if not accepted_release_can_start(store, decision):
        return False
    try:
        candidate = release_version(store=store, profile=profile, instance="testing")
    except ReleaseEvidenceUnavailable:
        return False
    return candidate == decision.checklist.candidate
