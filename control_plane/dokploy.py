import hashlib
import json
import re
import shlex
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane import secrets as control_plane_secrets
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetPolicies
from control_plane.storage.factory import resolve_database_url
from control_plane.storage.postgres import PostgresRecordStore

DEFAULT_DOKPLOY_DEPLOY_TIMEOUT_SECONDS = 600
DEFAULT_DOKPLOY_HEALTH_TIMEOUT_SECONDS = 180
DEFAULT_DOKPLOY_HEALTHCHECK_PATH = "/web/health"
DEFAULT_DOKPLOY_LOG_LINE_COUNT = 200
MAX_DOKPLOY_LOG_LINE_COUNT = 1000
DEFAULT_CONTROL_PLANE_DOKPLOY_SOURCE_FILE = Path("config/dokploy.toml")
DEFAULT_CONTROL_PLANE_DOKPLOY_TARGET_IDS_FILE = Path("config/dokploy-targets.toml")
DEFAULT_STABLE_REMOTE_INSTANCES = {"testing", "prod"}
DOKPLOY_DATA_WORKFLOW_SCHEDULE_NAME = "platform-data-workflow"
DOKPLOY_ODOO_BACKUP_GATE_SCHEDULE_NAME = "platform-odoo-backup-gate"
DOKPLOY_MANUAL_ONLY_CRON_EXPRESSION = "0 0 31 2 *"
DOKPLOY_RUNNING_DEPLOYMENT_STATUSES = {"pending", "queued", "running", "in_progress", "starting"}
DOKPLOY_CANCELLED_DEPLOYMENT_STATUSES = {"cancelled", "canceled"}
DOKPLOY_SUCCESS_DEPLOYMENT_STATUSES = {
    "done",
    "success",
    "succeeded",
    "completed",
    "finished",
    "healthy",
}
POST_DEPLOY_UPDATE_IGNORED_ENV_KEYS = {
    "DOKPLOY_HOST",
    "DOKPLOY_TOKEN",
}
POST_DEPLOY_UPDATE_ALLOWED_ENV_KEYS = {
    "ODOO_DB_NAME",
    "ODOO_FILESTORE_PATH",
    "ODOO_DATA_WORKFLOW_LOCK_FILE",
}
DEFAULT_DATA_WORKFLOW_LOCK_PATH = "/volumes/data/.data_workflow_in_progress"
DEFAULT_ODOO_BACKUP_ROOT = "/volumes/data/backups/launchplane"
ODOO_RAW_COMPOSE_REQUIRED_SERVICES = ("web", "database", "script-runner")
_LIKELY_SECRET_LOG_VALUE_PATTERN = re.compile(
    r"(?i)(\b[A-Z0-9_]*(?:PASSWORD|PASS|TOKEN|SECRET|API_KEY|ACCESS_KEY|PRIVATE_KEY)[A-Z0-9_]*\s*[=:]\s*)([^\s,;]+)"
)
_DOUBLE_QUOTED_SECRET_LOG_VALUE_PATTERN = re.compile(
    r'(?i)("?\b[A-Z0-9_]*(?:PASSWORD|PASS|TOKEN|SECRET|API_KEY|ACCESS_KEY|PRIVATE_KEY)[A-Z0-9_]*"?\s*[=:]\s*)"[^"\r\n]*"'
)
_SINGLE_QUOTED_SECRET_LOG_VALUE_PATTERN = re.compile(
    r"(?i)('?\b[A-Z0-9_]*(?:PASSWORD|PASS|TOKEN|SECRET|API_KEY|ACCESS_KEY|PRIVATE_KEY)[A-Z0-9_]*'?\s*[=:]\s*)'[^'\r\n]*'"
)
_BEARER_LOG_VALUE_PATTERN = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")
_DOKPLOY_LOG_SINCE_PATTERN = re.compile(r"^(all|\d+[smhd])$")
_DOKPLOY_LOG_SEARCH_PATTERN = re.compile(r"^[a-zA-Z0-9 ._-]{0,500}$")


type JsonPrimitive = str | int | float | bool | None
type JsonValue = JsonPrimitive | dict[str, "JsonValue"] | list["JsonValue"]
type JsonObject = dict[str, JsonValue]


