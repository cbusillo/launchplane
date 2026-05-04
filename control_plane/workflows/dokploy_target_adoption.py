from collections.abc import Callable
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord, DokployTargetType
from control_plane.dokploy import JsonObject
from control_plane.workflows.ship import utc_now_timestamp


class DokployTargetAdoptionRecordStore(Protocol):
    def write_dokploy_target_record(self, record: DokployTargetRecord) -> None: ...

    def write_dokploy_target_id_record(self, record: DokployTargetIdRecord) -> None: ...


class DokployTargetAdoptionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    applied: bool
    target_record: DokployTargetRecord
    target_id_record: DokployTargetIdRecord
    provider_fields: dict[str, str] = {}
    warnings: tuple[str, ...] = ()


FetchDokployTargetPayload = Callable[[str, str, DokployTargetType, str], JsonObject]


def adopt_dokploy_target(
    *,
    record_store: DokployTargetAdoptionRecordStore,
    host: str,
    token: str,
    context: str,
    instance: str,
    target_type: DokployTargetType,
    target_id: str,
    project_name: str = "",
    target_name: str = "",
    source_git_ref: str = "origin/main",
    healthcheck_path: str = "",
    domains: tuple[str, ...] = (),
    deploy_timeout_seconds: int | None = None,
    source_label: str = "cli:dokploy-targets:adopt",
    updated_at: str = "",
    apply: bool = False,
    fetch_target_payload: FetchDokployTargetPayload,
) -> DokployTargetAdoptionResult:
    normalized_context = _require_non_empty(context, "context")
    normalized_instance = _require_non_empty(instance, "instance")
    normalized_target_id = _require_non_empty(target_id, "target_id")
    normalized_source_git_ref = source_git_ref.strip() or "origin/main"
    recorded_at = updated_at.strip() or utc_now_timestamp()
    provider_payload = fetch_target_payload(host, token, target_type, normalized_target_id)
    provider_fields = _provider_fields(provider_payload=provider_payload, target_type=target_type)
    resolved_target_name = target_name.strip() or provider_fields.get("target_name", "")
    resolved_project_name = project_name.strip() or provider_fields.get("project_name", "")
    resolved_healthcheck_path = healthcheck_path.strip()
    if target_type == "compose" and not resolved_healthcheck_path:
        resolved_healthcheck_path = "/web/health"

    warnings = _adoption_warnings(
        target_type=target_type,
        provider_payload=provider_payload,
        target_name=resolved_target_name,
        project_name=resolved_project_name,
    )
    target_record = DokployTargetRecord(
        context=normalized_context,
        instance=normalized_instance,
        project_name=resolved_project_name,
        target_type=target_type,
        target_name=resolved_target_name,
        source_git_ref=normalized_source_git_ref,
        source_type=provider_fields.get("source_type", ""),
        custom_git_url=provider_fields.get("custom_git_url", ""),
        custom_git_branch=provider_fields.get("custom_git_branch", ""),
        compose_path=provider_fields.get("compose_path", ""),
        deploy_timeout_seconds=deploy_timeout_seconds,
        healthcheck_path=resolved_healthcheck_path,
        domains=domains,
        updated_at=recorded_at,
        source_label=source_label.strip() or "cli:dokploy-targets:adopt",
    )
    target_id_record = DokployTargetIdRecord(
        context=normalized_context,
        instance=normalized_instance,
        target_id=normalized_target_id,
        updated_at=recorded_at,
        source_label=source_label.strip() or "cli:dokploy-targets:adopt",
    )

    if apply:
        record_store.write_dokploy_target_record(target_record)
        record_store.write_dokploy_target_id_record(target_id_record)

    return DokployTargetAdoptionResult(
        applied=apply,
        target_record=target_record,
        target_id_record=target_id_record,
        provider_fields=provider_fields,
        warnings=warnings,
    )


def _require_non_empty(value: str, field_name: str) -> str:
    normalized_value = value.strip()
    if not normalized_value:
        raise ValueError(f"Dokploy target adoption requires {field_name}")
    return normalized_value


def _provider_fields(
    *, provider_payload: JsonObject, target_type: DokployTargetType
) -> dict[str, str]:
    fields: dict[str, str] = {}
    name = _string_field(provider_payload, "name")
    if name:
        fields["target_name"] = name
    project_name = _nested_string_field(provider_payload, ("project",), "name")
    if project_name:
        fields["project_name"] = project_name
    environment_project_name = _nested_string_field(
        provider_payload, ("environment", "project"), "name"
    )
    if environment_project_name:
        fields["project_name"] = environment_project_name

    if target_type == "compose":
        _put_if_present(fields, "source_type", _string_field(provider_payload, "sourceType"))
        _put_if_present(fields, "custom_git_url", _string_field(provider_payload, "customGitUrl"))
        _put_if_present(
            fields, "custom_git_branch", _string_field(provider_payload, "customGitBranch")
        )
        _put_if_present(fields, "compose_path", _string_field(provider_payload, "composePath"))
    return fields


def _adoption_warnings(
    *,
    target_type: DokployTargetType,
    provider_payload: JsonObject,
    target_name: str,
    project_name: str,
) -> tuple[str, ...]:
    warnings: list[str] = []
    if not target_name:
        warnings.append("provider target name was not discovered; pass --target-name if needed")
    if not project_name:
        warnings.append("provider project name was not discovered; pass --project-name if needed")
    if target_type == "application":
        env_text = _string_field(provider_payload, "env")
        if env_text:
            warnings.append(
                "provider env exists but was not copied; store runtime values through Launchplane runtime/secret records"
            )
    return tuple(warnings)


def _put_if_present(fields: dict[str, str], key: str, value: str) -> None:
    if value:
        fields[key] = value


def _string_field(payload: JsonObject, key: str) -> str:
    value = payload.get(key)
    if value is None:
        return ""
    return str(value).strip()


def _nested_string_field(payload: JsonObject, path: tuple[str, ...], key: str) -> str:
    current: object = payload
    for path_key in path:
        if not isinstance(current, dict):
            return ""
        current = current.get(path_key)
    if not isinstance(current, dict):
        return ""
    value = current.get(key)
    if value is None:
        return ""
    return str(value).strip()
