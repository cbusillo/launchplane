from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import io
import json
import os
import uuid
from pathlib import Path
from typing import Callable
from wsgiref.simple_server import make_server

import click
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from control_plane import dokploy as control_plane_dokploy
from control_plane import secrets as control_plane_secrets
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.idempotency_record import build_launchplane_idempotency_record_id
from control_plane.contracts.preview_mutation_request import (
    PreviewDestroyMutationRequest,
    PreviewGenerationMutationRequest,
    PreviewMutationRequest,
)
from control_plane.contracts.promotion_record import PromotionRecord
from control_plane.launchplane_mutations import (
    apply_launchplane_destroy_preview,
    apply_launchplane_generation_evidence,
    control_plane_root,
)
from control_plane.service_auth import (
    LaunchplaneAuthzPolicy,
    TokenVerifier,
    load_authz_policy,
    parse_authz_policy_toml,
)
from control_plane.storage.factory import build_record_store, storage_backend_name
from control_plane.workflows.evidence_ingestion import (
    apply_deployment_evidence,
    apply_promotion_evidence,
)
from control_plane.workflows.verireel_stable_deploy import (
    VeriReelStableDeployRequest,
    execute_verireel_stable_deploy,
)
from control_plane.workflows.verireel_prod_promotion import (
    VeriReelProdPromotionRequest,
    execute_verireel_prod_promotion,
)
from control_plane.workflows.verireel_prod_rollback import (
    VeriReelProdRollbackRequest,
    execute_verireel_prod_rollback,
)
from control_plane.workflows.verireel_preview_driver import (
    VeriReelPreviewDestroyRequest,
    VeriReelPreviewRefreshRequest,
    execute_verireel_preview_destroy,
    execute_verireel_preview_refresh,
)


_LAUNCHPLANE_SERVICE_CONTEXT = "launchplane"
_LAUNCHPLANE_IMAGE_REFERENCE_ENV_KEY = "DOCKER_IMAGE_REFERENCE"


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


class BackupGateEvidenceEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    backup_gate: BackupGateRecord

    @model_validator(mode="after")
    def _validate_alignment(self) -> "BackupGateEvidenceEnvelope":
        if not self.product.strip():
            raise ValueError("backup gate evidence requires product")
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
    deploy: VeriReelStableDeployRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "VeriReelTestingDeployEnvelope":
        if self.product.strip() != "verireel":
            raise ValueError("VeriReel testing deploy requires product 'verireel'.")
        if self.deploy.instance != "testing":
            raise ValueError("VeriReel testing deploy requires instance 'testing'.")
        return self


class VeriReelProdDeployEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    deploy: VeriReelStableDeployRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "VeriReelProdDeployEnvelope":
        if self.product.strip() != "verireel":
            raise ValueError("VeriReel prod deploy requires product 'verireel'.")
        if self.deploy.instance != "prod":
            raise ValueError("VeriReel prod deploy requires instance 'prod'.")
        return self


class VeriReelProdPromotionEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    promotion: VeriReelProdPromotionRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "VeriReelProdPromotionEnvelope":
        if self.product.strip() != "verireel":
            raise ValueError("VeriReel prod promotion requires product 'verireel'.")
        return self


class VeriReelProdRollbackEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    rollback: VeriReelProdRollbackRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "VeriReelProdRollbackEnvelope":
        if self.product.strip() != "verireel":
            raise ValueError("VeriReel prod rollback requires product 'verireel'.")
        return self


class VeriReelPreviewRefreshEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    refresh: VeriReelPreviewRefreshRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "VeriReelPreviewRefreshEnvelope":
        if self.product.strip() != "verireel":
            raise ValueError("VeriReel preview refresh requires product 'verireel'.")
        return self


class VeriReelPreviewDestroyEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    destroy: VeriReelPreviewDestroyRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "VeriReelPreviewDestroyEnvelope":
        if self.product.strip() != "verireel":
            raise ValueError("VeriReel preview destroy requires product 'verireel'.")
        return self


class LaunchplaneSelfDeployRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_type: str
    target_id: str
    image_reference: str
    policy_b64: str = ""
    no_cache: bool = False

    @model_validator(mode="after")
    def _validate_values(self) -> "LaunchplaneSelfDeployRequest":
        normalized_target_type = self.target_type.strip()
        if normalized_target_type not in {"compose", "application"}:
            raise ValueError(
                "Launchplane self deploy requires target_type 'compose' or 'application'."
            )
        if not self.target_id.strip():
            raise ValueError("Launchplane self deploy requires target_id.")
        if not self.image_reference.strip():
            raise ValueError("Launchplane self deploy requires image_reference.")
        normalized_policy_b64 = self.policy_b64.strip()
        if normalized_policy_b64:
            try:
                policy_text = base64.b64decode(normalized_policy_b64, validate=True).decode("utf-8")
            except Exception as error:
                raise ValueError(
                    "Launchplane self deploy requires valid base64 policy_b64."
                ) from error
            parse_authz_policy_toml(policy_text)
        self.target_type = normalized_target_type
        self.target_id = self.target_id.strip()
        self.image_reference = self.image_reference.strip()
        self.policy_b64 = normalized_policy_b64
        return self


class LaunchplaneSelfDeployEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    deploy: LaunchplaneSelfDeployRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "LaunchplaneSelfDeployEnvelope":
        if self.product.strip() != "launchplane":
            raise ValueError("Launchplane self deploy requires product 'launchplane'.")
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
        409: "Conflict",
        500: "Internal Server Error",
    }.get(status_code, "OK")


def _trace_id() -> str:
    return f"launchplane_req_{uuid.uuid4().hex}"


def _utc_now_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _not_found_response(
    *,
    start_response: Callable[[str, list[tuple[str, str]]], None],
    trace_id: str,
    path: str,
) -> list[bytes]:
    return _json_response(
        start_response=start_response,
        status_code=404,
        payload={
            "status": "rejected",
            "trace_id": trace_id,
            "error": {"code": "not_found", "message": f"No Launchplane route for {path}."},
        },
    )


def _match_read_route(path: str) -> tuple[str, dict[str, str]] | None:
    segments = [segment for segment in path.split("/") if segment]
    if len(segments) == 3 and segments[:2] == ["v1", "deployments"]:
        return "deployment.read", {"record_id": segments[2]}
    if len(segments) == 3 and segments[:2] == ["v1", "promotions"]:
        return "promotion.read", {"record_id": segments[2]}
    if len(segments) == 4 and segments[:2] == ["v1", "inventory"]:
        return "inventory.read", {"context": segments[2], "instance": segments[3]}
    if len(segments) == 3 and segments[:2] == ["v1", "previews"]:
        return "preview.read", {"preview_id": segments[2]}
    if len(segments) == 4 and segments[:2] == ["v1", "previews"] and segments[3] == "history":
        return "preview.read", {"preview_id": segments[2], "include_history": "true"}
    if len(segments) == 3 and segments[:2] == ["v1", "secrets"]:
        return "secret.read", {"secret_id": segments[2]}
    if len(segments) == 4 and segments[:2] == ["v1", "contexts"] and segments[3] == "secrets":
        return "secret.list", {"context": segments[2]}
    if (
        len(segments) == 6
        and segments[:2] == ["v1", "contexts"]
        and segments[3] == "instances"
        and segments[5] == "secrets"
    ):
        return "secret.list", {"context": segments[2], "instance": segments[4]}
    if (
        len(segments) == 5
        and segments[:2] == ["v1", "contexts"]
        and segments[3:] == ["operations", "recent"]
    ):
        return "operations.read", {"context": segments[2]}
    if len(segments) == 3 and segments == ["v1", "service", "runtime"]:
        return "launchplane_service.read", {}
    return None


def _secret_capable_store(record_store: object):
    if hasattr(record_store, "read_secret_record") and hasattr(record_store, "list_secret_records"):
        return record_store
    return None


def _idempotency_capable_store(record_store: object):
    if hasattr(record_store, "read_idempotency_record") and hasattr(
        record_store, "write_idempotency_record"
    ):
        return record_store
    return None


def _idempotency_key(environ: dict[str, object]) -> str:
    return str(environ.get("HTTP_IDEMPOTENCY_KEY", "")).strip()


def _idempotency_scope(identity) -> str:
    workflow_ref = identity.workflow_ref or identity.job_workflow_ref or ""
    return "|".join(
        (
            str(identity.repository).strip(),
            str(workflow_ref).strip(),
            str(identity.subject).strip(),
        )
    )


