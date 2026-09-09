"""Domain-backed ordinary client proposals and signed-browser decisions."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from time import time
from typing import Annotated
from urllib.parse import urlencode

from fastapi import Depends, HTTPException, Path, Request

from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.ordinary_agent_client import (
    ORDINARY_AGENT_ENROLLMENT_PROPOSE_ACTION,
    OrdinaryAgentDisconnectRequest,
    OrdinaryAgentEnrollmentClientRequest,
    OrdinaryAgentOperationClientResponse,
    OrdinaryAgentSessionClientRequest,
)
from control_plane.contracts.ordinary_agent_session_lifecycle import (
    OrdinaryAgentConnectionView,
    OrdinaryAgentSessionOperationView,
)
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.ordinary_agent_authentication import (
    OrdinaryAgentTokenProof,
    parse_ordinary_agent_token,
)
from control_plane.ordinary_agent_enrollment_preparation import (
    prepare_ordinary_agent_enrollment_intent,
)
from control_plane.ordinary_agent_session_approval import (
    approve_existing_ordinary_agent_session,
    approve_ordinary_agent_enrollment,
    cancel_pending_ordinary_agent_operation,
    disconnect_ordinary_agent_principal,
    read_human_ordinary_agent_session_operation,
    revoke_ordinary_agent_session,
)
from control_plane.ordinary_agent_session_lifecycle import OrdinaryAgentSessionAdmissionDenied
from control_plane.service_auth import (
    AuthorizationTarget,
    LaunchplaneIdentity,
    TerminalAgentIdentity,
)
from control_plane.service_human_auth import (
    BROWSER_CSRF_HEADER_NAME,
    HumanSessionManager,
    validate_browser_mutation_request_headers,
)
from control_plane.storage.postgres import PostgresRecordStore

ORDINARY_AGENT_ENROLLMENT_PROPOSALS_ROUTE = "/v1/agent/ordinary-agent-enrollments"
ORDINARY_AGENT_SESSION_PROPOSALS_ROUTE = "/v1/agent/ordinary-agent-session-proposals"
ORDINARY_AGENT_OPERATION_ROUTE = "/v1/ordinary-agent-operations/{principal_id}/{operation_id}"
PrincipalId = Annotated[str, Path(pattern=r"^[a-z][a-z0-9_-]{2,127}$")]
OperationId = Annotated[str, Path(pattern=r"^[a-z0-9][a-z0-9._-]{2,127}$")]


@dataclass(frozen=True, slots=True)
class OrdinaryAgentManagementDependencies:
    common: ReadRouteDependencies
    read_bearer_identity: Callable[..., LaunchplaneIdentity]
    policy_record_reader: Callable[[], object]
    human_session_manager: HumanSessionManager | None


@contextmanager
def _operation_errors() -> Iterator[None]:
    try:
        yield
    except HTTPException:
        raise
    except OrdinaryAgentSessionAdmissionDenied:
        raise HTTPException(403, "This agent operation is unavailable.") from None
    except PermissionError:
        raise HTTPException(403, "This agent operation is not authorized.") from None
    except ValueError:
        raise HTTPException(409, "This agent request conflicts with current state.") from None
    except Exception:
        raise HTTPException(503, "This agent operation is temporarily unavailable.") from None


def _client_response(
    view: OrdinaryAgentSessionOperationView,
) -> OrdinaryAgentOperationClientResponse:
    return OrdinaryAgentOperationClientResponse(
        operation=view,
        review_url="/ui/engineering/privileged-operations?"
        + urlencode({"principal_id": view.principal_id, "operation_id": view.operation_id}),
    )


def read_ordinary_agent_proof(request: Request) -> OrdinaryAgentTokenProof:
    headers = request.headers.getlist("authorization")
    if len(headers) != 1 or not headers[0].startswith("Bearer "):
        raise HTTPException(401, "An ordinary agent credential is required.")
    try:
        return parse_ordinary_agent_token(headers[0][7:])
    except ValueError:
        raise HTTPException(401, "An ordinary agent credential is required.") from None


def register_ordinary_agent_management_routes(
    app: ApiRouteRegistrar, *, dependencies: OrdinaryAgentManagementDependencies
) -> None:
    common = dependencies.common

    def get_record_store() -> PostgresRecordStore:
        store = common.get_record_store()
        if not isinstance(store, PostgresRecordStore):
            raise HTTPException(503, "Agent operations require shared service storage.")
        return store

    def read_terminal_enrollment_requester(
        request: Request,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_bearer_identity)],
    ) -> TerminalAgentIdentity:
        if len(request.headers.getlist("authorization")) != 1 or not isinstance(
            identity, TerminalAgentIdentity
        ):
            raise HTTPException(403, "An authenticated terminal client is required.")
        with _operation_errors():
            policy = LaunchplaneAuthzPolicyRecord.model_validate(
                dependencies.policy_record_reader()
            )
            matches = tuple(
                rule
                for rule in policy.policy.terminal_agents
                if rule.managed_set_id
                and rule.managed_rule_id
                and rule.allows(
                    identity=identity,
                    action=ORDINARY_AGENT_ENROLLMENT_PROPOSE_ACTION,
                    product="launchplane",
                    context="launchplane",
                    target=AuthorizationTarget(scope="global"),
                    schema_version=policy.policy.schema_version,
                )
            )
            if policy.policy.schema_version not in {2, 3} or len(matches) != 1:
                raise PermissionError(
                    "one current managed terminal enrollment capability is required"
                )
        return identity

    def manager_dependency(request: Request) -> HumanSessionManager:
        if request.headers.getlist("authorization"):
            raise HTTPException(403, "Use the signed browser session for this decision.")
        manager = dependencies.human_session_manager
        if manager is None:
            raise HTTPException(503, "Browser agent administration is unavailable.")
        if len(request.headers.getlist("cookie")) != 1:
            raise HTTPException(401, "Sign in to review this agent request.")
        return manager

    def browser_csrf(
        request: Request,
        manager: Annotated[HumanSessionManager, Depends(manager_dependency)],
    ) -> str:
        with _operation_errors():
            return validate_browser_mutation_request_headers(
                expected_origin=manager.public_origin,
                origin_values=tuple(request.headers.getlist("Origin")),
                sec_fetch_site_values=tuple(request.headers.getlist("Sec-Fetch-Site")),
                sec_fetch_mode_values=tuple(request.headers.getlist("Sec-Fetch-Mode")),
                sec_fetch_dest_values=tuple(request.headers.getlist("Sec-Fetch-Dest")),
                csrf_token_values=tuple(request.headers.getlist(BROWSER_CSRF_HEADER_NAME)),
            )

    def propose_enrollment(
        envelope: OrdinaryAgentEnrollmentClientRequest,
        requester: Annotated[TerminalAgentIdentity, Depends(read_terminal_enrollment_requester)],
        store: Annotated[PostgresRecordStore, Depends(get_record_store)],
    ) -> OrdinaryAgentOperationClientResponse:
        with _operation_errors():
            policy = LaunchplaneAuthzPolicyRecord.model_validate(
                dependencies.policy_record_reader()
            )
            intent = prepare_ordinary_agent_enrollment_intent(
                store=store, policy_record=policy, request=envelope, now=int(time())
            )
            store.propose_ordinary_agent_enrollment(intent=intent, requester=requester)
            return _client_response(
                store.read_proposed_ordinary_agent_enrollment(
                    requester=requester,
                    principal_id=intent.principal_id,
                    operation_id=intent.operation_id,
                )
            )

    def read_enrollment(
        principal_id: PrincipalId,
        operation_id: OperationId,
        requester: Annotated[TerminalAgentIdentity, Depends(read_terminal_enrollment_requester)],
        store: Annotated[PostgresRecordStore, Depends(get_record_store)],
    ) -> OrdinaryAgentOperationClientResponse:
        with _operation_errors():
            return _client_response(
                store.read_proposed_ordinary_agent_enrollment(
                    requester=requester, principal_id=principal_id, operation_id=operation_id
                )
            )

    def propose_session(
        envelope: OrdinaryAgentSessionClientRequest,
        proof: Annotated[OrdinaryAgentTokenProof, Depends(read_ordinary_agent_proof)],
        store: Annotated[PostgresRecordStore, Depends(get_record_store)],
    ) -> OrdinaryAgentOperationClientResponse:
        with _operation_errors():
            return _client_response(
                store.propose_ordinary_agent_session(
                    proof=proof,
                    operation_id=envelope.operation_id,
                    attenuation=envelope.attenuation,
                )
            )

    def read_session(
        operation_id: OperationId,
        proof: Annotated[OrdinaryAgentTokenProof, Depends(read_ordinary_agent_proof)],
        store: Annotated[PostgresRecordStore, Depends(get_record_store)],
    ) -> OrdinaryAgentOperationClientResponse:
        with _operation_errors():
            return _client_response(
                store.read_ordinary_agent_session_operation(proof=proof, operation_id=operation_id)
            )

    def cancel_session(
        operation_id: OperationId,
        proof: Annotated[OrdinaryAgentTokenProof, Depends(read_ordinary_agent_proof)],
        store: Annotated[PostgresRecordStore, Depends(get_record_store)],
    ) -> OrdinaryAgentOperationClientResponse:
        with _operation_errors():
            view = store.read_ordinary_agent_session_operation(
                proof=proof, operation_id=operation_id
            )
            if view.session_id is None:
                raise HTTPException(409, "This request has no issued session to cancel.")
            store.cancel_ordinary_agent_session(proof=proof, session_id=view.session_id)
            return _client_response(
                store.read_ordinary_agent_session_operation(proof=proof, operation_id=operation_id)
            )

    def human_read(
        principal_id: PrincipalId,
        operation_id: OperationId,
        request: Request,
        manager: Annotated[HumanSessionManager, Depends(manager_dependency)],
        store: Annotated[PostgresRecordStore, Depends(get_record_store)],
    ) -> OrdinaryAgentOperationClientResponse:
        with _operation_errors():
            return _client_response(
                read_human_ordinary_agent_session_operation(
                    store=store,
                    manager=manager,
                    cookie_header=request.headers.get("cookie", ""),
                    principal_id=principal_id,
                    operation_id=operation_id,
                )
            )

    def human_approve(
        principal_id: PrincipalId,
        operation_id: OperationId,
        request: Request,
        manager: Annotated[HumanSessionManager, Depends(manager_dependency)],
        csrf: Annotated[str, Depends(browser_csrf)],
        store: Annotated[PostgresRecordStore, Depends(get_record_store)],
    ) -> OrdinaryAgentOperationClientResponse:
        if request.headers.get("content-length", "0") != "0" or request.headers.get(
            "transfer-encoding"
        ):
            raise HTTPException(400, "This decision has no request body.")
        with _operation_errors():
            view = read_human_ordinary_agent_session_operation(
                store=store,
                manager=manager,
                cookie_header=request.headers.get("cookie", ""),
                principal_id=principal_id,
                operation_id=operation_id,
            )
            approve = (
                approve_ordinary_agent_enrollment
                if view.kind == "initial"
                else approve_existing_ordinary_agent_session
            )
            approve(
                store=store,
                manager=manager,
                cookie_header=request.headers.get("cookie", ""),
                csrf_token=csrf,
                principal_id=principal_id,
                operation_id=operation_id,
            )
            return human_read(principal_id, operation_id, request, manager, store)

    def human_cancel(
        principal_id: PrincipalId,
        operation_id: OperationId,
        request: Request,
        manager: Annotated[HumanSessionManager, Depends(manager_dependency)],
        csrf: Annotated[str, Depends(browser_csrf)],
        store: Annotated[PostgresRecordStore, Depends(get_record_store)],
    ) -> OrdinaryAgentOperationClientResponse:
        if request.headers.get("content-length", "0") != "0" or request.headers.get(
            "transfer-encoding"
        ):
            raise HTTPException(400, "This decision has no request body.")
        with _operation_errors():
            return _client_response(
                cancel_pending_ordinary_agent_operation(
                    store=store,
                    manager=manager,
                    cookie_header=request.headers.get("cookie", ""),
                    csrf_token=csrf,
                    principal_id=principal_id,
                    operation_id=operation_id,
                )
            )

    def human_revoke_session(
        principal_id: PrincipalId,
        session_id: Annotated[str, Path(min_length=1, max_length=128)],
        request: Request,
        manager: Annotated[HumanSessionManager, Depends(manager_dependency)],
        csrf: Annotated[str, Depends(browser_csrf)],
        store: Annotated[PostgresRecordStore, Depends(get_record_store)],
    ) -> OrdinaryAgentOperationClientResponse:
        if request.headers.get("content-length", "0") != "0" or request.headers.get(
            "transfer-encoding"
        ):
            raise HTTPException(400, "This decision has no request body.")
        with _operation_errors():
            return _client_response(
                revoke_ordinary_agent_session(
                    store=store,
                    manager=manager,
                    cookie_header=request.headers.get("cookie", ""),
                    csrf_token=csrf,
                    principal_id=principal_id,
                    session_id=session_id,
                )
            )

    def human_disconnect(
        principal_id: PrincipalId,
        envelope: OrdinaryAgentDisconnectRequest,
        request: Request,
        manager: Annotated[HumanSessionManager, Depends(manager_dependency)],
        csrf: Annotated[str, Depends(browser_csrf)],
        store: Annotated[PostgresRecordStore, Depends(get_record_store)],
    ) -> OrdinaryAgentConnectionView:
        with _operation_errors():
            return disconnect_ordinary_agent_principal(
                store=store,
                manager=manager,
                cookie_header=request.headers.get("cookie", ""),
                csrf_token=csrf,
                principal_id=principal_id,
                source_event_id=envelope.source_event_id,
            )

    for path, endpoint, method, operation_id in (
        (
            ORDINARY_AGENT_ENROLLMENT_PROPOSALS_ROUTE,
            propose_enrollment,
            "POST",
            "propose_ordinary_agent_enrollment",
        ),
        (
            ORDINARY_AGENT_ENROLLMENT_PROPOSALS_ROUTE + "/{principal_id}/{operation_id}",
            read_enrollment,
            "GET",
            "read_proposed_ordinary_agent_enrollment",
        ),
        (
            ORDINARY_AGENT_SESSION_PROPOSALS_ROUTE,
            propose_session,
            "POST",
            "propose_ordinary_agent_session",
        ),
        (
            ORDINARY_AGENT_SESSION_PROPOSALS_ROUTE + "/{operation_id}",
            read_session,
            "GET",
            "read_ordinary_agent_session_operation",
        ),
        (
            ORDINARY_AGENT_SESSION_PROPOSALS_ROUTE + "/{operation_id}/cancel",
            cancel_session,
            "POST",
            "cancel_ordinary_agent_session",
        ),
        (ORDINARY_AGENT_OPERATION_ROUTE, human_read, "GET", "read_human_ordinary_agent_operation"),
        (
            ORDINARY_AGENT_OPERATION_ROUTE + "/approve",
            human_approve,
            "POST",
            "approve_ordinary_agent_operation",
        ),
        (
            ORDINARY_AGENT_OPERATION_ROUTE + "/cancel",
            human_cancel,
            "POST",
            "cancel_ordinary_agent_operation",
        ),
        (
            "/v1/ordinary-agent-sessions/{principal_id}/{session_id}/revoke",
            human_revoke_session,
            "POST",
            "revoke_ordinary_agent_session",
        ),
    ):
        app.add_api_route(
            path,
            endpoint,
            methods=[method],
            operation_id=operation_id,
            response_model=OrdinaryAgentOperationClientResponse,
            tags=["ordinary-agent"],
        )
    app.add_api_route(
        "/v1/ordinary-agent-connections/{principal_id}/disconnect",
        human_disconnect,
        methods=["POST"],
        operation_id="disconnect_ordinary_agent_principal",
        response_model=OrdinaryAgentConnectionView,
        tags=["ordinary-agent"],
    )
