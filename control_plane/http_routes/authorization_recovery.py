import base64
import binascii
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Literal, TypeVar, cast

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from control_plane.authorization_recovery import (
    AUTHORIZATION_RECOVERY_SIGNATURE_MAX_BYTES,
    AuthorizationRecoveryAudit,
    AuthorizationRecoveryChallenge,
    AuthorizationRecoveryKey,
    AuthorizationRecoveryService,
    RecoveryOperation,
)
from control_plane.contracts.outbox_delivery import OutboxDeliveryRecord
from control_plane.http_routes.support import (
    ApiRouteRegistrar,
    LAUNCHPLANE_SERVICE_CONTEXT,
    ReadRouteDependencies,
)
from control_plane.service_auth import (
    AuthorizationTarget,
    GitHubHumanIdentity,
    LaunchplaneIdentity,
)


AUTHORIZATION_RECOVERY_BROWSER_STATUS_ROUTE = "/v1/authorization-recovery/status"
AUTHORIZATION_RECOVERY_BROWSER_ENROLL_ROUTE = "/v1/authorization-recovery/keys/enroll"
AUTHORIZATION_RECOVERY_BROWSER_PROOF_ROUTE = "/v1/authorization-recovery/keys/{key_id}/proof"
AUTHORIZATION_RECOVERY_BROWSER_VERIFY_ROUTE = "/v1/authorization-recovery/keys/{key_id}/verify"
AUTHORIZATION_RECOVERY_BROWSER_REVOKE_ROUTE = "/v1/authorization-recovery/keys/{key_id}/revoke"
AUTHORIZATION_RECOVERY_PUBLIC_PREPARE_ROUTE = "/v1/authorization-recovery/public/prepare"
AUTHORIZATION_RECOVERY_PUBLIC_STATUS_ROUTE = "/v1/authorization-recovery/public/challenges/{challenge_id}"
AUTHORIZATION_RECOVERY_PUBLIC_APPLY_ROUTE = "/v1/authorization-recovery/public/apply"

