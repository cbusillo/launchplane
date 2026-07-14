import hashlib
import json
import re

from typing import Literal

import click

from control_plane.dokploy import api
from control_plane.dokploy.source import DokployTargetDefinition


ODOO_RAW_COMPOSE_REQUIRED_SERVICES = ("web", "database", "script-runner")


def _traefik_route_name(*, domain_host: str) -> str:
    normalized_host = domain_host.strip().lower()
    host_slug = re.sub(r"[^a-z0-9]+", "-", normalized_host).strip("-")
    if not host_slug:
        raise click.ClickException("Traefik route rendering requires a domain host.")
    host_hash = hashlib.sha1(normalized_host.encode("utf-8")).hexdigest()[:8]
    return f"launchplane-odoo-web-{host_slug[:48]}-{host_hash}"


def _render_odoo_web_traefik_labels(
    *,
    domain_hosts: tuple[str, ...],
    runtime_port: int,
    domain_certificate_type: Literal["none", "letsencrypt"],
) -> str:
    if not domain_hosts:
        return ""
    if runtime_port <= 0:
        raise click.ClickException("Odoo Traefik label rendering requires a positive port.")
    lines: list[str] = [
        "    networks:",
        "      - default",
        "      - dokploy-network",
        "    labels:",
        '      - "traefik.enable=true"',
        '      - "traefik.docker.network=dokploy-network"',
    ]
    seen_hosts: set[str] = set()
    for raw_domain_host in domain_hosts:
        domain_host = raw_domain_host.strip().lower()
        if not domain_host or domain_host in seen_hosts:
            continue
        if "`" in domain_host:
            raise click.ClickException(
                f"Odoo Traefik label rendering received an invalid domain host: {domain_host}"
            )
        seen_hosts.add(domain_host)
        route_name = _traefik_route_name(domain_host=domain_host)
        lines.extend(
            [
                f'      - "traefik.http.routers.{route_name}-web.rule=Host(`{domain_host}`)"',
                f'      - "traefik.http.routers.{route_name}-web.entrypoints=web"',
                f'      - "traefik.http.routers.{route_name}-web.service={route_name}-web"',
                f'      - "traefik.http.routers.{route_name}-web.middlewares=redirect-to-https@file"',
                f'      - "traefik.http.services.{route_name}-web.loadbalancer.server.port={runtime_port}"',
                f'      - "traefik.http.routers.{route_name}-websecure.rule=Host(`{domain_host}`)"',
                f'      - "traefik.http.routers.{route_name}-websecure.entrypoints=websecure"',
                f'      - "traefik.http.routers.{route_name}-websecure.service={route_name}-websecure"',
                f'      - "traefik.http.routers.{route_name}-websecure.tls=true"',
            ]
        )
        if domain_certificate_type == "letsencrypt":
            lines.append(
                f'      - "traefik.http.routers.{route_name}-websecure.tls.certresolver=letsencrypt"'
            )
        lines.append(
            f'      - "traefik.http.services.{route_name}-websecure.loadbalancer.server.port={runtime_port}"'
        )
    return "\n".join(lines) + "\n"


