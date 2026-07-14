from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
import os
from pathlib import Path

import click
from fastapi import FastAPI
import uvicorn

from control_plane.drivers import native_routes
from control_plane.every_code_github_webhook import handle_every_code_github_webhook_request
from control_plane.http_app import (
    LaunchplaneAuthzPolicyRuntime,
    create_launchplane_fastapi_app,
    resolve_launchplane_authz_policy,
)
from control_plane.launchplane_mutations import control_plane_root
from control_plane.service_auth import (
    BearerIdentityConfig,
    GitHubOidcVerifier,
    LaunchplaneAuthzPolicy,
    TokenVerifier,
    load_authz_policy,
)
from control_plane.service_human_auth import (
    GitHubOAuthClient,
    HumanSessionManager,
    OAuthLoginStateStore,
    load_github_oauth_config_from_env,
)
from control_plane.storage.factory import build_shared_record_store
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.work_graph_github_projects import (
    build_github_project_planning_facts,
    load_github_project_planning_facts_config_from_env,
)
from control_plane.work_graph_issue_inbox import (
    build_github_issue_inbox_read_model,
    load_github_issue_inbox_config_from_env,
    reconcile_github_issue_inbox,
)


def _utc_now_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _now_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _every_code_worker_token_from_env() -> str:
    return os.environ.get("LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN", "").strip()


def _terminal_agent_read_token_from_env() -> str:
    return os.environ.get("LAUNCHPLANE_TERMINAL_AGENT_READ_TOKEN", "").strip()


def _terminal_agent_subject_from_env() -> str:
    return os.environ.get("LAUNCHPLANE_TERMINAL_AGENT_SUBJECT", "").strip()


def _terminal_agent_token_label_from_env() -> str:
    return os.environ.get("LAUNCHPLANE_TERMINAL_AGENT_TOKEN_LABEL", "").strip()


def _local_operator_token_from_env() -> str:
    return os.environ.get("LAUNCHPLANE_LOCAL_OPERATOR_TOKEN", "").strip()


def _local_operator_subject_from_env() -> str:
    return os.environ.get("LAUNCHPLANE_LOCAL_OPERATOR_SUBJECT", "").strip()


def _local_operator_token_label_from_env() -> str:
    return os.environ.get("LAUNCHPLANE_LOCAL_OPERATOR_TOKEN_LABEL", "").strip()


def _local_admin_token_from_env() -> str:
    return os.environ.get("LAUNCHPLANE_LOCAL_ADMIN_TOKEN", "").strip()


def _local_admin_subject_from_env() -> str:
    return os.environ.get("LAUNCHPLANE_LOCAL_ADMIN_SUBJECT", "").strip()


def _local_admin_token_label_from_env() -> str:
    return os.environ.get("LAUNCHPLANE_LOCAL_ADMIN_TOKEN_LABEL", "").strip()


def _bearer_identity_config_from_env() -> BearerIdentityConfig:
    return BearerIdentityConfig(
        every_code_worker_token=_every_code_worker_token_from_env(),
        local_admin_token=_local_admin_token_from_env(),
        local_admin_subject=_local_admin_subject_from_env(),
        local_admin_token_label=_local_admin_token_label_from_env(),
        local_operator_token=_local_operator_token_from_env(),
        local_operator_subject=_local_operator_subject_from_env(),
        local_operator_token_label=_local_operator_token_label_from_env(),
        terminal_agent_token=_terminal_agent_read_token_from_env(),
        terminal_agent_subject=_terminal_agent_subject_from_env(),
        terminal_agent_token_label=_terminal_agent_token_label_from_env(),
    )


def _bootstrap_policy_source_from_env() -> str:
    if os.environ.get("LAUNCHPLANE_POLICY_TOML", "").strip():
        return "bootstrap-env:LAUNCHPLANE_POLICY_TOML"
    if os.environ.get("LAUNCHPLANE_POLICY_B64", "").strip():
        return "bootstrap-env:LAUNCHPLANE_POLICY_B64"
    if os.environ.get("LAUNCHPLANE_POLICY_FILE", "").strip():
        return "bootstrap-env:LAUNCHPLANE_POLICY_FILE"
    return "bootstrap-policy"


