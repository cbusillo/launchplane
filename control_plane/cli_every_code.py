import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import cast

import click

from control_plane import every_code_reconciliation as control_plane_every_code_reconciliation
from control_plane import every_code_webhooks as control_plane_every_code_webhooks
from control_plane.every_code_reconciliation import (
    EveryCodeReconciliationStore,
    EveryCodeRerunStore,
)
from control_plane.contracts.every_code_work_request import build_every_code_work_request_id
from control_plane.every_code_worker import (
    EveryCodeWorkerApiError,
    EveryCodeWorkerApiStore,
    EveryCodeWorkerStore,
    apply_every_code_pr_feedback_for_host,
    build_every_code_worker_daemon_spec,
    every_code_worker_daemon_status,
    finish_every_code_work_request,
    request_ready_every_code_pr_preview_labels,
    route_every_code_pr_check_failures,
    run_every_code_worker_loop,
    run_every_code_worker_once,
    start_every_code_worker_daemon,
    stop_every_code_worker_daemon,
)


_DATABASE_URL_ENV_KEYS = ("LAUNCHPLANE_DATABASE_URL",)
_EVERY_CODE_WORKER_TOKEN_ENV_KEY = "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN"
_EVERY_CODE_WEBHOOK_URL_ENV_KEY = "LAUNCHPLANE_EVERY_CODE_WEBHOOK_URL"

StoreFactory = Callable[..., object]


_store_factory: StoreFactory | None = None


def register_every_code_commands(main: click.Group, *, store_factory: StoreFactory) -> None:
    global _store_factory
    _store_factory = store_factory
    main.add_command(every_code)


@click.group("every-code")
def every_code() -> None:
    """Every Code local worker commands."""


def _store(*, state_dir: Path, database_url: str | None = None) -> object:
    if _store_factory is None:
        raise click.ClickException("Every Code CLI store factory is not configured.")
    return _store_factory(state_dir=state_dir, database_url=database_url)


def _reconciliation_store(*, state_dir: Path, database_url: str) -> EveryCodeReconciliationStore:
    return cast(
        EveryCodeReconciliationStore,
        _store(state_dir=state_dir, database_url=database_url),
    )


def _rerun_store(*, state_dir: Path, database_url: str) -> EveryCodeRerunStore:
    return cast(
        EveryCodeRerunStore,
        _store(state_dir=state_dir, database_url=database_url),
    )


def _every_code_worker_store(
    *,
    state_dir: Path,
    database_url: str,
    service_url: str,
    worker_token_env: str,
) -> EveryCodeWorkerStore:
    if service_url.strip():
        token_env_key = worker_token_env.strip() or _EVERY_CODE_WORKER_TOKEN_ENV_KEY
        resolved_token = os.environ.get(token_env_key, "").strip()
        if not resolved_token:
            raise click.ClickException(f"{token_env_key} is required when --service-url is set.")
        return EveryCodeWorkerApiStore(
            service_url=service_url.strip(),
            worker_token=resolved_token,
        )
    return cast(EveryCodeWorkerStore, _store(state_dir=state_dir, database_url=database_url))


def _close_store(record_store: object) -> None:
    close = getattr(record_store, "close", None)
    if callable(close):
        close()


def _gh_current_user_login() -> str:
    try:
        result = subprocess.run(
            ("gh", "api", "user", "--jq", ".login"),
            text=True,
            capture_output=True,
            check=True,
        )
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or str(error)).strip()
        raise click.ClickException(detail) from error
    login = result.stdout.strip()
    if not login:
        raise click.ClickException("Could not determine GitHub user login from gh.")
    return login


