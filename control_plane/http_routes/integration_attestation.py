from pathlib import Path
from typing import Annotated, NoReturn

import click
from fastapi import Depends

from control_plane.contracts.integration_attestation import (
    INTEGRATION_ATTESTATION_ACTION,
    INTEGRATION_ATTESTATION_ROUTE,
    IntegrationAttestationRequest,
    IntegrationAttestationResponse,
)
from control_plane.http_routes.mutation_support import idempotency_scope
from control_plane.http_routes.support import (
    ApiRouteRegistrar,
    ReadRouteDependencies,
    LAUNCHPLANE_SERVICE_CONTEXT,
    run_in_worker_thread,
)
from control_plane.integration_attestation import (
    integration_attestation_store,
    plan_integration_attestation,
    record_integration_attestation,
)
from control_plane.service_auth import (
    LaunchplaneIdentity,
    LocalAdminIdentity,
    GitHubHumanIdentity,
    director_agent_identity,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.launchplane import github_api_request, launchplane_github_token


def register_integration_attestation_route(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
    control_plane_root: Path,
) -> None:
    async def integration_attestation(
        body: IntegrationAttestationRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> IntegrationAttestationResponse:
        trace = dependencies.next_trace_id()

        def refuse(status: int, code: str, message: str) -> NoReturn:
            raise dependencies.http_error(
                status_code=status, trace_id=trace, code=code, message=message
            )

        trusted = (
            director_agent_identity(identity)
            or isinstance(identity, LocalAdminIdentity)
            or isinstance(identity, GitHubHumanIdentity)
            and identity.role == "admin"
        )
        action = INTEGRATION_ATTESTATION_ACTION if body.mode == "apply" else "product_profile.read"
        if (body.mode == "apply" and not trusted) or not dependencies.authorization_allows(
            identity=identity,
            action=action,
            product=body.product,
            context=LAUNCHPLANE_SERVICE_CONTEXT,
            inherit_agent_read_role=True,
        ):
            refuse(
                403,
                "authorization_denied",
                "Integration attestations require an admin or the Director's authenticated agent.",
            )
        if not isinstance(record_store, PostgresRecordStore):
            refuse(503, "database_storage_required", "Shared database storage is required.")
        store = record_store

        def execute() -> IntegrationAttestationResponse:
            profile = store.read_product_profile_record(body.product)
            if profile.repository != body.repository:
                raise ValueError("The product does not own this repository.")
            attestation_store = integration_attestation_store(store)
            saved = next(
                (
                    record
                    for record in attestation_store.list_integration_attestation_records(
                        repository=body.repository, pull_request_number=body.pull_request_number
                    )
                    if record.evidence.product == body.product
                    and record.evidence.source_decision_id == body.source_decision_id
                    and record.evidence.merge_sha == body.merge_sha
                    and record.evidence.digest() == body.reviewed_evidence_sha256
                ),
                None,
            )
            if body.mode == "readback" or saved is not None:
                if saved is None:
                    raise FileNotFoundError("The exact attestation was not found.")
                if body.mode == "apply" and (
                    saved.review != body.review or saved.statement != body.statement
                ):
                    raise ValueError(
                        "Integration attestation is immutable; read back the original."
                    )
                return IntegrationAttestationResponse(
                    trace_id=trace,
                    evidence=saved.evidence,
                    evidence_sha256=saved.evidence.digest(),
                    record=saved,
                )
            with launchplane_github_token(
                control_plane_root=control_plane_root,
                context_name=profile.preview.context,
                repository=profile.repository,
                purpose="pull_request_feedback",
            ) as token:
                if not token:
                    raise OSError("Existing source-control read credential is unavailable.")
                evidence = plan_integration_attestation(
                    request=body,
                    profile=profile,
                    store=store,
                    read=lambda path: github_api_request(path=path, token=token),
                )
            record = (
                record_integration_attestation(
                    request=body,
                    evidence=evidence,
                    store=attestation_store,
                    actor=idempotency_scope(identity),
                    profile=store.read_product_profile_record(body.product),
                )
                if body.mode == "apply"
                else None
            )
            return IntegrationAttestationResponse(
                trace_id=trace,
                evidence=evidence,
                evidence_sha256=evidence.digest(),
                record=record,
            )

        try:
            return await run_in_worker_thread(execute)
        except FileNotFoundError:
            refuse(404, "not_found", "The product or exact attestation was not found.")
        except (ValueError, TypeError) as error:
            refuse(409, "integration_attestation_refused", str(error))
        except (OSError, click.ClickException):
            refuse(
                503,
                "integration_attestation_unavailable",
                "Source evidence is unavailable. Read back the exact evidence before retrying an apply.",
            )

    app.add_api_route(
        INTEGRATION_ATTESTATION_ROUTE,
        integration_attestation,
        methods=["POST"],
        response_model=IntegrationAttestationResponse,
        operation_id="integration_attestation",
        summary="Record reviewed engineering evidence for one exact integration, without a Client decision",
        responses={
            code: {"model": dependencies.error_response_model} for code in (401, 403, 404, 409, 503)
        },
    )
