from typing import Literal
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ScalarValue = str | int | float | bool
RuntimeEnvironmentScope = Literal["global", "context", "instance"]


def normalize_retired_provider_keys(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > 256:
        raise ValueError("Retired provider keys require a bounded list of environment key names")
    keys: list[str] = []
    for key in value:
        if not isinstance(key, str) or re.fullmatch(r"[A-Z_][A-Z0-9_]{0,127}", key) is None:
            raise ValueError("Retired provider keys must be uppercase environment key names")
        if key in keys:
            raise ValueError("Retired provider keys must be unique")
        keys.append(key)
    return tuple(sorted(keys))


class RuntimeEnvironmentRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    scope: RuntimeEnvironmentScope
    context: str = ""
    instance: str = ""
    env: dict[str, ScalarValue]
    retired_provider_keys: tuple[str, ...] = Field(default=(), exclude_if=lambda value: not value)
    updated_at: str
    source_label: str = ""

    @field_validator("retired_provider_keys", mode="before")
    @classmethod
    def _validate_retired_provider_keys(cls, value: object) -> tuple[str, ...]:
        return normalize_retired_provider_keys(value)

    @model_validator(mode="after")
    def _validate_retirement_scope(self) -> "RuntimeEnvironmentRecord":
        if self.retired_provider_keys:
            if self.schema_version != 2:
                raise ValueError("Provider key retirement requires runtime record schema version 2")
            if self.scope != "instance" or not self.context.strip() or not self.instance.strip():
                raise ValueError("Provider key retirement requires an exact context and instance")
            if set(self.retired_provider_keys) & self.env.keys():
                raise ValueError("A provider key cannot be both configured and retired")
        return self

    @field_validator("updated_at", mode="after")
    @classmethod
    def _validate_updated_at(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("runtime environment record requires updated_at")
        return value.strip()

    @field_validator("env", mode="after")
    @classmethod
    def _validate_env(cls, value: dict[str, ScalarValue]) -> dict[str, ScalarValue]:
        normalized: dict[str, ScalarValue] = {}
        for key, item in value.items():
            normalized_key = key.strip()
            if not normalized_key:
                raise ValueError("runtime environment record env keys must be non-empty")
            normalized[normalized_key] = item
        if not normalized:
            raise ValueError("runtime environment record requires at least one env value")
        return normalized


class RuntimeEnvironmentDeleteEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    event_id: str
    event_type: Literal["deleted"] = "deleted"
    recorded_at: str
    actor: str = ""
    scope: RuntimeEnvironmentScope
    context: str = ""
    instance: str = ""
    source_label: str = ""
    env_keys: tuple[str, ...]
    env_value_count: int = Field(ge=0)
    detail: str = ""

    @model_validator(mode="after")
    def _validate_event(self) -> "RuntimeEnvironmentDeleteEvent":
        if not self.event_id.strip():
            raise ValueError("runtime environment delete event requires event_id")
        if not self.recorded_at.strip():
            raise ValueError("runtime environment delete event requires recorded_at")
        if self.scope == "global" and (self.context.strip() or self.instance.strip()):
            raise ValueError("global runtime environment delete event cannot set context/instance")
        if self.scope == "context" and (not self.context.strip() or self.instance.strip()):
            raise ValueError("context runtime environment delete event requires context only")
        if self.scope == "instance" and (not self.context.strip() or not self.instance.strip()):
            raise ValueError(
                "instance runtime environment delete event requires context and instance"
            )
        if self.env_value_count != len(self.env_keys):
            raise ValueError("runtime environment delete event key count must match env_keys")
        return self
