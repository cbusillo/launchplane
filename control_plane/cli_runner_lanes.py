from json import JSONDecodeError
import json
import os
from pathlib import Path
from urllib.parse import urlencode
from urllib.parse import urlsplit
from urllib.request import Request
from urllib.request import urlopen

import click
from pydantic import ValidationError

from control_plane.contracts.runner_lane_baseline import RunnerLaneBaselineObservation
from control_plane.contracts.runner_lane_baseline import RunnerLaneBaselinePolicy
from control_plane.contracts.runner_lane_baseline import RunnerLaneBaselineReadiness
from control_plane.contracts.runner_lane_baseline import RunnerLaneDockerToolchainObservation
from control_plane.contracts.runner_lane_baseline import evaluate_runner_lane_baseline
from control_plane.contracts.runner_lane_control import RunnerLaneControlAction
from control_plane.contracts.runner_lane_control import RunnerLaneControlPolicy
from control_plane.contracts.runner_lane_control import RunnerLaneControlRequest
from control_plane.contracts.runner_lane_control import plan_runner_lane_control
from control_plane.contracts.runner_lane_inventory import RunnerLaneInventory
from control_plane.contracts.runner_lane_maintainer import RunnerLaneDesiredState
from control_plane.contracts.runner_lane_maintainer import RunnerLaneMaintainerPolicy
from control_plane.contracts.runner_lane_maintainer import plan_runner_lane_maintainer
from control_plane.contracts.runner_lane_registration import RunnerLaneRegistrationPolicy
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneObservation
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyAction
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyAuditRecord
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyPolicy
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyPlan
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyRequest
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneAdapterPolicy
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneAdapterProposal
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneAdapterType
from control_plane.contracts.runner_host_hygiene import RunnerHostHygienePolicy
from control_plane.contracts.runner_host_hygiene import RunnerHostHygienePrivilegedScope
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneReport
from control_plane.contracts.runner_host_hygiene import evaluate_runner_host_hygiene
from control_plane.contracts.runner_host_hygiene import plan_runner_host_hygiene_apply
from control_plane.contracts.runner_host_hygiene import plan_runner_host_hygiene_adapter_boundary
from control_plane.merge_train_github import MergeTrainGitHubError
from control_plane.merge_train_github import UrllibMergeTrainGitHubTransport
from control_plane.runner_lane_github import GitHubRunnerLaneInventoryReader
from control_plane.runner_lane_github import GitHubRunnerLaneRegistrationTokenFetcher
from control_plane.runner_lane_github import GitHubRunnerLaneRetirer
from control_plane.runner_lane_github import GitHubRepositoryActiveRunReader
from control_plane.runner_queue_wait_github import GitHubRunnerQueueWaitReader
from control_plane.workflows.runner_lane_registration_executor import (
    RunnerLaneRegistrationExecutorRequest,
)
from control_plane.workflows.runner_lane_registration_executor import (
    build_local_command_runner as build_runner_registration_command_runner,
)
from control_plane.workflows.runner_lane_registration_executor import (
    build_refreshing_service_audit_poster as build_runner_registration_service_audit_poster,
)
from control_plane.workflows.runner_lane_registration_executor import (
    AuditPoster as RunnerRegistrationAuditPoster,
)
from control_plane.workflows.runner_lane_registration_executor import dry_run_audit_poster
from control_plane.workflows.runner_lane_registration_executor import (
    execute_runner_lane_registration_executor,
)
from control_plane.workflows.runner_lane_registration_executor import (
    validate_local_executor_environment as validate_runner_registration_environment,
)
from control_plane.workflows.runner_lane_retirement_executor import (
    RunnerLaneRetirementExecutorRequest,
)
from control_plane.workflows.runner_lane_retirement_executor import (
    execute_runner_lane_retirement_executor,
)
from control_plane.workflows.runner_lane_retirement_executor import (
    validate_local_executor_environment as validate_runner_retirement_environment,
)
from control_plane.workflows.runner_host_hygiene_executor import RunnerHostHygieneExecutorRequest
from control_plane.workflows.runner_host_hygiene_executor import RunnerWorkdirRoot
from control_plane.workflows.runner_host_hygiene_executor import DOCKER_BUILDX_PLUGIN_PATH_COMMAND
from control_plane.workflows.runner_host_hygiene_executor import RemoteCommandRunner
from control_plane.workflows.runner_host_hygiene_executor import build_local_command_runner
from control_plane.workflows.runner_host_hygiene_executor import (
    build_refreshing_service_audit_poster,
)
from control_plane.workflows.runner_host_hygiene_executor import (
    execute_runner_host_hygiene_executor,
)
from control_plane.workflows.runner_host_hygiene_executor import (
    validate_local_executor_environment as validate_runner_host_hygiene_environment,
)
from control_plane.workflows.runner_host_hygiene_audit_spool import (
    RunnerHostHygieneAuditSpool,
)
from control_plane.workflows.ship import utc_now_timestamp


