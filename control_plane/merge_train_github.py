import json
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, model_validator

from control_plane.contracts.merge_train_policy import MergeTrainMergeMethod


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