def render_odoo_raw_compose_file(*, image_reference: str) -> str:
    normalized_image_reference = image_reference.strip()
    if not normalized_image_reference:
        raise click.ClickException(
            "Odoo raw compose rendering requires a non-empty image reference."
        )
    # Keep this intentionally close to odoo-devkit/docker-compose.yml. Launchplane
    # renders the image reference directly so Dokploy git checkout state cannot
    # decide what Odoo artifact is deployed.
    return f"""x-odoo-base: &odoo-base
  image: {normalized_image_reference}
  pull_policy: always
  restart: unless-stopped
  env_file:
    - path: .env
      required: false

x-odoo-env: &odoo-env
  ODOO_STACK_NAME: ${{ODOO_STACK_NAME:-}}
  ODOO_PROJECT_NAME: ${{ODOO_PROJECT_NAME:-}}
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
  ODOO_ADDONS_PATH: ${{ODOO_ADDONS_PATH:-/opt/project/addons,/opt/extra_addons,/opt/enterprise,/odoo/addons}}
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
      - ${{ODOO_WEB_COMMAND:-/odoo/odoo-bin}}
    volumes:
      - odoo_data:/volumes/data
      - odoo_logs:/volumes/logs
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


class DokployTargetDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: str
    instance: str
    project_name: str = ""
    target_type: Literal["compose", "application"] = "compose"
    target_id: str = ""
    target_name: str = ""
    git_branch: str = ""
    source_git_ref: str = "origin/main"
    source_type: str = ""
    custom_git_url: str = ""
    custom_git_branch: str = ""
    compose_path: str = ""
    watch_paths: tuple[str, ...] = ()
    enable_submodules: bool | None = None
    require_test_gate: bool = False
    require_prod_gate: bool = False
    deploy_timeout_seconds: int | None = Field(default=None, ge=1)
    healthcheck_enabled: bool = True
    healthcheck_path: str = DEFAULT_DOKPLOY_HEALTHCHECK_PATH
    healthcheck_timeout_seconds: int | None = Field(default=None, ge=1)
    env: dict[str, str] = Field(default_factory=dict)
    domains: tuple[str, ...] = ()
    policies: DokployTargetPolicies = Field(default_factory=DokployTargetPolicies)

    @model_validator(mode="after")
    def _validate_identity_fields(self) -> "DokployTargetDefinition":
        if not self.context.strip():
            raise ValueError("Dokploy target requires non-empty context")
        if not self.instance.strip():
            raise ValueError("Dokploy target requires non-empty instance")
        if not self.target_id.strip():
            raise ValueError(
                f"Dokploy target {self.context}/{self.instance} requires non-empty target_id"
            )
        return self


class DokploySourceOfTruth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(ge=1)
    targets: tuple[DokployTargetDefinition, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _normalize_inherited_targets(cls, raw_value: object) -> object:
        return _normalize_dokploy_source_payload(raw_value)

    @model_validator(mode="after")
    def _validate_unique_target_routes(self) -> "DokploySourceOfTruth":
        seen_targets: set[tuple[str, str]] = set()
        for target_definition in self.targets:
            target_route = (target_definition.context.strip(), target_definition.instance.strip())
            if target_route in seen_targets:
                context_name, instance_name = target_route
                raise ValueError(
                    f"Duplicate Dokploy target definition for {context_name}/{instance_name} in source-of-truth"
                )
            seen_targets.add(target_route)
            if target_definition.instance not in DEFAULT_STABLE_REMOTE_INSTANCES:
                supported_instances = ", ".join(sorted(DEFAULT_STABLE_REMOTE_INSTANCES))
                raise ValueError(
                    "Tracked Dokploy source-of-truth only supports stable remote instances "
                    f"{supported_instances}; found {target_definition.context}/{target_definition.instance}. "
                    "Use Launchplane preview records for PR previews instead of adding another tracked Dokploy lane."
                )
        return self


def find_dokploy_target_definition(
    source_of_truth: DokploySourceOfTruth,
    *,
    context_name: str,
    instance_name: str,
) -> DokployTargetDefinition | None:
    for target in source_of_truth.targets:
        if target.context == context_name and target.instance == instance_name:
            return target
    return None


def protected_shopify_store_keys_for_target_definition(
    target_definition: DokployTargetDefinition,
) -> tuple[str, ...]:
    return target_definition.policies.shopify.protected_store_keys


def resolve_ship_timeout_seconds(
    *,
    timeout_override_seconds: int | None,
    target_definition: DokployTargetDefinition,
) -> int:
    if timeout_override_seconds is not None:
        if timeout_override_seconds <= 0:
            raise click.ClickException("Ship timeout must be greater than zero seconds.")
        return timeout_override_seconds
    if target_definition.deploy_timeout_seconds is not None:
        return target_definition.deploy_timeout_seconds
    return DEFAULT_DOKPLOY_DEPLOY_TIMEOUT_SECONDS


def resolve_ship_health_timeout_seconds(
    *,
    health_timeout_override_seconds: int | None,
    target_definition: DokployTargetDefinition | None,
) -> int:
    if health_timeout_override_seconds is not None:
        if health_timeout_override_seconds <= 0:
            raise click.ClickException("Ship health timeout must be greater than zero seconds.")
        return health_timeout_override_seconds
    if target_definition is not None and target_definition.healthcheck_timeout_seconds is not None:
        return target_definition.healthcheck_timeout_seconds
    return DEFAULT_DOKPLOY_HEALTH_TIMEOUT_SECONDS


def resolve_dokploy_ship_mode(
    context_name: str, instance_name: str, environment_values: dict[str, str]
) -> str:
    specific_key = f"DOKPLOY_SHIP_MODE_{context_name}_{instance_name}".upper()
    configured_mode = environment_values.get(specific_key, "").strip().lower()
    if not configured_mode:
        configured_mode = (
            environment_values.get("DOKPLOY_SHIP_MODE", "auto").strip().lower() or "auto"
        )
    if configured_mode not in {"auto", "compose", "application"}:
        raise click.ClickException(
            f"Invalid Dokploy ship mode '{configured_mode}'. Expected auto, compose, or application."
        )
    return configured_mode


def normalize_healthcheck_path(raw_healthcheck_path: str) -> str:
    normalized_path = raw_healthcheck_path.strip() or DEFAULT_DOKPLOY_HEALTHCHECK_PATH
    if not normalized_path.startswith("/"):
        normalized_path = f"/{normalized_path}"
    return normalized_path


def resolve_healthcheck_base_urls(
    *,
    target_definition: DokployTargetDefinition | None,
    environment_values: dict[str, str],
) -> tuple[str, ...]:
    raw_base_urls: list[str] = []
    if target_definition is not None:
        raw_base_urls.extend(domain for domain in target_definition.domains if domain)

    normalized_base_urls: list[str] = []
    for raw_base_url in raw_base_urls:
        stripped_base_url = raw_base_url.strip()
        if not stripped_base_url:
            continue
        parsed_base_url = urlparse(stripped_base_url)
        if not parsed_base_url.scheme:
            stripped_base_url = f"https://{stripped_base_url}"
        stripped_base_url = stripped_base_url.rstrip("/")
        if stripped_base_url and stripped_base_url not in normalized_base_urls:
            normalized_base_urls.append(stripped_base_url)

    return tuple(normalized_base_urls)


def resolve_ship_healthcheck_urls(
    *,
    target_definition: DokployTargetDefinition | None,
    environment_values: dict[str, str],
) -> tuple[str, ...]:
    if target_definition is not None and not target_definition.healthcheck_enabled:
        return ()

    healthcheck_path = normalize_healthcheck_path(
        target_definition.healthcheck_path
        if target_definition is not None
        else DEFAULT_DOKPLOY_HEALTHCHECK_PATH
    )
    base_urls = resolve_healthcheck_base_urls(
        target_definition=target_definition, environment_values=environment_values
    )
    return tuple(f"{base_url}{healthcheck_path}" for base_url in base_urls)


def read_control_plane_environment_values(*, control_plane_root: Path) -> dict[str, str]:
    del control_plane_root
    return control_plane_secrets.overlay_dokploy_environment_values(environment_values={})


def read_control_plane_dokploy_source_of_truth(*, control_plane_root: Path) -> DokploySourceOfTruth:
    del control_plane_root
    database_url = resolve_database_url()
    if not database_url:
        raise click.ClickException(
            "Missing Launchplane tracked Dokploy target authority. Configure DB-backed tracked target records."
        )
    source_of_truth = _load_optional_dokploy_source_of_truth_from_database(
        database_url=database_url
    )
    if source_of_truth is None:
        raise click.ClickException("Missing DB-backed Launchplane tracked Dokploy target records.")
    return source_of_truth


def build_dokploy_target_record_from_definition(
    definition: DokployTargetDefinition,
    *,
    updated_at: str,
    source_label: str = "",
) -> DokployTargetRecord:
    return DokployTargetRecord(
        context=definition.context,
        instance=definition.instance,
        project_name=definition.project_name,
        target_type=definition.target_type,
        target_name=definition.target_name,
        git_branch=definition.git_branch,
        source_git_ref=definition.source_git_ref,
        source_type=definition.source_type,
        custom_git_url=definition.custom_git_url,
        custom_git_branch=definition.custom_git_branch,
        compose_path=definition.compose_path,
        watch_paths=definition.watch_paths,
        enable_submodules=definition.enable_submodules,
        require_test_gate=definition.require_test_gate,
        require_prod_gate=definition.require_prod_gate,
        deploy_timeout_seconds=definition.deploy_timeout_seconds,
        healthcheck_enabled=definition.healthcheck_enabled,
        healthcheck_path=definition.healthcheck_path,
        healthcheck_timeout_seconds=definition.healthcheck_timeout_seconds,
        env=dict(definition.env),
        domains=definition.domains,
        policies=definition.policies,
        updated_at=updated_at,
        source_label=source_label,
    )


def build_dokploy_source_of_truth_from_records(
    target_records: tuple[DokployTargetRecord, ...],
    target_id_records: tuple[DokployTargetIdRecord, ...],
) -> DokploySourceOfTruth:
    target_id_map = {
        (record.context.strip(), record.instance.strip()): record.target_id
        for record in target_id_records
    }
    remaining_target_id_routes = set(target_id_map)
    targets_payload: list[dict[str, object]] = []
    for record in target_records:
        target_route = (record.context.strip(), record.instance.strip())
        target_id = target_id_map.get(target_route, "").strip()
        if not target_id:
            raise click.ClickException(
                "Missing DB-backed Dokploy target-id record for "
                f"{record.context}/{record.instance}."
            )
        remaining_target_id_routes.discard(target_route)
        targets_payload.append(
            {
                "context": record.context,
                "instance": record.instance,
                "project_name": record.project_name,
                "target_type": record.target_type,
                "target_id": target_id,
                "target_name": record.target_name,
                "git_branch": record.git_branch,
                "source_git_ref": record.source_git_ref,
                "source_type": record.source_type,
                "custom_git_url": record.custom_git_url,
                "custom_git_branch": record.custom_git_branch,
                "compose_path": record.compose_path,
                "watch_paths": list(record.watch_paths),
                "enable_submodules": record.enable_submodules,
                "require_test_gate": record.require_test_gate,
                "require_prod_gate": record.require_prod_gate,
                "deploy_timeout_seconds": record.deploy_timeout_seconds,
                "healthcheck_enabled": record.healthcheck_enabled,
                "healthcheck_path": record.healthcheck_path,
                "healthcheck_timeout_seconds": record.healthcheck_timeout_seconds,
                "env": dict(record.env),
                "domains": list(record.domains),
                "policies": record.policies.model_dump(mode="python"),
            }
        )
    if remaining_target_id_routes:
        unknown_routes = ", ".join(
            f"{context_name}/{instance_name}"
            for context_name, instance_name in sorted(remaining_target_id_routes)
        )
        raise click.ClickException(
            "DB-backed Dokploy target-id records contain route(s) that are not present in the tracked target records: "
            f"{unknown_routes}"
        )
    return DokploySourceOfTruth.model_validate({"schema_version": 1, "targets": targets_payload})


def _load_optional_dokploy_source_of_truth_from_database(
    *, database_url: str
) -> DokploySourceOfTruth | None:
    record_store: PostgresRecordStore | None = None
    try:
        record_store = PostgresRecordStore(database_url=database_url)
        record_store.ensure_schema()
        target_records = record_store.list_dokploy_target_records()
        if not target_records:
            return None
        target_id_records = record_store.list_dokploy_target_id_records()
    except click.ClickException:
        raise
    except Exception as error:
        raise click.ClickException(
            f"Could not load tracked Dokploy targets from Launchplane Postgres storage: {error}"
        ) from error
    finally:
        try:
            if record_store is not None:
                record_store.close()
        except Exception:
            pass
    return build_dokploy_source_of_truth_from_records(target_records, target_id_records)


def read_dokploy_config(*, control_plane_root: Path) -> tuple[str, str]:
    environment_values = read_control_plane_environment_values(
        control_plane_root=control_plane_root
    )

    host = environment_values.get("DOKPLOY_HOST", "").strip()
    token = environment_values.get("DOKPLOY_TOKEN", "").strip()
    if not host or not token:
        raise click.ClickException(
            "Missing DOKPLOY_HOST or DOKPLOY_TOKEN for control-plane Dokploy execution. "
            "Configure Launchplane-managed Dokploy secrets in the shared store before running Dokploy operations."
        )
    return host, token


def trigger_deployment(
    *, host: str, token: str, target_type: str, target_id: str, no_cache: bool
) -> None:
    if target_type == "compose":
        endpoint_path = "/api/compose.redeploy" if no_cache else "/api/compose.deploy"
        payload: JsonObject = {"composeId": target_id}
    elif target_type == "application":
        endpoint_path = "/api/application.redeploy" if no_cache else "/api/application.deploy"
        payload = {"applicationId": target_id}
    else:
        raise click.ClickException(f"Unsupported Dokploy target type: {target_type}")
    if no_cache:
        payload["title"] = "Manual redeploy (no-cache requested)"
    dokploy_request(host=host, token=token, path=endpoint_path, method="POST", payload=payload)


def latest_deployment_for_target(
    *, host: str, token: str, target_type: str, target_id: str
) -> JsonObject | None:
    if target_type == "compose":
        compose_payload = dokploy_request(
            host=host,
            token=token,
            path="/api/compose.one",
            query={"composeId": target_id},
        )
        compose_payload_as_object = as_json_object(compose_payload)
        if compose_payload_as_object is None:
            return None
        deployments_payload = compose_payload_as_object.get("deployments")
        if not isinstance(deployments_payload, list):
            return None
        return _latest_deployment_from_list(_collect_object_items(deployments_payload))

    if target_type == "application":
        payload = dokploy_request(
            host=host,
            token=token,
            path="/api/deployment.all",
            query={"applicationId": target_id},
        )
        return _latest_deployment_from_list(extract_deployments(payload))

    raise click.ClickException(f"Unsupported Dokploy target type: {target_type}")


def fetch_dokploy_target_payload(
    *,
    host: str,
    token: str,
    target_type: str,
    target_id: str,
) -> JsonObject:
    if target_type == "compose":
        payload = dokploy_request(
            host=host,
            token=token,
            path="/api/compose.one",
            query={"composeId": target_id},
        )
    elif target_type == "application":
        payload = dokploy_request(
            host=host,
            token=token,
            path="/api/application.one",
            query={"applicationId": target_id},
        )
    else:
        raise click.ClickException(f"Unsupported target type: {target_type}")

    payload_as_object = as_json_object(payload)
    if payload_as_object is None:
        raise click.ClickException(
            f"Dokploy {target_type}.one returned an invalid response payload."
        )
    return payload_as_object


def normalize_dokploy_log_line_count(line_count: int) -> int:
    if line_count < 1:
        raise click.ClickException("Dokploy log line count must be at least 1.")
    if line_count > MAX_DOKPLOY_LOG_LINE_COUNT:
        raise click.ClickException(
            f"Dokploy log line count cannot exceed {MAX_DOKPLOY_LOG_LINE_COUNT}."
        )
    return line_count


def normalize_dokploy_log_since(raw_since: str) -> str:
    since = raw_since.strip() or "all"
    if not _DOKPLOY_LOG_SINCE_PATTERN.fullmatch(since):
        raise click.ClickException(
            "Dokploy log --since must be 'all' or a duration like 5m, 2h, or 1d."
        )
    return since


def normalize_dokploy_log_search(raw_search: str) -> str:
    search = raw_search.strip()
    if not _DOKPLOY_LOG_SEARCH_PATTERN.fullmatch(search):
        raise click.ClickException(
            "Dokploy log --search may contain only letters, numbers, spaces, dots, underscores, and dashes."
        )
    return search


def redact_dokploy_log_line(raw_line: str) -> str:
    redacted_line = _DOUBLE_QUOTED_SECRET_LOG_VALUE_PATTERN.sub(r'\1"[redacted]"', raw_line)
    redacted_line = _SINGLE_QUOTED_SECRET_LOG_VALUE_PATTERN.sub(r"\1'[redacted]'", redacted_line)
    redacted_line = _LIKELY_SECRET_LOG_VALUE_PATTERN.sub(r"\1[redacted]", redacted_line)
    return _BEARER_LOG_VALUE_PATTERN.sub(r"\1[redacted]", redacted_line)


def normalize_dokploy_log_payload(payload: JsonValue) -> tuple[str, ...]:
    raw_lines: list[str] = []
    if isinstance(payload, str):
        raw_lines.extend(payload.splitlines())
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, str):
                raw_lines.extend(item.splitlines())
            elif isinstance(item, dict):
                message = item.get("message") or item.get("log") or item.get("line")
                if message is not None:
                    raw_lines.extend(str(message).splitlines())
    elif isinstance(payload, dict):
        for key_name in ("message", "line"):
            value = payload.get(key_name)
            if value is not None:
                raw_lines.extend(str(value).splitlines())
                break
        for key_name in ("logs", "log", "lines", "output", "raw"):
            value = payload.get(key_name)
            if isinstance(value, str):
                raw_lines.extend(value.splitlines())
                break
            if isinstance(value, list):
                raw_lines.extend(
                    line for item in value for line in normalize_dokploy_log_payload(item)
                )
                break
    return tuple(redact_dokploy_log_line(line) for line in raw_lines)


def fetch_dokploy_application_logs(
    *,
    host: str,
    token: str,
    application_id: str,
    line_count: int = DEFAULT_DOKPLOY_LOG_LINE_COUNT,
    since: str = "all",
    search: str = "",
) -> tuple[str, ...]:
    normalized_application_id = application_id.strip()
    if not normalized_application_id:
        raise click.ClickException("Dokploy application logs require an application id.")
    normalized_line_count = normalize_dokploy_log_line_count(line_count)
    normalized_since = normalize_dokploy_log_since(since)
    normalized_search = normalize_dokploy_log_search(search)
    query: dict[str, str | int] = {
        "applicationId": normalized_application_id,
        "tail": normalized_line_count,
        "since": normalized_since,
    }
    if normalized_search:
        query["search"] = normalized_search
    payload = dokploy_request(
        host=host,
        token=token,
        path="/api/application.readLogs",
        query=query,
    )
    lines = normalize_dokploy_log_payload(payload)
    return lines[-normalized_line_count:]


def parse_dokploy_env_text(raw_env_text: str) -> dict[str, str]:
    env_map: dict[str, str] = {}
    for raw_line in raw_env_text.splitlines():
        stripped_line = raw_line.strip()
        if not stripped_line or stripped_line.startswith("#"):
            continue
        if stripped_line.startswith("export "):
            stripped_line = stripped_line[7:].strip()
        if "=" not in stripped_line:
            continue
        key_part, value_part = stripped_line.split("=", 1)
        env_map[key_part.strip()] = value_part
    return env_map


def render_dokploy_env_text_with_overrides(
    raw_env_text: str,
    *,
    updates: Mapping[str, str] | None = None,
    removals: tuple[str, ...] = (),
) -> str:
    env_map = parse_dokploy_env_text(raw_env_text)
    for env_key in removals:
        env_map.pop(env_key, None)
    if updates is not None:
        for env_key, env_value in updates.items():
            env_map[env_key] = env_value
    return serialize_dokploy_env_text(env_map)


def serialize_dokploy_env_text(env_map: dict[str, str]) -> str:
    if not env_map:
        return ""
    rendered_lines = [f"{env_key}={env_value}" for env_key, env_value in env_map.items()]
    return "\n".join(rendered_lines)


def update_dokploy_target_env(
    *,
    host: str,
    token: str,
    target_type: str,
    target_id: str,
    target_payload: JsonObject,
    env_text: str,
) -> None:
    if target_type == "compose":
        dokploy_request(
            host=host,
            token=token,
            path="/api/compose.update",
            method="POST",
            payload={"composeId": target_id, "env": env_text},
        )
        return

    if target_type == "application":
        build_args = target_payload.get("buildArgs")
        build_secrets = target_payload.get("buildSecrets")
        create_env_file = target_payload.get("createEnvFile")
        payload: JsonObject = {
            "applicationId": target_id,
            "env": env_text,
            "createEnvFile": bool(create_env_file) if isinstance(create_env_file, bool) else True,
        }
        payload["buildArgs"] = build_args if isinstance(build_args, str) else ""
        payload["buildSecrets"] = build_secrets if isinstance(build_secrets, str) else ""
        dokploy_request(
            host=host,
            token=token,
            path="/api/application.saveEnvironment",
            method="POST",
            payload=payload,
        )
        return

    raise click.ClickException(f"Unsupported target type: {target_type}")


def update_dokploy_target_source(
    *,
    host: str,
    token: str,
    target_definition: DokployTargetDefinition,
    target_payload: JsonObject,
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
    watch_paths = list(target_definition.watch_paths) or (
        list(raw_watch_paths) if isinstance(raw_watch_paths, list) else []
    )
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

    payload: JsonObject = {
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
        "watchPaths": watch_paths,
    }
    if custom_git_ssh_key_id:
        payload["customGitSSHKeyId"] = custom_git_ssh_key_id

    dokploy_request(
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
    target_payload: JsonObject,
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

    dokploy_request(
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
    refreshed_payload = fetch_dokploy_target_payload(
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


def wait_for_target_deployment(
    *,
    host: str,
    token: str,
    target_type: str,
    target_id: str,
    before_key: str,
    timeout_seconds: int,
) -> str:
    failure_message_prefix = (
        "Dokploy compose deployment failed"
        if target_type == "compose"
        else "Dokploy deployment failed"
    )
    return _wait_for_deployment_status(
        fetch_latest_deployment=lambda: latest_deployment_for_target(
            host=host,
            token=token,
            target_type=target_type,
            target_id=target_id,
        ),
        before_key=before_key,
        timeout_seconds=timeout_seconds,
        failure_message_prefix=failure_message_prefix,
    )


def resolve_dokploy_user_id(*, host: str, token: str) -> str:
    payload = dokploy_request(host=host, token=token, path="/api/user.session")
    payload_as_object = as_json_object(payload)
    if payload_as_object is None:
        raise click.ClickException("Dokploy user.session returned an invalid response payload.")
    user_payload = as_json_object(payload_as_object.get("user"))
    if user_payload is None:
        raise click.ClickException("Dokploy user.session returned no user payload.")
    user_id = str(user_payload.get("id") or "").strip()
    if not user_id:
        raise click.ClickException("Dokploy user.session returned no user id.")
    return user_id


def latest_deployment_for_schedule(*, host: str, token: str, schedule_id: str) -> JsonObject | None:
    payload = dokploy_request(
        host=host,
        token=token,
        path="/api/deployment.allByType",
        query={"id": schedule_id, "type": "schedule"},
    )
    return _latest_deployment_from_list(extract_deployments(payload))


def wait_for_dokploy_schedule_deployment(
    *,
    host: str,
    token: str,
    schedule_id: str,
    before_key: str,
    timeout_seconds: int,
) -> str:
    return _wait_for_deployment_status(
        fetch_latest_deployment=lambda: latest_deployment_for_schedule(
            host=host,
            token=token,
            schedule_id=schedule_id,
        ),
        before_key=before_key,
        timeout_seconds=timeout_seconds,
        failure_message_prefix="Dokploy schedule deployment failed",
    )


def list_dokploy_schedules(
    *,
    host: str,
    token: str,
    target_id: str,
    schedule_type: str,
) -> tuple[JsonObject, ...]:
    payload = dokploy_request(
        host=host,
        token=token,
        path="/api/schedule.list",
        query={"id": target_id, "scheduleType": schedule_type},
    )
    return tuple(extract_schedules(payload))


def find_matching_dokploy_schedule(
    *,
    host: str,
    token: str,
    target_id: str,
    schedule_type: str,
    schedule_name: str,
    app_name: str,
) -> JsonObject | None:
    for schedule in list_dokploy_schedules(
        host=host,
        token=token,
        target_id=target_id,
        schedule_type=schedule_type,
    ):
        if str(schedule.get("name") or "").strip() != schedule_name:
            continue
        if str(schedule.get("appName") or "").strip() != app_name:
            continue
        return schedule
    return None


def schedule_key(schedule: JsonObject) -> str:
    for key_name in ("scheduleId", "schedule_id", "id", "uuid"):
        value = schedule.get(key_name)
        if value:
            return str(value)
    return ""


def upsert_dokploy_schedule(
    *,
    host: str,
    token: str,
    target_id: str,
    schedule_type: str,
    schedule_name: str,
    app_name: str,
    schedule_payload: JsonObject,
) -> JsonObject:
    existing_schedule = find_matching_dokploy_schedule(
        host=host,
        token=token,
        target_id=target_id,
        schedule_type=schedule_type,
        schedule_name=schedule_name,
        app_name=app_name,
    )
    if existing_schedule is not None:
        updated_payload = dict(schedule_payload)
        updated_payload["scheduleId"] = schedule_key(existing_schedule)
        dokploy_request(
            host=host,
            token=token,
            path="/api/schedule.update",
            method="POST",
            payload=updated_payload,
        )
    else:
        dokploy_request(
            host=host,
            token=token,
            path="/api/schedule.create",
            method="POST",
            payload=schedule_payload,
        )

    resolved_schedule = find_matching_dokploy_schedule(
        host=host,
        token=token,
        target_id=target_id,
        schedule_type=schedule_type,
        schedule_name=schedule_name,
        app_name=app_name,
    )
    if resolved_schedule is None:
        raise click.ClickException(
            f"Dokploy schedule {schedule_name!r} for {schedule_type} target {target_id!r} could not be resolved after upsert."
        )
    return resolved_schedule


def run_compose_post_deploy_update(
    *,
    host: str,
    token: str,
    target_definition: DokployTargetDefinition,
    env_file: Path | None,
    workflow_environment_overrides: Mapping[str, str] | None = None,
    required_workflow_environment_keys: tuple[str, ...] = (),
    protected_shopify_store_keys: tuple[str, ...] = (),
) -> None:
    compose_id = target_definition.target_id.strip()
    compose_name = (
        target_definition.target_name.strip()
        or f"{target_definition.context}-{target_definition.instance}"
    )
    if not compose_id:
        raise click.ClickException(
            f"Dokploy compose target {target_definition.context}/{target_definition.instance} requires target_id for post-deploy update."
        )
    target_payload = fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type="compose",
        target_id=compose_id,
    )
    current_env_map = parse_dokploy_env_text(str(target_payload.get("env") or ""))
    desired_env_map = _apply_post_deploy_env_file_overrides(
        current_env_map=current_env_map,
        env_file=env_file,
    )
    schedule_timeout_seconds = (
        target_definition.deploy_timeout_seconds or DEFAULT_DOKPLOY_DEPLOY_TIMEOUT_SECONDS
    )
    if desired_env_map != current_env_map:
        update_dokploy_target_env(
            host=host,
            token=token,
            target_type="compose",
            target_id=compose_id,
            target_payload=target_payload,
            env_text=serialize_dokploy_env_text(desired_env_map),
        )
        latest_compose_deployment = latest_deployment_for_target(
            host=host,
            token=token,
            target_type="compose",
            target_id=compose_id,
        )
        trigger_deployment(
            host=host,
            token=token,
            target_type="compose",
            target_id=compose_id,
            no_cache=False,
        )
        wait_for_target_deployment(
            host=host,
            token=token,
            target_type="compose",
            target_id=compose_id,
            before_key=deployment_key(latest_compose_deployment),
            timeout_seconds=schedule_timeout_seconds,
        )

    database_name = desired_env_map.get("ODOO_DB_NAME", "").strip()
    if not database_name:
        raise click.ClickException(
            "Compose post-deploy update requires ODOO_DB_NAME in the live target environment or explicit env file."
        )
    filestore_path = (
        desired_env_map.get("ODOO_FILESTORE_PATH") or "/volumes/data/filestore"
    ).strip() or "/volumes/data/filestore"
    data_workflow_lock_path = (
        desired_env_map.get("ODOO_DATA_WORKFLOW_LOCK_FILE") or DEFAULT_DATA_WORKFLOW_LOCK_PATH
    ).strip() or DEFAULT_DATA_WORKFLOW_LOCK_PATH
    schedule_type, schedule_lookup_id, compose_app_name, schedule_server_id = (
        _resolve_dokploy_schedule_runtime(
            host=host,
            token=token,
            compose_id=compose_id,
            compose_name=compose_name,
            target_payload=target_payload,
        )
    )
    schedule_name = DOKPLOY_DATA_WORKFLOW_SCHEDULE_NAME
    schedule_app_name = _build_dokploy_data_workflow_schedule_app_name(
        context_name=target_definition.context,
        instance_name=target_definition.instance,
    )
    existing_schedule = find_matching_dokploy_schedule(
        host=host,
        token=token,
        target_id=schedule_lookup_id,
        schedule_type=schedule_type,
        schedule_name=schedule_name,
        app_name=schedule_app_name,
    )
    if _has_running_schedule_deployment(existing_schedule):
        raise click.ClickException(
            "Dokploy-managed post-deploy update already has a running schedule deployment for "
            f"{target_definition.context}/{target_definition.instance}."
        )
    schedule_script = _build_dokploy_data_workflow_script(
        compose_app_name=compose_app_name,
        database_name=database_name,
        filestore_path=filestore_path,
        clear_stale_lock=_should_clear_stale_data_workflow_lock(existing_schedule),
        data_workflow_lock_path=data_workflow_lock_path,
        workflow_environment_overrides=workflow_environment_overrides or {},
        required_workflow_environment_keys=required_workflow_environment_keys,
        protected_shopify_store_keys=protected_shopify_store_keys,
    )
    schedule_payload: JsonObject = {
        "name": schedule_name,
        "cronExpression": DOKPLOY_MANUAL_ONLY_CRON_EXPRESSION,
        "appName": schedule_app_name,
        "shellType": "bash",
        "scheduleType": schedule_type,
        "command": "control-plane post-deploy update",
        "script": schedule_script,
        "serverId": schedule_server_id,
        "userId": schedule_lookup_id if schedule_type == "dokploy-server" else None,
        "enabled": False,
        "timezone": "UTC",
    }
    schedule = upsert_dokploy_schedule(
        host=host,
        token=token,
        target_id=schedule_lookup_id,
        schedule_type=schedule_type,
        schedule_name=schedule_name,
        app_name=schedule_app_name,
        schedule_payload=schedule_payload,
    )
    schedule_id = schedule_key(schedule)
    if not schedule_id:
        raise click.ClickException(
            f"Dokploy schedule {schedule_name!r} for {target_definition.context}/{target_definition.instance} did not expose a schedule id."
        )
    latest_schedule_deployment = latest_deployment_for_schedule(
        host=host,
        token=token,
        schedule_id=schedule_id,
    )
    dokploy_request(
        host=host,
        token=token,
        path="/api/schedule.runManually",
        method="POST",
        payload={"scheduleId": schedule_id},
        timeout_seconds=schedule_timeout_seconds,
    )
    wait_for_dokploy_schedule_deployment(
        host=host,
        token=token,
        schedule_id=schedule_id,
        before_key=deployment_key(latest_schedule_deployment),
        timeout_seconds=schedule_timeout_seconds,
    )


def run_compose_odoo_backup_gate(
    *,
    host: str,
    token: str,
    target_definition: DokployTargetDefinition,
    backup_record_id: str,
    database_name: str,
    filestore_path: str,
    backup_root: str,
    timeout_seconds: int | None = None,
) -> None:
    compose_id = target_definition.target_id.strip()
    compose_name = (
        target_definition.target_name.strip()
        or f"{target_definition.context}-{target_definition.instance}"
    )
    if not compose_id:
        raise click.ClickException(
            f"Dokploy compose target {target_definition.context}/{target_definition.instance} requires target_id for Odoo backup gate."
        )
    target_payload = fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type="compose",
        target_id=compose_id,
    )
    normalized_database_name = database_name.strip()
    if not normalized_database_name:
        raise click.ClickException(
            "Odoo backup gate requires a non-empty database_name resolved from Launchplane runtime records."
        )
    normalized_filestore_path = filestore_path.strip() or "/volumes/data/filestore"
    normalized_backup_root = backup_root.strip() or DEFAULT_ODOO_BACKUP_ROOT
    schedule_timeout_seconds = (
        timeout_seconds
        or target_definition.deploy_timeout_seconds
        or DEFAULT_DOKPLOY_DEPLOY_TIMEOUT_SECONDS
    )
    schedule_type, schedule_lookup_id, compose_app_name, schedule_server_id = (
        _resolve_dokploy_schedule_runtime(
            host=host,
            token=token,
            compose_id=compose_id,
            compose_name=compose_name,
            target_payload=target_payload,
        )
    )
    schedule_app_name = (
        f"platform-{target_definition.context}-{target_definition.instance}-odoo-backup-gate"
    )
    schedule_script = _build_dokploy_odoo_backup_gate_script(
        compose_app_name=compose_app_name,
        database_name=normalized_database_name,
        filestore_path=normalized_filestore_path,
        backup_root=normalized_backup_root,
        backup_record_id=backup_record_id,
    )
    schedule_payload: JsonObject = {
        "name": DOKPLOY_ODOO_BACKUP_GATE_SCHEDULE_NAME,
        "cronExpression": DOKPLOY_MANUAL_ONLY_CRON_EXPRESSION,
        "appName": schedule_app_name,
        "shellType": "bash",
        "scheduleType": schedule_type,
        "command": "control-plane odoo backup gate",
        "script": schedule_script,
        "serverId": schedule_server_id,
        "userId": schedule_lookup_id if schedule_type == "dokploy-server" else None,
        "enabled": False,
        "timezone": "UTC",
    }
    schedule = upsert_dokploy_schedule(
        host=host,
        token=token,
        target_id=schedule_lookup_id,
        schedule_type=schedule_type,
        schedule_name=DOKPLOY_ODOO_BACKUP_GATE_SCHEDULE_NAME,
        app_name=schedule_app_name,
        schedule_payload=schedule_payload,
    )
    schedule_id = schedule_key(schedule)
    if not schedule_id:
        raise click.ClickException(
            f"Dokploy Odoo backup gate schedule for {target_definition.context}/{target_definition.instance} did not expose a schedule id."
        )
    latest_schedule_deployment = latest_deployment_for_schedule(
        host=host,
        token=token,
        schedule_id=schedule_id,
    )
    dokploy_request(
        host=host,
        token=token,
        path="/api/schedule.runManually",
        method="POST",
        payload={"scheduleId": schedule_id},
        timeout_seconds=schedule_timeout_seconds,
    )
    wait_for_dokploy_schedule_deployment(
        host=host,
        token=token,
        schedule_id=schedule_id,
        before_key=deployment_key(latest_schedule_deployment),
        timeout_seconds=schedule_timeout_seconds,
    )


def deployment_key(deployment: JsonObject | None) -> str:
    if deployment is None:
        return ""
    for key_name in ("deploymentId", "deployment_id", "id", "uuid"):
        value = deployment.get(key_name)
        if value:
            return str(value)
    return ""


def dokploy_request(
    *,
    host: str,
    token: str,
    path: str,
    method: str = "GET",
    payload: JsonObject | None = None,
    query: dict[str, str | int] | None = None,
    timeout_seconds: int | float = 60,
) -> JsonValue:
    normalized_host = host.rstrip("/")
    normalized_path = path if path.startswith("/") else f"/{path}"
    request_url = f"{normalized_host}{normalized_path}"
    if query:
        request_url = f"{request_url}?{urlencode(query)}"
    request_headers = {"x-api-key": token}
    request_body: bytes | None = None
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
        request_body = json.dumps(payload).encode()
    request = Request(request_url, data=request_body, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            raw_payload = response.read()
    except HTTPError as error:
        error_body = error.read().decode(errors="replace").strip()
        raise click.ClickException(
            f"Dokploy API {method} {normalized_path} failed ({error.code}): {error_body}"
        ) from error
    except URLError as error:
        raise click.ClickException(
            f"Dokploy API {method} {normalized_path} request failed: {error.reason}"
        ) from error

    if not raw_payload:
        return {}
    try:
        return json.loads(raw_payload)
    except json.JSONDecodeError:
        return {"raw": raw_payload.decode("utf-8", errors="replace")}


def as_json_object(value: JsonValue) -> JsonObject | None:
    if not isinstance(value, dict):
        return None
    if not all(isinstance(key_name, str) for key_name in value):
        return None
    return value


def extract_schedules(raw_payload: JsonValue) -> list[JsonObject]:
    if isinstance(raw_payload, list):
        return _collect_object_items(raw_payload)
    if isinstance(raw_payload, dict):
        for key_name in ("data", "schedules", "items", "result"):
            nested_items = raw_payload.get(key_name)
            if isinstance(nested_items, list):
                return _collect_object_items(nested_items)
    return []


def extract_deployments(raw_payload: JsonValue) -> list[JsonObject]:
    if isinstance(raw_payload, list):
        return _collect_object_items(raw_payload)
    if isinstance(raw_payload, dict):
        for key_name in ("data", "deployments", "items", "result"):
            nested_items = raw_payload.get(key_name)
            if isinstance(nested_items, list):
                return _collect_object_items(nested_items)
    return []


def _collect_object_items(raw_items: list[JsonValue]) -> list[JsonObject]:
    object_items: list[JsonObject] = []
    for raw_item in raw_items:
        item_as_object = as_json_object(raw_item)
        if item_as_object is not None:
            object_items.append(item_as_object)
    return object_items


def _wait_for_deployment_status(
    *,
    fetch_latest_deployment: Callable[[], JsonObject | None],
    before_key: str,
    timeout_seconds: int,
    failure_message_prefix: str,
) -> str:
    success_statuses = {"success", "succeeded", "done", "completed", "healthy", "finished"}
    failure_statuses = {
        "failed",
        "error",
        "canceled",
        "cancelled",
        "killed",
        "unhealthy",
        "timeout",
    }

    start_time = time.monotonic()
    while time.monotonic() - start_time <= timeout_seconds:
        latest_deployment = fetch_latest_deployment()
        if latest_deployment is None:
            time.sleep(3)
            continue

        latest_key = deployment_key(latest_deployment)
        latest_status = _deployment_status(latest_deployment)
        if latest_key and latest_key != before_key:
            if latest_status in success_statuses:
                return f"deployment={latest_key} status={latest_status}"
            if latest_status in failure_statuses:
                raise click.ClickException(
                    f"{failure_message_prefix}: deployment={latest_key} status={latest_status}"
                )
            if not latest_status:
                return f"deployment={latest_key} status=unknown"
        time.sleep(3)

    raise click.ClickException("Timed out waiting for Dokploy deployment status.")


def _latest_deployment_from_list(deployments: list[JsonObject]) -> JsonObject | None:
    if not deployments:
        return None
    return max(deployments, key=_deployment_sort_key)


def _deployment_sort_key(deployment: JsonObject) -> str:
    for key_name in ("createdAt", "created_at", "updatedAt", "updated_at"):
        value = deployment.get(key_name)
        if value:
            return str(value)
    return deployment_key(deployment)


def _deployment_status(deployment: JsonObject) -> str:
    for key_name in ("status", "state", "deploymentStatus"):
        value = deployment.get(key_name)
        if value:
            return str(value).strip().lower()
    return ""


def _build_dokploy_data_workflow_schedule_app_name(*, context_name: str, instance_name: str) -> str:
    return f"platform-{context_name}-{instance_name}-data-workflow"


def _resolve_dokploy_schedule_runtime(
    *,
    host: str,
    token: str,
    compose_id: str,
    compose_name: str,
    target_payload: JsonObject,
) -> tuple[str, str, str, str | None]:
    compose_app_name = str(target_payload.get("appName") or "").strip()
    if not compose_app_name:
        raise click.ClickException(
            f"Dokploy compose {compose_name!r} ({compose_id}) has no appName in API response."
        )
    compose_server_id = str(target_payload.get("serverId") or "").strip()
    if compose_server_id:
        return "server", compose_server_id, compose_app_name, compose_server_id
    user_id = resolve_dokploy_user_id(host=host, token=token)
    return "dokploy-server", user_id, compose_app_name, None


def _build_dokploy_data_workflow_script(
    *,
    compose_app_name: str,
    database_name: str,
    filestore_path: str,
    clear_stale_lock: bool,
    data_workflow_lock_path: str,
    workflow_environment_overrides: Mapping[str, str] | None = None,
    required_workflow_environment_keys: tuple[str, ...] = (),
    protected_shopify_store_keys: tuple[str, ...] = (),
) -> str:
    normalized_filestore_path = filestore_path.strip() or "/volumes/data/filestore"
    quoted_compose_app_name = shlex.quote(compose_app_name)
    quoted_database_name = shlex.quote(database_name)
    quoted_filestore_path = shlex.quote(normalized_filestore_path)
    quoted_lock_path = shlex.quote(data_workflow_lock_path)
    workflow_environment_lines = _render_docker_exec_environment_lines(
        workflow_environment_overrides or {}
    )
    required_workflow_environment_lines = _render_required_environment_key_lines(
        required_workflow_environment_keys
    )
    protected_shopify_store_key_lines = _render_bash_array_assignment_lines(
        "protected_shopify_store_keys",
        protected_shopify_store_keys,
    )
    return f"""#!/usr/bin/env bash
