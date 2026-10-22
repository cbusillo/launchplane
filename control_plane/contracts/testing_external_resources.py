from typing import Literal

from pydantic import BaseModel, ConfigDict


ExternalResourceReadFailure = Literal[
    "provider_unavailable",
    "provider_http_refused",
    "response_invalid",
    "response_too_large",
    "collection_budget_exhausted",
    "guild_partition_changed",
    "inputs_changed",
    "configuration_unavailable",
]


class DiscordBotResourceObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["discord_bot"] = "discord_bot"
    identity_status: Literal["observed", "unavailable"] = "unavailable"
    bot_id: str = ""
    application_id: str = ""
    guild_status: Literal["observed", "unavailable"] = "unavailable"
    guild_ids: tuple[str, ...] = ()
    guild_enumeration_complete: bool = False
    failure_code: ExternalResourceReadFailure | None = None
    http_status: int | None = None
    started_at: str
    finished_at: str
    qualification: Literal["not_evaluated"] = "not_evaluated"


class TestingExternalResourceEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    product: str
    context: str
    instance: Literal["testing", "prod"]
    binding_key: str
    version_id: str
    observation: DiscordBotResourceObservation


class TestingExternalResourceAudit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["observed", "unavailable", "not_configured"]
    entries: tuple[TestingExternalResourceEntry, ...] = ()
    failure_code: ExternalResourceReadFailure | None = None
    qualification: Literal["not_evaluated"] = "not_evaluated"
