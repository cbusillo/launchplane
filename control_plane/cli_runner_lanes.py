from json import JSONDecodeError
import json
import os
from pathlib import Path

import click
from pydantic import ValidationError

from control_plane.contracts.runner_lane_baseline import RunnerLaneBaselineObservation
from control_plane.contracts.runner_lane_baseline import RunnerLaneBaselinePolicy
from control_plane.contracts.runner_lane_baseline import RunnerLaneBaselineReadiness
from control_plane.contracts.runner_lane_baseline import evaluate_runner_lane_baseline
from control_plane.contracts.runner_lane_control import RunnerLaneControlAction
from control_plane.contracts.runner_lane_control import RunnerLaneControlPolicy
from control_plane.contracts.runner_lane_control import RunnerLaneControlRequest
from control_plane.contracts.runner_lane_control import plan_runner_lane_control
from control_plane.contracts.runner_lane_inventory import RunnerLaneInventory
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneObservation
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyAction
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyAuditRecord
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyPolicy
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyRequest
from control_plane.contracts.runner_host_hygiene import RunnerHostHygienePolicy
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneReport
from control_plane.contracts.runner_host_hygiene import evaluate_runner_host_hygiene
from control_plane.contracts.runner_host_hygiene import plan_runner_host_hygiene_apply
from control_plane.merge_train_github import MergeTrainGitHubError
from control_plane.merge_train_github import UrllibMergeTrainGitHubTransport
from control_plane.runner_lane_github import GitHubRunnerLaneInventoryReader
from control_plane.runner_queue_wait_github import GitHubRunnerQueueWaitReader
from control_plane.workflows.ship import utc_now_timestamp


def register_runner_lane_commands(work_graph: click.Group) -> None:
    work_graph.add_command(runner_inventory, name="runner-inventory")
    work_graph.add_command(runner_queue_wait, name="runner-queue-wait")
    work_graph.add_command(runner_baseline_observe, name="runner-baseline-observe")
    work_graph.add_command(runner_control_plan, name="runner-control-plan")
    work_graph.add_command(runner_host_hygiene_report, name="runner-host-hygiene-report")
    work_graph.add_command(runner_host_hygiene_apply_plan, name="runner-host-hygiene-apply-plan")


@click.command("runner-inventory")
@click.option(
    "--repository",
    required=True,
    help="owner/name repository whose self-hosted runner lanes should be listed.",
)
@click.option(
    "--github-token-env",
    default="GITHUB_TOKEN",
    show_default=True,
    help="Environment variable containing the GitHub token used for read-only runner inventory.",
)
@click.option(
    "--github-api-base-url",
    default="https://api.github.com",
    show_default=True,
    help="GitHub API base URL.",
)
def runner_inventory(repository: str, github_token_env: str, github_api_base_url: str) -> None:
    try:
        token_env = github_token_env.strip()
        if not token_env:
            raise click.ClickException("runner inventory requires --github-token-env.")
        token = os.environ.get(token_env, "").strip()
        if not token:
            raise click.ClickException(f"Missing GitHub token in environment variable {token_env}.")
        transport = UrllibMergeTrainGitHubTransport(token=token, api_base_url=github_api_base_url)
        inventory = GitHubRunnerLaneInventoryReader(transport=transport).read_runner_lane_inventory(
            repository=repository
        )
    except MergeTrainGitHubError as error:
        detail = str(error)
        if error.status_code is not None:
            detail = f"{detail} (HTTP {error.status_code})"
        raise click.ClickException(f"GitHub runner inventory request failed: {detail}") from error
    except (OSError, ValidationError, ValueError) as error:
        raise click.ClickException(str(error)) from error
    click.echo(json.dumps(inventory.model_dump(mode="json"), indent=2, sort_keys=True))