set -euo pipefail

compose_project={quoted_compose_app_name}
database_name={quoted_database_name}
filestore_root={quoted_filestore_path}
workflow_ssh_dir=/tmp/platform-data-workflow-ssh
workflow_arguments=(--update-only)
workflow_environment=()
{workflow_environment_lines}
required_workflow_environment_keys=()
{required_workflow_environment_lines}
protected_shopify_store_keys=()
{protected_shopify_store_key_lines}
clear_stale_lock={"1" if clear_stale_lock else "0"}
data_workflow_lock_path={quoted_lock_path}
restart_web_on_success=0
web_was_running=0

resolve_container_id() {{
    local service_name="$1"
    local container_id
    container_id=$(docker ps -aq \
        --filter "label=com.docker.compose.project=${{compose_project}}" \
        --filter "label=com.docker.compose.service=${{service_name}}" | head -n 1)
    if [ -z "${{container_id}}" ]; then
        echo "Missing container for service '${{service_name}}' in project '${{compose_project}}'." >&2
        exit 1
    fi
    printf '%s' "${{container_id}}"
}}

ensure_running() {{
    local container_id="$1"
    local service_name="$2"
    local current_status
    current_status=$(docker inspect -f '{{{{.State.Status}}}}' "${{container_id}}")
    if [ "${{current_status}}" != "running" ]; then
        echo "Starting ${{service_name}} container ${{container_id}}"
        docker start "${{container_id}}" >/dev/null
    fi
}}

