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
    DependencyHealthSnapshotProvenance,
    evaluate_dependency_health_absolute,
    evaluate_dependency_health_regression,
    extract_dependency_health_advisory_ids,
)
from control_plane.contracts.dependency_health_trivy import (
    dependency_health_snapshot_from_trivy_report,
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
@click.option("--target-advisory-id", multiple=True)
@click.option(
    "--target-advisory-text-file",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
)
def compare_dependency_health(
    baseline_snapshot: Path,
    candidate_snapshot: Path,
    policy_file: Path | None,
    target_advisory_id: tuple[str, ...],
    target_advisory_text_file: Path | None,
) -> None:
    """Compare one candidate snapshot against its asserted baseline."""
    try:
        baseline = DependencyHealthSnapshot.model_validate(_load_json_object(baseline_snapshot))
        candidate = DependencyHealthSnapshot.model_validate(_load_json_object(candidate_snapshot))
        policy = _load_dependency_health_policy(
            policy_file=policy_file,
            target_advisory_ids=target_advisory_id,
            target_advisory_text_file=target_advisory_text_file,
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


@dependency_health.command("trivy-snapshot")
@click.option(
    "--report",
    type=click.Path(path_type=Path, dir_okay=False),
    required=True,
)
@click.option("--repository", required=True)
@click.option("--source-commit", required=True)
@click.option("--baseline-commit", default="")
@click.option("--producer-version", required=True)
@click.option("--advisory-source", default="trivy-db", show_default=True)
@click.option("--advisory-revision", required=True)
@click.option("--scan-scope", required=True)
@click.option("--scan-configuration-sha256", required=True)
def trivy_dependency_health_snapshot(
    report: Path,
    repository: str,
    source_commit: str,
    baseline_commit: str,
    producer_version: str,
    advisory_source: str,
    advisory_revision: str,
    scan_scope: str,
    scan_configuration_sha256: str,
) -> None:
    """Normalize one Trivy JSON report into a dependency-health snapshot."""
    try:
        snapshot = dependency_health_snapshot_from_trivy_report(
            report=_load_json_object(report),
            provenance=DependencyHealthSnapshotProvenance(
                repository=repository,
                source_commit=source_commit,
                baseline_commit=baseline_commit,
                producer="trivy",
                producer_version=producer_version,
                advisory_source=advisory_source,
                advisory_revision=advisory_revision,
                scan_scope=scan_scope,
                scan_configuration_sha256=scan_configuration_sha256,
            ),
        )
    except (OSError, JSONDecodeError, ValidationError, ValueError) as error:
        raise click.ClickException(str(error)) from error
    click.echo(json.dumps(snapshot.model_dump(mode="json"), indent=2, sort_keys=True))


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


def _load_dependency_health_policy(
    *,
    policy_file: Path | None,
    target_advisory_ids: tuple[str, ...],
    target_advisory_text_file: Path | None,
) -> DependencyHealthPolicy:
    if policy_file is not None:
        if target_advisory_ids or target_advisory_text_file is not None:
            raise ValueError(
                "dependency health policy-file cannot be combined with target advisory options"
            )
        return DependencyHealthPolicy.model_validate(_load_json_object(policy_file))

    extracted_ids: tuple[str, ...] = ()
    if target_advisory_text_file is not None:
        extracted_ids = extract_dependency_health_advisory_ids(
            target_advisory_text_file.read_text(encoding="utf-8")
        )
    return DependencyHealthPolicy(
        target_advisory_ids=(*target_advisory_ids, *extracted_ids),
    )
