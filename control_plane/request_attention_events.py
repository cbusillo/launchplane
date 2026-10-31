from __future__ import annotations

from collections.abc import AsyncGenerator
import asyncio
import json
from typing import Literal, cast

import psycopg
from pydantic import BaseModel, Field
from sqlalchemy.engine import make_url

from control_plane.contracts.repository_inventory import RepositoryInventoryRecord
from control_plane.repository_inventory import (
    RepositoryInventoryReadStore,
    get_repository_inventory_read_model,
)
from control_plane.timestamps import utc_now_timestamp

ATTENTION_EVENT_CHANNEL = "launchplane_request_attention"
STREAM_SECONDS = 55


class AttentionEventHint(BaseModel):
    type: Literal["message"] = "message"
    repository: str
    source_kind: Literal["request", "change", "comment", "review_comment", "review"]
    source_id: int = Field(strict=True, gt=0)
    request_number: int = Field(strict=True, gt=0)
    delivery_id: str = Field(min_length=1, max_length=256)
    received_at: str


def notify_attention_for_event(
    store: object, event: str, payload: dict[str, object], delivery_id: str, received_at: str
) -> bool:
    shapes = {
        "issues": ("issue", "request", "number"),
        "pull_request": ("pull_request", "change", "number"),
        "issue_comment": ("comment", "comment", "id"),
        "pull_request_review_comment": ("comment", "review_comment", "id"),
        "pull_request_review": ("review", "review", "id"),
    }
    if event not in shapes or payload.get("action") not in {
        "created",
        "opened",
        "edited",
        "submitted",
    }:
        return False
    field, kind, identifier = shapes[event]
    if identifier == "number" and payload.get("action") == "edited":
        changes = payload.get("changes")
        if not isinstance(changes, dict) or not {"title", "body"}.intersection(changes):
            return False
    repository = payload.get("repository")
    source = payload.get(field)
    thread = payload.get("issue") or payload.get("pull_request")
    if not all(isinstance(item, dict) for item in (repository, source, thread)):
        return False
    repository = cast(dict[str, object], repository)
    source = cast(dict[str, object], source)
    thread = cast(dict[str, object], thread)
    repository_id = repository.get("id")
    if type(repository_id) is not int or repository_id <= 0:
        return False
    inventory: RepositoryInventoryRecord | None = get_repository_inventory_read_model(
        repository_id=str(repository_id), store=cast(RepositoryInventoryReadStore, store)
    ).current_record
    if inventory is None or inventory.inventory_state != "tracked":
        return False
    hint = AttentionEventHint.model_validate(
        {
            "repository": inventory.repository,
            "source_kind": kind,
            "source_id": source.get(identifier),
            "request_number": thread.get("number"),
            "delivery_id": delivery_id,
            "received_at": received_at,
        }
    )
    notify = getattr(store, "notify_request_attention", None)
    if not callable(notify):
        return False
    notify(hint.model_dump_json())
    return True


async def attention_event_lines(database_url: str) -> AsyncGenerator[bytes, None]:
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise ValueError("Attention subscriptions require PostgreSQL.")
    async with await psycopg.AsyncConnection.connect(
        url.set(drivername="postgresql").render_as_string(hide_password=False),
        autocommit=True,
        connect_timeout=5,
    ) as connection:
        await connection.execute(f"LISTEN {ATTENTION_EVENT_CHANNEL}")
        yield (
            json.dumps({"type": "connected", "observed_at": utc_now_timestamp()}) + "\n"
        ).encode()
        deadline = asyncio.get_running_loop().time() + STREAM_SECONDS
        while (remaining := deadline - asyncio.get_running_loop().time()) > 0:
            async for notification in connection.notifies(timeout=min(10, remaining), stop_after=1):
                hint = AttentionEventHint.model_validate_json(notification.payload)
                yield (hint.model_dump_json() + "\n").encode()
            yield (
                json.dumps({"type": "heartbeat", "observed_at": utc_now_timestamp()}) + "\n"
            ).encode()
