from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Literal, Protocol

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane import dokploy as control_plane_dokploy
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.dokploy import JsonObject, JsonValue


class OdooStableTargetReplacementStore(Protocol):
    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord: ...

    def read_dokploy_target_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetRecord: ...

    def read_dokploy_target_id_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetIdRecord: ...

    def read_environment_inventory(
        self, *, context_name: str, instance_name: str
    ) -> EnvironmentInventory: ...


DokployRequest = Callable[..., JsonValue]


class OdooStableTargetReplacementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    instance: str
    strategy: Literal["recreate-in-place", "replace-and-cutover"] = "recreate-in-place"
    allow_empty_data: bool = False

    @model_validator(mode="after")
    def _validate_request(self) -> "OdooStableTargetReplacementRequest":
        self.product = self.product.strip()
        self.instance = self.instance.strip().lower()
        if not self.product:
            raise ValueError("Odoo stable target replacement requires product.")
        if not self.instance:
            raise ValueError("Odoo stable target replacement requires instance.")
        return self


class OdooStableTargetRuntimeSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_type: str
    target_id: str
    target_name: str
    project_name: str = ""
    source_type: str = ""
    compose_path: str = ""
    compose_file_sha256: str = ""
    domain_hosts: tuple[str, ...] = ()
    latest_deployment_status: str = ""
    latest_deployment_id: str = ""
    env_keys: tuple[str, ...] = ()
    required_volume_keys_present: tuple[str, ...] = ()
    required_volume_keys_missing: tuple[str, ...] = ()
    runtime_identity_present: bool = False
    runtime_identity_deployment_record_id: str = ""


class OdooStableTargetReplacementStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str
    status: Literal["ready", "blocked", "manual"]
    message: str


class OdooStableTargetReplacementPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_status: Literal["ready", "blocked"]
    product: str
    context: str
    instance: str
    strategy: Literal["recreate-in-place", "replace-and-cutover"]
    target_record_found: bool
    target_id_record_found: bool
    inventory_found: bool
    current_target: OdooStableTargetRuntimeSnapshot | None = None
    expected_next_target_name: str
    expected_domain_hosts: tuple[str, ...] = ()
    expected_artifact_id: str = ""
    expected_source_git_ref: str = ""
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    steps: tuple[OdooStableTargetReplacementStep, ...] = ()


def _read_lane(
    *, profile: LaunchplaneProductProfileRecord, instance: str
) -> ProductLaneProfile:
    for lane in profile.lanes:
        if lane.instance == instance:
            return lane
    raise click.ClickException(
        f"Product {profile.product!r} has no stable lane for instance {instance!r}."
    )


def _read_optional(call: Callable[[], object]) -> object | None:
    try:
        return call()
    except FileNotFoundError:
        return None


def _domain_hosts_from_payload(raw_domains: JsonValue) -> tuple[str, ...]:
    if not isinstance(raw_domains, list):
        return ()
    hosts: list[str] = []
    for raw_domain in raw_domains:
        domain = control_plane_dokploy.as_json_object(raw_domain)
        if domain is None:
            continue
        host = str(domain.get("host") or "").strip()
        if host:
            hosts.append(host)
    return tuple(sorted(hosts))


def _domains_for_target(
    *,
    host: str,
    token: str,
    target_type: str,
    target_id: str,
    request: DokployRequest,
) -> tuple[str, ...]:
    if target_type == "compose":
        return _domain_hosts_from_payload(
            request(
                host=host,
                token=token,
                path="/api/domain.byComposeId",
                query={"composeId": target_id},
            )
        )
    if target_type == "application":
        return _domain_hosts_from_payload(
            request(
                host=host,
                token=token,
                path="/api/domain.byApplicationId",
                query={"applicationId": target_id},
            )
        )
    return ()


def _deployment_value(deployment: JsonObject | None, *keys: str) -> str:
    if deployment is None:
        return ""
    for key in keys:
        value = str(deployment.get(key) or "").strip()
        if value:
            return value
    return ""