def _request_fingerprint(payload: dict[str, object]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _accepted_payload(
    *,
    trace_id: str,
    result: dict[str, object],
    driver_result: BaseModel | dict[str, object] | None,
    replayed: bool = False,
    original_trace_id: str = "",
) -> dict[str, object]:
    serialized_driver_result: dict[str, object] | None = None
    if isinstance(driver_result, BaseModel):
        serialized_driver_result = driver_result.model_dump(mode="json")
    elif isinstance(driver_result, dict):
        serialized_driver_result = dict(driver_result)
    payload: dict[str, object] = {
        "status": "accepted",
        "trace_id": trace_id,
        "records": {
            key: str(value)
            for key, value in result.items()
            if key
            in {
                "deployment_record_id",
                "backup_gate_record_id",
                "backup_record_id",
                "inventory_record_id",
                "preview_id",
                "generation_id",
                "promotion_record_id",
                "target_id",
                "target_type",
                "image_reference",
                "transition",
            }
        },
        **({"result": serialized_driver_result} if serialized_driver_result else {}),
    }
    if replayed:
        payload["replayed"] = True
        payload["original_trace_id"] = original_trace_id
    return payload


def _replay_idempotent_response(
    *,
    start_response: Callable[[str, list[tuple[str, str]]], None],
    trace_id: str,
    stored_record: LaunchplaneIdempotencyRecord,
) -> list[bytes]:
    stored_payload = dict(stored_record.response_payload)
    stored_driver_result = stored_payload.get("result")
    result_payload = _accepted_payload(
        trace_id=trace_id,
        result=dict(stored_payload.get("records") or {}),
        driver_result=stored_driver_result if isinstance(stored_driver_result, dict) else None,
        replayed=True,
        original_trace_id=stored_record.response_trace_id,
    )
    return _json_response(
        start_response=start_response,
        status_code=stored_record.response_status_code,
        payload=result_payload,
    )


def _read_idempotency_record(
    *,
    record_store: object,
    scope: str,
    route_path: str,
    idempotency_key: str,
) -> LaunchplaneIdempotencyRecord | None:
    idempotency_store = _idempotency_capable_store(record_store)
    if idempotency_store is None or not idempotency_key:
        return None
    return idempotency_store.read_idempotency_record(
        scope=scope,
        route_path=route_path,
        idempotency_key=idempotency_key,
    )


def _write_idempotency_record(
    *,
    record_store: object,
    scope: str,
    route_path: str,
    idempotency_key: str,
    request_fingerprint: str,
    response_status_code: int,
    response_trace_id: str,
    response_payload: dict[str, object],
) -> None:
    idempotency_store = _idempotency_capable_store(record_store)
    if idempotency_store is None or not idempotency_key:
        return
    idempotency_store.write_idempotency_record(
        LaunchplaneIdempotencyRecord(
            record_id=build_launchplane_idempotency_record_id(
                scope=scope,
                route_path=route_path,
                idempotency_key=idempotency_key,
            ),
            scope=scope,
            route_path=route_path,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            response_status_code=response_status_code,
            response_trace_id=response_trace_id,
            recorded_at=_utc_now_timestamp(),
            response_payload=response_payload,
        )
    )


def _check_idempotent_request(
    *,
    record_store: object,
    scope: str,
    route_path: str,
    idempotency_key: str,
    request_fingerprint: str,
    start_response: Callable[[str, list[tuple[str, str]]], None],
    trace_id: str,
) -> list[bytes] | None:
    stored_record = _read_idempotency_record(
        record_store=record_store,
        scope=scope,
        route_path=route_path,
        idempotency_key=idempotency_key,
    )
    if stored_record is None:
        return None
    if stored_record.request_fingerprint != request_fingerprint:
        return _json_response(
            start_response=start_response,
            status_code=409,
            payload={
                "status": "rejected",
                "trace_id": trace_id,
                "error": {
                    "code": "idempotency_key_reused",
                    "message": (
                        "Idempotency-Key was already used for a different Launchplane request payload on this route."
                    ),
                },
            },
        )
    return _replay_idempotent_response(
        start_response=start_response,
        trace_id=trace_id,
        stored_record=stored_record,
    )


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


def _launchplane_policy_sha256_from_env() -> str:
    policy_toml = os.environ.get("LAUNCHPLANE_POLICY_TOML", "").strip()
    if policy_toml:
        return hashlib.sha256(policy_toml.encode("utf-8")).hexdigest()

    policy_b64 = os.environ.get("LAUNCHPLANE_POLICY_B64", "").strip()
    if policy_b64:
        try:
            policy_bytes = base64.b64decode(policy_b64, validate=True)
        except Exception:
            return ""
        return hashlib.sha256(policy_bytes).hexdigest()

    policy_file = os.environ.get("LAUNCHPLANE_POLICY_FILE", "").strip()
    if not policy_file:
        return ""
    try:
        return hashlib.sha256(Path(policy_file).read_bytes()).hexdigest()
    except OSError:
        return ""


def _launchplane_runtime_payload(*, storage_backend: str) -> dict[str, object]:
    return {
        "authz_policy_sha256": _launchplane_policy_sha256_from_env(),
        "docker_image_reference": os.environ.get(_LAUNCHPLANE_IMAGE_REFERENCE_ENV_KEY, "").strip(),
        "service_audience": os.environ.get("LAUNCHPLANE_SERVICE_AUDIENCE", "").strip(),
        "storage_backend": storage_backend,
    }


def _request_launchplane_self_deploy(
    *,
    control_plane_root_path: Path,
    request: LaunchplaneSelfDeployRequest,
) -> dict[str, object]:
    host, token = control_plane_dokploy.read_dokploy_config(
        control_plane_root=control_plane_root_path
    )
    target_payload = control_plane_dokploy.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type=request.target_type,
        target_id=request.target_id,
    )
    raw_env_text = str(target_payload.get("env") or "")
    previous_env_map = control_plane_dokploy.parse_dokploy_env_text(raw_env_text)
    updates = {_LAUNCHPLANE_IMAGE_REFERENCE_ENV_KEY: request.image_reference}
    removals: tuple[str, ...] = ()
    if request.policy_b64:
        updates["LAUNCHPLANE_POLICY_B64"] = request.policy_b64
        removals = ("LAUNCHPLANE_POLICY_TOML", "LAUNCHPLANE_POLICY_FILE")
    updated_env_text = control_plane_dokploy.render_dokploy_env_text_with_overrides(
        raw_env_text,
        updates=updates,
        removals=removals,
    )
    if updated_env_text != raw_env_text:
        control_plane_dokploy.update_dokploy_target_env(
            host=host,
            token=token,
            target_type=request.target_type,
            target_id=request.target_id,
            target_payload=target_payload,
            env_text=updated_env_text,
        )
    control_plane_dokploy.trigger_deployment(
        host=host,
        token=token,
        target_type=request.target_type,
        target_id=request.target_id,
        no_cache=request.no_cache,
    )
    return {
        "target_type": request.target_type,
        "target_id": request.target_id,
        "image_reference": request.image_reference,
        "image_reference_changed": previous_env_map.get(_LAUNCHPLANE_IMAGE_REFERENCE_ENV_KEY, "")
        != request.image_reference,
        "authz_policy_changed": bool(request.policy_b64)
        and previous_env_map.get("LAUNCHPLANE_POLICY_B64", "") != request.policy_b64,
        "authz_policy_sha256": (
            hashlib.sha256(base64.b64decode(request.policy_b64, validate=True)).hexdigest()
            if request.policy_b64
            else ""
        ),
    }


