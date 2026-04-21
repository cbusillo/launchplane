from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ScalarValue = str | int | float | bool
RuntimeEnvironmentScope = Literal["global", "context", "instance"]


class RuntimeEnvironmentRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    scope: RuntimeEnvironmentScope
    context: str = ""
    instance: str = ""
    env: dict[str, ScalarValue]
    updated_at: str
    source_label: str = ""

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

