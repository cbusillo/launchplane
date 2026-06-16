import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Literal, Protocol, cast
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from jwt import InvalidTokenError
from pydantic import BaseModel, ConfigDict, Field

from control_plane import product_read_service as control_plane_product_read_service
from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.driver_descriptor import DriverContextView, DriverDescriptor
from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    build_launchplane_idempotency_record_id,
)
from control_plane.contracts.product_environment_read_model import (
    ProductEnvironmentConfigStatus,
)
from control_plane.contracts.protected_artifacts import (
    ProtectedArtifactStore,
    ProtectedArtifactSet,
    build_protected_artifact_set,
)
from control_plane.drivers.registry import build_driver_context_view, list_driver_descriptors
from control_plane.drivers.registry import read_driver_descriptor as read_driver_descriptor_record
from control_plane.service_auth import (
    BearerIdentityConfig,
    GitHubActionsIdentity,
    GitHubHumanIdentity,
    LaunchplaneAuthzPolicy,
    LaunchplaneIdentity,
    LocalAdminIdentity,
    LocalOperatorIdentity,
    TerminalAgentIdentity,
    TokenVerifier,
    bearer_identity_from_token,
)
from control_plane.service_human_auth import HumanSessionManager, LaunchplaneHumanSession
from control_plane.storage.factory import build_shared_record_store
from control_plane.storage.factory import storage_backend_name
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.evidence_ingestion import (
    EvidenceIngestionStore,
    apply_deployment_evidence,
)
from control_plane.workflows.ship import utc_now_timestamp


_BEARER_CHALLENGE_HEADER = {"WWW-Authenticate": 'Bearer realm="Launchplane API"'}
_LAUNCHPLANE_DRIVER_READ_PRODUCT = "launchplane"
_LAUNCHPLANE_DRIVER_READ_CONTEXT = "launchplane"
_DEPLOYMENT_EVIDENCE_ROUTE = "/v1/evidence/deployments"


class LaunchplaneErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str


class LaunchplaneErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = "rejected"
    trace_id: str
    error: LaunchplaneErrorDetail


class HealthResponse(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "status": "ok",
                    "trace_id": "launchplane_req_00000000000000000000000000000000",
                    "storage_backend": "postgres",
                }
            ]
        },
    )

    status: Literal["ok"] = "ok"
    trace_id: str
    storage_backend: str


class ProductEnvironmentConfigStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = "ok"
    trace_id: str
    config_status: ProductEnvironmentConfigStatus


class DriverDescriptorsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    drivers: tuple[DriverDescriptor, ...]


class DriverDescriptorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    driver: DriverDescriptor


class DriverContextViewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    view: DriverContextView


class ProtectedArtifactsResponse(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "status": "ok",
                    "trace_id": "launchplane_req_00000000000000000000000000000000",
                    "protected_artifacts": {
                        "schema_version": 1,
                        "product": "example-product",
                        "context": "",
                        "entries": [],
                        "artifact_ids": ["artifact-example-prod"],
                        "image_references": ["ghcr.io/example-org/example-app@sha256:abc123"],
                        "image_digests": ["sha256:abc123"],
                        "warnings": [],
                    },
                }
            ]
        },
    )

    status: Literal["ok"] = "ok"
    trace_id: str
    protected_artifacts: ProtectedArtifactSet


class DeploymentEvidenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    deployment: DeploymentRecord

    def model_post_init(self, _context: object) -> None:
        if not self.product.strip():
            raise ValueError("deployment evidence requires product")


class AcceptedEvidenceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["accepted"] = "accepted"
    trace_id: str
    records: dict[str, str]
    replayed: bool | None = None
    original_trace_id: str | None = None


class _RecordStoreFactory(Protocol):
    def __call__(self) -> object: ...


class _IdempotencyCapableStore(Protocol):
    def read_idempotency_record(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
    ) -> LaunchplaneIdempotencyRecord | None: ...

    def write_idempotency_record(self, record: LaunchplaneIdempotencyRecord) -> object: ...


