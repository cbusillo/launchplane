from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


GUEST_CAPTURE_INTEGRATION = "production_backup_capture"


class GuestCaptureBoundary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str = Field(min_length=1)
    username: str = Field(min_length=1)
    guest_kind: Literal["lxc", "qemu"]
    guest_id: str = Field(pattern=r"^[0-9]+$")
    storage_id: str = Field(min_length=1)
    snapshot_prefix: str = Field(min_length=1)

    @field_validator("host")
    @classmethod
    def normalize_host(cls, value: str) -> str:
        return value.lower()

    @field_validator("guest_id")
    @classmethod
    def normalize_guest_id(cls, value: str) -> str:
        if int(value) <= 0:
            raise ValueError("Capture guest ID must be positive.")
        return str(int(value))
