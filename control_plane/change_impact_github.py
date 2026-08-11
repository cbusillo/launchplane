from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.change_impact import (
    ChangeImpactAuthorshipEvidence,
    ChangeImpactBaseEvidence,
    ChangeImpactChangeKind,
    ChangeImpactChangedFileEvidence,
    ChangeImpactRepositoryEvidence,
    ChangeImpactTarget,
    ChangeImpactTargetReference,
)


class ChangeImpactRepositoryEvidenceError(RuntimeError):
    """Raised when authoritative repository evidence cannot be resolved."""


class ChangeImpactRepositoryEvidenceStaleError(ChangeImpactRepositoryEvidenceError):
    """Raised when the pull request changes while evidence is being resolved."""


class GitHubOpenPullRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    repository: str
    pull_request_number: int = Field(ge=1)
    title: str
    url: str
    updated_at: str

    @model_validator(mode="after")
    def _normalize(self) -> "GitHubOpenPullRequest":
        object.__setattr__(self, "repository", self.repository.strip().lower())
        object.__setattr__(self, "title", self.title.strip())
        object.__setattr__(self, "url", self.url.strip())
        object.__setattr__(self, "updated_at", self.updated_at.strip())
        if not self.title or not self.url or not self.updated_at:
            raise ValueError("GitHub open pull request fields must be non-empty.")
        return self