@click.command("runner-queue-wait")
@click.option(
    "--repository",
    required=True,
    help="owner/name repository whose recent GitHub Actions jobs should be inspected.",
)
@click.option(
    "--github-token-env",
    default="GITHUB_TOKEN",
    show_default=True,
    help="Environment variable containing the GitHub token used for read-only Actions metadata.",
)
@click.option(
    "--github-api-base-url",
    default="https://api.github.com",
    show_default=True,
    help="GitHub API base URL.",
)
@click.option(
    "--workflow-run-limit",
    default=20,
    show_default=True,
    type=click.IntRange(min=1, max=100),
    help="Recent workflow runs to inspect for job timing evidence.",
)
@click.option(
    "--constrained-threshold-seconds",
    default=300,
    show_default=True,
    type=click.IntRange(min=0),
    help="Queue-wait threshold that marks the sample as capacity constrained.",
)
@click.option(
    "--include-runner-inventory/--skip-runner-inventory",
    default=True,
    show_default=True,
    help="Also read current runner inventory so queue wait can be correlated with lane capacity.",
)
def runner_queue_wait(
    repository: str,
    github_token_env: str,
    github_api_base_url: str,
    workflow_run_limit: int,
    constrained_threshold_seconds: int,
    include_runner_inventory: bool,
) -> None:
    try:
        token_env = github_token_env.strip()
        if not token_env:
            raise click.ClickException("runner queue wait requires --github-token-env.")
        token = os.environ.get(token_env, "").strip()
        if not token:
            raise click.ClickException(f"Missing GitHub token in environment variable {token_env}.")
        transport = UrllibMergeTrainGitHubTransport(token=token, api_base_url=github_api_base_url)
        inventory: RunnerLaneInventory | None = None
        if include_runner_inventory:
            inventory = GitHubRunnerLaneInventoryReader(
                transport=transport
            ).read_runner_lane_inventory(repository=repository)
        queue_wait = GitHubRunnerQueueWaitReader(transport=transport).read_runner_queue_wait(
            repository=repository,
            workflow_run_limit=workflow_run_limit,
            constrained_threshold_seconds=constrained_threshold_seconds,
            inventory_capacity_constrained=(
                inventory.capacity_constrained if inventory is not None else None
            ),
            inventory_capacity_reason=(inventory.capacity_reason if inventory is not None else ""),
        )
    except MergeTrainGitHubError as error:
        detail = str(error)
        if error.status_code is not None:
            detail = f"{detail} (HTTP {error.status_code})"
        raise click.ClickException(f"GitHub runner queue wait request failed: {detail}") from error
    except (OSError, ValidationError, ValueError) as error:
        raise click.ClickException(str(error)) from error
    click.echo(json.dumps(queue_wait.model_dump(mode="json"), indent=2, sort_keys=True))


@click.command("runner-baseline-observe")
@click.option(
    "--runner-name",
    default="",
    help="Observed runner name. Defaults to RUNNER_NAME or RUNNER_TRACKING_ID.",
)
@click.option(
    "--label",
    "labels",
    multiple=True,
    help="Observed runner label. Repeat for each label.",
)
@click.option(
    "--docker-config-isolated/--docker-config-not-isolated",
    default=None,
    help=(
        "Whether the runner has positive per-job Docker credential isolation "
        "evidence. Defaults to true when LAUNCHPLANE_ISOLATED_DOCKER_CONFIG "
        "and DOCKER_CONFIG are both set to the same path."
    ),
)
@click.option(
    "--service-user",
    default="",
    help="Observed service user. Defaults to USER, LOGNAME, or GITHUB_ACTOR.",
)
@click.option(
    "--home-directory",
    default="",
    help="Observed home directory. Defaults to HOME.",
)
@click.option(
    "--observed-at",
    default="",
    help="Observation timestamp. Defaults to the current UTC timestamp.",
)
@click.option(
    "--required-label",
    "required_labels",
    multiple=True,
    help="Required runner label. Defaults to self-hosted and launchplane.",
)
@click.option(
    "--allowed-service-user",
    "allowed_service_users",
    multiple=True,
    help="Allowed service user. Repeat for each allowed user.",
)
@click.option(
    "--allowed-home-root",
    "allowed_home_roots",
    multiple=True,
    help="Allowed home directory root. Repeat for each allowed root.",
)
def runner_baseline_observe(
    runner_name: str,
    labels: tuple[str, ...],
    docker_config_isolated: bool | None,
    service_user: str,
    home_directory: str,
    observed_at: str,
    required_labels: tuple[str, ...],
    allowed_service_users: tuple[str, ...],
    allowed_home_roots: tuple[str, ...],
) -> None:
    try:
        observation = RunnerLaneBaselineObservation(
            runner_name=_runner_baseline_runner_name(runner_name),
            labels=_runner_baseline_labels(labels),
            docker_config_isolated=_runner_baseline_docker_config_isolated(docker_config_isolated),
            service_user=_runner_baseline_service_user(service_user),
            home_directory=_runner_baseline_home_directory(home_directory),
            observed_at=observed_at.strip() or utc_now_timestamp(),
        )
        policy = RunnerLaneBaselinePolicy(
            required_labels=required_labels or ("self-hosted", "launchplane"),
            allowed_service_users=allowed_service_users,
            allowed_home_roots=allowed_home_roots,
        )
        readiness = evaluate_runner_lane_baseline(policy=policy, observations=(observation,))
    except (ValidationError, ValueError) as error:
        raise click.ClickException(str(error)) from error
    click.echo(
        json.dumps(
            {
                "observation": observation.model_dump(mode="json"),
                "policy": policy.model_dump(mode="json"),
                "readiness": readiness.model_dump(mode="json"),
            },
            indent=2,
            sort_keys=True,
        )
    )


