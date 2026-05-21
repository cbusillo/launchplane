from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from control_plane.contracts.every_code_work_request import EveryCodeWorkRequestRecord
from control_plane.contracts.product_environment_read_model import (
    ActionAllowed,
    ProductReadModelStore,
    build_product_site_overviews,
)
from control_plane.contracts.repo_product_mapping_read_model import (
    RepoProductMappingProductStore,
    RepoProductMappingWorkRequestStore,
    build_repo_product_mapping_from_records,
)
from control_plane.contracts.work_graph_read_model import (
    WorkGraphPlanningIssueFacts,
    WorkGraphSnapshot,
    build_work_graph_queue,
    build_work_graph_snapshot_from_repo_mapping,
)
from control_plane.work_graph_issue_inbox import GitHubIssueInboxReadModel
from control_plane.work_graph_issue_inbox import (
    GitHubIssueInboxReconcileRequest,
    GitHubIssueInboxReconcileResult,
)


WorkGraphPlanningFactsProvider = Callable[[], tuple[WorkGraphPlanningIssueFacts, ...]]
WorkGraphIssueInboxProvider = Callable[[], GitHubIssueInboxReadModel]
WorkGraphIssueInboxReconcileProvider = Callable[
    [GitHubIssueInboxReconcileRequest], GitHubIssueInboxReconcileResult
]


class WorkGraphWorkRequestStore(Protocol):
    def list_every_code_work_request_records(
        self,
        *,
        state: str = "",
        repository: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[EveryCodeWorkRequestRecord, ...]: ...


class WorkGraphRankEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    snapshot: WorkGraphSnapshot
    limit: int = Field(default=25, ge=1, le=100)


def build_work_graph_snapshot_service_payload(
    *,
    generated_at: str,
    product_store: ProductReadModelStore,
    work_request_store: WorkGraphWorkRequestStore,
    action_allowed: ActionAllowed,
    planning_facts_provider: WorkGraphPlanningFactsProvider | None,
) -> dict[str, object]:
    work_requests = work_request_store.list_every_code_work_request_records(limit=None)
    product_overviews = build_product_site_overviews(
        record_store=product_store,
        action_allowed=action_allowed,
    )
    readable_product_profiles = tuple(
        profile
        for profile in product_store.list_product_profile_records()
        if action_allowed("product_environment.read", profile.product, "launchplane")
    )
    planning_issue_facts = planning_facts_provider() if planning_facts_provider is not None else ()
    repo_mapping = build_repo_product_mapping_from_records(
        generated_at=generated_at,
        product_profiles=readable_product_profiles,
        work_requests=work_requests,
    )
    snapshot = build_work_graph_snapshot_from_repo_mapping(
        generated_at=generated_at,
        repo_mapping=repo_mapping,
        work_requests=work_requests,
        planning_issue_facts=planning_issue_facts,
    )
    return {
        "snapshot": snapshot.model_dump(mode="json"),
        "source": {
            "product_count": len(product_overviews),
            "work_request_count": len(work_requests),
            "planning_fact_count": len(planning_issue_facts),
        },
    }


def build_repo_product_mapping_service_payload(
    *,
    generated_at: str,
    product_store: RepoProductMappingProductStore,
    work_request_store: RepoProductMappingWorkRequestStore,
) -> dict[str, object]:
    product_profiles = product_store.list_product_profile_records()
    work_requests = work_request_store.list_every_code_work_request_records(limit=None)
    mapping = build_repo_product_mapping_from_records(
        generated_at=generated_at,
        product_profiles=product_profiles,
        work_requests=work_requests,
    )
    return {
        "mapping": mapping.model_dump(mode="json"),
        "source": {
            "product_count": len(product_profiles),
            "work_request_count": len(work_requests),
        },
    }


def build_work_graph_rank_result(
    rank_request: WorkGraphRankEnvelope,
) -> tuple[dict[str, object], dict[str, object]]:
    queue = build_work_graph_queue(
        rank_request.snapshot,
        limit=rank_request.limit,
    )
    return (
        {"item_count": len(queue.items), "hidden_count": queue.hidden_count},
        {"queue": queue.model_dump(mode="json")},
    )
