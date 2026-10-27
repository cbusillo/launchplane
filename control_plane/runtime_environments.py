from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast

import click

from control_plane import runtime_platform_credentials
from control_plane import secrets as control_plane_secrets
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.secret_record import SecretBinding, SecretScope
from control_plane.storage.factory import resolve_database_url
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.dokploy import source as dokploy_source
from control_plane.preview_settings import validate_preview_scope
from control_plane.runtime_key_safety import (
    RuntimeKeySafetyPolicyReadStore,
    evaluate_runtime_key_safety,
    latest_active_runtime_key_safety_policy,
    runtime_key_safety_environment_class,
)
from control_plane.contracts.runtime_key_safety_policy import RuntimeKeySafetyTarget

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


class SiteSecretDeliveryStore(
    control_plane_secrets.SecretBindingSelectionStore, RuntimeEnvironmentRecordStore, Protocol
):
    pass


def site_delivered_secret_bindings_from_store(
    *, record_store: SiteSecretDeliveryStore, context_name: str, instance_name: str
) -> dict[str, SecretBinding]:
    """The binding whose value ``resolve_site_runtime_environment`` delivers under each key.

    It makes the same selection as the lane's runtime delivery (integration, scopes and
    retired provider keys) without reading any value, so a setting that points at a
    binding can be checked against what the lane's container will actually receive.
    """
    selected = control_plane_secrets.resolve_effective_secret_bindings_from_store(
        record_store=record_store,
        integration=control_plane_secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
        context_name=context_name,
        instance_name=instance_name,
        scopes=site_secret_scopes(instance_name),
    )
    retired_keys = retired_provider_keys_from_store(
        record_store=record_store, context_name=context_name, instance_name=instance_name
    )
    return {
        binding_key: binding
        for binding_key, (binding, _record) in selected.items()
        if binding_key not in retired_keys
    }


@dataclass(frozen=True)
class SiteRuntimeEnvironment:
    """A site lane's environment after retirement and the platform-credential policy."""

    values: dict[str, str] = field(repr=False)
    secret_keys: frozenset[str]
    retired_keys: frozenset[str] = frozenset()
    site_keys: frozenset[str] = frozenset()
    setting_keys: frozenset[str] = frozenset()


_SITE_SHARED_AND_LANE_SECRET_SCOPES: frozenset[SecretScope] = frozenset(
    {"context", "context_instance"}
)
_LANE_ONLY_SECRET_SCOPES: frozenset[SecretScope] = frozenset({"context_instance"})


def site_secret_scopes(instance_name: str) -> frozenset[SecretScope]:
    """The managed-secret scopes a site lane's runtime receives."""
    if runtime_key_safety_environment_class(instance_name) in {"prod", "testing"}:
        return _SITE_SHARED_AND_LANE_SECRET_SCOPES
    return _LANE_ONLY_SECRET_SCOPES


def resolve_site_runtime_environment(
    *,
    control_plane_root: Path,
    context_name: str,
    instance_name: str,
    database_url: str | None = None,
    target_environment_override: dict[str, str] | None = None,
    secret_binding_keys: frozenset[str] | None = None,
) -> SiteRuntimeEnvironment:
    """The environment a site's lane runs with: its own values and nothing else.

    Values shared by every product and Launchplane's own credentials are left out. Secrets
    shared across the site reach its testing and prod lanes, never a preview.
    A replacement can supply its intended target layer explicitly, so preparing
    it never inherits overrides from the retained recipient.
    A private copy may restrict secret keys before any version is read or
    decrypted; the default retains the site's ordinary delivery contract.

    Every path that writes a site's app runtime from Launchplane records uses this
    resolver, so the lane's ``retired_provider_keys`` are dropped and the
    platform-credential policy applies the same way: a platform credential, or a
    GitHub token value under any key, refuses the render and names the key and its
    source. ``site_keys`` holds the key names before retirement; ``setting_keys`` holds
    the ones a plain setting supplies, leaving out keys only a managed secret supplies.
    """
    definition = load_runtime_environment_definition(
        control_plane_root=control_plane_root,
        database_url=database_url,
    )
    if context_name not in definition.contexts:
        raise MissingRuntimeContextDefinitionError(
            f"Runtime environments file has no context definition for {context_name!r}."
        )
    target_environment = (
        target_environment_override
        if target_environment_override is not None
        else resolve_tracked_target_environment_values(
            control_plane_root=control_plane_root,
            context_name=context_name,
            instance_name=instance_name,
            database_url=database_url,
        )
    )
    secret_values = control_plane_secrets.resolve_site_secret_values(
        context_name=context_name,
        instance_name=instance_name,
        include_site_shared="context" in site_secret_scopes(instance_name),
        database_url=database_url,
        binding_keys=secret_binding_keys,
    )
    retired_keys = retired_provider_keys_for_lane(
        context_name=context_name, instance_name=instance_name, database_url=database_url
    )
    secret_sources: dict[str, _RuntimeValueSource] = {}
    if runtime_platform_credentials.find_platform_credentials(secret_values):
        try:
            secret_sources = _runtime_value_sources(
                control_plane_root=control_plane_root,
                context_name=context_name,
                instance_name=instance_name,
                database_url=database_url,
                include_global=False,
                secret_scopes=site_secret_scopes(instance_name),
                secret_binding_keys=secret_binding_keys,
            )
        except click.ClickException:
            pass
    return _site_runtime_environment(
        definition=definition,
        context_name=context_name,
        instance_name=instance_name,
        target_environment=target_environment,
        secret_values=secret_values,
        retired_keys=retired_keys,
        secret_sources=secret_sources,
    )


