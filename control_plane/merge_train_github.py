import json
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, model_validator

from control_plane.contracts.merge_train_policy import MergeTrainMergeMethod
from control_plane.merge_train import MergeTrainCheckStatus
from control_plane.merge_train import MergeTrainDryRunSnapshot
from control_plane.merge_train import MergeTrainMergeableState
from control_plane.merge_train import MergeTrainPullRequestSnapshot
from control_plane.merge_train import MergeTrainPullRequestState


class MergeTrainGitHubError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class MergeTrainGitHubStaleHeadError(MergeTrainGitHubError):
    """Raised when GitHub refuses a guarded merge because the head SHA changed."""


class MergeTrainGitHubTransport(Protocol):
    def request(
        self, *, method: str, path: str, body: dict[str, object] | None = None
    ) -> object: ...


class UrllibMergeTrainGitHubTransport:
    def __init__(self, *, token: str, api_base_url: str = "https://api.github.com") -> None:
        self.token = _required_value(token, "GitHub token is required.")
        self.api_base_url = _required_value(api_base_url, "GitHub API base URL is required.").rstrip(
            "/"
        )

    def request(
        self, *, method: str, path: str, body: dict[str, object] | None = None
    ) -> object:
        request_body = None
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if body is not None:
            request_body = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            url=f"{self.api_base_url}{path}",
            method=method,
            headers=headers,
            data=request_body,
        )
        try:
            with urlopen(request, timeout=15) as response:
                response_text = response.read().decode("utf-8")
                return json.loads(response_text) if response_text.strip() else None
        except HTTPError as error:
            raise _github_http_error(path=path, status_code=error.code, error=error) from error
        except (URLError, OSError) as error:
            raise MergeTrainGitHubError(f"GitHub API request failed for {path}: {error}") from error
        except json.JSONDecodeError as error:
            raise MergeTrainGitHubError(
                f"GitHub API response for {path} was not valid JSON."
            ) from error


class GitHubMergeTrainClient:
    def __init__(self, *, transport: MergeTrainGitHubTransport) -> None:
        self.transport = transport

    def add_pull_request_label(
        self, *, repository: str, pull_request_number: int, label: str
    ) -> None:
        repository_path = _repository_path(repository)
        normalized_label = _required_value(label, "GitHub label is required.")
        self.transport.request(
            method="POST",
            path=f"/repos/{repository_path}/issues/{pull_request_number}/labels",
            body={"labels": [normalized_label]},
        )

    def update_pull_request_branch(
        self, *, repository: str, pull_request_number: int, expected_head_sha: str
    ) -> None:
        repository_path = _repository_path(repository)
        self.transport.request(
            method="PUT",
            path=f"/repos/{repository_path}/pulls/{pull_request_number}/update-branch",
            body={
                "expected_head_sha": _required_value(
                    expected_head_sha, "Expected pull request head SHA is required."
                )
            },
        )

    def merge_pull_request(
        self,
        *,
        repository: str,
        pull_request_number: int,
        head_sha: str,
        merge_method: MergeTrainMergeMethod,
    ) -> str:
        repository_path = _repository_path(repository)
        payload = self.transport.request(
            method="PUT",
            path=f"/repos/{repository_path}/pulls/{pull_request_number}/merge",
            body={
                "sha": _required_value(head_sha, "Pull request head SHA is required."),
                "merge_method": merge_method,
            },
        )
        if not isinstance(payload, dict):
            raise MergeTrainGitHubError(
                "GitHub merge response must be a JSON object.", status_code=None
            )
        merge_commit_sha = str(payload.get("sha") or "").strip()
        if not merge_commit_sha:
            raise MergeTrainGitHubError(
                "GitHub merge response did not include a merge commit SHA.", status_code=None
            )
        return merge_commit_sha


