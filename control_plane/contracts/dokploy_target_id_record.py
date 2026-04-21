from pydantic import BaseModel, ConfigDict, Field, field_validator


class DokployTargetIdRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str
    instance: str
    target_id: str
    updated_at: str
    source_label: str = ""

    @field_validator("context", "instance", "target_id", "updated_at", mode="after")
    @classmethod
    def _validate_required_string(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Dokploy target-id record requires non-empty string fields")
        return value.strip()

