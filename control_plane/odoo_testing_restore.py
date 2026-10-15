"""Host-side source capture and pre-registry sanitization for a fresh testing copy."""

import base64
import json
from typing import Literal, TypeVar
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductRuntimeConfigRequirement,
    ProductSecretConfigRequirement,
    product_config_requirement_applies_to_lane,
)

from pydantic import BaseModel, ConfigDict, Field

from control_plane.odoo_connection_isolation import connection_isolation_library


Requirement = TypeVar(
    "Requirement", ProductRuntimeConfigRequirement, ProductSecretConfigRequirement
)


class FreshOdooTestingGeneration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str = Field(min_length=1)
    context: str = Field(min_length=1)
    instance: Literal["testing"] = "testing"
    operation_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    namespace: str = Field(pattern=r"^lp-testing-[a-f0-9]{32}$")
    domain_host: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
    resources_binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    bootstrap_plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    request_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    certificate_type: Literal["none", "letsencrypt"] = "none"
    omitted_runtime_keys: tuple[str, ...] = ()
    prepared_at: str
    completed_at: str = ""
    reconcile_requested_at: str = ""


# This wrapper uses the artifact's SQL-only credential boundary before the first
# registry process. The runner gets no upstream or SSH settings. Additional copy
# connection rules run in that same transaction boundary and again after hooks.
# A missing boundary is an unsupported artifact, never permission to run it.
TESTING_COPY_RUNNER = (
    """import base64
import logging
import os
from pathlib import Path
import subprocess
import sys
sys.path.insert(0, '/volumes/scripts')
from run_odoo_data_workflows import LocalServerSettings, OdooDataWorkflowRunner
"""
    + connection_isolation_library()
    + r"""
logging.disable(logging.CRITICAL)
if os.environ.get("PLATFORM_INSTANCE") != "testing":
    raise SystemExit("Testing copy requires its exact instance")
for key in tuple(os.environ):
    if key.startswith("ODOO_UPSTREAM_") or key.startswith("DATA_WORKFLOW_SSH_"):
        os.environ.pop(key)
os.environ["ODOO_RESTORE_KEPT_INTEGRATIONS"] = ""
os.environ["ENV_OVERRIDE_DISABLE_CRON"] = "true"
os.environ["NO_SANITIZE"] = "false"


class CopyRunner(OdooDataWorkflowRunner):
    def overwrite_filestore(self, target_owner):
        # Host capture has already staged the fresh local filestore. No SSH key
        # or network source exists inside this container.
        return subprocess.Popen(["true"])

    def prepare_credentials_before_registry(self, *args, **kwargs):
        super().prepare_credentials_before_registry(*args, **kwargs)
        self.connect_to_db()
        with self.local.db_conn.cursor() as cursor:
            refused = connection_isolation(cursor, apply=True)
            if refused:
                raise RuntimeError("Testing copy connection isolation failed")
        self.local.db_conn.commit()
        observer = self._connect_with_retry(self.local.db_name)
        try:
            with observer.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                if connection_isolation(cursor, apply=False):
                    raise RuntimeError("Testing copy connection commit readback failed")
            observer.rollback()
        finally:
            observer.close()
        # The shared SQL-only integration readback checks all supported families,
        # including instance overrides reapplied by the native credential pass.
        checked = subprocess.run(["python3", "-", self.local.db_name, sys.argv[3]],
            input=base64.b64decode(sys.argv[4]), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=False)
        if checked.returncode or b"integration_readback_ok=true" not in checked.stdout.splitlines():
            raise RuntimeError("Testing copy pre-registry integration readback failed")
        print("testing_copy_pre_registry_sanitized=true", flush=True)


if not callable(getattr(OdooDataWorkflowRunner, "prepare_credentials_before_registry", None)):
    raise SystemExit("Artifact lacks the pre-registry credential boundary")
settings = LocalServerSettings()
if settings.host != "database" or settings.db_name != sys.argv[2] or settings.platform_instance != "testing":
    raise SystemExit("Testing copy settings no longer name the isolated database")
runner = CopyRunner(settings, None, None)
runner.acquire_data_workflow_lock()
restored = False
try:
    dump = Path(sys.argv[1])
    copy_marker = "launchplane.testing_copy.restore_complete"
    if sys.argv[5] == "fresh":
        subprocess.run(["pg_restore", "--file=/dev/null", str(dump)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        runner.overwrite_database(dump)
        restored = True
        dump.unlink()
        runner.connect_to_db()
        with runner.local.db_conn.cursor() as cursor:
            cursor.execute("INSERT INTO ir_config_parameter (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (copy_marker, sys.argv[6]))
        runner.local.db_conn.commit()
    elif sys.argv[5] == "resume":
        runner.connect_to_db()
        with runner.local.db_conn.cursor() as cursor:
            cursor.execute("SELECT value FROM ir_config_parameter WHERE key = %s", (copy_marker,))
            if cursor.fetchone() != (sys.argv[6],):
                raise RuntimeError("Existing copy lacks this generation's completed restore witness")
    else:
        raise RuntimeError("Unknown fresh testing restore phase")
    runner.prepare_credentials_before_registry(restored_copy=True, do_sanitize=True)
    modules = [name.strip() for name in (os.environ.get("ODOO_UPDATE_MODULES") or "").split(",") if name.strip()]
    if not modules or "AUTO" in modules:
        raise RuntimeError("Fresh copy needs exact declared module intent")
    before = runner.module_update_witnesses(modules)
    runner._prepare_restored_database(runner._resolve_filestore_owner(), do_sanitize=True)
    states = runner._module_states_by_name()
    after = runner.module_update_witnesses(modules)
    if any(states.get(name) != "installed" or not after.get(name) or before.get(name) == after[name] for name in modules) or any(state in {"to install", "to upgrade", "to remove"} for state in states.values()):
        raise RuntimeError("Fresh copy lacks completed module update witnesses")
    print("odoo_module_update_completed=true", flush=True)
    print("odoo_module_update_modules_configured=true", flush=True)
    runner.assert_core_schema_healthy()
    runner.ensure_gpt_users()
    runner.prepare_credentials_before_registry(restored_copy=True, do_sanitize=True)
    print("testing_copy_sanitized=true", flush=True)
except BaseException:
    if restored and sys.argv[5] == "fresh":
        runner._drop_database_after_failed_restore()
    raise
finally:
    runner.release_data_workflow_lock()
"""
)