class GitHubMergeTrainSnapshotReader:
    def __init__(self, *, transport: MergeTrainGitHubTransport) -> None:
        self.transport = transport

    def read_merge_train_snapshot(
        self, *, repository: str, base_branch: str
    ) -> MergeTrainDryRunSnapshot:
        repository_path = _repository_path(repository)
        normalized_base_branch = _required_value(
            base_branch, "GitHub pull request base branch is required."
        )
        pull_requests = tuple(
            self._pull_request_snapshot(
                repository=repository,
                repository_path=repository_path,
                pull_request=pull_request,
            )
            for pull_request in self._list_open_pull_requests(
                repository_path=repository_path, base_branch=normalized_base_branch
            )
        )
        return MergeTrainDryRunSnapshot(
            repository=repository,
            base_branch=normalized_base_branch,
            pull_requests=pull_requests,
        )

    def _list_open_pull_requests(
        self, *, repository_path: str, base_branch: str
    ) -> tuple[dict[str, object], ...]:
        pull_requests: list[dict[str, object]] = []
        page = 1
        while True:
            query = urlencode(
                {
                    "state": "open",
                    "base": base_branch,
                    "sort": "created",
                    "direction": "asc",
                    "per_page": "100",
                    "page": str(page),
                }
            )
            payload = self.transport.request(
                method="GET", path=f"/repos/{repository_path}/pulls?{query}"
            )
            if not isinstance(payload, list):
                raise MergeTrainGitHubError(
                    "GitHub pull request list response must be a JSON array."
                )
            page_pull_requests = [_json_object(item, "GitHub pull request entry") for item in payload]
            pull_requests.extend(page_pull_requests)
            if len(page_pull_requests) < 100:
                return tuple(pull_requests)
            page += 1

    def _pull_request_snapshot(
        self, *, repository: str, repository_path: str, pull_request: dict[str, object]
    ) -> MergeTrainPullRequestSnapshot:
        pull_request_number = _required_int(
            pull_request.get("number"), "GitHub pull request entry requires number."
        )
        detail = _json_object(
            self.transport.request(
                method="GET", path=f"/repos/{repository_path}/pulls/{pull_request_number}"
            ),
            "GitHub pull request detail response",
        )
        source = pull_request | detail
        head = _json_object(source.get("head"), "GitHub pull request head")
        base = _json_object(source.get("base"), "GitHub pull request base")
        head_sha = _required_text(head.get("sha"), "GitHub pull request head requires sha.")
        user = _json_object(source.get("user"), "GitHub pull request user")
        actor_role = self._actor_role_for_pull_request(
            repository_path=repository_path,
            username=_required_text(user.get("login"), "GitHub pull request user requires login."),
            author_association=str(source.get("author_association") or ""),
        )
        return MergeTrainPullRequestSnapshot(
            number=pull_request_number,
            url=str(source.get("html_url") or "").strip(),
            title=str(source.get("title") or "").strip(),
            state=_pull_request_state(str(source.get("state") or "")),
            is_draft=bool(source.get("draft")),
            created_at=_required_text(
                source.get("created_at"), "GitHub pull request entry requires created_at."
            ),
            labels=_labels(source.get("labels")),
            actor_role=actor_role,
            head_sha=head_sha,
            base_sha=str(base.get("sha") or "").strip(),
            mergeable=_mergeable_state(source),
            required_checks_status=self._required_checks_status(
                repository_path=repository_path, head_sha=head_sha
            ),
            branch_update_required=_branch_update_required(source),
        )

    def _actor_role_for_pull_request(
        self, *, repository_path: str, username: str, author_association: str
    ) -> str:
        if author_association.upper() == "OWNER":
            return "repo_owner"
        try:
            payload = _json_object(
                self.transport.request(
                    method="GET",
                    path=f"/repos/{repository_path}/collaborators/{quote(username, safe='')}/permission",
                ),
                "GitHub collaborator permission response",
            )
        except MergeTrainGitHubError as error:
            if error.status_code == 404:
                return "unknown"
            raise
        return "repo_admin" if str(payload.get("permission") or "") == "admin" else "unknown"

    def _required_checks_status(
        self, *, repository_path: str, head_sha: str
    ) -> MergeTrainCheckStatus:
        encoded_head_sha = quote(head_sha, safe="")
        status_payload = _json_object(
            self.transport.request(
                method="GET", path=f"/repos/{repository_path}/commits/{encoded_head_sha}/status"
            ),
            "GitHub combined status response",
        )
        check_runs_payload = self._list_check_runs(
            repository_path=repository_path, encoded_head_sha=encoded_head_sha
        )
        return _combine_check_statuses(
            _combined_status_state(status_payload), _check_runs_status(check_runs_payload)
        )

    def _list_check_runs(
        self, *, repository_path: str, encoded_head_sha: str
    ) -> dict[str, object]:
        check_runs: list[object] = []
        page = 1
        total_count: int | None = None
        while True:
            query = urlencode({"per_page": "100", "page": str(page)})
            payload = _json_object(
                self.transport.request(
                    method="GET",
                    path=(
                        f"/repos/{repository_path}/commits/{encoded_head_sha}/check-runs?{query}"
                    ),
                ),
                "GitHub check runs response",
            )
            raw_check_runs = payload.get("check_runs")
            if not isinstance(raw_check_runs, list):
                raise MergeTrainGitHubError("GitHub check runs response must include check_runs.")
            raw_total_count = payload.get("total_count")
            if isinstance(raw_total_count, int):
                total_count = raw_total_count
            check_runs.extend(raw_check_runs)
            if len(raw_check_runs) < 100:
                break
            page += 1
        return {"total_count": total_count if total_count is not None else len(check_runs), "check_runs": check_runs}


class MergeTrainGitHubRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: str
    path: str
    body: dict[str, object] | None = None

    @model_validator(mode="after")
    def _validate_request(self) -> "MergeTrainGitHubRequest":
        self.method = _required_value(self.method, "GitHub request method is required.").upper()
        self.path = _required_value(self.path, "GitHub request path is required.")
        if not self.path.startswith("/"):
            raise ValueError("GitHub request path must start with '/'.")
        return self


class RecordingMergeTrainGitHubTransport:
    def __init__(self, *, responses: tuple[object, ...] = ()) -> None:
        self.responses = list(responses)
        self.requests: list[MergeTrainGitHubRequest] = []

    def request(
        self, *, method: str, path: str, body: dict[str, object] | None = None
    ) -> object:
        self.requests.append(
            MergeTrainGitHubRequest.model_validate(
                {"method": method, "path": path, "body": body}
            )
        )
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            return response
        return {}


def _repository_path(repository: str) -> str:
    normalized = _required_value(repository, "GitHub repository is required.")
    parts = normalized.split("/")
    if len(parts) != 2 or not all(part.strip() for part in parts):
        raise ValueError("GitHub repository must be formatted as owner/name.")
    return "/".join(quote(part.strip(), safe="") for part in parts)


def _json_object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise MergeTrainGitHubError(f"{label} must be a JSON object.")
    return value


def _required_int(value: object, message: str) -> int:
    if not isinstance(value, int) or value <= 0:
        raise MergeTrainGitHubError(message)
    return value


def _required_text(value: object, message: str) -> str:
    if not isinstance(value, str):
        raise MergeTrainGitHubError(message)
    normalized = value.strip()
    if not normalized:
        raise MergeTrainGitHubError(message)
    return normalized


def _pull_request_state(value: str) -> MergeTrainPullRequestState:
    normalized = value.strip().lower()
    if normalized == "open":
        return "open"
    if normalized == "closed":
        return "closed"
    raise MergeTrainGitHubError("GitHub pull request state must be open or closed.")


def _labels(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise MergeTrainGitHubError("GitHub pull request labels must be a JSON array.")
    labels: list[str] = []
    for item in value:
        label = _json_object(item, "GitHub pull request label")
        name = _required_text(label.get("name"), "GitHub pull request label requires name.")
        if name not in labels:
            labels.append(name)
    return tuple(labels)


def _mergeable_state(source: dict[str, object]) -> MergeTrainMergeableState:
    if bool(source.get("draft")):
        return "unknown"
    mergeable = source.get("mergeable")
    if mergeable is True:
        return "mergeable"
    if mergeable is False:
        return "conflicting"
    return "unknown"


def _branch_update_required(source: dict[str, object]) -> bool:
    mergeable_state = str(source.get("mergeable_state") or "").strip().lower()
    return mergeable_state == "behind"


def _combined_status_state(payload: dict[str, object]) -> MergeTrainCheckStatus:
    if payload.get("total_count") == 0:
        return "unknown"
    state = str(payload.get("state") or "").strip().lower()
    if state == "success":
        return "pass"
    if state in {"failure", "error"}:
        return "fail"
    if state == "pending":
        return "pending"
    return "unknown"


def _check_runs_status(payload: dict[str, object]) -> MergeTrainCheckStatus:
    check_runs = payload.get("check_runs")
    if not isinstance(check_runs, list):
        raise MergeTrainGitHubError("GitHub check runs response must include check_runs.")
    if not check_runs:
        return "unknown"
    statuses: list[MergeTrainCheckStatus] = []
    for item in check_runs:
        check_run = _json_object(item, "GitHub check run")
        statuses.append(_check_run_status(check_run))
    return _combine_check_statuses(*statuses)


def _check_run_status(check_run: dict[str, object]) -> MergeTrainCheckStatus:
    status = str(check_run.get("status") or "").strip().lower()
    if status != "completed":
        return "pending" if status else "unknown"
    conclusion = str(check_run.get("conclusion") or "").strip().lower()
    if conclusion in {"success", "neutral", "skipped"}:
        return "pass"
    if conclusion in {"failure", "timed_out", "cancelled", "action_required"}:
        return "fail"
    return "unknown"


def _combine_check_statuses(*statuses: MergeTrainCheckStatus) -> MergeTrainCheckStatus:
    if any(status == "fail" for status in statuses):
        return "fail"
    if any(status == "pending" for status in statuses):
        return "pending"
    if statuses and all(status == "pass" for status in statuses):
        return "pass"
    if any(status == "pass" for status in statuses):
        return "pass"
    return "unknown"


def _required_value(value: str, message: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(message)
    return normalized


def _github_http_error(*, path: str, status_code: int, error: HTTPError) -> MergeTrainGitHubError:
    message = f"GitHub API request failed for {path}: HTTP {status_code}"
    if status_code == 409:
        return MergeTrainGitHubStaleHeadError(message, status_code=status_code)
    return MergeTrainGitHubError(message, status_code=status_code)