start_web_container() {{
    if [ "${{web_was_running}}" != "1" ]; then
        return
    fi
    local current_status
    current_status=$(docker inspect -f '{{{{.State.Status}}}}' "${{web_container_id}}" 2>/dev/null || true)
    if [ "${{current_status}}" != "running" ]; then
        echo "Starting web container ${{web_container_id}}"
        docker start "${{web_container_id}}" >/dev/null || true
    fi
}}

exit_trap() {{
    local exit_status="$?"
    if [ "${{exit_status}}" -eq 0 ] && [ "${{restart_web_on_success}}" = "1" ]; then
        start_web_container
    fi
    exit "${{exit_status}}"
}}

database_container_id=$(resolve_container_id "database")
script_runner_container_id=$(resolve_container_id "script-runner")
web_container_id=$(resolve_container_id "web")

ensure_running "${{database_container_id}}" "database"
ensure_running "${{script_runner_container_id}}" "script-runner"
workflow_uid=$(docker exec "${{script_runner_container_id}}" id -u)
workflow_gid=$(docker exec "${{script_runner_container_id}}" id -g)

if [ "${{clear_stale_lock}}" = "1" ]; then
    echo "Clearing stale data workflow lock ${{data_workflow_lock_path}}"
    docker exec -u root "${{script_runner_container_id}}" rm -f "${{data_workflow_lock_path}}"