def encoded_testing_copy_runner() -> str:
    return base64.b64encode(TESTING_COPY_RUNNER.encode()).decode()


def generation_binding(generation: FreshOdooTestingGeneration) -> str:
    from control_plane.contracts.canonical_json import canonical_json_sha256

    return canonical_json_sha256(
        generation.model_dump(mode="json", exclude={"completed_at", "reconcile_requested_at"})
    )


def encoded_restore_source(environment: dict[str, str]) -> str:
    """Only the existing supported source coordinates; no credential carrier."""
    from control_plane.dokploy.post_deploy import resolve_upstream_restore_workflow_environment

    source = resolve_upstream_restore_workflow_environment(desired_env_map=environment)
    return base64.b64encode(json.dumps(source, sort_keys=True).encode()).decode()


def build_fresh_testing_restore_script(
    *,
    generation: FreshOdooTestingGeneration,
    compose_project: str,
    image_reference: str,
    database_name: str,
    source_environment: dict[str, str],
    readback_spec: str,
    readback_program: str,
    workflow_environment: dict[str, str],
) -> str:
    spec = {
        "generation": generation.model_dump(mode="json"),
        "compose_project": compose_project,
        "image_reference": image_reference,
        "database_name": database_name,
        "source": json.loads(base64.b64decode(encoded_restore_source(source_environment))),
        "readback_spec": readback_spec,
        "readback_program": readback_program,
        "copy_binding_sha256": __import__("hashlib")
        .sha256(
            (
                generation_binding(generation)
                + "\n"
                + image_reference
                + "\n"
                + database_name
                + "\n"
                + workflow_environment.get("ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64", "")
            ).encode()
        )
        .hexdigest(),
        "runner": TESTING_COPY_RUNNER,
        "workflow_environment": workflow_environment,
        "ssh_key_path": source_environment.get("DATA_WORKFLOW_SSH_KEY", ""),
        "ssh_directory": source_environment.get("DATA_WORKFLOW_SSH_DIR", ""),
    }
    encoded = base64.b64encode(json.dumps(spec, sort_keys=True).encode()).decode()
    return (
        "#!/usr/bin/env bash\nset -euo pipefail\npython3 - "
        + encoded
        + " <<'LP_FRESH_COPY'\n"
        + FRESH_TESTING_RESTORE_HOST_PROGRAM
        + "\nLP_FRESH_COPY\n"
    )


