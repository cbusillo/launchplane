from datetime import datetime, timezone
from typing import Literal, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.repository_inventory import RepositoryInventoryRecord

REQUEST_PAGE_LIMIT = 100
SUPERVISOR_ACTOR = "agent:Supervisor"


class CollectedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str
    source_url: str
    source_kind: Literal["request", "change"]
    title: str
    requester: str
    next_actor: str | None = None
    next_action: str = "Triage the original request."
    canonical_source_ids: tuple[str, ...] = ()
    created_at: AwareDatetime
    open: bool


class OpenRequestPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    requests: tuple[CollectedRequest, ...]
    has_more: bool


class AttentionSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    repository: str
    products: tuple[str, ...]
    available: bool


class WaitingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str
    source_url: str
    source_kind: Literal["request", "change"]
    repository: str
    title: str
    products: tuple[str, ...]
    requester: str
    owning_agent: str | None
    next_actor: str
    next_action: str
    created_at: AwareDatetime
    age_seconds: int = Field(ge=0)


class RequestAttentionPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    observed_at: str
    sources: tuple[AttentionSource, ...]
    requests: tuple[WaitingRequest, ...] = ()
    truncated: bool = False


class RequestAttentionStore(Protocol):
    def list_repository_inventory_records(
        self, *, repository_id: str = "", limit: int | None = None
    ) -> tuple[RepositoryInventoryRecord, ...]: ...

    def list_product_profile_records(self) -> tuple[LaunchplaneProductProfileRecord, ...]: ...


class OpenRequestReader(Protocol):
    def __call__(
        self, repository: str, *, trusted_author_ids: tuple[int, ...]
    ) -> OpenRequestPage: ...


def attention_sources(store: RequestAttentionStore) -> tuple[AttentionSource, ...]:
    latest: dict[str, RepositoryInventoryRecord] = {}
    for record in store.list_repository_inventory_records():
        previous = latest.get(record.repository_id)
        if previous is None or record.inventory_revision > previous.inventory_revision:
            latest[record.repository_id] = record
    tracked = {
        record.repository for record in latest.values() if record.inventory_state == "tracked"
    }
    products: dict[str, list[str]] = {}
    for profile in store.list_product_profile_records():
        if profile.lifecycle_state != "retired":
            products.setdefault(profile.repository.casefold(), []).append(profile.display_name)
    return tuple(
        AttentionSource(
            repository=repository,
            products=tuple(sorted(products.get(repository, ()))),
            available=repository in tracked,
        )
        for repository in sorted(tracked | products.keys())
    )


def build_request_attention_page(
    *,
    sources: tuple[AttentionSource, ...],
    repository: str,
    trusted_author_ids: tuple[int, ...] = (),
    read: OpenRequestReader,
    trace_id: str,
    observed_at: str,
) -> RequestAttentionPage:
    result = RequestAttentionPage(trace_id=trace_id, observed_at=observed_at, sources=sources)
    if not repository:
        return result
    source = next((source for source in sources if source.repository == repository), None)
    if source is None or not source.available:
        raise LookupError("Request source is not in tracked inventory.")
    source_page = read(repository, trusted_author_ids=trusted_author_ids)
    now = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    requests: dict[str, WaitingRequest] = {}
    open_ids = {row.source_id for row in source_page.requests if row.open}
    for row in source_page.requests:
        if not row.open or open_ids.intersection(row.canonical_source_ids):
            continue
        actor, action = row.next_actor, row.next_action
        if actor is None or actor == "agent" or actor.casefold() == SUPERVISOR_ACTOR.casefold():
            actor = SUPERVISOR_ACTOR
        assigned = actor.removeprefix("agent:") if actor and actor.startswith("agent:") else None
        request = WaitingRequest(
            source_id=row.source_id,
            source_url=row.source_url,
            source_kind=row.source_kind,
            repository=repository,
            title=row.title,
            products=source.products,
            requester=row.requester,
            owning_agent=assigned if actor != SUPERVISOR_ACTOR else None,
            next_actor=actor,
            next_action=action,
            created_at=row.created_at,
            age_seconds=max(
                0, int((now.astimezone(timezone.utc) - row.created_at).total_seconds())
            ),
        )
        requests[request.source_id] = request
    more = source_page.has_more
    return result.model_copy(
        update={
            "requests": tuple(requests.values()),
            "truncated": more,
        }
    )
