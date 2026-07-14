from collections.abc import Mapping
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import urlparse

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane import secrets as control_plane_secrets
from control_plane.contracts.dokploy_target_record import DokployTargetPolicies, DokployTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.storage.factory import resolve_database_url
from control_plane.storage.postgres import PostgresRecordStore


DEFAULT_DOKPLOY_DEPLOY_TIMEOUT_SECONDS = 600
DEFAULT_DOKPLOY_HEALTH_TIMEOUT_SECONDS = 180
DEFAULT_DOKPLOY_HEALTHCHECK_PATH = "/web/health"
DEFAULT_CONTROL_PLANE_DOKPLOY_SOURCE_FILE = Path("config/dokploy.toml")
DEFAULT_CONTROL_PLANE_DOKPLOY_TARGET_IDS_FILE = Path("config/dokploy-targets.toml")
DEFAULT_STABLE_REMOTE_INSTANCES = {"testing", "prod"}


class DokployTargetRecordStore(Protocol):
    def list_dokploy_target_records(self) -> tuple[DokployTargetRecord, ...]: ...

    def list_dokploy_target_id_records(self) -> tuple[DokployTargetIdRecord, ...]: ...


class DokployTargetDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: str
    instance: str
    project_name: str = ""
    target_type: Literal["compose", "application"] = "compose"
    target_id: str = ""
    target_name: str = ""
    git_branch: str = ""
    source_git_ref: str = "origin/main"
    source_type: str = ""
    custom_git_url: str = ""
    custom_git_branch: str = ""
    compose_path: str = ""
    watch_paths: tuple[str, ...] = ()
    enable_submodules: bool | None = None
    require_test_gate: bool = False
    require_prod_gate: bool = False
    deploy_timeout_seconds: int | None = Field(default=None, ge=1)
    healthcheck_enabled: bool = True
    healthcheck_path: str = DEFAULT_DOKPLOY_HEALTHCHECK_PATH
    healthcheck_timeout_seconds: int | None = Field(default=None, ge=1)
    env: dict[str, str] = Field(default_factory=dict)
    domains: tuple[str, ...] = ()
    policies: DokployTargetPolicies = Field(default_factory=DokployTargetPolicies)

    @model_validator(mode="after")
    def _validate_identity_fields(self) -> "DokployTargetDefinition":
        if not self.context.strip():
            raise ValueError("Dokploy target requires non-empty context")
        if not self.instance.strip():
            raise ValueError("Dokploy target requires non-empty instance")
        return self