@click.command("runner-control-plan")
@click.option(
    "--action",
    "control_action",
    required=True,
    type=click.Choice(("create", "drain", "restart", "remove")),
    help="Runner lane control action to plan. The command never mutates hosts.",
)
@click.option("--repository", required=True, help="owner/name repository for the runner lane.")
@click.option("--lane-name", required=True, help="Runner lane name to plan against.")
@click.option(
    "--mutate/--dry-run",
    default=False,
    show_default=True,
    help="Set the request mutate intent. This command still emits only a dry-run plan.",
)
@click.option(
    "--drain-busy-lane/--do-not-drain-busy-lane",
    default=False,
    show_default=True,
    help="Whether the request explicitly permits draining a busy managed lane.",
)
@click.option(
    "--allow-create/--disallow-create",
    default=False,
    show_default=True,
    help="Enable create planning in the local policy.",
)
@click.option(
    "--allow-drain/--disallow-drain",
    default=False,
    show_default=True,
    help="Enable drain planning in the local policy.",
)
@click.option(
    "--allow-restart/--disallow-restart",
    default=False,
    show_default=True,
    help="Enable restart planning in the local policy.",
)
@click.option(
    "--allow-remove/--disallow-remove",
    default=False,
    show_default=True,
    help="Enable remove planning in the local policy.",
)
@click.option(
    "--allowed-repository",
    "allowed_repositories",
    multiple=True,
    help="Repository opted into runner lane control. Repeat for each allowed repo.",
)
@click.option(
    "--required-managed-label",
    default="launchplane-managed",
    show_default=True,
    help="Label required on existing lanes before control actions can target them.",
)
@click.option(
    "--inventory-file",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    required=True,
    help="RunnerLaneInventory JSON from runner-inventory or an equivalent fixture.",
)
@click.option(
    "--baseline-readiness-file",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    required=True,
    help="RunnerLaneBaselineReadiness JSON or runner-baseline-observe output.",
)
def runner_control_plan(
    control_action: RunnerLaneControlAction,
    repository: str,
    lane_name: str,
    mutate: bool,
    drain_busy_lane: bool,
    allow_create: bool,
    allow_drain: bool,
    allow_restart: bool,
    allow_remove: bool,
    allowed_repositories: tuple[str, ...],
    required_managed_label: str,
    inventory_file: Path,
    baseline_readiness_file: Path,
) -> None:
    try:
        policy = RunnerLaneControlPolicy(
            allowed_repositories=allowed_repositories,
            required_managed_label=required_managed_label,
            allow_create=allow_create,
            allow_drain=allow_drain,
            allow_restart=allow_restart,
            allow_remove=allow_remove,
        )
        request = RunnerLaneControlRequest(
            action=control_action,
            repository=repository,
            lane_name=lane_name,
            mutate=mutate,
            drain_busy_lane=drain_busy_lane,
        )
        inventory = _load_runner_lane_inventory(inventory_file)
        baseline_readiness = _load_runner_lane_baseline_readiness(baseline_readiness_file)
        plan = plan_runner_lane_control(
            policy=policy,
            request=request,
            inventory=inventory,
            baseline_readiness=baseline_readiness,
        )
    except (OSError, JSONDecodeError, ValidationError, ValueError) as error:
        raise click.ClickException(str(error)) from error
    click.echo(
        json.dumps(
            {
                "policy": policy.model_dump(mode="json"),
                "request": request.model_dump(mode="json"),
                "plan": plan.model_dump(mode="json"),
            },
            indent=2,
            sort_keys=True,
        )
    )


