from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Literal, Protocol

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.privileged_operation_worker_heartbeat import (
    PRIVILEGED_OPERATION_WORKER_HEARTBEAT_FUTURE_SKEW_SECONDS,
    PRIVILEGED_OPERATION_WORKER_KIND,
    PrivilegedOperationWorkerHeartbeatRecord,
    privileged_operation_worker_heartbeat_freshness_seconds,
)
from control_plane.dokploy import api as dokploy_api
from control_plane.dokploy import runtime_evidence as dokploy_runtime_evidence

DokployTargetType = Literal["application", "compose"]
_RUNTIME_EVIDENCE_LOG_LINE_COUNT = dokploy_api.MAX_DOKPLOY_LOG_LINE_COUNT
_MAX_WORKER_HEARTBEAT_RECORDS = 100


class FetchDokployTargetPayload(Protocol):
    def __call__(
        self, *, host: str, token: str, target_type: str, target_id: str
    ) -> dokploy_api.JsonObject: ...


class FetchDokployComposeServiceRuntime(Protocol):
    def __call__(
        self,
        *,
        host: str,
        token: str,
        compose_id: str,
        app_name: str,
        server_id: str,
        service_name: str,
    ) -> dokploy_api.JsonObject: ...


class FetchDokployComposeLogs(Protocol):
    def __call__(
        self,
        *,
        host: str,
        token: str,
        compose_id: str,
        container_id: str,
        line_count: int,
        search_text: str,
    ) -> tuple[str, ...]: ...


class DokployTargetInspectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str = "launchplane"
    context: str = ""
    instance: str = ""
    target_type: DokployTargetType | str = ""
    target_id: str = ""
    service: str = ""
    event: str = ""
    expected_image: str = ""

    @model_validator(mode="after")
    def _validate_request(self) -> "DokployTargetInspectRequest":
        self.product = self.product.strip()
        self.context = self.context.strip()
        self.instance = self.instance.strip()
        self.target_type = self.target_type.strip().lower()
        self.target_id = self.target_id.strip()
        try:
            self.service = dokploy_api.normalize_dokploy_compose_service_name(self.service)
            self.event = dokploy_runtime_evidence.normalize_structured_event_name(self.event)
            self.expected_image = dokploy_runtime_evidence.normalize_expected_image_reference(
                self.expected_image
            )
        except click.ClickException as error:
            raise ValueError(str(error)) from error
        if self.product != "launchplane":
            raise ValueError("Dokploy target inspect requires product 'launchplane'.")
        if self.event and not self.service:
            raise ValueError("Dokploy runtime event evidence requires a compose service.")
        if self.expected_image and not self.service:
            raise ValueError("Dokploy expected image evidence requires a compose service.")
        if self.service and not self.expected_image:
            raise ValueError("Dokploy runtime service evidence requires an expected image.")
        has_route = bool(self.context or self.instance)
        has_explicit_target = bool(self.target_type or self.target_id)
        if has_route and has_explicit_target:
            raise ValueError(
                "Dokploy target inspect accepts either context/instance or target_type/target_id, not both."
            )
        if has_route:
            if not self.context or not self.instance:
                raise ValueError("Tracked Dokploy target inspect requires context and instance.")
            return self
        if not self.target_type or not self.target_id:
            raise ValueError("Explicit Dokploy target inspect requires target_type and target_id.")
        if self.target_type not in {"application", "compose"}:
            raise ValueError("Dokploy target inspect target_type must be application or compose.")
        return self