def render_odoo_raw_compose_file(
    *,
    image_reference: str,
    domain_hosts: tuple[str, ...] = (),
    runtime_port: int = 8069,
    publish_host_ports: bool = True,
    domain_certificate_type: Literal["none", "letsencrypt"] = "none",
) -> str:
    normalized_image_reference = image_reference.strip()
    if not normalized_image_reference:
        raise click.ClickException(
            "Odoo raw compose rendering requires a non-empty image reference."
        )
    rendered_image_reference = json.dumps(normalized_image_reference)
    web_route_labels = _render_odoo_web_traefik_labels(
        domain_hosts=domain_hosts,
        runtime_port=runtime_port,
        domain_certificate_type=domain_certificate_type,
    )
    web_host_ports = ""
    if publish_host_ports:
        web_host_ports = """    ports:
      - "${ODOO_WEB_HOST_PORT:-8069}:8069"
      - "${ODOO_LONGPOLL_HOST_PORT:-8072}:8072"
"""
    # Keep this intentionally close to odoo-devkit/docker-compose.yml. Launchplane
    # renders the image reference directly so Dokploy git checkout state cannot
    # decide what Odoo artifact is deployed.
    return f"""x-odoo-base: &odoo-base
  image: {rendered_image_reference}
  pull_policy: always
  restart: unless-stopped
  env_file:
    - path: .env
      required: false

x-odoo-env: &odoo-env
  ODOO_STACK_NAME: ${{ODOO_STACK_NAME:-}}
  ODOO_PROJECT_NAME: ${{ODOO_PROJECT_NAME:-}}
  PLATFORM_CONTEXT: ${{PLATFORM_CONTEXT:-}}
  PLATFORM_INSTANCE: ${{PLATFORM_INSTANCE:-}}
  ODOO_DB_HOST: database
  ODOO_DB_PORT: "5432"
  ODOO_DB_NAME: ${{ODOO_DB_NAME:?missing}}
  ODOO_DB_USER: ${{ODOO_DB_USER:?missing}}
  ODOO_DB_PASSWORD: ${{ODOO_DB_PASSWORD:?missing}}
  ODOO_ADMIN_LOGIN: ${{ODOO_ADMIN_LOGIN:-}}
  ODOO_ADMIN_PASSWORD: ${{ODOO_ADMIN_PASSWORD:-}}
  ODOO_DB_MAXCONN: ${{ODOO_DB_MAXCONN:-44}}
  ODOO_MAX_CRON_THREADS: ${{ODOO_MAX_CRON_THREADS:-2}}
  ODOO_WORKERS: ${{ODOO_WORKERS:-6}}
  ODOO_LIMIT_TIME_CPU: ${{ODOO_LIMIT_TIME_CPU:-600}}
  ODOO_LIMIT_TIME_REAL: ${{ODOO_LIMIT_TIME_REAL:-1800}}
  ODOO_LIMIT_TIME_REAL_CRON: ${{ODOO_LIMIT_TIME_REAL_CRON:-1800}}
  ODOO_LIMIT_MEMORY_SOFT: ${{ODOO_LIMIT_MEMORY_SOFT:-671088640}}
  ODOO_LIMIT_MEMORY_HARD: ${{ODOO_LIMIT_MEMORY_HARD:-805306368}}
  ODOO_DEV_MODE: ${{ODOO_DEV_MODE:-}}
  ODOO_INSTALL_MODULES: ${{ODOO_INSTALL_MODULES:-}}
  ODOO_UPDATE_MODULES: ${{ODOO_UPDATE_MODULES:-AUTO}}
  ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64: ${{ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64:-}}
  LAUNCHPLANE_INSTANCE_OVERRIDES_REQUIRED: ${{LAUNCHPLANE_INSTANCE_OVERRIDES_REQUIRED:-}}
  LAUNCHPLANE_WEBSITE_BOOTSTRAP_REQUIRED: ${{LAUNCHPLANE_WEBSITE_BOOTSTRAP_REQUIRED:-}}
  ODOO_ADDONS_PATH: ${{ODOO_ADDONS_PATH:-/opt/project/addons,/opt/extra_addons,/opt/launchplane/addons,/opt/enterprise,/odoo/addons}}
  ODOO_SERVER_WIDE_MODULES: ${{ODOO_SERVER_WIDE_MODULES:-base,web,launchplane_runtime_health}}
  ODOO_DATA_WORKFLOW_LOCK_FILE: ${{ODOO_DATA_WORKFLOW_LOCK_FILE:-/volumes/data/.data_workflow_in_progress}}
  ODOO_DATA_WORKFLOW_LOCK_TIMEOUT_SECONDS: ${{ODOO_DATA_WORKFLOW_LOCK_TIMEOUT_SECONDS:-7200}}
  IMAGE_ODOO_ENTERPRISE_LOCATION: /volumes/enterprise_disabled
  IMAGE_EXTRA_ADDONS_LOCATION: /opt/extra_addons

x-healthcheck-defaults: &healthcheck-defaults
  interval: 30s
  timeout: 5s

name: ${{ODOO_PROJECT_NAME:-odoo}}
services:
  web:
    <<: *odoo-base
    command:
      - /bin/sh
      - -lc
      - ${{ODOO_WEB_COMMAND:-python3 /volumes/scripts/run_odoo_startup.py -c /tmp/platform.odoo.conf}}
    volumes:
      - odoo_data:/volumes/data
      - odoo_logs:/volumes/logs
{web_host_ports.rstrip()}
    environment:
      <<: *odoo-env
    healthcheck:
      <<: *healthcheck-defaults
      test: >-
        curl -fsS http://127.0.0.1:${{ODOO_HTTP_PORT:-8069}}/web/health || exit 1
      retries: 5
      start_period: 20s
    extra_hosts:
      - "host.docker.internal:host-gateway"
{web_route_labels}

  database:
    image: postgres:17
    restart: unless-stopped
    ulimits:
      nofile:
        soft: ${{POSTGRES_ULIMIT_NOFILE_SOFT:-8192}}
        hard: ${{POSTGRES_ULIMIT_NOFILE_HARD:-8192}}
    command:
      - postgres
      - -c
      - max_connections=${{POSTGRES_MAX_CONNECTIONS:-100}}
      - -c
      - max_files_per_process=${{POSTGRES_MAX_FILES_PER_PROCESS:-4096}}
      - -c
      - shared_buffers=${{POSTGRES_SHARED_BUFFERS:-1GB}}
      - -c
      - effective_cache_size=${{POSTGRES_EFFECTIVE_CACHE_SIZE:-4GB}}
      - -c
      - work_mem=${{POSTGRES_WORK_MEM:-32MB}}
      - -c
      - maintenance_work_mem=${{POSTGRES_MAINTENANCE_WORK_MEM:-256MB}}
      - -c
      - max_wal_size=${{POSTGRES_MAX_WAL_SIZE:-1GB}}
      - -c
      - min_wal_size=${{POSTGRES_MIN_WAL_SIZE:-80MB}}
      - -c
      - checkpoint_timeout=${{POSTGRES_CHECKPOINT_TIMEOUT:-5min}}
      - -c
      - random_page_cost=${{POSTGRES_RANDOM_PAGE_COST:-4}}
      - -c
      - effective_io_concurrency=${{POSTGRES_EFFECTIVE_IO_CONCURRENCY:-1}}
    volumes:
      - odoo_db:/var/lib/postgresql/data
    environment:
      - POSTGRES_DB=postgres
      - POSTGRES_PASSWORD=${{ODOO_DB_PASSWORD}}
      - POSTGRES_USER=${{ODOO_DB_USER}}
    healthcheck:
      <<: *healthcheck-defaults
      test: >-
        pg_isready -U $$POSTGRES_USER -d $$POSTGRES_DB -h 127.0.0.1 -p 5432
      retries: 5
      start_period: 10s

  script-runner:
    <<: *odoo-base
    volumes:
      - odoo_data:/volumes/data
      - odoo_logs:/volumes/logs
      - ${{DATA_WORKFLOW_SSH_DIR:-/home/ubuntu/.ssh}}:/home/ubuntu/.ssh:ro
      - ${{DATA_WORKFLOW_SSH_DIR:-/home/ubuntu/.ssh}}:/root/.ssh:ro
    command: tail -f /dev/null
    working_dir: /opt/project
    shm_size: "2gb"
    healthcheck:
      <<: *healthcheck-defaults
      test: >-
        test -x /odoo/odoo-bin && test -f /volumes/scripts/run_odoo_data_workflows.py
      retries: 3
      start_period: 10s
    environment:
      <<: *odoo-env
      CHROMIUM_BIN: /usr/bin/chromium
      CHROMIUM_FLAGS: >-
        --headless=new --no-sandbox --disable-gpu --disable-dev-shm-usage
        --disable-software-rasterizer --window-size=1920,1080 --no-first-run
        --no-default-browser-check
        --disable-features=TranslateUI,site-per-process,IsolateOrigins,BlockInsecurePrivateNetworkRequests

volumes:
  odoo_data:
    name: ${{ODOO_DATA_VOLUME:?missing}}
  odoo_logs:
    name: ${{ODOO_LOG_VOLUME:?missing}}
  odoo_db:
    name: ${{ODOO_DB_VOLUME:?missing}}
  testkit_db:
  testkit_data:
  testkit_logs:

secrets:
  github_token:
    environment: GITHUB_TOKEN

networks:
  dokploy-network:
    external: true
"""


