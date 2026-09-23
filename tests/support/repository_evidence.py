from pathlib import Path

from control_plane.change_impact_service import ChangeImpactRepositoryEvidenceProvider
from control_plane.contracts.change_impact import (
    ChangeImpactAuthorshipEvidence,
    ChangeImpactBaseEvidence,
    ChangeImpactChangedFileEvidence,
    ChangeImpactRepositoryEvidence,
    ChangeImpactTarget,
    ChangeImpactTargetReference,
)
from control_plane.service_auth import GitHubHumanIdentity
from control_plane.storage.filesystem import FilesystemRecordStore

REPOSITORY_ID = "1001"
REPOSITORY_OWNER_ID = "2001"
REPOSITORY = "example/web"
OWNER_GITHUB_ID = 3001
CONTRIBUTOR_GITHUB_ID = 4001
HEAD_SHA = "a" * 40
TREE_SHA = "b" * 40
BASE_SHA = "e" * 40
BASE_REF = "main"


class _EvidenceProvider(ChangeImpactRepositoryEvidenceProvider):
    def __init__(self, evidence: ChangeImpactRepositoryEvidence) -> None:
        self.evidence = evidence

    def resolve(self, target: ChangeImpactTargetReference) -> ChangeImpactRepositoryEvidence:
        return self.evidence


def _store(root: Path) -> FilesystemRecordStore:
    return FilesystemRecordStore(root)


def _human(
    github_id: int = OWNER_GITHUB_ID,
    *,
    login: str = "owner",
) -> GitHubHumanIdentity:
    return GitHubHumanIdentity(
        login=login,
        github_id=github_id,
        name="Owner",
        email="",
        organizations=frozenset(),
        teams=frozenset(),
        role="admin",
    )


def _repository_evidence(
    *,
    path: str = "src/runtime/app.py",
    head: str = HEAD_SHA,
    base_sha: str = BASE_SHA,
    authorship: ChangeImpactAuthorshipEvidence | None = None,
) -> ChangeImpactRepositoryEvidence:
    return ChangeImpactRepositoryEvidence(
        target=ChangeImpactTarget(
            repository_id=REPOSITORY_ID,
            repository_owner_id=REPOSITORY_OWNER_ID,
            repository=REPOSITORY,
            pull_request_number=2022,
            head_sha=head,
            tree_sha=TREE_SHA,
        ),
        changed_files=(ChangeImpactChangedFileEvidence(path=path),),
        base=ChangeImpactBaseEvidence(base_ref=BASE_REF, base_sha=base_sha),
        authorship=authorship
        or ChangeImpactAuthorshipEvidence(
            resolution="resolved",
            contributor_github_ids=(CONTRIBUTOR_GITHUB_ID,),
            commit_count=1,
        ),
    )
