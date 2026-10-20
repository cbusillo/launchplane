"""Service-owned review-label withdrawal; never a Client decision endpoint."""

from pathlib import Path
from typing import Annotated, NoReturn

import click
from fastapi import Depends, Header

from control_plane.http_routes.mutation_support import (
    idempotency_scope,
    AcceptedEvidenceResponse,
    accepted_evidence_response,
)
from control_plane.http_routes.support import (
    ApiRouteRegistrar,
    ReadRouteDependencies,
    LAUNCHPLANE_SERVICE_CONTEXT,
    run_in_worker_thread,
)
from control_plane.product_review_withdrawal import (
    WITHDRAWAL_ACTION,
    WITHDRAWAL_ROUTE,
    ReviewWithdrawalRequest,
    ReviewWithdrawalService,
    ReviewWithdrawalRefused,
    ReviewWithdrawalUnsettled,
)
from control_plane.product_review_withdrawal_github import GitHubReviewWithdrawalSource
from control_plane.service_auth import (
    LaunchplaneIdentity,
    GitHubHumanIdentity,
    LocalAdminIdentity,
    director_agent_identity,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.launchplane_github_delivery import resolve_delivery_github_app_id
from control_plane.workflows.launchplane import launchplane_github_token, github_api_request


def register_product_review_withdrawal_route(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
    control_plane_root: Path,
) -> None:
    async def withdraw_product_review(
        body: ReviewWithdrawalRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace = dependencies.next_trace_id()

        def refuse(status: int, code: str, message: str) -> NoReturn:
            raise dependencies.http_error(
                status_code=status, trace_id=trace, code=code, message=message
            )

        trusted = (
            director_agent_identity(identity)
            or isinstance(identity, LocalAdminIdentity)
            or (isinstance(identity, GitHubHumanIdentity) and identity.role == "admin")
        )
        action = WITHDRAWAL_ACTION if body.mode == "apply" else "product_profile.read"
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
                "Withdrawal requires the Director's authenticated agent or an admin and a recorded Director decision.",
            )
        if not isinstance(record_store, PostgresRecordStore):
            refuse(503, "database_storage_required", "Withdrawal requires shared database storage.")
        store = record_store

        def execute() -> dict[str, object]:
            profile = store.read_product_profile_record(body.product)
            if profile.repository.casefold() != body.repository.casefold():
                raise ReviewWithdrawalRefused("The product does not own this repository.")
            with launchplane_github_token(
                control_plane_root=control_plane_root,
                context_name=profile.preview.context,
                repository=profile.repository,
                purpose="pull_request_feedback",
            ) as token:
                if not token:
                    raise ReviewWithdrawalRefused(
                        "The existing preview feedback credential is unavailable."
                    )
                source = GitHubReviewWithdrawalSource(
                    profile.repository,
                    body.pull_request,
                    token,
                    github_api_request,
                    app_id=resolve_delivery_github_app_id(control_plane_root=control_plane_root),
                )
                return ReviewWithdrawalService(
                    store, source, profile, actor=idempotency_scope(identity)
                ).run(body, key=idempotency_key, trace=trace)

        try:
            result = await run_in_worker_thread(execute)
        except FileNotFoundError:
            refuse(404, "not_found", "The product profile was not found.")
        except ReviewWithdrawalUnsettled as error:
            refuse(409, "product_review_withdrawal_unsettled", str(error))
        except ReviewWithdrawalRefused as error:
            refuse(409, "product_review_withdrawal_refused", str(error))
        except (ValueError, click.ClickException, OSError):
            refuse(
                503,
                "product_review_withdrawal_unavailable",
                "Source-control evidence is unavailable. Preserve the original operation key and read back before retrying.",
            )
        return accepted_evidence_response(trace_id=trace, records={}, result=result)

    app.add_api_route(
        WITHDRAWAL_ROUTE,
        withdraw_product_review,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="withdraw_product_review",
        summary="Withdraw one pending Client review request on a recorded Director decision",
        responses={
            code: {"model": dependencies.error_response_model} for code in (401, 403, 404, 409, 503)
        },
    )