fi

trap exit_trap EXIT

web_status=$(docker inspect -f '{{{{.State.Status}}}}' "${{web_container_id}}")
if [ "${{web_status}}" = "running" ]; then
    web_was_running=1
    echo "Stopping web container ${{web_container_id}}"
    docker stop "${{web_container_id}}" >/dev/null
fi

if [ "${{#required_workflow_environment_keys[@]}}" -gt 0 ]; then
    docker exec "${{script_runner_container_id}}" /bin/bash -lc '
        set -euo pipefail
        for key_name in "$@"; do
            if [ -z "${{!key_name+x}}" ]; then
                echo "Missing required Odoo override environment key: $key_name" >&2
                exit 1
            fi
        done
    ' _ "${{required_workflow_environment_keys[@]}}"
fi

echo "Normalizing filestore ownership for ${{database_name}}"
workflow_identity_key=$(docker exec -u root \
    -e ODOO_DATABASE_NAME="${{database_name}}" \
    -e ODOO_FILESTORE_ROOT="${{filestore_root}}" \
    -e DATA_WORKFLOW_SSH_DIR="${{DATA_WORKFLOW_SSH_DIR:-/root/.ssh}}" \
    -e DATA_WORKFLOW_SSH_KEY="${{DATA_WORKFLOW_SSH_KEY:-}}" \
    -e WORKFLOW_UID="${{workflow_uid}}" \
    -e WORKFLOW_GID="${{workflow_gid}}" \
    -e WORKFLOW_SSH_DIR="${{workflow_ssh_dir}}" \
    "${{script_runner_container_id}}" \
    /bin/bash -lc '
        set -euo pipefail
        target_owner=$(stat -c "%u:%g" /volumes/data)
        filestore_database_path="$ODOO_FILESTORE_ROOT"
        if [ "$(basename "$filestore_database_path")" != "$ODOO_DATABASE_NAME" ]; then
            filestore_database_path="$filestore_database_path/$ODOO_DATABASE_NAME"
        fi
        mkdir -p "$ODOO_FILESTORE_ROOT" "$filestore_database_path"
        chown -R "$target_owner" "$filestore_database_path"
        chmod -R ug+rwX "$filestore_database_path"

        rm -rf "$WORKFLOW_SSH_DIR"
        install -d -m 700 -o "$WORKFLOW_UID" -g "$WORKFLOW_GID" "$WORKFLOW_SSH_DIR"

        if [ -f "$DATA_WORKFLOW_SSH_DIR/known_hosts" ]; then
            install -m 600 -o "$WORKFLOW_UID" -g "$WORKFLOW_GID" \
                "$DATA_WORKFLOW_SSH_DIR/known_hosts" "$WORKFLOW_SSH_DIR/known_hosts"
        fi

        source_key_path="$DATA_WORKFLOW_SSH_KEY"
        if [ -z "$source_key_path" ]; then
            for candidate_key in id_ed25519 id_ecdsa id_rsa id_dsa; do
                if [ -f "$DATA_WORKFLOW_SSH_DIR/$candidate_key" ]; then
                    source_key_path="$DATA_WORKFLOW_SSH_DIR/$candidate_key"
                    break
                fi
            done
        fi
        workflow_identity_key=""
        if [ -n "$source_key_path" ] && [ -f "$source_key_path" ]; then
            workflow_identity_key="$WORKFLOW_SSH_DIR/$(basename "$source_key_path")"
            install -m 600 -o "$WORKFLOW_UID" -g "$WORKFLOW_GID" \
                "$source_key_path" "$workflow_identity_key"
        fi
        printf "%s" "$workflow_identity_key"
    ')

echo "Running post-deploy update in container ${{script_runner_container_id}}"
docker exec \
    -e DATA_WORKFLOW_SSH_DIR="${{workflow_ssh_dir}}" \
    -e DATA_WORKFLOW_SSH_KEY="$workflow_identity_key" \
    "${{workflow_environment[@]}}" \
    "${{script_runner_container_id}}" \
    python3 -u /volumes/scripts/run_odoo_data_workflows.py "${{workflow_arguments[@]}}"

if [ "${{#protected_shopify_store_keys[@]}}" -gt 0 ]; then
    echo "Checking protected Shopify store keys for ${{database_name}}"
    docker exec "${{script_runner_container_id}}" python3 - "${{database_name}}" "${{protected_shopify_store_keys[@]}}" <<'PY'
import os
import sys

import psycopg2

database_name = sys.argv[1]
protected_store_keys = {{value.strip().lower() for value in sys.argv[2:] if value.strip()}}

connection = psycopg2.connect(
    host=(os.environ.get("ODOO_DB_HOST") or "database").strip(),
    port=(os.environ.get("ODOO_DB_PORT") or "5432").strip(),
    user=(os.environ.get("ODOO_DB_USER") or "odoo").strip(),
    password=os.environ.get("ODOO_DB_PASSWORD") or "",
    dbname=database_name,
)
try:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT value FROM ir_config_parameter WHERE key = %s LIMIT 1",
            ("shopify.shop_url_key",),
        )
        row = cursor.fetchone()
