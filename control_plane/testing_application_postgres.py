"""A fresh PostgreSQL recipient never borrows the retained application's database authority."""

import base64
import hashlib
import json

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.testing_activation import TestingActivationRecord
from control_plane.odoo_testing_resources import ApplicationTestingResources
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_application_credentials import postgres_coordinates, state_password
from control_plane.testing_application_capabilities import runtime_capability_program_header


def postgres_source(
    store: PostgresRecordStore,
    record: TestingActivationRecord,
    resources: ApplicationTestingResources,
) -> tuple[dict[str, object], dict[str, str]]:
    specification = record.request.application
    if specification is None or specification.state.kind != "postgresql":
        raise ValueError("PostgreSQL preparation requires its reviewed state specification.")
    database, role, hostname = postgres_coordinates(record)
    state = specification.state
    environment = {
        "LP_STATE_BOOTSTRAP_PASSWORD": state_password(store, record, "bootstrap"),
        "LP_STATE_APPLICATION_PASSWORD": state_password(store, record, "application"),
    }
    return {
        "services": {
            "postgres": {
                "image": state.image_reference,
                "command": ["postgres", "-p", str(state.port)],
                "environment": {
                    "PGDATA": state.container_path,
                    "POSTGRES_USER": "bootstrap_" + role.removeprefix("app_"),
                    "POSTGRES_PASSWORD": "${LP_STATE_BOOTSTRAP_PASSWORD:?required}",
                    "POSTGRES_DB": "postgres",
                    "POSTGRES_INITDB_ARGS": "--auth-host=scram-sha-256",
                    "POSTGRES_HOST_AUTH_METHOD": "scram-sha-256",
                    "LP_APPLICATION_DATABASE": database,
                    "LP_APPLICATION_ROLE": role,
                    "LP_APPLICATION_PASSWORD": "${LP_STATE_APPLICATION_PASSWORD:?required}",
                },
                "volumes": ["database:" + state.container_path],
                "networks": {"provider": {"aliases": [hostname]}},
            },
        },
        "volumes": {"database": {"name": resources.volumes["database"], "external": True}},
        "networks": {"provider": {"name": specification.network_name, "external": True}},
    }, environment


def postgres_bootstrap_script(
    store: PostgresRecordStore,
    record: TestingActivationRecord,
    resources: ApplicationTestingResources,
    *,
    readback: bool,
) -> tuple[str, str]:
    specification = record.request.application
    if specification is None or specification.state.kind != "postgresql":
        raise ValueError("PostgreSQL readback requires its original state specification.")
    database, role, hostname = postgres_coordinates(record)
    spec = {
        "application": record.state_recipient["app_name"],
        "volume": resources.volumes["database"],
        "resources_binding": resources.binding_sha256,
        "data_path": specification.state.container_path,
        "image": specification.state.image_reference,
        "port": specification.state.port,
        "network": specification.network_name,
        "database": database,
        "role": role,
        "hostname": hostname,
        "bootstrap": "bootstrap_" + role.removeprefix("app_"),
        "bootstrap_password_sha256": hashlib.sha256(
            state_password(store, record, "bootstrap").encode()
        ).hexdigest(),
        "application_password_sha256": hashlib.sha256(
            state_password(store, record, "application").encode()
        ).hexdigest(),
    }
    binding = canonical_json_sha256(spec)
    encoded = base64.b64encode(
        json.dumps({**spec, "readback": readback}, sort_keys=True).encode()
    ).decode()
    script = (
        "#!/usr/bin/env bash\nset -euo pipefail\npython3 - "
        + encoded
        + " <<'LP_FRESH_POSTGRES'\n"
        + runtime_capability_program_header()
        + POSTGRES_BOOTSTRAP_PROGRAM
        + "\nLP_FRESH_POSTGRES\n"
    )
    return script, binding


