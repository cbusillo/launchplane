from pathlib import Path
import click
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.runtime_environments import (
    RuntimeEnvironmentDefinition,
    RuntimeEnvironmentContextDefinition,
    RuntimeEnvironmentInstanceDefinition,
    ScalarMap,
)


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


def build_runtime_environment_records_from_definition(
    definition: RuntimeEnvironmentDefinition,
    *,
    updated_at: str,
    source_label: str,
) -> tuple[RuntimeEnvironmentRecord, ...]:
    records: list[RuntimeEnvironmentRecord] = []
    if definition.shared_env:
        records.append(
            RuntimeEnvironmentRecord(
                scope="global",
                env=dict(definition.shared_env),
                updated_at=updated_at,
                source_label=source_label,
            )
        )
    for context_name, context_definition in sorted(definition.contexts.items()):
        if context_definition.shared_env:
            records.append(
                RuntimeEnvironmentRecord(
                    scope="context",
                    context=context_name,
                    env=dict(context_definition.shared_env),
                    updated_at=updated_at,
                    source_label=source_label,
                )
            )
        for instance_name, instance_definition in sorted(context_definition.instances.items()):
            if not instance_definition.env:
                continue
            records.append(
                RuntimeEnvironmentRecord(
                    scope="instance",
                    context=context_name,
                    instance=instance_name,
                    env=dict(instance_definition.env),
                    updated_at=updated_at,
                    source_label=source_label,
                )
            )
    return tuple(records)


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