finally:
    connection.close()

current_store_key = str(row[0]).strip() if row and row[0] is not None else ""
normalized_store_key = current_store_key.lower()
if normalized_store_key in protected_store_keys:
    protected_list = ", ".join(sorted(protected_store_keys))
    raise SystemExit(
        "Protected Shopify store key is not allowed on this Dokploy lane. "
        f"db={{database_name}} current={{current_store_key or '<empty>'}} protected={{protected_list}}"
    )

print(f"shopify_store_key_guard_pass db={{database_name}} value={{current_store_key or '<empty>'}}")
PY
fi

restart_web_on_success=1
start_web_container
restart_web_on_success=0
trap - EXIT
"""


def _build_dokploy_odoo_backup_gate_script(
    *,
    compose_app_name: str,
    database_name: str,
    filestore_path: str,
    backup_root: str,
    backup_record_id: str,
) -> str:
    normalized_filestore_path = filestore_path.strip() or "/volumes/data/filestore"
    normalized_backup_root = backup_root.strip() or DEFAULT_ODOO_BACKUP_ROOT
    quoted_compose_app_name = shlex.quote(compose_app_name)
    quoted_database_name = shlex.quote(database_name)
    quoted_filestore_path = shlex.quote(normalized_filestore_path)
    quoted_backup_root = shlex.quote(normalized_backup_root)
    quoted_backup_record_id = shlex.quote(backup_record_id)
    return f"""#!/usr/bin/env bash
