from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import click

from control_plane import runtime_platform_credentials
from control_plane import secrets as control_plane_secrets
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.secret_record import SecretScope
from control_plane.storage.factory import resolve_database_url
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.dokploy import source as dokploy_source
from control_plane.runtime_key_safety import runtime_key_safety_environment_class

DEFAULT_RUNTIME_ENVIRONMENTS_FILE = "config/runtime-environments.toml"

ScalarValue = str | int | float | bool
ScalarMap = dict[str, ScalarValue]


class MissingRuntimeContextDefinitionError(click.ClickException):
    pass


class RuntimeEnvironmentRecordStore(Protocol):
    def list_runtime_environment_records(
        self, *, context_name: str = "", instance_name: str = ""
    ) -> tuple[RuntimeEnvironmentRecord, ...]: ...


def retired_provider_keys_from_store(
    *, record_store: RuntimeEnvironmentRecordStore, context_name: str, instance_name: str
) -> set[str]:
    records = tuple(
        record
        for record in record_store.list_runtime_environment_records(
            context_name=context_name, instance_name=instance_name
        )
        if record.scope == "instance"
        and record.context == context_name
        and record.instance == instance_name
    )
    if len(records) > 1:
        raise click.ClickException("Ambiguous instance runtime environment records.")
    return set(records[0].retired_provider_keys) if records else set()


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


def load_runtime_environment_definition(
    *, control_plane_root: Path, database_url: str | None = None
) -> RuntimeEnvironmentDefinition:
    database_url = resolve_database_url(database_url)
    if database_url:
        database_definition = _load_optional_runtime_environment_definition_from_database(
            database_url=database_url
        )
        if database_definition is not None:
            return database_definition
        raise click.ClickException("Missing DB-backed Launchplane runtime environment records.")

    raise click.ClickException(
        "Missing Launchplane runtime environment authority. Configure DB-backed runtime environment records."
    )


def resolve_runtime_environment_values(
    *,
    control_plane_root: Path,
    context_name: str,
    instance_name: str,
    database_url: str | None = None,
) -> dict[str, str]:
    definition = load_runtime_environment_definition(
        control_plane_root=control_plane_root,
        database_url=database_url,
    )
    merged_values = resolve_values_from_definition(
        definition=definition,
        context_name=context_name,
        instance_name=instance_name,
    )
    merged_values.update(
        resolve_tracked_target_environment_values(
            control_plane_root=control_plane_root,
            context_name=context_name,
            instance_name=instance_name,
            database_url=database_url,
        )
    )
    return control_plane_secrets.overlay_runtime_environment_secret_values(
        environment_values=merged_values,
        context_name=context_name,
        instance_name=instance_name,
        database_url=database_url,
    )


@dataclass(frozen=True)
class SiteRuntimeEnvironment:
    values: dict[str, str]
    secret_keys: frozenset[str]


def resolve_site_runtime_environment(
    *,
    control_plane_root: Path,
    context_name: str,
    instance_name: str,
    database_url: str | None = None,
) -> SiteRuntimeEnvironment:
    """The environment a site's lane runs with: its own values and nothing else.

    Values shared by every product and Launchplane's own credentials are left out. Secrets
    shared across the site reach its testing and prod lanes, never a preview.
    """
    definition = load_runtime_environment_definition(
        control_plane_root=control_plane_root,
        database_url=database_url,
    )
    context_definition = definition.contexts.get(context_name)
    if context_definition is None:
        raise MissingRuntimeContextDefinitionError(
            f"Runtime environments file has no context definition for {context_name!r}."
        )
    values = _normalize_scalar_map(context_definition.shared_env)
    instance_definition = context_definition.instances.get(instance_name)
    if instance_definition is not None:
        values.update(_normalize_scalar_map(instance_definition.env))
    values.update(
        resolve_tracked_target_environment_values(
            control_plane_root=control_plane_root,
            context_name=context_name,
            instance_name=instance_name,
            database_url=database_url,
        )
    )
    secret_values = control_plane_secrets.resolve_site_secret_values(
        context_name=context_name,
        instance_name=instance_name,
        include_site_shared=runtime_key_safety_environment_class(instance_name)
        in {"prod", "testing"},
        database_url=database_url,
    )
    values.update(secret_values)
    return SiteRuntimeEnvironment(values=values, secret_keys=frozenset(secret_values))


