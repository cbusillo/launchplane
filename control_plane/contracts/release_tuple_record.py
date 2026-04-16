import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ReleaseTupleProvenance = Literal["ship", "promotion"]
GIT_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{7,40}$")


class ReleaseTupleRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    tuple_id: str
    context: str
    channel: str
    artifact_id: str
    repo_shas: dict[str, str]
    image_repository: str = ""
    image_digest: str = ""
    deployment_record_id: str = ""
    promotion_record_id: str = ""
    promoted_from_channel: str = ""
    provenance: ReleaseTupleProvenance
    minted_at: str

    @field_validator(
        "tuple_id",
        "context",
        "channel",
        "artifact_id",
        "minted_at",
        mode="after",
    )
    @classmethod
    def _validate_required_string(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("release tuple record requires non-empty string fields")
        return value.strip()

    @field_validator("repo_shas", mode="after")
    @classmethod
    def _validate_repo_shas(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for repo_name, git_sha in value.items():
            normalized_repo_name = repo_name.strip()
            normalized_git_sha = git_sha.strip()
            if not normalized_repo_name:
                raise ValueError("release tuple repo_shas keys must be non-empty")
            if not GIT_SHA_PATTERN.match(normalized_git_sha):
                raise ValueError(
                    f"release tuple repo_shas.{normalized_repo_name} must be a 7-40 character hexadecimal git sha"
                )
            normalized[normalized_repo_name] = normalized_git_sha
        if not normalized:
            raise ValueError("release tuple record requires at least one repo sha")
        return normalized

    @model_validator(mode="after")
    def _validate_promotion_linkage(self) -> "ReleaseTupleRecord":
        if self.provenance == "promotion" and not self.promoted_from_channel.strip():
            raise ValueError("promoted release tuple records require promoted_from_channel")
        return self
