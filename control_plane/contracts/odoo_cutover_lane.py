"""Provider coordinates owned by the persisted product lane, never a release request."""

from typing import Self
from pydantic import Field, model_validator
from control_plane.contracts.prepared_public_site import FrozenRecord, WriterTarget


class OdooSlotCoordinates(FrozenRecord):
    slot_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    compose_id: str = Field(min_length=1)
    private_url: str = Field(min_length=1)


class OdooCutoverLane(FrozenRecord):
    route_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    host: str = Field(pattern=r"^[a-zA-Z0-9.-]+$")
    database: str = Field(min_length=1)
    database_uuid: str = Field(min_length=1)
    prepared_origin: str = Field(min_length=1)
    slots: tuple[OdooSlotCoordinates, OdooSlotCoordinates]
    writers: tuple[WriterTarget, ...]

    @model_validator(mode="after")
    def distinct_slots(self) -> Self:
        for field in ("slot_id", "compose_id", "private_url"):
            if getattr(self.slots[0], field) == getattr(self.slots[1], field):
                raise ValueError("lane requires two distinct app coordinates")
        if self.prepared_origin in {slot.private_url for slot in self.slots}:
            raise ValueError("prepared copy must be an independent endpoint")
        return self