class DokployTargetInspectStore(Protocol):
    def read_dokploy_target_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetRecord: ...

    def read_dokploy_target_id_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetIdRecord: ...

    def read_provider_target_record(
        self, *, context_name: str, instance_name: str
    ) -> ProviderTargetRecord: ...

    def list_privileged_operation_worker_heartbeat_records(
        self,
        *,
        worker_kind: str = "",
        limit: int | None = None,
    ) -> tuple[PrivilegedOperationWorkerHeartbeatRecord, ...]: ...


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _worker_heartbeat_evidence(
    *,
    records: tuple[PrivilegedOperationWorkerHeartbeatRecord, ...],
    container_identity_sha256: str,
    immutable_image_reference: str,
    now: datetime,
) -> dict[str, object]:
    bounded_records = records[:_MAX_WORKER_HEARTBEAT_RECORDS]
    fresh_records: list[PrivilegedOperationWorkerHeartbeatRecord] = []
    matching_record: PrivilegedOperationWorkerHeartbeatRecord | None = None
    matching_age_seconds = 0
    matching_freshness_limit_seconds = 0
    matching_last_poll_succeeded_at = ""
    matching_future_skew_detected = False

    for record in bounded_records:
        recorded_at = datetime.fromisoformat(record.last_poll_succeeded_at).astimezone(timezone.utc)
        age_seconds = (now - recorded_at).total_seconds()
        freshness_limit_seconds = privileged_operation_worker_heartbeat_freshness_seconds(
            record.poll_interval_seconds
        )
        future_skew_detected = (
            age_seconds < -PRIVILEGED_OPERATION_WORKER_HEARTBEAT_FUTURE_SKEW_SECONDS
        )
        if not future_skew_detected and age_seconds <= freshness_limit_seconds:
            fresh_records.append(record)
        if record.worker_identity_sha256 != container_identity_sha256:
            continue
        if matching_record is not None:
            continue
        matching_record = record
        matching_age_seconds = max(-86_400, min(86_400, int(age_seconds)))
        matching_freshness_limit_seconds = freshness_limit_seconds
        matching_last_poll_succeeded_at = record.last_poll_succeeded_at
        matching_future_skew_detected = future_skew_detected

    matching_fresh = bool(matching_record is not None and matching_record in fresh_records)
    image_matches = bool(
        matching_record is not None
        and matching_record.image_reference
        and matching_record.image_reference == immutable_image_reference
    )
    identity_consistent = matching_record is not None

    if not bounded_records:
        status = "missing"
    elif matching_record is None:
        status = "identity_mismatch"
    elif matching_future_skew_detected:
        status = "future_timestamp"
    elif not matching_fresh:
        status = "stale"
    elif not image_matches:
        status = "image_mismatch"
    else:
        status = "ready"

    return {
        "source": "launchplane_records",
        "status": status,
        "observed": bool(bounded_records),
        "matching_identity_observed": matching_record is not None,
        "fresh": matching_fresh,
        "identity_consistent": identity_consistent,
        "image_matches": image_matches,
        "fresh_worker_count": min(len(fresh_records), _MAX_WORKER_HEARTBEAT_RECORDS),
        "age_seconds": matching_age_seconds,
        "freshness_limit_seconds": matching_freshness_limit_seconds,
        "last_poll_succeeded_at": matching_last_poll_succeeded_at,
        "future_skew_detected": matching_future_skew_detected,
    }