@click.command("runner-host-hygiene-report")
@click.option(
    "--observation-file",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    required=True,
    help="RunnerHostHygieneObservation JSON collected by an approved read-only probe.",
)
@click.option(
    "--policy-file",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    help="Optional RunnerHostHygienePolicy JSON. CLI policy flags are ignored when set.",
)
@click.option(
    "--minimum-free-disk-bytes",
    default=0,
    show_default=True,
    type=click.IntRange(min=0),
    help="Minimum acceptable free disk bytes for report-only evaluation.",
)
@click.option(
    "--maximum-docker-reclaimable-bytes",
    type=click.IntRange(min=0),
    help="Maximum acceptable Docker reclaimable bytes before the host needs attention.",
)
@click.option(
    "--maximum-runner-workdir-bytes",
    type=click.IntRange(min=0),
    help="Maximum acceptable runner work-directory bytes before the host needs attention.",
)
@click.option(
    "--required-warm-builder",
    "required_warm_builders",
    multiple=True,
    help="Builder name that must remain warm. Repeat for each required builder.",
)
@click.option(
    "--allow-orphan-buildkit/--forbid-orphan-buildkit",
    default=False,
    show_default=True,
    help="Whether orphan BuildKit containers or volumes are acceptable in the report.",
)
def runner_host_hygiene_report(
    observation_file: Path,
    policy_file: Path | None,
    minimum_free_disk_bytes: int,
    maximum_docker_reclaimable_bytes: int | None,
    maximum_runner_workdir_bytes: int | None,
    required_warm_builders: tuple[str, ...],
    allow_orphan_buildkit: bool,
) -> None:
    try:
        observation = _load_runner_host_hygiene_observation(observation_file)
        policy = (
            _load_runner_host_hygiene_policy(policy_file)
            if policy_file is not None
            else RunnerHostHygienePolicy(
                minimum_free_disk_bytes=minimum_free_disk_bytes,
                maximum_docker_reclaimable_bytes=maximum_docker_reclaimable_bytes,
                maximum_runner_workdir_bytes=maximum_runner_workdir_bytes,
                required_warm_builders=required_warm_builders,
                allow_orphan_buildkit=allow_orphan_buildkit,
            )
        )
        report = evaluate_runner_host_hygiene(policy=policy, observation=observation)
    except (OSError, JSONDecodeError, ValidationError, ValueError) as error:
        raise click.ClickException(str(error)) from error
    click.echo(
        json.dumps(
            {
                "mode": "report-only",
                "observation": observation.model_dump(mode="json"),
                "policy": policy.model_dump(mode="json"),
                "report": report.model_dump(mode="json"),
            },
            indent=2,
            sort_keys=True,
        )
    )


@click.command("runner-host-hygiene-apply-plan")
@click.option(
    "--action",
    "apply_action",
    required=True,
    type=click.Choice(("prune_docker_cache", "prune_runner_workdir", "restart_runner_service")),
    help="Runner host hygiene apply action to plan. The command never mutates hosts.",
)
@click.option("--host-name", required=True, help="Runner host name to plan against.")
@click.option(
    "--mutate/--dry-run",
    default=False,
    show_default=True,
    help="Record explicit mutate intent. This command still emits only a dry-run plan.",
)
@click.option(
    "--audit-record-key",
    default="",
    help="Launchplane-owned audit record key the future apply adapter must write.",
)
@click.option(
    "--approved-host",
    "approved_hosts",
    multiple=True,
    help="Host approved for this apply boundary. Repeat for each approved host.",
)
@click.option(
    "--retained-warm-builder",
    "retained_warm_builders",
    multiple=True,
    help="Warm builder the request promises to retain. Repeat for each retained builder.",
)
@click.option(
    "--required-retained-warm-builder",
    "required_retained_warm_builders",
    multiple=True,
    help="Warm builder policy requires the apply request to retain. Repeat for each builder.",
)
@click.option(
    "--allow-docker-cache-prune/--disallow-docker-cache-prune",
    default=False,
    show_default=True,
    help="Enable Docker cache prune planning in the local policy.",
)
@click.option(
    "--allow-runner-workdir-prune/--disallow-runner-workdir-prune",
    default=False,
    show_default=True,
    help="Enable runner work-directory prune planning in the local policy.",
)
@click.option(
    "--allow-runner-service-restart/--disallow-runner-service-restart",
    default=False,
    show_default=True,
    help="Enable runner service restart planning in the local policy.",
)
@click.option(
    "--require-healthy-report/--allow-attention-report",
    default=True,
    show_default=True,
    help="Require a healthy pre-apply hygiene report before planning can become ready.",
)
@click.option(
    "--require-audit-record/--allow-missing-audit-record",
    default=True,
    show_default=True,
    help="Require an audit record key before planning can become ready.",
)
@click.option(
    "--report-file",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    required=True,
    help="RunnerHostHygieneReport JSON or runner-host-hygiene-report output.",
)
def runner_host_hygiene_apply_plan(
    apply_action: RunnerHostHygieneApplyAction,
    host_name: str,
    mutate: bool,
    audit_record_key: str,
    approved_hosts: tuple[str, ...],
    retained_warm_builders: tuple[str, ...],
    required_retained_warm_builders: tuple[str, ...],
    allow_docker_cache_prune: bool,
    allow_runner_workdir_prune: bool,
    allow_runner_service_restart: bool,
    require_healthy_report: bool,
    require_audit_record: bool,
    report_file: Path,
) -> None:
    try:
        report = _load_runner_host_hygiene_report(report_file)
        policy = RunnerHostHygieneApplyPolicy(
            approved_hosts=approved_hosts,
            required_retained_warm_builders=required_retained_warm_builders,
            require_healthy_report=require_healthy_report,
            require_audit_record=require_audit_record,
            allow_docker_cache_prune=allow_docker_cache_prune,
            allow_runner_workdir_prune=allow_runner_workdir_prune,
            allow_runner_service_restart=allow_runner_service_restart,
        )
        request = RunnerHostHygieneApplyRequest(
            action=apply_action,
            host_name=host_name,
            mutate=mutate,
            retained_warm_builders=retained_warm_builders,
            audit_record_key=audit_record_key,
        )
        plan = plan_runner_host_hygiene_apply(
            policy=policy,
            request=request,
            report=report,
        )
        audit_record = (
            RunnerHostHygieneApplyAuditRecord(
                audit_record_key=request.audit_record_key,
                status="planned",
                request=request,
                plan=plan,
                pre_apply_report=report,
                message="planned runner host hygiene apply; no host mutation was executed",
            )
            if request.audit_record_key
            else None
        )
    except (OSError, JSONDecodeError, ValidationError, ValueError) as error:
        raise click.ClickException(str(error)) from error
    payload: dict[str, object] = {
        "mode": "dry-run",
        "policy": policy.model_dump(mode="json"),
        "request": request.model_dump(mode="json"),
        "report": report.model_dump(mode="json"),
        "plan": plan.model_dump(mode="json"),
    }
    if audit_record is not None:
        payload["audit_record"] = audit_record.model_dump(mode="json")
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


