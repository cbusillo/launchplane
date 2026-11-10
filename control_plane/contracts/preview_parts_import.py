from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.runtime_identity import RuntimeIdentity

IMPORT_TIMEOUT_SECONDS: Literal[600] = 600


class PreviewImportCounts(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    parts: int | None = Field(default=None, ge=0)
    supplier_cost_rows: int | None = Field(default=None, ge=0)
    copied_fields: (
        dict[Literal["text", "number", "boolean", "date", "relation"], Annotated[int, Field(ge=0)]]
        | None
    ) = None
    conflicts: int | None = Field(default=None, ge=0)
    unmapped_rows: int | None = Field(default=None, ge=0)


class PreviewPartsImportReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    identity: RuntimeIdentity
    source_authority_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    operation: str = Field(pattern=r"^(|[a-f0-9]{64})$")
    mode: Literal["dry-run", "apply", "readback"]
    parts_only: Literal[True] = True
    grants: Literal["read_only", "unsafe", "unknown"] = "unknown"
    stage: Literal["configuration", "entrypoint", "grants", "qualified", "import", "complete"]
    outcome: Literal["qualified", "unavailable", "committed", "unknown"]
    committed: bool = False
    binding_current: Literal[True] = True
    timeout_seconds: Literal[600] = IMPORT_TIMEOUT_SECONDS
    elapsed_seconds: float = Field(ge=0, le=IMPORT_TIMEOUT_SECONDS)
    counts: PreviewImportCounts = Field(default_factory=PreviewImportCounts)

    @model_validator(mode="after")
    def validate_outcome(self) -> "PreviewPartsImportReceipt":
        if self.identity.environment_kind != "preview":
            raise ValueError("Import requires an exact preview.")
        if self.committed != (self.outcome == "committed"):
            raise ValueError("Import commit evidence disagrees with outcome.")
        if self.committed and (
            not self.operation or self.stage != "complete" or self.grants != "read_only"
        ):
            raise ValueError("Commit requires original-operation and grants evidence.")
        if self.outcome == "qualified" and (
            self.mode != "dry-run" or self.grants != "read_only" or self.stage != "qualified"
        ):
            raise ValueError("Qualification requires a read-only grants preflight.")
        return self
