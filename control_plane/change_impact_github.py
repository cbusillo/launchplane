from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

from control_plane.contracts.change_impact import (
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


class GitHubChangeImpactRepositoryEvidenceProvider:
    def __init__(
        self,
        *,
        control_plane_root: Path,
        github_token: Callable[..., str],
        github_api: Callable[..., object],
        token_context: str,
        max_file_pages: int = 30,
    ) -> None:
        if max_file_pages < 1:
            raise ValueError("change-impact GitHub provider requires at least one file page")
        self._control_plane_root = control_plane_root
        self._github_token = github_token
        self._github_api = github_api
        self._token_context = token_context.strip()
        self._max_file_pages = max_file_pages
        if not self._token_context:
            raise ValueError("change-impact GitHub provider requires a token context")

    def resolve(
        self,
        target: ChangeImpactTargetReference,
    ) -> ChangeImpactRepositoryEvidence:
        try:
            token = self._github_token(
                control_plane_root=self._control_plane_root,
                context_name=self._token_context,
            ).strip()
            if not token:
                raise ChangeImpactRepositoryEvidenceError(
                    "Launchplane GitHub repository evidence credentials are unavailable."
                )
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
            )
        except ChangeImpactRepositoryEvidenceError:
            raise
        except Exception as error:
            raise ChangeImpactRepositoryEvidenceError(
                "Launchplane could not resolve authoritative GitHub repository evidence."
            ) from error

    def _changed_files(
        self,
        *,
        repository_path: str,
        pull_request_number: int,
        token: str,
    ) -> tuple[ChangeImpactChangedFileEvidence, ...]:
        evidence_by_path: dict[str, ChangeImpactChangedFileEvidence] = {}
        for page in range(1, self._max_file_pages + 1):
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


def _pull_request_merge_commit_sha(pull_request: dict[str, object]) -> str:
    value = pull_request.get("merge_commit_sha")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ChangeImpactRepositoryEvidenceError(
            "GitHub pull request merge_commit_sha must be a string or null."
        )
    normalized = value.strip().lower()
    if normalized and (len(normalized) != 40 or any(ch not in "0123456789abcdef" for ch in normalized)):
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
