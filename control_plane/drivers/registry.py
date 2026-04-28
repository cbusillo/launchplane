from __future__ import annotations

from fnmatch import fnmatchcase
from typing import Any, Literal

from control_plane.contracts.driver_descriptor import (
    DriverActionDescriptor,
    DriverActionSafety,
    DriverActionScope,
    DriverCapabilityDescriptor,
    DriverContextView,
    DriverDescriptor,
    DriverSettingGroupDescriptor,
    DriverView,
)
from control_plane.contracts.lane_summary import LaunchplaneLaneSummary
from control_plane.contracts.preview_summary import LaunchplanePreviewSummary


PROVIDER_BOUNDARY_NOTE = (
    "Launchplane exposes product capabilities and evidence; runtime-provider details stay behind "
    "backend adapters and evidence records."
)


def _action(
    action_id: str,
    label: str,
    description: str,
    *,
    safety: DriverActionSafety,
    scope: DriverActionScope,
    route_path: str,
    method: Literal["GET", "POST"] = "POST",
    writes_records: tuple[str, ...] = (),
) -> DriverActionDescriptor:
    return DriverActionDescriptor(
        action_id=action_id,
        label=label,
        description=description,
        safety=safety,
        scope=scope,
        method=method,
        route_path=route_path,
        writes_records=writes_records,
    )


ODOO_DRIVER = DriverDescriptor(
    driver_id="odoo",
    label="Odoo",
    product="odoo",
    description="Stable-lane Odoo artifact, deploy, backup, promotion, rollback, and settings driver.",
    context_patterns=("cm", "opw"),
    provider_boundary=PROVIDER_BOUNDARY_NOTE,
    capabilities=(
        DriverCapabilityDescriptor(
            capability_id="artifact_publish",
            label="Artifact publish",
            description="Accept and persist immutable Odoo artifact manifests produced by product CI.",
            actions=("artifact_publish_inputs", "artifact_publish"),
            panels=("artifact_evidence",),
        ),
        DriverCapabilityDescriptor(
            capability_id="post_deploy_settings",
            label="Post-deploy settings",
            description="Apply typed Odoo instance settings and managed secret bindings after deploy.",
            actions=("post_deploy",),
            panels=("settings", "secret_bindings", "audit"),
        ),
        DriverCapabilityDescriptor(
            capability_id="stable_promotion",
            label="Stable promotion",
            description="Capture backup evidence, promote testing to prod, and roll back prod to stored artifacts.",
            actions=("prod_backup_gate", "prod_promotion", "prod_rollback"),
            panels=("lane_health", "deployment_evidence", "promotion_evidence", "backup_evidence"),
        ),
    ),
    actions=(
        _action(
            "artifact_publish_inputs",
            "Resolve publish inputs",
            "Resolve DB-backed publish settings for product CI without exposing provider internals.",
            safety="read",
            scope="instance",
            route_path="/v1/drivers/odoo/artifact-publish-inputs",
        ),
        _action(
            "artifact_publish",
            "Record artifact",
            "Persist a product-built immutable artifact manifest as Launchplane release evidence.",
            safety="safe_write",
            scope="instance",
            route_path="/v1/drivers/odoo/artifact-publish",
            writes_records=("artifact_manifest",),
        ),
        _action(
            "post_deploy",
            "Apply post-deploy settings",
            "Apply typed Odoo instance settings through the product driver after a deployment.",
            safety="mutation",
            scope="instance",
            route_path="/v1/drivers/odoo/post-deploy",
            writes_records=("odoo_instance_override",),
        ),
        _action(
            "prod_backup_gate",
            "Capture prod backup gate",
            "Capture concrete backup evidence before a prod-changing action.",
            safety="safe_write",
            scope="instance",
            route_path="/v1/drivers/odoo/prod-backup-gate",
            writes_records=("backup_gate",),
        ),
        _action(
            "prod_promotion",
            "Promote testing to prod",
            "Promote a stored testing artifact to prod after backup-gate evidence passes.",
            safety="mutation",
            scope="instance",
            route_path="/v1/drivers/odoo/prod-promotion",
            writes_records=("deployment", "promotion", "inventory", "release_tuple"),
        ),
        _action(
            "prod_rollback",
            "Roll back prod",
            "Roll prod back to an explicit stored artifact and record health evidence.",
            safety="destructive",
            scope="instance",
            route_path="/v1/drivers/odoo/prod-rollback",
            writes_records=("deployment", "promotion", "inventory", "release_tuple"),
        ),
    ),
    setting_groups=(
        DriverSettingGroupDescriptor(
            group_id="runtime_environment",
            label="Runtime environment",
            description="DB-backed runtime settings used by Odoo driver actions.",
            scope="instance",
            fields=("ODOO_DB_NAME", "ODOO_DB_USER", "ODOO_BACKUP_ROOT"),
        ),
        DriverSettingGroupDescriptor(
            group_id="odoo_instance_overrides",
            label="Odoo instance settings",
            description="Typed Odoo config parameters and addon settings applied by the post-deploy driver.",
            scope="instance",
            fields=("config_parameters", "addon_settings", "apply_on"),
            secret_bindings=("ODOO_OVERRIDE_SECRET__*",),
        ),
    ),
)