def compose_file_sha256(compose_file: str) -> str:
    return hashlib.sha256(compose_file.encode("utf-8")).hexdigest()


def _compose_file_has_required_service(*, compose_file: str, service_name: str) -> bool:
    return f"\n  {service_name}:" in f"\n{compose_file}"


def validate_odoo_raw_compose_file(*, compose_file: str) -> None:
    missing_services = [
        service_name
        for service_name in ODOO_RAW_COMPOSE_REQUIRED_SERVICES
        if not _compose_file_has_required_service(
            compose_file=compose_file, service_name=service_name
        )
    ]
    if missing_services:
        raise click.ClickException(
            "Odoo raw compose file is missing required services: " + ", ".join(missing_services)
        )


def update_dokploy_target_source(
    *,
    host: str,
    token: str,
    target_definition: DokployTargetDefinition,
    target_payload: api.JsonObject,
) -> None:
    if target_definition.target_type != "compose":
        raise click.ClickException(
            "Live target source sync currently supports compose targets only. "
            f"Configured={target_definition.target_type}."
        )

    environment_id = str(target_payload.get("environmentId") or "").strip()
    target_name = str(target_payload.get("name") or target_definition.target_name or "").strip()
    source_type = (
        target_definition.source_type.strip() or str(target_payload.get("sourceType") or "").strip()
    )
    compose_path = (
        target_definition.compose_path.strip()
        or str(target_payload.get("composePath") or "").strip()
    )
    custom_git_url = (
        target_definition.custom_git_url.strip()
        or str(target_payload.get("customGitUrl") or "").strip()
    )
    custom_git_branch = (
        target_definition.custom_git_branch.strip()
        or str(target_payload.get("customGitBranch") or "").strip()
    )
    custom_git_ssh_key_id = str(target_payload.get("customGitSSHKeyId") or "").strip()
    trigger_type = str(target_payload.get("triggerType") or "push").strip() or "push"
    raw_watch_paths = target_payload.get("watchPaths")
    watch_paths = list(target_definition.watch_paths) or api._string_items(raw_watch_paths)
    watch_path_values: list[api.JsonValue] = []
    watch_path_values.extend(watch_paths)
    auto_deploy = bool(target_payload.get("autoDeploy"))
    enable_submodules = (
        target_definition.enable_submodules
        if target_definition.enable_submodules is not None
        else bool(target_payload.get("enableSubmodules"))
    )

    if not environment_id:
        raise click.ClickException(
            f"Dokploy target {target_definition.context}/{target_definition.instance} is missing environmentId in the live payload."
        )
    if not target_name:
        raise click.ClickException(
            f"Dokploy target {target_definition.context}/{target_definition.instance} is missing name in the live payload."
        )
    if not source_type:
        raise click.ClickException(
            f"Dokploy target {target_definition.context}/{target_definition.instance} requires source_type before live source sync."
        )
    if source_type != "git":
        raise click.ClickException(
            f"Live target source sync currently supports source_type=git only. Configured={source_type}."
        )
    if not custom_git_url:
        raise click.ClickException(
            f"Dokploy target {target_definition.context}/{target_definition.instance} requires custom_git_url before live source sync."
        )
    if not custom_git_branch:
        raise click.ClickException(
            f"Dokploy target {target_definition.context}/{target_definition.instance} requires custom_git_branch before live source sync."
        )
    if not compose_path:
        raise click.ClickException(
            f"Dokploy target {target_definition.context}/{target_definition.instance} requires compose_path before live source sync."
        )

    payload: api.JsonObject = {
        "composeId": target_definition.target_id,
        "name": target_name,
        "environmentId": environment_id,
        "sourceType": source_type,
        "autoDeploy": auto_deploy,
        "composePath": compose_path,
        "customGitUrl": custom_git_url,
        "customGitBranch": custom_git_branch,
        "enableSubmodules": enable_submodules,
        "triggerType": trigger_type,
        "watchPaths": watch_path_values,
    }
    if custom_git_ssh_key_id:
        payload["customGitSSHKeyId"] = custom_git_ssh_key_id

    api.dokploy_request(
        host=host,
        token=token,
        path="/api/compose.update",
        method="POST",
        payload=payload,
    )


