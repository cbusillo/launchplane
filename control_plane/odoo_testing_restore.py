"""Host-side source capture and pre-registry sanitization for a fresh testing copy."""

import base64
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from control_plane.odoo_connection_isolation import connection_isolation_library


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
    prepared_at: str
    completed_at: str = ""


# This wrapper uses the artifact's SQL-only credential boundary before the first
# registry process. The runner gets no upstream or SSH settings. Additional copy
# connection rules run in that same transaction boundary and again after hooks.
# A missing boundary is an unsupported artifact, never permission to run it.
TESTING_COPY_RUNNER = (
    """import logging
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
        print("testing_copy_pre_registry_sanitized=true", flush=True)


if not callable(getattr(OdooDataWorkflowRunner, "prepare_credentials_before_registry", None)):
    raise SystemExit("Artifact lacks the pre-registry credential boundary")
settings = LocalServerSettings()
runner = CopyRunner(settings, None, None)
runner.acquire_data_workflow_lock()
try:
    dump = Path(sys.argv[1])
    subprocess.run(["pg_restore", "--file=/dev/null", str(dump)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    runner._restore_from_verified_dump(dump, do_sanitize=True)
    runner.prepare_credentials_before_registry(restored_copy=True, do_sanitize=True)
    print("testing_copy_sanitized=true", flush=True)
finally:
    runner.release_data_workflow_lock()
"""
)


def encoded_testing_copy_runner() -> str:
    return base64.b64encode(TESTING_COPY_RUNNER.encode()).decode()


def generation_binding(generation: FreshOdooTestingGeneration) -> str:
    from control_plane.contracts.canonical_json import canonical_json_sha256

    return canonical_json_sha256(generation.model_dump(mode="json", exclude={"completed_at"}))


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
        "runner": TESTING_COPY_RUNNER,
        "workflow_environment": workflow_environment,
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
            if any(environment.get(key) for key in ("GITHUB_TOKEN", "GH_TOKEN", "SSH_AUTH_SOCK", "DATA_WORKFLOW_SSH_KEY", "DATA_WORKFLOW_SSH_DIR")):
                raise RuntimeError("Testing holds an execution credential")
        if service != "web":
            networks = container.get("NetworkSettings", {}).get("Networks")
            if not isinstance(networks, dict) or len(networks) != 1:
                raise RuntimeError("Testing data containers are not privately networked")
            network = one_inspection(next(iter(networks)))
            if network.get("Internal") is not True:
                raise RuntimeError("Testing data network has outbound access")
    for container in all_containers:
        if container in [item[1] for item in containers.values()]:
            continue
        if any(mount.get("Name") in volumes for mount in container.get("Mounts", [])):
            raise RuntimeError("Testing storage is shared by another container")
    # Verify local volume identities after Docker has mounted the qualified set.
    for name in volumes:
        volume = json.loads(docker("volume", "inspect", name))[0]
        if volume.get("Driver") != "local" or volume.get("Options") not in (None, {}) or volume.get("Labels", {}).get("io.launchplane.testing-resources") != gen["resources_binding_sha256"]:
            raise RuntimeError("Testing backing storage changed")
    runner = containers["script-runner"][0]
    web = containers["web"][0]
    docker("stop", web)
    print("testing_copy_runtime_isolated=true", flush=True)
    present = docker("exec", "-i", containers["database"][0], "sh", "-c",
        'psql -X -q -t -A -v ON_ERROR_STOP=1 -v database_name="$1" -U "$POSTGRES_USER" -d postgres',
        "_", database, stdin=b"SELECT count(*) FROM pg_database WHERE datname = :'database_name';\n").decode().strip()
    if present != "0":
        raise RuntimeError("Fresh restore cannot overwrite an existing testing database")
    source = spec["source"]
    for key in ("ODOO_UPSTREAM_HOST", "ODOO_UPSTREAM_USER", "ODOO_UPSTREAM_DB_USER", "ODOO_UPSTREAM_DB_NAME"):
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.:-]*", source[key]):
            raise RuntimeError("Unsupported restore source coordinate")
    path = source["ODOO_UPSTREAM_FILESTORE_PATH"]
    if not path.startswith("/") or ".." in Path(path).parts or not re.fullmatch(r"[A-Za-z0-9_./-]+", path):
        raise RuntimeError("Unsupported restore source filestore")
    destination = source["ODOO_UPSTREAM_USER"] + "@" + source["ODOO_UPSTREAM_HOST"]
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes"]
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
    dump_path = "/tmp/" + namespace + "-database.dump"
    docker("cp", str(dump), runner + ":" + dump_path)
    docker("exec", "-u", "root", runner, "chmod", "644", dump_path)
    workflow = spec["workflow_environment"]
    environment = {**{key: value for key, value in workflow.items() if not key.startswith(("ODOO_UPSTREAM_", "DATA_WORKFLOW_SSH_"))}, "ODOO_FILESTORE_PATH": "/volumes/data/filestore", "ENV_OVERRIDE_DISABLE_CRON": "true", "ODOO_RESTORE_KEPT_INTEGRATIONS": ""}
    arguments = ["exec", "-i", "-u", "root"]
    for key, value in environment.items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise RuntimeError("Invalid workflow environment key")
        arguments.extend(["-e", key + "=" + value])
    output = docker(*arguments, runner, "python3", "-", dump_path, stdin=spec["runner"].encode()).decode()
    if "testing_copy_sanitized=true" not in output.splitlines() or "testing_copy_pre_registry_sanitized=true" not in output.splitlines():
        raise RuntimeError("Testing copy lacks sanitization proof")
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
    shutil.rmtree(staging)
    docker("start", web)
    for marker in ("testing_copy_pre_registry_sanitized", "testing_copy_sanitized", "integration_readback_ok", "odoo_module_update_completed", "odoo_module_update_image_match", "odoo_module_update_modules_configured", "odoo_restore_completed"):
        print(marker + "=true")


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
    target_id = getattr(store, "read_dokploy_target_id_record")(
        context_name=context, instance_name="testing"
    )
    if target_id.target_id != generation.target_id:
        raise ValueError("Fresh testing generation no longer owns the target.")
    allocation = getattr(store, "read_disposable_resource_preparation")(generation.operation_id)
    if (
        allocation is None
        or allocation.replacement_target_id != generation.target_id
        or allocation.resources_binding_sha256 != generation.resources_binding_sha256
        or allocation.resource_namespace != generation.namespace
    ):
        raise ValueError("Fresh testing generation lost its resource allocation.")
    return generation