@every_code.command("run-once")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    default="",
    show_default=False,
    help="Optional Postgres connection string for Launchplane work-request records.",
)
@click.option(
    "--service-url",
    default="",
    help="Launchplane service base URL for API-backed worker mode.",
)
@click.option(
    "--worker-token-env",
    default=_EVERY_CODE_WORKER_TOKEN_ENV_KEY,
    show_default=True,
    help="Environment variable containing the Every Code worker token.",
)
@click.option(
    "--state-dir",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    default=Path("state"),
    show_default=True,
    help="Filesystem state directory used when --database-url is not set.",
)
@click.option("--host", default="", help="Worker host name recorded on the claim.")
@click.option(
    "--workspace-root",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    default=Path.home() / "Developer",
    show_default=True,
    help="Directory containing repository checkouts.",
)
@click.option(
    "--checkout-root",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    default=None,
    help="Explicit checkout root for the claimed request.",
)
@click.option(
    "--worktree-state-dir",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    default=None,
    help="State directory where worker-owned request worktrees are created.",
)
@click.option("--repository", default="", help="Optional owner/repo queue filter.")
@click.option(
    "--command-template",
    default="",
    help="Optional shell command template for tmux. Fields include {issue_url} and {request_id}.",
)
@click.option("--tmux-binary", default="tmux", show_default=True)
def every_code_run_once(
    database_url: str,
    service_url: str,
    worker_token_env: str,
    state_dir: Path,
    host: str,
    workspace_root: Path,
    checkout_root: Path | None,
    worktree_state_dir: Path | None,
    repository: str,
    command_template: str,
    tmux_binary: str,
) -> None:
    resolved_host = host.strip() or os.uname().nodename
    record_store = _every_code_worker_store(
        state_dir=state_dir,
        database_url=database_url,
        service_url=service_url,
        worker_token_env=worker_token_env,
    )
    try:
        apply_every_code_pr_feedback_for_host(
            record_store=record_store,
            host=resolved_host,
            state_dir=state_dir,
            database_url=database_url,
            service_url=service_url,
            worker_token_env=worker_token_env,
            tmux_binary=tmux_binary,
        )
        request_ready_every_code_pr_preview_labels(
            record_store=record_store,
            repository=repository,
        )
        route_every_code_pr_check_failures(
            record_store=record_store,
            repository=repository,
        )
        apply_every_code_pr_feedback_for_host(
            record_store=record_store,
            host=resolved_host,
            state_dir=state_dir,
            database_url=database_url,
            service_url=service_url,
            worker_token_env=worker_token_env,
            tmux_binary=tmux_binary,
        )
        result = run_every_code_worker_once(
            record_store=record_store,
            host=resolved_host,
            workspace_root=workspace_root,
            checkout_root=checkout_root,
            worktree_state_dir=worktree_state_dir,
            repository=repository,
            command_template=command_template,
            state_dir=state_dir,
            database_url=database_url,
            service_url=service_url,
            worker_token_env=worker_token_env,
            tmux_binary=tmux_binary,
        )
    finally:
        _close_store(record_store)
    click.echo(json.dumps(result.as_payload(), indent=2, sort_keys=True))


@every_code.command("run")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    default="",
    show_default=False,
    help="Optional Postgres connection string for Launchplane work-request records.",
)
@click.option(
    "--service-url",
    default="",
    help="Launchplane service base URL for API-backed worker mode.",
)
@click.option(
    "--worker-token-env",
    default=_EVERY_CODE_WORKER_TOKEN_ENV_KEY,
    show_default=True,
    help="Environment variable containing the Every Code worker token.",
)
@click.option(
    "--state-dir",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    default=Path("state"),
    show_default=True,
    help="Filesystem state directory used when --database-url is not set.",
)
@click.option("--host", default="", help="Worker host name recorded on claims.")
@click.option(
    "--workspace-root",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    default=Path.home() / "Developer",
    show_default=True,
    help="Directory containing repository checkouts.",
)
@click.option(
    "--checkout-root",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    default=None,
    help="Explicit checkout root for claimed requests.",
)
@click.option(
    "--worktree-state-dir",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    default=None,
    help="State directory where worker-owned request worktrees are created.",
)
@click.option("--repository", default="", help="Optional owner/repo queue filter.")
@click.option(
    "--command-template",
    default="",
    help="Optional shell command template for tmux. Fields include {issue_url} and {request_id}.",
)
@click.option("--tmux-binary", default="tmux", show_default=True)
@click.option(
    "--interval-seconds",
    type=click.FloatRange(min=0),
    default=60.0,
    show_default=True,
    help="Seconds to sleep between queue scans.",
)
@click.option(
    "--max-iterations",
    type=click.IntRange(min=0),
    default=0,
    show_default=True,
    help="Stop after this many scans; 0 runs until interrupted.",
)
def every_code_run(
    database_url: str,
    service_url: str,
    worker_token_env: str,
    state_dir: Path,
    host: str,
    workspace_root: Path,
    checkout_root: Path | None,
    worktree_state_dir: Path | None,
    repository: str,
    command_template: str,
    tmux_binary: str,
    interval_seconds: float,
    max_iterations: int,
) -> None:
    resolved_host = host.strip() or os.uname().nodename
    record_store = _every_code_worker_store(
        state_dir=state_dir,
        database_url=database_url,
        service_url=service_url,
        worker_token_env=worker_token_env,
    )
    try:
        result = run_every_code_worker_loop(
            record_store=record_store,
            host=resolved_host,
            workspace_root=workspace_root,
            checkout_root=checkout_root,
            worktree_state_dir=worktree_state_dir,
            repository=repository,
            command_template=command_template,
            state_dir=state_dir,
            database_url=database_url,
            service_url=service_url,
            worker_token_env=worker_token_env,
            tmux_binary=tmux_binary,
            interval_seconds=interval_seconds,
            max_iterations=max_iterations,
        )
    except KeyboardInterrupt:
        raise click.ClickException("Every Code worker stopped by interrupt.") from None
    finally:
        _close_store(record_store)
    click.echo(json.dumps(result.as_payload(), indent=2, sort_keys=True))


