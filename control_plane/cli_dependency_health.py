from __future__ import annotations

import json
from json import JSONDecodeError
from pathlib import Path

import click
from pydantic import ValidationError

from control_plane.contracts.dependency_health import (
    DependencyHealthPolicy,
    DependencyHealthProvenanceMismatch,
    DependencyHealthSnapshot,
    evaluate_dependency_health_absolute,
    evaluate_dependency_health_regression,
)


def register_dependency_health_commands(main: click.Group) -> None:
    main.add_command(dependency_health)


@click.group("dependency-health")
def dependency_health() -> None:
    """Evaluate causal dependency-health evidence."""


@dependency_health.command("compare")
@click.option(
    "--baseline-snapshot",
    type=click.Path(path_type=Path, dir_okay=False),
    required=True,
)
@click.option(
    "--candidate-snapshot",
    type=click.Path(path_type=Path, dir_okay=False),
    required=True,
)
@click.option(
    "--policy-file",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
)
def compare_dependency_health(
    baseline_snapshot: Path,
    candidate_snapshot: Path,
    policy_file: Path | None,
) -> None:
    """Compare one candidate snapshot against its asserted baseline."""
    try:
        baseline = DependencyHealthSnapshot.model_validate(_load_json_object(baseline_snapshot))
        candidate = DependencyHealthSnapshot.model_validate(_load_json_object(candidate_snapshot))
        policy = (
            DependencyHealthPolicy.model_validate(_load_json_object(policy_file))
            if policy_file is not None
            else DependencyHealthPolicy()
        )
        evaluation = evaluate_dependency_health_regression(
            baseline=baseline,
            candidate=candidate,
            policy=policy,
        )
    except (
        OSError,
        JSONDecodeError,
        ValidationError,
        ValueError,
        DependencyHealthProvenanceMismatch,
    ) as error:
        raise click.ClickException(str(error)) from error
    click.echo(json.dumps(evaluation.model_dump(mode="json"), indent=2, sort_keys=True))
    if evaluation.policy_evaluation.status != "pass":
        raise click.exceptions.Exit(1)


@dependency_health.command("assess")
@click.option(
    "--snapshot",
    type=click.Path(path_type=Path, dir_okay=False),
    required=True,
)
def assess_dependency_health(snapshot: Path) -> None:
    """Evaluate absolute high/critical health for one snapshot."""
    try:
        observation = DependencyHealthSnapshot.model_validate(_load_json_object(snapshot))
        evaluation = evaluate_dependency_health_absolute(observation)
    except (OSError, JSONDecodeError, ValidationError, ValueError) as error:
        raise click.ClickException(str(error)) from error
    click.echo(json.dumps(evaluation.model_dump(mode="json"), indent=2, sort_keys=True))
    if evaluation.status != "pass":
        raise click.exceptions.Exit(1)


def _load_json_object(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain one JSON object")
    return payload