def inspect_dokploy_target(
    *,
    record_store: DokployTargetInspectStore,
    host: str,
    token: str,
    request: DokployTargetInspectRequest,
    fetch_target_payload: FetchDokployTargetPayload | None = None,
    fetch_compose_service_runtime: FetchDokployComposeServiceRuntime | None = None,
    fetch_compose_logs: FetchDokployComposeLogs | None = None,
    now: Callable[[], datetime] = _utc_now,
) -> dict[str, object]:
    target_record: DokployTargetRecord | None = None
    target_id_record: DokployTargetIdRecord | None = None
    provider_target_record: ProviderTargetRecord | None = None

    target_type: DokployTargetType
    if request.target_type == "application":
        target_type = "application"
    else:
        target_type = "compose"
    target_id = request.target_id
    if request.context and request.instance:
        target_record = record_store.read_dokploy_target_record(
            context_name=request.context,
            instance_name=request.instance,
        )
        target_id_record = record_store.read_dokploy_target_id_record(
            context_name=request.context,
            instance_name=request.instance,
        )
        try:
            provider_target_record = record_store.read_provider_target_record(
                context_name=request.context,
                instance_name=request.instance,
            )
        except FileNotFoundError:
            provider_target_record = None
        target_type = target_record.target_type
        target_id = target_id_record.target_id

    payload_fetcher = fetch_target_payload or dokploy_api.fetch_dokploy_target_payload
    try:
        provider_payload = payload_fetcher(
            host=host,
            token=token,
            target_type=target_type,
            target_id=target_id,
        )
    except click.ClickException as error:
        raise dokploy_runtime_evidence.DokployEvidenceProviderError("target-inspect") from error
    provider_summary = summarize_dokploy_target_payload(
        target_type=target_type,
        target_id=target_id,
        payload=provider_payload,
    )
    result: dict[str, object] = {
        "status": "ok",
        "target_type": target_type,
        "target_id": target_id,
        "provider_payload_redacted": True,
        "provider": provider_summary,
    }
    if request.service:
        if target_type != "compose":
            raise ValueError("Dokploy runtime service evidence supports compose targets only.")
        app_name = str(provider_payload.get("appName") or "").strip()
        server_id = str(provider_payload.get("serverId") or "").strip()
        runtime_fetcher = (
            fetch_compose_service_runtime or dokploy_runtime_evidence.fetch_compose_service_runtime
        )
        runtime_payload = runtime_fetcher(
            host=host,
            token=token,
            compose_id=target_id,
            app_name=app_name,
            server_id=server_id,
            service_name=request.service,
        )
        container_id = str(runtime_payload.get("container_id") or "").strip()
        if not container_id:
            raise dokploy_runtime_evidence.DokployEvidenceProviderError("service-select")
        container_identity_sha256 = str(
            runtime_payload.get("container_identity_sha256") or ""
        ).strip()
        if not container_identity_sha256:
            raise dokploy_runtime_evidence.DokployEvidenceProviderError("container-identity")
        logs_fetcher = fetch_compose_logs or dokploy_runtime_evidence.fetch_compose_container_logs
        log_read_status = "available"
        try:
            classification_logs = logs_fetcher(
                host=host,
                token=token,
                compose_id=target_id,
                container_id=container_id,
                line_count=_RUNTIME_EVIDENCE_LOG_LINE_COUNT,
                search_text="",
            )
            event_logs = (
                logs_fetcher(
                    host=host,
                    token=token,
                    compose_id=target_id,
                    container_id=container_id,
                    line_count=_RUNTIME_EVIDENCE_LOG_LINE_COUNT,
                    search_text=request.event,
                )
                if request.event
                else ()
            )
        except dokploy_runtime_evidence.DokployEvidenceProviderError as error:
            if error.operation != "runtime-log-read":
                raise
            classification_logs = ()
            event_logs = ()
            log_read_status = "unavailable"
        matching_event_count = dokploy_runtime_evidence.count_structured_log_events(
            event_logs,
            event_name=request.event,
        )
        event_observed = matching_event_count > 0
        activation_proof_eligible = dokploy_runtime_evidence.structured_event_is_activation_proof(
            request.event
        )
        log_classification = dokploy_runtime_evidence.summarize_runtime_log_lines(
            classification_logs
        )
        event_evidence: dict[str, object] = {
            "name": request.event,
            "observed": event_observed,
            "activation_proof_eligible": activation_proof_eligible,
            "matching_line_count": matching_event_count,
            "candidate_line_count": len(event_logs),
            "log_read_status": log_read_status,
            "log_classification": log_classification,
        }
        running = runtime_payload.get("running") is True
        image_reference_immutable = runtime_payload.get("image_reference_immutable") is True
        immutable_image_reference = str(runtime_payload.get("immutable_image_reference") or "")
        image_matches_expected = (
            not request.expected_image or immutable_image_reference == request.expected_image
        )
        heartbeat_records = record_store.list_privileged_operation_worker_heartbeat_records(
            worker_kind=PRIVILEGED_OPERATION_WORKER_KIND,
            limit=_MAX_WORKER_HEARTBEAT_RECORDS,
        )
        worker_heartbeat = _worker_heartbeat_evidence(
            records=heartbeat_records,
            container_identity_sha256=container_identity_sha256,
            immutable_image_reference=immutable_image_reference,
            now=now().astimezone(timezone.utc),
        )
        result["runtime_evidence"] = {
            "service": request.service,
            "state": str(runtime_payload.get("state") or ""),
            "status": str(runtime_payload.get("status") or ""),
            "running": running,
            "image_id": str(runtime_payload.get("image_id") or ""),
            "immutable_image_reference": immutable_image_reference,
            "image_reference_immutable": image_reference_immutable,
            "expected_image": request.expected_image,
            "image_matches_expected": image_matches_expected,
            "structured_event": event_evidence,
            "worker_heartbeat": worker_heartbeat,
            "proof_source": "worker_heartbeat_record",
            "proof_ready": (
                running
                and image_reference_immutable
                and image_matches_expected
                and worker_heartbeat["status"] == "ready"
            ),
        }
    if target_record is not None and target_id_record is not None:
        result["tracked_target"] = {
            "context": target_record.context,
            "instance": target_record.instance,
            "target_type": target_record.target_type,
            "target_name": target_record.target_name,
            "target_id": target_id_record.target_id,
            "project_name": target_record.project_name,
            "source_git_ref": target_record.source_git_ref,
            "source_type": target_record.source_type,
            "compose_path": target_record.compose_path,
            "domains": list(target_record.domains),
            "healthcheck_path": target_record.healthcheck_path,
            "deploy_timeout_seconds": target_record.deploy_timeout_seconds,
            "source_label": target_record.source_label,
            "updated_at": target_record.updated_at,
        }
        if provider_target_record is None:
            result["provider_target_record"] = {"status": "missing"}
        else:
            result["provider_target_record"] = {
                "status": "present",
                "provider_id": provider_target_record.provider_id,
                "target_category": provider_target_record.target_category,
                "target_id": provider_target_record.target_id,
                "display_name": provider_target_record.display_name,
                "provider_target_type": provider_target_record.provider_target_type,
                "source_label": provider_target_record.source_label,
                "updated_at": provider_target_record.updated_at,
            }
    return result