def register_runner_lane_commands(work_graph: click.Group) -> None:
    work_graph.add_command(runner_inventory, name="runner-inventory")
    work_graph.add_command(runner_queue_wait, name="runner-queue-wait")
    work_graph.add_command(runner_baseline_observe, name="runner-baseline-observe")
    work_graph.add_command(runner_control_plan, name="runner-control-plan")
    work_graph.add_command(runner_maintainer_plan, name="runner-maintainer-plan")
    work_graph.add_command(
        runner_lane_registration_executor,
        name="runner-lane-registration-executor",
    )
    work_graph.add_command(
        runner_lane_retirement_executor,
        name="runner-lane-retirement-executor",
    )
    work_graph.add_command(runner_host_hygiene_report, name="runner-host-hygiene-report")
    work_graph.add_command(runner_host_hygiene_apply_plan, name="runner-host-hygiene-apply-plan")
    work_graph.add_command(
        runner_host_hygiene_adapter_boundary_plan,
        name="runner-host-hygiene-adapter-boundary-plan",
    )
    work_graph.add_command(
        runner_host_hygiene_executor,
        name="runner-host-hygiene-executor",
    )


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
    "--observe-docker-toolchain/--skip-docker-toolchain-observation",
    default=False,
    show_default=True,
    help="Collect read-only Docker Engine, CLI, Buildx, and BuildKit version evidence.",
)
@click.option(
    "--docker-toolchain-timeout-seconds",
    default=10,
    show_default=True,
    type=click.IntRange(min=1),
    help="Timeout for each Docker toolchain observation command.",
)
@click.option("--docker-engine-version", default="", help="Observed Docker Engine version.")
@click.option("--docker-cli-version", default="", help="Observed Docker CLI version.")
@click.option("--docker-buildx-version", default="", help="Observed Docker Buildx version.")
@click.option(
    "--docker-buildx-plugin-path", default="", help="Observed Docker Buildx CLI plugin path."
)
@click.option(
    "--docker-buildx-package", default="", help="Observed Docker Buildx package name/version."
)
@click.option("--docker-buildx-source", default="", help="Observed Docker Buildx source.")
@click.option("--buildkit-version", default="", help="Observed BuildKit version.")
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
@click.option(
    "--require-docker-toolchain-observation/--allow-missing-docker-toolchain-observation",
    default=False,
    show_default=True,
    help="Require Docker toolchain observation evidence in baseline readiness.",
)
@click.option(
    "--minimum-docker-buildx-version",
    default="",
    help="Minimum Docker Buildx CLI plugin version accepted by baseline readiness.",
)
def runner_baseline_observe(
    runner_name: str,
    labels: tuple[str, ...],
    docker_config_isolated: bool | None,
    observe_docker_toolchain: bool,
    docker_toolchain_timeout_seconds: int,
    docker_engine_version: str,
    docker_cli_version: str,
    docker_buildx_version: str,
    docker_buildx_plugin_path: str,
    docker_buildx_package: str,
    docker_buildx_source: str,
    buildkit_version: str,
    service_user: str,
    home_directory: str,
    observed_at: str,
    required_labels: tuple[str, ...],
    allowed_service_users: tuple[str, ...],
    allowed_home_roots: tuple[str, ...],
    require_docker_toolchain_observation: bool,
    minimum_docker_buildx_version: str,
) -> None:
    try:
        observation = RunnerLaneBaselineObservation(
            runner_name=_runner_baseline_runner_name(runner_name),
            labels=_runner_baseline_labels(labels),
            docker_config_isolated=_runner_baseline_docker_config_isolated(docker_config_isolated),
            docker_toolchain=_runner_baseline_docker_toolchain(
                observe_docker_toolchain=observe_docker_toolchain,
                docker_toolchain_timeout_seconds=docker_toolchain_timeout_seconds,
                docker_engine_version=docker_engine_version,
                docker_cli_version=docker_cli_version,
                docker_buildx_version=docker_buildx_version,
                docker_buildx_plugin_path=docker_buildx_plugin_path,
                docker_buildx_package=docker_buildx_package,
                docker_buildx_source=docker_buildx_source,
                buildkit_version=buildkit_version,
            ),
            service_user=_runner_baseline_service_user(service_user),
            home_directory=_runner_baseline_home_directory(home_directory),
            observed_at=observed_at.strip() or utc_now_timestamp(),
        )
        policy = RunnerLaneBaselinePolicy(
            required_labels=required_labels or ("self-hosted", "launchplane"),
            require_docker_toolchain_observation=require_docker_toolchain_observation,
            minimum_docker_buildx_version=minimum_docker_buildx_version,
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


@click.command("runner-maintainer-plan")
@click.option("--repository", required=True, help="owner/name repository for the runner lane.")
@click.option("--host-name", required=True, help="Approved runner host name.")
@click.option("--lane-name", required=True, help="Desired runner lane name.")
@click.option(
    "--runner-directory",
    required=True,
    help="Desired absolute lane directory below an allowed registration root.",
)
@click.option("--service-user", required=True, help="Constrained runner service user.")
@click.option(
    "--systemd-unit-name",
    required=True,
    help="Desired systemd unit name, for example launchplane-runner@lane.service.",
)
@click.option("--label", "labels", multiple=True, required=True, help="Desired runner label.")
@click.option(
    "--runner-version",
    default="latest-approved",
    show_default=True,
    help="Desired runner version policy label. This command does not download runners.",
)
@click.option(
    "--allowed-repository",
    "allowed_repositories",
    multiple=True,
    help="Repository opted into runner lane maintenance. Repeat as needed.",
)
@click.option(
    "--approved-host",
    "approved_hosts",
    multiple=True,
    help="Host approved for runner lane maintenance. Repeat as needed.",
)
@click.option(
    "--allowed-registration-root",
    "allowed_registration_roots",
    multiple=True,
    help="Absolute root allowed for runner registration. Repeat as needed.",
)
@click.option(
    "--allowed-service-user",
    "allowed_service_users",
    multiple=True,
    help="Service user approved for runner lane maintenance. Repeat as needed.",
)
@click.option(
    "--required-label",
    "required_labels",
    multiple=True,
    help="Label required by maintainer policy. Defaults to self-hosted, launchplane, launchplane-managed.",
)
@click.option(
    "--required-managed-label",
    default="launchplane-managed",
    show_default=True,
    help="Label required on existing lanes before they can be adopted or removed.",
)
@click.option(
    "--require-baseline-readiness/--allow-missing-baseline-readiness",
    default=True,
    show_default=True,
    help="Whether baseline readiness must pass before maintainer planning is ready.",
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
def runner_maintainer_plan(
    repository: str,
    host_name: str,
    lane_name: str,
    runner_directory: str,
    service_user: str,
    systemd_unit_name: str,
    labels: tuple[str, ...],
    runner_version: str,
    allowed_repositories: tuple[str, ...],
    approved_hosts: tuple[str, ...],
    allowed_registration_roots: tuple[str, ...],
    allowed_service_users: tuple[str, ...],
    required_labels: tuple[str, ...],
    required_managed_label: str,
    require_baseline_readiness: bool,
    inventory_file: Path,
    baseline_readiness_file: Path,
) -> None:
    try:
        policy = RunnerLaneMaintainerPolicy(
            allowed_repositories=allowed_repositories,
            approved_hosts=approved_hosts,
            allowed_registration_roots=allowed_registration_roots,
            allowed_service_users=allowed_service_users,
            required_labels=(
                required_labels or ("self-hosted", "launchplane", "launchplane-managed")
            ),
            required_managed_label=required_managed_label,
            require_baseline_readiness=require_baseline_readiness,
        )
        desired_state = RunnerLaneDesiredState(
            repository=repository,
            host_name=host_name,
            lane_name=lane_name,
            runner_directory=runner_directory,
            service_user=service_user,
            systemd_unit_name=systemd_unit_name,
            labels=labels,
            runner_version=runner_version,
        )
        inventory = _load_runner_lane_inventory(inventory_file)
        baseline_readiness = _load_runner_lane_baseline_readiness(baseline_readiness_file)
        plan = plan_runner_lane_maintainer(
            policy=policy,
            desired_state=desired_state,
            inventory=inventory,
            baseline_readiness=baseline_readiness,
        )
    except (OSError, JSONDecodeError, ValidationError, ValueError) as error:
        raise click.ClickException(str(error)) from error
    click.echo(
        json.dumps(
            {
                "policy": policy.model_dump(mode="json"),
                "desired_state": desired_state.model_dump(mode="json"),
                "plan": plan.model_dump(mode="json"),
            },
            indent=2,
            sort_keys=True,
        )
    )


@click.command("runner-lane-registration-executor")
@click.option("--repository", required=True, help="owner/name repository for the runner lane.")
@click.option("--host-name", required=True, help="Approved runner host name.")
@click.option(
    "--execution-lane",
    required=True,
    help="Approved self-hosted ops lane label executing this registration.",
)
@click.option("--service-user", required=True, help="Constrained host service user.")
@click.option("--lane-name", required=True, help="Runner lane name to register.")
@click.option(
    "--registration-root",
    required=True,
    help="Absolute root directory where runner lanes may be registered.",
)
@click.option(
    "--runner-package-url",
    default="",
    help="GitHub Actions runner tarball URL used when --mutate creates a lane.",
)
@click.option("--label", "labels", multiple=True, required=True, help="Runner label.")
@click.option(
    "--mutate/--dry-run",
    default=False,
    show_default=True,
    help="Register a new lane through the supervised create-only executor.",
)
@click.option(
    "--audit-record-key",
    required=True,
    help="Launchplane-owned audit record key for this registration attempt.",
)
@click.option(
    "--allowed-repository",
    "allowed_repositories",
    multiple=True,
    help="Repository opted into runner lane registration. Repeat as needed.",
)
@click.option(
    "--approved-host",
    "approved_hosts",
    multiple=True,
    help="Host approved for runner lane registration. Repeat as needed.",
)
@click.option(
    "--allowed-registration-root",
    "allowed_registration_roots",
    multiple=True,
    help="Absolute root allowed for runner registration. Repeat as needed.",
)
@click.option(
    "--required-label",
    "required_labels",
    multiple=True,
    help="Label required by policy. Defaults to self-hosted, launchplane, launchplane-managed.",
)
@click.option(
    "--inventory-file",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    required=True,
    help="Pre-mutation RunnerLaneInventory JSON from runner-inventory.",
)
@click.option(
    "--github-token-env",
    default="GITHUB_TOKEN",
    show_default=True,
    help="Environment variable reserved for future supervised registration token fetches.",
)
@click.option(
    "--github-api-base-url",
    default="https://api.github.com",
    show_default=True,
    help="GitHub API base URL.",
)
@click.option(
    "--timeout-seconds",
    default=120,
    show_default=True,
    type=click.IntRange(min=1),
    help="Timeout for the local runner config command.",
)
@click.option(
    "--audit-mode",
    type=click.Choice(("local", "service")),
    default="local",
    show_default=True,
    help="Where runner registration audit records should be written.",
)
@click.option(
    "--service-url",
    default="",
    help="Launchplane service origin for --audit-mode service.",
)
@click.option(
    "--bearer-token-env",
    default="LAUNCHPLANE_SERVICE_TOKEN",
    show_default=True,
    help="Environment variable containing a Launchplane bearer token.",
)
def runner_lane_registration_executor(
    repository: str,
    host_name: str,
    execution_lane: str,
    service_user: str,
    lane_name: str,
    registration_root: str,
    runner_package_url: str,
    labels: tuple[str, ...],
    mutate: bool,
    audit_record_key: str,
    allowed_repositories: tuple[str, ...],
    approved_hosts: tuple[str, ...],
    allowed_registration_roots: tuple[str, ...],
    required_labels: tuple[str, ...],
    inventory_file: Path,
    github_token_env: str,
    github_api_base_url: str,
    timeout_seconds: int,
    audit_mode: str,
    service_url: str,
    bearer_token_env: str,
) -> None:
    try:
        pre_inventory = _load_runner_lane_inventory(inventory_file)
        token_env = github_token_env.strip()
        token = os.environ.get(token_env, "").strip() if token_env else ""
        if not token:
            raise click.ClickException(f"Missing GitHub token in environment variable {token_env}.")
        transport = UrllibMergeTrainGitHubTransport(
            token=token,
            api_base_url=github_api_base_url,
        )
        inventory_reader = GitHubRunnerLaneInventoryReader(transport=transport)
        audit_poster: RunnerRegistrationAuditPoster = dry_run_audit_poster
        if audit_mode == "service":
            token_env = bearer_token_env.strip()
            if not token_env:
                raise click.ClickException(
                    "runner lane registration executor requires --bearer-token-env."
                )
            audit_poster = build_runner_registration_service_audit_poster(
                service_url=service_url,
                bearer_token_provider=lambda: _launchplane_service_bearer_token(
                    service_url=service_url,
                    token_env=token_env,
                    label="runner lane registration",
                ),
            )
        request = RunnerLaneRegistrationExecutorRequest(
            repository=repository,
            host_name=host_name,
            execution_lane=execution_lane,
            service_user=service_user,
            lane_name=lane_name,
            registration_root=registration_root,
            runner_package_url=runner_package_url,
            labels=labels,
            mutate=mutate,
            audit_record_key=audit_record_key,
            timeout_seconds=timeout_seconds,
        )
        validate_runner_registration_environment(request=request)
        result = execute_runner_lane_registration_executor(
            request=request,
            policy=RunnerLaneRegistrationPolicy(
                allowed_repositories=allowed_repositories,
                approved_hosts=approved_hosts,
                allowed_registration_roots=allowed_registration_roots,
                required_labels=(
                    required_labels or ("self-hosted", "launchplane", "launchplane-managed")
                ),
            ),
            pre_inventory=pre_inventory,
            inventory_reader=lambda repo: inventory_reader.read_runner_lane_inventory(
                repository=repo
            ),
            token_fetcher=GitHubRunnerLaneRegistrationTokenFetcher(transport=transport),
            remote_runner=build_runner_registration_command_runner(),
            audit_poster=audit_poster,
        )
    except (OSError, JSONDecodeError, ValidationError, ValueError) as error:
        raise click.ClickException(str(error)) from error
    click.echo(json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True))


@click.command("runner-lane-retirement-executor")
@click.option("--repository", required=True, help="owner/name repository whose lane should retire.")
@click.option("--host-name", required=True, help="Approved runner host name.")
@click.option("--execution-lane", required=True, help="Approved ops execution lane.")
@click.option("--service-user", required=True, help="Expected constrained service user.")
@click.option("--lane-name", required=True, help="Exact runner lane name to retire.")
@click.option(
    "--registration-root",
    required=True,
    help="Absolute approved root containing the runner lane directory.",
)
@click.option("--mutate/--dry-run", default=False, show_default=True)
@click.option("--audit-record-key", required=True, help="Durable lifecycle audit key.")
@click.option(
    "--allowed-repository",
    "allowed_repositories",
    multiple=True,
    help="Repository opted into runner lane retirement. Repeat as needed.",
)
@click.option(
    "--approved-host",
    "approved_hosts",
    multiple=True,
    help="Host approved for runner lane retirement. Repeat as needed.",
)
@click.option(
    "--allowed-registration-root",
    "allowed_registration_roots",
    multiple=True,
    help="Absolute root allowed for runner retirement. Repeat as needed.",
)
@click.option(
    "--required-label",
    "required_labels",
    multiple=True,
    help="Observed managed label required for retirement.",
)
@click.option(
    "--inventory-file",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    required=True,
    help="Pre-mutation RunnerLaneInventory JSON from runner-inventory.",
)
@click.option(
    "--github-token-env",
    default="GITHUB_TOKEN",
    show_default=True,
    help="Environment variable containing runner administration authority.",
)
@click.option(
    "--github-api-base-url",
    default="https://api.github.com",
    show_default=True,
    help="GitHub API base URL.",
)
@click.option(
    "--timeout-seconds",
    default=120,
    show_default=True,
    type=click.IntRange(min=1),
    help="Timeout for each local runner retirement command.",
)
@click.option(
    "--audit-mode",
    type=click.Choice(("local", "service")),
    default="local",
    show_default=True,
    help="Where runner lifecycle audit records should be written.",
)
@click.option(
    "--service-url",
    default="",
    help="Launchplane service origin for --audit-mode service.",
)
@click.option(
    "--bearer-token-env",
    default="LAUNCHPLANE_SERVICE_TOKEN",
    show_default=True,
    help="Environment variable containing a Launchplane bearer token.",
)
def runner_lane_retirement_executor(
    repository: str,
    host_name: str,
    execution_lane: str,
    service_user: str,
    lane_name: str,
    registration_root: str,
    mutate: bool,
    audit_record_key: str,
    allowed_repositories: tuple[str, ...],
    approved_hosts: tuple[str, ...],
    allowed_registration_roots: tuple[str, ...],
    required_labels: tuple[str, ...],
    inventory_file: Path,
    github_token_env: str,
    github_api_base_url: str,
    timeout_seconds: int,
    audit_mode: str,
    service_url: str,
    bearer_token_env: str,
) -> None:
    try:
        pre_inventory = _load_runner_lane_inventory(inventory_file)
        github_token_name = github_token_env.strip()
        github_token = os.environ.get(github_token_name, "").strip() if github_token_name else ""
        if not github_token:
            raise click.ClickException(
                f"Missing GitHub token in environment variable {github_token_name}."
            )
        transport = UrllibMergeTrainGitHubTransport(
            token=github_token,
            api_base_url=github_api_base_url,
        )
        inventory_reader = GitHubRunnerLaneInventoryReader(transport=transport)
        activity_reader = GitHubRepositoryActiveRunReader(transport=transport)
        runner_retirer = GitHubRunnerLaneRetirer(transport=transport)
        if mutate and audit_mode != "service":
            raise click.ClickException(
                "runner lane retirement mutation requires --audit-mode service."
            )
        audit_poster: RunnerRegistrationAuditPoster = dry_run_audit_poster
        if audit_mode == "service":
            token_name = bearer_token_env.strip()
            if not token_name:
                raise click.ClickException(
                    "runner lane retirement executor requires --bearer-token-env."
                )
            audit_poster = build_runner_registration_service_audit_poster(
                service_url=service_url,
                bearer_token_provider=lambda: _launchplane_service_bearer_token(
                    service_url=service_url,
                    token_env=token_name,
                    label="runner lane retirement",
                ),
            )
        request = RunnerLaneRetirementExecutorRequest(
            repository=repository,
            host_name=host_name,
            execution_lane=execution_lane,
            service_user=service_user,
            lane_name=lane_name,
            registration_root=registration_root,
            mutate=mutate,
            audit_record_key=audit_record_key,
            timeout_seconds=timeout_seconds,
        )
        validate_runner_retirement_environment(request=request)
        result = execute_runner_lane_retirement_executor(
            request=request,
            policy=RunnerLaneRegistrationPolicy(
                allowed_repositories=allowed_repositories,
                approved_hosts=approved_hosts,
                allowed_registration_roots=allowed_registration_roots,
                required_labels=(required_labels or ("launchplane-managed",)),
            ),
            pre_inventory=pre_inventory,
            inventory_reader=lambda repo: inventory_reader.read_runner_lane_inventory(
                repository=repo
            ),
            active_run_reader=lambda repo: activity_reader.read_active_run_ids(repository=repo),
            runner_retirer=lambda repo, runner_id: runner_retirer.delete_runner(
                repository=repo,
                runner_id=runner_id,
            ),
            remote_runner=build_runner_registration_command_runner(),
            audit_poster=audit_poster,
        )
    except (
        OSError,
        JSONDecodeError,
        MergeTrainGitHubError,
        ValidationError,
        ValueError,
    ) as error:
        raise click.ClickException(str(error)) from error
    click.echo(json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True))


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
    type=click.Choice(
        (
            "prune_docker_cache",
            "prune_dangling_images",
            "remove_buildkit_state_volumes",
            "prune_runner_workdir",
            "restart_runner_service",
        )
    ),
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
    "--target-buildkit-state-volume",
    "target_buildkit_state_volumes",
    multiple=True,
    help="BuildKit state volume the request targets for removal. Supply at most one.",
)
@click.option(
    "--allowed-buildkit-state-volume",
    "allowed_buildkit_state_volumes",
    multiple=True,
    help="BuildKit state volume policy allows removal for. Repeat for each volume.",
)
@click.option(
    "--target-buildkit-builder",
    default="",
    help="Exact allowlisted Buildx builder whose cache the request targets.",
)
@click.option(
    "--allowed-buildkit-builder",
    "allowed_buildkit_builders",
    multiple=True,
    help="Buildx builder policy allows bounded pruning for. Repeat for each builder.",
)
@click.option(
    "--max-used-space-bytes",
    type=click.IntRange(min=0),
    default=0,
    show_default=True,
    help="Retained cache bytes for the default daemon cache or a target builder.",
)
@click.option(
    "--allow-docker-cache-prune/--disallow-docker-cache-prune",
    default=False,
    show_default=True,
    help="Enable Docker cache prune planning in the local policy.",
)
@click.option(
    "--allow-dangling-image-prune/--disallow-dangling-image-prune",
    default=False,
    show_default=True,
    help="Enable dangling-only Docker image prune planning in the local policy.",
)
@click.option(
    "--allow-buildkit-state-volume-remove/--disallow-buildkit-state-volume-remove",
    default=False,
    show_default=True,
    help="Enable allowlisted BuildKit state volume removal planning in the local policy.",
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
    target_buildkit_state_volumes: tuple[str, ...],
    allowed_buildkit_state_volumes: tuple[str, ...],
    target_buildkit_builder: str,
    allowed_buildkit_builders: tuple[str, ...],
    max_used_space_bytes: int,
    allow_docker_cache_prune: bool,
    allow_dangling_image_prune: bool,
    allow_buildkit_state_volume_remove: bool,
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
            allow_dangling_image_prune=allow_dangling_image_prune,
            allowed_buildkit_builders=allowed_buildkit_builders,
            allow_buildkit_state_volume_remove=allow_buildkit_state_volume_remove,
            allowed_buildkit_state_volumes=allowed_buildkit_state_volumes,
            allow_runner_workdir_prune=allow_runner_workdir_prune,
            allow_runner_service_restart=allow_runner_service_restart,
        )
        request = RunnerHostHygieneApplyRequest(
            action=apply_action,
            host_name=host_name,
            mutate=mutate,
            retained_warm_builders=retained_warm_builders,
            target_buildkit_builder=target_buildkit_builder,
            max_used_space_bytes=max_used_space_bytes,
            target_buildkit_state_volumes=target_buildkit_state_volumes,
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


@click.command("runner-host-hygiene-adapter-boundary-plan")
@click.option(
    "--adapter-type",
    required=True,
    type=click.Choice(("github_actions_runner", "launchplane_worker", "remote_host_executor")),
    help="Proposed host adapter type. The command never mutates hosts.",
)
@click.option("--host-name", required=True, help="Runner host name to plan against.")
@click.option(
    "--execution-lane",
    required=True,
    help="Dedicated execution lane proposed for privileged host work.",
)
@click.option("--service-user", required=True, help="Host service user for the proposed adapter.")
@click.option(
    "--repository-scope",
    "repository_scopes",
    multiple=True,
    help="Repository scope the adapter is allowed to affect. Repeat for each repository.",
)
@click.option(
    "--privileged-scope",
    "privileged_scopes",
    multiple=True,
    type=click.Choice(("docker_cache", "docker_volume", "runner_service", "runner_workdir")),
    help="Privileged host capability requested by the adapter. Repeat for each scope.",
)
@click.option(
    "--audit-record-key",
    required=True,
    help="Launchplane-owned audit record key the future adapter must write.",
)
@click.option(
    "--rollback-plan",
    default="",
    help="Rollback or stop condition required before implementing host mutation.",
)
@click.option(
    "--pre-apply-evidence",
    "pre_apply_evidence",
    multiple=True,
    help="Pre-apply evidence the adapter proposal will capture. Repeat for each item.",
)
@click.option(
    "--post-apply-evidence",
    "post_apply_evidence",
    multiple=True,
    help="Post-apply evidence the adapter proposal will capture. Repeat for each item.",
)
@click.option(
    "--approved-host",
    "approved_hosts",
    multiple=True,
    help="Host approved for adapter execution. Repeat for each approved host.",
)
@click.option(
    "--allowed-adapter-type",
    "allowed_adapter_types",
    multiple=True,
    type=click.Choice(("github_actions_runner", "launchplane_worker", "remote_host_executor")),
    help="Adapter type allowed by local policy. Repeat for each type.",
)
@click.option(
    "--allowed-execution-lane",
    "allowed_execution_lanes",
    multiple=True,
    help="Execution lane allowed by local policy. Repeat for each lane.",
)
@click.option(
    "--allowed-service-user",
    "allowed_service_users",
    multiple=True,
    help="Service user allowed by local policy. Repeat for each user.",
)
@click.option(
    "--allowed-repository-scope",
    "allowed_repository_scopes",
    multiple=True,
    help="Repository scope allowed by local policy. Repeat for each repository.",
)
@click.option(
    "--allowed-privileged-scope",
    "allowed_privileged_scopes",
    multiple=True,
    type=click.Choice(("docker_cache", "docker_volume", "runner_service", "runner_workdir")),
    help="Privileged host scope allowed by local policy. Repeat for each scope.",
)
@click.option(
    "--required-pre-apply-evidence",
    "required_pre_apply_evidence",
    multiple=True,
    help="Pre-apply evidence required by local policy. Repeat for each item.",
)
@click.option(
    "--required-post-apply-evidence",
    "required_post_apply_evidence",
    multiple=True,
    help="Post-apply evidence required by local policy. Repeat for each item.",
)
@click.option(
    "--audit-record-key-prefix",
    default="runner-host-hygiene/",
    show_default=True,
    help="Required audit record key prefix.",
)
@click.option(
    "--require-rollback-plan/--allow-missing-rollback-plan",
    default=True,
    show_default=True,
    help="Require a rollback or stop condition before the adapter boundary can be ready.",
)
@click.option(
    "--apply-plan-file",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    required=True,
    help="RunnerHostHygieneApplyPlan JSON or runner-host-hygiene-apply-plan output.",
)
def runner_host_hygiene_adapter_boundary_plan(
    adapter_type: RunnerHostHygieneAdapterType,
    host_name: str,
    execution_lane: str,
    service_user: str,
    repository_scopes: tuple[str, ...],
    privileged_scopes: tuple[RunnerHostHygienePrivilegedScope, ...],
    audit_record_key: str,
    rollback_plan: str,
    pre_apply_evidence: tuple[str, ...],
    post_apply_evidence: tuple[str, ...],
    approved_hosts: tuple[str, ...],
    allowed_adapter_types: tuple[RunnerHostHygieneAdapterType, ...],
    allowed_execution_lanes: tuple[str, ...],
    allowed_service_users: tuple[str, ...],
    allowed_repository_scopes: tuple[str, ...],
    allowed_privileged_scopes: tuple[RunnerHostHygienePrivilegedScope, ...],
    required_pre_apply_evidence: tuple[str, ...],
    required_post_apply_evidence: tuple[str, ...],
    audit_record_key_prefix: str,
    require_rollback_plan: bool,
    apply_plan_file: Path,
) -> None:
    try:
        apply_plan = _load_runner_host_hygiene_apply_plan(apply_plan_file)
        policy = RunnerHostHygieneAdapterPolicy(
            approved_hosts=approved_hosts,
            allowed_adapter_types=allowed_adapter_types,
            allowed_execution_lanes=allowed_execution_lanes,
            allowed_service_users=allowed_service_users,
            allowed_repository_scopes=allowed_repository_scopes,
            allowed_privileged_scopes=allowed_privileged_scopes,
            required_pre_apply_evidence=required_pre_apply_evidence,
            required_post_apply_evidence=required_post_apply_evidence,
            audit_record_key_prefix=audit_record_key_prefix,
            require_rollback_plan=require_rollback_plan,
        )
        proposal = RunnerHostHygieneAdapterProposal(
            adapter_type=adapter_type,
            host_name=host_name,
            execution_lane=execution_lane,
            service_user=service_user,
            repository_scopes=repository_scopes,
            privileged_scopes=privileged_scopes,
            audit_record_key=audit_record_key,
            rollback_plan=rollback_plan,
            pre_apply_evidence=pre_apply_evidence,
            post_apply_evidence=post_apply_evidence,
        )
        boundary = plan_runner_host_hygiene_adapter_boundary(
            policy=policy,
            proposal=proposal,
            apply_plan=apply_plan,
        )
    except (OSError, JSONDecodeError, ValidationError, ValueError) as error:
        raise click.ClickException(str(error)) from error
    click.echo(
        json.dumps(
            {
                "mode": "adapter-boundary-plan",
                "policy": policy.model_dump(mode="json"),
                "proposal": proposal.model_dump(mode="json"),
                "apply_plan": apply_plan.model_dump(mode="json"),
                "boundary": boundary.model_dump(mode="json"),
            },
            indent=2,
            sort_keys=True,
        )
    )


@click.command("runner-host-hygiene-executor")
@click.option(
    "--action",
    "apply_action",
    default="prune_docker_cache",
    show_default=True,
    type=click.Choice(
        ("prune_docker_cache", "prune_dangling_images", "remove_buildkit_state_volumes")
    ),
    help="Approved runner host hygiene executor action.",
)
@click.option(
    "--host-name",
    required=True,
    help="Approved runner host name. Must match the self-hosted ops lane boundary.",
)
@click.option(
    "--execution-lane",
    required=True,
    help="Approved execution lane label for this privileged host action.",
)
@click.option(
    "--service-user",
    required=True,
    help="Constrained service user expected to run the self-hosted ops lane.",
)
@click.option(
    "--repository-scope",
    required=True,
    help="owner/name repository scope approved for this executor run.",
)
@click.option(
    "--audit-record-key",
    required=True,
    help="Launchplane-owned audit record key to write through the service route.",
)
@click.option(
    "--retained-warm-builder",
    "retained_warm_builders",
    multiple=True,
    required=True,
    help="Warm builder image/tag that must remain after cleanup. Repeat as needed.",
)
@click.option(
    "--target-buildkit-state-volume",
    "target_buildkit_state_volumes",
    multiple=True,
    help="Explicit zero-link BuildKit state volume to remove. Supply at most one.",
)
@click.option(
    "--allowed-buildkit-state-volume",
    "allowed_buildkit_state_volumes",
    multiple=True,
    help="BuildKit state volume policy allows removal for. Repeat as needed.",
)
@click.option(
    "--target-buildkit-builder",
    default="",
    help="Exact allowlisted Buildx builder whose cache should be bounded.",
)
@click.option(
    "--allowed-buildkit-builder",
    "allowed_buildkit_builders",
    multiple=True,
    help="Buildx builder policy allows cache pruning for. Repeat as needed.",
)
@click.option(
    "--max-used-space-bytes",
    default=0,
    show_default=True,
    type=click.IntRange(min=0),
    help="Retained cache bytes for the default daemon cache or targeted Buildx builder.",
)
@click.option(
    "--mutate/--dry-run",
    default=False,
    show_default=True,
    help="Require explicit mutate=true before the executor runs Docker cache prune.",
)
@click.option(
    "--minimum-free-disk-bytes",
    default=0,
    show_default=True,
    type=click.IntRange(min=0),
    help="Minimum post/pre free disk policy in bytes.",
)
@click.option(
    "--prune-until",
    default="168h",
    show_default=True,
    help="Bounded Docker builder cache prune age filter, passed as until=<value>.",
)
@click.option(
    "--runner-workdir-root",
    "runner_workdir_roots",
    multiple=True,
    required=True,
    help="Approved runner root formatted as public-key=/absolute/path. Repeat as needed.",
)
@click.option(
    "--idle-observation-count",
    default=2,
    show_default=True,
    type=click.IntRange(min=2, max=10),
    help="Consecutive local idle samples required before mutation.",
)
@click.option(
    "--idle-observation-interval-seconds",
    default=5,
    show_default=True,
    type=click.IntRange(min=0, max=60),
    help="Delay between consecutive local idle samples.",
)
@click.option(
    "--resolve-action-started/--no-resolve-action-started",
    default=False,
    show_default=True,
    help="Record terminal failed evidence for a prior action_started state without rerunning it.",
)
@click.option(
    "--timeout-seconds",
    default=120,
    show_default=True,
    type=click.IntRange(min=1),
    help="Timeout for each remote evidence or prune command.",
)
@click.option(
    "--service-url",
    required=True,
    help="Launchplane service origin, for example https://launchplane.example.com.",
)
@click.option(
    "--bearer-token-env",
    default="LAUNCHPLANE_SERVICE_TOKEN",
    show_default=True,
    help="Environment variable containing a GitHub OIDC bearer token.",
)
@click.option(
    "--audit-spool-root",
    required=True,
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    help="Absolute host-persistent directory for runner hygiene audit delivery state.",
)
@click.option(
    "--audit-artifact-file",
    default=Path("runner-host-hygiene-audit-evidence.json"),
    show_default=True,
    type=click.Path(path_type=Path, file_okay=True, dir_okay=False),
    help="Current-run redacted audit envelope copied for workflow artifact upload.",
)
def runner_host_hygiene_executor(
    apply_action: RunnerHostHygieneApplyAction,
    host_name: str,
    execution_lane: str,
    service_user: str,
    repository_scope: str,
    audit_record_key: str,
    retained_warm_builders: tuple[str, ...],
    target_buildkit_state_volumes: tuple[str, ...],
    allowed_buildkit_state_volumes: tuple[str, ...],
    target_buildkit_builder: str,
    allowed_buildkit_builders: tuple[str, ...],
    max_used_space_bytes: int,
    mutate: bool,
    minimum_free_disk_bytes: int,
    prune_until: str,
    runner_workdir_roots: tuple[str, ...],
    idle_observation_count: int,
    idle_observation_interval_seconds: int,
    resolve_action_started: bool,
    timeout_seconds: int,
    service_url: str,
    bearer_token_env: str,
    audit_spool_root: Path,
    audit_artifact_file: Path,
) -> None:
    try:
        token_env = bearer_token_env.strip()
        if not token_env:
            raise click.ClickException("runner host hygiene executor requires --bearer-token-env.")
        request = RunnerHostHygieneExecutorRequest(
            action=apply_action,
            host_name=host_name,
            execution_lane=execution_lane,
            service_user=service_user,
            repository_scope=repository_scope,
            audit_record_key=audit_record_key,
            retained_warm_builders=retained_warm_builders,
            target_buildkit_builder=target_buildkit_builder,
            allowed_buildkit_builders=allowed_buildkit_builders,
            max_used_space_bytes=max_used_space_bytes,
            target_buildkit_state_volumes=target_buildkit_state_volumes,
            allowed_buildkit_state_volumes=allowed_buildkit_state_volumes,
            mutate=mutate,
            minimum_free_disk_bytes=minimum_free_disk_bytes,
            timeout_seconds=timeout_seconds,
            prune_until=prune_until,
            runner_workdir_roots=_parse_runner_workdir_roots(runner_workdir_roots),
            idle_observation_count=idle_observation_count,
            idle_observation_interval_seconds=idle_observation_interval_seconds,
            resolve_action_started=resolve_action_started,
        )
        validate_runner_host_hygiene_environment(request=request)
        result = execute_runner_host_hygiene_executor(
            request=request,
            remote_runner=build_local_command_runner(),
            audit_poster=build_refreshing_service_audit_poster(
                service_url=service_url,
                bearer_token_provider=lambda: _runner_host_hygiene_bearer_token(
                    service_url=service_url,
                    token_env=token_env,
                ),
            ),
            audit_spool=RunnerHostHygieneAuditSpool(
                root=audit_spool_root,
                artifact_file=audit_artifact_file,
            ),
        )
    except (OSError, ValidationError, ValueError) as error:
        raise click.ClickException(str(error)) from error
    click.echo(json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True))
    if result.audit_delivery_pending:
        raise click.ClickException("runner host hygiene audit delivery remains pending")
    if mutate and result.status != "completed":
        raise click.ClickException(
            f"runner host hygiene mutation did not complete: {result.status}"
        )


