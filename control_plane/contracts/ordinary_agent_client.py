"""Compiled, bounded client requests; approval provenance is service-owned."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Literal, TypeAlias

from pydantic import Discriminator, Field, Tag, TypeAdapter, field_validator, model_validator

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.ordinary_agent import (
    OrdinaryAgentPullRequest,
    OrdinaryAgentTarget,
    StrictFrozenModel,
)
from control_plane.contracts.ordinary_agent_lifecycle import OrdinaryAgentDeliveryBinding
from control_plane.contracts.ordinary_agent_session_lifecycle import (
    OrdinaryAgentSessionAttenuation,
    OrdinaryAgentSessionOperationView,
)

if TYPE_CHECKING:
    from control_plane.contracts.ordinary_agent_session_lifecycle import (
        OrdinaryAgentGuardedDeliveryFiniteRequestV2,
        OrdinaryAgentQualificationFiniteRequestV2,
    )


class OrdinaryAgentQualificationFiniteClientRequest(StrictFrozenModel):
    """Caller intent for a finite qualification request.

    The authenticated principal, target, request identity, timing and lifecycle
    fields are deliberately absent.  Those values are reconstructed by the
    admission transaction from the credential and locked server records.
    """

    schema_version: Literal[2] = 2
    purpose: Literal["qualification"] = "qualification"
    idempotency_key: str = Field(min_length=1, max_length=256)
    session_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    lease_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")


class OrdinaryAgentGuardedDeliveryFiniteClientRequest(StrictFrozenModel):
    """Caller intent for a finite guarded delivery request."""

    schema_version: Literal[2] = 2
    purpose: Literal["guarded_delivery"] = "guarded_delivery"
    idempotency_key: str = Field(min_length=1, max_length=256)
    session_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    lease_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    base_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    pull_requests: tuple[OrdinaryAgentPullRequest, ...] = Field(min_length=1)
    permitted_stack_edit_pull_requests: tuple[int, ...]
    refresh_allowance: int = Field(ge=0, le=2**63 - 1)

    @field_validator("pull_requests", "permitted_stack_edit_pull_requests", mode="before")
    @classmethod
    def read_json_tuples(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_scope(self) -> "OrdinaryAgentGuardedDeliveryFiniteClientRequest":
        numbers = tuple(item.number for item in self.pull_requests)
        if len(set(numbers)) != len(numbers):
            raise ValueError("request PRs must be unique")
        if len(set(self.permitted_stack_edit_pull_requests)) != len(
            self.permitted_stack_edit_pull_requests
        ) or not set(self.permitted_stack_edit_pull_requests).issubset(numbers):
            raise ValueError("stack edit scope must be a unique subset of request PRs")
        return self


OrdinaryAgentFiniteClientRequest: TypeAlias = Annotated[
    Annotated[OrdinaryAgentQualificationFiniteClientRequest, Tag("qualification")]
    | Annotated[OrdinaryAgentGuardedDeliveryFiniteClientRequest, Tag("guarded_delivery")],
    Discriminator("purpose"),
]

_ORDINARY_AGENT_FINITE_CLIENT_REQUEST_ADAPTER: TypeAdapter[OrdinaryAgentFiniteClientRequest] = (
    TypeAdapter(OrdinaryAgentFiniteClientRequest)
)


def parse_ordinary_agent_finite_client_request(value: object) -> OrdinaryAgentFiniteClientRequest:
    """Parse only the two public finite v2 request shapes."""

    return _ORDINARY_AGENT_FINITE_CLIENT_REQUEST_ADAPTER.validate_python(value)


def ordinary_agent_finite_client_intent_payload(
    request: OrdinaryAgentFiniteClientRequest,
) -> dict[str, object]:
    """Return the stable identity of caller intent, excluding server fields."""

    return {
        "domain": "ordinary-agent-finite-client-intent-v2",
        **request.model_dump(mode="json"),
    }


def ordinary_agent_finite_client_intent_sha256(
    request: OrdinaryAgentFiniteClientRequest,
) -> str:
    return canonical_json_sha256(ordinary_agent_finite_client_intent_payload(request))


def ordinary_agent_finite_request_id(*, principal_id: str, idempotency_key: str) -> str:
    """Derive the request identity from authenticated principal and retry key."""

    digest = canonical_json_sha256(
        {
            "domain": "ordinary-agent-finite-request-id-v2",
            "principal_id": principal_id,
            "idempotency_key": idempotency_key,
        }
    )
    return f"ordinary-request-{digest[:32]}"


@dataclass(frozen=True, slots=True)
class OrdinaryAgentFiniteRequestServerFields:
    """Locked, server-derived values used to build a complete persisted request."""

    principal_id: str
    target: OrdinaryAgentTarget
    admitted_at: int
    lease_expires_at: int
    continuation_expires_at: int | None
    binding_revision: int = 1
    request_lifetime_seconds: int = 300
    refresh_allowance_ceiling: int = 0


def ordinary_agent_finite_request_deadlines(
    *,
    admitted_at: int,
    lease_expires_at: int,
    continuation_expires_at: int | None,
    request_lifetime_seconds: int = 300,
) -> tuple[int, int | None]:
    """Derive finite deadlines from one transaction timestamp and locked rows."""

    if request_lifetime_seconds < 1:
        raise ValueError("request lifetime must be positive")
    if lease_expires_at <= admitted_at:
        raise ValueError("lease must outlive admission")
    if continuation_expires_at is not None and continuation_expires_at <= admitted_at:
        raise ValueError("continuation must outlive admission")
    expires_at = min(
        admitted_at + request_lifetime_seconds,
        lease_expires_at,
        continuation_expires_at or 2**63 - 1,
    )
    if expires_at <= admitted_at:
        raise ValueError("finite request has no remaining lifetime")
    return expires_at, continuation_expires_at


def build_ordinary_agent_finite_request_from_client(
    request: OrdinaryAgentFiniteClientRequest,
    *,
    server: OrdinaryAgentFiniteRequestServerFields,
) -> "OrdinaryAgentQualificationFiniteRequestV2 | OrdinaryAgentGuardedDeliveryFiniteRequestV2":
    """Construct the complete immutable persisted variant from locked evidence.

    The persisted models are imported lazily because they intentionally import
    the existing session lifecycle views from this client contract module.
    """

    from control_plane.contracts.ordinary_agent_session_lifecycle import (
        OrdinaryAgentGuardedDeliveryFiniteRequestV2,
        OrdinaryAgentQualificationFiniteRequestV2,
    )

    expires_at, continuation_expires_at = ordinary_agent_finite_request_deadlines(
        admitted_at=server.admitted_at,
        lease_expires_at=server.lease_expires_at,
        continuation_expires_at=server.continuation_expires_at,
        request_lifetime_seconds=server.request_lifetime_seconds,
    )
    common = {
        "schema_version": 2,
        "idempotency_key": request.idempotency_key,
        "request_id": ordinary_agent_finite_request_id(
            principal_id=server.principal_id,
            idempotency_key=request.idempotency_key,
        ),
        "principal_id": server.principal_id,
        "session_id": request.session_id,
        "lease_id": request.lease_id,
        "target": server.target,
        "binding_revision": server.binding_revision,
        "admitted_at": server.admitted_at,
        "expires_at": expires_at,
        "continuation_expires_at": continuation_expires_at,
        "status": "waiting",
        "cancellation_requested_at": None,
        "execution_record_ids": (),
    }
    if isinstance(request, OrdinaryAgentQualificationFiniteClientRequest):
        return OrdinaryAgentQualificationFiniteRequestV2.model_validate(
            {**common, "purpose": "qualification"}
        )
    if isinstance(request, OrdinaryAgentGuardedDeliveryFiniteClientRequest):
        if request.refresh_allowance > server.refresh_allowance_ceiling:
            raise ValueError("refresh allowance exceeds server ceiling")
        return OrdinaryAgentGuardedDeliveryFiniteRequestV2.model_validate(
            {
                **common,
                "purpose": "guarded_delivery",
                "base_sha": request.base_sha,
                "pull_requests": request.pull_requests,
                "permitted_stack_edit_pull_requests": request.permitted_stack_edit_pull_requests,
                "refresh_allowance_total": request.refresh_allowance,
                "refresh_used": 0,
            }
        )
    raise TypeError(f"unsupported ordinary finite client request: {type(request)!r}")


ORDINARY_AGENT_ENROLLMENT_PROPOSE_ACTION = "ordinary_agent_enrollment.propose"


class OrdinaryAgentEnrollmentClientRequest(StrictFrozenModel):
    descriptor_id: Literal["ordinary-agent-enrollment"] = "ordinary-agent-enrollment"
    operation_id: str = Field(
        pattern=r"^[a-z0-9][a-z0-9._-]{2,127}$",
        description=(
            "Client retry key scoped to the requested principal. Launchplane returns its "
            "canonical operation ID for review, status and private delivery. Repeating the "
            "exact proposal recovers that operation without preparing another credential."
        ),
    )
    action: Literal["enroll", "rotate_credential"]
    principal_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    target: OrdinaryAgentTarget
    github_app_id: int = Field(gt=0, le=2**63 - 1)
    secret_binding_id: str = Field(min_length=1, max_length=256)
    credential_valid_from: int = Field(ge=0, le=2**63 - 1)
    credential_expires_at: int = Field(ge=1, le=2**63 - 1)
    delivery: OrdinaryAgentDeliveryBinding
    session_attenuation: OrdinaryAgentSessionAttenuation | None = None

    @model_validator(mode="after")
    def validate_lifetimes(self) -> "OrdinaryAgentEnrollmentClientRequest":
        if not (
            self.credential_valid_from < self.delivery.expires_at <= self.credential_expires_at
        ):
            raise ValueError("delivery must fit inside the requested credential lifetime")
        if (
            self.session_attenuation is not None
            and self.session_attenuation.session_expires_at > self.credential_expires_at
        ):
            raise ValueError("requested session cannot outlive the credential")
        return self


class OrdinaryAgentSessionClientRequest(StrictFrozenModel):
    descriptor_id: Literal["ordinary-agent-session"] = "ordinary-agent-session"
    operation_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,127}$")
    attenuation: OrdinaryAgentSessionAttenuation


class OrdinaryAgentOperationClientResponse(StrictFrozenModel):
    schema_version: Literal[1] = 1
    operation: OrdinaryAgentSessionOperationView
    review_url: str


class OrdinaryAgentDisconnectRequest(StrictFrozenModel):
    source_event_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,127}$")