FRESH_TESTING_RESTORE_HOST_PROGRAM = r"""
import base64
import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile


def command(arguments, *, stdin=None, stdout=None):
    result = subprocess.run(arguments, input=stdin, stdout=stdout or subprocess.PIPE,
        stderr=subprocess.PIPE, check=False)
    if result.returncode:
        raise RuntimeError("Fresh testing copy command did not pass")
    return result.stdout or b""


def docker(*arguments, stdin=None):
    return command(["docker", *arguments], stdin=stdin)


def one_inspection(identifier):
    values = json.loads(docker("inspect", identifier))
    if not isinstance(values, list) or len(values) != 1:
        raise RuntimeError("Testing runtime inventory is incomplete")
    return values[0]


def main():
    spec = json.loads(base64.b64decode(sys.argv[1], validate=True))
    gen = spec["generation"]
    namespace = gen["namespace"]
    project = spec["compose_project"]
    database = spec["database_name"]
    if gen["instance"] != "testing" or not re.fullmatch(r"lp-testing-[a-f0-9]{32}", namespace):
        raise RuntimeError("Testing generation is invalid")
    if not re.fullmatch(re.escape(namespace) + r"(?:-[a-z0-9]{6})?", project):
        raise RuntimeError("Testing compose identity changed")
    if not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,62}", database):
        raise RuntimeError("Testing database identity is invalid")
    if not str(spec["workflow_environment"].get("ODOO_UPDATE_MODULES", "")).strip():
        raise RuntimeError("Testing restore has no declared module update intent")
    volumes = {namespace + "-data", namespace + "-db", namespace + "-logs"}
    containers = {}
    all_containers = []
    for identifier in docker("ps", "-aq").decode().splitlines():
        container = one_inspection(identifier)
        all_containers.append(container)
        labels = container.get("Config", {}).get("Labels") or {}
        if labels.get("com.docker.compose.project") == project:
            service = labels.get("com.docker.compose.service")
            if service not in {"web", "database", "script-runner"} or service in containers:
                raise RuntimeError("Testing compose has an unexpected container")
            containers[service] = (identifier, container)
    if set(containers) != {"web", "database", "script-runner"}:
        raise RuntimeError("Testing compose inventory is incomplete")
    for service, (identifier, container) in containers.items():
        configuration = container["Config"]
        if container.get("HostConfig", {}).get("Privileged") or container.get("HostConfig", {}).get("NetworkMode") == "host":
            raise RuntimeError("Testing compose has host capabilities")
        if any(container.get("HostConfig", {}).get("PortBindings", {}).values()):
            raise RuntimeError("Testing compose publishes host ports")
        expected = {namespace + "-db"} if service == "database" else {namespace + "-data", namespace + "-logs"}
        mounts = container.get("Mounts")
        if not isinstance(mounts, list) or {item.get("Name") for item in mounts} != expected or any(item.get("Type") != "volume" for item in mounts):
            raise RuntimeError("Testing compose has unexpected writable or credential mounts")
        if service != "database":
            if configuration.get("Image") != spec["image_reference"]:
                raise RuntimeError("Testing artifact identity changed")
            environment = dict(item.split("=", 1) for item in configuration.get("Env", []) if "=" in item)
            if environment.get("PLATFORM_INSTANCE") != "testing" or environment.get("ODOO_DB_HOST") != "database" or environment.get("ODOO_DB_NAME") != database:
                raise RuntimeError("Testing database endpoint is not local")
            if environment.get("ODOO_MAX_CRON_THREADS") != "0":
                raise RuntimeError("Fresh testing must have no cron workers")
            if container.get("HostConfig", {}).get("ExtraHosts"):
                raise RuntimeError("Fresh testing has host gateway aliases")
            if any(environment.get(key) for key in ("GITHUB_TOKEN", "GH_TOKEN", "SSH_AUTH_SOCK", "DATA_WORKFLOW_SSH_KEY", "ODOO_KEY", "ODOO_SMTP_PASSWORD")):
                raise RuntimeError("Testing holds an execution credential")
        if service != "web":
            networks = container.get("NetworkSettings", {}).get("Networks")
            if not isinstance(networks, dict) or len(networks) != 1:
                raise RuntimeError("Testing data containers are not privately networked")
            network = one_inspection(next(iter(networks)))
            if network.get("Internal") is not True:
                raise RuntimeError("Testing data network has outbound access")
    data_networks = set(containers["database"][1]["NetworkSettings"]["Networks"])
    if set(containers["script-runner"][1]["NetworkSettings"]["Networks"]) != data_networks or set(containers["web"][1]["NetworkSettings"]["Networks"]) != data_networks | {"dokploy-network"}:
        raise RuntimeError("Fresh testing has unexpected network reach")
    for container in all_containers:
        if container in [item[1] for item in containers.values()]:
            continue
        if any(mount.get("Name") in volumes for mount in container.get("Mounts", [])):
            raise RuntimeError("Testing storage is shared by another container")
    # Resolve actual backing paths too: a bind alias of a named volume is
    # storage sharing even when Docker reports different mount names.
    paths = []
    identities = set()
    for name in volumes:
        volume = json.loads(docker("volume", "inspect", name))[0]
        if volume.get("Driver") != "local" or volume.get("Options") not in (None, {}) or volume.get("Labels", {}).get("io.launchplane.testing-resources") != gen["resources_binding_sha256"]:
            raise RuntimeError("Testing backing storage changed")
        backing = Path(volume["Mountpoint"]).resolve(strict=True)
        identity = (backing.stat().st_dev, backing.stat().st_ino)
        if identity in identities:
            raise RuntimeError("Testing backing storage overlaps")
        identities.add(identity)
        paths.append(backing)
    own = {item[0] for item in containers.values()}
    for container in all_containers:
        if container.get("Id") in own:
            continue
        for mount in container.get("Mounts", []):
            if mount.get("Type") != "bind":
                continue
            source_path = Path(mount["Source"]).resolve(strict=False)
            if any(source_path == path or source_path.is_relative_to(path)
                   or (mount.get("RW", True) and path.is_relative_to(source_path))
                   for path in paths):
                raise RuntimeError("Testing storage has a bind consumer")
    runner = containers["script-runner"][0]
    web = containers["web"][0]
    docker("stop", web)
    print("testing_copy_runtime_isolated=true", flush=True)
    docker("exec", runner, "python3", "-c", "import sys;sys.path.insert(0,'/volumes/scripts');from run_odoo_data_workflows import OdooDataWorkflowRunner as R;assert all(callable(getattr(R,name,None)) for name in ('prepare_credentials_before_registry','_prepare_restored_database','module_update_witnesses','_module_states_by_name'))")
    present = docker("exec", "-i", containers["database"][0], "sh", "-c",
        'psql -X -q -t -A -v ON_ERROR_STOP=1 -v database_name="$1" -U "$POSTGRES_USER" -d postgres',
        "_", database, stdin=b"SELECT count(*) FROM pg_database WHERE datname = :'database_name';\n").decode().strip()
    if present not in {"0", "1"}:
        raise RuntimeError("Fresh testing database inventory is ambiguous")
    resume = present == "1"
    staging = None
    try:
        dump_path = "/tmp/" + namespace + "-database.dump"
        if not resume:
            source = spec["source"]
            for key in ("ODOO_UPSTREAM_HOST", "ODOO_UPSTREAM_USER", "ODOO_UPSTREAM_DB_USER", "ODOO_UPSTREAM_DB_NAME"):
                if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.:-]*", source[key]):
                    raise RuntimeError("Unsupported restore source coordinate")
            path = source["ODOO_UPSTREAM_FILESTORE_PATH"]
            if not path.startswith("/") or ".." in Path(path).parts or not re.fullmatch(r"[A-Za-z0-9_./-]+", path):
                raise RuntimeError("Unsupported restore source filestore")
            destination = source["ODOO_UPSTREAM_USER"] + "@" + source["ODOO_UPSTREAM_HOST"]
            ssh = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes"]
            for key in ("ssh_key_path", "ssh_directory"):
                if spec[key] and (not spec[key].startswith("/") or not re.fullmatch(r"[A-Za-z0-9_./-]+", spec[key]) or ".." in Path(spec[key]).parts):
                    raise RuntimeError("Unsupported existing host SSH location")
            ssh_directory = Path(spec["ssh_directory"] or "/home/ubuntu/.ssh")
            key_path = spec["ssh_key_path"]
            if not key_path:
                key_path = next((str(ssh_directory / name) for name in ("id_ed25519", "id_ecdsa", "id_rsa", "id_dsa") if (ssh_directory / name).is_file()), "")
            if not key_path:
                raise RuntimeError("Existing supported restore identity is unavailable")
            ssh.extend(["-i", key_path, "-o", "IdentitiesOnly=yes", "-o", "UserKnownHostsFile=" + str(ssh_directory / "known_hosts")])
            # The host's existing identity is used only by these two fixed source reads.
            # No key, agent socket or SSH directory enters an application container.
            remote = shlex.join(["sudo", "-u", source["ODOO_UPSTREAM_DB_USER"], "pg_dump", "-Fc", "--", source["ODOO_UPSTREAM_DB_NAME"]])
            staging = Path(tempfile.mkdtemp(prefix=namespace + "-copy-"))
            dump = staging / "database.dump"
            with dump.open("xb") as output:
                command([*ssh, destination, remote], stdout=output)
            filestore = staging / "filestore"
            filestore.mkdir()
            command(["rsync", "-a", "--safe-links", "--no-devices", "--no-specials", "--protect-args",
                "-e", shlex.join(ssh), destination + ":" + path.rstrip("/") + "/", str(filestore) + "/"])
            docker("exec", "-u", "root", runner, "mkdir", "-p", "/volumes/data/filestore/" + database)
            docker("cp", str(filestore) + "/.", runner + ":/volumes/data/filestore/" + database)
            docker("cp", str(dump), runner + ":" + dump_path)
            docker("exec", "-u", "root", runner, "chmod", "644", dump_path)
        uid = docker("exec", runner, "id", "-u").decode().strip()
        gid = docker("exec", runner, "id", "-g").decode().strip()
        if not uid.isdigit() or not gid.isdigit():
            raise RuntimeError("Testing workflow identity is unavailable")
        docker("exec", "-u", "root", runner, "chown", "-R", uid + ":" + gid, "/volumes/data", "/volumes/logs")
        if not resume:
            docker("exec", "-u", "root", runner, "chown", uid + ":" + gid, dump_path)
        workflow = spec["workflow_environment"]
        environment = {**{key: value for key, value in workflow.items() if not key.startswith(("ODOO_UPSTREAM_", "DATA_WORKFLOW_SSH_"))}, "ODOO_FILESTORE_PATH": "/volumes/data/filestore", "ODOO_FILESTORE_OWNER": uid + ":" + gid, "ENV_OVERRIDE_DISABLE_CRON": "true", "ODOO_RESTORE_KEPT_INTEGRATIONS": ""}
        arguments = ["exec", "-i"]
        for key, value in environment.items():
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
                raise RuntimeError("Invalid workflow environment key")
            arguments.extend(["-e", key + "=" + value])
        output = docker(*arguments, runner, "python3", "-", dump_path, database, spec["readback_spec"],
            base64.b64encode(spec["readback_program"].encode()).decode(), "resume" if resume else "fresh",
            spec["copy_binding_sha256"], stdin=spec["runner"].encode()).decode()
        markers = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        if any(markers.get(key) != "true" for key in ("testing_copy_sanitized", "testing_copy_pre_registry_sanitized", "odoo_module_update_completed", "odoo_module_update_modules_configured")):
            raise RuntimeError("Testing copy lacks sanitization or module update proof")
        readback = docker("exec", "-i", runner, "python3", "-", database, spec["readback_spec"], stdin=spec["readback_program"].encode()).decode()
        if "integration_readback_ok=true" not in readback.splitlines():
            raise RuntimeError("Testing copy integration readback did not pass")
        # Emit only the established value-free protocol markers, never runner logs.
        for line in output.splitlines():
            if re.fullmatch(r"(?:website_bootstrap_[a-z_]+|odoo_[a-z_]+)=(?:true|false|[0-9]+)", line):
                print(line)
        marker = "/volumes/data/.launchplane_integration_readback_passed"
        docker("exec", runner, "python3", "-c",
            "import hashlib,os;from pathlib import Path;Path(" + repr(marker) + ").write_text(hashlib.sha256((os.environ['ODOO_DB_NAME']+'\\n'+os.environ.get('ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64','')).encode()).hexdigest())")
        docker("exec", "-u", "root", runner, "rm", "-f", dump_path)
        docker("start", web)
        for marker in ("testing_copy_pre_registry_sanitized", "testing_copy_sanitized", "integration_readback_ok", "odoo_module_update_image_match", "odoo_restore_completed"):
            print(marker + "=true")
    finally:
        try:
            docker("exec", "-u", "root", runner, "rm", "-f", dump_path)
        finally:
            if staging is not None:
                shutil.rmtree(staging)


try:
    main()
except Exception:
    print("odoo_restore_completed=false")
    print("testing_copy_sanitized=false")
    # Failed copies and their stopped web remain for exact-generation recovery.
    sys.exit(1)
"""