class GitHubChangeImpactRepositoryEvidenceProvider:
    def __init__(
        self,
        *,
        control_plane_root: Path,
        github_token: Callable[..., str],
        github_api: Callable[..., object],
        token_context: str,
        max_file_pages: int = 30,
        max_commit_pages: int = 10,
    ) -> None:
        if max_file_pages < 1:
            raise ValueError("change-impact GitHub provider requires at least one file page")
        if max_commit_pages < 1:
            raise ValueError("change-impact GitHub provider requires at least one commit page")
        self._control_plane_root = control_plane_root
        self._github_token = github_token
        self._github_api = github_api
        self._token_context = token_context.strip()
        self._max_file_pages = max_file_pages
        self._max_commit_pages = max_commit_pages
        if not self._token_context:
            raise ValueError("change-impact GitHub provider requires a token context")

    def list_open_pull_requests(
        self,
        repository: str,
        *,
        limit: int,
    ) -> tuple[GitHubOpenPullRequest, ...]:
        if limit < 1 or limit > 100:
            raise ValueError("change-impact GitHub open pull request limit must be 1 through 100")
        try:
            token = self._token()
            repository_path = _repository_path(repository)
            pull_requests = _list_payload(
                self._github_api(
                    path=(
                        f"/repos/{repository_path}/pulls?state=open&sort=updated&direction=desc"
                        f"&per_page={limit}&page=1"
                    ),
                    token=token,
                ),
                "GitHub open pull requests",
            )
            return tuple(
                GitHubOpenPullRequest(
                    repository=repository,
                    pull_request_number=int(_positive_decimal(pull_request, "number")),
                    title=_required_string(pull_request, "title"),
                    url=_required_string(pull_request, "html_url"),
                    updated_at=_required_string(pull_request, "updated_at"),
                )
                for pull_request in pull_requests
            )
        except ChangeImpactRepositoryEvidenceError:
            raise
        except Exception as error:
            raise ChangeImpactRepositoryEvidenceError(
                "Launchplane could not enumerate GitHub open pull requests."
            ) from error

    def resolve(
        self,
        target: ChangeImpactTargetReference,
    ) -> ChangeImpactRepositoryEvidence:
        return self._resolve(target, max_file_pages=self._max_file_pages)

    def resolve_current_item(
        self,
        target: ChangeImpactTargetReference,
    ) -> ChangeImpactRepositoryEvidence:
        return self._resolve(target, max_file_pages=min(self._max_file_pages, 5))

    def _resolve(
        self,
        target: ChangeImpactTargetReference,
        *,
        max_file_pages: int,
    ) -> ChangeImpactRepositoryEvidence:
        try:
            token = self._token()
            repository_path = _repository_path(target.repository)
            repository = _object_payload(
                self._github_api(path=f"/repos/{repository_path}", token=token),
                "GitHub repository",
            )
            canonical_repository = _required_string(repository, "full_name").lower()
            if canonical_repository != target.repository:
                raise ChangeImpactRepositoryEvidenceError(
                    "GitHub repository identity does not match the requested target."
                )
            repository_id = _positive_decimal(repository, "id")
            repository_owner = _object_field(repository, "owner")
            repository_owner_id = _positive_decimal(repository_owner, "id")

            pull_request_path = f"/repos/{repository_path}/pulls/{target.pull_request_number}"
            pull_request = _object_payload(
                self._github_api(path=pull_request_path, token=token),
                "GitHub pull request",
            )
            _validate_pull_request_repository(
                pull_request=pull_request,
                repository=canonical_repository,
                repository_id=repository_id,
            )
            head_sha = _pull_request_head_sha(pull_request)
            base_sha = _pull_request_base_sha(pull_request)
            base_ref = _pull_request_base_ref(pull_request)
            merge_commit_sha = _pull_request_merge_commit_sha(pull_request)
            updated_at = _required_string(pull_request, "updated_at")
            tree_sha = _git_commit_tree_sha(
                self._github_api(
                    path=f"/repos/{repository_path}/git/commits/{head_sha}",
                    token=token,
                )
            )
            changed_files = self._changed_files(
                repository_path=repository_path,
                pull_request_number=target.pull_request_number,
                token=token,
                max_file_pages=max_file_pages,
            )
            authorship = self._authorship(
                repository_path=repository_path,
                pull_request=pull_request,
                pull_request_number=target.pull_request_number,
                token=token,
            )

            confirmed_pull_request = _object_payload(
                self._github_api(path=pull_request_path, token=token),
                "GitHub pull request confirmation",
            )
            confirmed_head_sha = _pull_request_head_sha(confirmed_pull_request)
            confirmed_base_sha = _pull_request_base_sha(confirmed_pull_request)
            confirmed_merge_commit_sha = _pull_request_merge_commit_sha(confirmed_pull_request)
            confirmed_updated_at = _required_string(confirmed_pull_request, "updated_at")
            if (
                confirmed_head_sha != head_sha
                or confirmed_base_sha != base_sha
                or confirmed_merge_commit_sha != merge_commit_sha
                or confirmed_updated_at != updated_at
            ):
                raise ChangeImpactRepositoryEvidenceStaleError(
                    "GitHub pull request changed while resolving repository evidence."
                )

            return ChangeImpactRepositoryEvidence(
                target=ChangeImpactTarget(
                    repository_id=repository_id,
                    repository_owner_id=repository_owner_id,
                    repository=canonical_repository,
                    pull_request_number=target.pull_request_number,
                    head_sha=head_sha,
                    tree_sha=tree_sha,
                ),
                merge_commit_sha=merge_commit_sha,
                changed_files=changed_files,
                base=ChangeImpactBaseEvidence(base_ref=base_ref, base_sha=base_sha),
                authorship=authorship,
            )
        except ChangeImpactRepositoryEvidenceError:
            raise
        except Exception as error:
            raise ChangeImpactRepositoryEvidenceError(
                "Launchplane could not resolve authoritative GitHub repository evidence."
            ) from error

    def _token(self) -> str:
        token = self._github_token(
            control_plane_root=self._control_plane_root,
            context_name=self._token_context,
        ).strip()
        if not token:
            raise ChangeImpactRepositoryEvidenceError(
                "Launchplane GitHub repository evidence credentials are unavailable."
            )
        return token

    def _authorship(
        self,
        *,
        repository_path: str,
        pull_request: dict[str, object],
        pull_request_number: int,
        token: str,
    ) -> ChangeImpactAuthorshipEvidence:
        """Resolve numeric GitHub contributing identities over the reviewed range.

        Bot or agent work pushed under a human GitHub identity resolves to that
        human identity because GitHub links the commit to it. Any commit without a
        linked numeric identity, any login that maps to two different numeric IDs,
        and any range longer than the provider page bound fail closed as unresolved.
        """
        identity_by_login: dict[str, int] = {}
        contributor_ids: set[int] = set()
        conflicts: list[str] = []

        def record(actor: object, label: str) -> bool:
            if not isinstance(actor, dict):
                return False
            raw_id = str(actor.get("id", "")).strip()
            login = str(actor.get("login", "")).strip().casefold()
            if not raw_id.isdecimal() or int(raw_id) < 1 or not login:
                return False
            github_id = int(raw_id)
            known_id = identity_by_login.get(login)
            if known_id is not None and known_id != github_id:
                conflicts.append(f"{label} login {login} maps to {known_id} and {github_id}")
                return True
            identity_by_login[login] = github_id
            if str(actor.get("type", "")).strip().casefold() != "user":
                return False
            contributor_ids.add(github_id)
            return True

        if not record(pull_request.get("user"), "pull request author"):
            return ChangeImpactAuthorshipEvidence(
                resolution="unresolved",
                reason="pull request author has no linked numeric GitHub identity",
            )

        commit_count = 0
        for page in range(1, self._max_commit_pages + 1):
            commits = _list_payload(
                self._github_api(
                    path=(
                        f"/repos/{repository_path}/pulls/{pull_request_number}/commits"
                        f"?per_page=100&page={page}"
                    ),
                    token=token,
                ),
                "GitHub pull request commits",
            )
            for commit in commits:
                commit_count += 1
                commit_sha = str(commit.get("sha", "")).strip().lower() or "unknown"
                linked = record(commit.get("author"), f"commit {commit_sha} author")
                linked = record(commit.get("committer"), f"commit {commit_sha} committer") or linked
                if not linked:
                    return ChangeImpactAuthorshipEvidence(
                        resolution="unresolved",
                        commit_count=commit_count,
                        reason=f"commit {commit_sha} has no linked numeric GitHub identity",
                    )
            if len(commits) < 100:
                break
        else:
            return ChangeImpactAuthorshipEvidence(
                resolution="unresolved",
                commit_count=commit_count,
                reason="reviewed commit range exceeded the provider page bound",
            )
        if conflicts:
            return ChangeImpactAuthorshipEvidence(
                resolution="conflicting",
                commit_count=commit_count,
                reason="; ".join(sorted(set(conflicts)))[:500],
            )
        if not commit_count:
            return ChangeImpactAuthorshipEvidence(
                resolution="unresolved",
                reason="pull request returned no commit authorship evidence",
            )
        if not contributor_ids:
            return ChangeImpactAuthorshipEvidence(
                resolution="unresolved",
                commit_count=commit_count,
                reason="reviewed range has no human GitHub contributing identity",
            )
        return ChangeImpactAuthorshipEvidence(
            resolution="resolved",
            contributor_github_ids=tuple(sorted(contributor_ids)),
            commit_count=commit_count,
        )

    def _changed_files(
        self,
        *,
        repository_path: str,
        pull_request_number: int,
        token: str,
        max_file_pages: int,
    ) -> tuple[ChangeImpactChangedFileEvidence, ...]:
        evidence_by_path: dict[str, ChangeImpactChangedFileEvidence] = {}
        for page in range(1, max_file_pages + 1):
            payload = self._github_api(
                path=(
                    f"/repos/{repository_path}/pulls/{pull_request_number}/files"
                    f"?per_page=100&page={page}"
                ),
                token=token,
            )
            files = _list_payload(payload, "GitHub pull request files")
            for file_payload in files:
                filename = _required_string(file_payload, "filename")
                status = _required_string(file_payload, "status").lower()
                change_kind = _change_kind(status)
                evidence_by_path[filename] = ChangeImpactChangedFileEvidence(
                    path=filename,
                    change_kind=change_kind,
                )
                if status == "renamed":
                    previous_filename = str(file_payload.get("previous_filename", "")).strip()
                    if previous_filename:
                        evidence_by_path[previous_filename] = ChangeImpactChangedFileEvidence(
                            path=previous_filename,
                            change_kind="removed",
                        )
            if len(files) < 100:
                break
        else:
            raise ChangeImpactRepositoryEvidenceError(
                "GitHub pull request file evidence exceeded the complete provider page bound."
            )
        if not evidence_by_path:
            raise ChangeImpactRepositoryEvidenceError(
                "GitHub pull request did not return changed-file evidence."
            )
        return tuple(evidence_by_path.values())


