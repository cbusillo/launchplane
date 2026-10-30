from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.runtime_identity import RuntimeIdentity


PREVIEW_FIXTURE_CANCEL_PHASE = "fixture_container_stop"


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
        default="",
        pattern=r"^(|[A-Za-z0-9][A-Za-z0-9 /._-]{0,79})$",
        exclude_if=lambda value: value == "",
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
        if self.committed and self.mode != "apply":
            raise ValueError("Fixture commit evidence disagrees with its mode.")
        if self.mode == "apply" and self.committed != bool(self.records):
            raise ValueError("Applied fixtures require commit evidence or an empty rejection.")
        return self


class PreviewFixtureBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity: RuntimeIdentity
    target_id: str = Field(min_length=1)
    target_name: str = Field(min_length=1)
    container_id: str = Field(min_length=1)
    app_name: str = Field(min_length=1)
    database: str = Field(min_length=1)
    schedule_type: Literal["server", "dokploy-server"]
    lookup_id: str = Field(min_length=1)
    server_id: str | None
    anchor_pr_number: int = Field(ge=1)


class PreviewFixtureIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str = Field(min_length=1)
    maintenance: PreviewFixtureRequest
    fixture_binding: PreviewFixtureBinding
    writer_schedule_app_name: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_binding(self) -> "PreviewFixtureIntent":
        identity = self.fixture_binding.identity
        request = self.maintenance
        if (
            request.mode != "apply"
            or identity.environment_kind != "preview"
            or identity.product != self.product
            or identity.context != request.context
            or identity.instance != request.instance
            or identity.preview_id != request.preview_id
            or identity.preview_generation_id != request.generation_id
        ):
            raise ValueError("Fixture intent must retain its exact preview binding.")
        return self
