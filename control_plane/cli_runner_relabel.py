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
    AuditPoster,
    build_refreshing_service_audit_poster,
    dry_run_audit_poster,
    validate_ops_lane_environment,
)
from control_plane.workflows.runner_lane_relabel import execute_relabel, deliver_terminal_audit
from control_plane.contracts.runner_lane_registration import RunnerLaneRegistrationAuditRecord
from control_plane.merge_train_github import MergeTrainGitHubError


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
@click.option("--audit-packet-file", type=click.Path(path_type=Path, exists=True, dir_okay=False))
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
    audit_packet_file: Path | None,
) -> None:
    """Replace custom labels on one reviewed existing runner, without restarting it."""
    from control_plane.cli_runner_lanes import _launchplane_service_bearer_token

    try:
        request = RunnerLaneRegistrationRequest.model_validate_json(request_file.read_text())
        if request.operation != "relabel" or request.mutate != mutate:
            raise ValueError("request operation/mutate must match the explicit relabel invocation")
        if mutate and not service_url:
            raise ValueError("mutating relabel requires service-backed audit")
        if (
            request.repository != allowed_repository.lower()
            or request.host_name != approved_host.lower()
            or request.registration_root != allowed_registration_root
        ):
            raise ValueError("request does not match the approved repository/host/root")
        if mutate:
            if socket.gethostname().lower().split(".", 1)[0] != request.host_name.split(".", 1)[0]:
                raise ValueError("runner host identity does not match")
            validate_ops_lane_environment(service_user=service_user, execution_lane=execution_lane)
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
        if audit_packet_file is not None:
            if not mutate:
                raise ValueError("retained audit delivery requires explicit mutate")
            packet = json.loads(audit_packet_file.read_text())
            if not isinstance(packet, dict):
                raise ValueError("retained packet must be an object")
            terminal = RunnerLaneRegistrationAuditRecord.model_validate(
                packet.get("terminal_audit")
            )
            if terminal.request != request:
                raise ValueError("retained audit does not match the reviewed request")
            result = deliver_terminal_audit(packet, audit_poster)
        else:
            token = os.environ.get("GITHUB_TOKEN", "").strip()
            if not token:
                raise ValueError("missing existing GITHUB_TOKEN for runner administration")
            transport = UrllibMergeTrainGitHubTransport(token=token)
            reader = GitHubRunnerLaneInventoryReader(transport=transport)
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
    except (OSError, ValueError, MergeTrainGitHubError):
        # Do not echo authentication/transport payloads or caller-controlled JSON.
        raise click.ClickException(
            "Runner relabel preflight failed; no label write was attempted."
        ) from None
    click.echo(json.dumps(result, indent=2))
    if result["status"] not in ("dry_run", "completed", "audit_delivered"):
        raise click.exceptions.Exit(1)