set -euo pipefail

compose_project={quoted_compose_app_name}
database_name={quoted_database_name}
filestore_root={quoted_filestore_path}
backup_root={quoted_backup_root}
backup_record_id={quoted_backup_record_id}

resolve_container_id() {{
    local service_name="$1"
    local container_id
    container_id=$(docker ps -aq \
        --filter "label=com.docker.compose.project=${{compose_project}}" \
        --filter "label=com.docker.compose.service=${{service_name}}" | head -n 1)
    if [ -z "${{container_id}}" ]; then
        echo "Missing container for service '${{service_name}}' in project '${{compose_project}}'." >&2
        exit 1
    fi
    printf '%s' "${{container_id}}"
}}

ensure_running() {{
    local container_id="$1"
    local service_name="$2"
    local current_status
    current_status=$(docker inspect -f '{{{{.State.Status}}}}' "${{container_id}}")
    if [ "${{current_status}}" != "running" ]; then
        echo "Starting ${{service_name}} container ${{container_id}}"
        docker start "${{container_id}}" >/dev/null
    fi
}}

database_container_id=$(resolve_container_id "database")
script_runner_container_id=$(resolve_container_id "script-runner")
web_container_id=$(resolve_container_id "web")
ensure_running "${{database_container_id}}" "database"
ensure_running "${{script_runner_container_id}}" "script-runner"

web_was_running=0
if [ "$(docker inspect -f '{{{{.State.Status}}}}' "${{web_container_id}}")" = "running" ]; then
    web_was_running=1
    echo "Stopping web container ${{web_container_id}} for backup consistency"
    docker stop "${{web_container_id}}" >/dev/null
fi

restart_web_on_exit() {{
    if [ "${{web_was_running}}" = "1" ]; then
        echo "Starting web container ${{web_container_id}}"
        docker start "${{web_container_id}}" >/dev/null
    fi
}}
trap restart_web_on_exit EXIT

backup_dir="${{backup_root}}/${{database_name}}/${{backup_record_id}}"
database_dump_path="${{backup_dir}}/${{database_name}}.dump"
filestore_archive_path="${{backup_dir}}/${{database_name}}-filestore.tar.gz"
manifest_path="${{backup_dir}}/manifest.json"

echo "Creating Odoo backup gate directory ${{backup_dir}}"
docker exec -u root \
    -e BACKUP_DIR="${{backup_dir}}" \
    "${{script_runner_container_id}}" \
    /bin/bash -lc 'install -d -m 700 "$BACKUP_DIR"'

echo "Capturing database dump for ${{database_name}}"
docker exec \
    -e ODOO_DATABASE_NAME="${{database_name}}" \
    -e DATABASE_DUMP_PATH="${{database_dump_path}}" \
    "${{script_runner_container_id}}" \
    /bin/bash -lc '
        set -euo pipefail
        export PGPASSWORD="${{ODOO_DB_PASSWORD:-}}"
        pg_dump \
            --host "${{ODOO_DB_HOST:-database}}" \
            --port "${{ODOO_DB_PORT:-5432}}" \
            --username "${{ODOO_DB_USER:-odoo}}" \
            --format custom \
            --file "$DATABASE_DUMP_PATH" \
            "$ODOO_DATABASE_NAME"
        test -s "$DATABASE_DUMP_PATH"
    '

echo "Capturing filestore archive for ${{database_name}}"
docker exec \
    -e ODOO_DATABASE_NAME="${{database_name}}" \
    -e ODOO_FILESTORE_ROOT="${{filestore_root}}" \
    -e FILESTORE_ARCHIVE_PATH="${{filestore_archive_path}}" \
    "${{script_runner_container_id}}" \
    /bin/bash -lc '
        set -euo pipefail
        filestore_database_path="$ODOO_FILESTORE_ROOT"
        if [ "$(basename "$filestore_database_path")" != "$ODOO_DATABASE_NAME" ]; then
            filestore_database_path="$filestore_database_path/$ODOO_DATABASE_NAME"
        fi
        if [ ! -d "$filestore_database_path" ]; then
            echo "Missing filestore path: $filestore_database_path" >&2
            exit 1
        fi
        tar -C "$(dirname "$filestore_database_path")" -czf "$FILESTORE_ARCHIVE_PATH" "$(basename "$filestore_database_path")"
        test -s "$FILESTORE_ARCHIVE_PATH"
    '

database_dump_size=$(docker exec "${{script_runner_container_id}}" stat -c %s "${{database_dump_path}}")
filestore_archive_size=$(docker exec "${{script_runner_container_id}}" stat -c %s "${{filestore_archive_path}}")

docker exec \
    -e MANIFEST_PATH="${{manifest_path}}" \
    -e BACKUP_RECORD_ID="${{backup_record_id}}" \
    -e DATABASE_NAME="${{database_name}}" \
    -e BACKUP_DIR="${{backup_dir}}" \
    -e DATABASE_DUMP_PATH="${{database_dump_path}}" \
    -e FILESTORE_ARCHIVE_PATH="${{filestore_archive_path}}" \
    -e DATABASE_DUMP_SIZE="${{database_dump_size}}" \
    -e FILESTORE_ARCHIVE_SIZE="${{filestore_archive_size}}" \
    "${{script_runner_container_id}}" \
    python3 - <<'PY'
import json
import os
from datetime import datetime, timezone

