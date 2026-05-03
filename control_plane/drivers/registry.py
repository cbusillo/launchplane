from __future__ import annotations

from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from typing import Literal, Protocol, cast

from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.data_provenance import DataProvenance, FreshnessStatus
from control_plane.contracts.deployment_record import DeploymentRecord
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
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.lane_summary import LaunchplaneLaneSummary
from control_plane.contracts.odoo_instance_override_record import OdooInstanceOverrideRecord
from control_plane.contracts.preview_generation_record import PreviewGenerationRecord
from control_plane.contracts.preview_inventory_scan_record import PreviewInventoryScanRecord
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.preview_summary import LaunchplanePreviewSummary
from control_plane.contracts.promotion_record import PromotionRecord
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord


PROVIDER_BOUNDARY_NOTE = (
    "Launchplane exposes product capabilities and evidence; runtime-provider details stay behind "
    "backend adapters and evidence records."
)
LANE_STALE_AFTER = timedelta(minutes=30)
PREVIEW_STALE_AFTER = timedelta(minutes=30)
PREVIEW_CAPABILITY_IDS = {"preview_lifecycle", "previewable", "preview_inventory_managed"}


class _ListProductProfileRecords(Protocol):
    def __call__(
        self, *, driver_id: str | None = None
    ) -> tuple[LaunchplaneProductProfileRecord, ...]: ...


class _ReadLaneSummary(Protocol):
    def __call__(self, *, context_name: str, instance_name: str) -> LaunchplaneLaneSummary: ...


class _ReadEnvironmentInventory(Protocol):
    def __call__(self, *, context_name: str, instance_name: str) -> EnvironmentInventory: ...


class _ReadReleaseTupleRecord(Protocol):
    def __call__(self, *, context_name: str, channel_name: str) -> ReleaseTupleRecord: ...


class _ListDeploymentRecords(Protocol):
    def __call__(
        self, *, context_name: str = "", instance_name: str = "", limit: int | None = None
    ) -> tuple[DeploymentRecord, ...]: ...


class _ListPromotionRecords(Protocol):
    def __call__(
        self, *, context_name: str = "", to_instance_name: str = "", limit: int | None = None
    ) -> tuple[PromotionRecord, ...]: ...


class _ListBackupGateRecords(Protocol):
    def __call__(
        self, *, context_name: str = "", instance_name: str = "", limit: int | None = None
    ) -> tuple[BackupGateRecord, ...]: ...


class _ReadOdooInstanceOverrideRecord(Protocol):
    def __call__(self, *, context_name: str, instance_name: str) -> OdooInstanceOverrideRecord: ...


class _ListPreviewInventoryScanRecords(Protocol):
    def __call__(
        self, *, context_name: str = "", limit: int | None = None
    ) -> tuple[PreviewInventoryScanRecord, ...]: ...


class _ListPreviewSummaries(Protocol):
    def __call__(
        self, *, context_name: str = "", generation_limit: int | None = 1
    ) -> tuple[LaunchplanePreviewSummary, ...]: ...


class _ListPreviewRecords(Protocol):
    def __call__(
        self, *, context_name: str = "", limit: int | None = None
    ) -> tuple[PreviewRecord, ...]: ...


class _ListPreviewGenerationRecords(Protocol):
    def __call__(
        self, *, preview_id: str = "", limit: int | None = None
    ) -> tuple[PreviewGenerationRecord, ...]: ...


def _callable_store_method(record_store: object, method_name: str) -> object | None:
    method = getattr(record_store, method_name, None)
    if not callable(method):
        return None
    return cast(object, method)


def _list_product_profile_records_method(
    record_store: object,
) -> _ListProductProfileRecords | None:
    return cast(
        _ListProductProfileRecords | None,
        _callable_store_method(record_store, "list_product_profile_records"),
    )


def _read_lane_summary_method(record_store: object) -> _ReadLaneSummary | None:
    return cast(
        _ReadLaneSummary | None,
        _callable_store_method(record_store, "read_lane_summary"),
    )


def _read_environment_inventory_method(
    record_store: object,
) -> _ReadEnvironmentInventory | None:
    return cast(
        _ReadEnvironmentInventory | None,
        _callable_store_method(record_store, "read_environment_inventory"),
    )


def _read_release_tuple_record_method(record_store: object) -> _ReadReleaseTupleRecord | None:
    return cast(
        _ReadReleaseTupleRecord | None,
        _callable_store_method(record_store, "read_release_tuple_record"),
    )


