"""Product-owned application lifecycle declarations for the shared web driver."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from control_plane.cron import validate_cron_expression


class GenericWebMaintenanceCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    command: str = Field(min_length=1, max_length=8000)
    timeout_seconds: int = Field(default=300, ge=1, le=3600)
    on_reset: bool = False

    @model_validator(mode="after")
    def _validate_command(self) -> "GenericWebMaintenanceCommand":
        self.name = self.name.strip()
        self.command = self.command.strip()
        if not self.name or not self.command:
            raise ValueError("application maintenance requires a name and command")
        return self


class GenericWebApplicationSchedule(GenericWebMaintenanceCommand):
    cron_expression: str = Field(min_length=1)
    enabled: bool = True

    @model_validator(mode="after")
    def _validate_cron(self) -> "GenericWebApplicationSchedule":
        self.cron_expression = self.cron_expression.strip()
        validate_cron_expression(self.cron_expression)
        return self


class GenericWebLaneExtensions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verification_extension: Literal["", "verireel-testing"] = Field(
        default="",
        exclude_if=lambda value: not value,
        json_schema_extra={"x-launchplane-optional-response": True},
    )
    maintenance: tuple[GenericWebMaintenanceCommand, ...] = ()
    schedules: tuple[GenericWebApplicationSchedule, ...] = ()

    @model_validator(mode="after")
    def _validate_names(self) -> "GenericWebLaneExtensions":
        names = [item.name for item in (*self.maintenance, *self.schedules)]
        if len(names) != len(set(names)):
            raise ValueError("application lifecycle command and schedule names must be unique")
        return self

    @property
    def configured(self) -> bool:
        return bool(self.maintenance or self.schedules or self.verification_extension)
