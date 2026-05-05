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
    provider_fields: dict[str, str | tuple[str, ...] | bool | int | None] = {}
    warnings: tuple[str, ...] = ()


FetchDokployTargetPayload = Callable[[str, str, DokployTargetType, str], JsonObject]
MutateDokployPayload = Callable[[str, str, str, JsonObject], JsonObject]


class DokployTargetCreatePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: dict[str, str]
    environment: dict[str, str]
    application: dict[str, str]


class DokployTargetCreateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    applied: bool
    plan: DokployTargetCreatePlan
    project_id: str = ""
    environment_id: str = ""
    target_record: DokployTargetRecord
    target_id_record: DokployTargetIdRecord
    provider_fields: dict[str, str | tuple[str, ...] | bool | int | None] = {}
    provider_requests: tuple[dict[str, object], ...] = ()
    warnings: tuple[str, ...] = ()


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
    normalized_context = _normalize_route_part(context, "context")
    normalized_instance = _normalize_route_part(instance, "instance")
    normalized_target_id = _require_non_empty(target_id, "target_id")
    recorded_at = updated_at.strip() or utc_now_timestamp()
    provider_payload = fetch_target_payload(host, token, target_type, normalized_target_id)
    provider_fields = _provider_fields(provider_payload=provider_payload, target_type=target_type)
    normalized_source_git_ref = _resolved_source_git_ref(
        explicit_source_git_ref=source_git_ref,
        provider_fields=provider_fields,
    )
    resolved_target_name = target_name.strip() or _provider_string_field(
        provider_fields, "target_name"
    )
    resolved_project_name = project_name.strip() or _provider_string_field(
        provider_fields, "project_name"
    )
    resolved_healthcheck_path = healthcheck_path.strip()
    if target_type == "compose" and not resolved_healthcheck_path:
        resolved_healthcheck_path = "/web/health"
    if resolved_healthcheck_path and not resolved_healthcheck_path.startswith("/"):
        raise ValueError("Dokploy target adoption requires --healthcheck-path to start with /")

    warnings = _adoption_warnings(
        target_type=target_type,
        provider_payload=provider_payload,
        target_name=resolved_target_name,
        project_name=resolved_project_name,
    )
    healthcheck_enabled = _bool_field(provider_payload, "healthcheckEnabled")
    if healthcheck_enabled is None:
        healthcheck_enabled = True
    target_record = DokployTargetRecord(
        context=normalized_context,
        instance=normalized_instance,
        project_name=resolved_project_name,
        target_type=target_type,
        target_name=resolved_target_name,
        source_git_ref=normalized_source_git_ref,
        git_branch=_provider_string_field(provider_fields, "git_branch")
        or _provider_string_field(provider_fields, "branch"),
        source_type=_provider_string_field(provider_fields, "source_type"),
        custom_git_url=_provider_string_field(provider_fields, "custom_git_url"),
        custom_git_branch=_provider_string_field(provider_fields, "custom_git_branch"),
        compose_path=_provider_string_field(provider_fields, "compose_path"),
        watch_paths=_tuple_string_field(provider_fields.get("watch_paths")),
        enable_submodules=_optional_bool_field(provider_fields.get("enable_submodules")),
        deploy_timeout_seconds=deploy_timeout_seconds,
        healthcheck_enabled=healthcheck_enabled,
        healthcheck_path=resolved_healthcheck_path,
        healthcheck_timeout_seconds=_int_field(provider_payload, "healthcheckTimeoutSeconds"),
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


def create_dokploy_application_target(
    *,
    record_store: DokployTargetAdoptionRecordStore,
    host: str,
    token: str,
    context: str,
    instance: str,
    target_name: str,
    project_id: str = "",
    project_name: str = "",
    project_description: str = "",
    environment_id: str = "",
    environment_name: str = "",
    environment_description: str = "",
    server_id: str = "",
    app_name: str = "",
    application_description: str = "",
    source_git_ref: str = "origin/main",
    healthcheck_path: str = "",
    domains: tuple[str, ...] = (),
    deploy_timeout_seconds: int | None = None,
    source_label: str = "cli:dokploy-targets:create-application",
    updated_at: str = "",
    apply: bool = False,
    mutate_provider: MutateDokployPayload,
    fetch_target_payload: FetchDokployTargetPayload,
) -> DokployTargetCreateResult:
    normalized_context = _normalize_route_part(context, "context")
    normalized_instance = _normalize_route_part(instance, "instance")
    normalized_target_name = _require_non_empty(target_name, "target_name")
    normalized_project_id = project_id.strip()
    normalized_project_name = project_name.strip()
    normalized_environment_id = environment_id.strip()
    normalized_environment_name = environment_name.strip() or normalized_instance
    normalized_healthcheck_path = healthcheck_path.strip()
    if normalized_healthcheck_path and not normalized_healthcheck_path.startswith("/"):
        raise ValueError("Dokploy target creation requires --healthcheck-path to start with /")
    if not normalized_environment_id and not normalized_project_id and not normalized_project_name:
        raise ValueError(
            "Dokploy target creation requires --project-id or --project-name when --environment-id is not supplied"
        )

    provider_requests = _planned_provider_requests(
        project_id=normalized_project_id,
        project_name=normalized_project_name,
        project_description=project_description,
        environment_id=normalized_environment_id,
        environment_name=normalized_environment_name,
        environment_description=environment_description,
        target_name=normalized_target_name,
        app_name=app_name,
        application_description=application_description,
        server_id=server_id,
    )
    plan = DokployTargetCreatePlan(
        project={
            "action": _project_plan_action(
                project_id=normalized_project_id,
                environment_id=normalized_environment_id,
            ),
            "project_id": normalized_project_id,
            "project_name": normalized_project_name,
        },
        environment={
            "action": "use_existing" if normalized_environment_id else "create",
            "environment_id": normalized_environment_id,
            "environment_name": normalized_environment_name,
        },
        application={
            "action": "create",
            "target_name": normalized_target_name,
            "app_name": app_name.strip(),
            "server_id": server_id.strip(),
        },
    )

    if not apply:
        target_record = DokployTargetRecord(
            context=normalized_context,
            instance=normalized_instance,
            project_name=normalized_project_name,
            target_type="application",
            target_name=normalized_target_name,
            source_git_ref=source_git_ref.strip() or "origin/main",
            deploy_timeout_seconds=deploy_timeout_seconds,
            healthcheck_path=normalized_healthcheck_path,
            domains=domains,
            updated_at=updated_at.strip() or utc_now_timestamp(),
            source_label=source_label.strip() or "cli:dokploy-targets:create-application",
        )
        target_id_record = DokployTargetIdRecord(
            context=normalized_context,
            instance=normalized_instance,
            target_id="planned-application-id",
            updated_at=target_record.updated_at,
            source_label=target_record.source_label,
        )
        return DokployTargetCreateResult(
            applied=False,
            plan=plan,
            target_record=target_record,
            target_id_record=target_id_record,
            provider_requests=provider_requests,
            warnings=("dry run only; provider was not mutated and records were not written",),
        )

    created_project_id = normalized_project_id
    if not created_project_id and not normalized_environment_id:
        project_payload: JsonObject = {"name": normalized_project_name}
        if project_description.strip():
            project_payload["description"] = project_description.strip()
        created_project = mutate_provider(host, token, "/api/project.create", project_payload)
        created_project_id = _extract_provider_id(created_project, "projectId", "project")

    created_environment_id = normalized_environment_id
    if not created_environment_id:
        environment_payload: JsonObject = {
            "name": normalized_environment_name,
            "projectId": created_project_id,
        }
        if environment_description.strip():
            environment_payload["description"] = environment_description.strip()
        created_environment = mutate_provider(
            host, token, "/api/environment.create", environment_payload
        )
        created_environment_id = _extract_provider_id(
            created_environment, "environmentId", "environment"
        )

    application_payload: JsonObject = {
        "name": normalized_target_name,
        "environmentId": created_environment_id,
    }
    if app_name.strip():
        application_payload["appName"] = app_name.strip()
    if application_description.strip():
        application_payload["description"] = application_description.strip()
    if server_id.strip():
        application_payload["serverId"] = server_id.strip()
    created_application = mutate_provider(
        host, token, "/api/application.create", application_payload
    )
    application_id = _extract_provider_id(created_application, "applicationId", "application")

    adoption = adopt_dokploy_target(
        record_store=record_store,
        host=host,
        token=token,
        context=normalized_context,
        instance=normalized_instance,
        target_type="application",
        target_id=application_id,
        project_name=normalized_project_name,
        target_name=normalized_target_name,
        source_git_ref=source_git_ref,
        healthcheck_path=normalized_healthcheck_path,
        domains=domains,
        deploy_timeout_seconds=deploy_timeout_seconds,
        source_label=source_label,
        updated_at=updated_at,
        apply=True,
        fetch_target_payload=fetch_target_payload,
    )
    return DokployTargetCreateResult(
        applied=True,
        plan=plan,
        project_id=created_project_id,
        environment_id=created_environment_id,
        target_record=adoption.target_record,
        target_id_record=adoption.target_id_record,
        provider_fields=adoption.provider_fields,
        provider_requests=provider_requests,
        warnings=adoption.warnings,
    )


def _require_non_empty(value: str, field_name: str) -> str:
    normalized_value = value.strip()
    if not normalized_value:
        raise ValueError(f"Dokploy target adoption requires {field_name}")
    return normalized_value


def _normalize_route_part(value: str, field_name: str) -> str:
    return _require_non_empty(value, field_name).lower()


def _resolved_source_git_ref(
    *,
    explicit_source_git_ref: str,
    provider_fields: dict[str, str | tuple[str, ...] | bool | int | None],
) -> str:
    resolved_source_git_ref = explicit_source_git_ref.strip()
    if resolved_source_git_ref:
        return resolved_source_git_ref
    provider_source_git_ref = str(provider_fields.get("source_git_ref") or "").strip()
    if provider_source_git_ref:
        return provider_source_git_ref
    provider_branch = str(provider_fields.get("branch") or "").strip()
    if provider_branch:
        return provider_branch
    return "origin/main"


def _provider_string_field(
    provider_fields: dict[str, str | tuple[str, ...] | bool | int | None], key: str
) -> str:
    value = provider_fields.get(key)
    if isinstance(value, str):
        return value
    return ""


def _planned_provider_requests(
    *,
    project_id: str,
    project_name: str,
    project_description: str,
    environment_id: str,
    environment_name: str,
    environment_description: str,
    target_name: str,
    app_name: str,
    application_description: str,
    server_id: str,
) -> tuple[dict[str, object], ...]:
    requests: list[dict[str, object]] = []
    if not environment_id and not project_id:
        project_payload: dict[str, object] = {"name": project_name}
        if project_description.strip():
            project_payload["description"] = project_description.strip()
        requests.append({"path": "/api/project.create", "payload": project_payload})
    if not environment_id:
        environment_payload: dict[str, object] = {
            "name": environment_name,
            "projectId": project_id or "<created-project-id>",
        }
        if environment_description.strip():
            environment_payload["description"] = environment_description.strip()
        requests.append({"path": "/api/environment.create", "payload": environment_payload})
    application_payload: dict[str, object] = {
        "name": target_name,
        "environmentId": environment_id or "<created-environment-id>",
    }
    if app_name.strip():
        application_payload["appName"] = app_name.strip()
    if application_description.strip():
        application_payload["description"] = application_description.strip()
    if server_id.strip():
        application_payload["serverId"] = server_id.strip()
    requests.append({"path": "/api/application.create", "payload": application_payload})
    return tuple(requests)


def _project_plan_action(*, project_id: str, environment_id: str) -> str:
    if project_id:
        return "use_existing"
    if environment_id:
        return "not_required"
    return "create"


def _extract_provider_id(payload: JsonObject, id_key: str, object_key: str) -> str:
    direct_id = _string_field(payload, id_key) or _string_field(payload, "id")
    if direct_id:
        return direct_id
    nested_id = _nested_string_field(payload, (object_key,), id_key) or _nested_string_field(
        payload, (object_key,), "id"
    )
    if nested_id:
        return nested_id
    raise ValueError(f"Dokploy {object_key}.create did not return {id_key}")


def _provider_fields(
    *, provider_payload: JsonObject, target_type: DokployTargetType
) -> dict[str, str | tuple[str, ...] | bool | int | None]:
    fields: dict[str, str | tuple[str, ...] | bool | int | None] = {}
    name = _string_field(provider_payload, "name")
    if name:
        fields["target_name"] = name
    _put_if_present(fields, "source_git_ref", _string_field(provider_payload, "sourceGitRef"))
    _put_if_present(fields, "branch", _string_field(provider_payload, "branch"))
    _put_if_present(fields, "git_branch", _string_field(provider_payload, "gitBranch"))
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
        _put_if_present(
            fields, "custom_git_build_path", _string_field(provider_payload, "customGitBuildPath")
        )
        _put_if_present(
            fields, "custom_git_ssh_key_id", _string_field(provider_payload, "customGitSSHKeyId")
        )
        _put_if_present(fields, "compose_path", _string_field(provider_payload, "composePath"))
        watch_paths = _string_list_field(provider_payload, "watchPaths")
        if watch_paths:
            fields["watch_paths"] = watch_paths
        enable_submodules = _bool_field(provider_payload, "enableSubmodules")
        if enable_submodules is not None:
            fields["enable_submodules"] = enable_submodules
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


def _put_if_present(
    fields: dict[str, str | tuple[str, ...] | bool | int | None], key: str, value: str
) -> None:
    if value:
        fields[key] = value


def _string_list_field(payload: JsonObject, key: str) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, list):
        return ()
    items: list[str] = []
    for raw_item in value:
        item = str(raw_item).strip()
        if item:
            items.append(item)
    return tuple(items)


def _tuple_string_field(value: object) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _optional_bool_field(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _bool_field(payload: JsonObject, key: str) -> bool | None:
    value = payload.get(key)
    if isinstance(value, bool):
        return value
    return None


def _int_field(payload: JsonObject, key: str) -> int | None:
    value = payload.get(key)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


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