def read_current_testing_generation(
    store: object,
    *,
    product: str,
    context: str,
    instance: str,
    bootstrap_preparation: bool = False,
) -> FreshOdooTestingGeneration | None:
    if instance != "testing":
        return None
    read = getattr(store, "read_fresh_odoo_testing_generation", None)
    if not callable(read):
        return None
    generation = read(product, context)
    if generation is None:
        return None
    if not isinstance(generation, FreshOdooTestingGeneration):
        raise ValueError("Fresh testing generation is unreadable.")
    if not bootstrap_preparation and not generation.reconcile_requested_at:
        raise ValueError("Fresh testing bootstrap has not committed its queue receipt.")
    target_id = getattr(store, "read_dokploy_target_id_record")(
        context_name=context, instance_name="testing"
    )
    if target_id.target_id != generation.target_id:
        raise ValueError("Fresh testing generation no longer owns the target.")
    profile = getattr(store, "read_product_profile_record")(product)
    if (
        not isinstance(profile, LaunchplaneProductProfileRecord)
        or profile.driver_id != "odoo"
        or not profile.is_active
    ):
        raise ValueError("Fresh testing generation no longer owns an active Odoo product.")
    require_distinct_testing_credentials(store, profile, context)
    allocation = getattr(store, "read_disposable_resource_preparation")(generation.operation_id)
    if (
        allocation is None
        or allocation.replacement_target_id != generation.target_id
        or allocation.resources_binding_sha256 != generation.resources_binding_sha256
        or allocation.resource_namespace != generation.namespace
    ):
        raise ValueError("Fresh testing generation lost its resource allocation.")
    target = getattr(store, "read_dokploy_target_record")(
        context_name=context, instance_name="testing"
    )
    if target.policies.staff_testing_hold is not None or target.pending_public_host_effects:
        raise ValueError("Fresh testing generation has a use or route hold.")
    values: dict[str, object] = {}
    for scope in ("context", "instance"):
        for runtime in getattr(store, "list_runtime_environment_records")(context_name=context):
            if runtime.scope == scope and runtime.instance == (
                "testing" if scope == "instance" else ""
            ):
                values.update(runtime.env)
    values.update(target.env)
    from control_plane.odoo_testing_resources import OdooTestingResources

    resources = OdooTestingResources(
        product=product,
        context=context,
        operation_id=generation.operation_id,
        target_id=generation.target_id,
        namespace=generation.namespace,
    )
    if any(values.get(key) != value for key, value in resources.volumes.items()):
        raise ValueError("Fresh testing volume authority changed before deployment.")
    if (
        values.get("ODOO_PROJECT_NAME") != generation.namespace
        or values.get("ODOO_STACK_NAME") != generation.namespace
    ):
        raise ValueError("Fresh testing project authority changed before deployment.")
    for other in getattr(store, "list_dokploy_target_id_records")():
        if other.target_id == generation.target_id and (other.context, other.instance) != (
            context,
            "testing",
        ):
            raise ValueError("Fresh testing target aliases another lane.")
    for other in getattr(store, "list_physical_provider_target_records")():
        if (
            other.provider_id == "dokploy"
            and other.target_id == generation.target_id
            and (other.context, other.instance) != (context, "testing")
        ):
            raise ValueError("Fresh testing provider target aliases another lane.")
    from control_plane.contracts.product_profile_record import (
        is_exclusive_product_context,
        product_context_owner_map,
    )

    if not is_exclusive_product_context(
        context=context,
        product=product,
        owners=product_context_owner_map(getattr(store, "list_product_profile_records")()),
    ):
        raise ValueError("Fresh testing context ownership changed.")
    return generation


