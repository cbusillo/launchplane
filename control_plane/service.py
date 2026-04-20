from __future__ import annotations

import io
import json
import uuid
from pathlib import Path
from typing import Callable
from wsgiref.simple_server import make_server

import click
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.preview_mutation_request import (
    PreviewDestroyMutationRequest,
    PreviewGenerationMutationRequest,
    PreviewMutationRequest,
)
from control_plane.contracts.promotion_record import PromotionRecord
from control_plane.harbor_mutations import (
    apply_harbor_destroy_preview,
    apply_harbor_generation_evidence,
    control_plane_root,
)
from control_plane.service_auth import HarborAuthzPolicy, TokenVerifier, load_authz_policy
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.workflows.evidence_ingestion import (
    apply_deployment_evidence,
    apply_promotion_evidence,
)
from control_plane.workflows.verireel_testing_deploy import (
    VeriReelTestingDeployRequest,
    execute_verireel_testing_deploy,
)


class PreviewGenerationEvidenceEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    preview: PreviewMutationRequest
    generation: PreviewGenerationMutationRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "PreviewGenerationEvidenceEnvelope":
        if not self.product.strip():
            raise ValueError("preview generation evidence requires product")
        if self.preview.context != self.generation.context:
            raise ValueError("preview generation evidence requires matching contexts")
        if self.preview.anchor_repo != self.generation.anchor_repo:
            raise ValueError("preview generation evidence requires matching anchor_repo")
        if self.preview.anchor_pr_number != self.generation.anchor_pr_number:
            raise ValueError("preview generation evidence requires matching anchor_pr_number")
        if self.preview.anchor_pr_url != self.generation.anchor_pr_url:
            raise ValueError("preview generation evidence requires matching anchor_pr_url")
        return self


class PreviewDestroyedEvidenceEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    destroy: PreviewDestroyMutationRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "PreviewDestroyedEvidenceEnvelope":
        if not self.product.strip():
            raise ValueError("preview destroyed evidence requires product")
        return self


class DeploymentEvidenceEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    deployment: DeploymentRecord

    @model_validator(mode="after")
    def _validate_alignment(self) -> "DeploymentEvidenceEnvelope":
        if not self.product.strip():
            raise ValueError("deployment evidence requires product")
        return self


class PromotionEvidenceEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    promotion: PromotionRecord

    @model_validator(mode="after")
    def _validate_alignment(self) -> "PromotionEvidenceEnvelope":
        if not self.product.strip():
            raise ValueError("promotion evidence requires product")
        return self


class VeriReelTestingDeployEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    deploy: VeriReelTestingDeployRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "VeriReelTestingDeployEnvelope":
        if self.product.strip() != "verireel":
            raise ValueError("VeriReel testing deploy requires product 'verireel'.")
        return self


def _json_response(
    *,
    start_response: Callable[[str, list[tuple[str, str]]], None],
    status_code: int,
    payload: dict[str, object],
) -> list[bytes]:
    encoded = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    status_line = f"{status_code} {_http_status_text(status_code)}"
    start_response(
        status_line,
        [
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(encoded))),
        ],
    )
    return [encoded]


def _http_status_text(status_code: int) -> str:
    return {
        200: "OK",
        202: "Accepted",
        400: "Bad Request",
        401: "Unauthorized",
        403: "Forbidden",
        404: "Not Found",
        405: "Method Not Allowed",
        500: "Internal Server Error",
    }.get(status_code, "OK")


def _trace_id() -> str:
    return f"harbor_req_{uuid.uuid4().hex}"


