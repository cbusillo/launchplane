from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Literal, Protocol

import click
from pydantic import BaseModel, ConfigDict, Field

from control_plane import odoo_instance_overrides as control_plane_odoo_instance_overrides
from control_plane import live_target_runtime as control_plane_live_target_runtime
from control_plane import release_tuples as control_plane_release_tuples
from control_plane import runtime_environments as control_plane_runtime_environments
from control_plane.contracts.artifact_identity import (
    ArtifactIdentityManifest,
    artifact_manifest_matches_image_repository,
)
from control_plane.contracts.deployment_record import DeploymentRecord, ResolvedTargetEvidence
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.odoo_instance_override_record import OdooInstanceOverrideRecord
from control_plane.contracts.odoo_instance_override_record import OdooOverrideApplyPhase
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.contracts.promotion_record import HealthcheckEvidence, PostDeployUpdateEvidence
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.contracts.runtime_identity import RuntimeIdentity, runtime_identity_env
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.ship_request import ShipRequest
from control_plane.contracts.odoo_stable_target_replacement import (
    LAUNCHPLANE_REQUIRED_ODOO_MODULES,
    OdooStableTargetReplacementApplyRequest,
    OdooStableTargetReplacementApplyResult,
    OdooStableTargetReplacementRequest,
    merge_odoo_install_modules,
    missing_required_odoo_modules_from_artifact,
)
from control_plane.workflows.inventory import build_environment_inventory
from control_plane.workflows.odoo_post_deploy import OdooPostDeployRequest, execute_odoo_post_deploy
from control_plane.workflows.odoo_verification import (
    OdooVerificationEvidence,
    default_odoo_health_url,
    is_legacy_derived_odoo_health_url,
    verify_odoo_stable_readiness,
)
from control_plane.workflows.runtime_identity_health import (
    RuntimeIdentityHealthcheckError,
    healthcheck_evidence_with_runtime_identity,
    wait_for_runtime_identity_healthcheck_with_retry,
)
from control_plane.runtime_key_safety import RuntimeKeySafetyPolicyReadStore
from control_plane.workflows.ship import (
    build_deployment_record,
    generate_deployment_record_id,
    utc_now_timestamp,
)
from control_plane.dokploy import api as dokploy_api
from control_plane.dokploy import source as dokploy_source
from control_plane.dokploy import compose as dokploy_compose
from control_plane.dokploy import post_deploy as dokploy_post_deploy
from control_plane.dokploy.api import JsonObject, JsonValue


class OdooStableTargetReplacementStore(RuntimeKeySafetyPolicyReadStore, Protocol):
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

    def read_artifact_manifest(self, artifact_id: str) -> ArtifactIdentityManifest: ...

    def read_odoo_instance_override_record(
        self, *, context_name: str, instance_name: str
    ) -> OdooInstanceOverrideRecord: ...

    def list_runtime_environment_records(
        self, *, context_name: str = "", instance_name: str = ""
    ) -> tuple[RuntimeEnvironmentRecord, ...]: ...

    def write_deployment_record(self, record: DeploymentRecord) -> object: ...

    def write_environment_inventory(self, record: EnvironmentInventory) -> object: ...

    def write_odoo_instance_override_record(self, record: OdooInstanceOverrideRecord) -> object: ...

    def write_release_tuple_record(self, record: ReleaseTupleRecord) -> object: ...


DokployRequest = Callable[..., JsonValue]
DokployConfigReader = Callable[..., tuple[str, str]]
ODOO_STABLE_TARGET_REPLACEMENT_VERIFY_RETRY_INTERVAL_SECONDS = 5
ODOO_INSTALL_MODULES_ENV_KEY = "ODOO_INSTALL_MODULES"
ODOO_REQUIRED_VOLUME_ENV_KEYS = ("ODOO_DATA_VOLUME", "ODOO_LOG_VOLUME", "ODOO_DB_VOLUME")


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
    live_volume_values: dict[str, str] = Field(default_factory=dict)
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
    allow_empty_data: bool = False
    data_source_mode: Literal["existing", "empty", "upstream_restore"] = "existing"
    approval_issue_url: str = ""
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    steps: tuple[OdooStableTargetReplacementStep, ...] = ()


class _ApplyResultBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    context: str
    instance: str
    deployment_record_id: str
    target_id: str
    target_name: str
    artifact_id: str
    image_reference: str

    def result(
        self,
        *,
        deploy_status: Literal["pass", "fail"],
        release_tuple_id: str = "",
        post_deploy_status: Literal["pass", "fail", "skipped"] = "skipped",
        post_deploy_result: object | None = None,
        health_status: Literal["pass", "fail", "skipped"] = "skipped",
        canonical_status: Literal["pass", "fail", "skipped"] = "skipped",
        logo_status: Literal["pass", "fail", "skipped"] = "skipped",
        health_url: str = "",
        canonical_url: str = "",
        logo_urls: tuple[str, ...] = (),
        verification_evidence: OdooVerificationEvidence | None = None,
        runtime_identity_injected: bool = False,
        runtime_source: dict[str, str] | None = None,
        error_message: str = "",
    ) -> OdooStableTargetReplacementApplyResult:
        post_deploy_payload = post_deploy_result
        return OdooStableTargetReplacementApplyResult(
            product=self.product,
            context=self.context,
            instance=self.instance,
            strategy="recreate-in-place",
            deployment_record_id=self.deployment_record_id,
            release_tuple_id=release_tuple_id,
            deploy_status=deploy_status,
            post_deploy_status=post_deploy_status,
            post_deploy_override_status=getattr(post_deploy_payload, "override_status", "skipped"),
            post_deploy_override_record_found=bool(
                getattr(post_deploy_payload, "override_record_found", False)
            ),
            post_deploy_override_payload_rendered=bool(
                getattr(post_deploy_payload, "override_payload_rendered", False)
            ),
            post_deploy_override_count=int(getattr(post_deploy_payload, "override_count", 0) or 0),
            post_deploy_website_bootstrap_included=bool(
                getattr(post_deploy_payload, "website_bootstrap_included", False)
            ),
            post_deploy_override_evidence=dict(
                getattr(post_deploy_payload, "override_evidence", {}) or {}
            ),
            health_status=health_status,
            canonical_status=canonical_status,
            logo_status=logo_status,
            health_url=health_url,
            canonical_url=canonical_url,
            logo_urls=logo_urls,
            verification_evidence=verification_evidence or OdooVerificationEvidence(),
            runtime_identity_injected=runtime_identity_injected,
            target_id=self.target_id,
            target_name=self.target_name,
            artifact_id=self.artifact_id,
            image_reference=self.image_reference,
            runtime_source=runtime_source or {},
            error_message=error_message,
        )


def _read_lane(*, profile: LaunchplaneProductProfileRecord, instance: str) -> ProductLaneProfile:
    matching_lanes = tuple(lane for lane in profile.lanes if lane.instance == instance)
    if len(matching_lanes) == 1:
        return matching_lanes[0]
    if len(matching_lanes) > 1:
        raise click.ClickException(
            f"Product {profile.product!r} has multiple stable lanes for instance {instance!r}."
        )
    raise click.ClickException(
        f"Product {profile.product!r} has no stable lane for instance {instance!r}."
    )


def _read_optional(call: Callable[[], object]) -> object | None:
    try:
        return call()
    except FileNotFoundError:
        return None


