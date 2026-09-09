"""Private ordinary-agent ingress, isolated from legacy credential resolution."""

from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Path, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.ordinary_agent_authentication import (
    OrdinaryAgentClaimSecret,
    receiver_claim_sha256,
)
from control_plane.storage.postgres import PostgresRecordStore

ORDINARY_AGENT_CLAIM_ROUTE = "/v1/agent/ordinary-agent-enrollments/{operation_id}/claim"
_PRIVATE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


class OrdinaryAgentCredentialClaimResponse(BaseModel):
    """Private machine delivery; never include this response in agent context."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    status: Literal["ready"] = "ready"
    credential: str = Field(repr=False, min_length=1, max_length=512)


@dataclass(frozen=True, slots=True)
class OrdinaryAgentRouteDependencies:
    common: ReadRouteDependencies


def read_ordinary_agent_receiver_claim(request: Request) -> OrdinaryAgentClaimSecret:
    """Accept only the receiver capability; no cookie, legacy, or Actions fallback."""
    headers = request.headers.getlist("authorization")
    value = headers[0] if len(headers) == 1 else ""
    if not value.startswith("Bearer ") or len(value) != 50:
        raise HTTPException(401, "Credential delivery is unavailable.", headers=_PRIVATE_HEADERS)
    claim = OrdinaryAgentClaimSecret(value[7:])
    try:
        receiver_claim_sha256(claim)
    except ValueError:
        raise HTTPException(
            401, "Credential delivery is unavailable.", headers=_PRIVATE_HEADERS
        ) from None
    return claim


def register_ordinary_agent_routes(
    app: ApiRouteRegistrar, *, dependencies: OrdinaryAgentRouteDependencies
) -> None:
    common = dependencies.common

    def claim_ordinary_agent_credential(
        request: Request,
        response: Response,
        operation_id: Annotated[str, Path(min_length=1, max_length=256)],
        claim: Annotated[OrdinaryAgentClaimSecret, Depends(read_ordinary_agent_receiver_claim)],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> OrdinaryAgentCredentialClaimResponse:
        response.headers.update(_PRIVATE_HEADERS)
        # This endpoint has no request body or query contract. Never parse or echo
        # a misplaced secret through ordinary Pydantic validation errors.
        if (
            request.query_params
            or request.headers.get("content-length", "0") != "0"
            or request.headers.get("transfer-encoding")
        ):
            raise HTTPException(
                400, "This operation has no request body.", headers=_PRIVATE_HEADERS
            )
        if not isinstance(record_store, PostgresRecordStore):
            raise HTTPException(
                503, "Credential delivery is unavailable.", headers=_PRIVATE_HEADERS
            )
        try:
            token = record_store.claim_ordinary_agent_credential(
                operation_id=operation_id, claim_secret=claim
            )
        except Exception:
            # Keyring/provider exception text and SQL server detail are private.
            # The store retains its sanitized error boundary; HTTP never serializes
            # an exception, request header, claim secret, or decrypted value.
            raise HTTPException(
                503, "Credential delivery is unavailable.", headers=_PRIVATE_HEADERS
            ) from None
        if token is None:
            raise HTTPException(
                401, "Credential delivery is unavailable.", headers=_PRIVATE_HEADERS
            )
        return OrdinaryAgentCredentialClaimResponse(credential=token.value)

    app.add_api_route(
        ORDINARY_AGENT_CLAIM_ROUTE,
        claim_ordinary_agent_credential,
        methods=["POST"],
        response_model=OrdinaryAgentCredentialClaimResponse,
        operation_id="claim_ordinary_agent_credential",
        tags=["ordinary-agent-private"],
        openapi_extra={
            "x-launchplane-response-custody": "private-client-only",
            "parameters": [
                {
                    "name": "Authorization",
                    "in": "header",
                    "required": True,
                    "description": "Private receiver capability, using the Bearer scheme.",
                    "schema": {"type": "string", "format": "password"},
                }
            ],
        },
        responses={401: {"description": "Delivery capability denied or unavailable"}},
    )