def _read_json_request(environ: dict[str, object]) -> dict[str, object]:
    content_length = int(str(environ.get("CONTENT_LENGTH", "0") or "0"))
    body_stream = environ.get("wsgi.input")
    if not isinstance(body_stream, io.BytesIO):
        body_bytes = body_stream.read(content_length) if body_stream is not None else b""
    else:
        body_bytes = body_stream.read(content_length)
    if not body_bytes:
        raise ValueError("Request body is required.")
    try:
        payload = json.loads(body_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Request body must be valid UTF-8 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Request body must decode to a JSON object.")
    return payload


def _bearer_token(environ: dict[str, object]) -> str:
    header = str(environ.get("HTTP_AUTHORIZATION", "")).strip()
    if not header:
        raise PermissionError("Authorization header is required.")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise PermissionError("Authorization header must use Bearer token format.")
    return token.strip()


def create_harbor_service_app(
    *,
    state_dir: Path,
    verifier: TokenVerifier,
    authz_policy: HarborAuthzPolicy,
    control_plane_root_path: Path | None = None,
):
    resolved_root = control_plane_root_path or control_plane_root()

    def app(
        environ: dict[str, object],
        start_response: Callable[[str, list[tuple[str, str]]], None],
    ) -> list[bytes]:
        request_trace_id = _trace_id()
        method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        path = str(environ.get("PATH_INFO", ""))
        if method == "GET" and path == "/v1/health":
            return _json_response(
                start_response=start_response,
                status_code=200,
                payload={"status": "ok", "trace_id": request_trace_id},
            )
        if path not in {
            "/v1/evidence/deployments",
            "/v1/evidence/previews/generations",
            "/v1/evidence/previews/destroyed",
            "/v1/evidence/promotions",
            "/v1/drivers/verireel/testing-deploy",
        }:
            return _json_response(
                start_response=start_response,
                status_code=404,
                payload={
                    "status": "rejected",
                    "trace_id": request_trace_id,
                    "error": {"code": "not_found", "message": f"No Harbor route for {path}."},
                },
            )
        if method != "POST":
            return _json_response(
                start_response=start_response,
                status_code=405,
                payload={
                    "status": "rejected",
                    "trace_id": request_trace_id,
                    "error": {
                        "code": "method_not_allowed",
                        "message": "Only POST is allowed for this Harbor route.",
                    },
                },
            )
        try:
            token = _bearer_token(environ)
            identity = verifier.verify(token)
            payload = _read_json_request(environ)
            record_store = FilesystemRecordStore(state_dir=state_dir)
            driver_result = None
            if path == "/v1/evidence/deployments":
                request = DeploymentEvidenceEnvelope.model_validate(payload)
                if not authz_policy.allows(
                    identity=identity,
                    action="deployment.write",
                    product=request.product,
                    context=request.deployment.context,
                ):
                    return _json_response(
                        start_response=start_response,
                        status_code=403,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "authorization_denied",
                                "message": (
                                    "Workflow cannot write deployment evidence for the requested"
                                    " product/context."
                                ),
                            },
                        },
                    )
                result = apply_deployment_evidence(
                    record_store=record_store,
                    deployment_record=request.deployment,
                )
            elif path == "/v1/drivers/verireel/testing-deploy":
                request = VeriReelTestingDeployEnvelope.model_validate(payload)
                if not authz_policy.allows(
                    identity=identity,
                    action="verireel_testing_deploy.execute",
                    product=request.product,
                    context=request.deploy.context,
                ):
                    return _json_response(
                        start_response=start_response,
                        status_code=403,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "authorization_denied",
                                "message": (
                                    "Workflow cannot execute the VeriReel testing deploy driver"
                                    " for the requested product/context."
                                ),
                            },
                        },
                    )
                driver_result = execute_verireel_testing_deploy(
                    control_plane_root=resolved_root,
                    record_store=record_store,
                    request=request.deploy,
                )
                result = {"deployment_record_id": driver_result.deployment_record_id}
            elif path == "/v1/evidence/promotions":
                request = PromotionEvidenceEnvelope.model_validate(payload)
                if not authz_policy.allows(
                    identity=identity,
                    action="promotion.write",
                    product=request.product,
                    context=request.promotion.context,
                ):
                    return _json_response(
                        start_response=start_response,
                        status_code=403,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "authorization_denied",
                                "message": (
                                    "Workflow cannot write promotion evidence for the requested"
                                    " product/context."
                                ),
                            },
                        },
                    )
                result = apply_promotion_evidence(
                    record_store=record_store,
                    promotion_record=request.promotion,
                )
            elif path == "/v1/evidence/previews/generations":
                request = PreviewGenerationEvidenceEnvelope.model_validate(payload)
                if not authz_policy.allows(
                    identity=identity,
                    action="preview_generation.write",
                    product=request.product,
                    context=request.preview.context,
                ):
                    return _json_response(
                        start_response=start_response,
                        status_code=403,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "authorization_denied",
                                "message": (
                                    "Workflow cannot write preview generation evidence for the"
                                    " requested product/context."
                                ),
                            },
                        },
                    )
                result = apply_harbor_generation_evidence(
                    control_plane_root_path=resolved_root,
                    record_store=record_store,
                    preview_request=request.preview,
                    generation_request=request.generation,
                )
            else:
                request = PreviewDestroyedEvidenceEnvelope.model_validate(payload)
                if not authz_policy.allows(
                    identity=identity,
                    action="preview_destroyed.write",
                    product=request.product,
                    context=request.destroy.context,
                ):
                    return _json_response(
                        start_response=start_response,
                        status_code=403,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "authorization_denied",
                                "message": (
                                    "Workflow cannot write preview destroyed evidence for the"
                                    " requested product/context."
                                ),
                            },
                        },
                    )
                result = apply_harbor_destroy_preview(
                    record_store=record_store,
                    request=request.destroy,
                )
        except PermissionError as exc:
            return _json_response(
                start_response=start_response,
                status_code=401,
                payload={
                    "status": "rejected",
                    "trace_id": request_trace_id,
                    "error": {"code": "authentication_required", "message": str(exc)},
                },
            )
        except ValidationError as exc:
            return _json_response(
                start_response=start_response,
                status_code=400,
                payload={
                    "status": "rejected",
                    "trace_id": request_trace_id,
                    "error": {"code": "invalid_request", "message": str(exc)},
                },
            )
        except (ValueError, click.ClickException) as exc:
            return _json_response(
                start_response=start_response,
                status_code=400,
                payload={
                    "status": "rejected",
                    "trace_id": request_trace_id,
                    "error": {"code": "invalid_request", "message": str(exc)},
                },
            )
        return _json_response(
            start_response=start_response,
            status_code=202,
            payload={
                "status": "accepted",
                "trace_id": request_trace_id,
                "records": {
                    key: str(value)
                    for key, value in result.items()
                    if key
                    in {
                        "deployment_record_id",
                        "inventory_record_id",
                        "preview_id",
                        "generation_id",
                        "promotion_record_id",
                        "transition",
                    }
                },
                **({"result": driver_result.model_dump(mode="json")} if driver_result else {}),
            },
        )

    return app


def serve_harbor_service(
    *,
    state_dir: Path,
    policy_file: Path,
    host: str,
    port: int,
    audience: str,
) -> None:
    from control_plane.service_auth import GitHubOidcVerifier

    authz_policy = load_authz_policy(policy_file)
    verifier = GitHubOidcVerifier(audience=audience)
    application = create_harbor_service_app(
        state_dir=state_dir,
        verifier=verifier,
        authz_policy=authz_policy,
    )
    with make_server(host, port, application) as server:
        click.echo(f"Harbor service listening on http://{host}:{port}")
        server.serve_forever()
