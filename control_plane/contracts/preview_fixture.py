from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.runtime_identity import RuntimeIdentity


class PreviewFixtureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["preview-fixture"]
    intent: Literal["preview-fixture"]
    context: str = Field(min_length=1, max_length=128)
    instance: str = Field(min_length=1, max_length=128)
    preview_id: str = Field(min_length=1, max_length=200)
    generation_id: str = Field(min_length=1, max_length=200)
    fixture: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    mode: Literal["dry-run", "apply", "readback"] = "dry-run"
    reviewed_plan_sha256: str = Field(default="", pattern=r"^(|[a-f0-9]{64})$")
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_preview(self) -> "PreviewFixtureRequest":
        if self.instance.strip().lower() in {"testing", "prod", "production"}:
            raise ValueError("Fixtures require a named preview.")
        if (self.mode == "apply") != bool(self.reviewed_plan_sha256):
            raise ValueError("Only apply requires the reviewed plan digest.")
        return self


class FixtureRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$", max_length=128)
    id: int = Field(strict=True, ge=1)
    disposition: Literal["created", "reused"]
    reference: str = Field(
        default="", pattern=r"^(|[A-Z0-9][A-Z0-9/._-]{0,79})$", exclude_if=lambda value: value == ""
    )


class PreviewFixtureReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity: RuntimeIdentity
    fixture: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    fixture_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    mode: Literal["dry-run", "apply", "readback"]
    committed: bool
    verified: Literal[True]
    records: tuple[FixtureRecord, ...] = Field(max_length=128)

    @model_validator(mode="after")
    def validate_receipt(self) -> "PreviewFixtureReceipt":
        if self.identity.environment_kind != "preview":
            raise ValueError("Fixture receipt must identify a preview.")
        keys = [(item.model, item.id) for item in self.records]
        if len(keys) != len(set(keys)):
            raise ValueError("Fixture record identities must be unique.")
        if self.committed != (self.mode == "apply"):
            raise ValueError("Fixture commit evidence disagrees with its mode.")
        if self.mode == "apply" and not self.records:
            raise ValueError("Applied fixtures require record readback.")
        return self