@dataclass(frozen=True)
class SiteAppRuntimeEnvironment:
    """A site lane's environment after retirement and the platform-credential policy."""

    values: dict[str, str]
    secret_keys: frozenset[str]
    retired_keys: frozenset[str]
    site_keys: frozenset[str]
    withheld_launchplane_keys: tuple[str, ...] = ()


def resolve_site_app_runtime_environment(
    *,
    control_plane_root: Path,
    context_name: str,
    instance_name: str,
    database_url: str | None = None,
) -> SiteAppRuntimeEnvironment:
    """The site environment a live-target sync may write into the app.

    Starts from ``resolve_site_runtime_environment`` and applies the same
    provider-key retirement and platform-credential policy as
    ``resolve_app_runtime_environment``: context-scope Launchplane credentials
    are withheld, any other platform credential refuses the render.
    """

    site_environment = resolve_site_runtime_environment(
        control_plane_root=control_plane_root,
        context_name=context_name,
        instance_name=instance_name,
        database_url=database_url,
    )
    retired_keys = retired_provider_keys_for_lane(
        context_name=context_name, instance_name=instance_name, database_url=database_url
    )
    values = {
        key: value for key, value in site_environment.values.items() if key not in retired_keys
    }
    withheld: tuple[str, ...] = ()
    if runtime_platform_credentials.find_platform_credentials(values):
        secret_scopes: frozenset[SecretScope] = (
            frozenset({"context", "context_instance"})
            if runtime_key_safety_environment_class(instance_name) in {"prod", "testing"}
            else frozenset({"context_instance"})
        )
        try:
            sources = _runtime_value_sources(
                control_plane_root=control_plane_root,
                context_name=context_name,
                instance_name=instance_name,
                database_url=database_url,
                include_global=False,
                secret_scopes=secret_scopes,
            )
        except click.ClickException:
            # Without attribution nothing can be withheld as Launchplane's own, so
            # every finding refuses.
            sources = {}
        values, withheld = _apply_platform_credential_policy(
            values=values, sources=sources, target=f"{context_name}/{instance_name}"
        )
    return SiteAppRuntimeEnvironment(
        values=values,
        secret_keys=frozenset(key for key in site_environment.secret_keys if key in values),
        retired_keys=retired_keys,
        site_keys=frozenset(site_environment.values),
        withheld_launchplane_keys=withheld,
    )


def site_application_keys(site_keys: frozenset[str] | set[str]) -> set[str]:
    """Site keys that count as application settings for retirement checks.

    Platform-credential names are never application settings: the app either
    never receives them (withheld Launchplane credentials) or the render refuses.
    """

    return set(site_keys) - runtime_platform_credentials.PLATFORM_CREDENTIAL_KEYS


@dataclass(frozen=True)
class AppRuntimeEnvironment:
    """Values Launchplane may write into an application runtime, with retirement applied."""

    values: dict[str, str]
    retired_keys: frozenset[str]
    withheld_launchplane_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class _RuntimeValueSource:
    label: str
    launchplane_scope: bool


