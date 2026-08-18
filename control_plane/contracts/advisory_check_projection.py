from __future__ import annotations

from typing import Final, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator


ENGINEERING_REVIEW_CHECK_NAME: Final = "launchplane/engineering-review"
OWNER_ACCEPTANCE_CHECK_NAME: Final = "launchplane/owner-acceptance"
LEGACY_ENGINEERING_REVIEW_STATUS_CONTEXT: Final = "launchplane/engineering-review-shadow"

LAUNCHPLANE_PROJECTED_CHECK_NAMES = frozenset(
    {
        ENGINEERING_REVIEW_CHECK_NAME.casefold(),
        OWNER_ACCEPTANCE_CHECK_NAME.casefold(),
        LEGACY_ENGINEERING_REVIEW_STATUS_CONTEXT.casefold(),
    }
)

AdvisoryCheckProjectionStatus = Literal["projected", "updated", "replayed"]
AdvisoryCheckRunStatus = Literal["in_progress", "completed"]
AdvisoryCheckConclusion = Literal["neutral", "success", "failure", "action_required"]


class AdvisoryCheckProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    name: str
    repository: str
    repository_id: str = Field(pattern=r"^[1-9][0-9]*$")
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    external_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    details_url: str
    title: str = Field(min_length=1, max_length=255)
    summary: str = Field(min_length=1, max_length=65535)
    check_status: AdvisoryCheckRunStatus = "completed"
    conclusion: AdvisoryCheckConclusion | None = "neutral"

    @model_validator(mode="after")
    def _validate_projection(self) -> "AdvisoryCheckProjection":
        if self.schema_version != 1:
            raise ValueError("Unsupported advisory check projection schema version.")
        if self.name not in {
            ENGINEERING_REVIEW_CHECK_NAME,
            OWNER_ACCEPTANCE_CHECK_NAME,
        }:
            raise ValueError("Advisory check projection uses an unreserved check name.")
        if self.repository.strip() != self.repository or self.repository.count("/") != 1:
            raise ValueError("Advisory check projection repository must use owner/name.")
        if not all(part.strip() for part in self.repository.split("/", 1)):
            raise ValueError("Advisory check projection repository must use owner/name.")
        if self.details_url.strip() != self.details_url or not self.details_url:
            raise ValueError("Advisory check projection requires a canonical details URL.")
        parsed_details_url = urlsplit(self.details_url)
        if (
            parsed_details_url.scheme not in {"http", "https"}
            or not parsed_details_url.netloc
            or parsed_details_url.username is not None
            or parsed_details_url.password is not None
            or any(character.isspace() or ord(character) < 32 for character in self.details_url)
        ):
            raise ValueError("Advisory check projection requires an absolute public details URL.")
        try:
            parsed_details_url.port
        except ValueError as error:
            raise ValueError(
                "Advisory check projection requires a valid details URL port."
            ) from error
        if self.title.strip() != self.title or self.summary.strip() != self.summary:
            raise ValueError("Advisory check projection text must be canonical.")
        if self.check_status == "completed" and self.conclusion is None:
            raise ValueError("Completed advisory checks require a conclusion.")
        if self.check_status == "in_progress" and self.conclusion is not None:
            raise ValueError("In-progress advisory checks cannot have a conclusion.")
        return self


class AdvisoryCheckProjectionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2] = 2
    status: AdvisoryCheckProjectionStatus
    name: str
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    external_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    app_id: int = Field(ge=1)
    installation_id: int = Field(ge=1)
    check_run_id: int = Field(ge=1)
    check_status: AdvisoryCheckRunStatus
    conclusion: AdvisoryCheckConclusion | None


def is_launchplane_projected_check(name: str) -> bool:
    return name.strip().casefold() in LAUNCHPLANE_PROJECTED_CHECK_NAMES
