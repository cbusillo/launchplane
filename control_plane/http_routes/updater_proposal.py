from collections.abc import Callable
from pathlib import Path
from typing import Annotated

from fastapi import Depends

from control_plane.contracts.updater_proposal import (
    UPDATER_PROPOSAL_ROUTE,
    UpdaterProposalRequest,
    UpdaterProposalResponse,
)
from control_plane.http_routes.support import (
    ApiRouteRegistrar,
    HttpErrorFactory,
    run_in_worker_thread,
)
from control_plane.service_auth import (
    GitHubActionsIdentity,
    LaunchplaneAuthzPolicy,
    LaunchplaneIdentity,
)
from control_plane.updater_proposal import (
    UpdaterProposalRefused,
    authorize_updater_proposal,
    open_updater_proposal,
)


def register_updater_proposal_route(
    app: ApiRouteRegistrar,
    *,
    read_write_identity: Callable[..., LaunchplaneIdentity],
    policy_reader: Callable[[], LaunchplaneAuthzPolicy],
    control_plane_root: Path,
    http_error: HttpErrorFactory,
    next_trace_id: Callable[[], str],
) -> None:
    async def create_updater_proposal(
        request: UpdaterProposalRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
    ) -> UpdaterProposalResponse:
        trace_id = next_trace_id()
        try:
            if not isinstance(identity, GitHubActionsIdentity):
                raise UpdaterProposalRefused("Updater proposals require GitHub Actions OIDC.")
            authorize_updater_proposal(identity=identity, policy=policy_reader(), request=request)
            return await run_in_worker_thread(
                lambda: open_updater_proposal(
                    identity=identity,
                    request=request,
                    control_plane_root=control_plane_root,
                )
            )
        except UpdaterProposalRefused as error:
            raise http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=str(error),
            ) from error
        except Exception as error:
            # Provider payloads and credential-resolution errors are private.
            # A ref may already exist: do not retry or patch it behind the caller's back.
            raise http_error(
                status_code=503,
                trace_id=trace_id,
                code="updater_proposal_unavailable",
                message="Updater proposal could not be confirmed; inspect the repository before retrying.",
            ) from error

    app.add_api_route(
        UPDATER_PROPOSAL_ROUTE,
        create_updater_proposal,
        methods=["POST"],
        response_model=UpdaterProposalResponse,
        status_code=201,
    )
