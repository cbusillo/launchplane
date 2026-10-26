from typing import Literal
import re

from pydantic import BaseModel, ConfigDict, Field


DISCORD_GUILD_LIMIT = 4000


def is_discord_snowflake(value: object) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"[1-9][0-9]{0,19}", value) is not None
        and int(value) < 2**64
    )


class TestingExternalCapabilityProof(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    activation_plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    binding_key: str
    binding_id: str
    secret_id: str
    version_id: str
    bot_id: str
    application_id: str
    guild_ids: tuple[str, ...]
    observed_at: str
    trace_id: str
    rate_limits: tuple["DiscordRateLimitEvidence", ...] = Field(default=(), max_length=2)
    old_writer_exclusion: Literal["not_evaluated"] = "not_evaluated"


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


class DiscordRateLimitEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    phase: Literal["identity", "application", "gateway", "guilds"]
    retry_after: float | None = Field(default=None, ge=0)
    global_limit: bool | None = None
    scope: Literal["user", "global", "shared"] | None = None
    header_global: bool | None = None
    header_retry_after: float | None = Field(default=None, ge=0)
    non_json_body: bool = False
    cloudflare_error_page: bool = False


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
    rate_limits: tuple[DiscordRateLimitEvidence, ...] = Field(default=(), max_length=2)
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
