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
from control_plane.service_deploy_drain import read_status
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.release_review_shared import repository_key
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
        if repository_key(repository) not in _release_repositories(store, decision):
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


def _release_repositories(
    store: MergeTrainReleaseHoldStore, decision: ReleaseReviewDecisionRecord
) -> set[str]:
    """The product repository and every source the accepted candidate was built from.

    The checklist names only shared sources that changed, so the candidate
    artifact supplies the unchanged ones too.
    """

    checklist = decision.checklist
    repositories = {repository_key(checklist.repository)} | {
        repository_key(source.repository) for source in checklist.shared_sources
    }
    try:
        artifact = store.read_artifact_manifest(checklist.candidate.artifact_id)
    except FileNotFoundError:
        return repositories
    return (
        repositories
        | {repository_key(source.repository) for source in artifact.addon_sources}
        | {repository_key(selector.repository) for selector in artifact.addon_selectors}
    )


def _release_in_flight(
    store: MergeTrainReleaseHoldStore,
    profile: LaunchplaneProductProfileRecord,
    decision: ReleaseReviewDecisionRecord,
) -> bool:
    """Whether the release worker is running this release or will start its next step.

    This mirrors the worker's own checks, so a release it would refuse to
    continue never holds the train.
    """

    run = read_client_release_run(store=store, profile=profile, decision=decision)
    if run is None:
        return False
    # A step or its automatic recovery is changing production now.
    if any(step.status in {"pending", "running"} for step in run.steps):
        return True
    if run.state != "waiting":
        return False
    # A drain only delays the next step; any other blocker waits on an admin.
    if run.blocked_reason and not read_status(store).get("admission_paused"):
        return False
    if not accepted_release_can_start(store, decision):
        return False
    next_step = next((step for step in run.steps if step.status == "not_started"), None)
    if next_step is None:
        return False
    checklist = decision.checklist
    try:
        candidate = release_version(store=store, profile=profile, instance="testing")
        production = release_version(store=store, profile=profile, instance="prod")
    except ReleaseEvidenceUnavailable:
        return False
    # A rollback starts from the promoted candidate; a backup or promotion needs
    # production on the reviewed version, and both need testing on the candidate.
    expected_production = (
        checklist.candidate if next_step.kind == "rollback" else checklist.production
    )
    return candidate == checklist.candidate and production == expected_production