def resolve_app_runtime_environment(
    *,
    control_plane_root: Path,
    context_name: str,
    instance_name: str,
    database_url: str | None = None,
) -> AppRuntimeEnvironment:
    """Resolve the environment an application runtime may receive for one lane.

    Paths that write an app runtime environment from Launchplane records use
    this function, so provider-key retirement and the platform-credential
    refusal apply to all of them the same way.

    Global- and context-scope records are also where Launchplane keeps its own
    operating credentials (for example the token it uses for preview PR
    comments). Named platform-credential keys from those scopes are withheld
    from the app. Any other platform credential, or a GitHub token value under
    any key, refuses the render and names the key and its source.
    """

    values = resolve_runtime_environment_values(
        control_plane_root=control_plane_root,
        context_name=context_name,
        instance_name=instance_name,
        database_url=database_url,
    )
    retired_keys = retired_provider_keys_for_lane(
        context_name=context_name, instance_name=instance_name, database_url=database_url
    )
    values = {key: value for key, value in values.items() if key not in retired_keys}
    if not runtime_platform_credentials.find_platform_credentials(values):
        return AppRuntimeEnvironment(values=values, retired_keys=retired_keys)
    try:
        sources = _runtime_value_sources(
            control_plane_root=control_plane_root,
            context_name=context_name,
            instance_name=instance_name,
            database_url=database_url,
        )
    except click.ClickException:
        # Without attribution nothing can be withheld as Launchplane's own, so
        # every finding refuses.
        sources = {}
    app_values, withheld = _apply_platform_credential_policy(
        values=values, sources=sources, target=f"{context_name}/{instance_name}"
    )
    return AppRuntimeEnvironment(
        values=app_values,
        retired_keys=retired_keys,
        withheld_launchplane_keys=withheld,
    )


def resolve_app_values_from_definition(
    *,
    definition: RuntimeEnvironmentDefinition,
    context_name: str,
    instance_name: str,
    target_env: dict[str, str],
    retired_keys: frozenset[str] | set[str] = frozenset(),
) -> dict[str, str]:
    """Record-and-target app values for a lane, under the same credential policy.

    For store-backed drivers that resolve records without the managed-secret
    overlay. Same withholding and refusal as ``resolve_app_runtime_environment``.
    """

    layers = _record_layers(
        definition=definition,
        context_name=context_name,
        instance_name=instance_name,
        target_env=target_env,
    )
    values: dict[str, str] = {}
    sources: dict[str, _RuntimeValueSource] = {}
    for layer_values, source in layers:
        for key, value in layer_values.items():
            values[key] = str(value)
            sources[key] = source
    values = {key: value for key, value in values.items() if key not in retired_keys}
    app_values, _withheld_keys = _apply_platform_credential_policy(
        values=values, sources=sources, target=f"{context_name}/{instance_name}"
    )
    return app_values


def merge_provider_environment(
    *,
    current_env_map: dict[str, str],
    desired_env_map: dict[str, str],
    retired_keys: frozenset[str] | set[str],
) -> dict[str, str]:
    """Keep provider-only keys, drop retired ones, and apply the desired values."""

    merged = {key: value for key, value in current_env_map.items() if key not in retired_keys}
    merged.update(desired_env_map)
    return merged


def _apply_platform_credential_policy(
    *, values: dict[str, str], sources: dict[str, _RuntimeValueSource], target: str
) -> tuple[dict[str, str], tuple[str, ...]]:
    withheld_keys: list[str] = []
    refused: list[runtime_platform_credentials.PlatformCredentialFinding] = []
    for finding in runtime_platform_credentials.find_platform_credentials(values):
        source = sources.get(finding.key)
        if (
            finding.reason == "platform_credential_key"
            and source is not None
            and source.launchplane_scope
        ):
            withheld_keys.append(finding.key)
            continue
        refused.append(
            runtime_platform_credentials.PlatformCredentialFinding(
                key=finding.key,
                reason=finding.reason,
                source=source.label if source is not None else "an unattributed runtime value",
            )
        )
    if refused:
        raise runtime_platform_credentials.PlatformCredentialRefusedError(
            target=target, findings=tuple(refused)
        )
    return (
        {key: value for key, value in values.items() if key not in withheld_keys},
        tuple(withheld_keys),
    )


def retired_provider_keys_for_lane(
    *, context_name: str, instance_name: str, database_url: str | None
) -> frozenset[str]:
    resolved_database_url = resolve_database_url(database_url)
    if resolved_database_url is None:
        return frozenset()
    record_store = PostgresRecordStore(database_url=resolved_database_url)
    try:
        record_store.ensure_schema()
        return frozenset(
            retired_provider_keys_from_store(
                record_store=record_store,
                context_name=context_name,
                instance_name=instance_name,
            )
        )
    finally:
        record_store.close()