def _raw_compose_route_evidence(
    *,
    compose_file: str,
    domain_hosts: tuple[str, ...],
) -> dict[str, str]:
    evidence: dict[str, str] = {
        "compose_sha256": dokploy_compose.compose_file_sha256(compose_file),
        "domain_hosts": ",".join(domain_hosts),
        "traefik_router_label_count": str(compose_file.count("traefik.http.routers.")),
        "traefik_service_label_count": str(compose_file.count("traefik.http.services.")),
        "traefik_enable_label_present": "true"
        if "traefik.enable=true" in compose_file
        else "false",
        "dokploy_network_label_present": "true"
        if "traefik.docker.network=dokploy-network" in compose_file
        else "false",
    }
    for raw_domain_host in domain_hosts:
        domain_host = raw_domain_host.strip().lower()
        if not domain_host:
            continue
        route_name = dokploy_compose._traefik_route_name(domain_host=domain_host)
        evidence[f"domain_{domain_host}_http_rule_present"] = (
            "true"
            if f"traefik.http.routers.{route_name}-web.rule=Host(`{domain_host}`)" in compose_file
            else "false"
        )
        evidence[f"domain_{domain_host}_https_rule_present"] = (
            "true"
            if f"traefik.http.routers.{route_name}-websecure.rule=Host(`{domain_host}`)"
            in compose_file
            else "false"
        )
    return evidence


def _runtime_source_value(value: object) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return ""
    return str(value)


def _dokploy_domain_route_evidence(
    *,
    raw_domains: JsonValue,
    expected_domain_hosts: tuple[str, ...],
    runtime_port: int,
) -> dict[str, str]:
    domains = _collect_json_objects(raw_domains)
    evidence: dict[str, str] = {
        "domain_record_count": str(len(domains)),
        "domain_hosts": ",".join(
            sorted(
                str(domain.get("host") or "").strip() for domain in domains if domain.get("host")
            )
        ),
    }
    for raw_domain_host in expected_domain_hosts:
        domain_host = raw_domain_host.strip().lower()
        if not domain_host:
            continue
        matching_domain = next(
            (
                domain
                for domain in domains
                if str(domain.get("host") or "").strip().lower() == domain_host
            ),
            None,
        )
        prefix = f"domain_{domain_host}"
        evidence[f"{prefix}_record_present"] = "true" if matching_domain else "false"
        if matching_domain is None:
            continue
        evidence[f"{prefix}_domain_id_present"] = (
            "true" if str(matching_domain.get("domainId") or "").strip() else "false"
        )
        evidence[f"{prefix}_service_name"] = _runtime_source_value(
            matching_domain.get("serviceName")
        )
        evidence[f"{prefix}_port"] = _runtime_source_value(matching_domain.get("port"))
        evidence[f"{prefix}_port_matches_runtime"] = (
            "true"
            if _runtime_source_value(matching_domain.get("port")) == str(runtime_port)
            else "false"
        )
        evidence[f"{prefix}_https"] = _runtime_source_value(matching_domain.get("https"))
        evidence[f"{prefix}_certificate_type"] = _runtime_source_value(
            matching_domain.get("certificateType")
        )
        evidence[f"{prefix}_domain_type"] = _runtime_source_value(matching_domain.get("domainType"))
        evidence[f"{prefix}_path"] = _runtime_source_value(matching_domain.get("path"))
        evidence[f"{prefix}_internal_path"] = _runtime_source_value(
            matching_domain.get("internalPath")
        )
        evidence[f"{prefix}_strip_path"] = _runtime_source_value(matching_domain.get("stripPath"))
        evidence[f"{prefix}_unique_config_key_present"] = (
            "true" if _runtime_source_value(matching_domain.get("uniqueConfigKey")) else "false"
        )
    return evidence


def _dokploy_compose_metadata_evidence(target_payload: JsonObject) -> dict[str, str]:
    deployments = _collect_json_objects(target_payload.get("deployments"))
    latest_deployment = dokploy_api._latest_deployment_from_list(deployments)
    evidence: dict[str, str] = {
        "compose_app_name": _runtime_source_value(target_payload.get("appName")),
        "compose_status": _runtime_source_value(target_payload.get("composeStatus")),
        "compose_type": _runtime_source_value(target_payload.get("composeType")),
        "compose_source_type": _runtime_source_value(target_payload.get("sourceType")),
        "compose_server_id_present": "true"
        if _runtime_source_value(target_payload.get("serverId"))
        else "false",
        "compose_isolated_deployment": _runtime_source_value(
            target_payload.get("isolatedDeployment")
        ),
        "compose_randomize": _runtime_source_value(target_payload.get("randomize")),
        "deployment_record_count": str(len(deployments)),
    }
    if latest_deployment is not None:
        evidence.update(
            {
                "latest_deployment_key": dokploy_api.deployment_key(latest_deployment),
                "latest_deployment_status": _runtime_source_value(latest_deployment.get("status")),
                "latest_deployment_title": _runtime_source_value(latest_deployment.get("title")),
                "latest_deployment_created_at": _runtime_source_value(
                    latest_deployment.get("createdAt") or latest_deployment.get("created_at")
                ),
                "latest_deployment_finished_at": _runtime_source_value(
                    latest_deployment.get("finishedAt") or latest_deployment.get("finished_at")
                ),
            }
        )
    return evidence


def _container_name(container: JsonObject) -> str:
    return _runtime_source_value(
        container.get("name") or container.get("Name") or container.get("Names")
    )


def _container_id(container: JsonObject) -> str:
    return _runtime_source_value(
        container.get("containerId") or container.get("id") or container.get("Id")
    )


def _web_container_for_app(*, containers_payload: JsonValue, app_name: str) -> JsonObject | None:
    containers = _collect_json_objects(containers_payload)
    if not app_name.strip():
        return None
    matching = [container for container in containers if app_name in _container_name(container)]
    if not matching:
        return None
    web_suffixes = ("-web-1", "_web_1", "-web", "_web")
    return next(
        (
            container
            for container in matching
            if any(_container_name(container).endswith(suffix) for suffix in web_suffixes)
        ),
        matching[0],
    )


def _container_config_labels(config_payload: JsonValue) -> dict[str, str]:
    config = dokploy_api.as_json_object(config_payload)
    if config is None:
        return {}
    current: object = config
    for key_name in ("Config", "Labels"):
        if not isinstance(current, dict):
            current = None
            break
        current = current.get(key_name)
    if not isinstance(current, dict):
        return {}
    return {str(key): str(value) for key, value in current.items()}


def _container_config_network_names(config_payload: JsonValue) -> tuple[str, ...]:
    config = dokploy_api.as_json_object(config_payload)
    if config is None:
        return ()
    current: object = config
    for key_name in ("NetworkSettings", "Networks"):
        if not isinstance(current, dict):
            current = None
            break
        current = current.get(key_name)
    if not isinstance(current, dict):
        return ()
    return tuple(sorted(str(key) for key in current))