VERIREEL_DRIVER = DriverDescriptor(
    driver_id="verireel",
    label="VeriReel",
    product="verireel",
    description="VeriReel stable-lane and preview lifecycle driver.",
    context_patterns=("verireel", "verireel-testing"),
    provider_boundary=PROVIDER_BOUNDARY_NOTE,
    capabilities=(
        DriverCapabilityDescriptor(
            capability_id="stable_deploy",
            label="Stable deploy",
            description="Deploy and inspect stable VeriReel environments through Launchplane evidence records.",
            actions=("testing_deploy", "prod_deploy", "stable_environment", "app_maintenance"),
            panels=("lane_health", "deployment_evidence", "settings"),
        ),
        DriverCapabilityDescriptor(
            capability_id="stable_promotion",
            label="Stable promotion",
            description="Capture backup evidence, promote testing to prod, and roll back prod to stored artifacts.",
            actions=("prod_backup_gate", "prod_promotion", "prod_rollback"),
            panels=("promotion_evidence", "backup_evidence"),
        ),
        DriverCapabilityDescriptor(
            capability_id="preview_lifecycle",
            label="Preview lifecycle",
            description="Refresh, inspect, and destroy ephemeral preview environments from stored preview records.",
            actions=("preview_refresh", "preview_inventory", "preview_destroy"),
            panels=("preview_inventory", "deployment_evidence", "audit"),
        ),
    ),
    actions=(
        _action(
            "testing_deploy",
            "Deploy testing",
            "Deploy a stored artifact to the testing lane and record deployment evidence.",
            safety="mutation",
            scope="instance",
            route_path="/v1/drivers/verireel/testing-deploy",
            writes_records=("deployment", "inventory", "release_tuple"),
        ),
        _action(
            "stable_environment",
            "Read stable environment",
            "Resolve DB-backed stable environment settings for driver execution.",
            safety="read",
            scope="instance",
            route_path="/v1/drivers/verireel/stable-environment",
        ),
        _action(
            "app_maintenance",
            "Run app maintenance",
            "Run an allow-listed product maintenance operation for a stable or preview environment.",
            safety="mutation",
            scope="instance",
            route_path="/v1/drivers/verireel/app-maintenance",
        ),
        _action(
            "prod_deploy",
            "Deploy prod",
            "Deploy a stored artifact directly to prod and record evidence.",
            safety="mutation",
            scope="instance",
            route_path="/v1/drivers/verireel/prod-deploy",
            writes_records=("deployment", "inventory", "release_tuple"),
        ),
        _action(
            "prod_backup_gate",
            "Capture prod backup gate",
            "Capture backup evidence before a prod-changing action.",
            safety="safe_write",
            scope="instance",
            route_path="/v1/drivers/verireel/prod-backup-gate",
            writes_records=("backup_gate",),
        ),
        _action(
            "prod_promotion",
            "Promote testing to prod",
            "Promote a stored testing artifact to prod after backup-gate evidence passes.",
            safety="mutation",
            scope="instance",
            route_path="/v1/drivers/verireel/prod-promotion",
            writes_records=("deployment", "promotion", "inventory", "release_tuple"),
        ),
        _action(
            "prod_rollback",
            "Roll back prod",
            "Roll prod back to an explicit stored artifact and record health evidence.",
            safety="destructive",
            scope="instance",
            route_path="/v1/drivers/verireel/prod-rollback",
            writes_records=("deployment", "promotion", "inventory", "release_tuple"),
        ),
        _action(
            "preview_refresh",
            "Refresh preview",
            "Refresh an ephemeral preview environment and return follow-up evidence instructions.",
            safety="mutation",
            scope="preview",
            route_path="/v1/drivers/verireel/preview-refresh",
            writes_records=("preview", "preview_generation"),
        ),
        _action(
            "preview_inventory",
            "Read preview inventory",
            "Read preview inventory and current preview lifecycle evidence.",
            safety="read",
            scope="context",
            route_path="/v1/drivers/verireel/preview-inventory",
        ),
        _action(
            "preview_destroy",
            "Destroy preview",
            "Destroy an ephemeral preview and record cleanup evidence.",
            safety="destructive",
            scope="preview",
            route_path="/v1/drivers/verireel/preview-destroy",
            writes_records=("preview", "preview_generation"),
        ),
    ),
    setting_groups=(
        DriverSettingGroupDescriptor(
            group_id="runtime_environment",
            label="Runtime environment",
            description="DB-backed runtime settings used by VeriReel driver actions.",
            scope="instance",
            fields=("VERIREEL_*", "LAUNCHPLANE_PREVIEW_BASE_URL"),
        ),
        DriverSettingGroupDescriptor(
            group_id="managed_secrets",
            label="Managed secret bindings",
            description="Write-only managed secret bindings surfaced as status metadata.",
            scope="instance",
            secret_bindings=("*_TOKEN", "*_SECRET", "*_KEY"),
        ),
    ),
)