_MAX_BODY_BYTES = 32 * 1024
_MAX_RECENT_EVIDENCE = 20
_TModel = TypeVar("_TModel", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class AuthorizationRecoveryRouteDependencies:
    common: ReadRouteDependencies
    read_github_human_identity: Callable[..., GitHubHumanIdentity]
    read_github_human_mutation_identity: Callable[..., GitHubHumanIdentity]
    reject_public_credentials: Callable[..., None]


class AuthorizationRecoveryKeyEnrollEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key_id: str = Field(min_length=1, max_length=160)
    custody_slot: str = Field(min_length=1, max_length=120)
    public_key: str = Field(min_length=1, max_length=8192)


class AuthorizationRecoverySignatureEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signature: str = Field(min_length=1, max_length=24 * 1024)


class AuthorizationRecoveryPrepareEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: RecoveryOperation
    intended_github_id: int = Field(ge=1)
    signing_key_id: str = Field(min_length=1, max_length=160)
    compromised_key_id: str = Field(default="", max_length=160)
    replacement_key_id: str = Field(default="", max_length=160)


class AuthorizationRecoveryPublicApplyEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    challenge_id: str = Field(min_length=1, max_length=160)
    signing_key_id: str = Field(min_length=1, max_length=160)
    signature: str = Field(min_length=1, max_length=24 * 1024)


class AuthorizationRecoveryKeyView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key_id: str
    custody_slot: str
    fingerprint_sha256: str
    key_type: str
    status: Literal["pending", "active", "revoked"]
    enrolled_at: str
    activated_at: str
    revoked_at: str


class AuthorizationRecoveryAuditView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    audit_id: str
    event: str
    status: Literal["accepted", "rejected", "completed"]
    recorded_at: str
    challenge_id: str
    operation: str
    key_id: str
    key_fingerprint_sha256: str
    reason_code: str


class AuthorizationRecoveryAlertView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    delivery_id: str
    state: str
    action: str
    created_at: str
    updated_at: str
    challenge_id: str


class AuthorizationRecoveryBrowserStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ok"] = "ok"
    trace_id: str
    readiness: dict[str, object]
    audits: tuple[AuthorizationRecoveryAuditView, ...]
    alerts: tuple[AuthorizationRecoveryAlertView, ...]


class AuthorizationRecoveryKeyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ok"] = "ok"
    trace_id: str
    key: AuthorizationRecoveryKeyView


class AuthorizationRecoveryProofResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ok"] = "ok"
    trace_id: str
    key_id: str
    signing_input_base64: str
    signing_input_text: str


class AuthorizationRecoveryPublicChallengeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["prepared", "expired", "consumed"]
    trace_id: str
    challenge_id: str
    operation: RecoveryOperation
    signing_key_id: str
    signing_key_fingerprint_sha256: str
    expires_at: str
    signing_input_base64: str
    signing_input_text: str


class AuthorizationRecoveryApplyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["applied", "adopted"]
    trace_id: str
    challenge_id: str


def register_authorization_recovery_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: AuthorizationRecoveryRouteDependencies,
) -> None:
    common = dependencies.common

    def recovery_service(record_store: object) -> AuthorizationRecoveryService:
        required = (
            "read_authorization_bootstrap_state",
            "list_authorization_recovery_keys",
            "read_authorization_recovery_key",
            "write_authorization_recovery_key",
            "read_authorization_recovery_challenge",
            "list_authorization_recovery_challenges",
            "write_authorization_recovery_challenge",
            "write_authorization_recovery_audit",
            "list_authz_policy_records",
            "apply_authorization_recovery",
        )
        if not all(callable(getattr(record_store, name, None)) for name in required):
            raise RuntimeError("authorization_recovery_store_unavailable")
        return AuthorizationRecoveryService(
            record_store=record_store,  # type: ignore[arg-type]
            service_identity=LAUNCHPLANE_SERVICE_CONTEXT,
        )

    def require_browser_lifecycle_authority(
        *,
        identity: GitHubHumanIdentity,
        service: AuthorizationRecoveryService,
        record_store: object,
        trace_id: str,
    ) -> None:
        if service.bootstrap_state() == "pending":
            if identity.role == "admin":
                return
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Authorization recovery bootstrap requires an administrator GitHub session.",
            )
        reader = getattr(record_store, "list_authz_policy_records", None)
        if not callable(reader):
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="authz_policy_unavailable",
                message="The active authorization policy record is unavailable.",
            )
        try:
            records = tuple(reader(status="active", limit=2))
        except (RuntimeError, TypeError, ValueError):
            records = ()
        if len(records) != 1:
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="authz_policy_unavailable",
                message="The active authorization policy record is unavailable.",
            )
        if not records[0].policy.allows(
            identity=identity,
            action="authz_policy_grant.write",
            product="launchplane",
            context="launchplane",
            target=AuthorizationTarget(scope="global"),
        ):
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="GitHub human is not authorized for authorization recovery key lifecycle.",
            )

    def response(model: BaseModel, *, status_code: int = 200) -> JSONResponse:
        return JSONResponse(
            status_code=status_code,
            content=model.model_dump(mode="json"),
            headers={"Cache-Control": "no-store"},
        )

    def error(*, trace_id: str, status_code: int, code: str) -> JSONResponse:
        return JSONResponse(
            status_code=status_code,
            content={
                "trace_id": trace_id,
                "error": {"code": code, "message": "Authorization recovery request was rejected."},
            },
            headers={"Cache-Control": "no-store"},
        )

    async def parse_body(request: Request, model_type: type[_TModel]) -> _TModel:
        content_length = request.headers.get("Content-Length", "")
        try:
            if content_length and int(content_length) > _MAX_BODY_BYTES:
                raise ValueError("body_too_large")
        except ValueError as exception:
            if str(exception) == "body_too_large":
                raise
            raise ValueError("invalid_body") from exception
        body = await request.body()
        if len(body) > _MAX_BODY_BYTES:
            raise ValueError("body_too_large")
        try:
            return model_type.model_validate_json(body)
        except ValidationError as exception:
            raise ValueError("invalid_body") from exception

    def key_view(key: AuthorizationRecoveryKey) -> AuthorizationRecoveryKeyView:
        return AuthorizationRecoveryKeyView.model_validate(key.redacted())

    def audit_view(audit: AuthorizationRecoveryAudit) -> AuthorizationRecoveryAuditView:
        return AuthorizationRecoveryAuditView(
            audit_id=audit.audit_id,
            event=audit.event,
            status=audit.status,
            recorded_at=audit.recorded_at,
            challenge_id=audit.challenge_id,
            operation=audit.operation,
            key_id=audit.key_id,
            key_fingerprint_sha256=audit.key_fingerprint_sha256,
            reason_code=audit.reason_code,
        )

    def alert_view(record: OutboxDeliveryRecord) -> AuthorizationRecoveryAlertView:
        return AuthorizationRecoveryAlertView(
            delivery_id=record.delivery_id,
            state=record.state,
            action=record.action,
            created_at=record.created_at,
            updated_at=record.updated_at,
            challenge_id=record.aggregate_id,
        )

    def public_challenge_response(
        *, challenge: AuthorizationRecoveryChallenge, trace_id: str
    ) -> AuthorizationRecoveryPublicChallengeResponse:
        now = datetime.now(timezone.utc)
        expires_at = datetime.fromisoformat(challenge.expires_at.replace("Z", "+00:00"))
        status: Literal["prepared", "expired", "consumed"] = "prepared"
        if challenge.used_at:
            status = "consumed"
        elif expires_at <= now:
            status = "expired"
        signing_input = challenge.canonical_bytes(service_identity=LAUNCHPLANE_SERVICE_CONTEXT)
        return AuthorizationRecoveryPublicChallengeResponse(
            status=status,
            trace_id=trace_id,
            challenge_id=challenge.challenge_id,
            operation=challenge.operation,
            signing_key_id=challenge.signing_key_id,
            signing_key_fingerprint_sha256=challenge.signing_key_fingerprint_sha256,
            expires_at=challenge.expires_at,
            signing_input_base64=base64.b64encode(signing_input).decode("ascii"),
            signing_input_text=signing_input.decode("utf-8"),
        )

    def decode_signature(value: str) -> bytes:
        normalized = value.strip()
        if normalized.startswith("-----BEGIN SSH SIGNATURE-----"):
            signature = normalized.encode("utf-8")
        else:
            try:
                signature = base64.b64decode(normalized, validate=True)
            except (binascii.Error, ValueError) as exception:
                raise ValueError("invalid_signature") from exception
        if not signature or len(signature) > AUTHORIZATION_RECOVERY_SIGNATURE_MAX_BYTES:
            raise ValueError("invalid_signature")
        return signature

    async def read_browser_status(
        identity: Annotated[
            GitHubHumanIdentity, Depends(dependencies.read_github_human_identity)
        ],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> JSONResponse:
        trace_id = common.next_trace_id()
        try:
            service = recovery_service(record_store)
            require_browser_lifecycle_authority(
                identity=identity,
                service=service,
                record_store=record_store,
                trace_id=trace_id,
            )
            audit_reader = getattr(record_store, "list_authorization_recovery_audits", None)
            alert_reader = getattr(record_store, "list_outbox_delivery_records", None)
            audits = tuple(audit_reader(limit=_MAX_RECENT_EVIDENCE)) if callable(audit_reader) else ()
            alerts = (
                tuple(
                    alert_reader(
                        kind="operator_authorization_recovery_alert",
                        aggregate_type="authorization_recovery",
                        limit=_MAX_RECENT_EVIDENCE,
                    )
                )
                if callable(alert_reader)
                else ()
            )
            return response(
                AuthorizationRecoveryBrowserStatusResponse(
                    trace_id=trace_id,
                    readiness=service.readiness(),
                    audits=tuple(audit_view(audit) for audit in audits),
                    alerts=tuple(alert_view(alert) for alert in alerts),
                )
            )
        except PermissionError:
            return error(trace_id=trace_id, status_code=403, code="authorization_denied")
        except RuntimeError:
            return error(trace_id=trace_id, status_code=503, code="recovery_unavailable")

    async def enroll_browser_key(
        request: Request,
        identity: Annotated[
            GitHubHumanIdentity, Depends(dependencies.read_github_human_mutation_identity)
        ],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> JSONResponse:
        trace_id = common.next_trace_id()
        try:
            envelope = await parse_body(request, AuthorizationRecoveryKeyEnrollEnvelope)
            service = recovery_service(record_store)
            require_browser_lifecycle_authority(
                identity=identity,
                service=service,
                record_store=record_store,
                trace_id=trace_id,
            )
            key = service.enroll_key(
                key_id=envelope.key_id,
                custody_slot=envelope.custody_slot,
                public_key=envelope.public_key,
                allow_after_bootstrap=True,
            )
            return response(AuthorizationRecoveryKeyResponse(trace_id=trace_id, key=key_view(key)))
        except ValueError as exception:
            return error(
                trace_id=trace_id,
                status_code=413 if str(exception) == "body_too_large" else 400,
                code=str(exception) if str(exception) in {"body_too_large", "invalid_body"} else "invalid_request",
            )
        except PermissionError:
            return error(trace_id=trace_id, status_code=403, code="authorization_denied")
        except RuntimeError:
            return error(trace_id=trace_id, status_code=503, code="recovery_unavailable")

    async def read_browser_proof(
        key_id: str,
        identity: Annotated[
            GitHubHumanIdentity, Depends(dependencies.read_github_human_identity)
        ],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> JSONResponse:
        trace_id = common.next_trace_id()
        try:
            service = recovery_service(record_store)
            require_browser_lifecycle_authority(
                identity=identity,
                service=service,
                record_store=record_store,
                trace_id=trace_id,
            )
            signing_input = service.proof_bytes(key_id=key_id)
            return response(
                AuthorizationRecoveryProofResponse(
                    trace_id=trace_id,
                    key_id=key_id,
                    signing_input_base64=base64.b64encode(signing_input).decode("ascii"),
                    signing_input_text=signing_input.decode("utf-8"),
                )
            )
        except ValueError:
            return error(trace_id=trace_id, status_code=400, code="proof_unavailable")
        except PermissionError:
            return error(trace_id=trace_id, status_code=403, code="authorization_denied")
        except RuntimeError:
            return error(trace_id=trace_id, status_code=503, code="recovery_unavailable")

    async def verify_browser_key(
        key_id: str,
        request: Request,
        identity: Annotated[
            GitHubHumanIdentity, Depends(dependencies.read_github_human_mutation_identity)
        ],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> JSONResponse:
        trace_id = common.next_trace_id()
        try:
            envelope = await parse_body(request, AuthorizationRecoverySignatureEnvelope)
            service = recovery_service(record_store)
            require_browser_lifecycle_authority(
                identity=identity,
                service=service,
                record_store=record_store,
                trace_id=trace_id,
            )
            key = service.verify_key_proof(key_id=key_id, signature=decode_signature(envelope.signature))
            return response(AuthorizationRecoveryKeyResponse(trace_id=trace_id, key=key_view(key)))
        except ValueError as exception:
            return error(
                trace_id=trace_id,
                status_code=413 if str(exception) == "body_too_large" else 400,
                code=str(exception) if str(exception) in {"body_too_large", "invalid_body"} else "proof_rejected",
            )
        except PermissionError:
            return error(trace_id=trace_id, status_code=403, code="authorization_denied")
        except RuntimeError:
            return error(trace_id=trace_id, status_code=503, code="recovery_unavailable")

    async def revoke_browser_key(
        key_id: str,
        request: Request,
        identity: Annotated[
            GitHubHumanIdentity, Depends(dependencies.read_github_human_mutation_identity)
        ],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> JSONResponse:
        trace_id = common.next_trace_id()
        try:
            await parse_body(request, EmptyEnvelope)
            service = recovery_service(record_store)
            require_browser_lifecycle_authority(
                identity=identity,
                service=service,
                record_store=record_store,
                trace_id=trace_id,
            )
            key = service.revoke_key(key_id=key_id)
            return response(AuthorizationRecoveryKeyResponse(trace_id=trace_id, key=key_view(key)))
        except ValueError as exception:
            return error(
                trace_id=trace_id,
                status_code=413 if str(exception) == "body_too_large" else 400,
                code=str(exception) if str(exception) in {"body_too_large", "invalid_body"} else "revoke_rejected",
            )
        except PermissionError:
            return error(trace_id=trace_id, status_code=403, code="authorization_denied")
        except RuntimeError:
            return error(trace_id=trace_id, status_code=503, code="recovery_unavailable")

    async def prepare_public_recovery(
        request: Request,
        record_store: Annotated[object, Depends(common.get_record_store)],
        _credentials: Annotated[None, Depends(dependencies.reject_public_credentials)] = None,
    ) -> JSONResponse:
        trace_id = common.next_trace_id()
        try:
            envelope = await parse_body(request, AuthorizationRecoveryPrepareEnvelope)
            prepared = recovery_service(record_store).prepare(
                operation=envelope.operation,
                intended_github_id=envelope.intended_github_id,
                signing_key_id=envelope.signing_key_id,
                compromised_key_id=envelope.compromised_key_id,
                replacement_key_id=envelope.replacement_key_id,
            )
            return response(public_challenge_response(challenge=prepared.challenge, trace_id=trace_id))
        except PermissionError:
            return error(trace_id=trace_id, status_code=403, code="credentials_not_accepted")
        except ValueError as exception:
            return error(
                trace_id=trace_id,
                status_code=413 if str(exception) == "body_too_large" else 400,
                code=str(exception) if str(exception) in {"body_too_large", "invalid_body"} else "prepare_rejected",
            )
        except RuntimeError:
            return error(trace_id=trace_id, status_code=503, code="recovery_unavailable")

    def read_public_recovery(
        challenge_id: str,
        record_store: Annotated[object, Depends(common.get_record_store)],
        _credentials: Annotated[None, Depends(dependencies.reject_public_credentials)] = None,
    ) -> JSONResponse:
        trace_id = common.next_trace_id()
        try:
            reader = getattr(record_store, "read_authorization_recovery_challenge", None)
            if not callable(reader):
                raise RuntimeError("authorization_recovery_store_unavailable")
            challenge = reader(challenge_id)
            if challenge is None:
                return error(trace_id=trace_id, status_code=404, code="challenge_not_found")
            return response(public_challenge_response(challenge=challenge, trace_id=trace_id))
        except PermissionError:
            return error(trace_id=trace_id, status_code=403, code="credentials_not_accepted")
        except (RuntimeError, ValueError):
            return error(trace_id=trace_id, status_code=503, code="recovery_unavailable")

    async def apply_public_recovery(
        request: Request,
        record_store: Annotated[object, Depends(common.get_record_store)],
        _credentials: Annotated[None, Depends(dependencies.reject_public_credentials)] = None,
    ) -> JSONResponse:
        trace_id = common.next_trace_id()
        try:
            envelope = await parse_body(request, AuthorizationRecoveryPublicApplyEnvelope)
            result = recovery_service(record_store).apply(
                challenge_id=envelope.challenge_id,
                key_id=envelope.signing_key_id,
                signature=decode_signature(envelope.signature),
                trace_id=trace_id,
            )
            return response(
                AuthorizationRecoveryApplyResponse(
                    status=cast(Literal["applied", "adopted"], result.status),
                    trace_id=trace_id,
                    challenge_id=envelope.challenge_id,
                )
            )
        except PermissionError:
            return error(trace_id=trace_id, status_code=403, code="credentials_not_accepted")
        except ValueError as exception:
            return error(
                trace_id=trace_id,
                status_code=413 if str(exception) == "body_too_large" else 400,
                code=str(exception) if str(exception) in {"body_too_large", "invalid_body", "invalid_signature"} else "apply_rejected",
            )
        except RuntimeError:
            return error(trace_id=trace_id, status_code=503, code="recovery_unavailable")

    app.add_api_route(
        AUTHORIZATION_RECOVERY_BROWSER_STATUS_ROUTE,
        read_browser_status,
        methods=["GET"],
        response_model=AuthorizationRecoveryBrowserStatusResponse,
        operation_id="read_authorization_recovery_status",
        summary="Read redacted authorization recovery readiness",
    )
    app.add_api_route(
        AUTHORIZATION_RECOVERY_BROWSER_ENROLL_ROUTE,
        enroll_browser_key,
        methods=["POST"],
        response_model=AuthorizationRecoveryKeyResponse,
        operation_id="enroll_authorization_recovery_key",
        summary="Enroll a pending authorization recovery hardware public key",
        openapi_extra=_request_body_schema(AuthorizationRecoveryKeyEnrollEnvelope),
    )
    app.add_api_route(
        AUTHORIZATION_RECOVERY_BROWSER_PROOF_ROUTE,
        read_browser_proof,
        methods=["GET"],
        response_model=AuthorizationRecoveryProofResponse,
        operation_id="read_authorization_recovery_key_proof",
        summary="Read exact authorization recovery key proof signing input",
    )
    app.add_api_route(
        AUTHORIZATION_RECOVERY_BROWSER_VERIFY_ROUTE,
        verify_browser_key,
        methods=["POST"],
        response_model=AuthorizationRecoveryKeyResponse,
        operation_id="verify_authorization_recovery_key",
        summary="Verify an authorization recovery key proof",
        openapi_extra=_request_body_schema(AuthorizationRecoverySignatureEnvelope),
    )
    app.add_api_route(
        AUTHORIZATION_RECOVERY_BROWSER_REVOKE_ROUTE,
        revoke_browser_key,
        methods=["POST"],
        response_model=AuthorizationRecoveryKeyResponse,
        operation_id="revoke_authorization_recovery_key",
        summary="Revoke an authorization recovery key without reducing custody independence",
        openapi_extra=_request_body_schema(EmptyEnvelope),
    )
    app.add_api_route(
        AUTHORIZATION_RECOVERY_PUBLIC_PREPARE_ROUTE,
        prepare_public_recovery,
        methods=["POST"],
        response_model=AuthorizationRecoveryPublicChallengeResponse,
        operation_id="prepare_authorization_recovery",
        summary="Prepare a bounded unsigned authorization recovery challenge",
        openapi_extra=_request_body_schema(AuthorizationRecoveryPrepareEnvelope),
    )
    app.add_api_route(
        AUTHORIZATION_RECOVERY_PUBLIC_STATUS_ROUTE,
        read_public_recovery,
        methods=["GET"],
        response_model=AuthorizationRecoveryPublicChallengeResponse,
        operation_id="read_authorization_recovery_challenge",
        summary="Read a redacted prepared authorization recovery challenge",
    )
    app.add_api_route(
        AUTHORIZATION_RECOVERY_PUBLIC_APPLY_ROUTE,
        apply_public_recovery,
        methods=["POST"],
        response_model=AuthorizationRecoveryApplyResponse,
        operation_id="apply_authorization_recovery",
        summary="Apply an exact hardware-signed authorization recovery challenge",
        openapi_extra=_request_body_schema(AuthorizationRecoveryPublicApplyEnvelope),
    )


class EmptyEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _request_body_schema(model: type[BaseModel]) -> dict[str, object]:
    return {
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": model.model_json_schema()}},
        }
    }