def _dokploy_container_route_evidence(
    *,
    containers_payload: JsonValue,
    config_payload: JsonValue | None,
    app_name: str,
    server_id: str,
    expected_domain_hosts: tuple[str, ...],
) -> dict[str, str]:
    containers = _collect_json_objects(containers_payload)
    app_containers = [
        container for container in containers if app_name in _container_name(container)
    ]
    web_container = _web_container_for_app(
        containers_payload=containers_payload,
        app_name=app_name,
    )
    labels = _container_config_labels(config_payload) if config_payload is not None else {}
    network_names = (
        _container_config_network_names(config_payload) if config_payload is not None else ()
    )
    evidence: dict[str, str] = {
        "container_app_name": app_name,
        "container_server_id_present": "true" if server_id else "false",
        "container_match_count": str(len(app_containers)),
        "container_web_found": "true" if web_container is not None else "false",
        "container_web_id_present": "true"
        if web_container and _container_id(web_container)
        else "false",
        "container_web_name": _container_name(web_container or {}),
        "container_web_state": _runtime_source_value(
            (web_container or {}).get("state") or (web_container or {}).get("State")
        ),
        "container_web_status": _runtime_source_value(
            (web_container or {}).get("status") or (web_container or {}).get("Status")
        ),
        "container_config_present": "true" if config_payload is not None else "false",
        "container_label_count": str(len(labels)),
        "container_traefik_label_count": str(
            len([key for key in labels if key.startswith("traefik.")])
        ),
        "container_traefik_enable": labels.get("traefik.enable", ""),
        "container_traefik_network": labels.get("traefik.docker.network", ""),
        "container_networks": ",".join(network_names),
        "container_has_dokploy_network": "true" if "dokploy-network" in network_names else "false",
    }
    label_items = tuple(labels.items())
    for raw_domain_host in expected_domain_hosts:
        domain_host = raw_domain_host.strip().lower()
        if not domain_host:
            continue
        route_name = dokploy_compose._traefik_route_name(domain_host=domain_host)
        prefix = f"container_domain_{domain_host}"
        evidence[f"{prefix}_http_rule_present"] = (
            "true"
            if labels.get(f"traefik.http.routers.{route_name}-web.rule") == f"Host(`{domain_host}`)"
            else "false"
        )
        evidence[f"{prefix}_https_rule_present"] = (
            "true"
            if labels.get(f"traefik.http.routers.{route_name}-websecure.rule")
            == f"Host(`{domain_host}`)"
            else "false"
        )
        evidence[f"{prefix}_any_host_rule_present"] = (
            "true"
            if any(f"Host(`{domain_host}`)" == value for _key, value in label_items)
            else "false"
        )
    return evidence


def _collect_json_objects(raw_items: JsonValue | None) -> list[JsonObject]:
    if not isinstance(raw_items, list):
        return []
    return [
        item_as_object
        for raw_item in raw_items
        if (item_as_object := dokploy_api.as_json_object(raw_item)) is not None
    ]


def _domain_hosts_from_payload(raw_domains: JsonValue) -> tuple[str, ...]:
    if not isinstance(raw_domains, list):
        return ()
    hosts: list[str] = []
    for raw_domain in raw_domains:
        domain = dokploy_api.as_json_object(raw_domain)
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


def _artifact_image_reference(manifest: ArtifactIdentityManifest) -> str:
    return f"{manifest.image.repository}@{manifest.image.digest}"


def _target_base_url(*, lane: ProductLaneProfile, domains: tuple[str, ...]) -> str:
    if lane.base_url.strip():
        return lane.base_url.strip().rstrip("/")
    if domains:
        return f"https://{domains[0].strip()}".rstrip("/")
    return ""


def _target_health_url(
    *, profile: LaunchplaneProductProfileRecord, lane: ProductLaneProfile, domains: tuple[str, ...]
) -> str:
    base_url = _target_base_url(lane=lane, domains=domains)
    lane_health_url = lane.health_url.strip()
    if lane_health_url and not is_legacy_derived_odoo_health_url(
        health_url=lane_health_url,
        base_url=base_url,
        profile_health_path=profile.health_path,
    ):
        return lane_health_url
    return default_odoo_health_url(base_url=base_url)


def _read_odoo_instance_override_record(
    *, record_store: OdooStableTargetReplacementStore, context: str, instance: str
) -> OdooInstanceOverrideRecord | None:
    try:
        return record_store.read_odoo_instance_override_record(
            context_name=context,
            instance_name=instance,
        )
    except FileNotFoundError:
        return None


def _record_with_target_replacement_canonical(
    *,
    record: OdooInstanceOverrideRecord | None,
    canonical_url: str,
    updated_at: str,
) -> OdooInstanceOverrideRecord | None:
    normalized_canonical_url = canonical_url.strip().rstrip("/")
    if record is None or record.website_bootstrap is None or not normalized_canonical_url:
        return record
    if record.website_bootstrap.canonical_url == normalized_canonical_url:
        return record
    return record.model_copy(
        update={
            "website_bootstrap": record.website_bootstrap.model_copy(
                update={"canonical_url": normalized_canonical_url}
            ),
            "updated_at": updated_at,
            "source_label": "odoo-stable-target-replacement",
        }
    )


def _merge_required_odoo_install_modules(raw_modules: str) -> str:
    return merge_odoo_install_modules(LAUNCHPLANE_REQUIRED_ODOO_MODULES, raw_modules)


def _normalize_domain(raw_domain: str) -> str:
    return raw_domain.strip().lower().removeprefix("https://").removeprefix("http://").rstrip("/")


def _assert_prelaunch_rebuild_policy_allows_request(
    *,
    request: OdooStableTargetReplacementRequest,
    lane: ProductLaneProfile,
    target_name: str,
    domain_hosts: tuple[str, ...],
) -> str:
    if request.data_source_mode == "existing":
        return ""
    policy = lane.odoo_prelaunch_rebuild
    if not policy.enabled:
        raise click.ClickException(
            f"Odoo prelaunch rebuild is not enabled for {request.product} {lane.context}/{lane.instance}."
        )
    if policy.data_source_mode != request.data_source_mode:
        raise click.ClickException(
            "Odoo prelaunch rebuild data source mode mismatch: "
            f"request={request.data_source_mode!r} policy={policy.data_source_mode!r}."
        )
    if not lane.odoo_data_policy.allows_rebuild_source(request.data_source_mode):
        raise click.ClickException(
            "Odoo lane data policy does not allow prelaunch rebuild data source "
            f"{request.data_source_mode!r} for {request.product} {lane.context}/{lane.instance}."
        )
    if request.confirmation != policy.confirmation:
        raise click.ClickException(
            f"Odoo prelaunch rebuild requires confirmation {policy.confirmation!r}."
        )
    if target_name != policy.expected_target_name:
        raise click.ClickException(
            "Odoo prelaunch rebuild target proof failed: expected target "
            f"{policy.expected_target_name!r}, observed {target_name!r}."
        )
    target_domains = {_normalize_domain(domain) for domain in domain_hosts if domain.strip()}
    missing_domains = tuple(
        domain for domain in policy.expected_domains if domain not in target_domains
    )
    if missing_domains:
        raise click.ClickException(
            "Odoo prelaunch rebuild target proof failed: missing expected domain(s) "
            + ", ".join(missing_domains)
        )
    return policy.approval_issue_url


def _build_ship_request(
    *,
    plan: OdooStableTargetReplacementPlan,
    target_name: str,
    artifact_id: str,
    source_git_ref: str,
    timeout_seconds: int | None,
    no_cache: bool,
) -> ShipRequest:
    return ShipRequest(
        artifact_id=artifact_id,
        context=plan.context,
        instance=plan.instance,
        source_git_ref=source_git_ref,
        target_name=target_name,
        target_type="compose",
        provider_id="dokploy",
        target_category="compose",
        provider_target_type="compose",
        deploy_mode="dokploy-compose-api",
        wait=True,
        timeout_seconds=timeout_seconds,
        verify_health=False,
        no_cache=no_cache,
        destination_health=HealthcheckEvidence(status="skipped"),
    )