def _list_deployment_records_method(record_store: object) -> _ListDeploymentRecords | None:
    return cast(
        _ListDeploymentRecords | None,
        _callable_store_method(record_store, "list_deployment_records"),
    )


def _list_promotion_records_method(record_store: object) -> _ListPromotionRecords | None:
    return cast(
        _ListPromotionRecords | None,
        _callable_store_method(record_store, "list_promotion_records"),
    )


def _list_backup_gate_records_method(record_store: object) -> _ListBackupGateRecords | None:
    return cast(
        _ListBackupGateRecords | None,
        _callable_store_method(record_store, "list_backup_gate_records"),
    )


def _read_odoo_instance_override_record_method(
    record_store: object,
) -> _ReadOdooInstanceOverrideRecord | None:
    return cast(
        _ReadOdooInstanceOverrideRecord | None,
        _callable_store_method(record_store, "read_odoo_instance_override_record"),
    )


def _list_preview_inventory_scan_records_method(
    record_store: object,
) -> _ListPreviewInventoryScanRecords | None:
    return cast(
        _ListPreviewInventoryScanRecords | None,
        _callable_store_method(record_store, "list_preview_inventory_scan_records"),
    )


def _list_preview_summaries_method(record_store: object) -> _ListPreviewSummaries | None:
    return cast(
        _ListPreviewSummaries | None,
        _callable_store_method(record_store, "list_preview_summaries"),
    )


def _list_preview_records_method(record_store: object) -> _ListPreviewRecords | None:
    return cast(
        _ListPreviewRecords | None,
        _callable_store_method(record_store, "list_preview_records"),
    )


def _list_preview_generation_records_method(
    record_store: object,
) -> _ListPreviewGenerationRecords | None:
    return cast(
        _ListPreviewGenerationRecords | None,
        _callable_store_method(record_store, "list_preview_generation_records"),
    )


def _optional_read_environment_inventory(
    method: _ReadEnvironmentInventory, *, context_name: str, instance_name: str
) -> EnvironmentInventory | None:
    try:
        return method(context_name=context_name, instance_name=instance_name)
    except FileNotFoundError:
        return None


def _optional_read_release_tuple_record(
    method: _ReadReleaseTupleRecord, *, context_name: str, channel_name: str
) -> ReleaseTupleRecord | None:
    try:
        return method(context_name=context_name, channel_name=channel_name)
    except FileNotFoundError:
        return None


def _optional_read_odoo_instance_override_record(
    method: _ReadOdooInstanceOverrideRecord, *, context_name: str, instance_name: str
) -> OdooInstanceOverrideRecord | None:
    try:
        return method(context_name=context_name, instance_name=instance_name)
    except FileNotFoundError:
        return None