def require_protected_artifact_store(record_store: object) -> ProtectedArtifactStore:
    required_methods = (
        "list_artifact_manifests",
        "list_environment_inventory",
        "list_product_profile_records",
        "list_release_tuple_records",
        "list_preview_records",
        "list_preview_generation_records",
        "list_preview_pr_feedback_records",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support protected artifact inventory "
            f"reads: {missing_summary}"
        )
    return cast(ProtectedArtifactStore, record_store)


def require_deployment_evidence_store(record_store: object) -> EvidenceIngestionStore:
    required_methods = (
        "write_deployment_record",
        "write_environment_inventory",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support deployment evidence writes: "
            f"{missing_summary}"
        )
    return cast(EvidenceIngestionStore, record_store)


def require_evidence_ingestion_store(record_store: object) -> EvidenceIngestionStore:
    required_methods = (
        "write_deployment_record",
        "read_deployment_record",
        "read_promotion_record",
        "write_promotion_record",
        "write_environment_inventory",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support evidence ingestion writes: "
            f"{missing_summary}"
        )
    return cast(EvidenceIngestionStore, record_store)


def idempotency_capable_store(record_store: object) -> _IdempotencyCapableStore | None:
    if callable(getattr(record_store, "read_idempotency_record", None)) and callable(
        getattr(record_store, "write_idempotency_record", None)
    ):
        return cast(_IdempotencyCapableStore, record_store)
    return None


def idempotency_scope(identity: LaunchplaneIdentity) -> str:
    if isinstance(identity, GitHubHumanIdentity):
        return "|".join(("github-human", identity.login, str(identity.github_id)))
    if isinstance(identity, LocalOperatorIdentity):
        return "|".join(("local-operator", identity.subject, identity.token_label))
    if isinstance(identity, LocalAdminIdentity):
        return "|".join(("local-admin", identity.subject, identity.token_label))
    if isinstance(identity, TerminalAgentIdentity):
        return "|".join(("terminal-agent", identity.subject, identity.token_label))
    if isinstance(identity, GitHubActionsIdentity):
        workflow_ref = identity.workflow_ref or identity.job_workflow_ref or ""
        return "|".join(
            (
                str(identity.repository).strip(),
                str(workflow_ref).strip(),
                str(identity.subject).strip(),
            )
        )
    raise TypeError(f"Unsupported Launchplane identity type: {type(identity).__name__}")


