from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.dokploy import api as dokploy_api

DokployTargetType = Literal["application", "compose"]


class FetchDokployTargetPayload(Protocol):
    def __call__(
        self, *, host: str, token: str, target_type: str, target_id: str
    ) -> dokploy_api.JsonObject: ...


class DokployTargetInspectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str = "launchplane"
    context: str = ""
    instance: str = ""
    target_type: DokployTargetType | str = ""
    target_id: str = ""

    @model_validator(mode="after")
    def _validate_request(self) -> "DokployTargetInspectRequest":
        self.product = self.product.strip()
        self.context = self.context.strip()
        self.instance = self.instance.strip()
        self.target_type = self.target_type.strip().lower()
        self.target_id = self.target_id.strip()
        if self.product != "launchplane":
            raise ValueError("Dokploy target inspect requires product 'launchplane'.")
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


def inspect_dokploy_target(
    *,
    record_store: DokployTargetInspectStore,
    host: str,
    token: str,
    request: DokployTargetInspectRequest,
    fetch_target_payload: FetchDokployTargetPayload | None = None,
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
    provider_payload = payload_fetcher(
        host=host,
        token=token,
        target_type=target_type,
        target_id=target_id,
    )
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
    if value is None:
        return False
    if value == "":
        return False
    if value == []:
        return False
    if value == {}:
        return False
    return True


def _object_field(payload: Mapping[str, object], key: str) -> dict[str, object]:
    value = payload.get(key)
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _string_field(payload: Mapping[str, object], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value is None:
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