def _runtime_identity_map(env_map: Mapping[str, str]) -> dict[str, str]:
    raw_identity = env_map.get("LAUNCHPLANE_RUNTIME_IDENTITY_JSON", "").strip()
    if not raw_identity:
        return {}
    try:
        import json

        payload = json.loads(raw_identity)
    except ValueError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key): str(value) for key, value in payload.items() if value is not None}


def _snapshot_current_target(
    *,
    host: str,
    token: str,
    target_record: DokployTargetRecord,
    target_id_record: DokployTargetIdRecord,
    request: DokployRequest,
) -> OdooStableTargetRuntimeSnapshot:
    target_payload = control_plane_dokploy.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type=target_record.target_type,
        target_id=target_id_record.target_id,
    )
    env_map = control_plane_dokploy.parse_dokploy_env_text(
        str(target_payload.get("env") or "")
    )
    required_volume_keys = ("ODOO_DATA_VOLUME", "ODOO_LOG_VOLUME", "ODOO_DB_VOLUME")
    present_volume_keys = tuple(key for key in required_volume_keys if env_map.get(key, "").strip())
    missing_volume_keys = tuple(key for key in required_volume_keys if key not in present_volume_keys)
    latest_deployment = control_plane_dokploy.latest_deployment_for_target(
        host=host,
        token=token,
        target_type=target_record.target_type,
        target_id=target_id_record.target_id,
    )
    runtime_identity = _runtime_identity_map(env_map)
    compose_file = str(target_payload.get("composeFile") or "")
    return OdooStableTargetRuntimeSnapshot(
        target_type=target_record.target_type,
        target_id=target_id_record.target_id,
        target_name=target_record.target_name or str(target_payload.get("name") or "").strip(),
        project_name=target_record.project_name,
        source_type=str(target_payload.get("sourceType") or target_record.source_type or "").strip(),
        compose_path=str(target_payload.get("composePath") or target_record.compose_path or "").strip(),
        compose_file_sha256=control_plane_dokploy.compose_file_sha256(compose_file)
        if compose_file
        else "",
        domain_hosts=_domains_for_target(
            host=host,
            token=token,
            target_type=target_record.target_type,
            target_id=target_id_record.target_id,
            request=request,
        ),
        latest_deployment_status=_deployment_value(latest_deployment, "status", "state"),
        latest_deployment_id=_deployment_value(
            latest_deployment, "deploymentId", "deployment_id", "id"
        ),
        env_keys=tuple(sorted(env_map.keys())),
        required_volume_keys_present=present_volume_keys,
        required_volume_keys_missing=missing_volume_keys,
        runtime_identity_present=bool(runtime_identity),
        runtime_identity_deployment_record_id=runtime_identity.get("deployment_record_id", ""),
    )


def _build_steps(
    *,
    current_target: OdooStableTargetRuntimeSnapshot | None,
    blockers: tuple[str, ...],
    plan_strategy: Literal["recreate-in-place", "replace-and-cutover"],
) -> tuple[OdooStableTargetReplacementStep, ...]:
    steps: list[OdooStableTargetReplacementStep] = []
    steps.append(
        OdooStableTargetReplacementStep(
            step_id="inventory",
            status="ready" if current_target is not None else "blocked",
            message="Read current Launchplane target record and live Dokploy payload.",
        )
    )
    steps.append(
        OdooStableTargetReplacementStep(
            step_id="volume-contract",
            status="ready"
            if current_target is not None and not current_target.required_volume_keys_missing
            else "blocked",
            message="Confirm Odoo data/log/database volume keys are explicit in target env.",
        )
    )
    steps.append(
        OdooStableTargetReplacementStep(
            step_id="replacement-apply",
            status="manual" if not blockers else "blocked",
            message=(
                "Future apply should create a replacement target and cut routes over."
                if plan_strategy == "replace-and-cutover"
                else "Future apply should recreate the target only after route and volume proof."
            ),
        )
    )
    steps.append(
        OdooStableTargetReplacementStep(
            step_id="post-deploy-verification",
            status="manual" if not blockers else "blocked",
            message="Run Odoo post-deploy, health, canonical URL, logo, and runtime identity checks.",
        )
    )
    return tuple(steps)