def sync_dokploy_compose_raw_source(
    *,
    host: str,
    token: str,
    compose_id: str,
    compose_name: str,
    target_payload: api.JsonObject,
    compose_file: str,
) -> dict[str, str]:
    normalized_compose_id = compose_id.strip()
    normalized_compose_name = compose_name.strip() or str(target_payload.get("name") or "").strip()
    environment_id = str(target_payload.get("environmentId") or "").strip()
    if not normalized_compose_id:
        raise click.ClickException("Raw compose source sync requires a non-empty compose id.")
    if not normalized_compose_name:
        raise click.ClickException(
            f"Raw compose source sync for {normalized_compose_id} requires a non-empty compose name."
        )
    if not environment_id:
        raise click.ClickException(
            f"Raw compose source sync for {normalized_compose_name} is missing environmentId in the live payload."
        )
    validate_odoo_raw_compose_file(compose_file=compose_file)

    expected_sha256 = compose_file_sha256(compose_file)
    existing_source_type = str(target_payload.get("sourceType") or "").strip()
    existing_compose_path = str(target_payload.get("composePath") or "").strip()
    existing_compose_file = str(target_payload.get("composeFile") or "")
    if (
        existing_source_type == "raw"
        and existing_compose_path == "docker-compose.yml"
        and compose_file_sha256(existing_compose_file) == expected_sha256
    ):
        return _build_raw_compose_evidence(
            source_type=existing_source_type,
            compose_file=compose_file,
            changed=False,
        )

    api.dokploy_request(
        host=host,
        token=token,
        path="/api/compose.update",
        method="POST",
        payload={
            "composeId": normalized_compose_id,
            "name": normalized_compose_name,
            "environmentId": environment_id,
            "sourceType": "raw",
            "composePath": "docker-compose.yml",
            "autoDeploy": bool(target_payload.get("autoDeploy")),
            "composeFile": compose_file,
        },
    )
    refreshed_payload = api.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type="compose",
        target_id=normalized_compose_id,
    )
    refreshed_source_type = str(refreshed_payload.get("sourceType") or "").strip()
    refreshed_compose_path = str(refreshed_payload.get("composePath") or "").strip()
    refreshed_compose_file = str(refreshed_payload.get("composeFile") or "")
    if refreshed_source_type != "raw":
        raise click.ClickException(
            f"Dokploy compose {normalized_compose_name} did not retain sourceType=raw after update. "
            f"Live sourceType={refreshed_source_type or '<empty>'}."
        )
    if refreshed_compose_path != "docker-compose.yml":
        raise click.ClickException(
            f"Dokploy compose {normalized_compose_name} did not retain composePath=docker-compose.yml after raw update. "
            f"Live composePath={refreshed_compose_path or '<empty>'}."
        )
    if compose_file_sha256(refreshed_compose_file) != expected_sha256:
        raise click.ClickException(
            f"Dokploy compose {normalized_compose_name} did not retain the Launchplane-rendered raw compose content."
        )
    validate_odoo_raw_compose_file(compose_file=refreshed_compose_file)
    return _build_raw_compose_evidence(
        source_type=refreshed_source_type,
        compose_file=refreshed_compose_file,
        changed=True,
    )