POSTGRES_BOOTSTRAP_PROGRAM = r"""
import base64, hashlib, json, re, subprocess, sys
from pathlib import Path

def docker(*arguments, stdin=None):
    result = subprocess.run(["docker", *arguments], input=stdin, capture_output=True, text=True, timeout=60)
    if result.returncode or len(result.stdout) > 2 * 1024 * 1024:
        raise ValueError("fresh state provider unavailable")
    return result.stdout

def identifier(value):
    return '"' + value.replace('"', '""') + '"'

def literal(value):
    return "'" + value.replace("'", "''") + "'"

try:
    spec = json.loads(base64.b64decode(sys.argv[1], validate=True))
    candidates = docker("ps", "-aq", "--no-trunc", "--filter", "label=com.docker.compose.project=" + spec["application"], "--filter", "label=com.docker.compose.service=postgres").splitlines()
    if len(candidates) != 1:
        raise ValueError("fresh state recipient is absent or ambiguous")
    container = json.loads(docker("inspect", candidates[0]))
    if not isinstance(container, list) or len(container) != 1 or container[0].get("Id") != candidates[0]:
        raise ValueError("fresh state recipient identity unavailable")
    container = container[0]
    state = container.get("State", {})
    if state.get("Running") is not True or state.get("Restarting") or state.get("Paused"):
        raise ValueError("fresh state recipient is not ready")
    configuration = container.get("Config", {})
    if configuration.get("Image") != spec["image"]:
        raise ValueError("fresh state immutable image changed")
    env = {}
    for value in configuration.get("Env", []):
        key, value = value.split("=", 1)
        if key in env:
            raise ValueError("fresh state runtime ambiguous")
        env[key] = value
    if find_platform_credentials(env) or env.get("DOCKER_HOST"):
        raise ValueError("fresh state inherited provider authority")
    if (env.get("POSTGRES_USER") != spec["bootstrap"] or env.get("PGDATA") != spec["data_path"]
        or env.get("POSTGRES_INITDB_ARGS") != "--auth-host=scram-sha-256"
        or env.get("POSTGRES_HOST_AUTH_METHOD") != "scram-sha-256"
        or env.get("LP_APPLICATION_DATABASE") != spec["database"] or env.get("LP_APPLICATION_ROLE") != spec["role"]
        or hashlib.sha256(env.get("POSTGRES_PASSWORD", "").encode()).hexdigest() != spec["bootstrap_password_sha256"]
        or hashlib.sha256(env.get("LP_APPLICATION_PASSWORD", "").encode()).hexdigest() != spec["application_password_sha256"]):
        raise ValueError("fresh state credential provenance changed")
    if container.get("HostConfig", {}).get("PortBindings") not in (None, {}):
        raise ValueError("fresh state unexpectedly publishes a port")
    if set(container.get("NetworkSettings", {}).get("Networks", {})) != {spec["network"]}:
        raise ValueError("fresh state network binding changed")
    network = container["NetworkSettings"]["Networks"][spec["network"]]
    def aliases(network):
        values = []
        for key in ("Aliases", "DNSNames"):
            names = network.get(key)
            if names is not None:
                if not isinstance(names, list) or any(not isinstance(value, str) for value in names):
                    raise ValueError("network aliases unavailable")
                values.extend(names)
        return {value.casefold() for value in values}
    if spec["hostname"].casefold() not in aliases(network) or not network.get("IPAddress") or not network.get("NetworkID"):
        raise ValueError("fresh state consumer address is unavailable")
    volume = json.loads(docker("volume", "inspect", spec["volume"]))
    if (not isinstance(volume, list) or len(volume) != 1 or volume[0].get("Name") != spec["volume"]
        or volume[0].get("Driver") != "local" or volume[0].get("Options") not in (None, {})
        or volume[0].get("Labels", {}).get("io.launchplane.testing-resources") != spec["resources_binding"]):
        raise ValueError("fresh state storage provenance changed")
    mounts = container.get("Mounts")
    if (not isinstance(mounts, list) or len(mounts) != 1 or mounts[0].get("Type") != "volume"
        or mounts[0].get("Name") != spec["volume"] or mounts[0].get("Destination") != spec["data_path"]
        or mounts[0].get("Source") != volume[0].get("Mountpoint")):
        raise ValueError("fresh state storage binding changed")
    backing = Path(volume[0]["Mountpoint"]).resolve(strict=True)
    ids = docker("ps", "-aq", "--no-trunc").splitlines()
    if len(ids) != len(set(ids)) or len(ids) > 10000:
        raise ValueError("mount inventory incomplete")
    for candidate in ids:
        if candidate == container["Id"]:
            continue
        # Shared host inspection reads mounts only, never another application's env.
        metadata = json.loads(docker("inspect", "--format", '{"mounts":{{json .Mounts}},"networks":{{json .NetworkSettings.Networks}}}', candidate))
        other = metadata.get("mounts")
        if not isinstance(other, list) or not isinstance(metadata.get("networks"), dict):
            raise ValueError("mount inventory unavailable")
        foreign = metadata["networks"].get(spec["network"])
        if foreign is not None and (not isinstance(foreign, dict) or spec["hostname"].casefold() in aliases(foreign)):
            raise ValueError("fresh state address has another endpoint owner")
        for mount in other:
            if mount.get("Type") == "tmpfs":
                continue
            source = mount.get("Source")
            if not isinstance(source, str) or not source.startswith("/"):
                raise ValueError("mount backing unavailable")
            path = Path(source).resolve()
            if mount.get("Type") == "bind" and mount.get("RW") is False and path in backing.parents:
                continue
            if mount.get("Name") == spec["volume"] or path == backing or path in backing.parents or backing in path.parents:
                raise ValueError("fresh state backing overlaps another recipient")
    service_ids = docker("service", "ls", "-q", "--no-trunc").splitlines()
    if len(service_ids) > 10000 or len(service_ids) != len(set(service_ids)):
        raise ValueError("network service inventory incomplete")
    for service in service_ids:
        metadata = json.loads(docker("service", "inspect", "--format", '{"name":{{json .Spec.Name}},"networks":{{json .Spec.TaskTemplate.Networks}}}', service))
        networks = metadata.get("networks")
        if networks is None and "networks" in metadata:
            networks = []
        if not isinstance(networks, list):
            raise ValueError("network service ownership unavailable")
        for attachment in networks:
            if attachment.get("Target") == network["NetworkID"]:
                if metadata.get("name") == spec["hostname"] or spec["hostname"].casefold() in aliases(attachment):
                    raise ValueError("fresh state address aliases another service")
    def sql(statement, *, application=False):
        user = spec["role"] if application else spec["bootstrap"]
        password = env["LP_APPLICATION_PASSWORD"] if application else env["POSTGRES_PASSWORD"]
        database = spec["database"] if application else "postgres"
        return docker("exec", "-i", "-e", "PGPASSWORD=" + password, container["Id"], "psql", "--no-password", "-h", spec["hostname"] if application else "127.0.0.1", "-p", str(spec["port"]), "-U", user, "-d", database, "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1", stdin=statement).strip()
    role = sql("SELECT rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls FROM pg_roles WHERE rolname=" + literal(spec["role"]) + ";")
    if not role and not spec["readback"]:
        sql("CREATE ROLE " + identifier(spec["role"]) + " LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD " + literal(env["LP_APPLICATION_PASSWORD"]) + ";")
        role = sql("SELECT rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls FROM pg_roles WHERE rolname=" + literal(spec["role"]) + ";")
    if role != "t|f|f|f|f|f":
        raise ValueError("fresh application database role unqualified")
    owner = sql("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=" + literal(spec["database"]) + ";")
    if not owner and not spec["readback"]:
        existing = sql("SELECT datname FROM pg_database WHERE datname NOT IN ('postgres','template0','template1');")
        if existing:
            raise ValueError("unexpected database on fresh state recipient")
        sql("CREATE DATABASE " + identifier(spec["database"]) + " OWNER " + identifier(spec["role"]) + ";")
        owner = sql("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=" + literal(spec["database"]) + ";")
    if owner != spec["role"] or sql("SELECT current_user || '|' || current_database() || '|' || host(inet_server_addr());", application=True) != spec["role"] + "|" + spec["database"] + "|" + network["IPAddress"]:
        raise ValueError("fresh application database authentication unqualified")
    if sql("SELECT rolname FROM pg_roles WHERE rolcanlogin ORDER BY rolname;").splitlines() != sorted([spec["bootstrap"], spec["role"]]):
        raise ValueError("fresh state has an unexpected login capability")
    invalid_password = hashlib.sha256(env["LP_APPLICATION_PASSWORD"].encode()).hexdigest()
    rejected = subprocess.run(["docker", "exec", "-i", "-e", "PGPASSWORD=" + invalid_password, container["Id"], "psql", "--no-password", "-h", spec["hostname"], "-p", str(spec["port"]), "-U", spec["role"], "-d", spec["database"], "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1"], input="SELECT current_user;", capture_output=True, text=True, timeout=60)
    if rejected.returncode == 0 or not re.search(r"password authentication failed", rejected.stderr, re.IGNORECASE):
        raise ValueError("fresh state did not enforce password authentication")
    current = json.loads(docker("inspect", container["Id"]))[0]
    if (docker("ps", "-aq", "--no-trunc").splitlines() != ids
        or docker("service", "ls", "-q", "--no-trunc").splitlines() != service_ids
        or any(current.get(key) != container.get(key) for key in ("Id", "Image", "Config", "Mounts", "NetworkSettings"))
        or current.get("State", {}).get("Running") is not True):
        raise ValueError("fresh state recipient changed")
    binding = {key: value for key, value in spec.items() if key != "readback"}
    print("testing_resources_binding_sha256=" + hashlib.sha256(json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
    print("testing_resources_ok=true")
except Exception:
    print("testing_resources_ok=false")
    sys.exit(1)
"""