def _parse_runner_workdir_roots(values: tuple[str, ...]) -> tuple[RunnerWorkdirRoot, ...]:
    roots: list[RunnerWorkdirRoot] = []
    for value in values:
        key, separator, path = value.partition("=")
        if not separator:
            raise click.ClickException("runner workdir roots must use public-key=/absolute/path")
        roots.append(RunnerWorkdirRoot(key=key, path=path))
    return tuple(roots)


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


def _load_runner_host_hygiene_apply_plan(apply_plan_file: Path) -> RunnerHostHygieneApplyPlan:
    payload = json.loads(apply_plan_file.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and isinstance(payload.get("plan"), dict):
        payload = payload["plan"]
    return RunnerHostHygieneApplyPlan.model_validate(payload)


def _runner_host_hygiene_bearer_token(*, service_url: str, token_env: str) -> str:
    return _launchplane_service_bearer_token(
        service_url=service_url,
        token_env=token_env,
        label="runner host hygiene",
    )


def _launchplane_service_bearer_token(*, service_url: str, token_env: str, label: str) -> str:
    env_token = os.environ.get(token_env, "").strip()
    if env_token:
        return env_token

    request_url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", "").strip()
    request_token = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "").strip()
    if not request_url or not request_token:
        raise click.ClickException(f"Missing Launchplane bearer token in {token_env}.")

    parsed_service_url = urlsplit(service_url.strip())
    if not parsed_service_url.hostname:
        raise click.ClickException(f"{label} service URL must include a hostname.")
    separator = "&" if "?" in request_url else "?"
    oidc_request = Request(
        request_url + separator + urlencode({"audience": parsed_service_url.hostname}),
        headers={"Authorization": f"Bearer {request_token}"},
        method="GET",
    )
    with urlopen(oidc_request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise click.ClickException("GitHub OIDC token response was not an object.")
    token = payload.get("value")
    if not isinstance(token, str) or not token.strip():
        raise click.ClickException("GitHub OIDC token response did not include a token.")
    return token.strip()


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


def _runner_baseline_docker_toolchain(
    *,
    observe_docker_toolchain: bool,
    docker_toolchain_timeout_seconds: int,
    docker_engine_version: str,
    docker_cli_version: str,
    docker_buildx_version: str,
    docker_buildx_plugin_path: str,
    docker_buildx_package: str,
    docker_buildx_source: str,
    buildkit_version: str,
) -> RunnerLaneDockerToolchainObservation | None:
    explicit = RunnerLaneDockerToolchainObservation(
        docker_engine_version=docker_engine_version,
        docker_cli_version=docker_cli_version,
        docker_buildx_version=docker_buildx_version,
        docker_buildx_plugin_path=docker_buildx_plugin_path,
        docker_buildx_package=docker_buildx_package,
        docker_buildx_source=docker_buildx_source,
        buildkit_version=buildkit_version,
    )
    if not observe_docker_toolchain:
        return explicit if explicit.has_evidence() else None

    runner = build_local_command_runner()
    observed_buildx_plugin_path = _first_line(
        _docker_command_output(
            runner,
            (
                "sh",
                "-c",
                DOCKER_BUILDX_PLUGIN_PATH_COMMAND,
            ),
            timeout_seconds=docker_toolchain_timeout_seconds,
        )
    )
    observed = RunnerLaneDockerToolchainObservation(
        docker_engine_version=_docker_command_output(
            runner,
            ("docker", "version", "--format", "{{.Server.Version}}"),
            timeout_seconds=docker_toolchain_timeout_seconds,
        ),
        docker_cli_version=_docker_command_output(
            runner,
            ("docker", "version", "--format", "{{.Client.Version}}"),
            timeout_seconds=docker_toolchain_timeout_seconds,
        ),
        docker_buildx_version=_first_version(
            _docker_command_output(
                runner,
                ("docker", "buildx", "version"),
                timeout_seconds=docker_toolchain_timeout_seconds,
            )
        ),
        docker_buildx_plugin_path=observed_buildx_plugin_path,
        docker_buildx_package=_docker_buildx_package(
            runner=runner, timeout_seconds=docker_toolchain_timeout_seconds
        ),
        docker_buildx_source=_docker_buildx_source(observed_buildx_plugin_path),
        buildkit_version=_first_version(
            _docker_command_output(
                runner,
                ("docker", "buildx", "inspect"),
                timeout_seconds=docker_toolchain_timeout_seconds,
            )
        ),
    )
    if not explicit.has_evidence():
        return observed if observed.has_evidence() else None
    return RunnerLaneDockerToolchainObservation(
        docker_engine_version=explicit.docker_engine_version or observed.docker_engine_version,
        docker_cli_version=explicit.docker_cli_version or observed.docker_cli_version,
        docker_buildx_version=explicit.docker_buildx_version or observed.docker_buildx_version,
        docker_buildx_plugin_path=(
            explicit.docker_buildx_plugin_path or observed.docker_buildx_plugin_path
        ),
        docker_buildx_package=explicit.docker_buildx_package or observed.docker_buildx_package,
        docker_buildx_source=explicit.docker_buildx_source or observed.docker_buildx_source,
        buildkit_version=explicit.buildkit_version or observed.buildkit_version,
    )


def _docker_command_output(
    runner: RemoteCommandRunner,
    command_args: tuple[str, ...],
    *,
    timeout_seconds: int,
) -> str:
    result = runner(command_args, timeout_seconds)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _docker_buildx_package(*, runner: RemoteCommandRunner, timeout_seconds: int) -> str:
    package = _docker_command_output(
        runner,
        ("sh", "-c", "dpkg-query -W -f='${Package} ${Version}' docker-buildx 2>/dev/null"),
        timeout_seconds=timeout_seconds,
    )
    if package:
        return package
    return _docker_command_output(
        runner,
        ("sh", "-c", "rpm -q docker-buildx 2>/dev/null"),
        timeout_seconds=timeout_seconds,
    )


def _docker_buildx_source(plugin_path: str) -> str:
    if plugin_path.startswith("/usr/libexec/") or plugin_path.startswith("/usr/lib/"):
        return "system package"
    if plugin_path.startswith("/usr/local/"):
        return "managed plugin"
    if "/.docker/cli-plugins/" in plugin_path:
        return "user plugin"
    return ""


def _first_line(value: str) -> str:
    for line in value.splitlines():
        stripped_line = line.strip()
        if stripped_line:
            return stripped_line
    return ""


def _first_version(value: str) -> str:
    for token in value.replace(",", " ").split():
        normalized = token.strip().removeprefix("v")
        if normalized and normalized[0].isdigit() and "." in normalized:
            return normalized
    return ""


def _runner_baseline_service_user(value: str) -> str:
    return (
        value.strip()
        or os.environ.get("USER", "").strip()
        or os.environ.get("LOGNAME", "").strip()
        or os.environ.get("GITHUB_ACTOR", "").strip()
    )


def _runner_baseline_home_directory(value: str) -> str:
    return value.strip() or os.environ.get("HOME", "").strip()