def _record_layers(
    *,
    definition: RuntimeEnvironmentDefinition,
    context_name: str,
    instance_name: str,
    target_env: dict[str, str],
    include_global: bool = True,
) -> list[tuple[ScalarMap | dict[str, str], _RuntimeValueSource]]:
    """Record and tracked-target layers in merge order, each with its source."""

    layers: list[tuple[ScalarMap | dict[str, str], _RuntimeValueSource]] = []
    if include_global:
        layers.append(
            (
                definition.shared_env,
                _RuntimeValueSource("the global runtime-environment record", True),
            )
        )
    context_definition = definition.contexts.get(context_name)
    if context_definition is not None:
        layers.append(
            (
                context_definition.shared_env,
                _RuntimeValueSource(f"the {context_name} context runtime-environment record", True),
            )
        )
        instance_definition = context_definition.instances.get(instance_name)
        if instance_definition is not None:
            layers.append(
                (
                    instance_definition.env,
                    _RuntimeValueSource(
                        f"the {context_name}/{instance_name} instance runtime-environment record",
                        False,
                    ),
                )
            )
    layers.append(
        (
            target_env,
            _RuntimeValueSource(
                f"the {context_name}/{instance_name} tracked Dokploy target record", False
            ),
        )
    )
    return layers


def _runtime_value_sources(
    *,
    control_plane_root: Path,
    context_name: str,
    instance_name: str,
    database_url: str | None,
    include_global: bool = True,
    secret_scopes: frozenset[SecretScope] | None = None,
) -> dict[str, _RuntimeValueSource]:
    """Attribute each effective key to the layer that supplied it, in merge order.

    ``include_global`` and ``secret_scopes`` must match the resolver whose values
    are being attributed, so a key is credited to the layer that actually won.
    """

    layers = _record_layers(
        definition=load_runtime_environment_definition(
            control_plane_root=control_plane_root, database_url=database_url
        ),
        context_name=context_name,
        instance_name=instance_name,
        target_env=resolve_tracked_target_environment_values(
            control_plane_root=control_plane_root,
            context_name=context_name,
            instance_name=instance_name,
            database_url=database_url,
        ),
        include_global=include_global,
    )
    sources: dict[str, _RuntimeValueSource] = {}
    for layer_values, source in layers:
        for key in layer_values:
            sources[key] = source
    resolved_database_url = resolve_database_url(database_url)
    if resolved_database_url is None:
        return sources
    record_store = PostgresRecordStore(database_url=resolved_database_url)
    try:
        scoped_secrets = (
            control_plane_secrets.resolve_scoped_secret_values_for_integration_from_store(
                record_store=record_store,
                integration=control_plane_secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
                context_name=context_name,
                instance_name=instance_name,
                scopes=secret_scopes,
            )
        )
    finally:
        record_store.close()
    scope_labels = {
        "global": "a global",
        "context": f"a {context_name} context",
        "context_instance": f"a {context_name}/{instance_name} instance",
    }
    for key, (_value, scope) in scoped_secrets.items():
        sources[key] = _RuntimeValueSource(
            f"{scope_labels[scope]} runtime-environment managed secret",
            scope in {"global", "context"},
        )
    return sources


