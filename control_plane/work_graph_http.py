from __future__ import annotations

from collections.abc import Callable
from typing import TypeAlias
from control_plane.service_auth import (
    GitHubActionsIdentity,
    GitHubHumanIdentity,
    LaunchplaneAuthzPolicy,
    LocalAdminIdentity,
    LocalOperatorIdentity,
    TerminalAgentIdentity,
)
from control_plane.work_graph_service import (
    WorkGraphIssueInboxReconcileProvider,
)
from control_plane.work_graph_issue_inbox import (
    GitHubIssueInboxReconcileRequest,
    GitHubIssueInboxReconcileResult,
)


JsonResponse = Callable[..., list[bytes]]
StartResponse = Callable[[str, list[tuple[str, str]]], None]
ResolvedLaunchplaneIdentity: TypeAlias = (
    GitHubActionsIdentity
    | GitHubHumanIdentity
    | TerminalAgentIdentity
    | LocalOperatorIdentity
    | LocalAdminIdentity
)

LAUNCHPLANE_SERVICE_CONTEXT = "launchplane"


def reconcile_work_graph_issue_inbox(
    *,
    authz_policy: LaunchplaneAuthzPolicy,
    identity: ResolvedLaunchplaneIdentity,
    payload: dict[str, object],
    issue_inbox_reconcile_provider: WorkGraphIssueInboxReconcileProvider | None,
) -> GitHubIssueInboxReconcileResult | None:
    request = GitHubIssueInboxReconcileRequest.model_validate(payload)
    required_action = (
        "work_graph.rank" if request.mode == "dry_run" else "work_graph.issue_inbox.reconcile"
    )
    if not authz_policy.allows(
        identity=identity,
        action=required_action,
        product="launchplane",
        context=LAUNCHPLANE_SERVICE_CONTEXT,
    ):
        return None
    if issue_inbox_reconcile_provider is None:
        raise ValueError("GitHub issue inbox reconciliation is not configured.")
    return issue_inbox_reconcile_provider(request)


def work_graph_issue_inbox_reconcile_denied_response(
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
                "message": "Workflow cannot reconcile the Launchplane GitHub issue inbox.",
            },
        },
    )
