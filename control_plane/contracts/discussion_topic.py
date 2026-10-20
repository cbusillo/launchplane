"""Client-safe discussion metadata. Private reasoning stays on its owning record."""

import unicodedata
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from control_plane.contracts.repository_inventory import normalize_utc_timestamp

TopicId = Annotated[
    str, Field(min_length=1, max_length=96, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
]


class DiscussionTopicInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str = Field(min_length=1, max_length=240)
    issue_url: str = Field(min_length=1, max_length=2048)

    @field_validator("title")
    @classmethod
    def neutral_title(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith("Decide: ") or not value.removeprefix("Decide: ").strip():
            raise ValueError("Use a neutral title beginning with 'Decide: '.")
        if any(unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"} for char in value):
            raise ValueError("Topic titles must be one plain-text line.")
        return value

    @field_validator("issue_url")
    @classmethod
    def safe_issue_link(cls, value: str) -> str:
        if any(char.isspace() or unicodedata.category(char) in {"Cc", "Cf"} for char in value):
            raise ValueError("Topic links cannot contain whitespace or control characters.")
        parts = urlsplit(value)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
        ):
            raise ValueError("Topic links require HTTPS without credentials.")
        return value


class DiscussionTopicRecord(DiscussionTopicInput):
    product: str = Field(min_length=1)
    topic_id: TopicId
    added_at: str
    closed_at: str | None = None

    @field_validator("added_at", "closed_at")
    @classmethod
    def utc_timestamp(cls, value: str | None) -> str | None:
        return normalize_utc_timestamp(value, "topic timestamp") if value is not None else None