def create_launchplane_service_application(
    *,
    state_dir: Path,
    bootstrap_authz_policy: LaunchplaneAuthzPolicy,
    verifier: TokenVerifier,
    service_record_store: PostgresRecordStore,
    control_plane_root_path: Path | None = None,
) -> FastAPI:
    resolved_fastapi_policy = resolve_launchplane_authz_policy(
        record_store=service_record_store,
        bootstrap_policy=bootstrap_authz_policy,
        policy_source=_bootstrap_policy_source_from_env(),
        now_timestamp=_now_timestamp(),
    )
    authz_policy_runtime = LaunchplaneAuthzPolicyRuntime(
        resolved_fastapi_policy.policy,
        policy_sha256=resolved_fastapi_policy.policy_sha256,
        source=resolved_fastapi_policy.source,
    )
    work_graph_project_config = load_github_project_planning_facts_config_from_env(dict(os.environ))
    work_graph_planning_facts_provider = (
        (lambda: build_github_project_planning_facts(work_graph_project_config))
        if work_graph_project_config is not None
        else None
    )
    work_graph_issue_inbox_config = load_github_issue_inbox_config_from_env(
        dict(os.environ),
        project_config=work_graph_project_config,
    )
    work_graph_issue_inbox_provider = (
        (
            lambda: build_github_issue_inbox_read_model(
                generated_at=_utc_now_timestamp(),
                config=work_graph_issue_inbox_config,
            )
        )
        if work_graph_issue_inbox_config is not None
        else None
    )
    work_graph_issue_inbox_reconcile_provider = (
        (
            lambda request: reconcile_github_issue_inbox(
                generated_at=_utc_now_timestamp(),
                config=work_graph_issue_inbox_config,
                request=request,
            )
        )
        if work_graph_issue_inbox_config is not None
        else None
    )
    github_oauth_config = load_github_oauth_config_from_env()
    human_session_manager = (
        HumanSessionManager(
            config=github_oauth_config,
            session_store=service_record_store,
        )
        if github_oauth_config is not None
        else None
    )
    github_oauth_client = (
        GitHubOAuthClient(github_oauth_config) if github_oauth_config is not None else None
    )
    oauth_login_state_store = OAuthLoginStateStore()
    return create_launchplane_fastapi_app(
        verifier=verifier,
        authz_policy=resolved_fastapi_policy.policy,
        authz_policy_runtime=authz_policy_runtime,
        record_store_factory=lambda: service_record_store,
        bearer_identity_config=_bearer_identity_config_from_env(),
        human_session_manager=human_session_manager,
        github_oauth_client=github_oauth_client,
        oauth_login_state_store=oauth_login_state_store,
        control_plane_root_path=control_plane_root_path or control_plane_root(),
        state_dir=state_dir,
        work_graph_planning_facts_provider=work_graph_planning_facts_provider,
        work_graph_issue_inbox_provider=work_graph_issue_inbox_provider,
        work_graph_issue_inbox_reconcile_provider=work_graph_issue_inbox_reconcile_provider,
        every_code_github_webhook_handler=handle_every_code_github_webhook_request,
    )


def serve_launchplane_service(
    *,
    state_dir: Path,
    policy_file: Path,
    host: str,
    port: int,
    audience: str,
    database_url: str | None = None,
) -> None:
    with ExitStack() as cleanup:
        bootstrap_authz_policy = load_authz_policy(policy_file)
        verifier = GitHubOidcVerifier(audience=audience)
        native_routes._validate_native_descriptor_driver_routes()
        service_record_store = build_shared_record_store(database_url=database_url)
        cleanup.callback(service_record_store.close)
        fastapi_application = create_launchplane_service_application(
            state_dir=state_dir,
            bootstrap_authz_policy=bootstrap_authz_policy,
            verifier=verifier,
            service_record_store=service_record_store,
        )
        native_routes._validate_native_fastapi_driver_routes(fastapi_application)
        click.echo(f"Launchplane service listening on http://{host}:{port}")
        uvicorn.run(
            fastapi_application,
            host=host,
            port=port,
            log_config=None,
        )