def _action(
    action_id: str,
    label: str,
    description: str,
    *,
    safety: DriverActionSafety,
    scope: DriverActionScope,
    route_path: str,
    method: Literal["GET", "POST"] = "POST",
    authz_action: str = "",
    operator_visible: bool = True,
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
        authz_action=authz_action,
        operator_visible=operator_visible,
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
            actions=("stable_deploy", "prod_promotion"),
            panels=("lane_health", "deployment_evidence", "promotion_evidence"),
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
            actions=("preview_inventory", "preview_destroy"),
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
            authz_action="generic_web_deploy.execute",
            writes_records=("deployment",),
        ),
        _action(
            "prod_promotion",
            "Promote testing to prod",
            "Promote a generic-web testing image to prod and record promotion health evidence.",
            safety="mutation",
            scope="instance",
            route_path="/v1/drivers/generic-web/prod-promotion",
            authz_action="generic_web_prod_promotion.execute",
            writes_records=("deployment", "promotion", "inventory"),
        ),
        _action(
            "prod_promotion_workflow",
            "Dispatch promote workflow",
            "Dispatch the product-owned GitHub workflow that promotes testing to prod.",
            safety="mutation",
            scope="instance",
            route_path="/v1/drivers/generic-web/prod-promotion-workflow",
            authz_action="generic_web_prod_promotion.dispatch",
            writes_records=(),
        ),
        _action(
            "preview_desired_state",
            "Discover desired previews",
            "Discover labeled pull requests for a generic-web product profile and record desired preview state.",
            safety="safe_write",
            scope="context",
            route_path="/v1/drivers/generic-web/preview-desired-state",
            authz_action="preview_desired_state.discover",
            writes_records=("preview_desired_state",),
        ),
        _action(
            "preview_refresh",
            "Refresh preview",
            "Create or update a generic-web preview application after readiness passes.",
            safety="mutation",
            scope="preview",
            route_path="/v1/drivers/generic-web/preview-refresh",
            authz_action="preview_refresh.execute",
        ),
        _action(
            "preview_inventory",
            "Read preview inventory",
            "Scan provider state for active generic-web previews and record inventory evidence.",
            safety="safe_write",
            scope="context",
            route_path="/v1/drivers/generic-web/preview-inventory",
            authz_action="preview_inventory.read",
            writes_records=("preview_inventory_scan",),
        ),
        _action(
            "preview_readiness",
            "Evaluate preview readiness",
            "Validate generic-web preview template settings before provider mutation.",
            safety="read",
            scope="context",
            route_path="/v1/drivers/generic-web/preview-readiness",
            authz_action="preview_readiness.evaluate",
        ),
        _action(
            "preview_destroy",
            "Destroy preview",
            "Destroy a generic-web preview application and record cleanup evidence.",
            safety="destructive",
            scope="preview",
            route_path="/v1/drivers/generic-web/preview-destroy",
            authz_action="preview_destroy.execute",
            writes_records=("preview",),
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
            authz_action="odoo_artifact_publish_inputs.read",
        ),
        _action(
            "artifact_publish",
            "Record artifact",
            "Persist a product-built immutable artifact manifest as Launchplane release evidence.",
            safety="safe_write",
            scope="instance",
            route_path="/v1/drivers/odoo/artifact-publish",
            authz_action="odoo_artifact_publish.write",
            writes_records=("artifact_manifest",),
        ),
        _action(
            "post_deploy",
            "Apply post-deploy settings",
            "Apply typed Odoo instance settings through the product driver after a deployment.",
            safety="mutation",
            scope="instance",
            route_path="/v1/drivers/odoo/post-deploy",
            authz_action="odoo_post_deploy.execute",
            writes_records=("odoo_instance_override",),
        ),
        _action(
            "prod_backup_gate",
            "Capture prod backup gate",
            "Capture concrete backup evidence before a prod-changing action.",
            safety="safe_write",
            scope="instance",
            route_path="/v1/drivers/odoo/prod-backup-gate",
            authz_action="odoo_prod_backup_gate.execute",
            writes_records=("backup_gate",),
        ),
        _action(
            "prod_promotion",
            "Promote testing to prod",
            "Promote a stored testing artifact to prod after backup-gate evidence passes.",
            safety="mutation",
            scope="instance",
            route_path="/v1/drivers/odoo/prod-promotion",
            authz_action="odoo_prod_promotion.execute",
            writes_records=("deployment", "promotion", "inventory", "release_tuple"),
        ),
        _action(
            "prod_rollback",
            "Roll back prod",
            "Roll prod back to an explicit stored artifact and record health evidence.",
            safety="destructive",
            scope="instance",
            route_path="/v1/drivers/odoo/prod-rollback",
            authz_action="odoo_prod_rollback.execute",
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
            actions=(
                "testing_deploy",
                "prod_deploy",
                "stable_environment",
                "runtime_verification",
                "app_maintenance",
            ),
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
            authz_action="verireel_testing_deploy.execute",
            writes_records=("deployment", "inventory", "release_tuple"),
        ),
        _action(
            "testing_verification",
            "Record testing verification",
            "Record VeriReel product smoke verification for a testing deployment.",
            safety="safe_write",
            scope="instance",
            route_path="/v1/drivers/verireel/testing-verification",
            authz_action="deployment.write",
            operator_visible=False,
            writes_records=("deployment",),
        ),
        _action(
            "stable_environment",
            "Read stable environment",
            "Resolve DB-backed stable environment settings for driver execution.",
            safety="read",
            scope="instance",
            route_path="/v1/drivers/verireel/stable-environment",
            authz_action="verireel_stable_environment.read",
        ),
        _action(
            "runtime_verification",
            "Verify stable runtime",
            "Verify stable VeriReel health and public page responses through Launchplane.",
            safety="read",
            scope="instance",
            route_path="/v1/drivers/verireel/runtime-verification",
            authz_action="verireel_stable_environment.read",
        ),
        _action(
            "app_maintenance",
            "Run app maintenance",
            "Run an allow-listed product maintenance operation for a stable or preview environment.",
            safety="mutation",
            scope="instance",
            route_path="/v1/drivers/verireel/app-maintenance",
            authz_action="verireel_app_maintenance.execute",
        ),
        _action(
            "prod_deploy",
            "Deploy prod",
            "Deploy a stored artifact directly to prod and record evidence.",
            safety="mutation",
            scope="instance",
            route_path="/v1/drivers/verireel/prod-deploy",
            authz_action="verireel_prod_deploy.execute",
            writes_records=("deployment", "inventory", "release_tuple"),
        ),
        _action(
            "prod_backup_gate",
            "Capture prod backup gate",
            "Capture backup evidence before a prod-changing action.",
            safety="safe_write",
            scope="instance",
            route_path="/v1/drivers/verireel/prod-backup-gate",
            authz_action="verireel_prod_backup_gate.execute",
            writes_records=("backup_gate",),
        ),
        _action(
            "prod_promotion",
            "Promote testing to prod",
            "Promote a stored testing artifact to prod after backup-gate evidence passes.",
            safety="mutation",
            scope="instance",
            route_path="/v1/drivers/verireel/prod-promotion",
            authz_action="verireel_prod_promotion.execute",
            writes_records=("deployment", "promotion", "inventory", "release_tuple"),
        ),
        _action(
            "prod_rollback",
            "Roll back prod",
            "Roll prod back to an explicit stored artifact and record health evidence.",
            safety="destructive",
            scope="instance",
            route_path="/v1/drivers/verireel/prod-rollback",
            authz_action="verireel_prod_rollback.execute",
            writes_records=("deployment", "promotion", "inventory", "release_tuple"),
        ),
        _action(
            "preview_refresh",
            "Refresh preview",
            "Refresh an ephemeral preview environment and return follow-up evidence instructions.",
            safety="mutation",
            scope="preview",
            route_path="/v1/drivers/verireel/preview-refresh",
            authz_action="verireel_preview_refresh.execute",
            writes_records=("preview", "preview_generation"),
        ),
        _action(
            "preview_inventory",
            "Read preview inventory",
            "Read preview inventory and current preview lifecycle evidence.",
            safety="read",
            scope="context",
            route_path="/v1/drivers/verireel/preview-inventory",
            authz_action="verireel_preview_inventory.read",
        ),
        _action(
            "preview_destroy",
            "Destroy preview",
            "Destroy an ephemeral preview and record cleanup evidence.",
            safety="destructive",
            scope="preview",
            route_path="/v1/drivers/verireel/preview-destroy",
            authz_action="verireel_preview_destroy.execute",
            writes_records=("preview", "preview_generation"),
        ),
        _action(
            "preview_verification",
            "Record preview verification",
            "Record VeriReel product smoke verification for the latest preview generation.",
            safety="safe_write",
            scope="preview",
            route_path="/v1/drivers/verireel/preview-verification",
            authz_action="preview_generation.write",
            operator_visible=False,
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


def _product_profile_lanes(profile: LaunchplaneProductProfileRecord) -> tuple[str, ...]:
    contexts = {lane.context.strip() for lane in profile.lanes if lane.context.strip()}
    if profile.preview.enabled and profile.preview.context.strip():
        contexts.add(profile.preview.context.strip())
    return tuple(sorted(contexts))


def _product_profile_matches_context(
    *, profile: LaunchplaneProductProfileRecord, context_name: str
) -> bool:
    normalized_context = context_name.strip()
    return normalized_context in _product_profile_lanes(profile)


def _descriptor_for_product_profile(
    *, descriptor: DriverDescriptor, profile: LaunchplaneProductProfileRecord
) -> DriverDescriptor:
    return descriptor.model_copy(
        update={
            "driver_id": profile.product,
            "base_driver_id": descriptor.driver_id,
            "label": profile.display_name,
            "product": profile.product,
            "description": f"{profile.display_name} generic-web lifecycle.",
            "context_patterns": _product_profile_lanes(profile),
        }
    )


def _product_profile_descriptors(
    *, record_store: object, descriptor: DriverDescriptor, context_name: str
) -> tuple[DriverDescriptor, ...]:
    list_profiles = _list_product_profile_records_method(record_store)
    if list_profiles is None:
        return ()
    try:
        profiles = list_profiles(driver_id=descriptor.driver_id)
    except FileNotFoundError:
        return ()
    return tuple(
        _descriptor_for_product_profile(descriptor=descriptor, profile=profile)
        for profile in profiles
        if _product_profile_matches_context(profile=profile, context_name=context_name)
    )


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
    list_scans = _list_preview_inventory_scan_records_method(record_store)
    if list_scans is not None:
        scans = list_scans(context_name=context_name, limit=1)
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
    read_summary = _read_lane_summary_method(record_store)
    if read_summary is not None:
        summary = read_summary(
            context_name=context_name,
            instance_name=instance_name,
        )
        return summary.model_copy(update={"provenance": _lane_provenance(summary)})

    inventory = None
    read_inventory = _read_environment_inventory_method(record_store)
    if read_inventory is not None:
        inventory = _optional_read_environment_inventory(
            read_inventory,
            context_name=context_name,
            instance_name=instance_name,
        )
    release_tuple = None
    read_release_tuple = _read_release_tuple_record_method(record_store)
    if read_release_tuple is not None:
        release_tuple = _optional_read_release_tuple_record(
            read_release_tuple,
            context_name=context_name,
            channel_name=instance_name,
        )
    latest_deployment = None
    list_deployments = _list_deployment_records_method(record_store)
    if list_deployments is not None:
        latest_deployment = next(
            iter(list_deployments(context_name=context_name, instance_name=instance_name, limit=1)),
            None,
        )
    latest_promotion = None
    list_promotions = _list_promotion_records_method(record_store)
    if list_promotions is not None:
        latest_promotion = next(
            iter(
                list_promotions(context_name=context_name, to_instance_name=instance_name, limit=1)
            ),
            None,
        )
    latest_backup_gate = None
    list_backup_gates = _list_backup_gate_records_method(record_store)
    if list_backup_gates is not None:
        latest_backup_gate = next(
            iter(
                list_backup_gates(context_name=context_name, instance_name=instance_name, limit=1)
            ),
            None,
        )
    odoo_instance_override = None
    read_odoo_instance_override = _read_odoo_instance_override_record_method(record_store)
    if read_odoo_instance_override is not None:
        odoo_instance_override = _optional_read_odoo_instance_override_record(
            read_odoo_instance_override,
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
    list_summaries = _list_preview_summaries_method(record_store)
    if list_summaries is not None:
        preview_summaries = list_summaries(
            context_name=context_name,
            generation_limit=1,
        )
        return tuple(
            summary.model_copy(update={"provenance": _preview_provenance(summary)})
            for summary in preview_summaries
        )
    list_previews = _list_preview_records_method(record_store)
    if list_previews is None:
        return ()
    previews = list_previews(context_name=context_name, limit=10)
    summaries: list[LaunchplanePreviewSummary] = []
    list_generations = _list_preview_generation_records_method(record_store)
    for preview in previews:
        generations: tuple[PreviewGenerationRecord, ...] = ()
        if list_generations is not None:
            generations = list_generations(
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
        matched_descriptors: tuple[DriverDescriptor, ...]
        if _driver_matches_context(descriptor=descriptor, context_name=context_name):
            matched_descriptors = (descriptor,)
        else:
            matched_descriptors = _product_profile_descriptors(
                record_store=record_store,
                descriptor=descriptor,
                context_name=context_name,
            )
        if not matched_descriptors:
            continue
        for matched_descriptor in matched_descriptors:
            lane_summary = _read_lane_summary(
                record_store=record_store,
                context_name=context_name,
                instance_name=instance_name,
            )
            preview_summaries: tuple[LaunchplanePreviewSummary, ...] = ()
            if _driver_exposes_preview_inventory(matched_descriptor):
                preview_summaries = _list_preview_summaries(
                    record_store=record_store,
                    context_name=context_name,
                )
            preview_inventory_provenance = None
            if _driver_exposes_preview_inventory(matched_descriptor):
                preview_inventory_provenance = _preview_inventory_provenance(
                    record_store=record_store,
                    context_name=context_name,
                    preview_summaries=preview_summaries,
                )
            drivers.append(
                DriverView(
                    driver_id=matched_descriptor.driver_id,
                    descriptor=matched_descriptor,
                    available_actions=matched_descriptor.actions,
                    lane_summary=lane_summary,
                    preview_summaries=preview_summaries,
                    preview_inventory_provenance=preview_inventory_provenance,
                )
            )
    return DriverContextView(context=context_name, instance=instance_name, drivers=tuple(drivers))
