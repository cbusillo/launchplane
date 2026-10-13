from pydantic import BaseModel, ConfigDict, Field


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
