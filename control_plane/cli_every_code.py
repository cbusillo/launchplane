import json
import os
from pathlib import Path

import click

from control_plane.engineering_review_worker import (
    EngineeringReviewWorkerApiStore,
    run_engineering_review_worker_once,
)

_EVERY_CODE_WORKER_TOKEN_ENV_KEY = "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN"


def register_every_code_commands(main: click.Group) -> None:
    main.add_command(every_code)


@click.group("every-code", help="Run advisory engineering review for retained work requests.")
def every_code() -> None:
    pass


@every_code.command("review-run-once")
@click.option("--service-url", envvar="LAUNCHPLANE_SERVICE_URL", required=True)
@click.option("--worker-token-env", default=_EVERY_CODE_WORKER_TOKEN_ENV_KEY, show_default=True)
@click.option("--worker-runtime-id", required=True)
@click.option("--host", "worker_host", required=True)
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
def engineering_review_run_once(
    service_url: str,
    worker_token_env: str,
    worker_runtime_id: str,
    worker_host: str,
    state_dir: Path,
) -> None:
    """Claim and execute one DB-authorized shadow engineering review run."""
    worker_token = os.environ.get(worker_token_env, "").strip()
    if not worker_token:
        raise click.ClickException(f"{worker_token_env} is required.")
    result = run_engineering_review_worker_once(
        store=EngineeringReviewWorkerApiStore(
            service_url=service_url,
            worker_token=worker_token,
        ),
        worker_runtime_id=worker_runtime_id,
        worker_host=worker_host,
        state_dir=state_dir,
        service_url=service_url,
    )
    click.echo(
        json.dumps(
            {
                "status": "empty" if result is None else result.state,
                "run": None if result is None else result.model_dump(mode="json"),
            },
            sort_keys=True,
        )
    )