def _repository_path(repository: str) -> str:
    owner, name = repository.split("/", maxsplit=1)
    return f"{quote(owner, safe='')}/{quote(name, safe='')}"


def _object_payload(payload: object, label: str) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ChangeImpactRepositoryEvidenceError(f"{label} response must be an object.")
    return {str(key): value for key, value in payload.items()}


def _list_payload(payload: object, label: str) -> tuple[dict[str, object], ...]:
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise ChangeImpactRepositoryEvidenceError(f"{label} response must be a list of objects.")
    return tuple({str(key): value for key, value in item.items()} for item in payload)


def _object_field(payload: dict[str, object], field_name: str) -> dict[str, object]:
    return _object_payload(payload.get(field_name), f"GitHub {field_name}")


def _required_string(payload: dict[str, object], field_name: str) -> str:
    value = str(payload.get(field_name, "")).strip()
    if not value:
        raise ChangeImpactRepositoryEvidenceError(
            f"GitHub response is missing required {field_name}."
        )
    return value


def _positive_decimal(payload: dict[str, object], field_name: str) -> str:
    value = str(payload.get(field_name, "")).strip()
    if not value.isdecimal() or int(value) < 1:
        raise ChangeImpactRepositoryEvidenceError(
            f"GitHub response is missing positive numeric {field_name}."
        )
    return str(int(value))


