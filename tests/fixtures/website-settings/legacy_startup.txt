#!/usr/bin/env python3
"""Initialize Odoo state if needed before launching the long-running web server.

This wrapper keeps first-start behavior deterministic:
- Wait for PostgreSQL connectivity
- Initialize the configured database when required
- Start the standard Odoo HTTP server process

The command is safe to run on every container start because initialization only
executes when the target database is missing required installed modules.
"""

import argparse
import configparser
import json
import os
import re
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

import psycopg2
from odoo_admin_password import validate_admin_password

RUNTIME_OPTION_MAP: tuple[tuple[str, str], ...] = (
    ("ODOO_DB_MAXCONN", "db_maxconn"),
    ("ODOO_MAX_CRON_THREADS", "max_cron_threads"),
    ("ODOO_WORKERS", "workers"),
    ("ODOO_LIMIT_TIME_CPU", "limit_time_cpu"),
    ("ODOO_LIMIT_TIME_REAL", "limit_time_real"),
    ("ODOO_LIMIT_TIME_REAL_CRON", "limit_time_real_cron"),
    ("ODOO_LIMIT_TIME_WORKER_CRON", "limit_time_worker_cron"),
    ("ODOO_LIMIT_MEMORY_SOFT", "limit_memory_soft"),
    ("ODOO_LIMIT_MEMORY_HARD", "limit_memory_hard"),
)
MAIL_OPTION_MAP: tuple[tuple[str, str], ...] = (
    ("ODOO_SMTP_SERVER", "smtp_server"),
    ("ODOO_SMTP_PORT", "smtp_port"),
    ("ODOO_SMTP_USER", "smtp_user"),
    ("ODOO_SMTP_PASSWORD", "smtp_password"),
    ("ODOO_SMTP_SSL", "smtp_ssl"),
    ("ODOO_EMAIL_FROM", "email_from"),
    ("ODOO_FROM_FILTER", "from_filter"),
)

UNSAFE_MASTER_PASSWORDS = {"admin"}
# An empty or unset PLATFORM_INSTANCE counts as public; only an explicit local
# name relaxes the public credential and dbfilter rules.
LOCAL_INSTANCE_NAMES = {"local", "dev", "development"}
RUNTIME_SCRIPTS_PATH = "/volumes/scripts"
LAUNCHPLANE_ADDONS_PATH = "/opt/launchplane/addons"


@dataclass(frozen=True)
class StartupSettings:
    config_path: str
    base_config_path: str
    platform_instance: str
    database_name: str
    database_host: str
    database_port: int
    database_user: str
    database_password: str
    master_password: str
    admin_login: str
    admin_password: str
    addons_path: str
    data_dir: str
    list_db: str
    install_modules: tuple[str, ...]
    data_workflow_lock_file: str
    data_workflow_lock_timeout_seconds: int
    ready_timeout_seconds: int
    poll_interval_seconds: float

    def __post_init__(self) -> None:
        validate_admin_password(self.admin_password)


def _split_modules(raw_modules: str) -> tuple[str, ...]:
    parsed_modules: list[str] = []
    for module_name in (raw_modules or "").split(","):
        normalized_module_name = module_name.strip()
        if not normalized_module_name:
            continue
        if normalized_module_name in parsed_modules:
            continue
        parsed_modules.append(normalized_module_name)
    return tuple(parsed_modules)


def _normalize_comma_list_with_first_item(first_item: str, raw_list: str) -> str:
    normalized_items = [first_item]
    for raw_item in raw_list.split(","):
        item = raw_item.strip()
        if not item or item in normalized_items:
            continue
        normalized_items.append(item)
    return ",".join(normalized_items)


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bootstrap Odoo database and launch web server")
    parser.add_argument("-c", "--config", dest="config_path", required=True)
    return parser.parse_args()