FRESH_TESTING_LOCAL_CREDENTIAL_KEYS = frozenset(
    {"ODOO_ADMIN_PASSWORD", "ODOO_DB_PASSWORD", "ODOO_MASTER_PASSWORD"}
)
FRESH_TESTING_OMITTED_SETTINGS = frozenset(
    {
        "ODOO_WEB_COMMAND",
        "ODOO_KEY",
        "ODOO_SMTP_SERVER",
        "ODOO_SMTP_HOST",
        "ODOO_SMTP_PORT",
        "ODOO_SMTP_USER",
        "ODOO_SMTP_PASSWORD",
        "ODOO_SMTP_SSL",
    }
)


def narrow_fresh_testing_requirements(
    profile: LaunchplaneProductProfileRecord, *, context: str, omitted_keys: frozenset[str]
) -> LaunchplaneProductProfileRecord:
    """Keep existing models; scope shared requirements to their other stable lanes.

    This changes only the testing lane's effective required keys. Production's
    existing requirements, owner-input descriptions and secret bindings remain.
    """
    testing = next(
        lane for lane in profile.lanes if (lane.context, lane.instance) == (context, "testing")
    )

    def narrow(requirements: tuple[Requirement, ...], *, secret: bool) -> tuple[Requirement, ...]:
        retained = []
        generated = []
        for requirement in requirements:
            key = str(getattr(requirement, "binding_key" if secret else "key"))
            if key not in omitted_keys or not product_config_requirement_applies_to_lane(
                requirement_context=requirement.context,
                requirement_instance=requirement.instance,
                lane=testing,
            ):
                retained.append(requirement)
                continue
            for lane in profile.lanes:
                if lane == testing or not product_config_requirement_applies_to_lane(
                    requirement_context=requirement.context,
                    requirement_instance=requirement.instance,
                    lane=lane,
                ):
                    continue
                generated.append(
                    requirement.model_copy(
                        update={"context": lane.context, "instance": lane.instance}
                    )
                )

        def identity(requirement: Requirement) -> tuple[str, str, str, str]:
            return (
                str(getattr(requirement, "integration", "")),
                str(getattr(requirement, "binding_key" if secret else "key")),
                requirement.context,
                requirement.instance,
            )

        result = {identity(requirement): requirement for requirement in retained}
        for requirement in generated:
            result.setdefault(identity(requirement), requirement)
        return tuple(result.values())

    expected = profile.expected_config.model_copy(
        update={
            "runtime_environment_keys": narrow(
                profile.expected_config.runtime_environment_keys, secret=False
            ),
            "managed_secret_bindings": narrow(
                profile.expected_config.managed_secret_bindings, secret=True
            ),
        }
    )
    result = profile.model_copy(update={"expected_config": expected})
    return LaunchplaneProductProfileRecord.model_validate(
        result.model_dump(mode="json")
    ).validate_write_contract()