def create_launchplane_service_app(
    *,
    state_dir: Path,
    verifier: TokenVerifier,
    authz_policy: LaunchplaneAuthzPolicy,
    control_plane_root_path: Path | None = None,
    database_url: str | None = None,
):
    resolved_root = control_plane_root_path or control_plane_root()
    record_store = build_record_store(state_dir=state_dir, database_url=database_url)
    storage_backend = storage_backend_name(record_store)
    write_routes = {
        "/v1/evidence/deployments",
        "/v1/evidence/backup-gates",
        "/v1/evidence/previews/generations",
        "/v1/evidence/previews/destroyed",
        "/v1/evidence/promotions",
        "/v1/drivers/launchplane/self-deploy",
        "/v1/drivers/verireel/preview-refresh",
        "/v1/drivers/verireel/preview-destroy",
        "/v1/drivers/verireel/testing-deploy",
        "/v1/drivers/verireel/prod-deploy",
        "/v1/drivers/verireel/prod-promotion",
        "/v1/drivers/verireel/prod-rollback",
    }

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
                payload={
                    "status": "ok",
                    "trace_id": request_trace_id,
                    "storage_backend": storage_backend,
                },
            )
        read_route = _match_read_route(path)
        if path not in write_routes and read_route is None:
            return _not_found_response(
                start_response=start_response,
                trace_id=request_trace_id,
                path=path,
            )
        if method not in {"GET", "POST"}:
            return _json_response(
                start_response=start_response,
                status_code=405,
                payload={
                    "status": "rejected",
                    "trace_id": request_trace_id,
                    "error": {
                        "code": "method_not_allowed",
                        "message": "Only GET and POST are allowed for Launchplane routes.",
                    },
                },
            )
        if method == "GET" and read_route is None:
            return _json_response(
                start_response=start_response,
                status_code=405,
                payload={
                    "status": "rejected",
                    "trace_id": request_trace_id,
                    "error": {
                        "code": "method_not_allowed",
                        "message": "Only POST is allowed for this Launchplane route.",
                    },
                },
            )
        if method == "POST" and path not in write_routes:
            return _json_response(
                start_response=start_response,
                status_code=405,
                payload={
                    "status": "rejected",
                    "trace_id": request_trace_id,
                    "error": {
                        "code": "method_not_allowed",
                        "message": "Only GET is allowed for this Launchplane route.",
                    },
                },
            )
        try:
            token = _bearer_token(environ)
            identity = verifier.verify(token)
            if method == "GET":
                assert read_route is not None
                action, params = read_route
                if action == "deployment.read":
                    deployment = record_store.read_deployment_record(params["record_id"])
                    if not authz_policy.allows(
                        identity=identity,
                        action=action,
                        product="launchplane",
                        context=deployment.context,
                    ):
                        return _json_response(
                            start_response=start_response,
                            status_code=403,
                            payload={
                                "status": "rejected",
                                "trace_id": request_trace_id,
                                "error": {
                                    "code": "authorization_denied",
                                    "message": "Workflow cannot read deployment records for the requested context.",
                                },
                            },
                        )
                    return _json_response(
                        start_response=start_response,
                        status_code=200,
                        payload={
                            "status": "ok",
                            "trace_id": request_trace_id,
                            "record": deployment.model_dump(mode="json"),
                        },
                    )
                if action == "promotion.read":
                    promotion = record_store.read_promotion_record(params["record_id"])
                    if not authz_policy.allows(
                        identity=identity,
                        action=action,
                        product="launchplane",
                        context=promotion.context,
                    ):
                        return _json_response(
                            start_response=start_response,
                            status_code=403,
                            payload={
                                "status": "rejected",
                                "trace_id": request_trace_id,
                                "error": {
                                    "code": "authorization_denied",
                                    "message": "Workflow cannot read promotion records for the requested context.",
                                },
                            },
                        )
                    return _json_response(
                        start_response=start_response,
                        status_code=200,
                        payload={
                            "status": "ok",
                            "trace_id": request_trace_id,
                            "record": promotion.model_dump(mode="json"),
                        },
                    )
                if action == "inventory.read":
                    inventory = record_store.read_environment_inventory(
                        context_name=params["context"],
                        instance_name=params["instance"],
                    )
                    if not authz_policy.allows(
                        identity=identity,
                        action=action,
                        product="launchplane",
                        context=inventory.context,
                    ):
                        return _json_response(
                            start_response=start_response,
                            status_code=403,
                            payload={
                                "status": "rejected",
                                "trace_id": request_trace_id,
                                "error": {
                                    "code": "authorization_denied",
                                    "message": "Workflow cannot read inventory for the requested context.",
                                },
                            },
                        )
                    return _json_response(
                        start_response=start_response,
                        status_code=200,
                        payload={
                            "status": "ok",
                            "trace_id": request_trace_id,
                            "record": inventory.model_dump(mode="json"),
                        },
                    )
                if action == "preview.read":
                    preview = record_store.read_preview_record(params["preview_id"])
                    if not authz_policy.allows(
                        identity=identity,
                        action=action,
                        product="launchplane",
                        context=preview.context,
                    ):
                        return _json_response(
                            start_response=start_response,
                            status_code=403,
                            payload={
                                "status": "rejected",
                                "trace_id": request_trace_id,
                                "error": {
                                    "code": "authorization_denied",
                                    "message": "Workflow cannot read previews for the requested context.",
                                },
                            },
                        )
                    if params.get("include_history") == "true":
                        generations = record_store.list_preview_generation_records(
                            preview_id=preview.preview_id
                        )
                        return _json_response(
                            start_response=start_response,
                            status_code=200,
                            payload={
                                "status": "ok",
                                "trace_id": request_trace_id,
                                "preview": preview.model_dump(mode="json"),
                                "generations": [
                                    generation.model_dump(mode="json") for generation in generations
                                ],
                            },
                        )
                    return _json_response(
                        start_response=start_response,
                        status_code=200,
                        payload={
                            "status": "ok",
                            "trace_id": request_trace_id,
                            "record": preview.model_dump(mode="json"),
                        },
                    )
                if action == "secret.read":
                    secret_store = _secret_capable_store(record_store)
                    if secret_store is None:
                        return _json_response(
                            start_response=start_response,
                            status_code=404,
                            payload={
                                "status": "rejected",
                                "trace_id": request_trace_id,
                                "error": {
                                    "code": "not_found",
                                    "message": "Launchplane secret status routes require the Postgres storage backend.",
                                },
                            },
                        )
                    secret_status = control_plane_secrets.build_secret_status(
                        secret_store,
                        secret_id=params["secret_id"],
                    )
                    if not authz_policy.allows(
                        identity=identity,
                        action=action,
                        product="launchplane",
                        context=str(secret_status["context"]),
                    ):
                        return _json_response(
                            start_response=start_response,
                            status_code=403,
                            payload={
                                "status": "rejected",
                                "trace_id": request_trace_id,
                                "error": {
                                    "code": "authorization_denied",
                                    "message": "Workflow cannot read Launchplane managed secret status for the requested context.",
                                },
                            },
                        )
                    return _json_response(
                        start_response=start_response,
                        status_code=200,
                        payload={
                            "status": "ok",
                            "trace_id": request_trace_id,
                            "secret": secret_status,
                        },
                    )
                if action == "secret.list":
                    context_name = params["context"]
                    if not authz_policy.allows(
                        identity=identity,
                        action=action,
                        product="launchplane",
                        context=context_name,
                    ):
                        return _json_response(
                            start_response=start_response,
                            status_code=403,
                            payload={
                                "status": "rejected",
                                "trace_id": request_trace_id,
                                "error": {
                                    "code": "authorization_denied",
                                    "message": "Workflow cannot list Launchplane managed secret status for the requested context.",
                                },
                            },
                        )
                    secret_store = _secret_capable_store(record_store)
                    if secret_store is None:
                        return _json_response(
                            start_response=start_response,
                            status_code=404,
                            payload={
                                "status": "rejected",
                                "trace_id": request_trace_id,
                                "error": {
                                    "code": "not_found",
                                    "message": "Launchplane secret status routes require the Postgres storage backend.",
                                },
                            },
                        )
                    statuses = control_plane_secrets.list_secret_statuses(
                        secret_store,
                        context_name=context_name,
                        instance_name=params.get("instance", ""),
                    )
                    return _json_response(
                        start_response=start_response,
                        status_code=200,
                        payload={
                            "status": "ok",
                            "trace_id": request_trace_id,
                            "context": context_name,
                            "instance": params.get("instance", ""),
                            "secrets": statuses,
                        },
                    )
                if action == "launchplane_service.read":
                    if not authz_policy.allows(
                        identity=identity,
                        action=action,
                        product="launchplane",
                        context=_LAUNCHPLANE_SERVICE_CONTEXT,
                    ):
                        return _json_response(
                            start_response=start_response,
                            status_code=403,
                            payload={
                                "status": "rejected",
                                "trace_id": request_trace_id,
                                "error": {
                                    "code": "authorization_denied",
                                    "message": "Workflow cannot read Launchplane service runtime state.",
                                },
                            },
                        )
                    return _json_response(
                        start_response=start_response,
                        status_code=200,
                        payload={
                            "status": "ok",
                            "trace_id": request_trace_id,
                            "runtime": _launchplane_runtime_payload(
                                storage_backend=storage_backend
                            ),
                        },
                    )
                context_name = params["context"]
                if not authz_policy.allows(
                    identity=identity,
                    action=action,
                    product="launchplane",
                    context=context_name,
                ):
                    return _json_response(
                        start_response=start_response,
                        status_code=403,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "authorization_denied",
                                "message": "Workflow cannot read recent operations for the requested context.",
                            },
                        },
                    )
                deployments = record_store.list_deployment_records(
                    context_name=context_name, limit=10
                )
                promotions = record_store.list_promotion_records(
                    context_name=context_name, limit=10
                )
                previews = record_store.list_preview_records(context_name=context_name, limit=10)
                inventory = [
                    record
                    for record in record_store.list_environment_inventory()
                    if record.context == context_name
                ]
                return _json_response(
                    start_response=start_response,
                    status_code=200,
                    payload={
                        "status": "ok",
                        "trace_id": request_trace_id,
                        "context": context_name,
                        "storage_backend": storage_backend,
                        "inventory": [record.model_dump(mode="json") for record in inventory],
                        "recent_deployments": [
                            record.model_dump(mode="json") for record in deployments
                        ],
                        "recent_promotions": [
                            record.model_dump(mode="json") for record in promotions
                        ],
                        "recent_previews": [record.model_dump(mode="json") for record in previews],
                    },
                )
            payload = _read_json_request(environ)
            request_idempotency_key = _idempotency_key(environ)
            request_scope = _idempotency_scope(identity)
            request_fingerprint = _request_fingerprint(payload)
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
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                result = apply_deployment_evidence(
                    record_store=record_store,
                    deployment_record=request.deployment,
                )
            elif path == "/v1/evidence/backup-gates":
                request = BackupGateEvidenceEnvelope.model_validate(payload)
                if not authz_policy.allows(
                    identity=identity,
                    action="backup_gate.write",
                    product=request.product,
                    context=request.backup_gate.context,
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
                                    "Workflow cannot write backup gate evidence for the requested"
                                    " product/context."
                                ),
                            },
                        },
                    )
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                record_store.write_backup_gate_record(request.backup_gate)
                result = {"backup_gate_record_id": request.backup_gate.record_id}
            elif path == "/v1/drivers/launchplane/self-deploy":
                request = LaunchplaneSelfDeployEnvelope.model_validate(payload)
                if not authz_policy.allows(
                    identity=identity,
                    action="launchplane_service_deploy.execute",
                    product=request.product,
                    context=_LAUNCHPLANE_SERVICE_CONTEXT,
                ):
                    return _json_response(
                        start_response=start_response,
                        status_code=403,
                        payload={
                            "status": "rejected",
                            "trace_id": request_trace_id,
                            "error": {
                                "code": "authorization_denied",
                                "message": "Workflow cannot execute Launchplane self deploy.",
                            },
                        },
                    )
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                result = _request_launchplane_self_deploy(
                    control_plane_root_path=resolved_root,
                    request=request.deploy,
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
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                driver_result = execute_verireel_stable_deploy(
                    control_plane_root=resolved_root,
                    record_store=record_store,
                    request=request.deploy,
                )
                result = {"deployment_record_id": driver_result.deployment_record_id}
            elif path == "/v1/drivers/verireel/prod-deploy":
                request = VeriReelProdDeployEnvelope.model_validate(payload)
                if not authz_policy.allows(
                    identity=identity,
                    action="verireel_prod_deploy.execute",
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
                                    "Workflow cannot execute the VeriReel prod deploy driver"
                                    " for the requested product/context."
                                ),
                            },
                        },
                    )
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                driver_result = execute_verireel_stable_deploy(
                    control_plane_root=resolved_root,
                    record_store=record_store,
                    request=request.deploy,
                )
                result = {"deployment_record_id": driver_result.deployment_record_id}
            elif path == "/v1/drivers/verireel/prod-promotion":
                request = VeriReelProdPromotionEnvelope.model_validate(payload)
                if not authz_policy.allows(
                    identity=identity,
                    action="verireel_prod_promotion.execute",
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
                                    "Workflow cannot execute the VeriReel prod promotion driver"
                                    " for the requested product/context."
                                ),
                            },
                        },
                    )
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                driver_result = execute_verireel_prod_promotion(
                    control_plane_root=resolved_root,
                    record_store=record_store,
                    request=request.promotion,
                )
                result = {
                    "promotion_record_id": driver_result.promotion_record_id,
                    "deployment_record_id": driver_result.deployment_record_id,
                }
            elif path == "/v1/drivers/verireel/prod-rollback":
                request = VeriReelProdRollbackEnvelope.model_validate(payload)
                if not authz_policy.allows(
                    identity=identity,
                    action="verireel_prod_rollback.execute",
                    product=request.product,
                    context=request.rollback.context,
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
                                    "Workflow cannot execute the VeriReel prod rollback driver"
                                    " for the requested product/context."
                                ),
                            },
                        },
                    )
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                driver_result = execute_verireel_prod_rollback(
                    control_plane_root=resolved_root,
                    record_store=record_store,
                    request=request.rollback,
                )
                result = {
                    "promotion_record_id": driver_result.promotion_record_id,
                    "backup_record_id": driver_result.backup_record_id,
                }
            elif path == "/v1/drivers/verireel/preview-refresh":
                request = VeriReelPreviewRefreshEnvelope.model_validate(payload)
                if not authz_policy.allows(
                    identity=identity,
                    action="verireel_preview_refresh.execute",
                    product=request.product,
                    context=request.refresh.context,
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
                                    "Workflow cannot execute the VeriReel preview refresh driver"
                                    " for the requested product/context."
                                ),
                            },
                        },
                    )
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                driver_result = execute_verireel_preview_refresh(
                    control_plane_root=resolved_root,
                    request=request.refresh,
                )
                result = {}
            elif path == "/v1/drivers/verireel/preview-destroy":
                request = VeriReelPreviewDestroyEnvelope.model_validate(payload)
                if not authz_policy.allows(
                    identity=identity,
                    action="verireel_preview_destroy.execute",
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
                                    "Workflow cannot execute the VeriReel preview destroy driver"
                                    " for the requested product/context."
                                ),
                            },
                        },
                    )
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                driver_result = execute_verireel_preview_destroy(
                    control_plane_root=resolved_root,
                    request=request.destroy,
                )
                result = {}
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
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
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
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                result = apply_launchplane_generation_evidence(
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
                idempotent_response = _check_idempotent_request(
                    record_store=record_store,
                    scope=request_scope,
                    route_path=path,
                    idempotency_key=request_idempotency_key,
                    request_fingerprint=request_fingerprint,
                    start_response=start_response,
                    trace_id=request_trace_id,
                )
                if idempotent_response is not None:
                    return idempotent_response
                result = apply_launchplane_destroy_preview(
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
        except FileNotFoundError:
            return _not_found_response(
                start_response=start_response,
                trace_id=request_trace_id,
                path=path,
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
        accepted_payload = _accepted_payload(
            trace_id=request_trace_id,
            result=result,
            driver_result=driver_result,
        )
        if method == "POST" and request_idempotency_key:
            _write_idempotency_record(
                record_store=record_store,
                scope=request_scope,
                route_path=path,
                idempotency_key=request_idempotency_key,
                request_fingerprint=request_fingerprint,
                response_status_code=202,
                response_trace_id=request_trace_id,
                response_payload=accepted_payload,
            )
        return _json_response(
            start_response=start_response,
            status_code=202,
            payload=accepted_payload,
        )

    return app


def serve_launchplane_service(
    *,
    state_dir: Path,
    policy_file: Path,
    host: str,
    port: int,
    audience: str,
    database_url: str | None = None,
) -> None:
    from control_plane.service_auth import GitHubOidcVerifier

    authz_policy = load_authz_policy(policy_file)
    verifier = GitHubOidcVerifier(audience=audience)
    application = create_launchplane_service_app(
        state_dir=state_dir,
        verifier=verifier,
        authz_policy=authz_policy,
        database_url=database_url,
    )
    with make_server(host, port, application) as server:
        click.echo(f"Launchplane service listening on http://{host}:{port}")
        server.serve_forever()