def _validate_pull_request_repository(
    *,
    pull_request: dict[str, object],
    repository: str,
    repository_id: str,
) -> None:
    base = _object_field(pull_request, "base")
    base_repository = _object_field(base, "repo")
    if _required_string(base_repository, "full_name").lower() != repository:
        raise ChangeImpactRepositoryEvidenceError(
            "GitHub pull request base repository does not match the requested target."
        )
    if _positive_decimal(base_repository, "id") != repository_id:
        raise ChangeImpactRepositoryEvidenceError(
            "GitHub pull request base repository identity is inconsistent."
        )


def _pull_request_head_sha(pull_request: dict[str, object]) -> str:
    return _required_string(_object_field(pull_request, "head"), "sha").lower()


def _pull_request_base_sha(pull_request: dict[str, object]) -> str:
    return _required_string(_object_field(pull_request, "base"), "sha").lower()


def _pull_request_base_ref(pull_request: dict[str, object]) -> str:
    return _required_string(_object_field(pull_request, "base"), "ref")


def _pull_request_merge_commit_sha(pull_request: dict[str, object]) -> str:
    value = pull_request.get("merge_commit_sha")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ChangeImpactRepositoryEvidenceError(
            "GitHub pull request merge_commit_sha must be a string or null."
        )
    normalized = value.strip().lower()
    if normalized and (
        len(normalized) != 40 or any(ch not in "0123456789abcdef" for ch in normalized)
    ):
        raise ChangeImpactRepositoryEvidenceError(
            "GitHub pull request merge_commit_sha must be a Git SHA."
        )
    return normalized


def _git_commit_tree_sha(payload: object) -> str:
    commit = _object_payload(payload, "GitHub git commit")
    return _required_string(_object_field(commit, "tree"), "sha").lower()


def _change_kind(status: str) -> ChangeImpactChangeKind:
    if status == "added":
        return "added"
    if status == "modified":
        return "modified"
    if status == "removed":
        return "removed"
    if status == "renamed":
        return "renamed"
    return "unknown"