def require_distinct_testing_credentials(
    store: object, profile: LaunchplaneProductProfileRecord, context: str
) -> None:
    """Opaque source preflight inside the service; no value or hash is transported.

    Use existing managed versions, not secret names/version IDs as value proof.
    This reads control-plane source authority and never writes a secret, binding,
    production target or credential. The supported copy runner separately strips
    credentials from the restored database before registry code executes.
    """
    import hmac
    from typing import cast
    from control_plane import secrets

    production = [lane for lane in profile.lanes if lane.instance == "prod"]
    if len(production) != 1:
        raise ValueError("A unique production source is required for credential comparison.")
    source = production[0]
    reader = cast(secrets.SecretReadStore, store)
    testing_values = secrets.resolve_secret_values_for_integration_from_store(
        record_store=reader,
        integration=secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
        context_name=context,
        instance_name="testing",
        scopes=frozenset({"context_instance"}),
    )
    source_values = secrets.resolve_secret_values_for_integration_from_store(
        record_store=reader,
        integration=secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
        context_name=source.context,
        instance_name="prod",
        scopes=frozenset({"context", "context_instance"}),
    )
    if any(not testing_values.get(key) for key in FRESH_TESTING_LOCAL_CREDENTIAL_KEYS) or any(
        not source_values.get(key) for key in FRESH_TESTING_LOCAL_CREDENTIAL_KEYS
    ):
        raise ValueError(
            "Credential comparison needs the existing testing and production source versions."
        )
    if any(
        hmac.compare_digest(testing_values[key].encode(), value.encode())
        for key in FRESH_TESTING_LOCAL_CREDENTIAL_KEYS
        for value in source_values.values()
        if value
    ):
        raise ValueError("Existing testing credentials overlap the supported production source.")