def ensure_compose_web_domain_route(
    *,
    host: str,
    token: str,
    compose_id: str,
    domain_host: str,
    runtime_port: int,
    certificate_type: str = "none",
) -> str:
    normalized_compose_id = compose_id.strip()
    normalized_domain_host = domain_host.strip()
    if not normalized_compose_id:
        raise click.ClickException("Compose domain route reconciliation requires a compose id.")
    if not normalized_domain_host:
        raise click.ClickException("Compose domain route reconciliation requires a domain host.")
    if runtime_port <= 0:
        raise click.ClickException("Compose domain route reconciliation requires a positive port.")
    normalized_certificate_type = certificate_type.strip() or "none"

    raw_domains = api.dokploy_request(
        host=host,
        token=token,
        path="/api/domain.byComposeId",
        query={"composeId": normalized_compose_id},
    )
    domains = raw_domains if isinstance(raw_domains, list) else []
    existing: api.JsonObject | None = None
    for raw_domain in domains:
        domain = api.as_json_object(raw_domain)
        if domain is None:
            continue
        if str(domain.get("host") or "").strip() == normalized_domain_host:
            existing = domain
            break

    payload: api.JsonObject = {
        "host": normalized_domain_host,
        "path": "/",
        "internalPath": "/",
        "port": runtime_port,
        "https": True,
        "applicationId": None,
        "certificateType": normalized_certificate_type,
        "customCertResolver": None,
        "composeId": normalized_compose_id,
        "serviceName": "web",
        "domainType": "compose",
        "previewDeploymentId": None,
        "stripPath": False,
    }
    if existing is not None:
        domain_id = str(existing.get("domainId") or "").strip()
        if not domain_id:
            raise click.ClickException(
                f"Dokploy domain {normalized_domain_host} is missing domainId."
            )
        api.dokploy_request(
            host=host,
            token=token,
            path="/api/domain.update",
            method="POST",
            payload={"domainId": domain_id, **payload},
        )
        return domain_id

    created = api.dokploy_request(
        host=host,
        token=token,
        path="/api/domain.create",
        method="POST",
        payload=payload,
    )
    created_domain = api.as_json_object(created)
    domain_id = str((created_domain or {}).get("domainId") or "").strip()
    if not domain_id:
        raise click.ClickException(
            f"Dokploy domain create returned no domainId for {normalized_domain_host}."
        )
    return domain_id


def fetch_dokploy_converted_compose_file(
    *,
    host: str,
    token: str,
    compose_id: str,
) -> str:
    normalized_compose_id = compose_id.strip()
    if not normalized_compose_id:
        raise click.ClickException("Converted compose fetch requires a compose id.")
    payload = api.dokploy_request(
        host=host,
        token=token,
        path="/api/compose.getConvertedCompose",
        query={"composeId": normalized_compose_id},
    )
    if isinstance(payload, str):
        return payload
    payload_as_object = api.as_json_object(payload)
    if payload_as_object is not None:
        for key_name in ("composeFile", "compose", "content", "raw"):
            value = payload_as_object.get(key_name)
            if isinstance(value, str):
                return value
    raise click.ClickException("Dokploy converted compose response did not include compose text.")


def _build_raw_compose_evidence(
    *, source_type: str, compose_file: str, changed: bool
) -> dict[str, str]:
    return {
        "source_type": source_type,
        "compose_sha256": compose_file_sha256(compose_file),
        "compose_bytes": str(len(compose_file.encode("utf-8"))),
        "compose_path": "docker-compose.yml",
        "required_services": ",".join(ODOO_RAW_COMPOSE_REQUIRED_SERVICES),
        "changed": "true" if changed else "false",
    }
