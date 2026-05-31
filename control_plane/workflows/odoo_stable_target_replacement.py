from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Literal, Protocol

import click
from pydantic import BaseModel, ConfigDict

from control_plane import dokploy as control_plane_dokploy
from control_plane import runtime_environments as control_plane_runtime_environments
from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.deployment_record import DeploymentRecord, ResolvedTargetEvidence
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.contracts.promotion_record import HealthcheckEvidence, PostDeployUpdateEvidence
from control_plane.contracts.runtime_identity import RuntimeIdentity, runtime_identity_env
from control_plane.contracts.ship_request import ShipRequest
from control_plane.contracts.odoo_stable_target_replacement import (
    OdooStableTargetReplacementApplyRequest,
    OdooStableTargetReplacementApplyResult,
    OdooStableTargetReplacementRequest,
)
from control_plane.dokploy import JsonObject, JsonValue
from control_plane.workflows.inventory import build_environment_inventory
from control_plane.workflows.odoo_post_deploy import OdooPostDeployRequest, execute_odoo_post_deploy
from control_plane.workflows.odoo_verification import (
    OdooVerificationEvidence,
    default_odoo_health_url,
    verify_odoo_stable_readiness,
)
from control_plane.workflows.ship import (
    build_deployment_record,
    generate_deployment_record_id,
    utc_now_timestamp,
)


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

    def read_artifact_manifest(self, artifact_id: str) -> ArtifactIdentityManifest: ...

    def write_deployment_record(self, record: DeploymentRecord) -> object: ...

    def write_environment_inventory(self, record: EnvironmentInventory) -> object: ...


DokployRequest = Callable[..., JsonValue]
DokployConfigReader = Callable[..., tuple[str, str]]
ODOO_STABLE_TARGET_REPLACEMENT_VERIFY_RETRY_INTERVAL_SECONDS = 5


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
        post_deploy_status: Literal["pass", "fail", "skipped"] = "skipped",
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
        return OdooStableTargetReplacementApplyResult(
            product=self.product,
            context=self.context,
            instance=self.instance,
            strategy="recreate-in-place",
            deployment_record_id=self.deployment_record_id,
            deploy_status=deploy_status,
            post_deploy_status=post_deploy_status,
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


def _raw_compose_route_evidence(
    *,
    compose_file: str,
    domain_hosts: tuple[str, ...],
) -> dict[str, str]:
    evidence: dict[str, str] = {
        "compose_sha256": control_plane_dokploy.compose_file_sha256(compose_file),
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
        route_name = control_plane_dokploy._traefik_route_name(domain_host=domain_host)
        evidence[f"domain_{domain_host}_http_rule_present"] = (
            "true"
            if f"traefik.http.routers.{route_name}-web.rule=Host(`{domain_host}`)"
            in compose_file
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
            sorted(str(domain.get("host") or "").strip() for domain in domains if domain.get("host"))
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
            "true" if _runtime_source_value(matching_domain.get("port")) == str(runtime_port) else "false"
        )
        evidence[f"{prefix}_https"] = _runtime_source_value(matching_domain.get("https"))
        evidence[f"{prefix}_certificate_type"] = _runtime_source_value(
            matching_domain.get("certificateType")
        )
        evidence[f"{prefix}_domain_type"] = _runtime_source_value(
            matching_domain.get("domainType")
        )
        evidence[f"{prefix}_path"] = _runtime_source_value(matching_domain.get("path"))
        evidence[f"{prefix}_internal_path"] = _runtime_source_value(
            matching_domain.get("internalPath")
        )
        evidence[f"{prefix}_strip_path"] = _runtime_source_value(
            matching_domain.get("stripPath")
        )
        evidence[f"{prefix}_unique_config_key_present"] = (
            "true" if _runtime_source_value(matching_domain.get("uniqueConfigKey")) else "false"
        )
    return evidence


def _dokploy_compose_metadata_evidence(target_payload: JsonObject) -> dict[str, str]:
    deployments = _collect_json_objects(target_payload.get("deployments"))
    latest_deployment = control_plane_dokploy._latest_deployment_from_list(deployments)
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
                "latest_deployment_key": control_plane_dokploy.deployment_key(latest_deployment),
                "latest_deployment_status": _runtime_source_value(
                    latest_deployment.get("status")
                ),
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


def _web_container_for_app(
    *, containers_payload: JsonValue, app_name: str
) -> JsonObject | None:
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
    config = control_plane_dokploy.as_json_object(config_payload)
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
    config = control_plane_dokploy.as_json_object(config_payload)
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
    app_containers = [container for container in containers if app_name in _container_name(container)]
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
        "container_web_id_present": "true" if web_container and _container_id(web_container) else "false",
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
        "container_has_dokploy_network": "true"
        if "dokploy-network" in network_names
        else "false",
    }
    label_items = tuple(labels.items())
    for raw_domain_host in expected_domain_hosts:
        domain_host = raw_domain_host.strip().lower()
        if not domain_host:
            continue
        route_name = control_plane_dokploy._traefik_route_name(domain_host=domain_host)
        prefix = f"container_domain_{domain_host}"
        evidence[f"{prefix}_http_rule_present"] = (
            "true"
            if labels.get(f"traefik.http.routers.{route_name}-web.rule")
            == f"Host(`{domain_host}`)"
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
        if (item_as_object := control_plane_dokploy.as_json_object(raw_item)) is not None
    ]


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
    if lane.health_url.strip():
        return lane.health_url.strip()
    base_url = _target_base_url(lane=lane, domains=domains)
    return default_odoo_health_url(base_url=base_url, health_path=profile.health_path)


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
    target_payload = control_plane_dokploy.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type=target_record.target_type,
        target_id=target_id_record.target_id,
    )
    env_map = control_plane_dokploy.parse_dokploy_env_text(str(target_payload.get("env") or ""))
    required_volume_keys = ("ODOO_DATA_VOLUME", "ODOO_LOG_VOLUME", "ODOO_DB_VOLUME")
    present_volume_keys = tuple(key for key in required_volume_keys if env_map.get(key, "").strip())
    missing_volume_keys = tuple(
        key for key in required_volume_keys if key not in present_volume_keys
    )
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
        source_type=str(
            target_payload.get("sourceType") or target_record.source_type or ""
        ).strip(),
        compose_path=str(
            target_payload.get("composePath") or target_record.compose_path or ""
        ).strip(),
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
    dokploy_request: DokployRequest | None = None,
    dokploy_config_reader: DokployConfigReader | None = None,
) -> OdooStableTargetReplacementPlan:
    resolved_dokploy_request = dokploy_request or control_plane_dokploy.dokploy_request
    resolved_dokploy_config_reader = (
        dokploy_config_reader or control_plane_dokploy.read_dokploy_config
    )
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
        allow_empty_data=request.allow_empty_data,
        data_source_mode=request.data_source_mode,
        approval_issue_url=approval_issue_url,
        blockers=blockers_tuple,
        warnings=tuple(warnings),
        steps=_build_steps(
            current_target=current_target,
            blockers=blockers_tuple,
            plan_strategy=request.strategy,
        ),
    )