def fresh_testing_authority_sha256(store: object, generation: FreshOdooTestingGeneration) -> str:
    """Bind rendered values to recipient and supported-source version metadata."""
    from typing import cast
    from control_plane import secrets
    from control_plane.disposable_lanes import DisposableLaneStore, disposable_lane_configuration
    from control_plane.contracts.canonical_json import canonical_json_sha256

    profile = getattr(store, "read_product_profile_record")(generation.product)
    production = [lane for lane in profile.lanes if lane.instance == "prod"]
    if len(production) != 1:
        raise ValueError("Fresh testing source authority is unavailable.")
    metadata = list(
        disposable_lane_configuration(
            cast(DisposableLaneStore, store), generation.context, "testing"
        )
    )
    selected = secrets.resolve_effective_secret_bindings_from_store(
        record_store=cast(secrets.SecretReadStore, store),
        integration=secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
        context_name=production[0].context,
        instance_name="prod",
        scopes=frozenset({"context", "context_instance"}),
    )
    for binding, record in selected.values():
        metadata.extend((binding, record))
    payloads = [item.model_dump(mode="json") for item in metadata]
    secret_ids = {
        getattr(item, "secret_id") for item in metadata if hasattr(item, "current_version_id")
    }
    for secret_id in secret_ids:
        record = getattr(store, "read_secret_record")(secret_id)
        version = getattr(store, "read_secret_version")(record.current_version_id)
        if version.secret_id != record.secret_id:
            raise ValueError("Fresh testing secret version provenance is inconsistent.")
        payloads.append(version.model_dump(mode="json"))
    override = None
    try:
        override = getattr(store, "read_odoo_instance_override_record")(
            context_name=generation.context, instance_name="testing"
        )
    except FileNotFoundError:
        pass
    return canonical_json_sha256(
        {
            "profile": profile.model_dump(mode="json"),
            "generation": generation.model_dump(
                mode="json", exclude={"completed_at", "reconcile_requested_at"}
            ),
            "target": getattr(store, "read_dokploy_target_record")(
                context_name=generation.context, instance_name="testing"
            ).model_dump(mode="json"),
            "target_id": generation.target_id,
            "configuration": sorted(payloads, key=lambda item: json.dumps(item, sort_keys=True)),
            "overrides": override.model_dump(mode="json") if override is not None else None,
        }
    )
