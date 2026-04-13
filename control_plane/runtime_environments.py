from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

import click

RUNTIME_ENVIRONMENTS_FILE_ENV_VAR = "ODOO_CONTROL_PLANE_RUNTIME_ENVIRONMENTS_FILE"
DEFAULT_RUNTIME_ENVIRONMENTS_FILE = "config/runtime-environments.toml"

ScalarValue = str | int | float | bool
ScalarMap = dict[str, ScalarValue]


@dataclass(frozen=True)
class RuntimeEnvironmentInstanceDefinition:
    env: ScalarMap


@dataclass(frozen=True)
class RuntimeEnvironmentContextDefinition:
    shared_env: ScalarMap
    instances: dict[str, RuntimeEnvironmentInstanceDefinition]


@dataclass(frozen=True)
class RuntimeEnvironmentDefinition:
    schema_version: int
    shared_env: ScalarMap
    contexts: dict[str, RuntimeEnvironmentContextDefinition]


def resolve_runtime_environments_file(control_plane_root: Path) -> Path:
    configured_file = os.environ.get(RUNTIME_ENVIRONMENTS_FILE_ENV_VAR, "").strip()
    if configured_file:
        candidate_path = Path(configured_file)
        if not candidate_path.is_absolute():
            candidate_path = control_plane_root / candidate_path
        return candidate_path
    return control_plane_root / DEFAULT_RUNTIME_ENVIRONMENTS_FILE


def load_runtime_environment_definition(
    *, control_plane_root: Path
) -> RuntimeEnvironmentDefinition:
    environments_file = resolve_runtime_environments_file(control_plane_root)
    if not environments_file.exists():
        raise click.ClickException(
            "Missing control-plane runtime environments file. "
            f"Create {environments_file} or point {RUNTIME_ENVIRONMENTS_FILE_ENV_VAR} at an alternate file."
        )
    try:
        payload = tomllib.loads(environments_file.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise click.ClickException(
            f"Invalid runtime environments file {environments_file}: {error}"
        ) from error
    return _parse_runtime_environment_definition(payload, source_file=environments_file)


def resolve_runtime_environment_values(
    *,
    control_plane_root: Path,
    context_name: str,
    instance_name: str,
) -> dict[str, str]:
    definition = load_runtime_environment_definition(control_plane_root=control_plane_root)
    merged_values: dict[str, str] = _normalize_scalar_map(definition.shared_env)
    context_definition = definition.contexts.get(context_name)
    if context_definition is None:
        raise click.ClickException(
            f"Runtime environments file has no context definition for {context_name!r}."
        )
    merged_values.update(_normalize_scalar_map(context_definition.shared_env))
    instance_definition = context_definition.instances.get(instance_name)
    if instance_definition is None:
        raise click.ClickException(
            f"Runtime environments file has no instance definition for {context_name}/{instance_name}."
        )
    merged_values.update(_normalize_scalar_map(instance_definition.env))
    return merged_values


def resolve_runtime_context_values(
    *,
    control_plane_root: Path,
    context_name: str,
) -> dict[str, str]:
    definition = load_runtime_environment_definition(control_plane_root=control_plane_root)
    merged_values: dict[str, str] = _normalize_scalar_map(definition.shared_env)
    context_definition = definition.contexts.get(context_name)
    if context_definition is None:
        raise click.ClickException(
            f"Runtime environments file has no context definition for {context_name!r}."
        )
    merged_values.update(_normalize_scalar_map(context_definition.shared_env))
    return merged_values


def _parse_runtime_environment_definition(
    payload: dict[str, object],
    *,
    source_file: Path,
) -> RuntimeEnvironmentDefinition:
    schema_version = _read_required_int(payload, "schema_version", scope="runtime_environments")
    contexts_table = _read_optional_table(payload, "contexts", scope="runtime_environments")
    contexts: dict[str, RuntimeEnvironmentContextDefinition] = {}
    for context_name, raw_context in contexts_table.items():
        context_table = _ensure_table(
            raw_context,
            scope=f"runtime_environments.contexts.{context_name}",
        )
        instances_table = _read_optional_table(
            context_table,
            "instances",
            scope=f"runtime_environments.contexts.{context_name}",
        )
        instances: dict[str, RuntimeEnvironmentInstanceDefinition] = {}
        for instance_name, raw_instance in instances_table.items():
            instance_table = _ensure_table(
                raw_instance,
                scope=f"runtime_environments.contexts.{context_name}.instances.{instance_name}",
            )
            instances[instance_name] = RuntimeEnvironmentInstanceDefinition(
                env=_read_optional_scalar_map(
                    instance_table,
                    "env",
                    scope=f"runtime_environments.contexts.{context_name}.instances.{instance_name}",
                )
            )
        contexts[context_name] = RuntimeEnvironmentContextDefinition(
            shared_env=_read_optional_scalar_map(
                context_table,
                "shared_env",
                scope=f"runtime_environments.contexts.{context_name}",
            ),
            instances=instances,
        )
    return RuntimeEnvironmentDefinition(
        schema_version=schema_version,
        shared_env=_read_optional_scalar_map(payload, "shared_env", scope="runtime_environments"),
        contexts=contexts,
    )


def _normalize_scalar_map(raw_values: ScalarMap) -> dict[str, str]:
    return {key: str(value) for key, value in raw_values.items()}


def _read_required_int(source: dict[str, object], key: str, *, scope: str) -> int:
    value = source.get(key)
    if not isinstance(value, int):
        raise click.ClickException(f"Expected {scope}.{key} to be an integer.")
    return value


def _read_optional_table(source: dict[str, object], key: str, *, scope: str) -> dict[str, object]:
    value = source.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise click.ClickException(f"Expected {scope}.{key} to be a table when present.")
    return value


def _ensure_table(value: object, *, scope: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise click.ClickException(f"Expected {scope} to be a table.")
    return value


def _read_optional_scalar_map(source: dict[str, object], key: str, *, scope: str) -> ScalarMap:
    value = source.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise click.ClickException(f"Expected {scope}.{key} to be a table when present.")
    scalar_map: ScalarMap = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str):
            raise click.ClickException(f"Expected {scope}.{key} keys to be strings.")
        if not isinstance(raw_value, (str, int, float, bool)):
            raise click.ClickException(f"Expected {scope}.{key}.{raw_key} to be a scalar value.")
        scalar_map[raw_key] = raw_value
    return scalar_map
