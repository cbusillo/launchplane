from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.runtime_identity import RuntimeIdentity


class OdooBusinessHoursPolicy(BaseModel):
    """Select the company calendar in Odoo; never copy its hours into Launchplane."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    company_id: int = Field(ge=1)
    private_endpoint_key: str = ""


class BusinessHoursWindow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    applies: bool = False
    blocked_reason: str = ""
    next_release_at: str = ""
    timezone: str = ""


class OdooBusinessHoursConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: Literal["inspect", "set", "clear"]
    company_id: int | None = Field(default=None, strict=True, ge=1)
    expected_calendar_id: int | None = Field(default=None, strict=True, ge=1)
    private_endpoint_key: str = Field(default="", max_length=256)

    @model_validator(mode="after")
    def validate_operation(self) -> "OdooBusinessHoursConfigRequest":
        if self.private_endpoint_key != self.private_endpoint_key.strip():
            raise ValueError("Endpoint key must not contain surrounding whitespace.")
        if self.operation == "set" and (
            self.company_id is None or self.expected_calendar_id is None
        ):
            raise ValueError("Set requires an explicit company and expected calendar.")
        if self.operation == "inspect" and self.expected_calendar_id is not None:
            raise ValueError("Inspect does not select a calendar.")
        if self.operation == "clear" and (
            self.company_id is not None
            or self.expected_calendar_id is not None
            or self.private_endpoint_key
        ):
            raise ValueError("Clear cannot select company, calendar or transport.")
        return self


class OdooCalendarCompanyReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    company_id: int = Field(ge=1)
    calendar_id: int | None = Field(default=None, ge=1)
    label: str = ""
    label_omitted: bool = False


class OdooBusinessHoursConfigResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: Literal["inspect", "set", "clear"]
    policy_before: OdooBusinessHoursPolicy | None = None
    policy_after: OdooBusinessHoursPolicy | None = None
    changed: bool
    applied: bool = False
    apply_eligible: bool = False
    effective_when: Literal["live"] = "live"
    company_scope: Literal["permitted_company_ids"] = "permitted_company_ids"
    permitted_company_ids: tuple[int, ...] = ()
    companies: tuple[OdooCalendarCompanyReference, ...] = ()
    truncated: bool = False
    runtime_identity: RuntimeIdentity | None = None
    company_id: int | None = None
    calendar_id: int | None = None
    timezone: str = ""
    window: BusinessHoursWindow | None = None
    authority_digest: str = ""
    plan_digest: str = ""