def request_fingerprint(payload: dict[str, object]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class LaunchplaneAuthzPolicyRuntime:
    def __init__(self, policy: LaunchplaneAuthzPolicy) -> None:
        self._policy = policy

    @property
    def policy(self) -> LaunchplaneAuthzPolicy:
        return self._policy

    def update(self, policy: LaunchplaneAuthzPolicy) -> None:
        self._policy = policy


class ResolvedLaunchplaneAuthzPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy: LaunchplaneAuthzPolicy
    policy_sha256: str
    source: str


def resolve_launchplane_authz_policy(
    *,
    record_store: object,
    bootstrap_policy: LaunchplaneAuthzPolicy,
    policy_source: str,
    now_timestamp: str,
) -> ResolvedLaunchplaneAuthzPolicy:
    list_records = getattr(record_store, "list_authz_policy_records", None)
    if callable(list_records):
        records = list_records(status="active", limit=1)
        if records:
            record = records[0]
            return ResolvedLaunchplaneAuthzPolicy(
                policy=record.policy,
                policy_sha256=record.policy_sha256,
                source="db",
            )

    policy_sha256 = authz_policy_sha256(bootstrap_policy)
    write_record = getattr(record_store, "write_authz_policy_record", None)
    if callable(write_record):
        record = LaunchplaneAuthzPolicyRecord(
            record_id=build_authz_policy_record_id(
                updated_at=now_timestamp,
                policy_sha256=policy_sha256,
            ),
            status="active",
            source=policy_source,
            updated_at=now_timestamp,
            policy_sha256=policy_sha256,
            policy=bootstrap_policy,
        )
        write_record(record)
        return ResolvedLaunchplaneAuthzPolicy(
            policy=record.policy,
            policy_sha256=record.policy_sha256,
            source="bootstrap_seeded_store",
        )

    return ResolvedLaunchplaneAuthzPolicy(
        policy=bootstrap_policy,
        policy_sha256=policy_sha256,
        source="bootstrap",
    )


def create_launchplane_fastapi_app(
    *,
    verifier: TokenVerifier,
    authz_policy: LaunchplaneAuthzPolicy,
    authz_policy_runtime: LaunchplaneAuthzPolicyRuntime | None = None,
    database_url: str | None = None,
    record_store_factory: _RecordStoreFactory | None = None,
    bearer_identity_config: BearerIdentityConfig | None = None,
    human_session_manager: HumanSessionManager | None = None,
) -> FastAPI:
    resolved_authz_policy_runtime = authz_policy_runtime or LaunchplaneAuthzPolicyRuntime(
        authz_policy
    )
    shared_record_store: object | None = (
        None
        if record_store_factory is not None
        else build_shared_record_store(database_url=database_url)
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if isinstance(shared_record_store, PostgresRecordStore):
                shared_record_store.close()

    app = FastAPI(title="Launchplane API", version="0.1.0", lifespan=lifespan)

    def next_trace_id() -> str:
        return f"launchplane_req_{uuid4().hex}"

    def get_record_store() -> object:
        if record_store_factory is not None:
            return record_store_factory()
        if shared_record_store is None:
            raise RuntimeError("Launchplane record store is not initialized.")
        return shared_record_store

    def read_human_session(
        *,
        cookie_header: str,
    ) -> tuple[LaunchplaneHumanSession, bool] | None:
        if human_session_manager is None:
            return None
        session = human_session_manager.read_cookie(cookie_header)
        if session is None:
            return None
        renewed_session = human_session_manager.renew_if_needed(session)
        if renewed_session is None:
            return None
        return renewed_session, renewed_session.expires_at != session.expires_at

    def read_human_session_identity(
        *,
        cookie_header: str,
        request: Request,
        response: Response,
    ) -> GitHubHumanIdentity | None:
        session_result = read_human_session(cookie_header=cookie_header)
        if session_result is None:
            return None
        session, was_renewed = session_result
        if was_renewed and human_session_manager is not None:
            session_cookie_header = human_session_manager.session_cookie_header(session)
            response.headers.append("Set-Cookie", session_cookie_header)
            request.state.launchplane_renewed_session_cookie = session_cookie_header
        return session.identity

    def read_identity(
        request: Request,
        response: Response,
        authorization: Annotated[str, Header(alias="Authorization")] = "",
        cookie: Annotated[str, Header(alias="Cookie")] = "",
    ) -> LaunchplaneIdentity:
        header = authorization.strip()
        if header:
            scheme, _, token = header.partition(" ")
            bearer_token = token.strip()
            if scheme.lower() == "bearer" and bearer_token:
                try:
                    owner_agent_identity = bearer_identity_from_token(
                        token=bearer_token,
                        config=bearer_identity_config or BearerIdentityConfig(),
                    )
                except PermissionError as error:
                    raise _authentication_required_error(str(error)) from error
                if owner_agent_identity is not None:
                    return owner_agent_identity
                try:
                    return verifier.verify(bearer_token)
                except (InvalidTokenError, ValueError) as error:
                    raise _authentication_required_error(str(error)) from error
        human_identity = read_human_session_identity(
            cookie_header=cookie,
            request=request,
            response=response,
        )
        if human_identity is not None:
            return human_identity
        raise _authentication_required_error("Authorization header is required.")

    def read_write_identity(
        authorization: Annotated[str, Header(alias="Authorization")] = "",
    ) -> LaunchplaneIdentity:
        header = authorization.strip()
        if not header:
            raise _authentication_required_error("Authorization header is required.")
        scheme, _, token = header.partition(" ")
        bearer_token = token.strip()
        if scheme.lower() != "bearer" or not bearer_token:
            raise _authentication_required_error("Bearer token is required.")
        try:
            owner_agent_identity = bearer_identity_from_token(
                token=bearer_token,
                config=bearer_identity_config or BearerIdentityConfig(),
            )
        except PermissionError as error:
            raise _authentication_required_error(str(error)) from error
        if isinstance(owner_agent_identity, LocalAdminIdentity | LocalOperatorIdentity):
            return owner_agent_identity
        if owner_agent_identity is not None:
            raise _launchplane_http_error(
                status_code=403,
                trace_id=next_trace_id(),
                code="authorization_denied",
                message=("Terminal agent credentials can only read redacted Launchplane context."),
            )
        try:
            oidc_identity = verifier.verify(bearer_token)
        except (InvalidTokenError, ValueError) as error:
            raise _authentication_required_error(str(error)) from error
        if not isinstance(oidc_identity, GitHubActionsIdentity):
            raise _authentication_required_error("Mutation routes require GitHub Actions OIDC.")
        return oidc_identity

    def read_product_environment_config_status(
        product: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        environment: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> ProductEnvironmentConfigStatusResponse:
        trace_id = next_trace_id()

        def product_action_allowed(
            requested_action: str, requested_product: str, requested_context: str
        ) -> bool:
            return resolved_authz_policy_runtime.policy.allows(
                identity=identity,
                action=requested_action,
                product=requested_product,
                context=requested_context,
            )

        try:
            product_read_store = (
                control_plane_product_read_service.require_product_environment_read_model_store(
                    record_store
                )
            )
            product_read_result = (
                control_plane_product_read_service.build_product_environment_read_service_result(
                    record_store=product_read_store,
                    params={
                        "product": product,
                        "environment": environment,
                        "config_status": "true",
                    },
                    action_allowed=product_action_allowed,
                )
            )
        except control_plane_product_read_service.ProductReadModelStoreCapabilityError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message=str(error),
            ) from error

        if not product_action_allowed(
            "product_environment.read",
            product_read_result.authorization_product,
            product_read_result.authorization_context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=product_read_result.denial_message,
            )

        config_status = ProductEnvironmentConfigStatus.model_validate(
            product_read_result.payload["config_status"]
        )
        return ProductEnvironmentConfigStatusResponse(
            trace_id=trace_id,
            config_status=config_status,
        )

    def read_protected_artifacts(
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        product: Annotated[str, Query()] = "",
        context: Annotated[str, Query()] = "",
    ) -> ProtectedArtifactsResponse:
        trace_id = next_trace_id()
        requested_product = product.strip()
        requested_context = context.strip()
        if not requested_product:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_query",
                message="Protected artifact inventory requires a product query parameter.",
            )
        authz_context = requested_context or "*"
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="artifact_protection.read",
            product=requested_product,
            context=authz_context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read protected artifact inventory.",
            )
        try:
            protected_artifact_store = require_protected_artifact_store(record_store)
            protected_artifacts = build_protected_artifact_set(
                protected_artifact_store,
                product=requested_product,
                context_name=requested_context,
            )
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        return ProtectedArtifactsResponse(
            trace_id=trace_id,
            protected_artifacts=protected_artifacts,
        )

    def ensure_driver_read_allowed(
        *,
        identity: LaunchplaneIdentity,
        trace_id: str,
        context: str = _LAUNCHPLANE_DRIVER_READ_CONTEXT,
    ) -> None:
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="driver.read",
            product=_LAUNCHPLANE_DRIVER_READ_PRODUCT,
            context=context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read driver metadata for the requested context.",
            )

    def read_driver_descriptors(
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
    ) -> DriverDescriptorsResponse:
        trace_id = next_trace_id()
        ensure_driver_read_allowed(identity=identity, trace_id=trace_id)
        return DriverDescriptorsResponse(
            trace_id=trace_id,
            drivers=list_driver_descriptors(),
        )

    def read_driver_descriptor(
        driver_id: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
    ) -> DriverDescriptorResponse:
        trace_id = next_trace_id()
        ensure_driver_read_allowed(identity=identity, trace_id=trace_id)
        try:
            descriptor = read_driver_descriptor_record(driver_id)
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        return DriverDescriptorResponse(trace_id=trace_id, driver=descriptor)

    def read_driver_context_view(
        context: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> DriverContextViewResponse:
        trace_id = next_trace_id()
        ensure_driver_read_allowed(identity=identity, trace_id=trace_id, context=context)
        view = build_driver_context_view(
            record_store=record_store,
            context_name=context,
        )
        return DriverContextViewResponse(trace_id=trace_id, view=view)

    def read_driver_instance_view(
        context: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        instance: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> DriverContextViewResponse:
        trace_id = next_trace_id()
        ensure_driver_read_allowed(identity=identity, trace_id=trace_id, context=context)
        view = build_driver_context_view(
            record_store=record_store,
            context_name=context,
            instance_name=instance,
        )
        return DriverContextViewResponse(trace_id=trace_id, view=view)

    def accepted_evidence_response(
        *,
        trace_id: str,
        records: dict[str, str],
        replayed: bool = False,
        original_trace_id: str = "",
    ) -> AcceptedEvidenceResponse:
        return AcceptedEvidenceResponse(
            trace_id=trace_id,
            records=records,
            replayed=True if replayed else None,
            original_trace_id=original_trace_id or None,
        )

    def replay_idempotent_response(
        *, trace_id: str, stored_record: LaunchplaneIdempotencyRecord
    ) -> AcceptedEvidenceResponse:
        stored_records = {
            str(key): str(value)
            for key, value in dict(stored_record.response_payload.get("records") or {}).items()
        }
        return accepted_evidence_response(
            trace_id=trace_id,
            records=stored_records,
            replayed=True,
            original_trace_id=stored_record.response_trace_id,
        )

    async def write_deployment_evidence(
        request: Request,
        deployment_request: DeploymentEvidenceRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(read_write_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = next_trace_id()
        if not resolved_authz_policy_runtime.policy.allows(
            identity=identity,
            action="deployment.write",
            product=deployment_request.product,
            context=deployment_request.deployment.context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot write deployment evidence for the requested product/context."
                ),
            )

        idempotency_store = idempotency_capable_store(record_store)
        normalized_idempotency_key = idempotency_key.strip()
        normalized_scope = idempotency_scope(identity)
        raw_payload = await request.json()
        payload_fingerprint = request_fingerprint(cast(dict[str, object], raw_payload))
        if idempotency_store is not None and normalized_idempotency_key:
            stored_record = idempotency_store.read_idempotency_record(
                scope=normalized_scope,
                route_path=_DEPLOYMENT_EVIDENCE_ROUTE,
                idempotency_key=normalized_idempotency_key,
            )
            if stored_record is not None:
                if stored_record.request_fingerprint != payload_fingerprint:
                    raise _launchplane_http_error(
                        status_code=409,
                        trace_id=trace_id,
                        code="idempotency_key_reused",
                        message=(
                            "Idempotency-Key was already used for a different "
                            "Launchplane request payload on this route."
                        ),
                    )
                return replay_idempotent_response(
                    trace_id=trace_id,
                    stored_record=stored_record,
                )

        try:
            evidence_store = require_deployment_evidence_store(record_store)
        except TypeError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error

        records = {
            str(key): str(value)
            for key, value in apply_deployment_evidence(
                record_store=evidence_store,
                deployment_record=deployment_request.deployment,
            ).items()
        }
        response = accepted_evidence_response(trace_id=trace_id, records=records)
        if idempotency_store is not None and normalized_idempotency_key:
            idempotency_store.write_idempotency_record(
                LaunchplaneIdempotencyRecord(
                    record_id=build_launchplane_idempotency_record_id(
                        response_trace_id=trace_id,
                    ),
                    scope=normalized_scope,
                    route_path=_DEPLOYMENT_EVIDENCE_ROUTE,
                    idempotency_key=normalized_idempotency_key,
                    request_fingerprint=payload_fingerprint,
                    response_status_code=202,
                    response_trace_id=trace_id,
                    recorded_at=utc_now_timestamp(),
                    response_payload=response.model_dump(mode="json", exclude_none=True),
                )
            )
        return response

    def read_health(record_store: Annotated[object, Depends(get_record_store)]) -> HealthResponse:
        return HealthResponse(
            trace_id=next_trace_id(),
            storage_backend=storage_backend_name(record_store),
        )

    def preserve_renewed_session_cookie(request: Request, response: JSONResponse) -> None:
        renewed_session_cookie = getattr(
            request.state,
            "launchplane_renewed_session_cookie",
            "",
        )
        if renewed_session_cookie:
            response.headers.append("Set-Cookie", str(renewed_session_cookie))

    app.add_api_route(
        "/v1/health",
        read_health,
        methods=["GET"],
        response_model=HealthResponse,
        operation_id="read_launchplane_health",
        summary="Read Launchplane service health",
    )

    app.add_api_route(
        "/v1/artifacts/protected",
        read_protected_artifacts,
        methods=["GET"],
        response_model=ProtectedArtifactsResponse,
        operation_id="read_protected_artifacts",
        summary="Read protected artifact inventory",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/drivers",
        read_driver_descriptors,
        methods=["GET"],
        response_model=DriverDescriptorsResponse,
        operation_id="read_driver_descriptors",
        summary="Read Launchplane driver descriptors",
        responses={
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/drivers/{driver_id}",
        read_driver_descriptor,
        methods=["GET"],
        response_model=DriverDescriptorResponse,
        operation_id="read_driver_descriptor",
        summary="Read one Launchplane driver descriptor",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/contexts/{context}/driver-view",
        read_driver_context_view,
        methods=["GET"],
        response_model=DriverContextViewResponse,
        operation_id="read_driver_context_view",
        summary="Read Launchplane driver view for a context",
        responses={
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        "/v1/contexts/{context}/instances/{instance}/driver-view",
        read_driver_instance_view,
        methods=["GET"],
        response_model=DriverContextViewResponse,
        operation_id="read_driver_instance_view",
        summary="Read Launchplane driver view for one context instance",
        responses={
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
        },
    )

    app.add_api_route(
        _DEPLOYMENT_EVIDENCE_ROUTE,
        write_deployment_evidence,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="write_deployment_evidence",
        summary="Write deployment evidence",
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            409: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )

    def launchplane_http_exception_handler(request: Request, error: Exception) -> JSONResponse:
        if not isinstance(error, HTTPException):
            raise error
        http_error = error
        trace_id = next_trace_id()
        code = "authentication_required" if http_error.status_code == 401 else "http_error"
        if isinstance(http_error.detail, dict):
            detail = http_error.detail
            trace_id = str(detail.get("trace_id", trace_id))
            code = str(detail.get("code", code))
            message = str(detail.get("message", "Launchplane request failed."))
        else:
            message = str(http_error.detail)
        payload = LaunchplaneErrorResponse(
            trace_id=trace_id,
            error=LaunchplaneErrorDetail(code=code, message=message),
        )
        response = JSONResponse(
            status_code=http_error.status_code,
            content=payload.model_dump(mode="json"),
            headers=http_error.headers,
        )
        preserve_renewed_session_cookie(request, response)
        return response

    def launchplane_request_validation_exception_handler(
        request: Request, error: Exception
    ) -> JSONResponse:
        if not isinstance(error, RequestValidationError):
            raise error
        payload = LaunchplaneErrorResponse(
            trace_id=next_trace_id(),
            error=LaunchplaneErrorDetail(
                code="invalid_request",
                message="Launchplane request validation failed.",
            ),
        )
        response = JSONResponse(
            status_code=400,
            content=payload.model_dump(mode="json"),
        )
        preserve_renewed_session_cookie(request, response)
        return response

    app.add_api_route(
        "/v1/products/{product}/environments/{environment}/config-status",
        read_product_environment_config_status,
        methods=["GET"],
        response_model=ProductEnvironmentConfigStatusResponse,
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )
    app.add_exception_handler(HTTPException, launchplane_http_exception_handler)
    app.add_exception_handler(
        RequestValidationError,
        launchplane_request_validation_exception_handler,
    )

    return app


def _launchplane_http_error(
    *, status_code: int, trace_id: str, code: str, message: str
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"trace_id": trace_id, "code": code, "message": message},
    )


def _authentication_required_error(message: str) -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={"code": "authentication_required", "message": message},
        headers=_BEARER_CHALLENGE_HEADER,
    )
