from collections.abc import Callable
from datetime import datetime, timezone
from http.client import HTTPException
import json
import re
from time import monotonic
from typing import Any
from urllib.parse import urlencode

from control_plane.contracts.testing_external_resources import (
    DiscordBotResourceObservation,
    ExternalResourceReadFailure,
)
from control_plane.outbound_http import request_public_http

DISCORD_API_URL = "https://discord.com/api/v10"
DISCORD_PAGE_LIMIT = 200
DISCORD_GUILD_LIMIT = 4000
DISCORD_PAGE_BUDGET = 48
DISCORD_PARTITION_LIMIT = 16
DISCORD_RESPONSE_LIMIT = 256 * 1024


class DiscordResourceReadError(ValueError):
    def __init__(self, code: ExternalResourceReadFailure, http_status: int | None = None):
        super().__init__(code)
        self.code = code
        self.http_status = http_status


def discord_snowflake(value: object) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[1-9][0-9]{0,19}", value) is None
        or int(value) >= 2**64
    ):
        raise DiscordResourceReadError("response_invalid")
    return value


def discord_json_read(
    path: str, token: str, deadline: float, check_current: Callable[[], None]
) -> Any:
    check_current()
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise DiscordResourceReadError("collection_budget_exhausted")
    try:
        response = request_public_http(
            DISCORD_API_URL + path,
            method="GET",
            headers={
                "Authorization": "Bot " + token,
                "Accept": "application/json",
                "User-Agent": "DiscordBot (https://github.com/cbusillo/launchplane, 1.0)",
            },
            timeout_seconds=min(10.0, remaining),
            max_redirects=0,
            max_response_body_bytes=DISCORD_RESPONSE_LIMIT + 1,
        )
    except (HTTPException, OSError, RuntimeError, ValueError):
        raise DiscordResourceReadError("provider_unavailable") from None
    if response.status_code != 200:
        raise DiscordResourceReadError("provider_http_refused", response.status_code)
    if len(response.body) > DISCORD_RESPONSE_LIMIT:
        raise DiscordResourceReadError("response_too_large")
    try:
        return json.loads(response.body)
    except (ValueError, UnicodeError):
        raise DiscordResourceReadError("response_invalid") from None


def discord_guild_partitions(read: Callable[[str], Any]) -> int:
    gateway = read("/gateway/bot")
    if not isinstance(gateway, dict) or not isinstance(gateway.get("session_start_limit"), dict):
        raise DiscordResourceReadError("response_invalid")
    count = gateway["session_start_limit"].get("max_concurrency")
    if type(count) is not int or not 1 <= count <= DISCORD_PARTITION_LIMIT:
        raise DiscordResourceReadError("collection_budget_exhausted")
    return count


def discord_guild_memberships(read: Callable[[str], Any]) -> tuple[str, ...]:
    partitions = discord_guild_partitions(read)
    identifiers: set[str] = set()
    pages = 0
    for partition in range(partitions):
        cursor = 0
        while True:
            if pages >= DISCORD_PAGE_BUDGET:
                raise DiscordResourceReadError("collection_budget_exhausted")
            query = {"limit": DISCORD_PAGE_LIMIT, "shard": partition}
            if cursor:
                query["after"] = cursor
            rows = read("/users/@me/guilds?" + urlencode(query))
            pages += 1
            if not isinstance(rows, list) or len(rows) > DISCORD_PAGE_LIMIT:
                raise DiscordResourceReadError("response_invalid")
            if not rows:
                break
            current = []
            for row in rows:
                if not isinstance(row, dict):
                    raise DiscordResourceReadError("response_invalid")
                identifier = discord_snowflake(row.get("id"))
                if int(identifier) <= cursor or identifier in identifiers:
                    raise DiscordResourceReadError("response_invalid")
                identifiers.add(identifier)
                current.append(int(identifier))
            if len(identifiers) > DISCORD_GUILD_LIMIT:
                raise DiscordResourceReadError("collection_budget_exhausted")
            cursor = max(current)
    if discord_guild_partitions(read) != partitions:
        raise DiscordResourceReadError("guild_partition_changed")
    return tuple(sorted(identifiers, key=int))


def observe_discord_bot(
    token: str, *, deadline: float, check_current: Callable[[], None]
) -> DiscordBotResourceObservation:
    started = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    result: dict[str, Any] = {"started_at": started}

    def read(path: str) -> Any:
        return discord_json_read(path, token, deadline, check_current)

    try:
        user = read("/users/@me")
        if not isinstance(user, dict) or user.get("bot") is not True:
            raise DiscordResourceReadError("response_invalid")
        bot_id = discord_snowflake(user.get("id"))
        application = read("/applications/@me")
        if not isinstance(application, dict):
            raise DiscordResourceReadError("response_invalid")
        application_id = discord_snowflake(application.get("id"))
        if "bot" in application:
            associated = application["bot"]
            if (
                not isinstance(associated, dict)
                or discord_snowflake(associated.get("id")) != bot_id
            ):
                raise DiscordResourceReadError("response_invalid")
        result.update(identity_status="observed", bot_id=bot_id, application_id=application_id)
        result["guild_ids"] = discord_guild_memberships(read)
        result.update(guild_status="observed", guild_enumeration_complete=True)
    except DiscordResourceReadError as error:
        result.update(failure_code=error.code, http_status=error.http_status)
    result["finished_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return DiscordBotResourceObservation.model_validate(result)
