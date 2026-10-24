from pydantic import BaseModel, ConfigDict, model_validator

from control_plane.contracts.text_fields import require_text


class MergeTrainGitHubRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: str
    path: str
    body: dict[str, object] | None = None

    @model_validator(mode="after")
    def _validate_request(self) -> "MergeTrainGitHubRequest":
        self.method = require_text(self.method, "GitHub request method is required.").upper()
        self.path = require_text(self.path, "GitHub request path is required.")
        if not self.path.startswith("/"):
            raise ValueError("GitHub request path must start with '/'.")
        return self


class RecordingMergeTrainGitHubTransport:
    def __init__(self, *, responses: tuple[object, ...] = ()) -> None:
        self.responses = list(responses)
        self.requests: list[MergeTrainGitHubRequest] = []

    def request(self, *, method: str, path: str, body: dict[str, object] | None = None) -> object:
        self.requests.append(
            MergeTrainGitHubRequest.model_validate({"method": method, "path": path, "body": body})
        )
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            return response
        return {}