def _load_settings(argument_namespace: argparse.Namespace) -> StartupSettings:
    database_name = os.environ.get("ODOO_DB_NAME", "").strip()
    if not database_name:
        raise RuntimeError("ODOO_DB_NAME must be set for startup.")

    database_port_raw = os.environ.get("ODOO_DB_PORT", "5432").strip() or "5432"
    database_port = int(database_port_raw)
    install_modules = _split_modules(os.environ.get("ODOO_INSTALL_MODULES", ""))
    master_password = os.environ.get("ODOO_MASTER_PASSWORD", "").strip()
    if not master_password:
        raise RuntimeError("ODOO_MASTER_PASSWORD must be set for startup.")

    return StartupSettings(
        config_path=argument_namespace.config_path,
        base_config_path=os.environ.get("ODOO_CONFIG", "/volumes/config/_generated.conf").strip()
        or "/volumes/config/_generated.conf",
        platform_instance=os.environ.get("PLATFORM_INSTANCE", "").strip(),
        database_name=database_name,
        database_host=os.environ.get("ODOO_DB_HOST", "database").strip() or "database",
        database_port=database_port,
        database_user=os.environ.get("ODOO_DB_USER", "odoo").strip() or "odoo",
        database_password=os.environ.get("ODOO_DB_PASSWORD", ""),
        master_password=master_password,
        admin_login=os.environ.get("ODOO_ADMIN_LOGIN", "").strip() or "admin",
        admin_password=os.environ.get("ODOO_ADMIN_PASSWORD", ""),
        addons_path=_normalize_comma_list_with_first_item(
            LAUNCHPLANE_ADDONS_PATH,
            os.environ.get("ODOO_ADDONS_PATH", "").strip(),
        ),
        data_dir=os.environ.get("ODOO_DATA_DIR", "/volumes/data").strip() or "/volumes/data",
        list_db=os.environ.get("ODOO_LIST_DB", "False").strip() or "False",
        install_modules=install_modules,
        data_workflow_lock_file=os.environ.get(
            "ODOO_DATA_WORKFLOW_LOCK_FILE",
            "/volumes/data/.data_workflow_in_progress",
        ).strip()
        or "/volumes/data/.data_workflow_in_progress",
        data_workflow_lock_timeout_seconds=int(os.environ.get("ODOO_DATA_WORKFLOW_LOCK_TIMEOUT_SECONDS", "7200").strip() or "7200"),
        ready_timeout_seconds=180,
        poll_interval_seconds=2.0,
    )


def _is_public_runtime(settings: StartupSettings) -> bool:
    return settings.platform_instance.strip().lower() not in LOCAL_INSTANCE_NAMES


def _database_filter_pattern(database_name: str) -> str:
    return f"^{re.escape(database_name)}$"


def _enforce_public_credential_preflight(settings: StartupSettings) -> None:
    if not _is_public_runtime(settings):
        return
    if settings.master_password.strip().lower() in UNSAFE_MASTER_PASSWORDS:
        raise RuntimeError("Insecure configuration: ODOO_MASTER_PASSWORD must not use a default value for public runtimes.")
    if not settings.admin_password.strip():
        raise RuntimeError("Insecure configuration: ODOO_ADMIN_PASSWORD must be set for public runtimes.")


def _write_runtime_config(settings: StartupSettings) -> None:
    config_parser = configparser.ConfigParser(interpolation=None)
    if settings.base_config_path and os.path.exists(settings.base_config_path):
        config_parser.read(settings.base_config_path, encoding="utf-8")
    if not config_parser.has_section("options"):
        config_parser.add_section("options")

    options = config_parser["options"]
    options["admin_passwd"] = settings.master_password
    options["db_name"] = settings.database_name
    if _is_public_runtime(settings):
        options["dbfilter"] = _database_filter_pattern(settings.database_name)
    options["db_user"] = settings.database_user
    options["db_password"] = settings.database_password
    options["db_host"] = settings.database_host
    options["db_port"] = str(settings.database_port)
    options["list_db"] = settings.list_db
    options["data_dir"] = settings.data_dir
    if settings.addons_path:
        options["addons_path"] = settings.addons_path

    # Keep runtime tuning deterministic by overlaying supported env-driven
    # options onto the generated base config on every container start.
    for env_name, option_name in RUNTIME_OPTION_MAP:
        option_value = os.environ.get(env_name, "").strip()
        if option_value:
            options[option_name] = option_value

    # Explicit empty values clear inherited mail options. Preserve password bytes.
    for env_name, option_name in MAIL_OPTION_MAP:
        if env_name in os.environ:
            value = os.environ[env_name]
            options[option_name] = value if option_name == "smtp_password" else value.strip()

    dev_mode_value = os.environ.get("ODOO_DEV_MODE", "").strip()
    if dev_mode_value:
        options["dev_mode"] = dev_mode_value
    elif "dev_mode" in options:
        options.pop("dev_mode", None)

    config_path = settings.config_path
    config_directory = os.path.dirname(config_path)
    if config_directory:
        os.makedirs(config_directory, exist_ok=True)
    descriptor = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as config_file:
        os.fchmod(config_file.fileno(), 0o600)
        config_parser.write(config_file)