def execute_odoo_stable_target_replacement_apply(
    *,
    control_plane_root: Path,
    record_store: OdooStableTargetReplacementStore,
    request: OdooStableTargetReplacementApplyRequest,
    dokploy_request: DokployRequest = control_plane_dokploy.dokploy_request,
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
    if not plan.expected_artifact_id.strip():
        raise click.ClickException(
            "Odoo target replacement apply requires inventory artifact evidence."
        )
    if not plan.expected_source_git_ref.strip():
        raise click.ClickException(
            "Odoo target replacement apply requires inventory source git ref evidence."
        )

    artifact_id = request.artifact_id or plan.expected_artifact_id
    source_git_ref = request.source_git_ref or plan.expected_source_git_ref
    artifact_manifest = record_store.read_artifact_manifest(artifact_id)
    if artifact_manifest.source_commit != source_git_ref:
        raise click.ClickException(
            "Odoo target replacement apply source ref does not match stored artifact manifest. "
            f"Request={source_git_ref} manifest={artifact_manifest.source_commit}."
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
        or control_plane_dokploy.DEFAULT_DOKPLOY_DEPLOY_TIMEOUT_SECONDS
    )
    health_timeout_seconds = (
        request.health_timeout_seconds
        or target_record.healthcheck_timeout_seconds
        or control_plane_dokploy.DEFAULT_DOKPLOY_HEALTH_TIMEOUT_SECONDS
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

    try:
        host, token = control_plane_dokploy.read_dokploy_config(
            control_plane_root=control_plane_root
        )
        target_payload = control_plane_dokploy.fetch_dokploy_target_payload(
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
        compose_file = control_plane_dokploy.render_odoo_raw_compose_file(
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
        raw_compose_evidence = control_plane_dokploy.sync_dokploy_compose_raw_source(
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
            control_plane_dokploy.ensure_compose_web_domain_route(
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
        converted_compose_file = control_plane_dokploy.fetch_dokploy_converted_compose_file(
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
        current_env_map = control_plane_dokploy.parse_dokploy_env_text(
            str(target_payload.get("env") or "")
        )
        desired_env_map = dict(current_env_map)
        desired_env_map.update(runtime_environment_values)
        desired_env_map["DOCKER_IMAGE_REFERENCE"] = image_reference
        desired_env_map.update(runtime_identity_env(runtime_identity))
        control_plane_dokploy.update_dokploy_target_env(
            host=host,
            token=token,
            target_type="compose",
            target_id=target_id_record.target_id,
            target_payload=target_payload,
            env_text=control_plane_dokploy.serialize_dokploy_env_text(desired_env_map),
        )
        refreshed_payload = control_plane_dokploy.fetch_dokploy_target_payload(
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
        refreshed_env_map = control_plane_dokploy.parse_dokploy_env_text(
            str(refreshed_payload.get("env") or "")
        )
        missing_keys = sorted(
            key for key, value in desired_env_map.items() if refreshed_env_map.get(key, "") != value
        )
        if missing_keys:
            raise click.ClickException(
                "Odoo target replacement env did not persist key(s): " + ", ".join(missing_keys)
            )
        latest_before = control_plane_dokploy.latest_deployment_for_target(
            host=host,
            token=token,
            target_type="compose",
            target_id=target_id_record.target_id,
        )
        control_plane_dokploy.trigger_deployment(
            host=host,
            token=token,
            target_type="compose",
            target_id=target_id_record.target_id,
            no_cache=request.no_cache,
        )
        control_plane_dokploy.wait_for_target_deployment(
            host=host,
            token=token,
            target_type="compose",
            target_id=target_id_record.target_id,
            before_key=control_plane_dokploy.deployment_key(latest_before),
            timeout_seconds=deploy_timeout_seconds,
        )
        deployed_payload = control_plane_dokploy.fetch_dokploy_target_payload(
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
        deployed_converted_compose_file = (
            control_plane_dokploy.fetch_dokploy_converted_compose_file(
                host=host,
                token=token,
                compose_id=target_id_record.target_id,
            )
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
            runtime_identity=runtime_identity,
            destination_health=HealthcheckEvidence(status="skipped"),
        )
        return base_result.result(
            deploy_status="fail",
            runtime_identity_injected=False,
            runtime_source=runtime_source,
            error_message=str(error),
        )

    post_deploy_result = execute_odoo_post_deploy(
        control_plane_root=control_plane_root,
        record_store=record_store,
        request=OdooPostDeployRequest(context=plan.context, instance=plan.instance, phase="deploy"),
        run_destructive_restore=plan.data_source_mode == "upstream_restore",
    )
    post_deploy_evidence = PostDeployUpdateEvidence(
        attempted=True,
        status=post_deploy_result.post_deploy_status,
        detail=post_deploy_result.error_message
        or "Odoo post-deploy completed after stable target replacement apply.",
    )
    if post_deploy_result.post_deploy_status != "pass":
        _write_failed_deployment(
            record_store=record_store,
            ship_request=ship_request,
            deployment_record_id=deployment_record_id,
            started_at=started_at,
            resolved_target=resolved_target,
            runtime_identity=runtime_identity,
            post_deploy_update=post_deploy_evidence,
            destination_health=HealthcheckEvidence(status="skipped"),
        )
        return base_result.result(
            deploy_status="fail",
            post_deploy_status=post_deploy_result.post_deploy_status,
            runtime_identity_injected=True,
            runtime_source=runtime_source,
            error_message=post_deploy_result.error_message or "Odoo post-deploy failed.",
        )

    base_url = _target_base_url(lane=lane, domains=plan.expected_domain_hosts)
    health_url = _target_health_url(profile=profile, lane=lane, domains=plan.expected_domain_hosts)
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
    destination_health = HealthcheckEvidence(
        verified=request.verify_health,
        urls=(health_url,) if request.verify_health and health_url else (),
        timeout_seconds=health_timeout_seconds if request.verify_health and health_url else None,
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
    return base_result.result(
        deploy_status="pass",
        post_deploy_status="pass",
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
