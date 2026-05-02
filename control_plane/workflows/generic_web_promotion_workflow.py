from __future__ import annotations

from pathlib import Path
import time
from typing import Literal
from urllib.parse import quote, urlencode

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.workflows.launchplane import github_api_request, resolve_launchplane_github_token


class GenericWebPromotionWorkflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    context: str
    dry_run: bool = True
    bump: Literal["patch", "minor", "major"] | None = None
    observe_timeout_seconds: int = Field(default=12, ge=0, le=60)

    @model_validator(mode="after")
    def _validate_request(self) -> "GenericWebPromotionWorkflowRequest":
        self.product = self.product.strip()
        self.context = self.context.strip()
        if not self.product:
            raise ValueError("generic web promotion workflow requires product")
        if not self.context:
            raise ValueError("generic web promotion workflow requires context")
        return self


class GenericWebPromotionWorkflowResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    context: str
    repository: str
    workflow_id: str
    ref: str
    dry_run: bool
    bump: Literal["patch", "minor", "major"]
    dispatch_status: Literal["dispatched"] = "dispatched"
    run_id: int = 0
    run_url: str = ""
    run_status: str = "pending"
    run_conclusion: str = ""


def dispatch_generic_web_promotion_workflow(
    *,
    control_plane_root: Path,
    profile: LaunchplaneProductProfileRecord,
    request: GenericWebPromotionWorkflowRequest,
) -> GenericWebPromotionWorkflowResult:
    if profile.product.strip() != request.product:
        raise click.ClickException("Generic-web promotion workflow product does not match profile.")
    contexts = {lane.context.strip() for lane in profile.lanes if lane.context.strip()}
    if request.context not in contexts:
        raise click.ClickException("Generic-web promotion workflow context is not in the product profile.")
    owner, repo = _repository_parts(profile.repository)
    token = resolve_launchplane_github_token(
        control_plane_root=control_plane_root,
        context_name=request.context,
    )
    if not token:
        raise click.ClickException(
            "Launchplane runtime records do not expose GITHUB_TOKEN for this context"
        )
    workflow = profile.promotion_workflow
    workflow_id = workflow.workflow_id.strip()
    ref = workflow.ref.strip()
    bump = request.bump or workflow.default_bump.strip()
    previous_run_ids = _workflow_dispatch_run_ids(
        owner=owner,
        repo=repo,
        workflow_id=workflow_id,
        ref=ref,
        token=token,
    )
    github_api_request(
        path=f"/repos/{owner}/{repo}/actions/workflows/{quote(workflow_id, safe='')}/dispatches",
        token=token,
        method="POST",
        body={
            "ref": ref,
            "inputs": {
                workflow.dry_run_input.strip(): str(request.dry_run).lower(),
                workflow.bump_input.strip(): bump,
            },
        },
    )
    run = _wait_for_workflow_run(
        owner=owner,
        repo=repo,
        workflow_id=workflow_id,
        ref=ref,
        token=token,
        previous_run_ids=previous_run_ids,
        timeout_seconds=request.observe_timeout_seconds,
    )
    return GenericWebPromotionWorkflowResult(
        product=profile.product,
        context=request.context,
        repository=profile.repository,
        workflow_id=workflow_id,
        ref=ref,
        dry_run=request.dry_run,
        bump=bump,
        run_id=_int_value(run.get("id")) if run else 0,
        run_url=_string_value(run.get("html_url")) if run else "",
        run_status=_string_value(run.get("status")) if run else "pending",
        run_conclusion=_string_value(run.get("conclusion")) if run else "",
    )


def _wait_for_workflow_run(
    *,
    owner: str,
    repo: str,
    workflow_id: str,
    ref: str,
    token: str,
    previous_run_ids: set[int],
    timeout_seconds: int,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        run = _latest_workflow_dispatch_run(
            owner=owner,
            repo=repo,
            workflow_id=workflow_id,
            ref=ref,
            token=token,
            previous_run_ids=previous_run_ids,
        )
        if run:
            return run
        if time.monotonic() >= deadline:
            return {}
        time.sleep(1)


def _latest_workflow_dispatch_run(
    *,
    owner: str,
    repo: str,
    workflow_id: str,
    ref: str,
    token: str,
    previous_run_ids: set[int],
) -> dict[str, object]:
    workflow_runs = _workflow_dispatch_runs(
        owner=owner,
        repo=repo,
        workflow_id=workflow_id,
        ref=ref,
        token=token,
    )
    new_runs: list[dict[str, object]] = []
    for raw_run in workflow_runs:
        run_id = _int_value(raw_run.get("id"))
        if not run_id or run_id in previous_run_ids:
            continue
        new_runs.append(raw_run)
    if len(new_runs) == 1:
        return new_runs[0]
    return {}


def _workflow_dispatch_run_ids(
    *, owner: str, repo: str, workflow_id: str, ref: str, token: str
) -> set[int]:
    return {
        run_id
        for run_id in (
            _int_value(run.get("id"))
            for run in _workflow_dispatch_runs(
                owner=owner,
                repo=repo,
                workflow_id=workflow_id,
                ref=ref,
                token=token,
            )
        )
        if run_id
    }


def _workflow_dispatch_runs(
    *, owner: str, repo: str, workflow_id: str, ref: str, token: str
) -> list[dict[str, object]]:
    query = urlencode({"event": "workflow_dispatch", "branch": ref, "per_page": 10})
    payload = github_api_request(
        path=f"/repos/{owner}/{repo}/actions/workflows/{quote(workflow_id, safe='')}/runs?{query}",
        token=token,
    )
    if not isinstance(payload, dict):
        raise click.ClickException(
            f"GitHub workflow runs response for {owner}/{repo}/{workflow_id} must be an object."
        )
    workflow_runs = payload.get("workflow_runs")
    if not isinstance(workflow_runs, list):
        raise click.ClickException(
            f"GitHub workflow runs response for {owner}/{repo}/{workflow_id} is missing workflow_runs."
        )
    return [run for run in workflow_runs if isinstance(run, dict)]


def _repository_parts(repository: str) -> tuple[str, str]:
    owner, separator, repo = repository.strip().partition("/")
    if not separator or not owner.strip() or not repo.strip() or "/" in repo.strip():
        raise click.ClickException("GitHub repository must use owner/repo format.")
    return owner.strip(), repo.strip()


def _string_value(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _int_value(value: object) -> int:
    return value if isinstance(value, int) else 0