@every_code.command("start")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    default="",
    show_default=False,
    help="Optional Postgres connection string for Launchplane work-request records.",
)
@click.option(
    "--service-url",
    default="",
    help="Launchplane service base URL for API-backed worker mode.",
)
@click.option(
    "--worker-token-env",
    default=_EVERY_CODE_WORKER_TOKEN_ENV_KEY,
    show_default=True,
    help="Environment variable containing the Every Code worker token.",
)
@click.option(
    "--state-dir",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    default=Path("state"),
    show_default=True,
    help="Filesystem state directory used when --database-url is not set.",
)
@click.option("--host", default="", help="Worker host name recorded on claims.")
@click.option(
    "--workspace-root",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    default=Path.home() / "Developer",
    show_default=True,
    help="Directory containing repository checkouts.",
)
@click.option(
    "--checkout-root",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    default=None,
    help="Explicit checkout root for claimed requests.",
)
@click.option(
    "--worktree-state-dir",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    default=None,
    help="State directory where worker-owned request worktrees are created.",
)
@click.option("--repository", default="", help="Optional owner/repo queue filter.")
@click.option(
    "--command-template",
    default="",
    help="Optional shell command template for tmux. Fields include {issue_url} and {request_id}.",
)
@click.option("--tmux-binary", default="tmux", show_default=True)
@click.option(
    "--interval-seconds",
    type=click.FloatRange(min=0),
    default=60.0,
    show_default=True,
    help="Seconds to sleep between queue scans.",
)
def every_code_start(
    database_url: str,
    service_url: str,
    worker_token_env: str,
    state_dir: Path,
    host: str,
    workspace_root: Path,
    checkout_root: Path | None,
    worktree_state_dir: Path | None,
    repository: str,
    command_template: str,
    tmux_binary: str,
    interval_seconds: float,
) -> None:
    if service_url.strip():
        token_env_key = worker_token_env.strip() or _EVERY_CODE_WORKER_TOKEN_ENV_KEY
        if not os.environ.get(token_env_key, "").strip():
            raise click.ClickException(f"{token_env_key} is required when --service-url is set.")
    spec = build_every_code_worker_daemon_spec(
        state_dir=state_dir,
        database_url=database_url,
        service_url=service_url,
        worker_token_env=worker_token_env,
        host=host.strip() or os.uname().nodename,
        workspace_root=workspace_root,
        checkout_root=checkout_root,
        worktree_state_dir=worktree_state_dir,
        repository=repository,
        command_template=command_template,
        tmux_binary=tmux_binary,
        interval_seconds=interval_seconds,
    )
    result = start_every_code_worker_daemon(spec=spec, cwd=Path.cwd())
    click.echo(json.dumps(result.as_payload(), indent=2, sort_keys=True))


@every_code.command("stop")
@click.option(
    "--state-dir",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    default=Path("state"),
    show_default=True,
    help="Filesystem state directory containing the worker pid file.",
)
def every_code_stop(state_dir: Path) -> None:
    spec = build_every_code_worker_daemon_spec(state_dir=state_dir)
    result = stop_every_code_worker_daemon(spec=spec)
    click.echo(json.dumps(result.as_payload(), indent=2, sort_keys=True))


@every_code.command("status")
@click.option(
    "--state-dir",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    default=Path("state"),
    show_default=True,
    help="Filesystem state directory containing the worker pid file.",
)
def every_code_status(state_dir: Path) -> None:
    spec = build_every_code_worker_daemon_spec(state_dir=state_dir)
    result = every_code_worker_daemon_status(spec=spec)
    click.echo(json.dumps(result.as_payload(), indent=2, sort_keys=True))


