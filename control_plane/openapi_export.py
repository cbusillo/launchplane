from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import (
    BearerIdentityConfig,
    GitHubActionsIdentity,
    GitHubHumanIdentity,
    LaunchplaneAuthzPolicy,
    TokenVerifier,
)
from control_plane.service_human_auth import (
    GitHubOAuthConfig,
    HumanSessionManager,
    InMemoryHumanSessionStore,
)


class _DeterministicOpenApiTokenVerifier(TokenVerifier):
    def verify(self, token: str) -> GitHubActionsIdentity:
        raise ValueError("Deterministic OpenAPI export does not verify live credentials.")


class _DeterministicGitHubOAuthLoginClient:
    def authorization_url(self, *, state: str, code_challenge: str) -> str:
        return (
            "https://github.example.invalid/login/oauth/authorize"
            f"?state={state}&code_challenge={code_challenge}"
        )

    def fetch_identity(
        self,
        *,
        code: str,
        code_verifier: str,
        authz_policy: LaunchplaneAuthzPolicy,
    ) -> GitHubHumanIdentity:
        return GitHubHumanIdentity(
            login="launchplane-docs",
            github_id=1,
            name="Launchplane Docs",
            email="docs@example.invalid",
            organizations=frozenset({"launchplane"}),
            teams=frozenset({"docs"}),
            role="read_only",
        )


def _deterministic_webhook_handler(
    _raw_body: bytes,
    _signature_header: str,
    _event_name: str,
    _delivery_id: str,
    _record_store: object,
    _state_dir: Path,
    _trigger_label: str,
) -> tuple[int, dict[str, object]]:
    return (
        202,
        {
            "status": "accepted",
            "trace_id": "launchplane_req_00000000000000000000000000000000",
        },
    )


def _build_export_app() -> FastAPI:
    human_session_manager = HumanSessionManager(
        config=GitHubOAuthConfig(
            client_id="launchplane-docs-client-id",
            client_secret="launchplane-docs-client-secret",
            public_url="https://launchplane.example.invalid",
            session_secret="launchplane-docs-session-secret",
            cookie_secure=False,
            bootstrap_admin_emails=frozenset({"docs@example.invalid"}),
        ),
        session_store=InMemoryHumanSessionStore(),
    )
    return create_launchplane_fastapi_app(
        verifier=_DeterministicOpenApiTokenVerifier(),
        authz_policy=LaunchplaneAuthzPolicy(),
        record_store_factory=lambda: object(),
        bearer_identity_config=BearerIdentityConfig(
            every_code_worker_token="deterministic-worker-token",
            local_admin_token="deterministic-local-admin-token",
            local_admin_subject="launchplane-docs-admin",
            local_admin_token_label="docs-admin",
            local_operator_token="deterministic-local-operator-token",
            local_operator_subject="launchplane-docs-operator",
            local_operator_token_label="docs-operator",
            terminal_agent_token="deterministic-terminal-agent-token",
            terminal_agent_subject="launchplane-docs-terminal-agent",
            terminal_agent_token_label="docs-terminal-agent",
        ),
        human_session_manager=human_session_manager,
        github_oauth_client=_DeterministicGitHubOAuthLoginClient(),
        every_code_github_webhook_handler=_deterministic_webhook_handler,
    )


def build_deterministic_export_app() -> FastAPI:
    return _build_export_app()


def _scrub_openapi_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        scrubbed: dict[str, Any] = {}
        for key, nested_value in value.items():
            if key in {"example", "examples"}:
                continue
            scrubbed[str(key)] = _scrub_openapi_payload(nested_value)
        return scrubbed
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_scrub_openapi_payload(item) for item in value]
    return value