_DESCRIPTORS: tuple[DriverDescriptor, ...] = (ODOO_DRIVER, VERIREEL_DRIVER)


def list_driver_descriptors() -> tuple[DriverDescriptor, ...]:
    return _DESCRIPTORS


def read_driver_descriptor(driver_id: str) -> DriverDescriptor:
    normalized_driver_id = driver_id.strip()
    for descriptor in _DESCRIPTORS:
        if descriptor.driver_id == normalized_driver_id:
            return descriptor
    raise FileNotFoundError(f"No Launchplane driver descriptor found for {driver_id!r}.")


def _driver_matches_context(*, descriptor: DriverDescriptor, context_name: str) -> bool:
    normalized_context = context_name.strip()
    return any(fnmatchcase(normalized_context, pattern) for pattern in descriptor.context_patterns)


def _optional_call(method: Any, **kwargs: object) -> object | None:
    try:
        return method(**kwargs)
    except FileNotFoundError:
        return None


def _read_lane_summary(
    *, record_store: object, context_name: str, instance_name: str
) -> LaunchplaneLaneSummary | None:
    if not instance_name:
        return None
    if hasattr(record_store, "read_lane_summary"):
        return getattr(record_store, "read_lane_summary")(
            context_name=context_name,
            instance_name=instance_name,
        )

    inventory = None
    if hasattr(record_store, "read_environment_inventory"):
        inventory = _optional_call(
            getattr(record_store, "read_environment_inventory"),
            context_name=context_name,
            instance_name=instance_name,
        )
    release_tuple = None
    if hasattr(record_store, "read_release_tuple_record"):
        release_tuple = _optional_call(
            getattr(record_store, "read_release_tuple_record"),
            context_name=context_name,
            channel_name=instance_name,
        )
    latest_deployment = None
    if hasattr(record_store, "list_deployment_records"):
        latest_deployment = next(
            iter(
                getattr(record_store, "list_deployment_records")(
                    context_name=context_name,
                    instance_name=instance_name,
                    limit=1,
                )
            ),
            None,
        )
    latest_promotion = None
    if hasattr(record_store, "list_promotion_records"):
        latest_promotion = next(
            iter(
                getattr(record_store, "list_promotion_records")(
                    context_name=context_name,
                    to_instance_name=instance_name,
                    limit=1,
                )
            ),
            None,
        )
    latest_backup_gate = None
    if hasattr(record_store, "list_backup_gate_records"):
        latest_backup_gate = next(
            iter(
                getattr(record_store, "list_backup_gate_records")(
                    context_name=context_name,
                    instance_name=instance_name,
                    limit=1,
                )
            ),
            None,
        )
    odoo_instance_override = None
    if hasattr(record_store, "read_odoo_instance_override_record"):
        odoo_instance_override = _optional_call(
            getattr(record_store, "read_odoo_instance_override_record"),
            context_name=context_name,
            instance_name=instance_name,
        )

    return LaunchplaneLaneSummary(
        context=context_name,
        instance=instance_name,
        inventory=inventory,
        release_tuple=release_tuple,
        latest_deployment=latest_deployment,
        latest_promotion=latest_promotion,
        latest_backup_gate=latest_backup_gate,
        odoo_instance_override=odoo_instance_override,
    )


def _list_preview_summaries(
    *, record_store: object, context_name: str
) -> tuple[LaunchplanePreviewSummary, ...]:
    if hasattr(record_store, "list_preview_summaries"):
        return getattr(record_store, "list_preview_summaries")(
            context_name=context_name,
            generation_limit=1,
        )
    if not hasattr(record_store, "list_preview_records"):
        return ()
    previews = getattr(record_store, "list_preview_records")(context_name=context_name, limit=10)
    summaries: list[LaunchplanePreviewSummary] = []
    for preview in previews:
        generations = ()
        if hasattr(record_store, "list_preview_generation_records"):
            generations = getattr(record_store, "list_preview_generation_records")(
                preview_id=preview.preview_id,
                limit=1,
            )
        summaries.append(
            LaunchplanePreviewSummary(
                preview=preview,
                latest_generation=next(iter(generations), None),
                recent_generations=generations,
            )
        )
    return tuple(summaries)


def build_driver_context_view(
    *,
    record_store: object,
    context_name: str,
    instance_name: str = "",
) -> DriverContextView:
    drivers: list[DriverView] = []
    for descriptor in _DESCRIPTORS:
        if not _driver_matches_context(descriptor=descriptor, context_name=context_name):
            continue
        lane_summary = _read_lane_summary(
            record_store=record_store,
            context_name=context_name,
            instance_name=instance_name,
        )
        preview_summaries = ()
        if descriptor.driver_id == "verireel":
            preview_summaries = _list_preview_summaries(
                record_store=record_store,
                context_name=context_name,
            )
        drivers.append(
            DriverView(
                driver_id=descriptor.driver_id,
                descriptor=descriptor,
                available_actions=descriptor.actions,
                lane_summary=lane_summary,
                preview_summaries=preview_summaries,
            )
        )
    return DriverContextView(context=context_name, instance=instance_name, drivers=tuple(drivers))
