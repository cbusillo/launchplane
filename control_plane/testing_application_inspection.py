from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal

import click
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from control_plane.contracts.runtime_identity import RuntimeIdentityStatus
from control_plane.contracts.testing_activation import (
    TestingActivationRecord,
    TestingActivationRefusal,
)
from control_plane.dokploy import api, source
from control_plane.http_error_evidence import log_handled_error_cause
from control_plane.provider_operations import provider_operation_title
from control_plane.testing_activation import preparation_resources
from control_plane.testing_application_runtime import (
    inspect_application_runtime,
    VerifiedApplicationRuntimeFailure,
)

if TYPE_CHECKING:
    from control_plane.testing_application_activation import ApplicationActivationAdapter

CandidateDeploymentStatus = Literal[
    "terminal_success", "terminal_failure", "pending", "missing", "unrecognized"
]
CandidateDomains = Literal["missing", "malformed", "empty", "nonempty"]


class ApplicationCandidateInspection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    placement: Literal["not_created", "unavailable", "matched", "changed"]
    domains: CandidateDomains | None = None
    history: Literal["unknown", "complete"] = "unknown"
    deployment_count: int | None = Field(default=None, ge=0)
    stored_deployment_recorded: bool = False
    sole_deployment_id_matches: bool | None = None
    sole_deployment_title_matches: bool | None = None
    sole_deployment_status: CandidateDeploymentStatus | None = None
    current_runtime_status: Literal["not_read", "unavailable", "available"] = "not_read"
    current_http_status: int | None = Field(default=None, ge=100, le=599)
    current_identity_status: RuntimeIdentityStatus | None = None
    current_task_failed: bool | None = None
    current_observed_at: AwareDatetime | None = None


def candidate_deployment_status(row: api.JsonObject) -> CandidateDeploymentStatus:
    status = api.deployment_status(row)
    if status in api._DEPLOYMENT_SUCCESS_STATUSES:
        return "terminal_success"
    if status in api._DEPLOYMENT_FAILURE_STATUSES:
        return "terminal_failure"
    if status in {"queued", "running", "pending", "building", "deploying", "in_progress"}:
        return "pending"
    return "missing" if not status else "unrecognized"


def candidate_domains(live: api.JsonObject) -> CandidateDomains:
    if "domains" not in live:
        return "missing"
    domains = live["domains"]
    if not isinstance(domains, list):
        return "malformed"
    return "nonempty" if domains else "empty"


def inspect_application_candidate(
    adapter: "ApplicationActivationAdapter", record: TestingActivationRecord, operation_key: str
) -> ApplicationCandidateInspection:
    from control_plane.testing_application_activation import activation_deployment_key

    observed = ApplicationCandidateInspection(
        placement="not_created" if not record.target_id else "unavailable",
        stored_deployment_recorded=bool(record.state_recipient.get("application_deployment_id")),
    )
    if not record.target_id:
        return observed
    try:
        live = adapter.live(record)
    except TestingActivationRefusal as error:
        log_handled_error_cause(error, adapter.trace_id)
        return observed.model_copy(update={"placement": "changed"})
    except (click.ClickException, ValueError, OSError, RuntimeError) as error:
        log_handled_error_cause(error, adapter.trace_id)
        return observed
    observed = observed.model_copy(
        update={"placement": "matched", "domains": candidate_domains(live)}
    )
    try:
        host, token = source.read_dokploy_config(control_plane_root=adapter.root)
        rows = api.list_deployments_for_target(
            host=host,
            token=token,
            target_type="application",
            target_id=record.target_id,
            require_complete=True,
        )
    except (click.ClickException, ValueError, OSError, RuntimeError) as error:
        log_handled_error_cause(error, adapter.trace_id)
        rows = None
    if rows is not None:
        observed = observed.model_copy(
            update={"history": "complete", "deployment_count": len(rows)}
        )
        if len(rows) == 1:
            observed = observed.model_copy(
                update={
                    "sole_deployment_id_matches": bool(api.deployment_key(rows[0]))
                    and api.deployment_key(rows[0])
                    == activation_deployment_key(
                        record.state_recipient.get("application_deployment_id")
                    ),
                    "sole_deployment_title_matches": rows[0].get("title")
                    == provider_operation_title(operation_key) + " application",
                    "sole_deployment_status": candidate_deployment_status(rows[0]),
                }
            )
    return inspect_candidate_runtime(adapter, record, live, observed)


def inspect_candidate_runtime(
    adapter: "ApplicationActivationAdapter",
    record: TestingActivationRecord,
    live: api.JsonObject,
    observed: ApplicationCandidateInspection,
) -> ApplicationCandidateInspection:
    if record.application_readiness is None or record.request.application is None:
        return observed
    try:
        resources, _ = preparation_resources(
            adapter.store, record.product, record.request, record.scope
        )
        host, token = source.read_dokploy_config(control_plane_root=adapter.root)
        specification = record.request.application
        try:
            frame = inspect_application_runtime(
                host=host,
                token=token,
                server_id=str(live.get("serverId") or ""),
                application=record.app_name,
                runtime_port=specification.runtime_port,
                network=specification.network_name,
                image=record.image_reference,
                identity=record.application_readiness.runtime_identity,
                values=adapter.application_values(record),
                volume=resources.volumes.get("state", ""),
                volume_binding=resources.binding_sha256,
                mount_path=specification.state.container_path,
                health_path=specification.health_path,
                qualify_backend=False,
            )
        except VerifiedApplicationRuntimeFailure as failure:
            frame = failure.evidence
        status = frame.get("http_status")
        return ApplicationCandidateInspection.model_validate(
            observed.model_dump()
            | {
                "current_runtime_status": "available",
                "current_http_status": status
                if isinstance(status, int) and 100 <= status <= 599
                else None,
                "current_identity_status": frame.get("identity_status"),
                "current_task_failed": frame.get("task_failed")
                if isinstance(frame.get("task_failed"), bool)
                else None,
                "current_observed_at": datetime.now(timezone.utc),
            }
        )
    except (click.ClickException, ValueError, OSError, RuntimeError) as error:
        log_handled_error_cause(error, adapter.trace_id)
        return observed.model_copy(update={"current_runtime_status": "unavailable"})