def _load_runner_lane_inventory(inventory_file: Path) -> RunnerLaneInventory:
    return RunnerLaneInventory.model_validate(
        json.loads(inventory_file.read_text(encoding="utf-8"))
    )


def _load_runner_lane_baseline_readiness(
    baseline_readiness_file: Path,
) -> RunnerLaneBaselineReadiness:
    payload = json.loads(baseline_readiness_file.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and isinstance(payload.get("readiness"), dict):
        payload = payload["readiness"]
    return RunnerLaneBaselineReadiness.model_validate(payload)


def _load_runner_host_hygiene_observation(
    observation_file: Path,
) -> RunnerHostHygieneObservation:
    return RunnerHostHygieneObservation.model_validate(
        json.loads(observation_file.read_text(encoding="utf-8"))
    )


def _load_runner_host_hygiene_policy(policy_file: Path) -> RunnerHostHygienePolicy:
    return RunnerHostHygienePolicy.model_validate(
        json.loads(policy_file.read_text(encoding="utf-8"))
    )


def _load_runner_host_hygiene_report(report_file: Path) -> RunnerHostHygieneReport:
    payload = json.loads(report_file.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and isinstance(payload.get("report"), dict):
        payload = payload["report"]
    return RunnerHostHygieneReport.model_validate(payload)


def _runner_baseline_runner_name(value: str) -> str:
    return (
        value.strip()
        or os.environ.get("RUNNER_NAME", "").strip()
        or os.environ.get("RUNNER_TRACKING_ID", "").strip()
    )


def _runner_baseline_labels(values: tuple[str, ...]) -> tuple[str, ...]:
    if values:
        return values
    raw_labels = os.environ.get("RUNNER_LABELS", "").strip()
    if raw_labels:
        return tuple(label.strip() for label in raw_labels.split(",") if label.strip())
    return ()


def _runner_baseline_docker_config_isolated(value: bool | None) -> bool | None:
    if value is not None:
        return value
    docker_config = os.environ.get("DOCKER_CONFIG", "").strip()
    isolated_config = os.environ.get("LAUNCHPLANE_ISOLATED_DOCKER_CONFIG", "").strip()
    if docker_config and isolated_config and docker_config == isolated_config:
        return True
    return None


def _runner_baseline_service_user(value: str) -> str:
    return (
        value.strip()
        or os.environ.get("USER", "").strip()
        or os.environ.get("LOGNAME", "").strip()
        or os.environ.get("GITHUB_ACTOR", "").strip()
    )


def _runner_baseline_home_directory(value: str) -> str:
    return value.strip() or os.environ.get("HOME", "").strip()