def canonical_openapi_document() -> dict[str, Any]:
    app = build_deterministic_export_app()
    openapi_payload: dict[str, Any] = app.openapi()
    scrubbed_payload: dict[str, Any] = _scrub_openapi_payload(openapi_payload)
    info = scrubbed_payload.get("info", {})
    if not isinstance(info, dict):
        info = {}
    scrubbed_payload["info"] = {
        **info,
        "title": "Launchplane API",
        "version": "0.1.0",
    }
    scrubbed_payload["x-launchplane-ui-read-operations"] = UI_OPENAPI_READ_OPERATIONS
    scrubbed_payload["x-launchplane-ui-write-operations"] = UI_OPENAPI_WRITE_OPERATIONS
    return scrubbed_payload


def write_canonical_openapi(output_path: Path) -> Path:
    payload = canonical_openapi_document()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output_path


UI_OPENAPI_READ_OPERATIONS: dict[str, str] = {
    "/v1/auth/session": "read_human_auth_session",
    "/v1/drivers": "read_driver_descriptors",
    "/v1/contexts/{context}/driver-view": "read_driver_context_view",
    "/v1/contexts/{context}/instances/{instance}/driver-view": "read_driver_instance_view",
    "/v1/product-profiles": "list_product_profiles",
    "/v1/products": "list_products",
    "/v1/products/{product}": "read_product",
    "/v1/products/{product}/activity": "read_product_activity",
    "/v1/products/{product}/environments": "list_product_environments",
    "/v1/products/{product}/environments/{environment}": "read_product_environment",
    "/v1/products/{product}/environments/{environment}/public-ingress/incidents": (
        "list_product_environment_public_ingress_incidents"
    ),
    "/v1/products/{product}/environments/{environment}/public-ingress/incidents/{incident_id}": (
        "read_product_environment_public_ingress_incident"
    ),
    "/v1/products/{product}/contexts/{context}/instances/{instance}/operational-readiness": (
        "read_product_operational_readiness"
    ),
    "/v1/products/{product}/environments/{environment}/config-status": (
        "read_product_environment_config_status"
    ),
    "/v1/products/{product}/environments/{environment}/promotion-status": (
        "read_product_promotion_status"
    ),
    "/v1/products/{product}/environments/{environment}/promotion/workflow-deliveries/{delivery_id}": (
        "read_product_promotion_workflow_delivery"
    ),
    "/v1/every-code/summary": "read_every_code_summary",
    "/v1/every-code/work-requests": "list_every_code_work_requests",
    "/v1/previews/readiness": "read_preview_readiness",
    "/v1/work-graph/snapshot": "read_work_graph_snapshot",
    "/v1/repo-product-mapping": "read_repo_product_mapping",
    "/v1/work-graph/github/issues": "read_work_graph_issue_inbox",
    "/v1/work-graph/merge-train/controller/status": "read_merge_train_controller_status",
    "/v1/work-graph/merge-train/policy-targets": "read_merge_train_policy_targets",
    "/v1/work-graph/tenant-admission/evaluation": "read_tenant_admission_evaluation",
    "/v1/governance/projection": "read_governance_projection",
    "/v1/owner-acceptance/evaluation": "evaluate_owner_acceptance",
    "/v1/owner-acceptance/current-items": "list_owner_acceptance_current_items",
    "/v1/owner-acceptance/queue": "list_owner_acceptance_queue",
    "/v1/privileged-operations/plans": "list_human_privileged_operations",
    "/v1/privileged-operations/plans/{operation_id}": ("read_human_privileged_operation"),
}


UI_OPENAPI_WRITE_OPERATIONS: dict[str, str] = {
    "/v1/work-graph/rank": "rank_work_graph_snapshot",
    "/v1/products/{product}/environments/{environment}/config/apply": (
        "apply_product_environment_config"
    ),
    "/v1/products/{product}/environments/{environment}/promotion/dry-run": (
        "dry_run_product_promotion"
    ),
    "/v1/products/{product}/environments/{environment}/promotion/workflow-dispatch": (
        "dispatch_product_promotion_workflow"
    ),
    "/v1/owner-acceptance/events": "write_owner_acceptance_event",
    "/v1/privileged-operations/plans/{operation_id}/approve": (
        "approve_human_privileged_operation"
    ),
    "/v1/privileged-operations/plans/{operation_id}/revoke": ("revoke_human_privileged_operation"),
}