def _build_runtime_identity(
    *,
    plan: OdooStableTargetReplacementPlan,
    deployment_record_id: str,
    artifact_id: str,
    source_git_ref: str,
    image_reference: str,
    deployed_at: str = "",
) -> RuntimeIdentity:
    return RuntimeIdentity(
        product=plan.product,
        context=plan.context,
        instance=plan.instance,
        environment_kind="stable",
        deployment_record_id=deployment_record_id,
        artifact_id=artifact_id,
        source_git_ref=source_git_ref,
        image_reference=image_reference,
        deployed_at=deployed_at,
    )


def _verify_required_runtime_identity_evidence(
    *,
    health_url: str,
    timeout_seconds: int,
    expected_runtime_identity: RuntimeIdentity,
) -> HealthcheckEvidence:
    evidence = HealthcheckEvidence(
        verified=True,
        urls=(health_url,),
        timeout_seconds=timeout_seconds,
        status="fail",
    )
    try:
        healthcheck_pass = wait_for_runtime_identity_healthcheck_with_retry(
            url=health_url,
            timeout_seconds=timeout_seconds,
            expected_runtime_identity=expected_runtime_identity,
            sleep=time.sleep,
            monotonic=time.monotonic,
        )
    except RuntimeIdentityHealthcheckError as error:
        if error.healthcheck_pass is not None:
            return healthcheck_evidence_with_runtime_identity(
                evidence,
                expected_runtime_identity=expected_runtime_identity,
                healthcheck_pass=error.healthcheck_pass,
            )
        return evidence.model_copy(
            update={
                "runtime_identity_status": "unverifiable",
                "runtime_identity_detail": str(error),
            }
        )
    except (click.ClickException, TimeoutError, OSError) as error:
        return evidence.model_copy(
            update={
                "runtime_identity_status": "unverifiable",
                "runtime_identity_detail": str(error),
            }
        )

    verified_evidence = healthcheck_evidence_with_runtime_identity(
        evidence.model_copy(update={"status": "pass"}),
        expected_runtime_identity=expected_runtime_identity,
        healthcheck_pass=healthcheck_pass,
    )
    if verified_evidence.runtime_identity_status != "match":
        return verified_evidence.model_copy(update={"status": "fail"})
    return verified_evidence


def _write_failed_deployment(
    *,
    record_store: OdooStableTargetReplacementStore,
    ship_request: ShipRequest,
    deployment_record_id: str,
    started_at: str,
    resolved_target: ResolvedTargetEvidence | None = None,
    runtime_source: dict[str, str] | None = None,
    runtime_identity: RuntimeIdentity | None = None,
    post_deploy_update: PostDeployUpdateEvidence | None = None,
    destination_health: HealthcheckEvidence | None = None,
) -> None:
    record_store.write_deployment_record(
        build_deployment_record(
            request=ship_request,
            record_id=deployment_record_id,
            deployment_id="control-plane-dokploy",
            deployment_status="fail",
            started_at=started_at,
            finished_at=utc_now_timestamp(),
            resolved_target=resolved_target,
            runtime_source=runtime_source,
            runtime_identity=runtime_identity,
            post_deploy_update=post_deploy_update,
            destination_health=destination_health,
        )
    )


def _snapshot_current_target(
    *,
    host: str,
    token: str,
    target_record: DokployTargetRecord,
    target_id_record: DokployTargetIdRecord,
    request: DokployRequest,
) -> OdooStableTargetRuntimeSnapshot:
    target_payload = dokploy_api.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type=target_record.target_type,
        target_id=target_id_record.target_id,
    )
    env_map = dokploy_api.parse_dokploy_env_text(str(target_payload.get("env") or ""))
    live_volume_values = {
        key: env_map.get(key, "").strip() for key in ODOO_REQUIRED_VOLUME_ENV_KEYS
    }
    present_volume_keys = tuple(
        key for key in ODOO_REQUIRED_VOLUME_ENV_KEYS if live_volume_values[key]
    )
    missing_volume_keys = tuple(
        key for key in ODOO_REQUIRED_VOLUME_ENV_KEYS if key not in present_volume_keys
    )
    latest_deployment = dokploy_api.latest_deployment_for_target(
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
        source_type=str(
            target_payload.get("sourceType") or target_record.source_type or ""
        ).strip(),
        compose_path=str(
            target_payload.get("composePath") or target_record.compose_path or ""
        ).strip(),
        compose_file_sha256=dokploy_compose.compose_file_sha256(compose_file)
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
        live_volume_values=live_volume_values,
        runtime_identity_present=bool(runtime_identity),
        runtime_identity_deployment_record_id=runtime_identity.get("deployment_record_id", ""),
    )


