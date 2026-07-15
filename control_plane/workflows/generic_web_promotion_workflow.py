from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import time
from collections.abc import Callable
from typing import Literal
from urllib.parse import quote, urlencode

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.outbox_delivery import (
    OutboxDeliveryRecord,
    retry_outbox_delivery,
)
from control_plane.workflows.generic_web_deploy import product_profile_uses_generic_web_base
from control_plane.workflows.launchplane import github_api_request, resolve_launchplane_github_token

BumpLevel = Literal["patch", "minor", "major"]


class GenericWebPromotionWorkflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    context: str
    dry_run: bool = True
    bump: BumpLevel | None = None
    artifact_id: str = ""
    source_git_ref: str = ""
    evidence_fingerprint: str = ""
    observe_timeout_seconds: int = Field(default=12, ge=0, le=60)

    @model_validator(mode="after")
    def _validate_request(self) -> "GenericWebPromotionWorkflowRequest":
        self.product = self.product.strip()
        self.context = self.context.strip()
        self.artifact_id = self.artifact_id.strip()
        self.source_git_ref = self.source_git_ref.strip()
        self.evidence_fingerprint = self.evidence_fingerprint.strip()
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
    bump: BumpLevel
    dispatch_status: Literal["pending", "dispatched"] = "dispatched"
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
    if not product_profile_uses_generic_web_base(profile):
        raise click.ClickException(
            f"Product {profile.product!r} is configured for driver {profile.driver_id!r}, "
            "not generic-web or a generic-web based driver."
        )
    contexts = {lane.context.strip() for lane in profile.lanes if lane.context.strip()}
    if request.context not in contexts:
        raise click.ClickException(
            "Generic-web promotion workflow context is not in the product profile."
        )
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
    bump = _normalize_bump(request.bump if request.bump is not None else workflow.default_bump)
    previous_run_ids = _workflow_dispatch_run_ids(
        owner=owner,
        repo=repo,
        workflow_id=workflow_id,
        ref=ref,
        token=token,
    )
    dispatch_started_at = _github_timestamp_precision(datetime.now(UTC))
    github_api_request(
        path=f"/repos/{owner}/{repo}/actions/workflows/{quote(workflow_id, safe='')}/dispatches",
        token=token,
        method="POST",
        body={
            "ref": ref,
            "inputs": {
                workflow.dry_run_input.strip(): str(request.dry_run).lower(),
                workflow.bump_input.strip(): bump,
                **(
                    {workflow.artifact_id_input.strip(): request.artifact_id}
                    if request.artifact_id
                    else {}
                ),
                **(
                    {workflow.source_git_ref_input.strip(): request.source_git_ref}
                    if request.source_git_ref
                    else {}
                ),
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
        min_created_at=dispatch_started_at,
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


def dispatch_generic_web_promotion_workflow_delivery(
    *,
    record: OutboxDeliveryRecord,
    control_plane_root: Path,
    mark_provider_started: Callable[[OutboxDeliveryRecord, str, str], None],
) -> OutboxDeliveryRecord:
    delivery_record = record
    payload = dict(record.payload)
    try:
        owner, repo = _repository_parts(_required_payload_text(payload, "repository"))
        workflow_id = _required_payload_text(payload, "workflow_id")
        ref = _required_payload_text(payload, "ref")
        credential_context = _required_payload_text(payload, "credential_context")
        inputs = _string_dict(payload.get("inputs"))
        token = resolve_launchplane_github_token(
            control_plane_root=control_plane_root,
            context_name=credential_context,
        )
        if not token:
            return _failed_outbox_delivery(record, "missing_managed_github_token")
        reconciling_existing_marker = bool(record.provider_operation_key)
        previous_run_ids = _int_set(payload.get("previous_run_ids"))
        min_created_at = _datetime_value(payload.get("dispatch_started_at"))
        if not reconciling_existing_marker:
            if "previous_run_ids" not in payload:
                previous_run_ids = _workflow_dispatch_run_ids(
                    owner=owner,
                    repo=repo,
                    workflow_id=workflow_id,
                    ref=ref,
                    token=token,
                )
            if min_created_at is None:
                min_created_at = _github_timestamp_precision(datetime.now(UTC))
            payload.update(
                {
                    "previous_run_ids": sorted(previous_run_ids),
                    "dispatch_started_at": min_created_at.isoformat().replace("+00:00", "Z"),
                }
            )
        elif min_created_at is None:
            min_created_at = _datetime_value(record.created_at) or datetime.now(UTC)
        provider_operation_key = _workflow_provider_operation_key(
            repository=f"{owner}/{repo}",
            workflow_id=workflow_id,
            ref=ref,
            dispatch_started_at=_github_timestamp_precision(min_created_at)
            .isoformat()
            .replace("+00:00", "Z"),
            inputs=inputs,
        )
        if (
            record.provider_operation_key
            and record.provider_operation_key != provider_operation_key
        ):
            return _failed_outbox_delivery(record, "provider_marker_mismatch")
        delivery_record = record.model_copy(
            update={
                "provider_operation_key": provider_operation_key,
                "provider_id": "github",
                "payload": payload,
            }
        )
        if not reconciling_existing_marker:
            mark_provider_started(delivery_record, provider_operation_key, "github")
        else:
            existing_run = _latest_workflow_dispatch_run(
                owner=owner,
                repo=repo,
                workflow_id=workflow_id,
                ref=ref,
                token=token,
                previous_run_ids=previous_run_ids,
                min_created_at=min_created_at,
            )
            if existing_run:
                return _delivered_workflow_outbox_delivery(delivery_record, existing_run)
            return delivery_record.model_copy(
                update={
                    "state": "reconcile_required",
                    "action": "workflow_dispatch_in_doubt",
                    "error_code": "workflow_run_not_observed",
                }
            )
        github_api_request(
            path=f"/repos/{owner}/{repo}/actions/workflows/{quote(workflow_id, safe='')}/dispatches",
            token=token,
            method="POST",
            body={"ref": ref, "inputs": inputs},
        )
        run = _wait_for_workflow_run(
            owner=owner,
            repo=repo,
            workflow_id=workflow_id,
            ref=ref,
            token=token,
            previous_run_ids=previous_run_ids,
            min_created_at=min_created_at,
            timeout_seconds=_bounded_observe_timeout(payload.get("observe_timeout_seconds")),
        )
        if run:
            return _delivered_workflow_outbox_delivery(delivery_record, run)
        return delivery_record.model_copy(
            update={
                "state": "reconcile_required",
                "provider_operation_key": provider_operation_key,
                "provider_id": "github",
                "action": "workflow_dispatch_sent",
            }
        )
    except click.ClickException as error:
        if error.__cause__ is None:
            return _failed_outbox_delivery(delivery_record, _provider_safe_error_code(error))
        return retry_outbox_delivery(
            delivery_record,
            error_code=_provider_safe_error_code(error),
        )
    except Exception as error:  # noqa: BLE001 - provider failures remain bounded retries.
        return retry_outbox_delivery(
            delivery_record,
            error_code=_provider_safe_error_code(error),
        )


def _normalize_bump(value: str) -> BumpLevel:
    normalized = value.strip()
    if normalized == "patch":
        return "patch"
    if normalized == "minor":
        return "minor"
    if normalized == "major":
        return "major"
    raise click.ClickException("generic web promotion workflow bump must be patch, minor, or major")


def _wait_for_workflow_run(
    *,
    owner: str,
    repo: str,
    workflow_id: str,
    ref: str,
    token: str,
    previous_run_ids: set[int],
    min_created_at: datetime,
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
            min_created_at=min_created_at,
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
    min_created_at: datetime,
) -> dict[str, object]:
    min_created_at = _github_timestamp_precision(min_created_at)
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
        created_at = _datetime_value(raw_run.get("created_at"))
        if created_at is None or created_at < min_created_at:
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
    owner = owner.strip()
    repo = repo.strip()
    if not separator or not owner or not repo or "/" in repo:
        raise click.ClickException("GitHub repository must use owner/repo format.")
    return owner, repo


def _string_value(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _int_value(value: object) -> int:
    return value if isinstance(value, int) else 0


def _datetime_value(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized.removesuffix("Z") + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed.astimezone(UTC)


def _github_timestamp_precision(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(microsecond=0)


def _workflow_provider_operation_key(
    *,
    repository: str,
    workflow_id: str,
    ref: str,
    dispatch_started_at: str,
    inputs: dict[str, str],
) -> str:
    input_fingerprint = "|".join(f"{key}={inputs[key]}" for key in sorted(inputs))
    return ":".join(
        (
            "github_workflow_dispatch",
            repository.strip(),
            workflow_id.strip(),
            ref.strip(),
            dispatch_started_at.strip(),
            input_fingerprint,
        )
    )


def _delivered_workflow_outbox_delivery(
    record: OutboxDeliveryRecord,
    run: dict[str, object],
) -> OutboxDeliveryRecord:
    payload = {
        **record.payload,
        "run_status": _string_value(run.get("status")) or "pending",
        "run_conclusion": _string_value(run.get("conclusion")),
    }
    return record.model_copy(
        update={
            "state": "delivered",
            "provider_id": "github",
            "external_id": str(_int_value(run.get("id")) or ""),
            "external_url": _string_value(run.get("html_url")),
            "action": "dispatched_workflow",
            "error_code": "",
            "payload": payload,
            "lease_owner": "",
            "lease_expires_at": "",
        }
    )


def _failed_outbox_delivery(record: OutboxDeliveryRecord, error_code: str) -> OutboxDeliveryRecord:
    return record.model_copy(
        update={
            "state": "failed",
            "error_code": error_code.strip() or "provider_error",
            "action": "",
            "external_id": "",
            "external_url": "",
            "lease_owner": "",
            "lease_expires_at": "",
        }
    )


def _provider_safe_error_code(error: Exception) -> str:
    if isinstance(error, click.ClickException) and error.__cause__ is None:
        return "github_dispatch_invalid_request"
    return "github_provider_error"


def _required_payload_text(payload: dict[str, object], key: str) -> str:
    value = _optional_payload_text(payload, key)
    if not value:
        raise click.ClickException(f"outbox workflow dispatch payload missing {key}")
    return value


def _optional_payload_text(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    return value.strip() if isinstance(value, str) else ""


def _string_dict(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items()}


def _int_set(value: object) -> set[int]:
    if not isinstance(value, list):
        return set()
    return {item for item in value if isinstance(item, int)}


def _bounded_observe_timeout(value: object) -> int:
    return value if isinstance(value, int) and 0 <= value <= 60 else 0