class SiteRuntimeEnvironmentStore(
    control_plane_secrets.SecretReadStore, RuntimeEnvironmentRecordStore, Protocol
):
    pass


def resolve_site_runtime_environment_from_store(
    *,
    record_store: SiteRuntimeEnvironmentStore,
    context_name: str,
    instance_name: str,
    target_environment: dict[str, str],
) -> SiteRuntimeEnvironment:
    """The canonical site render using the executing operation's record store."""
    definition = load_optional_runtime_environment_definition_from_store(record_store=record_store)
    if definition is None:
        definition = RuntimeEnvironmentDefinition(schema_version=1, shared_env={}, contexts={})
    if context_name not in definition.contexts:
        definition = RuntimeEnvironmentDefinition(
            schema_version=definition.schema_version,
            shared_env=definition.shared_env,
            contexts={
                **definition.contexts,
                context_name: RuntimeEnvironmentContextDefinition(shared_env={}, instances={}),
            },
        )
    scoped_secret_values = (
        control_plane_secrets.resolve_scoped_secret_values_for_integration_from_store(
            record_store=record_store,
            integration=control_plane_secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
            context_name=context_name,
            instance_name=instance_name,
            scopes=site_secret_scopes(instance_name),
        )
    )
    secret_values = {key: value for key, (value, _scope) in scoped_secret_values.items()}
    scope_labels = {
        "global": "a global",
        "context": f"a {context_name} context",
        "context_instance": f"a {context_name}/{instance_name} instance",
    }
    return _site_runtime_environment(
        definition=definition,
        context_name=context_name,
        instance_name=instance_name,
        target_environment=target_environment,
        secret_values=secret_values,
        secret_sources={
            key: _RuntimeValueSource(f"{scope_labels[scope]} runtime-environment managed secret")
            for key, (_value, scope) in scoped_secret_values.items()
        },
        retired_keys=retired_provider_keys_from_store(
            record_store=record_store,
            context_name=context_name,
            instance_name=instance_name,
        ),
    )


def resolve_preview_runtime_environment_from_store(
    *,
    record_store: SiteRuntimeEnvironmentStore,
    context_name: str,
    template_values: dict[str, str],
    legacy_values: dict[str, str] | None = None,
) -> SiteRuntimeEnvironment:
    bindings = control_plane_secrets.resolve_effective_secret_bindings_from_store(
        record_store=record_store,
        integration=control_plane_secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
        context_name=context_name,
        instance_name="preview",
        scopes=site_secret_scopes("preview"),
    )
    managed_values = {
        key: value
        for record in record_store.list_runtime_environment_records(
            context_name=context_name, instance_name="preview"
        )
        if record.scope == "instance"
        and record.context == context_name
        and record.instance == "preview"
        for key, value in record.env.items()
    }
    instance_values: dict[str, ScalarValue] = {**(legacy_values or {}), **managed_values}
    try:
        validate_preview_scope(
            instance_values, context=context_name, secret_keys=frozenset(bindings)
        )
        if bindings:
            policy = latest_active_runtime_key_safety_policy(
                cast(RuntimeKeySafetyPolicyReadStore, record_store)
            )
            evaluation = evaluate_runtime_key_safety(
                target=RuntimeKeySafetyTarget(
                    context=context_name, instance="preview", environment_class="preview"
                ),
                required_binding_keys=bindings,
                secret_bindings=tuple(binding for binding, _record in bindings.values()),
                secret_rules=policy.rules,
                integration_key_markers=policy.integration_key_markers,
            )
            if evaluation.status != "pass":
                raise ValueError("Managed preview secrets failed runtime key-safety evaluation.")
    except ValueError:
        raise click.ClickException(
            "Managed preview settings failed validation or key-safety evaluation."
        ) from None
    preview = resolve_site_runtime_environment_from_store(
        record_store=record_store,
        context_name=context_name,
        instance_name="preview",
        target_environment={},
    )
    overrides = {
        **(legacy_values or {}),
        **{
            key: value
            for key, value in preview.values.items()
            if key in managed_values or key in preview.secret_keys
        },
    }
    return SiteRuntimeEnvironment(
        values={
            key: value
            for key, value in {**template_values, **overrides}.items()
            if key not in preview.retired_keys
        },
        secret_keys=preview.secret_keys,
        retired_keys=preview.retired_keys,
        setting_keys=frozenset(instance_values),
    )