@every_code.command("finish")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    default="",
    show_default=False,
    help="Optional Postgres connection string for Launchplane work-request records.",
)
@click.option(
    "--service-url",
    default="",
    help="Launchplane service base URL for API-backed worker mode.",
)
@click.option(
    "--worker-token-env",
    default=_EVERY_CODE_WORKER_TOKEN_ENV_KEY,
    show_default=True,
    help="Environment variable containing the Every Code worker token.",
)
@click.option(
    "--state-dir",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    default=Path("state"),
    show_default=True,
    help="Filesystem state directory used when --database-url is not set.",
)
@click.option("--request-id", required=True, help="Every Code work request id.")
@click.option("--host", default="", help="Worker host name that claimed the request.")
@click.option("--exit-code", type=int, required=True, help="Visible session exit code.")
@click.option("--result-pr-url", default="", help="Optional PR URL created by the session.")
@click.option("--result-summary", default="", help="Optional terminal result summary.")
def every_code_finish(
    database_url: str,
    service_url: str,
    worker_token_env: str,
    state_dir: Path,
    request_id: str,
    host: str,
    exit_code: int,
    result_pr_url: str,
    result_summary: str,
) -> None:
    resolved_host = host.strip() or os.uname().nodename
    record_store = _every_code_worker_store(
        state_dir=state_dir,
        database_url=database_url,
        service_url=service_url,
        worker_token_env=worker_token_env,
    )
    try:
        result = finish_every_code_work_request(
            record_store=record_store,
            request_id=request_id,
            host=resolved_host,
            exit_code=exit_code,
            result_pr_url=result_pr_url,
            result_summary=result_summary,
        )
    finally:
        _close_store(record_store)
    click.echo(json.dumps(result.as_payload(), indent=2, sort_keys=True))


@every_code.command("reconcile-previews")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    default="",
    show_default=False,
    help="Optional Postgres connection string for Launchplane work-request records.",
)
@click.option(
    "--service-url",
    default="",
    help="Launchplane service base URL for API-backed worker mode.",
)
@click.option(
    "--worker-token-env",
    default=_EVERY_CODE_WORKER_TOKEN_ENV_KEY,
    show_default=True,
    help="Environment variable containing the Every Code worker token.",
)
@click.option(
    "--state-dir",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    default=Path("state"),
    show_default=True,
    help="Filesystem state directory used when --database-url is not set.",
)
@click.option("--repository", default="", help="Optional owner/repo filter.")
@click.option("--limit", type=click.IntRange(min=1), default=50, show_default=True)
def every_code_reconcile_previews(
    database_url: str,
    service_url: str,
    worker_token_env: str,
    state_dir: Path,
    repository: str,
    limit: int,
) -> None:
    record_store = _every_code_worker_store(
        state_dir=state_dir,
        database_url=database_url,
        service_url=service_url,
        worker_token_env=worker_token_env,
    )
    try:
        result = request_ready_every_code_pr_preview_labels(
            record_store=record_store,
            repository=repository,
            limit=limit,
        )
    finally:
        _close_store(record_store)
    click.echo(json.dumps(result.as_payload(), indent=2, sort_keys=True))


@every_code.command("rerun-issue")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    default="",
    show_default=False,
    help="Optional Postgres connection string for Launchplane work-request records.",
)
@click.option(
    "--service-url",
    default="",
    help="Launchplane service base URL for API-backed worker mode.",
)
@click.option(
    "--worker-token-env",
    default=_EVERY_CODE_WORKER_TOKEN_ENV_KEY,
    show_default=True,
    help="Environment variable containing the Every Code worker token.",
)
@click.option(
    "--state-dir",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    default=Path("state"),
    show_default=True,
    help="Filesystem state directory used when --database-url is not set.",
)
@click.option("--repository", required=True, help="GitHub owner/repo name.")
@click.option("--issue-number", type=int, required=True, help="GitHub issue number.")
@click.option("--trigger-label", default="every-code", show_default=True)
@click.option("--actor", default="rerun", show_default=True)
def every_code_rerun_issue(
    database_url: str,
    service_url: str,
    worker_token_env: str,
    state_dir: Path,
    repository: str,
    issue_number: int,
    trigger_label: str,
    actor: str,
) -> None:
    record_store: EveryCodeWorkerStore | EveryCodeRerunStore
    if service_url.strip():
        record_store = _every_code_worker_store(
            state_dir=state_dir,
            database_url=database_url,
            service_url=service_url,
            worker_token_env=worker_token_env,
        )
        try:
            request_id = build_every_code_work_request_id(
                repository=repository,
                issue_number=issue_number,
                trigger_label=trigger_label,
            )
            rerun = getattr(record_store, "rerun_every_code_work_request_record")
            request = rerun(request_id=request_id, trigger_actor=actor)
            payload = {
                "status": "queued",
                "detail": "Requeued terminal Every Code work request for this issue.",
                "request": request.model_dump(mode="json"),
            }
        except (EveryCodeWorkerApiError, ValueError) as error:
            raise click.ClickException(str(error)) from error
        finally:
            _close_store(record_store)
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
        return

    record_store = _rerun_store(state_dir=state_dir, database_url=database_url)
    try:
        result = control_plane_every_code_reconciliation.rerun_every_code_issue(
            record_store=record_store,
            repository=repository,
            issue_number=issue_number,
            trigger_label=trigger_label,
            actor=actor,
        )
    except (FileNotFoundError, ValueError) as error:
        raise click.ClickException(str(error)) from error
    finally:
        _close_store(record_store)
    click.echo(json.dumps(result.as_payload(), indent=2, sort_keys=True))


