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
from control_plane.workflows.ship import utc_now_timestamp


def register_runner_lane_commands(work_graph: click.Group) -> None:
    work_graph.add_command(runner_baseline_observe, name="runner-baseline-observe")
    work_graph.add_command(runner_control_plan, name="runner-control-plan")


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