payload = {{
    "backup_record_id": os.environ["BACKUP_RECORD_ID"],
    "database_name": os.environ["DATABASE_NAME"],
    "backup_dir": os.environ["BACKUP_DIR"],
    "database_dump_path": os.environ["DATABASE_DUMP_PATH"],
    "filestore_archive_path": os.environ["FILESTORE_ARCHIVE_PATH"],
    "database_dump_size": os.environ["DATABASE_DUMP_SIZE"],
    "filestore_archive_size": os.environ["FILESTORE_ARCHIVE_SIZE"],
    "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
}}
with open(os.environ["MANIFEST_PATH"], "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY

echo "Odoo backup gate complete: ${{backup_dir}}"
restart_web_on_exit
trap - EXIT
"""


def _render_docker_exec_environment_lines(environment_values: Mapping[str, str]) -> str:
    lines: list[str] = []
    for key_name in sorted(environment_values):
        normalized_key = key_name.strip()
        if not normalized_key:
            raise click.ClickException(
                "Post-deploy workflow environment override keys must be non-empty."
            )
        if not normalized_key.replace("_", "A").isalnum() or normalized_key[0].isdigit():
            raise click.ClickException(
                f"Invalid post-deploy workflow environment override key: {normalized_key!r}."
            )
        value = str(environment_values[key_name])
        lines.append(f"workflow_environment+=(-e {shlex.quote(f'{normalized_key}={value}')})")
    return "\n".join(lines)


def _render_required_environment_key_lines(environment_keys: tuple[str, ...]) -> str:
    lines: list[str] = []
    for key_name in sorted(set(environment_keys)):
        normalized_key = key_name.strip()
        if not normalized_key:
            raise click.ClickException(
                "Required post-deploy workflow environment keys must be non-empty."
            )
        if not normalized_key.replace("_", "A").isalnum() or normalized_key[0].isdigit():
            raise click.ClickException(
                f"Invalid required post-deploy workflow environment key: {normalized_key!r}."
            )
        lines.append(f"required_workflow_environment_keys+=({shlex.quote(normalized_key)})")
    return "\n".join(lines)


def _render_bash_array_assignment_lines(array_name: str, values: tuple[str, ...]) -> str:
    lines: list[str] = []
    for raw_value in values:
        normalized_value = raw_value.strip()
        if not normalized_value:
            raise click.ClickException(f"{array_name} values must be non-empty.")
        lines.append(f"{array_name}+=({shlex.quote(normalized_value)})")
    return "\n".join(lines)


def _schedule_deployments(schedule: JsonObject | None) -> tuple[JsonObject, ...]:
    if not isinstance(schedule, dict):
        return ()
    raw_deployments = schedule.get("deployments")
    if not isinstance(raw_deployments, list):
        return ()
    return tuple(_collect_object_items(raw_deployments))


def _has_running_schedule_deployment(schedule: JsonObject | None) -> bool:
    return any(
        _deployment_status(deployment) in DOKPLOY_RUNNING_DEPLOYMENT_STATUSES
        for deployment in _schedule_deployments(schedule)
    )


def _should_clear_stale_data_workflow_lock(schedule: JsonObject | None) -> bool:
    deployments = _schedule_deployments(schedule)
    if not deployments or _has_running_schedule_deployment(schedule):
        return False
    for deployment in deployments:
        deployment_status_value = _deployment_status(deployment)
        if deployment_status_value in DOKPLOY_CANCELLED_DEPLOYMENT_STATUSES:
            return True
        if deployment_status_value in DOKPLOY_SUCCESS_DEPLOYMENT_STATUSES:
            return False
    return False


def _apply_post_deploy_env_file_overrides(
    *, current_env_map: dict[str, str], env_file: Path | None
) -> dict[str, str]:
    if env_file is None:
        return dict(current_env_map)
    desired_env_map = dict(current_env_map)
    unsupported_keys: list[str] = []
    for env_key, env_value in _parse_env_file(env_file).items():
        if env_key in POST_DEPLOY_UPDATE_IGNORED_ENV_KEYS:
            continue
        if env_key not in POST_DEPLOY_UPDATE_ALLOWED_ENV_KEYS:
            unsupported_keys.append(env_key)
            continue
        desired_env_map[env_key] = env_value
    if unsupported_keys:
        allowed_key_list = ", ".join(sorted(POST_DEPLOY_UPDATE_ALLOWED_ENV_KEYS))
        unsupported_key_list = ", ".join(sorted(unsupported_keys))
        raise click.ClickException(
            "Compose post-deploy env overlay only supports "
            f"{allowed_key_list}. Unsupported keys: {unsupported_key_list}."
        )
    return desired_env_map


def _parse_env_file(env_file: Path) -> dict[str, str]:
    env_values: dict[str, str] = {}
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        stripped_line = raw_line.strip()
        if not stripped_line or stripped_line.startswith("#"):
            continue
        if stripped_line.startswith("export "):
            stripped_line = stripped_line[7:].strip()
        if "=" not in stripped_line:
            continue
        key_name, value = stripped_line.split("=", 1)
        env_values[key_name.strip()] = value
    return env_values


def _normalize_dokploy_source_payload(raw_value: object) -> object:
    if not isinstance(raw_value, Mapping):
        return raw_value

    normalized_payload = dict(raw_value)
    allowed_top_level_keys = {"defaults", "profiles", "projects", "schema_version", "targets"}
    unknown_keys = sorted(
        key_name for key_name in normalized_payload if key_name not in allowed_top_level_keys
    )
    if unknown_keys:
        unknown_key_list = ", ".join(unknown_keys)
        raise ValueError(f"Unknown top-level dokploy keys: {unknown_key_list}")

    raw_targets = normalized_payload.get("targets")
    if not isinstance(raw_targets, list):
        return raw_value

    defaults = _expect_mapping(normalized_payload.get("defaults"), label="defaults")
    raw_profiles = _expect_mapping(normalized_payload.get("profiles"), label="profiles")
    raw_projects = _expect_mapping(normalized_payload.get("projects"), label="projects")

    resolved_profiles: dict[str, dict[str, object]] = {}
    targets: list[object] = []
    for target_index, raw_target in enumerate(raw_targets, start=1):
        if not isinstance(raw_target, Mapping):
            targets.append(raw_target)
            continue

        target_payload = dict(raw_target)
        profile_name = str(target_payload.pop("profile", "") or "").strip()
        merged_target = dict(defaults)
        if profile_name:
            merged_target = _merge_dokploy_settings(
                merged_target,
                _resolve_dokploy_profile(
                    profile_name,
                    raw_profiles=raw_profiles,
                    raw_projects=raw_projects,
                    resolved_profiles=resolved_profiles,
                    active_profiles=(),
                ),
            )
        merged_target = _merge_dokploy_settings(merged_target, target_payload)
        targets.append(
            _resolve_dokploy_project_reference(
                merged_target,
                raw_projects=raw_projects,
                label=f"targets[{target_index}]",
            )
        )

    return {
        "schema_version": normalized_payload.get("schema_version"),
        "targets": targets,
    }


def _resolve_dokploy_profile(
    profile_name: str,
    *,
    raw_profiles: Mapping[str, object],
    raw_projects: Mapping[str, object],
    resolved_profiles: dict[str, dict[str, object]],
    active_profiles: tuple[str, ...],
) -> dict[str, object]:
    if profile_name in resolved_profiles:
        return dict(resolved_profiles[profile_name])
    if profile_name in active_profiles:
        profile_chain = " -> ".join((*active_profiles, profile_name))
        raise ValueError(f"Dokploy profile inheritance cycle detected: {profile_chain}")

    raw_profile = raw_profiles.get(profile_name)
    if raw_profile is None:
        raise ValueError(f"Unknown dokploy profile: {profile_name}")
    if not isinstance(raw_profile, Mapping):
        raise ValueError(f"Dokploy profile '{profile_name}' must be a table/object")

    profile_payload = dict(raw_profile)
    parent_profile_name = str(profile_payload.pop("extends", "") or "").strip()
    merged_profile: dict[str, object] = {}
    if parent_profile_name:
        merged_profile = _resolve_dokploy_profile(
            parent_profile_name,
            raw_profiles=raw_profiles,
            raw_projects=raw_projects,
            resolved_profiles=resolved_profiles,
            active_profiles=(*active_profiles, profile_name),
        )
    merged_profile = _merge_dokploy_settings(merged_profile, profile_payload)
    merged_profile = _resolve_dokploy_project_reference(
        merged_profile,
        raw_projects=raw_projects,
        label=f"profiles.{profile_name}",
    )
    resolved_profiles[profile_name] = dict(merged_profile)
    return merged_profile


def _resolve_dokploy_project_reference(
    payload: dict[str, object],
    *,
    raw_projects: Mapping[str, object],
    label: str,
) -> dict[str, object]:
    resolved_payload = dict(payload)
    raw_project_alias = resolved_payload.pop("project", None)
    if raw_project_alias in (None, ""):
        return resolved_payload

    project_alias = str(raw_project_alias).strip()
    if not project_alias:
        return resolved_payload
    if str(resolved_payload.get("project_name") or "").strip():
        raise ValueError(f"{label} cannot define both project and project_name")

    raw_project_value = raw_projects.get(project_alias)
    if raw_project_value is None:
        raise ValueError(f"Unknown dokploy project alias '{project_alias}' in {label}")
    if isinstance(raw_project_value, str):
        project_name = raw_project_value.strip()
    elif isinstance(raw_project_value, Mapping):
        project_name = str(raw_project_value.get("project_name") or "").strip()
    else:
        raise ValueError(
            f"Dokploy project alias '{project_alias}' in {label} must be a string or table"
        )
    if not project_name:
        raise ValueError(
            f"Dokploy project alias '{project_alias}' in {label} is missing project_name"
        )

    resolved_payload["project_name"] = project_name
    return resolved_payload


def _expect_mapping(raw_value: object, *, label: str) -> dict[str, object]:
    if raw_value in (None, ""):
        return {}
    if not isinstance(raw_value, Mapping):
        raise ValueError(f"Dokploy {label} must be a table/object")
    if not all(isinstance(key_name, str) for key_name in raw_value):
        raise ValueError(f"Dokploy {label} keys must be strings")
    return dict(raw_value)


def _merge_dokploy_settings(
    base: Mapping[str, object], overlay: Mapping[str, object]
) -> dict[str, object]:
    merged_settings = dict(base)
    for key_name, key_value in overlay.items():
        base_env = merged_settings.get("env")
        if key_name == "env" and isinstance(base_env, Mapping) and isinstance(key_value, Mapping):
            merged_env: dict[str, object] = {}
            for env_key, env_value in base_env.items():
                if isinstance(env_key, str):
                    merged_env[env_key] = env_value
            for env_key, env_value in key_value.items():
                if isinstance(env_key, str):
                    merged_env[env_key] = env_value
            merged_settings["env"] = merged_env
            continue
        merged_settings[key_name] = key_value
    return merged_settings