def _wait_for_database(settings: StartupSettings) -> None:
    deadline = time.monotonic() + settings.ready_timeout_seconds
    while time.monotonic() < deadline:
        try:
            with psycopg2.connect(
                host=settings.database_host,
                port=settings.database_port,
                user=settings.database_user,
                password=settings.database_password,
                dbname="postgres",
            ):
                return
        except psycopg2.OperationalError:
            time.sleep(settings.poll_interval_seconds)
    raise RuntimeError(
        "Database did not become reachable within "
        f"{settings.ready_timeout_seconds} seconds ({settings.database_host}:{settings.database_port})."
    )


def _database_exists(settings: StartupSettings) -> bool:
    with psycopg2.connect(
        host=settings.database_host,
        port=settings.database_port,
        user=settings.database_user,
        password=settings.database_password,
        dbname="postgres",
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (settings.database_name,))
            return cursor.fetchone() is not None


def _installed_module_names(settings: StartupSettings) -> set[str]:
    with psycopg2.connect(
        host=settings.database_host,
        port=settings.database_port,
        user=settings.database_user,
        password=settings.database_password,
        dbname=settings.database_name,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT name FROM ir_module_module WHERE state = 'installed'")
            return {str(row[0]) for row in cursor.fetchall()}


def _missing_required_modules(settings: StartupSettings) -> tuple[str, ...]:
    if not _database_exists(settings):
        return settings.install_modules

    try:
        installed_modules = _installed_module_names(settings)
    except psycopg2.errors.UndefinedTable:
        return settings.install_modules

    missing_modules: list[str] = []
    for module_name in settings.install_modules:
        if module_name not in installed_modules:
            missing_modules.append(module_name)
    return tuple(missing_modules)


def _build_odoo_command(
    settings: StartupSettings,
    *,
    initialize_modules: tuple[str, ...] | None = None,
    stop_after_init: bool,
) -> list[str]:
    command = [
        "/odoo/odoo-bin",
        "-c",
        settings.config_path,
        "-d",
        settings.database_name,
        f"--db_host={settings.database_host}",
        f"--db_port={settings.database_port}",
        f"--db_user={settings.database_user}",
    ]
    if _is_public_runtime(settings):
        command.append(f"--db-filter={_database_filter_pattern(settings.database_name)}")

    if initialize_modules is not None:
        normalized_modules = ["base", *initialize_modules]
        unique_modules: list[str] = []
        for module_name in normalized_modules:
            if module_name in unique_modules:
                continue
            unique_modules.append(module_name)
        command.extend(["-i", ",".join(unique_modules)])

    if stop_after_init:
        command.append("--stop-after-init")
    return command


def _build_odoo_shell_command(settings: StartupSettings) -> list[str]:
    return [
        "/odoo/odoo-bin",
        "shell",
        "-c",
        settings.config_path,
        "-d",
        settings.database_name,
        f"--db_host={settings.database_host}",
        f"--db_port={settings.database_port}",
        f"--db_user={settings.database_user}",
        "--no-http",
    ]


def _odoo_environment(settings: StartupSettings) -> dict[str, str]:
    environment = os.environ.copy()
    # Odoo's native environment option preserves whitespace that its conf reader trims.
    # Override even an inherited PGPASSWORD so every launch uses these settings.
    environment["PGPASSWORD"] = settings.database_password
    python_path_parts = [
        part for part in environment.get("PYTHONPATH", "").split(os.pathsep) if part and part != RUNTIME_SCRIPTS_PATH
    ]
    python_path_parts.insert(0, RUNTIME_SCRIPTS_PATH)
    environment["PYTHONPATH"] = os.pathsep.join(python_path_parts)
    return environment


def _run_odoo_shell(settings: StartupSettings, script_text: str, *, label: str) -> None:
    print(f"[platform-startup] running {label}", flush=True)
    subprocess.run(_build_odoo_shell_command(settings), input=script_text.encode(), env=_odoo_environment(settings), check=True)


def _addon_has_optional_dependency(pyproject_path: Path, extra_name: str) -> bool:
    pyproject_payload = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    optional_dependencies = pyproject_payload.get("project", {}).get("optional-dependencies", {}) or {}
    return extra_name in optional_dependencies


def _discover_project_addon_directories() -> tuple[Path, ...]:
    addons_root = Path("/opt/project/addons")
    if not addons_root.is_dir():
        return ()

    addon_directories: list[Path] = []
    for child_path in sorted(addons_root.iterdir()):
        if not child_path.is_dir():
            continue
        if child_path.name.startswith((".", "__")):
            continue
        if (
            (child_path / "pyproject.toml").exists()
            or (child_path / "requirements.txt").exists()
            or (child_path / "requirements-dev.txt").exists()
        ):
            addon_directories.append(child_path)
            continue
        for nested_path in sorted(child_path.iterdir()):
            if not nested_path.is_dir():
                continue
            if (
                (nested_path / "pyproject.toml").exists()
                or (nested_path / "requirements.txt").exists()
                or (nested_path / "requirements-dev.txt").exists()
            ):
                addon_directories.append(nested_path)
    return tuple(addon_directories)


def _install_local_addon_dependencies(sync_mode: str) -> None:
    python_sync_environment = dict(os.environ)
    python_sync_environment["UV_CACHE_DIR"] = "/tmp/uv-cache"
    Path(python_sync_environment["UV_CACHE_DIR"]).mkdir(parents=True, exist_ok=True)
    python_executable = "/venv/bin/python3"

    for addon_directory in _discover_project_addon_directories():
        requirements_path = addon_directory / "requirements.txt"
        if requirements_path.exists():
            print(f"[platform-startup] installing addon requirements: {requirements_path}", flush=True)
            subprocess.run(
                ["uv", "pip", "install", "--python", python_executable, "-r", str(requirements_path)],
                check=True,
                env=python_sync_environment,
            )

        pyproject_path = addon_directory / "pyproject.toml"
        if pyproject_path.exists():
            print(f"[platform-startup] installing addon package: {pyproject_path}", flush=True)
            install_target = ".[dev]" if sync_mode == "dev" and _addon_has_optional_dependency(pyproject_path, "dev") else "."
            subprocess.run(
                ["uv", "pip", "install", "--python", python_executable, install_target],
                check=True,
                env=python_sync_environment,
                cwd=addon_directory,
            )

        requirements_dev_path = addon_directory / "requirements-dev.txt"
        if sync_mode == "dev" and requirements_dev_path.exists():
            print(f"[platform-startup] installing addon dev requirements: {requirements_dev_path}", flush=True)
            subprocess.run(
                ["uv", "pip", "install", "--python", python_executable, "-r", str(requirements_dev_path)],
                check=True,
                env=python_sync_environment,
            )


def _sync_python_dependencies_if_needed(settings: StartupSettings) -> None:
    if settings.platform_instance != "local":
        return

    sync_mode = "dev" if os.environ.get("ODOO_DEV_MODE", "").strip() else "prod"
    print(
        f"[platform-startup] syncing mounted Python dependencies for local runtime ({sync_mode})",
        flush=True,
    )
    _install_local_addon_dependencies(sync_mode)


def _apply_admin_password_if_configured(settings: StartupSettings) -> None:
    if not settings.admin_password.strip():
        return

    payload = {
        "login": settings.admin_login,
        "password": settings.admin_password,
    }
    script = """
import json
from odoo.exceptions import AccessDenied

payload = json.loads(__PAYLOAD__)
admin_user = env['res.users'].sudo().with_context(active_test=False).search(
    [('login', '=', payload['login'])],
    limit=1,
)
if not admin_user:
    print(f"configured_admin_user_found=false login={payload['login']}")
else:
    try:
        admin_user.with_user(admin_user)._check_credentials(
            {'type': 'password', 'password': payload['password']},
            {'interactive': True},
        )
    except AccessDenied:
        admin_user.with_context(no_reset_password=True).sudo().write({'password': payload['password']})
        print('admin_password_updated=true')
    else:
        print('admin_password_updated=false')
    env.cr.commit()
""".replace("__PAYLOAD__", repr(json.dumps(payload)))
    _run_odoo_shell(settings, script, label="admin hardening")


def _apply_environment_overrides_if_available(settings: StartupSettings) -> None:
    script = """
from odoo_website_bootstrap import (
    apply_website_bootstrap,
    load_instance_override_payload,
    payload_has_launchplane_settings,
    require_launchplane_payloads_if_configured,
)

instance_override_payload = load_instance_override_payload()
settings_payload_present = payload_has_launchplane_settings(instance_override_payload)
require_launchplane_payloads_if_configured(instance_override_payload)
if 'launchplane.settings' in env.registry:
    env['launchplane.settings'].sudo().apply_from_env()
elif payload_has_launchplane_settings(instance_override_payload):
    raise RuntimeError(
        'Launchplane supplied ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64, '
        'but launchplane.settings is not installed.'
    )
apply_website_bootstrap(env, instance_override_payload)
env.cr.commit()
if settings_payload_present:
    print('launchplane_settings_applied=true')
elif instance_override_payload is None:
    print('launchplane_settings_applied=false reason=no_payload')
else:
    print('launchplane_settings_applied=false reason=no_managed_settings')
"""
    _run_odoo_shell(settings, script, label="environment overrides")


def _assert_active_admin_password_is_not_default(settings: StartupSettings) -> None:
    login_names_to_check = ["admin"]
    if settings.admin_login not in login_names_to_check:
        login_names_to_check.append(settings.admin_login)

    payload = {"logins": login_names_to_check}
    script = """
import json
from odoo.exceptions import AccessDenied

payload = json.loads(__PAYLOAD__)

for login_name in payload['logins']:
    target_user = env['res.users'].sudo().with_context(active_test=False).search(
        [('login', '=', login_name)],
        limit=1,
    )
    if not target_user:
        continue

    authenticated = False
    try:
        auth_info = env['res.users'].sudo().authenticate(
            {'type': 'password', 'login': login_name, 'password': 'admin'},
            {'interactive': False},
        )
        authenticated = bool(auth_info)
    except AccessDenied:
        authenticated = False

    if authenticated:
        raise ValueError(f"Insecure configuration: active password for {login_name} is 'admin'.")

print('admin_default_password_active=false')
""".replace("__PAYLOAD__", repr(json.dumps(payload)))
    _run_odoo_shell(settings, script, label="admin password policy")


def _run_initialization_if_needed(settings: StartupSettings) -> None:
    missing_modules = _missing_required_modules(settings)
    if not missing_modules and _database_exists(settings):
        print("[platform-startup] database already initialized; skipping init", flush=True)
        return

    print(
        f"[platform-startup] running database initialization for modules: {','.join(['base', *missing_modules])}",
        flush=True,
    )
    initialize_command = _build_odoo_command(settings, initialize_modules=missing_modules, stop_after_init=True)
    subprocess.run(initialize_command, env=_odoo_environment(settings), check=True)


def _wait_for_data_workflow_lock(settings: StartupSettings) -> None:
    if not settings.data_workflow_lock_file:
        return

    lock_path = settings.data_workflow_lock_file
    if not os.path.exists(lock_path):
        return

    deadline = time.monotonic() + settings.data_workflow_lock_timeout_seconds
    wait_seconds = 0
    while os.path.exists(lock_path):
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Data workflow lock {lock_path} still present after {settings.data_workflow_lock_timeout_seconds} seconds."
            )
        if wait_seconds % 10 == 0:
            print(
                f"[platform-startup] waiting for data workflow lock release: {lock_path}",
                flush=True,
            )
        time.sleep(settings.poll_interval_seconds)
        wait_seconds += 1

    print("[platform-startup] data workflow lock cleared; continuing startup", flush=True)


def main() -> None:
    arguments = _parse_arguments()
    settings = _load_settings(arguments)
    _enforce_public_credential_preflight(settings)
    _write_runtime_config(settings)
    _wait_for_database(settings)
    _wait_for_data_workflow_lock(settings)
    _sync_python_dependencies_if_needed(settings)
    _run_initialization_if_needed(settings)
    _apply_environment_overrides_if_available(settings)
    _apply_admin_password_if_configured(settings)
    if settings.admin_password.strip() or _is_public_runtime(settings):
        _assert_active_admin_password_is_not_default(settings)

    print("[platform-startup] starting Odoo web server", flush=True)
    server_command = _build_odoo_command(settings, stop_after_init=False)
    os.execve(server_command[0], server_command, _odoo_environment(settings))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # pragma: no cover - startup guard
        print(f"[platform-startup] fatal: {error}", file=sys.stderr, flush=True)
        raise
