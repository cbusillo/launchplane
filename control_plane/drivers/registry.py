from __future__ import annotations

from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from typing import Any, Literal

from control_plane.contracts.data_provenance import DataProvenance, FreshnessStatus
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
LANE_STALE_AFTER = timedelta(minutes=30)
PREVIEW_STALE_AFTER = timedelta(minutes=30)
PREVIEW_CAPABILITY_IDS = {"preview_lifecycle", "previewable", "preview_inventory_managed"}


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


GENERIC_WEB_DRIVER = DriverDescriptor(
    driver_id="generic-web",
    label="Generic web",
    product="generic-web",
    description=(
        "Reusable containerized web-app lifecycle driver for image deploys, "
        "health checks, previews, and PR feedback."
    ),
    context_patterns=(),
    provider_boundary=PROVIDER_BOUNDARY_NOTE,
    capabilities=(
        DriverCapabilityDescriptor(
            capability_id="image_deployable",
            label="Image deployable",
            description="Deploy immutable container images and record stable-lane deployment evidence.",
            actions=("stable_deploy",),
            panels=("lane_health", "deployment_evidence"),
        ),
        DriverCapabilityDescriptor(
            capability_id="health_checked",
            label="Health checked",
            description="Verify HTTP health endpoints and surface freshness through Launchplane read models.",
            panels=("lane_health",),
        ),
        DriverCapabilityDescriptor(
            capability_id="previewable",
            label="Previewable",
            description="Model desired preview state, creation, refresh, cleanup, and preview evidence for web products.",
            actions=("preview_desired_state",),
            panels=("preview_inventory", "deployment_evidence", "audit"),
        ),
        DriverCapabilityDescriptor(
            capability_id="preview_inventory_managed",
            label="Preview inventory managed",
            description="Read provider inventory and reconcile current preview state through Launchplane records.",
            panels=("preview_inventory", "deployment_evidence", "audit"),
        ),
        DriverCapabilityDescriptor(
            capability_id="pr_feedback",
            label="PR feedback",
            description="Render and persist pull-request feedback from Launchplane preview and deploy records.",
            panels=("audit",),
        ),
    ),
    actions=(
        _action(
            "stable_deploy",
            "Deploy lane",
            "Deploy an immutable container image to a configured generic-web product lane.",
            safety="mutation",
            scope="instance",
            route_path="/v1/drivers/generic-web/deploy",
            writes_records=("deployment",),
        ),
        _action(
            "preview_desired_state",
            "Discover desired previews",
            "Discover labeled pull requests for a generic-web product profile and record desired preview state.",
            safety="safe_write",
            scope="context",
            route_path="/v1/drivers/generic-web/preview-desired-state",
            writes_records=("preview_desired_state",),
        ),
    ),
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
    base_driver_id="generic-web",
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

_DESCRIPTORS: tuple[DriverDescriptor, ...] = (
    GENERIC_WEB_DRIVER,
    ODOO_DRIVER,
    VERIREEL_DRIVER,
)


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


def _parse_timestamp(value: str) -> datetime | None:
    normalized_value = value.strip()
    if not normalized_value:
        return None
    try:
        if normalized_value.endswith("Z"):
            normalized_value = f"{normalized_value[:-1]}+00:00"
        parsed = datetime.fromisoformat(normalized_value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _freshness_status(
    *, recorded_at: str, stale_after: timedelta, verified: bool
) -> tuple[FreshnessStatus, str]:
    recorded_timestamp = _parse_timestamp(recorded_at)
    if recorded_timestamp is None:
        return "recorded", ""
    stale_at = recorded_timestamp + stale_after
    if datetime.now(timezone.utc) > stale_at:
        return "stale", _format_timestamp(stale_at)
    return ("verified" if verified else "recorded"), _format_timestamp(stale_at)


def _lane_provenance(summary: LaunchplaneLaneSummary) -> DataProvenance:
    if summary.inventory is not None:
        status, stale_after = _freshness_status(
            recorded_at=summary.inventory.updated_at,
            stale_after=LANE_STALE_AFTER,
            verified=summary.inventory.destination_health.verified,
        )
        return DataProvenance(
            source_kind="record",
            source_record_id=summary.inventory.deployment_record_id,
            recorded_at=summary.inventory.updated_at,
            refreshed_at=summary.inventory.updated_at,
            freshness_status=status,
            stale_after=stale_after,
            detail="Launchplane environment inventory record.",
        )
    if summary.latest_deployment is not None:
        recorded_at = (
            summary.latest_deployment.deploy.finished_at
            or summary.latest_deployment.deploy.started_at
        )
        status, stale_after = _freshness_status(
            recorded_at=recorded_at,
            stale_after=LANE_STALE_AFTER,
            verified=summary.latest_deployment.destination_health.verified,
        )
        return DataProvenance(
            source_kind="record",
            source_record_id=summary.latest_deployment.record_id,
            recorded_at=recorded_at,
            refreshed_at=recorded_at,
            freshness_status=status,
            stale_after=stale_after,
            detail="Latest Launchplane deployment record; environment inventory is missing.",
        )
    return DataProvenance(
        source_kind="record",
        freshness_status="missing",
        detail="Launchplane has not recorded lane evidence for this context and instance.",
    )


def _preview_provenance(summary: LaunchplanePreviewSummary) -> DataProvenance:
    if summary.latest_generation is not None:
        generation = summary.latest_generation
        recorded_at = generation.finished_at or generation.ready_at or generation.requested_at
        status, stale_after = _freshness_status(
            recorded_at=recorded_at,
            stale_after=PREVIEW_STALE_AFTER,
            verified=generation.overall_health_status == "pass",
        )
        return DataProvenance(
            source_kind="record",
            source_record_id=generation.generation_id,
            recorded_at=recorded_at,
            refreshed_at=recorded_at,
            freshness_status=status,
            stale_after=stale_after,
            detail="Latest Launchplane preview generation record.",
        )
    status, stale_after = _freshness_status(
        recorded_at=summary.preview.updated_at,
        stale_after=PREVIEW_STALE_AFTER,
        verified=False,
    )
    return DataProvenance(
        source_kind="record",
        source_record_id=summary.preview.preview_id,
        recorded_at=summary.preview.updated_at,
        refreshed_at=summary.preview.updated_at,
        freshness_status=status,
        stale_after=stale_after,
        detail="Preview identity record exists, but no generation evidence is recorded.",
    )


def _preview_inventory_provenance(
    *,
    record_store: object,
    context_name: str,
    preview_summaries: tuple[LaunchplanePreviewSummary, ...],
) -> DataProvenance:
    if hasattr(record_store, "list_preview_inventory_scan_records"):
        scans = getattr(record_store, "list_preview_inventory_scan_records")(
            context_name=context_name,
            limit=1,
        )
        latest_scan = next(iter(scans), None)
        if latest_scan is not None:
            status, stale_after = _freshness_status(
                recorded_at=latest_scan.scanned_at,
                stale_after=PREVIEW_STALE_AFTER,
                verified=latest_scan.status == "pass",
            )
            return DataProvenance(
                source_kind="record",
                source_record_id=latest_scan.scan_id,
                recorded_at=latest_scan.scanned_at,
                refreshed_at=latest_scan.scanned_at,
                freshness_status=status,
                stale_after=stale_after,
                detail=(
                    "Latest Launchplane preview inventory scan; "
                    f"{latest_scan.preview_count} preview(s) observed."
                ),
            )
    latest_preview = next(iter(preview_summaries), None)
    if latest_preview is not None:
        return latest_preview.provenance
    return DataProvenance(
        source_kind="record",
        freshness_status="missing",
        detail="Launchplane has not recorded a preview inventory scan for this context.",
    )


def _read_lane_summary(
    *, record_store: object, context_name: str, instance_name: str
) -> LaunchplaneLaneSummary | None:
    if not instance_name:
        return None
    if hasattr(record_store, "read_lane_summary"):
        summary = getattr(record_store, "read_lane_summary")(
            context_name=context_name,
            instance_name=instance_name,
        )
        return summary.model_copy(update={"provenance": _lane_provenance(summary)})

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

    summary = LaunchplaneLaneSummary(
        context=context_name,
        instance=instance_name,
        inventory=inventory,
        release_tuple=release_tuple,
        latest_deployment=latest_deployment,
        latest_promotion=latest_promotion,
        latest_backup_gate=latest_backup_gate,
        odoo_instance_override=odoo_instance_override,
    )
    return summary.model_copy(update={"provenance": _lane_provenance(summary)})


def _list_preview_summaries(
    *, record_store: object, context_name: str
) -> tuple[LaunchplanePreviewSummary, ...]:
    if hasattr(record_store, "list_preview_summaries"):
        summaries = getattr(record_store, "list_preview_summaries")(
            context_name=context_name,
            generation_limit=1,
        )
        return tuple(
            summary.model_copy(update={"provenance": _preview_provenance(summary)})
            for summary in summaries
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
        summary = LaunchplanePreviewSummary(
            preview=preview,
            latest_generation=next(iter(generations), None),
            recent_generations=generations,
        )
        summaries.append(summary.model_copy(update={"provenance": _preview_provenance(summary)}))
    return tuple(summaries)


def _driver_exposes_preview_inventory(descriptor: DriverDescriptor) -> bool:
    capability_ids = {capability.capability_id for capability in descriptor.capabilities}
    if capability_ids.intersection(PREVIEW_CAPABILITY_IDS):
        return True
    return any("preview_inventory" in capability.panels for capability in descriptor.capabilities)


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
        if _driver_exposes_preview_inventory(descriptor):
            preview_summaries = _list_preview_summaries(
                record_store=record_store,
                context_name=context_name,
            )
        preview_inventory_provenance = None
        if _driver_exposes_preview_inventory(descriptor):
            preview_inventory_provenance = _preview_inventory_provenance(
                record_store=record_store,
                context_name=context_name,
                preview_summaries=preview_summaries,
            )
        drivers.append(
            DriverView(
                driver_id=descriptor.driver_id,
                descriptor=descriptor,
                available_actions=descriptor.actions,
                lane_summary=lane_summary,
                preview_summaries=preview_summaries,
                preview_inventory_provenance=preview_inventory_provenance,
            )
        )
    return DriverContextView(context=context_name, instance=instance_name, drivers=tuple(drivers))