def _build_steps(
    *,
    current_target: OdooStableTargetRuntimeSnapshot | None,
    blockers: tuple[str, ...],
    volume_authority_drift_keys: tuple[str, ...],
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
            if (
                current_target is not None
                and not current_target.required_volume_keys_missing
                and not volume_authority_drift_keys
            )
            else "blocked",
            message=(
                "Confirm live Odoo data/log/database volumes match DB-backed desired authority."
            ),
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
    dokploy_request: DokployRequest | None = None,
    dokploy_config_reader: DokployConfigReader | None = None,
) -> OdooStableTargetReplacementPlan:
    resolved_dokploy_request = dokploy_request or dokploy_api.dokploy_request
    resolved_dokploy_config_reader = dokploy_config_reader or dokploy_source.read_dokploy_config
    try:
        profile = record_store.read_product_profile_record(request.product)
    except FileNotFoundError as error:
        raise click.ClickException(
            f"Launchplane has no product profile record for {request.product!r}."
        ) from error
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
    desired_volume_values: dict[str, str] = {}
    volume_authority_drift_keys: tuple[str, ...] = ()
    if target_record is None:
        blockers.append("Launchplane has no Dokploy target record for this lane.")
    if target_id_record is None:
        blockers.append("Launchplane has no Dokploy target-id record for this lane.")
    if inventory is None:
        warnings.append("Launchplane has no current environment inventory for this lane.")
    if isinstance(target_record, DokployTargetRecord) and target_record.target_type != "compose":
        blockers.append("Odoo stable replacement currently requires a compose target.")
    approval_issue_url = ""
    if request.data_source_mode != "existing" and not request.allow_empty_data:
        blockers.append("Odoo prelaunch rebuild requests must explicitly set allow_empty_data.")
    if isinstance(target_record, DokployTargetRecord) and isinstance(
        target_id_record, DokployTargetIdRecord
    ):
        try:
            desired_volume_values = _resolve_desired_volume_values(
                record_store=record_store,
                target_record=target_record,
                context=lane.context,
                instance=lane.instance,
            )
        except click.ClickException:
            blockers.append(
                "Launchplane could not resolve DB-backed Odoo volume authority for this lane."
            )
        host, token = resolved_dokploy_config_reader(control_plane_root=control_plane_root)
        current_target = _snapshot_current_target(
            host=host,
            token=token,
            target_record=target_record,
            target_id_record=target_id_record,
            request=resolved_dokploy_request,
        )
        try:
            approval_issue_url = _assert_prelaunch_rebuild_policy_allows_request(
                request=request,
                lane=lane,
                target_name=current_target.target_name,
                domain_hosts=current_target.domain_hosts,
            )
        except click.ClickException as error:
            blockers.append(str(error))
        if current_target.required_volume_keys_missing and not request.allow_empty_data:
            blockers.append(
                "Current target is missing required Odoo volume env keys: "
                + ", ".join(current_target.required_volume_keys_missing)
            )
        if request.data_source_mode == "existing" and desired_volume_values:
            volume_authority_drift_keys = tuple(
                key
                for key in ODOO_REQUIRED_VOLUME_ENV_KEYS
                if desired_volume_values.get(key, "")
                != current_target.live_volume_values.get(key, "")
            )
            if volume_authority_drift_keys:
                blockers.append(
                    "Existing-data target replacement requires live Odoo volume values to match "
                    "DB-backed desired authority for: " + ", ".join(volume_authority_drift_keys)
                )
        if not current_target.domain_hosts:
            blockers.append("Current target has no discoverable Dokploy domains to cut over.")
        if not current_target.runtime_identity_present:
            warnings.append("Current target does not expose a Launchplane runtime identity yet.")
    elif isinstance(target_record, DokployTargetRecord):
        try:
            approval_issue_url = _assert_prelaunch_rebuild_policy_allows_request(
                request=request,
                lane=lane,
                target_name=target_record.target_name,
                domain_hosts=target_record.domains,
            )
        except click.ClickException as error:
            blockers.append(str(error))
    current_artifact_id = ""
    current_source_git_ref = ""
    if isinstance(inventory, EnvironmentInventory):
        current_artifact_id = (
            inventory.artifact_identity.artifact_id if inventory.artifact_identity else ""
        )
        current_source_git_ref = inventory.source_git_ref
    if (
        request.expected_current_artifact_id
        and current_artifact_id != request.expected_current_artifact_id
    ):
        blockers.append("Current inventory artifact changed after operational readiness preflight.")
    expected_artifact_id = request.artifact_id or current_artifact_id
    expected_source_git_ref = request.source_git_ref or current_source_git_ref
    if expected_artifact_id:
        try:
            artifact_manifest = record_store.read_artifact_manifest(expected_artifact_id)
        except FileNotFoundError:
            blockers.append(f"Launchplane has no artifact manifest for {expected_artifact_id!r}.")
        else:
            if not artifact_manifest_matches_image_repository(
                artifact_manifest,
                expected_repository=profile.image.repository,
            ):
                blockers.append(
                    "Selected artifact image repository does not match product profile."
                )
            if not expected_source_git_ref:
                blockers.append("Selected artifact is missing immutable source-git evidence.")
            elif artifact_manifest.source_commit != expected_source_git_ref:
                blockers.append("Selected artifact source ref does not match the stored manifest.")
            missing_required_modules = missing_required_odoo_modules_from_artifact(
                artifact_manifest
            )
            if missing_required_modules:
                blockers.append(
                    "Odoo target replacement requires artifact odoo_install_modules to declare required module(s): "
                    + ", ".join(missing_required_modules)
                )
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
        allow_empty_data=request.allow_empty_data,
        data_source_mode=request.data_source_mode,
        approval_issue_url=approval_issue_url,
        blockers=blockers_tuple,
        warnings=tuple(warnings),
        steps=_build_steps(
            current_target=current_target,
            blockers=blockers_tuple,
            volume_authority_drift_keys=volume_authority_drift_keys,
            plan_strategy=request.strategy,
        ),
    )


def _resolve_desired_volume_values(
    *,
    record_store: OdooStableTargetReplacementStore,
    target_record: DokployTargetRecord,
    context: str,
    instance: str,
) -> dict[str, str]:
    definition = (
        control_plane_runtime_environments.load_optional_runtime_environment_definition_from_store(
            record_store=record_store
        )
    )
    if definition is None:
        raise click.ClickException("Missing DB-backed runtime environment records.")
    values = control_plane_runtime_environments.resolve_values_from_definition(
        definition=definition,
        context_name=context,
        instance_name=instance,
    )
    values.update(target_record.env)
    return {key: values.get(key, "").strip() for key in ODOO_REQUIRED_VOLUME_ENV_KEYS}