def resolve_values_from_definition(
    *,
    definition: RuntimeEnvironmentDefinition,
    context_name: str,
    instance_name: str,
) -> dict[str, str]:
    merged_values: dict[str, str] = _normalize_scalar_map(definition.shared_env)
    context_definition = definition.contexts.get(context_name)
    if context_definition is None:
        raise MissingRuntimeContextDefinitionError(
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


def resolve_optional_values_from_definition(
    *,
    definition: RuntimeEnvironmentDefinition,
    context_name: str,
    instance_name: str,
) -> dict[str, str]:
    merged_values: dict[str, str] = _normalize_scalar_map(definition.shared_env)
    context_definition = definition.contexts.get(context_name)
    if context_definition is None:
        return merged_values
    merged_values.update(_normalize_scalar_map(context_definition.shared_env))
    instance_definition = context_definition.instances.get(instance_name)
    if instance_definition is None:
        return merged_values
    merged_values.update(_normalize_scalar_map(instance_definition.env))
    return merged_values


def resolve_runtime_context_values(
    *,
    control_plane_root: Path,
    context_name: str,
    database_url: str | None = None,
) -> dict[str, str]:
    definition = load_runtime_environment_definition(
        control_plane_root=control_plane_root,
        database_url=database_url,
    )
    merged_values: dict[str, str] = _normalize_scalar_map(definition.shared_env)
    context_definition = definition.contexts.get(context_name)
    if context_definition is None:
        raise MissingRuntimeContextDefinitionError(
            f"Runtime environments file has no context definition for {context_name!r}."
        )
    merged_values.update(_normalize_scalar_map(context_definition.shared_env))
    return control_plane_secrets.overlay_runtime_environment_secret_values(
        environment_values=merged_values,
        context_name=context_name,
        database_url=database_url,
    )


def resolve_tracked_target_environment_values(
    *,
    control_plane_root: Path,
    context_name: str,
    instance_name: str,
    database_url: str | None = None,
) -> dict[str, str]:
    try:
        source_of_truth = dokploy_source.read_control_plane_dokploy_source_of_truth(
            control_plane_root=control_plane_root,
            database_url=database_url,
        )
    except click.ClickException as error:
        error_message = str(error)
        if error_message.startswith(
            "Missing Launchplane tracked Dokploy target authority"
        ) or error_message.startswith(
            "Missing DB-backed Launchplane tracked Dokploy target records."
        ):
            return {}
        raise
    target_definition = dokploy_source.find_dokploy_target_definition(
        source_of_truth,
        context_name=context_name,
        instance_name=instance_name,
    )
    if target_definition is None:
        return {}
    return dict(target_definition.env)


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


def _load_optional_runtime_environment_definition_from_database(
    *, database_url: str
) -> RuntimeEnvironmentDefinition | None:
    record_store: PostgresRecordStore | None = None
    try:
        record_store = PostgresRecordStore(database_url=database_url)
        record_store.ensure_schema()
        return load_optional_runtime_environment_definition_from_store(record_store=record_store)
    except Exception as error:
        raise click.ClickException(
            f"Could not load runtime environments from Launchplane Postgres storage: {error}"
        ) from error
    finally:
        try:
            if record_store is not None:
                record_store.close()
        except Exception:
            pass


def load_optional_runtime_environment_definition_from_store(
    *, record_store: RuntimeEnvironmentRecordStore
) -> RuntimeEnvironmentDefinition | None:
    records = record_store.list_runtime_environment_records()
    if not records:
        return None
    return build_runtime_environment_definition_from_records(records)


def build_runtime_environment_definition_from_records(
    records: tuple[RuntimeEnvironmentRecord, ...],
) -> RuntimeEnvironmentDefinition:
    shared_env: ScalarMap = {}
    contexts: dict[str, RuntimeEnvironmentContextDefinition] = {}
    for record in sorted(records, key=lambda item: (item.scope, item.context, item.instance)):
        if record.scope == "global":
            shared_env.update(record.env)
            continue
        context_definition = contexts.setdefault(
            record.context,
            RuntimeEnvironmentContextDefinition(shared_env={}, instances={}),
        )
        if record.scope == "context":
            merged_shared_env = dict(context_definition.shared_env)
            merged_shared_env.update(record.env)
            contexts[record.context] = RuntimeEnvironmentContextDefinition(
                shared_env=merged_shared_env,
                instances=dict(context_definition.instances),
            )
            continue
        instances = dict(context_definition.instances)
        instances[record.instance] = RuntimeEnvironmentInstanceDefinition(env=dict(record.env))
        contexts[record.context] = RuntimeEnvironmentContextDefinition(
            shared_env=dict(context_definition.shared_env),
            instances=instances,
        )
    return RuntimeEnvironmentDefinition(schema_version=1, shared_env=shared_env, contexts=contexts)


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