@every_code.command("reconcile-issue")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    default="",
    show_default=False,
    help="Optional Postgres connection string for Launchplane work-request records.",
)
@click.option(
    "--state-dir",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    default=Path("state"),
    show_default=True,
    help="Filesystem state directory used when --database-url is not set.",
)
@click.option("--repository", required=True, help="GitHub owner/repo name.")
@click.option("--issue-number", type=int, required=True, help="GitHub issue number.")
@click.option("--issue-url", required=True, help="GitHub issue URL.")
@click.option("--issue-title", default="", help="GitHub issue title.")
@click.option(
    "--label",
    "labels",
    multiple=True,
    help="Issue label name. Repeat for each current issue label.",
)
@click.option("--trigger-label", default="every-code", show_default=True)
@click.option("--actor", default="reconciliation", show_default=True)
def every_code_reconcile_issue(
    database_url: str,
    state_dir: Path,
    repository: str,
    issue_number: int,
    issue_url: str,
    issue_title: str,
    labels: tuple[str, ...],
    trigger_label: str,
    actor: str,
) -> None:
    record_store = _reconciliation_store(state_dir=state_dir, database_url=database_url)
    try:
        result = control_plane_every_code_reconciliation.reconcile_every_code_issue(
            record_store=record_store,
            repository=repository,
            issue_number=issue_number,
            issue_url=issue_url,
            issue_title=issue_title,
            labels=labels,
            trigger_label=trigger_label,
            actor=actor,
        )
    except ValueError as error:
        raise click.ClickException(str(error)) from error
    finally:
        _close_store(record_store)
    click.echo(json.dumps(result.as_payload(), indent=2, sort_keys=True))


@every_code.command("sync-webhooks")
@click.option("--owner", default="", help="GitHub owner to scan; defaults to gh api user.")
@click.option("--topic", default="every-code", show_default=True)
@click.option(
    "--webhook-url",
    envvar=_EVERY_CODE_WEBHOOK_URL_ENV_KEY,
    default="",
    help=(
        "Every Code GitHub webhook URL. Can also be supplied via "
        f"{_EVERY_CODE_WEBHOOK_URL_ENV_KEY}."
    ),
)
@click.option(
    "--webhook-secret-env",
    default="LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET",
    show_default=True,
)
def every_code_sync_webhooks(
    owner: str,
    topic: str,
    webhook_url: str,
    webhook_secret_env: str,
) -> None:
    resolved_owner = owner.strip() or _gh_current_user_login()
    secret_env_key = webhook_secret_env.strip()
    webhook_secret = os.environ.get(secret_env_key, "").strip()
    if not webhook_secret:
        raise click.ClickException(f"{secret_env_key} is required.")
    resolved_webhook_url = webhook_url.strip()
    if not resolved_webhook_url:
        raise click.ClickException(
            f"--webhook-url or {_EVERY_CODE_WEBHOOK_URL_ENV_KEY} is required."
        )
    try:
        results = control_plane_every_code_webhooks.sync_every_code_webhooks(
            owner=resolved_owner,
            webhook_secret=webhook_secret,
            webhook_url=resolved_webhook_url,
            topic=topic,
        )
    except (ValueError, subprocess.CalledProcessError) as error:
        raise click.ClickException(str(error)) from error
    failed = [result for result in results if result.status == "error"]
    click.echo(
        json.dumps(
            {
                "status": "error" if failed else "ok",
                "owner": resolved_owner,
                "topic": topic,
                "webhook_url": resolved_webhook_url,
                "events": list(control_plane_every_code_webhooks.EVERY_CODE_WEBHOOK_EVENTS),
                "repositories": [result.as_payload() for result in results],
            },
            indent=2,
            sort_keys=True,
        )
    )
    if failed:
        raise click.ClickException(f"Failed to sync {len(failed)} Every Code webhook(s).")