def execute_odoo_stable_target_replacement_apply(
    *,
    control_plane_root: Path,
    record_store: OdooStableTargetReplacementStore,
    request: OdooStableTargetReplacementApplyRequest,
    dokploy_request: DokployRequest = dokploy_api.dokploy_request,
    provider_effect_checkpoint: Callable[[str], None] | None = None,
) -> OdooStableTargetReplacementApplyResult:
    plan = build_odoo_stable_target_replacement_plan(
        control_plane_root=control_plane_root,
        record_store=record_store,
        request=OdooStableTargetReplacementRequest(
            product=request.product,
            instance=request.instance,
            strategy=request.strategy,
            allow_empty_data=request.allow_empty_data,
            data_source_mode=request.data_source_mode,
            confirmation=request.confirmation,
            artifact_id=request.artifact_id,
            source_git_ref=request.source_git_ref,
            expected_current_artifact_id=request.expected_current_artifact_id,
        ),
        dokploy_request=dokploy_request,
    )
    if plan.plan_status != "ready" or plan.current_target is None:
        raise click.ClickException(
            "Odoo target replacement apply requires a ready replacement plan: "
            + "; ".join(plan.blockers or ("missing current target",))
        )
    if request.strategy != "recreate-in-place":
        raise click.ClickException("Odoo target replacement apply supports recreate-in-place only.")

    profile = record_store.read_product_profile_record(request.product)
    lane = _read_lane(profile=profile, instance=request.instance)
    target_record = record_store.read_dokploy_target_record(
        context_name=plan.context, instance_name=plan.instance
    )
    target_id_record = record_store.read_dokploy_target_id_record(
        context_name=plan.context, instance_name=plan.instance
    )
    if target_record.target_type != "compose":
        raise click.ClickException("Odoo target replacement apply requires a compose target.")
    artifact_id = request.artifact_id or plan.expected_artifact_id
    source_git_ref = request.source_git_ref or plan.expected_source_git_ref
    if not artifact_id.strip():
        raise click.ClickException(
            "Odoo target replacement apply requires artifact_id or inventory artifact evidence."
        )
    if not source_git_ref.strip():
        raise click.ClickException(
            "Odoo target replacement apply requires source_git_ref or inventory source git ref evidence."
        )
    artifact_manifest = record_store.read_artifact_manifest(artifact_id)
    if not artifact_manifest_matches_image_repository(
        artifact_manifest,
        expected_repository=profile.image.repository,
    ):
        raise click.ClickException(
            "Odoo target replacement artifact image repository does not match product profile."
        )
    if artifact_manifest.source_commit != source_git_ref:
        raise click.ClickException(
            "Odoo target replacement apply source ref does not match stored artifact manifest. "
            f"Request={source_git_ref} manifest={artifact_manifest.source_commit}."
        )
    missing_required_modules = missing_required_odoo_modules_from_artifact(artifact_manifest)
    if missing_required_modules:
        raise click.ClickException(
            "Odoo target replacement apply requires artifact odoo_install_modules to declare required module(s): "
            + ", ".join(missing_required_modules)
        )
    image_reference = _artifact_image_reference(artifact_manifest)
    target_name = (
        plan.current_target.target_name
        or target_record.target_name
        or plan.expected_next_target_name
    )
    deploy_timeout_seconds = (
        request.timeout_seconds
        or target_record.deploy_timeout_seconds
        or dokploy_source.DEFAULT_DOKPLOY_DEPLOY_TIMEOUT_SECONDS
    )
    health_timeout_seconds = (
        request.health_timeout_seconds
        or target_record.healthcheck_timeout_seconds
        or dokploy_source.DEFAULT_DOKPLOY_HEALTH_TIMEOUT_SECONDS
    )
    ship_request = _build_ship_request(
        plan=plan,
        target_name=target_name,
        artifact_id=artifact_id,
        source_git_ref=source_git_ref,
        timeout_seconds=deploy_timeout_seconds,
        no_cache=request.no_cache,
    )
    deployment_record_id = generate_deployment_record_id(
        context_name=plan.context, instance_name=plan.instance
    )
    started_at = utc_now_timestamp()
    resolved_target = ResolvedTargetEvidence(
        target_type="compose",
        target_id=target_id_record.target_id,
        target_name=target_name,
    )
    runtime_identity = _build_runtime_identity(
        plan=plan,
        deployment_record_id=deployment_record_id,
        artifact_id=artifact_id,
        source_git_ref=source_git_ref,
        image_reference=image_reference,
    )
    base_url = _target_base_url(lane=lane, domains=plan.expected_domain_hosts)
    health_url = _target_health_url(
        profile=profile,
        lane=lane,
        domains=plan.expected_domain_hosts,
    )
    if lane.odoo_data_policy.requires_runtime_identity:
        if not request.verify_health:
            raise click.ClickException(
                "Odoo target replacement requires health verification when the lane requires runtime identity."
            )
        if not health_url:
            raise click.ClickException(
                "Odoo target replacement runtime identity verification has no health URL."
            )

    record_store.write_deployment_record(
        build_deployment_record(
            request=ship_request,
            record_id=deployment_record_id,
            deployment_id="control-plane-dokploy",
            deployment_status="pending",
            started_at=started_at,
            finished_at="",
            resolved_target=resolved_target,
            runtime_identity=runtime_identity,
            destination_health=HealthcheckEvidence(status="pending")
            if request.verify_health
            else HealthcheckEvidence(status="skipped"),
        )
    )

    base_result = _ApplyResultBase(
        product=plan.product,
        context=plan.context,
        instance=plan.instance,
        deployment_record_id=deployment_record_id,
        target_id=target_id_record.target_id,
        target_name=resolved_target.target_name,
        artifact_id=artifact_id,
        image_reference=image_reference,
    )
    runtime_source: dict[str, str] = {}
    odoo_override_record = _read_odoo_instance_override_record(
        record_store=record_store,
        context=plan.context,
        instance=plan.instance,
    )
    normalized_override_record = _record_with_target_replacement_canonical(
        record=odoo_override_record,
        canonical_url=base_url,
        updated_at=started_at,
    )
    if (
        normalized_override_record is not None
        and normalized_override_record is not odoo_override_record
    ):
        record_store.write_odoo_instance_override_record(normalized_override_record)
    runtime_override_environment: dict[str, str] = {}
    runtime_override_payload = None
    if normalized_override_record is not None and "deploy" in normalized_override_record.apply_on:
        runtime_override = control_plane_odoo_instance_overrides.build_post_deploy_environment(
            normalized_override_record,
            workflow_intent="deploy",
            protected_shopify_store_keys=target_record.policies.shopify.protected_store_keys,
        )
        runtime_override_environment = runtime_override.inline_environment
        runtime_override_payload = runtime_override.payload
        runtime_source.update(
            {
                "runtime_override_payload_rendered": "true",
                "runtime_override_payload_sha256": runtime_override_payload.wire_sha256,
                "runtime_override_count": str(runtime_override_payload.override_count),
                "runtime_override_website_bootstrap_included": str(
                    runtime_override_payload.website_bootstrap_included
                ).lower(),
                "runtime_override_instance_required": runtime_override_environment.get(
                    control_plane_odoo_instance_overrides.LAUNCHPLANE_INSTANCE_OVERRIDES_REQUIRED_ENV_KEY,
                    "false",
                ),
                "runtime_override_website_bootstrap_required": runtime_override_environment.get(
                    control_plane_odoo_instance_overrides.LAUNCHPLANE_WEBSITE_BOOTSTRAP_REQUIRED_ENV_KEY,
                    "false",
                ),
            }
        )
    else:
        runtime_source["runtime_override_payload_rendered"] = "false"

    try:
        host, token = dokploy_source.read_dokploy_config(control_plane_root=control_plane_root)
        target_payload = dokploy_api.fetch_dokploy_target_payload(
            host=host,
            token=token,
            target_type="compose",
            target_id=target_id_record.target_id,
        )
        runtime_environment_values = (
            control_plane_runtime_environments.resolve_runtime_environment_values(
                control_plane_root=control_plane_root,
                context_name=plan.context,
                instance_name=plan.instance,
            )
        )
        try:
            runtime_secret_binding_keys = (
                control_plane_live_target_runtime.require_product_profile_runtime_secret_keys(
                    record_store=record_store,
                    product_name=plan.product,
                    context_name=plan.context,
                    instance_name=plan.instance,
                )
            )
            runtime_key_safety = (
                control_plane_live_target_runtime.evaluate_runtime_key_safety_for_live_target_sync(
                    record_store=record_store,
                    context_name=plan.context,
                    instance_name=plan.instance,
                    required_binding_keys=tuple(sorted(runtime_secret_binding_keys)),
                )
            )
        except control_plane_live_target_runtime.LiveTargetRuntimeError as error:
            raise click.ClickException(
                control_plane_live_target_runtime.runtime_key_safety_error_message(error)
            ) from error
        runtime_source.update(
            {
                f"runtime_key_safety_{key}": str(value)
                for key, value in runtime_key_safety.items()
                if key in {"required", "status", "policy_record_id", "policy_sha256"}
            }
        )
        compose_file = dokploy_compose.render_odoo_raw_compose_file(
            image_reference=image_reference,
            domain_hosts=plan.expected_domain_hosts,
            runtime_port=profile.runtime_port,
        )
        runtime_source.update(
            {
                f"rendered_{key}": value
                for key, value in _raw_compose_route_evidence(
                    compose_file=compose_file,
                    domain_hosts=plan.expected_domain_hosts,
                ).items()
            }
        )
        if provider_effect_checkpoint is not None:
            provider_effect_checkpoint("target_replacement_raw_source")
        raw_compose_evidence = dokploy_compose.sync_dokploy_compose_raw_source(
            host=host,
            token=token,
            compose_id=target_id_record.target_id,
            compose_name=resolved_target.target_name,
            target_payload=target_payload,
            compose_file=compose_file,
        )
        runtime_source.update(
            {f"raw_compose_{key}": value for key, value in raw_compose_evidence.items()}
        )
        for domain_host in plan.expected_domain_hosts:
            dokploy_compose.ensure_compose_web_domain_route(
                host=host,
                token=token,
                compose_id=target_id_record.target_id,
                domain_host=domain_host,
                runtime_port=profile.runtime_port,
            )
        raw_domains = dokploy_request(
            host=host,
            token=token,
            path="/api/domain.byComposeId",
            query={"composeId": target_id_record.target_id},
        )
        runtime_source.update(
            {
                f"domain_route_{key}": value
                for key, value in _dokploy_domain_route_evidence(
                    raw_domains=raw_domains,
                    expected_domain_hosts=plan.expected_domain_hosts,
                    runtime_port=profile.runtime_port,
                ).items()
            }
        )
        converted_compose_file = dokploy_compose.fetch_dokploy_converted_compose_file(
            host=host,
            token=token,
            compose_id=target_id_record.target_id,
        )
        runtime_source.update(
            {
                f"converted_{key}": value
                for key, value in _raw_compose_route_evidence(
                    compose_file=converted_compose_file,
                    domain_hosts=plan.expected_domain_hosts,
                ).items()
            }
        )
        current_env_map = dokploy_api.parse_dokploy_env_text(str(target_payload.get("env") or ""))
        legacy_odoo_install_modules = current_env_map.get(ODOO_INSTALL_MODULES_ENV_KEY, "")
        desired_env_map = dict(current_env_map)
        for key in dokploy_post_deploy.ODOO_RUNTIME_OVERRIDE_TARGET_ENV_KEYS:
            desired_env_map.pop(key, None)
        desired_env_map.pop(ODOO_INSTALL_MODULES_ENV_KEY, None)
        desired_env_map.update(runtime_environment_values)
        desired_env_map.update(runtime_override_environment)
        explicit_odoo_install_modules = desired_env_map.get(ODOO_INSTALL_MODULES_ENV_KEY, "")
        manifest_odoo_install_modules = artifact_manifest.odoo_install_modules
        fallback_odoo_install_modules = (
            "" if manifest_odoo_install_modules else legacy_odoo_install_modules
        )
        desired_env_map[ODOO_INSTALL_MODULES_ENV_KEY] = merge_odoo_install_modules(
            LAUNCHPLANE_REQUIRED_ODOO_MODULES,
            manifest_odoo_install_modules,
            fallback_odoo_install_modules,
            explicit_odoo_install_modules,
        )
        runtime_source["required_odoo_modules"] = ",".join(LAUNCHPLANE_REQUIRED_ODOO_MODULES)
        runtime_source["artifact_odoo_install_modules"] = ",".join(
            artifact_manifest.odoo_install_modules
        )
        runtime_source["odoo_install_modules"] = desired_env_map[ODOO_INSTALL_MODULES_ENV_KEY]
        if runtime_override_payload is not None:
            missing_override_secret_keys = tuple(
                key
                for key in runtime_override_payload.required_container_environment_keys
                if not desired_env_map.get(key, "").strip()
            )
            if missing_override_secret_keys:
                raise click.ClickException(
                    "Odoo target replacement requires override secret env key(s) before deployment: "
                    + ", ".join(missing_override_secret_keys)
                )
        if desired_env_map.get("ODOO_WEB_COMMAND", "").strip() == "/odoo/odoo-bin":
            desired_env_map.pop("ODOO_WEB_COMMAND", None)
        desired_env_map["PLATFORM_CONTEXT"] = plan.context
        desired_env_map["PLATFORM_INSTANCE"] = plan.instance
        desired_env_map["DOCKER_IMAGE_REFERENCE"] = image_reference
        desired_env_map.update(runtime_identity_env(runtime_identity))
        dokploy_api.update_dokploy_target_env(
            host=host,
            token=token,
            target_type="compose",
            target_id=target_id_record.target_id,
            target_payload=target_payload,
            env_text=dokploy_api.serialize_dokploy_env_text(desired_env_map),
        )
        refreshed_payload = dokploy_api.fetch_dokploy_target_payload(
            host=host,
            token=token,
            target_type="compose",
            target_id=target_id_record.target_id,
        )
        live_compose_file = str(refreshed_payload.get("composeFile") or "")
        runtime_source.update(
            {
                f"live_{key}": value
                for key, value in _raw_compose_route_evidence(
                    compose_file=live_compose_file,
                    domain_hosts=plan.expected_domain_hosts,
                ).items()
            }
        )
        runtime_source["live_source_type"] = str(refreshed_payload.get("sourceType") or "")
        runtime_source["live_compose_path"] = str(refreshed_payload.get("composePath") or "")
        runtime_source.update(
            {
                f"pre_deploy_{key}": value
                for key, value in _dokploy_compose_metadata_evidence(refreshed_payload).items()
            }
        )
        refreshed_env_map = dokploy_api.parse_dokploy_env_text(
            str(refreshed_payload.get("env") or "")
        )
        missing_keys = sorted(
            key for key, value in desired_env_map.items() if refreshed_env_map.get(key, "") != value
        )
        if missing_keys:
            raise click.ClickException(
                "Odoo target replacement env did not persist key(s): " + ", ".join(missing_keys)
            )
        latest_before = dokploy_api.latest_deployment_for_target(
            host=host,
            token=token,
            target_type="compose",
            target_id=target_id_record.target_id,
        )
        dokploy_api.trigger_deployment(
            host=host,
            token=token,
            target_type="compose",
            target_id=target_id_record.target_id,
            no_cache=request.no_cache,
        )
        dokploy_api.wait_for_target_deployment(
            host=host,
            token=token,
            target_type="compose",
            target_id=target_id_record.target_id,
            before_key=dokploy_api.deployment_key(latest_before),
            timeout_seconds=deploy_timeout_seconds,
        )
        deployed_payload = dokploy_api.fetch_dokploy_target_payload(
            host=host,
            token=token,
            target_type="compose",
            target_id=target_id_record.target_id,
        )
        runtime_source.update(
            {
                f"post_deploy_{key}": value
                for key, value in _dokploy_compose_metadata_evidence(deployed_payload).items()
            }
        )
        deployed_converted_compose_file = dokploy_compose.fetch_dokploy_converted_compose_file(
            host=host,
            token=token,
            compose_id=target_id_record.target_id,
        )
        runtime_source.update(
            {
                f"post_deploy_converted_{key}": value
                for key, value in _raw_compose_route_evidence(
                    compose_file=deployed_converted_compose_file,
                    domain_hosts=plan.expected_domain_hosts,
                ).items()
            }
        )
        deployed_app_name = str(deployed_payload.get("appName") or "")
        deployed_server_id = str(deployed_payload.get("serverId") or "").strip()
        container_query: dict[str, str] = {
            "appName": deployed_app_name,
            "appType": "docker-compose",
        }
        if deployed_server_id:
            container_query["serverId"] = deployed_server_id
        containers_payload = dokploy_request(
            host=host,
            token=token,
            path="/api/docker.getContainersByAppNameMatch",
            query=container_query,
        )
        web_container = _web_container_for_app(
            containers_payload=containers_payload,
            app_name=deployed_app_name,
        )
        config_payload: JsonValue | None = None
        if web_container is not None and _container_id(web_container):
            config_query = {"containerId": _container_id(web_container)}
            if deployed_server_id:
                config_query["serverId"] = deployed_server_id
            config_payload = dokploy_request(
                host=host,
                token=token,
                path="/api/docker.getConfig",
                query=config_query,
            )
        runtime_source.update(
            {
                f"post_deploy_{key}": value
                for key, value in _dokploy_container_route_evidence(
                    containers_payload=containers_payload,
                    config_payload=config_payload,
                    app_name=deployed_app_name,
                    server_id=deployed_server_id,
                    expected_domain_hosts=plan.expected_domain_hosts,
                ).items()
            }
        )
    except click.ClickException as error:
        _write_failed_deployment(
            record_store=record_store,
            ship_request=ship_request,
            deployment_record_id=deployment_record_id,
            started_at=started_at,
            resolved_target=resolved_target,
            runtime_source=runtime_source,
            runtime_identity=runtime_identity,
            destination_health=HealthcheckEvidence(status="skipped"),
        )
        return base_result.result(
            deploy_status="fail",
            runtime_identity_injected=False,
            runtime_source=runtime_source,
            error_message=str(error),
        )

    post_deploy_phase: OdooOverrideApplyPhase = (
        "restore" if plan.data_source_mode == "upstream_restore" else "deploy"
    )
    post_deploy_result = execute_odoo_post_deploy(
        control_plane_root=control_plane_root,
        record_store=record_store,
        request=OdooPostDeployRequest(
            context=plan.context,
            instance=plan.instance,
            phase=post_deploy_phase,
        ),
        run_destructive_restore=plan.data_source_mode == "upstream_restore",
        provider_effect_checkpoint=provider_effect_checkpoint,
    )
    post_deploy_evidence = PostDeployUpdateEvidence(
        attempted=True,
        status=post_deploy_result.post_deploy_status,
        detail=post_deploy_result.error_message
        or "Odoo post-deploy completed after stable target replacement apply.",
        evidence=post_deploy_result.override_evidence,
    )
    if post_deploy_result.post_deploy_status != "pass":
        _write_failed_deployment(
            record_store=record_store,
            ship_request=ship_request,
            deployment_record_id=deployment_record_id,
            started_at=started_at,
            resolved_target=resolved_target,
            runtime_source=runtime_source,
            runtime_identity=runtime_identity,
            post_deploy_update=post_deploy_evidence,
            destination_health=HealthcheckEvidence(status="skipped"),
        )
        return base_result.result(
            deploy_status="fail",
            post_deploy_status=post_deploy_result.post_deploy_status,
            post_deploy_result=post_deploy_result,
            runtime_identity_injected=True,
            runtime_source=runtime_source,
            error_message=post_deploy_result.error_message or "Odoo post-deploy failed.",
        )

    verification = verify_odoo_stable_readiness(
        base_url=base_url,
        health_url=health_url,
        verify_health=request.verify_health,
        verify_canonical=request.verify_canonical,
        verify_logo=request.verify_logo,
        timeout_seconds=health_timeout_seconds,
        retry_interval_seconds=ODOO_STABLE_TARGET_REPLACEMENT_VERIFY_RETRY_INTERVAL_SECONDS,
    )
    health_status = verification.health_status
    canonical_status = verification.canonical_status
    logo_status = verification.logo_status
    if verification.error_message:
        destination_health = HealthcheckEvidence(
            verified=health_status == "pass",
            urls=(health_url,) if health_url else (),
            timeout_seconds=health_timeout_seconds if health_url else None,
            status="fail",
        )
        _write_failed_deployment(
            record_store=record_store,
            ship_request=ship_request,
            deployment_record_id=deployment_record_id,
            started_at=started_at,
            resolved_target=resolved_target,
            runtime_source=runtime_source,
            runtime_identity=runtime_identity,
            post_deploy_update=post_deploy_evidence,
            destination_health=destination_health,
        )
        return base_result.result(
            deploy_status="fail",
            post_deploy_status="pass",
            post_deploy_result=post_deploy_result,
            health_status=health_status if health_status == "pass" else "fail",
            canonical_status=canonical_status if canonical_status == "pass" else "fail",
            logo_status=logo_status if logo_status == "pass" else "fail",
            health_url=verification.evidence.health_url,
            canonical_url=verification.evidence.canonical_url,
            logo_urls=verification.evidence.logo_urls,
            verification_evidence=verification.evidence,
            runtime_identity_injected=True,
            runtime_source=runtime_source,
            error_message=verification.error_message,
        )

    finished_at = utc_now_timestamp()
    final_runtime_identity = runtime_identity.model_copy(update={"deployed_at": finished_at})
    if lane.odoo_data_policy.requires_runtime_identity:
        destination_health = _verify_required_runtime_identity_evidence(
            health_url=health_url,
            timeout_seconds=health_timeout_seconds,
            expected_runtime_identity=runtime_identity,
        )
        if destination_health.runtime_identity_status != "match":
            error_message = "Odoo runtime identity verification failed: " + (
                destination_health.runtime_identity_detail
                or destination_health.runtime_identity_status
            )
            _write_failed_deployment(
                record_store=record_store,
                ship_request=ship_request,
                deployment_record_id=deployment_record_id,
                started_at=started_at,
                resolved_target=resolved_target,
                runtime_source=runtime_source,
                runtime_identity=runtime_identity,
                post_deploy_update=post_deploy_evidence,
                destination_health=destination_health,
            )
            return base_result.result(
                deploy_status="fail",
                post_deploy_status="pass",
                post_deploy_result=post_deploy_result,
                health_status="fail",
                canonical_status=canonical_status,
                logo_status=logo_status,
                health_url=verification.evidence.health_url,
                canonical_url=verification.evidence.canonical_url,
                logo_urls=verification.evidence.logo_urls,
                verification_evidence=verification.evidence,
                runtime_identity_injected=True,
                runtime_source=runtime_source,
                error_message=error_message,
            )
    else:
        destination_health = HealthcheckEvidence(
            verified=request.verify_health,
            urls=(health_url,) if request.verify_health and health_url else (),
            timeout_seconds=health_timeout_seconds
            if request.verify_health and health_url
            else None,
            status=health_status,
        )
    deployment_record = build_deployment_record(
        request=ship_request,
        record_id=deployment_record_id,
        deployment_id="control-plane-dokploy",
        deployment_status="pass",
        started_at=started_at,
        finished_at=finished_at,
        resolved_target=resolved_target,
        runtime_source=runtime_source,
        runtime_identity=final_runtime_identity,
        post_deploy_update=post_deploy_evidence,
        destination_health=destination_health,
    )
    record_store.write_deployment_record(deployment_record)
    record_store.write_environment_inventory(
        build_environment_inventory(deployment_record=deployment_record, updated_at=finished_at)
    )
    release_tuple_id = _write_release_tuple_from_deployment(
        record_store=record_store,
        deployment_record=deployment_record,
        artifact_manifest=artifact_manifest,
        minted_at=finished_at,
    )
    return base_result.result(
        deploy_status="pass",
        release_tuple_id=release_tuple_id,
        post_deploy_status="pass",
        post_deploy_result=post_deploy_result,
        health_status=health_status,
        canonical_status=canonical_status,
        logo_status=logo_status,
        health_url=verification.evidence.health_url,
        canonical_url=verification.evidence.canonical_url,
        logo_urls=verification.evidence.logo_urls,
        verification_evidence=verification.evidence,
        runtime_identity_injected=True,
        runtime_source=runtime_source,
    )


def _write_release_tuple_from_deployment(
    *,
    record_store: OdooStableTargetReplacementStore,
    deployment_record: DeploymentRecord,
    artifact_manifest: ArtifactIdentityManifest,
    minted_at: str,
) -> str:
    if not control_plane_release_tuples.should_mint_release_tuple_for_channel(
        deployment_record.instance
    ):
        return ""
    release_tuple = control_plane_release_tuples.build_release_tuple_record_from_artifact_manifest(
        context_name=deployment_record.context,
        channel_name=deployment_record.instance,
        artifact_manifest=artifact_manifest,
        deployment_record_id=deployment_record.record_id,
        minted_at=minted_at,
    )
    record_store.write_release_tuple_record(release_tuple)
    return release_tuple.tuple_id