def summarize_dokploy_target_payload(
    *, target_type: str, target_id: str, payload: Mapping[str, object]
) -> dict[str, object]:
    summary: dict[str, object] = {
        "target_type": target_type,
        "target_id": target_id,
        "id": _string_field(payload, "id", "uuid", "applicationId", "composeId"),
        "name": _string_field(payload, "name"),
        "app_name": _string_field(payload, "appName"),
        "server_id": _string_field(payload, "serverId"),
        "source_type": _string_field(payload, "sourceType"),
        "compose_path": _string_field(payload, "composePath"),
        "description_present": bool(_string_field(payload, "description")),
        "domains": _domain_summaries(payload),
        "environment": _environment_summary(payload),
        "env": _env_summary(payload),
    }
    return {key: value for key, value in summary.items() if _present(value)}


def _environment_summary(payload: Mapping[str, object]) -> dict[str, object]:
    environment = _object_field(payload, "environment")
    project = _object_field(environment, "project")
    summary = {
        "id": _string_field(payload, "environmentId") or _string_field(environment, "id", "uuid"),
        "name": _string_field(environment, "name"),
        "project_id": _string_field(payload, "projectId") or _string_field(project, "id", "uuid"),
        "project_name": _string_field(project, "name"),
    }
    return {key: value for key, value in summary.items() if _present(value)}


def _env_summary(payload: Mapping[str, object]) -> dict[str, object]:
    env_object = _object_field(payload, "env") or _object_field(payload, "environmentVariables")
    if env_object:
        return {
            "format": "object",
            "key_count": len(env_object),
            "keys": sorted(str(key) for key in env_object),
            "values_redacted": True,
        }
    env_value = _string_field(payload, "env")
    if not env_value:
        return {}
    keys = []
    for line in env_value.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        keys.append(stripped.split("=", 1)[0].strip())
    return {
        "format": "dotenv",
        "key_count": len(keys),
        "keys": sorted(key for key in keys if key),
        "values_redacted": True,
    }


def _domain_summaries(payload: Mapping[str, object]) -> list[dict[str, object]]:
    raw_domains = payload.get("domains") or payload.get("applicationDomains") or []
    if not isinstance(raw_domains, list):
        return []
    summaries: list[dict[str, object]] = []
    for raw_domain in raw_domains:
        if not isinstance(raw_domain, Mapping):
            continue
        summary = {
            "id": _string_field(raw_domain, "id", "uuid", "domainId"),
            "host": _string_field(raw_domain, "host", "domain", "domainName"),
            "port": _int_field(raw_domain, "port", "targetPort"),
            "https": _bool_field(raw_domain, "https", "secure"),
        }
        summaries.append({key: value for key, value in summary.items() if _present(value)})
    return summaries


def _present(value: object) -> bool:
    return value not in (None, "", [], {})


def _object_field(payload: Mapping[str, object], key: str) -> dict[str, object]:
    value = payload.get(key)
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _string_field(payload: Mapping[str, object], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if not isinstance(value, (str, int, float, bool)):
            continue
        normalized_value = str(value).strip()
        if normalized_value:
            return normalized_value
    return ""


def _int_field(payload: Mapping[str, object], *keys: str) -> int | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return None


def _bool_field(payload: Mapping[str, object], *keys: str) -> bool | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
            return value.strip().lower() == "true"
    return None
