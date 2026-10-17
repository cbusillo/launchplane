from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import click

from control_plane.contracts.runner_lane_registration import RunnerLaneRegistrationPolicy
from control_plane.contracts.runner_lane_registration import RunnerLaneRegistrationRequest
from control_plane.merge_train_github import UrllibMergeTrainGitHubTransport
from control_plane.runner_lane_github import GitHubRunnerLaneInventoryReader
from control_plane.workflows.runner_lane_registration_executor import (
    RunnerLaneRegistrationExecutorRequest,
    AuditPoster,
    build_refreshing_service_audit_poster,
    dry_run_audit_poster,
    validate_local_executor_environment,
)
from control_plane.workflows.runner_lane_relabel import execute_relabel


@click.command("runner-lane-relabel-executor")
@click.option(
    "--request-file", type=click.Path(path_type=Path, exists=True, dir_okay=False), required=True
)
@click.option("--allowed-repository", required=True)
@click.option("--approved-host", required=True)
@click.option("--allowed-registration-root", required=True)
@click.option("--execution-lane", required=True)
@click.option("--service-user", required=True)
@click.option("--service-url", default="")
@click.option("--mutate/--dry-run", default=False)
def runner_lane_relabel_executor(
    request_file: Path,
    allowed_repository: str,
    approved_host: str,
    allowed_registration_root: str,
    execution_lane: str,
    service_user: str,
    service_url: str,
    mutate: bool,
) -> None:
    """Replace custom labels on one reviewed existing runner, without restarting it."""
    from control_plane.cli_runner_lanes import _launchplane_service_bearer_token

    try:
        request = RunnerLaneRegistrationRequest.model_validate_json(request_file.read_text())
        if request.operation != "relabel" or request.mutate != mutate:
            raise ValueError("request operation/mutate must match the explicit relabel invocation")
        if mutate and not service_url:
            raise ValueError("mutating relabel requires service-backed audit")
        if mutate:
            if socket.gethostname().lower() != request.host_name:
                raise ValueError("runner host identity does not match")
            validate_local_executor_environment(
                request=RunnerLaneRegistrationExecutorRequest(
                    repository=request.repository,
                    host_name=request.host_name,
                    execution_lane=execution_lane,
                    service_user=service_user,
                    lane_name=request.lane_name,
                    registration_root=request.registration_root,
                    labels=request.labels,
                    mutate=True,
                    audit_record_key=request.audit_record_key,
                )
            )
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        if not token:
            raise ValueError("missing existing GITHUB_TOKEN for runner administration")
        transport = UrllibMergeTrainGitHubTransport(token=token)
        reader = GitHubRunnerLaneInventoryReader(transport=transport)
        audit_poster: AuditPoster = dry_run_audit_poster
        if mutate:
            audit_poster = build_refreshing_service_audit_poster(
                service_url=service_url,
                bearer_token_provider=lambda: _launchplane_service_bearer_token(
                    service_url=service_url,
                    token_env="LAUNCHPLANE_SERVICE_TOKEN",
                    label="runner lane relabel",
                ),
            )
        result = execute_relabel(
            request=request,
            policy=RunnerLaneRegistrationPolicy(
                allowed_repositories=(allowed_repository,),
                approved_hosts=(approved_host,),
                allowed_registration_roots=(allowed_registration_root,),
            ),
            transport=transport,
            inventory_reader=lambda: reader.read_runner_lane_inventory(
                repository=request.repository
            ),
            audit_poster=audit_poster,
            protected_lane_names=(os.environ.get("RUNNER_NAME", ""),),
            protected_labels=(execution_lane,),
        )
    except (OSError, ValueError):
        # Do not echo authentication/transport payloads or caller-controlled JSON.
        raise click.ClickException(
            "Runner relabel preflight failed; no label write was attempted."
        ) from None
    click.echo(json.dumps(result, indent=2))
    if result["status"] not in ("dry_run", "completed"):
        raise click.exceptions.Exit(1)
