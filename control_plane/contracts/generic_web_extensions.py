"""Product-owned application lifecycle declarations for the shared web driver."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from control_plane.cron import validate_cron_expression
from control_plane.contracts.testing_application import TestingApplicationState


class GenericWebTestingState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["none", "volume", "postgresql", "unknown"]
    container_path: str = ""

    @model_validator(mode="after")
    def validate_application_state(self) -> "GenericWebTestingState":
        if self.kind in {"none", "volume"}:
            TestingApplicationState.model_validate(self.model_dump())
        elif self.container_path:
            raise ValueError("Only a local volume can declare an application state path.")
        return self


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
        if self.on_reset:
            raise ValueError("Recurring schedules cannot be reset-only commands.")
        self.cron_expression = self.cron_expression.strip()
        validate_cron_expression(self.cron_expression)
        return self


class GenericWebLaneExtensions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    testing_state: GenericWebTestingState | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        json_schema_extra={"x-launchplane-optional-response": True},
    )

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
        return bool(
            self.testing_state or self.maintenance or self.schedules or self.verification_extension
        )
