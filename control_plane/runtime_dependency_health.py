"""Derive monitoring requirements from the same lane metadata used for delivery.

This reads binding metadata only. It never decrypts secrets or connects to a
product database. Nonstandard integration keys use an explained override.
"""

from typing import cast

from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.contracts.runtime_dependency_health import (
    RuntimeDependency,
    RuntimeDependencyExpectations,
)
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.runtime_environments import (
    SiteSecretDeliveryStore,
    site_delivered_secret_bindings_from_store,
)


def _dependency_for_key(key: str) -> RuntimeDependency | None:
    if key in {
        "DATABASE_URL",
        "DIRECT_URL",
        "DB_HOST",
        "DB_NAME",
        "DB_PASSWORD",
        "ODOO_DB_VOLUME",
    } or key.startswith("POSTGRES_"):
        return "database"
    if key.startswith(("SMTP_", "RESEND_", "SENDGRID_", "MAILGUN_")):
        return "email"
    if key in {"REDIS_URL", "REDIS_HOST", "MEMCACHED_URL", "MEMCACHED_HOST"}:
        return "cache"
    return None


def derive_runtime_dependencies(
    *, profile: LaunchplaneProductProfileRecord, lane: ProductLaneProfile, record_store: object
) -> RuntimeDependencyExpectations:
    override = lane.health_monitoring.dependency_health.override
    if override is not None:
        return RuntimeDependencyExpectations(dependencies=override.dependencies, source="override")
    try:
        return _derive_runtime_dependencies(profile=profile, lane=lane, record_store=record_store)
    except Exception:
        # One unavailable metadata source must not stop monitoring every product.
        # Never carry the exception text or interpret an error as no dependencies.
        return RuntimeDependencyExpectations(
            dependencies=("database",) if profile.driver_id == "odoo" else (),
            source="unavailable",
        )


def _derive_runtime_dependencies(
    *, profile: LaunchplaneProductProfileRecord, lane: ProductLaneProfile, record_store: object
) -> RuntimeDependencyExpectations:
    dependencies: set[RuntimeDependency] = set()
    # The Odoo driver always deploys PostgreSQL, including lanes not yet materialized.
    if profile.driver_id == "odoo":
        dependencies.add("database")
    read_runtime = getattr(record_store, "list_runtime_environment_records", None)
    read_target = getattr(record_store, "read_dokploy_target_record", None)
    if (
        not callable(read_runtime)
        or not callable(read_target)
        or not all(
            callable(getattr(record_store, name, None))
            for name in (
                "list_runtime_environment_records",
                "read_dokploy_target_record",
                "list_secret_records",
                "list_secret_bindings",
            )
        )
    ):
        return RuntimeDependencyExpectations(
            dependencies=tuple(sorted(dependencies)), source="unavailable"
        )
    values: dict[str, object] = {}
    retired_keys: set[str] = set()
    # Match site runtime: no global values; context then exact lane then target.
    records: tuple[RuntimeEnvironmentRecord, ...] = read_runtime(context_name=lane.context)
    for record in sorted(records, key=lambda item: item.scope == "instance"):
        if record.context != lane.context or record.scope == "global":
            continue
        if record.scope == "context":
            values.update(record.env)
        elif record.scope == "instance" and record.instance == lane.instance:
            values.update(record.env)
            retired_keys.update(record.retired_provider_keys)
    try:
        target = read_target(context_name=lane.context, instance_name=lane.instance)
    except FileNotFoundError:
        target = None
    if target is not None:
        values.update(target.env)
    bindings = site_delivered_secret_bindings_from_store(
        record_store=cast(SiteSecretDeliveryStore, record_store),
        context_name=lane.context,
        instance_name=lane.instance,
    )
    # Binding existence is enough: values stay encrypted, and disabled/mismatched
    # scopes and retired keys are filtered by the delivery selector itself.
    values.update({key: True for key in bindings})
    for key, value in values.items():
        if key not in retired_keys and value is not None and str(value).strip():
            dependency = _dependency_for_key(key)
            if dependency is not None:
                dependencies.add(dependency)
    return RuntimeDependencyExpectations(dependencies=tuple(sorted(dependencies)))