def _site_runtime_environment(
    *,
    definition: RuntimeEnvironmentDefinition,
    context_name: str,
    instance_name: str,
    target_environment: dict[str, str],
    secret_values: dict[str, str],
    retired_keys: frozenset[str] | set[str],
    secret_sources: dict[str, _RuntimeValueSource] | None = None,
) -> SiteRuntimeEnvironment:
    context_definition = definition.contexts.get(context_name)
    if context_definition is None:
        raise MissingRuntimeContextDefinitionError(
            f"Runtime environments file has no context definition for {context_name!r}."
        )
    values = _normalize_scalar_map(context_definition.shared_env)
    instance_definition = context_definition.instances.get(instance_name)
    if instance_definition is not None:
        values.update(_normalize_scalar_map(instance_definition.env))
    values.update(target_environment)
    setting_keys = frozenset(values)
    values.update(secret_values)
    site_keys = frozenset(values)
    values = {key: value for key, value in values.items() if key not in retired_keys}
    sources = {
        key: source
        for layer, source in _record_layers(
            definition=definition,
            context_name=context_name,
            instance_name=instance_name,
            target_env=target_environment,
            include_global=False,
        )
        for key in layer
    }
    sources.update(
        {
            key: (secret_sources or {}).get(
                key, _RuntimeValueSource("a site runtime-environment managed secret")
            )
            for key in secret_values
        }
    )
    _refuse_platform_credentials(
        values=values, sources=sources, target=f"{context_name}/{instance_name}"
    )
    return SiteRuntimeEnvironment(
        values=values,
        secret_keys=frozenset(key for key in secret_values if key in values),
        retired_keys=frozenset(retired_keys),
        site_keys=site_keys,
        setting_keys=setting_keys,
    )


def site_application_keys(site_keys: frozenset[str] | set[str]) -> set[str]:
    """Site keys that count as application settings for retirement checks.

    Platform-credential names are never application settings: the render refuses them.
    """

    return set(site_keys) - runtime_platform_credentials.PLATFORM_CREDENTIAL_KEYS


@dataclass(frozen=True)
class _RuntimeValueSource:
    label: str


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
    overlay. Same refusal as ``resolve_site_runtime_environment``.
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
    _refuse_platform_credentials(
        values=values, sources=sources, target=f"{context_name}/{instance_name}"
    )
    return values


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


def _refuse_platform_credentials(
    *, values: dict[str, str], sources: dict[str, _RuntimeValueSource], target: str
) -> None:
    findings = tuple(
        runtime_platform_credentials.PlatformCredentialFinding(
            key=finding.key,
            reason=finding.reason,
            source=(
                sources[finding.key].label
                if finding.key in sources
                else "an unattributed runtime value"
            ),
        )
        for finding in runtime_platform_credentials.find_platform_credentials(values)
    )
    if findings:
        raise runtime_platform_credentials.PlatformCredentialRefusedError(
            target=target, findings=findings
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
                _RuntimeValueSource("the global runtime-environment record"),
            )
        )
    context_definition = definition.contexts.get(context_name)
    if context_definition is not None:
        layers.append(
            (
                context_definition.shared_env,
                _RuntimeValueSource(f"the {context_name} context runtime-environment record"),
            )
        )
        instance_definition = context_definition.instances.get(instance_name)
        if instance_definition is not None:
            layers.append(
                (
                    instance_definition.env,
                    _RuntimeValueSource(
                        f"the {context_name}/{instance_name} instance runtime-environment record"
                    ),
                )
            )
    layers.append(
        (
            target_env,
            _RuntimeValueSource(
                f"the {context_name}/{instance_name} tracked Dokploy target record"
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
    secret_binding_keys: frozenset[str] | None = None,
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
                binding_keys=secret_binding_keys,
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
            f"{scope_labels[scope]} runtime-environment managed secret"
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


def _normalize_scalar_map(raw_values: ScalarMap) -> dict[str, str]:
    return {key: str(value) for key, value in raw_values.items()}
