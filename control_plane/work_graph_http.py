from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from control_plane.contracts.product_environment_read_model import (
    ActionAllowed,
    ProductReadModelStore,
)
from control_plane.service_auth import LaunchplaneAuthzPolicy, LaunchplaneIdentity
from control_plane.work_graph_service import (
    WorkGraphPlanningFactsProvider,
    WorkGraphRankEnvelope,
    WorkGraphWorkRequestStore,
    build_repo_product_mapping_service_payload,
    build_work_graph_rank_result,
    build_work_graph_snapshot_service_payload,
)


JsonResponse = Callable[..., list[bytes]]
StartResponse = Callable[[str, list[tuple[str, str]]], None]
UtcNow = Callable[[], str]

LAUNCHPLANE_SERVICE_CONTEXT = "launchplane"


@dataclass(frozen=True)
class WorkGraphRankResult:
    result: dict[str, object]
    driver_result: dict[str, object]


def handle_work_graph_snapshot_read(
    *,
    authz_policy: LaunchplaneAuthzPolicy,
    identity: LaunchplaneIdentity,
    trace_id: str,
    product_store: ProductReadModelStore,
    work_request_store: WorkGraphWorkRequestStore,
    planning_facts_provider: WorkGraphPlanningFactsProvider | None,
    utc_now: UtcNow,
    json_response: JsonResponse,
    start_response: StartResponse,
) -> list[bytes]:
    if not authz_policy.allows(
        identity=identity,
        action="work_graph.rank",
        product="launchplane",
        context=LAUNCHPLANE_SERVICE_CONTEXT,
    ):
        return json_response(
            start_response=start_response,
            status_code=403,
            payload={
                "status": "rejected",
                "trace_id": trace_id,
                "error": {
                    "code": "authorization_denied",
                    "message": "Workflow cannot read the Launchplane work graph snapshot.",
                },
            },
        )

    work_graph_payload = build_work_graph_snapshot_service_payload(
        generated_at=utc_now(),
        product_store=product_store,
        work_request_store=work_request_store,
        action_allowed=_work_graph_product_action_allowed(
            authz_policy=authz_policy,
            identity=identity,
        ),
        planning_facts_provider=planning_facts_provider,
    )
    return json_response(
        start_response=start_response,
        status_code=200,
        payload={
            "status": "ok",
            "trace_id": trace_id,
            **work_graph_payload,
        },
    )


def handle_repo_product_mapping_read(
    *,
    authz_policy: LaunchplaneAuthzPolicy,
    identity: LaunchplaneIdentity,
    trace_id: str,
    product_store: ProductReadModelStore,
    work_request_store: WorkGraphWorkRequestStore,
    utc_now: UtcNow,
    json_response: JsonResponse,
    start_response: StartResponse,
) -> list[bytes]:
    if not authz_policy.allows(
        identity=identity,
        action="product_environment.read",
        product="launchplane",
        context=LAUNCHPLANE_SERVICE_CONTEXT,
    ):
        return json_response(
            start_response=start_response,
            status_code=403,
            payload={
                "status": "rejected",
                "trace_id": trace_id,
                "error": {
                    "code": "authorization_denied",
                    "message": "Workflow cannot read the Launchplane repo product mapping.",
                },
            },
        )

    mapping_payload = build_repo_product_mapping_service_payload(
        generated_at=utc_now(),
        product_store=product_store,
        work_request_store=work_request_store,
    )
    return json_response(
        start_response=start_response,
        status_code=200,
        payload={
            "status": "ok",
            "trace_id": trace_id,
            **mapping_payload,
        },
    )


def rank_work_graph_snapshot(
    *,
    authz_policy: LaunchplaneAuthzPolicy,
    identity: LaunchplaneIdentity,
    payload: dict[str, object],
) -> WorkGraphRankResult | None:
    rank_request = WorkGraphRankEnvelope.model_validate(payload)
    if not authz_policy.allows(
        identity=identity,
        action="work_graph.rank",
        product="launchplane",
        context=LAUNCHPLANE_SERVICE_CONTEXT,
    ):
        return None
    result, driver_result = build_work_graph_rank_result(rank_request)
    return WorkGraphRankResult(result=result, driver_result=driver_result)


def work_graph_rank_denied_response(
    *,
    trace_id: str,
    json_response: JsonResponse,
    start_response: StartResponse,
) -> list[bytes]:
    return json_response(
        start_response=start_response,
        status_code=403,
        payload={
            "status": "rejected",
            "trace_id": trace_id,
            "error": {
                "code": "authorization_denied",
                "message": "Workflow cannot rank Launchplane work graph snapshots.",
            },
        },
    )


def _work_graph_product_action_allowed(
    *,
    authz_policy: LaunchplaneAuthzPolicy,
    identity: LaunchplaneIdentity,
) -> ActionAllowed:
    def action_allowed(requested_action: str, requested_product: str, requested_context: str) -> bool:
        return authz_policy.allows(
            identity=identity,
            action=requested_action,
            product=requested_product,
            context=requested_context,
        )

    return action_allowed
