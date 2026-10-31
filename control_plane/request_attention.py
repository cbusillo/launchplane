from datetime import datetime, timezone
from typing import Literal, Protocol

from github_plan_status import next_actor
from github_plan_status.body import contributor_plan_body
from github_plan_status.waits import live_lines
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.repository_inventory import RepositoryInventoryRecord

REQUEST_PAGE_LIMIT = 100


class CollectedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str
    source_url: str
    source_kind: Literal["request", "change"]
    title: str
    requester: str
    body: str
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
    next_page: int | None = None
    truncated: bool = False


class RequestAttentionStore(Protocol):
    def list_repository_inventory_records(
        self, *, repository_id: str = "", limit: int | None = None
    ) -> tuple[RepositoryInventoryRecord, ...]: ...

    def list_product_profile_records(self) -> tuple[LaunchplaneProductProfileRecord, ...]: ...


class OpenRequestReader(Protocol):
    def __call__(self, repository: str, page: int) -> OpenRequestPage: ...


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


def request_status(body: str) -> tuple[str | None, str]:
    managed = contributor_plan_body({"body": body})
    status_lines: list[str] = []
    in_status = False
    for _, line in live_lines(managed if managed is not None else body, prefix=""):
        if line.startswith("## "):
            in_status = line.strip().casefold() == "## current status"
        elif in_status:
            status_lines.append(line)
    status = "\n".join(status_lines)
    actor = next_actor.read(status)
    actions = [
        line.removeprefix("Next action:").strip()
        for line in status_lines
        if line.startswith("Next action:")
    ]
    action = actions[0] if len(actions) == 1 and actions[0] else "Triage the original request."
    return actor, action


def build_request_attention_page(
    *,
    sources: tuple[AttentionSource, ...],
    repository: str,
    page: int,
    read: OpenRequestReader,
    trace_id: str,
    observed_at: str,
    max_pages: int = REQUEST_PAGE_LIMIT,
) -> RequestAttentionPage:
    result = RequestAttentionPage(trace_id=trace_id, observed_at=observed_at, sources=sources)
    if not repository:
        return result
    source = next((source for source in sources if source.repository == repository), None)
    if source is None or not source.available:
        raise LookupError("Request source is not in tracked inventory.")
    source_page = read(repository, page)
    now = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    requests: dict[str, WaitingRequest] = {}
    for row in source_page.requests:
        if not row.open:
            continue
        try:
            actor, action = request_status(row.body)
        except ValueError:
            actor, action = None, "Review the original request's ambiguous Current Status."
        assigned = actor.removeprefix("agent:") if actor and actor.startswith("agent:") else None
        request = WaitingRequest(
            source_id=row.source_id,
            source_url=row.source_url,
            source_kind=row.source_kind,
            repository=repository,
            title=row.title,
            products=source.products,
            requester=row.requester,
            owning_agent=assigned if assigned and assigned.casefold() != "supervisor" else None,
            next_actor=actor or "agent:Supervisor",
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
            "next_page": page + 1 if more and page < max_pages else None,
            "truncated": more and page >= max_pages,
        }
    )