class DokploySourceOfTruth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(ge=1)
    targets: tuple[DokployTargetDefinition, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _normalize_inherited_targets(cls, raw_value: object) -> object:
        return _normalize_dokploy_source_payload(raw_value)

    @model_validator(mode="after")
    def _validate_unique_target_routes(self) -> "DokploySourceOfTruth":
        seen_targets: set[tuple[str, str]] = set()
        for target_definition in self.targets:
            target_route = (target_definition.context.strip(), target_definition.instance.strip())
            if target_route in seen_targets:
                context_name, instance_name = target_route
                raise ValueError(
                    f"Duplicate Dokploy target definition for {context_name}/{instance_name} in source-of-truth"
                )
            seen_targets.add(target_route)
            if target_definition.instance not in DEFAULT_STABLE_REMOTE_INSTANCES:
                supported_instances = ", ".join(sorted(DEFAULT_STABLE_REMOTE_INSTANCES))
                raise ValueError(
                    "Tracked Dokploy source-of-truth only supports stable remote instances "
                    f"{supported_instances}; found {target_definition.context}/{target_definition.instance}. "
                    "Use Launchplane preview records for PR previews instead of adding another tracked Dokploy lane."
                )
        return self


def find_dokploy_target_definition(
    source_of_truth: DokploySourceOfTruth,
    *,
    context_name: str,
    instance_name: str,
) -> DokployTargetDefinition | None:
    for target in source_of_truth.targets:
        if target.context == context_name and target.instance == instance_name:
            return target
    return None


def protected_shopify_store_keys_for_target_definition(
    target_definition: DokployTargetDefinition,
) -> tuple[str, ...]:
    return target_definition.policies.shopify.protected_store_keys


def resolve_ship_timeout_seconds(
    *,
    timeout_override_seconds: int | None,
    target_definition: DokployTargetDefinition,
) -> int:
    if timeout_override_seconds is not None:
        if timeout_override_seconds <= 0:
            raise click.ClickException("Ship timeout must be greater than zero seconds.")
        return timeout_override_seconds
    if target_definition.deploy_timeout_seconds is not None:
        return target_definition.deploy_timeout_seconds
    return DEFAULT_DOKPLOY_DEPLOY_TIMEOUT_SECONDS


def resolve_ship_health_timeout_seconds(
    *,
    health_timeout_override_seconds: int | None,
    target_definition: DokployTargetDefinition | None,
) -> int:
    if health_timeout_override_seconds is not None:
        if health_timeout_override_seconds <= 0:
            raise click.ClickException("Ship health timeout must be greater than zero seconds.")
        return health_timeout_override_seconds
    if target_definition is not None and target_definition.healthcheck_timeout_seconds is not None:
        return target_definition.healthcheck_timeout_seconds
    return DEFAULT_DOKPLOY_HEALTH_TIMEOUT_SECONDS


def resolve_dokploy_ship_mode(
    context_name: str, instance_name: str, environment_values: dict[str, str]
) -> str:
    specific_key = f"DOKPLOY_SHIP_MODE_{context_name}_{instance_name}".upper()
    configured_mode = environment_values.get(specific_key, "").strip().lower()
    if not configured_mode:
        configured_mode = (
            environment_values.get("DOKPLOY_SHIP_MODE", "auto").strip().lower() or "auto"
        )
    if configured_mode not in {"auto", "compose", "application"}:
        raise click.ClickException(
            f"Invalid Dokploy ship mode '{configured_mode}'. Expected auto, compose, or application."
        )
    return configured_mode


def normalize_healthcheck_path(raw_healthcheck_path: str) -> str:
    normalized_path = raw_healthcheck_path.strip() or DEFAULT_DOKPLOY_HEALTHCHECK_PATH
    if not normalized_path.startswith("/"):
        normalized_path = f"/{normalized_path}"
    return normalized_path


def resolve_healthcheck_base_urls(
    *,
    target_definition: DokployTargetDefinition | None,
    environment_values: dict[str, str],
) -> tuple[str, ...]:
    raw_base_urls: list[str] = []
    if target_definition is not None:
        raw_base_urls.extend(domain for domain in target_definition.domains if domain)

    normalized_base_urls: list[str] = []
    for raw_base_url in raw_base_urls:
        stripped_base_url = raw_base_url.strip()
        if not stripped_base_url:
            continue
        parsed_base_url = urlparse(stripped_base_url)
        if not parsed_base_url.scheme:
            stripped_base_url = f"https://{stripped_base_url}"
        stripped_base_url = stripped_base_url.rstrip("/")
        if stripped_base_url and stripped_base_url not in normalized_base_urls:
            normalized_base_urls.append(stripped_base_url)

    return tuple(normalized_base_urls)


def resolve_ship_healthcheck_urls(
    *,
    target_definition: DokployTargetDefinition | None,
    environment_values: dict[str, str],
) -> tuple[str, ...]:
    if target_definition is not None and not target_definition.healthcheck_enabled:
        return ()

    healthcheck_path = normalize_healthcheck_path(
        target_definition.healthcheck_path
        if target_definition is not None
        else DEFAULT_DOKPLOY_HEALTHCHECK_PATH
    )
    base_urls = resolve_healthcheck_base_urls(
        target_definition=target_definition, environment_values=environment_values
    )
    return tuple(f"{base_url}{healthcheck_path}" for base_url in base_urls)


def read_control_plane_environment_values(
    *, control_plane_root: Path, database_url: str | None = None
) -> dict[str, str]:
    del control_plane_root
    return control_plane_secrets.overlay_dokploy_environment_values(
        environment_values={}, database_url=database_url
    )


def read_control_plane_dokploy_source_of_truth(
    *,
    control_plane_root: Path,
    database_url: str | None = None,
    allow_incomplete_target_ids: bool = False,
    allowed_incomplete_target_routes: tuple[tuple[str, str], ...] = (),
) -> DokploySourceOfTruth:
    del control_plane_root
    database_url = resolve_database_url(database_url)
    if not database_url:
        raise click.ClickException(
            "Missing Launchplane tracked Dokploy target authority. Configure DB-backed tracked target records."
        )
    source_of_truth = _load_optional_dokploy_source_of_truth_from_database(
        database_url=database_url,
        allow_incomplete_target_ids=allow_incomplete_target_ids,
        allowed_incomplete_target_routes=allowed_incomplete_target_routes,
    )
    if source_of_truth is None:
        raise click.ClickException("Missing DB-backed Launchplane tracked Dokploy target records.")
    return source_of_truth


def build_dokploy_target_record_from_definition(
    definition: DokployTargetDefinition,
    *,
    updated_at: str,
    source_label: str = "",
) -> DokployTargetRecord:
    return DokployTargetRecord(
        context=definition.context,
        instance=definition.instance,
        project_name=definition.project_name,
        target_type=definition.target_type,
        target_name=definition.target_name,
        git_branch=definition.git_branch,
        source_git_ref=definition.source_git_ref,
        source_type=definition.source_type,
        custom_git_url=definition.custom_git_url,
        custom_git_branch=definition.custom_git_branch,
        compose_path=definition.compose_path,
        watch_paths=definition.watch_paths,
        enable_submodules=definition.enable_submodules,
        require_test_gate=definition.require_test_gate,
        require_prod_gate=definition.require_prod_gate,
        deploy_timeout_seconds=definition.deploy_timeout_seconds,
        healthcheck_enabled=definition.healthcheck_enabled,
        healthcheck_path=definition.healthcheck_path,
        healthcheck_timeout_seconds=definition.healthcheck_timeout_seconds,
        env=dict(definition.env),
        domains=definition.domains,
        policies=definition.policies,
        updated_at=updated_at,
        source_label=source_label,
    )


def build_dokploy_source_of_truth_from_records(
    target_records: tuple[DokployTargetRecord, ...],
    target_id_records: tuple[DokployTargetIdRecord, ...],
    *,
    allow_incomplete_target_ids: bool = False,
    allowed_incomplete_target_routes: tuple[tuple[str, str], ...] = (),
) -> DokploySourceOfTruth:
    target_id_map = {
        (record.context.strip(), record.instance.strip()): record.target_id
        for record in target_id_records
    }
    remaining_target_id_routes = set(target_id_map)
    allowed_incomplete_routes = {
        (context_name.strip(), instance_name.strip())
        for context_name, instance_name in allowed_incomplete_target_routes
    }
    targets_payload: list[dict[str, object]] = []
    for record in target_records:
        target_route = (record.context.strip(), record.instance.strip())
        target_id = target_id_map.get(target_route, "").strip()
        if not target_id:
            if allow_incomplete_target_ids:
                if target_route not in allowed_incomplete_routes:
                    continue
            else:
                raise click.ClickException(
                    "Missing DB-backed Dokploy target-id record for "
                    f"{record.context}/{record.instance}."
                )
        else:
            remaining_target_id_routes.discard(target_route)
        targets_payload.append(
            {
                "context": record.context,
                "instance": record.instance,
                "project_name": record.project_name,
                "target_type": record.target_type,
                "target_id": target_id,
                "target_name": record.target_name,
                "git_branch": record.git_branch,
                "source_git_ref": record.source_git_ref,
                "source_type": record.source_type,
                "custom_git_url": record.custom_git_url,
                "custom_git_branch": record.custom_git_branch,
                "compose_path": record.compose_path,
                "watch_paths": list(record.watch_paths),
                "enable_submodules": record.enable_submodules,
                "require_test_gate": record.require_test_gate,
                "require_prod_gate": record.require_prod_gate,
                "deploy_timeout_seconds": record.deploy_timeout_seconds,
                "healthcheck_enabled": record.healthcheck_enabled,
                "healthcheck_path": record.healthcheck_path,
                "healthcheck_timeout_seconds": record.healthcheck_timeout_seconds,
                "env": dict(record.env),
                "domains": list(record.domains),
                "policies": record.policies.model_dump(mode="python"),
            }
        )
    if remaining_target_id_routes:
        unknown_routes = ", ".join(
            f"{context_name}/{instance_name}"
            for context_name, instance_name in sorted(remaining_target_id_routes)
        )
        raise click.ClickException(
            "DB-backed Dokploy target-id records contain route(s) that are not present in the tracked target records: "
            f"{unknown_routes}"
        )
    return DokploySourceOfTruth.model_validate({"schema_version": 1, "targets": targets_payload})


def load_optional_dokploy_source_of_truth_from_store(
    *,
    record_store: DokployTargetRecordStore,
    allow_incomplete_target_ids: bool = False,
    allowed_incomplete_target_routes: tuple[tuple[str, str], ...] = (),
) -> DokploySourceOfTruth | None:
    target_records = record_store.list_dokploy_target_records()
    if not target_records:
        return None
    target_id_records = record_store.list_dokploy_target_id_records()
    return build_dokploy_source_of_truth_from_records(
        target_records,
        target_id_records,
        allow_incomplete_target_ids=allow_incomplete_target_ids,
        allowed_incomplete_target_routes=allowed_incomplete_target_routes,
    )


def _load_optional_dokploy_source_of_truth_from_database(
    *,
    database_url: str,
    allow_incomplete_target_ids: bool = False,
    allowed_incomplete_target_routes: tuple[tuple[str, str], ...] = (),
) -> DokploySourceOfTruth | None:
    record_store: PostgresRecordStore | None = None
    try:
        record_store = PostgresRecordStore(database_url=database_url)
        record_store.ensure_schema()
        return load_optional_dokploy_source_of_truth_from_store(
            record_store=record_store,
            allow_incomplete_target_ids=allow_incomplete_target_ids,
            allowed_incomplete_target_routes=allowed_incomplete_target_routes,
        )
    except click.ClickException:
        raise
    except Exception as error:
        raise click.ClickException(
            f"Could not load tracked Dokploy targets from Launchplane Postgres storage: {error}"
        ) from error
    finally:
        try:
            if record_store is not None:
                record_store.close()
        except Exception:
            pass


def read_dokploy_config(
    *, control_plane_root: Path, database_url: str | None = None
) -> tuple[str, str]:
    environment_values = read_control_plane_environment_values(
        control_plane_root=control_plane_root, database_url=database_url
    )

    host = environment_values.get("DOKPLOY_HOST", "").strip()
    token = environment_values.get("DOKPLOY_TOKEN", "").strip()
    if not host or not token:
        raise click.ClickException(
            "Missing DOKPLOY_HOST or DOKPLOY_TOKEN for control-plane Dokploy execution. "
            "Configure Launchplane-managed Dokploy secrets in the shared store before running Dokploy operations."
        )
    return host, token


def _normalize_dokploy_source_payload(raw_value: object) -> object:
    if not isinstance(raw_value, Mapping):
        return raw_value

    normalized_payload = dict(raw_value)
    allowed_top_level_keys = {"defaults", "profiles", "projects", "schema_version", "targets"}
    unknown_keys = sorted(
        key_name for key_name in normalized_payload if key_name not in allowed_top_level_keys
    )
    if unknown_keys:
        unknown_key_list = ", ".join(unknown_keys)
        raise ValueError(f"Unknown top-level dokploy keys: {unknown_key_list}")

    raw_targets = normalized_payload.get("targets")
    if not isinstance(raw_targets, list):
        return raw_value

    defaults = _expect_mapping(normalized_payload.get("defaults"), label="defaults")
    raw_profiles = _expect_mapping(normalized_payload.get("profiles"), label="profiles")
    raw_projects = _expect_mapping(normalized_payload.get("projects"), label="projects")

    resolved_profiles: dict[str, dict[str, object]] = {}
    targets: list[object] = []
    for target_index, raw_target in enumerate(raw_targets, start=1):
        if not isinstance(raw_target, Mapping):
            targets.append(raw_target)
            continue

        target_payload = dict(raw_target)
        profile_name = str(target_payload.pop("profile", "") or "").strip()
        merged_target = dict(defaults)
        if profile_name:
            merged_target = _merge_dokploy_settings(
                merged_target,
                _resolve_dokploy_profile(
                    profile_name,
                    raw_profiles=raw_profiles,
                    raw_projects=raw_projects,
                    resolved_profiles=resolved_profiles,
                    active_profiles=(),
                ),
            )
        merged_target = _merge_dokploy_settings(merged_target, target_payload)
        targets.append(
            _resolve_dokploy_project_reference(
                merged_target,
                raw_projects=raw_projects,
                label=f"targets[{target_index}]",
            )
        )

    return {
        "schema_version": normalized_payload.get("schema_version"),
        "targets": targets,
    }


def _resolve_dokploy_profile(
    profile_name: str,
    *,
    raw_profiles: Mapping[str, object],
    raw_projects: Mapping[str, object],
    resolved_profiles: dict[str, dict[str, object]],
    active_profiles: tuple[str, ...],
) -> dict[str, object]:
    if profile_name in resolved_profiles:
        return dict(resolved_profiles[profile_name])
    if profile_name in active_profiles:
        profile_chain = " -> ".join((*active_profiles, profile_name))
        raise ValueError(f"Dokploy profile inheritance cycle detected: {profile_chain}")

    raw_profile = raw_profiles.get(profile_name)
    if raw_profile is None:
        raise ValueError(f"Unknown dokploy profile: {profile_name}")
    if not isinstance(raw_profile, Mapping):
        raise ValueError(f"Dokploy profile '{profile_name}' must be a table/object")

    profile_payload = dict(raw_profile)
    parent_profile_name = str(profile_payload.pop("extends", "") or "").strip()
    merged_profile: dict[str, object] = {}
    if parent_profile_name:
        merged_profile = _resolve_dokploy_profile(
            parent_profile_name,
            raw_profiles=raw_profiles,
            raw_projects=raw_projects,
            resolved_profiles=resolved_profiles,
            active_profiles=(*active_profiles, profile_name),
        )
    merged_profile = _merge_dokploy_settings(merged_profile, profile_payload)
    merged_profile = _resolve_dokploy_project_reference(
        merged_profile,
        raw_projects=raw_projects,
        label=f"profiles.{profile_name}",
    )
    resolved_profiles[profile_name] = dict(merged_profile)
    return merged_profile


def _resolve_dokploy_project_reference(
    payload: dict[str, object],
    *,
    raw_projects: Mapping[str, object],
    label: str,
) -> dict[str, object]:
    resolved_payload = dict(payload)
    raw_project_alias = resolved_payload.pop("project", None)
    if raw_project_alias in (None, ""):
        return resolved_payload

    project_alias = str(raw_project_alias).strip()
    if not project_alias:
        return resolved_payload
    if str(resolved_payload.get("project_name") or "").strip():
        raise ValueError(f"{label} cannot define both project and project_name")

    raw_project_value = raw_projects.get(project_alias)
    if raw_project_value is None:
        raise ValueError(f"Unknown dokploy project alias '{project_alias}' in {label}")
    if isinstance(raw_project_value, str):
        project_name = raw_project_value.strip()
    elif isinstance(raw_project_value, Mapping):
        project_name = str(raw_project_value.get("project_name") or "").strip()
    else:
        raise ValueError(
            f"Dokploy project alias '{project_alias}' in {label} must be a string or table"
        )
    if not project_name:
        raise ValueError(
            f"Dokploy project alias '{project_alias}' in {label} is missing project_name"
        )

    resolved_payload["project_name"] = project_name
    return resolved_payload


def _expect_mapping(raw_value: object, *, label: str) -> dict[str, object]:
    if raw_value in (None, ""):
        return {}
    if not isinstance(raw_value, Mapping):
        raise ValueError(f"Dokploy {label} must be a table/object")
    if not all(isinstance(key_name, str) for key_name in raw_value):
        raise ValueError(f"Dokploy {label} keys must be strings")
    return dict(raw_value)


def _merge_dokploy_settings(
    base: Mapping[str, object], overlay: Mapping[str, object]
) -> dict[str, object]:
    merged_settings = dict(base)
    for key_name, key_value in overlay.items():
        base_env = merged_settings.get("env")
        if key_name == "env" and isinstance(base_env, Mapping) and isinstance(key_value, Mapping):
            merged_env: dict[str, object] = {}
            for env_key, env_value in base_env.items():
                if isinstance(env_key, str):
                    merged_env[env_key] = env_value
            for env_key, env_value in key_value.items():
                if isinstance(env_key, str):
                    merged_env[env_key] = env_value
            merged_settings["env"] = merged_env
            continue
        merged_settings[key_name] = key_value
    return merged_settings
