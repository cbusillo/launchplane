from json import JSONDecodeError
import json
import os
from pathlib import Path

import click
from pydantic import ValidationError

from control_plane.contracts.merge_train_policy import load_merge_train_policy
from control_plane.contracts.work_graph_read_model import (
    WorkGraphSnapshot,
    build_work_graph_queue,
)
from control_plane.merge_train import (
    MergeTrainDryRunSnapshot,
    build_merge_train_dry_run_result,
)
from control_plane.merge_train_github import (
    GitHubMergeTrainClient,
    GitHubMergeTrainSnapshotReader,
    MergeTrainGitHubError,
    UrllibMergeTrainGitHubTransport,
)
from control_plane.workflows.merge_train_worker import (
    MergeTrainWorkerClients,
    run_merge_train_worker_step,
)


_MERGE_TRAIN_GITHUB_TOKEN_ENV_KEY = "GH_TOKEN"


def register_work_graph_core_commands(work_graph: click.Group) -> None:
    work_graph.add_command(work_graph_rank, name="rank")
    work_graph.add_command(merge_train_policy, name="merge-train-policy")
    work_graph.add_command(merge_train_dry_run, name="merge-train-dry-run")
    work_graph.add_command(merge_train_run_once, name="merge-train-run-once")


@click.command("rank")
@click.option(
    "--snapshot-file",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    required=True,
    help="Operator-provided Code Plans/GitHub work graph snapshot JSON.",
)
@click.option("--limit", type=click.IntRange(min=1), default=25, show_default=True)
def work_graph_rank(snapshot_file: Path, limit: int) -> None:
    try:
        snapshot_payload = json.loads(snapshot_file.read_text())
        snapshot = WorkGraphSnapshot.model_validate(snapshot_payload)
        queue = build_work_graph_queue(snapshot, limit=limit)
    except (OSError, JSONDecodeError, ValidationError, ValueError) as error:
        raise click.ClickException(str(error)) from error
    click.echo(json.dumps(queue.model_dump(mode="json"), indent=2, sort_keys=True))


@click.command("merge-train-policy")
@click.option(
    "--policy-file",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    required=True,
    help="Merge-train policy TOML to validate before importing it as a DB record.",
)
@click.option("--repository", default="", help="Optional owner/name repository lookup.")
@click.option("--base-branch", default="main", show_default=True)
def merge_train_policy(policy_file: Path, repository: str, base_branch: str) -> None:
    try:
        policy = load_merge_train_policy(policy_file)
        selected_policy = None
        if repository.strip():
            selected_policy = policy.find_repository_policy(
                repository=repository, base_branch=base_branch
            )
    except (OSError, ValidationError, ValueError) as error:
        raise click.ClickException(str(error)) from error
    payload: dict[str, object] = {
        "status": "ok",
        "policy_sha256": policy.policy_sha256,
        "repository_count": len(policy.policies),
        "policies": [
            repository_policy.model_dump(mode="json") for repository_policy in policy.policies
        ],
    }
    if selected_policy is not None:
        payload["selected_policy"] = selected_policy.model_dump(mode="json")
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


@click.command("merge-train-dry-run")
@click.option(
    "--snapshot-file",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    required=True,
    help="Merge-train dry-run snapshot JSON containing repository, base_branch, and pull_requests.",
)
@click.option(
    "--policy-file",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    required=True,
    help="Merge-train policy TOML to use for this local dry-run.",
)
def merge_train_dry_run(snapshot_file: Path, policy_file: Path) -> None:
    try:
        snapshot_payload = json.loads(snapshot_file.read_text(encoding="utf-8"))
        snapshot = MergeTrainDryRunSnapshot.model_validate(snapshot_payload)
        policy = load_merge_train_policy(policy_file)
        result = build_merge_train_dry_run_result(policy=policy, snapshot=snapshot)
    except (OSError, JSONDecodeError, ValidationError, ValueError) as error:
        raise click.ClickException(str(error)) from error
    click.echo(json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True))


@click.command("merge-train-run-once")
@click.option(
    "--policy-file",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    required=True,
    help="Merge-train policy TOML to use for this local run.",
)
@click.option(
    "--repository",
    default="cbusillo/codex-skills",
    show_default=True,
    help="owner/name repository whose policy should run once.",
)
@click.option("--base-branch", default="main", show_default=True)
@click.option(
    "--github-token-env",
    default=_MERGE_TRAIN_GITHUB_TOKEN_ENV_KEY,
    show_default=True,
    help="Environment variable containing the GitHub token used by the merge train.",
)
@click.option(
    "--github-api-base-url",
    default="https://api.github.com",
    show_default=True,
    help="GitHub API base URL.",
)
@click.option(
    "--mutate/--dry-run",
    default=False,
    show_default=True,
    help="Apply the selected worker transition. The default only reads GitHub and reports intent.",
)
def merge_train_run_once(
    policy_file: Path,
    repository: str,
    base_branch: str,
    github_token_env: str,
    github_api_base_url: str,
    mutate: bool,
) -> None:
    try:
        policy = load_merge_train_policy(policy_file)
        policy.find_repository_policy(repository=repository, base_branch=base_branch)
        token_env = github_token_env.strip()
        if not token_env:
            raise click.ClickException("merge-train run-once requires --github-token-env.")
        token = os.environ.get(token_env, "").strip()
        if not token:
            raise click.ClickException(f"Missing GitHub token in environment variable {token_env}.")
        transport = UrllibMergeTrainGitHubTransport(token=token, api_base_url=github_api_base_url)
        snapshot_reader = GitHubMergeTrainSnapshotReader(transport=transport)
        snapshot = snapshot_reader.read_merge_train_snapshot(
            repository=repository, base_branch=base_branch
        )
        dry_run_result = build_merge_train_dry_run_result(policy=policy, snapshot=snapshot)
        if mutate:
            github_client = GitHubMergeTrainClient(transport=transport)
            worker_step_result = run_merge_train_worker_step(
                policy=policy,
                snapshot=snapshot,
                clients=MergeTrainWorkerClients(
                    label_client=github_client,
                    branch_client=github_client,
                    merge_client=github_client,
                ),
            )
            payload: dict[str, object] = worker_step_result.model_dump(mode="json")
            payload["mode"] = "mutate"
        else:
            payload = {
                "mode": "dry-run",
                "repository": snapshot.repository,
                "base_branch": snapshot.base_branch,
                "snapshot": snapshot.model_dump(mode="json"),
                "dry_run_result": dry_run_result.model_dump(mode="json"),
            }
    except MergeTrainGitHubError as error:
        detail = str(error)
        if error.status_code is not None:
            detail = f"{detail} (HTTP {error.status_code})"
        raise click.ClickException(f"GitHub merge train request failed: {detail}") from error
    except (OSError, ValidationError, ValueError) as error:
        raise click.ClickException(str(error)) from error
    click.echo(json.dumps(payload, indent=2, sort_keys=True))