def build_odoo_stable_target_replacement_plan(
    *,
    control_plane_root: Path,
    record_store: OdooStableTargetReplacementStore,
    request: OdooStableTargetReplacementRequest,
    dokploy_request: DokployRequest = control_plane_dokploy.dokploy_request,
) -> OdooStableTargetReplacementPlan:
    profile = record_store.read_product_profile_record(request.product)
    if profile.driver_id != "odoo":
        raise click.ClickException(
            f"Product {profile.product!r} is configured for driver {profile.driver_id!r}, not odoo."
        )
    lane = _read_lane(profile=profile, instance=request.instance)
    target_record = _read_optional(
        lambda: record_store.read_dokploy_target_record(
            context_name=lane.context, instance_name=lane.instance
        )
    )
    target_id_record = _read_optional(
        lambda: record_store.read_dokploy_target_id_record(
            context_name=lane.context, instance_name=lane.instance
        )
    )
    inventory = _read_optional(
        lambda: record_store.read_environment_inventory(
            context_name=lane.context, instance_name=lane.instance
        )
    )
    blockers: list[str] = []
    warnings: list[str] = []
    current_target: OdooStableTargetRuntimeSnapshot | None = None
    if target_record is None:
        blockers.append("Launchplane has no Dokploy target record for this lane.")
    if target_id_record is None:
        blockers.append("Launchplane has no Dokploy target-id record for this lane.")
    if inventory is None:
        warnings.append("Launchplane has no current environment inventory for this lane.")
    if isinstance(target_record, DokployTargetRecord) and target_record.target_type != "compose":
        blockers.append("Odoo stable replacement currently requires a compose target.")
    if isinstance(target_record, DokployTargetRecord) and isinstance(
        target_id_record, DokployTargetIdRecord
    ):
        host, token = control_plane_dokploy.read_dokploy_config(
            control_plane_root=control_plane_root
        )
        current_target = _snapshot_current_target(
            host=host,
            token=token,
            target_record=target_record,
            target_id_record=target_id_record,
            request=dokploy_request,
        )
        if current_target.required_volume_keys_missing and not request.allow_empty_data:
            blockers.append(
                "Current target is missing required Odoo volume env keys: "
                + ", ".join(current_target.required_volume_keys_missing)
            )
        if not current_target.domain_hosts:
            blockers.append("Current target has no discoverable Dokploy domains to cut over.")
        if not current_target.runtime_identity_present:
            warnings.append("Current target does not expose a Launchplane runtime identity yet.")
    expected_artifact_id = ""
    expected_source_git_ref = ""
    if isinstance(inventory, EnvironmentInventory):
        expected_artifact_id = (
            inventory.artifact_identity.artifact_id if inventory.artifact_identity else ""
        )
        expected_source_git_ref = inventory.source_git_ref
    expected_target_name = (
        target_record.target_name
        if isinstance(target_record, DokployTargetRecord) and target_record.target_name
        else f"{profile.product}-{lane.instance}"
    )
    blockers_tuple = tuple(blockers)
    return OdooStableTargetReplacementPlan(
        plan_status="blocked" if blockers_tuple else "ready",
        product=profile.product,
        context=lane.context,
        instance=lane.instance,
        strategy=request.strategy,
        target_record_found=target_record is not None,
        target_id_record_found=target_id_record is not None,
        inventory_found=inventory is not None,
        current_target=current_target,
        expected_next_target_name=expected_target_name,
        expected_domain_hosts=current_target.domain_hosts if current_target else (),
        expected_artifact_id=expected_artifact_id,
        expected_source_git_ref=expected_source_git_ref,
        blockers=blockers_tuple,
        warnings=tuple(warnings),
        steps=_build_steps(
            current_target=current_target,
            blockers=blockers_tuple,
            plan_strategy=request.strategy,
        ),
    )
