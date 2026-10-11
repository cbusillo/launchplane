"""Resolve Client release inputs from product and artifact records, never a catalog."""

from typing import Protocol

from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.release_review_shared import repository_key


class ClientReleaseRepositoryStore(Protocol):
    def list_product_profile_records(self) -> tuple[LaunchplaneProductProfileRecord, ...]: ...
    def read_release_tuple_record(
        self, *, context_name: str, channel_name: str
    ) -> ReleaseTupleRecord: ...
    def read_artifact_manifest(self, artifact_id: str) -> ArtifactIdentityManifest: ...


def client_release_profiles(
    *, store: ClientReleaseRepositoryStore, repository: str
) -> tuple[LaunchplaneProductProfileRecord, ...]:
    """Include shared addon inputs pinned in either current release lane."""
    key = repository_key(repository)
    result = []
    for profile in store.list_product_profile_records():
        if not profile.is_active or not profile.owner.is_set:
            continue
        if repository_key(profile.repository) == key:
            result.append(profile)
            continue
        if profile.driver_id != "odoo":
            continue
        for lane in profile.lanes:
            if lane.instance not in {"prod", "testing"}:
                continue
            try:
                release = store.read_release_tuple_record(
                    context_name=lane.context, channel_name=lane.instance
                )
                artifact = store.read_artifact_manifest(release.artifact_id)
            except FileNotFoundError:
                continue  # No recorded release input for this lane yet.
            if any(repository_key(source.repository) == key for source in artifact.addon_sources):
                result.append(profile)
                break
    return tuple(result)


def recorded_train_candidate(
    *, store: object, repository: str, head: object, base_branch: str = ""
) -> bool:
    """A branch name alone cannot suppress Client review of an ordinary change."""
    if not isinstance(head, dict):
        return False
    ref = str(head.get("ref") or "")
    source = head.get("repo")
    if not ref.startswith("launchplane/train/") or not isinstance(source, dict):
        return False
    if repository_key(str(source.get("full_name") or "")) != repository_key(repository):
        return False
    reader = getattr(store, "list_merge_train_batch_candidate_records", None)
    if not callable(reader):
        return False
    from typing import cast
    from control_plane.contracts.merge_train_batch import MergeTrainBatchCandidateRecord

    records = cast(
        tuple[MergeTrainBatchCandidateRecord, ...], reader(repository=repository.lower())
    )
    return any(
        record.candidate.candidate_ref.removeprefix("refs/heads/") == ref
        and record.candidate.candidate_sha == head.get("sha")
        and record.candidate.base_branch == base_branch.strip()
        for record in records
    )
